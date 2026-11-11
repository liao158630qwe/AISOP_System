"""Default pause blocks new workpieces while preserving history and detection."""
import uuid

from backend.config import Settings
from test_station_terminals import register, begin, send, fake_model


def payload(number=1):
    return dict(request_id=str(uuid.uuid4()), station=f'station-{number}',
                product='test', part_number='test-part', source='image')


def test_registration_is_paused_for_all_six_stations(authenticated):
    settings, app, client = authenticated
    assert settings.pilot_registration_enabled is False
    catalog = client.get('/api/v1/pilot/stations').json()['items']
    assert len(catalog) == 6 and all(not c['registration_enabled'] for c in catalog)
    for number in range(1, 7):
        result = client.post('/api/v1/pilot/cycles', json=payload(number))
        assert result.status_code == 409 and '停用' in result.text
        assert client.get(f'/api/v1/pilot/cycles?station=station-{number}').json()['total'] == 0
        assert not client.get(f'/api/v1/pilot/status?station=station-{number}').json()['config']['registration_enabled']
    fake_model(app)
    key = register(client)['credential']
    stream = begin(client, key)
    assert send(client, key, stream).status_code == 200
    final = client.post(f"/api/v1/station-streams/{stream['streamId']}/finish", headers={'X-Terminal-Key': key})
    assert final.status_code == 200 and final.json()['archived']


def test_pause_preserves_existing_records_and_blocks_rework(authenticated):
    settings, app, client = authenticated
    app.state.pilot.registration_enabled = True
    body = payload()
    row = client.post('/api/v1/pilot/cycles', json=body).json()['cycle']
    app.state.pilot.registration_enabled = False
    # An already committed request remains retrievable; it cannot create a new row.
    assert client.post('/api/v1/pilot/cycles', json=body).json()['cycle'] == row
    assert client.get(f"/api/v1/pilot/cycles/{row['id']}").json()['cycle'] == row
    assert client.post('/api/v1/pilot/cycles', json=payload()).status_code == 409
    response = client.post(f"/api/v1/pilot/cycles/{row['id']}/close", json=dict(
        request_id=str(uuid.uuid4()), revision=row['revision'], review_password=settings.password,
        reason='停止旧轮次', outcome='INTERRUPTED'))
    assert response.status_code == 200
    assert client.post('/api/v1/pilot/cycles', json=dict(payload(), parent_id=row['id'])).status_code == 409
    assert client.get('/api/v1/pilot/cycles').json()['total'] == 1
    app.state.pilot.registration_enabled = True
    assert client.post('/api/v1/pilot/cycles', json=payload()).status_code == 201


def test_registration_environment_switch_defaults_off(authenticated, monkeypatch):
    settings, _, _ = authenticated
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.setenv('DATABASE_URL', settings.database_url)
    monkeypatch.setenv('APP_PASSWORD', settings.password)
    monkeypatch.delenv('PILOT_REGISTRATION_ENABLED', raising=False)
    assert not Settings.from_env().pilot_registration_enabled
    monkeypatch.setenv('PILOT_REGISTRATION_ENABLED', 'true')
    assert Settings.from_env().pilot_registration_enabled


def test_dashboard_admin_detection_records_source_without_workpiece(authenticated):
    _, app, client = authenticated
    fake_model(app)
    for number in range(1, 7):
        key = register(client, number)['credential']
        response = client.post(f'/api/v1/station-streams?station=station-{number}', json={
            'requestId': str(uuid.uuid4()), 'inputSource': 'replay'})
        assert response.status_code == 201, response.text
        stream = response.json()
        assert stream['stationId'] == f'station-{number}'
        assert send(client, key, stream).status_code == 200
        final = client.post(f"/api/v1/station-streams/{stream['streamId']}/finish")
        assert final.status_code == 200 and final.json()['archived']
        saved = client.get(f'/api/v1/stations/station-{number}/inspections').json()['items'][0]
        assert saved['context']['inputSource'] == 'replay'
        assert 'cycleId' not in saved['context'] and 'product' not in saved['context']
    assert client.get('/api/v1/pilot/cycles').json()['total'] == 0
    assert client.post('/api/v1/station-streams?station=station-1', json={
        'requestId': str(uuid.uuid4()), 'inputSource': 'untrusted-value'}).status_code == 422
