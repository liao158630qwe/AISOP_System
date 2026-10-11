// Browser regression for the exported HA4M replay, using independent CLI outputs.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
let playwright;
try{playwright=require(process.env.PLAYWRIGHT_MODULE||'playwright');}
catch{playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));}
const root=path.resolve(__dirname,'..');
const version=process.argv[2]||'v2.1';
assert.ok(['v2.1','v2.2','v2.3'].includes(version),'Unknown replay version');
const directory=path.join(root,'work/simple-sop-2026-10-10',version);
const output=path.join(directory,'browser-checks');
fs.mkdirSync(output,{recursive:true});
const records=JSON.parse(fs.readFileSync(path.join(directory,'replays.json'),'utf8'));
const workflow=JSON.parse(fs.readFileSync(path.join(directory,'export/workflow.json'),'utf8'));
const read=filename=>JSON.parse(fs.readFileSync(filename,'utf8'));
const seek=async(page,value)=>page.locator('#seek').evaluate((el,n)=>{el.value=n;el.dispatchEvent(new Event('input',{bubbles:true}));},value);

async function benchmark(page){
  return page.evaluate(()=>{
    const times=[];
    for(let i=0;i<120;i++){const start=performance.now();draw();times.push(performance.now()-start);}
    times.sort((a,b)=>a-b);
    return {mean_ms:times.reduce((a,b)=>a+b,0)/times.length,p95_ms:times[Math.floor(times.length*.95)]};
  });
}

(async()=>{
  const browser=await playwright.chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(pathToFileURL(path.join(directory,'demo.html')).href);
    await page.locator('#prediction').waitFor();
    assert.equal(await page.locator('#record option').count(),records.length);
    for(let i=0;i<records.length;i++){
      const r=records[i],cli=read(path.join(directory,'runtime-outputs',r.name+'.json'));
      assert.deepEqual(r.prediction,cli.stages);
      assert.deepEqual(r.raw,cli.raw);
      assert.deepEqual(r.sop,cli.sop);
      await page.locator('#record').selectOption(String(i));
      assert.equal(await page.locator('#seek').inputValue(),'0');
      for(const frame of [0,Math.floor(r.truth.length/2),r.truth.length-1,0]){
        await seek(page,frame);
        const stage=cli.stages[frame];
        assert.equal(await page.locator('#prediction').textContent(),stage<0?'未知 / 未确认':workflow.labels_zh[stage]);
        assert.equal(await page.locator('#raw').textContent(),workflow.labels_zh[cli.raw[frame]]);
        assert.equal(await page.locator('#events > div').count(),cli.sop.events.filter(e=>e.frame<=frame).length);
        assert.equal(await page.locator('#time').textContent(),(frame/workflow.sample_hz).toFixed(1)+' / '+((r.truth.length-1)/workflow.sample_hz).toFixed(1)+' 秒');
        if(frame===r.truth.length-1)assert.match(await page.locator('#status').textContent(),/回放结束/);
      }
    }
    await page.locator('#record').selectOption('0');
    await page.locator('#play').click();
    await page.waitForFunction(()=>Number(document.querySelector('#seek').value)>1);
    await page.locator('#reset').click();
    assert.equal(await page.locator('#play').textContent(),'播放');
    await page.waitForTimeout(250);
    assert.equal(await page.locator('#seek').inputValue(),'0','reset must pause without stale elapsed time');
    await page.locator('#speed').selectOption('4');
    await page.locator('#play').click();
    await page.waitForFunction(()=>Number(document.querySelector('#seek').value)>=4);
    await seek(page,20);
    assert.equal(await page.locator('#play').textContent(),'播放');
    await page.waitForTimeout(200);
    assert.equal(await page.locator('#seek').inputValue(),'20','seek must remain paused');
    await page.locator('#play').click();
    await page.locator('#record').selectOption('1');
    assert.equal(await page.locator('#play').textContent(),'播放');
    assert.equal(await page.locator('#seek').inputValue(),'0');
    const end=records[1].truth.length-1;
    await seek(page,end-1);
    await page.locator('#play').click();
    await page.waitForFunction(()=>document.querySelector('#play').textContent==='播放');
    assert.equal(Number(await page.locator('#seek').inputValue()),end);
    await page.locator('#play').click();
    await page.waitForFunction(()=>Number(document.querySelector('#seek').value)<10);
    await page.locator('#reset').click();
    await page.locator('#record').selectOption('0');
    await seek(page,records[0].truth.length-1);
    await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
    const after=await benchmark(page);
    await page.setViewportSize({width:390,height:844});
    const overflow=await page.evaluate(()=>({width:document.documentElement.scrollWidth,viewport:innerWidth}));
    assert.ok(overflow.width<=overflow.viewport,JSON.stringify(overflow));
    const poseRatio=await page.locator('#pose').evaluate(el=>el.clientWidth/el.clientHeight);
    assert.ok(Math.abs(poseRatio-540/350)<.02,'mobile skeleton must preserve its aspect ratio');
    await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
    // A non-10 Hz fixture catches hard-coded time and duration in the template.
    const fixture=path.join(output,'sample-rate-fixture.html');
    const template=fs.readFileSync(path.join(root,'experiments/public_training/simple_scene_demo.html'),'utf8');
    const r=structuredClone(records[0]);
    r.sop.events.forEach(e=>e.seconds=e.frame/5);
    fs.writeFileSync(fixture,template.replace('/*PAYLOAD*/','const DATA = '+JSON.stringify({
      workflow:{...workflow,sample_hz:5},metrics:read(path.join(directory,'evaluation.json')),records:[r]})+';'));
    await page.goto(pathToFileURL(fixture).href);
    await seek(page,10);
    assert.match(await page.locator('#time').textContent(),/^2\.0 \/ /);
    assert.match(await page.locator('#record option').textContent(),new RegExp(((r.truth.length-1)/5).toFixed(1).replace('.','\\.')));
    await page.setViewportSize({width:1440,height:1080});
    await page.goto(pathToFileURL(path.join(root,'work/simple-sop-2026-10-10/v2/demo.html')).href);
    await seek(page,records[0].truth.length-1);
    const before=await benchmark(page);
    assert.deepEqual(errors,[]);
    const result={passed:true,recordings:records.length,cli_matches:true,controls:['play','pause','4x','seek','reset','record switch','end stop','restart'],
      no_future_events:true,custom_sample_rate:true,mobile_overflow:false,page_errors:errors,
      version,redraw_benchmark:{v2:before,current:after},screenshots:['desktop.png','mobile.png']};
    fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result,null,2));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
