"""Bounded generic model cache and owner-isolated scene streams."""
from collections import OrderedDict
from io import BytesIO
import threading
import time

import cv2
import numpy as np
from PIL import Image
from fastapi import HTTPException

from algorithms.scene_runtime import SceneDetector, SceneEvidence
from algorithms.timing import StageTimer
from algorithms.video import AnalysisError
from .streams import StreamRegistry


class RealtimeScenes:
    def __init__(self, registry):
        self.scenes = registry
        self.lock = threading.Lock()
        self.streams = StreamRegistry(ttl=300, maximum=16, per_owner=4)
        self.models = OrderedDict()

    def predict(self, payload, *, scene_id, stream_key, frame_time, owner):
        timer = StageTimer()
        if not self.lock.acquire(blocking=False):
            raise HTTPException(429, '检测计算繁忙，请稍后重试', headers={'Retry-After':'1'})
        try:
            try:
                with Image.open(BytesIO(payload)) as image:
                    if image.format != 'JPEG' or max(image.size) > 1920 or image.width*image.height > 1920*1080:
                        raise ValueError()
                frame = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError()
            except Exception as exc:
                raise HTTPException(422, '需要长边不超过1920且像素不超过1920×1080的 JPEG') from exc
            timer.mark('decode')
            frozen = self.scenes.snapshot(scene_id, owner)
            timer.mark('snapshot')
            key = frozen['sha256']
            current = self.streams.items.get(stream_key)
            stamp = (key, frame.shape[:2])
            if current and current['signature'] != stamp:
                raise HTTPException(409, '任务版本或画面尺寸已改变，请重新开始本轮检测')
            if key not in self.models:
                # One generic model at a time bounds GPU memory alongside legacy services.
                self.models.clear()
                try:
                    self.models[key] = SceneDetector(frozen)
                except AnalysisError as exc:
                    raise HTTPException(503, str(exc)) from exc
            timer.mark('modelLoad')
            stream = self.streams.get(stream_key, frame_time, stamp)
            with stream['lock']:
                if stream.get('finished'):
                    raise HTTPException(409, '本轮检测已结束，请重新开始')
                stream.setdefault('evidence', SceneEvidence(frozen['config']))
                stream.setdefault('sampleCount', 0)
                inspection = self.models[key].inspect(frame)
                timer.mark('inference')
                result = stream['evidence'].update(frame_time, inspection)
                stream['sampleCount'] += 1
                timer.mark('rules')
                result.update(analysisType='scene', sceneId=scene_id, sceneName=frozen['config']['name'],
                    sceneRevision=frozen['revision'], frameTime=frame_time, sampleCount=stream['sampleCount'],
                    model=frozen['model'], width=frame.shape[1], height=frame.shape[0], **timer.result())
                result['processingMs'] = result['totalProcessingMs']
                stream['result'], stream['result_at'] = result, time.monotonic()
                return result
        finally:
            self.lock.release()

    def finish(self, key):
        with self.streams.lock:
            stream = self.streams.items.get(key)
            if not stream or 'result' not in stream or time.monotonic()-stream['used'] > self.streams.ttl:
                raise HTTPException(404, '当前检测轮次不存在或没有采样')
            with stream['lock']:
                result = dict(stream['result'], final=True)
                if time.monotonic()-stream['result_at'] > max(2., stream['evidence'].gap):
                    result.update(observations=[], checks=[], compliance=dict(status='REVIEW',label='待复核',reason='最后采样已过期'))
                stream['finished'] = True
                return result

    def release(self, key):
        self.streams.drop(key)
