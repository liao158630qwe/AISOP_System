const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.DATASET_TEST_BASE,output=process.env.DATASET_TEST_OUTPUT;
if(!base||!output)throw Error('Run tools/test-datasets-ui.py');
(async()=>{
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(20000);
  const screenshot=name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
  const json=async r=>{assert.ok(r.ok(),await r.text());return r.json();};
  let csrf;
  const api=async(method,url,body)=>json(await page.request.fetch(base+'/api/v1'+url,{method,headers:{'X-CSRF-Token':csrf,'Idempotency-Key':crypto.randomUUID()},...(body===undefined?{}:{data:body})}));
  const wait=async(fn,timeout=60000)=>{const until=Date.now()+timeout;while(Date.now()<until){const value=await fn();if(value)return value;await new Promise(r=>setTimeout(r,300));}throw Error('Timed out waiting for dataset job');};
  try{
    await page.goto(base+'/datasets');await page.locator('#workspace-login').waitFor({state:'visible'});
    await page.locator('#workspace-login [name=username]').fill(process.env.APP_USERNAME);await page.locator('#workspace-login [name=password]').fill(process.env.APP_PASSWORD);
    await page.locator('#workspace-login button[type=submit]').click();await page.locator('#content').waitFor({state:'visible'});
    csrf=await page.evaluate(()=>Workspace.session.csrf);
    await page.locator('#new-dataset').click();await page.locator('#new-name').fill('视频流程验证 · 非现场数据');await page.locator('#new-classes').fill('螺丝\n孔位');await page.locator('#create-submit').click();
    await page.locator('#detail').waitFor({state:'visible'});const did=new URL(page.url()).searchParams.get('id');assert.ok(did);
    await page.locator('#video-file').setInputFiles(path.join(output,'video-0.avi'));
    for(const [id,v] of Object.entries({'frame-budget':'12',batch:'b0',cycle:'c0',product:'fixture',camera:'cam1',lighting:'normal',occlusion:'clear'}))await page.locator('#'+id).fill(v);
    await page.locator('#upload-button').click();await page.locator('.frame-card').first().waitFor({state:'visible',timeout:60000});
    assert.equal(await page.locator('.frame-card').count(),12);assert.ok(await page.locator('#export-button').isDisabled());
    await page.locator('.frame-card').first().click();await page.locator('#save-label').waitFor({state:'visible'});
    await wait(()=>page.locator('#save-label').isEnabled());
    for(const [cls,start,end] of [['0',.1,.4],['1',.6,.9]]){
      await page.locator('#annotation-class').selectOption(cls);const box=await page.locator('#canvas').boundingBox();
      await page.mouse.move(box.x+box.width*start,box.y+box.height*.2);await page.mouse.down();await page.mouse.move(box.x+box.width*end,box.y+box.height*.7,{steps:5});await page.mouse.up();
    }
    assert.equal(await page.locator('#box-list li').count(),2);await screenshot('annotation-desktop');await page.locator('#save-label').click();await page.locator('#annotator').waitFor({state:'hidden'});
    let result=await api('GET',`/admin/datasets/${did}/frames`);assert.equal(result.items[0].annotation.boxes.length,2);
    await page.locator('.frame-card').nth(1).click();await wait(()=>page.locator('#save-negative').isEnabled());await page.locator('#save-negative').click();await page.locator('#annotator').waitFor({state:'hidden'});
    // API exercises full-sized independent source groups through the real worker.
    for(let seed=1;seed<6;seed++){
      const split=['train','val','test'][Math.floor(seed/2)];
      const params=new URLSearchParams({filename:'video-'+seed+'.avi',frame_budget:'12',capture:JSON.stringify({split,batch:'b'+seed,cycle:'c'+seed,product:'fixture',camera:'cam1',lighting:'normal',occlusion:'clear'})});
      await json(await page.request.post(base+`/api/v1/admin/datasets/${did}/videos?`+params,{headers:{'X-CSRF-Token':csrf,'Idempotency-Key':crypto.randomUUID(),'Content-Type':'application/octet-stream'},data:fs.readFileSync(path.join(output,'video-'+seed+'.avi'))}));
    }
    let dataset=await wait(async()=>{const d=await api('GET','/admin/datasets/'+did);if(d.videos.some(v=>v.state==='FAILED'))throw Error(JSON.stringify(d.videos));return d.videos.every(v=>v.state==='SUCCEEDED')?d:null;});
    for(const v of dataset.videos){
      const list=await api('GET',`/admin/datasets/${did}/frames?video=${v.id}`);assert.equal(list.items.length,12);
      for(let i=0;i<list.items.length;i++){const f=list.items[i],positive=i<6;await api('PUT',`/admin/datasets/${did}/frames/${f.id}`,{revision:f.revision,review:positive?'labelled':'negative',boxes:positive?[{class_id:0,xyxy:[.1,.2,.4,.7]},{class_id:1,xyxy:[.6,.2,.9,.7]}]:[]});}
    }
    await page.locator('#refresh').click();await wait(async()=>!(await page.locator('#review-counts').innerText()).includes('待标注 1')&&(await page.locator('#review-counts').innerText()).includes('待标注 0'));
    for(const [id,v] of [['min-batches','2'],['min-classes','5'],['min-negatives','5']])await page.locator('#'+id).fill(v);
    await wait(()=>page.locator('#export-button').isEnabled());assert.equal(await page.locator('.frame-card').count(),24);
    await page.locator('#frame-next').click();await wait(async()=>(await page.locator('#frame-page').innerText()).startsWith('2 /'));
    await screenshot('dataset-desktop');await page.locator('#export-button').click();
    const trainLink=page.locator('#export-list a');await trainLink.waitFor({state:'visible',timeout:60000});
    const href=await trainLink.getAttribute('href'),exportId=new URL(href,base).searchParams.get('dataset');
    await page.setViewportSize({width:390,height:844});await screenshot('dataset-mobile');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.setViewportSize({width:1440,height:1100});await trainLink.click();
    await wait(async()=>(await page.locator('#training-dataset').inputValue())==='dataset://'+exportId);
    assert.equal(await page.locator('#training-station').inputValue(),'station1');assert.equal(await page.locator('#policy-batches').inputValue(),'2');
    await page.locator('#training-pretrained').selectOption('models/yolo11n.pt');await page.locator('#training-epochs').fill('1');await page.locator('#training-batch').fill('2');
    await page.locator('#training-imgsz').evaluate(el=>el.add(new Option('128 fixture','128')));await page.locator('#training-imgsz').selectOption('128');
    await page.locator('#training-submit').click();
    await page.waitForFunction(()=>document.querySelector('#training-runs').textContent.includes('测试未达标'),null,{timeout:180000});
    const runs=await api('GET','/admin/training-runs');assert.equal(runs.items[0].state,'REJECTED');
    const trained=await api('GET','/admin/training-runs/'+runs.items[0].id);assert.equal(trained.recipe._dataset_ref,'dataset://'+exportId);assert.ok(trained.audit);assert.ok(trained.evaluation);
    await screenshot('training-from-video');
    await page.goto(base+'/datasets');await page.locator('#new-dataset').click();await page.locator('#new-name').fill('分类验证');await page.locator('#new-task').selectOption('classify');await page.locator('#new-classes').fill('合格\n缺陷');await page.locator('#create-submit').click();await wait(async()=>(await page.locator('#dataset-title').innerText())==='分类验证');
    const classifyId=new URL(page.url()).searchParams.get('id');const params=new URLSearchParams({filename:'classification.avi',frame_budget:'1',capture:JSON.stringify({split:'train',batch:'classification-b',cycle:'classification-c',product:'fixture',camera:'cam1',lighting:'normal',occlusion:'clear'})});
    await json(await page.request.post(base+`/api/v1/admin/datasets/${classifyId}/videos?`+params,{headers:{'X-CSRF-Token':csrf,'Idempotency-Key':crypto.randomUUID(),'Content-Type':'application/octet-stream'},data:fs.readFileSync(path.join(output,'video-0.avi'))}));
    await page.locator('#refresh').click();await page.locator('.frame-card').first().waitFor({timeout:60000});await page.locator('.frame-card').first().click();await wait(()=>page.locator('#save-label').isEnabled());assert.ok(await page.locator('#save-negative').isHidden());
    await page.locator('#annotation-class').selectOption('1');await page.locator('#save-label').click();await page.locator('#annotator').waitFor({state:'hidden'});
    result=await api('GET',`/admin/datasets/${classifyId}/frames`);assert.equal(result.items[0].annotation.class_id,1);
    await page.locator('#account-button').click();await page.locator('#locked').waitFor({state:'visible'});assert.equal(await page.locator('.frame-card').count(),0);
    assert.deepEqual(errors,[]);console.log('PASS: real video uploads, CPU subprocess extraction, canvas boxes, negatives, classification, paging, export, training handoff, real CUDA epoch, responsive layout, logout.');
  }catch(e){await screenshot('failure');throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
