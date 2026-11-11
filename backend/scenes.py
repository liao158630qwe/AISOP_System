"""Persistent administrator model library and versioned scene definitions."""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from algorithms.asset_snapshot import signature
from algorithms.video import file_hash
from .config import ROOT
from .db import model_registry, scene_tasks, now, serialized_write


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
    class_id: int = Field(ge=0, strict=True)
    kind: Literal['required', 'forbidden'] = 'required'
    min_count: int = Field(default=1, ge=1, le=300, strict=True)
    max_count: int | None = Field(default=None, ge=1, le=300, strict=True)
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
    device: Literal['auto', 'cuda'] = 'cuda'
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


def rule_conflicts(rules, task):
    issues = []
    for index, left in enumerate(rules):
        for right in rules[index+1:]:
            if left.roi != right.roi:
                continue
            label = f'「{left.name}」与「{right.name}」'
            if left.kind == right.kind == 'required':
                if task == 'classify' and left.class_id != right.class_id:
                    issues.append(label + '要求同一区域同时属于不同类别，分类模型无法满足')
                elif left.class_id == right.class_id and left.threshold == right.threshold and (
                    max(left.min_count, right.min_count) > min(left.max_count or 300, right.max_count or 300)):
                    issues.append(label + '在相同区域和阈值下的数量范围互相矛盾')
            elif left.kind != right.kind and left.class_id == right.class_id:
                required, forbidden = (left, right) if left.kind == 'required' else (right, left)
                if required.threshold >= forbidden.threshold:
                    issues.append(label + '在同一区域要求检出且禁止同一目标，无法同时通过')
    return issues


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


def import_identity(owner, key=None):
    """A retry resolves to one registry row, including across API processes."""
    if key is None:
        return str(uuid.uuid4())
    try:
        key = str(uuid.UUID(key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(400, '模型导入请求编号必须是 UUID') from exc
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'aisop:model-import:' + owner + ':' + key))


