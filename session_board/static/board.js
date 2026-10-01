'use strict';
const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name=csrf]').content;
const labels = {working:'Al lavoro',waiting:'In attesa',blocked:'Bloccata',idle:'Inattiva',done:'Completata'};
let rows = [], filter = 'all', selectedPane = null, selectedIdentity = null, selectedSession = null, pollBusy = false, outputBusy = false;
let diagnosticsBusy = false, diagnosticsTimer = null, terminalSwitchBusy = false, terminalQuestionBusy = false;
let sessionView = 'terminal', chatEpoch = 0, terminalExpanded = false, pendingTerminalOutput = null;
let terminalAudio = null, terminalAudioBusy = false, chatShortcuts = null;
let verifiedAudioTarget=null, audioStatusBusy=false, audioStatusRetry=0, audioReadId=0;
let audioAvailability = {available:false, reason:'Verifica della trascrizione…'};
let pins;
try { pins = new Set(JSON.parse(localStorage.getItem('session-board-pins') || '[]')); } catch { pins = new Set(); }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'X-CSRF-Token': csrf, ...options.headers}, signal: AbortSignal.timeout(options.body instanceof FormData ? 60000 : 10000)});
  if (response.status === 401) { location.href = '/login'; throw Object.assign(new Error('Accesso scaduto'),{status:401}); }
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || 'Richiesta non riuscita'), {status:response.status, definiteRejection:typeof data.definite_rejection==='boolean'?data.definite_rejection:[400,401,403,413].includes(response.status)});
  return data;
}
async function prepareTerminalAudio() {
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
  // Only the actual transcription handles auth failures and updates the interface.
  try {await fetch('/api/audio/prepare',{method:'POST',headers:{'X-CSRF-Token':csrf},signal:controller.signal});}
  finally {clearTimeout(timeout);}
}
async function transcribeTerminalAudio(blob,{signal}) {
  const controller=new AbortController(),cancel=()=>controller.abort(signal?.reason);
  if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
  const timeout=setTimeout(()=>controller.abort(),180000),body=new FormData();
  body.append('audio',blob,blob.type.includes('mp4')?'registrazione.m4a':'registrazione.webm');
  try {
    const response=await fetch('/api/audio/transcribe',{method:'POST',headers:{'X-CSRF-Token':csrf},body,signal:controller.signal});
    if(response.status===401){location.href='/login';throw Object.assign(new Error('Accesso scaduto'),{status:401});}
    const data=await response.json().catch(()=>({}));if(!response.ok)throw Object.assign(new Error(data.error||'Trascrizione non riuscita'),{status:response.status});return data;
  } finally {clearTimeout(timeout);signal?.removeEventListener('abort',cancel);}
}
function updateTerminalActions(){
  $('drawer').classList.toggle('has-question',questions.awaiting&&!terminalAudioBusy);
  terminalComposer.update();questions.update();terminalAudio?.update();window.updateSwitchButton?.();
  const microphone=$('send-form').querySelector('.terminal-audio-controls');
  if(microphone)microphone.style.display=sessionView==='chat'?'':'none';chatShortcuts?.update();
}
function node(tag, cls, text) { const n = document.createElement(tag); if(cls)n.className = cls; if(text !== undefined)n.textContent = text; return n; }
const terminalExpand=node('button','','Espandi terminale');terminalExpand.type='button';terminalExpand.id='terminal-expand';terminalExpand.setAttribute('aria-expanded','false');terminalExpand.setAttribute('aria-controls','output');document.querySelector('.output-heading').append(terminalExpand);
const terminalCollapse=node('button','quiet','Torna ai controlli');terminalCollapse.type='button';terminalCollapse.id='terminal-collapse';terminalCollapse.hidden=true;$('close-drawer').before(terminalCollapse);
function setTerminalExpanded(value,focus=true){
  value=!!value&&$('drawer').open&&sessionView==='terminal';if(value===terminalExpanded)return;
  const output=$('output'),top=output.scrollTop,left=output.scrollLeft;
  if(value&&$('drawer').contains(document.activeElement))document.activeElement.blur();
  terminalExpanded=value;$('drawer').classList.toggle('terminal-expanded',value);terminalCollapse.hidden=!value;terminalExpand.setAttribute('aria-expanded',String(value));
  updateTerminalActions();if(focus)(value?output:terminalExpand).focus({preventScroll:true});output.scrollTop=top;output.scrollLeft=left;
}
function resetTerminalReading(){pendingTerminalOutput=null;setTerminalExpanded(false,false);}
terminalExpand.onclick=()=>setTerminalExpanded(true);terminalCollapse.onclick=()=>setTerminalExpanded(false);
$('drawer').addEventListener('keydown',event=>{if(terminalExpanded&&event.key==='Escape'){event.preventDefault();event.stopPropagation();setTerminalExpanded(false);}},true);
$('drawer').addEventListener('cancel',event=>{if(terminalExpanded){event.preventDefault();setTerminalExpanded(false);}});
function selectedTerminalOutput(){const selection=getSelection();return selection&&!selection.isCollapsed&&$('output').contains(selection.anchorNode)&&$('output').contains(selection.focusNode);}
function acceptTerminalOutput(text){
  const output=$('output');if(output.textContent===text){pendingTerminalOutput=null;return;}
  if(terminalExpanded&&selectedTerminalOutput()){pendingTerminalOutput=text;return;}
  pendingTerminalOutput=null;const top=output.scrollTop,left=output.scrollLeft;output.textContent=text;
  if(terminalExpanded){output.scrollTop=top;output.scrollLeft=left;}
}
document.addEventListener('selectionchange',()=>{if(pendingTerminalOutput!==null&&!selectedTerminalOutput()&&$('drawer').open&&sessionView==='terminal')acceptTerminalOutput(pendingTerminalOutput);});
function age(ts) { const s = Math.max(0,Math.floor(Date.now()/1000-ts)); return s<60 ? 'ora' : s<3600 ? `${Math.floor(s/60)} min fa` : `${Math.floor(s/3600)} h fa`; }
function state(row) { return row.stale ? 'unknown' : row.report?.status || 'unknown'; }
const sessionSummaries=new Map(), summaryPending=new Set(), summaryQueue=[];
let summaryRunning=0;
const terminalTarget=new URLSearchParams(location.search);
let terminalTargetPending=terminalTarget.get('view')==='terminal';
function activePane(row){return row.panes.find(p=>p.active)||row.panes[0];}
function summaryKey(row){const pane=activePane(row);return JSON.stringify([row.id,row.created_at,pane?.id,pane?.identity]);}
function paintSummary(box){
  const data=sessionSummaries.get(box.dataset.summaryKey)?.data||{};
  const count=Number.isSafeInteger(data.context_tokens)&&data.context_tokens>=0?data.context_tokens:null;
  const formatted=count===null?'Token n/d':`Contesto ${count>=1000?(count/1000).toLocaleString('it-IT',{maximumFractionDigits:1})+'k':count} token`;
  const tokens=node('span','session-context',formatted);
  tokens.title=count===null?'Token di contesto non disponibili':`${count.toLocaleString('it-IT')} token nel contesto, non consumo cumulativo`;
  if(data.sampled_at&&Number.isFinite(Date.parse(data.sampled_at)))tokens.title+=` · rilevati ${new Date(data.sampled_at).toLocaleString('it-IT')}`;
  const account=node('span','session-account-name',typeof data.account==='string'&&data.account?`👤 ${data.account}`:'Abbonamento n/d');
  account.title=account.textContent;box.replaceChildren(tokens,account);
}
function pumpSummaries(){
  if(document.hidden||!$('sessions').classList.contains('session-list'))return;
  while(summaryRunning<2&&summaryQueue.length){
    const job=summaryQueue.shift();
    if(!rows.some(row=>summaryKey(row)===job.key)){summaryPending.delete(job.key);continue;}
    summaryRunning++;
    api(`/api/panes/${job.pane.id}/summary?identity=${encodeURIComponent(job.pane.identity)}`)
      .then(data=>sessionSummaries.set(job.key,{data,updated:Date.now()}))
      .catch(()=>sessionSummaries.set(job.key,{data:{},updated:Date.now()}))
      .finally(()=>{
        summaryRunning--;summaryPending.delete(job.key);
        for(const box of document.querySelectorAll('[data-summary-key]'))if(box.dataset.summaryKey===job.key)paintSummary(box);
        pumpSummaries();
      });
  }
}
function refreshSummaries(){
  if(!$('sessions').classList.contains('session-list'))return;
  const visible=new Set(Array.from(document.querySelectorAll('[data-summary-key]'),box=>box.dataset.summaryKey));
  for(const row of rows){
    const key=summaryKey(row),pane=activePane(row),cached=sessionSummaries.get(key);
    if(!visible.has(key)||!pane||summaryPending.has(key)||(cached&&Date.now()-cached.updated<15000))continue;
    summaryPending.add(key);summaryQueue.push({key,pane});
  }
  pumpSummaries();
}
function fullTerminalURL(row){
  const pane=activePane(row),url=new URL('/',location.href);
  for(const [key,value] of Object.entries({session:row.id,created:row.created_at,pane:pane.id,identity:pane.identity,view:'terminal'}))url.searchParams.set(key,value);
  return url.href;
}
function openRequestedTerminal(){
  if(!terminalTargetPending)return;
  terminalTargetPending=false;
  const row=rows.find(row=>row.id===terminalTarget.get('session')&&String(row.created_at)===terminalTarget.get('created'));
  const pane=row?.panes.find(pane=>String(pane.id)===terminalTarget.get('pane')&&pane.identity===terminalTarget.get('identity'));
  if(!pane){$('error').textContent='Il terminale richiesto non è più disponibile. Apri la sessione dall’elenco.';$('error').hidden=false;return;}
  openSession(row,'terminal',{pane,fullscreen:true});
}
function render() {
  const search = $('search').value.toLowerCase();
  const visible = rows.filter(r => (filter !== 'pinned' || pins.has(r.name)) && (filter !== 'attention' || ['waiting','blocked'].includes(state(r))) && [r.name,...r.panes.flatMap(p=>[p.command,p.cwd])].join(' ').toLowerCase().includes(search));
  visible.sort((a,b) => Number(pins.has(b.name))-Number(pins.has(a.name)) || Number(['waiting','blocked'].includes(state(b)))-Number(['waiting','blocked'].includes(state(a))) || a.name.localeCompare(b.name));
  $('sessions').replaceChildren();
  $('count').textContent = rows.length;
  $('attention-count').textContent = rows.filter(r=>['waiting','blocked'].includes(state(r))).length;
  if (!visible.length) {
    const box=node('div','empty');box.append(node('h3','',rows.length ? 'Nessuna corrispondenza' : 'Pronto per la prima sessione'),node('p','',rows.length ? 'Prova un altro filtro o una ricerca diversa.' : 'Avvia tmux con lo stesso utente della board. Le sessioni compariranno qui.'));$('sessions').append(box);
  }
  for (const row of visible) {
    const card=node('article','card'), top=node('div','card-top'), s=state(row);
    top.append(node('span',`badge ${s}`,labels[s] || (row.stale ? 'Stato da aggiornare' : 'Stato non segnalato')));
    const pin=node('button',`pin ${pins.has(row.name)?'active':''}`,pins.has(row.name)?'★':'☆'); pin.setAttribute('aria-label',`Preferita: ${row.name}`);pin.setAttribute('aria-pressed',String(pins.has(row.name)));
    pin.onclick=()=>{pins.has(row.name)?pins.delete(row.name):pins.add(row.name);try{localStorage.setItem('session-board-pins',JSON.stringify([...pins]));}catch{}render();};top.append(pin);
    const current=activePane(row), path=node('div','path',current?.cwd||'');path.title=current?.cwd||'';
    card.append(top,node('h3','',row.name),path,node('p','detail',row.report?.detail || `${current?.command || 'tmux'} · ${row.panes.length} pannell${row.panes.length===1?'o':'i'}`));
    const summary=node('div','session-summary');summary.dataset.summaryKey=summaryKey(row);paintSummary(summary);card.append(summary);
    const foot=node('div','card-foot');foot.append(node('span','',row.report ? `Segnalata ${age(row.report.updated_at)}` : row.attached ? 'Terminale collegato' : 'In background'));
    // Un solo pulsante: il cassetto che si apre ha dentro le due schede, Chat e Terminale.
    const open=node('button','open-session primary','Apri sessione');open.onclick=()=>openSession(row,'chat');foot.append(open);
    card.append(foot);$('sessions').append(card);
  }
  const currentKeys=new Set(rows.map(summaryKey));for(const key of sessionSummaries.keys())if(!currentKeys.has(key))sessionSummaries.delete(key);
  refreshSummaries();
}
const draftStorageKey='session-board:portable-terminal-drafts:v1';
function terminalDraftKey(){return selectedPane===null||!selectedIdentity?null:JSON.stringify([location.host,selectedSession,selectedPane,selectedIdentity]);}
function readTerminalDrafts(){try{const value=JSON.parse(localStorage.getItem(draftStorageKey)||'{}');return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch{return {};}}
function saveTerminalDraft(){
  const key=terminalDraftKey();if(!key)return false;
  const drafts=readTerminalDrafts();if($('message').value)drafts[key]={text:$('message').value,updated:Date.now()};else delete drafts[key];
  const recent=Object.entries(drafts).filter(([,d])=>d&&typeof d.text==='string'&&Date.now()-d.updated<30*86400000).sort((a,b)=>b[1].updated-a[1].updated).slice(0,50);
  try{localStorage.setItem(draftStorageKey,JSON.stringify(Object.fromEntries(recent)));return true;}catch{return false;}
}
function restoreTerminalDraft(){const draft=readTerminalDrafts()[terminalDraftKey()];$('message').value=draft&&Date.now()-draft.updated<30*86400000&&typeof draft.text==='string'?draft.text.slice(0,4000):'';terminalComposer.update();}
// Capture before the image composer clears its input during pagehide.
window.addEventListener('pagehide',saveTerminalDraft,{capture:true});
function openSession(row,view='terminal',options={}) {
  saveTerminalDraft();audioReadId++;verifiedAudioTarget=null;resetTerminalReading();terminalAudio?.reset();
  chatEpoch++;outputBusy=false;conversation.reset();questions.reset();
  selectedSession=row.name;$('drawer-title').textContent=row.name;$('pane-select').replaceChildren();
  for(const p of row.panes){const option=node('option','',`Finestra ${p.window} · Pannello ${p.index} · ${p.command}`);option.value=p.id;option.dataset.identity=p.identity;$('pane-select').append(option);}
  const pane=options.pane||activePane(row); selectedPane=pane?.id ?? null; selectedIdentity=pane?.identity ?? null; $('pane-select').value=selectedPane;
  $('drawer').classList.toggle('fullscreen',options.fullscreen===true);
  $('send-result').textContent='';$('output').textContent='Caricamento…';$('drawer').showModal();document.body.classList.add('session-open');sizeSessionViewport();terminalComposer.reset();restoreTerminalDraft();setSessionView(view);window.refreshSessionAccount?.();
}
async function refreshOutput() {
  if(sessionView==='chat'){await Promise.all([conversation.refresh(),questions.refresh()]);return;}
  if (selectedPane===null || !$('drawer').open || outputBusy) return;
  outputBusy=true;const target=selectedPane, identity=selectedIdentity, ticket=chatEpoch;
  try { const data=await api(`/api/panes/${target}/output?identity=${encodeURIComponent(identity)}`);if(ticket===chatEpoch && selectedPane===target && selectedIdentity===identity && $('drawer').open){acceptTerminalOutput(data.output||'Nessun output.');$('output-state').textContent='Aggiornato '+new Date().toLocaleTimeString('it-IT');} }
  catch(e){if(ticket===chatEpoch && selectedPane===target && selectedIdentity===identity)$('output-state').textContent=e.message;}
  finally{if(ticket===chatEpoch)outputBusy=false;}
}
function sizeSessionViewport(){
  const viewport=window.visualViewport,height=viewport?.height||innerHeight;
  $('drawer').style.setProperty('--drawer-height',height+'px');$('drawer').style.setProperty('--drawer-top',(viewport?.offsetTop||0)+'px');
  $('drawer').classList.toggle('keyboard-open',height<540);
}
window.visualViewport?.addEventListener('resize',sizeSessionViewport);window.visualViewport?.addEventListener('scroll',sizeSessionViewport);window.addEventListener('resize',sizeSessionViewport);
function setSessionView(view){
  resetTerminalReading();terminalAudio?.reset();
  sessionView=view==='chat'?'chat':'terminal';const chat=sessionView==='chat';
  $('drawer').classList.toggle('chat-mode',chat);$('drawer').classList.toggle('single-pane',$('pane-select').options.length===1);
  $('chat').hidden=!chat;$('chat-tools').hidden=!chat;$('output').hidden=chat;document.querySelector('.output-heading').hidden=chat;
  if($('session-account'))$('session-account').hidden=chat;
  for(const tab of ['chat','terminal']){$(tab+'-tab').setAttribute('aria-selected',String(tab===sessionView));$(tab+'-tab').tabIndex=tab===sessionView?0:-1;}
  $('send-form').querySelector('button[type="submit"]').textContent=chat?'Invia':'Invia + Enter ↗';
  conversation.refresh();questions.refresh();updateTerminalActions();if(!chat)refreshOutput();
}
async function poll() {
  if(document.hidden || pollBusy)return;pollBusy=true;if(audioStatusRetry&&Date.now()>=audioStatusRetry)refreshAudioAvailability();
  try {const data=await api('/api/sessions');rows=data.sessions;render();$('sync').textContent='Aggiornato '+new Date().toLocaleTimeString('it-IT');$('error').hidden=true;openRequestedTerminal();}
  catch(e){$('error').textContent=e.message;$('error').hidden=false;$('sync').textContent='Connessione da verificare';}
  finally{pollBusy=false;}
  refreshOutput();
}
async function diagnostics() {
  clearTimeout(diagnosticsTimer);
  if(document.hidden || diagnosticsBusy)return;
  diagnosticsBusy=true;
  let nextCheck=30000;
  const results = await Promise.allSettled([api('/api/metrics'),api('/api/mcp-health')]);
  if(results[0].status==='fulfilled'){const m=results[0].value;$('cpu').textContent=`${m.cpu_percent}%`;$('memory').textContent=`${m.memory_percent}%`;$('disk').textContent=`Disco utilizzato ${m.disk_percent}%`;}
  if(results[1].status==='fulfilled'){
    const data=results[1].value;$('mcp-note').textContent=data.enabled?'Ultima verifica della CLI. Un esito sconosciuto richiede un controllo.':'Diagnostica opzionale disattivata. Puoi abilitarla dopo aver configurato i server MCP.';$('mcp-list').replaceChildren();
    if(data.servers.some(s=>s.reason==='check_in_progress'||s.stale))nextCheck=1000;
    for(const s of data.servers){const box=node('div','mcp-item',s.name);box.append(node('small','',`${s.status}${s.stale?' · dato precedente':''}`));box.title=s.reason||'';$('mcp-list').append(box);}
  }else{$('mcp-note').textContent='Diagnostica non disponibile.';}
  diagnosticsBusy=false;
  if(!document.hidden)diagnosticsTimer=setTimeout(diagnostics,nextCheck);
}
$('search').addEventListener('input',render);
document.querySelector('[data-session-layout-target="#sessions"]').addEventListener('click',()=>setTimeout(refreshSummaries,0));
for(const button of document.querySelectorAll('[data-filter]'))button.onclick=()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('selected',b===button));render();};
$('logout').onclick=async()=>{try{await api('/api/logout',{method:'POST'});location.href='/login';}catch(e){$('error').textContent=e.message;$('error').hidden=false;}};
$('close-drawer').onclick=()=>$('drawer').close();$('drawer').addEventListener('close',()=>{if($('drawer').open)return;saveTerminalDraft();audioReadId++;verifiedAudioTarget=null;resetTerminalReading();terminalAudio?.reset();chatEpoch++;outputBusy=false;selectedPane=null;selectedIdentity=null;conversation.reset();questions.reset();terminalComposer.reset();document.body.classList.remove('session-open');});
$('pane-select').onchange=()=>{saveTerminalDraft();audioReadId++;verifiedAudioTarget=null;resetTerminalReading();terminalAudio?.reset();chatEpoch++;outputBusy=false;selectedPane=Number($('pane-select').value);selectedIdentity=$('pane-select').selectedOptions[0].dataset.identity;conversation.reset();questions.reset();terminalComposer.reset();restoreTerminalDraft();$('output').textContent='Caricamento…';refreshOutput();window.refreshSessionAccount?.();};
function currentAudioTarget(target){return target&&$('drawer').open&&target.pane===selectedPane&&target.identity===selectedIdentity&&target.key===chatEpoch&&target.session===selectedSession;}
async function fetchChat(target){
  const readId=++audioReadId,current=()=>readId===audioReadId&&sessionView==='chat'&&currentAudioTarget(target);
  try{const data=await api(`/api/panes/${target.pane}/chat?identity=${encodeURIComponent(target.identity)}`);
    if(current())verifiedAudioTarget=data.available===true&&data.conversation_id&&data.engine?{...target,host:location.host,conversation_id:data.conversation_id,engine:data.engine}:null;
    return data;
  }catch(error){if(current()&&(error.definiteRejection||[401,403,404,409].includes(error.status)))verifiedAudioTarget=null;throw error;}
}
function audioContext(){return sessionView==='chat'&&currentAudioTarget(verifiedAudioTarget)?{...verifiedAudioTarget}:null;}
const conversation=SessionChat.create({container:$('chat'),status:$('chat-status'),latest:$('chat-latest'),
  context:()=>sessionView==='chat'&&$('drawer').open&&selectedPane!==null?{pane:selectedPane,identity:selectedIdentity,key:chatEpoch,session:selectedSession}:null,
  fetchData:fetchChat,onChange:updateTerminalActions});
