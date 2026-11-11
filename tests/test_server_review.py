"""Regressions found during the six-camera deployment review."""
import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.station_scheduler import StationScheduler
from backend.streams import StreamRegistry
from test_station_terminals import begin, fake_model, register, send


def test_scheduler_counts_time_spent_receiving_frame_against_deadline():
    async def scenario():
        scheduler = StationScheduler(500)
        async def unexpected(wait):
            pytest.fail('frame already stale before enqueue reached inference')
        try:
            with pytest.raises(HTTPException) as error:
                await scheduler.submit('station-1', unexpected, received_at=time.monotonic() - 1)
            assert error.value.status_code == 429
            assert scheduler.snapshot()['stations']['station-1']['expired'] == 1
        finally:
            await scheduler.close()
    asyncio.run(scenario())


def test_wall_clock_sampling_gap_cannot_be_hidden_by_client_timestamp(monkeypatch):
    registry = StreamRegistry()
    key = ('owner', 'stream')
    clock = [100.]
    monkeypatch.setattr('backend.streams.time.monotonic', lambda: clock[0])
    registry.get(key, 0, ('fixture',))
    registry.record_sample(key, 0)
    clock[0] += 5
    registry.get(key, .25, ('fixture',))
    registry.record_sample(key, .25)
    result = registry.protect_result(key, {'compliance': {'status': 'PASS'}})
    assert result['compliance']['status'] == 'REVIEW'
    assert result['originalCompliance']['status'] == 'PASS'
    assert result['evidenceGaps']


def test_station_finish_preserves_registry_evidence_gaps(authenticated):
    _, app, client = authenticated
    fake_model(app)
    credential = register(client)['credential']
    stream = begin(client, credential)
    assert send(client, credential, stream).status_code == 200
    state = app.state.terminals.streams['station-1']
    state['analysisType'] = 'screw'
    registry = app.state.screw_realtime.registry
    saved = registry.get(state['key'], 0, ('fixture',))
    registry._append_gap(saved, {'start': 0, 'end': 4, 'reason': 'fixture', 'source': 'server'})
    app.state.screw_realtime.finish = lambda key: {'final': True, 'compliance': {'status': 'PASS'}}
    response = client.post(f'/api/v1/station-streams/{stream["streamId"]}/finish',
                           headers={'X-Terminal-Key': credential})
    assert response.status_code == 200, response.text
    assert response.json()['compliance']['status'] == 'REVIEW'
    assert response.json()['evidenceGaps']


def test_old_station_result_does_not_become_fresh_while_next_evidence_is_saving(authenticated):
    _, app, client = authenticated
    fake_model(app)
    credential = register(client)['credential']
    stream = begin(client, credential)
    assert send(client, credential, stream).status_code == 200
    state = app.state.terminals.streams['station-1']
    state['lastCaptureAt'] = time.monotonic() - 2
    entered, release = threading.Event(), threading.Event()
    def save(*args, **kwargs):
        entered.set()
        assert release.wait(5)
    app.state.evidence.observe = save
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(send, client, credential, stream, 1, .25)
        assert entered.wait(2)
        try:
            status = client.get('/api/v1/stations/station-1/live-status',
                                headers={'X-Terminal-Key': credential}).json()
            assert status['state'] == 'STALE'
            assert status['result'] is None
        finally:
            release.set()
        assert pending.result().status_code == 200


def test_realtime_worker_mode_keeps_maintenance_without_claiming_gpu_work(context, monkeypatch):
    import backend.worker as worker
    import backend.evidence as evidence
    settings, app, _ = context
    calls = []
    monkeypatch.setattr(worker, 'execute_one', lambda *a, **kw: pytest.fail('offline GPU work started'))
    monkeypatch.setattr(worker, 'pulse_worker', lambda *a: calls.append('heartbeat'))
    monkeypatch.setattr(worker, 'cleanup_data', lambda *a, **kw: calls.append('cleanup'))
    monkeypatch.setattr(evidence, 'expire_evidence', lambda *a, **kw: calls.append('evidence'))
    assert worker.poll_once(app.state.engine, replace(settings, worker_analysis_enabled=False),
                            worker_id='test', cleanup_due=True) is False
    assert calls == ['evidence', 'cleanup', 'heartbeat']


def test_worker_workloads_can_be_disabled_explicitly(monkeypatch):
    from backend.config import Settings
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('APP_PASSWORD', 'test-only-password')
    monkeypatch.setenv('WORKER_ANALYSIS_ENABLED', 'false')
    monkeypatch.setenv('WORKER_TRAINING_ENABLED', 'false')
    settings = Settings.from_env()
    assert not settings.worker_analysis_enabled and not settings.worker_training_enabled
    monkeypatch.setenv('WORKER_TRAINING_ENABLED', 'typo')
    with pytest.raises(RuntimeError, match='WORKER_TRAINING_ENABLED'):
        Settings.from_env()


def test_runtime_status_uses_execution_device_instead_of_weight_loader(tmp_path):
    from backend.realtime import RealtimeDetector
    detector = RealtimeDetector(tmp_path/'model.pt', pool_size=1)
    detector.model_kind = 'yolo'
    detector.model = SimpleNamespace(device='cpu', predictor=SimpleNamespace(device='cuda:0'))
    assert detector.status_snapshot()['device'] == 'cuda:0'
