// Real isolated API/models: F8 must detect on the dashboard without workpieces.
const assert = require('node:assert/strict');
const path = require('node:path'), fs = require('node:fs');
const {chromium} = require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE, output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output)throw new Error('Run tools/test-pilot-ui.py --registration-paused');
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
  const context=await browser.newContext({viewport:{width:1440,height:1000},permissions:['camera']});
  const page=await context.newPage(),errors=[],manualPosts=[],streams=[];
  const board=await context.newPage(), snapshots=[], transitions=[];
  board.on('pageerror',e=>errors.push('board: '+e.message));
  board.on('response',async response=>{
    if(response.url().includes('/board/snapshot')&&response.status()===200){
      const value=await response.json().catch(()=>null);if(value)snapshots.push({receivedAt:Date.now(),...value});
    }
  });
  const stationCard=n=>board.locator(`[data-station="station-${n}"]`);
  async function boardState(n,id,archived,total){
    await board.waitForFunction(({n,id,archived,total})=>{
      const card=document.querySelector(`[data-station="station-${n}"]`);
      return card?.dataset.stream===id && (archived || card.dataset.frameSeq!=='') && card.querySelector('.station-decision').textContent.includes('上轮')===archived &&
        document.querySelector('#total').textContent===String(total);
    },{n,id,archived,total},{timeout:12000});
    const snapshot=[...snapshots].reverse().find(s=>s.stations[n-1].streamId===id&&s.stations[n-1].detection.archived===archived&&s.summary.total===total&&(archived||s.stations[n-1].online));
    assert.ok(snapshot,'Board DOM must come from a real matching API snapshot');
    assert.equal(snapshot.mode,'detection');
    assert.equal(snapshot.summary.total,snapshot.summary.passed+snapshot.summary.failed+snapshot.summary.pending);
    transitions.push({at:Date.now(),station:n,streamId:id,archived,summary:snapshot.summary,card:snapshot.stations[n-1]});
    return snapshot;
  }
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/api/v1/pilot/cycles'))manualPosts.push(r.url());if(r.method()==='POST'&&r.url().includes('/api/v1/station-streams?'))streams.push(r.url());});
  try {
    await page.goto(base+'/?task=station1');
    await page.locator('#workspace-login [name=username]').fill(process.env.APP_USERNAME);
    await page.locator('#workspace-login [name=password]').fill(process.env.APP_PASSWORD);
    await page.locator('#workspace-login button[type=submit]').click();
    await page.locator('#workspace-login').waitFor({state:'hidden'});
    await board.goto(base+'/board');
    await board.waitForFunction(()=>document.querySelector('#connection').dataset.state==='live');
    assert.equal(await board.locator('#total').textContent(),'0');
    await board.evaluate(()=>{
      window.boardFrames=[]; const seen=new Set();
      new MutationObserver(()=>{
        for(const card of document.querySelectorAll('[data-station]')){
          const seq=card.dataset.frameSeq,key=card.dataset.stream+':'+seq;
          if(seq===''||seen.has(key))continue;seen.add(key);
          boardFrames.push({streamId:card.dataset.stream,seq:Number(seq),at:performance.timeOrigin+performance.now(),
            verdict:card.querySelector('.station-decision')?.dataset.verdict,text:card.textContent});
        }
      }).observe(document.querySelector('#stations'),{childList:true,subtree:true});
    });
    for(let n=1;n<=6;n++){
      if(n>1)await page.locator('#inspection-task').selectOption('station'+n);
      await page.waitForFunction(n=>window.StationDetection?.state.station==='station-'+n&&StationDetection.state.connected&&!StationDetection.state.busy,n);
      assert.equal(await page.locator('#start-form').isVisible(),false);
      assert.equal(await page.locator('#sd-start').isVisible(),true);
      const gate=await page.evaluate(()=>InspectionTrigger.requestStart('test'));
      assert.equal(gate.accepted,false);assert.match(gate.reason,/模型/);
      await page.locator('#sd-config-open').click();
      await page.locator('#sd-model').selectOption('screw');
      await page.locator('#sd-setup button[type=submit]').click();
      await page.waitForFunction(()=>StationDetection.state.binding?.analysisType==='screw'&&!StationDetection.state.busy);
      await page.locator('#sd-config-close').click();
      await page.locator('#sd-source').selectOption('image');
      await page.locator('#sd-file').setInputFiles(path.join(output,'holes.jpg'));
      await page.waitForFunction(()=>StationDetection.state.ready);
      await page.waitForTimeout(650); // Separate intentional starts from the keyboard debounce window.
      await page.keyboard.press('F8');
      await page.waitForFunction(()=>StationDetection.state.frames>=2,null,{timeout:60000});
      const id=await page.evaluate(()=>StationDetection.state.stream.streamId);
      await page.keyboard.press('F8');
      assert.equal(await page.evaluate(()=>StationDetection.state.stream.streamId),id);
      assert.equal(await page.locator('#inspection-task').isDisabled(),true);
      assert.equal(await page.evaluate(()=>StationDetection.state.frame.stationId),'station-'+n);
      assert.notEqual(await page.locator('#metric-fps').textContent(),'—');
      const live=await boardState(n,id,false,n);
      assert.equal(live.stations[n-1].inputSource,'image');
      assert.equal(live.stations[n-1].online,true);
      assert.match(await stationCard(n).textContent(),/图片联调/);
      assert.equal(await stationCard(n).locator('h3 a').getAttribute('href'),'/?task=station'+n);
      await page.locator('#sd-finish').click();
      await page.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream&&!StationDetection.state.busy);
      const history=await(await context.request.get(base+'/api/v1/stations/station-'+n+'/inspections')).json();
      assert.equal(history.items.length,1);assert.equal(history.items[0].status,'FINISHED');assert.equal(history.items[0].context.inputSource,'image');
      assert.equal((await(await context.request.get(base+'/api/v1/pilot/cycles?station=station-'+n)).json()).total,0);
      const archived=await boardState(n,id,true,n);
      assert.equal(archived.stations[n-1].online,false);
      assert.equal(archived.stations[n-1].detection.status,history.items[0].result.compliance.status);
      console.log('Station '+n+' F8/image/archive passed');
    }
    // Camera can be opened by F8 itself, without an additional manual registration step.
    await page.locator('#sd-source').selectOption('camera');
    await page.waitForTimeout(650);
    await page.keyboard.press('F8');
    await page.waitForFunction(()=>StationDetection.state.running&&StationDetection.state.frames>=2,null,{timeout:60000});
    const cameraId=await page.evaluate(()=>StationDetection.state.stream.streamId);
    await boardState(6,cameraId,false,7);
    await page.locator('#sd-stop').click();
    await page.waitForFunction(()=>!StationDetection.state.stream&&!StationDetection.state.busy);
    const interrupted=await boardState(6,cameraId,true,7);
    assert.equal(interrupted.stations[5].detection.status,'REVIEW');
    assert.equal(interrupted.stations[5].detection.recordStatus,'INTERRUPTED');
    await page.reload();
    await page.waitForFunction(()=>StationDetection.state.connected&&StationDetection.state.binding?.analysisType==='screw');
    assert.equal(await page.locator('#sd-setup').isVisible(),false);
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'station-detection-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('#inspection-task').selectOption('station1');
    await page.waitForFunction(()=>StationDetection.state.station==='station-1'&&StationDetection.state.connected&&!StationDetection.state.busy);
    await page.locator('#sd-source').selectOption('replay');
    const video=process.env.STATION_DASHBOARD_VIDEO||'C:/Users/Administrator/Downloads/lv_0_20261017161554.mp4';
    await page.locator('#sd-file').setInputFiles(video);
    await page.waitForFunction(()=>StationDetection.state.ready);
    await page.evaluate(()=>{
      window.detectionFrames=[];const seen=new Set();
      new MutationObserver(()=>{
        const s=StationDetection.state,r=s.frame,key=r?.streamId+':'+r?.frameSeq;
        if(!r||seen.has(key))return;seen.add(key);
        detectionFrames.push({streamId:r.streamId,seq:r.frameSeq,latency:s.latency,
          sampleAt:performance.timeOrigin+s.lastAt-s.latency,renderedAt:performance.timeOrigin+s.lastAt,
          status:r.compliance?.status,alerts:document.querySelector('#metric-alerts').textContent,...s.timing});
      }).observe(document.querySelector('#sd-progress'),{childList:true,subtree:true,characterData:true});
    });
    await page.keyboard.press('F8');
    await page.waitForFunction(()=>StationDetection.state.running&&StationDetection.state.frames>=5,null,{timeout:60000});
    assert.equal(new URL(page.url()).searchParams.get('task'),'station1');
    await page.screenshot({path:path.join(output,'station-1-detecting.png'),fullPage:true});
    const videoId=await page.evaluate(()=>StationDetection.state.stream.streamId);
    await boardState(1,videoId,false,8);
    await board.screenshot({path:path.join(output,'board-video-detecting.png'),fullPage:true});
    await page.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream,null,{timeout:120000});
    const result=await page.evaluate(()=>({frames:StationDetection.state.frames,final:StationDetection.state.final,latency:StationDetection.state.latency}));
    fs.writeFileSync(path.join(output,'video-checkpoint.json'),JSON.stringify({result,frames:await page.evaluate(()=>window.detectionFrames)},null,2));
    const firstPass=(await page.evaluate(()=>window.detectionFrames)).find(row=>row.status==='PASS');
    if(firstPass)assert.equal(result.final.frameSeq,firstPass.seq,'A server PASS must end sampling immediately');
    else assert.ok(result.frames>250,'An incomplete video with Board open must maintain at least 250 samples');
    assert.ok(result.final.archived);assert.equal(result.final.stationId,'station-1');
    assert.equal(await page.locator('#start-form').isVisible(),false);
    await page.screenshot({path:path.join(output,'station-1-video-finished.png'),fullPage:true});
    if(result.final.compliance.status==='PASS'){
      assert.equal(await page.locator('#modal-title').textContent(),'工位1 · 合规');
      await page.locator('#modal [data-action=close-modal]').first().click();
    }
    const videoBoard=await boardState(1,videoId,true,8);
    assert.equal(videoBoard.stations[0].detection.status,result.final.compliance.status);
    assert.equal(videoBoard.stations[0].inputSource,'replay');
    const expected=result.final.sequence.operationsExpected+(result.final.sequence.preassembledExpected||0);
    const confirmed=result.final.sequence.operationsCompleted+(result.final.sequence.preassembledConfirmed||0);
    assert.equal(videoBoard.stations[0].detection.expected,expected);
    assert.equal(videoBoard.stations[0].detection.confirmed,confirmed);
    assert.match(await stationCard(1).textContent(),/视频联调/);
    await board.screenshot({path:path.join(output,'board-video-finished.png'),fullPage:true});
    const detectionFrames=await page.evaluate(()=>window.detectionFrames);
    const measuredBoardFrames=await board.evaluate(id=>window.boardFrames.filter(row=>row.streamId===id),videoId);
    const bySeq=new Map(detectionFrames.map(row=>[row.seq,row]));
    const boardDelays=measuredBoardFrames.filter(row=>bySeq.has(row.seq)).map(row=>({...row,
      captureToBoardMs:row.at-bySeq.get(row.seq).sampleAt,detectorToBoardMs:row.at-bySeq.get(row.seq).renderedAt}));
    assert.ok(boardDelays.length>(firstPass?0:20),'Measure actual rendered Board updates before the round ends');
    for(const row of boardDelays)assert.equal(row.verdict,bySeq.get(row.seq).status,'Board and detector agree for the same stream/frame');
    assert.ok(detectionFrames.filter(row=>row.status==='FAIL').every(row=>Number(row.alerts)>0),'Real FAIL results appear in the dashboard alert area');
    const percentile=(rows,p)=>[...rows].sort((a,b)=>a-b)[Math.min(rows.length-1,Math.ceil(rows.length*p)-1)];
    const timing={samples:detectionFrames.length,boardUpdates:boardDelays.length,
      captureToResultP95Ms:percentile(detectionFrames.map(row=>row.latency),.95),
      captureToBoardP95Ms:percentile(boardDelays.map(row=>row.captureToBoardMs),.95),
      detectorToBoardP95Ms:percentile(boardDelays.map(row=>row.detectorToBoardMs),.95),
      captureToBoardMaxMs:Math.max(...boardDelays.map(row=>row.captureToBoardMs))};
    assert.ok(timing.captureToResultP95Ms<500,'Board rendering must not starve frame encoding');
    await page.locator('#sd-history').click();await page.locator('#modal[open]').waitFor();
    assert.match(await page.locator('#modal-body').textContent(),/已归档/);
    const record=page.locator(`.sd-record[data-stream="${videoId}"]`);
    await record.locator('summary').click();
    const downloadPromise=page.waitForEvent('download');await record.locator('[data-sd-download]').click();
    const download=await downloadPromise;const exported=path.join(output,'history-video-export.json');await download.saveAs(exported);
    const exportedRecord=JSON.parse(fs.readFileSync(exported,'utf8'));
    assert.equal(exportedRecord.streamId,videoId);assert.deepEqual(exportedRecord.result,result.final);
    await page.locator('#modal [data-action=close-modal]').click();
    await board.reload();await board.waitForFunction(()=>document.querySelector('#connection').dataset.state==='live');
    await boardState(1,videoId,true,8);
    // Starting another round clears the previous PASS and increments the total exactly once.
    await page.keyboard.press('F8');
    await page.waitForFunction(()=>StationDetection.state.running&&StationDetection.state.frames>=3);
    const nextId=await page.evaluate(()=>StationDetection.state.stream.streamId);
    assert.notEqual(nextId,videoId);
    const next=await boardState(1,nextId,false,9);
    assert.equal(next.stations[0].detection.archived,false);
    assert.notEqual(next.stations[0].detection.status,'PASS');
    await page.locator('#sd-stop').click();await page.waitForFunction(()=>!StationDetection.state.stream&&!StationDetection.state.busy);
    await boardState(1,nextId,true,9);
    // Six independent browser decoders and six real inference streams, using the supplied video.
    const simultaneous=await Promise.all(Array.from({length:6},async(_,i)=>{
      const tab=await context.newPage();tab.on('pageerror',e=>errors.push('station-'+(i+1)+': '+e.message));
      await tab.goto(base+'/?task=station'+(i+1));
      await tab.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy);
      await tab.locator('#sd-source').selectOption('replay');await tab.locator('#sd-file').setInputFiles(video);
      await tab.waitForFunction(()=>StationDetection.state.ready);return tab;
    }));
    await Promise.all(simultaneous.map(tab=>tab.keyboard.press('F8')));
    await Promise.all(simultaneous.map(tab=>tab.waitForFunction(()=>StationDetection.state.running&&StationDetection.state.frames>=12,null,{timeout:60000})));
    await board.waitForFunction(()=>document.querySelector('#running-count').textContent==='6'&&document.querySelector('#total').textContent==='15',null,{timeout:20000});
    await board.screenshot({path:path.join(output,'board-six-live.png'),fullPage:true});
    const concurrent=await Promise.all(simultaneous.map(tab=>tab.evaluate(()=>{
      const s=StationDetection.state;return {station:s.station,streamId:s.stream.streamId,frames:s.frames,
        seconds:(performance.now()-s.started)/1000,lastLatencyMs:s.latency,frameStation:s.frame.stationId,status:s.frame.compliance.status};
    })));
    for(const row of concurrent)assert.equal(row.frameStation,row.station);
    assert.equal(new Set(concurrent.map(row=>row.streamId)).size,6);
    await Promise.all(simultaneous.map(tab=>tab.locator('#sd-finish').click()));
    await Promise.all(simultaneous.map(tab=>tab.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream&&!StationDetection.state.busy)));
    for(let i=0;i<6;i++){
      const saved=await boardState(i+1,concurrent[i].streamId,true,15);
      assert.equal(saved.stations[i].detection.status,await simultaneous[i].evaluate(()=>StationDetection.state.final.compliance.status));
      assert.equal((await(await context.request.get(base+'/api/v1/pilot/cycles?station=station-'+(i+1))).json()).total,0);
    }
    await board.screenshot({path:path.join(output,'board-six-archived.png'),fullPage:true});
    await Promise.all(simultaneous.map(tab=>tab.close()));
    const allHistory=await Promise.all(Array.from({length:6},async(_,i)=>(await(await context.request.get(base+'/api/v1/stations/station-'+(i+1)+'/inspections')).json()).items));
    assert.equal(allHistory.flat().length,15);
    assert.equal(allHistory.flat().filter(row=>row.status==='INTERRUPTED').length,2);
    console.log('Six concurrent browser videos and Board archive agreement passed');
    await page.locator('#inspection-task').selectOption('pose');
    await page.waitForFunction(()=>ScrewApp.state.task==='pose'&&!ScrewApp.state.busy);
    assert.equal(await page.locator('#sd-start').isVisible(),false);
    assert.deepEqual(manualPosts,[]);assert.deepEqual(errors,[]);
    assert.equal(streams.length,9);
    const report={passed:true,stations:6,manualWorkpiecesCreated:0,streamStarts:streams.length+6,realVideo:video,result,pageErrors:errors,
      timing,concurrent,allHistory,transitions,videoBoard,detectionFrames,boardDelays,
      checks:['F8-same-page-detection','six-station-isolation','repeat-key-guard','real-inference','camera-auto-open','stop','archive','source-audit','video-auto-finish','history-export','reload','mobile','task-switch','live-board','board-archive-agreement','next-round-reset','no-double-count','six-concurrent-browser-videos']};
    fs.writeFileSync(path.join(output,'registration-paused.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({passed:true,stations:6,frames:result.frames,final:result.final.compliance,timing,errors}));
  }catch(error){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
