// Real HTTP deletion runs only against the disposable browser-test database.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {randomUUID}=require('node:crypto'),{spawnSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output||!path.resolve(output).startsWith(path.resolve('test-results/pilot-browser-tests')+path.sep))
  throw new Error('Use the isolated test-pilot-ui.py runner');
function seed(count=1,{owner=process.env.APP_USERNAME,days=0,recycled=false}={}){
  const ids=Array.from({length:count},()=>randomUUID());
  const script=`import json,sqlite3,sys,time
p=json.loads(sys.stdin.buffer.read().decode('utf-8'))
with sqlite3.connect(p['db']) as c:
 for i,identity in enumerate(p['ids']):
  t=time.time()-p['days']*86400-5-i
  c.execute('INSERT INTO station_inspections (stream_id,station_id,owner,context,status,result,reason,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)',(identity,'station-2',p['owner'],json.dumps({'analysisType':'screw','cameraId':'selected-camera','inputSource':'replay'}),'FINISHED',json.dumps({'compliance':{'status':'FAIL','label':'检测异常','reason':'测试缺少螺丝'}}),'fixture',t-10,t))
  if p['recycled']: c.execute('INSERT INTO station_inspection_deletions (stream_id,owner,deleted_at,deleted_by) VALUES (?,?,?,?)',(identity,p['owner'],t,p['owner']))
`;
  const result=spawnSync(resolvePython(),['-c',script],{input:JSON.stringify({db:path.join(output,'test.db'),ids,owner,days,recycled}),encoding:'utf8',windowsHide:true});
  assert.equal(result.status,0,result.stderr);return ids;
}
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1080}}),page=await context.newPage();
  const errors=[],posts=[];let loseResponse=false;
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/v1/board/reset/selected',async route=>{
    const payload=route.request().postDataJSON();posts.push(payload.streamIds);
    if(loseResponse){loseResponse=false;const response=await route.fetch();assert.equal(response.status(),200);return route.abort('failed');}
    return route.continue();
  });
  const idle=()=>page.waitForFunction(()=>!document.querySelector('#advanced-record-refresh').disabled
    && document.querySelector('#advanced-record-list').getAttribute('aria-busy')==='false');
  const mutationIdle=()=>page.waitForFunction(()=>!document.querySelector('#advanced-close').disabled);
  const boxes=()=>page.locator('[data-select-record]');
  const chosen=async n=>assert.equal(await page.locator('#advanced-selected-count').textContent(),`已选择 ${n} 条`);
  const filter=async(id,value)=>{await page.locator('#'+id).selectOption(value);await idle();};
  const json=async url=>{const response=await context.request.get(base+'/api/v1'+url);assert.equal(response.status(),200);return response.json();};
  const confirm=async(password=process.env.APP_PASSWORD)=>{
    await page.locator('#advanced-selected-attest').check();await page.locator('#advanced-selected-password').fill(password);
    await page.locator('#advanced-selected-submit').click();await mutationIdle();
  };
  try{
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(login.status(),200);const session=await login.json();
    assert.equal((await context.request.put(base+'/api/v1/admin/terminals/station-2',{headers:{'X-CSRF-Token':session.csrf},
      data:{workstationId:'selected-pc',cameraId:'selected-camera',analysisType:'manual'}})).status(),200);
    const ids=seed(23),old=seed(1,{days:20})[0],trash=seed(1,{recycled:true})[0],privateId=seed(1,{owner:'other-account'})[0];
    await page.goto(base+'/board');await page.waitForFunction(()=>Workspace.session&&BoardAdvanced);
    await page.locator('[data-range=today]').click();
    assert.equal(await page.locator('#advanced-open').textContent(),'高级功能');
    await page.locator('#advanced-open').click();assert.equal(await page.locator('#advanced-dialog').isVisible(),false);
    await page.locator('#advanced-open').dblclick();await idle();
    assert.equal(await page.locator('#advanced-records-panel').isVisible(),true);
    assert.equal(await boxes().count(),20);assert.match(await page.locator('#advanced-record-page').textContent(),/共 23 条/);
    await boxes().first().check();await chosen(1);
    await filter('advanced-station','station-1');await chosen(0);assert.equal(await boxes().count(),0);
    await filter('advanced-station','station-2');assert.equal(await boxes().count(),20);
    await filter('advanced-verdict','PASS');assert.equal(await boxes().count(),0);
    await filter('advanced-verdict','FAIL');assert.equal(await boxes().count(),20);
    await filter('advanced-record-scope','deleted');assert.equal(await boxes().count(),1);
    assert.equal(await boxes().first().getAttribute('data-select-record'),trash);
    await filter('advanced-record-scope','active');
    const today=await page.locator('#advanced-date').inputValue();
    await page.locator('#advanced-date').fill(new Date(Date.now()+8*3600000-20*86400000).toISOString().slice(0,10));await idle();
    assert.equal(await boxes().count(),1);assert.equal(await boxes().first().getAttribute('data-select-record'),old);
    await page.locator('#advanced-date').fill(today);await idle();

    const first=await boxes().first().getAttribute('data-select-record');await boxes().first().check();
    await page.locator('#advanced-record-next').click();await idle();assert.equal(await boxes().count(),3);
    const last=await boxes().first().getAttribute('data-select-record');await boxes().first().check();await boxes().nth(1).check();
    await chosen(3);await page.locator('#advanced-record-prev').click();await idle();
    assert.equal(await page.locator(`[data-select-record="${first}"]`).isChecked(),true);
    assert.equal(await page.locator('#advanced-select-page').evaluate(el=>el.indeterminate),true);
    await page.locator('#advanced-select-page').check();await chosen(22);
    await page.locator('#advanced-select-page').uncheck();await chosen(2);
    await page.locator('#advanced-selection-clear').click();await chosen(0);
    await page.locator(`[data-select-record="${first}"]`).check();
    await page.locator('#advanced-record-next').click();await idle();await page.locator(`[data-select-record="${last}"]`).check();await chosen(2);
    await page.locator('#advanced-delete-selected').click();
    assert.equal(await page.locator('#advanced-selected-preview li').count(),2);
    assert.equal(await page.locator('#advanced-date').isDisabled(),true);
    assert.equal(await page.locator('#advanced-selected-submit').isDisabled(),true);
    await page.locator('#advanced-selected-cancel').click();assert.equal(posts.length,0);await chosen(2);
    await page.screenshot({path:path.join(output,'selected-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.ok(await page.locator('#advanced-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
    await page.screenshot({path:path.join(output,'selected-mobile.png'),fullPage:true});
    await page.locator('#advanced-delete-selected').click();
    assert.ok(await page.locator('#advanced-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
    await page.screenshot({path:path.join(output,'selected-confirm-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1080});
    await confirm('wrong');assert.match(await page.locator('#advanced-record-message').textContent(),/密码不正确/);
    assert.equal((await json('/board/reset')).total,25);await chosen(2);
    const added=seed()[0],kept=ids.find(id=>id!==first&&id!==last);
    await page.evaluate(async ids=>{
      await ReviewOutbox.storage();
      await new Promise((resolve,reject)=>{
        const request=indexedDB.open('aisop-review-outbox',2);request.onerror=()=>reject(request.error);
        request.onsuccess=()=>{const db=request.result,tx=db.transaction('reviews','readwrite');
          for(const streamId of ids)tx.objectStore('reviews').put({streamId,stationId:'station-2',status:'blocked',createdAt:1,nextAttemptAt:0,
            body:{requestId:streamId},actor:{account:Workspace.session.username}});
          tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>reject(tx.error);
        };
      });
    },[first,last,kept]);
    await page.locator('#advanced-selected-password').fill(process.env.APP_PASSWORD);
    const before=posts.length;await page.locator('#advanced-selected-submit').dblclick();await idle();
    assert.equal(posts.length,before+1);assert.deepEqual(new Set(posts.at(-1)),new Set([first,last]));
    assert.match(await page.locator('#advanced-record-message').textContent(),/已删除 2 条/);await chosen(0);
    assert.equal((await json('/board/reset')).total,24);
    const remaining=await json('/board/history?verdict=ALL&limit=100');
    assert.equal(remaining.total,22);assert.ok(remaining.items.some(row=>row.streamId===added));
    for(const id of [first,last])assert.equal(await page.evaluate(id=>ReviewOutbox.get(id),id),null);
    assert.equal((await page.evaluate(id=>ReviewOutbox.get(id),kept)).streamId,kept);
    await page.waitForFunction(()=>document.querySelector('#total').textContent==='22');

    // A lost success response must not silently replay or include new records.
    const uncertain=await boxes().first().getAttribute('data-select-record');await boxes().first().check();
    await page.locator('#advanced-delete-selected').click();loseResponse=true;const attempts=posts.length;
    await confirm();assert.match(await page.locator('#advanced-record-message').textContent(),/未确认完成/);
    assert.equal(await page.locator('#advanced-selected-password').inputValue(),'');
    assert.equal(posts.length,attempts+1);await page.locator('#advanced-selected-cancel').click();
    await page.locator('#advanced-record-refresh').click();await idle();await chosen(0);
    assert.ok(!(await json('/board/history?verdict=ALL&limit=100')).items.some(row=>row.streamId===uncertain));
    assert.equal(posts.length,attempts+1);
    await filter('advanced-record-scope','deleted');await boxes().first().check();await page.locator('#advanced-delete-selected').click();await confirm();await idle();
    assert.match(await page.locator('#advanced-record-message').textContent(),/已删除 1 条/);
    assert.equal((await json('/board/history?deleted=true&verdict=ALL')).total,0);
    await page.locator('#advanced-close').click();
    await page.locator('#advanced-open').dblclick();await idle();await chosen(0);
    assert.equal(await page.locator('#advanced-selected-form').isVisible(),false);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'board-selected.json'),JSON.stringify({passed:true,realAPI:true,isolatedDatabase:true,
      filters:true,crossPageSelection:true,confirmationCancel:true,exactIdsOnly:true,newRecordsPreserved:true,
      outboxSelective:true,lostResponse:true,recycle:true,mobile:true,posts:posts.length,errors},null,2));
    console.log('Selected record deletion passed: filters, cross-page selection, confirmation, exact IDs, preserved new records, receipts, lost response and mobile.');
  }catch(error){await page.screenshot({path:path.join(output,'selected-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
