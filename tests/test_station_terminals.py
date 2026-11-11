"""Device authorization, six-way isolation and bounded scheduling regressions."""
import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update

from backend.app import create_app
from backend.db import digest, station_terminals, station_stream_leases, station_inspections, terminal_sessions
from backend.station_scheduler import StationScheduler


def register(client, number=1, **options):
    response = client.put(f'/api/v1/admin/terminals/station-{number}', json=dict(
        workstationId=f'pc-{number:02}', cameraId=f'cam-{number:02}', analysisType='pose', **options))
    assert response.status_code == 200, response.text
    return response.json()


def begin(client, credential, **body):
    payload = dict(body)
    payload.setdefault('requestId', str(uuid.uuid4()))
    response = client.post('/api/v1/station-streams', headers={'X-Terminal-Key': credential}, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def send(client, credential, stream, seq=0, stamp=0, **headers):
    return client.post(f'/api/v1/station-streams/{stream["streamId"]}/frames', content=b'fixture', headers={
        'X-Terminal-Key': credential, 'Content-Type': 'image/jpeg', 'X-Frame-Seq': str(seq),
        'X-Frame-Time': str(stamp), 'X-Stream-Epoch': str(stream['streamEpoch']), **headers})


def fake_model(app):
    def predict(payload, *, stream_key, frame_time):
        state = app.state.realtime.streams.get(stream_key, frame_time, ('fixture',))
        state['count'] = state.get('count', 0)+1
        return dict(people=[], count=state['count'], compliance=dict(status='PASS', label='fixture', reason='fixture'))
    app.state.realtime.predict = predict
    app.state.evidence.observe = lambda *args, **kwargs: None


@pytest.mark.parametrize('last_state,mode,expected', [
    ('PRESENT', 'visual-cumulative', 'PASS'),
    ('EMPTY', 'visual-cumulative', 'FAIL'),
    ('UNKNOWN', 'visual-cumulative', 'REVIEW'),
    ('PRESENT', 'temporal-actions', 'REVIEW'),
])
@pytest.mark.parametrize('kind',['screw','sticker'])
def test_visual_completion_with_sampling_gap_archives_and_releases(authenticated, last_state, mode, expected, kind):
    from algorithms.screws import ScrewObservationHistory, compliance
    from algorithms.stickers import create_sequence,compliance as sticker_compliance
    _, app, client = authenticated
    detector = getattr(app.state,kind+'_realtime')
    targets = [dict(id=f'S{i}', name=f'位置{i}', preassembled=i >= 9) for i in range(11)]
    if kind=='sticker':
        targets=[dict(id=f'T{i}',name=f'位置{i}',expected='triangle' if i<2 else 'rectangle') for i in range(8)]
    operations=len(targets)-(2 if kind=='screw' else 0)

    def snapshot(history,final=False):
        if kind=='screw':
            aggregate,sequence=history.aggregate(final=final),history.sequence(final=final)
            return aggregate,sequence,compliance(aggregate,sequence,final=final)
        aggregate,sequence=history.history.aggregate(final=final),history.snapshot()
        return aggregate,sequence,sticker_compliance(aggregate,sequence,final=final)

    def predict(payload, *, stream_key, frame_time):
        stream = detector.registry.get(stream_key, frame_time, ('completion-test',))
        if 'history' not in stream:
            stream['history'] = ScrewObservationHistory(targets) if kind=='screw' else create_sequence(
                dict(targets=targets,verification_mode='visual-cumulative'))
            stream['sampleCount'] = 0
        observations = [dict(t, state=('PRESENT' if i < 5 or frame_time >= 4 else 'UNKNOWN'), score=.95)
                        for i, t in enumerate(targets)]
        observations[-1]['state'] = last_state if frame_time >= 4 else 'UNKNOWN'
        stream['history'].update(frame_time, dict(registered=True, targets=observations))
        aggregate,sequence,verdict=snapshot(stream['history'])
        stream['sampleCount'] += 1
        sequence['verificationMode'] = mode
        return dict(targets=observations, aggregateTargets=aggregate, sequence=sequence, compliance=verdict)

    def finish(key):
        history = detector.registry.items[key]['history']
        targets,sequence,verdict=snapshot(history,final=True)
        sequence['verificationMode'] = mode
        return dict(final=True, targets=targets, sequence=sequence, compliance=verdict)

    detector.predict = predict
    detector.finish = finish
    app.state.evidence.observe = lambda *args, **kwargs: None
    response = client.put('/api/v1/admin/terminals/station-1', json=dict(
        workstationId='completion-pc', cameraId='completion-camera', analysisType=kind,
        sampleFps=4, maxSampleGapSeconds=1))
    assert response.status_code == 200, response.text
    credential = response.json()['credential']
    headers = {'X-Terminal-Key': credential}
    stream = begin(client, credential)
    for seq, stamp in enumerate((0, .25, .5, 4, 4.25, 4.5)):
        response = send(client, credential, stream, seq, stamp)
        assert response.status_code == 200, response.text
    live = response.json()
    assert live['evidenceGaps'] and live['stationEvidenceGaps']
    assert (live['compliance']['status'] == 'PASS') == (expected == 'PASS')
    if expected == 'PASS':
        assert live['sequence']['operationsCompleted'] == operations
        assert live['sequence']['preassembledConfirmed'] == (2 if kind=='screw' else 0)
        board = client.get('/api/v1/board/snapshot?mode=detection').json()
        assert board['summary']['passed'] == 1
    # Finalization delays must not erase fully confirmed position evidence.
    state = app.state.terminals.streams['station-1']
    detector.registry.items[state['key']]['lastAcceptedAt'] -= 5
    state['lastAt'] -= 5
    url = '/api/v1/station-streams/' + stream['streamId']
    final = client.post(url + '/finish', headers=headers)
    assert final.status_code == 200, final.text
    final = final.json()
    assert final['compliance']['status'] == expected
    assert final['archived'] and final['evidenceGaps'] and final['stationEvidenceGaps']
    assert client.post(url + '/finish', headers=headers).json() == final
    assert client.delete(url, headers=headers).status_code == 200
    assert state['key'] not in detector.registry.items
    assert state['finished'] and state['preview'] is None
    records = client.get('/api/v1/stations/station-1/inspections', headers=headers).json()['items']
    assert len(records) == 1 and records[0]['result']['compliance']['status'] == expected
    new_stream = begin(client, credential)
    assert new_stream['streamId'] != stream['streamId']
    first = send(client, credential, new_stream).json()
    assert first['compliance']['status'] != 'PASS'
    assert first['sequence']['operationsCompleted'] == 0
    assert not first['evidenceGaps'] and not first['stationEvidenceGaps']
    assert client.delete('/api/v1/station-streams/' + new_stream['streamId'], headers=headers).status_code == 200


def test_binding_credentials_permissions_and_rotation(authenticated):
    _, app, client = authenticated
    first = register(client)
    credential = first['credential']
    with app.state.engine.connect() as c:
        stored = dict(c.execute(select(station_terminals)).mappings().one())
    assert credential not in str(stored)
    assert credential not in client.get('/api/v1/admin/terminals').text
    key = {'X-Terminal-Key': credential}
    assert client.get('/api/v1/terminal/context', headers=key).json()['binding']['stationId'] == 'station-1'
    for path in ('/api/v1/admin/terminals', '/api/v1/admin/station-metrics', '/api/v1/analysis-runs',
                 '/api/v1/pilot/status?station=station-2', '/api/v1/stations/station-2/live-status'):
        assert client.get(path, headers=key).status_code == 403, path
    assert client.post('/api/v1/realtime/frame', headers={**key, 'Content-Type':'image/jpeg'}, content=b'x').status_code == 403
    assert client.post('/api/v1/station-streams?station=station-2', headers=key,
        json={'requestId':str(uuid.uuid4())}).status_code == 403
    assert client.put('/api/v1/admin/terminals/station-2', json={
        'workstationId':'pc-01','cameraId':'cam-02'}).status_code == 409
    unchanged = register(client, revision=1)
    assert unchanged['binding']['revision'] == 1 and unchanged['credential'] is None and not unchanged['changed']
    assert client.get('/api/v1/terminal/context', headers=key).status_code == 200
    response = client.post('/api/v1/admin/terminals/station-1/credential', json={'revision':1})
    assert response.status_code == 200, response.text
    second = response.json()
    assert second['binding']['revision'] == 2 and second['credential'] != credential
    assert client.get('/api/v1/terminal/context', headers=key).status_code == 401
    assert client.post('/api/v1/admin/terminals/station-1/credential', json={'revision':1}).status_code == 409


@pytest.mark.parametrize('sampling', [
    {'sampleFps':8}, {'maxSampleGapSeconds':.5}, {'sampleFps':8, 'maxSampleGapSeconds':.5},
])
def test_sampling_tuning_preserves_credentials_and_current_terminal_sessions(authenticated, sampling):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']
    terminal = TestClient(app)
    try:
        login = terminal.post('/api/v1/terminal/login', json={'credential':key})
        assert login.status_code == 200
        terminal.headers['X-CSRF-Token'] = login.json()['csrf']
        with app.state.engine.begin() as c:
            original = dict(c.execute(select(station_terminals)).mappings().one())
            session = dict(c.execute(select(terminal_sessions)).mappings().one())
            # A session invalidated before this edit must not become valid again.
            c.execute(terminal_sessions.insert().values(**dict(session,
                token_hash=digest('stale-session'), revision=0)))
        updated = register(client, revision=1, **sampling)
        assert updated['changed'] and updated['credential'] is None
        assert updated['binding']['revision'] == 2
        for field, value in sampling.items():
            assert updated['binding'][field] == value
        with app.state.engine.connect() as c:
            current = c.execute(select(station_terminals)).mappings().one()
            assert current['credential_hash'] == original['credential_hash']
            sessions = {row['token_hash']: dict(row) for row in c.execute(select(terminal_sessions)).mappings()}
        assert sessions[session['token_hash']] == dict(session, revision=2)
        assert sessions[digest('stale-session')]['revision'] == 0
        assert terminal.get('/api/v1/terminal/context').json()['binding'] == updated['binding']
        assert client.get('/api/v1/terminal/context', headers={'X-Terminal-Key':key}).json()['binding'] == updated['binding']
        assert not register(client, revision=2, **sampling)['changed']
        assert client.put('/api/v1/admin/terminals/station-1', json=dict(
            workstationId='pc-01', cameraId='cam-01', analysisType='pose', revision=1, **sampling)).status_code == 409
        stream = terminal.post('/api/v1/station-streams', json={'requestId':str(uuid.uuid4())})
        assert stream.status_code == 201, stream.text
        state = app.state.terminals.streams['station-1']
        assert state['revision'] == 2
        assert state['sampleFps'] == sampling.get('sampleFps', 4)
        assert state['maxSampleGapSeconds'] == sampling.get('maxSampleGapSeconds', 1)
        assert send(client, key, stream.json()).status_code == 200
        assert terminal.delete('/api/v1/station-streams/' + stream.json()['streamId']).status_code == 200
    finally:
        terminal.close()


@pytest.mark.parametrize('change', [
    {'workstationId':'pc-new'}, {'cameraId':'cam-new'}, {'source':'rtsp'},
    {'analysisType':'manual'}, {'previewUrl':'/media/new-camera'}, {'enabled':False},
])
def test_non_sampling_binding_changes_still_rotate_credentials_and_revoke_sessions(authenticated, change):
    _, app, client = authenticated
    key = register(client)['credential']
    terminal = TestClient(app)
    try:
        assert terminal.post('/api/v1/terminal/login', json={'credential':key}).status_code == 200
        body = dict(workstationId='pc-01', cameraId='cam-01', analysisType='pose', revision=1, sampleFps=8)
        body.update(change)
        response = client.put('/api/v1/admin/terminals/station-1', json=body)
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated['changed'] and updated['binding']['revision'] == 2
        assert updated['credential'] and updated['credential'] != key
        with app.state.engine.connect() as c:
            binding = c.execute(select(station_terminals)).mappings().one()
            assert binding['credential_hash'] == digest(updated['credential'])
            assert not c.execute(select(terminal_sessions)).first()
        assert client.get('/api/v1/terminal/context', headers={'X-Terminal-Key':key}).status_code == 401
        assert terminal.get('/api/v1/terminal/context').status_code == 401
    finally:
        terminal.close()


def test_terminal_cookie_csrf_and_fixed_page(authenticated):
    _, _, client = authenticated
    first = register(client, number=3)
    response = client.post('/api/v1/terminal/login', json={'credential':first['credential']})
    assert 'HttpOnly' in response.headers['set-cookie']
    assert client.post('/api/v1/station-streams', json={'requestId':str(uuid.uuid4())}).status_code == 403
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    assert client.get('/api/v1/session').json()['role'] == 'terminal'
    page = client.get('/?task=station6')
    assert 'window.AISOP_TERMINAL=' in page.text and '"stationId": "station-3"' in page.text
    assert client.get('/api/v1/pilot/status?station=station-6').status_code == 403
    assert client.post('/api/v1/terminal/logout').status_code == 200


def test_six_streams_keep_counts_and_identity_separate(authenticated):
    _, app, client = authenticated
    fake_model(app)
    streams=[]
    for n in range(1,7):
        credential=register(client,n)['credential']
        streams.append((credential,begin(client,credential)))
    for n,(credential,stream) in enumerate(streams,1):
        response=send(client,credential,stream)
        assert response.status_code==200,response.text
        data=response.json()
        assert data['stationId']==f'station-{n}' and data['cameraId']==f'cam-{n:02}' and data['count']==1
        assert data['frameSeq']==0 and data['streamId']==stream['streamId']
    credential,stream=streams[0]
    assert send(client,credential,stream,1,.25).json()['count']==2
    assert send(client,credential,stream,1,.25).status_code==409
    assert send(client,credential,stream,2,.5,**{'X-Stream-Epoch':'99'}).status_code==409
    assert send(client,credential,stream,2,.5,**{'X-Station-Id':'station-2'}).status_code==403
    assert send(client,streams[1][0],stream,2,.5).status_code==403
    state=client.get('/api/v1/stations/station-1/live-status',headers={'X-Terminal-Key':credential}).json()
    assert state['result']['frameSeq']==1 and state['effectiveFps']>0
    app.state.terminals.streams['station-1']['lastCaptureAt']-=2
    state=client.get('/api/v1/stations/station-1/live-status',headers={'X-Terminal-Key':credential}).json()
    assert state['state']=='STALE' and state['result'] is None


def test_batch_status_matches_individual_states_with_two_queries(authenticated):
    _, app, client = authenticated
    fake_model(app)
    for number in range(1, 7):
        key = register(client, number)['credential']
        stream = begin(client, key)
        assert send(client, key, stream).status_code == 200
    statements = []
    def query(*args): statements.append(args[2])
    event.listen(app.state.engine, 'before_cursor_execute', query)
    try:
        batch = app.state.terminals.statuses('admin')
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', query)
    assert len(statements) == 2 and len(batch) == 6
    for station, state in batch.items():
        single = app.state.terminals.status(station)
        assert state['state'] == single['state'] == 'LIVE'
        assert state['streamId'] == single['streamId']
        assert state['result'] == single['result']
    assert app.state.terminals.statuses('another-owner') == {}


def test_stream_retry_fencing_restart_and_finish(authenticated):
    settings,app,client=authenticated
    fake_model(app)
    key=register(client)['credential']; headers={'X-Terminal-Key':key}
    rid=str(uuid.uuid4())
    first=begin(client,key,requestId=rid)
    assert begin(client,key,requestId=rid)==first
    assert client.post('/api/v1/station-streams',headers=headers,json={'requestId':str(uuid.uuid4())}).status_code==409
    assert send(client,key,first).status_code==200
    assert send(client,key,first,1,2).json()['compliance']['status']=='REVIEW'
    url='/api/v1/station-streams/'+first['streamId']
    final=client.post(url+'/finish',headers=headers)
    assert final.status_code==200 and final.json()['final']
    assert client.post(url+'/finish',headers=headers).json()==final.json()
    assert send(client,key,first,2,2.5).status_code==409
    assert client.delete(url,headers=headers).status_code==200
    assert client.delete(url,headers=headers).status_code==200
    second=begin(client,key)
    assert second['streamEpoch']==first['streamEpoch']+1
    assert send(client,key,first,2,2.5).status_code==404
    restarted=create_app(settings)
    with TestClient(restarted) as other:
        assert other.post('/api/v1/station-streams',headers=headers,json={'requestId':str(uuid.uuid4())}).status_code==409
        with app.state.engine.begin() as c:c.execute(update(station_stream_leases).values(lease_until=0))
        third=begin(other,key)
        assert third['streamEpoch']==second['streamEpoch']+1
        assert send(client,key,second).status_code==409, 'old process must honor the SQL lease fence'


def test_busy_controls_and_queue_do_not_block_health(authenticated):
    _,app,client=authenticated
    fake_model(app);key=register(client)['credential'];stream=begin(client,key)
    entered,release=threading.Event(),threading.Event()
    original=app.state.realtime.predict
    def slow(*args,**kwargs):
        entered.set();assert release.wait(5);return original(*args,**kwargs)
    app.state.realtime.predict=slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        frame=pool.submit(send,client,key,stream)
        assert entered.wait(2)
        try:
            assert client.get('/api/v1/health').status_code==200
            assert client.delete('/api/v1/station-streams/'+stream['streamId'],headers={'X-Terminal-Key':key}).status_code==429
            response=client.put('/api/v1/admin/terminals/station-1',json={'workstationId':'pc-01','cameraId':'cam-01','revision':1})
            assert response.status_code==409
        finally:release.set()
        assert frame.result().status_code==200


def test_scheduler_replacement_fairness_and_cancellation():
    async def scenario():
        scheduler=StationScheduler(1000);gate=asyncio.Event();started=asyncio.Event();order=[]
        async def first(wait):started.set();await gate.wait();order.append('first');return 1
        active=asyncio.create_task(scheduler.submit('station-1',first));await started.wait()
        async def work(label,wait):order.append(label);return label
        old=asyncio.create_task(scheduler.submit('station-2',lambda w:work('old',w)))
        await asyncio.sleep(0)
        other=asyncio.create_task(scheduler.submit('station-3',lambda w:work('other',w)))
        new=asyncio.create_task(scheduler.submit('station-2',lambda w:work('new',w)))
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as error:await old
        assert error.value.status_code==409
        active.cancel()
        with pytest.raises(asyncio.CancelledError):await active
        assert scheduler.active=='station-1', 'HTTP cancellation cannot free running GPU work'
        gate.set();await asyncio.gather(other,new)
        assert order==['first','new','other']
        assert scheduler.snapshot()['stations']['station-2']['replaced']==1
        assert scheduler.snapshot()['queueDepth']==0
        await scheduler.close()
    asyncio.run(scenario())


def test_scheduler_expires_waiting_frames_without_running_them():
    async def scenario():
        scheduler=StationScheduler(10);gate=asyncio.Event();started=asyncio.Event()
        async def first(wait):started.set();await gate.wait()
        a=asyncio.create_task(scheduler.submit('station-1',first));await started.wait()
        async def unexpected(wait):pytest.fail('stale frame ran')
        b=asyncio.create_task(scheduler.submit('station-2',unexpected));await asyncio.sleep(.03);gate.set();await a
        with pytest.raises(HTTPException) as error:await b
        assert error.value.status_code==429
        assert scheduler.snapshot()['stations']['station-2']['expired']==1
        await scheduler.close()
    asyncio.run(scenario())


def test_scheduler_parallel_groups_keep_same_station_and_model_serial():
    async def scenario():
        scheduler = StationScheduler(1000, concurrency=3)
        gate = asyncio.Event()
        entered = set()
        async def blocked(label, wait):
            entered.add(label)
            await gate.wait()
            return label
        a = asyncio.create_task(scheduler.submit('station-1', lambda w:blocked('pose-a',w),group='pose'))
        b = asyncio.create_task(scheduler.submit('station-2', lambda w:blocked('pose-b',w),group='pose'))
        c = asyncio.create_task(scheduler.submit('station-3', lambda w:blocked('sticker',w),group='sticker'))
        for _ in range(20):
            if len(entered)==2: break
            await asyncio.sleep(.005)
        assert entered == {'pose-a','sticker'}
        assert scheduler.active == ['station-1','station-3']
        assert scheduler.busy('station-2')
        a.cancel()
        with pytest.raises(asyncio.CancelledError): await a
        assert 'station-1' in scheduler.active_stations
        gate.set()
        await asyncio.gather(b,c)
        await scheduler.close()
        assert entered == {'pose-a','pose-b','sticker'}
        assert scheduler.snapshot()['queueDepth'] == 0
    asyncio.run(scenario())


def test_terminal_pages_and_default_manual_guard(authenticated):
    _,app,client=authenticated
    for path in ('/terminal','/terminals','/terminal.js','/terminals.js','/terminal.css'):
        assert client.get(path).status_code==200,path
    r=client.put('/api/v1/admin/terminals/station-1',json={'workstationId':'pc-01','cameraId':'cam-01'})
    assert r.status_code==200
    assert client.post('/api/v1/station-streams',headers={'X-Terminal-Key':r.json()['credential']},
        json={'requestId':str(uuid.uuid4())}).status_code==409


def test_expired_lease_can_recover_without_old_stream_id(authenticated):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']
    first = begin(client, key)
    assert send(client, key, first).status_code == 200
    with app.state.engine.begin() as c:
        c.execute(update(station_stream_leases).values(lease_until=0))
    assert send(client, key, first, 1, .25).status_code == 409
    assert app.state.terminals.status('station-1')['state'] == 'OFFLINE'
    second = begin(client, key)
    assert second['streamEpoch'] == first['streamEpoch'] + 1
    assert send(client, key, second).json()['count'] == 1
    assert send(client, key, first, 1, .25).status_code == 404


def test_late_inference_cannot_publish_after_lease_takeover(authenticated):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']
    stream = begin(client, key)
    original = app.state.realtime.predict
    observed = []
    app.state.evidence.observe = lambda *a, **kw: observed.append(a)
    def taken_over(*args, **kwargs):
        result = original(*args, **kwargs)
        with app.state.engine.begin() as c:
            c.execute(update(station_stream_leases).values(stream_id=str(uuid.uuid4()), epoch=2))
        return result
    app.state.realtime.predict = taken_over
    assert send(client, key, stream).status_code == 409
    assert observed == []
    assert app.state.terminals.streams['station-1']['result'] is None
    assert app.state.terminals.status('station-1')['state'] == 'OFFLINE'
    assert client.post('/api/v1/station-streams/' + stream['streamId'] + '/finish',
        headers={'X-Terminal-Key':key}).status_code == 409


@pytest.mark.parametrize('changed_after_prediction', [False, True])
def test_model_revision_fences_before_and_after_prediction(authenticated, monkeypatch, changed_after_prediction):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']
    stream = begin(client, key)
    state = app.state.terminals.streams['station-1']
    calls, observed = [], []
    expected = state['modelRevision']
    state['revision_check'] = lambda: expected if changed_after_prediction and not calls else 'changed'
    original = app.state.realtime.predict
    def predict(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(app.state.realtime, 'predict', predict)
    monkeypatch.setattr(app.state.evidence, 'observe', lambda *a, **kw: observed.append(True))
    assert send(client, key, stream).status_code == 409
    assert bool(calls) is changed_after_prediction
    assert observed == [] and state['result'] is None
    assert app.state.realtime_transport.snapshot()['services']['pose']['active'] == 0


def test_precheck_delay_cannot_send_expired_frame_to_model(authenticated, monkeypatch):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']
    stream = begin(client, key)
    state = app.state.terminals.streams['station-1']
    expected = state['modelRevision']
    # Budget consumed by asset/SQL checks remains part of the input deadline.
    app.state.terminals.scheduler.max_wait_ms = 50
    def slow_revision():
        time.sleep(.07)
        return expected
    state['revision_check'] = slow_revision
    monkeypatch.setattr(app.state.realtime, 'predict', lambda *a, **kw: pytest.fail('expired sample inferred'))
    assert send(client, key, stream).status_code == 429
    assert state['result'] is None and state['lastAccepted'] is None
    assert app.state.realtime_transport.snapshot()['services']['pose']['active'] == 0


def test_service_timings_preserve_detector_timings(authenticated, monkeypatch):
    import time
    from types import SimpleNamespace
    import backend.terminals as terminals
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']
    stream = begin(client, key)
    # Windows monotonic can stay unchanged across a short request. Performance
    # spans must still include the model and SQL work measured by perf_counter.
    coarse = time.monotonic()
    monkeypatch.setattr(terminals, 'time', SimpleNamespace(
        monotonic=lambda: coarse, perf_counter=time.perf_counter))
    original = app.state.realtime.predict
    def predict(*args, **kwargs):
        time.sleep(.005)
        return dict(original(*args, **kwargs), timingsMs={'detectorStage': 7.5})
    monkeypatch.setattr(app.state.realtime, 'predict', predict)
    result = send(client, key, stream).json()
    assert result['timingsMs'] == {'detectorStage': 7.5}
    stages = result['serviceTimingsMs']
    assert set(stages) == {'beforeWorkerMs', 'precheckMs', 'modelMs', 'postcheckMs', 'resultRulesMs', 'evidenceMs'}
    assert all(value >= 0 for value in stages.values())
    assert sum(stages.values()) <= result['serverProcessingMs'] + .1
    assert result['serverProcessingMs'] >= 5


def test_six_simultaneous_requests_are_bounded_and_isolated(authenticated):
    _, app, client = authenticated
    fake_model(app)
    streams = [(r['credential'], begin(client, r['credential']))
        for r in [register(client, n) for n in range(1, 7)]]
    original = app.state.realtime.predict
    barrier = threading.Barrier(6)
    def slow(*args, **kwargs):
        time.sleep(.02)
        return original(*args, **kwargs)
    app.state.realtime.predict = slow
    def submit(pair):
        barrier.wait(timeout=5)
        return send(client, *pair)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, streams))
    assert all(response.status_code == 200 for response in responses)
    assert {r.json()['stationId'] for r in responses} == {f'station-{n}' for n in range(1, 7)}
    assert all(r.json()['count'] == 1 for r in responses)
    metrics = app.state.terminals.scheduler.snapshot()
    assert metrics['queueDepth'] == 0 and metrics['active'] is None
    assert all(s['completed'] == 1 for s in metrics['stations'].values())


