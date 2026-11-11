"""Explicit, atomic schema upgrades for PostgreSQL and SQLite.

The legacy schema_version row remains the version watermark. Migration history,
DDL and data validation commit together. Run with API/worker stopped; existing
data is never silently repaired or deleted to make an upgrade pass.
"""
from contextlib import contextmanager
import os
import warnings

from sqlalchemy import ForeignKeyConstraint, and_, func, inspect, select, text
from sqlalchemy.exc import SAWarning
from sqlalchemy.schema import CreateIndex

from . import db
from .schema_constraints import CHECKS, FOREIGN_KEYS, UNIQUES

CURRENT_VERSION = 3
MIGRATION_NAMES = {1: 'legacy_core_baseline', 2: 'relational_integrity', 3: 'durable_collector_control'}


class SchemaError(RuntimeError):
    """Unsupported schema or data requiring operator review before upgrading."""


class MigrationValidationError(SchemaError):
    def __init__(self, issues):
        self.issues = issues
        names = ', '.join(f"{item['table']}.{item['constraint']} ({item['count']})" for item in issues)
        super().__init__('Database upgrade rejected; no changes committed: ' + names +
                         '. Run python -m backend.init_db --check for details.')


def _version(c, tables):
    if 'schema_version' not in tables:
        if tables.intersection(db.metadata.tables):
            raise SchemaError('Application tables exist without schema_version; review the database before upgrading')
        return 0
    versions = c.execute(select(db.schema.c.version)).scalars().all()
    if len(versions) != 1 or versions[0] not in MIGRATION_NAMES:
        raise SchemaError(f'Unsupported database schema versions: {versions}; expected one of {list(MIGRATION_NAMES)}')
    version = versions[0]
    if version >= 2:
        if 'schema_migrations' not in tables:
            raise SchemaError('Migration history is missing; refusing to trust the schema version')
        history = dict(c.execute(select(db.schema_migrations.c.version, db.schema_migrations.c.name)).all())
        if history != {v: MIGRATION_NAMES[v] for v in range(1, version + 1)}:
            raise SchemaError('Migration history does not match schema_version')
    return version


def require_current(c):
    """Cheap readiness/startup gate; never changes a schema."""
    versions = c.execute(select(db.schema.c.version)).scalars().all()
    if versions != [CURRENT_VERSION]:
        raise SchemaError('Database migration required; run python -m backend.init_db before starting services')
    history = dict(c.execute(select(db.schema_migrations.c.version, db.schema_migrations.c.name)).all())
    if history != MIGRATION_NAMES:
        raise SchemaError('Database migration history is incomplete')


def _layout_issues(c, tables):
    inspector = inspect(c)
    issues = []
    for name in sorted(tables.intersection(db.metadata.tables)):
        actual = {column['name'] for column in inspector.get_columns(name)}
        missing = set(db.metadata.tables[name].c.keys()) - actual
        if missing:
            issues.append(dict(table=name, constraint='required_columns', count=len(missing),
                               columns=sorted(missing)))
    return issues


def _data_issues(c, tables):
    """Read-only preflight with bounded examples, including tenant ownership."""
    issues = []

    def check(table_name, name, predicate):
        table = db.metadata.tables[table_name]
        count = c.execute(select(func.count()).select_from(table).where(predicate)).scalar_one()
        if count:
            examples = c.execute(select(*table.primary_key.columns).where(predicate).limit(5)).all()
            issues.append(dict(table=table_name, constraint=name, count=count,
                               sample_keys=[list(row) for row in examples]))

    for table, name, expression in CHECKS:
        if table in tables:
            check(table, name, text(f'NOT ({expression})'))
    for table, name, columns, parent, targets, _ in FOREIGN_KEYS:
        if table not in tables:
            continue
        child, target = db.metadata.tables[table], db.metadata.tables[parent]
        present = and_(*(child.c[col].is_not(None) for col in columns))
        if parent in tables:
            exists = select(1).select_from(target).where(
                and_(*(child.c[col] == target.c[dest] for col, dest in zip(columns, targets)))).exists()
            check(table, name, present & ~exists)
        else:
            check(table, name, present)
    for table, name, columns in UNIQUES:
        if table not in tables:
            continue
        source = db.metadata.tables[table]
        fields = [source.c[col] for col in columns]
        duplicates = (select(*fields).where(and_(*(col.is_not(None) for col in fields)))
                      .group_by(*fields).having(func.count() > 1))
        count = c.execute(select(func.count()).select_from(duplicates.subquery())).scalar_one()
        if count:
            issues.append(dict(table=table, constraint=name, count=count,
                               sample_keys=[list(row) for row in c.execute(duplicates.limit(5))]))
    return issues


