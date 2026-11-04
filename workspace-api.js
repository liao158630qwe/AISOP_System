/* Shared, same-origin session and request handling for management and scene consoles. */
window.Workspace=(()=>{
  let session=null,loginPromise=null,resolveLogin=null;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const taskLabels={detect:'目标检测',segment:'实例分割',pose:'关键点检测',obb:'旋转框检测',classify:'图像分类'};
  function changed(){document.dispatchEvent(new CustomEvent('workspace-session',{detail:session}));}
  async function request(path,options={}){
    const response=await fetch('/api/v1'+path,{...options,headers:{...options.headers,...(session?{'X-CSRF-Token':session.csrf}:{})},signal:options.signal||AbortSignal.timeout(options.timeout||35000)});
    const data=await response.json().catch(()=>({message:'服务器返回内容无效'}));
    if(!response.ok){if(response.status===401&&session){session=null;changed();}const error=new Error(data.message||data.detail||`请求失败（${response.status}）`);error.status=response.status;throw error;}
    return data;
  }
  const json=(path,method,value)=>request(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(value),timeout:120000});
  async function connect(){try{session=await request('/session');}catch(error){if(error.status!==401)throw error;session=null;}changed();return session;}
  function toast(message){let box=document.querySelector('#workspace-toast');if(!box){box=document.createElement('div');box.id='workspace-toast';box.className='toast';box.setAttribute('role','status');document.body.append(box);}box.textContent=message;box.hidden=false;clearTimeout(box.timer);box.timer=setTimeout(()=>box.hidden=true,4500);}
  function login(){
    if(session)return Promise.resolve(session);
    if(loginPromise)return loginPromise;
    let dialog=document.querySelector('#workspace-login');
    if(!dialog){
      dialog=document.createElement('dialog');dialog.id='workspace-login';dialog.className='narrow';
      dialog.innerHTML='<div class="dialog-heading"><div><div class="eyebrow">AI-SOP ACCOUNT</div><h2>登录检测平台</h2></div><button class="close-button" aria-label="关闭登录">×</button></div><form id="workspace-login-form" class="dialog-body login-form"><label>用户名<input name="username" autocomplete="username" required maxlength="100"></label><label>密码<input name="password" type="password" autocomplete="current-password" required maxlength="200"></label><p class="status-line danger" id="workspace-login-error" role="alert"></p><button class="primary" type="submit">登录</button><p class="hint">与检测首页共用账户和会话。</p></form>';
      document.body.append(dialog);dialog.querySelector('.close-button').onclick=()=>dialog.close();
      dialog.addEventListener('close',()=>{resolveLogin?.(null);resolveLogin=null;loginPromise=null;dialog.querySelector('[name=password]').value='';});
      dialog.querySelector('form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button[type=submit]');button.disabled=true;dialog.querySelector('#workspace-login-error').textContent='';try{session=await json('/login','POST',{username:form.username.value,password:form.password.value});changed();resolveLogin?.(session);resolveLogin=null;dialog.close();}catch(error){dialog.querySelector('#workspace-login-error').textContent=error.message;}finally{button.disabled=false;}};
    }
    loginPromise=new Promise(resolve=>resolveLogin=resolve);dialog.showModal();return loginPromise;
  }
  async function logout(){await request('/logout',{method:'POST'});session=null;changed();}
  function upload(path,file,onProgress=()=>{}){return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/v1'+path);xhr.setRequestHeader('X-CSRF-Token',session?.csrf||'');xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.upload.onprogress=event=>{if(event.lengthComputable)onProgress(Math.round(event.loaded/event.total*100));};xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);if(xhr.status<200||xhr.status>=300){if(xhr.status===401&&session){session=null;changed();}const error=new Error(data.message||'上传失败');error.status=xhr.status;throw error;}resolve(data);}catch(error){reject(error);}};xhr.onerror=()=>reject(new Error('上传连接中断，请重试'));xhr.onabort=()=>reject(new Error('上传已取消'));xhr.send(file);});}
  function account(){const button=document.querySelector('#account-button');if(button)button.textContent=session?session.username+' · 退出':'登录';}
  document.addEventListener('workspace-session',account);
  return {request,json,connect,login,logout,upload,escape,toast,taskLabels,get session(){return session;}};
})();
