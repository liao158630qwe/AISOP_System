/* Persist only operator commands. Camera capture belongs to the registered collector. */
window.StationCollectorControl = (() => {
  'use strict';
  const W = window.Workspace, database = 'aisop-collector-commands', active = new Map();
  let opening;
  const pending = entry => !!entry && !['complete','failed','blocked'].includes(entry.status);
  const actor = () => {
    const session = W.session;
    if (!session) throw new Error('请先登录服务器');
    return {account:session.username,role:session.role || 'admin',
      reviewer:session.role === 'terminal' ? session.binding.stationId : session.username,
      credentialRevision:session.role === 'terminal' ? session.binding.revision : null};
  };
  const sameActor = (a,b) => a && b && ['account','role','reviewer','credentialRevision'].every(key => (a[key] ?? null) === (b[key] ?? null));
  const key = station => JSON.stringify([actor(),station]);
  const path = station => '/stations/' + encodeURIComponent(station) + '/collector';
  function open() {
    if (!opening) opening = new Promise((resolve,reject) => {
      if (!window.indexedDB) return reject(new Error('浏览器无法保存控制请求，请使用支持本地存储的浏览器'));
      const request = indexedDB.open(database,1);
      request.onupgradeneeded = () => request.result.createObjectStore('commands',{keyPath:'key'});
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('控制请求库被旧页面占用，请关闭旧页面后重试'));
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => {db.close(); opening = null;};
        resolve(db);
      };
    }).catch(error => {opening = null; throw error;});
    return opening;
  }
  async function transaction(mode, action) {
    const db = await open();
    return new Promise((resolve,reject) => {
      let tx, result;
      try {tx = db.transaction('commands',mode,{durability:'strict'});}
      catch(error) {reject(error); return;}
      tx.oncomplete = () => resolve(result);
      tx.onabort = () => reject(tx.error || new Error('控制请求未能保存到本地'));
      tx.onerror = () => {};
      try {action(tx.objectStore('commands'),value => {result = value;});}
      catch(error) {tx.abort(); reject(error);}
    });
  }
  const get = station => {
    if (!W.session) return Promise.resolve(null);
    const identity = key(station);
    return transaction('readonly',(store,done) => {const request=store.get(identity); request.onsuccess=()=>done(request.result || null);});
  };
  const save = entry => transaction('readwrite',(store,done) => {
    const request = store.get(entry.key);
    request.onsuccess = () => {
      if (request.result && request.result.body.requestId !== entry.body.requestId) {done(request.result); return;}
      store.put({...entry,updatedAt:Date.now()}); done(entry);
    };
  });
  async function enqueue(station,binding,action,streamId,reason='',expected) {
    const identity = key(station), authenticated = actor();
    if (authenticated.role === 'terminal' && authenticated.reviewer !== station) throw new Error('当前账号不能控制此工位');
    const entry = {key:identity,station,actor:authenticated,bindingRevision:binding.revision,status:'pending',missing:true,
      createdAt:Date.now(),message:'控制请求已保存在本地，等待提交',
      body:{requestId:crypto.randomUUID(),action,streamId:streamId || null,reason:reason.trim(),
        expectedInstanceId:expected?.instanceId,expectedRuntimeId:expected?.runtimeId,expectedBindingRevision:binding.revision,expectedActor:authenticated}};
    if (!entry.body.expectedInstanceId || !entry.body.expectedRuntimeId) throw new Error('后台采集身份尚未核对，请刷新后台状态');
    return transaction('readwrite',(store,done) => {
      const request = store.get(identity);
      request.onsuccess = () => {
        if (pending(request.result)) done(request.result);
        else {store.put(entry); done(entry);}
      };
    });
  }
  async function reconcile(station,{submit=false}={}) {
    if (!W.session) return null;
    const identity = key(station);
    if (active.has(identity)) return active.get(identity);
    const work = async () => {
      let entry = await get(station);
      if (!pending(entry)) return entry;
      if (!sameActor(entry.actor,actor())) throw new Error('登录身份已变化，请使用原身份核对控制请求');
      try {
        let command;
        try {command = await W.request(path(station)+'/commands/'+entry.body.requestId,{cache:'no-store',timeout:5000});}
        catch(error) {if (error.status !== 404) throw error;}
        if (!command && submit) {
          const current = await W.request(path(station),{cache:'no-store',timeout:5000});
          if (current.instanceId !== entry.body.expectedInstanceId || current.runtimeId !== entry.body.expectedRuntimeId ||
              current.bindingRevision !== entry.body.expectedBindingRevision) {
            return save({...entry,status:'blocked',missing:false,message:'采集进程、服务或工位配置已变化，旧请求不会执行；请核对工件后重新开始'});
          }
          if (!current.online) return save({...entry,status:'pending',missing:true,message:'后台采集器离线，保留原请求；恢复后先核对工件再重试'});
          entry = await save({...entry,status:'submitting',missing:true,message:'正在提交控制请求…'});
          if (!W.session || !sameActor(entry.actor,actor()))
            return save({...entry,status:'blocked',missing:false,message:'登录身份已变化，原控制请求不会转交给其他账号执行'});
          const status = await W.request(path(station)+'/commands',{method:'POST',timeout:15000,
            headers:{'Content-Type':'application/json'},body:JSON.stringify(entry.body)});
          command = status.command?.requestId === entry.body.requestId ? status.command
            : await W.request(path(station)+'/commands/'+entry.body.requestId,{cache:'no-store',timeout:5000});
        }
        if (!command) return save({...entry,status:'pending',missing:true,message:'待核对：服务器尚未找到此请求。可使用原请求重试，不能另开一轮。'});
        const matches = ['requestId','action','streamId','reason'].every(field => (command[field] ?? '') === (entry.body[field] ?? ''));
        if (!matches || command.actor && !sameActor(command.actor,entry.actor))
          return save({...entry,status:'blocked',missing:false,message:'服务器请求内容或操作身份不一致，已停止重试'});
        return save({...entry,command,missing:false,status:command.status === 'SUCCEEDED' ? 'complete' : command.status === 'FAILED' ? 'failed' : 'accepted',
          message:command.status === 'SUCCEEDED' ? '控制操作已完成' : command.status === 'FAILED' ? '操作未完成：'+(command.error || '请核对检测轮次') : '采集器正在执行，等待保存结果'});
      } catch(error) {
        const rejected = [400,401,403,409,410,422].includes(error.status);
        return save({...entry,status:rejected ? 'blocked' : 'pending',message:(rejected ? '控制请求被拒绝：' : '待核对：')+error.message});
      }
    };
    const promise = navigator.locks ? navigator.locks.request(database+':'+identity,work) : work();
    active.set(identity,promise);
    try {return await promise;} finally {active.delete(identity);}
  }
  async function execute(station,binding,action,streamId,reason='',expected) {
    const entry = await enqueue(station,binding,action,streamId,reason,expected);
    if (entry.body.action !== action || (entry.body.streamId || null) !== (streamId || null))
      throw new Error('上一次控制请求仍待核对，请先核对原请求');
    return reconcile(station,{submit:true});
  }
  return {get,pending,reconcile,execute,path};
})();
