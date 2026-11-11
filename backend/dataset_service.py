"""Video datasets: durable CPU preparation, explicit review and immutable exports."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Literal
import uuid

from anyio import fail_after
from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from algorithms.video import file_hash, probe
from .config import ROOT, Settings
from .db import (dataset_frames as frames, dataset_jobs as jobs, dataset_videos as videos,
                 training_datasets as datasets, digest, make_engine, now, serialized_write)
from .scenes import Strict
from .training_service import Policy

log = logging.getLogger('datasets')
ACTIVE = ('QUEUED', 'RUNNING')
MAX_VIDEOS, MAX_FRAMES, MAX_BYTES = 100, 3000, 10 * 1024**3
LEASE = 30
MAX_EXPORTS = 20


def require_space(path, required=0):
    if shutil.disk_usage(path).free < required + 1024**3:
        raise ValueError('数据磁盘剩余空间不足（需保留至少1 GiB），请释放空间后重试')


class NewDataset(Strict):
    name: str = Field(min_length=1, max_length=100)
    station: str = Field(pattern=r'^station[1-6]$')
    task: Literal['detect', 'classify'] = 'detect'
    classes: list[str] = Field(min_length=1, max_length=80)

    @model_validator(mode='after')
    def names(self):
        self.classes = [value.strip() for value in self.classes]
        if any(not value or len(value) > 60 for value in self.classes) or len(set(self.classes)) != len(self.classes):
            raise ValueError('类别需唯一、非空且不超过60字符')
        return self


class Capture(Strict):
    split: Literal['train', 'val', 'test']
    batch: str = Field(min_length=1, max_length=64)
    cycle: str = Field(min_length=1, max_length=64)
    product: str = Field(min_length=1, max_length=64)
    camera: str = Field(min_length=1, max_length=64)
    lighting: str = Field(min_length=1, max_length=32)
    occlusion: str = Field(min_length=1, max_length=32)


class Box(Strict):
    class_id: int = Field(ge=0, strict=True)
    xyxy: tuple[float, float, float, float]

    @model_validator(mode='after')
    def valid(self):
        a, b, c, d = self.xyxy
        if not 0 <= a < c <= 1 or not 0 <= b < d <= 1:
            raise ValueError('目标框必须在图片内且具有宽度、高度')
        return self


class Review(Strict):
    revision: int = Field(ge=1, strict=True)
    review: Literal['labelled', 'negative', 'excluded', 'pending']
    boxes: list[Box] = Field(default_factory=list, max_length=100)
    class_id: int | None = Field(default=None, ge=0, strict=True)


class Export(Strict):
    revision: int = Field(ge=1, strict=True)
    policy: Policy = Field(default_factory=Policy)


def identity(request, scope):
    try:
        key = str(uuid.UUID(request.headers.get('Idempotency-Key', '')))
    except ValueError as exc:
        raise HTTPException(400, '请求需要UUID格式的Idempotency-Key') from exc
    return str(uuid.uuid5(uuid.NAMESPACE_URL, scope + ':' + key))


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False))


def public_job(row):
    return {key: row[key] for key in ('id', 'kind', 'state', 'progress', 'message', 'result', 'created_at', 'updated_at')}


def attempt_relative(job):
    # Token already uniquely names an attempt; avoid two UUIDs in deep Windows paths.
    return f'{job["dataset_id"]}/attempts/{uuid.UUID(job["token"]).hex}'


class DatasetService:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.root = settings.data_dir / 'video-datasets'
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_slots = threading.BoundedSemaphore(2)

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError('数据存储路径无效')
        return path

    def owned(self, c, did, owner, lock=False):
        query = select(datasets).where(datasets.c.id == str(did), datasets.c.owner == owner)
        row = c.execute(query.with_for_update() if lock else query).mappings().first()
        if not row:
            raise HTTPException(404, '数据集不存在')
        return dict(row)

    @staticmethod
    def editable(row):
        if row['active_export']:
            raise HTTPException(409, '正在生成冻结版本，请等待完成后再编辑数据')

    @staticmethod
    def touch(c, did):
        c.execute(update(datasets).where(datasets.c.id == did).values(revision=datasets.c.revision + 1, updated_at=now(c)))

    def create(self, owner, did, body):
        try:
            return self._create(owner, did, body)
        except IntegrityError:
            # Concurrent first requests can both observe an absent PostgreSQL row.
            return self._create(owner, did, body)

    def _create(self, owner, did, body):
        with serialized_write(self.engine) as c:
            previous = c.execute(select(datasets).where(datasets.c.id == did)).mappings().first()
            if previous:
                if previous['owner'] != owner or any(previous[key] != value for key, value in body.model_dump().items()):
                    raise HTTPException(409, '同一请求编号对应不同的数据集')
                return dict(previous)
            t = now(c)
            row = dict(id=did, owner=owner, **body.model_dump(), revision=1, active_export=None, created_at=t, updated_at=t)
            c.execute(datasets.insert().values(**row))
        return row

    def catalog(self, owner, offset=0):
        with self.engine.connect() as c:
            total = c.execute(select(func.count()).select_from(datasets).where(datasets.c.owner == owner)).scalar_one()
            rows = c.execute(select(datasets).where(datasets.c.owner == owner)
                .order_by(datasets.c.created_at.desc(), datasets.c.id.desc()).offset(offset).limit(25)).mappings().all()
        return {'items': [dict(row) for row in rows], 'total': total, 'has_more': offset + len(rows) < total}

    def detail(self, owner, did):
        with self.engine.connect() as c:
            dataset = self.owned(c, did, owner)
            source = videos.join(jobs, videos.c.job_id == jobs.c.id)
            video_rows = c.execute(select(videos, jobs.c.state, jobs.c.message, jobs.c.progress).select_from(source)
                .where(videos.c.dataset_id == str(did)).order_by(videos.c.created_at, videos.c.id)).mappings().all()
            review_counts = dict(c.execute(select(frames.c.review, func.count()).where(frames.c.dataset_id == str(did))
                .group_by(frames.c.review)).all())
            exports = c.execute(select(jobs).where(jobs.c.dataset_id == str(did), jobs.c.kind == 'EXPORT')
                .order_by(jobs.c.created_at.desc()).limit(10)).mappings().all()
            # Compact annotations only; image bytes and full manifests never enter a list response.
            reviewed = c.execute(select(frames.c.video_id, frames.c.review, frames.c.annotation)
                .where(frames.c.dataset_id == str(did), frames.c.review.in_(('labelled', 'negative')))).mappings().all()
        by_video = {v['id']: v for v in video_rows}
        coverage = {split: {'samples': 0, 'batches': set(), 'negatives': 0, 'classes': [0] * len(dataset['classes'])}
                    for split in ('train', 'val', 'test')}
        for frame in reviewed:
            capture = by_video[frame['video_id']]['capture']
            entry = coverage[capture['split']]
            entry['samples'] += 1
            entry['batches'].add(capture['batch'])
            entry['negatives'] += frame['review'] == 'negative'
            class_ids = {b['class_id'] for b in frame['annotation'].get('boxes', [])} if dataset['task'] == 'detect' else {frame['annotation']['class_id']}
            for cid in class_ids:
                entry['classes'][cid] += 1
        for value in coverage.values():
            value['batches'] = len(value['batches'])
        return {**dataset, 'videos': [{key: v[key] for key in ('id', 'name', 'capture', 'metadata', 'state', 'message', 'progress', 'job_id', 'frame_budget')} for v in video_rows],
                'reviews': review_counts, 'coverage': coverage, 'exports': [public_job(row) for row in exports],
                'limits': {'max_upload_bytes': self.settings.max_upload_bytes, 'max_video_seconds': self.settings.max_video_seconds,
                           'max_videos': MAX_VIDEOS, 'max_frames': MAX_FRAMES, 'max_bytes': MAX_BYTES}}

    def accept_video(self, owner, did, vid, name, path, size, sha, capture, frame_budget):
        request_hash = fingerprint([name, sha, capture.model_dump(), frame_budget])
        with serialized_write(self.engine) as c:
            dataset = self.owned(c, did, owner, True)
            previous = c.execute(select(videos).where(videos.c.id == vid)).mappings().first()
            if previous:
                if previous['dataset_id'] != str(did) or previous['request_hash'] != request_hash:
                    raise HTTPException(409, '同一上传请求对应不同的视频或采集信息')
                return {'id': vid, 'job_id': previous['job_id'], 'reused': True}
            self.editable(dataset)
            all_videos = c.execute(select(videos).where(videos.c.dataset_id == str(did))).mappings().all()
            if len(all_videos) >= MAX_VIDEOS or sum(v['size'] for v in all_videos) + size > MAX_BYTES:
                raise HTTPException(413, '单数据集最多100段视频、10 GiB，请新建数据集')
            if sum(v['frame_budget'] for v in all_videos) + frame_budget > MAX_FRAMES:
                raise HTTPException(413, '单数据集抽帧预算最多3000张，请减少每段抽帧数或新建数据集')
            for old in all_videos:
                if old['sha256'] == sha:
                    raise HTTPException(409, '该视频已上传，不能复制到另一集合；请在原记录继续标注')
                for key in ('batch', 'cycle'):
                    if old['capture'][key] == getattr(capture, key) and old['capture']['split'] != capture.split:
                        raise HTTPException(409, f'相同{key}必须归入同一集合，不能拆分批次或工件周期')
            t, jid = now(c), str(uuid.uuid4())
            c.execute(videos.insert().values(id=vid, dataset_id=str(did), name=name, path=path,
                size=size, sha256=sha, capture=capture.model_dump(), frame_budget=frame_budget,
                revision=1, request_hash=request_hash, job_id=jid, created_at=t))
            c.execute(jobs.insert().values(id=jid, dataset_id=str(did), owner=owner, kind='EXTRACT', state='QUEUED',
                payload={'video_id': vid}, progress=0, message='等待后台抽帧', created_at=t, updated_at=t))
            self.touch(c, str(did))
        return {'id': vid, 'job_id': jid, 'reused': False}

    def list_frames(self, owner, did, video=None, review='all', offset=0):
        with self.engine.connect() as c:
            self.owned(c, did, owner)
            conditions = [frames.c.dataset_id == str(did)]
            if video:
                conditions.append(frames.c.video_id == str(video))
            if review != 'all':
                conditions.append(frames.c.review == review)
            total = c.execute(select(func.count()).select_from(frames).where(*conditions)).scalar_one()
            rows = c.execute(select(frames).where(*conditions).order_by(frames.c.video_id, frames.c.ordinal)
                .offset(offset).limit(24)).mappings().all()
        return {'items': [{**{k: row[k] for k in ('id', 'video_id', 'timestamp', 'width', 'height', 'review', 'annotation', 'revision')},
                           'image': f'/api/v1/admin/datasets/{did}/frames/{row["id"]}/image'} for row in rows],
                'total': total, 'has_more': offset + len(rows) < total}

    def review(self, owner, did, fid, body):
        with serialized_write(self.engine) as c:
            dataset = self.owned(c, did, owner, True)
            self.editable(dataset)
            if body.review == 'labelled':
                if dataset['task'] == 'detect' and (not body.boxes or body.class_id is not None):
                    raise HTTPException(422, '目标检测需至少一个目标框；空背景请显式确认为负样本')
                if dataset['task'] == 'classify' and (body.class_id is None or body.boxes):
                    raise HTTPException(422, '图像分类需选择一个类别')
            elif body.boxes or body.class_id is not None:
                raise HTTPException(422, '空背景、排除或待标注状态不能携带标签')
            if body.review == 'negative' and dataset['task'] != 'detect':
                raise HTTPException(422, '图像分类不使用空背景标签，请选择类别或排除')
            if any(b.class_id >= len(dataset['classes']) for b in body.boxes) or (body.class_id is not None and body.class_id >= len(dataset['classes'])):
                raise HTTPException(422, '类别不属于本数据集')
            result = c.execute(update(frames).where(frames.c.id == str(fid), frames.c.dataset_id == str(did), frames.c.revision == body.revision)
                .values(review=body.review, annotation={'boxes': [box.model_dump() for box in body.boxes], 'class_id': body.class_id}, revision=body.revision + 1))
            if not result.rowcount:
                raise HTTPException(409, '标注已被其他窗口修改，请刷新后重新标注')
            self.touch(c, str(did))
        return {'id': str(fid), 'revision': body.revision + 1, 'review': body.review}

    def export(self, owner, did, jid, body):
        with serialized_write(self.engine) as c:
            dataset = self.owned(c, did, owner, True)
            previous = c.execute(select(jobs).where(jobs.c.id == jid)).mappings().first()
            if previous:
                if previous['dataset_id'] != str(did) or previous['payload'] != body.model_dump():
                    raise HTTPException(409, '同一请求编号对应不同的导出配置')
                return public_job(previous)
            self.editable(dataset)
            if dataset['revision'] != body.revision:
                raise HTTPException(409, '数据已变化，请刷新后重新生成版本')
            completed = c.execute(select(func.count()).select_from(jobs).where(jobs.c.dataset_id == str(did),
                jobs.c.kind == 'EXPORT', jobs.c.state == 'SUCCEEDED')).scalar_one()
            if completed >= MAX_EXPORTS:
                raise HTTPException(409, '本数据集已保存20个训练版本，请新建数据集继续迭代')
            if c.execute(select(jobs.c.id).where(jobs.c.dataset_id == str(did), jobs.c.state.in_(ACTIVE))).first():
                raise HTTPException(409, '请等待视频抽帧完成')
            pending = c.execute(select(func.count()).select_from(frames).where(frames.c.dataset_id == str(did), frames.c.review == 'pending')).scalar_one()
            if pending:
                raise HTTPException(422, f'还有{pending}张图片待复核，请标注、确认空背景或排除后再生成版本')
            t = now(c)
            row = dict(id=jid, dataset_id=str(did), owner=owner, kind='EXPORT', state='QUEUED', payload=body.model_dump(),
                progress=0, message='等待生成冻结版本并审计', result=None, created_at=t, updated_at=t)
            c.execute(jobs.insert().values(**row))
            c.execute(update(datasets).where(datasets.c.id == str(did)).values(active_export=jid))
        return public_job(row)

    def retry(self, owner, did, jid):
        with serialized_write(self.engine) as c:
            dataset = self.owned(c, did, owner, True)
            self.editable(dataset)
            job = c.execute(select(jobs).where(jobs.c.id == str(jid), jobs.c.dataset_id == str(did)).with_for_update()).mappings().first()
            if not job or job['kind'] != 'EXTRACT' or job['state'] != 'FAILED':
                raise HTTPException(409, '只有抽帧失败的任务可重试')
            c.execute(update(jobs).where(jobs.c.id == str(jid)).values(state='QUEUED', progress=0, message='等待重试抽帧', token=None, updated_at=now(c)))
        return {'ok': True}

    def remove_video(self, owner, did, vid):
        with serialized_write(self.engine) as c:
            dataset = self.owned(c, did, owner, True)
            self.editable(dataset)
            row = c.execute(select(videos).where(videos.c.id == str(vid), videos.c.dataset_id == str(did))).mappings().first()
            if not row:
                raise HTTPException(404, '视频不存在')
            job = c.execute(select(jobs).where(jobs.c.id == row['job_id']).with_for_update()).mappings().one()
            if job['state'] in ACTIVE:
                raise HTTPException(409, '请等待抽帧结束后移除视频')
            c.execute(delete(frames).where(frames.c.video_id == str(vid)))
            c.execute(delete(videos).where(videos.c.id == str(vid)))
            self.touch(c, str(did))
        # Immutable exports own independent image copies. Deferred GC reconciles
        # database references before deleting files, including uncertain commits.
        return {'ok': True}


def register(app, auth):
    service = DatasetService(app.state.engine, app.state.settings)
    app.state.datasets = service

    @app.get('/api/v1/admin/datasets')
    def listing(request: Request, offset: int = Query(0, ge=0)):
        return service.catalog(auth(request)['owner'], offset)

    @app.post('/api/v1/admin/datasets', status_code=201)
    def create(body: NewDataset, request: Request):
        owner = auth(request)['owner']
        return service.create(owner, identity(request, 'dataset:' + owner), body)

    @app.get('/api/v1/admin/datasets/{did}')
    def detail(did: uuid.UUID, request: Request):
        return service.detail(auth(request)['owner'], did)

    @app.post('/api/v1/admin/datasets/{did}/videos', status_code=202)
    async def upload(did: uuid.UUID, request: Request, filename: str = Query(min_length=1, max_length=200),
                     capture: str = Query(max_length=3000), frame_budget: int = Query(60, ge=1, le=300)):
        owner = (await run_in_threadpool(auth, request))['owner']
        try:
            info = Capture.model_validate_json(capture)
        except ValidationError as exc:
            raise HTTPException(422, '请完整填写集合、批次、周期、产品、机位、光照和遮挡信息') from exc
        vid = identity(request, 'video:' + str(did))
        def authorize():
            with service.engine.connect() as c:
                service.editable(service.owned(c, did, owner))
        await run_in_threadpool(authorize)
        name = filename.replace('\\', '/').split('/')[-1]
        suffix = Path(name).suffix.lower()
        if suffix not in ('.mp4', '.webm', '.mov', '.avi', '.m4v'):
            raise HTTPException(415, '支持MP4、WebM、MOV、AVI、M4V视频')
        length = request.headers.get('content-length', '')
        if length.isdecimal() and int(length) > service.settings.max_upload_bytes:
            raise HTTPException(413, '视频超过服务器上传限制')
        if not service.upload_slots.acquire(blocking=False):
            raise HTTPException(429, '已有两个视频正在上传，请稍后重试', headers={'Retry-After': '3'})
        relative = f'{did}/sources/{uuid.uuid4()}{suffix}'
        path = service.path(relative)
        temp = path.with_suffix('.part')
        size, sha, committing = 0, hashlib.sha256(), False
        try:
            try:
                await run_in_threadpool(require_space, service.root, int(length) if length.isdecimal() else 0)
            except ValueError as exc:
                raise HTTPException(507, str(exc)) from exc
            path.parent.mkdir(parents=True, exist_ok=True)
            with fail_after(600), temp.open('xb') as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > service.settings.max_upload_bytes:
                        raise HTTPException(413, '视频超过服务器上传限制')
                    await run_in_threadpool(handle.write, chunk)
                    sha.update(chunk)
                await run_in_threadpool(handle.flush)
                await run_in_threadpool(os.fsync, handle.fileno())
            if not size:
                raise HTTPException(422, '视频文件为空')
            temp.replace(path)
            committing = True
            result = await run_in_threadpool(service.accept_video, owner, did, vid, name, relative, size, sha.hexdigest(), info, frame_budget)
            if result['reused']:
                path.unlink(missing_ok=True)
            return result
        except TimeoutError as exc:
            temp.unlink(missing_ok=True)
            raise HTTPException(408, '上传超过10分钟，请重试') from exc
        except SQLAlchemyError:
            # A lost commit acknowledgement must not remove a registered source.
            temp.unlink(missing_ok=True)
            raise
        except BaseException:
            temp.unlink(missing_ok=True)
            if not committing:
                path.unlink(missing_ok=True)
            raise
        finally:
            service.upload_slots.release()

    @app.delete('/api/v1/admin/datasets/{did}/videos/{vid}')
    def remove_video(did: uuid.UUID, vid: uuid.UUID, request: Request):
        return service.remove_video(auth(request)['owner'], did, vid)

    @app.get('/api/v1/admin/datasets/{did}/frames')
    def frame_list(did: uuid.UUID, request: Request, video: uuid.UUID | None = None,
                   review: Literal['all', 'pending', 'labelled', 'negative', 'excluded'] = 'all', offset: int = Query(0, ge=0)):
        return service.list_frames(auth(request)['owner'], did, video, review, offset)

    @app.get('/api/v1/admin/datasets/{did}/frames/{fid}/image')
    def frame_image(did: uuid.UUID, fid: uuid.UUID, request: Request, thumbnail: bool = False):
        with service.engine.connect() as c:
            service.owned(c, did, auth(request)['owner'])
            row = c.execute(select(frames.c.path).where(frames.c.id == str(fid), frames.c.dataset_id == str(did))).first()
        if not row or not service.path(row.path).is_file():
            raise HTTPException(404, '图片不存在')
        path = service.path(row.path)
        if thumbnail and path.with_suffix('.thumb.jpg').is_file():
            path = path.with_suffix('.thumb.jpg')
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, no-store'})

    @app.put('/api/v1/admin/datasets/{did}/frames/{fid}')
    def review_frame(did: uuid.UUID, fid: uuid.UUID, body: Review, request: Request):
        return service.review(auth(request)['owner'], did, fid, body)

    @app.post('/api/v1/admin/datasets/{did}/exports', status_code=202)
    def export(did: uuid.UUID, body: Export, request: Request):
        return service.export(auth(request)['owner'], did, identity(request, 'export:' + str(did)), body)

    @app.post('/api/v1/admin/datasets/{did}/jobs/{jid}/retry')
    def retry(did: uuid.UUID, jid: uuid.UUID, request: Request):
        return service.retry(auth(request)['owner'], did, jid)

    @app.get('/api/v1/admin/dataset-exports/{jid}')
    def exported(jid: uuid.UUID, request: Request):
        return resolve_export(service.engine, service.settings, str(jid), auth(request)['owner'])


def resolve_export(engine, settings, jid, owner):
    with engine.connect() as c:
        row = c.execute(select(jobs).where(jobs.c.id == jid, jobs.c.owner == owner, jobs.c.kind == 'EXPORT', jobs.c.state == 'SUCCEEDED')).mappings().first()
    if not row:
        raise HTTPException(404, '冻结数据版本不存在或尚未审计通过')
    result = dict(row['result'])
    path = DatasetService(engine, settings).path(result['manifest'])
    if not path.is_file() or file_hash(path) != result['manifest_sha256']:
        raise HTTPException(409, '冻结数据清单缺失或已变化，请重新生成版本')
    return {**result, 'path': str(path), 'reference': 'dataset://' + jid}


def claim(engine):
    with serialized_write(engine) as c:
        t = now(c)
        stale = c.execute(select(jobs).where(jobs.c.state == 'RUNNING', jobs.c.lease_until < t)).mappings().all()
        for row in stale:
            c.execute(select(datasets.c.id).where(datasets.c.id == row['dataset_id']).with_for_update()).first()
            expired = c.execute(update(jobs).where(jobs.c.id == row['id'], jobs.c.state == 'RUNNING', jobs.c.lease_until < t)
                .values(state='FAILED', active_slot=None, lease_until=None,
                    message='后台处理失联；未发布结果，可重试抽帧或重新生成版本', updated_at=t))
            if expired.rowcount:
                c.execute(update(datasets).where(datasets.c.active_export == row['id']).values(active_export=None))
        if c.execute(select(jobs.c.id).where(jobs.c.active_slot.is_not(None))).first():
            return None
        row = c.execute(select(jobs).where(jobs.c.state == 'QUEUED').order_by(jobs.c.created_at, jobs.c.id)
            .with_for_update(skip_locked=True).limit(1)).mappings().first()
        if not row:
            return None
        token = str(uuid.uuid4())
        c.execute(update(jobs).where(jobs.c.id == row['id'], jobs.c.state == 'QUEUED').values(state='RUNNING', token=token,
            active_slot='dataset-cpu', lease_until=t + LEASE, message='后台处理中', updated_at=t))
        return dict(row, token=token, state='RUNNING', lease_until=t + LEASE)


def owned_job(job, t):
    return (jobs.c.id == job['id'], jobs.c.token == job['token'], jobs.c.state == 'RUNNING', jobs.c.lease_until >= t)


def progress(engine, job, value, message):
    with engine.begin() as c:
        if not c.execute(update(jobs).where(*owned_job(job, now(c))).values(progress=value, message=message, updated_at=now(c))).rowcount:
            raise RuntimeError('数据任务已失去租约，停止发布')


def finish(engine, job, state, message, result=None, extracted=None, metadata=None):
    with serialized_write(engine) as c:
        # Same lock order as API edits: dataset then job.
        c.execute(select(datasets.c.id).where(datasets.c.id == job['dataset_id']).with_for_update()).first()
        if not c.execute(update(jobs).where(*owned_job(job, now(c))).values(state=state, progress=1, message=message[:1000],
            result=result, active_slot=None, lease_until=None, updated_at=now(c))).rowcount:
            return False
        if extracted:
            c.execute(frames.insert(), extracted)
            c.execute(update(videos).where(videos.c.id == job['payload']['video_id']).values(metadata=metadata))
            DatasetService.touch(c, job['dataset_id'])
        c.execute(update(datasets).where(datasets.c.id == job['dataset_id'], datasets.c.active_export == job['id']).values(active_export=None))
    return True


def extract(service, job, folder):
    import cv2
    with service.engine.connect() as c:
        video = c.execute(select(videos).where(videos.c.id == job['payload']['video_id'])).mappings().one()
    source = service.path(video['path'])
    if file_hash(source) != video['sha256']:
        raise ValueError('原始视频内容已变化')
    meta = probe(source, service.settings.max_video_seconds)
    count = min(video['frame_budget'], meta['frame_count'])
    targets = {int(i * (meta['frame_count'] - 1) / max(1, count - 1)) for i in range(count)}
    cap = cv2.VideoCapture(str(source))
    records, seen, decoded = [], set(), 0
    try:
        for index in range(meta['frame_count']):
            if not cap.grab():
                raise ValueError('视频提前结束或存在损坏帧，未发布抽帧结果')
            if index not in targets:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                raise ValueError('抽取帧无法解码')
            if min(frame.shape[:2]) < 32 or frame.shape[0] * frame.shape[1] > 3840 * 2160:
                raise ValueError('抽取图片尺寸需至少32像素且不超过4K')
            require_space(folder, frame.nbytes)
            decoded += 1
            # Exact repeated pixels are redundant; near-duplicates are left for human review.
            key = hashlib.sha256(frame.tobytes()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            fid = str(uuid.uuid4())
            image = folder / f'{index:06d}.jpg'
            if not cv2.imwrite(str(image), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise ValueError('图片写入失败')
            ratio = min(1, 480 / frame.shape[1])
            thumb = cv2.resize(frame, (max(1, round(frame.shape[1] * ratio)), max(1, round(frame.shape[0] * ratio))))
            if not cv2.imwrite(str(image.with_suffix('.thumb.jpg')), thumb, [cv2.IMWRITE_JPEG_QUALITY, 80]):
                raise ValueError('缩略图写入失败')
            records.append(dict(id=fid, dataset_id=job['dataset_id'], video_id=video['id'], ordinal=index,
                timestamp=index / meta['fps'], path=image.relative_to(service.root).as_posix(), sha256=file_hash(image),
                width=frame.shape[1], height=frame.shape[0], review='pending', annotation={}, revision=1))
            if len(records) % 10 == 0:
                progress(service.engine, job, .9 * (index + 1) / meta['frame_count'], f'已抽取{len(records)}张，待人工复核')
        if not records:
            raise ValueError('没有可用图片')
        result = {'frames': len(records), 'sampled': decoded, 'duplicates_skipped': decoded - len(records)}
        finish(service.engine, job, 'SUCCEEDED', f'抽帧完成：{len(records)}张待标注，去除{decoded - len(records)}张完全重复帧', result, records, meta)
    finally:
        cap.release()


def export_dataset(service, job, folder):
    from algorithms.industrial_training import industrial_audit
    from algorithms.training import _checked_copy, write_json
    with service.engine.connect() as c:
        dataset = service.owned(c, job['dataset_id'], job['owner'])
        if dataset['active_export'] != job['id'] or dataset['revision'] != job['payload']['revision']:
            raise ValueError('数据版本发生变化，请重新生成')
        rows = c.execute(select(frames, videos.c.capture, videos.c.sha256.label('source_hash'))
            .join(videos, frames.c.video_id == videos.c.id)
            .where(frames.c.dataset_id == job['dataset_id'], frames.c.review.in_(('labelled', 'negative')))
            .order_by(frames.c.video_id, frames.c.ordinal)).mappings().all()
    if not rows:
        raise ValueError('没有已确认的训练样本')
    require_space(folder, sum(service.path(row['path']).stat().st_size for row in rows))
    samples = []
    for index, row in enumerate(rows):
        sample = dict(row['capture'], id=row['id'], station=dataset['station'],
            source=row['source_hash'], group=row['capture']['batch'], file=f'images/{index:06d}.jpg',
            video_id=row['video_id'], timestamp=row['timestamp'])
        _checked_copy(service.path(row['path']), folder / sample['file'], row['sha256'])
        if dataset['task'] == 'detect':
            sample['label'] = f'labels/{index:06d}.txt'
            lines = []
            for box in row['annotation'].get('boxes', []):
                a, b, c, d = box['xyxy']
                lines.append(f'{box["class_id"]} {(a+c)/2:.9f} {(b+d)/2:.9f} {c-a:.9f} {d-b:.9f}')
            path = folder / sample['label']
            path.parent.mkdir(exist_ok=True)
            path.write_text('\n'.join(lines), encoding='utf-8')
        else:
            sample['label'] = row['annotation']['class_id']
        samples.append(sample)
        if index % 50 == 0:
            progress(service.engine, job, .7 * index / len(rows), f'生成冻结样本 {index + 1}/{len(rows)}')
    manifest = folder / 'manifest.json'
    write_json(manifest, {'schema_version': 1, 'names': dataset['classes'], 'samples': samples})
    progress(service.engine, job, .8, '审计批次隔离、重复图像与逐类样本覆盖')
    audit = industrial_audit({'dataset': str(manifest), 'task': dataset['task']}, dataset['station'], job['payload']['policy'])
    result = {'manifest': manifest.relative_to(service.root).as_posix(), 'manifest_sha256': file_hash(manifest),
              'dataset_id': dataset['id'], 'revision': dataset['revision'], 'name': dataset['name'],
              'task': dataset['task'], 'station': dataset['station'], 'policy': job['payload']['policy'], 'audit': audit}
    write_json(folder / 'audit.json', audit)
    finish(service.engine, job, 'SUCCEEDED', '冻结数据版本已审计通过，可进入训练', result)


def execute_job(engine, settings, job):
    service = DatasetService(engine, settings)
    folder = service.path(attempt_relative(job))
    try:
        folder.mkdir(parents=True, exist_ok=False)
        (extract if job['kind'] == 'EXTRACT' else export_dataset)(service, job, folder)
    except Exception as exc:
        log.exception('Dataset task failed: %s', job['id'])
        finish(engine, job, 'FAILED', str(exc))


def cleanup_dataset(service, did, cutoff):
    """Only reclaim aged unreferenced files; successful frozen versions are retained."""
    with service.engine.connect() as c:
        source_paths = set(c.execute(select(videos.c.path).where(videos.c.dataset_id == did)).scalars())
        frame_paths = set(c.execute(select(frames.c.path).where(frames.c.dataset_id == did)).scalars())
        job_rows = c.execute(select(jobs).where(jobs.c.dataset_id == did)).mappings().all()
    kept_attempts = {str(Path(p).parent).replace('\\', '/') for p in frame_paths}
    for job in job_rows:
        if job['state'] == 'SUCCEEDED' and job['kind'] == 'EXPORT':
            kept_attempts.add(Path(job['result']['manifest']).parent.as_posix())
        if job['state'] == 'RUNNING':
            kept_attempts.add(attempt_relative(job))
    for category in ('sources', 'attempts'):
        parent = service.path(f'{did}/{category}')
        if not parent.is_dir():
            continue
        for path in parent.iterdir():
            if path.is_symlink() or path.stat().st_mtime >= cutoff:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(parent.resolve()) or resolved != path.absolute():
                continue
            relative = path.relative_to(service.root).as_posix()
            if category == 'sources' and path.is_file() and relative not in source_paths:
                path.unlink(missing_ok=True)
            elif category == 'attempts' and path.is_dir() and relative not in kept_attempts:
                # Paths were resolved under this dataset and no live token or
                # published result references this unique, never-reused attempt.
                shutil.rmtree(resolved)


def execute_one(engine, settings, stop=None):
    try:
        job = claim(engine)
    except IntegrityError:
        return False  # Unique slot arbitrates concurrent supervisors.
    if not job:
        return False
    env = dict(os.environ, DATABASE_URL=settings.database_url, DATA_DIR=str(settings.data_dir), APP_ENV=settings.environment,
        APP_USERNAME=settings.username, APP_PASSWORD=settings.password, COOKIE_SECURE=str(settings.secure_cookie).lower(),
        CUDA_VISIBLE_DEVICES='', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONUNBUFFERED='1')
    child = None
    started = time.monotonic()
    try:
        child = subprocess.Popen([sys.executable, '-m', 'backend.dataset_service', '--job', job['id'], '--token', job['token'],
            '--max-seconds', str(settings.max_video_seconds)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        while child.poll() is None:
            if (stop and stop.is_set()) or time.monotonic() - started > 1800:
                raise RuntimeError('后台处理已停止或超过30分钟，请重试')
            with engine.begin() as c:
                changed = c.execute(update(jobs).where(*owned_job(job, now(c))).values(lease_until=now(c) + LEASE))
            if not changed.rowcount:
                break
            (stop.wait(2) if stop else time.sleep(2))
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
        finish(engine, job, 'FAILED', f'处理进程退出（{child.returncode}），未提交完整结果，可重试')
    except Exception as exc:
        log.exception('Dataset supervisor failed')
        if child is not None and child.poll() is None:
            child.kill(); child.wait(timeout=5)
        finish(engine, job, 'FAILED', str(exc))
    return True


def poll(engine, settings, stop):
    maintenance, cursor = 0, ''
    while not stop.is_set():
        try:
            worked = execute_one(engine, settings, stop)
            if time.monotonic() >= maintenance:
                maintenance = time.monotonic() + 300
                with engine.connect() as c:
                    ids = list(c.execute(select(datasets.c.id).where(datasets.c.id > cursor).order_by(datasets.c.id).limit(10)).scalars())
                service = DatasetService(engine, settings)
                for did in ids:
                    cleanup_dataset(service, did, time.time() - 86400)
                cursor = ids[-1] if ids else ''
        except Exception:
            log.exception('Dataset queue unavailable')
            worked = False
        if not worked:
            stop.wait(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--max-seconds', type=int, default=600)
    args = parser.parse_args()
    import cv2
    cv2.setNumThreads(1)
    settings = replace(Settings.from_env(), max_video_seconds=args.max_seconds)
    engine = make_engine(settings.database_url)
    stopped = threading.Event()
    with engine.connect() as c:
        t = now(c)
        row = c.execute(select(jobs).where(jobs.c.id == args.job, jobs.c.token == args.token,
            jobs.c.state == 'RUNNING', jobs.c.lease_until >= t)).mappings().first()
    if row:
        def watch():
            deadline = time.monotonic() + max(0, row['lease_until'] - t)
            while not stopped.wait(2):
                try:
                    with engine.connect() as c:
                        current = now(c)
                        live = c.execute(select(jobs.c.state, jobs.c.lease_until).where(jobs.c.id == args.job, jobs.c.token == args.token)).first()
                    if not live or live.state != 'RUNNING' or live.lease_until < current:
                        os._exit(3)
                    deadline = time.monotonic() + live.lease_until - current
                except Exception:
                    if time.monotonic() > deadline:
                        os._exit(3)
        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        try:
            execute_job(engine, settings, dict(row))
        finally:
            stopped.set(); thread.join(timeout=3)
    engine.dispose()


if __name__ == '__main__':
    main()
