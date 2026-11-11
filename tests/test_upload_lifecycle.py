"""Slow publication and disconnects must not stall the API or lose stored media."""
import asyncio
import threading
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from backend.db import media


@pytest.mark.parametrize('phase', ['insert', 'reconcile'])
def test_upload_database_wait_does_not_block_event_loop(authenticated, video_bytes, monkeypatch, phase):
    settings, app, session = authenticated
    engine = app.state.engine
    entered, release, ack_lost = threading.Event(), threading.Event(), threading.Event()
    sql_threads = []
    begin = engine.begin

    if phase == 'reconcile':
        @contextmanager
        def lost_ack():
            with begin() as connection:
                yield connection
            ack_lost.set()
            raise OperationalError('commit acknowledgement lost', {}, Exception())
        monkeypatch.setattr(engine, 'begin', lost_ack)

    def slow_sql(connection, cursor, statement, parameters, execution, many):
        sql = statement.lstrip().upper()
        if ((phase == 'insert' and sql.startswith('INSERT INTO MEDIA')) or
                (phase == 'reconcile' and ack_lost.is_set() and sql.startswith('SELECT MEDIA.ID'))):
            sql_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(5), 'SQL test was not released'

    async def probe():
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver',
                cookies=session.cookies, headers={'X-CSRF-Token': session.headers['X-CSRF-Token']}) as client:
            upload = asyncio.create_task(client.post('/api/v1/media?filename=sample.avi', content=video_bytes))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                assert (await asyncio.wait_for(client.get('/api/v1/live'), timeout=1)).status_code == 200
                assert not upload.done()
                assert sql_threads and loop_thread not in sql_threads
            finally:
                release.set()
                response = await upload
            assert response.status_code == 201, response.text
            return response.json()['id']

    event.listen(engine, 'before_cursor_execute', slow_sql)
    try:
        identity = asyncio.run(probe())
    finally:
        release.set()
        event.remove(engine, 'before_cursor_execute', slow_sql)
    assert (settings.data_dir / 'uploads' / (identity + '.avi')).read_bytes() == video_bytes
    with engine.connect() as connection:
        assert connection.scalar(select(media.c.id)) == identity


def test_cancelled_upload_does_not_delete_file_during_commit(authenticated, video_bytes, monkeypatch):
    settings, app, session = authenticated
    engine = app.state.engine
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    begin = engine.begin

    @contextmanager
    def observe_commit():
        with begin() as connection:
            yield connection
        committed.set()

    def slow_insert(connection, cursor, statement, parameters, execution, many):
        if statement.lstrip().upper().startswith('INSERT INTO MEDIA'):
            entered.set()
            assert release.wait(5), 'SQL test was not released'

    async def probe():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver',
                cookies=session.cookies, headers={'X-CSRF-Token': session.headers['X-CSRF-Token']}) as client:
            upload = asyncio.create_task(client.post('/api/v1/media?filename=sample.avi', content=video_bytes))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                upload.cancel()
                # ASGI middleware may drain the worker before propagating
                # cancellation. Release independently, while asserting the
                # published file survives with the INSERT still blocked.
                await asyncio.sleep(.05)
                published = list((settings.data_dir / 'uploads').glob('*.avi'))
                assert len(published) == 1 and published[0].read_bytes() == video_bytes
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await upload
            finally:
                release.set()
            assert await asyncio.to_thread(committed.wait, 3)

    monkeypatch.setattr(engine, 'begin', observe_commit)
    event.listen(engine, 'before_cursor_execute', slow_insert)
    try:
        asyncio.run(probe())
    finally:
        release.set()
        event.remove(engine, 'before_cursor_execute', slow_insert)
    with engine.connect() as connection:
        row = connection.execute(select(media)).mappings().one()
    assert (settings.data_dir / 'uploads' / (row['id'] + row['suffix'])).read_bytes() == video_bytes
    assert not list((settings.data_dir / 'uploads').glob('*.part'))
