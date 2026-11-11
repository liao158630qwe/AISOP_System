"""Quality gates for the new screw task; inference metrics live in the run bundle."""
import copy
import json
import uuid

import numpy as np
import pytest
from sqlalchemy import select

from algorithms.screws import (ScrewObservationHistory, compliance, final_observations,
                               sequence_profiles, validate_sequence, verify)
from algorithms.video import AnalysisError
from backend.db import runs


TARGETS=[{'id':'S1','name':'位置1','point':[100,100]}, {'id':'S2','name':'位置2','point':[150,150]}]
SEQUENCE_TARGETS=[{'id':'T1','name':'工序1','point':[100,100]},
    {'id':'T2','name':'工序2','point':[150,150]},
    {'id':'T3','name':'工序3','point':[200,200]},
    {'id':'P1','name':'预装件','point':[250,250],'preassembled':True}]
SEQUENCE=['T1','T2','T3']


def frame(t,states,registered=True):
    return {'time':t,'inspection':{'registered':registered,'targets':[
        dict(**target,state=state,score=.9 if state!='UNKNOWN' else None)
        for target,state in zip(TARGETS,states)]}}


def sequence_frame(t,states,scores=None):
    scores=scores or {}
    return {'time':t,'inspection':{'registered':True,'targets':[
        dict(**target,state=states.get(target['id'],'UNKNOWN'),
             score=scores.get(target['id'],.9) if states.get(target['id'])!='UNKNOWN' else None)
        for target in SEQUENCE_TARGETS]}}


def feed(history,time,states,scores=None):
    return history.update(time,sequence_frame(time,states,scores)['inspection'])


def test_no_detection_is_unknown_and_one_frame_does_not_confirm():
    output,t=final_observations([frame(0,['PRESENT','UNKNOWN'])],TARGETS)
    assert [p['state'] for p in output]==['UNKNOWN','UNKNOWN']
    assert t==0


def test_confirmed_state_survives_unknown_and_single_contrary_frame():
    frames=[frame(0,['PRESENT','PRESENT']),frame(.25,['PRESENT','PRESENT']),
            frame(4,['EMPTY','UNKNOWN']),frame(4.25,['UNKNOWN','UNKNOWN'])]
    output,t=final_observations(frames,TARGETS)
    assert [p['state'] for p in output]==['PRESENT','PRESENT']
    assert output[0]['lastSeen']==.25
    frames += [frame(5,['EMPTY','UNKNOWN']),frame(5.25,['EMPTY','UNKNOWN'])]
    output,t=final_observations(frames,TARGETS)
    assert [p['state'] for p in output]==['EMPTY','PRESENT']
    assert output[0]['lastSeen']==5


def test_lost_registration_does_not_erase_confirmed_history():
    output,_=final_observations([frame(0,['PRESENT','PRESENT']),frame(.25,['PRESENT','PRESENT']),
        frame(.5,['UNKNOWN','UNKNOWN'],False),frame(.75,['PRESENT','UNKNOWN'])],TARGETS)
    assert [p['state'] for p in output]==['PRESENT','PRESENT']


def test_confirmed_history_survives_a_long_unobserved_gap():
    output,assessment=final_observations([frame(0,['PRESENT','EMPTY']),frame(.25,['PRESENT','EMPTY']),
        frame(30,['UNKNOWN','UNKNOWN']),frame(31,['UNKNOWN','UNKNOWN'],False)],TARGETS)
    assert [p['state'] for p in output]==['PRESENT','EMPTY']
    assert [p['lastSeen'] for p in output]==[.25,.25]
    assert assessment==30


def test_shared_history_keeps_sticker_wrong_state():
    frames=[frame(0,['PRESENT','EMPTY']),frame(.25,['PRESENT','EMPTY'])]
    frames[1]['inspection']['targets'][1]['state']='WRONG'
    frames.append(frame(.5,['PRESENT','WRONG']))
    output,_=final_observations(frames,TARGETS)
    assert [p['state'] for p in output]==['PRESENT','WRONG']


