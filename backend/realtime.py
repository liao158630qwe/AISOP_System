"""Bounded realtime inference pool with owner-isolated stream state."""
import os
import queue
import threading
import time
from collections import deque

from fastapi import HTTPException

BACKENDS = ("rtmo", "rtmpose", "yolo")


class _InferenceSlot:
    def __init__(self):
        self.model = None
        self.model_kind = None
        self.load_lock = threading.Lock()
        self.inspector = None
        self.lab = None


class RealtimeDetector:
    """单帧实时推理；使用有限模型槽位并在满载时立即返回 429。"""

    def __init__(self, model_path, backend=None, pool_size=None):
        self.model_path = model_path
        self.backend = (backend or os.getenv("REALTIME_BACKEND", "rtmo")).strip().lower() or "rtmo"
        if self.backend not in BACKENDS:
            raise ValueError(f"未知实时姿态后端 {self.backend}")
        configured = pool_size if pool_size is not None else int(os.getenv("REALTIME_POOL_SIZE", "2"))
        if not 1 <= configured <= 4:
            raise ValueError("实时推理池大小必须在 1–4 之间")
        self.pool_size = configured
        self._slots = [_InferenceSlot() for _ in range(configured)]
        self._available = queue.Queue(maxsize=configured)
        for slot in self._slots:
            self._available.put_nowait(slot)
        # Compatibility probe for callers/tests that force a busy response.
        # It is not held during inference and therefore does not serialize work.
        self.lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._latencies = deque(maxlen=512)
        self._queue_waits = deque(maxlen=512)
        self._completed = self._failed = 0
        self._started_at = time.monotonic()
        self._peak_gpu_memory = 0.0
        from .streams import StreamRegistry
        self.streams = StreamRegistry()

    @property
    def model(self):
        return self._slots[0].model

    @model.setter
    def model(self, value):
        self._slots[0].model = value

    @property
    def model_kind(self):
        return self._slots[0].model_kind

    @model_kind.setter
    def model_kind(self, value):
        self._slots[0].model_kind = value

    @property
    def inspector(self):
        return self._slots[0].inspector

    @inspector.setter
    def inspector(self, value):
        self._slots[0].inspector = value

    @property
    def lab(self):
        return self._slots[0].lab

    @lab.setter
    def lab(self, value):
        self._slots[0].lab = value

    def release(self, key):
        self.streams.drop(key)

    def _ensure_slot_model(self, slot):
        """Load one model copy lazily; other slots can load in parallel."""
        if slot.model is not None:
            return
        with slot.load_lock:
            if slot.model is not None:
                return
            failure = None
            if self.backend in ("rtmo", "rtmpose"):
                try:
                    from algorithms.rtmlib_pose import RTMLibDetector
                    slot.model = RTMLibDetector(variant=self.backend, mode="balanced")
                    slot.model_kind = "rtmlib"
                    return
                except Exception as exc:
                    failure = exc
            if not self.model_path.is_file():
                raise HTTPException(503, f"实时模型不可用（{self.backend}: {failure}）" if failure
                                    else "服务器模型文件尚未配置")
            from ultralytics import YOLO
            slot.model = YOLO(str(self.model_path))
            slot.model_kind = "yolo"

    def _ensure_model(self):
        self._ensure_slot_model(self._slots[0])

    def device(self, slot=None):
        slot = slot or self._slots[0]
        if slot.model_kind == "rtmlib":
            return slot.model._device
        import torch
        return 0 if torch.cuda.is_available() else "cpu"

    def model_label(self, slot=None):
        slot = slot or self._slots[0]
        return slot.model.label if slot.model_kind == "rtmlib" else self.model_path.stem

    def warmup(self):
        """Warm the primary slot; additional slots load on first use."""
        try:
            import numpy as np
            self._ensure_model()
            frame = np.zeros((360, 640, 3), dtype=np.uint8)
            device = self.device()
            for _ in range(2):
                self.model.predict(frame, imgsz=640, conf=.35, iou=.5, max_det=50,
                                    device=device, verbose=False)
            return True
        except Exception:
            return False

    def _record(self, latency_ms, queue_wait_ms, failed=False):
        with self._metrics_lock:
            self._latencies.append(float(latency_ms))
            self._queue_waits.append(float(queue_wait_ms))
            self._completed += 0 if failed else 1
            self._failed += 1 if failed else 0

    @staticmethod
    def _percentile(values, percentile):
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
        return round(ordered[index], 2)

    def metrics_snapshot(self):
        with self._metrics_lock:
            latencies, waits = list(self._latencies), list(self._queue_waits)
            completed, failed = self._completed, self._failed
        return {
            "poolSize": self.pool_size,
            "active": self.pool_size - self._available.qsize(),
            "completed": completed,
            "failed": failed,
            "throughputPerMinute": round(completed / max(1 / 60, (time.monotonic() - self._started_at) / 60), 2),
            "inferenceP95Ms": self._percentile(latencies, .95),
            "queueWaitP95Ms": self._percentile(waits, .95),
            "peakGpuMemoryMB": round(self._peak_gpu_memory, 1) if self._peak_gpu_memory else None,
        }

    def predict(self, payload, inspection_config=None, test_algorithms=None, *, stream_key=None,
                frame_time=None, workflow=None, sequential=True):
        if not self.lock.acquire(blocking=False):
            raise HTTPException(429, "实时检测繁忙，请稍后重试", headers={"Retry-After": "1"})
        self.lock.release()
        request_started = time.perf_counter()
        slot = None
        queue_started = time.perf_counter()
        try:
            try:
                slot = self._available.get_nowait()
            except queue.Empty:
                raise HTTPException(429, "实时检测繁忙，请稍后重试", headers={"Retry-After": "1"})
            queue_wait_ms = (time.perf_counter() - queue_started) * 1000
            import cv2
            import numpy as np
            from PIL import Image
            from io import BytesIO
            try:
                with Image.open(BytesIO(payload)) as header:
                    if header.format != "JPEG" or not (1 <= header.width <= 1280 and 1 <= header.height <= 720):
                        raise ValueError()
                frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError()
            except Exception as exc:
                raise HTTPException(422, "需要不超过1280×720的有效JPEG画面") from exc
            if test_algorithms and sequential and stream_key:
                from algorithms.action_sop import freeze
                from algorithms.lab import NAMES
                workflow = freeze({'id': 'algorithm-test', 'name': '算法测试顺序', 'version': '2.0',
                    'steps': [{'id': name, 'name': NAMES[name], 'actions': ['raised' if name == 'raise' else name],
                               'min_hold': .5, 'max_wait': 60} for name in test_algorithms]})
            self._ensure_slot_model(slot)
            if stream_key:
                signature = (frame.shape[:2], inspection_config['sha256'] if inspection_config else None,
                             tuple(test_algorithms or []), workflow['sha256'] if workflow else None, sequential)
                stream = self.streams.get(stream_key, frame_time, signature, workflow)
            else:
                stream = None
            started = time.perf_counter()
            from algorithms.tracking import PoseTracker
            device = self.device(slot)
            result = slot.model.predict(frame, imgsz=640, conf=.35, iou=.5,
                                        max_det=50, device=device, verbose=False)[0]
            h, w = frame.shape[:2]
            if stream:
                with stream['lock']:
                    people = stream['tracker'].update(result, w, h, frame_time)
            else:
                people = PoseTracker().update(result, w, h, 0)
            inspection = None
            lab = None
            if test_algorithms:
                from algorithms.lab import AlgorithmLab
                if slot.lab is None:
                    slot.lab = AlgorithmLab(self.model_path.parent)
                lab = slot.lab.run(frame, people, test_algorithms)
            if inspection_config:
                from algorithms.inspection import Inspector
                if slot.inspector is None or slot.inspector.frozen['sha256'] != inspection_config['sha256']:
                    slot.inspector = Inspector(inspection_config)
                inspection = slot.inspector.inspect(frame)
            if stream and stream['sequence']:
                with stream['lock']:
                    action_sequence = stream['sequence'].update(people, frame_time)
            else:
                action_sequence = None
            processing_ms = (time.perf_counter() - started) * 1000
            gpu_memory = None
            try:
                import torch
                if torch.cuda.is_available():
                    gpu_memory = round(torch.cuda.memory_allocated() / 1048576, 1)
            except Exception:
                pass
            with self._metrics_lock:
                if gpu_memory is not None:
                    self._peak_gpu_memory = max(self._peak_gpu_memory, gpu_memory)
            self._record(processing_ms, queue_wait_ms)
            return {"people": people, "width": w, "height": h, "verdict": "REVIEW",
                    "inspection": inspection, "lab": lab, "actionSequence": action_sequence,
                    "processingMs": round(processing_ms, 1), "device": str(device),
                    "model": self.model_label(slot), "backend": slot.model_kind,
                    "gpuMemoryMB": gpu_memory, "tracking": "stream-scoped" if stream else "frame-local",
                    "frameTime": frame_time, "streamEpoch": stream['created'] if stream else None}
        except ImportError as exc:
            self._record((time.perf_counter() - request_started) * 1000, 0, True)
            raise HTTPException(503, "实时推理依赖未安装，请安装requirements-worker.txt及PyTorch") from exc
        except Exception:
            self._record((time.perf_counter() - request_started) * 1000, 0, True)
            raise
        finally:
            if slot is not None:
                self._available.put_nowait(slot)
