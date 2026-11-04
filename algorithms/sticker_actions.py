"""Calibrated local action observations. Never infers sticker material/presence."""
import copy
import time
from pathlib import Path
import cv2
import numpy as np
from .video import AnalysisError
from .evidence import event_precedence

ROOT=Path(__file__).resolve().parents[1]


class StickerActionDetector:
    def __init__(self,frozen):
        import joblib
        self.frozen=frozen;self.config=frozen['config'];self.device='cpu'
        self.reference=cv2.imread(str(ROOT/self.config['reference']))
        self.height,self.width=self.reference.shape[:2]
        self.reference_gray=cv2.cvtColor(self.reference,cv2.COLOR_BGR2GRAY)
        self.reference_crops={t['id']:action_crop(self.reference,t) for t in self.config['targets']}
        self.hog=cv2.HOGDescriptor((48,48),(16,16),(8,8),(8,8),9)
        self.scale=min(1.,960/max(self.width,self.height))
        self.size=(round(self.width*self.scale),round(self.height*self.scale))
        self.sift=cv2.SIFT_create(nfeatures=3000,contrastThreshold=.015)
        self.registrations={}
        for scale in sorted({self.scale,1.}):
            size=(round(self.width*scale),round(self.height*scale))
            small=cv2.resize(self.reference,size);mask=np.zeros(small.shape[:2],np.uint8)
            cv2.fillPoly(mask,[np.round(np.array(p)*scale).astype(np.int32) for p in self.config['registration_regions']],255)
            keys,descriptors=self.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),mask)
            self.registrations[scale]=dict(size=size,keys=keys,descriptors=descriptors)
        self.matcher=cv2.BFMatcher();self.estimator=None
        if self.config.get('mode')=='action':self.estimator=joblib.load(ROOT/self.config['model'])

    def locate(self,image):
        # Native-resolution live frames keep more detail for short contacts;
        # larger camera frames use the lower scale to bound SIFT cost.
        scale=1. if max(image.shape[:2])<=1920 else self.scale
        registration=self.registrations[scale];size=registration['size'];descriptors=registration['descriptors']
        key,desc=self.sift.detectAndCompute(cv2.cvtColor(cv2.resize(image,size),cv2.COLOR_BGR2GRAY),None)
        if desc is None or descriptors is None:return None
        matches=[p[0] for p in self.matcher.knnMatch(descriptors,desc,k=2) if len(p)==2 and p[0].distance<.72*p[1].distance]
        if len(matches)<14:return None
        src=np.float32([registration['keys'][m.queryIdx].pt for m in matches]);dst=np.float32([key[m.trainIdx].pt for m in matches])
        matrix,keep=cv2.findHomography(src,dst,cv2.RANSAC,2)
        if matrix is None or keep.sum()<12 or keep.mean()<.3:return None
        scaling=np.diag([scale,scale,1.]);H=np.linalg.inv(scaling)@matrix@scaling
        a,b,c,d=self.config['product_bbox'];quad=cv2.perspectiveTransform(np.float32([[[a,b],[c,b],[c,d],[a,d]]]),H)[0]
        if not np.isfinite(quad).all() or not .45*(c-a)*(d-b)<abs(cv2.contourArea(quad))<2*(c-a)*(d-b):return None
        x1,y1,x2,y2=self.config['station_roi'];x,y=quad.mean(axis=0)/[self.width,self.height]
        if not(x1<=x<=x2 and y1<=y<=y2):return None
        aligned=cv2.warpPerspective(cv2.resize(image,(self.width,self.height)),np.linalg.inv(H),(self.width,self.height))
        gray=cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY);reference=self.reference_gray
        scores=[];m=self.config.get('anchor_search_radius',12)
        for a,b,c,d in self.config['component_anchors']:
            scores.append(float(cv2.minMaxLoc(cv2.matchTemplate(gray[b-m:d+m,a-m:c+m],reference[b:d,a:c],cv2.TM_CCOEFF_NORMED))[1]))
        threshold=self.config.get('component_threshold',.5)
        if scores[self.config.get('required_anchor',0)]<threshold or sum(s>=threshold for s in scores)<2:return None
        return aligned,H,int(keep.sum())

    def inspect(self,image):
        started=time.perf_counter();targets=[dict(**t,state='UNKNOWN',score=None,bbox=None) for t in self.config['targets']]
        result=dict(mode='action',registered=False,targets=targets,displayTargets=[],inliers=0,reason='当前帧未可靠定位工件与贴条模板')
        located=self.locate(image)
        if located:
            aligned,H,inliers=located;result.update(registered=True,inliers=inliers,reason='当前帧按位置识别贴放/按压动作')
            for target in targets:
                crop=action_crop(aligned,target);ref=self.reference_crops[target['id']]
                mask=foreground_mask(crop,ref)
                values=action_features(crop,ref,mask=mask,hog=self.hog);model=self.estimator['sites'][target['id']]
                scores=dict(zip(model.classes_,map(float,model.predict_proba([values])[0])))
                category=max(scores,key=scores.get);prob=scores[category]
                state='CONTACT' if category=='contact' and prob>=self.config['action']['contact_threshold'] else 'CLEAR' if category=='clear' and prob>=.7 else 'UNKNOWN'
                # A static black-on-black patch can never constitute a hand action.
                foreground=float(mask.mean())
                if state=='CONTACT' and foreground<.025:state='UNKNOWN'
                x,y=target['point'];side=180
                quad=cv2.perspectiveTransform(np.float32([[[x-side/2,y-side/2],[x+side/2,y-side/2],[x+side/2,y+side/2],[x-side/2,y+side/2]]]),H)[0]
                roi=[float(quad[:,0].min()/self.width),float(quad[:,1].min()/self.height),float(quad[:,0].max()/self.width),float(quad[:,1].max()/self.height)]
                target.update(state=state,score=round(prob,4) if state!='UNKNOWN' else None,actionCategory=category,
                    actionScores={k:round(v,4) for k,v in scores.items()},
                    foreground=round(foreground,4),searchRoi=roi,actionSignature=cv2.resize(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),(16,16)).flatten().tolist(),
                    reason='当前帧贴放/按压候选' if state=='CONTACT' else '当前帧手部离开贴合位' if state=='CLEAR' else '其他手部动作、遮挡或证据不足')
                if state=='CONTACT':
                    target.update(bbox=roi,positionMode='DETECTED');result['displayTargets'].append({k:v for k,v in target.items() if k!='actionSignature'})
        result['inferenceMs']=round((time.perf_counter()-started)*1000,2)
        return result


