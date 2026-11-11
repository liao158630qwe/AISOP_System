// Deterministic transport/UI regression; no production account or model is used.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),output=path.join(root,'test-results','realtime-transport-20261105');
const sceneId='f403926a-0000-4000-8000-000000000001';
const scene={id:sceneId,name:'通信回归场景',ready:true,revision:1,sample_fps:10,
 model:{name:'fixture',version:'1',classes:{0:'part'}},rules:[{id:'part',name:'工件存在',kind:'required',roi:[0,0,1,1],threshold:.5,class_id:0}]};
const decision={status:'PASS',label:'通过',reason:'测试采样中可见工件'};
const server=http.createServer((req,res)=>{
 const pathname=new URL(req.url,'http://localhost').pathname;
 const publicRoot=pathname.startsWith('/assets/')?root:path.join(root,'frontend');
 const file=path.join(publicRoot,pathname==='/'?'index.html':pathname);
 if(!file.startsWith(root+path.sep)){res.writeHead(403).end();return;}
 fs.readFile(file,(error,data)=>{if(error){res.writeHead(404).end();return;}res.setHeader('Content-Type',({'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'})[path.extname(file)]||'application/octet-stream');res.end(data);});
});
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({channel:'msedge',headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
 const context=await browser.newContext({viewport:{width:1440,height:1000},permissions:['camera']});
 const page=await context.newPage(),errors=[],requests=[],finishes=[],waits=[],released=[];let behavior='fast',busyOnce=false,createdRuns=0;
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/api/v1/**',async route=>{
  const req=route.request(),url=new URL(req.url()),headers=req.headers();
  const reply=(json,status=200,extra={})=>route.fulfill({status,json,headers:extra}).catch(()=>{});
  if(url.pathname.endsWith('/session'))return reply({username:'fixture',csrf:'test-csrf'});
  if(url.pathname.endsWith('/scenes'))return reply({items:[scene]});
  if(url.pathname.endsWith('/scenes/'+sceneId))return reply(scene);
  if(url.pathname.endsWith('-model'))return reply({ready:true,modelName:'fixture',targets:[],offlineBackend:'yolo'});
  if(url.pathname.endsWith('/realtime/frame')){
   requests.push({at:Date.now(),id:headers['x-frame-id'],capturedAt:headers['x-captured-at'],streamId:headers['x-stream-id'],frameTime:Number(headers['x-frame-time'])});
   if(behavior==='prepare-slow'){await sleep(3500);behavior='fast';}
   if(behavior==='prepare-held')await sleep(1500);
   if(behavior==='busy'&&!busyOnce){busyOnce=true;return reply({message:'busy'},429,{'Retry-After':'1'});}
   if(behavior==='slow')await sleep(1000);
   if(behavior==='timeout')await sleep(1500);
   const frameTime=Number(headers['x-frame-time']);
   return reply({frameId:headers['x-frame-id'],frameTime,processingMs:2,serverProcessingMs:3,
    observations:[{name:'part',bbox:[.1,.1,.3,.3],score:.99}],checks:[{id:'part',name:'工件存在',state:'PASS',count:1,reason:'可见工件'}],compliance:decision,
    evidenceGaps:behavior==='server-gap'?[{start:0,end:3.5,reason:'服务器检测到中断',source:'server'}]:[]});
  }
  if(url.pathname.endsWith('/finish')){finishes.push(req.postDataJSON());return reply({compliance:decision,checks:[],final:true});}
  if(url.pathname.includes('/realtime/streams/')){released.push(url.pathname.split('/').at(-1));return reply({released:true});}
  if(url.pathname.endsWith('/transport-test')){await sleep(500);return reply({ok:true});}
  if(url.pathname.endsWith('/media'))return reply({id:'c7000000-0000-4000-8000-000000000001'},201);
  if(url.pathname.endsWith('/analysis-runs')&&req.method()==='POST'){createdRuns++;return reply({id:'run-fixture',status:'QUEUED',updated_at:1,progress:0},201);}
  if(url.pathname.endsWith('/analysis-runs/run-fixture'))return reply({id:'run-fixture',status:'QUEUED',updated_at:1,progress:0});
  if(url.pathname.endsWith('/analysis-runs/run-fixture/wait')){waits.push(url.search);if(waits.length===1)return route.abort('connectionfailed');return reply({id:'run-fixture',status:'CANCELLED',updated_at:2,progress:0});}
  return reply({items:[]});
 });
 try{
  await page.goto('http://127.0.0.1:'+server.address().port+'/?task=scene:'+sceneId);
  await page.waitForFunction(()=>window.ScrewApp?.state.model?.ready&&!window.ScrewApp.state.busy);
  // An explicit AbortSignal and timeout are both honored by all front-end callers.
  const deadline=await page.evaluate(async()=>{try{await Workspace.fetchJSON('/transport-test',{signal:new AbortController().signal,timeout:30});return 'unexpected';}catch(e){return e.name;}});
  assert.equal(deadline,'TimeoutError');
  behavior='prepare-slow';await page.evaluate(()=>ScrewApp.state.frameTimeoutMs=120);
  await page.locator('#start-live').click();
  await page.waitForFunction(()=>Boolean(ScrewApp.state.preparing&&ScrewApp.state.pending));
  const preparationStream=await page.evaluate(()=>ScrewApp.state.streamId);
  assert.equal(await page.locator('#metric-compliance').textContent(),'准备中');
  assert.equal(await page.locator('#metric-fps').textContent(),'—');
  assert.equal(await page.evaluate(()=>ScrewApp.state.frames.length),0);
  await page.waitForFunction(()=>window.ScrewApp.state.frame&&window.ScrewApp.state.drawnBoxes.length===1);
  assert.ok(await page.evaluate(()=>ScrewApp.state.preparationMs>=3500));
  assert.equal(await page.evaluate(()=>ScrewApp.state.evidenceGaps.length),0,'Model preparation is excluded from production evidence gaps');
  assert.notEqual(await page.evaluate(()=>ScrewApp.state.streamId),preparationStream);
  assert.ok(released.includes(preparationStream),'The preparation stream is released');
  assert.ok(requests.find(r=>r.streamId!==preparationStream).frameTime<.5,'The production stream gets a new time origin');
  await page.evaluate(()=>ScrewApp.state.frameTimeoutMs=12000);
  const first=await page.evaluate(()=>{const s=ScrewApp.state;return {captured:s.lastSampleAt,received:s.lastResponse,latency:s.displayLatencyMs};});
  assert.ok(first.captured<=first.received&&first.latency>=0);
  assert.ok(requests.every(r=>r.id&&Number(r.capturedAt)>0));
  behavior='slow';
  await page.waitForFunction(()=>{const s=ScrewApp.state;return s.lastResponse-s.lastSampleAt>900&&s.drawnBoxes.length===0;});
  assert.equal(await page.locator('#metric-compliance').textContent(),'待新采样');
  assert.ok(await page.evaluate(()=>ScrewApp.state.displayLatencyMs>900));
  // Repeated animation frames must not manufacture new result freshness.
  const slowCount=requests.length;await sleep(350);assert.ok(requests.length<=slowCount+1);
  behavior='busy';const begin=requests.length;
  await page.waitForFunction(()=>window.ScrewApp.state.liveFailures===1);
  assert.equal(await page.locator('#metric-compliance').textContent(),'待新采样');
  await page.waitForFunction(()=>window.ScrewApp.state.liveFailures===0&&window.ScrewApp.state.actualFps>0);
  const retried=requests.slice(begin);assert.ok(retried.length>=2);assert.ok(retried.at(-1).at-retried.at(-2).at>=900,'Retry-After is respected');
  assert.equal(await page.evaluate(()=>ScrewApp.state.evidenceGaps.length),0,'A single busy response is not a persistent evidence gap');
  behavior='timeout';await page.evaluate(()=>ScrewApp.state.frameTimeoutMs=120);
  await page.waitForFunction(()=>ScrewApp.state.liveFailures>=1);
  await page.locator('#finish-detection').click();
  await page.waitForFunction(()=>!ScrewApp.state.busy&&!ScrewApp.state.streamId);
  const stopped=await page.evaluate(()=>({frames:ScrewApp.state.frames.length,stream:ScrewApp.state.stream}));
  const stoppedCount=requests.length;await sleep(1700);
  assert.equal(requests.length,stoppedCount,'Stopping must cancel retries');
  assert.equal(await page.evaluate(()=>ScrewApp.state.frames.length),stopped.frames,'Late response must not mutate a finished run');assert.equal(stopped.stream,null);
  behavior='fast';await page.evaluate(()=>ScrewApp.state.frameTimeoutMs=12000);
  await page.locator('#start-live').click();await page.waitForFunction(()=>ScrewApp.state.frame&&ScrewApp.state.drawnBoxes.length===1);
  // Exercise the browser visibility event without relying on headless tab focus semantics.
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
  const hiddenCount=requests.length;await sleep(3300);assert.equal(requests.length,hiddenCount,'Hidden pages stop sampling');
  assert.equal(await page.locator('#metric-compliance').textContent(),'待新采样');
  await page.evaluate(()=>{delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForFunction(()=>ScrewApp.state.evidenceGaps.length&&ScrewApp.state.drawnBoxes.length===1);
  assert.equal(await page.locator('#metric-compliance').textContent(),'缺口待复核');
  await page.locator('#finish-detection').click();await page.waitForFunction(()=>!ScrewApp.state.busy&&!ScrewApp.state.streamId);
  assert.equal(await page.evaluate(()=>ScrewApp.state.final.compliance.status),'REVIEW');
  assert.equal(await page.evaluate(()=>ScrewApp.state.final.originalCompliance.status),'PASS');
  assert.ok(finishes.at(-1).evidenceGaps.some(gap=>gap.end-gap.start>=3),'Evidence gaps are submitted with the finalization request');
  fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'evidence-gap.png'),fullPage:true});
  behavior='server-gap';await page.locator('#start-live').click();await page.waitForFunction(()=>ScrewApp.state.drawnBoxes.length===1);
  await sleep(250);assert.equal(await page.evaluate(()=>ScrewApp.state.evidenceGaps.length),1,'Repeated server gaps are merged once');
  assert.equal(await page.locator('#metric-compliance').textContent(),'缺口待复核');
  await page.evaluate(()=>{const track=ScrewApp.state.stream.getVideoTracks()[0];track.stop();track.dispatchEvent(new Event('ended'));});
  assert.equal(await page.locator('#metric-compliance').textContent(),'待新采样');
  assert.equal(await page.evaluate(()=>ScrewApp.state.drawnBoxes.length),0);
  const disconnectedCount=requests.length;await sleep(650);assert.equal(requests.length,disconnectedCount,'Disconnected cameras must not submit cached video frames');
  await page.locator('#finish-detection').click();await page.waitForFunction(()=>!ScrewApp.state.busy&&!ScrewApp.state.streamId);
  assert.ok(finishes.at(-1).evidenceGaps.length);assert.deepEqual(Object.keys(finishes.at(-1).evidenceGaps[0]).sort(),['end','reason','start'],'Finalization sends the declared gap schema only');
  behavior='prepare-held';const finishCount=finishes.length;
  await page.locator('#start-live').click();await page.waitForFunction(()=>Boolean(ScrewApp.state.preparing&&ScrewApp.state.pending));
  const cancelledPreparation=await page.evaluate(()=>ScrewApp.state.streamId);
  await page.locator('#finish-detection').click();await page.waitForFunction(()=>!ScrewApp.state.busy&&!ScrewApp.state.streamId);
  assert.equal(finishes.length,finishCount,'Cancelling preparation must not request a production verdict');
  assert.equal(await page.evaluate(()=>ScrewApp.state.final),null);
  const afterCancel=requests.length;await sleep(1700);
  assert.equal(requests.length,afterCancel);assert.equal(await page.evaluate(()=>ScrewApp.state.frames.length),0);
  assert.ok(released.includes(cancelledPreparation));
  await page.locator('#start-live').click();await page.waitForFunction(()=>Boolean(ScrewApp.state.preparing&&ScrewApp.state.pending));
  await page.locator('#inspection-task').selectOption('pose');await page.waitForFunction(()=>ScrewApp.state.task==='pose'&&!ScrewApp.state.busy&&!ScrewApp.state.switching);
  assert.equal(finishes.length,finishCount,'Switching during preparation must not request a production verdict');
  assert.equal(await page.evaluate(()=>ScrewApp.state.final),null);
  await page.locator('#inspection-task').selectOption('scene:'+sceneId);await page.waitForFunction(()=>ScrewApp.state.task.startsWith('scene:')&&!ScrewApp.state.busy&&!ScrewApp.state.switching);
  behavior='fast';
  await page.locator('.toolbar [data-action=upload]').click();
  await page.locator('#screw-upload-file').setInputFiles({name:'transport-fixture.mp4',mimeType:'video/mp4',buffer:Buffer.from('fixture video; model is mocked')});
  await page.locator('#screw-upload-start').click();
  await page.waitForFunction(()=>ScrewApp.state.watchRetries===1);
  assert.equal(await page.evaluate(()=>ScrewApp.state.busy),true,'A transient polling failure keeps the current task active');
  await page.waitForFunction(()=>ScrewApp.state.runStatus==='CANCELLED'&&!ScrewApp.state.busy);
  assert.equal(createdRuns,1,'Polling retries never create another analysis task');assert.equal(waits.length,2);assert.equal(waits[0],waits[1],'Retry uses the same run and progress cursor');
  assert.deepEqual(errors,[]);
  const report={passed:true,checks:['signal-and-deadline','isolated-model-preparation','preparation-cancel-and-task-switch','capture-based-freshness','late-response-removal','retry-after','short-busy-no-gap','timeout-cancel','late-response-isolation','hidden-pause-resume','gap-prevents-auto-pass','gaps-submitted-to-server','server-gap-merge-and-schema','camera-disconnect-clears-and-pauses','poll-reconnect-same-run-and-cursor'],requests:requests.length,pageErrors:errors};
  fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
