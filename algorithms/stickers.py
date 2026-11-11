"""Calibrated sticker appearance coverage, with frozen legacy order support."""
from collections import Counter
from datetime import datetime,timezone
import csv
import html
import json
from pathlib import Path
import time
import cv2
import numpy as np
from .video import AnalysisError,file_hash,probe,validate_options
from .asset_snapshot import AssetSnapshotCache
from .screws import ScrewObservationHistory,final_observations
from .sticker_sequence import StickerSequence,compliance as appearance_compliance

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/'config/sticker-inspection.json'
LABELS={'PRESENT':'已贴','EMPTY':'明确未贴','WRONG':'形状不符','UNKNOWN':'证据不足',
        'ACTION_DONE':'动作已确认','NOT_OBSERVED':'未观察到动作','CONTACT':'贴放/按压候选','CLEAR':'当前无动作'}


class StickerVisualCoverage:
    """Confirm every configured site in one cycle without requiring placement order."""

    def __init__(self,config):
        self.targets=config['targets']
        settings=config.get('confirmation',{})
        self.history=ScrewObservationHistory(self.targets,
            confirmation_frames=settings.get('frames',config.get('sequence',{}).get('confirmation_frames',3)),
            max_gap=settings.get('max_gap_seconds',2.0),max_window=settings.get('window_seconds',4.0))

    def update(self,time,inspection):
        observed={t['id']:t for t in inspection.get('targets',[])}
        # A partial frame must never reduce the configured number of sites.
        targets=[dict(t,**{key:observed.get(t['id'],{}).get(key,default)
            for key,default in (('state','UNKNOWN'),('score',None),('bbox',None))}) for t in self.targets]
        self.history.update(time,dict(inspection,targets=targets))
        return self.snapshot()

    def snapshot(self):
        targets=self.history.aggregate()
        completed=[t['id'] for t in targets if t['state']=='PRESENT']
        pending=[t['id'] for t in targets if t['state']!='PRESENT']
        wrong=[t['id'] for t in targets if t['state']=='WRONG']
        empty=[t['id'] for t in targets if t['state']=='EMPTY']
        status='FAIL' if wrong else 'PASS' if targets and not pending else 'PENDING'
        reason=('贴条形状不符：'+', '.join(wrong)) if wrong else (
            f'全部 {len(targets)} 处贴条位置均已取得累计视觉确认' if status=='PASS' else
            f'已确认 {len(completed)}/{len(targets)} 处贴条位置；待确认：'+', '.join(pending))
        events=[dict(id=t['id'],type='CONFIRMED',state=t['state'],time=t['confirmedAt'],
            confirmedAt=t['confirmedAt'],score=t['score']) for t in targets if t['historyConfirmed']]
        events.sort(key=lambda event:(event['time'],event['id']))
        return dict(verificationMode='visual-cumulative',configured=bool(targets),status=status,
            reason=reason,stage='位置确认齐全' if status=='PASS' else '逐位置外观确认',
            order=[t['id'] for t in targets],completed=completed,pending=pending,
            current=pending[0] if pending else None,empty=empty,wrong=wrong,events=events,violations=[],
            positionsExpected=len(targets),positionsConfirmed=len(completed),
            operationsExpected=len(targets),operationsCompleted=len(completed),
            preassembled={},preassembledExpected=0,preassembledConfirmed=0,
            trianglesExpected=sum(t.get('expected')=='triangle' for t in targets),
            trianglesObserved=sum(t.get('expected')=='triangle' and t['state']=='PRESENT' for t in targets),
            rectanglesExpected=sum(t.get('expected')=='rectangle' for t in targets),
            rectanglesObserved=sum(t.get('expected')=='rectangle' and t['state']=='PRESENT' for t in targets))


def create_sequence(config):
    if config.get('mode')=='action':
        from .sticker_actions import StickerActionSequence
        return StickerActionSequence(config['targets'],config['action'])
    if config.get('verification_mode')=='visual-cumulative':
        return StickerVisualCoverage(config)
    return StickerSequence(config['targets'],confirmation_frames=config.get('sequence',{}).get('confirmation_frames',2))


