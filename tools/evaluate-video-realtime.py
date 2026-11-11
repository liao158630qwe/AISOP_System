"""Measure one screw video with a disposable API and the actual browser UI.

No production database, camera binding, model or frontend source is changed.
API runs use prepared JPEGs; browser runs decode the original video at 1x speed.
"""
import argparse
import hashlib
import http.cookiejar
import json
import math
import os
from pathlib import Path
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def stats(values):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return {'samples': 0}
    return dict(samples=len(values), mean=round(statistics.mean(values), 3),
                p50=round(statistics.median(values), 3),
                p95=round(values[math.ceil(.95*len(values))-1], 3),
                p99=round(values[math.ceil(.99*len(values))-1], 3), max=round(values[-1], 3))


def serve(args):
    import uvicorn
    from backend.app import create_app
    from starlette.responses import FileResponse
    from starlette.routing import Route
    app = create_app()

    async def source(request):
        return FileResponse(args.video, media_type='video/mp4')

    # Local fixture only; precedes the app's catch-all static route.
    app.router.routes.insert(0, Route('/__test/source.mp4', source))
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


def main(args):
    import cv2
    from algorithms.screws import snapshot
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    frozen = snapshot()
    video = args.video.resolve()
    cap = cv2.VideoCapture(str(video))
    assert cap.isOpened(), video
    fps, count = cap.get(5), int(cap.get(7))
    duration = count/fps
    with video.open('rb') as handle:
        source_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
    meta = dict(path=str(video), bytes=video.stat().st_size, nativeFps=fps, frames=count,
                duration=duration, width=cap.get(3), height=cap.get(4),
                sha256=source_hash)
    save(out/'source.json', meta)
    save(out/'frozen-model.json', frozen)
    print('Source:', json.dumps(meta), flush=True)
    grids = {rate: [i/rate for i in range(math.ceil(duration*rate))] for rate in (4, 10)}
    wanted = {}
    for t in set(grids[4]+grids[10]):
        wanted.setdefault(min(count-1, math.ceil((t-1e-6)*fps)), []).append(t)
    prepared, encodings = {}, []
    started = time.perf_counter()
    for index in range(count):
        if index not in wanted:
            assert cap.grab()
            continue
        ok, frame = cap.read()
        assert ok
        stamp = time.perf_counter()
        frame = cv2.resize(frame, (1920,1080))
        ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY,82])
        if len(jpeg)>1024*1024:
            ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY,60])
        assert ok and len(jpeg)<=1024*1024
        encodings.append((time.perf_counter()-stamp)*1000)
        for t in wanted[index]:
            prepared[t]=(jpeg.tobytes(), index/fps)
    cap.release()
    save(out/'preparation.json', dict(seconds=time.perf_counter()-started, frames=len(wanted),
         resizeEncodeMs=stats(encodings), excludedFromApiTiming=True))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    env=dict(os.environ, APP_ENV='test', COOKIE_SECURE='false', REALTIME_WARMUP='false',
        DATABASE_URL='sqlite:///'+(out/'test.db').as_posix(), DATA_DIR=str(out/'data'),
        APP_USERNAME='video-evaluation', APP_PASSWORD=secrets.token_urlsafe(24),
        REALTIME_BACKEND='yolo', POSE_BACKEND='yolo', REALTIME_POOL_SIZE='2',
        SPECIALIST_POOL_SIZE='1', OPENCV_THREADS='4', TORCH_THREADS='4',
        STATION_INFERENCE_CONCURRENCY='1', WORKER_TRAINING_ENABLED='false',
        MODEL_PATH=str(ROOT/'models/yolo11s-pose.pt'),
        VIDEO_EVAL_BASE=base, VIDEO_EVAL_OUTPUT=str(out), PYTHONUNBUFFERED='1')
    save(out/'settings.json', dict(poseBackend='yolo', posePool=2, specialistPool=1,
         opencvThreads=4, torchThreads=4, scope='one full video, one stream at a time',
         gpuSharing='Other GPU applications are left running; utilization is sampled.'))
    subprocess.run([sys.executable,'-m','backend.init_db'],cwd=ROOT,env=env,check=True)
    stop=threading.Event()
    phase={'name':'startup'}

    def monitor():
        with (out/'gpu.jsonl').open('w',encoding='utf-8') as file:
            while not stop.is_set():
                result=subprocess.run(['nvidia-smi','--query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total',
                    '--format=csv,noheader,nounits'],capture_output=True,text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                file.write(json.dumps(dict(at=time.time(),phase=phase['name'],csv=result.stdout.strip()))+'\n')
                file.flush()
                stop.wait(1)

    thread=threading.Thread(target=monitor,daemon=True)
    thread.start()
    log=(out/'api.log').open('wb')
    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(video),'--serve','--port',str(port)],
        cwd=ROOT,env=env,stdout=log,stderr=log,stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf=None

    def request(path, method='GET', data=None, headers=None):
        h=dict(headers or {})
        if csrf: h['X-CSRF-Token']=csrf
        if isinstance(data,dict):
            data=json.dumps(data).encode()
            h['Content-Type']='application/json'
        req=urllib.request.Request(base+'/api/v1'+path,data=data,method=method,headers=h)
        with opener.open(req,timeout=90) as response:
            return json.load(response)

    def frame(stream, index, payload, timestamp):
        started=time.perf_counter()
        body=request('/realtime/frame?analysis_type=screw','POST',payload,
            {'Content-Type':'image/jpeg','X-Stream-Id':stream,'X-Frame-Id':str(index),
             'X-Frame-Time':str(timestamp),'X-Captured-At':str(round(time.time()*1000))})
        return body,(time.perf_counter()-started)*1000

    try:
        for _ in range(180):
            if process.poll() is not None: raise RuntimeError('API failed; see api.log')
            try:
                if request('/health')['status']=='ready': break
            except OSError: pass
            time.sleep(.25)
        else: raise RuntimeError('API startup timeout')
        csrf=request('/login','POST',dict(username=env['APP_USERNAME'],password=env['APP_PASSWORD']))['csrf']
        warm=str(uuid.uuid4())
        warmups=[]
        for i in range(4):
            _,ms=frame(warm,i,prepared[0.][0],i*.25)
            warmups.append(ms)
        request('/realtime/streams/'+warm,'DELETE')
        save(out/'warmup.json',dict(latencyMs=warmups))
        report={'source':meta,'apiRuns':[]}
        for rate in (4,10):
            phase['name']=f'api-{rate}fps'
            stream=str(uuid.uuid4())
            records,errors=[],[]
            origin=time.perf_counter()
            next_index=0
            while next_index<len(grids[rate]):
                due=origin+next_index/rate
                if due>time.perf_counter(): time.sleep(due-time.perf_counter())
                index=max(next_index,int((time.perf_counter()-origin)*rate))
                if index>=len(grids[rate]) or time.perf_counter()-origin>=duration: break
                payload,source_time=prepared[grids[rate][index]]
                sent=time.perf_counter()-origin
                try:
                    body,ms=frame(stream,index,payload,source_time)
                    records.append(dict(index=index,sourceTime=source_time,sentAt=sent,latencyMs=ms,
                        sourceToResultMs=(time.perf_counter()-origin-source_time)*1000,response=body))
                except Exception as exc:
                    errors.append(dict(index=index,error=str(exc)))
                next_index=index+1
                if len(records)%60==0:
                    print(f'API {rate} fps: {sent:.1f}/{duration:.1f}s, {len(records)} completed',flush=True)
            final=request(f'/realtime/streams/{stream}/finish?analysis_type=screw','POST')
            request('/realtime/streams/'+stream,'DELETE')
            passed=next((r for r in records if r['response']['compliance']['status']=='PASS'),None)
            completed=sum(r['sentAt']+r['latencyMs']/1000<=duration for r in records)
            result=dict(requestedFps=rate,sourceSeconds=duration,candidateFrames=len(grids[rate]),
                completedFrames=len(records),completedWithinVideo=completed,sustainedFps=completed/duration,
                skippedCandidateFrames=len(grids[rate])-len(records)-len(errors),errors=errors,
                latencyMs=stats([r['latencyMs'] for r in records]),
                sourceToResultMs=stats([r['sourceToResultMs'] for r in records]),
                stagesMs={key:stats([r['response'].get('timingsMs',{}).get(key) for r in records])
                    for key in records[-1]['response'].get('timingsMs',{})},
                firstPassSourceTime=passed['sourceTime'] if passed else None,
                liveFailCount=sum(r['response']['compliance']['status']=='FAIL' for r in records),
                registeredFrames=sum(bool(r['response']['inspection']['registered']) for r in records),
                final=final)
            save(out/f'api-{rate}fps-frames.json',records)
            save(out/f'api-{rate}fps.json',result)
            report['apiRuns'].append(result)
            save(out/'report.json',report)
            print('API result:',rate,round(result['sustainedFps'],3),final['compliance']['status'],flush=True)
        del prepared
        phase['name']='browser'
        subprocess.run(['node',str(ROOT/'tools/evaluate-video-realtime.cjs')],cwd=ROOT,env=env,check=True,timeout=300)
        report['browserRuns']=json.loads((out/'browser.json').read_text(encoding='utf-8'))
        report['unchangedModelAndConfig']=snapshot()==frozen
        save(out/'report.json',report)
        save(out/'metrics.json',request('/metrics'))
        print('Saved:',out,flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)
        log.close()
        stop.set()
        thread.join(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args()
    if args.serve: serve(args)
    else:
        if not args.output: parser.error('--output is required')
        main(args)
