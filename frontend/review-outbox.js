/* Durable business confirmations only. Never put camera frames in this store. */
window.ReviewOutbox = (() => {
  'use strict';
  const W = window.Workspace, databaseName = 'aisop-review-outbox';
  let opening, storageCheck, recovering = false;
  const active = new Map();
  const forgotten = new Set();
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel(databaseName) : null;
  const actorFields = ['account', 'role', 'reviewer', 'credentialRevision'];
  const sameActor = (a, b) => a && b && actorFields.every(key => (a[key] ?? null) === (b[key] ?? null));
  const path = id => '/station-streams/' + encodeURIComponent(id);
  function open() {
    if (!opening) opening = new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error('浏览器无法持久保存人工确认，请使用支持本地存储的浏览器')); return; }
      const request = indexedDB.open(databaseName, 2);
      request.onupgradeneeded = () => {
        const store = request.result.objectStoreNames.contains('reviews')
          ? request.transaction.objectStore('reviews') : request.result.createObjectStore('reviews', {keyPath:'streamId'});
        if (!store.indexNames.contains('status')) store.createIndex('status', 'status');
        if (!store.indexNames.contains('retry')) store.createIndex('retry', ['status', 'nextAttemptAt']);
      };
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('本地确认库被其他页面占用，请关闭旧页面后重试'));
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => { db.close(); opening = null; };
        resolve(db);
      };
    }).catch(error => { opening = null; throw error; });
    return opening;
  }
  async function transaction(mode, action) {
    const db = await open();
    return new Promise((resolve, reject) => {
      let tx;
      try { tx = db.transaction('reviews', mode, {durability:'strict'}); }
      catch (error) { reject(error); return; }
      let result;
      tx.oncomplete = () => resolve(result);
      tx.onabort = () => reject(tx.error || new Error('本地确认记录未能保存'));
      tx.onerror = () => {}; // The abort handler rejects after rollback.
      try { action(tx.objectStore('reviews'), value => { result = value; }); }
      catch (error) { tx.abort(); reject(error); }
    });
  }
  const get = streamId => transaction('readonly', (store, done) => {
    const request = store.get(streamId); request.onsuccess = () => done(request.result || null);
  });
  const list = ({pendingOnly=false, stationId=null}={}) => transaction('readonly', (store, done) => {
    const statuses = pendingOnly ? ['pending','submitting','blocked'] : [null];
    const entries = []; let remaining = statuses.length;
    for (const status of statuses) {
      const request = status ? store.index('status').getAll(status) : store.getAll();
      request.onsuccess = () => {
        entries.push(...request.result.filter(entry => !stationId || entry.stationId === stationId));
        if (!--remaining) done(entries.sort((a,b) => b.createdAt-a.createdAt || a.streamId.localeCompare(b.streamId)));
      };
    }
  });
  // Read only due confirmations, in bounded batches; completed receipts stay
  // available by stream ID without being cloned every five seconds.
  const due = () => transaction('readonly', (store, done) => {
    const entries = []; let remaining = 2;
    for (const status of ['pending','submitting']) {
      const request = store.index('retry').getAll(IDBKeyRange.bound([status,0],[status,Date.now()]),25);
      request.onsuccess = () => {
        entries.push(...request.result);
        if (!--remaining) done(entries.sort((a,b) => a.nextAttemptAt-b.nextAttemptAt || a.createdAt-b.createdAt).slice(0,25));
      };
    }
  });
  function dispatch(entry) { document.dispatchEvent(new CustomEvent('review-outbox', {detail:entry})); }
  function notify(entry) { dispatch(entry); channel?.postMessage(entry.streamId); }
  if (channel) channel.onmessage = async event => {
    if (event.data?.type === 'forgotten' && Array.isArray(event.data.streamIds)) {
      forgetNotice(event.data.streamIds); return;
    }
    if (typeof event.data !== 'string') return;
    try { const entry = await get(event.data); if (entry) dispatch(entry); }
    catch (error) { document.dispatchEvent(new CustomEvent('review-outbox-error', {detail:error.message})); }
  };
  function forgetNotice(streamIds) {
    streamIds.forEach(id => forgotten.add(id));
    document.dispatchEvent(new CustomEvent('inspection-data-reset', {detail:{streamIds}}));
  }
  async function forget(streamIds) {
    // The server returns only IDs actually deleted by the administrator.
    // Fence in-flight responses before deleting, so they cannot recreate receipts.
    streamIds.forEach(id => forgotten.add(id));
    await transaction('readwrite', store => { streamIds.forEach(id => store.delete(id)); });
    forgetNotice(streamIds); channel?.postMessage({type:'forgotten', streamIds});
  }
  async function save(entry) {
    if (forgotten.has(entry.streamId)) return {...entry, status:'cleared', record:null};
    entry = {...entry, updatedAt:Date.now()};
    await transaction('readwrite', (store, done) => { if (!forgotten.has(entry.streamId)) store.put(entry); done(entry); });
    if (forgotten.has(entry.streamId)) return {...entry, status:'cleared', record:null};
    notify(entry); return entry;
  }
  async function storage() {
    if (!storageCheck) storageCheck = (async () => {
      await open();
      let persistent = false;
      try { persistent = !!(await navigator.storage?.persisted()) || !!(await navigator.storage?.persist()); } catch {}
      return {persistent, message:persistent
        ? '确认记录保存在本浏览器，重启后会继续核对；请勿清除本站数据。'
        : '确认记录保存在本浏览器，但未获长期存储保护；请勿使用隐私模式、清除本站数据或更换浏览器配置。'};
    })().catch(error => { storageCheck = null; throw error; });
    return storageCheck;
  }
  async function enqueue({stationId, streamId, workstationId, outcome, reason}) {
    await storage();
    if (forgotten.has(streamId)) throw new Error('该轮检测记录已清空，请开始新一轮检测');
    // This identity comes from the authenticated server session, never a name input.
    const session = W.session;
    if (!session || session.role === 'terminal' && session.binding.stationId !== stationId)
      throw new Error('请使用有权复核本工位的身份登录');
    if (!['PASS','FAIL'].includes(outcome) || reason.trim().length > 1000) throw new Error('请核对人工结论和原因');
    const actor = {account:session.username, role:session.role || 'admin',
      reviewer:session.role === 'terminal' ? session.binding.stationId : session.username,
      credentialRevision:session.role === 'terminal' ? session.binding.revision : null};
    const entry = {streamId, stationId, workstationId:workstationId || null,
      actor, createdAt:Date.now(), updatedAt:Date.now(), status:'pending', attempts:0,
      message:'已保存本地确认，待提交', nextAttemptAt:0,
      body:{requestId:crypto.randomUUID(), outcome, reason:reason.trim(), expectedActor:actor}};
    // One immutable request per round, including concurrent browser tabs.
    const saved = await transaction('readwrite', (store, done) => {
      const existing = store.get(streamId);
      existing.onsuccess = () => {
        if (forgotten.has(streamId)) done(null);
        else if (existing.result) done(existing.result);
        else { store.add(entry); done(entry); }
      };
    });
    if (!saved) throw new Error('该轮检测记录已清空，请开始新一轮检测');
    notify(saved); return saved;
  }
  const matches = (entry, result) => result?.streamId === entry.streamId && result.stationId === entry.stationId
    && result.manualReview?.requestId === entry.body.requestId && result.manualReview.status === entry.body.outcome
    && result.manualReview.reason === entry.body.reason && sameActor(entry.actor, result.manualReview);
  async function attempt(streamId) {
    let entry = await get(streamId);
    if (!entry || entry.status === 'complete') return entry;
    // Blocked requests may be read again by the operator, but never resubmitted.
    const checkOnly = entry.status === 'blocked';
    try {
      const record = await W.request(path(streamId) + '/inspection', {cache:'no-store', timeout:10000});
      if (record.streamId !== entry.streamId || record.stationId !== entry.stationId)
        return save({...entry, status:'blocked', message:'服务器记录身份不匹配，已停止自动重试'});
      if (matches(entry, record.result))
        return save({...entry, status:'complete', record, message:'人工确认已保存'});
      if (record.result?.manualReview || !record.reviewAllowed || !sameActor(entry.actor, record.reviewActor) || checkOnly)
        return save({...entry, status:'blocked', record, message:record.result?.manualReview
          ? '该轮已有其他确认，已停止自动重试，请核对服务器结果'
          : '确认权限、身份或轮次状态已变化，已停止自动重试'});
      entry = await save({...entry, status:'submitting', record:null, attempts:entry.attempts + 1, message:'提交中…'});
      const result = await W.request(path(streamId) + '/manual-review', {method:'POST', timeout:15000,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify(entry.body)});
      if (!matches(entry, result))
        return save({...entry, status:'blocked', record:{...record, result}, message:'服务器确认内容不一致，已停止自动重试'});
      return save({...entry, status:'complete', record:{...record, result, reviewAllowed:false}, message:'人工确认已保存'});
    } catch (error) {
      const authentication = error.status === 401 || error.code === 'SESSION_CSRF_INVALID';
      const blocked = !authentication && error.status >= 400 && error.status < 500 && ![408, 425, 429].includes(error.status);
      const message = ['AbortError','TimeoutError'].includes(error.name) ? '请求超时或连接中断'
        : error instanceof TypeError ? '网络连接中断' : error.message;
      let record = error.record || entry.record;
      if (blocked && !error.record) {
        try { record = await W.request(path(streamId) + '/inspection', {cache:'no-store', timeout:10000}); } catch {}
      }
      return save({...entry, status:blocked || checkOnly ? 'blocked' : 'pending', record,
        nextAttemptAt:authentication ? 0 : Date.now() + Math.max(error.retryAfterMs || 0, Math.min(60000, 2000 * 2 ** Math.min(entry.attempts, 5))),
        message:blocked || checkOnly ? '已停止自动重试：' + message
          : authentication ? '等待登录状态恢复，原确认已保留；恢复后先核对服务器记录。'
          : '待核对：' + message + '。服务器可能已保存，将保留原请求核对。'});
    }
  }
  function reconcile(streamId) {
    if (active.has(streamId)) return active.get(streamId);
    const work = navigator.locks
      ? navigator.locks.request(databaseName + ':' + streamId, () => attempt(streamId)) : attempt(streamId);
    active.set(streamId, work);
    const clear = () => active.delete(streamId); work.then(clear, clear); return work;
  }
  async function recover() {
    if (recovering || !W.session || !navigator.onLine) return;
    recovering = true;
    try {
      for (const entry of await due()) {
        if (!W.session || !navigator.onLine) break;
        if (!['pending','submitting'].includes(entry.status) || entry.nextAttemptAt > Date.now()) continue;
        await reconcile(entry.streamId);
      }
    } catch (error) { document.dispatchEvent(new CustomEvent('review-outbox-error', {detail:error.message})); }
    finally { recovering = false; }
  }
  window.addEventListener('online', recover);
  document.addEventListener('workspace-session', recover);
  setInterval(recover, 5000);
  return {enqueue, get, list, reconcile, recover, storage, matches, forget};
})();
