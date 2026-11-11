// Browser + production API/SQL + collector loop. Camera and model alone are fixtures.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if(process.env.APP_ENV!=='test'||!base||!output||!path.resolve(output).startsWith(path.resolve('test-results/controlled-collection-e2e')+path.sep))
  throw new Error('Use test-controlled-collection-ui.py and its disposable API');
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const report={passed:false,realAPI:true,realSQL:true,realCollectorStateMachine:true,cameraMocked:true,inferenceMocked:true,checks:[]};
  const errors=[],posts=[];let deletes=0;
  context.on('request',request=>{
    if(request.method()==='DELETE'&&request.url().includes('/station-streams/'))deletes++;
    if(request.method()==='POST'&&request.url().endsWith('/collector/commands'))posts.push(request.postDataJSON());
  });
  try{
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(login.status(),200);
    let page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
    const ready=async()=>{
      await page.goto(base+'/?task=station1');
      await page.waitForFunction(()=>window.StationDetection?.state.source==='device'&&StationDetection.state.collector?.online);
    };
    await ready();
    assert.equal(await page.locator('#sd-source').inputValue(),'device');
    const get=async suffix=>(await context.request.get(base+'/api/v1/'+suffix)).json();
    assert.equal((await get('stations/station-1/inspections')).items.length,0);
    // Lose the HTTP response after the real command has been committed.
    let lost=false;
    await page.route('**/api/v1/stations/station-1/collector/commands',async route=>{
      const response=await route.fetch();assert.equal(response.status(),200);
      if(!lost){lost=true;await route.abort('failed');}else await route.fulfill({response});
    });
    await page.locator('#sd-start').click();
    await page.waitForFunction(()=>StationDetection.state.stream&&StationDetection.state.frames>=2,null,{timeout:20000});
    const first=await page.evaluate(()=>({id:StationDetection.state.stream.streamId,seq:StationDetection.state.frame.frameSeq}));
    assert.equal(posts.length,1,'Lost response is queried, never reposted');
    await page.reload();
    await page.waitForFunction(({id,seq})=>StationDetection.state.stream?.streamId===id&&StationDetection.state.frame?.frameSeq>seq,first,{timeout:15000});
    assert.equal(deletes,0,'Refresh must not release the independent producer');
    assert.equal((await get('stations/station-1/inspections')).items.length,1);
    report.checks.push('real-command-response-loss','refresh-keeps-same-stream-and-new-frames');
    // Closing the only page must still leave the independent collector running.
    const beforeClose=(await get('stations/station-1/live-status')).result.frameSeq;
    await page.close();
    await new Promise(resolve=>setTimeout(resolve,1000));
    const afterClose=await get('stations/station-1/live-status');
    assert.equal(afterClose.streamId,first.id);assert.ok(afterClose.result.frameSeq>beforeClose);
    page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
    await ready();
    await page.waitForFunction(()=>StationDetection.state.frames>0);
    report.checks.push('closed-page-keeps-collector-running');
    await page.locator('#sd-finish').click();
    await page.waitForFunction(()=>StationDetection.state.archived&&!StationDetection.state.stream,null,{timeout:15000});
    const completed=await get('stations/station-1/collector');
    assert.equal(completed.command.status,'SUCCEEDED');assert.equal(completed.record.status,'FINISHED');
    assert.equal(completed.record.streamId,first.id);assert.ok(completed.record.result.archived);
    assert.equal(deletes,0,'Only collector releases a controlled stream');
    await page.screenshot({path:path.join(output,'archived.png'),fullPage:true});
    const close=page.locator('#modal [data-action="close-modal"]').first();
    if(await close.isVisible())await close.click();
    await page.locator('#sd-start').click();
    await page.waitForFunction(id=>StationDetection.state.stream&&StationDetection.state.stream.streamId!==id&&StationDetection.state.frames>0,first.id,{timeout:15000});
    const second=await page.evaluate(()=>StationDetection.state.stream.streamId);
    await page.locator('#sd-stop').click();
    await page.waitForFunction(()=>!StationDetection.state.stream&&StationDetection.state.collector?.record?.status==='INTERRUPTED',null,{timeout:15000});
    const records=(await get('stations/station-1/inspections')).items;
    assert.equal(records.length,2);
    assert.equal(records.find(row=>row.streamId===second).status,'INTERRUPTED');
    assert.equal(records.find(row=>row.streamId===first.id).status,'FINISHED');
    assert.equal(deletes,0);assert.deepEqual(errors,[]);
    report.checks.push('real-finish-archive-release-ack','new-piece-new-stream','explicit-stop-interrupted');
    report.passed=true;report.commandRequests=posts.length;report.pageDeletes=deletes;
    console.log('Real controlled collection UI passed:',report.checks.join(', '));
  }catch(error){report.error=error.stack;throw error;}
  finally{
    report.errors=errors;fs.writeFileSync(path.join(output,'controlled-collection-ui.json'),JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
