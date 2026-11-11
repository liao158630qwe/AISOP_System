/* Server GPU telemetry has its own cadence; task renders do not overwrite it. */
(() => {
  'use strict';
  const W = window.Workspace, gpu = document.querySelector('#metric-gpu');
  if (!gpu || !W) return;
  const refreshMs = 1000, maxAgeMs = 3000;
  let timer, staleTimer, controller, generation = 0;
  const valid = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  function render(data, reason='登录后显示服务器 GPU 指标', transitMs=0) {
    clearTimeout(staleTimer);
    const age = (valid(data?.sampleAgeMs) ? data.sampleAgeMs : 0) + transitMs;
    if (data && age >= maxAgeMs) {render(null, 'GPU 指标已过期，等待新的服务器采样'); return;}
    gpu.textContent = data?.status !== 'unavailable' && valid(data?.utilizationPercent) && data.utilizationPercent <= 100 ? data.utilizationPercent.toFixed(0) : '—';
    const sampled = valid(data?.sampledAt) ? new Date(data.sampledAt*1000).toLocaleTimeString() : '—';
    const details = data && data.status !== 'unavailable'
      ? `${data.name} · ${data.device} · 服务器该 GPU 全部进程\n来源：${data.source || 'nvidia-smi'} · 每1秒刷新 · 采样时间 ${sampled}\n利用率为驱动最近采样窗口内执行 GPU 内核的时间比例；不代表显存占比或本检测任务独占比例。`
      : data?.reason || reason;
    gpu.parentElement.title = details;
    if (data && data.status !== 'unavailable') staleTimer = setTimeout(() => render(null, 'GPU 指标已过期，等待新的服务器采样'), maxAgeMs-age);
  }
  async function poll(current) {
    if (document.hidden || current !== generation) return;
    controller = new AbortController();
    const started = performance.now();
    let delay = refreshMs;
    try {
      // The legacy dashboard and station dashboard use separate session stores,
      // but share the authenticated cookie. Do not depend on either store here.
      const data = await W.fetchJSON('/system/gpu', {timeout:5000, signal:controller.signal});
      if (current === generation) render(data, undefined, performance.now()-started);
    } catch (error) {
      if (error.status === 401) delay = 5000;
      if (current === generation) render(null, error.status === 401 ? undefined : 'GPU 指标读取失败，等待重新连接');
    } finally {
      if (current === generation && !document.hidden) timer = setTimeout(() => poll(current), Math.max(0, delay-(performance.now()-started)));
    }
  }
  function refresh() {
    generation++; clearTimeout(timer); controller?.abort();
    render(null, document.hidden ? '页面已隐藏，GPU 指标暂停刷新' : W.session ? '正在读取服务器 GPU 指标' : undefined);
    if (window.AISOP_SERVER) void poll(generation);
  }
  document.addEventListener('workspace-session', refresh);
  document.addEventListener('visibilitychange', refresh);
  refresh();
})();
