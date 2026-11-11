// Real API/SQLite and persistent Chromium profile; failure injection is transport-only.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {spawnSync} = require('node:child_process');
const {resolvePython} = require('./python.cjs');
const {chromium} = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.PILOT_TEST_BASE, output = process.env.PILOT_TEST_OUTPUT;
if (!base || !output || !path.resolve(output).startsWith(path.resolve('test-results/pilot-browser-tests') + path.sep))
  throw new Error('Use the isolated test-pilot-ui.py runner');
const profile = path.join(output, 'review-profile'), errors = [], requests = [];
let context, page, mode = 'normal', conflicting = false;
function fixture(verdict = 'FAIL', status = 'FINISHED', kind = 'sticker') {
  const streamId = require('node:crypto').randomUUID();
  const result = {streamId, stationId:'station-2', workstationId:'recovery-pc', analysisType:kind, archived:true,
    compliance:{status:verdict, label:verdict, reason:'原始算法证据'}, targets:[{id:'R01', state:'UNKNOWN'}]};
  const run = spawnSync(resolvePython(), ['-c',
    'import json,sqlite3,sys,time\np=json.loads(sys.stdin.buffer.read().decode("utf-8"))\nwith sqlite3.connect(p["db"]) as c:\n c.execute("INSERT INTO station_inspections (stream_id,station_id,owner,context,status,result,reason,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",(p["id"],"station-2",p["owner"],json.dumps({"analysisType":p["kind"],"workstationId":"recovery-pc"}),p["status"],json.dumps(p["result"]),"fixture",time.time()-10,time.time()))'],
    {input:JSON.stringify({db:path.join(output,'test.db'), id:streamId, result, status, kind, owner:process.env.APP_USERNAME}), encoding:'utf8', windowsHide:true});
  assert.equal(run.status, 0, run.stderr); return result;
}
async function launch({seedLegacy=false,legacyPending=null}={}) {
  context = await chromium.launchPersistentContext(profile, {channel:'msedge', headless:true, viewport:{width:1440,height:1000}});
  page = context.pages()[0]; page.on('pageerror', error => errors.push(error.message));
  if (seedLegacy) {
    await page.route('**/outbox-seed',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><title>Migration fixture</title>'}));
    await page.goto(base+'/outbox-seed');
    await page.evaluate(legacyPending=>new Promise((resolve,reject)=>{
      const request=indexedDB.open('aisop-review-outbox',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('reviews',{keyPath:'streamId'});
      request.onerror=()=>reject(request.error);
      request.onsuccess=()=>{
        const db=request.result,tx=db.transaction('reviews','readwrite');
        tx.objectStore('reviews').put({streamId:'legacy-receipt',stationId:'station-2',status:'complete',
          createdAt:1,nextAttemptAt:0,body:{requestId:'legacy-immutable-id'},record:{receipt:'keep'}});
        if(legacyPending)tx.objectStore('reviews').put(legacyPending);
        tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>reject(tx.error);
      };
    }),legacyPending);
  }
  await context.route('**/api/v1/station-streams/*/manual-review', async route => {
    const body = route.request().postDataJSON(); requests.push(body);
    const stored = await page.evaluate(id => ReviewOutbox.get(id), route.request().url().split('/').at(-2));
    assert.equal(stored.status, 'submitting'); assert.deepEqual(stored.body, body, 'Persist before every submission');
    if (mode === 'reject') return route.fulfill({status:503,json:{message:'链路中断'}});
    if (mode === 'expired' || mode === 'csrf-rotated') {
      const current=await (await context.request.get(base+'/api/v1/session')).json();
      if(mode==='expired') {
        assert.equal((await context.request.post(base+'/api/v1/logout',{headers:{'X-CSRF-Token':current.csrf}})).status(),200);
        return route.fulfill({status:401,contentType:'text/html',body:'<h1>Session expired</h1>'});
      }
      assert.equal((await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}})).status(),200);
      return route.fulfill({status:403,json:{code:'SESSION_CSRF_INVALID',message:'会话已更新'}});
    }
    if (mode === 'lost-response') { const response = await route.fetch(); assert.equal(response.status(),200); return route.abort('failed'); }
    if (mode === 'conflict' && !conflicting) {
      conflicting = true;
      const session = await (await context.request.get(base+'/api/v1/session')).json();
      const response = await context.request.post(route.request().url(), {headers:{'X-CSRF-Token':session.csrf},
        data:{requestId:require('node:crypto').randomUUID(),outcome:'FAIL',reason:'其他确认人已核对'}});
      assert.equal(response.status(),200);
    }
    return route.continue();
  });
  await page.goto(base+'/?task=station2');
  await page.waitForFunction(() => !!window.ReviewOutbox && !!window.Workspace);
}
const enqueue = (result, outcome='PASS') => page.evaluate(({result,outcome}) => ReviewOutbox.enqueue({
  stationId:result.stationId,streamId:result.streamId,workstationId:result.workstationId,outcome,reason:'现场核对'}), {result,outcome});
