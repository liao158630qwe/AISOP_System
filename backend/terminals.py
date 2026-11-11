"""Registered station identities and isolated, bounded six-camera inference.

Bindings and lease epochs persist in SQL. Temporal state belongs to this API
process; after a restart callers must explicitly start a new stream. No model
is assumed to implement the manual station's entire process.
"""
import asyncio
import math
import re
import secrets
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from algorithms.video import AnalysisError

from .db import (digest, inspection_visible, now, serialized_write, station_terminals as bindings,
                 terminal_sessions, station_stream_leases as leases, station_inspections as inspections)
from .pilot_config import STATIONS
from .inspection_policy import complete_visual_coverage
from .station_scheduler import StationScheduler
from .stream_limits import StreamLimits

StationID = Literal['station-1', 'station-2', 'station-3', 'station-4', 'station-5', 'station-6']


class Binding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workstationId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,60}$')
    cameraId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,60}$')
    source: Literal['usb', 'rtsp'] = 'usb'
    analysisType: Literal['manual', 'pose', 'screw', 'sticker', 'scene'] = 'manual'
    sceneId: uuid.UUID | None = None
    sampleFps: float = Field(default=4, ge=.5, le=30, allow_inf_nan=False)
    maxSampleGapSeconds: float = Field(default=1, ge=.1, le=3, allow_inf_nan=False)
    previewUrl: str = Field(default='', max_length=500)
    enabled: bool = True
    revision: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def scene_contract(self):
        if (self.analysisType == 'scene') != (self.sceneId is not None):
            raise ValueError('场景检测须配置sceneId')
        # Use an authenticated same-origin media gateway; never expose camera passwords.
        if self.previewUrl and (not re.fullmatch(r'/media/[A-Za-z0-9_./-]+', self.previewUrl) or
                                any(p in ('.', '..') for p in self.previewUrl.split('/'))):
            raise ValueError('预览地址必须是同源/media/路径')
        return self


class TerminalLogin(BaseModel):
    credential: str = Field(min_length=32, max_length=200)


class StartStream(BaseModel):
    model_config = ConfigDict(extra='forbid')
    requestId: uuid.UUID
    replaces: uuid.UUID | None = None
    inputSource: Literal['device', 'camera', 'image', 'replay'] = 'device'