def _installed_constraints(c, name):
    inspector = inspect(c)
    with warnings.catch_warnings():
        # sqlite3 reflection cannot represent expression indexes. We preserve
        # their SQL directly and do not use this reflection to recreate them.
        warnings.filterwarnings('ignore', 'Skipped unsupported reflection of expression-based index', SAWarning)
        return {item['name'] for item in (
            inspector.get_check_constraints(name) + inspector.get_foreign_keys(name) +
            inspector.get_unique_constraints(name))}


def _structure_issues(c, tables, version=CURRENT_VERSION):
    required = set(db.metadata.tables)
    if version < 3:
        required -= {'station_collectors', 'station_collector_commands'}
    issues = [dict(table=name, constraint='required_table', count=1)
              for name in sorted(required - tables)]
    installed = {name: _installed_constraints(c, name) for name in tables.intersection(db.metadata.tables)}
    for constraint in db.v2_constraints:
        name = constraint.table.name
        if name in installed and constraint.name not in installed[name]:
            issues.append(dict(table=name, constraint=constraint.name, count=1, kind='missing_constraint'))
    return issues


def check_database(engine):
    """Report migration readiness without DDL, data repair, or WAL changes."""
    with engine.connect() as c:
        # sqlite3 legacy mode otherwise gives each SELECT a different snapshot.
        if c.dialect.name == 'sqlite':
            c.exec_driver_sql('BEGIN')
        else:
            c.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        tables = set(inspect(c).get_table_names())
        version = _version(c, tables)
        issues = _layout_issues(c, tables)
        if not issues:
            issues = _data_issues(c, tables)
        if version >= 2:
            issues.extend(_structure_issues(c, tables, version))
        return dict(current_version=version, target_version=CURRENT_VERSION,
                    pending=[MIGRATION_NAMES[v] for v in range(version + 1, CURRENT_VERSION + 1)],
                    ready=version == CURRENT_VERSION and not issues,
                    can_upgrade=not issues, issues=issues)


def _lock_timeout_ms():
    try:
        seconds = int(os.getenv('DB_MIGRATION_LOCK_TIMEOUT_SECONDS', '60'))
    except ValueError as exc:
        raise ValueError('DB_MIGRATION_LOCK_TIMEOUT_SECONDS must be an integer') from exc
    if not 1 <= seconds <= 600:
        raise ValueError('DB_MIGRATION_LOCK_TIMEOUT_SECONDS must be between 1 and 600')
    return seconds * 1000


@contextmanager
def _migration_transaction(engine):
    timeout_ms = _lock_timeout_ms()
    with engine.connect() as c:
        if c.dialect.name not in ('sqlite', 'postgresql'):
            raise SchemaError('Only PostgreSQL and SQLite migrations are supported')
        if c.dialect.name == 'sqlite':
            # Configure WAL before the atomic upgrade, so a journal-mode error
            # cannot report failure after a successfully committed migration.
            _version(c, set(inspect(c).get_table_names()))
            c.rollback()
            c.exec_driver_sql('PRAGMA journal_mode=WAL')
            c.commit()
            # PRAGMA foreign_keys is ineffective inside a transaction. Disable
            # only this maintenance connection, then explicitly BEGIN so DDL
            # is rollbackable even with sqlite3 legacy transaction control.
            previous_timeout = c.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
            c.rollback()
            c.exec_driver_sql('PRAGMA foreign_keys=OFF')
            c.exec_driver_sql(f'PRAGMA busy_timeout={timeout_ms}')
            c.commit()
            try:
                c.exec_driver_sql('BEGIN IMMEDIATE')
                yield c
                if c.exec_driver_sql('PRAGMA foreign_key_check').first():
                    raise SchemaError('Foreign key validation failed; migration rolled back')
                c.commit()
            except BaseException:
                c.rollback()
                raise
            finally:
                try:
                    c.exec_driver_sql('PRAGMA foreign_keys=ON')
                    c.exec_driver_sql(f'PRAGMA busy_timeout={previous_timeout}')
                    if c.exec_driver_sql('PRAGMA foreign_keys').scalar_one() != 1:
                        raise SchemaError('Cannot restore SQLite foreign-key enforcement')
                    c.commit()
                except BaseException:
                    c.invalidate()
                    raise
        else:
            with c.begin():
                c.execute(text("SELECT set_config('lock_timeout', :value, true)"), {'value': f'{timeout_ms}ms'})
                # One database/schema-specific transaction lock, including an
                # empty database where no application row exists to lock yet.
                c.execute(text('SELECT pg_advisory_xact_lock(1095324496, hashtext(current_schema()))'))
                yield c


