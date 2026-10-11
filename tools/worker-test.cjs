const assert=require('node:assert/strict');
const path=require('node:path');
let playwright;
try{playwright=require(process.env.PLAYWRIGHT_MODULE||'playwright');}
catch{playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));}
const {chromium}=playwright;
(async()=>{
  const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:900},acceptDownloads:true}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto((process.env.TEST_BASE_URL||'http://localhost:8080')+'/?video=workers');
    await page.waitForFunction(()=>document.querySelector('#source-video').currentSrc.includes('factory-workers.mp4')&&document.querySelectorAll('#detection-results tr').length>0);
    const state=await page.evaluate(()=>({targets:Number(document.querySelector('#metric-targets').textContent),rows:document.querySelectorAll('#detection-results tr').length,bars:document.querySelectorAll('#time-bars span').length,steps:document.querySelectorAll('#steps .step').length,alerts:document.querySelectorAll('.alert-card').length,compliance:document.querySelector('#metric-compliance').textContent,overflow:document.documentElement.scrollWidth-innerWidth}));
    assert.ok(state.targets>=1&&state.rows>=1&&state.bars>=1);assert.equal(state.steps,3);assert.ok(state.alerts>=1);assert.equal(state.compliance,'—');assert.ok(state.overflow<=0);
    const options=await page.locator('#temporal-track option').evaluateAll(nodes=>nodes.map(n=>n.value));
    assert.ok(options.length>1);
    await page.locator('#temporal-track').selectOption(options[1]);
    const segment=page.locator('[data-seek-action]').first();
    const seek=Number(await segment.getAttribute('data-seek-action'));
    await segment.click();
    await page.waitForFunction(t=>Math.abs(document.querySelector('#source-video').currentTime-t)<.05,seek);
    assert.equal(await page.locator('#temporal-track').inputValue(),options[1]);
    assert.ok(await page.locator('#timeline-playhead').isVisible());
    if(process.env.TEST_OUTPUT_DIR){
      await page.locator('#video-size').focus();await page.keyboard.press('End');
      await page.locator('.video-panel').screenshot({path:path.join(process.env.TEST_OUTPUT_DIR,'worker-skeleton.png')});
    }
    const downloadPromise=page.waitForEvent('download');await page.locator('[data-action="export"]').click();assert.match((await downloadPromise).suggestedFilename(),/动作时间轴/);
    const popupPromise=page.waitForEvent('popup');await page.locator('[data-action="report"]').click();const report=await popupPromise;await report.waitForLoadState();const body=await report.locator('body').textContent();assert.match(body,/工厂工作人员设备检查/);assert.match(body,/工序合规结论：暂不可判定/);assert.match(body,/YOLO11s-pose/);
    assert.deepEqual(errors,[]);console.log('PASS: real worker video, synchronized detections, action timeline, quality events, CSV and detailed report.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
