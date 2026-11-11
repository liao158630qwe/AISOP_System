"""CUDA features compatible with the trained 48px OpenCV HOG action classifier."""
from functools import lru_cache
import torch
from torch.nn import functional as F
from . import cuda_vision as v


def foreground(image, reference):
    """One CHW patch or a batch of NCHW patches, with identical per-pixel rules."""
    single=image.ndim==3
    if single:image,reference=image[None],reference[None]
    def sv(rgb):
        rgb=rgb.to(torch.int32)
        value=rgb.amax(1);diff=value-rgb.amin(1)
        table=((255<<12)/value.clamp_min(1).float()).round().to(torch.int32)
        saturation=(diff*table+2048)>>12
        return saturation,value
    s,value=sv(image);rs,rv=sv(reference)
    delta=(image.to(torch.int32)-reference.to(torch.int32)).square().sum(1)
    mask=((delta>45**2)&((value>110)|(s>70))&(((value-rv).abs()>28)|(s>rs+35))).float()
    eroded=-F.max_pool2d(F.pad(-mask[:,None],(2,2,2,2),value=-1),5,stride=1)
    opened=F.max_pool2d(eroded,5,stride=1,padding=2)[:,0]
    return opened[0] if single else opened


@lru_cache(maxsize=4)
def _hog_weights(device):
    y,x=torch.meshgrid(torch.arange(16,device=device),torch.arange(16,device=device),indexing='ij')
    gaussian=torch.exp(-((x-8).square()+(y-8).square())/32)
    cx,cy=(x+.5)/8-.5,(y+.5)/8-.5
    return torch.stack([(1-(cx-a).abs()).clamp_min(0)*(1-(cy-b).abs()).clamp_min(0)*gaussian
                        for a in range(2) for b in range(2)]).flatten(1)


def hog(gray):
    """Batched N×48×48 -> N×900, same block/cell ordering and L2Hys."""
    padded=F.pad(gray[:,None].float(),(1,1,1,1),mode='reflect')[:,0]
    dx=padded[:,1:-1,2:]-padded[:,1:-1,:-2]
    dy=padded[:,2:,1:-1]-padded[:,:-2,1:-1]
    ax,ay=dx.abs(),dy.abs();c=torch.minimum(ax,ay)/torch.maximum(ax,ay).clamp_min(1e-12)
    c2=c*c
    angle=((((-.04432655554792128*c2+.1555786518463281)*c2-.3258083974640975)*c2+.9997878412794807)*c)*(180/torch.pi)
    angle=torch.where(ax>=ay,angle,90-angle)
    angle=torch.where(dx<0,180-angle,angle);angle=torch.where(dy<0,360-angle,angle)
    bins=angle*(9/180)-.5;lo=bins.floor().long();fraction=bins-lo
    magnitude=(dx*dx+dy*dy).sqrt()
    angular=torch.zeros((len(gray),9,48,48),device=gray.device)
    angular.scatter_add_(1,lo.remainder(9)[:,None],(magnitude*(1-fraction))[:,None])
    angular.scatter_add_(1,(lo+1).remainder(9)[:,None],(magnitude*fraction)[:,None])
    patches=F.unfold(angular,16,stride=8).reshape(len(gray),9,256,25)
    weights=_hog_weights(gray.device)
    hist=torch.einsum('nbpk,cp->nkcb',patches,weights).reshape(len(gray),5,5,36).transpose(1,2)
    hist=(hist/(hist.square().sum(-1,keepdim=True).sqrt()+3.6)).clamp_max(.2)
    hist=hist/(hist.square().sum(-1,keepdim=True).sqrt()+1e-3)
    return hist.reshape(len(gray),900)


def features(crops,references):
    batch=torch.stack(crops)
    masks=foreground(batch,torch.stack(references))
    small=v.resize(batch*masks.to(torch.uint8)[:,None],(48,48))
    gray=v.gray(small)
    color=small.flip(1).permute(0,2,3,1).flatten(1).float()/255
    occupancy=v.resize(masks[:,None],(18,18),area=True).flatten(1)
    return torch.cat((color,hog(gray),occupancy),1),list(masks.unbind(0))


def observation_metadata(crops,masks):
    """Build each site's foreground mean and temporal signature on the device."""
    fractions=torch.stack(masks).flatten(1).mean(1,keepdim=True)
    signatures=v.resize(v.gray(torch.stack(crops))[:,None],(16,16)).flatten(1)
    return torch.cat((fractions,signatures.float()),1)


def crop(image,target,side=180):
    x,y=map(round,target['point']);half=side/2
    return image[:,max(0,round(y-half)):round(y+half),max(0,round(x-half)):round(x+half)]