@pytest.mark.parametrize('path', ['/media/../admin', '/media/%2e%2e/admin', '/media/cam?key=secret', '//outside/media/a'])
def test_preview_rejects_paths_outside_gateway(authenticated, path):
    _, _, client = authenticated
    response = client.put('/api/v1/admin/terminals/station-1', json={
        'workstationId':'pc-01', 'cameraId':'cam-01', 'previewUrl':path})
    assert response.status_code == 422


def test_missing_pose_model_returns_service_unavailable(authenticated):
    settings, _, client = authenticated
    key = register(client)['credential']
    settings.model_path.unlink()
    response = client.post('/api/v1/station-streams', headers={'X-Terminal-Key':key},
        json={'requestId':str(uuid.uuid4())})
    assert response.status_code == 503


def test_cookie_role_switch_and_expired_terminal_recovery(authenticated):
    settings, app, client = authenticated
    key = register(client)['credential']
    login = client.post('/api/v1/terminal/login', json={'credential':key})
    assert 'aisop_session' not in client.cookies
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    admin = client.post('/api/v1/login', json={'username':settings.username, 'password':settings.password})
    assert 'aisop_terminal' not in client.cookies
    client.headers['X-CSRF-Token'] = admin.json()['csrf']
    assert client.get('/api/v1/admin/terminals').status_code == 200
    client.post('/api/v1/terminal/login', json={'credential':key})
    with app.state.engine.begin() as c:
        c.execute(update(station_terminals).values(revision=2))
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/terminal'
    assert client.post('/api/v1/terminal/logout').status_code == 200


