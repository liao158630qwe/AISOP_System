// Original dashboard + actual inference API. Live video is a recorded fixture.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const playwright=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),out=path.join(root,'outputs/screw-inspection-2026-10-17');
(async()=>{
  const camera=path.join(root,'.runtime/screw-live-camera.y4m');
  const args=fs.existsSync(camera)?['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--use-file-for-fake-video-capture='+camera]:[];
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true,args});
  const errors=[];
  try{
    const context=await browser.newContext({viewport:{width:1512,height:1100},permissions:['camera']});
    const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
    const reference=JSON.parse(fs.readFileSync(path.join(root,'.runtime/screw-reference-run.json'),'utf8'));
    // Even saved legacy state must not reopen video on login or refresh.
    await page.addInitScript(id=>localStorage.setItem('aisop-screw-run',id),reference.id);
    await page.goto('http://127.0.0.1:8000/');
    await page.locator('#server-account').waitFor();
    assert.equal(await page.locator('.dashboard #monitor').count(),1);
    assert.equal(await page.locator('.screw-focus-strip,#screw-focus-canvas').count(),0,'Homepage target close-up has been removed');
    assert.equal(await page.locator('.sidebar .workflow-panel').count(),1);
    await page.locator('#server-account').click();
    const credentials=JSON.parse(fs.readFileSync(path.join(root,'.runtime/local-settings.json'),'utf8'));
    await page.locator('#login-user').fill(credentials.username);await page.locator('#login-password').fill(credentials.password);
    await page.locator('#screw-login button[type=submit]').click();
    await page.waitForFunction(()=>window.ScrewApp?.state.session&&window.ScrewApp.state.model);
    const blank=()=>page.evaluate(()=>window.ScrewApp.state.result===null&&window.ScrewApp.state.run===null&&
      !document.querySelector('#source-video').getAttribute('src')&&window.ScrewApp.state.final===null&&
      !document.querySelector('#video-empty').hidden);
    assert.ok(await blank(),'Login must keep the workspace empty despite a saved run and reference video');
    const openHistory=async()=>{
      await page.locator('[data-action=screw-history]').click();
      await page.locator(`[data-screw-run="${reference.id}"]`).click();
      await page.waitForFunction(()=>window.ScrewApp?.state.result&&document.querySelector('#source-video').readyState>=2);
    };
    await openHistory();
    await page.evaluate(()=>document.querySelector('#source-video').pause());
    await page.waitForLoadState('networkidle');
    let automaticMediaRequests=0;
    const countMedia=request=>{if(/\/api\/v1\/analysis-runs\/[^/]+(?:\/artifacts\/|\/wait|$)/.test(request.url()))automaticMediaRequests++;};
    page.on('request',countMedia);
    await page.reload();
    await page.waitForFunction(()=>window.ScrewApp?.state.session&&window.ScrewApp.state.model);
    assert.ok(await blank(),'Refresh after opening history must start with an empty workspace');
    assert.equal(automaticMediaRequests,0,'Refresh must not request the previous run or its video');
    await page.goto(`http://127.0.0.1:8000/?run=${reference.id}&video=workers`);
    await page.waitForFunction(()=>window.ScrewApp?.state.session&&window.ScrewApp.state.model);
    assert.ok(await blank(),'Legacy URL arguments must not automatically load a video');
    assert.equal(automaticMediaRequests,0);
    page.off('request',countMedia);
    await openHistory();
    await page.waitForFunction(()=>window.ScrewApp?.state.result&&document.querySelector('#source-video').readyState>=2);
    await page.evaluate(()=>document.querySelector('#source-video').pause());
    // Continuous playback must render every 4fps sample, not the 1Hz clock.
    await page.evaluate(()=>document.querySelector('#source-video').currentTime=30.9);
    await page.waitForFunction(()=>!document.querySelector('#source-video').seeking);
    await page.evaluate(()=>{
      window.__replaySamples=[];
      window.__replayObserver=new MutationObserver(()=>{
        const rows=[...document.querySelectorAll('#detection-results tr')];
        const frame=window.ScrewApp.state.renderedPanelFrame;
        const stamp=frame?.time;
        if(!Number.isFinite(stamp)||window.__replaySamples.some(s=>s.time===stamp))return;
        const aggregate=frame.aggregateTargets||frame.inspection.targets;
        const completed=aggregate.filter(t=>['COMPLETED','PREASSEMBLED_OK'].includes(t.sequenceState)).length;
        const confirmedStatuses=['已确认','预装件已确认'];
        window.__replaySamples.push({time:stamp,completed,
          rows:rows.filter(r=>confirmedStatuses.includes(r.children[1].textContent)).length,
          steps:[...document.querySelectorAll('#steps .step-status')].filter(s=>confirmedStatuses.includes(s.textContent)).length});
      });
      window.__replayObserver.observe(document.querySelector('#result-note'),{subtree:true,childList:true,characterData:true});
      return document.querySelector('#source-video').play();
    });
    await page.waitForFunction(()=>document.querySelector('#source-video').currentTime>=31.95);
    await page.evaluate(()=>{document.querySelector('#source-video').pause();window.__replayObserver.disconnect();});
    const playbackSamples=await page.evaluate(()=>window.__replaySamples.filter(s=>s.time>=31&&s.time<32));
    assert.ok(playbackSamples.length>=4,`Expected four distinct replay samples, got ${JSON.stringify(playbackSamples)}`);
    assert.ok(playbackSamples.every(s=>s.rows===s.completed&&s.steps===s.completed),JSON.stringify(playbackSamples));
    for(const t of [0,9,26]){
      await page.evaluate(t=>document.querySelector('#source-video').currentTime=t,t);
      await page.waitForFunction(t=>!document.querySelector('#source-video').seeking&&Math.abs(document.querySelector('#source-video').currentTime-t)<.1,t);
      await page.waitForFunction(()=>{const s=window.ScrewApp.state;let f=s.result.frames[0];for(const frame of s.result.frames){if(frame.time<=document.querySelector('#source-video').currentTime)f=frame;else break;}return s.drawnBoxes.length===f.inspection.displayTargets.length;});
      assert.ok(await page.evaluate(()=>window.ScrewApp.state.drawnBoxes.every(b=>b.mode==='DETECTED'&&['PRESENT','EMPTY'].includes(b.state))));
    }
    // All 10 configured operations complete at 48.267s; show the result at once,
    // but keep the replay/stream alive instead of ending the inspection.
    await page.evaluate(()=>document.querySelector('#source-video').currentTime=48.3);
    await page.locator('#screw-verdict').filter({hasText:'即时判定：合规'}).waitFor();
    const early=await page.evaluate(()=>{const v=document.querySelector('#source-video'),s=window.ScrewApp.state;return {time:v.currentTime,duration:v.duration,paused:v.paused,final:s.final,timely:s.timelyDecision?.status,running:s.running};});
    assert.ok(early.time<early.duration-1,`Expected an early verdict before video end, got ${JSON.stringify(early)}`);
    assert.equal(early.final,null,'Timely replay verdict must not end the run');
    assert.equal(early.timely,'PASS');
    assert.match(await page.locator('#workflow-status').textContent(),/^即时：合规 · 检测继续$/);
    assert.equal(await page.locator('#screw-verdict').getAttribute('data-verdict'),'PASS');
    assert.equal(await page.locator('#screw-verdict').getAttribute('data-mode'),'timely');
    assert.equal(await page.locator('#modal').isVisible(),false,'Timely verdict must not open a final-result modal');
    const confirmedOrder=await page.evaluate(()=>{
      const frame=window.ScrewApp.state.result.frames.filter(f=>f.time<=document.querySelector('#source-video').currentTime).at(-1);
      const aggregate=frame.aggregateTargets||frame.inspection.targets;
      const expected=aggregate.filter(t=>['COMPLETED','PREASSEMBLED_OK'].includes(t.sequenceState))
        .sort((a,b)=>(a.confirmedAt??Infinity)-(b.confirmedAt??Infinity)).map(t=>t.id);
      return {expected,
        rows:[...document.querySelectorAll('#detection-results tr')].map(row=>row.dataset.screwTarget),
        steps:[...document.querySelectorAll('#steps .screw-step')].map(row=>row.dataset.screwTarget),
        stepIndexes:[...document.querySelectorAll('#steps .screw-step .step-index')].map(node=>node.textContent)};
    });
    assert.deepEqual(confirmedOrder.rows,confirmedOrder.expected,'Result details must follow confirmation time');
    assert.deepEqual(confirmedOrder.steps,confirmedOrder.expected,'Process steps must follow confirmation time');
    assert.deepEqual(confirmedOrder.stepIndexes.filter(value=>value!=='预'),
      Array.from({length:10},(_,index)=>String(index+1)),'Operation steps must be numbered top to bottom');
    await page.evaluate(()=>document.querySelector('#source-video').currentTime=20.1);
    await page.waitForFunction(()=>!document.querySelector('#source-video').seeking&&window.ScrewApp.state.final===null&&window.ScrewApp.state.timelyDecision===null);
    assert.equal(await page.locator('#screw-verdict').isVisible(),false);
    const expectedAtSeek=await page.evaluate(()=>{
      const frame=window.ScrewApp.state.result.frames.filter(f=>f.time<=20.1).at(-1);
      const aggregate=frame.aggregateTargets||frame.inspection.targets;
      return {
        confirmed:aggregate.filter(t=>['COMPLETED','PREASSEMBLED_OK','EMPTY','PREASSEMBLED_MISSING'].includes(t.sequenceState)).map(t=>t.id),
        operations:aggregate.filter(t=>t.sequenceState==='COMPLETED').map(t=>t.id)
      };
    });
    assert.ok(expectedAtSeek.confirmed.length>0,'At least one position must be confirmed by 20.1s');
    assert.ok(expectedAtSeek.operations.every(id=>expectedAtSeek.confirmed.includes(id)));
    assert.equal(await page.locator('#steps .completed').count(),expectedAtSeek.operations.length);
    assert.equal(await page.locator('#metric-targets').textContent(),String(expectedAtSeek.confirmed.length));
    assert.equal(await page.locator('#metric-targets').evaluate(el=>el.nextElementSibling.textContent),'处');
    assert.equal(await page.locator('#noncompliant-count').textContent(),'0','Uninstalled positions must remain pending, not fail during the cycle');
    assert.equal(await page.locator('#metric-alerts').textContent(),'0');
    assert.ok(await page.locator('#steps .step-status').filter({hasText:'当前为空'}).count()>0);
    assert.equal(await page.locator('#steps .step-status').filter({hasText:'明确空孔'}).count(),0);
    await page.locator('#finish-detection').click();
    await page.locator('#modal [data-action=close-modal]').last().click();
    await page.locator('[data-screw-seek]').filter({hasText:'上部连接件'}).click();
    await page.waitForFunction(()=>!document.querySelector('#source-video').seeking);
    await page.waitForFunction(()=>window.ScrewApp.state.drawnBoxes.length>0);
    await page.locator('#finish-detection').click();await page.locator('#modal [data-action=close-modal]').last().click();
    await page.screenshot({path:path.join(out,'restored-desktop.png'),fullPage:true});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(out,'restored-mobile.png'),fullPage:true});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.setViewportSize({width:1512,height:1100});
    if(fs.existsSync(camera)){
      let frameRequests=0,forcePass=false;
      await page.route('**/api/v1/realtime/frame?analysis_type=screw',async route=>{
        if(++frameRequests>1)await new Promise(resolve=>setTimeout(resolve,4000));
        const response=await route.fetch().catch(()=>null);
        if(!response){await route.continue().catch(()=>{});return;}
        if(!forcePass){await route.fulfill({response}).catch(()=>{});return;}
        const body=await response.json().catch(()=>null);
        if(body?.aggregateTargets?.length){
          const order=body.sequence?.order?.length?body.sequence.order:
            body.aggregateTargets.filter(t=>!t.preassembled).map(t=>t.id);
          const preassembled=Object.fromEntries(body.aggregateTargets.filter(t=>t.preassembled)
            .map(t=>[t.id,'PREASSEMBLED_OK']));
          const targets=body.aggregateTargets.map(t=>({...t,state:'PRESENT',score:.99,
            sequenceState:Object.prototype.hasOwnProperty.call(preassembled,t.id)?'PREASSEMBLED_OK':'COMPLETED'}));
          const positionsConfirmed=targets.filter(t=>['COMPLETED','PREASSEMBLED_OK'].includes(t.sequenceState)).length;
          const sequence={...(body.sequence||{}),configured:true,verificationMode:'visual-cumulative',status:'PASS',order,cursor:order.length,
            current:null,pending:[],completed:[...order],early:[],empty:[],preassembled,
            reason:'测试夹具：全部位置累计确认，预装件可见',failure:null,events:[],
            operationsExpected:order.length,operationsCompleted:order.length,
            positionsExpected:targets.length,positionsConfirmed,
            preassembledExpected:Object.keys(preassembled).length,
            preassembledConfirmed:Object.keys(preassembled).length};
          body.aggregateTargets=targets;body.historyTargets=targets;body.sequence=sequence;
          if(body.inspection){
            body.inspection.sequence=sequence;
            body.inspection.targets=(body.inspection.targets||[]).map(t=>({...t,
              sequenceState:Object.prototype.hasOwnProperty.call(preassembled,t.id)?'PREASSEMBLED_OK':
                order.includes(t.id)?'COMPLETED':'UNKNOWN'}));
          }
          body.compliance={status:'PASS',label:'合规',reason:'测试夹具：全部位置累计确认'};
        }
        await route.fulfill({response,json:body}).catch(()=>{});
      });
      await page.locator('#video-mode').selectOption('realtime');
      await page.waitForFunction(()=>window.ScrewApp.state.running&&window.ScrewApp.state.frames.length>=1,null,{timeout:90000});
      const firstId=await page.evaluate(()=>window.ScrewApp.state.streamId);
      await page.waitForFunction(()=>window.ScrewApp.state.drawnBoxes.length>0);
      assert.ok(await page.evaluate(()=>window.ScrewApp.state.drawnBoxes.every(b=>b.mode==='DETECTED')));
      assert.equal(await page.locator('.cpu-chip').textContent(),await page.evaluate(()=>window.ScrewApp.state.model.modelName));
      await page.screenshot({path:path.join(out,'restored-realtime.png'),fullPage:true});
      await page.evaluate(()=>{
        const detail=document.querySelector('#detection-results'),steps=document.querySelector('#steps');
        window.__panelProbe={detail:detail.firstElementChild,step:steps.firstElementChild,
          detailText:detail.textContent,stepText:steps.textContent,removed:0};
        window.__panelProbe.step.focus();
        const observer=new MutationObserver(records=>{window.__panelProbe.removed+=records.reduce((n,r)=>n+r.removedNodes.length,0);});
        observer.observe(detail,{childList:true});observer.observe(steps,{childList:true});
        window.__panelProbe.observer=observer;
      });
      await page.waitForFunction(()=>window.__panelProbe&&window.ScrewApp.state.drawnBoxes.length===0,null,{timeout:5000});
      assert.ok(await page.evaluate(()=>{const p=window.__panelProbe;
        return document.querySelector('#detection-results').firstElementChild===p.detail&&
          document.querySelector('#steps').firstElementChild===p.step&&p.removed===0&&
          document.querySelector('#detection-results').textContent===p.detailText&&
          document.querySelector('#steps').textContent===p.stepText&&document.activeElement===p.step;
      }),'Result rows and process steps must survive expiry without being rebuilt or reset');
      assert.match(await page.locator('#result-note').textContent(),/本轮已确认 .*最近采样/);
      assert.equal(await page.locator('#detection-results tr').count(),12);
      assert.equal(await page.locator('#steps .screw-step').count(),12);
      assert.equal(await page.locator('.screw-focus-strip,#screw-focus-canvas').count(),0);
      await page.evaluate(()=>window.__panelProbe?.observer.disconnect());
      // Force the accumulated history to a complete PASS and verify the UI
      // publishes the result without ending the live stream.
      forcePass=true;
      await page.waitForFunction(()=>window.ScrewApp.state.timelyDecision?.status==='PASS',null,{timeout:30000});
      await page.locator('#screw-verdict').filter({hasText:'即时判定：合规'}).waitFor();
      const timely=await page.evaluate(()=>{const s=window.ScrewApp.state;return {running:s.running,streamId:s.streamId,final:s.final,frames:s.frames.length,timely:s.timelyDecision?.status};});
      assert.equal(timely.running,true,'Live inspection must keep running after the result appears');
      assert.ok(timely.streamId,'Live stream must remain active');
      assert.equal(timely.final,null,'Timely live verdict must not end the run');
      assert.equal(timely.timely,'PASS');
      assert.equal(await page.locator('#modal').isVisible(),false,'Timely verdict must not open a final-result modal');
      await page.waitForFunction(n=>window.ScrewApp.state.frames.length>n,timely.frames,{timeout:30000});
      assert.ok(await page.evaluate(id=>window.ScrewApp.state.running&&window.ScrewApp.state.streamId===id,timely.streamId),'Live sampling must continue after the timely verdict');
      await page.locator('#finish-detection').click();
      await page.waitForFunction(()=>!window.ScrewApp.state.busy&&window.ScrewApp.state.stream===null);
      assert.equal(await page.evaluate(()=>window.ScrewApp.state.final?.final),true);
      assert.equal(await page.evaluate(()=>window.ScrewApp.state.final?.auto),undefined,'Manual finish must not be marked automatic');
      await page.locator('#modal [data-action=close-modal]').last().click();
      await page.unrouteAll({behavior:'wait'});
      await page.locator('#start-live').click();
      await page.waitForFunction(id=>window.ScrewApp.state.running&&window.ScrewApp.state.streamId!==id&&window.ScrewApp.state.final===null,firstId,{timeout:90000});
      await page.waitForFunction(()=>window.ScrewApp.state.frames.length>=1,null,{timeout:90000});
      assert.equal(await page.evaluate(()=>window.ScrewApp.state.final),null);
      await page.locator('#finish-detection').click();await page.waitForFunction(()=>!window.ScrewApp.state.busy&&window.ScrewApp.state.stream===null);
      assert.equal(await page.evaluate(()=>window.ScrewApp.state.final?.auto),undefined,'Manual finish must remain available');
      await page.locator('#modal [data-action=close-modal]').last().click();
      await page.unrouteAll({behavior:'wait'});
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'restored-browser-result.json'),JSON.stringify({passed:true,liveFixture:fs.existsSync(camera),errors},null,2));
    console.log('Original UI, current-frame boxes, stable detail/process nodes and focus during expiry, timely verdict while the stream continues, manual finish and cycle reset: passed');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
