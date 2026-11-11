"""Wall-clock limits for a detection round; only current evidence resets idle time."""
import time

from fastapi import HTTPException


def target_present(kind, result):
    if kind in ('screw', 'sticker'):
        # Registration locates the workpiece, including one with empty positions.
        # People and cumulative targets cannot establish current workpiece presence.
        return result.get('inspection', {}).get('registered') is True
    if kind == 'scene':
        return any(check.get('count', 0) > 0 for check in result.get('checks', []))
    return bool(result.get('people'))


class StreamLimits:
    def __init__(self, maximum=600, no_target=60, *, clock=None):
        self.maximum, self.no_target = maximum, no_target
        self.started = time.monotonic() if clock is None else clock
        self.last_target = self.started
        self.stopped = None

    def public(self):
        return dict(maxDurationSeconds=self.maximum, noTargetTimeoutSeconds=self.no_target)

    def check(self, *, clock=None):
        now = time.monotonic() if clock is None else clock
        if self.stopped is None:
            if now - self.started >= self.maximum:
                self.stopped = f'本轮检测已达到最长 {self.maximum} 秒，已自动停止；请确认工件后重新开始'
            elif now - self.last_target >= self.no_target:
                self.stopped = f'连续 {self.no_target} 秒未检测到目标，已自动停止；请放置工件并确认画面后重新开始'
        if self.stopped:
            raise HTTPException(410, self.stopped)

    def observe(self, kind, result, *, clock=None):
        now = time.monotonic() if clock is None else clock
        if target_present(kind, result):
            self.last_target = now
        self.check(clock=now)
