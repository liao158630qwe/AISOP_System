/* Screw inspection in the original monitoring dashboard. */
window.ScrewApp=(()=>{
  'use strict';
  const state={task:'screw',get session(){return Workspace.session;},model:null,result:null,frames:[],frame:null,mode:'replay',running:false,
    final:null,run:null,runStatus:null,cancelling:false,generation:0,busy:false,finishing:false,stream:null,streamId:null,timer:null,pending:null,
    camera:null,capture:document.createElement('canvas'),started:0,processing:0,drawnBoxes:[],finalShown:false,aggregateTargets:[],liveCompliance:null,
    timelyDecision:null,timelyShown:false,timelyModalKey:null,liveController:null,liveFailures:0,liveIssue:null,
    evidenceGaps:[],interruptedAt:null,frameSequence:0,displayLatencyMs:null,latencySamples:[],frameTimeoutMs:12000,
    preparing:false,preparationMs:null,preparationStarted:null,preparationTimeoutMs:60000};
  const FRAME_FRESH_MS=750, EVIDENCE_GAP_MS=3000;
  const labels={PRESENT:'螺丝已装',EMPTY:'可见空孔',UNKNOWN:'尚未确认'};
  const colors={PRESENT:'#39d9a0',EMPTY:'#ff746e',WRONG:'#ff746e',UNKNOWN:'#f0c45b',CONTACT:'#49c5ff',CLEAR:'#8296a9',ACTION_DONE:'#39d9a0',NOT_OBSERVED:'#ff746e'};
  const sequenceLabels={COMPLETED:'已确认',PENDING:'待确认',EMPTY:'明确空孔',PREASSEMBLED_OK:'预装件已确认',PREASSEMBLED_MISSING:'预装件缺失',PREASSEMBLED_UNKNOWN:'预装件待确认',UNKNOWN:'证据不足'};
  const sequenceColors={COMPLETED:'#39d9a0',PENDING:'#8296a9',EMPTY:'#ff746e',PREASSEMBLED_OK:'#39d9a0',PREASSEMBLED_MISSING:'#ff746e',PREASSEMBLED_UNKNOWN:'#f0c45b',UNKNOWN:'#f0c45b'};
  const isAction=()=>state.task==='sticker'&&(state.result?(state.result.inspectionMode||'appearance'):state.model?.mode)==='action';
  const completed=t=>state.task==='screw'?t.sequenceState==='COMPLETED':['PRESENT','ACTION_DONE'].includes(t.state);
  const sequenceLabel=t=>{
    if(state.task==='screw'&&!state.final&&t?.sequenceState==='EMPTY')return '当前为空';
    if(state.task==='screw'&&!state.final&&t?.sequenceState==='PREASSEMBLED_MISSING')return '预装件当前为空';
    return sequenceLabels[t?.sequenceState]||sequenceLabels.UNKNOWN;
  };
  const sequenceColor=t=>{
    if(state.task==='screw'&&!state.final&&['EMPTY','PREASSEMBLED_MISSING'].includes(t?.sequenceState))return colors.UNKNOWN;
    return sequenceColors[t?.sequenceState]||sequenceColors.UNKNOWN;
  };
  const isPose=()=>state.task==='pose';
  const isPilot=()=>/^station[1-6]$/.test(state.task);
  const isScene=()=>state.task.startsWith('scene:');
  const sceneId=()=>state.task.slice(6);
  const taskType=()=>isScene()?'scene':state.task;
  const validTask=task=>(['screw','sticker','pose'].includes(task)||/^station[1-6]$/.test(task))||/^scene:[0-9a-f-]{36}$/i.test(task);
  const taskName=()=>isPilot()?'工位'+state.task.slice(7)+'人工流程':isScene()?(state.model?.name||'场景任务'):isPose()?'人体检测与骨骼':state.task==='sticker'?'贴条工序检测':'螺丝安装检测';
  const objectName=()=>isScene()?'目标':isPose()?'人体':state.task==='sticker'?'贴条':'螺丝';
  const taskScope=()=>isPose()?'仅检测人体并输出骨骼与姿态规则':state.task==='sticker'?(isAction()?'以贴放/按压动作计完成，检查先三角后方形；不核验贴片实际存在、外观或粘附':'历史贴条外观与操作顺序检查'):'螺丝安装逐位置累计视觉确认，不包含扭矩验证';
  const displayModelName=value=>String(value||'').replace(/螺丝增强/g,'螺丝安装检测').replace(/专用增强/g,'专用');
  const get=selector=>document.querySelector(selector);
  function tell(text){get('#server-status').textContent=text;}
  const request=(path,options={})=>Workspace.request(path,options);
  function interruptLive(reason){
    if(state.mode!=='realtime'||!state.running)return;
    state.liveIssue=reason;state.interruptedAt??=performance.now();state.actualFps=null;
    state.lastSampleAt=null;state.liveController?.abort();render();renderAlerts();
  }
  function recordGap(at=performance.now()){
    if(state.preparing)return;
    if(state.interruptedAt!=null&&at-state.interruptedAt>=EVIDENCE_GAP_MS){
      if(state.evidenceGaps.length<100)state.evidenceGaps.push({start:Math.max(0,(state.interruptedAt-state.started)/1000),end:Math.max(0,(at-state.started)/1000),reason:state.liveIssue||'采样连接中断'});
      state.interruptedAt=at;
    }
  }
  function mergeEvidenceGaps(gaps){
    const key=gap=>[gap.start,gap.end,gap.reason].join('|'),seen=new Set(state.evidenceGaps.map(key));
    for(const gap of gaps||[]){
      if(state.evidenceGaps.length>=100)break;
      if(!Number.isFinite(gap.start)||!Number.isFinite(gap.end)||gap.start<0||gap.end<gap.start||seen.has(key(gap)))continue;
      state.evidenceGaps.push({start:gap.start,end:gap.end,reason:String(gap.reason||'服务端记录到采样缺口').slice(0,160),source:gap.source||'server'});seen.add(key(gap));
    }
  }
  const confirmedScrewCoverage=result=>state.task==='screw'&&result?.compliance?.status==='PASS'&&result.sequence?.verificationMode==='visual-cumulative'&&result.sequence.status==='PASS';
  function protectVerdict(result){
    if(!state.evidenceGaps.length||result.compliance?.status!=='PASS'||confirmedScrewCoverage(result))return result;
    return {...result,originalCompliance:result.compliance,evidenceGaps:[...state.evidenceGaps],compliance:{status:'REVIEW',label:'证据缺口待复核',reason:'本轮存在超过3秒的采样中断，已保留原始判定与历史证据；请复核中断期间的操作或重新检测。'}};
  }
  function renderLiveHealth(){
    if(state.mode!=='realtime'||isPilot())return;
    const fresh=Boolean(currentFrame()),pending=state.running&&!state.finishing;
    const latency=get('#metric-latency');latency.previousElementSibling.textContent='采集至展示';
    latency.textContent=state.displayLatencyMs==null?'—':Math.round(state.displayLatencyMs);
    latency.title='从浏览器采集到本页完成结果更新，含编码、网络、服务端处理和页面更新；不含相机曝光与设备驱动延迟。';
    get('#finish-detection').textContent=state.preparing?'取消准备':isPose()?'结束检测':'结束并判定';
    if(pending&&state.preparing){
      const reason=state.liveIssue||'模型准备中，正式采样尚未开始';
      for(const id of ['metric-compliance','compliance-rate','video-compliance'])get('#'+id).textContent='准备中';
      for(const id of ['metric-fps','sample-fps','metric-latency','dwell'])get('#'+id).textContent='—';
      get('#metric-compliance').className='text-yellow';get('#realtime-alert').textContent='未开始检测';get('#run-state').textContent='模型准备中';get('.live').textContent='● 准备中';
      get('#workflow-status').textContent='模型准备中';get('#workflow-status').dataset.verdict='PENDING';
      get('#workflow-description').textContent=reason+'；请等待准备完成后再开始操作，本帧只用于加载模型。';
      get('#result-note').textContent='当前为预备画面，不记录工序证据；可随时取消或切换检测任务。';
      get('#screw-verdict').hidden=true;
      get('#footer-performance').textContent=`模型准备中 · 已等待 ${Math.max(0,(performance.now()-(state.preparationStarted||performance.now()))/1000).toFixed(1)} 秒 · 正式采样尚未开始`;
      return;
    }
    if(pending&&!fresh){
      const reason=state.liveIssue||(state.lastResponse?'采样已过期，等待新结果':'等待首个有效采样');
      for(const id of ['metric-compliance','compliance-rate','video-compliance'])get('#'+id).textContent='待新采样';
      get('#metric-compliance').className='text-yellow';get('#metric-fps').textContent='—';get('#sample-fps').textContent='—';
      get('#realtime-alert').textContent='采样中断';get('#run-state').textContent=reason;get('.live').textContent='● 等待采样';
      get('#workflow-status').textContent='待新采样';get('#workflow-status').dataset.verdict='REVIEW';
      get('#workflow-description').textContent=reason+'；已确认的本轮证据保留，当前画面不能确认通过。';
      get('#result-note').textContent=reason+'；当前标注已清除，本轮已确认记录仅作历史证据。';
      const banner=get('#screw-verdict');banner.hidden=isPose();banner.dataset.verdict='REVIEW';banner.textContent=reason+' · 等待当前有效采样';
      if(get('#modal').open&&get('#modal-title').textContent==='贴条工序即时判定')closeModal();
    }
    if(pending&&fresh&&state.evidenceGaps.length&&!isPose()&&state.liveCompliance?.status!=='FAIL'&&!confirmedScrewCoverage({compliance:state.liveCompliance,sequence:state.frame?.inspection?.sequence})){
      for(const id of ['metric-compliance','compliance-rate','video-compliance'])get('#'+id).textContent='缺口待复核';
      get('#metric-compliance').className='text-yellow';get('#workflow-status').textContent='本轮证据有缺口';get('#workflow-status').dataset.verdict='REVIEW';
      get('#realtime-alert').textContent='证据缺口';get('#workflow-description').textContent='采样已恢复，本轮中断期间的操作仍需复核；恢复后不会自动追认缺失证据。';
      const banner=get('#screw-verdict');banner.hidden=false;banner.dataset.verdict='REVIEW';banner.textContent='本轮存在采样缺口 · 请复核中断期间操作或重新开始检测';
    }
    if(state.running)get('#footer-performance').textContent=`采样 ${state.actualFps?.toFixed(1)||'—'} fps · 采集至展示 ${state.displayLatencyMs==null?'—':Math.round(state.displayLatencyMs)} ms · 服务端处理 ${Math.round(state.processing||0)} ms · 本轮 ${state.frames.length} 次${state.evidenceGaps.length?' · 有证据缺口待复核':''}`;
  }
  function applyTaskText(){
    if(isPilot())return;
    get('.toolbar [data-action="upload"] .task-action-label').textContent='上传视频检测';
    get('.check-chip').textContent=state.model?.ready?'自检 · 模型已就绪':'自检 · 模型待就绪';
    get('#live-rate').disabled=isScene();
    get('#live-rate').title=isScene()?'采样率由任务配置决定':'';
    if(isScene()){SceneDashboard.apply(state);actions.workflow=()=>SceneDashboard.rules(state);return;}
    const pose=isPose();
    get('#metric-alerts').previousElementSibling.textContent=pose?'姿态识别提示':'待处置告警';
    get('.alert-toolbar .filters').hidden=pose;get('[data-action="confirm-all"]').hidden=pose;
    get('#alert-search').placeholder=pose?'搜索人体轨迹或姿态提示':'搜索告警、工位或确认人';
    get('#finish-detection').textContent=pose?'结束检测':'结束并判定';
    get('.workflow-panel .section-title h2').textContent=pose?'人体识别过程':'检测判定过程（逐步骤）';
    get('.compliance-panel .subheading h3').innerHTML='<b class="number red-bg">4</b>'+(pose?'人体与姿态概况':'安装合规判定');
    get('.video-stats>div:first-child span').textContent=pose?'当前人体与主操作员':'运行中的工序';
    get('.video-stats>div:last-child span').textContent=pose?'骨骼可见度':'工序合规率';
    get('.alerts-panel .section-title h2').textContent=pose?'姿态识别提示':'实时告警中心';
    get('.timeline-panel .legend').innerHTML=pose?'<span><i style="background:#39bd95"></i>检测到人体</span><span><i style="background:#736e4c"></i>未检出人体</span>':'<span><i style="background:#39bd95"></i>可核对</span><span><i style="background:#736e4c"></i>待确认</span>';
    if(pose){
      get('.brand p').textContent='人体目标检测 · 17点骨骼 · 主操作员与姿态识别';
      get('.footer>span').textContent='AI-SOP · '+taskName();
      get('.video-panel .subheading h3').innerHTML='<b class="number blue">1</b>人体检测与骨骼 / 当前采样';
      get('.pose-legend').innerHTML='<span>● 青色：左侧肢体</span><span>● 橙色：右侧肢体</span><span>● 绿色：躯干／主操作员</span><small>低置信关键点隐藏 · 含头颈轮廓</small>';
      get('.timeline-panel .subheading h3').innerHTML='<b class="number yellow-bg">3</b>人体检测时间轴';
      get('.compliance-panel .positive span').textContent='当前人数';
      get('.compliance-grid>div:nth-child(2) span').textContent='姿态证据不足';
      get('.compliance-grid>div:nth-child(3) span').textContent='骨骼可见度';
      get('#metric-compliance').previousElementSibling.textContent='人体检测状态';
      get('#metric-compliance').nextElementSibling.textContent='';
      get('#metric-targets').previousElementSibling.textContent='当前人体';get('#metric-targets').nextElementSibling.textContent='人';
      get('#metric-fps').previousElementSibling.textContent='检测帧率';
      ['ID','姿态','人体置信','可见关键点','采样时间','角色'].forEach((value,index)=>get('.results-panel thead').querySelectorAll('th')[index].textContent=value);
      get('.alerts-foot').textContent='姿态规则用于观察人体动作';
      get('#video-empty p').textContent='上传视频或开启摄像头，查看人体框、骨骼和姿态';
      if(!state.result&&!state.camera){get('.camera-name').textContent='人体检测 · 等待视频或摄像头';get('.stream-info').textContent='人体框 + 17点骨骼';}
      get('#inspection-task').value=state.task;
      actions.workflow=()=>showModal('人体检测与骨骼规则','<p>仅检测人体。每个采样帧同时生成检测框和17个身体关键点，再通过跟踪、关键点平滑与姿态规则分析动作。</p><p>画面面积最大的人体框标为主操作员；轨迹编号用于关联连续采样。头部轮廓和颈部连接参考 bend_demo，并随人体朝向旋转。</p><p>低置信关键点隐藏，未检出人体或实时结果过期时清除框和骨骼。人体姿态不用于判定安装合规。</p>');
      return;
    }
    const sticker=state.task==='sticker';
    Object.assign(labels,{PRESENT:sticker?'已贴':'螺丝已装',EMPTY:sticker?'明确未贴':'可见空孔',WRONG:'形状不符',UNKNOWN:sticker?'当前帧未检出':'尚未确认',CONTACT:'贴放/按压候选',CLEAR:'当前无目标动作',ACTION_DONE:'动作已确认',NOT_OBSERVED:'未观察到动作'});
    get('.brand p').textContent=taskName()+' · 人体框与17点骨骼 · 本轮位置确认历史';
    get('.footer>span').textContent='AI-SOP · '+taskName();
    get('.video-panel .subheading h3').innerHTML='<b class="number blue">1</b>'+taskName()+' / 当前帧识别';
    get('.pose-legend').innerHTML=isAction()?'<span>● 蓝色：当前贴放/按压候选</span><span>完成计数见本轮动作记录</span><span>● 青橙：人体17点骨骼</span><span>无动作／未定位：不画动作框</span>':sticker?'<span>● 绿色：正确形状贴条</span><span>● 红色：明确未贴／形状不符</span><span>● 青橙：人体17点骨骼</span><span>遮挡／未定位：不画贴条框</span>':'<span>● 绿色：可见螺丝／主操作员</span><span>● 红色：可见空孔</span><span>● 青橙：人体17点骨骼</span>';
    get('.timeline-panel .subheading h3').innerHTML='<b class="number yellow-bg">3</b>'+objectName()+'检测时间轴';
    get('.compliance-panel .positive span').textContent=isAction()?'本轮动作已确认':sticker?'正确形状贴条位置':'已确认位置';
    get('.compliance-grid>div:nth-child(2) span').textContent=isAction()?'结束未观察到动作':sticker?'明确未贴／形状不符':'明确空孔／预装件缺失';
    get('.compliance-grid>div:nth-child(3) span').textContent=sticker?'工序合规率':'合规判断';
    get('#metric-compliance').previousElementSibling.textContent=sticker?'工序合规率':'合规判断';
    get('#metric-compliance').nextElementSibling.textContent='';
    get('#metric-targets').previousElementSibling.textContent=sticker?(isAction()?'已确认动作':'已贴位置'):'已确认位置';
    get('#metric-targets').nextElementSibling.textContent='处';
    get('#metric-fps').previousElementSibling.textContent=sticker?'主画面帧率':'检测帧率';
    const headings=get('.results-panel thead')?.querySelectorAll('th');
    if(headings?.length===6){
      const values=sticker?['ID','动作','置信','所属区域','停留','当前工序']:
        ['ID','位置状态','置信','位置名称','证据时间','位置／角色'];
      values.forEach((value,index)=>headings[index].textContent=value);
    }
    get('.alerts-foot').textContent=isAction()?'有贴放/按压动作即计完成 · 不核验黑色贴片外观':sticker?'历史外观检测 · 遮挡保留未知':'各位置独立累计确认 · 不要求安装顺序';
    get('#video-empty p').textContent='选择'+taskName()+'，查看当前证据和最终判定';
    if(!state.result&&!state.camera){get('.camera-name').textContent=taskName()+'工位 · 等待视频或摄像头';get('.stream-info').textContent=taskName()+' + 人体框 + 17点骨骼';}
    get('#inspection-task').value=state.task;
    actions.workflow=()=>showModal('检测位置与判定规则',sticker?'<p>先贴两处三角，再贴后续方形贴。每处需要至少两帧一致的真实视觉证据，并观察到未贴→已贴。</p><p>形状、位置和顺序齐全后立即弹出合规结果，检测继续，无需等待视频结束；明确未贴、形状不符或有可证实的错序为不合规；遮挡、视频从成品开始或工序证据不足为无法判定。</p><p>检测框只来自当前采样；下一件工件应开始新轮次，不共用上一件的过程证据。</p>':'<p>按当前标定视角的位置清单逐个累计确认，包含预装件。每个位置至少取得两帧一致证据后记为已确认；不要求安装先后顺序，也不要求从空孔开始。</p><p>全部操作位置和预装件确认后显示“检测合规”；实时摄像头检测自动结束并释放采样资源；明确空孔或预装件缺失立即判不合规；遮挡、定位失败或结束时仍有位置未确认则无法判定。</p><p>配置顺序只用于界面展示排序，未配置顺序的视角仍可正常判定。画面检测框只来自当前采样；下一件工件应开始新轮次。不验证扭矩。</p>');
    if(isAction())actions.workflow=()=>showModal('贴条动作判定规则','<p>先完成两处三角的贴放/按压动作，再完成六处方形动作。对应位置有动作即可计完成，不核验黑色贴片外观或实际粘附。</p><p>从无动作基线开始，连续观察到贴放/按压、运动及离开后确认一次。手部经过、扶住零件和静止成品不直接计完成。</p><p>八处动作和顺序齐全后立即弹出动作合规结果，检测继续，无需等待视频结束；结束仍有缺少动作或错序为动作不合规；没有动作或定位证据不足为动作证据不足。</p><p>蓝框仅显示当前采样动作候选。本轮动作记录保留已发生事件，下一件须开始新轮次。</p>');
  }
  async function loadTaskModel(){
    if(isPilot())return;
    const task=state.task;
    const model=await request(isScene()?'/scenes/'+sceneId():'/'+task+'-model');
    if(task!==state.task)return;
    state.model=isScene()?{...model,modelName:model.model.name+' v'+model.model.version,targets:[]}:model;
    applyTaskText();
  }
  async function setTask(task){
    if(window.AISOP_TERMINAL && task!=='station'+window.AISOP_TERMINAL.stationId.split('-')[1]){
      get('#inspection-task').value=state.task;
      tell('此终端仅允许操作绑定工位；实时AI请进入工位终端页面。');return;
    }
    if(!validTask(task)||task===state.task)return;
    if(state.busy||state.switching||isPilot()&&!PilotDashboard.canLeave()){get('#inspection-task').value=state.task;toast('当前操作尚未完成，请稍候再切换任务');return;}
    state.switching=true;get('#inspection-task').disabled=true;
    try {
    if(isPilot())PilotDashboard.deactivate();
    await stopCurrent();closeModal();state.generation++;state.task=task;
    delete get('#detection-results').dataset.poseRows;delete get('#steps').dataset.poseSteps;
    Object.assign(state,{model:null,result:null,frames:[],frame:null,run:null,runStatus:null,cancelling:false,final:null,finalShown:false,mode:'replay',finishing:false,
      camera:null,stream:null,streamId:null,samplePicture:null,frozen:null,lastResponse:null,lastSampleAt:null,actualFps:null,processing:0,busy:true,aggregateTargets:[],liveCompliance:null,timelyDecision:null,timelyShown:false,timelyModalKey:null});
    video.removeAttribute('src');video.load();get('#video-mode').value='replay';get('#play-button').disabled=true;get('#video-seek').disabled=true;
    try{localStorage.setItem('aisop-inspection-task',task);const url=new URL(location.href);url.searchParams.delete('scene');url.searchParams.set('task',task);window.history.replaceState(null,'',url);}catch{}
    if(isPilot()){
      get('#inspection-task').value=task;
      actions.workflow=()=>PilotDashboard.action('workflow');
      await PilotDashboard.activate(task);
      return;
    }
    applyTaskText();timeline();render();renderAlerts();tell('正在加载'+taskName()+'独立模型…');
    await loadTaskModel();tell(taskName()+'已选择 · 请选择视频或开始新一轮摄像头检测');
    }
    catch(e){tell(taskName()+'未就绪：'+e.message);}
    finally{state.busy=false;state.switching=false;get('#inspection-task').disabled=false;render();renderAlerts();}
  }
  function sequenceCounts(targets,sequence){
    const list=Array.isArray(targets)?targets:[];
    const expected=Number.isFinite(sequence?.operationsExpected)?sequence.operationsExpected:
      (sequence?.order?.length??list.filter(t=>!t.preassembled).length);
    const preExpected=Number.isFinite(sequence?.preassembledExpected)?sequence.preassembledExpected:
      (sequence?.preassembled?Object.keys(sequence.preassembled).length:list.filter(t=>t.preassembled).length);
    const preConfirmed=sequence?.preassembled?Object.values(sequence.preassembled).filter(v=>v==='PREASSEMBLED_OK').length:
      list.filter(t=>t.sequenceState==='PREASSEMBLED_OK').length;
    const completed=list.filter(t=>t.sequenceState==='COMPLETED').length;
    const confirmed=Number.isFinite(sequence?.positionsConfirmed)?sequence.positionsConfirmed:
      list.filter(t=>['COMPLETED','PREASSEMBLED_OK','EMPTY','PREASSEMBLED_MISSING'].includes(t.sequenceState)).length;
    return {completed,expected,preConfirmed,preExpected,confirmed,
      positionsExpected:Number.isFinite(sequence?.positionsExpected)?sequence.positionsExpected:list.length,
      current:null};
  }
  function orderedSummary(targets,sequence){
    const list=Array.isArray(targets)?targets:[];
    const order=Array.isArray(sequence?.order)?sequence.order:[];
    const rank=new Map(order.map((id,index)=>[id,index]));
    const confirmed=target=>['COMPLETED','PREASSEMBLED_OK'].includes(target?.sequenceState);
    const confirmedTime=target=>{
      const value=target?.confirmedAt;
      if(value==null||value==='')return null;
      const numeric=Number(value);
      return Number.isFinite(numeric)?numeric:null;
    };
    return list.map((target,index)=>({target,index})).sort((left,right)=>{
      const a=left.target,b=right.target;
      const aConfirmed=confirmed(a),bConfirmed=confirmed(b);
      if(aConfirmed!==bConfirmed)return aConfirmed?-1:1;
      if(aConfirmed){
        const at=confirmedTime(a),bt=confirmedTime(b);
        if(at!==null&&bt!==null&&at!==bt)return at-bt;
        if(at!==null&&bt===null)return -1;
        if(at===null&&bt!==null)return 1;
      }
      const ar=rank.has(a.id)?rank.get(a.id):Number.MAX_SAFE_INTEGER;
      const br=rank.has(b.id)?rank.get(b.id):Number.MAX_SAFE_INTEGER;
      if(ar!==br)return ar-br;
      if(Boolean(a.preassembled)!==Boolean(b.preassembled))return a.preassembled?1:-1;
      return left.index-right.index;
    }).map(item=>item.target);
  }
  function orderedStickerSummary(targets,sequence){
    const list=Array.isArray(targets)?targets:[];
    const events=new Map((Array.isArray(sequence?.events)?sequence.events:[]).map((event,index)=>[event.id,{event,index}]));
    const confirmed=target=>['PRESENT','EMPTY','WRONG','ACTION_DONE'].includes(target?.state);
    const confirmedTime=target=>{
      const event=events.get(target?.id)?.event;
      for(const value of [target?.confirmedAt,target?.lastSeen,event?.confirmedAt,event?.time]){
        if(value==null||value==='')continue;
        const numeric=Number(value);
        if(Number.isFinite(numeric))return numeric;
      }
      return null;
    };
    return list.map((target,index)=>({target,index})).sort((left,right)=>{
      const a=left.target,b=right.target;
      const aConfirmed=confirmed(a),bConfirmed=confirmed(b);
      if(aConfirmed!==bConfirmed)return aConfirmed?-1:1;
      if(aConfirmed){
        const at=confirmedTime(a),bt=confirmedTime(b);
        if(at!==null&&bt!==null&&at!==bt)return at-bt;
        if(at!==null&&bt===null)return -1;
        if(at===null&&bt!==null)return 1;
      }
      const ae=events.get(a.id),be=events.get(b.id);
      if(ae&&be&&ae.index!==be.index)return ae.index-be.index;
      if(ae&&!be)return -1;
      if(!ae&&be)return 1;
      return left.index-right.index;
    }).map(item=>item.target);
  }
  function sequenceProblemCount(targets,sequence){
    const flagged=(Array.isArray(targets)?targets:[]).filter(t=>['EMPTY','PREASSEMBLED_MISSING'].includes(t.sequenceState)).length;
    return sequence?.status==='FAIL'?Math.max(1,flagged):flagged;
  }
  function decision(targets,sequence,final=false){
    if(state.task==='sticker')return {status:'REVIEW',label:'无法判定',reason:'贴条工序顺序证据不可用'};
    if(!sequence)return {status:'REVIEW',label:'无法判定',scope:'螺丝安装逐位置累计视觉确认',
      reason:'未找到位置确认状态，不能判定合规；请重新分析该视频'};
    const status=sequence.status==='PASS'?'PASS':sequence.status==='FAIL'?'FAIL':'REVIEW';
    const counts=sequenceCounts(targets,sequence);
    return {status,label:{PASS:'合规',FAIL:'不合规',REVIEW:'无法判定'}[status],
      scope:`${counts.expected}处操作位置及${counts.preExpected}处预装件的累计视觉确认；不包含扭矩验证`,
      reason:sequence.reason||(sequence.configured?'位置证据不足，不能判定通过':'没有标定位置，无法判定'),
      empty:targets.filter(t=>t.state==='EMPTY').map(t=>t.id),
      early:[],preassembledMissing:Object.entries(sequence.preassembled||{}).filter(([,value])=>value==='PREASSEMBLED_MISSING').map(([id])=>id),
      operationsCompleted:counts.completed,operationsExpected:counts.expected,
      preassembledConfirmed:counts.preConfirmed,preassembledExpected:counts.preExpected,
      sequenceStatus:sequence.status,verificationMode:'visual-cumulative'};
  }
  function stickerTimelyVerdict(sequence,summary){
    if(!sequence||sequence.status!=='PASS')return null;
    const expected=state.result?.targets||state.model?.targets||initialTargets();
    const events=Array.isArray(sequence.events)?sequence.events:[];
    const confirmed=new Set(events.map(event=>event.id));
    if(!expected.length||expected.some(target=>!confirmed.has(target.id)))return null;
    if(isAction())return {status:'PASS',label:'动作合规',mode:'action',
      reason:sequence.reason||'两处三角动作先完成，后续方形动作已逐位置确认',
      confirmedActions:events.length,expectedActions:expected.length,events};
    if(state.liveCompliance)return state.liveCompliance.status==='PASS'?{...state.liveCompliance,confirmedPositions:events.length,expectedPositions:expected.length}:null;
    const visible=Array.isArray(summary)?summary:[];
    if(visible.some(target=>['EMPTY','WRONG'].includes(target.state)))return null;
    return {status:'PASS',label:'合规',mode:'appearance',
      reason:sequence.reason||'贴条位置与先三角后方形工序均已取得视觉证据',
      confirmedPositions:events.length,expectedPositions:expected.length,events};
  }
  function timelyRunKey(){return state.mode==='realtime'?(state.streamId||'live'):(state.run||state.result?.id||'replay');}
  function initialTargets(){return (state.result?.targets||(state.mode==="realtime"?state.frame?.inspection?.targets:null)||state.model?.targets||[]).map(t=>({...t,state:'UNKNOWN',score:null,bbox:null,sequenceState:'UNKNOWN',sequenceIndex:null,sequenceObserved:'UNKNOWN'}));}
  function prepareFrames(result){
    if(result.skeleton)adoptSkeletonTopology(result.skeleton);
    if(isPose())return;
    for(const frame of result.frames||[])frame.inspection.displayTargets=frame.inspection.targets.filter(t=>frame.inspection.registered&&['PRESENT','EMPTY','WRONG','CONTACT'].includes(t.state)&&(t.detectedBbox||t.bbox)).map(t=>({...t,bbox:t.detectedBbox||t.bbox,positionMode:'DETECTED'}));
    if(state.task==='sticker'&&result.frames?.length){
      const action=(result.inspectionMode||'appearance')==='action';
      const targets=result.targets||result.frames.find(frame=>frame.inspection?.targets?.length)?.inspection.targets||[];
      if(action){
        for(const frame of result.frames){
          const events=frame.inspection?.sequence?.events||[];
          const byId=new Map(events.map(event=>[event.id,event]));
          frame.aggregateTargets=targets.map(target=>{
            const event=byId.get(target.id);
            return {...target,state:event?'ACTION_DONE':'UNKNOWN',score:event?.score??null,
              lastSeen:event?.time??null,confirmedAt:event?.confirmedAt??null,bbox:null};
          });
        }
      }else{
        const sites=new Map();
        const ensure=id=>{if(!sites.has(id))sites.set(id,{state:null,score:null,lastSeen:null,confirmedAt:null,pending:null,count:0,pendingScore:null});return sites.get(id);};
        for(const frame of result.frames){
          for(const target of frame.inspection?.targets||[]){
            if(!['PRESENT','EMPTY','WRONG'].includes(target.state))continue;
            const site=ensure(target.id);
            if(target.state===site.state){site.pending=null;site.count=0;continue;}
            if(target.state===site.pending)site.count+=1;else{site.pending=target.state;site.count=1;site.pendingScore=target.score??null;}
            if(target.score!=null)site.pendingScore=site.pendingScore==null?target.score:Math.min(site.pendingScore,target.score);
            if(site.count>=2){site.state=target.state;site.score=site.pendingScore;site.lastSeen=site.confirmedAt=frame.time;site.pending=null;site.count=0;}
          }
          frame.aggregateTargets=targets.map(target=>{
            const site=sites.get(target.id);
            return {...target,state:site?.state||'UNKNOWN',score:site?.score??null,
              lastSeen:site?.lastSeen??null,confirmedAt:site?.confirmedAt??null,bbox:null};
          });
        }
      }
    }
    if(state.task!=='screw'||!result.frames?.length||result.sequence?.verificationMode==='visual-cumulative')return;
    // Legacy runs were produced by the retired strict sequence state machine.
    // Rebuild cumulative per-position confirmation from the saved samples.
    const transient=new Set(['state','score','lastSeen','confirmedAt','firstSeen','bbox','detectedBbox','reason','contextScore','searchRoi','positionMode','historyConfirmed','sequenceState','sequenceIndex','sequenceObserved','sequenceReason']);
    const modelSequences=state.model?.sequences||{};
    const topSequence=result.calibration?.config?.sequence||null;
    const profiles=new Map();let active=null;
    const configFor=profile=>profile==null?topSequence:(modelSequences[profile]??null);
    const orderFor=(profile,targets)=>{
      const operations=targets.filter(t=>!t.preassembled).map(t=>t.id);
      const configured=Array.isArray(configFor(profile))?configFor(profile):[];
      const order=[];
      for(const id of configured)if(operations.includes(id)&&!order.includes(id))order.push(id);
      for(const id of operations)if(!order.includes(id))order.push(id);
      return order;
    };
    const ensure=(profile,state)=>{
      const order=orderFor(profile,state.targets);
      const preassembled=Object.fromEntries(state.targets.filter(t=>t.preassembled).map(t=>[t.id,'PREASSEMBLED_UNKNOWN']));
      if(!state.sequence)state.sequence={configured:state.targets.length>0,verificationMode:'visual-cumulative',status:'PENDING',order,current:order[0]||null,completed:[],pending:[...order],early:[],empty:[],preassembled,reason:'等待各位置取得至少两帧一致的视觉证据',failure:null,events:[]};
      else Object.assign(state.sequence,{configured:state.targets.length>0,verificationMode:'visual-cumulative',order,preassembled});
      for(const target of state.targets)if(!state.sites.has(target.id))state.sites.set(target.id,{confirmed:null,score:null,lastSeen:null,confirmedAt:null,pending:null,count:0,pendingScore:null});
    };
    const snapshot=(profile,final=false)=>{
      const sequence=profile.sequence,order=sequence.order,sites=profile.sites;
      const completed=[],pending=[],empty=[],preassembled={},missing=[];
      for(const id of Object.keys(sequence.preassembled)){
        const confirmed=sites.get(id)?.confirmed;
        if(confirmed==='PRESENT')preassembled[id]='PREASSEMBLED_OK';
        else if(confirmed==='EMPTY'){preassembled[id]='PREASSEMBLED_MISSING';missing.push(id);empty.push(id);}
        else preassembled[id]='PREASSEMBLED_UNKNOWN';
      }
      for(const id of order){
        const confirmed=sites.get(id)?.confirmed;
        if(confirmed==='PRESENT')completed.push(id);else pending.push(id);
        if(confirmed==='EMPTY')empty.push(id);
      }
      const preOk=Object.values(preassembled).filter(v=>v==='PREASSEMBLED_OK').length;
      const total=order.length+Object.keys(preassembled).length;
      let status='PENDING',failure=null,reason='';
      if(!profile.targets.length){status='REVIEW';reason='没有标定位置，无法进行视觉确认';}
      else if(completed.length===order.length&&preOk===Object.keys(preassembled).length){status='PASS';reason=`全部 ${order.length} 处操作位置及 ${Object.keys(preassembled).length} 处预装件均已取得累计视觉确认`;}
      else if(final&&missing.length){status='FAIL';failure='检测结束时预装件仍明确缺失：'+missing.join(', ');reason=failure;}
      else if(final&&empty.length){status='FAIL';failure='检测结束时仍有明确空孔：'+empty.join(', ');reason=failure;}
      else{
        const pendingPositions=pending.concat(Object.entries(preassembled).filter(([,value])=>value==='PREASSEMBLED_UNKNOWN').map(([id])=>id));
        const emptyNote=empty.length?`；当前明确为空：${empty.join(', ')}`:'';
        reason=`已确认 ${completed.length+preOk}/${total} 处位置；待确认：${pendingPositions.join(', ')||'无'}${emptyNote}`;
        if(final){status='REVIEW';reason=pendingPositions.length?'检测结束时仍有位置证据不足：'+pendingPositions.join(', '):'检测结束时证据不足，不能判定通过';}
      }
      return {...sequence,status,reason,failure,completed,pending,current:pending[0]||null,empty,preassembled,operationsExpected:order.length,operationsCompleted:completed.length,positionsExpected:profile.targets.length,positionsConfirmed:[...sites.values()].filter(site=>['PRESENT','EMPTY'].includes(site.confirmed)).length,preassembledExpected:Object.keys(preassembled).length,preassembledConfirmed:preOk};
    };
    const aggregate=profile=>{
      if(!profile)return initialTargets();
      const sequence=snapshot(profile,false);
      return profile.targets.map(target=>{
        const site=profile.sites.get(target.id),confirmed=site?.confirmed;
        const index=sequence.order.indexOf(target.id);
        let sequenceState='UNKNOWN';
        if(sequence.configured){
          if(Object.prototype.hasOwnProperty.call(sequence.preassembled,target.id))sequenceState=sequence.preassembled[target.id];
          else if(confirmed==='PRESENT')sequenceState='COMPLETED';
          else if(confirmed==='EMPTY')sequenceState='EMPTY';
          else sequenceState='PENDING';
        }
        return {...target,state:confirmed||'UNKNOWN',score:confirmed?site.score:null,lastSeen:confirmed?site.lastSeen:null,confirmedAt:confirmed?site.confirmedAt:null,historyConfirmed:!!confirmed,bbox:null,sequenceState,sequenceIndex:index>=0?index:null,sequenceObserved:['PRESENT','EMPTY'].includes(confirmed)?confirmed:'UNKNOWN',sequenceReason:sequence.reason||null};
      });
    };
    for(const frame of result.frames){
      const inspection=frame.inspection;
      if(inspection?.registered&&inspection.targets?.length){
        const identity=inspection.calibrationId??null;let profile=profiles.get(identity);
        if(!profile){profile={targets:[],sites:new Map(),sequence:null};profiles.set(identity,profile);}
        profile.targets=inspection.targets.map(target=>Object.fromEntries(Object.entries(target).filter(([key])=>!transient.has(key))));
        active=identity;ensure(identity,profile);
        for(const target of inspection.targets){
          const value=target.state;
          if(!['PRESENT','EMPTY'].includes(value))continue;
          let site=profile.sites.get(target.id);
          if(!site){site={confirmed:null,score:null,lastSeen:null,confirmedAt:null,pending:null,count:0,pendingScore:null};profile.sites.set(target.id,site);}
          if(value===site.confirmed){site.pending=null;site.count=0;continue;}
          if(value===site.pending)site.count+=1;else{site.pending=value;site.count=1;site.pendingScore=target.score??null;}
          if(target.score!=null)site.pendingScore=site.pendingScore==null?target.score:Math.min(site.pendingScore,target.score);
          if(site.count>=2){site.confirmed=value;site.score=site.pendingScore;site.lastSeen=site.confirmedAt=frame.time;site.pending=null;site.count=0;}
        }
        inspection.sequence=snapshot(profile,false);
      }
      frame.aggregateTargets=aggregate(profiles.get(active));
      const currentProfile=profiles.get(active);
      if(currentProfile)inspection.sequence=snapshot(currentProfile,false);
    }
    const profile=profiles.get(active);
    if(profile){
      result.sequence=snapshot(profile,true);
      result.targets=aggregate(profile);
      result.compliance=decision(result.targets,result.sequence,true);
    }else{
      result.sequence={configured:false,verificationMode:'visual-cumulative',status:'REVIEW',order:[],completed:[],pending:[],preassembled:{},empty:[],early:[],reason:'没有可核对的位置采样，无法判定',failure:null,events:[],operationsExpected:0,operationsCompleted:0,positionsExpected:0,positionsConfirmed:0,preassembledExpected:0,preassembledConfirmed:0};
      result.targets=(result.targets||[]).map(target=>({...target,state:'UNKNOWN',score:null,lastSeen:null,confirmedAt:null,sequenceState:'UNKNOWN'}));
      result.compliance=decision(result.targets,result.sequence,true);
    }
    result.historyDerived=true;
  }
  function currentFrame(){if(state.mode==='realtime')return state.running&&!state.liveIssue&&!document.hidden&&state.lastResponse!=null&&state.lastSampleAt!=null&&performance.now()-state.lastSampleAt<=FRAME_FRESH_MS?state.frame:null;const frames=state.result?.frames;if(!frames?.length)return null;let lo=0,hi=frames.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(frames[mid].time<=video.currentTime)lo=mid;else hi=mid-1;}const frame=frames[lo];return video.currentTime<frame.time||Number.isFinite(frame.end)&&video.currentTime>=frame.end?null:frame;}
  function currentTargets(){if(isPose())return [];const frame=currentFrame();return frame?.inspection?.targets||initialTargets();}
  function panelFrame(){return isPose()||isScene()?currentFrame():state.mode==='realtime'?state.frame:currentFrame();}
  function cumulativeTargets(sample=panelFrame()){
    if(state.final?.targets)return state.final.targets;
    if(state.mode==='realtime')return state.aggregateTargets?.length?state.aggregateTargets:null;
    return sample?.aggregateTargets||null;
  }
  function setText(node,value){const text=String(value);if(node.textContent!==text)node.textContent=text;}
  function sequenceDetail(target,sample){
    const time=target.lastSeen!=null?`${target.lastSeen.toFixed(2)}s`:'';
    switch(target.sequenceState){
      case 'COMPLETED':return time?`已确认 ${time} · 点击定位`:'已取得累计视觉确认 · 点击定位';
      case 'PENDING':return '等待至少两帧一致的明确视觉证据';
      case 'EMPTY':return state.final?'明确观察到空孔，不能判定通过':'当前明确为空，等待后续安装证据';
      case 'PREASSEMBLED_OK':return '预装件已确认可见';
      case 'PREASSEMBLED_MISSING':return state.final?'预装件明确缺失':'预装件当前为空，等待后续证据';
      case 'PREASSEMBLED_UNKNOWN':return '预装件尚未取得明确证据';
      default:return target.sequenceReason||'位置确认状态不可用';
    }
  }
  function renderTargetPanels(targets,summary,sample){
    const rows=get('#detection-results'),steps=get('#steps');
    const sync=(root,items,step)=>{
      const stepRanks=new Map();
      if(step){
        let rank=0;
        for(const target of items)if(!target.preassembled)stepRanks.set(target.id,++rank);
      }
      const key=items.map(t=>t.id).join('|');
      // Cached IDs do not prove that another renderer left the actual nodes
      // intact. Reconcile against real elements and validate their structure.
      const existing=new Map([...root.children].map(node=>[node.dataset.screwTarget,node]));
      const nodes=items.map((target,index)=>{
        let node=existing.get(target.id);
        const valid=node&&(step?node.tagName==='BUTTON'&&['.step-index','.step-name','.step-status','.step-detail'].every(selector=>node.querySelector(selector)):
          node.tagName==='TR'&&node.children.length===6&&[...node.children].every(cell=>cell.tagName==='TD'));
        if(!valid){
          node=document.createElement(step?'button':'tr');node.dataset.screwTarget=target.id;
          if(step){node.type='button';node.className='step screw-step';node.innerHTML='<div class="step-top"><span class="step-index"></span><span class="step-name"></span><span class="step-status"></span></div><div class="step-detail"></div>';}
          else node.innerHTML='<td></td><td></td><td></td><td></td><td></td><td></td>';
        }
        if(root.children[index]!==node)root.insertBefore(node,root.children[index]||null);
        return node;
      });
      const kept=new Set(nodes);
      for(const node of [...root.childNodes])if(!kept.has(node))node.remove();
      root.dataset.targetKeys=key;
      items.forEach((target,index)=>{
        const node=nodes[index];
        const screw=state.task==='screw';
        const label=screw?sequenceLabel(target):
          target.state==='UNKNOWN'?(state.final?'证据不足':'尚未确认'):
          target.state==='NOT_OBSERVED'?labels.NOT_OBSERVED:labels[target.state]||labels.UNKNOWN;
        const statusColor=screw?sequenceColor(target):colors[target.state];
        if(step){
          node.classList.toggle('completed',completed(target));
          setText(node.querySelector('.step-index'),screw?(target.preassembled?'预':String(stepRanks.get(target.id)||'')):target.id.slice(1));
          setText(node.querySelector('.step-name'),target.name);
          const status=node.querySelector('.step-status');setText(status,label);status.style.color=statusColor;
          const evidence=screw?(target.lastSeen??(state.mode==='replay'?sample?.time:null)):target.lastSeen;
          if(evidence!=null)node.dataset.screwSeek=evidence;else delete node.dataset.screwSeek;
          setText(node.querySelector('.step-detail'),screw?sequenceDetail(target,sample):
            target.lastSeen!=null?`证据 ${target.lastSeen.toFixed(2)}s · 点击定位`:'尚未取得确认证据');
        }else{
          const evidence=target.lastSeen!=null?target.lastSeen.toFixed(2)+'s':'—';
          const values=[target.id,label,target.score==null?'—':(target.score*100).toFixed(0)+'%',target.name,
            evidence,state.task==='sticker'?(target.expected==='triangle'?'三角贴':'方形贴'):target.preassembled?'预装件':'操作位置'];
          values.forEach((value,i)=>setText(node.children[i],value));node.children[1].style.color=statusColor;
        }
      });
    };
    sync(rows,targets,false);sync(steps,summary,true);
  }
  function drawPeople(frame,x,y,w,h){
    adoptSkeletonTopology(frame?.skeleton||state.result?.skeleton||state.model?.skeleton);
    const unit=displayPixelRatio(ctx),fontSize=Math.round(15*unit),labelHeight=fontSize+8*unit;
    ctx.textBaseline='middle';ctx.font=`700 ${fontSize}px "Microsoft YaHei",sans-serif`;
    for(const person of frame?.people||[]){
      if(person.classId!=null&&person.classId!==0)continue;
      const box=person.bbox;if(!Array.isArray(box)||box.length!==4||!box.every(Number.isFinite))continue;
      const [a,b,c,d]=box,color=person.primary?'#39d9a0':'#49c5ff';
      if(c<=a||d<=b)continue;
      const bx=x+a*w,by=y+b*h,bw=(c-a)*w,bh=(d-b)*h;
      ctx.strokeStyle='#03101f';ctx.lineWidth=5*unit;ctx.strokeRect(bx,by,bw,bh);
      ctx.strokeStyle=color;ctx.lineWidth=3*unit;ctx.strokeRect(bx,by,bw,bh);
      drawPoseSkeleton(ctx,person.keypoints,x,y,w,h,person.skeletonOverlay);
      const label=`P${String(person.trackId).padStart(2,'0')}${person.primary?' 主操作员':''} · ${person.label}`;
      const labelWidth=Math.min(canvas.width,ctx.measureText(label).width+12*unit);
      const labelX=Math.max(0,Math.min(bx,canvas.width-labelWidth));
      const labelY=Math.max(0,Math.min(by-labelHeight,canvas.height-labelHeight));
      ctx.fillStyle='#07172bf0';ctx.fillRect(labelX,labelY,labelWidth,labelHeight);
      ctx.strokeStyle=color;ctx.lineWidth=1.2*unit;ctx.strokeRect(labelX,labelY,labelWidth,labelHeight);
      ctx.fillStyle=color;ctx.fillText(label,labelX+6*unit,labelY+labelHeight/2);
      state.drawnBoxes.push({id:person.trackId,bbox:box,className:'person',primary:person.primary});
      state.drawnSkeletons+=1;
    }
  }
  function draw(){
    if(isPilot())return;
    const source=state.mode==='realtime'?(state.running?(currentFrame()&&state.samplePicture||state.camera):state.frozen||state.camera):video;
    const frame=currentFrame(),sourceTime=source?.currentTime??0,stamp=performance.now();
    const last=state.lastDraw;
    if(last&&last.source===source&&last.sourceTime===sourceTime&&last.frame===frame&&last.width===canvas.width&&last.height===canvas.height&&stamp-last.at<100)return;
    state.lastDraw={source,sourceTime,frame,width:canvas.width,height:canvas.height,at:stamp};
    state.drawnSkeletons=0;
    ctx.fillStyle='#040c19';ctx.fillRect(0,0,canvas.width,canvas.height);
    if(!source||(!(source instanceof HTMLCanvasElement)&&source.readyState<2)){state.drawnBoxes=[];return;}
    const sw=source.videoWidth||source.width,sh=source.videoHeight||source.height;
    const ratio=Math.min(canvas.width/sw,canvas.height/sh),w=sw*ratio,h=sh*ratio,x=(canvas.width-w)/2,y=(canvas.height-h)/2;
    ctx.drawImage(source,x,y,w,h);ctx.save();
    const unit=displayPixelRatio(ctx);
    state.drawnBoxes=[];
    drawPeople(currentFrame(),x,y,w,h);
    const targetFont=Math.round(13*unit),targetHeight=targetFont+8*unit,pad=3*unit;
    ctx.textBaseline='middle';ctx.font=`700 ${targetFont}px "Microsoft YaHei",sans-serif`;
    for(const target of currentFrame()?.inspection?.displayTargets||[]){
      if(!target.bbox)continue;const [a,b,c,d]=target.bbox;
      const bx=x+a*w,by=y+b*h,bw=Math.max(9,(c-a)*w),bh=Math.max(9,(d-b)*h),color=colors[target.state]||colors.UNKNOWN;
      ctx.setLineDash(target.state==='UNKNOWN'?[5*unit,3*unit]:[]);
      ctx.strokeStyle='#03101f';ctx.lineWidth=4.5*unit;ctx.strokeRect(bx-pad,by-pad,bw+pad*2,bh+pad*2);
      ctx.strokeStyle=color;ctx.lineWidth=2.6*unit;ctx.strokeRect(bx-pad,by-pad,bw+pad*2,bh+pad*2);
      if(isScene()){
        if(target.polygon?.length){ctx.beginPath();target.polygon.forEach((point,i)=>{const px=x+point[0]*w,py=y+point[1]*h;i?ctx.lineTo(px,py):ctx.moveTo(px,py);});ctx.closePath();ctx.stroke();}
        for(const point of target.keypoints||[])if(point[2]>=.3){ctx.beginPath();ctx.arc(x+point[0]*w,y+point[1]*h,3*unit,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();}
      }
      ctx.setLineDash([]);
      const text=target.id,tw=ctx.measureText(text).width+8*unit;
      const tx=Math.max(0,Math.min(bx-pad,canvas.width-tw)),ty=Math.max(0,by-pad-targetHeight);
      ctx.fillStyle='#07172bf0';ctx.fillRect(tx,ty,tw,targetHeight);
      ctx.strokeStyle=color;ctx.lineWidth=1.2*unit;ctx.strokeRect(tx,ty,tw,targetHeight);
      ctx.fillStyle=color;ctx.fillText(text,tx+4*unit,ty+targetHeight/2);
      state.drawnBoxes.push({id:target.id,bbox:target.bbox,state:target.state,mode:target.positionMode});
    }
    ctx.restore();
    const t=state.mode==='realtime'?(performance.now()-state.started)/1000:video.currentTime;
    const duration=state.mode==='realtime'?t:state.result?.source.duration||video.duration||0;
    get('#video-time').textContent=`${fmtTime(t)} / ${state.mode==='realtime'?'实时':fmtTime(duration)}`;
    get('#video-seek').max=duration;get('#video-seek').value=t;
    const head=get('#timeline-playhead');if(head){head.hidden=false;head.style.left=(duration?t/duration*100:0)+'%';head.querySelector('span').textContent=t.toFixed(1)+'s';}
  }
  function renderPose(){
    const frame=currentFrame(),people=frame?.people||[],primary=people.find(p=>p.primary);
    state.renderedPanelFrame=frame;
    const visible=people.reduce((sum,p)=>sum+(p.visibleKeypoints||0),0),coverage=people.length?Math.round(visible/(people.length*17)*100):0;
    get('#clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
    get('#run-state').textContent=state.running?'人体实时检测中':state.final?'检测已结束':state.result?'人体视频回放':'等待检测';
    setText(get('.cpu-chip'),state.mode==='realtime'?(state.frame?.model||state.model?.modelName||'人体姿态模型'):(state.result?.model.name||state.model?.modelName||'人体姿态模型'));
    get('#metric-targets').textContent=people.length;get('#metric-alerts').textContent=people.filter(p=>p.action==='unknown').length;
    const fps=state.mode==='realtime'?state.actualFps:state.result?.model.sampleFps;
    get('#metric-fps').textContent=fps?fps.toFixed(1):'—';get('#sample-fps').textContent=get('#metric-fps').textContent;
    const processing=frame?.inferenceMs??state.processing;get('#metric-latency').textContent=processing?Math.round(processing):'—';
    get('#metric-compliance').textContent=people.length?'已检出人体':frame?'未检出人体':state.running?'等待采样':'待检测';get('#metric-compliance').className='text-green';
    get('#compliant-count').textContent=people.length;get('#noncompliant-count').textContent=people.filter(p=>p.action==='unknown').length;
    for(const id of ['compliance-rate','video-compliance'])get('#'+id).textContent=people.length?coverage+'%':'—';
    get('#realtime-alert').textContent=people.some(p=>p.action==='unknown')?'姿态待复核':'无';
    get('#current-process').textContent=primary?`${people.length} 人 · P${String(primary.trackId).padStart(2,'0')} 主操作员`:`${people.length} 人`;
    const t=state.mode==='realtime'?(state.started?(performance.now()-state.started)/1000:0):video.currentTime;
    get('#dwell').textContent=t.toFixed(1)+'s';get('#video-empty').hidden=state.mode==='realtime'?!!state.camera:!!state.result;
    const rows=get('#detection-results');
    const rowText=people.map(p=>[p.trackId,p.label,p.confidence,p.visibleKeypoints,frame.time,p.primary].join(':')).join('|');
    if(rows.dataset.poseRows!==rowText){
      rows.replaceChildren(...people.map(p=>{const row=document.createElement('tr');for(const value of [`P${String(p.trackId).padStart(2,'0')}`,p.label,(p.confidence*100).toFixed(0)+'%',`${p.visibleKeypoints}/17`,frame.time.toFixed(2)+'s',p.primary?'主操作员':'人体']){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}return row;}));
      rows.dataset.poseRows=rowText;
    }
    get('#result-note').textContent=frame?`采样 ${frame.time.toFixed(2)}s · ${people.length} 人 · 框与骨骼来自同一采样帧`:state.running?'等待新采样或结果已过期，画面标注已清除':'选择视频或开始摄像头人体检测';
    get('#workflow-name').textContent=taskName();get('#workflow-status').textContent=state.final?'检测已结束':state.running?'实时检测中':state.result?'随回放更新':'等待开始检测';
    get('#workflow-status').dataset.verdict='PENDING';get('#step-counter').textContent=people.length+' 人';
    get('#workflow-description').textContent=primary?`P${String(primary.trackId).padStart(2,'0')} 为当前画面面积最大的人体 · ${primary.label}`:'检测人体后显示骨骼及姿态';
    const details=[['人体目标检测',frame?`${people.length} 人`:'等待采样',Boolean(people.length)],['17点骨骼框架',people.length?`${visible} 个可见关键点 · ${coverage}%`:'低置信关键点隐藏',Boolean(visible)],['姿态规则分析',primary?primary.label:'等待人体姿态',Boolean(primary&&primary.action!=='unknown')]];
    const steps=get('#steps'),schema=JSON.stringify(details);
    if(steps.dataset.poseSteps!==schema){steps.innerHTML=details.map(([name,detail,done],i)=>`<div class="step${done?' completed':''}"><div class="step-top"><span class="step-index">${i+1}</span><span class="step-name">${name}</span><span class="step-status">${done?'已观察':'待观察'}</span></div><div class="step-detail">${escapeHTML(detail)}</div></div>`).join('');steps.dataset.poseSteps=schema;}
    get('#sticker-sequence').hidden=true;get('#screw-verdict').hidden=true;
    get('.compliance-foot').textContent='仅人体检测、骨骼和姿态观察';
    get('#finish-detection').disabled=state.busy||(!state.running&&!state.result);get('#start-live').disabled=state.running||state.busy||!state.model?.ready;
    for(const id of ['video-mode','server-account','inspection-task'])get('#'+id).disabled=state.busy;
    get('#camera-device').disabled=state.busy||state.running;
    const cancel=get('#cancel-task');cancel.disabled=state.cancelling;cancel.textContent=state.cancelling?'正在取消…':state.upload?'取消上传':'取消任务';
    get('.live').textContent=state.mode==='realtime'?(state.running?'● LIVE':'检测已结束'):(video.paused?'已暂停':'离线回放');
    get('#footer-performance').textContent=state.mode==='realtime'?`人体检测 · 本轮 ${state.frames.length} 个采样 · ${Math.round(state.processing)}ms/次`:state.result?`人体检测 · ${state.result.summary.analyzedFrames} 个采样 · ${state.result.summary.trackCount} 条轨迹`:'服务就绪 · 可上传视频或开启摄像头';
    draw();
  }
  function render(){
    renderContent();
    renderLiveHealth();
    if(!isPilot())get('#start-live').textContent=state.task==='screw'&&state.final?.compliance?.status==='PASS'?'检测下一个工件（F8）':'开始实时检测';
    if(get('#screw-next'))get('#screw-next').disabled=state.busy||state.finishing;
    window.InspectionTrigger?.refresh();
    if(state.mode!=='realtime'&&!isPilot())get('#metric-latency').previousElementSibling.textContent='单帧处理';
  }
  function renderContent(){
    if(isPilot())return;
    if(isScene()){SceneDashboard.render(state,currentFrame());draw();return;}
    if(isPose()){renderPose();return;}
    get('#clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
    get('#run-state').textContent=state.running?'实时检测中':state.final?'检测已结束':state.result?'视频回放':'等待检测';
    setText(get('.cpu-chip'),displayModelName(state.mode==='realtime'?state.model?.modelName||taskName()+'模型':state.result?.model.name||state.model?.modelName||taskName()+'模型'));
    const current=currentTargets(),sample=panelFrame(),panelTargets=sample?.inspection.targets||initialTargets();
    state.renderedPanelFrame=sample;
    const sequence=state.final?.sequence||sample?.inspection.sequence;
    const actionSummary=isAction()?initialTargets().map(t=>{const event=sequence?.events.find(e=>e.id===t.id);return {...t,state:event?'ACTION_DONE':'UNKNOWN',lastSeen:event?.time,score:event?.score};}):null;
    const rawSummary=state.final?.targets||cumulativeTargets(sample)||actionSummary||panelTargets;
    const summary=state.task==='screw'?orderedSummary(rawSummary,sequence):state.task==='sticker'?orderedStickerSummary(rawSummary,sequence):rawSummary,finalDecision=state.final?.compliance;
    const sequenceDecision=state.task==='screw'&&sequence?decision(summary,sequence,Boolean(finalDecision)):null;
    const stickerDecision=!finalDecision&&state.task==='sticker'?stickerTimelyVerdict(sequence,summary):null;
    const canShowTimely=state.mode!=='realtime'||Boolean(currentFrame())&&!state.evidenceGaps.length;
    const timelyDecision=!finalDecision&&canShowTimely?(state.task==='screw'&&sequence?.status==='PASS'?sequenceDecision:state.task==='sticker'?stickerDecision:null):null;
    const immediateFailure=!finalDecision&&state.task==='screw'&&sequence?.status==='FAIL'?sequenceDecision:null;
    const displayDecision=finalDecision||timelyDecision||immediateFailure;
    state.timelyDecision=timelyDecision;
    const counts=sequenceCounts(summary,sequence);
    const sequenceFailed=sequence?.status==='FAIL';
    const targets=state.task==='screw'?summary:current;
    const confirmed=state.task==='screw'?counts.confirmed:summary.filter(completed).length;
    const empty=summary.filter(t=>['EMPTY','WRONG'].includes(t.state)).length;
    get('#metric-targets').textContent=confirmed;
    get('#metric-alerts').textContent=state.task==='screw'?(sequenceFailed?1:0):displayDecision?.status==='FAIL'?1:empty;
    get('#metric-fps').textContent=state.mode==='realtime'?(state.actualFps?state.actualFps.toFixed(1):'—'):state.result?(state.result.summary.sampleFps||4).toFixed(1):'—';get('#sample-fps').textContent=get('#metric-fps').textContent;
    get('#metric-latency').textContent=state.processing?Math.round(state.processing):'—';
    get('#metric-compliance').textContent=displayDecision?.label||(state.task==='screw'&&sequence?.status==='REVIEW'?'无法判定':state.running||state.result?'检测中':'待检测');
    get('#metric-compliance').className=(displayDecision?.status==='FAIL'||state.task==='screw'&&sequence?.status==='REVIEW')?'text-yellow':'text-green';
    get('#compliant-count').textContent=state.task==='screw'?counts.confirmed:summary.filter(completed).length;
    get('#noncompliant-count').textContent=state.task==='screw'?(state.final?sequenceProblemCount(summary,sequence):0):summary.filter(t=>['EMPTY','WRONG','NOT_OBSERVED'].includes(t.state)).length;
    for(const id of ['compliance-rate','video-compliance'])get('#'+id).textContent=displayDecision?.label||(state.result||state.running?'待结束':'待检测');
    get('#realtime-alert').textContent=state.task==='screw'?(sequenceFailed?'位置确认不合规':state.final&&empty?'空孔待复核':'无'):displayDecision?.status==='FAIL'?'贴条不合规':empty?'未贴／形状待核对':'无';
    get('#current-process').textContent=isAction()?`${targets.filter(t=>t.state==='CONTACT').length}处当前动作 · ${summary.filter(completed).length}/${summary.length}处已确认`:state.task==='screw'?`${counts.completed}/${counts.expected} 处操作位置 · 预装件 ${counts.preConfirmed}/${counts.preExpected} · 各位置独立累计确认`:`${summary.filter(completed).length}/${summary.length} 处已确认`;
    get('#dwell').textContent=(state.mode==='realtime'?(performance.now()-state.started)/1000:video.currentTime).toFixed(1)+'s';
    get('#video-empty').hidden=state.mode==='realtime'?!!state.camera:!!state.result;
    renderTargetPanels(summary,summary,sample);
    const panelNote=sample?state.mode==='replay'?`回放同步采样 ${sample.time.toFixed(2)}s`:`明细为最近完成采样 ${sample.time.toFixed(2)}s`:'等待完成检测采样';
    get('#result-note').textContent=state.task==='screw'?`本轮已确认 ${counts.confirmed}/${counts.positionsExpected} 处位置 · 操作位置 ${counts.completed}/${counts.expected} · 预装件 ${counts.preConfirmed}/${counts.preExpected}${sample?` · 最近采样 ${sample.time.toFixed(2)}s`:''} · 各位置独立累计确认 · 画面框只来自当前采样`:(state.mode==='realtime'&&state.running&&!currentFrame()?`${panelNote}；当前等待新采样／结果已过期，画面不显示旧框`:sample?.inspection.registered?`${panelNote}；检测框只使用当前有效采样`:`${panelNote}；该采样未定位到工件，画面不显示检测框`);
    if(state.task==='screw'||state.task==='sticker'){
      const people=currentFrame()?.people;
      get('#result-note').textContent+=Array.isArray(people)?` · 当前 ${people.length} 人 · 人体框与骨骼同步显示`:state.result&&!state.result.skeleton?' · 该历史记录未分析人体，重新上传可查看人体与骨骼':' · 等待当前人体与骨骼采样';
    }
    const workflowStatus=finalDecision?`最终：${finalDecision.label}`:timelyDecision?`即时：${timelyDecision.label} · 检测继续`:immediateFailure?`即时：不合规 · 检测继续`:state.task==='screw'&&sequence?.status==='REVIEW'?'无法判定 · 仍有位置待确认':state.task==='screw'&&sequence?.status==='PENDING'?`检测中 · 已确认 ${counts.confirmed}/${counts.positionsExpected} 处` :state.running?'实时检测中':state.result?'随回放更新 · 待结束判定':'等待开始检测';
    get('#workflow-name').textContent=taskName();get('#workflow-status').textContent=workflowStatus;
    get('#workflow-status').dataset.verdict=displayDecision?.status||(state.task==='screw'?sequence?.status:null)||'PENDING';
    get('#step-counter').textContent=state.task==='screw'?`${counts.completed}/${counts.expected} 处操作位置`:`${summary.filter(completed).length}/${summary.length} 处${isAction()?'动作':''}`;
    get('#workflow-description').textContent=finalDecision?finalDecision.reason:timelyDecision?(state.task==='sticker'?`全部${isAction()?'贴放/按压动作':'工序位置'}已确认，已即时判定为${timelyDecision.label}；${state.mode==='realtime'?'摄像头检测继续':'视频回放继续'}，无需等待结束。`:`全部位置已取得累计视觉确认，已即时判定为${timelyDecision.label}；${state.mode==='realtime'?'摄像头检测继续':'视频回放继续'}，可手动结束。`):immediateFailure?`位置确认不合规：${sequence.reason}；检测继续，可手动结束。`:state.task==='screw'?(sequence?.status==='REVIEW'?`位置证据不足：${sequence.reason}`:`本轮已确认 ${counts.confirmed}/${counts.positionsExpected} 处位置，操作位置 ${counts.completed}/${counts.expected}，预装件 ${counts.preConfirmed}/${counts.preExpected}；各位置独立累计，不要求安装先后顺序；${state.mode==='realtime'?'当前帧只更新检测框，不清除已确认位置':'回放显示截至当前采样的累计确认'}。`):`${panelNote}${state.mode==='realtime'&&state.running&&!currentFrame()?' · 等待新结果，尚非最终判定':sample&&!sample.inspection.registered?' · 该采样未定位到可核对工件':''}；${isAction()?'本轮动作记录：先两处三角，再六处方形；有动作即计完成':'历史外观记录：先两处三角，再贴后续方形'}。`;
    get('#sticker-sequence').hidden=state.task!=='sticker';
    if(state.task==='sticker'){
      setText(get('#sticker-triangle-count'),`${sequence?.trianglesObserved||0}/2`);
      setText(get('#sticker-rectangle-count'),`${sequence?.rectanglesObserved||0}/${sequence?.rectanglesExpected||state.model?.targets.filter(t=>t.expected==='rectangle').length||0}`);
      setText(get('#sticker-sequence-note'),sequence?sequence.stage+' · '+sequence.reason:isAction()?'等待动作：先两处三角，再六处方形':'历史外观检测：等待视觉证据');
      get('#sticker-sequence').dataset.status=sequence?.status||'PENDING';
    }
    const banner=get('#screw-verdict');banner.hidden=!displayDecision;if(displayDecision){banner.dataset.verdict=displayDecision.status;banner.dataset.mode=finalDecision?'final':'timely';banner.textContent=finalDecision?`最终判定：${displayDecision.label} · ${displayDecision.reason}（${taskScope()}）`:immediateFailure?`即时告警：不合规 · ${displayDecision.reason}（检测继续，可手动结束）`:`即时判定：${displayDecision.label} · ${displayDecision.reason}（检测继续，可手动结束）`;}
    get('.compliance-foot').textContent=finalDecision?'判定范围：'+taskScope():displayDecision?'已即时判定；检测继续，可手动结束':state.task==='screw'?'各位置独立累计确认；全部操作位置和预装件确认后即时显示合规':'全部工序确认后即时弹出结果；检测继续，可手动结束';
    get('#finish-detection').disabled=state.busy||(!state.running&&!state.result);get('#start-live').disabled=state.running||state.busy||!state.model?.ready;
    get('#video-mode').disabled=state.busy;get('#server-account').disabled=state.busy;get('#inspection-task').disabled=state.busy;
    get('#camera-device').disabled=state.busy||state.running;
    const activeRun=['QUEUED','RUNNING','CANCELLING'].includes(state.runStatus);
    const cancelButton=get('#cancel-task');
    if(cancelButton){cancelButton.disabled=state.cancelling;cancelButton.title=state.cancelling?'取消请求已提交':state.upload?'中止当前上传':activeRun?'取消当前分析任务':'当前没有运行中的分析任务';setText(cancelButton,state.cancelling?'正在取消…':state.upload?'取消上传':'取消任务');}
    get('.live').textContent=state.mode==='realtime'?(state.running?'● LIVE':'检测已结束'):(video.paused?'已暂停':'离线回放');
    get('#footer-performance').textContent=state.running?`实时${objectName()}检测 · 已处理 ${state.frames.length} 帧 · ${Math.round(state.processing)}ms/次`:state.result?`${taskName()} · 原视频分析 ${state.result.summary.analyzedFrames} 帧`:'服务就绪 · 可上传视频或开启摄像头';
    draw();
    if(state.task==='sticker'&&timelyDecision&&!finalDecision&&!state.final&&!state.finishing&&!state.busy){
      const key=timelyRunKey();
      if(state.timelyModalKey!==key){
        state.timelyModalKey=key;showStickerTimelyModal(timelyDecision);
      }
    }
    if(timelyDecision&&!state.timelyShown){state.timelyShown=true;toast(`${state.task==='sticker'?'全部工序':'全部位置'}已确认 · 即时判定：${timelyDecision.label}`);}
    if(!timelyDecision&&!finalDecision)state.timelyShown=false;
  }
  function renderAlerts(){
    if(isPilot()){PilotDashboard.renderAlerts(window.StationOne?.state);return;}
    if(isScene()){SceneDashboard.alerts(state,currentFrame());return;}
    if(isPose()){
      const unknown=(currentFrame()?.people||[]).filter(p=>p.action==='unknown');
      const query=get('#alert-search').value.trim();
      const selected=unknown.filter(p=>(`P${p.trackId} ${p.label} ${p.evidence||''}`).includes(query));
      get('#alert-summary').textContent=unknown.length+' 人姿态待复核';get('#alert-count').textContent=unknown.length+' 条提示';
      get('#alert-count').textContent=selected.length+' 条提示';
      get('#alert-list').innerHTML=selected.map(p=>`<article class="alert-card"><div class="alert-content"><h4>P${p.trackId} 姿态证据不足</h4><p class="alert-description">${escapeHTML(p.evidence||'请检查遮挡和画面清晰度')}</p></div></article>`).join('')||'<div class="empty-alerts"><span>当前没有匹配的姿态识别提示</span><small>人体框和骨骼按当前采样更新</small></div>';
      return;
    }
    const screw=state.task==='screw';
    const targets=state.final?.targets||cumulativeTargets()||currentTargets();
    const process=state.final?.sequence||panelFrame()?.inspection.sequence;
    const issues=screw?targets.filter(t=>state.final&&(['EMPTY','PREASSEMBLED_MISSING'].includes(t.sequenceState)||
      process?.status==='REVIEW'&&['PENDING','PREASSEMBLED_UNKNOWN','UNKNOWN'].includes(t.sequenceState))):
      targets.filter(t=>['EMPTY','WRONG','NOT_OBSERVED'].includes(t.state)||state.final&&t.state==='UNKNOWN');
    const orderIssues=state.task==='sticker'?process?.violations||[]:[];
    const sequenceIssues=screw&&process?.status==='FAIL'?[{id:'SEQUENCE',reason:process.reason||'位置确认不合规'}]:[];
    const query=get('#alert-search').value.trim();
    const selected=issues.filter(t=>(t.id+t.name+(screw?sequenceLabel(t):labels[t.state])).includes(query));
    const total=issues.length+orderIssues.length+sequenceIssues.length;
    get('#alert-summary').textContent=`待复核 ${total} 处`;get('#alert-count').textContent=(selected.length+orderIssues.length+sequenceIssues.length)+' 条记录';
    const cards=selected.map(t=>{
      if(screw){
        const description=t.sequenceState==='EMPTY'?'该位置有明确空孔证据，不能判定通过。':
          t.sequenceState==='PREASSEMBLED_MISSING'?'预装件明确缺失，请复核装配。':
          '本轮结束时仍未取得明确的位置视觉证据。';
        return `<article class="alert-card"><div class="alert-content"><h4>${escapeHTML(t.id)} ${sequenceLabel(t)}</h4><p class="alert-description">${escapeHTML(t.name)}，${description}</p></div></article>`;
      }
      return `<article class="alert-card"><div class="alert-content"><h4>${escapeHTML(t.id)} ${labels[t.state]}</h4><p class="alert-description">${escapeHTML(t.name)}，${t.state==='EMPTY'?(state.task==='sticker'?'当前帧有明确未贴证据，请核对工序进度。':'观察到空孔，请复核安装。'):t.state==='WRONG'?'当前帧贴条形状不符。':t.state==='NOT_OBSERVED'?'本轮结束时未取得该位置的贴放/按压动作证据。':'画面证据不足。'}</p></div></article>`;
    });
    cards.push(...orderIssues.map(v=>`<article class="alert-card"><div class="alert-content"><h4>贴条顺序错误</h4><p class="alert-description">${escapeHTML(v.reason)}</p></div></article>`));
    cards.push(...sequenceIssues.map(v=>`<article class="alert-card"><div class="alert-content"><h4>螺丝位置确认不合规</h4><p class="alert-description">${escapeHTML(v.reason)}</p></div></article>`));
    const emptyText=screw?(process&&!process.configured?'当前视角没有可用位置清单':'当前没有明确位置告警'):
      `当前没有明确${state.task==='sticker'?'动作遗漏或错序':'空孔'}告警`;
    const emptyHint=screw?(process&&!process.configured?'没有标定位置时不会判定通过':process?.status==='PASS'?'全部位置已累计确认':'全部操作位置和预装件确认后即时显示合规'):
      state.timelyDecision?'已即时判定；检测继续':'全部工序确认后即时弹出结果；否则结束时汇总';
    get('#alert-list').innerHTML=cards.join('')||`<div class="empty-alerts"><span>${emptyText}</span><small>${emptyHint}</small></div>`;
  }
  function timeline(){
    if(isPilot())return;
    const frames=state.mode==='realtime'?state.frames:state.result?.frames||[];
    get('#track-name').textContent=isScene()?'采样':objectName();
    get('#time-bars').innerHTML=frames.filter((_,i)=>i%Math.max(1,Math.ceil(frames.length/100))===0).map(f=>`<span style="flex:1;background:${(isPose()?f.people?.length:f.inspection.registered)?'#39bd95':'#736e4c'}" title="${f.time.toFixed(1)}s"></span>`).join('');
    const begin=(isPose()||isScene())&&state.mode==='realtime'?(frames[0]?.time||0):0;
    const end=isPose()||isScene()?(state.result?.source.duration||frames.at(-1)?.time||0):30;
    get('.timeline-plot .axis').querySelectorAll('span').forEach((node,i)=>node.textContent=(begin+(end-begin)*i/6).toFixed(isPose()||isScene()?1:0)+'s');
  }
  function showStickerTimelyModal(verdict){
    const confirmed=verdict.confirmedActions??verdict.confirmedPositions??verdict.operationsCompleted??0;
    const expected=verdict.expectedActions??verdict.expectedPositions??verdict.operationsExpected??0;
    const evidence=isAction()?`${confirmed}/${expected} 处贴放/按压动作已确认`:`${confirmed}/${expected} 处工序位置已确认`;
    showModal('贴条工序即时判定',`<div class="screw-final-card" data-verdict="${verdict.status}"><h2>${escapeHTML(verdict.label)}</h2><p>${escapeHTML(verdict.reason)}</p><p>${evidence}</p></div><p class="hint">检测仍在继续，无需等待视频结束；可关闭此窗口继续观察。${isAction()?'本判定按贴放/按压动作计完成，不核验黑色贴片外观或实际粘附。':''}</p><div class="modal-actions"><button class="btn btn-primary" data-action="close-modal">关闭并继续检测</button></div>`);
  }
  function presentFinal(result,options={}){
    state.final=result;render();renderAlerts();
    if(isScene()){state.finalShown=true;return;}
    if(options.skipModal){state.finalShown=true;return;}
    if(!state.finalShown){
      state.finalShown=true;
      const counts=sequenceCounts(result.targets,result.sequence);
      const evidence=state.task==='screw'?`${counts.completed}/${counts.expected} 处操作位置已确认 · 预装件 ${counts.preConfirmed}/${counts.preExpected} 已确认`:`${result.targets.filter(completed).length}/${result.targets.length} 处取得${isAction()?'贴放/按压动作':objectName()}证据`;
      const passed=state.task==='screw'&&result.compliance.status==='PASS';
      showModal(passed?'检测合规':'本次检测最终判定',`<div class="screw-final-card" data-verdict="${result.compliance.status}"><h2>${passed?'检测合规':result.compliance.label}</h2><p>${escapeHTML(result.compliance.reason)}</p><p>${evidence}</p>${result.sequence?'<p>'+escapeHTML(result.sequence.reason)+'</p>':''}</div>${passed?'<p>本轮检测已结束。更换工件后可开始下一轮。</p>':''}<p class="hint">${taskScope()}。${state.task==='sticker'?'动作识别效果仍需独立工位样本验证。':'真实漏打检出率仍需缺陷样本验证。'}</p><div class="modal-actions"><button class="btn" data-action="close-modal">关闭</button>${passed?'<button class="btn btn-primary" id="screw-next">检测下一个工件</button>':''}</div>`);
      if(passed){get('#screw-next').disabled=state.busy||state.finishing;get('#screw-next').onclick=()=>{closeModal();state.mode==='realtime'?startLive():showUploadDialog();};}
    }
  }
  function finishReplay(event){
    if(isPilot())return;
    if(!state.result)return;video.pause();
    if(isScene()){presentFinal({...state.result.frames.at(-1),final:true});return;}
    if(isPose()){state.final={final:true};render();renderAlerts();tell('人体检测已结束 · 可回放骨骼与姿态结果');return;}
    const targets=state.result.frames.at(-1)?.aggregateTargets||state.result.targets;
    const sequence=state.result.sequence||state.result.frames.at(-1)?.inspection.sequence||null;
    const compliance=state.task==='screw'?decision(targets,sequence,true):state.result.compliance||decision(targets,sequence,true);
    const skipModal=event?.type==='ended'&&state.task==='sticker'&&state.timelyModalKey===timelyRunKey();
    presentFinal({targets,sequence,compliance,final:true},{skipModal});
  }
  async function finishLive({stopReason=''}={}){
    if(!state.streamId||state.finishing)return;
    const wasPreparing=state.preparing;
    recordGap();state.interruptedAt=null;state.finishing=true;state.running=false;state.busy=true;clearTimeout(state.timer);
    const id=state.streamId,body=JSON.stringify({evidenceGaps:state.evidenceGaps.slice(0,100).map(({start,end,reason})=>({start,end,reason:String(reason).slice(0,160)}))});
    state.liveController?.abort();stopTracks();render();
    try{
      if(state.pending)await state.pending.catch(()=>{});
      if(wasPreparing){state.final=null;tell('模型准备已取消 · 尚未开始本轮检测');}
      else if(isPose()){state.final={final:true};tell('人体检测已结束 · 本轮 '+state.frames.length+' 个采样');}
      else{
        let result;
        for(let attempt=0;attempt<3;attempt++){
          try{result=await request(`/realtime/streams/${id}/finish?analysis_type=${taskType()}`,{method:'POST',timeout:5000,headers:{'Content-Type':'application/json'},body});break;}
          catch(error){if(attempt===2||![429,503].includes(error.status))throw error;await new Promise(resolve=>setTimeout(resolve,Math.max(500,Math.min(1500,error.retryAfterMs||1000))));}
        }
        const protectedResult=protectVerdict(result);
        if(stopReason){protectedResult.originalCompliance=protectedResult.compliance;protectedResult.compliance={status:'REVIEW',label:'自动停止 · 待复核',reason:stopReason};}
        presentFinal(protectedResult);tell(stopReason||'实时检测已结束 · '+protectedResult.compliance.label);
      }
    }
    catch(e){state.final={targets:initialTargets(),compliance:{status:'REVIEW',label:'无法判定',reason:e.message},final:true};render();tell('检测已停止，最终结果无法获取：'+e.message);}
    finally{state.preparing=false;state.streamId=null;state.liveController=null;await request(`/realtime/streams/${id}`,{method:'DELETE'}).catch(()=>{});state.busy=false;state.finishing=false;render();}
  }
  function stopTracks(){if(state.camera?.readyState>=2){state.frozen=document.createElement('canvas');state.frozen.width=state.camera.videoWidth;state.frozen.height=state.camera.videoHeight;state.frozen.getContext('2d').drawImage(state.camera,0,0);}state.stream?.getTracks().forEach(t=>t.stop());state.stream=null;if(state.camera)state.camera.srcObject=null;}
  async function stopCurrent(){if(state.streamId)await finishLive();video.pause();}
  function stopBlocked(){
    if(state.switching||state.finishing||state.cancelling)return '当前操作尚未完成';
    if(isPilot())return PilotDashboard.stopBlocked();
    if(state.upload||state.run&&['QUEUED','RUNNING'].includes(state.runStatus))return '';
    if(state.busy)return '当前操作尚未完成';
    return state.streamId||state.result&&!state.final?'':'当前没有运行中的任务';
  }
  function stopFromTrigger(){
    if(isPilot())return PilotDashboard.stop();
    if(state.upload||state.run&&['QUEUED','RUNNING'].includes(state.runStatus))return cancelTask();
    state.finalShown=false;
    return state.mode==='realtime'?finishLive():finishReplay();
  }
  async function cancelTask(){
    if(state.upload){tell('正在取消上传…');toast('正在取消上传');state.upload.abort();return;}
    if(!state.run){const message=state.busy?'任务正在创建，请稍候…':'当前没有可取消的任务';tell(message);toast(message);return;}
    if(state.cancelling){const message='正在取消任务，请稍候…';tell(message);toast(message);return;}
    if(!['QUEUED','RUNNING','CANCELLING'].includes(state.runStatus)){tell('当前任务已结束，无需取消');toast('当前任务已结束');return;}
    state.cancelling=true;tell('正在取消任务…');toast('正在取消任务');render();
    try{
      const job=await request(`/analysis-runs/${state.run}/cancel`,{method:'POST'});
      state.runStatus=job.status;
      if(job.status==='CANCELLED'){state.cancelling=false;tell('任务已取消');toast('任务已取消');}
      else if(job.status==='CANCELLING'){state.cancelling=true;tell('正在取消任务…');}
      else{state.cancelling=false;tell('任务已结束 · '+job.status);toast('任务已结束');}
      render();
    }catch(e){state.cancelling=false;tell('取消失败：'+e.message);toast('取消失败：'+e.message);render();}
  }
  async function loadResult(id){const generation=state.generation;await stopCurrent();if(generation!==state.generation)return;const result=await request(`/analysis-runs/${id}/artifacts/analysis.json`);if(generation!==state.generation)return;if(result.analysisType!==taskType()||isScene()&&result.sceneId!==sceneId())throw new Error('请选择当前检测类型的独立任务');state.result=result;applyTaskText();state.run=id;state.runStatus='SUCCEEDED';state.cancelling=false;state.mode='replay';state.finishing=false;state.final=null;state.finalShown=false;state.frame=null;state.frames=[];state.aggregateTargets=[];state.liveCompliance=null;state.timelyDecision=null;state.timelyShown=false;state.timelyModalKey=null;get('#video-mode').value='replay';get('#play-button').disabled=false;get('#video-seek').disabled=false;if(isScene())result.frames=result.frames.map(SceneDashboard.normalize);else prepareFrames(result);video.loop=false;video.src=`/api/v1/analysis-runs/${id}/artifacts/video.webm`;get('.camera-name').textContent=result.source.title;get('.stream-info').textContent=isScene()?`${result.sceneName} · ${result.model.name} v${result.model.version}`:isPose()?`离线人体检测 · ${result.summary.trackCount} 条轨迹 · 17点骨骼`:`离线分析 · ${result.summary.expected}处${objectName()}`;get('#mode-text').textContent=isScene()?'任务视频回放 · 目标与规则按当前采样同步':isPose()?'人体检测回放 · 人体框与骨骼按采样同步':'视频检测回放 · 画面框仅标注当前采样检出 · 全部确认后即时显示结果';tell(isScene()?'分析已完成 · 可回放当前任务的目标、规则和判定':isPose()?'人体分析已完成 · 可回放人体框、骨骼和姿态':'分析已完成 · 全部确认后即时显示结果，检测继续，也可手动结束');timeline();render();}
  function login(then){
    if(get('#screw-login')&&get('#modal').open)return;
    const task=state.task;
    showModal('登录服务器',`<form id="screw-login"><label class="form-label">用户名<input id="login-user" class="field" autocomplete="username" value="admin" maxlength="100" required></label><label class="form-label">密码<input id="login-password" class="field" type="password" autocomplete="current-password" maxlength="200" required></label><p id="login-error" role="alert"></p><div class="modal-actions"><button class="btn btn-primary" type="submit">登录</button></div></form>`);
    Workspace.bindLoginForm(get('#modal'),get('#screw-login'),async()=>{
      if(task!==state.task)return;
      if(then)await then();else{tell('已登录 · 请选择视频、开启摄像头或从检测记录手动打开');render();renderAlerts();}
    });
  }
  async function startLive(){
    if(state.busy||state.running||state.switching||state.finishing)return;if(!state.session){login(startLive);return;}if(!state.model?.ready){tell(taskName()+'模型未就绪');return;}state.busy=true;render();await stopCurrent();state.generation++;const generation=state.generation;state.run=null;state.runStatus=null;state.cancelling=false;state.finishing=false;state.final=null;state.finalShown=false;state.mode='realtime';state.result=null;applyTaskText();state.frames=[];state.frame=null;state.frozen=null;state.samplePicture=null;state.actualFps=null;state.lastResponse=null;state.aggregateTargets=[];state.liveCompliance=null;state.timelyDecision=null;state.timelyShown=false;state.timelyModalKey=null;render();
    Object.assign(state,{liveFailures:0,liveIssue:null,interruptedAt:null,evidenceGaps:[],lastSampleAt:null,frameSequence:0,displayLatencyMs:null,latencySamples:[],lastCameraTime:null,lastCameraAdvance:null,sampleSpare:null,preparing:true,preparationMs:null,preparationStarted:null});
    try{if(!navigator.mediaDevices?.getUserMedia)throw new Error('摄像头需要本机地址或HTTPS');const deviceId=get('#camera-device').value;const liveSize=state.task==='sticker'?{width:{ideal:1080},height:{ideal:1920}}:{width:{ideal:1920},height:{ideal:1080}};const stream=await navigator.mediaDevices.getUserMedia({video:{...liveSize,...(deviceId?{deviceId:{exact:deviceId}}:{})},audio:false});if(generation!==state.generation){stream.getTracks().forEach(t=>t.stop());return;}state.stream=stream;state.camera=get('#live-camera');state.camera.srcObject=stream;await state.camera.play();state.started=performance.now();state.streamId=crypto.randomUUID();state.running=true;get('#video-mode').value='realtime';get('#play-button').disabled=true;get('#video-seek').disabled=true;get('.camera-name').textContent='实时摄像头 · '+taskName();get('.stream-info').textContent=isScene()?'摄像头采样 · '+state.model.modelName:isPose()?'人体框 + 17点骨骼 · 当前采样':'摄像头采样 · 全部位置确认后即时显示结果';get('#mode-text').textContent=isScene()?'当前模型实时检测 · 过期采样清除目标与判定':isPose()?'实时人体检测 · 人体框与骨骼来自同一采样':'实时检测画面 · 当前帧检出才画框 · 全部确认后即时显示结果';await cameras();tell(isScene()?'任务实时检测中 · 按配置规则逐项判定':isPose()?'人体实时检测中 · 未检出或采样过期时清除标注':state.task==='screw'?'实时检测中 · 全部位置确认后自动结束，可检测下一个工件':'实时检测中 · 全部位置确认后即时显示结果，检测继续');tick(generation);}
    catch(e){stopTracks();state.running=false;state.preparing=false;tell('无法启动实时检测：'+e.message);}
    finally{state.busy=false;for(const track of state.stream?.getVideoTracks()||[]){for(const name of ['ended','mute'])track.addEventListener(name,()=>{if(generation===state.generation&&state.running)interruptLive(name==='ended'?'摄像头已断开，请结束本轮后重新开启':'摄像头暂时无有效画面');});}render();}
  }
  async function tick(generation){
    if(!state.running||generation!==state.generation||state.pending)return;
    const maximum=state.streamLimits?.maxDurationSeconds??600;
    if(!state.preparing&&performance.now()-state.started>=maximum*1000){
      const reason=`本轮检测已达到最长 ${maximum} 秒，已自动停止；请确认工件后重新开始`;
      await finishLive({stopReason:reason});tell(reason);return;
    }
    if(document.hidden||!navigator.onLine){interruptLive(document.hidden?'页面在后台，采样已暂停':'网络已断开，等待恢复');recordGap();state.timer=setTimeout(()=>tick(generation),500);return;}
    const camera=state.camera,track=state.stream?.getVideoTracks()[0];
    if(!camera||camera.readyState<2||!camera.videoWidth||!camera.videoHeight||track?.readyState==='ended'||track?.muted){
      interruptLive('摄像头暂时无有效画面');recordGap();state.timer=setTimeout(()=>tick(generation),500);return;
    }
    const started=performance.now();
    if(state.lastCameraTime===camera.currentTime){
      if(started-(state.lastCameraAdvance??started)>=1000)interruptLive('摄像头画面停止更新');
      recordGap();state.timer=setTimeout(()=>tick(generation),100);return;
    }
    state.lastCameraTime=camera.currentTime;state.lastCameraAdvance=started;
    const preparing=state.preparing,controller=new AbortController(),streamId=state.streamId,frameId=String(++state.frameSequence);
    if(preparing)state.preparationStarted??=started;
    state.liveController=controller;let retryMs=0,complete=false;
    const pending=(async()=>{
      const portrait=state.task==='sticker'&&camera.videoHeight>camera.videoWidth;
      const scale=Math.min(1,(isPose()?1280:portrait?1080:1920)/camera.videoWidth,(isPose()?720:portrait?1920:1080)/camera.videoHeight);
      const sample=state.sampleSpare||(state.sampleSpare=document.createElement('canvas'));
      const width=Math.round(camera.videoWidth*scale),height=Math.round(camera.videoHeight*scale);
      if(sample.width!==width)sample.width=width;if(sample.height!==height)sample.height=height;
      sample.getContext('2d').drawImage(camera,0,0,width,height);
      const capturedAt=Date.now();
      const encode=quality=>new Promise((resolve,reject)=>sample.toBlob(blob=>blob?resolve(blob):reject(new Error('摄像头帧编码失败')),'image/jpeg',quality));
      let blob=await encode(state.task==='sticker'?.94:.82);
      if(blob.size>1024*1024)blob=await encode(.6);
      if(controller.signal.aborted||!state.running||generation!==state.generation)return;
      if(preparing)tell('模型准备中 · 请等待准备完成后再开始操作');
      const data=await request('/realtime/frame?analysis_type='+taskType()+(isScene()?'&scene_id='+encodeURIComponent(sceneId()):''),{method:'POST',timeout:preparing?state.preparationTimeoutMs:state.frameTimeoutMs,signal:controller.signal,headers:{'Content-Type':'image/jpeg','X-Stream-Id':streamId,'X-Frame-Id':frameId,'X-Captured-At':String(capturedAt),'X-Frame-Time':String((started-state.started)/1000)},body:blob});
      if(controller.signal.aborted||generation!==state.generation||streamId!==state.streamId||!state.running)return;
      if(data.frameId!=null&&String(data.frameId)!==frameId)throw new Error('采样编号不匹配，已忽略响应');
      state.streamLimits=data.streamLimits;
      const received=performance.now();
      if(preparing){
        // Model loading belongs to a disposable stream, never the production cycle.
        await request(`/realtime/streams/${streamId}`,{method:'DELETE',timeout:5000,signal:controller.signal}).catch(()=>{});
        if(controller.signal.aborted||generation!==state.generation||streamId!==state.streamId||!state.running)return;
        const ready=performance.now();
        Object.assign(state,{preparing:false,preparationMs:ready-state.preparationStarted,preparationStarted:null,
          streamId:crypto.randomUUID(),started:ready,frameSequence:0,frames:[],frame:null,aggregateTargets:[],liveCompliance:null,
          liveFailures:0,liveIssue:null,interruptedAt:null,evidenceGaps:[],lastSampleAt:null,lastResponse:null,actualFps:null,
          displayLatencyMs:null,latencySamples:[],lastCameraTime:null,lastCameraAdvance:null,processing:0});
        render();timeline();renderAlerts();tell('模型准备完成 · 现在可以开始本轮操作');
        return;
      }
      recordGap(received);
      state.liveFailures=0;state.liveIssue=null;state.interruptedAt=null;
      if(state.lastSampleAt!=null&&started-state.lastSampleAt>=EVIDENCE_GAP_MS&&state.evidenceGaps.length<100)state.evidenceGaps.push({start:(state.lastSampleAt-state.started)/1000,end:(started-state.started)/1000,reason:'连续有效采样间隔超过3秒'});
      state.lastSampleAt=started;state.sampleSpare=state.samplePicture;state.samplePicture=sample;
      mergeEvidenceGaps(data.evidenceGaps);
      state.aggregateTargets=data.aggregateTargets||data.historyTargets||[];state.liveCompliance=data.compliance||null;
      state.frame=isScene()?SceneDashboard.normalize(data):{time:data.frameTime,inspection:data.inspection,aggregateTargets:state.aggregateTargets,people:data.people,skeleton:data.skeleton,model:data.model};
      state.frames.push(state.frame);if(state.frames.length>600)state.frames.shift();
      state.processing=data.serverProcessingMs??data.processingMs??data.inferenceMs??0;
      if(state.lastResponse!=null)state.actualFps=1000/(received-state.lastResponse);state.lastResponse=received;
      state.displayLatencyMs=received-started;render();timeline();renderAlerts();
      state.displayLatencyMs=performance.now()-started;state.latencySamples.push(state.displayLatencyMs);if(state.latencySamples.length>300)state.latencySamples.shift();
      renderLiveHealth();tell(currentFrame()?'实时采样已连接 · 按当前帧显示检测结果':'采样耗时超过有效期 · 已清除过期画面标注');
      complete=confirmedScrewCoverage(data);
    })();
    state.pending=pending;
    try{await pending;}catch(error){
      if(!state.running||generation!==state.generation)return;
      state.liveFailures++;state.liveIssue=preparing?(error.name==='TimeoutError'?'模型准备超时':error.status===429?'模型准备等待服务空闲':'模型准备连接中断'):error.name==='TimeoutError'?'采样请求超时，正在重连':error.status===429?'服务繁忙，正在等待新采样':'采样连接中断，正在重试';
      state.interruptedAt??=started;recordGap();
      retryMs=Math.max(Math.min(5000,error.retryAfterMs||0),Math.min(4000,250*2**Math.min(state.liveFailures-1,4)));
      if(error.name!=='AbortError')tell(state.liveIssue+'：'+error.message);
      render();renderAlerts();
      if(preparing&&error.name==='TimeoutError'){
        state.pending=null;await finishLive();tell('模型准备超时，尚未开始本轮检测。请检查服务后重新开启。');return;
      }
      if(error.status===401||[403,404,409,410].includes(error.status)){
        state.pending=null;state.evidenceGaps.push({start:Math.max(0,(started-state.started)/1000),end:Math.max(0,(performance.now()-state.started)/1000),reason:error.message});
        await finishLive({stopReason:error.status===410?error.message:''});tell('检测已停止：'+error.message+'。请重新选择任务后开始。');return;
      }
    }finally{if(state.pending===pending)state.pending=null;if(state.liveController===controller)state.liveController=null;}
    // Finalize after clearing pending so finishLive cannot wait on its own frame.
    if(complete&&state.running&&generation===state.generation){await finishLive();return;}
    if(state.running&&generation===state.generation)state.timer=setTimeout(()=>tick(generation),Math.max(retryMs,1000/Math.max(.5,Number(isScene()?state.model.sample_fps:get('#live-rate').value)||4)-(performance.now()-started)));
  }
  async function cameras(){const selector=get('#camera-device'),selected=selector.value;const devices=await navigator.mediaDevices.enumerateDevices();selector.replaceChildren(new Option('默认摄像头',''));for(const d of devices.filter(d=>d.kind==='videoinput'))selector.add(new Option(d.label||'摄像头 '+(selector.length),d.deviceId));selector.value=selected;}
  function showUploadDialog(){
    if(state.busy){const text='当前任务正在处理，请完成或取消后再上传。';tell(text);toast(text);return;}
    if(!state.session){login(showUploadDialog);return;}
    if(!state.model?.ready){tell(taskName()+'模型尚未就绪');toast('当前任务模型尚未就绪');return;}
    if(state.mode==='replay')video.pause();
    showModal('上传视频检测',`<p class="modal-intro">选择工序视频后，点击“开始上传并检测”。视频会上传到当前检测服务器，使用${escapeHTML(displayModelName(state.model?.modelName||taskName()+'专用模型'))}分析。</p><div id="screw-upload-zone" class="upload-zone"><label class="form-label" for="screw-upload-file">选择视频文件（也可拖入此区域）</label><input id="screw-upload-file" class="field" type="file" accept=".mp4,.webm,.mov,.avi,.m4v,video/*"><p>支持 MP4 / WebM / MOV / AVI / M4V，最大500MB。</p></div><p id="screw-upload-selection" role="status">尚未选择视频。</p><p class="hint">若浏览器未弹出文件选择窗口，可将视频直接拖入上方区域。</p><div class="modal-actions"><button class="btn btn-secondary" data-action="close-modal">取消</button><button class="btn btn-primary" id="screw-upload-start" type="button" disabled>开始上传并检测</button></div>`);
    let selected=null;
    const choose=file=>{
      selected=null;get('#screw-upload-start').disabled=true;
      const note=get('#screw-upload-selection');
      if(!file){setText(note,'尚未选择视频。');return;}
      if(!/\.(mp4|webm|mov|avi|m4v)$/i.test(file.name)){setText(note,'请选择支持的MP4、WebM、MOV、AVI或M4V视频。');return;}
      if(!file.size){setText(note,'视频文件为空，请重新选择。');return;}
      if(file.size>500*1024*1024){setText(note,'视频超过500MB，请缩短或压缩后重新选择。');return;}
      selected=file;setText(note,`${file.name} · ${(file.size/1048576).toFixed(1)} MB · 可以开始检测`);get('#screw-upload-start').disabled=false;
    };
    get('#screw-upload-file').onchange=event=>choose(event.target.files[0]);
    const zone=get('#screw-upload-zone');
    zone.ondragover=event=>{event.preventDefault();zone.classList.add('drag-over');};
    zone.ondragleave=()=>zone.classList.remove('drag-over');
    zone.ondrop=event=>{event.preventDefault();zone.classList.remove('drag-over');choose(event.dataTransfer.files[0]);};
    get('#screw-upload-start').onclick=()=>{if(selected){get('#screw-upload-start').disabled=true;upload(selected);}};
  }
  async function upload(file){if(!state.session){login(()=>upload(file));return;}if(state.busy){tell('当前任务正在处理，请完成或取消后再上传');toast('当前任务正在处理');return;}await stopCurrent();closeModal();state.busy=true;state.run=null;state.runStatus=null;state.cancelling=false;tell('正在准备上传 '+file.name+'…');render();try{if(file.size>500*1024*1024)throw new Error('视频超过500MB');const controller=new AbortController();state.upload=controller;render();let media;
    try{media=await Workspace.upload('/media?filename='+encodeURIComponent(file.name),file,percent=>tell('正在上传 '+percent+'%'),{signal:controller.signal});}
    finally{if(state.upload===controller)state.upload=null;};const job=await request('/analysis-runs',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({media_id:media.id,options:{analysis_type:taskType(),...(isScene()?{scene_id:sceneId()}:{sample_fps:4}),...(isPose()?{pose_backend:state.model.offlineBackend}:{})}})});state.run=job.id;state.runStatus=job.status||'QUEUED';state.cancelling=false;await watch(job.id);}catch(e){state.cancelling=false;tell(e.message);}finally{state.busy=false;render();}}
  async function watch(id){
    const selectedTask=state.task,generation=state.generation;
    const statusRequest=async path=>{
      for(let attempt=0;;attempt++){
        if(state.task!==selectedTask||generation!==state.generation)return null;
        try{const job=await request(path);state.watchRetries=0;return job;}
        catch(error){
          const transient=!error.status||error.status===408||error.status===429||error.status>=500;
          if(!transient)throw error;
          if(attempt>=5)throw new Error('任务状态连接仍未恢复，后台任务可能继续执行。请从“检测记录”重新打开任务 '+id+'，或点击取消任务；无需重新上传。');
          state.watchRetries=attempt+1;
          tell(`状态连接中断，后台任务继续 · 正在重连 ${attempt+1}/5 · 无需重新上传`);render();
          await new Promise(resolve=>setTimeout(resolve,Math.max(Math.min(5000,error.retryAfterMs||0),Math.min(5000,500*2**attempt))));
        }
      }
    };
    let job=await statusRequest(`/analysis-runs/${id}`);
    if(!job)return;
    if(state.task!==selectedTask||generation!==state.generation)return;
    state.run=id;state.runStatus=job.status;state.cancelling=state.cancelling||job.status==='CANCELLING';render();
    const statusLabels={QUEUED:'排队中',RUNNING:'分析中',CANCELLING:'正在取消'};
    while(!['SUCCEEDED','FAILED','CANCELLED'].includes(job.status)){
      if(state.task!==selectedTask||generation!==state.generation)return;
      const cancelling=state.cancelling||job.status==='CANCELLING';
      state.runStatus=job.status;state.cancelling=cancelling;
      tell(cancelling?'正在取消任务…':`${taskName()} ${statusLabels[job.status]||job.status} · ${Math.round(job.progress*100)}%`);render();
      job=await statusRequest(`/analysis-runs/${id}/wait?since=${job.updated_at}&timeout=25`);
      if(!job)return;
    }
    if(state.task!==selectedTask||generation!==state.generation)return;
    state.runStatus=job.status;state.cancelling=false;render();
    if(job.status==='CANCELLED'){tell('任务已取消');toast('任务已取消');return;}
    if(job.status!=='SUCCEEDED')throw new Error(job.error_message||'任务失败');
    await loadResult(id);
  }
  async function history(){
    if(!state.session){login(history);return;}
    const generation=state.generation;
    showModal(taskName()+'记录','<div id="screw-history">正在读取…</div>');
    const container=get('#screw-history');
    try{
      const data=await request('/analysis-runs?analysis_type='+taskType()+(isScene()?'&scene_id='+encodeURIComponent(sceneId()):'')+'&limit=50');
      if(generation!==state.generation||!container.isConnected)return;
      container.innerHTML=data.items.map(r=>`<button class="server-run" data-screw-run="${r.id}"><b>${escapeHTML(r.media_name||r.id)}</b><span>${r.status} · ${escapeHTML(r.model_name||'历史模型')} · ${new Date(r.created_at*1000).toLocaleString()}</span></button>`).join('')||'暂无记录';
    }catch(e){if(generation===state.generation&&container.isConnected)container.textContent=e.message;}
  }
  async function loadSceneTasks(){
    const select=get('#inspection-task');if(!select)return;
    const catalog=await request('/scenes');
    for(const option of [...select.options])if(option.dataset.sceneOption)option.remove();
    for(const scene of catalog.items||[]){const option=new Option(scene.name+(scene.ready?'':'（模型不可用）'),'scene:'+scene.id);option.dataset.sceneOption='true';option.disabled=!scene.ready;select.add(option);}select.value=state.task;
  }
  window.addEventListener('focus',()=>loadSceneTasks().catch(()=>{}));
  window.addEventListener('storage',event=>{if(event.key==='aisop-scene-catalog-changed')loadSceneTasks().catch(()=>{});});
  async function init(){
    demo=false;analysisData=null;video.pause();video.removeAttribute('src');video.loop=false;document.body.classList.add('server-mode','screw-original-ui');poster.onload=()=>draw();
    get('.header').insertAdjacentHTML('afterend',`<section class="ops-strip" id="ops-strip"><div class="ops-row"><span id="server-status" role="status">正在连接螺丝检测服务…</span><div class="ops-actions"><button class="btn" id="server-account">登录服务器</button><button class="btn" data-action="screw-history">检测记录</button><button class="btn" id="cancel-task">取消任务</button></div></div></section>`);
    get('.ops-actions').insertAdjacentHTML('beforeend','<button class="btn" id="evidence-history-open">告警录像</button>');
    const openEvidence=()=>EvidenceHistory.open(request,{stationId:isPilot()?'station-'+state.task.slice(7):null});
    get('#evidence-history-open').onclick=()=>state.session?openEvidence():login(openEvidence);
    get('.video-panel .subheading').insertAdjacentHTML('afterend',`<div class="video-mode-control"><label for="inspection-task">检测任务</label><select id="inspection-task" class="field"><option value="screw">螺丝安装检测</option><option value="sticker">贴条工序检测</option><option value="pose">人体检测与骨骼</option><option value="station1">工位1 · 4孔检查后4螺丝</option><option value="station2">工位2 · 5螺丝检查</option><option value="station3">工位3 · 2螺丝与黄色卡扣</option><option value="station4">工位4 · 2螺丝检查</option><option value="station5">工位5 · 接端子后8螺丝</option><option value="station6">工位6 · 吹净、端子与4螺丝</option></select><label for="video-mode">检测模式</label><select id="video-mode" class="field"><option value="replay">视频分析回放</option><option value="realtime">实时摄像头检测</option></select><select id="camera-device" class="field" aria-label="摄像头"><option value="">默认摄像头</option></select><label for="live-rate">采样</label><select id="live-rate" class="field"><option value="2">2 fps</option><option value="4" selected>4 fps</option><option value="5">5 fps</option><option value="10">10 fps</option></select><button class="btn btn-green" id="start-live">开始实时检测</button><button class="btn" id="finish-detection">结束并判定</button></div><div id="screw-verdict" role="status" hidden></div>`);
    get('#steps').insertAdjacentHTML('beforebegin','<section id="sticker-sequence" class="sticker-sequence" hidden><div><span>① 三角贴</span><b id="sticker-triangle-count">0/2</b><span>② 方形贴</span><b id="sticker-rectangle-count">0/0</b></div><p id="sticker-sequence-note" role="status">等待视觉工序证据</p></section>');
    get('#inspection-task').onchange=event=>setTask(event.target.value).catch(error=>{state.switching=false;tell(error.message);});
    window.InspectionTrigger.attach({notify:message=>toast(message),
      blocked:()=>state.switching?'正在切换检测任务':isPilot()?PilotDashboard.startBlocked():!state.session?'请先登录服务器':state.busy||state.finishing?'当前操作尚未完成':state.running?'本轮检测中，重复触发已锁定':!state.model?.ready?'当前模型尚未就绪':'',
      start:()=>isPilot()?PilotDashboard.start():startLive(),stopBlocked,stop:stopFromTrigger});
    loadSceneTasks().catch(()=>{});
    const camera=document.createElement('video');camera.id='live-camera';camera.hidden=true;camera.muted=true;camera.playsInline=true;document.body.append(camera);
    get('.brand p').textContent='螺丝专项模型 · 当前帧检测 · 实时检测与结束合规判定';get('.cpu-chip').textContent='螺丝专用模型 · 连接中';get('.footer>span').textContent='AI-SOP · 螺丝安装视觉检测';
    get('.detection-panel .section-title h2').textContent='实时检测画面';get('.video-panel .subheading h3').innerHTML='<b class="number blue">1</b>螺丝安装检测 / 当前帧识别';get('.pose-legend').innerHTML='<span>● 绿色：可见螺丝</span><span>● 红色：可见空孔</span><span>未检出／未定位：不画框</span>';
    get('.timeline-panel .subheading h3').innerHTML='<b class="number yellow-bg">3</b>螺丝检测时间轴';get('.compliance-panel .subheading h3').innerHTML='<b class="number red-bg">4</b>安装合规判定';get('.compliance-panel .positive span').textContent='已确认位置';get('.compliance-grid>div:nth-child(2) span').textContent='明确空孔／预装件缺失';get('.compliance-grid>div:nth-child(3) span').textContent='合规判断';get('#metric-compliance').previousElementSibling.textContent='合规判断';get('#metric-compliance').nextElementSibling.textContent='';get('#metric-targets').nextElementSibling.textContent='处';get('.pending-step').hidden=true;get('.recent-cycles').hidden=true;get('.alerts-foot').textContent='各位置独立累计确认 · 不要求安装顺序';
    get('.camera-name').textContent='螺丝检测工位 · 等待视频或摄像头';get('.stream-info').textContent='专用模型 · 12处参考位置';get('#mode-text').textContent='等待选择视频或开启摄像头';get('#video-empty p').textContent='查看螺丝位置、检测证据和最终合规结论';get('.timeline-panel .micro-tag').textContent='检测采样';get('.timeline-panel .legend').innerHTML='<span><i style="background:#39bd95"></i>可核对</span><span><i style="background:#736e4c"></i>待确认</span>';get('#metric-fps').previousElementSibling.textContent='检测帧率';
    const headings=get('.results-panel thead').querySelectorAll('th');headings[1].textContent='位置状态';headings[3].textContent='位置名称';headings[4].textContent='证据时间';headings[5].textContent='位置／角色';
    actions.upload=()=>isPilot()?PilotDashboard.action('upload'):showUploadDialog();actions['start-upload']=actions.upload;actions.realtime=()=>isPilot()?PilotDashboard.action('realtime'):startLive();actions['screw-history']=()=>isPilot()?PilotDashboard.action('screw-history'):history();actions['sop-history']=()=>{if(isPilot())return PilotDashboard.action('sop-history');if(isScene())return history();if(isPose()){showModal('人体检测本轮记录',`<p>本轮已处理 ${state.result?.summary.analyzedFrames||state.frames.length} 个采样。</p><p>人体框、骨骼与姿态按同一采样帧显示，主操作员取当前最大人体框。</p>`);return;}const process=state.final?.sequence||panelFrame()?.inspection.sequence;const shown=state.final?.compliance||state.timelyDecision||(process?.status==='FAIL'?decision(state.final?.targets||cumulativeTargets()||currentTargets(),process,true):null);return showModal(taskName()+'本轮记录',`<p>已处理 ${state.result?.summary.analyzedFrames||state.frames.length} 个采样。</p><p>当前状态：${shown?.label||'检测尚未结束'}</p><p>${escapeHTML(shown?.reason||'全部位置累计确认后即时显示结果；结束后汇总本轮位置证据。')}</p>`);};actions.workflow=()=>showModal('检测位置与判定规则',`<p>按当前标定视角的位置清单逐个累计确认，包含预装件；每处至少两帧一致证据后才写入本轮历史。</p><p>各位置独立累计，不要求安装先后顺序，也不要求从空孔开始；明确空孔、预装件缺失或结束仍有位置未确认时不会判定通过。</p><p>全部操作位置和预装件确认后显示“检测合规”，实时摄像头检测自动结束并释放采样资源；实时框只来自当前采样；定位失败、推理超时或未检出时撤框，不延用旧位置。配置顺序只用于展示排序。不验证扭矩。</p>`);
    actions.report=()=>state.run&&state.result?window.open(`/api/v1/analysis-runs/${state.run}/artifacts/report.html`,'_blank','noopener'):toast('请先完成视频分析');actions.export=()=>isScene()&&!state.result?SceneDashboard.exportLive(state):isPose()&&!state.result?downloadCSV([['采样时间','人体轨迹','姿态','人体置信','可见关键点'],...state.frames.flatMap(f=>(f.people||[]).map(p=>[f.time,p.trackId,p.label,p.confidence,p.visibleKeypoints]))],'人体检测.csv'):state.result?location.assign(`/api/v1/analysis-runs/${state.run}/artifacts/segments.csv`):downloadCSV([['位置','状态'],...(state.final?.targets||cumulativeTargets()||currentTargets()).map(t=>[t.id,labels[t.state]])],objectName()+'检测.csv');actions['export-alerts']=actions.export;actions['confirm-all']=()=>toast('空孔及证据不足请结合画面复核');
    actions.system=()=>{
      const raw=state.mode==='realtime'?(state.model?.modelName||taskName()+'专用模型'):(state.result?.model.name||state.model?.modelName||taskName()+'专用模型');
      const model=displayModelName(raw);
      const samples=[...state.latencySamples].sort((a,b)=>a-b),percentile=q=>samples.length?Math.round(samples[Math.min(samples.length-1,Math.ceil(samples.length*q)-1)])+' ms':'—';
      const live=state.mode==='realtime'?`<p>模型准备：${state.preparing?'进行中':state.preparationMs==null?'—':Math.round(state.preparationMs)+' ms'}；准备帧不计入本轮工序证据与延迟统计。</p><p>采集至页面更新：当前 ${state.displayLatencyMs==null?'—':Math.round(state.displayLatencyMs)+' ms'}，P95 ${percentile(.95)}，P99 ${percentile(.99)}，最大 ${percentile(1)}；最近 ${samples.length} 次实际采样。</p><p>单帧在途，繁忙时退避重试；当前 ${state.actualFps?.toFixed(1)||'—'} fps，已记录 ${state.evidenceGaps.length} 段超过3秒的采样缺口。浏览器计时不包含相机曝光与驱动延迟。</p>`:'';
      showModal('系统运行详情',`<p>${escapeHTML(model)} · ${state.mode==='realtime'?'实时摄像头':'视频回放'}</p>${live}<p>服务端处理 ${Math.round(state.processing)} ms / 次；当前任务的真实缺陷检出率尚待独立样本验证。</p>`);
    };
    showSystem=actions.system;
    actions.checks=()=>showModal('运行环境自检',`<div class="system-grid"><div class="system-item"><span>${objectName()}专用模型</span><strong>${state.model?.ready?'已就绪':'未就绪'}</strong></div><div class="system-item"><span>画面渲染</span><strong>${ctx?'可用':'不可用'}</strong></div><div class="system-item"><span>摄像头接口</span><strong>${navigator.mediaDevices?.getUserMedia?'可用，开启时授权':'不可用'}</strong></div><div class="system-item"><span>当前视频源</span><strong>${state.running?'摄像头检测中':state.result?'离线结果已加载':'待选择'}</strong></div><div class="system-item"><span>当前判定</span><strong>${isPose()?(state.final?'已结束':'人体姿态观察'):state.final?.compliance?.label||state.timelyDecision?.label||'待确认'}</strong></div></div>`);
    actions['run-checks']=actions.checks;
    get('#server-account').onclick=async()=>{
      if(isPilot())return PilotDashboard.action('account');
      if(!state.session){login();return;}
      const button=get('#server-account');button.disabled=true;
      try{await stopCurrent();await Workspace.logout();tell('已退出登录');}
      catch(error){toast(error.message);}
      finally{button.disabled=false;}
    };
    get('#start-live').onclick=startLive;get('#finish-detection').onclick=()=>{state.finalShown=false;return state.mode==='realtime'?finishLive():finishReplay();};get('#cancel-task').onclick=cancelTask;get('#video-mode').onchange=async()=>{if(get('#video-mode').value==='realtime')await startLive();else{await stopCurrent();state.mode='replay';state.frame=null;render();}};
    document.addEventListener('click',e=>{const seek=e.target.closest('[data-screw-seek]');if(seek&&state.mode==='replay'){state.final=null;video.pause();video.currentTime=Number(seek.dataset.screwSeek);render();}const run=e.target.closest('[data-screw-run]');if(run){closeModal();state.cancelling=false;watch(run.dataset.screwRun).catch(error=>{state.cancelling=false;tell(error.message);});}});
    video.addEventListener('ended',finishReplay);video.addEventListener('play',()=>{if(state.mode==='replay'){state.final=null;state.finalShown=false;state.timelyShown=false;render();}});video.addEventListener('seeked',()=>{render();renderAlerts();});
    video.addEventListener('seeking',()=>{if(state.mode==='replay'){state.final=null;state.finalShown=false;state.timelyShown=false;}});
    video.addEventListener('pause',()=>{render();renderAlerts();});
    video.addEventListener('timeupdate',()=>{if(state.mode==='replay'&&panelFrame()!==state.renderedPanelFrame){render();renderAlerts();}});
    let selectedTask='screw';
    try{const url=new URL(location.href),requested=url.searchParams.get('task')||(url.searchParams.get('scene')?'scene:'+url.searchParams.get('scene'):null),remembered=localStorage.getItem('aisop-inspection-task');if(validTask(requested))selectedTask=requested;else if(validTask(remembered))selectedTask=remembered;}catch{}
    if(window.AISOP_TERMINAL){selectedTask='station'+window.AISOP_TERMINAL.stationId.split('-')[1];for(const option of get('#inspection-task').options)option.disabled=option.value!==selectedTask;}
    for(const name of ['workflow','report','export','export-alerts','system','checks','run-checks']){const original=actions[name];actions[name]=()=>isPilot()?PilotDashboard.action(name):isScene()&&name==='workflow'?SceneDashboard.rules(state):original();}
    if(selectedTask!=='screw'){await Workspace.connect().catch(()=>{});await loadSceneTasks().catch(()=>{});await setTask(selectedTask);return;}
    applyTaskText();
    await Promise.allSettled([loadTaskModel(),Workspace.connect()]);get('.check-chip').textContent=state.model?.ready?'自检 · 模型已就绪':'自检 · 模型待就绪';tell(state.model?.ready?(state.session?'服务已连接 · 可上传视频或开启摄像头':'服务已连接 · 请登录后检测'):taskName()+'模型尚未就绪');render();renderAlerts();
  }
  const api={setTask,draw,render,renderAlerts,upload:file=>isPilot()?PilotDashboard.action('upload'):upload(file),leaveRun:()=>{},get available(){return true;},get state(){return state;},get runtime(){return {user:state.session?.username,live:state.running?{phase:state.preparing?'preparing':state.liveIssue?'reconnecting':currentFrame()?'active':'waiting',data:currentFrame(),frames:state.frames.length,preparationMs:state.preparationMs,displayLatencyMs:state.displayLatencyMs,sampleAgeMs:state.lastSampleAt==null?null:performance.now()-state.lastSampleAt,evidenceGaps:state.evidenceGaps.length}:null};}};
  window.ServerApp=api;init().catch(e=>tell(e.message));
  let lastAnimation=0;
  function animation(at){if(!document.hidden&&at-lastAnimation>=1000/30){lastAnimation=at;const fresh=!!currentFrame();if(panelFrame()!==state.renderedPanelFrame||state.mode==='realtime'&&state.lastDisplayFresh!==fresh){state.lastDisplayFresh=fresh;render();renderAlerts();}draw();}requestAnimationFrame(animation);}requestAnimationFrame(animation);
  document.addEventListener('visibilitychange',()=>{if(state.running&&document.hidden)interruptLive('页面在后台，采样已暂停');else if(state.running){recordGap();render();if(!state.pending){clearTimeout(state.timer);tick(state.generation);}}});
  document.addEventListener('workspace-session',event=>{
    const button=get('#server-account');if(!button)return;
    button.textContent=event.detail?event.detail.username+' · 退出':'登录服务器';
    if(isPilot())return;
    if(!event.detail){
      state.generation++;state.running=false;state.preparing=false;state.liveController?.abort();clearTimeout(state.timer);
      state.upload?.abort();stopTracks();state.streamId=null;state.result=null;state.final=null;state.frame=null;state.frames=[];
      state.timelyDecision=null;state.timelyModalKey=null;video.pause();video.removeAttribute('src');video.load();
      tell('请登录后继续检测');
    }
    render();renderAlerts();
  });
  window.addEventListener('offline',()=>interruptLive('网络已断开，等待恢复'));
  window.addEventListener('online',()=>{if(state.running&&!state.pending){clearTimeout(state.timer);tick(state.generation);}});
  window.addEventListener('beforeunload',()=>{state.running=false;state.liveController?.abort();clearTimeout(state.timer);stopTracks();});
  return api;
})();
