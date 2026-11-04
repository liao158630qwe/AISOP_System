// Real inspection and person models, upload/worker/replay and recorded live camera.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {spawn,spawnSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const playwright=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),python=resolvePython();
const task=process.env.INSPECTION_PERSON_TEST_TASK||'screw';
assert.ok(['screw','sticker'].includes(task),'Unsupported inspection task');
const backend=process.env.INSPECTION_PERSON_TEST_BACKEND||process.env.SCREW_PERSON_TEST_BACKEND||'yolo',externalBase=process.env.INSPECTION_PERSON_TEST_BASE||process.env.SCREW_PERSON_TEST_BASE||'';
assert.ok(['yolo','rtmo','rtmpose'].includes(backend),'Unsupported person backend');
assert.ok(!externalBase||/^http:\/\/127\.0\.0\.1:\d+$/.test(externalBase),'Existing service must be on loopback');
const credentials=externalBase?JSON.parse(fs.readFileSync(path.join(root,'.runtime','local-settings.json'),'utf8')):null;
const runDir=path.join(root,'.runtime',task+'-person-ui-'+Date.now()),out=path.join(root,'outputs',task+'-person-detection-2026-10-28',...(externalBase?['local-service']:backend==='yolo'?[]:[backend]));
fs.mkdirSync(runDir,{recursive:true});fs.mkdirSync(out,{recursive:true});
const port=Number(process.env.INSPECTION_PERSON_TEST_PORT||process.env.SCREW_PERSON_TEST_PORT||8064),base=externalBase||'http://127.0.0.1:'+port;
const env={...process.env,APP_ENV:'development',COOKIE_SECURE:'false',APP_USERNAME:credentials?.username||'screw-person-test',APP_PASSWORD:credentials?.password||'local-screw-person-test-12345',
  DATABASE_URL:'sqlite:///'+path.join(runDir,'test.db').replaceAll('\\','/'),DATA_DIR:path.join(runDir,'data'),
  MODEL_PATH:path.join(root,'models','yolo11s-pose.pt'),POSE_BACKEND:backend,REALTIME_BACKEND:backend,REALTIME_POOL_SIZE:'1',REALTIME_WARMUP:'false',
  TEMP:runDir,TMP:runDir,YOLO_CONFIG_DIR:path.join(runDir,'ultralytics'),PYTHONUNBUFFERED:'1'};
