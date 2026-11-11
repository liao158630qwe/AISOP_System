"""Manual decisions preserve algorithm evidence and drive archived board totals."""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from backend.db import station_inspections


def archive(app, *, status='FINISHED', verdict='REVIEW', kind='sticker', station='station-2', owner='admin'):
    identity = str(uuid.uuid4())
    result = dict(streamId=identity, stationId=station, analysisType=kind, archived=True,
        compliance=dict(status=verdict, label='动作证据不足', reason='未观察到三个位置的动作'),
        targets=[dict(id='R01', name='左上方形贴', state='NOT_OBSERVED')],
        sequence=dict(events=[], status='PENDING'))
    with app.state.engine.begin() as c:
        c.execute(station_inspections.insert().values(stream_id=identity, station_id=station, owner=owner,
            context=dict(analysisType=kind, inputSource='camera'), status=status, result=result,
            reason='算法结果归档', started_at=time.time()-10, finished_at=time.time()))
    return identity, result


@pytest.mark.parametrize('outcome', ['PASS', 'FAIL'])
@pytest.mark.parametrize('reason', [None, '', '   ', '现场复核已完成'])
def test_review_reason_optional_original_evidence_retained_and_board_updated(authenticated, outcome, reason):
    _, app, client = authenticated
    identity, original = archive(app)
    body = dict(requestId=str(uuid.uuid4()), outcome=outcome)
    if reason is not None:
        body['reason'] = reason
    url = f'/api/v1/station-streams/{identity}/manual-review'
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert {k:v for k,v in result.items() if k != 'manualReview'} == original
    review = result['manualReview']
    assert review['source'] == 'manual' and review['status'] == outcome
    assert review['reason'] == (reason or '').strip()
    assert review['reviewer'] == review['account'] == 'admin'
    assert review['reviewedAt'] > 0
    assert client.post(url, json=body).json() == result
    assert client.post(url, json={**body, 'outcome':'FAIL' if outcome=='PASS' else 'PASS'}).status_code == 409
    assert client.post(url, json={**body, 'requestId':str(uuid.uuid4())}).status_code == 409
    saved = client.get('/api/v1/stations/station-2/inspections').json()['items'][0]
    assert saved['result'] == result
    assert client.post(f'/api/v1/station-streams/{identity}/finish').json() == result
    board = client.get('/api/v1/board/snapshot?mode=detection').json()
    assert board['summary']['passed'] == (1 if outcome=='PASS' else 0)
    assert board['summary']['failed'] == (1 if outcome=='FAIL' else 0)
    assert board['summary']['pending'] == 0
    station = next(s for s in board['stations'] if s['id']=='station-2')
    assert station['detection']['reviewed'] and station['detection']['label'] == review['label']
    if outcome == 'FAIL':
        assert board['alerts'][0]['source'] == '人工复核'
    else:
        assert board['alertCount'] == 0


@pytest.mark.parametrize('options', [dict(status='RUNNING'), dict(status='INTERRUPTED'),
    dict(kind='pose'), dict(verdict='PASS')])
def test_review_rejects_ineligible_rounds(authenticated, options):
    _, app, client = authenticated
    identity, original = archive(app, **options)
    response = client.post(f'/api/v1/station-streams/{identity}/manual-review',
        json=dict(requestId=str(uuid.uuid4()), outcome='PASS'))
    assert response.status_code == 409
    with app.state.engine.connect() as c:
        assert c.scalar(select(station_inspections.c.result).where(station_inspections.c.stream_id==identity)) == original


def test_review_identity_permissions_and_input_validation(authenticated):
    _, app, client = authenticated
    identity, _ = archive(app)
    url = f'/api/v1/station-streams/{identity}/manual-review'
    body = dict(requestId=str(uuid.uuid4()), outcome='PASS')
    for patch in [dict(outcome='UNKNOWN'), dict(reason='x'*1001), dict(reviewer='someone else')]:
        assert client.post(url, json={**body, **patch}).status_code == 422
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.post(url, json=body).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    private, _ = archive(app, owner='private')
    assert client.post(f'/api/v1/station-streams/{private}/manual-review', json=body).status_code == 404
    for number in (1, 2):
        binding = client.put(f'/api/v1/admin/terminals/station-{number}', json=dict(
            workstationId=f'pc-{number}', cameraId=f'cam-{number}', analysisType='manual')).json()
        response = client.post(url, json=body, headers={'X-Terminal-Key':binding['credential']})
        assert response.status_code == (403 if number==1 else 200)
    assert response.json()['manualReview']['reviewer'] == 'station-2'
    assert response.json()['manualReview']['role'] == 'terminal'