def test_cumulative_confirmation_passes_regardless_of_configured_order():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    feed(history,0,{'T1':'UNKNOWN','T2':'UNKNOWN','T3':'PRESENT','P1':'PRESENT'})
    feed(history,.25,{'T1':'UNKNOWN','T2':'UNKNOWN','T3':'PRESENT','P1':'PRESENT'})
    assert history.sequence()['status']=='PENDING'
    assert history.sequence()['completed']==['T3']
    feed(history,.5,{'T1':'PRESENT','T2':'UNKNOWN','T3':'PRESENT','P1':'PRESENT'})
    feed(history,.75,{'T1':'PRESENT','T2':'UNKNOWN','T3':'PRESENT','P1':'PRESENT'})
    assert set(history.sequence()['completed'])=={'T1','T3'}
    feed(history,1.0,{'T1':'PRESENT','T2':'PRESENT','T3':'PRESENT','P1':'PRESENT'})
    feed(history,1.25,{'T1':'PRESENT','T2':'PRESENT','T3':'PRESENT','P1':'PRESENT'})
    sequence=history.sequence()
    assert sequence['status']=='PASS' and set(sequence['completed'])==set(SEQUENCE)
    targets=history.aggregate(final=True)
    assert {t['id']:t['sequenceState'] for t in targets}=={
        'T1':'COMPLETED','T2':'COMPLETED','T3':'COMPLETED','P1':'PREASSEMBLED_OK'}
    assert compliance(targets,history.sequence(final=True),final=True)['status']=='PASS'


def test_finished_product_at_start_passes_without_installation_baseline():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    all_present={identity:'PRESENT' for identity in ['T1','T2','T3','P1']}
    for time in (0,.25,.5):
        feed(history,time,all_present)
    sequence=history.sequence(final=True)
    assert sequence['status']=='PASS'
    assert sequence['operationsCompleted']==3
    assert compliance(history.aggregate(final=True),sequence,final=True)['status']=='PASS'


def test_unconfigured_sequence_uses_position_list_and_passes():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={})
    all_present={identity:'PRESENT' for identity in ['T1','T2','T3','P1']}
    for time in (0,.25):
        feed(history,time,all_present)
    sequence=history.sequence(final=True)
    assert sequence['configured'] is True
    assert sequence['verificationMode']=='visual-cumulative'
    assert sequence['order']==['T1','T2','T3']
    assert sequence['status']=='PASS'


def test_invalid_sequence_only_changes_display_order():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':['T1','T2']})
    all_present={identity:'PRESENT' for identity in ['T1','T2','T3','P1']}
    for time in (0,.25):
        feed(history,time,all_present)
    sequence=history.sequence(final=True)
    assert sequence['order']==['T1','T2','T3']
    assert sequence['status']=='PASS'
    assert validate_sequence(['T1','T2'],SEQUENCE_TARGETS)
    assert validate_sequence(['T1','T2','T3','P1'],SEQUENCE_TARGETS)


def test_confirmed_empty_hole_fails_the_cycle():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    for time in (0,.25):
        feed(history,time,{'T1':'PRESENT','T2':'EMPTY','T3':'PRESENT','P1':'PRESENT'})
    sequence=history.sequence()
    assert sequence['status']=='PENDING' and sequence['empty']==['T2']
    final=history.sequence(final=True)
    assert final['status']=='FAIL' and 'T2' in final['reason']
    assert compliance(history.aggregate(final=True),final,final=True)['status']=='FAIL'


def test_completed_position_disappearing_fails_the_cycle():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    for time in (0,.25):
        feed(history,time,{'T1':'PRESENT','T2':'EMPTY','T3':'EMPTY','P1':'PRESENT'})
    assert history.sequence()['completed']==['T1']
    for time in (.5,.75):
        feed(history,time,{'T1':'EMPTY','T2':'EMPTY','T3':'EMPTY','P1':'PRESENT'})
    sequence=history.sequence()
    assert sequence['status']=='PENDING' and 'T1' in sequence['empty']
    final=history.sequence(final=True)
    assert final['status']=='FAIL' and 'T1' in final['reason']


def test_missing_preassembled_screw_fails_and_is_not_an_operation():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    for time in (0,.25):
        feed(history,time,{'T1':'EMPTY','T2':'EMPTY','T3':'EMPTY','P1':'EMPTY'})
    sequence=history.sequence()
    assert sequence['status']=='PENDING' and sequence['preassembled']['P1']=='PREASSEMBLED_MISSING'
    assert 'P1' not in sequence['order']
    final=history.sequence(final=True)
    assert final['status']=='FAIL' and 'P1' in final['reason']


