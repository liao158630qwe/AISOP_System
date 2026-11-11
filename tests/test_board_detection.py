"""Detection totals and live/archived handoff without creating any workpieces."""
from datetime import date, datetime
from types import SimpleNamespace
import time
import uuid
import pytest

from sqlalchemy import func, select, update

from backend.board import LOCAL_TIME
from backend.board_detection import snapshot
from backend.db import pilot_cycles, station_inspections, station_inspection_deletions


def round_row(app, at, *, station=1, status='FINISHED', verdict='PASS', owner='admin', **extra):
    identity = str(uuid.uuid4())
    row = dict(stream_id=identity, station_id=f'station-{station}', owner=owner, cycle_id=None,
        context={'cameraId': f'camera-{station}', 'analysisType': 'screw'}, status=status,
        result={'compliance': {'status': verdict, 'label': '合规' if verdict == 'PASS' else '异常',
                              'reason': '漏螺丝' if verdict == 'FAIL' else '累计视觉确认'},
                'sequence': {'positionsConfirmed': 11, 'positionsExpected': 11}},
        reason='', started_at=at - 60, finished_at=at if status != 'RUNNING' else None)
    row.update(extra)
    with app.state.engine.begin() as c:
        c.execute(station_inspections.insert().values(**row))
    return row


def setup_clock(monkeypatch):
    at = datetime(2026, 11, 7, 12, 30, tzinfo=LOCAL_TIME).timestamp()
    monkeypatch.setattr('backend.board_detection.now', lambda _: at)
    return at


def live_result(row, status='REVIEW'):
    return dict(state='LIVE', streamId=row['stream_id'], cameraId='camera-1', analysisType='screw',
        effectiveFps=3.8, gapCount=0, result={'compliance': {'status': status, 'label': status, 'reason': '实时判定'},
                                          'sequence': {'positionsConfirmed': 5, 'positionsExpected': 11}})


def test_unbound_rounds_live_progress_and_archive_do_not_double_count(authenticated, monkeypatch):
    _, app, client = authenticated
    at = setup_clock(monkeypatch)
    row = round_row(app, at - 1, status='RUNNING', result=None)
    live = live_result(row)
    monkeypatch.setattr(app.state.terminals, 'statuses', lambda _: {'station-1': live})
    def read():
        return snapshot(app, 'admin')[0]
    first = read()
    assert first['summary']['total'] == first['summary']['pending'] == 1
    assert first['summary']['online'] == 1
    card = first['stations'][0]
    assert card['online'] and card['status'] == 'REVIEW' and card['effectiveFps'] == 3.8
    assert card['detection']['confirmed'] == 5 and card['detection']['expected'] == 11
    assert card['title'] == '螺丝专项检测' and card['partNumber'] is None
    live['result']['compliance']['status'] = 'PASS'
    assert read()['summary']['passed'] == 1
    live['state'] = 'STALE'
    expired = read()
    assert expired['summary']['passed'] == 0 and expired['summary']['pending'] == 1
    assert not expired['stations'][0]['online']
    assert expired['stations'][0]['detection']['confirmed'] is None
    live['state'] = 'LIVE'; live['gapCount'] = 1
    assert read()['summary']['passed'] == 0
    live['gapCount'] = 0
    with app.state.engine.begin() as c:
        c.execute(update(station_inspections).where(station_inspections.c.stream_id == row['stream_id']).values(
            status='FINISHED', finished_at=at, result=live['result']))
    # SQL finish wins even if a concurrent live read still sees the old result.
    archived = read()
    assert archived['summary']['total'] == archived['summary']['passed'] == 1
    assert archived['summary']['pending'] == 0
    assert archived['stations'][0]['detection']['archived']
    assert archived['stations'][0]['detection']['label'] == '上轮合规'
    assert archived['trends']['hour'][12]['passed'] == 1
    with app.state.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(pilot_cycles)) == 0
    assert client.get('/api/v1/board/snapshot?mode=detection').json()['summary']['total'] == 1
    assert client.get('/api/v1/board/snapshot').json()['summary']['total'] == 0


