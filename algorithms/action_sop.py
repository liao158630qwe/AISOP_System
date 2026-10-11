"""Per-track causal sequence evidence; never turns pose into product PASS/FAIL."""
import hashlib
import json
import math
import copy
import threading
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .stations import Strict

Action = Literal['standing', 'walking', 'raised', 'reaching', 'bending', 'grip', 'objects']
CONFIG = Path(__file__).resolve().parents[1]/'config'/'action-workflows.json'
_cache_lock = threading.RLock()
_catalog_cache = None
_snapshot_cache = {}


def _config_signature():
    stat = CONFIG.stat()
    return (stat.st_mtime_ns, stat.st_size)


class ActionStep(Strict):
    id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=80)
    actions: list[Action] = Field(min_length=1, max_length=7)
    min_hold: float = Field(default=.5, ge=.2, le=60)
    max_wait: float = Field(default=30, ge=1, le=600)
    roi: tuple[float, float, float, float] | None = None

    @model_validator(mode='after')
    def validate_step(self):
        if len(set(self.actions)) != len(self.actions) or self.max_wait < self.min_hold:
            raise ValueError('动作不能重复，等待时限不得小于持续时间')
        if self.roi:
            a, b, c, d = self.roi
            if not (0 <= a < c <= 1 and 0 <= b < d <= 1):
                raise ValueError('动作区域必须是归一化非空矩形')
        return self


class ActionWorkflow(Strict):
    id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    steps: list[ActionStep] = Field(min_length=1, max_length=20)
    max_gap: float = Field(default=.75, ge=.2, le=2)
    release_seconds: float = Field(default=.2, ge=.1, le=2)
    threshold: float = Field(default=.65, ge=.5, le=.95)

    @model_validator(mode='after')
    def unique_steps(self):
        if len({s.id for s in self.steps}) != len(self.steps):
            raise ValueError('动作步骤编号不能重复')
        return self


def catalog():
    global _catalog_cache
    signature = _config_signature()
    with _cache_lock:
        if _catalog_cache and _catalog_cache[0] == signature:
            return _catalog_cache[1]
        values = [ActionWorkflow.model_validate(v) for v in json.loads(CONFIG.read_text(encoding='utf-8'))]
        if len({v.id for v in values}) != len(values):
            raise ValueError('动作模板编号重复')
        _catalog_cache = (signature, values)
        _snapshot_cache.clear()
        return values


