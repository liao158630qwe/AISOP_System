"""Order/isolation tests with explicit synthetic observations, not ML accuracy."""
import copy
import json
import uuid
import pytest
from sqlalchemy import select
from algorithms.sticker_sequence import StickerSequence,compliance
from algorithms.sticker_actions import StickerActionSequence,action_compliance
from backend.db import runs
from algorithms.video import file_hash

TARGETS=[dict(id='T1',name='三角1',point=[10,10],expected='triangle'),
         dict(id='T2',name='三角2',point=[20,20],expected='triangle'),
         dict(id='R1',name='方形1',point=[30,30],expected='rectangle')]

def observation(states,registered=True):
    return dict(registered=registered,targets=[dict(**t,state=s,score=.9 if s!='UNKNOWN' else None,bbox=None)
        for t,s in zip(TARGETS,states)])

def pair(sequence,time,states,registered=True):
    sequence.update(time,observation(states,registered))
    return sequence.update(time+.25,observation(states,registered))

def test_correct_order_requires_real_before_and_after_observations():
    sequence=StickerSequence(TARGETS)
    pair(sequence,0,['EMPTY']*3)
    pair(sequence,.5,['PRESENT','EMPTY','EMPTY'])
    pair(sequence,1,['PRESENT','PRESENT','EMPTY'])
    result=pair(sequence,1.5,['PRESENT']*3)
    assert result['status']=='PASS' and result['trianglesCompleteAt']<result['firstRectangleAt']
    assert compliance(observation(['PRESENT']*3)['targets'],result)['status']=='PASS'

def test_rectangle_installed_while_triangles_bare_is_sticky_violation():
    sequence=StickerSequence(TARGETS);pair(sequence,0,['EMPTY']*3)
    pair(sequence,.5,['EMPTY','EMPTY','PRESENT'])
    result=pair(sequence,1,['PRESENT']*3)
    assert result['status']=='FAIL' and len(result['violations'])==2
    assert compliance(observation(['PRESENT']*3)['targets'],result)['status']=='FAIL'

def test_complete_product_at_start_cannot_prove_installation_order():
    sequence=StickerSequence(TARGETS);result=pair(sequence,0,['PRESENT']*3)
    assert result['events']==[]
    assert compliance(observation(['PRESENT']*3)['targets'],result)['status']=='REVIEW'

def test_simultaneous_appearance_does_not_invent_triangle_precedence():
    sequence=StickerSequence(TARGETS);pair(sequence,0,['EMPTY']*3)
    result=pair(sequence,.5,['PRESENT']*3)
    assert result['status']=='PENDING'
    assert compliance(observation(['PRESENT']*3)['targets'],result)['status']=='REVIEW'

def test_three_frame_confirmation_rejects_short_flash_without_poisoning_order():
    sequence=StickerSequence(TARGETS,confirmation_frames=3)
    for t in [0,.25,.5]:sequence.update(t,observation(['EMPTY']*3))
    for t in [.75,1]:sequence.update(t,observation(['EMPTY','EMPTY','PRESENT']))
    sequence.update(1.25,observation(['EMPTY']*3))
    assert sequence.snapshot()['events']==[]
    for t in [1.5,1.75,2]:sequence.update(t,observation(['PRESENT','PRESENT','EMPTY']))
    for t in [2.25,2.5,2.75]:result=sequence.update(t,observation(['PRESENT']*3))
    assert result['status']=='PASS' and result['firstRectangleAt']==2.25

def test_brief_unknown_gaps_join_only_real_votes_and_expire():
    sequence=StickerSequence(TARGETS,confirmation_frames=3,max_gap=1)
    for t in [0,.25,.5]:sequence.update(t,observation(['EMPTY']*3))
    sequence.update(.75,observation(['PRESENT','PRESENT','EMPTY']))
    sequence.update(1,observation(['UNKNOWN']*3))
    sequence.update(1.25,observation(['PRESENT','PRESENT','EMPTY']))
    result=sequence.update(1.5,observation(['PRESENT','PRESENT','EMPTY']))
    assert result['trianglesObserved']==2 and result['trianglesCompleteAt']==.75
    second=StickerSequence(TARGETS,confirmation_frames=3,max_gap=1)
    for t in [0,.25,.5]:second.update(t,observation(['EMPTY']*3))
    second.update(.75,observation(['PRESENT']*3));second.update(2,observation(['UNKNOWN']*3))
    second.update(2.25,observation(['PRESENT']*3));result=second.update(2.5,observation(['PRESENT']*3))
    assert result['events']==[]

