"""Video workflow contracts use generated fixtures, never field accuracy claims."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from algorithms.video import file_hash
from backend.dataset_service import (Capture, DatasetService, claim, cleanup_dataset,
    execute_job, execute_one, finish, progress, resolve_export)
from backend.db import dataset_frames as frames, dataset_jobs as jobs, dataset_videos as videos
from backend.training_service import freeze_inputs, normalize_recipe

POLICY = dict(min_batches_per_split=2, min_class_samples=5, min_negative_samples=5,
              min_precision=.95, min_recall=.95)


def new(client, task='detect', key=None, **kwargs):
    response = client.post('/api/v1/admin/datasets', json=dict(name='视频夹具', station='station1',
        task=task, classes=['part', 'defect'], **kwargs), headers={'Idempotency-Key': key or str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    return response.json()


def capture(split='train', batch='batch1', cycle='cycle1'):
    return dict(split=split, batch=batch, cycle=cycle, camera='cam1', product='widget', lighting='normal', occlusion='clear')


def upload(client, did, raw, key=None, info=None, budget=60):
    return client.post(f'/api/v1/admin/datasets/{did}/videos', params=dict(filename='sample.avi',
        capture=json.dumps(info or capture()), frame_budget=budget), content=raw,
        headers={'Idempotency-Key': key or str(uuid.uuid4()), 'Content-Type': 'application/octet-stream'})


def video(root, seed=1, count=12):
    path = root / f'video-{seed}.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
    assert writer.isOpened()
    rng = np.random.default_rng(seed)
    for _ in range(count):
        writer.write(rng.integers(0, 256, (48, 64, 3), dtype=np.uint8))
    writer.release()
    return path.read_bytes()


def detail(client, did):
    response = client.get('/api/v1/admin/datasets/' + did)
    assert response.status_code == 200
    return response.json()


def frame_list(client, did, **params):
    return client.get(f'/api/v1/admin/datasets/{did}/frames', params=params).json()


def review(client, did, frame, state='labelled', task='detect', cls=0):
    body = dict(revision=frame['revision'], review=state)
    if state == 'labelled':
        body.update(boxes=[dict(class_id=cls, xyxy=[.1,.2,.7,.8])]) if task == 'detect' else body.update(class_id=cls)
    return client.put(f'/api/v1/admin/datasets/{did}/frames/{frame["id"]}', json=body)


def work(app, settings):
    job = claim(app.state.engine)
    assert job
    execute_job(app.state.engine, settings, job)
    return job


def export(client, did, key=None):
    return client.post(f'/api/v1/admin/datasets/{did}/exports', json=dict(revision=detail(client,did)['revision'], policy=POLICY),
        headers={'Idempotency-Key': key or str(uuid.uuid4())})


def build_reviewed(context, tmp_path, task='detect'):
    settings, app, client = context
    did = new(client, task)['id']
    for split_index, split in enumerate(('train','val','test')):
        for batch in range(2):
            index = split_index*2+batch
            r = upload(client,did,video(tmp_path,index),info=capture(split, f'b{index}', f'c{index}'),budget=12)
            assert r.status_code == 202, r.text
            work(app,settings)
            for i, frame in enumerate(frame_list(client,did,video=r.json()['id'])['items']):
                state = 'negative' if i>=6 and task=='detect' else 'labelled'
                body = dict(revision=frame['revision'],review=state)
                if state=='labelled':
                    if task=='detect':
                        body['boxes']=[dict(class_id=cls,xyxy=[.1,.1,.4,.4]) for cls in (0,1)]
                    else:
                        body['class_id']=i%2
                response=client.put(f'/api/v1/admin/datasets/{did}/frames/{frame["id"]}',json=body)
                assert response.status_code==200,response.text
    return did


def test_auth_schema_and_idempotency(context, authenticated):
    _, app, client = context
    key=str(uuid.uuid4()); first=new(client,key=key); assert new(client,key=key)['id']==first['id']
    body=dict(name='other',station='station1',classes=['a'])
    assert client.post('/api/v1/admin/datasets',json=body,headers={'Idempotency-Key':key}).status_code==409
    for classes in ([' '],['a',' a'],[],['a'*61]):
        body['classes']=classes
        assert client.post('/api/v1/admin/datasets',json=body,headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==422
    with pytest.raises(HTTPException) as e: app.state.datasets.detail('another-owner',first['id'])
    assert e.value.status_code==404
    body['classes']=['a']
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/admin/datasets',json=body).status_code==403
    client.cookies.clear()
    assert client.get('/api/v1/admin/datasets').status_code==401


def test_upload_is_streamed_without_decode_and_isolated(authenticated,video_bytes,monkeypatch):
    _,app,client=authenticated;did=new(client)['id'];key=str(uuid.uuid4())
    monkeypatch.setattr('backend.dataset_service.probe',lambda *a:pytest.fail('API must not decode videos'))
    first=upload(client,did,video_bytes,key=key);assert first.status_code==202,first.text
    assert upload(client,did,video_bytes,key=key).json()['reused']
    assert upload(client,did,video_bytes,key=key,budget=1).status_code==409
    assert upload(client,did,video_bytes).status_code==409
    for info in (capture('val'),capture('val','new','cycle1')):
        assert upload(client,did,b'other-video',info=info).status_code==409
    app.state.datasets.settings=replace(app.state.datasets.settings,max_upload_bytes=1)
    assert upload(client,did,b'too large').status_code==413
    assert len(detail(client,did)['videos'])==1


def test_real_extraction_review_and_failed_audit(authenticated,video_bytes):
    settings,app,client=authenticated;did=new(client)['id'];r=upload(client,did,video_bytes,budget=10);assert r.status_code==202
    assert export(client,did).status_code==409
    job=work(app,settings);view=detail(client,did)
    assert view['videos'][0]['state']=='SUCCEEDED'
    assert view['reviews']=={'pending':1}  # repeated pixels were removed
    row=frame_list(client,did)['items'][0]
    assert client.get(row['image']).status_code==200
    assert 'no-store' in client.get(row['image']+'?thumbnail=true').headers['cache-control']
    assert export(client,did).status_code==422
    assert review(client,did,row).status_code==200
    assert review(client,did,row).status_code==409
    row=frame_list(client,did)['items'][0]
    assert review(client,did,row,cls=3).status_code==422
    assert review(client,did,row,state='negative').status_code==200
    response=export(client,did);assert response.status_code==202,response.text
    assert review(client,did,frame_list(client,did)['items'][0]).status_code==409
    work(app,settings)
    view=detail(client,did);assert view['exports'][0]['state']=='FAILED'
    assert not view['active_export']
    assert not finish(app.state.engine,job,'FAILED','late stale result')
    assert view['coverage']['train']['negatives']==1


def test_corrupt_video_retry_fencing_and_remove(authenticated):
    settings,app,client=authenticated;did=new(client)['id'];r=upload(client,did,b'not a video').json()
    work(app,settings);assert detail(client,did)['videos'][0]['state']=='FAILED'
    assert client.post(f'/api/v1/admin/datasets/{did}/jobs/{r["job_id"]}/retry').status_code==200
    job=claim(app.state.engine)
    with app.state.engine.begin() as c:c.execute(update(jobs).where(jobs.c.id==job['id']).values(lease_until=0))
    assert claim(app.state.engine) is None
    with pytest.raises(RuntimeError):progress(app.state.engine,job,.5,'late')
    assert not finish(app.state.engine,job,'SUCCEEDED','late')
    assert client.delete(f'/api/v1/admin/datasets/{did}/videos/{r["id"]}').status_code==200
    assert detail(client,did)['videos']==[]


def test_upload_commit_ack_loss_keeps_registered_source(authenticated,video_bytes,monkeypatch):
    settings,app,client=authenticated;did=new(client)['id'];original=app.state.datasets.accept_video
    def lost_ack(*args):
        original(*args)
        raise OperationalError('commit',{},RuntimeError('lost acknowledgement'))
    monkeypatch.setattr(app.state.datasets,'accept_video',lost_ack)
    response=upload(client,did,video_bytes)
    assert response.status_code==503
    with app.state.engine.connect() as c: source=c.execute(select(videos).where(videos.c.dataset_id==did)).mappings().one()
    assert app.state.datasets.path(source['path']).is_file()
    cleanup_dataset(app.state.datasets,did,time.time()+1)
    assert app.state.datasets.path(source['path']).is_file()
    work(app,settings)
    assert len(frame_list(client,did)['items'])==1


def test_export_lease_recovery_unlocks_edits_and_get_is_read_only(authenticated,video_bytes):
    settings,app,client=authenticated;did=new(client)['id'];upload(client,did,video_bytes);work(app,settings)
    f=frame_list(client,did)['items'][0];assert review(client,did,f,state='negative').status_code==200
    assert export(client,did).status_code==202;job=claim(app.state.engine)
    with app.state.engine.begin() as c:c.execute(update(jobs).where(jobs.c.id==job['id']).values(lease_until=0))
    assert detail(client,did)['active_export']==job['id']
    assert claim(app.state.engine) is None
    assert not detail(client,did)['active_export']
    assert not finish(app.state.engine,job,'SUCCEEDED','stale completion')
    f=frame_list(client,did)['items'][0];assert review(client,did,f).status_code==200


@pytest.mark.parametrize('task',['detect','classify'])
def test_complete_export_freeze_and_training_reference(authenticated,tmp_path,task):
    settings,app,client=authenticated;did=build_reviewed(authenticated,tmp_path,task)
    view=detail(client,did);assert all(v['batches']==2 for v in view['coverage'].values())
    key=str(uuid.uuid4());r=export(client,did,key=key);assert r.status_code==202,r.text
    assert export(client,did,key=key).json()['id']==r.json()['id']
    work(app,settings)
    view=detail(client,did);job=view['exports'][0];assert job['state']=='SUCCEEDED',job
    result=client.get('/api/v1/admin/dataset-exports/'+job['id']).json();assert result['reference']=='dataset://'+job['id']
    assert result['audit']['batches']==dict(train=2,val=2,test=2)
    initial=settings.data_dir/'initial.pt';initial.write_bytes(settings.model_path.read_bytes())
    owner=view['owner'];raw=dict(backend='ultralytics',task=task,dataset=result['reference'],pretrained=str(initial))
    recipe=normalize_recipe(raw,settings,app.state.engine,owner,'station1')
    assert recipe['_dataset_ref']==result['reference']
    recipe['_source_manifest_sha256']=file_hash(recipe['dataset']);recipe['_pretrained_sha256']=file_hash(recipe['pretrained'])
    _,audit=freeze_inputs(recipe,'station1',POLICY,tmp_path/'frozen')
    assert audit['source_sha256']==result['audit']['sha256']
    for invalid in (dict(raw,task='obb'),dict(raw,dataset=result['path'])):
        with pytest.raises(ValueError):normalize_recipe(invalid,settings,app.state.engine,owner,'station1')
    with pytest.raises(ValueError):normalize_recipe(raw,settings,app.state.engine,owner,'station2')
    with pytest.raises(HTTPException):resolve_export(app.state.engine,settings,job['id'],'other')
    # Current annotations and source removal cannot mutate already exported copies.
    manifest=Path(result['path']);before=manifest.read_bytes();source=view['videos'][0]
    assert client.delete(f'/api/v1/admin/datasets/{did}/videos/{source["id"]}').status_code==200
    cleanup_dataset(app.state.datasets,did,time.time()+1)
    assert manifest.read_bytes()==before
    frozen_data=json.loads(before);image=manifest.parent/frozen_data['samples'][0]['file']
    cv2.imwrite(str(image),np.zeros((48,64,3),dtype=np.uint8))
    with pytest.raises(ValueError,match='内容发生变化'):freeze_inputs(recipe,'station1',POLICY,tmp_path/'tampered')
    manifest.write_text('{}')
    assert client.get('/api/v1/admin/dataset-exports/'+job['id']).status_code==409


def test_classification_review_and_pagination(authenticated,tmp_path):
    settings,app,client=authenticated;did=new(client,'classify')['id']
    upload(client,did,video(tmp_path,count=30),budget=30);work(app,settings)
    result=frame_list(client,did);assert len(result['items'])==24 and result['has_more']
    assert len(frame_list(client,did,offset=24)['items'])==6
    frame=result['items'][0]
    assert review(client,did,frame,state='negative').status_code==422
    assert review(client,did,frame).status_code==422
    assert review(client,did,frame,task='classify',cls=1).status_code==200
    assert detail(client,did)['coverage']['train']['classes']==[0,1]


def test_concurrent_claim_and_real_subprocess(authenticated,video_bytes):
    settings,app,client=authenticated;did=new(client)['id'];r=upload(client,did,video_bytes).json()
    with ThreadPoolExecutor(max_workers=3) as pool: results=list(pool.map(lambda _:claim(app.state.engine),range(3)))
    assert sum(r is not None for r in results)==1
    job=next(r for r in results if r);finish(app.state.engine,job,'FAILED','retry fixture')
    assert client.post(f'/api/v1/admin/datasets/{did}/jobs/{r["job_id"]}/retry').status_code==200
    assert execute_one(app.state.engine,settings)
    assert detail(client,did)['videos'][0]['state']=='SUCCEEDED'
    assert not execute_one(app.state.engine,settings)


def test_identical_frames_across_sets_are_rejected(authenticated,tmp_path):
    settings,app,client=authenticated;did=build_reviewed(authenticated,tmp_path)
    with app.state.engine.begin() as c:
        rows=c.execute(select(frames,videos.c.capture).join(videos,frames.c.video_id==videos.c.id)).mappings().all()
        train=next(r for r in rows if r['capture']['split']=='train');val=next(r for r in rows if r['capture']['split']=='val')
        source=app.state.datasets.path(train['path']);dest=app.state.datasets.path(val['path']);dest.write_bytes(source.read_bytes())
        c.execute(update(frames).where(frames.c.id==val['id']).values(sha256=file_hash(dest)))
    assert export(client,did).status_code==202;work(app,settings)
    job=detail(client,did)['exports'][0];assert job['state']=='FAILED';assert '跨集合' in job['message'] or 'crosses' in job['message']
