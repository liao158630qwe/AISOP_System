/* Shared, same-origin session and request handling for management and scene consoles. */
window.Workspace=(()=>{
  let session=null,sessionKnown=false,version=0,connecting=null,signingIn=null,loggingOut=null,loginPromise=null,resolveLogin=null;
  const loginRequired=document.body.hasAttribute('data-require-login');
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const taskLabels={detect:'目标检测',segment:'实例分割',pose:'关键点检测',obb:'旋转框检测',classify:'图像分类'};
  function changed(){document.dispatchEvent(new CustomEvent('workspace-session',{detail:session}));}
  const channel=typeof BroadcastChannel==='function'?new BroadcastChannel('aisop-session'):null;
  function publish(){
    if(channel)channel.postMessage('changed');
    else try{localStorage.setItem('aisop-session-changed',Date.now()+':'+Math.random());}catch{}
  }
  function setSession(value,broadcast=false){
    const different=!sessionKnown||JSON.stringify(value)!==JSON.stringify(session);
    session=value;sessionKnown=true;version++;
    if(different)changed();
    updateLoginGate();
    if(broadcast)publish();
    return session;
  }
  function responseError(status,data,retryAfter=null){
    const detail=typeof data?.detail==='string'?data.detail:Array.isArray(data?.detail)?data.detail.map(v=>v.msg).filter(Boolean).join('；'):'';
    const fallback={401:'登录已过期，请重新登录。',403:'没有操作权限，请核对登录身份。',413:'文件超过服务器或网关上传限制。',429:'请求过于频繁，请稍后重试。',502:'网关连接服务器失败，请稍后重试。',503:'服务暂不可用，请稍后重试。',504:'网关等待超时，请先检查服务器处理结果。'};
    const error=new Error(typeof data?.message==='string'&&data.message||detail||fallback[status]||`请求失败（${status}）`);
    error.status=status;error.code=data?.code;error.record=data?.record;
    const retry=retryAfter?.trim(),delay=retry?(Number.isFinite(Number(retry))?Number(retry)*1000:Date.parse(retry)-Date.now()):0;
    error.retryAfterMs=Number.isFinite(delay)?Math.max(0,delay):0;
    return error;
  }
  // A caller's cancellation signal must not disable the request deadline.
  async function fetchJSON(path,options={},csrf=null){
    const {timeout=35000,signal,...init}=options,controller=new AbortController();
    const abort=()=>controller.abort(signal.reason);
    if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true});
    const timer=setTimeout(()=>controller.abort(new DOMException('请求超时','TimeoutError')),timeout);
    try{
      const headers=new Headers(init.headers);if(csrf)headers.set('X-CSRF-Token',csrf);
      const response=await fetch('/api/v1'+path,{...init,credentials:'same-origin',headers,signal:controller.signal});
      if(response.status===204)return null;
      let data,parsed=false;
      try{data=await response.json();parsed=true;}catch(error){if(controller.signal.aborted)throw controller.signal.reason;}
      // Proxies can return HTML or empty bodies. Preserve HTTP metadata even
      // then, so authentication recovery and overload backoff still work.
      if(!response.ok)throw responseError(response.status,data,response.headers.get('Retry-After'));
      if(!parsed)throw new Error('服务器返回内容无效，请稍后重试');
      return data;
    }finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}
  }
  async function request(path,options={}){
    const started=version;
    try{return await fetchJSON(path,options,session?.csrf);}catch(error){await recover(error,started);throw error;}
  }
  const json=(path,method,value)=>request(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(value),timeout:120000});
  function connect(){
    if(signingIn)return signingIn;
    if(loggingOut)return loggingOut.then(()=>session);
    if(connecting?.version===version)return connecting.promise;
    const started=version;
    const promise=(async()=>{
      let value;
      try{value=await fetchJSON('/session');}catch(error){if(error.status!==401)throw error;value=null;}
      if(started===version)setSession(value);
      return session;
    })();
    connecting={version:started,promise};
    const clear=()=>{if(connecting?.promise===promise)connecting=null;};
    promise.then(clear,clear);return promise;
  }
  async function recover(error,started){
    if(started!==version||signingIn||loggingOut)return;
    if(error.status!==401&&error.code!=='SESSION_CSRF_INVALID')return;
    // Refresh credentials only. The failed operation may not be safe to replay.
    try{await connect();}catch{if(started===version)setSession(null);}
    if(session&&error.code==='SESSION_CSRF_INVALID')error.message='登录状态已同步，请重试刚才的操作';
  }
  function signIn(username,password){
    if(signingIn)return signingIn;
    if(loggingOut)return loggingOut.then(()=>signIn(username,password));
    const started=++version;connecting=null;
    const promise=(async()=>{
      const value=await fetchJSON('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      if(started===version)setSession(value,true);
      return session;
    })();
    signingIn=promise;
    const clear=()=>{if(signingIn===promise)signingIn=null;};
    promise.then(clear,clear);return promise;
  }
  function toast(message){let box=document.querySelector('#workspace-toast');if(!box){box=document.createElement('div');box.id='workspace-toast';box.className='toast';box.setAttribute('role','status');document.body.append(box);}box.textContent=message;box.classList.add('visible');box.hidden=false;clearTimeout(box.timer);box.timer=setTimeout(()=>box.hidden=true,4500);}
  function bindLoginForm(dialog,form,onSuccess,{reusable=false}={}){
    let submitting=false;
    const listeners=new AbortController();
    dialog.addEventListener('cancel',event=>{if(submitting)event.preventDefault();},{signal:listeners.signal});
    dialog.addEventListener('close',()=>{
      if(submitting){dialog.showModal();return;}
      form.querySelector('[type=password]').value='';
      if(!reusable)listeners.abort();
    },{signal:listeners.signal});
    form.onsubmit=async event=>{
      event.preventDefault();if(submitting)return;
      const button=form.querySelector('button[type=submit]'),errorLine=form.querySelector('[role=alert]');
      const closeButtons=[...dialog.querySelectorAll('.close-button,[data-action="close-modal"]'),...form.querySelectorAll('button[type=button]')];
      const label=button.textContent;
      const unlock=()=>{submitting=false;form.setAttribute('aria-busy','false');button.disabled=false;closeButtons.forEach(close=>close.disabled=false);button.textContent=label;updateLoginGate();};
      submitting=true;form.setAttribute('aria-busy','true');button.disabled=true;closeButtons.forEach(close=>close.disabled=true);button.textContent='登录中…';errorLine.textContent='';
      let value;
      try{
        value=await signIn(form.querySelector('[autocomplete=username]').value,form.querySelector('[type=password]').value);
      }catch(error){if(dialog.open&&form.isConnected)errorLine.textContent=error.message;else toast(error.message);return;}
      finally{unlock();}
      dialog.close();
      try{await onSuccess(value);}catch(error){toast(error.message);}
    };
  }
  function finishLogin(value){
    resolveLogin?.(value);resolveLogin=null;loginPromise=null;
    const dialog=document.querySelector('#workspace-login');
    if(dialog){dialog.querySelector('[name=password]').value='';if(dialog.open)dialog.close();}
  }
  function updateLoginGate(){
    if(loginRequired)document.body.inert=!session;
    if(session){
      // A local submit must unlock its controls before resolving follow-up work.
      // An existing session or a login in another tab can unlock immediately.
      if(document.querySelector('#workspace-login-form')?.getAttribute('aria-busy')!=='true')finishLogin(session);
    }else if(loginRequired)void login();
  }
  function login(){
    if(session)return Promise.resolve(session);
    if(loginPromise)return loginPromise;
    let dialog=document.querySelector('#workspace-login');
    if(!dialog){
      dialog=document.createElement('dialog');dialog.id='workspace-login';dialog.className='narrow pilot-ui';
      dialog.setAttribute('aria-labelledby','workspace-login-title');dialog.setAttribute('aria-describedby','workspace-login-hint');
      dialog.innerHTML='<div class="dialog-heading"><div><div class="eyebrow">AI-SOP ACCOUNT</div><h2 id="workspace-login-title">登录检测平台</h2></div><button class="close-button" aria-label="关闭登录">×</button></div><form id="workspace-login-form" class="dialog-body login-form"><label>用户名<input name="username" autocomplete="username" autofocus required maxlength="100"></label><label>密码<input name="password" type="password" autocomplete="current-password" required maxlength="200"></label><p class="status-line danger" id="workspace-login-error" role="alert"></p><button class="primary" type="submit">登录</button><p class="hint" id="workspace-login-hint">与检测首页共用账户和会话。</p></form>';
      dialog.querySelector('.close-button').hidden=loginRequired;
      if(loginRequired)dialog.querySelector('.hint').textContent='请先登录，登录成功后才能继续使用检测平台。';
      document.body.append(dialog);
      dialog.querySelector('.close-button').onclick=()=>dialog.close();
      dialog.addEventListener('cancel',event=>{if(loginRequired&&!session)event.preventDefault();});
      bindLoginForm(dialog,dialog.querySelector('form'),finishLogin,{reusable:true});
      dialog.addEventListener('close',()=>{
        if(dialog.open)return;
        if(loginRequired&&!session){dialog.showModal();return;}
        finishLogin(session);
      });
    }
    dialog.querySelector('#workspace-login-error').textContent='';
    loginPromise=new Promise(resolve=>resolveLogin=resolve);dialog.showModal();return loginPromise;
  }
  function logout(){
    if(loggingOut)return loggingOut;
    if(signingIn)return signingIn.catch(()=>{}).then(logout);
    const started=++version;connecting=null;
    const promise=(async()=>{
      for(let attempt=0;attempt<2;attempt++){
        let current=session;
        try{current=await fetchJSON('/session');}catch(error){if(error.status!==401)throw error;}
        try{await fetchJSON(current?.role==='terminal'?'/terminal/logout':'/logout',{method:'POST'},current?.csrf);break;}
        catch(error){if(error.code==='SESSION_CSRF_INVALID'&&attempt===0)continue;throw error;}
      }
      if(started===version)setSession(null,true);
      if(window.AISOP_TERMINAL)location.assign('/terminal');
    })();
    loggingOut=promise;
    const clear=()=>{if(loggingOut===promise)loggingOut=null;};
    promise.then(clear,clear);return promise;
  }
  function upload(path,file,onProgress=()=>{},options={}){
    return new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest(),{signal,headers={},timeout=600000}=options,started=version;
      xhr.open('POST','/api/v1'+path);xhr.timeout=timeout;xhr.withCredentials=true;
      xhr.setRequestHeader('X-CSRF-Token',session?.csrf||'');xhr.setRequestHeader('Content-Type','application/octet-stream');
      for(const [key,value] of Object.entries(headers))xhr.setRequestHeader(key,value);
      const abort=()=>xhr.abort();
      const done=(fn,value)=>{signal?.removeEventListener('abort',abort);fn(value);};
      const failure=message=>{const e=new Error(message);e.status=xhr.status;return e;};
      xhr.upload.onprogress=event=>{if(event.lengthComputable)onProgress(Math.round(event.loaded/event.total*100));};
      xhr.onload=async()=>{
        let data;try{data=JSON.parse(xhr.responseText);}catch{}
        if(xhr.status<200||xhr.status>=300){
          const error=responseError(xhr.status,data,xhr.getResponseHeader('Retry-After'));
          await recover(error,started);done(reject,error);return;
        }
        if(!data){done(reject,failure('服务器返回内容无效，请先检查导入结果。'));return;}
        done(resolve,data);
      };
      xhr.onerror=()=>done(reject,failure('网络连接中断，请检查服务器导入结果后重试。'));
      xhr.ontimeout=()=>done(reject,failure('上传或验证等待超时，请先检查服务器导入结果。'));
      xhr.onabort=()=>done(reject,failure('上传连接已停止；服务器可能仍在处理，请检查导入结果。'));
      if(signal?.aborted){done(reject,failure('上传已取消'));return;}
      signal?.addEventListener('abort',abort,{once:true});xhr.send(file);
    });
  }
  function account(){const button=document.querySelector('#account-button');if(button)button.textContent=session?session.username+' · 退出':'登录';}
  document.addEventListener('workspace-session',account);
  function synchronize(){
    // A tab notification may arrive while a /session response from before the
    // cookie change is still pending. Supersede that read instead of joining it.
    const refresh=()=>{version++;connecting=null;return connect();};
    const busy=signingIn||loggingOut;
    if(busy){busy.then(refresh,refresh).catch(()=>{});return;}
    refresh().catch(()=>{});
  }
  if(channel)channel.onmessage=synchronize;
  else window.addEventListener('storage',event=>{if(event.key==='aisop-session-changed')synchronize();});
  window.addEventListener('focus',synchronize);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)synchronize();});
  if(loginRequired){
    document.body.inert=true;
    const start=()=>connect().catch(error=>{
      if(session)return;
      void login();document.querySelector('#workspace-login-error').textContent='无法确认登录状态：'+error.message+'。请登录后重试。';
    });
    // Deferred page scripts must register session listeners before the first read.
    if(document.readyState!=='complete')document.addEventListener('DOMContentLoaded',start,{once:true});else queueMicrotask(start);
  }
  return {request,fetchJSON,json,connect,signIn,bindLoginForm,login,logout,upload,escape,toast,taskLabels,get session(){return session;}};
})();
