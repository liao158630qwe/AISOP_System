"""Start or repair the local API and Worker on the shared port-8000 entry point."""
import argparse
from contextlib import closing, contextmanager
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import psutil

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
BASE_URL = 'http://127.0.0.1:8000'
SERVICE_ARGS = {
    'api': ['-m', 'uvicorn', 'backend.app:create_app', '--factory', '--host', '127.0.0.1', '--port', '8000'],
    'worker': ['-m', 'backend.worker'],
}
INFERENCE_DEFAULTS = {
    'INFERENCE_DEVICE': 'cuda:0', 'TRAINING_DEVICE': 'cuda:0',
    'REALTIME_BACKEND': 'yolo', 'POSE_BACKEND': 'yolo', 'REALTIME_POOL_SIZE': 2,
    'SPECIALIST_POOL_SIZE': 1, 'STATION_INFERENCE_CONCURRENCY': 1,
    'OPENCV_THREADS': 4, 'TORCH_THREADS': 4, 'SCENE_MODEL_CACHE_SIZE': 1,
    'STATION_QUEUE_WAIT_MS': 500, 'WORKER_ANALYSIS_ENABLED': True,
    'WORKER_TRAINING_ENABLED': True, 'REALTIME_WARMUP': True,
    'REALTIME_MAX_DURATION_SECONDS': 600, 'REALTIME_NO_TARGET_SECONDS': 60,
}


@contextmanager
def startup_lock():
    """npm and the Windows launcher share an OS lock, released on crashes."""
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / 'local-start.lock').open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
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
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another local startup/restart is in progress. Try again after it finishes.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def process_role(process):
    """Never trust a saved PID: require this project's cwd and exact module."""
    try:
        if Path(process.cwd()).resolve() != ROOT.resolve():
            return None
        args = process.cmdline()
        if not args or not Path(args[0]).name.lower().startswith(('python', 'pypy')):
            return None
        module_index = args.index('-m')
        module = args[module_index + 1]
        if module == 'backend.worker' and '--once' not in args:
            return 'worker'
        if module == 'uvicorn' and args[module_index + 2] == 'backend.app:create_app':
            port = args[args.index('--port') + 1] if '--port' in args else '8000'
            return 'api' if port == '8000' else None
    except (psutil.Error, OSError, ValueError, IndexError):
        pass
    return None


def services():
    found = {'api': [], 'worker': []}
    for process in psutil.process_iter():
        role = process_role(process)
        if role:
            found[role].append(process)
    # Windows venv launchers and their interpreter child can have the same argv.
    for role, processes in found.items():
        ids = {process.pid for process in processes}
        roots = []
        for process in processes:
            try:
                if process.ppid() not in ids:
                    roots.append(process)
            except psutil.Error:
                pass
        found[role] = roots
    return found


def port_in_use():
    with socket.socket() as check:
        check.settimeout(.5)
        return check.connect_ex(('127.0.0.1', 8000)) == 0


def port_owners():
    return {entry.pid for entry in psutil.net_connections(kind='tcp')
            if entry.status == psutil.CONN_LISTEN and entry.laddr.port == 8000}


def process_tree(process):
    try:
        return [process, *process.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def verify_port_ownership(found):
    if not port_in_use():
        return False
    allowed = {child.pid for process in found['api'] for child in process_tree(process)}
    owners = port_owners()
    if not owners or None in owners or not owners.issubset(allowed):
        raise RuntimeError('Port 8000 belongs to an unverified service. No process was stopped. '
                           'Use the owning application to free the port.')
    return True


def read_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE_URL + '/api/v1/health', timeout=2) as response:
        payload = json.load(response)
    if (payload.get('status') != 'ready' or payload.get('analysis') != 'screw-and-sticker-inspection'
            or not isinstance(payload.get('capabilities'), dict)):
        raise RuntimeError('Port 8000 did not return the expected healthy AI-SOP service.')
    return payload


def verify_login(config):
    # Check that a reused API uses these credentials. Remove the temporary session.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    request = urllib.request.Request(BASE_URL + '/api/v1/login', data=json.dumps(config).encode(),
                                     headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=5) as response:
        session = json.load(response)
    request = urllib.request.Request(BASE_URL + '/api/v1/logout', data=b'', method='POST',
                                     headers={'X-CSRF-Token': session['csrf']})
    with opener.open(request, timeout=5):
        pass


