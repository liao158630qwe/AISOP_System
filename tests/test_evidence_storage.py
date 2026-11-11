"""Real media/SQL retention integration and crash-boundary fault injection.

Inference verdicts are fixtures; encoding, file serving, transactions and
retention are real. These tests do not measure detector accuracy.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import gzip
import json
from pathlib import Path
import threading
import time
import uuid

import cv2
import numpy as np
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.cleanup import cleanup_data
from backend.config import Settings
from backend.db import alert_evidence as alerts, initialize, make_engine, option_field, runs
from backend.evidence import EvidenceStore, MIN_RETENTION, expire_evidence, safe_path, sha256
from backend.worker import execute_one
from conftest import new_run
from test_transport import frame_headers
from test_worker import fake_analyzer

FAIL = {'compliance': {'status': 'FAIL', 'label': '异常', 'reason': '测试工件缺件'}}
PASS = {'compliance': {'status': 'PASS', 'label': '通过', 'reason': '测试工件正常'}}
REVIEW = {'compliance': {'status': 'REVIEW', 'label': '复核', 'reason': '证据不足'}}


def jpeg(value=0):
    ok, data = cv2.imencode('.jpg', np.full((48, 64, 3), value, np.uint8))
    assert ok
    return data.tobytes()


def capture(store, stream=None):
    stream = stream or str(uuid.uuid4())
    for i in range(4):
        result = store.observe('admin', 'session', stream, 'screw', i * .25, jpeg(i * 40), PASS)
        assert result is None
    return store.observe('admin', 'session', stream, 'screw', 1, jpeg(200), FAIL)


def row_for(engine, identity):
    with engine.connect() as c:
        return dict(c.execute(select(alerts).where(alerts.c.id == identity)).mappings().one())


def test_real_mp4_archive_private_range_download_and_restart(authenticated):
    settings, app, client = authenticated
    store = app.state.evidence
    receipt = capture(store)
    assert receipt['status'] == 'PENDING'
    folder = store.spool / receipt['id']
    package = json.loads(gzip.decompress((folder / 'manifest.json.gz').read_bytes()))
    assert len(package['samples']) == 5
    assert client.get(f"/api/v1/alerts/{receipt['id']}/video").status_code == 409
    assert store.process_one()
    row = row_for(app.state.engine, receipt['id'])
    assert row['status'] == 'READY', row
    assert row['retain_until'] - row['ready_at'] >= MIN_RETENTION
    video = settings.data_dir / row['video_path']
    assert row['sha256'] == sha256(video)
    raw = video.read_bytes()
    assert raw.index(b'moov') < raw.index(b'mdat'), 'MP4 must support fast start'
    cap = cv2.VideoCapture(str(video))
    decoded = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            decoded.append(float(frame.mean()))
    finally:
        cap.release()
    assert len(decoded) >= 5 and decoded[-1] - decoded[0] > 150
    assert not folder.exists()
    url = f"/api/v1/alerts/{row['id']}/video"
    response = client.get(url, headers={'Range': 'bytes=0-31'})
    assert response.status_code == 206 and response.content == raw[:32]
    assert client.head(url).status_code == 200
    assert 'details' not in client.get('/api/v1/alerts').json()['items'][0]
    restored = EvidenceStore(app.state.engine, settings)
    restored.reconcile()
    assert not restored.process_one()
    assert client.get(url).content == raw
    with app.state.engine.begin() as c:
        c.execute(update(alerts).values(owner='another-owner'))
    assert client.get(url).status_code == 404
    assert client.get(f"/api/v1/alerts/{row['id']}").status_code == 404
    assert client.get('/api/v1/alerts').json()['items'] == []


def test_http_fail_and_final_review_are_automatically_archived(authenticated):
    _, app, client = authenticated
    service = app.state.screw_realtime
    def predict(payload, *, stream_key, frame_time):
        service.registry.get(stream_key, frame_time, ('fixture',))
        return FAIL if frame_time else PASS
    service.predict = predict
    service.finish = lambda key: REVIEW
    headers = frame_headers()
    url = '/api/v1/realtime/frame?analysis_type=screw'
    assert 'evidence' not in client.post(url, content=jpeg(), headers=headers).json()
    failed = client.post(url, content=jpeg(200), headers={**headers, 'X-Frame-Time': '1'}).json()
    assert failed['evidence']['status'] == 'PENDING'
    finish = f"/api/v1/realtime/streams/{headers['X-Stream-Id']}/finish"
    first = client.post(finish).json()
    assert first['evidence']['id'] != failed['evidence']['id']
    assert client.post(finish).json() == first
    client.delete(f"/api/v1/realtime/streams/{headers['X-Stream-Id']}")
    assert app.state.evidence.snapshot()['bufferBytes'] == 0
    assert app.state.evidence.process_one()
    assert app.state.evidence.process_one()
    assert len(client.get('/api/v1/alerts').json()['items']) == 2


def test_thirty_day_boundary_hold_longer_policy_and_dry_run(authenticated):
    settings, app, client = authenticated
    receipt = capture(app.state.evidence)
    app.state.evidence.process_one()
    row = row_for(app.state.engine, receipt['id'])
    path = settings.data_dir / row['video_path']
    for stamp in (row['ready_at'] + 29*86400, row['retain_until']):
        assert expire_evidence(app.state.engine, settings.data_dir, now_ts=stamp)['expired'] == 0
        assert path.is_file()
    stamp = row['retain_until'] + 1
    assert expire_evidence(app.state.engine, settings.data_dir, now_ts=stamp, retention=60*86400)['eligible'] == 0
    with app.state.engine.begin() as c:
        c.execute(update(alerts).where(alerts.c.id == row['id']).values(legal_hold=True))
    assert expire_evidence(app.state.engine, settings.data_dir, now_ts=stamp)['eligible'] == 0
    with app.state.engine.begin() as c:
        c.execute(update(alerts).where(alerts.c.id == row['id']).values(legal_hold=False))
    assert expire_evidence(app.state.engine, settings.data_dir, now_ts=stamp, dry_run=True)['eligible'] == 1
    assert path.is_file() and row_for(app.state.engine, row['id'])['status'] == 'READY'
    assert expire_evidence(app.state.engine, settings.data_dir, now_ts=stamp)['expired'] == 1
    assert not path.exists()
    assert client.get(f"/api/v1/alerts/{row['id']}/video").status_code == 410
    with pytest.raises(ValueError):
        expire_evidence(app.state.engine, settings.data_dir, retention=MIN_RETENTION-1)


def test_storage_constraints_reject_short_or_null_ready_retention(context):
    settings, app, _ = context
    with pytest.raises(ValueError, match='30'):
        replace(settings, evidence_retention_seconds=1)
    receipt = capture(app.state.evidence)
    app.state.evidence.process_one()
    row = row_for(app.state.engine, receipt['id'])
    for value in (None, row['ready_at'] + MIN_RETENTION - 1):
        with pytest.raises(IntegrityError):
            with app.state.engine.begin() as c:
                c.execute(update(alerts).where(alerts.c.id == row['id']).values(retain_until=value))


def test_database_outage_leaves_recoverable_spool(context):
    settings, app, _ = context
    engine = app.state.engine
    def fail(connection, cursor, statement, parameters, context, executemany):
        if 'alert_evidence' in statement:
            raise OperationalError('unavailable', {}, Exception('offline'))
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(OperationalError):
            capture(app.state.evidence)
    finally:
        event.remove(engine, 'before_cursor_execute', fail)
    saved = list(app.state.evidence.spool.glob('*/manifest.json.gz'))
    assert len(saved) == 1
    restarted = EvidenceStore(engine, settings)
    restarted.reconcile()
    assert restarted.process_one()
    assert row_for(engine, saved[0].parent.name)['status'] == 'READY'


def test_committed_insert_ack_loss_keeps_spool_and_deduplicates(context, monkeypatch):
    _, app, _ = context
    store = app.state.evidence
    original = store._insert
    def lost(row):
        original(row)
        raise OperationalError('commit ack lost', {}, Exception('offline'))
    monkeypatch.setattr(store, '_insert', lost)
    stream = str(uuid.uuid4())
    with pytest.raises(OperationalError):
        capture(store, stream)
    monkeypatch.setattr(store, '_insert', original)
    result = store.observe('admin', 'session', stream, 'screw', 1.25, jpeg(), FAIL)
    assert result['status'] == 'PENDING'
    with app.state.engine.connect() as c:
        assert len(c.execute(select(alerts.c.id)).all()) == 1
    assert store.process_one()
    assert row_for(app.state.engine, result['id'])['status'] == 'READY'


def test_corrupt_spool_is_visible_and_retry_preserves_evidence(authenticated):
    _, app, client = authenticated
    store = app.state.evidence
    receipt = capture(store)
    image = store.spool / receipt['id'] / '00000.jpg'
    original = image.read_bytes()
    image.write_bytes(b'corrupt')
    for _ in range(3):
        assert store.process_one()
        with app.state.engine.begin() as c:
            c.execute(update(alerts).where(alerts.c.id == receipt['id'], alerts.c.status == 'PROCESSING').values(lease_until=0))
    row = row_for(app.state.engine, receipt['id'])
    assert row['status'] == 'FAILED' and row['video_path'] is None
    assert image.exists()
    image.write_bytes(original)
    assert client.post(f"/api/v1/alerts/{row['id']}/retry").status_code == 200
    assert store.process_one()
    assert row_for(app.state.engine, row['id'])['status'] == 'READY'


def test_two_archivers_claim_once_and_encoding_holds_no_write_lock(context, monkeypatch):
    settings, app, _ = context
    store = app.state.evidence
    capture(store)
    other = EvidenceStore(app.state.engine, settings)
    entered, release = threading.Event(), threading.Event()
    original = store._encode
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(store, '_encode', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(store.process_one)
        assert entered.wait(2)
        try:
            assert pool.submit(other.process_one).result(timeout=2) is False
            with app.state.engine.begin() as c:
                c.execute(update(alerts).values(reason='concurrent metadata update'))
        finally:
            release.set()
        assert work.result()


def test_old_archiver_token_cannot_publish(context, monkeypatch):
    settings, app, _ = context
    store = app.state.evidence
    receipt = capture(store)
    other = EvidenceStore(app.state.engine, settings)
    original = store._encode
    def stolen(*args):
        original(*args)
        with app.state.engine.begin() as c:
            c.execute(update(alerts).values(lease_until=0))
        assert other.process_one()
    monkeypatch.setattr(store, '_encode', stolen)
    assert store.process_one()
    row = row_for(app.state.engine, receipt['id'])
    assert row['status'] == 'READY' and row['token'] == 2
    assert '/2/video.mp4' in row['video_path']


def test_unlink_failure_is_retried_without_losing_metadata(context, monkeypatch):
    import backend.evidence as module
    settings, app, _ = context
    receipt = capture(app.state.evidence)
    app.state.evidence.process_one()
    row = row_for(app.state.engine, receipt['id'])
    real = module.shutil.rmtree
    def fail(path):
        raise PermissionError('file in use')
    monkeypatch.setattr(module.shutil, 'rmtree', fail)
    result = expire_evidence(app.state.engine, settings.data_dir, now_ts=row['retain_until']+1)
    assert result['errors'] == 1 and row_for(app.state.engine, row['id'])['status'] == 'DELETING'
    monkeypatch.setattr(module.shutil, 'rmtree', real)
    # Extending policy also protects a failed deletion already tombstoned.
    assert expire_evidence(app.state.engine, settings.data_dir, now_ts=row['retain_until']+2,
        retention=60*86400)['eligible'] == 0
    assert expire_evidence(app.state.engine, settings.data_dir, now_ts=row['retain_until']+2)['expired'] == 1


def test_offline_evidence_reference_protects_run_from_short_cleanup(authenticated, video_bytes):
    settings, app, client = authenticated
    job = new_run(client, video_bytes)
    def analysis(*args, **kwargs):
        fake_analyzer(*args, **kwargs)
        return FAIL
    assert execute_one(app.state.engine, settings, analysis)
    with app.state.engine.connect() as c:
        proof = dict(c.execute(select(alerts)).mappings().one())
        run = dict(c.execute(select(runs).where(runs.c.id == job['id'])).mappings().one())
    assert proof['source_id'] == job['id'] and proof['status'] == 'READY'
    early = proof['ready_at'] + 86400
    cleanup_data(app.state.engine, settings.data_dir, run_retention_seconds=100, now_ts=early)
    assert (settings.data_dir / proof['video_path']).is_file()
    with app.state.engine.connect() as c:
        assert c.execute(select(runs.c.id)).first()
    expire_evidence(app.state.engine, settings.data_dir, now_ts=proof['retain_until']+1)
    cleanup_data(app.state.engine, settings.data_dir, now_ts=proof['retain_until']+1)
    assert not (settings.data_dir / 'runs' / run['result_dir']).exists()


def test_pagination_stable_under_equal_timestamps_and_insert(authenticated):
    _, app, client = authenticated
    store = app.state.evidence
    for i in range(5):
        store.observe('admin', 'session', str(i), 'screw', 0, None, REVIEW, final=True)
    with app.state.engine.begin() as c:
        c.execute(update(alerts).values(created_at=100))
    first = client.get('/api/v1/alerts?limit=2').json()
    assert first['next_cursor']
    store.observe('admin', 'session', 'new', 'screw', 0, None, REVIEW, final=True)
    ids = [r['id'] for r in first['items']]
    cursor = first['next_cursor']
    while cursor:
        page = client.get('/api/v1/alerts', params={'limit': 2, 'cursor': cursor}).json()
        ids.extend(r['id'] for r in page['items'])
        cursor = page['next_cursor']
    assert len(ids) == len(set(ids)) == 5
    assert client.get('/api/v1/alerts?cursor=invalid').status_code == 422
    assert client.get('/api/v1/alerts?limit=101').status_code == 422


def test_evidence_paths_cannot_escape_or_delete_other_storage(context):
    settings, app, _ = context
    for path in ('../keep', str(settings.data_dir.parent / 'keep')):
        with pytest.raises(ValueError):
            safe_path(settings.data_dir, path)
    receipt = capture(app.state.evidence)
    app.state.evidence.process_one()
    row = row_for(app.state.engine, receipt['id'])
    with app.state.engine.begin() as c:
        c.execute(update(alerts).values(video_path='uploads/important.mp4'))
    with pytest.raises(ValueError, match='identity'):
        expire_evidence(app.state.engine, settings.data_dir, now_ts=row['retain_until']+1)
    assert row_for(app.state.engine, row['id'])['status'] == 'READY'


def test_buffers_are_bounded_without_per_frame_database_writes(context):
    _, app, _ = context
    store, statements = app.state.evidence, []
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(app.state.engine, 'before_cursor_execute', record)
    try:
        for i in range(40):
            for frame in range(10):
                store.observe('admin', 'session', str(i), 'screw', frame*.25, b'x'*(1024*1024), PASS)
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', record)
    assert not statements
    snapshot = store.snapshot()
    assert snapshot['bufferBytes'] <= snapshot['maxBufferBytes']
    assert snapshot['activeBuffers'] <= 32


def test_background_thread_processes_durable_queue_and_stops(context):
    _, app, _ = context
    store = app.state.evidence
    receipt = capture(store)
    store.start()
    try:
        deadline = time.monotonic()+10
        while row_for(app.state.engine, receipt['id'])['status'] != 'READY':
            assert time.monotonic() < deadline
            time.sleep(.05)
    finally:
        store.close()
    assert not store.thread.is_alive()


def test_env_cannot_shorten_evidence_or_run_retention(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('APP_PASSWORD', 'test-password')
    monkeypatch.setenv('EVIDENCE_RETENTION_SECONDS', '2591999')
    with pytest.raises(RuntimeError, match='30'):
        Settings.from_env()
    monkeypatch.setenv('EVIDENCE_RETENTION_SECONDS', '5184000')
    assert Settings.from_env().evidence_retention_seconds == 5184000
    monkeypatch.setenv('RUN_RETENTION_SECONDS', '3600')
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_bound_history_filters_use_expression_index(context):
    _, app, _ = context
    engine, observed = app.state.engine, []
    statement = select(runs.c.id).where(runs.c.owner == 'admin', option_field('analysis_type') == 'screw')
    statement = statement.order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(10)
    def record(connection, cursor, sql, parameters, context, executemany):
        if sql.startswith('SELECT analysis_runs.id'):
            observed.append((sql, parameters))
    event.listen(engine, 'before_cursor_execute', record)
    try:
        with engine.connect() as c:
            c.execute(statement).all()
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    sql, parameters = observed[0]
    with engine.connect() as c:
        plan = c.exec_driver_sql('EXPLAIN QUERY PLAN '+sql, parameters).all()
    assert any('ix_runs_owner_type_created' in str(item) for item in plan), plan


def test_postgres_schema_compiles_jsonb_and_check_constraints():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable, CreateIndex
    dialect = postgresql.dialect()
    ddl = str(CreateTable(alerts).compile(dialect=dialect))
    assert 'details JSONB' in ddl and 'size_bytes BIGINT' in ddl
    assert 'ck_evidence_30_days' in ddl and 'ck_evidence_ready' in ddl
    for index in alerts.indexes:
        assert 'CREATE INDEX' in str(CreateIndex(index).compile(dialect=dialect))


def test_disk_full_returns_explicit_unavailable_receipt(authenticated, monkeypatch):
    import backend.evidence as module
    _, app, client = authenticated
    app.state.screw_realtime.predict = lambda *args, **kwargs: FAIL
    def disk_full(*args):
        raise OSError('disk full')
    monkeypatch.setattr(module, 'durable_write', disk_full)
    response = client.post('/api/v1/realtime/frame?analysis_type=screw', content=jpeg(), headers=frame_headers())
    assert response.status_code == 200
    assert response.json()['compliance']['status'] == 'FAIL'
    assert response.json()['evidence']['status'] == 'UNAVAILABLE'
    assert client.get('/api/v1/alerts').json()['items'] == []
    assert app.state.evidence.snapshot()['lastError'] == 'OSError'
