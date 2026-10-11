const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
  const browser=await chromium.launch({channel:'msedge'});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://localhost:8080/?video=factory');
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    const meta=await page.locator('#source-video').evaluate(v=>({duration:v.duration,width:v.videoWidth,height:v.videoHeight,playing:!v.paused,loop:v.loop}));
    assert.ok(meta.playing&&meta.loop);assert.ok(meta.duration>0);
    await page.locator('#video-seek').evaluate(el=>{el.value=2;el.dispatchEvent(new Event('input',{bubbles:true}));});
    await page.waitForFunction(()=>document.querySelector('#source-video').currentTime>=2);
    await page.screenshot({path:'reference/factory-test.png'});
    assert.equal(await page.locator('#detection-results tr').count(),0);
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#source-video').currentSrc.includes('factory-test.mp4')&&document.querySelector('#source-video').readyState>=2);
    await page.locator('[data-action="upload"]').click();
    await page.locator('[data-action="test-video"]').click();
    await page.waitForFunction(()=>document.querySelector('#source-video').currentSrc.includes('people-test.mp4')&&document.querySelector('#source-video').readyState>=2);
    await page.locator('[data-action="upload"]').click();
    await page.locator('[data-action="factory-video"]').click();
    await page.waitForFunction(()=>document.querySelector('#source-video').currentSrc.includes('factory-test.mp4')&&document.querySelector('#source-video').readyState>=2);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'PASS',...meta,checks:['factory playback','seek','reload','people/factory switching','no simulated detections','no browser errors']}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
