"""CUDA image processing and calibrated geometry for station inspection.

Image-sized computations stay on CUDA. Only small geometry, scores and final
decisions are returned to Python. The calibrated reference uses the same DISK
features as live frames. GPU failures raise; this module has no CPU fallback.
"""
from pathlib import Path
from functools import lru_cache
import math
import numpy as np
import torch
from torch.nn import functional as F
import kornia as K
from torchvision.io import decode_jpeg, ImageReadMode
from .gpu import require_cuda
from .video import AnalysisError

ROOT=Path(__file__).resolve().parents[1]
FEATURE_WEIGHTS=ROOT/'models/disk-depth-v1.pth'
FEATURE_ASSET='models/disk-depth-v1.pth'


class CudaFrame:
    def __init__(self,rgb):
        if not rgb.is_cuda or rgb.ndim!=3 or rgb.shape[0]!=3:
            raise ValueError('Expected a CUDA RGB frame')
        self.rgb=rgb
        self.shape=(rgb.shape[1],rgb.shape[2],3)


def decode(payload):
    device=torch.device('cuda',require_cuda())
    encoded=torch.frombuffer(bytearray(payload),dtype=torch.uint8)
    return CudaFrame(decode_jpeg(encoded,mode=ImageReadMode.RGB,device=device))


def rgb(image):
    if isinstance(image,CudaFrame):return image.rgb
    if isinstance(image,torch.Tensor):
        if not image.is_cuda:raise ValueError('CPU tensors are not an inference backend')
        return image
    # Offline video readers supply BGR; the upload is the input boundary.
    return torch.as_tensor(np.ascontiguousarray(image),device=f'cuda:{require_cuda()}').permute(2,0,1).flip(0)


def gray(image):
    if image.dtype==torch.uint8:
        x=image.to(torch.int32)
        return ((x.select(-3,0)*4899+x.select(-3,1)*9617+x.select(-3,2)*1868+8192)>>14).to(torch.uint8)
    return image.select(-3,0)*.299+image.select(-3,1)*.587+image.select(-3,2)*.114


def resize(image,size,area=False):
    """RGB CHW or grayscale HW; size follows OpenCV's (width,height) order."""
    if tuple(image.shape[-2:])==(size[1],size[0]):return image
    mono=image.ndim==2
    batched=image.ndim==4
    x=image[None,None] if mono else image if batched else image[None]
    out=F.interpolate(x.float(),size=(size[1],size[0]),mode='area' if area else 'bilinear',
                      **({} if area else {'align_corners':False}))
    out=out[0,0] if mono else out if batched else out[0]
    return out.add(.5).floor().clamp(0,255).to(torch.uint8) if image.dtype==torch.uint8 else out


def transform(points,matrix):
    points=torch.as_tensor(points,device=matrix.device,dtype=matrix.dtype)
    homogeneous=torch.cat((points,torch.ones_like(points[...,:1])),dim=-1)
    result=homogeneous@matrix.T
    return result[...,:2]/result[...,2:]


def inside(points,polygon):
    polygon=torch.as_tensor(polygon,device=points.device,dtype=points.dtype)
    a=polygon;b=polygon.roll(-1,0)
    x,y=points[:,0,None],points[:,1,None]
    cross=((a[:,1]>y)!=(b[:,1]>y))&(x<(b[:,0]-a[:,0])*(y-a[:,1])/(b[:,1]-a[:,1]+1e-12)+a[:,0])
    return cross.sum(1).remainder(2).bool()


def polygon_area(points):
    return ((points[:,0]*points.roll(-1,0)[:,1]-points[:,1]*points.roll(-1,0)[:,0]).sum()/2).abs()


def warp(image,matrices,size):
    """Batched perspective warp with OpenCV's 1/32-pixel bilinear table.

    matrices map source pixels to destination pixels; border pixels are zero.
    GPU int-coordinate gathers preserve the uint8 inspection input contract.
    """
    matrices=torch.as_tensor(matrices,device=image.device,dtype=torch.float64)
    if matrices.ndim==2:matrices=matrices[None]
    inverse=torch.linalg.inv(matrices)
    width,height=size
    yy,xx=torch.meshgrid(torch.arange(height,device=image.device,dtype=torch.float64),
                         torch.arange(width,device=image.device,dtype=torch.float64),indexing='ij')
    grid=torch.stack((xx.flatten(),yy.flatten(),torch.ones(width*height,device=image.device,dtype=torch.float64)))
    projected=inverse@grid
    coordinates=projected[:,:2]/projected[:,2:]
    fixed=(coordinates*32).round().long()
    x0,y0=fixed[:,0]//32,fixed[:,1]//32
    ax,ay=fixed[:,0].remainder(32),fixed[:,1].remainder(32)
    ih,iw=image.shape[-2:]
    channels=image[None] if image.ndim==2 else image
    flat=channels.flatten(1)
    value=torch.zeros((len(matrices),len(channels),width*height),device=image.device,dtype=torch.float32)
    for dx,dy,weight in ((0,0,(32-ax)*(32-ay)),(1,0,ax*(32-ay)),(0,1,(32-ax)*ay),(1,1,ax*ay)):
        x,y=x0+dx,y0+dy
        valid=(x>=0)&(x<iw)&(y>=0)&(y<ih)
        index=(y.clamp(0,ih-1)*iw+x.clamp(0,iw-1)).flatten()
        pixels=flat[:,index].view(len(channels),len(matrices),-1).permute(1,0,2)
        value+=pixels.float()*(weight*valid)[:,None]
    value=value/1024
    if image.dtype==torch.uint8:value=value.add(.5).floor().clamp(0,255).to(torch.uint8)
    return value.reshape(len(matrices),len(channels),height,width)


