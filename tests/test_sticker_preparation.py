"""Preparation uses real model lanes but never creates inspection evidence."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import numpy as np
import pytest
from sqlalchemy import func, select

from backend.db import station_inspections, station_stream_leases
from backend.sticker_realtime import RealtimeStickers
from backend.specialist_pool import SpecialistPool
from backend.streams import StreamRegistry
from algorithms.video import AnalysisError


@pytest.fixture
def preparation(monkeypatch):
    import backend.sticker_realtime as module
    import algorithms.cuda_lane as lane
    import algorithms.cuda_vision as vision
    calls = []
    frozen = dict(config_hash='config1', assets={'model': 'weights1'},
                  config={'reference': 'fixture.jpg'})
    image = np.zeros((48, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(module, 'snapshot', lambda: frozen)
    monkeypatch.setattr(module.cv2, 'imread', lambda path: image)
    monkeypatch.setattr(vision, 'decode', lambda payload: image)
    monkeypatch.setattr(lane, 'run_in_lane', lambda owner, fn: fn())
    class Detector:
        def __init__(self, config):
            calls.append('load')
        def inspect(self, frame):
            assert frame is image
            calls.append('inspect')
    monkeypatch.setattr(module, 'StickerDetector', Detector)
    def pose(frame):
        assert frame is image
        calls.append('pose')
        return True
    service = RealtimeStickers(SimpleNamespace(warmup=pose))
    service._cuda_stream = SimpleNamespace(synchronize=lambda: calls.append('sync'))
    yield SimpleNamespace(service=service, calls=calls, frozen=frozen, module=module)
    service.close()


def test_prepare_is_cached_and_has_no_stream_evidence(preparation):
    p = preparation
    assert p.service.warmup() is True
    assert p.calls == ['pose', 'load', 'inspect', 'inspect', 'sync']
    assert not p.service.registry.items
    assert p.service.warmup() is True
    assert len(p.calls) == 5
    p.frozen['assets']['model'] = 'weights2'
    assert p.service.warmup() is True
    assert p.calls.count('load') == 2
    assert not p.service.registry.items


def test_concurrent_preparations_only_load_once(preparation, monkeypatch):
    p = preparation
    entered, release = threading.Event(), threading.Event()
    original = p.service._warmup_lane
    def wait(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(p.service, '_warmup_lane', wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(p.service.warmup)
        assert entered.wait(2)
        second = pool.submit(p.service.warmup)
        release.set()
        assert first.result(3) and second.result(3)
    assert p.calls.count('load') == 1


def test_failed_preparation_is_retryable(preparation, monkeypatch):
    p = preparation
    original = p.service.pose_detector.warmup
    monkeypatch.setattr(p.service.pose_detector, 'warmup', lambda frame: False)
    assert p.service.warmup() is False
    assert p.service._prepared_signature is None and p.service.detector is None
    monkeypatch.setattr(p.service.pose_detector, 'warmup', original)
    assert p.service.warmup() is True
    p.frozen['config_hash'] = 'config2'
    monkeypatch.setattr(p.module, 'StickerDetector', lambda frozen: (_ for _ in ()).throw(ValueError('load failed')))
    with pytest.raises(ValueError, match='load failed'):
        p.service.warmup()
    assert p.service._prepared_signature[0] == 'config1'
    assert not p.service.registry.items


def bind(client, station=2, kind='sticker'):
    response = client.put(f'/api/v1/admin/terminals/station-{station}', json=dict(
        workstationId=f'prepare-pc-{station}', cameraId=f'prepare-camera-{station}', analysisType=kind))
    assert response.status_code == 200, response.text
    return response.json()


def test_prepare_endpoint_auth_and_no_lease_or_inspection(authenticated, monkeypatch):
    _, app, client = authenticated
    data = bind(client)
    calls = []
    monkeypatch.setattr(app.state.sticker_realtime, 'warmup', lambda: calls.append(1) or True)
    response = client.post('/api/v1/stations/station-2/prepare')
    assert response.status_code == 200, response.text
    assert response.json() == dict(ready=True, analysisType='sticker', revision=data['binding']['revision'])
    assert calls == [1]
    assert not app.state.terminals.streams and not app.state.sticker_realtime.registry.items
    with app.state.engine.connect() as c:
        for table in (station_inspections, station_stream_leases):
            assert c.scalar(select(func.count()).select_from(table)) == 0
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/stations/station-2/prepare').status_code == 403
    client.cookies.clear()
    assert client.post('/api/v1/stations/station-2/prepare').status_code == 401
    assert client.post('/api/v1/stations/station-2/prepare', headers={'X-Terminal-Key': data['credential']}).status_code == 200
    assert client.post('/api/v1/stations/station-1/prepare', headers={'X-Terminal-Key': data['credential']}).status_code == 403


@pytest.mark.parametrize('failure', [False, AnalysisError('MISSING', '模型缺失')])
def test_prepare_endpoint_reports_retryable_failure(authenticated, monkeypatch, failure):
    _, app, client = authenticated
    bind(client)
    def warmup():
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(app.state.sticker_realtime, 'warmup', warmup)
    response = client.post('/api/v1/stations/station-2/prepare')
    assert response.status_code == 503, response.text
    assert not app.state.terminals.streams


def test_other_tasks_do_not_prepare_sticker(authenticated, monkeypatch):
    _, app, client = authenticated
    bind(client, kind='pose')
    def unexpected():
        pytest.fail('A pose station must not prepare sticker models')
    monkeypatch.setattr(app.state.sticker_realtime, 'warmup', unexpected)
    assert client.post('/api/v1/stations/station-2/prepare').status_code == 409


def test_pool_preparation_reserves_only_one_slot_and_restores_it():
    entered, release = threading.Event(), threading.Event()
    class Slot:
        def __init__(self):
            self.registry = StreamRegistry()
            self.detector = None
        def warmup(self):
            entered.set()
            assert release.wait(3)
            self.detector = 'prepared'
            return True
        def predict(self, payload):
            return self.detector
    pool = SpecialistPool(Slot, 2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(pool.warmup)
        assert entered.wait(2)
        try:
            assert pool.available.qsize() == 1
            assert pool.predict(b'') is None  # Another camera can use the idle slot.
        finally:
            release.set()
        assert pending.result(3)
    assert pool.predict(b'') == 'prepared'
    assert pool.available.qsize() == 2


def test_pool_failed_preparation_returns_capacity():
    class Slot:
        def __init__(self):
            self.registry = StreamRegistry()
        def warmup(self):
            raise ValueError('failed')
    pool = SpecialistPool(Slot, 2)
    with pytest.raises(ValueError, match='failed'):
        pool.warmup()
    assert pool.available.qsize() == 2
    held = [pool.available.get_nowait() for _ in range(2)]
    try:
        assert pool.warmup() is False
    finally:
        for slot in held:
            pool.available.put_nowait(slot)
