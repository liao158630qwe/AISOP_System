"""Screw tasks must carry same-frame person boxes and pose observations."""
import copy
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import torch

from algorithms.video import AnalysisError
from backend.worker import execute_one
from conftest import new_run


def pose_result(frame, present=True):
    h,w=frame.shape[:2]
    points=torch.tensor(np.tile([w*.5,h*.5,.9],(1,17,1)))
    return SimpleNamespace(
        boxes=SimpleNamespace(xyxy=torch.tensor([[w*.2,h*.1,w*.8,h*.9]]),
                              conf=torch.tensor([.9]),cls=torch.tensor([0.])),
        keypoints=SimpleNamespace(data=points) if present else None,speed={'inference':1.})


def inspection(present=True):
    return {'registered':present,'targets':[{'id':'S1','name':'位置1','point':[10,10],
            'state':'PRESENT' if present else 'UNKNOWN','score':.9 if present else None,
            'bbox':[.4,.4,.5,.5] if present else None}]}


def test_screw_realtime_same_frame_people_independent_of_registration(monkeypatch):
    import backend.screw_realtime as module
    from backend.realtime import RealtimeDetector
    from pathlib import Path
    received=[]
    class Pose:
        def predict(self, frame, **kwargs):
            assert kwargs['classes']==[0]
            received.append(frame)
            return [pose_result(frame, bool(frame.any()))]
    pose=RealtimeDetector(Path('unused'),backend='yolo',pool_size=1)
    pose.model=Pose();pose.model_kind='yolo'
    class Screw:
        def __init__(self,frozen):
            self.config=frozen['config'];self.device='cpu'
        def inspect(self,frame):
            assert frame is received[-1], 'Both models must see the same decoded array'
            return inspection(False)
    monkeypatch.setattr(module,'ScrewDetector',Screw)
    monkeypatch.setattr(module,'snapshot',lambda:{'config':{'targets':inspection()['targets']},'config_hash':'unit','assets':{}})
    service=module.RealtimeScrews(pose);key=('owner','stream')
    def send(value,t,key=key):
        ok,jpeg=cv2.imencode('.jpg',np.full((64,96,3),value,np.uint8));assert ok
        return service.predict(jpeg.tobytes(),stream_key=key,frame_time=t)
    first=send(100,0);second=send(100,.25)
    assert first['totalProcessingMs'] >= sum(first['timingsMs'].values()) - .01
    assert {'decode','snapshot','modelLoad','stream','person','inspection','rules'} == set(first['timingsMs'])
    assert first['analysisType']=='screw' and first['skeleton']['jointCount']==17
    assert len(first['people'])==1 and first['people'][0]['primary']
    assert second['people'][0]['trackId']==first['people'][0]['trackId']
    assert not first['inspection']['registered'] and first['compliance']['status']=='REVIEW'
    assert send(0,.5)['people']==[]
    assert len(send(100,0,('other','stream'))['people'])==1
    assert service.finish(key)['compliance']['status']=='REVIEW'
    service.release(key)
    assert key not in service.registry.items


def test_screw_upload_worker_people_and_model_integrity(authenticated,video_bytes,monkeypatch):
    import algorithms.screws as module
    from algorithms.person_detection import verify
    settings,app,client=authenticated
    received=[]
    class Pose:
        def predict(self,frame,**kwargs):
            assert kwargs['classes']==[0]
            received.append(frame)
            return [pose_result(frame)]
    class Screw:
        def __init__(self,frozen):
            self.config=frozen['config'];self.device='cpu'
        def inspect(self,frame):
            assert frame is received[-1]
            return copy.deepcopy(inspection())
    monkeypatch.setattr(module,'ScrewDetector',Screw)
    def analyzer(*args,**kwargs):
        return module.analyze(*args,pose_detector=Pose(),**kwargs)
    job=new_run(client,video_bytes,options={'analysis_type':'screw','sample_fps':4})
    from sqlalchemy import select
    from backend.db import runs
    with app.state.engine.connect() as connection:
        options=connection.execute(select(runs.c.options).where(runs.c.id==job['id'])).scalar_one()
    assert options['person_config']['backend']=='yolo'
    assert execute_one(app.state.engine,settings,analyzer=analyzer)
    assert client.get('/api/v1/analysis-runs/'+job['id']).json()['status']=='SUCCEEDED'
    data=client.get('/api/v1/analysis-runs/'+job['id']+'/artifacts/analysis.json').json()
    assert data['analysisType']=='screw' and data['detectionClasses']==['screw','person']
    assert data['skeleton']['jointCount']==17 and data['summary']['maxPeople']==1
    assert data['summary']['trackCount']==1
    assert all(len(f['people'])==1 and len(f['people'][0]['keypoints'])==17 and f['end']>f['time'] for f in data['frames'])
    manifest=json.loads(next((settings.data_dir/'runs').glob('*/manifest.json')).read_text(encoding='utf-8'))
    assert manifest['person_model_hash']==data['personModel']['sha256']
    settings.model_path.write_bytes(b'changed')
    with pytest.raises(AnalysisError,match='人体姿态模型'):
        verify(options['person_config'])