def test_empty_position_can_be_installed_later_without_early_failure():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    for time in (0,.25):
        feed(history,time,{'T1':'PRESENT','T2':'EMPTY','T3':'PRESENT','P1':'PRESENT'})
    assert history.sequence()['status']=='PENDING'
    for time in (.5,.75):
        feed(history,time,{'T1':'PRESENT','T2':'PRESENT','T3':'PRESENT','P1':'PRESENT'})
    assert history.sequence()['status']=='PASS'
    assert history.sequence(final=True)['status']=='PASS'


def test_preassembled_position_can_be_confirmed_later_without_early_failure():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    all_present={'T1':'PRESENT','T2':'PRESENT','T3':'PRESENT','P1':'PRESENT'}
    for time in (0,.25):
        feed(history,time,{**all_present,'P1':'EMPTY'})
    assert history.sequence()['status']=='PENDING'
    for time in (.5,.75):
        feed(history,time,all_present)
    assert history.sequence()['status']=='PASS'


def test_unknown_positions_at_finish_review_instead_of_passing():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    for time in (0,.25):
        feed(history,time,{'T1':'PRESENT','T2':'UNKNOWN','T3':'PRESENT','P1':'PRESENT'})
    assert history.sequence()['status']=='PENDING'
    sequence=history.sequence(final=True)
    assert sequence['status']=='REVIEW'
    assert sequence['operationsCompleted']==2
    assert 'T2' in sequence['reason']
    assert compliance(history.aggregate(final=True),sequence,final=True)['status']=='REVIEW'


def test_occlusion_after_confirmation_preserves_all_positions():
    history=ScrewObservationHistory(SEQUENCE_TARGETS,sequences={'original':SEQUENCE})
    all_present={identity:'PRESENT' for identity in ['T1','T2','T3','P1']}
    for time in (0,.25):
        feed(history,time,all_present)
    assert history.sequence()['status']=='PASS'
    feed(history,.5,{identity:'UNKNOWN' for identity in all_present})
    feed(history,.75,{identity:'UNKNOWN' for identity in all_present})
    sequence=history.sequence()
    assert sequence['status']=='PASS' and sequence['positionsConfirmed']==4


def test_realtime_history_survives_occlusion_and_finish(monkeypatch):
    import cv2
    import backend.screw_realtime as module

    def inspection(states,registered=True):
        return {'registered':registered,'targets':[dict(**target,state=state,
            score=.9 if state!='UNKNOWN' else None,bbox=None) for target,state in zip(TARGETS,states)]}

    samples=iter([inspection(['EMPTY','UNKNOWN']),inspection(['PRESENT','UNKNOWN']),
        inspection(['PRESENT','UNKNOWN']),
        inspection(['UNKNOWN','UNKNOWN'],False),inspection(['UNKNOWN','UNKNOWN'])])

    class FakeDetector:
        def __init__(self,frozen):
            self.config=frozen['config'];self.device='cpu'
        def inspect(self,image):
            return copy.deepcopy(next(samples))

    monkeypatch.setattr(module,'ScrewDetector',FakeDetector)
    monkeypatch.setattr(module,'snapshot',lambda:{'config':{'targets':TARGETS,'model_name':'unit',
        'sequence':['S1','S2']},
        'config_hash':'unit','assets':{}})
    ok,jpeg=cv2.imencode('.jpg',np.zeros((16,16,3),np.uint8));assert ok
    from types import SimpleNamespace
    pose=SimpleNamespace(detect_people=lambda *args:{'people':[],'skeleton':{}})
    service=module.RealtimeScrews(pose);key=('owner','stream')
    first=service.predict(jpeg.tobytes(),stream_key=key,frame_time=0)
    second=service.predict(jpeg.tobytes(),stream_key=key,frame_time=.25)
    third=service.predict(jpeg.tobytes(),stream_key=key,frame_time=.5)
    assert first['aggregateTargets'][0]['state']=='UNKNOWN'
    assert second['aggregateTargets'][0]['state']=='UNKNOWN'
    assert third['aggregateTargets'][0]['state']=='PRESENT'
    assert third['targets'][0]['state']=='PRESENT'
    assert third['sequence']['completed']==['S1'] and third['sequence']['current']=='S2'
    lost=service.predict(jpeg.tobytes(),stream_key=key,frame_time=3.5)
    assert lost['targets'][0]['state']=='UNKNOWN'
    assert lost['aggregateTargets'][0]['state']=='PRESENT'
    assert lost['aggregateTargets'][0]['lastSeen']==.5
    final=service.finish(key)
    assert final['targets'][0]['state']=='PRESENT'
    assert final['sequence']['configured'] is True
    assert final['sequence']['completed']==['S1']
    assert final['compliance']['status']=='REVIEW'


