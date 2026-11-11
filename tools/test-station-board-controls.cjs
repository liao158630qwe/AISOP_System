// Short real-inference regression for reports, system details and Board card layout.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output)throw new Error('Use test-pilot-ui.py --registration-paused --browser-script tools/test-station-board-controls.cjs');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),errors=[];
  const session=await(await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}})).json();
  const pages=[];
  for(let n=1;n<=6;n++){
   const response=await context.request.put(base+'/api/v1/admin/terminals/station-'+n,{headers:{'X-CSRF-Token':session.csrf},data:{
    workstationId:'flow-pc-'+n,cameraId:'flow-camera-'+n,analysisType:'screw',sampleFps:4,maxSampleGapSeconds:1,source:'usb',enabled:true,revision:0}});
   assert.equal(response.status(),200);
   const p=await context.newPage();p.on('pageerror',e=>errors.push(e.message));pages.push(p);
   await p.goto(base+'/?task=station'+n);await p.waitForFunction(()=>StationDetection.state.connected&&!StationDetection.state.busy);
   await p.locator('#sd-source').selectOption('replay');await p.locator('#sd-file').setInputFiles(process.env.STATION_DASHBOARD_VIDEO||'C:/Users/Administrator/Downloads/lv_0_20261017161554.mp4');
   await p.waitForFunction(()=>StationDetection.state.ready);
  }
  const board=await context.newPage();board.on('pageerror',e=>errors.push(e.message));await board.goto(base+'/board');
  await board.waitForFunction(()=>document.querySelector('#connection').dataset.state==='live');
  await Promise.all(pages.map(p=>p.keyboard.press('F8')));
  await Promise.all(pages.map(p=>p.waitForFunction(()=>StationDetection.state.frames>=12,null,{timeout:60000})));
  await board.waitForFunction(()=>document.querySelector('#running-count').textContent==='6',null,{timeout:20000});
  assert.equal(await board.locator('#total').textContent(),'6');
  assert.ok(await board.evaluate(()=>[...document.querySelectorAll('.station-title,.station-step')].every(n=>n.clientHeight>=parseFloat(getComputedStyle(n).fontSize))));
  assert.ok(await board.evaluate(()=>[...document.querySelectorAll('.station')].every(n=>n.scrollHeight<=n.clientHeight+1)));
  await board.screenshot({path:path.join(output,'board-six-live.png'),fullPage:true});
  const live=await(await context.request.get(base+'/api/v1/board/snapshot?mode=detection')).json();
  await Promise.all(pages.map(p=>p.locator('#sd-finish').click()));
  await Promise.all(pages.map(p=>p.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream&&!StationDetection.state.busy)));
  await board.waitForFunction(()=>document.querySelector('#running-count').textContent==='0');
  const p=pages[0],final=await p.evaluate(()=>StationDetection.state.final);
  await p.locator('[data-action=report]').click();await p.locator('#modal[open]').waitFor();
  assert.match(await p.locator('#modal-title').textContent(),/检测报告/);
  assert.ok((await p.locator('#modal-body').textContent()).includes(final.streamId));
  await p.screenshot({path:path.join(output,'detection-report.png'),fullPage:true});
  const wait=p.waitForEvent('download');await p.locator('#sd-report-download').click();const download=await wait;
  const exported=path.join(output,'report-export.json');await download.saveAs(exported);
  assert.deepEqual(JSON.parse(fs.readFileSync(exported,'utf8')).result,final);
  await p.locator('#modal [data-action=close-modal]').click();
  await p.locator('.toolbar [data-action=system]').click();await p.locator('#modal[open]').waitFor();
  assert.match(await p.locator('#modal-title').textContent(),/运行详情/);assert.match(await p.locator('#modal-body').textContent(),/数据库：就绪/);
  assert.match(await p.locator('#modal-body').textContent(),/离线分析服务：不可用/);
  await p.screenshot({path:path.join(output,'system-details.png'),fullPage:true});
  await p.locator('#modal [data-action=close-modal]').click();
  await p.locator('#sd-history').click();await p.locator('#modal[open]').waitFor();
  assert.equal(await p.locator('.sd-record').count(),1);assert.equal(await p.locator('#sd-history-next').isDisabled(),true);
  await p.locator('.sd-record summary').click();await p.screenshot({path:path.join(output,'history.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(output,'controls.json'),JSON.stringify({passed:true,live,final,pageErrors:errors},null,2));
  console.log('Six real video streams: card layout, report/export, system details and history passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
