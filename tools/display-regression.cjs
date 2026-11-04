// Display and workflow regressions, using the API response contract without inference.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let playwright;
try{playwright=require(process.env.PLAYWRIGHT_MODULE||'playwright');}
catch{playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));}
const root=path.resolve(__dirname,'..'),url=process.env.TEST_BASE_URL||'http://localhost:8080';
const output=process.env.TEST_OUTPUT_DIR;
const steps=[{id:'raise',name:'抬手'},{id:'reach',name:'伸手'},{id:'bend',name:'弯腰'},{id:'move',name:'移动'}];
const sequence={workflow:{config:{id:'generic-actions',name:'四步动作观察流程',steps}},scope:'同一人物的动作观察顺序，完成不代表产品合格',
  subjects:[7,8].map(trackId=>({trackId,cycle:1,observed:true,status:'OBSERVING',completed:[{stepId:'raise'}],currentStep:'reach',
    currentStepProgress:{observedSeconds:.2,requiredSeconds:.5,fraction:.4,waitingForRelease:false}})),
  events:[{id:1,trackId:7,cycle:1,code:'STEP_OBSERVED',time:1,detail:'持续观察到抬手'},
    {id:2,trackId:8,cycle:1,code:'OUT_OF_ORDER',time:2,detail:'提前观察到移动，需复核'},
    {id:3,trackId:7,cycle:1,code:'STEP_TIMEOUT',time:8.2,detail:'等待伸手超时 <img src=x onerror=alert(1)>'}],eventCount:3};
