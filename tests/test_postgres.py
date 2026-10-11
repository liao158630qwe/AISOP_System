"""Opt-in PostgreSQL concurrency test, isolated in a disposable schema.

Set TEST_POSTGRES_URL to a dedicated test database using psycopg. Never use production.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from backend.db import initialize, claim, runs


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
            c.execute(runs.insert().values(id=str(uuid.uuid4()),owner='test',media_id=str(uuid.uuid4()),
                status='QUEUED',request_key='one',request_hash='hash',options={},model_hash='hash',
                progress=0,attempts=0,token=0,created_at=1,updated_at=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:claim(engine,60),range(2)))
        assert sum(result is not None for result in results)==1
    finally:
        engine.dispose()
        with admin.begin() as c:c.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()