def create_history(config):
    """Retain confirmed appearance for one cycle, independently of frame caches."""
    return None if config.get('mode')=='action' or config.get('verification_mode')=='visual-cumulative' else ScrewObservationHistory(config['targets'])


def summarize(frames,config,sequence,final=False,*,history=None):
    if config.get('mode')=='action':
        events=sequence.snapshot()['events']
        return sequence.aggregate(final),max((e['confirmedAt'] for e in events),default=None)
    if isinstance(sequence,StickerVisualCoverage):
        targets=sequence.history.aggregate(final=final)
        reason=sequence.snapshot()['reason']
        for target in targets:
            target.update(sequenceReason=reason,sequenceObserved=target['state'],
                sequenceState='COMPLETED' if target['state']=='PRESENT' else 'PENDING' if target['state']=='UNKNOWN' else target['state'])
        return targets,sequence.history.assessment_time
    if history is not None:
        return history.aggregate(final=final),history.assessment_time
    return final_observations(frames,config['targets'])


def compliance(targets,sequence,final=False):
    if sequence.get('mode')=='action':
        from .sticker_actions import action_compliance
        return action_compliance(targets,sequence,final)
    if sequence.get('verificationMode')=='visual-cumulative':
        empty=[t['id'] for t in targets if t['state']=='EMPTY']
        wrong=[t['id'] for t in targets if t['state']=='WRONG']
        unknown=[t['id'] for t in targets if t['state'] not in ('PRESENT','EMPTY','WRONG')]
        complete=(bool(targets) and not (empty or wrong or unknown)
            and sequence.get('configured') is True and sequence.get('status')=='PASS'
            and len(targets)==sequence.get('positionsExpected')==sequence.get('positionsConfirmed'))
        status='FAIL' if wrong or (final and empty) else 'PASS' if complete else 'REVIEW'
        reason=('贴条形状不符：'+', '.join(wrong)) if wrong else (
            '检测结束时仍有明确未贴位置：'+', '.join(empty)) if final and empty else sequence['reason']
        return dict(status=status,label={'PASS':'合规','FAIL':'不合规','REVIEW':'无法判定' if final else '检测中'}[status],
            reason=reason,scope=f'{len(targets)}处标定位置贴条形状与存在状态的累计视觉确认；不核验顺序或胶粘强度',
            verificationMode='visual-cumulative',empty=empty,wrong=wrong,unknown=unknown)
    decision=appearance_compliance(targets,sequence)
    # Bare sites are expected while placement is in progress. The browser ends
    # a cycle on FAIL, so missing patches become a defect only at finalization.
    if not final and decision['empty'] and not decision['wrong'] and sequence['status']!='FAIL':
        decision.update(status='REVIEW',label='检测中',
            reason='当前仍有未贴位置，继续采集贴前至贴后证据；结束检测时再判定是否漏贴')
    return decision


def StickerDetector(frozen=None):
    frozen=frozen or snapshot();verify(frozen)
    if frozen['config'].get('mode')=='action':
        from .sticker_actions import StickerActionDetector
        return StickerActionDetector(frozen)
    return AppearanceStickerDetector(frozen)


_snapshot_cache=AssetSnapshotCache()


def _asset_names(config):
    names=[config['model'],config['reference']]
    if config.get('material_model'):names.append(config['material_model'])
    if config.get('mode')=='action':names.append('models/disk-depth-v1.pth')
    return names


def _validate_config(config):
    try:StickerSequence(config['targets'])
    except (ValueError,KeyError) as exc:raise AnalysisError('STICKER_CONFIG_INVALID','贴条工序需配置两个三角和后续方形贴') from exc


def snapshot():
    if not CONFIG.is_file():raise AnalysisError('STICKER_NOT_READY','贴条模型与工位标定尚未完成')
    return _snapshot_cache.get(CONFIG,ROOT,_asset_names,validate=_validate_config,
        missing_code='STICKER_MODEL_MISSING',missing_message='贴条专用模型或参考图缺失')