def freeze(workflow):
    config = ActionWorkflow.model_validate(workflow).model_dump(mode='json')
    return {'config': config, 'sha256': hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


def snapshot(workflow_id):
    workflow = next((w for w in catalog() if w.id == workflow_id), None)
    if workflow is None:
        raise ValueError('未知动作时序模板')
    with _cache_lock:
        cached = _snapshot_cache.get(workflow_id)
        if cached:
            return copy.deepcopy(cached)
        frozen = freeze(workflow)
        _snapshot_cache[workflow_id] = frozen
        return copy.deepcopy(frozen)


class ActionSequence:
    def __init__(self, frozen):
        if freeze(frozen['config'])['sha256'] != frozen['sha256']:
            raise ValueError('动作配置哈希校验失败')
        self.frozen = frozen
        self.spec = ActionWorkflow.model_validate(frozen['config'])
        self.subjects = {}
        self.events = []
        self.event_count = 0
        self.last_time = None
        self.limited = False

    def event(self, context, code, step, t, detail):
        self.event_count += 1
        self.events.append({'id': self.event_count, 'trackId': context['trackId'], 'cycle': context['cycle'],
                            'code': code, 'stepId': step.id if step else None,
                            'time': round(t, 4), 'detail': detail})
        if len(self.events) > 500:
            self.events.pop(0)

    def new_context(self, tid, t, cycle=1):
        return {'trackId': tid, 'cycle': cycle, 'index': 0, 'completed': [], 'pending': {},
                'last_valid': t, 'wait_since': t, 'warned': set(), 'release': None,
                'release_since': None, 'continuity': True, 'violations': 0, 'observed': True}

    def matches(self, person, step):
        if step.roi:
            box = person['bbox']
            x, y = (box[0]+box[2])/2, (box[1]+box[3])/2
            a, b, c, d = step.roi
            if not a <= x <= c or not b <= y <= d:
                return False
        # Current-frame signals prevent stale filtered labels completing steps.
        return any(math.isfinite(person.get('signals', {}).get(action, 0)) and
                   person.get('signals', {}).get(action, 0) >= self.spec.threshold for action in step.actions)

    def released(self, person, step):
        # Missing signals are not evidence that a repeated action ended.
        signals = person.get('signals', {})
        return all(action in signals and math.isfinite(signals[action]) and
                   0 <= signals[action] < self.spec.threshold-.15 for action in step.actions)

    def update(self, people, t):
        if not math.isfinite(t) or (self.last_time is not None and t <= self.last_time):
            raise ValueError('步骤时间戳必须严格递增且有限')
        self.last_time = t
        seen = {person['trackId'] for person in people}
        for ctx in self.subjects.values():
            ctx['observed'] = False
            if ctx['trackId'] not in seen:
                ctx['pending'].clear()
                ctx['release_since'] = None
            if t-ctx['last_valid'] > self.spec.max_gap:
                ctx['continuity'] = False
                ctx['pending'].clear()
        for person in people:
            tid = person['trackId']
            if tid not in self.subjects:
                if len(self.subjects) >= 256:
                    self.limited = True
                    continue
                self.subjects[tid] = self.new_context(tid, t)
            ctx = self.subjects[tid]
            ctx['observed'] = True
            if not person.get('tracking', {}).get('reliable', False):
                ctx['pending'].clear()
                ctx['release_since'] = None
                if person.get('tracking', {}).get('ambiguous'):
                    ctx['continuity'] = False
                continue
            if not ctx['continuity']:
                self.event(ctx, 'CONTINUITY_RESET', None, t, '遮挡间隔过长或人物关联不确定，重新累计当前人物流程')
                ctx = self.new_context(tid, t, ctx['cycle']+1)
                self.subjects[tid] = ctx
            ctx['last_valid'] = t
            if ctx['index'] >= len(self.spec.steps):
                continue
            index = ctx['index']
            step = self.spec.steps[index]
            if t-ctx['wait_since'] > step.max_wait and ('timeout', index) not in ctx['warned']:
                ctx['warned'].add(('timeout', index)); ctx['violations'] += 1
                self.event(ctx, 'STEP_TIMEOUT', step, t, f'等待“{step.name}”超过{step.max_wait:g}秒，需复核动作或流程配置')
            # Consecutive identical steps require an observed release between them.
            if ctx['release'] is not None:
                previous = self.spec.steps[ctx['release']]
                if not self.released(person, previous):
                    ctx['release_since'] = None
                    continue
                if ctx['release_since'] is None:
                    ctx['release_since'] = t
                if t-ctx['release_since'] < self.spec.release_seconds:
                    continue
                ctx['release'] = None
            matched = [j for j in range(index, len(self.spec.steps)) if self.matches(person, self.spec.steps[j])]
            for j in list(ctx['pending']):
                if j not in matched:
                    del ctx['pending'][j]
            for j in matched:
                onset, samples = ctx['pending'].get(j, (t, 0))
                ctx['pending'][j] = (onset, samples+1)
            if index in matched:
                onset, samples = ctx['pending'][index]
                if samples >= 2 and t-onset >= step.min_hold-1e-8:
                    ctx['completed'].append({'stepId': step.id, 'start': round(onset, 4), 'confirmedAt': round(t, 4),
                                             'observedSeconds': round(t-onset, 4)})
                    self.event(ctx, 'STEP_OBSERVED', step, t, f'同一轨迹持续观察到“{step.name}”')
                    ctx['index'] += 1; ctx['pending'].clear(); ctx['wait_since'] = t
                    if ctx['index'] < len(self.spec.steps) and set(step.actions)&set(self.spec.steps[ctx['index']].actions):
                        ctx['release'] = index
            else:
                for j in matched:
                    onset, samples = ctx['pending'][j]
                    if samples >= 2 and t-onset >= self.spec.steps[j].min_hold-1e-8 and ('order', j) not in ctx['warned']:
                        ctx['warned'].add(('order', j)); ctx['violations'] += 1
                        self.event(ctx, 'OUT_OF_ORDER', self.spec.steps[j], t,
                                   f'前置“{step.name}”尚未观察完成，已持续观察到后续“{self.spec.steps[j].name}”')
        return self.result()

    def result(self, final=False):
        subjects = []
        for ctx in self.subjects.values():
            complete = ctx['index'] == len(self.spec.steps)
            step = self.spec.steps[ctx['index']] if not complete else None
            pending = ctx['pending'].get(ctx['index'])
            held = max(0., self.last_time-pending[0]) if pending and ctx['observed'] else 0.
            subjects.append({'trackId': ctx['trackId'], 'cycle': ctx['cycle'], 'observed': ctx['observed'],
                'completed': list(ctx['completed']), 'currentStep': self.spec.steps[ctx['index']].id if not complete else None,
                'status': 'EVIDENCE_GAP' if not ctx['continuity'] else 'REVIEW_SEQUENCE' if ctx['violations'] else
                          'OBSERVED_SEQUENCE' if complete else 'INCOMPLETE' if final else 'OBSERVING',
                'violations': ctx['violations'],
                'currentStepProgress': {'observedSeconds': round(held, 4),
                    'requiredSeconds': step.min_hold if step else 0,
                    'fraction': min(1., held/step.min_hold) if step else 1.,
                    'waitingForRelease': ctx['release'] is not None},
                'unobservedSteps': [s.id for s in self.spec.steps[ctx['index']:]]})
        return {'mode': 'ACTION_SEQUENCE_DIAGNOSTIC', 'verdict': 'REVIEW', 'workflow': self.frozen,
                'subjects': subjects, 'events': list(self.events), 'eventCount': self.event_count,
                'truncated': self.event_count>len(self.events) or self.limited,
                'scope': '同一轨迹的动作观察顺序；未观察不等于漏做，观察完成不等于产品合格'}