def test_round_scope_owner_day_interruption_and_unbound_fail_alarms(authenticated, monkeypatch):
    _, app, _ = authenticated
    at = setup_clock(monkeypatch)
    round_row(app, at - 10, verdict='FAIL', station=2)
    round_row(app, at - 5, status='INTERRUPTED', verdict='PASS')
    round_row(app, at - 3, owner='private')
    round_row(app, at - 86400)
    # A round started yesterday and finished today counts exactly once today.
    round_row(app, at - 2, station=3, started_at=at - 86400)
    data = snapshot(app, 'admin')[0]
    assert {k: data['summary'][k] for k in ('total', 'passed', 'failed', 'pending')} == {
        'total': 3, 'passed': 1, 'failed': 1, 'pending': 1}
    assert data['summary']['complianceRate'] == 50
    assert data['alertCount'] == 1 and data['alerts'][0]['source'] == '算法归档'
    assert data['alerts'][0]['partNumber'] == 'camera-2'
    assert next(t['count'] for t in data['top'] if t['name'] == '漏螺丝') == 1
    assert snapshot(app, 'unrelated')[0]['summary']['total'] == 0


def test_live_fail_and_archived_fail_share_one_event_and_not_large_payload(authenticated, monkeypatch):
    _, app, _ = authenticated
    at = setup_clock(monkeypatch)
    row = round_row(app, at - 2, status='RUNNING', result=None)
    live = live_result(row, 'FAIL')
    # Missing samples prevent PASS but must not hide an observed failure.
    live['gapCount'] = 1
    live['result']['compliance']['reason'] = '漏螺丝，缺卡扣'
    monkeypatch.setattr(app.state.terminals, 'statuses', lambda _: {'station-1': live})
    current = snapshot(app, 'admin')[0]
    assert current['alertCount'] == 1
    assert current['stations'][0]['detection']['status'] == 'FAIL'
    assert current['stations'][0]['reason'] == '漏螺丝，缺卡扣'
    assert current['alerts'][0]['id'] == row['stream_id']
    assert sum(t['count'] for t in current['top']) == 2
    with app.state.engine.begin() as c:
        c.execute(update(station_inspections).where(station_inspections.c.stream_id == row['stream_id']).values(
            status='FINISHED', finished_at=at, result={**live['result'], 'large': 'x' * 10000}))
    saved = snapshot(app, 'admin')[0]
    assert saved['alertCount'] == 1 and len(saved['alerts']) == 1
    assert saved['alerts'][0]['id'] == current['alerts'][0]['id']
    assert saved['summary']['failed'] == 1 and sum(t['count'] for t in saved['top']) == 2
    assert 'large' not in str(saved)


def test_detection_requires_admin_and_valid_mode(authenticated):
    _, _, client = authenticated
    binding = client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='pc-detection', cameraId='camera-detection', analysisType='manual')).json()
    assert client.get('/api/v1/board/snapshot?mode=detection',
                      headers={'X-Terminal-Key': binding['credential']}).status_code == 403
    assert client.get('/api/v1/board/snapshot?mode=unknown').status_code == 422


def test_empty_holes_are_not_counted_as_installed_positions(authenticated, monkeypatch):
    _, app, _ = authenticated
    at = setup_clock(monkeypatch)
    row = round_row(app, at - 1, status='RUNNING', result=None)
    live = live_result(row)
    live['result']['sequence'].update(positionsConfirmed=11, operationsCompleted=7, operationsExpected=9,
                                      preassembledConfirmed=0, preassembledExpected=2)
    monkeypatch.setattr(app.state.terminals, 'statuses', lambda _: {'station-1': live})
    current = snapshot(app, 'admin')[0]['stations'][0]['detection']
    assert (current['confirmed'], current['expected']) == (7, 11)
    live['gapCount'] = 1
    assert snapshot(app, 'admin')[0]['stations'][0]['detection']['label'] == '待复核'
    with app.state.engine.begin() as c:
        c.execute(update(station_inspections).where(station_inspections.c.stream_id == row['stream_id']).values(
            status='FINISHED', finished_at=at, result=live['result']))
    archived = snapshot(app, 'admin')[0]['stations'][0]['detection']
    assert (archived['confirmed'], archived['expected']) == (7, 11)


