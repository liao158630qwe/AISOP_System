/* Administrator-only, explicit deletion of selected or all detection history. */
window.BoardAdvanced = (() => {
  'use strict';
  const $ = selector => document.querySelector(selector), W = window.Workspace;
  const phrase = '清空全部检测数据';
  let config, records, summary = null, busy = false, loading = false, epoch = 0;
  const working = () => busy || !!records?.busy;
  function message(text, kind='info') {
    const node = $('#advanced-message'); node.textContent = text; node.dataset.kind = kind; node.hidden = !text;
  }
  function controls() {
    const allowed = summary && summary.total > 0 && !summary.active && !summary.activeStations.length;
    $('#advanced-submit').disabled = busy || loading || !allowed || !W.session || W.session.role === 'terminal'
      || $('#advanced-confirmation').value !== phrase || !$('#advanced-password').value;
    $('#advanced-submit').textContent = busy ? '正在清空…' : '确认清空全部检测数据';
    for (const id of ['advanced-refresh','advanced-confirmation','advanced-password']) $('#' + id).disabled = busy;
    $('#advanced-refresh').disabled = busy || loading;
    for (const id of ['advanced-close','advanced-mode-records','advanced-mode-all']) $('#' + id).disabled = working();
  }
  function counts(value) {
    $('#advanced-total').textContent = value ? value.total.toLocaleString('zh-CN') : '—';
    $('#advanced-recycled').textContent = value ? value.recycled.toLocaleString('zh-CN') : '—';
    $('#advanced-active').textContent = value ? Math.max(value.active, value.activeStations.length) : '—';
  }
  async function refresh() {
    if (busy) return;
    const version = ++epoch; summary = null; loading = true; counts(null); message('正在核对所有日期的检测数据…'); controls();
    $('#advanced-confirmation').value = ''; $('#advanced-password').value = '';
    try {
      const result = await W.request('/board/reset', {cache:'no-store', timeout:15000});
      if (version !== epoch) return;
      summary = result; counts(result);
      message(result.active || result.activeStations.length ? '仍有检测正在运行，请先结束所有工位检测，再刷新清空范围。'
        : result.total ? '' : '当前没有检测记录，无需清空。', result.active || result.activeStations.length ? 'error' : 'info');
    } catch (error) { if (version === epoch) message('读取清空范围失败：' + error.message, 'error'); }
    finally { if (version === epoch) { loading = false; controls(); } }
  }
  async function switchMode(mode) {
    if (working()) return;
    epoch++; loading = false; summary = null;
    $('#advanced-password').value = ''; $('#advanced-confirmation').value = '';
    records.reset();
    $('#advanced-dialog').dataset.mode = mode;
    for (const value of ['records','all']) {
      $('#advanced-' + value + '-panel').hidden = value !== mode;
      $('#advanced-mode-' + value).setAttribute('aria-pressed', String(value === mode));
    }
    controls();
    if (mode === 'records') await records.open(); else await refresh();
  }
  async function open() {
    if ($('#advanced-dialog').open) return;
    if (config.getMode() !== 'live') { config.toast('请先切换到实时数据，再使用高级功能。'); return; }
    if (!W.session || W.session.role === 'terminal') { config.toast('请使用管理员账号登录后操作。'); return; }
    $('#advanced-dialog').showModal(); await switchMode('records');
  }
  async function submit(event) {
    event.preventDefault(); controls();
    if ($('#advanced-submit').disabled || !$('#advanced-form').reportValidity()) return;
    const version = epoch, revision = summary.revision;
    const body = {confirmation:phrase, revision, password:$('#advanced-password').value};
    busy = true; message('正在清空检测记录，请等待服务器确认。'); controls();
    try {
      const result = await W.request('/board/reset', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body), timeout:30000});
      if (version !== epoch) return;
      let localError = '';
      try { await window.ReviewOutbox.forget(result.streamIds); }
      catch { localError = ' 本浏览器旧确认队列清理失败，重新打开检测页面后会核对服务器状态。'; }
      summary = {...summary, total:0, recycled:0, active:0, activeStations:[]}; counts(summary);
      $('#advanced-confirmation').value = '';
      message(`已清空 ${result.deleted} 条检测记录，统计、趋势和违规告警已重置。${localError}`);
      config.onChanged();
    } catch (error) {
      if (version !== epoch) return;
      summary = null;
      message('清空未确认完成：' + error.message + '。请刷新清空范围核对结果，再决定是否重试。', 'error');
      config.onChanged();
    } finally {
      if (version === epoch) { busy = false; $('#advanced-password').value = ''; controls(); }
    }
  }
  function reset() {
    epoch++; summary = null; busy = loading = false;
    records.reset();
    $('#advanced-password').value = ''; $('#advanced-confirmation').value = '';
    if ($('#advanced-dialog').open) $('#advanced-dialog').close();
  }
  function init(options) {
    config = options;
    records = window.BoardRecordDelete.init({...options, onState:controls});
    const entry = $('#advanced-open');
    let lastKeyboardClick = null;
    entry.ondblclick = open;
    // Native keyboard/assistive clicks have detail=0 and no dblclick event.
    // Require two deliberate activations there as well as with the pointer.
    entry.onclick = event => {
      if (event.detail !== 0) { lastKeyboardClick = null; return; }
      const now = performance.now();
      if (lastKeyboardClick !== null && now-lastKeyboardClick <= 500) { lastKeyboardClick = null; void open(); }
      else lastKeyboardClick = now;
    };
    entry.onblur = () => { lastKeyboardClick = null; };
    $('#advanced-refresh').onclick = refresh; $('#advanced-form').onsubmit = submit;
    for (const mode of ['records','all']) $('#advanced-mode-' + mode).onclick = () => switchMode(mode);
    $('#advanced-close').onclick = () => { if (!working()) $('#advanced-dialog').close(); };
    for (const id of ['advanced-confirmation','advanced-password']) $('#' + id).oninput = controls;
    $('#advanced-dialog').addEventListener('cancel', event => { if (working()) event.preventDefault(); });
    $('#advanced-dialog').addEventListener('close', () => { if (!$('#advanced-dialog').open) { reset(); config.onClose(); } });
    document.addEventListener('workspace-session', reset);
    window.addEventListener('pagehide', reset);
  }
  return {init};
})();