def local_environment():
    config_path = RUNTIME / 'local-settings.json'
    if not config_path.exists():
        config_path.write_text(json.dumps({'username': 'admin', 'password': secrets.token_urlsafe(18)},
                                         indent=2), encoding='utf-8')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if not config.get('username') or not config.get('password'):
        raise RuntimeError('local-settings.json must contain a username and a nonempty password.')
    env = dict(os.environ, APP_ENV='development', COOKIE_SECURE='false',
               DATABASE_URL='sqlite:///' + (RUNTIME / 'local.db').as_posix(),
               DATA_DIR=str(RUNTIME / 'local-data'), MODEL_PATH=str(ROOT / 'models/yolo11s-pose.pt'),
               APP_USERNAME=config['username'], APP_PASSWORD=config['password'], PYTHONUNBUFFERED='1')
    inference = config.get('inference', {})
    if not isinstance(inference, dict) or set(inference)-INFERENCE_DEFAULTS.keys():
        raise RuntimeError('local-settings.json contains unsupported inference settings.')
    env.update({key:str(value) for key,value in inference.items()})
    return config, env


def inference_setting(env, key):
    """Compare effective values; absent settings retain the backend defaults."""
    default = INFERENCE_DEFAULTS[key]
    value = str(env.get(key, default)).strip().lower()
    if isinstance(default, bool):
        if value in ('1', 'true', 'yes'):
            return True
        if value in ('0', 'false', 'no'):
            return False
    elif isinstance(default, int):
        try:
            return int(value)
        except ValueError:
            pass  # Startup validation reports invalid settings, never hide them.
    elif key in ('REALTIME_BACKEND', 'POSE_BACKEND'):
        return value or default
    elif key in ('INFERENCE_DEVICE', 'TRAINING_DEVICE'):
        index = value.removeprefix('cuda:')
        if index.isdecimal():
            return 'cuda:' + str(int(index))
    return value


def verify_environment(found, env):
    for processes in found.values():
        for process in processes:
            try:
                current = process.environ()
            except psutil.Error as exc:
                raise RuntimeError('Cannot verify the existing local service configuration; use restart.') from exc
            if any(current.get(key) != env[key] for key in ('DATABASE_URL', 'DATA_DIR', 'APP_USERNAME')):
                raise RuntimeError('Existing project services use a different database/data configuration. '
                                   'Use npm run restart:local to switch them to the local configuration.')
            changed = [key for key in INFERENCE_DEFAULTS
                       if inference_setting(current, key) != inference_setting(env, key)]
            if changed:
                raise RuntimeError('Existing project services use different inference/workload settings ('
                                   + ', '.join(changed) + '). Use npm run restart:local to apply them.')


def spawn_service(name, env):
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    with (RUNTIME / f'local-{name}.log').open('ab') as log:
        return subprocess.Popen([sys.executable, *SERVICE_ARGS[name]], cwd=ROOT, env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                creationflags=flags, start_new_session=os.name != 'nt')


def terminate_service(process):
    # psutil verifies Process creation times when signalling to guard PID reuse.
    tree = process_tree(process)
    for child in reversed(tree):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(tree, timeout=10)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    if alive:
        raise RuntimeError('Timed out waiting for the old local services to stop.')


def wait_ready(created, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in created.values()):
            raise RuntimeError('A local service exited; see .runtime/local-api.log and local-worker.log.')
        try:
            read_health()
            read_readiness()
            time.sleep(.5)
            if all(process.poll() is None for process in created.values()):
                return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(.25)
    raise RuntimeError('API did not become ready; see .runtime/local-api.log.')


def save_processes(found):
    identities = {name: [{'pid': process.pid, 'created': process.create_time()} for process in processes]
                  for name, processes in found.items()}
    record = {name: entries[0]['pid'] if entries else None for name, entries in identities.items()}
    record.update(root=str(ROOT.resolve()), identities=identities, updated_at=time.time(), url=BASE_URL)
    temporary = RUNTIME / 'local-processes.json.tmp'
    temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
    temporary.replace(RUNTIME / 'local-processes.json')


