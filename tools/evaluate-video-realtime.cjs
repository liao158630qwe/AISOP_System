// Original frontend + real HTTP inference. Only the camera input is replaced by the user's file.
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.VIDEO_EVAL_BASE,out=process.env.VIDEO_EVAL_OUTPUT;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function stats(values){const v=values.filter(Number.isFinite).sort((a,b)=>a-b);return v.length?{samples:v.length,mean:v.reduce((a,b)=>a+b,0)/v.length,p50:v[Math.floor(v.length*.5)],p95:v[Math.ceil(v.length*.95)-1],p99:v[Math.ceil(v.length*.99)-1],max:v.at(-1)}:{samples:0};}
const save=(name,value)=>fs.writeFileSync(path.join(out,name),JSON.stringify(value,null,2));
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true,args:['--autoplay-policy=no-user-gesture-required']});
 const results=[];
 try{
  for(const rate of [4,10]){
   const context=await browser.newContext({viewport:{width:1600,height:1120}}),page=await context.newPage(),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(()=>{
    window.__probe={requests:[],panels:[],finishes:[],errors:[]};
    const nativeFetch=window.fetch.bind(window);
    window.fetch=async(input,options)=>{
     const url=String(input),isFrame=url.includes('/realtime/frame?'),isFinish=url.includes('/finish?');
     if(!isFrame&&!isFinish)return nativeFetch(input,options);
     const started=performance.now(),s=window.ScrewApp?.state;
     const record={started,preparing:!!s?.preparing,sourceTime:window.__fixture?.lastDrawnTime,
      frameTime:Number(new Headers(options?.headers).get('X-Frame-Time')),
      streamId:new Headers(options?.headers).get('X-Stream-Id')};
     try{
      const response=await nativeFetch(input,options);
      record.received=performance.now();record.httpMs=record.received-started;record.status=response.status;
      const copy=response.clone();
      copy.json().then(body=>{record.response=body;(isFrame?window.__probe.requests:window.__probe.finishes).push(record);}).catch(e=>window.__probe.errors.push(String(e)));
      return response;
     }catch(e){record.error=String(e);window.__probe.requests.push(record);throw e;}
    };
    navigator.mediaDevices.getUserMedia=async()=>{
     const video=document.createElement('video');video.muted=true;video.playsInline=true;video.preload='auto';
     video.src='/__test/source.mp4';document.body.appendChild(video);video.style.display='none';
     await new Promise((resolve,reject)=>{video.addEventListener('loadeddata',resolve,{once:true});video.addEventListener('error',()=>reject(new Error('Source decode failed')),{once:true});});
     const canvas=document.createElement('canvas');canvas.width=1920;canvas.height=1080;
     const ctx=canvas.getContext('2d'),fixture=window.__fixture={video,canvas,lastDrawnTime:0,started:null,ended:null};
     ctx.drawImage(video,0,0,1920,1080);
     // Continually emit the first source frame during disposable model preparation.
     // Once ready, decode and forward the entire original video at playbackRate=1.
     fixture.timer=setInterval(()=>{if(video.readyState>=2){ctx.drawImage(video,0,0,1920,1080);fixture.lastDrawnTime=video.currentTime;}},1000/30);
     video.addEventListener('ended',async()=>{
      fixture.ended=performance.now();
      // Stop scheduling new samples but let the last actual HTTP request finish.
      const state=window.ScrewApp.state;clearTimeout(state.timer);
      if(state.pending)await state.pending.catch(()=>{});
      clearTimeout(state.timer);
     });
     return canvas.captureStream(30);
    };
    let previous=null;
    setInterval(()=>{
     const s=window.ScrewApp?.state,f=window.__fixture;
     if(!s?.frame||s.preparing||s.frame.time===previous)return;
     previous=s.frame.time;
     const text=id=>document.querySelector(id)?.textContent;
     window.__probe.panels.push({time:s.frame.time,at:performance.now(),sourceTime:f?.lastDrawnTime,
      displayLatencyMs:s.displayLatencyMs,processingMs:s.processing,actualFps:s.actualFps,
      running:s.running,timely:s.timelyDecision?.status||null,verdict:text('#screw-verdict'),
      note:text('#result-note'),metricTargets:text('#metric-targets'),workflow:text('#workflow-status'),
      rows:[...document.querySelectorAll('#detection-results tr')].map(n=>({id:n.dataset.screwTarget,label:n.children[1]?.textContent})),
      steps:[...document.querySelectorAll('#steps [data-screw-target]')].map(n=>({id:n.dataset.screwTarget,label:n.querySelector('.step-status')?.textContent})),
      boxes:s.drawnBoxes,skeletons:s.drawnSkeletons,aggregate:s.aggregateTargets,gaps:s.evidenceGaps});
    },20);
   });
   try{
    await page.goto(base+'/?task=screw');
    await page.locator('#server-account').click();
    await page.locator('#login-user').fill(process.env.APP_USERNAME);
    await page.locator('#login-password').fill(process.env.APP_PASSWORD);
    await page.locator('#screw-login button[type=submit]').click();
    await page.waitForFunction(()=>window.ScrewApp?.state.session&&window.ScrewApp.state.model?.ready);
    await page.locator('#live-rate').selectOption(String(rate));
    await page.locator('#start-live').click();
    await page.waitForFunction(()=>window.ScrewApp?.state.running&&!window.ScrewApp.state.preparing,{},{timeout:90000});
    const preparationMs=await page.evaluate(async()=>{
     const f=window.__fixture;f.started=performance.now();await f.video.play();return window.ScrewApp.state.preparationMs;
    });
    console.log(`Browser ${rate} fps: source playback started`);
    const shots=new Set();let lastLog=-1;
    while(true){
     const state=await page.evaluate(()=>({time:window.__fixture.video.currentTime,ended:!!window.__fixture.ended,
      frames:window.__probe.panels.length,pass:window.ScrewApp.state.timelyDecision?.status}));
     const block=Math.floor(state.time/15);
     if(block!==lastLog){lastLog=block;console.log(`Browser ${rate} fps: ${state.time.toFixed(1)}s, ${state.frames} frames, ${state.pass||'pending'}`);}
     for(const t of [25,45,65])if(state.time>=t&&!shots.has(t)){
      shots.add(t);await page.screenshot({path:path.join(out,`browser-${rate}fps-${t}s.png`),fullPage:true});
     }
     if(state.ended)break;
     await sleep(250);
    }
    await page.evaluate(async()=>{const s=window.ScrewApp.state;if(s.pending)await s.pending.catch(()=>{});});
    await page.locator('#finish-detection').click();
    await page.waitForFunction(()=>window.ScrewApp.state.final&&!window.ScrewApp.state.busy);
    await page.screenshot({path:path.join(out,`browser-${rate}fps-final-modal.png`),fullPage:true});
    await page.locator('#modal [data-action=close-modal]').last().click();
    await page.screenshot({path:path.join(out,`browser-${rate}fps-final.png`),fullPage:true});
    const data=await page.evaluate(()=>{
     clearInterval(window.__fixture.timer);
     const s=window.ScrewApp.state,f=window.__fixture,q=f.video.getVideoPlaybackQuality?.();
     return {...window.__probe,started:f.started,ended:f.ended,duration:f.video.duration,
      playbackQuality:q?{totalVideoFrames:q.totalVideoFrames,droppedVideoFrames:q.droppedVideoFrames,corruptedVideoFrames:q.corruptedVideoFrames}:null,
      final:s.final,finalRows:[...document.querySelectorAll('#detection-results tr')].map(n=>({id:n.dataset.screwTarget,label:n.children[1]?.textContent})),
      finalVerdict:document.querySelector('#screw-verdict').textContent,
      finalBoxes:s.drawnBoxes,evidenceGaps:s.evidenceGaps};
    });
    save(`browser-${rate}fps-raw.json`,data);
    const frames=data.requests.filter(r=>!r.preparing&&r.started>=data.started&&r.response?.frameTime!=null),
     successful=frames.filter(r=>r.status===200),panels=data.panels.filter(p=>p.at>=data.started),
     mapping=new Map(successful.map(r=>[r.response.frameTime,r])),mismatches=[];
    const label=t=>({COMPLETED:'已确认',PREASSEMBLED_OK:'预装件已确认',EMPTY:'当前为空',PREASSEMBLED_MISSING:'预装件当前为空',PENDING:'待确认',PREASSEMBLED_UNKNOWN:'预装件待确认',UNKNOWN:'证据不足'})[t.sequenceState]||'证据不足';
    for(const p of panels){
     const r=mapping.get(p.time);if(!r)continue;
     const targets=r.response.aggregateTargets;
     if(JSON.stringify(p.aggregate)!==JSON.stringify(targets))mismatches.push({time:p.time,kind:'aggregate'});
     for(const kind of ['rows','steps'])if(p[kind].length!==targets.length)mismatches.push({time:p.time,kind:kind+'-count'});
     for(const kind of ['rows','steps'])for(const t of targets){const row=p[kind].find(row=>row.id===t.id);if(!row||row.label!==label(t))mismatches.push({time:p.time,kind,id:t.id,expected:label(t),actual:row?.label});}
     const expected=r.response.inspection.displayTargets||[];
     for(const b of p.boxes.filter(b=>b.className!=='person'))if(!expected.some(t=>t.id===b.id&&JSON.stringify(t.bbox)===JSON.stringify(b.bbox)))mismatches.push({time:p.time,kind:'stale-or-wrong-box',id:b.id});
     if(p.timely==='PASS'&&r.response.compliance.status!=='PASS')mismatches.push({time:p.time,kind:'verdict'});
     if(r.response.compliance.status==='PASS'&&p.displayLatencyMs<=750&&!p.gaps.length&&p.timely!=='PASS')mismatches.push({time:p.time,kind:'missing-pass'});
    }
    const finish=data.finishes.at(-1)?.response;
    if(JSON.stringify(finish?.targets)!==JSON.stringify(data.final.targets)||finish?.compliance.status!==data.final.compliance.status)mismatches.push({kind:'final'});
    const firstPass=successful.find(r=>r.response.compliance.status==='PASS'),firstUiPass=panels.find(p=>p.timely==='PASS');
    const result={requestedFps:rate,duration:data.duration,playbackWallSeconds:(data.ended-data.started)/1000,
     preparationMs,completedFrames:successful.length,
     completedWithinVideo:successful.filter(r=>r.received<=data.ended).length,
     sustainedFps:successful.filter(r=>r.received<=data.ended).length/((data.ended-data.started)/1000),
     displayedFrames:panels.length,comparedFrames:panels.filter(p=>mapping.has(p.time)).length,
     httpMs:stats(successful.map(r=>r.httpMs)),captureToDisplayMs:stats(panels.map(p=>p.displayLatencyMs)),
     serverMs:stats(successful.map(r=>r.response.serverProcessingMs)),
     captureGapMs:stats(successful.slice(1).map((r,i)=>(r.frameTime-successful[i].frameTime)*1000)),
     firstPassSourceTime:firstPass?.sourceTime,firstUiPassSourceTime:firstUiPass?.sourceTime,
     liveFailCount:successful.filter(r=>r.response.compliance.status==='FAIL').length,
     registeredFrames:successful.filter(r=>r.response.inspection.registered).length,
     framesWithPeople:successful.filter(r=>r.response.people?.length).length,
     framesWithScrewBoxes:panels.filter(p=>p.boxes.some(b=>b.className!=='person')).length,
     framesOver750ms:panels.filter(p=>p.displayLatencyMs>750).length,
     errors:[...errors,...data.errors,...data.requests.filter(r=>r.error||r.status!==200)],
     evidenceGaps:data.evidenceGaps,mismatches,final:data.final,finalVerdict:data.finalVerdict,
     finalBoxes:data.finalBoxes,playbackQuality:data.playbackQuality,
     inputDescription:'Original 4K file decoded at 1x; 1920x1080 canvas.captureStream(30) substitutes camera; original frontend sends JPEGs and renders actual API responses. Includes browser canvas forwarding and screenshot overhead.'};
    save(`browser-${rate}fps.json`,result);results.push(result);save('browser.json',results);
    console.log(`Browser result ${rate}fps: ${result.sustainedFps.toFixed(3)} fps, ${result.final.compliance.status}, ${mismatches.length} mismatches, ${result.errors.length} errors`);
    if(mismatches.length||result.errors.length)throw new Error(`Browser ${rate}fps comparison failed; see saved results`);
   }catch(error){await page.screenshot({path:path.join(out,`browser-${rate}fps-error.png`),fullPage:true}).catch(()=>{});save(`browser-${rate}fps-error.json`,{error:String(error),errors,state:await page.evaluate(()=>({status:document.querySelector('#server-status')?.textContent,preparing:window.ScrewApp?.state.preparing,running:window.ScrewApp?.state.running,probe:window.__probe})).catch(()=>null)});throw error;}
   finally{await context.close();}
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
