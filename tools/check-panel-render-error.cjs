// Reproduce the screenshot error with stale row-key metadata in an isolated page.
const path=require('node:path');
const p=require(path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
(async()=>{const b=await p.chromium.launch({channel:'msedge',headless:true});try{
 const page=await b.newPage();await page.goto('http://127.0.0.1:8000');
 await page.waitForFunction(()=>window.ScrewApp?.state.model&&document.querySelector('#detection-results').children.length===12);
 const result=await page.evaluate(()=>{const root=document.querySelector('#detection-results');const cached=root.dataset.targetKeys;root.replaceChildren();try{window.ScrewApp.render();return {cached,error:null,rows:root.children.length};}catch(e){return {cached,error:e.message,stack:e.stack};}});
 console.log(JSON.stringify(result));
}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
