"""SQLAlchemy Core persistence. PostgreSQL in production; SQLite only in tests/dev."""
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Float, Index, Integer, JSON, MetaData, String, Table,
                        UniqueConstraint, create_engine, event, func, literal, select, update, text)
from sqlalchemy.dialects.postgresql import JSONB

from .schema_constraints import attach_v2

metadata = MetaData()


def option_field(key):
    # Literalize only these fixed SQL identifiers: binding the JSON path as a
    # parameter prevents SQLite from matching the expression index at runtime.
    if key not in ('analysis_type', 'scene_id'):
        raise ValueError('Unsupported indexed option')
    return runs.c.options[literal(key, type_=JSON.JSONStrIndexType(), literal_execute=True)].as_string()

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
schema_migrations = Table('schema_migrations', metadata,
    Column('version', Integer, primary_key=True), Column('name', String(100), nullable=False),
    Column('applied_at', Float, nullable=False))
deployment_settings = Table('deployment_settings', metadata,
    Column('owner', String(100), primary_key=True), Column('revision', Integer, nullable=False),
    Column('config', JSON, nullable=False), Column('updated_at', Float, nullable=False))
station_terminals = Table('station_terminals', metadata,
    Column('station_id', String(30), primary_key=True), Column('owner', String(100), nullable=False),
    Column('workstation_id', String(60), nullable=False, unique=True),
    Column('camera_id', String(60), nullable=False, unique=True),
    Column('credential_hash', String(64), nullable=False, unique=True),
    Column('revision', Integer, nullable=False), Column('enabled', Boolean, nullable=False),
    Column('config', JSON, nullable=False), Column('updated_at', Float, nullable=False))
terminal_sessions = Table('terminal_sessions', metadata,
    Column('token_hash', String(64), primary_key=True), Column('station_id', String(30), nullable=False),
    Column('revision', Integer, nullable=False), Column('csrf', String(80), nullable=False),
    Column('expires_at', Float, nullable=False))
Index('ix_terminal_sessions_expires', terminal_sessions.c.expires_at)
station_stream_leases = Table('station_stream_leases', metadata,
    Column('station_id', String(30), primary_key=True), Column('stream_id', String(36), nullable=False),
    Column('epoch', Integer, nullable=False), Column('runtime_id', String(36), nullable=False),
    Column('request_id', String(36), nullable=False), Column('closed', Boolean, nullable=False),
    Column('context', JSON, nullable=False), Column('created_at', Float, nullable=False),
    Column('lease_until', Float, nullable=False))
station_inspections = Table('station_inspections', metadata,
    Column('stream_id', String(36), primary_key=True), Column('station_id', String(30), nullable=False),
    Column('owner', String(100), nullable=False), Column('cycle_id', String(36)),
    Column('context', JSON, nullable=False), Column('status', String(24), nullable=False),
    Column('result', JSON), Column('reason', String(1000), nullable=False, default=''),
    Column('started_at', Float, nullable=False), Column('finished_at', Float))
Index('ix_station_inspection_history', station_inspections.c.owner, station_inspections.c.station_id,
      station_inspections.c.started_at, station_inspections.c.stream_id)
Index('ix_station_inspection_cycle', station_inspections.c.cycle_id)
Index('ix_station_inspection_active', station_inspections.c.station_id, station_inspections.c.status)
Index('ix_inspections_owner_finished', station_inspections.c.owner, station_inspections.c.status,
      station_inspections.c.finished_at, station_inspections.c.stream_id)
Index('ix_inspections_archive_day', station_inspections.c.owner, station_inspections.c.status,
      func.coalesce(station_inspections.c.finished_at, station_inspections.c.started_at),
      station_inspections.c.station_id, station_inspections.c.stream_id)
station_inspection_deletions = Table('station_inspection_deletions', metadata,
    Column('stream_id', String(36), primary_key=True), Column('owner', String(100), nullable=False),
    Column('deleted_at', Float, nullable=False), Column('deleted_by', String(100), nullable=False))