def backup_local_database():
    """SQLite backup API includes committed WAL pages; plain file copies do not."""
    source = RUNTIME / 'local.db'
    folder = RUNTIME / 'database-backups'
    folder.mkdir(exist_ok=True)
    target = folder / ('local-before-upgrade-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4) + '.db')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(target)) as backup:
            original.backup(backup)
            if backup.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Database backup failed integrity verification; upgrade was cancelled.')
    print('Database backup: ' + str(target))
    return target


def start_local(restart=False):
    with startup_lock():
        found = services()
        occupied = verify_port_ownership(found)
        config, env = local_environment()
        # Validate dependencies and data without DDL before stopping services.
        subprocess.run([sys.executable, '-c', 'import backend.app, backend.worker, uvicorn'],
                       cwd=ROOT, env=env, check=True)
        migration_pending = False
        if (RUNTIME / 'local.db').exists():
            preflight = subprocess.run([sys.executable, '-m', 'backend.init_db', '--check'], cwd=ROOT, env=env)
            if preflight.returncode not in (0, 2):
                raise RuntimeError('Database preflight failed; existing services were left running. See diagnostics above.')
            migration_pending = preflight.returncode == 2
        if migration_pending and any(found.values()) and not restart:
            raise RuntimeError('Database migration requires stopped services. Use npm run restart:local to back up and upgrade.')
        if restart:
            for processes in found.values():
                for process in processes:
                    if process_role(process):
                        terminate_service(process)
            if port_in_use():
                raise RuntimeError('Port 8000 is still occupied. Startup was cancelled.')
            found, occupied = {'api': [], 'worker': []}, False
        else:
            verify_environment(found, env)
            if occupied:
                try:
                    read_health()
                    verify_login(config)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise RuntimeError('The existing API failed its health/login check. '
                                       'Use npm run restart:local; no running process was stopped.') from exc
            elif found['api']:
                raise RuntimeError('An API process is starting or is unhealthy without listening on 8000. '
                                   'Retry shortly, or use npm run restart:local.')
        if not any(found.values()):
            if migration_pending:
                backup_local_database()
            subprocess.run([sys.executable, '-m', 'backend.init_db'], cwd=ROOT, env=env, check=True)
        created, identities = {}, {}
        try:
            for name in ('api', 'worker'):
                if not found[name]:
                    created[name] = spawn_service(name, env)
                    identities[name] = psutil.Process(created[name].pid)
            wait_ready(created)
            if not occupied:
                verify_login(config)
            found = services()
            if not found['api'] or not found['worker']:
                raise RuntimeError('API/Worker process verification failed; see local service logs.')
            verify_port_ownership(found)
            save_processes(found)
        except Exception:
            # Failed repair must leave previously running services alone.
            for name, process in created.items():
                if process.poll() is None and name in identities:
                    terminate_service(identities[name])
            raise
        print('Ready: ' + BASE_URL)
        print('Started: ' + (', '.join(created) if created else 'none; existing API and Worker verified'))
        print('Credentials: ' + str(RUNTIME / 'local-settings.json'))
        print('Logs: ' + str(RUNTIME / 'local-api.log') + ' and local-worker.log')


def read_readiness():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE_URL + '/api/v1/ready', timeout=3) as response:
        result = json.load(response)
    if result.get('status') != 'ready':
        raise RuntimeError('API or Worker is not ready')
    return result


def status_local():
    found = services()
    payload = {'url': BASE_URL, 'api': [p.pid for p in found['api']], 'worker': [p.pid for p in found['worker']]}
    try:
        owned = verify_port_ownership(found)
        payload['apiReady'] = bool(owned and read_health().get('status') == 'ready')
        payload['workerReady'] = bool(payload['apiReady'] and found['worker'] and read_readiness().get('status') == 'ready')
        payload['status'] = 'ready' if payload['apiReady'] and payload['workerReady'] else 'degraded'
    except (OSError, ValueError, RuntimeError, psutil.Error) as exc:
        payload.update(status='degraded', apiReady=False, error=str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload['status'] == 'ready' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--restart', action='store_true', help='restart only verified services belonging to this project')
    group.add_argument('--status', action='store_true', help='read API health and Worker process status without changes')
    args = parser.parse_args()
    if args.status:
        return status_local()
    start_local(restart=args.restart)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError, psutil.Error) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
