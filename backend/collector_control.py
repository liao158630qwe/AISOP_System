"""Durable per-piece commands for a camera process independent of the browser."""
import json
import time
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool

from .db import (digest, now, serialized_write, station_collectors as collectors,
                 station_collector_commands as commands, station_terminals as bindings,
                 station_stream_leases as leases, station_inspections as inspections)
from .terminals import ReviewActor

LEASE_SECONDS = 45


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    requestId: uuid.UUID
    action: Literal['start', 'finish', 'stop']
    streamId: uuid.UUID | None = None
    reason: str = Field(default='', max_length=1000)
    expectedActor: ReviewActor | None = None
    expectedInstanceId: uuid.UUID | None = None
    expectedRuntimeId: uuid.UUID | None = None
    expectedBindingRevision: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def target(self):
        if (self.action == 'start') != (self.streamId is None):
            raise ValueError('开始新件不指定旧轮次；结束或中断必须指定原轮次')
        return self


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    instanceId: uuid.UUID
    streamId: uuid.UUID | None = None
    state: Literal['IDLE', 'RUNNING', 'ERROR'] = 'IDLE'
    error: str = Field(default='', max_length=1000)


class Acknowledge(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    instanceId: uuid.UUID
    streamId: uuid.UUID | None = None
    status: Literal['SUCCEEDED', 'FAILED']
    error: str = Field(default='', max_length=1000)


class CollectorControl:
    def __init__(self, terminals):
        self.terminals, self.engine = terminals, terminals.engine

    def binding(self, c, user, station):
        self.terminals.require_station(user, station)
        row = c.execute(select(bindings).where(bindings.c.station_id == station,
            bindings.c.owner == user['owner'], bindings.c.enabled.is_(True)).with_for_update()).mappings().first()
        if not row:
            raise HTTPException(404, '工位尚未启用设备绑定')
        return row

    @staticmethod
    def row(c, station):
        value = c.execute(select(collectors).where(collectors.c.station_id == station)).mappings().first()
        return dict(value) if value else None

    @staticmethod
    def command(c, identity):
        value = c.execute(select(commands).where(commands.c.request_id == identity)).mappings().first() if identity else None
        return dict(value) if value else None

    @staticmethod
    def public_command(value):
        if not value:
            return None
        return dict(requestId=value['request_id'], action=value['action'], streamId=value['target_stream_id'],
            assignedStreamId=value['stream_id'], status=value['status'], reason=value['reason'], error=value['error'],
            instanceId=value['instance_id'], bindingRevision=value['binding_revision'], runtimeId=value['runtime_id'],
            actor=value['actor'], createdAt=value['created_at'], completedAt=value['completed_at'])

    def online(self, c, row, binding):
        return bool(row and row['expires_at'] > now(c) and row['runtime_id'] == self.terminals.runtime_id
                    and row['binding_revision'] == binding['revision'])

    def interrupt(self, c, row, reason, *, expire=False):
        """SQL fencing prevents an old process from publishing or finishing after failure."""
        if row['stream_id']:
            c.execute(update(inspections).where(inspections.c.stream_id == row['stream_id'],
                inspections.c.status == 'RUNNING').values(status='INTERRUPTED', finished_at=now(c), reason=reason))
            c.execute(update(leases).where(leases.c.stream_id == row['stream_id']).values(closed=True))
        c.execute(update(commands).where(commands.c.station_id == row['station_id'],
            commands.c.owner == row['owner'], commands.c.status == 'PENDING').values(
                status='FAILED', error=reason, completed_at=now(c)))
        row.update(state='ERROR', error=reason)
        if expire:
            row['expires_at'] = 0
        c.execute(update(collectors).where(collectors.c.station_id == row['station_id']).values(
            state=row['state'], error=reason, expires_at=row['expires_at']))

    def reconcile(self, c, row, binding):
        if not row:
            return
        if not self.online(c, row, binding):
            # Preserve an explicit last error instead of replacing it on every poll.
            reason = ('推理服务已重启，原轮次已中断，请核对后开始新件' if row['runtime_id'] != self.terminals.runtime_id else
                      '设备绑定已变更，原轮次已中断' if row['binding_revision'] != binding['revision'] else
                      row['error'] or '后台采集器心跳中断，本轮未完成归档')
            self.interrupt(c, row, reason, expire=True)
        elif row['stream_id']:
            self.terminals.reconcile(c, row['station_id'], owner=row['owner'], stream_ids=[row['stream_id']])
            record = c.execute(select(inspections).where(inspections.c.stream_id == row['stream_id'])).mappings().first()
            pending = self.command(c, row['last_command_id'])
            if (record and record['status'] == 'INTERRUPTED' and pending and pending['status'] == 'PENDING'
                    and pending['stream_id'] == record['stream_id'] and pending['action'] != 'stop'):
                self.interrupt(c, row, record['reason'] or '本轮已中断，请核对后开始新件')

    def snapshot(self, c, station, row, binding, command=None):
        record = c.execute(select(inspections).where(inspections.c.stream_id == row['stream_id'],
            inspections.c.owner == row['owner'])).mappings().first() if row and row['stream_id'] else None
        online = self.online(c, row, binding)
        command = command or (self.command(c, row['last_command_id']) if row else None)
        live = self.terminals.streams.get(station)
        identity = live if live and record and live['streamId'] == record['stream_id'] else record['context'] if record else None
        stream = self.terminals.identity(identity) if online and record and record['status'] == 'RUNNING' else None
        state = 'OFFLINE' if not online else 'ERROR' if row['state'] == 'ERROR' else 'RUNNING' if stream else 'IDLE'
        if online and command and command['status'] == 'PENDING' and command['action'] in ('finish', 'stop'):
            state = 'FINISHING' if command['action'] == 'finish' else 'STOPPING'
        return dict(stationId=station, online=online, instanceId=row['instance_id'] if row else None,
            runtimeId=self.terminals.runtime_id, bindingRevision=binding['revision'], state=state, stream=stream,
            command=self.public_command(command), record=self.terminals.record(record) if record else None,
            error=row['error'] if row else '', heartbeatAt=row['heartbeat_at'] if row else None,
            leaseSeconds=LEASE_SECONDS)

    def status(self, user, station):
        with serialized_write(self.engine) as c:
            binding = self.binding(c, user, station)
            row = self.row(c, station)
            self.reconcile(c, row, binding)
            return self.snapshot(c, station, row, binding)

    def read_command(self, user, station, request_id):
        self.status(user, station)
        with self.engine.connect() as c:
            command = self.command(c, str(request_id))
            if not command or command['station_id'] != station or command['owner'] != user['owner']:
                raise HTTPException(404, '控制请求尚未保存')
            return self.public_command(command)

    def submit(self, user, station, body):
        self.status(user, station)
        actor = self.terminals.review_actor(user)
        payload = body.model_dump(mode='json')
        fingerprint = digest(json.dumps(dict(payload, stationId=station, actor=actor), sort_keys=True))
        with serialized_write(self.engine) as c:
            binding = self.binding(c, user, station)
            row = self.row(c, station)
            old = self.command(c, str(body.requestId))
            if old:
                if old['owner'] != user['owner'] or old['station_id'] != station or old['request_hash'] != fingerprint:
                    raise HTTPException(409, '原请求编号已用于其他内容或确认身份，请核对原请求')
                return self.snapshot(c, station, row, binding, old)
            if body.expectedActor and body.expectedActor.model_dump() != actor:
                raise HTTPException(409, '操作身份已变化，原控制请求不会以新身份执行')
            if not self.online(c, row, binding):
                raise HTTPException(409, '后台采集器尚未连接，不能开始或控制检测')
            if ((body.expectedInstanceId is not None and str(body.expectedInstanceId) != row['instance_id']) or
                    (body.expectedRuntimeId is not None and str(body.expectedRuntimeId) != self.terminals.runtime_id) or
                    (body.expectedBindingRevision is not None and body.expectedBindingRevision != binding['revision'])):
                raise HTTPException(409, '采集器、服务或绑定已变化，原控制请求不能提交到新轮次')
            previous = self.command(c, row['last_command_id'])
            if previous and previous['status'] == 'PENDING':
                raise HTTPException(409, '上一控制请求仍待确认，请先核对原请求')
            record = c.execute(select(inspections).where(inspections.c.stream_id == row['stream_id'])).mappings().first() if row['stream_id'] else None
            lease = c.execute(select(leases).where(leases.c.station_id == station)).mappings().first()
            if body.action == 'start':
                if row['state'] == 'ERROR':
                    raise HTTPException(409, '采集器尚未恢复就绪，请先检查设备')
                if record and record['status'] == 'RUNNING' or lease and not lease['closed'] and lease['lease_until'] > now(c):
                    raise HTTPException(409, '上一件尚未结束或释放，请先核对原轮次')
                if binding['config']['analysisType'] == 'manual':
                    raise HTTPException(409, '该工位尚未配置AI模型')
            elif not record or record['stream_id'] != str(body.streamId):
                raise HTTPException(409, '目标轮次已变化，请刷新核对当前工件')
            elif body.action == 'finish' and record['status'] == 'INTERRUPTED':
                raise HTTPException(409, '中断轮次不能补记为完成')
            command = dict(request_id=str(body.requestId), station_id=station, owner=user['owner'],
                instance_id=row['instance_id'], binding_revision=binding['revision'], runtime_id=self.terminals.runtime_id,
                action=body.action, target_stream_id=str(body.streamId) if body.streamId else None,
                stream_id=str(body.streamId) if body.streamId else None, reason=body.reason, actor=actor,
                request_hash=fingerprint, status='PENDING', error='', created_at=now(c), completed_at=None)
            c.execute(commands.insert().values(**command))
            row['last_command_id'] = command['request_id']
            c.execute(update(collectors).where(collectors.c.station_id == station).values(last_command_id=command['request_id']))
            return self.snapshot(c, station, row, binding, command)

    def heartbeat(self, user, station, body):
        self.status(user, station)
        with serialized_write(self.engine) as c:
            binding = self.binding(c, user, station)
            row = self.row(c, station)
            online = self.online(c, row, binding)
            instance = str(body.instanceId)
            if online and row['instance_id'] != instance:
                raise HTTPException(409, '另一后台采集进程仍持有设备，请勿重复启动')
            if body.streamId and (not online or row['instance_id'] != instance or row['stream_id'] != str(body.streamId)):
                raise HTTPException(409, '原采集轮次已失效，请停止旧轮次并重新核对')
            if not online:
                previous = row
                row = dict(station_id=station, owner=user['owner'], instance_id=instance,
                    binding_revision=binding['revision'], runtime_id=self.terminals.runtime_id,
                    stream_id=previous['stream_id'] if previous else None,
                    last_command_id=previous['last_command_id'] if previous else None)
            row.update(heartbeat_at=now(c), expires_at=now(c)+LEASE_SECONDS, state=body.state, error=body.error)
            if self.row(c, station):
                c.execute(update(collectors).where(collectors.c.station_id == station).values(**row))
            else:
                c.execute(collectors.insert().values(**row))
            if body.state == 'ERROR':
                self.interrupt(c, row, body.error or '后台采集器异常，本轮已中断')
            return self.snapshot(c, station, row, binding)

    def acknowledge(self, user, station, request_id, body):
        self.status(user, station)
        with serialized_write(self.engine) as c:
            binding = self.binding(c, user, station)
            row = self.row(c, station)
            command = self.command(c, str(request_id))
            if not command or command['station_id'] != station or command['owner'] != user['owner']:
                raise HTTPException(404, '控制请求不存在')
            if command['instance_id'] != str(body.instanceId):
                raise HTTPException(409, '控制请求不属于此采集进程')
            if command['status'] != 'PENDING':
                return self.snapshot(c, station, row, binding, command)
            self.require_instance(c, row, binding, str(body.instanceId))
            supplied = str(body.streamId) if body.streamId else None
            if supplied != command['stream_id']:
                raise HTTPException(409, '回执轮次与控制请求不一致')
            record = c.execute(select(inspections).where(inspections.c.stream_id == command['stream_id'])).mappings().first() if command['stream_id'] else None
            lease = c.execute(select(leases).where(leases.c.stream_id == command['stream_id'])).mappings().first() if command['stream_id'] else None
            if body.status == 'SUCCEEDED':
                expected = {'start': 'RUNNING', 'finish': 'FINISHED', 'stop': 'INTERRUPTED'}[command['action']]
                valid = record and (record['status'] == expected or command['action'] == 'stop' and record['status'] == 'FINISHED')
                if command['action'] in ('finish', 'stop'):
                    valid = valid and lease and lease['closed']
                if not valid:
                    raise HTTPException(409, '服务器尚未确认该轮次完成控制操作，请核对后重试')
            else:
                self.interrupt(c, row, body.error or '采集控制失败，本轮已中断')
            command.update(status=body.status, error=body.error, completed_at=now(c))
            c.execute(update(commands).where(commands.c.request_id == str(request_id)).values(
                status=body.status, error=body.error, completed_at=command['completed_at']))
            row.update(state='RUNNING' if body.status == 'SUCCEEDED' and command['action'] == 'start' else
                       'IDLE' if body.status == 'SUCCEEDED' else 'ERROR', error=body.error)
            c.execute(update(collectors).where(collectors.c.station_id == station).values(state=row['state'], error=row['error']))
            return self.snapshot(c, station, row, binding, command)

    def require_instance(self, c, row, binding, instance):
        if not self.online(c, row, binding) or row['instance_id'] != instance:
            raise HTTPException(409, '后台采集实例或租约已失效，请重新核对')

    def start_guard(self, c, user, binding, body, context):
        """Called under the same binding lock and transaction that creates the stream."""
        row = self.row(c, binding['stationId'])
        claim = user.get('_collector')
        if not claim:
            if self.online(c, row, {'revision': binding['revision']}):
                raise HTTPException(409, '后台采集器已连接，请使用正式采集的逐件控制')
            return None
        self.require_instance(c, row, {'revision': binding['revision']}, claim['instance'])
        command = self.command(c, claim['command'])
        if not command or command['station_id'] != binding['stationId'] or command['instance_id'] != claim['instance'] or command['action'] != 'start' or command['runtime_id'] != self.terminals.runtime_id or command['request_id'] != str(body.requestId) or body.replaces:
            raise HTTPException(409, '开始请求与当前后台控制命令不一致')
        if command['status'] == 'FAILED':
            raise HTTPException(409, '原开始请求已失效，不能重复开启工件')
        if command['stream_id']:
            old = c.execute(select(leases).where(leases.c.stream_id == command['stream_id'],
                leases.c.runtime_id == self.terminals.runtime_id, leases.c.closed.is_(False), leases.c.lease_until > now(c))).mappings().first()
            if not old:
                raise HTTPException(409, '原开始请求的轮次已结束，不能用相同请求开启新件')
            return dict(old)
        if command['status'] != 'PENDING' or row['last_command_id'] != command['request_id']:
            raise HTTPException(409, '原开始请求已完成或被替换')
        context.update(collectorInstanceId=claim['instance'], collectorRequestId=claim['command'])
        return None

    def started(self, c, user, row):
        claim = user.get('_collector')
        if claim:
            c.execute(update(commands).where(commands.c.request_id == claim['command']).values(stream_id=row['stream_id']))
            c.execute(update(collectors).where(collectors.c.station_id == row['station_id']).values(
                stream_id=row['stream_id'], state='RUNNING', error=''))

    def guard(self, user, context, request, action):
        if not context.get('collectorInstanceId'):
            return
        if not request.headers.get('x-terminal-key') or user.get('role') != 'terminal':
            raise HTTPException(403, '后台轮次仅允许已登记采集器发送采样和执行控制')
        station = context['stationId']
        self.status(user, station)
        with serialized_write(self.engine) as c:
            binding = self.binding(c, user, station)
            row = self.row(c, station)
            instance = request.headers.get('x-collector-instance', '')
            self.require_instance(c, row, binding, instance)
            if context['collectorInstanceId'] != instance or row['stream_id'] != context['streamId']:
                raise HTTPException(409, '后台轮次与当前采集器身份不一致')
            command_id = request.headers.get('x-collector-command')
            if action == 'frame':
                pending = self.command(c, row['last_command_id'])
                if row['state'] == 'ERROR' or pending and pending['status'] == 'PENDING' and pending['action'] != 'start':
                    raise HTTPException(429, '本轮正在结束，停止发送新采样', headers={'Retry-After': '1'})
                return
            if action == 'stop' and not command_id:
                return  # The camera process may always interrupt its own failed round.
            command = self.command(c, command_id)
            if not command or command['instance_id'] != instance or command['runtime_id'] != self.terminals.runtime_id or command['stream_id'] != context['streamId'] or command['status'] == 'FAILED' or command['action'] not in (('finish', 'stop') if action == 'stop' else ('finish',)):
                raise HTTPException(409, '控制命令与当前轮次不一致')

    def publish(self, state, result, payload, received):
        """Fence a late GPU/evidence result against process failure or takeover.

        This is the short publication transaction, after model and evidence IO.
        Lock order matches command/heartbeat reconciliation: binding then lease.
        """
        with serialized_write(self.engine) as c:
            binding = self.binding(c, {'owner': state['owner']}, state['stationId'])
            row = self.row(c, state['stationId'])
            self.require_instance(c, row, binding, state['collectorInstanceId'])
            lease = c.execute(select(leases).where(leases.c.station_id == state['stationId'])
                              .with_for_update()).mappings().first()
            record = c.execute(select(inspections.c.status).where(
                inspections.c.stream_id == state['streamId'])).scalar_one_or_none()
            if (row['state'] == 'ERROR' or row['stream_id'] != state['streamId'] or record != 'RUNNING'
                    or not lease or lease['stream_id'] != state['streamId'] or lease['closed']
                    or lease['runtime_id'] != self.terminals.runtime_id or lease['lease_until'] <= now(c)):
                raise HTTPException(409, '本轮已中断或变更，迟到检测结果不再发布')
            # Publish picture, identity and freshness together while interruption
            # cannot acquire the binding/lease lock and overtake this result.
            state.update(result=result, preview=payload, lastAccepted=result['frameTime'],
                         lastAt=time.monotonic(), lastCaptureAt=received)
            state['completedTimes'].append(time.monotonic())


def register(app, auth):
    from .terminals import StationID
    service = app.state.collector_control
    router = APIRouter(prefix='/api/v1')

    async def actor(request, worker=False):
        user = await run_in_threadpool(auth, request)
        if worker and (not request.headers.get('x-terminal-key') or user.get('role') != 'terminal'):
            raise HTTPException(403, '该接口只接受后台采集器设备凭据')
        return user

    @router.get('/stations/{station}/collector')
    async def status(station: StationID, request: Request):
        return await run_in_threadpool(service.status, await actor(request), station)

    @router.post('/stations/{station}/collector/commands')
    async def submit(station: StationID, body: Command, request: Request):
        return await run_in_threadpool(service.submit, await actor(request), station, body)

    @router.get('/stations/{station}/collector/commands/{request_id}')
    async def read_command(station: StationID, request_id: uuid.UUID, request: Request):
        return await run_in_threadpool(service.read_command, await actor(request), station, request_id)

    @router.post('/stations/{station}/collector/heartbeat')
    async def heartbeat(station: StationID, body: Heartbeat, request: Request):
        return await run_in_threadpool(service.heartbeat, await actor(request, True), station, body)

    @router.post('/stations/{station}/collector/commands/{request_id}/ack')
    async def acknowledge(station: StationID, request_id: uuid.UUID, body: Acknowledge, request: Request):
        return await run_in_threadpool(service.acknowledge, await actor(request, True), station, request_id, body)

    app.include_router(router)
