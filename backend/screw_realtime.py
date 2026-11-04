"""Bounded, owner-isolated live screw cycles using the offline detector."""
from collections import deque
from io import BytesIO
import threading
import time

import cv2
import numpy as np
from PIL import Image
from fastapi import HTTPException

from algorithms.screws import (ScrewDetector, ScrewObservationHistory, compliance, current_display,
                               sequence_profiles, snapshot, create_history)
from .streams import StreamRegistry
from algorithms.timing import StageTimer


class RealtimeScrews:
    def __init__(self, pose_detector=None):
        self.lock=threading.Lock()
        self.registry=StreamRegistry(ttl=300,maximum=16,per_owner=4)
        self.detector=None
        self.signature=None
        if pose_detector is None:
            from .config import ROOT
            from .realtime import RealtimeDetector
            pose_detector = RealtimeDetector(ROOT/'models/yolo11s-pose.pt')
        self.pose_detector = pose_detector

    def predict(self,payload,*,stream_key,frame_time):
        timer=StageTimer()
        if not self.lock.acquire(blocking=False):
            raise HTTPException(429,'螺丝检测繁忙，请稍后重试',headers={'Retry-After':'1'})
        try:
            try:
                with Image.open(BytesIO(payload)) as image:
                    if image.format!='JPEG' or image.width>1920 or image.height>1080:
                        raise ValueError()
                frame=cv2.imdecode(np.frombuffer(payload,np.uint8),cv2.IMREAD_COLOR)
                if frame is None:raise ValueError()
            except Exception as exc:
                raise HTTPException(422,'需要不超过1920×1080的有效JPEG画面') from exc
            timer.mark('decode')
            frozen=snapshot()
            timer.mark('snapshot')
            detector_signature=(frozen['config_hash'],tuple(frozen['assets'].values()))
            if self.detector is None or detector_signature!=self.signature:
                self.detector=ScrewDetector(frozen);self.signature=detector_signature
            timer.mark('modelLoad')
            signature=(detector_signature,frame.shape[:2])
            stream=self.registry.get(stream_key,frame_time,signature)
            with stream['lock']:
                if stream.get('finished'):
                    raise HTTPException(409,'该检测轮次已结束，请重新开始检测')
                if 'frames' not in stream:
                    stream['frames']=deque(maxlen=120)
                    stream['config']=frozen['config']
                    stream['sampleCount']=0
                    stream['history']=create_history(frozen['config'])
                if stream['frames'] and frame_time-stream['frames'][-1]['time']>2:
                    stream['frames'].clear()
                started=time.perf_counter()
                timer.mark('stream')
                person_result=self.pose_detector.detect_people(frame,stream['tracker'],frame_time)
                timer.mark('person')
                inspection=current_display(self.detector.inspect(frame))
                timer.mark('inspection')
                targets=stream['history'].update(frame_time,inspection)
                sequence=stream['history'].sequence()
                inspection['sequence']=sequence
                stream['frames'].append({'time':frame_time,'inspection':inspection})
                stream['sampleCount']+=1
                decision=compliance(targets,sequence)
                timer.mark('rules')
                return {'analysisType':'screw','detectionClasses':['screw','person'],**person_result,
                        'inspection':inspection,'targets':inspection['targets'],'aggregateTargets':targets,
                        'historyTargets':targets,'displayPolicy':'current-frame-only',
                        'historyPolicy':'confirmed-state-retained','sequence':sequence,
                        'compliance':decision,'final':False,
                        'assessmentTime':stream['history'].assessment_time,'frameTime':frame_time,
                        'processingMs':round((time.perf_counter()-started)*1000,1),
                        **timer.result(),
                        'model':self.detector.config.get('model_name','YOLO11n 螺丝专项'),'device':str(self.detector.device),
                        'sampleCount':stream['sampleCount'],'width':frame.shape[1],'height':frame.shape[0]}
        finally:
            self.lock.release()

    def finish(self,key):
        with self.registry.lock:
            stream=self.registry.items.get(key)
            if stream is None or time.monotonic()-stream['used']>self.registry.ttl:
                self.registry.items.pop(key,None)
                raise HTTPException(404,'当前检测轮次不存在或已过期')
            with stream['lock']:
                if stream.get('final_result'):
                    return stream['final_result']
                if not stream.get('sampleCount'):raise HTTPException(409,'当前轮次没有检测采样')
                history=stream.get('history')
                if history is None:
                    frames=list(stream.get('frames',[]))
                    history=create_history(stream['config'])
                    for frame in frames:
                        history.update(frame['time'],frame['inspection'])
                targets,assessment=history.aggregate(final=True),history.assessment_time
                sequence=history.sequence(final=True)
                result={'final':True,'targets':targets,'sequence':sequence,
                        'compliance':compliance(targets,sequence,final=True),
                        'assessmentTime':assessment,'sampleCount':stream['sampleCount']}
                stream['finished']=True;stream['final_result']=result
                stream['used']=time.monotonic()
                return result

    def release(self,key):
        self.registry.drop(key)
