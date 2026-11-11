const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.PILOT_TEST_BASE, output = process.env.PILOT_TEST_OUTPUT;
if (!base || !output) throw new Error('Run with tools/test-pilot-ui.py --registration-paused --browser-script tools/test-gpu-metrics-ui.cjs');

(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  const context = await browser.newContext({viewport:{width:1600, height:1000}});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(15000);
  try {
    const login = await context.request.post(base + '/api/v1/login', {data:{username:process.env.APP_USERNAME, password:process.env.APP_PASSWORD}});
    assert.equal(login.status(), 200);
    const {csrf} = await login.json();
    const binding = await context.request.put(base + '/api/v1/admin/terminals/station-2', {
      headers:{'X-CSRF-Token':csrf}, data:{workstationId:'gpu-ui-pc',cameraId:'gpu-ui-camera',analysisType:'sticker',sampleFps:4},
    });
    assert.equal(binding.status(), 200);
    await page.goto(base + '/?task=station2');
    await page.waitForFunction(() => window.StationDetection?.state.binding?.analysisType === 'sticker');
    await page.waitForFunction(() => document.querySelector('#metric-gpu').textContent !== '—');
    const readings = await page.locator('.metrics').innerText();
    assert.match(readings, /服务器 GPU 利用率/);
    assert.doesNotMatch(readings, /GPU 显存/);
    assert.equal(await page.locator('#metric-gpu-memory').count(), 0);
    assert.match(await page.locator('#metric-fps').evaluate(node => node.parentElement.title), /目标上限 4 fps/);
    assert.match(await page.locator('#metric-gpu').evaluate(node => node.parentElement.title), /全部进程/);
    assert.match(await page.locator('#metric-gpu').evaluate(node => node.parentElement.title), /nvidia-smi.*每1秒刷新.*采样时间/);
    await page.locator('.metrics').screenshot({path:path.join(output, 'gpu-metrics-desktop.png')});
    await page.locator('#inspection-task').selectOption('screw');
    await page.waitForFunction(() => window.ScrewApp?.state.task === 'screw' && !window.ScrewApp.state.switching);
    await page.evaluate(() => ScrewApp.render());
    assert.notEqual(await page.locator('#metric-gpu').textContent(), '—', 'task rendering must not erase hardware metrics');
    await page.route('**/api/v1/system/gpu', route => route.fulfill({json:{status:'ready',name:'GPU fixture',device:'cuda:0',utilizationPercent:0,memoryUsedMiB:2048,memoryTotalMiB:16384}}));
    await page.waitForFunction(() => document.querySelector('#metric-gpu').textContent === '0');
    await page.unroute('**/api/v1/system/gpu');
    await page.route('**/api/v1/system/gpu', route => route.fulfill({json:{status:'ready',name:'GPU fixture',device:'cuda:0',utilizationPercent:99,sampleAgeMs:4500}}));
    await page.waitForFunction(() => document.querySelector('#metric-gpu').textContent === '—' && document.querySelector('#metric-gpu').parentElement.title.includes('过期'));
    await page.unroute('**/api/v1/system/gpu');
    let hangingRequest, releaseRequest;
    const waiting = new Promise(resolve => {releaseRequest=resolve;});
    let gpuRequests=0;
    await page.route('**/api/v1/system/gpu', async route => {
      if (++gpuRequests===1) return route.fulfill({json:{status:'ready',name:'GPU fixture',device:'cuda:0',utilizationPercent:37,sampleAgeMs:0}});
      hangingRequest=route;
      await waiting;
      await route.abort().catch(()=>{});
    });
    await page.waitForFunction(() => document.querySelector('#metric-gpu').textContent === '37');
    await page.waitForFunction(() => document.querySelector('#metric-gpu').textContent === '—' && document.querySelector('#metric-gpu').parentElement.title.includes('过期'));
    assert.ok(hangingRequest, 'A slow network response must not keep showing the old GPU percentage');
    releaseRequest();
    await page.unroute('**/api/v1/system/gpu');
    await page.route('**/api/v1/system/gpu', route => route.abort());
    await page.waitForFunction(() => document.querySelector('#metric-gpu').textContent === '—');
    await page.unroute('**/api/v1/system/gpu');
    await page.waitForFunction(() => document.querySelector('#metric-gpu').textContent !== '—');
    await page.locator('#inspection-task').selectOption('station2');
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(() => window.StationDetection?.active && document.querySelector('#metric-gpu').textContent !== '—');
    await page.screenshot({path:path.join(output,'gpu-metrics-mobile.png'),fullPage:true});
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output,'gpu-metrics-ui.json'), JSON.stringify({passed:true,readings,errors},null,2));
    console.log('GPU UI passed: driver provenance, 1s refresh, stale/slow sample expiry, task switches, real zero, failed fetch, recovery, mobile.');
  } finally {await context.close();await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
