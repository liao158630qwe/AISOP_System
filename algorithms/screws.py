"""Source-calibrated screw-head/empty-hole inspection with explicit abstention."""
import copy
from collections import Counter
from datetime import datetime, timezone
import csv
import html
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from .video import AnalysisError, file_hash, probe, validate_options
from .asset_snapshot import AssetSnapshotCache
from .evidence import EvidenceWindow

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT/'config/screw-inspection.json'
LABELS = {'PRESENT': '可见螺丝头', 'EMPTY': '可见空孔', 'UNKNOWN': '遮挡或证据不足'}
SEQUENCE_LABELS = {'COMPLETED': '已确认', 'PENDING': '待确认', 'EMPTY': '明确空孔',
    'PREASSEMBLED_OK': '预装件已确认', 'PREASSEMBLED_MISSING': '预装件缺失',
    'PREASSEMBLED_UNKNOWN': '预装件待确认', 'UNKNOWN': '证据不足'}


_snapshot_cache = AssetSnapshotCache()


def _asset_names(config):
    names = [config['model'], config['reference']]
    names += [p['reference'] for p in config.get('calibrations',[])]
    names += list(config.get('target_models',{}).values())
    names += [path for p in config.get('calibrations',[]) for path in p.get('target_models',{}).values()]
    return names


def snapshot():
    return _snapshot_cache.get(CONFIG, ROOT, _asset_names,
        missing_code='SCREW_MODEL_MISSING', missing_message='螺丝专用模型或标定资源缺失')


def verify(frozen):
    if any(not (ROOT/n).is_file() or file_hash(ROOT/n) != sha for n,sha in frozen['assets'].items()):
        raise AnalysisError('MODEL_CHANGED', '螺丝模型或标定图已改变，请重新创建任务')


