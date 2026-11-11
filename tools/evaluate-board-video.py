"""Real video -> station inference -> original Board and terminal pages.

Disposable database/API, unchanged models. Runs one stream then six independent
streams of the same video at 1x speed. Six streams share source decoding only;
each uses real inference and its own temporal history. No production records.
Use --station-only to measure the station path without browser/Board requests.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.cookiejar
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('collector',ROOT/'tools/station-collector.py')
collector=importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def save(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)


def stats(values):
    v=sorted(x for x in values if isinstance(x,(int,float)))
    if not v:return {'samples':0}
    return dict(samples=len(v),mean=round(statistics.mean(v),3),p50=round(statistics.median(v),3),
                p95=round(v[math.ceil(.95*len(v))-1],3),p99=round(v[math.ceil(.99*len(v))-1],3),max=round(v[-1],3))


def finish_station_runs(stations):
    """Drain and archive each station independently after the video ends.

    A transport timeout on one station must not hold healthy stations open and
    manufacture end-of-video evidence gaps in all of their final decisions.
    """
    def finish(station):
        if station['future']:
            station['records'].append(station['future'].result())
        for attempt in range(3):
            try:
                return station['client'].request('/station-streams/'+station['stream']['streamId']+'/finish','POST')
            except collector.ApiError as exc:
                if exc.status not in (429,503) or attempt==2:
                    return {'error':str(exc),'status':exc.status}
                time.sleep(.2)
    with ThreadPoolExecutor(max_workers=len(stations)) as pool:
        return list(pool.map(finish,stations))


def main(args):
    import cv2
    from algorithms.screws import snapshot
    out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    video=args.video.resolve()
    cap=cv2.VideoCapture(str(video))
    assert cap.isOpened()
    fps,count=cap.get(5),int(cap.get(7))
    with video.open('rb') as f:source_hash=hashlib.file_digest(f,'sha256').hexdigest()
    source=dict(path=str(video),width=cap.get(3),height=cap.get(4),frames=count,
                nativeFps=fps,duration=count/fps,sha256=source_hash)
    ok,first=cap.read()
    assert ok
    cap.release()
    frozen=snapshot()
    save(out/'source.json',source)
    save(out/'frozen-model.json',frozen)
    def encode(frame):
        resized=cv2.resize(frame,(1920,1080),interpolation=cv2.INTER_AREA)
        for quality in (82,70,55):
            ok,jpg=cv2.imencode('.jpg',resized,[cv2.IMWRITE_JPEG_QUALITY,quality])
            if ok and len(jpg)<=1024*1024:return jpg.tobytes()
        raise ValueError('JPEG too large')
    # An empty opening frame only warms SIFT/person detection. Exercise the
    # configured calibrated views too, so lazy ROI models do not first load
    # when the workpiece enters the measured video. Use throwaway streams.
    warm_inputs=[('video-first-frame',encode(first))]
    references=dict.fromkeys([frozen['config']['reference']]+[
        profile['reference'] for profile in frozen['config'].get('calibrations',[]) if profile.get('reference')])
    for reference in references:
        frame=cv2.imread(str(ROOT/reference))
        if frame is None:raise ValueError('Cannot read warmup reference: '+reference)
        warm_inputs.append((reference,encode(frame)))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    env=dict(os.environ,APP_ENV='test',COOKIE_SECURE='false',REALTIME_WARMUP='false',
        DATABASE_URL='sqlite:///'+(out/'test.db').as_posix(),DATA_DIR=str(out/'data'),
        APP_USERNAME='board-video-test',APP_PASSWORD=secrets.token_urlsafe(24),
        REALTIME_BACKEND='yolo',POSE_BACKEND='yolo',REALTIME_POOL_SIZE=str(args.pose_pool),SPECIALIST_POOL_SIZE=str(args.specialist_pool),
        STATION_INFERENCE_CONCURRENCY=str(args.concurrency),STATION_QUEUE_WAIT_MS='500',
        OPENCV_THREADS=str(args.opencv_threads),TORCH_THREADS=str(args.torch_threads),
        MODEL_PATH=str(ROOT/'models/yolo11s-pose.pt'),WORKER_ANALYSIS_ENABLED='false',WORKER_TRAINING_ENABLED='false',
        BOARD_VIDEO_BASE=base,BOARD_VIDEO_OUTPUT=str(out),PYTHONUNBUFFERED='1',PYTHONIOENCODING='utf-8')
    save(out/'settings.json',dict(targetFps=args.fps,posePool=args.pose_pool,specialistPool=args.specialist_pool,
        concurrency=args.concurrency,queueWaitMs=500,maxSampleGapSeconds=1,
        opencvThreads=args.opencv_threads,torchThreads=args.torch_threads,
        stationOnly=args.station_only,deadlineMs=args.deadline_ms,
        warmupInputs=[name for name,_ in warm_inputs],
        mode='default independent detection; no workpiece linkage or manual PASS',
        source='one original 4K decode shared across six independent HTTP/model streams'))
    subprocess.run([sys.executable,'-m','backend.init_db'],cwd=ROOT,env=env,check=True)
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    api_log=(out/'api.log').open('wb')
    process=subprocess.Popen([sys.executable,'-m','uvicorn','backend.app:create_app','--factory',
        '--host','127.0.0.1','--port',str(port),*(['--access-log'] if args.access_log else ['--no-access-log'])],cwd=ROOT,env=env,
        stdin=subprocess.DEVNULL,stdout=api_log,stderr=api_log,creationflags=flags)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf=None
    def request(path,method='GET',body=None,headers=None):
        h=dict(headers or {})
        if csrf:h['X-CSRF-Token']=csrf
        if isinstance(body,dict):
            body=json.dumps(body).encode();h['Content-Type']='application/json'
        req=urllib.request.Request(base+'/api/v1'+path,method=method,data=body,headers=h)
        with opener.open(req,timeout=90) as response:return json.load(response)
    stop=threading.Event()
    phase={'name':'startup'}
    def monitor():
        with (out/'gpu.jsonl').open('w',encoding='utf-8') as file:
            while not stop.is_set():
                r=subprocess.run(['nvidia-smi','--query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total',
                    '--format=csv,noheader,nounits'],capture_output=True,text=True,creationflags=flags)
                file.write(json.dumps(dict(at=time.time(),phase=phase['name'],csv=r.stdout.strip()))+'\n');file.flush()
                stop.wait(1)
    monitor_thread=threading.Thread(target=monitor,daemon=True)
    monitor_thread.start()
    browser=None;browser_log=None;stations=[]
    report=dict(source=source,runs=[])
    def control(name,**extra):
        phase['name']=name
        save(out/'control.json',dict(phase=name,at=time.time(),**extra))
    def release(station):
        if station.get('stream'):
            station['client'].request('/station-streams/'+station['stream']['streamId'],'DELETE',
                {'reason':'视频回放实测已结束，释放测试流'})
            station['stream']=None
    try:
        for _ in range(160):
            if process.poll() is not None:raise RuntimeError('API stopped; see api.log')
            try:
                if request('/health')['status']=='ready':break
            except OSError:pass
            time.sleep(.25)
        else:raise RuntimeError('API startup timed out')
        csrf=request('/login','POST',{'username':env['APP_USERNAME'],'password':env['APP_PASSWORD']})['csrf']
        station_count=1 if args.station_only and args.runs=='single' else 6
        for n in range(1,station_count+1):
            binding=request(f'/admin/terminals/station-{n}','PUT',dict(workstationId=f'video-test-pc-{n}',
                cameraId=f'video-161554-{n}',source='rtsp',analysisType='screw',sampleFps=args.fps,maxSampleGapSeconds=1))
            env[f'BOARD_VIDEO_KEY_{n}']=binding['credential']
            stations.append(dict(id=f'station-{n}',client=collector.Client(base,binding['credential']),stream=None))
        # Warm all specialist replicas without creating station archive records.
        warm_streams=[str(uuid.uuid4()) for _ in range(args.specialist_pool)]
        warmups=[];warm_details=[]
        def warm(stream,i,payload):
            began=time.monotonic()
            body=request('/realtime/frame?analysis_type=screw','POST',payload,{'Content-Type':'image/jpeg',
                'X-Stream-Id':stream,'X-Frame-Time':str(i*.25),'X-Frame-Id':f'warm-{i}'})
            inspection=body.get('inspection') or {}
            return dict(latencyMs=(time.monotonic()-began)*1000,registered=inspection.get('registered'),
                calibrationId=inspection.get('calibrationId'),timingsMs=body.get('timingsMs'))
        with ThreadPoolExecutor(max_workers=args.specialist_pool) as pool:
            for image_index,(name,payload) in enumerate(warm_inputs):
                for iteration in range(3):
                    i=image_index*3+iteration
                    details=list(pool.map(lambda stream:warm(stream,i,payload),warm_streams))
                    warmups.append([item['latencyMs'] for item in details])
                    warm_details.append(dict(input=name,iteration=iteration,samples=details))
        for stream in warm_streams:request('/realtime/streams/'+stream,'DELETE')
        save(out/'warmup.json',dict(latencyMs=warmups,details=warm_details,metrics=request('/metrics')))
        control('ready')
        if not args.station_only:
            browser_log=(out/'browser.log').open('wb')
            browser=subprocess.Popen(['node',str(ROOT/'tools/evaluate-board-video.cjs')],cwd=ROOT,env=env,
                stdin=subprocess.DEVNULL,stdout=browser_log,stderr=browser_log,creationflags=flags)
            for _ in range(180):
                if browser.poll() is not None:raise RuntimeError('Browser stopped; see browser.log')
                if (out/'browser-ready.json').exists():break
                time.sleep(.25)
            else:raise RuntimeError('Browser setup timed out')
        profiles = [('single', stations[:1]), ('six', stations)]
        for name,active in profiles:
            if args.runs != 'both' and args.runs != name:
                continue
            for s in active:
                s.update(stream=s['client'].request('/station-streams','POST',{'requestId':str(uuid.uuid4())}),
                    future=None,records=[],skippedBusy=0,skippedLate=0)
                assert 'cycleId' not in s['stream'] and 'product' not in s['stream']
            cap=cv2.VideoCapture(str(video))
            candidates=math.ceil(source['duration']*args.fps)
            origin=time.monotonic();epoch=time.time();decoded=-1;decode_times=[]
            control(name,elapsed=0,origin=epoch,streams=[s['stream'] for s in active])
            print(f'{name}: {len(active)} streams, {source["duration"]:.2f}s at {args.fps}fps',flush=True)
            def send(station,seq,stamp,payload,scheduled,encoded_at):
                identity=station['stream'];began=time.monotonic()
                record=dict(index=seq,sourceTime=stamp,sentAt=began-origin,sentEpoch=time.time(),
                    decodeEncodeMs=(encoded_at-scheduled)*1000)
                try:
                    body=station['client'].request('/station-streams/'+identity['streamId']+'/frames','POST',payload,
                        {'Content-Type':'image/jpeg','X-Frame-Seq':str(seq),'X-Frame-Time':str(stamp),
                         'X-Stream-Epoch':str(identity['streamEpoch']),'X-Station-Id':identity['stationId'],
                         'X-Camera-Id':identity['cameraId']})
                    record.update(status=200,response=body,httpMs=(time.monotonic()-began)*1000)
                    assert 'cycleId' not in body and 'product' not in body
                    if any(body.get(k)!=identity.get(k) for k in ('stationId','streamId','streamEpoch','cameraId')) or body['frameSeq']!=seq:
                        record['identityMismatch']=True
                except collector.ApiError as exc:
                    cause=exc.__context__
                    record.update(status=exc.status,error=str(exc),errorType=type(cause).__name__ if cause else None,
                        errorReasonType=type(getattr(cause,'reason',None)).__name__ if cause else None)
                record.update(completedAt=time.monotonic()-origin,completedEpoch=time.time(),
                    captureToResponseMs=(time.monotonic()-scheduled)*1000,
                    sourceToResponseMs=(time.monotonic()-origin-stamp)*1000)
                return record
            with ThreadPoolExecutor(max_workers=6) as pool:
                for index in range(candidates):
                    due=origin+index/args.fps
                    time.sleep(max(0,due-time.monotonic()))
                    if time.monotonic()-due>=1/args.fps:
                        for s in active:s['skippedLate']+=1
                        continue
                    wanted=min(count-1,math.floor(index/args.fps*fps+1e-6))
                    while decoded+1<wanted:
                        assert cap.grab();decoded+=1
                    ok,frame=cap.read();decoded+=1
                    assert ok and decoded==wanted
                    payload=encode(frame);encoded_at=time.monotonic()
                    decode_times.append((encoded_at-due)*1000)
                    # Rotate submission order to avoid consistently favoring one station.
                    order=active[index%len(active):]+active[:index%len(active)]
                    for s in order:
                        if s['future'] and not s['future'].done():s['skippedBusy']+=1;continue
                        if s['future']:s['records'].append(s['future'].result())
                        s['future']=pool.submit(send,s,index,wanted/fps,payload,due,encoded_at)
                    if index%4==0:control(name,elapsed=index/args.fps,origin=epoch)
                    if index%60==0:print(f'{name}: {index/args.fps:.1f}s; completed {[len(s["records"]) for s in active]}',flush=True)
                time.sleep(max(0,origin+source['duration']-time.monotonic()))
                finals=finish_station_runs(active)
            cap.release()
            result=dict(name=name,streamCount=len(active),sourceSeconds=source['duration'],originEpoch=epoch,
                targetFps=args.fps,deadlineMs=args.deadline_ms,stationOnly=args.station_only,
                decodeAndEncodeMs=stats(decode_times),stations=[])
            for s,final in zip(active,finals):
                records=s['records'];good=[r for r in records if r['status']==200]
                first_pass=next((r for r in good if r['response']['compliance']['status']=='PASS'),None)
                completed=sum(r['completedAt']<=source['duration'] for r in good)
                latency=stats([r['captureToResponseMs'] for r in good])
                completion_times=[0.]+[r['completedAt'] for r in good if r['completedAt']<=source['duration']]+[source['duration']]
                within_deadline=sum(r['captureToResponseMs']<=args.deadline_ms and r['completedAt']<=source['duration'] for r in good)
                row=dict(stationId=s['id'],stream=s['stream'],candidates=candidates,submitted=len(records),
                    completed=len(good),completedWithinVideo=completed,effectiveFps=completed/source['duration'],
                    skippedBusy=s['skippedBusy'],skippedLate=s['skippedLate'],
                    errors={str(status):sum(r['status']==status for r in records) for status in set(r['status'] for r in records) if status!=200},
                    identityMismatches=sum(bool(r.get('identityMismatch')) for r in good),
                    httpMs=stats([r['httpMs'] for r in good]),captureToResponseMs=latency,
                    sourceToResponseMs=stats([r['sourceToResponseMs'] for r in good]),
                    queueWaitMs=stats([r['response']['queueWaitMs'] for r in good]),
                    serverMs=stats([r['response']['serverProcessingMs'] for r in good]),
                    longestNoResultMs=max(b-a for a,b in zip(completion_times,completion_times[1:]))*1000,
                    withinDeadline=within_deadline,deadlineCandidateRate=within_deadline/candidates,
                    targetMet=completed/source['duration']>=args.fps*.95 and latency.get('p95',float('inf'))<=args.deadline_ms,
                    maxSampleGapMs=max([(b['sourceTime']-a['sourceTime'])*1000 for a,b in zip(good,good[1:])] or [0]),
                    firstPassSourceTime=first_pass['sourceTime'] if first_pass else None,
                    firstPassEpoch=first_pass['completedEpoch'] if first_pass else None,
                    liveFailCount=sum(r['response']['compliance']['status']=='FAIL' for r in good),final=final)
                result['stations'].append(row)
                save(out/f'{name}-{s["id"]}-frames.json',records)
                release(s)
                row['archives']=s['client'].request('/stations/'+s['id']+'/inspections?limit=20')
            save(out/f'{name}.json',result);report['runs'].append(result);save(out/'report.json',report)
            control(name+'-finished',elapsed=source['duration'],origin=epoch)
            print(name,'result:',[(r['stationId'],round(r['effectiveFps'],2),r['final'].get('compliance',{}).get('status'),r['errors']) for r in result['stations']],flush=True)
            # Allow Board polling and screenshot capture to show the stopped state.
            if browser:time.sleep(6)
        report['unchangedModelAndConfig']=snapshot()==frozen
        save(out/'metrics.json',request('/metrics'))
        save(out/'scheduler.json',request('/admin/station-metrics'))
        save(out/'report.json',report)
        control('done')
        if browser:
            browser.wait(timeout=30)
            if browser.returncode:raise RuntimeError('Browser failed; see browser.log')
        with sqlite3.connect(out/'test.db') as db:
            rows=db.execute('SELECT cycle_id, context, result, status FROM station_inspections').fetchall()
            validation=dict(workpieceRows=db.execute('SELECT count(*) FROM pilot_cycles').fetchone()[0],
                inspectionRows=len(rows), linkedRows=sum(row[0] is not None for row in rows),
                finishedRows=sum(row[3]=='FINISHED' for row in rows),
                workpieceFields=sum(any(k in json.loads(value) for k in ('cycleId','product'))
                    for row in rows for value in row[1:3] if value))
        save(out/'no-workpiece-verification.json',validation)
        assert validation['workpieceRows']==validation['linkedRows']==validation['workpieceFields']==0, validation
        assert validation['finishedRows']==sum(len(run['stations']) for run in report['runs']), validation
        print('Artifacts:',out,flush=True)
    finally:
        for s in stations:
            try:release(s)
            except Exception:pass
        if browser and browser.poll() is None:browser.terminate();browser.wait(timeout=10)
        if process.poll() is None:process.terminate();process.wait(timeout=20)
        stop.set();monitor_thread.join(timeout=5)
        api_log.close()
        if browser_log:browser_log.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('video',type=Path)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--fps',type=float,default=4)
    p.add_argument('--runs', choices=('single', 'six', 'both'), default='both')
    p.add_argument('--station-only',action='store_true',help='Measure station inference only; do not launch Board/terminal browsers')
    p.add_argument('--deadline-ms',type=float,default=500,help='Response deadline used for realtime acceptance accounting')
    p.add_argument('--concurrency',type=int,default=3,help='Maximum simultaneous station inference requests (1 to 6)')
    p.add_argument('--pose-pool',type=int,default=3,help='Person model replicas (1 to 6); also bounds global concurrency')
    p.add_argument('--specialist-pool',type=int,default=2,help='Screw/sticker replicas per type (1 to 6)')
    p.add_argument('--opencv-threads',type=int,default=4,help='OpenCV CPU threads (1 to 16)')
    p.add_argument('--torch-threads',type=int,default=4,help='PyTorch CPU threads (1 to 16)')
    p.add_argument('--access-log',action='store_true',help='Save API HTTP access records for transport diagnosis')
    args=p.parse_args()
    if not .5<=args.fps<=10:p.error('--fps must be 0.5 to 10')
    if not 1<=args.deadline_ms<=30000:p.error('--deadline-ms must be 1 to 30000')
    if not all(1<=n<=6 for n in (args.concurrency,args.pose_pool,args.specialist_pool)):
        p.error('concurrency and model pools must be 1 to 6')
    if args.specialist_pool>args.pose_pool:
        p.error('--pose-pool must be at least --specialist-pool so all replicas can be warmed concurrently')
    if not all(1<=n<=16 for n in (args.opencv_threads,args.torch_threads)):
        p.error('OpenCV and PyTorch threads must be 1 to 16')
    main(args)
