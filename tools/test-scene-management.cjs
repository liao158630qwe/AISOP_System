// Real browser + isolated API + real model inference. Run through the Python harness.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.SCENE_TEST_BASE,output=process.env.SCENE_TEST_OUTPUT;
if(!base||!output)throw new Error('Run node tools/python.cjs tools/test-scene-management.py');

(async()=>{
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true,args:[
    '--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',
    '--use-file-for-fake-video-capture='+path.join(output,'camera.y4m')]});
  const context=await browser.newContext({viewport:{width:1440,height:1050},permissions:['camera']});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(20000);
  const screenshot=name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
  const noOverflow=()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);
  let modelId,sceneId,runId;
  try{
    await page.goto(base+'/admin');
    await page.locator('#workspace-login').waitFor({state:'visible'});
    await page.locator('#workspace-login [name=username]').fill(process.env.APP_USERNAME);
    await page.locator('#workspace-login [name=password]').fill(process.env.APP_PASSWORD);
    await page.locator('#workspace-login button[type=submit]').click();
    await page.locator('#admin-content').waitFor({state:'visible'});
    await page.locator('#models-tab').click();
    await page.locator('#import-model-button').click();
    assert.equal(await page.locator('#import-form input[value=upload]').isChecked(),true);
    await page.locator('#import-form input[value=local]').check();
    await page.locator('#candidate-select option[value="models/screw-head-v1.pt"]').waitFor({state:'attached'});
    await page.locator('#candidate-select').selectOption('models/screw-head-v1.pt');
    await page.locator('#model-name').fill('螺丝检测 · 浏览器验证');
    await page.locator('#model-version').fill('1.0');
    await page.locator('#import-submit').click();
    await page.waitForFunction(()=>window.AdminWorkspace.state.models.length===1,null,{timeout:120000});
    modelId=await page.evaluate(()=>window.AdminWorkspace.state.models[0].id);
    await page.locator('#import-result').waitFor({state:'visible'});
    await page.locator('#import-view-model').click();
    await page.locator('#model-search').fill('');
    await screenshot('models');
    await page.locator('#model-search').fill('no-matching-model');
    await page.locator('#model-no-results').waitFor({state:'visible'});
    await page.locator('#clear-model-filters').click();
    assert.equal(await page.locator('#model-rows tr').count(),1);

    await page.locator(`[data-model-use="${modelId}"]`).click();
    await page.locator('#scene-name').fill('新场景螺丝检测');
    await page.locator('#scene-description').fill('真实训练权重、区域规则与视频检测链路验证');
    await page.locator('#scene-device').selectOption('cuda');
    await page.locator('#scene-sharpness').fill('0');
    await page.locator('#roi-image').setInputFiles(path.join(output,'reference.jpg'));
    await page.locator('#roi-canvas').waitFor({state:'visible'});
    await page.locator('#roi-canvas').scrollIntoViewIfNeeded();
    const box=await page.locator('#roi-canvas').boundingBox();
    await page.mouse.move(box.x+box.width*.1,box.y+box.height*.1);
    await page.mouse.down();await page.mouse.move(box.x+box.width*.9,box.y+box.height*.9,{steps:8});await page.mouse.up();
    const roi=await page.evaluate(()=>window.AdminWorkspace.state.rules[0].roi);
    assert.ok(roi[0]>.09&&roi[2]<.91,'ROI drawing must update the saved rule');
    await screenshot('rule-editor');
    await page.locator('#scene-form button[value=draft]').click();
    await page.waitForFunction(()=>window.AdminWorkspace.state.scenes.length===1);
    sceneId=await page.evaluate(()=>window.AdminWorkspace.state.scenes[0].id);
    assert.equal((await(await context.request.get(base+'/api/v1/scenes')).json()).items.length,0);
    await page.locator('[data-task-filter="draft"]').click();
    await page.locator('#task-search').fill('no-matching-scene');
    await page.locator('#task-no-results').waitFor({state:'visible'});
    await page.locator('#clear-task-filters').click();
    await page.locator(`[data-scene-edit="${sceneId}"]`).first().click();
    await page.locator('#scene-form button[value=published]').click();
    await page.waitForFunction(()=>window.AdminWorkspace.state.scenes[0].status==='published');
    await screenshot('published-tasks');
    await page.setViewportSize({width:390,height:844});assert.ok(await noOverflow());await screenshot('admin-mobile');
    await page.setViewportSize({width:1440,height:1050});

    // Uploading a new weight file registers a separate version.
    await page.locator('#models-tab').click();await page.locator('#import-model-button').click();
    await page.locator('#import-form input[value=upload]').check();
    await page.locator('#model-file').setInputFiles(path.resolve('models/screw-head-v1.pt'));
    await page.locator('#model-name').fill('上传模型');await page.locator('#model-version').fill('2.0');
    await page.locator('#import-submit').click();
    await page.waitForFunction(()=>window.AdminWorkspace.state.models.length===2,null,{timeout:120000});
    await page.locator('#import-result').waitFor({state:'visible'});
    await screenshot('upload-success');
    const uploaded=await page.evaluate(()=>window.AdminWorkspace.state.models.find(m=>m.name==='上传模型'));
    await page.locator('#import-train-model').click();
    await page.waitForFunction(id=>document.querySelector('#training-pretrained')?.value==='model://'+id,uploaded.id);
    assert.equal(await page.locator('#training-task').inputValue(),uploaded.task);
    await screenshot('uploaded-model-training');
    await page.goto(base+'/admin');await page.locator('#admin-content').waitFor({state:'visible'});
    await page.locator('#models-tab').click();
    await page.locator(`[data-model-toggle="${modelId}"]`).click();
    await page.locator('#confirm-dialog').waitFor({state:'visible'});
    assert.match(await page.locator('#confirm-description').textContent(),/已发布任务/);
    await page.locator('#confirm-accept').click();
    await page.waitForFunction(id=>!window.AdminWorkspace.state.models.find(m=>m.id===id).enabled,modelId);
    assert.equal((await(await context.request.get(base+'/api/v1/scenes/'+sceneId)).json()).ready,false);
    await page.locator(`[data-model-toggle="${modelId}"]`).click();
    await page.waitForFunction(id=>window.AdminWorkspace.state.models.find(m=>m.id===id).enabled,modelId);

    await page.goto(base+'/');
    await page.locator(`#inspection-task option[value="scene:${sceneId}"]`).waitFor({state:'attached'});
    await page.waitForFunction(()=>window.ScrewApp?.state.model&&window.ScrewApp.state.session);
    await page.evaluate(()=>window.testMainGrid=document.querySelector('.main-grid'));
    await page.locator('#inspection-task').selectOption('scene:'+sceneId);
    await page.waitForFunction(()=>window.ScrewApp.state.model?.ready&&!window.ScrewApp.state.busy);
    assert.equal(new URL(page.url()).pathname,'/');
    assert.ok(await page.evaluate(()=>window.testMainGrid===document.querySelector('.main-grid')));
    await page.locator('.toolbar [data-action=upload]').click();
    await page.locator('#screw-upload-file').setInputFiles(path.join(output,'sample.avi'));
    await page.locator('#screw-upload-start').click();
    await page.waitForFunction(()=>window.ScrewApp.state.result&&!window.ScrewApp.state.busy,null,{timeout:120000});
    runId=await page.evaluate(()=>window.ScrewApp.state.run);
    const analysis=await page.evaluate(()=>window.ScrewApp.state.result);
    assert.equal(analysis.analysisType,'scene');assert.equal(analysis.sceneId,sceneId);
    assert.equal(analysis.model.id,modelId);assert.equal(analysis.sceneRevision,2);
    assert.equal(analysis.summary.sampleFps,2);assert.equal(analysis.summary.analyzedFrames,6);
    assert.ok(analysis.frames.every(frame=>Array.isArray(frame.observations)&&frame.checks.length===1));
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    await page.evaluate(()=>{document.querySelector('#source-video').currentTime=1.5;});
    await page.waitForFunction(()=>window.ScrewApp.state.renderedPanelFrame?.time===1.5);
    assert.equal(await page.locator('[data-action=report]').isEnabled(),true);
    const report=await context.request.get(base+`/api/v1/analysis-runs/${runId}/artifacts/report.html`);
    assert.equal(report.status(),200);assert.ok((await report.text()).includes('新场景螺丝检测'));
    await screenshot('video-replay');
    // A response from before switching away and back must not reopen an old run.
    let releaseRun, markRequested;
    const responseGate=new Promise(resolve=>releaseRun=resolve), requested=new Promise(resolve=>markRequested=resolve);
    await page.route('**/api/v1/analysis-runs/'+runId,async route=>{
      const response=await route.fetch();markRequested();await responseGate;await route.fulfill({response});
    },{times:1});
    await page.locator('[data-action=screw-history]').click();await page.locator(`[data-screw-run="${runId}"]`).click();
    await requested;
    await page.locator('#inspection-task').selectOption('pose');
    await page.waitForFunction(()=>!window.ScrewApp.state.switching);
    await page.locator('#inspection-task').selectOption('scene:'+sceneId);
    await page.waitForFunction(()=>!window.ScrewApp.state.switching);
    const delivered=page.waitForResponse(response=>response.url().endsWith('/analysis-runs/'+runId));
    releaseRun();await delivered;await page.waitForTimeout(200);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.result),null);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.run),null);
    await page.setViewportSize({width:390,height:844});assert.ok(await noOverflow());await screenshot('console-mobile');
    await page.setViewportSize({width:1440,height:1050});

    // Editing the scene cannot rewrite a completed recording's rules/version.
    const admin=await context.newPage();admin.on('pageerror',e=>errors.push(e.message));
    await admin.goto(base+'/admin');await admin.locator(`[data-scene-edit="${sceneId}"]`).first().click();
    await admin.locator('#scene-name').fill('修改后的任务');
    await admin.locator('#scene-form button[value=published]').click();
    await admin.waitForFunction(()=>window.AdminWorkspace.state.scenes[0].revision===3);
    await admin.close();await page.bringToFront();
    await page.reload();await page.waitForFunction(()=>window.ScrewApp?.state.model?.revision===3&&window.ScrewApp.state.session);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.result),null);
    await page.locator('[data-action=screw-history]').click();await page.locator(`[data-screw-run="${runId}"]`).click();
    await page.waitForFunction(()=>window.ScrewApp.state.result&&!window.ScrewApp.state.busy);
    assert.equal(await page.locator('#video-mode').inputValue(),'replay');
    await page.locator('[data-action=workflow]').click();assert.ok((await page.locator('#modal-body').textContent()).includes('新场景螺丝检测 · r2'));
    await page.locator('#modal [data-action=close-modal]').first().click();

    // Browser-provided camera frames are sent through the real inference endpoint.
    await page.locator('#video-mode').selectOption('realtime');
    await page.waitForFunction(()=>window.ScrewApp.state.frame?.sampleCount>=2,null,{timeout:120000});
    const live=await page.evaluate(()=>window.ScrewApp.state.frame);
    assert.equal(live.model.id,modelId);assert.equal(live.sceneRevision,3);
    await screenshot('camera');
    await page.evaluate(()=>{clearTimeout(window.ScrewApp.state.timer);});
    await page.waitForFunction(()=>!window.ScrewApp.state.pending);
    await page.evaluate(()=>{const s=window.ScrewApp.state;clearTimeout(s.timer);s.lastResponse=performance.now()-10000;s.lastSampleAt=s.lastResponse;});
    await page.waitForFunction(()=>window.ScrewApp.state.renderedPanelFrame===null&&window.ScrewApp.state.drawnBoxes.length===0);
    assert.equal(await page.locator('#workflow-status').getAttribute('data-verdict'),'REVIEW');
    await page.locator('#finish-detection').click();
    await page.waitForFunction(()=>!window.ScrewApp.state.streamId&&!window.ScrewApp.state.finishing);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.final.final),true);
    await page.locator('#start-live').click();
    await page.waitForFunction(()=>window.ScrewApp.state.frame?.sampleCount>=1&&window.ScrewApp.state.running,null,{timeout:120000});
    await context.clearCookies();
    await page.waitForFunction(()=>!window.ScrewApp.state.session&&!window.ScrewApp.state.running&&!window.ScrewApp.state.finishing);
    assert.equal(await page.evaluate(()=>window.ScrewApp.state.stream),null);
    assert.deepEqual(errors,[]);
    const summary={sceneId,modelId,runId,analyzedFrames:analysis.summary.analyzedFrames,
      effectiveFps:analysis.summary.sampleFps,cameraSamples:live.sampleCount,
      checks:['import','upload model','ROI','draft/publish','disable/enable','homepage','real worker',
        'video replay','frozen history','report','mobile','camera inference','stale result','expired session'],errors};
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(summary,null,2));
    console.log(JSON.stringify(summary));
  }catch(error){await screenshot('failure').catch(()=>{});console.error(await page.locator('body').innerText().catch(()=>''));throw error;}
  finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