def test_sample_preview_matches_current_frame_and_station(authenticated):
    _, app, client = authenticated
    fake_model(app)
    key = register(client, source='rtsp')['credential']
    other = register(client, number=2)['credential']
    stream = begin(client, key)
    url = '/api/v1/stations/station-1/preview?stream_id=' + stream['streamId'] + '&frame_seq=0'
    headers = {'X-Terminal-Key':key}
    assert client.get(url, headers=headers).status_code == 404
    assert send(client, key, stream).status_code == 200
    response = client.get(url, headers=headers)
    assert response.status_code == 200 and response.content == b'fixture'
    assert response.headers['X-Camera-Id'] == 'cam-01'
    assert response.headers['X-Stream-Id'] == stream['streamId']
    assert 'no-store' in response.headers['Cache-Control']
    assert client.get(url, headers={'X-Terminal-Key':other}).status_code == 403
    assert send(client, key, stream, 1, .25).status_code == 200
    assert client.get(url, headers=headers).status_code == 409
    app.state.terminals.streams['station-1']['lastCaptureAt'] -= 2
    assert client.get(url.replace('frame_seq=0','frame_seq=1'), headers=headers).status_code == 404


def test_inspection_archive_survives_restart_and_remains_station_scoped(authenticated):
    settings, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']
    other_key = register(client, 2)['credential']
    headers = {'X-Terminal-Key': key}
    stream = begin(client, key)
    assert send(client, key, stream).status_code == 200
    url = '/api/v1/station-streams/' + stream['streamId']
    result = client.post(url + '/finish', headers=headers).json()
    assert result['archived'] and result['recordStatus'] == 'FINISHED'
    # Leave the lease open to simulate a lost release request followed by restart.
    restarted = create_app(settings)
    with TestClient(restarted) as other:
        assert other.post(url + '/finish', headers=headers).json() == result
        history = other.get('/api/v1/stations/station-1/inspections', headers=headers).json()
        assert len(history['items']) == 1 and history['items'][0]['result'] == result
        assert history['items'][0]['context']['modelRevision'] == stream['modelRevision']
        foreign = {'X-Terminal-Key': other_key}
        for path in ('/api/v1/stations/station-1/context', '/api/v1/stations/station-1/inspections'):
            assert other.get(path, headers=foreign).status_code == 403
        assert other.post(url + '/finish', headers=foreign).status_code == 403
        assert other.delete(url, headers=foreign).status_code == 403
        assert other.delete(url, headers=headers).status_code == 200
        assert begin(other, key)['streamEpoch'] == stream['streamEpoch'] + 1


