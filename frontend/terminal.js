(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const capture = document.createElement('canvas');
  const say = text => { $('#message').textContent = text; };
  let csrf = '', binding = null, stream = null, media = null, pendingStart = null;
  let running = false, starting = false, stopping = false, localBlocked = false;
  let seq = 0, started = 0, generation = 0, sessionGeneration = 0;
  let timer, limitTimer, pollTimer, frameAbort, pending, lastPollAt = 0;
  let resultEpoch = -1, resultStream = null, resultSeq = -1, minResultSeq = 0;
  let previewUrl = null, previewKey = null;
  let resultDeadline = 0;
  let archivedResult = null, completedFrames = 0;
  let historyVersion = 0;
  let transportLost = false, transportGeneration = 0;
  let recoveryBarrier = null;
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '—';

  async function refreshHistory() {
    if (!binding) return;
    const session = sessionGeneration, version = ++historyVersion;
    try {
      const data = await api('/stations/' + binding.stationId + '/inspections?limit=20');
      if (session !== sessionGeneration || version !== historyVersion) return;
      const list = $('#inspection-history'); list.replaceChildren();
      if (!data.items.length) { list.textContent = '尚无检测记录'; return; }
      for (const item of data.items) {
        const box = document.createElement('details'), title = document.createElement('summary');
        const label = {RUNNING:'检测中', FINISHED:'已归档', INTERRUPTED:'中断 · 待复核'}[item.status] || item.status;
        title.textContent = `${date(item.startedAt)} · 检测轮次 ${item.context.streamEpoch} · ${label}`;
        const text = document.createElement('p');
        text.textContent = `${item.result?.compliance?.label || ''} ${item.result?.compliance?.reason || ''} ${item.reason}`;
        const identity = document.createElement('p'); identity.className = 'code';
        identity.textContent = `${item.context.cameraId} · 绑定版本 ${item.context.revision} · 流代次 ${item.context.streamEpoch} · ${item.streamId} · 结束 ${date(item.finishedAt)}`;
        box.append(title, text, identity); list.append(box);
      }
    } catch (error) {
      if (session === sessionGeneration && version === historyVersion) $('#inspection-history').textContent = '记录读取失败：' + error.message;
    }
  }
  $('#refresh-history').onclick = refreshHistory;

  function clearPreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = previewKey = null;
    $('#sample-preview').hidden = $('#sample-caption').hidden = true;
    $('#sample-preview').removeAttribute('src');
    if (binding?.source === 'rtsp' && !binding.previewUrl) $('#network-note').hidden = false;
  }

  async function samplePreview(result, id) {
    if (!result || binding?.source !== 'rtsp' || binding.previewUrl || document.hidden) return;
    const key = result.streamId + ':' + result.frameSeq;
    if (key === previewKey) return;
    const controller = new AbortController(), deadline = setTimeout(() => controller.abort(), 2000);
    try {
      const query = new URLSearchParams({stream_id:result.streamId, frame_seq:String(result.frameSeq)});
      const response = await fetch('/api/v1/stations/' + binding.stationId + '/preview?' + query,
        {credentials:'same-origin', signal:controller.signal});
      if (!response.ok) { if (id === sessionGeneration) clearPreview(); return; }
      if (response.headers.get('X-Stream-Id') !== result.streamId ||
          Number(response.headers.get('X-Stream-Epoch')) !== result.streamEpoch ||
          Number(response.headers.get('X-Frame-Seq')) !== result.frameSeq) return;
      const blob = await response.blob();
      if (id !== sessionGeneration || localBlocked || document.hidden || performance.now() >= resultDeadline) return;
      clearPreview();
      previewUrl = URL.createObjectURL(blob); previewKey = key;
      $('#sample-preview').src = previewUrl;
      $('#sample-preview').hidden = $('#sample-caption').hidden = false;
      $('#network-note').hidden = true;
    } catch { if (id === sessionGeneration) clearPreview(); }
    finally { clearTimeout(deadline); }
  }

  async function api(path, init = {}, timeout = 5000) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    init.signal?.addEventListener('abort', abort, {once: true});
    if (init.signal?.aborted) controller.abort();
    const deadline = setTimeout(abort, timeout);
    try {
      const response = await fetch('/api/v1' + path, {
        ...init, signal: controller.signal, credentials: 'same-origin',
        headers: {...init.headers, ...(csrf ? {'X-CSRF-Token': csrf} : {})}
      });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(data.message || data.detail || '请求失败');
        error.status = response.status;
        error.retryMs = Math.min(5000, Math.max(250, Number(response.headers.get('Retry-After') || 0) * 1000));
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时或已停止，请检查连接');
      throw error;
    } finally {
      clearTimeout(deadline);
      init.signal?.removeEventListener('abort', abort);
    }
  }
  const json = (path, method, body) => api(path, {
    method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  }, 15000);

  function clearResult(message = '等待有效采样') {
    resultDeadline = 0;
    clearPreview();
    $('#decision').textContent = message;
    $('#reason').textContent = '';
    $('#identity').textContent = '';
    $('.terminal-result').dataset.status = 'REVIEW';
    $('#latency').textContent = '—';
  }
  function controls() {
    const occupied = starting || running || !!stream || !!pendingStart;
    $('#start').disabled = occupied || stopping;
    $('#stop').disabled = !occupied || stopping || starting;
    $('#device').disabled = occupied || stopping;
    $('#authorize').disabled = occupied || stopping;
    $('#finish').disabled = !stream || !completedFrames || !!archivedResult || stopping;
    $('#stop-reason').disabled = stopping || starting;
    $('#logout').disabled = stopping || starting;
  }
  function matches(identity, expected) {
    return ['stationId', 'cameraId', 'workstationId', 'revision', 'streamId', 'streamEpoch']
      .every(key => identity[key] === expected[key]);
  }
  function renderStatus(data, requestedAt) {
    if (!binding || data.stationId !== binding.stationId) return;
    if (transportLost) { clearResult('中央推理连接中断 · 无法检测'); return; }
    const warnings = [data.reason || '等待采样状态'];
    if (data.samplingHealth === 'BELOW_TARGET') warnings.push(`近10秒有效帧率低于目标 ${data.targetFps} fps`);
    if (data.gapCount) warnings.push(`本轮有 ${data.gapCount} 处采样缺口，需要复核`);
    $('#sampling-note').textContent = warnings.join('；');
    const age = data.resultAgeMs == null ? null : data.resultAgeMs + performance.now() - requestedAt;
    const fresh = data.state === 'LIVE' && age !== null && age <= 1000;
    $('#online').textContent = fresh ? '检测在线' : data.state === 'OFFLINE' ? '采集离线' : '结果已过期';
    $('#online').className = 'badge ' + (fresh ? '' : 'review');
    $('#fps').textContent = Number(data.effectiveFps || 0).toFixed(1) + ' fps';
    $('#age').textContent = age == null ? '—' : Math.round(age) + ' ms';
    if (document.hidden || localBlocked) {
      $('#online').textContent = document.hidden ? '页面已暂停' : '采集已停止';
      $('#online').className = 'badge review';
      $('#fps').textContent = '—';
      $('#age').textContent = '—';
      clearResult(document.hidden ? '页面在后台 · 等待恢复采样' : '采集已停止 · 待新采样');
      return;
    }
    const result = data.result;
    if (!fresh || !result) {
      clearResult(data.state === 'STALE' ? '结果已过期 · 待新采样' : '等待有效采样');
      return;
    }
    if (recoveryBarrier?.streamId === result.streamId && result.frameSeq <= recoveryBarrier.frameSeq) {
      clearResult('连接已恢复 · 等待新采样'); return;
    }
    if (result.stationId !== binding.stationId || result.cameraId !== binding.cameraId ||
        result.workstationId !== binding.workstationId || result.revision !== binding.revision ||
        (stream && (!matches(result, stream) || result.frameSeq < minResultSeq))) {
      clearResult('结果身份不匹配 · 待新采样');
      return;
    }
    if (result.streamEpoch < resultEpoch ||
        (result.streamEpoch === resultEpoch && resultStream && result.streamId !== resultStream)) return;
    if (result.streamEpoch > resultEpoch) {
      resultEpoch = result.streamEpoch;
      resultStream = result.streamId;
      resultSeq = -1;
    }
    if (result.frameSeq < resultSeq) return;
    resultSeq = result.frameSeq;
    resultDeadline = performance.now() + Math.max(0, 1000 - age);
    $('#latency').textContent = Math.round(result.serverProcessingMs) + ' ms';
    $('#decision').textContent = result.compliance?.label || `人体 ${result.people?.length || 0} 人 · 观察中`;
    $('#reason').textContent = result.compliance?.reason || '人体姿态结果不代表工艺完成';
    $('#identity').textContent = `${result.cameraId} · 检测轮次 ${result.streamEpoch} · 帧 ${result.frameSeq}`;
    $('.terminal-result').dataset.status = result.compliance?.status || 'REVIEW';
    return result;
  }
  function localStop(abort = true) {
    running = false;
    generation++;
    localBlocked = true;
    clearTimeout(timer);
    clearInterval(limitTimer);
    if (abort) frameAbort?.abort();
    media?.getTracks().forEach(track => track.stop());
    media = null;
    $('#preview').srcObject = null;
    if (binding?.source === 'usb') {
      $('.terminal-stage').classList.add('idle');
      $('#usb-note').hidden = false;
    }
    $('#online').textContent = '采集已停止';
    $('#online').className = 'badge review';
    $('#fps').textContent = $('#age').textContent = '—';
    clearResult('采集已停止 · 待新采样');
  }
  function disconnected(message) {
    localStop();
    sessionGeneration++;
    clearTimeout(pollTimer);
    binding = stream = pendingStart = null;
    archivedResult = null; completedFrames = 0;
    $('#final-record').hidden = true;
    $('#inspection-history').replaceChildren();
    csrf = '';
    $('#login-panel').hidden = false;
    $('#terminal-panel').hidden = true;
    $('#logout').hidden = true;
    controls();
    say(message);
  }
  async function poll(id) {
    if (!binding || id !== sessionGeneration) return;
    const requestedAt = performance.now(), transport = transportGeneration;
    try {
      const data = await api('/stations/' + binding.stationId + '/live-status', {}, 2000);
      if (id !== sessionGeneration) return;
      if (transport !== transportGeneration) { pollTimer = setTimeout(() => poll(id), 500); return; }
      lastPollAt = requestedAt;
      if (transportLost) {
        if (stream && (!matches(data, stream) || !['LIVE','STALE'].includes(data.state) || data.reasonCode === 'FINALIZING')) {
          localStop(); controls(); say('原租约或检测轮次已变化，请停止本轮，再开始新一轮');
        }
        transportLost = false; minResultSeq = seq;
        recoveryBarrier = {streamId:data.result?.streamId || resultStream, frameSeq:data.result?.frameSeq ?? resultSeq};
        clearResult('连接已恢复 · 等待新采样');
      } else await samplePreview(renderStatus(data, requestedAt), id);
    } catch (error) {
      if (id !== sessionGeneration) return;
      loseTransport();
      $('#online').textContent = '连接中断';
      say(error.message);
      if (error.status === 401 || error.status === 403) {
        disconnected(error.message);
        return;
      }
    }
    if (id === sessionGeneration) pollTimer = setTimeout(() => poll(id), 500);
  }
  function connected(context) {
    csrf = context.csrf;
    binding = context.binding;
    stream = pendingStart = null;
    archivedResult = null; completedFrames = 0;
    $('#final-record').hidden = true;
    localBlocked = false;
    resultEpoch = -1; resultStream = null; resultSeq = -1;
    $('#login-panel').hidden = true;
    $('#terminal-panel').hidden = false;
    $('#logout').hidden = false;
    $('#station-name').textContent = '工位 ' + binding.stationId.split('-')[1];
    $('#binding-name').textContent = `${binding.workstationId} · ${binding.cameraId} · 绑定版本 ${binding.revision}`;
    const labels = {manual:'人工检查', pose:'人体姿态', screw:'螺丝专项验证', sticker:'贴条专项验证', scene:'自定义场景'};
    $('#task-note').textContent = `任务：${labels[binding.analysisType]} · 目标 ${binding.sampleFps} fps · 独立检测，结果直接同步大屏。`;
    if (binding.analysisType === 'manual') $('#task-note').textContent = '本工位尚未启用AI检测，请由管理员配置检测任务。';
    $('.terminal-stage').hidden = $('.stats').hidden = $('.terminal-result').hidden = binding.analysisType === 'manual';
    $('#usb-controls').hidden = binding.source !== 'usb' || binding.analysisType === 'manual';
    $('.terminal-stage').classList.toggle('idle', binding.source === 'usb');
    $('#usb-note').hidden = binding.source !== 'usb';
    $('#preview').hidden = binding.source !== 'usb';
    $('#network-note').hidden = binding.source === 'usb';
    $('#network-preview').hidden = !binding.previewUrl;
    if (binding.previewUrl) {
      $('#network-preview').src = binding.previewUrl;
      $('#network-note').hidden = true;
    } else $('#network-preview').removeAttribute('src');
    clearTimeout(pollTimer);
    lastPollAt = performance.now();
    clearResult(); controls();
    poll(++sessionGeneration);
    refreshHistory();
    say('已连接固定工位');
  }
  $('#terminal-login').onsubmit = async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button');
    button.disabled = true;
    try {
      const context = await json('/terminal/login', 'POST', {credential: $('#credential').value});
      $('#credential').value = '';
      connected(context);
    } catch (error) { say(error.message); }
    finally { button.disabled = false; }
  };
  async function enumerate() {
    const devices = await navigator.mediaDevices.enumerateDevices(), select = $('#device');
    select.replaceChildren(new Option('请选择相机', ''));
    for (const device of devices.filter(d => d.kind === 'videoinput')) {
      select.add(new Option(device.label || '未命名摄像头', device.deviceId));
    }
    let saved = '';
    try { saved = localStorage.getItem('aisop-terminal-camera:' + binding.cameraId); } catch {}
    if ([...select.options].some(option => option.value && option.value === saved)) select.value = saved;
    else if (select.options.length === 2) select.selectedIndex = 1;
  }
  $('#authorize').onclick = async () => {
    try {
      const probe = await navigator.mediaDevices.getUserMedia({video:true, audio:false});
      probe.getTracks().forEach(track => track.stop());
      await enumerate();
      say('核对并选择与本工位相连的相机');
    } catch (error) { say(error.message); }
  };
  async function stop({reason=''}={}) {
    if (stopping) return false;
    stopping = true;
    localStop(); controls();
    try {
      if (pending) await pending.catch(() => {});
      // Retry an uncertain start with the same idempotency key before releasing it.
      if (!stream && pendingStart) stream = await json('/station-streams', 'POST', pendingStart);
      if (stream) await json('/station-streams/' + stream.streamId, 'DELETE', {reason:reason || $('#stop-reason').value});
      stream = pendingStart = null;
      say(reason || (archivedResult ? '结果已归档，采集流已释放' : '采集流已释放；未归档的检测已记为中断，请复核本轮检测'));
      refreshHistory();
      return true;
    } catch (error) {
      if (error.status === 404) {
        stream = pendingStart = null;
        say('原采集流已失效，可重新开始');
        return true;
      }
      say(error.message + '；请稍后再次点击停止采集');
      return false;
    } finally { stopping = false; controls(); }
  }
  $('#stop').onclick = stop;
  $('#finish').onclick = async () => {
    if (!stream || stopping || archivedResult) return;
    stopping = true;
    localStop(false); controls();
    try {
      if (pending) await pending.catch(() => {});
      const result = await api('/station-streams/' + stream.streamId + '/finish', {method:'POST'}, 15000);
      if (!matches(result, stream) || !result.archived) throw new Error('归档响应不完整，请重试结束并归档');
      archivedResult = result;
      $('#final-record').hidden = false;
      $('#final-text').textContent = `检测轮次 ${result.streamEpoch} · ${result.compliance?.label || '待复核'} · ${result.compliance?.reason || ''} · ${date(result.archivedAt)}`;
      await json('/station-streams/' + stream.streamId, 'DELETE', {reason:'算法结果已归档，释放采集流'});
      stream = null;
      say('本轮算法结果已归档，可开始下一轮检测');
    } catch (error) {
      say(error.message + (archivedResult ? '；结果已归档，请再次点击停止采集释放连接' : '；可重试结束并归档，或选择停止原因后停止采集'));
    } finally {
      stopping = false; controls(); refreshHistory();
    }
  };
  $('#start').onclick = async () => {
    if (starting || running || stream || pendingStart || stopping) return;
    starting = true; controls();
    const id = ++generation;
    try {
      const deviceId = $('#device').value;
      if (!deviceId) throw new Error('请先授权并选择本工位摄像头');
      const acquired = await navigator.mediaDevices.getUserMedia({video:{
        deviceId:{exact:deviceId}, width:{ideal:1920}, height:{ideal:1080}
      }, audio:false});
      if (id !== generation) { acquired.getTracks().forEach(track => track.stop()); return; }
      media = acquired;
      $('.terminal-stage').classList.remove('idle');
      $('#usb-note').hidden = true;
      $('#preview').srcObject = media;
      await $('#preview').play();
      pendingStart = {requestId:crypto.randomUUID()};
      stream = await json('/station-streams', 'POST', pendingStart);
      pendingStart = null;
      if (id !== generation) { controls(); return; }
      try { localStorage.setItem('aisop-terminal-camera:' + binding.cameraId, deviceId); } catch {}
      started = performance.now(); seq = 0; minResultSeq = 0;
      completedFrames = 0; archivedResult = null; $('#final-record').hidden = true;
      running = true; localBlocked = false;
      limitTimer = setInterval(() => {
        const maximum = stream?.streamLimits?.maxDurationSeconds ?? 600;
        if (running && !stopping && performance.now() - started >= maximum * 1000) {
          void stop({reason:`本轮检测已达到最长 ${maximum} 秒，已自动停止；请确认工件后重新开始`});
        }
      }, 500);
      controls(); clearResult();
      media.getVideoTracks()[0].addEventListener('ended', () => {
        if (running && id === generation) {
          localStop(); controls();
          say('相机已断开，请停止并重新建立采集流');
        }
      });
      tick(id);
      refreshHistory();
    } catch (error) {
      if (error.status && error.status >= 400 && error.status < 500) pendingStart = null;
      localStop(); controls();
      say(error.message + (stream || pendingStart ? '；请点击停止采集以释放本轮' : ''));
    } finally { starting = false; controls(); }
  };
  async function tick(id) {
    if (!running || id !== generation) return;
    if (document.hidden || !navigator.onLine || transportLost) { timer = setTimeout(() => tick(id), 500); return; }
    const sampledAt = performance.now(), video = $('#preview');
    let retryMs = 0;
    const controller = new AbortController();
    frameAbort = controller;
    const work = (async () => {
      if (!video.videoWidth || !video.videoHeight) throw new Error('等待摄像头画面');
      const pose = binding.analysisType === 'pose';
      const flexible = ['sticker', 'scene'].includes(binding.analysisType);
      const scale = flexible
        ? Math.min(1, 1920 / Math.max(video.videoWidth, video.videoHeight),
            Math.sqrt(1920 * 1080 / (video.videoWidth * video.videoHeight)))
        : Math.min(1, (pose ? 1280 : 1920) / video.videoWidth, (pose ? 720 : 1080) / video.videoHeight);
      capture.width = Math.max(1, Math.floor(video.videoWidth * scale));
      capture.height = Math.max(1, Math.floor(video.videoHeight * scale));
      capture.getContext('2d').drawImage(video, 0, 0, capture.width, capture.height);
      let blob;
      for (const quality of [.82, .70, .55]) {
        blob = await new Promise(resolve => capture.toBlob(resolve, 'image/jpeg', quality));
        if (blob && blob.size <= 1048576) break;
      }
      if (!blob || blob.size > 1048576) throw new Error('采样过大或编码失败');
      if (!running || id !== generation || document.hidden || !navigator.onLine || transportLost || performance.now()-sampledAt>500) return;
      const currentSeq = seq++;
      const result = await api('/station-streams/' + stream.streamId + '/frames', {
        method:'POST', signal:controller.signal, body:blob, headers:{
          'Content-Type':'image/jpeg', 'X-Frame-Seq':String(currentSeq),
          'X-Frame-Time':String((sampledAt - started) / 1000), 'X-Stream-Epoch':String(stream.streamEpoch),
          'X-Station-Id':binding.stationId, 'X-Camera-Id':binding.cameraId
        }
      }, 8000);
      if (!running || id !== generation) return;
      if (!matches(result, stream) || result.frameSeq !== currentSeq) {
        localStop(); controls();
        say('服务器结果身份不匹配，采集已停止；请停止本轮并重新开始');
        return;
      }
      say(`已处理帧 ${result.frameSeq} · 采样至响应 ${Math.round(performance.now() - sampledAt)} ms`);
      completedFrames++; controls();
    })();
    pending = work;
    try { await work; }
    catch (error) {
      if (id !== generation) return;
      clearResult('等待新采样'); say(error.message);
      if ([401, 403].includes(error.status)) { disconnected(error.message); return; }
      if (error.status === 410) {
        localStop(); stream = null; pendingStart = null; controls(); refreshHistory();
        say(error.message); return;
      }
      if ([404, 409, 422].includes(error.status)) {
        localStop(); controls();
        say(error.message + '；请停止本轮并重新开始');
        return;
      }
      if (!error.status || error.status >= 500 || error.status === 408) loseTransport();
      retryMs = (error.retryMs || 500) + Math.random() * 250;
    } finally {
      if (pending === work) pending = null;
      if (frameAbort === controller) frameAbort = null;
    }
    if (running && id === generation) {
      const interval = 1000 / binding.sampleFps;
      timer = setTimeout(() => tick(id), Math.max(retryMs, interval - (performance.now() - sampledAt), 0));
    }
  }
  $('#logout').onclick = async () => {
    if (!await stop()) return;
    try { await api('/terminal/logout', {method:'POST'}); location.reload(); }
    catch (error) { say(error.message); }
  };
  document.addEventListener('visibilitychange', () => {
    if (stream) minResultSeq = seq;
    clearResult(document.hidden ? '页面在后台 · 采样暂停' : '等待恢复后的新采样');
  });
  function loseTransport() {
    if (!transportLost) transportGeneration++;
    transportLost = true; minResultSeq = seq;
    clearResult('中央推理连接中断 · 无法检测');
    $('#online').textContent = '无法检测';
  }
  window.addEventListener('offline', loseTransport);
  window.addEventListener('online', () => clearResult('连接恢复中 · 等待租约和轮次核对'));
  window.addEventListener('pagehide', () => localStop());
  setInterval(() => {
    if (resultDeadline && performance.now() >= resultDeadline) {
      clearResult('结果已过期 · 待新采样');
      $('#online').textContent = '结果已过期';
      $('#online').className = 'badge review';
      $('#age').textContent = '≥1000 ms';
    }
    if (binding && performance.now() - lastPollAt > 1500) {
      loseTransport();
      $('#online').textContent = '连接中断';
    }
  }, 250);
  api('/terminal/context').then(context => { if (context.binding) connected(context); }).catch(() => {});
})();
