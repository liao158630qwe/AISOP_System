"""Monotonic per-stage timing; excludes camera/network/browser latency."""
import time


class StageTimer:
    def __init__(self):
        self.started = self.previous = time.perf_counter()
        self.stages = {}

    def mark(self, name):
        now = time.perf_counter()
        self.stages[name] = round((now - self.previous) * 1000, 3)
        self.previous = now

    def result(self):
        return dict(totalProcessingMs=round((time.perf_counter() - self.started) * 1000, 3),
                    timingsMs=dict(self.stages))