const fixture=spawnSync(python,['-c',`
import cv2,json,sys
from pathlib import Path
out=Path(sys.argv[1]); task=sys.argv[2]; root=Path('outputs')/(task+'-inspection-2026-10-17')
if task=='screw':
    source=json.loads((root/'source.json').read_text(encoding='utf-8'))['samples']; times=(50.53,50.76,51.03); folder='frames'; size=(1920,1080)
else:
    source=json.loads((root/'action-samples.json').read_text(encoding='utf-8')); times=(22.9,23.,23.133); folder='action-frames'; size=(1080,1920)
samples=[min(source,key=lambda s:abs(s['time']-t)) for t in times]
images=[cv2.resize(cv2.imread(str(root/folder/s['file'])),size) for s in samples]
writer=cv2.VideoWriter(str(out/'assembly.avi'),cv2.VideoWriter_fourcc(*'MJPG'),4,size); assert writer.isOpened()
with (out/'camera.y4m').open('wb') as raw:
    raw.write(f'YUV4MPEG2 W{size[0]} H{size[1]} F4:1 Ip A1:1 C420jpeg\\n'.encode())
    for image in images*4:
        writer.write(image); raw.write(b'FRAME\\n'); raw.write(cv2.cvtColor(image,cv2.COLOR_BGR2YUV_I420).tobytes())
writer.release()
`,runDir,task],{cwd:root,env,windowsHide:true,encoding:'utf8'});
assert.equal(fixture.status,0,fixture.stderr);
const children=[];let browser,logs='';
function start(args){const child=spawn(python,args,{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});children.push(child);for(const stream of [child.stdout,child.stderr])stream.on('data',data=>{logs=(logs+data).slice(-12000);});return child;}
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  try{
    if(!externalBase)start(['-c',`from backend.app import create_app; from backend.db import initialize; import uvicorn; app=create_app(); initialize(app.state.engine); uvicorn.run(app,host='127.0.0.1',port=${port},log_level='warning')`]);
    let ready=false;
    for(let i=0;i<100;i++){try{ready=(await fetch(base+'/api/v1/health')).ok;if(ready)break;}catch{}await delay(250);}
    assert.ok(ready,logs);if(!externalBase)start(['-m','backend.worker']);
    browser=await playwright.chromium.launch({channel:'msedge',headless:true,env,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--use-file-for-fake-video-capture='+path.join(runDir,'camera.y4m')]});
    const context=await browser.newContext({viewport:{width:1512,height:1100},permissions:['camera']});
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/?task='+task);await page.waitForFunction(()=>window.ScrewApp?.state.model?.ready&&!window.ScrewApp.state.busy);
    assert.match(await page.locator('.brand p').textContent(),/人体框与17点骨骼/);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.model.skeleton.jointCount),17);
    if(task==='sticker')assert.match(await page.locator('.pose-legend').textContent(),/当前贴放\/按压候选.*人体17点骨骼/);
    await page.locator('.toolbar [data-action=upload]').click();
    await page.locator('#login-user').fill(env.APP_USERNAME);await page.locator('#login-password').fill(env.APP_PASSWORD);
    await page.locator('#screw-login button[type=submit]').click();
    await page.locator('#screw-upload-file').setInputFiles(path.join(runDir,'assembly.avi'));
    await page.locator('#screw-upload-start').click();
    await page.waitForFunction(task=>!window.ScrewApp.state.busy&&(window.ScrewApp.state.result?.analysisType===task||window.ScrewApp.state.runStatus==='FAILED'),task,{timeout:180000});
    assert.ok(await page.evaluate(task=>window.ScrewApp.state.result?.analysisType===task,task),await page.locator('#server-status').textContent());
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    const sample=await page.evaluate(()=>{const s=window.ScrewApp.state,f=s.result.frames.find(f=>f.people.length&&f.inspection.displayTargets.length);if(!f)return null;const v=document.querySelector('#source-video');v.pause();v.currentTime=f.time;return {time:f.time,people:f.people.length,targets:f.inspection.displayTargets.length};});
    assert.ok(sample,'Real assembly sample must contain person and inspection detections');
    await page.waitForFunction(s=>window.ScrewApp.state.drawnSkeletons===s.people&&window.ScrewApp.state.drawnBoxes.length===s.people+s.targets,sample);
    await page.screenshot({path:path.join(out,task+'-people-replay.png'),fullPage:true});
    const offline=await page.evaluate(()=>{const r=window.ScrewApp.state.result;return {type:r.analysisType,skeleton:r.skeleton.jointCount,maxPeople:r.summary.maxPeople,personBackend:r.personModel.backend,personModel:r.personModel.name,width:r.source.width,height:r.source.height};});
    assert.equal(offline.type,task);assert.equal(offline.skeleton,17);assert.ok(offline.maxPeople>0);assert.equal(offline.personBackend,backend);
    if(task==='sticker')assert.ok(offline.height>offline.width,'Portrait source must stay portrait');
    await page.locator('#start-live').click();
    await page.waitForFunction(()=>window.ScrewApp.state.running&&window.ScrewApp.state.drawnSkeletons>0&&window.ScrewApp.state.drawnBoxes.some(b=>b.className!=='person'),null,{timeout:90000});
    const live=await page.evaluate(()=>{const s=window.ScrewApp.state;return {task:s.task,people:s.frame.people.length,skeletons:s.drawnSkeletons,targets:s.drawnBoxes.filter(b=>b.className!=='person').length,frameTime:s.frame.time,width:s.capture.width,height:s.capture.height};});
    assert.equal(live.task,task);assert.equal(live.people,live.skeletons);assert.ok(live.targets>0);
    if(task==='sticker')assert.ok(live.height>live.width,'Portrait camera sample must stay portrait');
    await page.screenshot({path:path.join(out,task+'-people-live.png'),fullPage:true});
    await page.route('**/api/v1/realtime/frame?analysis_type='+task,async route=>{const response=await route.fetch();const body=await response.json();body.people=[];await route.fulfill({response,json:body});});
    await page.waitForFunction(()=>window.ScrewApp.state.frame.people.length===0&&window.ScrewApp.state.drawnSkeletons===0&&window.ScrewApp.state.drawnBoxes.every(b=>b.className!=='person'));
    await page.unroute('**/api/v1/realtime/frame?analysis_type='+task);
    await page.waitForFunction(()=>window.ScrewApp.state.drawnSkeletons>0,null,{timeout:90000});
    const stale=await page.evaluate(async()=>{const s=window.ScrewApp.state;clearTimeout(s.timer);if(s.pending)await s.pending;clearTimeout(s.timer);s.lastResponse=performance.now()-1000;s.lastSampleAt=performance.now()-1000;window.ScrewApp.render();return {boxes:s.drawnBoxes.length,skeletons:s.drawnSkeletons};});
    assert.deepEqual(stale,{boxes:0,skeletons:0});
    await page.locator('#finish-detection').click();await page.waitForFunction(()=>!window.ScrewApp.state.busy&&!window.ScrewApp.state.streamId);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.drawnSkeletons),0);
    assert.deepEqual(errors,[]);
    const report={base,backend,sample,offline,live,checks:['same-'+task+'-task','actual-inspection-and-person-models','upload-worker-replay','recorded-camera','empty-person-clear','expired-result-clear','finish-clear',...(task==='sticker'?['portrait-source-and-camera']:[])],pageErrors:errors};
    fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
  }catch(e){if(browser){for(const c of browser.contexts())for(const p of c.pages())await p.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});}console.error(logs);throw e;}
  finally{if(browser)await browser.close();for(const child of children)child.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
