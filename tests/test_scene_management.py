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


@pytest.fixture(autouse=True)
def scene_cpu_lane(monkeypatch):
    import backend.scene_realtime as module
    services = []
    original_init = module.RealtimeScenes.__init__
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        services.append(self)
    monkeypatch.setattr(module.RealtimeScenes, '__init__', initialize)
    monkeypatch.setattr(module, 'run_model_lane', lambda entry, fn: entry.executor.submit(fn).result())
    yield
    for service in services:
        service.close()


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
    assert 'scene-dashboard.js?v=' in client.get('/?scene='+draft['id']).text
    assert 'scene-console.js' not in client.get('/?scene='+draft['id']).text
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


def test_catalog_batches_queries_and_checks_each_model_once(registered, monkeypatch):
    from sqlalchemy import event
    from backend.db import scene_tasks
    _, app, client, model = registered
    publish(registered)
    with app.state.engine.begin() as c:
        original = dict(c.execute(select(scene_tasks)).mappings().one())
        c.execute(scene_tasks.insert(), [dict(original, id=str(uuid.uuid4())) for _ in range(199)])
    statements, checks = [], []
    verify = app.state.scenes.verify_asset
    def checked(value):
        checks.append(value['id'])
        return verify(value)
    monkeypatch.setattr(app.state.scenes, 'verify_asset', checked)
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(app.state.engine, 'before_cursor_execute', record)
    try:
        catalog = app.state.scenes.catalog('admin')
        assert len(statements) == 1 and checks == [model['id']]
        statements.clear(); checks.clear()
        public = app.state.scenes.scenes()
        assert len(statements) == 1 and checks == [model['id']]
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', record)
    assert len(catalog['scenes']) == len(public) == 200 and len(catalog['models']) == 1
    assert all('model' not in scene and scene['ready'] for scene in catalog['scenes'])
    assert app.state.scenes.catalog('another') == {'models': [], 'scenes': []}
    assert client.get('/api/v1/admin/catalog').json() == catalog
    client.patch('/api/v1/admin/models/' + model['id'], json={'enabled': False})
    disabled = client.get('/api/v1/admin/catalog').json()
    assert not disabled['models'][0]['enabled'] and not any(scene['ready'] for scene in disabled['scenes'])


def test_publication_rechecks_model_after_file_validation(registered, monkeypatch):
    _, app, client, model = registered
    verify = app.state.scenes.verify_asset
    def disable_after_check(value):
        path = verify(value)
        app.state.scenes.toggle(model['id'], False, 'admin')
        return path
    monkeypatch.setattr(app.state.scenes, 'verify_asset', disable_after_check)
    response = client.post('/api/v1/admin/scenes', json=definition(model['id']))
    assert response.status_code == 409
    assert app.state.scenes.scenes('admin') == []


def test_candidate_scan_cache_keeps_registered_models_fresh(registered, monkeypatch):
    _, app, client, _ = registered
    scans = []
    scan = app.state.scenes._file_candidates
    def counted():
        scans.append(1)
        return scan()
    monkeypatch.setattr(app.state.scenes, '_file_candidates', counted)
    first = app.state.scenes.candidates('admin')
    first[0]['name'] = 'client mutation'
    added = client.post('/api/v1/admin/models/upload?name=fresh&filename=weights.pt', content=b'fresh').json()
    second = app.state.scenes.candidates('admin')
    assert len(scans) == 1 and second[0]['name'] != 'client mutation'
    assert any(row.get('model_id') == added['id'] for row in second)
    assert not any(row.get('model_id') for row in app.state.scenes.candidates('another'))
    app.state.scenes._candidate_until = 0
    app.state.scenes.candidates('admin')
    assert len(scans) == 2


