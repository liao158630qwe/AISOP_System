import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from backend.db import deployment_settings, digest, now, sessions, station_terminals

URL = '/api/v1/admin/deployment-settings'


def test_defaults_and_runtime_are_six_stations(authenticated):
    settings, app, client = authenticated
    data = client.get(URL).json()
    config = data['config']
    assert (config['stationCount'], config['videoChannels'], config['terminalCount']) == (6, 6, 6)
    assert len(config['cameras']) == len(config['terminals']) == 6
    assert len(config['switches']) == 4
    inventory = {item['id']: item for item in config['equipment']}
    assert len(inventory) == 9
    assert inventory['terminal']['quantity'] == 6
    assert inventory['lamp']['quantity'] == 6
    assert inventory['nvr']['model'] == 'DS-7716NX-K5/Pro'
    assert inventory['core']['model'] == 'DS-3E6603'
    assert config['server']['memoryOriginal'] == '16G×64G'
    assert config['server']['memoryConfirmedGb'] is None
    assert config['nvr']['retentionDays'] == 30
    assert config['nvr']['usableStorageTb'] is None
    assert config['nvr']['diskCount'] is None and config['nvr']['diskCapacityTb'] is None
    assert config['nvr']['purpose'] == '保存相机视频流、AI 告警取证录像'
    assert config['nvr']['location'] == '' and config['nvr']['evidenceRetentionDays'] == 30
    assert all(not c['host'] and not c['rtspUrl'] for c in config['cameras'])
    assert [c['stationId'] for c in config['cameras']] == [f'station-{n}' for n in range(1, 7)]
    assert data['revision'] == 0 and data['updatedAt'] is None
    assert data['runtime']['noTargetTimeoutSeconds'] == settings.realtime_no_target_seconds
    assert data['runtime']['maxDurationSeconds'] == settings.realtime_max_duration_seconds
    assert data['runtime']['evidenceStorage'] == dict(location=str(settings.data_dir.resolve() / 'evidence'),
        retentionDays=settings.evidence_retention_seconds / 86400)
    assert not any(data['runtime'][k] for k in ('nvrIntegrated', 'modbusConnected', 'crossStationGate', 'performanceVerified'))
    with app.state.engine.connect() as connection:
        assert connection.execute(select(deployment_settings)).first() is None


def test_shared_persistence_conflicts_and_retry(authenticated):
    _, app, client = authenticated
    config = client.get(URL).json()['config']
    config['server']['host'] = '192.0.2.10'
    config['cameras'][0].update(rtspUrl='rtsp://192.0.2.11:554/Streaming/Channels/101', poeId='poe-1', poePort=1)
    config['nvr'].update(host='192.0.2.20', location='车间弱电机柜', diskCount=2, diskCapacityTb=8,
        usableStorageTb=14, bitrateMbps=4, evidenceRetentionDays=60)
    body = dict(revision=0, config=config)
    saved = client.put(URL, json=body)
    assert saved.status_code == 200, saved.text
    first = saved.json()
    assert first['revision'] == 1 and first['updatedAt'] > 0
    assert client.put(URL, json=body).json() == first  # lost response retry
    with TestClient(app) as other:
        other.cookies.update(client.cookies)
        assert other.get(URL).json() == first
    config['notes'] = 'other edit'
    conflict = client.put(URL, json=body)
    assert conflict.status_code == 409
    body['revision'] = 1
    assert client.put(URL, json=body).json()['revision'] == 2
    with app.state.engine.connect() as connection:
        assert connection.execute(select(station_terminals)).first() is None
        row = connection.execute(select(deployment_settings)).mappings().one()
        assert row['config'] == config


def test_legacy_record_gains_storage_defaults_without_overwriting_saved_values(authenticated):
    settings, app, client = authenticated
    config = client.get(URL).json()['config']
    config['nvr'].update(host='192.0.2.30', retentionDays=45, usableStorageTb=16, bitrateMbps=4)
    config['notes'] = '已有现场记录'
    assert client.put(URL, json=dict(revision=0, config=config)).status_code == 200
    for name in ('location', 'purpose', 'diskCount', 'diskCapacityTb', 'evidenceRetentionDays'):
        config['nvr'].pop(name)
    with app.state.engine.begin() as connection:
        connection.execute(update(deployment_settings).values(config=config))
        original = dict(connection.execute(select(deployment_settings)).mappings().one())
    loaded = client.get(URL).json()
    assert loaded['revision'] == original['revision'] == 1
    assert loaded['updatedAt'] == original['updated_at']
    assert loaded['config']['notes'] == '已有现场记录'
    assert loaded['config']['nvr']['host'] == '192.0.2.30'
    assert loaded['config']['nvr']['retentionDays'] == 45
    assert loaded['config']['nvr']['usableStorageTb'] == 16
    assert loaded['config']['nvr']['diskCount'] is None
    assert loaded['config']['nvr']['evidenceRetentionDays'] == 30
    assert loaded['runtime']['evidenceStorage']['retentionDays'] == settings.evidence_retention_seconds / 86400
    with app.state.engine.connect() as connection:
        assert dict(connection.execute(select(deployment_settings)).mappings().one()) == original
    body = dict(revision=1, config=loaded['config'])
    saved = client.put(URL, json=body)
    assert saved.status_code == 200 and saved.json()['revision'] == 2
    assert client.put(URL, json=body).json() == saved.json()
    with app.state.engine.connect() as connection:
        assert connection.execute(select(deployment_settings.c.config)).scalar_one() == loaded['config']


