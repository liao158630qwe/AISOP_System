// Real API fixture is owned by test-evidence-history.py. Never uses the local account.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
(async()=>{
  const base=process.env.EVIDENCE_TEST_BASE, output=process.env.EVIDENCE_TEST_OUTPUT;
  assert.ok(base?.startsWith('http://127.0.0.1:'),'Use only the isolated fixture server');
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(login.status(),200);
    const page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base+'/');
    await page.waitForFunction(()=>Boolean(window.ScrewApp?.state.session));
    await page.locator('#evidence-history-open').click();
    const host=page.locator('#evidence-history');
    const settled=()=>page.waitForFunction(()=>document.querySelector('#evidence-history')?.getAttribute('aria-busy')==='false');
    const selectStation=async station=>{await page.locator('#evidence-station').selectOption(station);await settled();};
    await settled();
    assert.equal(await page.locator('#evidence-station').inputValue(),'station-1');
    assert.equal(await host.locator('tbody tr').count(),1);
    assert.equal(await host.locator('.evidence-station-name').textContent(),'工位1');
    assert.equal(await page.locator('#evidence-station option[value="all"]').count(),0);
    await selectStation('station-2');
    await host.locator('tbody tr').nth(29).waitFor();
    assert.equal(await host.locator('tbody tr').count(),30);
    assert.match(await host.textContent(),/至少保留30天/);
    await page.locator('#evidence-more').click();
    await page.waitForFunction(()=>document.querySelectorAll('#evidence-history tbody tr').length===33);
    assert.equal(await host.locator('tbody tr').count(),33);
    assert.deepEqual([...new Set(await host.locator('.evidence-station-name').allTextContents())],['工位2']);
    const retry=host.locator('[data-evidence-retry="'+process.env.EVIDENCE_TEST_FAILED+'"]');
    await retry.click();
    const deadline=Date.now()+10000;
    while(true){
      const response=await context.request.get(base+'/api/v1/alerts/'+process.env.EVIDENCE_TEST_FAILED);
      const row=await response.json();
      if(row.status==='READY')break;
      assert.ok(Date.now()<deadline,'Retry must reach READY');
      await page.waitForTimeout(100);
    }
    await host.locator('#evidence-refresh').click();
    await page.waitForFunction(id=>Boolean(document.querySelector('[data-evidence-play="'+id+'"]')),process.env.EVIDENCE_TEST_FAILED);
    await page.screenshot({path:path.join(output,'history.png'),fullPage:true});
    await host.locator('[data-evidence-play="'+process.env.EVIDENCE_TEST_READY+'"]').click();
    const video=page.locator('#modal-body video');
    await video.waitFor();
    await page.waitForFunction(()=>document.querySelector('#modal-body video')?.readyState>=2);
    assert.equal(await video.evaluate(element=>element.videoWidth),320);
    assert.equal(await video.evaluate(element=>element.videoHeight),180);
    await video.evaluate(async element=>{await element.play();});
    await page.waitForFunction(()=>document.querySelector('#modal-body video').currentTime>.2);
    await video.evaluate(element=>element.pause());
    assert.match(await page.locator('#modal-body').textContent(),/SHA-256/);
    await page.screenshot({path:path.join(output,'playback.png'),fullPage:true});
    assert.match(await page.locator('#modal-title').textContent(),/工位2/);
    await page.locator('#evidence-back').click();
    assert.equal(await page.locator('#evidence-station').inputValue(),'station-2');
    await selectStation('station-6');
    assert.equal(await host.locator('tbody tr').count(),0);
    assert.match(await host.textContent(),/工位6暂无已记录的告警/);
    assert.equal(await page.locator('#evidence-more').count(),0);
    await selectStation('unassigned');
    assert.equal(await host.locator('tbody tr').count(),1);
    assert.equal(await host.locator('.evidence-station-name').textContent(),'未关联工位');
    // Hold a real response while another station loads; the late page must not
    // overwrite the new selection or restore the old pagination cursor.
    let release,arrived,finished;
    const gate=new Promise(resolve=>{release=resolve;}),intercepted=new Promise(resolve=>{arrived=resolve;}),delivered=new Promise(resolve=>{finished=resolve;});
    await page.route('**/api/v1/alerts?*',async route=>{
      const response=await route.fetch();
      const held=new URL(route.request().url()).searchParams.get('station_id')==='station-2';
      if(held){arrived();await gate;}
      await route.fulfill({response});if(held)finished();
    });
    await page.locator('#evidence-station').selectOption('station-2');
    await intercepted;
    assert.equal(await host.locator('tbody tr').count(),0);
    await selectStation('station-1');
    release();await delivered;
    await page.waitForTimeout(150);
    assert.equal(await host.locator('tbody tr').count(),1);
    assert.equal(await host.locator('.evidence-station-name').textContent(),'工位1');
    assert.equal(await page.locator('#evidence-more').count(),0);
    await page.unroute('**/api/v1/alerts?*');
    // A failed station request must show its own error, never previous rows.
    await page.route('**/api/v1/alerts?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'测试：暂时无法读取'})}),{times:1});
    await selectStation('station-6');
    assert.equal(await host.locator('tbody tr').count(),0);
    assert.match(await page.locator('#evidence-error').textContent(),/暂时无法读取/);
    await page.locator('#evidence-refresh').click();await settled();
    assert.equal(await page.locator('#evidence-error').textContent(),'');
    // A station page supplies its own context, regardless of the last filter.
    await page.goto(base+'/?task=station3');
    await page.waitForFunction(()=>window.StationDetection?.active&&window.Workspace?.session);
    await page.locator('#evidence-history-open').click();await settled();
    assert.equal(await page.locator('#evidence-station').inputValue(),'station-3');
    assert.equal(await host.locator('.evidence-station-name').textContent(),'工位3');
    await page.setViewportSize({width:390,height:844});
    await selectStation('station-2');
    await page.screenshot({path:path.join(output,'history-mobile.png'),fullPage:true});
    const modalBounds=await page.locator('#modal').boundingBox();
    assert.ok(modalBounds.x>=0&&modalBounds.x+modalBounds.width<=391&&modalBounds.y>=0&&modalBounds.y+modalBounds.height<=845);
    assert.ok(await page.locator('#evidence-station').isVisible());
    await selectStation('station-1');
    assert.equal(await host.locator('tbody tr').count(),1);
    await page.setViewportSize({width:1440,height:1000});
    await page.goto(base+'/board');
    await page.waitForFunction(()=>document.querySelector('#connection')?.dataset.state==='live');
    await page.locator('#history-open').click();
    await page.locator('[data-history-detail="'+process.env.EVIDENCE_TEST_STREAM+'"]').click();
    const detail=page.locator('#history-detail');
    await detail.locator('.history-evidence summary').click();
    assert.match(await detail.textContent(),/人工确认后保留告警录像/);
    assert.match(await detail.textContent(),/保留至/);
    const archivedVideo=detail.locator('video');
    assert.equal(await archivedVideo.getAttribute('src'),base+'/api/v1/alerts/'+process.env.EVIDENCE_TEST_READY+'/video');
    await archivedVideo.evaluate(async element=>{await element.play();});
    await page.waitForFunction(()=>document.querySelector('#history-detail video')?.currentTime>.2);
    assert.equal(await archivedVideo.evaluate(element=>element.videoWidth),320);
    await archivedVideo.evaluate(element=>element.pause());
    await page.screenshot({path:path.join(output,'station-history-playback.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    const report={passed:true,scope:'Real SQLite/API/MP4/auth with synthetic frames and verdicts',
      checks:['archive-entry','station-selector','no-combined-view','current-station-default','unassigned-history',
        'station-isolation','empty-station','pagination-reset','late-response-isolation','failed-filter-recovery',
        'mobile-station-selection','playback-return-keeps-station','30-row-first-page','33-row-pagination','failed-retry-to-ready','retention-date',
        'real-mp4-browser-decode','video-playback','sha256-display','station-finish-only-failure',
        'manual-review-keeps-video','station-history-playback','no-page-errors'],pageErrors:errors};
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
