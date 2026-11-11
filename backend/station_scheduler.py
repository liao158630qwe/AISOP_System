"""One latest pending frame per station; FIFO turns across at most six stations.

The runner owns GPU work, so cancelling an HTTP waiter cannot free a running
slot early. Replacement keeps the original queue position (no starvation).
Optional resource keys serialize shared models without blocking other models.
"""
import asyncio
import time
from collections import OrderedDict, deque
from fastapi import HTTPException


class StationScheduler:
    def __init__(self, max_wait_ms=500, concurrency=1, group_limits=None):
        if type(concurrency) is not int or not 1 <= concurrency <= 6:
            raise ValueError('Station inference concurrency must be 1–6')
        self.group_limits = dict(group_limits or {})
        if any(type(limit) is not int or not 1 <= limit <= 6 for limit in self.group_limits.values()):
            raise ValueError('Station group concurrency must be 1–6')
        self.max_wait_ms = max_wait_ms
        self.concurrency = concurrency
        self.pending = OrderedDict()
        self.active_stations = set()
        self.active_groups = {}
        self.active_resources = set()
        self.tasks = set()
        self.changed = asyncio.Event()
        self.runner = None
        self.closed = False
        self.stats = {}

    def _stats(self, station):
        return self.stats.setdefault(station, dict(submitted=0, completed=0, replaced=0,
            expired=0, cancelled=0, failed=0, waits=deque(maxlen=512), latencies=deque(maxlen=512)))

    def busy(self, station):
        return station in self.active_stations or station in self.pending

    @property
    def active(self):
        if not self.active_stations:
            return None
        return sorted(self.active_stations)[0] if self.concurrency == 1 else sorted(self.active_stations)

    async def submit(self, station, work, group=None, *, received_at=None, resource=None):
        if self.closed:
            raise HTTPException(503, '工位推理服务正在停止')
        if resource is not None:
            hash(resource)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        # Retrieve exceptions even when the HTTP caller disconnects after dispatch.
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        stats = self._stats(station)
        stats['submitted'] += 1
        queued = time.monotonic()
        received = queued if received_at is None else received_at
        deadline = received + self.max_wait_ms / 1000
        if queued >= deadline:
            stats['expired'] += 1
            raise HTTPException(429, '采样接收已超时，请发送最新采样', headers={'Retry-After': '1'})
        if station in self.pending:
            old = self.pending[station]
            stats['replaced'] += 1
            if not old['future'].done():
                old['future'].set_exception(HTTPException(409, '待处理采样已被更新帧替换，请发送最新采样'))
        elif len(self.pending) >= 6:
            raise HTTPException(429, '六工位待处理容量已满', headers={'Retry-After': '1'})
        job = dict(future=future, work=work, queued=queued, received=received,
                   deadline=deadline, group=group or station, resource=resource)
        self.pending[station] = job
        self.changed.set()
        if self.runner is None or self.runner.done():
            self.runner = asyncio.create_task(self._run())
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if self.pending.get(station) is job:
                del self.pending[station]
                future.cancel()
                stats['cancelled'] += 1
                self.changed.set()
            raise

    async def _run(self):
        while self.pending or self.tasks:
            self.changed.clear()
            for station, job in list(self.pending.items()):
                wait = (time.monotonic() - job['queued']) * 1000
                if time.monotonic() >= job['deadline']:
                    del self.pending[station]
                    self._stats(station)['expired'] += 1
                    job['future'].set_exception(HTTPException(429, '采样排队已超时，请发送最新采样',
                                                             headers={'Retry-After':'1'}))
                elif (len(self.tasks) < self.concurrency and station not in self.active_stations and
                      self.active_groups.get(job['group'], 0) < self.group_limits.get(job['group'], 1) and
                      (job['resource'] is None or job['resource'] not in self.active_resources)):
                    del self.pending[station]
                    self.active_stations.add(station)
                    self.active_groups[job['group']] = self.active_groups.get(job['group'], 0) + 1
                    if job['resource'] is not None:
                        self.active_resources.add(job['resource'])
                    task = asyncio.create_task(self._execute(station, job, wait))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            if self.pending or self.tasks:
                timeout = min((max(.001, j['deadline']-time.monotonic())
                               for j in self.pending.values()), default=None)
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass

    async def _execute(self, station, job, wait):
        stats = self._stats(station)
        try:
            result = await job['work'](wait)
            stats['completed'] += 1
            stats['waits'].append(wait)
            stats['latencies'].append((time.monotonic() - job['received']) * 1000)
            if not job['future'].done():
                job['future'].set_result(result)
        except asyncio.CancelledError:
            stats['cancelled'] += 1
            if not job['future'].done():
                job['future'].cancel()
            raise
        except Exception as exc:
            stats['failed'] += 1
            if not job['future'].done():
                job['future'].set_exception(exc)
        finally:
            self.active_stations.discard(station)
            self.active_groups[job['group']] -= 1
            if not self.active_groups[job['group']]:
                del self.active_groups[job['group']]
            if job['resource'] is not None:
                self.active_resources.discard(job['resource'])
            self.tasks.discard(asyncio.current_task())
            self.changed.set()

    async def close(self):
        self.closed = True
        for job in self.pending.values():
            job['future'].set_exception(HTTPException(503, '工位推理服务正在停止'))
        self.pending.clear()
        self.changed.set()
        if self.runner:
            await asyncio.shield(self.runner)

    def snapshot(self):
        def percentile(values, fraction):
            ordered = sorted(values)
            return round(ordered[min(len(ordered)-1, int((len(ordered)-1)*fraction))], 2) if ordered else None
        return dict(policy='one-latest-frame-fair-turns', maxPendingPerStation=1, concurrency=self.concurrency,
            groupLimits=dict(self.group_limits), activeGroups=dict(self.active_groups),
            activeResources=len(self.active_resources),
            deadlineOrigin='server-request-received',
            queueDepth=len(self.pending), active=self.active, maxQueueWaitMs=self.max_wait_ms,
            stations={station: {**{k: v for k, v in stats.items() if k not in ('waits', 'latencies')},
                'queueWaitP95Ms': percentile(stats['waits'], .95),
                'serverP95Ms': percentile(stats['latencies'], .95),
                'serverP99Ms': percentile(stats['latencies'], .99)} for station, stats in list(self.stats.items())})
