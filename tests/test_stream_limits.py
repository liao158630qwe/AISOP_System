"""Round deadlines cannot be extended by uploads, stale evidence, or client clocks."""
from dataclasses import replace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.db import station_inspections, station_stream_leases
from backend.stream_limits import StreamLimits, target_present


@pytest.mark.parametrize('kind,result,present', [
    ('screw', {'inspection': {'registered': False}, 'people': [{}],
               'aggregateTargets': [{'state': 'PRESENT'}]}, False),
    ('sticker', {'inspection': {'registered': False}, 'targets': [{'state': 'ACTION_DONE'}]}, False),
    ('screw', {'inspection': {'registered': True, 'targets': [{'state': 'EMPTY'}]}}, True),
    ('sticker', {'inspection': {'registered': True}}, True),
    ('pose', {'people': []}, False),
    ('pose', {'people': [{}]}, True),
    ('scene', {'checks': [{'count': 0}], 'observations': [{}]}, False),
    ('scene', {'checks': [{'count': 1}]}, True),
])
def test_presence_uses_current_task_evidence(kind, result, present):
    assert target_present(kind, result) is present


def test_idle_resets_on_target_but_total_round_never_resets():
    guard = StreamLimits(clock=0)
    guard.observe('pose', {'people': []}, clock=59)
    guard.observe('pose', {'people': [{}]}, clock=59.5)
    guard.check(clock=119)
    with pytest.raises(HTTPException, match='60 秒') as error:
        guard.check(clock=119.5)
    assert error.value.status_code == 410
    with pytest.raises(HTTPException):
        guard.observe('pose', {'people': [{}]}, clock=120)
    active = StreamLimits(clock=0)
    for second in range(1, 600):
        active.observe('pose', {'people': [{}]}, clock=second)
    with pytest.raises(HTTPException, match='600 秒'):
        active.observe('pose', {'people': [{}]}, clock=600)


def start_station(client):
    response = client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='limit-pc', cameraId='limit-camera', analysisType='pose'))
    assert response.status_code == 200, response.text
    response = client.post('/api/v1/station-streams?station=station-1',
                           json={'requestId': str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    return response.json()


def frame(client, identity, sequence):
    # Deliberately keep the client's elapsed timestamp tiny.
    return client.post('/api/v1/station-streams/' + identity['streamId'] + '/frames',
        content=b'fixture', headers={'Content-Type': 'image/jpeg', 'X-Frame-Seq': str(sequence),
        'X-Frame-Time': str(sequence / 1000), 'X-Stream-Epoch': str(identity['streamEpoch'])})


@pytest.mark.parametrize('cause', ['empty', 'maximum'])
def test_station_timeout_interrupts_releases_and_requires_new_round(authenticated, cause):
    _, app, client = authenticated
    calls = []
    def predict(payload, *, stream_key, frame_time):
        calls.append(frame_time)
        app.state.realtime.streams.get(stream_key, frame_time, ('test',))
        # A historical PASS must never be archived as a successful timed-out round.
        return dict(people=[] if cause == 'empty' else [{}], compliance=dict(status='PASS'))
    app.state.realtime.predict = predict
    app.state.evidence.observe = lambda *args, **kwargs: None
    identity = start_station(client)
    assert identity['streamLimits'] == dict(maxDurationSeconds=600, noTargetTimeoutSeconds=60)
    assert frame(client, identity, 0).status_code == 200
    state = app.state.terminals.streams['station-1']
    guard = state['limits']
    if cause == 'empty':
        guard.last_target -= 61
    else:
        guard.started -= 601
    response = frame(client, identity, 1)
    assert response.status_code == 410, response.text
    assert ('60 秒' if cause == 'empty' else '600 秒') in response.text
    assert len(calls) == 1, 'An expired round must not start another inference'
    assert state['finished'] and state['preview'] is None and state['result'] is None
    assert state['key'] not in app.state.realtime.streams.items
    assert frame(client, identity, 2).status_code == 410
    with app.state.engine.connect() as connection:
        record = connection.execute(select(station_inspections)).mappings().one()
        lease = connection.execute(select(station_stream_leases)).mappings().one()
    assert record['status'] == 'INTERRUPTED' and record['result'] is None
    assert '自动停止' in record['reason'] and lease['closed']
    live = client.get('/api/v1/stations/station-1/live-status').json()
    assert live['reasonCode'] == 'AUTO_STOPPED' and live['result'] is None
    assert client.post('/api/v1/station-streams/' + identity['streamId'] + '/finish').status_code == 409
    assert client.delete('/api/v1/station-streams/' + identity['streamId']).status_code == 200
    next_round = client.post('/api/v1/station-streams?station=station-1',
                            json={'requestId': str(uuid.uuid4())}).json()
    assert next_round['streamId'] != identity['streamId']
    assert frame(client, next_round, 0).status_code == 200


def test_legacy_stream_rejects_timeout_before_inference_and_protects_finish(authenticated):
    _, app, client = authenticated
    detector = app.state.screw_realtime
    keys = []
    def predict(payload, *, stream_key, frame_time):
        keys.append(stream_key)
        detector.registry.get(stream_key, frame_time, ('test',))
        return dict(inspection={'registered': False}, compliance={'status': 'PENDING'})
    detector.predict = predict
    detector.finish = lambda key: dict(final=True, compliance={'status': 'PASS'})
    app.state.evidence.observe = lambda *args, **kwargs: None
    identity = str(uuid.uuid4())
    def send(stamp):
        return client.post('/api/v1/realtime/frame?analysis_type=screw', content=b'fixture', headers={
            'Content-Type': 'image/jpeg', 'X-Stream-Id': identity, 'X-Frame-Time': str(stamp)})
    assert send(0).status_code == 200
    detector.registry.items[keys[0]]['limits'].last_target -= 61
    assert send(.1).status_code == 410
    assert len(keys) == 1
    final = client.post('/api/v1/realtime/streams/' + identity + '/finish').json()
    assert final['compliance']['status'] == 'REVIEW'
    assert '自动停止' in final['compliance']['reason']
    assert final['originalCompliance']['status'] == 'PASS'
    assert client.delete('/api/v1/realtime/streams/' + identity).status_code == 200
    assert keys[0] not in detector.registry.items


def test_finish_cannot_bypass_expired_station_round(authenticated):
    _, app, client = authenticated
    app.state.realtime.predict = lambda *args, **kwargs: dict(people=[{}], compliance={'status':'PASS'})
    app.state.evidence.observe = lambda *args, **kwargs: None
    identity = start_station(client)
    assert frame(client, identity, 0).status_code == 200
    app.state.terminals.streams['station-1']['limits'].started -= 601
    response = client.post('/api/v1/station-streams/' + identity['streamId'] + '/finish')
    assert response.status_code == 410
    with app.state.engine.connect() as connection:
        record = connection.execute(select(station_inspections)).mappings().one()
    assert record['status'] == 'INTERRUPTED' and record['result'] is None


@pytest.mark.parametrize('field,value', [
    ('realtime_max_duration_seconds', 0), ('realtime_no_target_seconds', -1),
    ('realtime_no_target_seconds', 86401), ('realtime_max_duration_seconds', 1.5),
])
def test_limits_cannot_be_disabled_or_nonfinite(context, field, value):
    settings, _, _ = context
    with pytest.raises(ValueError):
        replace(settings, **{field: value})
