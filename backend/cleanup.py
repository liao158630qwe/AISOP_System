"""Retention cleanup for uploaded media and analysis artifacts.

The cleaner only removes terminal, unreferenced data past an explicit age. It
is safe to run from more than one worker because filesystem deletes are
idempotent and database rows are selected/deleted in one transaction.
"""
import shutil
import time
from pathlib import Path

from sqlalchemy import delete, select

from .db import media, run_metrics, runs


TERMINAL = ("CANCELLED", "FAILED", "SUCCEEDED")
ACTIVE = ("QUEUED", "RUNNING", "CANCELLING")


def _old(path, cutoff):
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return False


def _remove(path, dry_run):
    if dry_run or not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        else:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        # Another cleaner may have removed it between the existence check and
        # the delete. Treat that race as already clean.
        return False
    return True


def cleanup_data(engine, data_dir, *, run_retention_seconds=30 * 86400,
                 upload_retention_seconds=7 * 86400, orphan_retention_seconds=3600,
                 now_ts=None, dry_run=False):
    """Remove old terminal database records and their files.

    Returns counts and paths so the CLI can support both audit and execution.
    Active runs and media referenced by active runs are always retained.
    """
    root = Path(data_dir)
    now_ts = time.time() if now_ts is None else float(now_ts)
    run_cutoff, upload_cutoff = now_ts - run_retention_seconds, now_ts - upload_retention_seconds
    orphan_cutoff = now_ts - orphan_retention_seconds
    removed = {"runs": 0, "media": 0, "files": 0, "bytes": 0, "parts": 0}
    run_dirs, upload_files = root / "runs", root / "uploads"

    with engine.begin() as connection:
        rows = connection.execute(select(runs)).mappings().all()
        media_rows = connection.execute(select(media)).mappings().all()
        known_uploads = {row["id"] + row["suffix"] for row in media_rows}
        active_media = {row["media_id"] for row in rows if row["status"] in ACTIVE}
        old_runs = [row for row in rows if row["status"] in TERMINAL and row["updated_at"] < run_cutoff]
        old_run_ids = {row["id"] for row in old_runs}
        referenced_dirs = {row["result_dir"] for row in rows if row["result_dir"]}

        for row in old_runs:
            if row["result_dir"]:
                path = run_dirs / row["result_dir"]
                if _remove(path, dry_run):
                    removed["files"] += 1
            if not dry_run:
                connection.execute(delete(runs).where(runs.c.id == row["id"]))
                connection.execute(delete(run_metrics).where(run_metrics.c.run_id == row["id"]))
            removed["runs"] += 1

        remaining_runs = [row for row in rows if row["id"] not in old_run_ids]
        remaining_media = {row["id"] for row in media_rows
                           if any(run["media_id"] == row["id"] for run in remaining_runs)}
        for row in media_rows:
            if row["id"] in active_media or row["id"] in remaining_media:
                continue
            if row["created_at"] >= upload_cutoff:
                continue
            path = root / "uploads" / (row["id"] + row["suffix"])
            if path.exists():
                try:
                    removed["bytes"] += path.stat().st_size
                except FileNotFoundError:
                    pass
                if _remove(path, dry_run):
                    removed["files"] += 1
            if not dry_run:
                connection.execute(delete(media).where(media.c.id == row["id"]))
            removed["media"] += 1

        if not dry_run:
            # Remove any media row whose run was deleted and which is past the
            # upload retention window. The loop above handles normal rows; this
            # keeps concurrent cleanup runs from leaving stale metadata.
            connection.execute(delete(media).where(
                media.c.created_at < upload_cutoff,
                ~media.c.id.in_(select(runs.c.media_id))))

    for folder in (run_dirs, upload_files):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.name.endswith(".part"):
                if _old(path, orphan_cutoff):
                    if _remove(path, dry_run):
                        removed["parts"] += 1
                continue
            if folder == run_dirs and path.name in referenced_dirs:
                continue
            if folder == upload_files and path.name in known_uploads:
                continue
            # Unknown directories/files are only removed after a grace period.
            if _old(path, orphan_cutoff) and _remove(path, dry_run):
                removed["files"] += 1
    return removed
