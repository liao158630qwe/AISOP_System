// Actual browser keyboard events + real dashboard code; isolated API/media fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root = path.resolve(__dirname, '..'), output = path.join(root, 'test-results', 'inspection-settings-20261105');
const sceneId = 'f403926a-0000-4000-8000-000000000001';
const scene = {id: sceneId, name: '按键验证任务', ready: true, revision: 1, sample_fps: 4,
  model: {name: 'fixture', version: '1', classes: {0: 'part'}}, rules: []};
const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  const publicRoot = pathname.startsWith('/assets/') ? root : path.join(root, 'frontend');
  const file = path.resolve(publicRoot, '.' + (pathname === '/' ? '/index.html' : pathname === '/settings' ? '/settings.html' : pathname));
  if (!file.startsWith(root + path.sep)) {res.writeHead(403).end(); return;}
  fs.readFile(file, (error, data) => {
    if (error) {res.writeHead(404).end(); return;}
    res.setHeader('Content-Type', ({'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'})[path.extname(file)] || 'application/octet-stream');
    res.end(data);
  });
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({channel: 'msedge', headless: true,
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream']});
  const context = await browser.newContext({viewport: {width: 1440, height: 1050}, permissions: ['camera']});
  const page = await context.newPage(), errors = [], frames = [], stops = [], cancellations = [];
  const stationStreams = new Map();
  const stations = Array.from({length: 6}, (_, i) => ({station: 'station-' + (i + 1), name: '工位' + (i + 1), registration_enabled: false}));
  let loggedIn = true, settingsPage;
  page.on('pageerror', error => errors.push(error.message)); page.setDefaultTimeout(10000);
  await page.addInitScript(() => {
    const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    window.__cameraCalls = 0; window.__cameraMode = 'normal';
    navigator.mediaDevices.getUserMedia = async options => {
      window.__cameraCalls++;
      if (window.__cameraMode === 'fail') throw new Error('fixture camera unavailable');
      if (window.__cameraMode === 'hold') await new Promise(resolve => window.__releaseCamera = resolve);
      return getMedia(options);
    };
  });
  await context.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, headers = request.headers();
    const reply = (json, status = 200) => route.fulfill({status, json}).catch(() => {});
    if (p.endsWith('/session')) return reply(loggedIn ? {username: 'fixture', csrf: 'test'} : {detail: '请登录'}, loggedIn ? 200 : 401);
    if (p.endsWith('/pilot/stations')) return reply({items: stations});
    if (p.endsWith('/admin/terminals')) return reply({items: stations.map(s => ({stationId: s.station, analysisType: 'screw', cameraId: s.station + '-camera', sampleFps: 4, enabled: true}))});
    if (p.endsWith('/station-streams') && request.method() === 'POST') {
      const stationId = url.searchParams.get('station');
      const stream = {streamId: 'fixture-' + stationId, streamEpoch: 1, stationId, cameraId: stationId + '-camera'};
      stationStreams.set(stream.streamId, stream); return reply(stream);
    }
    if (p.includes('/station-streams/')) {
      const id = p.split('/station-streams/')[1].split('/')[0], stream = stationStreams.get(id);
      if (request.method() === 'DELETE') {stops.push(id); return reply({released: true});}
      return reply({...stream, frameSeq: Number(headers['x-frame-seq']), people: [], aggregateTargets: [{id: 'S01', name: '测试位置', state: 'UNKNOWN'}], compliance: {status: 'REVIEW', label: '待确认'}});
    }
    if (p.endsWith('/cancel')) {cancellations.push(p); return reply({status: 'CANCELLED'});}
    if (p.endsWith('/scenes')) return reply({items: [scene]});
    if (p.endsWith('/scenes/' + sceneId)) return reply(scene);
    if (p.endsWith('-model')) return reply({ready: true, modelName: 'fixture', targets: [], offlineBackend: 'yolo'});
    if (p.endsWith('/realtime/frame')) {
      frames.push(url.searchParams.get('analysis_type'));
      return reply({frameId: headers['x-frame-id'], frameTime: Number(headers['x-frame-time']), processingMs: 1,
        people: [], observations: [], checks: [], compliance: {status: 'REVIEW', label: '测试待确认', reason: 'fixture'},
        inspection: {registered: false, targets: [], displayTargets: [], sequence: null}});
    }
    if (p.endsWith('/finish')) return reply({final: true, targets: [], checks: [], compliance: {status: 'REVIEW', label: '测试待确认', reason: 'fixture'}});
    if (p.includes('/realtime/streams/')) {if (request.method() === 'DELETE') stops.push(p); return reply({released: true});}
    return reply({items: []});
  });
  const base = 'http://127.0.0.1:' + server.address().port;
  const ready = () => page.waitForFunction(() => ScrewApp.state.model?.ready && !ScrewApp.state.busy && !ScrewApp.state.switching);
  const count = () => page.evaluate(() => window.__cameraCalls);
  const running = () => page.waitForFunction(() => ScrewApp.state.running && !ScrewApp.state.busy);
  const stop = async () => {
    const previousStops = stops.length;
    await page.evaluate(() => window.__stoppedTracks = ScrewApp.state.stream?.getTracks() || []);
    await page.keyboard.down('F9'); await page.keyboard.down('F9'); await page.keyboard.up('F9');
    await page.waitForFunction(() => !ScrewApp.state.busy && !ScrewApp.state.streamId);
    assert.ok(await page.evaluate(() => window.__stoppedTracks.every(track => track.readyState === 'ended')));
    await page.waitForTimeout(50);
    assert.equal(stops.length, previousStops + 1, 'holding F9 releases the stream exactly once');
    await page.evaluate(() => {for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();});
    await sleep(650);
  };
  const bind = async key => {
    await settingsPage.bringToFront();
    await settingsPage.locator('#trigger-bind').click(); await settingsPage.keyboard.press(key);
    await page.bringToFront(); await sleep(650);
  };
  const toggle = async enabled => {
    await settingsPage.bringToFront(); await settingsPage.locator('#trigger-enabled').setChecked(enabled);
    await page.bringToFront();
  };
  const unchanged = async action => {const before = await count(); await action(); assert.equal(await count(), before);};
  try {
    await page.goto(base + '/?task=pose'); await ready();
    assert.equal(await page.locator('.inspection-trigger, #trigger-enabled, #trigger-key').count(), 0, 'dashboard has no trigger settings panel');
    const settingsOpened = page.waitForEvent('popup');
    await page.locator('#settings-link').click(); settingsPage = await settingsOpened;
    settingsPage.on('pageerror', error => errors.push(error.message));
    await settingsPage.locator('[aria-controls="trigger-section-content"]').click();
    await settingsPage.locator('#trigger-key').waitFor();
    assert.equal(new URL(settingsPage.url()).pathname, '/settings');
    assert.equal(await settingsPage.locator('#trigger-key').textContent(), 'F8');
    assert.equal(await settingsPage.locator('#trigger-stop-key').textContent(), 'F9');
    await settingsPage.keyboard.press('F8');
    await settingsPage.keyboard.press('F9');
    assert.equal(await settingsPage.evaluate(() => Boolean(window.ScrewApp)), false);
    assert.deepEqual(await settingsPage.evaluate(() => InspectionTrigger.requestStart()), {accepted: false, reason: '检测页面尚未就绪'});
    assert.deepEqual(await settingsPage.evaluate(() => InspectionTrigger.requestStop()), {accepted: false, reason: '检测页面尚未就绪'});
    assert.equal(await count(), 0, 'settings never starts detection');
    await require('./test-modbus-settings.cjs')({page: settingsPage, context, base, output});
    await page.bringToFront();
    // Hold a real getUserMedia request open: key repeats and mouse calls cannot create another stream.
    await page.evaluate(() => window.__cameraMode = 'hold');
    await page.keyboard.down('F8'); await page.waitForFunction(() => window.__cameraCalls === 1);
    await page.keyboard.down('F8'); await page.keyboard.up('F8'); await page.keyboard.press('F8');
    await page.evaluate(() => document.querySelector('#start-live').onclick());
    assert.equal(await count(), 1);
    await page.evaluate(() => {window.__cameraMode = 'normal'; window.__releaseCamera();}); await running();
    await unchanged(() => page.keyboard.press('F8'));
    assert.ok(await page.evaluate(() => ScrewApp.state.running), 'start key never toggles stop');
    await page.locator('[data-action=system]').first().click();
    await stop();
    await page.evaluate(() => {window.__cameraMode = 'hold'; document.querySelector('#start-live').onclick(); document.querySelector('#start-live').onclick();});
    assert.equal(await count(), 2, 'mouse double start shares the synchronous lock');
    await page.evaluate(() => {window.__cameraMode = 'normal'; window.__releaseCamera();}); await running(); await stop();

    // Both native dialogs and typing controls are protected, without losing F8 in a part-number input.
    await page.locator('[data-action=system]').first().click();
    await unchanged(() => page.keyboard.press('F8'));
    await page.evaluate(() => document.querySelector('#modal').close());
    await unchanged(() => page.keyboard.press('Shift+F8'));
    await unchanged(() => page.keyboard.press('Control+F8'));
    await unchanged(() => page.evaluate(() => document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'F8', code: 'F8', isComposing: true, bubbles: true}))));
    await toggle(false);
    await unchanged(() => page.keyboard.press('F8'));
    await page.reload(); await ready();
    await settingsPage.reload(); await settingsPage.locator('[aria-controls="trigger-section-content"]').click(); await settingsPage.locator('#trigger-enabled').waitFor();
    assert.equal(await settingsPage.locator('#trigger-enabled').isChecked(), false, 'disabled setting persists');
    await toggle(true);
    await bind('Space'); assert.equal(await settingsPage.locator('#trigger-key').textContent(), '空格');
    await page.locator('#alert-search').fill('scan'); await unchanged(() => page.keyboard.press('Space'));
    assert.equal(await page.locator('#alert-search').inputValue(), 'scan ');
    await page.evaluate(() => document.activeElement.blur());
    await page.keyboard.press('Space'); await running(); await stop();
    await bind('Enter');
    await page.locator('#alert-search').focus(); await unchanged(() => page.keyboard.press('Enter'));
    await page.evaluate(() => document.activeElement.blur());
    await page.keyboard.press('Enter'); await running(); await stop();
    await bind('F7'); await page.reload(); await ready();
    assert.equal(await settingsPage.locator('#trigger-key').textContent(), 'F7');
    await unchanged(() => page.keyboard.press('F8'));
    await settingsPage.bringToFront();
    await settingsPage.locator('#trigger-bind').click(); await settingsPage.keyboard.press('F9');
    assert.equal(await settingsPage.locator('#trigger-key').textContent(), 'F7');
    assert.match(await settingsPage.locator('#trigger-feedback').textContent(), /F9.*停止/);
    await settingsPage.keyboard.press('Escape');
    await settingsPage.locator('#trigger-bind').click(); await settingsPage.keyboard.press('Escape');
    assert.equal(await settingsPage.locator('#trigger-key').textContent(), 'F7');
    await settingsPage.locator('#trigger-bind').click(); await settingsPage.keyboard.press('F5');
    assert.equal(await settingsPage.locator('#trigger-key').textContent(), 'F7', 'reserved browser keys cannot be bound');
    await settingsPage.keyboard.press('Escape'); await page.bringToFront();

    // State gates are evaluated at the moment of the press, including background and task switches.
    for (const patch of [{busy: true}, {switching: true}, {finishing: true}, {session: null}, {model: {ready: false}}]) {
      await page.evaluate(patch => {window.__beforeGate = {}; for (const key of Object.keys(patch)) window.__beforeGate[key] = ScrewApp.state[key]; Object.assign(ScrewApp.state, patch); ScrewApp.render();}, patch);
      await unchanged(() => page.keyboard.press('F7'));
      await page.evaluate(() => {Object.assign(ScrewApp.state, window.__beforeGate); ScrewApp.render();});
    }
    await page.evaluate(() => {Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); document.dispatchEvent(new Event('visibilitychange'));});
    await unchanged(() => page.keyboard.press('F7'));
    await page.evaluate(() => {delete document.hidden; document.dispatchEvent(new Event('visibilitychange'));});
    await page.evaluate(() => window.__cameraMode = 'fail'); await sleep(650);
    const beforeFailure = await count(); await page.keyboard.press('F7');
    await page.waitForFunction(() => !ScrewApp.state.busy && document.querySelector('#server-status').textContent.includes('fixture camera unavailable'));
    await unchanged(() => page.keyboard.press('F7')); // debounce, even after a fast failure
    await sleep(650); await page.evaluate(() => window.__cameraMode = 'normal');
    await page.locator('#alert-search').focus(); await page.keyboard.press('F7'); await running();
    assert.equal(await count(), beforeFailure + 2, 'failure releases lock; function keys work in an input');
    await stop();
    for (const task of ['screw', 'sticker', 'scene:' + sceneId]) {
      await page.locator('#inspection-task').selectOption(task); await ready(); await sleep(650);
      await page.keyboard.press('F7'); await running();
      await page.waitForFunction(() => ScrewApp.state.frames.length > 0);
      assert.equal(frames.at(-1), task.startsWith('scene:') ? 'scene' : task);
      await stop();
    }
    // Learning never starts inspection, and cross-tab settings are honored.
    const peer = await context.newPage(); await peer.goto(base + '/settings');
    await peer.locator('[aria-controls="trigger-section-content"]').click();
    await peer.locator('#trigger-enabled').waitFor();
    await peer.locator('#trigger-enabled').uncheck();
    await settingsPage.waitForFunction(() => !document.querySelector('#trigger-enabled').checked);
    await peer.close(); await page.bringToFront();
    await unchanged(() => page.keyboard.press('F7'));
    await toggle(true); await bind('F8');
    // Migrate a previously saved F9 start binding without allowing it to start a task.
    await page.evaluate(() => localStorage.setItem('aisop-inspection-trigger-v1', JSON.stringify({enabled: true, code: 'F9'})));
    await page.reload(); await ready();
    await unchanged(() => page.keyboard.press('F9'));
    await settingsPage.reload(); await settingsPage.locator('[aria-controls="trigger-section-content"]').click(); await settingsPage.locator('#trigger-key').waitFor();
    assert.equal(await settingsPage.locator('#trigger-key').textContent(), 'F8');
    await page.bringToFront();
    // F9 uses the existing cancellation path for queued/running offline jobs.
    await page.evaluate(() => Object.assign(ScrewApp.state, {run: 'stop-shortcut-job', runStatus: 'RUNNING', busy: true}));
    await page.keyboard.press('F9');
    await page.waitForFunction(() => ScrewApp.state.runStatus === 'CANCELLED');
    assert.equal(cancellations.length, 1);
    await page.evaluate(() => Object.assign(ScrewApp.state, {run: null, runStatus: null, busy: false}));
    await sleep(650);
    // Exercise the real station controller in all six slots, including its in-flight frame and camera cleanup.
    for (let n = 1; n <= 6; n++) {
      await page.locator('#inspection-task').selectOption('station' + n);
      await page.waitForFunction(n => StationDetection.state.station === 'station-' + n && StationDetection.state.connected && !StationDetection.state.busy && !ScrewApp.state.switching, n);
      await page.keyboard.press('F8');
      await page.waitForFunction(() => StationDetection.state.running && !StationDetection.state.busy && StationDetection.state.frames > 0);
      const previousStops = stops.length;
      await page.evaluate(() => window.__stationTracks = StationDetection.state.media.getTracks());
      if (n === 1) {
        await page.keyboard.press('Shift+F9'); await page.keyboard.press('Control+F9');
        assert.ok(await page.evaluate(() => StationDetection.state.running));
        await page.locator('[data-action=system]').first().click();
        await page.locator('#modal[open]').waitFor();
      } else await page.locator('#alert-search').focus();
      await page.keyboard.down('F9'); await page.keyboard.down('F9'); await page.keyboard.up('F9');
      await page.waitForFunction(() => !StationDetection.state.running && !StationDetection.state.busy && !StationDetection.state.stream && !StationDetection.state.media);
      assert.equal(stops.length, previousStops + 1);
      assert.equal(stops.at(-1), 'fixture-station-' + n);
      assert.ok(await page.evaluate(() => window.__stationTracks.every(track => track.readyState === 'ended')));
      assert.equal(await page.evaluate(() => StationDetection.state.archived), false, 'F9 stops the round without archiving a passing result');
      await page.keyboard.press('F9');
      assert.equal(stops.length, previousStops + 1, 'idle F9 does not repeat stop requests');
      await page.evaluate(() => {for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();});
      await sleep(650);
    }
    await page.locator('#inspection-task').selectOption('pose'); await ready();
    await page.screenshot({path: path.join(output, 'dashboard-desktop.png'), fullPage: true});
    await settingsPage.screenshot({path: path.join(output, 'settings-desktop.png'), fullPage: true});
    for (const width of [768, 390, 320]) {
      await page.setViewportSize({width, height: 900});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no overflow at ' + width);
      assert.equal(await page.locator('.inspection-trigger').count(), 0);
      assert.ok(await page.locator('#settings-link').isVisible());
      await settingsPage.setViewportSize({width, height: 900});
      assert.ok(await settingsPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'settings fit at ' + width);
      assert.ok(await settingsPage.locator('#trigger-key').isVisible(), 'trigger key remains visible at ' + width);
    }
    await page.screenshot({path: path.join(output, 'dashboard-mobile.png'), fullPage: true});
    await settingsPage.screenshot({path: path.join(output, 'settings-mobile.png'), fullPage: true});
    loggedIn = false; await page.reload(); await ready();
    await unchanged(() => page.keyboard.press('F8'));
    assert.match(await page.locator('#toast').textContent(), /登录/);
    assert.deepEqual(errors, []);
    const report = {passed: true, checks: ['separate settings page and toolbar link', 'no settings panel on dashboard', 'settings cannot start inspection', 'keyboard and USB-style press/release', 'hold/repeat rejection', 'mouse and key concurrency',
      'independent start and F9 stop', 'stop from dialogs and inputs', 'six station stop and camera cleanup', 'offline cancellation', 'F9 binding migration', 'dialog and input protection', 'IME and modifiers', 'configuration and persistence', 'disabled switch', 'reserved keys',
      'busy/switching/finishing/model/session gates', 'background guard', 'failure/debounce/retry', 'four model tasks', 'cross-tab settings', 'Modbus drafts and lamp preview', '320–1440 layout'], pageErrors: errors};
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report));
  } catch (error) {
    await page.screenshot({path: path.join(output, 'failure.png'), fullPage: true}); throw error;
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(error => {console.error(error); process.exitCode = 1; server.close();});
