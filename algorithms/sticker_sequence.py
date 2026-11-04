"""Observed installation order. Timestamps are evidence, never a video script."""
import copy


class StickerSequence:
    def __init__(self,targets,confirmation_frames=2,max_gap=1.5):
        self.targets=copy.deepcopy(targets)
        self.minimum=confirmation_frames;self.max_gap=max_gap
        self.triangles=[t['id'] for t in targets if t['expected']=='triangle']
        self.rectangles=[t['id'] for t in targets if t['expected']=='rectangle']
        if len(self.triangles)!=2 or not self.rectangles:
            raise ValueError('贴条工序需要两处三角和至少一处方形位置')
        self.sites={t['id']:dict(state='UNKNOWN',pending=None,count=0,last=None,
            first=None,emptySeen=None,placement=None,initialPresence=None,lastEmpty=None) for t in targets}
        self.violations=[];self.events=[];self.last_time=None

    def update(self,time,inspection):
        if self.last_time is not None and time<=self.last_time:raise ValueError('采样时间必须严格递增')
        self.last_time=time
        observations={t['id']:t for t in inspection['targets']} if inspection['registered'] else {}
        for identity,site in self.sites.items():
            value=observations.get(identity,{}).get('state','UNKNOWN')
            if value=='UNKNOWN':
                # Occlusion supplies no contrary observation. A bounded gap
                # can join actual agreeing samples for process events only;
                # it never creates a current-frame detection or a new vote.
                if site['last'] is None or time-site['last']>self.max_gap:
                    site.update(pending=None,count=0)
                continue
            same=site['pending']==value and site['last'] is not None and time-site['last']<=self.max_gap
            site.update(pending=value,count=site['count']+1 if same else 1,last=time,
                        first=site['first'] if same else time)
            if site['count']<self.minimum:continue
            site['state']=value
            if value=='EMPTY':
                site['lastEmpty']=time
                if site['emptySeen'] is None:site['emptySeen']=site['first']
            elif value=='PRESENT':
                if site['initialPresence'] is None:site['initialPresence']=site['first']
                if site['placement'] is None and site['emptySeen'] is not None and site['emptySeen']<site['first']:
                    site['placement']=site['first']
                    self.events.append(dict(id=identity,time=site['first'],confirmedAt=time,event='PLACED'))
        # An order violation needs actual bare-site evidence while a rectangle
        # is being installed; unobserved/occluded triangles cannot prove it.
        for identity in self.rectangles:
            start=self.sites[identity]['placement']
            if start is None:continue
            for triangle in self.triangles:
                site=self.sites[triangle]
                if site['lastEmpty'] is not None and site['lastEmpty']>=start and (
                        site['placement'] is None or site['placement']>=start):
                    violation=dict(rectangle=identity,triangle=triangle,time=start,
                                   reason=f'{identity}贴上时，{triangle}仍有未贴证据')
                    if not any(v['rectangle']==identity and v['triangle']==triangle for v in self.violations):
                        self.violations.append(violation)
        return self.snapshot()

    def snapshot(self):
        placements={identity:s['placement'] for identity,s in self.sites.items() if s['placement'] is not None}
        triangle_times=[placements.get(k) for k in self.triangles]
        rectangle_first=[s['placement'] for k,s in self.sites.items() if k in self.rectangles and s['placement'] is not None]
        triangles_done=all(t is not None for t in triangle_times)
        first_rectangle=min(rectangle_first) if rectangle_first else None
        before=triangles_done and first_rectangle is not None and max(triangle_times)<first_rectangle
        complete=all(k in placements for k in self.rectangles)
        status='FAIL' if self.violations else 'PASS' if before and complete else 'PENDING'
        stage='顺序错误' if status=='FAIL' else '工序证据齐全' if status=='PASS' else '贴方形贴' if triangles_done else '先贴两处三角'
        return dict(status=status,stage=stage,trianglesObserved=sum(k in placements for k in self.triangles),
            trianglesExpected=2,rectanglesObserved=sum(k in placements for k in self.rectangles),
            rectanglesExpected=len(self.rectangles),trianglesCompleteAt=max(triangle_times) if triangles_done else None,
            firstRectangleAt=first_rectangle,events=copy.deepcopy(sorted(self.events,key=lambda x:x['time'])),
            violations=copy.deepcopy(self.violations),
            reason=self.violations[0]['reason'] if self.violations else
                '已观察到两处三角贴在方形贴之前完成，且后续方形贴均有贴前与贴后证据' if status=='PASS' else
                '缺少贴前→贴后或先后三角/方形的连续视觉证据；成品已贴好不证明操作顺序')


def compliance(targets,sequence):
    empty=[t['id'] for t in targets if t['state']=='EMPTY']
    wrong=[t['id'] for t in targets if t['state']=='WRONG']
    unknown=[t['id'] for t in targets if t['state']=='UNKNOWN']
    status='FAIL' if empty or wrong or sequence['status']=='FAIL' else 'REVIEW' if unknown or sequence['status']!='PASS' else 'PASS'
    reason=('末次可核对画面确认未贴：'+', '.join(empty)) if empty else ('贴条形状不符：'+', '.join(wrong)) if wrong else sequence['reason'] if sequence['status']=='FAIL' else ('位置证据不足：'+', '.join(unknown)) if unknown else sequence['reason']
    return dict(status=status,label={'PASS':'合规','FAIL':'不合规','REVIEW':'无法判定'}[status],reason=reason,
        scope='贴条外观与先两处三角、后方形贴的工序顺序；不测量胶粘强度',empty=empty,wrong=wrong,unknown=unknown)
