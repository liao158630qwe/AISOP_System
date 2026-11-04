"""Independent owner-isolated sticker cycles, using the same offline detector."""
from collections import deque
from io import BytesIO
import threading
import time
import cv2
import numpy as np
from PIL import Image
from fastapi import HTTPException
from algorithms.stickers import StickerDetector,snapshot,summarize,compliance,create_sequence
from algorithms.video import AnalysisError
from .streams import StreamRegistry
from algorithms.timing import StageTimer


class RealtimeStickers:
    def __init__(self, pose_detector=None):
        self.lock=threading.Lock();self.registry=StreamRegistry(ttl=300,maximum=16,per_owner=4)
        self.detector=None;self.signature=None
        if pose_detector is None:
            from .config import ROOT
            from .realtime import RealtimeDetector
            pose_detector=RealtimeDetector(ROOT/'models/yolo11s-pose.pt')
        self.pose_detector=pose_detector

    def predict(self,payload,*,stream_key,frame_time):
        timer=StageTimer()
        if not self.lock.acquire(blocking=False):raise HTTPException(429,'贴条检测繁忙，请稍后重试',headers={'Retry-After':'1'})
        try:
            try:
                with Image.open(BytesIO(payload)) as im:
                    if im.format!='JPEG' or max(im.width,im.height)>1920 or im.width*im.height>1920*1080:raise ValueError()
                image=cv2.imdecode(np.frombuffer(payload,np.uint8),cv2.IMREAD_COLOR)
                if image is None:raise ValueError()
            except Exception as exc:raise HTTPException(422,'需要长边不超过1920且像素不超过1920×1080的JPEG采样') from exc
            timer.mark('decode')
            try:frozen=snapshot()
            except AnalysisError as exc:raise HTTPException(503,str(exc)) from exc
            timer.mark('snapshot')
            signature=(frozen['config_hash'],tuple(frozen['assets'].values()))
            if self.detector is None or signature!=self.signature:
                self.detector=StickerDetector(frozen);self.signature=signature
            timer.mark('modelLoad')
            stream=self.registry.get(stream_key,frame_time,(signature,image.shape[:2]))
            with stream['lock']:
                if stream.get('finished'):raise HTTPException(409,'当前贴条轮次已结束，请为下一件开始新轮次')
                if 'frames' not in stream:
                    stream['frames']=deque(maxlen=120);stream['config']=frozen['config'];stream['sampleCount']=0
                    stream['sticker_sequence']=create_sequence(frozen['config'])
                started=time.perf_counter()
                timer.mark('stream')
                person_result=self.pose_detector.detect_people(image,stream['tracker'],frame_time)
                timer.mark('person')
                inspection=self.detector.inspect(image)
                timer.mark('inspection')
                process=stream['sticker_sequence'].update(frame_time,inspection);inspection['sequence']=process
                stream['frames'].append(dict(time=frame_time,inspection=inspection));stream['sampleCount']+=1
                targets,assessment=summarize(list(stream['frames']),stream['config'],stream['sticker_sequence'])
                decision=compliance(targets,process)
                timer.mark('rules')
                return dict(analysisType='sticker',detectionClasses=['sticker','person'],**person_result,
                    inspection=inspection,targets=inspection['targets'],aggregateTargets=targets,sequence=process,
                    inspectionMode=stream['config'].get('mode','appearance'),displayPolicy='current-frame-only',compliance=decision,final=False,
                    assessmentTime=assessment,frameTime=frame_time,processingMs=round((time.perf_counter()-started)*1000,1),
                    **timer.result(),
                    model=self.detector.config['model_name'],modelHash=frozen['assets'].get(frozen['config'].get('model')),
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
                targets,assessment=summarize(list(stream['frames']),stream['config'],stream['sticker_sequence'],final=True)
                sequence=stream['sticker_sequence'].snapshot()
                result=dict(final=True,targets=targets,sequence=sequence,compliance=compliance(targets,sequence,final=True),
                    assessmentTime=assessment,sampleCount=stream['sampleCount'])
                stream['finished']=True;stream['final_result']=result;stream['used']=time.monotonic()
                return result

    def release(self,key):self.registry.drop(key)
