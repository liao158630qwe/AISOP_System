"""Opt-in PostgreSQL concurrency test, isolated in a disposable schema.

Set TEST_POSTGRES_URL to a dedicated test database using psycopg. Never use production.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from backend.db import initialize, claim, media, runs


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='PostgreSQL service not configured')
def test_postgres_dataset_concurrency_and_lease_recovery(tmp_path):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError
    from backend.config import Settings
    from backend.dataset_service import DatasetService, NewDataset, Capture, claim as claim_dataset, finish
    from backend.db import dataset_jobs
    url=os.environ['TEST_POSTGRES_URL'];admin=create_engine(url);name='test_'+uuid.uuid4().hex
    with admin.begin() as c:c.execute(text(f'CREATE SCHEMA {name}'))
    engine=create_engine(url,connect_args={'options':f'-csearch_path={name} -clock_timeout=5000'})
    try:
        initialize(engine)
        service=DatasetService(engine,Settings(url,tmp_path,'test','fixture-password',tmp_path/'unused','test',False))
        did=str(uuid.uuid4());body=NewDataset(name='concurrent',station='station1',classes=['part'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:service.create('test',did,body),range(4)))
        assert len({row['id'] for row in results})==1
        for i in range(2):
            service.accept_video('test',did,str(uuid.uuid4()),'fixture.avi',f'{did}/fixture{i}.avi',1,str(i)*64,
                Capture(split='train',batch='b',cycle='c',camera='cam',product='p',lighting='normal',occlusion='none'),1)
        def claimant(_):
            try:return claim_dataset(engine)
            except IntegrityError:return None
        with ThreadPoolExecutor(max_workers=4) as pool: claimed=list(pool.map(claimant,range(4)))
        assert sum(row is not None for row in claimed)==1
        job=next(row for row in claimed if row)
        with engine.begin() as c:c.execute(update(dataset_jobs).where(dataset_jobs.c.id==job['id']).values(lease_until=0))
        following=claim_dataset(engine);assert following and following['id']!=job['id']
        assert not finish(engine,job,'SUCCEEDED','stale')
        assert finish(engine,following,'FAILED','fixture complete')
    finally:
        engine.dispose()
        with admin.begin() as c:c.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='PostgreSQL service not configured')
def test_postgres_management_reads_and_disable_publication_race(tmp_path):
    import threading
    from fastapi import HTTPException
    from sqlalchemy import inspect, select, update
    from algorithms.video import file_hash
    from backend.config import Settings
    from backend.db import model_registry, training_runs
    from backend.scenes import SceneDefinition, SceneRegistry
    from backend.training_service import list_runs
    url = os.environ['TEST_POSTGRES_URL']
    admin = create_engine(url)
    name = 'test_' + uuid.uuid4().hex
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {name}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={name} -clock_timeout=5000'})
    try:
        initialize(engine)
        initialize(engine)
        indexes = {item['name'] for item in inspect(engine).get_indexes('training_runs')}
        assert 'ix_training_owner_created_id' in indexes
        settings = Settings(url, tmp_path, 'test', 'fixture-password', tmp_path / 'unused', 'test', False)
        registry = SceneRegistry(engine, settings)
        identity = str(uuid.uuid4())
        path = registry.directory / (identity + '.pt')
        path.write_bytes(b'fixture-never-loaded')
        with engine.begin() as c:
            c.execute(model_registry.insert().values(id=identity, owner='test', name='fixture', version='1', task='detect',
                classes={'0': 'fixture'}, path='registered-models/' + path.name, sha256=file_hash(path), size=path.stat().st_size,
                enabled=True, notes='', created_at=1))
            c.execute(training_runs.insert().values(id=str(uuid.uuid4()), owner='test', name='100%_fixture',
                station='station1', request_key='fixture', request_hash='fixture', state='FAILED', stage='FAILED',
                recipe={'task': 'detect'}, policy={}, progress=1, message='', created_at=1, updated_at=1))
        listed = list_runs(engine, 'test', search='%_', status='attention')
        assert listed['total'] == 1 and listed['items'][0]['task'] == 'detect'
        assert list_runs(engine, 'other')['total'] == 0
        reached = threading.Event()
        verify = registry.verify_asset
        def checked(model):
            result = verify(model)
            reached.set()
            return result
        registry.verify_asset = checked
        body = SceneDefinition(name='fixture', model_id=identity, status='published',
            rules=[dict(id='one', name='fixture', class_id=0)])
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as c:
                c.execute(select(model_registry).where(model_registry.c.id == identity).with_for_update()).all()
                future = pool.submit(registry.save, body, 'test')
                assert reached.wait(3)
                c.execute(update(model_registry).where(model_registry.c.id == identity).values(enabled=False))
            with pytest.raises(HTTPException) as error:
                future.result(timeout=10)
            assert error.value.status_code == 409
        catalog = registry.catalog('test')
        assert catalog['scenes'] == [] and not catalog['models'][0]['enabled']
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='PostgreSQL service not configured')
def test_postgres_scene_create_idempotency_across_model_locks(tmp_path):
    import threading
    from fastapi import HTTPException
    from sqlalchemy import select
    from algorithms.video import file_hash
    from backend.config import Settings
    from backend.db import model_registry, scene_tasks
    from backend.scenes import SceneDefinition, SceneRegistry
    url = os.environ['TEST_POSTGRES_URL']
    admin = create_engine(url)
    name = 'test_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {name}'))
    engine = create_engine(url, connect_args={'options':f'-csearch_path={name} -clock_timeout=5000'})
    try:
        initialize(engine)
        registry = SceneRegistry(engine, Settings(url,tmp_path,'test','fixture-password',tmp_path/'unused','test',False))
        bodies = []
        for index in range(2):
            identity = str(uuid.uuid4())
            path = registry.directory / (identity + '.pt')
            path.write_bytes(f'fixture-{index}-never-loaded'.encode())
            with engine.begin() as connection:
                connection.execute(model_registry.insert().values(id=identity,owner='test',name=f'model-{index}',version='1',
                    task='detect',classes={'0':'fixture'},path='registered-models/'+path.name,sha256=file_hash(path),
                    size=path.stat().st_size,enabled=True,notes='',created_at=1))
            bodies.append(SceneDefinition(name='fixture',model_id=identity,status='published',
                rules=[dict(id='one',name='fixture',class_id=0)]))
        key = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:registry.save(bodies[0],'test',request_key=key),range(4)))
        assert len({row['id'] for row in results}) == 1
        # Two models lock different rows, then race to create the same scene ID.
        # One insert commits; the other must reconcile the unique-key conflict.
        gate = threading.Barrier(2)
        synchronized = threading.local()
        readiness = registry._readiness
        def synchronized_readiness(model):
            value = readiness(model)
            if not getattr(synchronized, 'waited', False):
                synchronized.waited = True
                gate.wait(timeout=10)
            return value
        registry._readiness = synchronized_readiness
        conflict_key = str(uuid.uuid4())
        def submit(body):
            try:
                return registry.save(body,'test',request_key=conflict_key)
            except HTTPException as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit,bodies))
        assert sum(isinstance(row,dict) for row in results) == 1
        assert results.count(409) == 1
        with engine.connect() as connection:
            assert len(connection.execute(select(scene_tasks)).all()) == 2
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='PostgreSQL service not configured')
def test_postgres_board_json_aggregates_and_hour_boundaries(monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from sqlalchemy import inspect
    from backend import board
    from backend.db import pilot_cycles, pilot_events, station_inspections
    url = os.environ['TEST_POSTGRES_URL']
    admin = create_engine(url)
    name = 'test_' + uuid.uuid4().hex
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {name}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={name}'})
    try:
        initialize(engine)
        initialize(engine)
        indexes = {item['name'] for item in inspect(engine).get_indexes('pilot_cycles')}
        assert 'ix_pilot_part_latest' in indexes
        start = datetime(2026, 11, 6, tzinfo=board.LOCAL_TIME).timestamp()
        monkeypatch.setattr(board, 'now', lambda _: start + 86399)
        rows = [dict(id=str(uuid.uuid4()), owner='board', station=f'station-{i}', active_station=None,
            product='fixture', part_number='P-1', source='camera', stage='CLOSED', result='MANUAL_OK',
            revision=1, config={}, checks={}, reason='', started_at=start+100, updated_at=start+101,
            finished_at=start+(12*3600+3599 if i < 6 else 86398)) for i in range(1, 7)]
        with engine.begin() as c:
            c.execute(pilot_cycles.insert(), rows)
            c.execute(pilot_events.insert().values(id=str(uuid.uuid4()), cycle_id=rows[0]['id'], owner='board',
                request_key=str(uuid.uuid4()), request_hash='fixture', revision=1, kind='REVIEW',
                at=start+200, payload={'disposition':'needs_rework','reason':'漏螺丝，缺卡扣'}, response={}))
            c.execute(station_inspections.insert().values(stream_id=str(uuid.uuid4()), station_id='station-1',
                owner='board', cycle_id=rows[0]['id'], context={}, status='FINISHED', reason='',
                started_at=start+100, finished_at=start+201,
                result={'compliance':{'status':'FAIL','reason':'', 'label':'未吹净'}}))
        app = SimpleNamespace(state=SimpleNamespace(engine=engine, terminals=SimpleNamespace(statuses=lambda _: {})))
        value, _ = board.snapshot(app, 'board')
        assert value['summary']['passed'] == 1 and value['summary']['processPassed'] == 6
        assert value['trends']['hour'][12]['passed'] == 5
        assert value['trends']['hour'][13]['passed'] == 0
        assert value['trends']['hour'][23]['passed'] == 1
        assert value['alertCount'] == 2 and len(value['alerts']) == 2
        counts = {item['name']: item['count'] for item in value['top']}
        assert counts['漏螺丝'] == counts['缺卡扣'] == counts['未吹净'] == 1
        assert board.snapshot(app, 'another-owner')[0]['summary']['total'] == 0
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='PostgreSQL service not configured')
def test_postgres_workers_claim_once():
    url=os.environ['TEST_POSTGRES_URL']
    admin=create_engine(url)
    name='test_'+uuid.uuid4().hex
    with admin.begin() as c:c.execute(text(f'CREATE SCHEMA {name}'))
    engine=create_engine(url,connect_args={'options':f'-csearch_path={name}'})
    try:
        initialize(engine)
        with engine.begin() as c:
            mid = str(uuid.uuid4())
            c.execute(media.insert().values(id=mid, owner='test', name='fixture.mp4', suffix='.mp4',
                sha256='fixture', size=1, metadata={}, created_at=1))
            c.execute(runs.insert().values(id=str(uuid.uuid4()),owner='test',media_id=mid,
                status='QUEUED',request_key='one',request_hash='hash',options={},model_hash='hash',
                progress=0,attempts=0,token=0,created_at=1,updated_at=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:claim(engine,60),range(2)))
        assert sum(result is not None for result in results)==1
    finally:
        engine.dispose()
        with admin.begin() as c:c.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='PostgreSQL service not configured')
def test_postgres_alert_archive_claim_retention_and_jsonb(tmp_path):
    from backend.config import Settings
    from backend.db import alert_evidence
    from backend.evidence import EvidenceStore, MIN_RETENTION, expire_evidence
    import cv2
    import numpy as np
    from sqlalchemy import select, update
    from sqlalchemy.exc import IntegrityError
    url=os.environ['TEST_POSTGRES_URL']
    admin=create_engine(url)
    name='test_'+uuid.uuid4().hex
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {name}'))
    engine=create_engine(url,connect_args={'options':f'-csearch_path={name}'})
    try:
        initialize(engine)
        initialize(engine)
        settings=Settings(url,tmp_path/'data','test','test-only-password',tmp_path/'model',environment='test')
        first,second=EvidenceStore(engine,settings),EvidenceStore(engine,settings)
        ok,encoded=cv2.imencode('.jpg',np.zeros((48,64,3),np.uint8))
        assert ok
        receipt=first.observe('test','session','stream','screw',0,encoded.tobytes(),
            {'compliance':{'status':'FAIL','reason':'PostgreSQL integration fixture'}})
        # SKIP LOCKED must exclude a spool locked by another claimant.
        with engine.begin() as c:
            c.execute(select(alert_evidence).where(alert_evidence.c.id==receipt['id']).with_for_update()).all()
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(second.process_one).result(timeout=3) is False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda store:store.process_one(),[first,second]))
        assert sum(results)==1
        with engine.connect() as c:
            row=c.execute(select(alert_evidence).where(alert_evidence.c.id==receipt['id'])).mappings().one()
            kind=c.execute(text("select data_type from information_schema.columns where table_schema=:schema and table_name='alert_evidence' and column_name='details'"),{'schema':name}).scalar_one()
        assert row['status']=='READY' and kind=='jsonb'
        assert row['retain_until']-row['ready_at']>=MIN_RETENTION
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(update(alert_evidence).values(retain_until=row['ready_at']+1))
        assert expire_evidence(engine,settings.data_dir,now_ts=row['retain_until'])['expired']==0
        assert expire_evidence(engine,settings.data_dir,now_ts=row['retain_until']+1)['expired']==1
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()
