"""Exercise terminal UI against disposable SQL state and browser camera fixtures."""
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

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / 'test-results' / 'terminal-browser-tests'
    folder.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='run-', dir=folder))
    (output / 'fixture.pt').write_bytes(b'UI fixture, never loaded for inference')
    from PIL import Image
    Image.new('RGB', (640,360), '#223244').save(output / 'preview.jpg')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, APP_ENV='test', COOKIE_SECURE='false', REALTIME_WARMUP='false',
        DATABASE_URL='sqlite:///' + (output / 'test.db').as_posix(), DATA_DIR=str(output / 'data'),
        APP_USERNAME='terminal-ui-test', APP_PASSWORD=secrets.token_urlsafe(24),
        MODEL_PATH=str(output / 'fixture.pt'), REALTIME_BACKEND='yolo', POSE_BACKEND='yolo',
        TERMINAL_TEST_BASE=f'http://127.0.0.1:{port}', TERMINAL_TEST_OUTPUT=str(output))
    subprocess.run([sys.executable, '-m', 'backend.init_db'], cwd=ROOT, env=env, check=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with (output / 'api.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.app:create_app', '--factory',
            '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError(f'Test API exited; inspect {output / "api.log"}')
                try:
                    with opener.open(env['TERMINAL_TEST_BASE'] + '/api/v1/health', timeout=1) as response:
                        if json.load(response)['status'] == 'ready':
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(.25)
            else:
                raise RuntimeError('Test API startup timed out')
            result = subprocess.run(['node', 'tools/test-terminal-ui.cjs'], cwd=ROOT, env=env)
            print('Terminal browser artifacts:', output, flush=True)
            return result.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    raise SystemExit(main())
