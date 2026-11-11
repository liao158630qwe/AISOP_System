"""Real command/stream API + production collector state machine, synthetic model.

Only camera frames and model predictions are fixtures. SQL, authorization,
command reconciliation, release, and the collector HTTP decisions are real.
"""
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url

from backend.app import create_app
from backend.db import initialize, station_inspections, station_collectors


@pytest.fixture(params=['sqlite', 'postgresql'])
def collection_api(request, authenticated):
    if request.param == 'sqlite':
        yield authenticated
        return
    url = os.getenv('TEST_POSTGRES_URL')
    if not url:
        pytest.skip('PostgreSQL service not configured')
    admin = create_engine(url)
    schema = 'test_' + uuid.uuid4().hex
    app = None
    with admin.begin() as c:
        c.exec_driver_sql(f'CREATE SCHEMA {schema}')
    try:
        isolated = make_url(url).update_query_dict({'options': f'-csearch_path={schema} -clock_timeout=5000'})
        settings = replace(authenticated[0], database_url=isolated.render_as_string(hide_password=False))
        app = create_app(settings)
        initialize(app.state.engine)
        with TestClient(app) as client:
            login = client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password})
            assert login.status_code == 200
            client.headers['X-CSRF-Token'] = login.json()['csrf']
            yield settings, app, client
    finally:
        if app:
            app.state.engine.dispose()
        with admin.begin() as c:
            c.exec_driver_sql(f'DROP SCHEMA {schema} CASCADE')
        admin.dispose()


@pytest.fixture
def controlled(collection_api):
    _, app, client = collection_api
    path = Path(__file__).resolve().parents[1] / 'tools' / 'station-collector.py'
    spec = importlib.util.spec_from_file_location('integration_collector', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='controlled-pc', cameraId='controlled-camera',
        analysisType='pose', sampleFps=4, maxSampleGapSeconds=1))
    assert response.status_code == 200, response.text
    registered = response.json()
    binding = registered['binding']

    def predict(payload, *, stream_key, frame_time):
        state = app.state.realtime.streams.get(stream_key, frame_time, ('synthetic',))
        state['samples'] = state.get('samples', 0) + 1
        return dict(people=[], samples=state['samples'],
                    compliance=dict(status='REVIEW', label='合成观察', reason='测试样例'))
    app.state.realtime.predict = predict
    app.state.evidence.observe = lambda *args, **kwargs: None

    class Transport:
        lose = None

        def __init__(self):
            self.calls = []

        def request(self, path, method='GET', body=None, headers=None):
            request_headers = {'X-Terminal-Key': registered['credential'], **(headers or {})}
            options = {'content': body} if isinstance(body, bytes) else {'json': body} if body is not None else {}
            response = client.request(method, '/api/v1' + path, headers=request_headers, **options)
            self.calls.append((path, method, response.status_code))
            data = response.json()
            if response.status_code >= 400:
                raise module.ApiError(response.status_code, str(data.get('message') or data.get('detail') or data))
            kind = ('start' if path == '/station-streams' else 'finish' if path.endswith('/finish') else
                    'ack' if path.endswith('/ack') else 'delete' if method == 'DELETE' else None)
            if kind and self.lose == kind:
                self.lose = None
                raise module.ApiError(0, 'Injected lost response after the real database commit')
            return data

    clock = [10.]
    transport = Transport()
    worker = module.ControlledCollector(transport, binding, clock=lambda: clock[0])

    def tick(*, frame=True, generation=1):
        clock[0] += .6
        item = dict(payload=b'fixture-frame', at=clock[0], generation=generation) if frame else None
        worker.step(item)

    def command(action, stream=None, request_id=None):
        body = dict(requestId=request_id or str(uuid.uuid4()), action=action,
                    streamId=stream, reason='集成测试的逐件操作')
        response = client.post('/api/v1/stations/station-1/collector/commands', json=body)
        assert response.status_code == 200, response.text
        return body, response.json()

    def records():
        with app.state.engine.connect() as c:
            return [dict(row) for row in c.execute(select(station_inspections)).mappings()]

    tick()
    return app, client, worker, transport, tick, command, records


