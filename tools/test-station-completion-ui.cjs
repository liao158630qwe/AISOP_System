// Real dashboard/session/stream lifecycle; inference and final verdicts are explicit UI fixtures.
// Run: node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-completion-ui.cjs
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.PILOT_TEST_BASE, output = process.env.PILOT_TEST_OUTPUT;
if (!base || !output) throw new Error('Run with test-pilot-ui.py --registration-paused');
const reason = '全部 9 处操作位置及 2 处预装件均已取得累计视觉确认';
const names = ['长件左下端','外侧长件中部','下侧三角支座','灯板下端预装螺丝','透明支架下端','内侧支座左端','灯板上端预装螺丝','透明支架上端','内侧长件连接端','顶部连接件','内侧长件末端'];
(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
  const context = await browser.newContext({viewport:{width:1440,height:1000},permissions:['camera']});
  const page = await context.newPage(), errors = [], rounds = new Map();
  let mode = 'pass', current;
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.testCameraTracks = []; window.testWorkers = {created:0,closed:0};
    const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async options => {
      const stream = await getUserMedia(options); window.testCameraTracks.push(...stream.getTracks()); return stream;
    };
    const FrameWorker = window.Worker;
    window.Worker = class extends FrameWorker {
      constructor(...args) {super(...args); window.testWorkers.created++;}
      terminate() {window.testWorkers.closed++; return super.terminate();}
    };
  });
  await page.route('**/api/v1/station-streams?*', async route => {
    const response = await route.fetch(), identity = await response.json();
    assert.equal(response.status(),201);
    current = {identity,mode,frames:0,finishes:0,releases:0}; rounds.set(identity.streamId,current);
    await route.fulfill({response,json:identity});
  });
  await page.route('**/api/v1/station-streams/*/frames', async route => {
    const row = rounds.get(route.request().url().split('/').at(-2)), headers = route.request().headers();
    row.frames++;
    const complete = row.frames >= 3, sticker = row.identity.stationId === 'station-6';
    const targets = names.map((name,i) => ({id:'T'+i,name,preassembled:i===3||i===6,
      state:complete || i===0 ? sticker ? 'ACTION_DONE' : 'PRESENT' : 'UNKNOWN',
      score:.95,confirmedAt:0,sequenceReason:reason}));
    let status = complete ? 'PASS' : 'PENDING';
    if (row.mode === 'review' && complete) status = 'REVIEW';
    if (row.mode === 'fail' && complete) {status = 'FAIL'; targets.at(-1).state = 'EMPTY';}
    if (row.mode === 'count-only' && complete) status = 'PENDING';
    row.latest = {...row.identity,frameSeq:Number(headers['x-frame-seq']),targets,aggregateTargets:targets,
      serverProcessingMs:6.5,
      compliance:{status,label:{PASS:'合规',PENDING:'检测中',FAIL:'不合规',REVIEW:'采样缺口待复核'}[status],reason},
      // A server-confirmed visual PASS can retain historical sampling gaps.
      stationEvidenceGaps:['pass','review'].includes(row.mode) ? [{reason:'采样间隔超限'}] : []};
    if (row.mode.startsWith('visual-')) {
      row.latest.sequence = {verificationMode:'visual-cumulative',configured:true,status:complete?'PASS':'PENDING',
        positionsExpected:11,positionsConfirmed:complete?11:0,operationsExpected:9,operationsCompleted:complete?9:0,
        preassembledExpected:2,preassembledConfirmed:complete?2:0};
      if (row.mode==='visual-review') row.latest.compliance={status:'REVIEW',label:'待复核',reason:'服务端尚未通过'};
      if (row.mode==='visual-incomplete') row.latest.sequence.operationsCompleted=8;
    }
    const result = row.mode === 'wrong-camera' ? {...row.latest,cameraId:'wrong-camera'} : row.latest;
    await route.fulfill({json:result});
  });
  await page.route('**/api/v1/station-streams/*/finish', async route => {
    const row = rounds.get(route.request().url().split('/').at(-2)); row.finishes++;
    if (row.mode === 'archive-failure' && row.finishes === 1) return route.fulfill({status:503,json:{message:'归档暂不可用'}});
    const final = {...row.latest,final:true,archived:true};
    if (row.mode === 'downgraded') {
      final.originalCompliance = {...final.compliance};
      final.compliance = {status:'REVIEW',label:'待复核',reason:'结束时证据待复核'};
      final.evidenceGaps = [{start:6.572839,end:10.466893,reason:'连续有效采样间隔超过3秒'}];
      final.stationEvidenceGaps = [{start:6.572839,end:10.466893,reason:'工位有效采样间隔超限'}];
    }
    await route.fulfill({json:final});
  });
  await page.route('**/api/v1/station-streams/*', async route => {
    if (route.request().method() !== 'DELETE') return route.fallback();
    const row = rounds.get(route.request().url().split('/').at(-1)); row.releases++;
    if (row.mode === 'release-failure' && row.releases === 1) return route.fulfill({status:503,json:{message:'释放暂不可用'}});
    await route.continue();
  });
  const idle = () => page.waitForFunction(() => StationDetection.state.archived && !StationDetection.state.stream && !StationDetection.state.busy);
  const close = () => page.locator('#modal [data-action=close-modal]').first().click();
  async function start() {
    const previous=current?.identity.streamId;
    await page.locator('#sd-start').click();
    await page.waitForFunction(previous => StationDetection.state.frames > 0 &&
      !!StationDetection.state.frame?.streamId && StationDetection.state.frame.streamId !== previous, previous);
  }
  async function noMoreFrames(row) {
    const count = row.frames; await page.waitForTimeout(650);
    assert.equal(row.frames,count,'A completed or failed-finalization round must not submit more inference');
  }
  try {
    const session = await (await context.request.post(base+'/api/v1/login',{data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}})).json();
    for (let n=1;n<=6;n++) {
      const response = await context.request.put(base+'/api/v1/admin/terminals/station-'+n,{headers:{'X-CSRF-Token':session.csrf},data:{
        workstationId:'completion-pc-'+n,cameraId:'completion-camera-'+n,analysisType:n===6?'sticker':'screw',
        sampleFps:10,maxSampleGapSeconds:1,source:'usb',enabled:true,revision:0}});
      assert.equal(response.status(),200);
    }
    await page.goto(base+'/?task=station1');
    for (let n=1;n<=6;n++) {
      if (n>1) await page.locator('#inspection-task').selectOption('station'+n);
      await page.waitForFunction(n => StationDetection.state.station === 'station-'+n && StationDetection.state.connected && !StationDetection.state.busy,n);
      assert.equal(await page.evaluate(() => StationDetection.state.timing), null, 'A different station must not reuse the previous station timing');
      if (n>1) {
        await page.locator('#sd-source').selectOption(n===2?'replay':'image');
        await page.locator('#sd-file').setInputFiles(n===2?'assets/reference.mp4':path.join(output,'holes.jpg'));
        await page.waitForFunction(() => StationDetection.state.ready);
      }
      await start();
      if (n===2) {
        await page.waitForFunction(() => StationDetection.state.frames>=5);
        assert.equal(current.finishes,0,'A live PASS must not finish a replay before the video ends');
        assert.equal(await page.locator('#workflow-status').textContent(),'检测中');
        await page.locator('#sd-finish').click();
      }
      await idle();
      if (n===2) assert.ok(current.frames>=5); else assert.equal(current.frames,3);
      assert.equal(current.finishes,1); assert.equal(current.releases,1);
      assert.equal(await page.locator('#modal-title').textContent(),'工位'+n+' · 检测合规');
      assert.match(await page.locator('#modal-body').textContent(),/已停止并归档/);
      assert.equal(await page.locator('#sd-completion').isVisible(),true);
      assert.equal(await page.locator('#sd-next').isEnabled(),true);
      assert.equal(await page.locator('#steps .step-name').count(),11);
      assert.equal(await page.locator('#steps .step-detail').count(),0);
      assert.equal(await page.locator('#workflow-description').isVisible(),false);
      assert.equal(await page.locator('#steps').textContent().then(text=>text.includes(reason)),false);
      assert.equal(await page.evaluate(() => StationDetection.state.running),false);
      assert.equal(await page.evaluate(() => document.querySelector('#sd-video').paused),true);
      assert.equal(await page.locator('#sd-start').isEnabled(),true);
      assert.deepEqual(await page.evaluate(() => testWorkers),{created:n,closed:n});
      if (n===1) {
        assert.ok(await page.evaluate(() => testCameraTracks.length > 0 && testCameraTracks.every(track => track.readyState==='ended')));
        await page.screenshot({path:path.join(output,'station-compliant-dialog.png'),fullPage:true});
      }
      await noMoreFrames(current); await close();
      // Repainting unchanged results preserves the existing list nodes and does not reopen the popup.
      assert.ok(await page.evaluate(() => {
        const selectors=['#steps','#detection-results','#alert-list'];
        const children=selectors.map(selector=>document.querySelector(selector).firstElementChild);
        for (let i=0;i<10;i++) StationDetection.render();
        return selectors.every((selector,i)=>document.querySelector(selector).firstElementChild===children[i]);
      }));
      assert.equal(await page.locator('#modal').isVisible(),false);
      if (n===1) {
        await page.locator('.workflow-panel').screenshot({path:path.join(output,'station-compact-panel.png')});
        await page.locator('.toolbar [data-action="system"]').click();
        await page.locator('#modal[open]').waitFor();
        const details = await page.locator('#modal-body').innerText();
        assert.match(details, /目标采样\s+10\s+fps/);
        assert.match(details, /目标间隔 100\.0 ms/);
        assert.match(details, /采图与缩放\s+\d/);
        assert.match(details, /JPEG 编码\s+\d/);
        assert.match(details, /请求往返\s+\d/);
        assert.match(details, /其中服务器处理\s+已包含在请求往返内\s+6\.5 ms/);
        await page.screenshot({path:path.join(output,'station-timing-details.png'),fullPage:true});
        await close();
      }
    }
    // Both completion actions create a fresh round and stop again after PASS.
    const previous = current.identity.streamId;
    await page.locator('#sd-next').click(); await idle();
    assert.notEqual(current.identity.streamId,previous);
    assert.equal(current.frames,3);
    const next = current.identity.streamId;
    await page.locator('#sd-modal-next').click(); await idle();
    assert.notEqual(current.identity.streamId,next);
    assert.equal(current.frames,3); await close();
    // Counts alone, explicit FAIL and a server REVIEW must never auto-pass.
    for (mode of ['count-only','review']) {
      await start(); await page.waitForFunction(() => StationDetection.state.frames >= 5);
      assert.equal(current.finishes,0); assert.equal(await page.locator('#modal').isVisible(),false);
      assert.equal(await page.evaluate(() => StationDetection.state.running),true);
      if (mode !== 'fail') assert.equal(await page.locator('#step-counter').textContent(),'11/11 处');
      await page.locator('#sd-stop').click(); await page.waitForFunction(() => !StationDetection.state.stream && !StationDetection.state.busy);
      await page.locator('#sd-file').setInputFiles(path.join(output,'holes.jpg')); await page.waitForFunction(() => StationDetection.state.ready);
    }
    mode='fail'; await start(); await idle();
    assert.equal(await page.evaluate(()=>StationDetection.state.final.compliance.status),'FAIL');
    assert.equal(await page.locator('#modal-title').textContent(),'工位6 · 检测不合规');
    assert.equal(await page.locator('#sd-modal-review').isVisible(),true);
    await noMoreFrames(current); await close();
    // The archived verdict is authoritative even if a live PASS initiated finalization.
    mode='downgraded'; await start(); await idle();
    assert.equal(await page.locator('#workflow-status').textContent(),'待复核');
    assert.match(await page.locator('#sd-completion p').textContent(),/结束时证据待复核/);
    assert.match(await page.locator('.sd-evidence-gap').textContent(),/模型原判定：合规.*6\.57～10\.47 秒（3\.89 秒）/);
    assert.equal((await page.locator('.sd-evidence-gap').textContent()).match(/6\.57～10\.47/g).length,1,'Both server gap registries describe the same missing interval');
    await page.screenshot({path:path.join(output,'station-sampling-gap-dialog.png'),fullPage:true});
    assert.equal(await page.locator('#modal-title').textContent(),'工位6 · 检测证据不足'); await close();
    await noMoreFrames(current);
    for (mode of ['archive-failure','release-failure']) {
      await start(); await page.waitForFunction(() => !!StationDetection.state.error && !StationDetection.state.busy);
      assert.equal(await page.locator('#sd-start').isDisabled(),true);
      assert.equal(await page.locator('#sd-finish').isEnabled(),true);
      assert.equal(await page.locator('#modal').isVisible(),false);
      await noMoreFrames(current);
      await page.locator('#sd-finish').click(); await idle();
      assert.equal(current.finishes,mode==='archive-failure'?2:1,'Release retry must not repeat archival');
      assert.equal(await page.locator('#modal-title').textContent(),'工位6 · 检测合规'); await close();
    }
    // Identity-rejected frames deliberately do not increment the accepted frame count.
    mode='wrong-camera'; await page.locator('#sd-start').click();
    await page.waitForFunction(() => !StationDetection.state.running && !!StationDetection.state.error);
    assert.match(await page.locator('#server-status').textContent(),/身份不匹配/);
    assert.equal(current.finishes,0);
    await page.locator('#sd-stop').click(); await page.waitForFunction(() => !StationDetection.state.stream && !StationDetection.state.busy);
    mode='pass'; await page.locator('#sd-file').setInputFiles(path.join(output,'holes.jpg'));
    await page.waitForFunction(() => StationDetection.state.ready);
    await page.keyboard.press('F8'); await idle();
    assert.equal(await page.locator('#modal-title').textContent(),'工位6 · 检测合规'); await close();
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(output,'station-completion-mobile.png'),fullPage:true});
    // PASS and FAIL remain provisional during video playback. Exercise the
    // browser's ended handler explicitly; inference and archive are fixtures.
    await page.setViewportSize({width:1440,height:1000});
    for (mode of ['pass','fail']) {
      await page.locator('#sd-source').selectOption('replay');
      await page.locator('#sd-file').setInputFiles('assets/reference.mp4');
      await page.waitForFunction(() => StationDetection.state.ready);
      await start(); await page.waitForFunction(() => StationDetection.state.frames>=5);
      assert.equal(current.finishes,0,'An intermediate verdict must not archive a replay');
      assert.equal(await page.evaluate(() => StationDetection.state.running),true);
      assert.equal(await page.locator('#modal').isVisible(),false);
      assert.equal(await page.locator('#workflow-status').textContent(),'检测中');
      assert.equal(await page.locator('#workflow-status').getAttribute('data-verdict'),'PENDING');
      if (mode==='fail') assert.match(await page.locator('#alert-list').textContent(),/过程异常提示/);
      await page.locator('#sd-video').evaluate(video => video.dispatchEvent(new Event('ended')));
      await idle();
      assert.equal(current.finishes,1); assert.equal(current.releases,1);
      assert.equal(await page.evaluate(() => StationDetection.state.final.compliance.status),mode==='pass'?'PASS':'FAIL');
      await noMoreFrames(current); await close();
    }
    // A visual coverage replay follows camera completion; counts or mode alone cannot pass.
    for (const n of [1,6]) {
      await page.locator('#inspection-task').selectOption('station'+n);
      await page.waitForFunction(n=>StationDetection.state.station==='station-'+n&&StationDetection.state.connected&&!StationDetection.state.busy,n);
      await page.locator('#sd-source').selectOption('replay');
      await page.locator('#sd-file').setInputFiles('assets/reference.mp4');
      await page.waitForFunction(()=>StationDetection.state.ready);
      mode='visual-pass'; await start(); await idle();
      assert.equal(current.frames,3); assert.equal(current.finishes,1); assert.equal(current.releases,1);
      assert.equal(await page.locator('#modal-title').textContent(),'工位'+n+' · 检测合规');
      await noMoreFrames(current); await close();
    }
    for (mode of ['visual-review','visual-incomplete']) {
      await start(); await page.waitForFunction(()=>StationDetection.state.frames>=5);
      assert.equal(current.finishes,0);assert.equal(await page.locator('#modal').isVisible(),false);
      await page.locator('#sd-finish').click();await idle();await close();
    }
    // The standalone "螺丝安装检测 / 当前帧识别" camera path also stops on PASS.
    await page.setViewportSize({width:1440,height:1000});
    const cameraRounds = new Map(); let cameraMode = 'pass';
    const modelTargets = names.map((name,i)=>({id:'T'+i,name,preassembled:i===3||i===6,point:[100+i*30,100]}));
    await page.route('**/api/v1/screw-model', route=>route.fulfill({json:{ready:true,modelName:'completion fixture',targets:modelTargets}}));
    await page.route('**/api/v1/realtime/frame?*', async route=>{
      const headers=route.request().headers(), id=headers['x-stream-id'];
      const row=cameraRounds.get(id)||{frames:0,finishes:0,releases:0}; cameraRounds.set(id,row); row.frames++;
      const done=row.frames>=3, status=done?'PASS':'PENDING';
      const targets=modelTargets.map(t=>({...t,state:done?'PRESENT':'UNKNOWN',score:.95,
        sequenceState:done?t.preassembled?'PREASSEMBLED_OK':'COMPLETED':'PENDING'}));
      const order=modelTargets.filter(t=>!t.preassembled).map(t=>t.id);
      const sequence={configured:true,verificationMode:'visual-cumulative',status,reason,order,
        completed:done?order:[],pending:done?[]:order,events:[],empty:[],early:[],
        preassembled:Object.fromEntries(modelTargets.filter(t=>t.preassembled).map(t=>[t.id,done?'PREASSEMBLED_OK':'PREASSEMBLED_UNKNOWN'])),
        positionsExpected:11,positionsConfirmed:done?11:0,operationsExpected:9,operationsCompleted:done?9:0,
        preassembledExpected:2,preassembledConfirmed:done?2:0};
      row.latest={analysisType:'screw',frameId:headers['x-frame-id'],frameTime:Number(headers['x-frame-time']),
        targets,aggregateTargets:targets,inspection:{registered:true,targets,displayTargets:[],sequence},sequence,people:[],
        compliance:{status:done&&cameraMode==='review'?'REVIEW':status,label:done?'合规':'检测中',reason},
        evidenceGaps:[{start:0,end:4,reason:'测试历史采样中断',source:'server'}]};
      await route.fulfill({json:row.latest});
    });
    await page.route('**/api/v1/realtime/streams/**',async route=>{
      const url=new URL(route.request().url()),parts=url.pathname.split('/'),finishing=parts.at(-1)==='finish';
      const row=cameraRounds.get(parts.at(finishing?-2:-1));
      if(finishing){row.finishes++;return route.fulfill({json:{...row.latest,final:true}});}
      row.releases++; await route.fulfill({json:{released:true}});
    });
    await page.goto(base+'/?task=screw');
    await page.waitForFunction(()=>ScrewApp.state.session&&ScrewApp.state.model?.ready);
    const cameraIdle=()=>page.waitForFunction(()=>ScrewApp.state.final&&!ScrewApp.state.busy&&!ScrewApp.state.streamId);
    await page.locator('#start-live').click(); await cameraIdle();
    assert.equal(await page.locator('#modal-title').textContent(),'检测合规');
    assert.equal(await page.evaluate(()=>ScrewApp.state.final.compliance.status),'PASS');
    assert.ok(await page.evaluate(()=>testCameraTracks.every(t=>t.readyState==='ended')));
    let count=[...cameraRounds.values()].reduce((n,row)=>n+row.frames,0);
    await page.waitForTimeout(650);
    assert.equal([...cameraRounds.values()].reduce((n,row)=>n+row.frames,0),count);
    await page.locator('#screw-next').click(); await cameraIdle();
    assert.equal([...cameraRounds.values()].filter(row=>row.finishes===1&&row.releases===1).length,2);
    await close(); cameraMode='review';
    await page.locator('#start-live').click();
    await page.waitForFunction(()=>ScrewApp.state.frames.length>=5);
    assert.equal(await page.evaluate(()=>ScrewApp.state.running),true);
    assert.equal(await page.locator('#modal').isVisible(),false);
    await page.locator('#finish-detection').click(); await cameraIdle();
    assert.equal(await page.evaluate(()=>ScrewApp.state.final.compliance.status),'REVIEW');
    await close();
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'station-completion.json'),JSON.stringify({passed:true,inferenceMocked:true,archiveMocked:true,
      rounds:[...rounds.values()].map(({identity,mode,frames,finishes,releases})=>({station:identity.stationId,mode,frames,finishes,releases})),
      standaloneCameraRounds:[...cameraRounds.values()].map(({frames,finishes,releases})=>({frames,finishes,releases})),errors},null,2));
    console.log('Six stations and standalone screw camera: automatic completion, resource release, retries, server verdict guards and next round passed.');
  } catch (error) {
    await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}); throw error;
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode=1;});
