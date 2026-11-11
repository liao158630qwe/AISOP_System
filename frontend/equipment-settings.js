/* Shared deployment records. Opening local keyboard/Modbus sections never calls the API. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector), esc = Workspace.escape;
  const form = $('#equipment-form'), fields = $('#equipment-fields'), status = $('#equipment-status');
  const saveStatus = $('#equipment-save-status'), loadButton = $('#equipment-load');
  const endpoint = '/admin/deployment-settings';
  const tabs = [['inventory', '设备清单'], ['network', '服务器与网络'], ['cameras', '六路相机'], ['terminals', '六台终端'], ['storage', '录像存储与大屏'], ['runtime', '运行与接入状态']];
  let snapshot = null, dirty = false, busy = false, selectedTab = 'inventory';
  function message(node, text, error = false) {node.textContent = text; node.dataset.error = String(error);}
  function working(value) {
    busy = value; fields.disabled = value; loadButton.disabled = value;
    $('#equipment-save').disabled = value; $('#equipment-export').disabled = value;
    form.setAttribute('aria-busy', String(value));
  }
  const get = (data, path) => path.split('.').reduce((value, key) => value[key], data);
  function set(data, path, value) {const keys = path.split('.'), last = keys.pop(); get(data, keys.join('.'))[last] = value;}
  function input(path, label, options = {}) {
    const {type = 'text', wide = false, hint = '', placeholder = '待现场填写', readonly = false, min, max, step, pattern, required = false, choices} = options;
    const value = get(snapshot.config, path) ?? '', name = `name="${esc(path)}"`;
    const attrs = `${name} ${readonly ? 'readonly' : ''} ${required ? 'required' : ''} ${min !== undefined ? `min="${min}"` : ''} ${max !== undefined ? `max="${max}"` : ''} ${step !== undefined ? `step="${step}"` : ''} ${pattern ? `pattern="${esc(pattern)}"` : ''}`;
    let control;
    if (choices) control = `<select ${name}>${choices.map(([v, text]) => `<option value="${esc(v)}" ${v === value ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select>`;
    else if (type === 'textarea') control = `<textarea ${attrs} maxlength="${path === 'notes' ? 2000 : 500}" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`;
    else control = `<input ${attrs} type="${type}" ${type === 'number' ? '' : 'maxlength="500"'} value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off">`;
    return `<label class="${wide ? 'equipment-wide' : ''}">${esc(label)}${control}${hint ? `<small>${esc(hint)}</small>` : ''}</label>`;
  }
  const group = (title, content, description = '') => `<section class="equipment-group"><h3>${title}</h3>${description ? `<p>${description}</p>` : ''}<div class="equipment-fields">${content}</div></section>`;
  function activate(id, focus = false) {
    selectedTab = id;
    for (const [key] of tabs) {
      const button = $(`#equipment-tab-${key}`), active = key === id;
      button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
      $(`#equipment-panel-${key}`).hidden = !active;
      if (active && focus) button.focus();
    }
    if (id === 'storage') recorderSummary();
  }
  function render() {
    const c = snapshot.config, r = snapshot.runtime;
    $('.equipment-tabs').innerHTML = tabs.map(([id, label]) => `<button type="button" id="equipment-tab-${id}" role="tab" aria-controls="equipment-panel-${id}" data-equipment-tab="${id}">${label}</button>`).join('');
    const content = {};
    content.inventory = `<div class="equipment-grid">${c.equipment.map((item, i) => group(`${esc(item.name)} <small>${esc(item.quantity ?? '待定')} ${esc(item.unit)}</small>`,
      input(`equipment.${i}.brand`, '品牌') + input(`equipment.${i}.model`, '型号') + input(`equipment.${i}.specification`, '配置 / 规格', {wide:true}) +
      input(`equipment.${i}.quantity`, '数量', {type:'number', min:1, max:1000, step:1, readonly:['terminal','poe'].includes(item.id)}) + input(`equipment.${i}.unit`, '单位', {required:true}) + input(`equipment.${i}.note`, '核对说明', {type:'textarea', wide:true}))).join('')}</div>`;
    content.network = group('集中式 AI 推理服务器', input('server.host', '服务器 IP / 主机名') + input('server.apiUrl', '服务访问地址', {placeholder:'http://服务器地址:端口'}) + input('server.memoryOriginal', '原清单内存规格') + input('server.memoryConfirmedGb', '确认后的总内存（GB）', {type:'number', min:1, max:16384, step:1}), '目标检测、行为识别与工序判定集中运行。原内存规格“16G×64G”待核对，六路负载需实机验收。') +
      group('核心交换机与局域网', input('network.coreHost', '核心交换机管理地址') + input('network.subnet', '网段（CIDR）', {placeholder:'例如 192.168.10.0/24'}) + input('network.gateway', '网关') + input('network.uplink', '上联端口 / 速率说明'), '相机 → PoE 交换机 → 核心交换机 → 推理服务器 / NVR；终端与大屏通过局域网访问服务器。') +
      `<div class="equipment-grid" style="margin-top:16px">${c.switches.map((item,i) => group(`PoE 交换机 ${esc(item.id)}`, input(`switches.${i}.host`, '管理地址（如支持）') + input(`switches.${i}.location`, '安装位置') + input(`switches.${i}.uplink`, '上联核心端口') + input(`switches.${i}.powerBudgetW`, '可用 PoE 总功率（W）', {type:'number', min:0.01, max:10000, step:'any'}))).join('')}</div>`;
    content.cameras = '<p class="equipment-hint">每工位一路相机。填写无账号密码的 RTSP 地址；私有采集地址在采集主机环境变量中配置。保存此表后，需按映射配置采集服务。</p>' +
      `<div class="equipment-grid">${c.cameras.map((item,i) => group(`工位 ${esc(item.stationId.slice(-1))} · 视频通道`,
        input(`cameras.${i}.id`, '相机编号', {pattern:'[A-Za-z0-9_\\-]{1,60}', required:true}) + input(`cameras.${i}.model`, '相机品牌 / 型号') +
        input(`cameras.${i}.host`, '相机 IP / 主机名') + input(`cameras.${i}.nvrChannel`, 'NVR 通道', {type:'number', min:1, max:256, step:1}) +
        input(`cameras.${i}.rtspUrl`, 'RTSP 地址（不含凭据）', {wide:true, placeholder:'rtsp://相机地址:554/视频路径'}) +
        input(`cameras.${i}.sourceEnv`, '私有采集地址环境变量名', {wide:true, pattern:'[A-Z][A-Z0-9_]{0,79}', required:true}) +
        input(`cameras.${i}.poeId`, '连接 PoE 交换机', {choices:[['','待确定'], ...c.switches.map(s=>[s.id,s.id])]}) +
        input(`cameras.${i}.poePort`, 'PoE 端口号', {type:'number', min:1, max:8, step:1}))).join('')}</div>`;
    content.terminals = '<p class="equipment-hint">6 台研显 Windows 触摸一体机，各对应一个工位。此处记录设备分配；实际授权、相机绑定与检测任务在 <a href="/terminals">工位终端管理</a> 中完成。</p>' +
      `<div class="equipment-grid">${c.terminals.map((item,i) => group(`工位 ${esc(item.stationId.slice(-1))} · 显示终端`, input(`terminals.${i}.id`, '终端编号', {pattern:'[A-Za-z0-9_\\-]{1,60}', required:true}) + input(`terminals.${i}.host`, '终端 IP / 主机名') + input(`terminals.${i}.location`, '安装位置', {wide:true}))).join('')}</div>`;
    content.storage = '<section class="equipment-group" id="equipment-current-storage"><h3>当前告警录像存储</h3><dl class="equipment-runtime">' + [
      ['存储位置', 'AI 服务器本地硬盘'], ['录像目录', r.evidenceStorage?.location || '待读取服务器存储位置'],
      ['当前保存期限', r.evidenceStorage ? `成功归档后至少 ${r.evidenceStorage.retentionDays} 天` : '待读取服务器保留策略'],
      ['NVR 告警联动', r.nvrIntegrated ? '已接入' : '待接入']
    ].map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('') + '</dl><p>现有告警录像可在首页“告警录像”中查看。下方保存后续 NVR 接入参数；实际接入完成后再切换录像存储。</p></section>' +
      group('独立录像存储 · NVR',
        input('nvr.purpose', '存储用途', {wide:true, required:true}) + input('nvr.host', 'NVR IP / 主机名') + input('nvr.location', '设备安装位置') +
        input('nvr.diskCount', '已安装硬盘数量（块）', {type:'number', min:1, max:128, step:1}) + input('nvr.diskCapacityTb', '单块硬盘容量（TB）', {type:'number', min:0.01, max:1000, step:'any', hint:'硬盘容量一致时填写；混装请在现场备注中记录。'}) +
        input('nvr.usableStorageTb', '实际可用录像容量（TB）', {type:'number', min:0.01, max:10000, step:'any', hint:'填写设备实际可用容量，容量估算以此为准。'}) + input('nvr.bitrateMbps', '单路平均码率（Mbps）', {type:'number', min:0.01, max:1000, step:'any'}) +
        input('nvr.retentionDays', '连续录像保存目标（天）', {type:'number', min:30, max:3650, step:1, required:true}) + input('nvr.evidenceRetentionDays', 'AI 告警取证录像保存目标（天）', {type:'number', min:30, max:3650, step:1, required:true}),
        '<span id="equipment-nvr-summary"></span> 多硬盘配置按设备清单记录；硬盘与地址待现场填写。六路相机与 NVR 通道在“六路相机”中配置，30 天存证需按实际容量、码率和录像计划验收。') +
      '<p class="equipment-storage" id="equipment-storage-estimate" role="status"></p>' +
      group('集中大屏', input('display.host', '大屏 IP（如支持）') + input('display.playerHost', '播放主机 IP / 主机名') + input('display.page', '集中看板路径', {readonly:true, wide:true}), '德天 100 英寸 4K 工业大屏。使用内置或外接主机打开服务器的 <a href="/board">/board 集中看板</a>，展示六工位状态与统计。') +
      group('现场备注', input('notes', '补充说明', {type:'textarea', wide:true, placeholder:'例如布线长度、设备安装位置、待确认项；请勿填写账号密码'}));
    content.runtime = '<section class="equipment-group"><h3>当前服务与设备接入</h3><p>下列状态反映软件接入范围。设备清单保存成功后仍需进行现场连接与性能验证。</p><dl class="equipment-runtime">' + [
      ['连续无目标停止', r.noTargetTimeoutSeconds > 0 ? `${r.noTargetTimeoutSeconds} 秒` : '未启用'], ['单轮检测上限', r.maxDurationSeconds > 0 ? `${r.maxDurationSeconds} 秒` : '未启用'],
      ['六工位检测服务', '支持独立检测与终端授权'], ['六路并发性能', '待实机验收'], ['工位间顺序放行', '尚未接入跨工位约束'], ['NVR 告警联动', '尚未接入'], ['三色灯硬件控制', '尚未接入 I/O 模块'], ['大屏看板', '/board']
    ].map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('') + '</dl><p>检测时限由服务器运行配置统一控制。本页保存设备部署记录，不修改正在运行的检测任务。</p></section>';
    $('#equipment-panels').innerHTML = tabs.map(([id]) => `<div id="equipment-panel-${id}" role="tabpanel" aria-labelledby="equipment-tab-${id}" tabindex="0">${content[id]}</div>`).join('');
    for (const button of $('.equipment-tabs').querySelectorAll('button')) {
      button.onclick = () => activate(button.dataset.equipmentTab);
      button.onkeydown = event => {
        const index = tabs.findIndex(([id]) => id === selectedTab);
        const next = event.key === 'ArrowRight' ? (index+1)%tabs.length : event.key === 'ArrowLeft' ? (index+tabs.length-1)%tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : -1;
        if (next >= 0) {event.preventDefault(); activate(tabs[next][0], true);}
      };
    }
    form.hidden = false; activate(selectedTab); estimate();
  }
  function values() {
    const config = structuredClone(snapshot.config);
    for (const field of fields.querySelectorAll('[name]')) {
      const value = field.value.trim();
      if (field.name === 'notes') config.notes = value;
      else set(config, field.name, field.type === 'number' ? (value === '' ? null : Number(value)) : value);
    }
    return config;
  }
  const field = name => form.elements.namedItem(name);
  function recorderSummary() {
    const target = $('#equipment-nvr-summary'); if (!target) return;
    const index = snapshot.config.equipment.findIndex(item => item.id === 'nvr');
    const value = key => field(`equipment.${index}.${key}`).value.trim();
    target.textContent = `${value('brand')} ${value('model')} · ${value('quantity') || '待定'} ${value('unit')}，独立录像存储设备。`;
  }
  function check() {
    for (const item of fields.querySelectorAll('[name]')) item.setCustomValidity('');
    for (const prefix of ['cameras','terminals']) {
      for (const key of prefix === 'cameras' ? ['id','sourceEnv','nvrChannel'] : ['id']) {
        const seen = new Set();
        for (let i=0;i<6;i++) {const item = field(`${prefix}.${i}.${key}`), value = item.value.trim(); if (value && seen.has(value)) item.setCustomValidity('编号、采集变量或 NVR 通道不能重复'); seen.add(value);}
      }
    }
    const ports = new Set();
    for (let i=0;i<6;i++) {
      const port = field(`cameras.${i}.poePort`), device = field(`cameras.${i}.poeId`).value, key = `${device}:${port.value}`;
      if (port.value && !device) port.setCustomValidity('请先选择此端口所属的 PoE 交换机');
      if (device && port.value && ports.has(key)) port.setCustomValidity('同一交换机端口不能连接两路相机');
      ports.add(key);
    }
    for (const item of fields.querySelectorAll('[name$=".rtspUrl"], [name="server.apiUrl"]')) {
      if (!item.value.trim()) continue;
      try {
        const url = new URL(item.value.trim()), protocols = item.name.endsWith('.rtspUrl') ? ['rtsp:', 'rtsps:'] : ['http:', 'https:'];
        if (!protocols.includes(url.protocol) || !url.hostname || url.hash) throw Error('请填写有效的视频或服务地址');
        if (url.username || url.password || [...url.searchParams.keys()].some(key => /^(password|passwd|token|key|secret|authorization|username|user)$/i.test(key))) throw Error('此处不保存账号密码或令牌，请在采集主机配置私有环境变量');
      } catch(error) {item.setCustomValidity(error.message);}
    }
    const invalid = [...fields.querySelectorAll('[name]')].find(item => !item.validity.valid);
    if (invalid) {activate(invalid.closest('[role=tabpanel]').id.replace('equipment-panel-', '')); invalid.reportValidity(); message(saveStatus, '请核对标出的字段，当前修改尚未保存。', true); return false;}
    return true;
  }
  function estimate() {
    const target = $('#equipment-storage-estimate'); if (!target) return;
    const bitrate = Number(field('nvr.bitrateMbps').value), days = Number(field('nvr.retentionDays').value), capacity = Number(field('nvr.usableStorageTb').value);
    if (!(bitrate > 0 && days >= 30 && capacity > 0)) {target.textContent = '填写单路平均码率、保存天数与实际可用容量后，估算六路连续录像容量。'; return;}
    const needed = 6 * bitrate * 86400 * days / 8 / 1e6, availableDays = capacity * 1e6 * 8 / (6 * bitrate * 86400);
    target.textContent = `六路全天连续录像 ${days} 天约需 ${needed.toFixed(2)} TB；当前 ${capacity} TB 约可保存 ${availableDays.toFixed(1)} 天。${capacity < needed ? '当前容量低于目标估算。' : ''}按十进制 TB 估算，未计另外归档的告警副本、音频、索引与容量余量，实际留存以现场验证为准。`;
  }
  async function load(interactive = false) {
    if (busy) return;
    if (dirty && !window.confirm('当前修改尚未保存。重新读取会丢弃这些修改，是否继续？可先取消并导出当前配置。')) return;
    working(true); message(status, '正在读取服务器配置…');
    try {
      let session = await Workspace.connect();
      if (!session && interactive) session = await Workspace.login();
      if (!session) {message(status, '请登录管理账户，读取与保存六工位设备配置。'); return;}
      snapshot = await Workspace.request(endpoint); dirty = false; render();
      message(status, snapshot.revision ? `已读取服务器配置 · 版本 ${snapshot.revision} · ${new Date(snapshot.updatedAt*1000).toLocaleString()}` : '已载入六工位预填清单，首次保存后存入服务器。');
      message(saveStatus, snapshot.revision ? '当前配置已保存。' : '设备信息已预填，待保存到服务器。'); loadButton.textContent = '重新读取服务器';
    } catch(error) {message(status, error.message, true);} finally {working(false);}
  }
  form.addEventListener('input', event => {
    if (!event.target.name) return;
    event.target.setCustomValidity(''); dirty = true; message(saveStatus, '有未保存的修改，请保存到服务器。'); estimate();
  });
  form.addEventListener('change', event => {if (event.target.name) {dirty = true; message(saveStatus, '有未保存的修改，请保存到服务器。');}});
  form.onsubmit = async event => {
    event.preventDefault(); if (!snapshot || busy || !check()) return;
    working(true); message(saveStatus, '正在保存…');
    try {
      snapshot = await Workspace.json(endpoint, 'PUT', {revision:snapshot.revision, config:values()}); dirty = false;
      message(saveStatus, '已保存到服务器 · 设备接入待现场验证');
      message(status, `服务器配置 · 版本 ${snapshot.revision} · ${new Date(snapshot.updatedAt*1000).toLocaleString()}`);
    } catch(error) {message(saveStatus, error.status === 409 ? '其他页面已更新配置。当前修改已保留，请先导出，再重新读取服务器配置。' : `${error.message}。当前修改已保留。`, true);} finally {working(false);}
  };
  $('#equipment-export').onclick = () => {
    if (!snapshot || busy || !check()) return;
    const data = {version:1, revision:snapshot.revision, status:'commissioning-plan', hardwareVerified:false, unsavedChanges:dirty, exportedAt:new Date().toISOString(), config:values()};
    const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)], {type:'application/json;charset=utf-8'}));
    const a = document.createElement('a'); a.href = url; a.download = 'aisop-six-station-deployment.json'; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    message(saveStatus, dirty ? '已导出当前修改；修改尚未保存到服务器。' : '已导出六工位设备配置。');
  };
  loadButton.onclick = () => load(true);
  $('[aria-controls="equipment-section-content"]').addEventListener('click', () => {if (!$('#equipment-section-content').hidden && !snapshot) load();});
  window.addEventListener('beforeunload', event => {if (dirty) {event.preventDefault(); event.returnValue = '';}});
})();
