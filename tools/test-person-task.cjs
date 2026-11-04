// Isolated API/worker and real YOLO inference with a recorded browser camera.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {spawn,spawnSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const playwright=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),python=resolvePython();
const runDir=path.join(root,'.runtime','person-ui-'+Date.now()),out=path.join(root,'outputs','person-detection-2026-10-28');
fs.mkdirSync(runDir,{recursive:true});fs.mkdirSync(out,{recursive:true});
const port=Number(process.env.PERSON_TEST_PORT||8063),base='http://127.0.0.1:'+port;
const env={...process.env,APP_ENV:'development',COOKIE_SECURE:'false',APP_USERNAME:'person-ui-test',APP_PASSWORD:'local-person-ui-test-12345',
  DATABASE_URL:'sqlite:///'+path.join(runDir,'test.db').replaceAll('\\','/'),DATA_DIR:path.join(runDir,'data'),
  MODEL_PATH:path.join(root,'models','yolo11s-pose.pt'),POSE_BACKEND:'yolo',REALTIME_BACKEND:'yolo',REALTIME_POOL_SIZE:'1',REALTIME_WARMUP:'false',
  TEMP:runDir,TMP:runDir,YOLO_CONFIG_DIR:path.join(runDir,'ultralytics'),PYTHONUNBUFFERED:'1'};