def test_stop_reason_and_expired_lease_are_persisted_as_interrupted(authenticated):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']; headers = {'X-Terminal-Key':key}
    stream = begin(client, key)
    assert send(client, key, stream).status_code == 200
    url = '/api/v1/station-streams/' + stream['streamId']
    reason = '换班前相机断线，原工件需要复核'
    assert client.request('DELETE', url, headers=headers, json={'reason':reason}).status_code == 200
    assert client.post(url + '/finish', headers=headers).status_code == 409
    record = client.get('/api/v1/stations/station-1/inspections', headers=headers).json()['items'][0]
    assert record['status'] == 'INTERRUPTED' and record['reason'] == reason and record['result'] is None
    expired = begin(client, key)
    with app.state.engine.begin() as c:
        c.execute(update(station_stream_leases).values(lease_until=0))
    history = client.get('/api/v1/stations/station-1/inspections?limit=1', headers=headers).json()
    assert history['hasMore'] and history['items'][0]['streamId'] == expired['streamId']
    assert history['items'][0]['status'] == 'INTERRUPTED'
    page = client.get('/api/v1/stations/station-1/inspections?limit=1&offset=1', headers=headers).json()
    assert not page['hasMore'] and page['items'][0]['streamId'] == stream['streamId']
    assert client.post('/api/v1/station-streams/' + expired['streamId'] + '/finish', headers=headers).status_code == 409
    fresh = begin(client, key)
    assert fresh['streamEpoch'] == expired['streamEpoch'] + 1


