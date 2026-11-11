"""Bounded specialist replicas with one shared registry of stream evidence.

Each slot owns its CUDA registration/YOLO state. Stream histories never belong to a
slot, so subsequent frames can use any available replica without losing order.
"""
import queue
import logging
import threading

from fastapi import HTTPException


class SpecialistPool:
    def __init__(self, factory, size):
        if type(size) is not int or not 1 <= size <= 6:
            raise ValueError('Specialist pool size must be 1–6')
        self.slots = [factory() for _ in range(size)]
        self.registry = self.slots[0].registry
        self.available = queue.LifoQueue(maxsize=size)
        self._preparation_lock = threading.Lock()
        for slot in self.slots:
            slot.registry = self.registry
            self.available.put_nowait(slot)

    def predict(self, payload, **kwargs):
        try:
            slot = self.available.get_nowait()
        except queue.Empty:
            raise HTTPException(429, '专项检测繁忙，请发送最新采样', headers={'Retry-After': '1'}) from None
        try:
            return slot.predict(payload, **kwargs)
        finally:
            self.available.put_nowait(slot)

    def finish(self, key):
        return self.slots[0].finish(key)

    def warmup(self):
        # Prepare the preferred idle replica, as predict() will use it next.
        # Reserving it keeps live requests on other available replicas; extra
        # copies remain lazy until concurrent cameras actually need them.
        with self._preparation_lock:
            try:
                slot = self.available.get_nowait()
            except queue.Empty:
                return False
            try:
                return slot.warmup()
            finally:
                self.available.put_nowait(slot)

    def release(self, key):
        self.registry.drop(key)

    def snapshot(self):
        return dict(capacity=len(self.slots), available=self.available.qsize(),
                    loaded=sum(slot.detector is not None for slot in self.slots))

    def close(self):
        close_specialist(self)


def close_specialist(service):
    """Stop every replica first, then wait for requests and close its workers."""
    slots = service.slots if isinstance(service, SpecialistPool) else [service]
    first_error = None
    for method in ('stop_accepting', 'close'):
        for slot in slots:
            try:
                getattr(slot, method)()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                logging.getLogger(__name__).exception('Specialist replica %s failed', method)
    if first_error is not None:
        raise first_error


def create_specialist(factory, size):
    # Preserve the existing single-instance interface and allocation by default.
    return factory() if size == 1 else SpecialistPool(factory, size)


def specialist_snapshot(service):
    if isinstance(service, SpecialistPool):
        return service.snapshot()
    return dict(capacity=1, available=int(not service.lock.locked()), loaded=int(service.detector is not None))