class StopStream(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    reason: str = Field(default='采集端停止本轮，未完成算法归档', min_length=2, max_length=1000)


class ReviewActor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account: str = Field(min_length=1, max_length=200)
    role: Literal['admin', 'terminal']
    reviewer: str = Field(min_length=1, max_length=200)
    credentialRevision: int | None = None


class ManualReview(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    requestId: uuid.UUID
    outcome: Literal['PASS', 'FAIL']
    reason: str = Field(default='', max_length=1000)
    expectedActor: ReviewActor | None = None


class RotateCredential(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)


def public_binding(row):
    return dict(stationId=row['station_id'], workstationId=row['workstation_id'], cameraId=row['camera_id'],
                revision=row['revision'], enabled=row['enabled'], **row['config'])


class Terminals:
    def __init__(self, app):
        self.app, self.engine, self.settings = app, app.state.engine, app.state.settings
        self.runtime_id = str(uuid.uuid4())
        # Every built-in specialist also calls the shared person model. Limit
        # dispatch to its capacity so parallel specialists cannot exhaust it.
        self.scheduler = StationScheduler(self.settings.station_queue_wait_ms,
            min(self.settings.station_inference_concurrency, self.settings.realtime_pool_size),
            group_limits={'pose': self.settings.realtime_pool_size,
                          'screw': self.settings.specialist_pool_size,
                          'sticker': self.settings.specialist_pool_size,
                          'scene': app.state.scene_realtime.concurrency})
        self.streams = {}
        self.uploading = set()
        self.changing = set()

    @asynccontextmanager
    async def control(self, station):
        if station in self.changing or self.scheduler.busy(station) or station in self.uploading:
            raise HTTPException(429, '工位仍有采样或控制操作，请稍后重试', headers={'Retry-After': '1'})
        self.changing.add(station)
        try:
            yield
        finally:
            self.changing.discard(station)

    def authenticate(self, request, restrict=True):
        key = request.headers.get('x-terminal-key')
        cookie = request.cookies.get('aisop_terminal')
        if not key and not cookie:
            return None
        with self.engine.connect() as c:
            if key:
                row = c.execute(select(bindings).where(bindings.c.credential_hash == digest(key),
                                                       bindings.c.enabled.is_(True))).mappings().first()
                csrf = ''
            else:
                session = c.execute(select(terminal_sessions).where(
                    terminal_sessions.c.token_hash == digest(cookie), terminal_sessions.c.expires_at > now(c))).mappings().first()
                row = c.execute(select(bindings).where(bindings.c.station_id == session['station_id'],
                    bindings.c.revision == session['revision'], bindings.c.enabled.is_(True))).mappings().first() if session else None
                csrf = session['csrf'] if session else ''
                if row and request.method not in ('GET', 'HEAD', 'OPTIONS'):
                    if not secrets.compare_digest(csrf, request.headers.get('x-csrf-token', '')):
                        raise HTTPException(403, {'code': 'SESSION_CSRF_INVALID', 'message': '终端会话已更新，请同步登录状态后重试'})
        if not row:
            raise HTTPException(401, '终端凭据无效或绑定已变更，请重新登记')
        path = request.url.path
        allowed = path.startswith(('/api/v1/pilot/', '/api/v1/station-streams', '/api/v1/stations/')) or path in (
            '/api/v1/terminal/context', '/api/v1/terminal/logout', '/api/v1/session', '/api/v1/system/gpu')
        if restrict and not allowed:
            raise HTTPException(403, '工位终端仅允许访问所属工位')
        return dict(owner=row['owner'], token_hash='terminal:' + row['credential_hash'], csrf=csrf,
                    role='terminal', station_id=row['station_id'], binding=public_binding(row))

    @staticmethod
    def require_station(user, station):
        if user.get('role') == 'terminal' and user['station_id'] != station:
            raise HTTPException(403, '该终端不属于此工位')

    def binding_for(self, user, station=None):
        station = station or user.get('station_id')
        if station is None:
            raise HTTPException(422, '请指定工位')
        self.require_station(user, station)
        with self.engine.connect() as c:
            row = c.execute(select(bindings).where(bindings.c.station_id == station,
                bindings.c.owner == user['owner'], bindings.c.enabled.is_(True))).mappings().first()
        if not row:
            raise HTTPException(404, '工位尚未启用设备绑定')
        return public_binding(row)

    def reconcile(self, c, station, *, owner=None, stream_ids=None):
        """Persist interrupted sessions after lease expiry/restart without fabricating a final PASS."""
        lease = c.execute(select(leases).where(leases.c.station_id == station).with_for_update()).mappings().first()
        conditions = [inspections.c.station_id == station, inspections.c.status == 'RUNNING']
        if owner is not None:
            conditions.append(inspections.c.owner == owner)
        if stream_ids is not None:
            conditions.append(inspections.c.stream_id.in_(stream_ids))
        active = c.execute(select(inspections.c.stream_id).where(*conditions)).scalars().all()
        for identity in active:
            if not lease or lease['stream_id'] != identity or lease['closed'] or lease['lease_until'] <= now(c):
                c.execute(update(inspections).where(inspections.c.stream_id == identity,
                    inspections.c.status == 'RUNNING').values(status='INTERRUPTED', finished_at=now(c),
                    reason='采集租约失效或服务重启，本轮未完成归档，请复核本轮检测'))

    @staticmethod
    def detection_fields(value):
        # Keep old SQL records intact while retiring their workpiece API fields.
        return {k: v for k, v in value.items() if k not in ('cycleId', 'product')} if value else value

    @staticmethod
    def record(row):
        return dict(streamId=row['stream_id'], stationId=row['station_id'],
            status=row['status'], reason=row['reason'], startedAt=row['started_at'],
            finishedAt=row['finished_at'], context=Terminals.detection_fields(row['context']),
            result=Terminals.detection_fields(row['result']))

    def saved(self, user, identity):
        with self.engine.connect() as c:
            row = c.execute(select(inspections).where(inspections.c.stream_id == str(identity),
                inspections.c.owner == user['owner'])).mappings().first()
        if row:
            self.require_station(user, row['station_id'])
        return row

    def archive(self, state, result=None, reason='采集端停止本轮，未完成算法归档'):
        with serialized_write(self.engine) as c:
            lease = c.execute(select(leases).where(leases.c.station_id == state['stationId']).with_for_update()).mappings().first()
            row = c.execute(select(inspections).where(inspections.c.stream_id == state['streamId'])).mappings().first()
            if row and row['status'] != 'RUNNING':
                if result is not None and row['status'] != 'FINISHED':
                    raise HTTPException(409, '本轮已中断，不能补记为完成')
                return self.detection_fields(row['result'])
            if not row:
                raise HTTPException(409, '本轮缺少归档记录，请重新建立检测流')
            if result is not None:
                if (not lease or lease['stream_id'] != state['streamId'] or lease['runtime_id'] != self.runtime_id or
                        lease['closed'] or lease['lease_until'] <= now(c)):
                    raise HTTPException(409, '采集租约已失效，不能提交完成判定')
                result = dict(result, archived=True, archivedAt=now(c), recordStatus='FINISHED')
            c.execute(update(inspections).where(inspections.c.stream_id == state['streamId']).values(
                status='FINISHED' if result is not None else 'INTERRUPTED', result=result,
                finished_at=now(c), reason='本轮算法结果已归档' if result is not None else reason))
            return result

    @staticmethod
    def review_actor(user):
        return dict(account=user['owner'], role=user.get('role', 'admin'),
            reviewer=user.get('station_id') or user['owner'],
            credentialRevision=user['binding']['revision'] if user.get('role') == 'terminal' else None)

    @staticmethod
    def review_allowed(row):
        result = row['result'] or {}
        return (row['status'] == 'FINISHED' and row['context'].get('analysisType') != 'pose'
            and result.get('compliance', {}).get('status') in ('FAIL', 'REVIEW')
            and not result.get('manualReview'))

    def inspection(self, user, identity):
        with self.engine.connect() as c:
            row = c.execute(select(inspections).where(inspections.c.stream_id == str(identity),
                inspections.c.owner == user['owner'], inspection_visible())).mappings().first()
        if not row:
            raise HTTPException(404, '检测记录不存在')
        self.require_station(user, row['station_id'])
        return dict(self.record(row), reviewAllowed=self.review_allowed(row), reviewActor=self.review_actor(user))

    def review(self, user, identity, body):
        with serialized_write(self.engine) as c:
            actor = self.review_actor(user)
            # Follow configuration/credential rotation lock order: binding, then inspection.
            if actor['role'] == 'terminal':
                current = c.execute(select(bindings).where(bindings.c.station_id == user['station_id'])
                    .with_for_update()).mappings().first()
                if (not current or not current['enabled'] or current['owner'] != user['owner']
                        or current['revision'] != actor['credentialRevision']
                        or user['token_hash'] != 'terminal:' + current['credential_hash']):
                    raise HTTPException(403, '设备凭据或权限已变化，已停止人工确认')
            row = c.execute(select(inspections).where(inspections.c.stream_id == str(identity),
                inspections.c.owner == user['owner'], inspection_visible()).with_for_update()).mappings().first()
            if not row:
                raise HTTPException(404, '检测记录不存在')
            self.require_station(user, row['station_id'])
            result = dict(row['result'] or {})
            if body.expectedActor is not None and body.expectedActor.model_dump() != actor:
                raise HTTPException(409, dict(code='REVIEW_ACTOR_CHANGED',
                    message='确认身份或设备权限已变化，已停止重试，请核对检测记录', record=self.record(row)))
            if row['status'] != 'FINISHED' or row['context'].get('analysisType') == 'pose':
                raise HTTPException(409, '请先完成装配检测归档，再进行人工确认')
            previous = result.get('manualReview')
            if previous:
                if (previous['requestId'] == str(body.requestId) and previous['status'] == body.outcome
                        and previous['reason'] == body.reason
                        and all(previous.get(key) == value for key, value in actor.items())):
                    return self.detection_fields(result)
                raise HTTPException(409, dict(code='REVIEW_CONFLICT',
                    message='本轮已完成人工确认，请核对服务器结果', record=self.record(row)))
            if result.get('compliance', {}).get('status') not in ('FAIL', 'REVIEW'):
                raise HTTPException(409, '本轮没有需要人工确认的异常或证据不足结果')
            result['manualReview'] = dict(source='manual', requestId=str(body.requestId), status=body.outcome,
                label='人工确认合规' if body.outcome == 'PASS' else '人工确认不合规', reason=body.reason,
                **actor, stationId=row['station_id'], reviewedAt=now(c))
            c.execute(update(inspections).where(inspections.c.stream_id == str(identity)).values(result=result))
            return self.detection_fields(result)

    def _lease(self, user, binding, body, context):
        station = binding['stationId']
        with serialized_write(self.engine) as c:
            # Bindings row exists and provides a per-station lock on PostgreSQL.
            current = c.execute(select(bindings).where(bindings.c.station_id == station).with_for_update()).mappings().one()
            if current['revision'] != binding['revision'] or not current['enabled']:
                raise HTTPException(409, '工位绑定已更新，请重新登录')
            existing_controlled = self.app.state.collector_control.start_guard(c, user, binding, body, context)
            if existing_controlled:
                return existing_controlled, False
            old = c.execute(select(leases).where(leases.c.station_id == station)).mappings().first()
            self.reconcile(c, station)
            if old and old['runtime_id'] != self.runtime_id and not old['closed'] and old['lease_until'] > now(c):
                raise HTTPException(409, '该相机由另一推理进程持有；请等待旧租约结束后重试')
            if old and old['runtime_id'] == self.runtime_id and not old['closed'] and old['lease_until'] > now(c):
                if old['request_id'] == str(body.requestId):
                    return dict(old), False
                if str(body.replaces) != old['stream_id']:
                    raise HTTPException(409, '该相机已有采集流，请结束原流后再开始')
            if old:
                c.execute(update(inspections).where(inspections.c.stream_id == old['stream_id'],
                    inspections.c.status == 'RUNNING').values(status='INTERRUPTED', finished_at=now(c),
                    reason='新采集流替换了未归档的原检测，请复核原检测轮次'))
            row = dict(station_id=station, stream_id=str(uuid.uuid4()), epoch=(old['epoch'] + 1 if old else 1),
                runtime_id=self.runtime_id, request_id=str(body.requestId), closed=False, context=context,
                created_at=now(c), lease_until=now(c)+30)
            if old:
                c.execute(update(leases).where(leases.c.station_id == station).values(**row))
            else:
                c.execute(leases.insert().values(**row))
            c.execute(inspections.insert().values(stream_id=row['stream_id'], station_id=station, owner=user['owner'],
                context=dict(context, streamId=row['stream_id'], streamEpoch=row['epoch']),
                status='RUNNING', reason='', started_at=row['created_at']))
            self.app.state.collector_control.started(c, user, row)
            return row, True

    async def start(self, user, body, station=None):
        binding = await run_in_threadpool(self.binding_for, user, station)
        station = binding['stationId']
        async with self.control(station):
            # Complete the SQL lease + in-memory state handoff even if the caller leaves.
            task = asyncio.create_task(self._start(user, body, binding))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _start(self, user, body, binding):
        station = binding['stationId']
        if binding['analysisType'] == 'manual':
            raise HTTPException(409, '该工位为人工检查，尚未配置AI检测任务')
        context = dict(binding)
        context['inputSource'] = body.inputSource
        pose_paths = None
        model_resource = None
        # Freeze declared model/scene assets before accepting the first frame.
        def revision():
            nonlocal pose_paths, model_resource
            kind = binding['analysisType']
            if kind == 'scene':
                snapshot = self.app.state.scenes.snapshot(binding['sceneId'], user['owner'])
                if model_resource is None:
                    model_resource = ('scene', self.app.state.scene_realtime.model_key(snapshot, user['owner']))
                return snapshot['sha256']
            if kind in ('screw', 'sticker'):
                from algorithms.screws import snapshot as screw_snapshot
                from algorithms.stickers import snapshot as sticker_snapshot
                frozen = (screw_snapshot if kind == 'screw' else sticker_snapshot)()
                return digest(str((frozen['config_hash'], sorted(frozen['assets'].items()))))
            try:
                from algorithms.asset_snapshot import signature
                if pose_paths is None:
                    if self.settings.realtime_backend == 'yolo':
                        pose_paths = [('pose', self.settings.model_path)]
                    else:
                        from algorithms.rtmlib_pose import model_files
                        pose_paths = model_files(self.settings.realtime_backend)
                return digest(str((self.settings.realtime_backend,
                    [(name, str(Path(path).resolve()), signature(path)) for name, path in pose_paths])))
            except (OSError, ImportError) as exc:
                raise HTTPException(503, '姿态模型资源未就绪，请检查服务器模型配置') from exc
        context['modelRevision'] = await run_in_threadpool(revision)
        row, created = await run_in_threadpool(self._lease, user, binding, body, context)
        existing = self.streams.get(station)
        if not created and existing and existing['streamId'] == row['stream_id']:
            return self.identity(existing)
        if existing:
            await run_in_threadpool(self.release_state, existing)
        state = dict(**row['context'], streamId=row['stream_id'], streamEpoch=row['epoch'],
            owner=user['owner'], key=('station:' + station, row['stream_id']), lastSeq=-1, lastTime=-1.,
            lastAccepted=None, lastAt=None, lastCaptureAt=None, result=None, preview=None, finished=False, gaps=[], completedTimes=deque(maxlen=512),
            created=time.monotonic(), revision_check=revision, model_resource=model_resource)
        state['limits'] = StreamLimits(self.settings.realtime_max_duration_seconds,
                                      self.settings.realtime_no_target_seconds)
        state['streamLimits'] = state['limits'].public()
        self.streams[station] = state
        return self.identity(state)

    @staticmethod
    def identity(state):
        return {k: state.get(k) for k in ('stationId', 'workstationId', 'cameraId', 'streamId',
                'streamEpoch', 'revision', 'analysisType', 'sceneId', 'modelRevision', 'streamLimits')}

    def get_stream(self, user, stream_id):
        state = next((s for s in self.streams.values() if s['streamId'] == str(stream_id)), None)
        if not state or state['owner'] != user['owner']:
            raise HTTPException(404, '检测流不存在或服务已重启，请建立新流')
        self.require_station(user, state['stationId'])
        if user.get('role') == 'terminal' and user['binding']['revision'] != state['revision']:
            raise HTTPException(409, '工位绑定已更新，请建立新流')
        if state['finished']:
            if state.get('autoStopReason'):
                raise HTTPException(410, state['autoStopReason'])
            raise HTTPException(409, '该检测流已结束')
        return state

    def service(self, state):
        return {'pose': self.app.state.realtime, 'screw': self.app.state.screw_realtime,
                'sticker': self.app.state.sticker_realtime, 'scene': self.app.state.scene_realtime}[state['analysisType']]

    def release_state(self, state):
        self.service(state).release(state['key'])
        self.app.state.evidence.release(*state['key'])
        self.app.state.realtime_transport.forget(*state['key'])

    def enforce_limits(self, state, result=None):
        try:
            if result is None:
                state['limits'].check()
            else:
                state['limits'].observe(state['analysisType'], result)
        except HTTPException as exc:
            # Persist the interruption before releasing evidence and closing the lease.
            # This executes inside the station's serialized scheduler turn.
            self.archive(state, reason=exc.detail)
            with self.engine.begin() as c:
                c.execute(update(leases).where(leases.c.stream_id == state['streamId']).values(closed=True))
            state.update(finished=True, autoStopReason=exc.detail, preview=None, result=None)
            self.release_state(state)
            raise

    def renew(self, state):
        with self.engine.begin() as c:
            current = now(c)
            updated = c.execute(update(leases).where(leases.c.station_id == state['stationId'],
                leases.c.stream_id == state['streamId'], leases.c.runtime_id == self.runtime_id,
                leases.c.closed.is_(False), leases.c.lease_until > current).values(lease_until=current+30))
            if updated.rowcount != 1:
                raise HTTPException(409, '采集租约已失效，请建立新流')

    def protect(self, state, result):
        result = dict(result, stationEvidenceGaps=list(state['gaps']))
        if (state['gaps'] and result.get('compliance', {}).get('status') == 'PASS'
                and not complete_visual_coverage(result)):
            result['originalCompliance'] = result['compliance']
            result['compliance'] = dict(status='REVIEW', label='采样缺口待复核', reason='关键采样间隔超过工位配置')
        return result

    async def infer(self, state, payload, seq, stamp, frame_id, wait, received, received_perf):
        # Runs only inside the scheduler, independent of HTTP client cancellation.
        service = self.service(state)
        registry = service.registry if state['analysisType'] in ('screw', 'sticker') else service.streams
        def work():
            # Keep SQL, model revision checks and inference in one worker turn.
            # These are service wall times, not individual CUDA kernel timings.
            previous_stage = time.perf_counter()
            stages = dict(beforeWorkerMs=round((previous_stage-received_perf)*1000, 3))
            def mark(name):
                nonlocal previous_stage
                current = time.perf_counter()
                stages[name] = round((current-previous_stage)*1000, 3)
                previous_stage = current
            self.enforce_limits(state)
            self.renew(state)
            if state['revision_check']() != state['modelRevision']:
                raise HTTPException(409, '模型或规则已变更，请结束本轮并重新建立流')
            mark('precheckMs')
            # Include upload, authorization, SQL checks and thread-pool wait.
            # Expensive inference must not start on a frame that is already stale.
            if (time.monotonic()-received)*1000 >= self.scheduler.max_wait_ms:
                raise HTTPException(429, '采样已超时，请发送最新采样', headers={'Retry-After': '1'})
            extra = dict(stream_key=state['key'], frame_time=stamp)
            if state['analysisType'] == 'scene':
                extra.update(scene_id=state['sceneId'], owner=state['owner'])
            result = service.predict(payload, **extra)
            self.enforce_limits(state, result)
            mark('modelMs')
            # A lease takeover invalidates a late GPU result.
            self.renew(state)
            if state['revision_check']() != state['modelRevision']:
                raise HTTPException(409, '模型或规则已变更，请结束本轮并重新建立流')
            mark('postcheckMs')
            previous = state['lastAccepted']
            start = previous if previous is not None else 0.
            arrival_gap = received-state['lastCaptureAt'] if state['lastCaptureAt'] is not None else 0.
            if max(stamp-start, arrival_gap) > state['maxSampleGapSeconds'] and len(state['gaps']) < 128:
                state['gaps'].append(dict(start=start, end=stamp, reason='工位有效采样间隔超限'))
            registry.record_sample(state['key'], stamp)
            result = self.protect(state, registry.protect_result(state['key'], result))
            result.update(**self.identity(state), frameSeq=seq, frameId=frame_id, frameTime=stamp,
                queueWaitMs=round(wait, 2), serverProcessingMs=round((time.perf_counter()-received_perf)*1000, 2))
            mark('resultRulesMs')
            try:
                proof = self.app.state.evidence.observe(state['owner'], state['key'][0], state['streamId'],
                    state['analysisType'], stamp, payload, result)
            except (OSError, SQLAlchemyError, ValueError):
                proof = dict(status='UNAVAILABLE', message='告警取证存储暂不可用')
            if proof:
                result['evidence'] = proof
            mark('evidenceMs')
            result['serviceTimingsMs'] = stages
            result['serverProcessingMs'] = round((time.perf_counter()-received_perf)*1000, 2)
            return result
        kind, transport = state['analysisType'], self.app.state.realtime_transport
        admission_key = (kind, *state['key'])
        rejection = transport.enter(kind, admission_key, stamp)
        if rejection:
            raise HTTPException(*rejection, headers={'Retry-After': '1'})
        status, started = 500, time.perf_counter()
        try:
            result = await run_in_threadpool(work)
            status = 200
        except HTTPException as exc:
            status = exc.status_code
            raise
        finally:
            transport.leave(kind, admission_key, stamp, status, (time.perf_counter()-started)*1000)
        # Publish result, preview and their clock together, without an await.
        # Refreshing the clock in the worker while evidence IO is still running
        # would make the previous frame's verdict appear current to the UI.
        if state.get('collectorInstanceId'):
            await run_in_threadpool(self.app.state.collector_control.publish, state, result, payload, received)
        else:
            state.update(result=result, preview=payload, lastAccepted=stamp,
                         lastAt=time.monotonic(), lastCaptureAt=received)
            state['completedTimes'].append(time.monotonic())
        return result

    def statuses(self, owner):
        """Read six stations with two SQL queries, scoped to the requesting owner."""
        with self.engine.connect() as c:
            rows = {row['station_id']: row for row in c.execute(select(bindings).where(bindings.c.owner == owner)).mappings()}
            current = set(c.execute(select(leases.c.stream_id).where(leases.c.station_id.in_(rows),
                leases.c.runtime_id == self.runtime_id, leases.c.closed.is_(False), leases.c.lease_until > now(c))).scalars())
        context = dict(bindings=rows, leases=current, scheduler=self.scheduler.snapshot()['stations'])
        return {station: self.status(station, _context=context) for station in rows}

    def status(self, station, *, _context=None):
        if _context is None:
            with self.engine.connect() as c:
                row = c.execute(select(bindings).where(bindings.c.station_id == station)).mappings().first()
        else:
            row = _context['bindings'].get(station)
        if not row or not row['enabled'] or row['config']['analysisType'] == 'manual':
            code, reason = ('UNREGISTERED', '尚未登记工位设备') if not row else (
                ('DISABLED', '工位已停用') if not row['enabled'] else ('MANUAL', '人工检查工位，未启用AI检测'))
            return dict(stationId=station, state='OFFLINE', effectiveFps=0, result=None, reasonCode=code, reason=reason)
        state = self.streams.get(station)
        if not state or state['finished']:
            return dict(stationId=station, state='OFFLINE', effectiveFps=0, result=None,
                        reasonCode='AUTO_STOPPED' if state and state.get('autoStopReason') else 'STOPPED' if state else 'NOT_STARTED',
                        reason=state.get('autoStopReason', '采集已停止') if state else '尚未开始采集')
        if _context is None:
            with self.engine.connect() as c:
                current = c.execute(select(leases.c.stream_id).where(leases.c.station_id == station,
                    leases.c.stream_id == state['streamId'], leases.c.runtime_id == self.runtime_id,
                    leases.c.closed.is_(False), leases.c.lease_until > now(c))).first()
        else:
            current = state['streamId'] in _context['leases']
        if not current:
            return dict(**self.identity(state), state='OFFLINE', effectiveFps=0, result=None,
                        reasonCode='LEASE_EXPIRED', reason='采集连接已失效，请停止本轮并重新连接')
        if state.get('finalResult'):
            return dict(**self.identity(state), state='FINISHED', effectiveFps=0, result=None,
                        reasonCode='ARCHIVED', reason='本轮算法结果已归档，等待释放采集流')
        if state.get('finalizing'):
            return dict(**self.identity(state), state='STALE', effectiveFps=0, result=None,
                        reasonCode='FINALIZING', reason='本轮已停止采样，正在等待归档确认；失败后可重试归档')
        clock = time.monotonic()
        age = (clock-state['lastCaptureAt'])*1000 if state['lastCaptureAt'] is not None else None
        elapsed = max(.001, min(10., clock-state['created']))
        fps = sum(t >= clock-10 for t in tuple(state['completedTimes'])) / elapsed
        fresh = age is not None and age <= 1000
        health = 'WARMING' if elapsed < 10 else ('ON_TARGET' if fps >= state['sampleFps']*.95 else 'BELOW_TARGET')
        return dict(**self.identity(state), state='LIVE' if age is not None and age <= 1000 else 'STALE',
            reasonCode='LIVE' if fresh else 'STALE' if age is not None else 'WAITING_FRAME',
            reason='正在接收检测结果' if fresh else '结果已过期，请检查相机和采集服务' if age is not None else '等待首张有效检测采样',
            targetFps=state['sampleFps'], samplingHealth=health, gapCount=len(state['gaps']),
            resultAgeMs=round(age, 1) if age is not None else None, effectiveFps=round(fps, 2),
            result=state['result'] if age is not None and age <= 1000 else None,
            scheduler=(_context['scheduler'] if _context is not None else self.scheduler.snapshot()['stations']).get(station, {}))


def register(app, auth):
    service = app.state.terminals
    router = APIRouter(prefix='/api/v1')

    async def actor(request):
        """Accept a station terminal session or the normal authenticated admin."""
        return await run_in_threadpool(auth, request)

    @router.get('/admin/terminals')
    async def catalog(request: Request):
        user = await run_in_threadpool(auth, request)
        def read():
            with service.engine.connect() as c:
                return [public_binding(row) for row in c.execute(select(bindings).where(
                    bindings.c.owner == user['owner']).order_by(bindings.c.station_id)).mappings()]
        return {'items': await run_in_threadpool(read), 'stations': list(STATIONS)}

    @router.put('/admin/terminals/{station}')
    async def configure(station: StationID, body: Binding, request: Request):
        user = await run_in_threadpool(auth, request)
        if service.scheduler.busy(station) or station in service.uploading:
            raise HTTPException(409, '工位仍在处理采样，请停止采集后更新绑定')
        credential = secrets.token_urlsafe(36)
        config = body.model_dump(mode='json', exclude={'workstationId', 'cameraId', 'enabled', 'revision'})
        if body.analysisType == 'scene' and body.enabled:
            await run_in_threadpool(service.app.state.scenes.snapshot, str(body.sceneId), user['owner'])
        def save():
            with serialized_write(service.engine) as c:
                old = c.execute(select(bindings).where(bindings.c.station_id == station).with_for_update()).mappings().first()
                if old and (old['owner'] != user['owner'] or old['revision'] != body.revision):
                    raise HTTPException(409, '绑定已更新，请刷新后重试')
                if not old and body.revision:
                    raise HTTPException(409, '绑定不存在，请刷新后重试')
                if old and (old['workstation_id'], old['camera_id'], old['enabled'], old['config']) == (
                        body.workstationId, body.cameraId, body.enabled, config):
                    return public_binding(old), False, False
                lease = c.execute(select(leases).where(leases.c.station_id == station)).mappings().first()
                if lease and not lease['closed'] and lease['lease_until'] > now(c):
                    raise HTTPException(409, '工位仍有有效采集流，请先停止采集再更新配置')
                service.reconcile(c, station)
                sampling_keys = {'sampleFps', 'maxSampleGapSeconds'}
                sampling_only = old is not None and (
                    old['workstation_id'], old['camera_id'], old['enabled'],
                    {k: v for k, v in old['config'].items() if k not in sampling_keys}) == (
                    body.workstationId, body.cameraId, body.enabled,
                    {k: v for k, v in config.items() if k not in sampling_keys})
                row = dict(station_id=station, owner=user['owner'], workstation_id=body.workstationId,
                    camera_id=body.cameraId, credential_hash=old['credential_hash'] if sampling_only else digest(credential),
                    revision=(old['revision']+1 if old else 1),
                    enabled=body.enabled, config=config, updated_at=now(c))
                if old:
                    c.execute(update(bindings).where(bindings.c.station_id == station).values(**row))
                else:
                    c.execute(bindings.insert().values(**row))
                if sampling_only:
                    # Keep current logins valid without reviving already stale sessions.
                    c.execute(update(terminal_sessions).where(terminal_sessions.c.station_id == station,
                        terminal_sessions.c.revision == old['revision']).values(revision=row['revision']))
                else:
                    c.execute(delete(terminal_sessions).where(terminal_sessions.c.station_id == station))
                c.execute(update(leases).where(leases.c.station_id == station).values(closed=True))
                return public_binding(row), True, not sampling_only
        async with service.control(station):
            try:
                result, changed, rotated = await run_in_threadpool(save)
            except IntegrityError as exc:
                raise HTTPException(409, '工位机或相机编号已绑定到其他工位') from exc
            state = service.streams.pop(station, None) if changed else None
            if state:
                await run_in_threadpool(service.release_state, state)
        return dict(binding=result, credential=credential if rotated else None, changed=changed,
                    message='凭据仅本次显示；更新绑定会使旧凭据失效' if rotated else
                    '采样配置已更新，下轮检测生效；原设备凭据和终端登录继续有效' if changed else
                    '配置未变化，原设备凭据继续有效')

    @router.post('/terminal/login')
    async def login(body: TerminalLogin):
        token, csrf = secrets.token_urlsafe(36), secrets.token_urlsafe(24)
        def create():
            with serialized_write(service.engine) as c:
                row = c.execute(select(bindings).where(bindings.c.credential_hash == digest(body.credential),
                    bindings.c.enabled.is_(True))).mappings().first()
                if not row:
                    raise HTTPException(401, '终端凭据无效')
                c.execute(delete(terminal_sessions).where(terminal_sessions.c.expires_at <= now(c)))
                c.execute(terminal_sessions.insert().values(token_hash=digest(token), station_id=row['station_id'],
                    revision=row['revision'], csrf=csrf, expires_at=now(c)+service.settings.session_seconds))
                return public_binding(row)
        binding = await run_in_threadpool(create)
        response = JSONResponse(dict(binding=binding, csrf=csrf))
        response.set_cookie('aisop_terminal', token, max_age=service.settings.session_seconds, httponly=True,
            secure=service.settings.secure_cookie, samesite='strict', path='/')
        response.delete_cookie('aisop_session', path='/')
        return response

    @router.post('/admin/terminals/{station}/credential')
    async def rotate(station: StationID, body: RotateCredential, request: Request):
        user = await actor(request)
        credential = secrets.token_urlsafe(36)
        def save():
            with serialized_write(service.engine) as c:
                row = c.execute(select(bindings).where(bindings.c.station_id == station,
                    bindings.c.owner == user['owner']).with_for_update()).mappings().first()
                if not row:
                    raise HTTPException(404, '工位尚未登记')
                if row['revision'] != body.revision:
                    raise HTTPException(409, '配置版本已变化，请刷新后重新核对')
                lease = c.execute(select(leases).where(leases.c.station_id == station)).mappings().first()
                if lease and not lease['closed'] and lease['lease_until'] > now(c):
                    raise HTTPException(409, '请先停止采集，再重新签发凭据')
                service.reconcile(c, station)
                updated = dict(row, credential_hash=digest(credential), revision=row['revision']+1, updated_at=now(c))
                c.execute(update(bindings).where(bindings.c.station_id == station).values(**updated))
                c.execute(delete(terminal_sessions).where(terminal_sessions.c.station_id == station))
                return public_binding(updated)
        async with service.control(station):
            binding = await run_in_threadpool(save)
        return dict(binding=binding, credential=credential, message='新凭据仅本次显示，旧凭据和会话已失效')

    @router.post('/terminal/logout')
    async def logout(request: Request):
        try:
            await run_in_threadpool(auth, request)
        except HTTPException as exc:
            # An expired/revoked cookie must still be removable; valid sessions need CSRF.
            if exc.status_code != 401:
                raise
        def remove():
            with service.engine.begin() as c:
                c.execute(delete(terminal_sessions).where(terminal_sessions.c.token_hash == digest(request.cookies.get('aisop_terminal', ''))))
        await run_in_threadpool(remove)
        response = JSONResponse({'ok': True})
        response.delete_cookie('aisop_terminal', path='/')
        return response

    @router.get('/terminal/context')
    async def context(request: Request):
        user = await run_in_threadpool(auth, request)
        if user.get('role') != 'terminal':
            return dict(role='admin', binding=None)
        return dict(role='terminal', csrf=user['csrf'], binding=user['binding'])

    @router.post('/station-streams', status_code=201)
    async def start(body: StartStream, request: Request, station: StationID | None = None):
        user = await actor(request)
        if request.headers.get('x-collector-instance') or request.headers.get('x-collector-command'):
            if not request.headers.get('x-terminal-key') or user.get('role') != 'terminal':
                raise HTTPException(403, '后台控制只接受采集器设备凭据')
            if not request.headers.get('x-collector-instance') or not request.headers.get('x-collector-command') or body.inputSource != 'device':
                raise HTTPException(422, '后台开始请求需要采集器实例、原命令编号和设备来源')
            user = dict(user, _collector=dict(instance=request.headers['x-collector-instance'],
                command=request.headers['x-collector-command']))
        return await service.start(user, body, station)

    @router.post('/station-streams/{stream_id}/frames')
    async def frame(stream_id: uuid.UUID, request: Request):
        received_perf = time.perf_counter()
        received = time.monotonic()
        user = await actor(request)
        state = service.get_stream(user, stream_id)
        station = state['stationId']
        await run_in_threadpool(app.state.collector_control.guard, user, state, request, 'frame')
        if state.get('finalResult') or state.get('finalizing'):
            raise HTTPException(409, '本轮已经完成判定，请释放后建立新流')
        if station in service.uploading or station in service.changing:
            raise HTTPException(429, '上一帧仍在上传', headers={'Retry-After': '1'})
        try:
            seq = int(request.headers.get('x-frame-seq', ''))
            stamp = float(request.headers.get('x-frame-time', ''))
            epoch = int(request.headers.get('x-stream-epoch', ''))
            if not 0 <= seq <= 2**53-1 or not math.isfinite(stamp) or not 0 <= stamp <= 86400:
                raise ValueError()
        except ValueError as exc:
            raise HTTPException(422, '需要有效帧序号、流代次和0–86400秒时间戳') from exc
        if epoch != state['streamEpoch'] or seq <= state['lastSeq'] or stamp <= state['lastTime']:
            raise HTTPException(409, '流代次或帧序号已过期，请发送新采样')
        for header, expected in [('x-station-id', station), ('x-camera-id', state['cameraId'])]:
            if header in request.headers and request.headers[header] != expected:
                raise HTTPException(403, '采样身份与工位绑定不一致')
        if request.headers.get('content-type', '').split(';')[0] != 'image/jpeg':
            raise HTTPException(415, '仅支持JPEG采样')
        length = request.headers.get('content-length')
        if length is not None:
            try:
                size = int(length)
                if size < 0:
                    raise ValueError()
            except ValueError as exc:
                raise HTTPException(422, '采样长度无效') from exc
            if size > 1024*1024:
                raise HTTPException(413, '采样不能超过1MiB')
        service.uploading.add(station)
        try:
            payload = bytearray()
            async def read():
                async for chunk in request.stream():
                    if len(payload) + len(chunk) > 1024*1024:
                        raise HTTPException(413, '采样不能超过1MiB')
                    payload.extend(chunk)
            try:
                await asyncio.wait_for(read(), timeout=5)
            except asyncio.TimeoutError as exc:
                raise HTTPException(408, '采样传输超时') from exc
            if service.streams.get(station) is not state or state['finished']:
                raise HTTPException(409, '检测流已变更')
            state['lastSeq'], state['lastTime'] = seq, stamp
        finally:
            service.uploading.discard(station)
        frame_id = f'{state["streamId"]}:{seq}'
        return await service.scheduler.submit(station, lambda wait: service.infer(
            state, bytes(payload), seq, stamp, frame_id, wait, received, received_perf),
            group=state['analysisType'], received_at=received, resource=state.get('model_resource'))

    @router.delete('/station-streams/{stream_id}')
    async def stop(stream_id: uuid.UUID, request: Request, body: StopStream | None = None):
        user = await actor(request)
        saved = await run_in_threadpool(service.saved, user, stream_id)
        if saved:
            await run_in_threadpool(app.state.collector_control.guard, user, saved['context'], request, 'stop')
        if saved and saved['status'] != 'RUNNING' and not any(s['streamId'] == str(stream_id) for s in service.streams.values()):
            def release_saved():
                with service.engine.begin() as c:
                    c.execute(update(leases).where(leases.c.stream_id == str(stream_id)).values(closed=True))
            await run_in_threadpool(release_saved)
            return dict(ok=True, streamId=str(stream_id), recordStatus=saved['status'])
        previous = next((s for s in service.streams.values() if s['streamId'] == str(stream_id)), None)
        if previous and previous['owner'] == user['owner'] and previous['finished']:
            service.require_station(user, previous['stationId'])
            return dict(ok=True, **service.identity(previous))
        state = service.get_stream(user, stream_id)
        station = state['stationId']
        def close():
            service.archive(state, reason=(body or StopStream()).reason)
            with service.engine.begin() as c:
                c.execute(update(leases).where(leases.c.station_id == station,
                    leases.c.stream_id == str(stream_id)).values(closed=True))
            service.release_state(state)
        async with service.control(station):
            await run_in_threadpool(close)
            state['finished'] = True
            state['preview'] = None
        return dict(ok=True, **service.identity(state))

    @router.post('/station-streams/{stream_id}/finish')
    async def finish(stream_id: uuid.UUID, request: Request):
        user = await actor(request)
        saved = await run_in_threadpool(service.saved, user, stream_id)
        if saved:
            await run_in_threadpool(app.state.collector_control.guard, user, saved['context'], request, 'finish')
        if saved and saved['status'] == 'FINISHED':
            return service.detection_fields(saved['result'])
        if saved and saved['status'] == 'INTERRUPTED':
            raise HTTPException(409, '本轮已中断，不能补记为完成')
        state = service.get_stream(user, stream_id)
        if state.get('finalResult'):
            return state['finalResult']
        async with service.control(state['stationId']):
            await run_in_threadpool(service.enforce_limits, state)
            await run_in_threadpool(service.renew, state)
            if state['lastAccepted'] is None:
                raise HTTPException(409, '尚无有效检测采样')
            def finalize():
                if state['revision_check']() != state['modelRevision']:
                    raise HTTPException(409, '模型或规则已变更，请中断本轮后重新开始')
                state['finalizing'] = True
                # Freeze once: retrying a failed SQL commit must not finish the
                # detector again or turn database downtime into a sampling gap.
                if 'pendingFinal' not in state:
                    detector = service.service(state)
                    if state['analysisType'] == 'pose':
                        result = dict(final=True, compliance=dict(status='REVIEW', label='观察结束', reason='人体姿态不判定工序通过'))
                    else:
                        result = detector.finish(state['key'])
                    registry = detector.registry if state['analysisType'] in ('screw', 'sticker') else detector.streams
                    result = registry.protect_result(state['key'], result, final=True)
                    gap = time.monotonic() - state['lastAt']
                    if gap > state['maxSampleGapSeconds'] and len(state['gaps']) < 128:
                        state['gaps'].append(dict(start=state['lastAccepted'], end=state['lastAccepted']+gap,
                                                 reason='结束前有效采样间隔超限'))
                    result = service.protect(state, result)
                    result.update(**service.identity(state), final=True, frameSeq=state['result']['frameSeq'])
                    result.pop('evidence', None)  # The final assessment gets its own receipt.
                    state['pendingFinal'] = result
                # Missing actions can first become FAIL at finish. Persist the
                # last accepted samples before the client releases this stream.
                # Pose-only observation has no process-compliance assessment.
                if state['analysisType'] != 'pose' and 'evidence' not in state['pendingFinal']:
                    try:
                        proof = app.state.evidence.observe(state['owner'], state['key'][0], state['streamId'],
                            state['analysisType'], state['lastAccepted'], None, state['pendingFinal'], final=True)
                    except (OSError, SQLAlchemyError, ValueError) as exc:
                        app.state.evidence.last_error = type(exc).__name__
                        # Keep the frozen verdict and buffer for a finish retry;
                        # never report archived then discard unsaved samples.
                        raise HTTPException(503, '告警录像保存失败，请点击“结束并归档”重试',
                                            headers={'Retry-After': '1'}) from exc
                    if proof:
                        state['pendingFinal']['evidence'] = proof
                state['finalResult'] = service.archive(state, state['pendingFinal'])
                return state['finalResult']
            task = asyncio.create_task(run_in_threadpool(finalize))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    @router.post('/station-streams/{stream_id}/manual-review')
    async def manual_review(stream_id: uuid.UUID, body: ManualReview, request: Request):
        user = await actor(request)
        return await run_in_threadpool(service.review, user, stream_id, body)

    @router.get('/station-streams/{stream_id}/inspection')
    async def inspection(stream_id: uuid.UUID, request: Request):
        user = await actor(request)
        return await run_in_threadpool(service.inspection, user, stream_id)

    @router.get('/stations/{station}/context')
    async def station_context(station: StationID, request: Request):
        user = await actor(request)
        binding = await run_in_threadpool(service.binding_for, user, station)
        return dict(binding=binding)

    @router.post('/stations/{station}/prepare')
    async def prepare_station(station: StationID, request: Request):
        user = await actor(request)
        binding = await run_in_threadpool(service.binding_for, user, station)
        if binding['analysisType'] != 'sticker':
            raise HTTPException(409, '当前工位无需贴条模型准备')
        try:
            ready = await run_in_threadpool(app.state.sticker_realtime.warmup)
        except AnalysisError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not ready:
            raise HTTPException(503, '贴条模型正在准备或使用中，请稍后重试', headers={'Retry-After': '1'})
        return dict(ready=True, analysisType='sticker', revision=binding['revision'])

    @router.get('/stations/{station}/inspections')
    async def history(station: StationID, request: Request,
                      limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1000000)):
        user = await actor(request)
        service.require_station(user, station)
        if 'cycle_id' in request.query_params:
            raise HTTPException(422, '检测记录按工位和轮次查询，已取消工件号关联')
        def read():
            with serialized_write(service.engine) as c:
                service.reconcile(c, station)
                query = select(inspections).where(inspections.c.owner == user['owner'],
                    inspections.c.station_id == station, inspection_visible())
                rows = c.execute(query.order_by(inspections.c.started_at.desc(), inspections.c.stream_id.desc())
                                 .offset(offset).limit(limit+1)).mappings().all()
                return dict(items=[service.record(row) for row in rows[:limit]], hasMore=len(rows)>limit, offset=offset)
        return await run_in_threadpool(read)

    @router.get('/stations/{station}/live-status')
    async def status(station: StationID, request: Request):
        user = await actor(request)
        await run_in_threadpool(service.binding_for, user, station)
        return await run_in_threadpool(service.status, station)

    @router.get('/admin/station-metrics')
    async def metrics(request: Request):
        user = await actor(request)
        if user.get('role') == 'terminal':
            raise HTTPException(403, '工位终端不能读取全局指标')
        statuses = await run_in_threadpool(lambda: [service.status(s) for s in STATIONS])
        return dict(scheduler=service.scheduler.snapshot(), stations=statuses)

    @router.get('/stations/{station}/preview')
    async def preview(station: StationID, request: Request, stream_id: uuid.UUID, frame_seq: int):
        user = await actor(request)
        await run_in_threadpool(service.binding_for, user, station)
        current = await run_in_threadpool(service.status, station)
        state = service.streams.get(station)
        if not state or current['state'] != 'LIVE' or not current.get('result') or not state.get('preview'):
            raise HTTPException(404, '当前没有有效检测采样画面')
        result = state['result']
        if state['streamId'] != str(stream_id) or result['frameSeq'] != frame_seq:
            raise HTTPException(409, '采样画面已更新，请读取最新状态')
        return Response(state['preview'], media_type='image/jpeg', headers={
            'Cache-Control':'private, no-store', 'X-Station-Id':station, 'X-Camera-Id':state['cameraId'],
            'X-Stream-Id':state['streamId'], 'X-Stream-Epoch':str(state['streamEpoch']),
            'X-Frame-Seq':str(frame_seq)})

    app.include_router(router)
