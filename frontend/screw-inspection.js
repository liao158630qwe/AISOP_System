/* Dedicated video screw inspection. Every status comes from server inference. */
(() => {
  'use strict';
  const $=selector=>document.querySelector(selector), video=$('#video'), canvas=$('#canvas');
  const state={get session(){return Workspace.session;},model:null,result:null,run:null,runStatus:null,cancelling:false,file:null,busy:false,localUrl:null,pollToken:0};
  const names={PRESENT:'可见螺丝头',EMPTY:'可见空孔',UNKNOWN:'证据不足'};
  const sequenceNames={COMPLETED:'已确认',CURRENT:'待确认',PENDING:'待确认',EARLY_PRESENT:'待确认',EMPTY:'当前空孔',PREASSEMBLED_OK:'预装件已确认',PREASSEMBLED_MISSING:'预装件当前为空',PREASSEMBLED_UNKNOWN:'预装件待确认',UNKNOWN:'证据不足'};
  const colors={PRESENT:'#36d1a6',EMPTY:'#ff8068',UNKNOWN:'#a9b9c2'};
  function status(message,error=false){$('#status').textContent=message;$('#status').classList.toggle('error',error);}
  function save(key,value){try{localStorage.setItem(key,value);}catch{}}
  function read(key){try{return localStorage.getItem(key);}catch{return null;}}
  const api=(path,options={})=>Workspace.request(path,options);
  let initialized=false;
  function requireLogin(){if(state.session)return true;void Workspace.login();return false;}
  function renderTargets(targets){
    const root=$('#targets');root.replaceChildren();
    for(const target of targets){
      const row=document.createElement('div');row.className='target '+((target.sequenceState||target.state)||'UNKNOWN');
      const id=document.createElement('span');id.className='target-id';id.textContent=target.id;
      const label=document.createElement('span');label.className='target-name';label.textContent=target.name;
      if(target.lastSeen!=null){const evidence=document.createElement('small');evidence.textContent=`证据 ${target.lastSeen.toFixed(1)}s · 点击定位`;label.append(evidence);
        row.classList.add('has-evidence');row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label',`${target.id} ${target.name}，定位到 ${target.lastSeen.toFixed(1)} 秒证据`);
        const seek=()=>{video.pause();video.currentTime=target.lastSeen;};row.onclick=seek;row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();seek();}};}
      const value=document.createElement('span');value.className='target-state';value.textContent=sequenceNames[target.sequenceState]||names[target.state]||'待检测';
      row.append(id,label,value);root.append(row);
    }
  }
  function setBusy(busy){state.busy=busy;if(!busy)state.cancelling=false;$('#analyze').disabled=busy||!state.file;$('#file').disabled=busy;$('#cancel').hidden=!busy;$('#progress').hidden=!busy;if(!busy){$('#cancel').disabled=false;$('#cancel').textContent='取消任务';}}
  let finalSeek=false;
  function showFinal(){if(!state.result)return;renderTargets(state.result.targets);$('#list-heading').textContent='最终位置确认汇总';$('#show-final').hidden=true;const t=state.result.summary.assessmentTime;if(t!=null&&Math.abs(video.currentTime-t)>.05){finalSeek=true;video.currentTime=t;}}
  function frameAt(t){const frames=state.result?.frames;if(!frames?.length)return null;let lo=0,hi=frames.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(frames[mid].time<=t)lo=mid;else hi=mid-1;}return frames[lo];}
  function draw(){
    const rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
    if(canvas.width!==Math.round(rect.width*dpr)||canvas.height!==Math.round(rect.height*dpr)){canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);}
    const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,rect.width,rect.height);
    const frame=frameAt(video.currentTime);if(!frame)return;
    const present=frame.inspection.targets.filter(t=>t.state==='PRESENT').length;
    $('#frame-status').textContent=`${video.currentTime.toFixed(1)}s · ${frame.inspection.registered?`本帧可见 ${present}/${frame.inspection.targets.length} 处`:'工件定位证据不足'}`;
    if(!$('#overlay').checked)return;
    const ratio=Math.min(rect.width/(video.videoWidth||16),rect.height/(video.videoHeight||9));
    const w=(video.videoWidth||16)*ratio,h=(video.videoHeight||9)*ratio,ox=(rect.width-w)/2,oy=(rect.height-h)/2;
    const topology=state.result.skeleton;
    if(video.currentTime>=frame.time&&(!Number.isFinite(frame.end)||video.currentTime<frame.end)){
      for(const person of frame.people||[]){
        const [a,b,c,d]=person.bbox,color=person.primary?'#36d1a6':'#53e8ff';
        ctx.strokeStyle=color;ctx.lineWidth=2;ctx.strokeRect(ox+a*w,oy+b*h,(c-a)*w,(d-b)*h);
        const points=person.keypoints||[],visible=i=>points[i]?.[2]>=(topology?.visibilityThreshold??.35);
        const left=new Set(topology?.leftJoints||[]),right=new Set(topology?.rightJoints||[]);
        const jointColor=i=>left.has(i)?'#53e8ff':right.has(i)?'#ffbe63':'#36d1a6';
        for(const [i,j] of topology?.edges||[]){
          if(!visible(i)||!visible(j))continue;
          ctx.beginPath();ctx.moveTo(ox+points[i][0]*w,oy+points[i][1]*h);ctx.lineTo(ox+points[j][0]*w,oy+points[j][1]*h);
          ctx.strokeStyle=jointColor(i);ctx.stroke();
        }
        points.forEach((point,i)=>{if(visible(i)){ctx.beginPath();ctx.arc(ox+point[0]*w,oy+point[1]*h,2.5,0,Math.PI*2);ctx.fillStyle=jointColor(i);ctx.fill();}});
        const overlay=person.skeletonOverlay;
        if(overlay?.neck){const [p,q]=overlay.neck;ctx.beginPath();ctx.moveTo(ox+p[0]*w,oy+p[1]*h);ctx.lineTo(ox+q[0]*w,oy+q[1]*h);ctx.strokeStyle='#36d1a6';ctx.stroke();}
        if(overlay?.head){const head=overlay.head;ctx.beginPath();ctx.ellipse(ox+head.center[0]*w,oy+head.center[1]*h,head.axes[0]*h,head.axes[1]*h,head.rotation,0,Math.PI*2);ctx.strokeStyle='#ffbe63';ctx.stroke();}
        ctx.font='12px Segoe UI';ctx.fillStyle=color;ctx.fillText(`P${person.trackId}${person.primary?' 主操作员':''}`,ox+a*w,Math.max(14,oy+b*h-4));
      }
    }
    for(const target of frame.inspection.targets){if(!target.bbox)continue;
      const [a,b,c,d]=target.bbox;const x=ox+a*w,y=oy+b*h,bw=Math.max((c-a)*w,8),bh=Math.max((d-b)*h,8);
      ctx.strokeStyle=colors[target.state];ctx.lineWidth=1.6;ctx.setLineDash(target.state==='UNKNOWN'?[3,3]:[]);ctx.strokeRect(x-3,y-3,bw+6,bh+6);ctx.setLineDash([]);
      ctx.font='10px Segoe UI';const text=target.id;const tw=ctx.measureText(text).width;ctx.fillStyle=colors[target.state];ctx.fillRect(x-3,y-18,tw+8,13);ctx.fillStyle='#09202a';ctx.fillText(text,x+1,y-8);
    }
  }
  function onFrame(){draw();if('requestVideoFrameCallback'in video)video.requestVideoFrameCallback(onFrame);}
  if('requestVideoFrameCallback'in video)video.requestVideoFrameCallback(onFrame);else video.addEventListener('timeupdate',draw);
  new ResizeObserver(draw).observe(canvas);$('#overlay').onchange=draw;
  video.addEventListener('seeked',()=>{draw();if(finalSeek){finalSeek=false;return;}const frame=frameAt(video.currentTime);if(frame){renderTargets(frame.inspection.targets);$('#list-heading').textContent=`${frame.time.toFixed(1)}s 画面中的位置状态`;$('#show-final').hidden=false;}});
  $('#show-final').onclick=showFinal;
  function artifact(id,name){return `/api/v1/analysis-runs/${encodeURIComponent(id)}/artifacts/${name}`;}
  async function loadResult(id){
    const result=await api(`/analysis-runs/${id}/artifacts/analysis.json`);
    if(result.analysisType!=='screw')throw new Error('该记录属于已停用的旧分析流程，请选择新的螺丝检测记录。');
    state.result=result;state.run=id;state.runStatus='SUCCEEDED';state.cancelling=false;save('aisop-screw-run',id);
    if(state.localUrl){URL.revokeObjectURL(state.localUrl);state.localUrl=null;}
    video.src=artifact(id,'video.webm');$('#placeholder').hidden=true;$('#video-name').textContent=result.source.title;
    $('#present').textContent=result.summary.present;$('#empty').textContent=result.summary.empty;$('#unknown').textContent=result.summary.unknown;
    const completed=result.sequence?.operationsCompleted??0,expected=result.sequence?.operationsExpected??0;
    $('#assessment').textContent=`${result.compliance?.label||'无法判定'} · 操作位置 ${completed}/${expected} · ${result.summary.assessmentTime==null?'工件未能可靠定位':`末次工件 ${result.summary.assessmentTime.toFixed(1)}s`}`;
    renderTargets(result.targets);$('#list-heading').textContent='最终位置确认汇总';$('#show-final').hidden=true;
    $('#downloads').hidden=false;$('#report').href=artifact(id,'report.html');$('#csv').href=artifact(id,'segments.csv');$('#json').href=artifact(id,'analysis.json');
    const timeline=$('#timeline');timeline.replaceChildren();
    const stride=Math.max(1,Math.ceil(result.frames.length/140));
    for(let i=0;i<result.frames.length;i+=stride){const f=result.frames[i],b=document.createElement('button');const p=f.inspection.targets.filter(t=>t.state==='PRESENT').length;
      b.style.background=f.inspection.registered?`hsl(165 35% ${84-p/result.targets.length*40}%)`:'#e4e8eb';
      b.title=`${f.time.toFixed(1)}s · 可见 ${p}/${result.targets.length}`;b.setAttribute('aria-label',b.title);b.onclick=()=>{video.currentTime=f.time;};timeline.append(b);}
    video.addEventListener('loadedmetadata',showFinal,{once:true});
    status(`检测完成：${result.compliance?.label||'无法判定'} · 操作位置 ${completed}/${expected} · 螺丝头 ${result.summary.present}/${result.summary.expected}，空孔 ${result.summary.empty}，证据不足 ${result.summary.unknown}。${result.compliance?.reason||'结果需复核。'}`);
  }
  async function poll(id){
    const token=++state.pollToken;setBusy(true);state.run=id;state.runStatus=null;state.cancelling=false;save('aisop-screw-run',id);
    try{let job=await api(`/analysis-runs/${id}`);
      while(token===state.pollToken){state.runStatus=job.status;$('#progress').value=job.progress*100;
        if(job.status==='CANCELLING')state.cancelling=true;
        if(state.cancelling&&['QUEUED','RUNNING','CANCELLING'].includes(job.status)){status('正在取消任务…');$('#cancel').disabled=true;$('#cancel').textContent='正在取消…';}
        else{status(`${job.status==='QUEUED'?'排队等待分析':job.status==='RUNNING'?'正在检测螺丝位置':job.status} · ${Math.round(job.progress*100)}%`);if(['QUEUED','RUNNING'].includes(job.status)){$('#cancel').disabled=false;$('#cancel').textContent='取消任务';}}
        if(['SUCCEEDED','FAILED','CANCELLED'].includes(job.status))state.cancelling=false;
        if(job.status==='SUCCEEDED'){await loadResult(id);break;}
        if(job.status==='FAILED')throw new Error(job.error_message||'分析失败');
        if(job.status==='CANCELLED'){status('检测任务已取消');break;}
        job=await api(`/analysis-runs/${id}/wait?since=${job.updated_at}&timeout=25`);
      }
    }catch(error){status(error.message,true);}finally{if(token===state.pollToken)setBusy(false);}
  }
  $('#file').onchange=()=>{state.file=$('#file').files[0]||null;$('#filename').textContent=state.file?`${state.file.name} · ${(state.file.size/1024/1024).toFixed(1)} MB`:'支持 MP4 / WebM / MOV · 最大 500 MB';$('#analyze').disabled=!state.file||state.busy;
    if(state.file?.size>500*1024*1024){status('视频超过 500 MB，请先缩短或压缩。',true);$('#analyze').disabled=true;return;}
    if(state.file){state.result=null;$('#downloads').hidden=true;$('#timeline').replaceChildren();for(const id of ['present','empty','unknown'])$('#'+id).textContent='—';$('#assessment').textContent='等待新视频检测';if(state.model)renderTargets(state.model.targets);
      if(state.localUrl)URL.revokeObjectURL(state.localUrl);state.localUrl=URL.createObjectURL(state.file);video.src=state.localUrl;$('#placeholder').hidden=true;$('#video-name').textContent=state.file.name;status('视频已选择，点击“上传并检测”。');}}
  let uploadXHR=null;
  async function upload(file){
    const controller=new AbortController();uploadXHR=controller;
    try{return await Workspace.upload('/media?filename='+encodeURIComponent(file.name),file,percent=>{
      $('#progress').value=percent;status(`正在上传 · ${percent}%`);
    },{signal:controller.signal,timeout:300000});}
    finally{if(uploadXHR===controller)uploadXHR=null;}
  }
  $('#analyze').onclick=async()=>{if(!requireLogin()||!state.file)return;setBusy(true);state.run=null;state.runStatus=null;state.cancelling=false;$('#cancel').textContent='取消上传';$('#cancel').disabled=false;try{const media=await upload(state.file);status('上传完成，正在创建检测任务…');$('#cancel').textContent='取消任务';const job=await api('/analysis-runs',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({media_id:media.id,options:{analysis_type:'screw',sample_fps:4}})});await poll(job.id);}catch(e){status(e.message,true);setBusy(false);}};
  $('#cancel').onclick=async()=>{if(uploadXHR){status('正在取消上传…');uploadXHR.abort();return;}if(!state.run){status(state.busy?'任务正在创建，请稍候…':'当前没有可取消的任务');return;}if(state.cancelling){status('正在取消任务，请稍候…');return;}if(!['QUEUED','RUNNING'].includes(state.runStatus)){status('当前任务已结束，无需取消');return;}const button=$('#cancel');state.cancelling=true;button.disabled=true;button.textContent='正在取消…';status('正在取消任务…');try{const job=await api(`/analysis-runs/${state.run}/cancel`,{method:'POST'});state.runStatus=job.status;if(job.status==='CANCELLED'){state.cancelling=false;status('任务已取消');button.disabled=false;button.textContent='取消任务';}else if(job.status==='CANCELLING'){state.cancelling=true;status('正在取消任务…');}else{state.cancelling=false;status('任务已结束 · '+job.status);button.disabled=false;button.textContent='取消任务';}}catch(e){state.cancelling=false;status(e.message,true);button.disabled=false;button.textContent='取消任务';}};
  $('#account').onclick=async()=>{
    if(!state.session){void Workspace.login();return;}
    const button=$('#account');button.disabled=true;
    try{await Workspace.logout();status('已退出登录');}catch(error){status(error.message,true);}
    finally{button.disabled=false;}
  };
  document.addEventListener('workspace-session',event=>{
    $('#account').textContent=event.detail?event.detail.username+' · 退出':'登录';
    if(event.detail){status('已登录，可以上传检测。');if(initialized&&!state.file)void restore();return;}
    uploadXHR?.abort();state.runStatus=null;state.cancelling=false;state.pollToken++;setBusy(false);
    video.pause();video.removeAttribute('src');video.load();state.result=null;$('#placeholder').hidden=false;$('#downloads').hidden=true;
  });
  $('#history').onclick=async()=>{if(!requireLogin())return;$('#history-dialog').showModal();const root=$('#history-items');root.textContent='正在加载…';try{const response=await api('/analysis-runs?limit=50&analysis_type=screw');root.replaceChildren();for(const job of response.items){const row=document.createElement('div');row.className='history-item';const title=document.createElement('span');title.textContent=job.media_name||job.id;const small=document.createElement('small');small.textContent=`${job.status} · ${new Date(job.created_at*1000).toLocaleString()}`;title.append(small);const button=document.createElement('button');button.textContent=job.status==='SUCCEEDED'?'查看':'查看进度';button.onclick=async()=>{$('#history-dialog').close();if(job.status==='SUCCEEDED'){try{await loadResult(job.id);}catch(e){status(e.message,true);}}else await poll(job.id);};row.append(title,button);root.append(row);}if(!response.items.length)root.textContent='暂无检测记录';}catch(e){root.textContent=e.message;}};
  $('#close-history').onclick=()=>$('#history-dialog').close();
  async function restore(){const id=read('aisop-screw-run')||state.model?.referenceRun;if(!id)return;try{const job=await api(`/analysis-runs/${id}`);if(job.status==='SUCCEEDED')await loadResult(id);else if(['RUNNING','QUEUED','CANCELLING'].includes(job.status))await poll(id);}catch(e){status(e.message,true);}}
  async function init(){
    await Promise.allSettled([
      api('/health').then(h=>{$('#health').textContent=h.status==='ready'?'● 服务在线':'服务启动中';}).catch(()=>{$('#health').textContent='服务器连接失败';}),
      api('/screw-model').then(m=>{state.model=m;$('#model-state').textContent=m.ready?'螺丝专用模型已就绪 · '+m.targets.length+' 处参考位置':'专用模型正在训练';renderTargets(m.targets);const root=$('#training');root.replaceChildren();if(m.training){const metrics=document.createElement('div');metrics.className='train-metrics';for(const [value,label] of [[(m.training.results['metrics/mAP50(B)']*100).toFixed(1)+'%','内部裁剪测试 mAP@50'],[String(m.targets.length),'成品可见参考位置'],['未验证','真实漏打检出率']]){const item=document.createElement('div');const strong=document.createElement('strong');strong.textContent=value;const span=document.createElement('span');span.textContent=label;item.append(strong,span);metrics.append(item);}root.append(metrics);const p=document.createElement('p');p.textContent='YOLO11n 已用原视频实际微调；训练、验证、测试使用分开的时间段。标签由参考点与图像匹配辅助生成，仅有单视频，内部指标不能代表独立现场效果。';root.append(p);}else root.textContent='训练进行中，完成后显示实测记录。';}),
      Workspace.connect().catch(()=>{})
    ]);initialized=true;if(state.session&&!state.file)await restore();
  }
  init();
})();
