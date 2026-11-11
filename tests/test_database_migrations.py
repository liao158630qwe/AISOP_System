"""Real SQLite/PostgreSQL upgrades, rollback and relational integrity contracts."""
from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import pytest
from sqlalchemy import ForeignKeyConstraint, MetaData, create_engine, event, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend import db
from backend.migrations import (CURRENT_VERSION, MigrationValidationError, SchemaError,
                                check_database, require_current)


@pytest.fixture(params=['sqlite', 'postgresql'])
def database(request, tmp_path):
    if request.param == 'sqlite':
        engine = db.make_engine('sqlite:///' + (tmp_path / 'migration.db').as_posix())
        try:
            yield engine
        finally:
            engine.dispose()
    else:
        url = os.getenv('TEST_POSTGRES_URL')
        if not url:
            pytest.skip('PostgreSQL service not configured')
        admin = create_engine(url)
        name = 'test_' + uuid.uuid4().hex
        with admin.begin() as c:
            c.exec_driver_sql(f'CREATE SCHEMA {name}')
        engine = create_engine(url, connect_args={'options': f'-csearch_path={name} -clock_timeout=5000'})
        try:
            yield engine
        finally:
            engine.dispose()
            with admin.begin() as c:
                c.exec_driver_sql(f'DROP SCHEMA {name} CASCADE')
            admin.dispose()


def legacy_database(engine, *, only=None):
    """Reproduce the shipped v1 tables, omitting every v2 constraint/history."""
    old = MetaData()
    for table in db.metadata.tables.values():
        if table.name != 'schema_migrations':
            table.to_metadata(old)
    names = {constraint.name for constraint in db.v2_constraints}
    for table in old.tables.values():
        for constraint in list(table.constraints):
            if constraint.name in names:
                table.constraints.remove(constraint)
                if isinstance(constraint, ForeignKeyConstraint):
                    for fk in constraint.elements:
                        table.foreign_keys.remove(fk)
                        fk.parent.foreign_keys.remove(fk)
    with engine.begin() as c:
        old.create_all(c, tables=[old.tables[name] for name in only] if only else None)
        c.execute(db.schema.insert().values(version=1))


def media_row(identity='media', owner='owner'):
    return dict(id=identity, owner=owner, name='原始录像.mp4', suffix='.mp4', sha256='fixture',
                size=1, metadata={'camera': '工位一'}, created_at=1)


def run_row(identity='run', **extra):
    return dict(dict(id=identity, owner='owner', media_id='media', status='QUEUED', request_key=identity,
                     request_hash='fixture', options={}, model_hash='fixture', progress=0,
                     attempts=0, token=0, created_at=1, updated_at=1), **extra)


def seed(c):
    c.execute(db.media.insert().values(**media_row()))
    c.execute(db.runs.insert().values(**run_row()))


def test_fresh_schema_and_repeated_initialization(database):
    assert db.initialize(database) == CURRENT_VERSION
    with database.begin() as c:
        seed(c)
    db.initialize(database)
    assert check_database(database)['ready']
    with database.connect() as c:
        require_current(c)
        assert c.execute(select(db.runs.c.id)).scalar_one() == 'run'
        assert c.execute(select(db.schema_migrations.c.version).order_by(db.schema_migrations.c.version)).scalars().all() == list(range(1, CURRENT_VERSION + 1))


def test_v1_upgrade_preserves_data_and_adds_constraints(database):
    legacy_database(database)
    with database.begin() as c:
        seed(c)
        before = dict(c.execute(select(db.media)).mappings().one())
    preview = check_database(database)
    assert preview['can_upgrade'] and preview['pending'] == ['relational_integrity', 'durable_collector_control']
    assert preview['current_version'] == 1 and not preview['ready']
    db.initialize(database)
    db.initialize(database)
    assert check_database(database)['ready']
    with database.connect() as c:
        assert dict(c.execute(select(db.media)).mappings().one()) == before
    with pytest.raises(IntegrityError), database.begin() as c:
        c.execute(db.runs.insert().values(**run_row('invalid', media_id='absent')))


def test_partial_additive_v1_is_upgraded(database):
    legacy_database(database, only=['schema_version', 'media', 'analysis_runs', 'scene_tasks'])
    with database.begin() as c:
        seed(c)
    db.initialize(database)
    assert check_database(database)['ready']


