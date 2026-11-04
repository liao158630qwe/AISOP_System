"""Current-frame, local screw inspection; no all-component prerequisite."""
import time
import cv2
import numpy as np

from .screws import ROOT, snapshot, verify


class RefinedScrewDetector:
    def __init__(self,frozen=None,model=None):
        from ultralytics import YOLO
        import torch
        self.frozen=frozen or snapshot();verify(self.frozen)
        self.config=self.frozen['config']
        self.reference=cv2.imread(str(ROOT/self.config['reference']))
        self.reference_gray=cv2.cvtColor(self.reference,cv2.COLOR_BGR2GRAY)
        self.sift=cv2.SIFT_create(nfeatures=2200,contrastThreshold=.02)
        self.registration_size=(960,540)
        reference=cv2.resize(self.reference,self.registration_size,interpolation=cv2.INTER_AREA)
        mask=np.zeros(reference.shape[:2],np.uint8)
        regions=self.config.get('registration_regions')
        polygons=[np.round(np.array(p)/2).astype(np.int32) for p in regions] if regions else [np.array([[340,90],[450,100],[495,495],[290,500],[250,340]])]
        cv2.fillPoly(mask,polygons,255)
        self.keypoints,self.descriptors=self.sift.detectAndCompute(cv2.cvtColor(reference,cv2.COLOR_BGR2GRAY),mask)
        self.matcher=cv2.BFMatcher()
        self.model=model if model is not None else YOLO(str(ROOT/self.config['model']))
        self.target_models={path:YOLO(str(ROOT/path)) for path in set(self.config.get('target_models',{}).values())}
        self.device=0 if torch.cuda.is_available() else 'cpu'

    def localize(self,key,desc):
        if desc is None or self.descriptors is None:return None
        matches=[pair[0] for pair in self.matcher.knnMatch(self.descriptors,desc,k=2)
                 if len(pair)==2 and pair[0].distance<.72*pair[1].distance]
        if len(matches)<14:return None
        src=np.float32([self.keypoints[m.queryIdx].pt for m in matches])
        dst=np.float32([key[m.trainIdx].pt for m in matches])
        matrix,inliers=cv2.findHomography(src,dst,cv2.RANSAC,2)
        if matrix is None or inliers.sum()<12 or inliers.mean()<.3:return None
        scale=np.diag([2.,2.,1.]);H=scale@matrix@np.linalg.inv(scale)
        bounds=self.config.get('product_polygon',[[650,200],[930,200],[980,1000],[560,1000]])
        polygon=cv2.perspectiveTransform(np.float32([bounds]),H)[0]
        if not np.isfinite(polygon).all() or not 70000<abs(cv2.contourArea(polygon))<700000:return None
        regions=self.config.get('component_regions',[
            [[739,253],[780,255],[858,704],[828,718],[704,346]],
            [[680,590],[725,600],[905,920],[875,930],[665,670]],
            [[706,480],[748,478],[808,602],[778,618]]])
        counts=[sum(bool(keep) and cv2.pointPolygonTest(np.float32(p),tuple(map(float,point*2)),False)>=0
            for point,keep in zip(src,inliers.ravel())) for p in regions]
        return dict(matrix=matrix,H=H,inliers=int(inliers.sum()),componentInliers=counts)

    def inspect(self,image,registration=None):
        started=time.perf_counter()
        targets=[dict(**t,state='UNKNOWN',score=None,bbox=None,reason='当前帧未可靠定位') for t in self.config['targets']]
        result={'targets':targets,'registered':False,'inliers':0,'reason':'当前帧未可靠定位','algorithm':'local-roi-v2'}
        def finish():
            result['inferenceMs']=round((time.perf_counter()-started)*1000,2)
            return result
        small=cv2.resize(image,self.registration_size,interpolation=cv2.INTER_AREA)
        if registration is None:
            key,desc=self.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),None)
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
        crops=[];origins=[];contexts=[]
        context_mask=np.ones((48,48),np.uint8)*255;context_mask[12:36,12:36]=0
        for target in targets:
            x,y=target['point'];x0,y0=round(x)-40,round(y)-40
            region=cv2.perspectiveTransform(np.float32([[[x0,y0],[x0+80,y0],[x0+80,y0+80],[x0,y0+80]]]),H)[0]
            target['searchRoi']=[float(region[:,0].min()/1920),float(region[:,1].min()/1080),
                                 float(region[:,0].max()/1920),float(region[:,1].max()/1080)]
            move=np.array([[1.,0.,-x0],[0.,1.,-y0],[0.,0.,1.]])
            crop=cv2.warpPerspective(image,detail_matrix@move@inv@normalize,(side,side))
            crops.append(crop);origins.append((x0,y0))
            local=cv2.cvtColor(cv2.resize(crop,(80,80)),cv2.COLOR_BGR2GRAY)[16:64,16:64]
            reference=self.reference_gray[y0+16:y0+64,x0+16:x0+64]
            score=cv2.matchTemplate(local,reference,cv2.TM_CCOEFF_NORMED,mask=context_mask)[0,0]
            contexts.append(float(score) if np.isfinite(score) else 0.)
            target.update(contextScore=round(contexts[-1],4),reason='当前帧未检出可靠螺丝头或空孔')
        predictions=self.model.predict(crops,imgsz=self.config.get('detector_input',256),
            conf=self.config.get('threshold',.45),iou=.45,device=self.device,verbose=False,max_det=12)
        # A specifically trained crop model replaces only that position's
        # prediction. Other positions retain the verified general model.
        for path,model in self.target_models.items():
            indices=[i for i,t in enumerate(targets) if self.config.get('target_models',{}).get(t['id'])==path]
            specialized=model.predict([crops[i] for i in indices],imgsz=self.config.get('detector_input',256),
                conf=self.config.get('threshold',.45),iou=.45,device=self.device,verbose=False,max_det=12)
            for i,prediction in zip(indices,specialized):predictions[i]=prediction
        candidates=[]
        for prediction,(x0,y0) in zip(predictions,origins):
            for raw,cls,confidence in zip(prediction.boxes.xyxy.cpu().tolist(),prediction.boxes.cls.cpu().tolist(),prediction.boxes.conf.cpu().tolist()):
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
        base_threshold=float(self.config.get('threshold',.45))
        overrides={str(k):float(v) for k,v in (self.config.get('target_thresholds') or {}).items()}
        fallback=[(i,overrides[str(t['id'])]) for i,t in enumerate(targets)
                  if t['state']=='UNKNOWN' and str(t['id']) in overrides and overrides[str(t['id'])]<base_threshold]
        if fallback:
            claimed=[(p[2],p[3]) for j,p in enumerate(unique) if j in used_detections]
            fallback_predictions=self.model.predict([crops[i] for i,_ in fallback],
                imgsz=self.config.get('detector_input',256),conf=min(threshold for _,threshold in fallback),
                iou=.45,device=self.device,verbose=False,max_det=12)
            for (i,threshold),prediction in zip(fallback,fallback_predictions):
                special=self.config.get('target_models',{}).get(targets[i]['id'])
                if special:
                    prediction=self.target_models[special].predict([crops[i]],imgsz=self.config.get('detector_input',256),
                        conf=threshold,iou=.45,device=self.device,verbose=False,max_det=12)[0]
                x0,y0=origins[i];best=None
                for raw,cls,score in zip(prediction.boxes.xyxy.cpu().tolist(),prediction.boxes.cls.cpu().tolist(),prediction.boxes.conf.cpu().tolist()):
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
        aligned=cv2.warpPerspective(small,np.linalg.inv(matrix),(960,540))
        gray=cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY);ref=cv2.resize(self.reference_gray,(960,540))
        anchors=[]
        for a,b,c,d in self.config['component_anchors']:
            a,b,c,d=round(a/2),round(b/2),round(c/2),round(d/2)
            anchors.append(float(cv2.minMaxLoc(cv2.matchTemplate(gray[b-6:d+6,a-6:c+6],ref[b:d,a:c],cv2.TM_CCOEFF_NORMED))[1]))
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
