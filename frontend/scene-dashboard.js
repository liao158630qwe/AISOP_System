/* Render published model tasks in the shared monitoring dashboard. */
window.SceneDashboard = (() => {
  const $ = selector => document.querySelector(selector), E = value => window.Workspace.escape(value);
  const labels = {PASS: '通过', CLEAR: '未出现禁项', FAIL: '异常', REVIEW: '待复核'};
  const normalize = frame => ({...frame, time: frame.time ?? frame.frameTime ?? 0,
    inspection: {registered: true, targets: [], displayTargets: (frame.observations || []).map(item => ({...item, id: item.name, state: 'PRESENT', positionMode: 'DETECTED'}))}});
  function apply(state) {
    const task = state.result?.config || state.model, name = task?.name || '场景检测任务';
    $('.brand p').textContent = name + ' · 按任务切换模型、类别与检测规则';
    $('.video-panel .subheading h3').innerHTML = '<b class="number blue">1</b>' + E(name) + ' / 当前采样';
    $('.pose-legend').textContent = '绿色为当前模型检出目标 · 过期采样清除标注 · 规则按当前任务配置判定';
    $('.workflow-panel .section-title h2').textContent = '任务规则判定';
    $('.timeline-panel .subheading h3').innerHTML = '<b class="number yellow-bg">3</b>任务采样时间轴';
    $('.compliance-panel .subheading h3').innerHTML = '<b class="number red-bg">4</b>视觉规则判定';
    $('.alerts-panel .section-title h2').textContent = '规则异常与待复核';
    $('#alert-search').placeholder = '搜索规则名称或异常说明';
    $('.video-stats>div:first-child span').textContent = '当前检测任务';
    $('.video-stats>div:last-child span').textContent = '视觉规则状态';
    $('.compliance-grid>div:nth-child(3) span').textContent = '视觉规则状态';
    $('.alert-toolbar .filters').hidden = true; $('[data-action="confirm-all"]').hidden = true;
    $('.alerts-foot').textContent = '仅核验本任务配置的视觉规则';
    $('.camera-name').textContent = name + ' · 等待输入';
    $('.stream-info').textContent = task?.modelName || '模型读取中';
    $('.footer>span').textContent = 'AI-SOP · ' + name;
    ['ID', '规则状态', '检出数量', '规则名称', '采样时间', '说明'].forEach((value, i) => $('.results-panel thead').querySelectorAll('th')[i].textContent = value);
    $('#video-empty p').textContent = '选择视频或开启摄像头，使用本任务绑定的模型检测';
    $('#finish-detection').textContent = '结束并判定';
    $('#metric-targets').previousElementSibling.textContent = '当前检出目标'; $('#metric-targets').nextElementSibling.textContent = '个';
    $('#metric-compliance').previousElementSibling.textContent = '视觉规则'; $('#metric-compliance').nextElementSibling.textContent = '';
    $('.compliance-panel .positive span').textContent = '规则通过'; $('.compliance-grid>div:nth-child(2) span').textContent = '规则异常';
    $('.timeline-panel .legend').innerHTML = '<span><i style="background:#39bd95"></i>有效采样</span>';
    $('#inspection-task').value = state.task;
    const rate = String(task?.sample_fps || 4), selector = $('#live-rate');
    if (![...selector.options].some(option => option.value === rate)) selector.add(new Option(rate + ' fps', rate));
    selector.value = rate;
  }
  function rules(state) {
    const config = state.result?.config || state.model, model = state.result?.model || config?.model;
    if (!config) return;
    showModal('检测任务与模型', `<p>${E(config.name)} · r${E(state.result?.sceneRevision || config.revision)} · ${E(model?.name)} v${E(model?.version)}</p><p>模型校验 ${E(model?.sha256 || '')}</p>${(config.rules || []).map(rule => `<p><strong>${E(rule.name)}</strong> · ${rule.kind === 'required' ? '必须检出' : '禁止出现'} ${E(model?.classes?.[rule.class_id] || rule.class_id)} · 阈值 ${rule.threshold} · 区域 ${rule.roi.join(', ')}</p>`).join('')}`);
  }
  function render(state, frame) {
    state.renderedPanelFrame = frame;
    const snapshot = state.final || frame, checks = snapshot?.checks || [], observations = frame?.observations || [];
    const decision = snapshot?.compliance || {status: 'REVIEW', label: '待采样', reason: '等待当前模型的有效采样'};
    const t = frame?.time || 0, name = state.result?.sceneName || state.model?.name || '场景检测';
    $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour12: false});
    $('.cpu-chip').textContent = state.result?.model.name || state.model?.modelName || '模型读取中';
    $('#metric-targets').textContent = String(observations.length)+(frame?.observationsTruncated?'+':''); $('#metric-alerts').textContent = checks.filter(c => c.state === 'FAIL').length;
    $('#metric-fps').textContent = state.mode === 'realtime' ? state.actualFps?.toFixed(1) || '—' : state.result?.summary.sampleFps || '—';
    $('#sample-fps').textContent = $('#metric-fps').textContent;
    $('#metric-latency').textContent = Math.round(frame?.inferenceMs || state.processing) || '—';
    for (const id of ['metric-compliance', 'compliance-rate', 'video-compliance']) $('#' + id).textContent = decision.label;
    $('#metric-compliance').className = decision.status === 'PASS' ? 'text-green' : 'text-yellow';
    $('#compliant-count').textContent = checks.filter(c => ['PASS', 'CLEAR'].includes(c.state)).length;
    $('#noncompliant-count').textContent = checks.filter(c => c.state === 'FAIL').length;
    $('#realtime-alert').textContent = decision.status === 'FAIL' ? '检出异常' : decision.status === 'REVIEW' ? '待复核' : '无';
    $('#current-process').textContent = name; $('#dwell').textContent = t.toFixed(1) + 's';
    $('#video-empty').hidden = Boolean(state.mode === 'realtime' ? state.camera : state.result);
    $('#detection-results').innerHTML = checks.map(c => `<tr><td>${E(c.id)}</td><td>${E(labels[c.state] || c.state)}</td><td>${c.count ?? '—'}</td><td>${E(c.name)}</td><td>${t.toFixed(2)}s</td><td>${E(c.reason)}</td></tr>`).join('');
    $('#result-note').textContent = `当前 ${observations.length} 个目标 · ${checks.length} 项规则 · ${state.result ? '历史任务 r' + state.result.sceneRevision : '任务 r' + (state.frame?.sceneRevision || state.model?.revision || '—')} · 仅显示当前采样${frame?.observationsTruncated ? ' · 画面仅展示前300个目标，规则按各区域完整结果判断' : ''}`;
    $('#workflow-name').textContent = name; $('#workflow-status').textContent = decision.label; $('#workflow-status').dataset.verdict = decision.status;
    $('#workflow-description').textContent = decision.reason; $('#step-counter').textContent = checks.length + ' 项规则';
    const list = checks.length ? checks : (state.model?.rules || []).map(c => ({...c, state: 'REVIEW', reason: '等待有效采样'}));
    $('#steps').innerHTML = list.map((c, i) => `<div class="step ${['PASS', 'CLEAR'].includes(c.state) ? 'completed' : ''}"><div class="step-top"><span class="step-index">${i + 1}</span><span class="step-name">${E(c.name)}</span><span class="step-status">${E(labels[c.state])}</span></div><div class="step-detail">${E(c.reason)}</div></div>`).join('');
    $('#sticker-sequence').hidden = true; $('#screw-verdict').hidden = !snapshot;
    $('#screw-verdict').dataset.verdict = decision.status;
    $('#screw-verdict').textContent = (state.final ? '结束判定：' : '当前采样：') + decision.label + ' · ' + decision.reason;
    $('.compliance-foot').textContent = '逐采样视觉规则，不代表完整生产工序合格率';
    $('#finish-detection').disabled = state.busy || (!state.running && !state.result);
    $('#start-live').disabled = state.running || state.busy || !state.model?.ready;
    for (const id of ['video-mode', 'server-account', 'inspection-task']) $('#' + id).disabled = state.busy;
    $('#camera-device').disabled = state.busy || state.running;
    $('#cancel-task').disabled = state.cancelling; $('#cancel-task').textContent = state.cancelling ? '正在取消…' : state.upload ? '取消上传' : '取消任务';
    $('#run-state').textContent = state.running ? '实时检测中' : state.final ? '检测已结束' : '等待检测';
    $('.live').textContent = state.running ? '● LIVE' : '任务检测';
    $('#footer-performance').textContent = `${name} · ${state.result?.summary.analyzedFrames || state.frames.length} 次采样`;
  }
  function alerts(state, frame) {
    const checks = (state.final || frame)?.checks || [], query = $('#alert-search').value.trim();
    const issues = checks.filter(c => ['FAIL', 'REVIEW'].includes(c.state));
    $('#alert-summary').textContent = issues.length + ' 项需关注'; $('#alert-count').textContent = issues.length + ' 条记录';
    $('#alert-list').innerHTML = issues.filter(c => (c.name + c.reason).includes(query)).map(c => `<article class="alert-card"><div class="alert-content"><h4>${E(c.name)} · ${E(labels[c.state])}</h4><p class="alert-description">${E(c.reason)}</p></div></article>`).join('') || '<div class="empty-alerts"><span>当前没有匹配的异常或待复核记录</span></div>';
  }
  function exportLive(state) {
    downloadCSV([['采样时间', '规则', '状态', '数量', '说明'], ...state.frames.flatMap(frame =>
      (frame.checks || []).map(check => [frame.time, check.name, labels[check.state], check.count, check.reason]))], '场景检测.csv');
  }
  return {normalize, apply, rules, render, alerts, exportLive};
})();
