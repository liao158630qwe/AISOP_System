"""Station finish -> durable samples -> history -> real MP4 regressions.

Model verdicts are fixtures; station routes, SQL, encoding and playback bytes
are real and use only the temporary database supplied by authenticated.
"""
import uuid

import cv2
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from backend.db import alert_evidence, station_inspections
from test_evidence_storage import jpeg, row_for


@pytest.fixture
def station_capture(authenticated, monkeypatch):
    settings, app, client = authenticated

    def start(kind='sticker', verdict='FAIL', live='REVIEW'):
        detector = {'sticker': app.state.sticker_realtime, 'screw': app.state.screw_realtime,
                    'pose': app.state.realtime}[kind]
        registry = detector.streams if kind == 'pose' else detector.registry
        calls = []

        def predict(payload, *, stream_key, frame_time):
            registry.get(stream_key, frame_time, ('station-evidence-test',))
            return dict(compliance=dict(status=live, label=live, reason='采样判定'))

        def finish(key):
            calls.append(key)
            return dict(final=True, compliance=dict(status=verdict, label=verdict,
                                                    reason='结束时未观察到动作：R01, R05'))

        monkeypatch.setattr(detector, 'predict', predict)
        if kind != 'pose':
            monkeypatch.setattr(detector, 'finish', finish)
        binding = client.put('/api/v1/admin/terminals/station-2', json=dict(
            workstationId='evidence-pc', cameraId='evidence-camera', analysisType=kind,
            sampleFps=4, maxSampleGapSeconds=3))
        assert binding.status_code == 200, binding.text
        headers = {'X-Terminal-Key': binding.json()['credential']}
        response = client.post('/api/v1/station-streams', headers=headers,
                               json={'requestId': str(uuid.uuid4())})
        assert response.status_code == 201, response.text
        stream = response.json()
        url = '/api/v1/station-streams/' + stream['streamId']
        for i in range(5):
            response = client.post(url + '/frames', content=jpeg(i * 45), headers={**headers,
                'Content-Type': 'image/jpeg', 'X-Frame-Seq': str(i), 'X-Frame-Time': str(i * .25),
                'X-Stream-Epoch': str(stream['streamEpoch'])})
            assert response.status_code == 200, response.text
        return url, headers, stream['streamId'], calls

    return settings, app, client, start


