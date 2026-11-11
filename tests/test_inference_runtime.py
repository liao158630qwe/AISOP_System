from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend import inference_runtime as runtime
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def cpu_runtime(monkeypatch):
    events = []
    cv = SimpleNamespace(threads=16)

    def set_threads(value):
        events.append(('set', value))
        cv.threads = value

    cv.setNumThreads = set_threads
    cv.getNumThreads = lambda: cv.threads

    def load(name):
        if name == 'cv2':
            return cv
        assert name == 'ultralytics.utils'
        events.append(('import', name))
        cv.threads = 1  # The dependency resets this process-wide setting.

    monkeypatch.setattr(runtime, '_configuration', None)
    monkeypatch.setattr(runtime.importlib, 'import_module', load)
    monkeypatch.setattr(runtime.os, 'cpu_count', lambda: 8)
    return events, cv


def test_budget_survives_dependency_import_and_repeated_startup(cpu_runtime):
    events, cv = cpu_runtime
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(runtime.configure_inference_runtime, [4] * 8)) == [4] * 8
    assert events == [('import', 'ultralytics.utils'), ('set', 4)]
    with pytest.raises(RuntimeError, match='restart'):
        runtime.configure_inference_runtime(1)
    assert cv.threads == 4


def test_budget_is_capped_on_small_cpu(cpu_runtime, monkeypatch):
    monkeypatch.setattr(runtime.os, 'cpu_count', lambda: 2)
    assert runtime.configure_inference_runtime(4) == 2


def test_missing_optional_backend_keeps_service_initialization_available(cpu_runtime, monkeypatch, caplog):
    events, cv = cpu_runtime

    def missing(name):
        if name == 'cv2':
            return cv
        raise ImportError('optional backend unavailable')

    monkeypatch.setattr(runtime.importlib, 'import_module', missing)
    assert runtime.configure_inference_runtime(4) == 4
    assert 'optional backend unavailable' in caplog.text


@pytest.mark.parametrize('value', ['0', '-1', '17', '1.5', 'bad'])
def test_invalid_thread_environment_is_rejected(monkeypatch, value):
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('APP_PASSWORD', 'test-only-password')
    monkeypatch.setenv('OPENCV_THREADS', value)
    with pytest.raises(RuntimeError, match='OPENCV_THREADS'):
        Settings.from_env()


def test_cpu_configuration_precedes_api_warmup(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(runtime, 'configure_inference_runtime', lambda threads: events.append('configured') or threads)
    from backend import realtime
    from backend.sticker_realtime import RealtimeStickers
    from backend.evidence import EvidenceStore
    # This test makes warmup synchronous; isolate the continuous archive loop
    # from that global Thread patch and verify lifecycle ordering explicitly.
    monkeypatch.setattr(EvidenceStore, 'start', lambda self: events.append('evidence-start'))
    monkeypatch.setattr(EvidenceStore, 'close', lambda self: events.append('evidence-stop'))
    monkeypatch.setattr(realtime.RealtimeDetector, 'warmup', lambda self: events.append('warmup') or True)
    monkeypatch.setattr(RealtimeStickers, 'warmup', lambda self: events.append('sticker-warmup') or True)
    import backend.app as app_module

    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def join(self):
            events.append('warmup-joined')

    monkeypatch.setattr(app_module.threading, 'Thread', ImmediateThread)
    settings = Settings('sqlite://', tmp_path, 'test', 'test-only-password', tmp_path / 'absent.pt',
                        environment='development', secure_cookie=False, realtime_warmup=True)
    app = create_app(settings)
    from backend.db import initialize
    initialize(app.state.engine)
    assert events == []
    # Use the lifespan directly: patching Thread must not affect TestClient's portal.
    import asyncio

    async def startup():
        async with app.router.lifespan_context(app):
            assert events == ['configured', 'sticker-warmup', 'warmup', 'evidence-start']
            assert app.state.inference_runtime['opencvThreads'] == 4

    asyncio.run(startup())
    assert events[-1] == 'evidence-stop'
    assert events.index('warmup-joined') < events.index('evidence-stop')


def test_runtime_metrics_report_effective_budget(authenticated):
    _, app, client = authenticated
    assert client.get('/api/v1/metrics').json()['inferenceRuntime'] == app.state.inference_runtime
    assert 1 <= app.state.inference_runtime['opencvThreads'] <= 4
    assert 1 <= app.state.inference_runtime['torchThreads'] <= 4
    assert app.state.inference_runtime['torchInteropThreads'] == 1


def test_torch_thread_budget_is_once_per_process(monkeypatch):
    calls = []
    fake = SimpleNamespace(intra=40, inter=40)
    def intra(value):
        calls.append(('intra', value))
        fake.intra = value
    def inter(value):
        calls.append(('inter', value))
        fake.inter = value
    fake.set_num_threads, fake.set_num_interop_threads = intra, inter
    fake.get_num_threads = lambda: fake.intra
    fake.get_num_interop_threads = lambda: fake.inter
    monkeypatch.setattr(runtime, '_torch_configuration', None)
    monkeypatch.setattr(runtime.importlib, 'import_module', lambda name: fake)
    monkeypatch.setattr(runtime.os, 'cpu_count', lambda: 2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(runtime.configure_torch_runtime, [4]*8))
    assert all(v == {'torchThreads': 2, 'torchInteropThreads': 1} for v in values)
    assert calls == [('inter', 1), ('intra', 2)]
    with pytest.raises(RuntimeError, match='restart'):
        runtime.configure_torch_runtime(8)


@pytest.mark.parametrize('value', ['0', '-1', '17', '1.5', 'bad'])
def test_invalid_torch_thread_environment_is_rejected(monkeypatch, value):
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('APP_PASSWORD', 'test-only-password')
    monkeypatch.setenv('TORCH_THREADS', value)
    with pytest.raises(RuntimeError, match='TORCH_THREADS'):
        Settings.from_env()
