"""CPU concurrency regressions with real exclusive model worker threads."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import cv2
import numpy as np
import pytest
from fastapi import HTTPException

import backend.scene_realtime as realtime
from backend.scenes import SceneDefinition
from test_scene_management import definition, raw_checks


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    snapshots = {}
    for name in ('a', 'b', 'c'):
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'scene-cache-' + name))
        snapshots[name] = dict(model={'id': identity, 'sha256': 'weights-' + name,
            'task': 'detect', 'classes': {'0': 'widget', '1': 'defect'}},
            model_path=str(tmp_path / (name + '.pt')), sha256='scene-' + name, revision=1,
            config=SceneDefinition(**definition(identity, name=name)).model_dump(mode='json'))
    snapshots['shared'] = deepcopy(snapshots['a'])
    snapshots['shared']['config']['name'] = 'shared'
    snapshots['shared']['sha256'] = 'scene-shared'
    hooks = {'load': None, 'inspect': None, 'snapshot': None}
    records = []
    services = []

    def snapshot(name, owner):
        if hooks['snapshot'] is not None:
            hooks['snapshot'](name, owner)
        return deepcopy(snapshots[name])

    def load(frozen):
        records.append(('load', frozen['model']['id'], threading.get_ident()))
        if hooks['load'] is not None:
            hooks['load'](frozen)
        return SimpleNamespace(identity=frozen['model']['id'])

    class Detector:
        def __init__(self, frozen, model):
            self.frozen, self.model = frozen, model
        def inspect(self, frame):
            assert self.model.identity == self.frozen['model']['id']
            records.append(('inspect', self.model.identity, threading.get_ident()))
            if hooks['inspect'] is not None:
                hooks['inspect'](self.frozen)
            return raw_checks()

    monkeypatch.setattr(realtime, 'run_model_lane', lambda entry, fn: entry.executor.submit(fn).result())
    monkeypatch.setattr(realtime, 'load_model', load)
    monkeypatch.setattr(realtime, 'SceneDetector', Detector)
    _, jpeg = cv2.imencode('.jpg', np.full((64, 96, 3), 100, np.uint8))

    def create(capacity=3, concurrency=2):
        service = realtime.RealtimeScenes(SimpleNamespace(snapshot=snapshot), capacity, concurrency)
        services.append(service)
        return service

    def frame(service, scene='a', stream=None, timestamp=0, owner='owner', payload=None):
        return service.predict(jpeg.tobytes() if payload is None else payload,
            scene_id=scene, stream_key=(owner, stream or scene), frame_time=timestamp, owner=owner)

    yield SimpleNamespace(create=create, frame=frame, hooks=hooks, records=records, snapshots=snapshots)
    for service in services:
        service.close()


def rejected(status, function, *args, **kwargs):
    started = time.monotonic()
    with pytest.raises(HTTPException) as error:
        function(*args, **kwargs)
    assert error.value.status_code == status
    assert time.monotonic() - started < 1
    if status == 429:
        assert error.value.headers == {'Retry-After': '1'}


def test_distinct_models_overlap_and_keep_loading_and_inference_on_fixed_threads(runtime):
    service = runtime.create()
    entered = {name: threading.Event() for name in ('a', 'b')}
    proceed = threading.Event()
    def inspect(frozen):
        entered[frozen['config']['name']].set()
        assert proceed.wait(5)
    runtime.hooks['inspect'] = inspect
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.frame, service, 'a')
        second = pool.submit(runtime.frame, service, 'b')
        try:
            assert all(event.wait(3) for event in entered.values())
            assert service.cache_metrics()['activeModels'] == 2
            assert service.cache_metrics()['modelLoads'] == 2
        finally:
            proceed.set()
        assert first.result(3)['sampleCount'] == second.result(3)['sampleCount'] == 1
    runtime.hooks['inspect'] = None
    assert runtime.frame(service, 'a', timestamp=.25)['compliance']['status'] == 'PASS'
    assert runtime.frame(service, 'b', timestamp=.25)['compliance']['status'] == 'PASS'
    by_model = {}
    for _, identity, worker in runtime.records:
        by_model.setdefault(identity, set()).add(worker)
    assert all(len(workers) == 1 for workers in by_model.values())
    assert len(set.union(*by_model.values())) == 2
    assert service.cache_metrics()['cacheHits'] == 2


@pytest.mark.parametrize('phase', ['load', 'inspect'])
def test_same_weight_never_loads_twice_or_reenters_while_busy(runtime, phase):
    service = runtime.create(capacity=2, concurrency=2)
    entered, proceed = threading.Event(), threading.Event()
    def block(frozen):
        entered.set()
        assert proceed.wait(5)
    runtime.hooks[phase] = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runtime.frame, service)
        try:
            assert entered.wait(3)
            rejected(429, runtime.frame, service, 'shared')
            assert ('owner', 'shared') not in service.streams.items
            assert len(service.models) == 1
            assert service.cache_metrics()['loadingModels'] == int(phase == 'load')
        finally:
            proceed.set()
        first.result(3)
    runtime.hooks[phase] = None
    result = runtime.frame(service, 'shared')
    assert result['sampleCount'] == 1 and result['compliance']['status'] == 'REVIEW'
    assert sum(record[0] == 'load' for record in runtime.records) == 1


@pytest.mark.parametrize('capacity,concurrency,phase', [(3, 1, 'inspect'), (1, 2, 'load'), (1, 2, 'inspect')])
def test_busy_global_budget_or_full_cache_rejects_without_advancing_stream(runtime, capacity, concurrency, phase):
    service = runtime.create(capacity, concurrency)
    entered, proceed = threading.Event(), threading.Event()
    runtime.frame(service, 'b')
    previous = dict(service.streams.items[('owner', 'b')])
    def block(frozen):
        entered.set()
        assert proceed.wait(5)
    runtime.hooks[phase] = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runtime.frame, service, 'a')
        try:
            assert entered.wait(3)
            before = service.cache_metrics()
            rejected(429, runtime.frame, service, 'b', timestamp=.25)
            after = service.cache_metrics()
            assert after['evictions'] == before['evictions']
            state = service.streams.items[('owner', 'b')]
            assert state['timestamp'] == previous['timestamp']
            assert state['used'] == previous['used']
        finally:
            proceed.set()
        first.result(3)
    runtime.hooks[phase] = None
    assert runtime.frame(service, 'b', timestamp=.25)['compliance']['status'] == 'PASS'


def test_active_lru_is_skipped_and_idle_executor_is_reclaimed(runtime):
    service = runtime.create(capacity=2, concurrency=2)
    entered, proceed = threading.Event(), threading.Event()
    def block_a(frozen):
        if frozen['config']['name'] == 'a':
            entered.set()
            assert proceed.wait(5)
    runtime.hooks['inspect'] = block_a
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runtime.frame, service, 'a')
        try:
            assert entered.wait(3)
            runtime.frame(service, 'b')
            old_b = service.models[service.model_key(runtime.snapshots['b'], 'owner')]
            assert next(iter(service.models))[1] == runtime.snapshots['a']['model']['id']
            result = runtime.frame(service, 'c')
            assert result['sceneName'] == 'c'
            assert {entry.key[1] for entry in service.models.values()} == {
                runtime.snapshots['a']['model']['id'], runtime.snapshots['c']['model']['id']}
            assert old_b.model is None
            assert all(not worker.is_alive() for worker in old_b.executor._threads)
            assert service.cache_metrics()['evictions'] == 1
        finally:
            proceed.set()
        assert first.result(3)['compliance']['status'] == 'REVIEW'


@pytest.mark.parametrize('phase', ['load', 'inspect'])
def test_failed_model_releases_capacity_and_invalidates_only_its_stream(runtime, phase):
    service = runtime.create(capacity=2, concurrency=2)
    for scene in ('a', 'b'):
        runtime.frame(service, scene)
        runtime.frame(service, scene, timestamp=.25)
    if phase == 'load':
        # A changed asset requires a fresh model while B remains valid.
        runtime.snapshots['a']['model']['sha256'] += '-new'
    def fail_a(frozen):
        if frozen['config']['name'] == 'a':
            raise RuntimeError('deliberate model failure')
    runtime.hooks[phase] = fail_a
    with pytest.raises((RuntimeError, HTTPException)):
        runtime.frame(service, 'a', timestamp=.5)
    assert service.cache_metrics()['activeModels'] == 0
    assert all(not entry.loading and not entry.busy for entry in service.models.values())
    rejected(404, service.finish, ('owner', 'a'))
    assert service.finish(('owner', 'b'))['compliance']['status'] == 'PASS'
    runtime.hooks[phase] = None
    assert runtime.frame(service, 'a', timestamp=.75)['compliance']['status'] == 'REVIEW'
    assert runtime.frame(service, 'a', timestamp=1)['compliance']['status'] == 'PASS'


def test_stream_reentry_and_finish_do_not_overtake_active_frame(runtime):
    service = runtime.create()
    entered, proceed = threading.Event(), threading.Event()
    def block(frozen):
        entered.set()
        assert proceed.wait(5)
    runtime.hooks['inspect'] = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runtime.frame, service)
        try:
            assert entered.wait(3)
            rejected(429, runtime.frame, service, 'b', stream='a', timestamp=.25)
            rejected(429, service.finish, ('owner', 'a'))
            assert service.streams.items[('owner', 'a')]['timestamp'] == 0
        finally:
            proceed.set()
        first.result(3)
    runtime.hooks['inspect'] = None
    assert runtime.frame(service, timestamp=.25)['sampleCount'] == 2


@pytest.mark.parametrize('phase', ['snapshot', 'load', 'inspect'])
def test_release_cancels_inflight_result_and_prevents_stream_resurrection(runtime, phase):
    service = runtime.create()
    entered, proceed = threading.Event(), threading.Event()
    def block(*args):
        entered.set()
        assert proceed.wait(5)
    runtime.hooks[phase] = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runtime.frame, service)
        try:
            assert entered.wait(3)
            service.release(('owner', 'a'))
            assert ('owner', 'a') not in service.streams.items
            rejected(429, runtime.frame, service, timestamp=.25)
        finally:
            proceed.set()
        with pytest.raises(HTTPException) as cancelled:
            first.result(3)
        assert cancelled.value.status_code == 409
    assert ('owner', 'a') not in service.streams.items
    assert service.cache_metrics()['activeModels'] == 0
    runtime.hooks[phase] = None
    assert runtime.frame(service)['sampleCount'] == 1


def test_close_waits_for_active_work_without_holding_cache_lock(runtime):
    service = runtime.create()
    entered, proceed = threading.Event(), threading.Event()
    def block(frozen):
        entered.set()
        assert proceed.wait(5)
    runtime.hooks['inspect'] = block
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(runtime.frame, service)
        try:
            assert entered.wait(3)
            entry = next(iter(service.models.values()))
            shutdown = pool.submit(service.close)
            deadline = time.monotonic() + 2
            while not service._closed and time.monotonic() < deadline:
                time.sleep(.005)
            assert service._closed and not shutdown.done()
            assert service.cache_metrics()['activeModels'] == 1
            rejected(503, runtime.frame, service, 'b')
            second_shutdown = pool.submit(service.close)
        finally:
            proceed.set()
        first.result(3)
        shutdown.result(3)
        second_shutdown.result(3)
    assert service.cache_metrics()['residentModels'] == 0
    assert service._close_complete and not service._entries
    assert entry.model is None
    assert all(not worker.is_alive() for worker in entry.executor._threads)


@pytest.mark.parametrize('part', ['owner', 'id', 'sha256', 'model_path', 'task', 'device'])
def test_model_key_keeps_all_asset_and_security_boundaries(runtime, part):
    frozen = deepcopy(runtime.snapshots['a'])
    original = realtime.model_key(frozen, 'owner')
    owner = 'another' if part == 'owner' else 'owner'
    if part in ('id', 'sha256', 'task'):
        frozen['model'][part] += '-other'
    elif part == 'model_path':
        frozen[part] += '.different'
    elif part == 'device':
        frozen['config'][part] = 'cuda:1'
    assert realtime.model_key(frozen, owner) != original


def test_model_key_normalizes_path_and_cuda_aliases(runtime):
    frozen = deepcopy(runtime.snapshots['a'])
    original = realtime.model_key(frozen, 'owner')
    frozen['model_path'] = str(Path(frozen['model_path']).parent / 'nested' / '..' / 'a.pt')
    frozen['config']['device'] = str(original[-1])
    assert realtime.model_key(frozen, 'owner') == original


@pytest.mark.parametrize('value', [False, True, 0, 7, 1.5, '2', None])
@pytest.mark.parametrize('field', ['model_cache_size', 'concurrency'])
def test_cache_and_concurrency_require_bounded_integer(field, value):
    with pytest.raises(ValueError):
        realtime.RealtimeScenes(None, **{field: value})


def fail_shutdown_after_join(monkeypatch, entry, error):
    original = entry.executor.shutdown
    def fail(wait=True):
        original(wait=wait)
        raise error
    monkeypatch.setattr(entry.executor, 'shutdown', fail)


def assert_entry_closed(entry):
    assert entry.model is None and entry.cuda_stream is None
    assert entry.executor._shutdown
    assert all(not worker.is_alive() for worker in entry.executor._threads)


def test_close_attempts_every_entry_after_cleanup_failure(runtime, monkeypatch):
    service = runtime.create(capacity=3)
    for scene in ('a', 'b', 'c'):
        runtime.frame(service, scene)
    entries = list(service._entries)
    first_error = RuntimeError('first entry stream cleanup failed')
    def fail_sync(): raise first_error
    entries[0].cuda_stream = SimpleNamespace(synchronize=fail_sync)
    fail_shutdown_after_join(monkeypatch, entries[1], ValueError('second entry shutdown failed'))
    with pytest.raises(RuntimeError) as raised:
        service.close()
    assert raised.value is first_error
    assert service._close_complete and not service.models and not service._entries
    for entry in entries:
        assert_entry_closed(entry)
    service.close()


@pytest.mark.parametrize('phase', ['load', 'inspect'])
def test_cleanup_failure_releases_model_lease_and_preserves_request_error(runtime, monkeypatch, phase):
    service = runtime.create(capacity=1, concurrency=1)
    inference_error = RuntimeError('original model failure')
    cleanup_error = ValueError('model cleanup also failed')
    entries = []
    def fail(frozen):
        entry = next(iter(service.models.values()))
        entries.append(entry)
        fail_shutdown_after_join(monkeypatch, entry, cleanup_error)
        raise inference_error
    runtime.hooks[phase] = fail
    with pytest.raises(HTTPException if phase == 'load' else RuntimeError) as raised:
        runtime.frame(service)
    if phase == 'load':
        assert raised.value.status_code == 503
        assert raised.value.__cause__ is inference_error
    else:
        assert raised.value is inference_error
    assert service._active == 0 and not service._active_streams
    assert not service.models and not service._entries
    assert not entries[0].busy
    assert_entry_closed(entries[0])
    runtime.hooks[phase] = None
    assert runtime.frame(service, timestamp=.25)['sampleCount'] == 1


@pytest.mark.parametrize('rollback_fails', [False, True])
def test_eviction_cleanup_failure_rolls_back_unreturned_reservation(runtime, monkeypatch, rollback_fails):
    service = runtime.create(capacity=1, concurrency=1)
    runtime.frame(service, 'a')
    retired = next(iter(service.models.values()))
    first_error = RuntimeError('evicted entry cleanup failed')
    fail_shutdown_after_join(monkeypatch, retired, first_error)
    original = realtime.ModelEntry
    allocated = []
    def create(key):
        entry = original(key)
        allocated.append(entry)
        if rollback_fails:
            fail_shutdown_after_join(monkeypatch, entry, ValueError('rollback also failed'))
        return entry
    with monkeypatch.context() as local:
        local.setattr(realtime, 'ModelEntry', create)
        with pytest.raises(RuntimeError) as raised:
            runtime.frame(service, 'b')
    assert raised.value is first_error
    assert service._active == 0 and not service._active_streams
    assert not service.models and not service._entries
    assert len(allocated) == 1 and not allocated[0].busy
    for entry in (retired, allocated[0]):
        assert_entry_closed(entry)
    # Reservation failure happened before the stream accepted its timestamp.
    assert runtime.frame(service, 'b')['sampleCount'] == 1


def test_entry_with_preclosed_executor_still_clears_model():
    from backend.scene_model_cache import ModelEntry
    entry = ModelEntry(('test', 0), model=object())
    synchronized = []
    entry.cuda_stream = SimpleNamespace(synchronize=lambda: synchronized.append(True))
    entry.executor.submit(lambda: None).result()
    entry.executor.shutdown(wait=True)
    with pytest.raises(RuntimeError, match='cannot schedule new futures after shutdown'):
        entry.close()
    assert synchronized == [True]
    assert_entry_closed(entry)


@pytest.mark.parametrize('inference_fails', [False, True])
def test_lane_sync_failure_preserves_original_inference_error(monkeypatch, inference_fails):
    import torch
    import backend.scene_model_cache as cache
    entry = cache.ModelEntry(('test', 0))
    inference_error = ValueError('original inference error')
    cleanup_error = RuntimeError('stream wait error')
    def fail_sync(): raise cleanup_error
    entry.cuda_stream = SimpleNamespace(synchronize=fail_sync)
    monkeypatch.setattr(cache, 'require_cuda', lambda device: 0)
    monkeypatch.setattr(torch.cuda, 'device', lambda device: nullcontext())
    monkeypatch.setattr(torch.cuda, 'stream', lambda stream: nullcontext())
    def infer():
        if inference_fails:
            raise inference_error
        return 'success'
    try:
        with pytest.raises(ValueError if inference_fails else RuntimeError) as raised:
            cache.run_model_lane(entry, infer)
        assert raised.value is (inference_error if inference_fails else cleanup_error)
    finally:
        entry.cuda_stream = None
        entry.close()
