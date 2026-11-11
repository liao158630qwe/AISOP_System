"""Retention cleanup: commit metadata before removing unreferenced files."""
import logging
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import delete, select

from .db import alert_evidence, media, run_metrics, runs, serialized_write, sessions

log = logging.getLogger(__name__)
TERMINAL = ('CANCELLED', 'FAILED', 'SUCCEEDED')
ACTIVE = ('QUEUED', 'RUNNING', 'CANCELLING')


def _old(path, cutoff):
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return False


def _child(folder, name):
    path = folder / name
    if not name or Path(name).name != name or path.resolve().parent != folder.resolve():
        raise ValueError('Cleanup path is outside its storage directory')
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Cleanup does not follow links')
    return path


def _remove(path, dry_run):
    if dry_run or not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        return False
    return True


def cleanup_data(engine, data_dir, *, run_retention_seconds=30 * 86400,
                 upload_retention_seconds=7 * 86400, orphan_retention_seconds=3600,
                 now_ts=None, dry_run=False, batch_size=500):
    """Expire a bounded batch; failed file removals are retried as orphans.

    Disk IO is outside transactions so a slow disk cannot block worker leases.
    Active attempt directories are retained even before result_dir is published.
    Manual station evidence and its append-only audit log are never swept here.
    """
    if min(run_retention_seconds, upload_retention_seconds, orphan_retention_seconds) <= 0 or not 1 <= batch_size <= 1000:
        raise ValueError('Retention intervals and batch size must be positive')
    root = Path(data_dir).resolve()
    run_dirs, upload_files = root / 'runs', root / 'uploads'
    for folder in (run_dirs, upload_files):
        if folder.resolve() != folder or folder.is_symlink():
            raise ValueError('Storage directory must not redirect outside DATA_DIR')
    now_ts = time.time() if now_ts is None else float(now_ts)
    run_cutoff = now_ts - run_retention_seconds
    upload_cutoff = now_ts - upload_retention_seconds
    orphan_cutoff = now_ts - orphan_retention_seconds
    removed = dict(runs=0, media=0, files=0, bytes=0, parts=0, sessions=0, errors=0)
    paths = []
    # Commit first: rollback must never leave surviving records with lost files.
    # A crash after commit leaves safe orphans for the next sweep.
    with serialized_write(engine) as c:
        old_runs = c.execute(select(runs.c.id, runs.c.result_dir).where(
            runs.c.status.in_(TERMINAL), runs.c.updated_at < run_cutoff,
            ~select(alert_evidence.c.id).where(alert_evidence.c.source_kind == 'run',
                alert_evidence.c.source_id == runs.c.id, alert_evidence.c.status != 'EXPIRED').exists())
            .order_by(runs.c.updated_at, runs.c.id).limit(batch_size)
            .with_for_update(skip_locked=True)).mappings().all()
        for row in old_runs:
            if row['result_dir']:
                paths.append((_child(run_dirs, row['result_dir']), False))
        if old_runs and not dry_run:
            old_ids = [r['id'] for r in old_runs]
            c.execute(delete(run_metrics).where(run_metrics.c.run_id.in_(old_ids)))
            c.execute(delete(runs).where(runs.c.id.in_(old_ids)))
        removed['runs'] = len(old_runs)
        references = select(runs.c.media_id)
        if dry_run and old_runs:
            references = references.where(~runs.c.id.in_([row['id'] for row in old_runs]))
        old_media = c.execute(select(media.c.id, media.c.suffix).where(
            media.c.created_at < upload_cutoff, ~media.c.id.in_(references))
            .order_by(media.c.created_at, media.c.id).limit(batch_size)
            .with_for_update(skip_locked=True)).mappings().all()
        # One fresh PostgreSQL snapshot after acquiring media row locks avoids
        # the old per-media recheck/delete round trips.
        protected_media = set(c.execute(select(runs.c.media_id).where(
            runs.c.media_id.in_([r['id'] for r in old_media]),
            ~runs.c.id.in_([r['id'] for r in old_runs]) if dry_run else True)).scalars()) if old_media else set()
        expired_media = []
        for row in old_media:
            if row['id'] in protected_media:
                continue
            paths.append((_child(upload_files, row['id'] + row['suffix']), True))
            expired_media.append(row['id'])
            removed['media'] += 1
        if expired_media and not dry_run:
            c.execute(delete(media).where(media.c.id.in_(expired_media)))
        expired = c.execute(select(sessions.c.token_hash).where(sessions.c.expires_at < now_ts)
            .order_by(sessions.c.expires_at).limit(batch_size)).scalars().all()
        removed['sessions'] = len(expired)
        if expired and not dry_run:
            c.execute(delete(sessions).where(sessions.c.token_hash.in_(expired), sessions.c.expires_at < now_ts))

    visited = {path for path, _upload in paths}

    def remove(path, upload=False):
        try:
            size = path.stat().st_size if upload and path.is_file() else 0
            if _remove(path, dry_run):
                removed['parts' if path.name.endswith('.part') else 'files'] += 1
                removed['bytes'] += size
        except OSError:
            removed['errors'] += 1
            log.exception('Retention file removal failed: %s', path.name)

    for path, upload in paths:
        remove(path, upload)
    # Stream directory entries in chunks and query only their identities.
    # Memory and SQL bind counts are independent of total history size.
    def chunks(folder):
        batch = []
        with os.scandir(folder) as entries:
            for entry in entries:
                batch.append(Path(entry.path))
                if len(batch) >= min(batch_size, 400):
                    yield batch
                    batch = []
            if batch:
                yield batch

    for folder in (run_dirs, upload_files):
        if not folder.is_dir():
            continue
        for batch in chunks(folder):
            with engine.connect() as c:
                if folder == run_dirs:
                    names = [p.name for p in batch]
                    ids = [p.name.rpartition('-')[0] for p in batch]
                    known_results = set(c.execute(select(runs.c.result_dir).where(runs.c.result_dir.in_(names))).scalars())
                    active_ids = set(c.execute(select(runs.c.id).where(runs.c.id.in_(ids), runs.c.status.in_(ACTIVE))).scalars())
                    known_media = set()
                else:
                    known_results, active_ids = set(), set()
                    known_media = set(c.execute(select(media.c.id).where(media.c.id.in_([p.stem for p in batch]))).scalars())
            for candidate in batch:
                sweep_candidate(candidate, folder, known_results, active_ids, known_media, visited,
                    orphan_cutoff, engine, remove, removed, run_dirs, upload_files)
    return removed


def sweep_candidate(candidate, folder, known_results, active_ids, known_media, visited,
                    orphan_cutoff, engine, remove, removed, run_dirs, upload_files):
    if candidate in visited:
        return
    try:
        path = _child(folder, candidate.name)
        if not _old(path, orphan_cutoff):
            return
        identity, _, _token = path.name.rpartition('-')
        if folder == run_dirs and (path.name in known_results or identity in active_ids):
            return
        if folder == upload_files and path.stem in known_media:
            return
        # Fresh check protects outputs created during the sweep. An unfinished
        # worker has no result_dir but owns id-token folders.
        with engine.connect() as c:
            if folder == run_dirs:
                protected = c.execute(select(runs.c.id).where(
                    (runs.c.result_dir == path.name) |
                    ((runs.c.id == identity) & runs.c.status.in_(ACTIVE))
                ).limit(1)).first()
            else:
                protected = c.execute(select(media.c.id).where(media.c.id == path.stem).limit(1)).first()
        if not protected:
            remove(path, folder == upload_files)
    except (OSError, ValueError):
        removed['errors'] += 1
        log.exception('Retention candidate skipped: %s', candidate.name)
