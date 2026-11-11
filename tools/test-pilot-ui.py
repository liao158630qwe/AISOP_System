"""Isolated browser workflows; paused registration uses real inference and Board."""
import argparse
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

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def main(paused=False, browser_script=None, source_root=None):
    # Run the server from a frozen source tree while keeping the current probe
    # and disposable artifacts in this checkout for comparable A/B runs.
    source_root = Path(source_root or ROOT).expanduser().resolve()
    script = Path(browser_script or ('tools/test-pilot-registration-ui.cjs' if paused else 'tools/test-pilot-ui.cjs'))
    script = (ROOT / script).resolve()
    if not (source_root / 'backend' / 'app.py').is_file() or not (source_root / 'backend' / 'init_db.py').is_file():
        raise ValueError(f'Source root must contain backend/app.py and backend/init_db.py: {source_root}')
    if not script.is_file():
        raise ValueError(f'Browser probe does not exist: {script}')
    output_root = ROOT / 'test-results' / 'pilot-browser-tests'
    output_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='run-', dir=output_root))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    for phase in ('holes', 'screws'):
        picture = Image.new('RGB', (960, 600), '#e2e7ed')
        draw = ImageDraw.Draw(picture)
        draw.rounded_rectangle((170, 100, 790, 500), radius=30, fill='#7c8796', outline='#3e4855', width=4)
        for i, (x, y) in enumerate(((220, 150), (740, 150), (220, 450), (740, 450)), 1):
            draw.ellipse((x-22, y-22, x+22, y+22), fill='#253343' if phase == 'holes' else '#c9cfd5', outline='#253343', width=3)
            if phase == 'screws':
                draw.line((x-12, y, x+12, y), fill='#253343', width=4)
                draw.line((x, y-12, x, y+12), fill='#253343', width=4)
            draw.text((x-12, y+28), f'{"H" if phase == "holes" else "S"}0{i}', fill='#253343')
        draw.text((365, 290), 'WORKFLOW TEST FIXTURE - NO INFERENCE', fill='white')
        picture.save(output / f'{phase}.jpg')
    env = dict(os.environ, APP_ENV='test', COOKIE_SECURE='false', REALTIME_WARMUP='false',
        DATABASE_URL='sqlite:///' + (output / 'test.db').as_posix(), DATA_DIR=str(output / 'data'),
        APP_USERNAME='pilot-test', APP_PASSWORD=secrets.token_urlsafe(24), REALTIME_BACKEND='yolo', POSE_BACKEND='yolo',
        PILOT_TEST_BASE=f'http://127.0.0.1:{port}', PILOT_TEST_OUTPUT=str(output),
        PILOT_TEST_SOURCE_ROOT=str(source_root), PYTHONUNBUFFERED='1',
        PILOT_REGISTRATION_ENABLED='false' if paused else 'true')
    (output / 'launcher.json').write_text(json.dumps(dict(sourceRoot=str(source_root),
        probeRoot=str(ROOT), browserScript=str(script), output=str(output), base=env['PILOT_TEST_BASE'],
        registrationPaused=paused), indent=2), encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'backend.init_db'], cwd=source_root, env=env, check=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = None
    with (output / 'api.log').open('wb') as log:
        try:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.app:create_app', '--factory',
                '--host', '127.0.0.1', '--port', str(port)], cwd=source_root, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=flags)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError(f'Test API exited; inspect {output / "api.log"}')
                try:
                    with opener.open(env['PILOT_TEST_BASE'] + '/api/v1/health', timeout=1) as response:
                        if json.load(response)['status'] == 'ready':
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(.25)
            else:
                raise RuntimeError('Test API startup timed out')
            result = subprocess.run(['node', str(script)], cwd=ROOT, env=env)
            print('Pilot browser artifacts:', output, flush=True)
            return result.returncode
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration-paused', action='store_true')
    parser.add_argument('--browser-script', help='Optional browser probe script; uses the same disposable API/database')
    parser.add_argument('--source-root', type=Path, help='Optional frozen API/frontend source tree; probes and artifacts stay in the main checkout')
    args = parser.parse_args()
    raise SystemExit(main(args.registration_paused, args.browser_script, args.source_root))
