"""CPU-only lifetime checks for direct pose/person calls and startup warmup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import HTTPException

from backend.realtime import RealtimeDetector


def wait_until(predicate, timeout=3):
    deadline = time.perf_counter() + timeout
    while not predicate():
        assert time.perf_counter() < deadline, 'Timed out waiting for CPU test worker'
        time.sleep(.005)


@pytest.fixture
def runtime(monkeypatch):
    import torch
    import algorithms.cuda_lane as lane
    import algorithms.cuda_vision as vision
    import algorithms.cuda_yolo as yolo
    import algorithms.person_detection as person
    import algorithms.tracking as tracking

    calls, hooks, services = [], {}, []
    class Stream:
        def wait_stream(self, other): pass
        def wait_event(self, event): pass
        def synchronize(self):
            calls.append(('synchronize', threading.get_ident()))
    class Event:
        def record(self): pass
    monkeypatch.setattr(lane, 'require_cuda', lambda: 0)
    monkeypatch.setattr(torch.cuda, 'Stream', lambda **kwargs: Stream())
    monkeypatch.setattr(torch.cuda, 'Event', Event)
    monkeypatch.setattr(torch.cuda, 'stream', lambda stream: nullcontext())
    monkeypatch.setattr(torch.cuda, 'default_stream', lambda *args: None)
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    image = np.zeros((48, 64, 3), dtype=np.uint8)
    _, encoded = cv2.imencode('.jpg', image)
    monkeypatch.setattr(vision, 'decode', lambda payload: image)
    monkeypatch.setattr(tracking.PoseTracker, 'update', lambda *args, **kwargs: [])

    def work():
        calls.append(('work', threading.get_ident()))
        if 'work' in hooks:
            hooks['work']()
    class Model:
        def predict(self, *args, **kwargs):
            work()
            return [SimpleNamespace()]
    def pose(*args, **kwargs):
        work()
        return [SimpleNamespace()]
    def people(*args, **kwargs):
        work()
        return []
    monkeypatch.setattr(yolo, 'predict', pose)
    monkeypatch.setattr(person, 'predict_people', people)

    def create(size=1):
        service = RealtimeDetector(Path('cpu-test-model.pt'), backend='yolo', pool_size=size)
        def ensure(slot):
            if slot.model is None:
                slot.model, slot.model_kind = Model(), 'yolo'
        monkeypatch.setattr(service, '_ensure_slot_model', ensure)
        monkeypatch.setattr(service, 'device', lambda slot=None: 0)
        services.append(service)
        def invoke(kind):
            if kind == 'pose':
                return service.predict(encoded.tobytes())
            if kind == 'people':
                return service.detect_people(image, object(), 0)
            return service.warmup()
        return SimpleNamespace(service=service, invoke=invoke, calls=calls, hooks=hooks)
    yield create
    for service in services:
        service.close()


@pytest.mark.parametrize('kind', ['pose', 'people', 'warmup'])
def test_close_waits_for_direct_requests_and_rejects_new_entrypoints(runtime, kind):
    task = runtime()
    entered, proceed = threading.Event(), threading.Event()
    def block():
        entered.set()
        assert proceed.wait(5)
    task.hooks['work'] = block
    with ThreadPoolExecutor(max_workers=3) as pool:
        running = pool.submit(task.invoke, kind)
        try:
            assert entered.wait(3)
            assert task.service._active_requests == 1
            closing = pool.submit(task.service.close)
            wait_until(lambda: not task.service._accepting)
            assert not closing.done()
            assert task.service.model is not None
            for entrypoint in ('pose', 'people'):
                with pytest.raises(HTTPException) as stopped:
                    task.invoke(entrypoint)
                assert stopped.value.status_code == 503
            assert task.invoke('warmup') is False
            second_close = pool.submit(task.service.close)
            assert not second_close.done()
        finally:
            proceed.set()
        assert running.result(3)
        closing.result(3)
        second_close.result(3)
    assert task.service._closed and task.service._active_requests == 0
    assert task.service.model is None
    assert task.service._available.qsize() == task.service.pool_size
    for slot in task.service._slots:
        executor = getattr(slot, '_cuda_executor', None)
        if executor is not None:
            assert all(not thread.is_alive() for thread in executor._threads)
    worker_ids = {ident for name, ident in task.calls if name == 'work'}
    cleanup_ids = {ident for name, ident in task.calls if name == 'synchronize'}
    assert worker_ids == cleanup_ids
    task.service.close()


@pytest.mark.parametrize('kind', ['pose', 'people', 'warmup'])
def test_request_errors_release_lifecycle_and_pool_slot(runtime, kind):
    task = runtime()
    def fail(): raise ValueError('fake inference error')
    task.hooks['work'] = fail
    if kind == 'warmup':
        assert task.invoke(kind) is False
    else:
        with pytest.raises(ValueError, match='fake inference error'):
            task.invoke(kind)
    assert task.service._active_requests == 0
    assert task.service._available.qsize() == task.service.pool_size
    task.hooks.clear()
    assert task.invoke(kind)


def test_warmup_reuses_real_person_lane_without_stream_evidence(runtime, monkeypatch):
    import algorithms.person_detection as person
    task = runtime(size=2)
    observed = []
    predict = person.predict_people
    def record(model, frame, tracker, timestamp):
        observed.append((model, frame.shape, tracker, threading.get_ident()))
        return predict(model, frame, tracker, timestamp)
    monkeypatch.setattr(person, 'predict_people', record)
    portrait = np.zeros((1920, 1080, 3), dtype=np.uint8)
    assert task.service.warmup(portrait)
    assert [row[1] for row in observed] == [portrait.shape, portrait.shape]
    assert all(row[0] is task.service.model for row in observed)
    assert observed[0][2] is observed[1][2]
    assert all(row[3] != threading.get_ident() for row in observed)
    assert task.service.streams.items == {}
    assert task.service.metrics_snapshot()['completed'] == 0
    assert task.service._slots[1].model is None
    for _ in range(3):
        assert task.invoke('people')
    assert task.service._slots[1].model is None, 'Single-stream use must retain the warmed replica'
    assert len({row[0] for row in observed}) == 1
    assert len({row[3] for row in observed}) == 1, 'Warmup must run on the actual inference lane'
    assert all(row[2] is not observed[0][2] for row in observed[2:])


def test_warmup_is_cached_per_model_and_input_shape(runtime):
    task = runtime()
    landscape = np.zeros((1080, 1920, 3), dtype=np.uint8)
    portrait = np.zeros((1920, 1080, 3), dtype=np.uint8)
    assert task.service.warmup()
    assert len(task.calls) == 2
    assert task.service.warmup(landscape)
    assert len(task.calls) == 2
    assert task.service.warmup(portrait)
    assert len(task.calls) == 4
    assert task.service.warmup(portrait.copy())
    assert len(task.calls) == 4
    task.service.model = None
    assert task.service.warmup(portrait)
    assert len(task.calls) == 6, 'Replacing the model must invalidate warmup state'


def test_warmup_does_not_borrow_an_active_slot(runtime):
    task = runtime()
    slot = task.service._available.get_nowait()
    try:
        assert task.service.warmup() is False
        assert task.calls == [] and task.service.model is None
        assert task.service._active_requests == 0
    finally:
        task.service._available.put_nowait(slot)
    assert task.service.warmup()


def test_cancelled_async_waiter_does_not_release_direct_running_person(runtime):
    task = runtime()
    entered, proceed = threading.Event(), threading.Event()
    def block():
        entered.set()
        assert proceed.wait(5)
    task.hooks['work'] = block
    async def scenario():
        running = asyncio.create_task(asyncio.to_thread(task.invoke, 'people'))
        closing = None
        try:
            deadline = asyncio.get_running_loop().time() + 3
            while not entered.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(.005)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert task.service._active_requests == 1
            closing = asyncio.create_task(asyncio.to_thread(task.service.close))
            while task.service._accepting:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(.005)
            assert not closing.done() and task.service.model is not None
        finally:
            proceed.set()
            await asyncio.gather(running, return_exceptions=True)
            if closing is not None:
                await asyncio.wait_for(closing, 3)
    asyncio.run(scenario())
    assert task.service._active_requests == 0 and task.service._closed
    assert task.service.model is None


def test_cleanup_error_still_retires_all_models_and_threads(runtime, caplog):
    task = runtime(size=2)
    cleanup_calls = []
    for index, slot in enumerate(task.service._slots):
        slot._cuda_executor = ThreadPoolExecutor(max_workers=1)
        def sync(index=index):
            cleanup_calls.append(index)
            if index == 0:
                raise RuntimeError('fake CUDA cleanup error')
        slot._cuda_stream = SimpleNamespace(synchronize=sync)
        slot.model = slot.inspector = slot.lab = slot.hands = object()
        slot.model_kind = 'yolo'
    task.service.close()
    assert cleanup_calls == [0, 1]
    assert 'Realtime model worker cleanup failed' in caplog.text
    for slot in task.service._slots:
        assert slot.model is slot.model_kind is slot.inspector is slot.lab is slot.hands is None
        assert slot._cuda_stream is None
        assert all(not thread.is_alive() for thread in slot._cuda_executor._threads)


def test_close_releases_model_if_executor_was_already_stopped(runtime):
    task = runtime()
    slot = task.service._slots[0]
    slot.model = object()
    slot._cuda_executor = ThreadPoolExecutor(max_workers=1)
    slot._cuda_executor.submit(lambda: None).result()
    slot._cuda_executor.shutdown()
    synchronized = []
    slot._cuda_stream = SimpleNamespace(synchronize=lambda: synchronized.append(True))
    task.service.close()
    assert task.service._closed and slot.model is None and synchronized == [True]


def test_concurrent_close_waits_until_cleanup_finishes(runtime):
    task = runtime()
    entered, proceed = threading.Event(), threading.Event()
    slot = task.service._slots[0]
    slot.model = object()
    slot._cuda_executor = ThreadPoolExecutor(max_workers=1)
    def synchronize():
        entered.set()
        assert proceed.wait(5)
    slot._cuda_stream = SimpleNamespace(synchronize=synchronize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(task.service.close)
        try:
            assert entered.wait(3)
            second = pool.submit(task.service.close)
            assert not first.done() and not second.done() and not task.service._closed
        finally:
            proceed.set()
        first.result(3)
        second.result(3)
    assert task.service._closed and slot.model is None
