"""Manual pilot invariants; use generated images, never pretend to test a model."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, update

from backend.app import create_app
from backend.db import initialize, pilot_cycles, pilot_events
from backend.pilot import Start

pytestmark = pytest.mark.usefixtures('pilot_registration_enabled')


def key():
    return str(uuid.uuid4())


def picture():
    stream = io.BytesIO()
    Image.new('RGB', (320, 240), '#425c78').save(stream, 'JPEG')
    return base64.b64encode(stream.getvalue()).decode()


def start(client, **values):
    return client.post('/api/v1/pilot/cycles', json=dict(request_id=key(), product='灯具A', part_number='P-001', source='image', **values))


def manual(settings, row, **values):
    return dict(request_id=key(), revision=row['revision'], review_password=settings.password, reason='现场逐项人工复检', **values)


def stage(client, settings, row, phase, **values):
    body = manual(settings, row, stage=phase, checked=[f'{"H" if phase == "HOLES" else "S"}{i:02}' for i in range(1, 5)], image=picture())
    body.update(values)
    return client.post(f'/api/v1/pilot/cycles/{row["id"]}/stage', json=body)


def ready(client, settings):
    row = start(client).json()['cycle']
    row = stage(client, settings, row, 'HOLES').json()['cycle']
    return stage(client, settings, row, 'SCREWS').json()['cycle']


def test_complete_persisted_manual_cycle_and_private_evidence(authenticated):
    settings, app, client = authenticated
    row = ready(client, settings)
    assert row['stage'] == 'READY' and row['result'] is None
    body = manual(settings, row)
    url = f'/api/v1/pilot/cycles/{row["id"]}/finish'
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert client.post(url, json=body).json() == response.json()
    done = response.json()['cycle']
    assert done['result'] == 'MANUAL_OK' and done['algorithm_result'] == 'NOT_EVALUATED'
    detail = client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()
    assert [event['kind'] for event in detail['events']] == ['START', 'STAGE', 'STAGE', 'FINISH']
    assert all(proof['source'] == 'manual' for proof in done['checks'].values())
    assert settings.password not in json.dumps(detail)
    image_url = done['checks']['HOLES']['evidence']['url']
    assert client.get(image_url).headers['content-type'] == 'image/jpeg'
    with app.state.engine.connect() as c:
        assert settings.password not in str(c.execute(select(pilot_events)).all())
    # Recreate the service with the same database: neither browser nor server owns the record.
    fresh = create_app(settings)
    with TestClient(fresh) as reader:
        reader.cookies.update(client.cookies)
        assert reader.get(f'/api/v1/pilot/cycles/{row["id"]}').json() == detail
    assert client.post('/api/v1/logout').status_code == 200
    assert client.get(image_url).status_code == 401


def test_phase_order_four_distinct_positions_and_duplicate_stage(authenticated):
    settings, _, client = authenticated
    row = start(client).json()['cycle']
    assert stage(client, settings, row, 'SCREWS').status_code == 409
    assert stage(client, settings, row, 'HOLES', checked=['H01'] * 4).status_code == 422
    assert client.post(f'/api/v1/pilot/cycles/{row["id"]}/finish', json=manual(settings, row)).status_code == 409
    body = manual(settings, row, stage='HOLES', checked=['H01', 'H02', 'H03', 'H04'], image=picture())
    url = f'/api/v1/pilot/cycles/{row["id"]}/stage'
    response = client.post(url, json=body)
    assert response.status_code == 200
    assert client.post(url, json=body).json() == response.json()
    changed = dict(body, reason='使用相同编号修改内容')
    assert client.post(url, json=changed).status_code == 409
    assert stage(client, settings, row, 'HOLES').status_code == 409
    assert len(client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()['events']) == 2


def test_start_idempotency_and_single_station_concurrency(authenticated):
    settings, app, client = authenticated
    bodies = [Start(request_id=uuid.uuid4(), product='A', source='image') for _ in range(2)]
    def create(body):
        try:
            return app.state.pilot.command(settings.username, body, 'START')
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, bodies))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert 409 in results
    index = next(i for i, result in enumerate(results) if isinstance(result, dict))
    assert create(bodies[index]) == results[index]
    assert client.get('/api/v1/pilot/cycles').json()['total'] == 1


def test_concurrent_stage_confirmations_keep_one_event_and_one_proof(authenticated):
    from backend.pilot import Stage
    settings, app, client = authenticated
    row = start(client).json()['cycle']
    def confirm(_):
        body = Stage(**manual(settings, row, stage='HOLES', checked=['H01', 'H02', 'H03', 'H04'], image=picture()))
        try:
            return app.state.pilot.command(settings.username, body, 'STAGE', row['id'])
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1 and 409 in results
    data = client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()
    assert len(data['events']) == 2 and data['cycle']['revision'] == 2
    assert len(list(app.state.pilot.root.rglob('*.jpg'))) == 1


def test_lost_database_commit_acknowledgement_preserves_saved_evidence(authenticated, monkeypatch):
    from sqlalchemy.exc import OperationalError
    settings, app, client = authenticated
    row = start(client).json()['cycle']
    original = app.state.engine.dialect.do_commit
    failed = False
    def commit(connection):
        nonlocal failed
        original(connection)
        if not failed:
            failed = True
            raise OperationalError('commit', {}, Exception('commit succeeded, acknowledgement lost'))
    monkeypatch.setattr(app.state.engine.dialect, 'do_commit', commit)
    result = stage(client, settings, row, 'HOLES')
    assert result.status_code == 200, result.text
    proof = result.json()['cycle']['checks']['HOLES']['evidence']['url']
    assert client.get(proof).status_code == 200
    assert len(client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()['events']) == 2


def test_reauth_csrf_and_read_isolation(authenticated):
    settings, app, client = authenticated
    row = start(client).json()['cycle']
    assert stage(client, settings, row, 'HOLES', review_password='wrong').status_code == 403
    assert stage(client, settings, row, 'HOLES', review_password='').status_code == 422
    csrf = client.headers.pop('X-CSRF-Token')
    assert stage(client, settings, row, 'HOLES').status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    with app.state.engine.connect() as c:
        with pytest.raises(HTTPException) as error:
            app.state.pilot._get(c, 'other-account', row['id'])
    assert error.value.status_code == 404
    assert client.get('/api/v1/pilot/cycles/' + key()).status_code == 404


def test_storage_failure_rolls_back_and_same_request_can_retry(authenticated, monkeypatch):
    settings, app, client = authenticated
    row = start(client).json()['cycle']
    body = manual(settings, row, stage='HOLES', checked=['H01', 'H02', 'H03', 'H04'], image=picture())
    write = app.state.pilot._write
    def fail(*args):
        raise OSError('test storage unavailable')
    monkeypatch.setattr(app.state.pilot, '_write', fail)
    url = f'/api/v1/pilot/cycles/{row["id"]}/stage'
    assert client.post(url, json=body).status_code == 503
    assert client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()['cycle']['stage'] == 'HOLES'
    monkeypatch.setattr(app.state.pilot, '_write', write)
    assert client.post(url, json=body).status_code == 200
    assert len(list(app.state.pilot.root.rglob('*.jpg'))) == 1


def test_event_commit_failure_does_not_advance_or_leave_proof(authenticated, monkeypatch):
    from sqlalchemy.engine import Connection
    from sqlalchemy.exc import OperationalError
    settings, app, client = authenticated
    row = start(client).json()['cycle']
    original = Connection.execute
    def execute(self, statement, *args, **kwargs):
        if getattr(statement, 'is_insert', False) and getattr(statement, 'table', None) is pilot_events:
            raise OperationalError('insert', {}, Exception('test database failure'))
        return original(self, statement, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Connection, 'execute', execute)
        assert stage(client, settings, row, 'HOLES').status_code == 503
    data = client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()
    assert data['cycle']['stage'] == 'HOLES' and len(data['events']) == 1
    assert not list(app.state.pilot.root.rglob('*.jpg'))


@pytest.mark.parametrize('invalid', ['not-an-image', base64.b64encode(b'hello').decode()])
def test_invalid_evidence_never_confirms(authenticated, invalid):
    settings, _, client = authenticated
    row = start(client).json()['cycle']
    assert stage(client, settings, row, 'HOLES', image=invalid).status_code == 422


def test_stale_or_missing_evidence_cannot_finish(authenticated):
    settings, app, client = authenticated
    row = ready(client, settings)
    with app.state.engine.begin() as c:
        checks = row['checks']
        checks['SCREWS']['at'] -= 181
        c.execute(update(pilot_cycles).where(pilot_cycles.c.id == row['id']).values(checks=checks))
    url = f'/api/v1/pilot/cycles/{row["id"]}/finish'
    assert client.post(url, json=manual(settings, row)).status_code == 409
    row = stage(client, settings, row, 'SCREWS').json()['cycle']
    assert row['stage'] == 'READY'
    app.state.pilot._file(row['id'], row['checks']['HOLES']['evidence']).unlink()
    assert client.post(url, json=manual(settings, row)).status_code == 409
    assert client.get('/api/v1/pilot/status').json()['active']['id'] == row['id']


def test_review_rework_and_next_cycle_never_copy_checks(authenticated):
    settings, _, client = authenticated
    row = start(client).json()['cycle']
    row = stage(client, settings, row, 'HOLES').json()['cycle']
    response = client.post(f'/api/v1/pilot/cycles/{row["id"]}/close', json=manual(settings, row, outcome='NG'))
    assert response.status_code == 200
    row = response.json()['cycle']
    assert client.get('/api/v1/pilot/status').json()['active'] is None
    response = client.post(f'/api/v1/pilot/cycles/{row["id"]}/review', json=manual(settings, row, disposition='needs_rework'))
    assert response.status_code == 200
    revised = response.json()['cycle']
    assert revised['result'] == 'NG' and revised['review_status'] == 'needs_rework'
    assert revised['checks'] == row['checks']
    new = start(client, parent_id=row['id']).json()['cycle']
    assert new['parent_id'] == row['id'] and new['id'] != row['id']
    assert new['checks'] == {} and new['stage'] == 'HOLES'
    assert client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()['rework_ids'] == [new['id']]
    assert stage(client, settings, row, 'SCREWS').status_code == 409


def test_active_cycle_survives_restart_and_resume_is_audited(authenticated):
    settings, app, client = authenticated
    row = start(client).json()['cycle']
    row = stage(client, settings, row, 'HOLES').json()['cycle']
    fresh = create_app(settings)
    with TestClient(fresh) as reader:
        reader.cookies.update(client.cookies); reader.headers.update(client.headers)
        restored = reader.get('/api/v1/pilot/status').json()['active']
        assert restored['stage'] == 'SCREWS' and restored['checks'] == row['checks']
        assert reader.post(f'/api/v1/pilot/cycles/{row["id"]}/resume', json=manual(settings, restored)).status_code == 200
    assert client.get(f'/api/v1/pilot/cycles/{row["id"]}').json()['events'][-1]['kind'] == 'RESUME'
    initialize(app.state.engine)  # additive initialization preserves pilot records
    assert client.get('/api/v1/pilot/cycles').json()['total'] == 1


def test_replay_requires_forward_media_time(authenticated):
    settings, _, client = authenticated
    row = client.post('/api/v1/pilot/cycles', json=dict(request_id=key(), product='A', source='replay')).json()['cycle']
    assert stage(client, settings, row, 'HOLES').status_code == 422
    row = stage(client, settings, row, 'HOLES', media_time=10).json()['cycle']
    assert stage(client, settings, row, 'SCREWS', media_time=9).status_code == 409
    assert stage(client, settings, row, 'SCREWS', media_time=12).status_code == 200


def test_route_no_model_and_history_search(authenticated):
    _, _, client = authenticated
    response = client.get('/station-1')
    assert response.status_code == 200 and 'pilot-dashboard.js?v=' in response.text
    assert response.url.params['task'] == 'station1'
    assert 'scene-console.js' not in client.get('/?scene=' + key()).text
    assert client.get('/api/v1/pilot/status').json()['config']['model'] is None
    start(client)
    assert client.get('/api/v1/pilot/cycles?q=P-001').json()['total'] == 1
    assert client.get('/api/v1/pilot/cycles?q=missing').json()['total'] == 0
    assert client.get('/api/v1/pilot/cycles?q=%25').json()['total'] == 0


def test_stale_capture_rejected_but_committed_retry_survives_age(authenticated, monkeypatch):
    import time
    import backend.pilot as pilot
    settings, _, client = authenticated
    clock = time.time()
    monkeypatch.setattr(pilot, 'now', lambda connection: clock)
    row = start(client).json()['cycle']
    assert stage(client, settings, row, 'HOLES', captured_at=clock - 121).status_code == 409
    assert stage(client, settings, row, 'HOLES', captured_at=clock + 31).status_code == 409
    body = manual(settings, row, stage='HOLES', checked=['H01', 'H02', 'H03', 'H04'],
                  image=picture(), captured_at=clock)
    url = f'/api/v1/pilot/cycles/{row["id"]}/stage'
    saved = client.post(url, json=body)
    assert saved.status_code == 200
    assert saved.json()['cycle']['checks']['HOLES']['captured_at'] == clock
    clock += 300
    assert client.post(url, json=body).json() == saved.json(), 'Already committed retry must replay even after capture expires'
    row = saved.json()['cycle']
    assert stage(client, settings, row, 'SCREWS', captured_at=clock - 121).status_code == 409
    row = stage(client, settings, row, 'SCREWS', captured_at=clock - 100).json()['cycle']
    clock += 90
    response = client.post(f'/api/v1/pilot/cycles/{row["id"]}/finish', json=manual(settings, row))
    assert response.status_code == 409, 'Finish freshness must use capture time rather than delayed receipt'
