"""Real browser/API/collector control integration with synthetic camera and model."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
import httpx
import numpy as np
import uvicorn

from backend.app import create_app
from backend.config import Settings
from backend.db import initialize


def main():
    folder = ROOT / 'test-results' / 'controlled-collection-e2e'
    folder.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='run-', dir=folder))
    model = output / 'fixture.pt'
    model.write_bytes(b'Synthetic model; never used for inference')
    settings = Settings('sqlite:///' + (output / 'test.db').as_posix(), output / 'data',
        'collector-ui-test', secrets.token_urlsafe(24), model, 'test', False,
        realtime_warmup=False, pose_backend='yolo')
    app = create_app(settings)
    initialize(app.state.engine)

    def predict(payload, *, stream_key, frame_time):
        state = app.state.realtime.streams.get(stream_key, frame_time, ('synthetic',))
        state['samples'] = state.get('samples', 0) + 1
        return dict(people=[], samples=state['samples'],
                    compliance=dict(status='REVIEW', label='合成观察', reason='测试模型'))
    app.state.realtime.predict = predict
    app.state.evidence.observe = lambda *args, **kwargs: None
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
        access_log=False, log_level='error'))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    stop = threading.Event()
    worker_thread = None
    errors = []
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=15) as http:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            else:
                raise RuntimeError('Disposable API failed to start')
            login = http.post('/api/v1/login', json={'username':settings.username,'password':settings.password})
            login.raise_for_status()
            binding = http.put('/api/v1/admin/terminals/station-1',
                headers={'X-CSRF-Token':login.json()['csrf']}, json=dict(workstationId='e2e-pc',
                cameraId='e2e-camera', analysisType='pose', sampleFps=4, maxSampleGapSeconds=1))
            binding.raise_for_status()
            registered = binding.json()
        spec = importlib.util.spec_from_file_location('e2e_collector', ROOT / 'tools' / 'station-collector.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        producer = module.Client(base, registered['credential'])
        control = module.ControlledCollector(producer, registered['binding'])
        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        frame[:] = (100, 70, 30)
        cv2.rectangle(frame, (80, 40), (240, 140), (30, 200, 120), -1)
        ok, encoded = cv2.imencode('.jpg', frame)
        assert ok
        payload = encoded.tobytes()

        def produce():
            try:
                while not stop.is_set():
                    control.step(dict(payload=payload, at=time.monotonic(), generation=1))
                    stop.wait(.2)
            except BaseException as error:
                errors.append(type(error).__name__)
            finally:
                control.shutdown()
                producer.close()
        worker_thread = threading.Thread(target=produce, daemon=True)
        worker_thread.start()
        env = dict(os.environ, APP_ENV='test', PILOT_TEST_BASE=base, PILOT_TEST_OUTPUT=str(output),
                   APP_USERNAME=settings.username, APP_PASSWORD=settings.password)
        result = subprocess.run(['node', 'tools/test-controlled-collection-ui.cjs'], cwd=ROOT, env=env)
        print('Controlled collection integration artifacts:', output, flush=True)
        if errors:
            raise RuntimeError('Collector thread failed: ' + ', '.join(errors))
        return result.returncode
    finally:
        stop.set()
        if worker_thread:
            worker_thread.join(timeout=20)
        server.should_exit = True
        server_thread.join(timeout=15)
        app.state.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