const analysis=JSON.parse(fs.readFileSync(path.join(root,'assets/worker-analysis/analysis.json'),'utf8'));
analysis.actionSequence=sequence;
for(const frame of analysis.frames)frame.actionSequence={subjects:sequence.subjects,events:[sequence.events[0]],eventCount:1};
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return{promise,resolve};};
async function checkLayout(page){
  for(const width of [1920,1440,1280,1024,768,390]){
    await page.setViewportSize({width,height:720});
    const layout=await page.evaluate(()=>{
      const body=document.querySelector('.detection-body'),workflow=document.querySelector('.workflow-body');
      const panel=document.querySelector('.workflow-panel'),results=document.querySelector('.analysis-grid');
      return{width:document.documentElement.scrollWidth,viewport:innerWidth,
        bodyScroll:body.scrollHeight,bodyHeight:body.clientHeight,overflow:getComputedStyle(body).overflowY,
        resultBottom:results.getBoundingClientRect().bottom,bodyBottom:body.getBoundingClientRect().bottom,
        workflowBottom:workflow.getBoundingClientRect().bottom,panelBottom:panel.getBoundingClientRect().bottom};
    });
    assert.ok(layout.width<=layout.viewport,`horizontal overflow at ${width}: ${JSON.stringify(layout)}`);
    assert.equal(layout.overflow,'visible',`nested scrolling hides results at ${width}`);
    assert.ok(layout.bodyScroll<=layout.bodyHeight+1,`results exceed their parent at ${width}`);
    assert.ok(layout.resultBottom<=layout.bodyBottom+1,`analysis clipped at ${width}`);
    assert.ok(layout.workflowBottom<=layout.panelBottom+1,`workflow clipped at ${width}`);
    await page.locator('.compliance-panel').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('.detection-body').evaluate(el=>el.scrollTop),0,'page scrolling must expose results');
  }
}
(async()=>{
  const browser=await playwright.chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:720}}),errors=[],healthReady=deferred();
  let liveSequence={...sequence,subjects:[],events:[],eventCount:0};
  page.on('pageerror',error=>errors.push(error.message));
  try{
    await page.addInitScript(()=>{
      window.AISOP_SERVER=true;
      navigator.mediaDevices.getUserMedia=async()=>{
        const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;
        canvas.getContext('2d').fillRect(0,0,640,360);return canvas.captureStream(10);
      };
      navigator.mediaDevices.enumerateDevices=async()=>[];
    });
    await page.route('**/api/v1/**',async route=>{
      const endpoint=new URL(route.request().url()).pathname.replace('/api/v1','');
      if(endpoint==='/health'){await healthReady.promise;return route.fulfill({json:{analysis:'offline-pose'}}).catch(()=>{});}
      if(endpoint==='/session')return route.fulfill({json:{username:'display-user',csrf:'fixture'}});
      if(endpoint==='/stations')return route.fulfill({status:503,json:{message:'工位目录暂不可用'}});
      if(endpoint==='/action-workflows')return route.fulfill({json:{items:[{id:'generic-actions',name:'四步动作观察流程'}]}});
      if(endpoint==='/realtime/frame')return route.fulfill({json:{people:[],processingMs:1,frameTime:.1,actionSequence:liveSequence}});
      if(endpoint.startsWith('/realtime/streams/'))return route.fulfill({json:{released:true}});
      if(endpoint==='/analysis-runs')return route.fulfill({json:{items:[{id:'offline-fixture',media_name:'流程验收视频.mp4',status:'SUCCEEDED',created_at:1700000000}],total:1}});
      if(endpoint==='/analysis-runs/offline-fixture')return route.fulfill({json:{id:'offline-fixture',status:'SUCCEEDED',progress:1,updated_at:1}});
      if(endpoint.endsWith('/artifacts/analysis.json'))return route.fulfill({json:analysis});
      if(endpoint.endsWith('/artifacts/video.webm'))return route.fulfill({status:302,headers:{Location:url+'/assets/worker-analysis/video.webm'},body:''});
      return route.fulfill({status:404,json:{message:'Not found'}});
    });
    await page.goto(url,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('#server-account')?.textContent.includes('display-user'));
    assert.equal(await page.locator('#server-status').textContent(),'正在连接服务器…','controls initialize before the health request completes');
    await page.waitForFunction(()=>document.querySelector('#station-requirements').textContent.includes('工位目录暂不可用'));
    assert.ok(await page.locator('#video-empty').isVisible());
    await page.locator('#station-select').selectOption('algorithm-test');
    assert.ok(await page.locator('#test-controls').isVisible(),'failed catalog must not disable built-in algorithms');
    assert.ok(await page.locator('#action-workflow').isDisabled());
    await page.locator('#pose-model').selectOption('yolo');
    await page.locator('#station-select').selectOption('');
    healthReady.resolve();
    await page.waitForFunction(()=>!document.querySelector('#server-status').textContent.includes('正在连接'));
    await checkLayout(page);
    await page.setViewportSize({width:1280,height:720});
    await page.locator('#video-size').focus();await page.keyboard.press('End');
    await page.locator('.compliance-panel').scrollIntoViewIfNeeded();
    assert.ok(await page.evaluate(()=>scrollY>0),'large picture leaves analysis reachable via page scrolling');
    assert.equal(await page.locator('.detection-body').evaluate(el=>el.scrollTop),0);
    await page.locator('#video-size-reset').click();
    if(output)await page.screenshot({path:path.join(output,'server-empty.png'),fullPage:true});

    await page.locator('#video-mode').selectOption('realtime');
    await page.locator('#live-start').click();
    await page.waitForFunction(()=>document.querySelector('#workflow-status').textContent==='等待人物轨迹');
    assert.equal(await page.locator('#steps .sequence-steps li').count(),4,'waiting for a person must still show the configured steps');
    assert.match(await page.locator('#detection-results').textContent(),/未检出可靠人体/);
    liveSequence=sequence;
    await page.locator('#sequence-subject').waitFor();
    assert.equal(await page.locator('#sequence-subject').count(),1,'mirrored inspection must not duplicate selector IDs');
    assert.equal(await page.locator('#sequence-subject').inputValue(),'8','latest visible subject is selected initially');
    await page.locator('#sequence-subject').focus();
    await page.evaluate(()=>window.focusedSubject=document.activeElement);
    await page.waitForResponse(response=>response.url().includes('/api/v1/realtime/frame'));
    await page.waitForResponse(response=>response.url().includes('/api/v1/realtime/frame'));
    assert.ok(await page.evaluate(()=>document.activeElement===window.focusedSubject),'incoming frames must not interrupt keyboard selection');
    await page.locator('#sequence-subject').selectOption('7');
    assert.match(await page.locator('.latest-focused .station-check-head').first().textContent(),/人物 7/);
    await page.locator('.workflow-panel .section-title [data-action="sop-history"]').click();
    assert.equal(await page.locator('#sequence-history-rows tr').count(),3);
    assert.equal(await page.locator('#sequence-history-rows img').count(),0,'history details render as text');
    await page.locator('#sequence-history-track').selectOption('7');
    assert.equal(await page.locator('#sequence-history-rows tr').count(),2);
    assert.equal(await page.locator('[data-sequence-seek]').count(),0,'unsaved camera frames have no replay button');
    await page.locator('#modal [data-action="close-modal"]').first().click();
    await page.locator('#live-stop').click();
    await checkLayout(page);
    await page.setViewportSize({width:1280,height:720});
    if(output)await page.screenshot({path:path.join(output,'realtime-workflow.png'),fullPage:true});
    // A new stream cannot reuse the previous stream's evidence.
    liveSequence={...sequence,subjects:[],events:[],eventCount:0};
    await page.locator('#live-start').click();
    await page.waitForFunction(()=>document.querySelector('#workflow-status').textContent==='等待人物轨迹');
    await page.locator('.workflow-panel .section-title [data-action="sop-history"]').click();
    assert.match(await page.locator('#sequence-history-summary').textContent(),/显示 0 条/);
    await page.locator('#modal [data-action="close-modal"]').first().click();
    await page.locator('#video-mode').selectOption('replay');
    await page.locator('.workflow-panel .section-title [data-action="sop-history"]').click();
    assert.match(await page.locator('#modal-body').textContent(),/当前没有动作流程记录/);
    await page.locator('#modal [data-action="close-modal"]').first().click();

    await page.locator('[data-action="server-history"]').first().click();
    await page.locator('[data-server-run="offline-fixture"]').click();
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2&&document.querySelector('#mode-text').textContent.includes('服务器实际'));
    await page.locator('#source-video').evaluate(video=>video.pause());
    await checkLayout(page);
    await page.setViewportSize({width:1280,height:720});
    if(output)await page.screenshot({path:path.join(output,'offline-results.png'),fullPage:true});
    await page.locator('.workflow-panel .section-title [data-action="sop-history"]').click();
    assert.equal(await page.locator('#sequence-history-rows tr').count(),3,'offline history contains all events, including those outside the current frame');
    await page.locator('#sequence-history-track').selectOption('7');
    for(const viewport of [{width:390,height:844},{width:1280,height:420}]){
      await page.setViewportSize(viewport);
      const visible=await page.locator('#modal .modal-heading .icon-button').evaluate(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;});
      assert.ok(visible,'dialog close remains reachable');
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    }
    await page.setViewportSize({width:390,height:844});
    if(output)await page.screenshot({path:path.join(output,'workflow-history-mobile.png'),fullPage:true});
    await page.locator('[data-sequence-seek]').first().click();
    await page.waitForFunction(()=>Math.abs(document.querySelector('#source-video').currentTime-8.2)<.05);
    assert.ok(await page.locator('#source-video').evaluate(video=>video.paused));
    assert.equal(await page.locator('#temporal-track').inputValue(),'7');
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#pose-model')?.value==='yolo');
    assert.deepEqual(errors,[]);

    const guest=await browser.newPage();guest.on('pageerror',error=>errors.push(error.message));
    await guest.addInitScript(()=>window.AISOP_SERVER=true);
    await guest.route('**/api/v1/**',route=>{
      const endpoint=new URL(route.request().url()).pathname;
      return route.fulfill(endpoint.endsWith('/session')?{status:401,json:{message:'请登录'}}:
        {json:endpoint.endsWith('/health')?{analysis:'offline-pose'}:{items:[]}});
    });
    await guest.goto(url);await guest.locator('#video-mode').selectOption('realtime');
    await guest.locator('#server-login').waitFor();
    assert.equal(await guest.locator('#video-mode').inputValue(),'replay','cancelled login does not leave an inactive camera mode selected');
    await guest.locator('#modal [data-action="close-modal"]').first().click();
    assert.equal(await guest.locator('#inline-live').count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS: unclipped desktop/mobile/large-picture layouts, immediate controls and catalog fallback, per-person workflow, history filtering/replay/reset, escaped evidence, camera login cancellation.');
  }catch(error){if(output)await page.screenshot({path:path.join(output,'display-failure.png'),fullPage:true}).catch(()=>{});throw error;}
  finally{healthReady.resolve();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
