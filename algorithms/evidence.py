"""Causal evidence utilities independent of model architecture or class names."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Confirmation:
    value: str
    first: float
    last: float
    score: float | None


class EvidenceWindow:
    """Require real agreeing observations within bounded time, not frame count alone.

    Unknown observations do not vote. Expiration only removes pending evidence;
    the caller owns already confirmed product-cycle history.
    """

    def __init__(self, frames=2, max_gap=2.0, max_window=4.0):
        if isinstance(frames, bool) or int(frames) != frames or frames < 1:
            raise ValueError('frames must be a positive integer')
        if not all(math.isfinite(v) and v > 0 for v in (max_gap, max_window)):
            raise ValueError('evidence time limits must be finite and positive')
        self.frames, self.max_gap, self.max_window = int(frames), max_gap, max_window
        self.time = None
        self.reset()

    def reset(self):
        self.value = self.first = self.last = self.score = None
        self.count = 0

    def observe(self, timestamp, value=None, score=None):
        if not math.isfinite(timestamp) or (self.time is not None and timestamp <= self.time):
            raise ValueError('采样时间必须有限且严格递增')
        self.time = timestamp
        if self.last is not None and (timestamp - self.last > self.max_gap or
                                      timestamp - self.first > self.max_window):
            self.reset()
        if value is None:
            return None
        if score is not None and (not math.isfinite(score) or not 0 <= score <= 1):
            return None
        if value != self.value:
            self.reset()
            self.value, self.first = value, timestamp
        self.last = timestamp
        self.count += 1
        if score is not None:
            self.score = score if self.score is None else min(self.score, score)
        if self.count < self.frames:
            return None
        result = Confirmation(self.value, self.first, self.last, self.score)
        self.reset()
        return result


def event_precedence(predecessor, successor):
    """Compare sampled end/start intervals: before, violation, or uncertain.

    The true end is (lastContactAt, releasedAt]; the true start is
    (startAfter, time]. Confirmation latency is intentionally excluded.
    """
    if predecessor is None:
        return 'violation'
    end_upper = predecessor.get('releasedAt')
    start_lower = successor.get('startAfter')
    if end_upper is not None and start_lower is not None and end_upper <= start_lower:
        return 'before'
    if predecessor.get('lastContactAt', float('-inf')) >= successor['time']:
        return 'violation'
    return 'uncertain'
