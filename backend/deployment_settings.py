"""Shared equipment inventory and commissioning plan; never writes to devices."""
import ipaddress
import re
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .db import deployment_settings as table, now, serialized_write

Text = Annotated[str, Field(max_length=500)]
Station = Literal['station-1', 'station-2', 'station-3', 'station-4', 'station-5', 'station-6']


def validate_host(value):
    if not value:
        return value
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if len(value) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                                     for part in value.split('.')):
            raise ValueError('请填写有效 IP 或主机名')
        if re.fullmatch(r'[0-9.]+', value):
            raise ValueError('IP 地址无效')
    return value


def endpoint(value, schemes):
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.fragment:
        raise ValueError('地址协议或主机无效')
    validate_host(parsed.hostname)
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('端口无效')
    if parsed.username is not None or parsed.password is not None or any(
            key.lower() in ('password', 'passwd', 'token', 'key', 'secret', 'authorization', 'username', 'user')
            for key, _ in parse_qsl(parsed.query)):
        raise ValueError('设备清单不保存连接凭据，请在采集主机配置私有环境变量')
    return value


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)


class Equipment(Record):
    id: Literal['server', 'terminal', 'lamp', 'poe', 'nvr', 'core', 'display', 'network-cable', 'power-cable']
    name: Annotated[str, Field(min_length=1, max_length=80)]
    brand: Text
    model: Text
    specification: Text
    quantity: int | None = Field(default=None, ge=1, le=1000)
    unit: Annotated[str, Field(min_length=1, max_length=20)]
    note: Text = ''


def equipment_defaults():
    rows = [
        ('server', 'SOP 推理主机', '锦爵', 'GPU 服务器工作站', '双路至强 6148 · RTX4090', 1, '台', '内存原清单为“16G×64G”，待核对；六路推理性能待实机验收'),
        ('terminal', '工位显示一体机', '研显', 'i7-8550U', '8G+256G · 27 寸工业触摸屏 · Windows', 6, '台', '六工位各一台，显示检测结果与人工复检'),
        ('lamp', '三色报警指示灯', '正泰', 'ZTP-NP', '24V · 红黄绿三色 + 声音告警', 6, '套', '每工位一套；供电类型、控制模块与接线待确认'),
        ('poe', 'PoE 千兆交换机', '海康威视', 'DS-3E0508P-S', '8 口 PoE 千兆（按设备清单）', 4, '台', '相机供电与网络接入；PoE 功率预算待填写'),
        ('nvr', '硬盘录像机', '海康威视', 'DS-7716NX-K5/Pro', '独立录像存储设备，支持多块大容量硬盘（按设备清单）', 1, '台', '保存相机视频流、AI 告警取证录像；30 天存证目标，需按实际容量与码率验收'),
        ('core', '核心交换机', '海康威视', 'DS-3E6603', '框式核心汇聚交换机', 1, '台', '连接推理主机、NVR、PoE 交换机、终端与大屏'),
        ('display', '大屏显示终端', '德天', '100 英寸 4K 工业大屏', '六工位集中看板', 1, '套', '需配置内置或外接播放主机'),
        ('network-cable', '网络布线', '威迅', '超六类屏蔽网线', '车间设备网络布线', 3, '圈', '每圈长度、现场距离及屏蔽接地待核对'),
        ('power-cable', '设备供电布线', '珠江电缆', '国标 3 芯 1 平方', '设备供电布线', 3, '圈', '每圈长度与实际供电负载待核对'),
    ]
    return [Equipment(**dict(zip(('id', 'name', 'brand', 'model', 'specification', 'quantity', 'unit', 'note'), row))) for row in rows]


class Server(Record):
    host: Text = ''
    apiUrl: Text = ''
    memoryOriginal: Text = '16G×64G'
    memoryConfirmedGb: int | None = Field(default=None, ge=1, le=16384)
    _host = field_validator('host')(validate_host)

    @field_validator('apiUrl')
    @classmethod
    def api_url(cls, value):
        return endpoint(value, ('http', 'https'))


class Network(Record):
    coreHost: Text = ''
    subnet: Text = ''
    gateway: Text = ''
    uplink: Text = ''
    _hosts = field_validator('coreHost', 'gateway')(validate_host)

    @field_validator('subnet')
    @classmethod
    def subnet_valid(cls, value):
        if value:
            ipaddress.ip_network(value, strict=False)
        return value


class Switch(Record):
    id: Literal['poe-1', 'poe-2', 'poe-3', 'poe-4']
    host: Text = ''
    location: Text = ''
    uplink: Text = ''
    powerBudgetW: float | None = Field(default=None, gt=0, le=10000)
    _host = field_validator('host')(validate_host)


class Camera(Record):
    stationId: Station
    id: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,60}$')]
    model: Text = ''
    host: Text = ''
    rtspUrl: Text = ''
    sourceEnv: Annotated[str, Field(pattern=r'^[A-Z][A-Z0-9_]{0,79}$')]
    poeId: Literal['', 'poe-1', 'poe-2', 'poe-3', 'poe-4'] = ''
    poePort: int | None = Field(default=None, ge=1, le=8)
    nvrChannel: int | None = Field(default=None, ge=1, le=256)
    _host = field_validator('host')(validate_host)

    @field_validator('rtspUrl')
    @classmethod
    def rtsp_valid(cls, value):
        return endpoint(value, ('rtsp', 'rtsps'))


class Terminal(Record):
    stationId: Station
    id: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,60}$')]
    host: Text = ''
    location: Text = ''
    _host = field_validator('host')(validate_host)


