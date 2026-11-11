"""CPU-only ASGI checks for admission ownership after caller cancellation."""
import asyncio
import threading
import uuid

from starlette.concurrency import run_in_threadpool

from backend.transport import RealtimeTransport, RealtimeTransportMiddleware


async def wait_until(predicate, timeout=2):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, 'Timed out waiting for test worker'
        await asyncio.sleep(.005)


async def request(middleware, kind, stream_id=None):
    scope = dict(type='http', asgi={'version':'3.0'}, http_version='1.1',
        method='POST', scheme='http', root_path='', path='/api/v1/realtime/frame',
        raw_path=b'/api/v1/realtime/frame', query_string=('analysis_type=' + kind).encode(),
        headers=[(b'content-type', b'image/jpeg'), (b'cookie', b'aisop_session=test-session'),
                 (b'x-stream-id', (stream_id or str(uuid.uuid4())).encode()),
                 (b'x-frame-time', b'0')], state={})
    messages = []

    async def receive():
        return {'type':'http.request', 'body':b'fixture', 'more_body':False}

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)
    return next(message['status'] for message in messages if message['type'] == 'http.response.start')


def slow_application():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def work():
        entered.set()
        try:
            assert release.wait(5), 'Test did not release the model worker'
        finally:
            finished.set()

    async def app(scope, receive, send):
        calls.append(scope['query_string'])
        await run_in_threadpool(work)
        await send({'type':'http.response.start', 'status':200, 'headers':[]})
        await send({'type':'http.response.body', 'body':b'{}'})

    return app, entered, release, finished, calls


def test_cancelled_http_waiter_keeps_shared_capacity_until_worker_returns():
    async def scenario():
        # Both kinds have spare per-kind capacity; only the shared total is full.
        transport = RealtimeTransport(2, 1, scene_capacity=2, total_capacity=1)
        app, entered, release, finished, calls = slow_application()
        middleware = RealtimeTransportMiddleware(app, transport)
        first = asyncio.create_task(request(middleware, 'scene'))
        try:
            await wait_until(entered.is_set)
            first.cancel()
            await asyncio.sleep(0)
            assert not finished.is_set()
            assert transport.snapshot()['active'] == 1
            assert transport.snapshot()['services']['scene']['active'] == 1
            assert await request(middleware, 'pose') == 429
            assert len(calls) == 1, 'A different kind bypassed the shared in-flight budget'
            assert not finished.is_set()
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await wait_until(lambda: finished.is_set() and transport.snapshot()['active'] == 0)
            assert transport.snapshot()['services']['scene']['completed'] == 1
            assert await request(middleware, 'pose') == 200
            assert len(calls) == 2
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await asyncio.wait_for(transport.close(), timeout=2)
        assert transport.snapshot()['active'] == 0

    asyncio.run(scenario())


def test_transport_close_waits_for_worker_and_rejects_new_admission():
    async def scenario():
        transport = RealtimeTransport(2, 1, scene_capacity=2, total_capacity=2)
        app, entered, release, finished, calls = slow_application()
        middleware = RealtimeTransportMiddleware(app, transport)
        first = asyncio.create_task(request(middleware, 'scene'))
        closing = None
        try:
            await wait_until(entered.is_set)
            closing = asyncio.create_task(transport.close())
            await asyncio.sleep(0)
            assert not closing.done(), 'Shutdown released a still-running model worker'
            assert transport.snapshot()['active'] == 1 and not finished.is_set()
            # Total and per-kind capacity have room; shutdown must reject anyway.
            assert await request(middleware, 'pose') == 503
            assert len(calls) == 1
            assert not closing.done()
            release.set()
            assert await first == 200
            await asyncio.wait_for(closing, timeout=2)
            assert finished.is_set() and transport.snapshot()['active'] == 0
            assert await request(middleware, 'scene') == 503
            assert len(calls) == 1
            await asyncio.wait_for(transport.close(), timeout=2)
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            if closing is not None:
                await asyncio.wait_for(closing, timeout=2)
            else:
                await asyncio.wait_for(transport.close(), timeout=2)

    asyncio.run(scenario())
