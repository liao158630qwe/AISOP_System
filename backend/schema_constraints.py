"""Frozen v2 integrity rules shared by metadata, preflight and migration.

Do not edit shipped rules in place. Add a new numbered migration for changes.
Historical snapshots and polymorphic evidence references intentionally have no FK.
"""
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint


UNIQUES = (
    ('media', 'uq_media_id_owner', ('id', 'owner')),
    ('model_registry', 'uq_models_id_owner', ('id', 'owner')),
    ('training_runs', 'uq_training_id_owner', ('id', 'owner')),
    ('training_datasets', 'uq_datasets_id_owner', ('id', 'owner')),
    ('dataset_videos', 'uq_videos_id_dataset', ('id', 'dataset_id')),
    ('dataset_frames', 'uq_frames_video_ordinal', ('video_id', 'ordinal')),
    ('pilot_cycles', 'uq_cycles_id_owner', ('id', 'owner')),
    ('station_inspections', 'uq_inspections_id_owner', ('stream_id', 'owner')),
)

FOREIGN_KEYS = (
    ('analysis_runs', 'fk_runs_media_owner', ('media_id', 'owner'), 'media', ('id', 'owner'), 'RESTRICT'),
    ('run_metrics', 'fk_metrics_run', ('run_id',), 'analysis_runs', ('id',), 'CASCADE'),
    ('scene_tasks', 'fk_scenes_model_owner', ('model_id', 'owner'), 'model_registry', ('id', 'owner'), 'RESTRICT'),
    ('training_runs', 'fk_training_model_owner', ('model_id', 'owner'), 'model_registry', ('id', 'owner'), 'RESTRICT'),
    ('training_jobs', 'fk_training_jobs_run_owner', ('run_id', 'owner'), 'training_runs', ('id', 'owner'), 'RESTRICT'),
    ('dataset_videos', 'fk_videos_dataset', ('dataset_id',), 'training_datasets', ('id',), 'RESTRICT'),
    ('dataset_frames', 'fk_frames_video_dataset', ('video_id', 'dataset_id'), 'dataset_videos', ('id', 'dataset_id'), 'CASCADE'),
    ('dataset_jobs', 'fk_dataset_jobs_owner', ('dataset_id', 'owner'), 'training_datasets', ('id', 'owner'), 'RESTRICT'),
    ('pilot_events', 'fk_events_cycle_owner', ('cycle_id', 'owner'), 'pilot_cycles', ('id', 'owner'), 'RESTRICT'),
    ('terminal_sessions', 'fk_terminal_sessions_station', ('station_id',), 'station_terminals', ('station_id',), 'CASCADE'),
    ('station_inspection_deletions', 'fk_deletions_inspection_owner', ('stream_id', 'owner'), 'station_inspections', ('stream_id', 'owner'), 'RESTRICT'),
)

CHECKS = (
    ('media', 'ck_media_size', 'size >= 0'),
    ('model_registry', 'ck_models_size', 'size >= 0'),
    ('analysis_runs', 'ck_runs_status', "status IN ('QUEUED','RUNNING','CANCELLING','CANCELLED','FAILED','SUCCEEDED')"),
    ('analysis_runs', 'ck_runs_progress', 'progress >= 0 AND progress <= 1'),
    ('analysis_runs', 'ck_runs_counters', 'attempts >= 0 AND token >= 0'),
    ('deployment_settings', 'ck_settings_revision', 'revision > 0'),
    ('station_terminals', 'ck_terminals_revision', 'revision > 0'),
    ('terminal_sessions', 'ck_terminal_sessions_revision', 'revision >= 0'),
    ('scene_tasks', 'ck_scenes_revision', 'revision > 0'),
    ('training_runs', 'ck_training_state', "state IN ('QUEUED','RUNNING','CANCELLING','CANCELLED','FAILED','REJECTED','CANDIDATE')"),
    ('training_runs', 'ck_training_progress', 'progress >= 0 AND progress <= 1'),
    ('training_jobs', 'ck_training_jobs_status', "status IN ('QUEUED','RUNNING','CANCELLING','CANCELLED','FAILED','REJECTED','CANDIDATE')"),
    ('training_jobs', 'ck_training_jobs_progress', 'progress >= 0 AND progress <= 1'),
    ('training_datasets', 'ck_datasets_revision', 'revision > 0'),
    ('dataset_videos', 'ck_videos_values', 'size >= 0 AND frame_budget > 0 AND revision > 0'),
    ('dataset_frames', 'ck_frames_values', 'ordinal >= 0 AND timestamp >= 0 AND width > 0 AND height > 0 AND revision > 0'),
    ('dataset_jobs', 'ck_dataset_jobs_state', "state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')"),
    ('dataset_jobs', 'ck_dataset_jobs_progress', 'progress >= 0 AND progress <= 1'),
    ('pilot_cycles', 'ck_cycles_revision', 'revision > 0'),
    ('pilot_events', 'ck_events_revision', 'revision > 0'),
    ('station_inspections', 'ck_inspections_status', "status IN ('RUNNING','FINISHED','INTERRUPTED')"),
)


def attach_v2(metadata):
    """Keep fresh databases and upgraded databases on the same constraints."""
    constraints = []
    for table, name, columns in UNIQUES:
        constraints.append((table, UniqueConstraint(*columns, name=name)))
    for table, name, columns, parent, targets, ondelete in FOREIGN_KEYS:
        constraints.append((table, ForeignKeyConstraint(columns, [f'{parent}.{col}' for col in targets],
                                                       name=name, ondelete=ondelete)))
    for table, name, expression in CHECKS:
        constraints.append((table, CheckConstraint(expression, name=name)))
    for table, constraint in constraints:
        constraint.info['migration'] = 2
        metadata.tables[table].append_constraint(constraint)
    return tuple(constraint for _, constraint in constraints)
