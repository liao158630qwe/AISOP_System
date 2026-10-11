// Browser regressions with explicitly mocked API responses (no inference).
const assert=require('node:assert/strict');
const path=require('node:path');
let playwright;
try{playwright=require(process.env.PLAYWRIGHT_MODULE||'playwright');}
catch{playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));}
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
(async()=>{
  const browser=await playwright.chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const heldRun=deferred(),heldUpload=deferred();
  let runRequests=0,uploadRequests=0,created=0;
  const items=Array.from({length:42},(_,i)=>({id:'task-'+String(i).padStart(3,'0'),media_name:`装配视频-${i}.mp4`,
    status:i%2?'FAILED':'QUEUED',progress:0,created_at:1700000000+i,error_message:null}));
  try{
    await page.addInitScript(()=>{
      window.AISOP_SERVER=true;
      // Station controls must also work when browser storage is blocked.
      Storage.prototype.getItem=()=>{throw new DOMException('Blocked','SecurityError');};
      Storage.prototype.setItem=()=>{throw new DOMException('Blocked','SecurityError');};
      const send=XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.send=function(...args){window.testUploadXHR=this;return send.apply(this,args);};
    });
    await page.route('**/api/v1/**',async route=>{
      const url=new URL(route.request().url()),endpoint=url.pathname.replace('/api/v1','');
      if(endpoint==='/health')return route.fulfill({json:{analysis:'offline-pose',limits:{max_upload_bytes:5242880,max_video_seconds:120}}});
      if(endpoint==='/session')return route.fulfill({json:{username:'test-user',csrf:'fixture'}});
      if(endpoint==='/stations')return route.fulfill({json:{items:[]}});
      if(endpoint==='/action-workflows')return route.fulfill({json:{items:[{id:'generic-actions',name:'抬手 → 伸手 → 弯腰 → 移动'}]}});
      if(endpoint==='/analysis-runs'&&route.request().method()==='GET'){
        const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||100);
        const q=url.searchParams.get('q')||'',status=url.searchParams.get('status');
        const filtered=items.filter(item=>(item.media_name.includes(q)||item.id.includes(q))&&(!status||status===item.status));
        return route.fulfill({json:{items:filtered.slice(offset,offset+limit),total:filtered.length,offset,limit}});
      }
      if(endpoint.startsWith('/analysis-runs/task-')){
        const first=++runRequests===1;
        if(first)await heldRun.promise;
        return route.fulfill({json:{...items[0],status:first?'FAILED':'CANCELLED',error_message:first?'stale response':null}}).catch(()=>{});
      }
      if(endpoint==='/media'){
        uploadRequests++;await heldUpload.promise;
        return route.fulfill({status:201,json:{id:'uploaded-fixture',metadata:{fps:30}}}).catch(()=>{});
      }
      if(endpoint==='/analysis-runs'&&route.request().method()==='POST'){created++;return route.fulfill({status:202,json:{id:'new-task'}});}
      return route.fulfill({status:404,json:{message:'Not found'}});
    });
    await page.goto(process.env.TEST_BASE_URL||'http://localhost:8080');
    await page.waitForFunction(()=>document.querySelector('#server-account')?.textContent.includes('test-user'));
    assert.equal(await page.locator('#station-select').inputValue(),'','first visit defaults to upload-compatible pose mode');
    assert.match(await page.locator('#station-requirements').textContent(),/人体与姿态检测/);
    assert.ok(await page.locator('#test-controls').isHidden());
    // Resize real layout through keyboard and pointer input, including camera mode.
    const pictureHeight=selector=>page.locator(selector).evaluate(el=>Math.round(el.getBoundingClientRect().height));
    assert.equal(await pictureHeight('#video-stage'),300);
    await page.locator('#video-size').focus();
    await page.keyboard.press('End');
    assert.equal(await pictureHeight('#video-stage'),720);
    await page.locator('#video-mode').selectOption('realtime');
    assert.equal(await pictureHeight('.live-picture'),720);
    await page.evaluate(()=>{
      const person={trackId:7,action:'raised',label:'抬手'};
      renderLiveTimeline([person],0);
      renderLiveTimeline([person],.1);
      renderLiveTimeline([{...person,trackId:8}],31);
    });
    assert.equal(await page.locator('#temporal-track').inputValue(),'7','missing person must not switch to another track');
    assert.match(await page.locator('#temporal-state').textContent(),/未检出/);
    assert.equal(await page.locator('#time-bars span').count(),0,'gaps must not imply continued action');
    await page.evaluate(()=>resetLiveTimeline());
    assert.equal(await page.locator('#temporal-track option').count(),0);
    await page.locator('#video-size-reset').click();
    assert.equal(await pictureHeight('.live-picture'),300);
    const grip=page.locator('#video-resize-handle');
    await grip.scrollIntoViewIfNeeded();
    const box=await grip.boundingBox();
    await page.mouse.move(box.x+box.width/2,box.y+box.height/2);
    await page.mouse.down();await page.mouse.move(box.x+box.width/2,box.y+box.height/2+80,{steps:8});await page.mouse.up();
    assert.equal(await pictureHeight('.live-picture'),380);
    await page.locator('#video-mode').selectOption('replay');
    assert.equal(await pictureHeight('#video-stage'),380);
    await page.setViewportSize({width:390,height:900});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.locator('#video-size-reset').click();
    assert.equal(await pictureHeight('#video-stage'),220);
    await page.setViewportSize({width:1440,height:1000});
    assert.equal(await pictureHeight('#video-stage'),300);
    await page.locator('[data-action="server-history"]').first().click();
    await page.waitForFunction(()=>document.querySelectorAll('[data-server-run]').length===20);
    assert.match(await page.locator('#history-summary').textContent(),/共 42/);
    await page.locator('#history-next').click();
    await page.locator('[data-server-run="task-020"]').waitFor();
    await page.locator('#history-prev').click();
    await page.locator('[data-server-run="task-000"]').waitFor();
    await page.locator('#history-filter').selectOption('FAILED');
    await page.waitForFunction(()=>document.querySelector('#history-summary').textContent.includes('共 21'));
    await page.locator('#history-search').fill('missing');
    await page.waitForFunction(()=>document.querySelector('#history-summary').textContent==='共 0 个任务');
    await page.locator('#history-search').fill('');
    await page.locator('#history-filter').selectOption('');
    await page.locator('[data-server-run="task-000"]').waitFor();
    // Select the same task twice while its first request is still in flight.
    await page.locator('[data-server-run="task-000"]').click();
    await page.waitForFunction(()=>document.querySelector('#server-status').textContent==='正在读取任务…');
    await page.locator('[data-action="server-history"]').first().click();
    await page.locator('[data-server-run="task-000"]').click();
    await page.waitForFunction(()=>document.querySelector('#server-status').textContent.startsWith('已取消'));
    heldRun.resolve();
    await page.waitForTimeout(300);
    assert.match(await page.locator('#server-status').textContent(),/^已取消/);
    assert.ok(await page.locator('#server-cancel').isHidden());
    assert.ok(runRequests>=2);
    await page.locator('[data-action="upload"]').first().click();
    assert.match(await page.locator('#server-drop').textContent(),/5 MiB、2 分钟/);
    await page.locator('#video-upload').setInputFiles({name:'upload.mp4',mimeType:'video/mp4',buffer:Buffer.alloc(1024*1024,1)});
    await page.locator('#server-upload-cancel').waitFor();
    // Intercepted requests do not reach the transport; supply a browser progress
    // event to exercise the UI while the mock server deliberately holds its reply.
    await page.evaluate(()=>window.testUploadXHR.upload.dispatchEvent(new ProgressEvent('progress',{
      lengthComputable:true,loaded:524288,total:1048576,
    })));
    await page.waitForFunction(()=>document.querySelector('#server-progress').value>0);
    assert.equal(await page.locator('#server-progress').evaluate(el=>el.value),50);
    assert.equal(uploadRequests,1);
    await page.locator('#video-mode').selectOption('realtime');
    assert.equal(await page.locator('#video-mode').inputValue(),'replay','upload keeps its active workspace');
    assert.equal(await page.locator('#inline-live').count(),0);
    await page.locator('#server-upload-cancel').click();
    await page.waitForFunction(()=>document.querySelector('#server-status').textContent==='上传已取消');
    heldUpload.resolve();
    await page.waitForTimeout(300);
    assert.equal(created,0,'cancelled upload must not create an analysis run');
    assert.ok(await page.locator('#server-progress').isHidden());
    await page.locator('[data-action="server-history"]').first().click();
    await page.locator('[data-server-run]').first().waitFor();
    for(const width of [1440,1024,768,390]){
      await page.setViewportSize({width,height:900});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`overflow at ${width}`);
      assert.ok(await page.locator('#modal').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
      assert.ok(await page.locator('#history-next').evaluate(el=>{const r=el.getBoundingClientRect();return r.bottom<=innerHeight&&r.top>=0;}),'pagination stays in the viewport');
    }
    if(process.env.TEST_OUTPUT_DIR)await page.screenshot({path:path.join(process.env.TEST_OUTPUT_DIR,'history-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: mocked API pagination/filter/search, same-task response race, upload progress/cancel, blocked storage and responsive history.');
  }finally{heldRun.resolve();heldUpload.resolve();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