class Recorder(Record):
    host: Text = ''
    location: Text = ''
    purpose: Annotated[str, Field(min_length=1, max_length=500)] = '保存相机视频流、AI 告警取证录像'
    diskCount: int | None = Field(default=None, ge=1, le=128)
    diskCapacityTb: float | None = Field(default=None, gt=0, le=1000)
    retentionDays: int = Field(default=30, ge=30, le=3650)
    evidenceRetentionDays: int = Field(default=30, ge=30, le=3650)
    usableStorageTb: float | None = Field(default=None, gt=0, le=10000)
    bitrateMbps: float | None = Field(default=None, gt=0, le=1000)
    _host = field_validator('host')(validate_host)


class Display(Record):
    host: Text = ''
    playerHost: Text = ''
    page: Literal['/board'] = '/board'
    _hosts = field_validator('host', 'playerHost')(validate_host)


class Deployment(Record):
    stationCount: Literal[6] = 6
    videoChannels: Literal[6] = 6
    terminalCount: Literal[6] = 6
    equipment: list[Equipment] = Field(default_factory=equipment_defaults, min_length=9, max_length=9)
    server: Server = Field(default_factory=Server)
    network: Network = Field(default_factory=Network)
    switches: list[Switch] = Field(default_factory=lambda: [Switch(id=f'poe-{n}') for n in range(1, 5)], min_length=4, max_length=4)
    cameras: list[Camera] = Field(default_factory=lambda: [Camera(stationId=f'station-{n}', id=f'cam-{n:02}',
        sourceEnv=f'AISOP_CAMERA_SOURCE_{n}', nvrChannel=n) for n in range(1, 7)], min_length=6, max_length=6)
    terminals: list[Terminal] = Field(default_factory=lambda: [Terminal(stationId=f'station-{n}', id=f'pc-{n:02}')
        for n in range(1, 7)], min_length=6, max_length=6)
    nvr: Recorder = Field(default_factory=Recorder)
    display: Display = Field(default_factory=Display)
    notes: Annotated[str, Field(max_length=2000)] = ''

    @model_validator(mode='after')
    def topology(self):
        for items, field in ((self.equipment, 'id'), (self.switches, 'id'), (self.cameras, 'id'),
                             (self.cameras, 'stationId'), (self.terminals, 'id'), (self.terminals, 'stationId'),
                             (self.cameras, 'sourceEnv')):
            if len({getattr(item, field) for item in items}) != len(items):
                raise ValueError('设备编号、工位归属或采集变量重复')
        items = {item.id: item for item in self.equipment}
        if items['terminal'].quantity != 6 or items['poe'].quantity != 4:
            raise ValueError('当前清单为六台终端与四台 PoE 交换机')
        ports = [(c.poeId, c.poePort) for c in self.cameras if c.poeId and c.poePort is not None]
        if any(c.poePort is not None and not c.poeId for c in self.cameras):
            raise ValueError('填写 PoE 端口时必须选择所属交换机')
        channels = [c.nvrChannel for c in self.cameras if c.nvrChannel is not None]
        if len(ports) != len(set(ports)) or len(channels) != len(set(channels)):
            raise ValueError('相机不能重复占用同一交换机端口或 NVR 通道')
        return self


class Save(Record):
    revision: int = Field(ge=0)
    config: Deployment


def register(app, auth):
    router = APIRouter(prefix='/api/v1/admin/deployment-settings')
    engine, settings = app.state.engine, app.state.settings

    def owner(request):
        user = auth(request)
        if user.get('role') == 'terminal':
            raise HTTPException(403, '设备部署配置需要管理员账户')
        return user['owner']

    def config_for(row):
        # Older saved records gain newly introduced defaults without a GET write.
        return Deployment.model_validate(row['config'] if row else {}).model_dump(mode='json')

    def response(row=None):
        return dict(revision=row['revision'] if row else 0, updatedAt=row['updated_at'] if row else None,
            config=config_for(row),
            status='commissioning-plan', hardwareVerified=False,
            runtime=dict(stationCount=6, maxDurationSeconds=settings.realtime_max_duration_seconds,
                noTargetTimeoutSeconds=settings.realtime_no_target_seconds, nvrIntegrated=False,
                modbusConnected=False, crossStationGate=False, performanceVerified=False,
                evidenceStorage=dict(location=str(settings.data_dir.resolve() / 'evidence'),
                    retentionDays=settings.evidence_retention_seconds / 86400)))

    @router.get('')
    def get(request: Request):
        identity = owner(request)
        with engine.connect() as connection:
            row = connection.execute(select(table).where(table.c.owner == identity)).mappings().first()
        return response(row)

    @router.put('')
    def save(body: Save, request: Request):
        identity, config = owner(request), body.config.model_dump(mode='json')
        try:
            with serialized_write(engine) as connection:
                row = connection.execute(select(table).where(table.c.owner == identity).with_for_update()).mappings().first()
                if row and row['config'] == config and body.revision in (row['revision'], row['revision'] - 1):
                    return response(row)
                if body.revision != (row['revision'] if row else 0):
                    raise HTTPException(409, '配置已被其他页面更新，请导出当前修改后重新读取服务器配置')
                values = dict(revision=body.revision + 1, config=config, updated_at=now(connection))
                if row:
                    connection.execute(update(table).where(table.c.owner == identity).values(**values))
                else:
                    connection.execute(table.insert().values(owner=identity, **values))
        except IntegrityError as exc:
            raise HTTPException(409, '配置已被其他页面创建，请重新读取服务器配置') from exc
        return response(values)

    app.include_router(router)
