'use strict';
const byId = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const roleName = {exporter:'出口商', importer:'进口商', factory:'工厂'};
const stages = {quoted:'待执行报价',contracted:'外贸合同已确认',supplied:'国内已交货',shipped:'已发运交单',completed:'已结案'};
const pages = {overview:['交易总览','查看角色余额、等级和学校平台的业务待办。'],trade:['新建交易','先测算各角色的预计利润和现金占用，再提交整批订单。'],archive:['合同存档','查看本机保存的合同，继续办理中断的交易。'],settings:['连接设置','管理本次学校账号会话和可选模型服务。']};
const pageId = () => Object.hasOwn(pages, location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
const chip = (val, style='gray') => '<span class="tag '+style+'">'+esc(val)+'</span>';
const money = (n) => n === null || n === undefined || n === '' || !Number.isFinite(Number(n)) ? '—' : Number(n).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const num = (n) => Number.isFinite(Number(n)) ? Number(n).toLocaleString('zh-CN') : '—';
const option = (value, label, select) => '<option value="'+esc(value)+'"'+(value===select?' selected':'')+'>'+esc(label)+'</option>';
const panel = (title, hint, body) => '<section class="panel"><div class="section-head"><div><h2>'+esc(title)+'</h2>'+(hint?'<p>'+esc(hint)+'</p>':'')+'</div></div>'+body+'</section>';
const empty = (title, hint, action='') => '<div class="empty"><h3>'+esc(title)+'</h3><p>'+esc(hint)+'</p>'+action+'</div>';
const linkButton = (dest, label) => '<a class="btn secondary" href="#'+dest+'">'+esc(label)+'</a>';
let latestSync='', taskSignature='';
let state=null, page='', frame='', image='', runningRequest=false, seq=0, archiveFilter='', previewEdited=false, previewBefore='', schedule;
const busy = () => runningRequest || state?.task?.status === 'running';
function feedback(msg='',bad=false){const node=byId('feedback');node.hidden=!msg;node.className='feedback'+(bad?' error':'');node.textContent=msg;}
function updateControls(){document.querySelectorAll('[data-lock]').forEach(el=>{el.disabled=busy()});document.querySelectorAll('#quote input,#quote select').forEach(el=>{el.disabled=busy()});}
async function request(path, body){
  const req=body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state?.token||''},body:JSON.stringify(body)};
  const resp=await fetch('./api/'+path,req); let out;
  try{out=await resp.json()}catch{throw new Error('本地服务返回了无法读取的响应。')}
  if(!resp.ok)throw new Error(out.error||'本次请求失败。');return out;
}
async function refresh(){
  const current=++seq; const fresh=await request('state'); if(current!==seq)return;
  if(!fresh||!fresh.token)throw new Error('本地会话尚未建立。请重新打开工作台。');
  if(fresh.preview?.id!==previewBefore){previewBefore=fresh.preview?.id||'';previewEdited=false;}
  if(state?.accounts?.join('|')!==fresh.accounts?.join('|'))latestSync='';
  if(fresh.synced_at)latestSync=fresh.synced_at;
  else if(['login','refresh'].includes(fresh.task?.kind)&&fresh.task.status==='done')latestSync=fresh.task.finished_at||latestSync;
  state=fresh; render(); clearTimeout(schedule); schedule=setTimeout(poll,busy()||state.agent?.busy?700:4000);
}
async function poll(){if(runningRequest){schedule=setTimeout(poll,900);return;}try{await refresh();}catch(e){byId('connection').textContent='本地服务未连接';byId('connection').className='connection';feedback('无法读取本地工作台。请确认启动窗口仍在运行。',true);schedule=setTimeout(poll,4000);}}
async function mutate(path, payload, message){
  if(busy())return false; runningRequest=true; clearTimeout(schedule); seq++; updateControls(); feedback(path==='refresh'?'':message||'正在提交…');
  try{await request(path,payload);await refresh();feedback();return true;}
  catch(err){feedback(err.message,true);return false;}
  finally{runningRequest=false;updateControls();clearTimeout(schedule);schedule=setTimeout(poll,state?.task?.status==='running'||state?.agent?.busy?700:4000);}
}
function sessionCard(){
  return '<section class="panel"><div class="section-head"><div><h2>连接实训账号</h2><p>输入本次参与业务的 1–3 个账号。密码仅在本次运行中使用。</p></div></div><form id="login" autocomplete="off"><div id="account-list"></div><div class="accounts-control"><button type="button" class="btn secondary small" id="add-account">＋ 添加账号</button></div><details class="server-box"'+(state.base?'':' open')+'><summary>学校平台地址</summary><label class="field"><span>服务地址</span><input type="url" name="base" required value="'+esc(state.base)+'" placeholder="https://school.example/SIMtrade/" autocomplete="off"></label></details><div class="form-foot"><span class="hint">请使用能访问学校平台的网络。</span><button class="btn primary" data-lock>连接账号</button></div></form></section>';
}
function addAccount(){
  const list=byId('account-list');if(!list||list.children.length>=3)return;
  const div=document.createElement('div');div.className='account-row';
  div.innerHTML='<label class="field"><span class="account-label">学生账号</span><input name="account" type="text" placeholder="输入账号" maxlength="40" required autocomplete="off" autocapitalize="none" spellcheck="false"></label><label class="field"><span>密码</span><input name="password" type="password" placeholder="无密码可留空" autocomplete="off"></label><button type="button" class="icon-btn remove" aria-label="移除这一组账号">×</button>';
  list.appendChild(div);syncAccounts();
}
function syncAccounts(){const rows=[...document.querySelectorAll('.account-row')];rows.forEach((r,i)=>{r.querySelector('.account-label').textContent=rows.length===1?'学生账号':'学生账号 '+(i+1);r.querySelector('.remove').hidden=rows.length===1;});byId('add-account').disabled=rows.length>=3;}
function loginView(){return '<div class="grid split">'+sessionCard()+'<aside class="panel dark"><h2>本次交易如何执行</h2><ol class="workflow"><li><i>01</i><div><strong>连接商业角色</strong><small>同一账号可分别作为出口商、进口商和工厂。</small></div></li><li><i>02</i><div><strong>核对报价与利润</strong><small>从学校读取行情，按各角色本币计算预算。</small></div></li><li><i>03</i><div><strong>由终端履约</strong><small>确认后依次办理签约、采购、发运和收付。</small></div></li></ol><div class="facts">本机保留存档 · 中断可续办</div></aside></div>';}
function header(){
  const title=pages[page];document.title=title[0]+' · SIMtrade 工作台';
  byId('heading').textContent=title[0];byId('subtitle').textContent=title[1];
  document.querySelectorAll('.nav a').forEach(n=>{const on=n.dataset.page===page;n.classList.toggle('active',on);if(on)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});
  byId('head-action').innerHTML=page==='overview'&&state.accounts?.length?'<a href="#trade" class="btn primary">＋ 新建交易</a>':'';
  const conn=byId('connection'), n=state.accounts?.length||0;conn.className='connection'+(n?' connected':'');conn.textContent=n?'已连接 '+n+' 个账号':'本地服务已连接';
}
function skeleton(){
  if(page==='settings')return settingsForm();
  if(!state.accounts?.length)return loginView()+'<div id="live-data" class="space"></div>';
  if(page==='trade')return tradeForm()+'<div id="live-data" class="space"></div>';
  return '<div id="live-data"></div>';
}
function render(){
  const route=pageId(), key=JSON.stringify([route,state.accounts,state.base,state.routes,state.products]);
  if(page!==route)feedback();
  page=route;header();
  if(frame!==key){frame=key;image='';archiveFilter='';byId('view').innerHTML=skeleton();if(byId('login'))addAccount();if(byId('quote')){planFields();routeHint();}}
  const dynamic=byId('live-data');
  if(dynamic){
    const signature=JSON.stringify([state.task,state.preview,state.jobs,state.role_rows,archiveFilter,previewEdited,page]);
    if(signature!==image){image=signature;const open=[...dynamic.querySelectorAll('details[open][data-key]')].map(x=>x.dataset.key);const focused=document.activeElement;const focusId=dynamic.contains(focused)?focused.id:'';dynamic.innerHTML=dynamicView();dynamic.querySelectorAll('details[data-key]').forEach(el=>{el.open=open.includes(el.dataset.key)});if(focusId)byId(focusId)?.focus({preventScroll:true});}
  }
  if(page==='settings'&&byId('model-status'))byId('model-status').textContent=state.model_enabled?'当前已启用':'当前已关闭';
  renderTask();window.workbenchChat.render(state);updateControls();
}
function dynamicView(){
  if(!state.accounts?.length)return '';
  if(page==='overview')return overview();
  if(page==='trade')return previewCard();
  if(page==='archive')return archives();
  return '';
}
function renderTask(){
  const host=byId('task-status'),t=state.task;
  const owner={start:'trade',preview:'trade',resume:'archive'}[t?.kind];
  const hidden=!t||(['refresh','login'].includes(t.kind)&&t.status!=='error'&&state.accounts?.length)
    ||(t.status==='done'&&['login','refresh','preview'].includes(t.kind))
    ||(owner && owner!==page && t.status!=='error');
  const signature=hidden?'':JSON.stringify(t);
  if(signature===taskSignature)return;
  taskSignature=signature;
  if(hidden){host.replaceChildren();return;}
  const kinds={login:'连接账号',refresh:'同步资料',preview:'生成报价',start:'执行交易',resume:'续办合同'};
  const label=kinds[t.kind]||'工作台任务';
  const done=t.status==='done',error=t.status==='error';
  const wasOpen=host.querySelector('details')?.open;
  const meta=error?'需要处理':done?'已完成':(t.total?t.done+' / '+t.total:'执行中');
  const logs=t.logs?.length?'<pre class="log">'+esc(t.logs.join('\n'))+'</pre>':'';
  host.innerHTML='<details class="task-strip '+(error?'task-error':'')+'" '+(wasOpen?'open':'')+'><summary><b>'+esc(label)+'</b> <span>'+esc(meta)+'</span></summary>'+(t.message?'<p>'+esc(t.message)+'</p>':'')+logs+'</details>';
  if(error)host.querySelector('details').open=true;
}
function syncControl(){
  const t=state.task;
  const syncing=t?.kind==='refresh'&&t.status==='running';
  const date=latestSync?new Date(latestSync):null;
  const time=date&&Number.isFinite(date.getTime())?date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'';
  const caption=syncing?'正在同步 '+t.done+'/'+t.total:time?'上次同步 '+time:'';
  return '<div class="sync-actions"><span class="sync-caption" role="status">'+esc(caption)+'</span><button class="btn secondary small" data-action="refresh" data-lock>同步资料</button></div>';
}
function overview(){
  const rows=state.role_rows||[];
  const data=rows.length?'<div class="tablebox"><table><thead><tr><th>账号</th><th>角色</th><th>等级</th><th class="num">可用余额</th><th class="num">贷款</th><th class="num">库存账面</th></tr></thead><tbody>'+rows.map(r=>'<tr><td class="code">'+esc(r.account)+'</td><td>'+esc(roleName[r.role]||r.role)+'</td><td>'+chip('Lv. '+r.level,'good')+'</td><td class="num">'+esc(r.currency)+' '+money(r.cash)+'</td><td class="num">'+money(r.loan)+'</td><td class="num">'+money(r.stock)+'</td></tr>').join('')+'</tbody></table></div>':empty('尚未取得角色资料','');
  const todo=rows.filter(r=>r.todo?.length);
  return '<section class="panel"><div class="section-head"><div><h2>角色账本</h2><p>金额按各角色的本币列示。</p></div>'+syncControl()+'</div>'+data+'</section>'+panel('平台待办','',todo.length?'<ul class="compact-rows">'+todo.map(r=>'<li><strong>'+esc(r.account)+' · '+esc(roleName[r.role]||r.role)+'</strong>'+r.todo.map(t=>'<p class="textwrap">'+esc(t)+'</p>').join('')+'</li>').join('')+'</ul>':empty(rows.length?'已读取的角色暂无待办':'等待读取平台待办','同步资料后可在这里查看学校给出的提示。'));
}
function focusValue(account,role){
  return role==='exporter'?account+'|exporter':account;
}
function tradeForm(){
  const focusOptions=state.accounts.flatMap(a=>['importer','exporter'].map(role=>
    option(focusValue(a,role),'优先 '+a+' 的'+(role==='exporter'?'出口':'进口')+'业务'))).join('');
  const productOptions=(state.products||[]).map(p=>option(p.id,p.name+' · '+p.id)).join('');
  const fields='<div class="fields">'+
    '<label class="field"><span>贸易术语</span><select name="terms" required>'+routeOptionList(termList,'ROTATE','自动轮换贸易术语')+'</select></label>'+
    '<label class="field"><span>结算方式</span><select name="payment" required>'+routeOptionList(paymentList,'AUTO','自动轮换结算方式')+'</select></label>'+
    '<label class="field"><span>角色分配</span><select name="focus">'+option('','均衡轮换')+focusOptions+'</select></label>'+
    '<label class="field"><span>起始商品</span><select name="product" required>'+productOptions+'</select></label>'+
    '<label class="field"><span>订单数（1–12）</span><input name="count" type="number" min="1" max="12" step="1" value="'+esc(state.accounts.length)+'" required></label>'+
    '<label class="field"><span>每笔最多箱数</span><input name="cartons" type="number" min="1" max="100" step="1" value="3" required></label>'+
    '<label class="field"><span>最低现金保留比例（%）</span><input name="floor" type="number" min="0" max="100" step="1" value="70" required></label>'+
    '<label class="field wide"><span>英文目的港（选填）</span><input name="port" maxlength="60" placeholder="留空时按进口商所在国家匹配"></label></div>';
  const plans='<div class="space"><label class="check"><input type="checkbox" name="custom">为每笔订单单独指定搭配</label>'+
    '<div id="custom-box" hidden><div id="plan-lines"></div></div><p id="route-hint" class="note space"></p></div>';
  const submit='<div class="form-foot"><label class="check"><input type="checkbox" name="closing_mail">每单结案后给本单进口商发结款函</label><button class="btn primary" data-lock>生成报价</button></div>';
  return panel('报价参数','资金保留比例对三个角色分别生效。','<form id="quote">'+fields+plans+submit+'</form>');
}
function previewCard(){
  const p=state.preview;if(!p)return '';
  const orders=p.orders||[];
  return panel('订单预览','以下利润均为预计金额，实际收支可在合同结案后核对。',orders.map(o=>'<article class="order"><div class="order-head"><h3>'+esc(o.product)+' × '+num(o.quantity)+' <small class="quiet">（'+esc(o.cartons)+' 箱）</small></h3>'+chip(o.terms+' · '+o.payment+' · '+o.port,'gray')+statusChip(o.terms,o.payment,o.status)+'</div><p class="hint code">'+esc(o.contract)+'</p><p class="hint">出口商 '+esc(o.seller)+' → 进口商 '+esc(o.buyer)+'，供货工厂 '+esc(o.factory)+'</p><p class="hint">外销单价 USD '+money(o.foreign_unit)+'，总额 USD '+money(o.foreign_total)+'。内采单价 RMB '+money(o.domestic_unit)+'。</p><div class="profits"><span>出口利润 RMB '+money(o.export_profit)+'</span><span>进口利润 '+esc(o.currency)+' '+money(o.import_profit)+'</span><span>工厂利润 RMB '+money(o.factory_profit)+'</span></div>'+(o.floors?'<p class="fine">保留现金：出口 RMB '+money(o.floors.exporter)+'，进口 '+esc(o.currency)+' '+money(o.floors.importer)+'，工厂 RMB '+money(o.floors.factory)+'</p>':'')+'</article>').join('')+'<div class="form-foot">'+(previewEdited?'<p class="hint">报价参数已修改，请重新生成报价。</p>':'<span class="hint">共 '+orders.length+' 笔，将逐笔签约并办理。</span>')+(!previewEdited&&orders.length?'<button type="button" class="btn primary" data-action="start" data-lock>确认并执行…</button>':'')+'</div>');
}

