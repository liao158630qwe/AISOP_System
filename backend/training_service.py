"""Durable industrial training with isolated execution and fenced publication."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from typing import Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from algorithms.industrial_training import candidate_gate, industrial_audit
from algorithms.training import (audit_dataset, evaluate_model, train_model,
                                 validate_recipe, write_json, _checked_copy)
from algorithms.video import file_hash
from .config import ROOT, Settings
from .db import digest, make_engine, model_registry, now, training_jobs, training_runs
from .scenes import Strict, inspect_model

log = logging.getLogger('training')
ACTIVE = ('QUEUED', 'RUNNING', 'CANCELLING')
GROUPS = {'queued': ('QUEUED',), 'running': ('RUNNING', 'CANCELLING'),
          'candidate': ('CANDIDATE',), 'attention': ('FAILED', 'REJECTED'),
          'cancelled': ('CANCELLED',)}


def list_runs(engine, owner, *, limit=25, offset=0, search='', station='all', status='all'):
    """Bounded history projection: reports stay on the detail/download endpoints."""
    columns = [training_runs.c[k] for k in ('id', 'name', 'station', 'stage', 'state',
        'progress', 'message', 'model_id', 'created_at', 'updated_at')]
    columns.append(training_runs.c.recipe['task'].as_string().label('task'))
    conditions = [training_runs.c.owner == owner]
    if station != 'all':
        conditions.append(training_runs.c.station == station)
    if status != 'all':
        conditions.append(training_runs.c.state.in_(GROUPS[status]))
    if search.strip():
        # Escape SQL wildcard characters so search has literal substring semantics.
        text = search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append(or_(training_runs.c.name.ilike('%' + text + '%', escape='\\'),
                              training_runs.c.id.ilike('%' + text + '%', escape='\\')))
    with engine.connect() as c:
        counts = dict(c.execute(select(training_runs.c.state, func.count())
            .where(training_runs.c.owner == owner).group_by(training_runs.c.state)).all())
        total = c.execute(select(func.count()).select_from(training_runs).where(*conditions)).scalar_one()
        rows = c.execute(select(*columns).where(*conditions)
            .order_by(training_runs.c.created_at.desc(), training_runs.c.id.desc())
            .offset(offset).limit(limit)).mappings().all()
    return {'items': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit,
            'has_more': offset + len(rows) < total,
            'counts': {key: sum(counts.get(state, 0) for state in states) for key, states in GROUPS.items()}}


class Policy(Strict):
    min_batches_per_split: int = Field(default=3, ge=2, le=1000, strict=True)
    min_class_samples: int = Field(default=20, ge=5, le=100000, strict=True)
    min_negative_samples: int = Field(default=20, ge=5, le=100000, strict=True)
    min_precision: float = Field(default=.95, ge=.5, le=1)
    min_recall: float = Field(default=.95, ge=.5, le=1)


class CreateTraining(Strict):
    name: str = Field(min_length=1, max_length=100)
    station: str = Field(pattern=r'^station[1-6]$')
    recipe: dict
    policy: Policy = Field(default_factory=Policy)


def _key(request):
    key = request.headers.get('Idempotency-Key', '')
    try:
        return str(uuid.UUID(key))
    except ValueError:
        raise HTTPException(400, '训练请求需要UUID格式的Idempotency-Key')


def normalize_recipe(value, settings, engine=None, owner=None, station=None):
    dataset_export = None
    if isinstance(value.get('dataset'), str) and value['dataset'].startswith('dataset://'):
        from .dataset_service import resolve_export
        dataset_export = resolve_export(engine, settings, value['dataset'][len('dataset://'):], owner)
        if dataset_export['task'] != value.get('task') or (station and dataset_export['station'] != station):
            raise ValueError('冻结数据版本的任务类型或工位与训练配置不一致')
        value = dict(value, dataset=dataset_export['path'])
    if isinstance(value.get('pretrained'), str) and value['pretrained'].startswith('inbox/'):
        inbox = (settings.data_dir / 'model-imports').resolve()
        path = (inbox / value['pretrained'][len('inbox/'):]).resolve()
        if not path.is_relative_to(inbox):
            raise ValueError('模型导入目录中的路径无效')
        value = dict(value, pretrained=str(path))
    if isinstance(value.get('pretrained'), str) and value['pretrained'].startswith('model://'):
        from .scenes import SceneRegistry
        registry = SceneRegistry(engine, settings)
        model = registry._model(value['pretrained'][len('model://'):], owner)
        if model['task'] != value.get('task'):
            raise ValueError('所选模型类型与训练类型不一致')
        value = dict(value, pretrained=str(registry.verify_asset(model)))
    value, _ = validate_recipe(value, ROOT)
    if value['task'] not in ('detect', 'obb', 'classify', 'feature-classify'):
        raise ValueError('后台准入当前支持检测、旋转框、分类和特征分类；分割/姿态请用CLI独立验证')
    allowed = {'backend', 'task', 'dataset', 'pretrained', 'seed', 'train', 'feature_schema', 'evaluation'}
    if set(value) - allowed:
        raise ValueError('recipe包含不支持的字段')
    if value['seed'] > 2147483647:
        raise ValueError('seed不能超过2147483647')
    if value['task'] == 'feature-classify' and (not isinstance(value.get('feature_schema'), str)
            or not 1 <= len(value['feature_schema'].strip()) <= 200):
        raise ValueError('特征模式必须填写200字符以内的提取器及特征顺序版本')
    for key in ('dataset', 'pretrained'):
        if not value.get(key):
            continue
        path = Path(value[key]).resolve()
        if key == 'dataset' and path.is_relative_to((settings.data_dir / 'video-datasets').resolve()) and not dataset_export:
            raise ValueError('视频数据集必须通过已审计的 dataset:// 版本引用提交')
        if not any(path.is_relative_to(p.resolve()) for p in (ROOT, settings.data_dir)) or not path.is_file():
            raise ValueError(f'{key}必须位于项目或DATA_DIR内且文件存在')
        if key == 'pretrained' and path.is_relative_to((settings.data_dir / 'registered-models').resolve()):
            with engine.connect() as c:
                registered = c.execute(select(model_registry).where(model_registry.c.owner == owner,
                    model_registry.c.path == path.relative_to(settings.data_dir).as_posix())).mappings().first()
            if not registered:
                raise ValueError('初始模型不属于当前账户')
            if registered['task'] != value['task'] or file_hash(path) != registered['sha256']:
                raise ValueError('初始模型类型不匹配或文件发生变化')
    if value['backend'] == 'ultralytics':
        if Path(value['pretrained']).suffix.lower() != '.pt':
            raise ValueError('后台训练只接受可信本地.pt初始权重')
        train = value.setdefault('train', {})
        from algorithms.gpu import cuda_index
        if train.get('workers', 0) != 0:
            raise ValueError('后台训练要求workers=0，避免数据加载进程挤占服务资源')
        train.update(device=cuda_index(train.get('device', os.getenv('TRAINING_DEVICE', 'cuda:0'))), workers=0)
        for key, default, low, high in [('epochs', 100, 1, 1000), ('batch', 8, 1, 32),
                                        ('imgsz', 640, 32, 1280), ('patience', 20, 1, 1000)]:
            n = train.get(key, default)
            if isinstance(n, bool) or not isinstance(n, int) or not low <= n <= high:
                raise ValueError(f'{key}必须为{low}–{high}的整数')
            train[key] = n
        if train['imgsz'] % 32:
            raise ValueError('imgsz必须为32的倍数')
        value.setdefault('evaluation', {}).setdefault('confidence', .6)
    else:
        from algorithms.gpu import cuda_index
        value['backend'] = 'torch'
        train = value.setdefault('train', {})
        train['device'] = cuda_index(train.get('device', os.getenv('TRAINING_DEVICE', 'cuda:0')))
    if dataset_export:
        value['_dataset_ref'] = dataset_export['reference']
        value['_dataset_audit_sha256'] = dataset_export['audit']['sha256']
    return value


def public_run(row):
    return {k: row[k] for k in ('id', 'name', 'station', 'stage', 'state', 'recipe', 'policy',
        'progress', 'message', 'audit', 'card', 'evaluation', 'model_id', 'created_at', 'updated_at')}


def get_run(engine, identity, owner):
    with engine.connect() as c:
        row = c.execute(select(training_runs).where(training_runs.c.id == str(identity),
                                                    training_runs.c.owner == owner)).mappings().first()
    if not row:
        raise HTTPException(404, '训练任务不存在')
    return public_run(row)


def create_run(engine, settings, owner, key, body):
    # Hash the request before touching mutable filesystem inputs so network retries
    # still return the same immutable task if the source files later move/change.
    rhash = digest(json.dumps(body.model_dump(mode='json'), sort_keys=True, ensure_ascii=False))
    def existing():
        with engine.connect() as c:
            old = c.execute(select(training_runs).where(training_runs.c.owner == owner,
                training_runs.c.request_key == key)).mappings().first()
        if old:
            if old['request_hash'] != rhash:
                raise HTTPException(409, '同一幂等键对应不同训练请求')
            return public_run(old)
    old = existing()
    if old:
        return old
    try:
        recipe = normalize_recipe(body.recipe, settings, engine, owner, body.station)
        recipe['_source_manifest_sha256'] = file_hash(recipe['dataset'])
        recipe['_pretrained_sha256'] = file_hash(recipe['pretrained']) if recipe.get('pretrained') else None
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    identity = str(uuid.uuid4())
    try:
        with engine.begin() as c:
            t = now(c)
            c.execute(training_runs.insert().values(id=identity, owner=owner, request_key=key,
                request_hash=rhash, name=body.name, station=body.station, recipe=recipe,
                policy=body.policy.model_dump(), stage='QUEUED', state='QUEUED', progress=0,
                message='等待GPU训练工作器', created_at=t, updated_at=t))
            c.execute(training_jobs.insert().values(id=str(uuid.uuid4()), run_id=identity,
                owner=owner, request_key=key, kind='TRAIN', status='QUEUED', progress=0,
                message='等待领取', created_at=t, updated_at=t))
    except IntegrityError:
        old = existing()
        if old:
            return old
        raise
    return get_run(engine, identity, owner)


def recover_expired(engine):
    with engine.begin() as c:
        t = now(c)
        stale = c.execute(select(training_jobs).where(training_jobs.c.status.in_(('RUNNING', 'CANCELLING')),
            training_jobs.c.lease_until < t).with_for_update(skip_locked=True)).mappings().all()
        for row in stale:
            state = 'CANCELLED' if row['status'] == 'CANCELLING' else 'FAILED'
            msg = '工作器租约过期；结果未提交，需创建新任务'
            changed = c.execute(update(training_jobs).where(training_jobs.c.id == row['id'],
                training_jobs.c.token == row['token'], training_jobs.c.status == row['status'],
                training_jobs.c.lease_until < t).values(status=state, active_run=None,
                    active_slot=None, lease_until=None, message=msg, updated_at=t))
            if changed.rowcount:
                c.execute(update(training_runs).where(training_runs.c.id == row['run_id'],
                    training_runs.c.state.in_(ACTIVE)).values(state=state, stage=state, message=msg, updated_at=t))


def claim_training(engine, lease_seconds):
    recover_expired(engine)
    try:
        with engine.begin() as c:
            if c.execute(select(training_jobs.c.id).where(training_jobs.c.active_slot.is_not(None))).first():
                return None
            row = c.execute(select(training_jobs).where(training_jobs.c.status == 'QUEUED')
                .order_by(training_jobs.c.created_at, training_jobs.c.id).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not row:
                return None
            t, token = now(c), str(uuid.uuid4())
            changed = c.execute(update(training_jobs).where(training_jobs.c.id == row['id'],
                training_jobs.c.status == 'QUEUED').values(status='RUNNING', token=token,
                    lease_until=t + lease_seconds, active_run=row['run_id'], active_slot='training-gpu', updated_at=t))
            if not changed.rowcount:
                return None
            c.execute(update(training_runs).where(training_runs.c.id == row['run_id'],
                training_runs.c.state == 'QUEUED').values(state='RUNNING', stage='AUDIT',
                    progress=.01, message='审计工业数据集', updated_at=t))
            return dict(c.execute(select(training_jobs).where(training_jobs.c.id == row['id'])).mappings().one())
    except IntegrityError:
        # Unique active_slot arbitrates concurrent workers on SQLite/PostgreSQL.
        return None


def _owned(job, t, statuses=('RUNNING',)):
    return (training_jobs.c.id == job['id'], training_jobs.c.token == job['token'],
            training_jobs.c.status.in_(statuses), training_jobs.c.lease_until >= t)


def progress(engine, job, stage, fraction, message, **fields):
    with engine.begin() as c:
        t = now(c)
        changed = c.execute(update(training_jobs).where(*_owned(job, t)).values(
            progress=min(.99, fraction), message=message[:1000], updated_at=t))
        if not changed.rowcount:
            raise RuntimeError('任务已取消或工作器失去租约，停止提交')
        c.execute(update(training_runs).where(training_runs.c.id == job['run_id'],
            training_runs.c.state == 'RUNNING').values(stage=stage, progress=min(.99, fraction),
                message=message[:1000], updated_at=t, **fields))


def finish(engine, job, state, message, *, registration=None, **fields):
    with engine.begin() as c:
        t = now(c)
        statuses = ('RUNNING', 'CANCELLING') if state == 'CANCELLED' else ('RUNNING',)
        changed = c.execute(update(training_jobs).where(*_owned(job, t, statuses)).values(
            status=state, active_run=None, active_slot=None, lease_until=None, progress=1,
            message=message[:1000], updated_at=t, result={'state': state}))
        if not changed.rowcount:
            return False
        if registration:
            c.execute(model_registry.insert().values(**registration, created_at=t))
            fields['model_id'] = registration['id']
        c.execute(update(training_runs).where(training_runs.c.id == job['run_id'],
            training_runs.c.state.in_(statuses)).values(state=state, stage=state, progress=1,
                message=message[:1000], updated_at=t, **fields))
    return True


def cancel(engine, identity, owner):
    get_run(engine, identity, owner)
    with engine.begin() as c:
        row = c.execute(select(training_jobs).where(training_jobs.c.run_id == str(identity),
            training_jobs.c.owner == owner).with_for_update()).mappings().one()
        if row['status'] not in ('QUEUED', 'RUNNING'):
            return get_run(engine, identity, owner)
        state = 'CANCELLED' if row['status'] == 'QUEUED' else 'CANCELLING'
        fields = dict(status=state, message='已请求取消', updated_at=now(c))
        if state == 'CANCELLED':
            fields.update(active_run=None, active_slot=None, lease_until=None)
        changed = c.execute(update(training_jobs).where(training_jobs.c.id == row['id'],
            training_jobs.c.status == row['status']).values(**fields))
        if changed.rowcount:
            c.execute(update(training_runs).where(training_runs.c.id == str(identity),
                training_runs.c.state == row['status']).values(state=state, stage=state,
                    message='已请求取消', updated_at=now(c)))
    return get_run(engine, identity, owner)


def freeze_inputs(recipe, station, policy, folder):
    if file_hash(recipe['dataset']) != recipe['_source_manifest_sha256']:
        raise ValueError('创建任务后数据清单发生变化，请创建新任务')
    summary = industrial_audit(recipe, station, policy)
    if recipe.get('_dataset_audit_sha256') and summary['sha256'] != recipe['_dataset_audit_sha256']:
        raise ValueError('已审核的视频数据版本内容发生变化，请重新生成版本')
    audit = audit_dataset(recipe['dataset'], recipe['task'])
    if audit['sha256'] != summary['sha256']:
        raise ValueError('数据在审计期间发生变化')
    frozen = {k: v for k, v in recipe.items() if not k.startswith('_')}
    data = dict(audit['dataset'])
    samples = []
    for i, sample in enumerate(data['samples']):
        record = {k: v for k, v in sample.items() if k not in ('sha256', 'label_sha256')}
        source = Path(audit['root']) / sample['file']
        record['file'] = f'samples/{i:09d}' + source.suffix.lower()
        _checked_copy(source, folder / record['file'], sample['sha256'])
        if recipe['task'] not in ('classify', 'feature-classify'):
            record['label'] = f'labels/{i:09d}.txt'
            _checked_copy(Path(audit['root']) / sample['label'], folder / record['label'], sample['label_sha256'])
        samples.append(record)
    data['samples'] = samples
    write_json(folder / 'manifest.json', data)
    frozen['dataset'] = str(folder / 'manifest.json')
    if frozen.get('pretrained'):
        path = folder / 'initial.pt'
        _checked_copy(frozen['pretrained'], path, recipe['_pretrained_sha256'])
        frozen['pretrained'] = str(path)
    snapshot = industrial_audit(frozen, station, policy)
    snapshot['source_sha256'] = summary['sha256']
    return frozen, snapshot


def execute_training(engine, settings, job):
    with engine.connect() as c:
        run = dict(c.execute(select(training_runs).where(training_runs.c.id == job['run_id'])).mappings().one())
    folder = settings.data_dir / 'training' / run['id']
    try:
        from algorithms.gpu import training_device
        device = training_device(run['recipe'].get('train', {}).get('device'))
        import torch
        fraction = float(os.getenv('TRAINING_GPU_MEMORY_FRACTION', '.25'))
        if not 0 < fraction <= 1:
            raise ValueError('TRAINING_GPU_MEMORY_FRACTION 必须在 (0, 1] 内')
        torch.cuda.set_per_process_memory_fraction(fraction, device)
        progress(engine, job, 'AUDIT', .01, '审计来源、批次、周期、负样本与类别覆盖')
        folder.mkdir(parents=True, exist_ok=False)
        frozen, audit = freeze_inputs(run['recipe'], run['station'], run['policy'], folder / 'snapshot')
        write_json(folder / 'recipe.json', frozen)
        write_json(folder / 'industrial-audit.json', audit)
        progress(engine, job, 'TRAIN', .08, f'数据已冻结，开始CUDA GPU {device}训练', audit=audit)
        card = train_model(folder / 'recipe.json', folder / 'fit',
            progress=lambda f, msg: progress(engine, job, 'TRAIN', .08 + .62 * f, msg))
        card['execution'] = dict(device=f'cuda:{device}', cpuFallback=False, memoryFraction=fraction,
            torch_threads=torch.get_num_threads(), data_workers=0)
        if card['dataset_sha256'] != audit['sha256']:
            raise ValueError('训练使用的数据与工业审计快照不一致')
        progress(engine, job, 'TEST', .75, '候选已冻结，执行独立测试', card=card)
        evaluation = evaluate_model(folder / 'fit', folder / 'evaluation')
        evaluation['gate'] = candidate_gate(evaluation, run['policy'])
        card.update(test_evaluated=True, production_qualified=False)
        write_json(folder / 'model-card.json', card)
        write_json(folder / 'evaluation' / 'test-evaluation.json', evaluation)
        if not evaluation['gate']['passed']:
            finish(engine, job, 'REJECTED', '独立测试未达标：' + '；'.join(evaluation['gate']['failures'][:4]),
                   card=card, evaluation=evaluation)
            return
        progress(engine, job, 'REGISTER', .95, '独立测试通过，登记停用候选')
        registration = None
        if card['task'] != 'feature-classify':
            identity = str(uuid.uuid4())
            path = settings.data_dir / 'registered-models' / (identity + '.pt')
            _checked_copy(card['model'], path, card['model_sha256'])
            task, classes = inspect_model(path)
            if task != card['task'] or len(classes) != len(audit['names']):
                raise ValueError('登记模型任务/类别与训练结果不一致')
            registration = dict(id=identity, owner=run['owner'], name=run['name'], version='train-' + run['id'][:8],
                task=task, classes={str(i): n for i, n in enumerate(audit['names'])},
                path=path.relative_to(settings.data_dir).as_posix(), sha256=file_hash(path), size=path.stat().st_size,
                enabled=False, notes=f'训练任务 {run["id"]}；{run["station"]}；仅模型测试通过，现场规则与工件验收待完成')
        msg = '测试通过；候选模型已停用登记，现场规则与工件验收待完成' if registration else '特征分类测试通过；需要专用推理适配器，不能直接发布场景'
        finish(engine, job, 'CANDIDATE', msg, registration=registration, card=card, evaluation=evaluation)
    except Exception as exc:
        log.exception('Training failed: %s', run['id'])
        finish(engine, job, 'FAILED', str(exc))


def execute_one(engine, settings):
    """Supervisor runs beside inference polling; model work lives in a child process."""
    job = claim_training(engine, max(15, settings.lease_seconds))
    if not job:
        return False
    log_dir = settings.data_dir / 'training-logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DATABASE_URL=settings.database_url, DATA_DIR=str(settings.data_dir),
        APP_ENV=settings.environment, APP_USERNAME=settings.username, APP_PASSWORD=settings.password,
        COOKIE_SECURE=str(settings.secure_cookie).lower(), MODEL_PATH=str(settings.model_path),
        OMP_NUM_THREADS='2', MKL_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2', NUMEXPR_NUM_THREADS='2',
        PYTHONUNBUFFERED='1')
    child = None
    try:
        with (log_dir / (job['run_id'] + '.log')).open('ab') as handle:
            child = subprocess.Popen([sys.executable, '-m', 'backend.training_service',
                '--job', job['id'], '--token', job['token']], cwd=ROOT, env=env,
                stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while child.poll() is None:
                with engine.begin() as c:
                    t = now(c)
                    changed = c.execute(update(training_jobs).where(*_owned(job, t)).values(
                        lease_until=t + max(15, settings.lease_seconds)))
                if not changed.rowcount:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                    finish(engine, job, 'CANCELLED', '已取消训练，未提交新模型')
                    break
                time.sleep(.5)
        with engine.connect() as c:
            state = c.execute(select(training_jobs.c.status).where(training_jobs.c.id == job['id'])).scalar_one()
        if state == 'CANCELLING':
            finish(engine, job, 'CANCELLED', '已取消训练，未提交新模型')
        else:
            finish(engine, job, 'FAILED', f'训练进程退出（{child.returncode}）且未提交完整结果，请查看任务日志')
    except Exception as exc:
        log.exception('Training supervisor failed')
        finish(engine, job, 'FAILED', str(exc))
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    return True


def poll(engine, settings, stop):
    while not stop.is_set():
        try:
            worked = execute_one(engine, settings)
        except Exception:
            log.exception('Training polling failed')
            worked = False
        if not worked:
            stop.wait(2)


def register(app, auth):
    engine, settings = app.state.engine, app.state.settings

    @app.get('/api/v1/admin/training-runs')
    def listing(request: Request, limit: int = Query(default=25, ge=1, le=100),
                offset: int = Query(default=0, ge=0), search: str = Query(default='', max_length=100),
                station: str = Query(default='all', pattern=r'^(all|station[1-6])$'),
                status: Literal['all', 'queued', 'running', 'candidate', 'attention', 'cancelled'] = 'all'):
        owner = auth(request)['owner']
        return {**list_runs(engine, owner, limit=limit, offset=offset, search=search, station=station, status=status),
                'defaults': Policy().model_dump(),
                'execution': 'CUDA GPU / 禁止CPU回退 / 单任务 / 独立进程'}

    @app.post('/api/v1/admin/training-runs', status_code=202)
    def create(body: CreateTraining, request: Request):
        return create_run(engine, settings, auth(request)['owner'], _key(request), body)

    @app.get('/api/v1/admin/training-runs/{identity}')
    def detail(identity: uuid.UUID, request: Request):
        owner = auth(request)['owner']
        return get_run(engine, identity, owner)

    @app.post('/api/v1/admin/training-runs/{identity}/cancel')
    def cancelling(identity: uuid.UUID, request: Request):
        return cancel(engine, identity, auth(request)['owner'])

    @app.get('/api/v1/admin/training-runs/{identity}/report')
    def report(identity: uuid.UUID, request: Request):
        row = get_run(engine, identity, auth(request)['owner'])
        return JSONResponse(row, headers={'Content-Disposition': f'attachment; filename="training-{identity}.json"'})

    @app.get('/api/v1/admin/training-runs/{identity}/log')
    def log_tail(identity: uuid.UUID, request: Request):
        get_run(engine, identity, auth(request)['owner'])
        path = settings.data_dir / 'training-logs' / (str(identity) + '.log')
        if not path.is_file():
            return {'text': '暂无工作器日志', 'truncated': False}
        with path.open('rb') as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 65536))
            tail = handle.read(65536).decode('utf-8', errors='replace')
        return {'text': tail, 'truncated': size > 65536}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    import torch
    import cv2
    # Host threads only handle loading/serialization. Model math stays on CUDA.
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    cv2.setNumThreads(2)
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    with engine.connect() as c:
        initial_time = now(c)
        row = c.execute(select(training_jobs).where(training_jobs.c.id == args.job,
            training_jobs.c.token == args.token, training_jobs.c.status == 'RUNNING',
            training_jobs.c.lease_until >= initial_time)).mappings().first()
    if row:
        stopped = threading.Event()
        def watch_lease():
            # Parent crashes must not leave an orphan occupying GPU memory.
            deadline = time.monotonic() + max(0, row['lease_until'] - initial_time)
            while not stopped.wait(1):
                try:
                    with engine.connect() as c:
                        t = now(c)
                        live = c.execute(select(training_jobs).where(training_jobs.c.id == args.job,
                            training_jobs.c.token == args.token)).mappings().first()
                    if not live or live['status'] not in ('RUNNING',):
                        os._exit(3)
                    if live['lease_until'] < t:
                        os._exit(3)
                    deadline = time.monotonic() + live['lease_until'] - t
                except Exception:
                    if time.monotonic() >= deadline:
                        os._exit(3)
        threading.Thread(target=watch_lease, name='training-lease-watch', daemon=True).start()
        try:
            execute_training(engine, settings, dict(row))
        finally:
            stopped.set()
    engine.dispose()


if __name__ == '__main__':
    main()
