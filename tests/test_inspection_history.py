"""Daily archive boundaries, effective outcomes, access control and recycling."""
from datetime import datetime
import uuid

import pytest
from sqlalchemy import event, func, select

from backend.board import LOCAL_TIME
from backend.db import (alert_evidence, initialize, station_inspections,
                        station_inspection_deletions, station_stream_leases)


@pytest.fixture
def archive(authenticated, monkeypatch):
    _, app, client = authenticated
    at = datetime(2026, 11, 8, 12, 30, tzinfo=LOCAL_TIME).timestamp()
    monkeypatch.setattr('backend.inspection_history.now', lambda _: at)
    monkeypatch.setattr('backend.board_detection.now', lambda _: at)
    monkeypatch.setattr('backend.terminals.now', lambda _: at)
    return app, client, at


def seed(app, at, *, verdict='PASS', status='FINISHED', station=1, owner='admin', **extra):
    row = dict(stream_id=str(uuid.uuid4()), station_id=f'station-{station}', owner=owner,
        cycle_id=None, context={'cameraId': f'camera-{station}', 'analysisType': 'screw', 'inputSource': 'replay'},
        status=status, result={'compliance': {'status': verdict, 'label': verdict, 'reason': '检查判定'}},
        reason='归档完成', started_at=at - 60, finished_at=at if status != 'RUNNING' else None)
    row.update(extra)
    with app.state.engine.begin() as connection:
        connection.execute(station_inspections.insert().values(**row))
        if status == 'RUNNING':
            connection.execute(station_stream_leases.insert().values(station_id=row['station_id'],
                stream_id=row['stream_id'], epoch=1, runtime_id=app.state.terminals.runtime_id,
                request_id=str(uuid.uuid4()), closed=False, context={}, created_at=at,
                lease_until=at + 3600))
    return row['stream_id']


def test_history_defaults_and_finish_day_boundaries(archive):
    app, client, at = archive
    start = datetime(2026, 11, 8, tzinfo=LOCAL_TIME).timestamp()
    fail = seed(app, start, verdict='FAIL', started_at=start - 3600)
    seed(app, start - .01, verdict='FAIL')
    seed(app, start + 86400, verdict='FAIL')
    seed(app, at, verdict='FAIL', owner='someone-else')
    seed(app, at, status='RUNNING')
    seed(app, at - 30, status='INTERRUPTED', finished_at=None)
    seed(app, at - 10, station=2)
    data = client.get('/api/v1/board/history').json()
    assert data['date'] == '2026-11-08' and data['timezone'] == 'UTC+08:00'
    assert [item['streamId'] for item in data['items']] == [fail]
    assert data['summary'] == dict(total=3, failed=1, passed=1, pending=1, complianceRate=50)
    assert data['total'] == 1 and data['deletedCount'] == 0 and not data['hasMore']
    assert data['items'][0]['sourceLabel'] == '视频联调'
    station = client.get('/api/v1/board/history?stationId=station-1&verdict=ALL').json()
    assert station['summary'] == dict(total=2, failed=1, passed=0, pending=1, complianceRate=0)
    previous = client.get('/api/v1/board/history?date=2026-11-07').json()
    assert previous['total'] == 1 and fail not in [item['streamId'] for item in previous['items']]