class ScrewDetector:
    def __init__(self, frozen=None):
        from ultralytics import YOLO
        self.frozen = frozen or snapshot()
        verify(self.frozen)
        self.config = self.frozen['config']
        self.refined=None
        if self.config.get('algorithm') in ('local-roi-v2','multi-view-local-v1'):
            if self.config.get('algorithm')=='multi-view-local-v1':
                from .screw_multiview import MultiViewScrewDetector
                self.refined=MultiViewScrewDetector(self.frozen)
            else:
                from .screw_refined import RefinedScrewDetector
                self.refined=RefinedScrewDetector(self.frozen)
            self.reference=self.refined.reference
            self.model=self.refined.model
            self.device=self.refined.device
            return
        self.reference = cv2.imread(str(ROOT/self.config['reference']))
        self.sift = cv2.SIFT_create(nfeatures=2500, contrastThreshold=.025)
        mask = np.zeros(self.reference.shape[:2], np.uint8)
        cv2.fillPoly(mask, [np.array([[680,180],[900,200],[990,990],[580,1000],[500,680]])], 255)
        self.keypoints,self.descriptors = self.sift.detectAndCompute(cv2.cvtColor(self.reference,cv2.COLOR_BGR2GRAY),mask)
        self.matcher = cv2.BFMatcher()
        self.model = YOLO(str(ROOT/self.config['model']))
        self.device = 0 if __import__('torch').cuda.is_available() else 'cpu'

    def inspect(self, image):
        if self.refined is not None:return self.refined.inspect(image)
        started = time.perf_counter()
        frame = cv2.resize(image, tuple(self.config['reference_size']), interpolation=cv2.INTER_AREA)
        targets = [dict(**target, state='UNKNOWN', score=None, bbox=None) for target in self.config['targets']]
        result = dict(targets=targets, registered=False, inliers=0, reason='工件被遮挡、已移走或视角与标定不匹配')
        gray = cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        kp,desc = self.sift.detectAndCompute(gray,None)
        if desc is None:
            return result
        good = [m for m,n in self.matcher.knnMatch(self.descriptors,desc,k=2) if m.distance < .7*n.distance]
        if len(good)<14:
            return result
        src = np.float32([self.keypoints[m.queryIdx].pt for m in good])
        dst = np.float32([kp[m.trainIdx].pt for m in good])
        H, inliers = cv2.findHomography(src,dst,cv2.RANSAC,3)
        if H is None or inliers.sum()<12 or inliers.mean()<.35:
            return result
        polygon=np.float32([[[650,200],[930,200],[980,1000],[560,1000]]])
        projected=cv2.perspectiveTransform(polygon,H)[0]
        area=abs(cv2.contourArea(projected))
        if not 70000<area<700000 or not np.isfinite(projected).all():
            return result
        # A position can remain geometrically located while a hand hides the
        # component. Keep that box, but leave its observation UNKNOWN.
        for target in targets:
            x,y=target['point']
            corners=np.float32([[[x-12,y-12],[x+12,y+12]]])
            a,b=cv2.perspectiveTransform(corners,H)[0]
            target['bbox']=[float(min(a[0],b[0])/1920),float(min(a[1],b[1])/1080),
                            float(max(a[0],b[0])/1920),float(max(a[1],b[1])/1080)]
        aligned=cv2.warpPerspective(frame,np.linalg.inv(H),(1920,1080))
        # Fixture/background features can survive removal of the product.
        # Require appearance support on two separate component bodies. These
        # anchors exclude screw heads and do not depend on the detected count.
        aligned_gray=cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY)
        reference_gray=cv2.cvtColor(self.reference,cv2.COLOR_BGR2GRAY)
        anchor_scores=[]
        for x1,y1,x2,y2 in self.config['component_anchors']:
            template=reference_gray[y1:y2,x1:x2]
            search=aligned_gray[y1-12:y2+12,x1-12:x2+12]
            anchor_scores.append(float(cv2.minMaxLoc(cv2.matchTemplate(search,template,cv2.TM_CCOEFF_NORMED))[1]))
        result['componentScores']=[round(s,4) for s in anchor_scores]
        if sum(s>=self.config['component_threshold'] for s in anchor_scores)<self.config['minimum_visible_components']:
            result['reason']='工件主体被遮挡、移动或已离场；治具匹配不作为工件完成证据'
            return result
        crops,origins=[],[]
        for target in targets:
            x,y=map(round,target['point'])
            x0,y0=x-40,y-40
            origins.append((x0,y0))
            crops.append(aligned[y0:y0+80,x0:x0+80])
            corners=np.float32([[[x-12,y-12],[x+12,y+12]]])
            a,b=cv2.perspectiveTransform(corners,H)[0]
            target['bbox']=[float(min(a[0],b[0])/1920),float(min(a[1],b[1])/1080),
                            float(max(a[0],b[0])/1920),float(max(a[1],b[1])/1080)]
        predictions=self.model.predict(crops,imgsz=192,conf=self.config['threshold'],iou=.45,
            device=self.device,verbose=False,max_det=12)
        candidates=[]
        for prediction,(x0,y0) in zip(predictions,origins):
            for box,cls,score in zip(prediction.boxes.xyxy.cpu().tolist(),prediction.boxes.cls.cpu().tolist(),prediction.boxes.conf.cpu().tolist()):
                x,y=x0+(box[0]+box[2])/2,y0+(box[1]+box[3])/2
                candidates.append((float(score),int(cls),x,y,[x0+box[0],y0+box[1],x0+box[2],y0+box[3]]))
        unique=[]
        for candidate in sorted(candidates,reverse=True):
            if not any(np.hypot(candidate[2]-p[2],candidate[3]-p[3])<9 for p in unique):
                unique.append(candidate)
        # Each detected head belongs to at most one configured location.
        pairs=sorted((float(np.hypot(p[2]-t['point'][0],p[3]-t['point'][1])),i,j)
                     for i,t in enumerate(targets) for j,p in enumerate(unique))
        used_targets,used_detections=set(),set()
        for distance,i,j in pairs:
            if distance>16 or i in used_targets or j in used_detections:
                continue
            used_targets.add(i); used_detections.add(j)
            score,cls,_,_,box=unique[j]
            # Empty holes require stronger evidence; absence of a detection
            # always remains UNKNOWN, including glove/skin/tool occlusion.
            targets[i].update(state='PRESENT' if cls==0 else 'EMPTY' if score>=.65 else 'UNKNOWN',score=round(score,4))
            corners=np.float32([[[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]]])
            projected_box=cv2.perspectiveTransform(corners,H)[0]
            targets[i]['detectedBbox']=[float(projected_box[:,0].min()/1920),float(projected_box[:,1].min()/1080),
                                         float(projected_box[:,0].max()/1920),float(projected_box[:,1].max()/1080)]
        result.update(registered=True,inliers=int(inliers.sum()),reason='按工件特征对齐；逐位置检测',
                      inferenceMs=round((time.perf_counter()-started)*1000,2))
        return result