Index('ix_inspection_deletions_owner', station_inspection_deletions.c.owner,
      station_inspection_deletions.c.deleted_at)

# The capture process owns the camera; web pages only submit durable commands.
station_collectors = Table('station_collectors', metadata,
    Column('station_id', String(30), primary_key=True), Column('owner', String(100), nullable=False),
    Column('instance_id', String(36), nullable=False), Column('binding_revision', Integer, nullable=False),
    Column('runtime_id', String(36), nullable=False), Column('heartbeat_at', Float, nullable=False),
    Column('expires_at', Float, nullable=False), Column('state', String(20), nullable=False),
    Column('stream_id', String(36)), Column('last_command_id', String(36)),
    Column('error', String(1000), nullable=False, default=''))
station_collector_commands = Table('station_collector_commands', metadata,
    Column('request_id', String(36), primary_key=True), Column('station_id', String(30), nullable=False),
    Column('owner', String(100), nullable=False), Column('instance_id', String(36), nullable=False),
    Column('binding_revision', Integer, nullable=False), Column('runtime_id', String(36), nullable=False),
    Column('action', String(12), nullable=False), Column('target_stream_id', String(36)),
    Column('stream_id', String(36)), Column('reason', String(1000), nullable=False),
    Column('actor', JSON, nullable=False), Column('request_hash', String(64), nullable=False),
    Column('status', String(20), nullable=False), Column('error', String(1000), nullable=False, default=''),
    Column('created_at', Float, nullable=False), Column('completed_at', Float),
    CheckConstraint("action IN ('start','finish','stop')", name='ck_collector_command_action'),
    CheckConstraint("status IN ('PENDING','SUCCEEDED','FAILED')", name='ck_collector_command_status'))
Index('ix_collector_commands_station', station_collector_commands.c.owner,
      station_collector_commands.c.station_id, station_collector_commands.c.created_at)


def inspection_visible():
    """Keep the original result and evidence while hiding a recycled round."""
    return ~select(station_inspection_deletions.c.stream_id).where(
        station_inspection_deletions.c.stream_id == station_inspections.c.stream_id,
        station_inspection_deletions.c.owner == station_inspections.c.owner).exists()


worker_heartbeats = Table('worker_heartbeats', metadata,
    Column('id', String(36), primary_key=True),
    Column('updated_at', Float, nullable=False),
    Column('expires_at', Float, nullable=False, index=True))

model_registry = Table('model_registry', metadata,
    Column('id', String(36), primary_key=True), Column('owner', String(100), nullable=False),
    Column('name', String(100), nullable=False), Column('version', String(60), nullable=False),
    Column('task', String(30), nullable=False), Column('classes', JSON, nullable=False),
    Column('path', String(255), nullable=False), Column('sha256', String(64), nullable=False),
    Column('size', Integer, nullable=False), Column('enabled', Boolean, nullable=False, default=True),
    Column('notes', String(1000), nullable=False, default=''), Column('created_at', Float, nullable=False))
scene_tasks = Table('scene_tasks', metadata,
    Column('id', String(36), primary_key=True), Column('owner', String(100), nullable=False),
    Column('name', String(100), nullable=False), Column('model_id', String(36), nullable=False),
    Column('status', String(20), nullable=False), Column('revision', Integer, nullable=False),
    Column('config', JSON, nullable=False), Column('created_at', Float, nullable=False),
    Column('updated_at', Float, nullable=False))

training_runs = Table('training_runs', metadata,
    Column('id', String(36), primary_key=True), Column('owner', String(100), nullable=False),
    Column('request_key', String(100), nullable=False), Column('request_hash', String(64), nullable=False),
    Column('name', String(100), nullable=False), Column('station', String(30), nullable=False),
    Column('stage', String(24), nullable=False), Column('recipe', JSON, nullable=False),
    Column('policy', JSON, nullable=False), Column('state', String(24), nullable=False),
    Column('progress', Float, nullable=False, default=0), Column('message', String(1000), nullable=False, default=''),
    Column('audit', JSON), Column('card', JSON), Column('evaluation', JSON),
    Column('model_id', String(36)), Column('created_at', Float, nullable=False), Column('updated_at', Float, nullable=False),
    UniqueConstraint('owner', 'request_key', name='uq_training_request'))
