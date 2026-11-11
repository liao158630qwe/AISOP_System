/* Browser interaction regression with explicit fixture APIs, no production writes. */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE || process.env.HOME, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root = path.resolve(__dirname, '..'), output = path.join(root, 'test-results', 'board-browser-tests');
fs.mkdirSync(output, {recursive:true});
const files = new Set(['board.html','board.js','board.css','board-history.js','board-history.css','board-advanced.js','board-advanced.css','review-outbox.js','workspace-api.js','assets/favicon.svg']);
const server = http.createServer((req, res) => {
  const name = new URL(req.url, 'http://localhost').pathname.replace(/^\//, '') || 'board.html';
  const file = name === 'board' ? 'board.html' : name;
  if (!files.has(file)) return res.writeHead(404).end();
  const content = fs.readFileSync(path.join(root, file.startsWith('assets/') ? '' : 'frontend', file));
  res.writeHead(200, {'Content-Type':({'.html':'text/html; charset=utf-8','.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml'})[path.extname(file)]});
  res.end(content);
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({channel:process.env.BROWSER_CHANNEL || 'msedge', headless:true});
  const context = await browser.newContext({viewport:{width:1920,height:1080}, deviceScaleFactor:1});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(12000);
  const fixture = {generatedAt:Date.now()/1000, summary:{total:3,passed:1,failed:1,pending:1,complianceRate:90},
    stations:Array.from({length:6}, (_, i) => ({id:'station-' + (i+1),name:'工位' + (i+1),title:'工位流程',
      status:'RUNNING',partNumber:'P-001',stage:'螺丝检查',total:10,complianceRate:90,effectiveFps:4,reason:'检测在线',steps:['检查']})),
    alerts:[{id:'history',at:Date.now()/1000,stationId:'station-1',partNumber:'P-001',types:['漏螺丝'],reason:'历史异常',source:'人工确认'}],alertCount:1,
    top:['漏螺丝','缺卡扣','未吹净','漏接端子','工序跳步','其他异常'].map((name, i) => ({name,count:i ? 0 : 1})),
    trends:{hour:Array.from({length:24}, (_, h) => ({label:String(h).padStart(2,'0') + ':00',rate:h < 12 ? 90 : null,passed:9,failed:1,pending:0})),
      shift:[{label:'夜班 00–08',rate:90,passed:90,failed:10,pending:0},{label:'早班 08–16',rate:null,passed:0,failed:0,pending:0},{label:'晚班 16–24',rate:null,passed:0,failed:0,pending:0}]}};
  let signedIn = false, fail = false, calls = 0, retryAfter = '';
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    let status = 200, body;
    if (url.pathname.endsWith('/login')) { signedIn = true; body = {username:'board-test',csrf:'fixture'}; }
    else if (url.pathname.endsWith('/logout')) { signedIn = false; body = {ok:true}; }
    else if (url.pathname.endsWith('/session')) { status = signedIn ? 200 : 401; body = signedIn ? {username:'board-test',csrf:'fixture'} : {detail:'请先登录'}; }
    else if (url.pathname.endsWith('/board/snapshot')) { calls++; status = fail ? 503 : 200; body = fail ? {detail:'测试断线'} : fixture; }
    else { status = 404; body = {}; }
    await route.fulfill({status, headers:fail && retryAfter ? {'Retry-After':retryAfter} : {}, contentType:'application/json', body:JSON.stringify(body)});
  });
  async function screenshot(name) { await page.screenshot({path:path.join(output,name + '.png'),fullPage:true}); }
  async function noOverflow() { assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); }
  async function sync() {
    fixture.summary.total++; fixture.generatedAt++;
    await page.clock.runFor(4000);
    await page.waitForFunction(total => document.querySelector('#total').textContent === String(total), fixture.summary.total);
  }
  try {
    await page.clock.install();
    await page.goto(base + '/board?demo=1');
    await page.waitForFunction(() => document.querySelector('#connection').dataset.state === 'demo');
    assert.equal(await page.locator('.station').count(),6);
    await screenshot('overview-1920'); await noOverflow();
    assert.ok(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight), '1080p board fits one screen');
    await page.locator('[data-period=shift]').click();
    await page.clock.runFor(100);
    assert.equal(await page.locator('#trend-chart').getAttribute('id'), 'trend-chart');
    assert.ok((await page.locator('#trend-chart').textContent()).includes('早班'));
    await page.locator('.view-switch [data-view=stations]').click();
    assert.equal(await page.locator('.alerts-panel').isHidden(),true); await screenshot('stations-1920');
    await page.locator('.view-switch [data-view=quality]').click();
    assert.equal(await page.locator('.station-panel').isHidden(),true); await screenshot('quality-1920');
    await page.locator('#test-alert').click();
    await page.locator('#alert-dialog:not([hidden])').waitFor();
    assert.ok((await page.locator('#alarm-source').textContent()).includes('预览'));
    const card = await page.locator('#alert-dialog').boundingBox();
    assert.ok(card.width <= 500 && card.height <= 300 && card.y > 540, 'compact notice stays at the bottom');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'test-alert', 'notification does not steal focus');
    await page.locator('.view-switch [data-view=overview]').click();
    assert.equal(await page.locator('.board').getAttribute('data-view'),'overview', 'board remains interactive with alert visible');
    await screenshot('bottom-alert');
    await page.setViewportSize({width:390,height:844});
    await noOverflow();
    const mobileCard = await page.locator('#alert-dialog').boundingBox();
    assert.ok(mobileCard.width <= 366 && mobileCard.height <= 300 && mobileCard.y + mobileCard.height <= 844);
    await screenshot('bottom-alert-mobile');
    await page.setViewportSize({width:1920,height:1080});
    await page.locator('#dismiss-alert').click();
    await page.locator('.view-switch [data-view=overview]').click();
    await page.locator('[data-period=hour]').click();
    await page.locator('#auto-alert').click();
    for (const [width,height] of [[1440,900],[390,844]]) {
      await page.setViewportSize({width,height}); await noOverflow(); await screenshot('overview-' + width);
    }
    await page.setViewportSize({width:1440,height:900});
    await page.locator('#live-mode').click();
    assert.equal(await page.locator('#total').textContent(),'—');
    await page.locator('#account').click();
    await page.locator('[name=username]').fill('board-test');
    await page.locator('[name=password]').fill('fixture-password');
    await page.locator('#workspace-login button[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#connection').dataset.state === 'live');
    assert.equal(await page.locator('#total').textContent(),'3');
    assert.equal(await page.locator('#alert-dialog').isHidden(),true, 'history does not pop up on initial load');
    await page.clock.runFor(100); // Flush the chart's scheduled animation frame before taking DOM identities.
    await page.evaluate(() => { window.stableNodes = ['#stations .station','#ranking .rank-row','#trend-chart svg','#alert-list button'].map(selector => document.querySelector(selector)); });
    await sync();
    assert.equal(await page.evaluate(() => stableNodes.every(node => node?.isConnected)), true, 'unchanged cards, ranking, chart and alerts retain DOM nodes');
    fixture.stations[2].partNumber = 'P-NEW';
    await sync();
    assert.equal(await page.evaluate(() => stableNodes[0].isConnected), true, 'one changed station does not replace other stations');
    assert.ok((await page.locator('#stations .station').nth(2).textContent()).includes('P-NEW'));
    await page.locator('#auto-alert').click();
    fixture.alerts.unshift(...['new-a','new-b'].map((id, i) => ({id,at:Date.now()/1000+i,
      stationId:'station-2',partNumber:id,types:['缺卡扣'],source:'人工确认',reason:'新事件 ' + id})));
    fixture.alertCount = 3;
    await page.locator('#alert-dialog:not([hidden])').waitFor();
    assert.equal(await page.locator('#alarm-part').textContent(),'new-a');
    await page.locator('#dismiss-alert').click();
    await page.waitForFunction(() => document.querySelector('#alarm-part').textContent === 'new-b' && !document.querySelector('#alert-dialog').hidden);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(3500);
    assert.equal(await page.locator('#alert-dialog').isHidden(),true,'duplicate polls do not re-alert');
    await page.locator('#auto-alert').click();
    fixture.alerts.unshift({id:'injection',at:Date.now()/1000,stationId:'station-1',partNumber:'<img src=x onerror=alert(1)>',types:['<script>'],reason:'<img src=x onerror=alert(1)>',source:'fixture'});
    fixture.alertCount++;
    await page.waitForFunction(() => document.querySelector('#alert-count').textContent === '4');
    assert.equal(await page.locator('#alert-list img, #alert-list script').count(),0);
    fail = true;
    await page.waitForFunction(() => document.querySelector('#connection').dataset.state === 'error');
    assert.equal(await page.locator('.station[data-status=RUNNING]').count(),0);
    assert.equal(await page.locator('#total').textContent(),String(fixture.summary.total));
    fail = false;
    await page.waitForFunction(() => document.querySelector('#connection').dataset.state === 'live');
    await page.locator('#fullscreen').click();
    await page.waitForFunction(() => !!document.fullscreenElement);
    await page.locator('#fullscreen').click();
    await page.waitForFunction(() => !document.fullscreenElement);
    await page.locator('#rotation-toggle').click();
    await page.clock.runFor(16000);
    assert.equal(await page.locator('.board').getAttribute('data-view'),'stations');
    await page.locator('#definitions').click();
    await page.clock.runFor(18000);
    assert.equal(await page.locator('.board').getAttribute('data-view'),'stations','dialog pauses carousel');
    await page.locator('#close-definitions').click();
    await page.locator('#rotation-toggle').click();
    // Lifecycle stops network/timers and resumes immediately with a fresh snapshot.
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', {configurable:true,get:() => true});
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const hiddenCalls = calls;
    await page.clock.runFor(60000);
    assert.equal(calls, hiddenCalls, 'hidden screen must not keep polling');
    fixture.summary.total++;
    await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
    await page.waitForFunction(total => document.querySelector('#total').textContent === String(total), fixture.summary.total);
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'onLine', {configurable:true,get:() => false});
      window.dispatchEvent(new Event('offline'));
    });
    const offlineCalls = calls;
    await page.clock.runFor(60000);
    assert.equal(calls, offlineCalls, 'offline screen must not keep polling');
    fixture.summary.total++;
    await page.evaluate(() => { delete navigator.onLine; window.dispatchEvent(new Event('online')); });
    await page.waitForFunction(total => document.querySelector('#total').textContent === String(total), fixture.summary.total);
    fail = true; retryAfter = '12';
    await page.clock.runFor(4000);
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('约 12 秒后重试'));
    const failedCalls = calls;
    await page.clock.runFor(7000);
    assert.equal(calls, failedCalls, 'Retry-After must delay retries');
    fail = false; retryAfter = '';
    await page.clock.runFor(6000);
    await page.waitForFunction(() => document.querySelector('#connection').dataset.state === 'live');
    // Keep an active live alarm visible across more than 2,000 other event IDs.
    const live = {id:'live:persistent',at:Date.now()/1000,stationId:'station-1',partNumber:'LIVE',types:['漏螺丝'],reason:'持续异常',source:'实时检测'};
    for (let batch = 0; batch < 22; batch++) {
      fixture.alerts = [live, ...Array.from({length:99}, (_, i) => ({...live,id:`burst-${batch}-${i}`,partNumber:`B-${batch}-${i}`}))];
      fixture.alertCount = 100;
      await sync();
    }
    await page.locator('#auto-alert').click();
    await sync();
    assert.equal(await page.locator('#alert-dialog').isHidden(),true,'persistent live alarm stays remembered after the seen-ID limit');
    await page.locator('.view-switch [data-view=overview]').click();
    await page.locator('#test-alert').click();
    fixture.alerts = Array.from({length:100}, (_, i) => ({...live,id:'overflow-'+i,at:Date.now()/1000+i,partNumber:'O-'+i}));
    await sync();
    assert.equal(await page.locator('#alarm-queue').textContent(),'待看 50 条 · 另 50 条请查记录');
    await page.locator('#auto-alert').click();
    await page.locator('#dismiss-alert').click();
    await page.locator('#auto-alert').click();
    fixture.alerts = [{...live,id:'live:resolved'}];
    await sync();
    assert.equal(await page.locator('#alert-dialog').isVisible(),true);
    fixture.alerts = []; fixture.alertCount = 0;
    await sync();
    assert.equal(await page.locator('#alert-dialog').isHidden(),true,'resolved live alarm closes its notice');
    // Detection mode reports live REVIEW as online and displays confirmation progress.
    fixture.mode='detection'; fixture.pollAfterMs=1000;
    fixture.stations=fixture.stations.map((s,i)=>({...s,partNumber:null,cameraId:'camera-'+i,
      online:true,status:i===0?'REVIEW':'RUNNING',stage:'累计视觉确认',reason:'累计视觉确认',
      detection:{status:i===0?'REVIEW':'PASS',label:i===0?'待确认':'合规',confirmed:i===0?5:11,expected:11}}));
    await sync();
    assert.equal(await page.locator('#running-count').textContent(),'6');
    assert.equal(await page.locator('.station-badge').first().textContent(),'采样在线');
    assert.equal(await page.locator('.station-product small').first().textContent(),'4.0 fps');
    assert.equal(await page.locator('.station-decision').first().textContent(),'待确认 · 5/11 已确认');
    assert.equal(await page.locator('.station-decision').nth(1).textContent(),'合规 · 11/11 已确认');
    assert.ok(!(await page.locator('#stations').textContent()).includes('等待工件'));
    for(const [width,height] of [[1440,1000],[1920,1080]]){
      await page.setViewportSize({width,height});await page.clock.runFor(100);
      assert.ok(await page.evaluate(()=>[...document.querySelectorAll('.station-title,.station-step')].every(node=>node.clientHeight>=parseFloat(getComputedStyle(node).fontSize))), 'station labels must not shrink to clipped lines');
      assert.ok(await page.evaluate(()=>[...document.querySelectorAll('.station')].every(card=>card.scrollHeight<=card.clientHeight+1)), 'station contents fit their cards');
      await screenshot('detection-rounds-'+width);
    }
    assert.ok(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight));
    await noOverflow();
    fail=true;
    await page.clock.runFor(4000);
    await page.waitForFunction(()=>document.querySelector('#connection').dataset.state==='error');
    assert.equal(await page.locator('.station-decision[data-verdict=PASS]').count(),0,'stale board clears live PASS');
    fail=false;
    await page.locator('#account').click();
    await page.waitForFunction(() => document.querySelector('#total').textContent === '—');
    assert.equal(await page.locator('#alert-list [data-alert]').count(),0,'logout clears private data');
    assert.ok(calls >= 5);
    assert.deepEqual(errors,[]);
    console.log('Board browser checks passed; screenshots: ' + output);
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
