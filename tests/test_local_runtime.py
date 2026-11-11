"""Local startup safety and API/Worker recovery without touching running services."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'tools' / 'start-local.py'
    spec = importlib.util.spec_from_file_location('aisop_local_launcher', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'RUNTIME', tmp_path / '.runtime')
    return module


def test_local_gpu_settings_survive_restart_without_overriding_service_identity(launcher):
    launcher.RUNTIME.mkdir()
    path=launcher.RUNTIME/'local-settings.json'
    path.write_text(json.dumps({'username':'test-user','password':'test-secret',
        'inference':{'REALTIME_POOL_SIZE':3,'SPECIALIST_POOL_SIZE':3,'INFERENCE_DEVICE':'cuda:0'}}),encoding='utf-8')
    _,env=launcher.local_environment()
    assert env['REALTIME_POOL_SIZE']==env['SPECIALIST_POOL_SIZE']=='3'
    assert env['INFERENCE_DEVICE']=='cuda:0' and env['APP_USERNAME']=='test-user'
    value=json.loads(path.read_text(encoding='utf-8'));value['inference']['DATABASE_URL']='sqlite:///other.db'
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(RuntimeError,match='unsupported'):
        launcher.local_environment()


def test_local_workload_and_capacity_settings_reach_both_services(launcher, monkeypatch):
    launcher.RUNTIME.mkdir()
    path = launcher.RUNTIME / 'local-settings.json'
    configured = {'WORKER_ANALYSIS_ENABLED': False, 'WORKER_TRAINING_ENABLED': False,
                  'SCENE_MODEL_CACHE_SIZE': 3, 'STATION_QUEUE_WAIT_MS': 750}
    path.write_text(json.dumps({'username': 'test-user', 'password': 'test-secret',
                               'inference': configured}), encoding='utf-8')
    original = path.read_bytes()
    spawned, _, processes = harness(launcher, monkeypatch, {'api': [], 'worker': []})
    launcher.start_local()
    assert spawned == ['api', 'worker']
    assert path.read_bytes() == original
    for rows in processes.values():
        for key, value in configured.items():
            assert rows[0].environment[key] == str(value)
        assert rows[0].environment['APP_USERNAME'] == 'test-user'


def test_local_omitted_workload_settings_keep_inherited_environment(launcher, monkeypatch):
    launcher.RUNTIME.mkdir()
    (launcher.RUNTIME / 'local-settings.json').write_text(
        json.dumps({'username': 'test-user', 'password': 'test-secret'}), encoding='utf-8')
    monkeypatch.setenv('WORKER_ANALYSIS_ENABLED', 'false')
    monkeypatch.delenv('WORKER_TRAINING_ENABLED', raising=False)
    monkeypatch.delenv('SCENE_MODEL_CACHE_SIZE', raising=False)
    monkeypatch.delenv('STATION_QUEUE_WAIT_MS', raising=False)
    _, env = launcher.local_environment()
    assert env['WORKER_ANALYSIS_ENABLED'] == 'false'
    assert not {'WORKER_TRAINING_ENABLED', 'SCENE_MODEL_CACHE_SIZE', 'STATION_QUEUE_WAIT_MS'} & env.keys()


class Process:
    def __init__(self, root, role, pid, parent=0):
        self.root, self.role, self.pid, self.parent = root, role, pid, parent
        self.environment = {}
        self.extra_args = []
        self.exit_code = None

    def cwd(self):
        return str(self.root)

    def cmdline(self):
        module = ['uvicorn', 'backend.app:create_app', '--port', '8000'] if self.role == 'api' else ['backend.worker']
        return ['python.exe', '-m', *module, *self.extra_args]

    def ppid(self):
        return self.parent

    def children(self, recursive=False):
        return []

    def create_time(self):
        return 123.5

    def environ(self):
        return self.environment

    def poll(self):
        return self.exit_code


def harness(module, monkeypatch, initial):
    spawned, terminated = [], []
    final = {'api': list(initial['api']), 'worker': list(initial['worker'])}
    monkeypatch.setattr(module, 'services', lambda: final)
    monkeypatch.setattr(module, 'verify_port_ownership', lambda found: bool(found['api']))
    monkeypatch.setattr(module, 'read_health', lambda: {'status': 'ready'})
    monkeypatch.setattr(module, 'read_readiness', lambda: {'status': 'ready'})
    monkeypatch.setattr(module, 'verify_login', lambda config: None)
    monkeypatch.setattr(module, 'verify_environment', lambda found, env: None)
    monkeypatch.setattr(module, 'wait_ready', lambda created: None)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module, 'terminate_service', lambda process: terminated.append(process.pid))
    monkeypatch.setattr(module.psutil, 'Process', lambda pid: next(p for values in final.values() for p in values if p.pid == pid))

    def spawn(name, env):
        process = Process(module.ROOT, name, 100 + len(spawned))
        process.environment = dict(env)
        spawned.append(name)
        final[name].append(process)
        return process

    monkeypatch.setattr(module, 'spawn_service', spawn)
    return spawned, terminated, final


def test_module_cwd_and_port_establish_ownership(launcher, tmp_path):
    api = Process(tmp_path, 'api', 1)
    assert launcher.process_role(api) == 'api'
    assert launcher.process_role(Process(tmp_path / 'different-project', 'api', 2)) is None
    worker = Process(tmp_path, 'worker', 3)
    worker.extra_args = ['--once']
    assert launcher.process_role(worker) is None
    api.cmdline = lambda: ['python.exe', '-m', 'uvicorn', 'backend.app:create_app', '--port', '8765']
    assert launcher.process_role(api) is None


def test_windows_launcher_child_is_not_duplicate_worker(launcher, monkeypatch):
    root = Process(launcher.ROOT, 'worker', 10)
    child = Process(launcher.ROOT, 'worker', 11, parent=10)
    unrelated = Process(launcher.ROOT / 'other', 'worker', 12)
    monkeypatch.setattr(launcher.psutil, 'process_iter', lambda: iter([root, child, unrelated]))
    assert launcher.services() == {'api': [], 'worker': [root]}


def test_foreign_port_is_rejected_before_mutations(launcher, monkeypatch):
    monkeypatch.setattr(launcher, 'services', lambda: {'api': [], 'worker': []})
    monkeypatch.setattr(launcher, 'port_in_use', lambda: True)
    monkeypatch.setattr(launcher, 'port_owners', lambda: {777})
    monkeypatch.setattr(launcher, 'local_environment', lambda: pytest.fail('must not create credentials'))
    monkeypatch.setattr(launcher, 'terminate_service', lambda p: pytest.fail('must not terminate foreign services'))
    with pytest.raises(RuntimeError, match='unverified service'):
        launcher.start_local(restart=True)


def test_start_recovers_missing_worker_only(launcher, monkeypatch):
    spawned, terminated, final = harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)], 'worker': []})
    launcher.start_local()
    assert spawned == ['worker']
    assert terminated == []
    record = json.loads((launcher.RUNTIME / 'local-processes.json').read_text())
    assert record['api'] == 2
    assert record['worker'] == final['worker'][0].pid
    assert record['identities']['api'] == [{'pid': 2, 'created': 123.5}]
    assert 'password' not in record


def test_start_recovers_missing_api_without_duplicate_worker(launcher, monkeypatch):
    spawned, terminated, _ = harness(launcher, monkeypatch, {'api': [], 'worker': [Process(launcher.ROOT, 'worker', 2)]})
    launcher.start_local()
    assert spawned == ['api']
    assert terminated == []


def test_repeated_start_leaves_healthy_pair_running(launcher, monkeypatch):
    initial = {name: [Process(launcher.ROOT, name, index)] for index, name in enumerate(('api', 'worker'), 10)}
    spawned, terminated, _ = harness(launcher, monkeypatch, initial)
    launcher.start_local()
    launcher.start_local()
    assert spawned == terminated == []


def test_failed_worker_repair_preserves_api(launcher, monkeypatch):
    spawned, terminated, _ = harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)], 'worker': []})
    def fail(created):
        raise RuntimeError('worker failed startup')
    monkeypatch.setattr(launcher, 'wait_ready', fail)
    with pytest.raises(RuntimeError, match='worker failed'):
        launcher.start_local()
    assert spawned == ['worker']
    assert terminated == [100]
    assert not (launcher.RUNTIME / 'local-processes.json').exists()


def test_unhealthy_existing_api_does_not_spawn_or_stop(launcher, monkeypatch):
    spawned, terminated, _ = harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)], 'worker': []})
    def fail():
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(launcher, 'read_health', fail)
    with pytest.raises(RuntimeError, match='health/login check'):
        launcher.start_local()
    assert spawned == terminated == []


def test_dependency_failure_before_restart_keeps_existing_services(launcher, monkeypatch):
    spawned, terminated, _ = harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)], 'worker': []})
    def fail(*args, **kwargs):
        raise RuntimeError('dependency missing')
    monkeypatch.setattr(launcher.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='dependency missing'):
        launcher.start_local(restart=True)
    assert spawned == terminated == []


def test_pending_migration_never_runs_ddl_against_live_services(launcher, monkeypatch):
    spawned, terminated, _ = harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)], 'worker': []})
    launcher.RUNTIME.mkdir()
    (launcher.RUNTIME / 'local.db').write_bytes(b'preflight is mocked')
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=2 if '--check' in args else 0)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='migration requires stopped services'):
        launcher.start_local()
    assert spawned == terminated == []
    assert all('--check' in args for args in commands if 'backend.init_db' in args)


def test_invalid_preflight_does_not_stop_services(launcher, monkeypatch):
    spawned, terminated, _ = harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)], 'worker': []})
    launcher.RUNTIME.mkdir()
    (launcher.RUNTIME / 'local.db').write_bytes(b'preflight is mocked')
    monkeypatch.setattr(launcher.subprocess, 'run', lambda args, **kwargs: SimpleNamespace(returncode=1 if '--check' in args else 0))
    with pytest.raises(RuntimeError, match='preflight failed'):
        launcher.start_local(restart=True)
    assert spawned == terminated == []


def test_restart_orders_preflight_stop_backup_upgrade_and_start(launcher, monkeypatch):
    initial = {name: [Process(launcher.ROOT, name, index)] for index, name in enumerate(('api', 'worker'), 10)}
    _, _, final = harness(launcher, monkeypatch, initial)
    launcher.RUNTIME.mkdir()
    (launcher.RUNTIME / 'local.db').write_bytes(b'preflight is mocked')
    events = []
    def run(args, **kwargs):
        if '--check' in args:
            events.append('check')
            return SimpleNamespace(returncode=2)
        if 'backend.init_db' in args:
            events.append('upgrade')
        return SimpleNamespace(returncode=0)
    def terminate(process):
        events.append('stop-' + process.role)
        final[process.role].remove(process)
    spawn = launcher.spawn_service
    def start(name, env):
        events.append('start-' + name)
        return spawn(name, env)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher, 'terminate_service', terminate)
    monkeypatch.setattr(launcher, 'port_in_use', lambda: False)
    monkeypatch.setattr(launcher, 'backup_local_database', lambda: events.append('backup'))
    monkeypatch.setattr(launcher, 'spawn_service', start)
    launcher.start_local(restart=True)
    assert events == ['check', 'stop-api', 'stop-worker', 'backup', 'upgrade', 'start-api', 'start-worker']


def test_backup_includes_committed_wal_pages(launcher):
    import sqlite3
    from contextlib import closing
    launcher.RUNTIME.mkdir()
    source = launcher.RUNTIME / 'local.db'
    with closing(sqlite3.connect(source)) as original:
        original.execute('PRAGMA journal_mode=WAL')
        original.execute('PRAGMA wal_autocheckpoint=0')
        original.execute('CREATE TABLE records (value TEXT)')
        original.execute("INSERT INTO records VALUES ('committed')")
        original.commit()
        assert Path(str(source) + '-wal').stat().st_size > 0
        target = launcher.backup_local_database()
        with closing(sqlite3.connect(target)) as backup:
            assert backup.execute('SELECT value FROM records').fetchall() == [('committed',)]
            assert backup.execute('PRAGMA quick_check').fetchone() == ('ok',)


def test_existing_worker_must_use_same_database(launcher):
    worker = Process(launcher.ROOT, 'worker', 4)
    worker.environment = {'DATABASE_URL': 'sqlite:///other.db', 'DATA_DIR': 'data', 'APP_USERNAME': 'admin'}
    expected = dict(worker.environment, DATABASE_URL='sqlite:///local.db')
    with pytest.raises(RuntimeError, match='different database'):
        launcher.verify_environment({'worker': [worker]}, expected)


@pytest.mark.parametrize('key,value', [
    ('INFERENCE_DEVICE', 'cuda:1'), ('TRAINING_DEVICE', 'cuda:1'),
    ('REALTIME_BACKEND', 'rtmo'), ('POSE_BACKEND', 'rtmpose'), ('REALTIME_POOL_SIZE', '3'),
    ('SPECIALIST_POOL_SIZE', '3'), ('STATION_INFERENCE_CONCURRENCY', '3'),
    ('OPENCV_THREADS', '8'), ('TORCH_THREADS', '8'), ('SCENE_MODEL_CACHE_SIZE', '3'),
    ('STATION_QUEUE_WAIT_MS', '750'), ('WORKER_ANALYSIS_ENABLED', 'false'),
    ('WORKER_TRAINING_ENABLED', 'false'),
])
def test_existing_service_requires_restart_for_changed_inference_setting(launcher, key, value):
    process = Process(launcher.ROOT, 'worker', 4)
    process.environment = {'DATABASE_URL': 'sqlite:///local.db', 'DATA_DIR': 'data', 'APP_USERNAME': 'admin'}
    desired = dict(process.environment, **{key: value})
    with pytest.raises(RuntimeError, match='restart:local') as failure:
        launcher.verify_environment({'worker': [process]}, desired)
    assert key in str(failure.value)
    # Removing an explicit override also requires restart when it changes the effective value.
    process.environment, desired = desired, process.environment
    with pytest.raises(RuntimeError, match='restart:local'):
        launcher.verify_environment({'worker': [process]}, desired)


@pytest.mark.parametrize('explicit_current', [False, True])
def test_missing_inference_values_match_explicit_backend_defaults(launcher, explicit_current):
    process = Process(launcher.ROOT, 'api', 4)
    identity = {'DATABASE_URL': 'sqlite:///local.db', 'DATA_DIR': 'data', 'APP_USERNAME': 'admin'}
    explicit = dict(identity, **{key: str(value) for key, value in launcher.INFERENCE_DEFAULTS.items()})
    explicit.update(INFERENCE_DEVICE='0', TRAINING_DEVICE='cuda:00', WORKER_ANALYSIS_ENABLED='yes',
                    WORKER_TRAINING_ENABLED='1', REALTIME_POOL_SIZE='02', POSE_BACKEND=' YOLO ')
    process.environment = explicit if explicit_current else identity
    launcher.verify_environment({'api': [process]}, identity if explicit_current else explicit)


def test_launcher_backend_defaults_match_settings(launcher, monkeypatch):
    from backend.config import Settings
    for key in launcher.INFERENCE_DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
    monkeypatch.setenv('APP_PASSWORD', 'test-only')
    settings = Settings.from_env()
    fields = {'POSE_BACKEND': 'pose_backend', 'REALTIME_BACKEND': 'realtime_backend',
              'REALTIME_POOL_SIZE': 'realtime_pool_size', 'SPECIALIST_POOL_SIZE': 'specialist_pool_size',
              'STATION_INFERENCE_CONCURRENCY': 'station_inference_concurrency',
              'OPENCV_THREADS': 'opencv_threads', 'TORCH_THREADS': 'torch_threads',
              'SCENE_MODEL_CACHE_SIZE': 'scene_model_cache_size', 'STATION_QUEUE_WAIT_MS': 'station_queue_wait_ms',
              'WORKER_ANALYSIS_ENABLED': 'worker_analysis_enabled', 'WORKER_TRAINING_ENABLED': 'worker_training_enabled'}
    for key, field in fields.items():
        assert launcher.inference_setting({}, key) == getattr(settings, field)


def test_status_reports_missing_worker_as_degraded_without_config_writes(launcher, monkeypatch, capsys):
    harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)], 'worker': []})
    assert launcher.status_local() == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'degraded'
    assert not launcher.RUNTIME.exists()


def test_startup_lock_blocks_parallel_start_and_releases(launcher):
    with launcher.startup_lock():
        with pytest.raises(RuntimeError, match='in progress'):
            with launcher.startup_lock():
                pytest.fail('two launchers must not run concurrently')
    with launcher.startup_lock():
        pass

def test_status_does_not_trust_worker_pid_without_heartbeat(launcher, monkeypatch, capsys):
    harness(launcher, monkeypatch, {'api': [Process(launcher.ROOT, 'api', 2)],
                                  'worker': [Process(launcher.ROOT, 'worker', 3)]})
    monkeypatch.setattr(launcher, 'read_readiness', lambda: {'status': 'unavailable'})
    assert launcher.status_local() == 1
    status = json.loads(capsys.readouterr().out)
    assert status['apiReady'] is True and status['workerReady'] is False
