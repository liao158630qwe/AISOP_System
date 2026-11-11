"""Bounded stream state. Call under the detector lock; no raw frames retained."""
import time
import math
import threading
from fastapi import HTTPException
from algorithms.tracking import PoseTracker
from algorithms.action_sop import ActionSequence
from .inspection_policy import complete_visual_coverage
from .stream_limits import StreamLimits


class StreamRegistry:
    def __init__(self, ttl=300, maximum=32, per_owner=16):
        self.ttl, self.maximum, self.per_owner = ttl, maximum, per_owner
        self.items = {}
        self.lock = threading.RLock()
        self.limit_seconds = (600, 60)

    def get(self, key, timestamp, signature, workflow=None, clock=None):
        with self.lock:
            if timestamp is None or not math.isfinite(timestamp) or timestamp < 0:
                raise HTTPException(422, '采样时间戳必须有限且非负')
            now = time.monotonic() if clock is None else clock
            # Expiry of this stream is explicit: never silently reset an ongoing
            # SOP cycle and later report PASS using only its surviving samples.
            expired = self.items.get(key)
            if expired and now-expired['used'] >= self.ttl:
                raise HTTPException(409, '检测状态已过期，请重新开始本轮检测')
            self.items = {k: v for k, v in self.items.items() if now-v['used'] < self.ttl}
            state = self.items.get(key)
            if state and timestamp <= state['timestamp']:
                raise HTTPException(409, '采样时间戳已过期，请重新开始检测')
            if state and signature != state['signature']:
                raise HTTPException(409, '模型或画面配置已改变，请重新开始本轮检测')
            if state is None:
                owned = [k for k in self.items if k[0] == key[0]]
                if len(owned) >= self.per_owner or len(self.items) >= self.maximum:
                    raise HTTPException(429, '实时检测状态容量已满，请先结束已有检测', headers={'Retry-After': '1'})
                state = {'tracker': PoseTracker(), 'sequence': ActionSequence(workflow) if workflow else None,
                         'signature': signature, 'created': now, 'lock': threading.RLock()}
                state['limits'] = StreamLimits(*self.limit_seconds, clock=now)
                if timestamp > 3:
                    self._append_gap(state, {'start': 0, 'end': timestamp,
                        'reason': '检测状态缺失或首帧延迟超过3秒，需复核此前操作', 'source': 'server'})
                self.items[key] = state
            state['used'], state['timestamp'] = now, timestamp
            return state

    def snapshot(self):
        with self.lock:
            active = sum(time.monotonic() - state['used'] < self.ttl for state in self.items.values())
            return {'active': active, 'maximum': self.maximum, 'perSession': self.per_owner,
                    'ttlSeconds': self.ttl, 'overflowPolicy': 'reject-new-stream'}

    def drop(self, key):
        with self.lock:
            self.items.pop(key, None)

    def check_limits(self, key):
        with self.lock:
            state = self.items.get(key)
            if state:
                state['limits'].check()

    def observe_limits(self, key, kind, result):
        with self.lock:
            state = self.items.get(key)
            if state:
                state['limits'].observe(kind, result)
                result['streamLimits'] = state['limits'].public()

    @staticmethod
    def _append_gap(state, gap):
        gaps = state.setdefault('evidenceGaps', [])
        if gap not in gaps:
            if len(gaps) < 128:
                gaps.append(gap)
            else:
                state['evidenceGapsTruncated'] = True

    def record_sample(self, key, timestamp):
        """Track successful samples only; a failed model call cannot close a gap."""
        with self.lock:
            state = self.items.get(key)
            if state is None:
                return
            previous = state.get('lastAcceptedFrameTime')
            accepted_at = time.monotonic()
            if previous is not None and timestamp - previous > 3:
                self._append_gap(state, {'start': previous, 'end': timestamp,
                    'reason': '连续有效采样间隔超过3秒', 'source': 'server'})
            elif previous is not None and accepted_at - state['lastAcceptedAt'] > 3:
                # Client-relative clocks can pause with a hidden tab or camera.
                # Server elapsed time still proves an interruption in evidence.
                self._append_gap(state, {'start': previous, 'end': timestamp,
                    'elapsedSeconds': round(accepted_at-state['lastAcceptedAt'], 3),
                    'reason': '服务器连续有效采样间隔超过3秒', 'source': 'server'})
            state['lastAcceptedFrameTime'] = timestamp
            state['lastAcceptedAt'] = accepted_at

    def protect_result(self, key, result, *, client_gaps=(), final=False):
        with self.lock:
            state = self.items.get(key)
            if state is None:
                return result
            for gap in client_gaps:
                self._append_gap(state, {**gap, 'source': 'client'})
            # Freeze the ending gap once. Retrying finish must not keep extending it.
            if final and not state.get('gapFinalized'):
                elapsed = time.monotonic() - state.get('lastAcceptedAt', time.monotonic())
                if elapsed > 3 and 'lastAcceptedFrameTime' in state:
                    start = state['lastAcceptedFrameTime']
                    self._append_gap(state, {'start': start, 'end': start + elapsed,
                        'reason': '结束前超过3秒没有有效采样', 'source': 'server'})
                state['gapFinalized'] = True
            protected = dict(result, evidenceGaps=[dict(gap) for gap in state.get('evidenceGaps', [])],
                             evidenceGapsTruncated=bool(state.get('evidenceGapsTruncated')))
            if state.get('limits') and state['limits'].stopped:
                protected['originalCompliance'] = dict(result.get('compliance') or {})
                protected['compliance'] = dict(status='REVIEW', label='自动停止 · 待复核',
                                               reason=state['limits'].stopped)
                return protected
            if (protected['evidenceGaps'] and result.get('compliance', {}).get('status') == 'PASS'
                    and not complete_visual_coverage(result)):
                protected['originalCompliance'] = dict(result['compliance'])
                protected['compliance'] = {'status': 'REVIEW', 'label': '证据缺口待复核',
                    'reason': '本轮存在采样中断，已保留原始判定；请复核中断期间的操作或重新检测。'}
            return protected