def action_crop(image,target,side=180):
    half=round(side)/2;x,y=map(round,target['point'])
    return image[max(0,round(y-half)):round(y+half),max(0,round(x-half)):round(x+half)]


def foreground_mask(image,reference):
    # Local foreground in front of the jig: uncovered fingers, white gloves,
    # wrist bands or sleeves. Black sticker texture is deliberately excluded.
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV);ref=cv2.cvtColor(reference,cv2.COLOR_BGR2HSV)
    delta=np.linalg.norm(image.astype(float)-reference.astype(float),axis=2)
    mask=np.uint8((delta>45)&((hsv[:,:,2]>110)|(hsv[:,:,1]>70))&
                    ((np.abs(hsv[:,:,2].astype(float)-ref[:,:,2])>28)|(hsv[:,:,1]>ref[:,:,1].astype(float)+35)))
    return cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((5,5),np.uint8))


def foreground_fraction(image,reference):return float(foreground_mask(image,reference).mean())


def action_features(image,reference,*,mask=None,hog=None):
    if mask is None:mask=foreground_mask(image,reference)
    # Erase static fixture/sticker appearance. Only foreground color and shape
    # remain available to the classifier.
    isolated=image*mask[:,:,None];small=cv2.resize(isolated,(48,48))
    if hog is None:hog=cv2.HOGDescriptor((48,48),(16,16),(8,8),(8,8),9)
    return np.concatenate([small.flatten()/255.,hog.compute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY)).flatten(),
        cv2.resize(mask.astype(np.float32),(18,18),interpolation=cv2.INTER_AREA).flatten()]).astype(np.float32)


