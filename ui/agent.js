/* In-memory UI for the desktop agent. The server owns conversation and operation state. */
window.workbenchChat = (() => {
  'use strict';
  const el = id => document.getElementById(id);
  const drawer = el('agent-panel'), input = el('chat-input');
  const log = el('chat-messages'), scroll = el('agent-scroll'), pending = el('chat-pending');
  const toolNames = new Map(Object.entries({
    status: '工作台状态', jobs: '合同存档', refresh: '同步资料', preview: '生成报价',
    start: '执行交易', resume: '续办合同', terminal_check: '终端自检', terminal_audit: '终端合同核算'
  }));
  let lastState = null, scope = '', submitting = false, conversationKey = '';
  let promptKey = '', pendingKey = '', retry = null;

  function node(tag, text, cls) {
    const item = document.createElement(tag);
    if (cls) item.className = cls;
    if (text) item.textContent = text;
    return item;
  }
  function error(message = '') {
    el('chat-error').textContent = message;
    el('chat-error').hidden = !message;
  }
  function controls() {
    const thinking = !!lastState?.agent?.busy;
    el('chat-thinking').hidden = !thinking;
    el('chat-send').disabled = !lastState || thinking || submitting || !input.value.trim();
    pending.querySelectorAll('button').forEach(button => {
      button.disabled = submitting || thinking || (button.dataset.choice === 'confirm' && lastState?.task?.status === 'running');
    });
    el('open-agent').classList.toggle('has-pending',!!lastState?.agent?.pending);
  }
  function renderMessages(messages) {
    const signature = JSON.stringify(messages);
    if (signature === conversationKey) return false;
    conversationKey = signature;
    const existing = new Map([...log.children].map(row => [row.dataset.id,row]));
    for (const message of messages) {
      const id = String(message.id);
      const previous = existing.get(id);
      const tool = message.tool || '';
      if (previous && previous.dataset.text === message.text && previous.dataset.role === message.role && previous.dataset.tool === tool) {
        existing.delete(id);
        continue;
      }
      const role = ['user','assistant','tool'].includes(message.role)?message.role:'assistant';
      const row = node('div','', 'chat-entry chat-'+role);
      row.dataset.id=id;
      row.dataset.text=message.text;
      row.dataset.role=role;
      row.dataset.tool=tool;
      const label = role==='user'?'你':role==='assistant'?'Agent':toolNames.get(tool)||'工具回执';
      row.append(node('span',label,'chat-who'),node('p',message.text||'','chat-text'));
      if (previous) {previous.replaceWith(row);existing.delete(id);}
      else log.append(row);
    }
    existing.forEach(row => row.remove());
    el('chat-welcome').hidden=messages.length>0;
    return true;
  }
  function suggestions(s) {
    const connected = s.accounts.length>0;
    const key = String(connected);
    if (promptKey === key)return;
    promptKey=key;
    const prompts = connected ? ['查询各角色的余额和等级','帮我试算一笔 CIF＋T/T 床单报价','运行终端自检'] :
      ['怎样开始第一笔交易？','运行终端自检'];
    el('chat-prompts').replaceChildren(...prompts.map(text=>{
      const button=node('button',text,'agent-prompt');
      button.type='button';
      button.addEventListener('click',()=>{input.value=text;controls();input.focus();});
      return button;
    }));
  }
  function renderPending(plan) {
    const signature = JSON.stringify(plan||null);
    if (signature===pendingKey)return false;
    pendingKey=signature;
    pending.replaceChildren();pending.hidden=!plan;
    if (!plan)return false;
    const list=node('ul');
    for (const detail of plan.details||[])list.append(node('li',String(detail)));
    const controls=node('div','','btn-row');
    for (const [choice,label,style] of [['cancel','取消','secondary'],['confirm','确认办理','primary']]) {
      const button=node('button',label,'btn '+style+' small');
      button.type='button';button.dataset.choice=choice;
      button.addEventListener('click',()=>act(plan.id,choice));
      controls.append(button);
    }
    pending.append(node('h3',plan.title||'请确认操作'),list,controls);
    return true;
  }
  function render(s) {
    const nextScope=JSON.stringify([s.base,s.accounts]);
    if(scope&&scope!==nextScope){input.value='';retry=null;error();}
    scope=nextScope;lastState=s;
    const wasAtEnd=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<65;
    const agent=s.agent||{};
    el('agent-mode').textContent=agent.mode==='model'?'模型已连接 · 设置':'本地模式 · 模型设置';
    suggestions(s);
    const changed=renderMessages(agent.messages||[]);
    const planChanged=renderPending(agent.pending);
    controls();
    if(drawer.open && (planChanged || (changed && wasAtEnd)))scroll.scrollTop=scroll.scrollHeight;
  }
  async function send() {
    const text=input.value.trim();
    if(!text || !lastState || submitting || lastState.agent?.busy)return;
    if(!retry||retry.text!==text)retry={text,id:crypto.randomUUID()};
    const attempt=retry;
    submitting=true;error();controls();
    try {
      await request('chat',{message:text,request_id:attempt.id});
      if(input.value.trim()===text)input.value='';
      retry=null;
      await refresh();
      scroll.scrollTop=scroll.scrollHeight;
    } catch(err) {error(err.message);}
    finally{submitting=false;controls();if(drawer.open)input.focus();}
  }
  async function act(id,choice) {
    if(submitting || lastState?.agent?.pending?.id!==id)return;
    submitting=true;error();controls();
    try {await request('chat-action',{id,choice});await refresh();}
    catch(err){error(err.message);try{await refresh();}catch{}}
    finally{submitting=false;controls();}
  }
  el('open-agent').addEventListener('click',async()=>{
    if(!drawer.open)drawer.showModal();
    input.focus();
    try{await refresh();scroll.scrollTop=scroll.scrollHeight;}catch(err){error(err.message);}
  });
  el('close-agent').addEventListener('click',()=>drawer.close());
  drawer.addEventListener('close',()=>el('open-agent').focus({preventScroll:true}));
  el('agent-mode').addEventListener('click',()=>drawer.close());
  input.addEventListener('input',controls);
  input.addEventListener('keydown',event=>{
    if (event.key==='Enter' && !event.shiftKey && !event.isComposing && event.keyCode!==229) {
      event.preventDefault();send();
    }
  });
  el('chat-compose').addEventListener('submit',event=>{event.preventDefault();send();});
  controls();
  return {render};
})();
