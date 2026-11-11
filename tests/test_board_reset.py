"""Board reset is scoped, atomic, and cannot consume newly started rounds."""
import time
import uuid

import pytest
from sqlalchemy import delete, event, func, select

from backend.db import (alert_evidence, station_inspections as inspections,
                        station_inspection_deletions as deletions,
                        station_stream_leases as leases, station_terminals as bindings)

URL = '/api/v1/board/reset'


def seed(app, *, owner='admin', status='FINISHED', days_ago=0, recycled=False):
    identity = str(uuid.uuid4())
    at = time.time()-days_ago*86400-10
    with app.state.engine.begin() as c:
        c.execute(inspections.insert().values(stream_id=identity, station_id='station-1', owner=owner,
            context={'analysisType':'screw', 'cameraId':'test-camera'}, status=status,
            result={'compliance':{'status':'FAIL', 'reason':'漏螺丝'},
                    'manualReview':{'status':'PASS', 'reviewer':owner, 'reason':'测试人工确认'}},
            started_at=at-60, finished_at=None if status=='RUNNING' else at))
        if recycled:
            c.execute(deletions.insert().values(stream_id=identity, owner=owner, deleted_at=at, deleted_by=owner))
    return identity


def body(client, **extra):
    response = client.get(URL)
    assert response.status_code == 200
    return dict(confirmation='清空全部检测数据', password='test-password-12345',
                revision=response.json()['revision'], **extra)


def count(app, table):
    with app.state.engine.connect() as c:
        return c.scalar(select(func.count()).select_from(table))


def test_reset_all_dates_recycle_review_and_board_preserves_configuration(authenticated):
    _, app, client = authenticated
    client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='reset-pc', cameraId='reset-camera', analysisType='manual'))
    owned = [seed(app), seed(app, days_ago=40), seed(app, recycled=True)]
    private = seed(app, owner='other-account')
    with app.state.engine.begin() as c:
        c.execute(alert_evidence.insert().values(id=str(uuid.uuid4()), owner='admin', source_kind='realtime',
            source_id=owned[0], analysis_type='screw', severity='FAIL', reason='独立保存的告警录像',
            status='MISSING', details={}, created_at=time.time(), updated_at=time.time()))
    before = client.get('/api/v1/board/snapshot?mode=detection').json()
    assert before['summary']['total'] == 1
    preview = client.get(URL).json()
    assert (preview['total'], preview['recycled'], preview['active']) == (3, 1, 0)
    request = body(client)
    result = client.post(URL, json=request)
    assert result.status_code == 200, result.text
    assert result.json()['deleted'] == 3
    assert set(result.json()['streamIds']) == set(owned)
    assert count(app, inspections) == count(app, bindings) == count(app, alert_evidence) == 1
    assert count(app, deletions) == 0
    with app.state.engine.connect() as c:
        assert c.scalar(select(inspections.c.stream_id)) == private
    snapshot = client.get('/api/v1/board/snapshot?mode=detection').json()
    assert snapshot['summary']['total'] == snapshot['alertCount'] == 0
    assert all(s['streamId'] is None for s in snapshot['stations'])
    assert all(p['passed'] == p['failed'] == p['pending'] == 0 for p in snapshot['trends']['hour'])
    assert client.get('/api/v1/board/history?verdict=ALL').json()['total'] == 0
    assert client.get('/api/v1/board/history?deleted=true').json()['total'] == 0
    assert client.get('/api/v1/station-streams/'+owned[0]+'/inspection').status_code == 404
    assert client.post(URL, json=request).json()['deleted'] == 0  # Safe retry after a lost response.


def test_reset_requires_admin_csrf_phrase_and_password(authenticated):
    _, app, client = authenticated
    seed(app)
    request = body(client)
    assert client.post(URL, json={**request, 'confirmation':'clear'}).status_code == 422
    assert client.post(URL, json={**request, 'password':'wrong'}).status_code == 403
    assert client.post(URL, json=request, headers={'X-CSRF-Token':'wrong'}).status_code == 403
    binding = client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='reset-pc', cameraId='reset-camera', analysisType='manual')).json()
    assert client.get(URL, headers={'X-Terminal-Key':binding['credential']}).status_code == 403
    assert client.post(URL, json=request, headers={'X-Terminal-Key':binding['credential']}).status_code == 403
    client.cookies.clear()
    assert client.get(URL).status_code == client.post(URL, json=request).status_code == 401
    assert count(app, inspections) == 1


def test_reset_rejects_new_records_even_with_same_count(authenticated):
    _, app, client = authenticated
    first = seed(app)
    request = body(client)
    with app.state.engine.begin() as c:
        c.execute(delete(inspections).where(inspections.c.stream_id == first))
    seed(app)
    response = client.post(URL, json=request)
    assert response.status_code == 409 and '记录已变化' in response.text
    assert count(app, inspections) == 1


@pytest.mark.parametrize('status', ['RUNNING', 'FINISHED'])
def test_reset_blocks_active_lease_including_archived_not_stopped(authenticated, status):
    _, app, client = authenticated
    client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='reset-pc', cameraId='reset-camera', analysisType='manual'))
    identity = seed(app, status=status)
    with app.state.engine.begin() as c:
        c.execute(leases.insert().values(station_id='station-1', stream_id=identity, epoch=1,
            runtime_id='other-runtime', request_id=str(uuid.uuid4()), closed=False, context={},
            created_at=time.time(), lease_until=time.time()+60))
    assert client.get(URL).json()['activeStations'] == ['station-1']
    response = client.post(URL, json=body(client))
    assert response.status_code == 409 and '正在运行' in response.text
    assert count(app, inspections) == 1


