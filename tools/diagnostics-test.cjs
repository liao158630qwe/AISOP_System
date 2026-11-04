// Diagnostic dialogs must describe the active workspace, not static demo defaults.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let playwright;
try{playwright=require(process.env.PLAYWRIGHT_MODULE||'playwright');}
catch{playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));}
const root=path.resolve(__dirname,'..'),url=process.env.TEST_BASE_URL||'http://localhost:8080',output=process.env.TEST_OUTPUT_DIR;
const analysis=JSON.parse(fs.readFileSync(path.join(root,'assets/worker-analysis/analysis.json'),'utf8'));
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return{promise,resolve};};
(async()=>{
  const browser=await playwright.chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[],heldHealth=deferred();
  let healthFailed=false,holdNextHealth=false;
  const health={status:'ready',analysis:'offline-pose',sop:'not-implemented',
    realtime:{state:'not_loaded',model:'RTMO-m',backend:'rtmo',device:null},
    capabilities:{personDetection:true,poseEstimation:true,actionSequence:true,productionSop:false}};
  const systemValue=label=>page.locator('.system-item').filter({has:page.getByText(label,{exact:true})}).locator('strong').textContent();
  const close=()=>page.locator('#modal .modal-heading [data-action="close-modal"]').click();
  page.on('pageerror',error=>errors.push(error.message));
  try{
    await page.addInitScript(()=>{
      window.AISOP_SERVER=true;
      // Existence of localStorage does not imply configuration can be saved.
      Storage.prototype.setItem=()=>{throw new DOMException('Blocked','SecurityError');};
      navigator.mediaDevices.getUserMedia=async()=>{
        const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;
        canvas.getContext('2d').fillRect(0,0,640,360);return canvas.captureStream(10);
      };
      navigator.mediaDevices.enumerateDevices=async()=>[];
    });
    await page.route('**/api/v1/**',async route=>{
      const endpoint=new URL(route.request().url()).pathname.replace('/api/v1','');
      if(endpoint==='/health'){
        if(holdNextHealth){holdNextHealth=false;await heldHealth.promise;}
        return route.fulfill(healthFailed?{status:503,json:{message:'数据库暂不可用'}}:{json:health}).catch(()=>{});
      }
      if(endpoint==='/session')return route.fulfill({json:{username:'diagnostic-user',csrf:'fixture'}});
      if(endpoint==='/stations')return route.fulfill({json:{items:[]}});
      if(endpoint==='/action-workflows')return route.fulfill({json:{items:[{id:'generic-actions',name:'动作顺序观察'}]}});
      if(endpoint==='/realtime/frame')return route.fulfill({json:{people:[],width:640,height:360,processingMs:7.25,
        model:'RTMO-live-test',device:'cuda:1',backend:'rtmlib',frameTime:.1}});
      if(endpoint.startsWith('/realtime/streams/'))return route.fulfill({json:{released:true}});
      if(endpoint==='/analysis-runs')return route.fulfill({json:{items:[{id:'diagnostic-fixture',media_name:'工厂视频.mp4',status:'SUCCEEDED',created_at:1700000000}],total:1}});
      if(endpoint==='/analysis-runs/diagnostic-fixture')return route.fulfill({json:{id:'diagnostic-fixture',status:'SUCCEEDED',progress:1,updated_at:1}});
      if(endpoint.endsWith('/artifacts/analysis.json'))return route.fulfill({json:analysis});
      if(endpoint.endsWith('/artifacts/video.webm'))return route.fulfill({status:302,headers:{Location:url+'/assets/worker-analysis/video.webm'},body:''});
      return route.fulfill({status:404,json:{message:'Not found'}});
    });
    await page.goto(url);
    await page.waitForFunction(()=>document.querySelector('#server-account')?.textContent.includes('diagnostic-user'));
    await page.waitForFunction(()=>window.ServerApp.runtime.health?.status==='ready');
    await page.locator('[data-action="system"]').first().click();
    assert.equal(await systemValue('运行模式'),'服务器视频分析');
    assert.equal(await systemValue('分析服务'),'已连接');
    assert.equal(await systemValue('视频源'),'未选择视频');
    assert.match(await systemValue('实时姿态模型'),/RTMO-m.*按需加载/);
    assert.doesNotMatch(await page.locator('#modal-body').textContent(),/0 × 0|自动判定需接入/);
    if(output)await page.locator('#modal').screenshot({path:path.join(output,'system-server-idle.png')});
    await page.locator('#modal [data-action="checks"]').click();
    assert.equal(await page.locator('[data-check="source"] .check-state').textContent(),'待选择');
    assert.equal(await page.locator('[data-check="people"] .check-state').textContent(),'已接入');
    assert.equal(await page.locator('[data-check="pose"] .check-state').textContent(),'已接入');
    assert.equal(await page.locator('[data-check="sop"] .check-state').textContent(),'待人工复核');
    assert.equal(await page.locator('[data-check="storage"] .check-state').textContent(),'不可用');
    if(output)await page.locator('#modal').screenshot({path:path.join(output,'checks-server-idle.png')});
    await close();

    // Keep an offline result loaded to catch stale playback metadata in camera mode.
    await page.locator('[data-action="server-history"]').first().click();
    await page.locator('[data-server-run="diagnostic-fixture"]').click();
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2&&document.querySelector('#mode-text').textContent.includes('服务器实际'));
    await page.locator('[data-action="system"]').first().click();
    assert.equal(await systemValue('运行模式'),'服务器离线分析回放');
    assert.equal(await systemValue('识别模型'),analysis.model.name);
    assert.equal(await systemValue('视频源分辨率'),`${analysis.source.width} × ${analysis.source.height}`);
    assert.equal(await systemValue('分析帧数'),String(analysis.summary.analyzedFrames));
    await close();
    await page.locator('#video-mode').selectOption('realtime');
    await page.locator('#live-start').click();
    await page.waitForFunction(()=>document.querySelector('#live-status').textContent.startsWith('检测到'));
    await page.locator('[data-action="system"]').first().click();
    assert.equal(await systemValue('运行模式'),'实时摄像头检测');
    assert.equal(await systemValue('识别模型'),'RTMO-live-test');
    assert.equal(await systemValue('推理设备'),'cuda:1');
    assert.equal(await systemValue('检测状态'),'检测中');
    assert.equal(await systemValue('最近服务端处理'),'7.3 ms');
    assert.ok(Number(await systemValue('本轮检测帧数'))>=1);
    assert.doesNotMatch(await page.locator('#modal-body').textContent(),/YOLO11s-pose|本地视频回放|0 × 0/);
    if(output)await page.locator('#modal').screenshot({path:path.join(output,'system-realtime.png')});
    await page.locator('#modal [data-action="checks"]').click();
    assert.equal(await page.locator('[data-check="source"] .check-state').textContent(),'已就绪');
    assert.equal(await page.locator('[data-check="people"] .check-state').textContent(),'已验证');
    assert.equal(await page.locator('[data-check="pose"] .check-state').textContent(),'已验证');
    assert.match(await page.locator('[data-check="pose"] .check-desc').textContent(),/RTMO-live-test/);
    for(const viewport of [{width:390,height:844},{width:1280,height:420}]){
      await page.setViewportSize(viewport);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert.ok(await page.locator('#modal .modal-heading .icon-button').evaluate(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;}));
      await page.locator('#modal [data-action="run-checks"]').scrollIntoViewIfNeeded();
      assert.ok(await page.locator('#modal [data-action="run-checks"]').isVisible());
    }
    await page.setViewportSize({width:1280,height:900});
    await close();await page.locator('#live-stop').click();
    await page.locator('[data-action="system"]').first().click();
    assert.equal(await systemValue('检测状态'),'已停止');
    assert.equal(await systemValue('识别模型'),'RTMO-live-test');
    assert.doesNotMatch(await systemValue('摄像头画面分辨率'),/0 × 0/);
    await page.locator('#modal [data-action="checks"]').click();
    assert.equal(await page.locator('[data-check="source"] .check-state').textContent(),'已停止');
    await close();await page.locator('#video-mode').selectOption('replay');

    healthFailed=true;
    await page.locator('.check-chip').click();
    await page.waitForFunction(()=>document.querySelector('[data-check="people"] .check-state').textContent==='连接失败');
    assert.equal(await page.locator('[data-check="pose"] .check-state').textContent(),'连接失败');
    assert.match(await page.locator('[data-check="people"] .check-desc').textContent(),/数据库暂不可用/);
    await close();
    healthFailed=false;holdNextHealth=true;
    const healthRequest=page.waitForRequest(request=>request.url().endsWith('/api/v1/health'));
    await page.locator('[data-action="system"]').first().click();await healthRequest;
    await page.locator('#modal [data-action="checks"]').click();
    await page.waitForFunction(()=>document.querySelector('[data-check="pose"] .check-state').textContent==='已接入');
    const oldResponse=page.waitForResponse(response=>response.url().endsWith('/api/v1/health'));
    heldHealth.resolve();await oldResponse;
    assert.equal(await page.locator('#modal-title').textContent(),'运行环境自检','late system request must not overwrite a different dialog');
    assert.ok(await page.locator('[data-check="pose"]').isVisible());
    assert.deepEqual(errors,[]);
    console.log('PASS: server idle/live/stopped/offline diagnostic modes, actual model/device/sample data, blocked storage, expected idle source, failed health, dialog response race and responsive controls.');
  }catch(error){if(output)await page.screenshot({path:path.join(output,'diagnostics-failure.png'),fullPage:true}).catch(()=>{});throw error;}
  finally{heldHealth.resolve();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
