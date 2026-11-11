/* Server-owned alert archive; clips remain available after stream release. */
window.EvidenceHistory=(()=>{
  'use strict';
  const statusNames={PENDING:'等待归档',PROCESSING:'正在归档',READY:'已保存',FAILED:'归档失败',MISSING:'缺少采样',DELETING:'正在清理',EXPIRED:'已过期'};
  const stations=Array.from({length:6},(_,i)=>({id:'station-'+(i+1),name:'工位'+(i+1)}));
  stations.push({id:'unassigned',name:'未关联工位'});
  let generation=0, next=null, rows=[], activeRequest=null, selectedStation='station-1', loading=false;
  const E=value=>window.Workspace.escape(value);
  const date=value=>value?new Date(value*1000).toLocaleString():'—';
  const size=value=>value==null?'—':(value/1024/1024).toFixed(2)+' MB';
  const stationName=()=>stations.find(station=>station.id===selectedStation).name;
  const current=(version,host)=>version===generation&&host===document.querySelector('#evidence-history')&&document.querySelector('#modal').open;
  function table(){
    const host=document.querySelector('#evidence-history');
    if(!host)return;
    host.setAttribute('aria-busy',String(loading));
    host.innerHTML=`<div class="evidence-toolbar"><label for="evidence-station">查看工位<select id="evidence-station">${stations.map(station=>`<option value="${station.id}" ${station.id===selectedStation?'selected':''}>${E(station.name)}</option>`).join('')}</select></label><p id="evidence-scope" role="status">${E(stationName())}的告警录像 · ${loading?'正在读取…':'已显示 '+rows.length+' 条'}</p></div><p class="hint">${selectedStation==='unassigned'?'这些录像未记录有效工位，单独保留供查阅。':'仅显示所选工位的告警，可切换工位查看。'}实时告警包含告警前最多5秒采样画面；成功归档后至少保留30天。人工检查截图在本件记录中查看。</p><div class="table-wrap"><table><thead><tr><th>工位</th><th>告警时间</th><th>检测</th><th>原因</th><th>录像</th><th>保留至</th><th>操作</th></tr></thead><tbody>${rows.map(row=>`<tr><td class="evidence-station-name">${E(row.station_name)}</td><td class="evidence-date">${E(date(row.created_at))}</td><td>${E({screw:'螺丝',sticker:'贴条',scene:'场景',pose:'人体'}[row.analysis_type]||row.analysis_type)}</td><td class="evidence-reason">${E(row.reason)}</td><td>${E(statusNames[row.status]||row.status)}${row.status==='READY'?'<br>'+E(size(row.size_bytes)):''}${row.error?'<br>'+E(row.error):''}</td><td class="evidence-date">${E(date(row.retain_until))}</td><td>${row.video_url?`<button class="tiny-button" data-evidence-play="${E(row.id)}">播放录像</button>`:''}${row.status==='FAILED'?`<button class="tiny-button" data-evidence-retry="${E(row.id)}">重试归档</button>`:''}</td></tr>`).join('')}</tbody></table></div>${rows.length||loading?'':`<p>${E(stationName())}暂无已记录的告警。</p>`}<div class="modal-actions"><button class="btn" id="evidence-refresh">刷新</button>${next?'<button class="btn" id="evidence-more">加载更多</button>':''}</div><p id="evidence-error" role="alert"></p>`;
    host.querySelector('#evidence-station').onchange=event=>{selectedStation=event.target.value;load(true);};
    host.querySelectorAll('button').forEach(button=>button.disabled=loading);
    host.querySelector('#evidence-refresh').onclick=()=>load(true);
    host.querySelector('#evidence-more')?.addEventListener('click',()=>load(false));
    host.querySelectorAll('[data-evidence-play]').forEach(button=>button.onclick=()=>play(button));
    host.querySelectorAll('[data-evidence-retry]').forEach(button=>button.onclick=async()=>{
      const version=generation;button.disabled=true;
      try{await activeRequest('/alerts/'+encodeURIComponent(button.dataset.evidenceRetry)+'/retry',{method:'POST'});if(current(version,host))await load(true);}
      catch(error){if(current(version,host)){host.querySelector('#evidence-error').textContent=error.message;button.disabled=false;}}
    });
  }
  async function load(reset){
    const version=++generation;
    const host=document.querySelector('#evidence-history');
    if(!host)return;
    if(reset){rows=[];next=null;}
    loading=true;table();
    try{
      const page=await activeRequest('/alerts?limit=30&station_id='+encodeURIComponent(selectedStation)+(!reset&&next?'&cursor='+encodeURIComponent(next):''));
      if(!current(version,host))return;
      rows=reset?page.items:[...rows,...page.items];next=page.next_cursor;loading=false;table();
    }catch(error){if(current(version,host)){loading=false;table();host.querySelector('#evidence-error').textContent=error.message;}}
  }
  async function play(button){
    const version=generation,host=document.querySelector('#evidence-history');button.disabled=true;
    try{
      const row=await activeRequest('/alerts/'+encodeURIComponent(button.dataset.evidencePlay));
      if(!current(version,host))return;
      if(!row.video_url){button.disabled=false;host.querySelector('#evidence-error').textContent='录像当前不可播放，请刷新列表。';return;}
      generation++;
      window.showModal(row.station_name+' · 告警取证录像',`<div class="evidence-playback"><button class="btn" id="evidence-back">返回${E(stationName())}录像列表</button><video controls playsinline preload="metadata" src="${E(row.video_url)}"></video><p>${E(row.reason)}</p><p class="hint">${row.details?.capture_mode==='sampled-jpeg'?'检测采样录像':'离线分析录像'} · 保留至 ${E(date(row.retain_until))}</p><p class="hint" style="overflow-wrap:anywhere">SHA-256：${E(row.sha256)}</p></div>`);
      document.querySelector('#evidence-back').onclick=showList;
    }catch(error){if(current(version,host)){host.querySelector('#evidence-error').textContent=error.message;button.disabled=false;}}
  }
  function showList(){
    generation++;
    window.showModal('告警取证录像 · 按工位查看','<div id="evidence-history"></div>');table();
  }
  function open(request,{stationId}={}){
    activeRequest=request;rows=[];next=null;
    if(stations.some(station=>station.id===stationId))selectedStation=stationId;
    showList();load(true);
  }
  return {open};
})();
