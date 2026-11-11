/* Keyboard and keyboard-emulating USB switches control the current task. */
window.InspectionTrigger = (() => {
  'use strict';
  const storageKey = 'aisop-inspection-trigger-v1';
  const defaults = {enabled: true, code: 'F8'};
  const stopCode = 'F9';
  const allowed = code => /^(F([1-9]|1\d|2[0-4])|Key[A-Z]|Digit[0-9]|Numpad[0-9]|Space|Enter|NumpadEnter)$/.test(code) && !['F1', 'F5', stopCode, 'F11', 'F12'].includes(code);
  const label = code => ({Space: '空格', Enter: 'Enter', NumpadEnter: '数字键盘 Enter'}[code] || code.replace('Key', '').replace('Digit', '').replace('Numpad', '数字键盘 '));
  function read() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey));
      if (saved?.code === stopCode && typeof saved.enabled === 'boolean') return {enabled: saved.enabled, code: defaults.code};
      if (saved && allowed(saved.code) && typeof saved.enabled === 'boolean') return {enabled: saved.enabled, code: saved.code};
    } catch {}
    return {...defaults};
  }
  let config = read(), adapter = null, root = null, learning = false, pending = false, listening = false, lastPress = -Infinity, lastStopPress = -Infinity;
  const held = new Set();
  const $ = selector => root?.querySelector(selector);
  const modalOpen = () => Boolean(document.querySelector('dialog[open], [aria-modal="true"]:not([hidden])'));
  const editable = target => target instanceof Element && Boolean(target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"]'));
  function message(text) {
    if ($('#trigger-feedback')) $('#trigger-feedback').textContent = text;
    else if (text) adapter?.notify?.(text);
  }
  function status(action = 'start') {
    if (!config.enabled) return '按键触发已关闭';
    if (learning) return '等待录入按键 · Esc 取消';
    if (document.hidden) return '页面在后台，触发已暂停';
    if (action === 'start' && modalOpen()) return '请先关闭当前弹窗';
    if (pending) return pending === 'stop' ? '正在停止，请稍候' : '正在启动，请稍候';
    return (action === 'stop' ? adapter?.stopBlocked?.() : adapter?.blocked()) || '';
  }
  function refresh() {
    if (!root) return;
    root.dataset.enabled = String(config.enabled);
    $('#trigger-enabled').checked = config.enabled;
    $('#trigger-key').textContent = label(config.code);
    const text = learning ? '等待录入按键 · Esc 取消' : config.enabled ? '已启用 · 返回检测工作台后生效' : '已关闭 · 可继续使用界面开始和停止按钮';
    if ($('#trigger-status').textContent !== text) $('#trigger-status').textContent = text;
    $('#trigger-bind').textContent = learning ? '取消录入' : '设置按键';
  }
  function save() {
    try {localStorage.setItem(storageKey, JSON.stringify(config));}
    catch {message('浏览器无法保存设置，本次页面内仍可使用。');}
    refresh();
  }
  // Future device adapters must supply a fresh press edge, never replay queued inputs.
  // This is a page-local entry point, not a Modbus connection or a server endpoint.
  async function requestStart(source = 'keyboard') {
    if (!adapter) return {accepted: false, reason: '检测页面尚未就绪'};
    const reason = status();
    if (reason) {message(reason); refresh(); return {accepted: false, reason};}
    const now = performance.now();
    if (now - lastPress < 600) return {accepted: false, reason: '重复触发已忽略'};
    lastPress = now; pending = 'start'; refresh();
    try {
      await adapter.start({source});
      return {accepted: true};
    } catch (error) {
      const reason = error.message || '启动失败，请检查后重试';
      message(reason); return {accepted: false, reason};
    } finally {pending = false; refresh();}
  }
  async function requestStop(source = 'keyboard') {
    if (!adapter?.stop) return {accepted: false, reason: '检测页面尚未就绪'};
    const reason = status('stop');
    if (reason) {message(reason); refresh(); return {accepted: false, reason};}
    const now = performance.now();
    if (now - lastStopPress < 600) return {accepted: false, reason: '重复触发已忽略'};
    lastStopPress = now; pending = 'stop'; refresh();
    try {
      await adapter.stop({source});
      return {accepted: true};
    } catch (error) {
      const reason = error.message || '停止失败，请检查后重试';
      message(reason); return {accepted: false, reason};
    } finally {pending = false; refresh();}
  }
  function keydown(event) {
    if ((!root && !adapter) || document.hidden || event.isComposing || event.keyCode === 229) return;
    if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
    const code = event.code || (event.key === ' ' ? 'Space' : event.key);
    if (learning) {
      event.preventDefault(); event.stopImmediatePropagation();
      if (event.repeat || held.has(code)) return;
      held.add(code);
      if (code === 'Escape') {learning = false; message('已取消录入'); refresh(); return;}
      if (code === stopCode) {message('F9 已保留为停止键，请选择其他开始键。'); return;}
      if (!allowed(code)) {message('请选择 F8 等功能键、字母、数字、空格或 Enter；不支持组合键和浏览器常用快捷键。'); return;}
      config.code = code; learning = false; lastPress = performance.now();
      message('已保存，开始键为 ' + label(code) + '。返回检测工作台后使用。'); save(); document.activeElement?.blur(); return;
    }
    const dismissible = code === 'F8' && document.querySelector('dialog[open][data-dismiss-key="F8"]');
    if (dismissible) {
      event.preventDefault(); event.stopImmediatePropagation();
      if (event.repeat || held.has(code)) return;
      held.add(code); dismissible.close(); return;
    }
    const stopping = code === stopCode;
    if (!adapter || !config.enabled || (!stopping && (code !== config.code || modalOpen()))) return;
    // Function keys work while scanning/entering a part number; text keys keep editing semantics.
    if (editable(event.target) && !/^F\d+$/.test(code)) return;
    if (root?.contains(event.target) && !/^F\d+$/.test(code)) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (event.repeat || held.has(code)) return;
    held.add(code);
    if (stopping) void requestStop('keyboard'); else void requestStart('keyboard');
  }
  function attach(options) {
    if (adapter || root) throw new Error('触发控制器已绑定');
    adapter = options;
    listen();
  }
  function attachSettings(host) {
    if (adapter || root) throw new Error('触发控制器已绑定');
    root = document.createElement('section'); root.className = 'inspection-trigger'; root.setAttribute('aria-label', '检测开始与停止按键');
    root.innerHTML = '<div class="trigger-enable-row"><div><h3>按键控制检测</h3><p>使用键盘、USB 按钮或脚踏开关控制当前检测任务。</p></div><label class="trigger-toggle"><input id="trigger-enabled" type="checkbox" role="switch"><span>启用按键触发</span></label></div><div class="trigger-binding"><div><span class="setting-label">当前开始键</span><div class="trigger-key-line"><kbd id="trigger-key"></kbd><span>开始当前检测任务</span></div></div><button id="trigger-bind" type="button">设置按键</button></div><div class="trigger-binding"><div><span class="setting-label">固定停止键</span><div class="trigger-key-line"><kbd id="trigger-stop-key">F9</kbd><span>停止当前检测任务</span></div></div></div><p id="trigger-status" role="status" aria-live="polite"></p><p id="trigger-feedback" role="status" aria-live="polite"></p><p class="trigger-help">点击「设置按键」，再按下键盘按键或踩一次 USB 脚踏开关。推荐使用 F8 开始，F9 固定用于停止；扫码输入时功能键也能触发。Esc 取消录入。</p>';
    host.append(root);
    $('#trigger-enabled').onchange = event => {config.enabled = event.target.checked; learning = false; message(config.enabled ? '已保存，按键触发已启用。' : '已保存，按键触发已关闭。'); save();};
    $('#trigger-bind').onclick = () => {learning = !learning; message(learning ? '请按下键盘按键或踩下 USB 脚踏开关，Esc 取消。' : '已取消录入'); refresh();};
    listen(); refresh();
  }
  function listen() {
    if (listening) return;
    listening = true;
    document.addEventListener('keydown', keydown, true);
    document.addEventListener('keyup', event => held.delete(event.code || (event.key === ' ' ? 'Space' : event.key)), true);
    window.addEventListener('blur', () => {held.clear(); learning = false; refresh();});
    document.addEventListener('visibilitychange', () => {held.clear(); learning = false; refresh();});
    window.addEventListener('storage', event => {if (event.key === storageKey || event.key === null) {config = read(); learning = false; refresh();}});
    const reloadSettings = () => {config = read(); refresh();};
    window.addEventListener('focus', reloadSettings);
    window.addEventListener('pageshow', reloadSettings);
    document.addEventListener('toggle', refresh, true);
    document.addEventListener('close', refresh, true);
    refresh();
  }
  function cancelLearning() {
    if (!learning) return;
    learning = false; message('已取消录入'); refresh();
  }
  return {attach, attachSettings, refresh, requestStart, requestStop, cancelLearning};
})();
