/* Station detection in the original dashboard, independent of manual workpieces. */
window.StationDetection = (() => {
  'use strict';
  const $ = selector => document.querySelector(selector), W = window.Workspace, E = W.escape;
  const reviews = window.ReviewOutbox, collector = window.StationCollectorControl;
  const state = {active:false, station:null, config:null, binding:null, connected:false, busy:false,
    running:false, stream:null, pendingStart:null, pending:null, sampling:null, queued:null,
    timer:null, sendTimer:null, retryAt:0, freshnessTimer:null, resultExpiryTimer:null, resultDeadline:0,
    inputGeneration:0, stoppedAt:0, completedTimes:[], counters:null,
    previewMode:'detection', previewFps:null, previewAt:0,
    generation:0, mediaGeneration:0, media:null, sourceURL:null, ready:false, frame:null, final:null,
    frames:0, started:0, lastAt:0, latency:null, timing:null, error:'', source:'camera', archived:false,
    editing:false, scenes:[], scenesError:'', configuredTargets:[], preparation:null, transportLost:false,
    collector:null, collectorCommand:null, remoteTimer:null, remotePoll:null, remoteFps:null, remoteBarrier:null, remoteAnnounce:null, remoteManual:false, remoteNotice:null, remoteAutoFinish:null};
  let transportRecovery = null;
  const names = {screw:'螺丝安装检测', sticker:'贴条工序检测', pose:'人体检测与骨骼', scene:'已配置场景模型', manual:'尚未配置AI模型'};
  const sources = {camera:'页面摄像头',replay:'视频联调',image:'图片联调',device:'后台采集（正式）'};
  const sampleMaxAge = 500, resultMaxAge = 1000;
  const identityFields = ['stationId','workstationId','cameraId','revision','streamId','streamEpoch','analysisType','sceneId','modelRevision'];
  const matchesResult = (result, identity) => result && identityFields.every(key => result[key] === identity[key]);
  const detectionScope = () => state.binding?.analysisType === 'screw' ? '螺丝模型按已标定视角累计确认位置，不核验扭矩、工序先后或未训练的孔位工艺。' : state.binding?.analysisType === 'sticker' ? '贴条模型按本轮标定位置累计确认形状与存在状态，全部位置确认后合规；不核验操作顺序或胶粘强度。' : state.binding?.analysisType === 'pose' ? '人体姿态观察不代表装配工序通过。' : '模型判定范围以当前已配置任务为准。';
  const visualCoverageComplete = result => result?.compliance?.status === 'PASS'
    && result.sequence?.verificationMode === 'visual-cumulative' && result.sequence?.status === 'PASS'
    && result.sequence?.configured === true && result.sequence.positionsExpected > 0
    && result.sequence.positionsConfirmed === result.sequence.positionsExpected
    && result.sequence.operationsCompleted === result.sequence.operationsExpected
    && result.sequence.preassembledConfirmed === result.sequence.preassembledExpected
    && result.sequence.operationsExpected + result.sequence.preassembledExpected === result.sequence.positionsExpected;
  const assessment = result => result?.manualReview || result?.compliance;
  function samplingGapDetail(result) {
    if (result?.manualReview || result?.compliance?.status!=='REVIEW') return '';
    const gaps = [...(result.evidenceGaps || []),...(result.stationEvidenceGaps || [])];
    const unique = new Map();
    for (const gap of gaps) {
      if (!Number.isFinite(gap.start) || !Number.isFinite(gap.end) || gap.start<0 || gap.end<gap.start) continue;
      const key = `${gap.start}:${gap.end}`, previous = unique.get(key);
      if (!previous || (gap.elapsedSeconds || 0)>(previous.elapsedSeconds || 0)) unique.set(key,gap);
    }
    if (!unique.size) return '';
    const ranges = [...unique.values()].sort((a,b)=>a.start-b.start).slice(0,4).map(gap =>
      `${gap.start.toFixed(2)}～${gap.end.toFixed(2)} 秒（${(gap.end-gap.start).toFixed(2)} 秒${Number.isFinite(gap.elapsedSeconds) && gap.elapsedSeconds>gap.end-gap.start ? `，服务器间隔 ${gap.elapsedSeconds.toFixed(2)} 秒` : ''}）`);
    const original = result.originalCompliance?.status==='PASS' ? '模型原判定：合规；采样完整性检查后需复核。' : '';
    return `${original}采样缺口：${ranges.join('、')}${unique.size>4?`，共 ${unique.size} 处`:''}。`;
  }
  const canReview = result => result && !result.manualReview && ['FAIL','REVIEW'].includes(result.compliance?.status)
    && (result.analysisType || state.binding?.analysisType) !== 'pose';
  const reviewSummary = result => result?.manualReview ? `<p class="sd-review-audit">${E(result.manualReview.label)} · ${E(result.manualReview.reviewer)} · ${E(new Date(result.manualReview.reviewedAt*1000).toLocaleString())}<br>原因：${E(result.manualReview.reason || '未填写')}</p>` : '';
  let mounted = false, renderedHTML = new WeakMap();
  // Encoding in a worker avoids main-thread idle delays when the Board repaints.
  const capture = typeof OffscreenCanvas === 'function' ? new OffscreenCanvas(1,1) : document.createElement('canvas');
  const captureContext = capture.getContext('2d');
  let overlayContext;
  let encoder, encodingId=0, previewCallback=null, previewWindow=null, cancelMediaLoad=null;
  const validPicture = media => {
    const width=media?.videoWidth ?? media?.naturalWidth, height=media?.videoHeight ?? media?.naturalHeight;
    return Number.isFinite(width) && width>0 && Number.isFinite(height) && height>0 && !media.error;
  };
  const videoFormatHelp = '未解码出有效视频画面，请换用浏览器支持的 H.264 MP4 或 WebM 视频';
  const encoding=new Map();
  function closeEncoder(message='图像编码已停止') {
    encoder?.terminate(); encoder=null;
    for (const task of encoding.values()) {clearTimeout(task.timer); task.reject(new Error(message));}
    encoding.clear();
  }
  async function encodeSample() {
    if (typeof Worker === 'function' && capture.convertToBlob && typeof createImageBitmap === 'function') {
      if (!encoder) {
        encoder=new Worker('/station-frame-worker.js');
        encoder.onmessage=({data})=>{
          const task=encoding.get(data.id); if (!task) {data.bitmap?.close(); return;}
          encoding.delete(data.id); clearTimeout(task.timer);
          if (data.error) {data.bitmap?.close(); task.reject(new Error(data.error));}
          else task.resolve({blob:data.blob,image:data.bitmap});
        };
        encoder.onerror=()=>closeEncoder('图像编码线程异常，请重试');
      }
      const worker=encoder, bitmap=await createImageBitmap(capture), id=++encodingId;
      // A pause/stop can terminate this worker while bitmap creation is pending.
      if (worker!==encoder) {bitmap.close(); throw new Error('图像编码已停止');}
      return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>closeEncoder('图像编码超时，请重试'),8000);
        encoding.set(id,{resolve,reject,timer});
        try {worker.postMessage({id,bitmap},[bitmap]);}
        catch (error) {bitmap.close();closeEncoder(error.message);}
      });
    }
    // Keep a frame-owned canvas for browsers without transferable worker encoding.
    const image=document.createElement('canvas'); image.width=capture.width; image.height=capture.height;
    image.getContext('2d').drawImage(capture,0,0);
    let blob;
    for (const quality of [.82,.70,.55]) {
      blob=await new Promise(resolve=>image.toBlob(resolve,'image/jpeg',quality));
      if (blob && blob.size<=1048576) break;
    }
    return {blob,image};
  }
  function discardSample(sample) {sample?.image?.close?.();}
  function count(name) {if (state.counters) state.counters[name]++;}
  function clearQueuedSample(reason='discarded') {
    if (state.queued) count(reason);
    discardSample(state.queued); state.queued=null;
  }
  function resetMeasurements() {
    state.completedTimes=[]; state.stoppedAt=0; state.resultDeadline=0; state.lastSubmittedSeq=-1;
    state.counters={captured:0,submitted:0,replaced:0,expired:0,discarded:0,failed:0,staleResults:0};
    clearTimeout(state.resultExpiryTimer);
  }
  function effectiveFps() {
    if (state.source === 'device') return state.remoteFps;
    if (!state.started) return null;
    const clock=state.stoppedAt || performance.now(), elapsed=Math.min(10000,clock-state.started);
    return elapsed>0 ? state.completedTimes.filter(at=>at>clock-10000 && at<=clock).length/elapsed*1000 : null;
  }
  function resetPreviewRate() {previewWindow=null; state.previewFps=null; state.previewAt=0;}
  function stopPreviewMeter() {
    if (previewCallback!==null) $('#sd-video')?.cancelVideoFrameCallback?.(previewCallback);
    previewCallback=null; resetPreviewRate();
  }
  function startPreviewMeter(video) {
    stopPreviewMeter();
    if (typeof video.requestVideoFrameCallback!=='function') return;
    const generation=state.mediaGeneration;
    const observe=(at, metadata)=>{
      previewCallback=null;
      if (!state.active || generation!==state.mediaGeneration) return;
      if (document.hidden || video.paused) resetPreviewRate();
      else {
        previewWindow ||= {at,frames:metadata.presentedFrames};
        const elapsed=at-previewWindow.at, frames=metadata.presentedFrames-previewWindow.frames;
        if (frames<0) resetPreviewRate();
        else if (elapsed>=1000) {
          state.previewFps=frames/elapsed*1000; state.previewAt=at;
          previewWindow={at,frames:metadata.presentedFrames};
        }
      }
      previewCallback=video.requestVideoFrameCallback(observe);
    };
    previewCallback=video.requestVideoFrameCallback(observe);
  }
  const previewFps = () => state.source==='image' || document.hidden ? null
    : state.previewFps===null ? null : performance.now()-state.previewAt>1500 ? 0 : state.previewFps;
  function expireResult() {
    clearTimeout(state.resultExpiryTimer); state.resultDeadline=0;
    if (mounted) $('#sd-overlay').hidden=true;
  }
  function haltSampling() {
    clearTimeout(state.timer); clearTimeout(state.sendTimer); state.timer=state.sendTimer=null;
    if (!state.stoppedAt && state.started) state.stoppedAt=performance.now();
    clearQueuedSample(); closeEncoder(); expireResult();
  }
  const currentRun = (generation, identity) => state.active && state.running && generation===state.generation && identity===state.stream;
  const inputAvailable = media => validPicture(media) && !state.transportLost && !document.hidden && navigator.onLine && state.ready && !state.media?.getVideoTracks()[0]?.muted &&
    (state.source==='image' || media.readyState>=2 && !media.paused);
  const liveResult = () => state.running && !state.error && state.frame && state.resultDeadline>performance.now() &&
    (state.source === 'device' ? !state.transportLost && !document.hidden && navigator.onLine
      : inputAvailable($(state.source==='image' ? '#sd-image' : '#sd-video')));
  function invalidateInput() {
    if (!state.active) return;
    state.inputGeneration++; state.lastMediaTime=null;
    if (state.source === 'device' && state.frame) state.remoteBarrier = {streamId:state.frame.streamId,frameSeq:state.frame.frameSeq};
    clearQueuedSample(); closeEncoder(); expireResult(); resetPreviewRate();
    clearTimeout(state.sendTimer); render();
  }
  function loseTransport() {
    if (!state.active || !state.running) return;
    state.transportLost = true;
    state.error = state.source === 'device' ? '页面连接中断，后台采集状态待核对' : '中央推理连接中断，无法检测；等待核对租约和检测轮次';
    invalidateInput(); say(state.error);
  }
  async function recoverTransport() {
    if (state.source === 'device') {if (state.active && navigator.onLine) void pollCollector(); return;}
    if (transportRecovery || !state.active || !state.running || !state.stream || !state.transportLost || !navigator.onLine) return;
    const generation = state.generation, identity = state.stream, inputGeneration = state.inputGeneration;
    const work = (async () => {
      try {
        const status = await W.request('/stations/' + identity.stationId + '/live-status', {cache:'no-store', timeout:3000});
        if (!currentRun(generation, identity) || inputGeneration !== state.inputGeneration) return;
        if (!matchesResult(status, identity) || !['LIVE','STALE'].includes(status.state) || status.reasonCode === 'FINALIZING') {
          state.running = false; haltSampling();
          state.error = '无法检测：原租约或检测轮次已变化，请结束或停止本轮，再开始新一轮';
        } else {
          // Status may contain a pre-disconnect PASS; only a newly sampled response can restore the verdict.
          state.transportLost = false; state.inputGeneration++; state.frame = null; expireResult();
          state.error = '连接已恢复，等待新采样';
        }
        say(state.error); render();
      } catch (error) {
        if (currentRun(generation, identity) && [401,403,404].includes(error.status)) {
          state.running = false; haltSampling(); state.error = '无法检测：' + error.message; render();
        }
      }
    })();
    transportRecovery = work;
    try { await work; } finally { if (transportRecovery === work) transportRecovery = null; }
  }
  setInterval(recoverTransport, 2000);
  const occupied = () => state.busy || state.running || !!state.stream || !!state.pendingStart || state.source === 'device' && remotePending();
  const remotePending = () => collector?.pending(state.collectorCommand) || state.collector?.command?.status === 'PENDING';
  function stopCollectorPolling() {clearTimeout(state.remoteTimer); state.remoteTimer = null;}
  function applyCollector(status) {
    if (status.stationId !== state.station || status.bindingRevision != null && status.bindingRevision !== state.binding?.revision)
      throw new Error('后台采集配置已变化，请重新加载工位配置');
    const previous = state.stream?.streamId, identity = status.stream;
    if (identity && (identity.stationId !== state.station || identity.cameraId !== state.binding.cameraId ||
        identity.workstationId !== state.binding.workstationId || identity.revision !== state.binding.revision))
      throw new Error('后台检测轮次与当前工位身份不一致');
    state.collector = status;
    state.transportLost = false;
    if (identity?.streamId !== previous) {
      clearResults(); state.remoteBarrier = null;
      if (identity) state.started = performance.now();
    }
    state.stream = identity || null;
    state.running = !!identity && !['FINISHING','STOPPING'].includes(status.state);
    state.error = status.error || '';
    if (!status.online && identity) state.error = '后台采集器离线，本轮状态待核对';
    if (!state.running) expireResult();
    const record = status.record;
    if (!identity && status.command?.status !== 'PENDING' && record?.status === 'FINISHED' && record.result?.archived && record.result.streamId === record.streamId &&
        record.result.stationId === state.station) {
      state.final = record.result; state.archived = true;
      if (state.remoteAnnounce && status.command?.requestId === state.remoteAnnounce && status.command.status === 'SUCCEEDED') {
        const manual = state.remoteManual; state.remoteAnnounce = null; state.remoteManual = false;
        queueMicrotask(() => {if (state.active && state.source === 'device' && state.final === record.result)
          manual && canReview(record.result) ? void showManualReview(record.result) : showOutcome(record.result);});
      }
    } else if (!identity && record?.status === 'INTERRUPTED') {
      state.final = null; state.archived = false; state.error = record.reason || '本轮已中断，请复核后开始下一件';
    }
    if (state.remoteAnnounce && status.command?.requestId === state.remoteAnnounce && status.command.status === 'FAILED') {
      state.remoteAnnounce = null; state.remoteManual = false;
    }
    set('#sd-source-status', status.online ? '后台采集器在线 · 刷新页面不停止采集' : '后台采集器离线');
  }
  async function collectorPreview(generation) {
    const identity = state.stream, inputGeneration = state.inputGeneration;
    if (!identity || !state.running || document.hidden || !navigator.onLine) return;
    const requestedAt = performance.now();
    const live = await W.request('/stations/'+state.station+'/live-status',{cache:'no-store',timeout:2500});
    const current = () => state.active && state.source === 'device' && state.generation === generation &&
      state.inputGeneration === inputGeneration && state.stream?.streamId === identity.streamId && state.running && !document.hidden && navigator.onLine;
    if (!current()) return;
    state.remoteFps = Number.isFinite(live.effectiveFps) ? live.effectiveFps : null;
    const result = live.result, deadline = requestedAt + resultMaxAge - (live.resultAgeMs ?? resultMaxAge);
    if (live.state !== 'LIVE' || !matchesResult(live,identity) || !matchesResult(result,identity) ||
        !Number.isSafeInteger(result.frameSeq) || result.frameSeq < (state.frame?.frameSeq ?? -1) || deadline <= performance.now()) {
      expireResult(); return;
    }
    if (state.remoteBarrier?.streamId === result.streamId && result.frameSeq <= state.remoteBarrier.frameSeq) {expireResult(); return;}
    if (state.frame?.frameSeq === result.frameSeq && state.resultDeadline > performance.now()) return;
    const controller = new AbortController(), timer = setTimeout(()=>controller.abort(),2000);
    let image;
    try {
      const query = new URLSearchParams({stream_id:result.streamId,frame_seq:String(result.frameSeq)});
      const response = await fetch('/api/v1/stations/'+state.station+'/preview?'+query,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if (!current()) return;
      if (!response.ok) {expireResult(); return;}
      if (response.headers.get('X-Station-Id') !== identity.stationId || response.headers.get('X-Camera-Id') !== identity.cameraId ||
          response.headers.get('X-Stream-Id') !== identity.streamId || Number(response.headers.get('X-Stream-Epoch')) !== identity.streamEpoch ||
          Number(response.headers.get('X-Frame-Seq')) !== result.frameSeq) {expireResult(); return;}
      image = await createImageBitmap(await response.blob());
      if (!current() || deadline <= performance.now()) return;
      // Install one sample's picture and its result together after both are validated.
      draw(result,image); state.frame = result; state.ready = true; state.error = ''; state.transportLost = false;
      state.remoteBarrier = null; state.frames = Math.max(state.frames,result.frameSeq + 1); state.lastAt = performance.now();
      state.latency = live.resultAgeMs + performance.now() - requestedAt; state.resultDeadline = deadline;
      clearTimeout(state.resultExpiryTimer);
      state.resultExpiryTimer = setTimeout(()=>{expireResult(); render();},Math.max(0,deadline-performance.now()));
    } catch(error) {
      if (current()) throw error;
    } finally {clearTimeout(timer); image?.close();}
  }
  async function pollCollector() {
    if (!collector || !state.active || state.source !== 'device' || !W.session || !state.binding) return;
    if (state.remotePoll) return state.remotePoll;
    stopCollectorPolling();
    const generation = state.generation, station = state.station;
    const current = () => state.active && state.source === 'device' && state.generation === generation && state.station === station;
    const work = (async () => {
      try {
        const [status,entry] = await Promise.all([W.request(collector.path(station),{cache:'no-store',timeout:5000}),collector.reconcile(station)]);
        if (!current()) return;
        state.collectorCommand = entry; applyCollector(status);
        if (status.online) await collectorPreview(generation); else expireResult();
        if (current() && state.running && !state.busy && !state.error && !remotePending()
            && liveResult() && visualCoverageComplete(state.frame) && state.remoteAutoFinish !== state.stream?.streamId) {
          state.remoteAutoFinish = state.stream.streamId;
          await collectorCommand('finish');
        }
        if (current()) {
          const notice = JSON.stringify([status.state,status.online,status.command?.requestId,status.command?.status,
            status.record?.status,state.error,entry?.status,entry?.message]);
          if (notice !== state.remoteNotice) {
            state.remoteNotice = notice;
            say(state.error || (collector.pending(entry) || ['failed','blocked'].includes(entry?.status) ? entry.message
              : status.command?.status === 'PENDING' ? '采集器正在执行，等待保存结果'
              : state.archived ? '本件已归档 · 后台采集器等待下一件'
              : state.running ? '后台采集中 · 刷新页面不停止本件检测'
              : status.online ? '后台采集器就绪 · 可按 F8 开始下一件' : '后台采集器离线，请检查采集主机'));
          }
          render();
        }
      } catch(error) {
        if (current()) {
          if (state.frame) state.remoteBarrier = {streamId:state.frame.streamId,frameSeq:state.frame.frameSeq};
          state.transportLost = true; state.error = '后台采集状态待核对：'+error.message; expireResult(); render();
        }
      }
    })();
    state.remotePoll = work;
    try {await work;} finally {
      if (state.remotePoll === work) state.remotePoll = null;
      if (current()) state.remoteTimer = setTimeout(pollCollector,500);
    }
  }
  async function collectorCommand(action,{manual=false,reason=''}={}) {
    if (!collector || state.busy || remotePending()) return;
    const generation = state.generation, station = state.station, identity = state.stream?.streamId || null;
    state.busy = true; state.error = ''; render();
    try {
      const entry = await collector.execute(station,state.binding,action,action === 'start' ? null : identity,reason,state.collector);
      if (!state.active || generation !== state.generation || station !== state.station) return;
      state.collectorCommand = entry;
      if (action === 'finish' && (collector.pending(entry) || entry.status === 'complete')) {state.remoteAnnounce = entry.body.requestId; state.remoteManual = manual;}
      say(entry.message);
    } finally {if (generation === state.generation) {state.busy = false; render(); void pollCollector();}}
  }
  const configuring = () => !!W.session && W.session.role !== 'terminal' && state.connected &&
    (state.editing || !state.binding || state.binding.analysisType === 'manual');
  const configurationDirty = () => configuring() && ($('#sd-model').value !== (state.binding?.analysisType === 'manual' ? '' : state.binding?.analysisType || '') ||
    ($('#sd-model').value === 'scene' ? $('#sd-scene').value : null) !== (state.binding?.sceneId || null) ||
    Number($('#sd-fps').value) !== (state.binding?.sampleFps ?? 4));
  const json = (path, method, body) => W.request(path, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), timeout:15000});
  const say = message => {if (state.active) {$('#server-status').textContent = message; set('#sd-dialog-status', message);}};
  const attempt = fn => Promise.resolve().then(fn).catch(error => {state.error = error.message; say(error.message); render();});
  function prepareModel() {
    if (!state.active || !W.session || !state.binding?.enabled || state.binding.analysisType !== 'sticker') return Promise.resolve();
    const generation = state.generation, station = state.station, revision = state.binding.revision;
    const key = generation + ':' + station + ':' + revision;
    if (state.preparation?.key === key && state.preparation.status !== 'failed') return state.preparation.promise;
    const preparation = {key, status:'pending', promise:null};
    state.preparation = preparation;
    const current = () => state.active && state.generation === generation && state.station === station &&
      state.binding?.revision === revision && state.preparation === preparation;
    say('正在准备贴条模型，可先选择检测画面…');
    preparation.promise = W.request('/stations/' + station + '/prepare', {method:'POST', timeout:120000}).then(result => {
      if (!result.ready) throw new Error('贴条模型准备未完成，请稍后重试');
      preparation.status = 'ready';
      if (current() && !state.running && !state.editing) {say('贴条模型已就绪 · 准备画面后按 F8 开始检测'); render();}
    }).catch(error => {
      preparation.status = 'failed';
      if (current() && !state.running) {say('贴条模型准备失败：' + error.message + '；可按 F8 重试'); render();}
      throw error;
    });
    return preparation.promise;
  }
  const set = (selector, value) => {const node = $(selector); if (node && node.textContent !== String(value)) node.textContent = value;};
  const setHTML = (selector, value) => {
    const node = $(selector);
    if (node && renderedHTML.get(node) !== value) {node.innerHTML = value; renderedHTML.set(node, value);}
  };

  function mount() {
    if (mounted) return;
    const add = (selector, html, cls='') => {
      const node = document.createElement('div'); node.className = 'pilot-ui station-detection-ui ' + cls;
      node.innerHTML = html; $(selector).append(node); return node;
    };
    const controls = add('.video-panel', `<div class="source-controls">
      <label>图像来源<select id="sd-source"><option value="device">后台采集（正式）</option><option value="camera">页面摄像头（刷新会中断）</option><option value="replay">视频实时检测</option><option value="image">图片联调</option></select></label>
      <button id="sd-camera">开启摄像头</button><label id="sd-file-label" hidden>选择素材<input id="sd-file" type="file" accept="video/*" /></label>
      <label id="sd-preview-label">画面显示<select id="sd-preview-mode"><option value="detection">检测采样与识别框</option><option value="live">实时视频（无识别框）</option></select></label>
      <button id="sd-start" class="primary">开始检测（F8）</button><button id="sd-finish">结束并归档</button><button id="sd-stop" aria-keyshortcuts="F9">停止本轮（F9）</button>
      <span id="sd-source-status" class="hint">等待输入</span></div>
      <div id="sd-collector-controls" hidden><p id="sd-collector-note" class="hint" role="status"></p>
      <label>中断原因<select id="sd-stop-reason"><option value="操作员中断本件检测，需复核">操作员中断本件</option><option value="工件更换，原检测未完成">工件更换</option><option value="设备或采样异常，本件需复核">设备或采样异常</option></select></label>
      <button id="sd-command-check">核对后台状态</button><button id="sd-command-retry" hidden>重试原控制请求</button></div>`);
    $('.video-mode-control').after(controls);
    add('.video-frame', '<video id="sd-video" muted playsinline hidden></video><img id="sd-image" alt="当前检测素材" hidden><canvas id="sd-overlay" hidden></canvas><p id="sd-preview-wait" role="status" hidden>等待首帧检测结果…</p><p id="sd-empty" class="empty-note">开启摄像头或选择素材，然后按 F8 开始检测</p>', 'station-detection-media');
    const configButton = document.createElement('button');
    configButton.id = 'sd-config-open'; configButton.className = 'btn station-detection-ui';
    configButton.type = 'button'; configButton.textContent = '工位配置';
    configButton.setAttribute('aria-haspopup', 'dialog'); configButton.setAttribute('aria-controls', 'sd-config-dialog');
    $('#evidence-history-open').after(configButton);
    const configDialog = document.createElement('dialog');
    configDialog.id = 'sd-config-dialog'; configDialog.className = 'pilot-ui station-detection-ui narrow';
    configDialog.setAttribute('aria-labelledby', 'sd-config-title');
    configDialog.innerHTML = '<div class="dialog-heading"><h2 id="sd-config-title">工位配置</h2><button id="sd-config-close" type="button" aria-label="关闭工位配置">×</button></div>';
    document.body.append(configDialog);
    add('#sd-config-dialog', `<p class="notice">无需产品型号或工件编号。关闭弹窗后按 F8 开始当前工位检测。</p>
      <button id="sd-login">登录服务器</button><button id="sd-refresh">刷新工位配置</button>
      <button id="sd-edit" hidden>修改检测配置</button>
      <form id="sd-setup" hidden><label>检测模型<select id="sd-model" required><option value="">请选择检测模型</option><option value="screw">螺丝安装检测</option><option value="sticker">贴条工序检测</option><option value="pose">人体检测与骨骼</option><option value="scene">自定义场景</option></select></label>
      <label id="sd-scene-field" hidden>已发布场景<select id="sd-scene"></select></label><p id="sd-scene-note" class="hint" hidden></p>
      <label>目标采样（fps）<input id="sd-fps" type="number" min="0.5" max="30" step="any" value="4" required></label>
      <p class="hint">选择已适配当前画面的模型。工位工艺清单不会自动转换成模型能力；配置变化后会重新签发设备凭据，外部采集端需更新凭据。</p>
      <p id="sd-config-note" class="hint" role="status"></p><button id="sd-save" type="submit">保存检测配置</button><button id="sd-cancel-edit" type="button" hidden>取消修改</button></form>
      <p id="sd-binding" class="hint"></p><p id="sd-credential" class="credential-note" hidden></p>
      <a href="/terminals" target="_blank" rel="noopener">管理工位模型与设备配置</a>
      <p id="sd-dialog-status" class="hint" role="status"></p>`, 'dialog-body');
    configButton.onclick = () => {render(); if (!configDialog.open) configDialog.showModal();};
    $('#sd-config-close').onclick = () => configDialog.close();
    configDialog.addEventListener('close', () => window.InspectionTrigger?.refresh());
    const completion = add('.workflow-body', `<div id="sd-completion" role="status" hidden>
      <strong>检测合规</strong><p>本轮已停止并归档，可更换工件。</p>
      <button id="sd-next" class="primary">检测下一个工件（F8）</button></div>`, 'station-status-row');
    $('#steps').before(completion);
    const review = add('.workflow-body', `<div id="sd-review-panel" hidden><p id="sd-review-note"></p>
      <button id="sd-review" aria-describedby="sd-review-note">人工确认</button></div>`, 'station-status-row');
    $('#steps').before(review);
    const outbox = add('.workflow-body', '<div id="sd-outbox" hidden><p id="sd-outbox-note" role="status"></p><button id="sd-outbox-open">核对人工确认</button></div>', 'station-status-row');
    $('#steps').before(outbox);
    $('#sd-outbox-open').onclick = () => attempt(showReviewQueue);
    add('.results-panel', '<p id="sd-archive" class="hint" role="status"></p>', 'station-archive-status');
    add('.alert-toolbar', '<button id="sd-history">查看工位检测记录</button>');
    $('#sd-source').onchange = () => {
      stopCollectorPolling(); clearMedia(); state.generation++; state.remotePoll = null; state.remoteNotice = null; state.inputGeneration++; state.source = $('#sd-source').value;
      state.stream = null; state.running = false; clearResults(); render();
      if (state.source === 'device') void pollCollector();
    };
    $('#sd-command-check').onclick = () => attempt(pollCollector);
    $('#sd-command-retry').onclick = () => attempt(async () => {
      if (state.busy || state.source !== 'device') return;
      const generation = state.generation, station = state.station;
      state.busy = true; render();
      try {
        const entry = await collector.reconcile(station,{submit:true});
        if (generation === state.generation) {state.collectorCommand = entry; say(entry?.message || '状态已核对');}
      } finally {if (generation === state.generation) {state.busy = false; render(); void pollCollector();}}
    });
    $('#sd-preview-mode').onchange = () => {
      state.previewMode=$('#sd-preview-mode').value;
      $('#sd-overlay').hidden=true; render();
    };
    $('#sd-file').onchange = e => attempt(() => loadFile(e.target.files[0]));
    $('#sd-camera').onclick = () => attempt(camera);
    $('#sd-start').onclick = () => attempt(start);
    $('#sd-next').onclick = () => attempt(start);
    $('#sd-review').onclick = () => attempt(async () => {
      if (state.stream) await finish({manual:true}); else if (state.final) showManualReview(state.final);
    });
    $('#sd-finish').onclick = () => attempt(finish);
    $('#sd-stop').onclick = () => attempt(stop);
    $('#sd-login').onclick = () => W.login();
    $('#sd-refresh').onclick = () => attempt(sync);
    $('#sd-edit').onclick = () => {if (occupied()) return; populateSetup(); state.editing = true; render(); $('#sd-model').focus();};
    $('#sd-cancel-edit').onclick = () => {if (occupied()) return; populateSetup(); state.editing = false; render();};
    $('#sd-setup').oninput = render;
    $('#sd-model').onchange = render;
    $('#sd-scene').onchange = render;
    $('#sd-setup').onsubmit = e => {e.preventDefault(); attempt(configure);};
    $('#sd-history').onclick = () => attempt(history);
    $('#sd-video').addEventListener('ended', () => {if (state.running && state.source === 'replay') attempt(finish);});
    // Pausing/seeking does not feed repeated or out-of-order frames into the detector.
    $('#sd-video').addEventListener('seeking', () => {if (state.running && state.source === 'replay') attempt(stop);});
    $('#sd-video').addEventListener('pause', () => {if (state.running) invalidateInput(); else resetPreviewRate();});
    $('#sd-video').addEventListener('playing', resetPreviewRate);
    mounted = true;
  }

  function populateSetup() {
    const binding = state.binding, select = $('#sd-scene');
    $('#sd-model').value = binding?.analysisType === 'manual' ? '' : binding?.analysisType || '';
    $('#sd-fps').value = binding?.sampleFps ?? 4;
    select.replaceChildren(new Option('请选择已发布且可用的场景', ''));
    for (const scene of state.scenes) {
      const option = new Option(scene.name + (scene.ready ? '' : ' · 暂不可用'), scene.id);
      option.disabled = !scene.ready; select.add(option);
    }
    if (binding?.sceneId && !state.scenes.some(scene => scene.id === binding.sceneId)) {
      const option = new Option('原场景未能加载，请刷新或重新选择', binding.sceneId);
      option.disabled = true; select.add(option);
    }
    select.value = binding?.sceneId || '';
  }
  async function loadConfiguredTargets(binding) {
    if (!['screw','sticker'].includes(binding?.analysisType)) return [];
    const model = await W.request('/' + binding.analysisType + '-model');
    return (model.targets || []).map(target => ({...target,
      state:'UNKNOWN', score:null, bbox:null, confirmedAt:null}));
  }

  function clearResults() {
    Object.assign(state,{frame:null,final:null,frames:0,archived:false,started:0,lastAt:0,latency:null,timing:null,transportLost:false});
    resetMeasurements();
    $('#sd-overlay').hidden = true;
  }

  async function sync() {
    if (!state.active || !W.session || occupied()) return;
    state.busy = true; state.connected = false; render();
    const generation = state.generation;
    try {
      let binding;
      let scenes = [], scenesError = '';
      if (W.session.role === 'terminal') binding = (await W.request('/stations/' + state.station + '/context')).binding;
      else {
        const results = await Promise.allSettled([W.request('/admin/terminals'), W.request('/admin/scenes')]);
        if (results[0].status === 'rejected') throw results[0].reason;
        binding = results[0].value.items.find(row => row.stationId === state.station) || null;
        if (results[1].status === 'fulfilled') scenes = results[1].value.items.filter(scene => scene.status === 'published');
        else scenesError = '场景读取失败：' + results[1].reason.message + '。请刷新工位配置后重试。';
      }
      if (!state.active || generation !== state.generation) return;
      const configuredTargets = await loadConfiguredTargets(binding);
      if (!state.active || generation !== state.generation) return;
      if (state.binding?.revision !== binding?.revision) clearResults();
      state.configuredTargets = configuredTargets;
      state.binding = binding; state.connected = true; state.error = '';
      state.scenes = scenes; state.scenesError = scenesError; state.editing = false; populateSetup();
      if (binding && collector) {
        const [status,entry] = await Promise.all([
          W.request(collector.path(state.station),{cache:'no-store',timeout:5000}).catch(()=>null), collector.get(state.station).catch(()=>null)]);
        if (!state.active || generation !== state.generation) return;
        state.collector = status; state.collectorCommand = entry;
        if (status?.online || status?.stream || collector.pending(entry) || binding.source === 'rtsp' || state.source === 'device') {
          clearMedia(); state.source = 'device';
          if (status) applyCollector(status);
          void pollCollector();
        }
      }
      if (binding && binding.analysisType !== 'manual') {
        say(binding.enabled ? state.source === 'device' ? '后台采集 · 按件开始、结束并归档；刷新页面不停止采集' : '已连接 · 无需工件登记，准备画面后按 F8 开始检测' : '该工位设备已停用，请在设备配置中启用');
      } else say('请打开“工位配置”选择检测模型并保存，然后按 F8 开始检测');
      void prepareModel().catch(() => {});
    } catch (error) {state.error = error.message; say(error.message);}
    finally {state.busy = false; render();}
  }

  async function configure() {
    if (occupied() || !configuring() || !$('#sd-setup').reportValidity()) return;
    const previous = state.binding, generation = state.generation, station = state.station;
    if ($('#sd-model').value === 'scene' && !state.scenes.some(scene => scene.id === $('#sd-scene').value && scene.ready))
      throw new Error('请选择已发布且模型可用的场景；场景读取失败时请刷新工位配置');
    state.busy = true; render();
    try {
      const number = station.split('-')[1];
      const saved = await json('/admin/terminals/' + station, 'PUT', {
        workstationId:previous?.workstationId || 'dashboard-pc-' + number,
        cameraId:previous?.cameraId || 'dashboard-cam-' + number, source:previous?.source || 'usb',
        analysisType:$('#sd-model').value, sampleFps:Number($('#sd-fps').value),
        sceneId:$('#sd-model').value === 'scene' ? $('#sd-scene').value : null,
        maxSampleGapSeconds:previous?.maxSampleGapSeconds || 1, previewUrl:previous?.previewUrl || '',
        enabled:previous?.enabled ?? true, revision:previous?.revision || 0,
      });
      if (!state.active || generation !== state.generation) return;
      if (saved.changed) clearResults();
      state.binding = saved.binding; state.connected = true; state.error = ''; state.editing = false; populateSetup();
      $('#sd-credential').hidden = !saved.credential;
      $('#sd-credential').textContent = saved.credential ? '设备凭据（仅本次显示，本页登录后无需输入）：' + saved.credential : '';
      state.configuredTargets = [];
      const configuredTargets = await loadConfiguredTargets(saved.binding);
      if (!state.active || generation !== state.generation) return;
      state.configuredTargets = configuredTargets;
      say(saved.changed ? '检测配置已保存 · 下轮使用新配置，按 F8 开始检测' : '配置未变化，原设备凭据继续有效');
      void prepareModel().catch(() => {});
    } finally {state.busy = false; render();}
  }

  function clearMedia() {
    state.mediaGeneration++;
    cancelMediaLoad?.(); cancelMediaLoad=null;
    stopPreviewMeter(); expireResult();
    state.media?.getTracks().forEach(track => track.stop()); state.media = null;
    if (!mounted) return;
    const video = $('#sd-video'); video.pause(); video.srcObject = null; video.removeAttribute('src'); video.load(); video.hidden = true;
    $('#sd-image').hidden = true; $('#sd-image').removeAttribute('src'); $('#sd-overlay').hidden = true;
    if (state.sourceURL) URL.revokeObjectURL(state.sourceURL);
    state.sourceURL = null; state.ready = false; $('#sd-file').value = ''; $('#sd-source-status').textContent = '等待输入';
  }
  async function camera() {
    if (!W.session) {await W.login(); return;}
    if (state.running || state.stream) return;
    clearMedia(); const generation = state.mediaGeneration;
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('摄像头需要本机地址或HTTPS');
    const media = await navigator.mediaDevices.getUserMedia({video:{width:{ideal:1920}, height:{ideal:1080}}, audio:false});
    if (!state.active || generation !== state.mediaGeneration) {media.getTracks().forEach(track => track.stop()); return;}
    state.media = media; const video = $('#sd-video'); video.srcObject = media; video.controls = false;
    try {
      await video.play();
      if (generation === state.mediaGeneration && !validPicture(video)) throw new Error('摄像头未提供有效画面，请检查设备后重新连接');
    } catch (error) {
      if (generation !== state.mediaGeneration) return;
      clearMedia(); throw error;
    }
    if (generation !== state.mediaGeneration) return;
    state.ready = true; video.hidden = false; $('#sd-source-status').textContent = '摄像头已连接';
    startPreviewMeter(video);
    for (const event of ['mute','unmute']) media.getVideoTracks()[0].addEventListener(event, () => {
      if (state.media===media) invalidateInput();
    });
    media.getVideoTracks()[0].addEventListener('ended', () => {
      if (state.media !== media) return;
      state.ready = false; state.running = false; haltSampling();
      state.error = '摄像头已断开，请停止本轮后重新连接'; say(state.error); render();
    });
    render();
  }
  async function loadFile(file) {
    if (!file || occupied()) return;
    if (file.size > 500 * 1024 * 1024) throw new Error('素材请控制在500MB以内');
    clearMedia(); const generation = state.mediaGeneration, replay = state.source === 'replay';
    state.sourceURL = URL.createObjectURL(file);
    const media = $(replay ? '#sd-video' : '#sd-image');
    state.error=''; clearResults(); $('#sd-source-status').textContent='正在读取 '+file.name+'…'; render();
    try {
      const loaded = await new Promise((resolve, reject) => {
        const event=replay ? 'loadeddata' : 'load'; let timer;
        const cleanup=()=>{
          clearTimeout(timer);media.removeEventListener(event,ready);media.removeEventListener('error',failed);
          if (cancelMediaLoad===cancel) cancelMediaLoad=null;
        };
        const cancel=()=>{cleanup();resolve(false);};
        const failed=()=>{cleanup();reject(new Error(replay ? videoFormatHelp : '图片无法读取，请使用有效的 JPEG 或 PNG 图片'));};
        const ready=()=>{if(!validPicture(media)){failed();return;}cleanup();resolve(true);};
        cancelMediaLoad=cancel;
        media.addEventListener(event,ready);media.addEventListener('error',failed);
        timer=setTimeout(()=>{cleanup();reject(new Error(replay ? '视频加载超时。'+videoFormatHelp : '图片加载超时，请重新选择图片'));},30000);
        media.src = state.sourceURL; if (replay) {media.controls = true; media.load();}
      });
      if (!loaded) return;
    } catch (error) {
      if (generation !== state.mediaGeneration || !state.active) return;
      clearMedia(); $('#sd-source-status').textContent=error.message; throw error;
    }
    if (generation !== state.mediaGeneration || !state.active) return;
    state.ready = true; media.hidden = false; clearResults();
    if (replay) startPreviewMeter(media);
    $('#sd-source-status').textContent = file.name; render();
  }
  function startBlocked() {
    if (!state.active) return '工位尚未就绪';
    if (!W.session) return '请先登录服务器';
    if (state.busy) return '当前操作尚未完成';
    if (state.running) return '本轮检测中，重复触发已锁定';
    if (state.stream || state.pendingStart) return '请先结束或停止上一轮检测';
    if (!state.connected) return '请先刷新工位配置';
    if (!state.binding || state.binding.analysisType === 'manual') return '请打开“工位配置”选择检测模型并保存';
    if (configurationDirty()) return '检测配置有未保存修改，请先保存或取消修改';
    if (!state.binding.enabled) return '该工位设备已停用';
    if (state.source === 'device') {
      if (!collector) return '后台采集控制未载入，请刷新页面';
      if (remotePending()) return '原控制请求待核对，请等待采集器回执';
      if (!state.collector?.online || state.transportLost) return '请先连接后台采集器并核对状态';
      if (state.collector.state === 'ERROR') return '后台采集器尚未恢复就绪，请先检查设备';
      return '';
    }
    if (state.source !== 'camera' && !state.ready) return '请先选择检测视频或图片';
    if (state.ready && !validPicture($(state.source==='image' ? '#sd-image' : '#sd-video')))
      return state.source==='camera' ? '摄像头未提供有效画面，请重新连接' : state.source==='image' ? '图片没有有效画面，请重新选择' : videoFormatHelp;
    return '';
  }
  async function start() {
    const reason = startBlocked(); if (reason) throw new Error(reason);
    if (state.source === 'device') return collectorCommand('start');
    const generation = state.generation, revision = state.binding.revision;
    state.busy = true; state.error = ''; render();
    try {
      if (state.source === 'camera' && !state.ready) await camera();
      if (!state.ready) throw new Error('画面尚未就绪');
      if (state.source === 'replay') {
        const video = $('#sd-video'); video.pause();
        if (video.currentTime !== 0) await new Promise(resolve => {video.addEventListener('seeked', resolve, {once:true}); video.currentTime = 0;});
      }
      if (state.binding.analysisType === 'sticker') {
        if (state.preparation?.status !== 'ready') say('正在准备贴条模型，请稍候…');
        await prepareModel();
        if (!state.active || generation !== state.generation || revision !== state.binding?.revision || !W.session) return;
      }
      if (!state.active || generation !== state.generation || revision !== state.binding?.revision || !W.session) return;
      if (!validPicture($(state.source==='image' ? '#sd-image' : '#sd-video'))) throw new Error('画面已失效，请重新连接摄像头或选择素材');
      state.pendingStart = {requestId:crypto.randomUUID(), inputSource:state.source};
      state.stream = await json('/station-streams?station=' + state.station, 'POST', state.pendingStart);
      state.pendingStart = null;
      state.frames = 0; state.frame = state.final = null; state.archived = false; state.latency = state.timing = null; state.lastAt = 0;
      resetMeasurements(); state.inputGeneration++;
      state.started = performance.now(); state.lastMediaTime = null; state.sequence = 0; state.retryAt = 0; state.transportLost = false; state.running = true;
      clearInterval(state.freshnessTimer); state.freshnessTimer = setInterval(() => {
        if (!state.running) return;
        const maximum = state.stream?.streamLimits?.maxDurationSeconds ?? 600;
        if (!state.busy && performance.now() - state.started >= maximum * 1000) {
          void attempt(() => stop({reason:`本轮检测已达到最长 ${maximum} 秒，已自动停止；请确认工件后重新开始`}));
        } else render();
      }, 500);
      if (state.source === 'replay') {$('#sd-video').controls = false; await $('#sd-video').play();}
      say(`正在检测 · 连续 ${state.stream.streamLimits?.noTargetTimeoutSeconds ?? 60} 秒无目标停止 · 本轮最长 ${state.stream.streamLimits?.maxDurationSeconds ?? 600} 秒`);
      void tick(state.generation);
    } catch (error) {
      state.running = false; haltSampling();
      if (error.status >= 400 && error.status < 500) state.pendingStart = null;
      state.error = error.message; throw error;
    } finally {state.busy = false; render();}
  }
  async function tick(generation) {
    if (!state.active || !state.running || generation !== state.generation || state.sampling) return;
    const sampledAt = performance.now(), media = $(state.source === 'image' ? '#sd-image' : '#sd-video');
    if (!inputAvailable(media) || state.source !== 'image' && state.lastMediaTime === media.currentTime) {
      if (!inputAvailable(media)) clearQueuedSample();
      state.timer = setTimeout(() => tick(generation), 100); return;
    }
    // Retain at most one waiting sample, including a sample being encoded.
    clearQueuedSample('replaced');
    state.lastMediaTime = media.currentTime;
    const identity = state.stream, inputGeneration=state.inputGeneration, seq = state.sequence++, stamp = state.source === 'replay' ? media.currentTime : (sampledAt - state.started) / 1000;
    count('captured');
    let retry = 0;
    const work = (async () => {
      const w = media.videoWidth || media.naturalWidth, h = media.videoHeight || media.naturalHeight;
      const scale = Math.min(1, 1920 / Math.max(w,h), Math.sqrt(1920*1080/(w*h)));
      const width = Math.max(1, Math.floor(w*scale)), height = Math.max(1, Math.floor(h*scale));
      if (capture.width !== width) capture.width = width;
      if (capture.height !== height) capture.height = height;
      // Preserve the cleared input boundary for transparent image fixtures too.
      captureContext.clearRect(0, 0, capture.width, capture.height);
      captureContext.drawImage(media, 0, 0, capture.width, capture.height);
      const drawnAt = performance.now();
      const sample = {...await encodeSample(),identity,inputGeneration,seq,stamp,sampledAt,drawnAt,encodedAt:performance.now()};
      if (!sample.blob || sample.blob.size > 1048576) {discardSample(sample); throw new Error('画面编码失败或采样过大');}
      if (!currentRun(generation,identity) || inputGeneration!==state.inputGeneration || !inputAvailable(media)) {count('discarded'); discardSample(sample); return;}
      state.queued=sample;
      void sendLatest(generation);
    })();
    state.sampling = work;
    try {await work;}
    catch (error) {
      count('discarded');
      if (currentRun(generation,identity) && inputGeneration===state.inputGeneration) {
        state.error=error.message; $('#sd-overlay').hidden=true; say(error.message); render(); retry=500;
      }
    } finally {if (state.sampling===work) state.sampling=null;}
    if (currentRun(generation,identity)) state.timer=setTimeout(()=>tick(generation), Math.max(retry,1000/state.binding.sampleFps-(performance.now()-sampledAt),0));
  }
  async function sendLatest(generation) {
    if (!state.active || !state.running || generation!==state.generation || state.pending || !state.queued) return;
    const media=$(state.source==='image' ? '#sd-image' : '#sd-video');
    if (!inputAvailable(media)) {clearQueuedSample(); return;}
    if (performance.now()<state.retryAt) {
      clearTimeout(state.sendTimer);
      state.sendTimer=setTimeout(()=>sendLatest(generation),Math.max(0,state.retryAt-performance.now())); return;
    }
    const sample=state.queued; state.queued=null;
    // A slow response never causes a backlog of old pictures to be replayed.
    if (sample.identity!==state.stream || sample.inputGeneration!==state.inputGeneration) {count('discarded'); discardSample(sample); return;}
    if (performance.now()-sample.sampledAt>sampleMaxAge) {count('expired'); discardSample(sample); return;}
    const {identity,inputGeneration,seq,stamp,blob,sampledAt,drawnAt,encodedAt}=sample;
    let complete=false, autoStopReason='';
    const work=(async()=>{
      const requestAt = performance.now();
      state.lastSubmittedSeq=seq;
      count('submitted');
      const result = await W.request('/station-streams/' + identity.streamId + '/frames', {method:'POST',body:blob,timeout:20000,
        headers:{'Content-Type':'image/jpeg','X-Frame-Seq':String(seq),'X-Frame-Time':String(stamp),
          'X-Stream-Epoch':String(identity.streamEpoch),'X-Station-Id':identity.stationId,'X-Camera-Id':identity.cameraId}});
      if (generation !== state.generation || identity !== state.stream || !state.active) return;
      if (!matchesResult(result,identity) || result.frameSeq !== seq) {
        const error = new Error('检测结果工位或帧身份不匹配，已停止采样'); error.status = 409; throw error;
      }
      state.frame = result; state.frames++; state.lastAt = performance.now(); state.latency = state.lastAt - sampledAt;
      if (!state.stoppedAt) {
        state.completedTimes.push(state.lastAt);
        state.completedTimes=state.completedTimes.filter(at=>at>state.lastAt-10000).slice(-512);
      }
      state.timing = {drawMs:drawnAt-sampledAt,encodeMs:encodedAt-drawnAt,queueMs:requestAt-encodedAt,
        requestMs:state.lastAt-requestAt,serverMs:result.serverProcessingMs};
      if (state.latency>=resultMaxAge) count('staleResults');
      // Drained responses remain available for archive, but cannot restore a
      // verdict after stop, loss of input, or a hidden/offline page transition.
      if (!state.running || inputGeneration!==state.inputGeneration || !inputAvailable(media)) return;
      state.error = ''; expireResult();
      state.resultDeadline=sampledAt+resultMaxAge;
      if (state.resultDeadline>performance.now()) {
        state.resultExpiryTimer=setTimeout(()=>{expireResult(); render();},state.resultDeadline-performance.now());
        if (state.previewMode==='detection' || state.source==='image') draw(result,sample.image);
        // Visual coverage ends as soon as the server confirms every site,
        // including a camera demonstration replay. Temporal replays wait to end.
        complete = (state.source !== 'replay' || visualCoverageComplete(result)) && ['PASS','FAIL'].includes(result.compliance?.status)
          && state.binding.analysisType !== 'pose';
      }
      render();
    })();
    state.pending = work;
    try {await work;}
    catch (error) {
      count('failed');
      if (currentRun(generation,identity)) {
        state.error = error.message; expireResult();
        if (!error.status || error.status >= 500 || error.status === 408) {loseTransport(); void recoverTransport();}
        if (error.status === 410) {autoStopReason=error.message; state.running=false; haltSampling();}
        if ([401,403,404,409,422].includes(error.status)) {state.running = false; haltSampling();}
        state.retryAt=performance.now()+Math.max(500,Math.min(3000,error.retryAfterMs || 500)); say(error.message); render();
      }
    } finally {discardSample(sample); if (state.pending === work) state.pending = null;}
    if (autoStopReason && generation === state.generation && identity === state.stream) {
      await attempt(() => stop({reason:autoStopReason})); return;
    }
    // Finish after clearing the in-flight frame, so finish never awaits itself.
    // Only the server verdict can pass; confirmed counts cannot bypass evidence gaps.
    if (complete && liveResult() && generation === state.generation && inputGeneration===state.inputGeneration) {await attempt(finish); return;}
    if (currentRun(generation,identity)) void sendLatest(generation);
  }
  async function finish({manual=false}={}) {
    if (state.source === 'device') return collectorCommand('finish',{manual});
    if (state.busy || !state.stream) return;
    state.busy = true; state.running = false; haltSampling(); clearInterval(state.freshnessTimer);
    state.timer = state.freshnessTimer = null; $('#sd-video').pause();
    const media = state.media; state.media = null;
    if (media) {
      stopPreviewMeter(); media.getTracks().forEach(track => track.stop()); $('#sd-video').srcObject = null; state.ready = false;
      $('#sd-source-status').textContent = '摄像头已关闭';
    }
    say('本轮采样已停止，正在归档'); render();
    try {
      await Promise.allSettled([state.pending,state.sampling]);
      if (!state.archived) {
        const result = await W.request('/station-streams/' + state.stream.streamId + '/finish', {method:'POST',timeout:20000});
        if (!matchesResult(result,state.stream) || !result.final || !result.archived || !Number.isSafeInteger(result.frameSeq) ||
            result.frameSeq<(state.frame?.frameSeq ?? 0) || result.frameSeq>state.lastSubmittedSeq)
          throw new Error('归档响应身份或保存状态不匹配，请重试结束并归档');
        state.final = result;
      }
      state.archived = true;
      await json('/station-streams/' + state.stream.streamId, 'DELETE', {reason:'当前界面检测已结束并归档'});
      state.stream = null; state.error = ''; say('本轮检测已归档 · 可按 F8 检测下一个工件');
    } catch (error) {
      throw new Error((state.archived ? '本轮已归档，资源释放失败' : '本轮采样已停止，归档失败') + '：' + error.message + '。请点击“结束并归档”重试。');
    } finally {state.busy = false; if (state.source === 'replay') $('#sd-video').controls = true; render();}
    if (manual && canReview(state.final)) showManualReview(state.final);
    else showOutcome(state.final);
  }
  function showOutcome(result) {
    if ((result.analysisType || state.binding?.analysisType) === 'pose') return;
    const verdict = assessment(result), passed = verdict?.status === 'PASS', pending = canReview(result);
    const autoDismiss = ['PASS','FAIL'].includes(verdict?.status);
    const title = result.manualReview?.label || (passed ? '检测合规' : verdict?.status === 'FAIL' ? '检测不合规' : '检测证据不足');
    window.showModal(state.config.name + ' · ' + title,
      `<div class="screw-final-card" data-verdict="${E(verdict?.status || 'REVIEW')}" role="${passed?'status':'alert'}">
      <h2>${E(title)}</h2><p>${E(verdict?.reason || (passed ? '本轮检测已确认合规。' : '请核对本轮检测结果。'))}</p></div>
      ${samplingGapDetail(result)?`<p class="sd-evidence-gap">${E(samplingGapDetail(result))}</p>`:''}
      <p>${state.source === 'device' ? '本轮检测已停止并归档，后台采集器等待下一件。' : '本轮检测已停止并归档，摄像头和采样资源已释放。'}${pending?'可人工复核后选择合规或不合规，原因选填。':'可更换工件后开始下一轮。'}</p>
      ${autoDismiss?'<p class="hint">3 秒后自动关闭，也可按 F8 关闭。</p>':''}
      ${reviewSummary(result)}<div class="modal-actions"><button class="btn" data-action="close-modal"${autoDismiss?' aria-keyshortcuts="F8"':''}>${pending?'稍后处理':'知道了'}</button>
      ${pending?'<button class="btn btn-primary" id="sd-modal-review">人工确认</button>':'<button class="btn btn-primary" id="sd-modal-next">检测下一个工件</button>'}</div>`,
      autoDismiss ? {autoCloseMs:3000,dismissKey:'F8'} : {});
    if (pending) $('#sd-modal-review').onclick = () => showManualReview(result);
    else $('#sd-modal-next').onclick = () => {window.closeModal(); attempt(start);};
  }
  async function showManualReview(result) {
    if (!canReview(result) || !result.archived || state.busy) return;
    const generation = state.generation;
    let existing, storage;
    try { existing = await reviews.get(result.streamId); storage = await reviews.storage(); }
    catch (error) { say('无法持久保存人工确认：' + error.message); return; }
    if (generation !== state.generation || !state.active) return;
    if (existing) { showReviewEntry(existing); return; }
    const targets = result.aggregateTargets || result.historyTargets || result.targets || [];
    const missing = targets.filter(t=>!['PRESENT','ACTION_DONE'].includes(t.state));
    window.showModal(state.config.name + ' · 人工确认', `<form id="sd-review-form">
      <p>算法原判定：${E(result.compliance.label || result.compliance.status)}</p><p>${E(result.compliance.reason || '')}</p>
      ${missing.length?'<p>待核对位置：'+missing.map(t=>E(t.name || t.id)).join('、')+'</p>':''}
      <p>请现场复核后选择结论。原始算法结果和检测证据会保留。</p>
      <p class="hint">${E(storage.message)}</p>
      <label for="sd-review-outcome">人工结论</label><select id="sd-review-outcome" class="field" required>
      <option value="">请选择</option><option value="PASS">合规</option><option value="FAIL">不合规</option></select>
      <label for="sd-review-reason">确认原因（选填）</label><textarea id="sd-review-reason" class="field" maxlength="1000" placeholder="可填写现场复核说明"></textarea>
      <p id="sd-review-error" role="alert"></p><div class="modal-actions"><button class="btn" type="button" data-action="close-modal">取消</button>
      <button class="btn btn-primary" id="sd-review-submit" type="submit">保存人工确认</button></div></form>`);
    const form = $('#sd-review-form'), submit = $('#sd-review-submit'), error = $('#sd-review-error');
    form.onsubmit = async event => {
      event.preventDefault(); if (state.busy || !form.reportValidity()) return;
      state.busy = true; submit.disabled = true; error.textContent = ''; render();
      let entry;
      try {
        entry = await reviews.enqueue({stationId:result.stationId, streamId:result.streamId, workstationId:result.workstationId,
          outcome:$('#sd-review-outcome').value, reason:$('#sd-review-reason').value});
        if (generation === state.generation && state.active && form.isConnected) showReviewEntry(entry);
        await reviews.reconcile(entry.streamId);
      } catch (failure) {error.textContent = '未能完成本地保存或核对：' + failure.message; say(error.textContent);}
      finally {state.busy = false; submit.disabled = false; render();}
    };
  }
  function showReviewEntry(entry) {
    if (entry.status === 'complete' && entry.record?.result) { showOutcome(entry.record.result); return; }
    const saved = entry.record?.result, actual = saved?.manualReview || saved?.compliance;
    window.showModal(state.config.name + ' · 人工确认核对', `<div id="sd-review-pending" data-stream="${E(entry.streamId)}">
      <p id="sd-review-error" role="status">${E(entry.message)}</p>
      <p>检测轮次：${E(entry.streamId)}<br>请求编号：${E(entry.body.requestId)}</p>
      <p>本次结论：${entry.body.outcome === 'PASS' ? '合规' : '不合规'} · 确认身份：${E(entry.actor.reviewer)}<br>原因：${E(entry.body.reason || '未填写')}</p>
      ${entry.status === 'blocked' ? `<p>服务器实际结果：${E(actual?.label || entry.record?.status || '暂时无法读取，请恢复权限后核对')}</p>${reviewSummary(saved)}` : ''}
      <p>本地记录已保留。待核对期间使用原请求编号和内容，可关闭页面后继续核对。</p>
      <div class="modal-actions"><button class="btn" data-action="close-modal">关闭</button>
      <button class="btn btn-primary" id="sd-review-submit" ${entry.status==='submitting'?'disabled':''}>${entry.status==='blocked'?'刷新服务器结果':'立即核对'}</button></div></div>`);
    $('#sd-review-submit').onclick = () => { $('#sd-review-submit').disabled = true; void reviews.reconcile(entry.streamId).catch(error => say(error.message)); };
  }
  async function refreshReviewQueue() {
    if (!state.active || !mounted) return;
    const station = state.station;
    try {
      const entries = await reviews.list({pendingOnly:true, stationId:station});
      if (!state.active || station !== state.station) return;
      $('#sd-outbox').hidden = !entries.length; $('#sd-outbox').parentElement.hidden = !entries.length;
      set('#sd-outbox-note', `人工确认：${entries.filter(entry=>entry.status!=='blocked').length} 条待核对，${entries.filter(entry=>entry.status==='blocked').length} 条已停止自动重试`);
    } catch (error) { say('本地确认记录读取失败：' + error.message); }
  }
  async function showReviewQueue() {
    const station = state.station;
    const entries = await reviews.list({pendingOnly:true, stationId:station});
    if (!state.active || station !== state.station) return;
    window.showModal(state.config.name + ' · 人工确认记录', entries.map((entry, i) => `<p>${E(entry.streamId)} · ${E(entry.message)} <button data-review-entry="${i}">查看并核对</button></p>`).join('') || '<p>没有待核对的人工确认。</p>');
    document.querySelectorAll('[data-review-entry]').forEach(button => { button.onclick = () => showReviewEntry(entries[Number(button.dataset.reviewEntry)]); });
  }
  document.addEventListener('review-outbox', event => {
    const entry = event.detail;
    if (!state.active || state.station !== entry.stationId) return;
    if (entry.record?.result && state.final?.streamId === entry.streamId) state.final = entry.record.result;
    if ($('#modal')?.open && $('#sd-review-pending')?.dataset.stream === entry.streamId) showReviewEntry(entry);
    void refreshReviewQueue(); render();
  });
  document.addEventListener('review-outbox-error', event => { if (state.active) say('本地确认核对暂停：' + event.detail); });
  document.addEventListener('inspection-data-reset', event => {
    if (!state.active) return;
    const ids = new Set(event.detail.streamIds);
    if (!state.stream && ids.has(state.final?.streamId)) {
      state.final = null; state.frame = null; state.frames = 0; resetMeasurements();
      if ($('#modal')?.open && document.querySelector('#sd-review-form, #sd-review-pending, [data-sd-download]')) window.closeModal();
      say('检测记录已清空，可开始新一轮测试。'); render();
    }
    void refreshReviewQueue();
  });
  function stopBlocked() {
    if (!state.active) return '工位尚未就绪';
    if (state.busy) return '当前操作尚未完成';
    if (state.source === 'device' && remotePending()) return '原控制请求待核对，请等待采集器回执';
    if (!state.stream && !state.pendingStart && !state.media) return '当前没有运行中的任务';
    return '';
  }
  async function stop({reason=''}={}) {
    if (state.source === 'device') return collectorCommand('stop',{reason:reason || $('#sd-stop-reason').value});
    if (state.busy) return;
    state.busy = true; state.running = false; haltSampling(); clearInterval(state.freshnessTimer); $('#sd-video').pause(); render();
    clearMedia();
    try {
      await Promise.allSettled([state.pending,state.sampling]);
      if (!state.stream && state.pendingStart) state.stream = await json('/station-streams?station=' + state.station,'POST',state.pendingStart);
      if (state.stream) await json('/station-streams/' + state.stream.streamId, 'DELETE', {reason:reason || '操作员在检测首页停止本轮'});
      state.stream = state.pendingStart = null; state.error = ''; if (!state.archived) state.final = null;
      clearMedia(); say(reason || (state.archived ? '本轮检测已归档 · 可按 F8 开始下一轮' : '本轮已停止，未自动判定通过'));
    } finally {state.busy = false; render();}
  }
  function draw(result, image) {
    const canvas = $('#sd-overlay');
    if (canvas.width !== image.width) canvas.width = image.width;
    if (canvas.height !== image.height) canvas.height = image.height;
    const ctx = overlayContext ||= canvas.getContext('2d');
    // Always remove the previous picture and annotations, including transparent
    // regions. This retained picture belongs to this response's frame identity.
    ctx.clearRect(0,0,canvas.width,canvas.height); ctx.beginPath();
    ctx.drawImage(image,0,0); ctx.lineWidth = Math.max(2,canvas.width/600); ctx.font = `${Math.max(16,canvas.width/90)}px sans-serif`;
    if (result.skeleton) window.adoptSkeletonTopology?.(result.skeleton);
    for (const person of result.people || []) {
      const box = person.bbox;
      if (box?.length === 4) {ctx.strokeStyle='#49c5ff';ctx.strokeRect(box[0]*canvas.width,box[1]*canvas.height,(box[2]-box[0])*canvas.width,(box[3]-box[1])*canvas.height);}
      if (person.keypoints) window.drawPoseSkeleton?.(ctx,person.keypoints,0,0,canvas.width,canvas.height,person.skeletonOverlay);
    }
    const targets = result.targets || result.inspection?.targets || [];
    for (const target of targets) {
      const box = target.bbox; if (!Array.isArray(box) || box.length !== 4) continue;
      const normalized = box.every(value => value >= 0 && value <= 1), sx = normalized ? canvas.width : canvas.width/(result.width || canvas.width), sy = normalized ? canvas.height : canvas.height/(result.height || canvas.height);
      ctx.strokeStyle = ctx.fillStyle = target.state === 'PRESENT' ? '#39d9a0' : '#ffba63';
      ctx.strokeRect(box[0]*sx,box[1]*sy,(box[2]-box[0])*sx,(box[3]-box[1])*sy);
      ctx.fillText(String(target.id || ''),box[0]*sx,Math.max(20,box[1]*sy-5));
    }
    canvas.hidden = false;
  }
  function render() {
    if (!state.active || !mounted) return;
    set('#sd-config-title', state.config.name + ' · 工位配置');
    const busy = occupied(), binding = state.binding, result = state.final || state.frame;
    const stale = state.running && !liveResult();
    const targets = result?.aggregateTargets || result?.historyTargets || result?.targets || state.configuredTargets;
    const confirmed = targets.filter(t => ['PRESENT','ACTION_DONE'].includes(t.state)).length;
    const statusLabel = target => !result ? '待检测' :
      ({PRESENT:'已确认',EMPTY:binding?.analysisType==='sticker'?'明确未贴':'可见空孔',WRONG:'形状不符',ACTION_DONE:'动作已确认',UNKNOWN:'待确认'}[target.state] || target.state);
    const observedVerdict = assessment(state.final) || (!stale && state.running && !state.error ? state.frame?.compliance : null);
    const replayPending = state.source === 'replay' && state.running && !state.final && !visualCoverageComplete(result);
    const verdict = replayPending && observedVerdict ? {...observedVerdict, status:'PENDING', label:'检测中'} : observedVerdict;
    const label = verdict?.label || (state.transportLost ? '无法检测' : state.running ? stale ? '等待新结果' : '检测中' : state.stream ? '待结束或重试' : '待开始检测');
    for (const id of ['sd-source','sd-file','sd-camera','sd-refresh','sd-model','sd-fps','sd-scene','sd-edit','sd-save','sd-cancel-edit']) $('#'+id).disabled = busy;
    $('#sd-start').disabled = !!startBlocked(); $('#sd-finish').disabled = state.busy || !state.stream ||
      (state.source === 'device' ? remotePending() : !state.frames);
    const complete = state.archived && !state.stream && !!state.final && binding?.analysisType !== 'pose';
    $('#sd-completion').hidden = !complete;
    $('#sd-completion').parentElement.hidden = !complete;
    $('#sd-completion').dataset.verdict = verdict?.status || 'REVIEW';
    set('#sd-completion strong',state.final?.manualReview?.label || (verdict?.status==='PASS'?'检测合规':verdict?.status==='FAIL'?'检测不合规':'检测证据不足'));
    set('#sd-completion p',state.final?.manualReview ? `人工确认已保存 · ${state.final.manualReview.reviewer} · ${new Date(state.final.manualReview.reviewedAt*1000).toLocaleString()}` :
      ['本轮已停止并归档'+(canReview(state.final)?'，请人工复核。':'，可更换工件。'),verdict?.status!=='PASS'?verdict?.reason:'',samplingGapDetail(state.final)].filter(Boolean).join(' '));
    $('#sd-next').hidden = !!canReview(state.final);
    $('#sd-next').disabled = !!startBlocked();
    set('#sd-start', complete ? '检测下一个工件（F8）' : '开始检测（F8）');
    $('#sd-review-panel').hidden = !canReview(result) || (!state.final && !state.running);
    $('#sd-review-panel').parentElement.hidden = $('#sd-review-panel').hidden;
    $('#sd-review').disabled = state.busy || state.source === 'device' && remotePending() || !!state.stream && !state.frames;
    set('#sd-review-note',state.final ? '本轮待复核，请确认结果' : '工件完成后，可结束并确认');
    $('#sd-review').title = state.final ? '人工核对本轮结果，选择合规或不合规' : '结束本轮检测并人工确认；当前动作证据仍在累计';
    $('#sd-stop').disabled = !!stopBlocked();
    $('#sd-login').hidden = !!W.session;
    $('#sd-setup').hidden = !configuring();
    $('#sd-edit').hidden = !W.session || !state.connected || W.session.role === 'terminal' || !binding || binding.analysisType === 'manual' || state.editing;
    $('#sd-cancel-edit').hidden = !state.editing;
    const sceneSelected = $('#sd-model').value === 'scene';
    $('#sd-scene-field').hidden = !sceneSelected; $('#sd-scene').required = sceneSelected; $('#sd-scene').disabled = busy || !sceneSelected;
    $('#sd-scene-note').hidden = !sceneSelected;
    set('#sd-scene-note', state.scenesError || (state.scenes.some(scene => scene.ready) ? '使用所选场景关联的模型与检测规则。' : '暂无可用场景，请先在模型与场景管理中配置并发布。'));
    set('#sd-config-note', busy ? '请先结束或停止本轮检测，再修改配置。' : configurationDirty() ? '有未保存修改；保存后下轮检测使用新配置。' : '当前配置已载入，可选择模型后保存。');
    const remote = state.source === 'device';
    $('#sd-source').value = state.source; $('#sd-camera').hidden = state.source !== 'camera'; $('#sd-file-label').hidden = !['image','replay'].includes(state.source);
    $('#sd-preview-label').hidden = state.source==='image' || remote; $('#sd-preview-mode').value=state.previewMode;
    $('#sd-collector-controls').hidden = !remote;
    $('#sd-command-check').disabled = state.busy;
    $('#sd-command-retry').hidden = !remote || !collector?.pending(state.collectorCommand) || !state.collectorCommand.missing;
    $('#sd-command-retry').disabled = state.busy;
    $('#sd-stop-reason').disabled = state.busy || remotePending();
    const collectorLabels = {IDLE:'等待开始下一件',RUNNING:'本件检测中',FINISHING:'正在结束并归档',STOPPING:'正在中断本件',OFFLINE:'采集器离线',ERROR:'采集异常，需复核'};
    set('#sd-collector-note', [collectorLabels[state.collector?.state] || '等待后台采集状态',
      state.collectorCommand?.message || state.collector?.command?.error || '',
      '页面刷新、关闭或切换工位不停止后台采集。每件结束后请归档，异常请选择中断原因。'].filter(Boolean).join(' · '));
    $('#sd-file').accept = state.source === 'image' ? 'image/jpeg,image/png' : 'video/*,.mp4,.webm,.mov';
    $('#sd-empty').hidden = state.ready;
    set('#sd-empty', remote ? '等待后台采集器的有效检测画面' : '开启摄像头或选择素材，然后按 F8 开始检测');
    if (stale || !state.running || state.error || state.previewMode==='live' && state.source!=='image') $('#sd-overlay').hidden = true;
    // Keep the decode source running beneath an opaque surface. Switching back
    // to its newer picture whenever a result expires makes the preview jump.
    const detectionPreview = (remote || state.previewMode==='detection' || state.source==='image') &&
      (state.running || !!state.stream || state.busy && state.ready);
    $('.station-detection-media').dataset.preview = detectionPreview ? 'detection' : 'source';
    $('#sd-preview-wait').hidden = !detectionPreview || !$('#sd-overlay').hidden;
    set('#sd-preview-wait',state.error ? '检测画面暂不可用，等待采样恢复…' : state.frames ? '等待新的检测画面…' : '等待首帧检测结果…');
    const modelName = binding?.analysisType === 'scene' ? state.scenes.find(scene => scene.id === binding.sceneId)?.name || names.scene : names[binding?.analysisType];
    set('#sd-binding', binding ? `${state.config.name} · ${modelName || binding.analysisType} · 目标 ${binding.sampleFps} fps · ${binding.cameraId}${binding.enabled ? '' : ' · 设备停用'}` : '本工位尚未配置检测模型');
    set('.brand p', '按工位独立检测 · 无需工件登记'); set('.cpu-chip', 'AI检测 · ' + (names[binding?.analysisType] || '待配置模型'));
    set('.check-chip', state.config.name + ' · ' + label); set('.camera-name', state.config.name + ' · ' + (names[binding?.analysisType] || '选择检测模型'));
    set('.stream-info', state.source==='image' ? '图片联调 · 检测框对应当前采样' : state.previewMode==='live'
      ? '实时视频 · 判定来自最近有效采样' : '检测采样预览 · 识别框对应同一帧');
    set('#run-state', label); set('#mode-text', state.config.name + ' · ' + label); set('.live', state.running ? '● 检测中' : '已暂停');
    const fps=effectiveFps(), videoFps=previewFps();
    set('#metric-fps', fps===null ? '—' : fps.toFixed(2));
    $('#metric-fps').parentElement.title = `近10秒成功返回的检测帧数 ÷ 窗口时长；不足10秒按本轮实际运行时间，停止后冻结。目标上限 ${binding?.sampleFps ?? '—'} fps，与视频预览帧率分别统计。`;
    $('#metric-latency').parentElement.title = remote ? '服务器收到采样至页面显示的保守结果年龄，包含状态查询与画面读取，不含相机曝光、驱动缓存及采集端编码。'
      : '最近一帧从浏览器采图到收到检测结果的耗时，包含编码、传输、排队和服务器处理；不含相机曝光及驱动缓存。';
    $('#metric-latency').previousElementSibling.textContent = remote ? '当前结果年龄' : '采样到响应';
    set('#sample-fps', $('#metric-fps').textContent); set('#metric-latency',state.latency == null ? '—' : Math.round(state.latency));
    set('#metric-latency + small','ms'); set('#metric-targets',binding?.analysisType === 'pose' ? result?.people?.length || 0 : confirmed);
    set('#metric-targets + small', binding?.analysisType === 'pose' ? '人' : '处'); set('#metric-compliance',label); set('#metric-compliance + small','');
    set('#current-process', binding?.analysisType === 'pose' ? '人体观察' : `${confirmed}/${targets.length} 处累计确认`);
    set('#video-compliance',label); set('#dwell',state.frames ? ((state.lastAt-state.started)/1000).toFixed(1)+' s' : '—');
    set('#workflow-name',state.config.name + ' · '+(names[binding?.analysisType] || '待配置检测模型'));
    set('#workflow-status',label); set('#workflow-description','');
    const verdictCode = verdict?.status || 'PENDING';
    $('#workflow-status').dataset.verdict = verdictCode;
    $('#metric-compliance').className = verdictCode === 'PASS' ? 'text-green' : verdictCode === 'FAIL' ? 'text-red' : 'text-yellow';
    set('#step-counter',targets.length ? `${confirmed}/${targets.length} 处` : '待采样');
    let stepIndex = 0;
    setHTML('#steps', targets.map(t => {
      const confirmed = ['PRESENT','ACTION_DONE'].includes(t.state);
      const index = t.preassembled ? '预' : binding?.analysisType === 'screw' ? ++stepIndex : t.id;
      return `<div class="step inspection-step${confirmed ? ' completed' : ''}" data-state="${E(t.state)}" data-target-id="${E(t.id)}">
        <div class="step-top"><span class="step-index">${E(index)}</span><span class="step-name">${E(t.name || t.id)}</span><span class="step-status">${E(statusLabel(t))}</span></div></div>`;
    }).join(''));
    setHTML('#detection-results', targets.map(t => `<tr><td>${E(t.id)}</td><td>${E(statusLabel(t))}</td><td>${t.score == null ? '—' : E(t.score)}</td><td>${E(t.name || '')}</td><td>${t.confirmedAt == null ? '—' : Number(t.confirmedAt).toFixed(2)+'s'}</td><td>${result ? '模型' : '配置'}</td></tr>`).join('') || '<tr><td colspan="6">'+(binding?.analysisType === 'pose' && result ? `当前采样检出 ${result.people?.length || 0} 人` : '等待本工位有效检测结果')+'</td></tr>');
    set('#result-note',state.previewMode==='live' && state.source!=='image'
      ? '实时视频不叠加识别框；判定来自最近有效采样，累计记录保留本轮已确认位置。'
      : '检测框与采样画面同步；累计记录保留本轮已确认位置。');
    set('#sd-archive',state.archived && state.final ? '本轮已归档 · 可查看工位检测记录' : state.running ? '本轮检测中 · 结束后归档' : state.stream ? state.busy ? '正在处理本轮 · 等待归档' : '本轮尚未归档 · 请结束或重试' : state.frames ? '本轮已停止 · 已记录中断' : '等待开始检测');
    set('#compliant-count',confirmed); set('#noncompliant-count',Math.max(0,targets.length-confirmed)); set('#compliance-rate',label);
    set('.compliance-foot',detectionScope());
    const alerts = [];
    if (state.error) alerts.push(state.error);
    if (observedVerdict?.status === 'FAIL') {
      const reason = replayPending && result?.sequence?.status === 'FAIL' ? result.sequence.reason : observedVerdict.reason;
      alerts.push((replayPending ? '过程异常提示（结束后统一判定）：' : '') +
        (reason || observedVerdict.label || '算法检测异常，请核对本轮结果'));
    }
    if (state.final && canReview(state.final)) alerts.push('本轮'+(state.final.compliance.label || '检测结果异常')+'，请人工确认');
    const query = $('#alert-search').value.trim(), shown = alerts.filter(text => text.includes(query));
    set('#metric-alerts',alerts.length); set('#realtime-alert',alerts.length ? '需处理' : '无');
    set('#alert-summary',alerts.length ? '待处理 '+alerts.length : '无异常'); set('#alert-count',shown.length+' 条');
    setHTML('#alert-list', shown.map(text => `<div class="alert-card">${E(text)}</div>`).join('') || '<div class="empty-alerts">'+(query ? '没有匹配的告警' : '暂无检测异常')+'</div>');
    set('#footer-performance',`视频预览 ${videoFps===null?'—':videoFps.toFixed(1)} fps · 有效检测 ${fps===null?'—':fps.toFixed(1)} fps · ${state.frames} 个采样`);
    $('#footer-performance').title='视频预览帧率来自浏览器呈现的视频帧计数，与检测请求分别统计；图片联调或浏览器不支持该计数时显示 —。';
    set('#server-account',W.session ? W.session.username + ' · 退出' : '登录服务器');
    $('#inspection-task').disabled = !remote && busy || !!window.AISOP_TERMINAL;
    window.InspectionTrigger?.refresh();
  }
  function download(value, filename) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
    const a = document.createElement('a'); a.href=url; a.download=filename; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function report() {
    const result=state.final || state.frame;
    const fresh=state.final || liveResult();
    const pendingReplay=state.source==='replay' && state.running && !state.final && !visualCoverageComplete(result);
    const targets=result?.aggregateTargets || result?.historyTargets || result?.targets || [];
    window.showModal(state.config.name+'检测报告', result ?
      `<p>打开时的检测快照 · ${state.archived ? '已归档' : '尚未归档'} · ${E(sources[state.source])}</p>
      <p>轮次：${E(result.streamId)}<br>模型：${E(names[state.binding?.analysisType] || state.binding?.analysisType)}<br>采样数量：${state.frames}</p>
      <p>结论：${E(fresh ? pendingReplay ? '检测中（视频结束后统一判定）' : assessment(result)?.label || '待判定' : '结果已过期或检测已停止')}<br>${E(fresh ? assessment(result)?.reason || '' : '历史采样不能作为当前检测结论。')}</p>
      ${result.manualReview?'<p>算法原判定：'+E(result.compliance?.label || '')+' · '+E(result.compliance?.reason || '')+'</p>'+reviewSummary(result):''}
      <p>${E(detectionScope())}</p><p>采样缺口：${(result.stationEvidenceGaps || []).length}</p>
      ${targets.map(t=>`<p>${E(t.id)} · ${E(t.name || '')} · ${E({PRESENT:'已确认',EMPTY:'可见空孔',ACTION_DONE:'动作已确认',UNKNOWN:'待确认'}[t.state] || t.state)}</p>`).join('')}
      <button id="sd-report-download">导出检测结果</button>` : '<p>暂无本轮检测结果。请准备画面后按 F8，或通过“查看工位检测记录”查询历史结果。</p>');
    if (result) $('#sd-report-download').onclick=()=>download({stationId:state.station,inputSource:state.source,result},state.station+'-detection.json');
  }
  async function system() {
    if (!W.session && !await W.login()) return;
    // Readiness deliberately returns 503 when the offline worker is absent.
    // Keep its diagnostic payload visible even when real-time detection works.
    const checks=await Promise.allSettled([
      fetch('/api/v1/ready',{signal:AbortSignal.timeout(8000)}).then(response=>response.json()),
      state.binding ? W.request('/stations/'+state.station+'/live-status') : null,
    ]);
    const ready=checks[0].status==='fulfilled' ? checks[0].value : {status:'无法连接',reason:checks[0].reason.message};
    const live=checks[1].status==='fulfilled' ? checks[1].value : {reason:checks[1].reason.message};
    const label=value=>({ready:'就绪',unavailable:'不可用'}[value] || value || '—');
    const milliseconds=value=>typeof value==='number' && Number.isFinite(value) && value>=0 ? value.toFixed(1)+' ms' : '—';
    const timing=state.timing, counters=state.counters, interval=state.binding?.sampleFps ? 1000/state.binding.sampleFps : null;
    const timingNote=!timing ? '本轮尚无有效采样，开始检测后可查看分段耗时。'
      : interval==null ? '以下为最近一帧的耗时，不能代表持续检测能力。'
      : '采图编码与上一帧上传重叠；最多一个在途请求和一张待送帧。结果有效期从浏览器采图起算，不含相机曝光与驱动缓存。';
    const isLive=live?.state==='LIVE', connectionIssue=checks[1].status==='rejected'
      || ['STALE','LEASE_EXPIRED'].includes(live?.reasonCode);
    const connectionTitle=checks[1].status==='rejected' ? '暂时无法获取状态' : ({LIVE:'正在采集',STALE:'等待恢复采样',
      WAITING_FRAME:'等待首帧',STOPPED:'采集已停止',NOT_STARTED:'等待开始检测',UNREGISTERED:'尚未登记设备',
      DISABLED:'工位已停用',MANUAL:'人工检查工位',LEASE_EXPIRED:'采集连接已失效',ARCHIVED:'本轮已归档',
      FINALIZING:'正在归档'}[live?.reasonCode] || (isLive?'正在采集':'等待开始检测'));
    const connectionDetail=live?.reason===connectionTitle ? '当前工位未在采集，可在检测页开始新一轮。' : live?.reason || '尚未配置工位模型';
    const startIssue=startBlocked(), gaps=live?.gapCount ?? null;
    const formatNumber=value=>typeof value==='number' && Number.isFinite(value) && value>=0 ? value.toFixed(1) : '—';
    const health=(name,value)=>`<div class="sd-runtime-service"><span>${E(name)}：</span><strong data-tone="${value==='ready'?'good':value==='unavailable'?'warn':'muted'}"><i data-icon="${value==='ready'?'check':'warning'}"></i>${E(label(value))}</strong></div>`;
    const metric=(name,value,unit,note,tone='')=>`<div class="sd-runtime-metric"${tone?` data-tone="${tone}"`:''}><dt>${E(name)}</dt><dd class="sd-runtime-metric-value">${E(value)}${unit?`<small>${E(unit)}</small>`:''}</dd><dd class="sd-runtime-metric-note">${E(note)}</dd></div>`;
    const stages=[['采图与缩放',timing?.drawMs],['JPEG 编码',timing?.encodeMs],['编码后待发送',timing?.queueMs],['请求往返',timing?.requestMs]];
    const snapshotTime=new Date().toLocaleTimeString('zh-CN',{hour12:false});
    window.showModal(state.config.name+'运行详情', `<div class="sd-runtime">
      <div class="sd-runtime-overview" data-tone="${isLive?'good':connectionIssue?'warn':'muted'}">
        <div class="sd-runtime-status-icon"><i data-icon="${connectionIssue?'warning':'server'}"></i></div>
        <div class="sd-runtime-connection"><span class="sd-runtime-caption">工位连接</span><h3>${E(connectionTitle)}</h3><p>${E(connectionDetail)}</p></div>
        <div class="sd-runtime-snapshot"><span>状态快照</span><time>${E(snapshotTime)}</time></div>
      </div>
      <div class="sd-runtime-services" aria-label="服务检查结果">${health('服务检查',ready.status)}${health('数据库',ready.database)}${health('离线分析服务',ready.workers?.status)}</div>
      ${ready.reason ? `<p class="sd-runtime-service-note"><i data-icon="warning"></i><span>${E(ready.reason)}</span></p>` : ''}
      <dl class="sd-runtime-metrics" aria-label="采样关键指标">
        ${metric('目标采样',state.binding?.sampleFps ?? '—','fps','目标间隔 '+milliseconds(interval),'accent')}
        ${metric('后端有效采样',formatNumber(live?.effectiveFps),'fps',`本轮已完成 ${state.frames} 个采样`)}
        ${metric('采样到响应',formatNumber(state.latency),'ms','最近一帧的完整响应耗时')}
        ${metric('采样缺口',gaps ?? '—','处',gaps==null?'暂未获取工位状态':gaps?'存在采样中断，请核对本轮':'本轮未记录采样缺口',gaps>0?'warn':'')}
      </dl>
      <p class="sd-runtime-note">视频预览 ${E(formatNumber(previewFps()))} fps · 本机有效检测 ${E(formatNumber(effectiveFps()))} fps · 预览统计不代表检测吞吐。</p>
      <div class="sd-runtime-details">
        <section class="sd-runtime-panel" aria-labelledby="sd-runtime-timing-title">
          <div class="sd-runtime-section-heading"><h3 id="sd-runtime-timing-title"><span>01</span>单帧耗时</h3><small>${timing?'最近一次有效采样':'等待首帧'}</small></div>
          <dl class="sd-runtime-timings">${stages.map(([name,value],index)=>`<div><dt><span>${String(index+1).padStart(2,'0')}</span>${E(name)}</dt><dd>${E(milliseconds(value))}</dd></div>`).join('')}</dl>
          <div class="sd-runtime-server"><span>其中服务器处理<small>已包含在请求往返内</small></span><strong>${E(milliseconds(timing?.serverMs))}</strong></div>
        </section>
        <section class="sd-runtime-panel sd-runtime-start" aria-labelledby="sd-runtime-start-title">
          <div class="sd-runtime-section-heading"><h3 id="sd-runtime-start-title"><span>02</span>检测准备</h3></div>
          <div class="sd-runtime-start-state" data-tone="${state.running?'good':startIssue?'warn':'muted'}"><i data-icon="${state.running?'play':startIssue?'warning':'check'}"></i><strong>${state.running?'本轮检测进行中':startIssue?'尚需完成准备':'可以开始检测'}</strong></div>
          <p>${E(state.running ? (isLive ? '检测已启动，采样结果会持续更新。' : '本轮仍在进行，请检查采样连接状态。') : startIssue || (state.ready ? '画面与配置已就绪，可按 F8 开始' : '可按 F8 开启摄像头'))}</p>
          ${!startIssue?'<div class="sd-runtime-shortcut"><kbd>F8</kbd><span>关闭弹窗后开始检测</span></div>':''}
          <div class="sd-runtime-device"><span>采集设备</span><strong>${E(state.binding?.cameraId || '尚未绑定')}</strong></div>
        </section>
      </div>
      <p class="sd-runtime-note"><i data-icon="report"></i><span>${E(timingNote)}</span></p>
      ${counters?`<p class="sd-runtime-note">本轮采图 ${counters.captured} · 提交 ${counters.submitted} · 待送替换 ${counters.replaced} · 发送前过期 ${counters.expired} · 暂停或停止丢弃 ${counters.discarded} · 请求失败 ${counters.failed} · 返回时已过期 ${counters.staleResults}</p>`:''}
      <div class="sd-runtime-footer"><span>打开时的状态快照 · 重新打开可更新</span><button class="btn" data-action="close-modal">完成</button></div>
    </div>`);
  }
  async function history(offset=0) {
    if (!W.session && !await W.login()) return;
    const station = state.station, title = state.config.name;
    const data = await W.request('/stations/'+station+'/inspections?limit=20&offset='+offset);
    if (station !== state.station || !state.active) return;
    const status = {RUNNING:'检测中',FINISHED:'已归档',INTERRUPTED:'已中断'};
    const time = stamp => stamp ? new Date(stamp*1000).toLocaleString() : '—';
    window.showModal(title+'检测记录', '<p>按检测轮次保存，无需工件号。展开可核对结果并导出；视频和图片为联调来源。</p>' +
      (data.items.map((row,i) => `<details class="sd-record" data-stream="${E(row.streamId)}"><summary>${E(time(row.startedAt))} · ${E(status[row.status] || row.status)} · ${E(assessment(row.result)?.label || (row.status === 'INTERRUPTED' ? '待复核' : '待判定'))} · ${E(sources[row.context?.inputSource] || '来源未记录')}</summary>
      <p>检测轮次：${E(row.streamId)}<br>模型：${E(names[row.context?.analysisType] || row.context?.analysisType || '—')}<br>结束时间：${E(time(row.finishedAt))}</p>
      <p>算法原判定：${E(row.result?.compliance?.label || '待判定')} · ${E(row.result?.compliance?.reason || row.reason || '等待检测结果')}</p>
      ${reviewSummary(row.result)}${row.status==='FINISHED'&&canReview({...row.result,analysisType:row.context.analysisType})?'<button data-sd-review="'+i+'" '+(occupied()?'disabled':'')+'>人工确认</button>':''}
      <button data-sd-download="${i}">导出本轮完整记录</button></details>`).join('') || '<p>本工位暂无检测记录。</p>') +
      `<p>第 ${Math.floor(offset/20)+1} 页</p><button id="sd-history-prev" ${offset ? '' : 'disabled'}>上一页</button> <button id="sd-history-next" ${data.hasMore ? '' : 'disabled'}>下一页</button>`);
    $('#sd-history-prev').onclick = () => attempt(() => history(Math.max(0,offset-20)));
    $('#sd-history-next').onclick = () => attempt(() => history(offset+20));
    document.querySelectorAll('[data-sd-review]').forEach(button => {button.onclick = () => showManualReview(data.items[Number(button.dataset.sdReview)].result);});
    document.querySelectorAll('[data-sd-download]').forEach(button => {button.onclick = () => {
      const row = data.items[Number(button.dataset.sdDownload)]; download(row,station+'-'+row.streamId+'.json');
    };});
  }
  async function activate(config) {
    mount(); stopCollectorPolling(); state.generation++; clearMedia(); clearInterval(state.freshnessTimer); renderedHTML = new WeakMap();
    Object.assign(state,{active:true,station:config.station,config,binding:null,connected:false,busy:false,running:false,stream:null,pendingStart:null,frames:0,frame:null,final:null,error:'',source:'camera',archived:false,started:0,lastAt:0,latency:null,timing:null,editing:false,scenes:[],scenesError:'',configuredTargets:[],preparation:null});
    resetMeasurements(); state.inputGeneration++; state.previewMode='detection';
    Object.assign(state,{collector:null,collectorCommand:null,remotePoll:null,remoteFps:null,remoteBarrier:null,remoteAnnounce:null,remoteManual:false,remoteNotice:null,transportLost:false});
    document.body.classList.add('station-task','station-detection-task');
    $('#sd-model').value = ''; $('#sd-credential').hidden = true; $('#sd-credential').textContent = '';
    $('.video-panel .subheading h3').innerHTML = '<b class="number blue">1</b>'+E(config.name)+' · 实时检测';
    set('.workflow-panel .section-title h2','工位检测');
    set('.compliance-panel .subheading h3','检测状态'); set('.compliance-grid>div:nth-child(1) span','累计确认'); set('.compliance-grid>div:nth-child(2) span','待确认');
    set('.compliance-grid>div:nth-child(3) span','当前结论');
    set('.toolbar [data-action="upload"] .task-action-label','选择图像来源');
    set('.footer>span','AI-SOP · 当前工位检测'); set('.alerts-foot','工位检测异常');
    $('#metric-fps').previousElementSibling.textContent='有效检测帧率'; $('#metric-latency').previousElementSibling.textContent='采样到响应';
    $('#metric-targets').previousElementSibling.textContent='累计确认'; $('#metric-compliance').previousElementSibling.textContent='检测结论';
    ['ID','检测状态','模型分数','检测位置','确认时刻','来源'].forEach((label,i)=>$('.results-panel thead').querySelectorAll('th')[i].textContent=label);
    render(); void refreshReviewQueue(); await W.connect(); await sync(); void reviews.recover();
  }
  function deactivate() {
    if (!state.active) return;
    $('#sd-config-dialog').close();
    stopCollectorPolling(); state.active = false; state.generation++; state.preparation = null; haltSampling(); clearInterval(state.freshnessTimer); clearMedia();
    document.body.classList.remove('station-detection-task');
  }
  document.addEventListener('workspace-session', event => {
    if (!state.active) return;
    if (event.detail) {
      if (state.source === 'device') {
        // A new authenticated session must re-read its own station and commands.
        stopCollectorPolling(); state.generation++; state.inputGeneration++; clearMedia();
        Object.assign(state,{stream:null,running:false,busy:false,collector:null,collectorCommand:null,remotePoll:null,
          remoteNotice:null,frame:null,final:null,connected:false,binding:null,archived:false});
      }
      void sync(); return;
    }
    state.generation++;
    stopCollectorPolling(); state.collector = state.collectorCommand = null;
    state.preparation = null;
    state.running = false; haltSampling(); clearMedia(); state.connected = false; state.binding = null;
    state.frame = state.final = null; state.frames = 0; $('#sd-credential').textContent = ''; $('#sd-credential').hidden = true; render();
  });
  document.addEventListener('visibilitychange', invalidateInput);
  window.addEventListener('offline', loseTransport);
  window.addEventListener('online', () => {invalidateInput(); void recoverTransport();});
  window.addEventListener('pageshow', event => {
    if (!event.persisted || !state.active || state.source !== 'device') return;
    state.generation++; state.inputGeneration++; state.remotePoll = null;
    expireResult(); void pollCollector();
  });
  window.addEventListener('pagehide', () => {
    if (!state.active) return;
    stopCollectorPolling(); state.inputGeneration++;
    if (state.source === 'device') {state.generation++; state.remotePoll = null; expireResult(); clearMedia(); return;}
    state.running = false; haltSampling(); clearInterval(state.freshnessTimer); clearMedia();
    if (state.stream && W.session) fetch('/api/v1/station-streams/'+state.stream.streamId, {method:'DELETE',keepalive:true,
      headers:{'Content-Type':'application/json','X-CSRF-Token':W.session.csrf},body:JSON.stringify({reason:'检测页面关闭，本轮中断'})}).catch(()=>{});
  });
  return {activate,deactivate,render,start,startBlocked,stop,stopBlocked, get active(){return state.active;}, get state(){return state;},canLeave:()=>state.source === 'device' || !occupied(),
    action(name) {
      if (['upload','start-upload','realtime'].includes(name)) {$('#sd-source').focus(); return state.source === 'device' ? attempt(pollCollector) : state.source === 'camera' ? attempt(camera) : $('#sd-file').click();}
      if (['screw-history','sop-history'].includes(name)) return attempt(history);
      if (name === 'account') return attempt(async () => {if (!W.session) await W.login(); else {if (state.source !== 'device' && occupied()) {await stop(); if (state.stream || state.pendingStart) return;} stopCollectorPolling(); clearMedia(); await W.logout();}});
      if (name === 'finish') return attempt(finish);
      if (name === 'report') return report();
      if (['system','checks','run-checks'].includes(name)) return attempt(system);
      if (['export','export-alerts'].includes(name)) {
        if (!state.final && !state.frame) {say('暂无检测结果，请先按 F8 检测或从工位历史记录导出。'); return;}
        download({stationId:state.station,inputSource:state.source,result:state.final||state.frame},state.station+'-detection.json'); return;
      }
      window.showModal(state.config.name+'检测说明', `<p>${E(detectionScope())}</p><p>当前模型：${E(names[state.binding?.analysisType] || '未配置')}。无需工件登记；${state.source === 'device' ? '按 F8 开始本件，页面收到全部位置合规确认后自动请求归档，采集器保存完成后弹出结果；也可手动结束。页面刷新或关闭不会停止后台采集；异常中断会单独记录。' : '按 F8 开始，合规后自动停止并归档，弹出结果提示。页面摄像头依赖当前页面，刷新会中断本轮。'}</p>`);
    }};
})();
