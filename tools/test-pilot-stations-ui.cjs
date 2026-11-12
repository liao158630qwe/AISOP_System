/* Run inside the isolated pilot UI fixture; never writes to the port-8000 database. */
const assert = require('node:assert/strict');
const path = require('node:path');

module.exports = async ({page, context, base, output}) => {
  const modelRequests = [];
  const observe = request => {if (/\/api\/v1\/(realtime|analysis-runs|screw\/|sticker\/|pose\/)/.test(request.url())) modelRequests.push(request.url());};
  page.on('request', observe);
  const select = async number => {
    await page.locator('#inspection-task').selectOption('station' + number);
    await page.waitForFunction(number => window.StationOne.state.station === 'station-' + number &&
      window.StationOne.state.connected && !window.ScrewApp.state.switching, number);
    assert.match(await page.locator('#workflow-name').textContent(), new RegExp('工位' + number));
  };
  const submit = async reason => {
    await page.locator('#manual-reason').fill(reason);
    await page.locator('#manual-attest').check();
    await page.locator('#manual-password').fill(process.env.APP_PASSWORD);
    await page.locator('#manual-submit').click();
    await page.locator('#manual-dialog').waitFor({state: 'hidden'});
    await page.waitForFunction(() => !StationOne.state.busy);
  };
  const image = async () => {
    await page.locator('#source-file').setInputFiles(path.join(output, 'holes.jpg'));
    await page.waitForFunction(() => StationOne.state.frameReady);
  };
  const start = async number => {
    await page.locator('#source-select').selectOption('image'); await image();
    await page.locator('#product').fill('六工位流程联调');
    await page.locator('#part-number').fill('MULTI-' + number);
    await page.locator('#start-cycle').click();
    await page.waitForFunction(() => !!StationOne.state.cycle && !StationOne.state.busy);
  };
  await page.locator('#workspace-login').waitFor({state:'visible'});
  await page.locator('#workspace-login [name=username]').fill(process.env.APP_USERNAME);
  await page.locator('#workspace-login [name=password]').fill(process.env.APP_PASSWORD);
  await page.locator('#workspace-login button[type=submit]').click();
  await page.waitForFunction(() => Boolean(window.Workspace.session));
  const initialStationOneResponse = await context.request.get(base + '/api/v1/pilot/cycles?station=station-1');
  assert.equal(initialStationOneResponse.status(), 200);
  const initialStationOneCount = (await initialStationOneResponse.json()).total;
  await page.locator('#workspace-login').waitFor({state: 'hidden'});
  await select(2); await start(2);
  const station2ID = await page.evaluate(() => StationOne.state.cycle.id);
  await page.locator('#target-list input').first().check();
  await select(4); await start(4);
  assert.equal(await page.locator('#target-list input').count(), 2);
  const station4ID = await page.evaluate(() => StationOne.state.cycle.id);
  assert.notEqual(station2ID, station4ID);
  await select(2);
  await page.locator('#restore-panel').waitFor({state: 'visible'});
  assert.equal(await page.evaluate(() => StationOne.state.cycle.id), station2ID);
  assert.equal(await page.locator('#target-list input:checked').count(), 0);
  await page.reload();
  await page.locator('#restore-panel').waitFor({state: 'visible'});
  assert.equal(await page.evaluate(() => StationOne.state.cycle.id), station2ID);

  const expected = {2: {count: 5, phases: ['SCREWS']}, 3: {count: 3, phases: ['CLIPS', 'SCREWS']},
    4: {count: 2, phases: ['SCREWS']}, 5: {count: 9, phases: ['TERMINALS', 'SCREWS']},
    6: {count: 7, phases: ['SCREWS', 'TERMINALS', 'FRAME_AIR', 'HOUSING_AIR']}};
  for (const number of [2, 3, 4, 5, 6]) {
    await select(number);
    if (number === 2 || number === 4) {
      await page.locator('#resume-cycle').click(); await submit('已核对仍为本工位同一工件，恢复检查');
    } else await start(number);
    const {count, phases} = expected[number];
    assert.equal(await page.locator('#detection-results tr').count(), count);
    if (number === 5) {
      assert.equal(await page.locator('#target-list input').count(), 1);
      assert.ok(await page.locator('#stage-choice').isHidden());
    }
    for (const phase of phases) {
      if (await page.locator('#stage-choice').isVisible()) await page.locator('#stage-select').selectOption(phase);
      await image();
      for (const checkbox of await page.locator('#target-list input').all()) await checkbox.check();
      await page.locator('#confirm-stage').click(); await submit('人工记录本工位当前项目，测试阶段证据');
    }
    assert.equal(await page.evaluate(() => StationOne.state.cycle.stage), 'READY');
    assert.equal(await page.locator('#pilot-stage-evidence img').count(), phases.length);
    if (number === 6) {
      await page.locator('#refresh-screws').click();
      assert.equal(await page.locator('#stage-select option').count(), 4);
      await page.locator('#stage-select').selectOption('FRAME_AIR'); await image();
      await page.locator('#target-list input').check();
      await page.locator('#confirm-stage').click(); await submit('重新核对装饰框总成吹净并更新证据');
    }
    await page.screenshot({path: path.join(output, `station-${number}-ready.png`), fullPage: true});
    await page.locator('#finish-cycle').click(); await submit('全部检查项已人工记录，保存本工位结果');
    const result = [3, 5, 6].includes(number) ? 'REVIEW' : 'MANUAL_OK';
    assert.equal(await page.evaluate(() => StationOne.state.cycle.result), result);
    assert.equal(await page.locator('#workflow-status').getAttribute('data-verdict'), result === 'REVIEW' ? 'REVIEW' : 'PASS');
    await page.locator('#current-detail').click();
    await page.locator('#detail-dialog').waitFor({state: 'visible'});
    assert.match(await page.locator('#detail-content').textContent(), new RegExp('工位' + number));
    assert.equal(await page.locator('#detail-dialog .evidence-grid img').count(), phases.length);
    await page.locator('#detail-dialog .close-button').click();
    await page.locator('#pilot-open-history').click();
    await page.locator('#pilot-history-dialog').waitFor({state: 'visible'});
    assert.equal(await page.locator('#history-rows tr').count(), 1);
    assert.match(await page.locator('#history-rows').textContent(), new RegExp('MULTI-' + number));
    await page.locator('#pilot-history-dialog .close-button').click();
  }
  await page.setViewportSize({width: 390, height: 844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path: path.join(output, 'station-6-mobile.png'), fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});
  for (const number of [2, 3, 4, 5, 6]) {
    await page.goto(base + '/station-' + number);
    await page.waitForFunction(number => window.StationOne?.state.station === 'station-' + number && window.StationOne.state.connected, number);
    assert.equal(new URL(page.url()).searchParams.get('task'), 'station' + number);
  }
  assert.equal((await (await context.request.get(base + '/api/v1/pilot/cycles?station=station-1')).json()).total, initialStationOneCount);
  page.off('request', observe);
  assert.deepEqual(modelRequests, [], 'manual stations must not submit model inference or video jobs');
  console.log('Stations 2–6 UI passed: independent cycles, ordered and unordered checks, evidence, history, reload, routes and mobile.');
};
