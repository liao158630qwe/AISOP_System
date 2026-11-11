"""Collector lifecycle and benchmark accounting; fixtures do not measure GPU speed."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import ssl
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('size', [(1080, 1920), (1920, 1080), (2160, 3840), (1600, 1200), (300, 300)])
def test_portrait_scene_and_sticker_sampling_preserves_pixel_budget(size):
    collector = load('station-collector')
    width, height = collector.sample_size(*size, 1920, 1080, allow_portrait=True)
    assert max(width, height) <= 1920 and width*height <= 1920*1080
    assert abs(width/height - size[0]/size[1]) < .002
    if size == (1080, 1920):
        assert (width, height) == size, 'portrait detail must not be reduced to 607x1080'
    if size == (300, 300):
        assert (width, height) == size, 'do not upscale input'


def test_pose_and_screw_sampling_keep_their_existing_dimension_contract():
    collector = load('station-collector')
    assert collector.sample_size(1080, 1920, 1280, 720) == (405, 720)
    assert collector.sample_size(1080, 1920, 1920, 1080) == (607, 1080)


def test_collector_latest_frame_and_server_validation():
    module = load('station-collector')
    mailbox = queue.Queue(maxsize=1)
    module.latest_put(mailbox, {'frame':1})
    module.latest_put(mailbox, {'frame':2})
    assert mailbox.get_nowait() == {'frame':2}
    for server in ('http://remote.example', 'https://name:password@example.com', 'ftp://example.com', 'https://example.com?key=x'):
        with pytest.raises(ValueError):
            module.Client(server, 'fixture')
    assert module.Client('http://127.0.0.1:8000', 'fixture').server.endswith('8000')


@pytest.mark.parametrize('failure', ['connection', 'manual'])
def test_collector_closes_connection_when_startup_fails(monkeypatch, failure):
    module = load('station-collector')
    closed = []
    class Client:
        def __init__(self, *args): pass
        def request(self, path):
            if failure == 'connection':
                raise module.ApiError(0, 'connection lost')
            return {'binding': {'analysisType': 'manual'}}
        def close(self): closed.append(True)
    monkeypatch.setattr(module, 'Client', Client)
    monkeypatch.setenv('TEST_COLLECTOR_KEY', 'fixture')
    monkeypatch.setenv('TEST_COLLECTOR_SOURCE', '0')
    args = SimpleNamespace(server='http://localhost', key_env='TEST_COLLECTOR_KEY',
                           source_env='TEST_COLLECTOR_SOURCE')
    with pytest.raises((module.ApiError, ValueError)):
        module.run(args)
    assert closed == [True]


@pytest.fixture
def persistent_collector_server():
    state = dict(connections=0,requests=[])
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def setup(self):
            super().setup()
            with lock:
                state['connections'] += 1
                self.connection_id = state['connections']
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.respond()
        def do_POST(self):
            self.respond()
        def respond(self):
            body = self.rfile.read(int(self.headers.get('Content-Length',0)))
            with lock:
                state['requests'].append(dict(path=self.path,method=self.command,body=body,
                    connection=self.connection_id,key=self.headers.get('X-Terminal-Key')))
            if self.path.endswith('/drop-post'):
                self.send_response(200)
                self.send_header('Content-Length','100')
                self.end_headers()
                self.wfile.write(b'{"ok":');self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close();self.close_connection=True
                return
            error = self.path.endswith('/error')
            redirect = self.path.endswith('/redirect')
            close = self.path.endswith('/close')
            payload = json.dumps({'message':'fixture failure'} if error else
                {'connection':self.connection_id,'path':self.path}).encode()
            self.send_response(500 if error else 301 if redirect else 200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            if redirect:
                # Different hostname = different origin, even on this CPU fixture.
                self.send_header('Location',f'http://localhost:{self.server.server_port}/credential-sink')
            if close:
                self.send_header('Connection','close');self.close_connection=True
            self.end_headers();self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/prefix/',state
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_collector_reuses_connection_and_drains_http_errors(persistent_collector_server):
    module=load('station-collector')
    base,state=persistent_collector_server
    client=module.Client(base,'fixture-key')
    try:
        first=[client.request('/probe?i='+str(i)) for i in range(3)]
        assert len({row['connection'] for row in first}) == 1
        assert state['connections'] == 1
        assert first[0]['path'] == '/prefix/api/v1/probe?i=0'
        with pytest.raises(module.ApiError) as failure:
            client.request('/error')
        assert failure.value.status == 500 and str(failure.value) == 'fixture failure'
        assert client.request('/after-error')['connection'] == first[0]['connection']
        assert client.request('/close')['connection'] == first[0]['connection']
        assert client.request('/after-close')['connection'] != first[0]['connection']
        assert state['connections'] == 2
    finally:
        client.close()


def test_collector_does_not_replay_interrupted_post(persistent_collector_server):
    module=load('station-collector')
    base,state=persistent_collector_server
    client=module.Client(base,'fixture-key')
    try:
        with pytest.raises(module.ApiError) as failure:
            client.request('/drop-post','POST',b'unique-frame')
        assert failure.value.status == 0 and str(failure.value) == '服务器连接中断'
        dropped=[row for row in state['requests'] if row['path'].endswith('/drop-post')]
        assert len(dropped) == 1 and dropped[0]['body'] == b'unique-frame'
        assert client.request('/next-frame','POST',b'new-frame')['connection'] != dropped[0]['connection']
        assert sum(row['path'].endswith('/drop-post') for row in state['requests']) == 1
    finally:
        client.close()


def test_collector_reconnects_idle_socket_before_sending_post(persistent_collector_server, monkeypatch):
    module = load('station-collector')
    base, state = persistent_collector_server
    clock = [10.]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    client = module.Client(base, 'fixture-key')
    try:
        first = client.request('/first')
        clock[0] += 3.9
        assert client.request('/still-active')['connection'] == first['connection']
        clock[0] += 4.1
        next_request = client.request('/idle-post', 'POST', b'only-once')
        assert next_request['connection'] != first['connection']
        posted = [row for row in state['requests'] if row['path'].endswith('/idle-post')]
        assert len(posted) == 1 and posted[0]['body'] == b'only-once'
        assert state['connections'] == 2
    finally:
        client.close()


def test_benchmark_summary_excludes_warmup_and_keeps_gpu_devices_separate(tmp_path):
    module = load('summarize-station-benchmark')
    records = [dict(at=9, csv='0, 100, 100, 1000', returnCode=0),
        dict(at=10, csv='0, 20, 100, 1000\n1, 90, 200, 2000', returnCode=0),
        dict(at=11, csv='0, 40, 120, 1000\n1, 70, 200, 2000', returnCode=0),
        dict(at=12, csv='0, N/A, 150, 1000', returnCode=0),
        dict(at=13, csv='0, 60, 160, 1000', returnCode=0),
        dict(at=14, csv='', returnCode=1),
        dict(at=20, csv='0, 100, 500, 1000', returnCode=0)]
    path = tmp_path/'gpu.jsonl'
    path.write_text('\n'.join(json.dumps(row) for row in records), encoding='utf-8')
    summary = module.gpu_summary(path, 10, 20)
    first, second = summary['devices']
    assert first['validUtilizationSamples'] == 3 and first['samples'] == 4
    assert first['meanUtilizationPercent'] == 40 and first['p95UtilizationPercent'] == 60
    assert second['meanUtilizationPercent'] == 80 and second['p95UtilizationPercent'] == 90
    assert summary['ignoredOutsideWindow'] == 2 and summary['failedSamples'] == 1
    assert first['maxUnsampledGapSeconds'] == 7 and summary['scope'] == 'device-all-processes'


@pytest.mark.parametrize('failed', [False, True])
def test_benchmark_summary_never_invents_measurements(tmp_path, failed):
    module = load('summarize-station-benchmark')
    if failed:
        (tmp_path/'report.json').write_text(json.dumps(dict(status='failed', measurementCompleted=False,
            failurePhase='cold-warmup', httpStatus=0)), encoding='utf-8')
    result = module.summarize_run(tmp_path)
    assert result['measurementCompleted'] is False and result['stations'] == []
    assert 'totalEffectiveFps' not in result and result['gpu']['status'] == 'unavailable'


def test_benchmark_reports_cold_warmup_failure_without_secret(tmp_path, monkeypatch):
    from PIL import Image
    module = load('benchmark-stations')
    requests = []
    class Client:
        def __init__(self, server, key): pass
        def close(self): requests.append('disconnect')
        def request(self, path, method='GET', body=None, headers=None):
            requests.append(path)
            if path == '/terminal/context':
                return {'binding': dict(stationId='station-1', analysisType='pose', sampleFps=10)}
            if path == '/station-streams':
                return dict(streamId='fixture', streamEpoch=1, stationId='station-1', cameraId='one')
            if method == 'DELETE': return {'ok': True}
            raise module.collector.ApiError(0, 'private-device-key must not be saved')
    monkeypatch.setattr(module.collector, 'Client', Client)
    monkeypatch.setenv('AISOP_TERMINAL_KEY_1', 'private-device-key')
    image = tmp_path / 'frame.jpg'
    Image.new('RGB', (64, 48)).save(image)
    report = tmp_path / 'failure.json'
    with pytest.raises(module.collector.ApiError):
        module.run(SimpleNamespace(server='http://localhost', images=[image], fps=10, duration=1,
            deadline_ms=500, report=str(report), arrival_mode='serial'))
    data = json.loads(report.read_text(encoding='utf-8'))
    assert data['status'] == 'failed' and data['failurePhase'] == 'cold-warmup'
    assert data['httpStatus'] == 0 and data['measurementCompleted'] is False
    assert data['stations'] == [] and 'totalEffectiveFps' not in data
    assert 'private-device-key' not in report.read_text(encoding='utf-8')
    assert requests[:3] == ['/terminal/context', 'disconnect', '/station-streams']


def test_benchmark_waits_for_busy_release_without_replaying_frame(tmp_path, monkeypatch):
    from PIL import Image
    module = load('benchmark-stations')
    calls, close_attempts = [], []
    class Client:
        def __init__(self, server, key):
            self.binding = dict(stationId='station-1', cameraId='one', analysisType='pose', sampleFps=4)
            self.epoch = 0
        def close(self): pass
        def request(self, path, method='GET', body=None, headers=None):
            if path == '/terminal/context': return {'binding': self.binding}
            if path == '/station-streams':
                self.epoch += 1
                self.identity = dict(self.binding, streamId='stream-'+str(self.epoch), streamEpoch=self.epoch)
                return self.identity
            if method == 'DELETE':
                close_attempts.append(path)
                if len(close_attempts) == 1:
                    raise module.collector.ApiError(429, 'draining')
                return {'ok': True}
            sequence = int(headers['X-Frame-Seq'])
            calls.append((self.epoch, sequence))
            return dict(self.identity,frameSeq=sequence,queueWaitMs=0,serverProcessingMs=0)
    monkeypatch.setattr(module.collector, 'Client', Client)
    monkeypatch.setenv('AISOP_TERMINAL_KEY_1', 'fixture')
    image = tmp_path/'frame.jpg'
    Image.new('RGB', (64,48)).save(image)
    report = tmp_path/'report.json'
    module.run(SimpleNamespace(server='http://localhost',images=[image],fps=4,duration=1,
        deadline_ms=500,report=str(report),arrival_mode='serial'))
    assert len(close_attempts) == 3
    assert len(calls) == len(set(calls)), 'busy release cannot replay a frame POST'
    data = json.loads(report.read_text(encoding='utf-8'))
    assert data['measurementCompleted'] is True
    assert data['inputs'][0]['jpegQuality'] == 82 and len(data['inputs'][0]['encodedSha256']) == 64


def test_local_benchmark_manifest_has_file_hashes_without_environment_secrets(tmp_path, monkeypatch):
    module = load('benchmark-stations-local')
    code = tmp_path/'code'
    (code/'backend').mkdir(parents=True)
    (code/'algorithms').mkdir()
    (code/'backend'/'example.py').write_text('VALUE=1\n', encoding='utf-8')
    image = tmp_path/'frame.jpg'
    image.write_bytes(b'image')
    monkeypatch.setenv('APP_PASSWORD', 'do-not-export-this-secret')
    monkeypatch.setenv('AISOP_TERMINAL_KEY_1', 'do-not-export-this-device-key')
    manifest = module.reproducibility_manifest(code, ['pose'], {'pose': image})
    encoded = json.dumps(manifest)
    assert 'do-not-export' not in encoded
    assert len(manifest['sourceSha256']['backend/example.py']) == 64
    assert len(manifest['sourceAggregateSha256']) == 64
    assert manifest['gpuMeasurement']['scope'] == 'device-all-processes'
    assert manifest['inputs'][0]['stationId'] == 'station-1'


def test_collector_rejects_redirects_and_foreign_paths(persistent_collector_server):
    module=load('station-collector')
    base,state=persistent_collector_server
    client=module.Client(base,'fixture-key')
    try:
        with pytest.raises(module.ApiError) as failure:
            client.request('/redirect')
        assert failure.value.status == 301
        assert [row['path'] for row in state['requests']] == ['/prefix/api/v1/redirect']
        for endpoint in ('relative','//foreign.example/path','https://foreign.example/path','/path#fragment','/path\r\nHost: foreign'):
            with pytest.raises(ValueError):
                client.request(endpoint)
        with pytest.raises(ValueError):
            client.request('/probe',headers={'Host':'foreign.example'})
        client.request('/valid',headers={'x-terminal-key':'override-attempt'})
        assert all(row['key'] == 'fixture-key' and row['path'].startswith('/prefix/api/v1/') for row in state['requests'])
        assert state['connections'] == 1
    finally:
        client.close()
    secure=module.Client('https://example.com/prefix','fixture-key')
    assert secure._tls.check_hostname is True and secure._tls.verify_mode == ssl.CERT_REQUIRED
    secure.close()


def test_collector_serializes_shared_client_requests(persistent_collector_server):
    from concurrent.futures import ThreadPoolExecutor
    module=load('station-collector')
    base,state=persistent_collector_server
    client=module.Client(base,'fixture-key')
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda n:client.request('/parallel?i='+str(n)),range(8)))
        assert len(results) == len(state['requests']) == 8
        assert state['connections'] == 1
        assert len({row['connection'] for row in results}) == 1
    finally:
        client.close()


def test_collector_process_with_video_fixture(tmp_path, video_bytes):
    source = tmp_path / 'camera.avi'
    source.write_bytes(video_bytes)
    state = dict(epoch=0, frames=0, closed=0)
    binding = dict(stationId='station-1',workstationId='pc-01',cameraId='cam-01',revision=1,
        source='rtsp',analysisType='pose',sampleFps=4)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, data):
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def do_GET(self):
            self.reply({'binding':binding})
        def do_DELETE(self):
            state['closed'] += 1
            self.reply({'ok':True})
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length',0)))
            if self.path == '/api/v1/station-streams':
                assert set(json.loads(body)) == {'requestId'}
                state['epoch'] += 1
                state['stream'] = dict(binding,streamId=str(uuid.uuid4()),streamEpoch=state['epoch'])
                self.reply(state['stream'])
            else:
                assert body.startswith(b'\xff\xd8')
                assert self.headers['X-Station-Id'] == 'station-1'
                assert self.headers['X-Camera-Id'] == 'cam-01'
                state['frames'] += 1
                self.reply(dict(state['stream'],frameSeq=int(self.headers['X-Frame-Seq'])))
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    report = tmp_path / 'report.json'
    env = dict(os.environ,AISOP_TERMINAL_KEY='fixture-key',AISOP_CAMERA_SOURCE=str(source))
    try:
        result = subprocess.run([sys.executable,str(ROOT/'tools/station-collector.py'),
            '--server',f'http://127.0.0.1:{server.server_port}','--duration','3','--report',str(report)],
            cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
        assert result.returncode == 0, (result.stdout,result.stderr)
        data = json.loads(report.read_text(encoding='utf-8'))
        assert data['completed'] == state['frames'] > 0
        assert state['closed'] == state['epoch'] >= 1
        assert b'fixture-key' not in result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_fixed_arrival_benchmark_accounts_for_skipped_frames(tmp_path, monkeypatch):
    import time
    from PIL import Image
    module = load('benchmark-stations')
    closed = []
    class Client:
        def __init__(self, server, key):
            self.n = int(key)
            self.binding = dict(stationId=f'station-{key}',cameraId=f'cam-{key}',analysisType='pose',sampleFps=4)
            self.identity = dict(self.binding,streamId=str(uuid.uuid4()),streamEpoch=1)
            self.calls = 0
        def request(self,path,method='GET',body=None,headers=None):
            if path == '/terminal/context':
                return {'binding':self.binding}
            if method == 'DELETE':
                closed.append(self.n)
                return {'ok':True}
            if path == '/station-streams':
                return self.identity
            self.calls += 1
            if self.calls > 1:  # Leave cold preparation fast, then overload fixed arrivals.
                time.sleep(.32)
            return dict(self.identity,frameSeq=int(headers['X-Frame-Seq']),queueWaitMs=0,serverProcessingMs=320,inspection=None)
    monkeypatch.setattr(module.collector,'Client',Client)
    image = tmp_path/'frame.jpg'
    Image.new('RGB',(64,48)).save(image)
    for n in range(1,7):
        monkeypatch.setenv(f'AISOP_TERMINAL_KEY_{n}',str(n))
    report = tmp_path/'report.json'
    assert module.run(SimpleNamespace(server='http://localhost',images=[image]*6,fps=4,duration=1,
        deadline_ms=500,report=str(report))) == 1
    data = json.loads(report.read_text(encoding='utf-8'))
    assert data['arrivalMode'] == 'fixed'
    rows = data['stations']
    for row in rows:
        assert row['candidates'] == row['submitted']+row['skippedBusy']+row['skippedLate'] == 4
        assert row['skippedBusy'] >= 1 and not row['targetMet']
        assert row['completed'] == row['submitted']
    assert len(closed) == 12


def test_serial_benchmark_uses_independent_single_inflight_workers(tmp_path, monkeypatch):
    import time
    from PIL import Image
    module = load('benchmark-stations')
    clients, closed = {}, []
    lock = threading.Lock()
    class Client:
        def __init__(self, server, key):
            self.n = int(key)
            self.binding = dict(stationId=f'station-{key}',cameraId=f'cam-{key}',analysisType='pose',sampleFps=10)
            self.epoch = self.active = self.max_active = 0
            self.started = []
            clients[self.n] = self
        def request(self,path,method='GET',body=None,headers=None):
            if path == '/terminal/context':
                return {'binding':self.binding}
            if method == 'DELETE':
                closed.append(self.n)
                return {'ok':True}
            if path == '/station-streams':
                self.epoch += 1
                self.identity = dict(self.binding,streamId=str(uuid.uuid4()),streamEpoch=self.epoch)
                return self.identity
            if self.epoch == 2:
                with lock:
                    self.active += 1
                    self.max_active = max(self.max_active,self.active)
                    self.started.append(time.monotonic())
                # One station exceeds its interval; the other should continue
                # independently at target rate without waiting on the slow one.
                time.sleep(.15 if self.n == 1 else .02)
                with lock:
                    self.active -= 1
            return dict(self.identity,frameSeq=int(headers['X-Frame-Seq']),queueWaitMs=0,serverProcessingMs=150,
                serviceTimingsMs={'beforeWorkerMs':2,'modelMs':140},
                timingsMs={'pose':5},inspection={'timingsMs':{'registration':12}})
    monkeypatch.setattr(module.collector,'Client',Client)
    image = tmp_path/'frame.jpg'
    Image.new('RGB',(64,48)).save(image)
    for n in (1,2):
        monkeypatch.setenv(f'AISOP_TERMINAL_KEY_{n}',str(n))
    report = tmp_path/'report.json'
    assert module.run(SimpleNamespace(server='http://localhost',images=[image]*2,fps=10,duration=1,
        deadline_ms=500,report=str(report),arrival_mode='serial')) == 1
    data = json.loads(report.read_text(encoding='utf-8'))
    assert data['arrivalMode'] == 'serial'
    assert 'Actual serial request attempts' in data['candidateMeaning']
    assert 'browser capture/JPEG encoding' in data['source']
    slow, fast = data['stations']
    assert slow['completedWithinWindow'] < fast['completedWithinWindow']
    assert slow['completedWithinWindow'] < slow['completed'], 'final response outside window must not inflate effective FPS'
    for n, row in enumerate(data['stations'],1):
        assert clients[n].max_active == 1
        assert row['candidates'] == row['submitted'] == row['completed'] == len(clients[n].started)
        assert row['skippedBusy'] is None and row['skippedLate'] is None
        assert row['effectiveFps'] == row['completedWithinWindow']/data['seconds']
        assert row['stagesP95Ms']['service.beforeWorkerMs'] == 2
        assert row['stagesP95Ms']['service.modelMs'] == 140
        assert row['stagesP95Ms']['inspection.registration'] == 12
        assert row['stagesP95Ms']['pose'] == 5
    fast_intervals = [b-a for a,b in zip(clients[2].started,clients[2].started[1:])]
    # Windows monotonic() can tick at roughly 16 ms. This still clearly
    # distinguishes the target cadence from immediate 20 ms resubmission.
    assert fast_intervals and min(fast_intervals) >= .075, 'response completion must leave the remainder of the target interval'
    assert len(closed) == 4