TRANSIENT_TARGET_FIELDS={'state','score','lastSeen','confirmedAt','firstSeen','bbox','detectedBbox',
    'reason','contextScore','searchRoi','positionMode','historyConfirmed','sequenceState',
    'sequenceIndex','sequenceObserved','sequenceReason'}
OBSERVED_STATES={'PRESENT','EMPTY','WRONG'}


def sequence_profiles(config):
    """Return operation orders keyed by the calibration that owns them.

    The original view uses the top-level sequence.  A calibrated view must
    declare its own sequence because target ids are view-local; falling back to
    the original ids would silently validate the wrong physical positions.
    """
    result={}
    if config.get('sequence') is not None:
        result['original']=copy.deepcopy(config['sequence'])
    for calibration in config.get('calibrations',[]):
        if calibration.get('id') and calibration.get('sequence') is not None:
            result[calibration['id']]=copy.deepcopy(calibration['sequence'])
    return result


def validate_sequence(sequence,targets):
    """Validate an operation order; return a human-readable error or None."""
    if not isinstance(sequence,list) or not sequence:
        return '工序顺序必须是非空位置编号列表'
    ids=[target.get('id') for target in targets]
    if any(identity is None for identity in ids) or len(ids)!=len(set(ids)):
        return '标定位置编号缺失或重复'
    preassembled={target['id'] for target in targets if target.get('preassembled')}
    expected=[identity for identity in ids if identity not in preassembled]
    if len(sequence)!=len(set(sequence)):
        return '工序顺序中存在重复位置'
    unknown=[identity for identity in sequence if identity not in set(ids)]
    if unknown:
        return '工序顺序包含未知位置：'+', '.join(map(str,unknown))
    preassembled_in_order=[identity for identity in sequence if identity in preassembled]
    if preassembled_in_order:
        return '预装件不应出现在操作工序中：'+', '.join(preassembled_in_order)
    missing=[identity for identity in expected if identity not in sequence]
    if missing or set(sequence)!=set(expected):
        return '工序顺序必须恰好包含全部非预装位置，缺少：'+', '.join(missing or ['无'])
    return None


def _target_schema(target):
    return {k:copy.deepcopy(v) for k,v in target.items() if k not in TRANSIENT_TARGET_FIELDS}


