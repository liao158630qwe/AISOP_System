"""Persistent administrator model library and versioned scene definitions."""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import shutil
import threading
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update

from algorithms.asset_snapshot import signature
from algorithms.video import file_hash
from .config import ROOT
from .db import model_registry, scene_tasks, now


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)


class ModelImport(Strict):
    path: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(default='1.0', min_length=1, max_length=60)
    notes: str = Field(default='', max_length=1000)


class ModelToggle(Strict):
    enabled: bool


class Rule(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,50}$')
    name: str = Field(min_length=1, max_length=100)
    class_id: int = Field(ge=0)
    kind: Literal['required', 'forbidden'] = 'required'
    min_count: int = Field(default=1, ge=1, le=300)
    max_count: int | None = Field(default=None, ge=1, le=300)
    threshold: float = Field(default=.6, ge=.05, le=.99)
    roi: tuple[float, float, float, float] = (0, 0, 1, 1)

    @model_validator(mode='after')
    def valid(self):
        x1, y1, x2, y2 = self.roi
        if not 0 <= x1 < x2 <= 1 or not 0 <= y1 < y2 <= 1:
            raise ValueError('检测区域必须是归一化非空矩形')
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError('最大数量不能小于最小数量')
        return self


class SceneDefinition(Strict):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1000)
    model_id: uuid.UUID
    status: Literal['draft', 'published'] = 'draft'
    revision: int | None = Field(default=None, ge=1)
    imgsz: Literal[320, 640, 960, 1280] = 640
    sample_fps: float = Field(default=4, ge=.5, le=10)
    confirmation_frames: int = Field(default=2, ge=1, le=5)
    min_sharpness: float = Field(default=20, ge=0, le=10000)
    device: Literal['auto', 'cpu', 'cuda'] = 'auto'
    rules: list[Rule] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def valid(self):
        if self.imgsz % 32:
            raise ValueError('输入尺寸需为32的倍数')
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError('规则编号不能重复')
        if self.status == 'published' and not any(r.kind == 'required' for r in self.rules):
            raise ValueError('发布前至少配置一项必须检出的正向证据')
        return self


def inspect_model(path):
    """Loading is restricted to the authenticated administrator's trusted weights."""
    from ultralytics import YOLO
    model = YOLO(str(path))
    if model.task not in ('detect', 'segment', 'pose', 'obb', 'classify'):
        raise ValueError('当前运行适配器支持 Ultralytics 检测、分割、关键点、旋转框与分类模型')
    classes = {str(k): str(v) for k, v in model.names.items()}
    if not classes or any(int(k) < 0 for k in classes):
        raise ValueError('模型中未找到有效类别')
    return model.task, classes


