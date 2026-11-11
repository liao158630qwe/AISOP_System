"""CPU checks for same-frame parallel branches and failure-safe tracker lifetime."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import HTTPException

import backend.person_overlap as overlap


@pytest.fixture
def runtime(monkeypatch):
    services = []
    import algorithms.cuda_lane
    monkeypatch.setattr(algorithms.cuda_lane, 'run_in_lane', lambda owner, fn, ready=None: fn())
    def create(kind):
        module = importlib.import_module('backend.' + kind + '_realtime')
        calls, hooks = [], {}
        image = np.zeros((64, 96, 3), np.uint8)
        _, jpeg = cv2.imencode('.jpg', image)
        def event(name, frame=None):
            calls.append((name, frame, threading.get_ident()))
            if name in hooks:
                hooks[name]()
        class Ready:
            def synchronize(self): event('ready')
        def record():
            event('record')
            return Ready()
        monkeypatch.setattr(overlap, 'record_frame_ready', record)
        import algorithms.cuda_vision
        monkeypatch.setattr(algorithms.cuda_vision, 'decode', lambda payload: image)
        config = dict(targets=[], model_name='test', sequence=[])
        monkeypatch.setattr(module, 'snapshot', lambda: dict(config=config, config_hash='fixed', assets={}))
        class Detector:
            def __init__(self, frozen):
                self.config, self.device = frozen['config'], 'cpu-test'
            def inspect(self, frame):
                event('inspection', frame)
                event('inspection_done', frame)
                return dict(targets=[], registered=True)
        def people(frame, tracker, stamp):
            event('person', frame)
            event('person_done', frame)
            return dict(people=[], skeleton={}, personProcessingMs=1)
        class History:
            assessment_time = 0
            def update(self, *args):
                event('rules')
                return []
            def sequence(self): return {}
        class Sequence:
            def update(self, *args):
                event('rules')
                return {}
        monkeypatch.setattr(module, 'ScrewDetector' if kind == 'screw' else 'StickerDetector', Detector)
        monkeypatch.setattr(module, 'compliance', lambda *args, **kwargs: dict(status='PASS'))
        if kind == 'screw':
            monkeypatch.setattr(module, 'current_display', lambda result: result)
            monkeypatch.setattr(module, 'create_history', lambda config: History())
            service = module.RealtimeScrews(SimpleNamespace(detect_people=people))
        else:
            monkeypatch.setattr(module, 'create_sequence', lambda config: Sequence())
            monkeypatch.setattr(module, 'summarize', lambda *args, **kwargs: ([], 0))
            service = module.RealtimeStickers(SimpleNamespace(detect_people=people))
        services.append(service)
        def predict(stamp=0):
            return service.predict(jpeg.tobytes(), stream_key=('owner', 'camera'), frame_time=stamp)
        return SimpleNamespace(service=service, predict=predict, calls=calls, hooks=hooks, image=image)
    yield create
    for service in services:
        service.close()


@pytest.mark.parametrize('kind', ['screw', 'sticker'])
def test_person_and_inspection_overlap_once_on_identical_frame_before_rules(runtime, kind):
    task = runtime(kind)
    barrier = threading.Barrier(2)
    task.hooks['person'] = task.hooks['inspection'] = lambda: barrier.wait(timeout=3)
    first = task.predict()
    second = task.predict(.25)
    assert first['sampleCount'] == 1 and second['sampleCount'] == 2
    names = [name for name, _, _ in task.calls]
    assert names.count('person') == names.count('inspection') == names.count('rules') == 2
    persons = [frame for name, frame, _ in task.calls if name == 'person']
    inspections = [frame for name, frame, _ in task.calls if name == 'inspection']
    assert all(a is b is task.image for a, b in zip(persons, inspections))
    person_threads = {worker for name, _, worker in task.calls if name == 'person'}
    inspect_threads = {worker for name, _, worker in task.calls if name == 'inspection'}
    assert len(person_threads) == len(inspect_threads) == 1 and person_threads != inspect_threads
    records = [i for i, name in enumerate(names) if name == 'record']
    ends = [*records[1:], len(names)]
    for begin, end in zip(records, ends):
        frame_names = names[begin:end]
        assert frame_names.index('record') < frame_names.index('ready') < frame_names.index('person')
        assert frame_names.index('rules') > max(frame_names.index('person_done'), frame_names.index('inspection_done'))
    assert set(first['timingsMs']) == {'decode', 'snapshot', 'modelLoad', 'stream', 'person', 'inspection', 'rules'}
    assert first['totalProcessingMs'] >= sum(first['timingsMs'].values()) - .01


@pytest.mark.parametrize('kind', ['screw', 'sticker'])
def test_inspection_error_drains_person_before_releasing_stream_and_capacity(runtime, kind):
    task = runtime(kind)
    person_started, inspection_failed, proceed = threading.Event(), threading.Event(), threading.Event()
    def person():
        person_started.set()
        assert proceed.wait(5)
    def inspection():
        assert person_started.wait(3)
        inspection_failed.set()
        raise ValueError('inspection failed')
    task.hooks.update(person=person, inspection=inspection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(task.predict)
        try:
            assert inspection_failed.wait(3)
            assert not pending.done() and task.service.lock.locked()
            with pytest.raises(HTTPException) as busy:
                task.predict(.25)
            assert busy.value.status_code == 429
            state = task.service.registry.items[('owner', 'camera')]
            assert state['sampleCount'] == 0 and state['timestamp'] == 0
            assert not any(name == 'rules' for name, _, _ in task.calls)
        finally:
            proceed.set()
        with pytest.raises(ValueError, match='inspection failed'):
            pending.result(3)
    assert not task.service.lock.locked() and not task.service._person_worker._busy
    task.hooks.clear()
    assert task.predict(.5)['sampleCount'] == 1


@pytest.mark.parametrize('kind', ['screw', 'sticker'])
def test_person_error_prevents_evidence_update_and_recovers_on_next_frame(runtime, kind):
    task = runtime(kind)
    barrier = threading.Barrier(2)
    def person():
        barrier.wait(3)
        raise ValueError('person failed')
    task.hooks.update(person=person, inspection=lambda: barrier.wait(3))
    with pytest.raises(ValueError, match='person failed'):
        task.predict()
    assert not any(name == 'rules' for name, _, _ in task.calls)
    assert task.service.registry.items[('owner', 'camera')]['sampleCount'] == 0
    assert not task.service._person_worker._busy
    task.hooks.clear()
    assert task.predict(.25)['sampleCount'] == 1


def test_worker_rejects_queue_and_close_waits_for_complete_context(monkeypatch):
    monkeypatch.setattr(overlap, 'record_frame_ready', lambda: SimpleNamespace(synchronize=lambda: None))
    worker = overlap.PersonWorker()
    entered, proceed = threading.Event(), threading.Event()
    def work():
        with worker.run(lambda: 'person complete') as pending:
            assert pending.result() == 'person complete'
            entered.set()
            assert proceed.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(work)
        try:
            assert entered.wait(3)
            with pytest.raises(HTTPException) as busy:
                with worker.run(lambda: None): pass
            assert busy.value.status_code == 429
            shutdown = pool.submit(worker.close)
            # Close must also wait for the inspection-side context, even when
            # the person future has already completed.
            assert not shutdown.done()
        finally:
            proceed.set()
        running.result(3)
        shutdown.result(3)
    with pytest.raises(HTTPException) as closed:
        with worker.run(lambda: None): pass
    assert closed.value.status_code == 503
    assert all(not thread.is_alive() for thread in worker.executor._threads)
    worker.close()


def test_worker_preserves_inspection_error_after_person_error(monkeypatch):
    monkeypatch.setattr(overlap, 'record_frame_ready', lambda: SimpleNamespace(synchronize=lambda: None))
    worker = overlap.PersonWorker()
    def person(): raise RuntimeError('person failure')
    try:
        with pytest.raises(ValueError, match='inspection failure'):
            with worker.run(person):
                raise ValueError('inspection failure')
        assert not worker._busy
    finally:
        worker.close()


@pytest.mark.parametrize('kind', ['screw', 'sticker'])
def test_specialist_close_stops_new_frames_and_waits_for_both_branches(runtime, kind):
    import time
    from backend.specialist_pool import close_specialist
    task = runtime(kind)
    entered, proceed = threading.Event(), threading.Event()
    def inspection():
        entered.set()
        assert proceed.wait(5)
    task.hooks['inspection'] = inspection
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(task.predict)
        try:
            assert entered.wait(3)
            shutdown = pool.submit(close_specialist, task.service)
            deadline = time.monotonic()+2
            while task.service._lifecycle._accepting and time.monotonic()<deadline:
                time.sleep(.005)
            assert not task.service._lifecycle._accepting and not shutdown.done()
            with pytest.raises(HTTPException) as rejected:
                task.predict(.25)
            assert rejected.value.status_code == 503
        finally:
            proceed.set()
        assert pending.result(3)['sampleCount'] == 1
        shutdown.result(3)
    assert task.service._lifecycle._closed
    assert all(not thread.is_alive() for thread in task.service._person_worker.executor._threads)
