"""Real server-database coverage; each run owns a disposable PostgreSQL schema."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from backend.app import create_app
from backend.config import Settings
from backend.db import initialize, station_inspections


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='PostgreSQL service not configured')
def test_postgres_daily_history_and_concurrent_deletion(tmp_path):
    url = os.environ['TEST_POSTGRES_URL']
    admin = create_engine(url)
    schema_name = 'test_' + uuid.uuid4().hex
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema_name}'))
    isolated_url = make_url(url).update_query_dict({'options': f'-csearch_path={schema_name} -clock_timeout=5000'})
    model = tmp_path / 'model.pt'
    model.write_bytes(b'fixture-not-for-inference')
    settings = Settings(isolated_url.render_as_string(hide_password=False), tmp_path / 'data',
                        'admin', 'fixture-password-12345', model, 'test', False, pose_backend='yolo')
    app = None
    try:
        app = create_app(settings)
        initialize(app.state.engine)
        initialize(app.state.engine)
        local = datetime.now(timezone(timedelta(hours=8)))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        day = local.strftime('%Y-%m-%d')
        ids = [str(uuid.uuid4()) for _ in range(4)]
        rows = []
        for identity, verdict, ended in zip(ids, ('FAIL', 'FAIL', 'PASS', 'REVIEW'),
                                            (start + 2, start + 3, start - 1, start + 4)):
            result = {'compliance': {'status': verdict, 'label': verdict, 'reason': '原始算法证据'}}
            if identity == ids[1]:
                result['manualReview'] = {'source': 'manual', 'status': 'PASS', 'label': '人工确认合规',
                                          'reason': '现场确认完成', 'reviewer': 'admin', 'reviewedAt': ended}
            rows.append(dict(stream_id=identity, station_id='station-2', owner='admin', cycle_id=None,
                             context={'analysisType': 'sticker', 'cameraId': 'pg-fixture', 'inputSource': 'camera'},
                             status='INTERRUPTED' if identity == ids[3] else 'FINISHED', result=result,
                             reason='结束归档', started_at=start - 60, finished_at=ended))
        with app.state.engine.begin() as c:
            c.execute(station_inspections.insert(), rows)
        with TestClient(app) as client:
            login = client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password})
            assert login.status_code == 200
            client.headers['X-CSRF-Token'] = login.json()['csrf']
            endpoint = '/api/v1/board/history'
            response = client.get(endpoint, params={'date': day, 'verdict': 'ALL'})
            assert response.status_code == 200, response.text
            history = response.json()
            assert history['summary'] == dict(total=3, failed=1, passed=1, pending=1, complianceRate=50)
            assert [item['verdict'] for item in history['items']] == ['FAIL', 'REVIEW', 'PASS']
            assert client.get(endpoint, params={'date': day}).json()['items'][0]['streamId'] == ids[0]
            detail = client.get(endpoint + '/' + ids[1]).json()['item']
            assert detail['verdict'] == 'PASS' and detail['result']['compliance']['status'] == 'FAIL'
            target = endpoint + '/' + ids[0]
            with ThreadPoolExecutor(max_workers=3) as pool:
                deleted = list(pool.map(lambda _: client.delete(target), range(3)))
            assert [r.status_code for r in deleted] == [200, 200, 200]
            live = client.get(endpoint, params={'date': day, 'verdict': 'ALL'}).json()
            assert live['summary']['failed'] == 0 and live['deletedCount'] == 1
            trash = client.get(endpoint, params={'date': day, 'deleted': 'true', 'verdict': 'ALL'}).json()
            assert trash['total'] == 1 and trash['items'][0]['streamId'] == ids[0]
            with ThreadPoolExecutor(max_workers=3) as pool:
                restored = list(pool.map(lambda _: client.post(target + '/restore'), range(3)))
            assert [r.status_code for r in restored] == [200, 200, 200]
            assert client.get(endpoint, params={'date': day}).json()['summary']['failed'] == 1
            with app.state.engine.connect() as c:
                assert c.scalar(select(station_inspections.c.result).where(
                    station_inspections.c.stream_id == ids[0])) == rows[0]['result']
    finally:
        if app is not None:
            app.state.engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA {schema_name} CASCADE'))
        admin.dispose()
