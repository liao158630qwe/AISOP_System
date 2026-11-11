"""Bound realtime HTTP admission before the shared ASGI thread pool is used."""
from collections import OrderedDict, deque
from contextlib import contextmanager
import asyncio
import hashlib
import math
import threading
import time
import uuid
from fastapi import HTTPException

from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RealtimeTransport:
    def __init__(self, pose_capacity=2, specialist_capacity=1, *, scene_capacity=1, total_capacity=None):
        self.capacities = {'pose': pose_capacity, 'screw': specialist_capacity,
                           'sticker': specialist_capacity, 'scene': scene_capacity}
        if any(type(value) is not int or not 1 <= value <= 6 for value in self.capacities.values()):
            raise ValueError('Realtime service capacity must be 1–6')
        if total_capacity is not None and (type(total_capacity) is not int or not 1 <= total_capacity <= 6):
            raise ValueError('Total realtime capacity must be 1–6')
        self.total_capacity = sum(self.capacities.values()) if total_capacity is None else total_capacity
        self.lock = threading.Lock()
        self.closing = False
        self.requests = set()
        self.active = {kind: 0 for kind in self.capacities}
        self.pending = set()
        self.finished = OrderedDict()
        self.stats = {kind: {'completed': 0, 'failed': 0, 'rejected': 0, 'latencies': deque(maxlen=512)}
                      for kind in self.capacities}

    def enter(self, kind, key, timestamp):
        with self.lock:
            if self.closing:
                self.stats[kind]['rejected'] += 1
                return 503, '实时检测服务正在停止'
            last = self.finished.get(key) if key else None
            if last and time.monotonic() - last[1] < 300 and timestamp <= last[0]:
                self.stats[kind]['rejected'] += 1
                return 409, '采样已处理或时间戳已过期，请发送新采样'
            # Station streams and the generic realtime endpoint share this budget.
            if (sum(self.active.values()) >= self.total_capacity or
                    self.active[kind] >= self.capacities[kind] or (key and key in self.pending)):
                self.stats[kind]['rejected'] += 1
                return 429, '实时检测繁忙，请稍后发送最新采样'
            self.active[kind] += 1
            if key:
                self.pending.add(key)
            return None

    def leave(self, kind, key, timestamp, status, duration):
        with self.lock:
            self.active[kind] -= 1
            self.pending.discard(key)
            stats = self.stats[kind]
            stats['completed' if 200 <= status < 300 else 'failed'] += 1
            stats['latencies'].append(duration)
            if key and 200 <= status < 300:
                self.finished[key] = (timestamp, time.monotonic())
                self.finished.move_to_end(key)
                while len(self.finished) > 256:
                    self.finished.popitem(last=False)

    async def close(self):
        with self.lock:
            self.closing = True
        # Generic requests retain ownership after their HTTP waiter disconnects.
        if self.requests:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self.requests)),
                                 return_exceptions=True)

    def forget(self, token_hash, stream_id):
        with self.lock:
            for kind in self.capacities:
                self.finished.pop((kind, token_hash, stream_id), None)

    def is_pending(self, kind, token_hash, stream_id):
        with self.lock:
            return (kind, token_hash, stream_id) in self.pending

    @contextmanager
    def control(self, token_hash, stream_id, kinds=None):
        """Atomically exclude frames while finishing or releasing stream state."""
        keys = {(kind, token_hash, stream_id) for kind in (kinds or self.capacities)}
        with self.lock:
            if keys & self.pending:
                raise HTTPException(429, '当前采样或结束操作仍在处理，请稍后重试', headers={'Retry-After': '1'})
            self.pending.update(keys)
        try:
            yield
        finally:
            with self.lock:
                self.pending.difference_update(keys)

    def snapshot(self):
        with self.lock:
            result = {}
            for kind, stats in self.stats.items():
                values = sorted(stats['latencies'])
                def percentile(fraction):
                    return round(values[min(len(values)-1, math.ceil(len(values)*fraction)-1)], 2) if values else 0.0
                result[kind] = {name: stats[name] for name in ('completed', 'failed', 'rejected')}
                result[kind].update(active=self.active[kind], capacity=self.capacities[kind],
                    queueDepth=0, serverP95Ms=percentile(.95), serverP99Ms=percentile(.99),
                    serverMaxMs=round(max(values), 2) if values else 0.0)
            return {'policy': 'reject-busy-send-latest', 'services': result,
                    'capacity': self.total_capacity, 'active': sum(self.active.values())}


class RealtimeTransportMiddleware:
    def __init__(self, app, transport):
        self.app, self.transport = app, transport

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] != '/api/v1/realtime/frame' or scope['method'] != 'POST':
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        scope.setdefault('state', {})['realtime_received_at'] = time.time() * 1000
        scope['state']['realtime_started'] = started
        request = Request(scope)
        kind = request.query_params.get('analysis_type', 'pose')
        kind = kind if kind in self.transport.capacities else 'pose'
        key, timestamp = None, None
        # Only valid identities participate in ordering; the route validates malformed headers.
        try:
            stream_id = str(uuid.UUID(request.headers.get('x-stream-id', '')))
            timestamp = float(request.headers.get('x-frame-time', ''))
            token = request.cookies.get('aisop_session')
            if token and math.isfinite(timestamp) and 0 <= timestamp <= 86400:
                key = (kind, hashlib.sha256(token.encode()).hexdigest(), stream_id)
        except (TypeError, ValueError, AttributeError):
            pass
        status = 500

        async def timed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                headers = MutableHeaders(scope=message)
                headers['Server-Timing'] = f'realtime;dur={(time.perf_counter()-started)*1000:.2f}'
                headers['Cache-Control'] = 'no-store'
                headers['X-Content-Type-Options'] = 'nosniff'
                if status in (429, 503):
                    headers.setdefault('Retry-After', '1')
            await send(message)

        rejection = self.transport.enter(kind, key, timestamp)
        if rejection:
            await JSONResponse({'message': rejection[1]}, status_code=rejection[0])(scope, receive, timed_send)
            return
        async def admitted_request():
            try:
                await self.app(scope, receive, timed_send)
            finally:
                self.transport.leave(kind, key, timestamp, status, (time.perf_counter()-started)*1000)

        # A cancelled HTTP task cannot cancel a running native/CUDA worker.
        # Let the admitted request own its slot until that worker really ends.
        task = asyncio.create_task(admitted_request())
        self.transport.requests.add(task)
        def completed(task):
            self.transport.requests.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(completed)
        await asyncio.shield(task)


class TextCompressionMiddleware:
    """Compress large text, leaving realtime frames, media ranges and auth untouched."""
    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=2048, compresslevel=3)

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        compress = scope['type'] == 'http' and scope.get('method') == 'GET' and (
            not path.startswith('/api/') and (path in ('/', '/admin') or path.endswith(('.html', '.js', '.css')))
            or '/artifacts/' in path and path.endswith(('.json', '.html', '.csv')))
        await (self.compressed if compress else self.app)(scope, receive, send)
