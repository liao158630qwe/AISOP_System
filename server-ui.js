'use strict';
// Server adapter. Legacy static-preview mode remains usable without the API.
window.ServerApp = (() => {
  const state={available:false,csrf:null,user:null,active:null,loaded:null,timer:null,busy:false,
    health:null,healthError:null,run:null,workflowsLoaded:false,
    runGeneration:0,runAbort:null,pollUpdatedAt:null,upload:null,limits:{max_upload_bytes:500*1024*1024,max_video_seconds:600}};
  const labels={QUEUED:'等待分析',RUNNING:'正在分析',CANCELLING:'正在取消',CANCELLED:'已取消',FAILED:'分析失败',SUCCEEDED:'分析完成'};
  const isLab=()=>$('#station-select')?.value==='algorithm-test';
  const selectedStation=()=>isLab()?null:$('#station-select')?.value||null;
  const selectedWorkflow=()=>isLab()?null:$('#action-workflow')?.value||null;
  const sequenceStatuses={OBSERVING:'观察中',INCOMPLETE:'尚未完整观察',EVIDENCE_GAP:'证据中断',REVIEW_SEQUENCE:'顺序需复核',OBSERVED_SEQUENCE:'已观察完整顺序'};
  let currentSequence=null,sequenceRevision=-1,sequenceTrack=null;
  function clearSequence(){currentSequence=null;sequenceRevision=-1;sequenceTrack=null;}
  function renderSequence(sequence){
    if(sequenceRevision!==sceneRevision)sequenceTrack=null;
    currentSequence=sequence;sequenceRevision=sceneRevision;
    $('#pending-step').hidden=true;$('.recent-cycles').hidden=true;
    const steps=sequence.workflow.config.steps;
    $('#workflow-name').textContent=sequence.workflow.config.name;
    $('#workflow-status').textContent='按人物观察 · 待复核';
    $('#step-counter').textContent=`${sequence.subjects.length} 条人物轨迹`;
    $('#workflow-description').textContent=sequence.scope;

    const subjects = sequence.subjects;
    const wanted=$('#inline-live')?sequenceTrack:temporalTrack;
    const s = subjects.find(sub=>String(sub.trackId)===String(wanted)) || subjects.filter(sub=>sub.observed).at(-1) || subjects.at(-1);
    if(!s){
      $('#workflow-status').textContent='等待人物轨迹';
      $('#steps').innerHTML = '<p class="empty-note">等待可靠的人物轨迹，检出后显示各步骤进度。</p><ol class="sequence-steps">'+steps.map(step=>`<li>○ ${escapeHTML(step.name)}</li>`).join('')+'</ol>';
      if($('#live-inspection'))$('#live-inspection').innerHTML=$('#steps').innerHTML;
      return;
    }
    sequenceTrack=s.trackId;
    $('#workflow-status').textContent=sequenceStatuses[s.status]||'待复核';
    const current = steps.find(step => step.id === s.currentStep);
    const p = s.currentStepProgress;
    const eventCount = sequence.eventCount ?? sequence.events?.length ?? 0;

    const optionsHTML=subjects.map(sub=>`<option value="${escapeHTML(sub.trackId)}" ${sub===s?'selected':''}>人物 ${escapeHTML(sub.trackId)} · 第 ${escapeHTML(sub.cycle)} 轮${sub.observed?' · 当前可见':' · 当前未见'}</option>`).join('');
    const selectorHTML=subjects.length>1?`<label class="sequence-subject-control" for="sequence-subject">查看人物流程 <select id="sequence-subject" class="field">${optionsHTML}</select></label>`:'';
    const cardHTML = `
      <div class="station-check latest-focused">
        <div class="station-check-head">
          <b>人物 ${s.trackId} · 第 ${s.cycle} 轮（最新判定）</b>
          <span class="status-badge">${sequenceStatuses[s.status] || '待复核'}</span>
        </div>
        <p class="cycle-summary">${s.completed.length}/${steps.length} 步 · ${s.observed ? '当前作业中' : '当前未见'}</p>
        <progress class="sequence-progress" value="${s.completed.length}" max="${steps.length}" aria-label="人物${s.trackId}步骤完成进度"></progress>

        <div class="current-stage-card">
          <span class="stage-label">实时判定阶段</span>
          <strong class="stage-name">${escapeHTML(current?.name || '本轮观察完成')}</strong>
          ${current && p ? `
            <div class="stage-evidence">
              <small>${p.waitingForRelease ? '等待上一动作释放' : `持续证据 ${p.observedSeconds.toFixed(2)}s / ${p.requiredSeconds.toFixed(2)}s`}</small>
              <progress class="sequence-progress" max="1" value="${p.fraction}" aria-label="当前步骤证据进度"></progress>
            </div>
          ` : ''}
        </div>

        <ol class="sequence-steps">
          ${steps.map(step => `
            <li class="${s.completed.some(c => c.stepId === step.id) ? 'done' : step.id === s.currentStep ? 'current' : ''}">
              ${s.completed.some(c => c.stepId === step.id) ? '✓' : step.id === s.currentStep ? '▶' : '○'} ${escapeHTML(step.name)}
            </li>
          `).join('')}
        </ol>

        <div class="history-chip" data-action="sop-history">
          <span class="chip-dot"></span>
          <span>当前检测记录 (${eventCount} 条)</span>
          <button class="btn-link" type="button" data-action="sop-history">查看记录 ›</button>
        </div>
      </div>
    `;
    const container=$('#steps'),card=$('.latest-focused',container),picker=$('#sequence-subject',container);
    if(!card)container.innerHTML=selectorHTML+cardHTML;
    else{
      card.outerHTML=cardHTML;
      if(subjects.length>1){
        if(!picker)container.insertAdjacentHTML('afterbegin',selectorHTML);
        else if(document.activeElement!==picker){
          if(picker.innerHTML!==optionsHTML)picker.innerHTML=optionsHTML;
          picker.value=String(s.trackId);
        }
      }else picker?.closest('label').remove();
    }
    if($('#sequence-subject'))$('#sequence-subject').onchange=event=>{
      sequenceTrack=event.target.value;
      if(!$('#inline-live')&&analysisData){temporalTrack=Number(sequenceTrack);renderedWorker=null;updateWorkerDashboard();}
      else renderSequence(currentSequence);
    };
    if($('#live-inspection')) $('#live-inspection').innerHTML = $('#steps .latest-focused').outerHTML;
  }
  function showSequenceHistory(){
    const sequence=$('#inline-live')?(sequenceRevision===sceneRevision?currentSequence:null):analysisData?.actionSequence;
    const events=Array.isArray(sequence?.events)?sequence.events:[];
    if(!sequence){showModal('动作流程历史记录','<p class="modal-intro">当前没有动作流程记录。选择动作顺序后开始摄像头检测，或打开包含动作流程的分析结果。</p><div class="modal-actions"><button class="btn btn-primary" data-action="close-modal">关闭</button></div>');return;}
    const codes={STEP_OBSERVED:'已观察步骤',STEP_TIMEOUT:'等待超时',OUT_OF_ORDER:'顺序需复核',CONTINUITY_RESET:'连续性中断'};
    const tracks=[...new Set(events.map(event=>event.trackId))];
    const replay=!$('#inline-live')&&!!analysisData;
    showModal('动作流程历史记录',`<p class="modal-intro">${escapeHTML(sequence.workflow.config.name)} · ${replay?'当前视频分析结果，可定位到事件画面':'当前摄像头检测，未保存录像'}。观察记录需人工复核。</p><label class="sequence-subject-control" for="sequence-history-track">筛选人物 <select id="sequence-history-track" class="field"><option value="">全部人物</option>${tracks.map(id=>`<option value="${escapeHTML(id)}">人物 ${escapeHTML(id)}</option>`).join('')}</select></label><p id="sequence-history-summary" class="hint" role="status"></p><div class="history-table-wrap"><table class="sop-history-table"><thead><tr><th>时间</th><th>人物 / 周期</th><th>判定</th><th>依据</th>${replay?'<th>回放</th>':''}</tr></thead><tbody id="sequence-history-rows"></tbody></table></div><div class="modal-actions"><button class="btn btn-primary" data-action="close-modal">关闭</button></div>`);
    const render=()=>{
      const track=$('#sequence-history-track').value;
      const selected=events.filter(event=>!track||String(event.trackId)===track).slice().reverse();
      $('#sequence-history-summary').textContent=`显示 ${selected.length} 条记录${sequence.truncated?` · 累计 ${sequence.eventCount??events.length} 条，仅展示保留记录`:''}`;
      $('#sequence-history-rows').innerHTML=selected.length?selected.map(event=>`<tr><td>${Number(event.time).toFixed(2)}s</td><td>P${escapeHTML(event.trackId)} / ${escapeHTML(event.cycle)}</td><td>${escapeHTML(codes[event.code]||event.code)}</td><td class="cell-desc">${escapeHTML(event.detail)}</td>${replay?`<td><button class="btn" data-sequence-seek="${Number(event.time)}" data-sequence-track="${escapeHTML(event.trackId)}">定位画面</button></td>`:''}</tr>`).join(''):`<tr><td colspan="${replay?5:4}">暂无记录，持续观察到步骤或复核提示后会显示。</td></tr>`;
    };
    $('#sequence-history-track').onchange=render;render();
    $('#sequence-history-rows').onclick=event=>{
      const button=event.target.closest('[data-sequence-seek]');if(!button)return;
      temporalTrack=Number(button.dataset.sequenceTrack);renderedWorker=null;
      video.pause();video.currentTime=Number(button.dataset.sequenceSeek);closeModal();updateWorkerDashboard();
    };
  }
  const labNames={raise:'举手',grip:'握拳／抓握候选',objects:'通用目标识别'};
  const savedLab=readStorage('aisop-lab',null);
  let labOrder=Array.isArray(savedLab?.order)&&savedLab.order.length===3&&new Set(savedLab.order).size===3&&savedLab.order.every(id=>id in labNames)?savedLab.order:['raise','grip','objects'];
  function setupLabControls(){
    $('#ops-strip').insertAdjacentHTML('beforeend','<div id="test-controls" hidden><label><input type="checkbox" id="test-sequential" checked> 按所选顺序测试（同一人物持续观察0.5秒）</label><div id="test-algorithms"></div><small>可取消顺序模式，独立测试各算法。抓握与通用目标尚未关联人物，不能完成顺序步骤。修改后请重新开始检测。</small></div>');
    const save=()=>writeStorage('aisop-lab',{order:labOrder,enabled:Array.from(document.querySelectorAll('[data-test-check]:checked')).map(e=>e.value),sequential:$('#test-sequential').checked});
    const render=()=>{let selected=Array.from(document.querySelectorAll('[data-test-check]:checked')).map(e=>e.value);if(!$('#test-algorithms').children.length)selected=Array.isArray(savedLab?.enabled)?savedLab.enabled:labOrder;$('#test-algorithms').innerHTML=labOrder.map((id,i)=>`<label class="test-item"><input id="test-${id}" data-test-check type="checkbox" value="${id}" ${selected.includes(id)?'checked':''}>${labNames[id]}<button type="button" class="btn" data-test-up="${id}" ${i===0?'disabled':''} aria-label="上移${labNames[id]}">↑</button></label>`).join('');};
    $('#test-sequential').checked=savedLab?.sequential!==false;
    render();$('#test-controls').onchange=save;$('#test-algorithms').onclick=e=>{const id=e.target.dataset.testUp;if(!id)return;const i=labOrder.indexOf(id);if(i>0){[labOrder[i-1],labOrder[i]]=[labOrder[i],labOrder[i-1]];render();save();}};
  }
  function renderLab(lab,ids,sequence,sequential){
    // Sequence progress is exclusively computed by the server per tracked person.
    sequence={index:0};
    const labels={DETECTED:'已检出',CANDIDATE:'候选检出',NOT_OBSERVED:'未观察到',UNAVAILABLE:'模型未就绪'};
    $('#workflow-name').textContent='算法测试流程';$('#workflow-status').textContent=sequential&&sequence.index===ids.length?'测试完成':'测试中';
    $('#step-counter').textContent=sequential?`${sequence.index}/${ids.length}`:'独立测试';
    $('#workflow-description').textContent='测试结果不代表生产SOP合格；抓握仅为手指弯曲候选';
    $('#steps').innerHTML=ids.map((id,i)=>{const c=lab.checks.find(c=>c.id===id);return `<div class="station-check"><b>${i+1}. ${labNames[id]}</b><span>${sequential&&i<sequence.index?'测试已完成':labels[c?.state]||'等待'}</span><p>${escapeHTML(c?.detail||'等待推理')}</p></div>`;}).join('');
    $('#live-inspection').innerHTML=$('#steps').innerHTML;
  }
  function inspectionHTML(inspection){
    if(!inspection)return '<p>通用人体姿态模式，未选择灯具工位。</p>';
    return `<p>${escapeHTML(inspection.stationName)} · ${escapeHTML(inspection.version)} · 待复核</p>`+inspection.checks.map(c=>`<div class="station-check"><b>${escapeHTML(c.name)}</b><span>待复核</span><p>${escapeHTML(c.reason)}</p>${(c.targets||[]).map(t=>`<p>${escapeHTML(t.id)}：${t.state==='CANDIDATE'?'可见候选':'不确定'} · ${escapeHTML(t.reason)}</p>`).join('')}</div>`).join('');
  }
  function responseError(code, data, retryAfter=0){
    if(code===401){state.csrf=null;state.user=null;renderAccount();}
    const error=new Error(data.message||`请求失败（${code}），请稍后重试`);
    error.status=code;
    error.retryAfter=Math.min(10000,Math.max(0,Number(retryAfter)||0)*1000);
    return error;
  }
  async function request(path, options={}){
    const {timeoutMs=30000,signal,...init}=options;
    const controller=new AbortController();
    const cancel=()=>controller.abort();
    if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
    const timer=setTimeout(cancel,timeoutMs);
    const headers={...(options.headers||{})};
    if(options.method&&options.method!=='GET'&&state.csrf)headers['X-CSRF-Token']=state.csrf;
    try{
      const response=await fetch('/api/v1'+path,{...init,headers,signal:controller.signal,credentials:'same-origin'});
      const data=await response.json().catch(()=>({message:'服务器返回无效响应'}));
      if(!response.ok)throw responseError(response.status,data,response.headers.get('Retry-After'));
      return data;
    }catch(error){
      if(controller.signal.aborted&&!signal?.aborted)throw new Error('请求超时，请检查网络或稍后重试');
      throw error;
    }finally{clearTimeout(timer);signal?.removeEventListener('abort',cancel);}
  }
  function status(text,phase='idle'){
    $('#server-status').textContent=text;
    $('#ops-strip').dataset.phase=phase;
  }
  function progress(value){
    const bar=$('#server-progress');
    if(!bar)return;
    bar.hidden=value===null;
    if(value!==null)bar.value=value;
  }
  function renderAccount(){
    if(!$('#server-account'))return;
    $('#server-account').textContent=state.user?`${state.user} · 退出`:'登录服务器';
  }
  function leaveRun(){
    clearSequence();
    state.run=null;
    state.runGeneration++;state.runAbort?.abort();state.runAbort=null;
    state.active=null;state.loaded=null;state.pollUpdatedAt=null;clearTimeout(state.timer);progress(null);
    if($('#server-cancel'))$('#server-cancel').hidden=true;
    const url=new URL(location.href);url.searchParams.delete('run');history.replaceState(null,'',url);
  }
  function login(after){
    showModal('登录分析服务器',`<form id="server-login"><label class="form-label" for="server-user">用户名</label><input class="field" id="server-user" autocomplete="username" value="admin" required><label class="form-label" for="server-password">密码</label><input class="field" id="server-password" type="password" autocomplete="current-password" required><p id="login-error" role="alert"></p><div class="modal-actions"><button class="btn btn-primary" type="submit">登录</button></div></form>`);
    $('#server-login').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
      try{const data=await request('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('#server-user').value,password:$('#server-password').value})});state.user=data.username;state.csrf=data.csrf;renderAccount();closeModal();if(after)await after();else await showHistory();}
      catch(error){if($('#login-error'))$('#login-error').textContent=error.message;else toast(error.message);}finally{button.disabled=false;}
    });
  }
  function showUpload(){
    if(state.busy){toast('正在上传或创建任务，请稍候');return;}
    if(isLab()){toast('算法测试工位用于实时摄像头；上传分析请选择灯具工位或通用姿态');return;}
    if(!state.user){login(showUpload);return;}
    showModal('上传视频到服务器分析',`<p class="modal-intro">视频将上传到当前服务器。关闭浏览器不会中止已创建的分析任务。</p><div class="upload-zone" id="server-drop"><h3>选择或拖放工位视频</h3><p>MP4 / WebM / MOV / AVI / M4V，最大 ${Math.floor(state.limits.max_upload_bytes/1048576)} MiB、${Math.round(state.limits.max_video_seconds/60)} 分钟。当前识别人体与姿态，不判定SOP合格。</p><button class="btn btn-primary" data-action="choose-video">选择视频并分析</button></div><div class="modal-actions"><button class="btn btn-secondary" data-action="server-history">查看服务器任务</button><button class="btn btn-secondary" data-action="worker-video">查看既有分析样例</button></div>`);
    $('#server-drop').addEventListener('dragover',e=>e.preventDefault());
    $('#server-drop').addEventListener('dragenter',()=>$('#server-drop')?.classList.add('dragging'));
    $('#server-drop').addEventListener('dragleave',()=>$('#server-drop')?.classList.remove('dragging'));
    $('#server-drop').addEventListener('drop',e=>{e.preventDefault();if(e.dataTransfer.files[0])upload(e.dataTransfer.files[0]);});
  }
  let liveStreamId=null, liveGeneration=0, liveStream=null, liveTimer=null, liveAbort=null, liveDeviceChange=null;
  let livePhase='idle',liveData=null,liveFrames=0,liveFps=null,liveError=null;
  function stopLive(){
    const released=liveStreamId;liveStreamId=null;
    if($('#inline-live'))$('.timeline-panel .micro-tag').textContent='实时检测已停止';
    if(released)request('/realtime/streams/'+released,{method:'DELETE'}).catch(()=>{});
    liveGeneration++;clearTimeout(liveTimer);liveAbort?.abort();liveAbort=null;
    livePhase='stopped';
    liveStream?.getTracks().forEach(track=>track.stop());liveStream=null;
    const camera=$('#live-camera');if(camera)camera.srcObject=null;
    if($('#live-stop'))$('#live-stop').disabled=true;
    if($('#live-start'))$('#live-start').disabled=false;
  }
  window.addEventListener('pagehide',stopLive);
  function leaveLive(){
    stopLive();
    clearSequence();
    if(liveDeviceChange){navigator.mediaDevices?.removeEventListener('devicechange',liveDeviceChange);liveDeviceChange=null;}
    $('#inline-live')?.remove();$('#video-stage').hidden=false;$('.video-stats').hidden=false;
    if($('#video-mode'))$('#video-mode').value='replay';
    if(analysisData){$('.camera-name').textContent=analysisData.source.title;$('.stream-info').textContent=analysisData.model.name;}
    updateDashboard();
  }
  function drawHands(ctx,hands,output){
    if(!Array.isArray(hands)||!hands.length)return;
    const unit=displayPixelRatio(ctx);
    ctx.save();ctx.lineCap='round';ctx.lineJoin='round';
    ctx.font=`600 ${Math.round(12*unit)}px "Microsoft YaHei",sans-serif`;
    for(const hand of hands){
      const p=hand.points;if(!Array.isArray(p))continue;
      ctx.beginPath();
      for(const chain of poseSkeleton.handChains)for(let i=1;i<chain.length;i++){const a=p[chain[i-1]],b=p[chain[i]];if(!a||!b)continue;ctx.moveTo(a[0]*output.width,a[1]*output.height);ctx.lineTo(b[0]*output.width,b[1]*output.height);}
      ctx.strokeStyle='#03101f';ctx.lineWidth=4.5*unit;ctx.stroke();
      ctx.strokeStyle='#ffcf60';ctx.lineWidth=2.4*unit;ctx.stroke();
      ctx.fillStyle='#ffcf60';ctx.fillText(hand.label,p[0][0]*output.width,p[0][1]*output.height);
    }
    ctx.restore();
  }
  function showLive(){
    if(state.busy){toast('请先完成或取消上传，再开始实时检测');$('#video-mode').value='replay';return;}
    if(!state.user){$('#video-mode').value='replay';login(showLive);return;}
    leaveLive();leaveRun();sceneRevision++;demo=false;video.pause();$('#video-mode').value='realtime';$('#video-stage').hidden=true;$('.video-stats').hidden=true;
    livePhase='idle';liveData=null;liveFrames=0;liveFps=null;liveError=null;
    for(const id of ['metric-compliance','video-compliance','compliance-rate','compliant-count','noncompliant-count','metric-fps','metric-latency','metric-targets'])$('#'+id).textContent='—';
    $('#detection-results').innerHTML='';$('#time-bars').innerHTML='';$('#timeline-playhead')?.setAttribute('hidden','');$('#result-note').textContent='等待实时采样结果';
    resetLiveTimeline();
    $('#workflow-description').textContent='实时单帧姿态与配置工位检查，不判定工艺完成';$('#steps').innerHTML='';$('#workflow-status').textContent='待复核';
    $('#pending-step').hidden=true;$('.recent-cycles').hidden=true;
    $('.compliance-foot').textContent='实时检测 · SOP待复核';$('#realtime-alert').textContent='待复核';
    $('.camera-name').textContent='浏览器摄像头';$('.stream-info').textContent='实时预览与采样检测';$('.live').textContent='等待开启';status('实时摄像头模式');
    $('#video-stage').insertAdjacentHTML('afterend',`<section id="inline-live"><div class="live-controls"><label>摄像头 <select id="live-device" class="field" aria-label="选择摄像头设备"><option value="">默认摄像头</option></select></label><label>视图 <select id="live-view" class="field"><option value="detection">算法检测画面</option><option value="preview">原始预览（不叠加检测）</option></select></label><label>检测目标帧率 <select id="live-rate" class="field"><option>5</option><option>10</option><option>15</option><option selected>30</option><option>60</option></select> fps</label><button class="btn btn-primary" id="live-start">开始检测</button><button class="btn btn-secondary" id="live-stop">停止检测</button></div><div class="live-picture"><video id="live-camera" autoplay muted playsinline hidden></video><canvas id="live-result" width="640" height="360" aria-label="实时检测采样画面"></canvas></div><p id="live-status" role="status">点击开始授权摄像头。画面发送到当前服务器，不保存录像。</p><p id="live-metrics">预览 — fps · 检测 — fps · 延迟 — ms</p></section>`);
    const message=$('#live-status'),start=$('#live-start'),camera=$('#live-camera'),output=$('#live-result'),deviceSelect=$('#live-device');
    $('#live-stop').disabled=true;
    message.insertAdjacentHTML('afterend','<div id="live-inspection" hidden></div>');
    $('#live-start').insertAdjacentHTML('beforebegin','<label>手部骨架 <input type="checkbox" id="live-hands"></label>');
    $('#live-hands').disabled=isLab();
    $('#live-view').onchange=()=>{const preview=$('#live-view').value==='preview';camera.hidden=!preview;output.hidden=preview;};
    const cameraKey=()=>{try{return localStorage.getItem('aisop-camera-device')||'';}catch{return '';}};
    const rememberCamera=id=>{try{if(id)localStorage.setItem('aisop-camera-device',id);else localStorage.removeItem('aisop-camera-device');}catch{}};
    let desiredCamera=cameraKey();
    const refreshCameraDevices=async()=>{
      if(!navigator.mediaDevices?.enumerateDevices)return;
      let devices;try{devices=await navigator.mediaDevices.enumerateDevices();}catch{return;}
      if(!document.body.contains(deviceSelect))return;
      const cameras=devices.filter(device=>device.kind==='videoinput');
      const current=deviceSelect.value;
      deviceSelect.innerHTML='<option value="">默认摄像头</option>';
      cameras.forEach((device,index)=>{const option=document.createElement('option');option.value=device.deviceId;option.textContent=device.label||`摄像头 ${index+1}`;deviceSelect.append(option);});
      if(desiredCamera&&cameras.some(device=>device.deviceId===desiredCamera)){deviceSelect.value=desiredCamera;desiredCamera='';}
      else if(current&&cameras.some(device=>device.deviceId===current))deviceSelect.value=current;
      else deviceSelect.value='';
    };
    deviceSelect.onchange=()=>{desiredCamera='';rememberCamera(deviceSelect.value);if(liveStreamId)beginLive();};
    refreshCameraDevices();
    liveDeviceChange=refreshCameraDevices;
    navigator.mediaDevices?.addEventListener('devicechange',liveDeviceChange);
    $('#live-stop').onclick=()=>{stopLive();start.disabled=false;message.textContent='已停止，摄像头已释放';output.getContext('2d').clearRect(0,0,output.width,output.height);};
    const beginLive=async()=>{
      stopLive();const generation=liveGeneration;start.disabled=true;$('#live-stop').disabled=false;
      livePhase='starting';liveData=null;liveFrames=0;liveFps=null;liveError=null;
      clearSequence();$('#steps').innerHTML='<p class="empty-note">等待新一轮检测结果</p>';
      $('#workflow-status').textContent='等待检测';$('#step-counter').textContent='—';
      const stationId=selectedStation(),testIds=isLab()?labOrder.filter(id=>$('#test-'+id)?.checked):[];
      if(isLab()&&!testIds.length){stopLive();message.textContent='请至少选择一个测试算法';return;}
      const streamId=crypto.randomUUID(),streamStarted=performance.now(),workflowId=selectedWorkflow();liveStreamId=streamId;
      resetLiveTimeline();
      const sequence={},sequential=$('#test-sequential')?.checked;
      const handsOn=!testIds.length&&!!$('#live-hands')?.checked;
      const frameQuery=new URLSearchParams();
      if(testIds.length){frameQuery.set('test_algorithms',testIds.join(','));frameQuery.set('sequential',String(sequential));}
      else{if(stationId)frameQuery.set('station_id',stationId);if(workflowId)frameQuery.set('workflow_id',workflowId);if(handsOn)frameQuery.set('hands','1');}
      const frameQueryString=frameQuery.toString(),frameUrl='/realtime/frame'+(frameQueryString?'?'+frameQueryString:'');
      let previewFrames=0,previewFps=0,previewStart=performance.now(),lastResult=0,retries=0;
      function countPreview(now){if(generation!==liveGeneration)return;previewFrames++;if(now-previewStart>=1000){previewFps=previewFrames*1000/(now-previewStart);previewFrames=0;previewStart=now;}camera.requestVideoFrameCallback?.(countPreview);}
      try{
        if(!navigator.mediaDevices?.getUserMedia)throw new Error('摄像头需要HTTPS或localhost环境');
        message.textContent='正在请求摄像头权限…';
        const videoConstraints={width:{ideal:1280},height:{ideal:720},frameRate:{ideal:30}};
        const cameraId=deviceSelect.value;
        let stream;
        try{stream=await navigator.mediaDevices.getUserMedia({video:cameraId?{...videoConstraints,deviceId:{exact:cameraId}}:videoConstraints,audio:false});}
        catch(error){
          if(cameraId&&['OverconstrainedError','NotFoundError','DevicesNotFoundError'].includes(error.name)){
            desiredCamera='';rememberCamera('');deviceSelect.value='';
            message.textContent='所选摄像头不可用，已改用默认摄像头';
            stream=await navigator.mediaDevices.getUserMedia({video:videoConstraints,audio:false});
          }else throw error;
        }
        if(generation!==liveGeneration){stream.getTracks().forEach(t=>t.stop());return;}
        liveStream=stream;camera.srcObject=stream;await camera.play();
        if(generation!==liveGeneration)return;
        await refreshCameraDevices();
        const trackLabel=stream.getVideoTracks()[0]?.label;
        if(trackLabel)$('.camera-name').textContent=trackLabel;
        $('.live').textContent='实时检测';
        camera.requestVideoFrameCallback?.(countPreview);
        const capture=document.createElement('canvas');
        const captureContext=capture.getContext('2d');
        async function tick(){
          if(generation!==liveGeneration)return;
          if(document.hidden){liveTimer=setTimeout(tick,1000);return;}
          try{
            const tickStarted=performance.now();
            if(!camera.videoWidth)throw new Error('摄像头未提供有效画面');
            const scale=Math.min(1,(stationId?1280:640)/camera.videoWidth,(stationId?720:360)/camera.videoHeight);
            const width=Math.floor(camera.videoWidth*scale),height=Math.floor(camera.videoHeight*scale);
            if(capture.width!==width||capture.height!==height){capture.width=width;capture.height=height;}
            captureContext.drawImage(camera,0,0,capture.width,capture.height);
            const blob=await new Promise(resolve=>capture.toBlob(resolve,'image/jpeg',.75));
            if(generation!==liveGeneration)return;
            if(!blob)throw new Error('画面编码失败');
            const sent=performance.now();if(!lastResult)message.textContent='正在检测…首次加载模型可能较慢';
            liveAbort=new AbortController();
            const controller=liveAbort,timeout=setTimeout(()=>controller.abort(),60000);
            let data;
            try{data=await request(frameUrl,{method:'POST',headers:{'Content-Type':'image/jpeg','X-Stream-Id':streamId,'X-Frame-Time':String((tickStarted-streamStarted)/1000)},body:blob,signal:controller.signal,timeoutMs:60000});}
            catch(error){if(error.name==='AbortError'&&generation===liveGeneration)throw new Error('服务器60秒内未返回结果，请检查推理服务后重试');throw error;}
            finally{clearTimeout(timeout);}
            if(generation!==liveGeneration)return;
            retries=0;
            adoptSkeletonTopology(data.skeleton);
            if(output.width!==capture.width||output.height!==capture.height){output.width=capture.width;output.height=capture.height;}
            const ctx=output.getContext('2d');ctx.drawImage(capture,0,0);
            const unit=displayPixelRatio(ctx),fontSize=Math.round(15*unit),labelHeight=fontSize+8*unit;
            ctx.textBaseline='middle';ctx.font=`700 ${fontSize}px "Microsoft YaHei",sans-serif`;
            for(const p of data.people){
              const [x,y,x2,y2]=p.bbox,bx=x*output.width,by=y*output.height,bw=(x2-x)*output.width,bh=(y2-y)*output.height;
              ctx.strokeStyle='#03101f';ctx.lineWidth=5*unit;ctx.strokeRect(bx,by,bw,bh);
              ctx.strokeStyle='#3fcf9b';ctx.lineWidth=3*unit;ctx.strokeRect(bx,by,bw,bh);
              const label=`P${String(p.trackId).padStart(2,'0')} ${p.label}`;
              const labelWidth=Math.min(output.width,ctx.measureText(label).width+12*unit);
              const labelX=Math.max(0,Math.min(bx,output.width-labelWidth));
              const labelY=Math.max(0,Math.min(by-labelHeight,output.height-labelHeight));
              ctx.fillStyle='#041822ee';ctx.fillRect(labelX,labelY,labelWidth,labelHeight);
              ctx.strokeStyle='#3fcf9b';ctx.lineWidth=1.2*unit;ctx.strokeRect(labelX,labelY,labelWidth,labelHeight);
              ctx.fillStyle='#b5ffe1';ctx.fillText(label,labelX+6*unit,labelY+labelHeight/2);
              drawPoseSkeleton(ctx,p.keypoints,0,0,output.width,output.height);
            }
            if(data.lab){
              const labFont=Math.round(13*unit),labHeight=labFont+8*unit;
              ctx.font=`700 ${labFont}px "Microsoft YaHei",sans-serif`;
              for(const o of data.lab.objects){
                const [a,b,c,d]=o.bbox,bx=a*output.width,by=b*output.height,bw=(c-a)*output.width,bh=(d-b)*output.height;
                ctx.strokeStyle='#03101f';ctx.lineWidth=4.5*unit;ctx.strokeRect(bx,by,bw,bh);
                ctx.strokeStyle='#c594ff';ctx.lineWidth=2.6*unit;ctx.strokeRect(bx,by,bw,bh);
                const text=`${o.label} ${(o.confidence*100).toFixed(0)}%`,tw=ctx.measureText(text).width+10*unit;
                const tx=Math.max(0,Math.min(bx,output.width-tw)),ty=Math.max(0,by-labHeight);
                ctx.fillStyle='#110b1fee';ctx.fillRect(tx,ty,tw,labHeight);
                ctx.strokeStyle='#c594ff';ctx.lineWidth=1.2*unit;ctx.strokeRect(tx,ty,tw,labHeight);
                ctx.fillStyle='#dcc4ff';ctx.fillText(text,tx+5*unit,ty+labHeight/2);
              }
              drawHands(ctx,data.lab.hands,output);
            }
            if(Array.isArray(data.hands))drawHands(ctx,data.hands,output);
            const modelTag=data.model?' · '+data.model:'';
            message.textContent=`检测到 ${data.people.length} 人 · 请求耗时 ${Math.round(performance.now()-sent)} ms${modelTag}${handsOn&&!Array.isArray(data.hands)?' · 手部模型未就绪':''} · SOP待复核`;
            const now=performance.now(),fps=lastResult?1000/(now-lastResult):0;lastResult=now;
            livePhase='active';liveData=data;liveFrames++;liveFps=fps||null;liveError=null;
            $('#live-metrics').textContent=`预览 ${previewFps?previewFps.toFixed(1):'—'} fps · 检测 ${fps?fps.toFixed(1):'—'} fps · 延迟 ${Math.round(now-tickStarted)} ms · 服务端 ${data.processingMs} ms${modelTag}`;
            $('#footer-performance').textContent=$('#live-metrics').textContent;$('#clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
            $('#metric-fps').textContent=fps?fps.toFixed(1):'—';$('#metric-latency').textContent=Math.round(now-tickStarted);$('#metric-targets').textContent=data.people.length;
            $('#detection-results').innerHTML=data.people.length?data.people.map(p=>`<tr><td>${p.trackId}</td><td>${escapeHTML(p.label)}</td><td>${(p.confidence*100).toFixed(0)}%</td><td>当前画面</td><td>—</td><td>待复核</td></tr>`).join(''):'<tr><td colspan="6">当前画面未检出可靠人体，请检查画面范围与光照。</td></tr>';
            $('#result-note').textContent='连续流跟踪 · 遮挡或关联不确定会中断证据';renderLiveTimeline(data.people,data.frameTime??(tickStarted-streamStarted)/1000);
            if(!data.actionSequence){clearSequence();$('#steps').innerHTML=inspectionHTML(data.inspection);}
            $('#workflow-name').textContent=data.inspection?.stationName||'实时姿态检测';$('#workflow-status').textContent='待复核';$('#step-counter').textContent='待复核';
            $('#live-inspection').innerHTML=inspectionHTML(data.inspection);
            if(data.lab){if(!data.actionSequence)renderLab(data.lab,testIds,sequence,sequential);$('#result-note').textContent=data.lab.checks.map(c=>`${c.name}：${c.state==='DETECTED'||c.state==='CANDIDATE'?'检出':c.state==='UNAVAILABLE'?'模型未就绪':'未观察到'}`).join('；');}
            if(data.actionSequence){renderSequence(data.actionSequence);if(data.inspection)$('#live-inspection').insertAdjacentHTML('beforeend',inspectionHTML(data.inspection));}
            if(data.inspection){ctx.strokeStyle='#efb956';for(const check of data.inspection.checks)for(const target of check.targets){const [a,b,c,d]=target.roi;ctx.strokeRect(a*output.width,b*output.height,(c-a)*output.width,(d-b)*output.height);}}
            liveTimer=setTimeout(tick,Math.max(0,1000/Number($('#live-rate').value)-(performance.now()-tickStarted)));
          }catch(error){
            if(generation!==liveGeneration)return;
            if((error.status===429||error instanceof TypeError)&&retries<5){
              const delay=Math.max(error.retryAfter||0,Math.min(8000,500*2**retries++));
              livePhase='retrying';liveError=error.message;
              message.textContent=`${error.status===429?'检测服务繁忙':'网络暂时中断'}，${(delay/1000).toFixed(1)} 秒后重试（${retries}/5）`;
              liveTimer=setTimeout(tick,delay);return;
            }
            stopLive();livePhase='error';liveError=error.message;start.disabled=false;message.textContent='检测已停止：'+error.message;
          }
        }
        await tick();
      }catch(error){if(generation!==liveGeneration)return;stopLive();livePhase='error';liveError=error.message;start.disabled=false;message.textContent='无法开始：'+error.message;}
    };
    start.onclick=beginLive;
  }
  function sendVideo(file){
    return new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest();state.upload=xhr;
      xhr.open('POST','/api/v1/media?filename='+encodeURIComponent(file.name));
      xhr.timeout=10*60*1000;
      xhr.setRequestHeader('Content-Type','application/octet-stream');
      xhr.setRequestHeader('X-CSRF-Token',state.csrf);
      xhr.upload.onprogress=e=>{
        if(!e.lengthComputable)return;
        const percent=Math.min(100,Math.floor(e.loaded/e.total*100));progress(percent);
        status(percent===100?'上传完成，正在校验视频…':`正在上传：${file.name} · ${percent}%（${(e.loaded/1048576).toFixed(1)} / ${(e.total/1048576).toFixed(1)} MiB）`,'uploading');
      };
      xhr.onload=()=>{
        let data;try{data=JSON.parse(xhr.responseText);}catch{reject(new Error('服务器返回无效响应'));return;}
        if(xhr.status>=200&&xhr.status<300)resolve(data);else reject(responseError(xhr.status,data));
      };
      xhr.onerror=()=>reject(new Error('上传连接中断，请检查网络后重新选择视频'));
      xhr.ontimeout=()=>reject(new Error('上传超时，请检查网络或缩小视频后重试'));
      xhr.onabort=()=>reject(new DOMException('上传已取消','AbortError'));
      xhr.onloadend=()=>{if(state.upload===xhr)state.upload=null;};
      xhr.send(file);
    });
  }
  async function upload(file){
    if(isLab()){toast('算法测试工位请使用实时摄像头检测');return;}
    if(!state.user){login(()=>upload(file));return;}
    if(state.busy){toast('正在上传，请稍候');return;}
    if(!file.size||file.size>state.limits.max_upload_bytes){toast(`请选择非空且不超过 ${Math.floor(state.limits.max_upload_bytes/1048576)} MiB 的视频`);return;}
    if(!/\.(mp4|webm|mov|avi|m4v)$/i.test(file.name)){toast('支持 MP4 / WebM / MOV / AVI / M4V 视频');return;}
    const stationId=selectedStation();
    leaveLive();
    state.busy=true;closeModal();leaveRun();sceneRevision++;video.pause();demo=false;analysisData=null;updateDashboard();renderAlerts();
    const revision=sceneRevision;status(`正在上传：${file.name}`,'uploading');progress(0);
    video.removeAttribute('src');video.load();updateDashboard();
    $('#server-upload-cancel').hidden=false;
    try{
      const media=await sendVideo(file);
      $('#server-upload-cancel').hidden=true;
      status('上传完成，正在创建分析任务','uploading');
      const run=await request('/analysis-runs',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({media_id:media.id,options:{station_id:stationId,workflow_id:selectedWorkflow(),sample_fps:Math.min(5,media.metadata.fps),pose_backend:$('#pose-model')?.value||'rtmo'}})});
      if(sceneRevision===revision)await selectRun(run.id,true);else status('任务已创建，可在服务器任务列表查看');
    }catch(error){
      progress(null);
      if(error.name==='AbortError')status('上传已取消');
      else{status('上传或创建失败：'+error.message,'error');toast(error.message);}
    }finally{state.busy=false;$('#server-upload-cancel').hidden=true;}
  }
  async function selectRun(id,fromUpload=false){
    if(state.busy&&!fromUpload){toast('请先完成或取消上传，再切换任务');return;}
    leaveLive();
    leaveRun();state.active=id;sceneRevision++;demo=false;analysisData=null;video.pause();updateDashboard();renderAlerts();
    video.removeAttribute('src');video.load();updateDashboard();
    const url=new URL(location.href);url.searchParams.delete('video');url.searchParams.set('run',id);history.replaceState(null,'',url);
    status('正在读取任务…','loading');closeModal();await poll(id,state.runGeneration);
  }
  async function poll(id,generation){
    const current=()=>state.active===id&&state.runGeneration===generation;
    if(!current())return;
    const controller=new AbortController();state.runAbort=controller;
    try{
      const path=state.pollUpdatedAt===null?'/analysis-runs/'+id:`/analysis-runs/${id}/wait?since=${encodeURIComponent(state.pollUpdatedAt)}&timeout=25`;
      const run=await request(path,{signal:controller.signal,timeoutMs:32000});if(!current())return;
      state.run=run;
      state.pollUpdatedAt=run.updated_at;
      status(`${labels[run.status]||run.status} · ${id.slice(0,8)}${run.status==='RUNNING'?' · '+Math.floor(run.progress*100)+'%':''}${run.error_message?' · '+run.error_message:''}`,run.status.toLowerCase());
      progress(run.status==='RUNNING'?Math.floor(run.progress*100):null);
      $('#server-cancel').hidden=!['QUEUED','RUNNING','CANCELLING'].includes(run.status);
      $('#server-cancel').disabled=run.status==='CANCELLING';
      $('#server-cancel').textContent=run.status==='CANCELLING'?'正在取消…':'取消当前任务';
      if(run.status==='SUCCEEDED'&&state.loaded!==id){
        const revision=sceneRevision,base='/api/v1/analysis-runs/'+id+'/artifacts/';
        const data=await request('/analysis-runs/'+id+'/artifacts/analysis.json',{signal:controller.signal});
        if(!current()||revision!==sceneRevision)return;
        if(!Array.isArray(data.frames)||!data.source?.duration)throw new Error('分析结果结构无效');
        analysisData=data;analysisBase=base;state.loaded=id;demo=false;video.loop=false;video.src=base+'video.webm';video.load();
        $('.camera-name').textContent=data.source.title;$('.stream-info').textContent=`${data.source.width}×${data.source.height} · ${data.model.name}`;
        $('#mode-text').textContent='服务器实际离线分析 · SOP待人工复核';updateWorkerDashboard();renderAlerts();
      }
      if(['QUEUED','RUNNING','CANCELLING'].includes(run.status))state.timer=setTimeout(()=>poll(id,generation),0);
    }catch(error){
      if(!current())return;
      status(error.status===401?'登录已过期，请重新登录查看任务':error.status===404?'任务不存在或当前账户无权访问':'状态连接中断：'+error.message+'；任务在服务器继续运行','error');
      if(state.user&&![401,403,404].includes(error.status))state.timer=setTimeout(()=>poll(id,generation),5000);
    }finally{if(state.runAbort===controller)state.runAbort=null;}
  }
  async function showHistory(){
    if(!state.user){login(showHistory);return;}
    showModal('服务器分析任务',`<p class="modal-intro">按文件名或任务编号查找。分析在服务器继续运行，完成后可打开结果。</p><div class="history-toolbar"><input id="history-search" class="field" type="search" maxlength="200" placeholder="搜索视频名称或任务编号" aria-label="搜索任务"><select id="history-filter" class="field" aria-label="筛选任务状态"><option value="">全部状态</option>${Object.entries(labels).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')}</select><button class="btn" id="history-refresh">刷新</button></div><div class="server-run-list" id="history-list" aria-live="polite"></div><div class="history-pagination"><span id="history-summary" role="status"></span><button class="btn" id="history-prev">上一页</button><button class="btn" id="history-next">下一页</button></div>`);
    const list=$('#history-list'),search=$('#history-search'),filter=$('#history-filter');
    const summary=$('#history-summary'),prev=$('#history-prev'),next=$('#history-next');
    let cursor=null,nextCursor=null,cursorStack=[],legacyOffset=0,cursorMode=null,revision=0,debounce,controller;
    async function load(){
      const version=++revision;controller?.abort();controller=new AbortController();
      prev.disabled=next.disabled=true;list.setAttribute('aria-busy','true');summary.textContent='正在加载…';
      const query=new URLSearchParams({limit:'20',q:search.value});
      if(cursorMode===false)query.set('offset',String(legacyOffset));
      else if(cursor)query.set('cursor',cursor);
      if(filter.value)query.set('status',filter.value);
      try{
        const data=await request('/analysis-runs?'+query,{signal:controller.signal});
        if(version!==revision||!list.isConnected||!modal.open)return;
        list.innerHTML=data.items.length?data.items.map(r=>`<button class="server-run" data-server-run="${escapeHTML(r.id)}"><div class="run-title"><b>${escapeHTML(r.media_name||'未命名视频')}</b><span class="run-badge" data-status="${escapeHTML(r.status)}">${labels[r.status]||escapeHTML(r.status)}${r.status==='RUNNING'?' '+Math.floor(r.progress*100)+'%':''}</span></div><span>${escapeHTML(r.id.slice(0,8))} · ${new Date(r.created_at*1000).toLocaleString('zh-CN')}</span>${r.error_message?`<small>${escapeHTML(r.error_message)}</small>`:''}</button>`).join(''):'<div class="history-empty">暂无匹配任务<br><small>上传视频开始分析，或调整搜索条件。</small></div>';
        cursorMode=Object.prototype.hasOwnProperty.call(data,'next_cursor');
        nextCursor=data.next_cursor||null;
        summary.textContent=cursorMode&&data.total===null?`当前页 ${data.items.length} 个任务`:`共 ${data.total} 个任务${data.total?' · 当前页 '+data.items.length:''}`;
        prev.disabled=cursorMode?cursorStack.length===0:legacyOffset===0;
        next.disabled=cursorMode?!nextCursor:legacyOffset+20>=data.total;
      }catch(error){if(version===revision&&list.isConnected&&error.name!=='AbortError'){summary.textContent=error.message;list.innerHTML='';}}
      finally{if(version===revision)list.removeAttribute('aria-busy');}
    }
    search.oninput=()=>{clearTimeout(debounce);controller?.abort();revision++;debounce=setTimeout(()=>{cursor=null;cursorStack=[];legacyOffset=0;load();},250);};
    filter.onchange=()=>{clearTimeout(debounce);cursor=null;cursorStack=[];legacyOffset=0;load();};
    $('#history-refresh').onclick=()=>{clearTimeout(debounce);load();};
    prev.onclick=()=>{if(cursorMode===false){legacyOffset=Math.max(0,legacyOffset-20);}else{cursor=cursorStack.pop()||null;}load();};
    next.onclick=()=>{if(cursorMode===false){legacyOffset+=20;load();}else if(nextCursor){cursorStack.push(cursor);cursor=nextCursor;load();}};
    const cleanup=()=>{clearTimeout(debounce);controller?.abort();};
    modal.addEventListener('close',cleanup,{once:true});
    await load();
  }
  async function initialize(){
    let health=null;
    if(!window.AISOP_SERVER){
      try{health=await request('/health',{timeoutMs:10000});if(health.analysis!=='offline-pose')return;}
      catch{return;}
    }
    state.available=true;
    document.body.classList.add('server-mode');
    $('.header').insertAdjacentHTML('afterend','<section class="ops-strip" id="ops-strip" aria-label="服务器分析与工位"><div class="ops-row"><span id="server-status" role="status">服务器已连接 · 请登录后上传分析</span><progress id="server-progress" max="100" value="0" aria-label="当前任务进度" hidden></progress><div class="ops-actions"><button class="btn" id="server-account">登录服务器</button><button class="btn" data-action="server-history">服务器任务</button><button class="btn" id="server-cancel" hidden>取消当前任务</button><button class="btn" id="server-upload-cancel" hidden>取消上传</button></div></div><div class="ops-row"><label for="station-select">检测工位（用于下一次上传或实时检测）</label><select id="station-select" class="field"><option value="">通用人体姿态</option><option value="algorithm-test">算法测试工位（可自选流程）</option></select><div id="station-requirements" role="status"></div><label for="pose-model">离线分析姿态模型</label><select id="pose-model" class="field"><option value="rtmo" selected>RTMO-m 单阶段（最新，rtmlib）</option><option value="rtmpose">RTMPose-m 两阶段（rtmlib）</option><option value="yolo">YOLO11s-pose</option></select></div></section>');
    $('#server-upload-cancel').onclick=()=>state.upload?.abort();
    $('.brand p').textContent='服务器离线视频分析 · 人体检测与姿态规则 · SOP待人工复核';
    $('.cpu-chip').textContent='服务器任务与结果';$('.footer>span').textContent='AI-SOP · 服务器离线分析首版';
    $('.alerts-foot').textContent='分析质量提示 · 非实时违规告警';
    $('.detection-panel .section-title h2').textContent='视频分析';
    $('.alerts-panel .section-title h2').textContent='分析质量提示';
    $('[data-action="workflow"]').textContent='演示流程配置';
    actions.upload=showUpload;actions['start-upload']=showUpload;actions['server-history']=showHistory;
    actions['realtime']=showLive;
    $('.video-panel .subheading').insertAdjacentHTML('afterend','<div class="video-mode-control"><label for="video-mode">视频分析模式</label><select id="video-mode" class="field"><option value="replay">视频分析回放</option><option value="realtime">实时摄像头检测</option></select></div>');
    $('#video-mode').onchange=()=>{if($('#video-mode').value==='realtime')showLive();else leaveLive();};
    setupLabControls();
    $('#station-requirements').insertAdjacentHTML('beforebegin','<label for="action-workflow">通用动作顺序（用于下一次检测）</label><select id="action-workflow" class="field"><option value="">仅观察动作</option></select>');
    let workflowChanged=false,stationChanged=false,savedStation;
    $('#action-workflow').onchange=()=>{workflowChanged=true;if($('#inline-live'))leaveLive();};
    try{savedStation=localStorage.getItem('aisop-station');}catch{}
    if(savedStation==='algorithm-test')$('#station-select').value=savedStation;
    let catalog={items:[]},catalogError='';
    const updateStation=(stopCamera=true,persist=true)=>{
      const id=$('#station-select').value;
      if(persist)try{localStorage.setItem('aisop-station',id||'');}catch{}
      $('#test-controls').hidden=!isLab();$('#action-workflow').disabled=isLab();
      if(stopCamera&&$('#inline-live'))leaveLive();
      const station=catalog.items.find(station=>station.id===id);
      $('#station-requirements').textContent=(station?station.checks.map(check=>`${check.name}：${check.configured?'视觉候选检测已配置；质量待复核':'未配置／待复核'}`).join('；'):isLab()?'测试流程：自行勾选算法、调整顺序；仅用于功能测试。':'人体与姿态检测，不代表灯具工艺检测。')+catalogError;
    };
    $('#station-select').onchange=()=>{stationChanged=true;updateStation();};updateStation(false,false);
    const modelSelect=$('#pose-model');
    let savedModel;try{savedModel=localStorage.getItem('aisop-pose-model');}catch{}
    if(['rtmo','rtmpose','yolo'].includes(savedModel))modelSelect.value=savedModel;
    modelSelect.onchange=()=>{try{localStorage.setItem('aisop-pose-model',modelSelect.value);}catch{}};
    $('#server-account').onclick=async()=>{if(!state.user){login();return;}if(state.busy){toast('请先完成或取消上传');return;}try{await request('/logout',{method:'POST'});state.user=null;state.csrf=null;leaveLive();leaveRun();video.pause();video.removeAttribute('src');video.load();analysisData=null;demo=false;updateDashboard();renderAlerts();renderAccount();status('已退出登录');}catch(error){toast(error.message);}};
    $('#server-cancel').onclick=async()=>{
      const id=state.active,generation=state.runGeneration,button=$('#server-cancel');if(!id)return;
      button.disabled=true;
      try{await request('/analysis-runs/'+id+'/cancel',{method:'POST'});if(state.active===id&&state.runGeneration===generation)status('已请求取消','cancelling');}
      catch(error){toast(error.message);}
      finally{if(state.active===id&&state.runGeneration===generation)button.disabled=false;}
    };
    document.addEventListener('click',e=>{const button=e.target.closest('[data-server-run]');if(button)selectRun(button.dataset.serverRun);});
    const params=new URLSearchParams(location.search),run=params.get('run');
    if(!params.get('video')){sceneRevision++;demo=false;analysisData=null;video.pause();video.removeAttribute('src');video.load();updateDashboard();renderAlerts();$('.camera-name').textContent='尚未选择分析结果';$('.stream-info').textContent='未加载视频';$('#mode-text').textContent='服务器模式 · 上传视频开始分析';}
    status('正在连接服务器…');
    await Promise.allSettled([
      (async()=>{
        try{
          health=health||await request('/health',{timeoutMs:10000});
          state.health=health;state.healthError=null;
          if(health.limits)state.limits=health.limits;
          if(health.analysis!=='offline-pose')throw new Error('分析服务未就绪');
          if($('#server-status').textContent==='正在连接服务器…')status(state.user?'服务器已连接 · 可上传视频或开始摄像头检测':'服务器已连接 · 请登录后上传分析');
        }catch(error){state.health=null;state.healthError=error.message;if($('#server-status').textContent==='正在连接服务器…')status('服务端暂不可用，请检查数据库与服务状态','error');}
      })(),
      (async()=>{
        try{const data=await request('/session',{timeoutMs:10000});state.user=data.username;state.csrf=data.csrf;}catch{}
        renderAccount();
      })(),
      (async()=>{
        try{
          const workflows=await request('/action-workflows',{timeoutMs:10000});
          state.workflowsLoaded=true;
          for(const workflow of workflows.items){const option=document.createElement('option');option.value=workflow.id;option.textContent=workflow.name;$('#action-workflow').append(option);}
          if(!workflowChanged&&!state.busy&&!$('#inline-live')&&workflows.items.some(workflow=>workflow.id==='generic-actions'))$('#action-workflow').value='generic-actions';
        }catch{$('#action-workflow').title='动作顺序加载失败；当前可仅观察动作';}
      })(),
      (async()=>{
        try{
          catalog=await request('/stations',{timeoutMs:10000});
          for(const station of catalog.items){const option=document.createElement('option');option.value=station.id;option.textContent=station.name;$('#station-select').append(option);}
          if(!stationChanged&&!state.busy&&!$('#inline-live')&&(savedStation==='algorithm-test'||catalog.items.some(station=>station.id===savedStation)))$('#station-select').value=savedStation;
        }catch(error){catalogError=' 工位配置加载失败：'+error.message+'；仍可使用通用姿态或算法测试。';}
        updateStation(false,false);
      })(),
    ]);
    if(run){if(state.user)await selectRun(run);else login(()=>selectRun(run));}
  }
  actions['sop-history']=showSequenceHistory;
  initialize();
  return {get available(){return state.available;},
    get runtime(){
      const camera=$('#live-camera');
      return {health:state.health,healthError:state.healthError,user:state.user,run:state.run,
        uploading:state.busy,workflowsLoaded:state.workflowsLoaded,
        live:$('#inline-live')?{phase:livePhase,data:liveData,frames:liveFrames,fps:liveFps,error:liveError,
          sourceReady:!!liveStream?.getVideoTracks().some(track=>track.readyState==='live')&&camera?.readyState>=2,
          width:camera?.videoWidth||liveData?.width||null,height:camera?.videoHeight||liveData?.height||null}:null};
    },
    async refreshDiagnostics(){
      try{const health=await request('/health',{timeoutMs:5000});if(health.analysis!=='offline-pose')throw new Error('分析服务未就绪');state.health=health;state.healthError=null;}
      catch(error){state.health=null;state.healthError=error.message;}
    },
    upload,leaveRun,inspectionHTML,renderSequence};
})();
