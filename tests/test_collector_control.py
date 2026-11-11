"""Real SQL/HTTP command recovery; model fixtures do not claim inference accuracy."""
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from backend.app import create_app
from backend.db import station_collectors, station_collector_commands, station_inspections
from test_station_terminals import register, fake_model, send


BASE = '/api/v1/stations/station-1/collector'


@pytest.fixture
def controlled(authenticated):
    settings, app, client = authenticated
    registered = register(client)
    fake_model(app)
    instance = str(uuid.uuid4())
    headers = {'X-Terminal-Key': registered['credential'], 'X-Collector-Instance': instance}
    result = client.post(BASE + '/heartbeat', json=dict(instanceId=instance, state='IDLE'), headers=headers)
    assert result.status_code == 200, result.text
    return settings, app, client, headers


def command(client, action='start', stream=None, request=None, reason=''):
    payload = dict(requestId=request or str(uuid.uuid4()), action=action, streamId=stream, reason=reason)
    result = client.post(BASE + '/commands', json=payload)
    assert result.status_code == 200, result.text
    return result.json()['command'], payload


def start(client, headers):
    requested, payload = command(client)
    # A new command can be polled while the last archived/interrupted record is retained.
    polled = client.post(BASE + '/heartbeat', headers=headers,
        json=dict(instanceId=headers['X-Collector-Instance'], state='IDLE'))
    assert polled.status_code == 200 and polled.json()['command']['status'] == 'PENDING', polled.text
    active = dict(headers, **{'X-Collector-Command': requested['requestId']})
    result = client.post('/api/v1/station-streams', headers=active, json={
        'requestId': requested['requestId'], 'inputSource': 'device'})
    assert result.status_code == 201, result.text
    return result.json(), requested, payload, active


def acknowledge(client, headers, requested, stream, status='SUCCEEDED'):
    return client.post(BASE + '/commands/' + requested['requestId'] + '/ack', headers=headers,
        json=dict(instanceId=headers['X-Collector-Instance'], streamId=stream, status=status, error=''))


def test_page_refresh_is_observation_and_cannot_release_background_round(controlled):
    _, app, client, headers = controlled
    stream, requested, _, _ = start(client, headers)
    assert acknowledge(client, headers, requested, stream['streamId']).status_code == 200
    assert send(client, headers['X-Terminal-Key'], stream, **{'X-Collector-Instance': headers['X-Collector-Instance']}).status_code == 200
    for _ in range(3):
        status = client.get(BASE).json()
        assert status['state'] == 'RUNNING' and status['stream']['streamId'] == stream['streamId']
    # A page cannot end the process-owned stream even if it knows its identity.
    assert client.delete('/api/v1/station-streams/' + stream['streamId']).status_code == 403
    assert send(client, headers['X-Terminal-Key'], stream, seq=1, stamp=.2).status_code == 409
    assert send(client, headers['X-Terminal-Key'], stream, seq=1, stamp=.2,
        **{'X-Collector-Instance': headers['X-Collector-Instance']}).status_code == 200
    with app.state.engine.connect() as c:
        assert c.execute(select(station_inspections.c.status)).scalar_one() == 'RUNNING'


def test_lost_start_response_uses_original_stream_and_cannot_reopen_finished_piece(controlled):
    _, app, client, headers = controlled
    stream, requested, payload, active = start(client, headers)
    status = client.get(BASE).json()
    assert status['command']['assignedStreamId'] == stream['streamId']
    assert status['stream'] == stream
    retry = client.post('/api/v1/station-streams', headers=active,
        json={'requestId': requested['requestId'], 'inputSource': 'device'})
    assert retry.status_code == 201 and retry.json() == stream
    assert acknowledge(client, headers, requested, stream['streamId']).status_code == 200
    assert client.post(BASE + '/commands', json=payload).json()['command']['status'] == 'SUCCEEDED'
    stop, _ = command(client, 'stop', stream['streamId'], reason='换件前中断')
    stop_headers = dict(headers, **{'X-Collector-Command': stop['requestId']})
    assert client.request('DELETE', '/api/v1/station-streams/' + stream['streamId'], headers=stop_headers,
        json=dict(reason='换件前中断')).status_code == 200
    assert acknowledge(client, headers, stop, stream['streamId']).json()['command']['status'] == 'SUCCEEDED'
    assert client.post('/api/v1/station-streams', headers=active,
        json={'requestId': requested['requestId'], 'inputSource': 'device'}).status_code == 409
    assert client.get(BASE + '/commands/' + requested['requestId']).json()['status'] == 'SUCCEEDED'
    with app.state.engine.connect() as c:
        assert len(c.execute(select(station_inspections)).all()) == 1


