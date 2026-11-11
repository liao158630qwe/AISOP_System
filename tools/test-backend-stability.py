"""Disposable GPU service test: six streams, worker crash/retry, API restart.

Uses a repeated reference image, not six physical cameras or accuracy evidence.
Only the subprocesses and temporary database created here are modified.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import cv2
import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    output_root = ROOT / 'test-results' / 'backend-stability'
    output_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='run-', dir=output_root))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    env = dict(os.environ, APP_ENV='test', COOKIE_SECURE='false', REALTIME_WARMUP='false',
        REALTIME_BACKEND='yolo', POSE_BACKEND='yolo', REALTIME_POOL_SIZE='2',
        DATABASE_URL='sqlite:///' + (output / 'test.db').as_posix(), DATA_DIR=str(output / 'data'),
        APP_USERNAME='stability-test', APP_PASSWORD=secrets.token_urlsafe(24),
        MODEL_PATH=str(ROOT / 'models' / 'yolo11s-pose.pt'), PYTHONUNBUFFERED='1')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    processes, logs = {}, []
    report = {'passed': False, 'scope': 'SQLite, six HTTP streams with reference imagery, real CUDA inference; not field acceptance'}
    client = requests.Session()
    client.trust_env = False

    def start(role):
        args = ['-m', 'backend.worker'] if role == 'worker' else [
            '-m', 'uvicorn', 'backend.app:create_app', '--factory', '--host', '127.0.0.1', '--port', str(port)]
        handle = (output / (role + '.log')).open('ab')
        logs.append(handle)
        processes[role] = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, creationflags=flags)

    def stop(role):
        process = processes.get(role)
        if process and process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            else:
                process.kill()
            process.wait(timeout=15)

    def wait_for(check, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = check()
                if result:
                    return result
            except requests.ConnectionError:
                pass
            time.sleep(.15)
        raise AssertionError('Timed out waiting for service condition')

    def get(path):
        return client.get(base + path, timeout=5)

    try:
        subprocess.run([sys.executable, '-m', 'backend.init_db'], cwd=ROOT, env=env, check=True, creationflags=flags)
        start('api')
        wait_for(lambda: get('/api/v1/health').status_code == 200)
        assert get('/api/v1/ready').status_code == 503
        start('worker')
        wait_for(lambda: get('/api/v1/ready').status_code == 200)
        response = client.post(base + '/api/v1/login', json={
            'username': env['APP_USERNAME'], 'password': env['APP_PASSWORD']}, timeout=10)
        response.raise_for_status()
        client.headers['X-CSRF-Token'] = response.json()['csrf']
        image = cv2.imread(str(ROOT / 'models' / 'screw-reference.jpg'))
        assert image is not None
        image = cv2.resize(image, (640, 360))
        ok, encoded = cv2.imencode('.jpg', image)
        assert ok
        payload = encoded.tobytes()
        # Model load is measured separately from steady request latency.
        warmed = client.post(base + '/api/v1/realtime/frame', data=payload,
            headers={'Content-Type': 'image/jpeg'}, timeout=60)
        warmed.raise_for_status()
        ids = [str(uuid.uuid4()) for _ in range(6)]
        cookie, csrf = client.cookies.get_dict(), client.headers['X-CSRF-Token']

        def stream(index):
            with requests.Session() as peer:
                peer.trust_env = False
                peer.cookies.update(cookie)
                peer.headers.update({'X-CSRF-Token': csrf, 'Content-Type': 'image/jpeg', 'X-Stream-Id': ids[index]})
                statuses, latencies = [], []
                for frame in range(12):
                    for retry in range(30):
                        started = time.perf_counter()
                        response = peer.post(base + '/api/v1/realtime/frame', data=payload,
                            headers={'X-Frame-Time': str(frame / 4)}, timeout=60)
                        statuses.append(response.status_code)
                        if response.status_code != 429:
                            break
                        time.sleep(.1 + index * .015)
                    assert response.status_code == 200, response.text
                    latencies.append((time.perf_counter() - started) * 1000)
                return statuses, latencies

        def health():
            with requests.Session() as peer:
                peer.trust_env = False
                return [peer.get(base + '/api/v1/health', timeout=5).status_code for _ in range(20)]

        with ThreadPoolExecutor(max_workers=21) as pool:
            frames = [pool.submit(stream, i) for i in range(6)]
            probes = [pool.submit(health) for _ in range(15)]
            results = [future.result() for future in frames]
            statuses = [status for future in probes for status in future.result()]
        assert statuses == [200] * 300
        metrics = get('/api/v1/metrics').json()
        assert metrics['streamStates']['pose']['active'] == 6
        report['sixStreams'] = {'successfulFrames': 72, 'statuses': dict(Counter(
            code for codes, _ in results for code in codes)), 'healthChecksPassed': 300,
            'preservedStates': 6, 'maxSuccessfulHttpMs': round(max(t for _, times in results for t in times), 2)}
        print('Six streams and 300 health probes passed', flush=True)
        for identity in ids:
            assert client.delete(base + '/api/v1/realtime/streams/' + identity, timeout=5).status_code == 200

        fixture = output / 'reference.avi'
        writer = cv2.VideoWriter(str(fixture), cv2.VideoWriter_fourcc(*'MJPG'), 4, (640, 360))
        assert writer.isOpened()
        for _ in range(32):
            writer.write(image)
        writer.release()
        uploaded = client.post(base + '/api/v1/media?filename=reference.avi', data=fixture.read_bytes(), timeout=30)
        uploaded.raise_for_status()
        key = str(uuid.uuid4())
        body = {'media_id': uploaded.json()['id'], 'options': {'sample_fps': 4}}
        queued = client.post(base + '/api/v1/analysis-runs', json=body, headers={'Idempotency-Key': key}, timeout=30)
        queued.raise_for_status()
        identity = queued.json()['id']
        path = '/api/v1/analysis-runs/' + identity
        def running():
            row = get(path).json()
            assert row['status'] not in ('FAILED', 'SUCCEEDED'), row
            return row['status'] == 'RUNNING'
        wait_for(running, 30)
        stop('worker')
        print('Worker stopped during inference; checking heartbeat expiry and lease recovery', flush=True)
        wait_for(lambda: get('/api/v1/ready').status_code == 503, 40)
        assert get('/api/v1/live').status_code == 200
        start('worker')
        wait_for(lambda: get('/api/v1/ready').status_code == 200)
        def completed():
            row = get(path).json()
            assert row['status'] not in ('FAILED', 'CANCELLED'), row
            return row if row['status'] == 'SUCCEEDED' else None
        final = wait_for(completed, 120)
        assert final['attempts'] == 2, final
        artifact = get(path + '/artifacts/analysis.json')
        artifact.raise_for_status()
        report['workerRecovery'] = {'status': final['status'], 'attempts': final['attempts'], 'readinessDetectedLoss': True}
        stop('api')
        start('api')
        wait_for(lambda: get('/api/v1/ready').status_code == 200)
        assert get('/api/v1/session').status_code == 200
        assert get(path + '/artifacts/analysis.json').content == artifact.content
        retried = client.post(base + '/api/v1/analysis-runs', json=body, headers={'Idempotency-Key': key}, timeout=30)
        assert retried.status_code == 202 and retried.json()['id'] == identity
        report.update(apiRestart={'sessionRetained': True, 'artifactsRetained': True, 'idempotencyRetained': True}, passed=True)
        print('Worker retry and API restart recovery passed', flush=True)
    finally:
        for role in ('api', 'worker'):
            stop(role)
        for handle in logs:
            handle.close()
        client.close()
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Report:', output / 'report.json', flush=True)


if __name__ == '__main__':
    main()
