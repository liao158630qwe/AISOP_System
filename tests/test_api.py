import uuid
from dataclasses import replace
from sqlalchemy import select, update
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.db import runs, sessions
from conftest import new_run


def test_auth_and_csrf(context):
    settings, app, client = context
    assert client.get('/api/v1/analysis-runs').status_code == 401
    assert client.post('/api/v1/login', json={'username':'admin','password':'wrong'}).status_code == 401
    response = client.post('/api/v1/login', json={'username':'admin','password':settings.password})
    assert 'HttpOnly' in response.headers['set-cookie']
    assert client.post('/api/v1/media?filename=x.mp4', content=b'x').status_code == 403
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    assert client.post('/api/v1/logout').status_code == 200
    assert client.get('/api/v1/session').status_code == 401


def test_upload_rejects_invalid_and_cleans_files(authenticated):
    settings, app, client = authenticated
    for name, data, expected in [('empty.mp4', b'', 400), ('bad.exe', b'abc', 415), ('broken.mp4', b'not-video', 422)]:
        assert client.post('/api/v1/media?filename='+name, content=data).status_code == expected
    assert not list((settings.data_dir/'uploads').iterdir())


def test_limit_streamed_upload(context):
    settings, _, _ = context
    app = create_app(replace(settings, max_upload_bytes=4))
    with TestClient(app) as client:
        token = client.post('/api/v1/login', json={'username':'admin','password':settings.password}).json()['csrf']
        assert client.post('/api/v1/media?filename=big.mp4', content=b'12345', headers={'X-CSRF-Token':token}).status_code == 413
    assert not list((settings.data_dir/'uploads').iterdir())


def test_idempotency_cancel_and_private_artifacts(authenticated, video_bytes):
    _, app, client = authenticated
    row = new_run(client, video_bytes)
    rid, mid = row['id'], row['media_id']
    body = {'media_id':mid}
    repeat = client.post('/api/v1/analysis-runs', json=body, headers={'Idempotency-Key':'test-run'})
    assert repeat.json()['id'] == rid
    assert client.post('/api/v1/analysis-runs', json={'media_id':mid,'options':{'sample_fps':2}}, headers={'Idempotency-Key':'test-run'}).status_code == 409
    assert client.get(f'/api/v1/analysis-runs/{rid}/artifacts/analysis.json').status_code == 409
    assert client.post(f'/api/v1/analysis-runs/{rid}/cancel').json()['status'] == 'CANCELLED'
    assert client.post(f'/api/v1/analysis-runs/{rid}/cancel').json()['status'] == 'CANCELLED'
    with app.state.engine.begin() as c:
        c.execute(update(runs).where(runs.c.id == rid).values(owner='someone-else'))
    assert client.get(f'/api/v1/analysis-runs/{rid}').status_code == 404
    assert not client.get('/api/v1/analysis-runs').json()['items']


def test_options_rejected_before_queuing(authenticated, video_bytes):
    _, _, client = authenticated
    mid = client.post('/api/v1/media?filename=sample.avi', content=video_bytes).json()['id']
    for options in [{'start':2,'end':1}, {'crop':[0,0,999,999]}, {'sample_fps':0}, {'imgsz':123}, {'run_mode':'PRODUCTION'}]:
        result = client.post('/api/v1/analysis-runs', json={'media_id':mid,'options':options}, headers={'Idempotency-Key':str(uuid.uuid4())})
        assert result.status_code == 422, result.text
    assert not client.get('/api/v1/analysis-runs').json()['items']


def test_server_files_are_not_public(context):
    _, _, client = context
    for path in ['/backend/config.py','/models/yolo11s-pose.pt','/.env','/runtime-data/test.db',
                 '/assets/..%2fbackend/config.py','/assets/..%2f.env','/assets/..%2f..%2fREADME.md']:
        assert client.get(path).status_code == 404


def test_result_range_requests_and_fenced_file_access(authenticated, video_bytes):
    from backend.worker import execute_one
    from test_worker import fake_analyzer
    settings, app, client = authenticated
    row = new_run(client,video_bytes)
    execute_one(app.state.engine,settings,fake_analyzer)
    route=f'/api/v1/analysis-runs/{row["id"]}/artifacts/video.webm'
    response=client.get(route,headers={'Range':'bytes=0-1'})
    assert response.status_code==206 and response.content==b'te'
    assert client.get(route,headers={'Range':'bytes=999-'}).status_code==416
    assert client.get(route.replace('video.webm','private.txt')).status_code==404


def test_session_expiry(authenticated):
    _, app, client = authenticated
    with app.state.engine.begin() as c:
        c.execute(update(sessions).values(expires_at=0))
    assert client.get('/api/v1/session').status_code == 401


def test_api_restart_preserves_session_and_task(authenticated, video_bytes):
    from backend.worker import execute_one
    from test_worker import fake_analyzer
    settings, _, client = authenticated
    row = new_run(client,video_bytes)
    restarted = create_app(settings)
    with TestClient(restarted) as other:
        other.cookies.update(client.cookies)
        assert other.get('/api/v1/session').status_code==200
        assert other.get('/api/v1/analysis-runs/'+row['id']).json()['status']=='QUEUED'
        execute_one(restarted.state.engine,settings,fake_analyzer)
        assert other.get('/api/v1/analysis-runs/'+row['id']).json()['status']=='SUCCEEDED'
