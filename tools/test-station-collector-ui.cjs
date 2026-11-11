// Real browser, authentication and durable IndexedDB; collector commands/model responses are explicit fixtures.
// Run with test-pilot-ui.py --registration-paused --browser-script tools/test-station-collector-ui.cjs.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{randomUUID}=require('node:crypto');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output||process.env.APP_ENV!=='test') throw new Error('Use the isolated test-pilot-ui.py launcher');
const rows=new Map(),commands=new Map(),posts=[],errors=[];
let context,page,session,lostResponse=false,unsent=false,forbiddenRelease=0,holdPreview=null;
const report={passed:false,collectorControlMocked:true,inferenceMocked:true,persistentIndexedDB:true,checks:[]};
function snapshot(row){return {stationId:row.station,online:row.online,instanceId:row.instance,bindingRevision:1,
  runtimeId:row.runtime,state:row.phase,stream:row.stream,command:row.command,record:row.record,error:''};}
function complete(row){
  const command=row.command;assert.equal(command.status,'PENDING');
  if(command.action==='start'){
    row.stream={stationId:row.station,workstationId:'collector-'+row.station,cameraId:'camera-'+row.station,
      revision:1,streamId:randomUUID(),streamEpoch:++row.epoch,analysisType:'screw',sceneId:null,modelRevision:'fixture-model'};
    row.seq=0;row.phase='RUNNING';row.record=null;command.assignedStreamId=row.stream.streamId;
  }else{
    const finished=command.action==='finish',result=finished?{...row.latest,final:true,archived:true,
      compliance:row.visualPass?{status:'PASS',label:'合规',reason:'全部位置已确认'}:{status:'FAIL',label:'不合规',reason:'测试原始结论'}}:null;
    row.record={stationId:row.station,streamId:row.stream.streamId,context:row.stream,status:finished?'FINISHED':'INTERRUPTED',
      result,reason:finished?'本轮算法结果已归档':command.reason,startedAt:Date.now()/1000-10,finishedAt:Date.now()/1000};
    row.stream=null;row.phase='IDLE';
  }
  command.status='SUCCEEDED';command.completedAt=Date.now()/1000;
}
async function launch(){
  context=await chromium.launchPersistentContext(path.join(output,'collector-profile'),{channel:'msedge',headless:true,viewport:{width:1440,height:1000}});
  page=context.pages()[0];page.on('pageerror',error=>errors.push(error.message));
  context.on('request',request=>{if(request.method()==='DELETE'&&/\/station-streams\//.test(request.url()))forbiddenRelease++;});
  await context.route('**/api/v1/stations/*/collector**',async route=>{
    const url=new URL(route.request().url()),match=url.pathname.match(/\/stations\/(station-\d)\/collector(?:\/commands(?:\/([^/]+))?)?$/);
    if(!match)return route.fallback();
    const row=rows.get(match[1]);
    if(!row)return route.fallback();
    if(route.request().method()==='POST'){
      const body=route.request().postDataJSON();posts.push(body);
      const stored=await page.evaluate(station=>StationCollectorControl.get(station),row.station);
      assert.equal(stored.body.requestId,body.requestId,'Persist original command before network submission');
      if(unsent)return route.fulfill({status:503,json:{message:'测试请求未送达'}});
      if(!commands.has(body.requestId)){
        row.command={...body,status:'PENDING',instanceId:row.instance,runtimeId:row.runtime,bindingRevision:1,
          actor:{account:process.env.APP_USERNAME,role:'admin',reviewer:process.env.APP_USERNAME,credentialRevision:null}};
        commands.set(body.requestId,row.command);
        if(body.action==='finish')row.phase='FINISHING';if(body.action==='stop')row.phase='STOPPING';
      }
      if(lostResponse){lostResponse=false;return route.abort('failed');}
      return route.fulfill({status:202,json:snapshot(row)});
    }
    if(match[2])return route.fulfill(commands.has(match[2])?{json:commands.get(match[2])}:{status:404,json:{message:'未保存'}});
    return route.fulfill({json:snapshot(row)});
  });
  await context.route('**/api/v1/stations/*/live-status',async route=>{
    const station=new URL(route.request().url()).pathname.split('/')[4],row=rows.get(station);
    if(!row)return route.fallback();
    if(!row?.stream)return route.fulfill({json:{stationId:station,state:'OFFLINE',result:null}});
    row.latest={...row.stream,frameSeq:++row.seq,serverProcessingMs:5,targets:[{id:'T1',state:'PRESENT',bbox:[.1,.1,.4,.4]}],
      compliance:{status:'PENDING',label:'检测中',reason:'测试采样'}};
    if(row.visualPass){
      row.latest.compliance={status:'PASS',label:'合规',reason:'全部位置已确认'};
      row.latest.sequence={verificationMode:'visual-cumulative',configured:true,status:'PASS',positionsExpected:1,positionsConfirmed:1,
        operationsExpected:1,operationsCompleted:1,preassembledExpected:0,preassembledConfirmed:0};
    }
    return route.fulfill({json:{...row.stream,state:'LIVE',resultAgeMs:5,effectiveFps:4,result:row.latest}});
  });
  await context.route('**/api/v1/stations/*/preview?*',async route=>{
    const url=new URL(route.request().url()),row=rows.get(url.pathname.split('/')[4]);
    const identity={...row.stream},seq=url.searchParams.get('frame_seq');
    let fault;
    if(holdPreview){const held=holdPreview;holdPreview=null;held.started();await held.promise;fault=held.fault;}
    if(fault==='409')return route.fulfill({status:409,json:{message:'采样已更新'}});
    await route.fulfill({contentType:'image/jpeg',body:fs.readFileSync(path.join(output,'holes.jpg')),headers:{
      'X-Station-Id':identity.stationId,'X-Camera-Id':fault==='identity'?'wrong-camera':identity.cameraId,'X-Stream-Id':identity.streamId,
      'X-Stream-Epoch':String(identity.streamEpoch),'X-Frame-Seq':seq}});
  });
  await page.goto(base+'/?task=station1');
  await page.waitForFunction(()=>!!window.StationCollectorControl&&!!window.StationDetection);
}
const waitReady=()=>page.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy&&StationDetection.state.source==='device');
const waitPending=()=>page.waitForFunction(()=>StationDetection.state.collector?.command?.status==='PENDING');
async function closeModal(){if(await page.locator('#modal').isVisible())await page.locator('#modal [data-action=close-modal]').first().click();}
(async()=>{
  try{
    await launch();
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(login.status(),200);session=await login.json();
    for(let n=1;n<=2;n++){
      const station='station-'+n;
      const response=await context.request.put(base+'/api/v1/admin/terminals/'+station,{headers:{'X-CSRF-Token':session.csrf},data:{
        workstationId:'collector-'+station,cameraId:'camera-'+station,analysisType:'screw',sampleFps:4,maxSampleGapSeconds:1,source:'usb',enabled:true,revision:0}});
      assert.equal(response.status(),200);
      rows.set(station,{station,instance:randomUUID(),runtime:randomUUID(),online:true,phase:'IDLE',stream:null,command:null,record:null,epoch:0,seq:0});
    }
    await page.reload();await waitReady();assert.equal(await page.locator('#sd-start').isEnabled(),true);
    // Accepted command with lost response survives a full browser process restart without a second POST.
    lostResponse=true;await page.locator('#sd-start').click();await waitPending();
    const first=rows.get('station-1').command.requestId,postCount=posts.length;
    await context.close();await launch();await waitReady();await waitPending();
    assert.equal((await page.evaluate(()=>StationCollectorControl.get('station-1'))).body.requestId,first);
    assert.equal(posts.length,postCount);complete(rows.get('station-1'));
    await page.waitForFunction(()=>StationDetection.state.running&&StationDetection.state.frames>0);
    const stream=rows.get('station-1').stream.streamId;
    await page.reload();await waitReady();await page.waitForFunction(()=>StationDetection.state.frames>0);
    assert.equal(rows.get('station-1').stream.streamId,stream);assert.equal(forbiddenRelease,0);
    assert.equal(await page.evaluate(()=>StationDetection.state.stream.streamId),stream);
    await page.evaluate(()=>{window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));});
    await page.waitForFunction(()=>StationDetection.state.resultDeadline>performance.now()&&StationDetection.state.ready);
    assert.equal(forbiddenRelease,0);
    report.checks.push('lost-response-restart-original-request','refresh-preserves-background-round');
    report.checks.push('bfcache-return-resumes-status-polling');
    // A late picture from the previous station cannot paint the selected station.
    let release,started;const previewStarted=new Promise(resolve=>{started=resolve;});
    holdPreview={started,promise:new Promise(resolve=>{release=resolve;})};await previewStarted;
    await page.locator('#inspection-task').selectOption('station2');
    await page.waitForFunction(()=>StationDetection.state.station==='station-2'&&StationDetection.state.connected);
    release();await page.waitForTimeout(600);
    assert.equal(await page.evaluate(()=>StationDetection.state.frame),null);assert.equal(forbiddenRelease,0);
    await page.locator('#inspection-task').selectOption('station1');await waitReady();await page.waitForFunction(()=>StationDetection.state.frames>0);
    report.checks.push('switch-preserves-round','late-preview-does-not-cross-stations');
    for(const fault of ['409','identity']){
      let releaseFault,startedFault;const oldStarted=new Promise(resolve=>{startedFault=resolve;});
      holdPreview={fault,started:startedFault,promise:new Promise(resolve=>{releaseFault=resolve;})};await oldStarted;
      await page.locator('#inspection-task').selectOption('station2');
      await page.waitForFunction(()=>StationDetection.state.station==='station-2'&&StationDetection.state.connected&&!StationDetection.state.busy);
      if(!rows.get('station-2').stream){await page.locator('#sd-start').click();await waitPending();complete(rows.get('station-2'));}
      await page.waitForFunction(()=>StationDetection.state.frame?.stationId==='station-2'&&!document.querySelector('#sd-overlay').hidden);
      releaseFault();await page.waitForTimeout(50);
      assert.ok(await page.evaluate(()=>StationDetection.state.resultDeadline>performance.now()&&!document.querySelector('#sd-overlay').hidden));
      await page.locator('#inspection-task').selectOption('station1');await waitReady();await page.waitForFunction(()=>StationDetection.state.frames>0);
    }
    report.checks.push('late-preview-error-cannot-clear-new-station');
    // Finish must wait for the worker's durable archive acknowledgement.
    await page.locator('#sd-finish').click();await waitPending();
    assert.equal(await page.evaluate(()=>StationDetection.state.archived),false);
    assert.equal(await page.locator('#sd-start').isEnabled(),false);
    complete(rows.get('station-1'));await page.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream);
    assert.equal(await page.evaluate(()=>StationDetection.state.final.compliance.status),'FAIL');await closeModal();
    assert.match(await page.locator('#server-status').textContent(),/本件已归档/);
    report.checks.push('finish-waits-for-durable-worker-ack','final-server-verdict-preserved');
    // Unknown submission uses one immutable command; no automatic new start after reload.
    unsent=true;await page.locator('#sd-start').click();await page.waitForFunction(()=>StationDetection.state.collectorCommand?.missing&&StationDetection.state.collectorCommand.status==='pending');
    const original=posts.at(-1);await page.reload();await waitReady();
    assert.equal(await page.locator('#sd-start').isEnabled(),false);unsent=false;
    await page.locator('#sd-command-retry').click();await waitPending();assert.deepEqual(posts.at(-1),original);
    complete(rows.get('station-1'));await page.waitForFunction(()=>StationDetection.state.frames>0&&StationDetection.state.running);
    assert.notEqual(rows.get('station-1').stream.streamId,stream);
    report.checks.push('unsent-request-explicit-same-id-retry','next-piece-new-stream');
    // Explicit interruption records its selected reason and never reuses the prior final result.
    await page.locator('#sd-stop-reason').selectOption('设备或采样异常，本件需复核');await page.locator('#sd-stop').click();await waitPending();
    complete(rows.get('station-1'));await page.waitForFunction(()=>!StationDetection.state.stream&&!StationDetection.state.running);
    assert.equal(await page.evaluate(()=>StationDetection.state.final),null);
    assert.match(await page.locator('#alert-list').textContent(),/设备或采样异常/);assert.equal(forbiddenRelease,0);
    report.checks.push('explicit-interruption-reason','no-browser-stream-delete');
    // Camera coverage uses one durable finish command and waits for the collector acknowledgement.
    await page.locator('#inspection-task').selectOption('station2');await waitReady();
    rows.get('station-2').visualPass=true;
    await page.waitForFunction(()=>StationDetection.state.collector?.command?.action==='finish'&&StationDetection.state.collector.command.status==='PENDING');
    const automatic=rows.get('station-2').command.requestId;
    assert.equal(await page.locator('#modal').isVisible(),false);
    await page.waitForTimeout(700);
    assert.equal(posts.filter(p=>p.requestId===automatic).length,1);
    complete(rows.get('station-2'));
    await page.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream);
    assert.equal(await page.locator('#modal-title').textContent(),'工位2 · 检测合规');await closeModal();
    report.checks.push('visual-camera-auto-finish-once','visual-camera-waits-for-archive-popup');
    await page.locator('#inspection-task').selectOption('station1');await waitReady();
    unsent=true;await page.locator('#sd-start').click();
    await page.waitForFunction(()=>StationDetection.state.collectorCommand?.missing&&StationDetection.state.collectorCommand.status==='pending');
    const beforeChangedInstance=posts.length;rows.get('station-1').instance=randomUUID();unsent=false;
    await page.reload();await waitReady();await page.locator('#sd-command-retry').click();
    await page.waitForFunction(()=>StationDetection.state.collectorCommand?.status==='blocked');
    assert.equal(posts.length,beforeChangedInstance);assert.match(await page.locator('#sd-collector-note').textContent(),/旧请求不会执行/);
    const storageFailure=await page.evaluate(async()=>{
      const original=IDBDatabase.prototype.transaction;
      IDBDatabase.prototype.transaction=function(name,mode,...args){if(mode==='readwrite')throw new DOMException('测试存储失败','QuotaExceededError');return original.call(this,name,mode,...args);};
      try{await StationCollectorControl.execute('station-1',StationDetection.state.binding,'start',null,'',StationDetection.state.collector);return null;}
      catch(error){return error.name;}finally{IDBDatabase.prototype.transaction=original;}
    });
    assert.equal(storageFailure,'QuotaExceededError');assert.equal(posts.length,beforeChangedInstance);
    report.checks.push('old-unsent-start-blocked-after-collector-change','storage-failure-prevents-submission');
    await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'collector-mobile.png'),fullPage:true});
    // A separate station exercises the real command/stream/ack API from the actual controls.
    // No frames or model inference are needed to prove start, refresh and explicit interruption.
    await page.setViewportSize({width:1440,height:1000});
    const registered=await context.request.put(base+'/api/v1/admin/terminals/station-3',{headers:{'X-CSRF-Token':session.csrf},data:{
      workstationId:'real-collector-pc',cameraId:'real-collector-camera',analysisType:'screw',sampleFps:4,maxSampleGapSeconds:1,source:'usb',enabled:true,revision:0}});
    assert.equal(registered.status(),200);const registration=await registered.json(),instance=randomUUID();
    const realPath=base+'/api/v1/stations/station-3/collector',headers={'X-Terminal-Key':registration.credential,'X-Collector-Instance':instance};
    assert.equal((await context.request.post(realPath+'/heartbeat',{headers,data:{instanceId:instance,state:'IDLE'}})).status(),200);
    await page.locator('#inspection-task').selectOption('station3');
    await page.waitForFunction(()=>StationDetection.state.station==='station-3'&&StationDetection.state.source==='device'&&StationDetection.state.connected&&!StationDetection.state.busy);
    await page.locator('#sd-start').click();await waitPending();
    const actualStart=(await(await context.request.get(realPath)).json()).command;
    const opened=await context.request.post(base+'/api/v1/station-streams',{headers:{...headers,'X-Collector-Command':actualStart.requestId},
      data:{requestId:actualStart.requestId,inputSource:'device'}});
    assert.equal(opened.status(),201,await opened.text());const realStream=await opened.json();
    assert.equal((await context.request.post(realPath+'/commands/'+actualStart.requestId+'/ack',{headers,
      data:{instanceId:instance,streamId:realStream.streamId,status:'SUCCEEDED',error:''}})).status(),200);
    await page.waitForFunction(id=>StationDetection.state.stream?.streamId===id&&StationDetection.state.running,realStream.streamId);
    await page.reload();await page.waitForFunction(id=>StationDetection.state.stream?.streamId===id&&StationDetection.state.running&&!StationDetection.state.busy,realStream.streamId);
    assert.equal((await(await context.request.get(realPath)).json()).record.status,'RUNNING');
    await page.locator('#sd-stop').click();await waitPending();
    const actualStop=(await(await context.request.get(realPath)).json()).command;
    const stopped=await context.request.delete(base+'/api/v1/station-streams/'+realStream.streamId,{headers:{...headers,'X-Collector-Command':actualStop.requestId},data:{reason:actualStop.reason}});
    assert.equal(stopped.status(),200,await stopped.text());
    assert.equal((await context.request.post(realPath+'/commands/'+actualStop.requestId+'/ack',{headers,
      data:{instanceId:instance,streamId:realStream.streamId,status:'SUCCEEDED',error:''}})).status(),200);
    await page.waitForFunction(()=>!StationDetection.state.stream&&StationDetection.state.collector?.record?.status==='INTERRUPTED');
    assert.equal(forbiddenRelease,0);report.realCommandApi=true;
    report.checks.push('real-command-api-start-refresh-stop');
    // Session identity changes invalidate the page's collector control view; no old command is submitted as the new actor.
    await page.evaluate(()=>Workspace.logout());
    await page.waitForFunction(()=>StationDetection.state.binding===null&&StationDetection.state.stream===null);
    report.checks.push('session-switch-clears-collector-view');
    assert.deepEqual(errors,[]);report.passed=true;report.posts=posts.length;report.browserDeletes=forbiddenRelease;
    console.log('Collector UI passed:',report.checks.join(', '));
  }finally{fs.writeFileSync(path.join(output,'station-collector-ui.json'),JSON.stringify(report,null,2));await context?.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
