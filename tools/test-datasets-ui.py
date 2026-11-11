"""Isolated real API/Worker/browser video workflow, generated non-field fixtures."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_datasets import video


def main():
    output = ROOT / 'test-results' / ('datasets-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    for seed in range(6):
        video(output, seed)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    env = dict(os.environ, APP_ENV='test', COOKIE_SECURE='false', REALTIME_WARMUP='false',
        REALTIME_BACKEND='yolo', POSE_BACKEND='yolo', DATABASE_URL='sqlite:///' + (output/'test.db').as_posix(),
        DATA_DIR=str(output/'data'), APP_USERNAME='admin', APP_PASSWORD=secrets.token_urlsafe(24),
        MODEL_PATH=str(ROOT/'models/yolo11n-pose.pt'), DATASET_TEST_BASE=base, DATASET_TEST_OUTPUT=str(output), PYTHONUNBUFFERED='1')
    subprocess.run([sys.executable, '-m', 'backend.init_db'], cwd=ROOT, env=env, check=True)
    processes, logs = [], []
    try:
        for name, args in [('api', ['-m','uvicorn','backend.app:create_app','--factory','--host','127.0.0.1','--port',str(port)]), ('worker',['-m','backend.worker'])]:
            log = (output/(name+'.log')).open('wb'); logs.append(log)
            processes.append(subprocess.Popen([sys.executable,*args], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(120):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError(f'Test service stopped: {output}')
            try:
                with opener.open(base+'/api/v1/health', timeout=1): break
            except OSError: time.sleep(.25)
        else: raise RuntimeError('Test API did not start')
        result = subprocess.run(['node','tools/test-datasets-ui.cjs'], cwd=ROOT, env=env)
        print('Verification artifacts:', output, flush=True)
        return result.returncode
    finally:
        import psutil
        for p in processes:
            if p.poll() is None:
                try:
                    children=psutil.Process(p.pid).children(recursive=True)
                    for child in reversed(children): child.terminate()
                    p.terminate()
                    _,alive=psutil.wait_procs(children,timeout=5)
                    for child in alive: child.kill()
                    p.wait(timeout=5)
                except (psutil.Error,subprocess.TimeoutExpired): p.kill()
        for log in logs: log.close()


if __name__=='__main__':
    sys.exit(main())