training_jobs = Table('training_jobs', metadata,
    Column('id', String(36), primary_key=True), Column('run_id', String(36), nullable=False),
    Column('owner', String(100), nullable=False), Column('request_key', String(100), nullable=False),
    Column('kind', String(20), nullable=False), Column('status', String(24), nullable=False),
    Column('active_run', String(36), unique=True), Column('active_slot', String(30), unique=True),
    Column('token', String(36)), Column('lease_until', Float), Column('progress', Float, nullable=False),
    Column('message', String(1000), nullable=False), Column('result', JSON),
    Column('created_at', Float, nullable=False), Column('updated_at', Float, nullable=False),
    UniqueConstraint('owner', 'request_key', name='uq_training_job_request'))
Index('ix_training_jobs_queue', training_jobs.c.status, training_jobs.c.created_at)
Index('ix_training_owner', training_runs.c.owner, training_runs.c.created_at)

training_datasets = Table('training_datasets', metadata,
    Column('id', String(36), primary_key=True), Column('owner', String(100), nullable=False),
    Column('name', String(100), nullable=False), Column('station', String(30), nullable=False),
    Column('task', String(24), nullable=False), Column('classes', JSON, nullable=False),
    Column('revision', Integer, nullable=False), Column('active_export', String(36)),
    Column('created_at', Float, nullable=False), Column('updated_at', Float, nullable=False))
dataset_videos = Table('dataset_videos', metadata,
    Column('id', String(36), primary_key=True), Column('dataset_id', String(36), nullable=False),
    Column('name', String(200), nullable=False), Column('path', String(255), nullable=False),
    Column('sha256', String(64), nullable=False), Column('size', BigInteger, nullable=False),
    Column('capture', JSON, nullable=False), Column('metadata', JSON),
    Column('frame_budget', Integer, nullable=False), Column('revision', Integer, nullable=False),
    Column('request_hash', String(64), nullable=False), Column('job_id', String(36), nullable=False),
    Column('created_at', Float, nullable=False),
    UniqueConstraint('dataset_id', 'sha256', name='uq_dataset_video_content'))
dataset_frames = Table('dataset_frames', metadata,
    Column('id', String(36), primary_key=True), Column('dataset_id', String(36), nullable=False),
    Column('video_id', String(36), nullable=False), Column('ordinal', Integer, nullable=False),
    Column('timestamp', Float, nullable=False), Column('path', String(255), nullable=False),
    Column('sha256', String(64), nullable=False), Column('width', Integer, nullable=False),
    Column('height', Integer, nullable=False), Column('review', String(20), nullable=False),
    Column('annotation', JSON, nullable=False), Column('revision', Integer, nullable=False))
dataset_jobs = Table('dataset_jobs', metadata,
    Column('id', String(36), primary_key=True), Column('dataset_id', String(36), nullable=False),
    Column('owner', String(100), nullable=False), Column('kind', String(20), nullable=False),
    Column('state', String(20), nullable=False), Column('payload', JSON, nullable=False),
    Column('token', String(36)), Column('lease_until', Float), Column('active_slot', String(30), unique=True),
    Column('progress', Float, nullable=False), Column('message', String(1000), nullable=False),
    Column('result', JSON), Column('created_at', Float, nullable=False), Column('updated_at', Float, nullable=False))
Index('ix_datasets_owner_created', training_datasets.c.owner, training_datasets.c.created_at)
Index('ix_dataset_videos_dataset', dataset_videos.c.dataset_id, dataset_videos.c.created_at)
Index('ix_dataset_frames_order', dataset_frames.c.dataset_id, dataset_frames.c.video_id, dataset_frames.c.ordinal)
Index('ix_dataset_frames_review', dataset_frames.c.dataset_id, dataset_frames.c.review)
Index('ix_dataset_jobs_queue', dataset_jobs.c.state, dataset_jobs.c.created_at)
Index('ix_dataset_jobs_lease', dataset_jobs.c.state, dataset_jobs.c.lease_until)
Index('ix_dataset_jobs_dataset', dataset_jobs.c.dataset_id, dataset_jobs.c.created_at)

