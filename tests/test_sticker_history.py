"""Full-cycle realtime regression with explicit observations, not ML accuracy."""
import copy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from algorithms.stickers import summarize

TARGETS=[dict(id=f'T{i}',expected='triangle') for i in range(1,3)]+[
    dict(id=f'R{i}',expected='rectangle') for i in range(1,7)]


@pytest.fixture
def runtime(monkeypatch):
    import backend.sticker_realtime as module
    import backend.person_overlap as overlap
    import algorithms.cuda_lane as lane
    import algorithms.cuda_vision as vision
    image=np.zeros((64,96,3),np.uint8)
    _,jpeg=cv2.imencode('.jpg',image)
    monkeypatch.setattr(lane,'run_in_lane',lambda owner,fn,ready=None:fn())
    monkeypatch.setattr(vision,'decode',lambda payload:image)
    monkeypatch.setattr(overlap,'record_frame_ready',lambda:SimpleNamespace(synchronize=lambda:None))
    config=dict(mode='appearance',targets=TARGETS,model_name='history-test',sequence={'confirmation_frames':3})
    current={}
    class Detector:
        def __init__(self,frozen):self.config=frozen['config']
        def inspect(self,image):return copy.deepcopy(current)
    monkeypatch.setattr(module,'StickerDetector',Detector)
    monkeypatch.setattr(module,'snapshot',lambda:dict(config=config,config_hash='history-test',assets={}))
    pose=SimpleNamespace(detect_people=lambda *args:dict(people=[],personProcessingMs=0))
    service=module.RealtimeStickers(pose)
    clocks={};frames={}
    def send(states,registered=True,key=('owner','first')):
        stamp=round(clocks.get(key,-.1)+.1,4);clocks[key]=stamp
        current.clear()
        current.update(registered=registered,targets=[dict(t,state=s,score=.95 if s!='UNKNOWN' else None,bbox=[0,0,.1,.1])
            for t,s in zip(TARGETS,states)])
        frames.setdefault(key,[]).append(dict(time=stamp,inspection=copy.deepcopy(current)))
        return service.predict(jpeg.tobytes(),stream_key=key,frame_time=stamp)
    try:yield SimpleNamespace(service=service,send=send,config=config,frames=frames)
    finally:service.close()


def confirm(runtime,states,**kwargs):
    for _ in range(3):result=runtime.send(states,**kwargs)
    return result


def completed(runtime):
    confirm(runtime,['EMPTY']*8)
    confirm(runtime,['PRESENT']*2+['EMPTY']*6)
    return confirm(runtime,['PRESENT']*8)


@pytest.mark.parametrize('registered',[False,True])
def test_completed_cycle_survives_bounded_cache_and_matches_offline(runtime,registered):
    assert completed(runtime)['compliance']['status']=='PASS'
    for _ in range(130):live=runtime.send(['UNKNOWN']*8,registered)
    assert all(t['state']=='UNKNOWN' for t in live['targets'])
    assert all(t['state']=='PRESENT' for t in live['aggregateTargets'])
    assert all(t['bbox'] is None for t in live['aggregateTargets'])
    key=('owner','first');stream=runtime.service.registry.items[key]
    assert len(stream['frames'])==120 and live['sampleCount']==139
    final=runtime.service.finish(key)
    expected,assessment=summarize(runtime.frames[key],runtime.config,stream['sticker_sequence'],final=True)
    assert final['targets']==expected and final['assessmentTime']==assessment
    assert final['compliance']['status']=='PASS'
    assert len(final['sequence']['events'])==8
    assert runtime.service.finish(key)==final
    new=runtime.send(['UNKNOWN']*8,key=('owner','next'))
    assert all(t['state']=='UNKNOWN' for t in new['aggregateTargets'])
    assert new['sequence']['events']==[] and new['compliance']['status']=='REVIEW'


@pytest.mark.parametrize('contrary',['EMPTY','WRONG'])
def test_confirmed_contrary_evidence_updates_retained_history(runtime,contrary):
    completed(runtime)
    for _ in range(125):runtime.send(['UNKNOWN']*8,False)
    live=runtime.send([contrary]+['UNKNOWN']*7)
    assert live['aggregateTargets'][0]['state']=='PRESENT'
    live=runtime.send([contrary]+['UNKNOWN']*7)
    assert live['aggregateTargets'][0]['state']==contrary
    final=runtime.service.finish(('owner','first'))
    assert final['targets'][0]['state']==contrary
    assert final['compliance']['status']=='FAIL'


def test_finished_product_retained_without_inventing_order(runtime):
    confirm(runtime,['PRESENT']*8)
    for _ in range(125):runtime.send(['UNKNOWN']*8,False)
    final=runtime.service.finish(('owner','first'))
    assert all(t['state']=='PRESENT' for t in final['targets'])
    assert final['sequence']['events']==[]
    assert final['compliance']['status']=='REVIEW'


def test_true_wrong_order_is_not_cleared_by_long_tail(runtime):
    confirm(runtime,['EMPTY']*8)
    confirm(runtime,['EMPTY']*2+['PRESENT']*6)
    confirm(runtime,['PRESENT']*8)
    for _ in range(125):runtime.send(['UNKNOWN']*8,False)
    final=runtime.service.finish(('owner','first'))
    assert all(t['state']=='PRESENT' for t in final['targets'])
    assert final['sequence']['status']=='FAIL' and final['sequence']['violations']
    assert final['compliance']['status']=='FAIL'


