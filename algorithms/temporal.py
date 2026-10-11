"""Causal, elapsed-time action decoding. Scores are rule support, not accuracy."""
import math


class ActionDecoder:
    def __init__(self, min_hold=.3, max_gap=.75, smoothing_seconds=.12,
                 candidate_grace=.12, release_seconds=.18):
        values = (min_hold, max_gap, smoothing_seconds, candidate_grace, release_seconds)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError('时序参数必须是有限正数')
        self.min_hold, self.max_gap, self.tau = min_hold, max_gap, smoothing_seconds
        self.candidate_grace, self.release_seconds = candidate_grace, release_seconds
        self.reset()

    def reset_candidate(self):
        self.candidate = None
        self.since = None
        self.samples = 0
        self.candidate_seconds = 0.
        self.supported_at = None
        self.previous_supported = False

    def reset(self):
        self.last = None
        self.scores = {}
        self.action = 'unknown'
        self.weak_since = None
        self.reset_candidate()

    def update(self, scores, t, valid=True):
        if not math.isfinite(t) or (self.last is not None and t <= self.last):
            raise ValueError('动作时间戳必须严格递增且有限')
        # Reject malformed evidence before changing the state or timestamp.
        raw = {name: float(value) for name, value in scores.items() if name != 'unknown'}
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in raw.values()):
            raise ValueError('动作证据必须在0到1之间且有限')
        dt = None if self.last is None else t-self.last
        interrupted = dt is not None and dt > self.max_gap
        if not valid or interrupted:
            self.reset()
            dt = None
        self.last = t
        if not valid:
            return self.output('关键点不足或轨迹关联不确定')
        alpha = 1 if dt is None else 1-math.exp(-dt/self.tau)
        self.scores = {name: alpha*raw.get(name, 0)+(1-alpha)*self.scores.get(name, raw.get(name, 0))
                       for name in self.scores.keys() | raw.keys()}
        # A label requires fresh evidence, even while its filtered score is high.
        eligible = [name for name in sorted(raw) if raw[name] >= .6 and self.scores[name] >= .6]
        winner = max(eligible, key=self.scores.get, default=None)
        if (self.action in eligible and winner != self.action
                and self.scores[winner]-self.scores[self.action] < .12):
            winner = self.action

        if self.action != 'unknown':
            if raw.get(self.action, 0) < .35:
                self.action = 'unknown'
                self.weak_since = None
            elif raw.get(self.action, 0) < .5:
                if self.weak_since is None:
                    self.weak_since = t
                if t-self.weak_since >= self.release_seconds-1e-8:
                    self.action = 'unknown'
                    self.weak_since = None
            else:
                self.weak_since = None

        if winner is not None:
            if (not self.previous_supported and self.supported_at is not None
                    and t-self.supported_at > self.candidate_grace):
                self.reset_candidate()
            if winner != self.candidate:
                self.reset_candidate()
                self.candidate, self.since = winner, t
            elif self.previous_supported and dt is not None:
                self.candidate_seconds += dt
            self.samples += 1
            self.supported_at = t
            self.previous_supported = True
        else:
            # A short threshold wobble pauses accumulation; missing evidence resets it.
            if (self.candidate is None or raw.get(self.candidate, 0) < .35
                    or t-self.supported_at > self.candidate_grace):
                self.reset_candidate()
            else:
                self.previous_supported = False
        if (winner is not None and self.samples >= 2
                and self.candidate_seconds >= self.min_hold-1e-8):
            self.action = winner
            self.weak_since = None
        reason = ('检测间隔过长，重新累计动作证据' if interrupted else
                  '稳定动作' if self.action != 'unknown' else '动作转换或持续证据不足')
        return self.output(reason)

    def output(self, reason):
        return {'action': self.action, 'scores': {k: round(v, 5) for k, v in self.scores.items()},
                'reason': reason, 'candidate': self.candidate,
                'candidateSeconds': round(self.candidate_seconds, 4),
                'requiredSeconds': self.min_hold,
                'method': 'causal-duration-v3'}
