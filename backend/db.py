"""SQLAlchemy Core persistence. PostgreSQL in production; SQLite only in tests/dev."""
import hashlib
import json
import time
import uuid

from sqlalchemy import (Column, Float, Index, Integer, JSON, MetaData, String, Table,
                        UniqueConstraint, create_engine, select, update, text)

metadata = MetaData()
media = Table("media", metadata,
    Column("id", String(36), primary_key=True), Column("owner", String(100), nullable=False),
    Column("name", String(255), nullable=False), Column("suffix", String(10), nullable=False),
    Column("sha256", String(64), nullable=False), Column("size", Integer, nullable=False),
    Column("metadata", JSON, nullable=False), Column("created_at", Float, nullable=False))
runs = Table("analysis_runs", metadata,
    Column("id", String(36), primary_key=True), Column("owner", String(100), nullable=False),
    Column("media_id", String(36), nullable=False), Column("status", String(24), nullable=False, index=True),
    Column("request_key", String(100), nullable=False), Column("request_hash", String(64), nullable=False),
    Column("options", JSON, nullable=False), Column("model_hash", String(64), nullable=False),
    Column("progress", Float, nullable=False, default=0), Column("attempts", Integer, nullable=False, default=0),
    Column("token", Integer, nullable=False, default=0), Column("lease_until", Float),
    Column("error_code", String(80)), Column("error_message", String(500)),
    Column("result_dir", String(100)), Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("owner", "request_key", name="uq_run_request"))
run_metrics = Table("run_metrics", metadata,
    Column("run_id", String(36), primary_key=True), Column("queued_at", Float, nullable=False),
    Column("started_at", Float), Column("finished_at", Float), Column("queue_wait_seconds", Float),
    Column("processing_seconds", Float), Column("inference_p95_ms", Float),
    Column("peak_memory_mb", Float), Column("peak_gpu_memory_mb", Float),
    Column("analyzed_frames", Integer), Column("failure_code", String(80)))
sessions = Table("sessions", metadata,
    Column("token_hash", String(64), primary_key=True), Column("owner", String(100), nullable=False),
    Column("csrf", String(80), nullable=False), Column("expires_at", Float, nullable=False))
schema = Table("schema_version", metadata, Column("version", Integer, primary_key=True))

# Cover task history, queue polling and expired-session cleanup.
query_indexes = (
    Index("ix_runs_owner_created", runs.c.owner, runs.c.created_at, runs.c.id),
    Index("ix_runs_status_created", runs.c.status, runs.c.created_at),
    Index("ix_sessions_expires", sessions.c.expires_at),
)


def make_engine(url):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(url, **kwargs)


def initialize(engine):
    # Run explicitly before starting API/worker, never once per request.
    if engine.dialect.name == "sqlite":
        with engine.connect() as c:
            c.exec_driver_sql("PRAGMA journal_mode=WAL")
    metadata.create_all(engine)
    with engine.begin() as c:
        versions = c.execute(select(schema.c.version)).scalars().all()
        if not versions:
            c.execute(schema.insert().values(version=1))
        elif versions != [1]:
            raise RuntimeError("Unsupported database schema; run an explicit reviewed migration")
    # create_all does not add indexes to existing tables. This additive upgrade
    # is safe to repeat on v1 databases and never rewrites their records.
    for index in query_indexes:
        index.create(engine, checkfirst=True)


def now(c):
    if c.dialect.name == "postgresql":
        return float(c.execute(text("select extract(epoch from clock_timestamp())")).scalar_one())
    return time.time()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def request_hash(media_id, options):
    return digest(json.dumps([media_id, options], sort_keys=True, separators=(",", ":")))


def claim(engine, lease_seconds):
    with engine.begin() as c:
        t = now(c)
        stale = c.execute(select(runs).where(runs.c.status.in_(["RUNNING", "CANCELLING"]),
                                            runs.c.lease_until < t).with_for_update(skip_locked=True)).mappings().all()
        for row in stale:
            status = "CANCELLED" if row["status"] == "CANCELLING" else ("FAILED" if row["attempts"] >= 3 else "QUEUED")
            c.execute(update(runs).where(runs.c.id == row["id"], runs.c.token == row["token"],
                runs.c.lease_until < t).values(status=status, token=row["token"] + 1,
                lease_until=None, updated_at=t, error_code="WORKER_LOST" if status == "FAILED" else None,
                error_message="Worker租约多次过期" if status == "FAILED" else None))
        row = c.execute(select(runs).where(runs.c.status == "QUEUED")
                        .order_by(runs.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
        if not row:
            return None
        token = row["token"] + 1
        changed = c.execute(update(runs).where(runs.c.id == row["id"], runs.c.status == "QUEUED", runs.c.token == row["token"])
            .values(status="RUNNING", token=token, attempts=row["attempts"] + 1,
                    lease_until=t + lease_seconds, updated_at=t, progress=0))
        if not changed.rowcount:
            return None
        metric_exists = c.execute(select(run_metrics.c.run_id).where(run_metrics.c.run_id == row["id"])).first()
        if metric_exists:
            c.execute(update(run_metrics).where(run_metrics.c.run_id == row["id"]).values(
                started_at=t, finished_at=None, failure_code=None))
        else:
            c.execute(run_metrics.insert().values(run_id=row["id"], queued_at=row["created_at"], started_at=t))
        return dict(c.execute(select(runs).where(runs.c.id == row["id"])).mappings().one())


def heartbeat(engine, run_id, token, lease_seconds, progress=0):
    with engine.begin() as c:
        t = now(c)
        changed = c.execute(update(runs).where(runs.c.id == run_id, runs.c.token == token,
            runs.c.status == "RUNNING", runs.c.lease_until >= t)
            .values(lease_until=t + lease_seconds, updated_at=t, progress=min(.99, progress)))
        return changed.rowcount == 1


def finish(engine, run_id, token, status, result_dir=None, error=None, metrics=None):
    with engine.begin() as c:
        t = now(c)
        allowed = ["RUNNING", "CANCELLING"] if status == "CANCELLED" else ["RUNNING"]
        changed = c.execute(update(runs).where(runs.c.id == run_id, runs.c.token == token,
            runs.c.status.in_(allowed), runs.c.lease_until >= t).values(status=status,
            result_dir=result_dir, error_code=error[0] if error else None,
            error_message=error[1][:500] if error else None, lease_until=None, updated_at=t,
            progress=1 if status == "SUCCEEDED" else 0))
        if changed.rowcount == 1:
            values = {"finished_at": t, "failure_code": error[0] if error else None}
            if metrics:
                values.update({k: metrics[k] for k in (
                    "queue_wait_seconds", "processing_seconds", "inference_p95_ms",
                    "peak_memory_mb", "peak_gpu_memory_mb", "analyzed_frames") if k in metrics})
            c.execute(update(run_metrics).where(run_metrics.c.run_id == run_id).values(**values))
        return changed.rowcount == 1
