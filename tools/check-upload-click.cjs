const fs=require('node:fs'),path=require('node:path');
const p=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
(async()=>{
 const browser=await p.chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  let count=0;page.on('filechooser',async chooser=>{count++;await chooser.setFiles([]);});
  await page.goto('http://127.0.0.1:8000');await page.locator('#server-account').waitFor();
  await page.locator('.toolbar [data-action=upload]').click();
  const title=await page.locator('#modal-title').textContent();
  const c=JSON.parse(fs.readFileSync(path.join(__dirname,'../.runtime/local-settings.json'),'utf8'));
  await page.locator('#login-user').fill(c.username);await page.locator('#login-password').fill(c.password);
  await page.locator('#screw-login button[type=submit]').click();
  await page.waitForFunction(()=>window.ScrewApp.state.session);await page.waitForTimeout(300);
  const afterLogin=count;await page.locator('.toolbar [data-action=upload]').click();await page.waitForTimeout(300);
  console.log(JSON.stringify({initialDialog:title,fileChooserAfterLogin:afterLogin,fileChooserAfterSecondClick:count,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