def test_active_stream_blocks_configuration_and_rotation_but_not_noop(authenticated):
    _, app, client = authenticated
    key = register(client)['credential']
    stream = begin(client, key)
    assert register(client, revision=1)['credential'] is None
    assert client.put('/api/v1/admin/terminals/station-1', json={
        'workstationId':'pc-01','cameraId':'cam-01','analysisType':'pose','revision':1,'sampleFps':8}).status_code == 409
    assert client.post('/api/v1/admin/terminals/station-1/credential', json={'revision':1}).status_code == 409
    assert client.delete('/api/v1/station-streams/' + stream['streamId'], headers={'X-Terminal-Key':key}).status_code == 200
    assert app.state.terminals.streams['station-1']['finished']
    changed = register(client, revision=1, sampleFps=8)
    assert changed['binding']['revision'] == 2 and changed['credential'] is None and changed['changed']
    assert 'station-1' not in app.state.terminals.streams
    assert send(client, key, stream).status_code == 404
    new_stream = begin(client, key)
    assert new_stream['streamId'] != stream['streamId'] and new_stream['revision'] == 2
    assert app.state.terminals.streams['station-1']['sampleFps'] == 8


@pytest.mark.usefixtures('pilot_registration_enabled')
def test_detection_is_independent_of_manual_workpieces(authenticated):
    settings, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']; headers = {'X-Terminal-Key':key}
    register(client, 2)
    response = client.post('/api/v1/pilot/cycles', json={
        'request_id':str(uuid.uuid4()), 'station':'station-1', 'product':'测试装配件',
        'part_number':'PART-2026-001', 'source':'camera'})
    assert response.status_code == 201, response.text
    cycle = response.json()['cycle']
    stream = begin(client, key)
    assert 'cycleId' not in stream and 'product' not in stream
    assert send(client, key, stream).status_code == 200
    response = client.post('/api/v1/pilot/cycles/' + cycle['id'] + '/close', json={
        'request_id':str(uuid.uuid4()),'revision':cycle['revision'],'review_password':settings.password,
        'reason':'测试工件主动中断','outcome':'INTERRUPTED'})
    assert response.status_code == 200, response.text
    statements = []
    def capture_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(app.state.engine, 'before_cursor_execute', capture_sql)
    try:
        assert set(client.get('/api/v1/stations/station-1/context', headers=headers).json()) == {'binding'}
        assert send(client, key, stream, seq=1, stamp=.25).status_code == 200
        assert client.get('/api/v1/stations/station-1/live-status', headers=headers).json()['reasonCode'] == 'LIVE'
        assert client.get('/api/v1/board/snapshot?mode=detection').status_code == 200
        url = '/api/v1/station-streams/' + stream['streamId']
        final = client.post(url + '/finish', headers=headers)
        assert final.status_code == 200, final.text
        assert final.json()['archived'] and 'cycleId' not in final.json()
        assert client.delete(url, headers=headers).status_code == 200
        records = client.get('/api/v1/stations/station-1/inspections', headers=headers).json()['items']
        assert len(records) == 1 and records[0]['status'] == 'FINISHED'
        assert 'cycleId' not in records[0] and 'product' not in records[0]['context']
        assert not any('pilot_cycles' in sql for sql in statements)
        with app.state.engine.connect() as c:
            assert c.execute(select(station_inspections.c.cycle_id)).scalar_one() is None
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', capture_sql)