def _sqlite_add_constraints(c, grouped):
    """SQLite's documented new-table/copy/drop/rename sequence in one transaction.

    Preserve original column DDL, extra columns, expression indexes, triggers
    and views. Reflection alone would silently lose expression indexes.
    """
    quote = c.dialect.identifier_preparer.quote
    objects = c.exec_driver_sql("SELECT type, name, sql FROM sqlite_schema "
                               "WHERE type IN ('trigger','view') AND sql IS NOT NULL").all()
    for kind, name, _ in sorted(objects, key=lambda row: row[0]):
        c.exec_driver_sql(f'DROP {kind.upper()} {quote(name)}')
    for name, constraints in grouped.items():
        original = c.execute(text('SELECT sql FROM sqlite_schema WHERE type=\'table\' AND name=:name'),
                             {'name': name}).scalar_one()
        indexes = c.execute(text("SELECT sql FROM sqlite_schema WHERE type='index' "
                                 'AND tbl_name=:name AND sql IS NOT NULL'), {'name': name}).scalars().all()
        temporary = '__aisop_v2_' + name
        start, end = original.index('('), original.rfind(')')
        compiler = c.dialect.ddl_compiler(c.dialect, None)
        additions = ',\n\t'.join(compiler.process(constraint) for constraint in constraints)
        c.exec_driver_sql(f'CREATE TABLE {quote(temporary)} ' + original[start:end].rstrip() +
                          ',\n\t' + additions + '\n' + original[end:])
        # table_xinfo preserves extra user columns and skips generated columns.
        columns = ', '.join(quote(row[1]) for row in c.exec_driver_sql(f'PRAGMA table_xinfo({quote(name)})') if row[6] == 0)
        c.exec_driver_sql(f'INSERT INTO {quote(temporary)} ({columns}) SELECT {columns} FROM {quote(name)}')
        c.exec_driver_sql(f'DROP TABLE {quote(name)}')
        c.exec_driver_sql(f'ALTER TABLE {quote(temporary)} RENAME TO {quote(name)}')
        for statement in indexes:
            c.exec_driver_sql(statement)
    for _, _, statement in sorted(objects, key=lambda row: row[0], reverse=True):
        c.exec_driver_sql(statement)


def _upgrade_v2(c, existing):
    # Adding referenced unique keys precedes adding child foreign keys.
    installed = {name: _installed_constraints(c, name) for name in existing.intersection(db.metadata.tables)}
    pending = [constraint for constraint in db.v2_constraints
               if constraint.table.name in existing and constraint.name not in installed[constraint.table.name]]
    if c.dialect.name == 'postgresql':
        # SQLAlchemy 2.0.34 AddConstraint mutates shared metadata's create rule.
        # Compile the fragment directly so future fresh databases still get it.
        compiler = c.dialect.ddl_compiler(c.dialect, None)
        quote = c.dialect.identifier_preparer.quote

        def add(constraint):
            c.exec_driver_sql(f'ALTER TABLE {quote(constraint.table.name)} ADD ' + compiler.process(constraint))

        for constraint in pending:
            if not isinstance(constraint, ForeignKeyConstraint):
                add(constraint)
        db.metadata.create_all(c)
        for constraint in pending:
            if isinstance(constraint, ForeignKeyConstraint):
                add(constraint)
    elif pending:
        grouped = {}
        for constraint in pending:
            grouped.setdefault(constraint.table.name, []).append(constraint)
        _sqlite_add_constraints(c, grouped)


def upgrade(engine):
    with _migration_transaction(engine) as c:
        existing = set(inspect(c).get_table_names())
        version = _version(c, existing)  # Reject unknown versions BEFORE any DDL.
        issues = _layout_issues(c, existing)
        if version >= 2:
            issues.extend(_structure_issues(c, existing, version))
        if issues:
            raise MigrationValidationError(issues)
        if c.dialect.name == 'postgresql' and version < CURRENT_VERSION:
            names = sorted(existing.intersection(db.metadata.tables))
            if names:
                quote = c.dialect.identifier_preparer.quote
                c.exec_driver_sql('LOCK TABLE ' + ', '.join(map(quote, names)) + ' IN SHARE ROW EXCLUSIVE MODE')
        if version < CURRENT_VERSION:
            issues = _data_issues(c, existing)
            if issues:
                raise MigrationValidationError(issues)
        # v1 evolved additively. Bring missing tables up to the current layout;
        # add constraints to existing parents first so new children can refer to
        # their composite unique keys on PostgreSQL.
        if version < 2:
            _upgrade_v2(c, existing)
        db.metadata.create_all(c)
        for table in db.metadata.sorted_tables:
            for index in sorted(table.indexes, key=lambda item: item.name):
                c.execute(CreateIndex(index, if_not_exists=True))
        for target in range(version + 1, CURRENT_VERSION + 1):
            c.execute(db.schema_migrations.insert().values(version=target, name=MIGRATION_NAMES[target], applied_at=db.now(c)))
        if version == 1:
            c.execute(db.schema_migrations.insert().values(version=1, name=MIGRATION_NAMES[1], applied_at=db.now(c)))
        c.execute(db.schema.delete())
        c.execute(db.schema.insert().values(version=CURRENT_VERSION))
    return CURRENT_VERSION