def test_finish_requires_durable_archive_and_release_before_ack(controlled):
    _, _, client, headers = controlled
    stream, requested, _, _ = start(client, headers)
    assert acknowledge(client, headers, requested, stream['streamId']).status_code == 200
    assert send(client, headers['X-Terminal-Key'], stream,
        **{'X-Collector-Instance': headers['X-Collector-Instance']}).status_code == 200
    requested, _ = command(client, 'finish', stream['streamId'])
    active = dict(headers, **{'X-Collector-Command': requested['requestId']})
    assert client.get(BASE).json()['state'] == 'FINISHING'
    assert send(client, headers['X-Terminal-Key'], stream, seq=1, stamp=.2,
        **{'X-Collector-Instance': headers['X-Collector-Instance']}).status_code == 429
    assert acknowledge(client, headers, requested, stream['streamId']).status_code == 409
    final = client.post('/api/v1/station-streams/' + stream['streamId'] + '/finish', headers=active)
    assert final.status_code == 200 and final.json()['archived'], final.text
    assert client.post('/api/v1/station-streams/' + stream['streamId'] + '/finish', headers=active).json() == final.json()
    assert acknowledge(client, headers, requested, stream['streamId']).status_code == 409
    assert client.delete('/api/v1/station-streams/' + stream['streamId'], headers=active).status_code == 200
    receipt = acknowledge(client, headers, requested, stream['streamId'])
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()['state'] == 'IDLE' and receipt.json()['record']['status'] == 'FINISHED'
    assert acknowledge(client, headers, requested, stream['streamId']).json()['command']['status'] == 'SUCCEEDED'
    next_stream, _, _, _ = start(client, headers)
    assert next_stream['streamId'] != stream['streamId']


def test_only_one_collector_and_one_pending_command_are_allowed(controlled):
    _, app, client, headers = controlled
    assert client.post(BASE + '/heartbeat', headers=headers,
        json=dict(instanceId=str(uuid.uuid4()))).status_code == 409
    assert client.post(BASE + '/heartbeat', json=dict(instanceId=headers['X-Collector-Instance'])).status_code == 403
    payloads = [dict(requestId=str(uuid.uuid4()), action='start') for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda payload: client.post(BASE + '/commands', json=payload), payloads))
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(result.json()['command'] for result in results if result.status_code == 200)
    assert client.post(BASE + '/commands', json=dict(requestId=winner['requestId'], action='start', reason='changed')).status_code == 409
    assert client.post('/api/v1/station-streams?station=station-1', json=dict(requestId=str(uuid.uuid4()))).status_code == 409
    with app.state.engine.connect() as c:
        assert len(c.execute(select(station_collector_commands)).all()) == 1


@pytest.mark.parametrize('failure', ['expired', 'runtime', 'binding'])
def test_expired_or_changed_control_never_replays_old_start(controlled, failure):
    _, app, client, headers = controlled
    requested, payload = command(client)
    active = dict(headers, **{'X-Collector-Command': requested['requestId']})
    with app.state.engine.begin() as c:
        values = {'expires_at': time.time()-1} if failure == 'expired' else (
            {'runtime_id': str(uuid.uuid4())} if failure == 'runtime' else {'binding_revision': 999})
        c.execute(update(station_collectors).values(**values))
    status = client.get(BASE).json()
    assert not status['online'] and status['command']['status'] == 'FAILED'
    assert client.post(BASE + '/heartbeat', headers=headers,
        json=dict(instanceId=headers['X-Collector-Instance'], state='IDLE')).status_code == 200
    assert client.post('/api/v1/station-streams', headers=active,
        json=dict(requestId=requested['requestId'], inputSource='device')).status_code == 409
    assert client.post(BASE + '/commands', json=payload).json()['command']['status'] == 'FAILED'


def test_process_failure_interrupts_round_and_preserves_reason(controlled):
    _, _, client, headers = controlled
    stream, requested, _, _ = start(client, headers)
    assert acknowledge(client, headers, requested, stream['streamId']).status_code == 200
    response = client.post(BASE + '/heartbeat', headers=headers, json=dict(
        instanceId=headers['X-Collector-Instance'], streamId=stream['streamId'], state='ERROR', error='相机断开'))
    assert response.status_code == 200, response.text
    assert response.json()['record']['status'] == 'INTERRUPTED'
    assert response.json()['record']['reason'] == '相机断开'
    assert client.post(BASE + '/commands', json=dict(requestId=str(uuid.uuid4()), action='finish', streamId=stream['streamId'])).status_code == 409
    assert client.post(BASE + '/heartbeat', headers=headers, json=dict(instanceId=headers['X-Collector-Instance'], state='IDLE')).status_code == 200
    next_stream, _, _, _ = start(client, headers)
    assert next_stream['streamId'] != stream['streamId']


