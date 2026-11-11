"""Board aggregation uses real SQL with synthetic fixtures, never model inference."""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from backend import board
from backend.board import LOCAL_TIME, BoardService
from backend.db import pilot_cycles, pilot_events, station_inspections
from backend.pilot_config import STATIONS


def seed(app, part='P-001', station=1, result='MANUAL_OK', at=0, owner='admin', **extra):
    row = dict(id=str(uuid.uuid4()), owner=owner, station=f'station-{station}', active_station=None,
        product='灯具A', part_number=part, source='camera', parent_id=None, stage='CLOSED', result=result,
        review_status=None, revision=1, config=STATIONS[f'station-{station}'], checks={}, reason='',
        started_at=at-30, updated_at=at, finished_at=at)
    row.update(extra)
    with app.state.engine.begin() as c:
        c.execute(pilot_cycles.insert().values(**row))
    return row


def freeze(monkeypatch, app):
    instant = datetime(2026, 11, 6, 12, 30, tzinfo=LOCAL_TIME).timestamp()
    monkeypatch.setattr('backend.board.now', lambda _: instant)
    return instant


def test_board_requires_login_and_empty_data(context):
    _, _, client = context
    assert client.get('/api/v1/board/snapshot').status_code == 401
    assert client.get('/board').status_code == 200


def test_board_empty_data_is_not_perfect_compliance(authenticated, monkeypatch):
    _, app, client = authenticated
    freeze(monkeypatch, app)
    response = client.get('/api/v1/board/snapshot')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    data = response.json()
    assert data['summary'] == dict(total=0, passed=0, failed=0, pending=0,
        complianceRate=None, processPassed=0, processFailed=0)
    assert len(data['stations']) == 6
    assert all(s['status'] == 'OFFLINE' for s in data['stations'])
    assert all(p['rate'] is None for p in data['trends']['hour'])


def test_workpieces_deduplicate_rework_pending_and_other_owners(authenticated, monkeypatch):
    _, app, client = authenticated
    t = freeze(monkeypatch, app)
    for station in range(1, 7):
        seed(app, station=station, at=t-300)
    # A newer successful rework supersedes the earlier failure at the same station.
    seed(app, part='REWORK', result='NG', at=t-600)
    for station in range(1, 7):
        seed(app, part='REWORK', station=station, at=t-100)
    seed(app, part='BAD', result='NG', at=t-100)
    seed(app, part='UNFINISHED', result='REVIEW', at=t-100)
    seed(app, part='ACTIVE', result=None, stage='SCREWS', at=t-100,
         finished_at=None, active_station='station-2', station=2)
    seed(app, part='REPLAY', source='replay', result='NG', at=t-100)
    seed(app, part='IMAGE', source='image', result='NG', at=t-100)
    seed(app, part='PRIVATE', owner='someone-else', result='NG', at=t-100)
    seed(app, part='YESTERDAY', at=t-86400)
    data = client.get('/api/v1/board/snapshot').json()
    assert data['summary'] == dict(total=5, passed=2, failed=1, pending=2,
        complianceRate=92.3, processPassed=12, processFailed=1)
    assert data['stations'][1]['status'] == 'MANUAL'
    assert data['stations'][1]['partNumber'] == 'ACTIVE'
    assert data['trends']['hour'][12]['passed'] == 12
    assert data['trends']['hour'][12]['failed'] == 1
    assert data['trends']['shift'][1]['rate'] == 92.3
    assert data['trends']['hour'][13]['rate'] is None


def test_cross_midnight_final_station_and_review_overrides_pass(authenticated, monkeypatch):
    _, app, client = authenticated
    t = freeze(monkeypatch, app)
    for station in range(1, 6):
        seed(app, station=station, at=t-86400)
    seed(app, station=6, at=t-100)
    seed(app, part='REVIEWED', review_status='needs_rework', at=t-100)
    data = client.get('/api/v1/board/snapshot').json()
    assert data['summary']['total'] == 2
    assert data['summary']['passed'] == 1
    assert data['summary']['failed'] == 1
    assert data['summary']['processPassed'] == 1
    assert data['summary']['complianceRate'] == 50