def test_occlusion_is_not_bare_evidence_or_false_order_failure():
    sequence=StickerSequence(TARGETS);pair(sequence,0,['EMPTY']*3)
    result=pair(sequence,5,['UNKNOWN','UNKNOWN','PRESENT'])
    assert result['violations']==[] and result['status']=='PENDING'
    assert compliance(observation(['UNKNOWN','UNKNOWN','PRESENT'])['targets'],result)['status']=='REVIEW'

def test_unregistered_frame_and_single_empty_frame_do_not_confirm_placement():
    sequence=StickerSequence(TARGETS)
    pair(sequence,0,['EMPTY']*3,False)
    sequence.update(.5,observation(['EMPTY']*3))
    result=pair(sequence,.75,['PRESENT']*3)
    assert result['events']==[]
    with pytest.raises(ValueError,match='递增'):sequence.update(.75,observation(['PRESENT']*3))

def test_cycles_do_not_share_order_and_wrong_shape_is_failure():
    first=StickerSequence(TARGETS);pair(first,0,['EMPTY']*3);pair(first,.5,['PRESENT','PRESENT','EMPTY']);pair(first,1,['PRESENT']*3)
    second=StickerSequence(TARGETS);result=pair(second,0,['PRESENT']*3)
    assert result['events']==[] and result['status']=='PENDING'
    assert compliance(observation(['PRESENT','PRESENT','WRONG'])['targets'],result)['status']=='FAIL'


ACTION_SETTINGS=dict(contact_frames=2,min_contact_seconds=.2,clear_frames=2,max_gap=1.5,min_motion=2.5,
    single_contact_threshold=.9)

def action_observation(states,scores=None,signatures=None,registered=True):
    scores=scores or {};signatures=signatures or {}
    return dict(registered=registered,targets=[dict(id=t['id'],state=s,score=scores.get(t['id']),
        actionSignature=signatures.get(t['id'])) for t,s in zip(TARGETS,states)])

def action_pair(sequence,time,states,scores=None,signatures=None):
    sequence.update(time,action_observation(states,scores,signatures))
    return sequence.update(time+.25,action_observation(states,scores,signatures))

def test_action_requires_clear_baseline_and_observed_release():
    sequence=StickerActionSequence(TARGETS,ACTION_SETTINGS)
    sequence.update(0,action_observation(['CONTACT','CLEAR','CLEAR'],{'T1':.95},{'T1':[0,0]}))
    action_pair(sequence,.25,['CLEAR']*3)
    assert sequence.snapshot()['events']==[]
    action_pair(sequence,.75,['CONTACT','CLEAR','CLEAR'],{'T1':.95},{'T1':[0,0]})
    action_pair(sequence,1.25,['CONTACT','CLEAR','CLEAR'],{'T1':.95},{'T1':[10,10]})
    result=action_pair(sequence,1.75,['CLEAR']*3)
    assert result['status']=='PENDING' and result['events'][0]['id']=='T1'
    assert result['events'][0]['evidence']=='multi-frame-motion'

def test_single_high_confidence_contact_counts_after_release_and_unknown_gap():
    sequence=StickerActionSequence(TARGETS,ACTION_SETTINGS)
    action_pair(sequence,0,['CLEAR']*3)
    sequence.update(.5,action_observation(['CONTACT','CLEAR','CLEAR'],{'T1':.92}))
    sequence.update(.75,action_observation(['UNKNOWN']*3))
    sequence.update(1.0,action_observation(['CLEAR']*3))
    result=sequence.update(1.25,action_observation(['CLEAR']*3))
    event=result['events'][0]
    assert event['id']=='T1' and event['contactFrames']==1
    assert event['evidence']=='single-high-confidence'

def test_single_low_confidence_contact_does_not_count():
    sequence=StickerActionSequence(TARGETS,ACTION_SETTINGS)
    action_pair(sequence,0,['CLEAR']*3)
    sequence.update(.5,action_observation(['CONTACT','CLEAR','CLEAR'],{'T1':.7}))
    action_pair(sequence,1.0,['CLEAR']*3)
    assert sequence.snapshot()['events']==[]

def test_action_rectangle_before_triangles_fails_order():
    sequence=StickerActionSequence(TARGETS,ACTION_SETTINGS)
    action_pair(sequence,0,['CLEAR']*3)
    action_pair(sequence,.5,['CLEAR','CLEAR','CONTACT'],{'R1':.95},{'R1':[0,0]})
    action_pair(sequence,1.0,['CLEAR','CLEAR','CONTACT'],{'R1':.95},{'R1':[10,10]})
    result=action_pair(sequence,1.5,['CLEAR']*3)
    assert result['status']=='FAIL' and result['violations'][0]['rectangle']=='R1'
    assert set(result['violations'][0]['triangles'])=={'T1','T2'}