class SceneRegistry:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.directory = settings.data_dir / 'registered-models'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.inbox = settings.data_dir / 'model-imports'
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._checks = OrderedDict()
        self._candidate_lock = threading.Lock()
        self._candidate_until = 0.0
        self._candidate_files = []
        self._inspection_slot = threading.BoundedSemaphore(1)

    def candidates(self, owner=None):
        # Recursive output discovery is disk-heavy. Cache only shared files;
        # owner-scoped registered models are always read fresh below.
        with self._candidate_lock:
            if time.monotonic() >= self._candidate_until:
                self._candidate_files = self._file_candidates()
                self._candidate_until = time.monotonic() + 30
            result = [dict(item) for item in self._candidate_files]
        if owner is not None:
            with self.engine.connect() as c:
                models = c.execute(select(model_registry).where(model_registry.c.owner == owner)
                    .order_by(model_registry.c.created_at.desc())).mappings().all()
            for model in models:
                path = self.asset_path(model)
                if path.is_file():
                    result.append(dict(path='model://' + model['id'], name=model['name'],
                        size=model['size'], source='registered', model_id=model['id'],
                        task=model['task'], version=model['version']))
        return result

    def _file_candidates(self):
        paths = list((ROOT / 'models').glob('*.pt'))
        paths += list((ROOT / 'outputs').rglob('best.pt'))
        for card in (ROOT / 'outputs').rglob('model-card.json'):
            try:
                candidate = Path(json.loads(card.read_text(encoding='utf-8'))['model'])
                candidate = candidate if candidate.is_absolute() else ROOT / candidate
                if candidate.suffix == '.pt':
                    paths.append(candidate)
            except (ValueError, TypeError, KeyError, OSError):
                continue
        result = []
        for path in sorted(set(p.resolve() for p in paths)):
            if path.is_file() and any(path.is_relative_to((ROOT / directory).resolve()) for directory in ('models', 'outputs')):
                result.append(dict(path=path.relative_to(ROOT).as_posix(), name=path.name,
                                   size=path.stat().st_size, source='project'))
        for path in sorted(self.inbox.rglob('*.pt')):
            resolved = path.resolve()
            if resolved.is_file() and resolved.is_relative_to(self.inbox.resolve()):
                result.append(dict(path='inbox/' + path.relative_to(self.inbox).as_posix(),
                                   name=path.name, size=path.stat().st_size, source='inbox'))
        return result

    def import_model(self, body, owner, key=None):
        if body.path.startswith('inbox/'):
            source = (self.inbox / body.path[len('inbox/'):]).resolve()
            allowed = source.is_relative_to(self.inbox.resolve())
        else:
            source = (ROOT / body.path).resolve()
            allowed = any(source.is_relative_to((ROOT / name).resolve()) for name in ('models', 'outputs'))
        if not allowed:
            raise HTTPException(422, '请选择服务器 models、outputs 或模型导入目录中的文件')
        if source.suffix.lower() != '.pt' or not source.is_file():
            raise HTTPException(422, '需要存在的 Ultralytics .pt 模型文件')
        if not source.stat().st_size:
            raise HTTPException(422, '模型文件为空')
        if source.stat().st_size > self.settings.max_upload_bytes:
            raise HTTPException(413, '模型文件超过上传大小限制')
        identity = import_identity(owner, key)
        # Each attempt owns a different file: concurrent retries cannot overwrite
        # or clean up the file referenced by the winning registry transaction.
        destination = self.directory / ((str(uuid.uuid4()) if key else identity) + '.pt')
        try:
            shutil.copy2(source, destination)
            if destination.stat().st_size > self.settings.max_upload_bytes:
                raise HTTPException(413, '模型文件超过上传大小限制')
            digest = file_hash(destination)
            # Preserve semantic names from the previous turn's generic classifier trainer.
            aliases = None
            for parent in list(source.parents)[:3]:
                card = parent / 'model-card.json'
                if card.is_file():
                    try:
                        value = json.loads(card.read_text(encoding='utf-8'))
                    except (ValueError, OSError):
                        continue
                    if isinstance(value, dict) and value.get('model_sha256') == digest:
                        aliases = value.get('class_index_to_name')
                        break
            result = self.register(identity, destination, owner, body.name, body.version, body.notes, aliases)
            if result.get('reused'):
                destination.unlink(missing_ok=True)
            return result
        except SQLAlchemyError:
            # COMMIT may have succeeded despite a lost acknowledgement. Keep
            # this attempt's file until the registry can be reconciled.
            raise
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def register(self, identity, path, owner, name, version, notes='', aliases=None):
        digest = file_hash(path)
        def existing():
            with self.engine.connect() as c:
                row = c.execute(select(model_registry).where(model_registry.c.id == identity,
                    model_registry.c.owner == owner)).mappings().first()
            if not row:
                return None
            if any(row[k] != v for k, v in dict(name=name, version=version, notes=notes, sha256=digest).items()):
                raise HTTPException(409, '同一导入请求对应不同文件或名称版本，请重新发起导入')
            self.verify_asset(row)
            return dict(self.public_model(row),
                reused=row['path'] != path.relative_to(self.settings.data_dir).as_posix())
        previous = existing()
        if previous:
            return previous
        # Do not let concurrent admin imports fill RAM with unbounded YOLO loads.
        if not self._inspection_slot.acquire(blocking=False):
            raise HTTPException(429, '正在校验其他模型，请稍后重试导入', headers={'Retry-After': '3'})
        try:
            try:
                task, classes = inspect_model(path)
            except Exception as exc:
                raise HTTPException(422, '模型无法加载：请使用可信来源的 Ultralytics .pt 权重并检查依赖/模型版本') from exc
        finally:
            self._inspection_slot.release()
        if isinstance(aliases, list) and len(aliases) == len(classes) and all(isinstance(v, str) for v in aliases):
            classes = {str(i): value for i, value in enumerate(aliases)}
        row = dict(id=identity, owner=owner, name=name, version=version, task=task, classes=classes,
                   path=path.relative_to(self.settings.data_dir).as_posix(), sha256=digest,
                   size=path.stat().st_size, enabled=True, notes=notes)
        try:
            with self.engine.begin() as c:
                row['created_at'] = now(c)
                c.execute(model_registry.insert().values(**row))
        except IntegrityError:
            previous = existing()
            if previous:
                return previous
            raise
        except SQLAlchemyError:
            previous = existing()
            if previous:
                return previous
            raise
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

    def catalog(self, owner):
        """One database snapshot, with each model sent/checked only once."""
        query = select(model_registry, *[column.label('scene_' + column.name) for column in scene_tasks.c]).select_from(
            model_registry.outerjoin(scene_tasks, (scene_tasks.c.model_id == model_registry.c.id) &
                                     (scene_tasks.c.owner == model_registry.c.owner)))
        with self.engine.connect() as c:
            rows = c.execute(query.where(model_registry.c.owner == owner)).mappings().all()
        models, scenes, checks = {}, [], {}
        for row in rows:
            model = {column.name: row[column.name] for column in model_registry.c}
            identity = model['id']
            if identity not in models:
                models[identity] = self.public_model(model)
            if row['scene_id'] is None:
                continue
            if identity not in checks:
                checks[identity] = self._readiness(model)
            scene = {column.name: row['scene_' + column.name] for column in scene_tasks.c}
            public = self._public_scene(scene, model, checks[identity])
            del public['model']  # The browser resolves model_id against models.
            scenes.append(public)
        return {'models': sorted(models.values(), key=lambda row: (row['created_at'], row['id']), reverse=True),
                'scenes': sorted(scenes, key=lambda row: (row['updated_at'], row['id']), reverse=True)}

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

    def save(self, body, owner, identity=None, request_key=None):
        try:
            return self._save(body, owner, identity, request_key)
        except IntegrityError:
            if identity is not None or not request_key:
                raise
            # Different-model retries can lock different rows before racing on
            # the same deterministic scene ID. Reconcile after transaction rollback.
            return self._save(body, owner, identity, request_key)

    def _save(self, body, owner, identity=None, request_key=None):
        creating = identity is None
        config = body.model_dump(mode='json', exclude={'revision'})
        if creating and request_key:
            try:
                identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'aisop:scene:' + owner + ':' + str(uuid.UUID(request_key))))
            except ValueError as exc:
                raise HTTPException(400, '场景请求编号必须是UUID') from exc
            with self.engine.connect() as c:
                previous = c.execute(select(scene_tasks).where(scene_tasks.c.id == identity)).mappings().first()
            if previous:
                if previous['owner'] != owner or previous['config'] != config:
                    raise HTTPException(409, '同一场景请求编号对应不同配置或场景已修改')
                return self.scene(identity, owner=owner)
        model = self._model(body.model_id, owner)
        if body.status == 'published' and not model['enabled']:
            raise HTTPException(422, '请先启用所选模型再发布任务')
        if body.status == 'published':
            self.verify_asset(model)
        if any(str(rule.class_id) not in model['classes'] for rule in body.rules):
            raise HTTPException(422, '规则引用了模型中不存在的类别')
        if model['task'] == 'classify' and any(r.min_count != 1 or r.max_count not in (None, 1) for r in body.rules):
            raise HTTPException(422, '分类模型每个区域只输出一个类别，数量需设置为1')
        conflicts = rule_conflicts(body.rules, model['task'])
        if body.status == 'published' and conflicts:
            raise HTTPException(422, '；'.join(conflicts[:5]))
        readiness = self._readiness(model) if creating and request_key else None
        with serialized_write(self.engine) as c:
            # Serialize publication with model disable. Hashing stays outside
            # this short transaction; registered model metadata is immutable.
            current = c.execute(select(model_registry.c.enabled).where(
                model_registry.c.id == str(body.model_id), model_registry.c.owner == owner)
                .with_for_update()).first()
            if not current:
                raise HTTPException(404, '模型不存在')
            if creating and identity is not None:
                previous = c.execute(select(scene_tasks).where(scene_tasks.c.id == identity)).mappings().first()
                if previous:
                    if previous['owner'] != owner or previous['config'] != config:
                        raise HTTPException(409, '同一场景请求编号对应不同配置或场景已修改')
                    return self._public_scene(previous, model, readiness)
            if body.status == 'published' and not current.enabled:
                raise HTTPException(409, '模型已被停用，请刷新后重新检查发布配置')
            timestamp = now(c)
            if creating:
                identity = identity or str(uuid.uuid4())
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

    @staticmethod
    def _joined_query():
        return select(scene_tasks, *[column.label('asset_' + column.name) for column in model_registry.c]).select_from(
            scene_tasks.join(model_registry, (scene_tasks.c.model_id == model_registry.c.id) &
                             (scene_tasks.c.owner == model_registry.c.owner)))

    @staticmethod
    def _joined_model(row):
        return {column.name: row['asset_' + column.name] for column in model_registry.c}

    def _readiness(self, model):
        ready, reason = bool(model['enabled']), ''
        try:
            self.verify_asset(model)
        except HTTPException as exc:
            ready, reason = False, exc.detail
        if not model['enabled']:
            reason = '关联模型已停用'
        return ready, reason

    def _public_scene(self, row, model, readiness):
        ready, reason = readiness
        return dict(id=row['id'], revision=row['revision'], **row['config'],
                    model=self.public_model(model), ready=ready, readiness_reason=reason,
                    updated_at=row['updated_at'])

    def _joined_scene(self, identity, owner=None):
        query = self._joined_query().where(scene_tasks.c.id == str(identity))
        if owner is not None:
            query = query.where(scene_tasks.c.owner == owner)
        with self.engine.connect() as c:
            row = c.execute(query).mappings().first()
        if not row:
            raise HTTPException(404, '检测任务不存在')
        return row, self._joined_model(row)

    def scene(self, identity, owner=None):
        row, model = self._joined_scene(identity, owner)
        if owner is None and row['status'] != 'published':
            raise HTTPException(404, '检测任务未发布')
        return self._public_scene(row, model, self._readiness(model))

    def scenes(self, owner=None):
        query = self._joined_query().order_by(scene_tasks.c.updated_at.desc(), scene_tasks.c.id.desc())
        query = query.where(scene_tasks.c.owner == owner) if owner is not None else query.where(scene_tasks.c.status == 'published')
        with self.engine.connect() as c:
            rows = c.execute(query).mappings().all()
        checks, result = {}, []
        for row in rows:
            model = self._joined_model(row)
            if model['id'] not in checks:
                checks[model['id']] = self._readiness(model)
            result.append(self._public_scene(row, model, checks[model['id']]))
        return result

    def snapshot(self, identity, owner=None):
        row, model = self._joined_scene(identity, owner)
        if row['status'] != 'published':
            raise HTTPException(409, '检测任务尚未发布')
        if not model['enabled']:
            raise HTTPException(409, '关联模型已停用')
        path = self.verify_asset(model)
        frozen = dict(scene_id=row['id'], revision=row['revision'], config=row['config'],
                      model=self.public_model(model), model_path=str(path))
        frozen['sha256'] = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return frozen
