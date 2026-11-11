"""Login concurrency, session recovery and trusted proxy regressions."""
import asyncio
import hashlib
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from backend.db import sessions
from backend.login_limits import LoginLimiter


def test_concurrent_failures_cannot_overwrite_the_limit(context, monkeypatch):
    settings, app, client = context
    bad = {'username': settings.username, 'password': 'wrong'}
    for _ in range(8):
        assert client.post('/api/v1/login', json=bad).status_code == 401
    original = hashlib.pbkdf2_hmac
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    active = 0

    def hold(*args, **kwargs):
        nonlocal active
        with lock:
            active += 1
            if active == 2:
                entered.set()
        assert release.wait(5), 'Hashing test was not released'
        return original(*args, **kwargs)

    monkeypatch.setattr(hashlib, 'pbkdf2_hmac', hold)
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(client.post, '/api/v1/login', json=bad) for _ in range(12)]
        try:
            assert entered.wait(5)
            # Requests beyond the two reservations are refused before hashing.
            for future in futures:
                if future.done():
                    assert future.result().status_code == 429
        finally:
            release.set()
        statuses = Counter(future.result().status_code for future in futures)
    assert statuses == {401: 2, 429: 10}
    response = client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password})
    assert response.status_code == 429
    assert 1 <= int(response.headers['retry-after']) <= 300
    assert response.json()['code'] == 'LOGIN_RATE_LIMITED'


def test_slow_login_transaction_does_not_block_other_requests(context):
    settings, app, _ = context
    entered, release = threading.Event(), threading.Event()
    sql_threads = []

    def hold_insert(conn, cursor, statement, parameters, execution, many):
        if statement.lstrip().upper().startswith('INSERT INTO SESSIONS'):
            sql_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(5), 'SQL test was not released'

    async def probe():
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            login = asyncio.create_task(client.post('/api/v1/login', json={
                'username': settings.username, 'password': settings.password}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                health = await asyncio.wait_for(client.get('/api/v1/live'), timeout=1)
                assert health.status_code == 200
                assert not login.done()
                assert sql_threads and loop_thread not in sql_threads
            finally:
                release.set()
                assert (await login).status_code == 200

    event.listen(app.state.engine, 'before_cursor_execute', hold_insert)
    try:
        asyncio.run(probe())
    finally:
        release.set()
        event.remove(app.state.engine, 'before_cursor_execute', hold_insert)


def test_login_cleanup_is_bounded(context):
    settings, app, client = context
    with app.state.engine.begin() as conn:
        conn.execute(sessions.insert(), [dict(token_hash=f'expired-{i}', owner='admin', csrf='x', expires_at=0)
                                         for i in range(150)])
    assert client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password}).status_code == 200
    with app.state.engine.connect() as conn:
        assert len(conn.execute(select(sessions).where(sessions.c.expires_at == 0)).all()) == 50


def test_stale_csrf_has_distinct_error_and_expired_logout_is_idempotent(authenticated):
    settings, app, client = authenticated
    old_csrf = client.headers['X-CSRF-Token']
    new = client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password})
    response = client.post('/api/v1/logout', headers={'X-CSRF-Token': old_csrf})
    assert response.status_code == 403 and response.json()['code'] == 'SESSION_CSRF_INVALID'
    assert client.get('/api/v1/session').json()['csrf'] == new.json()['csrf']
    with app.state.engine.begin() as conn:
        conn.execute(update(sessions).values(expires_at=0))
    assert client.post('/api/v1/logout').status_code == 200
    assert 'aisop_session' not in client.cookies
    assert client.post('/api/v1/logout').status_code == 200


def test_limiter_preserves_other_inflight_reservations_and_expires_idle_peers():
    clock = [1000]
    limiter = LoginLimiter(limit=2, window=10, per_peer=2, capacity=2, max_peers=2, clock=lambda: clock[0])
    with limiter.attempt('a') as first:
        with limiter.attempt('a') as second:
            with pytest.raises(HTTPException) as blocked:
                with limiter.attempt('b'):
                    pytest.fail('Global capacity must be enforced')
            assert blocked.value.status_code == 429
            second.success = False
        first.success = False
    with pytest.raises(HTTPException) as blocked:
        with limiter.attempt('a'):
            pytest.fail('Both failures must be retained')
    assert blocked.value.headers['Retry-After'] == '10'
    clock[0] += 11
    with limiter.attempt('c') as attempt:
        attempt.success = True
    with limiter.attempt('a') as attempt:
        attempt.success = True
    assert not limiter._peers and limiter._active == 0


def test_invalid_terminal_cookie_cannot_skip_admin_logout_csrf(authenticated):
    _, _, client = authenticated
    client.cookies.set('aisop_terminal', 'invalid')
    csrf = client.headers.pop('X-CSRF-Token')
    response = client.post('/api/v1/logout')
    assert response.status_code == 403 and response.json()['code'] == 'SESSION_CSRF_INVALID'
    client.cookies.delete('aisop_terminal')
    assert client.get('/api/v1/session').status_code == 200
    assert client.post('/api/v1/logout', headers={'X-CSRF-Token': csrf}).status_code == 200


def test_limiter_releases_capacity_on_internal_error():
    limiter = LoginLimiter(capacity=1, max_peers=1)
    with pytest.raises(RuntimeError):
        with limiter.attempt('a'):
            raise RuntimeError('Hash service failed')
    with limiter.attempt('b') as attempt:
        attempt.success = True
    assert not limiter._peers and limiter._active == 0


@pytest.mark.parametrize('peer,forwarded,expected', [
    ('172.30.240.10', '192.0.2.1', '192.0.2.1'),
    ('172.30.240.10', '192.0.2.2', '192.0.2.2'),
    ('192.0.2.99', '192.0.2.1', '192.0.2.99'),
])
def test_only_designated_proxy_can_set_client_address(peer, forwarded, expected):
    captured = []

    async def downstream(scope, receive, send):
        captured.append(scope['client'][0])

    middleware = ProxyHeadersMiddleware(downstream, trusted_hosts=['172.30.240.10'])
    asyncio.run(middleware({'type': 'http', 'client': (peer, 1234),
        'headers': [(b'x-forwarded-for', forwarded.encode())]}, None, None))
    assert captured == [expected]
