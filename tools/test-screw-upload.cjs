// Real native file input -> upload -> worker -> playback, without mocked APIs.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const playwright=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base=process.env.SCREW_UPLOAD_TEST_BASE,out=process.env.SCREW_UPLOAD_TEST_OUTPUT,fixture=process.env.SCREW_UPLOAD_TEST_VIDEO;
const credentials={username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD};
(async()=>{
  assert.ok(base&&out&&fixture&&credentials.password,'Run npm run test:upload to create isolated services and the video fixture');
  const browser=await playwright.chromium.launch({channel:'msedge',headless:true});
  const errors=[],requests=[];let page;
  try{
    page=await browser.newPage({viewport:{width:1440,height:1000}});
    const waitForRun=async previous=>{
      await page.waitForFunction(previous=>{
        const s=window.ScrewApp.state;
        return s.run&&s.run!==previous&&!s.busy&&(s.result?.id===s.run||s.runStatus==='FAILED');
      },previous,{timeout:120000});
      const data=await page.evaluate(()=>({run:window.ScrewApp.state.run,result:window.ScrewApp.state.result?.id,
        title:window.ScrewApp.state.result?.source.title,status:document.querySelector('#server-status').textContent}));
      assert.equal(data.result,data.run,data.status);
      assert.equal(data.title,'upload-native-check.avi');
      await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
      return data.run;
    };
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(r.method()==='POST'&&/\/api\/v1\/(media\?|analysis-runs$)/.test(r.url()))requests.push(r.url());});
    await page.goto(base+'/?task=screw');
    await page.waitForFunction(()=>window.ScrewApp?.state.model?.ready&&!window.ScrewApp.state.busy);
    const targetCount=await page.evaluate(()=>window.ScrewApp.state.model.targets.length);
    assert.ok(targetCount>=5,'The fixture must have enough targets to exercise row repair');
    await page.locator('.toolbar [data-action=upload]').click();
    await page.locator('#modal-title').filter({hasText:'登录服务器'}).waitFor();
    await page.locator('#login-user').fill(credentials.username);await page.locator('#login-password').fill(credentials.password);
    await page.locator('#screw-login button[type=submit]').click();
    await page.locator('#modal-title').filter({hasText:'上传视频检测'}).waitFor();
    assert.ok(await page.locator('#screw-upload-file').isVisible());
    assert.ok(await page.locator('#screw-upload-start').isDisabled());
    await page.locator('#screw-upload-file').setInputFiles(fixture);
    await page.locator('#screw-upload-start').click();
    const reference=await waitForRun(null);
    assert.equal(requests.filter(u=>u.includes('/media?')).length,1);
    assert.equal(requests.filter(u=>u.endsWith('/analysis-runs')).length,1);
    await page.locator('[data-action=close-modal]').evaluateAll(nodes=>nodes.forEach(node=>{if(node.offsetParent)node.click();}));
    requests.length=0;
    await page.locator('[data-action=screw-history]').click();
    await page.locator(`[data-screw-run="${reference}"]`).click();
    await page.waitForFunction(()=>window.ScrewApp.state.result&&document.querySelector('#source-video').readyState>=2);
    await page.evaluate(()=>{
      document.querySelector('#source-video').pause();
      // Preserve the key cache while simulating cleared legacy renderer DOM.
      document.querySelector('#detection-results').replaceChildren();
      document.querySelector('#steps').innerHTML='<p>等待结果</p>';
      window.ScrewApp.render();
    });
    assert.equal(await page.locator('#detection-results tr').count(),targetCount);
    assert.equal(await page.locator('#steps .screw-step').count(),targetCount);
    await page.locator('.toolbar [data-action=upload]').click();
    const chooser=page.waitForEvent('filechooser');
    await page.locator('#screw-upload-file').click();
    await (await chooser).setFiles(fixture);
    assert.match(await page.locator('#screw-upload-selection').textContent(),/upload-native-check\.avi/);
    assert.ok(await page.locator('#screw-upload-start').isEnabled());
    await page.screenshot({path:path.join(out,'upload-dialog.png'),fullPage:true});
    await page.locator('#screw-upload-start').click();
    const firstRun=await waitForRun(reference);
    assert.equal(requests.filter(u=>u.includes('/media?')).length,1);
    assert.equal(requests.filter(u=>u.endsWith('/analysis-runs')).length,1);
    assert.equal(await page.locator('#source-video').getAttribute('src'),`/api/v1/analysis-runs/${await page.evaluate(()=>window.ScrewApp.state.run)}/artifacts/video.webm`);
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
    await page.locator('#screw-upload-file').setInputFiles(fixture);
    await page.locator('#screw-upload-start').click();
    const secondRun=await waitForRun(firstRun);
    assert.equal(requests.filter(u=>u.includes('/media?')).length,2);
    assert.equal(requests.filter(u=>u.endsWith('/analysis-runs')).length,2);
    await page.waitForFunction(()=>document.querySelector('#source-video').readyState>=2);
    await page.evaluate(()=>document.querySelector('#source-video').pause());
    assert.equal(await page.locator('#detection-results tr').count(),targetCount);
    assert.equal(await page.locator('#steps .screw-step').count(),targetCount);
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
    fs.writeFileSync(path.join(out,'upload-browser-result.json'),JSON.stringify({passed:true,recordedAt:new Date().toISOString(),
      input:'Synthetic video repeating the screw reference image; workflow regression, not accuracy validation',
      isolatedDatabase:true,runIds:[reference,firstRun,secondRun],requests,errors},null,2));
    console.log('History -> upload -> second upload, stale-key/cleared/damaged node repair, native picker, real worker/playback, invalid input and busy feedback: passed');
  }catch(error){if(page)await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});throw error;}
  finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