class StickerActionSequence:
    """Each cycle owns this state; detector and timestamps supply no script.

    Require clear baseline, then either multiple moving contact observations
    or one high-confidence contact, then an observed release. Unknown frames
    do not vote or manufacture a release. Completion is an event record;
    current display boxes remain frame-only.
    """
    def __init__(self,targets,settings=None):
        self.targets=copy.deepcopy(targets);self.settings=settings or {};self.last_time=None
        self.triangles=[t['id'] for t in targets if t['expected']=='triangle']
        self.rectangles=[t['id'] for t in targets if t['expected']=='rectangle']
        if len(self.triangles)!=2 or not self.rectangles:raise ValueError('贴条动作需要两处三角和后续方形位置')
        self.sites={t['id']:dict(ready=False,clear=0,release=0,start=None,last=None,last_observation=None,
            count=0,motion=0,signature=None,score=0,last_clear=None,start_after=None,released_at=None) for t in targets}
        self.events=[];self.violations=[];self.uncertainties=[];self.registered=0

    def _confirm(self,identity,site,t,release_kind):
        if site['start'] is None:return
        duration=site['last']-site['start']
        moving=(site['count']>=self.settings.get('contact_frames',2) and
                duration>=self.settings.get('min_contact_seconds',.2)-1e-6 and
                site['motion']>=self.settings.get('min_motion',2.5))
        strong=(site['count']>=self.settings.get('strong_contact_frames',2) and
                duration>=self.settings.get('min_contact_seconds',.2)-1e-6 and
                site['score']>=self.settings.get('strong_contact_threshold',.85))
        single=(release_kind=='clear' and site['count']==1 and
                site['score']>=self.settings.get('single_contact_threshold',.9))
        if (moving or strong or single) and not any(e['id']==identity for e in self.events):
            evidence='multi-frame-motion' if moving else 'high-confidence-contact-release' if strong else 'single-high-confidence'
            self.events.append(dict(id=identity,time=round(site['start'],4),confirmedAt=round(t,4),
                startAfter=site['start_after'],lastContactAt=site['last'],releasedAt=site['released_at'],
                event='ACTION_CONFIRMED',score=round(site['score'],4),contactFrames=site['count'],
                motion=round(site['motion'],3),evidence=evidence))
        site.update(start=None,count=0,motion=0,signature=None,score=0,release=0,released_at=None)

    def update(self,t,inspection):
        if self.last_time is not None and t<=self.last_time:raise ValueError('采样时间必须严格递增')
        self.last_time=t;self.registered+=bool(inspection['registered'])
        seen={v['id']:v for v in inspection['targets']} if inspection['registered'] else {}
        for identity,site in self.sites.items():
            value=seen.get(identity,{});kind=value.get('state','UNKNOWN');signature=value.get('actionSignature')
            release_other=(kind=='UNKNOWN' and value.get('actionCategory')=='other')
            if site['last_observation'] is not None and t-site['last_observation']>self.settings.get('max_gap',.8):
                site.update(ready=False,clear=0,release=0,start=None,last=None,last_observation=None,count=0,motion=0,signature=None,
                    score=0,last_clear=None,start_after=None,released_at=None)
            if kind=='CLEAR':
                site['last_observation']=t
                site['last_clear']=t
                if site['start'] is not None and site['released_at'] is None:site['released_at']=t
                site['clear']+=1
                site['release']=0
                if site['clear']>=self.settings.get('clear_frames',2):
                    self._confirm(identity,site,t,'clear')
                    site.update(ready=True)
                # Contact last timestamp is needed for dwell, so clear frames
                # do not extend it. Once reset, track actual clear timestamp.
                if site['start'] is None:site['last']=t
            elif kind=='CONTACT':
                site['last_observation']=t
                site['clear']=0;site['release']=0;site['released_at']=None
                if site['ready']:
                    if site['start'] is None:site.update(start=t,count=0,motion=0,signature=None,score=0,start_after=site['last_clear'])
                    if signature is not None and site['signature'] is not None:
                        site['motion']=max(site['motion'],float(np.mean(np.abs(np.array(signature,float)-site['signature']))))
                    site.update(count=site['count']+1,last=t,signature=np.array(signature,float) if signature is not None else None,
                        score=max(site['score'],value.get('score') or 0))
            elif release_other:
                # A registered "other hand action" frame is positive evidence
                # that the contact ended; it does not need two CLEAR frames.
                site['clear']=0
                if site['start'] is not None:
                    if site['released_at'] is None:site['released_at']=t
                    site['release']+=1
                    if site['release']>=self.settings.get('other_release_frames',1):
                        self._confirm(identity,site,t,'other')
                else:site['release']=0
            else:
                site['clear']=0;site['release']=0;site['released_at']=None
        # Use sampled release/start intervals, not the delayed confirmation time.
        events={e['id']:e for e in self.events}
        self.violations=[];self.uncertainties=[]
        for identity in self.rectangles:
            if identity not in events:continue
            start=events[identity]['time'];missing=[];uncertain=[]
            for predecessor in self.triangles:
                relation=event_precedence(events.get(predecessor),events[identity])
                pending=self.sites[predecessor]
                if predecessor not in events and pending['start'] is not None and pending['start']<start:
                    relation='violation' if pending['last']>=start else 'uncertain'
                if relation=='violation':missing.append(predecessor)
                elif relation=='uncertain':uncertain.append(predecessor)
            if missing:
                self.violations.append(dict(rectangle=identity,triangles=missing,time=start,
                    reason=f'{identity}发生贴放/按压动作时，尚未先观察到两处三角动作完成'))
            if uncertain:
                self.uncertainties.append(dict(rectangle=identity,triangles=uncertain,time=start,
                    reason=f'{identity}与前序动作的采样时间区间重叠，无法确认先后顺序'))
        # Signatures are private temporal features, never stored in artifacts.
        for target in inspection['targets']:target.pop('actionSignature',None)
        return self.snapshot()

    def snapshot(self):
        by_id={e['id']:e for e in self.events};ts=[by_id[k]['releasedAt'] for k in self.triangles if k in by_id]
        rs=[by_id[k]['time'] for k in self.rectangles if k in by_id]
        status='FAIL' if self.violations else 'REVIEW' if self.uncertainties else 'PASS' if len(by_id)==len(self.targets) else 'PENDING'
        return dict(mode='action',status=status,stage='动作顺序错误' if self.violations else '动作记录齐全' if status=='PASS' else '贴方形贴动作' if len(ts)==2 else '先完成两处三角动作',
            trianglesObserved=len(ts),trianglesExpected=2,rectanglesObserved=len(rs),rectanglesExpected=len(self.rectangles),
            trianglesCompleteAt=max(ts) if len(ts)==2 else None,firstRectangleAt=min(rs) if rs else None,
            events=copy.deepcopy(sorted(self.events,key=lambda e:e['time'])),violations=copy.deepcopy(self.violations),
            uncertainties=copy.deepcopy(self.uncertainties),
            reason=self.violations[0]['reason'] if self.violations else self.uncertainties[0]['reason'] if self.uncertainties else '两处三角动作先完成，后续方形动作已逐位置确认' if status=='PASS' else '等待对应位置的贴放/按压动作；静止成品不计完成')

    def aggregate(self,final=False):
        by_id={e['id']:e for e in self.events}
        return [dict(**t,state='ACTION_DONE' if t['id'] in by_id else 'NOT_OBSERVED' if final and self.events else 'UNKNOWN',
            score=by_id.get(t['id'],{}).get('score'),lastSeen=by_id.get(t['id'],{}).get('time'),bbox=None) for t in self.targets]


def action_compliance(targets,sequence,final=False):
    missing=[t['id'] for t in targets if t['state']!='ACTION_DONE'];events=sequence['events']
    status='FAIL' if sequence['status']=='FAIL' or (final and events and missing) else 'PASS' if not missing and sequence['status']=='PASS' else 'REVIEW'
    return dict(status=status,label={'PASS':'动作合规','FAIL':'动作不合规','REVIEW':'动作证据不足'}[status],mode='action',
        reason=sequence['reason'] if sequence['status'] in ('FAIL','REVIEW') or status=='PASS' else '结束时未观察到动作：'+', '.join(missing) if final and events else '尚未取得完整贴放/按压动作证据',
        scope='按用户要求，有贴放/按压动作即计完成；不验证贴片实际存在、外观或粘附',missingActions=missing,empty=[],wrong=[],unknown=missing)
