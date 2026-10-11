import copy
import cv2
import numpy as np
import pytest
from algorithms.stations import Station, profiles, snapshot
from algorithms.inspection import Inspector
from algorithms.process_rules import evaluate, ProcessSpec, order_relation, duration_relation
from conftest import new_run


def clip_profile(**kwargs):
    config={'id':'test','name':'测试工位','version':'test-1','approved':True,
            'fixed_fixture':True,'min_sharpness':0,
            'checks':[{'id':'clip','name':'黄色卡扣','kind':'presence','expected':{'yellow_clip':1},
                       'targets':[{'id':'clip-1','category':'yellow_clip','roi':[0,0,1,1],
                                   'method':'hsv','min_area':.1,'max_area':.6}]}]}
    config.update(kwargs)
    return {'config':config,'assets':{},'sha256':'test-only'}


def test_catalog_matches_document():
    catalog=profiles()
    assert len(catalog)==6
    assert sum(len(s.checks) for s in catalog)==11
    assert catalog[4].checks[1].expected=={'screw':8}
    for s in catalog:
        result=Inspector(snapshot(s.id)).inspect(np.zeros((50,50,3),np.uint8))
        assert result['verdict']=='REVIEW'
        assert all(c['state']=='REVIEW' for c in result['checks'])


def test_color_candidate_is_not_completion_and_no_detection_is_unknown():
    frame=np.zeros((100,100,3),np.uint8)
    frame[20:70,20:70]=(0,255,255)
    inspector=Inspector(clip_profile())
    result=inspector.inspect(frame)
    assert result['checks'][0]['targets'][0]['state']=='CANDIDATE'
    assert result['verdict']=='REVIEW'
    result=inspector.inspect(np.zeros_like(frame))
    assert result['checks'][0]['targets'][0]['state']=='UNKNOWN'


@pytest.mark.parametrize('changes',[{'approved':False},{'fixed_fixture':False},{'min_sharpness':1e8}])
def test_configuration_and_visibility_gate(changes):
    frame=np.zeros((100,100,3),np.uint8);frame[20:70,20:70]=(0,255,255)
    assert Inspector(clip_profile(**changes)).inspect(frame)['checks'][0]['targets'][0]['state']=='UNKNOWN'


def test_bad_roi_or_target_count_rejected():
    config=clip_profile()['config']
    config['checks'][0]['targets'][0]['roi']=[.5,0,.4,1]
    with pytest.raises(ValueError):Station.model_validate(config)
    config=clip_profile()['config'];config['checks'][0]['expected']['yellow_clip']=2
    with pytest.raises(ValueError):Station.model_validate(config)


def test_template_matching(monkeypatch,tmp_path):
    template=np.random.default_rng(123).integers(0,255,(16,16),dtype=np.uint8)
    path=tmp_path/'template.png';cv2.imwrite(str(path),template)
    monkeypatch.setattr('algorithms.inspection.asset_path',lambda name:path)
    profile=clip_profile()
    profile['config']['checks'][0]['targets'][0].update(method='template',asset='test.png',threshold=.99)
    frame=np.zeros((64,64,3),np.uint8);frame[20:36,20:36]=cv2.cvtColor(template,cv2.COLOR_GRAY2BGR)
    assert Inspector(profile).inspect(frame)['checks'][0]['targets'][0]['state']=='CANDIDATE'


def test_pose_weights_cannot_be_used_as_parts_detector(monkeypatch,tmp_path):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules,'ultralytics',SimpleNamespace(YOLO=lambda path:SimpleNamespace(task='pose')))
    monkeypatch.setattr('algorithms.inspection.asset_path',lambda name:tmp_path/'pose.pt')
    profile=clip_profile()
    profile['config']['checks'][0]['targets'][0].update(method='yolo',asset='pose.pt',class_name='yellow_clip')
    result=Inspector(profile).inspect(np.zeros((64,64,3),np.uint8))
    assert result['checks'][0]['targets'][0]['state']=='UNKNOWN'
    assert '人体姿态权重' in result['checks'][0]['targets'][0]['reason']


