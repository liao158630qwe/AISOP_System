const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');
(async () => {
  fs.mkdirSync('reference', { recursive: true });
  const browser = await chromium.launch({channel:'msedge', headless:true});
  const page = await browser.newPage({viewport:{width:1280,height:720}});
  await page.goto(require('node:url').pathToFileURL(require('node:path').resolve(process.argv[2] || 'assets/reference.mp4')).href);
  await page.waitForFunction(() => document.querySelector('video')?.readyState >= 2);
  const meta = await page.evaluate(() => {const v=document.querySelector('video');v.pause();return {duration:v.duration,width:v.videoWidth,height:v.videoHeight};});
  console.log(meta);
  for(let t=0; t<meta.duration; t+=Math.max(1,meta.duration/18)) {
    await page.evaluate(async t => {
      const v=document.querySelector('video');v.currentTime=Math.min(t,v.duration-.05);
      await new Promise(r => v.addEventListener('seeked',r,{once:true}));
      const c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;c.getContext('2d').drawImage(v,0,0);
      return true;
    },t+.01);
    await page.locator('video').screenshot({path:`reference/frame-${t.toFixed(1)}.jpg`});
  }
  await browser.close();
})();

