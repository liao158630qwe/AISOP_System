"""Durable alert archives: bounded JPEG prebuffer -> disk outbox -> MP4.

The video contains accepted inference samples, not an original camera stream.
No image bytes are stored in SQL. Publishing is fenced by a worker token;
failed/uncertain commits keep the immutable spool for reconciliation.
"""
from collections import OrderedDict, deque
from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .db import alert_evidence as alerts, now, serialized_write, station_inspections
from .pilot_config import STATIONS

log = logging.getLogger(__name__)
MIN_RETENTION = 30 * 86400
MAX_BUFFER_BYTES = 64 * 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024


def safe_path(root, relative):
    """Reject traversal and links, including intermediate Windows junctions."""
    root = Path(root).resolve()
    value = Path(relative)
    if value.is_absolute() or not value.parts or any(p in ('..', '.') for p in value.parts):
        raise ValueError('Evidence path is outside storage')
    path = root
    for part in value.parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Evidence path cannot follow links')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Evidence path is outside storage')
    return path


def video_path(root, row):
    """Validate the expected storage layout as well as containment."""
    value = row['video_path'] or ''
    parts = value.split('/')
    if row['source_kind'] == 'realtime':
        valid = (len(parts) == 7 and parts[0] == 'evidence' and
                 parts[4] == row['id'] and parts[5].isdigit() and parts[6] == 'video.mp4' and
                 all(part.isdigit() for part in parts[1:4]))
    else:
        valid = (row['source_kind'] == 'run' and len(parts) == 3 and parts[0] == 'runs' and
                 parts[1].startswith(row['source_id'] + '-') and parts[2] == 'video.webm')
    if not valid:
        raise ValueError('Evidence path does not match its archive identity')
    return safe_path(root, value)


def durable_write(path, data):
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def decision(result, final=False):
    verdict = result.get('compliance') or {}
    status = verdict.get('status')
    if status == 'FAIL' or (final and status == 'REVIEW'):
        return status, str(verdict.get('reason') or verdict.get('label') or status)[:1000]
    return None


def realtime_source_key(session, stream, kind):
    """Stable dedup namespace, also used to find archives written before stream IDs."""
    return hashlib.sha256(f'{session}:{stream}:{kind}'.encode()).hexdigest()


def compact_result(result):
    # Keep the frozen rule/model identity and verdict, not keypoint/frame arrays.
    return {k: result[k] for k in ('compliance', 'originalCompliance', 'evidenceGaps',
        'evidenceGapsTruncated', 'model', 'modelHash', 'configHash', 'sceneId', 'sceneRevision',
        'frameTime', 'final', 'streamId', 'stationId', 'cameraId', 'analysisType') if k in result}


def evidence_station():
    """Resolve frozen attribution, with an owner-scoped fallback for old streams.

    Unknown history stays unassigned; detector type is never a station identity.
    The indexed stream lookup also works before a round has finished archiving.
    """
    recorded = alerts.c.details['result']['stationId'].as_string()
    legacy = select(station_inspections.c.station_id).where(
        alerts.c.source_kind == 'realtime',
        station_inspections.c.stream_id == alerts.c.source_id,
        station_inspections.c.owner == alerts.c.owner).correlate(alerts).scalar_subquery()
    return func.coalesce(case((recorded.in_(STATIONS), recorded)),
                         case((legacy.in_(STATIONS), legacy))).label('station_id')


def offline_record(job, folder, result, retention):
    alarm = decision(result, final=True)
    if not alarm:
        # Generic scene results may put the final assessment on the last frame.
        frames = result.get('frames', [])
        alarm = decision(frames[-1], final=True) if frames else None
    if not alarm:
        return None
    path = folder / 'video.webm'
    t = time.time()
    return dict(id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'aisop:run:' + job['id'])),
        owner=job['owner'], source_kind='run', source_id=job['id'],
        analysis_type=job['options'].get('analysis_type', 'pose'), severity=alarm[0], reason=alarm[1],
        status='READY', video_path=f"runs/{folder.name}/video.webm", sha256=sha256(path),
        size_bytes=path.stat().st_size, format='webm', frame_count=result.get('summary', {}).get('analyzedFrames', 0),
        details={'schema_version': 1, 'capture_mode': 'offline-analysis', 'result': compact_result(result)},
        created_at=t, updated_at=t, ready_at=t, retain_until=t + max(MIN_RETENTION, retention),
        legal_hold=False, attempts=0, token=0)


