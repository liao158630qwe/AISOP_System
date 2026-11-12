// Observe the original Board and terminal pages during real station inference.
// No mocked requests, injected verdicts, or demo mode.
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.BOARD_VIDEO_BASE,out=process.env.BOARD_VIDEO_OUTPUT;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const save=(name,data)=>fs.writeFileSync(path.join(out,name),JSON.stringify(data,null,2));
const control=()=>JSON.parse(fs.readFileSync(path.join(out,'control.json'),'utf8'));
async function instrument(page,kind){
 await page.addInitScript(kind=>{
  window.__probe={kind,responses:[],previews:[],errors:[]};
  const native=window.fetch.bind(window);
  const text=selector=>document.querySelector(selector)?.textContent;
  const dom=()=>kind==='board'?{
   at:Date.now()/1000,hidden:document.hidden,connection:document.querySelector('#connection')?.dataset.state,
   total:text('#total'),passed:text('#passed'),failed:text('#failed'),pending:text('#pending'),rate:text('#rate'),running:text('#running-count'),
   stations:[...document.querySelectorAll('#stations .station')].map(n=>({status:n.dataset.status,
    label:n.querySelector('.station-badge')?.textContent,fps:n.querySelector('.station-product small')?.textContent,
    product:n.querySelector('.station-product span')?.textContent,reason:n.querySelector('.station-step')?.getAttribute('title'),
    step:n.querySelector('.station-step')?.textContent,decision:n.querySelector('.station-decision')?.textContent})),alerts:text('#alert-count')
  }:{at:Date.now()/1000,hidden:document.hidden,online:text('#online'),fps:text('#fps'),age:text('#age'),latency:text('#latency'),
   status:document.querySelector('.terminal-result')?.dataset.status,decision:text('#decision'),reason:text('#reason'),identity:text('#identity'),
   sampling:text('#sampling-note'),previewVisible:!document.querySelector('#sample-preview')?.hidden,
   previewWidth:document.querySelector('#sample-preview')?.naturalWidth};
  window.fetch=async(input,options)=>{
   const url=String(input),interesting=url.includes('/board/snapshot')||url.includes('/live-status')||url.includes('/inspections?');
   const preview=url.includes('/preview?');
   if(!interesting&&!preview)return native(input,options);
   const record={url,started:Date.now()/1000};
   try{
    const response=await native(input,options);record.received=Date.now()/1000;record.status=response.status;
    if(preview){record.streamId=response.headers.get('X-Stream-Id');record.frameSeq=response.headers.get('X-Frame-Seq');record.epoch=response.headers.get('X-Stream-Epoch');window.__probe.previews.push(record);}
    else response.clone().json().then(body=>{record.body=body;setTimeout(()=>{record.dom=dom();window.__probe.responses.push(record);},50);}).catch(e=>window.__probe.errors.push(String(e)));
    return response;
   }catch(e){record.error=String(e);window.__probe.errors.push(record);throw e;}
  };
 },kind);
}
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const errors=[],shots=[],workpieceRequests=[];
 try{
  const bc=await browser.newContext({viewport:{width:1920,height:1080},deviceScaleFactor:1});
  const tc=await browser.newContext({viewport:{width:1440,height:1100},deviceScaleFactor:1});
  const board=await bc.newPage(),terminal=await tc.newPage();
  board.on('pageerror',e=>errors.push({page:'board',error:e.message}));
  terminal.on('pageerror',e=>errors.push({page:'terminal',error:e.message}));
  terminal.on('request',r=>{if(/\/api\/v1\/(pilot\/|stations\/[^/]+\/context)/.test(r.url()))workpieceRequests.push(r.url());});
  await instrument(board,'board');await instrument(terminal,'terminal');
  await board.goto(base+'/board');
  await board.locator('#workspace-login').waitFor({state:'visible'});
  await board.locator('#workspace-login [name=username]').fill(process.env.APP_USERNAME);
  await board.locator('#workspace-login [name=password]').fill(process.env.APP_PASSWORD);
  await board.locator('#workspace-login button[type=submit]').click();
  await board.waitForFunction(()=>document.querySelector('#connection').dataset.state==='live');
  await terminal.goto(base+'/terminal');
  await terminal.locator('#credential').fill(process.env.BOARD_VIDEO_KEY_1);
  await terminal.locator('#terminal-login button').click();
  await terminal.locator('#terminal-panel').waitFor({state:'visible'});
  if(await terminal.locator('.terminal-product, #cycle-id, #current-product, #manual-link').count())throw new Error('Default terminal still contains workpiece controls');
  save('browser-ready.json',{ready:true,at:Date.now()/1000});
  const seen=new Set();let previous='';
  const shot=async(name,c)=>{
   const at=Date.now()/1000;
   await board.screenshot({path:path.join(out,name+'-board.png'),fullPage:true});
   await terminal.screenshot({path:path.join(out,name+'-terminal.png'),fullPage:true});
   const data={name,requestedControl:c,at,finished:Date.now()/1000,
    board:await board.evaluate(()=>window.__probe.responses.at(-1)),
    terminal:await terminal.evaluate(()=>window.__probe.responses.filter(r=>r.url.includes('/live-status')).at(-1))};
   shots.push(data);save(name+'-screenshot.json',data);console.log('Saved',name);
  };
  while(true){
   const c=control();
   if(c.phase!==previous){previous=c.phase;console.log('Phase:',previous);}
   if(['single','six'].includes(c.phase))for(const t of [20,40,62,70]){
    const name=`${c.phase}-${t}s`;
    if(c.elapsed>=t&&!seen.has(name)){seen.add(name);await shot(name,c);}
   }
   if(c.phase.endsWith('-finished')&&!seen.has(c.phase)){
    seen.add(c.phase);
    await terminal.locator('#refresh-history').click();
    await terminal.waitForFunction(()=>document.querySelector('#inspection-history summary')?.textContent.includes('已归档'));
    await terminal.locator('#inspection-history details').first().locator('summary').click();
    await sleep(3300);
    await shot(c.phase,c);
   }
   if(c.phase==='done')break;
   await sleep(150);
  }
  const bp=await board.evaluate(()=>window.__probe),tp=await terminal.evaluate(()=>window.__probe);
  save('board-browser-raw.json',bp);save('terminal-browser-raw.json',tp);save('screenshots.json',shots);
  const mismatches=[],labels={RUNNING:'检测运行',ALARM:'违规告警',REVIEW:'待复核',MANUAL:'人工检查',OFFLINE:'离线',UNKNOWN:'待连接'};
  const snapshots=bp.responses.filter(r=>r.status===200&&r.body?.stations&&r.dom?.stations?.length===6);
  const originalFrames=new Map();
  for(const name of fs.readdirSync(out).filter(n=>/-station-\d-frames\.json$/.test(n)))
   for(const f of JSON.parse(fs.readFileSync(path.join(out,name),'utf8')))
    if(f.status===200)originalFrames.set(f.response.streamId+':'+f.response.frameSeq,f.response);
  for(const r of snapshots){
   const {body:b,dom:d}=r;
   if(d.hidden||d.connection!=='live')mismatches.push({kind:'board visibility',at:d.at});
   for(const key of ['total','passed','failed'])if(Number(d[key].replaceAll(',',''))!==b.summary[key])mismatches.push({kind:'summary '+key,at:d.at});
   if(Number(d.running)!==b.stations.filter(s=>s.online ?? ['RUNNING','MANUAL'].includes(s.status)).length)mismatches.push({kind:'running count',at:d.at});
   b.stations.forEach((s,i)=>{
    const displayed=d.stations[i],fps=s.status==='MANUAL'?'人工确认':(s.online ?? ['RUNNING','ALARM'].includes(s.status))?Number(s.effectiveFps||0).toFixed(1)+' fps':'—';
    const label=s.detection&&s.online?'采样在线':labels[s.status];
    if(displayed.status!==s.status||displayed.label!==label||displayed.fps!==fps||displayed.reason!==(s.reason||s.stage))mismatches.push({kind:'station',station:s.id,at:d.at,expected:s,actual:displayed});
    if(s.detection){
     const v=s.detection,progress=v.expected>0&&v.confirmed!=null?` · ${v.confirmed}/${v.expected} 已确认`:'';
     if(displayed.decision!==v.label+progress)mismatches.push({kind:'detection progress',station:s.id,at:d.at});
     if(v.frameSeq!=null){
      const source=originalFrames.get(s.streamId+':'+v.frameSeq),seq=source?.sequence;
      if(!source)mismatches.push({kind:'missing original inference',station:s.id,frame:v.frameSeq});
      else if(seq?.operationsExpected!=null&&v.confirmed!==(seq.operationsCompleted||0)+(seq.preassembledConfirmed||0))
       mismatches.push({kind:'installed progress differs from inference',station:s.id,frame:v.frameSeq});
     }
    }
   });
  }
  const terminalResponses=tp.responses.filter(r=>r.url.includes('/live-status')&&r.status===200);
  let comparedTerminal=0;
  for(const r of terminalResponses){
   const b=r.body,d=r.dom;
   // Leave enough margin for the page's stricter 1-second result expiry.
   if(b.state==='LIVE'&&b.result&&b.resultAgeMs+(d.at-r.started)*1000<900){
    comparedTerminal++;
    if(d.status!==b.result.compliance.status||d.decision!==b.result.compliance.label||d.reason!==b.result.compliance.reason)mismatches.push({kind:'terminal verdict',at:d.at,expected:b.result.compliance,actual:d});
    if(!d.identity.includes('帧 '+b.result.frameSeq))mismatches.push({kind:'terminal identity',at:d.at});
   }
  }
  const result={mode:'real API; original pages; no fixture responses or demo mode',
   boardSnapshots:snapshots.length,comparedStationCards:snapshots.length*6,
   terminalPolls:terminalResponses.length,comparedTerminal,previewResponses:tp.previews.length,
   previewErrors:tp.previews.filter(r=>r.status!==200),pageErrors:errors,fetchErrors:[...bp.errors,...tp.errors],mismatches,workpieceRequests,
   firstTerminalPass:terminalResponses.find(r=>r.dom.status==='PASS')?.dom.at,
   finalHistory:await terminal.locator('#inspection-history').textContent(),
   finalBoard:await board.evaluate(()=>window.__probe.responses.at(-1)?.body)};
  save('browser-verification.json',result);
  const final=result.finalBoard;
  if(final?.mode==='detection'){
   const completedRuns=['single','six'].filter(n=>fs.existsSync(path.join(out,n+'.json'))).map(n=>JSON.parse(fs.readFileSync(path.join(out,n+'.json'),'utf8')));
   const verdicts=completedRuns.flatMap(r=>r.stations.map(s=>s.final.compliance.status));
   const expected=verdicts.length;
   if(final.summary.total!==expected||final.summary.passed+final.summary.failed+final.summary.pending!==expected)
    mismatches.push({kind:'archived round totals',expected,actual:final.summary});
   for(const [field,status] of [['passed','PASS'],['failed','FAIL'],['pending','REVIEW']])
    if(final.summary[field]!==verdicts.filter(v=>v===status).length)mismatches.push({kind:'archived verdict count',field});
   save('browser-verification.json',result);
  }
  console.log(JSON.stringify({boardSnapshots:result.boardSnapshots,comparedTerminal,mismatches:mismatches.length,pageErrors:errors.length,previewErrors:result.previewErrors.length}));
  if(errors.length||mismatches.length||workpieceRequests.length)throw new Error('Browser verification failed; see browser-verification.json');
 }finally{await browser.close();}
})().catch(e=>{save('browser-error.json',{error:String(e),stack:e.stack});console.error(e);process.exitCode=1;});
