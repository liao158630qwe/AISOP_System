"""Bounded stream state. Call under the detector lock; no raw frames retained."""
import time
import math
import threading
from fastapi import HTTPException
from algorithms.tracking import PoseTracker
from algorithms.action_sop import ActionSequence


class StreamRegistry:
    def __init__(self, ttl=60, maximum=16, per_owner=4):
        self.ttl, self.maximum, self.per_owner = ttl, maximum, per_owner
        self.items = {}
        self.lock = threading.RLock()

    def get(self, key, timestamp, signature, workflow=None, clock=None):
        with self.lock:
            if timestamp is None or not math.isfinite(timestamp) or timestamp < 0:
                raise HTTPException(422, '采样时间戳必须有限且非负')
            now = time.monotonic() if clock is None else clock
            self.items = {k: v for k, v in self.items.items() if now-v['used'] < self.ttl}
            state = self.items.get(key)
            if state and timestamp <= state['timestamp']:
                raise HTTPException(409, '采样时间戳已过期，请重新开始检测')
            if state and signature != state['signature']:
                del self.items[key]
                state = None
            if state is None:
                owned = [k for k in self.items if k[0] == key[0]]
                if len(owned) >= self.per_owner:
                    del self.items[min(owned, key=lambda k:self.items[k]['used'])]
                if len(self.items) >= self.maximum:
                    del self.items[min(self.items, key=lambda k:self.items[k]['used'])]
                state = {'tracker': PoseTracker(), 'sequence': ActionSequence(workflow) if workflow else None,
                         'signature': signature, 'created': now, 'lock': threading.RLock()}
                self.items[key] = state
            state['used'], state['timestamp'] = now, timestamp
            return state

    def drop(self, key):
        with self.lock:
            self.items.pop(key, None)