def test_overlapping_slots_rejected():
    config=clip_profile()['config'];check=config['checks'][0]
    check['expected']['yellow_clip']=2
    duplicate=copy.deepcopy(check['targets'][0]);duplicate['id']='clip-2'
    check['targets'].append(duplicate)
    with pytest.raises(ValueError):Station.model_validate(config)


def test_station_task_freezes_config_and_rejects_unknown(authenticated,video_bytes):
    _,app,client=authenticated
    from backend.db import runs
    from sqlalchemy import select
    job=new_run(client,video_bytes,options={'station_id':'s5'})
    with app.state.engine.connect() as c:
        options=c.execute(select(runs.c.options).where(runs.c.id==job['id'])).scalar_one()
    assert options['inspection_config']['config']['id']=='s5'
    assert len(options['inspection_config']['sha256'])==64
    response=client.post('/api/v1/analysis-runs',json={'media_id':job['media_id'],'options':{'station_id':'missing'}},headers={'Idempotency-Key':'invalid-station'})
    assert response.status_code==422
    response=client.post('/api/v1/realtime/frame?station_id=missing',content=b'jpeg',headers={'Content-Type':'image/jpeg'})
    assert response.status_code==422


def spec():
    return dict(version='test',approved=True,targets=['a','b'],programs={'a':'P1','b':'P1'},precedes=[['a','b']])


def attempt(id,target,t):
    return dict(id=id,cycle='product1',target=target,program='P1',result='OK',start=[t,t],end=[t+1,t+1],verified=True)


def run(events,**kwargs):
    return evaluate(spec(),events,cycle='product1',checkpoint=True,coverage_complete=True,**kwargs)


def test_rules_pass_order_violation_and_ambiguous_time():
    assert run([attempt('1','a',0),attempt('2','b',2)])['verdict']=='PASS'
    assert run([attempt('1','a',2),attempt('2','b',0)])['verdict']=='FAIL'
    assert order_relation([1,3],[2,4])=='UNKNOWN'
    assert duration_relation([0,1],[3,4],[2.5,3.5])=='UNKNOWN'
    assert duration_relation([0,0],[4,4],[1,2])=='FALSE'


def test_same_position_not_counted_twice_and_missing_coverage_is_unknown():
    events=[attempt('1','a',0),attempt('2','a',2)]
    result=run(events)
    assert result['coveredTargets']==['a']
    assert result['verdict']=='FAIL'
    assert evaluate(spec(),events,cycle='product1',checkpoint=True,coverage_complete=False)['verdict']=='REVIEW'


def test_wrong_product_program_unverified_and_revision_conflict():
    for changes in [{'cycle':'other'},{'program':'P2'},{'verified':False}]:
        b=attempt('2','b',2);b.update(changes)
        assert run([attempt('1','a',0),b])['verdict']=='REVIEW'
    a=attempt('1','a',0);other=copy.deepcopy(a);other['result']='NG'
    with pytest.raises(ValueError):run([a,other])
    assert run([a,a,attempt('2','b',2)])['verdict']=='PASS'


def test_invalidated_attempt_does_not_complete_and_history_ng_retained():
    a=attempt('1','a',0);a['result']='NG'
    revised=copy.deepcopy(a);revised.update(revision=2,valid=False)
    result=run([a,revised,attempt('2','b',2)])
    assert result['verdict']=='REVIEW'
    assert result['historyNG']==['1']


def test_cyclic_or_empty_spec_rejected():
    config=spec();config['precedes'].append(['b','a'])
    with pytest.raises(ValueError):ProcessSpec.model_validate(config)
    config=spec();config['targets']=[]
    with pytest.raises(ValueError):ProcessSpec.model_validate(config)
