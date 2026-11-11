// Real video decoding/capture; held response fixtures exercise preview ownership.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output||process.env.APP_ENV!=='test')throw new Error('Use the disposable test-pilot-ui.py launcher');
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
  const report={passed:false,inferenceMocked:true,checks:[],errors:[]};
  let identity,latest,frames=0,held,release;
  page.on('pageerror',error=>report.errors.push(error.message));
  const checkSurface=async waiting=>{
    const display=await page.evaluate(()=>{
      const surface=document.querySelector('.station-detection-media'),cover=getComputedStyle(surface,'::after');
      const canvas=document.querySelector('#sd-overlay'),video=document.querySelector('#sd-video');
      return {mode:surface.dataset.preview,cover:cover.content,coverColor:cover.backgroundColor,
        coverZ:Number(cover.zIndex),canvasZ:Number(getComputedStyle(canvas).zIndex),
        canvasHidden:canvas.hidden,waiting:!document.querySelector('#sd-preview-wait')?.hidden,
        time:video.currentTime,paused:video.paused};
    });
    assert.equal(display.mode,'detection','Detection mode must own the visible surface even without a fresh result');
    assert.equal(display.cover,'""');assert.equal(display.coverColor,'rgb(8, 13, 20)');
    assert.ok(display.coverZ>0 && display.canvasZ>display.coverZ);
    assert.equal(display.canvasHidden,waiting);assert.equal(display.waiting,waiting);
    assert.equal(display.paused,false);return display;
  };
  try{
    const session=await(await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}})).json();
    const response=await context.request.put(base+'/api/v1/admin/terminals/station-1',{headers:{'X-CSRF-Token':session.csrf},data:{
      workstationId:'preview-test',cameraId:'preview-test',analysisType:'screw',sampleFps:10,maxSampleGapSeconds:1,enabled:true,revision:0}});
    assert.equal(response.status(),200);
    await page.route('**/api/v1/station-streams?*',async route=>{
      const response=await route.fetch();identity=await response.json();await route.fulfill({response,json:identity});
    });
    await page.route('**/api/v1/station-streams/*/frames',async route=>{
      frames++;const headers=route.request().headers();
      latest={...identity,frameSeq:Number(headers['x-frame-seq']),frameTime:Number(headers['x-frame-time']),
        targets:[],people:[],compliance:{status:'REVIEW',label:'检测中',reason:'Preview fixture'}};
      await new Promise(resolve=>{release=resolve;held=frames;});held=null;release=null;
      await route.fulfill({json:latest});
    });
    await page.goto(base+'/?task=station1');await page.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy);
    await page.locator('#sd-source').selectOption('replay');await page.locator('#sd-file').setInputFiles('assets/reference.mp4');
    await page.waitForFunction(()=>StationDetection.state.ready);
    assert.equal(await page.locator('#sd-video').isVisible(),true);
    await page.locator('#sd-start').click();
    while(!held)await page.waitForTimeout(20);
    await checkSurface(true);report.checks.push('Startup covers raw video until first result');
    await page.screenshot({path:path.join(output,'preview-startup.png'),fullPage:true});
    release();await page.waitForFunction(()=>StationDetection.state.frames===1&&!document.querySelector('#sd-overlay').hidden);
    await checkSurface(false);report.checks.push('Fresh result has its own captured image');
    while(!held)await page.waitForTimeout(20);
    await page.waitForFunction(()=>StationDetection.state.resultDeadline===0);
    const expired=await checkSurface(true);report.checks.push('Expired result shows waiting state without revealing raw video');
    const before=expired.time;await page.waitForTimeout(200);
    assert.ok(await page.locator('#sd-video').evaluate((video,before)=>video.currentTime>before,before),'Covered video must continue decoding for sampling');
    await page.screenshot({path:path.join(output,'preview-waiting.png'),fullPage:true});
    release();await page.waitForFunction(()=>StationDetection.state.frames>=2);
    while(held!==3)await page.waitForTimeout(20);
    release();await page.waitForFunction(()=>StationDetection.state.frames>=3&&!document.querySelector('#sd-overlay').hidden);
    await checkSurface(false);report.checks.push('Fresh response resumes detected preview');
    await page.locator('#sd-preview-mode').selectOption('live');
    assert.equal(await page.locator('.station-detection-media').getAttribute('data-preview'),'source');
    assert.equal(await page.locator('#sd-overlay').isHidden(),true);
    assert.equal(await page.locator('#sd-preview-wait').isHidden(),true);
    await page.locator('#sd-preview-mode').selectOption('detection');await checkSurface(true);
    report.checks.push('Explicit mode switch is the only live-source fallback');
    await page.locator('#sd-stop').click();if(release)release();
    await page.waitForFunction(()=>!StationDetection.state.stream&&!StationDetection.state.busy);
    assert.deepEqual(report.errors,[]);report.passed=true;
    console.log(JSON.stringify(report));
  }catch(error){report.failure=error.message;await page.screenshot({path:path.join(output,'preview-failure.png'),fullPage:true});throw error;}
  finally{if(release)release();fs.writeFileSync(path.join(output,'station-preview-ui.json'),JSON.stringify(report,null,2));await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
