// Statistics range integration against a disposable API/database only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {randomUUID}=require('node:crypto'),{spawnSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.PILOT_TEST_BASE,output=process.env.PILOT_TEST_OUTPUT;
if(!base||!output||!path.resolve(output).startsWith(path.resolve('test-results/pilot-browser-tests')+path.sep))
  throw Error('Use the isolated test-pilot-ui.py runner');
const today=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
const yesterday=new Date(Date.now()+8*3600000-86400000).toISOString().slice(0,10);
const older=new Date(Date.now()+8*3600000-2*86400000).toISOString().slice(0,10);
const rows=[
  {day:today,verdict:'PASS',station:1},{day:today,verdict:'FAIL',station:2},
  {day:yesterday,verdict:'PASS',station:1},{day:yesterday,verdict:'PASS',station:2},
  {day:yesterday,verdict:'FAIL',station:3},{day:yesterday,verdict:'REVIEW',station:4},
  {day:older,verdict:'PASS',station:5},
  {day:yesterday,verdict:'FAIL',station:1,recycled:true},
  {day:yesterday,verdict:'FAIL',station:1,private:true}
].map(row=>({...row,id:randomUUID()}));
const script=`import json,sqlite3,sys,time,datetime
p=json.loads(sys.stdin.buffer.read().decode('utf-8'))
with sqlite3.connect(p['db']) as c:
 for i,row in enumerate(p['rows']):
  t=min(time.time()-2,datetime.datetime.fromisoformat(row['day']+'T12:00:00+08:00').timestamp())-i
  owner='private-account' if row.get('private') else p['owner']
  c.execute('INSERT INTO station_inspections (stream_id,station_id,owner,context,status,result,reason,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)',(row['id'],'station-'+str(row['station']),owner,json.dumps({'analysisType':'screw','cameraId':'scope-camera','inputSource':'replay'}),'INTERRUPTED' if row['verdict']=='REVIEW' else 'FINISHED',json.dumps({'compliance':{'status':row['verdict'],'label':row['verdict'],'reason':'漏螺丝' if row['verdict']=='FAIL' else '范围测试'}}),'fixture',t-10,t))
  if row.get('recycled'): c.execute('INSERT INTO station_inspection_deletions (stream_id,owner,deleted_at,deleted_by) VALUES (?,?,?,?)',(row['id'],owner,t,owner))
`;
const seeded=spawnSync(resolvePython(),['-c',script],{input:JSON.stringify({db:path.join(output,'test.db'),owner:process.env.APP_USERNAME,rows}),encoding:'utf8',windowsHide:true});
assert.equal(seeded.status,0,seeded.stderr);
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1920,height:1080}}),page=await context.newPage();
  const errors=[],queries=[];let mutations=0,hold=false,release,held;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/v1/board/**',async route=>{
    if(route.request().method()!=='GET'){mutations++;return route.abort('blockedbyclient');}
    const url=new URL(route.request().url());
    if(url.pathname.endsWith('/snapshot')){
      queries.push(Object.fromEntries(url.searchParams));
      if(hold&&url.searchParams.get('range')==='all'){
        hold=false;const response=await route.fetch();held?.();
        await new Promise(resolve=>{release=resolve;});
        try{return await route.fulfill({response});}catch{return;}
      }
    }
    return route.continue();
  });
  const count=async expected=>page.waitForFunction(n=>document.querySelector('#total').textContent===String(n),expected);
  const choose=async(range,total)=>{await page.locator(`[data-range=${range}]`).click();await count(total);};
  try{
    const login=await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    assert.equal(login.status(),200);
    await page.goto(base+'/board');await count(7);
    assert.equal(await page.locator('[data-range=all]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#total-label').textContent(),'总体检测轮次');
    assert.equal(await page.locator('#alert-count').textContent(),'2');
    await page.waitForFunction(()=>document.querySelector('#trend-note').textContent.includes('同时段汇总'));
    await page.screenshot({path:path.join(output,'ranges-all-desktop.png'),fullPage:true});
    await choose('today',2);assert.match(await page.locator('#scope-label').textContent(),new RegExp(today));
    assert.equal(await page.locator('#alert-count').textContent(),'1');
    await page.locator('[data-range=date]').click();await page.locator('#board-date').fill(yesterday);await count(4);
    assert.equal(await page.locator('#passed').textContent(),'2');assert.equal(await page.locator('#failed').textContent(),'1');
    assert.match(await page.locator('#pending').textContent(),/1/);
    assert.equal(await page.locator('#alerts-heading').textContent(),'所选日期违规告警');
    assert.match(await page.locator('#station-count-label').textContent(),/归档记录/);
    assert.equal(await page.locator('#running-count').textContent(),'4');
    assert.equal(await page.locator('#alert-dialog').isHidden(),true,'Historical alerts must not pop up when switching dates');
    await page.locator('#history-open').click();
    assert.equal(await page.locator('#history-date').inputValue(),yesterday);await page.locator('#history-close').click();
    await page.locator('#advanced-open').dblclick();
    assert.equal(await page.locator('#advanced-date').inputValue(),yesterday);await page.locator('#advanced-close').click();
    await page.reload();await count(4);
    assert.equal(await page.locator('[data-range=date]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#board-date').inputValue(),yesterday);
    for(const [width,height] of [[1920,1080],[1440,1000],[390,844]]){
      await page.setViewportSize({width,height});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No horizontal overflow');
      if(width>=1440)assert.ok(await page.evaluate(()=>[...document.querySelectorAll('.station')].every(node=>node.scrollHeight<=node.clientHeight+1)),'Station cards fit');
      await page.screenshot({path:path.join(output,`ranges-date-${width}.png`),fullPage:true});
    }
    await page.setViewportSize({width:1920,height:1080});
    await page.locator('#board-date').fill(older);await count(1);
    const emptyDay='2025-01-01';await page.locator('#board-date').fill(emptyDay);await count(0);
    assert.match(await page.locator('#scope-label').textContent(),/2025-01-01/);
    await page.locator('#board-date').fill(yesterday);await count(4);
    // A response from the previous range arrives after switching again.
    const waiting=new Promise(resolve=>{held=resolve;});hold=true;
    await page.locator('[data-range=all]').click();await waiting;
    await page.locator('[data-range=date]').click();await count(4);release();
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#total').textContent(),'4');
    assert.match(await page.locator('#scope-label').textContent(),new RegExp(yesterday));
    await choose('all',7);await choose('today',2);await choose('all',7);
    await page.reload();await count(7);assert.equal(await page.locator('#board-date').isHidden(),true);
    assert.equal(mutations,0);assert.deepEqual(errors,[]);
    assert.ok(queries.some(q=>q.range==='date'&&q.date===yesterday));
    fs.writeFileSync(path.join(output,'board-ranges.json'),JSON.stringify({passed:true,realAPI:true,isolatedDatabase:true,
      all:7,today:2,yesterday:4,older:1,recycleExcluded:true,ownerIsolated:true,persistence:true,
      historicalNoPopup:true,staleResponseIgnored:true,mobile:true,mutations,errors},null,2));
    console.log('Board ranges passed: all/today/date, counts/alerts/trends, date handoff, saved preferences, late responses and mobile.');
  }catch(error){await page.screenshot({path:path.join(output,'ranges-failure.png'),fullPage:true});throw error;}
  finally{release?.();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