def test_busy_model_inspection_rejects_without_leaking_upload(registered):
    _, app, client, _ = registered
    registry = app.state.scenes
    before = set(registry.directory.iterdir())
    with registry._inspection_slot:
        response = client.post('/api/v1/admin/models/upload?name=busy&filename=weights.pt', content=b'busy')
    assert response.status_code == 429 and response.headers['retry-after'] == '3'
    assert set(registry.directory.iterdir()) == before
    assert client.post('/api/v1/admin/models/upload?name=ready&filename=weights.pt', content=b'ready').status_code == 201


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
        def __init__(self, frozen, model=None): pass
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
    frozen = dict(model={'task':task,'classes':{'0':'widget'}}, config={'device':'cuda','rules':[
        dict(id='normal',name='normal',class_id=0,kind='required',min_count=1,max_count=None,
             threshold=.6,roi=[.25,.25,.75,.75])], 'imgsz':320,'min_sharpness':0})
    boxes=SimpleNamespace(cls=torch.tensor([0.]),conf=torch.tensor([.95]),xyxy=torch.tensor([[2.,4.,20.,24.]]),
                          xyxyxyxy=torch.tensor([[[2.,4.],[20.,4.],[20.,24.],[2.,24.]]]))
    result=SimpleNamespace(boxes=boxes,obb=boxes,masks=SimpleNamespace(xy=[np.array([[2.,4.],[20.,4.],[20.,24.]])]),
        keypoints=SimpleNamespace(data=torch.tensor([[[5.,6.,.9],[10.,12.,.8]]])),
        probs=SimpleNamespace(top1=0,top1conf=.95,data=torch.tensor([.95])))
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
        def __init__(self, frozen, model=None): pass
        def inspect(self, frame): return raw_checks()
    monkeypatch.setattr(module,'SceneDetector',FakeDetector)
    monkeypatch.setattr(module,'load_model',lambda frozen: object())
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


@pytest.mark.parametrize('capacity,expected_loads', [(1, 1), (2, 1)])
def test_scene_cache_reuses_models_without_sharing_temporal_state(registered, monkeypatch, capacity, expected_loads):
    import backend.scene_realtime as module
    settings, app, _, _ = registered
    first = publish(registered, name='相机一')
    second = publish(registered, name='相机二')
    loads = []
    class FakeDetector:
        def __init__(self, frozen, model=None):
            self.frozen = frozen
        def inspect(self, frame):
            return raw_checks()
    monkeypatch.setattr(module, 'SceneDetector', FakeDetector)
    def load(frozen):
        loads.append(frozen['model']['id'])
        return object()
    monkeypatch.setattr(module, 'load_model', load)
    service = module.RealtimeScenes(app.state.scenes, capacity)
    _, image = cv2.imencode('.jpg', np.ones((64,96,3),np.uint8)*100)
    def frame(scene, key, stamp):
        return service.predict(image.tobytes(), scene_id=scene['id'], stream_key=('station',key),
            frame_time=stamp, owner=settings.username)
    assert frame(first, 'one', 0)['compliance']['status'] == 'REVIEW'
    assert frame(second, 'two', 0)['compliance']['status'] == 'REVIEW'
    assert frame(first, 'one', .25)['compliance']['status'] == 'PASS'
    assert len(loads) == expected_loads
    assert len(service.models) <= capacity
    assert service.streams.items[('station','one')]['sampleCount'] == 2
    assert service.streams.items[('station','two')]['sampleCount'] == 1


