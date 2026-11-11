/* Configured scenes use the same API/worker and model snapshots as uploaded jobs. */
(()=>{
  'use strict';
  const W=window.Workspace,$=selector=>document.querySelector(selector),E=W.escape;
  const sceneId=new URL(location.href).searchParams.get('scene');
  const state={scene:null,result:null,frame:null,runId:null,busy:false,running:false,stopping:false,stream:null,streamId:null,generation:0,pending:null,timer:null,started:0,sample:null,received:0};
  const canvas=$('#scene-canvas'),ctx=canvas.getContext('2d'),video=$('#scene-video'),camera=$('#scene-camera');
  const labels={PASS:'满足规则',FAIL:'异常',REVIEW:'待确认',CLEAR:'未检出'};
  function tell(text,error=false){$('#console-message').textContent=text;$('#console-message').classList.toggle('error',error);}
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const time=value=>{const n=Math.max(0,Math.floor(value||0));return String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');};
  function controls(){const ready=!!state.scene?.ready,occupied=state.busy||state.running||state.stopping;$('#upload-button').disabled=!ready||occupied;$('#start-camera').disabled=!ready||occupied;$('#scene-select').disabled=occupied;$('#source-mode').disabled=occupied;$('#camera-select').disabled=occupied;$('#stop-camera').hidden=!state.running;$('#cancel-job').hidden=!state.busy||!state.runId||state.running;$('#report-button').disabled=!state.result||!state.runId;$('#history-button').disabled=state.busy||state.stopping;$('#play-video').disabled=!state.result||occupied;$('#video-seek').disabled=!state.result||occupied;}
  function showFrame(frame){
    state.frame=frame;
    const checks=frame?.checks||state.scene?.rules.map(rule=>({...rule,state:'REVIEW',count:0,reason:'等待检测采样'}))||[];
    $('#rule-counter').textContent=checks.length+' 项';$('#rule-results').innerHTML=checks.map(check=>`<div class="rule-result"><div class="card-top"><strong>${E(check.name)}</strong><span class="badge ${check.state==='FAIL'?'error':check.state==='REVIEW'?'review':check.state==='CLEAR'?'draft':''}">${labels[check.state]||E(check.state)}</span></div><p>可靠检出 <b>${check.count??0}</b> 个${check.class_name?' · '+E(check.class_name):''}</p><p>${E(check.reason||'')}</p></div>`).join('')||'<p class="muted">任务未配置规则。</p>';
    const decision=frame?.compliance;$('#decision').dataset.status=decision?.status||'REVIEW';$('#decision-label').textContent=decision?.label||'等待检测';$('#decision-reason').textContent=decision?.reason||'取得可靠视觉证据后，逐项核对任务规则。';
    const objects=frame?.observations||[];$('#object-count').textContent=String(objects.length)+(frame?.observationsTruncated?'（仅展示前300个）':'');$('#objects-body').innerHTML=objects.length?objects.map(object=>`<tr><td>${E(object.name)}</td><td>${(object.score*100).toFixed(1)}%</td><td class="code">${object.bbox.map(value=>value.toFixed(3)).join(', ')}</td></tr>`).join(''):'<tr><td colspan="3" class="muted">当前采样没有可靠目标</td></tr>';
    $('#processing-time').textContent=frame?Math.round(frame.processingMs??frame.inferenceMs??0):'—';
  }
  function draw(){
    const live=state.running,hasVideo=video.readyState>=2&&!live;
    const fresh=live&&state.received&&performance.now()-state.received<Math.max(2000,3000/(state.scene?.sample_fps||4));
    if(hasVideo){const stamp=video.currentTime;let selected=null;for(const frame of state.result?.frames||[]){if(frame.time<=stamp+.001)selected=frame;else break;}if(selected!==state.frame)showFrame(selected);$('#video-seek').value=stamp;$('#video-clock').textContent=time(stamp)+' / '+time(video.duration);$('#play-video').textContent=video.paused?'播放':'暂停';}
    if(live&&!fresh&&state.frame&&!state.frame.stale){showFrame({...state.frame,stale:true,observations:[],checks:state.frame.checks.map(check=>({...check,state:'REVIEW',count:0,reason:'结果已过期，等待新采样'})),compliance:{status:'REVIEW',label:'采样等待中',reason:'结果已过期，等待新采样'}});}
    let source=live?(fresh?state.sample:camera):hasVideo?video:null;
    if(source&&(source.videoWidth||source.width)){
      const width=source.videoWidth||source.width,height=source.videoHeight||source.height;
      if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
      ctx.drawImage(source,0,0,width,height);$('#console-placeholder').hidden=true;
      const objects=(live?!fresh:!hasVideo)?[]:state.frame?.observations||[];
      ctx.lineWidth=Math.max(2,width/500);ctx.font=`${Math.max(13,width/70)}px sans-serif`;
      for(const object of objects){const [a,b,c,d]=object.bbox;ctx.strokeStyle='#58daba';ctx.fillStyle='#58daba';if(object.polygon?.length){ctx.beginPath();object.polygon.forEach((point,i)=>{const x=point[0]*width,y=point[1]*height;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.closePath();ctx.stroke();}else ctx.strokeRect(a*width,b*height,(c-a)*width,(d-b)*height);ctx.fillText(object.name+' '+Math.round(object.score*100)+'%',Math.max(3,a*width),Math.max(18,b*height-5));for(const point of object.keypoints||[])if(point[2]>=.3){ctx.beginPath();ctx.arc(point[0]*width,point[1]*height,Math.max(2,width/500),0,Math.PI*2);ctx.fill();}}
    }else{ctx.fillStyle='#080d14';ctx.fillRect(0,0,canvas.width,canvas.height);$('#console-placeholder').hidden=false;}
    requestAnimationFrame(draw);
  }
  async function catalog(){
    const data=await W.request('/scenes'),select=$('#scene-select');select.replaceChildren(new Option('螺丝安装检测','builtin:screw'),new Option('贴条工序检测','builtin:sticker'),new Option('人体检测与骨骼','builtin:pose'));
    for(const scene of data.items){const option=new Option(scene.name+(scene.ready?'':'（模型不可用）'),scene.id);option.disabled=!scene.ready;select.add(option);}if(!data.items.some(scene=>scene.id===sceneId))select.add(new Option('当前任务未发布或不可用',sceneId));select.value=sceneId;
  }
  async function loadScene(){
    if(!sceneId){tell('缺少检测任务，请从首页或管理后台选择已发布任务。',true);return;}
    try{state.scene=await W.request('/scenes/'+encodeURIComponent(sceneId));const scene=state.scene;$('#scene-title').textContent=scene.name;document.title=scene.name+' · 视觉检测平台';$('#scene-description').textContent=scene.description||'按已配置的类别、数量与检测区域核对视觉证据。';$('#model-note').textContent=`${scene.model.name} · v${scene.model.version} · ${W.taskLabels[scene.model.task]} · ${scene.imgsz}px · ${scene.sample_fps} fps`;$('#camera-rate').textContent=`任务采样 ${scene.sample_fps} fps · 连续确认 ${scene.confirmation_frames} 帧`;$('#version-note').textContent=`任务 r${scene.revision} · 模型校验 ${scene.model.sha256.slice(0,12)}`;showFrame(null);tell(scene.ready?'任务已就绪 · 选择视频或开启摄像头':scene.readiness_reason||'关联模型尚未就绪',!scene.ready);}
    catch(error){tell(error.message,true);}controls();
  }
  async function release(){const identity=state.streamId;state.streamId=null;if(identity&&W.session)try{await W.request('/realtime/streams/'+identity,{method:'DELETE'});}catch{}}
  async function stopLive(finish=false){
    const identity=state.streamId;state.stopping=true;state.running=false;state.generation++;clearTimeout(state.timer);state.stream?.getTracks().forEach(track=>track.stop());state.stream=null;camera.srcObject=null;controls();
    if(state.pending)await state.pending.catch(()=>{});
    if(finish&&identity&&W.session)try{const result=await W.request('/realtime/streams/'+identity+'/finish?analysis_type=scene',{method:'POST'});showFrame(result);tell('本轮已结束 · 显示最后一次有效采样判定');}catch(error){tell(error.message,true);}
    await release();state.sample=null;state.stopping=false;$('#console-phase').textContent='检测已结束';controls();
  }
  async function enumerateCameras(){const selected=$('#camera-select').value;const devices=await navigator.mediaDevices.enumerateDevices();$('#camera-select').replaceChildren(new Option('默认摄像头',''));for(const device of devices.filter(value=>value.kind==='videoinput'))$('#camera-select').add(new Option(device.label||'摄像头 '+$('#camera-select').length,device.deviceId));$('#camera-select').value=selected;}
  async function startLive(){
    if(!state.scene?.ready)return;if(!W.session&&!await W.login())return;if(state.running||state.busy||state.stopping)return;
    state.busy=true;controls();await stopLive();video.pause();video.removeAttribute('src');video.load();state.result=null;state.runId=null;state.received=0;showFrame(null);controls();
    try{if(!navigator.mediaDevices?.getUserMedia)throw new Error('当前浏览器不可用摄像头，请使用本机地址或 HTTPS');const deviceId=$('#camera-select').value;state.stream=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:1920},height:{ideal:1080},...(deviceId?{deviceId:{exact:deviceId}}:{})},audio:false});camera.srcObject=state.stream;await camera.play();state.running=true;state.started=performance.now();state.streamId=crypto.randomUUID();state.sample=null;state.generation++;$('#sample-count').textContent='0';$('#console-phase').textContent='摄像头检测中';tell('实时检测中 · 规则与目标来自同一采样帧');await enumerateCameras();tick(state.generation);}
    catch(error){await stopLive();tell(error.message,true);}finally{state.busy=false;controls();}
  }
  async function tick(generation){
    if(!state.running||generation!==state.generation)return;
    if(document.hidden){state.timer=setTimeout(()=>tick(generation),300);return;}
    const started=performance.now(),rate=state.scene.sample_fps;
    state.pending=(async()=>{const width=camera.videoWidth,height=camera.videoHeight,scale=Math.min(1,1920/width,1920/height,Math.sqrt(1920*1080/(width*height)));const sample=document.createElement('canvas');sample.width=Math.max(1,Math.floor(width*scale));sample.height=Math.max(1,Math.floor(height*scale));sample.getContext('2d').drawImage(camera,0,0,sample.width,sample.height);let blob=await new Promise(resolve=>sample.toBlob(resolve,'image/jpeg',.9));if(blob.size>1024*1024)blob=await new Promise(resolve=>sample.toBlob(resolve,'image/jpeg',.6));const data=await W.request('/realtime/frame?analysis_type=scene&scene_id='+encodeURIComponent(sceneId),{method:'POST',headers:{'Content-Type':'image/jpeg','X-Stream-Id':state.streamId,'X-Frame-Time':String((started-state.started)/1000)},body:blob});if(generation!==state.generation)return;state.sample=sample;state.received=performance.now();showFrame(data);$('#sample-count').textContent=data.sampleCount;$('#model-note').textContent=`${data.model.name} · v${data.model.version} · 本轮任务 r${data.sceneRevision}`;})();
    try{await state.pending;}
    catch(error){if(generation!==state.generation)return;tell(error.message,error.status!==429);if(error.status!==429){await stopLive();return;}}
    finally{state.pending=null;}
    if(state.running&&generation===state.generation)state.timer=setTimeout(()=>tick(generation),Math.max(25,1000/rate-(performance.now()-started)));
  }
  async function upload(file){
    if(!file||!state.scene?.ready)return;if(!W.session&&!await W.login())return;if(state.busy||state.running||state.stopping)return;
    if(!/\.(mp4|webm|mov|avi|m4v)$/i.test(file.name)){tell('请选择 MP4 / WebM / MOV / AVI / M4V 视频。',true);return;}
    if(!file.size){tell('视频文件为空。',true);return;}state.busy=true;controls();await stopLive();const generation=state.generation;video.pause();video.removeAttribute('src');video.load();state.runId=null;state.result=null;showFrame(null);$('#sample-count').textContent='0';$('#console-phase').textContent='上传中';controls();
    try{const media=await W.upload('/media?filename='+encodeURIComponent(file.name),file,percent=>tell(`视频上传 ${percent}% · ${file.name}`));if(generation!==state.generation)return;const job=await W.request('/analysis-runs',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({media_id:media.id,options:{analysis_type:'scene',scene_id:sceneId}}),timeout:120000});if(generation!==state.generation)return;state.runId=job.id;controls();await watch(job.id,generation);}
    catch(error){tell(error.message,true);$('#console-phase').textContent='检测失败';}finally{state.busy=false;controls();}
  }
  async function loadResult(id){
    $('#source-mode').value='upload';$('#source-mode').onchange();
    const data=await W.request('/analysis-runs/'+id+'/artifacts/analysis.json');if(data.analysisType!=='scene'||data.sceneId!==sceneId)throw new Error('检测记录与当前场景不匹配');state.result=data;state.runId=id;state.received=0;state.sample=null;state.frame=null;video.src='/api/v1/analysis-runs/'+id+'/artifacts/video.webm';video.load();$('#video-seek').disabled=false;$('#play-video').disabled=false;$('#sample-count').textContent=data.summary.analyzedFrames;$('#console-phase').textContent='分析完成 · 可回放';$('#model-note').textContent=`历史任务 r${data.sceneRevision} · ${data.model.name} v${data.model.version} · ${data.summary.sampleFps} fps`;$('#version-note').textContent=`记录 ${id} · 任务配置 ${data.configHash.slice(0,12)}`;showFrame(data.frames[0]||null);tell(`视频分析完成：${data.summary.analyzedFrames} 次采样，可回放查看每个时刻的目标与规则判定。`);controls();
  }
  async function watch(id,generation=state.generation){
    for(;;){const job=await W.request('/analysis-runs/'+id);if(generation!==state.generation)return;if(job.status==='SUCCEEDED'){await loadResult(id);return;}if(['FAILED','CANCELLED'].includes(job.status)){if(job.status==='FAILED')throw new Error(job.error_message||'检测失败');tell('任务已取消');$('#console-phase').textContent='已取消';return;}tell(`后台分析中 · ${Math.round(job.progress*100)}% · ${job.status==='QUEUED'?'等待计算资源':'正在检测'}`);$('#console-phase').textContent='分析 '+Math.round(job.progress*100)+'%';await wait(1000);}
  }
  async function history(){if(!W.session&&!await W.login())return;$('#history-list').textContent='正在读取…';$('#history-dialog').showModal();try{const data=await W.request('/analysis-runs?analysis_type=scene&scene_id='+encodeURIComponent(sceneId)+'&limit=50');$('#history-list').innerHTML=data.items.map(item=>`<button class="history-item" data-run="${item.id}"><span><strong>${E(item.media_name||item.id)}</strong><br><small>${E(item.model_name)} · ${new Date(item.created_at*1000).toLocaleString()}</small></span><span class="badge ${item.status==='FAILED'?'error':'draft'}">${E({SUCCEEDED:'已完成',RUNNING:'检测中',QUEUED:'排队中',FAILED:'失败',CANCELLED:'已取消',CANCELLING:'取消中'}[item.status]||item.status)}</span></button>`).join('')||'<p class="muted">该场景还没有检测记录。</p>';}catch(error){$('#history-list').textContent=error.message;}}
  $('#source-mode').onchange=()=>{const live=$('#source-mode').value==='camera';$('#upload-button').hidden=live;$('#start-camera').hidden=!live;$('#camera-options').hidden=!live;$('#replay-controls').hidden=live;};
  $('#scene-select').onchange=()=>{const value=$('#scene-select').value;location.assign(value.startsWith('builtin:')?'/?task='+value.slice(8):'/?scene='+encodeURIComponent(value));};
  $('#upload-button').onclick=()=>$('#scene-video-upload').click();$('#scene-video-upload').onchange=()=>{const file=$('#scene-video-upload').files[0];$('#scene-video-upload').value='';upload(file);};$('#start-camera').onclick=startLive;$('#stop-camera').onclick=()=>stopLive(true);$('#play-video').onclick=()=>video.paused?video.play().catch(error=>tell(error.message,true)):video.pause();$('#video-seek').oninput=()=>video.currentTime=Number($('#video-seek').value);video.onloadedmetadata=()=>$('#video-seek').max=video.duration;$('#history-button').onclick=history;
  $('#cancel-job').onclick=async()=>{if(!state.runId)return;try{await W.request('/analysis-runs/'+state.runId+'/cancel',{method:'POST'});tell('已请求取消，正在等待后台确认…');}catch(error){tell(error.message,true);}};
  $('#report-button').onclick=()=>{if(state.result&&state.runId)window.open('/api/v1/analysis-runs/'+state.runId+'/artifacts/report.html','_blank','noopener');};
  $('#rules-button').onclick=()=>{const scene=state.result?{...state.result.config,model:state.result.model,revision:state.result.sceneRevision}:state.scene;if(!scene)return W.toast('检测任务尚未读取');$('#rules-content').innerHTML=`<div class="model-detail"><h3>${E(scene.name)} · r${scene.revision}</h3><p>${E(scene.model.name)} v${E(scene.model.version)} · ${E(W.taskLabels[scene.model.task])}</p><p class="hint code">SHA-256 ${E(scene.model.sha256)}</p></div><div class="result-list">${scene.rules.map(rule=>`<div class="rule-result"><strong>${E(rule.name)}</strong><p>${rule.kind==='required'?'必须检出':'禁止出现'} ${E(scene.model.classes[String(rule.class_id)])} · 阈值 ${rule.threshold}</p><p>数量 ${rule.min_count}–${rule.max_count??'不限'} · ROI ${rule.roi.join(', ')}</p></div>`).join('')}</div>`;$('#rules-dialog').showModal();};
  document.addEventListener('click',async event=>{const close=event.target.closest('[data-close]');if(close)$('#'+close.dataset.close).close();const run=event.target.closest('[data-run]');if(run){if(state.busy)return W.toast('当前正在分析，请先完成或取消');$('#history-dialog').close();await stopLive();state.busy=true;state.runId=run.dataset.run;controls();try{await watch(state.runId);}catch(error){tell(error.message,true);}finally{state.busy=false;controls();}}});
  $('#account-button').onclick=async()=>{try{if(W.session){await stopLive();await W.logout();tell('已退出登录');}else await W.login();}catch(error){tell(error.message,true);}};
  document.addEventListener('workspace-session',()=>{if(W.session)return;state.result=null;state.runId=null;state.received=0;video.pause();video.removeAttribute('src');video.load();showFrame(null);$('#sample-count').textContent='0';stopLive().catch(error=>tell(error.message,true));for(const dialog of document.querySelectorAll('dialog[open]'))if(dialog.id!=='workspace-login')dialog.close();controls();});
  window.addEventListener('beforeunload',()=>{state.running=false;clearTimeout(state.timer);state.stream?.getTracks().forEach(track=>track.stop());});window.addEventListener('focus',()=>catalog().catch(()=>{}));window.addEventListener('storage',event=>{if(event.key==='aisop-scene-catalog-changed')catalog().catch(()=>{});});
  window.SceneConsole={state,upload,startLive,stopLive,loadResult};
  Promise.allSettled([W.connect(),catalog(),loadScene()]).then(results=>{for(const result of results)if(result.status==='rejected')tell(result.reason.message,true);});requestAnimationFrame(draw);
})();