def test_manual_review_priority_pagination_and_full_detail(archive):
    app, client, at = archive
    passed = seed(app, at)
    interrupted = seed(app, at - 10, status='INTERRUPTED', verdict='PASS', reason='采集停止')
    fail1 = seed(app, at - 30, verdict='FAIL')
    fail2 = seed(app, at - 20, verdict='FAIL')
    reviewed = seed(app, at - 5, verdict='FAIL', result={
        'compliance': {'status': 'FAIL', 'reason': '原始异常'}, 'large': 'x' * 10000,
        'manualReview': {'source': 'manual', 'status': 'PASS', 'label': '人工确认合规', 'reason': '复核通过'}})
    first = client.get('/api/v1/board/history?verdict=ALL&limit=2').json()
    assert first['total'] == first['summary']['total'] == 5 and first['hasMore']
    assert [item['streamId'] for item in first['items']] == [fail2, fail1]
    second = client.get('/api/v1/board/history?verdict=ALL&limit=2&offset=2').json()
    assert [item['streamId'] for item in second['items']] == [interrupted, passed]
    assert second['items'][0]['label'] == '待复核' and second['items'][0]['reason'] == '采集停止'
    final = client.get('/api/v1/board/history?verdict=ALL&limit=2&offset=4').json()
    assert not final['hasMore'] and final['items'][0]['streamId'] == reviewed
    assert final['items'][0]['reviewed'] and final['items'][0]['verdict'] == 'PASS'
    assert 'large' not in str(final)
    detail = client.get(f'/api/v1/board/history/{reviewed}').json()
    assert detail['item']['result']['large'] == 'x' * 10000
    assert detail['item']['result']['compliance']['status'] == 'FAIL'
    assert detail['item']['reason'] == '复核通过'
    assert detail['evidence'] == []


def test_recycle_is_idempotent_and_updates_all_views_without_destroying_record(archive):
    app, client, at = archive
    identity = seed(app, at - 1, verdict='FAIL')
    path = f'/api/v1/board/history/{identity}'
    board = '/api/v1/board/snapshot?mode=detection'
    assert client.get(board).json()['summary']['failed'] == 1
    assert len(app.state.board.entries) == 1
    for _ in range(2):
        assert client.delete(path).json() == dict(ok=True, streamId=identity, deleted=True)
    assert not app.state.board.entries
    data = client.get('/api/v1/board/history').json()
    assert data['summary']['total'] == data['total'] == 0 and data['deletedCount'] == 1
    recycled = client.get('/api/v1/board/history?deleted=true').json()
    assert recycled['total'] == 1 and recycled['summary']['total'] == 0
    assert recycled['items'][0]['deletedAt'] == at and recycled['items'][0]['deletedBy'] == 'admin'
    snapshot = client.get(board).json()
    assert snapshot['summary']['total'] == snapshot['alertCount'] == 0
    assert all(bucket['failed'] == 0 for bucket in snapshot['trends']['hour'])
    assert snapshot['stations'][0]['streamId'] is None
    assert client.get('/api/v1/stations/station-1/inspections').json()['items'] == []
    assert client.get(path).json()['item']['deletedAt'] == at
    with app.state.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(station_inspections)) == 1
        assert connection.scalar(select(func.count()).select_from(station_inspection_deletions)) == 1
    for _ in range(2):
        assert client.post(path + '/restore').json() == dict(ok=True, streamId=identity, deleted=False)
    assert client.get(board).json()['summary']['failed'] == 1
    assert client.get('/api/v1/board/history').json()['deletedCount'] == 0
    assert len(client.get('/api/v1/stations/station-1/inspections').json()['items']) == 1


def test_access_control_csrf_and_running_round_protection(archive):
    app, client, at = archive
    identity = seed(app, at)
    foreign = seed(app, at, owner='private')
    running = seed(app, at, status='RUNNING')
    for target in (foreign, str(uuid.uuid4())):
        path = f'/api/v1/board/history/{target}'
        assert client.get(path).status_code == 404
        assert client.delete(path).status_code == 404
        assert client.post(path + '/restore').status_code == 404
    assert client.get(f'/api/v1/board/history/{running}').status_code == 404
    assert client.delete(f'/api/v1/board/history/{running}').status_code == 409
    assert client.post(f'/api/v1/board/history/{running}/restore').status_code == 409
    assert client.delete(f'/api/v1/board/history/{identity}', headers={'X-CSRF-Token': ''}).status_code == 403
    binding = client.put('/api/v1/admin/terminals/station-6', json=dict(
        workstationId='history-pc', cameraId='history-camera', analysisType='manual')).json()
    headers = {'X-Terminal-Key': binding['credential']}
    assert client.get('/api/v1/board/history', headers=headers).status_code == 403
    assert client.get(f'/api/v1/board/history/{identity}', headers=headers).status_code == 403
    assert client.delete(f'/api/v1/board/history/{identity}', headers=headers).status_code == 403
    assert client.post(f'/api/v1/board/history/{identity}/restore', headers=headers).status_code == 403
    client.cookies.clear()
    assert client.get('/api/v1/board/history').status_code == 401


