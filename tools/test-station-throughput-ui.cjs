// Real browser replay/capture/JPEG/HTTP/model probe; no routes, mocks, or product overrides.
// Run with the disposable API/database launcher:
// node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-throughput-ui.cjs
// Optional: STATION_THROUGHPUT_SECONDS (default 30), STATION_THROUGHPUT_VIDEO,
// STATION_THROUGHPUT_FPS (comma-separated targets, default 4,6).
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {resolvePython} = require('./python.cjs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE,
  '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root = path.resolve(__dirname, '..'), base = process.env.PILOT_TEST_BASE;
const sourceRoot = path.resolve(process.env.PILOT_TEST_SOURCE_ROOT || root);
const output = process.env.PILOT_TEST_OUTPUT, seconds = Number(process.env.STATION_THROUGHPUT_SECONDS || 30);
const video = path.resolve(process.env.STATION_THROUGHPUT_VIDEO || 'C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4');
const targets = (process.env.STATION_THROUGHPUT_FPS || '4,6').split(',').map(Number);
if (!base || !output || process.env.APP_ENV !== 'test' || new URL(base).hostname !== '127.0.0.1' ||
    !path.resolve(output).startsWith(path.join(root, 'test-results', 'pilot-browser-tests') + path.sep)) {
  throw new Error('Use test-pilot-ui.py --registration-paused --browser-script tools/test-station-throughput-ui.cjs');
}
assert.ok(Number.isFinite(seconds) && seconds >= 1 && seconds <= 120, 'Probe duration must be 1–120 seconds per round');
assert.ok(targets.length>=1 && targets.length<=6 && targets.every(fps=>Number.isFinite(fps)&&fps>=.5&&fps<=30) &&
  new Set(targets).size===targets.length, 'Probe FPS targets must be 1–6 distinct values from 0.5 to 30');
assert.ok(fs.existsSync(video), 'Replay video is missing');
const identityFields = ['stationId','workstationId','cameraId','streamId','streamEpoch','revision','analysisType','sceneId','modelRevision'];
const pick = (value, keys) => Object.fromEntries(keys.filter(key => value?.[key] !== undefined).map(key => [key,value[key]]));
const jsonFile = (name, value) => fs.writeFileSync(path.join(output,name),JSON.stringify(value,null,2));
const digestFile = async file => {
  const hash = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
};
function stats(values) {
  const data = values.filter(Number.isFinite).sort((a,b) => a-b), round = v => Math.round(v*1000)/1000;
  if (!data.length) return {count:0,medianMs:null,p95Ms:null,maxMs:null};
  const n=data.length, median=n%2 ? data[(n-1)/2] : (data[n/2-1]+data[n/2])/2;
  return {count:n,medianMs:round(median),p95Ms:round(data[Math.ceil(n*.95)-1]),maxMs:round(data.at(-1))};
}

(async () => {
  const report = {passed:false,source:'Real Edge browser replay, video decode, canvas capture/scale, JPEG encoding, HTTP and configured CUDA models',
    sourceRoot,probeRoot:root,
    requestedSecondsPerRound:seconds,targets,stationId:'station-2',analysisType:'sticker',rounds:[],warmup:[],pageErrors:[],instrumentationErrors:[],
    measurement:'Actual browser running interval, from StationDetection.state.started to observed running=false; warmup, model load and archive time excluded. UI accepted results and successful HTTP responses within this interval are counted separately.',
    limitations:'One development video, one browser, sequential requested FPS targets; no accuracy/generalization claim. Read-only MutationObserver and one live screenshot per round add uncorrected probe overhead. Target attainment is reported, not required for probe validity.'};
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on('pageerror', error => report.pageErrors.push({message:error.message,atEpochMs:Date.now()}));
  let csrf, currentRound = null, warmStream = null;
  // An external GPU monitor should collect timestamped samples continuously and
  // select only these browser intervals. Startup/warmup/archive are excluded.
  const publishMeasurementWindows = () => jsonFile('throughput-measurement-windows.json', {
    sourceRoot,updatedAtEpochMs:Date.now(),clock:'Unix milliseconds from browser performance.timeOrigin + performance.now',
    policy:'Include GPU samples only from measurementStartedAtEpochMs through measurementStoppedAtEpochMs; no warmup or archive samples.',
    windows:report.rounds.filter(round=>round.measurementStartedAtEpochMs!==undefined).map(round=>({
      targetFps:round.targetFps,streamId:round.identity?.streamId,
      measurementStartedAtEpochMs:round.measurementStartedAtEpochMs,
      measurementStoppedAtEpochMs:round.measurementStoppedAtEpochMs ?? null,
    })),
  });
  const networkRows = new Map(), inFlight = new Set(), pendingBodies = new Set();
  const isFrame = request => request.method() === 'POST' && /^\/api\/v1\/station-streams\/[^/]+\/frames$/.test(new URL(request.url()).pathname);
  const finishNetwork = request => {
    const row = networkRows.get(request);
    if (!row) return;
    if (inFlight.delete(request)) row.finishedObservedAtEpochMs = Date.now();
    const timing = request.timing();
    row.startedAtEpochMs = timing.startTime;
    row.finishedAtEpochMs = timing.responseEnd >= 0 ? timing.startTime+timing.responseEnd : row.finishedObservedAtEpochMs;
    row.networkMs = timing.responseEnd >= 0 ? timing.responseEnd : null;
  };
  page.on('request', request => {
    if (!isFrame(request) || !currentRound) return;
    const headers = request.headers();
    const row = {roundTargetFps:currentRound.targetFps,streamId:new URL(request.url()).pathname.split('/').at(-2),
      stationId:headers['x-station-id'],cameraId:headers['x-camera-id'],streamEpoch:Number(headers['x-stream-epoch']),
      frameSeq:Number(headers['x-frame-seq']),frameTime:Number(headers['x-frame-time']),
      jpegBytes:request.postDataBuffer()?.length ?? null,startedObservedAtEpochMs:Date.now()};
    networkRows.set(request,row); currentRound.requests.push(row); inFlight.add(request);
    currentRound.maxInFlight = Math.max(currentRound.maxInFlight,inFlight.size);
  });
  page.on('requestfinished', finishNetwork);
  page.on('requestfailed', request => {
    const row=networkRows.get(request);
    if (row) {row.networkError=request.failure()?.errorText || 'request failed';finishNetwork(request);}
  });
  page.on('response', response => {
    const row=networkRows.get(response.request());
    if (!row) return;
    const task=(async () => {
      row.httpStatus=response.status();
      const result=await response.json();
      row.response={...pick(result,identityFields),frameSeq:result.frameSeq,serverProcessingMs:result.serverProcessingMs,
        queueWaitMs:result.queueWaitMs,timingsMs:result.timingsMs,serviceTimingsMs:result.serviceTimingsMs,
        compliance:result.compliance,registered:result.inspection?.registered,
        inspectionTimingsMs:result.inspection?.timingsMs,
        message:response.ok() ? undefined : result.message || result.detail};
    })().catch(error => {row.responseReadError=error.message;report.instrumentationErrors.push(error.message);});
    pendingBodies.add(task);void task.finally(() => pendingBodies.delete(task));
  });
  async function api(route,method='GET',data,timeout=120000) {
    const response=await context.request.fetch(base+'/api/v1'+route,{method,data,timeout,
      headers:csrf ? {'X-CSRF-Token':csrf} : {}});
    assert.ok(response.ok(),`${method} ${route} returned HTTP ${response.status()}: ${(await response.text()).slice(0,300)}`);
    return response.json();
  }
  const bindingBody = binding => pick(binding,['workstationId','cameraId','source','analysisType','sceneId','sampleFps','maxSampleGapSeconds','previewUrl','enabled','revision']);
  const release = stream => api('/station-streams/'+stream.streamId,'DELETE',{reason:'Disposable browser throughput probe finished'});
  async function warmup(binding) {
    const config=JSON.parse(fs.readFileSync(path.join(sourceRoot,'config/sticker-inspection.json'),'utf8'));
    const reference=path.resolve(sourceRoot,config.reference), jpeg=path.join(output,'throughput-warmup-reference.jpg');
    // Preparation is CPU-only and outside all measured intervals. Use the UI's
    // actual pixel/long-edge budget and JPEG quality, without modifying assets.
    const source = [
      'import cv2, math, sys',
      'image=cv2.imread(sys.argv[1]); assert image is not None',
      'h,w=image.shape[:2]; scale=min(1.,1920/max(w,h),math.sqrt(1920*1080/(w*h)))',
      'size=(max(1,math.floor(w*scale)),max(1,math.floor(h*scale)))',
      'image=cv2.resize(image,size,interpolation=cv2.INTER_AREA) if size!=(w,h) else image',
      "ok,data=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,82]); assert ok and data.size<=1048576",
      "open(sys.argv[2],'wb').write(data.tobytes())"
    ].join('\n');
    const prepared=spawnSync(resolvePython(),['-c',source,reference,jpeg],{cwd:root,encoding:'utf8',windowsHide:true});
    assert.equal(prepared.status,0,prepared.stderr || 'Warmup image preparation failed');
    const payload=fs.readFileSync(jpeg);
    warmStream=await api('/station-streams?station=station-2','POST',{requestId:crypto.randomUUID(),inputSource:'image'});
    try {
      for(let seq=0;seq<3;seq++) {
        const began=performance.now();
        const response=await context.request.post(base+'/api/v1/station-streams/'+warmStream.streamId+'/frames',{
          data:payload,timeout:120000,headers:{'X-CSRF-Token':csrf,'Content-Type':'image/jpeg',
            'X-Frame-Seq':String(seq),'X-Frame-Time':String(seq/binding.sampleFps),
            'X-Stream-Epoch':String(warmStream.streamEpoch),'X-Station-Id':warmStream.stationId,'X-Camera-Id':warmStream.cameraId}});
        assert.ok(response.ok(),`Warmup frame ${seq}: HTTP ${response.status()}`);
        const result=await response.json();
        report.warmup.push({identity:pick(result,identityFields),frameSeq:result.frameSeq,httpMs:performance.now()-began,
          serverMs:result.serverProcessingMs,registered:result.inspection?.registered,excludedFromMeasurement:true});
        for(const key of identityFields) assert.deepEqual(result[key],warmStream[key],`Warmup identity ${key}`);
      }
    } finally {await release(warmStream);warmStream=null;}
  }
  async function installObserver() {
    await page.evaluate(() => {
      const state=window.__stationThroughput={frames:[],events:[],identity:null,startedAt:null,stoppedAt:null,
        stopReason:null,lastRunning:false,manualFinishAt:null,manualStopAt:null,videoEndedAt:null,
        longTasks:[],videoGaps:[],lastVideoFrame:null,sourceExposure:[]};
      const video=document.querySelector('#sd-video'),surface=document.querySelector('.station-detection-media');
      for(const name of ['waiting','stalled','playing','pause','seeking','seeked','error'])video.addEventListener(name,()=>{
        if(StationDetection.state.running)state.events.push({event:name,at:performance.now(),mediaTime:video.currentTime,readyState:video.readyState});
      });
      document.addEventListener('visibilitychange',()=>state.events.push({event:'visibility',hidden:document.hidden,at:performance.now()}));
      if(PerformanceObserver.supportedEntryTypes.includes('longtask'))new PerformanceObserver(list=>{
        if(StationDetection.state.running)for(const task of list.getEntries())state.longTasks.push({at:task.startTime,duration:task.duration});
      }).observe({type:'longtask'});
      const readVideo=(at,metadata)=>{
        if(StationDetection.state.running){
          const previous=state.lastVideoFrame;
          if(previous && (at-previous.at>500 || metadata.mediaTime-previous.mediaTime>.5))
            state.videoGaps.push({at,mediaTime:metadata.mediaTime,elapsedMs:at-previous.at,mediaSeconds:metadata.mediaTime-previous.mediaTime});
          state.lastVideoFrame={at,mediaTime:metadata.mediaTime};
          if(surface.dataset.preview!=='detection'&&StationDetection.state.previewMode==='detection')state.sourceExposure.push({at,mediaTime:metadata.mediaTime});
        }
        video.requestVideoFrameCallback(readVideo);
      };
      video.requestVideoFrameCallback(readVideo);
      const identityKeys=['stationId','workstationId','cameraId','streamId','streamEpoch','revision','analysisType','sceneId','modelRevision'];
      const select = value => Object.fromEntries(identityKeys.map(key=>[key,value?.[key]]));
      const seen=new Set();
      const read=() => {
        const s=StationDetection.state,now=performance.now(),frame=s.frame;
        if(s.stream && !state.identity)state.identity=select(s.stream);
        if(s.started && state.startedAt===null)state.startedAt=s.started;
        const key=frame?.streamId+':'+frame?.frameSeq;
        if(frame && !seen.has(key)) {
          seen.add(key);
          state.frames.push({...select(frame),frameSeq:frame.frameSeq,completedAt:s.lastAt,sampledAt:s.lastAt-s.latency,
            frameTime:frame.frameTime,counters:{...s.counters},
            sampledAtEpochMs:performance.timeOrigin+s.lastAt-s.latency,completedAtEpochMs:performance.timeOrigin+s.lastAt,
            latencyMs:s.latency,...s.timing,registered:frame.inspection?.registered,compliance:frame.compliance,
            serviceTimingsMs:frame.serviceTimingsMs,queueWaitMs:frame.queueWaitMs});
        }
        if(state.lastRunning && !s.running && state.stoppedAt===null) {
          state.stoppedAt=now;
          state.stopReason=state.manualFinishAt!==null || state.manualStopAt!==null ? 'test_manual_stop' :
            state.videoEndedAt!==null ? 'video_natural_end' : ['PASS','FAIL'].includes(frame?.compliance?.status) ? 'automatic_verdict' : 'ui_error_or_unexpected_stop';
          state.events.push({event:'sampler_stopped',at:now,reason:state.stopReason,error:s.error || null});
        }
        state.lastRunning=s.running;
      };
      for(const [selector,key,event] of [['#sd-finish','manualFinishAt','test_finish_click'],['#sd-stop','manualStopAt','test_stop_click']]) {
        document.querySelector(selector).addEventListener('click',() => {
          if(StationDetection.state.running) {state[key]=performance.now();state.events.push({event,at:state[key]});}
        },true);
      }
      document.querySelector('#sd-video').addEventListener('ended',() => {
        state.videoEndedAt=performance.now();state.events.push({event:'video_ended',at:state.videoEndedAt});
      },true);
      new MutationObserver(read).observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true});
      window.__readStationThroughput=read;
      read();
    });
  }
  try {
    csrf=(await api('/login','POST',{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD})).csrf;
    assert.equal((await api('/admin/terminals')).items.length,0,'Probe requires the launcher’s fresh disposable database');
    report.browserVersion=browser.version();
    report.video={path:video,bytes:fs.statSync(video).size,sha256:await digestFile(video)};
    report.configSha256Before=await digestFile(path.join(sourceRoot,'config/sticker-inspection.json'));
    report.sourceSha256={};
    for(const file of ['frontend/station-detection.js','frontend/station-frame-worker.js',
        'algorithms/stickers.py','algorithms/screws.py','algorithms/scene_runtime.py',
        'backend/realtime.py','backend/screw_realtime.py','backend/sticker_realtime.py','backend/specialist_pool.py']) {
      report.sourceSha256[file]=await digestFile(path.join(sourceRoot,file));
    }
    report.runtimeBefore=pick(await api('/metrics'),['inferenceRuntime','realtime','specialistPools']);
    let binding=(await api('/admin/terminals/station-2','PUT',{
      workstationId:'throughput-ui-pc-2',cameraId:'throughput-ui-camera-2',source:'usb',analysisType:'sticker',
      sampleFps:targets[0],maxSampleGapSeconds:1,enabled:true,revision:0})).binding;
    await warmup(binding);
    for(const fps of targets) {
      if(binding.sampleFps!==fps)binding=(await api('/admin/terminals/station-2','PUT',{...bindingBody(binding),sampleFps:fps})).binding;
      const round={targetFps:fps,binding:bindingBody(binding),requests:[],maxInFlight:0,requestedSeconds:seconds};
      report.rounds.push(round);currentRound=round;
      await page.goto(base+'/?task=station2');
      await page.waitForFunction(fps => window.StationDetection?.state.connected && !StationDetection.state.busy &&
        StationDetection.state.binding?.analysisType==='sticker' && StationDetection.state.binding.sampleFps===fps,fps);
      await page.locator('#sd-source').selectOption('replay');
      await page.locator('#sd-file').setInputFiles(video);
      await page.waitForFunction(() => StationDetection.state.ready);
      round.videoMetadata=await page.locator('#sd-video').evaluate(v => ({width:v.videoWidth,height:v.videoHeight,
        durationSeconds:v.duration,playbackRate:v.playbackRate}));
      assert.equal(round.videoMetadata.playbackRate,1,'Replay must run at natural speed');
      await installObserver();
      await page.locator('#sd-start').click();
      await page.waitForFunction(() => window.__stationThroughput.startedAt!==null);
      Object.assign(round,await page.evaluate(()=>({identity:__stationThroughput.identity,
        measurementStartedAtEpochMs:performance.timeOrigin+__stationThroughput.startedAt})));
      publishMeasurementWindows();
      let liveScreenshot=false;
      while(true) {
        const progress=await page.evaluate(() => {
          __readStationThroughput();const p=__stationThroughput,s=StationDetection.state;
          return {elapsed:(performance.now()-p.startedAt)/1000,stopped:p.stoppedAt!==null,
            measurementStoppedAtEpochMs:p.stoppedAt===null?null:performance.timeOrigin+p.stoppedAt,
            frames:s.frames,running:s.running,busy:s.busy,error:s.error};
        });
        if(progress.stopped) {round.measurementStoppedAtEpochMs=progress.measurementStoppedAtEpochMs;publishMeasurementWindows();break;}
        if(!liveScreenshot && progress.frames>0 && progress.elapsed>=Math.min(5,seconds/3)) {
          await page.screenshot({path:path.join(output,`throughput-${fps}fps-live.png`),fullPage:true});
          liveScreenshot=true;
        }
        if(progress.elapsed>=seconds && !progress.busy) {
          if(progress.frames>0)await page.locator('#sd-finish').click();
          else await page.locator('#sd-stop').click();
        }
        if(progress.elapsed>seconds+30)throw new Error(`Target ${fps}: sampler did not stop after time limit`);
        await page.waitForTimeout(100);
      }
      await page.waitForFunction(() => !StationDetection.state.stream && !StationDetection.state.busy,null,{timeout:60000});
      const countAfterRelease=round.requests.length;
      await page.waitForTimeout(1200);
      await Promise.all([...pendingBodies]);
      const browserData=await page.evaluate(() => {
        __readStationThroughput();const s=StationDetection.state,v=document.querySelector('#sd-video');
        return {probe:__stationThroughput,timeOrigin:performance.timeOrigin,observedAt:performance.now(),
          final:s.final,archived:s.archived,frames:s.frames,error:s.error,stream:s.stream,running:s.running,
          video:{ended:v.ended,currentTime:v.currentTime,duration:v.duration,paused:v.paused},
          metricFps:document.querySelector('#metric-fps').textContent};
      });
      const p=browserData.probe;
      round.identity=p.identity;round.stopReason=p.stopReason;round.browser=browserData;
      round.actualRunningSeconds=(p.stoppedAt-p.startedAt)/1000;
      round.measurementStartedAtEpochMs=browserData.timeOrigin+p.startedAt;
      round.measurementStoppedAtEpochMs=browserData.timeOrigin+p.stoppedAt;
      publishMeasurementWindows();
      round.archiveAndQuietObservationMs=browserData.observedAt-p.stoppedAt;
      round.uiResultsWithinWindow=p.frames.filter(r=>r.completedAt>=p.startedAt && r.completedAt<=p.stoppedAt).length;
      round.successfulHttp=round.requests.filter(r=>r.httpStatus>=200 && r.httpStatus<300 && !r.networkError && !r.responseReadError).length;
      round.successfulHttpWithinWindow=round.requests.filter(r=>r.httpStatus>=200 && r.httpStatus<300 && !r.networkError && !r.responseReadError &&
        r.finishedAtEpochMs>=round.measurementStartedAtEpochMs && r.finishedAtEpochMs<=round.measurementStoppedAtEpochMs).length;
      round.effectiveFps=round.uiResultsWithinWindow/round.actualRunningSeconds;
      round.successfulHttpFps=round.successfulHttpWithinWindow/round.actualRunningSeconds;
      round.registeredResultsWithinWindow=p.frames.filter(r=>r.completedAt>=p.startedAt && r.completedAt<=p.stoppedAt && r.registered===true).length;
      round.registeredFps=round.registeredResultsWithinWindow/round.actualRunningSeconds;
      round.targetMet=round.effectiveFps>=fps*.95;
      round.timing=Object.fromEntries(['drawMs','encodeMs','queueMs','requestMs','serverMs','latencyMs','queueWaitMs'].map(key=>[key,stats(p.frames.map(r=>r[key]))]));
      round.captureOverlappedPreviousResult=p.frames.slice(1).filter((frame,index)=>frame.sampledAt<p.frames[index].completedAt).length;
      round.clientIdleBetweenUploadsMs=stats(round.requests.slice(1).map((row,index)=>row.startedAtEpochMs-round.requests[index].finishedAtEpochMs));
      const stages=[...new Set(p.frames.flatMap(r=>Object.keys(r.serviceTimingsMs || {})))];
      round.serviceTiming=Object.fromEntries(stages.map(key=>[key,stats(p.frames.map(r=>r.serviceTimingsMs?.[key]))]));
      round.identityMismatches=[];
      for(const row of round.requests) {
        if(row.response && row.httpStatus>=200 && row.httpStatus<300) {
          for(const key of identityFields)if(JSON.stringify(row.response[key])!==JSON.stringify(p.identity?.[key]))round.identityMismatches.push({frameSeq:row.frameSeq,key});
          for(const key of ['streamId','stationId','cameraId','streamEpoch','frameSeq'])if(row.response[key]!==row[key])round.identityMismatches.push({frameSeq:row.frameSeq,key:'request.'+key});
        }
      }
      round.requestsAfterSamplerStop=round.requests.filter(r=>r.startedAtEpochMs>round.measurementStoppedAtEpochMs+5).length;
      round.noMoreUploadsAfterRelease=round.requests.length===countAfterRelease;
      round.httpErrors=round.requests.filter(r=>!(r.httpStatus>=200 && r.httpStatus<300) || r.networkError || r.responseReadError);
      await page.screenshot({path:path.join(output,`throughput-${fps}fps-ended.png`),fullPage:true});
      jsonFile(`throughput-${fps}fps.json`,round);
      fs.writeFileSync(path.join(output,`throughput-${fps}fps-frames.jsonl`),p.frames.map(frame=>JSON.stringify(frame)).join('\n')+'\n');
      assert.ok(round.actualRunningSeconds>0 && round.uiResultsWithinWindow>0,'Round must contain real accepted results');
      assert.equal(round.maxInFlight,1,'Browser must have at most one frame request in flight');
      assert.equal(inFlight.size,0,'No frame request may remain after stream release');
      assert.deepEqual(round.identityMismatches,[],'Result model/stream/frame identity must match');
      assert.deepEqual(round.httpErrors,[],'Unexpected frame request errors');
      assert.equal(round.requestsAfterSamplerStop,0,'Sampling must not restart after stop');
      assert.equal(round.noMoreUploadsAfterRelease,true,'Uploads must remain stopped after release');
      assert.ok(['test_manual_stop','video_natural_end','automatic_verdict'].includes(round.stopReason),'Unexpected UI termination');
      assert.equal(browserData.video.paused,true,'Replay must be paused at completion');
      assert.equal(browserData.archived,true,'A measured round with results must be archived through the UI');
      assert.deepEqual(report.pageErrors,[],'Browser pageerror');
      console.log(JSON.stringify({targetFps:fps,effectiveFps:round.effectiveFps,registeredFps:round.registeredFps,
        runningSeconds:round.actualRunningSeconds,requests:round.requests.length,successfulHttp:round.successfulHttp,
        uiResultsWithinWindow:round.uiResultsWithinWindow,stopReason:round.stopReason,verdict:browserData.final?.compliance?.status}));
      currentRound=null;
    }
    assert.equal(new Set(report.rounds.map(round=>round.identity.streamId)).size,report.rounds.length,'Rounds must use independent streams');
    const revisions=new Set([...report.warmup.map(r=>r.identity.modelRevision),...report.rounds.map(r=>r.identity.modelRevision)]);
    assert.equal(revisions.size,1,'Warmup and both runs must use the same model revision');
    report.configSha256After=await digestFile(path.join(sourceRoot,'config/sticker-inspection.json'));
    assert.equal(report.configSha256After,report.configSha256Before,'Model config must not change during this probe');
    report.runtimeAfter=pick(await api('/metrics'),['inferenceRuntime','realtime','specialistPools']);
    report.schedulerAfter=await api('/admin/station-metrics');
    assert.deepEqual(report.instrumentationErrors,[]);
    report.passed=true;
  } catch(error) {
    report.failure={message:error.message,stack:error.stack};
    await page.screenshot({path:path.join(output,'throughput-failure.png'),fullPage:true}).catch(()=>{});
    throw error;
  } finally {
    if(warmStream)await release(warmStream).catch(()=>{});
    if(currentRound) {
      currentRound.partialBrowser=await page.evaluate(() => ({probe:window.__stationThroughput,
        timeOrigin:performance.timeOrigin,error:window.StationDetection?.state.error,running:window.StationDetection?.state.running})).catch(()=>null);
      if(currentRound.partialBrowser?.probe?.stoppedAt!=null)
        currentRound.measurementStoppedAtEpochMs=currentRound.partialBrowser.timeOrigin+currentRound.partialBrowser.probe.stoppedAt;
      const stream=await page.evaluate(() => window.StationDetection?.state.stream).catch(()=>null);
      // The page's own stop lifecycle waits for its pending request before release.
      if(stream) {
        await page.locator('#sd-stop').click({timeout:10000}).catch(()=>{});
        await page.waitForFunction(() => !StationDetection.state.stream,null,{timeout:30000}).catch(()=>{});
      }
    }
    if(csrf) {
      try {report.temporarySessionLoggedOut=Boolean((await api('/logout','POST',{})).ok);}
      catch {report.temporarySessionLoggedOut=false;}
    }
    jsonFile('station-throughput-ui.json',report);
    publishMeasurementWindows();
    await browser.close();
    console.log('Real browser throughput artifacts: '+output);
  }
})().catch(error => {console.error(error);process.exitCode=1;});
