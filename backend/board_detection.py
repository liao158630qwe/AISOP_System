"""Read-only detection-round board, independent of the workpiece workflow."""
from collections import Counter
from datetime import datetime

from sqlalchemy import and_, case, cast, func, Integer, or_, select

from .board import LOCAL_TIME, categories, event_summary, rate, recent_events
from .db import inspection_visible, now, station_inspections as inspections
from .pilot_config import STATIONS
from .inspection_policy import complete_visual_coverage

TASKS = {'screw': '螺丝专项检测', 'sticker': '贴条专项检测', 'pose': '人体姿态检测',
         'scene': '场景工艺检测', 'manual': '人工检查'}
SOURCES = {'device': '设备采集', 'camera': '现场摄像头', 'replay': '视频联调', 'image': '图片联调'}


def assessment_columns():
    """One effective verdict for dashboard aggregates and archived history."""
    compliance, review = inspections.c.result['compliance'], inspections.c.result['manualReview']
    reviewed = and_(inspections.c.status == 'FINISHED', review['source'].as_string() == 'manual',
                    review['status'].as_string().in_(('PASS', 'FAIL')))
    def field(name):
        return case((reviewed, review[name].as_string()), else_=compliance[name].as_string())
    status, label = field('status'), field('label')
    verdict = case((and_(inspections.c.status == 'FINISHED', status.in_(('PASS', 'FAIL'))), status),
                   else_='REVIEW')
    reason = func.coalesce(func.nullif(field('reason'), ''), label)
    return verdict, label, reason, reviewed


def progress(result):
    sequence = result.get('sequence') or {}
    compliance = result.get('compliance') or {}
    # Screw positionsConfirmed includes confirmed EMPTY holes. Installation
    # progress must count completed operations and present preassembled parts.
    source = sequence if sequence.get('operationsExpected') is not None else compliance
    if source.get('operationsExpected') is not None:
        expected = source['operationsExpected'] + source.get('preassembledExpected', 0)
        confirmed = source.get('operationsCompleted', 0) + source.get('preassembledConfirmed', 0)
    else:
        source = sequence if sequence.get('positionsExpected') is not None else compliance
        confirmed, expected = source.get('positionsConfirmed'), source.get('positionsExpected')
    return dict(confirmed=confirmed, expected=expected)


