"""Same-frame people in sticker tasks, independent of workpiece registration and verdicts."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import torch
from sqlalchemy import select

from backend.db import runs
from backend.worker import execute_one
from conftest import new_run


TARGETS=[dict(id='T1',name='三角1',point=[10,10],expected='triangle'),
         dict(id='T2',name='三角2',point=[20,20],expected='triangle'),
         dict(id='R1',name='方形1',point=[30,30],expected='rectangle')]


def pose_result(frame,present=True):
    h,w=frame.shape[:2]
    return SimpleNamespace(
        boxes=SimpleNamespace(xyxy=torch.tensor([[w*.2,h*.1,w*.8,h*.9]]),
                              conf=torch.tensor([.9]),cls=torch.tensor([0.])),
        keypoints=SimpleNamespace(data=torch.tensor(np.tile([w*.5,h*.5,.9],(1,17,1)))) if present else None,
        speed={'inference':1.})


def inspection():
    return dict(registered=False,inferenceMs=1.,displayTargets=[],
                targets=[dict(**t,state='UNKNOWN',score=None,bbox=None) for t in TARGETS])


@pytest.mark.parametrize('size',[(96,64),(64,96)])
def test_sticker_realtime_people_use_same_frame_even_without_workpiece(monkeypatch,size):
    import backend.sticker_realtime as module
    from backend.realtime import RealtimeDetector
    import algorithms.cuda_lane
    import algorithms.cuda_vision
    import algorithms.gpu
    monkeypatch.setattr(algorithms.cuda_lane, 'run_in_lane', lambda owner, fn, ready=None: fn())
    monkeypatch.setattr(algorithms.cuda_vision, 'decode', lambda payload: cv2.imdecode(np.frombuffer(payload,np.uint8),cv2.IMREAD_COLOR))
    monkeypatch.setattr(algorithms.gpu, 'require_cuda', lambda *args: 0)
    monkeypatch.setattr(torch.cuda, 'Event', lambda *args, **kwargs: SimpleNamespace(record=lambda:None,synchronize=lambda:None))
    received,inspected=[],[]
    class Pose:
        def predict(self,frame,**kwargs):
            assert kwargs['classes']==[0]
            received.append(frame)
            return [pose_result(frame,bool(frame.any()))]
    pose=RealtimeDetector(Path('unused'),backend='yolo',pool_size=1)
    pose.model=Pose();pose.model_kind='yolo'
    class Sticker:
        def __init__(self,frozen):self.config=frozen['config']
        def inspect(self,frame):
            inspected.append(frame)
            return inspection()
    monkeypatch.setattr(module,'StickerDetector',Sticker)
    monkeypatch.setattr(module,'snapshot',lambda:{'config':{'targets':TARGETS,'model_name':'unit'},
        'config_hash':'unit','assets':{}})
    service=module.RealtimeStickers(pose);key=('owner','stream')
    def send(value,t,key=key):
        w,h=size
        ok,jpeg=cv2.imencode('.jpg',np.full((h,w,3),value,np.uint8));assert ok
        before=len(received)
        result=service.predict(jpeg.tobytes(),stream_key=key,frame_time=t)
        assert len(received)==before+1 and inspected[-1] is received[-1]
        return result
    first=send(100,0);second=send(100,.25)
    assert first['totalProcessingMs'] >= sum(first['timingsMs'].values()) - .01
    assert {'decode','snapshot','modelLoad','stream','person','inspection','rules'} == set(first['timingsMs'])
    assert first['analysisType']=='sticker' and first['detectionClasses']==['sticker','person']
    assert first['skeleton']['jointCount']==17 and len(first['people'])==1
    assert first['people'][0]['primary'] and len(first['people'][0]['keypoints'])==17
    assert second['people'][0]['trackId']==first['people'][0]['trackId']
    assert not first['inspection']['registered'] and first['compliance']['status']=='REVIEW'
    assert send(0,.5)['people']==[]
    assert len(send(100,0,('other','stream'))['people'])==1
    assert service.finish(key)['compliance']['status']=='REVIEW'
    service.release(key)
    assert key not in service.registry.items
    service.close()


@pytest.mark.parametrize('present',[True,False])
def test_sticker_upload_people_and_verdict_are_independent(authenticated,video_bytes,monkeypatch,present):
    import algorithms.stickers as module
    settings,app,client=authenticated
    received=[]
    class Pose:
        def predict(self,frame,**kwargs):
            assert kwargs['classes']==[0]
            received.append(frame)
            return [pose_result(frame,present)]
    class Sticker:
        def __init__(self,frozen):self.config=frozen['config'];self.device='cpu'
        def inspect(self,frame):
            assert frame is received[-1]
            return inspection()
    frozen=module.snapshot()
    frozen['config']=dict(frozen['config'],mode='appearance',targets=TARGETS)
    monkeypatch.setattr(module,'snapshot',lambda:copy.deepcopy(frozen))
    monkeypatch.setattr(module,'StickerDetector',Sticker)
    def analyzer(*args,**kwargs):return module.analyze(*args,pose_detector=Pose(),**kwargs)
    job=new_run(client,video_bytes,options={'analysis_type':'sticker','sample_fps':4})
    with app.state.engine.connect() as connection:
        options=connection.execute(select(runs.c.options).where(runs.c.id==job['id'])).scalar_one()
    assert options['person_config']['backend']=='yolo'
    assert execute_one(app.state.engine,settings,analyzer=analyzer)
    assert client.get('/api/v1/analysis-runs/'+job['id']).json()['status']=='SUCCEEDED'
    data=client.get('/api/v1/analysis-runs/'+job['id']+'/artifacts/analysis.json').json()
    expected=1 if present else 0
    assert data['analysisType']=='sticker' and data['detectionClasses']==['sticker','person']
    assert data['skeleton']['jointCount']==17 and data['summary']['maxPeople']==expected
    assert data['summary']['trackCount']==expected
    assert all(len(f['people'])==expected and f['end']>f['time'] for f in data['frames'])
    assert data['compliance']['status']=='REVIEW' and data['sequence']['events']==[]
    if present:assert all(len(f['people'][0]['keypoints'])==17 for f in data['frames'])
    manifest=json.loads(next((settings.data_dir/'runs').glob('*/manifest.json')).read_text(encoding='utf-8'))
    assert manifest['person_model_hash']==data['personModel']['sha256']==options['person_config']['sha256']


def test_sticker_worker_rejects_changed_person_model(authenticated,video_bytes):
    settings,app,client=authenticated
    job=new_run(client,video_bytes,options={'analysis_type':'sticker','sample_fps':4})
    settings.model_path.write_bytes(b'changed-after-queue')
    def must_not_run(*args,**kwargs):pytest.fail('Changed model must be rejected before analysis')
    assert execute_one(app.state.engine,settings,analyzer=must_not_run)
    result=client.get('/api/v1/analysis-runs/'+job['id']).json()
    assert result['status']=='FAILED' and result['error_code']=='MODEL_CHANGED'


def test_sticker_catalog_exposes_person_overlay(context):
    _,_,client=context
    data=client.get('/api/v1/sticker-model').json()
    assert data['detectionClasses']==['sticker','person'] and data['skeleton']['jointCount']==17
    assert data['personModelName']
