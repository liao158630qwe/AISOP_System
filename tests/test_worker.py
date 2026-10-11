import json
import sys
import types
import time
from sqlalchemy import select, update
from algorithms.video import file_hash
from backend.db import claim, finish, heartbeat, runs
from backend.worker import execute_one
from conftest import new_run


def fake_analyzer(video, model, folder, **kwargs):
    """Synthetic artifacts for orchestration tests only, never production inference."""
    folder.mkdir(parents=True)
    for name, content in [('analysis.json','{}'),('report.html','test'),('segments.csv','test'),('video.webm','test')]:
        (folder/name).write_text(content)
    (folder/'manifest.json').write_text(json.dumps({'files':{p.name:file_hash(p) for p in folder.iterdir()}}))


def test_worker_publishes_only_complete_output(authenticated, video_bytes):
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    assert execute_one(app.state.engine, settings, fake_analyzer)
    result = client.get('/api/v1/analysis-runs/'+row['id']).json()
    assert result['status'] == 'SUCCEEDED'
    assert client.get(f'/api/v1/analysis-runs/{row["id"]}/artifacts/analysis.json').status_code == 200
    assert not execute_one(app.state.engine, settings, fake_analyzer)


def test_old_lease_cannot_publish_and_expired_jobs_recover(authenticated, video_bytes):
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    first = claim(app.state.engine, 60)
    assert claim(app.state.engine, 60) is None
    with app.state.engine.begin() as c:
        c.execute(update(runs).where(runs.c.id == row['id']).values(lease_until=0))
    second = claim(app.state.engine, 60)
    assert second['id'] == first['id'] and second['token'] > first['token']
    assert not heartbeat(app.state.engine, first['id'], first['token'],60)
    assert not finish(app.state.engine, first['id'], first['token'],'SUCCEEDED','old')
    assert finish(app.state.engine, second['id'],second['token'],'SUCCEEDED','new')


def test_cancel_running_and_terminal_race(authenticated, video_bytes):
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    job = claim(app.state.engine, 60)
    assert client.post(f'/api/v1/analysis-runs/{row["id"]}/cancel').json()['status'] == 'CANCELLING'
    assert not finish(app.state.engine,job['id'],job['token'],'SUCCEEDED','bad')
    assert finish(app.state.engine,job['id'],job['token'],'CANCELLED')


def test_model_change_fails_without_publishing(authenticated, video_bytes):
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    settings.model_path.write_bytes(b'changed')
    execute_one(app.state.engine,settings,fake_analyzer)
    result = client.get('/api/v1/analysis-runs/'+row['id']).json()
    assert result['status']=='FAILED' and result['error_code']=='MODEL_CHANGED'


def test_failure_does_not_publish_partial_results(authenticated, video_bytes):
    settings, app, client = authenticated
    row = new_run(client,video_bytes)
    def fail(*args,**kwargs):
        raise RuntimeError('test failure')
    execute_one(app.state.engine,settings,fail)
    assert client.get('/api/v1/analysis-runs/'+row['id']).json()['status']=='FAILED'
    assert client.get(f'/api/v1/analysis-runs/{row["id"]}/artifacts/report.html').status_code==409


def test_run_records_selected_pose_backend_model_hash(authenticated, video_bytes, monkeypatch, tmp_path):
    settings, app, client = authenticated
    onnx = tmp_path / 'rtmo-m.onnx'
    onnx.write_bytes(b'rtmo-weights')
    stub = types.SimpleNamespace(model_files=lambda backend, mode='balanced': [('pose', onnx)])
    monkeypatch.setitem(sys.modules, 'algorithms.rtmlib_pose', stub)
    row = new_run(client, video_bytes, key='rtmo-run', options={'pose_backend': 'rtmo'})
    with app.state.engine.connect() as c:
        stored = c.execute(select(runs).where(runs.c.id == row['id'])).mappings().one()
    assert stored['options']['pose_backend'] == 'rtmo'
    assert stored['model_hash'] == file_hash(onnx)


def test_run_defaults_to_configured_pose_backend(authenticated, video_bytes):
    settings, app, client = authenticated
    row = new_run(client, video_bytes, key='default-backend')
    with app.state.engine.connect() as c:
        stored = c.execute(select(runs).where(runs.c.id == row['id'])).mappings().one()
    assert stored['options']['pose_backend'] == settings.pose_backend == 'yolo'
    assert stored['model_hash'] == file_hash(settings.model_path)