@pytest.mark.parametrize('kind', ['sticker', 'screw'])
@pytest.mark.parametrize('verdict', ['FAIL', 'REVIEW', 'PASS'])
def test_final_assessment_saves_before_release_and_links_playable_history(station_capture, kind, verdict):
    settings, app, client, start = station_capture
    url, headers, identity, calls = start(kind, verdict)
    store = app.state.evidence
    with app.state.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(alert_evidence)) == 0
    response = client.post(url + '/finish', headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['compliance']['status'] == verdict and result['archived']
    assert client.post(url + '/finish', headers=headers).json() == result
    assert len(calls) == 1
    assert client.delete(url, headers=headers).status_code == 200
    assert store.snapshot()['bufferBytes'] == 0
    detail_url = '/api/v1/board/history/' + identity
    detail = client.get(detail_url).json()
    if verdict == 'PASS':
        assert 'evidence' not in result and detail['evidence'] == []
        assert not store.process_one()
        return

    evidence_id = result['evidence']['id']
    assert result['evidence']['status'] == 'PENDING'
    assert [item['id'] for item in detail['evidence']] == [evidence_id]
    assert detail['evidence'][0]['video_url'] is None
    assert store.process_one() and not store.process_one()
    row = row_for(app.state.engine, evidence_id)
    assert row['source_id'] == identity and row['frame_count'] == 5
    assert row['severity'] == verdict and row['details']['result']['final']
    assert row['details']['result']['stationId'] == 'station-2'
    listing = client.get('/api/v1/alerts?station_id=station-2').json()['items']
    assert [item['id'] for item in listing] == [evidence_id]
    assert listing[0]['station_id'] == 'station-2' and listing[0]['station_name'] == '工位2'
    assert client.get('/api/v1/alerts?station_id=station-1').json()['items'] == []
    assert client.get('/api/v1/alerts?station_id=unassigned').json()['items'] == []
    evidence = client.get(detail_url).json()['evidence'][0]
    assert evidence['status'] == 'READY' and evidence['retain_until'] >= row['ready_at'] + 30 * 86400
    response = client.get(evidence['video_url'], headers={'Range': 'bytes=0-63'})
    assert response.status_code == 206 and b'ftyp' in response.content
    capture = cv2.VideoCapture(str(settings.data_dir / row['video_path']))
    decoded = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            decoded.append(float(frame.mean()))
    finally:
        capture.release()
    assert len(decoded) >= 5 and decoded[-1] - decoded[0] > 150
    reviewed = client.post(url + '/manual-review', json=dict(
        requestId=str(uuid.uuid4()), outcome='FAIL', reason='现场确认'))
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()['evidence']['id'] == evidence_id
    assert client.get(detail_url).json()['evidence'] == [evidence]


@pytest.mark.parametrize('failure', ['disk', 'insert-ack', 'inspection-archive'])
def test_failed_finish_retains_samples_and_retries_once(station_capture, monkeypatch, failure):
    _, app, client, start = station_capture
    url, headers, identity, calls = start()
    store, service = app.state.evidence, app.state.terminals
    with monkeypatch.context() as fault:
        if failure == 'disk':
            def unavailable(*args):
                raise OSError('test disk unavailable')
            fault.setattr('backend.evidence.durable_write', unavailable)
        elif failure == 'insert-ack':
            original = store._insert
            def unavailable(row):
                original(row)
                raise OperationalError('test commit acknowledgement lost', {}, Exception('offline'))
            fault.setattr(store, '_insert', unavailable)
        else:
            def unavailable(*args):
                raise HTTPException(503, 'test inspection archive unavailable')
            fault.setattr(service, 'archive', unavailable)
        response = client.post(url + '/finish', headers=headers)
        assert response.status_code == 503, response.text
        if failure != 'inspection-archive':
            assert '告警录像保存失败' in response.json()['message']

    state = service.streams['station-2']
    assert state['finalizing'] and not state.get('finalResult')
    assert store.snapshot()['bufferBytes'] > 0
    with app.state.engine.connect() as c:
        assert c.scalar(select(station_inspections.c.status).where(
            station_inspections.c.stream_id == identity)) == 'RUNNING'
    state['lastAt'] -= 20  # A storage retry must not alter the frozen verdict/gaps.
    result = client.post(url + '/finish', headers=headers)
    assert result.status_code == 200, result.text
    result = result.json()
    assert result['compliance']['status'] == 'FAIL' and result['stationEvidenceGaps'] == []
    assert client.post(url + '/finish', headers=headers).json() == result and len(calls) == 1
    with app.state.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(alert_evidence)) == 1
    assert client.delete(url, headers=headers).status_code == 200
    assert store.process_one()
    assert row_for(app.state.engine, result['evidence']['id'])['frame_count'] == 5
    assert store.snapshot()['bufferBytes'] == 0


def test_intermediate_failure_stays_linked_when_final_passes(station_capture):
    _, app, client, start = station_capture
    url, headers, identity, _ = start(verdict='PASS', live='FAIL')
    result = client.post(url + '/finish', headers=headers)
    assert result.status_code == 200 and result.json()['compliance']['status'] == 'PASS'
    assert client.delete(url, headers=headers).status_code == 200
    assert app.state.evidence.process_one()
    evidence = client.get('/api/v1/board/history/' + identity).json()['evidence']
    assert len(evidence) == 1 and evidence[0]['status'] == 'READY' and evidence[0]['severity'] == 'FAIL'


def test_pose_observation_does_not_create_process_alarm(station_capture):
    _, app, client, start = station_capture
    url, headers, identity, _ = start(kind='pose')
    result = client.post(url + '/finish', headers=headers)
    assert result.status_code == 200 and result.json()['compliance']['status'] == 'REVIEW'
    assert 'evidence' not in result.json()
    assert client.delete(url, headers=headers).status_code == 200
    assert client.get('/api/v1/board/history/' + identity).json()['evidence'] == []
    assert not app.state.evidence.process_one()
