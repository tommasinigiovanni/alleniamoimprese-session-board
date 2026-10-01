'use strict';
let accountRows = [], accountsBusy = false, accountTimer = null, loginTimer = null;
let switching = false;
let loginSlug = null, loginDone = false, loginBusy = false, inspectedSession = null;
let loginState = null, loginGeneration = 0, loginRevision = 0, loginPollBusy = false, loginStartBusy = false;
let pendingLoginCancel = Promise.resolve(), addGeneration = 0, sessionReadGeneration = 0, sessionReading = false;
const loginLabels = {starting:'Preparazione accesso…',waiting_url:'Preparazione del link…',waiting_code:'Apri il link e incolla il codice di autorizzazione.',checking:'Verifica del codice…',verifying:'Verifica del codice…',ok:'Abbonamento collegato.',invalid_code:'Codice non valido. Riprova.',expired:'Accesso scaduto. Chiudi e avvia un nuovo collegamento.',cancelled:'Accesso annullato.',failed:'Accesso non riuscito. Chiudi e riavvia il collegamento.',error:'Accesso non riuscito.'};
const write = (path, body = {}, method = 'POST') => api(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const accountPath = slug => `/api/accounts/${encodeURIComponent(slug)}`;
function quota(container, limit) {
  if(!Number.isFinite(limit.used_percent))return;
  const box=node('div','quota'), heading=node('div','quota-heading');
  heading.append(node('span','',limit.label||limit.id),node('strong','',`${Math.round(limit.used_percent)}%`));
  const progress=document.createElement('progress');progress.max=100;progress.value=limit.used_percent;progress.setAttribute('aria-label',limit.label||limit.id);
  box.append(heading,progress);
  if(limit.resets_at)box.append(node('small','muted','Reset '+new Date(limit.resets_at*1000).toLocaleString('it-IT')));
  container.append(box);
}
function renderAccounts(data) {
  accountRows=data.accounts||[];$('accounts-list').replaceChildren();
  $('accounts-state').textContent=data.refreshing?'Verifica account in corso…':'Stato account e quote disponibili. I consumi si aggiornano ogni cinque minuti.';
  for(const row of accountRows){
    const card=node('article','account-card');
    card.append(node('p','eyebrow','CLAUDE'),node('h3','',row.slug),node('p','muted',[row.email,row.plan].filter(Boolean).join(' · ')||'Account da collegare'),node('span','badge',row.logged_in===true?'Collegato':row.logged_in===false?'Da collegare':'Da verificare'));
    if(row.error)card.append(node('p','detail',row.error));
    for(const limit of [...(row.limits||[]),...(row.scoped_limits||[])])quota(card,limit);
    if(!row.limits?.length)card.append(node('p','detail','Quote non disponibili'));
    if(row.usage_error)card.append(node('p','detail',row.usage_error));
    if(row.sampled_at)card.append(node('small','muted',`${row.usage_stale?'Dato precedente · ':''}${age(row.sampled_at)}`));
    if(data.allow_changes){
      const actions=node('div','account-actions');
      if(row.slug!=='principale'&&row.logged_in===false){const login=node('button','','Collega');login.onclick=()=>startLogin(row.slug);actions.append(login);}
      if(row.slug!=='principale'){
        const logout=node('button','','Scollega');logout.onclick=()=>mutateAccount(row.slug,'logout');
        const remove=node('button','','Rimuovi');remove.onclick=()=>mutateAccount(row.slug,'remove');actions.append(logout,remove);
      }
      card.append(actions);
    }
    $('accounts-list').append(card);
  }
  for(const provider of data.providers||[]){
    const card=node('article','account-card');card.append(node('p','eyebrow','CODEX'),node('h3','','Codex'),node('p','muted',[typeof provider.account==='string'?provider.account:provider.account?.email,provider.plan].filter(Boolean).join(' · ')),node('span','badge',({active:'Collegato',signed_out:'Da collegare',configured:'Configurato',authenticated:'Collegato',available:'Disponibile',unavailable:'Non disponibile',unknown:'Da verificare'})[provider.status]||'Da verificare'));
    for(const limit of provider.limits||[])quota(card,limit);
    if(provider.message)card.append(node('p','detail',provider.message));
    if(!provider.limits?.length)card.append(node('p','detail','Quote non disponibili'));
    if(provider.sampled_at)card.append(node('small','muted',`${provider.usage_stale?'Dato precedente · ':''}${age(provider.sampled_at)}`));
    $('accounts-list').append(card);
  }
  updateSwitchChoices();
}
async function refreshAccounts(){
  clearTimeout(accountTimer);if(document.hidden||accountsBusy)return;accountsBusy=true;let delay=30000;
  try{const data=await api('/api/subscriptions');renderAccounts(data);if(data.refreshing||data.providers?.some(p=>p.refreshing))delay=1500;}
  catch(e){$('accounts-state').textContent=e.message;}
  finally{accountsBusy=false;if(!document.hidden)accountTimer=setTimeout(refreshAccounts,delay);}
}
async function mutateAccount(slug,action){
  const verb=action==='remove'?'Rimuovere':'Scollegare';
  if(!confirm(`${verb} l’abbonamento ${slug}? Le sessioni attive devono essere prima spostate o chiuse.`))return;
  try{await write(accountPath(slug)+(action==='logout'?'/logout':''),{},action==='remove'?'DELETE':'POST');refreshAccounts();}
  catch(e){$('accounts-state').textContent=e.message;}
}
function showLogin(slug,payload){
  ++loginGeneration;loginRevision=0;loginSlug=slug;loginDone=false;loginBusy=false;loginPollBusy=false;$('account-code').value='';$('account-login-title').textContent='Collega '+slug;
  $('account-login').showModal();renderLogin(payload);scheduleLogin();
}
function updateLoginSubmit(){
  $('account-code-form').querySelector('button').disabled=loginBusy||loginDone||['checking','verifying'].includes(loginState);
}
function renderLogin(data){
  loginState=data.state;loginDone=['ok','expired','cancelled','failed','error'].includes(loginState);
  $('account-login-state').textContent=data.message||loginLabels[data.state]||'Verifica accesso…';
  const link=$('account-auth-url');let url=null;
  try{url=new URL(data.auth_url);if(url.protocol!=='https:'||!['claude.ai','console.anthropic.com','platform.claude.com','claude.com'].includes(url.hostname)||!url.pathname.includes('oauth')||url.username||url.password)url=null;}catch{}
  $('account-login-link').hidden=!url||loginDone;
  if(url)link.href=url.href;else link.removeAttribute('href');
  $('account-code-form').hidden=!url||loginDone;
  updateLoginSubmit();
  if(loginDone){clearTimeout(loginTimer);refreshAccounts();}
}
function scheduleLogin(){clearTimeout(loginTimer);if(loginSlug&&!loginDone&&$('account-login').open&&!document.hidden)loginTimer=setTimeout(pollLogin,1500);}
async function pollLogin(){
  if(!loginSlug||loginDone||loginBusy||loginPollBusy||!$('account-login').open){scheduleLogin();return;}
  const slug=loginSlug,generation=loginGeneration,revision=loginRevision;loginPollBusy=true;
  try{const data=await api(accountPath(slug)+'/web-login');if(generation===loginGeneration&&revision===loginRevision&&!loginBusy&&!loginDone&&$('account-login').open)renderLogin(data);}
  catch(e){if(generation===loginGeneration&&revision===loginRevision)$('account-login-state').textContent=e.message;}
  finally{if(generation===loginGeneration){loginPollBusy=false;scheduleLogin();}}
}
async function startLogin(slug){
  if(loginStartBusy||$('account-login').open)return;loginStartBusy=true;
  try{await pendingLoginCancel;showLogin(slug,await write(accountPath(slug)+'/web-login'));}
  catch(e){$('accounts-state').textContent=e.message;}
  finally{loginStartBusy=false;}
}
$('account-add-form').onsubmit=async e=>{
  e.preventDefault();if(loginStartBusy)return;loginStartBusy=true;const generation=addGeneration,button=e.target.querySelector('button');button.disabled=true;$('account-add-error').hidden=true;
  try{await pendingLoginCancel;const slug=$('account-slug').value;const data=await write('/api/accounts',{slug,email:$('account-email').value||null});if(generation===addGeneration&&$('account-add').open){$('account-add').close();showLogin(slug,data);}else{await write(accountPath(slug)+'/web-login',{},'DELETE');}refreshAccounts();}
  catch(err){$('account-add-error').textContent=err.message;$('account-add-error').hidden=false;}
  finally{loginStartBusy=false;button.disabled=false;}
};
$('account-code-form').onsubmit=async e=>{
  e.preventDefault();if(loginBusy||loginDone||!loginSlug||['checking','verifying'].includes(loginState))return;
  loginBusy=true;const slug=loginSlug,generation=loginGeneration,revision=++loginRevision;updateLoginSubmit();
  try{const data=await write(accountPath(slug)+'/web-login/code',{code:$('account-code').value});if(generation===loginGeneration&&revision===loginRevision){$('account-code').value='';renderLogin(data);}}
  catch(err){if(generation===loginGeneration&&revision===loginRevision)$('account-login-state').textContent=err.message;}
  finally{if(generation===loginGeneration&&revision===loginRevision){loginBusy=false;updateLoginSubmit();scheduleLogin();}}
};
$('account-copy-link').onclick=async()=>{try{await navigator.clipboard.writeText($('account-auth-url').href);$('account-login-state').textContent='Link copiato.';}catch{$('account-login-state').textContent='Tieni premuto sul link o usa il menu del browser per copiarlo.';}};
$('account-login').addEventListener('close',()=>{clearTimeout(loginTimer);const slug=loginSlug,done=loginDone;++loginGeneration;loginSlug=null;loginState=null;loginBusy=false;loginPollBusy=false;$('account-code').value='';if(slug&&!done){pendingLoginCancel=write(accountPath(slug)+'/web-login',{},'DELETE').catch(e=>{$('accounts-state').textContent=e.message;});}});
$('account-add').addEventListener('close',()=>{++addGeneration;});
$('close-login').onclick=()=>$('account-login').close();$('close-add').onclick=()=>$('account-add').close();
$('add-account').onclick=()=>{$('account-add-error').hidden=true;$('account-add-form').reset();$('account-add').showModal();};
function updateSwitchChoices(){
  const select=$('switch-account'),previous=select.value;select.replaceChildren(node('option','','Scegli abbonamento…'));select.firstChild.value='';
  for(const row of accountRows.filter(r=>r.logged_in===true&&r.slug!==inspectedSession?.account)){const option=node('option','',row.slug+(row.plan?' · '+row.plan:''));option.value=row.slug;select.append(option);}select.value=previous;updateSwitchButton();
}
function updateSwitchButton(){const metadata=inspectedSession;$('switch-button').disabled=!!(switching||sessionReading||terminalComposer.sending||terminalQuestionBusy||terminalAudioBusy)||!(metadata&&metadata.allow_changes&&$('switch-account').value&&(metadata.switchable||(metadata.requires_conversation&&$('switch-conversation').value)));}
window.refreshSessionAccount=async(preserve=false)=>{
  if(switching||selectedPane===null||!$('drawer').open)return;
  const pane=selectedPane,identity=selectedIdentity,generation=++sessionReadGeneration;
  sessionReading=true;inspectedSession=null;if(!preserve){$('switch-result').textContent='';$('switch-reason').textContent='';$('switch-conversation-label').hidden=true;$('switch-account').value='';}
  $('session-account-state').textContent='Verifica abbonamento…';updateSwitchButton();
  try{const info=await api(`/api/panes/${pane}/session?identity=${encodeURIComponent(identity)}`);if(generation!==sessionReadGeneration||pane!==selectedPane||identity!==selectedIdentity||!$('drawer').open)return;inspectedSession=info;
    $('session-account-state').textContent=info.account?`Claude · ${info.account}${info.conversation_id?' · '+info.conversation_id:''}`:`Processo: ${info.engine}`;
    $('switch-reason').textContent=info.reason||'La conversazione verrà ripresa nello stesso pannello.';
    const choices=$('switch-conversation'),conversation=preserve?choices.value:'';choices.replaceChildren(node('option','','Scegli la conversazione…'));choices.firstChild.value='';
    for(const item of info.candidates||[]){const option=node('option','',`${item.id} · ${new Date(item.updated_at*1000).toLocaleString('it-IT')}`);option.value=item.id;choices.append(option);}
    if((info.candidates||[]).some(item=>item.id===conversation))choices.value=conversation;
    $('switch-conversation-label').hidden=!info.requires_conversation;updateSwitchChoices();
  }catch(e){if(generation===sessionReadGeneration&&pane===selectedPane&&identity===selectedIdentity)$('session-account-state').textContent=e.message;}
  finally{if(generation===sessionReadGeneration){sessionReading=false;updateSwitchButton();}}
};
$('switch-account').onchange=updateSwitchButton;$('switch-conversation').onchange=updateSwitchButton;
$('switch-form').onsubmit=async e=>{
  e.preventDefault();if(switching||$('switch-button').disabled)return;
  const generation=terminalComposer.generation,pane=selectedPane,identity=selectedIdentity,account=$('switch-account').value,conversation=inspectedSession?.conversation_id||$('switch-conversation').value;
  if(!confirm(`Riprendere la conversazione ${conversation} di ${selectedSession} sull’abbonamento ${account}? Il processo del pannello verrà riavviato.`))return;
  terminalAudio?.reset();switching=true;terminalSwitchBusy=true;updateTerminalActions();$('switch-button').disabled=true;
  try{const data=await write(`/api/panes/${pane}/switch-account`,{identity,account,conversation_id:conversation});if(generation===terminalComposer.generation&&pane===selectedPane&&identity===selectedIdentity){saveTerminalDraft();selectedIdentity=data.new_identity||identity;const option=$('pane-select').selectedOptions[0];if(option)option.dataset.identity=selectedIdentity;terminalComposer.reset();restoreTerminalDraft();$('switch-result').textContent=(data.message||'Processo riavviato con resume su '+account)+(data.warning?'. '+data.warning:'');inspectedSession=null;refreshOutput();poll();}refreshAccounts();}
  catch(err){if(generation===terminalComposer.generation&&pane===selectedPane&&identity===selectedIdentity)$('switch-result').textContent=err.message;}
  finally{switching=false;terminalSwitchBusy=false;updateTerminalActions();}
};
for(const tab of ['sessions','accounts'])$('tab-'+tab).onclick=()=>{for(const other of ['sessions','accounts']){$(other+'-panel').hidden=other!==tab;$('tab-'+other).setAttribute('aria-pressed',String(other===tab));}if(tab==='accounts')refreshAccounts();};
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshAccounts();scheduleLogin();}else{clearTimeout(accountTimer);clearTimeout(loginTimer);}});
refreshAccounts();

setInterval(()=>{if(!document.hidden&&$('drawer').open&&selectedPane!==null&&!switching)window.refreshSessionAccount(true);},15000);
