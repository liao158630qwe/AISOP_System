"""Daily inspection archives and reversible deletion, independent of host timezone."""
from datetime import date as Date, datetime, time, timedelta
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, case, delete, func, or_, select

from .board import LOCAL_TIME, rate
from .board_detection import SOURCES, TASKS, assessment_columns
from .db import (alert_evidence, now, serialized_write, station_inspections as inspections,
                 station_inspection_deletions as deletions, station_stream_leases as leases,
                 station_terminals as bindings)
from .pilot_config import STATIONS
from .evidence import realtime_source_key

StationID = Literal['station-1', 'station-2', 'station-3', 'station-4', 'station-5', 'station-6']
VERDICT_LABELS = {'FAIL': '不合规', 'PASS': '合规', 'REVIEW': '待复核'}


def projection():
    verdict, label, reason, reviewed = assessment_columns()
    # Lists never select context/result JSON blobs. They can contain thousands
    # of detections; their scalar fields and counters are projected in SQL.
    return (inspections.c.stream_id.label('streamId'), inspections.c.station_id.label('stationId'),
        inspections.c.status, inspections.c.started_at.label('startedAt'),
        inspections.c.finished_at.label('finishedAt'), inspections.c.reason.label('recordReason'),
        inspections.c.context['analysisType'].as_string().label('analysisType'),
        inspections.c.context['cameraId'].as_string().label('cameraId'),
        inspections.c.context['inputSource'].as_string().label('inputSource'),
        verdict.label('verdict'), label.label('assessmentLabel'), reason.label('assessmentReason'),
        reviewed.label('reviewed'), deletions.c.deleted_at.label('deletedAt'),
        deletions.c.deleted_by.label('deletedBy'))


def history_source():
    return inspections.outerjoin(deletions, and_(deletions.c.stream_id == inspections.c.stream_id,
                                                deletions.c.owner == inspections.c.owner))


def public_item(row):
    item = dict(row)
    assessment_label = item.pop('assessmentLabel')
    assessment_reason = item.pop('assessmentReason')
    record_reason = item.pop('recordReason')
    item['stationName'] = STATIONS[item['stationId']]['name']
    item['taskLabel'] = TASKS.get(item['analysisType'], '检测任务')
    item['sourceLabel'] = SOURCES.get(item['inputSource'], '来源未记录')
    item['reviewed'] = bool(item['reviewed'])
    # An interrupted round cannot inherit a provisional PASS label or reason.
    item['label'] = (assessment_label if item['reviewed'] else None) or VERDICT_LABELS[item['verdict']]
    item['reason'] = ((record_reason or '本轮检测中断，请复核') if item['status'] == 'INTERRUPTED'
                      else assessment_reason or record_reason or '未记录判定原因')
    return item


def reconcile_expired(app, owner):
    # Finished history stays read-only. Only stations with orphaned/expired
    # RUNNING rows enter short write transactions, where leases are rechecked.
    with app.state.engine.connect() as connection:
        timestamp = now(connection)
        candidates = connection.execute(select(inspections.c.station_id, inspections.c.stream_id).select_from(
            inspections.outerjoin(leases, inspections.c.station_id == leases.c.station_id)).where(
                inspections.c.owner == owner, inspections.c.status == 'RUNNING',
                inspections.c.station_id.in_(STATIONS),
                or_(leases.c.stream_id.is_(None), leases.c.stream_id != inspections.c.stream_id,
                    leases.c.closed.is_(True), leases.c.lease_until <= timestamp))).all()
    stations = {}
    for station, identity in candidates:
        stations.setdefault(station, []).append(identity)
    for station, identities in stations.items():
        with serialized_write(app.state.engine) as connection:
            # Match _lease's lock order; a newly started detector cannot race
            # between reading its lease and checking RUNNING inspection rows.
            connection.execute(select(bindings.c.station_id).where(bindings.c.station_id == station,
                bindings.c.owner == owner).with_for_update()).first()
            # Also constrain orphaned stations without a binding to the exact
            # rows observed earlier, never a subsequently created live round.
            app.state.terminals.reconcile(connection, station, owner=owner, stream_ids=identities)
    if candidates:
        app.state.board.invalidate(owner)


