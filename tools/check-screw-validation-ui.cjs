// Open an explicitly selected independent video run in the original dashboard.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..');
const [runId,output,expectedVerdict,seek='25']=process.argv.slice(2);
if(!runId||!output)throw new Error('Usage: node tools/check-screw-validation-ui.cjs RUN_ID OUTPUT_DIR');
(async()=>{
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true});
  const errors=[];
  try{
    const page=await browser.newPage({viewport:{width:1512,height:1100}});
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:8000/');
    await page.locator('#workspace-login').waitFor({state:'visible'});
    const credentials=JSON.parse(fs.readFileSync(path.join(root,'.runtime/local-settings.json'),'utf8'));
    await page.locator('#workspace-login [name=username]').fill(credentials.username);
    await page.locator('#workspace-login [name=password]').fill(credentials.password);
    await page.locator('#workspace-login button[type=submit]').click();
    await page.waitForFunction(()=>window.ScrewApp?.state.session);
    assert.ok(await page.evaluate(()=>window.ScrewApp.state.result===null));
    await page.locator('[data-action=screw-history]').click();
    await page.locator(`[data-screw-run="${runId}"]`).click();
    await page.waitForFunction(id=>window.ScrewApp?.state.run===id&&document.querySelector('#source-video').readyState>=2,runId);
    await page.evaluate(t=>{document.querySelector('#source-video').pause();document.querySelector('#source-video').currentTime=t;},Number(seek));
    await page.waitForFunction(t=>!document.querySelector('#source-video').seeking&&Math.abs(document.querySelector('#source-video').currentTime-t)<.1,Number(seek));
    await page.waitForFunction(()=>document.querySelector('#detection-results').children.length===window.ScrewApp.state.result.summary.expected&&
      document.querySelector('#steps').children.length===window.ScrewApp.state.result.summary.expected);
    const loaded=await page.evaluate(()=>({id:window.ScrewApp.state.run,title:window.ScrewApp.state.result.source.title,
      boxes:window.ScrewApp.state.drawnBoxes.length,expected:window.ScrewApp.state.result.summary.expected,rows:document.querySelector('#detection-results').children.length,
      steps:document.querySelector('#steps').children.length}));
    assert.equal(loaded.id,runId);assert.equal(loaded.rows,loaded.expected);assert.equal(loaded.steps,loaded.expected);
    await page.evaluate(()=>{
      const frame=window.ScrewApp.state.result.frames.filter(f=>f.time<=document.querySelector('#source-video').currentTime).at(-1);
      for(const row of document.querySelectorAll('#detection-results tr')){
        const target=frame.inspection.targets.find(t=>t.id===row.dataset.screwTarget);
        if(!target||row.children[3].textContent!==target.name)throw new Error('Result row uses a name from a different view');
      }
      if(document.querySelector('.screw-focus-strip'))throw new Error('Homepage close-up should be removed');
    });
    await page.locator('#finish-detection').click();
    await page.locator('#modal-title').filter({hasText:'本次检测最终判定'}).waitFor();
    const verdict=await page.evaluate(()=>window.ScrewApp.state.final.compliance);
    if(expectedVerdict)assert.equal(verdict.status,expectedVerdict);
    assert.equal(await page.locator('#screw-verdict').getAttribute('data-verdict'),verdict.status);
    await page.locator('#modal [data-action=close-modal]').last().click();
    await page.screenshot({path:path.join(output,'dashboard-validation.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    const result={...loaded,verdict,errors};
    fs.writeFileSync(path.join(output,'ui-validation.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