def verify(frozen):
    if any(not (ROOT/p).is_file() or file_hash(ROOT/p)!=sha for p,sha in frozen['assets'].items()):
        raise AnalysisError('MODEL_CHANGED','贴条模型或标定图改变，请重新创建检测任务')


class AppearanceStickerDetector:
    def __init__(self,frozen=None):
        from ultralytics import YOLO
        import torch
        self.frozen=frozen or snapshot();verify(self.frozen);self.config=self.frozen['config']
        self.reference=cv2.imread(str(ROOT/self.config['reference']))
        self.height,self.width=self.reference.shape[:2]
        self.scale=min(1.,960/max(self.width,self.height))
        self.size=(round(self.width*self.scale),round(self.height*self.scale))
        self.sift=cv2.SIFT_create(nfeatures=3000,contrastThreshold=.015)
        small=cv2.resize(self.reference,self.size)
        mask=np.zeros(small.shape[:2],np.uint8)
        regions=self.config.get('registration_regions')
        if regions:
            cv2.fillPoly(mask,[np.round(np.array(p)*self.scale).astype(np.int32) for p in regions],255)
        else:
            roi=self.config.get('product_bbox',[0,0,self.width,self.height])
            a,b,c,d=[round(v*self.scale) for v in roi];mask[b:d,a:c]=255
        self.keys,self.descriptors=self.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),mask)
        self.matcher=cv2.BFMatcher();self.model=YOLO(str(ROOT/self.config['model']))
        from .gpu import require_cuda
        self.device=require_cuda()
        self.material_models={}
        if self.config.get('material_model'):
            import joblib
            from .gpu_classifier import CudaSVC
            self.material_models={key:CudaSVC(model) for key,model in joblib.load(ROOT/self.config['material_model'])['sites'].items()}
        gray=cv2.cvtColor(self.reference,cv2.COLOR_BGR2GRAY)
        self.reference_gray=gray
        self.site_masks={}
        for target in self.config['targets']:
            box=target.get('reference_bbox')
            if box:
                a,b,c,d=map(round,box)
                self.site_masks[target['id']]=cv2.erode(np.uint8(gray[b:d,a:c]<70),np.ones((5,5),np.uint8)).astype(bool)

    def inspect(self,image):
        from .cuda_vision import CudaFrame
        started=time.perf_counter()
        if isinstance(image,CudaFrame):
            # OpenCV expects interleaved HWC. A planar NumPy view triggers a
            # full-frame copy inside every resize/warp/color conversion.
            image=image.rgb.flip(0).permute(1,2,0).contiguous().cpu().numpy()
        image=np.ascontiguousarray(image)
        targets=[dict(**t,state='UNKNOWN',score=None,bbox=None) for t in self.config['targets']]
        result=dict(registered=False,targets=targets,inliers=0,reason='当前帧未可靠定位贴条工件')
        def finish():
            result['inferenceMs']=round((time.perf_counter()-started)*1000,2)
            result['displayTargets']=[dict(t,positionMode='DETECTED') for t in targets if result['registered'] and
                t['state'] in ('PRESENT','EMPTY','WRONG') and t.get('bbox') is not None]
            return result
        small=cv2.resize(image,self.size)
        key,desc=self.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),None)
        if desc is None or self.descriptors is None:return finish()
        matches=[pair[0] for pair in self.matcher.knnMatch(self.descriptors,desc,k=2)
                 if len(pair)==2 and pair[0].distance<.72*pair[1].distance]
        if len(matches)<14:return finish()
        src=np.float32([self.keys[m.queryIdx].pt for m in matches]);dst=np.float32([key[m.trainIdx].pt for m in matches])
        matrix,keep=cv2.findHomography(src,dst,cv2.RANSAC,2)
        if matrix is None or keep.sum()<12 or keep.mean()<.3:return finish()
        a,b,c,d=self.config.get('product_bbox',[0,0,self.width,self.height])
        shape=np.float32([[[a,b],[c,b],[c,d],[a,d]]]);scale=np.diag([self.scale,self.scale,1.])
        H=np.linalg.inv(scale)@matrix@scale
        projected=cv2.perspectiveTransform(shape,H)[0]
        ref_area=(c-a)*(d-b);area=abs(cv2.contourArea(projected))
        if not np.isfinite(projected).all() or not .45*ref_area<area<2.0*ref_area:return finish()
        # A spare white template on the table is outside the calibrated working
        # area. Its cutouts must never establish an active product inspection.
        if self.config.get('station_roi'):
            x1,y1,x2,y2=self.config['station_roi'];center=projected.mean(axis=0)/[self.width,self.height]
            if not (x1<=center[0]<=x2 and y1<=center[1]<=y2):return finish()
        inv=np.linalg.inv(H)
        # Match the audited training preprocessing. Resizing the full image
        # first avoids a half-pixel/aliasing difference on dark foam textures.
        normalized=cv2.resize(image,(self.width,self.height))
        if self.config.get('component_anchors'):
            scores=[]
            margin=self.config.get('anchor_search_radius',5)
            for a,b,c,d in self.config['component_anchors']:
                # Warp only the searched anchor, preserving the existing
                # resize -> color warp -> grayscale preprocessing order.
                x0,x1,_=slice(a-margin,c+margin).indices(self.width)
                y0,y1,_=slice(b-margin,d+margin).indices(self.height)
                move=np.array([[1.,0.,-x0],[0.,1.,-y0],[0.,0.,1.]])
                if x1-x0<c-a or y1-y0<d-b:
                    scores.append(-1.)
                    continue
                patch=cv2.warpPerspective(normalized,move@inv,(x1-x0,y1-y0))
                gray=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)
                score=cv2.minMaxLoc(cv2.matchTemplate(gray,self.reference_gray[b:d,a:c],cv2.TM_CCOEFF_NORMED))[1]
                scores.append(float(score))
            result['componentScores']=[round(s,4) for s in scores]
            threshold=self.config.get('component_threshold',.5)
            if scores[self.config.get('required_anchor',0)]<threshold or sum(s>=threshold for s in scores)<2:
                result['reason']='定位模板与工件相对位置不可核对、遮挡或模板正在取走'
                return finish()
        crops=[];regions=[];observable=[]
        for target in targets:
            x,y=target['point']
            box=target.get('reference_bbox',target.get('box'))
            side=self.config.get('crop_size',160)
            if box:side=max(side,(box[2]-box[0])*1.6,(box[3]-box[1])*1.6)
            side=round(side);left,top=round(x-side/2),round(y-side/2)
            move=np.array([[1.,0.,-left],[0.,1.,-top],[0.,0.,1.]])
            crop=cv2.warpPerspective(normalized,move@inv,(side,side))
            crops.append(crop);regions.append((left,top,side))
            valid=True
            if target['id'] in self.site_masks:
                a,b,c,d=map(round,target['reference_bbox']);pixels=crop[b-top:d-top,a-left:c-left]
                mask=self.site_masks[target['id']]
                if pixels.shape[:2]!=mask.shape or mask.sum()<20:valid=False
                else:
                    values=cv2.cvtColor(pixels,cv2.COLOR_BGR2GRAY)[mask];rgb=pixels[mask].astype(float)
                    glove=(values>105).mean()>.1
                    green=((rgb[:,1]>rgb[:,2]*1.4)&(rgb[:,1]>rgb[:,0]*1.05)).mean()>.35
                    valid=not (glove or green)
            observable.append(valid)
            quad=cv2.perspectiveTransform(np.float32([[[left,top],[left+side,top],[left+side,top+side],[left,top+side]]]),H)[0]
            target['searchRoi']=[float(quad[:,0].min()/self.width),float(quad[:,1].min()/self.height),
                                 float(quad[:,0].max()/self.width),float(quad[:,1].max()/self.height)]
        # Occluded sites cannot contribute evidence below. Avoid spending GPU
        # inference on those crops, retaining each result's original owner.
        visible=[i for i,valid in enumerate(observable) if valid]
        predictions=self.model.predict([crops[i] for i in visible],imgsz=self.config.get('detector_input',320),
            conf=self.config.get('threshold',.55),device=self.device,verbose=False,max_det=16) if visible else []
        if len(predictions)!=len(visible):
            raise ValueError('贴条模型返回数量与采样区域不一致')
        candidates=[]
        if predictions:
            import torch
            lengths=[len(p.boxes.data) for p in predictions]
            rows=torch.cat([p.boxes.data for p in predictions]).cpu().split(lengths)
        else:rows=[]
        for owner,detections in zip(visible,rows):
            left,top,side=regions[owner]
            for *raw,score,cls in detections.tolist():
                box=[raw[0]+left,raw[1]+top,raw[2]+left,raw[3]+top]
                center=[(box[0]+box[2])/2,(box[1]+box[3])/2]
                candidates.append((int(cls),float(score),box,center,owner))
        unique=[]
        for item in sorted(candidates,key=lambda p:p[1],reverse=True):
            if not any(item[4]==p[4] and np.linalg.norm(np.array(item[3])-p[3])<10 for p in unique):unique.append(item)
        candidates=unique
        pairs=sorted((float(np.linalg.norm(np.array(t['point'])-p[3])),i,j)
                     for i,t in enumerate(targets) for j,p in enumerate(candidates) if p[4]==i)
        used_targets=set();used_predictions=set()
        classes=self.config.get('class_map',{'0':'triangle','1':'rectangle','2':'bare'})
        for distance,i,j in pairs:
            target=targets[i]
            if not observable[i] or distance>target.get('assignment_radius',self.config.get('assignment_radius',35)) or i in used_targets or j in used_predictions:continue
            cls,score,box,_,_=candidates[j];kind=classes.get(str(cls))
            if kind not in ('triangle','rectangle','bare'):continue
            if kind=='bare' and score<self.config.get('bare_threshold',.75):continue
            state='EMPTY' if kind=='bare' else 'PRESENT' if kind==target['expected'] else 'WRONG'
            if state=='WRONG' and score<self.config.get('wrong_threshold',.8):continue
            if state=='PRESENT' and target['id'] in self.material_models:
                from .sticker_material import features
                left,top,side=regions[i];a,b,c,d=map(round,target['reference_bbox'])
                local=crops[i][b-top:d-top,a-left:c-left];mask=self.site_masks[target['id']]
                estimator=self.material_models[target['id']]['estimator']
                probability=float(estimator.predict_proba([features(local,mask)])[0][list(estimator.classes_).index(0)])
                target['materialBareScore']=round(probability,4)
                if probability>=self.config.get('material_bare_threshold',.9):
                    if score<self.config.get('bare_threshold',.75):continue
                    state='EMPTY';score=min(score,probability);kind='bare'
            used_targets.add(i);used_predictions.add(j)
            quad=cv2.perspectiveTransform(np.float32([[[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]]]),H)[0]
            target.update(state=state,score=round(score,4),actualClass=kind,
                reason='当前帧检出对应形状贴条' if state=='PRESENT' else '当前帧明确检出裸露贴合位' if state=='EMPTY' else '当前帧检出形状与该位置不符',
                bbox=[float(quad[:,0].min()/self.width),float(quad[:,1].min()/self.height),
                      float(quad[:,0].max()/self.width),float(quad[:,1].max()/self.height)])
        result.update(registered=bool(used_targets),inliers=int(keep.sum()),
            reason='按当前帧定位并逐贴合位检测' if used_targets else '定位匹配，但没有可靠贴条/裸位证据')
        return finish()


