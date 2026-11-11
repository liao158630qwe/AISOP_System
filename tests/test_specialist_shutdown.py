"""CPU fault injection with real specialist executors and person workers."""
from concurrent.futures import ThreadPoolExecutor
import importlib
from types import SimpleNamespace

import pytest

from backend.person_overlap import PersonWorker
from backend.specialist_lifecycle import SpecialistLifecycle
from backend.specialist_pool import SpecialistPool, close_specialist
from backend.streams import StreamRegistry


@pytest.fixture
def replicas(monkeypatch):
    monkeypatch.setattr('backend.person_overlap.record_frame_ready',
                        lambda: SimpleNamespace(synchronize=lambda: None))
    instances, cleanup = [], []
    class Replica:
        def __init__(self):
            self.registry = StreamRegistry()
            self.detector = object()
            self._lifecycle = SpecialistLifecycle()
            self._person_worker = PersonWorker()
            self._cuda_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='test-specialist')
            self._cuda_stream = SimpleNamespace(synchronize=lambda: None)
            # Start both actual Python workers; no CUDA object or kernel exists.
            self._cuda_executor.submit(lambda: None).result()
            with self._person_worker.run(lambda: None) as pending:
                pending.result()
            cleanup.extend((self._cuda_executor.shutdown, self._person_worker.executor.shutdown))
            instances.append(self)
        def stop_accepting(self): self._lifecycle.stop_accepting()
        def close(self): self._lifecycle.close(self)
    yield Replica
    # Retain unpatched shutdown methods so a failing assertion cannot leak test workers.
    for shutdown in cleanup:
        shutdown(wait=True)


def assert_closed(replica):
    assert replica._lifecycle._closed and replica._person_worker._close_complete
    assert replica.detector is None and replica._cuda_stream is None
    for executor in (replica._cuda_executor, replica._person_worker.executor):
        assert executor._shutdown and all(not worker.is_alive() for worker in executor._threads)


def test_executor_shutdown_error_still_closes_real_person_worker(replicas, monkeypatch):
    replica = replicas()
    original = replica._cuda_executor.shutdown
    failure = RuntimeError('executor shutdown failed')
    def fail_after_join(wait=True):
        original(wait=wait)
        raise failure
    monkeypatch.setattr(replica._cuda_executor, 'shutdown', fail_after_join)
    with pytest.raises(RuntimeError) as raised:
        replica.close()
    assert raised.value is failure
    assert_closed(replica)
    replica.close()


def test_pool_closes_every_replica_before_raising_first_cleanup_error(replicas, monkeypatch):
    pool = SpecialistPool(replicas, 3)
    first_error = RuntimeError('first stream synchronization failed')
    second_error = ValueError('second executor shutdown failed')
    def fail_sync(): raise first_error
    pool.slots[0]._cuda_stream.synchronize = fail_sync
    original = pool.slots[1]._cuda_executor.shutdown
    def fail_shutdown(wait=True):
        original(wait=wait)
        raise second_error
    monkeypatch.setattr(pool.slots[1]._cuda_executor, 'shutdown', fail_shutdown)
    with pytest.raises(RuntimeError) as raised:
        close_specialist(pool)
    assert raised.value is first_error
    for replica in pool.slots:
        assert_closed(replica)


def test_stop_error_does_not_skip_other_replicas_or_close_phase(replicas, monkeypatch):
    pool = SpecialistPool(replicas, 2)
    events = []
    failure = RuntimeError('stop admission failed')
    for index, replica in enumerate(pool.slots):
        stop, close = replica.stop_accepting, replica.close
        def stop_with_trace(index=index, stop=stop):
            events.append(('stop', index))
            stop()
            if index == 0:
                raise failure
        def close_with_trace(index=index, close=close):
            events.append(('close', index))
            close()
        monkeypatch.setattr(replica, 'stop_accepting', stop_with_trace)
        monkeypatch.setattr(replica, 'close', close_with_trace)
    with pytest.raises(RuntimeError) as raised:
        close_specialist(pool)
    assert raised.value is failure
    assert events == [('stop', 0), ('stop', 1), ('close', 0), ('close', 1)]
    for replica in pool.slots:
        assert_closed(replica)


def test_stopped_executor_rejection_still_releases_model_and_person_worker(replicas):
    replica = replicas()
    replica._cuda_executor.shutdown(wait=True)
    with pytest.raises(RuntimeError, match='cannot schedule new futures after shutdown'):
        replica.close()
    assert_closed(replica)


@pytest.mark.parametrize('kind', ['screw', 'sticker'])
@pytest.mark.parametrize('owned', [True, False])
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_specialist_closes_only_owned_pose_and_preserves_first_error(
        monkeypatch, kind, owned, cleanup_fails):
    module = importlib.import_module('backend.' + kind + '_realtime')
    pose_calls = []
    first_error = RuntimeError('specialist synchronization failed')
    pose_error = ValueError('owned pose cleanup failed')
    class Pose:
        def __init__(self, *args): pass
        def close(self):
            pose_calls.append('close')
            if cleanup_fails:
                raise pose_error
    monkeypatch.setattr('backend.realtime.RealtimeDetector', Pose)
    pose = None if owned else Pose()
    service = (module.RealtimeScrews if kind == 'screw' else module.RealtimeStickers)(pose)
    assert service._owns_pose_detector is owned
    assert isinstance(service.pose_detector, Pose)
    if not owned:
        assert service.pose_detector is pose
    service.detector = object()
    if cleanup_fails:
        def fail_sync(): raise first_error
        service._cuda_stream = SimpleNamespace(synchronize=fail_sync)
        with pytest.raises(RuntimeError) as raised:
            service.close()
        assert raised.value is first_error
    else:
        service.close()
    assert service.detector is None
    assert service._lifecycle._closed and service._person_worker._close_complete
    assert pose_calls == (['close'] if owned else [])
    service.close()
    assert pose_calls == (['close'] if owned else [])


@pytest.mark.parametrize('kind', ['screw', 'sticker'])
def test_owned_pose_failure_is_reported_after_successful_specialist_cleanup(monkeypatch, kind):
    module = importlib.import_module('backend.' + kind + '_realtime')
    error = RuntimeError('pose close failure')
    class Pose:
        def __init__(self, *args): pass
        def close(self): raise error
    monkeypatch.setattr('backend.realtime.RealtimeDetector', Pose)
    service = (module.RealtimeScrews if kind == 'screw' else module.RealtimeStickers)()
    with pytest.raises(RuntimeError) as raised:
        service.close()
    assert raised.value is error
    assert service._lifecycle._closed and service._person_worker._close_complete
