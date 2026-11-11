const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.MODEL_IMPORT_BASE||'http://127.0.0.1:8000',output=path.resolve('test-results/model-import-20261105');
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(10000);
 let models=[],mode='success',catalogFail=false,registered=null,release=null;
 const result={id:'fixture-model',name:'服务器模型',version:'2',task:'detect',classes:{0:'part',1:'<script>literal</script>'},size:12,sha256:'a'.repeat(64),enabled:true,created_at:1,notes:''};
 await page.route('**/api/v1/**',async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname.replace('/api/v1','');
  const reply=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  if(p==='/session')return reply({username:'fixture',csrf:'csrf'});
  if(p==='/logout')return reply({});
  if(p==='/admin/models')return reply({items:models});
  if(p==='/admin/scenes')return reply({items:[]});
  if(p==='/admin/catalog')return reply({models,scenes:[]});
  if(p==='/admin/training-runs')return reply({items:[],total:0,counts:{queued:0,running:0,candidate:0,attention:0,cancelled:0},has_more:false});
  if(p==='/admin/model-candidates')return reply(catalogFail?{detail:'目录暂不可读'}:{limits:{max_upload_bytes:1024},items:[
   {path:'inbox/line1.pt',size:64,source:'inbox'},...models.map(m=>({path:'model://'+m.id,model_id:m.id,source:'registered',name:m.name,version:m.version,task:m.task,size:m.size}))
  ]},catalogFail?503:200);
  if(p.startsWith('/admin/model-imports/'))return reply(registered||{detail:'not complete'},registered?200:404);
  if(['/admin/models/upload','/admin/models/import'].includes(p)){
   requests.push({key:req.headers()['idempotency-key'],notes:req.headers()['x-model-notes'],url:url.toString(),body:p.endsWith('/import')?req.postDataJSON():null});
   if(mode==='413')return route.fulfill({status:413,contentType:'text/html',body:'<html>Too large</html>'});
   if(mode==='422')return reply({detail:'模型无法加载：权重版本不兼容'},422);
   if(mode==='network')return route.abort('failed');
   if(mode==='wait')await new Promise(resolve=>release=resolve);
   const r={...result,name:url.searchParams.get('name')||req.postDataJSON()?.name||result.name};
   models=[r];registered=r;
   if(mode==='lost-response')return route.fulfill({status:504,contentType:'text/html',body:'<html>Gateway Timeout</html>'});
   return reply(r,201);
  }
  return reply({detail:'unhandled '+p},404);
 });
 const open=async()=>{
  await page.goto(base+'/admin');await page.locator('#admin-content').waitFor({state:'visible'});
  await page.locator('#heading-import').click();await page.waitForFunction(()=>document.querySelector('#model-import-limit').textContent.includes('KB')||document.querySelector('#model-import-limit').textContent.includes('暂未'));
 };
 const choose=async(name='weights.pt',length=20)=>{
  await page.locator('#model-file').setInputFiles({name,mimeType:'application/octet-stream',buffer:Buffer.alloc(length,1)});
  await page.locator('#model-name').fill('服务器模型');await page.locator('#model-version').fill('2');
 };
 try{
  await open();assert.equal(await page.locator('[name=source][value=upload]').isChecked(),true);
  await choose('wrong.onnx');assert.equal(await page.locator('#import-submit').isDisabled(),true);
  await choose('empty.pt',0);assert.equal(await page.locator('#import-submit').isDisabled(),true);
  await choose('large.pt',2048);assert.equal(await page.locator('#import-submit').isDisabled(),true);
  assert.ok((await page.locator('#model-file-info').innerText()).includes('超过'));
  await choose();await page.locator('#model-notes').fill('工位版本说明');
  mode='413';await page.locator('#import-submit').click();
  await page.waitForFunction(()=>document.querySelector('#import-status').textContent.includes('网关上传限制'));
  mode='422';await page.locator('#import-submit').click();
  await page.waitForFunction(()=>document.querySelector('#import-status').textContent.includes('权重版本不兼容'));
  assert.equal(requests[0].key,requests[1].key);
  mode='network';await page.locator('#import-submit').click();
  await page.locator('#import-recover').waitFor({state:'visible'});
  await page.locator('#import-recover').click();
  await page.waitForFunction(()=>document.querySelector('#import-status').textContent.includes('尚无已完成'));
  mode='lost-response';await page.locator('#import-submit').click();
  await page.locator('#import-result').waitFor({state:'visible'});
  assert.ok((await page.locator('#import-result-description').innerText()).includes('已找到'));
  assert.equal(await page.locator('#import-result-classes script').count(),0);
  assert.equal(new Set(requests.map(r=>r.key)).size,1,'all retries retain the idempotency key');
  assert.equal(Buffer.from(requests[0].notes,'base64').toString('utf8'),'工位版本说明');
  await page.screenshot({path:path.join(output,'import-success.png'),fullPage:true});
  await page.locator('#import-train-model').click();
  await page.waitForFunction(()=>document.querySelector('#training-pretrained')?.value==='model://fixture-model');
  assert.equal(await page.locator('#training-task').inputValue(),'detect');
  await page.screenshot({path:path.join(output,'training-preselected.png'),fullPage:true});

  await open();await page.locator('[name=source][value=local]').check();
  assert.equal(await page.locator('#candidate-select option').count(),2,'registered assets are excluded from re-import picker');
  await page.locator('#candidate-select').selectOption('inbox/line1.pt');
  mode='success';await page.locator('#import-submit').click();await page.locator('#import-result').waitFor({state:'visible'});
  assert.equal(requests.at(-1).body.path,'inbox/line1.pt');
  await page.locator('#import-create-scene').click();await page.locator('#scene-dialog').waitFor({state:'visible'});
  assert.equal(await page.locator('#scene-model').inputValue(),'fixture-model');
  // A failed directory scan must still allow a client file upload.
  catalogFail=true;await open();await choose();
  assert.equal(await page.locator('#import-submit').isEnabled(),true);
  mode='wait';await page.locator('#import-submit').click();
  await page.waitForFunction(()=>document.querySelector('#import-form').getAttribute('aria-busy')==='true');
  assert.equal(await page.locator('#model-notes').isDisabled(),true);
  await page.locator('#import-dialog > .dialog-heading [data-close]').click();
  assert.equal(await page.locator('#import-dialog').isVisible(),true);
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(output,'upload-mobile.png'),fullPage:true});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.evaluate(()=>window.Workspace.logout());
  await page.locator('#locked-panel').waitFor({state:'visible'});release?.();
  await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('#import-dialog').isVisible(),false);
  assert.equal(await page.locator('#import-result-summary').innerText(),'');
  assert.equal(await page.locator('#model-file').inputValue(),'');
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify({passed:true,scope:'Browser interaction fixtures; API requests intercepted',checks:['client/server separation','size and extension','HTML 413','422 detail','network recovery','idempotent retry','metadata','server inbox','model-to-training','model-to-scene','directory scan failure','busy state','mobile','logout'],pageErrors:errors},null,2));
  console.log('Model import interactions passed: '+output);
 }finally{release?.();await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});

