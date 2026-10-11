"""Regression coverage for paginated history and additive DB optimization."""
from sqlalchemy import inspect, select, text

from backend.db import initialize, media, query_indexes, runs


def seed_history(app):
    with app.state.engine.begin() as connection:
        connection.execute(media.insert(), [dict(
            id=f"media-{i}", owner=owner, name=name, suffix=".mp4", size=1,
            sha256="fixture", metadata={}, created_at=1,
        ) for i, (owner, name) in enumerate([
            ("admin", "灯具_100%.mp4"), ("admin", "灯具普通.mp4"), ("other", "private.mp4")
        ])])
        connection.execute(runs.insert(), [dict(
            id=f"run-{i:04}", owner="other" if i == 121 else "admin",
            media_id=f"media-{2 if i == 121 else i % 2}", status="FAILED" if i % 2 else "QUEUED",
            request_key=f"key-{i}", request_hash="fixture", options={}, model_hash="fixture",
            progress=0, attempts=0, token=0, created_at=100, updated_at=100,
        ) for i in range(122)])


def test_history_pagination_names_filters_and_ownership(authenticated):
    _, app, client = authenticated
    seed_history(app)
    first = client.get('/api/v1/analysis-runs?limit=20').json()
    second = client.get('/api/v1/analysis-runs?limit=20&offset=20').json()
    assert first['total'] == 121
    assert first['items'][0]['id'] == 'run-0120'
    assert first['items'][0]['media_name'] == '灯具_100%.mp4'
    assert len(first['items']) == len(second['items']) == 20
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}
    assert len(client.get('/api/v1/analysis-runs?offset=120').json()['items']) == 1
    assert client.get('/api/v1/analysis-runs?q=private').json()['total'] == 0
    assert client.get('/api/v1/analysis-runs?q=run-0000').json()['total'] == 1
    filtered = client.get('/api/v1/analysis-runs?status=FAILED').json()
    assert filtered['total'] == 60 and all(r['status'] == 'FAILED' for r in filtered['items'])
    # Search wildcard characters literally, rather than matching every file.
    assert client.get('/api/v1/analysis-runs', params={'q': '_100%'}).json()['total'] == 61
    for query in ['limit=0', 'limit=101', 'offset=-1', 'status=INVALID']:
        response = client.get('/api/v1/analysis-runs?' + query)
        assert response.status_code == 422 and '请求参数无效' in response.json()['message']


def test_additive_indexes_upgrade_preserves_existing_data(authenticated):
    _, app, _ = authenticated
    seed_history(app)
    for index in query_indexes:
        index.drop(app.state.engine)
    initialize(app.state.engine)
    initialize(app.state.engine)
    names = {item['name'] for item in inspect(app.state.engine).get_indexes('analysis_runs')}
    assert {'ix_runs_owner_created', 'ix_runs_status_created'} <= names
    with app.state.engine.connect() as connection:
        assert len(connection.execute(select(runs.c.id)).all()) == 122
        assert connection.execute(text('PRAGMA journal_mode')).scalar_one() == 'wal'


def test_validation_does_not_echo_password_and_limits_match(context):
    settings, _, client = context
    secret = 'never-echo-this-password-' * 20
    response = client.post('/api/v1/login', json={'username': 'admin', 'password': secret})
    assert response.status_code == 422
    assert secret not in response.text and 'password' in response.json()['message']
    assert client.get('/api/v1/health').json()['limits'] == {
        'max_upload_bytes': settings.max_upload_bytes, 'max_video_seconds': settings.max_video_seconds,
    }
    html = client.get('/').text
    assert 'window.AISOP_SERVER=true' in html and 'src="assets/reference.mp4"' not in html


def test_cursor_pagination_long_poll_and_metrics(authenticated):
    import uuid
    _, app, client = authenticated
    seed_history(app)
    first = client.get('/api/v1/analysis-runs?limit=20').json()
    assert first['next_cursor']
    second = client.get('/api/v1/analysis-runs?limit=20&cursor=' + first['next_cursor']).json()
    assert second['total'] is None
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}
    run_id = str(uuid.uuid4())
    with app.state.engine.begin() as connection:
        connection.execute(runs.insert().values(id=run_id, owner='admin', media_id='media-0', status='FAILED',
            request_key='wait-test', request_hash='fixture', options={}, model_hash='fixture', progress=0,
            attempts=1, token=1, created_at=100, updated_at=100, error_code='TEST', error_message='failed'))
    waited = client.get('/api/v1/analysis-runs/' + run_id + '/wait?since=0&timeout=1')
    assert waited.status_code == 200 and waited.json()['status'] == 'FAILED'
    metrics = client.get('/api/v1/metrics')
    assert metrics.status_code == 200
    assert {'queueWaitP95Seconds', 'inferenceP95Ms', 'realtime'} <= metrics.json().keys()
