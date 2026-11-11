"""HTTP-level realtime backpressure, ordering and measurement regressions."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import pytest


def frame_headers(stream=None, timestamp='0'):
    return {'Content-Type': 'image/jpeg', 'X-Stream-Id': stream or str(uuid.uuid4()),
            'X-Frame-Time': timestamp, 'X-Frame-Id': 'sample-42', 'X-Captured-At': '1793836800123'}


def test_frame_identity_measurement_and_no_stale_replay(authenticated):
    _, app, client = authenticated
    calls = []
    def predict(payload, **kwargs):
        calls.append(kwargs['frame_time'])
        return {'people': []}
    app.state.realtime.predict = predict
    headers = frame_headers()
    first = client.post('/api/v1/realtime/frame', content=b'frame', headers=headers)
    assert first.status_code == 200
    data = first.json()
    assert data['frameId'] == 'sample-42'
    assert data['capturedAt'] == 1793836800123
    assert data['serverRespondedAt'] >= data['serverReceivedAt']
    assert data['serverProcessingMs'] >= 0
    assert first.headers['server-timing'].startswith('realtime;dur=')
    repeated = client.post('/api/v1/realtime/frame', content=b'frame', headers=headers)
    assert repeated.status_code == 409
    assert calls == [0]
    assert client.post('/api/v1/realtime/frame', content=b'frame', headers={**headers, 'X-Frame-Time': '0.25'}).status_code == 200
    metrics = client.get('/api/v1/metrics').json()['realtimeTransport']
    assert metrics['services']['pose']['completed'] == 2
    assert metrics['services']['pose']['rejected'] == 1
    assert metrics['services']['pose']['queueDepth'] == 0
    client.delete('/api/v1/realtime/streams/' + headers['X-Stream-Id'])
    assert client.post('/api/v1/realtime/frame', content=b'frame', headers=headers).status_code == 200


def test_slow_inference_rejects_overload_before_shared_threads(authenticated):
    _, app, client = authenticated
    entered, release = threading.Event(), threading.Event()
    def slow(payload, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'people': []}
    app.state.screw_realtime.predict = slow
    headers = frame_headers()
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(client.post, '/api/v1/realtime/frame?analysis_type=screw', content=b'frame', headers=headers)
        assert entered.wait(2)
        try:
            start = time.monotonic()
            busy = client.post('/api/v1/realtime/frame?analysis_type=screw', content=b'frame', headers=frame_headers())
            assert busy.status_code == 429
            assert busy.headers['Retry-After'] == '1'
            assert 'Server-Timing' in busy.headers
            assert time.monotonic() - start < 1
            assert client.get('/api/v1/health').status_code == 200
            finishing = client.post('/api/v1/realtime/streams/' + headers['X-Stream-Id'] + '/finish')
            assert finishing.status_code == 429 and finishing.headers['Retry-After'] == '1'
            metrics = client.get('/api/v1/metrics').json()['realtimeTransport']['services']['screw']
            assert metrics['active'] == metrics['capacity'] == 1
            assert metrics['rejected'] == 1
        finally:
            release.set()
        assert future.result().status_code == 200
    assert app.state.realtime_transport.snapshot()['services']['screw']['active'] == 0


def test_same_pose_stream_cannot_update_out_of_order_in_parallel(authenticated):
    _, app, client = authenticated
    entered, release = threading.Event(), threading.Event()
    headers = frame_headers()
    def slow(payload, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'people': []}
    app.state.realtime.predict = slow
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.post, '/api/v1/realtime/frame', content=b'frame', headers=headers)
        assert entered.wait(2)
        try:
            second = client.post('/api/v1/realtime/frame', content=b'frame', headers={**headers, 'X-Frame-Time': '0.25'})
            assert second.status_code == 429
        finally:
            release.set()
        assert first.result().status_code == 200


@pytest.mark.parametrize('header,value', [('X-Captured-At', 'nan'), ('X-Captured-At', '-1'), ('X-Frame-Id', 'x'*129)])
def test_invalid_identity_releases_admission(authenticated, header, value):
    _, app, client = authenticated
    app.state.realtime.predict = lambda *args, **kwargs: pytest.fail('invalid frame reached inference')
    response = client.post('/api/v1/realtime/frame', content=b'frame', headers={**frame_headers(), header: value})
    assert response.status_code == 422
    assert app.state.realtime_transport.snapshot()['services']['pose']['active'] == 0


def test_text_assets_are_compressed_and_revalidated(context):
    _, _, client = context
    for path in ('/', '/admin', '/workspace-api.js', '/station-one.js', '/scene-dashboard.js'):
        response = client.get(path, headers={'Accept-Encoding': 'gzip'})
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == ('no-cache' if path == '/' else 'no-store, max-age=0')
        assert response.headers.get('Content-Encoding') == 'gzip'
        assert 'Accept-Encoding' in response.headers['Vary']


def test_realtime_503_retry_and_no_compression(authenticated):
    from fastapi import HTTPException
    _, app, client = authenticated
    def unavailable(*args, **kwargs):
        raise HTTPException(503, 'model unavailable')
    app.state.realtime.predict = unavailable
    response = client.post('/api/v1/realtime/frame', content=b'frame', headers=frame_headers())
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '1'
    assert 'Content-Encoding' not in response.headers
    assert app.state.realtime_transport.snapshot()['services']['pose']['failed'] == 1


def test_warmup_reserves_model_slot(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from backend.realtime import RealtimeDetector
    import algorithms.cuda_lane as lane
    import algorithms.person_detection as person
    entered, release = threading.Event(), threading.Event()
    def predict_people(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr(person, 'predict_people', predict_people)
    monkeypatch.setattr(lane, 'run_in_lane', lambda slot, work, ready=None: work())
    detector = RealtimeDetector(tmp_path / 'model.pt', pool_size=1)
    detector.model, detector.model_kind = object(), 'rtmlib'
    with ThreadPoolExecutor(max_workers=1) as executor:
        warmed = executor.submit(detector.warmup)
        assert entered.wait(2)
        try:
            with pytest.raises(HTTPException) as error:
                detector.predict(b'frame')
            assert error.value.status_code == 429
        finally:
            release.set()
        assert warmed.result() is True
    assert detector.metrics_snapshot()['active'] == 0
    detector.close()


def test_disconnected_long_poll_exits_without_waiting(authenticated, monkeypatch):
    from starlette.requests import Request
    from backend.db import media, runs
    _, app, client = authenticated
    run_id = str(uuid.uuid4())
    with app.state.engine.begin() as connection:
        connection.execute(media.insert().values(id='poll-media', owner='admin', name='sample.mp4',
            suffix='.mp4', size=1, sha256='fixture', metadata={}, created_at=1))
        connection.execute(runs.insert().values(id=run_id, owner='admin', media_id='poll-media', status='QUEUED',
            request_key='disconnected', request_hash='fixture', options={}, model_hash='fixture',
            progress=0, attempts=0, token=0, created_at=1, updated_at=1))
    async def disconnected(self):
        return True
    monkeypatch.setattr(Request, 'is_disconnected', disconnected)
    started = time.monotonic()
    response = client.get(f'/api/v1/analysis-runs/{run_id}/wait?since=1&timeout=30')
    assert response.status_code == 204
    assert time.monotonic() - started < 1


def test_server_and_client_evidence_gaps_protect_pass_and_finish_is_idempotent(authenticated):
    _, app, client = authenticated
    service = app.state.screw_realtime
    original = {'status': 'PASS', 'label': '通过', 'reason': 'all targets observed'}
    def predict(payload, *, stream_key, frame_time):
        service.registry.get(stream_key, frame_time, ('test',))
        return {'compliance': original, 'frameTime': frame_time}
    service.predict = predict
    service.finish = lambda key: {'final': True, 'compliance': original}
    headers = frame_headers()
    url = '/api/v1/realtime/frame?analysis_type=screw'
    assert client.post(url, content=b'frame', headers=headers).json()['compliance']['status'] == 'PASS'
    after_gap = client.post(url, content=b'frame', headers={**headers, 'X-Frame-Time': '4'}).json()
    assert after_gap['compliance']['status'] == 'REVIEW'
    assert after_gap['originalCompliance'] == original
    assert after_gap['evidenceGaps'] == [{'start': 0, 'end': 4, 'reason': '连续有效采样间隔超过3秒', 'source': 'server'}]
    finish_url = '/api/v1/realtime/streams/' + headers['X-Stream-Id'] + '/finish'
    body = {'evidenceGaps': [{'start': 6, 'end': 10, 'reason': '页面隐藏'}]}
    first = client.post(finish_url, json=body).json()
    repeated = client.post(finish_url, json=body).json()
    without_body = client.post(finish_url).json()
    assert first == repeated == without_body
    assert first['compliance']['status'] == 'REVIEW' and first['originalCompliance'] == original
    assert len(first['evidenceGaps']) == 2
    assert original['status'] == 'PASS', 'must retain the immutable original model verdict'


def test_evidence_gaps_cannot_override_fail_and_finish_gap_is_frozen():
    from backend.streams import StreamRegistry
    registry = StreamRegistry()
    key = ('owner', 'stream')
    stream = registry.get(key, 0, ())
    registry.record_sample(key, 0)
    stream['lastAcceptedAt'] -= 10
    raw = {'compliance': {'status': 'FAIL', 'label': '异常', 'reason': 'wrong order'}}
    first = registry.protect_result(key, raw, final=True)
    assert first['compliance'] == raw['compliance']
    assert first['evidenceGaps'][0]['source'] == 'server'
    assert 'originalCompliance' not in first
    assert registry.protect_result(key, raw, final=True) == first


@pytest.mark.parametrize('gaps', [
    [{'start': 2, 'end': 1, 'reason': 'reverse'}],
    [{'start': -1, 'end': 1, 'reason': 'negative'}],
    [{'start': 0, 'end': 1, 'reason': 'x'}]*101,
])
def test_finish_validates_evidence_gaps_before_mutating_cycle(authenticated, gaps):
    _, app, client = authenticated
    app.state.screw_realtime.finish = lambda key: pytest.fail('invalid evidence mutated cycle')
    response = client.post(f'/api/v1/realtime/streams/{uuid.uuid4()}/finish', json={'evidenceGaps': gaps})
    assert response.status_code == 422