# The pilot is independent of inference jobs; completed records are append-only.
pilot_cycles = Table('pilot_cycles', metadata,
    Column('id', String(36), primary_key=True), Column('owner', String(100), nullable=False),
    Column('station', String(30), nullable=False),
    Column('active_station', String(30), unique=True),
    Column('part_number', String(100), nullable=False), Column('product', String(100), nullable=False),
    Column('source', String(20), nullable=False), Column('parent_id', String(36)),
    Column('stage', String(24), nullable=False), Column('result', String(24)),
    Column('review_status', String(24)),
    Column('revision', Integer, nullable=False), Column('config', JSON, nullable=False),
    Column('checks', JSON, nullable=False), Column('reason', String(1000), nullable=False),
    Column('started_at', Float, nullable=False), Column('updated_at', Float, nullable=False),
    Column('finished_at', Float))
pilot_events = Table('pilot_events', metadata,
    Column('id', String(36), primary_key=True), Column('cycle_id', String(36), nullable=False),
    Column('owner', String(100), nullable=False), Column('request_key', String(36), nullable=False),
    Column('request_hash', String(64), nullable=False), Column('kind', String(30), nullable=False),
    Column('revision', Integer, nullable=False), Column('at', Float, nullable=False),
    Column('payload', JSON, nullable=False), Column('response', JSON, nullable=False),
    UniqueConstraint('owner', 'request_key', name='uq_pilot_request'),
    UniqueConstraint('cycle_id', 'revision', name='uq_pilot_revision'))
Index('ix_pilot_history', pilot_cycles.c.owner, pilot_cycles.c.started_at, pilot_cycles.c.id)
Index('ix_pilot_events', pilot_events.c.cycle_id, pilot_events.c.revision)
Index('ix_pilot_owner_source_started', pilot_cycles.c.owner, pilot_cycles.c.source, pilot_cycles.c.started_at)
Index('ix_pilot_owner_source_finished', pilot_cycles.c.owner, pilot_cycles.c.source, pilot_cycles.c.finished_at)
Index('ix_pilot_part_latest', pilot_cycles.c.owner, pilot_cycles.c.source, pilot_cycles.c.product,
      pilot_cycles.c.part_number, pilot_cycles.c.station, pilot_cycles.c.started_at, pilot_cycles.c.id)
Index('ix_pilot_active_owner', pilot_cycles.c.owner, pilot_cycles.c.active_station)
Index('ix_pilot_event_owner_at', pilot_events.c.owner, pilot_events.c.at, pilot_events.c.id)

# Immutable media stays on the volume; only searchable facts and compact audit
# metadata belong in the database. JSONB is used for this new PostgreSQL table.
alert_evidence = Table('alert_evidence', metadata,
    Column('id', String(36), primary_key=True), Column('owner', String(100), nullable=False),
    Column('source_kind', String(20), nullable=False), Column('source_id', String(100), nullable=False),
    Column('analysis_type', String(20), nullable=False), Column('severity', String(20), nullable=False),
    Column('reason', String(1000), nullable=False), Column('status', String(20), nullable=False),
    Column('archive_key', String(36)), Column('video_path', String(255)),
    Column('sha256', String(64)), Column('size_bytes', BigInteger),
    Column('format', String(32)), Column('frame_count', Integer, nullable=False, default=0),
    Column('details', JSON().with_variant(JSONB, 'postgresql'), nullable=False),
    Column('created_at', Float, nullable=False), Column('updated_at', Float, nullable=False),
    Column('ready_at', Float), Column('retain_until', Float),
    Column('legal_hold', Boolean, nullable=False, default=False),
    Column('attempts', Integer, nullable=False, default=0), Column('token', Integer, nullable=False, default=0),
    Column('lease_until', Float), Column('error', String(1000)),
    CheckConstraint('ready_at IS NULL OR retain_until >= ready_at + 2592000', name='ck_evidence_30_days'),
    CheckConstraint("status != 'READY' OR (ready_at IS NOT NULL AND retain_until IS NOT NULL AND "
                    "video_path IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL AND size_bytes > 0)", name='ck_evidence_ready'),
    CheckConstraint("status IN ('PENDING','PROCESSING','READY','FAILED','MISSING','DELETING','EXPIRED')", name='ck_evidence_status'))
