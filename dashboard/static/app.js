'use strict';
const $ = id => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const timeFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const hourFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',hour:'2-digit',hour12:false});
const format = ts => ts ? dateFormat.format(new Date(ts*1000)) : '—';
const formatTime = ts => ts ? timeFormat.format(new Date(ts*1000)) : '—';
const number = n => Number(n || 0).toLocaleString('zh-CN');
let snapshot = null, serverOffset = 0, filter = 'all', history = [], hasMore = false, expanded = false, refreshing = false;
const states = {
  waiting: {badge:'自动重试中',title:'正在等待目标区域的空闲容量',description:'最近一次申请返回容量不足。任务会等待片刻，再自动尝试。'},
  checking: {badge:'核查资源中',title:'正在为下一次申请做准备',description:'检查已有实例和当前资源，确认后继续提交申请。'},
  requesting: {badge:'正在申请',title:'申请已经发出，正在等待结果',description:'Oracle 正在处理这次请求，返回结果后会自动更新。'},
  success: {badge:'已获得实例',title:'抢到了，这台 A1 已经就位。',description:'已确认获得实例，任务已停止继续创建。你可以查看下方的机器信息。'},
  paused: {badge:'任务已停止',title:'自动申请目前没有运行',description:'已有过程记录会继续保留。任务恢复后，页面会自动同步。'},
  error: {badge:'需要检查',title:'任务遇到异常，已停止申请',description:'请查看最近进展。权限、配额或配置问题需要处理后再继续。'}
};
function element(tag, className, text) {const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
async function getJSON(url) {const r=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(12000)});if(!r.ok)throw new Error('Request failed');return r.json();}
function showWarning(message) {$('connection-warning').textContent=message||'';$('connection-warning').hidden=!message;}
function render(data) {
  snapshot=data;serverOffset=data.server_time*1000-Date.now();
  const state=states[data.phase] || states.checking;
  $('hero').className='hero panel '+data.phase;
  $('status-pill').lastElementChild.textContent=state.badge;
  $('status-title').textContent=state.title.replace('目标区域',data.target.region_label);
  $('region-caption').replaceChildren(element('span','region-dot'),document.createTextNode(data.target.region.toUpperCase()+' · AMPERE A1'));
  $('result-name').textContent=data.target.name;
  $('status-description').textContent=state.description;
  $('last-attempt-time').textContent=data.last_attempt ? `${format(data.last_attempt.started_at)} · 第 ${number(data.last_attempt.number)} 次` : '等待首次申请';
  $('started-at').textContent=format(data.first_attempt_at);
  $('total-attempts').textContent=number(data.total_attempts);
  $('today-attempts').textContent=number(data.today_attempts);
  $('capacity-count').textContent=number(data.counts.capacity);
  $('success-count').textContent=data.instance_id?'1':'0';
  const days=Math.max(0,(data.server_time-(data.first_attempt_at||data.server_time))/86400);
  $('elapsed').textContent=days>=1?`已持续 ${Math.floor(days)} 天 ${Math.floor(days%1*24)} 小时`:`已持续 ${Math.floor(days*24)} 小时 ${Math.floor(days*1440%60)} 分钟`;
  $('today-label').textContent=`${new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric'}).format(new Date(data.server_time*1000))} · 北京时间`;
  $('track-request').className='track-step '+(data.phase==='requesting'?'current':'complete');
  $('track-result').className='track-step '+(data.phase==='success'?'complete':data.phase==='waiting'?'current':'');
  $('track-result-title').textContent=data.phase==='success'?'已获得实例':'等待空闲容量';
  $('track-result-sub').textContent=data.phase==='success'?'自动申请已结束':'成功后自动结束';
  $('result-panel').hidden=!data.instance_id;
  $('success-hint').textContent=data.instance_id?'任务已停止继续创建':'获得一台后停止申请';
  if(data.instance_id){$('instance-id').textContent=data.instance_id;$('success-time').textContent=format(data.success_at);}
  $('sync-state').className='sync-state';
  $('sync-state').lastChild.textContent='已同步 '+formatTime(data.collected_at);
  $('footer-time').textContent=`每 5 秒刷新 · 最近同步 ${formatTime(data.collected_at)} · 北京时间`;
  showWarning(data.data_warning);
  renderChart(data.hourly);
  const events=$('events');events.replaceChildren();
  if(!data.events.length)events.append(element('div','empty','暂无任务事件'));
  for(const event of data.events){const row=element('div','event '+event.kind);row.append(element('i','event-dot'));const content=element('div','event-content');content.append(element('p','',event.message));row.append(content,element('time','',formatTime(event.ts)));events.append(row);}
  updateCountdown();
}
function renderChart(buckets){
  const chart=$('chart');chart.replaceChildren();const max=Math.max(1,...buckets.map(b=>b.count));
  buckets.forEach((bucket,index)=>{const bar=element('div','chart-bar'+(index===23?' latest':''));bar.style.height=Math.max(1,bucket.count/max*91)+'%';bar.append(element('span','',`${format(bucket.at).slice(0,11)} · ${bucket.count} 次`));chart.append(bar);});
  $('chart-axis').replaceChildren(...[0,6,12,18,23].map(i=>element('span','',hourFormat.format(new Date(buckets[i].at*1000)).replace(/\D/g,'').padStart(2,'0')+':00')));
  $('chart-total').textContent=`${number(buckets.reduce((sum,b)=>sum+b.count,0))} 次 / 24h`;
}
function updateCountdown(){
  if(!snapshot)return;
  const now=(Date.now()+serverOffset)/1000, phase=snapshot.phase;
  if(phase==='success'){$('countdown').textContent='✓';$('countdown-label').textContent='申请结束';$('countdown-sub').textContent='实例已获得';$('countdown-ring').style.setProperty('--progress',100);return;}
  if(phase==='paused'||phase==='error'){$('countdown').textContent='—';$('countdown-label').textContent='下一次尝试';$('countdown-sub').textContent='任务当前已停止';$('countdown-ring').style.setProperty('--progress',0);return;}
  if(phase==='waiting'&&snapshot.next_attempt_at>now){const seconds=Math.max(0,Math.ceil(snapshot.next_attempt_at-now));$('countdown').textContent=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;$('countdown-label').textContent='下次尝试';$('countdown-sub').textContent='倒计时结束后自动核查';$('countdown-ring').style.setProperty('--progress',Math.min(100,Math.max(0,(1-seconds/(snapshot.wait_seconds||330))*100)));}
  else{$('countdown').textContent='进行中';$('countdown-label').textContent=phase==='requesting'?'提交申请':'准备申请';$('countdown-sub').textContent='等待云端返回';$('countdown-ring').style.setProperty('--progress',95);}
}
const resultNames={capacity:'容量不足',success:'创建成功',transient:'临时错误',error:'异常停止',running:'申请中'};
function renderHistory(){
  const body=$('history-body');body.replaceChildren();
  if(!history.length){const tr=element('tr');const td=element('td','empty',filter==='all'?'暂无申请记录':'这个筛选条件下暂无记录');td.colSpan=5;tr.append(td);body.append(tr);}
  for(const row of history){const tr=element('tr');const status=element('td');status.append(element('span','row-status '+row.status,resultNames[row.status]||'等待更新'));const duration=row.finished_at?`${Math.max(0,row.finished_at-row.started_at).toFixed(1)} 秒`:'—';const domain=(row.ad||'—').replace(/^.*:/,'');tr.append(element('td','',`# ${number(row.number)}`),element('td','',format(row.started_at)),status,element('td','',duration),element('td','',domain));body.append(tr);}
  $('history-count').textContent=`已显示 ${number(history.length)} 条`;
  $('load-more').hidden=!hasMore;
  $('history-note').textContent=expanded?'正在回看历史记录 · 最新进展仍会同步':'真实任务记录 · 自动同步';
}
async function loadHistory(more=false){
  const before=more&&history.length?`&before=${history[history.length-1].number}`:'';
  const data=await getJSON(`/api/attempts?limit=40&status=${filter}${before}`);
  if(more){const seen=new Set(history.map(r=>r.number));history.push(...data.items.filter(r=>!seen.has(r.number)));expanded=true;}else history=data.items;
  hasMore=data.has_more;renderHistory();
}
async function refresh(){
  if(refreshing)return;refreshing=true;$('refresh').disabled=true;
  try{const data=await getJSON('/api/status');render(data);if(!expanded)await loadHistory();}
  catch(error){showWarning('暂时连不上状态页。正在显示最近数据，连接恢复后会自动更新。');$('sync-state').className='sync-state offline';$('sync-state').lastChild.textContent='连接中断';}
  finally{refreshing=false;$('refresh').disabled=false;}
}
$('refresh').addEventListener('click',()=>{expanded=false;refresh();});
for(const button of document.querySelectorAll('[data-filter]'))button.addEventListener('click',async()=>{filter=button.dataset.filter;expanded=false;for(const b of document.querySelectorAll('[data-filter]')){b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));}try{await loadHistory();}catch{showWarning('记录读取失败，请稍后刷新。');}});
$('load-more').addEventListener('click',async()=>{const button=$('load-more');button.disabled=true;try{await loadHistory(true);}catch{showWarning('暂时无法加载更早记录，请稍后重试。');}finally{button.disabled=false;}});
$('copy-id').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(snapshot.instance_id);$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,2000);}catch{showWarning('自动复制未完成，可以直接选中实例 OCID 复制。');}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},5000);setInterval(updateCountdown,1000);refresh();
