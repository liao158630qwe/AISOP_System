/* Persistent daily inspection records. Dates always follow UTC+08:00. */
window.BoardHistory = (() => {
  'use strict';
  const $ = selector => document.querySelector(selector), W = window.Workspace, E = W.escape;
  const labels = {FAIL:'不合规', PASS:'合规', REVIEW:'待复核', ALL:'全部'};
  const states = {RUNNING:'检测中', FINISHED:'已归档', INTERRUPTED:'已中断 · 待复核'};
  const sources = {device:'设备采集', camera:'现场摄像头', replay:'视频联调', image:'图片联调'};
  const tasks = {screw:'螺丝专项检测', sticker:'贴条专项检测', scene:'场景工艺检测', pose:'人体姿态检测', manual:'人工检查'};
  const targetStates = {PRESENT:'已确认', EMPTY:'可见空孔', ACTION_DONE:'动作已确认', UNKNOWN:'待确认', NOT_OBSERVED:'未观察到'};
  const fullTime = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
  const shortTime = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
  const today = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
  const count = value => Number(value || 0).toLocaleString('zh-CN');
  const stamp = (value, full = true) => {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? '—' : (full ? fullTime : shortTime).format(date);
  };
  let config, initialized = false, epoch = 0, listVersion = 0, detailVersion = 0;
  let listRequest, detailRequest, mutationRequest, busy = false, loading = false;
  let rows = [], page = null, detailItem = null, deleteItem = null, lastVerdict = 'FAIL';
  let state = {date:today(), station:'', verdict:'FAIL', deleted:false, offset:0};
  const LIMIT = 20;
  const verdict = row => ['PASS','FAIL'].includes(row.verdict) ? row.verdict : 'REVIEW';
  const stationName = row => row.stationName || String(row.stationId || '').replace('station-', '工位') || '工位未记录';
  const archiveTime = row => row.finishedAt ?? row.startedAt;
  const taskName = row => row.taskLabel || tasks[row.analysisType] || row.analysisType || '任务未记录';
  const sourceName = row => row.sourceLabel || sources[row.inputSource] || '来源未记录';
  const accessible = () => !!W.session && config.getMode() === 'live';
  const errorText = error => typeof error?.message === 'string' ? error.message : '请求失败，请稍后重试';

  function message(text = '', kind = '') {
    $('#history-message').textContent = text;
    $('#history-message').dataset.kind = kind;
    $('#history-message').hidden = !text;
  }
  function empty(title, description, retry = false) {
    $('#history-list').innerHTML = `<div class="history-empty"><span class="history-empty-symbol" aria-hidden="true">${retry ? '!' : '◎'}</span><strong>${E(title)}</strong><p>${E(description)}</p>${retry ? '<button id="history-retry">重新加载</button>' : ''}</div>`;
    $('#history-retry')?.addEventListener('click', () => load());
  }
  function clearSummary() {
    document.querySelectorAll('[data-history-count]').forEach(el => { el.textContent = '—'; });
    $('#history-trash-count').textContent = '—';
  }
  function controls() {
    const day = today();
    $('#history-date').max = day;
    $('#history-date').value = state.date;
    $('#history-station').value = state.station;
    $('#history-trash').setAttribute('aria-pressed', String(state.deleted));
    document.querySelectorAll('[data-history-verdict]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.historyVerdict === state.verdict));
    });
    $('#history-dialog').querySelectorAll('button, input, select').forEach(el => { el.disabled = busy; });
    $('#history-next-day').disabled = busy || state.date >= day;
    $('#history-today').disabled = busy || state.date === day;
    $('#history-prev-day').disabled = busy || state.date <= '0001-01-01';
    $('#history-prev-page').disabled = busy || loading || !page || !state.offset;
    $('#history-next-page').disabled = busy || loading || !page?.hasMore;
    $('#history-close-detail').disabled = busy;
    $('#history-detail').querySelectorAll('button').forEach(el => { el.disabled = busy; });
    $('#history-confirm-dialog').querySelectorAll('button').forEach(el => { el.disabled = busy; });
    $('#history-confirm-delete').textContent = busy && deleteItem ? '正在移入…' : '移入回收站';
    $('#history-list-title').textContent = (state.deleted ? '回收站 · ' : '') + labels[state.verdict] + '记录 · ' + (state.verdict === 'ALL' ? '不合规优先，同类按最新归档排序' : '最近归档优先');
    const rate = page?.summary?.complianceRate;
    $('#history-summary-note').textContent = '有效归档合规率 ' + (rate == null || !Number.isFinite(Number(rate)) ? '—' : Number(rate).toFixed(1) + '%') + ' · 待复核不计入';
  }
  function recordHTML(row) {
    const v = verdict(row), id = E(row.streamId);
    const reason = E(row.reason || row.label || '暂无判定说明，请查看详情');
    return `<article class="history-record${state.deleted ? ' deleted' : ''}" data-history-id="${id}" data-verdict="${v}">
      <details class="history-record-details"><summary class="history-record-summary" title="展开或收起记录">
      <span class="history-record-top"><span class="history-badge" data-verdict="${v}">${labels[v]}</span><strong>${E(stationName(row))}</strong><time title="${E(stamp(archiveTime(row)))}${row.finishedAt == null ? '（缺少结束时间，按开始时间归档）' : ''}">${E(stamp(archiveTime(row), false))}</time>${row.reviewed ? '<span class="history-reviewed">人工复核</span>' : ''}</span>
      <span class="history-record-preview">${reason}</span></summary>
      <div class="history-record-body"><p class="history-record-reason">${reason}</p>
      <div class="history-record-meta"><span>${E(taskName(row))}</span><span>${E(sourceName(row))}</span>${row.cameraId ? `<span>${E(row.cameraId)}</span>` : ''}${row.status === 'INTERRUPTED' ? '<span>检测中断</span>' : ''}${state.deleted ? `<span>删除于 ${E(stamp(row.deletedAt))}</span>` : ''}</div></div></details>
      <div class="history-record-actions"><button data-history-detail="${id}" aria-label="查看${E(stationName(row))} ${E(stamp(archiveTime(row), false))}的详情">查看详情</button>${state.deleted ? `<button data-history-restore="${id}">恢复记录</button>` : `<button data-history-delete="${id}">删除</button>`}</div></article>`;
  }
  function renderPage(result) {
    page = result; rows = Array.isArray(result.items) ? result.items : [];
    const summary = result.summary || {};
    document.querySelectorAll('[data-history-count]').forEach(el => { el.textContent = count(summary[el.dataset.historyCount]); });
    $('#history-trash-count').textContent = count(result.deletedCount);
    if (rows.length) $('#history-list').innerHTML = rows.map(recordHTML).join('');
    else empty(state.deleted ? '回收站暂无匹配记录' : state.verdict === 'FAIL' ? '当天暂无不合规记录' : '当天暂无匹配记录',
      state.deleted ? '可切换日期、工位或检测结论查看已删除记录。' : Number(summary.total) > 0 ? '可切换上方结论筛选，查看该日期的其他检测记录。' : '检测结束后会自动归档；也可切换日期或工位查看其他记录。');
    const total = Number(result.total || 0);
    $('#history-page-label').textContent = total ? `共 ${count(total)} 条 · 第 ${Math.floor(state.offset / LIMIT) + 1} 页 · 显示 ${count(state.offset + 1)}–${count(state.offset + rows.length)} 条` : '共 0 条记录';
  }
  async function load({keepMessage = false} = {}) {
    if (!$('#history-dialog').open || !accessible() || busy) return;
    const version = ++listVersion, life = epoch;
    listRequest?.abort(); listRequest = new AbortController();
    page = null; rows = []; loading = true;
    if (!keepMessage) message();
    clearSummary(); controls();
    $('#history-list').setAttribute('aria-busy', 'true');
    $('#history-page-label').textContent = '正在读取…';
    empty('正在加载记录…', `${state.date} · UTC+08:00`);
    const params = new URLSearchParams({date:state.date, verdict:state.verdict, deleted:String(state.deleted), limit:String(LIMIT), offset:String(state.offset)});
    if (state.station) params.set('stationId', state.station);
    try {
      const result = await W.request('/board/history?' + params, {signal:listRequest.signal, timeout:15000});
      if (life !== epoch || version !== listVersion || !$('#history-dialog').open) return;
      if (!result || !Array.isArray(result.items)) throw new Error('记录返回格式无效，请刷新重试');
      if (!result.items.length && state.offset > 0) {
        state.offset = Math.max(0, Math.floor((Math.max(1, Number(result.total) || 1) - 1) / LIMIT) * LIMIT);
        return load({keepMessage});
      }
      renderPage(result);
    } catch (error) {
      if (life !== epoch || version !== listVersion || !$('#history-dialog').open) return;
      empty('历史记录读取失败', errorText(error), true);
      $('#history-page-label').textContent = '尚未加载记录';
    } finally {
      if (life === epoch && version === listVersion) {
        listRequest = null; loading = false;
        $('#history-list').setAttribute('aria-busy', 'false'); controls();
      }
    }
  }
  function stopDetail() {
    detailVersion++; detailRequest?.abort(); detailRequest = null; detailItem = null;
    $('#history-detail').querySelectorAll('video').forEach(video => { video.pause(); video.removeAttribute('src'); video.load(); });
    $('#history-detail').replaceChildren(); $('#history-detail').setAttribute('aria-busy', 'false');
  }
  function clear() {
    epoch++; listVersion++; loading = false; busy = false;
    listRequest?.abort(); listRequest = null; mutationRequest?.abort(); mutationRequest = null;
    stopDetail(); deleteItem = null; rows = []; page = null;
    $('#history-list').replaceChildren(); $('#history-list').setAttribute('aria-busy', 'false');
    $('#history-confirm-record').textContent = ''; $('#history-confirm-error').textContent = '';
    clearSummary(); message();
    if ($('#history-confirm-dialog').open) $('#history-confirm-dialog').close();
    if ($('#history-detail-dialog').open) $('#history-detail-dialog').close();
  }
  function reset() {
    if (!initialized) return;
    clear();
    if ($('#history-dialog').open) $('#history-dialog').close();
    state = {date:today(), station:'', verdict:'FAIL', deleted:false, offset:0}; lastVerdict = 'FAIL';
  }
  async function open() {
    if (config.getMode() === 'demo') { config.toast('每日历史保存实际检测记录，请先切换“实时数据”。'); return; }
    try {
      if (!W.session && !await W.login()) return;
      if (!accessible() || $('#history-dialog').open) return;
      clear(); state = {date:config.getDate?.() || today(), station:'', verdict:'FAIL', deleted:false, offset:0}; lastVerdict = 'FAIL';
      controls(); $('#history-dialog').showModal(); load();
    } catch (error) { config.toast(errorText(error)); }
  }
  function filter(changes) {
    if (busy) return;
    Object.assign(state, changes, {offset:0}); load();
  }
  function shiftDay(amount) {
    const next = new Date(state.date + 'T00:00:00Z');
    next.setUTCDate(next.getUTCDate() + amount);
    filter({date:next.toISOString().slice(0, 10)});
  }
  function progress(result) {
    const sequence = result.sequence || {}, compliance = result.compliance || {};
    const operations = sequence.operationsExpected != null ? sequence : compliance.operationsExpected != null ? compliance : null;
    if (operations) return {expected:Number(operations.operationsExpected) + Number(operations.preassembledExpected || 0), confirmed:Number(operations.operationsCompleted || 0) + Number(operations.preassembledConfirmed || 0)};
    const positions = sequence.positionsExpected != null ? sequence : compliance;
    return {expected:positions.positionsExpected, confirmed:positions.positionsConfirmed};
  }
  function field(name, value, wide = false) { return `<div${wide ? ' class="history-wide"' : ''}><dt>${E(name)}</dt><dd>${E(value ?? '—')}</dd></div>`; }
  function safeVideo(value) {
    if (typeof value !== 'string' || !value) return '';
    try { const url = new URL(value, location.href); return ['http:', 'https:'].includes(url.protocol) && url.origin === location.origin ? url.href : ''; } catch { return ''; }
  }
  function renderDetail(payload) {
    const row = payload.item, result = row.result || {}, context = row.context || {}, v = verdict(row);
    const algorithm = result.compliance || {}, review = row.reviewed ? result.manualReview : null;
    const candidates = result.aggregateTargets || result.historyTargets || result.targets || [];
    const targets = Array.isArray(candidates) ? candidates.filter(item => item && typeof item === 'object') : [];
    const confirmed = targets.filter(item => ['PRESENT','ACTION_DONE'].includes(item.state));
    const missing = targets.filter(item => !['PRESENT','ACTION_DONE'].includes(item.state));
    const p = progress(result), gaps = result.stationEvidenceGaps || result.evidenceGaps;
    const targetList = items => items.length ? '<ul>' + items.map(item => `<li>${E([item.id, item.name].filter(Boolean).join(' · '))}<span>${E(targetStates[item.state] || item.state || '待确认')}</span></li>`).join('') + '</ul>' : '<p>暂无对应位置记录</p>';
    const evidence = (Array.isArray(payload.evidence) ? payload.evidence : []).filter(item => item && typeof item === 'object');
    const evidenceHTML = evidence.map(item => {
      const url = safeVideo(item.video_url || item.videoUrl);
      return `<details class="history-evidence"><summary>${E(item.reason || '告警取证录像')} · ${url ? '查看录像' : E({PENDING:'等待归档',PROCESSING:'正在归档',FAILED:'归档失败',EXPIRED:'已过期',MISSING:'缺少采样'}[item.status] || '暂无可播放录像')}</summary>${url ? `<video controls playsinline preload="none" src="${E(url)}"></video>` : ''}${item.error ? `<p>${E(item.error)}</p>` : ''}${item.retain_until ? `<p>保留至 ${E(stamp(item.retain_until))}</p>` : ''}</details>`;
    }).join('');
    $('#history-detail').innerHTML = `<p id="history-detail-message" class="history-message" role="status" hidden></p><div class="history-outcome" data-verdict="${v}"><div class="history-outcome-top"><h3>最终判定：${labels[v]}</h3><span>${row.reviewed ? '以人工复核结论为准' : '归档检测结论'}</span></div><p>${E(row.reason || row.label || '暂无判定说明')}</p></div>
      <dl class="history-detail-grid">${field('工位', stationName(row))}${field('检测任务', taskName({...row, analysisType:row.analysisType || context.analysisType}))}${field('开始时间（UTC+08:00）', stamp(row.startedAt))}${field('结束时间（UTC+08:00）', stamp(row.finishedAt))}${field('检测来源', sourceName({...row, inputSource:row.inputSource || context.inputSource}))}${field('采集设备', row.cameraId || context.cameraId || '未记录')}${field('归档状态', states[row.status] || row.status)}${field('检测轮次', row.streamId)}${row.deletedAt != null ? field('删除记录', `${stamp(row.deletedAt)}${row.deletedBy ? ' · ' + row.deletedBy : ''}`, true) : ''}</dl>
      <section><h3>算法原判</h3><div class="history-algorithm-card"><p>${E(algorithm.label || labels[algorithm.status] || '待判定')}</p><p>${E(algorithm.reason || '无算法判定说明')}</p></div></section>
      ${review ? `<section><h3>人工复核</h3><div class="history-review-card"><p>${E(review.label || labels[review.status] || '已复核')} · ${E(review.reviewer || '复核人未记录')}</p><p>复核时间：${E(stamp(review.reviewedAt))}</p><p>复核原因：${E(review.reason || '未填写')}</p></div></section>` : ''}
      <section><h3>检测进度与证据</h3><div class="history-progress">${p.expected != null && p.confirmed != null ? `<span>安装 / 动作确认 ${E(p.confirmed)} / ${E(p.expected)}</span>` : ''}${result.frameSeq != null ? `<span>最终帧序号 ${E(result.frameSeq)}</span>` : ''}${Array.isArray(gaps) ? `<span>采样缺口 ${count(gaps.length)} 处</span>` : ''}</div>${targets.length ? `<div class="history-targets"><div><h4>待核对位置 · ${count(missing.length)}</h4>${targetList(missing)}</div><div><h4>已确认位置 · ${count(confirmed.length)}</h4>${targetList(confirmed)}</div></div>` : '<p class="history-hint">本轮未记录可展示的位置明细，请结合判定原因核对。</p>'}</section>
      <section><h3>告警录像</h3>${evidenceHTML || '<p class="history-hint">本轮暂无关联的告警录像。</p>'}<p class="history-hint">告警录像按独立留存规则保留，移动检测记录不会删除录像。</p></section>
      <div class="history-detail-actions"><p>${row.deletedAt != null ? '该记录已在回收站，可恢复到原归档日期。' : '删除后可在当天回收站恢复。'}</p>${row.deletedAt != null ? `<button data-history-restore="${E(row.streamId)}">恢复记录</button>` : `<button class="history-danger" data-history-delete="${E(row.streamId)}">删除记录</button>`}</div>`;
  }
  async function details(id) {
    if (!accessible() || busy || !$('#history-dialog').open) return;
    const version = ++detailVersion, life = epoch;
    detailRequest?.abort(); detailRequest = new AbortController(); detailItem = null;
    $('#history-detail').innerHTML = '<div class="history-empty"><strong>正在加载详情…</strong></div>';
    $('#history-detail').setAttribute('aria-busy', 'true');
    if (!$('#history-detail-dialog').open) $('#history-detail-dialog').showModal();
    try {
      const result = await W.request('/board/history/' + encodeURIComponent(id), {signal:detailRequest.signal, timeout:15000});
      if (life !== epoch || version !== detailVersion || !$('#history-detail-dialog').open) return;
      if (!result?.item?.streamId) throw new Error('详情返回格式无效，请重试');
      detailItem = result.item; renderDetail(result); $('#history-detail').scrollTop = 0;
    } catch (error) {
      if (life !== epoch || version !== detailVersion || !$('#history-detail-dialog').open) return;
      $('#history-detail').innerHTML = `<div class="history-empty"><strong>记录详情读取失败</strong><p>${E(errorText(error))}</p><button id="history-detail-retry">重新加载</button></div>`;
      $('#history-detail-retry').onclick = () => details(id);
    } finally {
      if (life === epoch && version === detailVersion) { detailRequest = null; $('#history-detail').setAttribute('aria-busy', 'false'); }
    }
  }
  function askDelete(row) {
    if (!accessible() || busy || !row) return;
    deleteItem = row;
    $('#history-confirm-record').textContent = `${stationName(row)} · ${stamp(archiveTime(row))} · ${labels[verdict(row)]}\n${row.reason || row.label || ''}`;
    $('#history-confirm-error').hidden = true; $('#history-confirm-error').textContent = '';
    controls(); $('#history-confirm-dialog').showModal();
  }
  async function mutate(row, restore = false) {
    if (!accessible() || busy || !row || !$('#history-dialog').open) return;
    const life = epoch;
    busy = true; mutationRequest = new AbortController(); controls();
    if (restore) {
      message('正在恢复记录…');
      const note = $('#history-detail-message');
      if (note) { note.textContent = '正在恢复记录…'; note.dataset.kind = ''; note.hidden = false; }
    }
    try {
      await W.request('/board/history/' + encodeURIComponent(row.streamId) + (restore ? '/restore' : ''), {method:restore ? 'POST' : 'DELETE', signal:mutationRequest.signal, timeout:15000});
      if (life !== epoch) return;
      busy = false; deleteItem = null;
      if ($('#history-confirm-dialog').open) $('#history-confirm-dialog').close();
      if ($('#history-detail-dialog').open) { stopDetail(); $('#history-detail-dialog').close(); }
      message(restore ? '记录已恢复，已重新计入原归档日期的统计。' : '记录已移入回收站，可在此日期的回收站恢复。');
      config.onChanged();
      await load({keepMessage:true});
    } catch (error) {
      if (life !== epoch) return;
      if (restore) {
        const text = '恢复失败：' + errorText(error) + '。如遇网络中断，请刷新确认记录状态。';
        message(text, 'error');
        const note = $('#history-detail-message');
        if (note) { note.textContent = text; note.dataset.kind = 'error'; note.hidden = false; $('#history-detail').scrollTop = 0; }
      }
      else { $('#history-confirm-error').textContent = '删除未确认完成：' + errorText(error) + '。如遇网络中断，可取消后刷新确认记录状态。'; $('#history-confirm-error').hidden = false; }
    } finally { if (life === epoch) { busy = false; mutationRequest = null; controls(); } }
  }
  function rowAction(event) {
    const button = event.target.closest('button');
    if (!button || busy) return;
    const id = button.dataset.historyDetail || button.dataset.historyDelete || button.dataset.historyRestore;
    if (!id) return;
    const row = detailItem?.streamId === id ? detailItem : rows.find(item => item.streamId === id);
    if (button.dataset.historyDetail) details(id);
    else if (button.dataset.historyDelete) askDelete(row);
    else if (button.dataset.historyRestore) mutate(row, true);
  }
  function init(options) {
    if (initialized) return;
    initialized = true; config = options;
    $('#history-open').onclick = open;
    $('#history-close').onclick = () => { if (!busy) $('#history-dialog').close(); };
    $('#history-close-detail').onclick = () => { if (!busy) $('#history-detail-dialog').close(); };
    $('#history-close-confirm').onclick = $('#history-confirm-cancel').onclick = () => { if (!busy) $('#history-confirm-dialog').close(); };
    $('#history-confirm-delete').onclick = () => mutate(deleteItem);
    $('#history-dialog').addEventListener('close', () => { if (!$('#history-dialog').open) { clear(); config.onClose(); } });
    $('#history-detail-dialog').addEventListener('close', () => { if (!$('#history-detail-dialog').open) { stopDetail(); config.onClose(); } });
    $('#history-confirm-dialog').addEventListener('close', () => { if (!$('#history-confirm-dialog').open) deleteItem = null; });
    for (const id of ['history-dialog','history-detail-dialog','history-confirm-dialog']) $( '#' + id).addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    $('#history-date').onchange = () => {
      const date = $('#history-date').value;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || date > today() || date < '0001-01-01') { controls(); message('请选择今天或之前的有效日期。', 'error'); return; }
      filter({date});
    };
    $('#history-prev-day').onclick = () => shiftDay(-1);
    $('#history-next-day').onclick = () => shiftDay(1);
    $('#history-today').onclick = () => filter({date:today()});
    $('#history-station').onchange = () => filter({station:$('#history-station').value});
    document.querySelectorAll('[data-history-verdict]').forEach(button => { button.onclick = () => filter({verdict:button.dataset.historyVerdict}); });
    $('#history-trash').onclick = () => {
      if (busy) return;
      if (!state.deleted) lastVerdict = state.verdict;
      filter({deleted:!state.deleted, verdict:state.deleted ? lastVerdict : 'ALL'});
    };
    $('#history-refresh').onclick = () => load();
    $('#history-prev-page').onclick = () => { if (!busy && !loading && state.offset) { state.offset = Math.max(0, state.offset - LIMIT); load(); } };
    $('#history-next-page').onclick = () => { if (!busy && !loading && page?.hasMore) { state.offset += LIMIT; load(); } };
    $('#history-list').addEventListener('click', rowAction);
    $('#history-detail').addEventListener('click', rowAction);
    window.addEventListener('pagehide', reset);
  }
  return {init, reset};
})();