def register(app, auth):
    router = APIRouter(prefix='/api/v1/board/history')

    def actor(request):
        user = auth(request)
        if user.get('role') == 'terminal':
            raise HTTPException(403, '工位终端不能访问全局检测历史')
        return user

    def response(payload):
        return JSONResponse(payload, headers={'Cache-Control': 'no-store'})

    @router.get('')
    def history(request: Request,
                history_date: str | None = Query(None, alias='date', pattern=r'^\d{4}-\d{2}-\d{2}$'),
                station_id: StationID | None = Query(None, alias='stationId'),
                verdict: Literal['FAIL', 'PASS', 'REVIEW', 'ALL'] = 'FAIL', deleted: bool = False,
                limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000)):
        user = actor(request)
        reconcile_expired(app, user['owner'])
        with app.state.engine.connect() as connection:
            try:
                day = Date.fromisoformat(history_date) if history_date else datetime.fromtimestamp(
                    now(connection), LOCAL_TIME).date()
                start = datetime.combine(day, time.min, LOCAL_TIME).timestamp()
                end = datetime.combine(day + timedelta(days=1), time.min, LOCAL_TIME).timestamp()
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, '请选择有效的历史日期') from None
            archived_at = func.coalesce(inspections.c.finished_at, inspections.c.started_at)
            conditions = [inspections.c.owner == user['owner'], inspections.c.station_id.in_(STATIONS),
                          inspections.c.status.in_(('FINISHED', 'INTERRUPTED')),
                          archived_at >= start, archived_at < end]
            if station_id:
                conditions.append(inspections.c.station_id == station_id)
            effective_verdict = assessment_columns()[0]
            source = history_source()
            counts = {'PASS': 0, 'FAIL': 0, 'REVIEW': 0}
            deleted_count = 0
            selected_count = 0
            for row in connection.execute(select(effective_verdict.label('verdict'),
                    deletions.c.stream_id.is_not(None).label('deleted'), func.count().label('amount'))
                    .select_from(source).where(*conditions).group_by(
                        effective_verdict, deletions.c.stream_id.is_not(None))).mappings():
                if row['deleted']:
                    deleted_count += row['amount']
                else:
                    counts[row['verdict']] += row['amount']
                if bool(row['deleted']) == deleted and (verdict == 'ALL' or row['verdict'] == verdict):
                    selected_count += row['amount']
            conditions.append(deletions.c.stream_id.is_not(None) if deleted else deletions.c.stream_id.is_(None))
            if verdict != 'ALL':
                conditions.append(effective_verdict == verdict)
            priority = case((effective_verdict == 'FAIL', 0), (effective_verdict == 'REVIEW', 1), else_=2)
            rows = connection.execute(select(*projection()).select_from(source).where(*conditions)
                .order_by(priority, archived_at.desc(), inspections.c.stream_id.desc())
                .offset(offset).limit(limit + 1)).mappings().all()
        return response(dict(date=day.isoformat(), timezone='UTC+08:00',
            items=[public_item(row) for row in rows[:limit]],
            summary=dict(total=sum(counts.values()), failed=counts['FAIL'], passed=counts['PASS'],
                         pending=counts['REVIEW'], complianceRate=rate(counts['PASS'], counts['FAIL'])),
            total=selected_count, hasMore=len(rows) > limit, offset=offset, limit=limit, deletedCount=deleted_count))

    @router.get('/{stream_id}')
    def detail(stream_id: uuid.UUID, request: Request):
        user = actor(request)
        with app.state.engine.connect() as connection:
            row = connection.execute(select(*projection(), inspections.c.context, inspections.c.result)
                .select_from(history_source()).where(inspections.c.stream_id == str(stream_id),
                    inspections.c.owner == user['owner'], inspections.c.station_id.in_(STATIONS),
                    inspections.c.status.in_(('FINISHED', 'INTERRUPTED')))).mappings().first()
            if not row:
                raise HTTPException(404, '检测历史不存在')
            # Older archives used the station/session hash as source_id. Keep
            # them discoverable without rewriting immutable evidence manifests.
            sources = (str(stream_id), realtime_source_key(
                'station:' + row['stationId'], str(stream_id), row['analysisType']))
            # Evidence paths and internal storage state never leave this API.
            evidence = [dict(item, video_url=f'/api/v1/alerts/{item["id"]}/video'
                            if item['status'] == 'READY' else None) for item in connection.execute(
                select(*[alert_evidence.c[name] for name in ('id', 'status', 'analysis_type', 'severity',
                    'reason', 'created_at', 'ready_at', 'frame_count', 'error', 'retain_until')]).where(
                        alert_evidence.c.owner == user['owner'], alert_evidence.c.source_kind == 'realtime',
                        alert_evidence.c.source_id.in_(sources))
                    .order_by(alert_evidence.c.created_at.desc(), alert_evidence.c.id.desc()).limit(100)).mappings()]
        return response(dict(item=public_item(row), evidence=evidence))

    def set_deleted(stream_id, request, deleted):
        user = actor(request)
        identity = str(stream_id)
        with serialized_write(app.state.engine) as connection:
            # Lock the existing inspection row: concurrent delete/restore calls
            # serialize even when no tombstone exists yet (PostgreSQL + SQLite).
            row = connection.execute(select(inspections.c.status).where(inspections.c.stream_id == identity,
                inspections.c.owner == user['owner'], inspections.c.station_id.in_(STATIONS))
                .with_for_update()).mappings().first()
            if not row:
                raise HTTPException(404, '检测历史不存在')
            if row['status'] not in ('FINISHED', 'INTERRUPTED'):
                raise HTTPException(409, '本轮检测尚未结束，不能删除或恢复')
            match = (deletions.c.stream_id == identity, deletions.c.owner == user['owner'])
            exists = connection.execute(select(deletions.c.stream_id).where(*match)).first()
            if deleted and not exists:
                connection.execute(deletions.insert().values(stream_id=identity, owner=user['owner'],
                    deleted_at=now(connection), deleted_by=user['owner']))
            elif not deleted and exists:
                connection.execute(delete(deletions).where(*match))
        app.state.board.invalidate(user['owner'])
        return response(dict(ok=True, streamId=identity, deleted=deleted))

    @router.delete('/{stream_id}')
    def remove(stream_id: uuid.UUID, request: Request):
        return set_deleted(stream_id, request, True)

    @router.post('/{stream_id}/restore')
    def restore(stream_id: uuid.UUID, request: Request):
        return set_deleted(stream_id, request, False)

    app.include_router(router)
