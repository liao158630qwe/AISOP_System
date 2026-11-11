"""Six-station isolation, stage dependencies and incomplete process specifications."""
import pytest
from sqlalchemy import select, update

from backend.db import pilot_cycles
from test_pilot import key, picture, manual

pytestmark = pytest.mark.usefixtures('pilot_registration_enabled')


def begin(client, station, **extra):
    return client.post('/api/v1/pilot/cycles', json=dict(
        request_id=key(), product='灯具多工位', part_number='SAME-PART', source='image', station=station, **extra))


def confirm(client, settings, row, stage, **extra):
    definition = next(item for item in row['config']['stages'] if item['id'] == stage)
    body = manual(settings, row, stage=stage, checked=[item['id'] for item in definition['items']], image=picture())
    body.update(extra)
    return client.post(f'/api/v1/pilot/cycles/{row["id"]}/stage', json=body)


@pytest.mark.parametrize('number,count,result', [(1, 8, 'MANUAL_OK'), (2, 5, 'MANUAL_OK'),
    (3, 3, 'REVIEW'), (4, 2, 'MANUAL_OK'), (5, 9, 'REVIEW'), (6, 7, 'REVIEW')])
def test_each_station_completes_its_own_manual_workflow(authenticated, number, count, result):
    settings, _, client = authenticated
    station = f'station-{number}'
    response = begin(client, station)
    assert response.status_code == 201, response.text
    row = response.json()['cycle']
    assert sum(len(item['items']) for item in row['config']['stages']) == count
    assert row['config']['model'] is None and row['station'] == station
    for definition in row['config']['stages']:
        response = confirm(client, settings, row, definition['id'])
        assert response.status_code == 200, response.text
        row = response.json()['cycle']
    response = client.post(f'/api/v1/pilot/cycles/{row["id"]}/finish', json=manual(settings, row))
    assert response.status_code == 200, response.text
    saved = response.json()['cycle']
    assert saved['result'] == result and saved['algorithm_result'] == 'NOT_EVALUATED'
    assert client.get('/api/v1/pilot/status', params={'station': station}).json()['active'] is None
    assert client.get('/api/v1/pilot/cycles', params={'station': station}).json()['total'] == 1
    for proof in saved['checks'].values():
        assert client.get(proof['evidence']['url']).status_code == 200
    if result == 'REVIEW':
        assert '工艺要求待确认' in saved['reason']
    assert client.get(f'/station-{number}', follow_redirects=False).headers['location'] == f'/?task=station{number}'


def test_stations_can_be_active_together_and_history_is_isolated(authenticated):
    _, _, client = authenticated
    rows = [begin(client, f'station-{number}').json()['cycle'] for number in range(1, 7)]
    for row in rows:
        station = row['station']
        assert begin(client, station).status_code == 409
        assert client.get('/api/v1/pilot/status', params={'station': station}).json()['active']['id'] == row['id']
        history = client.get('/api/v1/pilot/cycles', params={'station': station, 'q': 'SAME-PART'}).json()
        assert history['total'] == 1 and history['items'][0]['id'] == row['id']
    assert client.get('/api/v1/pilot/cycles').json()['items'][0]['station'] == 'station-1'


def test_station_five_requires_terminals_before_eight_screws(authenticated):
    settings, _, client = authenticated
    row = begin(client, 'station-5').json()['cycle']
    assert confirm(client, settings, row, 'SCREWS').status_code == 409
    row = confirm(client, settings, row, 'TERMINALS').json()['cycle']
    assert confirm(client, settings, row, 'SCREWS', checked=['S01'] * 8).status_code == 422
    assert confirm(client, settings, row, 'SCREWS', checked=['S01', 'S02', 'S03', 'S04']).status_code == 422
    assert confirm(client, settings, row, 'SCREWS').status_code == 200


@pytest.mark.parametrize('station,order', [('station-3', ['CLIPS', 'SCREWS']),
    ('station-6', ['SCREWS', 'TERMINALS', 'FRAME_AIR', 'HOUSING_AIR'])])
def test_independent_projects_do_not_invent_process_order(authenticated, station, order):
    settings, _, client = authenticated
    row = begin(client, station).json()['cycle']
    for phase in order:
        response = confirm(client, settings, row, phase)
        assert response.status_code == 200, response.text
        row = response.json()['cycle']
    assert row['stage'] == 'READY'


def test_no_cross_station_rework_or_unknown_configuration(authenticated):
    settings, _, client = authenticated
    row = begin(client, 'station-2').json()['cycle']
    client.post(f'/api/v1/pilot/cycles/{row["id"]}/close', json=manual(settings, row, outcome='INTERRUPTED'))
    assert begin(client, 'station-3', parent_id=row['id']).status_code == 409
    new = begin(client, 'station-2', parent_id=row['id']).json()['cycle']
    assert new['parent_id'] == row['id'] and new['checks'] == {}
    assert begin(client, 'station-7').status_code == 422
    assert client.get('/api/v1/pilot/status?station=station-7').status_code == 422
    assert client.get('/api/v1/pilot/cycles?station=station-7').status_code == 422
    assert client.post(f'/api/v1/pilot/cycles/{new["id"]}/stage', json=manual(
        settings, new, stage='HOLES', checked=['H01'], image=picture())).status_code == 422


def test_legacy_station_one_frozen_configuration_still_works(authenticated):
    settings, app, client = authenticated
    row = begin(client, 'station-1').json()['cycle']
    legacy = {key: value for key, value in row['config'].items()
              if key in ('station', 'name', 'version', 'mode', 'model', 'holes', 'screws', 'final_evidence_max_age_seconds')}
    with app.state.engine.begin() as connection:
        connection.execute(update(pilot_cycles).where(pilot_cycles.c.id == row['id']).values(config=legacy))
    row = client.get('/api/v1/pilot/status').json()['active']
    for phase in ('HOLES', 'SCREWS'):
        row = confirm(client, settings, row, phase).json()['cycle']
    assert client.post(f'/api/v1/pilot/cycles/{row["id"]}/finish', json=manual(settings, row)).json()['cycle']['result'] == 'MANUAL_OK'
    with app.state.engine.connect() as connection:
        assert connection.execute(select(pilot_cycles.c.config).where(pilot_cycles.c.id == row['id'])).scalar_one() == legacy


def test_every_independent_final_proof_must_be_fresh(authenticated):
    settings, app, client = authenticated
    row = begin(client, 'station-6').json()['cycle']
    for definition in row['config']['stages']:
        row = confirm(client, settings, row, definition['id']).json()['cycle']
    with app.state.engine.begin() as connection:
        checks = row['checks']
        checks['HOUSING_AIR']['at'] -= 181
        connection.execute(update(pilot_cycles).where(pilot_cycles.c.id == row['id']).values(checks=checks))
    assert client.post(f'/api/v1/pilot/cycles/{row["id"]}/finish', json=manual(settings, row)).status_code == 409
    row = confirm(client, settings, row, 'HOUSING_AIR').json()['cycle']
    assert client.post(f'/api/v1/pilot/cycles/{row["id"]}/finish', json=manual(settings, row)).status_code == 200