def test_all_sites_retained_with_ambiguous_order_still_need_review(runtime):
    confirm(runtime,['EMPTY']*8)
    confirm(runtime,['UNKNOWN','PRESENT']+['EMPTY']*6)
    confirm(runtime,['UNKNOWN','PRESENT','PRESENT']+['EMPTY']*5)
    confirm(runtime,['PRESENT']*3+['EMPTY']*5)
    confirm(runtime,['PRESENT']*8)
    for _ in range(125):runtime.send(['UNKNOWN']*8,False)
    final=runtime.service.finish(('owner','first'))
    assert all(t['state']=='PRESENT' for t in final['targets'])
    assert len(final['sequence']['events'])==8 and final['sequence']['violations']==[]
    assert final['sequence']['firstRectangleAt']<final['sequence']['trianglesCompleteAt']
    assert final['compliance']['status']=='REVIEW'


def test_action_mode_keeps_event_aggregation_after_long_tail(runtime):
    runtime.config.update(mode='action',action={})
    confirm(runtime,['CLEAR']*8)
    runtime.send(['CONTACT']*2+['CLEAR']*6)
    confirm(runtime,['CLEAR']*8)
    runtime.send(['CLEAR']*2+['CONTACT']*6)
    live=confirm(runtime,['CLEAR']*8)
    assert live['compliance']['status']=='PASS'
    for _ in range(125):runtime.send(['UNKNOWN']*8,False)
    final=runtime.service.finish(('owner','first'))
    assert all(t['state']=='ACTION_DONE' for t in final['targets'])
    assert final['compliance']['status']=='PASS'


@pytest.mark.parametrize('bare_first',[False,True])
def test_visual_coverage_completes_without_order_or_bare_baseline(runtime,bare_first):
    runtime.config['verification_mode']='visual-cumulative'
    if bare_first:
        assert confirm(runtime,['EMPTY']*8)['compliance']['status']=='REVIEW'
        assert confirm(runtime,['EMPTY']*2+['PRESENT']*6)['compliance']['status']=='REVIEW'
    for _ in range(2):
        assert runtime.send(['PRESENT']*8)['compliance']['status']=='REVIEW'
    result=runtime.send(['PRESENT']*8)
    assert result['compliance']['status']=='PASS'
    assert result['sequence']['positionsConfirmed']==result['sequence']['positionsExpected']==8
    assert result['sequence']['verificationMode']=='visual-cumulative'
    for _ in range(130):result=runtime.send(['UNKNOWN']*8,False)
    assert result['compliance']['status']=='PASS'
    final=runtime.service.finish(('owner','first'))
    assert final['compliance']['status']=='PASS'
    assert runtime.service.finish(('owner','first'))==final
    assert all(t['bbox'] is None for t in final['targets'])
    fresh=runtime.send(['UNKNOWN']*8,key=('owner','next'))
    assert fresh['sequence']['positionsConfirmed']==0
    assert fresh['compliance']['status']=='REVIEW'


@pytest.mark.parametrize('state,live,final',[
    ('EMPTY','REVIEW','FAIL'),('WRONG','FAIL','FAIL'),('UNKNOWN','REVIEW','REVIEW')])
def test_visual_coverage_does_not_pass_missing_wrong_or_unknown(runtime,state,live,final):
    runtime.config['verification_mode']='visual-cumulative'
    result=confirm(runtime,['PRESENT']*7+[state])
    assert result['compliance']['status']==live
    assert result['sequence']['positionsConfirmed']==7
    assert runtime.service.finish(('owner','first'))['compliance']['status']==final


@pytest.mark.parametrize('contrary',['EMPTY','WRONG'])
def test_visual_coverage_requires_three_frames_to_replace_confirmation(runtime,contrary):
    runtime.config['verification_mode']='visual-cumulative'
    confirm(runtime,['PRESENT']*8)
    for _ in range(2):
        assert runtime.send([contrary]+['UNKNOWN']*7)['aggregateTargets'][0]['state']=='PRESENT'
    assert runtime.send([contrary]+['UNKNOWN']*7)['aggregateTargets'][0]['state']==contrary
    assert runtime.service.finish(('owner','first'))['compliance']['status']=='FAIL'


def test_visual_coverage_retains_configured_sites_and_resets_distant_votes():
    from algorithms.stickers import create_sequence,compliance
    sequence=create_sequence(dict(targets=TARGETS,verification_mode='visual-cumulative'))
    partial=dict(registered=True,targets=[dict(TARGETS[0],state='PRESENT',score=.95)])
    for stamp in (0,.1,4):sequence.update(stamp,partial)
    assert sequence.snapshot()['positionsConfirmed']==0
    for stamp in (4.1,4.2):sequence.update(stamp,partial)
    coverage=sequence.snapshot()
    assert coverage['positionsConfirmed']==1 and coverage['positionsExpected']==8
    assert compliance(sequence.history.aggregate(),coverage)['status']=='REVIEW'
