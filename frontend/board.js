/* Live board with persistent daily archives; no external chart/CDN dependency. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector), W = window.Workspace, E = W.escape;
  const number = value => Number(value || 0).toLocaleString('zh-CN');
  const percent = value => value == null ? '—' : Number(value).toFixed(1);
  const timeFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const dayFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',weekday:'short'});
  const alertDateFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  const today = () => new Date(Date.now() + 8*3600000).toISOString().slice(0,10);
  let timeRange = 'all', selectedDate = today();
  try {
    const saved = JSON.parse(localStorage.getItem('aisop-board-range') || 'null');
    if (['all','today','date'].includes(saved?.range)) timeRange = saved.range;
    if (/^\d{4}-\d{2}-\d{2}$/.test(saved?.date || '') && Number.isFinite(Date.parse(saved.date))) selectedDate = saved.date;
  } catch {}
  const boardDate = () => timeRange === 'date' ? selectedDate : today();
  const historical = () => mode === 'live' && (data?.range ? !data.range.includesLive : timeRange === 'date' && selectedDate !== today());
  const rangeLabel = () => mode === 'demo' ? '演示数据' : data?.range?.label ||
    (timeRange === 'all' ? '总体 · 全部日期' : (timeRange === 'today' ? '当日 · ' : '指定日期 · ') + boardDate());
  const time = value => timeFormat.format(new Date(value * 1000));
  const states = {RUNNING:'检测运行', ALARM:'违规告警', REVIEW:'待复核', MANUAL:'人工检查', OFFLINE:'离线', UNKNOWN:'待连接'};
  const titles = ['4孔检查后4螺丝', '5螺丝检查', '2螺丝与黄色卡扣', '2螺丝检查', '接端子后8螺丝', '吹净、端子与4螺丝'];
  const views = ['overview', 'stations', 'quality'];
  let mode = new URLSearchParams(location.search).get('demo') === '1' ? 'demo' : 'live';
  let data = null, period = 'hour', view = 'overview', generation = 0, requestController, pollTimer;
  let seen = new Set(), initialized = false, queue = [], rotation = false, rotationAt = 0;
  let autoAlert = true, autoScroll = true, hovering = false, toastTimer, alertSignature = '', demoTick = 0;
  let lastSync = 0, connected = false, modeStarted = 0, pendingDialogTimer;
  let lastReceiveMono = 0, retryCount = 0, blocked = false, suspended = false, queueOverflow = 0;
  let activeAlert = null, snapshotDay = '', chartFrame = 0, lifecycleTimer, scrollTimer;
  const signatures = new Map(), stationSignatures = new Map();
  const MAX_QUEUE = 50, MAX_SEEN = 2000;
  function changed(key, value) {
    const signature = JSON.stringify(value);
    if (signatures.get(key) === signature) return false;
    signatures.set(key, signature); return true;
  }
  function modalOpen() { return !!document.querySelector('dialog[open]'); }
  function scheduleChart() {
    if (chartFrame || suspended || document.hidden) return;
    chartFrame = requestAnimationFrame(() => { chartFrame = 0; if (data) renderChart(); });
  }
  const demoTypes = ['漏螺丝', '缺卡扣', '未吹净', '漏接端子', '工序跳步', '其他异常'];

  function emptyData() {
    return {summary:{total:null, passed:null, failed:null, pending:null, complianceRate:null}, stations:titles.map((title, i) => ({
      id:'station-' + (i + 1), name:'工位' + (i + 1), title, status:'UNKNOWN', total:0,
      partNumber:null, stage:'等待数据连接', complianceRate:null, effectiveFps:0, steps:[], reason:''})),
      alerts:[], alertCount:0, top:demoTypes.map(name => ({name, count:0})), trends:{hour:[], shift:[]}};
  }

  function toast(message) {
    $('#board-toast').textContent = message; $('#board-toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#board-toast').hidden = true; }, 4500);
  }
  function notice(message, kind = '') {
    $('#notice').textContent = message; $('#notice').dataset.kind = kind; $('#notice').hidden = !message;
  }
  function connection(state, label) {
    $('#connection').dataset.state = state; $('#connection span').textContent = label;
  }
  function clock() {
    const date = new Date();
    $('#clock').textContent = timeFormat.format(date);
    const calendar = dayFormat.format(date);
    if ($('#calendar').textContent !== calendar) $('#calendar').textContent = calendar;
    if (lastSync) {
      const age = Math.floor((performance.now() - lastReceiveMono) / 1000);
      $('#updated').textContent = (mode === 'demo' ? '演示更新 ' : '数据同步 ') + time(lastSync / 1000) + (age > 6 ? ` · ${age} 秒前` : ' · UTC+08:00');
      if (mode === 'live' && connected && age > 10 && !document.hidden) {
        connected = false; connection('error', '数据已过期');
        notice('数据超过 10 秒未更新，当前显示上次快照，正在重新连接。', 'error');
        staleStations();
      }
    }
    if (rotation) {
      if (modalOpen() || document.hidden) rotationAt = performance.now();
      const remaining = Number($('#rotation-interval').value) - Math.floor((performance.now() - rotationAt) / 1000);
      $('#rotation-toggle').textContent = `Ⅱ 暂停 · ${Math.max(0, remaining)}s`;
      if (remaining <= 0) changeView(views[(views.indexOf(view) + 1) % views.length]);
    }
  }

  function renderStations(stations) {
    if (changed('station-status', [historical(),stations.map(s => [s.name,s.status,s.online,s.total])])) {
    $('#running-count').textContent = stations.filter(station => historical() ? station.total > 0 : station.online ?? ['RUNNING','MANUAL'].includes(station.status)).length;
    $('#station-count-label').textContent = historical() ? ' / 6 工位有归档记录' : ' / 6 工位采样在线';
    $('#station-note').textContent = historical() ? '所选日期最后归档结果' : stations.some(s => s.status === 'ALARM') ? '异常工位请优先处置' : '直接接收检测结果 · 无需工件';
    $('#production-track').innerHTML = stations.map(s => `<i data-status="${E(s.status)}" title="${E(s.name + ' · ' + states[s.status])}"></i>`).join('');
    }
    stations.forEach((s, i) => {
      const signature = JSON.stringify([s,timeRange,boardDate(),historical()]);
      if (stationSignatures.get(s.id) === signature) return;
      stationSignatures.set(s.id, signature);
      const holder = document.createElement('div');
      const detection = s.detection;
      const label = historical() ? (s.total ? '历史归档' : '暂无记录') : detection && s.online ? '采样在线' : states[s.status] || '待连接';
      const subject = detection ? s.cameraId || '等待相机接入' : s.partNumber || '— 等待工件接入';
      const showFps = detection ? s.online : ['RUNNING','ALARM'].includes(s.status);
      const knownProgress = detection && detection.expected > 0 && detection.confirmed != null;
      const progress = knownProgress ? `${detection.confirmed}/${detection.expected} 已确认` : '';
      const currentLabel = detection ? `${detection.label}${progress ? ' · ' + progress : ''}` : '';
      const meter = knownProgress ? detection.confirmed / detection.expected * 100 : s.complianceRate || 0;
      holder.innerHTML = `<article class="station" data-station="${E(s.id)}" data-stream="${E(s.streamId || '')}" data-frame-seq="${E(detection?.frameSeq ?? '')}" data-status="${E(s.status)}" aria-label="${E(s.name + ' ' + states[s.status])}">
      <div class="station-top"><span class="station-number">0${i + 1}</span><h3>${detection && mode === 'live' ? `<a href="/?task=${E(s.id.replace('station-', 'station'))}" target="_blank" rel="noopener" title="打开本工位检测与记录">${E(s.name)} ↗</a>` : E(s.name)}</h3><span class="station-badge">${E(label)}</span></div>
      <p class="station-title">${E(s.title)}${s.sourceLabel ? ' · ' + E(s.sourceLabel) : ''}</p><div class="station-product"><span title="${E(subject)}">${E(subject)}</span><small>${s.status === 'MANUAL' ? '人工确认' : showFps ? E(Number(s.effectiveFps || 0).toFixed(1)) + ' fps' : '—'}</small></div>
      ${detection ? `<p class="station-decision" data-verdict="${E(detection.status)}">${E(currentLabel)}</p>` : ''}
      <p class="station-step" title="${E(s.reason || s.stage)}"><span>↳</span>${E(s.status === 'ALARM' || s.status === 'REVIEW' ? s.reason : s.stage)}</p>
      <div class="station-extra">${(s.steps || []).map(step => `<span>${E(step)}</span>`).join('')}</div>
      <div class="station-bottom"><span>${detection ? timeRange === 'all' ? '累计' : timeRange === 'date' ? '所选日' : '今日' : '已检'}</span><b>${s.status === 'UNKNOWN' ? '—' : number(s.total)}</b><span>${detection ? '轮检测' : '件'}</span><strong>${percent(s.complianceRate)}${s.complianceRate == null ? '' : '%'}</strong></div>
      <div class="station-meter" aria-hidden="true"><i style="--rate:${Math.min(100, Math.max(0, meter))}%"></i></div></article>`;
      const old = $('#stations').children[i];
      if (old) old.replaceWith(holder.firstElementChild); else $('#stations').append(holder.firstElementChild);
    });
  }
  function staleStations() {
    if (!data) return;
    renderStations(data.stations.map(station => ({...station, status:'UNKNOWN', online:false,
      detection:station.detection ? {status:'REVIEW',label:'数据已过期',confirmed:null,expected:null} : undefined,
      reason:'连接中断，等待新快照', stage:'数据已过期 · 等待重新连接'})));
    $('#running-count').textContent = '—';
    $('#station-note').textContent = '实时状态暂不可用';
  }

  function renderAlerts() {
    $('#alert-count').textContent = number(data.alertCount);
    $('#alert-note').textContent = data.alertCount > 100 ? `共 ${number(data.alertCount)} 条 · 展示最新 100 条` : '最新事件优先 · 点击查看详情';
    const signature = JSON.stringify(data.alerts);
    if (signature === alertSignature) return;
    alertSignature = signature;
    const viewport = $('#alert-viewport'), position = viewport.scrollTop;
    const focusedId = viewport.contains(document.activeElement) ? document.activeElement.dataset.alert : null;
    $('#alert-list').innerHTML = data.alerts.length ? data.alerts.map(a => `<button class="alert-row" data-alert="${E(a.id)}" aria-label="查看${E(a.types.join('、'))}告警">
      <span class="alert-icon" aria-hidden="true">!</span><span class="alert-main"><strong>${E(a.types.join(' · '))}</strong><small><span>${E(a.stationId.replace('station-', '工位 '))}</span>${E(a.partNumber)} · ${E(a.source)}</small></span><time>${E(mode === 'live' && timeRange === 'all' ? alertDateFormat.format(new Date(a.at*1000)) : time(a.at))}</time></button>`).join('')
      : '<div class="empty-state"><span>◎</span><strong>暂无违规事件</strong><p>新事件将在此自动更新</p></div>';
    viewport.scrollTop = position;
    if (focusedId) [...$('#alert-list').children].find(element => element.dataset.alert === focusedId)?.focus({preventScroll:true});
  }

  function renderRanking() {
    if (!changed('ranking', data.top)) return;
    const total = data.top.reduce((sum, item) => sum + item.count, 0);
    const max = Math.max(1, ...data.top.map(item => item.count));
    $('#ranking').innerHTML = data.top.map((item, i) => `<div class="rank-row"><span class="rank-number">${String(i + 1).padStart(2,'0')}</span><span class="rank-label">${E(item.name)}</span><div class="rank-bar"><i style="--width:${item.count / max * 100}%"></i></div><b>${number(item.count)}</b><small>${total ? Math.round(item.count / total * 100) : 0}%</small></div>`).join('');
    $('#top-total').textContent = number(total) + ' 次';
  }

  function renderChart() {
    if (!$('#trend-chart').clientWidth || !$('#trend-chart').clientHeight || document.hidden) return;
    const points = data.trends[period], values = points.filter(point => point.rate != null).map(point => point.rate);
    const width = Math.max(340, $('#trend-chart').clientWidth - 30), height = Math.max(150, $('#trend-chart').clientHeight - 9);
    if (!changed('chart', [period, width, height, points,rangeLabel()])) return;
    const left = 39, right = width - 18, top = 17, bottom = height - 28;
    const minimum = values.length ? Math.max(0, Math.min(90, Math.floor((Math.min(...values) - 3) / 10) * 10)) : 0;
    const x = i => left + (right - left) * (points.length > 1 ? i / (points.length - 1) : .5);
    const y = value => bottom - (value - minimum) / (100 - minimum) * (bottom - top);
    const grid = Array.from({length:5}, (_, i) => {
      const value = minimum + (100 - minimum) * i / 4;
      return `<line class="chart-grid" x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}"/><text x="${left - 9}" y="${y(value) + 4}" text-anchor="end">${Number(value.toFixed(1))}%</text>`;
    }).join('');
    const segments = []; let segment = [];
    points.forEach((point, i) => { if (point.rate == null) { if (segment.length) segments.push(segment); segment = []; } else segment.push([x(i), y(point.rate)]); });
    if (segment.length) segments.push(segment);
    const paths = segments.map(items => {
      const path = items.map(([px, py], i) => `${i ? 'L' : 'M'}${px},${py}`).join(' ');
      return `<path d="${path} L${items.at(-1)[0]},${bottom} L${items[0][0]},${bottom} Z" fill="url(#chart-fill)"/><path class="chart-line" d="${path}"/>`;
    }).join('');
    const labels = points.map((point, i) => (period === 'shift' || i % (width < 500 ? 4 : 3) === 0 || (width >= 500 && i === points.length - 1))
      ? `<text x="${x(i)}" y="${height - 6}" text-anchor="${i === points.length - 1 ? 'end' : 'middle'}">${E(point.label)}</text>` : '').join('');
    const dots = points.map((point, i) => point.rate == null ? '' : `<circle class="chart-point" data-point="${i}" tabindex="0" role="img" aria-label="${E(point.label)} 合规率 ${percent(point.rate)}%，通过 ${point.passed}，不通过 ${point.failed}" cx="${x(i)}" cy="${y(point.rate)}" r="3.2"><title>${E(point.label)} · ${percent(point.rate)}% · 通过 ${point.passed} / 不通过 ${point.failed}</title></circle>`).join('');
    const pending = points.reduce((sum, point) => sum + Number(point.pending || 0), 0);
    const empty = pending ? `已归档 ${number(pending)} 轮待复核 · 暂无可计算合规率` : '暂无已归档检测结果';
    $('#trend-chart').innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-label="${period === 'hour' ? '小时' : '班次'}归档检测合规率趋势"><defs><linearGradient id="chart-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#efb54a" stop-opacity=".19"/><stop offset="1" stop-color="#efb54a" stop-opacity=".01"/></linearGradient></defs>${grid}<line class="target-line" x1="${left}" x2="${right}" y1="${y(98)}" y2="${y(98)}"/>${paths}${dots}${labels}</svg>${values.length ? '' : `<div class="chart-empty">${E(empty)}</div>`}<div class="chart-tooltip" hidden></div>`;
    $('#trend-note').textContent = (mode === 'live' && timeRange === 'all' ? '全部日期 · 同时段汇总' : rangeLabel()) +
      (period === 'hour' ? ' · UTC+08:00' : ' · 每班 8 小时');
  }

  function render(next) {
    data = next;
    $('#scope-label').textContent = rangeLabel();
    $('#total-label').textContent = (mode === 'demo' ? '演示' : timeRange === 'all' ? '总体' : timeRange === 'today' ? '当日' : '所选日') + '检测轮次';
    $('#total-code').textContent = mode === 'demo' ? 'DETECTIONS / DEMO' : timeRange === 'date' ? boardDate() : 'DETECTIONS / ' + timeRange.toUpperCase();
    $('.metrics').setAttribute('aria-label',rangeLabel() + '检测统计');
    $('#verdict-note').textContent = historical() ? '按所选日期归档结论' : '含本轮实时判定';
    $('#alerts-heading').textContent = mode === 'demo' ? '实时违规告警' : timeRange === 'all' ? '总体违规告警' : historical() ? '所选日期违规告警' : '当日违规告警';
    $('#ranking-scope').textContent = historical() ? '所选日期归档异常' : '归档与当前实时异常';
    const s = data.summary;
    if (changed('summary', s)) {
    for (const key of ['total','passed','failed']) $('#' + key).textContent = s[key] == null ? '—' : number(s[key]);
    $('#pending').textContent = `待判定 ${s.pending == null ? '—' : number(s.pending)} 轮`;
    $('#pass-share').textContent = s.total ? '占比 ' + percent(s.passed / s.total * 100) + '%' : '占比 —';
    $('#rate').textContent = percent(s.complianceRate);
    }
    $('#rate').title = next.statisticsAt ? `${rangeLabel()}，更新于 ${time(next.statisticsAt)}${historical() ? '，按归档结论统计' : '，含当前实时判定'}` : '';
    renderStations(data.stations); renderAlerts(); renderRanking(); scheduleChart();
  }

  function receive(next) {
    if (next.date && snapshotDay && snapshotDay !== next.date) {
      queue = []; queueOverflow = 0; seen.clear(); initialized = false; activeAlert = null; $('#alert-dialog').hidden = true;
    }
    snapshotDay = next.date || snapshotDay;
    if (initialized && autoAlert && (next.range ? next.range.includesLive : !historical())) {
      const fresh = next.alerts.filter(alert => !seen.has(alert.id)).sort((a, b) => a.at - b.at);
      queue.push(...fresh);
      if (queue.length > MAX_QUEUE) {
        queueOverflow += queue.length - MAX_QUEUE; queue = queue.slice(-MAX_QUEUE);
      }
    }
    // Refresh visible IDs so a long-running alarm cannot age out and re-alert.
    next.alerts.forEach(alert => { seen.delete(alert.id); seen.add(alert.id); });
    // Retain recent IDs without allowing an unattended display to grow forever.
    if (seen.size > MAX_SEEN) seen = new Set([...seen].slice(-MAX_SEEN));
    if ((activeAlert?.source === '实时检测' || activeAlert?.id?.startsWith('live:')) && !next.alerts.some(alert => alert.id === activeAlert.id)) {
      activeAlert = null; $('#alert-dialog').hidden = true;
    }
    queue = queue.filter(alert => !(alert.source === '实时检测' || alert.id.startsWith('live:')) || next.alerts.some(item => item.id === alert.id));
    initialized = true; lastSync = Date.now(); lastReceiveMono = performance.now(); connected = true;
    updateQueueLabel();
    render(next); showNextAlert(); clock();
  }
  function showAlert(alert, preview = false) {
    activeAlert = alert;
    $('#alarm-title').textContent = alert.types.join(' · ');
    $('#alarm-station').textContent = alert.stationId.replace('station-', '工位 ');
    $('#alarm-reason').textContent = alert.reason;
    $('#alarm-part').textContent = alert.partNumber;
    $('#alarm-time').textContent = new Date(alert.at * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
    $('#alarm-kind').textContent = alert.source;
    $('#alarm-source').textContent = preview ? '预览 · 不计入统计' : mode === 'demo' ? '演示数据 · 非现场事件' : '现场事件';
    updateQueueLabel();
    $('#alert-dialog').hidden = false;
  }
  function updateQueueLabel() {
    $('#alarm-queue').textContent = queueOverflow ? `待看 ${queue.length} 条 · 另 ${queueOverflow} 条请查记录`
      : queue.length ? `另有 ${queue.length} 条新告警等待查看` : '现场核对后请及时处置';
  }
  function showNextAlert() {
    if (!autoAlert || document.hidden || !$('#alert-dialog').hidden || modalOpen() || !queue.length) return;
    showAlert(queue.shift());
  }
  function closeAlert() {
    activeAlert = null;
    const focused = $('#alert-dialog').contains(document.activeElement);
    $('#alert-dialog').hidden = true;
    if (focused) $('#test-alert').focus({preventScroll:true});
    clearTimeout(pendingDialogTimer); pendingDialogTimer = setTimeout(showNextAlert, 180);
  }
  $('#dismiss-alert').onclick = closeAlert;
  $('#close-alert').onclick = closeAlert;
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('#alert-dialog').hidden && !document.querySelector('dialog[open]')) closeAlert();
  });
  $('#alert-list').onclick = event => {
    const id = event.target.closest('[data-alert]')?.dataset.alert;
    const alert = data.alerts.find(item => item.id === id);
    if (alert) showAlert(alert);
  };
  $('#test-alert').onclick = () => showAlert({types:['漏螺丝'], stationId:'station-3', partNumber:'PREVIEW-0001',
    reason:'告警提示预览：螺丝 S02 未检测到，请在工位终端核对安装状态。此提示不会写入生产记录。', at:Date.now() / 1000, source:'提示预览'}, true);
  $('#auto-alert').onclick = () => {
    autoAlert = !autoAlert; $('#auto-alert').setAttribute('aria-pressed', String(autoAlert));
    if (!autoAlert) { queue = []; queueOverflow = 0; updateQueueLabel(); }
    toast(autoAlert ? '新违规事件将在页面底部提示' : '已关闭自动提示，告警列表继续更新');
  };

  function demoData() {
    const now = Date.now() / 1000, counts = [18, 11, 7, 4, 3, 2], hour = new Date((now + 8 * 3600) * 1000).getUTCHours();
    const total = 1286, failed = 32, pending = 6, passed = total - failed - pending;
    const statuses = ['RUNNING','RUNNING','ALARM','RUNNING','REVIEW','RUNNING'];
    const rates = [99.2,98.7,96.8,99.6,97.4,98.5];
    const stations = titles.map((title, i) => ({id:'station-' + (i + 1),name:'工位' + (i + 1),title,status:statuses[i],
      reason:i === 2 ? 'S02 螺丝缺失，请核对安装状态' : i === 4 ? '端子图像遮挡，等待人工复核' : '正在接收检测结果',
      partNumber:'ASM-' + String(1287 + i).padStart(6, '0'),stage:['定位孔检查','螺丝安装检查','黄色卡扣检查','螺丝安装检查','端子连接检查','壳体吹净检查'][i],
      total:[1286,1284,1281,1279,1277,1274][i],passed:1200,failed:5,pending:2,complianceRate:rates[i],effectiveFps:[9.8,10,9.6,9.9,8.2,9.7][i],
      steps:[['定位孔检查','4处螺丝'],['5处螺丝'],['2处螺丝','黄色卡扣'],['2处螺丝'],['端子连接','8处螺丝'],['壳体吹净','装饰框吹净','线束端子','4处螺丝']][i]}));
    const alerts = Array.from({length:12}, (_, i) => ({id:'demo-' + i, at:now - i * 137 - 8,
      stationId:'station-' + [3,5,6,2,1,4][i % 6], partNumber:'ASM-' + String(1281 - i * 3).padStart(6,'0'),
      types:[demoTypes[i % 5]], source:i % 3 ? '算法归档' : '人工确认',
      reason:['S02 螺丝缺失，需重新核对该位置的安装状态。','黄色卡扣缺失，需补装并确认到位。','壳体未吹净，请核对规定区域。','端子漏接，请检查线束连接位置。','检测到工序跳步，请核对前置检查记录。'][i % 5]}));
    const hourPoints = Array.from({length:24}, (_, h) => {
      const r = [96.7,97.4,98.2,97.6,98.6,98.1,99.1,98.3,97.8,98.6,99.2,98.7,99.1,98.5,99.4,98.9,99.3,98.4,98.7,99.1,98.5,99.2,98.8,99.5][h];
      return {label:String(h).padStart(2,'0') + ':00', rate:h <= hour ? r : null, passed:Math.round(r * 4),failed:Math.round((100 - r) * 4),pending:0};
    });
    return {generatedAt:now, summary:{total,passed,failed,pending,complianceRate:98.4},stations,alerts,alertCount:45,
      top:demoTypes.map((name, i) => ({name,count:counts[i]})),trends:{hour:hourPoints,shift:['夜班 00–08','早班 08–16','晚班 16–24'].map((label, i) => ({label,rate:hour >= i * 8 ? [98.1,98.7,99.1][i] : null,passed:[1820,2300,1890][i],failed:[35,30,17][i],pending:0}))}};
  }

  function updateDemo() {
    demoTick++;
    const next = structuredClone(data);
    next.generatedAt = Date.now() / 1000;
    next.summary.total += 2; next.summary.passed += 2;
    next.stations.forEach(station => { station.total += 2; });
    if (demoTick % 3 === 0) {
      const type = demoTypes[(demoTick / 3 - 1) % 5];
      next.alerts.unshift({id:'demo-new-' + demoTick,at:Date.now() / 1000,stationId:'station-3',partNumber:'ASM-' + (1286 + demoTick * 2),types:[type],source:'演示事件',reason:'演示告警：' + type + '，请在对应工位核对。'});
      next.alerts = next.alerts.slice(0,100); next.alertCount++;
      next.top.find(item => item.name === type).count++;
      next.top.sort((a,b) => b.count - a.count);
      next.summary.passed--; next.summary.failed++;
    }
    receive(next);
  }

  async function poll(version) {
    if (version !== generation || mode !== 'live' || !W.session || blocked || suspended || document.hidden) return;
    if (!navigator.onLine) { connection('error','网络离线'); staleStations(); notice('网络已断开，连接恢复后自动同步。','error'); return; }
    requestController = new AbortController();
    let nextDelay = 3000;
    try {
      const params = new URLSearchParams({mode:'detection', range:timeRange});
      if (timeRange === 'date') params.set('date',selectedDate);
      const next = await W.request('/board/snapshot?' + params, {timeout:8000, signal:requestController.signal});
      if (version !== generation) return;
      retryCount = 0;
      nextDelay = Math.max(1000, Math.min(30000, Number(next.pollAfterMs) || 3000));
      receive(next); connection('live','实时连接'); notice('');
    } catch (error) {
      if (version !== generation) return;
      connected = false; connection('error','连接中断'); staleStations();
      blocked = [401,403,404,422].includes(error.status);
      retryCount++;
      nextDelay = Math.max(Math.min(30000, 3000 * 2 ** Math.min(retryCount - 1, 4)) * (1 + Math.random() * .15),
        Math.min(60000, Number(error.retryAfterMs) || 0));
      notice('实时数据获取失败，' + (lastSync ? '当前保留上次统计；' : '') + (blocked ? '请重新登录或检查服务版本。' : `约 ${Math.ceil(nextDelay / 1000)} 秒后重试。`) +
        (error.status === 403 ? '当前账户无全局看板权限。' : error.message), 'error');
    } finally {
      if (version === generation) {
        requestController = null;
        if (W.session && !blocked && !document.hidden && !suspended) pollTimer = setTimeout(() => poll(version), nextDelay);
      }
    }
  }

  function startMode() {
    window.BoardHistory?.reset();
    generation++; clearTimeout(pollTimer); requestController?.abort(); clearTimeout(pendingDialogTimer);
    queue = []; seen = new Set(); initialized = false; lastSync = 0; connected = false; alertSignature = 'reset'; demoTick = 0;
    retryCount = 0; blocked = false; activeAlert = null; queueOverflow = 0; snapshotDay = '';
    signatures.clear(); stationSignatures.clear();
    $('#alert-dialog').hidden = true;
    $('#updated').textContent = '尚未同步'; modeStarted = performance.now();
    $('#live-mode').setAttribute('aria-pressed', String(mode === 'live'));
    $('#demo-mode').setAttribute('aria-pressed', String(mode === 'demo'));
    document.querySelectorAll('[data-range]').forEach(button => {
      button.disabled = mode === 'demo'; button.setAttribute('aria-pressed',String(button.dataset.range === timeRange));
    });
    $('#board-date').value = selectedDate; $('#board-date').max = today();
    $('#board-date').hidden = timeRange !== 'date'; $('#board-date').disabled = mode === 'demo';
    $('#account').textContent = W.session ? '退出登录' : '登录看板';
    if (mode === 'demo') {
      connection('demo','演示数据'); notice('演示模式 · 以下为模拟生产数据，每 8 秒更新；不计入实际生产统计。'); receive(demoData());
    } else {
      render(emptyData()); connection('idle', W.session ? '正在连接' : '等待登录');
      notice(W.session ? '正在同步产线数据…' : '请登录以查看现场数据，或切换“演示模式”预览看板。');
      if (W.session) poll(generation);
    }
  }
  $('#live-mode').onclick = () => { if (mode !== 'live') { mode = 'live'; startMode(); } };
  $('#demo-mode').onclick = () => { if (mode !== 'demo') { mode = 'demo'; startMode(); } };
  function changeRange(next, date=selectedDate) {
    if (mode !== 'live') return;
    timeRange = next; selectedDate = date;
    try { localStorage.setItem('aisop-board-range',JSON.stringify({range:timeRange,date:selectedDate})); } catch {}
    startMode();
  }
  document.querySelectorAll('[data-range]').forEach(button => { button.onclick = () => {
    if (timeRange !== button.dataset.range) changeRange(button.dataset.range);
    if (timeRange === 'date') $('#board-date').focus();
  }; });
  $('#board-date').onchange = () => {
    if (!$('#board-date').checkValidity()) { $('#board-date').reportValidity(); return; }
    changeRange('date', $('#board-date').value);
  };
  $('#account').onclick = async () => {
    try { if (W.session) await W.logout(); else await W.login(); } catch (error) { toast(error.message); }
  };
  document.addEventListener('workspace-session', startMode);

  function changeView(next) {
    view = next; $('.board').dataset.view = view; rotationAt = performance.now();
    document.querySelectorAll('.view-switch [data-view]').forEach(button => {
      if (button.dataset.view === view) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
    });
    $('#rotation-count').textContent = '0' + (views.indexOf(view) + 1) + ' / 03';
    if (data) scheduleChart();
  }
  document.querySelectorAll('.view-switch [data-view]').forEach(button => { button.onclick = () => changeView(button.dataset.view); });
  $('#rotation-toggle').onclick = () => {
    rotation = !rotation; rotationAt = performance.now(); $('#rotation-toggle').setAttribute('aria-pressed',String(rotation));
    if (!rotation) $('#rotation-toggle').textContent = '▶ 开始轮播'; else clock();
  };
  $('#rotation-interval').onchange = () => { rotationAt = performance.now(); clock(); };
  document.querySelectorAll('[data-period]').forEach(button => { button.onclick = () => {
    period = button.dataset.period;
    document.querySelectorAll('[data-period]').forEach(item => item.setAttribute('aria-pressed',String(item === button)));
    scheduleChart();
  }; });
  $('#fullscreen').onclick = async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
    catch { toast('浏览器未允许全屏，请使用 F11 进入全屏显示。'); }
  };
  document.addEventListener('fullscreenchange', () => {
    const label = document.fullscreenElement ? '退出全屏' : '进入全屏';
    $('#fullscreen').title = label; $('#fullscreen').setAttribute('aria-label',label);
  });
  $('#definitions').onclick = () => $('#definitions-dialog').showModal();
  $('#close-definitions').onclick = () => $('#definitions-dialog').close();
  $('#definitions-dialog').addEventListener('close', showNextAlert);
  document.addEventListener('close', event => { if (event.target.id === 'workspace-login') showNextAlert(); }, true);
  $('#scroll-toggle').onclick = () => {
    autoScroll = !autoScroll; $('#scroll-toggle').setAttribute('aria-pressed',String(autoScroll));
    $('#scroll-toggle').textContent = autoScroll ? '暂停滚动' : '继续滚动';
  };
  $('#alert-viewport').addEventListener('mouseenter', () => { hovering = true; });
  $('#alert-viewport').addEventListener('mouseleave', () => { hovering = false; });
  let edgePause = 0;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  function scrollAlerts() {
    const viewport = $('#alert-viewport');
    if (!autoScroll || hovering || viewport.contains(document.activeElement) || document.hidden || modalOpen() || reducedMotion.matches || !viewport.clientHeight) return;
    if (viewport.scrollHeight <= viewport.clientHeight + 1) return;
    if (performance.now() < edgePause) return;
    if (viewport.scrollTop >= viewport.scrollHeight - viewport.clientHeight - 1) { viewport.scrollTop = 0; edgePause = performance.now() + 2200; }
    else viewport.scrollTop += 2;
  }
  function pointHint(event) {
    const point = event.target.closest('[data-point]'), tooltip = $('.chart-tooltip');
    if (!tooltip) return;
    tooltip.hidden = !point;
    if (!point) return;
    const item = data.trends[period][Number(point.dataset.point)];
    tooltip.textContent = `${item.label} · ${percent(item.rate)}% · 通过 ${item.passed} / 不通过 ${item.failed}`;
  }
  $('#trend-chart').addEventListener('pointerover', pointHint);
  $('#trend-chart').addEventListener('focusin', pointHint);
  for (const type of ['pointerleave','focusout']) $('#trend-chart').addEventListener(type, () => { const tooltip = $('.chart-tooltip'); if (tooltip) tooltip.hidden = true; });
  const chartObserver = new ResizeObserver(scheduleChart);
  chartObserver.observe($('#trend-chart'));
  function tick() {
    clock();
    if (mode === 'demo' && !document.hidden && performance.now() - modeStarted >= 8000) { modeStarted = performance.now(); updateDemo(); }
  }
  function pauseWork() {
    generation++; clearTimeout(pollTimer); requestController?.abort(); requestController = null;
    clearInterval(lifecycleTimer); clearInterval(scrollTimer); clearTimeout(pendingDialogTimer);
    cancelAnimationFrame(chartFrame); chartFrame = 0;
  }
  function resumeWork() {
    pauseWork();
    if (suspended || document.hidden) return;
    rotationAt = performance.now();
    lifecycleTimer = setInterval(tick, 1000); scrollTimer = setInterval(scrollAlerts, 140);
    tick(); scheduleChart(); showNextAlert();
    if (mode === 'live' && W.session && !blocked) poll(generation);
  }
  document.addEventListener('visibilitychange', resumeWork);
  window.addEventListener('offline', () => {
    if (mode === 'live') { generation++; clearTimeout(pollTimer); requestController?.abort(); requestController = null;
      connected = false; connection('error','网络离线'); staleStations(); notice('网络已断开，连接恢复后自动同步。','error'); }
  });
  window.addEventListener('online', () => { retryCount = 0; resumeWork(); });
  window.addEventListener('pagehide', () => { suspended = true; pauseWork(); chartObserver.disconnect(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { suspended = false; chartObserver.observe($('#trend-chart')); resumeWork(); } });
  window.BoardHistory?.init({getMode:() => mode, getDate:boardDate, toast, onClose:showNextAlert, onChanged:() => {
    queue = []; activeAlert = null; $('#alert-dialog').hidden = true; updateQueueLabel();
    if (mode === 'live' && W.session) resumeWork();
  }});
  window.BoardAdvanced?.init({getMode:() => mode, getDate:boardDate, toast, onClose:showNextAlert, onChanged:() => {
    window.BoardHistory?.reset();
    queue = []; seen = new Set(); initialized = false; activeAlert = null;
    $('#alert-dialog').hidden = true; updateQueueLabel();
    if (mode === 'live' && W.session) resumeWork();
  }});
  document.addEventListener('inspection-data-reset', () => {
    queue = []; seen = new Set(); initialized = false; activeAlert = null;
    $('#alert-dialog').hidden = true; updateQueueLabel(); window.BoardHistory?.reset();
    if (mode === 'live' && W.session) resumeWork();
  });
  clock(); startMode();
  resumeWork();
  W.connect().catch(error => { if (mode === 'live') { connection('error','服务未连接'); notice('无法连接服务：' + error.message + '。可切换演示模式预览。','error'); } });
})();
