'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icons = {
  upload:'M7 8l5-5 5 5M12 3v12M4 14v6h16v-6',
  download:'M7 10l5 5 5-5M12 3v12M4 16v5h16v-5',
  sliders:'M4 7h16M4 17h16M8 4v6M16 14v6',
  server:'M4 3h16v7H4zM4 14h16v7H4zM7 6h.01M7 17h.01M11 6h6M11 17h6',
  pause:'M8 5v14M16 5v14',play:'M7 4l14 8-14 8z',
  expand:'M8 3H3v5M16 3h5v5M21 16v5h-5M3 16v5h5',
  warning:'M12 3L2 21h20L12 3zM12 9v5M12 17h.01',
  search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  close:'M6 6l12 12M6 18L18 6',
  check:'M5 12l4 4L19 6',
  shield:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3zM8 12l3 3 5-6',
  report:'M6 3h9l4 4v14H6zM15 3v5h5M9 13h6M9 17h6',
};
function icon(name){return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[name] || icons.check}"/></svg>`;}
function paintIcons(root = document){$$('[data-icon]',root).forEach(el => el.innerHTML = icon(el.dataset.icon));}
function escapeHTML(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function readStorage(key,fallback){try{return JSON.parse(localStorage.getItem(key)) ?? fallback;}catch{return fallback;}}
function writeStorage(key,value){try{localStorage.setItem(key,JSON.stringify(value));}catch{toast('浏览器未允许本地保存，本次会话内仍然有效');}}
const defaultWorkflow = {name:'车门玻璃总成装配标准流程',area:'车门玻璃装配作业位',steps:[{name:'进入作业位·取工具',duration:4},{name:'取件·装配后部竖向密封件',duration:27},{name:'交还工具·离场',duration:8}]};
let workflow = readStorage('aisop-workflow',defaultWorkflow);
if(!workflow?.name || !Array.isArray(workflow.steps) || workflow.steps.length<2 || workflow.steps.some(s=>!s.name || !Number.isFinite(s.duration) || s.duration<1)) workflow=structuredClone(defaultWorkflow);
const defaultAlerts = [
  {id:1,severity:'medium',title:'区域人员超限',camera:'CAM010 车门玻璃装配工位·检测视频源',area:'车门玻璃装配作业位',description:'车门玻璃装配作业位 当前 2 人，超过 1 人上限',time:'18:54:14',confirmed:true,by:'值班员',videoAt:19.5},
  {id:2,severity:'medium',title:'区域人员超限',camera:'CAM010 车门玻璃装配工位·检测视频源',area:'车门玻璃装配作业位',description:'车门玻璃装配作业位 当前 2 人，超过 1 人上限',time:'18:52:31',confirmed:true,by:'值班员',videoAt:33.4},
];
let alerts = readStorage('aisop-alerts',defaultAlerts);
if(!Array.isArray(alerts)||alerts.length!==2||alerts.some(a=>!a.title||!a.description)) alerts=structuredClone(defaultAlerts);
let filter='all', demo=!window.AISOP_SERVER, analysisData=null, uploadedURL=null, sessionStart=Date.now(), toastTimer, frameId, workflowDraft;
let analysisBase='assets/worker-analysis/', sceneRevision=0;
let videoFrameId;
const video=$('#source-video'), canvas=$('#monitor'), ctx=canvas.getContext('2d'), modal=$('#modal');
const START=7.8, END=49.8;
// Demo action model: one segment table drives the timeline, detection table and SOP step checks.
const TIMELINE_WINDOW=30, demoDuration=END-START;
const demoSegments=[
  {start:0,end:5,action:'站立',color:'#e6c150'},
  {start:5,end:15,action:'举手取物',color:'#b47af0'},
  {start:15,end:22,action:'行走',color:'#39c98c'},
  {start:22,end:29,action:'举手取物',color:'#b47af0'},
  {start:29,end:35,action:'行走',color:'#39c98c'},
  {start:35,end:42,action:'站立',color:'#e6c150'}];
const demoActionColor=action=>demoSegments.find(s=>s.action===action)?.color||'#e6c150';
function demoActionAt(t){return demoSegments.find(s=>t>=s.start&&t<s.end)?.action||'站立';}
// Expected action for a workflow step = the dominant demo action inside the step's time window.
function expectedStepAction(index){
  const total=workflow.steps.reduce((sum,step)=>sum+step.duration,0);
  let start=0;for(let i=0;i<index;i++)start+=workflow.steps[i].duration;
  const end=Math.min(total,start+workflow.steps[index].duration);
  let best='站立',bestOverlap=-1;
  for(const segment of demoSegments){
    const overlap=Math.min(segment.end,end)-Math.max(segment.start,start);
    if(overlap>bestOverlap){bestOverlap=overlap;best=segment.action;}
  }
  return best;
}
const poster=new Image();poster.src='assets/factory.jpg';poster.onload=()=>{if(video.readyState<2)ctx.drawImage(poster,0,0,1024,576);};
paintIcons();

function toast(message){clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').classList.add('visible');toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),3600);}
function fmtTime(seconds){const n=Math.max(0,Math.floor(seconds || 0));return `${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;}
function showModal(title,html){$('#modal-title').textContent=title;$('#modal-body').innerHTML=html;paintIcons($('#modal-body'));if(!modal.open)modal.showModal();}
function closeModal(){modal.close();}
modal.addEventListener('click',e=>{if(e.target===modal){const r=modal.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeModal();}});

function renderVideo(){
  if(document.hidden||$('#inline-live'))return;
  if(video.readyState>=2){
    if(demo){ctx.drawImage(video,296,260,336,188,0,0,1024,576);}else{
      ctx.fillStyle='#040c19';ctx.fillRect(0,0,canvas.width,canvas.height);
      const ratio=Math.min(canvas.width/video.videoWidth,canvas.height/video.videoHeight);
      const w=video.videoWidth*ratio,h=video.videoHeight*ratio;
      ctx.drawImage(video,(canvas.width-w)/2,(canvas.height-h)/2,w,h);
      if(analysisData)drawAnalysisOverlay((canvas.width-w)/2,(canvas.height-h)/2,w,h);
    }
  }
  if(demo && video.currentTime>=END){video.currentTime=START;}
  const elapsed=demo?Math.max(0,video.currentTime-START):video.currentTime;
  const duration=demo?END-START:(Number.isFinite(video.duration)?video.duration:0);
  $('#video-time').textContent=`${fmtTime(elapsed)} / ${fmtTime(duration)}`;
  $('#video-seek').max=duration;$('#video-seek').value=elapsed;
  updateTimelinePlayhead();
  if(!video.paused){
    if(video.requestVideoFrameCallback)videoFrameId=video.requestVideoFrameCallback(renderVideo);
    else frameId=requestAnimationFrame(renderVideo);
  }
}
function updateTimelinePlayhead(){
  const head=$('#timeline-playhead');if(!head)return;
  let fraction=null;
  if(demo){
    const t=Math.max(0,Math.min(video.currentTime-START,demoDuration));
    const start=Math.max(0,Math.min(t-TIMELINE_WINDOW,demoDuration-TIMELINE_WINDOW));
    fraction=(t-start)/TIMELINE_WINDOW;
  }else if(analysisData){
    const duration=Number.isFinite(video.duration)&&video.duration>0?video.duration:analysisData.source?.duration;
    if(duration>0)fraction=video.currentTime/duration;
  }
  if(fraction==null||!Number.isFinite(fraction)){head.hidden=true;return;}
  head.hidden=false;
  head.style.left=`${(Math.min(1,Math.max(0,fraction))*100).toFixed(2)}%`;
  head.firstElementChild.textContent=fmtTime(demo?Math.max(0,video.currentTime-START):video.currentTime);
}
function renderDemoTimeline(t){
  const start=Math.max(0,Math.min(t-TIMELINE_WINDOW,demoDuration-TIMELINE_WINDOW)),end=start+TIMELINE_WINDOW;
  let bars='';
  for(const segment of demoSegments){
    const from=Math.max(segment.start,start),to=Math.min(segment.end,end);
    if(to<=from)continue;
    const width=(to-from)/TIMELINE_WINDOW*100;
    bars+=`<span title="${segment.action} ${segment.start}–${segment.end}s" style="width:${width.toFixed(2)}%;background:${segment.color}">${width>=15?segment.action:''}</span>`;
  }
  const timeline=$('#time-bars');
  Object.assign(timeline.style,{left:'0',width:'100%',maxWidth:'100%'});
  timeline.innerHTML=bars;
  $$('.axis span').forEach((el,i)=>el.textContent=`${Math.round(start+(end-start)*i/6)}s`);
  $('.timeline-panel .micro-tag').textContent=`最近 ${TIMELINE_WINDOW}s`;
  $('.legend').innerHTML=[...new Set(demoSegments.map(s=>s.action))].map(action=>`<span><i style="background:${demoActionColor(action)}"></i>${action}</span>`).join('');
  updateTimelinePlayhead();
}
function requestVideoRender(){
  cancelAnimationFrame(frameId);
  if(videoFrameId!==undefined)video.cancelVideoFrameCallback?.(videoFrameId);
  frameId=requestAnimationFrame(renderVideo);
}
for(const event of ['play','pause','loadeddata','seeked','emptied'])video.addEventListener(event,requestVideoRender);
document.addEventListener('visibilitychange',()=>{requestVideoRender();if(!document.hidden)updateDashboard();});
// 骨骼拓扑以服务端 algorithms/skeleton.py 的 topology() 为准；此常量是静态预览等
// 无接口场景的兜底，字段与 topology() 保持一致（tests/test_skeleton.py 校验一致性）。
const fallbackSkeleton={"jointCount":17,"edges":[[5,6],[5,7],[7,9],[6,8],[8,10],[5,11],[6,12],[11,12],[11,13],[13,15],[12,14],[14,16],[0,1],[0,2],[1,3],[2,4]],"leftJoints":[1,3,5,7,9,11,13,15],"rightJoints":[2,4,6,8,10,12,14,16],"handChains":[[0,1,2,3,4],[0,5,6,7,8],[5,9,10,11,12],[9,13,14,15,16],[13,17,18,19,20],[0,17]],"visibilityThreshold":0.35};
let poseSkeleton=fallbackSkeleton;
let adoptedTopology=null;
function adoptSkeletonTopology(topology){
  if(!topology||!Array.isArray(topology.edges)||!topology.edges.length)return false;
  if(topology===adoptedTopology)return true;
  const jointCount=Number.isInteger(topology.jointCount)&&topology.jointCount>=17?topology.jointCount:fallbackSkeleton.jointCount;
  const validEdge=edge=>Array.isArray(edge)&&edge.length===2&&edge.every(i=>Number.isInteger(i)&&i>=0&&i<jointCount);
  if(!topology.edges.every(validEdge))return false;
  const jointList=value=>Array.isArray(value)&&value.every(i=>Number.isInteger(i)&&i>=0&&i<jointCount)?value:null;
  const chains=Array.isArray(topology.handChains)&&topology.handChains.every(chain=>Array.isArray(chain)&&chain.length>=2&&chain.every(i=>Number.isInteger(i)&&i>=0&&i<21))?topology.handChains:fallbackSkeleton.handChains;
  poseSkeleton={jointCount,edges:topology.edges,leftJoints:jointList(topology.leftJoints)||fallbackSkeleton.leftJoints,rightJoints:jointList(topology.rightJoints)||fallbackSkeleton.rightJoints,handChains:chains,visibilityThreshold:Number.isFinite(topology.visibilityThreshold)?topology.visibilityThreshold:fallbackSkeleton.visibilityThreshold};
  adoptedTopology=topology;
  return true;
}
function drawPoseSkeleton(context,points,x,y,w,h){
  if(!Array.isArray(points))return;
  const visible=i=>points[i]?.length>=3&&points[i].every(Number.isFinite)&&points[i][2]>=poseSkeleton.visibilityThreshold;
  const left=new Set(poseSkeleton.leftJoints),right=new Set(poseSkeleton.rightJoints);
  const color=i=>left.has(i)?'#53e8ff':right.has(i)?'#ffbe63':'#ecf7ff';
  const scale=Math.max(1,Math.min(w,h)/420),line=2.4*scale,radius=3.2*scale;
  context.save();context.lineCap='round';context.lineJoin='round';
  context.beginPath();context.rect(x,y,w,h);context.clip();
  for(const [a,b] of poseSkeleton.edges){
    if(!visible(a)||!visible(b))continue;
    const pa=points[a],pb=points[b];
    context.globalAlpha=Math.min(pa[2],pb[2])<.5?.55:1;
    context.beginPath();context.moveTo(x+pa[0]*w,y+pa[1]*h);context.lineTo(x+pb[0]*w,y+pb[1]*h);
    context.strokeStyle='#03101fe6';context.lineWidth=line+3*scale;context.stroke();
    context.strokeStyle=left.has(a)&&left.has(b)?color(a):right.has(a)&&right.has(b)?color(a):'#9ff2bd';
    context.lineWidth=line;context.stroke();
  }
  for(let i=0;i<Math.min(points.length,poseSkeleton.jointCount);i++){
    if(!visible(i))continue;
    const p=points[i];context.globalAlpha=p[2]<.5?.55:1;
    context.beginPath();context.arc(x+p[0]*w,y+p[1]*h,i<5?radius*.65:radius,0,Math.PI*2);
    context.fillStyle=color(i);context.fill();context.strokeStyle='#03101f';context.lineWidth=1.5*scale;context.stroke();
  }
  context.restore();
}
function analysisFrame(){
  const frames=analysisData?.frames;if(!frames?.length)return null;
  const t=video.currentTime;let lo=0,hi=frames.length;
  while(lo<hi){const mid=(lo+hi)>>>1;if(frames[mid].time<=t)lo=mid+1;else hi=mid;}
  const f=frames[lo-1];return f&&t<f.end?f:null;
}
function drawAnalysisOverlay(x,y,w,h){
  const frame=analysisFrame();if(!frame)return;
  if(analysisData?.skeleton)adoptSkeletonTopology(analysisData.skeleton);
  ctx.save();ctx.lineWidth=Math.max(2,w/450);ctx.font=`${Math.max(13,w/65)}px "Microsoft YaHei"`;
  for(const p of frame.people){
    const b=p.bbox;ctx.strokeStyle='#33d7ef';ctx.fillStyle='#33d7ef';ctx.strokeRect(x+b[0]*w,y+b[1]*h,(b[2]-b[0])*w,(b[3]-b[1])*h);
    const label=`P${String(p.trackId).padStart(2,'0')} ${p.label} ${(p.confidence*100).toFixed(0)}%`,ty=Math.max(y+18,y+b[1]*h-5);const tw=ctx.measureText(label).width+10;ctx.fillStyle='#062a3cdd';ctx.fillRect(x+b[0]*w,ty-17,tw,20);ctx.fillStyle='#65e9f8';ctx.fillText(label,x+b[0]*w+5,ty-2);
    drawPoseSkeleton(ctx,p.keypoints,x,y,w,h);
  }
  ctx.restore();
}
video.addEventListener('loadedmetadata',()=>{
  video.currentTime=demo?START:0;
  video.play().catch(()=>{toast('点击画面中的播放按钮开始回放');updatePlayButton();});
});
video.addEventListener('error',()=>{toast('视频无法播放，请上传浏览器支持的 MP4 / WebM 视频');$('#mode-text').textContent='视频加载失败 · 请重新选择文件';updatePlayButton();});
video.addEventListener('play',updatePlayButton);video.addEventListener('pause',updatePlayButton);video.addEventListener('seeked',updateDashboard);
video.addEventListener('ended',()=>{if(demo){video.currentTime=START;video.play().catch(()=>{});}else updatePlayButton();});
function updatePlayButton(){const paused=video.paused;$('#play-button').innerHTML=icon(paused?'play':'pause');$('#play-button').title=paused?'播放视频':'暂停视频';$('#play-button').setAttribute('aria-label',paused?'播放视频':'暂停视频');$('.video-frame').classList.toggle('paused',paused);$('.live').textContent=paused?'已暂停':analysisData?'离线回放':demo?'演示回放':'视频回放';}
$('#play-button').addEventListener('click',()=>{if(video.paused)video.play().catch(()=>toast('此视频格式不受浏览器支持'));else video.pause();});
$('#video-seek').addEventListener('input',e=>video.currentTime=Number(e.target.value)+(demo?START:0));
renderVideo();

function currentState(){
  const t=Math.max(0,video.currentTime-START);
  const id=t<16?79:t<29?86:93;
  const action=demoActionAt(t);
  const total=workflow.steps.reduce((sum,step)=>sum+step.duration,0),elapsed=t%total;
  let cursor=0,active=workflow.steps.findIndex(step=>(cursor+=step.duration)>elapsed);
  if(active<0)active=workflow.steps.length-1;
  return {t,id,action,active,elapsed,fps:(4.3+Math.sin(t*.7)*.7+Math.cos(t*.21)*.5).toFixed(1),latency:(24.5+Math.sin(t*.43)*2.4).toFixed(1),cpu:Math.round(75+Math.sin(t*.6)*10),memory:468+Math.round(Math.sin(t*.27)*5),dwell:(3.2+t%13).toFixed(1),confidence:Math.round(70+Math.sin(t*.3)*8)};
}
function updateDashboard(){
  if($('#inline-live'))return;
  $('#video-empty').hidden=!(window.AISOP_SERVER&&!demo&&!analysisData&&!video.getAttribute('src'));
  $('#noncompliant-count').textContent=demo?'0':'—';
  const s=currentState(), pending=demo?alerts.filter(a=>!a.confirmed).length:0;
  $('#clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  $('#run-state').textContent=window.ServerApp?.available?`页面会话 ${Math.floor((Date.now()-sessionStart)/60000)}分`:`${video.paused?'已暂停':'运行中'} · 运行 ${Math.floor((Date.now()-sessionStart)/60000)+3}分`;
  if(analysisData){updateWorkerDashboard();return;}
  $$('.temporal-controls,.temporal-state,.temporal-segments').forEach(el=>el.hidden=true);
  $('#metric-fps').textContent=demo?s.fps:'—';if($('#title-fps'))$('#title-fps').textContent=demo?s.fps:'—';$('#sample-fps').textContent=demo?s.fps:'—';
  $('#metric-latency').textContent=demo?s.latency:'—';$('#metric-cpu').textContent=demo?s.cpu:'—';$('#metric-memory').textContent=demo?s.memory:'—';
  $('#metric-alerts').textContent=pending;$('#metric-targets').textContent=demo?'1':'—';
  // SOP counts derive from the same segment model that drives the timeline.
  const matched=demo&&s.action===expectedStepAction(s.active);
  const compliantCount=demo?s.active+(matched?1:0):0;
  const complianceRate=demo?Math.round(compliantCount/workflow.steps.length*100):null;
  $('#metric-compliance').textContent=demo?complianceRate:'—';$('#video-compliance').textContent=demo?complianceRate+'%':'—';$('#compliance-rate').textContent=demo?complianceRate+'%':'—';
  $('#compliant-count').textContent=demo?compliantCount:'—';$('#realtime-alert').textContent=demo?(pending?`${pending} 条待处置`:'无'):'待识别';
  $('#current-process').textContent=demo?`1 在岗 / 1 绑定 · ${workflow.steps[s.active].name}`:'待接入识别引擎';$('#dwell').textContent=demo?`${s.dwell} s`:'—';
  $('#detection-results').innerHTML=demo?`<tr><td>#${s.id}</td><td><span class="action-text" style="color:${demoActionColor(s.action)}">${s.action}</span> <small>${(s.t%8+1).toFixed(1)}s</small></td><td>${s.confidence}%</td><td class="cell-ellipsis" title="${escapeHTML(workflow.area)}">${escapeHTML(workflow.area)}</td><td>${s.dwell}s</td><td class="cell-ellipsis" title="${escapeHTML(workflow.steps[s.active].name)}">${escapeHTML(workflow.steps[s.active].name)}</td></tr>`:'';
  $('#result-note').textContent=demo?'每一帧，都有迹可循':'等待识别引擎返回检测结果';
  $('#track-name').textContent=demo?`ID${s.id}`:'—';
  if(demo)renderDemoTimeline(s.t);else $('#time-bars').innerHTML='';
  $('#footer-performance').textContent=demo?`配置 30fps · 全局 34.1fps · 主画面 ${s.fps}fps · 延迟 ${s.latency}ms · 丢帧 0`:'本地视频回放 · 识别引擎尚未连接';
  $('.compliance-foot').innerHTML=demo?'<span class="dot green"></span>工位安全状态正常':'等待识别结果';
  $('.recent-cycles').hidden=!demo;
  $('.recent-cycles').style.display=demo?'':'none';
  renderWorkflow(s);
}
const analysisColors={standing:'#3fcf9b',walking:'#5aa7ff',raised:'#b17bfd',reaching:'#efb956',bending:'#ed8396',unknown:'#7890ac'};
const analysisIndexes=new WeakMap();
let renderedWorker=null;
const actionNames={standing:'站立 / 原地作业',walking:'位置移动候选',raised:'抬手',reaching:'手臂伸出',bending:'躯干前倾',unknown:'姿态不确定'};
let temporalSource=null, temporalTrack=null, liveTimelineFrames=[];
function temporalControls(ids,source,onChange){
  if(!$('#temporal-track')){
    $('.timeline').insertAdjacentHTML('beforebegin','<div class="temporal-controls"><label for="temporal-track">人物轨迹</label><select id="temporal-track" aria-label="选择时间轴人物"></select></div><div id="temporal-state" class="temporal-state"></div>');
    $('.timeline').insertAdjacentHTML('afterend','<div id="temporal-segments" class="temporal-segments" aria-label="动作片段列表"></div>');
  }
  if(temporalSource!==source){temporalSource=source;temporalTrack=null;}
  $$('.temporal-controls,.temporal-state,.temporal-segments').forEach(el=>el.hidden=false);
  if(temporalTrack==null)temporalTrack=ids[0]??null;
  // Keep a selected person even after they leave the rolling window.
  if(temporalTrack!=null&&!ids.includes(temporalTrack))ids=[temporalTrack,...ids];
  const select=$('#temporal-track'),options=ids.map(id=>`<option value="${id}">P${String(id).padStart(2,'0')}</option>`).join('');
  if(select.innerHTML!==options)select.innerHTML=options;
  select.value=temporalTrack;select.disabled=!ids.length;
  select.onchange=()=>{temporalTrack=Number(select.value);onChange();};
  return temporalTrack;
}
function renderTemporalState(person){
  const target=$('#temporal-state');if(!target)return;
  if(!person){target.textContent='当前未检出该轨迹 · 空白区间表示缺少观测';return;}
  const temporal=person.temporal,candidate=temporal?.candidate;
  const held=Number(temporal?.candidateSeconds)||0,required=Number(temporal?.requiredSeconds)||.3;
  target.innerHTML=`<strong>当前：${escapeHTML(person.label||actionNames[person.action]||'不确定')}</strong>`+
    (candidate?`<span>候选：${escapeHTML(actionNames[candidate]||candidate)} · ${held.toFixed(2)} / ${required.toFixed(2)}s</span><progress max="${required}" value="${Math.min(held,required)}" aria-label="候选动作有效证据进度"></progress>`:'<span>等待持续动作证据</span>');
}
function resetLiveTimeline(){liveTimelineFrames=[];temporalSource=null;temporalTrack=null;renderLiveTimeline([],0,false);}
function renderLiveTimeline(people,t,append=true){
  if(append){
    liveTimelineFrames.push({time:t,people});
    liveTimelineFrames=liveTimelineFrames.filter(f=>f.time>=t-30).slice(-1000);
  }
  const ids=[...new Set(liveTimelineFrames.flatMap(f=>f.people.map(p=>p.trackId)))].sort((a,b)=>a-b);
  const track=temporalControls(ids,'live',()=>renderLiveTimeline(people,t,false));
  const start=Math.max(0,t-30),duration=30,bars=[];
  for(let i=0;i<liveTimelineFrames.length-1;i++){
    const f=liveTimelineFrames[i],next=liveTimelineFrames[i+1],p=f.people.find(p=>p.trackId===track);
    if(!p||next.time-f.time>.75||!next.people.some(p=>p.trackId===track))continue;
    const end=next.time,left=Math.max(start,f.time);
    if(end>left)bars.push(`<span title="${escapeHTML(p.label)} ${left.toFixed(1)}–${end.toFixed(1)}s" style="position:absolute;left:${(left-start)/duration*100}%;width:${(end-left)/duration*100}%;background:${analysisColors[p.action]||analysisColors.unknown}"></span>`);
  }
  $('#time-bars').innerHTML=bars.join('');$('#time-bars')._bars=null;$('#time-bars').style.position='relative';
  $('#track-name').textContent=track==null?'—':`P${String(track).padStart(2,'0')}`;
  $('.timeline-panel .micro-tag').textContent='实时 · 最近30s';
  $$('.axis span').forEach((el,i)=>el.textContent=`${(start+i*5).toFixed(0)}s`);
  $('#timeline-playhead').hidden=true;
  $('#temporal-segments').textContent='颜色为采样间显示保持；空白为未观测或检测中断。';
  $('.legend').innerHTML=Object.entries(actionNames).map(([key,label])=>`<span><i style="background:${analysisColors[key]}"></i>${label}</span>`).join('');
  renderTemporalState(people.find(p=>p.trackId===track));
}
function analysisIndex(data){
  if(analysisIndexes.has(data))return analysisIndexes.get(data);
  const index={tracks:new Map(data.tracks.map(track=>[track.id,track])),segments:new Map(),timelines:new Map()};
  for(const segment of data.segments){
    if(!index.segments.has(segment.trackId))index.segments.set(segment.trackId,[]);
    index.segments.get(segment.trackId).push(segment);
  }
  for(const segments of index.segments.values())segments.sort((a,b)=>a.start-b.start);
  analysisIndexes.set(data,index);return index;
}
function timelineHTML(index,trackId,duration){
  if(index.timelines.has(trackId))return index.timelines.get(trackId);
  let cursor=0,bars='';
  for(const segment of index.segments.get(trackId)||[]){
    if(segment.start>cursor)bars+=`<span style="width:${(segment.start-cursor)/duration*100}%;background:transparent"></span>`;
    bars+=`<span data-start="${segment.start}" data-end="${segment.end}" title="${escapeHTML(segment.label)} ${segment.start.toFixed(1)}–${segment.end.toFixed(1)}s" style="width:${segment.duration/duration*100}%;background:${analysisColors[segment.action]}">${segment.duration>=1.5?escapeHTML(segment.label):''}</span>`;
    cursor=segment.end;
  }
  if(cursor<duration)bars+=`<span style="width:${(duration-cursor)/duration*100}%;background:transparent"></span>`;
  index.timelines.set(trackId,bars);return bars;
}
function updateWorkerDashboard(){
  const sample=analysisFrame();
  if(renderedWorker?.data===analysisData&&renderedWorker.frame===sample&&renderedWorker.revision===sceneRevision)return;
  renderedWorker={data:analysisData,frame:sample,revision:sceneRevision};
  const index=analysisIndex(analysisData);
  $('#noncompliant-count').textContent='—';
  const frame=sample||{people:[],time:0},people=frame.people||[],summary=analysisData.summary,source=analysisData.source;
  const set=(id,value)=>{$(id).textContent=value;};
  set('#metric-fps',analysisData.model.sampleFps.toFixed(1));if($('#title-fps'))set('#title-fps',analysisData.model.sampleFps.toFixed(1));set('#sample-fps',analysisData.model.sampleFps.toFixed(1));
  set('#metric-latency',summary.meanInferenceMs.toFixed(1));set('#metric-cpu','—');set('#metric-memory','—');set('#metric-targets',people.length);set('#metric-alerts',analysisData.events.length);set('#metric-compliance','—');
  set('#video-compliance','待人工复核');set('#compliance-rate','—');set('#compliant-count','—');set('#realtime-alert',analysisData.events.length?'需复核':'无');set('#current-process',`${people.length} 人检出 / ${summary.maxPeople} 人峰值`);set('#dwell',`${frame.time.toFixed(1)} s`);
  $('#detection-results').innerHTML=people.length?people.map(p=>{const tr=index.tracks.get(p.trackId);return `<tr data-track="${p.trackId}"><td>P${String(p.trackId).padStart(2,'0')}</td><td><span class="action-text" style="color:${analysisColors[p.action]}">${escapeHTML(p.label)}</span></td><td>${(p.confidence*100).toFixed(1)}%</td><td class="cell-ellipsis">${escapeHTML(source.title||'视频分析')}</td><td>${tr?tr.visibleSeconds.toFixed(1):'—'}s</td><td class="cell-ellipsis" title="${escapeHTML(p.evidence)}">实际姿态分析</td></tr>`;}).join(''):'<tr><td colspan="6">当前采样点没有达到阈值的人体检测；不代表人员离岗</td></tr>';
  $('#result-note').textContent=`实际模型 · ${source.width}×${source.height} · ${summary.analyzedFrames} 个采样帧`;
  $('.timeline-panel .micro-tag').textContent='完整片段';
  $('.legend').innerHTML=(analysisData.actionSummary||[]).map(item=>`<span><i style="background:${analysisColors[item.action]||'#7890ac'}"></i>${escapeHTML(item.label)}</span>`).join('');
  const trackId=temporalControls(analysisData.tracks.map(t=>t.id),analysisData,()=>{renderedWorker=null;updateWorkerDashboard();});
  renderTemporalState(people.find(p=>p.trackId===trackId));
  set('#track-name',trackId!=null?`P${String(trackId).padStart(2,'0')}`:'—');
  const bars=timelineHTML(index,trackId,source.duration),timeline=$('#time-bars');
  if(timeline._bars!==bars||timeline._revision!==sceneRevision){timeline.innerHTML=bars;timeline._bars=bars;timeline._revision=sceneRevision;}
  const segments=index.segments.get(trackId)||[];
  $('#temporal-segments').innerHTML=segments.map(s=>`<button type="button" data-seek-action="${s.start}" class="action-segment ${s.start<=video.currentTime&&video.currentTime<s.end?'active':''}"><i style="background:${analysisColors[s.action]||analysisColors.unknown}"></i>${escapeHTML(s.label)} <small>${s.start.toFixed(1)}–${s.end.toFixed(1)}s</small></button>`).join('')||'该轨迹暂无动作片段';
  $('#temporal-segments').onclick=event=>{const button=event.target.closest('[data-seek-action]');if(button){video.pause();video.currentTime=Number(button.dataset.seekAction);}};
  Object.assign(timeline.style,{left:'0',width:'100%',maxWidth:'100%'});$$('.axis span').forEach((el,i)=>el.textContent=`${(source.duration*i/6).toFixed(i%2?1:0)}s`);
  const now=video.currentTime;
  for(const el of $$('#time-bars span[data-start]'))el.classList.toggle('current',+el.dataset.start<=now&&now<+el.dataset.end);
  updateTimelinePlayhead();
  $('#footer-performance').textContent=`实际离线分析 · ${analysisData.model.name} · ${summary.meanInferenceMs.toFixed(1)}ms/采样帧 · 检出率 ${(summary.presenceRate*100).toFixed(1)}%`;
  $('.compliance-foot').innerHTML='<span class="dot yellow"></span>已完成人体与姿态分析 · SOP 结论待人工复核';$('.recent-cycles').style.display='none';
  $('#workflow-status').textContent='证据不足 · 待人工复核';$('#workflow-status').className='badge amber';$('#step-counter').textContent='0/3 步';$('#workflow-name').textContent='SOP证据检查（未配置正式工艺）';$('#workflow-description').textContent=`实际检测 ${summary.analyzedFrames} 帧 · 人员检出率 ${(summary.presenceRate*100).toFixed(1)}% · 轨迹段 ${summary.trackCount} 条（非人数）`;
  $('#steps').innerHTML=analysisData.sop.map((step,i)=>`<div class="step"><div class="step-top"><span class="step-index">${i+1}</span><span class="step-name">${escapeHTML(step.name)}</span><span class="step-status">待复核</span></div><div class="step-detail">${escapeHTML(step.reason)}</div></div>`).join('');
  $('#pending-step').innerHTML=`${icon('warning')}<span>未接入工具、工件和设备状态，不能判定工序合格</span>`;
  const sequence=analysisFrame()?.actionSequence;
  if(sequence&&analysisData.actionSequence&&window.ServerApp){
    window.ServerApp.renderSequence({...analysisData.actionSequence,...sequence});
    return;
  }
  const inspection=analysisFrame()?.inspection;
  if(inspection&&window.ServerApp){
    $('#workflow-name').textContent=inspection.stationName+' · 工位检查';
    $('#step-counter').textContent=`${inspection.checks.length} 项待复核`;
    $('#steps').innerHTML=window.ServerApp.inspectionHTML(inspection);
  }
}
function renderWorkflow(s){
  const elapsed=s.elapsed,active=demo?s.active:-1;
  $('#workflow-name').textContent=workflow.name;
  $('#workflow-status').textContent=demo?`进行中 · 第${active+1}/${workflow.steps.length}步`:'等待识别';
  $('#step-counter').textContent=`${Math.max(0,active+1)}/${workflow.steps.length} 步`;
  $('#workflow-description').textContent=demo?`区域 ${workflow.area} · 已合格 ${active}/${workflow.steps.length} 步 · 本周期已运行 ${Math.floor(elapsed)}s · 目标 #${s.id}`:`区域 ${workflow.area} · 当前视频尚无识别结果`;
  let stepStart=0;for(let i=0;i<active;i++)stepStart+=workflow.steps[i].duration;
  $('#steps').innerHTML=workflow.steps.map((step,i)=>{
    const expected=expectedStepAction(i);
    let progress=0,status='未开始',tag='',detail=`等待前置步骤完成 · 阶段时长 ${step.duration}s · 预期动作 ${expected}`;
    if(i<active){progress=100;status='合格';detail=`配置时长 ${step.duration}s · 预期动作 ${expected} · 已完成`;}
    else if(i===active){
      const stepElapsed=Math.min(step.duration,Math.max(0,elapsed-stepStart));
      const matched=s.action===expected;
      progress=Math.round(stepElapsed/step.duration*100);
      status=`进行中 ${Math.round(stepElapsed)}/${step.duration}s`;
      tag=`<span class="step-tag ${matched?'ok':'warn'}">${matched?'动作匹配':'动作偏离'}</span>`;
      detail=`预期动作 ${expected} · 观察到 ${s.action} · 置信 ${s.confidence}% · 阶段时长 ${step.duration}s`;
    }
    return `<div class="step ${i<active?'completed':i===active?'active':''}"><div class="step-top"><span class="step-index">${i+1}</span><span class="step-name">${escapeHTML(step.name)}</span>${tag}<span class="step-status">${status}</span></div><div class="step-progress" aria-hidden="true"><i style="width:${progress}%"></i></div><div class="step-detail">${detail}</div></div>`;
  }).join('');
  $('#pending-step').innerHTML=`${icon('warning')}<span>${demo?'未完成步骤：'+escapeHTML(workflow.steps[active].name):'等待接入识别引擎，暂不进行合规判定'}</span>`;
}
setInterval(()=>{if(!document.hidden)updateDashboard();},1000);updateDashboard();

function filteredAlerts(){if(!demo)return [];const query=$('#alert-search').value.trim().toLowerCase();return alerts.filter(a=>(filter==='all'||a.severity===filter)&&`${a.title} ${a.camera} ${a.area} ${a.by||''}`.toLowerCase().includes(query));}
const severityLabels={urgent:'紧急',high:'高',medium:'中',low:'低'};
function renderAlerts(){
  if(analysisData){renderWorkerAlerts();return;}
  const list=filteredAlerts(),pending=demo?alerts.filter(a=>!a.confirmed).length:0;
  $('#alert-summary').textContent=demo?`累计 ${alerts.length} · 待处置 ${pending}`:'当前视频 · 待接入检测';$('#metric-alerts').textContent=pending;$('#alert-count').textContent=`${list.length} 条记录`;
  $('#alert-list').innerHTML=list.length?list.map(a=>`<article class="alert-card ${a.confirmed?'confirmed':''}"><img class="alert-thumbnail" src="assets/factory.jpg" alt="${escapeHTML(a.title)}现场快照" data-alert="${a.id}" tabindex="0" role="button"><div class="alert-content"><h4><span class="severity">${severityLabels[a.severity]}</span>${escapeHTML(a.title)}</h4><div class="alert-location"><span title="${escapeHTML(a.camera)}">${escapeHTML(a.camera)}</span><time>${escapeHTML(a.time)}</time></div><p class="alert-description">${escapeHTML(a.description)}</p><div class="alert-bottom"><button class="export-small" data-alert="${a.id}">查看详情</button>${a.confirmed?`<span class="confirmed-tag">✓ 已确认 · ${escapeHTML(a.by)}</span>`:`<button class="confirm-alert" data-confirm="${a.id}">确认告警</button>`}</div></div></article>`).join(''):`<div class="empty-alerts"><i>${icon('shield')}</i><span>暂无${filter==='all'?'匹配的':severityLabels[filter]+'级'}告警</span><small>当前筛选下没有告警记录</small></div>`;
}
function renderWorkerAlerts(){
  const query=$('#alert-search').value.trim().toLowerCase();
  const list=analysisData.events.filter(e=>(filter==='all'||e.severity===filter)&&`${e.title} ${e.description}`.toLowerCase().includes(query));
  $('#alert-summary').textContent=`质量事件 ${analysisData.events.length} · 均待复核`;$('#metric-alerts').textContent=analysisData.events.length;$('#alert-count').textContent=`${list.length} 条记录`;
  $('#alert-list').innerHTML=list.length?list.map(e=>`<article class="alert-card"><div class="alert-content"><h4><span class="severity">${severityLabels[e.severity]||'信息'}</span>${escapeHTML(e.title)}</h4><div class="alert-location"><span>${escapeHTML(analysisData.source.title||'视频分析')}</span><time>${e.start.toFixed(1)}–${e.end.toFixed(1)}s</time></div><p class="alert-description">${escapeHTML(e.description)}</p><div class="alert-bottom"><button class="export-small" data-worker-event="${e.id}">定位视频并查看依据</button><span>非违规结论</span></div></div></article>`).join(''):`<div class="empty-alerts"><i>${icon('shield')}</i><span>暂无匹配的质量事件</span><small>调整级别或搜索条件</small></div>`;
}
$$('[data-filter]').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.filter;$$('[data-filter]').forEach(el=>el.classList.toggle('active',el===b));renderAlerts();}));
$('#alert-search').addEventListener('input',renderAlerts);
function confirmAlert(id){const a=alerts.find(a=>a.id===id);if(!a)return;a.confirmed=true;a.by='当前值班员';writeStorage('aisop-alerts',alerts);renderAlerts();toast('告警已确认，处置记录已保存');}
function showAlert(id){const a=alerts.find(a=>a.id===id);if(!a)return;showModal('告警事件详情',`<p class="modal-intro">参考视频中的告警演示记录 · 事件 #${a.id.toString().padStart(4,'0')}</p><img class="preview-image" src="assets/factory.jpg" alt="工位现场参考画面"><p class="hint">画面为参考视频工位示意，非该历史事件发生瞬间的快照。</p><dl class="alert-details"><dt>告警类型</dt><dd><span class="severity">${severityLabels[a.severity]}</span> ${escapeHTML(a.title)}</dd><dt>发生时间</dt><dd>${escapeHTML(a.time)}</dd><dt>所属工位</dt><dd>${escapeHTML(a.area)}</dd><dt>事件描述</dt><dd>${escapeHTML(a.description)}</dd><dt>处置状态</dt><dd>${a.confirmed?'已确认 · '+escapeHTML(a.by):'待处置'}</dd></dl><div class="modal-actions"><button class="btn btn-secondary" data-replay="${a.id}">查看工位回放</button><button class="btn btn-primary" data-resolve="${a.id}">${a.confirmed?'重新标记为待处置':'确认此告警'}</button></div>`);}
renderAlerts();