def test_v2_adds_durable_collector_tables_without_changing_history(database):
    with database.begin() as c:
        tables = [table for table in db.metadata.sorted_tables if table.name not in
                  ('station_collectors', 'station_collector_commands')]
        db.metadata.create_all(c, tables=tables)
        c.execute(db.schema.insert().values(version=2))
        c.execute(db.schema_migrations.insert(), [dict(version=version, name=name, applied_at=1) for version, name in
            ((1, 'legacy_core_baseline'), (2, 'relational_integrity'))])
        seed(c)
    assert check_database(database)['pending'] == ['durable_collector_control']
    db.initialize(database)
    assert check_database(database)['ready']
    with database.connect() as c:
        assert c.execute(select(db.runs.c.id)).scalar_one() == 'run'
        assert not c.execute(select(db.station_collectors)).first()


@pytest.mark.parametrize('versions', [[], [0], [1, 2], [99]])
def test_invalid_watermark_rejected_before_ddl(database, versions):
    with database.begin() as c:
        db.schema.create(c)
        for version in versions:
            c.execute(db.schema.insert().values(version=version))
    with pytest.raises(SchemaError):
        db.initialize(database)
    assert inspect(database).get_table_names() == ['schema_version']
    with database.connect() as c:
        assert sorted(c.execute(select(db.schema.c.version)).scalars()) == versions


@pytest.mark.parametrize('change,expected', [
    ({'media_id': 'absent'}, 'fk_runs_media_owner'),
    ({'owner': 'another'}, 'fk_runs_media_owner'),
    ({'progress': 1.5}, 'ck_runs_progress'),
    ({'status': 'UNKNOWN'}, 'ck_runs_status'),
])
def test_bad_history_blocks_upgrade_without_repairing_it(database, change, expected):
    legacy_database(database)
    with database.begin() as c:
        c.execute(db.media.insert().values(**media_row()))
        c.execute(db.runs.insert().values(**run_row(**change)))
    preview = check_database(database)
    assert not preview['can_upgrade']
    assert expected in {issue['constraint'] for issue in preview['issues']}
    with pytest.raises(MigrationValidationError):
        db.initialize(database)
    assert 'schema_migrations' not in inspect(database).get_table_names()
    with database.connect() as c:
        assert c.execute(select(db.schema.c.version)).scalar_one() == 1
        row = c.execute(select(db.runs)).mappings().one()
        assert all(row[key] == value for key, value in change.items())
    with database.begin() as c:
        c.execute(db.runs.update().values(media_id='media', owner='owner', progress=0, status='QUEUED'))
    db.initialize(database)
    assert check_database(database)['ready']


def test_failure_after_ddl_rolls_back_and_can_retry(database):
    legacy_database(database)
    with database.begin() as c:
        seed(c)
    def fail(c, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO schema_migrations'):
            raise RuntimeError('simulated migration interruption')
    event.listen(database, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='simulated migration interruption'):
            db.initialize(database)
    finally:
        event.remove(database, 'before_cursor_execute', fail)
    assert 'schema_migrations' not in inspect(database).get_table_names()
    assert inspect(database).get_foreign_keys('analysis_runs') == []
    with database.connect() as c:
        assert c.execute(select(db.schema.c.version)).scalar_one() == 1
        assert c.execute(select(db.runs.c.id)).scalar_one() == 'run'
        if c.dialect.name == 'sqlite':
            assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
    db.initialize(database)
    assert check_database(database)['ready']


def test_concurrent_initializers_commit_one_history(database):
    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(lambda _: db.initialize(database), range(4)))
    assert versions == [CURRENT_VERSION] * 4
    assert check_database(database)['ready']


