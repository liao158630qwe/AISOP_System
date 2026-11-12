// Real browser/API: selectable isolated tasks, native upload and replay order.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),out=path.join(root,'outputs/sticker-inspection-2026-10-17');
const existingRun=process.argv[2];
(async()=>{
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true});const errors=[];
  try{
    const page=await browser.newPage({viewport:{width:1512,height:1100}});page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:8000/');await page.locator('#inspection-task').waitFor();
    await page.locator('#workspace-login').waitFor({state:'visible'});const login=JSON.parse(fs.readFileSync(path.join(root,'.runtime/local-settings.json'),'utf8'));
    await page.locator('#workspace-login [name=username]').fill(login.username);await page.locator('#workspace-login [name=password]').fill(login.password);
    await page.locator('#workspace-login button[type=submit]').click();await page.waitForFunction(()=>window.ScrewApp.state.session);
    await page.locator('#inspection-task').selectOption('sticker');
    await page.waitForFunction(()=>window.ScrewApp.state.task==='sticker'&&window.ScrewApp.state.model?.ready&&!window.ScrewApp.state.busy);
    const blank=()=>page.evaluate(()=>!window.ScrewApp.state.result&&!window.ScrewApp.state.run&&!document.querySelector('#source-video').getAttribute('src'));
    assert.ok(await blank());assert.ok(await page.locator('#sticker-sequence').isVisible());
    if(existingRun){
      await page.locator('[data-action=screw-history]').click();
      await page.locator(`[data-screw-run="${existingRun}"]`).click();
    }else{
      await page.locator('.toolbar [data-action=upload]').click();
      await page.locator('#screw-upload-file').setInputFiles('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4');
      await page.locator('#screw-upload-start').click();
      await page.waitForFunction(()=>window.ScrewApp.state.busy);
      assert.ok(await page.locator('#inspection-task').isDisabled());
    }
    await page.waitForFunction(()=>window.ScrewApp.state.result?.analysisType==='sticker'&&!window.ScrewApp.state.busy,null,{timeout:300000});
    const id=await page.evaluate(()=>window.ScrewApp.state.run);
    if(existingRun)assert.equal(id,existingRun);
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    const stages=[];
    for(const time of [16,28,40,48]){
      await page.evaluate(t=>{const v=document.querySelector('#source-video');v.pause();v.currentTime=t;},time);
      await page.waitForFunction(t=>!document.querySelector('#source-video').seeking&&Math.abs(document.querySelector('#source-video').currentTime-t)<.1,time);
      await page.waitForFunction(()=>{
        const s=window.ScrewApp.state;const f=s.result.frames.filter(f=>f.time<=document.querySelector('#source-video').currentTime).at(-1);
        return document.querySelector('#sticker-triangle-count').textContent===f.inspection.sequence.trianglesObserved+'/2';
      });
      stages.push(await page.evaluate(()=>({time:document.querySelector('#source-video').currentTime,
        triangles:document.querySelector('#sticker-triangle-count').textContent,
        rectangles:document.querySelector('#sticker-rectangle-count').textContent,boxes:window.ScrewApp.state.drawnBoxes.length})));
    }
    assert.equal(stages[0].triangles,'0/2');assert.equal(stages.at(-1).triangles,'2/2');
    await page.locator('#finish-detection').click();await page.locator('#modal-title').filter({hasText:'本次检测最终判定'}).waitFor();
    const final=await page.evaluate(()=>window.ScrewApp.state.final);assert.equal(final.compliance.status,'PASS');assert.equal(final.targets.length,8);
    await page.locator('#modal [data-action=close-modal]').last().click();
    await page.screenshot({path:path.join(out,'sticker-dashboard.png'),fullPage:true});
    await page.locator('#inspection-task').selectOption('screw');
    await page.waitForFunction(()=>window.ScrewApp.state.task==='screw'&&window.ScrewApp.state.model?.ready&&!window.ScrewApp.state.busy);
    assert.ok(await blank());assert.ok(await page.locator('#sticker-sequence').isHidden());
    await page.locator('[data-action=screw-history]').click();
    await page.locator('#screw-history').waitFor();await page.waitForFunction(()=>!document.querySelector('#screw-history').textContent.includes('正在读取'));
    assert.equal(await page.locator(`[data-screw-run="${id}"]`).count(),0);
    await page.locator('#modal [data-action=close-modal]').first().click();
    await page.locator('#inspection-task').selectOption('sticker');
    await page.waitForFunction(()=>window.ScrewApp.state.task==='sticker'&&window.ScrewApp.state.model?.ready&&!window.ScrewApp.state.busy);
    await page.reload();await page.waitForFunction(()=>window.ScrewApp.state.task==='sticker'&&window.ScrewApp.state.model?.ready);
    assert.ok(await blank());
    await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'task-ui-result.json'),JSON.stringify({run:id,stages,final,errors},null,2));
    console.log(JSON.stringify({run:id,verdict:final.compliance.status,stages,errors}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
