"""Overlap same-frame people with inspection without letting tracker work escape."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

from fastapi import HTTPException


def record_frame_ready():
    """Called on the producing specialist lane after it decodes the frame."""
    import torch
    event = torch.cuda.Event()
    event.record()
    return event


class PersonWorker:
    """One worker per specialist instance; admission never queues another frame."""
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='station-person')
        self._condition = threading.Condition()
        self._busy = self._closed = self._close_complete = False

    @contextmanager
    def run(self, function):
        with self._condition:
            if self._closed:
                raise HTTPException(503, '人体检测执行器已关闭')
            if self._busy:
                raise HTTPException(429, '人体检测仍在执行，请发送最新采样', headers={'Retry-After': '1'})
            self._busy = True
        try:
            ready = record_frame_ready()
            def execute():
                # Only wait for this frame's producer event. The existing
                # detect_people method owns the person-model slot and CUDA lane.
                ready.synchronize()
                return function()
            pending = self.executor.submit(execute)
            try:
                yield pending
            except BaseException:
                # Preserve the inspection exception, but never release the
                # stream/model lease while the tracker can still be modified.
                try:
                    pending.result()
                except BaseException:
                    pass
                raise
            else:
                pending.result()
        finally:
            with self._condition:
                self._busy = False
                self._condition.notify_all()

    def close(self):
        with self._condition:
            if self._closed:
                self._condition.wait_for(lambda: self._close_complete)
                return
            self._closed = True
            self._condition.wait_for(lambda: not self._busy)
        try:
            self.executor.shutdown(wait=True)
        finally:
            with self._condition:
                self._close_complete = True
                self._condition.notify_all()
