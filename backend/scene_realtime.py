"""Bounded concurrent scene models and owner-isolated evidence streams."""
from collections import OrderedDict
from io import BytesIO
import math
import logging
import threading
import time

import cv2
import numpy as np
from PIL import Image
from fastapi import HTTPException

from algorithms.scene_runtime import SceneDetector, SceneEvidence
from algorithms.timing import StageTimer
from algorithms.video import AnalysisError
from .scene_model_cache import ModelEntry, model_key, run_model_lane
from .streams import StreamRegistry


def load_model(frozen):
    from algorithms.scene_runtime import verify
    from ultralytics import YOLO
    verify(frozen)
    return YOLO(frozen['model_path'])


class RealtimeScenes:
    model_key = staticmethod(model_key)

    def __init__(self, registry, model_cache_size=1, concurrency=1):
        if type(model_cache_size) is not int or not 1 <= model_cache_size <= 6:
            raise ValueError('Scene model cache size must be 1–6')
        if type(concurrency) is not int or not 1 <= concurrency <= 6:
            raise ValueError('Scene concurrency must be 1–6')
        self.scenes = registry
        self.model_cache_size, self.concurrency = model_cache_size, concurrency
        self.lock = threading.RLock()
        self._condition = threading.Condition(self.lock)
        self.streams = StreamRegistry()
        self.models = OrderedDict()
        self._entries = set()
        self._active_streams = {}
        self._active = 0
        self._closed = self._close_complete = False
        self.model_loads = self.cache_hits = self.evictions = self.busy_rejections = 0

    def cache_metrics(self):
        with self.lock:
            return dict(residentModels=len(self.models), capacity=self.model_cache_size,
                        modelLoads=self.model_loads, cacheHits=self.cache_hits, evictions=self.evictions,
                        concurrency=self.concurrency, activeModels=self._active,
                        loadingModels=sum(entry.loading for entry in self.models.values()),
                        busyRejections=self.busy_rejections)

    def _busy(self):
        self.busy_rejections += 1
        raise HTTPException(429, '检测计算繁忙，请发送最新采样', headers={'Retry-After': '1'})

    def _begin_stream(self, key):
        with self._condition:
            if self._closed:
                raise HTTPException(503, '检测服务已关闭')
            if key in self._active_streams:
                self._busy()
            token = {'cancelled': False}
            self._active_streams[key] = token
            return token

    def _end_stream(self, key):
        with self._condition:
            self._active_streams.pop(key, None)
            self._condition.notify_all()

    @staticmethod
    def _check_cancelled(token):
        if token['cancelled']:
            raise HTTPException(409, '本轮检测已释放，请重新开始')

    def _reserve_model(self, key):
        retired = None
        with self._condition:
            if self._closed:
                raise HTTPException(503, '检测服务已关闭')
            entry = self.models.get(key)
            if self._active >= self.concurrency or (entry is not None and entry.busy):
                self._busy()
            if entry is None:
                if len(self.models) >= self.model_cache_size:
                    idle = next((k for k, value in self.models.items()
                                 if not value.busy and not value.loading), None)
                    if idle is None:
                        self._busy()
                    retired = self.models.pop(idle)
                    self.evictions += 1
                entry = ModelEntry(key)
                self.models[key] = entry
                self._entries.add(entry)
            entry.busy = True
            self._active += 1
            self.models.move_to_end(key)
        if retired is not None:
            try:
                self._dispose(retired)
            except BaseException:
                # The caller has not received this new lease yet. Roll it back
                # here so a retirement error cannot strand its busy/active slot.
                try:
                    self._release_model(entry, failed=True)
                except BaseException:
                    logging.getLogger(__name__).exception('Scene reservation rollback failed')
                raise
        return entry

    def _dispose(self, entry):
        try:
            entry.close()
        finally:
            with self._condition:
                self._entries.discard(entry)

    def _release_model(self, entry, failed=False):
        with self._condition:
            retire = failed or entry.model is None or self.models.get(entry.key) is not entry
        try:
            if retire:
                # Keep the lease until cleanup completes, so a failed model cannot
                # be reloaded while its previous allocation is still being retired.
                self._dispose(entry)
        finally:
            with self._condition:
                if retire and self.models.get(entry.key) is entry:
                    self.models.pop(entry.key)
                entry.busy = False
                self._active -= 1
                self._condition.notify_all()

    def _invalidate_failed_frame(self, key, timestamp):
        # Replayed/invalid timestamps and completed rounds cannot erase valid results.
        with self.streams.lock:
            stream = self.streams.items.get(key)
        if stream is None or timestamp is None or not math.isfinite(timestamp) or timestamp <= stream['timestamp']:
            return
        with stream['lock']:
            if not stream.get('finished'):
                for field in ('evidence', 'result', 'result_at'):
                    stream.pop(field, None)

    def _validate_stream(self, key, timestamp, signature):
        # Read-only preflight: busy models must not advance accepted frame time.
        # StreamRegistry.get performs the authoritative capacity check on admission.
        if timestamp is None or not math.isfinite(timestamp) or timestamp < 0:
            raise HTTPException(422, '采样时间戳必须有限且非负')
        with self.streams.lock:
            stream = self.streams.items.get(key)
            if stream is None:
                return
            if time.monotonic() - stream['used'] >= self.streams.ttl:
                raise HTTPException(409, '检测状态已过期，请重新开始本轮检测')
            if timestamp <= stream['timestamp']:
                raise HTTPException(409, '采样时间戳已过期，请重新开始检测')
            if signature != stream['signature']:
                raise HTTPException(409, '模型或画面配置已改变，请重新开始本轮检测')
            if stream.get('finished'):
                raise HTTPException(409, '本轮检测已结束，请重新开始')

    @staticmethod
    def _clear_failed_sample(stream, frozen):
        stream['evidence'] = SceneEvidence(frozen['config'])
        stream.pop('result', None)
        stream.pop('result_at', None)

    def predict(self, payload, *, scene_id, stream_key, frame_time, owner):
        timer = StageTimer()
        token = self._begin_stream(stream_key)
        entry, failed, request_failed = None, False, False
        try:
            try:
                with Image.open(BytesIO(payload)) as image:
                    if image.format != 'JPEG' or max(image.size) > 1920 or image.width*image.height > 1920*1080:
                        raise ValueError()
                frame = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError()
            except Exception as exc:
                self._invalidate_failed_frame(stream_key, frame_time)
                raise HTTPException(422, '需要长边不超过1920且像素不超过1920×1080的 JPEG') from exc
            timer.mark('decode')
            try:
                frozen = self.scenes.snapshot(scene_id, owner)
            except Exception:
                self._invalidate_failed_frame(stream_key, frame_time)
                raise
            timer.mark('snapshot')
            key = model_key(frozen, owner)
            stamp = (frozen['sha256'], frame.shape[:2])
            try:
                self._validate_stream(stream_key, frame_time, stamp)
            except HTTPException as exc:
                if exc.status_code == 409:
                    self._invalidate_failed_frame(stream_key, frame_time)
                raise
            entry = self._reserve_model(key)
            with self._condition:
                self._check_cancelled(token)
                stream = self.streams.get(stream_key, frame_time, stamp)
            with stream['lock']:
                if entry.model is None:
                    try:
                        entry.model = run_model_lane(entry, lambda: load_model(frozen))
                        with self.lock:
                            entry.loading = False
                            self.model_loads += 1
                    except Exception as exc:
                        failed = True
                        self._clear_failed_sample(stream, frozen)
                        detail = str(exc) if isinstance(exc, AnalysisError) else '模型无法加载，请检查权重与服务器依赖'
                        raise HTTPException(503, detail) from exc
                else:
                    with self.lock:
                        self.cache_hits += 1
                timer.mark('modelLoad')
                with self._condition:
                    self._check_cancelled(token)
                if 'evidence' not in stream:
                    stream['evidence'] = SceneEvidence(frozen['config'])
                stream.setdefault('sampleCount', 0)
                try:
                    # Only weights/predictor are cached. Per-scene config is fresh.
                    inspection = run_model_lane(entry, lambda: SceneDetector(frozen, model=entry.model).inspect(frame))
                    timer.mark('inference')
                except Exception:
                    failed = True
                    self._clear_failed_sample(stream, frozen)
                    raise
                try:
                    result = stream['evidence'].update(frame_time, inspection)
                except Exception:
                    self._clear_failed_sample(stream, frozen)
                    raise
                with self._condition:
                    self._check_cancelled(token)
                    stream['sampleCount'] += 1
                    timer.mark('rules')
                    result.update(analysisType='scene', sceneId=scene_id, sceneName=frozen['config']['name'],
                        sceneRevision=frozen['revision'], frameTime=frame_time, sampleCount=stream['sampleCount'],
                        model=frozen['model'], width=frame.shape[1], height=frame.shape[0], **timer.result())
                    result['processingMs'] = result['totalProcessingMs']
                    stream['result'], stream['result_at'] = result, time.monotonic()
                    return result
        except BaseException:
            request_failed = True
            raise
        finally:
            try:
                if entry is not None:
                    try:
                        self._release_model(entry, failed)
                    except BaseException:
                        if not request_failed:
                            raise
                        logging.getLogger(__name__).exception('Scene model cleanup failed after request error')
            finally:
                self._end_stream(stream_key)

    def finish(self, key):
        self._begin_stream(key)
        try:
            with self.streams.lock:
                stream = self.streams.items.get(key)
            if not stream or time.monotonic()-stream['used'] > self.streams.ttl:
                raise HTTPException(404, '当前检测轮次不存在或没有采样')
            with stream['lock'], self._condition:
                self._check_cancelled(self._active_streams[key])
                if 'result' not in stream:
                    raise HTTPException(404, '当前检测轮次没有有效采样，请重新采样')
                if stream.get('final_result'):
                    return stream['final_result']
                result = dict(stream['result'], final=True)
                if time.monotonic()-stream['result_at'] > max(2., stream['evidence'].gap):
                    result.update(observations=[], checks=[], compliance=dict(status='REVIEW',label='待复核',reason='最后采样已过期'))
                stream['finished'] = True
                stream['final_result'] = result
                return result
        finally:
            self._end_stream(key)

    def release(self, key):
        with self._condition:
            token = self._active_streams.get(key)
            if token is not None:
                token['cancelled'] = True
            self.streams.drop(key)

    def close(self):
        with self._condition:
            if self._closed:
                self._condition.wait_for(lambda: self._close_complete)
                return
            self._closed = True
            self._condition.wait_for(lambda: not self._active_streams)
            entries = list(self._entries)
            self.models.clear()
        first_error = None
        try:
            for entry in entries:
                try:
                    self._dispose(entry)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    logging.getLogger(__name__).exception('Scene entry cleanup failed')
        finally:
            with self._condition:
                self._close_complete = True
                self._condition.notify_all()
        if first_error is not None:
            raise first_error