def test_real_api_restart_interrupts_old_round_and_keeps_command_receipt(controlled):
    settings, _, client, headers = controlled
    stream, requested, payload, _ = start(client, headers)
    restarted = create_app(settings)
    with TestClient(restarted) as other:
        status = other.get(BASE, headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()['record']['status'] == 'INTERRUPTED'
        assert status.json()['command']['status'] == 'FAILED'
        assert other.post(BASE + '/heartbeat', headers=headers, json=dict(
            instanceId=headers['X-Collector-Instance'], streamId=stream['streamId'], state='RUNNING')).status_code == 409
        assert other.post(BASE + '/heartbeat', headers=headers, json=dict(instanceId=str(uuid.uuid4()), state='IDLE')).status_code == 200
        assert other.get(BASE + '/commands/' + requested['requestId'], headers=headers).json()['status'] == 'FAILED'
    restarted.state.engine.dispose()


def test_cross_station_key_cannot_observe_or_ack_commands(controlled):
    _, _, client, _ = controlled
    requested, _ = command(client)
    credential = register(client, number=2)['credential']
    wrong = {'X-Terminal-Key': credential}
    for url in (BASE, BASE + '/commands/' + requested['requestId']):
        assert client.get(url, headers=wrong).status_code == 403
    assert client.post(BASE + '/commands/' + requested['requestId'] + '/ack', headers=wrong,
        json=dict(instanceId=requested['instanceId'], status='SUCCEEDED')).status_code == 403


@pytest.mark.parametrize('field,bad', [('expectedInstanceId', str(uuid.uuid4())),
    ('expectedRuntimeId', str(uuid.uuid4())), ('expectedBindingRevision', 999)])
def test_never_delivered_old_command_cannot_target_a_changed_collector(controlled, field, bad):
    _, app, client, headers = controlled
    payload = dict(requestId=str(uuid.uuid4()), action='start', expectedInstanceId=headers['X-Collector-Instance'],
        expectedRuntimeId=app.state.terminals.runtime_id, expectedBindingRevision=1)
    assert client.post(BASE + '/commands', json=dict(payload, **{field: bad})).status_code == 409
    assert client.get(BASE + '/commands/' + payload['requestId']).status_code == 404
    created = client.post(BASE + '/commands', json=payload)
    assert created.status_code == 200, created.text
    with app.state.engine.begin() as c:
        c.execute(update(station_collectors).values(expires_at=0))
    assert client.get(BASE).json()['command']['status'] == 'FAILED'
    # The original request remains readable after the process changes; it does not run again.
    receipt = client.post(BASE + '/commands', json=payload)
    assert receipt.status_code == 200 and receipt.json()['command']['status'] == 'FAILED'


def test_failure_during_evidence_io_cannot_publish_the_late_pass(controlled):
    _, app, client, headers = controlled
    stream, requested, _, _ = start(client, headers)
    assert acknowledge(client, headers, requested, stream['streamId']).status_code == 200
    entered, release = threading.Event(), threading.Event()
    def evidence(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return None
    app.state.evidence.observe = evidence
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(send, client, headers['X-Terminal-Key'], stream,
            **{'X-Collector-Instance': headers['X-Collector-Instance']})
        try:
            assert entered.wait(5)
            failed = client.post(BASE + '/heartbeat', headers=headers, json=dict(
                instanceId=headers['X-Collector-Instance'], streamId=stream['streamId'], state='ERROR', error='相机断开'))
            assert failed.status_code == 200 and failed.json()['record']['status'] == 'INTERRUPTED'
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 409
    state = app.state.terminals.streams['station-1']
    assert state['result'] is None and state['preview'] is None
    assert client.get('/api/v1/stations/station-1/live-status').json()['result'] is None


def test_control_request_cannot_switch_operator_during_submission(controlled):
    settings, _, client, _ = controlled
    actor = dict(account=settings.username, role='admin', reviewer=settings.username, credentialRevision=None)
    payload = dict(requestId=str(uuid.uuid4()), action='start', expectedActor=dict(actor, reviewer='other-operator'))
    assert client.post(BASE + '/commands', json=payload).status_code == 409
    assert client.get(BASE + '/commands/' + payload['requestId']).status_code == 404
    payload['expectedActor'] = actor
    response = client.post(BASE + '/commands', json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['command']['actor'] == actor
