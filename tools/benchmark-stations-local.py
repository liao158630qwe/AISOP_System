"""Disposable six-stream real-GPU benchmark; never changes production bindings."""
import argparse
import http.cookiejar
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def reproducibility_manifest(code_root, tasks, inputs):
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    sources = {}
    for folder in ('backend', 'algorithms'):
        for path in sorted((code_root/folder).rglob('*.py')):
            sources[path.relative_to(code_root).as_posix()] = digest(path)
    for filename in ('station-collector.py', 'benchmark-stations.py', 'benchmark-stations-local.py'):
        path = ROOT/'tools'/filename
        sources['tools/'+filename] = digest(path)
    versions = {}
    for package in ('torch', 'torchvision', 'ultralytics', 'kornia', 'numpy', 'opencv-python',
                    'fastapi', 'uvicorn', 'sqlalchemy'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    commit = subprocess.run(['git', '-C', str(code_root), 'rev-parse', 'HEAD'],
        capture_output=True, text=True)
    return dict(createdAt=time.time(), python=sys.version, executable=sys.executable,
        platform=platform.platform(), cpuLogicalCount=os.cpu_count(), packages=versions,
        sourceRoot=str(code_root), gitHead=commit.stdout.strip() if commit.returncode==0 else None,
        sourceSha256=sources, sourceAggregateSha256=hashlib.sha256(
            json.dumps(sources, sort_keys=True).encode()).hexdigest(),
        inputs=[dict(stationId=f'station-{n}',analysisType=task,path=str(inputs[task]),
            sha256=digest(inputs[task])) for n,task in enumerate(tasks,1)],
        gpuMeasurement=dict(scope='device-all-processes',query='index,utilization.gpu,memory.used,memory.total',
            sampleIntervalSeconds=1,window='report.measurementStartedAt <= sample.at < report.measurementFinishedAt'))


def main(args):
    code_root = args.source_root.resolve() if args.source_root else ROOT
    root = ROOT/'test-results'/'station-gpu-benchmarks'
    root.mkdir(parents=True,exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=args.profile+'-',dir=root))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    tasks = args.tasks or (['pose','screw','sticker']*2 if args.profile=='mixed' else [args.profile]*6)
    inputs = {'pose':ROOT/'assets/factory.jpg','screw':ROOT/'models/screw-reference-overhead-v4.jpg',
        'sticker':ROOT/'models/sticker-reference-v1.jpg'}
    (output/'manifest.json').write_text(json.dumps(reproducibility_manifest(code_root,tasks,inputs),
        ensure_ascii=False,indent=2),encoding='utf-8')
    env = dict(os.environ,APP_ENV='test',COOKIE_SECURE='false',REALTIME_WARMUP='false',
        DATABASE_URL='sqlite:///'+(output/'test.db').as_posix(),DATA_DIR=str(output/'data'),
        APP_USERNAME='benchmark',APP_PASSWORD=secrets.token_urlsafe(24),REALTIME_BACKEND='yolo',
        POSE_BACKEND='yolo',REALTIME_POOL_SIZE=str(args.pose_pool),OPENCV_THREADS=str(args.opencv_threads),STATION_QUEUE_WAIT_MS='500',
        SPECIALIST_POOL_SIZE=str(args.specialist_pool),
        STATION_INFERENCE_CONCURRENCY=str(args.concurrency),
        TORCH_THREADS=str(args.torch_threads),
        MODEL_PATH=str(ROOT/'models/yolo11s-pose.pt'))
    subprocess.run([sys.executable,'-m','backend.init_db'],cwd=code_root,env=env,check=True)
    gpu = subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,utilization.gpu',
        '--format=csv,noheader'],capture_output=True,text=True)
    (output/'hardware.txt').write_text(gpu.stdout,encoding='utf-8')
    (output/'settings.json').write_text(json.dumps(dict(profile=args.profile,fps=args.fps,duration=args.duration,
        concurrency=args.concurrency,posePool=args.pose_pool,specialistPool=args.specialist_pool,opencvThreads=args.opencv_threads,
        torchThreads=args.torch_threads,queueWaitMs=500,tasks=tasks,sourceRoot=str(code_root),
        arrivalMode=args.arrival_mode),indent=2),encoding='utf-8')
    flags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (output/'api.log').open('wb') as log:
        process = subprocess.Popen([sys.executable,'-m','uvicorn','backend.app:create_app','--factory',
            '--host','127.0.0.1','--port',str(port)],cwd=code_root,env=env,stdout=log,stderr=log,
            stdin=subprocess.DEVNULL,creationflags=flags)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def request(path,method='GET',body=None,csrf=None):
                headers={'Content-Type':'application/json'}
                if csrf: headers['X-CSRF-Token']=csrf
                req=urllib.request.Request(base+'/api/v1'+path,method=method,headers=headers,
                    data=json.dumps(body).encode() if body is not None else None)
                with opener.open(req,timeout=30) as response:
                    return json.load(response)
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError('Benchmark API stopped; see '+str(output/'api.log'))
                try:
                    if request('/health')['status']=='ready': break
                except OSError: pass
                time.sleep(.25)
            else: raise RuntimeError('Benchmark API did not start')
            csrf=request('/login','POST',{'username':env['APP_USERNAME'],'password':env['APP_PASSWORD']})['csrf']
            for n,task in enumerate(tasks,1):
                data=request(f'/admin/terminals/station-{n}','PUT',{
                    'workstationId':f'benchmark-pc-{n}','cameraId':f'benchmark-cam-{n}',
                    'source':'rtsp','analysisType':task,'sampleFps':args.fps},csrf)
                env[f'AISOP_TERMINAL_KEY_{n}']=data['credential']
            command=[sys.executable,str(ROOT/'tools/benchmark-stations.py'),'--server',base,
                '--fps',str(args.fps),'--duration',str(args.duration),'--report',str(output/'report.json'),
                '--arrival-mode',args.arrival_mode,
                '--images',*[str(inputs[t]) for t in tasks]]
            stop = threading.Event()
            def monitor():
                with (output/'gpu.jsonl').open('w',encoding='utf-8') as samples:
                    while not stop.is_set():
                        at=time.time()
                        try:
                            reading=subprocess.run(['nvidia-smi','--query-gpu=index,utilization.gpu,memory.used,memory.total',
                                '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3,creationflags=flags)
                            samples.write(json.dumps(dict(at=at,csv=reading.stdout.strip(),returnCode=reading.returncode))+'\n')
                            samples.flush()
                        except (OSError,subprocess.SubprocessError):
                            pass
                        stop.wait(1)
            monitor_thread=threading.Thread(target=monitor,daemon=True)
            monitor_thread.start()
            try:
                result=subprocess.run(command,cwd=ROOT,env=env,timeout=args.duration+240)
            finally:
                stop.set();monitor_thread.join(timeout=4)
            (output/'metrics.json').write_text(json.dumps(request('/metrics'),ensure_ascii=False,indent=2),encoding='utf-8')
            (output/'scheduler.json').write_text(json.dumps(request('/admin/station-metrics'),ensure_ascii=False,indent=2),encoding='utf-8')
            gpu = subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,utilization.gpu',
                '--format=csv,noheader'],capture_output=True,text=True)
            (output/'hardware-after.txt').write_text(gpu.stdout,encoding='utf-8')
            print('Isolated real-GPU benchmark:',output,flush=True)
            return result.returncode
        finally:
            if process.poll() is None:
                process.terminate();process.wait(timeout=20)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=['pose','screw','sticker','mixed'],default='mixed')
    parser.add_argument('--tasks',nargs='+',choices=['pose','screw','sticker'],help='Explicit 1–6 station tasks, in station order')
    parser.add_argument('--source-root',type=Path,help='Isolated source snapshot root for before/after comparison')
    parser.add_argument('--fps',type=float,default=4)
    parser.add_argument('--arrival-mode',choices=('fixed','serial'),default='fixed',
                        help='Request arrival model; serial excludes browser capture and JPEG encoding')
    parser.add_argument('--duration',type=float,default=20)
    parser.add_argument('--concurrency',type=int,default=1)
    parser.add_argument('--pose-pool',type=int,default=1)
    parser.add_argument('--torch-threads',type=int,default=4)
    parser.add_argument('--specialist-pool',type=int,default=1)
    parser.add_argument('--opencv-threads',type=int,default=4)
    args=parser.parse_args()
    if not ((not args.tasks or 1<=len(args.tasks)<=6) and .5<=args.fps<=30 and 1<=args.duration<=3600 and 1<=args.concurrency<=6 and
            1<=args.pose_pool<=6 and 1<=args.specialist_pool<=6 and
            1<=args.torch_threads<=16 and 1<=args.opencv_threads<=16):
        parser.error('FPS .5–30, duration 1–3600, concurrency/pools 1–6 and threads 1–16 required')
    raise SystemExit(main(args))
