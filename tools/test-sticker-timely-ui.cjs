// Focused UI regression for the sticker timely verdict popup.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const playwright=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),out=path.join(root,'outputs/sticker-inspection-2026-10-17');
(async()=>{
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true});
  const errors=[];
  try{
    const page=await browser.newPage({viewport:{width:1512,height:1100}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto('http://127.0.0.1:8000/');
    await page.locator('#server-account').waitFor();
    await page.locator('#server-account').click();
    const credentials=JSON.parse(fs.readFileSync(path.join(root,'.runtime/local-settings.json'),'utf8'));
    await page.locator('#login-user').fill(credentials.username);
    await page.locator('#login-password').fill(credentials.password);
    await page.locator('#screw-login button[type=submit]').click();
    await page.waitForFunction(()=>window.ScrewApp?.state.session&&window.ScrewApp.state.model);

    const replay=await page.evaluate(async()=>{
      const model=await fetch('/api/v1/sticker-model').then(response=>response.json());
      const targets=model.targets;
      const events=targets.map((target,index)=>({id:target.id,time:10+index,confirmedAt:10.25+index,score:.9}));
      const sequence={mode:'action',status:'PASS',stage:'动作记录齐全',trianglesObserved:2,trianglesExpected:2,
        rectanglesObserved:6,rectanglesExpected:6,trianglesCompleteAt:11,firstRectangleAt:12,events,violations:[],
        reason:'两处三角动作先完成，后续六处方形动作已逐位置确认'};
      const frame={time:0,end:30,inspection:{registered:true,targets:targets.map(target=>({...target,state:'UNKNOWN'})),
        displayTargets:[],sequence},aggregateTargets:targets.map(target=>({...target,state:'ACTION_DONE'}))};
      const state=window.ScrewApp.state;
      Object.assign(state,{task:'sticker',model,mode:'replay',run:'fixture-sticker-timely',busy:false,running:false,
        result:{analysisType:'sticker',inspectionMode:'action',targets:targets.map(target=>({...target,state:'ACTION_DONE'})),
          frames:[frame],sequence,compliance:{status:'PASS',label:'动作合规',reason:sequence.reason},
          summary:{expected:targets.length,sampleFps:4,analyzedFrames:1},source:{title:'fixture.mp4',duration:30},
          model:{name:'fixture'}},final:null,finalShown:false,timelyDecision:null,timelyShown:false,timelyModalKey:null,
        liveCompliance:null,processing:0});
      window.ScrewApp.render();
      return {targets:targets.length,modal:document.querySelector('#modal').open,
        title:document.querySelector('#modal-title').textContent,
        body:document.querySelector('#modal-body').textContent,
        final:state.final,timely:state.timelyDecision?.status,key:state.timelyModalKey,
        videoTime:document.querySelector('#source-video').currentTime};
    });
    assert.equal(replay.targets,8);
    assert.equal(replay.modal,true,'A completed sticker sequence must open the timely result popup');
    assert.equal(replay.title,'贴条工序即时判定');
    assert.match(replay.body,/动作合规/);
    assert.match(replay.body,/8\/8 处贴放\/按压动作已确认/);
    assert.equal(replay.final,null,'The timely popup must not mark the inspection as finished');
    assert.equal(replay.timely,'PASS');
    assert.equal(replay.key,'fixture-sticker-timely');

    await page.locator('#modal [data-action=close-modal]').last().click();
    const closed=await page.evaluate(()=>{
      window.ScrewApp.render();
      return {modal:document.querySelector('#modal').open,key:window.ScrewApp.state.timelyModalKey};
    });
    assert.equal(closed.modal,false,'The timely popup must not reopen for the same run');
    assert.equal(closed.key,'fixture-sticker-timely');

    const live=await page.evaluate(()=>{
      const state=window.ScrewApp.state,model=state.model,targets=model.targets;
      const events=targets.map((target,index)=>({id:target.id,time:index,confirmedAt:index+.25,score:.9}));
      const sequence={mode:'action',status:'PASS',stage:'动作记录齐全',trianglesObserved:2,trianglesExpected:2,
        rectanglesObserved:6,rectanglesExpected:6,trianglesCompleteAt:1,firstRectangleAt:2,events,violations:[],
        reason:'两处三角动作先完成，后续六处方形动作已逐位置确认'};
      Object.assign(state,{task:'sticker',mode:'realtime',running:true,streamId:'fixture-live',result:null,busy:false,
        final:null,finalShown:false,timelyDecision:null,timelyShown:false,timelyModalKey:null,
        liveCompliance:{status:'PASS',label:'动作合规',reason:sequence.reason},
        lastResponse:performance.now(),lastSampleAt:performance.now(),started:performance.now(),processing:0,
        frame:{time:1,inspection:{registered:true,targets:targets.map(target=>({...target,state:'UNKNOWN'})),
          displayTargets:[],sequence},aggregateTargets:[]},frames:[]});
      window.ScrewApp.render();
      return {modal:document.querySelector('#modal').open,running:state.running,streamId:state.streamId,
        final:state.final,timely:state.timelyDecision?.status,key:state.timelyModalKey};
    });
    assert.equal(live.modal,true,'A completed live sticker sequence must open the timely result popup');
    assert.equal(live.running,true,'Live inspection must keep running after the timely popup');
    assert.equal(live.streamId,'fixture-live','Live stream must remain active after the timely popup');
    assert.equal(live.final,null,'The live timely popup must not end the run');
    assert.equal(live.timely,'PASS');
    assert.equal(live.key,'fixture-live');

    await page.locator('#modal [data-action=close-modal]').last().click();
    const liveAfter=await page.evaluate(()=>({running:window.ScrewApp.state.running,
      streamId:window.ScrewApp.state.streamId,modal:document.querySelector('#modal').open}));
    assert.equal(liveAfter.running,true);
    assert.equal(liveAfter.streamId,'fixture-live');
    assert.equal(liveAfter.modal,false);
    assert.deepEqual(errors,[]);
    fs.mkdirSync(out,{recursive:true});
    fs.writeFileSync(path.join(out,'sticker-timely-ui-test.json'),JSON.stringify({passed:true,replay,live},null,2));
    console.log('Sticker timely verdict popup: passed');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