Index('ix_alert_owner_created', alert_evidence.c.owner, alert_evidence.c.created_at, alert_evidence.c.id)
Index('ix_alert_owner_type_created', alert_evidence.c.owner, alert_evidence.c.analysis_type,
      alert_evidence.c.created_at, alert_evidence.c.id)
Index('ix_alert_queue', alert_evidence.c.status, alert_evidence.c.created_at, alert_evidence.c.id)
Index('ix_alert_lease', alert_evidence.c.status, alert_evidence.c.lease_until)
Index('ix_alert_retention', alert_evidence.c.status, alert_evidence.c.legal_hold, alert_evidence.c.retain_until)
Index('ix_alert_source', alert_evidence.c.source_kind, alert_evidence.c.source_id, alert_evidence.c.status)
Index('ix_alert_archive', alert_evidence.c.archive_key)

# Cover task history, queue polling and expired-session cleanup.
query_indexes = (
    Index("ix_runs_owner_created", runs.c.owner, runs.c.created_at, runs.c.id),
    Index("ix_runs_status_created", runs.c.status, runs.c.created_at),
    Index("ix_runs_media", runs.c.media_id),
    Index("ix_runs_retention", runs.c.status, runs.c.updated_at, runs.c.id),
    Index("ix_media_created", media.c.created_at, media.c.id),
    Index("ix_sessions_expires", sessions.c.expires_at),
    Index('ix_runs_owner_status_created', runs.c.owner, runs.c.status, runs.c.created_at, runs.c.id),
    Index('ix_runs_status_lease', runs.c.status, runs.c.lease_until),
    Index('ix_runs_result_dir', runs.c.result_dir),
    Index('ix_runs_owner_type_created', runs.c.owner, runs.c.options['analysis_type'].as_string(), runs.c.created_at, runs.c.id),
    Index('ix_runs_owner_scene_created', runs.c.owner, runs.c.options['scene_id'].as_string(), runs.c.created_at, runs.c.id),
    Index('ix_metrics_finished', run_metrics.c.finished_at, run_metrics.c.run_id),
    Index('ix_models_owner_created', model_registry.c.owner, model_registry.c.created_at),
    Index('ix_scenes_owner_updated', scene_tasks.c.owner, scene_tasks.c.updated_at),
    Index('ix_training_owner_created_id', training_runs.c.owner, training_runs.c.created_at, training_runs.c.id),
    Index('ix_training_owner_state_created', training_runs.c.owner, training_runs.c.state, training_runs.c.created_at),
    Index('ix_training_owner_station_created', training_runs.c.owner, training_runs.c.station, training_runs.c.created_at),
    Index('ix_training_jobs_lease', training_jobs.c.status, training_jobs.c.lease_until),
)

v2_constraints = attach_v2(metadata)
# Index child-side references used by FK checks/deletes and job lookup.
Index('ix_training_jobs_run', training_jobs.c.run_id)
Index('ix_training_model', training_runs.c.model_id)
Index('ix_scenes_model', scene_tasks.c.model_id)
Index('ix_terminal_sessions_station', terminal_sessions.c.station_id)


