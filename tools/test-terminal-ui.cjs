// Real auth/binding/lease APIs; fake browser camera and explicitly mocked AI results.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.TERMINAL_TEST_BASE, output = process.env.TERMINAL_TEST_OUTPUT;
if (!base || !output) throw new Error('Run npm run test:terminal-ui');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true,
    args:['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream']});
  const context = await browser.newContext({viewport:{width:1440,height:1000}, permissions:['camera']});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const workpieceRequests = [];
  page.on('request', request => {
    if (/\/api\/v1\/(pilot\/|stations\/[^/]+\/context)/.test(request.url())) workpieceRequests.push(request.url());
  });
  page.setDefaultTimeout(15000);
  let identity, latest = null, behavior = 'normal', heldPoll = false, busyDelete = true;
  let count = 0, frameTimes = [], starts = 0, finalFixture = null, finishAttempts = 0;
  try {
    await page.goto(base + '/terminals');
    await page.locator('#account').click();
    await page.locator('#workspace-login [name=username]').fill(process.env.APP_USERNAME);
    await page.locator('#workspace-login [name=password]').fill(process.env.APP_PASSWORD);
    await page.locator('#workspace-login button[type=submit]').click();
    await page.waitForFunction(() => !!window.Workspace.session);
    await page.locator('[data-station="station-6"]').waitFor();
    await page.locator('#source').selectOption('usb');
    await page.locator('#analysis').selectOption('pose');
    await page.locator('#sample-fps').fill('10');
    await page.locator('#binding-form [type=submit]').click();
    await page.locator('#issued').waitFor({state:'visible'});
    const firstCredential = await page.locator('#issued-key').textContent();
    await page.waitForFunction(() => !document.querySelector('#account').disabled);
    await page.locator('#binding-form [type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#message').textContent.includes('配置未变化'));
    assert.equal(await page.locator('#issued').isHidden(), true);
    assert.equal((await context.request.get(base + '/api/v1/terminal/context', {headers:{'X-Terminal-Key':firstCredential}})).status(), 200);
    await page.locator('#sample-fps').fill('12');
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.querySelector('#message').textContent.includes('未保存的修改已保留'));
    assert.equal(await page.locator('#sample-fps').inputValue(), '12');
    await page.locator('#reload-binding').click();
    await page.waitForFunction(() => document.querySelector('#sample-fps').value === '10');
    await page.locator('#rotate-key').click();
    await page.locator('#issued').waitFor({state:'visible'});
    const credential = await page.locator('#issued-key').textContent();
    assert.ok(credential.length >= 32);
    assert.notEqual(credential, firstCredential);
    assert.equal((await context.request.get(base + '/api/v1/terminal/context', {headers:{'X-Terminal-Key':firstCredential}})).status(), 401);
    await page.screenshot({path:path.join(output,'admin.png'), fullPage:true, mask:[page.locator('#issued-key')]});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(output,'admin-mobile.png'), fullPage:true, mask:[page.locator('#issued-key')]});
    await page.setViewportSize({width:1440,height:1000});
    await page.waitForFunction(() => !document.querySelector('#account').disabled);
    await page.locator('#account').click();
    await page.waitForFunction(() => !window.Workspace.session);
    assert.equal(await page.locator('#issued-key').textContent(), '');
    assert.equal(await page.locator('#binding-form').isHidden(), true);
    await page.goto(base + '/terminal');
    await page.locator('#credential').fill(credential);
    await page.locator('#terminal-login button').click();
    await page.locator('#terminal-panel').waitFor({state:'visible'});
    assert.equal(await page.locator('#station-name').textContent(), '工位 1');
    assert.equal((await context.request.get(base + '/api/v1/stations/station-2/live-status')).status(), 403);
    assert.equal((await context.request.get(base + '/api/v1/admin/terminals')).status(), 403);
    assert.equal(await page.locator('#cycle-id, #current-product, #refresh-product, #manual-link, .terminal-product').count(), 0);

    await page.route('**/api/v1/station-streams', async route => {
      assert.deepEqual(Object.keys(route.request().postDataJSON()), ['requestId']);
      const response = await route.fetch();
      if (response.ok()) {
        identity = await response.json(); starts++; latest = null;
        assert.ok(!('cycleId' in identity) && !('product' in identity));
      }
      await route.fulfill({response});
    });
    await page.route('**/api/v1/station-streams/*/frames', async route => {
      const headers = route.request().headers();
      const current = {...identity};
      const frameSeq = Number(headers['x-frame-seq']);
      assert.equal(headers['x-station-id'], 'station-1');
      assert.equal(headers['x-camera-id'], 'cam-01');
      frameTimes.push(Date.now());
      await sleep(65); // Reveals the old "interval + processing" sampling bug.
      latest = {...current, frameSeq, serverProcessingMs:65,
        compliance:{status:'PASS',label:'测试夹具通过',reason:'模拟AI响应，仅测试通信'}};
      count++;
      await route.fulfill({json:latest}).catch(() => {});
    });
    await page.route('**/api/v1/stations/station-1/live-status', async route => {
      if (behavior === 'hung-poll') { heldPoll = true; await sleep(2600); }
      const result = latest && {...latest};
      if (result && behavior === 'wrong-camera') result.cameraId = 'another-camera';
      if (result && behavior === 'old-epoch') result.streamEpoch--;
      await route.fulfill({json:{...identity,stationId:'station-1',state:latest?'LIVE':'STALE',
        effectiveFps:10,resultAgeMs:behavior==='stale'?1200:10,result}}).catch(() => {});
    });
    await page.route('**/api/v1/station-streams/*', async route => {
      if (route.request().method() !== 'DELETE') return route.fallback();
      if (busyDelete) {busyDelete=false; return route.fulfill({status:429,json:{message:'仍在处理采样'},headers:{'Retry-After':'1'}});}
      return route.continue();
    });
    await page.route('**/api/v1/station-streams/*/finish', route => {
      if (++finishAttempts === 1) return route.fulfill({status:503,json:{message:'测试归档暂不可用'}});
      finalFixture = {...latest,final:true,archived:true,archivedAt:Date.now()/1000,recordStatus:'FINISHED'};
      return route.fulfill({json:finalFixture});
    });
    await page.route('**/api/v1/stations/station-1/inspections?**', async route => {
      const response = await route.fetch(), data = await response.json();
      // Final AI output is explicitly a UI fixture; Python tests verify real SQL archival.
      if (finalFixture) data.items = data.items.map(item => item.streamId === finalFixture.streamId
        ? {...item,status:'FINISHED',result:finalFixture,finishedAt:finalFixture.archivedAt,reason:'浏览器归档展示夹具'} : item);
      await route.fulfill({json:data});
    });
    await page.locator('#authorize').click();
    await page.waitForFunction(() => document.querySelector('#device').value);
    await page.locator('#start').click();
    await page.waitForFunction(() => document.querySelector('#decision').textContent === '测试夹具通过');
    while (count < 8) await sleep(100);
    const intervals = frameTimes.slice(1).map((at,i) => at-frameTimes[i]).sort((a,b)=>a-b);
    assert.ok(intervals[Math.floor(intervals.length/2)] < 155, `Sampling interval includes processing time: ${intervals}`);
    await page.screenshot({path:path.join(output,'terminal-live.png'),fullPage:true});
    behavior = 'wrong-camera';
    await page.waitForFunction(() => document.querySelector('#decision').textContent.includes('身份不匹配'));
    behavior = 'old-epoch';
    await sleep(700);
    assert.notEqual(await page.locator('.terminal-result').getAttribute('data-status'),'PASS');
    behavior = 'stale';
    await page.waitForFunction(() => document.querySelector('#decision').textContent.includes('等待有效采样'));
    behavior = 'normal';
    await page.waitForFunction(() => document.querySelector('.terminal-result').dataset.status === 'PASS');
    behavior = 'hung-poll';
    while (!heldPoll) await sleep(50);
    await page.waitForFunction(() => document.querySelector('#decision').textContent.includes('无法检测'));
    behavior = 'normal';
    await page.waitForFunction(() => document.querySelector('.terminal-result').dataset.status === 'PASS');
    // Simulate background visibility deterministically without OS focus dependencies.
    await page.evaluate(() => {
      Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.testHidden});
      window.testHidden=true; document.dispatchEvent(new Event('visibilitychange'));
    });
    await sleep(700);
    assert.notEqual(await page.locator('.terminal-result').getAttribute('data-status'),'PASS');
    await page.evaluate(() => {window.testHidden=false;document.dispatchEvent(new Event('visibilitychange'));});
    await page.waitForFunction(() => document.querySelector('.terminal-result').dataset.status === 'PASS');
    await page.locator('#stop').click();
    await page.waitForFunction(() => document.querySelector('#message').textContent.includes('再次点击'));
    assert.ok(await page.locator('#start').isDisabled(), 'failed release must retain stream ownership');
    await sleep(700);
    assert.notEqual(await page.locator('.terminal-result').getAttribute('data-status'),'PASS');
    await page.locator('#stop').click();
    await page.locator('#start').waitFor({state:'visible'});
    await page.waitForFunction(() => !document.querySelector('#start').disabled);
    await page.locator('#start').click();
    await page.waitForFunction(() => document.querySelector('.terminal-result').dataset.status === 'PASS');
    assert.equal(starts,2);
    await page.evaluate(() => document.querySelector('video').srcObject.getVideoTracks()[0].dispatchEvent(new Event('ended')));
    await sleep(700);
    assert.notEqual(await page.locator('.terminal-result').getAttribute('data-status'),'PASS');
    await page.locator('#stop').click();
    await page.waitForFunction(() => !document.querySelector('#start').disabled);
    await page.waitForFunction(() => document.querySelector('#inspection-history').textContent.includes('中断'));
    await page.locator('#start').click();
    await page.waitForFunction(() => !document.querySelector('#finish').disabled);
    await page.locator('#finish').click();
    await page.waitForFunction(() => document.querySelector('#message').textContent.includes('可重试结束并归档'));
    assert.equal(await page.locator('#start').isDisabled(), true);
    busyDelete = true;
    await page.locator('#finish').click();
    await page.waitForFunction(() => document.querySelector('#message').textContent.includes('结果已归档，请再次点击'));
    assert.equal(await page.locator('#final-record').isVisible(), true);
    assert.equal(await page.locator('#finish').isDisabled(), true);
    await page.locator('#stop').click();
    await page.waitForFunction(() => !document.querySelector('#start').disabled);
    await page.waitForFunction(() => document.querySelector('#inspection-history').textContent.includes('已归档'));
    assert.equal(finishAttempts, 2);
    assert.equal(starts, 3);
    await page.screenshot({path:path.join(output,'terminal-archived.png'),fullPage:true});
    // Render an authenticated sample preview without requiring an external media gateway.
    const terminalContext = await (await context.request.get(base+'/api/v1/terminal/context')).json();
    await page.route('**/api/v1/terminal/context', route => route.fulfill({json:{...terminalContext,
      binding:{...terminalContext.binding,source:'rtsp'}}}));
    await page.route('**/api/v1/stations/station-1/preview?**', route => route.fulfill({
      body:fs.readFileSync(path.join(output,'preview.jpg')),contentType:'image/jpeg',headers:{
        'X-Stream-Id':latest.streamId,'X-Stream-Epoch':String(latest.streamEpoch),'X-Frame-Seq':String(latest.frameSeq)}}));
    await page.reload();
    await page.waitForFunction(() => !document.querySelector('#sample-preview').hidden && document.querySelector('#sample-preview').naturalWidth === 640);
    await page.screenshot({path:path.join(output,'network-sample.png'),fullPage:true});
    behavior = 'stale';
    await page.waitForFunction(() => document.querySelector('#sample-preview').hidden);
    behavior = 'normal';
    await page.unroute('**/api/v1/terminal/context');
    await page.reload();
    await page.locator('#terminal-panel').waitFor({state:'visible'});
    await page.waitForFunction(() => document.querySelector('#inspection-history').textContent.includes('已归档'));
    await page.locator('#authorize').click();
    await page.waitForFunction(() => document.querySelector('#device').value);
    const nextRound = page.waitForResponse(response => response.url().endsWith('/api/v1/station-streams') && response.request().method() === 'POST');
    await page.locator('#start').click();
    await nextRound;
    await page.waitForFunction(() => document.querySelector('.terminal-result').dataset.status === 'PASS');
    assert.equal(starts,4);
    await page.locator('#stop').click();
    await page.waitForFunction(() => !document.querySelector('#start').disabled);
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(output,'terminal-mobile.png'),fullPage:true});
    assert.deepEqual(workpieceRequests, [], 'default detection flow must never look up manual workpieces');
    await page.goto(base + '/?task=station6');
    await page.waitForFunction(() => window.AISOP_TERMINAL?.stationId === 'station-1');
    await page.waitForFunction(() => document.querySelector('#inspection-task')?.value === 'station1');
    assert.equal(await page.locator('#inspection-task option[value="station6"]').isDisabled(),true);
    assert.deepEqual(errors,[]);
    const report={passed:true,modelInference:false,finalArchiveMocked:true,checks:['admin-binding','fixed-station-auth','USB-capture',
      'sampling-cadence','camera-and-epoch-isolation','stale-and-hung-poll','background-pause','stop-retry',
      'camera-disconnect','network-sample-preview','mobile-layout','manual-station-lock','no-op-save',
      'explicit-credential-rotation','draft-preservation','credential-cleared-on-logout','no-workpiece-controls-or-requests',
      'interruption-history','finish-and-release-retry','archive-history-after-reload','next-round-without-workpiece'],frames:count,pageErrors:errors};
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
