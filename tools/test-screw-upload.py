"""Exercise screw uploads with disposable services and a synthetic reference clip."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import cv2

ROOT = Path(__file__).resolve().parents[1]


def main():
    output_root = ROOT / 'test-results' / 'screw-upload-tests'
    output_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='run-', dir=output_root))
    image = cv2.imread(str(ROOT / 'models' / 'screw-reference.jpg'))
    if image is None:
        raise RuntimeError('The screw reference image is required for this workflow test')
    image = cv2.resize(image, (1920, 1080))
    fixture = output / 'upload-native-check.avi'
    writer = cv2.VideoWriter(str(fixture), cv2.VideoWriter_fourcc(*'MJPG'), 4, (1920, 1080))
    if not writer.isOpened():
        raise RuntimeError('MJPG video encoding is unavailable')
    try:
        for _ in range(9):
            writer.write(image)
    finally:
        writer.release()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    env = dict(os.environ, APP_ENV='test', COOKIE_SECURE='false', REALTIME_WARMUP='false',
               REALTIME_BACKEND='yolo', POSE_BACKEND='yolo',
               DATABASE_URL='sqlite:///' + (output / 'test.db').as_posix(), DATA_DIR=str(output / 'data'),
               APP_USERNAME='screw-upload-test', APP_PASSWORD=secrets.token_urlsafe(24),
               MODEL_PATH=str(ROOT / 'models' / 'yolo11s-pose.pt'),
               SCREW_UPLOAD_TEST_BASE=base, SCREW_UPLOAD_TEST_OUTPUT=str(output),
               SCREW_UPLOAD_TEST_VIDEO=str(fixture), PYTHONUNBUFFERED='1')
    subprocess.run([sys.executable, '-m', 'backend.init_db'], cwd=ROOT, env=env, check=True)
    processes, logs = [], []
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        for name, args in [('api', ['-m', 'uvicorn', 'backend.app:create_app', '--factory', '--host', '127.0.0.1', '--port', str(port)]),
                           ('worker', ['-m', 'backend.worker'])]:
            log = (output / f'{name}.log').open('wb')
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(120):
            if any(process.poll() is not None for process in processes):
                raise RuntimeError(f'Test service exited; see {output}')
            try:
                with opener.open(base + '/api/v1/health', timeout=1) as response:
                    if json.load(response)['status'] == 'ready':
                        break
            except (OSError, ValueError):
                pass
            time.sleep(.25)
        else:
            raise RuntimeError('Test API startup timed out')
        result = subprocess.run(['node', 'tools/test-screw-upload.cjs'], cwd=ROOT, env=env)
        if result.returncode:
            for name in ('api', 'worker'):
                print(f'{name} log tail:', flush=True)
                print((output / f'{name}.log').read_text(encoding='utf-8', errors='replace')[-6000:], flush=True)
        return result.returncode
    finally:
        for process in processes:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
                else:
                    process.terminate()
                process.wait(timeout=15)
        for log in logs:
            log.close()
        print('Screw upload artifacts:', output, flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