def correlation(image,reference,mask=None):
    """Maximum zero-mean normalized correlation over every valid offset."""
    x=image.float();template=reference.float()
    if mask is None:mask=torch.ones_like(template)
    else:mask=mask.float()
    if x.shape[-2]<template.shape[-2] or x.shape[-1]<template.shape[-1]:
        return x.new_tensor(float('-inf'))
    count=mask.sum().clamp_min(1)
    reference_centered=(template-(template*mask).sum()/count)*mask
    kernel=mask[None,None]
    patch_sum=F.conv2d(x[None,None],kernel)
    patch_square=F.conv2d(x.square()[None,None],kernel)
    numerator=F.conv2d(x[None,None],reference_centered[None,None])
    variance=(patch_square-patch_sum.square()/count).clamp_min(0)
    denominator=(variance*reference_centered.square().sum()).sqrt()
    score=numerator/denominator.clamp_min(1e-12)
    return torch.where(denominator>1e-6,score,torch.zeros_like(score)).amax()


@lru_cache(maxsize=8)
def _region_grid(device, regions):
    grids=[]
    for x0,y0,x1,y1 in regions:
        yy,xx=torch.meshgrid(torch.arange(y0,y1,device=device,dtype=torch.float64),
                            torch.arange(x0,x1,device=device,dtype=torch.float64),indexing='ij')
        grids.append(torch.stack((xx.flatten(),yy.flatten(),torch.ones(xx.numel(),device=device,dtype=torch.float64))))
    grid=torch.cat(grids,dim=1)
    ready=torch.cuda.Event();ready.record(torch.cuda.current_stream(grid.device))
    return grid,ready


def warp_regions(image,matrix,size,regions):
    """Exactly the requested slices of warp(), without constructing the full image.

    Coordinates remain absolute destination pixels, so perspective division and
    the 1/32-pixel interpolation table match a full warp followed by slicing.
    """
    width,height=size
    boxes=[]
    for a,b,c,d in regions:
        x0,x1,_=slice(a,c).indices(width);y0,y1,_=slice(b,d).indices(height)
        boxes.append((x0,y0,max(x0,x1),max(y0,y1)))
    if not boxes:return []
    grid,ready=_region_grid(image.device,tuple(boxes))
    consumer=torch.cuda.current_stream(image.device)
    consumer.wait_event(ready)
    # A cached grid can be evicted while another detector stream still reads it.
    grid.record_stream(consumer)
    matrix=torch.as_tensor(matrix,device=image.device,dtype=torch.float64)
    inverse=torch.linalg.inv(matrix)
    projected=inverse@grid
    fixed=(projected[:2]/projected[2:]*32).round().long()
    x0,y0=fixed[0]//32,fixed[1]//32
    ax,ay=fixed[0].remainder(32),fixed[1].remainder(32)
    ih,iw=image.shape[-2:]
    channels=image[None] if image.ndim==2 else image
    flat=channels.flatten(1)
    value=torch.zeros((len(channels),grid.shape[1]),device=image.device,dtype=torch.float32)
    for dx,dy,weight in ((0,0,(32-ax)*(32-ay)),(1,0,ax*(32-ay)),(0,1,(32-ax)*ay),(1,1,ax*ay)):
        x,y=x0+dx,y0+dy
        valid=(x>=0)&(x<iw)&(y>=0)&(y<ih)
        index=y.clamp(0,ih-1)*iw+x.clamp(0,iw-1)
        value+=flat[:,index].float()*(weight*valid)[None]
    value=value/1024
    if image.dtype==torch.uint8:value=value.add(.5).floor().clamp(0,255).to(torch.uint8)
    result=[];offset=0
    for a,b,c,d in boxes:
        count=(c-a)*(d-b)
        result.append(value[:,offset:offset+count].reshape(len(channels),d-b,c-a))
        offset+=count
    return result