const termList=['CIF','CFR','FOB'], paymentList=['T/T','D/P','D/A','L/C'];
const routeStatus = {'verified':'整单已实测','assisted':'需人工协作','integrated':'已接入分支'};
const termGuide={CIF:'出口商安排订舱和投保，凭保单及提单交单。',CFR:'出口商订舱并支付海运费，进口商根据合同办理投保。',FOB:'进口商先在平台指定承运人并安排运输，出口商按约交货。'};
const paymentGuide={'L/C':'进口商申请开证，开证行开证、通知行通知。出口商审证后装运并向银行交单，进口商依据信用证办理付款取单。','D/P':'出口商发运后经银行办理托收，进口商收到通知后付款赎单，再办理换单、提货。','D/A':'出口商发运后经银行托收。进口商承兑远期汇票后取单、提货，到期按约付款，双方核对到账。','T/T':'出口商按约发运并交付单据，进口商按合同约定电汇。双方逐一核对本单收、付款流水。'};
function routeInfo(t,p){return (state.routes||[]).find(r=>r.terms===t&&r.payment===p);}
function statusLabel(t,p,mode){const r=routeInfo(t,p);return routeStatus[mode||r?.status]||(r?.automatic?'整单已实测':'需人工协作');}
function statusChip(t,p,mode){const label=statusLabel(t,p,mode);return chip(label,label==='整单已实测'?'good':label==='需人工协作'?'warn':'gray');}
function routeOptionList(list,auto,label){return (auto?option(auto,label):'')+list.map(x=>option(x,x)).join('');}
function planFields(){
  const form=byId('quote');if(!form)return;
  const n=Math.max(1,Math.min(12,parseInt(form.count.value,10)||1)), node=byId('plan-lines');
  const saved=[...node.children].map(row=>[row.querySelector('.line-terms')?.value,row.querySelector('.line-payment')?.value]);
  node.innerHTML=Array.from({length:n},(_,i)=>'<div class="plan-line"><span>第 '+(i+1)+' 笔</span><select class="filter line-terms" aria-label="第 '+(i+1)+' 笔贸易术语">'+termList.map(x=>option(x,x,saved[i]?.[0]||(termList.includes(form.terms.value)?form.terms.value:termList[i%3]))).join('')+'</select><select class="filter line-payment" aria-label="第 '+(i+1)+' 笔结算方式">'+paymentList.map(x=>option(x,x,saved[i]?.[1]||(paymentList.includes(form.payment.value)?form.payment.value:paymentList[i%4]))).join('')+'</select></div>').join('');
}
function routeHint(){const f=byId('quote');if(!f)return;
  const t=f.terms.value,p=f.payment.value, node=byId('route-hint');
  byId('custom-box').hidden=!f.custom.checked;
  if(f.custom.checked){node.textContent='本批使用逐笔搭配，请核对每一行。';return;}
  if(t==='ROTATE'||p==='AUTO'){node.textContent='自动项由工作台在可用条款中安排，每笔最终组合显示在报价预览。';return;}
  node.innerHTML=statusChip(t,p)+' '+esc(termGuide[t]||'')+esc(paymentGuide[p]||'');
}
function archives(){const jobs=state.jobs||[];const shown=jobs.filter(j=>!archiveFilter||(archiveFilter==='open'?j.stage!=='completed':j.stage==='completed'));
  return panel('本机合同','仅列出与当前学校及账号组合相符的存档。','<label class="filters"><span class="quiet">合同状态</span><select class="filter" id="archive-filter">'+[['','全部状态'],['open','待办或执行中'],['done','已结案']].map(o=>option(o[0],o[1],archiveFilter)).join('')+'</select></label>'+(shown.length?'<div class="tablebox"><table><thead><tr><th>合同号</th><th>商品</th><th>条款</th><th>进度</th><th>操作</th></tr></thead><tbody>'+shown.map(j=>'<tr><td class="code">'+esc(j.contract)+'</td><td>'+esc(j.product||'—')+'</td><td>'+esc(j.terms&&j.payment?j.terms+' ＋ '+j.payment:j.kind==='purchase'?'国内采购':'—')+'</td><td>'+chip(stages[j.stage]||j.stage,j.stage==='completed'?'good':'gray')+(j.resume_hint?'<p class="hint">'+esc(j.resume_hint)+'</p>':'')+'</td><td>'+(j.stage!=='completed'?'<button type="button" class="btn secondary small" data-lock data-action="resume" data-contract="'+esc(j.contract)+'">续办</button>':'已完成')+'</td></tr>').join('')+'</tbody></table></div>':empty('当前筛选下没有合同','新交易提交后将在本机保存办理进度。',linkButton('trade','新建交易'))));}