def make_engine(url):
    def integer(name, default, minimum=1, maximum=100000):
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError as exc:
            raise ValueError(f'{name} must be an integer') from exc
        if not minimum <= value <= maximum:
            raise ValueError(f'{name} must be between {minimum} and {maximum}')
        return value
    kwargs = {"pool_pre_ping": True,
              'json_serializer': lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(pool_size=integer('DB_POOL_SIZE', 5, maximum=64),
                      max_overflow=integer('DB_MAX_OVERFLOW', 5, minimum=0, maximum=64),
                      pool_timeout=integer('DB_POOL_TIMEOUT_SECONDS', 5, maximum=60),
                      pool_recycle=integer('DB_POOL_RECYCLE_SECONDS', 1800), pool_use_lifo=True)
        if url.startswith('postgresql+psycopg:'):
            kwargs['connect_args'] = {'connect_timeout': 10}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def configure_sqlite(connection, _):
            cursor = connection.cursor()
            for pragma in ('busy_timeout=30000', 'synchronous=FULL', 'foreign_keys=ON', 'cache_size=-8192'):
                cursor.execute('PRAGMA ' + pragma)
            cursor.close()
    return engine


@contextmanager
def serialized_write(engine):
    """Lock before reading on SQLite, which has no SELECT FOR UPDATE support.

    PostgreSQL callers still lock the individual rows they inspect. Keep this
    transaction short; model loading and file IO belong outside it.
    """
    with engine.connect() as connection:
        if engine.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            connection.begin()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def initialize(engine):
    # Explicit deployment step, never an implicit per-request migration.
    from .migrations import upgrade
    return upgrade(engine)


def now(c):
    if c.dialect.name == "postgresql":
        return float(c.execute(text("select extract(epoch from clock_timestamp())")).scalar_one())
    return time.time()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def request_hash(media_id, options):
    return digest(json.dumps([media_id, options], sort_keys=True, separators=(",", ":")))


def claim(engine, lease_seconds):
    with serialized_write(engine) as c:
        t = now(c)
        stale = c.execute(select(runs).where(runs.c.status.in_(["RUNNING", "CANCELLING"]),
                                            runs.c.lease_until < t).order_by(runs.c.lease_until)
                          .limit(100).with_for_update(skip_locked=True)).mappings().all()
        for row in stale:
            status = "CANCELLED" if row["status"] == "CANCELLING" else ("FAILED" if row["attempts"] >= 3 else "QUEUED")
            changed = c.execute(update(runs).where(runs.c.id == row["id"], runs.c.token == row["token"],
                runs.c.status == row["status"],
                runs.c.lease_until < t).values(status=status, token=row["token"] + 1,
                lease_until=None, updated_at=t, error_code="WORKER_LOST" if status == "FAILED" else None,
                error_message="Worker租约多次过期" if status == "FAILED" else None))
            if changed.rowcount and status in ('FAILED', 'CANCELLED'):
                c.execute(update(run_metrics).where(run_metrics.c.run_id == row['id']).values(
                    finished_at=t, failure_code='WORKER_LOST' if status == 'FAILED' else None))
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


def finish(engine, run_id, token, status, result_dir=None, error=None, metrics=None, evidence=None):
    with engine.begin() as c:
        t = now(c)
        allowed = ["RUNNING", "CANCELLING"] if status == "CANCELLED" else ["RUNNING"]
        changed = c.execute(update(runs).where(runs.c.id == run_id, runs.c.token == token,
            runs.c.status.in_(allowed), runs.c.lease_until >= t).values(status=status,
            result_dir=result_dir, error_code=error[0] if error else None,
            error_message=error[1][:500] if error else None, lease_until=None, updated_at=t,
            progress=1 if status == "SUCCEEDED" else 0))
        if changed.rowcount == 1:
            if evidence:
                # The same database clock and transaction publish the run and
                # its retention reference; worker clock skew cannot shorten it.
                retention = max(2592000, evidence['retain_until'] - evidence['ready_at'])
                c.execute(alert_evidence.insert().values(**dict(evidence, created_at=t, updated_at=t,
                    ready_at=t, retain_until=t + retention)))
            values = {"finished_at": t, "failure_code": error[0] if error else None}
            if metrics:
                values.update({k: metrics[k] for k in (
                    "queue_wait_seconds", "processing_seconds", "inference_p95_ms",
                    "peak_memory_mb", "peak_gpu_memory_mb", "analyzed_frames") if k in metrics})
            c.execute(update(run_metrics).where(run_metrics.c.run_id == run_id).values(**values))
        return changed.rowcount == 1