def _fit_homography(source,target,weights=None):
    """Normalized 8-parameter DLT, all candidate systems solved on CUDA."""
    if source.ndim==2:source,target=source[None],target[None]
    x,y=source.unbind(-1);u,v=target.unbind(-1)
    z=torch.zeros_like(x);o=torch.ones_like(x)
    a=torch.stack((torch.stack((x,y,o,z,z,z,-u*x,-u*y),-1),
                   torch.stack((z,z,z,x,y,o,-v*x,-v*y),-1)),-2).flatten(-3,-2)
    b=torch.stack((u,v),-1).flatten(-2)[...,None]
    if weights is not None:
        weight=weights.repeat_interleave(2,dim=-1)[...,None]
        a=a*weight;b=b*weight
    ata=a.transpose(-1,-2)@a
    # Regularize only enough to avoid singular bad samples. Those samples are
    # still rejected by the geometric inlier test rather than accepted blindly.
    ata=ata+torch.eye(8,device=a.device,dtype=a.dtype)*1e-10
    solution,info=torch.linalg.solve_ex(ata,a.transpose(-1,-2)@b)
    matrix=torch.cat((solution[...,0],torch.ones_like(solution[...,:1,0])),-1).view(-1,3,3)
    return matrix,info


def homography(source,target,threshold=2.,seed=7301):
    if len(source)<14:return None
    dtype=torch.float64
    # One scale and translation for both coordinate systems; unit-sized values
    # keep DLT conditioning independent of source image resolution.
    center=(source.mean(0)+target.mean(0))/2
    scale=torch.cat((source,target)).std(0).mean().clamp_min(1)
    src=(source-center).to(dtype)/scale
    dst=(target-center).to(dtype)/scale
    generator=torch.Generator(device=source.device);generator.manual_seed(seed)
    indices=torch.rand((1024,len(source)),device=source.device,generator=generator).topk(4,largest=False).indices
    candidates,info=_fit_homography(src[indices],dst[indices])
    p=torch.cat((src,torch.ones_like(src[:,:1])),-1)
    projected=p@candidates.transpose(-1,-2)
    error=((projected[...,:2]/projected[...,2:]-dst).square().sum(-1))*scale.square()
    inliers=(error<=threshold**2)&(info==0)[:,None]&torch.isfinite(error)
    count=inliers.sum(-1)
    best=count.argmax();mask=inliers[best]
    if int(count[best])<12 or float(mask.float().mean())<.3:return None
    matrix=candidates[best]
    for _ in range(3):
        fitted,info=_fit_homography(src,dst,mask[None].to(dtype))
        matrix=fitted[0]
        q=p@matrix.T
        mask=((q[:,:2]/q[:,2:]-dst).square().sum(1)*scale.square()<=threshold**2)
    if int(mask.sum())<12 or float(mask.float().mean())<.3 or not bool(torch.isfinite(matrix).all()):return None
    norm=torch.eye(3,device=source.device,dtype=dtype)
    norm[0,0]=norm[1,1]=1/scale;norm[:2,2]=-center/scale
    actual=torch.linalg.inv(norm)@matrix@norm
    return actual/actual[2,2],mask


class CudaRegistration:
    def __init__(self,count=2200):
        self.device=torch.device('cuda',require_cuda())
        if not FEATURE_WEIGHTS.is_file():raise AnalysisError('GPU_VISION_MISSING','缺少 CUDA 定位权重 disk-depth-v1.pth')
        self.model=K.feature.DISK().to(self.device).eval()
        self.model.load_state_dict(torch.load(FEATURE_WEIGHTS,map_location=self.device,weights_only=True)['extractor'])
        self.model.half()
        self.count=count

    @torch.inference_mode()
    def extract(self,image,size,regions=None,area=False):
        image=resize(rgb(image),size,area)
        # Kornia 0.8.3's n-limited selector calls kthvalue(k=0) when the
        # heatmap contains no positive peaks. Empty frames are valid inputs.
        result=self.model(image[None].half()/255,n=None,pad_if_not_divisible=True)[0]
        points,desc=result.keypoints.float(),result.descriptors.float()
        scores=result.detection_scores
        if len(scores):
            threshold=torch.kthvalue(-scores,min(self.count+1,len(scores))).values
            selected=torch.nonzero(scores>-threshold,as_tuple=True)[0][:self.count]
            points,desc=points[selected],desc[selected]
        keep=torch.isfinite(desc).all(1)
        if regions is not None:
            membership=torch.zeros(len(points),device=self.device,dtype=torch.bool)
            for polygon in regions:membership|=inside(points,polygon)
            keep&=membership
        return points[keep],desc[keep]

    @torch.inference_mode()
    def match(self,reference,current):
        points,desc=reference;live_points,live_desc=current
        if len(desc)<14 or len(live_desc)<2:return None
        distance=torch.cdist(desc,live_desc)
        scores,indices=distance.topk(2,largest=False)
        keep=(scores[:,0]<.72*scores[:,1])&torch.isfinite(scores).all(1)
        source,target=points[keep],live_points[indices[keep,0]]
        fitted=homography(source,target)
        if fitted is None:return None
        matrix,inliers=fitted
        return dict(matrix=matrix,source=source,inliers=inliers)
