// Real history routes and SQL in a disposable test-pilot-ui.py database.
// Never run this browser probe against the developer's local data.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const {randomUUID}=require('node:crypto'), {spawnSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE||process.env.HOME,
  '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE, output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output||!path.resolve(output).startsWith(path.resolve('test-results/pilot-browser-tests')+path.sep))
  throw new Error('Run only through isolated tools/test-pilot-ui.py');
const today=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
const start=Date.parse(today+'T00:00:00+08:00')/1000, stamp=Date.now()/1000;
const yesterday=new Date((start-86400)*1000+8*3600000).toISOString().slice(0,10);
const rows=[], ids={};
function row(name,verdict='FAIL',options={}){
  const id=ids[name]=randomUUID(), at=options.finishedAt??(stamp-Math.min(600,(stamp-start)/2)-rows.length*.01);
  const result={analysisType:'screw',compliance:{status:verdict,label:{FAIL:'螺丝不合规',PASS:'检测合规',REVIEW:'待复核'}[verdict],
    reason:options.reason||name},sequence:{positionsConfirmed:9,positionsExpected:11},
    targets:[{id:'S01',name:'上方螺丝',state:verdict==='PASS'?'INSTALLED':'EMPTY'}]};
  if(options.manual)result.manualReview={source:'manual',status:'PASS',label:'人工确认合规',reason:'现场确认十一颗螺丝完整',
    reviewedAt:at,reviewer:process.env.APP_USERNAME,account:process.env.APP_USERNAME};
  rows.push({stream_id:id,station_id:options.station||'station-1',owner:options.owner||process.env.APP_USERNAME,
    context:{analysisType:'screw',cameraId:'history-camera',inputSource:'replay'},status:options.status||'FINISHED',
    result,reason:'归档测试',started_at:options.startedAt??at-60,finished_at:options.status==='RUNNING'?null:at});
  return id;
}
row('missing-screws','FAIL',{reason:'缺少 S10、S11 两颗螺丝'});
row('unsafe-text','FAIL',{reason:'<img src=x onerror="window.historyInjected=true">不合规内容按文本显示'});
for(let i=0;i<20;i++)row('failed-'+i);
row('cross-midnight','FAIL',{station:'station-2',startedAt:start-60,finishedAt:start+.01,reason:'跨日归档缺少螺丝'});
row('manual-pass','FAIL',{manual:true,station:'station-2'});
row('algorithm-pass','PASS');
row('needs-review','REVIEW');
row('interrupted','FAIL',{status:'INTERRUPTED'});
row('previous-day-fail','FAIL',{finishedAt:start-2,startedAt:start-62});
row('previous-day-pass','PASS',{finishedAt:start-3,startedAt:start-63});
row('still-running','FAIL',{status:'RUNNING',station:'station-6'});
row('private-record','FAIL',{owner:'another-owner'});
const seeded=spawnSync(resolvePython(),['-c',
  'import json,sqlite3,sys,time\np=json.loads(sys.stdin.buffer.read().decode("utf-8"))\nwith sqlite3.connect(p["db"]) as c:\n for r in p["rows"]:\n  c.execute("INSERT INTO station_inspections (stream_id,station_id,owner,context,status,result,reason,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",(r["stream_id"],r["station_id"],r["owner"],json.dumps(r["context"]),r["status"],json.dumps(r["result"]),r["reason"],r["started_at"],r["finished_at"]))\n c.execute("INSERT INTO station_stream_leases (station_id,stream_id,epoch,runtime_id,request_id,closed,context,created_at,lease_until) VALUES (?,?,?,?,?,?,?,?,?)",("station-6",p["running"],1,"fixture-other-runtime",p["running"],0,"{}",time.time(),time.time()+3600))\n'],
  {input:JSON.stringify({db:path.join(output,'test.db'),rows,running:ids['still-running']}),encoding:'utf8',windowsHide:true});
assert.equal(seeded.status,0,seeded.stderr);