@pytest.mark.parametrize('result,expected', [
    ({'compliance': {'operationsCompleted': 4, 'operationsExpected': 7,
                     'preassembledConfirmed': 2, 'preassembledExpected': 2}}, (6, 9)),
    ({'compliance': {'positionsConfirmed': 3, 'positionsExpected': 6}}, (3, 6)),
    ({'sequence': {'positionsConfirmed': 5, 'positionsExpected': 6},
      'compliance': {'operationsCompleted': 2, 'operationsExpected': 6}}, (2, 6)),
    ({'sequence': {'operationsCompleted': 1, 'operationsExpected': 4},
      'compliance': {'operationsCompleted': 2, 'operationsExpected': 6}}, (1, 4)),
])
def test_progress_survives_archive_and_source_is_preserved(authenticated, monkeypatch, result, expected):
    _, app, _ = authenticated
    at = setup_clock(monkeypatch)
    row = round_row(app, at - 1, status='RUNNING', result=None,
                    context={'cameraId': 'replay-camera', 'analysisType': 'screw', 'inputSource': 'replay'})
    live = live_result(row)
    live['result'] = result
    monkeypatch.setattr(app.state.terminals, 'statuses', lambda _: {'station-1': live})
    current = snapshot(app, 'admin')[0]['stations'][0]
    assert current['inputSource'] == 'replay' and current['sourceLabel'] == '视频联调'
    assert (current['detection']['confirmed'], current['detection']['expected']) == expected
    with app.state.engine.begin() as c:
        c.execute(update(station_inspections).where(station_inspections.c.stream_id == row['stream_id']).values(
            status='FINISHED', finished_at=at, result=result))
    archived = snapshot(app, 'admin')[0]['stations'][0]
    assert archived['inputSource'] == 'replay'
    assert archived['detection']['recordStatus'] == 'FINISHED'
    assert (archived['detection']['confirmed'], archived['detection']['expected']) == expected


def test_all_today_and_date_scopes_include_matching_counts_alerts_and_trends(authenticated, monkeypatch):
    _, app, client = authenticated
    at = setup_clock(monkeypatch)
    old = round_row(app, at-86400, verdict='FAIL')
    round_row(app, at-2, verdict='PASS', station=2)
    interrupted = round_row(app, at-86400-1, verdict='PASS', status='INTERRUPTED', station=3, finished_at=None)
    recycled = round_row(app, at-86400-3, verdict='FAIL')
    round_row(app, at-86400-4, verdict='FAIL', owner='private')
    with app.state.engine.begin() as c:
        c.execute(station_inspection_deletions.insert().values(stream_id=recycled['stream_id'],
            owner='admin', deleted_at=at, deleted_by='admin'))
    live_row = round_row(app, at-1, status='RUNNING', station=4, result=None)
    live = live_result(live_row, 'FAIL')
    monkeypatch.setattr(app.state.terminals, 'statuses', lambda _: {'station-4':live})
    overall = snapshot(app, 'admin', time_range='all')[0]
    today = snapshot(app, 'admin')[0]
    previous = snapshot(app, 'admin', time_range='date', selected_date=date(2026,11,6))[0]
    assert overall['summary']['total'] == 4
    assert today['summary']['total'] == 2
    assert previous['summary']['total'] == 2
    assert overall['alertCount'] == 2 and previous['alertCount'] == today['alertCount'] == 1
    assert previous['alerts'][0]['id'] == old['stream_id']
    assert previous['stations'][0]['streamId'] == old['stream_id']
    assert previous['stations'][2]['streamId'] == interrupted['stream_id']
    assert previous['summary']['online'] == 0 and previous['stations'][3]['streamId'] is None
    assert previous['range'] == dict(kind='date',date='2026-11-06',label='指定日期 · 2026-11-06',includesLive=False)
    assert all(s['historical'] for s in previous['stations'])
    for value, expected in [(overall,3),(today,1),(previous,2)]:
        for period in ('hour','shift'):
            assert sum(p['passed']+p['failed']+p['pending'] for p in value['trends'][period]) == expected
    assert snapshot(app, 'private', time_range='all')[0]['summary']['total'] == 1
    assert snapshot(app, 'admin', time_range='date', selected_date=date(2026,11,8))[0]['summary']['total'] == 0


