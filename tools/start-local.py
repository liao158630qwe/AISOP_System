"""Start both local services with a persistent, generated development credential."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'


def main():
    RUNTIME.mkdir(exist_ok=True)
    config_path = RUNTIME / 'local-settings.json'
    if not config_path.exists():
        config_path.write_text(json.dumps({'username':'admin', 'password':secrets.token_urlsafe(18)}, indent=2), encoding='utf-8')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if len(config['password']) < 12:
        raise RuntimeError('local-settings.json password must contain at least 12 characters')
    with socket.socket() as check:
        if check.connect_ex(('127.0.0.1',8000)) == 0:
            print('Port 8000 is already in use. Existing service was left running. Open http://127.0.0.1:8000')
            return
    env = dict(os.environ, APP_ENV='development', COOKIE_SECURE='false',
        DATABASE_URL='sqlite:///' + (RUNTIME / 'local.db').as_posix(),
        DATA_DIR=str(RUNTIME / 'local-data'), MODEL_PATH=str(ROOT / 'models/yolo11s-pose.pt'),
        APP_USERNAME=config['username'], APP_PASSWORD=config['password'], PYTHONUNBUFFERED='1')
    subprocess.run([sys.executable,'-m','backend.init_db'],cwd=ROOT,env=env,check=True)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    processes = []
    try:
        for name, args in [('api',['-m','uvicorn','backend.app:create_app','--factory','--host','127.0.0.1','--port','8000']),
                           ('worker',['-m','backend.worker'])]:
            with (RUNTIME / f'local-{name}.log').open('ab') as log:
                child = subprocess.Popen([sys.executable,*args],cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                    stdout=log,stderr=log,creationflags=flags,start_new_session=os.name!='nt')
            processes.append(child)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(60):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('Service exited; see .runtime/local-api.log and local-worker.log')
            try:
                with opener.open('http://127.0.0.1:8000/api/v1/health',timeout=2) as r:
                    if json.load(r).get('status') == 'ready':
                        break
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        else:
            raise RuntimeError('Server did not become ready; see local-api.log')
        body=json.dumps(config).encode()
        req=urllib.request.Request('http://127.0.0.1:8000/api/v1/login',data=body,headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=5) as r:
            assert r.status==200
        (RUNTIME / 'local-processes.json').write_text(json.dumps({'api':processes[0].pid,'worker':processes[1].pid}),encoding='utf-8')
        print('Ready: http://127.0.0.1:8000')
        print('Credentials: '+str(config_path))
        print('API PID:',processes[0].pid,'Worker PID:',processes[1].pid)
    except Exception:
        for child in processes:
            if child.poll() is None:
                child.terminate()
        raise


if __name__ == '__main__':
    main()