def test_owner_isolation(authenticated):
    _, app, client = authenticated
    config = client.get(URL).json()['config']
    config['notes'] = 'owner A'
    assert client.put(URL, json=dict(revision=0, config=config)).status_code == 200
    with app.state.engine.begin() as connection:
        connection.execute(sessions.insert().values(token_hash=digest('owner-b-token'), owner='owner-b', csrf='owner-b-csrf', expires_at=now(connection)+3600))
    with TestClient(app) as other:
        other.cookies.set('aisop_session', 'owner-b-token')
        other.headers['X-CSRF-Token'] = 'owner-b-csrf'
        own = other.get(URL).json()
        assert own['revision'] == 0 and own['config']['notes'] == ''
        assert other.put(URL, json=dict(revision=0, config=own['config'])).status_code == 200
    assert client.get(URL).json()['config']['notes'] == 'owner A'


@pytest.mark.parametrize('path,value', [
    (('stationCount',), 15), (('videoChannels',), 15), (('terminalCount',), 15),
    (('server','host'), '999.999.0.1'), (('network','subnet'), 'not-a-subnet'),
    (('server','apiUrl'), 'http://user:secret@server/'),
    (('cameras',0,'rtspUrl'), 'rtsp://user:secret@camera/stream'),
    (('cameras',0,'rtspUrl'), 'rtsp://camera/stream?token=secret'),
    (('cameras',0,'rtspUrl'), 'https://camera/stream'),
    (('cameras',0,'rtspUrl'), 'rtsp://camera:99999/stream'),
    (('cameras',1,'id'), 'cam-01'), (('cameras',1,'stationId'), 'station-1'),
    (('cameras',1,'sourceEnv'), 'AISOP_CAMERA_SOURCE_1'),
    (('cameras',1,'nvrChannel'), 1), (('cameras',0,'poePort'), 1),
    (('terminals',1,'id'), 'pc-01'), (('nvr','retentionDays'), 29),
    (('nvr','evidenceRetentionDays'), 29), (('nvr','diskCount'), 0),
    (('nvr','diskCount'), 1.5), (('nvr','diskCapacityTb'), 0), (('nvr','purpose'), ''),
    (('equipment',1,'quantity'), 15), (('equipment',3,'quantity'), 5),
])
def test_rejects_invalid_mappings_and_secrets(authenticated, path, value):
    _, _, client = authenticated
    config = client.get(URL).json()['config']
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    response = client.put(URL, json=dict(revision=0, config=config))
    assert response.status_code == 422, response.text
    assert 'secret' not in response.text
    assert client.get(URL).json()['revision'] == 0


def test_duplicate_ports(authenticated):
    _, _, client = authenticated
    config = client.get(URL).json()['config']
    for camera in config['cameras'][:2]:
        camera.update(poeId='poe-1', poePort=1)
    assert client.put(URL, json=dict(revision=0, config=config)).status_code == 422


def test_requires_admin_and_csrf(authenticated):
    _, app, client = authenticated
    body = dict(revision=0, config=client.get(URL).json()['config'])
    with TestClient(app) as guest:
        assert guest.get(URL).status_code == 401
        assert guest.put(URL, json=body).status_code == 401
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.put(URL, json=body).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    registered = client.put('/api/v1/admin/terminals/station-1', json=dict(workstationId='pc-01', cameraId='cam-01', analysisType='pose'))
    assert registered.status_code == 200
    with TestClient(app) as terminal:
        key = registered.json()['credential']
        headers = {'X-Terminal-Key': key}
        assert terminal.get(URL, headers=headers).status_code == 403
        assert terminal.put(URL, json=body, headers=headers).status_code == 403
