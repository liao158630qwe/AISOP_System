"""Pure diagnostic replay of supplied process evidence, never pose-derived completion."""
from collections import defaultdict
from typing import Literal
from pydantic import Field, model_validator
from .stations import Strict


class Attempt(Strict):
    id: str
    revision: int = Field(default=1, ge=1)
    cycle: str
    target: str
    program: str
    result: Literal['OK','NG','UNKNOWN']
    start: tuple[float,float]
    end: tuple[float,float]
    verified: bool = False
    valid: bool = True

    @model_validator(mode='after')
    def valid_times(self):
        if self.start[0] > self.start[1] or self.end[0] > self.end[1] or self.end[1] < self.start[0]:
            raise ValueError('起止时间区间冲突')
        return self


class ProcessSpec(Strict):
    version: str
    approved: bool = False
    targets: list[str] = Field(min_length=1, max_length=100)
    programs: dict[str,str]
    precedes: list[tuple[str,str]] = Field(default_factory=list)
    durations: dict[str,tuple[float,float]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_graph(self):
        if len(set(self.targets)) != len(self.targets) or set(self.programs) != set(self.targets):
            raise ValueError('位置编号重复或必需程序未完整配置')
        edges=defaultdict(list)
        for a,b in self.precedes:
            if a not in self.targets or b not in self.targets:
                raise ValueError('前置规则引用未知位置')
            edges[a].append(b)
        visiting,done=set(),set()
        def visit(node):
            if node in visiting:
                raise ValueError('前置规则存在环')
            if node in done:
                return
            visiting.add(node)
            for child in edges[node]:
                visit(child)
            visiting.remove(node)
            done.add(node)
        for node in self.targets:
            visit(node)
        for target,(lo,hi) in self.durations.items():
            if target not in self.targets or not 0 <= lo <= hi:
                raise ValueError('持续时间规则无效')
        return self


def order_relation(a_end,b_start):
    return 'TRUE' if a_end[1] <= b_start[0] else 'FALSE' if a_end[0] > b_start[1] else 'UNKNOWN'


def duration_relation(start,end,allowed):
    lo,hi=max(0,end[0]-start[1]),end[1]-start[0]
    if hi < 0:
        return 'UNKNOWN'
    return 'TRUE' if allowed[0] <= lo and hi <= allowed[1] else 'FALSE' if hi < allowed[0] or lo > allowed[1] else 'UNKNOWN'


def evaluate(spec, attempts, *, cycle, checkpoint=False, coverage_complete=False):
    spec=ProcessSpec.model_validate(spec)
    attempts=[Attempt.model_validate(a) for a in attempts]
    versions,latest={},{}
    for a in attempts:
        key=(a.id,a.revision)
        if key in versions and versions[key] != a:
            raise ValueError('同事件版本内容冲突')
        versions[key]=a
        if a.id in latest and latest[a.id].cycle != a.cycle:
            raise ValueError('同事件不能跨产品周期')
        if a.id not in latest or a.revision > latest[a.id].revision:
            latest[a.id]=a
    selected=defaultdict(list)
    rejected=[]
    for a in latest.values():
        if a.cycle != cycle or a.target not in spec.targets or a.program != spec.programs.get(a.target) or not a.verified or not a.valid:
            rejected.append(a.id)
            continue
        selected[a.target].append(a)
    rules=[]
    for target in spec.targets:
        records=selected[target]
        ok=[a for a in records if a.result=='OK']
        # Any unresolved NG stays FALSE; repair requires explicit revision/invalidation.
        state=('FALSE' if any(a.result=='NG' for a in records) else 'TRUE' if ok else
               'FALSE' if checkpoint and coverage_complete and not rejected and not records else 'UNKNOWN')
        rules.append({'rule':'coverage','target':target,'state':state,'attempts':len(records)})
        if target in spec.durations:
            durations=[duration_relation(a.start,a.end,spec.durations[target]) for a in ok]
            state='TRUE' if 'TRUE' in durations else 'FALSE' if durations and all(s=='FALSE' for s in durations) else 'UNKNOWN'
            rules.append({'rule':'duration','target':target,'state':state})
    for a,b in spec.precedes:
        predecessors=[x for x in selected[a] if x.result=='OK']
        successors=selected[b]
        # Each accepted successor attempt needs an already-completed predecessor.
        relations=[]
        for successor in successors:
            states=[order_relation(p.end,successor.start) for p in predecessors]
            relations.append('TRUE' if 'TRUE' in states else 'FALSE' if states and all(s=='FALSE' for s in states) else 'UNKNOWN')
        state='FALSE' if 'FALSE' in relations else 'TRUE' if relations and all(s=='TRUE' for s in relations) else 'UNKNOWN'
        rules.append({'rule':'order','before':a,'after':b,'state':state})
    ready=checkpoint and spec.approved and coverage_complete and not rejected
    verdict='FAIL' if any(r['state']=='FALSE' for r in rules) else 'PASS' if ready and all(r['state']=='TRUE' for r in rules) else 'REVIEW'
    return {'mode':'DIAGNOSTIC_REPLAY','cycle':cycle,'version':spec.version,'verdict':verdict,
            'rules':rules,'coveredTargets':sorted(t for t in spec.targets if any(a.result=='OK' for a in selected[t])),
            'rejectedEvents':rejected,'historyNG':sorted({a.id for a in attempts if a.cycle==cycle and a.result=='NG'}),
            'scope':'仅对输入事件及所给配置进行规则回放，不证明原始视频检测准确性或生产合格'}
