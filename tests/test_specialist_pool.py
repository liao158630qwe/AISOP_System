"""Replica concurrency, evidence continuity and release under overload."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest
from fastapi import HTTPException

from backend.specialist_pool import SpecialistPool, create_specialist
from backend.streams import StreamRegistry
from backend.station_scheduler import StationScheduler


class Specialist:
    def __init__(self):
        self.registry = StreamRegistry()
        self.detector = None

    def predict(self, payload, *, stream_key, frame_time, entered=None, gate=None, fail=False):
        self.detector = object()
        if entered:
            entered.set()
        if gate:
            assert gate.wait(5)
        if fail:
            raise RuntimeError('inference failed')
        state = self.registry.get(stream_key, frame_time, 'unchanged-model')
        with state['lock']:
            state.setdefault('samples', []).append(frame_time)
            return list(state['samples'])

    def finish(self, key):
        return list(self.registry.items[key]['samples'])


def test_replicas_keep_history_when_stream_moves_to_another_slot():
    pool = SpecialistPool(Specialist, 2)
    key = ('owner', 'stream')
    assert pool.predict(b'', stream_key=key, frame_time=0) == [0]
    # Hold the slot used by frame 0 with a different stream; frame 1 must use
    # another replica and still see frame 0's history and timestamp contract.
    entered, gate = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        other = executor.submit(pool.predict, b'', stream_key=('owner', 'other'),
            frame_time=0, entered=entered, gate=gate)
        assert entered.wait(2)
        try:
            assert pool.predict(b'', stream_key=key, frame_time=1) == [0, 1]
            with pytest.raises(HTTPException) as error:
                pool.predict(b'', stream_key=key, frame_time=.5)
            assert error.value.status_code == 409
            assert pool.finish(key) == [0, 1]
        finally:
            gate.set()
        other.result()
    assert pool.snapshot() == dict(capacity=2, available=2, loaded=2)
    pool.release(key)
    assert all(key not in slot.registry.items for slot in pool.slots)


def test_overload_is_bounded_and_failed_work_returns_slot():
    pool = SpecialistPool(Specialist, 2)
    entered = [threading.Event(), threading.Event()]
    gate = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        work = [executor.submit(pool.predict, b'', stream_key=('owner', str(i)),
            frame_time=0, entered=entered[i], gate=gate, fail=i == 0) for i in range(2)]
        assert all(event.wait(2) for event in entered)
        try:
            with pytest.raises(HTTPException) as error:
                pool.predict(b'', stream_key=('owner', 'overflow'), frame_time=0)
            assert error.value.status_code == 429
            assert not pool.registry.items
        finally:
            gate.set()
        with pytest.raises(RuntimeError, match='inference failed'):
            work[0].result()
        assert work[1].result() == [0]
    assert pool.available.qsize() == 2


def test_single_slot_retains_existing_interface():
    assert isinstance(create_specialist(Specialist, 1), Specialist)


def test_scheduler_uses_same_group_replicas_without_reordering_station():
    async def scenario():
        scheduler = StationScheduler(2000, concurrency=6, group_limits={'sticker': 2})
        entered = []
        gate = asyncio.Event()
        async def work(name, wait):
            entered.append(name)
            await gate.wait()
            return name
        first = asyncio.create_task(scheduler.submit('station-1', lambda w: work('first', w), 'sticker'))
        second = asyncio.create_task(scheduler.submit('station-2', lambda w: work('second', w), 'sticker'))
        for _ in range(100):
            if len(entered) == 2:
                break
            await asyncio.sleep(.001)
        later = asyncio.create_task(scheduler.submit('station-1', lambda w: work('later', w), 'sticker'))
        third = asyncio.create_task(scheduler.submit('station-3', lambda w: work('third', w), 'sticker'))
        await asyncio.sleep(.01)
        assert entered == ['first', 'second']
        assert scheduler.snapshot()['activeGroups'] == {'sticker': 2}
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert scheduler.active_groups == {'sticker': 2}
        gate.set()
        await asyncio.gather(second, later, third)
        await scheduler.close()
        assert entered == ['first', 'second', 'later', 'third']
        assert scheduler.active_groups == {} and not scheduler.tasks
    asyncio.run(scenario())


@pytest.mark.parametrize('value', [0, 7, True, 1.5])
def test_invalid_replica_capacity_is_rejected(value):
    with pytest.raises(ValueError):
        SpecialistPool(Specialist, value)
    with pytest.raises(ValueError):
        StationScheduler(group_limits={'sticker': value})


def test_app_aligns_admission_scheduler_and_replica_capacities(context):
    from backend.app import create_app
    settings, _, _ = context
    app = create_app(replace(settings, specialist_pool_size=2, realtime_pool_size=4,
                             station_inference_concurrency=6))
    assert app.state.terminals.scheduler.concurrency == 4
    assert app.state.terminals.scheduler.group_limits == dict(pose=4, screw=2, sticker=2, scene=1)
    assert app.state.realtime_transport.capacities == dict(pose=4, screw=2, sticker=2, scene=1)
    assert app.state.screw_realtime.registry is app.state.screw_realtime.slots[1].registry
    assert app.state.sticker_realtime.snapshot()['loaded'] == 0
    app.state.engine.dispose()


def test_six_scene_cache_slots_do_not_expand_the_shared_execution_budget(context):
    from backend.app import create_app
    settings, _, _ = context
    app = create_app(replace(settings, scene_model_cache_size=6, realtime_pool_size=3,
                             station_inference_concurrency=2))
    try:
        assert app.state.scene_realtime.model_cache_size == 6
        assert app.state.scene_realtime.concurrency == 2
        assert app.state.terminals.scheduler.concurrency == 2
        assert app.state.terminals.scheduler.group_limits['scene'] == 2
        assert app.state.realtime_transport.capacities['scene'] == 2
        assert app.state.realtime_transport.total_capacity == 2
        assert app.state.realtime_transport.snapshot()['capacity'] == 2
    finally:
        app.state.scene_realtime.close()
        app.state.engine.dispose()


@pytest.mark.parametrize('value', ['0', '7', '1.5', 'bad'])
def test_invalid_pool_environment_is_rejected(monkeypatch, value):
    from backend.config import Settings
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('APP_PASSWORD', 'test-only-password')
    monkeypatch.setenv('SPECIALIST_POOL_SIZE', value)
    with pytest.raises(RuntimeError):
        Settings.from_env()
