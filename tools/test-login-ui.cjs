// Browser regressions against an isolated HTTP fixture; no real credentials/models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root = path.resolve(__dirname, '..');
const deferred = () => {let resolve;const promise = new Promise(r => resolve = r);return {promise, resolve};};
let nextSession, nextLogin, nextFailure, nextLoginStatus, nextScript, serial = 0, logins = 0, writes = 0, sessionReads = 0;
const sessions = new Map();
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  const reply = (status, value, headers = {}) => {if(!res.destroyed)res.writeHead(status, {'Content-Type':'application/json', ...headers}).end(JSON.stringify(value));};
  const token = /(?:^|;\s*)fixture_session=([^;]+)/.exec(req.headers.cookie || '')?.[1];
  const session = sessions.get(token);
  if(url.pathname === '/api/v1/gateway') {
    for await (const chunk of req) { /* Drain fixture uploads. */ }
    const status=Number(url.searchParams.get('status') || 503);
    if(status===401)sessions.delete(token);
    res.writeHead(status, {'Content-Type':'text/html', 'Retry-After':url.searchParams.get('retry') || '2'}).end('<h1>Gateway unavailable</h1>');return;
  }
  if(url.pathname === '/api/v1/empty') {res.writeHead(204).end();return;}
  if(url.pathname === '/api/v1/headers')return reply(200, {custom:req.headers['x-custom'],csrf:req.headers['x-csrf-token']});
  if(url.pathname === '/api/v1/validation')return reply(422, {detail:[{msg:'Field required'},{msg:'Invalid value'}]});
  if(url.pathname === '/api/v1/invalid-success') {res.writeHead(200, {'Content-Type':'text/plain'}).end('not JSON');return;}
  if(url.pathname === '/api/v1/slow-body') {
    res.writeHead(200, {'Content-Type':'application/json'});res.write('{');
    const timer=setTimeout(()=>res.end('"ok":true}'),2000);res.on('close',()=>clearTimeout(timer));return;
  }
  if(url.pathname === '/api/v1/session') {
    sessionReads++;
    const hold = nextSession;nextSession = null;
    if(hold){hold.entered.resolve();await hold.release.promise;}
    if(hold?.status)return reply(hold.status, {message:'服务暂不可用'});
    return reply(session ? 200 : 401, session || {message:'请先登录'});
  }
  if(url.pathname === '/api/v1/login') {
    logins++;
    for await (const chunk of req) { /* Consume the fixture request without logging credentials. */ }
    const hold = nextLogin;nextLogin = null;
    if(hold){hold.entered.resolve();await hold.release.promise;}
    if(nextLoginStatus){const status=nextLoginStatus;nextLoginStatus=null;return reply(status,{message:'用户名或密码错误'});}
    const id = 'session-' + ++serial, value = {username:'fixture', csrf:id};
    sessions.set(id, value);
    return reply(200, value, {'Set-Cookie':`fixture_session=${id}; HttpOnly; SameSite=Strict; Path=/`});
  }
  if(url.pathname === '/api/v1/old-request') {
    const hold = nextFailure;nextFailure = null;hold.entered.resolve();await hold.release.promise;
    return reply(401, {message:'旧会话失效'});
  }
  if(url.pathname === '/api/v1/permission')return reply(403, {message:'没有操作权限'});
  if(['/api/v1/write', '/api/v1/media', '/api/v1/logout'].includes(url.pathname)) {
    for await (const chunk of req) { /* Drain uploads. */ }
    writes++;
    if(!session)return reply(401, {message:'请先登录'});
    if(req.headers['x-csrf-token'] !== session.csrf)return reply(403, {code:'SESSION_CSRF_INVALID',message:'会话已更新'});
    if(url.pathname.endsWith('/logout')) {
      sessions.delete(token);
      return reply(200, {ok:true}, {'Set-Cookie':'fixture_session=; Max-Age=0; Path=/'});
    }
    return reply(url.pathname.endsWith('/media') ? 201 : 200, {ok:true,id:'media-fixture'});
  }
  if(url.pathname === '/api/v1/admin/catalog')return reply(200, {models:[{id:'fixture-model',name:'登录入口验证模型',version:'1',task:'detect',enabled:false,size:1,created_at:1,classes:{0:'part'}}],scenes:[]});
  if(url.pathname.endsWith('-model'))return reply(200, {ready:true, targets:[], modelName:'fixture', offlineBackend:'yolo'});
  if(url.pathname.startsWith('/api/'))return reply(200, {items:[]});
  if(url.pathname === '/login-test.html') {
    res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}).end('<!doctype html><button id="account-button">登录</button><script src="/workspace-api.js"></script>');return;
  }
  if(url.pathname === '/admin.js' && nextScript){
    const gate=nextScript;nextScript=null;gate.entered.resolve();await gate.release.promise;
  }
  const publicRoot = url.pathname.startsWith('/assets/') ? root : path.join(root, 'frontend');
  const file = path.resolve(publicRoot, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if(!file.startsWith(publicRoot + path.sep)){res.writeHead(404).end();return;}
  fs.readFile(file, (error, body) => {
    if(error){res.writeHead(404).end();return;}
    res.writeHead(200, {'Content-Type':({'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'})[path.extname(file)] || 'application/octet-stream'}).end(body);
  });
});
const pendingHolds = new Set();
const hold = () => {const gate={entered:deferred(), release:deferred()};pendingHolds.add(gate);return gate;};

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const passed = [], errors = [];
  const check = async (name, run) => {
    const context = await browser.newContext();
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
    try {await run(context);passed.push(name);console.log('PASS ' + name);}
    finally {for(const gate of pendingHolds)gate.release.resolve();pendingHolds.clear();nextSession=nextLogin=nextFailure=nextLoginStatus=nextScript=null;await context.close();}
  };
  const open = async context => {const page=await context.newPage();await page.goto(base+'/login-test.html');return page;};
  const signIn = page => page.evaluate(() => Workspace.signIn('fixture','test-only'));
  const submitLogin = async page => {
    await page.locator('#workspace-login [name=username]').fill('fixture');
    await page.locator('#workspace-login [name=password]').fill('test-only');
    await page.locator('#workspace-login button[type=submit]').click();
  };
  try {
    await check('homepage locks before session check and requires successful login',async context=>{
      const gate=nextSession=hold(),page=await context.newPage();await page.goto(base+'/');await gate.entered.promise;
      assert.equal(await page.locator('body').evaluate(node=>node.inert),true);
      await page.locator('#server-account').click({force:true});
      assert.equal(await page.locator('#screw-login').count(),0,'The page must not accept clicks while checking the session');
      await page.keyboard.press('Control+Shift+G');
      assert.equal(await page.locator('#modal').evaluate(node=>node.open),false,'Keyboard shortcuts must stay locked during the session check');
      gate.release.resolve();await page.locator('#workspace-login').waitFor({state:'visible'});
      assert.equal(await page.locator('#workspace-login .close-button').isVisible(),false);
      assert.equal(await page.locator('#workspace-login [name=username]').evaluate(node=>node===document.activeElement),true);
      await page.keyboard.press('Control+Shift+G');
      assert.equal(await page.locator('#modal').evaluate(node=>node.open),false,'Keyboard shortcuts must not cover the login prompt');
      await page.keyboard.press('Escape');
      await page.mouse.click(2,2);
      assert.equal(await page.locator('#workspace-login').evaluate(node=>node.open),true);
      await page.locator('#workspace-login').evaluate(node=>node.close());
      await page.waitForFunction(()=>document.querySelector('#workspace-login').open);
      nextLoginStatus=401;await submitLogin(page);
      await page.waitForFunction(()=>document.querySelector('#workspace-login-error').textContent.includes('密码错误'));
      assert.equal(await page.locator('body').evaluate(node=>node.inert),true);
      assert.equal(await page.locator('#workspace-login button[type=submit]').isEnabled(),true);
      await submitLogin(page);await page.locator('#workspace-login').waitFor({state:'hidden'});
      assert.equal(await page.locator('body').evaluate(node=>node.inert),false);
      assert.equal(await page.locator('#workspace-login [name=password]').inputValue(),'');
      await page.locator('[data-action=upload]').click();await page.locator('#screw-upload-file').waitFor({state:'visible'});
    });
    await check('every workspace entry prompts automatically and keeps its destination after login',async context=>{
      for(const route of ['/?task=station2','/admin.html','/datasets.html','/training.html','/settings.html','/terminals.html','/scene-console.html','/station-one.html','/screw-inspection.html','/board.html']){
        await context.clearCookies();const page=await context.newPage();await page.goto(base+route);
        await page.locator('#workspace-login').waitFor({state:'visible'});
        assert.equal(await page.locator('body').evaluate(node=>node.inert),true,route);
        assert.equal(await page.locator('#workspace-login .close-button').isVisible(),false,route);
        await submitLogin(page);await page.locator('#workspace-login').waitFor({state:'hidden'});
        assert.equal(await page.locator('body').evaluate(node=>node.inert),false,route);
        assert.equal(page.url(),base+route);await page.close();
      }
    });
    await check('valid sessions skip the prompt on entry and refresh',async context=>{
      await context.request.post(base+'/api/v1/login',{data:{username:'fixture',password:'test-only'}});
      const page=await context.newPage();await page.goto(base+'/');
      for(let i=0;i<2;i++){
        await page.waitForFunction(()=>Workspace.session&&!document.body.inert);
        assert.equal(await page.locator('#workspace-login[open]').count(),0);
        if(!i)await page.reload();
      }
    });
    await check('deferred page initialization keeps authenticated deep links working',async context=>{
      await context.request.post(base+'/api/v1/login',{data:{username:'fixture',password:'test-only'}});
      const gate=nextScript=hold(),page=await context.newPage();let reads=0;
      page.on('request',request=>{if(new URL(request.url()).pathname==='/api/v1/session')reads++;});
      const navigating=page.goto(base+'/admin.html?model=fixture-model');
      await gate.entered.promise;await page.waitForFunction(()=>window.Workspace&&document.readyState==='interactive');
      assert.equal(reads,0,'Wait for deferred page listeners before checking the session');
      assert.equal(await page.locator('body').evaluate(node=>node.inert),true);
      gate.release.resolve();await navigating;
      await page.locator('#model-details-dialog').waitFor({state:'visible'});
      assert.equal(await page.locator('#models-tab').getAttribute('aria-selected'),'true');
      assert.equal(await page.locator('#model-search').inputValue(),'fixture-model');
      assert.equal(await page.locator('#workspace-login[open]').count(),0);
    });
    await check('unavailable session service stays locked and allows login retry',async context=>{
      const gate=nextSession=hold();gate.status=503;const page=await context.newPage();await page.goto(base+'/settings.html');
      await gate.entered.promise;gate.release.resolve();await page.locator('#workspace-login').waitFor({state:'visible'});
      assert.equal(await page.locator('body').evaluate(node=>node.inert),true);
      await page.waitForFunction(()=>document.querySelector('#workspace-login-error').textContent.includes('无法确认登录状态'));
      await submitLogin(page);await page.locator('#workspace-login').waitFor({state:'hidden'});
      assert.equal(await page.locator('body').evaluate(node=>node.inert),false);
    });
    await check('logout and expired sessions reopen the mandatory prompt; another tab can unlock it',async context=>{
      const page=await context.newPage();await page.goto(base+'/settings.html');await submitLogin(page);
      await page.locator('#workspace-login').waitFor({state:'hidden'});await page.evaluate(()=>Workspace.logout());
      await page.locator('#workspace-login').waitFor({state:'visible'});
      const second=await open(context);await signIn(second);await page.locator('#workspace-login').waitFor({state:'hidden'});
      assert.equal(await page.locator('body').evaluate(node=>node.inert),false);
      await page.evaluate(()=>Workspace.request('/gateway?status=401').catch(()=>{}));
      await page.locator('#workspace-login').waitFor({state:'visible'});
      assert.equal(await page.locator('body').evaluate(node=>node.inert),true);
      await page.close();await second.close();
    });
    await check('gateway errors preserve status, safe messages and retry delays for JSON and uploads',async context=>{
      const page=await open(context);await signIn(page);
      for(const upload of [false,true])for(const status of [413,429,502,503,504]) {
        const result=await page.evaluate(async ({upload,status})=>{
          try {if(upload)await Workspace.upload('/gateway?status='+status,new Blob(['fixture']));else await Workspace.request('/gateway?status='+status);}
          catch(error){return {status:error.status,retry:error.retryAfterMs,message:error.message};}
        },{upload,status});
        assert.equal(result.status,status);assert.equal(result.retry,2000);assert.ok(!result.message.includes('<h1>'));
      }
      const delay=await page.evaluate(()=>Workspace.request('/gateway?retry=invalid').catch(error=>error.retryAfterMs));
      assert.equal(delay,0);
      const dated=await page.evaluate(()=>Workspace.request('/gateway?retry='+encodeURIComponent(new Date(Date.now()+60000).toUTCString())).catch(error=>error.retryAfterMs));
      assert.ok(dated>55000 && dated<=60000);
      assert.equal(await page.evaluate(()=>Workspace.request('/validation').catch(error=>error.message)),'Field required；Invalid value');
      assert.equal(await page.evaluate(()=>Workspace.request('/empty')),null);
      assert.match(await page.evaluate(()=>Workspace.request('/invalid-success').catch(error=>error.message)),/返回内容无效/);
    });
    await check('non-JSON authentication failure refreshes the session without replaying',async context=>{
      const page=await open(context);await signIn(page);
      assert.equal(await page.evaluate(()=>Workspace.request('/gateway?status=401').catch(error=>error.status)),401);
      assert.equal(await page.evaluate(()=>Workspace.session),null);
    });
    await check('Headers objects and request deadlines survive caller cancellation options',async context=>{
      const page=await open(context),session=await signIn(page);
      const headers=await page.evaluate(()=>Workspace.request('/headers',{headers:new Headers({'X-Custom':'kept','X-CSRF-Token':'stale'})}));
      assert.deepEqual(headers,{custom:'kept',csrf:session.csrf});
      assert.equal(await page.evaluate(()=>Workspace.request('/slow-body',{timeout:50,signal:new AbortController().signal}).catch(error=>error.name)),'TimeoutError');
      assert.equal(await page.evaluate(()=>{
        const controller=new AbortController();controller.abort();return Workspace.request('/headers',{signal:controller.signal}).catch(error=>error.name);
      }),'AbortError');
    });
    await check('late initial 401 cannot erase a successful login', async context => {
      const page=await open(context),gate=nextSession=hold();
      await page.evaluate(()=>{window.initial=Workspace.connect();});await gate.entered.promise;
      const current=await signIn(page);gate.release.resolve();await page.evaluate(()=>window.initial);
      assert.equal(await page.evaluate(()=>Workspace.session.csrf),current.csrf);
    });
    await check('late authenticated request cannot erase a newer session', async context => {
      const page=await open(context);await signIn(page);const gate=nextFailure=hold();
      await page.evaluate(()=>{window.old=Workspace.request('/old-request').catch(error=>error.status);});await gate.entered.promise;
      const current=await signIn(page);gate.release.resolve();assert.equal(await page.evaluate(()=>window.old),401);
      assert.equal(await page.evaluate(()=>Workspace.session.csrf),current.csrf);
    });
    await check('stale CSRF refreshes credentials without replaying a write or upload', async context => {
      const page=await open(context);await signIn(page);
      for(const upload of [false,true]) {
        await context.request.post(base+'/api/v1/login',{data:{username:'fixture',password:'test-only'}});
        const before=writes;
        const failed=await page.evaluate(async upload=>{
          try{if(upload)await Workspace.upload('/media',new Blob(['fixture']));else await Workspace.json('/write','POST',{value:1});}
          catch(error){return {status:error.status,code:error.code,session:Workspace.session?.csrf};}
        },upload);
        assert.equal(failed.status,403);assert.equal(failed.code,'SESSION_CSRF_INVALID');assert.ok(failed.session);
        assert.equal(writes,before+1,'The rejected write must not be replayed');
        assert.equal((await page.evaluate(()=>Workspace.json('/write','POST',{value:1}))).ok,true);
      }
      const reads=sessionReads;
      assert.equal(await page.evaluate(()=>Workspace.request('/permission').catch(error=>error.status)),403);
      assert.equal(sessionReads,reads,'Permission errors must not trigger authentication recovery');
      await context.request.post(base+'/api/v1/login',{data:{username:'fixture',password:'test-only'}});
      await page.evaluate(()=>Workspace.logout());assert.equal(await page.evaluate(()=>Workspace.session),null);
    });
    for(const main of [false,true])await check((main?'dashboard':'shared dialog')+' blocks duplicate submits and dismissal while signing in',async context=>{
      const page=await context.newPage();await page.goto(base+(main?'/':'/login-test.html'));await page.evaluate(()=>Workspace.connect());
      if(main){await page.waitForFunction(()=>window.ScrewApp?.state.model?.ready);await page.locator('#workspace-login').waitFor({state:'visible'});}
      else await page.evaluate(()=>{window.loginResult='pending';Workspace.login().then(value=>window.loginResult=value);});
      const dialog='#workspace-login',form='#workspace-login-form';
      await page.locator(form+' [autocomplete=username]').fill('fixture');await page.locator(form+' [type=password]').fill('test-only');
      const gate=nextLogin=hold(),before=logins;
      await page.evaluate(selector=>{const form=document.querySelector(selector);for(let i=0;i<3;i++)form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));},form);
      await gate.entered.promise;
      assert.equal(logins,before+1);assert.equal(await page.locator(form+' button[type=submit]').isDisabled(),true);
      await page.keyboard.press('Escape');assert.equal(await page.locator(dialog).evaluate(node=>node.open),true);
      await page.locator(dialog).evaluate(node=>node.close());await page.waitForFunction(selector=>document.querySelector(selector).open,dialog);
      gate.release.resolve();await page.waitForFunction(()=>Workspace.session!==null);
      await page.locator(dialog).waitFor({state:'hidden'});
      if(main)assert.equal(await page.evaluate(()=>ScrewApp.state.session===Workspace.session),true);
      else assert.equal((await page.evaluate(()=>window.loginResult)).username,'fixture');
    });
    await check('cancel before submit clears the promise and allows reopening',async context=>{
      const page=await open(context),before=logins;
      await page.evaluate(()=>{window.cancelResult='pending';Workspace.login().then(value=>window.cancelResult=value);});
      await page.locator('#workspace-login .close-button').click();await page.waitForFunction(()=>window.cancelResult===null);
      assert.equal(logins,before);await page.evaluate(()=>{Workspace.login();});assert.equal(await page.locator('#workspace-login').evaluate(node=>node.open),true);
    });
    await check('standalone inspection uses the shared session and submit lock',async context=>{
      const page=await context.newPage();await page.goto(base+'/screw-inspection.html');await page.evaluate(()=>Workspace.connect());
      await page.locator('#workspace-login [name=username]').fill('fixture');await page.locator('#workspace-login [name=password]').fill('test-only');
      const gate=nextLogin=hold(),before=logins;
      await page.evaluate(()=>{const form=document.querySelector('#workspace-login-form');for(let i=0;i<3;i++)form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));});
      await gate.entered.promise;assert.equal(logins,before+1);assert.equal(await page.locator('#workspace-login button[type=submit]').isDisabled(),true);
      await page.keyboard.press('Escape');assert.equal(await page.locator('#workspace-login').evaluate(node=>node.open),true);
      gate.release.resolve();await page.waitForFunction(()=>Workspace.session!==null);await page.locator('#workspace-login').waitFor({state:'hidden'});
      assert.match(await page.locator('#account').textContent(),/fixture/);
      await page.locator('#account').click();await page.waitForFunction(()=>Workspace.session===null);
    });
    await check('standalone inspection restores saved work after the shared login',async context=>{
      const page=await context.newPage();
      await page.addInitScript(()=>localStorage.setItem('aisop-screw-run','login-restore'));
      await page.goto(base+'/screw-inspection.html');
      await page.locator('#workspace-login').waitFor({state:'visible'});
      assert.equal(await page.locator('#login-dialog').count(),0);
      const restoring=page.waitForRequest('**/api/v1/analysis-runs/login-restore');
      await submitLogin(page);await restoring;
      await page.locator('#workspace-login').waitFor({state:'hidden'});
    });
    await check('login and logout synchronize across tabs',async context=>{
      const first=await open(context),second=await open(context);await second.evaluate(()=>Workspace.connect());
      const current=await signIn(first);await second.waitForFunction(csrf=>Workspace.session?.csrf===csrf,current.csrf);
      await first.evaluate(()=>Workspace.logout());await second.waitForFunction(()=>Workspace.session===null);
    });
    await check('cross-tab login supersedes an older session read',async context=>{
      const first=await open(context),second=await open(context),gate=nextSession=hold();
      await second.evaluate(()=>{window.initial=Workspace.connect();});await gate.entered.promise;
      const current=await signIn(first);
      await second.waitForFunction(csrf=>Workspace.session?.csrf===csrf,current.csrf);
      gate.release.resolve();await second.evaluate(()=>window.initial);
      assert.equal(await second.evaluate(()=>Workspace.session.csrf),current.csrf);
    });
    await check('login controls unlock before awaiting the requested follow-up action',async context=>{
      const page=await open(context);
      await page.evaluate(()=>{
        const dialog=document.createElement('dialog');dialog.id='followup';
        dialog.innerHTML='<button class="close-button">Close</button><form><input autocomplete="username" value="fixture"><input type="password" value="test-only"><p role="alert"></p><button type="submit">Login</button></form>';
        document.body.append(dialog);
        Workspace.bindLoginForm(dialog,dialog.querySelector('form'),()=>new Promise(resolve=>window.finishFollowup=resolve));
        dialog.showModal();dialog.querySelector('form').requestSubmit();
      });
      await page.waitForFunction(()=>typeof window.finishFollowup==='function');
      assert.equal(await page.locator('#followup .close-button').isDisabled(),false);
      await page.evaluate(()=>window.finishFollowup());
    });
    await check('logout waits for in-flight login before clearing the cookie',async context=>{
      const page=await open(context),gate=nextLogin=hold();
      await page.evaluate(()=>{window.signing=Workspace.signIn('fixture','test-only');});await gate.entered.promise;
      await page.evaluate(()=>{window.leaving=Workspace.logout();});gate.release.resolve();await page.evaluate(()=>window.leaving);
      assert.equal(await page.evaluate(()=>Workspace.session),null);assert.equal((await context.request.get(base+'/api/v1/session')).status(),401);
    });
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:passed.length,browserErrors:errors.length}));
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