@pytest.mark.parametrize('cycle', [None, str(uuid.uuid4())])
def test_removed_workpiece_parameters_are_rejected(authenticated, cycle):
    _, _, client = authenticated
    key = register(client)['credential']; headers = {'X-Terminal-Key': key}
    response = client.post('/api/v1/station-streams', headers=headers,
        json=dict(requestId=str(uuid.uuid4()), cycleId=cycle))
    assert response.status_code == 422
    assert client.get('/api/v1/stations/station-1/inspections?cycle_id=' + str(cycle), headers=headers).status_code == 422
    assert client.get('/api/v1/stations/station-1/inspections', headers=headers).json()['items'] == []


def test_legacy_workpiece_archives_remain_readable_without_linkage(authenticated):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']; headers = {'X-Terminal-Key': key}
    stream = begin(client, key)
    assert send(client, key, stream).status_code == 200
    url = '/api/v1/station-streams/' + stream['streamId']
    result = client.post(url + '/finish', headers=headers).json()
    assert client.delete(url, headers=headers).status_code == 200
    legacy_id = str(uuid.uuid4())
    with app.state.engine.begin() as c:
        row = c.execute(select(station_inspections)).mappings().one()
        c.execute(update(station_inspections).values(cycle_id=legacy_id,
            context=dict(row['context'], cycleId=legacy_id, product={'part_number':'OLD-001'}),
            result=dict(result, cycleId=legacy_id, product={'part_number':'OLD-001'})))
    item = client.get('/api/v1/stations/station-1/inspections', headers=headers).json()['items'][0]
    for value in (item, item['context'], item['result'], client.post(url + '/finish', headers=headers).json()):
        assert 'cycleId' not in value and 'product' not in value
    assert item['result'] == result
    with app.state.engine.connect() as c:
        assert c.execute(select(station_inspections.c.cycle_id)).scalar_one() == legacy_id


