/* Video preparation uses durable server jobs; all labels require explicit review. */
(()=>{
  'use strict';
  const W=window.Workspace,$=s=>document.querySelector(s),E=W.escape;
  const labels={pending:'待标注',labelled:'已标注',negative:'空背景',excluded:'已排除',QUEUED:'等待处理',RUNNING:'处理中',SUCCEEDED:'已完成',FAILED:'失败'};
  const splits={train:'训练集',val:'验证集',test:'测试集'},active=j=>['QUEUED','RUNNING'].includes(j.state);
  let did=new URLSearchParams(location.search).get('id'),data=null,items=[],frameItems=[],datasetOffset=0,frameOffset=0;
  let listHasMore=false,epoch=0,generation=0,timer,controller,uploadController,failures=0,busy=false,frameSignature='',listSignature='';
  let annotation=null,boxes=[],picture=null,drag=null,dirty=false,saving=false,imageGeneration=0;
  const keys=new Map(),value=id=>$('#'+id).value;
  function error(message=''){$('#error').textContent=message;$('#error').hidden=!message;}
  function requestKey(scope,body){const signature=JSON.stringify(body),old=keys.get(scope);if(old?.signature===signature)return old.key;const key=crypto.randomUUID();keys.set(scope,{signature,key});return key;}
  function post(path,body,scope){return W.request(path,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':requestKey(scope,body)},body:JSON.stringify(body)});}
  function toggleBusy(){
    $('#upload-fields').disabled=busy||!!data?.active_export;$('#export-fields').disabled=busy||!!data?.active_export;
    for(const b of document.querySelectorAll('.dataset-choice,#new-dataset,[data-remove],[data-retry]'))b.disabled=busy;
    $('#dataset-prev').disabled=busy||datasetOffset===0;$('#dataset-next').disabled=busy||!listHasMore;
  }
  function renderList(result){
    items=result.items;listHasMore=result.has_more;
    const signature=JSON.stringify([items.map(d=>[d.id,d.name,d.task,d.station]),did,busy]);
    if(signature!==listSignature){listSignature=signature;$('#dataset-list').innerHTML=items.map(d=>`<button class="dataset-choice" data-dataset="${E(d.id)}" aria-current="${d.id===did}" ${busy?'disabled':''}><strong>${E(d.name)}</strong><small>${E(d.station.replace('station','工位 '))} · ${d.task==='detect'?'目标检测':'图像分类'}</small></button>`).join('')||'<p class="empty">还没有数据集</p>';}
    $('#dataset-page').textContent=`${result.total?Math.floor(datasetOffset/25)+1:0} / ${Math.ceil(result.total/25)}`;
    $('#dataset-prev').disabled=busy||datasetOffset===0;$('#dataset-next').disabled=busy||!result.has_more;
  }
  function policy(){return {min_batches_per_split:Number(value('min-batches')),min_class_samples:Number(value('min-classes')),min_negative_samples:Number(value('min-negatives')),min_precision:.95,min_recall:.95};}
  function coverage(){
    if(!data)return;
    const p=policy(),gaps=[],cell=(count,min)=>`<span class="${count>=min?'met':'missing'}">${count} / ${min}</span>`;
    const rows=Object.entries(data.coverage).map(([split,c])=>{
      if(c.batches<p.min_batches_per_split)gaps.push(`${splits[split]}缺 ${p.min_batches_per_split-c.batches} 个独立批次`);
      for(let i=0;i<c.classes.length;i++)if(c.classes[i]<p.min_class_samples)gaps.push(`${splits[split]}「${data.classes[i]}」缺 ${p.min_class_samples-c.classes[i]} 张`);
      if(data.task==='detect'&&c.negatives<p.min_negative_samples)gaps.push(`${splits[split]}缺 ${p.min_negative_samples-c.negatives} 张空背景`);
      return `<tr><th>${splits[split]}<small>${c.samples} 张确认样本</small></th><td>${cell(c.batches,p.min_batches_per_split)}</td>${c.classes.map(n=>`<td>${cell(n,p.min_class_samples)}</td>`).join('')}${data.task==='detect'?`<td>${cell(c.negatives,p.min_negative_samples)}</td>`:''}</tr>`;
    });
    $('#coverage').innerHTML=`<table><thead><tr><th>集合</th><th>独立批次</th>${data.classes.map(n=>`<th>${E(n)}</th>`).join('')}${data.task==='detect'?'<th>空背景</th>':''}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
    $('#coverage-gaps').textContent=(data.reviews.pending?`还有 ${data.reviews.pending} 张待复核。 `:'')+(gaps.length?gaps.join('；'):'数量已满足当前门槛，生成版本时将继续审核来源与内容隔离。');
    $('#export-button').disabled=!!data.active_export||data.videos.some(active)||!!data.reviews.pending||gaps.length>0;
  }
  function renderDetail(){
    $('#empty').hidden=!!data;$('#detail').hidden=!data;if(!data)return;
    $('#dataset-title').textContent=data.name;$('#dataset-meta').textContent=`${data.station.replace('station','工位 ')} · ${data.task==='detect'?'目标检测':'图像分类'} · ${data.classes.join(' / ')} · 修订 ${data.revision}`;
    $('#freeze-notice').hidden=!data.active_export;
    $('#upload-limits').textContent=`单段不超过 ${Math.round(data.limits.max_upload_bytes/1024/1024)} MiB、${data.limits.max_video_seconds} 秒、4K / 120 fps。`;
    $('#negative-policy').hidden=data.task!=='detect';
    $('#review-counts').textContent=['pending','labelled','negative','excluded'].map(k=>`${labels[k]} ${data.reviews[k]||0}`).join(' · ');
    const videoSignature=JSON.stringify([data.videos,busy,!!data.active_export]);
    if($('#video-list').dataset.signature!==videoSignature){
      $('#video-list').dataset.signature=videoSignature;
      $('#video-list').innerHTML=data.videos.map(v=>`<div class="video-row"><div><strong>${E(v.name)}</strong><p>${splits[v.capture.split]} · ${E(v.capture.batch)} · ${E(v.capture.cycle)} · 最多 ${v.frame_budget} 张</p><p class="${v.state==='FAILED'?'danger':''}">${E(v.message)}</p></div><span class="badge ${v.state==='FAILED'?'error':active(v)?'review':'draft'}">${labels[v.state]}${v.state==='RUNNING'?' '+Math.round(v.progress*100)+'%':''}</span><div class="actions">${v.state==='FAILED'?`<button data-retry="${v.job_id}">重试</button>`:''}${!active(v)&&!data.active_export?`<button class="quiet" data-remove="${v.id}">移除</button>`:''}</div></div>`).join('')||'<p class="hint">尚未上传视频。</p>';
    }
    const select=$('#video-filter'),old=select.value;
    if(select.dataset.signature!==JSON.stringify(data.videos.map(v=>v.id))){select.dataset.signature=JSON.stringify(data.videos.map(v=>v.id));select.replaceChildren(new Option('全部视频',''));for(const v of data.videos)select.add(new Option(v.name+' · '+splits[v.capture.split],v.id));select.value=data.videos.some(v=>v.id===old)?old:'';}
    $('#export-list').innerHTML=data.exports.map(j=>`<div class="export-row"><div><strong>${labels[j.state]} · ${new Date(j.created_at*1000).toLocaleString('zh-CN')}</strong><p>${E(j.message)}</p></div>${j.state==='SUCCEEDED'?`<a class="button primary" href="/training?dataset=${encodeURIComponent(j.id)}">进入训练 ↗</a>`:`<span class="badge ${j.state==='FAILED'?'error':'review'}">${Math.round(j.progress*100)}%</span>`}</div>`).join('');
    toggleBusy();coverage();
  }
  function renderFrames(result){
    frameItems=result.items;const signature=JSON.stringify([frameItems,frameOffset]);
    if(signature!==frameSignature){frameSignature=signature;$('#frames').innerHTML=frameItems.map(f=>`<button class="frame-card" data-frame="${f.id}" data-review="${f.review}" aria-label="${labels[f.review]} ${f.timestamp.toFixed(2)} 秒"><img src="${E(f.image)}?thumbnail=true" loading="lazy" decoding="async" alt="视频抽帧 ${f.timestamp.toFixed(2)} 秒"><span>${f.timestamp.toFixed(2)} 秒 · ${labels[f.review]}</span></button>`).join('')||'<div class="empty">暂无符合条件的图片。上传后等待抽帧完成，或调整筛选条件。</div>';}
    $('#frame-page').textContent=`${result.total?Math.floor(frameOffset/24)+1:0} / ${Math.ceil(result.total/24)} 页 · ${result.total} 张`;
    $('#frame-prev').disabled=frameOffset===0;$('#frame-next').disabled=!result.has_more;
  }
  async function refresh(){
    clearTimeout(timer);controller?.abort();controller=new AbortController();const signal=controller.signal,current=++generation,session=epoch;
    if(!W.session)return;
    try{
      const listing=await W.request('/admin/datasets?offset='+datasetOffset,{signal});
      if(current!==generation||session!==epoch)return;
      if(datasetOffset&&datasetOffset>=listing.total){datasetOffset=Math.max(0,Math.ceil(listing.total/25)-1)*25;return refresh();}
      if(!did&&listing.items.length){did=listing.items[0].id;history.replaceState(null,'','/datasets?id='+encodeURIComponent(did));}
      renderList(listing);
      if(did){
        const detail=await W.request('/admin/datasets/'+encodeURIComponent(did),{signal});
        if(current!==generation||session!==epoch)return;data=detail;renderDetail();
        const params=new URLSearchParams({offset:frameOffset,review:value('review-filter')});if(value('video-filter'))params.set('video',value('video-filter'));
        const result=await W.request('/admin/datasets/'+did+'/frames?'+params,{signal});
        if(current!==generation||session!==epoch)return;
        if(frameOffset&&frameOffset>=result.total){frameOffset=Math.max(0,Math.ceil(result.total/24)-1)*24;return refresh();}
        renderFrames(result);$('#sync').textContent='已同步 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
      }else{data=null;renderDetail();}
      failures=0;
    }catch(e){if(!signal.aborted&&session===epoch){failures++;error('读取失败：'+e.message);}}
    finally{if(current===generation&&W.session&&!document.hidden)timer=setTimeout(refresh,failures?Math.min(60000,3000*2**Math.min(failures,5)):data&&(data.videos.some(active)||data.active_export)?3000:15000);}
  }
  function selectDataset(id){if(busy)return;did=id;data=null;frameOffset=0;frameSignature='';$('#frames').replaceChildren();$('#review-filter').value='all';$('#video-filter').replaceChildren(new Option('全部视频',''));$('#video-filter').dataset.signature='';$('#upload-form').reset();$('#upload-status').textContent='';$('#export-status').textContent='';history.replaceState(null,'','/datasets?id='+encodeURIComponent(id));error();void refresh();}
  $('#dataset-list').onclick=e=>{const b=e.target.closest('[data-dataset]');if(b)selectDataset(b.dataset.dataset);};
  $('#dataset-prev').onclick=()=>{datasetOffset=Math.max(0,datasetOffset-25);void refresh();};$('#dataset-next').onclick=()=>{datasetOffset+=25;void refresh();};
  $('#frame-prev').onclick=()=>{frameOffset=Math.max(0,frameOffset-24);void refresh();};$('#frame-next').onclick=()=>{frameOffset+=24;void refresh();};
  for(const id of ['review-filter','video-filter'])$('#'+id).onchange=()=>{frameOffset=0;void refresh();};
  $('#refresh').onclick=()=>{error();void refresh();};
  $('#new-dataset').onclick=()=>{$('#create-error').textContent='';$('#create-dialog').showModal();};
  $('#create-close').onclick=()=>$('#create-dialog').close();
  $('#create-form').onsubmit=async e=>{
    e.preventDefault();if(busy)return;const session=epoch,body={name:value('new-name').trim(),station:value('new-station'),task:value('new-task'),classes:value('new-classes').split(/\r?\n/).map(s=>s.trim()).filter(Boolean)};
    if(!body.name||!body.classes.length||body.classes.some(n=>n.length>60)||new Set(body.classes).size!==body.classes.length){$('#create-error').textContent='请填写名称与唯一的类别名称，每个类别不超过60字符。';return;}
    busy=true;$('#create-submit').disabled=true;toggleBusy();
    try{const r=await post('/admin/datasets',body,'create');if(session!==epoch)return;busy=false;keys.delete('create');$('#create-dialog').close();$('#create-form').reset();selectDataset(r.id);}
    catch(err){if(session===epoch)$('#create-error').textContent=err.message;}
    finally{if(session===epoch){busy=false;$('#create-submit').disabled=false;toggleBusy();}}
  };
  $('#upload-form').onsubmit=async e=>{
    e.preventDefault();if(busy||!data)return;const file=$('#video-file').files[0],session=epoch,id=did;
    if(file.size>data.limits.max_upload_bytes){error('视频超过上传大小限制。');return;}
    const capture=Object.fromEntries(['split','batch','cycle','product','camera','lighting','occlusion'].map(k=>[k,value(k).trim()]));
    if(Object.values(capture).some(v=>!v)){error('请完整填写真实采集信息，不能只包含空格。');return;}
    const budget=Number(value('frame-budget')),params=new URLSearchParams({filename:file.name,capture:JSON.stringify(capture),frame_budget:budget});
    const key=requestKey('upload:'+id,[file.name,file.size,file.lastModified,capture,budget]);
    busy=true;uploadController=new AbortController();toggleBusy();error();$('#upload-status').textContent='开始上传…';
    try{
      await W.upload('/admin/datasets/'+id+'/videos?'+params,file,n=>{if(session===epoch)$('#upload-status').textContent=n===100?'上传完成，正在登记后台任务…':`上传中 ${n}%`;},{signal:uploadController.signal,headers:{'Idempotency-Key':key}});
      if(session!==epoch)return;$('#upload-status').textContent='已入队，页面关闭后后台仍会继续抽帧。';$('#video-file').value='';
      $('#cycle').value='';keys.delete('upload:'+id);void refresh();
    }catch(err){if(session===epoch){$('#upload-status').textContent=err.message+' 可重试同一文件与配置。';void refresh();}}
    finally{if(session===epoch){busy=false;toggleBusy();}}
  };
  $('#video-list').onclick=async e=>{
    const button=e.target.closest('[data-retry],[data-remove]');if(!button||busy||!data)return;const session=epoch,id=did;
    if(button.dataset.remove&&!confirm('移除此视频及其当前标注？已经生成的训练版本会保留。'))return;
    busy=true;toggleBusy();error();
    try{if(button.dataset.retry)await W.json('/admin/datasets/'+id+'/jobs/'+button.dataset.retry+'/retry','POST',{});else await W.request('/admin/datasets/'+id+'/videos/'+button.dataset.remove,{method:'DELETE'});if(session===epoch)void refresh();}
    catch(err){if(session===epoch)error(err.message);}finally{if(session===epoch){busy=false;toggleBusy();}}
  };
  $('#export-form').oninput=coverage;
  $('#export-form').onsubmit=async e=>{
    e.preventDefault();if(busy||!data)return;const session=epoch,id=did,body={revision:data.revision,policy:policy()};busy=true;toggleBusy();error();
    try{await post('/admin/datasets/'+id+'/exports',body,'export:'+id);if(session===epoch){$('#export-status').textContent='已提交审核，请等待后台结果。';void refresh();}}
    catch(err){if(session===epoch){$('#export-status').textContent=err.message;void refresh();}}finally{if(session===epoch){busy=false;toggleBusy();}}
  };
  const canvas=$('#canvas'),ctx=canvas.getContext('2d');
  function draw(){
    if(!picture)return;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(picture,0,0,canvas.width,canvas.height);
    const all=[...boxes];if(drag)all.push({class_id:Number(value('annotation-class')),xyxy:[Math.min(drag.x,drag.toX),Math.min(drag.y,drag.toY),Math.max(drag.x,drag.toX),Math.max(drag.y,drag.toY)]});
    ctx.lineWidth=2;ctx.font='14px sans-serif';
    all.forEach((b,i)=>{const [a,y,c,d]=b.xyxy,x=a*canvas.width,top=y*canvas.height;ctx.strokeStyle=i===boxes.length?'#ffb020':'#4fcf7f';ctx.strokeRect(x,top,(c-a)*canvas.width,(d-y)*canvas.height);const label=`${i+1} ${data.classes[b.class_id]}`,w=ctx.measureText(label).width+8;ctx.fillStyle='#102719';ctx.fillRect(x,Math.max(0,top-22),w,22);ctx.fillStyle='#7aefa5';ctx.fillText(label,x+4,Math.max(16,top-6));});
    $('#box-list').innerHTML=boxes.map((b,i)=>`<li><span>${i+1}. ${E(data.classes[b.class_id])}</span><button data-box="${i}" aria-label="删除第 ${i+1} 个框">删除</button></li>`).join('');
  }
  function annotationControls(disabled){for(const el of document.querySelectorAll('#annotator button,#annotation-class'))el.disabled=disabled;$('#annotation-close').disabled=saving;}
  function openFrame(frame){
    if(!data||data.active_export){error('正在生成版本，请完成后再编辑。');return;}
    annotation={...frame,dataset:did};boxes=structuredClone(frame.annotation.boxes||[]);picture=null;dirty=false;drag=null;saving=false;const current=++imageGeneration,session=epoch;
    $('#annotation-class').replaceChildren(...data.classes.map((n,i)=>new Option(n,i)));$('#annotation-class').value=String(frame.annotation.class_id??0);
    const v=data.videos.find(v=>v.id===frame.video_id);$('#annotation-title').textContent=data.task==='detect'?'框出图中所有目标':'选择图片类别';$('#annotation-meta').textContent=`${v?.name||''} · ${splits[v?.capture.split]} · ${frame.timestamp.toFixed(2)} 秒 · ${labels[frame.review]}`;
    $('#annotation-help').textContent=data.task==='detect'?'选择类别后拖动画框，可添加多个框；框错可删除重画。':'选择与整张图片对应的类别后保存。';
    $('#save-negative').hidden=data.task!=='detect';$('#annotation-error').textContent='';$('#box-list').replaceChildren();$('#image-status').textContent='正在加载原图…';canvas.width=640;canvas.height=360;annotationControls(true);$('#annotator').showModal();
    const img=new Image();img.onload=()=>{if(current!==imageGeneration||session!==epoch)return;picture=img;const ratio=Math.min(1000,Math.max(640,img.naturalWidth))/img.naturalWidth;canvas.width=Math.round(img.naturalWidth*ratio);canvas.height=Math.round(img.naturalHeight*ratio);$('#image-status').textContent=`${img.naturalWidth} × ${img.naturalHeight} px`;annotationControls(false);draw();};img.onerror=()=>{if(current===imageGeneration)$('#image-status').textContent='图片加载失败，请关闭后重试。';};img.src=frame.image;
  }
  function closeAnnotation(){if(saving)return;if(dirty&&!confirm('放弃尚未保存的标注？'))return;$('#annotator').close();}
  $('#annotation-close').onclick=closeAnnotation;$('#annotator').oncancel=e=>{e.preventDefault();closeAnnotation();};
  $('#annotator').onclose=()=>{++imageGeneration;annotation=null;picture=null;boxes=[];dirty=false;drag=null;ctx.clearRect(0,0,canvas.width,canvas.height);};
  $('#frames').onclick=e=>{const b=e.target.closest('[data-frame]'),frame=frameItems.find(f=>f.id===b?.dataset.frame);if(frame)openFrame(frame);};
  const point=e=>{const r=canvas.getBoundingClientRect();return {x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};};
  canvas.onpointerdown=e=>{if(e.button!==0||!picture||saving||data.task!=='detect')return;const p=point(e);drag={...p,toX:p.x,toY:p.y,pointer:e.pointerId};canvas.setPointerCapture(e.pointerId);};
  canvas.onpointermove=e=>{if(!drag||drag.pointer!==e.pointerId)return;const p=point(e);drag.toX=p.x;drag.toY=p.y;draw();};
  canvas.onpointerup=e=>{if(!drag||drag.pointer!==e.pointerId)return;const p=point(e),a=Math.min(drag.x,p.x),b=Math.min(drag.y,p.y),c=Math.max(drag.x,p.x),d=Math.max(drag.y,p.y);if((c-a)*picture.naturalWidth>=2&&(d-b)*picture.naturalHeight>=2&&boxes.length<100){boxes.push({class_id:Number(value('annotation-class')),xyxy:[a,b,c,d]});dirty=true;}drag=null;draw();};
  canvas.onpointercancel=()=>{drag=null;draw();};
  $('#box-list').onclick=e=>{const b=e.target.closest('[data-box]');if(b&&!saving){boxes.splice(Number(b.dataset.box),1);dirty=true;draw();}};
  $('#annotation-class').onchange=()=>{if(data?.task==='classify')dirty=true;};
  async function save(review){
    if(!annotation||saving||!picture)return;
    if(review==='labelled'&&data.task==='detect'&&!boxes.length){$('#annotation-error').textContent='请先画出目标框；没有目标请点击“确认没有任何目标”。';return;}
    if(review!=='labelled'&&boxes.length&&!confirm('此操作会清除当前图片的目标框，是否继续？'))return;
    const session=epoch,frame=annotation,body={revision:frame.revision,review,boxes:review==='labelled'&&data.task==='detect'?boxes:[],class_id:review==='labelled'&&data.task==='classify'?Number(value('annotation-class')):null};saving=true;annotationControls(true);$('#annotation-error').textContent='';
    try{await W.json('/admin/datasets/'+frame.dataset+'/frames/'+frame.id,'PUT',body);if(session!==epoch)return;dirty=false;$('#annotator').close();void refresh();}
    catch(e){if(session===epoch)$('#annotation-error').textContent=e.message+(e.status===409?'。请关闭标注窗口，刷新并重新打开。':'');}
    finally{if(session===epoch){saving=false;annotationControls(false);}}
  }
  for(const [id,review] of Object.entries({'save-label':'labelled','save-negative':'negative','save-excluded':'excluded','save-pending':'pending'}))$('#'+id).onclick=()=>save(review);
  $('#login').onclick=()=>W.login();$('#account-button').onclick=async()=>{try{if(W.session)await W.logout();else await W.login();}catch(e){error(e.message);}};
  document.addEventListener('workspace-session',()=>{
    ++epoch;++generation;clearTimeout(timer);controller?.abort();uploadController?.abort();keys.clear();busy=false;data=null;frameItems=[];items=[];listHasMore=false;failures=0;listSignature=frameSignature='';frameOffset=datasetOffset=0;
    $('#annotator').close();$('#create-dialog').close();$('#frames').replaceChildren();$('#dataset-list').replaceChildren();$('#video-list').replaceChildren();$('#video-list').dataset.signature='';$('#export-list').replaceChildren();$('#coverage').replaceChildren();$('#dataset-title').textContent='';$('#dataset-meta').textContent='';$('#upload-form').reset();$('#create-form').reset();$('#upload-status').textContent='';$('#export-status').textContent='';$('#create-submit').disabled=false;
    $('#video-filter').replaceChildren(new Option('全部视频',''));$('#video-filter').dataset.signature='';$('#review-filter').value='all';error();$('#locked').hidden=!!W.session;$('#content').hidden=!W.session;$('#detail').hidden=true;toggleBusy();if(W.session)void refresh();
  });
  document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(document.hidden)controller?.abort();else if(W.session)void refresh();});
  window.addEventListener('beforeunload',e=>{if(busy||dirty){e.preventDefault();e.returnValue='';}});
  window.addEventListener('pagehide',()=>{clearTimeout(timer);controller?.abort();uploadController?.abort();});
  W.connect().catch(e=>error('连接失败：'+e.message));
})();