def snapshot(app, owner, *, aggregates=None, live_memory=None, time_range='today', selected_date=None):
    # Read live state first: a subsequently archived SQL row takes precedence,
    # so a finish occurring during this snapshot cannot count the round twice.
    live_all = app.state.terminals.statuses(owner)
    active_ids = [s['streamId'] for s in live_all.values() if s.get('streamId')
                  and s['state'] in ('LIVE', 'STALE', 'FINISHED')]
    with app.state.engine.connect() as connection:
        timestamp = now(connection)
        local = datetime.fromtimestamp(timestamp, LOCAL_TIME)
        day = selected_date if time_range == 'date' else local.date()
        start = datetime.combine(day, datetime.min.time(), LOCAL_TIME).timestamp()
        end = start + 86400
        include_live = time_range == 'all' or day == local.date()
        archived_at = func.coalesce(inspections.c.finished_at, inspections.c.started_at)
        scope = [inspections.c.owner == owner, inspection_visible(), inspections.c.station_id.in_(STATIONS),
                 inspections.c.started_at <= timestamp,
                 or_(inspections.c.finished_at.is_(None), inspections.c.finished_at <= timestamp)]
        if time_range != 'all':
            # Finished rounds belong to their archive date, matching daily history.
            # Only today's view may include a currently running cross-day round.
            ended = and_(inspections.c.status != 'RUNNING', archived_at >= start, archived_at < end)
            current = and_(inspections.c.status == 'RUNNING',
                or_(inspections.c.started_at >= start, inspections.c.stream_id.in_(active_ids)))
            scope.append(or_(ended, current) if include_live else ended)
        verdict, assessment_label, assessment_reason, reviewed = assessment_columns()
        counts = {identity: Counter() for identity in STATIONS}
        for row in connection.execute(select(inspections.c.station_id, verdict.label('verdict'),
                                              func.count().label('amount')).where(*scope).group_by(
                                                  inspections.c.station_id, verdict)).mappings():
            counts[row['station_id']][row['verdict']] += row['amount']

        # Only scalar JSON fields from the latest row of each of six stations.
        result = inspections.c.result
        compliance = result['compliance']
        sequence = result['sequence']
        def progress_column(confirmed):
            operation = 'operationsCompleted' if confirmed else 'operationsExpected'
            preassembled = 'preassembledConfirmed' if confirmed else 'preassembledExpected'
            position = 'positionsConfirmed' if confirmed else 'positionsExpected'
            return case(*[(source['operationsExpected'].as_integer().is_not(None),
                           func.coalesce(source[operation].as_integer(), 0)
                           + func.coalesce(source[preassembled].as_integer(), 0))
                          for source in (sequence, compliance)],
                        (sequence['positionsExpected'].as_integer().is_not(None), sequence[position].as_integer()),
                        else_=compliance[position].as_integer())
        confirmed, expected = progress_column(True), progress_column(False)
        latest = select(inspections.c.station_id, inspections.c.stream_id, inspections.c.status,
            inspections.c.started_at, inspections.c.finished_at, inspections.c.reason.label('recordReason'),
            inspections.c.context['cameraId'].as_string().label('cameraId'),
            inspections.c.context['analysisType'].as_string().label('analysisType'),
            inspections.c.context['inputSource'].as_string().label('inputSource'),
            verdict.label('verdict'), assessment_label.label('label'),
            assessment_reason.label('reason'), reviewed.label('reviewed'),
            confirmed.label('confirmed'), expected.label('expected'),
            func.row_number().over(partition_by=inspections.c.station_id,
                order_by=(inspections.c.started_at.desc(), inspections.c.stream_id.desc())).label('rank')
            ).where(*scope).subquery()
        records = {r['station_id']: dict(r) for r in connection.execute(
            select(latest).where(latest.c.rank == 1)).mappings()}

        hours = [Counter() for _ in range(24)]
        shifts = [Counter() for _ in range(3)]
        # UTC+08:00 hour of day; all-time views combine the same hour/shift
        # across every date without fetching individual records or JSON blobs.
        hour = (cast(func.floor(archived_at / 3600), Integer) + 8) % 24
        for row in connection.execute(select(hour.label('hour'), verdict.label('verdict'),
                func.count().label('amount')).where(*scope, inspections.c.status != 'RUNNING',
                    archived_at <= timestamp
                ).group_by(hour, verdict)).mappings():
            hours[row['hour']][row['verdict']] += row['amount']
            shifts[row['hour'] // 8][row['verdict']] += row['amount']

        alarms = select(inspections.c.stream_id.label('id'), archived_at.label('at'),
            inspections.c.station_id.label('stationId'),
            func.coalesce(inspections.c.context['cameraId'].as_string(), '检测轮次').label('partNumber'),
            func.coalesce(assessment_reason, '算法检测异常').label('reason'),
            # Same event ID for a live failure and its eventual archived FAIL.
            func.coalesce(assessment_label, '算法归档').label('label'),
            case((reviewed, '人工复核'), else_='算法归档').label('source')
            ).where(*scope, inspections.c.status == 'FINISHED',
                archived_at <= timestamp,
                verdict == 'FAIL').subquery()
        queries = (select(alarms.c.id, alarms.c.at, alarms.c.stationId, alarms.c.partNumber,
                          alarms.c.reason, alarms.c.source),)
        alert_count, top = event_summary(connection, queries)
        alerts = recent_events(connection, queries)

    stations = []
    for identity, config in STATIONS.items():
        live = live_all.get(identity, {'state': 'OFFLINE', 'reason': '尚未登记工位设备'})
        if not include_live:
            live = {'state': 'OFFLINE', 'reason': '所选日期暂无检测记录'}
        record = records.get(identity)
        count = counts[identity]
        current = bool(include_live and record and record['status'] == 'RUNNING' and live.get('streamId') == record['stream_id'])
        online = current and live['state'] == 'LIVE'
        live_result = (live.get('result') or {}) if online else {}
        assessment = live_result.get('compliance') or {}
        current_verdict = assessment.get('status') if assessment.get('status') in ('PASS', 'FAIL') else 'REVIEW'
        if live.get('gapCount') and current_verdict == 'PASS' and not complete_visual_coverage(live_result):
            current_verdict = 'REVIEW'
        if current:
            count['REVIEW'] -= 1
            count[current_verdict] += 1
        archived = bool(record and record['status'] != 'RUNNING')
        effective_verdict = current_verdict if current else record['verdict'] if archived else 'REVIEW'
        reason = (assessment.get('reason') or live.get('reason') or '等待检测采样') if current else (
            record['reason'] or record['recordReason'] or '本轮检测已结束' if archived else live.get('reason', '等待检测采样'))
        if current and live.get('gapCount') and current_verdict == 'REVIEW':
            reason = f"{live['gapCount']} 处采样缺口，等待复核"
        review_label = '待复核' if live.get('gapCount') or not online else '待确认'
        label = ({'PASS': '合规', 'FAIL': '异常', 'REVIEW': review_label}[effective_verdict]
                 if current else ('上轮' if include_live else '归档') + {'PASS': '合规', 'FAIL': '异常', 'REVIEW': '待复核'}[effective_verdict]
                 if archived else '等待采样' if include_live else '暂无记录')
        if archived and record['reviewed']:
            label = record['label']
        status = ('ALARM' if online and effective_verdict == 'FAIL' else
                  'REVIEW' if current and (not online or effective_verdict == 'REVIEW') else
                  'RUNNING' if online else 'OFFLINE')
        p = progress(live_result) if online else dict(
            confirmed=record['confirmed'] if archived else None, expected=record['expected'] if archived else None)
        kind = live.get('analysisType') if current else record['analysisType'] if record else None
        camera = live.get('cameraId') if current else record['cameraId'] if record else None
        source = record['inputSource'] if record else None
        stations.append(dict(id=identity, name=config['name'], title=TASKS.get(kind, '等待检测任务'),
            status=status, online=online, reason=reason, stage=reason, partNumber=None, cameraId=camera,
            analysisType=kind, streamId=record['stream_id'] if record else None,
            inputSource=source, sourceLabel=SOURCES.get(source, '来源未记录' if record else '等待输入'),
            detection=dict(status=effective_verdict, label=label, archived=archived,
                           reviewed=bool(archived and record['reviewed']),
                           recordStatus=record['status'] if record else None,
                           frameSeq=live_result.get('frameSeq'), **p),
            passed=count['PASS'], failed=count['FAIL'], pending=count['REVIEW'], total=sum(count.values()),
            complianceRate=rate(count['PASS'], count['FAIL']), effectiveFps=live.get('effectiveFps', 0) if current else 0,
            mode='检测联动', historical=not include_live, steps=[]))
        if online and effective_verdict == 'FAIL':
            kinds = categories(reason)
            alerts.append(dict(id=record['stream_id'], at=record['started_at'], stationId=identity,
                partNumber=camera or '检测轮次', reason=reason, types=kinds, source='实时检测'))
            alert_count += 1
            for item in top:
                if item['name'] in kinds:
                    item['count'] += 1
    total = sum(counts.values(), Counter())
    def point(label, bucket):
        return dict(label=label, passed=bucket['PASS'], failed=bucket['FAIL'], pending=bucket['REVIEW'],
                    rate=rate(bucket['PASS'], bucket['FAIL']))
    alerts.sort(key=lambda a: (a['at'], a['id']), reverse=True)
    value = dict(mode='detection', generatedAt=timestamp, statisticsAt=timestamp,
        date=local.strftime('%Y-%m-%d'), timezone='UTC+08:00', pollAfterMs=1000,
        range=dict(kind=time_range, date=None if time_range == 'all' else day.isoformat(),
                   label='总体 · 全部日期' if time_range == 'all' else
                         ('当日 · ' if time_range == 'today' else '指定日期 · ') + day.isoformat(),
                   includesLive=include_live),
        summary=dict(total=sum(total.values()), passed=total['PASS'], failed=total['FAIL'], pending=total['REVIEW'],
                     complianceRate=rate(total['PASS'], total['FAIL']),
                     online=sum(s['online'] for s in stations),
                     active=sum(r['status'] == 'RUNNING' for r in records.values())),
        stations=stations, alerts=alerts[:100], alertCount=alert_count, top=sorted(top, key=lambda x: -x['count']),
        trends=dict(hour=[point(f'{h:02}:00', bucket) for h, bucket in enumerate(hours)],
                    shift=[point(label, bucket) for label, bucket in zip(('夜班 00–08', '早班 08–16', '晚班 16–24'), shifts)]))
    return value, {'date': value['date']}