def test_action_finished_product_without_actions_is_review_not_pass():
    sequence=StickerActionSequence(TARGETS,ACTION_SETTINGS)
    action_pair(sequence,0,['CLEAR']*3)
    targets=sequence.aggregate(final=True);decision=action_compliance(targets,sequence.snapshot(),final=True)
    assert sequence.snapshot()['events']==[] and decision['status']=='REVIEW'

def test_action_missing_after_other_events_fails_final_compliance():
    sequence=StickerActionSequence(TARGETS,ACTION_SETTINGS)
    action_pair(sequence,0,['CLEAR']*3)
    action_pair(sequence,.5,['CONTACT','CONTACT','CLEAR'],{'T1':.95,'T2':.95},{'T1':[0,0],'T2':[0,0]})
    action_pair(sequence,1.0,['CONTACT','CONTACT','CLEAR'],{'T1':.95,'T2':.95},{'T1':[10,10],'T2':[10,10]})
    action_pair(sequence,1.5,['CLEAR']*3)
    targets=sequence.aggregate(final=True);decision=action_compliance(targets,sequence.snapshot(),final=True)
    assert decision['status']=='FAIL' and decision['missingActions']==['R1']

def test_sticker_job_freezes_own_model_and_worker_dispatch(authenticated,video_bytes,monkeypatch):
    import algorithms.stickers as module
    from backend.worker import execute_one
    from conftest import new_run
    settings,app,client=authenticated
    frozen={'config':{'model':'models/sticker.pt','model_name':'贴条测试模型','targets':TARGETS},
            'assets':{'models/sticker.pt':'sticker-hash'},'config_hash':'sticker-config'}
    monkeypatch.setattr(module,'snapshot',lambda:copy.deepcopy(frozen))
    verified=[];monkeypatch.setattr(module,'verify',lambda value:verified.append(value))
    from algorithms.person_detection import snapshot as person_snapshot
    monkeypatch.setattr('algorithms.person_detection.load_detector',lambda *args:object())
    def analyze(video,model,out,**kw):
        assert kw['options']['sticker_config']==frozen
        out.mkdir(parents=True)
        for name in ['analysis.json','segments.csv','report.html','video.webm']:(out/name).write_text('{}')
        assert 'pose_detector' in kw
        manifest=dict(model_hash='sticker-hash',person_model_hash=person_snapshot(model,'yolo')['sha256'],
            source_hash=file_hash(video),files={p.name:file_hash(p) for p in out.iterdir()})
        (out/'manifest.json').write_text(json.dumps(manifest))
        return {'summary':{'analyzedFrames':2}}
    monkeypatch.setattr(module,'analyze',analyze)
    job=new_run(client,video_bytes,key='sticker-worker',options={'analysis_type':'sticker','sample_fps':4})
    with app.state.engine.connect() as c:
        row=c.execute(select(runs).where(runs.c.id==job['id'])).mappings().one()
    assert row['model_hash']=='sticker-hash' and 'screw_config' not in row['options']
    assert execute_one(app.state.engine,settings)
    assert client.get('/api/v1/analysis-runs/'+job['id']).json()['status']=='SUCCEEDED'
    assert verified==[frozen]
    listing=client.get('/api/v1/analysis-runs?analysis_type=sticker').json()
    assert len(listing['items'])==1 and listing['items'][0]['analysis_type']=='sticker'
    assert client.get('/api/v1/analysis-runs?analysis_type=screw').json()['items']==[]
    response=client.post('/api/v1/analysis-runs',json={'media_id':job['media_id'],
        'options':{'analysis_type':'sticker','sample_fps':1}},headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==422

def test_live_task_finish_is_explicitly_isolated(authenticated,monkeypatch):
    _,app,client=authenticated
    seen=[]
    monkeypatch.setattr(app.state.screw_realtime,'finish',lambda key:seen.append('screw') or {'task':'screw'})
    monkeypatch.setattr(app.state.sticker_realtime,'finish',lambda key:seen.append('sticker') or {'task':'sticker'})
    identity=str(uuid.uuid4())
    assert client.post(f'/api/v1/realtime/streams/{identity}/finish').json()['task']=='screw'
    assert client.post(f'/api/v1/realtime/streams/{identity}/finish?analysis_type=sticker').json()['task']=='sticker'
    assert seen==['screw','sticker']
    assert client.get('/api/v1/inspection-tasks').json()['items'][1]['id']=='sticker'

def test_live_sticker_accepts_portrait_within_pixel_budget(authenticated,monkeypatch):
    import cv2
    import numpy as np
    import backend.sticker_realtime as module
    _,app,client=authenticated
    monkeypatch.setattr(app.state.realtime,'detect_people',lambda *args:{'people':[]})
    monkeypatch.setattr(module,'snapshot',lambda:{'config':{'targets':TARGETS,'model_name':'unit-only'},'config_hash':'unit','assets':{}})
    class Detector:
        def __init__(self,frozen):self.config=frozen['config']
        def inspect(self,image):return dict(**observation(['UNKNOWN']*3),displayTargets=[])
    monkeypatch.setattr(module,'StickerDetector',Detector)
    for width,height,expected in [(1080,1920,200),(1920,1080,200),(1081,1920,422),(1921,1080,422)]:
        ok,jpeg=cv2.imencode('.jpg',np.zeros((height,width,3),np.uint8));assert ok
        response=client.post('/api/v1/realtime/frame?analysis_type=sticker',content=jpeg.tobytes(),
            headers={'Content-Type':'image/jpeg','X-Stream-Id':str(uuid.uuid4()),'X-Frame-Time':'0'})
        assert response.status_code==expected,response.text
        if expected==200:assert (response.json()['width'],response.json()['height'])==(width,height)


def test_live_sticker_action_mode_confirms_actions_before_final(authenticated,monkeypatch):
    import cv2
    import numpy as np
    import backend.sticker_realtime as module
    _,app,client=authenticated
    monkeypatch.setattr(app.state.realtime,'detect_people',lambda *args:{'people':[]})
    config={'targets':TARGETS,'mode':'action','action':ACTION_SETTINGS,
        'model':'models/actions.joblib','model_name':'unit-action'}
    monkeypatch.setattr(module,'snapshot',lambda:{'config':config,'config_hash':'unit-action',
        'assets':{'models/actions.joblib':'unit-hash'}})
    states=[['CLEAR']*3,['CLEAR']*3,['CONTACT','CONTACT','CLEAR'],['CONTACT','CONTACT','CLEAR'],
        ['CLEAR']*3,['CLEAR']*3,['CLEAR','CLEAR','CONTACT'],['CLEAR','CLEAR','CONTACT'],
        ['CLEAR']*3,['CLEAR']*3]
    signatures=[[None]*3,[None]*3,[[0,0],[0,0],None],[[10,10],[10,10],None],[None]*3,[None]*3,
        [None,None,[0,0]],[None,None,[10,10]],[None]*3,[None]*3]
    class Detector:
        def __init__(self,frozen):self.config=frozen['config'];self.index=0
        def inspect(self,image):
            index=self.index;self.index+=1
            targets=[dict(id=t['id'],state=state,score=.95 if state=='CONTACT' else None,
                actionSignature=signature,bbox=None) for t,state,signature in zip(TARGETS,states[index],signatures[index])]
            return dict(mode='action',registered=True,targets=targets,displayTargets=[],reason='unit')
    monkeypatch.setattr(module,'StickerDetector',Detector)
    service=module.RealtimeStickers(app.state.realtime);key=('unit','action')
    ok,jpeg=cv2.imencode('.jpg',np.zeros((480,640,3),np.uint8));assert ok
    for index in range(len(states)):
        result=service.predict(jpeg.tobytes(),stream_key=key,frame_time=index*.25)
    assert result['inspectionMode']=='action'
    final=service.finish(key)
    assert final['compliance']['status']=='PASS'
    assert all(t['state']=='ACTION_DONE' for t in final['targets'])

def test_material_model_is_frozen_together_with_detector_and_reference(tmp_path,monkeypatch):
    import algorithms.stickers as module
    from algorithms.video import AnalysisError
    config=dict(model='patch.pt',reference='reference.jpg',material_model='material.joblib',targets=TARGETS)
    for name in ['patch.pt','reference.jpg','material.joblib']:(tmp_path/name).write_bytes(name.encode())
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'CONFIG',path)
    frozen=module.snapshot();assert set(frozen['assets'])=={'patch.pt','reference.jpg','material.joblib'}
    (tmp_path/'material.joblib').write_bytes(b'changed')
    with pytest.raises(AnalysisError,match='改变'):module.verify(frozen)