def test_reset_handles_orphaned_rounds_and_fences_retry_after_new_data(authenticated):
    _, app, client = authenticated
    seed(app, status='RUNNING')
    request = body(client)  # Reconcile abandoned rows, without requiring a restart.
    assert client.post(URL, json=request).json()['deleted'] == 1
    seed(app)
    assert client.post(URL, json=request).status_code == 409
    assert count(app, inspections) == 1


def test_reset_rolls_back_recycle_deletion_on_database_failure(authenticated):
    _, app, client = authenticated
    seed(app, recycled=True)
    request = body(client)
    def fail(conn, cursor, statement, params, context, many):
        if statement.startswith('DELETE FROM station_inspections '):
            raise RuntimeError('injected reset failure')
    event.listen(app.state.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='injected reset failure'):
            client.post(URL, json=request)
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', fail)
    assert count(app, inspections) == count(app, deletions) == 1


def selected_body(*identities):
    return dict(confirmation='删除选中检测记录', streamIds=list(identities), password='test-password-12345')


def test_selected_deletion_preserves_unselected_and_is_safe_to_retry(authenticated):
    _, app, client = authenticated
    owned = [seed(app), seed(app, days_ago=40, recycled=True)]
    kept = seed(app)
    private = seed(app, owner='other-account')
    before = client.get('/api/v1/board/snapshot?mode=detection').json()
    assert before['summary']['total'] == 2
    request = selected_body(*owned, owned[0])  # Duplicate IDs delete once.
    result = client.post(URL+'/selected', json=request)
    assert result.status_code == 200, result.text
    assert result.json()['deleted'] == 2 and result.json()['alreadyDeleted'] == 0
    assert set(result.json()['streamIds']) == set(owned)
    assert count(app, deletions) == 0
    assert client.get('/api/v1/board/snapshot?mode=detection').json()['summary']['total'] == 1
    new = seed(app)
    retry = client.post(URL+'/selected', json=request).json()
    assert retry['deleted'] == 0 and retry['alreadyDeleted'] == 2
    with app.state.engine.connect() as c:
        assert set(c.execute(select(inspections.c.stream_id)).scalars()) == {kept, private, new}
    missing = client.post(URL+'/selected', json=selected_body(str(uuid.uuid4())))
    assert missing.status_code == 200 and missing.json()['alreadyDeleted'] == 1


def test_selected_deletion_rejects_foreign_selection_atomically(authenticated):
    _, app, client = authenticated
    owned = seed(app, recycled=True)
    private = seed(app, owner='other-account')
    result = client.post(URL+'/selected', json=selected_body(owned, private))
    assert result.status_code == 404
    assert count(app, inspections) == 2 and count(app, deletions) == 1


@pytest.mark.parametrize('status,leased', [('RUNNING', False), ('FINISHED', True)])
def test_selected_active_round_is_rejected_atomically(authenticated, status, leased):
    _, app, client = authenticated
    kept = seed(app, recycled=True)
    identity = seed(app, status=status)
    if leased:
        client.put('/api/v1/admin/terminals/station-1', json=dict(
            workstationId='selected-pc', cameraId='selected-camera', analysisType='manual'))
        with app.state.engine.begin() as c:
            c.execute(leases.insert().values(station_id='station-1', stream_id=identity, epoch=1,
                runtime_id='other-runtime', request_id=str(uuid.uuid4()), closed=False, context={},
                created_at=time.time(), lease_until=time.time()+60))
    result = client.post(URL+'/selected', json=selected_body(kept, identity))
    assert result.status_code == 409
    assert count(app, inspections) == 2 and count(app, deletions) == 1


def test_selected_history_can_be_deleted_during_another_round(authenticated):
    _, app, client = authenticated
    identity = seed(app)
    running = seed(app, status='RUNNING')
    result = client.post(URL+'/selected', json=selected_body(identity))
    assert result.status_code == 200 and result.json()['deleted'] == 1
    with app.state.engine.connect() as c:
        assert c.scalar(select(inspections.c.stream_id)) == running


def test_selected_requires_admin_csrf_password_and_bounded_explicit_ids(authenticated):
    _, app, client = authenticated
    identity = seed(app)
    request = selected_body(identity)
    for patch in ({'streamIds':[]}, {'streamIds':['bad']}, {'streamIds':[str(uuid.uuid4()) for _ in range(201)]},
                  {'confirmation':'clear'}, {'all':True}):
        assert client.post(URL+'/selected', json={**request, **patch}).status_code == 422
    assert client.post(URL+'/selected', json={**request, 'password':'wrong'}).status_code == 403
    assert client.post(URL+'/selected', json=request, headers={'X-CSRF-Token':'wrong'}).status_code == 403
    binding = client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='selected-pc', cameraId='selected-camera', analysisType='manual')).json()
    assert client.post(URL+'/selected', json=request, headers={'X-Terminal-Key':binding['credential']}).status_code == 403
    client.cookies.clear()
    assert client.post(URL+'/selected', json=request).status_code == 401
    assert count(app, inspections) == 1


def test_selected_rolls_back_recycle_on_database_failure(authenticated):
    _, app, client = authenticated
    identity = seed(app, recycled=True)
    def fail(conn, cursor, statement, params, context, many):
        if statement.startswith('DELETE FROM station_inspections '):
            raise RuntimeError('injected selected failure')
    event.listen(app.state.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='injected selected failure'):
            client.post(URL+'/selected', json=selected_body(identity))
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', fail)
    assert count(app, inspections) == count(app, deletions) == 1
