"""Run PostgreSQL integration tests in a project-owned, loopback-only cluster.

Uses portable PostgreSQL binaries; credentials stay under ignored .runtime/.
The test process receives TEST_POSTGRES_URL without changing the app database.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime/postgres-test'
DATA = RUNTIME / 'data'
SETTINGS = RUNTIME / 'settings.json'
DEFAULT_BIN = ROOT / '.runtime/postgres-test-runtime/pgsql/bin'
DATABASE = 'aisop_integration_test'
USER = 'aisop_test'
ADMIN = 'aisop_test_admin'


@contextmanager
def operation_lock():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / 'runner.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another PostgreSQL test runner is active') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def command(bin_dir, program, *args, check=True, timeout=45):
    path = bin_dir / (program + ('.exe' if os.name == 'nt' else ''))
    if not path.is_file():
        raise RuntimeError(f'PostgreSQL binary missing: {path}; see docs/guides/postgres-testing.md')
    # Windows child servers inherit pipe handles from pg_ctl. A real file avoids
    # waiting for the long-running server to close the parent's output pipe.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run([str(path), *map(str, args)], cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=output, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        output.seek(0)
        result.stdout = output.read().decode('utf-8', errors='replace')
        result.stderr = ''
    if check and result.returncode:
        raise RuntimeError(f'{program} failed ({result.returncode}): {result.stderr or result.stdout}')
    return result


def load_settings(args):
    if SETTINGS.exists():
        settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
        if settings.get('project') != str(ROOT) or settings.get('database') != DATABASE:
            raise RuntimeError('Test cluster ownership marker does not match this project')
        if args.port is not None and args.port != settings['port']:
            raise RuntimeError('An existing cluster uses a different port; use its saved configuration')
        if args.bin_dir is not None and args.bin_dir.resolve() != Path(settings['bin_dir']):
            raise RuntimeError('An existing cluster uses different binaries')
        return settings
    if DATA.exists() and any(DATA.iterdir()):
        raise RuntimeError('Existing data has no ownership marker; refusing to initialize or control it')
    settings = dict(project=str(ROOT), database=DATABASE, port=args.port or 15432,
        bin_dir=str((args.bin_dir or DEFAULT_BIN).resolve()),
        admin_password=secrets.token_urlsafe(32), password=secrets.token_urlsafe(32))
    SETTINGS.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    return settings


def initialize(settings):
    if (DATA / 'PG_VERSION').is_file():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    password_file = RUNTIME / 'init-password.txt'
    try:
        password_file.write_text(settings['admin_password'], encoding='utf-8')
        command(Path(settings['bin_dir']), 'initdb', '-D', DATA, '-U', ADMIN,
            '--pwfile', password_file, '--auth-host=scram-sha-256', '--auth-local=scram-sha-256',
            '--encoding=UTF8', '--locale=C', '--no-instructions')
    finally:
        password_file.unlink(missing_ok=True)
    with (DATA / 'postgresql.conf').open('a', encoding='utf-8') as handle:
        handle.write(f"\n# AISOP disposable integration database\nlisten_addresses = '127.0.0.1'\n"
            f"port = {settings['port']}\nshared_buffers = '64MB'\nmax_connections = 40\n"
            "log_statement = 'none'\nlog_min_error_statement = 'panic'\n")


def running(settings):
    if not (DATA / 'PG_VERSION').exists():
        return False
    return command(Path(settings['bin_dir']), 'pg_ctl', '-D', DATA, 'status', check=False).returncode == 0


def connect(settings, admin=False):
    return psycopg.connect(host='127.0.0.1', port=settings['port'],
        dbname='postgres' if admin else DATABASE, user=ADMIN if admin else USER,
        password=settings['admin_password' if admin else 'password'], connect_timeout=5, autocommit=True)


def provision(settings):
    with connect(settings, admin=True) as connection:
        directory = Path(connection.execute('SHOW data_directory').fetchone()[0]).resolve()
        if directory != DATA.resolve():
            raise RuntimeError('Connected to a different PostgreSQL data directory')
        if not connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (USER,)).fetchone():
            connection.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}')
                .format(sql.Identifier(USER), sql.Literal(settings['password'])))
        if not connection.execute('SELECT 1 FROM pg_database WHERE datname=%s', (DATABASE,)).fetchone():
            connection.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(DATABASE), sql.Identifier(USER)))
    with connect(settings) as connection:
        role = connection.execute('SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
        if any(role):
            raise RuntimeError('The integration test role must not have elevated cluster privileges')
        return connection.execute('SELECT version()').fetchone()[0]


def start(settings):
    if running(settings):
        return False
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', settings['port']))
        except OSError as exc:
            raise RuntimeError(f"Port {settings['port']} is occupied by another process") from exc
    initialize(settings)
    try:
        command(Path(settings['bin_dir']), 'pg_ctl', '-D', DATA, '-l', RUNTIME/'server.log', '-w', '-t', '30', 'start')
    except (RuntimeError, subprocess.TimeoutExpired):
        if running(settings):
            stop(settings)
        raise
    return True


def stop(settings):
    if running(settings):
        command(Path(settings['bin_dir']), 'pg_ctl', '-D', DATA, '-m', 'fast', '-w', '-t', '30', 'stop')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--start', action='store_true', help='Start the test cluster and leave it running')
    actions.add_argument('--stop', action='store_true', help='Stop only this project test cluster')
    actions.add_argument('--status', action='store_true', help='Show status without starting the cluster')
    parser.add_argument('--bin-dir', type=Path)
    parser.add_argument('--port', type=int)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER, help='Arguments after -- override the default PostgreSQL test selection')
    args = parser.parse_args()
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error('The test port must be between 1024 and 65535')
    with operation_lock():
        if (args.stop or args.status) and not SETTINGS.exists():
            print('PostgreSQL test cluster is not configured')
            return 0
        settings = load_settings(args)
        if args.status:
            print(json.dumps(dict(running=running(settings),host='127.0.0.1',port=settings['port'],database=DATABASE)))
            return 0
        if args.stop:
            stop(settings)
            print('PostgreSQL test cluster stopped')
            return 0
        started = start(settings)
        keep_running = False
        try:
            version = provision(settings)
            print(f"Test PostgreSQL ready at 127.0.0.1:{settings['port']}/{DATABASE}\n{version}", flush=True)
            if args.start:
                keep_running = True
                return 0
            env = dict(os.environ, TEST_POSTGRES_URL=URL.create('postgresql+psycopg',username=USER,
                password=settings['password'],host='127.0.0.1',port=settings['port'],database=DATABASE).render_as_string(hide_password=False))
            selection = args.pytest_args
            if selection and selection[0] == '--':
                selection = selection[1:]
            if not selection:
                selection = ['tests/test_postgres.py', 'tests/test_database_migrations.py',
                             'tests/test_inspection_history_postgres.py', '-q', '--tb=short',
                             '--junitxml=test-results/postgres-integration.xml']
            result = subprocess.run([sys.executable,'-m','pytest',*selection],cwd=ROOT,env=env)
            with connect(settings) as connection:
                schemas = connection.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'test\\_%' ESCAPE '\\'").fetchall()
            print(json.dumps(dict(testExitCode=result.returncode,leftoverTestSchemas=len(schemas))),flush=True)
            return result.returncode or (1 if schemas else 0)
        finally:
            if started and not keep_running:
                stop(settings)
                print('Stopped the test PostgreSQL process; configuration retained',flush=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, psycopg.Error, subprocess.TimeoutExpired) as error:
        message = str(error)
        if SETTINGS.exists():
            saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
            for key in ('password','admin_password'):
                message = message.replace(saved[key], '[redacted]')
        raise SystemExit(message)