def test_date_boundaries_and_selected_today_match_history(authenticated, monkeypatch):
    _, app, client = authenticated
    at = setup_clock(monkeypatch)
    monkeypatch.setattr('backend.inspection_history.now', lambda _: at)
    start = datetime(2026,11,7,tzinfo=LOCAL_TIME).timestamp()
    before = round_row(app, start-.001, verdict='FAIL')
    after = round_row(app, start, started_at=start-3600)
    # The finished cross-midnight round belongs only to its archive day.
    today = snapshot(app,'admin')[0]
    dated = snapshot(app,'admin',time_range='date',selected_date=date(2026,11,7))[0]
    previous = snapshot(app,'admin',time_range='date',selected_date=date(2026,11,6))[0]
    assert today['summary'] == dated['summary']
    assert previous['summary']['total'] == today['summary']['total'] == 1
    assert previous['stations'][0]['streamId'] == before['stream_id']
    assert today['stations'][0]['streamId'] == after['stream_id']
    assert previous['trends']['hour'][23]['failed'] == 1
    assert today['trends']['hour'][0]['passed'] == 1
    for day in ('2026-11-06','2026-11-07'):
        result = client.get('/api/v1/board/history',params={'date':day,'verdict':'ALL'}).json()
        assert result['total'] == 1


def test_range_cache_separation_and_deletion_invalidates_every_range(authenticated, monkeypatch):
    _, app, client = authenticated
    at = setup_clock(monkeypatch)
    monkeypatch.setattr('backend.board.time', SimpleNamespace(time=lambda:at,
        monotonic=time.monotonic, perf_counter=time.perf_counter))
    old = round_row(app,at-86400,verdict='FAIL')
    round_row(app,at-1)
    app.state.board.snapshot_ttl = 60
    def read(scope, day=None):
        params = dict(mode='detection',range=scope)
        if day: params['date'] = day
        response = client.get('/api/v1/board/snapshot',params=params)
        assert response.status_code == 200, response.text
        return response.json()
    assert read('all')['summary']['total'] == 2
    assert read('today')['summary']['total'] == 1
    assert read('date','2026-11-06')['summary']['total'] == 1
    assert read('date','2026-11-05')['summary']['total'] == 0
    before = app.state.board.hits
    assert read('all')['summary']['total'] == 2
    assert app.state.board.hits == before+1
    response = client.post('/api/v1/board/reset/selected',json=dict(confirmation='删除选中检测记录',
        password='test-password-12345', streamIds=[old['stream_id']]))
    assert response.status_code == 200
    assert len(app.state.board.entries) == 0
    assert read('all')['summary']['total'] == 1
    assert read('date','2026-11-06')['summary']['total'] == 0
    assert read('today')['summary']['total'] == 1


def test_range_midnight_rollover_keeps_all_and_fixed_date(authenticated, monkeypatch):
    _, app, _ = authenticated
    clock = [datetime(2026,11,8,23,59,59,tzinfo=LOCAL_TIME).timestamp()]
    monkeypatch.setattr('backend.board_detection.now',lambda _:clock[0])
    monkeypatch.setattr('backend.board.time', SimpleNamespace(time=lambda:clock[0],
        monotonic=time.monotonic, perf_counter=time.perf_counter))
    round_row(app,clock[0]-1)
    app.state.board.snapshot_ttl = 60
    def read(scope):
        return app.state.board.snapshot('admin','detection',time_range=scope,
            selected_date=date(2026,11,8) if scope=='date' else None)
    assert all(read(scope)['summary']['total']==1 for scope in ('all','today','date'))
    clock[0] += 2
    assert read('today')['summary']['total'] == 0
    assert read('all')['summary']['total'] == read('date')['summary']['total'] == 1
    assert read('date')['range']['includesLive'] is False


@pytest.mark.parametrize('query', [
    'mode=detection&range=unknown', 'mode=detection&range=date',
    'mode=detection&range=date&date=2026-02-30', 'mode=detection&range=date&date=9999-12-31',
    'mode=detection&range=date&date=11-08-2026', 'mode=detection&range=all&date=2026-11-08',
    'mode=production&range=all'])
def test_invalid_statistics_scope_rejected(authenticated, query):
    _, _, client = authenticated
    assert client.get('/api/v1/board/snapshot?'+query).status_code == 422