class ScrewObservationHistory:
    """Accumulate confirmed screw observations for one inspection cycle.

    A registered frame is evidence, not the result. UNKNOWN, a lost product
    registration and one-frame confidence changes never erase a confirmed
    observation. Two agreeing clear observations are required before a state
    is written, and two agreeing contrary observations update it.

    Compliance is a per-position visual coverage check.  Configured sequence
    data only controls display order; it never gates completion and is not an
    installation-order requirement.
    """

    def __init__(self,targets,confirmation_frames=2,sequences=None,early_presence_threshold=None,
                 max_gap=2.0,max_window=4.0):
        self.targets=[_target_schema(t) for t in targets]
        self.confirmation_frames=max(1,int(confirmation_frames))
        self.profiles={}
        self.active_profile=None
        self.assessment_time=None
        self.sequence_configs=self._normalize_sequences(sequences)
        self._confirmation_serial=0
        self._votes={}
        self.last_time=None
        self.max_gap=max_gap;self.max_window=max_window
        # Validate settings even before a frame arrives.
        EvidenceWindow(self.confirmation_frames,max_gap,max_window)

    @staticmethod
    def _normalize_sequences(sequences):
        if sequences is None:
            return None
        if isinstance(sequences,dict):
            return {key:copy.deepcopy(value) for key,value in sequences.items()}
        return {'original':copy.deepcopy(sequences)}

    def _sequence_config(self,profile):
        if self.sequence_configs is None:
            return None
        key='original' if profile is None else profile
        return self.sequence_configs.get(key)

    def _coverage_order(self,profile,targets):
        operation_ids=[target['id'] for target in targets
            if target.get('id') is not None and not target.get('preassembled')]
        configured=self._sequence_config(profile) or []
        order=[]
        for identity in configured:
            if identity in operation_ids and identity not in order:
                order.append(identity)
        for identity in operation_ids:
            if identity not in order:
                order.append(identity)
        return order

    def _ensure_coverage(self,state,profile):
        targets=state['targets']
        order=self._coverage_order(profile,targets)
        preassembled={target['id']:'PREASSEMBLED_UNKNOWN' for target in targets
            if target.get('id') is not None and target.get('preassembled')}
        if 'sequence' not in state:
            state['sequence']=dict(configured=bool(targets),verificationMode='visual-cumulative',
                status='PENDING',order=order,current=order[0] if order else None,
                completed=[],pending=list(order),early=[],empty=[],preassembled=preassembled,
                reason='等待各位置取得至少两帧一致的视觉证据',failure=None,events=[],cursor=0)
        else:
            state['sequence'].update(configured=bool(targets),
                verificationMode='visual-cumulative',order=order,preassembled=preassembled)
        for target in targets:
            identity=target.get('id')
            if identity is not None:
                state['sites'].setdefault(identity,dict(confirmed=None,score=None,confirmedAt=None,
                    lastSeen=None,firstSeen=None,lastObserved=None,pending=None,pendingCount=0,
                    pendingFirst=None,pendingScore=None,confirmationVersion=0))

    def _update_coverage(self,state,time):
        sequence=state.get('sequence')
        if not sequence:
            return
        if not sequence['configured']:
            sequence.update(status='REVIEW',reason='没有标定位置，无法进行视觉确认',
                failure=None,completed=[],pending=[],current=None,empty=[],early=[])
            return
        sites=state['sites']
        order=sequence['order']
        completed=[];pending=[];empty=[]
        preassembled={};missing=[]
        for identity in sequence['preassembled']:
            confirmed=(sites.get(identity) or {}).get('confirmed')
            if confirmed=='PRESENT':
                preassembled[identity]='PREASSEMBLED_OK'
            elif confirmed=='EMPTY':
                preassembled[identity]='PREASSEMBLED_MISSING'
                missing.append(identity);empty.append(identity)
            else:
                preassembled[identity]='PREASSEMBLED_UNKNOWN'
        for identity in order:
            confirmed=(sites.get(identity) or {}).get('confirmed')
            if confirmed=='PRESENT':
                completed.append(identity)
            else:
                pending.append(identity)
            if confirmed=='EMPTY':
                empty.append(identity)
        sequence.update(completed=completed,pending=pending,cursor=len(completed),
            current=pending[0] if pending else None,preassembled=preassembled,
            empty=empty,early=[])
        sequence['events']=[dict(type='CONFIRMED',id=identity,state=site.get('confirmed'),
            time=site.get('confirmedAt'),score=site.get('score'))
            for identity,site in sites.items() if site.get('confirmed') in OBSERVED_STATES]
        sequence['events'].sort(key=lambda event:(event.get('time') or 0,str(event.get('id'))))
        pre_ok=sum(value=='PREASSEMBLED_OK' for value in preassembled.values())
        if len(completed)==len(order) and pre_ok==len(preassembled):
            sequence.update(status='PASS',failure=None,
                reason=(f'全部 {len(order)} 处操作位置及 {len(preassembled)} 处预装件'
                        '均已取得累计视觉确认'))
        else:
            # An operation position can legitimately be empty before it is
            # installed.  Keep that observation in the cycle history, but do
            # not turn it into a failure until the cycle is finalized.
            pending_positions=pending+[identity for identity,value in preassembled.items()
                if value!='PREASSEMBLED_OK']
            empty_note=f'；当前明确为空：{", ".join(empty)}' if empty else ''
            sequence.update(status='PENDING',failure=None,
                reason=(f'已确认 {len(completed)+pre_ok}/{len(order)+len(preassembled)} 处位置；'
                        f'待确认：{", ".join(pending_positions) or "无"}{empty_note}'))

    def _sequence_snapshot(self,state,final=False):
        sequence=state.get('sequence') if state else None
        if sequence is None:
            return None
        result=copy.deepcopy(sequence)
        sites=state.get('sites',{})
        order=result.get('order',[])
        result['pending']=list(result.get('pending',[]))
        result['current']=result.get('current')
        result['operationsExpected']=len(order)
        result['operationsCompleted']=len(result.get('completed',[]))
        result['positionsExpected']=len(state.get('targets',[]))
        result['positionsConfirmed']=sum((site or {}).get('confirmed') in ('PRESENT','EMPTY')
            for site in sites.values())
        result['preassembledExpected']=len(result['preassembled'])
        result['preassembledConfirmed']=sum(value=='PREASSEMBLED_OK'
            for value in result['preassembled'].values())
        if not result['configured']:
            result['status']='REVIEW'
            result['reason']='没有标定位置，无法进行视觉确认'
        elif final and result['status']=='PENDING':
            missing=[identity for identity,value in result['preassembled'].items()
                if value=='PREASSEMBLED_MISSING']
            empty=[identity for identity in order
                if (sites.get(identity) or {}).get('confirmed')=='EMPTY']
            unknown=[identity for identity in order
                if (sites.get(identity) or {}).get('confirmed') not in ('PRESENT','EMPTY')]
            unknown += [identity for identity,value in result['preassembled'].items()
                if value=='PREASSEMBLED_UNKNOWN']
            if missing:
                result['status']='FAIL'
                result['failure']='检测结束时预装件仍明确缺失：'+', '.join(missing)
            elif empty:
                result['status']='FAIL'
                result['failure']='检测结束时仍有明确空孔：'+', '.join(empty)
            else:
                result['status']='REVIEW'
                result['reason']=('检测结束时仍有位置证据不足：'+', '.join(unknown)
                    if unknown else '检测结束时证据不足，不能判定通过')
        if result['status']=='FAIL':
            result['reason']=result.get('failure') or '明确空孔或预装件缺失，不能判定通过'
        return result

    def update(self,time,inspection):
        if not math.isfinite(time) or (self.last_time is not None and time<=self.last_time):
            raise ValueError('采样时间必须有限且严格递增')
        self.last_time=time
        if not inspection.get('registered'):
            return self.aggregate()
        targets=inspection.get('targets') or []
        if not targets:
            return self.aggregate()
        profile=inspection.get('calibrationId')
        state=self.profiles.setdefault(profile,dict(targets=[],sites={}))
        state['targets']=[_target_schema(t) for t in targets]
        self.active_profile=profile
        self.assessment_time=time
        self._ensure_coverage(state,profile)
        for target in targets:
            identity=target.get('id')
            if identity is None:
                continue
            value=target.get('state')
            score=target.get('score')
            site=state['sites'].setdefault(identity,dict(confirmed=None,score=None,confirmedAt=None,
                lastSeen=None,firstSeen=None,lastObserved=None,pending=None,pendingCount=0,
                pendingFirst=None,pendingScore=None,confirmationVersion=0))
            votes=self._votes.get((profile,identity))
            if votes is None:
                votes=self._votes[(profile,identity)]=EvidenceWindow(
                    self.confirmation_frames,self.max_gap,self.max_window)
            if value not in OBSERVED_STATES:
                votes.observe(time)
                continue
            if value==site['confirmed']:
                votes.reset()
                votes.observe(time)
                site['lastObserved']=time
                site.update(pending=None,pendingCount=0,pendingFirst=None,pendingScore=None)
                continue
            confirmed=votes.observe(time,value,score)
            if confirmed is not None:
                self._confirmation_serial+=1
                site.update(confirmed=value,score=confirmed.score,confirmedAt=time,lastSeen=time,
                    firstSeen=confirmed.first,confirmationVersion=self._confirmation_serial)
                site.update(pending=None,pendingCount=0,pendingFirst=None,pendingScore=None)
        self._update_coverage(state,time)
        return self.aggregate()

    def aggregate(self,final=False):
        state=self.profiles.get(self.active_profile)
        targets=state['targets'] if state else self.targets
        sites=state['sites'] if state else {}
        sequence=self._sequence_snapshot(state,final) if state else None
        output=[]
        for target in targets:
            site=sites.get(target.get('id'))
            confirmed=site['confirmed'] if site else None
            item=dict(target,state=confirmed or 'UNKNOWN',score=site['score'] if confirmed else None,
                lastSeen=site['lastSeen'] if confirmed else None,confirmedAt=site['confirmedAt'] if confirmed else None,
                historyConfirmed=bool(confirmed),bbox=None)
            if sequence is not None:
                identity=target.get('id')
                observed=confirmed if confirmed in ('PRESENT','EMPTY') else 'UNKNOWN'
                index=sequence['order'].index(identity) if identity in sequence['order'] else None
                if not sequence['configured']:
                    sequence_state='UNKNOWN'
                elif identity in sequence['preassembled']:
                    sequence_state=sequence['preassembled'][identity]
                elif observed=='PRESENT':
                    sequence_state='COMPLETED'
                elif observed=='EMPTY':
                    sequence_state='EMPTY'
                else:
                    sequence_state='PENDING'
                item.update(sequenceState=sequence_state,sequenceIndex=index,
                    sequenceObserved=observed,sequenceReason=sequence['reason'])
            output.append(item)
        return output

    def sequence(self,final=False):
        state=self.profiles.get(self.active_profile)
        return self._sequence_snapshot(state,final) if state else None