window.BoardRecordDelete = (() => {
  'use strict';
  const $ = selector => document.querySelector(selector), W = window.Workspace, E = W.escape;
  const LIMIT = 20, MAX = 200;
  const filters = ['advanced-date','advanced-station','advanced-verdict','advanced-record-scope'];
  const labels = {PASS:'合规', FAIL:'不合规', REVIEW:'待复核'};
  const timeFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
  const stamp = row => timeFormat.format(new Date((row.finishedAt ?? row.startedAt) * 1000));
  const name = row => row.stationName || row.stationId;
  let config, request, epoch = 0, busy = false, loading = false, confirming = false;
  let rows = [], page = null, offset = 0;
  const selected = new Map();
  function message(text = '', kind = 'info') {
    const node = $('#advanced-record-message'); node.textContent = text; node.dataset.kind = kind; node.hidden = !text;
  }
  function controls() {
    const frozen = busy || confirming;
    for (const id of filters) $('#' + id).disabled = frozen;
    $('#advanced-record-refresh').disabled = frozen || loading;
    $('#advanced-record-prev').disabled = frozen || loading || !page || offset === 0;
    $('#advanced-record-next').disabled = frozen || loading || !page?.hasMore;
    $('#advanced-selected-count').textContent = `已选择 ${selected.size} 条`;
    $('#advanced-selection-clear').disabled = frozen || !selected.size;
    $('#advanced-delete-selected').disabled = frozen || loading || !page || !selected.size;
    $('#advanced-selected-form').hidden = !confirming;
    $('#advanced-selection-actions').hidden = confirming;
    $('#advanced-selected-cancel').disabled = busy;
    $('#advanced-selected-password').disabled = busy;
    $('#advanced-selected-attest').disabled = busy;
    $('#advanced-selected-submit').disabled = busy || !confirming || !selected.size || !W.session
      || W.session.role === 'terminal' || !$('#advanced-selected-attest').checked || !$('#advanced-selected-password').value;
    $('#advanced-selected-submit').textContent = busy ? '正在删除…' : `确认删除 ${selected.size} 条记录`;
    const count = rows.filter(row => selected.has(row.streamId)).length;
    $('#advanced-select-page').checked = rows.length > 0 && count === rows.length;
    $('#advanced-select-page').indeterminate = count > 0 && count < rows.length;
    $('#advanced-select-page').disabled = frozen || loading || !rows.length || (!count && selected.size >= MAX);
    $('#advanced-record-list').querySelectorAll('[data-select-record]').forEach(input => {
      input.checked = selected.has(input.dataset.selectRecord);
      input.disabled = frozen || loading || (!input.checked && selected.size >= MAX);
      input.closest('.advanced-record').classList.toggle('is-selected', input.checked);
    });
    config.onState();
  }
  function render() {
    $('#advanced-record-list').innerHTML = rows.length ? rows.map(row => {
      const verdict = labels[row.verdict] ? row.verdict : 'REVIEW';
      return `<label class="advanced-record"><input type="checkbox" data-select-record="${E(row.streamId)}"
        aria-label="选择${E(name(row))} ${E(stamp(row))}的记录 ${E(row.streamId.slice(0,8))}">
        <span class="advanced-record-content"><span class="advanced-record-top"><strong>${E(name(row))}</strong>
        <span class="history-badge" data-verdict="${verdict}">${labels[verdict]}</span><time>${E(stamp(row))}</time>
        ${row.reviewed ? '<span class="advanced-record-reviewed">人工复核</span>' : ''}</span>
        <span class="advanced-record-reason">${E(row.reason || row.label || '未记录判定原因')}</span>
        <span class="advanced-record-meta">${E(row.taskLabel || row.analysisType || '检测任务')} · ${E(row.sourceLabel || '来源未记录')}
        ${row.status === 'INTERRUPTED' ? ' · 检测中断' : ''} · <span title="${E(row.streamId)}">${E(row.streamId.slice(0,8))}</span></span></span></label>`;
    }).join('') : '<p class="advanced-record-empty">暂无匹配记录，可切换日期、工位、检测结论或记录位置。</p>';
    $('#advanced-record-page').textContent = page.total ? `共 ${page.total} 条 · 第 ${Math.floor(offset/LIMIT)+1} 页 · 显示 ${offset+1}–${offset+rows.length} 条` : '共 0 条记录';
  }
  async function load({keepMessage = false} = {}) {
    if (busy || confirming || !$('#advanced-dialog').open || $('#advanced-records-panel').hidden) return;
    const version = ++epoch;
    request?.abort(); request = new AbortController();
    rows = []; page = null; loading = true;
    if (!keepMessage) message();
    $('#advanced-record-list').setAttribute('aria-busy', 'true');
    $('#advanced-record-list').innerHTML = '<p class="advanced-record-empty">正在加载记录…</p>';
    $('#advanced-record-page').textContent = '正在读取…'; controls();
    try {
      if (!$('#advanced-date').checkValidity()) throw new Error('请选择有效的归档日期');
      const params = new URLSearchParams({date:$('#advanced-date').value, verdict:$('#advanced-verdict').value,
        deleted:String($('#advanced-record-scope').value === 'deleted'), limit:String(LIMIT), offset:String(offset)});
      if ($('#advanced-station').value) params.set('stationId', $('#advanced-station').value);
      const result = await W.request('/board/history?' + params, {cache:'no-store', signal:request.signal, timeout:15000});
      if (version !== epoch) return;
      if (offset && offset >= result.total) {
        offset = Math.max(0, Math.floor((result.total-1)/LIMIT)*LIMIT);
        return await load({keepMessage});
      }
      page = result; rows = result.items; render();
    } catch (error) {
      if (version !== epoch) return;
      $('#advanced-record-list').innerHTML = '<p class="advanced-record-empty">记录读取失败，请刷新列表重试。</p>';
      $('#advanced-record-page').textContent = '未能读取记录';
      message('读取记录失败：' + error.message, 'error');
    } finally {
      if (version === epoch) { loading = false; $('#advanced-record-list').setAttribute('aria-busy','false'); controls(); }
    }
  }
  function cancelConfirmation() {
    confirming = false; $('#advanced-selected-password').value = ''; $('#advanced-selected-attest').checked = false;
    controls();
  }
  function confirm() {
    if (busy || loading || !selected.size || !page) return;
    confirming = true;
    $('#advanced-selected-heading').textContent = `确认永久删除 ${selected.size} 条记录`;
    // Keep every chosen identity reviewable, including selections on other pages.
    $('#advanced-selected-preview').innerHTML = [...selected.values()].map(row =>
      `<li>${E(name(row))} · ${E(stamp(row))} · ${E(labels[row.verdict] || '待复核')} · ${E(row.streamId.slice(0,8))}</li>`).join('');
    $('#advanced-selected-attest').checked = false; $('#advanced-selected-password').value = '';
    message(); controls(); $('#advanced-selected-attest').focus();
  }
  async function submit(event) {
    event.preventDefault(); controls();
    if ($('#advanced-selected-submit').disabled || !$('#advanced-selected-form').reportValidity()) return;
    const version = epoch, identities = [...selected.keys()];
    const body = {confirmation:'删除选中检测记录', streamIds:identities, password:$('#advanced-selected-password').value};
    busy = true; message(`正在删除所选 ${identities.length} 条记录，请等待服务器确认。`); controls();
    let completed = false;
    try {
      const result = await W.request('/board/reset/selected', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body), timeout:30000});
      if (version !== epoch) return;
      let localError = '';
      try { await window.ReviewOutbox.forget(result.streamIds); }
      catch { localError = ' 本浏览器确认队列清理失败，重新打开检测页面后会核对服务器状态。'; }
      if (version !== epoch) return;
      selected.clear(); confirming = false; completed = true;
      message(`已删除 ${result.deleted} 条记录${result.alreadyDeleted ? `，另有 ${result.alreadyDeleted} 条已被删除` : ''}，对应统计和违规告警已更新。${localError}`);
      config.onChanged();
    } catch (error) {
      if (version !== epoch) return;
      message('删除未确认完成：' + error.message + '。可取消后刷新列表核对结果，再决定是否重试。', 'error');
      config.onChanged();
    } finally {
      if (version === epoch) { busy = false; $('#advanced-selected-password').value = ''; controls(); }
    }
    if (completed && version === epoch) await load({keepMessage:true});
  }
  function reset() {
    epoch++; request?.abort(); request = null; busy = loading = confirming = false;
    rows = []; page = null; offset = 0; selected.clear();
    $('#advanced-selected-password').value = ''; $('#advanced-selected-attest').checked = false;
    $('#advanced-selected-preview').innerHTML = ''; message(); controls();
  }
  function open() {
    $('#advanced-date').value = config.getDate?.() || new Date(Date.now()+8*3600000).toISOString().slice(0,10);
    $('#advanced-station').value = ''; $('#advanced-verdict').value = 'ALL'; $('#advanced-record-scope').value = 'active';
    return load();
  }
  function init(options) {
    config = options;
    for (const id of filters) $('#' + id).onchange = () => {
      if (busy || confirming) return;
      selected.clear(); offset = 0; void load();
    };
    $('#advanced-record-refresh').onclick = () => { if (!busy && !confirming) { selected.clear(); void load(); } };
    $('#advanced-record-prev').onclick = () => { offset = Math.max(0,offset-LIMIT); void load(); };
    $('#advanced-record-next').onclick = () => { offset += LIMIT; void load(); };
    $('#advanced-selection-clear').onclick = () => { selected.clear(); controls(); };
    $('#advanced-record-list').onchange = event => {
      const input = event.target.closest('[data-select-record]');
      if (!input || busy || loading || confirming) return;
      const row = rows.find(item => item.streamId === input.dataset.selectRecord);
      if (!row) return;
      if (!input.checked) selected.delete(row.streamId);
      else if (selected.size < MAX) selected.set(row.streamId, row);
      else message(`每次最多选择 ${MAX} 条记录。`, 'error');
      controls();
    };
    $('#advanced-select-page').onchange = event => {
      if (busy || loading || confirming) return;
      for (const row of rows) {
        if (!event.target.checked) selected.delete(row.streamId);
        else if (selected.size < MAX) selected.set(row.streamId, row);
      }
      if (event.target.checked && rows.some(row => !selected.has(row.streamId))) message(`每次最多选择 ${MAX} 条记录。`, 'error');
      controls();
    };
    $('#advanced-delete-selected').onclick = confirm;
    $('#advanced-selected-cancel').onclick = () => { if (!busy) { cancelConfirmation(); $('#advanced-delete-selected').focus(); } };
    $('#advanced-selected-form').onsubmit = submit;
    $('#advanced-selected-password').oninput = controls;
    $('#advanced-selected-attest').onchange = controls;
    return {open, reset, get busy() { return busy; }};
  }
  return {init};
})();
