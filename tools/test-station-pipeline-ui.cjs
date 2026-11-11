// Real capture/JPEG/dashboard lifecycle with delayed model responses as UI fixtures.
// No GPU inference; run with test-pilot-ui.py --registration-paused.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE,
  '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE, output=process.env.PILOT_TEST_OUTPUT;
if (!base || !output || process.env.APP_ENV!=='test') throw new Error('Use the disposable test-pilot-ui.py launcher');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true,args:['--disable-gpu']});
  const context=await browser.newContext({viewport:{width:1440,height:1000}}), report={passed:false,inferenceMocked:true,archiveMocked:true,rounds:[]};
  try {
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    const session=await login.json(); assert.equal(login.status(),200);
    assert.equal((await (await context.request.get(base+'/api/v1/admin/terminals')).json()).items.length,0);
    const saved=await context.request.put(base+'/api/v1/admin/terminals/station-1',{headers:{'X-CSRF-Token':session.csrf},data:{
      workstationId:'pipeline-fixture-pc',cameraId:'pipeline-fixture-camera',analysisType:'screw',sampleFps:20,
      maxSampleGapSeconds:1,source:'usb',enabled:true,revision:0}});
    assert.equal(saved.status(),200);
    for (const scenario of [
      {name:'worker-stop',mode:'stop',worker:true}, {name:'worker-finish',mode:'finish',worker:true},
      {name:'worker-auto-pass',mode:'pass',worker:true}, {name:'worker-backpressure',mode:'busy',worker:true},
      {name:'worker-wrong-identity',mode:'wrong',worker:true}, {name:'canvas-fallback',mode:'stop',worker:false},
      {name:'worker-wrong-workstation',mode:'wrong',worker:true,wrongField:'workstationId'},
      {name:'worker-wrong-revision',mode:'wrong',worker:true,wrongField:'revision'},
      {name:'worker-wrong-model',mode:'wrong',worker:true,wrongField:'modelRevision'},
      {name:'late-pass-is-not-current',mode:'late',worker:true,responseDelay:1100},
      {name:'freshness-expires-from-capture',mode:'expiry',worker:true,responseDelay:600},
      {name:'hidden-response-after-resume',mode:'boundary',boundary:'hidden',worker:true},
      {name:'offline-response-after-resume',mode:'boundary',boundary:'offline',worker:true},
      {name:'transport-recovery-requires-new-sample',mode:'transport',worker:true},
      {name:'transport-recovery-rejects-changed-lease',mode:'transport',changedLease:true,worker:true},
      {name:'paused-response-after-resume',mode:'boundary',boundary:'pause',worker:true},
      {name:'camera-ended-with-pending-pass',mode:'camera-ended',worker:true},
      {name:'live-preview-without-boxes',mode:'live-preview',worker:true},
      {name:'stopped-fps-excludes-drained-response',mode:'frozen-fps',worker:true},
      {name:'archive-must-confirm-persistence',mode:'archive-error',archiveFault:'unconfirmed',worker:true},
      {name:'archive-must-match-stream',mode:'archive-error',archiveFault:'identity',worker:true},
      {name:'worker-slow-encode-expiry',mode:'stop',worker:true,encodeDelay:650},
      {name:'worker-stop-during-encode',mode:'early-stop',worker:true,encodeDelay:650},
      {name:'worker-stop-during-bitmap',mode:'early-stop',worker:true,bitmapDelay:650},
      {name:'fallback-stop-during-encode',mode:'early-stop',worker:false,encodeDelay:650}
    ]) {
      const page=await context.newPage(), errors=[], active=new Set();
      const round={...scenario,requests:[],maxInFlight:0,finishes:0,releases:0};report.rounds.push(round);
      let identity, latest, releaseHeld=null;
      page.on('pageerror',error=>errors.push(error.message));
      const isFrame=request=>request.method()==='POST' && /\/station-streams\/[^/]+\/frames$/.test(new URL(request.url()).pathname);
      page.on('request',request=>{if(isFrame(request)){active.add(request);round.maxInFlight=Math.max(round.maxInFlight,active.size);}});
      page.on('requestfinished',request=>active.delete(request));page.on('requestfailed',request=>active.delete(request));
      await page.addInitScript(({worker,bitmapDelay,encodeDelay})=>{
        window.__pipeline={captures:[],overlays:[],tracks:[],bitmaps:[],workers:0,closedWorkers:0};
        const createBitmap=window.createImageBitmap;
        window.createImageBitmap=async(...args)=>{
          const bitmap=await createBitmap(...args);__pipeline.bitmaps.push(bitmap);
          if(bitmapDelay)await new Promise(resolve=>setTimeout(resolve,bitmapDelay));return bitmap;
        };
        if (!worker) window.Worker=undefined;
        else {
          const NativeWorker=window.Worker;
          window.Worker=class extends NativeWorker {
            constructor(...args){super(...args);__pipeline.workers++;
              this.addEventListener('message',({data})=>{if(data.bitmap)__pipeline.bitmaps.push(data.bitmap);});}
            terminate(){__pipeline.closedWorkers++;return super.terminate();}
          };
        }
        if(!worker && encodeDelay) {
          const toBlob=HTMLCanvasElement.prototype.toBlob;let first=true;
          HTMLCanvasElement.prototype.toBlob=function(callback,...args){
            const wait=first?encodeDelay:0;first=false;return toBlob.call(this,blob=>setTimeout(()=>callback(blob),wait),...args);
          };
        }
        navigator.mediaDevices.getUserMedia=async()=>{
          const source=document.createElement('canvas');source.width=960;source.height=600;
          const ctx=source.getContext('2d');let n=0,stream;
          const paint=()=>{
            ctx.fillStyle=`rgb(${(n*11)%230},${(n*17+35)%230},${(n*23+70)%230})`;
            ctx.fillRect(0,0,source.width,source.height);n++;
            if (!stream || stream.getVideoTracks()[0].readyState==='live') requestAnimationFrame(paint);
          };
          paint();stream=source.captureStream(30);__pipeline.tracks.push(...stream.getTracks());return stream;
        };
        for (const prototype of [CanvasRenderingContext2D.prototype,window.OffscreenCanvasRenderingContext2D?.prototype].filter(Boolean)) {
          const draw=prototype.drawImage;
          prototype.drawImage=function(source,...args){
            const value=draw.call(this,source,...args), state=window.StationDetection?.state;
            if (source?.id==='sd-video' && state?.running) __pipeline.captures.push({seq:state.sequence-1,at:performance.now(),
              pixel:Array.from(this.getImageData(4,4,1,1).data),width:this.canvas.width,height:this.canvas.height,
              pending:Boolean(state.pending)});
            if (this.canvas.id==='sd-overlay') __pipeline.overlays.push({seq:state.frame?.frameSeq,at:performance.now(),
              pixel:Array.from(this.getImageData(4,4,1,1).data),width:this.canvas.width,height:this.canvas.height});
            return value;
          };
        }
      },scenario);
      if(scenario.worker && scenario.encodeDelay)await page.route('**/station-frame-worker.js',async route=>{
        const response=await route.fetch(),source=await response.text();
        const injection=`const originalConvert=OffscreenCanvas.prototype.convertToBlob;let firstEncode=true;
          OffscreenCanvas.prototype.convertToBlob=async function(...args){
            if(firstEncode){firstEncode=false;await new Promise(resolve=>setTimeout(resolve,${scenario.encodeDelay}));}
            return originalConvert.apply(this,args);
          };\n`;
        await route.fulfill({response,body:injection+source});
      });
      await page.route('**/api/v1/station-streams?*',async route=>{
        const response=await route.fetch();assert.equal(response.status(),201);identity=await response.json();
        await route.fulfill({response,json:identity});
      });
      if (scenario.mode==='transport') await page.route('**/api/v1/stations/station-1/live-status',async route=>{
        await new Promise(resolve=>{releaseHeld=resolve;}); releaseHeld=null;
        const status={...identity,state:'LIVE',resultAgeMs:10,result:{...latest,
          compliance:{status:'PASS',label:'断线前旧合规结果',reason:'Must not restore this verdict'}}};
        if (scenario.changedLease) status.streamEpoch++;
        await route.fulfill({json:status});
      });
      await page.route('**/api/v1/station-streams/*/frames',async route=>{
        const headers=route.request().headers(), seq=Number(headers['x-frame-seq']);
        const row={seq,stamp:Number(headers['x-frame-time']),startedAt:Date.now(),bytes:route.request().postDataBuffer().length};
        round.requests.push(row);
        const requestNumber=round.requests.length;
        assert.equal(headers['x-camera-id'],identity.cameraId);assert.equal(headers['x-station-id'],identity.stationId);
        assert.equal(Number(headers['x-stream-epoch']),identity.streamEpoch);assert.ok(row.bytes>0 && row.bytes<=1048576);
        if ((['boundary','camera-ended'].includes(scenario.mode) && requestNumber===1) || (['expiry','frozen-fps'].includes(scenario.mode) && requestNumber===2)) {
          await new Promise(resolve=>{releaseHeld=resolve;}); releaseHeld=null;
        }
        await delay(scenario.responseDelay || 250);
        if (scenario.mode==='transport' && requestNumber===2) {
          row.status=503;row.finishedAt=Date.now();
          await route.fulfill({status:503,json:{message:'中央推理连接中断'}});return;
        }
        if (scenario.mode==='busy' && round.requests.length===1) {
          row.status=429;row.finishedAt=Date.now();
          await route.fulfill({status:429,headers:{'Retry-After':'1'},json:{message:'Deliberate busy fixture'}});return;
        }
        const status=(scenario.mode==='pass' && requestNumber===3) || scenario.mode==='late' ||
          (['boundary','camera-ended'].includes(scenario.mode) && requestNumber===1) ? 'PASS' : 'REVIEW';
        latest={...identity,frameSeq:seq,serverProcessingMs:scenario.responseDelay || 250,people:[],targets:[],
          compliance:{status,label:status==='PASS'?'合规':'待复核',reason:'Delayed UI fixture; no model inference'}};
        row.status=200;row.finishedAt=Date.now();
        await route.fulfill({json:scenario.mode==='wrong'?{...latest,[scenario.wrongField || 'cameraId']:'wrong-identity'}:latest});
      });
      await page.route('**/api/v1/station-streams/*/finish',async route=>{
        round.finishes++;assert.equal(active.size,0,'Archive raced the in-flight upload');
        if (scenario.mode==='archive-error' && round.finishes===1) return route.fulfill({json:{...latest,final:true,
          archived:scenario.archiveFault!=='unconfirmed',streamId:scenario.archiveFault==='identity'?'another-stream':latest.streamId}});
        await route.fulfill({json:{...latest,final:true,archived:true}});
      });
      await page.route('**/api/v1/station-streams/*',async route=>{
        if(route.request().method()!=='DELETE')return route.fallback();
        round.releases++;assert.equal(active.size,0,'Release raced the in-flight upload');await route.continue();
      });
      try {
        await page.goto(base+'/?task=station1');
        await page.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy);
        if (scenario.mode==='live-preview') await page.locator('#sd-preview-mode').selectOption('live');
        await page.locator('#sd-start').click();
        if(scenario.mode==='early-stop') {
          await page.waitForFunction(()=>StationDetection.state.sampling && __pipeline.captures.length>=1);
          round.requestsAtStop=round.requests.length;
          await page.locator('#sd-stop').click();
        } else await page.waitForFunction(()=>StationDetection.state.pending && (StationDetection.state.queued || StationDetection.state.sampling));
        if(scenario.mode==='early-stop') {}
        else if(scenario.mode==='boundary' || scenario.mode==='camera-ended') {
          while (!releaseHeld) await delay(20);
          await page.evaluate(async boundary=>{
            if (boundary==='hidden') {
              Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.__testHidden});
              window.__testHidden=true;document.dispatchEvent(new Event('visibilitychange'));
              window.__testHidden=false;document.dispatchEvent(new Event('visibilitychange'));
            } else if (boundary==='offline') {
              Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>window.__testOnline});
              window.__testOnline=false;window.dispatchEvent(new Event('offline'));
              window.__testOnline=true;window.dispatchEvent(new Event('online'));
            } else if (boundary==='pause') {
              const video=document.querySelector('#sd-video');
              video.pause(); await new Promise(resolve=>video.addEventListener('pause',resolve,{once:true}));
              await video.play();
            } else StationDetection.state.media.getVideoTracks()[0].dispatchEvent(new Event('ended'));
          },scenario.boundary);
          releaseHeld();
          await page.waitForFunction(()=>StationDetection.state.frames>=1);
          assert.equal(round.finishes,0,'A pre-pause PASS caused automatic archival after the input changed');
          assert.equal(await page.locator('#sd-overlay').isHidden(),true,'A pre-pause response restored the old picture');
          if(scenario.mode==='camera-ended') {
            assert.match(await page.evaluate(()=>StationDetection.state.error),/摄像头已断开/);
          } else await page.waitForFunction(()=>StationDetection.state.frames>=2&&!document.querySelector('#sd-overlay').hidden);
          await page.locator('#sd-stop').click();
        }
        else if(scenario.mode==='transport') {
          await page.waitForFunction(()=>StationDetection.state.transportLost);
          while (!releaseHeld) await delay(20);
          assert.equal(await page.locator('#metric-compliance').textContent(),'无法检测');
          assert.equal(await page.locator('#sd-overlay').isHidden(),true);
          const count=round.requests.length;await delay(300);
          assert.equal(round.requests.length,count,'Frames were sent before reconciling the lease');
          releaseHeld();
          if (scenario.changedLease) {
            await page.waitForFunction(()=>!StationDetection.state.running);
            assert.equal(round.requests.length,count,'A changed lease resumed frame submission');
            assert.match(await page.evaluate(()=>StationDetection.state.error),/租约或检测轮次已变化/);
          } else await page.waitForFunction(()=>StationDetection.state.frames>=2&&!StationDetection.state.transportLost);
          assert.equal(round.finishes,0,'A pre-disconnect PASS from status caused automatic archival');
          await page.locator('#sd-stop').click();
        }
        else if(scenario.mode==='late') {
          await page.waitForFunction(()=>StationDetection.state.frames>=2);
          assert.equal(await page.locator('#sd-overlay').isHidden(),true);
          assert.equal(await page.locator('#metric-compliance').textContent(),'等待新结果');
          assert.equal(round.finishes,0,'An already stale PASS automatically finished the round');
          assert.ok(await page.evaluate(()=>StationDetection.state.counters.staleResults>=2));
          await page.locator('#sd-stop').click();
        }
        else if(scenario.mode==='expiry') {
          await page.waitForFunction(()=>StationDetection.state.frames===1&&!document.querySelector('#sd-overlay').hidden);
          round.remainingFreshMs=await page.evaluate(()=>StationDetection.state.resultDeadline-StationDetection.state.lastAt);
          assert.ok(round.remainingFreshMs>0 && round.remainingFreshMs<450,'Freshness was reset to one second on response');
          await page.waitForFunction(()=>StationDetection.state.resultDeadline===0&&document.querySelector('#sd-overlay').hidden);
          assert.equal(await page.evaluate(()=>StationDetection.state.frames),1);
          releaseHeld();await page.locator('#sd-stop').click();
        }
        else if(scenario.mode==='frozen-fps') {
          while (!releaseHeld) await delay(20);
          await page.locator('#sd-stop').click();
          await page.waitForFunction(()=>!StationDetection.state.running&&StationDetection.state.busy);
          round.fpsAtStop=await page.locator('#metric-fps').textContent();
          const framesAtStop=await page.evaluate(()=>StationDetection.state.frames);
          assert.notEqual(round.fpsAtStop,'—');
          releaseHeld();
          await page.waitForFunction(()=>!StationDetection.state.stream&&!StationDetection.state.busy);
          assert.ok(await page.evaluate(frames=>StationDetection.state.frames>frames,framesAtStop),'The held response never drained');
          assert.equal(await page.locator('#metric-fps').textContent(),round.fpsAtStop,'A drained response changed the stopped FPS');
        }
        else if(scenario.mode==='live-preview') {
          await page.waitForFunction(()=>StationDetection.state.frames>=3&&StationDetection.state.previewFps>0 &&
            /视频预览 [\d.]+ fps/.test(document.querySelector('#footer-performance').textContent));
          assert.equal(await page.locator('#sd-overlay').isHidden(),true);
          assert.equal(await page.locator('#sd-video').isVisible(),true);
          assert.match(await page.locator('#footer-performance').textContent(),/视频预览 [\d.]+ fps · 有效检测 [\d.]+ fps/);
          await page.screenshot({path:path.join(output,'live-preview.png'),fullPage:true});
          await page.locator('#sd-preview-mode').selectOption('detection');
          await page.waitForFunction(()=>!document.querySelector('#sd-overlay').hidden);
          await page.screenshot({path:path.join(output,'detection-preview.png'),fullPage:true});
          await page.locator('#sd-stop').click();
        }
        else if(scenario.mode==='archive-error') {
          await page.waitForFunction(()=>StationDetection.state.frames>=2);
          await page.locator('#sd-finish').click();
          await page.waitForFunction(()=>!StationDetection.state.busy&&StationDetection.state.error.includes('保存状态不匹配'));
          assert.equal(round.releases,0,'An unconfirmed/mismatched archive released the producer');
          assert.equal(await page.evaluate(()=>StationDetection.state.archived),false);
          assert.equal(await page.locator('#sd-start').isDisabled(),true);
          await page.locator('#sd-finish').click();
        }
        else if(scenario.mode==='pass') await page.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream&&!StationDetection.state.busy);
        else if(scenario.mode==='wrong') {
          await page.waitForFunction(()=>!StationDetection.state.running&&StationDetection.state.error.includes('身份不匹配'));
          await page.locator('#sd-stop').click();
        } else {
          await page.waitForFunction(()=>StationDetection.state.frames>=3&&StationDetection.state.pending&&StationDetection.state.queued);
          round.requestsAtStop=round.requests.length;
          await page.locator(scenario.mode==='finish'?'#sd-finish':'#sd-stop').click();
        }
        await page.waitForFunction(()=>!StationDetection.state.stream&&!StationDetection.state.busy);
        const count=round.requests.length;await page.waitForTimeout(650);
        assert.equal(round.requests.length,count,'A stopped sampler submitted another frame');
        if(scenario.mode==='frozen-fps') {
          await page.evaluate(()=>StationDetection.render());
          assert.equal(await page.locator('#metric-fps').textContent(),round.fpsAtStop,'Stopped FPS decayed after time passed');
        }
        if(round.requestsAtStop!==undefined)assert.equal(round.requests.length,round.requestsAtStop,'A prepared frame was submitted after stop');
        round.browser=await page.evaluate(()=>({captures:__pipeline.captures,overlays:__pipeline.overlays,
          workers:__pipeline.workers,closedWorkers:__pipeline.closedWorkers,
          retainedOpenBitmaps:__pipeline.bitmaps.filter(bitmap=>bitmap.width>0||bitmap.height>0).length,
          tracksEnded:__pipeline.tracks.every(track=>track.readyState==='ended'),
          pending:Boolean(StationDetection.state.pending),sampling:Boolean(StationDetection.state.sampling),
          queued:Boolean(StationDetection.state.queued),timing:StationDetection.state.timing,
          archived:StationDetection.state.archived,frames:StationDetection.state.frames}));
        assert.equal(round.maxInFlight,scenario.mode==='early-stop'?0:1);assert.equal(active.size,0);
        if(scenario.mode!=='early-stop')assert.ok(round.browser.captures.some(frame=>frame.pending),'Capture never overlapped the previous HTTP request');
        assert.ok(round.browser.captures.length>round.requests.length,'Slow model fixture did not replace prepared samples');
        for(const overlay of round.browser.overlays){
          const capture=round.browser.captures.find(frame=>frame.seq===overlay.seq);
          assert.ok(capture,`Missing captured frame ${overlay.seq}`);
          assert.deepEqual(overlay.pixel,capture.pixel,`Result ${overlay.seq} was drawn onto a different captured picture`);
          assert.equal(overlay.width,capture.width);assert.equal(overlay.height,capture.height);
        }
        for(let i=1;i<round.requests.length;i++) {
          assert.ok(round.requests[i].seq>round.requests[i-1].seq);assert.ok(round.requests[i].stamp>round.requests[i-1].stamp);
        }
        assert.equal(round.browser.pending,false);assert.equal(round.browser.sampling,false);assert.equal(round.browser.queued,false);
        assert.equal(round.browser.tracksEnded,true);assert.equal(round.browser.closedWorkers,round.browser.workers);
        assert.equal(round.browser.retainedOpenBitmaps,0,'A transferred or retained sample bitmap remained open after stop');
        assert.equal(round.releases,1);assert.deepEqual(errors,[]);
        if(scenario.mode==='pass'){assert.equal(round.requests.length,3);assert.equal(round.finishes,1);assert.equal(round.browser.archived,true);}
        if(scenario.mode==='finish'){assert.equal(round.finishes,1);assert.equal(round.browser.archived,true);}
        if(scenario.mode==='archive-error'){assert.equal(round.finishes,2);assert.equal(round.browser.archived,true);}
        if(scenario.mode==='wrong'){assert.equal(round.requests.length,1);assert.equal(round.browser.frames,0);assert.equal(round.browser.overlays.length,0);}
        if(scenario.mode==='early-stop'){assert.equal(round.requests.length,0);assert.equal(round.browser.frames,0);}
        if(scenario.encodeDelay && scenario.mode==='stop')assert.ok(round.requests[0].seq>0,'A frame older than 500 ms was uploaded after slow encoding');
        if(scenario.mode==='busy') {
          assert.ok(round.requests[1].startedAt-round.requests[0].finishedAt>=900,'Retry-After was ignored');
          const resumed=round.browser.captures.find(frame=>frame.seq===round.requests[1].seq);
          const previous=round.browser.captures.find(frame=>frame.seq===round.requests[0].seq);
          assert.ok(resumed.at-previous.at>=1000,'Backpressure resumed an old queued picture');
        }
        round.passed=true;console.log(JSON.stringify({scenario:scenario.name,requests:round.requests.length,captures:round.browser.captures.length,maxInFlight:round.maxInFlight,passed:true}));
      } catch(error) {
        round.failure={message:error.message,stack:error.stack};
        await page.screenshot({path:path.join(output,scenario.name+'-failure.png'),fullPage:true});throw error;
      } finally {await page.close();}
    }
    report.passed=true;
  } finally {
    fs.writeFileSync(path.join(output,'station-pipeline-ui.json'),JSON.stringify(report,null,2));
    await browser.close();console.log('Station pipeline UI artifacts: '+output);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