def test_alerts_classification_excludes_notes_and_unbound_algorithms(authenticated, monkeypatch):
    _, app, client = authenticated
    t = freeze(monkeypatch, app)
    row = seed(app, result='NG', at=t-100)
    with app.state.engine.begin() as c:
        for revision, kind, payload in [
            (1, 'CLOSE', {'outcome':'NG','reason':'漏螺丝，同时缺卡扣'}),
            (2, 'REVIEW', {'disposition':'note','reason':'备注：漏螺丝已确认'}),
            (3, 'REVIEW', {'disposition':'needs_rework','reason':'工序跳步，请返工'}),
        ]:
            c.execute(pilot_events.insert().values(id=str(uuid.uuid4()), cycle_id=row['id'], owner='admin',
                request_key=str(uuid.uuid4()), request_hash='test', revision=revision, kind=kind,
                at=t-100+revision, payload=payload, response={}))
        for cycle in [row['id'], None]:
            c.execute(station_inspections.insert().values(stream_id=str(uuid.uuid4()), station_id='station-1',
                owner='admin', cycle_id=cycle, context={}, status='FINISHED',
                result={'compliance':{'status':'FAIL','reason':'未吹净'}}, reason='', started_at=t-200, finished_at=t-20))
    data = client.get('/api/v1/board/snapshot').json()
    assert data['alertCount'] == 3
    counts = {item['name']: item['count'] for item in data['top']}
    assert counts == {'漏螺丝':1, '缺卡扣':1, '工序跳步':1, '未吹净':1, '漏接端子':0, '其他异常':0}
    assert data['alerts'][0]['source'] == '算法归档'
    assert len({a['id'] for a in data['alerts']}) == 3


def test_terminal_cannot_read_global_board(authenticated):
    _, _, client = authenticated
    binding = client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='pc-01', cameraId='cam-01', source='usb', analysisType='manual',
        sampleFps=4, maxSampleGapSeconds=1, enabled=True, revision=0))
    assert binding.status_code == 200
    key = binding.json()['credential']
    assert client.get('/api/v1/board/snapshot', headers={'X-Terminal-Key':key}).status_code == 403


def test_cache_refresh_is_bounded_scoped_and_rolls_over_at_midnight(authenticated, monkeypatch):
    _, app, _ = authenticated
    instant = freeze(monkeypatch, app)
    clock = [100., instant]
    monkeypatch.setattr(board, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[1], perf_counter=time.perf_counter))
    monkeypatch.setattr(board, 'now', lambda _: clock[1])
    service = BoardService(app, capacity=2)
    seed(app, at=instant-10)
    statements = []
    def query(*args): statements.append(args[2])
    event.listen(app.state.engine, 'before_cursor_execute', query)
    try:
        first = service.snapshot('admin')
        assert first['summary']['total'] == 1
        statements.clear()
        assert service.snapshot('admin') is first
        assert statements == [], 'a warm snapshot must perform no database queries'
        seed(app, part='NEW', at=instant)
        clock[0] += 2; clock[1] += 2
        second = service.snapshot('admin')
        assert second is not first and second['generatedAt'] > first['generatedAt']
        assert second['summary']['total'] == 1
        assert second['statisticsAt'] == first['statisticsAt']
        clock[0] += 14; clock[1] += 14
        assert service.snapshot('admin')['summary']['total'] == 2
        assert service.snapshot('other')['summary']['total'] == 0
        service.snapshot('third')
        assert len(service.entries) == 2 and 'admin' not in service.entries
        # A new date invalidates the cache even before its monotonic TTL expires.
        service.snapshot('admin')
        clock[1] = datetime(2026, 11, 7, tzinfo=LOCAL_TIME).timestamp()
        midnight = service.snapshot('admin')
        assert midnight['date'] == '2026-11-07'
        assert midnight['summary']['total'] == 0
        assert midnight['alertCount'] == 0
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', query)


