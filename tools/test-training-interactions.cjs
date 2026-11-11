/* UI behavior fixtures; no production training data or model registry writes. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.TRAIN_INTERACTION_BASE||'http://127.0.0.1:8000';
const output=path.resolve('test-results/training-interactions-20261105');
fs.mkdirSync(output,{recursive:true});
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[],posts=[],cancels=[];
  page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(10000);
  const policy={min_batches_per_split:3,min_class_samples:20,min_negative_samples:20,min_precision:.95,min_recall:.95};
  const recipe={backend:'ultralytics',task:'detect',dataset:'runtime-data/datasets/station1/manifest.json',pretrained:'D:\\Project\\AISOP\\models\\yolo11n.pt',seed:123,train:{epochs:120,batch:4,imgsz:768,patience:30,device:0,workers:0},evaluation:{confidence:.67}};
  const timestamp=Math.floor(Date.now()/1000);
  let loggedIn=true,failList=false,failCreate=false,delayCreate=null,releaseCreate=null,listReads=0,detailReads=0;
  let runs=['QUEUED','RUNNING','CANDIDATE','REJECTED','FAILED','CANCELLED'].map((state,index)=>({
    id:'fixture-'+state.toLowerCase(),name:state==='FAILED'?'<img src=x onerror=alert(1)> 审计失败':'工位训练 '+state,
    state,stage:state==='RUNNING'?'TRAIN':state,station:index%2?'station2':'station1',recipe,policy,created_at:timestamp-index*100,updated_at:timestamp,progress:.42,message:'交互验证夹具，非真实训练结果',audit:null,card:null,evaluation:null,model_id:state==='CANDIDATE'?'candidate-model':null
  }));
  const model={id:'candidate-model',name:'工位1候选',version:'train-fixture',task:'detect',enabled:false,size:1000,created_at:timestamp,classes:{0:'component'},sha256:'test-hash',notes:'测试夹具'};
  await page.route('**/api/v1/**',async route=>{
    const req=route.request(),u=new URL(req.url()),p=u.pathname.replace('/api/v1',''),method=req.method();
    const reply=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(p==='/session')return reply(loggedIn?{username:'test-admin',csrf:'test-csrf'}:{detail:'请登录'},loggedIn?200:401);
    if(p==='/logout'){loggedIn=false;return reply({ok:true});}
    if(p==='/admin/model-candidates')return reply({items:[{path:'models/yolo11n.pt',size:1000},{path:'models/classify.pt',size:1000}]});
    if(p==='/admin/models')return reply({items:[model]});
    if(p==='/admin/scenes')return reply({items:[]});
    if(p==='/admin/catalog')return reply({models:[model],scenes:[]});
    if(p==='/admin/training-runs'&&method==='GET'){
      listReads++;
      if(failList)return reply({detail:'测试断线'},503);
      const group=r=>r.state==='QUEUED'?'queued':['RUNNING','CANCELLING'].includes(r.state)?'running':r.state==='CANDIDATE'?'candidate':['FAILED','REJECTED'].includes(r.state)?'attention':'cancelled';
      const counts=Object.fromEntries(['queued','running','candidate','attention','cancelled'].map(key=>[key,runs.filter(r=>group(r)===key).length]));
      const {search='',station='all',status='all',limit=25,offset=0}=Object.fromEntries(u.searchParams);
      const filtered=runs.filter(r=>(station==='all'||r.station===station)&&(status==='all'||group(r)===status)&&[r.name,r.id].join(' ').toLowerCase().includes(search.toLowerCase()));
      return reply({items:filtered.slice(+offset,+offset+ +limit).map(({recipe,policy,audit,card,evaluation,...r})=>({...r,task:recipe.task})),total:filtered.length,counts,has_more:+offset+ +limit<filtered.length,execution:'CUDA GPU'});
    }
    if(/^\/admin\/training-runs\/[^/]+$/.test(p)&&method==='GET'){detailReads++;return reply(runs.find(r=>r.id===p.split('/')[3]));}
    if(p==='/admin/training-runs'&&method==='POST'){
      posts.push({body:req.postDataJSON(),key:req.headers()['idempotency-key']});
      if(delayCreate)await delayCreate;
      if(failCreate)return reply({detail:'测试网络故障'},503);
      const run={...runs[0],...req.postDataJSON(),id:'new-'+posts.length,state:'QUEUED',stage:'QUEUED'};
      runs=[run,...runs];return reply(run,202);
    }
    if(p.endsWith('/cancel')){
      const id=p.split('/')[3];cancels.push(id);runs=runs.map(r=>r.id===id?{...r,state:'CANCELLED',stage:'CANCELLED'}:r);
      return reply(runs.find(r=>r.id===id));
    }
    if(p.endsWith('/log'))return reply({text:'fixture log <script>literal</script>'});
    return reply({detail:'Unhandled fixture endpoint '+p},404);
  });
  const go=async()=>{await page.goto(base+'/training');await page.locator('.training-run').first().waitFor();await page.waitForFunction(()=>document.querySelector('#training-pretrained').options.length===3);};
  const count=async n=>page.waitForFunction(n=>document.querySelectorAll('.training-run').length===n,n);
  const fits=async()=>assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'layout fits viewport');
  try{
    await go();await count(6);
    assert.equal(detailReads,0,'list load does not request complete training reports');
    assert.equal(await page.locator('#count-attention').innerText(),'2');
    assert.equal(await page.locator('#training-runs img').count(),0,'dataset text is escaped');
    await page.locator('#training-filter-station').selectOption('station2');await count(3);
    await page.locator('#training-filter-status').selectOption('attention');await count(1);
    await page.locator('#training-search').fill('no match');await count(0);
    assert.equal(await page.locator('#training-no-results').isVisible(),true);
    await page.locator('#training-clear-filters').click();await count(6);
    await page.locator('[data-overview=attention]').click();await count(2);
    await page.locator('[data-overview=attention]').click();await count(6);
    await page.locator('[data-cancel="fixture-queued"]').click();
    await page.locator('#confirm-dismiss').click();assert.equal(cancels.length,0);
    await page.locator('[data-cancel="fixture-queued"]').click();
    await page.locator('#confirm-accept').click();
    await page.waitForFunction(()=>!document.querySelector('[data-cancel="fixture-queued"]'));assert.deepEqual(cancels,['fixture-queued']);

    await page.locator('#training-submit').click();assert.equal(posts.length,0);
    await page.locator('#training-name').fill('   ');await page.locator('#training-submit').click();assert.equal(posts.length,0);
    await page.locator('#training-name').fill('分类交互验证');
    await page.locator('#training-task').selectOption('classify');
    assert.equal(await page.locator('#training-confidence').isVisible(),false);
    assert.equal(await page.locator('#training-confidence').isDisabled(),true);
    assert.equal(await page.locator('#policy-negatives').isDisabled(),true);
    await page.locator('#training-dataset').fill(recipe.dataset);
    await page.locator('#training-pretrained').selectOption('models/classify.pt');
    await page.locator('#training-batch').fill('33');await page.locator('#training-submit').click();assert.equal(posts.length,0);
    await page.locator('#training-batch').fill('8');
    // A field inside a collapsed policy panel must become reachable on validation.
    await page.evaluate(()=>{document.querySelector('#policy-batches').value='1';});
    await page.locator('#training-submit').click();assert.equal(posts.length,0);
    assert.equal(await page.locator('.training-policy').getAttribute('open'),'');
    await page.locator('#policy-batches').fill('3');
    failCreate=true;
    await page.locator('#training-submit').click();
    await page.waitForFunction(()=>document.querySelector('#training-submit-status').textContent.includes('测试网络故障'));
    assert.equal(posts.length,1);assert.ok(!('evaluation' in posts[0].body.recipe));
    assert.ok(!('min_negative_samples' in posts[0].body.policy));
    failCreate=false;await page.locator('#training-submit').click();
    await page.waitForFunction(()=>document.querySelector('#training-submit').textContent==='此配置已提交');
    assert.equal(posts.length,2);assert.equal(posts[0].key,posts[1].key,'same payload retry reuses the request key');
    assert.equal(await page.locator('#training-submit').isDisabled(),true);

    await page.locator('[data-copy="fixture-failed"]').click();
    await page.waitForFunction(()=>document.querySelector('#training-imgsz').value==='768');
    assert.equal(await page.locator('#training-imgsz').inputValue(),'768');
    assert.equal(await page.locator('#training-pretrained').inputValue(),recipe.pretrained);
    assert.equal(await page.locator('#training-seed').inputValue(),'123');
    assert.equal(await page.locator('#training-confidence').inputValue(),'.67'.replace(/^\./,'0.'));
    await page.locator('#training-submit').click();
    await page.waitForFunction(()=>document.querySelector('#training-submit').textContent==='此配置已提交');
    assert.equal(posts[2].body.recipe.train.patience,30,'copy preserves unexposed recipe parameters');
    assert.equal(posts[2].body.recipe.train.imgsz,768);
    assert.equal(posts[2].body.recipe.pretrained,recipe.pretrained);
    await page.locator('#training-name').fill('尚未提交的编辑');
    await page.locator('[data-copy="fixture-rejected"]').click();
    await page.locator('#confirm-dismiss').click();assert.equal(await page.locator('#training-name').inputValue(),'尚未提交的编辑');
    await page.locator('#training-reset').click();await page.locator('#confirm-dismiss').click();
    assert.equal(await page.locator('#training-name').inputValue(),'尚未提交的编辑');
    await page.locator('#training-reset').click();await page.locator('#confirm-accept').click();
    assert.equal(await page.locator('#training-name').inputValue(),'');
    await page.locator('#training-task').selectOption('feature-classify');
    assert.equal(await page.locator('#training-pretrained').isDisabled(),true);
    assert.equal(await page.locator('#training-epochs').isDisabled(),true);
    assert.equal(await page.locator('#training-feature-schema').isVisible(),true);
    failList=true;await page.locator('#refresh-training').click();
    await page.waitForFunction(()=>document.querySelector('#training-connection').dataset.status==='error');
    assert.equal(await page.locator('.training-run').count(),8,'stale records remain on sync error');
    failList=false;await page.locator('#refresh-training').click();
    await page.waitForFunction(()=>document.querySelector('#training-connection').dataset.status==='online');
    await page.locator('[data-detail="fixture-failed"]').click();
    await page.locator('#training-log summary').click();
    await page.waitForFunction(()=>document.querySelector('#training-log-text').textContent.includes('fixture log'));
    assert.equal(await page.locator('#training-log-text script').count(),0);
    await page.screenshot({path:path.join(output,'failed-run-detail.png'),fullPage:true});
    await page.locator('#training-close').click();
    await page.locator('#training-task').selectOption('detect');
    await page.evaluate(()=>scrollTo(0,0));
    await page.screenshot({path:path.join(output,'training-desktop.png'),fullPage:true});
    for(const width of [768,390,320]){await page.setViewportSize({width,height:900});await fits();}
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,'training-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1050});
    await page.locator('[data-run="fixture-candidate"] a').click();
    await page.locator('#model-details-dialog').waitFor({state:'visible'});
    assert.equal(await page.locator('#models-tab').getAttribute('aria-selected'),'true');
    assert.equal(await page.locator('#model-search').inputValue(),'candidate-model');
    assert.equal(await page.locator('#model-rows tr').count(),1);
    assert.ok((await page.locator('#model-details-content').innerText()).includes('工位1候选'));
    await page.screenshot({path:path.join(output,'candidate-model-link.png'),fullPage:true});

    // History beyond the former 100-row cap remains searchable; counts cover all pages.
    runs.push(...Array.from({length:120},(_,i)=>({...runs[0],id:'archive-'+i,name:'历史归档 '+i,state:'CANDIDATE',stage:'CANDIDATE',model_id:null})));
    await go();await count(25);
    assert.equal(await page.locator('#count-candidate').innerText(),'121');
    await page.locator('#training-next').click();
    await page.waitForFunction(()=>document.querySelector('#training-page').textContent==='第 2 页');
    await count(25);
    await page.locator('#training-search').fill('历史归档 119');await count(1);
    assert.ok((await page.locator('.training-run').innerText()).includes('archive-119'));
    assert.equal(await page.locator('#count-candidate').innerText(),'121');
    await page.locator('#training-search').fill('');await count(25);
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
    await page.waitForLoadState('networkidle');const beforeHidden=listReads;
    await page.waitForTimeout(3300);
    assert.equal(listReads,beforeHidden,'hidden page stops polling');
    await page.evaluate(()=>{delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));});
    await page.waitForFunction(()=>document.querySelector('#training-connection').dataset.status==='online');

    // A late create response must never restore training data after logout.
    await go();await page.locator('#training-task').selectOption('feature-classify');
    await page.locator('#training-name').fill('登出竞态验证');await page.locator('#training-dataset').fill(recipe.dataset);
    await page.locator('#training-feature-schema').fill('extractor-v1');
    delayCreate=new Promise(resolve=>releaseCreate=resolve);
    await page.locator('#training-submit').click();
    await page.waitForFunction(()=>document.querySelector('#training-form').getAttribute('aria-busy')==='true');
    assert.equal(await page.locator('#training-name').isDisabled(),true);
    await page.locator('#account-button').click();await page.locator('#training-locked').waitFor({state:'visible'});
    releaseCreate();delayCreate=null;
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('.training-run').count(),0);
    assert.equal(await page.locator('#training-submit-status').innerText(),'');
    assert.equal(await page.locator('#training-name').inputValue(),'');
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify({passed:true,scope:'Mock API interaction fixtures; real HTML/CSS/JS served from '+base,checks:['filters','escaped content','cancel confirmation','conditional parameters','form validation','idempotent retry','copy recipe','reset confirmation','sync recovery','logs','320/390/768/1440 layout','candidate deep link','logout race'],pageErrors:errors},null,2));
    console.log('Training interaction checks passed. Artifacts: '+output);
  }finally{if(releaseCreate)releaseCreate();await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});

