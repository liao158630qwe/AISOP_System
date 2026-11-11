/* Pure catalog and rule checks are shared by the editor and its regression tests. */
const AdminCatalog = (() => {
  const normalized = value => String(value ?? '').toLocaleLowerCase().trim();
  const attention = scene => scene.status === 'published' && !scene.ready;
  function scenes(items, query = '', status = 'all', sort = 'updated') {
    const text = normalized(query);
    return items.filter(scene => (status === 'all' || (status === 'attention' ? attention(scene) : scene.status === status)) &&
      normalized([scene.name, scene.description, scene.model?.name, scene.model?.version, ...(scene.rules || []).map(rule => rule.name)].join(' ')).includes(text))
      .sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name, 'zh-CN') :
        sort === 'rules' ? b.rules.length - a.rules.length || b.updated_at - a.updated_at : b.updated_at - a.updated_at);
  }
  function models(items, query = '', status = 'all', task = 'all') {
    const text = normalized(query);
    return items.filter(model => (status === 'all' || model.enabled === (status === 'enabled')) &&
      (task === 'all' || model.task === task) && normalized([model.id, model.name, model.version, ...Object.values(model.classes)].join(' ')).includes(text));
  }
  function validate(body, model) {
    const issues = [];
    if (!body.name.trim()) issues.push('请填写任务名称。');
    if (!model) issues.push('请选择有效的关联模型。');
    if (body.status === 'published' && !model?.enabled) issues.push('当前模型已停用，请先启用模型，或保存为草稿。');
    if (body.status === 'published' && !body.rules.some(rule => rule.kind === 'required')) issues.push('发布前至少添加一项“必须检出”规则。');
    if (body.rules.length > 32) issues.push('每个任务最多配置 32 项规则。');
    const ids = new Set();
    body.rules.forEach((rule, index) => {
      const label = `规则 ${index + 1}${rule.name ? '「' + rule.name + '」' : ''}`;
      if (!rule.name.trim()) issues.push(`${label}：请填写规则名称。`);
      if (ids.has(rule.id)) issues.push(`${label}：规则编号重复，请复制或重新添加此规则。`);
      ids.add(rule.id);
      if (!Object.hasOwn(model?.classes || {}, String(rule.class_id))) issues.push(`${label}：类别不属于当前模型，请重新选择。`);
      if (!Number.isFinite(rule.threshold) || rule.threshold < .05 || rule.threshold > .99) issues.push(`${label}：置信阈值需在 0.05–0.99 之间。`);
      if (!Number.isInteger(rule.min_count) || rule.min_count < 1 || rule.min_count > 300 ||
          (rule.max_count !== null && (!Number.isInteger(rule.max_count) || rule.max_count < rule.min_count || rule.max_count > 300))) {
        issues.push(`${label}：数量需为 1–300 的整数，最多数量不能小于至少数量。`);
      }
      if (model?.task === 'classify' && (rule.min_count !== 1 || (rule.max_count !== null && rule.max_count !== 1))) issues.push(`${label}：分类模型每个区域的数量只能设为 1。`);
      const [x1, y1, x2, y2] = rule.roi;
      if (![x1, y1, x2, y2].every(Number.isFinite) || !(0 <= x1 && x1 < x2 && x2 <= 1 && 0 <= y1 && y1 < y2 && y2 <= 1)) {
        issues.push(`${label}：检测区域必须有宽度和高度，坐标需在 0–1 之间。`);
      }
    });
    if (body.status === 'published') {
      body.rules.forEach((left, i) => body.rules.slice(i + 1).forEach(right => {
        if (left.roi.some((v, index) => v !== right.roi[index])) return;
        const label = `「${left.name}」与「${right.name}」`;
        if (left.kind === 'required' && right.kind === 'required') {
          if (model?.task === 'classify' && left.class_id !== right.class_id) issues.push(label + '要求同一区域同时属于不同类别，分类模型无法满足。');
          else if (left.class_id === right.class_id && left.threshold === right.threshold && Math.max(left.min_count, right.min_count) > Math.min(left.max_count ?? 300, right.max_count ?? 300)) issues.push(label + '在相同区域和阈值下的数量范围互相矛盾。');
        } else if (left.kind !== right.kind && left.class_id === right.class_id) {
          const required = left.kind === 'required' ? left : right, forbidden = left.kind === 'forbidden' ? left : right;
          if (required.threshold >= forbidden.threshold) issues.push(label + '在同一区域要求检出且禁止同一目标，无法同时通过。');
        }
      }));
    }
    return issues;
  }
  return {scenes, models, attention, validate};
})();
if (typeof module !== 'undefined' && module.exports) module.exports = AdminCatalog;
/* Persistent model library and visual scene-rule editor. */
if (typeof window !== 'undefined') (()=>{
  'use strict';
  const W=window.Workspace,$=selector=>document.querySelector(selector),E=W.escape;
  const state={models:[],scenes:[],editing:null,tab:'tasks',rules:[],activeROI:null,image:null,imageURL:null,loading:0,filter:'all',modelId:null,snapshot:null,saving:false,importing:false,confirm:null,importGeneration:0};
  let linkedModel=new URLSearchParams(location.search).get('model');
  let refreshController=null,taskPage=0,modelPage=0,sessionEpoch=0,searchTimer=null,lastSync=0;
  let sceneRequestKey=null,sceneRequestBody=null;
  let modelIndex=new Map(),linkedScenes=new Map();
  const pageSize=20;
  function pageItems(items,kind){
    const last=Math.max(0,Math.ceil(items.length/pageSize)-1);
    if(kind==='task')taskPage=Math.min(taskPage,last);else modelPage=Math.min(modelPage,last);
    const page=kind==='task'?taskPage:modelPage;
    $('#'+kind+'-pagination').hidden=items.length<=pageSize;
    $('#'+kind+'-previous').disabled=page===0;$('#'+kind+'-next').disabled=page>=last;
    $('#'+kind+'-page').textContent=`第 ${page+1} / ${last+1} 页`;
    return items.slice(page*pageSize,(page+1)*pageSize);
  }
  const size=bytes=>bytes>=1024*1024?(bytes/1024/1024).toFixed(1)+' MB':bytes>=1024?(bytes/1024).toFixed(1)+' KB':bytes+' B';
  const message=(text='')=>{$('#page-message').textContent=text;$('#page-message').hidden=!text;};
  const modelById=id=>modelIndex.get(id);
  function tab(name){
    state.tab=name;
    for(const value of ['tasks','models']){
      $('#'+value+'-panel').hidden=value!==name;
      $('#'+value+'-tab').setAttribute('aria-selected',String(value===name));
      $('#'+value+'-tab').tabIndex=value===name?0:-1;
    }
  }
  const date=value=>new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  const classTags=model=>Object.values(model.classes).slice(0,4).map(name=>`<span class="tag">${E(name)}</span>`).join('')+(Object.keys(model.classes).length>4?`<span class="tag">+${Object.keys(model.classes).length-4}</span>`:'');
  function filterTasks(value){state.filter=value;taskPage=0;tab('tasks');render();}
  function renderModels(){
    const shown=AdminCatalog.models(state.models,$('#model-search').value,$('#model-status').value,$('#model-type').value);
    $('#model-total').textContent=state.models.length?`${shown.length} / ${state.models.length}`:'';
    $('#model-empty').hidden=state.models.length>0;
    $('#model-table').hidden=!shown.length;
    $('#model-no-results').hidden=!state.models.length||shown.length>0;
    $('#model-rows').innerHTML=pageItems(shown,'model').map(model=>{
      const linked=(linkedScenes.get(model.id)||[]).length;
      return `<tr><td><div class="model-title"><span class="model-avatar" aria-hidden="true">◇</span><div><strong>${E(model.name)}</strong><small>v${E(model.version)}</small></div></div></td><td>${E(W.taskLabels[model.task]||model.task)}</td><td class="model-classes">${classTags(model)}</td><td>${size(model.size)}<small>${linked} 个关联任务</small></td><td><span class="badge ${model.enabled?'':'draft'}">${model.enabled?'已启用':'已停用'}</span></td><td><div class="row-actions"><button class="small" data-model-details="${E(model.id)}" aria-label="查看 ${E(model.name)} 的信息">信息</button><button class="small" data-model-use="${E(model.id)}" ${model.enabled?'':'disabled'}>创建任务</button><button class="small quiet ${model.enabled?'danger':''}" data-model-toggle="${E(model.id)}">${model.enabled?'停用':'启用'}</button></div></td></tr>`;
    }).join('');
  }
  function render(includeModels=true){
    const enabled=state.models.filter(model=>model.enabled).length;
    const published=state.scenes.filter(scene=>scene.status==='published').length;
    const drafts=state.scenes.filter(scene=>scene.status==='draft').length;
    const unavailable=state.scenes.filter(AdminCatalog.attention).length;
    $('#model-count').textContent=state.models.length;
    $('#enabled-model-count').textContent=`${enabled} 个已启用`;
    $('#published-count').textContent=published;
    $('#draft-count').textContent=drafts;
    $('#attention-count').textContent=unavailable;
    $('.attention-stat').classList.toggle('has-attention',unavailable>0);
    $('#new-scene-button').disabled=!enabled||!W.session;
    $('#new-scene-button').title=enabled?'创建场景并配置检测规则':'请先导入并启用一个模型';
    $('#heading-import').disabled=!W.session;
    $('#task-tab-count').textContent=state.scenes.length;
    $('#model-tab-count').textContent=state.models.length;
    for(const [status,count] of Object.entries({all:state.scenes.length,published,draft:drafts,attention:unavailable})){
      $('#filter-'+status+'-count').textContent=count;
      $(`[data-task-filter="${status}"]`).setAttribute('aria-pressed',String(state.filter===status));
    }
    const shown=AdminCatalog.scenes(state.scenes,$('#task-search').value,state.filter,$('#task-sort').value);
    $('#task-total').textContent=state.scenes.length?`${shown.length} / ${state.scenes.length}`:'';
    $('#scene-empty').hidden=state.scenes.length>0;
    $('#task-no-results').hidden=!state.scenes.length||shown.length>0;
    $('#scene-cards').innerHTML=pageItems(shown,'task').map(scene=>{
      const attention=AdminCatalog.attention(scene),draft=scene.status==='draft';
      return `<article class="task-card ${attention?'needs-attention':''}">
        <div class="card-top"><span class="eyebrow" style="margin:0">${E(W.taskLabels[scene.model.task]||scene.model.task)}</span><span class="badge ${draft?'draft':attention?'error':''}">${draft?'草稿':attention?'需要关注':'已发布'}</span></div>
        <h3>${E(scene.name)}</h3><p class="task-description" title="${E(scene.description||'')}">${E(scene.description||'添加场景说明，帮助团队了解检测内容。')}</p>
        <dl><dt>关联模型</dt><dd>${E(scene.model.name)} <span class="muted">v${E(scene.model.version)}</span></dd><dt>检测规则</dt><dd>${scene.rules.length} 项 <span class="muted">· ${scene.sample_fps} fps · ${scene.imgsz}px</span></dd><dt>任务版本</dt><dd>r${scene.revision} <span class="muted">· ${scene.device==='auto'?'CUDA GPU':E(scene.device.toUpperCase())}</span></dd></dl>
        ${attention?`<p class="readiness-note">! ${E(scene.readiness_reason||'关联模型不可用，请检查模型状态与文件。')}</p>`:''}
        <div class="row-actions"><button class="small" data-scene-edit="${E(scene.id)}">${draft?'继续配置':'编辑规则'}</button><button class="small quiet" data-scene-copy="${E(scene.id)}" aria-label="复制 ${E(scene.name)}">复制</button>${!draft&&scene.ready?`<a class="button small primary" href="/?scene=${encodeURIComponent(scene.id)}">进入检测 ↗</a>`:draft?`<button class="small primary" data-scene-edit="${E(scene.id)}">配置并发布 →</button>`:`<button class="small" data-model-details="${E(scene.model_id)}">查看模型</button>`}</div>
        <p class="card-date">最近更新 ${date(scene.updated_at)}</p>
      </article>`;
    }).join('');
    $('#empty-import').textContent=enabled?'＋ 新建检测任务':state.models.length?'前往模型库启用模型':'↓ 导入第一个模型';
    $('#scene-empty-title').textContent=enabled?'模型已就绪，开始配置检测场景':'创建你的第一个检测场景';
    $('#scene-empty-description').textContent=enabled?'选择模型、配置目标与区域，然后发布到检测工作台。':'先导入并启用模型，再定义需要检出的目标、数量和区域。';
    $('#setup-model').classList.toggle('complete',enabled>0);
    if(includeModels)renderModels();
  }
  async function refresh(){
    refreshController?.abort();
    const generation=++state.loading;
    if(!W.session){
      $('#locked-panel').hidden=false;$('#admin-content').hidden=true;
      $('#new-scene-button').disabled=true;$('#heading-import').disabled=true;
      $('#refresh-button').disabled=false;message();return;
    }
    $('#refresh-button').disabled=true;$('#refresh-button').textContent='↻ 同步中';
    $('#admin-content').setAttribute('aria-busy','true');message();
    const controller=new AbortController();refreshController=controller;
    try{
      const catalog=await W.request('/admin/catalog',{signal:controller.signal});
      if(generation!==state.loading||!W.session)return;
      state.models=catalog.models;modelIndex=new Map(state.models.map(model=>[model.id,model]));
      lastSync=Date.now();
      state.scenes=catalog.scenes.map(scene=>({...scene,model:modelIndex.get(scene.model_id)}));
      linkedScenes=new Map();for(const scene of state.scenes){if(!linkedScenes.has(scene.model_id))linkedScenes.set(scene.model_id,[]);linkedScenes.get(scene.model_id).push(scene);}
      $('#locked-panel').hidden=true;$('#admin-content').hidden=false;
      $('#sync-status').textContent='已同步 '+new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
      render();
      if(linkedModel){
        const identity=linkedModel;linkedModel=null;tab('models');
        $('#model-search').value=identity;$('#model-status').value='all';$('#model-type').value='all';renderModels();
        const button=[...document.querySelectorAll('[data-model-details]')].find(el=>el.dataset.modelDetails===identity);
        if(button)button.click();else message('未找到此候选模型。它可能已被删除，或不属于当前账户。');
      }
    }catch(error){if(!controller.signal.aborted&&generation===state.loading&&W.session){message('同步失败：'+error.message+'。请点击刷新重试。');$('#sync-status').textContent='同步失败';}}
    finally{if(generation===state.loading){$('#refresh-button').disabled=false;$('#refresh-button').textContent='↻ 刷新';$('#admin-content').setAttribute('aria-busy','false');}}
  }
  function confirmAction(title,description,label){
    if(state.confirm)return Promise.resolve(false);
    $('#confirm-title').textContent=title;$('#confirm-description').textContent=description;
    $('#confirm-accept').textContent=label;$('#confirm-dialog').showModal();
    return new Promise(resolve=>state.confirm=resolve);
  }
  $('#confirm-accept').onclick=()=>{state.confirm?.(true);state.confirm=null;$('#confirm-dialog').close();};
  $('#confirm-cancel').onclick=()=>$('#confirm-dialog').close();
  $('#confirm-dialog').addEventListener('close',()=>{state.confirm?.(false);state.confirm=null;});
  function editorBody(status='draft'){
    return {name:$('#scene-name').value.trim(),description:$('#scene-description').value.trim(),model_id:$('#scene-model').value,status,
      imgsz:Number($('#scene-imgsz').value),device:$('#scene-device').value,sample_fps:Number($('#scene-rate').value),
      confirmation_frames:Number($('#scene-confirmation').value),min_sharpness:Number($('#scene-sharpness').value),rules:readRules()};
  }
  const editorSnapshot=()=>JSON.stringify(editorBody());
  async function closeDialog(id){
    if((id==='scene-dialog'&&state.saving)||(id==='import-dialog'&&state.importing)){W.toast('正在保存或验证，请稍候。');return false;}
    if(id==='scene-dialog'&&state.snapshot!==editorSnapshot()&&!await confirmAction('放弃未保存的修改？','当前任务的修改尚未保存。你可以返回继续编辑，或放弃本次修改。','放弃修改'))return false;
    $('#'+id).close();return true;
  }
  function runtimeSummary(){
    const fps=Number($('#scene-rate').value),frames=Number($('#scene-confirmation').value);
    const rules=readRules(),regions=new Set(rules.map(r=>JSON.stringify(r.roi))).size;
    $('#runtime-summary').textContent=fps>0&&frames>0?`${rules.length} 项规则共用 ${regions} 个检测区域；每秒最多约 ${regions*fps} 次区域检测。连续 ${frames} 帧确认，帧间跨度约 ${((frames-1)/fps).toFixed(2)} 秒。实际吞吐和延迟需在服务器实测。`:'';
    const model=modelById($('#scene-model').value),people=rules.some(r=>/person|people|worker|人员|人体|工人|行人/i.test(model?.classes?.[r.class_id]||''));
    $('#person-rule-note').hidden=!people;
  }
  let importLimit=0,importCandidates=[],importKey=null,importFingerprint=null,importFile=null,importResult=null,importController=null;
  const importStatus=(text='',bad=false)=>{$('#import-status').textContent=text;$('#import-status').classList.toggle('danger',bad);};
  function importPhase(phase){
    for(const el of document.querySelectorAll('#import-steps li'))el.dataset.active=String(el.dataset.phase===phase);
  }
  function importMode(){
    const local=$('#import-form').elements.source.value==='local';
    $('#local-model-fields').hidden=!local;$('#upload-model-fields').hidden=local;
    $('#candidate-select').required=local;$('#model-file').required=!local;
    for(const el of $('#import-form').querySelectorAll('input,select,textarea,button'))el.disabled=state.importing;
    $('#candidate-select').disabled=state.importing||!local;$('#model-file').disabled=state.importing||local;
    const file=$('#model-file').files[0];
    $('#model-file').setCustomValidity(file&&(!/\.pt$/i.test(file.name)||!file.size)?'请选择非空的 .pt 模型文件':file&&importLimit&&file.size>importLimit?'文件超过服务器上传限制（'+size(importLimit)+'）':'');
    $('#model-file-info').textContent=file?file.name+' · '+size(file.size)+' · 将上传到服务器':'选择文件后会上传到服务器，浏览器电脑的磁盘路径不会作为服务器路径使用。';
    if(file&&!$('#model-file').validity.valid)$('#model-file-info').textContent=$('#model-file').validationMessage;
    $('#model-file-info').classList.toggle('danger',!!file&&!$('#model-file').validity.valid);
    const item=importCandidates.find(v=>v.path===$('#candidate-select').value);
    $('#candidate-select').setCustomValidity(item&&importLimit&&item.size>importLimit?'服务器文件超过当前导入大小限制':'');
    $('#import-submit').disabled=state.importing||(local?!item||!$('#candidate-select').validity.valid:!file||!$('#model-file').validity.valid);
    $('#import-submit').textContent=state.importing?'正在导入…':local?'复制到模型库并验证':'上传并验证';
  }
  async function loadImportCandidates(){
    const generation=state.importGeneration;
    $('#refresh-candidates').disabled=true;
    try{
      const data=await W.request('/admin/model-candidates');
      if(!W.session||!$('#import-dialog').open||generation!==state.importGeneration)return;
      importLimit=data.limits?.max_upload_bytes||0;
      $('#model-import-limit').textContent=importLimit?'服务器单文件上限 '+size(importLimit)+' · 支持 .pt 权重':'服务器将在接收文件时校验大小与格式。';
      importCandidates=data.items.filter(v=>v.source!=='registered');
      $('#server-file-info').textContent=importCandidates.length?'来自服务器 models/、outputs/ 或 DATA_DIR/model-imports/。容器部署时需要挂载对应目录。':'服务器暂无可导入文件。可从当前电脑上传，或将文件放入 DATA_DIR/model-imports/ 后刷新。';
      const select=$('#candidate-select'),prior=select.value;
      select.replaceChildren(new Option(importCandidates.length?'请选择服务器文件':'服务器暂无可导入文件，请从当前电脑上传',''));
      for(const item of importCandidates)select.add(new Option((item.source==='inbox'?'导入目录 / ':'')+item.path+' · '+size(item.size),item.path));
      select.value=prior;
    }catch(e){
      if(generation===state.importGeneration&&W.session){
        $('#server-file-info').textContent='读取服务器文件失败：'+e.message+'。可点击刷新文件重试，或从当前电脑上传。';
        $('#model-import-limit').textContent='限制暂未读取，将由服务器校验文件大小。';
      }
    }finally{if(generation===state.importGeneration)importMode();}
  }
  async function openImport(){
    if(!W.session&&!await W.login())return;
    if(state.importing){$('#import-dialog').showModal();return;}
    ++state.importGeneration;importKey=importFingerprint=importFile=importResult=null;importLimit=0;importCandidates=[];
    $('#import-form').reset();$('#import-form').hidden=false;$('#import-result').hidden=true;$('#import-recover').hidden=true;
    importStatus();importPhase('');$('#model-upload-progress').hidden=true;$('#model-upload-progress').value=0;
    $('#server-file-info').textContent='来自服务器 models/、outputs/ 或 DATA_DIR/model-imports/。容器部署时需要挂载对应目录。';
    $('#candidate-select').replaceChildren(new Option('正在读取服务器文件…',''));
    importMode();$('#import-dialog').showModal();void loadImportCandidates();
  }
  $('#refresh-candidates').onclick=loadImportCandidates;
  $('#import-form').addEventListener('change',()=>{
    importMode();
    const local=$('#import-form').elements.source.value==='local';
    const source=local?$('#candidate-select').value:$('#model-file').files[0]?.name;
    if(!$('#model-name').value&&source)$('#model-name').value=source.split(/[\\/]/).at(-1).replace(/\.pt$/i,'');
  });
  function showImportResult(result){
    importResult=result;importPhase('ready');$('#import-form').hidden=true;$('#import-result').hidden=false;
    $('#import-result-description').textContent=(result.reused?'已找到本次请求的导入结果。':'导入完成。')+'模型文件已保存到服务器共享存储。';
    const rows=[['模型名称',result.name],['版本',result.version],['模型类型',W.taskLabels[result.task]||result.task],['文件大小',size(result.size)],['SHA256',result.sha256]];
    $('#import-result-summary').innerHTML=rows.map(([k,v])=>'<dt>'+E(k)+'</dt><dd>'+E(v)+'</dd>').join('');
    $('#import-result-classes').innerHTML=Object.entries(result.classes).map(([id,name])=>'<span class="tag">'+E(id)+' · '+E(name)+'</span>').join('');
    const trainable=['detect','obb','classify'].includes(result.task);
    $('#import-train-model').hidden=!trainable;$('#import-train-model').href='/training?model='+encodeURIComponent(result.id);
    $('#import-create-scene').disabled=!result.enabled;
    state.models=[result,...state.models.filter(v=>v.id!==result.id)];modelIndex.set(result.id,result);render();tab('models');notifyCatalog();
    $('#import-view-model').focus();void refresh();
  }
  $('#import-view-model').onclick=()=>{
    $('#import-dialog').close();tab('models');$('#model-search').value=importResult.id;$('#model-status').value='all';$('#model-type').value='all';renderModels();
  };
  $('#import-create-scene').onclick=()=>{const result=importResult;$('#import-dialog').close();openScene(null,result.id);};
  async function recoverImport(generation,silent=false){
    if(!importKey)return false;
    try{
      const result=await W.request('/admin/model-imports/'+encodeURIComponent(importKey));
      if(generation!==state.importGeneration||!W.session)return false;
      showImportResult({...result,reused:true});return true;
    }catch(e){
      if(!silent&&generation===state.importGeneration&&W.session)importStatus(e.status===404?'服务器尚无已完成的结果。可稍后再次检查，或重试同一文件。':'检查结果失败：'+e.message,true);
      return false;
    }
  }
  $('#import-recover').onclick=async()=>{
    if(state.importing)return;state.importing=true;importMode();
    try{await recoverImport(state.importGeneration);}finally{state.importing=false;importMode();}
  };
  $('#import-form').onsubmit=async event=>{
    event.preventDefault();if(state.importing||!W.session)return;
    const name=$('#model-name').value.trim(),version=$('#model-version').value.trim(),notes=$('#model-notes').value.trim();
    if(!name||!version){importStatus('请填写模型名称和版本。',true);return;}
    importMode();if(!$('#import-form').reportValidity())return;
    const local=$('#import-form').elements.source.value==='local',path=$('#candidate-select').value,file=$('#model-file').files[0],generation=state.importGeneration;
    const fingerprint=JSON.stringify({local,path:local?path:null,name,version,notes});
    if(!importKey||fingerprint!==importFingerprint||(!local&&file!==importFile)){importKey=crypto.randomUUID();importFingerprint=fingerprint;importFile=file;}
    const headers={'Idempotency-Key':importKey};
    state.importing=true;importController=new AbortController();importMode();$('#import-form').setAttribute('aria-busy','true');$('#import-recover').hidden=true;
    importPhase(local?'verify':'transfer');importStatus(local?'服务器正在复制文件并验证模型…':'正在上传到服务器…');
    try{
      let result;
      if(local)result=await W.request('/admin/models/import',{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({path,name,version,notes}),timeout:600000,signal:importController.signal});
      else{
        $('#model-upload-progress').hidden=false;$('#model-upload-progress').value=0;
        headers['X-Model-Notes']=btoa(String.fromCharCode(...new TextEncoder().encode(notes)));
        result=await W.upload('/admin/models/upload?'+new URLSearchParams({name,version,filename:file.name}),file,percent=>{
          if(generation!==state.importGeneration||!W.session)return;
          $('#model-upload-progress').value=percent;
          if(percent===100)importPhase('verify');
          importStatus(percent===100?'文件已传输，服务器正在验证模型类型和类别，请稍候…':'上传到服务器 '+percent+'%');
        },{headers,signal:importController.signal,timeout:600000});
      }
      if(generation===state.importGeneration&&W.session)showImportResult(result);
    }catch(e){
      if(generation!==state.importGeneration||!W.session)return;
      const uncertain=!e.status||e.status>=500;
      if(uncertain&&await recoverImport(generation,true))return;
      importStatus(e.message+(uncertain?' 可以检查服务器结果；重试当前文件会沿用本次请求编号。':''),true);
      $('#import-recover').hidden=!uncertain;
    }finally{
      if(generation===state.importGeneration){state.importing=false;importController=null;$('#import-form').setAttribute('aria-busy','false');importMode();}
    }
  };
  const options=(model,selected)=>Object.entries(model?.classes||{}).map(([id,name])=>`<option value="${id}" ${Number(id)===selected?'selected':''}>${E(name)}（${id}）</option>`).join('');
  function readRules(){return [...$('#rule-editors').querySelectorAll('.rule')].map(node=>({id:node.dataset.rule,name:node.querySelector('[data-field=name]').value.trim(),class_id:Number(node.querySelector('[data-field=class_id]').value),kind:node.querySelector('[data-field=kind]').value,min_count:Number(node.querySelector('[data-field=min_count]').value),max_count:node.querySelector('[data-field=max_count]').value===''?null:Number(node.querySelector('[data-field=max_count]').value),threshold:Number(node.querySelector('[data-field=threshold]').value),roi:['x1','y1','x2','y2'].map(field=>Number(node.querySelector(`[data-field=${field}]`).value))}));}
  function renderRules(){
    const model=modelById($('#scene-model').value),classification=model?.task==='classify';
    $('#rule-editors').innerHTML=state.rules.map((rule,index)=>`<section class="rule ${rule.id===state.activeROI?'active-roi':''}" data-rule="${E(rule.id)}">
      <div class="rule-heading"><span class="muted">规则 ${String(index+1).padStart(2,'0')}</span><div class="row-actions"><button class="small quiet" type="button" data-copy-rule="${E(rule.id)}" ${state.rules.length>=32?'disabled':''}>复制</button><button class="small quiet danger" type="button" data-remove-rule="${E(rule.id)}">移除</button></div></div>
      <div class="rule-grid"><label>规则名称<input data-field="name" value="${E(rule.name)}" maxlength="100" required></label>
      <label>模型类别<select data-field="class_id" required>${options(model,rule.class_id)}</select></label>
      <label>判定方式<select data-field="kind"><option value="required" ${rule.kind==='required'?'selected':''}>必须检出</option><option value="forbidden" ${rule.kind==='forbidden'?'selected':''}>禁止出现</option></select></label>
      <label>置信阈值<input data-field="threshold" type="number" min="0.05" max="0.99" step="0.01" value="${rule.threshold}" required></label>
      <label>至少数量<input data-field="min_count" type="number" min="1" max="${classification?'1':'300'}" step="1" value="${classification?1:rule.min_count}" ${classification?'readonly':''} required></label>
      <label>最多数量<input data-field="max_count" type="number" min="1" max="${classification?'1':'300'}" step="1" placeholder="不限" value="${classification?1:rule.max_count??''}" ${classification?'readonly':''}></label></div>
      <p class="rule-note">${rule.kind==='forbidden'?'该类别连续检出达到确认帧数后判为异常；恢复未检出也需连续确认。数量设置不参与禁止规则判定。':'目标数量、置信度与检测区域同时满足后，才计为有效证据。'}</p>
      <details style="margin-top:12px"><summary class="hint">检测区域 · ${rule.roi.join(',')==='0,0,1,1'?'整幅画面':'已限定区域'}</summary>
      <div class="roi-grid" style="margin-top:10px">${['x1','y1','x2','y2'].map((key,i)=>`<label>${key}<input data-field="${key}" type="number" min="0" max="1" step="0.001" value="${rule.roi[i]}" required></label>`).join('')}
      <button type="button" class="small" data-select-roi="${E(rule.id)}">图上框选</button></div></details></section>`).join('');
    $('#rule-count').textContent=`${state.rules.length} / 32`;
    $('#rule-empty').hidden=state.rules.length>0;
    $('#add-rule').disabled=!model||state.rules.length>=32;
    for(const button of document.querySelectorAll('[data-add-class]'))button.disabled=state.rules.length>=32;
    drawROI();
    runtimeSummary();
  }
  const ruleId=()=>'rule-'+crypto.randomUUID().slice(0,8);
  function addRule(classId){
    state.rules=readRules();const model=modelById($('#scene-model').value);
    if(!model||state.rules.length>=32)return;
    const key=typeof classId==='number'?String(classId):Object.keys(model.classes)[0],name=model.classes[key];
    if(name===undefined)return;
    const id=ruleId();state.rules.push({id,name:'检出 '+name,class_id:Number(key),kind:'required',min_count:1,max_count:model.task==='classify'?1:null,threshold:.6,roi:[0,0,1,1]});
    state.activeROI=id;renderRules();
    $('#rule-editors').lastElementChild?.querySelector('[data-field=name]').focus({preventScroll:true});
  }
  function modelInfo(){
    const model=modelById($('#scene-model').value);
    $('#selected-model-info').innerHTML=model?`<span class="model-avatar" aria-hidden="true">◇</span><span><strong>${E(W.taskLabels[model.task])}</strong> · ${Object.keys(model.classes).length} 个类别 · v${E(model.version)}${model.enabled?'':' <span class="danger">· 已停用，仅可保存草稿</span>'}</span>`:'请选择模型';
    const entries=Object.entries(model?.classes||{});
    $('#class-shortcuts').innerHTML=entries.length?'<span class="hint">快速添加类别规则</span>'+entries.slice(0,30).map(([id,name])=>`<button type="button" data-add-class="${id}">＋ ${E(name)}</button>`).join('')+(entries.length>30?'<span class="hint">其余类别可通过“添加规则”选择</span>':''):'';
  }
  function openScene(scene=null,modelId=null,copy=false){
    if(!W.session)return W.login();
    if(!state.models.length)return openImport();
    sceneRequestKey=sceneRequestBody=null;
    state.editing=scene&&!copy?scene:null;state.snapshot=null;$('#scene-validation').hidden=true;$('#scene-form').reset();$('#scene-save-status').classList.remove('danger');$('#scene-form button[value=draft]').textContent=state.editing?.status==='published'?'转为草稿':'保存草稿';$('#scene-save-status').textContent='';$('#scene-editor-title').textContent=state.editing?'编辑检测任务 · r'+scene.revision:copy?'复制检测任务':'新建检测任务';
    $('#scene-model').replaceChildren(...state.models.map(model=>new Option(model.name+' · v'+model.version+(model.enabled?'':'（已停用）'),model.id)));
    $('#scene-model').value=modelId||scene?.model_id||state.models.find(m=>m.enabled)?.id||state.models[0].id;
    state.modelId=$('#scene-model').value;$('#scene-name').value=scene?scene.name+(copy?' · 副本':''):'';$('#scene-description').value=scene?.description||'';$('#scene-imgsz').value=scene?.imgsz||640;$('#scene-device').value='cuda';$('#scene-rate').value=scene?.sample_fps||4;$('#scene-confirmation').value=scene?.confirmation_frames||2;$('#scene-sharpness').value=scene?.min_sharpness??20;
    state.rules=structuredClone(scene?.rules||[]);state.activeROI=state.rules[0]?.id||null;state.image=null;if(state.imageURL){URL.revokeObjectURL(state.imageURL);state.imageURL=null;}$('#roi-image').value='';$('#roi-canvas').hidden=true;modelInfo();renderRules();if(!state.rules.length)addRule();runtimeSummary();$('#scene-dialog').showModal();state.snapshot=editorSnapshot();$('#scene-name').focus();
  }
  $('#scene-model').onchange=async()=>{
    const next=$('#scene-model').value,previous=state.modelId,epoch=sessionEpoch;
    if(next===previous)return;
    $('#scene-model').value=previous;
    const rules=readRules();
    if(rules.length&&!await confirmAction('切换关联模型？',`当前 ${rules.length} 项规则属于原模型。切换后会清空这些规则，任务名称、说明和运行参数将保留。请按新模型的类别重新配置。`,'切换并重建规则'))return;
    if(epoch!==sessionEpoch||!W.session)return;
    $('#scene-model').value=next;state.modelId=next;state.rules=[];state.activeROI=null;
    modelInfo();renderRules();addRule();$('#scene-validation').hidden=true;
  };
  $('#class-shortcuts').onclick=event=>{const button=event.target.closest('[data-add-class]');if(button)addRule(Number(button.dataset.addClass));};
  $('#rule-editors').addEventListener('input',()=>{state.rules=readRules();drawROI();});
  $('#rule-editors').addEventListener('change',event=>{
    if(event.target.dataset.field==='kind'){
      const rule=event.target.closest('.rule');
      rule.querySelector('.rule-note').textContent=event.target.value==='forbidden'?'该类别连续检出达到确认帧数后判为异常；恢复未检出也需连续确认。数量设置不参与禁止规则判定。':'目标数量、置信度与检测区域同时满足后，才计为有效证据。';
    }
  });
  $('#rule-editors').onclick=event=>{
    const remove=event.target.closest('[data-remove-rule]'),copy=event.target.closest('[data-copy-rule]'),select=event.target.closest('[data-select-roi]');
    if(remove){
      state.rules=readRules().filter(rule=>rule.id!==remove.dataset.removeRule);
      if(!state.rules.some(rule=>rule.id===state.activeROI))state.activeROI=state.rules[0]?.id||null;
      renderRules();
    }
    if(copy&&state.rules.length<32){
      state.rules=readRules();const source=state.rules.find(rule=>rule.id===copy.dataset.copyRule);
      const duplicate={...structuredClone(source),id:ruleId(),name:(source.name+' · 副本').slice(0,100)};
      state.rules.splice(state.rules.indexOf(source)+1,0,duplicate);state.activeROI=duplicate.id;renderRules();
    }
    if(select){
      state.activeROI=select.dataset.selectRoi;const name=readRules().find(rule=>rule.id===state.activeROI)?.name;
      $('#roi-instruction').textContent=`当前框选：${name}。${state.image?'在下方图像上拖动。':'请先选择一张参考图。'}`;
      (state.image?$('#roi-canvas'):$('#roi-image')).scrollIntoView({block:'nearest'});drawROI();
    }
  };
  $('#add-rule').onclick=()=>addRule();
  $('#scene-form').addEventListener('input',runtimeSummary);
  $('#scene-form').onsubmit=async event=>{
    event.preventDefault();if(state.saving)return;
    const epoch=sessionEpoch;
    const body=editorBody(event.submitter?.value||'draft'),issues=AdminCatalog.validate(body,modelById(body.model_id));
    if(issues.length){
      $('#scene-validation').innerHTML='<strong>请先检查以下配置</strong><ul>'+issues.map(issue=>`<li>${E(issue)}</li>`).join('')+'</ul>';
      $('#scene-validation').hidden=false;$('#scene-validation').scrollIntoView({block:'nearest'});
      $('#scene-save-status').textContent='有 '+issues.length+' 项配置需要调整';$('#scene-save-status').classList.add('danger');return;
    }
    if(state.editing?.status==='published'&&body.status==='draft'&&!await confirmAction('将已发布任务转为草稿？','转为草稿后，该任务将从检测工作台的可选任务中移除。已有检测记录会保留，后续可再次发布。','确认转为草稿'))return;
    if(epoch!==sessionEpoch||!W.session)return;
    $('#scene-validation').hidden=true;$('#scene-save-status').classList.remove('danger');
    state.saving=true;$('#scene-form').setAttribute('aria-busy','true');
    const controls=[...$('#scene-form').querySelectorAll('input,select,textarea,button')],disabled=controls.map(control=>control.disabled);
    controls.forEach(control=>control.disabled=true);
    $('#scene-save-status').textContent=body.status==='published'?'正在验证并发布…':'正在保存草稿…';
    try{
      if(state.editing)body.revision=state.editing.revision;
      const serialized=JSON.stringify(body);
      if(sceneRequestBody!==serialized){sceneRequestBody=serialized;sceneRequestKey=crypto.randomUUID();}
      const result=await W.request('/admin/scenes'+(state.editing?'/'+state.editing.id:''),{method:state.editing?'PUT':'POST',headers:{'Content-Type':'application/json','Idempotency-Key':sceneRequestKey},body:serialized});
      if(epoch!==sessionEpoch||!W.session)return;
      $('#scene-dialog').close();notifyCatalog();await refresh();
      if(epoch!==sessionEpoch||!W.session)return;
      // Make the saved item visible even when the previous list used a filter.
      $('#task-search').value='';$('#task-sort').value='updated';filterTasks('all');
      W.toast(result.status==='published'?'任务已发布，可在检测工作台选择':'草稿已保存，可随时继续配置');
    }catch(error){
      if(epoch!==sessionEpoch||!W.session)return;
      $('#scene-save-status').classList.add('danger');
      $('#scene-save-status').textContent=error.status===409?'任务或模型已发生变化，请保留当前配置，关闭后刷新并重新打开任务。':error.message;
    }finally{if(epoch===sessionEpoch){state.saving=false;controls.forEach((control,index)=>control.disabled=disabled[index]);$('#scene-form').setAttribute('aria-busy','false');}}
  };
  function notifyCatalog(){try{localStorage.setItem('aisop-scene-catalog-changed',String(Date.now()));}catch{}}
  function drawROI(){const canvas=$('#roi-canvas');if(!state.image)return;const ctx=canvas.getContext('2d');const scale=Math.min(1000/state.image.naturalWidth,1440/state.image.naturalHeight,1);canvas.width=Math.max(1,Math.round(scale*state.image.naturalWidth));canvas.height=Math.max(1,Math.round(scale*state.image.naturalHeight));ctx.drawImage(state.image,0,0,canvas.width,canvas.height);const theme=getComputedStyle(document.body),accent=theme.getPropertyValue('--accent').trim(),muted=theme.getPropertyValue('--muted').trim();for(const rule of state.rules){const [a,b,c,d]=rule.roi,active=rule.id===state.activeROI;ctx.strokeStyle=active?accent:muted;ctx.lineWidth=active?3:1;ctx.save();ctx.globalAlpha=active?.1:.04;ctx.fillStyle=ctx.strokeStyle;ctx.fillRect(a*canvas.width,b*canvas.height,(c-a)*canvas.width,(d-b)*canvas.height);ctx.restore();ctx.strokeRect(a*canvas.width,b*canvas.height,(c-a)*canvas.width,(d-b)*canvas.height);ctx.font='14px sans-serif';ctx.fillStyle=ctx.strokeStyle;ctx.fillText(rule.name,Math.max(4,a*canvas.width+5),Math.max(18,b*canvas.height+18));}}
  $('#roi-image').onchange=()=>{
    const file=$('#roi-image').files[0];if(!file)return;
    if(state.imageURL)URL.revokeObjectURL(state.imageURL);
    state.image=null;$('#roi-canvas').hidden=true;
    const image=new Image(),url=URL.createObjectURL(file);state.imageURL=url;
    image.onload=()=>{
      if(state.imageURL!==url||!$('#scene-dialog').open)return;
      state.image=image;$('#roi-canvas').hidden=false;state.activeROI=state.activeROI||state.rules[0]?.id;drawROI();
    };
    image.onerror=()=>{if(state.imageURL===url)W.toast('参考图无法读取，请选择有效图片');};image.src=url;
  };
  let drag=null;const canvas=$('#roi-canvas');const pointer=event=>{const box=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))];};
  canvas.onpointerdown=event=>{if(state.saving||event.button!==0||!state.activeROI)return;state.rules=readRules();drag={id:event.pointerId,start:pointer(event),roi:[...state.rules.find(rule=>rule.id===state.activeROI).roi]};canvas.setPointerCapture(event.pointerId);};canvas.onpointermove=event=>{if(!drag||event.pointerId!==drag.id)return;const end=pointer(event),rule=state.rules.find(rule=>rule.id===state.activeROI);if(!rule)return;rule.roi=[Math.min(drag.start[0],end[0]),Math.min(drag.start[1],end[1]),Math.max(drag.start[0],end[0]),Math.max(drag.start[1],end[1])].map(v=>Math.round(v*1000)/1000);drawROI();};canvas.onpointerup=()=>{if(drag){const rule=state.rules.find(rule=>rule.id===state.activeROI);if(rule&&(rule.roi[2]<=rule.roi[0]||rule.roi[3]<=rule.roi[1]))rule.roi=drag.roi;drag=null;renderRules();}};canvas.onpointercancel=()=>{if(drag){const rule=state.rules.find(rule=>rule.id===state.activeROI);if(rule)rule.roi=drag.roi;drag=null;renderRules();}};
  document.addEventListener('click',async event=>{
    const close=event.target.closest('[data-close]');if(close){await closeDialog(close.dataset.close);return;}
    const edit=event.target.closest('[data-scene-edit]'),copy=event.target.closest('[data-scene-copy]'),use=event.target.closest('[data-model-use]'),details=event.target.closest('[data-model-details]'),toggle=event.target.closest('[data-model-toggle]');
    if(edit)openScene(state.scenes.find(scene=>scene.id===edit.dataset.sceneEdit));
    if(copy)openScene(state.scenes.find(scene=>scene.id===copy.dataset.sceneCopy),null,true);
    if(use)openScene(null,use.dataset.modelUse);
    if(details){
      const model=modelById(details.dataset.modelDetails);if(!model){W.toast('模型信息已变化，请刷新后查看。');return;}
      const linked=linkedScenes.get(model.id)||[];
      $('#model-details-content').innerHTML=`<div class="model-detail"><h3>${E(model.name)} <span class="muted">v${E(model.version)}</span></h3><dl><dt>任务类型</dt><dd>${E(W.taskLabels[model.task])}</dd><dt>模型状态</dt><dd>${model.enabled?'已启用':'已停用'}</dd><dt>文件大小</dt><dd>${size(model.size)}</dd><dt>注册时间</dt><dd>${date(model.created_at)}</dd><dt>关联场景</dt><dd>${linked.length?linked.map(scene=>E(scene.name)).join('、'):'尚未关联场景'}</dd><dt>文件校验</dt><dd class="code">${E(model.sha256)}</dd></dl>${model.notes?`<p class="hint">${E(model.notes)}</p>`:''}<h3>类别映射 <span class="muted">${Object.keys(model.classes).length}</span></h3><div class="model-class-list">${Object.entries(model.classes).map(([id,name])=>`<span class="tag"><span class="muted">${E(id)}</span> ${E(name)}</span>`).join('')}</div></div>`;
      $('#model-details-dialog').showModal();
    }
    if(toggle){
      const epoch=sessionEpoch;
      const model=modelById(toggle.dataset.modelToggle);if(!model)return;
      const published=(linkedScenes.get(model.id)||[]).filter(scene=>scene.status==='published').length;
      if(model.enabled&&published&&!await confirmAction('停用这个模型？',`「${model.name}」关联了 ${published} 个已发布任务。停用后，这些任务将无法接受新的检测；已有检测记录会保留。`,'确认停用'))return;
      if(epoch!==sessionEpoch||!W.session)return;
      toggle.disabled=true;
      try{await W.json('/admin/models/'+model.id,'PATCH',{enabled:!model.enabled});if(epoch!==sessionEpoch||!W.session)return;await refresh();if(epoch!==sessionEpoch||!W.session)return;notifyCatalog();W.toast(model.enabled?'模型已停用，关联任务停止接受新检测':'模型已启用');}
      catch(error){if(epoch===sessionEpoch)message(error.message);}finally{toggle.disabled=false;}
    }
  });
  for(const id of ['scene-dialog','import-dialog'])$('#'+id).addEventListener('cancel',event=>{event.preventDefault();closeDialog(id);});
  $('#scene-dialog').addEventListener('close',()=>{if(state.imageURL)URL.revokeObjectURL(state.imageURL);state.imageURL=null;state.image=null;drag=null;});
  window.addEventListener('beforeunload',event=>{if(state.importing||state.saving||($('#scene-dialog').open&&state.snapshot!==editorSnapshot())){event.preventDefault();event.returnValue='';}});
  $('#tasks-tab').onclick=()=>tab('tasks');$('#models-tab').onclick=()=>tab('models');
  $('.tabs').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();tab(event.key==='Home'?'tasks':event.key==='End'?'models':state.tab==='tasks'?'models':'tasks');$('#'+state.tab+'-tab').focus();});
  $('#task-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{taskPage=0;render(false);},180);};$('#task-sort').onchange=()=>{taskPage=0;render(false);};
  for(const kind of ['task','model'])for(const direction of ['previous','next'])$('#'+kind+'-'+direction).onclick=()=>{
    const delta=direction==='next'?1:-1;if(kind==='task'){taskPage+=delta;render();}else{modelPage+=delta;renderModels();}
  };
  for(const button of document.querySelectorAll('[data-task-filter]'))button.onclick=()=>filterTasks(button.dataset.taskFilter);
  for(const button of document.querySelectorAll('[data-overview]'))button.onclick=()=>{const value=button.dataset.overview;if(value==='models')tab('models');else filterTasks(value);};
  $('#clear-task-filters').onclick=()=>{$('#task-search').value='';filterTasks('all');$('#task-search').focus();};
  const filterModels=()=>{modelPage=0;renderModels();};
  $('#model-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(filterModels,180);};$('#model-status').onchange=filterModels;$('#model-type').onchange=filterModels;
  $('#clear-model-filters').onclick=()=>{$('#model-search').value='';$('#model-status').value='all';$('#model-type').value='all';filterModels();$('#model-search').focus();};
  $('#new-scene-button').onclick=()=>openScene();$('#heading-import').onclick=openImport;
  $('#import-model-button').onclick=openImport;$('#empty-model-import').onclick=openImport;
  $('#empty-import').onclick=()=>state.models.some(model=>model.enabled)?openScene():state.models.length?tab('models'):openImport();
  $('#refresh-button').onclick=refresh;$('#locked-login').onclick=()=>W.login();
  $('#account-button').onclick=async()=>{try{if(W.session){if($('#scene-dialog').open&&!await closeDialog('scene-dialog'))return;await W.logout();}else await W.login();}catch(error){message(error.message);}};
  document.addEventListener('workspace-session',()=>{
    ++sessionEpoch;clearTimeout(searchTimer);
    if(!W.session){
      state.models=[];state.scenes=[];modelIndex.clear();linkedScenes.clear();state.snapshot=null;
      state.rules=[];state.editing=null;state.modelId=null;state.activeROI=null;
      state.saving=false;sceneRequestKey=sceneRequestBody=null;
      for(const id of ['scene-cards','model-rows','model-details-content','class-shortcuts','selected-model-info','rule-editors'])$('#'+id).replaceChildren();
      $('#scene-form').reset();$('#scene-form').querySelectorAll('input,select,textarea,button').forEach(el=>el.disabled=false);
      ++state.importGeneration;importController?.abort();importController=null;state.importing=false;
      importKey=importFingerprint=importFile=importResult=null;$('#import-form').reset();
      $('#import-result-summary').replaceChildren();$('#import-result-classes').replaceChildren();
      for(const dialog of document.querySelectorAll('dialog[open]'))if(dialog.id!=='workspace-login')dialog.close();
    }
    refresh();
  });
  window.AdminWorkspace={state,refresh,openScene};
  const syncVisible=()=>{if(W.session&&!document.hidden&&!state.saving&&!state.importing&&Date.now()-lastSync>15000)void refresh();};
  window.addEventListener('focus',syncVisible);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)refreshController?.abort();else syncVisible();});
  window.addEventListener('storage',event=>{if(event.key==='aisop-scene-catalog-changed'){lastSync=0;syncVisible();}});
  window.addEventListener('pagehide',()=>{clearTimeout(searchTimer);refreshController?.abort();});
  W.connect().catch(error=>{message(error.message);$('#locked-panel').hidden=false;});
})();