(async()=>{
  const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1920,height:1080},timezoneId:'America/Los_Angeles'});
  const page=await context.newPage(), errors=[], mutations=[];
  page.setDefaultTimeout(15000); page.on('pageerror',error=>errors.push(error.message));
  let failList=false, failDelete=false, failRestore=false, listCalls=0, holdList=null, holdDetail=null;
  const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
  const delayed=()=>({seen:deferred(),release:deferred(),done:deferred()});
  async function deliverLater(route,hold){
    const response=await route.fetch();hold.seen.resolve();await hold.release.promise;
    try{await route.fulfill({response});}catch(error){
      // Closing/changing a view deliberately aborts this original request.
      if(!/closed|cancel|abort|Invalid InterceptionId|already handled/i.test(error.message))throw error;
    }finally{hold.done.resolve();}
  }
  await page.route('**/api/v1/board/history**',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(request.method()==='GET'&&url.pathname==='/api/v1/board/history'){
      listCalls++;
      if(failList){failList=false;return route.fulfill({status:503,json:{detail:'测试历史读取失败，请重试'}});}
      if(holdList){const hold=holdList;holdList=null;return deliverLater(route,hold);}
    }
    if(request.method()==='GET'&&url.pathname.startsWith('/api/v1/board/history/')&&holdDetail){
      const hold=holdDetail;holdDetail=null;return deliverLater(route,hold);
    }
    if(['DELETE','POST'].includes(request.method())){
      mutations.push({method:request.method(),path:url.pathname});
      if(failDelete&&request.method()==='DELETE'){
        failDelete=false;return route.fulfill({status:503,json:{detail:'测试删除未保存，请重试'}});
      }
      if(failRestore&&request.method()==='POST'){
        failRestore=false;return route.fulfill({status:503,json:{detail:'测试恢复未保存，请重试'}});
      }
    }
    await route.continue();
  });
  const api=async suffix=>{const r=await context.request.get(base+'/api/v1/board/history'+suffix);assert.equal(r.status(),200,await r.text());return r.json();};
  const record=id=>page.locator(`[data-history-id="${id}"]`);
  const summary=async(expected)=>{
    for(const [key,value] of Object.entries(expected))
      await page.waitForFunction(({key,value})=>document.querySelector(`[data-history-count="${key}"]`)?.textContent.trim()===String(value),{key,value});
  };
  const choose=async verdict=>{
    await page.locator(`[data-history-verdict="${verdict}"]`).click();
    await page.waitForFunction(v=>document.querySelector(`[data-history-verdict="${v}"]`)?.getAttribute('aria-pressed')==='true',verdict);
  };
  const listSettled=async()=>{
    await page.waitForFunction(()=>!document.querySelector('#history-list')?.textContent.includes('正在加载'));
  };
  const screenshot=async name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
  const noOverflow=async()=>{
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'page fits viewport');
    for(const selector of ['#history-dialog[open]','#history-detail-dialog[open]','#history-confirm-dialog[open]']){
      const dialog=page.locator(selector);if(await dialog.count())
        assert.ok(await dialog.evaluate(el=>el.scrollWidth<=el.clientWidth+1),selector+' has no horizontal overflow');
    }
  };
  try{
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(login.status(),200);const session=await login.json();
    const first=await api('?date='+today);
    assert.equal(first.summary.total,27);assert.equal(first.summary.failed,23);
    assert.equal(first.summary.passed,2);assert.equal(first.summary.pending,2);
    assert.equal(first.total,23);assert.equal(first.items.length,20);assert.equal(first.hasMore,true);
    const all=await api('?date='+today+'&verdict=ALL&limit=100');
    assert.equal(all.items.length,27);assert.equal(all.items[0].verdict,'FAIL');assert.equal(all.items.at(-1).verdict,'PASS');
    assert.ok(all.items.some(r=>r.streamId===ids['cross-midnight']));
    assert.ok(!all.items.some(r=>[ids['still-running'],ids['private-record'],ids['previous-day-fail']].includes(r.streamId)));
    const stationPage=await context.newPage();stationPage.on('pageerror',error=>errors.push(error.message));
    try{
      await stationPage.goto(base+'/?task=station1');
      for(let station=1;station<=6;station++){
        if(station>1)await stationPage.locator('#inspection-task').selectOption('station'+station);
        await stationPage.waitForFunction(id=>window.StationDetection?.active&&StationDetection.state.station===id&&!ScrewApp.state.switching,'station-'+station);
        assert.equal(await stationPage.locator('.toolbar [data-action="upload"]').isVisible(),false,'station toolbar has no duplicate source button');
        assert.equal(await stationPage.locator('#sd-source').isVisible(),true);
        await stationPage.locator('#sd-source').selectOption('replay');
        assert.equal(await stationPage.locator('#sd-file').isVisible(),true,'inline source selection remains available');
      }
      await stationPage.locator('#inspection-task').selectOption('screw');
      await stationPage.waitForFunction(()=>!ScrewApp.state.switching&&!StationDetection.active);
      assert.equal(await stationPage.locator('.toolbar [data-action="upload"]').isVisible(),true,'non-station tasks retain upload');
    }finally{await stationPage.close();}
    await page.goto(base+'/board');
    await page.waitForFunction(()=>document.querySelector('#connection').dataset.state==='live');
    await page.locator('#history-open').click();await page.locator('#history-dialog[open]').waitFor();
    await summary({total:27,failed:23,passed:2,pending:2});
    assert.equal(await page.locator('#history-date').inputValue(),today,'history date is UTC+8 despite browser timezone');
    assert.equal(await page.locator('[data-history-verdict="FAIL"]').getAttribute('aria-pressed'),'true');
    await page.waitForFunction(()=>document.querySelectorAll('#history-list [data-history-id]').length===20);
    await noOverflow();await screenshot('history-failures-1920');
    const firstRecord=record(ids['missing-screws']),closedBounds=await firstRecord.boundingBox();
    assert.ok(closedBounds.height<=64,'collapsed records are compact');
    assert.equal(await firstRecord.locator('.history-record-body').isVisible(),false);
    await firstRecord.locator('summary').click();
    assert.equal(await firstRecord.locator('.history-record-body').isVisible(),true);
    assert.match(await firstRecord.locator('.history-record-reason').textContent(),/缺少 S10、S11/);
    assert.ok((await firstRecord.boundingBox()).height>closedBounds.height);
    await screenshot('history-record-expanded');
    await firstRecord.locator('summary').press('Enter');
    assert.equal(await firstRecord.locator('.history-record-body').isVisible(),false,'keyboard collapses a record');
    const dialogBox=await page.locator('#history-dialog').boundingBox();
    await page.mouse.move(dialogBox.x+dialogBox.width-4,dialogBox.y+dialogBox.height-4);
    await page.mouse.down();
    await page.mouse.move(dialogBox.x+dialogBox.width-204,dialogBox.y+dialogBox.height-124,{steps:12});
    await page.mouse.up();
    const resizedBox=await page.locator('#history-dialog').boundingBox();
    assert.ok(resizedBox.width<dialogBox.width-50&&resizedBox.height<dialogBox.height-50,'dragging the corner resizes the history dialog');
    await noOverflow();await screenshot('history-resized');
    assert.equal(await page.locator('#history-list img, #history-list script').count(),0,'unsafe reason is escaped');
    assert.equal(await page.evaluate(()=>!!window.historyInjected),false);
    await page.locator('#history-next-page').click();
    await page.waitForFunction(()=>document.querySelectorAll('#history-list [data-history-id]').length===3);
    await page.locator('#history-prev-page').click();
    await page.waitForFunction(()=>document.querySelectorAll('#history-list [data-history-id]').length===20);
    await choose('PASS');await record(ids['manual-pass']).waitFor();
    assert.equal(await page.locator('#history-list [data-history-id]').count(),2);
    await page.locator(`[data-history-detail="${ids['manual-pass']}"]`).click();
    await page.locator('#history-detail-dialog[open]').waitFor();
    await page.waitForFunction(()=>document.querySelector('#history-detail').textContent.includes('现场确认十一颗螺丝完整'));
    const detail=await page.locator('#history-detail').textContent();
    assert.match(detail,/人工确认合规/);assert.match(detail,/螺丝不合规/);assert.match(detail,/history-camera/);
    await screenshot('history-manual-review');await page.locator('#history-close-detail').click();
    await choose('REVIEW');await record(ids['interrupted']).waitFor();
    assert.equal(await page.locator('#history-list [data-history-id]').count(),2);
    await choose('ALL');await listSettled();
    await page.locator('#history-station').selectOption('station-2');await record(ids['cross-midnight']).waitFor();
    await summary({total:2,failed:1,passed:1,pending:0});
    assert.equal(await page.locator('#history-list [data-history-id]').count(),2);
    await page.locator('#history-station').selectOption('');await summary({total:27});
    const oldList=holdList=delayed();await page.locator('#history-refresh').click();await oldList.seen.promise;
    await page.locator('#history-date').fill(yesterday);await page.locator('#history-date').dispatchEvent('change');
    await summary({total:2,failed:1,passed:1,pending:0});await record(ids['previous-day-fail']).waitFor();
    oldList.release.resolve();await oldList.done.promise;
    await summary({total:2,failed:1,passed:1,pending:0});
    assert.equal(await page.locator('#history-date').inputValue(),yesterday,'slow old-date response does not replace new date');
    assert.equal(await record(ids['cross-midnight']).count(),0,'cross-midnight round only belongs to finish day');
    await page.locator('#history-today').click();await summary({total:27});
    await choose('FAIL');await record(ids['missing-screws']).waitFor();
    const closedDetail=holdDetail=delayed();
    await page.locator(`[data-history-detail="${ids['missing-screws']}"]`).click();await closedDetail.seen.promise;
    await page.locator('#history-close-detail').click();closedDetail.release.resolve();await closedDetail.done.promise;
    await page.waitForFunction(()=>!document.querySelector('#history-detail-dialog').open&&!document.querySelector('#history-detail').textContent);
    await page.locator(`#history-list [data-history-delete="${ids['missing-screws']}"]`).click();
    await page.locator('#history-confirm-dialog[open]').waitFor();
    await page.locator('#history-confirm-cancel').click();
    assert.equal(mutations.length,0,'cancel does not issue delete');
    assert.equal((await api('?date='+today)).summary.total,27);
    await page.locator(`#history-list [data-history-delete="${ids['missing-screws']}"]`).click();
    failDelete=true;await page.locator('#history-confirm-delete').click();
    await page.waitForFunction(()=>document.body.textContent.includes('测试删除未保存'));
    assert.equal((await api('?date='+today)).summary.total,27,'failed delete preserves record');
    await page.locator('#history-confirm-delete').click();
    await summary({total:26,failed:22,passed:2,pending:2});
    assert.equal(await record(ids['missing-screws']).count(),0);
    const deleted=await api('?date='+today+'&deleted=true');
    assert.equal(deleted.total,1);assert.equal(deleted.items[0].streamId,ids['missing-screws']);
    await page.waitForFunction(()=>document.querySelector('#failed').textContent==='22');
    await page.locator('#history-trash').click();await record(ids['missing-screws']).waitFor();
    await screenshot('history-recycle-bin');
    await page.reload();await page.waitForFunction(()=>document.querySelector('#connection').dataset.state==='live');
    await page.locator('#history-open').click();await summary({total:26,failed:22});
    await page.locator('#history-trash').click();await record(ids['missing-screws']).waitFor();
    await page.locator(`[data-history-detail="${ids['missing-screws']}"]`).click();
    await page.locator(`#history-detail [data-history-restore="${ids['missing-screws']}"]`).waitFor();
    failRestore=true;await page.locator(`#history-detail [data-history-restore="${ids['missing-screws']}"]`).click();
    await page.waitForFunction(()=>document.querySelector('#history-detail-message')?.textContent.includes('测试恢复未保存'));
    assert.equal(await page.locator('#history-detail-message').isVisible(),true,'restore error is visible above modal');
    assert.equal((await api('?date='+today)).summary.total,26);
    await page.locator(`#history-detail [data-history-restore="${ids['missing-screws']}"]`).click();
    await summary({total:27,failed:23});
    assert.equal((await api('?date='+today+'&deleted=true')).total,0);
    await page.locator('#history-trash').click();await record(ids['missing-screws']).waitFor();
    failList=true;await page.locator('#history-refresh').click();
    await page.waitForFunction(()=>document.querySelector('#history-dialog').textContent.includes('测试历史读取失败'));
    await page.locator('#history-refresh').click();await record(ids['missing-screws']).waitFor();
    await summary({total:27,failed:23});
    await page.setViewportSize({width:390,height:844});await noOverflow();await screenshot('history-failures-mobile');
    await page.locator(`[data-history-detail="${ids['missing-screws']}"]`).click();
    await page.locator('#history-detail-dialog[open]').waitFor();
    await page.waitForFunction(()=>document.querySelector('#history-detail').textContent.includes('缺少 S10、S11'));
    await noOverflow();await screenshot('history-detail-mobile');await page.locator('#history-close-detail').click();
    await page.locator(`#history-list [data-history-delete="${ids['missing-screws']}"]`).click();
    await page.locator('#history-confirm-dialog[open]').waitFor();await noOverflow();await screenshot('history-delete-mobile');
    await page.locator('#history-confirm-cancel').click();await page.locator('#history-close').click();
    await page.setViewportSize({width:1920,height:1080});
    await page.locator('#demo-mode').click();
    const callsBeforeDemo=listCalls,mutationsBeforeDemo=mutations.length;
    await page.locator('#history-open').click();
    assert.equal(await page.locator('#history-dialog[open]').count(),0,'demo does not show real history');
    assert.equal(listCalls,callsBeforeDemo);assert.equal(mutations.length,mutationsBeforeDemo);
    await page.locator('#live-mode').click();await page.locator('#history-open').click();await summary({total:27});
    const loggedOutDetail=holdDetail=delayed();
    await page.locator(`[data-history-detail="${ids['missing-screws']}"]`).click();await loggedOutDetail.seen.promise;
    await page.evaluate(()=>Workspace.logout());
    loggedOutDetail.release.resolve();await loggedOutDetail.done.promise;
    assert.equal(await page.locator('#history-dialog[open],#history-detail-dialog[open],#history-confirm-dialog[open]').count(),0);
    assert.equal(await page.locator('#history-list [data-history-id]').count(),0,'logout clears private history DOM');
    assert.equal(await page.locator('#history-detail').textContent(),'','late detail cannot restore logged-out data');
    const unauthorized=await context.request.get(base+'/api/v1/board/history');assert.equal(unauthorized.status(),401);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'history-ui-report.json'),JSON.stringify({date:today,previousDay:yesterday,
      seeded:rows.length,visible:27,failed:23,passed:2,pending:2,mutations,listCalls,errors,checks:'passed'},null,2));
    console.log('Daily history browser checks passed; screenshots: '+output);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