def test_failed_archive_freezes_result_for_retry_and_hides_live_pass(authenticated, monkeypatch):
    _, app, client = authenticated
    fake_model(app)
    key = register(client)['credential']; headers = {'X-Terminal-Key':key}
    stream = begin(client, key)
    assert send(client, key, stream).status_code == 200
    service = app.state.terminals
    original = service.archive
    def unavailable(*args, **kwargs):
        raise HTTPException(503, '归档存储暂不可用')
    monkeypatch.setattr(service, 'archive', unavailable)
    url = '/api/v1/station-streams/' + stream['streamId']
    assert client.post(url + '/finish', headers=headers).status_code == 503
    assert send(client, key, stream, seq=1, stamp=.25).status_code == 409
    status = client.get('/api/v1/stations/station-1/live-status', headers=headers).json()
    assert status['reasonCode'] == 'FINALIZING' and status['result'] is None
    state = service.streams['station-1']
    frozen = dict(state['pendingFinal'])
    state['lastAt'] -= 20  # SQL retry delay is not a new camera sampling gap.
    monkeypatch.setattr(service, 'archive', original)
    result = client.post(url + '/finish', headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['stationEvidenceGaps'] == frozen['stationEvidenceGaps']
    assert result.json()['archived']
    assert client.post(url + '/finish', headers=headers).json() == result.json()


def test_scene_binding_requires_available_published_scene(authenticated, monkeypatch):
    _, app, client = authenticated
    def unavailable(*args, **kwargs):
        raise HTTPException(409, '检测任务尚未发布')
    monkeypatch.setattr(app.state.scenes, 'snapshot', unavailable)
    response = client.put('/api/v1/admin/terminals/station-1', json={
        'workstationId':'pc-01','cameraId':'cam-01','analysisType':'scene','sceneId':str(uuid.uuid4())})
    assert response.status_code == 409
    assert client.get('/api/v1/admin/terminals').json()['items'] == []