def analyze(video,model_path,out,*,run_id,options=None,progress=None,max_seconds=600,pose_detector=None,**unused):
    started=time.perf_counter();options=options or {};frozen=options.get('sticker_config') or snapshot();verify(frozen)
    detector=StickerDetector(frozen);sequence=create_sequence(frozen['config'])
    meta=probe(video,max_seconds)
    start,end,sample_fps,_,_=validate_options(meta,options.get('start',0),options.get('end'),options.get('sample_fps',4),None,640)
    if sample_fps<2:raise AnalysisError('INVALID_SAMPLE_FPS','贴条连续视觉确认至少需要2FPS采样')
    if options.get('crop'):raise AnalysisError('INVALID_STATION_CROP','贴条检测需要完整工位画面进行定位')
    from .person_detection import load_detector,predict_people,snapshot as person_snapshot,verify as verify_person
    from .tracking import PoseTracker
    from .skeleton import topology
    person_config=options.get('person_config') or person_snapshot(model_path,options.get('pose_backend') or 'yolo')
    verify_person(person_config)
    if pose_detector is None:pose_detector=load_detector(model_path,person_config['backend'])
    tracker=PoseTracker()
    out=Path(out);out.mkdir(parents=True,exist_ok=True);cap=cv2.VideoCapture(str(video))
    preview=(720,1280) if meta['height']>meta['width'] else (1280,720)
    writer=cv2.VideoWriter(str(out/'video.webm'),cv2.VideoWriter_fourcc(*'VP80'),min(meta['fps'],15),preview)
    if not writer.isOpened():cap.release();raise AnalysisError('ENCODER_UNAVAILABLE','无法编码检测回放')
    first,last=round(start*meta['fps']),min(meta['frame_count'],int(end*meta['fps']));cap.set(cv2.CAP_PROP_POS_FRAMES,first)
    frames=[];next_sample=next_preview=0;evidence=None
    try:
        for index in range(first,last):
            if progress:progress((index-first)/max(1,last-first))
            ok,image=cap.read()
            if not ok:raise AnalysisError('DECODE_INTERRUPTED','贴条视频解码提前中断')
            t=(index-first)/meta['fps']
            if t+1e-6>=next_preview:writer.write(cv2.resize(image,preview));next_preview+=1/min(meta['fps'],15)
            if t+1e-6<next_sample:continue
            next_sample+=1/sample_fps
            sample_started=time.perf_counter()
            people=predict_people(pose_detector,image,tracker,t)
            inspection=detector.inspect(image);inspection['sequence']=sequence.update(t,inspection)
            frames.append(dict(time=round(t,4),inspection=inspection,people=people,
                               processingMs=round((time.perf_counter()-sample_started)*1000,1)))
            if inspection['registered']:evidence=cv2.resize(image,preview)
    finally:cap.release();writer.release()
    verify(frozen);verify_person(person_config)
    for index,frame in enumerate(frames):
        frame['end']=frames[index+1]['time'] if index+1<len(frames) else round(end-start,4)
    targets,assessment=summarize(frames,frozen['config'],sequence,final=True)
    process=sequence.snapshot();decision=compliance(targets,process,final=True);counts=Counter(t['state'] for t in targets)
    action_mode=frozen['config'].get('mode')=='action'
    summary=dict(expected=len(targets),present=counts['ACTION_DONE'] if action_mode else counts['PRESENT'],
        empty=counts['EMPTY'],unknown=counts['NOT_OBSERVED'] if action_mode else counts['UNKNOWN'],wrong=counts['WRONG'],
        assessmentTime=assessment,analyzedFrames=len(frames),registeredFrames=sum(f['inspection']['registered'] for f in frames),
        sequenceStatus=process['status'],sampleFps=sample_fps,processingSeconds=round(time.perf_counter()-started,2),
        maxPeople=max((len(f['people']) for f in frames),default=0),
        trackCount=len({p['trackId'] for f in frames for p in f['people']}),
        p95InferenceMs=float(np.percentile([f['processingMs'] for f in frames],95)) if frames else 0,
        actionsConfirmed=counts['ACTION_DONE'],missingActions=counts['NOT_OBSERVED'])
    model_hash=frozen['assets'][frozen['config']['model']]
    data=dict(version=2,analysisType='sticker',detectionClasses=['sticker','person'],skeleton=topology(),
        personModel=dict(name=getattr(pose_detector,'label',Path(model_path).stem),
                         backend=person_config['backend'],sha256=person_config['sha256']),
        id=run_id,generatedAt=datetime.now(timezone.utc).isoformat(),
        source=dict(title=options.get('title',Path(video).name),sha256=file_hash(video),duration=end-start,width=meta['width'],height=meta['height'],video='video.webm'),
        model=dict(name=frozen['config']['model_name'],sha256=model_hash,device=str(detector.device)),calibration=frozen,
        inspectionMode=frozen['config'].get('mode','appearance'),verdict=decision['status'],compliance=decision,summary=summary,targets=targets,sequence=process,frames=frames,
        limitations=['正确视频开发训练；真实漏贴/错序成品检出率尚未独立验证。','遮挡和未检出保留未知，不等同未贴。',
            '每处需要真实裸位→已贴视觉证据，按观察时间核对先两个三角后方形；不使用视频时间脚本。',
            '从成品开始或遮挡期间贴好时不能证明工序顺序；不测量胶粘强度。'])
    if isinstance(sequence,StickerVisualCoverage):
        data['limitations']=['同机位正确示范视频开发训练；真实漏贴、错形成品检出率尚未独立验证。',
            '按本轮所有标定位置的多帧外观确认判定；不要求贴前基线或三角、方形的操作先后。',
            '遮挡、未检出和定位失败保留未知或已有确认；新轮次清空历史。',
            '确认贴片存在及形状，不测量胶粘强度；明确相反证据会更新位置状态。']
    elif frozen['config'].get('mode')=='action':
        data['limitations']=['按用户要求以贴放/按压动作计完成，不核验贴片实际存在、形状、外观或粘附。',
            '动作模型在当前单条视频上开发训练；同源帧核对不代表独立工位准确率。',
            '需要同工位定位模板及明确新轮次；离开、接触和运动证据不足时不计动作。',
            '相似扶持/经过动作仍可能混淆，部署前需补录不同人员及真实漏动作、错序样本。']
    data['limitations'].append('人体框、17点骨骼和姿态来自同一采样帧；人体姿态观察不参与贴条工序合规判定。')
    (out/'analysis.json').write_text(json.dumps(data,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    (out/'events.json').write_text(json.dumps(process,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'segments.csv').open('w',encoding='utf-8-sig',newline='') as f:
        table=csv.writer(f);table.writerow(['位置','名称','类型','最终状态','分数','动作时间'])
        for t in targets:table.writerow([t['id'],t['name'],t['expected'],LABELS[t['state']],t['score'],t['lastSeen']])
    if evidence is not None:cv2.imwrite(str(out/'evidence-1.jpg'),evidence)
    rows=''.join(f'<tr><td>{html.escape(t["id"])}</td><td>{html.escape(t["name"])}</td><td>{LABELS[t["state"]]}</td><td>{t["lastSeen"]}</td></tr>' for t in targets)
    order_title='先完成两处三角动作，再完成方形动作' if action_mode else '先贴两处三角，再贴后续方形贴'
    if isinstance(sequence,StickerVisualCoverage):order_title='全部标定位置累计外观确认'
    report=f'<!doctype html><meta charset="utf-8"><title>贴条工序检测报告</title><style>body{{font:16px/1.8 system-ui;max-width:1000px;margin:30px auto;padding:20px}}td{{padding:10px;border-bottom:1px solid #ddd}}img{{max-width:100%}}</style><h1>贴条工序检测：{decision["label"]}</h1><p>{html.escape(decision["reason"])}</p><p>{html.escape(data["source"]["title"])}</p><p>人体检测：峰值 {summary["maxPeople"]} 人 · {summary["trackCount"]} 条轨迹 · 17点骨骼随视频回放显示</p><h2>{order_title}</h2><p>{html.escape(process["reason"])}</p><table>{rows}</table><h2>验证边界</h2><ul>'+''.join('<li>'+html.escape(v)+'</li>' for v in data['limitations'])+'</ul><img src="evidence-1.jpg">'
    (out/'report.html').write_text(report,encoding='utf-8')
    manifest=dict(schema_version=1,run_id=run_id,model_hash=model_hash,person_model_hash=person_config['sha256'],source_hash=data['source']['sha256'],options=options,
        verdict=decision['status'],files={p.name:file_hash(p) for p in out.iterdir() if p.is_file() and p.name!='manifest.json'})
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return data
