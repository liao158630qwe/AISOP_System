/* Local operator preferences; configuring keys never starts an inspection. */
window.InspectionTrigger.attachSettings(document.querySelector('#trigger-settings'));

for (const button of document.querySelectorAll('.settings-collapse-toggle')) {
  const content = document.getElementById(button.getAttribute('aria-controls'));
  const section = button.closest('.settings-section');
  button.onclick = () => {
    const expanded = button.getAttribute('aria-expanded') !== 'true';
    content.hidden = !expanded;
    section.dataset.collapsed = String(!expanded);
    button.setAttribute('aria-expanded', String(expanded));
    button.querySelector('span').textContent = expanded ? '收起' : '展开';
    button.setAttribute('aria-label', (expanded ? '收起' : '展开') + section.querySelector('h2').textContent);
    if (!expanded && content.contains(document.querySelector('#trigger-settings'))) window.InspectionTrigger.cancelLearning();
  };
}

/* Device setup remains a local draft until the I/O module and wiring are verified. */
(() => {
  'use strict';
  const storageKey = 'aisop-modbus-drafts-v1';
  const form = document.querySelector('#modbus-form'), stationSelect = document.querySelector('#modbus-station');
  const status = document.querySelector('#modbus-save-status'), preview = document.querySelector('#modbus-preview-state');
  const stations = Array.from({length: 6}, (_, i) => 'station-' + (i + 1));
  const points = [
    {id: 'start', label: '开始按键', input: true}, {id: 'stop', label: '停止按键', input: true},
    {id: 'red', label: '红灯'}, {id: 'yellow', label: '黄灯'}, {id: 'green', label: '绿灯'}, {id: 'buzzer', label: '蜂鸣器'},
  ];
  const modes = {off: '全部熄灭', red: '红灯常亮', 'red-flash': '红灯闪烁', yellow: '黄灯常亮', 'yellow-flash': '黄灯闪烁', green: '绿灯常亮', 'green-flash': '绿灯闪烁'};
  const rules = [
    {id: 'idle', label: '待检测 / 已停止', light: 'yellow', sound: false},
    {id: 'running', label: '检测中', light: 'yellow-flash', sound: false},
    {id: 'pass', label: '检测合格', light: 'green', sound: false},
    {id: 'fail', label: '检测不合格', light: 'red-flash', sound: true},
    {id: 'review', label: '待复核 / 检测异常', light: 'yellow-flash', sound: false},
  ];
  let station = stations[0];
  function pointRow(point) {
    const areas = point.input ? '<option value="discrete-input">离散输入</option><option value="coil">线圈</option><option value="input-register">输入寄存器</option><option value="holding-register">保持寄存器</option>' : '<option value="coil">线圈</option><option value="holding-register">保持寄存器</option>';
    return `<div class="modbus-point" data-point="${point.id}"><strong class="modbus-point-title"><i class="modbus-dot ${point.id}" aria-hidden="true"></i>${point.label}</strong>
      <label>点位类型<select name="${point.id}.area"><option value="">待确定</option>${areas}</select></label>
      <label>地址偏移<input name="${point.id}.address" type="number" min="0" max="65535" step="1" placeholder="待确定"></label>
      <label>位号 <small>可选</small><input name="${point.id}.bit" type="number" min="0" max="15" step="1" placeholder="寄存器位号" disabled></label>
      <label>有效电平<select name="${point.id}.active"><option value="">待确定</option><option value="1">1 有效</option><option value="0">0 有效</option></select></label></div>`;
  }
  document.querySelector('#modbus-inputs').innerHTML = points.filter(p => p.input).map(pointRow).join('');
  document.querySelector('#modbus-outputs').innerHTML = points.filter(p => !p.input).map(pointRow).join('');
  document.querySelector('#modbus-rules').innerHTML = rules.map(rule => `<div class="modbus-rule"><span>${rule.label}</span><select name="rule.${rule.id}.light" aria-label="${rule.label}灯光">${Object.entries(modes).map(([value, label]) => `<option value="${value}">${label}</option>`).join('')}</select><label><input name="rule.${rule.id}.sound" type="checkbox">蜂鸣器</label></div>`).join('');
  const fields = [...form.querySelectorAll('[name]')];
  const defaults = Object.fromEntries(fields.map(field => [field.name, field.type === 'checkbox' ? false : '']));
  Object.assign(defaults, {lampBrand: '正泰', lampModel: 'ZTP-NP', lampVoltage: '24V'});
  for (const rule of rules) {defaults[`rule.${rule.id}.light`] = rule.light; defaults[`rule.${rule.id}.sound`] = rule.sound;}
  const field = name => form.elements.namedItem(name);
  function message(text, error = false) {status.textContent = text; status.dataset.error = String(error);}
  function read() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey));
      if (saved?.version === 1 && saved.stations && typeof saved.stations === 'object') return saved;
    } catch {}
    return {version: 1, stations: {}};
  }
  function values() {return Object.fromEntries(fields.map(item => [item.name, item.type === 'checkbox' ? item.checked : item.value.trim()]));}
  function syncControls() {
    for (const transport of ['tcp', 'rtu']) {
      const group = document.querySelector('#modbus-' + transport);
      group.hidden = group.disabled = field('transport').value !== transport;
    }
    for (const point of points) {
      const bit = field(point.id + '.bit');
      bit.disabled = !field(point.id + '.area').value.endsWith('register');
      if (bit.disabled) bit.value = '';
    }
    const linked = field('buzzerControl').value === 'linked';
    for (const input of form.querySelectorAll('[data-point="buzzer"] input, [data-point="buzzer"] select')) input.disabled = linked;
    if (!linked) field('buzzer.bit').disabled = !field('buzzer.area').value.endsWith('register');
  }
  function check() {
    for (const item of fields) item.setCustomValidity('');
    const mapped = [];
    for (const point of points) {
      const area = field(point.id + '.area'), address = field(point.id + '.address'), bit = field(point.id + '.bit');
      if (area.disabled || !area.value || address.value === '') continue;
      const conflict = mapped.find(p => p.area === area.value && p.address === Number(address.value) && (p.bit === '' || bit.value === '' || p.bit === bit.value));
      if (conflict) address.setCustomValidity(`${point.label}与${conflict.label}使用了重叠点位，请核对。`);
      mapped.push({area: area.value, address: Number(address.value), bit: bit.value, label: point.label});
    }
    return form.checkValidity();
  }
  function updatePreview() {
    const rule = rules.find(item => item.id === preview.value), light = field(`rule.${rule.id}.light`).value;
    const sound = field(`rule.${rule.id}.sound`).checked, [color, flash] = light.split('-');
    for (const lamp of document.querySelectorAll('[data-lamp]')) {
      lamp.classList.toggle('is-on', lamp.dataset.lamp === color);
      lamp.classList.toggle('is-flashing', lamp.dataset.lamp === color && flash === 'flash');
    }
    document.querySelector('#modbus-preview-label').textContent = rule.label;
    document.querySelector('#modbus-preview-summary').textContent = modes[light];
    const buzzer = document.querySelector('#modbus-preview-buzzer');
    buzzer.textContent = sound ? '蜂鸣器开启 · 预览' : '蜂鸣器关闭 · 预览'; buzzer.dataset.on = String(sound);
  }
  function save(report = false) {
    syncControls(); updatePreview();
    if (!check()) {message('有参数需要核对，当前修改尚未保存。', true); if (report) form.reportValidity(); return false;}
    const data = read(); data.stations[station] = {values: values(), updatedAt: new Date().toISOString()};
    try {
      localStorage.setItem(storageKey, JSON.stringify(data));
      message(`工位${station.slice(-1)}草稿已保存到本机浏览器 · 未接入设备`); return true;
    } catch {message('浏览器无法保存草稿，请使用导出功能保留当前配置。', true); return false;}
  }
  function load() {
    const saved = read().stations[station];
    for (const item of fields) {
      const value = saved?.values?.[item.name] ?? defaults[item.name];
      item.setCustomValidity('');
      if (item.type === 'checkbox') item.checked = value === true;
      else {
        item.value = typeof value === 'string' ? value : defaults[item.name];
        if (item.tagName === 'SELECT' && ![...item.options].some(option => option.value === item.value)) item.value = defaults[item.name];
        if (!item.validity.valid) item.value = defaults[item.name];
      }
    }
    preview.value = 'idle'; syncControls(); updatePreview();
    message(saved ? `已载入工位${station.slice(-1)}的本机草稿 · 未接入设备` : `工位${station.slice(-1)}尚未配置 · 点位可留空，修改后自动保存草稿`);
  }
  form.addEventListener('input', event => {if (event.target.name) save();});
  form.addEventListener('change', event => {if (event.target.name) save();});
  form.addEventListener('submit', event => {event.preventDefault(); save(true);});
  stationSelect.onchange = () => {
    const next = stationSelect.value;
    if (!save(true)) {stationSelect.value = station; return;}
    station = next; load();
  };
  preview.onchange = updatePreview;
  for (const button of document.querySelectorAll('[data-modbus-preview]')) button.onclick = () => {preview.value = button.dataset.modbusPreview; updatePreview();};
  document.querySelector('#modbus-export').onclick = () => {
    syncControls();
    if (!check()) {form.reportValidity(); message('请先核对标出的参数，再导出草稿。', true); return;}
    const data = read(); data.stations[station] = {...data.stations[station], values: values()};
    const exported = {version: 1, status: 'draft', hardwareVerified: false, exportedAt: new Date().toISOString(),
      stations: Object.fromEntries(stations.map(id => [id, {values: {...defaults, ...data.stations[id]?.values}, updatedAt: data.stations[id]?.updatedAt || null}]))};
    const url = URL.createObjectURL(new Blob([JSON.stringify(exported, null, 2)], {type: 'application/json;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = 'aisop-modbus-drafts.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); message('六工位配置草稿已导出 · 现场连接与接线尚未验证');
  };
  window.addEventListener('storage', event => {if (event.key === storageKey || event.key === null) {
    if (status.dataset.error === 'true') return;
    load();
  }});
  load();
})();
