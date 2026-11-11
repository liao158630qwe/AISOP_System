"""Bounded, read-only production snapshots; no inference or production writes."""
from collections import Counter, OrderedDict
from datetime import date as Date, datetime, timedelta, timezone
import hashlib
import threading
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Integer, and_, case, cast, func, literal, or_, select, tuple_, union, union_all

from .db import now, pilot_cycles as cycles, pilot_events as events
from .db import inspection_visible, station_inspections as inspections
from .pilot_config import STATIONS

LOCAL_TIME = timezone(timedelta(hours=8))
TYPES = ('漏螺丝', '缺卡扣', '未吹净', '漏接端子', '工序跳步', '其他异常')
ALIASES = {
    '漏螺丝': ('漏螺丝', '缺螺丝', '螺丝缺失', '螺丝漏装', 'missing screw'),
    '缺卡扣': ('缺卡扣', '卡扣缺失', '漏装卡扣', 'missing clip'),
    '未吹净': ('未吹净', '未吹气', '漏吹', '吹净不合格'),
    '漏接端子': ('漏接端子', '端子漏接', '端子未连接', '未接端子'),
    '工序跳步': ('工序跳步', '跳步', '工序顺序错误', '未完成前置'),
}


def categories(reason):
    text = (reason or '').lower()
    return [name for name, words in ALIASES.items() if any(word in text for word in words)] or ['其他异常']


def rate(passed, failed):
    return round(passed / (passed + failed) * 100, 1) if passed + failed else None


def process_query(owner, start, timestamp):
    """Return only grouped counters, never all cycles or their JSON evidence."""
    scope = (cycles.c.owner == owner, cycles.c.source == 'camera', cycles.c.station.in_(STATIONS))
    touched = union(*[select(cycles.c.product, cycles.c.part_number).where(*scope,
        column >= start, column <= timestamp) for column in (cycles.c.started_at, cycles.c.finished_at)]).cte('touched')
    verdict = case((or_(cycles.c.result == 'NG', cycles.c.review_status.in_(('confirmed_ng', 'needs_rework'))), 'FAIL'),
        (and_(cycles.c.stage == 'CLOSED', cycles.c.result == 'MANUAL_OK'), 'PASS'), else_='REVIEW').label('verdict')
    ranked = select(cycles.c.product, cycles.c.part_number, cycles.c.station, cycles.c.finished_at, verdict,
        func.row_number().over(partition_by=(cycles.c.product, cycles.c.part_number, cycles.c.station),
            order_by=(cycles.c.started_at.desc(), cycles.c.id.desc())).label('rank')).where(
                *scope, cycles.c.started_at <= timestamp,
                tuple_(cycles.c.product, cycles.c.part_number).in_(select(touched))).cte('ranked')
    latest = select(ranked).where(ranked.c.rank == 1).cte('latest')
    part = select(case((func.sum(case((latest.c.verdict == 'FAIL', 1), else_=0)) > 0, 'FAIL'),
        (and_(func.count() == len(STATIONS), func.sum(case((latest.c.verdict == 'PASS', 1), else_=0)) == len(STATIONS)), 'PASS'),
        else_='REVIEW').label('verdict')).group_by(latest.c.product, latest.c.part_number).cte('parts')
    summary = select(literal('parts').label('kind'), literal('').label('station'), literal(0).label('hour'),
        part.c.verdict, func.count().label('amount')).group_by(part.c.verdict)
    hour = cast(func.floor((latest.c.finished_at - start) / 3600), Integer)
    station = select(literal('process'), latest.c.station, hour, latest.c.verdict, func.count()).where(
        latest.c.finished_at >= start, latest.c.finished_at <= timestamp).group_by(latest.c.station, hour, latest.c.verdict)
    return union_all(summary, station)


