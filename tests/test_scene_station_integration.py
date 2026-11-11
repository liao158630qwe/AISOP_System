"""Real HTTP integration with CPU model doubles for scene scheduling/admission."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time
from types import SimpleNamespace
import uuid

import cv2
import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db import digest,initialize,now,sessions
from test_station_terminals import begin


def wait_until(predicate,timeout=3):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>=deadline:
            pytest.fail('Timed out waiting for request state')
        time.sleep(.002)


class Values:
    def __init__(self,values):self.values=values
    def cpu(self):return self
    def tolist(self):return self.values


@pytest.fixture
def scene_app(tmp_path,monkeypatch):
    import algorithms.gpu as gpu
    import backend.scene_realtime as runtime
    monkeypatch.setattr(gpu,'check_cuda',lambda:dict(available=False,device='cpu-test-double'))
    monkeypatch.setattr(gpu,'require_cuda',lambda *args:0)
    monkeypatch.setattr(runtime,'run_model_lane',lambda entry,fn:entry.executor.submit(fn).result())
    model_path=tmp_path/'pose.pt';model_path.write_bytes(b'not-inference-weights')
    settings=Settings('sqlite:///'+str(tmp_path/'test.db'),tmp_path/'data','admin','integration-password',
        model_path,environment='test',secure_cookie=False,realtime_warmup=False,
        realtime_pool_size=3,station_inference_concurrency=2,scene_model_cache_size=3,
        station_queue_wait_ms=5000)
    app=create_app(settings);initialize(app.state.engine)
    harness=SimpleNamespace(app=app,scenes={},calls=[],loads=[],gates={},entered={},failures={},snapshot_owners=[])
    def snapshot(scene_id,owner):
        harness.snapshot_owners.append((str(scene_id),owner))
        value=harness.scenes.get((owner,str(scene_id)))
        if value is None:raise HTTPException(404,'Scene does not belong to owner')
        return copy.deepcopy(value)
    monkeypatch.setattr(app.state.scenes,'snapshot',snapshot)
    monkeypatch.setattr(app.state.evidence,'observe',lambda *args,**kwargs:None)
    class Model:
        task='detect'
        def __init__(self,frozen):self.key=(frozen['_owner'],frozen['model']['id'])
        def predict(self,images,**options):
            harness.calls.append((self.key,options))
            harness.entered.setdefault(self.key,threading.Event()).set()
            gate=harness.gates.get(self.key)
            if gate is not None:assert gate.wait(10),'Test did not release model gate'
            if self.key in harness.failures:raise harness.failures[self.key]
            return [SimpleNamespace(boxes=SimpleNamespace(cls=Values([0.]),conf=Values([.95]),
                xyxy=Values([[8.,8.,24.,24.]]))) for _ in images]
    def load(frozen):
        harness.loads.append((frozen['_owner'],frozen['model']['id']))
        return Model(frozen)
    monkeypatch.setattr(runtime,'load_model',load)
    ok,payload=cv2.imencode('.jpg',np.full((64,64,3),128,np.uint8));assert ok
    harness.payload=payload.tobytes()
    with TestClient(app) as client:
        response=client.post('/api/v1/login',json={'username':settings.username,'password':settings.password})
        assert response.status_code==200,response.text
        client.headers['X-CSRF-Token']=response.json()['csrf']
        harness.client=client
        try:yield harness
        finally:
            for gate in harness.gates.values():gate.set()


def make_scene(harness,label,*,model_id=None,owner='admin',minimum=1):
    scene_id=str(uuid.uuid4());model_id=model_id or str(uuid.uuid4())
    model=dict(id=model_id,name='model-'+model_id,sha256=hashlib.sha256(model_id.encode()).hexdigest(),
               task='detect',classes={'0':'part'})
    value=dict(id=scene_id,revision=1,sha256=hashlib.sha256(label.encode()).hexdigest(),
        model=model,model_path=str(harness.app.state.settings.data_dir/(model_id+'.pt')),_owner=owner,
        config=dict(name=label,device='cuda:0',sample_fps=4,confirmation_frames=1,imgsz=640,min_sharpness=0,
            rules=[dict(id=label,name=label,class_id=0,kind='required',roi=[0,0,1,1],threshold=.6,
                        min_count=minimum,max_count=None)]))
    harness.scenes[(owner,scene_id)]=value
    harness.entered[(owner,model_id)]=threading.Event()
    return value


def other_owner_headers(harness,owner):
    token=str(uuid.uuid4());csrf=str(uuid.uuid4())
    with harness.app.state.engine.begin() as connection:
        connection.execute(sessions.insert().values(token_hash=digest(token),owner=owner,csrf=csrf,
            expires_at=now(connection)+3600))
    return {'Cookie':'aisop_session='+token,'X-CSRF-Token':csrf}


def station(harness,number,scene=None,*,headers=None,kind='scene'):
    body=dict(workstationId=f'pc-{number}',cameraId=f'camera-{number}',analysisType=kind)
    if scene is not None:body['sceneId']=scene['id']
    response=harness.client.put(f'/api/v1/admin/terminals/station-{number}',json=body,headers=headers)
    assert response.status_code==200,response.text
    credential=response.json()['credential']
    stream=begin(harness.client,credential)
    return credential,stream


def station_headers(credential,stream,seq=0):
    return {'X-Terminal-Key':credential,'Content-Type':'image/jpeg','X-Frame-Seq':str(seq),
        'X-Frame-Time':str(seq/4),'X-Stream-Epoch':str(stream['streamEpoch'])}


def station_send(harness,binding,seq=0):
    credential,stream=binding
    return harness.client.post(f'/api/v1/station-streams/{stream["streamId"]}/frames',
        content=harness.payload,headers=station_headers(credential,stream,seq))


def generic_send(harness,kind='pose',scene=None):
    query='?analysis_type='+kind
    if scene is not None:query+='&scene_id='+scene['id']
    return harness.client.post('/api/v1/realtime/frame'+query,content=harness.payload,
        headers={'Content-Type':'image/jpeg','X-Stream-Id':str(uuid.uuid4()),'X-Frame-Time':'0'})


def test_scene_capacities_and_shared_model_station_turns_keep_rules_and_owners_isolated(scene_app):
    h=scene_app;app=h.app;service=app.state.scene_realtime
    assert service.concurrency==2
    assert app.state.terminals.scheduler.concurrency==2
    assert app.state.terminals.scheduler.group_limits['scene']==2
    assert app.state.realtime_transport.capacities['scene']==2
    assert app.state.realtime_transport.total_capacity==2
    first=make_scene(h,'one-part')
    second=make_scene(h,'two-parts',model_id=first['model']['id'],minimum=2)
    third=make_scene(h,'other-owner',owner='other')
    bindings=[station(h,1,first),station(h,2,second),
              station(h,3,third,headers=other_owner_headers(h,'other'))]
    states=[app.state.terminals.streams[f'station-{index}'] for index in (1,2,3)]
    assert states[0]['model_resource']==states[1]['model_resource']
    assert states[2]['model_resource']!=states[0]['model_resource']
    assert [state['owner'] for state in states]==['admin','admin','other']
    key_a=('admin',first['model']['id']);key_b=('other',third['model']['id'])
    h.gates[key_a]=threading.Event();h.gates[key_b]=threading.Event()
    with ThreadPoolExecutor(max_workers=3) as executor:
        a=executor.submit(station_send,h,bindings[0])
        assert h.entered[key_a].wait(3)
        queued=executor.submit(station_send,h,bindings[1])
        wait_until(lambda:'station-2' in app.state.terminals.scheduler.pending)
        b=executor.submit(station_send,h,bindings[2])
        try:
            assert h.entered[key_b].wait(3)
            assert not queued.done()
            assert len(h.calls)==2
            metrics=app.state.realtime_transport.snapshot()
            assert metrics['active']==metrics['capacity']==2
            assert metrics['services']['scene']['active']==2
            assert app.state.terminals.scheduler.snapshot()['activeResources']==2
        finally:
            h.gates[key_a].set();h.gates[key_b].set()
        responses=[a.result(),queued.result(),b.result()]
    assert [response.status_code for response in responses]==[200,200,200],[r.text for r in responses]
    outputs=[response.json() for response in responses]
    assert [value['sceneId'] for value in outputs]==[scene['id'] for scene in (first,second,third)]
    assert [value['checks'][0]['id'] for value in outputs]==['one-part','two-parts','other-owner']
    assert [value['compliance']['status'] for value in outputs]==['PASS','REVIEW','PASS']
    assert [value['sampleCount'] for value in outputs]==[1,1,1]
    assert len(h.loads)==2
    evidence=[service.streams.items[state['key']]['evidence'] for state in states]
    assert len({id(value) for value in evidence})==3
    assert [value.config['rules'][0]['min_count'] for value in evidence]==[1,2,1]
    assert generic_send(h,'scene',third).status_code==404
    assert app.state.realtime_transport.snapshot()['active']==0
    assert app.state.terminals.scheduler.snapshot()['activeResources']==0
    # Rechecking a changed revision must not mutate the already-frozen resource.
    frozen_resource=states[0]['model_resource']
    h.scenes[('admin',first['id'])]['model']['id']=str(uuid.uuid4())
    h.scenes[('admin',first['id'])]['sha256']='changed-revision'
    assert states[0]['revision_check']()=='changed-revision'
    assert states[0]['model_resource']==frozen_resource
    calls=len(h.calls)
    assert station_send(h,bindings[0],1).status_code==409
    assert len(h.calls)==calls
    assert states[0]['model_resource']==frozen_resource


def test_realtime_and_station_routes_share_one_total_budget_across_kinds(scene_app,monkeypatch):
    h=scene_app;app=h.app
    scene=make_scene(h,'scene-part');scene_binding=station(h,1,scene)
    pose_binding=station(h,2,kind='pose')
    key=('admin',scene['model']['id']);h.gates[key]=threading.Event()
    pose_entered=threading.Event();pose_gate=threading.Event()
    def pose(*args,**kwargs):
        pose_entered.set();assert pose_gate.wait(10)
        return {'people':[]}
    monkeypatch.setattr(app.state.realtime,'predict',pose)
    with ThreadPoolExecutor(max_workers=2) as executor:
        station_work=executor.submit(station_send,h,scene_binding)
        assert h.entered[key].wait(3)
        generic_work=executor.submit(generic_send,h)
        try:
            assert pose_entered.wait(3)
            assert app.state.realtime_transport.snapshot()['active']==2
            for kind in ('pose','screw','sticker','scene'):
                assert generic_send(h,kind,scene if kind=='scene' else None).status_code==429
            assert station_send(h,pose_binding).status_code==429
        finally:
            h.gates[key].set();pose_gate.set()
        assert station_work.result().status_code==200
        assert generic_work.result().status_code==200
    assert app.state.realtime_transport.snapshot()['active']==0
    assert station_send(h,pose_binding,1).status_code==200
    assert generic_send(h,'scene',scene).status_code==200


@pytest.mark.parametrize('endpoint',['station','realtime'])
def test_model_failure_returns_shared_budget_and_later_frame_recovers(scene_app,endpoint):
    h=scene_app;scene=make_scene(h,'recover');binding=station(h,1,scene)
    key=('admin',scene['model']['id'])
    h.failures[key]=HTTPException(503,'Expected simulated model failure')
    failed=station_send(h,binding) if endpoint=='station' else generic_send(h,'scene',scene)
    assert failed.status_code==503,failed.text
    assert h.app.state.realtime_transport.snapshot()['active']==0
    assert h.app.state.scene_realtime.cache_metrics()['activeModels']==0
    assert h.app.state.terminals.scheduler.snapshot()['activeResources']==0
    del h.failures[key]
    recovered=station_send(h,binding,1) if endpoint=='station' else generic_send(h,'scene',scene)
    assert recovered.status_code==200,recovered.text
    assert recovered.json()['sampleCount']==1