def create_history(config):
    settings=config.get('confirmation',{})
    EvidenceWindow(settings.get('frames',2),settings.get('max_gap_seconds',2.0),settings.get('window_seconds',4.0))
    return ScrewObservationHistory(config['targets'],
        confirmation_frames=settings.get('frames',2),sequences=sequence_profiles(config),
        max_gap=settings.get('max_gap_seconds',2.0),max_window=settings.get('window_seconds',4.0))


def final_observations(frames,targets,max_age=2,confirmation_frames=2,sequences=None,
        early_presence_threshold=None,max_gap=2.0,max_window=4.0):
    """Aggregate a completed sample list with cycle-scoped confirmed history.

    max_age is retained for callers of the previous API but no longer erases
    history. Start a new ScrewObservationHistory for the next product cycle.
    """
    history=ScrewObservationHistory(targets,confirmation_frames,sequences=sequences,
        max_gap=max_gap,max_window=max_window)
    for frame in frames:
        history.update(frame['time'],frame['inspection'])
    return history.aggregate(final=True),history.assessment_time


def compliance(targets,sequence=None,final=False):
    """Verdict for cumulative per-position visual confirmation.

    When sequence is omitted the legacy visible-state rule is retained for the
    sticker appearance detector, which shares final_observations.  Screw
    inspection passes its per-view coverage snapshot so every operation
    position and preassembled part must be confirmed, without requiring a
    particular installation order.
    """
    empty=[t['id'] for t in targets if t['state']=='EMPTY']
    unknown=[t['id'] for t in targets if t['state']=='UNKNOWN']
    if sequence is None:
        status='FAIL' if empty else 'REVIEW' if unknown or not targets else 'PASS'
        return {'status':status,'label':{'PASS':'合规','FAIL':'不合规','REVIEW':'无法判定'}[status],
                'scope':f'{len(targets)}处标定位置螺丝安装视觉检查（含预装件，不包含扭矩验证）',
                'reason':('明确发现空孔：'+', '.join(empty)) if empty else
                         ('证据不足：'+', '.join(unknown)) if unknown else
                         '全部参考位置均取得至少两帧可见螺丝头证据' if targets else '没有有效检测位置',
                'empty':empty,'unknown':unknown}
    sequence_status=sequence.get('status','REVIEW')
    status='PASS' if sequence_status=='PASS' else 'FAIL' if sequence_status=='FAIL' else 'REVIEW'
    missing=[identity for identity,value in sequence.get('preassembled',{}).items()
             if value=='PREASSEMBLED_MISSING']
    sequence_reason=sequence.get('reason') or (
        '没有标定位置，无法进行视觉确认' if not sequence.get('configured',False)
        else '位置证据不足，不能判定通过')
    operations_expected=sequence.get('operationsExpected',len(targets))
    preassembled_expected=sequence.get('preassembledExpected',len(sequence.get('preassembled',{})))
    return {'status':status,'label':{'PASS':'合规','FAIL':'不合规','REVIEW':'无法判定'}[status],
            'scope':(f'{operations_expected}处操作位置及{preassembled_expected}处预装件的'
                     '累计视觉确认；不包含扭矩验证'),
            'reason':sequence_reason,'empty':empty,'unknown':unknown,'early':[],
            'preassembledMissing':missing,'sequenceStatus':sequence_status,
            'operationsCompleted':sequence.get('operationsCompleted',0),
            'operationsExpected':operations_expected,
            'preassembledConfirmed':sequence.get('preassembledConfirmed',0),
            'preassembledExpected':preassembled_expected,
            'verificationMode':'visual-cumulative'}


