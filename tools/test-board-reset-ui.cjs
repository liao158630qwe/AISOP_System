// Destructive reset is exercised only against the disposable browser-test server.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {randomUUID}=require('node:crypto'),{spawnSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output||!path.resolve(output).startsWith(path.resolve('test-results/pilot-browser-tests')+path.sep))
  throw new Error('Use the isolated test-pilot-ui.py runner');
function seed({owner=process.env.APP_USERNAME,days=0,recycled=false,running=false}={}){
  const id=randomUUID();
  const script=`import json,sqlite3,sys,time
p=json.loads(sys.stdin.buffer.read().decode('utf-8'))
t=time.time()-p['days']*86400-2
with sqlite3.connect(p['db']) as c:
 c.execute('INSERT INTO station_inspections (stream_id,station_id,owner,context,status,result,reason,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)',(p['id'],'station-2',p['owner'],json.dumps({'analysisType':'screw','cameraId':'reset-camera','inputSource':'replay'}),'RUNNING' if p['running'] else 'FINISHED',json.dumps({'compliance':{'status':'FAIL','label':'检测异常','reason':'测试缺少螺丝'}}),'fixture',t-10,None if p['running'] else t))
 if p['recycled']: c.execute('INSERT INTO station_inspection_deletions (stream_id,owner,deleted_at,deleted_by) VALUES (?,?,?,?)',(p['id'],p['owner'],t,p['owner']))
 if p['running']: c.execute('INSERT OR REPLACE INTO station_stream_leases (station_id,stream_id,epoch,runtime_id,request_id,closed,context,created_at,lease_until) VALUES (?,?,?,?,?,?,?,?,?)',('station-2',p['id'],1,'test-runtime',p['id'],0,'{}',t,time.time()+60))
`;
  const result=spawnSync(resolvePython(),['-c',script],{input:JSON.stringify({db:path.join(output,'test.db'),id,owner,days,recycled,running}),encoding:'utf8',windowsHide:true});
  assert.equal(result.status,0,result.stderr);return id;
}
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1920,height:1080}}),page=await context.newPage();
  const errors=[],posts=[];let loseResponse=false;
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/v1/board/reset',async route=>{
    if(route.request().method()==='POST'){
      posts.push(route.request().postDataJSON().revision);
      if(loseResponse){loseResponse=false;const response=await route.fetch();assert.equal(response.status(),200);return route.abort('failed');}
    }
    return route.continue();
  });
  const idle=()=>page.waitForFunction(()=>!document.querySelector('#advanced-refresh').disabled);
  const fill=async(password=process.env.APP_PASSWORD)=>{
    await page.locator('#advanced-confirmation').fill('清空全部检测数据');
    await page.locator('#advanced-password').fill(password);
  };
  const json=async url=>{const response=await context.request.get(base+'/api/v1'+url);assert.equal(response.status(),200);return response.json();};
  try{
    await page.goto(base+'/board');await page.waitForFunction(()=>!!window.Workspace && !!window.BoardAdvanced);
    await page.locator('#advanced-open').dblclick();assert.equal(await page.locator('#advanced-dialog').isVisible(),false);
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(login.status(),200);const session=await login.json();
    assert.equal((await context.request.put(base+'/api/v1/admin/terminals/station-2',{headers:{'X-CSRF-Token':session.csrf},data:{workstationId:'reset-pc',cameraId:'reset-camera',analysisType:'manual'}})).status(),200);
    const ids=[seed(),seed({days:20}),seed({recycled:true})],privateId=seed({owner:'other-account'});
    await page.reload();await page.waitForFunction(()=>Workspace.session);
    await page.locator('#demo-mode').click();await page.locator('#advanced-open').dblclick();
    assert.equal(await page.locator('#advanced-dialog').isVisible(),false);assert.equal(posts.length,0);
    await page.locator('#live-mode').click();await page.locator('#advanced-open').click();
    assert.equal(await page.locator('#advanced-dialog').isVisible(),false,'A single click must not open advanced functions');
    await page.locator('#advanced-open').dblclick();await page.locator('#advanced-mode-all').click();await idle();
    assert.equal(await page.locator('#advanced-dialog').isVisible(),true);
    assert.equal(await page.locator('#advanced-total').textContent(),'3');
    assert.equal(await page.locator('#advanced-recycled').textContent(),'1');
    assert.equal(await page.locator('#advanced-submit').isDisabled(),true);
    await page.locator('#advanced-close').click();assert.equal((await json('/board/reset')).total,3);
    await page.locator('#advanced-open').press('Enter');
    assert.equal(await page.locator('#advanced-dialog').isVisible(),false);
    await page.locator('#advanced-open').press('Enter');await page.locator('#advanced-mode-all').click();await idle();await fill('incorrect');
    await page.locator('#advanced-submit').click();await idle();
    assert.match(await page.locator('#advanced-message').textContent(),/密码不正确/);
    assert.equal((await json('/board/reset')).total,3);
    await page.locator('#advanced-refresh').click();await idle();
    ids.push(seed());await fill();await page.locator('#advanced-submit').click();await idle();
    assert.match(await page.locator('#advanced-message').textContent(),/记录已变化/);
    assert.equal((await json('/board/reset')).total,4);
    await page.locator('#advanced-refresh').click();await idle();
    await page.screenshot({path:path.join(output,'advanced-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.ok(await page.locator('#advanced-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
    await page.screenshot({path:path.join(output,'advanced-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1920,height:1080});

    // A different detection tab receives the reset and drops only deleted receipts.
    const second=await context.newPage();await second.goto(base+'/?task=station2');
    await second.waitForFunction(()=>Workspace.session&&!!ReviewOutbox);
    await second.evaluate(async({ids,privateId})=>{
      await ReviewOutbox.storage();
      await new Promise((resolve,reject)=>{
        const request=indexedDB.open('aisop-review-outbox',2);request.onerror=()=>reject(request.error);
        request.onsuccess=()=>{
          const db=request.result,tx=db.transaction('reviews','readwrite');
          for(const streamId of [...ids,privateId])tx.objectStore('reviews').put({streamId,stationId:'station-2',
            status:'blocked',createdAt:1,nextAttemptAt:0,body:{requestId:streamId},actor:{account:streamId===privateId?'other-account':Workspace.session.username}});
          tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>reject(tx.error);
        };
      });
      window.resetNotice=null;document.addEventListener('inspection-data-reset',event=>{window.resetNotice=event.detail;});
      const original=Workspace.request;
      Workspace.request=async(path,options)=>{
        const response=await original(path,options);
        if(path==='/station-streams/'+ids[0]+'/inspection')
          await new Promise(resolve=>{window.releaseResetRead=resolve;});
        return response;
      };
      window.delayedReview=ReviewOutbox.reconcile(ids[0]);
    },{ids,privateId});
    await second.waitForFunction(()=>!!window.releaseResetRead);
    await fill();const before=posts.length;
    await page.locator('#advanced-submit').dblclick();await idle();
    assert.equal(posts.length,before+1,'Repeated click must submit only once');
    assert.match(await page.locator('#advanced-message').textContent(),/已清空 4 条/);
    await page.waitForFunction(()=>document.querySelector('#total').textContent==='0'&&document.querySelector('#alert-count').textContent==='0');
    await second.waitForFunction(()=>window.resetNotice?.streamIds.length===4);
    assert.equal(await second.evaluate(async()=>{window.releaseResetRead();return (await window.delayedReview).status;}),'cleared');
    for(const id of ids)assert.equal(await second.evaluate(id=>ReviewOutbox.get(id),id),null);
    assert.equal((await second.evaluate(id=>ReviewOutbox.get(id),privateId)).streamId,privateId);
    assert.equal((await json('/board/history?verdict=ALL')).total,0);
    assert.equal((await json('/board/history?deleted=true')).total,0);
    assert.equal((await json('/admin/terminals')).items.length,1);
    await page.screenshot({path:path.join(output,'advanced-cleared.png'),fullPage:true});
    await second.close();await page.reload();await page.waitForFunction(()=>document.querySelector('#total').textContent==='0');

    seed();await page.locator('#advanced-open').dblclick();await page.locator('#advanced-mode-all').click();await idle();await fill();loseResponse=true;
    await page.locator('#advanced-submit').click();await idle();
    assert.match(await page.locator('#advanced-message').textContent(),/未确认完成/);
    assert.equal(await page.locator('#advanced-submit').isDisabled(),true);
    await page.locator('#advanced-refresh').click();await idle();
    assert.equal(await page.locator('#advanced-total').textContent(),'0');
    assert.match(await page.locator('#advanced-message').textContent(),/无需清空/);

    seed({running:true});await page.locator('#advanced-refresh').click();await idle();await fill();
    assert.equal(await page.locator('#advanced-submit').isDisabled(),true);
    assert.match(await page.locator('#advanced-message').textContent(),/正在运行/);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'board-reset.json'),JSON.stringify({passed:true,realAPI:true,isolatedDatabase:true,
      clearedAllDates:true,recycleCleared:true,configurationPreserved:true,localOutboxCleared:true,
      crossTab:true,inFlightReceiptFenced:true,lostResponse:true,runningBlocked:true,posts:posts.length,errors},null,2));
    console.log('Board reset passed: scope, confirmation, password, concurrency revision, all dates, trash, counters, cross-tab receipts, mobile and lost response.');
  }catch(error){await page.screenshot({path:path.join(output,'advanced-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
