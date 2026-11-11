"""Station attribution and pagination against SQLite and isolated PostgreSQL."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url

from backend.app import create_app
from backend.config import Settings
from backend.db import alert_evidence, initialize, station_inspections


@pytest.fixture(params=['sqlite', 'postgres'])
def archive(request, tmp_path):
    admin = schema_name = None
    if request.param == 'postgres':
        postgres = os.getenv('TEST_POSTGRES_URL')
        if not postgres:
            pytest.skip('PostgreSQL service not configured')
        admin = create_engine(postgres)
        schema_name = 'test_' + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA {schema_name}'))
        url = make_url(postgres).update_query_dict({'options': f'-csearch_path={schema_name} -clock_timeout=5000'})
        url = url.render_as_string(hide_password=False)
    else:
        url = 'sqlite:///' + str(tmp_path / 'test.db')
    model = tmp_path / 'model.pt'
    model.write_bytes(b'test-model-not-for-inference')
    settings = Settings(url, tmp_path / 'data', 'admin', 'test-password-12345', model, 'test', False, pose_backend='yolo')
    app = create_app(settings)
    try:
        initialize(app.state.engine)
        with TestClient(app) as client:
            response = client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password})
            assert response.status_code == 200
            client.headers['X-CSRF-Token'] = response.json()['csrf']
            yield app, client
    finally:
        app.state.engine.dispose()
        if admin:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA {schema_name} CASCADE'))
            admin.dispose()


def record(app, station=None, *, owner='admin', stream=None, kind='sticker'):
    result = {'compliance': {'status': 'FAIL', 'reason': '分工位录像测试'}}
    if station is not None:
        result['stationId'] = station
    return app.state.evidence.observe(owner, 'test', stream or str(uuid.uuid4()), kind, 0, None, result, final=True)['id']


def listing(client, station, **params):
    response = client.get('/api/v1/alerts', params={'station_id': station, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_stations_do_not_mix_even_with_same_detector_and_owner(archive):
    app, client = archive
    expected = {f'station-{i}': record(app, f'station-{i}') for i in range(1, 7)}
    private = record(app, 'station-2', owner='other-owner')
    unassigned = {record(app), record(app, ''), record(app, 'station-99')}
    for station, identity in expected.items():
        rows = listing(client, station)['items']
        assert [row['id'] for row in rows] == [identity]
        assert rows[0]['station_id'] == station and rows[0]['station_name'] == '工位' + station[-1]
        assert 'details' not in rows[0]
        detail = client.get('/api/v1/alerts/' + identity).json()
        assert detail['station_id'] == station and detail['station_name'] == rows[0]['station_name']
    rows = listing(client, 'unassigned')['items']
    assert {row['id'] for row in rows} == unassigned
    assert all(row['station_id'] is None and row['station_name'] == '未关联工位' for row in rows)
    # Existing API clients may still omit the filter, but never see other owners.
    assert len(client.get('/api/v1/alerts').json()['items']) == 9
    assert client.get('/api/v1/alerts/' + private).status_code == 404
    assert client.get('/api/v1/alerts/' + private + '/video').status_code == 404


def test_station_filter_applies_before_cursor_pagination(archive):
    app, client = archive
    expected = set()
    for i in range(67):
        station = 'station-1' if i % 2 else 'station-2'
        identity = record(app, station)
        if station == 'station-2':
            expected.add(identity)
    record(app, 'station-2', kind='screw')
    with app.state.engine.begin() as connection:
        connection.execute(update(alert_evidence).values(created_at=1000))
    found, cursor = [], None
    for _ in range(10):
        page = listing(client, 'station-2', limit=7, analysis_type='sticker', **({'cursor': cursor} if cursor else {}))
        assert all(row['station_id'] == 'station-2' and row['analysis_type'] == 'sticker' for row in page['items'])
        found.extend(row['id'] for row in page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(found) == 34 and set(found) == expected
    assert found == sorted(expected, reverse=True)
    assert listing(client, 'station-6')['items'] == []


def test_old_stream_attribution_is_owner_scoped_and_frozen_context_wins(archive):
    app, client = archive
    streams = [str(uuid.uuid4()) for _ in range(4)]
    with app.state.engine.begin() as connection:
        for i, stream in enumerate(streams):
            connection.execute(station_inspections.insert().values(stream_id=stream, station_id='station-2',
                owner='other-owner' if i == 1 else 'admin', cycle_id=None, context={}, status='FINISHED',
                result={}, reason='', started_at=100, finished_at=101))
    legacy = record(app, stream=streams[0])
    foreign = record(app, stream=streams[1])
    frozen = record(app, 'station-3', stream=streams[2])
    offline = record(app, stream=streams[3])
    with app.state.engine.begin() as connection:
        connection.execute(update(alert_evidence).where(alert_evidence.c.id == offline).values(source_kind='run'))
    assert [row['id'] for row in listing(client, 'station-2')['items']] == [legacy]
    assert client.get('/api/v1/alerts/' + legacy).json()['station_id'] == 'station-2'
    assert [row['id'] for row in listing(client, 'station-3')['items']] == [frozen]
    assert {row['id'] for row in listing(client, 'unassigned')['items']} == {foreign, offline}
    # Older hashed source IDs remain attributable through their saved result.
    hashed = record(app, 'station-4', stream='a' * 64)
    assert [row['id'] for row in listing(client, 'station-4')['items']] == [hashed]


@pytest.mark.parametrize('station', ['station-0', 'station-7', 'all', 'station-2,station-3', ''])
def test_invalid_station_filter_is_rejected(archive, station):
    _, client = archive
    assert client.get('/api/v1/alerts', params={'station_id': station}).status_code == 422