def test_concurrent_snapshots_share_one_build_and_busy_wait_is_bounded(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    value = {'date': datetime.now(LOCAL_TIME).strftime('%Y-%m-%d')}
    builds = []
    def build(*args, **kwargs):
        builds.append(1); entered.set()
        assert release.wait(2)
        return value, {}
    monkeypatch.setattr(board, 'snapshot', build)
    service = BoardService(None)
    with ThreadPoolExecutor(max_workers=6) as pool:
        first = pool.submit(service.snapshot, 'admin')
        assert entered.wait(1)
        rest = [pool.submit(service.snapshot, 'admin') for _ in range(5)]
        release.set()
        assert all(job.result(timeout=2) is value for job in [first, *rest])
    assert len(builds) == 1 and service.metrics()['hits'] == 5
    with service.lock:
        started = time.perf_counter()
        with pytest.raises(HTTPException) as error:
            service.snapshot('admin')
        assert error.value.status_code == 503
        assert error.value.headers['Retry-After'] == '2'
        assert time.perf_counter() - started < 1
    assert service.metrics()['busy'] == 1


def test_failed_refresh_does_not_disguise_stale_data(monkeypatch):
    value = {'date': datetime.now(LOCAL_TIME).strftime('%Y-%m-%d'), 'generatedAt': 123}
    monkeypatch.setattr(board, 'snapshot', lambda *a, **kw: (value, {}))
    service = BoardService(None, snapshot_ttl=0)
    assert service.snapshot('admin') == value
    def fail(*args, **kwargs): raise RuntimeError('database unavailable')
    monkeypatch.setattr(board, 'snapshot', fail)
    with pytest.raises(RuntimeError, match='database unavailable'):
        service.snapshot('admin')
    assert service.entries['admin']['value']['generatedAt'] == 123
    assert service.metrics()['failures'] == 1
    assert service.lock.acquire(blocking=False)
    service.lock.release()


def test_live_alarm_episode_identity_and_offline_visual_station(authenticated, monkeypatch):
    _, app, _ = authenticated
    instant = [freeze(monkeypatch, app)]
    monkeypatch.setattr(board, 'now', lambda _: instant[0])
    seed(app, at=instant[0]-10, result=None, stage='SCREWS', finished_at=None, active_station='station-1')
    live = dict(state='LIVE', streamId='stream', cycleId='cycle', result={'compliance':{'status':'FAIL','reason':'漏螺丝'}})
    monkeypatch.setattr(app.state.terminals, 'statuses', lambda _: {'station-1':live})
    memory = {}
    def read():
        instant[0] += 2
        return board.snapshot(app, 'admin', live_memory=memory)[0]
    first = read()['alerts'][0]
    assert read()['alerts'][0] == first, 'persistent FAIL must not become a new alert every poll'
    live['state'] = 'STALE'
    assert read()['stations'][0]['status'] == 'REVIEW'
    live['state'] = 'LIVE'
    assert read()['alerts'][0]['id'] == first['id']
    live['result']['compliance']['reason'] = '缺卡扣'
    changed = read()['alerts'][0]
    assert changed['id'] != first['id']
    live['result']['compliance']['status'] = 'PASS'
    assert read()['alerts'] == []
    live['result']['compliance']['status'] = 'FAIL'
    assert read()['alerts'][0]['id'] != changed['id'], 'cleared and recurring failures are new episodes'
    live['result']['compliance']['status'] = 'REVIEW'
    assert read()['stations'][0]['status'] == 'REVIEW'
    live.update(state='OFFLINE', reasonCode='NOT_STARTED', result=None)
    assert read()['stations'][0]['status'] == 'OFFLINE', 'unfinished work does not imply manual mode'


def test_alert_limit_does_not_limit_daily_totals(authenticated, monkeypatch):
    _, app, _ = authenticated
    instant = freeze(monkeypatch, app)
    row = seed(app, at=instant-300, result='NG')
    with app.state.engine.begin() as c:
        c.execute(pilot_events.insert(), [dict(id=f'manual-{i:03}', cycle_id=row['id'], owner='admin',
            request_key=f'event-{i}', request_hash='fixture', revision=i+1, kind='CLOSE', at=instant-280+i,
            payload={'outcome':'NG','reason':'漏螺丝'}, response={}) for i in range(130)])
        c.execute(station_inspections.insert(), [dict(stream_id=f'archive-{i:03}', station_id='station-1',
            owner='admin', cycle_id=row['id'], context={}, status='FINISHED', reason='', started_at=instant-300,
            finished_at=instant-150+i, result={'compliance':{'status':'FAIL','reason':'缺卡扣'},'large':'x'*8192})
            for i in range(120)])
    value, _ = board.snapshot(app, 'admin')
    assert value['alertCount'] == 250 and len(value['alerts']) == 100
    assert value['alerts'][0]['id'] == 'archive-119'
    assert all(alert['source'] == '算法归档' for alert in value['alerts'])
    assert {item['name']: item['count'] for item in value['top']}['漏螺丝'] == 130
    assert 'large' not in str(value)
