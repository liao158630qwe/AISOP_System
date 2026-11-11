"""Isolated real API + browser validation for alert archive playback.

Synthetic verdicts and frames; real SQL, MP4 encoding, auth, retry and HTTP.
Only subprocesses started here and data under test-results are changed.
"""
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
import requests
from fastapi.testclient import TestClient
from sqlalchemy import update
from backend.app import create_app
from backend.config import Settings
from backend.db import alert_evidence, initialize


def factory():
    settings = Settings.from_env()
    app = create_app(settings)
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            app.state.evidence.start()
            yield
    app.router.lifespan_context = lifespan
    return app


def fixture(settings):
    app = create_app(settings)
    initialize(app.state.engine)
    store = app.state.evidence
    verdict = {'compliance': {'status': 'FAIL', 'label': '异常', 'reason': '浏览器验收：自动保存录像'}}
    def record(stream, reason):
        result = None
        for i in range(8):
            frame = np.full((180, 320, 3), (i*30, 80, 150), np.uint8)
            cv2.putText(frame, 'sample '+str(i), (30, 90), cv2.FONT_HERSHEY_SIMPLEX, .8, (240,240,240), 2)
            ok, encoded = cv2.imencode('.jpg', frame)
            assert ok
            result = store.observe(settings.username, 'browser-test', stream, 'screw', i*.25,
                encoded.tobytes(), {'stationId':'station-2', 'compliance': {'status': 'FAIL', 'reason': reason}} if i==7 else {})
        return result['id']
    # Exercise the same station finish path as the dashboard. No intermediate
    # FAIL occurs; the final assessment must persist the buffered samples.
    detector = app.state.sticker_realtime
    def predict(payload, *, stream_key, frame_time):
        detector.registry.get(stream_key, frame_time, ('browser-evidence-test',))
        return {'compliance': {'status': 'REVIEW', 'reason': '检测进行中'}}
    detector.predict = predict
    detector.finish = lambda key: dict(verdict, final=True)
    with TestClient(app) as client:
        login = client.post('/api/v1/login', json=dict(username=settings.username, password=settings.password))
        assert login.status_code == 200
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        binding = client.put('/api/v1/admin/terminals/station-2', json=dict(
            workstationId='evidence-browser-pc', cameraId='evidence-browser-camera',
            analysisType='sticker', sampleFps=4, maxSampleGapSeconds=3))
        assert binding.status_code == 200, binding.text
        headers = {'X-Terminal-Key': binding.json()['credential']}
        response = client.post('/api/v1/station-streams', headers=headers,
                               json={'requestId': str(uuid.uuid4())})
        assert response.status_code == 201, response.text
        stream = response.json()
        url = '/api/v1/station-streams/' + stream['streamId']
        for i in range(8):
            frame = np.full((180, 320, 3), (i*30, 80, 150), np.uint8)
            cv2.putText(frame, 'TEST sample '+str(i), (20, 90), cv2.FONT_HERSHEY_SIMPLEX, .7, (240,240,240), 2)
            ok, encoded = cv2.imencode('.jpg', frame)
            assert ok
            response = client.post(url + '/frames', headers={**headers, 'Content-Type': 'image/jpeg',
                'X-Frame-Seq': str(i), 'X-Frame-Time': str(i*.25), 'X-Stream-Epoch': str(stream['streamEpoch'])},
                content=encoded.tobytes())
            assert response.status_code == 200 and 'evidence' not in response.json(), response.text
        response = client.post(url + '/finish', headers=headers)
        assert response.status_code == 200, response.text
        ready = response.json()['evidence']['id']
        assert client.delete(url, headers=headers).status_code == 200
        review = client.post(url + '/manual-review', json=dict(requestId=str(uuid.uuid4()),
                            outcome='FAIL', reason='浏览器验收：人工确认后保留告警录像'))
        assert review.status_code == 200, review.text
    assert store.process_one()
    failed = record('retry', '浏览器验收：重试归档')
    with app.state.engine.begin() as c:
        c.execute(update(alert_evidence).where(alert_evidence.c.id==failed).values(status='FAILED',
            error='验收夹具：可重试的编码故障', attempts=3))
    for i in range(31):
        store.observe(settings.username,'browser-test','missing-'+str(i),'screw',0,None,
            {'stationId':'station-2', 'compliance': {'status':'REVIEW','reason':'分页验收记录 '+str(i)}},final=True)
    for station in ('station-1', 'station-3', None):
        store.observe(settings.username,'browser-test','bucket-'+str(station),'sticker',0,None,
            {'stationId':station, 'compliance': {'status':'REVIEW','reason':'工位分组验收 '+str(station)}},final=True)
    with app.state.engine.begin() as c:
        base = time.time()-100
        c.execute(update(alert_evidence).values(created_at=base))
        c.execute(update(alert_evidence).where(alert_evidence.c.id==ready).values(created_at=base+2))
        c.execute(update(alert_evidence).where(alert_evidence.c.id==failed).values(created_at=base+1))
    app.state.engine.dispose()
    return {'ready':ready,'failed':failed,'rows':33,'stream':stream['streamId']}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--port',type=int)
    args=parser.parse_args()
    if args.serve:
        import uvicorn
        uvicorn.run(factory(),host='127.0.0.1',port=args.port,log_level='warning')
        return
    output=ROOT/'test-results'/'evidence-ui'/('run-'+uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    env=dict(os.environ,APP_ENV='test',COOKIE_SECURE='false',REALTIME_WARMUP='false',
        DATABASE_URL='sqlite:///'+(output/'test.db').as_posix(), DATA_DIR=str(output/'data'),
        APP_USERNAME='evidence-browser',APP_PASSWORD=secrets.token_urlsafe(24),
        MODEL_PATH=str(ROOT/'models/yolo11s-pose.pt'))
    # Configure fixture generation without exposing the temporary password.
    original=dict(os.environ)
    os.environ.update(env)
    try:
        ids=fixture(Settings.from_env())
    finally:
        os.environ.clear()
        os.environ.update(original)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    base='http://127.0.0.1:'+str(port)
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (output/'server.log').open('wb') as log:
        process=subprocess.Popen([sys.executable,__file__,'--serve','--port',str(port)],cwd=ROOT,env=env,
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags)
        try:
            client=requests.Session()
            client.trust_env=False
            deadline=time.monotonic()+30
            while True:
                if process.poll() is not None:
                    raise RuntimeError('Fixture server exited; see '+str(output/'server.log'))
                try:
                    if client.get(base+'/api/v1/live',timeout=1).status_code==200:
                        break
                except requests.RequestException:
                    pass
                if time.monotonic()>deadline:
                    raise TimeoutError('Fixture server startup')
                time.sleep(.1)
            browser_env=dict(env,EVIDENCE_TEST_BASE=base,EVIDENCE_TEST_OUTPUT=str(output),
                EVIDENCE_TEST_READY=ids['ready'],EVIDENCE_TEST_FAILED=ids['failed'],EVIDENCE_TEST_STREAM=ids['stream'])
            probe = subprocess.run(['node',str(ROOT/'tools/test-evidence-history.cjs')],cwd=ROOT,env=browser_env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90, creationflags=flags)
            (output/'browser.log').write_bytes(probe.stdout)
            if probe.returncode:
                raise RuntimeError('Browser validation failed; see '+str(output/'browser.log')+'\n'+
                                   probe.stdout.decode('utf-8', errors='replace'))
            print('Evidence browser report: '+str(output/'verification.json'))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__=='__main__':
    main()