def test_model_import_retry_recovery_and_conflicting_payload(registered):
    _, app, client, _ = registered
    key = str(uuid.uuid4())
    headers = {'Idempotency-Key': key}
    url = '/api/v1/admin/models/upload?name=remote&version=2&filename=weight.pt&notes=server'
    original_files = set(app.state.scenes.directory.iterdir())
    first = client.post(url, content=b'retry-weights', headers=headers)
    assert first.status_code == 201, first.text
    second = client.post(url, content=b'retry-weights', headers=headers)
    assert second.status_code == 201 and second.json()['reused']
    assert second.json()['id'] == first.json()['id']
    assert client.get('/api/v1/admin/model-imports/' + key).json()['id'] == first.json()['id']
    assert client.post(url, content=b'different', headers=headers).status_code == 409
    assert client.post(url.replace('name=remote', 'name=changed'), content=b'retry-weights', headers=headers).status_code == 409
    assert len(set(app.state.scenes.directory.iterdir()) - original_files) == 1
    assert client.post(url, content=b'retry-weights', headers={'Idempotency-Key': 'invalid'}).status_code == 400
    from backend.db import model_registry
    from sqlalchemy import update
    with app.state.engine.begin() as c:
        c.execute(update(model_registry).where(model_registry.c.id == first.json()['id']).values(owner='another'))
    assert client.get('/api/v1/admin/model-imports/' + key).status_code == 404


def test_server_inbox_import_limits_and_path_boundaries(registered):
    settings, app, client, _ = registered
    inbox = app.state.scenes.inbox
    (inbox / 'line1').mkdir()
    (inbox / 'line1' / 'weight.pt').write_bytes(b'server-weights')
    # Bad optional metadata must not prevent a valid checkpoint from being imported.
    (inbox / 'line1' / 'model-card.json').write_text('{invalid')
    candidates = client.get('/api/v1/admin/model-candidates').json()
    assert candidates['limits']['max_upload_bytes'] == settings.max_upload_bytes
    assert any(c['path'] == 'inbox/line1/weight.pt' and c['source'] == 'inbox' for c in candidates['items'])
    body = {'path': 'inbox/line1/weight.pt', 'name': 'server copy'}
    result = client.post('/api/v1/admin/models/import', json=body, headers={'Idempotency-Key': str(uuid.uuid4())})
    assert result.status_code == 201, result.text
    for path in ('inbox/../outside.pt', 'inbox/../../models/widget.pt'):
        assert client.post('/api/v1/admin/models/import', json=dict(body, path=path)).status_code == 422
    from dataclasses import replace
    app.state.scenes.settings = replace(settings, max_upload_bytes=1)
    assert client.post('/api/v1/admin/models/import', json=body).status_code == 413


def test_parallel_import_retries_keep_one_file_and_model(registered):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    from backend.scenes import ModelImport
    _, app, _, _ = registered
    before = set(app.state.scenes.directory.iterdir())
    body = ModelImport(path='models/widget.pt', name='parallel')
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = [pool.submit(app.state.scenes.import_model, body, 'admin', key) for _ in range(4)]
        values, retry = [], 0
        for attempt in attempts:
            try:
                values.append(attempt.result())
            except HTTPException as exc:
                assert exc.status_code == 429
                retry += 1
    values += [app.state.scenes.import_model(body, 'admin', key) for _ in range(retry)]
    assert len({value['id'] for value in values}) == 1
    assert sum(bool(value.get('reused')) for value in values) == 3
    assert len(set(app.state.scenes.directory.iterdir()) - before) == 1
    row = app.state.scenes._model(values[0]['id'], 'admin')
    assert app.state.scenes.verify_asset(row).read_bytes() == b'fake-registry-test-weights'


def test_registered_models_are_training_candidates_with_owner_and_type_checks(registered):
    from backend.db import model_registry
    from sqlalchemy import update
    settings, app, client, model = registered
    candidates = client.get('/api/v1/admin/model-candidates').json()['items']
    assert any(c['path'] == 'model://' + model['id'] and c['task'] == 'detect' for c in candidates)
    assert not any(c['source'] == 'registered' for c in app.state.scenes.candidates('another'))
    manifest = settings.data_dir / 'manifest.json'
    manifest.write_text('{"schema_version": 1, "names": ["widget"], "samples": []}')
    recipe = dict(backend='ultralytics', task='detect', dataset=str(manifest), pretrained='model://' + model['id'])
    def submit(recipe):
        return client.post('/api/v1/admin/training-runs', json=dict(name='import to train', station='station1', recipe=recipe),
                           headers={'Idempotency-Key': str(uuid.uuid4())})
    result = submit(recipe)
    assert result.status_code == 202, result.text
    frozen_path = result.json()['recipe']['pretrained']
    assert Path(frozen_path).is_file() and 'model://' not in frozen_path
    assert submit(dict(recipe, task='classify')).status_code == 422
    with app.state.engine.begin() as c:
        c.execute(update(model_registry).where(model_registry.c.id == model['id']).values(owner='another'))
    assert submit(recipe).status_code == 404
    assert submit(dict(recipe, pretrained=frozen_path)).status_code == 422