def start_piece(controlled):
    _, _, worker, _, tick, command, _ = controlled
    body, _ = command('start')
    tick()
    assert worker.stream and worker.command is None, worker.error
    identity = dict(worker.stream)
    tick()
    assert worker.seq == 1 and not worker.error, worker.error
    return body, identity


def test_real_commands_keep_round_across_page_reads_then_archive_and_next_piece(controlled):
    app, client, worker, _, tick, command, records = controlled
    assert not records(), 'An idle collector never opens a workpiece'
    start, first = start_piece(controlled)
    for _ in range(3):
        status = client.get('/api/v1/stations/station-1/collector').json()
        assert status['stream']['streamId'] == first['streamId']
        tick()
    assert len(records()) == 1 and worker.seq == 4
    finish, _ = command('finish', first['streamId'])
    tick()
    assert worker.stream is None and worker.command is None, worker.error
    assert records()[0]['status'] == 'FINISHED'
    assert records()[0]['result']['archived'] is True
    original = client.get('/api/v1/stations/station-1/collector/commands/' + start['requestId']).json()
    assert original['status'] == 'SUCCEEDED' and original['assignedStreamId'] == first['streamId']
    repeated = client.post('/api/v1/stations/station-1/collector/commands', json=finish).json()
    assert repeated['command']['status'] == 'SUCCEEDED'
    _, second = start_piece(controlled)
    assert second['streamId'] != first['streamId'] and second['streamEpoch'] > first['streamEpoch']
    assert app.state.terminals.streams['station-1']['result']['samples'] == 1
    command('stop', second['streamId'])
    tick()
    assert [row['status'] for row in records()] == ['FINISHED', 'INTERRUPTED']


@pytest.mark.parametrize('loss', ['start', 'finish', 'delete', 'ack'])
def test_committed_response_loss_is_reconciled_without_duplicate_piece_or_finish(controlled, loss):
    _, client, worker, transport, tick, command, records = controlled
    if loss == 'start':
        transport.lose = loss
    _, first = (None, None) if loss == 'start' else start_piece(controlled)
    if loss == 'start':
        command('start')
        tick()
        assert len(records()) == 1
        tick()
        first = dict(worker.stream)
        tick()
    transport.lose = loss if loss != 'start' else None
    command('finish', first['streamId'])
    for _ in range(4):
        tick()
    assert worker.stream is None and worker.command is None, worker.error
    assert len(records()) == 1 and records()[0]['status'] == 'FINISHED'
    assert client.get('/api/v1/stations/station-1/collector').json()['command']['status'] == 'SUCCEEDED'
    assert sum(path.endswith('/finish') and code == 200 for path, _, code in transport.calls) == 1
    assert sum(path == '/station-streams' and code == 201 for path, _, code in transport.calls) == 1


def test_api_restart_and_camera_reconnect_cannot_silently_reopen_piece(controlled):
    app, client, worker, _, tick, command, records = controlled
    _, first = start_piece(controlled)
    app.state.terminals.runtime_id = str(uuid.uuid4())
    tick()
    tick()
    assert worker.stream is None and records()[0]['status'] == 'INTERRUPTED'
    assert len(records()) == 1
    _, second = start_piece(controlled)
    assert second['streamId'] != first['streamId']
    tick(generation=2)
    tick(generation=2)
    assert worker.stream is None and records()[-1]['status'] == 'INTERRUPTED'
    assert len(records()) == 2
    assert client.get('/api/v1/stations/station-1/collector').json()['stream'] is None


def test_collector_lost_lease_cannot_resume_a_running_piece(controlled):
    app, _, worker, _, tick, _, records = controlled
    start_piece(controlled)
    with app.state.engine.begin() as c:
        c.execute(update(station_collectors).values(expires_at=0))
    tick()
    tick()
    assert worker.stream is None and records()[0]['status'] == 'INTERRUPTED'
    assert len(records()) == 1


def test_parallel_tabs_cannot_create_two_start_commands(controlled):
    _, client, worker, _, tick, _, records = controlled
    def submit(_):
        return client.post('/api/v1/stations/station-1/collector/commands', json=dict(
            requestId=str(uuid.uuid4()), action='start', streamId=None, reason='并发开始'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sorted(result.status_code for result in results) == [200, 409]
    tick()
    assert worker.stream and len(records()) == 1