class EvidenceStore:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.root = settings.data_dir.resolve()
        self.spool = safe_path(self.root, 'evidence-spool')
        self.archive = safe_path(self.root, 'evidence')
        self.spool.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.buffers = OrderedDict()
        self.buffer_bytes = 0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.last_error = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name='evidence-archiver', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=60)

    def _loop(self):
        last_cleanup, last_reconcile = 0, 0
        while not self.stop.is_set():
            try:
                if time.monotonic() - last_reconcile > 60:
                    self.reconcile(limit=100)
                    last_reconcile = time.monotonic()
                worked = self.process_one()
                if time.monotonic() - last_cleanup > self.settings.cleanup_interval_seconds:
                    expire_evidence(self.engine, self.root, retention=self.settings.evidence_retention_seconds)
                    last_cleanup = time.monotonic()
                if worked:
                    continue
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.exception('Evidence maintenance failed; retained spool will be retried')
            self.wake.wait(2)
            self.wake.clear()

    def _drop(self, key):
        state = self.buffers.pop(key, None)
        if state:
            self.buffer_bytes -= state['bytes']

    def release(self, session, stream):
        with self.lock:
            for key in list(self.buffers):
                if key[:2] == (session, stream):
                    self._drop(key)

    def observe(self, owner, session, stream, kind, timestamp, payload, result, *, final=False):
        """Called after inference, outside its GPU lock. Only alerts touch disk/SQL."""
        key = (session, stream, kind)
        alarm = decision(result, final)
        with self.lock:
            t = time.monotonic()
            for old in list(self.buffers):
                if t - self.buffers[old]['used'] > 300:
                    self._drop(old)
            if key not in self.buffers:
                while len(self.buffers) >= 32:
                    self._drop(next(iter(self.buffers)))
                self.buffers[key] = dict(frames=deque(), bytes=0, used=t, saved={})
            state = self.buffers[key]
            state['used'] = t
            self.buffers.move_to_end(key)
            if payload:
                entry = (float(timestamp), bytes(payload))
                state['frames'].append(entry)
                state['bytes'] += len(payload)
                self.buffer_bytes += len(payload)
                while state['frames'] and (len(state['frames']) > 128 or state['bytes'] > MAX_STREAM_BYTES or
                        timestamp - state['frames'][0][0] > 5):
                    _, removed = state['frames'].popleft()
                    state['bytes'] -= len(removed)
                    self.buffer_bytes -= len(removed)
                while self.buffer_bytes > MAX_BUFFER_BYTES and len(self.buffers) > 1:
                    self._drop(next(iter(self.buffers)))
            if not alarm:
                return None
            # One durable clip per severity per 30-second interval; finish is a
            # separate immutable assessment and repeat finish reuses its ID.
            marker = f'{alarm[0]}:final' if final else f'{alarm[0]}:{int(timestamp // 30)}'
            if marker in state['saved']:
                return state['saved'][marker]
            frames = list(state['frames'])
        source = realtime_source_key(session, stream, kind)
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f'aisop:alert:{owner}:{source}:{marker}'))
        try:
            with self.engine.connect() as c:
                existing = c.execute(select(alerts.c.id, alerts.c.status).where(alerts.c.id == identity)).first()
        except SQLAlchemyError:
            # Capture must still reach durable storage during a DB outage.
            # Reconciliation inserts the outbox record after connectivity returns.
            existing = None
        if existing:
            answer = {'id': identity, 'status': existing.status}
        elif not frames:
            t = time.time()
            row = self._row(identity, owner, stream, kind, alarm, t, 0,
                {'schema_version': 1, 'capture_mode': 'sampled-jpeg', 'result': compact_result(result)})
            row.update(status='MISSING', error='告警时没有可用采样，录像未保存')
            self._insert(row)
            answer = {'id': identity, 'status': 'MISSING'}
        else:
            destination = safe_path(self.spool, identity)
            if destination.exists():
                row = self._package(destination)['record']
            else:
                staging = safe_path(self.spool, identity + '.' + uuid.uuid4().hex + '.part')
                staging.mkdir()
                try:
                    samples = []
                    for i, (stamp, jpeg) in enumerate(frames):
                        name = f'{i:05d}.jpg'
                        durable_write(staging / name, jpeg)
                        samples.append({'time': stamp, 'file': name, 'sha256': hashlib.sha256(jpeg).hexdigest()})
                    t = time.time()
                    details = {'schema_version': 1, 'capture_mode': 'sampled-jpeg',
                        'sample_start': frames[0][0], 'sample_end': frames[-1][0],
                        'result': compact_result(result)}
                    row = self._row(identity, owner, stream, kind, alarm, t, len(frames), details)
                    raw = json.dumps({'record': row, 'samples': samples}, ensure_ascii=False,
                        separators=(',', ':'), allow_nan=False).encode('utf-8')
                    durable_write(staging / 'manifest.json.gz', gzip.compress(raw, mtime=0))
                    sync_directory(staging)
                    try:
                        staging.rename(destination)
                    except OSError:
                        if not destination.is_dir():
                            raise
                        shutil.rmtree(staging)
                    sync_directory(self.spool)
                except BaseException:
                    # A complete staging directory can be recovered manually;
                    # never advertise a successful save before atomic rename.
                    raise
            self._insert(row)
            answer = {'id': identity, 'status': 'PENDING'}
            self.wake.set()
        with self.lock:
            if key in self.buffers:
                # Bound dedup memory even for day-long streams.
                saved = self.buffers[key]['saved']
                saved[marker] = answer
                while len(saved) > 16:
                    saved.pop(next(iter(saved)))
        return answer

    @staticmethod
    def _row(identity, owner, source, kind, alarm, t, count, details):
        return dict(id=identity, owner=owner, source_kind='realtime', source_id=source,
            analysis_type=kind, severity=alarm[0], reason=alarm[1], status='PENDING', archive_key=identity,
            frame_count=count, details=details, created_at=t, updated_at=t, legal_hold=False, attempts=0, token=0)

    def _insert(self, row):
        try:
            with self.engine.begin() as c:
                c.execute(alerts.insert().values(**row))
        except IntegrityError:
            with self.engine.connect() as c:
                if not c.execute(select(alerts.c.id).where(alerts.c.id == row['id'])).first():
                    raise

    @staticmethod
    def _package(folder):
        with gzip.open(folder / 'manifest.json.gz', 'rt', encoding='utf-8') as handle:
            return json.load(handle)

    def reconcile(self, limit=100):
        # Directory iterator + batches bound memory; only outstanding spools
        # are examined (READY spools are removed after the SQL commit).
        paths = []
        with os.scandir(self.spool) as entries:
            for entry in entries:
                if entry.name.endswith('.part') or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    uuid.UUID(entry.name)
                except ValueError:
                    continue
                paths.append(safe_path(self.spool, entry.name))
                if len(paths) >= limit:
                    self._reconcile_batch(paths)
                    paths.clear()
            self._reconcile_batch(paths)

    def _reconcile_batch(self, paths):
        if not paths:
            return
        with self.engine.connect() as c:
            existing = dict(c.execute(select(alerts.c.id, alerts.c.status).where(alerts.c.id.in_([p.name for p in paths]))).all())
        for path in paths:
            try:
                if path.name not in existing:
                    self._insert(self._package(path)['record'])
                elif existing[path.name] in ('READY', 'EXPIRED'):
                    shutil.rmtree(path)
            except FileNotFoundError:
                pass  # another archiver completed this spool concurrently
            except (OSError, ValueError, EOFError):
                self.last_error = 'SPOOL_INVALID'
                log.exception('Invalid spool retained for inspection: %s', path.name)

    def process_one(self):
        with serialized_write(self.engine) as c:
            t = now(c)
            row = c.execute(select(alerts).where(or_(alerts.c.status == 'PENDING',
                and_(alerts.c.status == 'PROCESSING', alerts.c.lease_until < t)))
                .order_by(alerts.c.created_at, alerts.c.id).limit(1)
                .with_for_update(skip_locked=True)).mappings().first()
            if not row:
                return False
            row = dict(row)
            token = row['token'] + 1
            c.execute(update(alerts).where(alerts.c.id == row['id']).values(status='PROCESSING', token=token,
                lease_until=t + 120, attempts=row['attempts'] + 1, updated_at=t))
        folder = safe_path(self.spool, row['id'])
        day = datetime.fromtimestamp(row['created_at'], timezone.utc).strftime('%Y/%m/%d')
        relative = f'evidence/{day}/{row["id"]}/{token}'
        output = safe_path(self.root, relative)
        try:
            output.mkdir(parents=True, exist_ok=True)
            package = self._package(folder)
            self._encode(folder, output, package)
            video = output / 'video.mp4'
            checksum, size = sha256(video), video.stat().st_size
            with self.engine.begin() as c:
                t = now(c)
                saved = c.execute(update(alerts).where(alerts.c.id == row['id'], alerts.c.token == token,
                    alerts.c.status == 'PROCESSING', alerts.c.lease_until >= t).values(status='READY',
                    video_path=relative + '/video.mp4', sha256=checksum, size_bytes=size, format='mp4/h264',
                    ready_at=t, retain_until=t + max(MIN_RETENTION, self.settings.evidence_retention_seconds),
                    lease_until=None, error=None, updated_at=t)).rowcount
            if saved:
                shutil.rmtree(folder)
        except SQLAlchemyError:
            raise  # uncertain commit: leave both spool and archive for retry
        except Exception as exc:
            self.last_error = type(exc).__name__
            log.exception('Evidence encoding failed: %s', row['id'])
            with self.engine.begin() as c:
                # Transient encoding/IO failures get up to two delayed retries;
                # permanent failures remain visible and manually retryable.
                retry = row['attempts'] < 2
                t = now(c)
                c.execute(update(alerts).where(alerts.c.id == row['id'], alerts.c.token == token,
                    alerts.c.status == 'PROCESSING').values(status='PROCESSING' if retry else 'FAILED',
                    lease_until=t + 30 if retry else None,
                    error=('录像归档失败：' + type(exc).__name__)[:1000], updated_at=t))
        return True

    @staticmethod
    def _encode(folder, output, package):
        executable = os.getenv('FFMPEG_PATH') or shutil.which('ffmpeg')
        if not executable:
            import imageio_ffmpeg
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        samples = package['samples']
        if not samples:
            raise ValueError('Empty evidence')
        lines = ['ffconcat version 1.0']
        for i, sample in enumerate(samples):
            source = safe_path(folder, sample['file'])
            if sha256(source) != sample['sha256']:
                raise ValueError('Evidence checksum mismatch')
            # Fixed local names avoid concat quoting and injection issues.
            name = f'{i:05d}.jpg'
            shutil.copyfile(source, output / name)
            duration = samples[i+1]['time'] - sample['time'] if i+1 < len(samples) else .25
            if not math.isfinite(duration) or duration <= 0 or duration > 10:
                raise ValueError('Invalid sample interval')
            lines += [f"file '{name}'", f'duration {duration:.6f}']
        lines.append(f"file '{len(samples)-1:05d}.jpg'")
        (output / 'frames.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')
        command = [executable, '-nostdin', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '1',
            '-i', 'frames.txt', '-an', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p',
            '-fps_mode', 'vfr', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-threads', '1',
            '-movflags', '+faststart', '-f', 'mp4', 'video.part']
        subprocess.run(command, cwd=output, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        part = output / 'video.part'
        if part.stat().st_size == 0:
            raise ValueError('Empty video')
        # Use a writable descriptor for the durability barrier on Windows;
        # fsync on a read-only descriptor is rejected by the CRT.
        with part.open('r+b') as handle:
            os.fsync(handle.fileno())
        part.replace(output / 'video.mp4')
        durable_write(output / 'manifest.json.gz', gzip.compress(json.dumps(package, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode('utf-8'), mtime=0))
        sync_directory(output)
        for image in output.glob('*.jpg'):
            image.unlink()
        (output / 'frames.txt').unlink()

    def snapshot(self, owner=None):
        with self.lock:
            result = {'activeBuffers': len(self.buffers), 'bufferBytes': self.buffer_bytes,
                'maxBufferBytes': MAX_BUFFER_BYTES, 'captureMode': 'sampled-jpeg',
                'minimumRetentionDays': 30, 'retentionSeconds': self.settings.evidence_retention_seconds,
                'lastError': self.last_error}
        if owner is not None:
            with self.engine.connect() as c:
                rows = c.execute(select(alerts.c.status, func.count()).where(alerts.c.owner == owner)
                    .group_by(alerts.c.status)).all()
                oldest = c.execute(select(func.min(alerts.c.created_at)).where(alerts.c.owner == owner,
                    alerts.c.status.in_(('PENDING', 'PROCESSING')))).scalar_one()
            result.update(statusCounts=dict(rows), oldestPendingAgeSeconds=max(0, time.time()-oldest) if oldest else 0)
        result['diskFreeBytes'] = shutil.disk_usage(self.root).free
        return result


def expire_evidence(engine, root, *, retention=MIN_RETENTION, now_ts=None, batch_size=100, dry_run=False):
    """Tombstone -> delete files -> expire. Failed unlink stays visible for retry.

    No disk IO under SQL locks. Immutable retention/hold cannot be shortened by
    runtime configuration; increasing retention also protects existing rows.
    """
    if retention < MIN_RETENTION or not 1 <= batch_size <= 1000:
        raise ValueError('Evidence requires >=30 days and a bounded batch')
    root = Path(root).resolve()
    selected = []
    with serialized_write(engine) as c:
        t = now(c) if now_ts is None else float(now_ts)
        rows = c.execute(select(alerts).where(alerts.c.status.in_(('READY', 'DELETING')),
            alerts.c.legal_hold.is_(False), alerts.c.retain_until < t,
            alerts.c.ready_at + retention < t)
            .order_by(alerts.c.retain_until, alerts.c.id).limit(batch_size)
            .with_for_update(skip_locked=True)).mappings().all()
        for row in rows:
            if row['legal_hold']:
                continue
            if row['video_path']:
                video_path(root, row)
            selected.append(dict(row))
        if selected and not dry_run:
            c.execute(update(alerts).where(alerts.c.id.in_([r['id'] for r in selected])).values(status='DELETING', updated_at=t))
    result = {'eligible': len(selected), 'expired': 0, 'errors': 0}
    if dry_run:
        return result
    for row in selected:
        try:
            # Offline run files are shared with reports; the ordinary run
            # cleaner removes them only after this reference is expired.
            if row['source_kind'] == 'realtime' and row['video_path']:
                # All attempt directories share the same retention lifecycle.
                folder = video_path(root, row).parent.parent
                if folder.is_dir():
                    shutil.rmtree(folder)
            with engine.begin() as c:
                changed = c.execute(update(alerts).where(alerts.c.id == row['id'],
                    alerts.c.status == 'DELETING').values(status='EXPIRED', updated_at=t)).rowcount
            result['expired'] += changed
        except (OSError, ValueError):
            result['errors'] += 1
            log.exception('Evidence expiration failed: %s', row['id'])
    return result


def register(app, auth):
    store = app.state.evidence
    station = evidence_station()

    def owned(identity, owner):
        with store.engine.connect() as c:
            row = c.execute(select(alerts, station).where(alerts.c.id == str(identity), alerts.c.owner == owner)).mappings().first()
        if not row:
            raise HTTPException(404, '告警不存在')
        return row

    def public(row):
        fields = ('id', 'source_kind', 'source_id', 'analysis_type', 'severity', 'reason', 'status',
                  'sha256', 'size_bytes', 'format', 'frame_count', 'created_at', 'ready_at', 'retain_until', 'legal_hold', 'error')
        return {**{k: row[k] for k in fields}, 'station_id': row['station_id'],
            'station_name': STATIONS[row['station_id']]['name'] if row['station_id'] else '未关联工位',
            'video_url': f'/api/v1/alerts/{row["id"]}/video' if row['status'] == 'READY' else None}

    @app.get('/api/v1/alerts')
    def history(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=200),
                analysis_type: str | None = Query(None, pattern='^(pose|screw|sticker|scene)$'),
                station_id: str | None = Query(None, pattern='^(station-[1-6]|unassigned)$')):
        from .app import _decode_cursor, _encode_cursor
        conditions = [alerts.c.owner == auth(request)['owner']]
        if analysis_type:
            conditions.append(alerts.c.analysis_type == analysis_type)
        if station_id:
            conditions.append(station.is_(None) if station_id == 'unassigned' else station == station_id)
        if cursor:
            stamp, identity = _decode_cursor(cursor)
            conditions.append(or_(alerts.c.created_at < stamp, and_(alerts.c.created_at == stamp, alerts.c.id < identity)))
        columns = [column for column in alerts.c if column.name != 'details']
        with store.engine.connect() as c:
            rows = c.execute(select(*columns, station).where(*conditions).order_by(alerts.c.created_at.desc(), alerts.c.id.desc())
                .limit(limit+1)).mappings().all()
        return {'items': [public(r) for r in rows[:limit]],
            'next_cursor': _encode_cursor(rows[limit-1]['created_at'], rows[limit-1]['id']) if len(rows) > limit else None}

    @app.get('/api/v1/alerts/{identity}')
    def detail(identity: uuid.UUID, request: Request):
        row = owned(identity, auth(request)['owner'])
        return {**public(row), 'details': row['details']}

    @app.post('/api/v1/alerts/{identity}/retry')
    def retry(identity: uuid.UUID, request: Request):
        row = owned(identity, auth(request)['owner'])
        if row['status'] != 'FAILED':
            raise HTTPException(409, '只有归档失败的录像可以重试')
        with store.engine.begin() as c:
            c.execute(update(alerts).where(alerts.c.id == row['id'], alerts.c.status == 'FAILED')
                .values(status='PENDING', error=None, updated_at=now(c)))
        store.wake.set()
        return public(owned(identity, row['owner']))

    @app.api_route('/api/v1/alerts/{identity}/video', methods=['GET', 'HEAD'])
    def video(identity: uuid.UUID, request: Request):
        row = owned(identity, auth(request)['owner'])
        if row['status'] in ('EXPIRED', 'DELETING'):
            raise HTTPException(410, '录像已超过保留期并清理')
        if row['status'] != 'READY':
            raise HTTPException(409, row['error'] or '录像正在归档')
        path = video_path(store.root, row)
        if not path.is_file():
            raise HTTPException(410, '录像文件缺失，请检查存储')
        if store.settings.artifact_accel_redirect:
            prefix = '/_protected-runs/' if row['source_kind'] == 'run' else '/_protected-evidence/'
            relative = row['video_path'].split('/', 1)[1]
            return Response(headers={'X-Accel-Redirect': prefix + relative, 'Content-Disposition': 'inline'})
        return FileResponse(path, media_type='video/webm' if row['format'] == 'webm' else 'video/mp4')