def test_switching_calibration_does_not_merge_observations_or_target_names():
    first=frame(0,['PRESENT','PRESENT'])
    first['inspection']['calibrationId']='original'
    second=frame(.25,['PRESENT'],True)
    second['inspection']['calibrationId']='overhead'
    second['inspection']['targets'][0]['name']='新视角位置'
    output,_=final_observations([first,second],TARGETS)
    assert len(output)==1 and output[0]['name']=='新视角位置'
    assert output[0]['state']=='UNKNOWN'
    third=copy.deepcopy(second);third['time']=.5
    output,_=final_observations([first,second,third],TARGETS)
    assert output[0]['state']=='PRESENT'


def test_snapshot_freezes_all_calibration_images(tmp_path,monkeypatch):
    import algorithms.screws as module
    config={'model':'m.pt','reference':'old.jpg','calibrations':[{'reference':'new.jpg','target_models':{'S02':'specialist.pt'}}]}
    for name in ['m.pt','old.jpg','new.jpg','specialist.pt']:(tmp_path/name).write_bytes(name.encode())
    (tmp_path/'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'CONFIG',tmp_path/'config.json')
    frozen=module.snapshot()
    assert set(frozen['assets'])=={'m.pt','old.jpg','new.jpg','specialist.pt'}
    (tmp_path/'specialist.pt').write_bytes(b'changed')
    with pytest.raises(AnalysisError,match='改变'):verify(frozen)


def test_multiview_uses_current_features_and_clears_every_old_detection(monkeypatch):
    from types import SimpleNamespace
    import algorithms.screw_multiview as module
    from algorithms.screws import current_display
    created=[]
    class FakeRefined:
        def __init__(self,frozen,model=None,registration=None):
            self.config=frozen['config'];self.model=model if model is not None else object()
            self.reference=None;self.device='cpu';self.registration_size=(2,2)
            self.registration=registration or object()
            created.append(self)
        def extract(self,image):return None,int(image[0,0,0])
        def localize(self,key,desc):
            expected=22 if self.config.get('id')=='overhead' else 11
            return {'componentInliers':[10],'inliers':20} if desc==expected else None
        def inspect(self,image,registration):
            return {'registered':True,'targets':[dict(**t,state='PRESENT',score=.9,bbox=[.1,.1,.2,.2])
                for t in self.config['targets']]}
    monkeypatch.setattr(module,'RefinedScrewDetector',FakeRefined)
    detector=module.MultiViewScrewDetector({'config':{'targets':TARGETS,
        'calibrations':[{'id':'overhead','targets':TARGETS[:1]}]}})
    assert created[0].model is created[1].model
    first=detector.inspect(np.full((2,2,3),11,np.uint8))
    second=detector.inspect(np.full((2,2,3),22,np.uint8))
    lost=current_display(detector.inspect(np.zeros((2,2,3),np.uint8)))
    assert first['calibrationId']=='original' and len(first['targets'])==2
    assert second['calibrationId']=='overhead' and len(second['targets'])==1
    assert lost['registered'] is False and lost['displayTargets']==[]
    assert all(t['state']=='UNKNOWN' and t['score'] is None and t['bbox'] is None for t in lost['targets'])
    assert all('point' not in t and 'detectedBbox' not in t for t in lost['targets'])


def test_target_threshold_retries_only_configured_position(monkeypatch):
    import cv2
    from algorithms.screw_refined import RefinedScrewDetector
    from algorithms import cuda_vision as vision
    monkeypatch.setattr('algorithms.screw_refined.predict_cuda',lambda model,images,**kwargs:model.predict(images,**kwargs))

    class Values:
        def __init__(self,values): self.values=np.asarray(values)
        def cpu(self): return self
        def tolist(self): return self.values.tolist()

    class Prediction:
        def __init__(self,rows):
            import torch
            self.boxes=type('Boxes',(),{
                'data':torch.tensor([row[:4]+[row[5],row[4]] for row in rows],device='cuda').reshape(-1,6),
                'xyxy':Values([row[:4] for row in rows]),
                'cls':Values([row[4] for row in rows]),
                'conf':Values([row[5] for row in rows])})()

    class Model:
        def predict(self,crops,**kwargs):
            if kwargs['conf']>=.45: return [Prediction([]) for _ in crops]
            return [Prediction([[35,35,45,45,0,.35]]) for _ in crops]

    detector=object.__new__(RefinedScrewDetector)
    detector.config={'targets':[{'id':'S03','point':[100,100]},{'id':'S04','point':[150,150]}],
        'threshold':.45,'target_thresholds':{'S03':.3},'assignment_radius':20,
        'local_context_threshold':-1,'empty_threshold':.65,'detector_input':256,
        'component_anchors':[[100,100,110,110]],'component_threshold':.6,'minimum_visible_components':1}
    detector.registration_size=(960,540)
    detector.reference_gray=np.zeros((1080,1920),np.uint8)
    import torch
    detector.reference_gray_gpu=torch.zeros((1080,1920),device='cuda',dtype=torch.uint8)
    detector.reference_small=vision.resize(detector.reference_gray_gpu,(960,540))
    detector.model=Model();detector.target_models={};detector.device='cpu'
    registration={'matrix':np.eye(3),'H':np.eye(3),'inliers':20,'componentInliers':[10]}
    result=detector.inspect(np.zeros((200,200,3),np.uint8),registration)
    targets={t['id']:t for t in result['targets']}
    assert targets['S03']['state']=='PRESENT' and targets['S03']['score']==.35
    assert targets['S04']['state']=='UNKNOWN'
    assert result['registered'] is True


def test_model_hash_change_fails_closed(tmp_path,monkeypatch):
    import algorithms.screws as module
    monkeypatch.setattr(module,'ROOT',tmp_path)
    (tmp_path/'model.pt').write_bytes(b'changed')
    with pytest.raises(AnalysisError,match='改变'):
        verify({'assets':{'model.pt':'old-hash'}})


def test_new_home_and_retired_catalog(context):
    _,_,client=context
    response=client.get('/')
    assert response.status_code==200
    assert 'src="screw-dashboard.js?v=' in response.text
    assert 'class="dashboard"' in response.text
    assert 'generic-actions' not in response.text
    assert client.get('/api/v1/stations').json()=={'items':[]}
    assert client.get('/api/v1/action-workflows').json()=={'items':[]}
    assert client.get('/api/v1/health').json()['capabilities']['screwInspection']


def test_frontend_assets_revalidate_as_one_version(context):
    import hashlib
    from backend.config import ROOT
    _,_,client=context
    response=client.get('/')
    assert response.headers['cache-control']=='no-cache'
    for name in ['app.js','styles.css','screw-dashboard.js','screw-dashboard.css']:
        revision=hashlib.sha256((ROOT/'frontend'/name).read_bytes()).hexdigest()[:12]
        assert f'{name}?v={revision}' in response.text
        assert client.get(f'/{name}?v={revision}').headers['cache-control']=='no-cache'


@pytest.mark.parametrize('states,status',[(['PRESENT','PRESENT'],'PASS'),(['EMPTY','PRESENT'],'FAIL'),
    (['UNKNOWN','PRESENT'],'REVIEW'),(['EMPTY','UNKNOWN'],'FAIL')])
def test_visual_compliance_has_explicit_outcomes(states,status):
    from algorithms.screws import compliance
    assert compliance([{'id':str(i),'state':s} for i,s in enumerate(states)])['status']==status


def test_current_frame_has_no_boxes_after_detection_is_lost():
    from algorithms.screws import current_display
    visible=frame(0,['PRESENT','EMPTY'])['inspection']
    for target in visible['targets']:target['bbox']=[.1,.1,.2,.2]
    assert len(current_display(visible)['displayTargets'])==2
    observation=frame(0,['UNKNOWN','UNKNOWN'],False)['inspection']
    for _ in range(3):
        assert current_display(copy.deepcopy(observation))['displayTargets']==[]


def test_uncertain_current_candidates_do_not_draw_position_guides():
    from algorithms.screws import current_display
    observation=frame(0,['PRESENT','UNKNOWN'])['inspection']
    for target in observation['targets']:target['bbox']=[.1,.1,.2,.2]
    observation['targets'][0]['detectedBbox']=[.12,.13,.16,.17]
    boxes=current_display(observation)['displayTargets']
    assert len(boxes)==1 and boxes[0]['bbox']==[.12,.13,.16,.17]
    observation['registered']=False
    assert current_display(observation)['displayTargets']==[]


def test_selected_algorithm_dispatches_the_frozen_specialized_model(monkeypatch):
    from types import SimpleNamespace
    import algorithms.screws as module
    import algorithms.screw_refined as refined
    frozen={'config':{'algorithm':'local-roi-v2','model':'models/selected.pt'},'assets':{}}
    received=[]
    class FakeRefined:
        def __init__(self,value):
            received.append(value);self.reference=None;self.model='selected';self.device='cpu'
        def inspect(self,image):return {'registered':False,'targets':[],'algorithm':'local-roi-v2'}
    monkeypatch.setattr(refined,'RefinedScrewDetector',FakeRefined)
    detector=module.ScrewDetector(frozen)
    assert received==[frozen] and detector.model=='selected'
    assert detector.inspect(None)['algorithm']=='local-roi-v2'


def test_live_finish_is_owner_isolated_and_requires_frames(authenticated):
    _,_,client=authenticated
    identity=str(uuid.uuid4())
    assert client.post(f'/api/v1/realtime/streams/{identity}/finish').status_code==404
    assert client.post('/api/v1/realtime/frame?analysis_type=screw',content=b'x',
        headers={'Content-Type':'image/jpeg'}).status_code==422


def test_screw_run_freezes_dedicated_assets(authenticated,video_bytes,monkeypatch):
    import algorithms.screws as module
    _,app,client=authenticated
    monkeypatch.setattr(module,'snapshot',lambda:{'config':{'model':'models/screw.pt'},
        'assets':{'models/screw.pt':'dedicated-hash'},'config_hash':'config-hash'})
    media=client.post('/api/v1/media?filename=sample.avi',content=video_bytes).json()
    response=client.post('/api/v1/analysis-runs',headers={'Idempotency-Key':str(uuid.uuid4())},
        json={'media_id':media['id'],'options':{'analysis_type':'screw'}})
    assert response.status_code==202,response.text
    with app.state.engine.connect() as c:
        job=c.execute(select(runs).where(runs.c.id==response.json()['id'])).mappings().one()
    assert job['model_hash']=='dedicated-hash'
    assert job['options']['screw_config']['config_hash']=='config-hash'
    for extra in [{'workflow_id':'old'},{'station_id':'s1'},{'crop':[0,0,32,32]}]:
        response=client.post('/api/v1/analysis-runs',headers={'Idempotency-Key':str(uuid.uuid4())},
            json={'media_id':media['id'],'options':{'analysis_type':'screw',**extra}})
        assert response.status_code==422


def test_history_can_filter_new_screw_tasks(authenticated,video_bytes,monkeypatch):
    import algorithms.screws as module
    from conftest import new_run
    _,_,client=authenticated
    monkeypatch.setattr(module,'snapshot',lambda:{'config':{'model':'models/screw.pt'},
        'assets':{'models/screw.pt':'dedicated-hash'},'config_hash':'config-hash'})
    new_run(client,video_bytes,key='old-pose',options={'analysis_type':'pose'})
    current=new_run(client,video_bytes,key='new-screw',options={'analysis_type':'screw'})
    response=client.get('/api/v1/analysis-runs?analysis_type=screw')
    assert response.json()['total']==1
    assert response.json()['items'][0]['id']==current['id']
