// Real API and durable database; run through tools/test-pilot-ui.py.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.PILOT_TEST_BASE, output = process.env.PILOT_TEST_OUTPUT;
(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1050}, acceptDownloads:true});
    const page = await context.newPage(), errors = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {if (request.url().includes('/api/')) requests.push(request.url());});
    page.setDefaultTimeout(15000);
    const field = name => page.locator(`#equipment-form [name="${name}"]`);
    const tab = id => page.locator(`#equipment-tab-${id}`).click();
    const expand = () => page.locator('[aria-controls="equipment-section-content"]').click();
    const save = async () => {
      const response = page.waitForResponse(r=>r.url().endsWith('/admin/deployment-settings') && r.request().method() === 'PUT');
      await page.locator('#equipment-save').click(); return await response;
    };
    await page.goto(base + '/settings');
    await page.locator('#equipment-load').waitFor({state:'attached'});
    await page.locator('#workspace-login').waitFor({state:'visible'});
    assert.ok(requests.every(url=>url.endsWith('/session')),'Only authentication is checked before login');
    await page.locator('#workspace-login-form [name=username]').fill(process.env.APP_USERNAME);
    await page.locator('#workspace-login-form [name=password]').fill(process.env.APP_PASSWORD);
    await page.locator('#workspace-login-form button[type=submit]').click();
    await page.locator('#workspace-login').waitFor({state:'hidden'});
    await expand();
    await page.locator('#equipment-form').waitFor();
    assert.equal(await page.locator('#equipment-panel-inventory .equipment-group').count(),9);
    assert.equal(await field('equipment.1.quantity').inputValue(),'6');
    assert.equal(await field('equipment.3.quantity').inputValue(),'4');
    assert.equal(await field('equipment.4.model').inputValue(),'DS-7716NX-K5/Pro');
    await tab('network');
    await field('server.host').fill('192.0.2.10');
    await field('server.apiUrl').fill('http://192.0.2.10:8000');
    assert.equal(await field('server.memoryConfirmedGb').inputValue(),'');
    await tab('cameras');
    assert.equal(await page.locator('#equipment-panel-cameras .equipment-group').count(),6);
    await field('cameras.0.rtspUrl').fill('rtsp://operator:secret@192.0.2.11/stream');
    await page.locator('#equipment-save').click();
    assert.match(await field('cameras.0.rtspUrl').evaluate(el=>el.validationMessage),/不保存/);
    await field('cameras.0.rtspUrl').fill('rtsp://192.0.2.11:554/Streaming/Channels/101');
    await field('cameras.0.poeId').selectOption('poe-1'); await field('cameras.0.poePort').fill('1');
    await field('cameras.1.poeId').selectOption('poe-1'); await field('cameras.1.poePort').fill('1');
    // Submit from another tab reveals the invalid mapping automatically.
    await tab('inventory'); await page.locator('#equipment-save').click();
    assert.equal(await page.locator('#equipment-tab-cameras').getAttribute('aria-selected'),'true');
    assert.match(await field('cameras.1.poePort').evaluate(el=>el.validationMessage),/不能连接/);
    await field('cameras.1.poePort').fill('2');
    await tab('storage');
    assert.match(await page.locator('#equipment-current-storage').textContent(),/待接入/);
    assert.ok((await page.locator('#equipment-current-storage').textContent()).includes(path.join(output,'data','evidence')));
    assert.match(await page.locator('#equipment-nvr-summary').textContent(),/海康威视 DS-7716NX-K5\/Pro · 1 台/);
    assert.equal(await field('nvr.purpose').inputValue(),'保存相机视频流、AI 告警取证录像');
    assert.equal(await field('nvr.diskCount').inputValue(),'');
    assert.equal(await field('nvr.evidenceRetentionDays').inputValue(),'30');
    await field('nvr.evidenceRetentionDays').fill('29');
    await page.locator('#equipment-save').click();
    assert.equal(await field('nvr.evidenceRetentionDays').evaluate(el=>el.validity.rangeUnderflow),true);
    await field('nvr.evidenceRetentionDays').fill('60');
    await field('nvr.host').fill('192.0.2.20'); await field('nvr.location').fill('车间弱电机柜');
    await field('nvr.diskCount').fill('2'); await field('nvr.diskCapacityTb').fill('8');
    await field('nvr.bitrateMbps').fill('4'); await field('nvr.usableStorageTb').fill('10');
    assert.match(await page.locator('#equipment-storage-estimate').textContent(),/7.78 TB/);
    await field('notes').fill('六工位现场联调记录');
    let response = await save(); assert.equal(response.status(),200,await response.text());
    const first = await response.json(); assert.equal(first.revision,1);
    assert.equal(first.config.nvr.evidenceRetentionDays,60);
    assert.equal(first.runtime.evidenceStorage.retentionDays,30,'planned NVR retention must not alter current storage');
    await page.reload(); await expand(); await page.locator('#equipment-form').waitFor();
    await tab('network'); assert.equal(await field('server.host').inputValue(),'192.0.2.10');
    await tab('storage');
    assert.equal(await field('nvr.location').inputValue(),'车间弱电机柜');
    assert.equal(await field('nvr.diskCount').inputValue(),'2');
    assert.equal(await field('nvr.diskCapacityTb').inputValue(),'8');
    assert.equal(await field('nvr.evidenceRetentionDays').inputValue(),'60');
    const other = await context.newPage();
    await other.goto(base+'/settings'); await other.locator('[aria-controls="equipment-section-content"]').click();
    await other.locator('#equipment-form').waitFor(); await other.locator('#equipment-tab-storage').click();
    assert.equal(await other.locator('[name="notes"]').inputValue(),'六工位现场联调记录');
    await other.locator('[name="notes"]').fill('另一管理页面的修改');
    const updated = other.waitForResponse(r=>r.url().endsWith('/admin/deployment-settings') && r.request().method()==='PUT');
    await other.locator('#equipment-save').click(); assert.equal((await updated).status(),200); await other.close();
    await tab('storage'); await field('notes').fill('尚未合并的当前修改');
    response = await save(); assert.equal(response.status(),409);
    assert.equal(await field('notes').inputValue(),'尚未合并的当前修改');
    await page.waitForFunction(()=>document.querySelector('#equipment-save-status').textContent.includes('其他页面'));
    const downloaded = page.waitForEvent('download'); await page.locator('#equipment-export').click();
    const download = await downloaded; const exportPath = path.join(output,'equipment-export.json'); await download.saveAs(exportPath);
    const data = JSON.parse(fs.readFileSync(exportPath,'utf8'));
    assert.equal(data.config.notes,'尚未合并的当前修改'); assert.equal(data.unsavedChanges,true);
    assert.equal(data.hardwareVerified,false); assert.equal(data.config.videoChannels,6);
    assert.equal(data.config.nvr.host,'192.0.2.20'); assert.equal(data.config.nvr.evidenceRetentionDays,60);
    page.once('dialog',dialog=>dialog.dismiss()); await page.locator('#equipment-load').click();
    assert.equal(await field('notes').inputValue(),'尚未合并的当前修改');
    page.once('dialog',dialog=>dialog.accept()); await page.locator('#equipment-load').click();
    await page.waitForFunction(()=>document.querySelector('[name="notes"]').value==='另一管理页面的修改');
    // All tabs remain readable at desktop, tablet and narrow phone widths.
    for (const width of [1440,768,390,320]) {
      await page.setViewportSize({width,height:1050});
      for (const id of ['inventory','network','cameras','terminals','storage','runtime']) {
        await tab(id);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`${id} overflow at ${width}`);
        if ((width===1440 && ['inventory','cameras','storage'].includes(id)) || (width===390 && ['network','runtime','storage'].includes(id))) {
          await page.locator('#equipment-settings').screenshot({path:path.join(output,`equipment-${id}-${width}.png`)});
        }
      }
    }
    await tab('inventory');
    await page.locator('#equipment-tab-inventory').press('ArrowRight');
    assert.equal(await page.locator('#equipment-tab-network').getAttribute('aria-selected'),'true');
    assert.deepEqual(errors,[]);
    console.log('PASS equipment settings: real persistence, six mappings, credential/port validation, stale-write protection, export and responsive tabs.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
