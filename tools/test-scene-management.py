"""Run the browser workflow against isolated API/worker processes and real weights."""
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
    (ROOT / '.runtime').mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='scene-browser-', dir=ROOT / '.runtime'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    image = cv2.imread(str(ROOT / 'models/screw-reference.jpg'))
    if image is None:
        raise RuntimeError('The screw reference image is required for this integration test')
    image = cv2.resize(image, (640, 360))
    cv2.imwrite(str(output / 'reference.jpg'), image)
    writer = cv2.VideoWriter(str(output / 'sample.avi'), cv2.VideoWriter_fourcc(*'MJPG'), 2, (640, 360))
    if not writer.isOpened():
        raise RuntimeError('MJPG writer is unavailable')
    for _ in range(6):
        writer.write(image)
    writer.release()
    with (output / 'camera.y4m').open('wb') as handle:
        handle.write(b'YUV4MPEG2 W640 H360 F10:1 Ip A1:1 C420jpeg\n')
        pixels = cv2.cvtColor(image, cv2.COLOR_BGR2YUV_I420).tobytes()
        for _ in range(30):
            handle.write(b'FRAME\n' + pixels)
    env = dict(os.environ, APP_ENV='test', COOKIE_SECURE='false', REALTIME_WARMUP='false',
               REALTIME_BACKEND='yolo', POSE_BACKEND='yolo',
               DATABASE_URL='sqlite:///' + (output / 'test.db').as_posix(),
               DATA_DIR=str(output / 'data'), APP_USERNAME='admin', APP_PASSWORD=secrets.token_urlsafe(24),
               MODEL_PATH=str(ROOT / 'models/yolo11n-pose.pt'),
               SCENE_TEST_BASE=base, SCENE_TEST_OUTPUT=str(output), PYTHONUNBUFFERED='1')
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
                raise RuntimeError(f'Test server exited; see {output}')
            try:
                with opener.open(base + '/api/v1/health', timeout=1) as response:
                    if json.load(response)['status'] == 'ready':
                        break
            except (OSError, ValueError):
                pass
            time.sleep(.25)
        else:
            raise RuntimeError('Test server startup timed out')
        result = subprocess.run(['node', 'tools/test-scene-management.cjs'], cwd=ROOT, env=env)
        print('Browser test artifacts:', output, flush=True)
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


if __name__ == '__main__':
    raise SystemExit(main())
