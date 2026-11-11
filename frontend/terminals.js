(() => {
  'use strict';
  const $ = s => document.querySelector(s), W = window.Workspace;
  const bindings = new Map(), drafts = new Map();
  const labels = {manual:'人工检查',pose:'人体姿态观察',screw:'螺丝专项',sticker:'贴条专项',scene:'自定义场景'};
  let revision = 0, timer, generation = 0, refreshVersion = 0, scenes = [], busy = false;
  const say = text => { $('#message').textContent = text; };
  const isAdmin = () => !!W.session && W.session.role !== 'terminal';
  for (let i = 1; i <= 6; i++) $('#station').add(new Option('工位 ' + i, 'station-' + i));
  function hideKey() { $('#issued-key').textContent = ''; $('#issued').hidden = true; }
  function payload() {
    return {workstationId:$('#workstation').value.trim(), cameraId:$('#camera').value.trim(),
      source:$('#source').value, analysisType:$('#analysis').value,
      sceneId:$('#analysis').value === 'scene' ? $('#scene').value || null : null,
      sampleFps:Number($('#sample-fps').value), maxSampleGapSeconds:Number($('#sample-gap').value),
      previewUrl:$('#preview-url').value.trim(), enabled:$('#enabled').checked, revision};
  }
  function controls() {
    for (const element of $('#binding-form').elements) element.disabled = busy;
    $('#account').disabled = busy;
    $('#scene').disabled = busy || $('#analysis').value !== 'scene';
    $('#scene').required = $('#analysis').value === 'scene';
    $('#rotate-key').disabled = busy || !bindings.get($('#station').value)?.enabled || drafts.has($('#station').value);
    $('#draft-note').textContent = drafts.has($('#station').value)
      ? '有未保存修改；刷新状态和切换工位会保留草稿。'
      : `当前配置版本：${revision || '尚未登记'}`;
  }
  function sceneOptions(selected) {
    const select = $('#scene');
    select.replaceChildren(new Option('请选择已发布且可用的场景', ''));
    for (const scene of scenes) {
      const option = new Option(scene.name + (scene.ready ? '' : ' · 暂不可用'), scene.id);
      option.disabled = !scene.ready;
      select.add(option);
    }
    if (selected && !scenes.some(scene => scene.id === selected)) {
      const option = new Option('原场景已下架或不可用，请重新选择', selected);
      option.disabled = true; select.add(option);
    }
    select.value = selected || '';
  }
  function select() {
    const id = $('#station').value, number = id.split('-')[1], b = drafts.get(id) || bindings.get(id);
    revision = b?.revision || 0;
    $('#workstation').value = b?.workstationId || 'pc-0' + number;
    $('#camera').value = b?.cameraId || 'cam-0' + number;
    $('#source').value = b?.source || 'rtsp'; $('#analysis').value = b?.analysisType || 'manual';
    sceneOptions(b?.sceneId);
    $('#sample-fps').value = b?.sampleFps || 4; $('#sample-gap').value = b?.maxSampleGapSeconds || 1;
    $('#preview-url').value = b?.previewUrl || ''; $('#enabled').checked = b?.enabled ?? true;
    controls();
  }
  function cards(stations) {
    $('#stations').innerHTML = stations.map(id => {
      const b = bindings.get(id);
      return `<article class="task-card" data-station="${W.escape(id)}"><h2>工位 ${id.split('-')[1]}</h2><p>${b ? W.escape(b.workstationId + ' · ' + b.cameraId + ' · ' + labels[b.analysisType]) : '尚未登记设备'}</p><p class="terminal-admin-status">等待状态</p><button type="button" data-edit="${id}">配置本工位</button></article>`;
    }).join('');
  }
  async function statuses(id) {
    if (!isAdmin() || id !== generation) return;
    try {
      const data = await W.request('/admin/station-metrics', {timeout:3000});
      if (id !== generation) return;
      for (const s of data.stations) {
        const box = document.querySelector('[data-station="' + s.stationId + '"] .terminal-admin-status');
        if (!box) continue;
        const parts = [s.reason || (s.state === 'LIVE' ? '检测在线' : '采集离线')];
        if (s.targetFps) parts.push(`有效 ${s.effectiveFps} / 目标 ${s.targetFps} fps`);
        if (s.samplingHealth === 'BELOW_TARGET') parts.push('帧率未达目标');
        if (s.gapCount) parts.push(`${s.gapCount} 处采样缺口待复核`);
        if (s.resultAgeMs != null) parts.push('结果年龄 ' + Math.round(s.resultAgeMs) + ' ms');
        box.textContent = parts.join(' · ');
      }
    } catch (error) {
      if (id !== generation) return;
      document.querySelectorAll('.terminal-admin-status').forEach(el => { el.textContent = '状态获取失败：' + error.message; });
    }
    if (id === generation && isAdmin()) timer = setTimeout(() => statuses(id), 2000);
  }
  async function refresh({quiet = false} = {}) {
    if (!isAdmin()) return;
    const id = generation, version = ++refreshVersion;
    try {
      const results = await Promise.allSettled([W.request('/admin/terminals'), W.request('/admin/scenes')]);
      if (id !== generation || version !== refreshVersion) return;
      if (results[0].status !== 'fulfilled') throw results[0].reason;
      const data = results[0].value;
      bindings.clear(); data.items.forEach(b => bindings.set(b.stationId, b));
      if (results[1].status === 'fulfilled') scenes = results[1].value.items.filter(s => s.status === 'published');
      cards(data.stations); select();
      if (!quiet) say(results[1].status === 'rejected' ? '绑定已刷新；场景读取失败：' + results[1].reason.message
        : drafts.size ? '绑定信息已刷新，未保存的修改已保留；版本冲突时请重新载入配置' : '绑定信息已刷新');
    } catch (error) { if (id === generation) say(error.message); }
  }
  function sessionChanged() {
    generation++; refreshVersion++; clearTimeout(timer); hideKey();
    bindings.clear(); drafts.clear(); scenes = [];
    $('#stations').replaceChildren(); $('#binding-form').hidden = !isAdmin();
    $('#account').textContent = W.session ? W.session.role === 'terminal' ? '退出工位会话' : '退出管理员' : '管理员登录';
    if (isAdmin()) { refresh(); statuses(generation); }
    else say(W.session ? '当前为工位会话，请退出后使用管理员账号登录' : '请先以管理员账号登录');
  }
  document.addEventListener('workspace-session', sessionChanged);
  $('#station').onchange = () => { hideKey(); select(); };
  $('#binding-form').addEventListener('input', event => {
    if (event.target.id === 'station') return;
    drafts.set($('#station').value, payload()); controls();
  });
  $('#analysis').onchange = () => { drafts.set($('#station').value, payload()); controls(); };
  $('#refresh').onclick = () => refresh();
  $('#reload-binding').onclick = async () => { drafts.delete($('#station').value); hideKey(); await refresh(); };
  $('#stations').onclick = event => {
    const id = event.target.dataset.edit;
    if (id && !busy) { $('#station').value = id; hideKey(); select(); $('#binding-form').scrollIntoView({behavior:'smooth'}); }
  };
  $('#account').onclick = async () => {
    try { if (W.session) await W.logout(); else await W.login(); } catch (error) { say(error.message); }
  };
  async function save(rotate) {
    if (!isAdmin() || busy) return;
    const id = generation, station = $('#station').value, body = payload();
    busy = true; hideKey(); controls();
    try {
      const data = rotate
        ? await W.json('/admin/terminals/' + station + '/credential', 'POST', {revision})
        : await W.json('/admin/terminals/' + station, 'PUT', body);
      if (id !== generation) return;
      drafts.delete(station); bindings.set(station, data.binding); select();
      if (data.credential) {
        $('#issued-title').textContent = '工位 ' + station.split('-')[1] + ' · 新设备凭据';
        $('#issued-key').textContent = data.credential; $('#issued').hidden = false;
      }
      await refresh({quiet:true});
      if (id === generation) say(data.message);
    } catch (error) { if (id === generation) say(error.message); }
    finally { busy = false; controls(); }
  }
  $('#binding-form').onsubmit = event => { event.preventDefault(); save(false); };
  $('#rotate-key').onclick = () => save(true);
  $('#hide-key').onclick = hideKey;
  $('#copy-key').onclick = async () => {
    try { await navigator.clipboard.writeText($('#issued-key').textContent); say('设备凭据已复制'); }
    catch { say('浏览器未允许复制，请选中凭据手动复制'); }
  };
  W.connect().catch(error => say(error.message));
})();
