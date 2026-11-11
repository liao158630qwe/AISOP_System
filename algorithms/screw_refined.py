"""Current-frame, local screw inspection; no all-component prerequisite."""
import time
import cv2
import numpy as np
import torch
from . import cuda_vision as vision
from .cuda_yolo import predict as predict_cuda

from .screws import ROOT, snapshot, verify


class RefinedScrewDetector:
    def __init__(self,frozen=None,model=None,registration=None):
        from ultralytics import YOLO
        import torch
        self.frozen=frozen or snapshot();verify(self.frozen)
        self.config=self.frozen['config']
        self.reference=cv2.imread(str(ROOT/self.config['reference']))
        self.reference_gpu=vision.rgb(self.reference)
        self.reference_gray_gpu=vision.gray(self.reference_gpu)
        self.reference_small=vision.resize(self.reference_gray_gpu,(960,540))
        self.registration_size=(960,540)
        self.registration=registration or vision.CudaRegistration()
        regions=self.config.get('registration_regions',[[[680,180],[900,200],[990,990],[580,1000],[500,680]]])
        polygons=[np.round(np.array(p)/2).tolist() for p in regions]
        self.features=self.registration.extract(self.reference_gpu,self.registration_size,polygons,area=True)
        self.model=model if model is not None else YOLO(str(ROOT/self.config['model']))
        # An override can name the primary weights again. Share that model so
        # the same network is neither loaded twice nor invoked for a singleton.
        primary=(ROOT/self.config['model']).resolve()
        self.target_models={path:self.model if (ROOT/path).resolve()==primary else YOLO(str(ROOT/path))
                            for path in set(self.config.get('target_models',{}).values())}
        from .gpu import require_cuda
        self.device=require_cuda()

    def extract(self,image):
        return self.registration.extract(image,self.registration_size,area=True)

    @torch.inference_mode()
    def localize(self,key,desc):
        fitted=self.registration.match(self.features,(key,desc))
        if fitted is None:return None
        matrix=fitted['matrix'];scale=matrix.new_tensor([[2.,0,0],[0,2.,0],[0,0,1.]])
        H=scale@matrix@torch.linalg.inv(scale)
        bounds=self.config.get('product_polygon',[[650,200],[930,200],[980,1000],[560,1000]])
        polygon=vision.transform(bounds,H)
        if not bool(torch.isfinite(polygon).all()) or not 70000<float(vision.polygon_area(polygon))<700000:return None
        regions=self.config.get('component_regions',[
            [[739,253],[780,255],[858,704],[828,718],[704,346]],
            [[680,590],[725,600],[905,920],[875,930],[665,670]],
            [[706,480],[748,478],[808,602],[778,618]]])
        counts=[int((vision.inside(fitted['source']*2,p)&fitted['inliers']).sum()) for p in regions]
        # Only small geometry/decision metadata crosses to Python.
        return dict(matrix=matrix,H=H.cpu().numpy(),inliers=int(fitted['inliers'].sum()),componentInliers=counts)

    @torch.inference_mode()
    def inspect(self,image,registration=None):
        started=time.perf_counter()
        targets=[dict(**t,state='UNKNOWN',score=None,bbox=None,reason='当前帧未可靠定位') for t in self.config['targets']]
        result={'targets':targets,'registered':False,'inliers':0,'reason':'当前帧未可靠定位','algorithm':'local-roi-v2'}
        def finish():
            result['inferenceMs']=round((time.perf_counter()-started)*1000,2)
            return result
        image_gpu=vision.rgb(image)
        small=vision.resize(image_gpu,self.registration_size,area=True)
        if registration is None:
            key,desc=self.extract(image_gpu)
            registration=self.localize(key,desc)
        if registration is None:return finish()
        matrix,H=registration['matrix'],registration['H']
        result['inliers']=registration['inliers']
        component_counts=registration['componentInliers']
        result['componentInliers']=component_counts
        result['localized']=True
        inv=np.linalg.inv(H)
        h,w=image.shape[:2]
        detail=min(2.,max(1.,min(w/1920,h/1080)))
        normalize=np.diag([1920/w,1080/h,1.])
        detail_matrix=np.diag([detail,detail,1.])
        side=round(80*detail)
        origins=[];contexts=[];transforms=[]
        context_mask=torch.ones((48,48),device=image_gpu.device);context_mask[12:36,12:36]=0
        for target in targets:
            x,y=target['point'];x0,y0=round(x)-40,round(y)-40
            region=cv2.perspectiveTransform(np.float32([[[x0,y0],[x0+80,y0],[x0+80,y0+80],[x0,y0+80]]]),H)[0]
            target['searchRoi']=[float(region[:,0].min()/1920),float(region[:,1].min()/1080),
                                 float(region[:,0].max()/1920),float(region[:,1].max()/1080)]
            move=np.array([[1.,0.,-x0],[0.,1.,-y0],[0.,0.,1.]])
            transforms.append(detail_matrix@move@inv@normalize);origins.append((x0,y0))
        crops=vision.warp(image_gpu,np.stack(transforms),(side,side))
        locals_=vision.gray(vision.resize(crops,(80,80)))[:,16:64,16:64].float()
        refs=torch.stack([self.reference_gray_gpu[y+16:y+64,x+16:x+64] for x,y in origins]).float()
        count=context_mask.sum()
        locals_=(locals_-(locals_*context_mask).sum((1,2),keepdim=True)/count)*context_mask
        refs=(refs-(refs*context_mask).sum((1,2),keepdim=True)/count)*context_mask
        denominator=(locals_.square().sum((1,2))*refs.square().sum((1,2))).sqrt()
        contexts=((locals_*refs).sum((1,2))/denominator.clamp_min(1e-12)).cpu().tolist()
        for target,score in zip(targets,contexts):
            target.update(contextScore=round(score,4),reason='当前帧未检出可靠螺丝头或空孔')
        # Run each crop only on its selected model, once at the lowest required
        # threshold. High-confidence NMS results do not depend on lower-scoring
        # boxes. Keep the original two-stage assignment/claiming rules below.
        base_threshold=float(self.config.get('threshold',.45))
        overrides={str(k):float(v) for k,v in (self.config.get('target_thresholds') or {}).items()}
        predictions=self.predict_crops(crops,targets,base_threshold,overrides)
        lengths=[len(p.boxes.data) for p in predictions]
        rows=torch.cat([p.boxes.data for p in predictions]).cpu().split(lengths)
        rows=[r.tolist() for r in rows]
        candidates=[]
        for detections,(x0,y0) in zip(rows,origins):
            for *raw,confidence,cls in detections:
                if confidence < base_threshold:continue
                box=[x0+raw[0]/detail,y0+raw[1]/detail,x0+raw[2]/detail,y0+raw[3]/detail]
                candidates.append((float(confidence),int(cls),(box[0]+box[2])/2,(box[1]+box[3])/2,box))
        unique=[]
        for item in sorted(candidates,reverse=True):
            if not any(np.hypot(item[2]-p[2],item[3]-p[3])<9 for p in unique):unique.append(item)
        pairs=sorted((float(np.hypot(p[2]-t['point'][0],p[3]-t['point'][1])),i,j)
            for i,t in enumerate(targets) for j,p in enumerate(unique))
        used_targets=set();used_detections=set()
        for distance,i,j in pairs:
            if distance>self.config.get('assignment_radius',20) or i in used_targets or j in used_detections:continue
            score,cls,_,_,box=unique[j]
            # A wire or hand can alter the surroundings of a clearly visible
            # screw. Strong local head evidence does not require template-like
            # surroundings; weak heads and empty holes still need context.
            if contexts[i]<self.config.get('local_context_threshold',.1) and not (cls==0 and score>=.75):continue
            if cls==1 and score<self.config.get('empty_threshold',.65):continue
            used_targets.add(i);used_detections.add(j)
            target=targets[i]
            target.update(state='PRESENT' if cls==0 else 'EMPTY',score=round(score,4),
                          contextScore=round(contexts[i],4),reason='当前帧局部检出螺丝头' if cls==0 else '当前帧局部检出空孔')
            corners=np.float32([[[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]]])
            transformed=cv2.perspectiveTransform(corners,H)[0]
            target['detectedBbox']=target['bbox']=[float(transformed[:,0].min()/1920),float(transformed[:,1].min()/1080),
                float(transformed[:,0].max()/1920),float(transformed[:,1].max()/1080)]
        # A position whose head is partly hidden by a cable or housing can score
        # consistently below the global threshold. Retry only those configured
        # positions at their own threshold, and keep the highest-scoring match.
        fallback=[(i,overrides[str(t['id'])]) for i,t in enumerate(targets)
                  if t['state']=='UNKNOWN' and str(t['id']) in overrides and overrides[str(t['id'])]<base_threshold]
        if fallback:
            claimed=[(p[2],p[3]) for j,p in enumerate(unique) if j in used_detections]
            for i,threshold in fallback:
                detections=rows[i]
                x0,y0=origins[i];best=None
                for *raw,score,cls in detections:
                    box=[x0+raw[0]/detail,y0+raw[1]/detail,x0+raw[2]/detail,y0+raw[3]/detail]
                    cx,cy=(box[0]+box[2])/2,(box[1]+box[3])/2
                    distance=float(np.hypot(cx-targets[i]['point'][0],cy-targets[i]['point'][1]))
                    if distance>self.config.get('assignment_radius',20):continue
                    if cls==0 and score<threshold:continue
                    if cls==1 and score<self.config.get('empty_threshold',.65):continue
                    if contexts[i]<self.config.get('local_context_threshold',.1) and not (cls==0 and score>=.75):continue
                    if any(np.hypot(cx-ux,cy-uy)<9 for ux,uy in claimed):continue
                    candidate=(float(score),int(cls),cx,cy,box)
                    if best is None or candidate[0]>best[0]:best=candidate
                if best is None:continue
                score,cls,_,_,box=best
                claimed.append((best[2],best[3]))
                target=targets[i]
                target.update(state='PRESENT' if cls==0 else 'EMPTY',score=round(score,4),
                              contextScore=round(contexts[i],4),reason='当前帧局部检出螺丝头' if cls==0 else '当前帧局部检出空孔')
                corners=np.float32([[[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]]])
                transformed=cv2.perspectiveTransform(corners,H)[0]
                target['detectedBbox']=target['bbox']=[float(transformed[:,0].min()/1920),float(transformed[:,1].min()/1080),
                    float(transformed[:,0].max()/1920),float(transformed[:,1].max()/1080)]
        # Any currently visible component may establish the product position.
        # Fixture-only matches after product removal are insufficient. This
        # check is independent of head count and also permits all-empty screws.
        matrix=torch.as_tensor(matrix,device=image_gpu.device,dtype=torch.float64)
        boxes=[tuple(round(value/2) for value in box) for box in self.config['component_anchors']]
        searches=vision.warp_regions(small,torch.linalg.inv(matrix),(960,540),
            [(a-6,b-6,c+6,d+6) for a,b,c,d in boxes])
        ref=self.reference_small
        anchors=[vision.correlation(vision.gray(patch),ref[b:d,a:c])
            for patch,(a,b,c,d) in zip(searches,boxes)]
        anchors=torch.stack(anchors).cpu().tolist()
        body=sum(s>=self.config['component_threshold'] for s in anchors)>=self.config['minimum_visible_components']
        strong=[t for t in targets if t['state']=='PRESENT' and t['score']>=.85]
        separate_heads=any(np.hypot(a['point'][0]-b['point'][0],a['point'][1]-b['point'][1])>60
            for i,a in enumerate(strong) for b in strong[i+1:])
        product_visible=body or max(component_counts,default=0)>=4 or separate_heads
        result.update(registered=bool(product_visible),componentScores=[round(s,4) for s in anchors],
            reason='逐位置局部检测；其他位置遮挡不阻断可见目标' if product_visible else '主体不可核对或已离场，当前帧证据不足',
            detailScale=detail)
        if not result['registered']:
            for t in targets:t.update(state='UNKNOWN',score=None,bbox=None,detectedBbox=None,reason='主体不可核对或已离场')
        return finish()

    def predict_crops(self,crops,targets,base_threshold,overrides):
        groups={}
        for i,target in enumerate(targets):
            path=self.config.get('target_models',{}).get(target['id'])
            if path is not None and self.target_models[path] is self.model:path=None
            groups.setdefault(path,[]).append(i)
        predictions=[None]*len(crops)
        for path,indices in groups.items():
            model=self.model if path is None else self.target_models[path]
            threshold=min([base_threshold]+[overrides.get(str(targets[i]['id']),base_threshold) for i in indices])
            batch=predict_cuda(model,[crops[i] for i in indices],imgsz=self.config.get('detector_input',256),
                conf=threshold,iou=.45,device=self.device,verbose=False,max_det=12)
            if len(batch)!=len(indices):
                raise ValueError('螺丝模型返回数量与采样区域不一致')
            for i,prediction in zip(indices,batch):predictions[i]=prediction
        return predictions
