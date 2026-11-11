"""Fault injection for retention, worker recovery and multi-terminal sessions."""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError

from backend.cleanup import cleanup_data
from backend.db import claim, media, run_metrics, runs, sessions
from backend.streams import StreamRegistry
from backend.worker import execute_one, poll_once
from conftest import new_run
from test_cleanup import _media, _run
from test_transport import frame_headers


def old_data(settings, engine, *, status='SUCCEEDED'):
    root = settings.data_dir
    with engine.begin() as c:
        c.execute(media.insert().values(**_media('retention-media', 1)))
        c.execute(runs.insert().values(**_run('retention-run', 'retention-media', status, 1,
            'retention-run-1' if status == 'SUCCEEDED' else None)))
    folder = root / 'runs' / 'retention-run-1'
    folder.mkdir()
    (folder / 'report.html').write_text('evidence')
    upload = root / 'uploads' / 'retention-media.mp4'
    upload.write_bytes(b'media')
    for path in (folder, upload):
        os.utime(path, (1, 1))
    return folder, upload


def sweep(settings, engine, **kwargs):
    return cleanup_data(engine, settings.data_dir, now_ts=10000,
        run_retention_seconds=100, upload_retention_seconds=100,
        orphan_retention_seconds=100, **kwargs)


@pytest.mark.parametrize('status', ['RUNNING', 'CANCELLING', 'QUEUED'])
def test_active_unpublished_artifacts_survive_cleanup(context, status):
    settings, app, _ = context
    folder, upload = old_data(settings, app.state.engine, status=status)
    result = sweep(settings, app.state.engine)
    assert result['runs'] == result['media'] == result['files'] == 0
    assert (folder / 'report.html').read_text() == 'evidence' and upload.is_file()


