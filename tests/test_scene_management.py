"""Model/scene contracts, version freezing, runtime rule evidence and shared worker routing."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import cv2
import numpy as np
import pytest
import torch
from sqlalchemy import select

from algorithms.scene_runtime import SceneDetector, SceneEvidence
from backend.db import runs
from backend.worker import execute_one
from conftest import new_run
from test_worker import fake_analyzer


@pytest.fixture
def registered(authenticated, tmp_path, monkeypatch):
    import backend.scenes as module
    root = tmp_path / 'project'
    (root / 'models').mkdir(parents=True)
    (root / 'outputs').mkdir()
    (root / 'models/widget.pt').write_bytes(b'fake-registry-test-weights')
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module, 'inspect_model', lambda path: ('detect', {'0':'widget', '1':'defect'}))
    settings, app, client = authenticated
    response = client.post('/api/v1/admin/models/import', json={'path':'models/widget.pt', 'name':'组件检测', 'version':'1.2'})
    assert response.status_code == 201, response.text
    return settings, app, client, response.json()


def definition(model_id, **extra):
    value = dict(name='端子到位检测', description='第三种可配置场景', model_id=model_id, status='published',
        min_sharpness=0, rules=[dict(id='normal', name='必须检出组件', class_id=0),
                               dict(id='bad', name='禁止缺陷', class_id=1, kind='forbidden')])
    value.update(extra)
    return value


def publish(registered, **extra):
    _, _, client, model = registered
    body = definition(model['id'], **extra)
    response = client.post('/api/v1/admin/scenes', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_management_requires_auth_and_csrf(context):
    _, _, client = context
    assert client.get('/api/v1/admin/models').status_code == 401
    assert client.get('/api/v1/admin/scenes').status_code == 401
    assert client.get('/api/v1/admin/model-candidates').status_code == 401
    assert client.get('/api/v1/scenes').json() == {'items':[]}
    assert client.post('/api/v1/admin/models/import', json={'path':'models/a.pt', 'name':'a'}).status_code == 401
    assert client.post('/api/v1/login', json={'username':'admin','password':'test-password-12345'}).status_code == 200
    assert client.post('/api/v1/admin/scenes', json=definition(str(uuid.uuid4()))).status_code == 403


def test_model_library_catalog_and_scene_publication(registered):
    settings, app, client, model = registered
    assert model['classes'] == {'0':'widget','1':'defect'}
    assert 'path' not in model and 'owner' not in model
    assert client.get('/api/v1/admin/model-candidates').json()['items'][0]['path'] == 'models/widget.pt'
    draft = publish(registered, status='draft', rules=[])
    assert client.get('/api/v1/scenes').json()['items'] == []
    assert client.get('/api/v1/scenes/'+draft['id']).status_code == 404
    body = definition(model['id'], revision=draft['revision'])
    updated = client.put('/api/v1/admin/scenes/'+draft['id'], json=body)
    assert updated.status_code == 200, updated.text
    assert updated.json()['revision'] == 2
    assert client.put('/api/v1/admin/scenes/'+draft['id'], json=body).status_code == 409
    public = client.get('/api/v1/scenes').json()['items']
    assert len(public) == 1 and public[0]['ready']
    assert 'model_path' not in public[0]
    assert client.get('/?scene='+draft['id']).status_code == 200
    assert 'scene-console.js' in client.get('/?scene='+draft['id']).text
    assert '模型与任务管理' in client.get('/').text
    assert client.get('/admin').status_code == 200


def test_scene_rejects_invalid_classes_regions_and_no_positive_evidence(registered):
    _, _, client, model = registered
    for rules in [[], [dict(id='bad', name='bad', class_id=1, kind='forbidden')],
                  [dict(id='missing', name='missing', class_id=4)],
                  [dict(id='roi', name='bad roi', class_id=0, roi=[.7,.2,.4,.8])]]:
        assert client.post('/api/v1/admin/scenes', json=definition(model['id'], rules=rules)).status_code == 422
    assert client.post('/api/v1/admin/models/import', json={'path':'../private.pt','name':'bad'}).status_code == 422
    assert client.post('/api/v1/admin/models/upload?name=bad&filename=bad.pt', content=b'').status_code == 422
    assert client.post('/api/v1/admin/models/upload?name=bad&filename=bad.pkl', content=b'123').status_code == 415


def test_model_disable_and_asset_changes_make_scene_unavailable(registered):
    _, app, client, model = registered
    scene = publish(registered)
    assert client.patch('/api/v1/admin/models/'+model['id'], json={'enabled':False}).status_code == 200
    assert client.get('/api/v1/scenes/'+scene['id']).json()['ready'] is False
    with pytest.raises(Exception) as exc:
        app.state.scenes.snapshot(scene['id'], 'admin')
    assert exc.value.status_code == 409
    client.patch('/api/v1/admin/models/'+model['id'], json={'enabled':True})
    assert client.get('/api/v1/scenes/'+scene['id']).json()['ready']
    path = app.state.scenes.directory/(model['id']+'.pt')
    path.write_bytes(b'changed-weights')
    assert client.get('/api/v1/scenes/'+scene['id']).json()['ready'] is False
    assert not app.state.scenes.models('another-owner')


def test_scene_run_freezes_version_and_dispatches_worker(registered, video_bytes):
    settings, app, client, model = registered
    scene = publish(registered)
    job = new_run(client, video_bytes, options={'analysis_type':'scene','scene_id':scene['id']})
    with app.state.engine.connect() as c:
        row = c.execute(select(runs).where(runs.c.id==job['id'])).mappings().one()
    frozen = row['options']['scene_config']
    assert frozen['revision'] == 1 and row['model_hash'] == model['sha256']
    body = definition(model['id'], name='已修改规则', revision=1)
    assert client.put('/api/v1/admin/scenes/'+scene['id'], json=body).status_code == 200
    assert frozen['config']['name'] == '端子到位检测'
    assert execute_one(app.state.engine, settings, fake_analyzer)
    assert client.get('/api/v1/analysis-runs/'+job['id']).json()['status'] == 'SUCCEEDED'
    items = client.get('/api/v1/analysis-runs?analysis_type=scene&scene_id='+scene['id']).json()['items']
    assert items[0]['scene_name'] == '端子到位检测' and items[0]['model_name'] == '组件检测'
    assert client.get('/api/v1/analysis-runs?scene_id='+str(uuid.uuid4())).json()['items'] == []


def test_uploaded_models_are_registered_and_invalid_uploads_leave_no_files(registered, monkeypatch):
    import backend.scenes as module
    _, app, client, _ = registered
    response = client.post('/api/v1/admin/models/upload?name=uploaded&version=2&filename=weights.pt',
                           content=b'uploaded-test-weights')
    assert response.status_code == 201, response.text
    model = response.json()
    assert model['version'] == '2' and model['classes']['0'] == 'widget'
    path = app.state.scenes.directory / (model['id'] + '.pt')
    assert path.read_bytes() == b'uploaded-test-weights'
    previous = set(app.state.scenes.directory.iterdir())
    def invalid(path):
        raise ValueError('invalid checkpoint')
    monkeypatch.setattr(module, 'inspect_model', invalid)
    assert client.post('/api/v1/admin/models/upload?name=invalid&filename=weights.pt', content=b'bad').status_code == 422
    assert set(app.state.scenes.directory.iterdir()) == previous


def test_low_fps_video_uses_effective_rate_and_exports_frozen_scene(registered, tmp_path, monkeypatch):
    import algorithms.scene_runtime as runtime
    settings, app, client, model = registered
    scene = publish(registered, sample_fps=4)
    video = tmp_path / 'slow.avi'
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), .5, (64, 48))
    assert writer.isOpened()
    for _ in range(3):
        writer.write(np.zeros((48, 64, 3), np.uint8))
    writer.release()
    job = new_run(client, video.read_bytes(), options={'analysis_type':'scene','scene_id':scene['id']})
    with app.state.engine.connect() as c:
        row = c.execute(select(runs).where(runs.c.id == job['id'])).mappings().one()
    assert row['options']['sample_fps'] == .5
    assert row['options']['scene_config']['config']['sample_fps'] == 4
    assert client.put('/api/v1/admin/scenes/'+scene['id'],
                      json=definition(model['id'], name='new revision', revision=1)).status_code == 200
    class FakeDetector:
        def __init__(self, frozen): pass
        def inspect(self, frame): return raw_checks()
    monkeypatch.setattr(runtime, 'SceneDetector', FakeDetector)
    assert execute_one(app.state.engine, settings)
    result = client.get('/api/v1/analysis-runs/'+job['id'])
    assert result.json()['status'] == 'SUCCEEDED', result.text
    analysis = client.get('/api/v1/analysis-runs/'+job['id']+'/artifacts/analysis.json').json()
    assert analysis['sceneRevision'] == 1 and analysis['sceneName'] == scene['name']
    assert analysis['summary']['sampleFps'] == .5
    assert [frame['time'] for frame in analysis['frames']] == [0, 2, 4]
    assert [frame['compliance']['status'] for frame in analysis['frames']] == ['REVIEW','PASS','PASS']
    for name in ('manifest.json','report.html','segments.csv','video.webm'):
        response = client.get('/api/v1/analysis-runs/'+job['id']+'/artifacts/'+name)
        assert response.status_code == 200 and response.content


def raw_checks(normal='PASS', bad='CLEAR'):
    return dict(observations=[], inferenceMs=1, checks=[
        dict(id='normal',name='normal',kind='required',state=normal,score=.95,count=1,reason=normal),
        dict(id='bad',name='bad',kind='forbidden',state=bad,score=.95 if bad=='FAIL' else None,count=int(bad=='FAIL'),reason=bad)])


def test_current_frame_evidence_does_not_reuse_pass_on_unknown():
    config = definition(str(uuid.uuid4()), sample_fps=4, confirmation_frames=2)
    evidence = SceneEvidence(config)
    assert evidence.update(0, raw_checks())['compliance']['status'] == 'REVIEW'
    assert evidence.update(.25, raw_checks())['compliance']['status'] == 'PASS'
    assert evidence.update(.5, raw_checks('REVIEW'))['compliance']['status'] == 'REVIEW'
    assert evidence.update(.75, raw_checks())['compliance']['status'] == 'REVIEW'
    assert evidence.update(1, raw_checks())['compliance']['status'] == 'PASS'
    assert evidence.update(10, raw_checks())['compliance']['status'] == 'REVIEW'
    assert evidence.update(10.25, raw_checks(bad='FAIL'))['compliance']['status'] == 'REVIEW'
    assert evidence.update(10.5, raw_checks(bad='FAIL'))['compliance']['status'] == 'FAIL'


@pytest.mark.parametrize('task', ['detect','segment','pose','obb','classify'])
def test_runtime_normalizes_all_supported_model_outputs(task):
    model = SimpleNamespace(task=task)
    frozen = dict(model={'task':task,'classes':{'0':'widget'}}, config={'device':'cpu','rules':[
        dict(id='normal',name='normal',class_id=0,kind='required',min_count=1,max_count=None,
             threshold=.6,roi=[.25,.25,.75,.75])], 'imgsz':320,'min_sharpness':0})
    boxes=SimpleNamespace(cls=torch.tensor([0.]),conf=torch.tensor([.95]),xyxy=torch.tensor([[2.,4.,20.,24.]]),
                          xyxyxyxy=torch.tensor([[[2.,4.],[20.,4.],[20.,24.],[2.,24.]]]))
    result=SimpleNamespace(boxes=boxes,obb=boxes,masks=SimpleNamespace(xy=[np.array([[2.,4.],[20.,4.],[20.,24.]])]),
        keypoints=SimpleNamespace(data=torch.tensor([[[5.,6.,.9],[10.,12.,.8]]])),
        probs=SimpleNamespace(top1=0,top1conf=.95))
    model.predict=lambda crops,**kwargs:[result for _ in crops]
    detector=SceneDetector(frozen, model=model)
    data=detector.inspect(np.ones((100,200,3),np.uint8)*100)
    assert len(data['observations'])==1 and data['checks'][0]['state']=='PASS'
    assert data['observations'][0]['name']=='widget'
    box=data['observations'][0]['bbox']
    assert .25<=box[0]<box[2]<=.75 and .25<=box[1]<box[3]<=.75
    if task in ('segment','obb'): assert len(data['observations'][0]['polygon'])>=3
    if task=='pose': assert len(data['observations'][0]['keypoints'])==2


def test_realtime_stream_version_owner_and_timestamp_isolation(registered, monkeypatch):
    import backend.scene_realtime as module
    _, app, client, model = registered
    scene=publish(registered)
    class FakeDetector:
        def __init__(self, frozen): pass
        def inspect(self, frame): return raw_checks()
    monkeypatch.setattr(module,'SceneDetector',FakeDetector)
    ok,jpeg=cv2.imencode('.jpg',np.ones((64,96,3),np.uint8)*100)
    identity=str(uuid.uuid4())
    def frame(t):
        return client.post('/api/v1/realtime/frame?analysis_type=scene&scene_id='+scene['id'],
            headers={'Content-Type':'image/jpeg','X-Stream-Id':identity,'X-Frame-Time':str(t)},content=jpeg.tobytes())
    assert frame(0).json()['compliance']['status']=='REVIEW'
    result=frame(.25)
    assert result.status_code==200 and result.json()['compliance']['status']=='PASS'
    assert result.json()['sceneRevision']==1 and result.json()['model']['sha256']==model['sha256']
    assert set(result.json()['timingsMs'])=={'decode','snapshot','modelLoad','inference','rules'}
    assert frame(.25).status_code==409
    body=definition(model['id'],revision=1)
    client.put('/api/v1/admin/scenes/'+scene['id'],json=body)
    assert frame(.5).status_code==409
    assert client.post('/api/v1/realtime/streams/'+str(uuid.uuid4())+'/finish?analysis_type=scene').status_code==404
    assert client.delete('/api/v1/realtime/streams/'+identity).status_code==200
    assert frame(1).status_code==200
    assert client.post('/api/v1/realtime/streams/'+identity+'/finish?analysis_type=scene').json()['final']
    assert frame(1.25).status_code==409
