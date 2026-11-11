"""Explicit administrator deletion of selected or all detection board history."""
import hashlib
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from .db import (now, serialized_write, station_inspections as inspections,
                 station_inspection_deletions as deletions,
                 station_stream_leases as leases, station_terminals as bindings)
from .inspection_history import reconcile_expired
from .pilot_config import STATIONS


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmation: Literal['清空全部检测数据']
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    password: str = Field(min_length=1, max_length=200)


class SelectedDeleteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmation: Literal['删除选中检测记录']
    streamIds: list[uuid.UUID] = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


def scope(owner):
    return inspections.c.owner == owner, inspections.c.station_id.in_(STATIONS)


def lock_stations(connection, owner):
    stations = list(connection.execute(select(bindings.c.station_id).where(
        bindings.c.owner == owner, bindings.c.station_id.in_(STATIONS))
        .order_by(bindings.c.station_id).with_for_update()).scalars())
    connection.execute(select(leases.c.station_id).where(leases.c.station_id.in_(stations))
        .order_by(leases.c.station_id).with_for_update()).all()


def delete_records(connection, owner, identities):
    for offset in range(0, len(identities), 400):
        batch = identities[offset:offset+400]
        connection.execute(delete(deletions).where(deletions.c.owner == owner,
                                                   deletions.c.stream_id.in_(batch)))
        connection.execute(delete(inspections).where(*scope(owner), inspections.c.stream_id.in_(batch)))


def inventory(connection, owner, *, lock=False):
    query = select(inspections.c.stream_id, inspections.c.status).where(*scope(owner)).order_by(
        inspections.c.stream_id)
    rows = connection.execute(query.with_for_update() if lock else query).all()
    revision = hashlib.sha256(owner.encode())
    for row in rows:
        revision.update(('\n' + row.stream_id + ':' + row.status).encode())
    recycled = connection.scalar(select(func.count()).select_from(deletions).where(
        deletions.c.owner == owner, deletions.c.stream_id.in_(select(inspections.c.stream_id).where(*scope(owner)))))
    # An archived round can still own camera resources until its stop completes.
    active_stations = list(connection.execute(select(leases.c.station_id).where(
        leases.c.station_id.in_(STATIONS), leases.c.closed.is_(False), leases.c.lease_until > now(connection),
        leases.c.station_id.in_(select(bindings.c.station_id).where(bindings.c.owner == owner))
    ).order_by(leases.c.station_id)).scalars())
    active = sum(row.status == 'RUNNING' for row in rows)
    return dict(total=len(rows), recycled=recycled, archived=len(rows)-active, active=active,
                activeStations=active_stations, revision=revision.hexdigest()), [row.stream_id for row in rows]


def register(app, auth, verify_password):
    router = APIRouter(prefix='/api/v1/board/reset')

    def actor(request):
        user = auth(request)
        if user.get('role') == 'terminal':
            raise HTTPException(403, '仅管理员可以清理全局检测数据')
        return user

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    @router.get('')
    def preview(request: Request):
        user = actor(request)
        reconcile_expired(app, user['owner'])
        with app.state.engine.connect() as connection:
            summary, _ = inventory(connection, user['owner'])
        return response(summary)

    @router.post('')
    def reset(body: ResetRequest, request: Request):
        user = actor(request)
        verify_password(request, body.password)
        with serialized_write(app.state.engine) as connection:
            # Same order as detector start: binding -> lease -> inspection.
            # Lock all owned stations so no start can slip past the busy check.
            lock_stations(connection, user['owner'])
            summary, identities = inventory(connection, user['owner'], lock=True)
            if summary['active'] or summary['activeStations']:
                raise HTTPException(409, '仍有检测正在运行，请先结束所有工位检测，再刷新清空范围')
            if summary['total'] and summary['revision'] != body.revision:
                raise HTTPException(409, '检测记录已变化，请刷新清空范围并重新确认')
            # Delete only the previewed identities, including recycled records.
            # Batches keep SQLite bind counts bounded; one transaction is atomic.
            delete_records(connection, user['owner'], identities)
        app.state.board.invalidate(user['owner'])
        return response(dict(ok=True, deleted=summary['total'], recycled=summary['recycled'], streamIds=identities))

    @router.post('/selected')
    def delete_selected(body: SelectedDeleteRequest, request: Request):
        user = actor(request)
        verify_password(request, body.password)
        identities = sorted({str(identity) for identity in body.streamIds})
        with serialized_write(app.state.engine) as connection:
            lock_stations(connection, user['owner'])
            rows = connection.execute(select(inspections.c.stream_id, inspections.c.owner,
                inspections.c.station_id, inspections.c.status).where(inspections.c.stream_id.in_(identities))
                .order_by(inspections.c.stream_id).with_for_update()).mappings().all()
            # Validate the entire selection before touching any row. Missing IDs
            # are already deleted, so an identical retry cannot affect new data.
            if any(row['owner'] != user['owner'] or row['station_id'] not in STATIONS for row in rows):
                raise HTTPException(404, '所选记录不存在或无权操作，请刷新列表')
            active_lease = connection.execute(select(leases.c.stream_id).where(
                leases.c.stream_id.in_(identities), leases.c.closed.is_(False),
                leases.c.lease_until > now(connection))).first()
            if active_lease or any(row['status'] not in ('FINISHED', 'INTERRUPTED') for row in rows):
                raise HTTPException(409, '所选记录中仍有检测未结束，请结束对应轮次后再删除')
            delete_records(connection, user['owner'], [row['stream_id'] for row in rows])
        app.state.board.invalidate(user['owner'])
        return response(dict(ok=True, deleted=len(rows), alreadyDeleted=len(identities)-len(rows), streamIds=identities))

    app.include_router(router)