function exportCSV(kind){
  if(analysisData&&kind!=='alerts'){const a=document.createElement('a');a.href=analysisBase+'segments.csv';a.download='工厂工人动作时间轴.csv';a.click();toast('已导出实际分析动作时间轴');return;}
  if(analysisData&&kind==='alerts'){
    const rows=[['事件编号','级别','事件类型','开始秒','结束秒','说明','结论'],...analysisData.events.map(e=>[e.id,severityLabels[e.severity]||'信息',e.title,e.start,e.end,e.description,'人工复核提示，非违规结论'])];downloadCSV(rows,'工厂工人质量事件.csv');toast(`已导出 ${analysisData.events.length} 条质量事件`);return;
  }
  const mode=demo?'参考视频演示':'本地视频（未识别）';
  let rows;
  if(kind==='alerts')rows=[['事件编号','告警级别','告警类型','所属区域','告警内容','发生时间','状态','确认人','数据来源'],...filteredAlerts().map(a=>[a.id,severityLabels[a.severity],a.title,a.area,a.description,a.time,a.confirmed?'已确认':'待处置',a.by, '参考视频演示'])];
  else {const s=currentState();rows=[['目标ID','动作','置信度','所属区域','停留秒数','流程','数据来源','导出时间'],...(demo?[[s.id,s.action,`${s.confidence}%`,workflow.area,s.dwell,workflow.name,mode,new Date().toLocaleString('zh-CN')]]:[])];}
  const safe=value=>{let text=String(value??'');if(/^[=+@\-\t\r]/.test(text))text="'"+text;return '"'+text.replace(/"/g,'""')+'"';};
  const blob=new Blob(['\ufeff'+rows.map(row=>row.map(safe).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8;'}), url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download=`AI-SOP_${kind==='alerts'?'告警记录':'检测结果'}_${new Date().toISOString().slice(0,10)}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);toast(`已导出 ${rows.length-1} 条${kind==='alerts'?'告警':'检测'}记录`);
}
function downloadCSV(rows,name){const safe=value=>{let text=String(value??'');if(/^[=+@\-\t\r]/.test(text))text="'"+text;return '"'+text.replace(/"/g,'""')+'"';};const blob=new Blob(['\ufeff'+rows.map(row=>row.map(safe).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);}
function showWorkflow(){workflowDraft=structuredClone(workflow);showModal('作业流程管理',`<p class="modal-intro">定义工位的标准作业步骤与演示阶段时长。保存后立即应用到当前工位，并保存在本机浏览器。</p><form id="workflow-form"><label class="form-label" for="flow-name">流程名称</label><input class="field" id="flow-name" maxlength="40" required value="${escapeHTML(workflow.name)}"><label class="form-label" for="flow-area">绑定区域</label><input class="field" id="flow-area" maxlength="35" required value="${escapeHTML(workflow.area)}"><label class="form-label">作业步骤</label><div class="editor-head"><span>按实际作业顺序配置</span><span>时长（秒）</span></div><div id="step-editors"></div><button class="add-step" type="button" data-action="add-step">＋ 添加步骤</button><p class="hint">支持 2–8 个步骤，单步骤时长 1–300 秒。演示按配置时长切换；真实判定需要识别服务。</p><div class="modal-actions"><button class="btn btn-secondary" type="button" data-action="reset-workflow">恢复默认</button><button class="btn btn-primary" type="submit">保存并应用</button></div></form>`);renderStepEditors();$('#workflow-form').addEventListener('submit',e=>{e.preventDefault();syncDraft();const name=$('#flow-name').value.trim(),area=$('#flow-area').value.trim();if(!name||!area||workflowDraft.steps.some(s=>!s.name.trim()||s.duration<1||s.duration>300||!Number.isFinite(s.duration)))return toast('请填写完整名称，并将时长设为 1–300 秒');workflow={name,area,steps:workflowDraft.steps.map(s=>({name:s.name.trim(),duration:s.duration}))};writeStorage('aisop-workflow',workflow);updateDashboard();closeModal();toast('作业流程已保存并应用');});}
function syncDraft(){workflowDraft.steps=$$('.step-editor').map(row=>({name:$('[data-step-name]',row).value,duration:Number($('[data-step-duration]',row).value)}));}
function renderStepEditors(){$('#step-editors').innerHTML=workflowDraft.steps.map((s,i)=>`<div class="step-editor"><span class="editor-index">${i+1}</span><input class="field" data-step-name value="${escapeHTML(s.name)}" required maxlength="35" aria-label="步骤 ${i+1} 名称"><input class="field" data-step-duration type="number" min="1" max="300" required value="${s.duration}" aria-label="步骤 ${i+1} 时长"><button class="icon-button" type="button" data-remove-step="${i}" aria-label="删除步骤 ${i+1}" ${workflowDraft.steps.length<=2?'disabled':''}>${icon('close')}</button></div>`).join('');$('[data-action="add-step"]').disabled=workflowDraft.steps.length>=8;}
function showSystem(){const s=currentState(),real=analysisData?.summary;showModal('系统运行详情',`<p class="modal-intro">当前工作区运行状态与视频源信息。</p><div class="system-grid"><div class="system-item"><span>运行模式</span><strong>${real?'实际离线分析':demo?'参考视频演示':'本地视频回放'}</strong></div><div class="system-item"><span>识别模型</span><strong>${real?escapeHTML(analysisData.model.name):'未连接'}</strong></div><div class="system-item"><span>推理设备</span><strong>${real?escapeHTML(analysisData.model.device):'—'}</strong></div><div class="system-item"><span>分析采样</span><strong>${real?analysisData.model.sampleFps+' fps':'—'}</strong></div><div class="system-item"><span>平均模型推理</span><strong>${real?real.meanInferenceMs.toFixed(1)+' ms':demo?s.latency+' ms':'—'}</strong></div><div class="system-item"><span>人体检出率</span><strong>${real?(real.presenceRate*100).toFixed(1)+'%':'—'}</strong></div><div class="system-item"><span>视频源分辨率</span><strong>${video.videoWidth} × ${video.videoHeight}</strong></div><div class="system-item"><span>分析帧数</span><strong>${real?real.analyzedFrames:'—'}</strong></div></div><div class="system-note">${real?'当前检测结果来自实际模型离线推理。动作由关键点几何规则生成；未用人工真值验证，也未接入工具、工件和设备状态，因此不输出工序合格率。':'演示数据不是本机真实推理结果。上传视频可回放，自动判定需接入模型服务。'}</div><div class="modal-actions">${real?'<button class="btn btn-secondary" data-action="report">打开详细报告</button>':'<button class="btn btn-secondary" data-action="checks">运行环境自检</button>'}<button class="btn btn-primary" data-action="close-modal">完成</button></div>`);}
let checksRun=false;
function showChecks(){
  const supported=[['视频播放能力',!!document.createElement('video').canPlayType('video/mp4')],['Canvas 画面渲染',!!ctx],['本地文件读取',typeof FileReader!=='undefined'],['浏览器本地存储',(()=>{try{return !!localStorage;}catch{return false;}})()],['CSV 文件导出',typeof Blob!=='undefined'],['全屏显示',!!document.fullscreenEnabled],['视频源就绪',video.readyState>=2],['人体检测模型服务',false],['姿态估计模型服务',false],['SOP 判定服务',false]];
  showModal('运行环境自检',`<p class="modal-intro">检查浏览器能力与模型连接状态。当前 ${checksRun?supported.filter(x=>x[1]).length:0} 项通过，${checksRun?supported.filter(x=>!x[1]).length:10} 项${checksRun?'待接入':'待检查'}。</p><div>${supported.map(([label,ok],i)=>`<div class="check-row"><span class="check-mark">${checksRun&&ok?'✓':'○'}</span><span>${String(i+1).padStart(2,'0')} · ${label}</span><span class="check-state ${checksRun&&ok?'':'pending'}">${checksRun?(ok?'已通过':i>6?'待接入':'不可用'):'待检查'}</span></div>`).join('')}</div><div class="modal-actions"><button class="btn btn-secondary" data-action="close-modal">关闭</button><button class="btn btn-primary" data-action="run-checks">${checksRun?'重新检查':'开始检查'}</button></div>`);
  if(checksRun)$('.check-chip').innerHTML=`<span class="dot yellow"></span>自检 ${supported.filter(x=>!x[1]).length} 项待处理`;
}
function showUpload(){showModal('上传视频检测',`<p class="modal-intro">选择本地工位视频。文件仅在当前浏览器中读取，不会上传到外部服务。</p><div class="upload-zone" id="upload-zone" tabindex="0" role="button" aria-label="选择或拖放视频"><i>${icon('upload')}</i><h3>将视频拖放到这里</h3><p>支持浏览器可解码的 MP4、WebM、MOV · 最大 500 MB</p><button class="btn btn-primary" data-action="choose-video">选择本地视频</button></div><div class="system-note">当前为界面复刻与视频回放版本。上传后可播放、暂停和拖动进度；自动检测需要接入 AI 推理引擎，系统不会生成虚构识别结果。</div><div class="modal-actions"><button class="btn btn-secondary" data-action="restore-demo">恢复参考视频演示</button><button class="btn btn-secondary" data-action="close-modal">取消</button></div>`);
  const zone=$('#upload-zone');zone.insertAdjacentHTML('afterend','<button class="add-step" style="margin-top:12px" data-action="worker-video">播放工厂工人实际分析视频</button><button class="add-step" data-action="factory-video">播放瓶装生产线测试视频</button><button class="add-step" data-action="test-video">播放行人测试视频</button>');zone.addEventListener('click',e=>{if(!e.target.closest('button'))$('#video-upload').click();});zone.addEventListener('keydown',e=>{if(e.target===zone&&(e.key==='Enter'||e.key===' ')){e.preventDefault();$('#video-upload').click();}});zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('drag-over');});zone.addEventListener('dragleave',()=>zone.classList.remove('drag-over'));zone.addEventListener('drop',e=>{e.preventDefault();zone.classList.remove('drag-over');if(e.dataTransfer.files[0])loadUpload(e.dataTransfer.files[0]);});
}
function setVideoRoute(sample){const url=new URL(location.href);if(sample)url.searchParams.set('video',sample);else url.searchParams.delete('video');history.replaceState(null,'',url);}
function loadTestVideo(sample='people'){
  sceneRevision++;window.ServerApp?.leaveRun();
  const factory=sample==='factory';
  video.pause();if(uploadedURL){URL.revokeObjectURL(uploadedURL);uploadedURL=null;}
  demo=false;analysisData=null;video.loop=true;video.src=factory?'assets/factory-test.mp4':'assets/people-test.mp4';video.load();
  $('.camera-name').textContent=factory?'工厂瓶装生产线 · 公开测试视频':'行人通行 · 公开测试视频';$('.stream-info').textContent='原视频自带标注 · 未运行识别';$('#mode-text').textContent=`${factory?'工厂生产线':'行人'}示例 · 检测框与计数为视频原有标注`;
  setVideoRoute(factory?'factory':'people');updateDashboard();renderAlerts();closeModal();
}
async function loadWorkerVideo(){
  const revision=++sceneRevision;window.ServerApp?.leaveRun();
  try{
    const response=await fetch('assets/worker-analysis/analysis.json',{cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();if(revision!==sceneRevision)return;analysisData=data;analysisBase='assets/worker-analysis/';
    video.pause();if(uploadedURL){URL.revokeObjectURL(uploadedURL);uploadedURL=null;}demo=false;video.loop=true;video.src='assets/factory-workers.mp4';video.load();setVideoRoute('workers');
    $('.camera-name').textContent='电子装配/检验车间 · 工人实际分析';$('.stream-info').innerHTML=`1280×720 · ${analysisData.model.sampleFps.toFixed(1)}fps 分析 · YOLO11s-pose`;$('#mode-text').textContent='实际模型结果 · 青色框和骨架为本次分析输出';
    updateWorkerDashboard();renderAlerts();closeModal();toast('已载入工厂工人实际分析：检测明细、动作时序和质量事件已同步');
  }catch(error){console.error(error);toast('工人分析数据加载失败，请检查本地服务');}
}
function loadUpload(file){
  if(window.ServerApp?.available){window.ServerApp.upload(file);return;}
  sceneRevision++;
  if(!file.type.startsWith('video/')&&!/\.(mp4|webm|mov|m4v|ogv)$/i.test(file.name))return toast('请选择视频文件');
  if(file.size>500*1024*1024)return toast('视频超过 500 MB，请选择较小的文件');
  if(file.size===0)return toast('视频文件为空，请重新选择');
  video.pause();if(uploadedURL)URL.revokeObjectURL(uploadedURL);uploadedURL=URL.createObjectURL(file);demo=false;analysisData=null;video.loop=false;video.src=uploadedURL;video.load();
  setVideoRoute(null);$('.camera-name').textContent=file.name;$('.stream-info').textContent='本地视频 · 待识别';$('#mode-text').textContent='本地视频 · 待接入识别引擎';updateDashboard();renderAlerts();closeModal();toast('视频已加载，当前为本地回放模式');
}
$('#video-upload').addEventListener('change',e=>{if(e.target.files[0])loadUpload(e.target.files[0]);e.target.value='';});
function restoreDemo(){sceneRevision++;window.ServerApp?.leaveRun();video.pause();if(uploadedURL){URL.revokeObjectURL(uploadedURL);uploadedURL=null;}demo=true;analysisData=null;video.loop=false;video.src='assets/reference.mp4';video.load();setVideoRoute(null);$('.camera-name').textContent='CAM010 车门玻璃装配工位·检测视频源';$('.stream-info').innerHTML='1024×576 · <span id="title-fps">5.8</span>fps 采样 · 1人';$('#mode-text').textContent='参考视频 · 演示检测';updateDashboard();renderAlerts();closeModal();toast('已恢复参考视频演示');}
function showReport(){if(!analysisData&&window.ServerApp?.available){toast('请先完成分析并打开结果');return;}window.open(analysisBase+'report.html','_blank','noopener');}
async function toggleFullscreen(element){try{if(document.fullscreenElement)await document.exitFullscreen();else await element.requestFullscreen();}catch{toast('当前浏览器不支持全屏显示');}}
const actions={upload:showUpload,workflow:showWorkflow,export:()=>exportCSV('detection'),report:showReport,system:showSystem,checks:showChecks,'export-alerts':()=>exportCSV('alerts'),'close-modal':closeModal,'choose-video':()=>$('#video-upload').click(),'restore-demo':restoreDemo,'run-checks':()=>{checksRun=true;showChecks();},'confirm-all':()=>{if(analysisData)return toast('质量事件来自模型证据，请在详细报告中人工复核');const pending=alerts.filter(a=>!a.confirmed);if(!pending.length)return toast('当前所有告警均已确认');alerts.forEach(a=>{if(!a.confirmed){a.confirmed=true;a.by='当前值班员';}});writeStorage('aisop-alerts',alerts);renderAlerts();toast(`已确认 ${pending.length} 条告警`);},'add-step':()=>{syncDraft();if(workflowDraft.steps.length<8){workflowDraft.steps.push({name:'',duration:5});renderStepEditors();$$('[data-step-name]').at(-1).focus();}},'reset-workflow':()=>{workflowDraft=structuredClone(defaultWorkflow);$('#flow-name').value=workflowDraft.name;$('#flow-area').value=workflowDraft.area;renderStepEditors();},fullscreen:e=>toggleFullscreen(e.closest('.video-hover')?$('#video-stage'):document.documentElement)};
document.addEventListener('click',e=>{
  const action=e.target.closest('[data-action]');if(action){actions[action.dataset.action]?.(action);return;}
  const alert=e.target.closest('[data-alert]');if(alert){showAlert(Number(alert.dataset.alert));return;}
  const confirm=e.target.closest('[data-confirm]');if(confirm){confirmAlert(Number(confirm.dataset.confirm));return;}
  const resolve=e.target.closest('[data-resolve]');if(resolve){const a=alerts.find(a=>a.id===Number(resolve.dataset.resolve));if(a.confirmed){a.confirmed=false;a.by='';writeStorage('aisop-alerts',alerts);renderAlerts();toast('告警已标记为待处置');}else confirmAlert(a.id);closeModal();return;}
  const replay=e.target.closest('[data-replay]');if(replay){const a=alerts.find(a=>a.id===Number(replay.dataset.replay));if(!demo)restoreDemo();const seek=()=>{video.currentTime=a.videoAt;video.play().catch(()=>{});};if(video.readyState>=1)seek();else video.addEventListener('loadedmetadata',seek,{once:true});closeModal();toast('已定位到参考工位画面');return;}
  const workerEvent=e.target.closest('[data-worker-event]');if(workerEvent){const item=analysisData?.events.find(x=>x.id===Number(workerEvent.dataset.workerEvent));if(item){video.currentTime=item.start;video.play().catch(()=>{});toast(`${item.title}：${item.description}`);}return;}
  const remove=e.target.closest('[data-remove-step]');if(remove&&!remove.disabled){syncDraft();workflowDraft.steps.splice(Number(remove.dataset.removeStep),1);renderStepEditors();}
});
document.addEventListener('keydown',e=>{
  if(e.ctrlKey&&e.shiftKey&&e.key.toLowerCase()==='g'){e.preventDefault();showSystem();}
  const thumb=e.target.closest('[data-alert]');if(thumb&&e.key==='Enter'){e.preventDefault();showAlert(Number(thumb.dataset.alert));}
});
actions['test-video']=()=>loadTestVideo('people');
actions['factory-video']=()=>loadTestVideo('factory');
actions['worker-video']=loadWorkerVideo;
const requestedSample=new URLSearchParams(location.search).get('video');
if(requestedSample==='workers')loadWorkerVideo();else if(requestedSample==='people'||requestedSample==='factory')loadTestVideo(requestedSample);
window.addEventListener('beforeunload',()=>{cancelAnimationFrame(frameId);if(uploadedURL)URL.revokeObjectURL(uploadedURL);});
// Share the picture size across replay and dynamically created camera views.
(()=>{
  const panel=document.querySelector('.video-panel');
  const slider=document.querySelector('#video-size');
  const output=document.querySelector('#video-size-value');
  const handle=document.querySelector('#video-resize-handle');
  const key='aisop-video-height';
  const defaultHeight=()=>innerWidth<=760?220:300;
  let height=defaultHeight(),custom=false,drag=null;
  function apply(value,persist=false){
    height=Math.max(180,Math.min(720,Math.round(value/10)*10));
    panel.style.setProperty('--video-height',height+'px');
    slider.value=height;output.value=height+'px';
    if(persist){custom=true;try{localStorage.setItem(key,String(height));}catch{}}
  }
  try{const saved=Number(localStorage.getItem(key));if(saved>=180&&saved<=720){height=saved;custom=true;}}catch{}
  apply(height);
  slider.addEventListener('input',()=>apply(Number(slider.value),true));
  document.querySelector('#video-size-reset').addEventListener('click',()=>{
    custom=false;apply(defaultHeight());try{localStorage.removeItem(key);}catch{}
  });
  handle.addEventListener('pointerdown',event=>{
    if(event.button!==0)return;
    drag={id:event.pointerId,y:event.clientY,height};
    handle.setPointerCapture(event.pointerId);event.preventDefault();
  });
  handle.addEventListener('pointermove',event=>{
    if(drag&&event.pointerId===drag.id)apply(drag.height+event.clientY-drag.y,true);
  });
  const end=()=>{drag=null;};
  handle.addEventListener('pointerup',end);
  handle.addEventListener('pointercancel',end);
  handle.addEventListener('lostpointercapture',end);
  handle.addEventListener('keydown',event=>{
    const delta={ArrowUp:-10,ArrowDown:10,ArrowLeft:-10,ArrowRight:10}[event.key];
    if(delta){event.preventDefault();apply(height+delta,true);}
  });
  window.addEventListener('resize',()=>{if(!custom)apply(defaultHeight());});
})();

