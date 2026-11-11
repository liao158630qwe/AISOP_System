// Actual browser media lifecycle; decode failures are injected, inference never runs.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE,
  '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if (!base || !output || process.env.APP_ENV!=='test') throw new Error('Use the disposable test-pilot-ui.py launcher');
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),errors=[],checks=[];
  let page,starts=0,frames=0;
  try {
    const response=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(response.status(),200);const session=await response.json();
    assert.equal((await context.request.put(base+'/api/v1/admin/terminals/station-1',{headers:{'X-CSRF-Token':session.csrf},data:{
      workstationId:'media-fixture-pc',cameraId:'media-fixture-camera',analysisType:'screw',source:'usb',enabled:true,revision:0}})).status(),200);
    page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{
      window.__mediaTest={mode:'normal',urls:[],revoked:[],tracks:[]};
      const create=URL.createObjectURL,revoke=URL.revokeObjectURL;
      URL.createObjectURL=function(...args){const url=create.apply(this,args);__mediaTest.urls.push(url);return url;};
      URL.revokeObjectURL=function(url){__mediaTest.revoked.push(url);return revoke.call(this,url);};
      const load=HTMLMediaElement.prototype.load,setTimer=window.setTimeout;
      HTMLMediaElement.prototype.load=function(){
        if(this.id!=='sd-video' || !this.getAttribute('src') || __mediaTest.mode==='normal')return load.call(this);
        if(__mediaTest.mode==='zero')setTimer(()=>this.dispatchEvent(new Event('loadeddata')),0);
      };
      window.setTimeout=function(fn,ms,...args){return setTimer(fn,__mediaTest.mode==='timeout'&&ms===30000?50:ms,...args);};
      navigator.mediaDevices.getUserMedia=async()=>{
        const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;
        canvas.getContext('2d').fillRect(0,0,640,360);
        const stream=canvas.captureStream(10);__mediaTest.tracks.push(...stream.getTracks());return stream;
      };
      const play=HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play=function(){
        if(this.id==='sd-video'&&__mediaTest.mode==='camera-failure')return Promise.reject(new DOMException('摄像头播放失败','NotSupportedError'));
        return play.call(this);
      };
    });
    await page.route('**/api/v1/station-streams?*',route=>{starts++;return route.fulfill({status:500,json:{message:'Must validate media before creating a stream'}});});
    await page.route('**/api/v1/station-streams/*/frames',route=>{frames++;return route.fulfill({status:500,json:{message:'No inference expected'}});});
    await page.goto(base+'/?task=station1');await page.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy);
    const file={name:'undecodable.mp4',mimeType:'video/mp4',buffer:Buffer.from('no decodable picture')};
    for(const mode of ['normal','zero','timeout']) {
      await page.locator('#sd-source').selectOption('replay');
      await page.evaluate(mode=>{__mediaTest.mode=mode;},mode);
      await page.locator('#sd-file').setInputFiles(file);
      await page.waitForFunction(()=>StationDetection.state.error.includes('H.264'));
      assert.equal(await page.evaluate(()=>StationDetection.state.ready),false);
      assert.equal(await page.locator('#sd-start').isDisabled(),true);
      assert.equal(await page.evaluate(()=>StationDetection.state.sourceURL),null);
      assert.equal(starts,0);assert.equal(frames,0);checks.push(mode+' video is rejected before opening a round');
    }
    await page.screenshot({path:path.join(output,'unsupported-video.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'unsupported-video-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1000});

    await page.evaluate(()=>{__mediaTest.mode='normal';});
    await page.locator('#sd-file').setInputFiles(path.resolve('assets/reference.mp4'));
    await page.waitForFunction(()=>StationDetection.state.ready);
    assert.equal(await page.locator('#sd-start').isEnabled(),true);
    assert.equal(await page.locator('#sd-source-status').textContent(),'reference.mp4');
    checks.push('supported video decodes normally after a failed load');

    // Replacing a loading source settles the old loader; it cannot mutate the
    // new picture or leave a timeout/error handler alive on the shared video.
    await page.evaluate(()=>{__mediaTest.mode='stall';});
    await page.locator('#sd-file').setInputFiles({...file,name:'pending.mp4'});
    await page.waitForFunction(()=>document.querySelector('#sd-source-status').textContent.includes('正在读取'));
    await page.locator('#sd-source').selectOption('image');
    await page.evaluate(()=>{__mediaTest.mode='normal';});
    await page.locator('#sd-file').setInputFiles(path.join(output,'holes.jpg'));
    await page.waitForFunction(()=>StationDetection.state.ready);
    await page.evaluate(()=>document.querySelector('#sd-video').dispatchEvent(new Event('error')));
    assert.equal(await page.evaluate(()=>StationDetection.state.error),'');
    assert.equal(await page.locator('#sd-start').isEnabled(),true);
    assert.equal(await page.locator('#sd-source-status').textContent(),'holes.jpg');
    checks.push('source replacement cancels stale loaders and valid images recover');

    // A source may lose its decoded dimensions after it was initially ready.
    await page.evaluate(()=>Object.defineProperty(document.querySelector('#sd-image'),'naturalWidth',{configurable:true,get:()=>0}));
    const blocked=await page.evaluate(()=>StationDetection.start().then(()=>'',error=>error.message));
    assert.match(blocked,/有效画面/);assert.equal(starts,0);
    await page.evaluate(()=>delete document.querySelector('#sd-image').naturalWidth);
    checks.push('decoded dimensions are rechecked at start');

    await page.locator('#sd-source').selectOption('camera');
    await page.evaluate(()=>{__mediaTest.mode='camera-failure';});
    await page.locator('#sd-camera').click();
    await page.waitForFunction(()=>StationDetection.state.error.includes('摄像头播放失败'));
    assert.equal(await page.evaluate(()=>StationDetection.state.media),null);
    assert.ok(await page.evaluate(()=>__mediaTest.tracks.length>0&&__mediaTest.tracks.every(track=>track.readyState==='ended')));
    assert.ok(await page.evaluate(()=>__mediaTest.urls.every(url=>__mediaTest.revoked.includes(url))));
    checks.push('failed camera playback releases tracks and selected object URLs');
    assert.equal(starts,0);assert.equal(frames,0);assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'station-media-ui.json'),JSON.stringify({passed:true,checks,starts,frames,errors},null,2));
    console.log(JSON.stringify({passed:checks.length,starts,frames,errors}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