def test_model_upload_stream_limit_cleans_partial_files(authenticated, monkeypatch):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from backend.app import create_app
    import backend.scenes as module
    settings, _, _ = authenticated
    monkeypatch.setattr(module, 'inspect_model', lambda p: ('detect', {'0': 'widget'}))
    app = create_app(replace(settings, max_upload_bytes=4))
    with TestClient(app) as client:
        response = client.post('/api/v1/login', json=dict(username=settings.username,password=settings.password))
        client.headers['X-CSRF-Token'] = response.json()['csrf']
        # Chunked transfer has no Content-Length, so the stream limit must enforce it.
        response = client.post('/api/v1/admin/models/upload?name=large&filename=large.pt',
                               content=iter([b'123', b'456']))
        assert response.status_code == 413
        assert not list(app.state.scenes.directory.iterdir())
    app.state.engine.dispose()


def test_model_upload_preserves_long_unicode_notes(registered):
    import base64
    _, app, client, _ = registered
    notes = '工位验证说明' * 166
    header = base64.b64encode(notes.encode()).decode()
    response = client.post('/api/v1/admin/models/upload?name=unicode&filename=weights.pt',
                           content=b'notes-test', headers={'X-Model-Notes': header})
    assert response.status_code == 201 and response.json()['notes'] == notes
    before = set(app.state.scenes.directory.iterdir())
    for value in ('not-base64%', base64.b64encode(('字' * 1001).encode()).decode()):
        response = client.post('/api/v1/admin/models/upload?name=bad&filename=weights.pt',
                               content=b'bad-notes', headers={'X-Model-Notes': value})
        assert response.status_code == 422
    assert set(app.state.scenes.directory.iterdir()) == before


def test_upload_limit_environment_configuration(monkeypatch, tmp_path):
    from backend.config import Settings
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'config.db'))
    monkeypatch.setenv('APP_PASSWORD', 'test-password-12345')
    monkeypatch.setenv('MAX_UPLOAD_BYTES', '123456')
    assert Settings.from_env().max_upload_bytes == 123456
    for invalid in ('0', '-1', 'not-a-number'):
        monkeypatch.setenv('MAX_UPLOAD_BYTES', invalid)
        with pytest.raises(RuntimeError):
            Settings.from_env()

@pytest.mark.parametrize('upload', [False, True])
def test_model_commit_ack_loss_preserves_registered_asset(registered, monkeypatch, upload):
    from contextlib import contextmanager
    from sqlalchemy.exc import OperationalError
    from backend.db import model_registry
    settings, app, client, _ = registered
    engine = app.state.engine
    begin = engine.begin
    @contextmanager
    def lost_ack():
        with begin() as connection:
            yield connection
        raise OperationalError('commit acknowledgement lost', {}, Exception())
    monkeypatch.setattr(engine, 'begin', lost_ack)
    if upload:
        response = client.post('/api/v1/admin/models/upload?name=commit-recovery&filename=model.pt',
                               content=b'fake-weights')
    else:
        response = client.post('/api/v1/admin/models/import',
            json={'path': 'models/widget.pt', 'name': 'commit-recovery', 'version': '1.0'})
    assert response.status_code == 201, response.text
    with engine.connect() as c:
        row = c.execute(select(model_registry).where(model_registry.c.id == response.json()['id'])).mappings().one()
    assert (settings.data_dir / row['path']).is_file()