function settingsForm(){
  const accounts=state.accounts||[];
  const school=accounts.length
    ? '<p class="code textwrap">'+esc(state.base)+'</p><div class="taglist space">'+accounts.map(x=>chip(x,'good')).join('')+'</div><div class="form-foot"><span class="hint">退出后可用相同账号续办原合同。</span><button class="btn secondary" data-action="logout" data-lock>退出账号</button></div>'
    : '<p class="hint">连接学校账号后，可以查询角色账本和办理交易。</p><div class="space">'+linkButton('overview','连接账号')+'</div>';
  const model='<form id="model" autocomplete="off"><div class="fields"><label class="field wide"><span>完整 Chat Completions 地址</span><input name="endpoint" type="url" placeholder="https://…/v1/chat/completions" required autocomplete="off"></label><label class="field"><span>模型名</span><input name="model" required autocomplete="off"></label><label class="field"><span>API Key</span><input name="key" type="password" autocomplete="off"></label></div><div class="form-foot"><span class="hint" id="model-status"></span><div class="btn-row"><button class="btn secondary" type="button" data-action="model-off" data-lock>关闭模型</button><button class="btn primary" data-lock>启用模型</button></div></div></form>';
  return '<div class="grid split">'+panel('Agent 模型','接入兼容服务后，Agent 可以理解自由提问并选择工作台工具。密钥仅用于本次运行。',model)+panel('学校账号','',school)+'</div>';
}

