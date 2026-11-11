"""Bound password verification work and failed attempts within the single API process."""
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi import HTTPException


@dataclass
class _Peer:
    failures: deque = field(default_factory=deque)
    active: int = 0


@dataclass
class Attempt:
    success: bool | None = None


class LoginLimiter:
    def __init__(self, *, limit=10, window=300, per_peer=2, capacity=8, max_peers=4096, clock=time.monotonic):
        self.limit, self.window = limit, window
        self.per_peer, self.capacity, self.max_peers = per_peer, capacity, max_peers
        self.clock = clock
        self._lock = threading.Lock()
        self._peers = {}
        self._active = 0
        self._next_sweep = 0

    def _prune(self, peer, current):
        while peer.failures and peer.failures[0] <= current - self.window:
            peer.failures.popleft()

    @staticmethod
    def _reject(seconds):
        raise HTTPException(429, {'code': 'LOGIN_RATE_LIMITED', 'message': '登录尝试过多，请稍后重试'},
                            headers={'Retry-After': str(max(1, math.ceil(seconds)))})

    @contextmanager
    def attempt(self, key):
        with self._lock:
            current = self.clock()
            if current >= self._next_sweep or len(self._peers) >= self.max_peers:
                for name, peer in list(self._peers.items()):
                    self._prune(peer, current)
                    if not peer.active and not peer.failures:
                        del self._peers[name]
                self._next_sweep = current + 60
            peer = self._peers.get(key)
            if peer is None:
                if len(self._peers) >= self.max_peers:
                    self._reject(1)
                peer = self._peers[key] = _Peer()
            self._prune(peer, current)
            if len(peer.failures) >= self.limit:
                self._reject(peer.failures[0] + self.window - current)
            if (len(peer.failures) + peer.active >= self.limit or
                    peer.active >= self.per_peer or self._active >= self.capacity):
                self._reject(1)
            peer.active += 1
            self._active += 1
        outcome = Attempt()
        try:
            yield outcome
        finally:
            with self._lock:
                peer.active -= 1
                self._active -= 1
                self._prune(peer, self.clock())
                if outcome.success is True:
                    peer.failures.clear()
                elif outcome.success is False:
                    peer.failures.append(self.clock())
                if not peer.active and not peer.failures:
                    self._peers.pop(key, None)
