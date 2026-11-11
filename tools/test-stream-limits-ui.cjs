// Real session/round/archive endpoints; frames are fixtures so no camera/GPU is required.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.PILOT_TEST_BASE, output = process.env.PILOT_TEST_OUTPUT;
if (!base || !output) throw new Error('Run with tools/test-pilot-ui.py --registration-paused --browser-script tools/test-stream-limits-ui.cjs');

(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true,
    args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
  const context = await browser.newContext({viewport:{width:1440,height:1000},permissions:['camera']});
  const page = await context.newPage(), errors = [], rounds = new Map();
  let mode = 'empty', current;
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.limitTestTracks = [];
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async options => {
      const stream = await original(options); window.limitTestTracks.push(...stream.getTracks()); return stream;
    };
  });
  await page.route('**/api/v1/station-streams?*', async route => {
    const response = await route.fetch(), identity = await response.json();
    assert.equal(response.status(), 201);
    assert.deepEqual(identity.streamLimits, {maxDurationSeconds:600,noTargetTimeoutSeconds:60});
    current = {identity,mode,frames:0}; rounds.set(identity.streamId,current);
    await route.fulfill({response,json:identity});
  });
  await page.route('**/api/v1/station-streams/*/frames', async route => {
    const row = rounds.get(route.request().url().split('/').at(-2)); row.frames++;
    if (row.mode === 'empty' && row.frames >= 3) return route.fulfill({status:410,
      json:{message:'连续 60 秒未检测到目标，已自动停止；请放置工件并确认画面后重新开始'}});
    await route.fulfill({json:{...row.identity,frameSeq:Number(route.request().headers()['x-frame-seq']),
      people:[],compliance:{status:'REVIEW',label:'等待目标',reason:'当前未检测到目标'}}});
  });
  const idle = () => page.waitForFunction(() => !StationDetection.state.running && !StationDetection.state.stream && !StationDetection.state.busy);
  try {
    const login = await context.request.post(base+'/api/v1/login',
      {data:{username:process.env.APP_USERNAME,password:process.env.APP_PASSWORD}});
    const {csrf} = await login.json();
    const bound = await context.request.put(base+'/api/v1/admin/terminals/station-1',
      {headers:{'X-CSRF-Token':csrf},data:{workstationId:'limit-ui-pc',cameraId:'limit-ui-camera',analysisType:'pose',sampleFps:10}});
    assert.equal(bound.status(),200);
    await page.goto(base+'/?task=station1');
    await page.waitForFunction(() => StationDetection.state.connected && !StationDetection.state.busy);
    for (const nextMode of ['empty','maximum']) {
      mode = nextMode;
      const previous = current?.identity.streamId;
      await page.locator('#sd-start').click();
      await page.waitForFunction(previous => StationDetection.state.running && StationDetection.state.frames > 0 &&
        StationDetection.state.stream?.streamId !== previous, previous);
      if (mode === 'maximum') {
        // Simulate a stuck camera, then advance only this round's monotonic origin.
        await page.evaluate(() => {document.querySelector('#sd-video').pause(); StationDetection.state.started -= 601000;});
      }
      await idle();
      const reason = await page.locator('#server-status').textContent();
      assert.match(reason, mode === 'empty' ? /连续 60 秒/ : /最长 600 秒/);
      assert.ok(await page.evaluate(() => limitTestTracks.every(track => track.readyState === 'ended')));
      assert.equal(await page.evaluate(() => StationDetection.state.final),null);
      assert.equal(await page.locator('#sd-start').isEnabled(),true);
      const count = current.frames;
      await page.waitForTimeout(700);
      assert.equal(current.frames,count,'Stopped cameras must stop sending frames');
      const records = await (await context.request.get(base+'/api/v1/stations/station-1/inspections')).json();
      const record = records.items.find(row => row.streamId === current.identity.streamId);
      assert.equal(record.status,'INTERRUPTED'); assert.equal(record.result,null);
      assert.match(record.reason,/自动停止/);
    }
    assert.equal(rounds.size,2,'A stopped round can be followed by a new round');
    assert.deepEqual(errors,[]);
    await page.screenshot({path:path.join(output,'stream-limits.png'),fullPage:true});
    fs.writeFileSync(path.join(output,'stream-limits.json'),JSON.stringify({passed:true,
      frameInferenceMocked:true,rounds:[...rounds.values()],errors},null,2));
    console.log('Idle timeout and maximum duration: camera release, no more frames, interruption reason and next round passed.');
  } catch (error) {
    await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}); throw error;
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
