const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')); }
const directory = path.resolve(__dirname, '../assets/demos/squat-stream-2026-10-10');
const data = JSON.parse(fs.readFileSync(path.join(directory, 'realtime-results.json'), 'utf8'));
(async () => {
  const browser = await playwright.chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1366, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:8000/assets/demos/squat-stream-2026-10-10/demo.html');
    await page.waitForFunction(() => document.querySelector('video').readyState >= 2);
    await page.waitForFunction(() => document.querySelector('video').currentTime > .3);
    assert.equal(await page.locator('#samples').textContent(), '37 帧');
    await page.locator('video').evaluate(video => {video.pause(); video.currentTime = 3;});
    await page.waitForFunction(() => document.querySelector('#time').textContent.startsWith('3.0'));
    assert.equal(await page.locator('#people tr').count(), data.frames[15].people.length);
    await page.locator('#original').click();
    await page.waitForFunction(() => {
      const v = document.querySelector('video');
      return v.readyState >= 2 && v.currentSrc.endsWith('original.webm') && Math.abs(v.currentTime - 3) < .05;
    });
    assert.equal(await page.locator('video').evaluate(video => video.paused), true);
    await page.locator('#detected').click();
    await page.waitForFunction(() => {
      const v = document.querySelector('video');
      return v.readyState >= 2 && v.currentSrc.endsWith('detection.webm') && Math.abs(v.currentTime - 3) < .05;
    });
    await page.screenshot({path: path.join(directory, 'desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: path.join(directory, 'mobile.png'), fullPage: true});
    assert.deepEqual(errors, []);
    const result = {passed: true, frames: data.frames.length, videoDecoded: true, autoplay: true,
      seek: true, originalDetectionSwitchPreservesTimeAndPause: true, mobileOverflow: false, pageErrors: errors};
    fs.writeFileSync(path.join(directory, 'browser-checks.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