const questionBox=node('section');questionBox.id='chat-question';questionBox.hidden=true;$('send-form').before(questionBox);
const questions=SessionQuestions.create({container:questionBox,
  context:()=>sessionView==='chat'&&$('drawer').open&&selectedPane!==null?{pane:selectedPane,identity:selectedIdentity,key:chatEpoch}:null,
  fetchQuestion:target=>api(`/api/panes/${target.pane}/question?identity=${encodeURIComponent(target.identity)}`),
  answerQuestion:(target,body)=>api(`/api/panes/${target.pane}/question/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  canAnswer:()=>!$('send-form').hidden&&!terminalSwitchBusy&&!terminalAudioBusy&&!terminalComposer.sending,
  onBusy:value=>{terminalQuestionBusy=value;updateTerminalActions();},onChange:updateTerminalActions,
  onAnswered:()=>setTimeout(()=>refreshOutput(),150)});
const terminalComposer=TerminalImages.create({form:$('send-form'),input:$('message'),result:$('send-result'),context:()=>$('drawer').open&&selectedPane!==null?{pane:selectedPane,identity:selectedIdentity,session:selectedSession,...(sessionView==='chat'&&conversation.scope?{chat_conversation:conversation.scope.conversation_id}:{})}:null,canSend:()=>!terminalExpanded&&!$('send-form').hidden&&!terminalSwitchBusy&&!terminalQuestionBusy&&!terminalAudioBusy&&(sessionView!=='chat'||conversation.ready&&!questions.awaiting),onBusy:updateTerminalActions,onDelivery:event=>conversation.delivery(event),onSent:()=>{terminalAudio?.reset();saveTerminalDraft();refreshOutput();},send:(pane,body)=>api(`/api/panes/${pane}/send`,body instanceof FormData?{method:'POST',body}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
terminalAudio=TerminalAudio.create({form:$('send-form'),input:$('message'),result:$('send-result'),
  context:audioContext,
  canRecord:()=>!document.hidden&&!$('send-form').hidden&&!terminalSwitchBusy&&!terminalQuestionBusy&&!terminalComposer.sending&&conversation.ready,
  availability:()=>audioAvailability,canTranscribe:()=>conversation.ready&&!$('send-form').hidden&&!terminalSwitchBusy&&!terminalQuestionBusy&&!terminalComposer.sending,onDraft:saveTerminalDraft,prepare:prepareTerminalAudio,transcribe:transcribeTerminalAudio,onBusy:value=>{terminalAudioBusy=value;updateTerminalActions();}});
$('send-form').querySelector('.terminal-audio-controls').style.display='none';
chatShortcuts=ChatShortcuts.create({container:$('drawer'),form:$('send-form'),input:$('message'),
  context:()=>$('drawer').open&&selectedPane!==null?{pane:selectedPane,identity:selectedIdentity}:null,
  isEnabled:()=>$('drawer').open&&!terminalExpanded});
async function refreshAudioAvailability(){
  if(audioStatusBusy)return;audioStatusBusy=true;
  try{const data=await api('/api/audio/status');audioAvailability={available:data.available===true,reason:data.reason};audioStatusRetry=0;}
  catch{audioAvailability={available:false,reason:'Trascrizione non raggiungibile. Riprovo al ritorno della connessione.'};audioStatusRetry=Date.now()+15000;}
  finally{audioStatusBusy=false;updateTerminalActions();}
}
refreshAudioAvailability();
window.addEventListener('online',()=>{refreshAudioAvailability();poll();});
$('message').addEventListener('input',saveTerminalDraft);
window.addEventListener('pageshow',event=>{if(event.persisted&&$('drawer').open){restoreTerminalDraft();poll();}});
$('message').placeholder='Scrivi un messaggio…';
$('chat-tab').onclick=()=>setSessionView('chat');$('terminal-tab').onclick=()=>setSessionView('terminal');$('chat-refresh').onclick=()=>refreshOutput();
$('session-view-tabs').onkeydown=event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();setSessionView(event.key==='Home'?'chat':event.key==='End'?'terminal':sessionView==='chat'?'terminal':'chat');$(sessionView+'-tab').focus();}};
document.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)&&!$('drawer').open){e.preventDefault();$('search').focus();}});
document.addEventListener('visibilitychange',()=>{terminalAudio?.update();if(!document.hidden){poll();diagnostics();}});
poll();diagnostics();setInterval(poll,5000);