def test_database_rollback_never_removes_evidence(context):
    settings, app, _ = context
    engine = app.state.engine
    folder, upload = old_data(settings, engine)
    def fail_delete(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM analysis_runs'):
            raise RuntimeError('database failure')
    event.listen(engine, 'before_cursor_execute', fail_delete)
    try:
        with pytest.raises(RuntimeError, match='database failure'):
            sweep(settings, engine)
    finally:
        event.remove(engine, 'before_cursor_execute', fail_delete)
    with engine.connect() as c:
        assert c.execute(select(runs.c.id)).scalar_one() == 'retention-run'
    assert folder.is_dir() and upload.is_file()


def test_disk_io_holds_no_database_write_lock_and_retries_failures(context, monkeypatch):
    import backend.cleanup as cleanup
    settings, app, _ = context
    engine = app.state.engine
    folder, upload = old_data(settings, engine)
    real_remove = cleanup._remove
    def locked_file(path, dry_run):
        if path == folder:
            # A separate writer must succeed while filesystem work is running.
            def write():
                with engine.begin() as c:
                    c.execute(sessions.insert().values(token_hash='during-cleanup', owner='admin',
                        csrf='x', expires_at=999999))
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(write).result(timeout=2)
            raise PermissionError('file held open')
        return real_remove(path, dry_run)
    monkeypatch.setattr(cleanup, '_remove', locked_file)
    assert sweep(settings, engine)['errors'] == 1
    assert folder.is_dir() and not upload.exists()
    monkeypatch.setattr(cleanup, '_remove', real_remove)
    sweep(settings, engine)
    assert not folder.exists()


def test_corrupt_cleanup_path_is_rejected_without_deleting_anything(context, tmp_path):
    settings, app, _ = context
    folder, upload = old_data(settings, app.state.engine)
    outside = settings.data_dir / 'keep'
    outside.mkdir()
    with app.state.engine.begin() as c:
        c.execute(update(runs).values(result_dir='../keep'))
    with pytest.raises(ValueError, match='outside'):
        sweep(settings, app.state.engine)
    assert outside.is_dir() and folder.is_dir() and upload.is_file()


def test_cleanup_is_bounded_and_expires_sessions(context):
    settings, app, _ = context
    with app.state.engine.begin() as c:
        c.execute(media.insert(), [_media('old-' + str(i), 1) for i in range(3)])
        c.execute(sessions.insert(), [dict(token_hash=str(i), owner='admin', csrf='x',
            expires_at=1 if i < 2 else 20000) for i in range(3)])
    result = sweep(settings, app.state.engine, batch_size=1)
    assert result['media'] == result['sessions'] == 1
    with app.state.engine.connect() as c:
        assert len(c.execute(select(media.c.id)).all()) == 2
        assert len(c.execute(select(sessions.c.token_hash)).all()) == 2


def test_cleanup_failure_does_not_starve_worker(context, monkeypatch):
    import backend.worker as worker
    settings, app, _ = context
    calls = []
    def fail(*args, **kwargs):
        raise PermissionError('disk busy')
    monkeypatch.setattr(worker, 'cleanup_data', fail)
    monkeypatch.setattr(worker, 'execute_one', lambda *args: calls.append('job') or True)
    assert poll_once(app.state.engine, settings, cleanup_due=True)
    assert calls == ['job']


def test_missing_optional_dependency_finishes_job(authenticated, video_bytes, monkeypatch):
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    def fail(kind):
        raise ImportError('optional analyzer unavailable')
    monkeypatch.setattr('backend.worker.load_analyzer', fail)
    assert execute_one(app.state.engine, settings)
    result = client.get('/api/v1/analysis-runs/' + row['id']).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'ANALYSIS_FAILED'


def test_lost_heartbeat_leaves_job_for_retry_not_user_cancel(authenticated, video_bytes, monkeypatch):
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    failed = threading.Event()
    def fail(*args):
        failed.set()
        raise OperationalError('heartbeat', {}, Exception('offline'))
    monkeypatch.setattr('backend.worker.heartbeat', fail)
    def analyzer(*args, progress, **kwargs):
        assert failed.wait(3)
        # The heartbeat sets lost immediately after the injected exception.
        for _ in range(100):
            progress(.1)
            time.sleep(.01)
        pytest.fail('worker did not stop after losing its lease')
    execute_one(app.state.engine, settings, analyzer)
    result = client.get('/api/v1/analysis-runs/' + row['id']).json()
    assert result['status'] == 'RUNNING'
    with app.state.engine.begin() as c:
        c.execute(update(runs).where(runs.c.id == row['id']).values(lease_until=0))
    assert claim(app.state.engine, 60)['attempts'] == 2


def test_database_read_outage_preserves_claimed_job(authenticated, video_bytes, monkeypatch):
    import backend.worker as worker
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    original_claim, connect = worker.claim, app.state.engine.connect
    def fail_once():
        monkeypatch.setattr(app.state.engine, 'connect', connect)
        raise OperationalError('temporary read outage', {}, Exception())
    def claim_then_fail(*args):
        job = original_claim(*args)
        monkeypatch.setattr(app.state.engine, 'connect', fail_once)
        return job
    monkeypatch.setattr(worker, 'claim', claim_then_fail)
    assert worker.execute_one(app.state.engine, settings, lambda *args, **kwargs: pytest.fail('must not infer'))
    assert client.get('/api/v1/analysis-runs/' + row['id']).json()['status'] == 'RUNNING'


def test_exhausted_lease_updates_terminal_metrics(authenticated, video_bytes):
    settings, app, client = authenticated
    row = new_run(client, video_bytes)
    claim(app.state.engine, 60)
    with app.state.engine.begin() as c:
        c.execute(update(runs).where(runs.c.id == row['id']).values(attempts=3, lease_until=0))
    assert claim(app.state.engine, 60) is None
    with app.state.engine.connect() as c:
        metric = c.execute(select(run_metrics)).mappings().one()
    assert metric['finished_at'] and metric['failure_code'] == 'WORKER_LOST'


def test_sqlite_parallel_claims_are_unique(context):
    _, app, _ = context
    with app.state.engine.begin() as c:
        c.execute(media.insert().values(**_media('media', 1)))
        c.execute(runs.insert(), [_run('queue-' + str(i), 'media', 'QUEUED', i) for i in range(8)])
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: claim(app.state.engine, 60), range(8)))
    assert len({row['id'] for row in claimed if row}) == 8


def test_sixteen_terminal_states_survive_capacity_and_configuration_changes():
    registry = StreamRegistry()
    original = [registry.get(('operator', str(i)), 0, ('model',), clock=0) for i in range(16)]
    with pytest.raises(HTTPException) as error:
        registry.get(('operator', 'overflow'), 0, ('model',), clock=1)
    assert error.value.status_code == 429
    for i, state in enumerate(original):
        assert registry.get(('operator', str(i)), 1, ('model',), clock=1) is state
    with pytest.raises(HTTPException) as error:
        registry.get(('operator', '0'), 2, ('changed-model',), clock=2)
    assert error.value.status_code == 409 and registry.items[('operator', '0')] is original[0]
    with pytest.raises(HTTPException):
        registry.get(('operator', '0'), 400, ('model',), clock=400)


def test_restart_cannot_pass_using_only_post_restart_frames():
    registry = StreamRegistry()
    key = ('operator', 'restored-browser-stream')
    registry.get(key, 45, ('model',))
    result = registry.protect_result(key, {'compliance': {'status': 'PASS'}})
    assert result['compliance']['status'] == 'REVIEW'
    assert result['evidenceGaps'][0]['start'] == 0 and result['evidenceGaps'][0]['end'] == 45