const fixture=spawnSync(python,['-c',`
import cv2, sys
from pathlib import Path
out=Path(sys.argv[1]); cap=cv2.VideoCapture('assets/factory-workers.mp4')
writer=cv2.VideoWriter(str(out/'people.avi'),cv2.VideoWriter_fourcc(*'MJPG'),10,(640,360))
assert writer.isOpened()
with (out/'camera.y4m').open('wb') as raw:
    raw.write(b'YUV4MPEG2 W640 H360 F10:1 Ip A1:1 C420jpeg\\n')
    for _ in range(20):
        ok,frame=cap.read(); assert ok
        frame=cv2.resize(frame,(640,360)); writer.write(frame)
        raw.write(b'FRAME\\n'); raw.write(cv2.cvtColor(frame,cv2.COLOR_BGR2YUV_I420).tobytes())
cap.release(); writer.release()
`,runDir],{cwd:root,env,windowsHide:true,encoding:'utf8'});
assert.equal(fixture.status,0,fixture.stderr);
const children=[];let browser,logs='';
function start(args){const child=spawn(python,args,{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});children.push(child);for(const stream of [child.stdout,child.stderr])stream.on('data',data=>{logs=(logs+data).slice(-12000);});return child;}
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  try{
    start(['-c',`from backend.app import create_app; from backend.db import initialize; import uvicorn; app=create_app(); initialize(app.state.engine); uvicorn.run(app,host='127.0.0.1',port=${port},log_level='warning')`]);
    let ready=false;
    for(let i=0;i<100;i++){try{ready=(await fetch(base+'/api/v1/health')).ok;if(ready)break;}catch{}await delay(250);}
    assert.ok(ready,logs);start(['-m','backend.worker']);
    browser=await playwright.chromium.launch({channel:'msedge',headless:true,env,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--use-file-for-fake-video-capture='+path.join(runDir,'camera.y4m')]});
    const context=await browser.newContext({viewport:{width:1512,height:1100},permissions:['camera']});
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/?task=screw');await page.waitForFunction(()=>window.ScrewApp?.state.model&&!window.ScrewApp.state.busy);
    assert.equal(await page.locator('.screw-focus-strip,#screw-focus-canvas').count(),0);
    await page.locator('#inspection-task').selectOption('pose');
    await page.waitForFunction(()=>window.ScrewApp.state.task==='pose'&&window.ScrewApp.state.model?.ready&&!window.ScrewApp.state.busy);
    assert.equal(await page.locator('#metric-targets').evaluate(e=>e.nextElementSibling.textContent),'人');
    assert.equal(await page.locator('#sticker-sequence').isVisible(),false);
    await page.locator('.toolbar [data-action=upload]').click();
    await page.locator('#login-user').fill(env.APP_USERNAME);await page.locator('#login-password').fill(env.APP_PASSWORD);
    await page.locator('#screw-login button[type=submit]').click();
    await page.locator('#screw-upload-file').setInputFiles(path.join(runDir,'people.avi'));
    await page.locator('#screw-upload-start').click();
    await page.waitForFunction(()=>!window.ScrewApp.state.busy&&(window.ScrewApp.state.result?.analysisType==='pose'||window.ScrewApp.state.runStatus==='FAILED'),null,{timeout:180000});
    assert.ok(await page.evaluate(()=>window.ScrewApp.state.result?.analysisType==='pose'),await page.locator('#server-status').textContent());
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    const sample=await page.evaluate(()=>{const state=window.ScrewApp.state,f=state.result.frames.find(f=>f.time>=.75&&f.people.length)||state.result.frames.find(f=>f.people.length);if(!f)return null;const video=document.querySelector('#source-video');video.pause();video.currentTime=f.time;return {time:f.time,count:f.people.length};});
    assert.ok(sample,'Real model must detect people in the fixture');
    await page.waitForFunction(n=>window.ScrewApp.state.drawnBoxes.length===n,sample.count);
    assert.equal(await page.locator('#detection-results tr').count(),sample.count);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.drawnSkeletons),sample.count);
    assert.equal(await page.locator('#screw-verdict').isVisible(),false);
    await page.locator('[data-action=workflow]').click();await page.locator('#modal-title').filter({hasText:'人体检测与骨骼规则'}).waitFor();
    await page.locator('#modal [data-action=close-modal]').first().click();
    await page.locator('[data-action=sop-history]').click();await page.locator('#modal-title').filter({hasText:'人体检测本轮记录'}).waitFor();
    await page.locator('#modal [data-action=close-modal]').first().click();
    await page.screenshot({path:path.join(out,'person-replay.png'),fullPage:true});
    await page.setViewportSize({width:390,height:900});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'Mobile layout must not overflow');
    await page.screenshot({path:path.join(out,'person-mobile.png'),fullPage:true});await page.setViewportSize({width:1512,height:1100});
    await page.locator('#finish-detection').click();
    assert.ok(await page.evaluate(()=>window.ScrewApp.state.final?.final));
    assert.equal(await page.locator('#modal').isVisible(),false,'Pose task should end without an installation verdict');
    await page.locator('#start-live').click();
    await page.waitForFunction(()=>window.ScrewApp.state.running&&window.ScrewApp.state.drawnBoxes.length>0,null,{timeout:90000});
    const realLive=await page.evaluate(()=>({people:window.ScrewApp.state.frame.people.length,boxes:window.ScrewApp.state.drawnBoxes.length,skeletons:window.ScrewApp.state.drawnSkeletons,width:window.ScrewApp.state.samplePicture.width,height:window.ScrewApp.state.samplePicture.height}));
    assert.equal(realLive.boxes,realLive.people);assert.equal(realLive.skeletons,realLive.people);assert.ok(realLive.width<=1280&&realLive.height<=720);
    await page.screenshot({path:path.join(out,'person-live.png'),fullPage:true});
    // Inject a valid empty result to exercise removal while video remains live.
    await page.route('**/api/v1/realtime/frame?analysis_type=pose',route=>route.fulfill({json:{analysisType:'pose',people:[],frameTime:Number(route.request().headers()['x-frame-time']),processingMs:1,model:'empty-fixture'}}));
    await page.waitForFunction(()=>window.ScrewApp.state.frame?.people.length===0&&window.ScrewApp.state.drawnBoxes.length===0&&window.ScrewApp.state.drawnSkeletons===0);
    assert.equal(await page.locator('#detection-results tr').count(),0);
    await page.unroute('**/api/v1/realtime/frame?analysis_type=pose');
    await page.waitForFunction(()=>window.ScrewApp.state.drawnBoxes.length>0,null,{timeout:90000});
    const stale=await page.evaluate(async()=>{const s=window.ScrewApp.state;clearTimeout(s.timer);if(s.pending)await s.pending;clearTimeout(s.timer);s.lastResponse=performance.now()-1000;s.lastSampleAt=performance.now()-1000;window.ScrewApp.render();return {boxes:s.drawnBoxes.length,skeletons:s.drawnSkeletons,rows:document.querySelectorAll('#detection-results tr').length};});
    assert.deepEqual(stale,{boxes:0,skeletons:0,rows:0});
    await page.locator('#finish-detection').click();await page.waitForFunction(()=>!window.ScrewApp.state.busy&&!window.ScrewApp.state.streamId);
    assert.equal(await page.locator('#modal').isVisible(),false);
    for(const task of ['sticker','screw','pose']){
      await page.locator('#inspection-task').selectOption(task);await page.waitForFunction(task=>window.ScrewApp.state.task===task&&!window.ScrewApp.state.busy,task);
      assert.equal(await page.locator('.screw-focus-strip').count(),0);
      if(task==='pose')assert.equal(await page.locator('#detection-results tr').count(),0);
    }
    await page.reload();await page.waitForFunction(()=>window.ScrewApp.state.model&&!window.ScrewApp.state.busy);
    assert.equal(await page.locator('#inspection-task').inputValue(),'pose');
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.result),null);
    assert.deepEqual(errors,[]);
    const report={sample,realLive,checks:['upload-worker-replay','person-boxes-and-skeletons','desktop-mobile','live-camera','empty-frame-clear','expired-result-clear','finish-without-installation-verdict','isolated-task-switching','remembered-task-empty-reload'],pageErrors:errors};
    fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
  }catch(e){if(browser){for(const context of browser.contexts())for(const page of context.pages())await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});}console.error(logs);throw e;}
  finally{if(browser)await browser.close();for(const child of children)child.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
