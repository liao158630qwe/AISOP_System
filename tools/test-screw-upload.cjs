// Real native file input -> upload -> worker -> playback, without mocked APIs.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {resolvePython}=require('./python.cjs');
const playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),out=path.join(root,'outputs/screw-inspection-2026-10-17');
(async()=>{
  execFileSync(resolvePython(),['-c',`import json,cv2,pathlib
p=pathlib.Path('outputs/screw-inspection-2026-10-17');s=json.loads((p/'source.json').read_text());images=[]
for t in [50.53,50.76,51.03]:
 e=min(s['samples'],key=lambda e:abs(e['time']-t));images.append(cv2.imread(str(p/'frames'/e['file'])))
w=cv2.VideoWriter('.runtime/upload-native-check.avi',cv2.VideoWriter_fourcc(*'MJPG'),4,(1920,1080))
assert w.isOpened()
for image in images*3:w.write(image)
w.release()`],{cwd:root,windowsHide:true,stdio:'pipe'});
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true});
  const errors=[],requests=[];
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(r.method()==='POST'&&/\/api\/v1\/(media\?|analysis-runs$)/.test(r.url()))requests.push(r.url());});
    await page.goto('http://127.0.0.1:8000/');await page.locator('#server-account').waitFor();
    await page.locator('.toolbar [data-action=upload]').click();
    await page.locator('#modal-title').filter({hasText:'登录服务器'}).waitFor();
    const credentials=JSON.parse(fs.readFileSync(path.join(root,'.runtime/local-settings.json'),'utf8'));
    await page.locator('#login-user').fill(credentials.username);await page.locator('#login-password').fill(credentials.password);
    await page.locator('#screw-login button[type=submit]').click();
    await page.locator('#modal-title').filter({hasText:'上传视频检测'}).waitFor();
    assert.ok(await page.locator('#screw-upload-file').isVisible());
    assert.ok(await page.locator('#screw-upload-start').isDisabled());
    await page.locator('#modal [data-action=close-modal]').first().click();
    const reference=JSON.parse(fs.readFileSync(path.join(root,'.runtime/screw-reference-run.json'),'utf8'));
    await page.locator('[data-action=screw-history]').click();
    await page.locator(`[data-screw-run="${reference.id}"]`).click();
    await page.waitForFunction(()=>window.ScrewApp.state.result&&document.querySelector('#source-video').readyState>=2);
    await page.evaluate(()=>{
      document.querySelector('#source-video').pause();
      // Preserve the key cache while simulating cleared legacy renderer DOM.
      document.querySelector('#detection-results').replaceChildren();
      document.querySelector('#steps').innerHTML='<p>等待结果</p>';
      window.ScrewApp.render();
    });
    assert.equal(await page.locator('#detection-results tr').count(),12);
    assert.equal(await page.locator('#steps .screw-step').count(),12);
    await page.locator('.toolbar [data-action=upload]').click();
    const chooser=page.waitForEvent('filechooser');
    await page.locator('#screw-upload-file').click();
    await (await chooser).setFiles(path.join(root,'.runtime/upload-native-check.avi'));
    assert.match(await page.locator('#screw-upload-selection').textContent(),/upload-native-check\.avi/);
    assert.ok(await page.locator('#screw-upload-start').isEnabled());
    await page.screenshot({path:path.join(out,'upload-dialog.png'),fullPage:true});
    await page.locator('#screw-upload-start').click();
    await page.waitForFunction(()=>window.ScrewApp?.state.result?.source.title==='upload-native-check.avi'&&!window.ScrewApp.state.busy,null,{timeout:120000});
    assert.equal(requests.filter(u=>u.includes('/media?')).length,1);
    assert.equal(requests.filter(u=>u.endsWith('/analysis-runs')).length,1);
    assert.equal(await page.locator('#source-video').getAttribute('src'),`/api/v1/analysis-runs/${await page.evaluate(()=>window.ScrewApp.state.run)}/artifacts/video.webm`);
    const firstRun=await page.evaluate(()=>window.ScrewApp.state.run);
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    await page.evaluate(()=>{
      document.querySelector('#source-video').pause();
      const detail=document.querySelector('#detection-results'),steps=document.querySelector('#steps');
      const preservedRow=detail.children[0],preservedStep=steps.children[0];
      detail.children[3].lastElementChild.remove();
      steps.children[4].querySelector('.step-status').remove();
      window.ScrewApp.render();
      if(detail.children[0]!==preservedRow||steps.children[0]!==preservedStep)throw new Error('Unaffected nodes should be reused');
      if(detail.children[3].children.length!==6||!steps.children[4].querySelector('.step-status'))throw new Error('Damaged row/step not repaired');
    });
    await page.locator('.toolbar [data-action=upload]').click();
    await page.locator('#screw-upload-file').setInputFiles(path.join(root,'.runtime/upload-native-check.avi'));
    await page.locator('#screw-upload-start').click();
    await page.waitForFunction(id=>window.ScrewApp.state.run!==id&&window.ScrewApp.state.result?.id===window.ScrewApp.state.run&&!window.ScrewApp.state.busy,firstRun,{timeout:120000});
    assert.equal(requests.filter(u=>u.includes('/media?')).length,2);
    assert.equal(requests.filter(u=>u.endsWith('/analysis-runs')).length,2);
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    await page.evaluate(()=>document.querySelector('#source-video').pause());
    assert.equal(await page.locator('#detection-results tr').count(),12);
    assert.equal(await page.locator('#steps .screw-step').count(),12);
    await page.screenshot({path:path.join(out,'upload-recovered-result.png'),fullPage:true});
    await page.locator('.toolbar [data-action=upload]').click();
    await page.locator('#screw-upload-zone').dispatchEvent('drop',{dataTransfer:await page.evaluateHandle(()=>{const d=new DataTransfer();d.items.add(new File(['invalid'],'not-video.txt',{type:'text/plain'}));return d;})});
    assert.ok(await page.locator('#screw-upload-start').isDisabled());
    assert.match(await page.locator('#screw-upload-selection').textContent(),/请选择支持/);
    await page.locator('#modal [data-action=close-modal]').first().click();
    await page.evaluate(()=>{window.ScrewApp.state.busy=true;});
    await page.locator('.toolbar [data-action=upload]').click();
    assert.match(await page.locator('#server-status').textContent(),/当前任务正在处理/);
    await page.evaluate(()=>{window.ScrewApp.state.busy=false;});
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'upload-browser-result.json'),JSON.stringify({passed:true,runId:await page.evaluate(()=>window.ScrewApp.state.run),requests,errors},null,2));
    console.log('History -> upload -> second upload, stale-key/cleared/damaged node repair, native picker, real worker/playback, invalid input and busy feedback: passed');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
