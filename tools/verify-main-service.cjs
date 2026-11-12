// Exercise the running 8000 service with a recorded browser camera and real models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root = path.resolve(__dirname, '..');
const base = process.env.AISOP_TEST_BASE || 'http://127.0.0.1:8000';
const output = path.resolve(root, process.env.AISOP_TEST_OUTPUT || 'test-results/service-optimization-20261105/main-service');
const sampleCount = Number(process.env.AISOP_TEST_SAMPLES || 12);
assert.ok(Number.isInteger(sampleCount) && sampleCount >= 2 && sampleCount <= 300, 'AISOP_TEST_SAMPLES must be an integer from 2 to 300');
const camera = path.join(root, '.runtime/screw-live-camera.y4m');
const credentials = JSON.parse(fs.readFileSync(path.join(root, '.runtime/local-settings.json'), 'utf8'));
const percentile = (values, p) => [...values].sort((a, b) => a - b)[Math.ceil(values.length * p) - 1];

(async () => {
  const startedAt = new Date().toISOString();
  assert.ok(fs.existsSync(camera), 'Recorded camera fixture required');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({channel: 'msedge', headless: true, args: [
    '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    '--use-file-for-fake-video-capture=' + camera,
  ]});
  const errors = [], requests = [], results = [];
  const context = await browser.newContext({viewport: {width: 1512, height: 1080}, permissions: ['camera']});
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (request.url().includes('/api/')) requests.push(request.url());});
  try {
    await page.goto(base);
    await page.waitForFunction(() => window.ScrewApp?.state.model);
    await page.locator('#workspace-login').waitFor({state:'visible'});
    await page.locator('#workspace-login [name=username]').fill(credentials.username);
    await page.locator('#workspace-login [name=password]').fill(credentials.password);
    await page.locator('#workspace-login button[type=submit]').click();
    await page.waitForFunction(() => window.ScrewApp.state.session);
    for (const task of ['pose', 'screw', 'sticker']) {
      await page.locator('#inspection-task').selectOption(task);
      await page.waitForFunction(task => ScrewApp.state.task === task && ScrewApp.state.model?.ready && !ScrewApp.state.switching, task);
      await page.locator('#start-live').click();
      await page.waitForFunction(count => ScrewApp.state.latencySamples.length >= count, sampleCount,
        {timeout: Math.max(90000, sampleCount * 1500)});
      await page.waitForFunction(() => {
        const s = ScrewApp.state;
        return s.running && !s.preparing && !s.liveIssue && s.frame && performance.now() - s.lastSampleAt < 700 &&
          s.latencySamples.filter(ms => ms < 700).length >= 5;
      }, null, {timeout: 20000});
      const data = await page.evaluate(() => ({task: ScrewApp.state.task,
        latencies: [...ScrewApp.state.latencySamples], processingMs: ScrewApp.state.processing,
        actualFps: ScrewApp.state.actualFps, gaps: ScrewApp.state.evidenceGaps,
        preparationMs: ScrewApp.state.preparationMs,
        frameCount: ScrewApp.state.frames.length, model: ScrewApp.state.frame?.model,
        footer: document.querySelector('#footer-performance').textContent}));
      const steady = data.latencies.slice(1);
      assert.deepEqual(data.gaps, [], 'Normal recorded-camera operation must not inherit gaps from model preparation');
      results.push({...data, firstFrameMs: data.latencies[0], steadyP95Ms: percentile(steady, .95),
        steadyP99Ms: percentile(steady, .99), steadyMaxMs: Math.max(...steady), steadySamples: steady.length,
        latencyTargetMet: {includingFirstFrame: data.latencies.every(ms => ms <= 500), steady: steady.every(ms => ms <= 500)}});
      console.log(JSON.stringify({task, frameCount: data.frameCount, steadyP95Ms: percentile(steady, .95),
        steadyP99Ms: percentile(steady, .99), steadyMaxMs: Math.max(...steady)}));
      await page.screenshot({path: path.join(output, task + '.png'), fullPage: true});
      await page.locator('#finish-detection').click();
      await page.waitForFunction(() => !ScrewApp.state.busy && !ScrewApp.state.streamId);
      if (await page.locator('#modal').isVisible()) await page.locator('#modal [data-action=close-modal]').first().click();
      assert.equal(await page.evaluate(() => ScrewApp.state.stream), null);
    }
    assert.ok(requests.every(url => new URL(url).origin === base), 'All APIs must use the main origin');
    assert.deepEqual(errors, []);
    const metrics = await page.evaluate(() => Workspace.fetchJSON('/metrics', {}, ScrewApp.state.session.csrf));
    const report = {passed: true, base, startedAt, recordedAt: new Date().toISOString(), requestedSamplesPerTask: sampleCount,
      input: 'Recorded video presented as a browser camera; real model inference',
      scope: 'Browser capture to DOM result update, excludes camera exposure/driver and final screen scanout',
      results, serverRealtime: metrics.realtimeTransport, errors};
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({passed: true, results: results.map(({task, preparationMs, firstFrameMs, steadyP95Ms, steadyP99Ms, steadyMaxMs, actualFps, frameCount, latencyTargetMet}) =>
      ({task, preparationMs, firstFrameMs, steadyP95Ms, steadyP99Ms, steadyMaxMs, actualFps, frameCount, latencyTargetMet})), errors}));
    await page.locator('#server-account').click();
  } catch (error) {
    await page.screenshot({path: path.join(output, 'failure.png'), fullPage: true});
    throw error;
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
