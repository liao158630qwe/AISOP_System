/* Same-origin industrial training console. Dataset text is always escaped. */
(()=>{
  'use strict';
  const W=window.Workspace,$=s=>document.querySelector(s),E=W.escape,form=$('#training-form');
  const labels={QUEUED:'等待执行',RUNNING:'执行中',AUDIT:'数据审计',TRAIN:'训练中',TEST:'独立测试',REGISTER:'候选登记',CANDIDATE:'测试通过 · 待现场验收',REJECTED:'测试未达标',FAILED:'失败',CANCELLING:'正在取消',CANCELLED:'已取消'};
  const taskNames={...W.taskLabels,'feature-classify':'特征分类'};
  const taskHelp={
    detect:'使用水平矩形框标注目标。测试按固定置信阈值逐类计算精确率与召回率，并计入真实负样本的误检。',
    obb:'使用四个角点标注旋转目标，初始权重必须支持 OBB。测试同时检查定位与类别。',
    classify:'每张图片对应一个类别，使用分类初始权重。按 top1 结果评估，不使用检测置信阈值或空标签负样本。',
    'feature-classify':'使用一维 .npy 特征和类别索引训练分类器，无需视觉初始权重。产物需要匹配特征版本的专用推理适配器。'
  };
  const active=r=>['QUEUED','RUNNING','CANCELLING'].includes(r.state);
  const group=r=>r.state==='QUEUED'?'queued':['RUNNING','CANCELLING'].includes(r.state)?'running':r.state==='CANDIDATE'?'candidate':['FAILED','REJECTED'].includes(r.state)?'attention':'cancelled';
  const badge=r=>r.state==='CANDIDATE'?'':group(r)==='attention'?'error':group(r)==='running'?'review':'draft';
  const percent=v=>Number.isFinite(v)?(v*100).toFixed(1)+'%':'—';
  const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'—';
  const value=id=>$('#'+id).value,number=id=>Number(value(id));
  const modelLink=id=>'/admin?model='+encodeURIComponent(id);
  let items=[],candidates=[],timer=null,generation=0,modelGeneration=0,sessionGeneration=0;
  let selected=null,detailVersion='',renderVersion='',confirmResolve=null,submitting=false;
  let offset=0,total=0,counts={},hasMore=false,failures=0,listController=null,searchTimer=null;
  let detailRun=null,detailGeneration=0,detailController=null;
  const pageSize=25;
  let requestBody=null,requestKey=null,submittedBody=null,extraRecipe=null;
  let linkedModel=new URLSearchParams(location.search).get('model');
  let linkedDataset=new URLSearchParams(location.search).get('dataset');
  const controls=[...form.querySelectorAll('input,select,button')];
  const snapshot=()=>JSON.stringify(controls.filter(el=>el.matches('input,select')).map(el=>[el.id,el.value]));
  let cleanSnapshot=snapshot();
  const error=text=>{$('#training-error').textContent=text||'';$('#training-error').hidden=!text;};
  const status=(text='',bad=false)=>{$('#training-submit-status').textContent=text;$('#training-submit-status').dataset.error=String(bad);};
  function connection(state,text){$('#training-connection').dataset.status=state;$('#training-connection span').textContent=text;}
  function confirmAction(title,description,label){
    if(confirmResolve)return Promise.resolve(false);
    $('#confirm-title').textContent=title;$('#confirm-description').textContent=description;$('#confirm-accept').textContent=label;
    $('#training-confirm').showModal();$('#confirm-dismiss').focus();
    return new Promise(resolve=>confirmResolve=resolve);
  }
  $('#confirm-accept').onclick=()=>{confirmResolve?.(true);confirmResolve=null;$('#training-confirm').close();};
  for(const id of ['confirm-close','confirm-dismiss'])$('#'+id).onclick=()=>$('#training-confirm').close();
  $('#training-confirm').onclose=()=>{confirmResolve?.(false);confirmResolve=null;};
  function recipeBody(){
    const task=value('training-task'),feature=task==='feature-classify',detection=['detect','obb'].includes(task);
    const recipe={backend:feature?'torch':'ultralytics',task,dataset:value('training-dataset').trim(),seed:number('training-seed')};
    const extras=extraRecipe?.task===task?extraRecipe:null;
    if(feature){recipe.feature_schema=value('training-feature-schema').trim();if(extras?.train)recipe.train={...extras.train};}
    else{
      recipe.pretrained=value('training-pretrained');
      recipe.train={...extras?.train,epochs:number('training-epochs'),imgsz:number('training-imgsz'),batch:number('training-batch'),workers:0};
      if(detection)recipe.evaluation={confidence:number('training-confidence')};
    }
    const policy={min_batches_per_split:number('policy-batches'),min_class_samples:number('policy-classes'),min_precision:number('policy-precision'),min_recall:number('policy-recall')};
    if(detection)policy.min_negative_samples=number('policy-negatives');
    return {name:value('training-name').trim(),station:value('training-station'),recipe,policy};
  }
  function validateText(){
    for(const id of ['training-name','training-dataset','training-feature-schema']){
      const input=$('#'+id);
      input.setCustomValidity(!input.disabled&&input.required&&!input.value.trim()?'请填写此项，不能仅包含空格。':'');
    }
  }
  function updateSummary(){
    const task=value('training-task'),feature=task==='feature-classify',detection=['detect','obb'].includes(task);
    validateText();
    const model=candidates.find(m=>m.path===value('training-pretrained'));
    $('#training-pretrained').setCustomValidity(!feature&&model?.task&&model.task!==task?'所选模型为'+(taskNames[model.task]||model.task)+'，请与训练类型保持一致。':'');
    const pairs=[['采集工位',$('#training-station').selectedOptions[0]?.textContent],['训练类型',taskNames[task]],
      ['初始模型',feature?'无需视觉权重':value('training-pretrained')||'待选择'],
      ['训练配置',feature?'CUDA GPU · 特征分类':value('training-epochs')+' 轮 · '+value('training-imgsz')+' px · batch '+value('training-batch')],
      ['测试门槛',percent(number('policy-precision'))+' / '+percent(number('policy-recall'))],
      ['评估方式',detection?'固定置信阈值 '+value('training-confidence'):'逐类分类结果']];
    $('#training-summary').innerHTML=pairs.map(([k,v])=>'<dt>'+E(k)+'</dt><dd>'+E(v)+'</dd>').join('');
    $('#policy-preview').textContent=percent(number('policy-precision'))+' / '+percent(number('policy-recall'));
    const invalid=controls.find(el=>el.willValidate&&!el.validity.valid);
    const readiness=$('#training-readiness');
    readiness.dataset.ready=String(!invalid);
    readiness.textContent=invalid?'待完善：'+(invalid.closest('label')?.childNodes[0].textContent.trim()||'训练配置'):submittedBody===JSON.stringify(recipeBody())?'任务已提交，请在训练记录中查看进度。':'配置已填写，提交后开始数据审计。';
    $('#training-submit').disabled=submitting||!W.session||submittedBody===JSON.stringify(recipeBody());
    $('#training-submit').textContent=submitting?'正在提交…':submittedBody===JSON.stringify(recipeBody())?'此配置已提交':'提交并开始审计';
  }
  function taskFields(){
    const task=value('training-task'),feature=task==='feature-classify',detection=['detect','obb'].includes(task);
    for(const [id,show] of [['feature-field',feature],['weights-field',!feature],['visual-parameters',!feature],['confidence-field',detection],['negatives-field',detection]])$('#'+id).hidden=!show;
    $('#training-pretrained').required=!feature;$('#training-feature-schema').required=feature;
    for(const el of controls)el.disabled=submitting||!W.session||!!el.closest('#feature-field[hidden],#weights-field[hidden],#visual-parameters[hidden],#confidence-field[hidden],#negatives-field[hidden]');
    $('#training-task-help').textContent=taskHelp[task];
    $('#weights-help').textContent='请选择'+taskNames[task]+'对应的可信服务器 .pt 权重；实际类型由训练工作器核验。';
    updateSummary();
  }
  function render(){
    const filter=value('training-filter-status'),shown=items,all=Object.values(counts).reduce((a,b)=>a+b,0);
    $('#training-count').textContent=total?'第 '+(offset+1)+'–'+(offset+shown.length)+' 项 / '+total+' 项':'0 项';
    $('#training-empty').hidden=all>0;$('#training-no-results').hidden=!all||shown.length>0;
    $('#training-previous').disabled=offset===0;$('#training-next').disabled=!hasMore;
    $('#training-page').textContent='第 '+(Math.floor(offset/pageSize)+1)+' 页';
    $('#training-pagination').hidden=total<=pageSize&&offset===0;
    for(const kind of ['queued','running','candidate','attention']){
      $('#count-'+kind).textContent=counts[kind]||0;
      $('[data-overview="'+kind+'"]').setAttribute('aria-pressed',String(filter===kind));
    }
    const markup=shown.map(r=>'<article class="training-run" data-run="'+E(r.id)+'"><div><h3>'+E(r.name)+'</h3><small>工位 '+E(r.station.slice(-1))+' · '+E(taskNames[r.task]||r.task)+'</small><p>'+date(r.created_at)+'<br><span class="run-id">'+E(r.id)+'</span></p></div><div><span class="badge '+badge(r)+'">'+E(labels[r.state]||r.state)+'</span> <small>'+(r.state==='RUNNING'?E(labels[r.stage]||r.stage):'')+'</small>'+(active(r)?'<progress max="1" value="'+Math.max(0,Math.min(1,Number(r.progress)||0))+'" aria-label="当前阶段进度"></progress>':'')+'<p>'+E(r.message)+'</p></div><div class="row-actions"><button class="small" data-detail="'+E(r.id)+'">详情</button>'+(!active(r)?'<button class="small quiet" data-copy="'+E(r.id)+'">'+(group(r)==='attention'?'调整后重试':'复用配方')+'</button>':'')+(active(r)&&r.state!=='CANCELLING'?'<button class="small quiet danger" data-cancel="'+E(r.id)+'">取消训练</button>':'')+(r.model_id?'<a class="button small" href="'+E(modelLink(r.model_id))+'">查看候选模型 ↗</a>':'')+'</div></article>').join('');
    if(markup!==renderVersion){
      const focused=document.activeElement,run=focused?.closest('[data-run]')?.dataset.run,action=focused?.hasAttribute('data-detail')?'detail':focused?.hasAttribute('data-cancel')?'cancel':'copy';
      $('#training-runs').innerHTML=markup;renderVersion=markup;
      if(run){const target=[...$('#training-runs').querySelectorAll('[data-'+action+']')].find(el=>el.dataset[action]===run);target?.focus({preventScroll:true});}
    }
  }
  function detail(r){
    if(!r)return;
    const serialized=JSON.stringify(r);
    if(selected===r.id&&detailVersion===serialized)return;
    selected=r.id;detailVersion=serialized;$('#training-detail-title').textContent=r.name;
    const logOpen=$('#training-log')?.open,logText=$('#training-log-text')?.textContent,recipeOpen=$('#training-recipe')?.open;
    const ev=r.evaluation,gate=ev?.gate,audit=r.audit,rows=ev?.per_class||ev?.metrics?.per_class||[],detection=['detect','obb'].includes(r.recipe.task);
    let guidance='';
    if(r.state==='FAILED')guidance='请先查看失败原因及工作器日志，核对清单路径、集合隔离和初始权重类型，再复用配方创建新任务。';
    if(r.state==='REJECTED')guidance='请检查未达标类别及样本覆盖，再调整数据或训练配置。根据测试结果反复调参后，需要新的独立留出集。';
    if(r.state==='CANDIDATE')guidance=r.model_id?'候选模型已停用登记。完成现场验收后，在模型库启用并配置场景。':'特征分类产物需要专用推理适配器，并与训练时的特征定义一致。';
    if(r.state==='CANCELLED')guidance='任务已取消。可复用配方重新创建；新任务会重新进行数据审计。';
    $('#training-detail-body').innerHTML='<div class="training-detail-summary"><span class="badge '+badge(r)+'">'+E(labels[r.state]||r.state)+'</span><p>'+E(r.message)+'</p><p class="hint">任务 '+E(r.id)+'<br>工位 '+E(r.station.slice(-1))+' · '+E(taskNames[r.recipe.task])+'<br>创建 '+date(r.created_at)+' · 更新 '+date(r.updated_at)+'</p>'+(guidance?'<p class="notice">'+E(guidance)+'</p>':'')+
      (audit?'<h3>数据审计</h3><div class="table-scroll"><table><thead><tr><th>集合</th><th>样本</th><th>独立批次</th>'+(detection?'<th>真实负样本</th>':'')+'</tr></thead><tbody>'+['train','val','test'].map(s=>'<tr><td>'+E({train:'训练',val:'验证',test:'测试'}[s])+'</td><td>'+E(audit.counts?.[s]??'—')+'</td><td>'+E(audit.batches?.[s]??'—')+'</td>'+(detection?'<td>'+E(audit.negatives?.[s]??0)+'</td>':'')+'</tr>').join('')+'</tbody></table></div><p class="hint">隔离字段：'+E((audit.isolation_keys||[]).join('、'))+'<br>快照 SHA256：'+E(audit.sha256)+'</p>':'')+
      (ev?'<h3>独立测试与准入</h3><p>'+(gate?.passed?'模型测试门槛通过。现场工件误放行率、漏检率和端到端时延仍需验收。':'未通过，不能登记为可用候选。')+'</p>'+(gate?.failures?.length?'<ul>'+gate.failures.map(x=>'<li>'+E(x)+'</li>').join('')+'</ul>':'')+'<div class="table-scroll"><table><thead><tr><th>类别</th><th>精确率</th><th>召回率</th><th>支持数</th></tr></thead><tbody>'+rows.map(x=>'<tr><td>'+E(x.name)+'</td><td>'+percent(x.precision)+'</td><td>'+percent(x.recall)+'</td><td>'+E(x.support)+'</td></tr>').join('')+'</tbody></table></div><p class="hint">'+E(gate?.scope||'')+(ev.operating_point?'<br>评估点：'+E(JSON.stringify(ev.operating_point)):'')+'</p>':'')+
      (r.card?'<h3>模型产物</h3><p class="hint">'+E(r.card.model)+'<br>SHA256：'+E(r.card.model_sha256)+'</p>':'')+
      '<div class="row-actions">'+(!active(r)?'<button data-copy="'+E(r.id)+'">复用配方创建新任务</button>':'')+(r.model_id?'<a class="button primary" href="'+E(modelLink(r.model_id))+'">查看候选模型 ↗</a>':'')+'<a class="button small" href="/api/v1/admin/training-runs/'+encodeURIComponent(r.id)+'/report">下载训练记录 JSON</a></div><details id="training-log"><summary>工作器日志（最近64KB）</summary><pre id="training-log-text">展开后读取</pre></details><details id="training-recipe"><summary>冻结配方与准入门槛</summary><pre>'+E(JSON.stringify({recipe:r.recipe,policy:r.policy},null,2))+'</pre></details></div>';
    const log=$('#training-log');
    if(logOpen){log.open=true;$('#training-log-text').textContent=logText||'';}
    $('#training-recipe').open=!!recipeOpen;
    log.ontoggle=async()=>{
      if(!log.open)return;const epoch=sessionGeneration,node=$('#training-log-text');
      try{const result=await W.request('/admin/training-runs/'+encodeURIComponent(r.id)+'/log');if(epoch===sessionGeneration&&selected===r.id&&node.isConnected)node.textContent=result.text||'暂无工作器日志。';}
      catch(e){if(epoch===sessionGeneration&&node.isConnected)node.textContent=e.message;}
    };
  }
  function clearFilters(){
    $('#training-search').value='';$('#training-filter-station').value='all';$('#training-filter-status').value='all';offset=0;
  }
  async function loadDetail(id){
    if(detailController&&!detailController.signal.aborted)return;
    const controller=new AbortController();detailController=controller;
    const current=++detailGeneration,epoch=sessionGeneration;
    try{
      const result=await W.request('/admin/training-runs/'+encodeURIComponent(id),{signal:controller.signal});
      if(current!==detailGeneration||epoch!==sessionGeneration||selected!==id||!$('#training-detail').open)return;
      detailRun=result;detail(result);
    }catch(e){if(!controller.signal.aborted&&current===detailGeneration&&selected===id)error('详情读取失败：'+e.message);}
    finally{if(current===detailGeneration)detailController=null;}
  }
  async function refresh(loadModels=false){
    clearTimeout(timer);clearTimeout(searchTimer);listController?.abort();const current=++generation;
    $('#training-locked').hidden=!!W.session;$('#training-content').hidden=!W.session;
    if(!W.session){connection('idle','等待登录');return;}
    if(loadModels)void loadCandidates();
    const controller=new AbortController();listController=controller;
    $('#refresh-training').disabled=true;connection('syncing','正在同步');
    try{
      const params=new URLSearchParams({limit:pageSize,offset,search:value('training-search').trim(),station:value('training-filter-station'),status:value('training-filter-status')});
      const result=await W.request('/admin/training-runs?'+params,{signal:controller.signal});
      if(current!==generation||!W.session)return;
      failures=0;items=result.items;total=result.total;counts=result.counts;hasMore=result.has_more;
      if(offset&&offset>=total){offset=Math.max(0,Math.ceil(total/pageSize)-1)*pageSize;return refresh();}
      render();error();connection('online','服务已连接');
      $('#training-sync').textContent='已同步 '+new Date().toLocaleTimeString('zh-CN',{hour12:false})+' · CUDA GPU 单任务队列';
      const summary=items.find(item=>item.id===selected);
      if(selected&&$('#training-detail').open&&(!detailRun||(active(detailRun)&&(!summary||summary.updated_at!==detailRun.updated_at||summary.state!==detailRun.state))))void loadDetail(selected);
    }catch(e){
      if(!controller.signal.aborted&&current===generation&&W.session){failures++;error('同步失败：'+e.message);connection('error','同步中断');$('#training-sync').textContent='显示上次记录 · 稍后自动重试';}
    }finally{
      if(current===generation){$('#refresh-training').disabled=false;if(W.session&&!document.hidden)timer=setTimeout(()=>refresh(),failures?Math.min(60000,3000*2**Math.min(failures,5)):(counts.queued||counts.running?3000:15000));}
    }
  }
  async function loadCandidates(){
    const current=++modelGeneration,epoch=sessionGeneration,initialSnapshot=snapshot();
    try{
      const result=await W.request('/admin/model-candidates');
      if(current!==modelGeneration||epoch!==sessionGeneration||!W.session)return;
      const wasClean=initialSnapshot===cleanSnapshot&&snapshot()===initialSnapshot;
      candidates=result.items.filter(m=>m.source!=='registered'||['detect','obb','classify'].includes(m.task));
      const select=$('#training-pretrained'),prior=select.value;
      select.replaceChildren(new Option(candidates.length?'请选择服务器可信权重':'暂无服务器权重，请先放入 models 目录',''));
      for(const m of candidates)select.add(new Option(m.source==='registered'?m.name+' · v'+m.version+' · '+(taskNames[m.task]||m.task)+'（模型库）':m.path,m.path));
      if(prior&&!candidates.some(m=>m.path===prior))select.add(new Option(prior+'（历史路径，提交时核验）',prior));
      select.value=prior;
      if(linkedModel){
        const model=candidates.find(m=>m.model_id===linkedModel);linkedModel=null;
        if(model&&['detect','obb','classify'].includes(model.task)){
          $('#training-task').value=model.task;select.value=model.path;taskFields();
          $('#draft-notice').textContent='已选择服务器模型「'+model.name+'」作为初始权重，请准备对应工位的数据清单。';$('#draft-notice').hidden=false;
        }else error('所选模型不存在、不可读取或暂不支持后台训练，请重新选择。');
      }
      updateSummary();if(wasClean)cleanSnapshot=snapshot();
    }catch(e){if(epoch===sessionGeneration&&W.session){$('#weights-help').textContent='权重列表读取失败：'+e.message+'。点击页面刷新重试。';}}
  }
  async function copyRun(r){
    if(!r||submitting)return;
    const epoch=sessionGeneration;
    if(snapshot()!==cleanSnapshot&&!await confirmAction('替换当前表单？','当前表单已有编辑。继续后将载入「'+r.name+'」的配置。','载入配方'))return;
    if(epoch!==sessionGeneration||!W.session)return;
    form.reset();extraRecipe=JSON.parse(JSON.stringify(r.recipe));
    const fields={'training-name':(r.name+' · 复训').slice(0,100),'training-station':r.station,'training-task':r.recipe.task,'training-dataset':r.recipe._dataset_ref||r.recipe.dataset,'training-seed':r.recipe.seed??42,'training-feature-schema':r.recipe.feature_schema||'',
      'training-epochs':r.recipe.train?.epochs??100,'training-imgsz':r.recipe.train?.imgsz??640,'training-batch':r.recipe.train?.batch??8,'training-confidence':r.recipe.evaluation?.confidence??.6,
      'policy-batches':r.policy.min_batches_per_split,'policy-classes':r.policy.min_class_samples,'policy-negatives':r.policy.min_negative_samples,'policy-precision':r.policy.min_precision,'policy-recall':r.policy.min_recall};
    for(const [id,v] of Object.entries(fields)){
      const el=$('#'+id);
      if(el.tagName==='SELECT'&&![...el.options].some(o=>o.value===String(v)))el.add(new Option(String(v)+'（历史配置）',String(v)));
      el.value=v;
    }
    if(r.recipe.pretrained){
      const select=$('#training-pretrained');
      if(![...select.options].some(o=>o.value===r.recipe.pretrained))select.add(new Option(r.recipe.pretrained+'（历史路径）',r.recipe.pretrained));
      select.value=r.recipe.pretrained;
    }
    requestBody=requestKey=submittedBody=null;
    $('#draft-notice').textContent='已载入「'+r.name+'」的配方，并保留其他训练参数。请核对数据路径、初始权重及测试集，确认后提交为新任务。';
    $('#draft-notice').hidden=false;status();taskFields();
    $('#training-detail').close();form.scrollIntoView({block:'start',behavior:'instant'});$('#training-name').focus({preventScroll:true});
  }
  async function cancelRun(r){
    if(!r||!active(r)||r.state==='CANCELLING')return;
    const epoch=sessionGeneration;
    if(!await confirmAction('取消这项训练？','「'+r.name+'」将停止执行，已产生的审计记录会保留。重新训练需要创建新任务。','确认取消'))return;
    if(epoch!==sessionGeneration||!W.session)return;
    try{await W.json('/admin/training-runs/'+encodeURIComponent(r.id)+'/cancel','POST',{});if(epoch===sessionGeneration){W.toast('已发送取消请求');await refresh();}}
    catch(e){if(epoch===sessionGeneration)error('取消失败：'+e.message);}
  }
  form.addEventListener('input',updateSummary);
  form.addEventListener('change',()=>{taskFields();});
  form.onsubmit=async event=>{
    event.preventDefault();if(!W.session||submitting)return;
    validateText();
    const invalid=controls.find(el=>el.willValidate&&!el.validity.valid);
    if(invalid){invalid.closest('details')?.setAttribute('open','');invalid.reportValidity();status('请先检查并修正表单中的必填项或参数范围。',true);return;}
    const serialized=JSON.stringify(recipeBody()),epoch=sessionGeneration;
    if(serialized===submittedBody)return;
    if(serialized!==requestBody){requestKey=crypto.randomUUID();requestBody=serialized;}
    submitting=true;taskFields();form.setAttribute('aria-busy','true');status('正在创建任务，配置将冻结…');
    try{
      const r=await W.request('/admin/training-runs',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':requestKey},body:serialized});
      if(epoch!==sessionGeneration||!W.session)return;
      submittedBody=serialized;cleanSnapshot=snapshot();status('已入队：'+r.name+'。可在训练记录中查看进度。');$('#draft-notice').hidden=true;
      clearFilters();await refresh();W.toast('训练任务已提交');
    }catch(e){if(epoch===sessionGeneration&&W.session)status('提交失败：'+e.message+'。重试相同配置不会重复创建任务。',true);}
    finally{if(epoch===sessionGeneration){submitting=false;form.setAttribute('aria-busy','false');taskFields();}}
  };
  $('#training-reset').onclick=async()=>{
    if(submitting)return;const epoch=sessionGeneration;
    if(snapshot()!==cleanSnapshot&&!await confirmAction('重置训练配置？','表单中的编辑内容将被清除，恢复默认训练参数。','重置表单'))return;
    if(epoch!==sessionGeneration||!W.session)return;
    form.reset();extraRecipe=null;requestBody=requestKey=submittedBody=null;$('#draft-notice').hidden=true;status();taskFields();cleanSnapshot=snapshot();
  };
  async function runAction(event){
    const button=event.target.closest('button');if(!button)return;
    const id=button.dataset.detail||button.dataset.copy||button.dataset.cancel,r=items.find(r=>r.id===id)||(detailRun?.id===id?detailRun:null);
    if(!r)return;
    if(button.dataset.detail){selected=id;detailRun=null;detailVersion='';$('#training-detail-title').textContent=r.name;$('#training-detail-body').textContent='正在读取详情…';$('#training-detail').showModal();await loadDetail(id);}
    if(button.dataset.copy){
      const epoch=sessionGeneration;button.disabled=true;
      try{const full=detailRun?.id===id?detailRun:await W.request('/admin/training-runs/'+encodeURIComponent(id));if(epoch===sessionGeneration&&W.session)await copyRun(full);}
      catch(e){if(epoch===sessionGeneration)error('配方读取失败：'+e.message);}finally{button.disabled=false;}
    }
    if(button.dataset.cancel)await cancelRun(r);
  }
  $('#training-runs').onclick=runAction;$('#training-detail-body').onclick=runAction;
  $('#training-close').onclick=()=>$('#training-detail').close();
  $('#training-detail').onclose=()=>{selected=null;detailRun=null;detailVersion='';++detailGeneration;detailController?.abort();};
  const filterChanged=()=>{offset=0;void refresh();};
  $('#training-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(filterChanged,300);};
  for(const id of ['training-filter-station','training-filter-status'])$('#'+id).onchange=filterChanged;
  $('#training-clear-filters').onclick=()=>{clearFilters();void refresh();};
  $('#training-previous').onclick=()=>{offset=Math.max(0,offset-pageSize);void refresh();};
  $('#training-next').onclick=()=>{if(hasMore){offset+=pageSize;void refresh();}};
  for(const button of document.querySelectorAll('[data-overview]'))button.onclick=()=>{
    $('#training-filter-status').value=value('training-filter-status')===button.dataset.overview?'all':button.dataset.overview;
    filterChanged();$('#training-history').scrollIntoView({behavior:'instant',block:'start'});
  };
  $('#refresh-training').onclick=()=>refresh(true);$('#training-login').onclick=()=>W.login();
  $('#account-button').onclick=async()=>{try{if(W.session)await W.logout();else await W.login();}catch(e){error(e.message);}};
  async function loadDataset(){
    if(!linkedDataset||!W.session)return;
    const id=linkedDataset,epoch=sessionGeneration,initial=snapshot();
    try{
      const dataset=await W.request('/admin/dataset-exports/'+encodeURIComponent(id));
      if(epoch!==sessionGeneration||!W.session)return;
      if(snapshot()!==initial&&!await confirmAction('载入视频训练数据？','当前表单已有编辑。继续将载入数据集名称、工位、类型与审核要求。','载入数据版本'))return;
      if(epoch!==sessionGeneration||!W.session)return;
      linkedDataset=null;
      for(const [key,v] of Object.entries({'training-name':(dataset.name+' · 训练').slice(0,100),'training-station':dataset.station,'training-task':dataset.task,'training-dataset':dataset.reference,
        'policy-batches':dataset.policy.min_batches_per_split,'policy-classes':dataset.policy.min_class_samples,'policy-negatives':dataset.policy.min_negative_samples,'policy-precision':dataset.policy.min_precision,'policy-recall':dataset.policy.min_recall}))$('#'+key).value=v;
      $('#draft-notice').textContent='已载入审核通过的视频数据版本（修订 '+dataset.revision+'）。请选择匹配的初始模型并核对训练参数后提交。';$('#draft-notice').hidden=false;
      taskFields();cleanSnapshot=snapshot();
    }catch(e){if(epoch===sessionGeneration)error('读取数据版本失败：'+e.message);}
  }
  document.addEventListener('workspace-session',()=>{
    ++sessionGeneration;++modelGeneration;++generation;clearTimeout(timer);clearTimeout(searchTimer);listController?.abort();
    offset=total=failures=0;counts={};hasMore=false;
    submitting=false;items=[];candidates=[];selected=null;renderVersion=detailVersion='';
    requestBody=requestKey=submittedBody=extraRecipe=null;
    form.reset();form.setAttribute('aria-busy','false');$('#draft-notice').hidden=true;status();error();
    $('#training-pretrained').replaceChildren(new Option(W.session?'正在读取服务器权重…':'请登录后读取服务器权重',''));
    $('#training-runs').replaceChildren();$('#training-detail').close();$('#training-confirm').close();$('#training-detail-body').replaceChildren();
    $('#refresh-training').disabled=false;taskFields();cleanSnapshot=snapshot();clearFilters();void refresh(true);void loadDataset();
  });
  document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(document.hidden){listController?.abort();detailController?.abort();}else if(W.session)void refresh();});
  window.addEventListener('pagehide',()=>{clearTimeout(timer);clearTimeout(searchTimer);listController?.abort();detailController?.abort();});
  taskFields();cleanSnapshot=snapshot();
  W.connect().catch(e=>{connection('error','连接失败');error('连接服务失败：'+e.message);});
})();