def aggregate(connection, owner, timestamp):
    local = datetime.fromtimestamp(timestamp, LOCAL_TIME)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    stats = {station: Counter() for station in STATIONS}
    hours, shifts, totals = [Counter() for _ in range(24)], [Counter() for _ in range(3)], Counter()
    for row in connection.execute(process_query(owner, start, timestamp)).mappings():
        if row['kind'] == 'parts':
            totals[row['verdict']] += row['amount']
        else:
            stats[row['station']][row['verdict']] += row['amount']
            hours[row['hour']][row['verdict']] += row['amount']
            shifts[row['hour'] // 8][row['verdict']] += row['amount']
    passed, failed = sum(s['PASS'] for s in stats.values()), sum(s['FAIL'] for s in stats.values())

    def point(label, bucket):
        return dict(label=label, passed=bucket['PASS'], failed=bucket['FAIL'], pending=bucket['REVIEW'],
                    rate=rate(bucket['PASS'], bucket['FAIL']))

    return dict(date=local.strftime('%Y-%m-%d'), statisticsAt=timestamp, stats=stats,
        summary=dict(total=sum(totals.values()), passed=totals['PASS'], failed=totals['FAIL'], pending=totals['REVIEW'],
                     complianceRate=rate(passed, failed), processPassed=passed, processFailed=failed),
        trends=dict(hour=[point(f'{h:02}:00', bucket) for h, bucket in enumerate(hours)],
            shift=[point(label, bucket) for label, bucket in zip(('夜班 00–08', '早班 08–16', '晚班 16–24'), shifts)]))


def event_queries(owner, start, timestamp):
    """Project scalar JSON fields in SQL; do not deserialize inference payloads."""
    payload, compliance = events.c.payload, inspections.c.result['compliance']
    manual_reason = func.coalesce(func.nullif(payload['reason'].as_string(), ''), '人工确认异常，未填写违规类型')
    algorithm_reason = func.coalesce(func.nullif(compliance['reason'].as_string(), ''),
        func.nullif(compliance['label'].as_string(), ''), '算法检测异常，待人工复核')
    manual = select(events.c.id.label('id'), events.c.at.label('at'), cycles.c.station.label('stationId'),
        cycles.c.part_number.label('partNumber'), manual_reason.label('reason'), literal('人工确认').label('source')).join(
            cycles, events.c.cycle_id == cycles.c.id).where(events.c.owner == owner, cycles.c.owner == owner,
                cycles.c.source == 'camera', events.c.at >= start, events.c.at <= timestamp,
                or_(and_(events.c.kind == 'CLOSE', payload['outcome'].as_string() == 'NG'),
                    and_(events.c.kind == 'REVIEW', payload['disposition'].as_string().in_(('confirmed_ng', 'needs_rework')))))
    algorithm = select(inspections.c.stream_id.label('id'), inspections.c.finished_at.label('at'),
        inspections.c.station_id.label('stationId'), cycles.c.part_number.label('partNumber'),
        algorithm_reason.label('reason'), literal('算法归档').label('source')).join(cycles, inspections.c.cycle_id == cycles.c.id).where(
            inspections.c.owner == owner, inspection_visible(), cycles.c.owner == owner,
            cycles.c.source == 'camera', inspections.c.status == 'FINISHED',
            inspections.c.finished_at >= start, inspections.c.finished_at <= timestamp, compliance['status'].as_string() == 'FAIL')
    return manual, algorithm


def event_summary(connection, queries):
    source = union_all(*queries).subquery('alarms')
    reason = func.lower(source.c.reason)
    matches = [or_(*[reason.contains(word) for word in words]) for words in ALIASES.values()]
    counts = [func.coalesce(func.sum(case((match, 1), else_=0)), 0).label(f'count_{i}')
              for i, match in enumerate([*matches, ~or_(*matches)])]
    row = connection.execute(select(func.count().label('total'), *counts).select_from(source)).mappings().one()
    top = [dict(name=name, count=row[f'count_{i}']) for i, name in enumerate(TYPES)]
    return row['total'], sorted(top, key=lambda item: -item['count'])


def recent_events(connection, queries):
    # Limit each source before UNION, so only 200 compact rows can reach the merge.
    limited = [query.order_by(query.selected_columns.at.desc(), query.selected_columns.id.desc()).limit(100).subquery()
               for query in queries]
    source = union_all(*[select(item) for item in limited]).subquery('recent_alarms')
    return [dict(row, types=categories(row['reason'])) for row in connection.execute(
        select(source).order_by(source.c.at.desc(), source.c.id.desc()).limit(100)).mappings()]


def snapshot(app, owner, *, aggregates=None, live_memory=None):
    live_memory = live_memory if live_memory is not None else {}
    with app.state.engine.connect() as connection:
        timestamp = now(connection)
        local = datetime.fromtimestamp(timestamp, LOCAL_TIME)
        date = local.strftime('%Y-%m-%d')
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if aggregates is None or aggregates['date'] != date:
            aggregates = aggregate(connection, owner, timestamp)
        active = connection.execute(select(cycles.c.station, cycles.c.id, cycles.c.part_number, cycles.c.stage).where(
            cycles.c.owner == owner, cycles.c.source == 'camera', cycles.c.active_station.is_not(None))).mappings().all()
        queries = event_queries(owner, start, timestamp)
        alert_count, top = event_summary(connection, queries)
        alerts = recent_events(connection, queries)
    current = {row['station']: row for row in active}
    live_all = app.state.terminals.statuses(owner)
    stations = []
    for identity, config in STATIONS.items():
        live = live_all.get(identity, dict(state='OFFLINE', reason='尚未登记工位设备', reasonCode='UNREGISTERED'))
        cycle = current.get(identity)
        compliance = (live.get('result') or {}).get('compliance') or {}
        status = ('ALARM' if live['state'] == 'LIVE' and compliance.get('status') == 'FAIL' else
                  'REVIEW' if live['state'] == 'STALE' or live.get('gapCount') or (
                      live['state'] == 'LIVE' and compliance.get('status') == 'REVIEW') else
                  'RUNNING' if live['state'] == 'LIVE' else
                  'MANUAL' if cycle and live.get('reasonCode') in ('MANUAL', 'UNREGISTERED') else 'OFFLINE')
        count = aggregates['stats'][identity]
        reason = compliance.get('reason') or live.get('reason', '')
        if live.get('gapCount'):
            reason = f"{live['gapCount']} 处采样缺口，等待复核"
        stations.append(dict(id=identity, name=config['name'], title=config['title'], status=status, reason=reason,
            partNumber=cycle['part_number'] if cycle else None,
            stage=next((step['name'] for step in config['stages'] if cycle and step['id'] == cycle['stage']),
                       '等待结束判定' if cycle and cycle['stage'] == 'READY' else '等待工件'),
            passed=count['PASS'], failed=count['FAIL'], pending=count['REVIEW'], total=sum(count.values()),
            complianceRate=rate(count['PASS'], count['FAIL']), effectiveFps=live.get('effectiveFps', 0),
            mode='人工检查' if status == 'MANUAL' else '视觉检测', steps=[step['name'] for step in config['stages']]))
        if status == 'ALARM':
            reason = compliance.get('reason') or compliance.get('label') or '实时检测异常'
            kinds = categories(reason)
            fingerprint = (live.get('streamId'), live.get('cycleId'), tuple(kinds))
            previous = live_memory.get(identity)
            if not previous or previous['fingerprint'] != fingerprint:
                previous = dict(fingerprint=fingerprint, at=timestamp, id='live:' + hashlib.sha256(
                    repr((owner, identity, fingerprint, timestamp)).encode()).hexdigest()[:32])
                live_memory[identity] = previous
            alerts.append(dict(id=previous['id'], at=previous['at'], stationId=identity,
                partNumber=cycle['part_number'] if cycle else '未绑定工件', reason=reason, types=kinds, source='实时检测'))
            alert_count += 1
        elif live['state'] != 'STALE':
            live_memory.pop(identity, None)
    alerts.sort(key=lambda alert: (alert['at'], alert['id']), reverse=True)
    return dict(generatedAt=timestamp, statisticsAt=aggregates['statisticsAt'], date=date, timezone='UTC+08:00',
        summary=aggregates['summary'], stations=stations, alerts=alerts[:100], alertCount=alert_count,
        top=top, trends=aggregates['trends'], pollAfterMs=3000), aggregates


class BoardService:
    """Single-flight, bounded per-owner cache. Authentication always precedes access."""
    def __init__(self, app, snapshot_ttl=1.0, statistics_ttl=15.0, capacity=8):
        self.app, self.snapshot_ttl, self.statistics_ttl, self.capacity = app, snapshot_ttl, statistics_ttl, capacity
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.hits = self.builds = self.failures = self.busy = 0
        self.last_build_ms = 0

    def snapshot(self, owner, mode='production', *, time_range='today', selected_date=None):
        # Never let a slow database occupy every request thread with cache waiters.
        if not self.lock.acquire(timeout=.25):
            self.busy += 1
            raise HTTPException(503, '看板数据正在同步，请稍后重试', headers={'Retry-After': '2'})
        try:
            clock = time.monotonic()
            day = datetime.fromtimestamp(time.time(), LOCAL_TIME).strftime('%Y-%m-%d')
            key = owner if mode == 'production' else (owner, mode, time_range, selected_date)
            entry = self.entries.get(key)
            if entry and entry['day'] == day and clock < entry['expires']:
                self.hits += 1
                self.entries.move_to_end(key)
                return entry['value']
            aggregates = entry['aggregates'] if entry and entry['day'] == day and clock < entry['stats_expires'] else None
            memory = dict(entry['memory']) if entry and entry['day'] == day else {}
            started = time.perf_counter()
            try:
                builder = snapshot
                if mode == 'detection':
                    from .board_detection import snapshot as builder
                    value, updated = builder(self.app, owner, aggregates=aggregates, live_memory=memory,
                                             time_range=time_range, selected_date=selected_date)
                else:
                    value, updated = builder(self.app, owner, aggregates=aggregates, live_memory=memory)
            except Exception:
                self.failures += 1
                raise  # Fail explicitly; do not disguise old data with a new timestamp.
            finished = time.monotonic()
            self.last_build_ms = round((time.perf_counter() - started) * 1000, 2)
            self.builds += 1
            self.entries[key] = dict(value=value, aggregates=updated, memory=memory, day=value['date'],
                expires=finished+self.snapshot_ttl,
                stats_expires=entry['stats_expires'] if aggregates is updated else finished+self.statistics_ttl)
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
            return value
        finally:
            self.lock.release()

    def invalidate(self, owner):
        # Called after the history transaction commits. A concurrent snapshot
        # finishes under this same lock before its cached value is discarded.
        with self.lock:
            for key in list(self.entries):
                if key == owner or (isinstance(key, tuple) and key[0] == owner):
                    self.entries.pop(key, None)

    def metrics(self):
        return dict(cacheEntries=len(self.entries), cacheCapacity=self.capacity, hits=self.hits, builds=self.builds,
                    failures=self.failures, busy=self.busy, lastBuildMs=self.last_build_ms,
                    snapshotTtlSeconds=self.snapshot_ttl, statisticsTtlSeconds=self.statistics_ttl)


def register(app, auth):
    router = APIRouter(prefix='/api/v1/board')
    service = app.state.board = BoardService(app)

    @router.get('/snapshot')
    def read(request: Request, mode: Literal['production', 'detection'] = 'production',
             time_range: Literal['today', 'all', 'date'] = Query('today', alias='range'),
             selected_date: str | None = Query(None, alias='date', pattern=r'^\d{4}-\d{2}-\d{2}$')):
        user = auth(request)
        if user.get('role') == 'terminal':
            raise HTTPException(403, '工位终端不能读取全局看板')
        day = None
        if mode != 'detection' and (time_range != 'today' or selected_date is not None):
            raise HTTPException(422, '日期范围切换仅用于检测看板')
        if time_range == 'date':
            try:
                day = Date.fromisoformat(selected_date or '')
                # Reject dates that cannot form a complete day on this platform.
                datetime.combine(day + timedelta(days=1), datetime.min.time(), LOCAL_TIME).timestamp()
                datetime.combine(day, datetime.min.time(), LOCAL_TIME).timestamp()
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, '请选择有效的统计日期') from None
        elif selected_date is not None:
            raise HTTPException(422, '仅指定日期模式可以传入统计日期')
        started = time.perf_counter()
        payload = service.snapshot(user['owner'], mode, time_range=time_range, selected_date=day)
        return JSONResponse(payload, headers={'Cache-Control': 'no-store',
            'Server-Timing': f'board;dur={(time.perf_counter()-started)*1000:.2f}'})

    app.include_router(router)
