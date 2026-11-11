"""Independent owner-isolated sticker cycles, using the same offline detector."""
from collections import deque
from io import BytesIO
import threading
import time
import cv2
import numpy as np
from PIL import Image
from fastapi import HTTPException
from algorithms.stickers import StickerDetector,snapshot,summarize,compliance,create_sequence,create_history
from algorithms.video import AnalysisError
from .streams import StreamRegistry
from .person_overlap import PersonWorker
from .specialist_lifecycle import SpecialistLifecycle, specialist_request
from algorithms.timing import StageTimer
from algorithms.cuda_lane import cuda_request


class RealtimeStickers:
    def __init__(self, pose_detector=None):
        self.lock=threading.Lock();self.registry=StreamRegistry()
        self.detector=None;self.signature=None
        self._preparation_lock=threading.Lock()
        self._prepared_signature=None
        self._owns_pose_detector = pose_detector is None
        if pose_detector is None:
            from .config import ROOT
            from .realtime import RealtimeDetector
            pose_detector=RealtimeDetector(ROOT/'models/yolo11s-pose.pt')
        self.pose_detector=pose_detector
        self._person_worker=PersonWorker()
        self._lifecycle=SpecialistLifecycle()

    def _ensure_detector(self, frozen):
        signature=(frozen['config_hash'],tuple(frozen['assets'].values()))
        if self.detector is None or signature!=self.signature:
            self.detector=StickerDetector(frozen);self.signature=signature
            self._prepared_signature=None
        return signature

    @specialist_request
    def warmup(self):
        """Prepare the real CUDA lanes without creating a detection cycle."""
        with self._preparation_lock:
            frozen=snapshot()
            signature=(frozen['config_hash'],tuple(frozen['assets'].values()))
            if self.detector is not None and self.signature==self._prepared_signature==signature:
                return True
            from .config import ROOT
            reference=cv2.imread(str(ROOT/frozen['config']['reference']))
            if reference is None:
                raise AnalysisError('STICKER_MODEL_MISSING','贴条参考图无法读取')
            # Match browser/terminal sampling limits, including portrait cameras.
            height,width=reference.shape[:2]
            scale=min(1.,1920/max(width,height),(1920*1080/(width*height))**.5)
            if scale<1:
                reference=cv2.resize(reference,(max(1,int(width*scale)),max(1,int(height*scale))))
            if not self.pose_detector.warmup(reference):
                return False
            ok,encoded=cv2.imencode('.jpg',reference)
            if not ok:
                raise AnalysisError('STICKER_MODEL_MISSING','贴条参考图无法编码')
            try:
                return self._warmup_lane(frozen,encoded.tobytes())
            except HTTPException as exc:
                if exc.status_code==429:
                    return False
                raise

    @cuda_request
    def _warmup_lane(self, frozen, payload):
        with self.lock:
            from algorithms.cuda_vision import decode
            image=decode(payload)
            signature=self._ensure_detector(frozen)
            # Exercise registration, local features and all eight classifiers.
            # No tracker/sequence/registry/evidence is updated by these frames.
            for _ in range(2):
                self.detector.inspect(image)
            self._cuda_stream.synchronize()
            self._prepared_signature=signature
            return True

    @specialist_request
    @cuda_request
    def predict(self,payload,*,stream_key,frame_time):
        timer=StageTimer()
        if not self.lock.acquire(blocking=False):raise HTTPException(429,'贴条检测繁忙，请稍后重试',headers={'Retry-After':'1'})
        try:
            try:
                with Image.open(BytesIO(payload)) as im:
                    if im.format!='JPEG' or max(im.width,im.height)>1920 or im.width*im.height>1920*1080:raise ValueError()
                from algorithms.cuda_vision import decode
                image=decode(payload)
                if image is None:raise ValueError()
            except Exception as exc:raise HTTPException(422,'需要长边不超过1920且像素不超过1920×1080的JPEG采样') from exc
            timer.mark('decode')
            try:frozen=snapshot()
            except AnalysisError as exc:raise HTTPException(503,str(exc)) from exc
            timer.mark('snapshot')
            signature=self._ensure_detector(frozen)
            timer.mark('modelLoad')
            stream=self.registry.get(stream_key,frame_time,(signature,image.shape[:2]))
            with stream['lock']:
                if stream.get('finished'):raise HTTPException(409,'当前贴条轮次已结束，请为下一件开始新轮次')
                if 'frames' not in stream:
                    stream['frames']=deque(maxlen=120);stream['config']=frozen['config'];stream['sampleCount']=0
                    stream['sticker_sequence']=create_sequence(frozen['config'])
                    # The bounded frame cache is not the cycle's evidence history.
                    # Losing registration must not erase earlier confirmations.
                    stream['sticker_history']=create_history(frozen['config'])
                started=time.perf_counter()
                timer.mark('stream')
                with self._person_worker.run(lambda: self.pose_detector.detect_people(
                        image,stream['tracker'],frame_time)) as person:
                    inspection=self.detector.inspect(image)
                    timer.mark('inspection')
                    person_result=person.result()
                    # Remaining wait only; personProcessingMs is the full branch.
                    timer.mark('person')
                process=stream['sticker_sequence'].update(frame_time,inspection);inspection['sequence']=process
                history=stream['sticker_history']
                if history is not None:history.update(frame_time,inspection)
                stream['frames'].append(dict(time=frame_time,inspection=inspection));stream['sampleCount']+=1
                targets,assessment=summarize(stream['frames'],stream['config'],stream['sticker_sequence'],history=history)
                decision=compliance(targets,process)
                timer.mark('rules')
                return dict(analysisType='sticker',detectionClasses=['sticker','person'],**person_result,
                    inspection=inspection,targets=inspection['targets'],aggregateTargets=targets,sequence=process,
                    inspectionMode=stream['config'].get('mode','appearance'),displayPolicy='current-frame-only',compliance=decision,final=False,
                    assessmentTime=assessment,frameTime=frame_time,processingMs=round((time.perf_counter()-started)*1000,1),
                    **timer.result(),
                    model=self.detector.config['model_name'],modelHash=frozen['assets'].get(frozen['config'].get('model')),
                    visionBackend='cuda-disk-v1' if frozen['config'].get('mode')=='action' else 'legacy-opencv',
                    configHash=frozen['config_hash'],sampleCount=stream['sampleCount'],width=image.shape[1],height=image.shape[0])
        finally:self.lock.release()

    def finish(self,key):
        with self.registry.lock:
            stream=self.registry.items.get(key)
            if stream is None or time.monotonic()-stream['used']>self.registry.ttl:
                self.registry.items.pop(key,None);raise HTTPException(404,'当前贴条轮次不存在或已过期')
            with stream['lock']:
                if stream.get('final_result'):return stream['final_result']
                if not stream.get('frames'):raise HTTPException(409,'当前贴条轮次没有检测采样')
                targets,assessment=summarize(stream['frames'],stream['config'],stream['sticker_sequence'],
                    final=True,history=stream['sticker_history'])
                sequence=stream['sticker_sequence'].snapshot()
                result=dict(final=True,targets=targets,sequence=sequence,compliance=compliance(targets,sequence,final=True),
                    assessmentTime=assessment,sampleCount=stream['sampleCount'])
                stream['finished']=True;stream['final_result']=result;stream['used']=time.monotonic()
                return result

    def release(self,key):self.registry.drop(key)

    def close(self):
        # Shared pose pools are closed by their application owner.
        self._lifecycle.close(self)

    def stop_accepting(self):self._lifecycle.stop_accepting()