def test_release_is_rejected_while_frame_is_in_flight(authenticated):
    _, app, client = authenticated
    started, release = threading.Event(), threading.Event()
    stream = str(uuid.uuid4())
    def predict(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return {'people': []}
    app.state.realtime.predict = predict
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, '/api/v1/realtime/frame', content=b'frame', headers=frame_headers(stream))
        assert started.wait(2)
        try:
            assert client.delete('/api/v1/realtime/streams/' + stream).status_code == 429
        finally:
            release.set()
        assert future.result().status_code == 200
    assert client.delete('/api/v1/realtime/streams/' + stream).status_code == 200


def test_finish_atomically_blocks_late_frames(authenticated):
    _, app, client = authenticated
    started, release = threading.Event(), threading.Event()
    stream = str(uuid.uuid4())
    def finish(key):
        started.set()
        assert release.wait(5)
        return {'final': True}
    app.state.screw_realtime.finish = finish
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f'/api/v1/realtime/streams/{stream}/finish')
        assert started.wait(2)
        try:
            result = client.post('/api/v1/realtime/frame?analysis_type=screw', content=b'frame', headers=frame_headers(stream))
            assert result.status_code == 429
        finally:
            release.set()
        assert future.result().status_code == 200
    assert not app.state.realtime_transport.pending


def test_database_outage_returns_retryable_error_without_details(authenticated, monkeypatch):
    _, app, client = authenticated
    def fail(*args, **kwargs):
        raise OperationalError('SELECT secret_password', {}, Exception('secret-host'))
    monkeypatch.setattr(app.state.engine, 'connect', fail)
    response = client.get('/api/v1/session')
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '2'
    assert 'secret' not in response.text


def test_readiness_requires_recent_worker_but_liveness_survives_database_outage(context, monkeypatch):
    from backend.db import worker_heartbeats
    from backend.health import pulse_worker, remove_worker
    _, app, client = context
    assert client.get('/api/v1/live').json()['status'] == 'alive'
    assert client.get('/api/v1/ready').status_code == 503
    identity = str(uuid.uuid4())
    pulse_worker(app.state.engine, identity)
    assert client.get('/api/v1/ready').json()['workers']['active'] == 1
    with app.state.engine.begin() as c:
        c.execute(update(worker_heartbeats).values(expires_at=0))
    assert client.get('/api/v1/ready').status_code == 503
    pulse_worker(app.state.engine, identity)
    assert client.get('/api/v1/ready').status_code == 200
    remove_worker(app.state.engine, identity)
    assert client.get('/api/v1/ready').status_code == 503
    def unavailable():
        raise OperationalError('db offline', {}, Exception())
    monkeypatch.setattr(app.state.engine, 'connect', unavailable)
    assert client.get('/api/v1/ready').status_code == 503
    assert client.get('/api/v1/live').status_code == 200


@pytest.mark.parametrize('reconnect', [True, False])
def test_upload_commit_ack_loss_keeps_committed_file(authenticated, video_bytes, monkeypatch, reconnect):
    from contextlib import contextmanager
    settings, app, client = authenticated
    engine = app.state.engine
    begin, connect = engine.begin, engine.connect
    def unavailable():
        raise OperationalError('db offline', {}, Exception())
    @contextmanager
    def lost_ack():
        with begin() as c:
            yield c
        if not reconnect:
            monkeypatch.setattr(engine, 'connect', unavailable)
        raise OperationalError('commit acknowledgement lost', {}, Exception())
    monkeypatch.setattr(engine, 'begin', lost_ack)
    response = client.post('/api/v1/media?filename=sample.avi', content=video_bytes)
    assert response.status_code == (201 if reconnect else 503)
    monkeypatch.setattr(engine, 'connect', connect)
    with engine.connect() as c:
        row = c.execute(select(media)).mappings().one()
    assert (settings.data_dir / 'uploads' / (row['id'] + row['suffix'])).is_file()


def test_media_expiring_during_validation_cannot_create_broken_job(authenticated, video_bytes, monkeypatch):
    import algorithms.video as video
    from sqlalchemy import delete
    _, app, client = authenticated
    mid = client.post('/api/v1/media?filename=sample.avi', content=video_bytes).json()['id']
    original = video.file_hash
    def expire(path):
        with app.state.engine.begin() as c:
            c.execute(delete(media).where(media.c.id == mid))
        return original(path)
    monkeypatch.setattr(video, 'file_hash', expire)
    response = client.post('/api/v1/analysis-runs', json={'media_id': mid}, headers={'Idempotency-Key': 'expired'})
    assert response.status_code == 404
    with app.state.engine.connect() as c:
        assert c.execute(select(runs.c.id)).first() is None