def test_migration_lock_wait_is_bounded(database, monkeypatch):
    db.initialize(database)
    monkeypatch.setenv('DB_MIGRATION_LOCK_TIMEOUT_SECONDS', '1')
    with database.connect() as holder:
        if database.dialect.name == 'sqlite':
            holder.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            holder.execute(text('SELECT pg_advisory_xact_lock(1095324496, hashtext(current_schema()))'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(db.initialize, database)
            with pytest.raises(DBAPIError):
                future.result(timeout=5)
        holder.rollback()
    db.initialize(database)
    assert check_database(database)['ready']


def test_missing_table_on_current_version_is_not_silently_recreated(database):
    db.initialize(database)
    with database.begin() as c:
        c.exec_driver_sql('DROP TABLE run_metrics')
    assert not check_database(database)['ready']
    with pytest.raises(MigrationValidationError):
        db.initialize(database)
    assert 'run_metrics' not in inspect(database).get_table_names()


def test_constraints_reject_orphans_owner_mismatch_and_bad_values(database):
    db.initialize(database)
    with database.begin() as c:
        seed(c)
        c.execute(db.run_metrics.insert().values(run_id='run', queued_at=1))
    for values in ({'owner': 'another'}, {'media_id': 'absent'}, {'status': 'UNKNOWN'},
                   {'progress': -0.01}, {'progress': 1.01}, {'attempts': -1}, {'token': -1}):
        with pytest.raises(IntegrityError), database.begin() as c:
            c.execute(db.runs.insert().values(**run_row('invalid', **values)))
    with pytest.raises(IntegrityError), database.begin() as c:
        c.execute(db.run_metrics.insert().values(run_id='absent', queued_at=1))
    with pytest.raises(IntegrityError), database.begin() as c:
        c.execute(db.media.delete())
    with database.begin() as c:
        c.execute(db.runs.delete())
        assert not c.execute(select(db.run_metrics)).first()
        c.execute(db.media.delete())


def test_dataset_frames_cannot_cross_datasets_or_repeat_ordinal(database):
    db.initialize(database)
    with database.begin() as c:
        c.execute(db.training_datasets.insert(), [dict(id=identity, owner='owner', name='fixture', station='station1',
            task='detect', classes=[], revision=1, created_at=1, updated_at=1) for identity in ('a', 'b')])
        c.execute(db.dataset_videos.insert().values(id='video', dataset_id='a', name='fixture', path='fixture',
            sha256='fixture', size=1, capture={}, frame_budget=1, revision=1, request_hash='fixture', job_id='job', created_at=1))
        frame = dict(id='one', dataset_id='a', video_id='video', ordinal=0, timestamp=0, path='fixture',
                     sha256='fixture', width=10, height=10, review='pending', annotation={}, revision=1)
        c.execute(db.dataset_frames.insert().values(**frame))
    for values in ({'dataset_id': 'b', 'ordinal': 1}, {'ordinal': 0}, {'ordinal': 1, 'width': 0}):
        with pytest.raises(IntegrityError), database.begin() as c:
            c.execute(db.dataset_frames.insert().values(**dict(frame, id='two', **values)))
    with database.begin() as c:
        c.execute(db.dataset_videos.delete())
        assert not c.execute(select(db.dataset_frames)).first()


def test_sqlite_rebuild_preserves_extra_columns_indexes_triggers_and_views(database):
    if database.dialect.name != 'sqlite':
        pytest.skip('SQLite table rebuild only')
    legacy_database(database)
    with database.begin() as c:
        seed(c)
        c.exec_driver_sql('ALTER TABLE media ADD COLUMN custom_note TEXT')
        c.exec_driver_sql("UPDATE media SET custom_note='保留字段'")
        c.exec_driver_sql('CREATE INDEX custom_media_name ON media(lower(name))')
        c.exec_driver_sql('CREATE VIEW media_names AS SELECT name, custom_note FROM media')
        c.exec_driver_sql('CREATE TRIGGER custom_media_trigger AFTER UPDATE OF name ON media '
                          "BEGIN UPDATE media SET custom_note='updated' WHERE id=NEW.id; END")
    db.initialize(database)
    with database.begin() as c:
        assert c.exec_driver_sql('SELECT custom_note FROM media_names').scalar_one() == '保留字段'
        indexes = set(c.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='index'").scalars())
        assert {'custom_media_name', 'ix_runs_owner_type_created', 'ix_inspections_archive_day'} <= indexes
        c.exec_driver_sql("UPDATE media SET name='changed'")
        assert c.exec_driver_sql('SELECT custom_note FROM media_names').scalar_one() == 'updated'
        assert not c.exec_driver_sql('PRAGMA foreign_key_check').first()


def test_readiness_refuses_old_or_corrupt_schema(context):
    from backend.health import pulse_worker
    _, app, client = context
    pulse_worker(app.state.engine, 'test-worker')
    assert client.get('/api/v1/ready').status_code == 200
    with app.state.engine.begin() as c:
        c.execute(db.schema.update().values(version=1))
    response = client.get('/api/v1/ready')
    assert response.status_code == 503 and response.json()['database'] == 'migration_required'
    with app.state.engine.begin() as c:
        c.execute(db.schema.update().values(version=CURRENT_VERSION))
        c.execute(db.schema_migrations.delete().where(db.schema_migrations.c.version == 2))
    assert client.get('/api/v1/ready').status_code == 503


def test_cli_check_is_read_only_and_reports_pending(tmp_path, monkeypatch, capsys):
    from backend.init_db import main
    path = tmp_path / 'cli.db'
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + path.as_posix())
    monkeypatch.setattr('sys.argv', ['init_db', '--check'])
    with pytest.raises(SystemExit):
        main()
    assert not path.exists()
    engine = db.make_engine(os.environ['DATABASE_URL'])
    try:
        legacy_database(engine)
        before = path.read_bytes()
        assert main() == 2
        assert path.read_bytes() == before
        assert '"current_version": 1' in capsys.readouterr().out
        monkeypatch.setattr('sys.argv', ['init_db'])
        assert main() == 0
        monkeypatch.setattr('sys.argv', ['init_db', '--check'])
        assert main() == 0
    finally:
        engine.dispose()