def current_display(inspection):
    """Show only detections in this sample; no prior geometry or optical flow."""
    inspection['displayTargets']=[dict(t,bbox=list(t.get('detectedBbox') or t['bbox']),positionMode='DETECTED')
        for t in inspection['targets'] if inspection['registered'] and t['state'] in ('PRESENT','EMPTY')
        and (t.get('detectedBbox') is not None or t.get('bbox') is not None)]
    return inspection


def analyze(video,model_path,out,*,run_id,options=None,progress=None,max_seconds=600,pose_detector=None,**unused):
    started=time.perf_counter()
    options=options or {}
    frozen=options.get('screw_config') or snapshot()
    verify(frozen)
    detector=ScrewDetector(frozen)
    history=create_history(frozen['config'])
    meta=probe(video,max_seconds)
    start,end,sample_fps,crop,imgsz=validate_options(meta,options.get('start',0),options.get('end'),options.get('sample_fps',4),None,640)
    if options.get('crop'):
        raise AnalysisError('INVALID_STATION_CROP','螺丝检测需要完整画面进行工件定位')
    from .person_detection import load_detector, predict_people, snapshot as person_snapshot, verify as verify_person
    from .tracking import PoseTracker
    from .skeleton import topology
    person_config=options.get('person_config') or person_snapshot(model_path,options.get('pose_backend') or 'yolo')
    verify_person(person_config)
    if pose_detector is None:
        pose_detector=load_detector(model_path,person_config['backend'])
    tracker=PoseTracker()
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    cap=cv2.VideoCapture(str(video))
    writer=cv2.VideoWriter(str(out/'video.webm'),cv2.VideoWriter_fourcc(*'VP80'),min(meta['fps'],15),(1280,720))
    if not writer.isOpened():
        cap.release()
        raise AnalysisError('ENCODER_UNAVAILABLE','无法编码回放视频')
    frames=[];next_sample=next_preview=0;registered_image=None
    first,last=round(start*meta['fps']),min(meta['frame_count'],int(end*meta['fps']))
    cap.set(cv2.CAP_PROP_POS_FRAMES,first)
    try:
        for index in range(first,last):
            if progress: progress((index-first)/max(1,last-first))
            ok,image=cap.read()
            if not ok: raise AnalysisError('DECODE_INTERRUPTED','视频提前解码中断')
            t=(index-first)/meta['fps']
            if t+1e-6>=next_preview:
                writer.write(cv2.resize(image,(1280,720)))
                next_preview+=1/min(meta['fps'],15)
            if t+1e-6<next_sample: continue
            next_sample+=1/sample_fps
            sample_started=time.perf_counter()
            people=predict_people(pose_detector,image,tracker,t)
            inspection=current_display(detector.inspect(image))
            sample_time=round(t,4)
            aggregate=history.update(sample_time,inspection)
            inspection['sequence']=history.sequence()
            frames.append(dict(time=sample_time,inspection=inspection,aggregateTargets=aggregate,
                               people=people,processingMs=round((time.perf_counter()-sample_started)*1000,1)))
            if inspection['registered']:
                registered_image=cv2.resize(image,(1920,1080))
    finally:
        cap.release();writer.release()
    verify(frozen)
    verify_person(person_config)
    for index,frame in enumerate(frames):
        frame['end']=frames[index+1]['time'] if index+1<len(frames) else round(end-start,4)
    targets,assessment_time=history.aggregate(final=True),history.assessment_time
    sequence=history.sequence(final=True)
    counts=Counter(t['state'] for t in targets)
    summary=dict(expected=len(targets),present=counts['PRESENT'],empty=counts['EMPTY'],unknown=counts['UNKNOWN'],
        assessmentTime=assessment_time,analyzedFrames=len(frames),registeredFrames=sum(f['inspection']['registered'] for f in frames),
        sampleFps=sample_fps,maxPeople=max((len(f['people']) for f in frames),default=0),
        trackCount=len({p['trackId'] for f in frames for p in f['people']}),
        sequenceStatus=sequence['status'] if sequence else None,
        verificationMode='visual-cumulative',
        positionsExpected=sequence['positionsExpected'] if sequence else len(targets),
        positionsConfirmed=sequence['positionsConfirmed'] if sequence else 0,
        operationsExpected=sequence['operationsExpected'] if sequence else None,
        operationsCompleted=sequence['operationsCompleted'] if sequence else None,
        preassembledExpected=sequence['preassembledExpected'] if sequence else None,
        preassembledConfirmed=sequence['preassembledConfirmed'] if sequence else None,
        processingSeconds=round(time.perf_counter()-started,2),
        p95InferenceMs=float(np.percentile([f['processingMs'] for f in frames],95)) if frames else 0)
    model_hash=frozen['assets'][frozen['config']['model']]
    decision=compliance(targets,sequence,final=True)
    data=dict(version=2,analysisType='screw',detectionClasses=['screw','person'],skeleton=topology(),
        personModel=dict(name=getattr(pose_detector,'label',Path(model_path).stem),
                         backend=person_config['backend'],sha256=person_config['sha256']),
        id=run_id,generatedAt=datetime.now(timezone.utc).isoformat(),
        verificationMode='visual-cumulative',
        verdict=decision['status'],compliance=decision,visualResult='ALL_VISIBLE' if counts['PRESENT']==len(targets) else 'EMPTY_OBSERVED' if counts['EMPTY'] else 'INSUFFICIENT_EVIDENCE',
        source=dict(title=options.get('title',Path(video).name),sha256=file_hash(video),duration=end-start,width=meta['width'],height=meta['height'],video='video.webm'),
        model=dict(name=frozen['config'].get('model_name','YOLO11n 螺丝头／空孔专用微调'),sha256=model_hash,device=str(detector.device)),
        calibration=frozen,summary=summary,targets=targets,sequence=sequence,frames=frames,
        limitations=[frozen['config']['scope'],'训练数据主要来自正确工序视频；真实漏打检出率尚未验证。',
                     '人体框、17点骨骼和姿态来自同一采样帧；姿态观察不参与螺丝合规判定。',
                     '未检出不等于漏打；遮挡、工件移走、模糊和不匹配视角均保留证据不足。',
                     '每个位置至少两帧一致才写入本轮历史；各位置独立累计确认，不要求安装先后顺序。',
                     '配置顺序只用于界面展示排序；未配置顺序的标定视角仍按位置清单正常判定。',
                     '全部操作位置和预装件确认后即时判为合规；明确空孔或预装件缺失判为不合规。',
                     '历史仅属于当前检测轮次；下一件工件必须开始新轮次，不继承上一件结果。',
                     '用于同工件、近似视角的原型测试；新工件和机位需要重新标定与补充训练。'])
    (out/'analysis.json').write_text(json.dumps(data,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    with (out/'segments.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer_csv=csv.writer(f);writer_csv.writerow(['位置','名称','视觉状态','模型分数','证据时间秒'])
        for target in targets: writer_csv.writerow([target['id'],target['name'],LABELS[target['state']],target['score'],target['lastSeen']])
    if registered_image is not None:
        cv2.imwrite(str(out/'evidence-1.jpg'),registered_image)
    rows=''.join(f'<tr><td>{html.escape(t["id"])}</td><td>{html.escape(t["name"])}</td><td>{LABELS[t["state"]]}</td><td>{html.escape(SEQUENCE_LABELS.get(t.get("sequenceState"), "—"))}</td><td>{t["score"] if t["score"] is not None else "—"}</td><td>{t["lastSeen"] if t["lastSeen"] is not None else "—"}</td></tr>' for t in targets)
    report=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>螺丝检测报告</title><style>body{{font:16px/1.7 system-ui;max-width:1000px;margin:40px auto;padding:20px;color:#162c38}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}img{{max-width:100%}}.note{{padding:18px;background:#fff4d8}}</style><h1>螺丝安装视觉检测报告</h1><h2>最终判定：{decision['label']}（逐位置视觉确认）</h2><p>{html.escape(decision['reason'])}</p><p>{html.escape(sequence['reason'] if sequence else '没有标定位置')}</p><p>{html.escape(data['source']['title'])}</p><p>人体检测：峰值 {summary['maxPeople']} 人 · {summary['trackCount']} 条轨迹 · 17点骨骼随视频回放显示</p><p>可见螺丝头 {counts['PRESENT']}/{len(targets)} · 可见空孔 {counts['EMPTY']} · 证据不足 {counts['UNKNOWN']} · 末次可定位时间 {assessment_time}s</p><p class="note">视觉原型，需复核。仅有正确视频，尚未验证真实漏打检出率；螺丝头可见不能证明扭矩合格。检测不要求安装先后顺序。</p><table><tr><th>位置</th><th>名称</th><th>视觉状态</th><th>位置确认</th><th>分数</th><th>证据时间</th></tr>{rows}</table><h2>检测依据</h2><ul>{''.join('<li>'+html.escape(v)+'</li>' for v in data['limitations'])}</ul><h2>末次可定位工件</h2><img src="evidence-1.jpg"><p>模型 SHA256: {model_hash}</p></html>'''
    (out/'report.html').write_text(report,encoding='utf-8')
    manifest=dict(schema_version=1,run_id=run_id,model_hash=model_hash,person_model_hash=person_config['sha256'],source_hash=data['source']['sha256'],
        options=options,verdict=decision['status'],files={p.name:file_hash(p) for p in out.iterdir() if p.is_file() and p.name!='manifest.json'})
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return data
