// Isolated API/database: inference and final evidence are fixtures; manual review,
// authorization, history, exports and Board use the real server and persisted SQL.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const {spawnSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE, output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output||!path.resolve(output).startsWith(path.resolve('test-results/pilot-browser-tests')+path.sep)) throw new Error('Run with isolated test-pilot-ui.py');
const names=['左侧三角贴','右侧三角贴','左上方形贴','右上方形贴','左中方形贴','右中方形贴','左下方形贴','右下方形贴'];
const python=resolvePython();
function archiveFixture(identity,result){
  const saved={...result,final:true,archived:true,archivedAt:Date.now()/1000,recordStatus:'FINISHED'};
  const run=spawnSync(python,['-c',
    'import json,sqlite3,sys,time\np=json.loads(sys.stdin.buffer.read().decode("utf-8"))\nwith sqlite3.connect(p["db"]) as c:\n r=c.execute("UPDATE station_inspections SET status=\'FINISHED\', result=?, finished_at=? WHERE stream_id=? AND status=\'RUNNING\'",(json.dumps(p["result"]),time.time(),p["id"]))\n assert r.rowcount==1\n'],
    {input:JSON.stringify({db:path.join(output,'test.db'),id:identity.streamId,result:saved}),encoding:'utf8',windowsHide:true});
  assert.equal(run.status,0,run.stderr); return saved;
}
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
  const context=await browser.newContext({viewport:{width:1440,height:1000},permissions:['camera']});
  const page=await context.newPage(), errors=[], rounds=new Map(), reviews=[];
  let mode='pending',current,rejectOnce=false;
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{
    window.reviewCameraTracks=[];
    const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia=async options=>{const stream=await original(options);reviewCameraTracks.push(...stream.getTracks());return stream;};
  });
  await page.route('**/api/v1/station-streams?*',async route=>{
    const response=await route.fetch(), identity=await response.json();assert.equal(response.status(),201);
    current={identity,frames:0,mode,finishes:0};rounds.set(identity.streamId,current);await route.fulfill({response,json:identity});
  });
  await page.route('**/api/v1/station-streams/*/frames',async route=>{
    const row=rounds.get(route.request().url().split('/').at(-2));row.frames++;
    const headers=route.request().headers(),hasActions=row.frames>=3;
    const status=row.mode==='fail'&&hasActions?'FAIL':'REVIEW';
    const targets=names.map((name,i)=>({id:['T01','T02','R01','R02','R03','R04','R05','R06'][i],name,
      state:hasActions&&[0,1,3,4,7].includes(i)?'ACTION_DONE':'UNKNOWN'}));
    row.latest={...row.identity,frameSeq:Number(headers['x-frame-seq']),targets,aggregateTargets:targets,
      sequence:{mode:'action',status:status==='FAIL'?'FAIL':'PENDING',events:[]},
      compliance:{mode:'action',status,label:status==='FAIL'?'动作不合规':'动作证据不足',
        reason:status==='FAIL'?'方形贴动作先于三角贴，动作顺序错误':'尚未取得完整贴放/按压动作证据'}};
    await route.fulfill({json:row.latest});
  });
  await page.route('**/api/v1/station-streams/*/finish',async route=>{
    const row=rounds.get(route.request().url().split('/').at(-2));row.finishes++;
    const status=row.mode==='review'?'REVIEW':'FAIL';
    row.final=archiveFixture(row.identity,{...row.latest,compliance:{...row.latest.compliance,status,
      label:status==='FAIL'?'动作不合规':'动作证据不足',reason:status==='FAIL'?'结束时未观察到动作：R01, R04, R05':'本轮动作证据有缺口，需现场复核'}});
    await route.fulfill({json:row.final});
  });
  await page.route('**/api/v1/station-streams/*/manual-review',async route=>{
    reviews.push(route.request().postDataJSON());
    if(rejectOnce){rejectOnce=false;return route.fulfill({status:503,json:{message:'测试保存失败，请重试'}});}
    await route.continue();
  });
  const idle=()=>page.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream&&!StationDetection.state.busy);
  const close=()=>page.locator('#modal [data-action=close-modal]').first().click();
  const start=async()=>{const previous=current?.identity.streamId;await page.locator('#sd-start').click();
    await page.waitForFunction(previous=>StationDetection.state.frames>=3&&StationDetection.state.frame?.streamId!==previous,previous);};
  async function stopped(){const count=current.frames;await page.waitForTimeout(500);assert.equal(current.frames,count);assert.ok(await page.evaluate(()=>reviewCameraTracks.every(t=>t.readyState==='ended')));}
  try{
    const session=await(await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}})).json();
    assert.equal((await context.request.put(base+'/api/v1/admin/terminals/station-2',{headers:{'X-CSRF-Token':session.csrf},data:{
      workstationId:'review-pc',cameraId:'review-camera',analysisType:'sticker',sampleFps:10,maxSampleGapSeconds:1,source:'usb',enabled:true,revision:0}})).status(),200);
    await page.goto(base+'/?task=station2');await page.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy);
    await start();await page.waitForFunction(()=>StationDetection.state.frames>=5);
    assert.equal(await page.locator('#modal').isVisible(),false,'Pending action evidence must not repeatedly interrupt the operator');
    assert.equal(await page.locator('#step-counter').textContent(),'5/8 处');
    await page.locator('#sd-review').click();await idle();await stopped();
    assert.equal(await page.locator('#modal-title').textContent(),'工位2 · 人工确认');
    assert.match(await page.locator('#modal-body').textContent(),/左上方形贴.*右中方形贴.*左下方形贴/);
    assert.equal(await page.locator('#sd-review-reason').getAttribute('required'),null);
    await page.locator('#sd-review-outcome').selectOption('PASS');await page.locator('#sd-review-submit').click();
    await page.waitForFunction(()=>StationDetection.state.final?.manualReview?.status==='PASS'&&!StationDetection.state.busy);
    assert.equal(await page.locator('#modal-title').textContent(),'工位2 · 人工确认合规');
    assert.equal(await page.evaluate(()=>StationDetection.state.final.compliance.status),'FAIL');
    assert.equal(await page.evaluate(()=>StationDetection.state.final.manualReview.reason),'');
    assert.equal(await page.locator('#step-counter').textContent(),'5/8 处','Manual review must preserve algorithm position evidence');
    const first=current.identity.streamId;
    let board=await(await context.request.get(base+'/api/v1/board/snapshot?mode=detection')).json();
    assert.equal(board.summary.passed,1);
    await close();await page.locator('#sd-history').click();
    await page.locator(`details[data-stream="${first}"]`).click();
    assert.match(await page.locator('#modal-body').textContent(),/人工确认合规/);
    assert.match(await page.locator('#modal-body').textContent(),/算法原判定：动作不合规/);
    const downloaded=page.waitForEvent('download');await page.locator('[data-sd-download="0"]').click();
    const download=await downloaded,filename=path.join(output,'manual-review-export.json');await download.saveAs(filename);
    const exported=JSON.parse(fs.readFileSync(filename,'utf8'));
    assert.equal(exported.result.manualReview.reason,'');assert.equal(exported.result.compliance.status,'FAIL');
    await close();
    mode='fail';await start();await idle();await stopped();
    assert.notEqual(current.identity.streamId,first);
    assert.equal(await page.evaluate(()=>StationDetection.state.final.manualReview||null),null);
    assert.equal(await page.locator('#modal-title').textContent(),'工位2 · 检测不合规');
    await page.screenshot({path:path.join(output,'action-noncompliant-dialog.png'),fullPage:true});
    await close();await page.locator('#sd-review').click();
    await page.locator('#sd-review-outcome').selectOption('FAIL');
    await page.locator('#sd-review-reason').fill('   ');rejectOnce=true;
    await page.locator('#sd-review-submit').click();await page.waitForFunction(()=>document.querySelector('#sd-review-error').textContent.includes('保存失败'));
    assert.equal(await page.evaluate(()=>StationDetection.state.final.manualReview||null),null);
    await page.locator('#sd-review-submit').click();await page.waitForFunction(()=>StationDetection.state.final?.manualReview?.status==='FAIL');
    assert.equal(reviews.at(-1).requestId,reviews.at(-2).requestId);
    assert.equal(await page.locator('#modal-title').textContent(),'工位2 · 人工确认不合规');await close();
    mode='review';await start();await page.locator('#sd-finish').click();await idle();
    assert.equal(await page.locator('#modal-title').textContent(),'工位2 · 检测证据不足');
    const historical=current.identity.streamId;await close();
    await page.reload();await page.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy);
    await page.locator('#sd-history').click();await page.locator(`details[data-stream="${historical}"]`).click();
    await page.locator('[data-sd-review="0"]').click();
    await page.locator('#sd-review-outcome').selectOption('PASS');await page.locator('#sd-review-reason').fill('现场复核贴放动作已完成');
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'manual-review-mobile.png'),fullPage:true});
    await page.locator('#sd-review-submit').click();await page.waitForFunction(()=>document.querySelector('#modal-title').textContent.includes('人工确认合规'));
    const history=await(await context.request.get(base+'/api/v1/stations/station-2/inspections')).json();
    assert.equal(history.items[0].result.manualReview.reason,'现场复核贴放动作已完成');
    assert.equal(history.items[0].result.compliance.status,'REVIEW');
    board=await(await context.request.get(base+'/api/v1/board/snapshot?mode=detection')).json();
    assert.equal(board.summary.passed,2);assert.equal(board.summary.failed,1);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'station-review.json'),JSON.stringify({passed:true,inferenceMocked:true,archiveFixture:true,
      manualReviewReal:true,historyAndBoardReal:true,rounds:rounds.size,reviewRequests:reviews.length,errors},null,2));
    console.log('Manual review: optional reason, live/final alerts, saved audit, retries, history, export, Board and next round passed.');
  }catch(error){await page.screenshot({path:path.join(output,'review-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
