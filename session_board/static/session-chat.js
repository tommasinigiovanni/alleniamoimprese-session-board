/* Shared chat projection for full and portable boards; transport stays with the caller. */
(() => {
  'use strict';
  function create({container:chatView,status,latest:chatLatest,context,fetchData,onChange=()=>{}}){
  let generation=0,chatReady=false,readyFor=null,chatRequest=null,chatConversation=null;
  let lastData=null;
  // This journal belongs to the page, not to the currently mounted conversation.
  const deliveries=new Map(),consumed=new Map();
  const normalize=text=>text.replace(/\r\n?/g,'\n').trim();
  const scopeKey=(target,data)=>JSON.stringify([target.host||'',target.session||'',target.pane,target.identity,data.engine,data.conversation_id]);
  const samePane=(a,b)=>!!a&&!!b&&a.pane===b.pane&&a.identity===b.identity&&(a.session||'')===(b.session||'')&&(a.host||'')===(b.host||'');
  function sameTarget(a,b){return !!a&&!!b&&a.pane===b.pane&&a.identity===b.identity&&a.key===b.key;}
  function chatNode(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
  function chatCopy(text,label){
    const button=chatNode('button','tl-chat-copy','Copia');button.type='button';button.setAttribute('aria-label',label);
    button.onclick=async()=>{
      const target=context(),ticket=generation;if(!target)return;const saved={...target};
      const current=()=>ticket===generation&&sameTarget(saved,context());
      try{await navigator.clipboard.writeText(text);if(current())status.textContent='Copiato negli appunti.';}
      catch{if(current())status.textContent='Copia non disponibile. Seleziona il testo da copiare.';}
    };
    return button;
  }
  function chatLink(href,label){
    if(!/^https?:\/\//i.test(href)||/[\u0000-\u0020\u007f-\u009f]/.test(href))return null;
    try{
      const url=new URL(href);
      if(!['http:','https:'].includes(url.protocol)||!url.hostname||url.username||url.password)return null;
      const node=chatNode('a','',label);node.href=url.href;node.target='_blank';node.rel='noopener noreferrer';return node;
    }catch{return null;}
  }
  function chatUrlEnd(text){
    const opening={'(':')','[':']','{':'}'},extra={')':0,']':0,'}':0};
    for(const char of text){if(opening[char])extra[opening[char]]--;else if(Object.hasOwn(extra,char))extra[char]++;}
    let end=text.length;
    while(end){
      const char=text[end-1];
      if(/[.,!?;:'"…’”]/.test(char)){end--;continue;}
      if(extra[char]>0){extra[char]--;end--;continue;}
      break;
    }
    return end;
  }
  function chatAutolinks(parent,text){
    const urls=/(?:https?:\/\/|www\.)[^\s<>"`]+/gi;let offset=0;
    for(const match of text.matchAll(urls)){
      if(match.index&&/[\p{L}\p{N}_@./:+-]/u.test(text[match.index-1]))continue;
      const label=match[0].slice(0,chatUrlEnd(match[0]));
      const node=chatLink(/^www\./i.test(label)?'https://'+label:label,label);
      if(!node)continue;
      parent.append(text.slice(offset,match.index),node);offset=match.index+label.length;
    }
    parent.append(text.slice(offset));
  }
  function chatInline(parent,text){
    // Render a small Markdown subset via DOM APIs. Raw HTML remains literal.
    const tokens=/(`+)|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\(/g;
    let offset=0,match,closing=null,codeClosing=null;
    while((match=tokens.exec(text))){
      chatAutolinks(parent,text.slice(offset,match.index));let node;
      if(match[1]){
        if(!codeClosing){
          codeClosing=new Map();const previous=new Map();
          for(const run of text.matchAll(/`+/g)){
            const length=run[0].length;
            if(previous.has(length))codeClosing.set(previous.get(length),run.index);
            previous.set(length,run.index);
          }
        }
        const end=codeClosing.get(match.index);
        if(end===undefined)node=document.createTextNode(match[0]);
        else{node=chatNode('code','',text.slice(tokens.lastIndex,end));tokens.lastIndex=end+match[1].length;}
      }
      else if(match[2]){node=chatNode('strong');chatInline(node,match[2]);}
      else{
        // Match once so repeated unclosed Markdown cannot rescan every suffix.
        if(!closing){
          closing=new Map();const opening=[];
          for(let index=0;index<text.length;index++){
            if(text[index]==='(')opening.push(index);
            else if(text[index]===')'&&opening.length)closing.set(opening.pop(),index);
          }
        }
        const start=tokens.lastIndex,end=closing.get(start-1);
        if(end===undefined){parent.append(match[0]);offset=tokens.lastIndex;continue;}
        node=chatLink(text.slice(start,end),match[3])||document.createTextNode(text.slice(match.index,end+1));
        tokens.lastIndex=end+1;
      }
      parent.append(node);offset=tokens.lastIndex;
    }
    chatAutolinks(parent,text.slice(offset));
  }
  function tableCells(line){
    const cells=[];let cell='',code=0,pipes=0;
    for(let i=0;i<line.length;i++){
      const char=line[i];
      if(char==='\\'&&(line[i+1]==='|'||!code&&['\\','`'].includes(line[i+1]))){cell+=line[++i];continue;}
      if(char==='`'){
        let end=i+1;while(line[end]==='`')end++;
        const ticks=line.slice(i,end);
        if(code===ticks.length)code=0;
        else if(!code&&line.indexOf(ticks,end)!==-1)code=ticks.length;
        cell+=ticks;i=end-1;continue;
      }
      if(char==='|'&&!code){cells.push(cell.trim());cell='';pipes++;}else cell+=char;
    }
    if(!pipes)return null;
    cells.push(cell.trim());
    if(!cells[0]&&line.trimStart().startsWith('|'))cells.shift();
    if(!cells[cells.length-1]&&line.trimEnd().endsWith('|'))cells.pop();
    return cells.length?cells:null;
  }
  function chatMarkdown(text){
    const body=chatNode('div','tl-chat-body');const lines=text.split('\n');let paragraph=[],list=null;
    const flush=()=>{if(paragraph.length){const p=chatNode('p');chatInline(p,paragraph.join('\n'));body.append(p);paragraph=[];}list=null;};
    for(let i=0;i<lines.length;i++){
      const line=lines[i];
      if(/^\s*```/.test(line)){flush();const code=[];while(++i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i]);const block=chatNode('div','tl-chat-code'),pre=chatNode('pre'),text=code.join('\n');pre.append(chatNode('code','',text));block.append(chatCopy(text,'Copia codice'),pre);body.append(block);continue;}
      if(!line.trim()){flush();continue;}
      const headers=tableCells(line),separators=i+1<lines.length?tableCells(lines[i+1]):null;
      if(headers&&separators&&headers.length===separators.length&&separators.every(cell=>/^:?-+:?$/.test(cell))){
        flush();
        const wrap=chatNode('div','tl-chat-table-wrap'),table=chatNode('table','tl-chat-table'),head=chatNode('thead'),rows=chatNode('tbody');
        wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','Tabella: scorri orizzontalmente per leggere tutte le colonne');
        const align=separators.map(cell=>cell.endsWith(':')?(cell.startsWith(':')?'center':'right'):'left');
        const row=(cells,header=false)=>{const tr=chatNode('tr');for(let column=0;column<headers.length;column++){const cell=chatNode(header?'th':'td');if(header)cell.scope='col';cell.style.textAlign=align[column];chatInline(cell,cells[column]||'');tr.append(cell);}return tr;};
        head.append(row(headers,true));
        i+=2;
        for(;i<lines.length;i++){
          if(/^(?:\s*```|#{1,6}\s+|\s*(?:[-*]|\d+\.)\s+)/.test(lines[i]))break;
          const cells=tableCells(lines[i]);if(!cells)break;rows.append(row(cells));
        }
        i--;table.append(head,rows);wrap.append(table);body.append(wrap);continue;
      }
      const heading=line.match(/^#{1,6}\s+(.+)$/),item=line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)$/);
      if(heading){flush();const h=chatNode('h3');chatInline(h,heading[1]);body.append(h);}
      else if(item){if(!list){flush();list=chatNode(/^\s*\d+\./.test(line)?'ol':'ul');body.append(list);}const li=chatNode('li');chatInline(li,item[1]);list.append(li);}
      else{if(list)flush();paragraph.push(line);}
    }
    flush();return body;
  }
  function renderActions(group,existing){
    const details=existing||chatNode('details','tl-chat-actions');
    details.dataset.id=String(group.id);
    if(!existing){details.append(chatNode('summary','tl-chat-actions-summary'),chatNode('ol','tl-chat-action-list'));}
    const signature=JSON.stringify(group.actions);
    if(details.dataset.signature!==signature){
      const actions=group.actions.filter(action=>action&&typeof action.label==='string');
      const errors=actions.filter(action=>action.status==='error').length;
      details.firstElementChild.textContent=`⚙️ ${actions.length} ${actions.length===1?'azione':'azioni'}`+(errors?` · ${errors} ${errors===1?'errore':'errori'}`:'');
      const list=details.lastElementChild;
      const previous=new Map([...list.children].map(node=>[node.dataset.id,node])),keep=new Set();
      for(const [index,action] of actions.entries()){
        const id=String(action.id),actionSignature=JSON.stringify(action);let item=previous.get(id);
        if(!item){item=chatNode('li','tl-chat-action');item.dataset.id=id;}
        if(item.dataset.signature!==actionSignature){
          const contentSignature=JSON.stringify([action.icon,action.label,action.tool,action.detail,action.detail_truncated]);
          if(item.dataset.contentSignature!==contentSignature){
            const detailOpen=item.querySelector('.tl-chat-action-detail')?.open===true;
            const heading=chatNode('div','tl-chat-action-heading');
            const icon=chatNode('span','tl-chat-action-icon',typeof action.icon==='string'?action.icon:'🔧');icon.setAttribute('aria-hidden','true');
            heading.append(icon,chatNode('span','tl-chat-action-label',action.label),chatNode('span','tl-chat-action-state'));
            item.replaceChildren(heading);
            if(typeof action.tool==='string'&&action.tool){const tool=chatNode('p','tl-chat-action-tool','Strumento: ');tool.append(chatNode('code','',action.tool));item.append(tool);}
            if(typeof action.detail==='string'&&action.detail){
              const detail=chatNode('details','tl-chat-action-detail'),pre=chatNode('pre');detail.open=detailOpen;
              pre.append(chatNode('code','',action.detail));detail.append(chatNode('summary','','Dettagli'),chatCopy(action.detail,'Copia dettagli azione'),pre);
              if(action.detail_truncated===true)detail.append(chatNode('small','tl-chat-action-truncated','Dettagli abbreviati'));
              item.append(detail);
            }
            item.dataset.contentSignature=contentSignature;
          }
          const state={started:'Avviata',completed:'Esito ricevuto',error:'Errore'}[action.status];
          const stateNode=item.querySelector('.tl-chat-action-state');stateNode.textContent=state||'';stateNode.hidden=!state;
          item.dataset.status=state?action.status:'';
          item.dataset.signature=actionSignature;
        }
        keep.add(item);if(list.children[index]!==item)list.insertBefore(item,list.children[index]||null);
      }
      for(const item of [...list.children])if(!keep.has(item))item.remove();
      details.dataset.signature=signature;
    }
    return details;
  }
  function messageImages(images,scope){
    if(!Array.isArray(images)||!scope||!Number.isSafeInteger(scope.target.pane)||scope.target.pane<0||
       typeof scope.target.identity!=='string'||!scope.target.identity||scope.target.identity.length>200||
       typeof scope.conversation!=='string'||!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(scope.conversation))return null;
    const gallery=chatNode('div','tl-chat-images');
    for(const media of images.slice(0,8)){
      if(!media||typeof media.id!=='string'||!/^[a-f0-9]{64}$/.test(media.id))continue;
      const alt=(typeof media.alt==='string'?media.alt.replace(/[\u0000-\u001f\u007f]/g,'').slice(0,160):'')||'Immagine della conversazione';
      const query=new URLSearchParams({identity:scope.target.identity,conversation_id:scope.conversation});
      const href=`/api/panes/${scope.target.pane}/chat/images/${media.id}?${query}`;
      const figure=chatNode('figure','tl-chat-media'),link=chatNode('a'),picture=chatNode('img');
      const caption=chatNode('span','tl-chat-media-caption','Apri immagine');
      link.href=href;link.target='_blank';link.rel='noopener noreferrer';link.setAttribute('aria-label','Apri immagine: '+alt);
      picture.alt=alt;picture.loading='lazy';picture.decoding='async';picture.referrerPolicy='no-referrer';
      picture.addEventListener('error',()=>{picture.hidden=true;caption.textContent='Immagine non disponibile';});
      picture.src=href;link.append(picture,caption);figure.append(link);gallery.append(figure);
    }
    return gallery.children.length?gallery:null;
  }
  function messageNode(message,engine,existing,mediaScope){
    const text=typeof message.text==='string'?message.text:'';
    const notice=typeof message.media_notice==='string'?message.media_notice.slice(0,300):'';
    const signature=JSON.stringify([message.role,text,message.timestamp,engine,message.delivery,message.images,notice,
      mediaScope?[mediaScope.target.pane,mediaScope.target.identity,mediaScope.conversation]:null]);
    const article=existing||chatNode('article','tl-chat-message '+message.role+(message.delivery?' local':''));article.dataset.id=String(message.id);
    if(article.dataset.signature!==signature){
      const byline=chatNode('div','tl-chat-byline',message.role==='media'?'Immagine dello strumento':message.role==='user'?'Tu':engine==='codex'?'Codex':'Claude');
      if(message.timestamp){const date=new Date(message.timestamp);if(!Number.isNaN(date.getTime())){const time=chatNode('time','',date.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'}));time.dateTime=date.toISOString();time.title=date.toLocaleString('it-IT');byline.append(time);}}
      if(text)byline.append(chatCopy(text,message.role==='user'?'Copia il tuo messaggio':'Copia messaggio di '+(engine==='codex'?'Codex':'Claude')));
      article.replaceChildren(byline,chatMarkdown(text));
      const images=messageImages(message.images,mediaScope);if(images)article.append(images);
      if(notice)article.append(chatNode('p','tl-chat-media-notice',notice));
      if(message.delivery){
        const labels={pending:'Invio in corso…',confirmed:'Inviato al terminale',uncertain:'Invio da verificare',failed:'Invio non riuscito',observed:'Preso in carico'};
        const badge=chatNode('p','tl-chat-delivery',labels[message.delivery]);badge.setAttribute('role','status');
        badge.title=message.delivery==='confirmed'?'La presa in carico verrà confermata quando il messaggio compare nella conversazione.':message.delivery==='uncertain'?'Controlla la conversazione o il terminale prima di riprovare.':'';
        article.append(badge);article.dataset.delivery=message.delivery;
      }
      article.dataset.signature=signature;
    }
    return article;
  }
  function reconcile(data,target){
    if(data.available!==true||!data.transcript_identity)return;
    const key=scopeKey(target,data),used=consumed.get(key)||new Set();consumed.set(key,used);
    for(const message of data.messages||[]){
      if(message?.role!=='user'||typeof message.text!=='string'||!Number.isSafeInteger(message.source_offset)||used.has(message.id))continue;
      // Queued prompts are acknowledged against their original enqueue, not
      // the later row where the CLI consumes them. Null means unverified.
      const inputOffset=Object.hasOwn(message,'delivery_offset')?message.delivery_offset:message.source_offset;
      if(!Number.isSafeInteger(inputOffset))continue;
      for(const record of deliveries.values()){
        const receipt=record.receipt;
        if(record.key!==key||record.observed||record.status!=='confirmed'||!receipt||receipt.transcript_identity!==data.transcript_identity||
           inputOffset<=receipt.before_offset||receipt.baseline.includes(message.id)||normalize(receipt.text)!==normalize(message.text))continue;
        record.observed=String(message.id);record.status='observed';used.add(message.id);break;
      }
    }
  }
  function renderLocal(data,target){
    const key=scopeKey(target,data),existing=new Map([...chatView.querySelectorAll(':scope > .local')].map(node=>[node.dataset.id,node]));
    const visible=new Set([...chatView.children].filter(node=>!node.classList.contains('local')).map(node=>node.dataset.id)),keep=new Set();
    if(data.available===true)for(const record of deliveries.values()){
      if(record.observed&&visible.has(record.observed)){deliveries.delete(record.id);continue;}
      if(record.key!==key)continue;
      const id='local:'+record.id,node=messageNode({id,role:'user',text:record.text,timestamp:record.timestamp,delivery:record.status},data.engine,existing.get(id));
      if(!node.isConnected)chatView.append(node);keep.add(node);
    }
    for(const node of existing.values())if(!keep.has(node))node.remove();
    if(keep.size)chatView.querySelector('.tl-chat-empty')?.remove();
  }
  function delivery(event){
    if(!event||typeof event.id!=='string'||typeof event.text!=='string'||!event.target)return;
    let record=deliveries.get(event.id);
    if(event.status==='pending'){
      const target=context();
      if(record||!chatReady||!lastData||!sameTarget(readyFor,target)||!samePane(event.target,target))return;
      const imageCount=Number.isInteger(event.imageCount)&&event.imageCount>=0&&event.imageCount<=5?event.imageCount:event.hasImage?1:0;
      record={id:event.id,key:scopeKey(target,lastData),target:{...event.target},conversation:lastData.conversation_id,engine:lastData.engine,
        text:normalize(event.text)+(imageCount?(event.text.trim()?'\n\n':'')+Array(imageCount).fill('[Immagine allegata]').join('\n\n'):''),timestamp:new Date().toISOString(),status:'pending'};
      deliveries.set(event.id,record);
    }else{
      if(!record||record.observed||!samePane(record.target,event.target)||!['confirmed','uncertain','failed'].includes(event.status))return;
      record.status=event.status;
      const receipt=event.receipt;
      if(event.status==='confirmed'&&receipt&&receipt.conversation_id===record.conversation&&receipt.engine===record.engine&&receipt.identity===record.target.identity&&
         Number.isSafeInteger(receipt.before_offset)&&typeof receipt.transcript_identity==='string'&&Array.isArray(receipt.baseline)&&typeof receipt.text==='string'){
        record.receipt=receipt;record.text=receipt.text;
      }
    }
    const target=context();
    if(lastData&&sameTarget(readyFor,target)){
      reconcile(lastData,target);const bottom=chatView.scrollHeight-chatView.scrollTop-chatView.clientHeight<40;
      renderLocal(lastData,target);if(event.status==='pending'||bottom)chatView.scrollTop=chatView.scrollHeight;updateChatLatest();
    }
  }
  function updateChatLatest(){chatLatest.hidden=!context()||chatView.scrollHeight-chatView.scrollTop-chatView.clientHeight<40;}
  chatView.addEventListener('scroll',updateChatLatest);
  chatView.addEventListener('toggle',updateChatLatest,true);
  chatLatest.onclick=()=>{chatView.scrollTop=chatView.scrollHeight;updateChatLatest();};
  function reset(){generation++;chatReady=false;readyFor=null;chatRequest=null;chatConversation=null;lastData=null;chatView.replaceChildren(chatNode('p','tl-chat-empty','Caricamento conversazione…'));status.textContent='';chatLatest.hidden=true;onChange();}
  function renderChat(data,follow,target){
    const changed=!lastData||!readyFor||scopeKey(readyFor,lastData)!==scopeKey(target,data)||
      (lastData.transcript_identity||null)!==(data.transcript_identity||null)||
      (Number.isSafeInteger(lastData.cursor)&&Number.isSafeInteger(data.cursor)&&data.cursor<lastData.cursor);
    lastData=data;reconcile(data,target);
    const selected=getSelection();
    const selectedNodes=new Set(data.available===true&&!changed&&selected&&!selected.isCollapsed?
      [...chatView.children].filter(node=>selected.containsNode(node,true)):[]);
    const top=chatView.scrollTop,atBottom=follow||changed||(!selectedNodes.size&&chatView.scrollHeight-top-chatView.clientHeight<40);
    chatConversation=data.conversation_id;chatReady=data.available===true;readyFor={...target};
    const messages=(Array.isArray(data.timeline)?data.timeline:data.messages||[]).filter(m=>m&&(['user','assistant'].includes(m.role)&&typeof m.text==='string'||m.role==='actions'&&Array.isArray(m.actions)&&m.actions.length||m.role==='media'&&(Array.isArray(m.images)&&m.images.length||typeof m.media_notice==='string'&&m.media_notice)));
    if(!chatReady){chatView.replaceChildren(chatNode('p','tl-chat-empty',data.reason||'Conversazione non disponibile. Apri la vista Terminale.'));}
    else{
      const existing=new Map([...chatView.children].filter(node=>node.dataset.id!==undefined).map(node=>[node.dataset.id,node]));
      const keep=new Set(),messageIds=new Set(messages.map(message=>String(message.id)));let cursor=chatView.firstElementChild;
      for(const message of messages){
        const id=String(message.id);let article=changed?null:existing.get(id);
        // Defer only the selected node; unrelated replies can keep arriving.
        if(selectedNodes.has(article)){
          keep.add(article);
          if(cursor&&(cursor===article||cursor.compareDocumentPosition(article)&Node.DOCUMENT_POSITION_FOLLOWING))cursor=article.nextElementSibling;
          continue;
        }
        article=message.role==='actions'?renderActions(message,article?.tagName==='DETAILS'?article:null):messageNode(message,data.engine,article,{target,conversation:data.conversation_id});
        keep.add(article);
        while(cursor&&selectedNodes.has(cursor)&&!messageIds.has(cursor.dataset.id))cursor=cursor.nextElementSibling;
        if(cursor!==article)chatView.insertBefore(article,cursor);
        cursor=article.nextElementSibling;
      }
      for(const node of [...chatView.children])if(!keep.has(node)&&!selectedNodes.has(node)&&!node.classList.contains('local'))node.remove();
    }
    renderLocal(data,target);
    if(chatReady&&!chatView.children.length)chatView.append(chatNode('p','tl-chat-empty','La conversazione è pronta. Scrivi il primo messaggio.'));
    chatView.scrollTop=atBottom?chatView.scrollHeight:top;updateChatLatest();
    status.textContent=(data.truncated?'Messaggi recenti · ':'')+(data.status==='working'?'Al lavoro…':data.status==='waiting'?'In attesa di risposta':chatReady?'Aggiornato '+new Date().toLocaleTimeString('it-IT'):'Usa Terminale per continuare');
    if(chatReady&&typeof data.media_notice==='string'&&data.media_notice)status.textContent+=' · '+data.media_notice.slice(0,300);
    onChange();
  }
  async function refresh(follow=false){
    const target=context();updateChatLatest();
    if(!target||(chatRequest?.generation===generation&&sameTarget(chatRequest.target,target)))return;
    const operation={generation,target:{...target}};chatRequest=operation;
    const current=()=>chatRequest===operation&&operation.generation===generation&&sameTarget(operation.target,context());
    try{const data=await fetchData(operation.target);if(current())renderChat(data,follow,operation.target);}
    catch(error){if(current()){chatReady=false;readyFor=null;status.textContent=error.message+' · Aggiorna per riprovare.';if(error.definiteRejection||[404,409].includes(error.status))chatView.replaceChildren(chatNode('p','tl-chat-empty','Riapri la sessione per aggiornare la conversazione.'));onChange();}}
    finally{if(chatRequest===operation)chatRequest=null;}
  }
  return {reset,refresh,delivery,get ready(){return chatReady&&sameTarget(readyFor,context());},get scope(){return chatReady&&sameTarget(readyFor,context())?{conversation_id:chatConversation,engine:lastData?.engine}:null;}};
  }
  window.SessionChat={create};
})();
