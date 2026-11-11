"""Drain real specialist requests before shutting down their worker threads."""
from contextlib import contextmanager
from functools import wraps
import logging
import threading

from fastapi import HTTPException


class SpecialistLifecycle:
    def __init__(self):
        self._condition = threading.Condition()
        self._accepting = True
        self._active = 0
        self._closing = self._closed = False

    @contextmanager
    def request(self):
        with self._condition:
            if not self._accepting:
                raise HTTPException(503, '专项检测服务正在停止')
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def stop_accepting(self):
        with self._condition:
            self._accepting = False

    def close(self, owner):
        with self._condition:
            if self._closing:
                self._condition.wait_for(lambda: self._closed)
                return
            self._accepting = False
            self._closing = True
            self._condition.wait_for(lambda: self._active == 0)
        first_error = None
        def attempt(function):
            nonlocal first_error
            try:
                function()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                logging.getLogger(__name__).exception('Specialist worker cleanup failed')
        try:
            executor = getattr(owner, '_cuda_executor', None)
            drained = False
            def drain():
                nonlocal drained
                try:
                    stream = getattr(owner, '_cuda_stream', None)
                    if stream is not None:
                        stream.synchronize()
                finally:
                    owner.detector = None
                    owner._cuda_stream = None
                    drained = True
            if executor is not None:
                attempt(lambda: executor.submit(drain).result())
                attempt(lambda: executor.shutdown(wait=True))
            if not drained:
                # Also release a model whose executor had already stopped and
                # rejected submit; real requests have been drained above.
                attempt(drain)
            attempt(owner._person_worker.close)
            if getattr(owner, '_owns_pose_detector', False):
                attempt(owner.pose_detector.close)
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()
        if first_error is not None:
            raise first_error


def specialist_request(function):
    @wraps(function)
    def request(self, *args, **kwargs):
        with self._lifecycle.request():
            return function(self, *args, **kwargs)
    return request
