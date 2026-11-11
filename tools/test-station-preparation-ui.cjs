// Run with test-pilot-ui.py --registration-paused; no real detection or production writes.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.PILOT_TEST_BASE, output = process.env.PILOT_TEST_OUTPUT;
if (!base || !output) throw new Error('Run with test-pilot-ui.py --registration-paused');

(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage(), errors = [], checks = [], preparations = [], waiters = new Map();
  let streamRequests = 0;
  page.on('pageerror', error => errors.push(error.message)); page.setDefaultTimeout(10000);
  const preparationAt = index => preparations[index] ? Promise.resolve(preparations[index]) : new Promise(resolve => waiters.set(index,resolve));
  const finishPreparation = (route,ready=true) => route.fulfill({json:{ready,analysisType:'sticker'}});
  const waitReady = station => page.waitForFunction(station => StationDetection.state.station === station &&
    StationDetection.state.connected && !StationDetection.state.busy && !ScrewApp.state.switching,station);
  const selectStation = async task => {await page.locator('#inspection-task').selectOption(task); await waitReady('station-'+task.slice(7));};
  try {
    const session = await (await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}})).json();
    for (const [number,analysisType] of [[1,'pose'],[2,'sticker']]) {
      const response = await context.request.put(base+'/api/v1/admin/terminals/station-'+number,{
        headers:{'X-CSRF-Token':session.csrf},data:{workstationId:'prepare-pc-'+number,cameraId:'prepare-cam-'+number,
          source:'usb',analysisType,sampleFps:4,enabled:true,revision:0}});
      assert.equal(response.status(),200,await response.text());
    }
    await page.route('**/api/v1/stations/*/prepare', route => {
      assert.equal(route.request().method(),'POST');
      assert.equal(route.request().headers()['x-csrf-token'],session.csrf);
      const index = preparations.push(route)-1; waiters.get(index)?.(route); waiters.delete(index);
    });
    await page.route('**/api/v1/station-streams?*', route => {
      streamRequests++;
      return route.fulfill({status:409,json:{detail:'测试：已观察到创建轮次请求'}});
    });
    await page.goto(base+'/?task=station2');
    const abandoned = await preparationAt(0); await waitReady('station-2');
    assert.equal(await page.locator('#inspection-task').isDisabled(),false);
    assert.equal(await page.evaluate(()=>StationDetection.state.preparation.status),'pending');
    await selectStation('station1');
    const abandonedResponse = page.waitForResponse(response=>response.url().endsWith('/stations/station-2/prepare'));
    await abandoned.fulfill({status:503,json:{detail:'旧工位预热失败，不应出现在当前工位'}});
    await abandonedResponse;
    assert.equal(await page.evaluate(()=>StationDetection.state.preparation),null);
    assert.doesNotMatch(await page.locator('#server-status').textContent(),/旧工位预热失败/);
    checks.push('Background preparation leaves task selection usable; stale responses do not change the next station');

    await selectStation('station2'); const pending = await preparationAt(1);
    await page.locator('#sd-source').selectOption('replay');
    await page.locator('#sd-file').setInputFiles(path.resolve(__dirname,'../assets/reference.mp4'));
    await page.waitForFunction(()=>StationDetection.state.ready);
    await page.locator('#sd-start').click();
    await page.waitForFunction(()=>StationDetection.state.busy);
    assert.equal(preparations.length,2,'Start must reuse the background request');
    assert.equal(streamRequests,0);
    assert.deepEqual(await page.locator('#sd-video').evaluate(video=>({paused:video.paused,time:video.currentTime})),{paused:true,time:0});
    assert.match(await page.locator('#server-status').textContent(),/正在准备贴条模型/);
    await finishPreparation(pending);
    await page.waitForFunction(()=>!StationDetection.state.busy && StationDetection.state.error.includes('已观察到创建轮次'));
    assert.equal(streamRequests,1);
    checks.push('Start shares the preparation promise and keeps replay at frame zero until models are ready');

    await page.reload(); const failed = await preparationAt(2); await waitReady('station-2');
    await failed.fulfill({status:503,json:{detail:'测试模型暂未就绪'}});
    await page.waitForFunction(()=>StationDetection.state.preparation.status==='failed');
    await page.locator('#sd-source').selectOption('image');
    await page.locator('#sd-file').setInputFiles(path.join(output,'holes.jpg'));
    await page.waitForFunction(()=>StationDetection.state.ready);
    await page.locator('#sd-start').click(); const retry = await preparationAt(3);
    assert.equal(streamRequests,1);
    await finishPreparation(retry,false);
    await page.waitForFunction(()=>!StationDetection.state.busy && StationDetection.state.preparation.status==='failed');
    assert.equal(streamRequests,1,'An unsuccessful preparation must not start a detection cycle');
    assert.match(await page.locator('#server-status').textContent(),/准备未完成/);
    await page.locator('#sd-start').click(); const recovered = await preparationAt(4);
    await finishPreparation(recovered);
    await page.waitForFunction(()=>!StationDetection.state.busy && StationDetection.state.error.includes('已观察到创建轮次'));
    assert.equal(streamRequests,2);
    checks.push('HTTP errors and ready=false can be retried without creating premature cycles');

    await page.locator('#sd-config-open').click(); await page.locator('#sd-edit').click();
    await page.locator('#sd-model').selectOption('pose'); await page.locator('#sd-save').click();
    await page.waitForFunction(()=>!StationDetection.state.busy && StationDetection.state.binding.analysisType==='pose');
    assert.equal(preparations.length,5);
    await page.locator('#sd-edit').click(); await page.locator('#sd-model').selectOption('sticker');
    await page.locator('#sd-save').click(); const changed = await preparationAt(5);
    await finishPreparation(changed); await page.waitForFunction(()=>StationDetection.state.preparation.status==='ready');
    checks.push('Saving a new sticker binding prepares its new revision; other model types do not request preparation');
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'station-preparation.json'),JSON.stringify({passed:true,checks,pageErrors:errors},null,2));
    console.log(JSON.stringify({passed:true,checks}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