def test_concurrent_review_cannot_overwrite_another_confirmation(authenticated):
    _, app, client = authenticated
    identity, original = archive(app, verdict='FAIL')
    url = f'/api/v1/station-streams/{identity}/manual-review'
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(client.post, url, json=dict(requestId=str(uuid.uuid4()), outcome=outcome))
                    for outcome in ('PASS', 'FAIL')]
        responses = [future.result() for future in requests]
    assert sorted(r.status_code for r in responses) == [200, 409]
    with app.state.engine.connect() as c:
        saved = c.scalar(select(station_inspections.c.result).where(station_inspections.c.stream_id==identity))
    assert saved['compliance'] == original['compliance']
    assert saved == next(r.json() for r in responses if r.status_code==200)


@pytest.mark.parametrize('options,allowed', [({}, True), (dict(status='RUNNING'), False),
    (dict(status='INTERRUPTED'), False), (dict(kind='pose'), False), (dict(verdict='PASS'), False)])
def test_exact_record_reconciliation_reports_eligibility(authenticated, options, allowed):
    _, app, client = authenticated
    identity, original = archive(app, **options)
    response = client.get(f'/api/v1/station-streams/{identity}/inspection')
    assert response.status_code == 200
    record = response.json()
    assert record['streamId'] == identity and record['result'] == original
    assert record['reviewAllowed'] == allowed
    assert record['reviewActor'] == dict(account='admin', reviewer='admin', role='admin', credentialRevision=None)
    private, _ = archive(app, owner='someone-else')
    assert client.get(f'/api/v1/station-streams/{private}/inspection').status_code == 404


def test_pending_identity_and_idempotent_receipt_cannot_switch_to_another_actor(authenticated):
    _, app, client = authenticated
    identity, _ = archive(app)
    url = f'/api/v1/station-streams/{identity}'
    admin = client.get(url + '/inspection').json()['reviewActor']
    credential = client.put('/api/v1/admin/terminals/station-2', json=dict(
        workstationId='pc-2', cameraId='cam-2', analysisType='manual')).json()['credential']
    headers = {'X-Terminal-Key':credential}
    body = dict(requestId=str(uuid.uuid4()), outcome='PASS', reason='原请求', expectedActor=admin)
    assert client.post(url + '/manual-review', json=body, headers=headers).status_code == 409
    terminal = client.get(url + '/inspection', headers=headers).json()['reviewActor']
    assert terminal['reviewer'] == 'station-2'
    assert terminal['credentialRevision'] == 1
    body['expectedActor'] = terminal
    result = client.post(url + '/manual-review', json=body, headers=headers).json()
    assert client.post(url + '/manual-review', json=body, headers=headers).json() == result
    # Even legacy callers without expectedActor cannot claim another actor's receipt.
    body.pop('expectedActor')
    conflict = client.post(url + '/manual-review', json=body)
    assert conflict.status_code == 409
    assert conflict.json()['record']['result'] == result
    assert not client.get(url + '/inspection').json()['reviewAllowed']


def test_credential_rotation_stops_pending_review(authenticated):
    _, app, client = authenticated
    identity, original = archive(app)
    url = f'/api/v1/station-streams/{identity}'
    binding = client.put('/api/v1/admin/terminals/station-2', json=dict(
        workstationId='pc-2', cameraId='cam-2', analysisType='manual')).json()
    headers = {'X-Terminal-Key':binding['credential']}
    actor = client.get(url + '/inspection', headers=headers).json()['reviewActor']
    updated = client.post('/api/v1/admin/terminals/station-2/credential', json=dict(revision=1)).json()
    body = dict(requestId=str(uuid.uuid4()), outcome='FAIL', expectedActor=actor)
    assert client.post(url + '/manual-review', json=body, headers=headers).status_code == 401
    conflict = client.post(url + '/manual-review', json=body, headers={'X-Terminal-Key':updated['credential']})
    assert conflict.status_code == 409
    assert conflict.json()['code'] == 'REVIEW_ACTOR_CHANGED'
    assert conflict.json()['record']['result'] == original


def test_saved_receipt_survives_service_restart(authenticated):
    from fastapi.testclient import TestClient
    from backend.app import create_app

    settings, app, client = authenticated
    identity, _ = archive(app)
    url = f'/api/v1/station-streams/{identity}'
    body = dict(requestId=str(uuid.uuid4()), outcome='PASS', reason='响应丢失测试',
        expectedActor=client.get(url + '/inspection').json()['reviewActor'])
    saved = client.post(url + '/manual-review', json=body).json()
    restarted = create_app(settings)
    try:
        with TestClient(restarted) as other:
            session = other.post('/api/v1/login', json=dict(username=settings.username, password=settings.password)).json()
            other.headers['X-CSRF-Token'] = session['csrf']
            assert other.get(url + '/inspection').json()['result'] == saved
            assert other.post(url + '/manual-review', json=body).json() == saved
    finally:
        restarted.state.engine.dispose()
