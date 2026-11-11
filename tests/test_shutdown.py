import asyncio
import threading

import pytest
from starlette.concurrency import run_in_threadpool

from backend.shutdown import close_in_order


def test_shutdown_attempts_later_steps_before_raising_first_error():
    async def scenario():
        called = []
        async def failing():
            called.append('model')
            raise ValueError('model cleanup failed')
        async def later():
            called.append('evidence')
            raise RuntimeError('evidence cleanup failed')
        async def last():
            called.append('database')
        with pytest.raises(ValueError, match='model cleanup failed'):
            await close_in_order([('model', failing), ('evidence', later), ('database', last)])
        assert called == ['model', 'evidence', 'database']
    asyncio.run(scenario())


def test_cancelled_lifespan_waits_for_real_thread_and_remaining_cleanup():
    async def scenario():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        called = []
        def model_close():
            entered.set()
            assert release.wait(3)
            finished.set()
        async def last():
            assert finished.is_set()
            called.append('database')
        task = asyncio.create_task(close_in_order([
            ('model', lambda: run_in_threadpool(model_close)), ('database', last)]))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            assert not called
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert called == ['database']
    asyncio.run(scenario())


def test_cancelled_cleanup_step_does_not_skip_later_steps():
    async def scenario():
        called = []
        async def cancelled():
            raise asyncio.CancelledError
        async def last():
            called.append('database')
        with pytest.raises(asyncio.CancelledError):
            await close_in_order([('model', cancelled), ('database', last)])
        assert called == ['database']
    asyncio.run(scenario())
