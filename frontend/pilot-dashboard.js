/* Mount the station workflow into the existing dashboard's video and process panels. */
window.PilotDashboard = (() => {
  const $ = selector => document.querySelector(selector);
  let mounted = false;
  let config = null, catalog = null;
  const definitions = state => state.cycle?.config.stages || state.config?.stages || config?.stages || [];
  function wrap(parent, nodes, extra = '') {
    const box = document.createElement('div'); box.className = 'pilot-ui ' + extra;
    box.append(...nodes); parent.append(box); return box;
  }
  async function mount() {
    if (mounted) return;
    const response = await fetch('/station-one.html?embed=1');
    if (!response.ok) throw new Error('工位流程组件加载失败');
    const source = new DOMParser().parseFromString(await response.text(), 'text/html');
    const take = selector => document.adoptNode(source.querySelector(selector));
    source.querySelector('#source-video').id = 'pilot-video';
    const controls = wrap($('.video-panel'), [take('.source-controls')], 'pilot-source-controls');
    $('.video-mode-control').after(controls);
    const status = document.createElement('span'); status.id = 'source-state'; status.className = 'hint';
    controls.querySelector('.source-controls').append(status);
    wrap($('.video-frame'), [take('.pilot-media')], 'pilot-main-media');
    wrap($('.workflow-body'), [take('#refresh-cycle'), take('#login-gate'), take('#pilot-process-note'), take('#pilot-registration-paused'), take('#start-form'), take('#current-cycle')]);
    const evidence = document.createElement('div'); evidence.id = 'pilot-stage-evidence';
    wrap($('.timeline-panel'), [evidence]);
    const historyButton = document.createElement('button'); historyButton.id = 'pilot-open-history';
    historyButton.textContent = '查看工位1记录'; historyButton.onclick = () => window.StationOne.history();
    wrap($('.alert-toolbar'), [historyButton]);
    const history = document.createElement('dialog'); history.id = 'pilot-history-dialog'; history.className = 'pilot-ui';
    history.innerHTML = '<div class="dialog-heading"><h2>工位1检查记录</h2><button class="close-button" aria-label="关闭记录">×</button></div>';
    history.querySelector('button').onclick = () => history.close();
    history.append(take('.history-panel')); document.body.append(history);
    for (const id of ['manual-dialog', 'detail-dialog']) {const dialog = take('#' + id); dialog.classList.add('pilot-ui'); document.body.append(dialog);}
    window.STATION_ONE_EMBEDDED = true;
    await new Promise((resolve, reject) => {const script = document.createElement('script'); script.src = source.querySelector('script[src*="station-one.js"]').getAttribute('src'); script.onload = resolve; script.onerror = () => reject(new Error('工位1控制器加载失败')); document.body.append(script);});
    mounted = true;
  }
  async function activate(task = 'station1') {
    catalog ||= (await window.Workspace.request('/pilot/stations')).items;
    config = catalog.find(item => item.station === 'station-' + task.slice(7));
    if (!config) throw new Error('工位配置不存在');
    if (!config.registration_enabled) {
      for (const station of catalog) {
        const option = $('#inspection-task option[value="station' + station.station.split('-')[1] + '"]');
        if (option) option.textContent = station.name + ' · 工位检测';
      }
      await window.StationDetection.activate(config);
      return;
    }
    await mount();
    document.body.classList.add('station-task');
    const text = (selector, value) => {$(selector).textContent = value;};
    text('.brand p', '按检测任务切换模型与流程 · ' + config.name + '当前使用人工确认');
    $('.video-panel .subheading h3').innerHTML = '<b class="number blue">1</b>' + window.Workspace.escape(config.name + ' · ' + config.title);
    text('.workflow-panel .section-title h2', '检测判定流程');
    text('.timeline-panel .subheading h3', '阶段证据');
    text('.alerts-panel .section-title h2', '实时告警中心');
    text('.alerts-foot', config.name + ' · 当前异常与待复核事项');
    $('#alert-search').placeholder = '搜索异常或复核事项';
    text('#pilot-open-history', '查看' + config.name + '记录');
    text('#pilot-history-dialog .dialog-heading h2', config.name + '检查记录');
    text('.toolbar [data-action="upload"] .task-action-label', '选择图像来源');
    $('.compliance-panel .subheading h3').innerHTML = '<b class="number red-bg">4</b>本件检查状态';
    text('.compliance-grid>div:nth-child(3) span', '本件状态');
    text('.cpu-chip', '人工辅助 · 模型待现场训练');
    text('.check-chip', config.name + ' · 流程与存储自检');
    text('.pose-legend', '');
    text('.camera-name', config.name + ' · ' + config.title); text('.stream-info', '人工辅助流程');
    text('.footer>span', 'AI-SOP · ' + config.name + '人工检查');
    text('#pilot-process-note', config.notes + (config.pending_specs.length ? ' 待现场确认：' + config.pending_specs.join('；') + '。完成记录后仍为待复核。' : ''));
    ['ID', '检查状态', '判定来源', '检查项目', '证据时间', '确认人'].forEach((name, i) => $('.results-panel thead').querySelectorAll('th')[i].textContent = name);
    await window.StationOne.activate(config);
  }
  function workflow(state, label) {
    const row = state.cycle, checks = row?.checks || {}, closed = row?.stage === 'CLOSED', E = window.Workspace.escape;
    const steps = [
      ...definitions(state).map(item => ({name: item.name, detail: item.items.map(target => target.label + ' (' + target.id + ')').join(' · '),
        done: Boolean(checks[item.id]) && !(state.redoScrews && $('#stage-select').value === item.id),
        current: !!row && !closed && (!checks[item.id] && item.requires.every(id => checks[id]) || state.redoScrews && $('#stage-select').value === item.id)})),
      {name: '结束判定', detail: closed ? label : '保存本件检查结果', done: closed, current: row?.stage === 'READY' && !state.redoScrews},
    ];
    $('#workflow-name').textContent = config.name + ' · ' + config.title;
    $('#workflow-status').textContent = label;
    $('#workflow-status').dataset.verdict = !state.connected ? 'REVIEW' : row?.result === 'NG' || row?.review_status === 'confirmed_ng' ? 'FAIL' : row?.result === 'MANUAL_OK' && !['needs_rework', 'confirmed_ng'].includes(row.review_status) ? 'PASS' : 'REVIEW';
    $('#workflow-description').textContent = row ? '工件 ' + row.part_number + ' · 人工确认' : state.config?.registration_enabled ? '等待开始本件' : '工件登记暂已停用';
    $('#steps').innerHTML = steps.map((step, i) => `<div class="step ${step.done ? 'completed' : step.current ? 'active' : ''}"><div class="step-top"><span class="step-index">${i + 1}</span><span class="step-name">${E(step.name)}</span><span class="step-status">${step.done ? i === steps.length - 1 ? '已保存' : '已确认' : closed ? '未完成' : step.current ? state.restore ? '待核对恢复' : '待确认' : '待进行'}</span></div><div class="step-detail">${E(step.detail)}</div></div>`).join('');
    $('#step-counter').textContent = steps.filter(step => step.done).length + '/' + steps.length + ' 步';
    $('#refresh-cycle').hidden = !window.Workspace.session;
  }
  function renderAlerts(state) {
    if (window.StationDetection.active) {window.StationDetection.render(); return;}
    if (!state || !document.body.classList.contains('station-task')) return;
    const row = state.cycle, active = row && row.stage !== 'CLOSED', alerts = [], E = window.Workspace.escape;
    if (window.Workspace.session) {
      if (state.storage === false) alerts.push({title: '证据存储不可用', reason: '无法保存阶段截图，请检查存储后重新同步。'});
      if (state.occupied && !row) alerts.push({title: '工位正在使用', reason: '另一个账户有未结束的检查记录。'});
      if (active && state.restore) alerts.push({title: '恢复检查待核对', reason: '请核对当前工件及前序记录后继续。'});
      if (active && !state.frameReady && row.stage !== 'READY') alerts.push({title: '当前阶段缺少图像', reason: '请连接摄像头或选择当前阶段的素材。'});
      if (row?.result && row.result !== 'MANUAL_OK') alerts.push({title: {NG: '本件确认异常', REVIEW: '本件待复核', INTERRUPTED: '本件检查已中断'}[row.result] || '本件需处置', reason: row.reason});
      if (['confirmed_ng', 'needs_rework'].includes(row?.review_status)) alerts.push({title: row.review_status === 'needs_rework' ? '本件需要返工' : '复核确认异常', reason: '请查看本件复核记录并处理。'});
      if (state.error) alerts.push({title: '操作未完成', reason: state.error});
    }
    const query = $('#alert-search').value.trim(), shown = alerts.filter(alert => (alert.title + alert.reason).includes(query));
    $('#alert-summary').textContent = '待处置 ' + alerts.length;
    $('#alert-count').textContent = shown.length + ' 条记录';
    $('#metric-alerts').textContent = alerts.length; $('#realtime-alert').textContent = alerts.length ? '需处置' : '无';
    $('#alert-list').innerHTML = shown.map(alert => `<article class="alert-card"><div class="alert-content"><h4>${E(alert.title)}</h4><p class="alert-description">${E(alert.reason || '')}</p>${row ? `<button class="tiny-button" data-detail="${E(row.id)}">查看本件记录</button>` : ''}</div></article>`).join('') || `<div class="empty-alerts"><span>${query ? '没有匹配的告警' : '暂无告警'}</span></div>`;
  }
  function update(state) {
    if (!document.body.classList.contains('station-task')) return;
    const row = state.cycle, checks = row?.checks || {}, E = window.Workspace.escape;
    const items = definitions(state).flatMap(stage => stage.items.map(item => ({...item, stage: stage.id})));
    const done = Object.values(checks).reduce((total, proof) => total + proof.items.length, 0);
    const label = row ? $('#cycle-stage')?.textContent || '待检查' : state.config?.registration_enabled ? '待开始本件' : '登记已停用';
    for (const id of ['metric-fps', 'metric-latency', 'sample-fps']) $('#' + id).textContent = '—';
    $('#metric-targets').textContent = done; $('#metric-targets').previousElementSibling.textContent = '已人工确认'; $('#metric-targets').nextElementSibling.textContent = '项';
    $('#metric-compliance').previousElementSibling.textContent = '本件状态'; $('#metric-compliance').textContent = label; $('#metric-compliance').className = 'text-yellow'; $('#metric-compliance').nextElementSibling.textContent = '';
    $('#current-process').textContent = `${done}/${items.length} 项人工确认`; $('#video-compliance').textContent = label;
    $('#compliant-count').textContent = done; $('#noncompliant-count').textContent = items.length - done;
    $('.compliance-panel .positive span').textContent = '人工确认项目'; $('.compliance-grid>div:nth-child(2) span').textContent = '尚未确认';
    $('#compliance-rate').textContent = label;
    $('.compliance-foot').textContent = '当前不运行模型，所有通过均含人工确认';
    workflow(state, label); renderAlerts(state);
    $('#run-state').textContent = label; $('#footer-performance').textContent = '阶段截图与人工操作保存在服务器';
    $('#mode-text').textContent = config.name + ' · ' + (row?.record_kind || (state.config?.registration_enabled ? '选择输入后开始本件' : '工件登记暂已停用'));
    $('#dwell').textContent = row ? Math.max(0, (row.finished_at || Date.now() / 1000) - row.started_at).toFixed(0) + 's' : '—';
    $('#detection-results').innerHTML = items.map(item => {const proof = checks[item.stage]; return `<tr><td>${E(item.id)}</td><td>${proof ? '已人工确认' : '待检查'}</td><td>${proof ? '人工' : '—'}</td><td>${E(item.label)}</td><td>${proof ? E(new Date(proof.at * 1000).toLocaleTimeString()) : '—'}</td><td>${proof ? E(proof.actor) : '—'}</td></tr>`;}).join('');
    $('#result-note').textContent = row ? `${config.name} · 工件 ${row.part_number} · ${row.record_kind} · 原始算法结论：未运行模型` : `本工位${items.length}项独立记录，下一件重新检查；动作记录项不代表零件数量`;
    const evidence = $('#pilot-stage-evidence');
    const signature = JSON.stringify([state.station, ...Object.values(checks).map(proof => proof.evidence.url)]);
    if (evidence.dataset.signature !== signature) {evidence.dataset.signature = signature; evidence.innerHTML = Object.entries(checks).map(([stage, proof]) => {const name = definitions(state).find(item => item.id === stage)?.name || stage; return `<a href="${E(proof.evidence.url)}" target="_blank" rel="noopener"><img src="${E(proof.evidence.url)}" alt="${E(name)}阶段证据"><span>${E(name)} · 已保存</span></a>`;}).join('') || '<p class="hint">阶段确认后，在此查看已保存的截图。</p>';}
    $('#server-account').textContent = window.Workspace.session ? window.Workspace.session.username + ' · 退出' : '登录服务器';
    $('#inspection-task').disabled = state.busy || Boolean(window.AISOP_TERMINAL);
  }
  return {activate, update, renderAlerts,
    startBlocked: () => window.StationDetection.active ? window.StationDetection.startBlocked() : window.StationOne?.startBlocked() || (!window.StationOne ? '工位尚未就绪' : ''),
    start: () => window.StationDetection.active ? window.StationDetection.start() : window.StationOne.startFromTrigger(),
    stopBlocked: () => window.StationDetection.active ? window.StationDetection.stopBlocked() : '当前工位未运行自动检测',
    stop: () => window.StationDetection.stop(),
    canLeave: () => window.StationDetection.active ? window.StationDetection.canLeave() : !window.StationOne || window.StationOne.canLeave(),
    deactivate: () => {window.StationDetection.deactivate(); window.StationOne?.deactivate(); document.body.classList.remove('station-task');},
    action(name) {
      if (window.StationDetection.active) return window.StationDetection.action(name);
      const controller = window.StationOne;
      if (['upload', 'start-upload', 'realtime'].includes(name)) return controller?.chooseSource();
      if (['screw-history', 'sop-history'].includes(name)) return controller?.history();
      if (['report', 'export', 'export-alerts'].includes(name)) return controller?.showRecord();
      if (name === 'account') return controller?.account();
      if (name === 'finish') return controller?.finish();
      const E = window.Workspace.escape;
      return showModal(config.name + '检查流程', `<p>${E(config.notes)}</p><p>${config.stages.map(item => E(item.name)).join('；')}</p>${config.pending_specs.length ? '<p>待现场确认：' + E(config.pending_specs.join('；')) + '。完成后保存为待复核。</p>' : ''}<p>当前为人工辅助，模型及视频自动分析待后续接入。异常、复核与返工记录按工位保存。</p>`);
    }};
})();
