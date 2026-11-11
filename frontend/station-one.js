/* Shared manual station workflow; server records are the source of truth. */
window.StationOne = (() => {
  'use strict';
  const embedded = Boolean(window.STATION_ONE_EMBEDDED);
  let enabled = !embedded;
  const aliases = embedded ? {'#source-video': '#pilot-video', '#connection-status': '#server-status', '#account-button': '#server-account'} : {};
  const $ = selector => document.querySelector(aliases[selector] || selector), W = window.Workspace, esc = W.escape;
  const state = {cycle: null, active: null, storage: null, occupied: false, busy: false, restore: false,
    stream: null, sourceURL: null, frameReady: false, parent: null, offset: 0, total: 0, detail: null,
    operation: null, redoScrews: false, startRequest: null, generation: 0,
    connected: false, lastSynced: null, station: 'station-1', config: null, selectedStage: null};
  let monitorTimer = null, syncController = null, syncEpoch = 0, monitorFailures = 0;
  const names = {HOLES: '检查4个定位孔', SCREWS: '检查4处螺丝', READY: '检查完成，待保存',
    MANUAL_OK: '人工参与通过', NG: '确认异常 NG', REVIEW: '待复核', INTERRUPTED: '已中断'};
  const sourceNames = {camera: '现场摄像头', image: '图片联调', replay: '视频回放联调'};
  const eventNames = {START: '建立周期', STAGE: '人工阶段确认', FINISH: '结束并保存', CLOSE: '异常或中断', REVIEW: '追加复核', RESUME: '恢复前核对工件'};
  const stamp = value => new Date(value * 1000).toLocaleString('zh-CN', {hour12: false});
  const stages = (cycle = state.cycle) => cycle?.config.stages || state.config?.stages || [];
  const stageName = (id, cycle = state.cycle) => stages(cycle).find(item => item.id === id)?.name || names[id] || id;
  const label = cycle => (cycle.result ? names[cycle.result] : stageName(cycle.stage, cycle)) + (cycle.review_status === 'confirmed_ng' ? ' · 复核异常' : cycle.review_status === 'needs_rework' ? ' · 待返工' : '');
  const tell = (message, error = false) => {state.error = error ? message : null; $('#connection-status').textContent = message; $('#connection-status').classList.toggle('error', error); if (embedded && enabled) window.PilotDashboard?.renderAlerts(state);};
  const source = () => state.cycle?.stage !== 'CLOSED' && state.cycle ? state.cycle.source : $('#source-select').value;
  const availableStages = () => {
    const checks = state.cycle?.checks || {}, definitions = stages();
    if (state.cycle?.stage === 'READY') return definitions.filter(item => !definitions.some(other => other.requires.includes(item.id)));
    return definitions.filter(item => !checks[item.id] && item.requires.every(id => checks[id]));
  };
  const phase = () => {
    const available = availableStages();
    return available.find(item => item.id === state.selectedStage)?.id || available[0]?.id;
  };
  const currentStage = () => stages().find(item => item.id === phase());
  const active = () => state.cycle && state.cycle.stage !== 'CLOSED';
  const registrationEnabled = () => state.config?.registration_enabled === true;
  function requireRestore() {
    state.restore = Boolean(active());
    const op = state.operation;
    if (state.restore && op && !op.pending && ['stage', 'finish'].includes(op.kind)) {
      op.invalid = true;
      $('#manual-error').textContent = '画面或连接已中断，请关闭后重新核对当前工件并取证。';
    }
  }
  function buttons() {
    const blocked = state.busy || !W.session || !state.connected;
    $('#start-cycle').disabled = !registrationEnabled() || blocked || !state.storage || state.occupied || !state.frameReady;
    $('#product').disabled = $('#part-number').disabled = !registrationEnabled() || state.busy;
    $('#confirm-stage').disabled = blocked || state.restore || !state.frameReady || !currentStage() || document.querySelectorAll('#target-list input:checked').length !== currentStage().items.length;
    $('#finish-cycle').disabled = blocked || state.restore || state.cycle?.stage !== 'READY';
    for (const id of ['close-cycle', 'refresh-screws', 'next-cycle', 'resume-cycle', 'current-detail', 'refresh-cycle']) $('#' + id).disabled = blocked;
    $('#refresh-cycle').disabled = state.busy || !W.session;
    $('#source-select').disabled = blocked || Boolean(active());
    $('#account-button').disabled = state.busy;
    $('#target-checks').disabled = blocked || state.restore;
    $('#stage-select').disabled = blocked || state.restore;
    $('#manual-submit').disabled = state.busy || Boolean(state.operation?.invalid) || (!state.connected && !state.operation?.pending);
    const pending = Boolean(state.operation?.pending);
    $('#manual-reason').readOnly = pending;
    $('#manual-outcome').disabled = pending;
    $('#manual-disposition').disabled = pending;
    $('#manual-dialog .close-button').disabled = state.busy;
    if (embedded && enabled) window.PilotDashboard?.update(state);
    window.InspectionTrigger?.refresh();
  }
  function render() {
    const cycle = state.cycle, closed = cycle?.stage === 'CLOSED';
    if (!embedded) $('#pilot-content').hidden = !W.session;
    $('#login-gate').hidden = Boolean(W.session);
    $('#start-form').hidden = !registrationEnabled() || Boolean(cycle) || !W.session;
    $('#pilot-process-note').hidden = !registrationEnabled();
    $('#pilot-registration-paused').hidden = registrationEnabled();
    $('#current-cycle').hidden = !cycle || !W.session;
    if (cycle) {
      $('#cycle-part').textContent = cycle.part_number; $('#cycle-product').textContent = cycle.product;
      $('#cycle-id').textContent = '周期 ' + cycle.id;
      $('#cycle-kind').textContent = cycle.record_kind + (cycle.parent_id ? ' · 返工周期' : '');
      $('#cycle-stage').textContent = state.connected ? label(cycle) : '连接待恢复 · 上次记录：' + label(cycle);
      $('#cycle-decision').dataset.result = state.connected ? cycle.result || '' : 'REVIEW';
      $('#cycle-guidance').textContent = closed ? cycle.result === 'MANUAL_OK' ? '本工位证据及人工记录已保存。本轮未运行模型。' : cycle.reason :
        cycle.stage === 'READY' && !state.redoScrews ? '确认仍为同一工件后结束保存。末端项目证据超过3分钟须重新核对。' : currentStage()?.guidance || '';
      $('#restore-panel').hidden = !state.restore || closed;
      const showChecks = !closed && (cycle.stage !== 'READY' || state.redoScrews);
      $('#target-checks').hidden = !showChecks;
      const available = availableStages();
      $('#stage-choice').hidden = !showChecks || available.length < 2;
      $('#stage-select').innerHTML = available.map(item => `<option value="${esc(item.id)}">${esc(item.name)}${cycle.checks[item.id] ? ' · 重新取证' : ''}</option>`).join('');
      $('#stage-select').value = phase() || '';
      $('#checks-legend').textContent = (currentStage()?.name || '本阶段') + ' · 逐项核对';
      $('#target-list').innerHTML = (currentStage()?.items || []).map(item => `<label class="target-choice"><input type="checkbox" value="${esc(item.id)}"><span>${esc(item.id)} · ${esc(item.label)}</span></label>`).join('');
      $('#refresh-screws').textContent = stages().length === 2 && cycle.station === 'station-1' ? '重新核对螺丝' : '重新核对末端项目';
      $('#confirm-stage').hidden = !showChecks;
      $('#finish-cycle').hidden = cycle.stage !== 'READY' || state.redoScrews;
      $('#refresh-screws').hidden = cycle.stage !== 'READY' || state.redoScrews;
      $('#close-cycle').hidden = closed; $('#next-cycle').hidden = !closed;
    }
    const step = !cycle ? 0 : cycle.stage === 'HOLES' ? 1 : cycle.stage === 'SCREWS' ? 2 : 3;
    document.querySelectorAll('.pilot-steps li').forEach((item, i) => {item.classList.toggle('active', i === step); item.classList.toggle('done', i < step);});
    buttons();
  }
  function updateSourceUI() {
    const kind = $('#source-select').value;
    $('#camera-start').hidden = kind !== 'camera' || Boolean(state.stream);
    $('#camera-stop').hidden = !state.stream;
    $('#file-label').hidden = kind === 'camera';
    $('#file-label').firstChild.textContent = kind === 'replay' ? '选择视频' : '选择图片';
    $('#source-file').accept = kind === 'replay' ? 'video/*,.mp4,.webm,.mov,.avi' : 'image/jpeg,image/png';
    if (!embedded) $('#source-note').textContent = kind === 'camera' ? '保持同一工件，换件前先结束本轮。仅在人工确认时保存截图。' : '此来源记为联调；图片或视频画面不能证明现场真实工序已执行。';
  }
  function clearMedia() {
    state.generation++;
    document.querySelectorAll('#target-list input').forEach(input => {input.checked = false;});
    if (state.stream) {state.stream.getTracks().forEach(track => track.stop()); state.stream = null;}
    const video = $('#source-video'); video.pause(); video.srcObject = null; video.removeAttribute('src'); video.load(); video.hidden = true;
    $('#source-image').hidden = true; $('#source-image').removeAttribute('src');
    if (state.sourceURL) URL.revokeObjectURL(state.sourceURL);
    state.sourceURL = null; state.frameReady = false; $('#source-file').value = '';
    $('#source-placeholder').hidden = false; $('#source-state').textContent = '等待输入';
    updateSourceUI(); buttons();
  }
  async function camera() {
    if (!W.session && !await W.login()) return;
    clearMedia(); const generation = state.generation;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({video: {width: {ideal: 1920}, height: {ideal: 1080}}, audio: false});
      if (generation !== state.generation) {stream.getTracks().forEach(t => t.stop()); return;}
      state.stream = stream; const video = $('#source-video'); video.srcObject = stream; video.controls = false;
      await video.play();
      if (generation !== state.generation || state.stream !== stream) {stream.getTracks().forEach(t => t.stop()); return;}
      video.hidden = false; $('#source-placeholder').hidden = true;
      state.frameReady = true; $('#source-state').textContent = '摄像头已连接'; updateSourceUI(); buttons();
      const track = stream.getVideoTracks()[0];
      track.addEventListener('ended', () => {if (state.stream !== stream) return; clearMedia(); requireRestore(); render(); tell('摄像头已断开，不能取证。请重新连接并核对工件，或将本轮结束为中断。', true);});
      track.addEventListener('mute', () => {if (state.stream !== stream) return; state.frameReady = false; requireRestore(); render(); tell('摄像头画面中断，等待恢复后核对工件。', true);});
      track.addEventListener('unmute', () => {if (state.stream !== stream) return; state.frameReady = true; buttons();});
    } catch (error) {if (generation !== state.generation) return; clearMedia(); tell('无法开启摄像头：' + error.message, true);}
  }
  async function loadFile(file) {
    if (!file) return;
    if (file.size > 500 * 1024 * 1024) {tell('联调文件请控制在500MB以内', true); return;}
    clearMedia(); const generation = state.generation;
    state.sourceURL = URL.createObjectURL(file);
    const replay = source() === 'replay', element = replay ? $('#source-video') : $('#source-image');
    try {
      await new Promise((resolve, reject) => {
        element[replay ? 'onloadeddata' : 'onload'] = resolve;
        element.onerror = () => reject(new Error('浏览器无法读取此素材，请换用JPEG/PNG或MP4/WebM'));
        element.src = state.sourceURL;
        if (replay) {element.controls = true; element.load();}
      });
      if (generation !== state.generation) return;
      element.hidden = false; $('#source-placeholder').hidden = true; state.frameReady = true;
      $('#source-state').textContent = file.name; buttons();
    } catch (error) {if (generation !== state.generation) return; clearMedia(); tell(error.message, true);}
  }
  async function capture() {
    if (!state.frameReady) throw new Error('请先取得当前阶段的有效图像');
    const element = source() === 'image' ? $('#source-image') : $('#source-video');
    if (source() === 'camera' && !state.stream?.getVideoTracks().some(track => track.readyState === 'live' && !track.muted)) throw new Error('摄像头已断开或画面中断，请重新连接');
    const width = element.videoWidth || element.naturalWidth, height = element.videoHeight || element.naturalHeight;
    if (!width || !height || (element.tagName === 'VIDEO' && element.readyState < 2)) throw new Error('当前画面尚未就绪');
    const canvas = document.createElement('canvas'), scale = Math.min(1, 1600 / width, 1600 / height);
    canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    canvas.getContext('2d').drawImage(element, 0, 0, canvas.width, canvas.height);
    const mediaTime = source() === 'replay' ? element.currentTime : null;
    if (source() === 'replay') element.pause();
    return {image: canvas.toDataURL('image/jpeg', .86).split(',')[1], media_time: mediaTime, captured_at: Date.now() / 1000};
  }
  function scheduleMonitor(delay = 5000) {
    clearTimeout(monitorTimer);
    if (enabled && W.session && !document.hidden) monitorTimer = setTimeout(() => {
      if (state.busy || state.operation) scheduleMonitor();
      else sync(false, true).catch(() => {});
    }, delay);
  }
  function stopMonitor() {
    clearTimeout(monitorTimer); syncEpoch++; syncController?.abort(); syncController = null;
  }
  function disconnected(message = '服务器连接中断，停止提交；正在重连。') {
    if (!enabled || !W.session) return;
    const changed = state.connected;
    state.connected = false; requireRestore();
    if (changed) render(); else buttons();
    tell(message + (state.lastSynced ? ' 上次同步 ' + new Date(state.lastSynced).toLocaleTimeString() : ''), true);
  }
  async function sync(initial = false, background = false) {
    if (!enabled || !W.session || syncController) return;
    clearTimeout(monitorTimer);
    const controller = new AbortController(), epoch = syncEpoch, account = W.session;
    syncController = controller;
    const before = state.cycle, revision = before?.revision;
    const valid = () => enabled && W.session === account && epoch === syncEpoch;
    try {
      const status = await W.request('/pilot/status?station=' + state.station, {signal: controller.signal, timeout: 5000});
      if (!valid() || state.cycle !== before || state.cycle?.revision !== revision || background && (state.busy || state.operation)) return;
      let cycle = status.active;
      if (!cycle && state.cycle) cycle = (await W.request('/pilot/cycles/' + state.cycle.id, {signal: controller.signal, timeout: 5000})).cycle;
      if (!valid() || state.cycle !== before || state.cycle?.revision !== revision || background && (state.busy || state.operation)) return;
      const recovered = !state.connected, changed = cycle && (cycle.id !== state.cycle?.id || cycle.revision !== state.cycle?.revision);
      const statusChanged = state.storage !== status.storage_ready || state.occupied !== status.occupied;
      state.connected = true; state.lastSynced = Date.now(); monitorFailures = 0;
      state.storage = status.storage_ready; state.occupied = status.occupied; state.active = status.active;
      state.config = status.config;
      if (cycle) {
        state.cycle = cycle;
        if (cycle.stage !== 'CLOSED' && (initial || recovered || changed)) state.restore = true;
        if ($('#source-select').value !== cycle.source) {$('#source-select').value = cycle.source; clearMedia();}
      }
      if (!background || recovered || changed || statusChanged) {
        tell(!state.storage ? '证据目录不可写，暂不能开始新周期。请检查存储后重新同步。' : status.occupied && !status.active ? state.config.name + '正由其他账户使用，请稍后重试。' : '已连接 · 阶段结果与证据保存在服务器 · 当前为人工辅助流程', !state.storage);
        updateSourceUI(); render(); await history();
      }
    } catch (error) {
      if (valid() && error.name !== 'AbortError') {monitorFailures++; disconnected(); if (!background) throw error;}
    } finally {
      if (syncController === controller) syncController = null;
      if (epoch === syncEpoch) scheduleMonitor(Math.min(20000, 5000 * 2 ** Math.min(monitorFailures, 2)));
    }
  }
  async function history() {
    if (!enabled || !W.session) return;
    const epoch = syncEpoch, account = W.session;
    const result = await W.request('/pilot/cycles?station=' + state.station + '&q=' + encodeURIComponent($('#history-query').value) + '&offset=' + state.offset);
    if (!enabled || W.session !== account || epoch !== syncEpoch) return;
    state.total = result.total;
    $('#history-rows').innerHTML = result.items.length ? result.items.map(row => `<tr><td><strong>${esc(row.part_number)}</strong><small>${esc(row.product)}</small></td><td><span class="badge ${row.result === 'NG' ? 'error' : row.result ? 'review' : 'draft'}">${esc(label(row))}</span></td><td>${esc(sourceNames[row.source])}</td><td>${esc(stamp(row.started_at))}</td><td><button class="small" data-detail="${row.id}">查看</button></td></tr>`).join('') : '<tr><td colspan="5" class="muted">暂无匹配记录</td></tr>';
    $('#history-count').textContent = `共 ${result.total} 条 · 第 ${Math.floor(state.offset / 20) + 1} 页`;
    $('#history-prev').disabled = state.offset === 0; $('#history-next').disabled = state.offset + 20 >= result.total;
  }
  function startBlocked() {
    if (!enabled) return '工位尚未就绪';
    if (!registrationEnabled()) return '工件登记暂已停用，请返回检测首页按F8开始工位检测';
    if (!W.session) return '请先登录服务器';
    if (state.busy) return '当前操作尚未完成';
    if (!state.connected) return '工位连接未就绪，请先同步';
    if (!state.storage) return '证据存储不可用';
    if (active()) return '本件检查中，重复触发已锁定';
    if (state.occupied) return '工位正在使用';
    if (state.cycle && source() !== 'camera') return '请先准备下一件的新图片或视频';
    if (!state.frameReady) return '请先连接摄像头或选择当前工件图像';
    if (!$('#product').value.trim()) return '请先填写产品型号';
    return '';
  }
  async function startFromTrigger() {
    const reason = startBlocked();
    if (reason) throw new Error(reason);
    if (state.cycle?.stage === 'CLOSED') next();
    await start();
  }
  async function start(event) {
    event?.preventDefault();
    const reason = startBlocked();
    if (reason) {tell(reason, true); return;}
    if (!$('#start-form').reportValidity()) return;
    const values = {station: state.station, product: $('#product').value.trim(), part_number: $('#part-number').value.trim(), source: source(), parent_id: state.parent?.id || null};
    const fingerprint = JSON.stringify(values);
    if (!state.startRequest || state.startRequest.fingerprint !== fingerprint) state.startRequest = {fingerprint, body: {...values, request_id: crypto.randomUUID()}};
    state.busy = true; buttons();
    try {
      const data = await W.json('/pilot/cycles', 'POST', state.startRequest.body);
      state.cycle = data.cycle; state.active = data.cycle; state.occupied = true; state.restore = false;
      state.parent = null; state.startRequest = null; state.redoScrews = false; state.selectedStage = null;
      tell('本件已建立，请按当前工位要求逐项检查并保存证据。'); render(); await history();
    } catch (error) {tell(error.message, true); if (error.status === 409) await sync(true);}
    finally {state.busy = false; buttons();}
  }
  async function manual(kind, row = state.cycle) {
    if (state.busy || !row || !state.connected) return;
    if (state.restore && ['stage', 'finish'].includes(kind)) {tell('请先核对当前工件，再继续本件。', true); return;}
    try {
      let proof = {}, checked;
      if (kind === 'stage') {proof = await capture(); checked = [...document.querySelectorAll('#target-list input:checked')].map(input => input.value);}
      if (kind === 'close' && state.frameReady) {try {proof = await capture();} catch { /* Missing evidence is explicitly recorded. */ }}
      state.operation = {kind, row, proof, checked, stage: phase(), prepared: Date.now(), pending: null};
      $('#manual-title').textContent = {stage: stageName(phase()) + ' · 保存证据', finish: '结束本件并保存', close: '异常或中断本轮', review: '追加复核记录', resume: '核对并继续本件'}[kind];
      $('#manual-description').textContent = kind === 'review' ? '复核作为后续事件保存，原结论保留；纠正实物后请建立返工周期。' : kind === 'close' ? (proof.image ? '保存当前截图及处置原因，再释放工位。' : '当前无法取证。本轮将明确标记证据缺失，请填写原因。') : kind === 'resume' ? '请重新核对工件编号、当前工序及已存证据。若换件或前序过程无法确认，请中断后整站重检。' : '确认人：' + W.session.username + '。请核对工件 ' + row.part_number + '，本次确认会写入历史记录。';
      if (kind === 'finish' && row.config.pending_specs?.length) $('#manual-description').textContent += ' 工艺要求尚待确认，本件保存为待复核：' + row.config.pending_specs.join('；') + '。';
      $('#manual-preview').hidden = !proof.image; $('#manual-preview').src = proof.image ? 'data:image/jpeg;base64,' + proof.image : '';
      $('#outcome-label').hidden = kind !== 'close'; $('#disposition-label').hidden = kind !== 'review';
      $('#manual-reason').value = ''; $('#manual-password').value = ''; $('#manual-attest').checked = false; $('#manual-error').textContent = '';
      $('#attest-label').textContent = kind === 'review' ? '我确认以上复核内容，并保留原始检查结论。' : '我已核对当前工件和以上内容，确认本次记录与实际情况一致。';
      buttons(); $('#manual-dialog').showModal();
    } catch (error) {tell(error.message, true);}
  }
  async function submitManual(event) {
    event.preventDefault(); const op = state.operation; if (!op || state.busy || op.invalid || (!state.connected && !op.pending)) return;
    if (state.restore && !op.pending && ['stage', 'finish'].includes(op.kind)) {requireRestore(); buttons(); return;}
    if (op.kind === 'stage' && !op.pending && Date.now() - op.prepared > 120000) {$('#manual-error').textContent = '取证已超过2分钟，请关闭后重新取证并核对。'; return;}
    if (!op.pending) {
      op.pending = {request_id: crypto.randomUUID(), revision: op.row.revision, reason: $('#manual-reason').value.trim()};
      if (op.kind === 'stage') Object.assign(op.pending, op.proof, {stage: op.stage, checked: op.checked});
      if (op.kind === 'close') Object.assign(op.pending, {outcome: $('#manual-outcome').value, image: op.proof.image || null});
      if (op.kind === 'review') op.pending.disposition = $('#manual-disposition').value;
    }
    state.busy = true; buttons(); $('#manual-error').textContent = '';
    try {
      const result = await W.json('/pilot/cycles/' + op.row.id + '/' + op.kind, 'POST', {...op.pending, review_password: $('#manual-password').value});
      if (state.cycle?.id === result.cycle.id || op.kind !== 'review') state.cycle = result.cycle;
      if (op.kind === 'resume') state.restore = false;
      if (op.kind === 'stage') {state.redoScrews = false; state.selectedStage = null; if (source() === 'image' && result.cycle.stage !== 'READY') {state.frameReady = false; $('#source-state').textContent = '请选择下一检查项目的新图片';}}
      if (result.cycle.stage === 'CLOSED') {state.active = null; state.occupied = false; state.restore = false;}
      $('#manual-dialog').close(); state.operation = null; tell('已保存：' + label(result.cycle)); render(); await history();
      if (op.kind === 'review') await detail(op.row.id);
    } catch (error) {
      $('#manual-error').textContent = error.message + (!error.status || error.status >= 500 ? '。可再次点击确认，用同一请求重试；已提交的内容将保持不变。' : '');
      if (error.status && error.status < 500) op.pending = null;
      if (error.status === 409) {op.invalid = true; await sync(); $('#manual-error').textContent += '。请关闭此窗口，核对更新后的阶段。';}
    } finally {state.busy = false; $('#manual-password').value = ''; buttons();}
  }
  async function detail(identity) {
    const generation = state.generation;
    const data = await W.request('/pilot/cycles/' + identity);
    if (!enabled || !W.session || generation !== state.generation) return;
    state.detail = data;
    const row = data.cycle;
    const proofs = Object.entries(row.checks).map(([phase, proof]) => `<figure><a href="${esc(proof.evidence.url)}" target="_blank" rel="noopener"><img src="${esc(proof.evidence.url)}" alt="${esc(stageName(phase, row))}阶段证据"></a><figcaption>${esc(stageName(phase, row))} · ${esc(proof.actor)} · ${esc(stamp(proof.at))}</figcaption></figure>`).join('');
    $('#detail-content').innerHTML = `<dl class="detail-meta"><dt>工位</dt><dd>${esc(row.config.name)}</dd><dt>工件编号</dt><dd>${esc(row.part_number)}</dd><dt>产品</dt><dd>${esc(row.product)}</dd><dt>本轮结果</dt><dd>${esc(label(row))}</dd><dt>输入来源</dt><dd>${esc(sourceNames[row.source])} · ${esc(row.record_kind)}</dd><dt>算法结论</dt><dd>未运行模型</dd><dt>周期编号</dt><dd class="code">${esc(row.id)}</dd><dt>记录说明</dt><dd>${esc(row.reason || row.config.notes || "—")}</dd><dt>规则版本</dt><dd>${esc(row.config.version)}</dd>${row.parent_id ? `<dt>返工自</dt><dd><button class="small" data-detail="${row.parent_id}">查看原周期</button></dd>` : ''}</dl><div class="evidence-grid">${proofs}</div><h3>操作与复核留痕</h3><ol class="event-list">${data.events.map(event => `<li><strong>${esc(eventNames[event.kind] || event.kind)}${event.payload.stage ? ' · ' + esc(stageName(event.payload.stage, row)) : ''}</strong><p>${esc(stamp(event.at))} · ${esc(event.payload.actor || row.operator)}</p><p>${esc(event.payload.reason || '')}${event.payload.evidence_missing ? ' · 未取得异常截图' : ''}${event.payload.disposition ? ' · ' + esc({note: '补充说明', confirmed_ng: '复核确认异常', needs_rework: '需要返工重检'}[event.payload.disposition]) : ''}</p>${event.payload.evidence ? `<a href="${esc(event.payload.evidence.url)}" target="_blank" rel="noopener">查看本次证据</a>` : ''}</li>`).join('')}</ol>${data.rework_ids.length ? '<h3>关联返工</h3>' + data.rework_ids.map(id => `<button class="small" data-detail="${id}">${esc(id)}</button>`).join(' ') : ''}`;
    $('#detail-review').disabled = row.stage !== 'CLOSED'; $('#detail-rework').disabled = !registrationEnabled() || row.stage !== 'CLOSED' || state.occupied;
    if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
  }
  function next() {
    state.cycle = null; state.restore = false; state.parent = null; state.redoScrews = false; state.selectedStage = null; $('#part-number').value = '';
    if (source() !== 'camera') clearMedia(); render(); $('#part-number').focus();
  }
  async function handle(action) {try {await action();} catch (error) {tell(error.message, true);}}
  $('#source-select').onchange = () => {state.startRequest = null; clearMedia();};
  $('#source-file').onchange = event => handle(() => loadFile(event.target.files[0]));
  $('#camera-start').onclick = () => handle(camera); $('#camera-stop').onclick = clearMedia;
  $('#target-list').onchange = buttons; $('#start-form').onsubmit = start;
  $('#start-form').addEventListener('input', () => window.InspectionTrigger?.refresh());
  $('#start-form').addEventListener('keydown', event => {
    // Barcode readers commonly append Enter; only the explicit start action begins a cycle.
    if (event.key === 'Enter' && event.target.matches('input')) event.preventDefault();
  });
  $('#stage-select').onchange = () => {state.selectedStage = $('#stage-select').value; render();};
  $('#confirm-stage').onclick = () => manual('stage'); $('#finish-cycle').onclick = () => manual('finish');
  $('#close-cycle').onclick = () => manual('close'); $('#resume-cycle').onclick = () => manual('resume');
  $('#refresh-screws').onclick = () => {state.redoScrews = true; render();};
  $('#next-cycle').onclick = next; $('#current-detail').onclick = () => handle(() => detail(state.cycle.id));
  $('#refresh-cycle').onclick = () => handle(() => sync()); $('#manual-form').onsubmit = submitManual;
  $('#manual-dialog').addEventListener('cancel', event => {if (state.busy) event.preventDefault();});
  $('#manual-dialog').addEventListener('close', () => {$('#manual-password').value = ''; $('#manual-preview').removeAttribute('src'); state.operation = null;});
  document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => $('#' + button.dataset.close).close());
  document.addEventListener('click', event => {const button = event.target.closest('[data-detail]'); if (button) handle(() => detail(button.dataset.detail));});
  $('#history-search').onsubmit = event => {event.preventDefault(); state.offset = 0; handle(history);};
  $('#history-prev').onclick = () => {state.offset = Math.max(0, state.offset - 20); handle(history);};
  $('#history-next').onclick = () => {state.offset += 20; handle(history);};
  $('#detail-review').onclick = () => {const row = state.detail.cycle; $('#detail-dialog').close(); manual('review', row);};
  $('#detail-rework').onclick = () => {const row = state.detail.cycle; next(); state.parent = row; $('#product').value = row.product; $('#part-number').value = row.part_number; $('#detail-dialog').close(); tell('返工已关联原周期，请取得新的起始画面后开始。'); $('#start-form').scrollIntoView({block: 'center'});};
  $('#detail-export').onclick = () => {const url = URL.createObjectURL(new Blob([JSON.stringify(state.detail, null, 2)], {type: 'application/json'})); const a = document.createElement('a'); a.href = url; a.download = state.detail.cycle.station + '-' + state.detail.cycle.id + '.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);};
  if (!embedded) $('#account-button').onclick = () => handle(async () => {if (W.session) await W.logout(); else await W.login();});
  $('#login-start').onclick = () => handle(() => W.login());
  document.addEventListener('workspace-session', event => {
    if (!enabled) return;
    stopMonitor(); state.connected = false;
    if (event.detail) handle(() => sync(true));
    else {clearMedia(); state.cycle = null; state.detail = null; state.active = null; state.operation = null; $('#detail-dialog').close(); $('#manual-dialog').close(); $('#detail-content').textContent = ''; $('#history-rows').textContent = ''; render(); tell('请登录后开始检查。');}
  });
  window.addEventListener('pagehide', () => {stopMonitor(); clearMedia();});
  window.addEventListener('offline', () => disconnected());
  window.addEventListener('online', () => {if (enabled) sync(false, true).catch(() => {});});
  document.addEventListener('visibilitychange', () => {
    if (!enabled) return;
    if (document.hidden) stopMonitor();
    else {disconnected('正在核对服务器状态。'); sync(false, true).catch(() => {});}
  });
  if (!embedded) {
    window.InspectionTrigger?.attach({blocked: startBlocked, start: startFromTrigger, notify: message => W.toast(message)});
  }
  updateSourceUI(); render(); if (!embedded) handle(() => W.connect());
  return {
    get state() {return state;},
    startBlocked, startFromTrigger,
    canLeave: () => !state.busy,
    activate: async config => {
      stopMonitor(); enabled = false; clearMedia();
      Object.assign(state, {station: config.station, config, cycle: null, active: null, parent: null, detail: null,
        operation: null, startRequest: null, selectedStage: null, redoScrews: false, restore: false,
        storage: null, occupied: false, connected: false, lastSynced: null, offset: 0, total: 0, error: null});
      $('#part-number').value = ''; $('#product').value = ''; $('#history-query').value = ''; $('#history-rows').textContent = ''; $('#detail-content').textContent = '';
      $('#source-placeholder span').textContent = config.name;
      enabled = true; await W.connect(); render();
    },
    deactivate: () => {enabled = false; stopMonitor(); clearMedia(); for (const id of ['manual-dialog', 'detail-dialog', 'pilot-history-dialog']) $('#' + id)?.close();},
    account: () => handle(async () => {if (W.session) {clearMedia(); await W.logout();} else await W.login();}),
    history: async () => {if (!W.session && !await W.login()) return; await history(); $('#pilot-history-dialog').showModal();},
    showRecord: () => state.cycle ? handle(() => detail(state.cycle.id)) : W.toast('请先选择或建立本件记录'),
    chooseSource: () => {if (source() === 'camera') camera(); else $('#source-file').click();},
    finish: () => state.cycle?.stage === 'READY' ? manual('finish') : W.toast('请先完成当前工位全部检查项目'),
  };
})();
