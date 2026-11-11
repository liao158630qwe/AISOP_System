// Real isolated authentication, model/scene catalog and station configuration APIs.
// Run: node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-model-ui.cjs
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.PILOT_TEST_BASE, output = process.env.PILOT_TEST_OUTPUT;
if (!base || !output) throw new Error('Run with test-pilot-ui.py --registration-paused');

(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage(), errors = [], checks = [];
  page.on('pageerror',error => errors.push(error.message)); page.setDefaultTimeout(15000);
  try {
    const session = await (await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}})).json();
    const headers = {'X-CSRF-Token':session.csrf}, initial = new Map();
    const body = binding => Object.fromEntries(['workstationId','cameraId','source','analysisType','sceneId','sampleFps','maxSampleGapSeconds','previewUrl','enabled','revision'].map(key => [key,binding[key]]));
    const saveAPI = async (station,data) => {
      const response = await context.request.put(base+'/api/v1/admin/terminals/'+station,{headers,data});
      assert.equal(response.status(),200,await response.text()); return response.json();
    };
    const waitReady = station => page.waitForFunction(station => StationDetection.state.station === station && StationDetection.state.connected && !StationDetection.state.busy,station);
    const openConfig = async () => {if (!await page.locator('#sd-config-dialog').isVisible()) await page.locator('#sd-config-open').click();};
    const submit = async (status=200) => {
      const response = page.waitForResponse(r => r.request().method()==='PUT' && r.url().includes('/admin/terminals/'));
      await page.locator('#sd-save').click(); const result = await response;
      assert.equal(result.status(),status,await result.text());
      await page.waitForFunction(() => !StationDetection.state.busy); return result.json();
    };
    for (const n of [1,6]) initial.set(n,await saveAPI('station-'+n,{
      workstationId:'edit-pc-'+n,cameraId:'edit-camera-'+n,source:'rtsp',analysisType:n===1?'screw':'sticker',
      sampleFps:n===1?7.5:12,maxSampleGapSeconds:1.7,previewUrl:'/media/edit-'+n+'/',enabled:true,revision:0,
    }));
    await page.goto(base+'/?task=station1');
    for (const n of [1,6]) {
      if (n===6) await page.locator('#inspection-task').selectOption('station6');
      await waitReady('station-'+n);
      assert.equal(await page.locator('#sd-config-dialog').isVisible(),false);
      assert.equal(await page.locator('#evidence-history-open + #sd-config-open').count(),1);
      assert.equal(await page.locator('.workflow-body #sd-binding, .workflow-body #sd-setup').count(),0);
      if (n===1) await page.screenshot({path:path.join(output,'station-config-button.png'),fullPage:true});
      assert.equal(await page.locator('#sd-setup').isVisible(),false); await openConfig(); await page.locator('#sd-edit').click();
      assert.equal(await page.locator('#sd-model').inputValue(),initial.get(n).binding.analysisType);
      assert.equal(await page.locator('#sd-fps').inputValue(),String(initial.get(n).binding.sampleFps));
      await page.locator('#sd-model').selectOption('pose');
      assert.match(await page.evaluate(() => StationDetection.startBlocked()),/未保存/);
      assert.equal(await page.locator('#sd-start').isDisabled(),true); await page.locator('#sd-cancel-edit').click();
      assert.equal(await page.evaluate(() => StationDetection.state.binding.analysisType),initial.get(n).binding.analysisType);
      assert.equal(await page.locator('#sd-start').isDisabled(),false);
      await page.locator('#sd-edit').click(); await page.locator('#sd-model').selectOption('pose');
      const changed = await submit();
      assert.equal(changed.changed,true); assert.equal(changed.binding.analysisType,'pose');
      for (const key of ['workstationId','cameraId','source','sampleFps','maxSampleGapSeconds','previewUrl','enabled'])
        assert.equal(changed.binding[key],initial.get(n).binding[key],key+' must be preserved');
      assert.equal(changed.binding.revision,2);
      assert.equal((await context.request.get(base+'/api/v1/terminal/context',{headers:{'X-Terminal-Key':initial.get(n).credential}})).status(),401);
      assert.equal((await context.request.get(base+'/api/v1/terminal/context',{headers:{'X-Terminal-Key':changed.credential}})).status(),200);
      await page.reload(); await waitReady('station-'+n); await openConfig(); await page.locator('#sd-edit').click();
      assert.equal(await page.locator('#sd-model').inputValue(),'pose');
      const unchanged = await submit();
      assert.equal(unchanged.changed,false); assert.equal(unchanged.binding.revision,2); assert.equal(unchanged.credential,null);
      await page.locator('#sd-config-close').click();
      checks.push('Station '+n+': edit, cancel, save, preserve device settings, reload, unchanged save');
    }

    // Stale editors keep their draft instead of overwriting another administrator.
    await openConfig(); await page.locator('#sd-edit').click(); await page.locator('#sd-model').selectOption('screw');
    const previous = await page.evaluate(() => StationDetection.state.binding);
    await saveAPI('station-6',{...body(previous),sampleFps:8}); await submit(409);
    assert.equal(await page.locator('#sd-model').inputValue(),'screw');
    assert.equal(await page.locator('#sd-setup').isVisible(),true);
    assert.match(await page.locator('#server-status').textContent(),/刷新/);
    await page.locator('#sd-refresh').click(); await waitReady('station-6'); await page.locator('#sd-edit').click();
    assert.equal(await page.locator('#sd-fps').inputValue(),'8'); assert.equal(await page.locator('#sd-model').inputValue(),'pose');
    checks.push('Version conflict preserves draft; refresh reloads current configuration');

    // A real active lease in another producer must reject model changes.
    const started = await context.request.post(base+'/api/v1/station-streams?station=station-6',{headers,data:{requestId:crypto.randomUUID(),inputSource:'image'}});
    assert.equal(started.status(),201,await started.text()); const stream = await started.json();
    await page.locator('#sd-model').selectOption('sticker'); await submit(409);
    assert.match(await page.locator('#server-status').textContent(),/停止采集/);
    const stopped = await context.request.delete(base+'/api/v1/station-streams/'+stream.streamId,{headers,data:{reason:'配置回归测试结束'}});
    assert.equal(stopped.status(),200,await stopped.text());
    const afterStop = await submit(); assert.equal(afterStop.binding.analysisType,'sticker');
    checks.push('Active producer blocks save; after stop the same draft can be saved');

    // Import a real local model and bind its published scene through the editor.
    const imported = await context.request.post(base+'/api/v1/admin/models/import',{headers,data:{path:'models/yolo11s-pose.pt',name:'工位配置回归模型',version:'test'},timeout:120000});
    assert.equal(imported.status(),201,await imported.text()); const model = await imported.json();
    const created = await context.request.post(base+'/api/v1/admin/scenes',{headers,data:{name:'工位场景切换回归',model_id:model.id,status:'published',rules:[{id:'person',name:'人员观察',class_id:0,kind:'required'}]}});
    assert.equal(created.status(),201,await created.text()); const scene = await created.json();
    await page.locator('#sd-refresh').click(); await waitReady('station-6'); await page.locator('#sd-edit').click();
    await page.locator('#sd-model').selectOption('scene'); assert.equal(await page.locator('#sd-scene-field').isVisible(),true);
    await page.locator('#sd-scene').selectOption(scene.id); const custom = await submit();
    assert.equal(custom.binding.analysisType,'scene'); assert.equal(custom.binding.sceneId,scene.id);
    assert.match(await page.locator('#sd-binding').textContent(),/工位场景切换回归/);
    await page.reload(); await waitReady('station-6'); await openConfig(); await page.locator('#sd-edit').click();
    assert.equal(await page.locator('#sd-model').inputValue(),'scene'); assert.equal(await page.locator('#sd-scene').inputValue(),scene.id);
    await page.screenshot({path:path.join(output,'station-6-model-editor.png'),fullPage:true,mask:[page.locator('#sd-credential')]});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(output,'station-model-editor-mobile.png'),fullPage:true,mask:[page.locator('#sd-credential')]});
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('#sd-model').selectOption('screw'); const builtin = await submit();
    assert.equal(builtin.binding.sceneId,null); assert.equal(builtin.binding.analysisType,'screw');
    checks.push('Real model / scene binding persists; switching back clears scene binding');

    await page.route('**/api/v1/admin/scenes',route => route.fulfill({status:503,json:{detail:'测试场景目录暂不可用'}}));
    await page.locator('#sd-refresh').click(); await waitReady('station-6'); await page.locator('#sd-edit').click();
    await page.locator('#sd-model').selectOption('scene');
    assert.match(await page.locator('#sd-scene-note').textContent(),/场景读取失败/);
    await page.locator('#sd-model').selectOption('pose'); await submit();
    checks.push('Scene catalog failure does not block built-in model changes');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#sd-config-dialog').isVisible(),false);
    await page.locator('#inspection-task').selectOption('pose');
    await page.waitForFunction(() => !StationDetection.active);
    assert.equal(await page.locator('#sd-config-open').isVisible(),false);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'station-model-edit.json'),JSON.stringify({passed:true,checks,pageErrors:errors},null,2));
    console.log(checks.join('\n'));
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode=1;});