let onConfirm=null, confirmReturn=null;
function ask(title, content, button, fn){const d=byId('dialog');confirmReturn=document.activeElement;byId('dialog-title').textContent=title;byId('dialog-content').innerHTML=content;byId('dialog-confirm').textContent=button;onConfirm=fn;d.showModal();byId('dialog-cancel').focus();}
function closeDialog(){const d=byId('dialog');if(d.open)d.close();onConfirm=null;}
byId('dialog-cancel').addEventListener('click',closeDialog);
byId('dialog-close').addEventListener('click',closeDialog);
byId('dialog').addEventListener('close',()=>{confirmReturn?.focus?.({preventScroll:true});onConfirm=null;});
byId('dialog-confirm').addEventListener('click',()=>{const fn=onConfirm;closeDialog();if(fn)fn();});

document.addEventListener('submit',async event=>{
  const f=event.target;if(!['login','quote','model'].includes(f.id))return;
  event.preventDefault();if(busy()||!f.checkValidity())return;
  const val=n=>f.elements.namedItem(n).value.trim();
  if(f.id==='login'){
    const ac=[...f.querySelectorAll('.account-row')].map(r=>({id:r.querySelector('[name=account]').value.trim(),password:r.querySelector('[name=password]').value}));
    if(ac.some(x=>!x.id)||new Set(ac.map(x=>x.id)).size!==ac.length){feedback('请输入 1–3 个不同的账号。',true);return;}
    const base=val('base');f.querySelectorAll('input[type=password]').forEach(el=>{el.value=''});await mutate('login',{base,accounts:ac},'正在登录学校平台…');
  }else if(f.id==='quote'){
    const [focus,focus_role='importer']=val('focus').split('|');
    const job={terms:val('terms'),payment:val('payment'),focus,focus_role,product:val('product'),count:Number(val('count')),cartons:Number(val('cartons')),floor:Number(val('floor')),closing_mail:f.closing_mail.checked};
    if(val('port'))job.port=val('port');
    if(f.custom.checked){job.plans=[...byId('plan-lines').children].map(row=>({terms:row.querySelector('.line-terms').value,payment:row.querySelector('.line-payment').value}));}
    await mutate('preview',job,'正在读取行情和测算报价…');
  }else{
    const body={endpoint:val('endpoint'),model:val('model'),key:f.key.value};f.key.value='';
    if(await mutate('model',body))feedback('模型设置已更新。');
  }
});
document.addEventListener('click',event=>{
  const el=event.target.closest('button');if(!el||el.disabled)return;
  if(el.id==='add-account'){addAccount();byId('account-list').lastElementChild?.querySelector('input')?.focus();return;}
  if(el.classList.contains('remove')){el.closest('.account-row').remove();syncAccounts();return;}
  const a=el.dataset.action;
  if(a==='refresh')mutate('refresh',{},'正在读取角色资料与合同条款…');
  else if(a==='start'){
    const p=state.preview;if(!p||previewEdited)return;
    const orders=p.orders||[];ask('确认执行这批交易','<p>工作台将用下面的组合正式签约、采购并办理收付。各角色的学校账户会产生业务记录。</p><ul>'+orders.map(o=>'<li>'+esc(o.contract)+' · '+esc(o.product)+' × '+num(o.quantity)+' · '+esc(o.terms)+' ＋ '+esc(o.payment)+'</li>').join('')+'</ul>','执行 '+orders.length+' 笔交易',()=>mutate('start',{id:p.id},'正在交给本地终端执行…'));
  }else if(a==='resume'){const id=el.dataset.contract;ask('续办原合同','<p>终端将核对合同 <strong>'+esc(id)+'</strong> 的业务回执，然后继续办理下一步。</p>','确认续办',()=>mutate('resume',{contract:id}));}
  else if(a==='logout')ask('退出当前账号','<p>个人业务存档会保留在本机。再次登录相同账号组合后可继续办理。</p>','退出账号',async()=>{if(await mutate('logout',{}))location.hash='overview';});
  else if(a==='model-off')mutate('model',{endpoint:'',model:'',key:''}).then(ok=>{if(ok)feedback('模型服务已关闭。');});
});
document.addEventListener('input',event=>{
  const f=event.target.closest('#quote');if(!f)return;
  if(state.preview&&!previewEdited){previewEdited=true;render();}
});
document.addEventListener('change',event=>{
  const el=event.target;
  if(el.id==='archive-filter'){archiveFilter=el.value;render();return;}
  const f=el.closest('#quote');
  if(f){if(el.name==='count'||el.name==='custom')planFields();routeHint();}
});
window.addEventListener('hashchange',()=>{if(state){render();}window.scrollTo(0,0);});
poll();