const reconcile = id => page.evaluate(id => ReviewOutbox.reconcile(id), id);
const read = id => page.evaluate(id => ReviewOutbox.get(id), id);
async function authenticate() {
  const response = await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
  assert.equal(response.status(),200); await page.evaluate(() => Workspace.connect());
  return response.json();
}
(async () => {
  try {
    const legacyResult=fixture(),actor={account:process.env.APP_USERNAME,role:'admin',reviewer:process.env.APP_USERNAME,credentialRevision:null};
    const legacyPending={streamId:legacyResult.streamId,stationId:'station-2',workstationId:'recovery-pc',
      actor,createdAt:1,updatedAt:1,status:'pending',attempts:0,nextAttemptAt:0,message:'legacy pending',
      body:{requestId:require('node:crypto').randomUUID(),outcome:'PASS',reason:'版本升级前的确认',expectedActor:actor}};
    await launch({seedLegacy:true,legacyPending}); const session = await authenticate();
    assert.equal((await read('legacy-receipt')).body.requestId,'legacy-immutable-id','Upgrade preserves old receipts');
    assert.equal((await reconcile(legacyResult.streamId)).status,'complete','Upgrade retains recoverable pending requests');
    assert.deepEqual((await read(legacyResult.streamId)).body,legacyPending.body);
    const binding = await (await context.request.put(base+'/api/v1/admin/terminals/station-2', {
      headers:{'X-CSRF-Token':session.csrf},data:{workstationId:'recovery-pc',cameraId:'recovery-camera',analysisType:'manual'}})).json();
    await page.reload(); await page.waitForFunction(() => Workspace.session && StationDetection.state.connected);

    // Saved at server, response lost: after a real process restart use GET, never POST again.
    const saved = fixture(); mode = 'lost-response'; const first = await enqueue(saved);
    assert.equal((await reconcile(saved.streamId)).status,'pending');
    await page.locator('#sd-outbox-open').click(); await page.locator('[data-review-entry]').first().click();
    assert.match(await page.locator('#sd-review-error').textContent(), /待核对/);
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(output,'review-pending-mobile.png'),fullPage:true});
    const savedRequestCount = requests.length;
    await context.close(); mode = 'normal'; await launch(); await page.evaluate(() => Workspace.connect());
    const restored = await read(saved.streamId); assert.equal(restored.body.requestId,first.body.requestId);
    const completed = await reconcile(saved.streamId);
    assert.equal(completed.status,'complete'); assert.equal(requests.length,savedRequestCount);
    assert.equal(completed.record.result.compliance.status,'FAIL');

    // Request not delivered: freeze content, survive process restart and resend exactly once.
    const unsent = fixture(); mode = 'reject'; const original = await enqueue(unsent);
    assert.equal((await reconcile(unsent.streamId)).status,'pending');
    assert.deepEqual((await enqueue(unsent,'FAIL')).body, original.body, 'Cannot change an uncertain request');
    await context.close(); mode = 'normal'; await launch(); await page.evaluate(() => Workspace.connect());
    assert.equal((await reconcile(unsent.streamId)).status,'complete');
    assert.deepEqual(requests.at(-1),original.body);

    // Authentication failures keep the immutable request recoverable; renewed
    // credentials still require a fresh GET and the original actor check.
    for (const failureMode of ['expired','csrf-rotated']) {
      const interrupted=fixture();mode=failureMode;const original=await enqueue(interrupted);
      const pending=await reconcile(interrupted.streamId);
      assert.equal(pending.status,'pending');assert.match(pending.message,/登录状态恢复/);
      assert.deepEqual(pending.body,original.body);
      mode='normal';
      if(failureMode==='expired')await authenticate();
      await page.evaluate(()=>ReviewOutbox.recover());
      const deadline=Date.now()+15000;
      while((await read(interrupted.streamId))?.status!=='complete' && Date.now()<deadline)
        await new Promise(resolve=>setTimeout(resolve,50));
      assert.equal((await read(interrupted.streamId)).status,'complete');
      const attempts=requests.filter(body=>body.requestId===original.body.requestId);
      assert.equal(attempts.length,2);assert.deepEqual(attempts[1],original.body);
    }

    // A long-lived workstation must not scan completed payloads to find work.
    const future=await enqueue(fixture());
    const indexed=await page.evaluate(async future=>{
      const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('aisop-review-outbox',2);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
      await new Promise((resolve,reject)=>{
        const tx=db.transaction('reviews','readwrite'),store=tx.objectStore('reviews');
        for(let i=0;i<2000;i++)store.put({streamId:'receipt-'+i,stationId:'station-2',status:'complete',createdAt:i,nextAttemptAt:0,record:{evidence:'x'.repeat(1024)}});
        store.put({...future,nextAttemptAt:Date.now()+3600000});
        tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error);
      });db.close();
      const original=IDBObjectStore.prototype.getAll;
      IDBObjectStore.prototype.getAll=function(){throw new Error('Full history scan during recovery');};
      try {
        const start=performance.now();await ReviewOutbox.recover();
        const pending=await ReviewOutbox.list({pendingOnly:true,stationId:'station-2'});
        return {elapsedMs:performance.now()-start,ids:pending.map(entry=>entry.streamId)};
      } finally {IDBObjectStore.prototype.getAll=original;}
    },future);
    assert.ok(indexed.ids.includes(future.streamId));assert.ok(indexed.ids.every(id=>!id.startsWith('receipt-')));
    assert.equal(requests.filter(body=>body.requestId===future.body.requestId).length,0,'Future retry must not run early');
    assert.equal((await reconcile(future.streamId)).status,'complete');

    // Concurrent tabs must retain one UUID and one payload.
    const concurrent = fixture(), second = await context.newPage();
    await second.goto(base+'/?task=station2'); await second.waitForFunction(() => Workspace.session);
    const [a,b] = await Promise.all([enqueue(concurrent),second.evaluate(result=>ReviewOutbox.enqueue({
      streamId:result.streamId,stationId:result.stationId,outcome:'FAIL',reason:'另一标签页'}),concurrent)]);
    assert.deepEqual(a.body,b.body);
    await second.evaluate(id => {
      window.crossTabReview = null;
      document.addEventListener('review-outbox', event => {
        if (event.detail.streamId === id) window.crossTabReview = event.detail.status;
      });
    },concurrent.streamId);
    await Promise.all([reconcile(concurrent.streamId),second.evaluate(id=>ReviewOutbox.reconcile(id),concurrent.streamId)]);
    assert.equal(requests.filter(body=>body.requestId===a.body.requestId).length,1);
    await second.waitForFunction(() => window.crossTabReview === 'complete'); await second.close();

    // Another confirmation wins between GET and POST: show actual server result, stop retries.
    const conflict = fixture(); mode = 'conflict'; await enqueue(conflict);
    const blocked = await reconcile(conflict.streamId); assert.equal(blocked.status,'blocked');
    assert.equal(blocked.record.result.manualReview.reason,'其他确认人已核对');
    await page.locator('#sd-outbox-open').click(); await page.locator('[data-review-entry]').first().click();
    assert.match(await page.locator('#sd-review-pending').textContent(), /其他确认人已核对/);
    await page.screenshot({path:path.join(output,'review-conflict.png'),fullPage:true});
    await page.locator('#modal [data-action=close-modal]').first().click();
    const count = requests.length; mode = 'normal'; await reconcile(conflict.streamId);
    assert.equal(requests.length,count);

    // Eligibility is rechecked before sending: running, interrupted, pose, and PASS rounds never POST.
    for (const result of [fixture('FAIL','RUNNING'),fixture('REVIEW','INTERRUPTED'),fixture('REVIEW','FINISHED','pose'),fixture('PASS')]) {
      await enqueue(result); assert.equal((await reconcile(result.streamId)).status,'blocked');
    }
    assert.equal(requests.length,count);

    // Session switching does not transfer pending confirmation to the device identity.
    const switched = fixture(); await enqueue(switched);
    assert.equal((await context.request.post(base+'/api/v1/terminal/login',{data:{credential:binding.credential}})).status(),200);
    await page.evaluate(() => Workspace.connect());
    assert.equal((await reconcile(switched.streamId)).status,'blocked'); assert.equal(requests.length,count);
    const own = fixture(); const device = await enqueue(own); assert.equal(device.actor.reviewer,'station-2');
    assert.equal((await reconcile(own.streamId)).record.result.manualReview.reviewer,'station-2');

    // Storage failure prevents submission; surface the failure to the operator.
    const beforeStorageFailure = requests.length;
    const failure = await page.evaluate(async result => {
      const original = IDBDatabase.prototype.transaction;
      IDBDatabase.prototype.transaction = function(name,mode,...args) {
        if (mode === 'readwrite') throw new DOMException('测试磁盘写入失败','QuotaExceededError');
        return original.call(this,name,mode,...args);
      };
      try { await ReviewOutbox.enqueue({streamId:result.streamId,stationId:result.stationId,outcome:'PASS',reason:''}); return null; }
      catch(error) { return error.name; }
      finally { IDBDatabase.prototype.transaction = original; }
    },fixture());
    assert.equal(failure,'QuotaExceededError'); assert.equal(requests.length,beforeStorageFailure);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'review-recovery.json'),JSON.stringify({passed:true, browserProcessRestart:true,
      realAPI:true, realSQLite:true, persistentIndexedDB:true, migratedVersion:2,indexedRecovery:indexed,
      authenticationRecovery:true,requests:requests.length, errors},null,2));
    console.log('Review recovery passed: lost response, unsent retry, process restart, immutable request, concurrent tabs, conflicts, eligibility, identity and storage failure.');
  } finally { await context?.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