class SceneRegistry:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.directory = settings.data_dir / 'registered-models'
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._checks = OrderedDict()

    def candidates(self):
        paths = list((ROOT / 'models').glob('*.pt'))
        paths += list((ROOT / 'outputs').rglob('best.pt'))
        for card in (ROOT / 'outputs').rglob('model-card.json'):
            try:
                candidate = Path(json.loads(card.read_text(encoding='utf-8'))['model'])
                candidate = candidate if candidate.is_absolute() else ROOT / candidate
                if candidate.suffix == '.pt':
                    paths.append(candidate)
            except (ValueError, KeyError, OSError):
                continue
        result = []
        for path in sorted(set(p.resolve() for p in paths)):
            if path.is_file() and any(path.is_relative_to((ROOT / directory).resolve()) for directory in ('models', 'outputs')):
                result.append(dict(path=path.relative_to(ROOT).as_posix(), name=path.name,
                                   size=path.stat().st_size))
        return result[:200]

    def import_model(self, body, owner):
        source = (ROOT / body.path).resolve()
        if not any(source.is_relative_to((ROOT / name).resolve()) for name in ('models', 'outputs')):
            raise HTTPException(422, '请选择项目 models 或 outputs 目录中的模型')
        if source.suffix.lower() != '.pt' or not source.is_file():
            raise HTTPException(422, '需要存在的 Ultralytics .pt 模型文件')
        if source.stat().st_size > self.settings.max_upload_bytes:
            raise HTTPException(413, '模型文件超过上传大小限制')
        identity = str(uuid.uuid4())
        destination = self.directory / (identity + '.pt')
        try:
            shutil.copy2(source, destination)
            digest = file_hash(destination)
            # Preserve semantic names from the previous turn's generic classifier trainer.
            aliases = None
            for parent in list(source.parents)[:3]:
                card = parent / 'model-card.json'
                if card.is_file():
                    value = json.loads(card.read_text(encoding='utf-8'))
                    if value.get('model_sha256') == digest:
                        aliases = value.get('class_index_to_name')
                        break
            return self.register(identity, destination, owner, body.name, body.version, body.notes, aliases)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def register(self, identity, path, owner, name, version, notes='', aliases=None):
        try:
            task, classes = inspect_model(path)
        except Exception as exc:
            raise HTTPException(422, '模型无法加载：请使用可信来源的 Ultralytics .pt 权重并检查依赖/模型版本') from exc
        if isinstance(aliases, list) and len(aliases) == len(classes) and all(isinstance(v, str) for v in aliases):
            classes = {str(i): value for i, value in enumerate(aliases)}
        row = dict(id=identity, owner=owner, name=name, version=version, task=task, classes=classes,
                   path=path.relative_to(self.settings.data_dir).as_posix(), sha256=file_hash(path),
                   size=path.stat().st_size, enabled=True, notes=notes)
        with self.engine.begin() as c:
            row['created_at'] = now(c)
            c.execute(model_registry.insert().values(**row))
        return self.public_model(row)

    def _model(self, identity, owner=None):
        query = select(model_registry).where(model_registry.c.id == str(identity))
        if owner is not None:
            query = query.where(model_registry.c.owner == owner)
        with self.engine.connect() as c:
            row = c.execute(query).mappings().first()
        if not row:
            raise HTTPException(404, '模型不存在')
        return dict(row)

    @staticmethod
    def public_model(row):
        return {k: row[k] for k in ('id', 'name', 'version', 'task', 'classes', 'sha256', 'size', 'enabled', 'notes', 'created_at')}

    def models(self, owner):
        with self.engine.connect() as c:
            rows = c.execute(select(model_registry).where(model_registry.c.owner == owner)
                             .order_by(model_registry.c.created_at.desc())).mappings().all()
        return [self.public_model(row) for row in rows]

    def toggle(self, identity, enabled, owner):
        self._model(identity, owner)
        with self.engine.begin() as c:
            c.execute(update(model_registry).where(model_registry.c.id == str(identity), model_registry.c.owner == owner)
                      .values(enabled=enabled))
        return self.public_model(self._model(identity, owner))

    def asset_path(self, model):
        path = (self.settings.data_dir / model['path']).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise HTTPException(503, '模型存储路径无效')
        return path

    def verify_asset(self, model):
        path = self.asset_path(model)
        try:
            stamp = signature(path)
            key = (model['id'], model['sha256'])
            with self._lock:
                if self._checks.get(key) != stamp:
                    if file_hash(path) != model['sha256'] or signature(path) != stamp:
                        raise HTTPException(409, '模型内容已改变，请重新注册模型')
                    self._checks[key] = stamp
                    self._checks.move_to_end(key)
                    while len(self._checks) > 64:
                        self._checks.popitem(last=False)
        except OSError as exc:
            raise HTTPException(503, '模型文件缺失或不可读取') from exc
        return path

    def save(self, body, owner, identity=None):
        model = self._model(body.model_id, owner)
        if body.status == 'published' and not model['enabled']:
            raise HTTPException(422, '请先启用所选模型再发布任务')
        if body.status == 'published':
            self.verify_asset(model)
        if any(str(rule.class_id) not in model['classes'] for rule in body.rules):
            raise HTTPException(422, '规则引用了模型中不存在的类别')
        if model['task'] == 'classify' and any(r.min_count != 1 or r.max_count not in (None, 1) for r in body.rules):
            raise HTTPException(422, '分类模型每个区域只输出一个类别，数量需设置为1')
        config = body.model_dump(mode='json', exclude={'revision'})
        with self.engine.begin() as c:
            timestamp = now(c)
            if identity is None:
                identity = str(uuid.uuid4())
                c.execute(scene_tasks.insert().values(id=identity, owner=owner, name=body.name,
                    model_id=str(body.model_id), status=body.status, revision=1, config=config,
                    created_at=timestamp, updated_at=timestamp))
            else:
                result = c.execute(update(scene_tasks).where(scene_tasks.c.id == str(identity),
                    scene_tasks.c.owner == owner, scene_tasks.c.revision == body.revision)
                    .values(name=body.name, model_id=str(body.model_id), status=body.status,
                            revision=body.revision + 1 if body.revision else 1, config=config, updated_at=timestamp))
                if not result.rowcount:
                    raise HTTPException(409, '任务已被修改或不存在，请刷新后重试')
        return self.scene(identity, owner=owner)

    def _scene_row(self, identity, owner=None):
        query = select(scene_tasks).where(scene_tasks.c.id == str(identity))
        if owner is not None:
            query = query.where(scene_tasks.c.owner == owner)
        with self.engine.connect() as c:
            row = c.execute(query).mappings().first()
        if not row:
            raise HTTPException(404, '检测任务不存在')
        return dict(row)

    def scene(self, identity, owner=None):
        row = self._scene_row(identity, owner)
        if owner is None and row['status'] != 'published':
            raise HTTPException(404, '检测任务未发布')
        model = self._model(row['model_id'], row['owner'])
        ready, reason = bool(model['enabled']), ''
        try:
            self.verify_asset(model)
        except HTTPException as exc:
            ready, reason = False, exc.detail
        if not model['enabled']:
            reason = '关联模型已停用'
        return dict(id=row['id'], revision=row['revision'], **row['config'],
                    model=self.public_model(model), ready=ready, readiness_reason=reason,
                    updated_at=row['updated_at'])

    def scenes(self, owner=None):
        query = select(scene_tasks.c.id).order_by(scene_tasks.c.updated_at.desc())
        query = query.where(scene_tasks.c.owner == owner) if owner is not None else query.where(scene_tasks.c.status == 'published')
        with self.engine.connect() as c:
            identities = c.execute(query).scalars().all()
        return [self.scene(identity, owner) for identity in identities]

    def snapshot(self, identity, owner=None):
        row = self._scene_row(identity, owner)
        if row['status'] != 'published':
            raise HTTPException(409, '检测任务尚未发布')
        model = self._model(row['model_id'], row['owner'])
        if not model['enabled']:
            raise HTTPException(409, '关联模型已停用')
        path = self.verify_asset(model)
        frozen = dict(scene_id=row['id'], revision=row['revision'], config=row['config'],
                      model=self.public_model(model), model_path=str(path))
        frozen['sha256'] = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return frozen
