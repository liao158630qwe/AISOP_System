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
    await page.locator('#locked-login').click();
    await page.locator('#workspace-login [name=username]').fill(process.env.APP_USERNAME);
    await page.locator('#workspace-login [name=password]').fill(process.env.APP_PASSWORD);
    await page.locator('#workspace-login button[type=submit]').click();
    await page.locator('#admin-content').waitFor({state:'visible'});
    await page.locator('#models-tab').click();
    await page.locator('#import-model-button').click();
    await page.locator('#candidate-select option[value="models/screw-head-v1.pt"]').waitFor({state:'attached'});
    await page.locator('#candidate-select').selectOption('models/screw-head-v1.pt');
    await page.locator('#model-name').fill('螺丝检测 · 浏览器验证');
    await page.locator('#model-version').fill('1.0');
    await page.locator('#import-submit').click();
    await page.waitForFunction(()=>window.AdminWorkspace.state.models.length===1,null,{timeout:120000});
    modelId=await page.evaluate(()=>window.AdminWorkspace.state.models[0].id);
    await screenshot('models');
    await page.locator(`[data-model-use="${modelId}"]`).click();
    await page.locator('#scene-name').fill('新场景螺丝检测');
    await page.locator('#scene-description').fill('真实训练权重、区域规则与视频检测链路验证');
    await page.locator('#scene-device').selectOption('cpu');
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
    await page.locator(`[data-scene-edit="${sceneId}"]`).click();
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
    await page.locator(`[data-model-toggle="${modelId}"]`).click();
    await page.waitForFunction(id=>!window.AdminWorkspace.state.models.find(m=>m.id===id).enabled,modelId);
    assert.equal((await(await context.request.get(base+'/api/v1/scenes/'+sceneId)).json()).ready,false);
    await page.locator(`[data-model-toggle="${modelId}"]`).click();
    await page.waitForFunction(id=>window.AdminWorkspace.state.models.find(m=>m.id===id).enabled,modelId);

    await page.goto(base+'/');
    await page.locator(`#inspection-task option[value="scene:${sceneId}"]`).waitFor({state:'attached'});
    await page.locator('#inspection-task').selectOption('scene:'+sceneId);
    await page.waitForFunction(()=>window.SceneConsole?.state.scene?.ready&&window.Workspace.session);
    await page.locator('#scene-video-upload').setInputFiles(path.join(output,'sample.avi'));
    await page.waitForFunction(()=>window.SceneConsole.state.result&&!window.SceneConsole.state.busy,null,{timeout:120000});
    runId=await page.evaluate(()=>window.SceneConsole.state.runId);
    const analysis=await page.evaluate(()=>window.SceneConsole.state.result);
    assert.equal(analysis.analysisType,'scene');assert.equal(analysis.sceneId,sceneId);
    assert.equal(analysis.model.id,modelId);assert.equal(analysis.sceneRevision,2);
    assert.equal(analysis.summary.sampleFps,2);assert.equal(analysis.summary.analyzedFrames,6);
    assert.ok(analysis.frames.every(frame=>Array.isArray(frame.observations)&&frame.checks.length===1));
    await page.waitForFunction(()=>document.querySelector('#scene-video').readyState>=2);
    await page.evaluate(()=>{document.querySelector('#scene-video').currentTime=1.5;});
    await page.waitForFunction(()=>window.SceneConsole.state.frame?.time===1.5);
    assert.equal(await page.locator('#report-button').isEnabled(),true);
    const report=await context.request.get(base+`/api/v1/analysis-runs/${runId}/artifacts/report.html`);
    assert.equal(report.status(),200);assert.ok((await report.text()).includes('新场景螺丝检测'));
    await screenshot('video-replay');
    await page.setViewportSize({width:390,height:844});assert.ok(await noOverflow());await screenshot('console-mobile');
    await page.setViewportSize({width:1440,height:1050});

    // Editing the scene cannot rewrite a completed recording's rules/version.
    const admin=await context.newPage();admin.on('pageerror',e=>errors.push(e.message));
    await admin.goto(base+'/admin');await admin.locator(`[data-scene-edit="${sceneId}"]`).click();
    await admin.locator('#scene-name').fill('修改后的任务');
    await admin.locator('#scene-form button[value=published]').click();
    await admin.waitForFunction(()=>window.AdminWorkspace.state.scenes[0].revision===3);
    await admin.close();await page.bringToFront();
    await page.reload();await page.waitForFunction(()=>window.SceneConsole?.state.scene?.revision===3&&window.Workspace.session);
    assert.equal(await page.evaluate(()=>window.SceneConsole.state.result),null);
    await page.locator('#source-mode').selectOption('camera');
    await page.locator('#history-button').click();await page.locator(`[data-run="${runId}"]`).click();
    await page.waitForFunction(()=>window.SceneConsole.state.result&&!window.SceneConsole.state.busy);
    assert.equal(await page.locator('#source-mode').inputValue(),'upload');
    await page.locator('#rules-button').click();assert.ok((await page.locator('#rules-content').textContent()).includes('新场景螺丝检测 · r2'));
    await page.locator('[data-close=rules-dialog]').click();

    // Browser-provided camera frames are sent through the real inference endpoint.
    await page.locator('#source-mode').selectOption('camera');await page.locator('#start-camera').click();
    await page.waitForFunction(()=>window.SceneConsole.state.frame?.sampleCount>=2,null,{timeout:120000});
    const live=await page.evaluate(()=>window.SceneConsole.state.frame);
    assert.equal(live.model.id,modelId);assert.equal(live.sceneRevision,3);
    await screenshot('camera');
    await page.evaluate(()=>{clearTimeout(window.SceneConsole.state.timer);});
    await page.waitForFunction(()=>!window.SceneConsole.state.pending);
    await page.evaluate(()=>{const s=window.SceneConsole.state;clearTimeout(s.timer);s.received=performance.now()-10000;s.frame={...s.frame,observations:[],compliance:{status:'PASS',label:'old',reason:'old'}};});
    await page.waitForFunction(()=>window.SceneConsole.state.frame.stale);
    assert.equal(await page.locator('#decision').getAttribute('data-status'),'REVIEW');
    await page.locator('#stop-camera').click();
    await page.waitForFunction(()=>!window.SceneConsole.state.streamId&&!window.SceneConsole.state.stopping);
    assert.equal(await page.evaluate(()=>window.SceneConsole.state.frame.final),true);
    await page.locator('#start-camera').click();
    await page.waitForFunction(()=>window.SceneConsole.state.frame?.sampleCount>=1&&window.SceneConsole.state.running,null,{timeout:120000});
    await context.clearCookies();
    await page.waitForFunction(()=>!window.Workspace.session&&!window.SceneConsole.state.running&&!window.SceneConsole.state.stopping);
    assert.equal(await page.evaluate(()=>window.SceneConsole.state.stream),null);
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