@pytest.mark.parametrize('query', ['date=2026-02-30', 'date=11-08-2026', 'stationId=station-7',
                                  'verdict=NG', 'limit=101', 'offset=-1'])
def test_invalid_history_filters(archive, query):
    _, client, _ = archive
    assert client.get('/api/v1/board/history?' + query).status_code == 422


@pytest.mark.parametrize('legacy', [False, True])
def test_evidence_is_owner_scoped_and_retained_after_recycling(archive, legacy):
    import hashlib
    app, client, at = archive
    identity = seed(app, at, verdict='FAIL')
    legacy_source = lambda station, kind: hashlib.sha256(f'station:{station}:{identity}:{kind}'.encode()).hexdigest()
    source = legacy_source('station-1', 'screw') if legacy else identity
    sources = [source, source, str(uuid.uuid4()), legacy_source('station-2', 'screw'),
               legacy_source('station-1', 'sticker')]
    ids = [str(uuid.uuid4()) for _ in sources]
    with app.state.engine.begin() as connection:
        for index, evidence_id in enumerate(ids):
            connection.execute(alert_evidence.insert().values(id=evidence_id,
                owner='private' if index == 1 else 'admin', source_kind='realtime',
                source_id=sources[index], analysis_type='screw',
                severity='FAIL', reason='测试告警', status='PENDING', archive_key=evidence_id,
                details={'internal': True}, created_at=at, updated_at=at, legal_hold=False, attempts=0, token=0))
    path = f'/api/v1/board/history/{identity}'
    assert client.delete(path).status_code == 200
    data = client.get(path).json()
    assert len(data['evidence']) == 1 and data['evidence'][0]['id'] == ids[0]
    assert 'video_path' not in data['evidence'][0] and 'details' not in data['evidence'][0]
    with app.state.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(alert_evidence)) == len(ids)


def test_additive_initialization_and_scalar_list_projection(archive):
    from backend.db import schema, schema_migrations
    app, client, at = archive
    # Represents a v1 database created before the tombstone table existed.
    with app.state.engine.begin() as c:
        station_inspection_deletions.drop(c)
        schema_migrations.drop(c)
        c.execute(schema.update().values(version=1))
    initialize(app.state.engine)
    initialize(app.state.engine)
    seed(app, at, verdict='FAIL', result={'compliance': {'status': 'FAIL'}, 'large': 'x' * 10000})
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if 'station_inspections' in statement:
            statements.append((statement, [column.key for column in _context.compiled.statement.selected_columns]))
    event.listen(app.state.engine, 'before_cursor_execute', capture)
    try:
        response = client.get('/api/v1/board/history')
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200 and 'large' not in response.text
    assert len(statements) == 3
    assert all(not {'result', 'context'} & set(columns) for _, columns in statements)


def test_expired_running_round_is_recovered_without_touching_other_owners(archive):
    app, client, at = archive
    identity = seed(app, at - 4000, status='RUNNING')
    foreign = seed(app, at - 4000, status='RUNNING', station=2, owner='private')
    data = client.get('/api/v1/board/history?verdict=ALL').json()
    assert data['summary']['pending'] == 1 and data['items'][0]['streamId'] == identity
    assert data['items'][0]['status'] == 'INTERRUPTED' and data['items'][0]['finishedAt'] == at
    with app.state.engine.connect() as connection:
        assert connection.scalar(select(station_inspections.c.status).where(
            station_inspections.c.stream_id == foreign)) == 'RUNNING'
