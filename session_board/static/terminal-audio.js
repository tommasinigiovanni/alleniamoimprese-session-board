/* Dictation stays on this device until it can become a reviewed draft. */
(() => {
  'use strict';
  const MAX_SECONDS=120, MAX_BYTES=15*1024*1024, MAX_TEXT=4000;
  const TARGET_FIELDS=['pane','identity','session','host','key','conversation_id','chat_conversation','engine'];
  const SCOPE_FIELDS=TARGET_FIELDS.filter(field=>field!=='key');
  const MIME_TYPES=['audio/webm;codecs=opus','audio/webm','audio/mp4;codecs=mp4a.40.2','audio/mp4'];
  const RETRIES=[2000,5000,15000];
  let database, nextNoticeId=0;

  function openDatabase() {
    if (!database) database=new Promise((resolve,reject)=>{
      const request=indexedDB.open('session-board-audio-v1',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('recordings',{keyPath:'id'});
      request.onsuccess=()=>resolve(request.result);
      request.onerror=()=>reject(request.error);
      request.onblocked=()=>reject(new Error('Archivio audio non disponibile'));
    });
    return database;
  }
  async function records() {
    const db=await openDatabase();
    return new Promise((resolve,reject)=>{
      const transaction=db.transaction('recordings','readonly'),request=transaction.objectStore('recordings').getAll();
      transaction.oncomplete=()=>resolve(request.result);
      transaction.onabort=transaction.onerror=()=>reject(transaction.error);
    });
  }
  async function store(row,existingOnly=false) {
    const db=await openDatabase();
    return new Promise((resolve,reject)=>{
      const transaction=db.transaction('recordings','readwrite'),table=transaction.objectStore('recordings');
      let failure,written=false;
      const request=table.getAll();
      request.onsuccess=()=>{
        // A deletion in another tab wins over an outstanding upload response.
        // The existence check and update share the same write transaction.
        if(existingOnly&&!request.result.some(item=>item.id===row.id))return;
        const others=request.result.filter(item=>item.id!==row.id);
        if (others.length>=10 || others.reduce((total,item)=>total+(item.blob?.size||0),0)+row.blob.size>50*1024*1024) {
          failure=new Error('Archivio audio pieno');transaction.abort();return;
        }
        try{table.put(row);written=true;}catch(error){failure=error;transaction.abort();}
      };
      // A request success alone does not mean the transaction was committed.
      transaction.oncomplete=()=>resolve(written);
      transaction.onabort=transaction.onerror=()=>reject(failure||transaction.error);
    });
  }
  async function remove(id) {
    const db=await openDatabase();
    return new Promise((resolve,reject)=>{
      const transaction=db.transaction('recordings','readwrite');
      transaction.objectStore('recordings').delete(id);
      transaction.oncomplete=resolve;
      transaction.onabort=transaction.onerror=()=>reject(transaction.error);
    });
  }
  const stamp=(target,fields=TARGET_FIELDS)=>target?JSON.stringify(fields.map(field=>target[field]??null)):null;

  function create({form,input,result,context,transcribe,prepare=()=>{},canRecord=()=>true,
                   canTranscribe=()=>true,onDraft=()=>false,availability=()=>({available:true}),onBusy=()=>{}}) {
    const controls=document.createElement('div');controls.className='terminal-audio-controls';
    controls.innerHTML='<button type="button" class="terminal-audio-record" aria-label="Registra messaggio" title="Registra messaggio">🎙️</button><output class="terminal-audio-timer" aria-label="Durata registrazione" hidden></output><button type="button" class="terminal-audio-cancel" aria-label="Annulla registrazione" hidden>Annulla</button>';
    const actions=form.querySelector('.terminal-image-actions');
    if(actions)actions.insertBefore(controls,actions.querySelector('span'));
    else form.insertBefore(controls,form.querySelector('button[type="submit"]'));
    const record=controls.querySelector('.terminal-audio-record'),timer=controls.querySelector('.terminal-audio-timer'),cancel=controls.querySelector('.terminal-audio-cancel');
    const unavailable=document.createElement('p');unavailable.className='terminal-audio-unavailable';
    unavailable.id='terminal-audio-unavailable-'+(++nextNoticeId);unavailable.hidden=true;controls.append(unavailable);
    const recovery=document.createElement('div');recovery.className='terminal-audio-recovery';recovery.hidden=true;
    recovery.innerHTML='<p role="status"></p><textarea readonly aria-label="Trascrizione da recuperare" hidden></textarea><button type="button" data-audio-action="retry">Riprova trascrizione</button><button type="button" data-audio-action="apply" hidden>Aggiungi alla bozza</button><button type="button" data-audio-action="copy" hidden>Copia trascrizione</button><button type="button" data-audio-action="download">Scarica audio</button><button type="button" data-audio-action="delete">Elimina audio salvato</button>';
    form.append(recovery);
    const archive=document.createElement('details');archive.className='terminal-audio-archive';archive.hidden=true;
    const archiveTitle=document.createElement('summary'),archiveList=document.createElement('div');archive.append(archiveTitle,archiveList);form.append(archive);
    const recovered=recovery.querySelector('textarea'),notice=recovery.querySelector('p');
    const button=name=>recovery.querySelector('[data-audio-action="'+name+'"]');
    const memory=new Map(),ignored=new Set(),archiveRows=new Map();
    let active=null,generation=0,busy=false,loading=false,loadedTarget=null,leaving=false;

    const supported=()=>window.isSecureContext&&typeof navigator.mediaDevices?.getUserMedia==='function'&&typeof window.MediaRecorder==='function';
    const allowed=()=>supported()&&availability()?.available!==false&&!!context()&&canRecord()&&!input.readOnly&&!input.disabled&&!!input.getClientRects().length;
    const sameTarget=run=>!!context()&&stamp(context())===stamp(run.target);
    const attached=run=>active===run&&!run.cancelled&&!run.detached&&sameTarget(run);
    const busyRun=run=>!!run&&['requesting','recording','stopping','saving','transcribing'].includes(run.state);
    const canUpload=run=>attached(run)&&!leaving&&!document.hidden&&navigator.onLine!==false&&canTranscribe()&&!input.readOnly&&!input.disabled;
    function download(row){
      const url=URL.createObjectURL(row.blob),link=document.createElement('a');
      link.href=url;link.download=row.blob.type.includes('mp4')?'registrazione.m4a':'registrazione.webm';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    function paintArchive(){
      const rows=new Map(archiveRows);for(const [id,run] of memory)if(run.state!=='saving')rows.set(id,run.row);
      const others=[...rows.values()].filter(row=>row.id!==active?.row?.id&&!ignored.has(row.id));
      archive.hidden=!context()||!others.length;archiveTitle.textContent='Altri audio salvati ('+others.length+')';
      if(archive.hidden){archiveList.replaceChildren();return;}
      const current=new Map([...archiveList.children].map(node=>[node.dataset.id,node]));
      for(const row of others){
        if(current.has(row.id)){current.delete(row.id);continue;}
        const item=document.createElement('div'),label=document.createElement('span'),save=document.createElement('button'),erase=document.createElement('button');
        item.dataset.id=row.id;label.textContent=(row.target?.session||'Sessione precedente')+' · Pannello '+(row.target?.pane??'?')+' · '+new Date(row.created).toLocaleString('it-IT');
        save.type=erase.type='button';save.textContent='Scarica audio';erase.textContent='Elimina audio salvato';
        save.onclick=()=>download(row);
        erase.onclick=async()=>{
          const ticket=generation,target=stamp(context());
          erase.disabled=true;
          try{
            const cached=memory.get(row.id);
            if(cached){cached.cancelled=true;cached.abort.abort();await cached.saving;}
            if(archiveRows.has(row.id)||cached?.stored)await remove(row.id);
            memory.delete(row.id);archiveRows.delete(row.id);ignored.add(row.id);
            if(active?.row?.id===row.id){active.cancelled=true;active.abort.abort();end(active,'Audio eliminato dal dispositivo.');}
            paintArchive();
          }catch(_){erase.disabled=false;if(ticket===generation&&stamp(context())===target)result.textContent='Impossibile eliminare l’audio salvato. Riprova.';}
        };
        item.append(label,save,erase);archiveList.append(item);
      }
      for(const item of current.values())item.remove();
    }
    function paint() {
      const state=active?.state,isRecording=state==='recording',nowBusy=busyRun(active);
      record.textContent=isRecording?'■':'🎙️';
      record.setAttribute('aria-label',isRecording?'Ferma e trascrivi':'Registra messaggio');
      record.setAttribute('aria-pressed',String(isRecording));
      const backend=availability(),reason=backend?.available===false?(backend.reason||'Trascrizione non disponibile.'):!supported()?'Il microfono richiede HTTPS e un browser compatibile.':'';
      record.title=reason||(isRecording?'Ferma e trascrivi':'Registra messaggio');
      unavailable.hidden=!reason||!!active;unavailable.textContent=reason;
      controls.classList.toggle('is-unavailable',!unavailable.hidden);
      if(unavailable.hidden)record.removeAttribute('aria-describedby');else record.setAttribute('aria-describedby',unavailable.id);
      record.disabled=active?!isRecording:loading||!allowed();
      cancel.hidden=!nowBusy;timer.hidden=!active;controls.dataset.state=state||'idle';
      if(state==='requesting')timer.textContent='Microfono…';
      else if(state==='saving'||state==='stopping')timer.textContent='Salvataggio audio…';
      else if(state==='transcribing')timer.textContent='Trascrizione…';
      else if(isRecording){const elapsed=Math.min(MAX_SECONDS,Math.floor((performance.now()-active.started)/1000));timer.textContent=Math.floor(elapsed/60)+':'+String(elapsed%60).padStart(2,'0');}
      else timer.textContent=state==='ready'?'Trascrizione pronta':'Audio da recuperare';
      const recoverable=!!active?.row&&!nowBusy;
      recovery.hidden=!recoverable;
      if(recoverable){
        const text=active.row.text||'';
        notice.textContent=active.durable?(text?'Trascrizione conservata su questo dispositivo.':'Audio conservato su questo dispositivo. Riproveremo quando la connessione sarà disponibile.'):'Audio solo in memoria: il salvataggio sul dispositivo non è riuscito. Scaricalo prima di chiudere la pagina.';
        if(active.durable&&!text&&(active.state==='blocked'||active.attempts>=RETRIES.length&&!active.retry||active.restored&&!navigator.locks))notice.textContent='Audio conservato su questo dispositivo. Puoi riprovare manualmente o scaricarlo.';
        recovered.value=text;recovered.hidden=!text;
        button('retry').hidden=!!text;button('retry').disabled=!canUpload(active)||active.inFlight;
        button('apply').hidden=!text;button('apply').disabled=!canUpload(active);
        button('apply').textContent=active.applied?'Salva bozza':'Aggiungi alla bozza';
        button('copy').hidden=!text;
      }
      paintArchive();
      if(nowBusy!==busy){busy=nowBusy;onBusy(busy);}
    }
    function stopTracks(run){if(!run.stream||run.tracksStopped)return;run.tracksStopped=true;for(const track of run.stream.getTracks())track.stop();}
    function clearTimers(run){clearInterval(run.ticker);clearTimeout(run.limit);clearTimeout(run.retry);run.retry=null;}
    function end(run,message){
      clearTimers(run);stopTracks(run);
      if(active!==run)return;
      active=null;loadedTarget=stamp(context());
      if(message!==undefined)result.textContent=message;
      paint();
    }
    async function persist(run){
      memory.set(run.row.id,run);
      const row={...run.row};
      const operation=(run.saving||Promise.resolve()).then(async()=>{
        if(run.cancelled)return false;
        try{
          const written=await store(row,run.stored===true);
          if(!written){
            run.cancelled=true;memory.delete(row.id);archiveRows.delete(row.id);ignored.add(row.id);run.abort.abort();
            end(run,'Audio eliminato in un’altra scheda.');return false;
          }
          run.durable=true;run.stored=true;memory.delete(row.id);archiveRows.set(row.id,row);return true;
        }
        catch(_){run.durable=false;return false;}
      });
      run.saving=operation;return operation;
    }
    async function discard(run,message='Registrazione annullata.'){
      const ticket=generation;
      run.cancelled=true;ignored.add(run.row?.id);run.abort.abort();clearTimers(run);
      if(run.recorder?.state!=='inactive'){try{run.recorder?.stop();}catch(_){}}
      stopTracks(run);run.chunks=[];
      end(run,message);
      await run.saving;
      if(run.row){memory.delete(run.row.id);try{if(run.stored)await remove(run.row.id);archiveRows.delete(run.row.id);paintArchive();}catch(_){
        ignored.delete(run.row.id);archiveRows.set(run.row.id,run.row);paintArchive();
        if(ticket===generation&&!active&&stamp(context(),SCOPE_FIELDS)===run.row.scope)result.textContent='Impossibile eliminare l’audio salvato. Riprova da Altri audio salvati.';
      }}
    }
    function reset(){
      generation++;loadedTarget=null;loading=false;
      const run=active;active=null;
      if(run){
        run.detached=true;clearTimers(run);run.abort.abort();
        if(run.state==='requesting')run.cancelled=true;
        if(run.recorder?.state==='recording'){
          run.state='stopping';try{run.recorder.stop();}catch(_){run.cancelled=true;}
        }
        stopTracks(run);
      }
      paint();
    }
    function live(run){if(!attached(run)){if(active===run)reset();return false;}return true;}
    function recoveredRun(row){return {target:{...context()},state:row.text?'ready':row.blocked?'blocked':'queued',row,durable:true,stored:true,abort:new AbortController(),chunks:[],attempts:0,restored:true};}
    async function restore(){
      const target=context(),key=stamp(target),ticket=generation;
      if(active||loading||!target||loadedTarget===key)return;
      loading=true;paint();
      let saved=[],read=false;
      try{saved=await records();read=true;}catch(_){/* Memory recovery remains usable when storage is unavailable. */}
      if(ticket!==generation||stamp(context())!==key){loading=false;return;}
      loadedTarget=key;loading=false;
      if(read){archiveRows.clear();for(const row of saved)archiveRows.set(row.id,row);}
      const combined=new Map(saved.map(row=>[row.id,row]));
      for(const [id,run] of memory){
        if(read&&run.stored&&!combined.has(id)){memory.delete(id);continue;}
        // An unfinished write will invalidate loadedTarget on completion.
        if(run.state!=='saving')combined.set(id,run.row);
      }
      const row=[...combined.values()].filter(item=>!ignored.has(item.id)&&item.scope===stamp(target,SCOPE_FIELDS)).sort((a,b)=>a.created-b.created)[0];
      if(row&&!active){
        const cached=memory.get(row.id),stored=saved.some(item=>item.id===row.id);
        active=recoveredRun(row);
        // A saved Blob does not make a newer, memory-only transcript durable.
        active.durable=cached&&cached.state!=='saving'?cached.durable:stored;
        active.stored=stored||cached?.stored===true;
      }
      paint();
      if(active?.state==='queued'&&navigator.locks)attempt(active);
    }
    function update(){
      if(active&&!sameTarget(active))reset();
      paint();
      if(!active)restore();
      else if(active.state==='queued'&&!active.retry&&!active.inFlight&&(!active.attempts||active.waitingForContext)&&(!active.restored||navigator.locks))attempt(active);
    }
    function queued(run,message){
      run.state=run.row.blocked?'blocked':'queued';
      if(attached(run)){result.textContent=message||(run.durable?'Audio conservato su questo dispositivo. In attesa di trascrizione.':'Audio solo in memoria. Scaricalo prima di chiudere la pagina.');paint();}
    }
    function schedule(run){
      if(!attached(run)||run.row.blocked||run.attempts>=RETRIES.length)return;
      run.retry=setTimeout(()=>{run.retry=null;attempt(run);},RETRIES[run.attempts++]);
    }
    async function apply(run){
      if(!attached(run)||!run.row.text||!canUpload(run))return;
      if(!run.durable){
        const saved=await persist(run);
        if(!attached(run))return;
        if(!saved){result.textContent='Salvataggio locale non riuscito. Copia il testo per recuperarlo.';paint();return;}
      }
      if(run.stored){
        try{
          if(!(await records()).some(row=>row.id===run.row.id)){memory.delete(run.row.id);archiveRows.delete(run.row.id);end(run,'Audio eliminato in un’altra scheda.');return;}
        }catch(_){if(attached(run))result.textContent='Impossibile verificare il recupero salvato. Riprova o copia il testo.';return;}
        if(!attached(run))return;
      }
      const text=run.row.text;
      if(!run.applied){
        const draft=input.value,combined=draft+(draft&&!/\s$/.test(draft)?'\n':'')+text;
        const maxLength=input.maxLength>0?Math.min(MAX_TEXT,input.maxLength):MAX_TEXT;
        if(combined.length>maxLength){result.textContent='La bozza con la trascrizione supera '+maxLength+' caratteri. Copia il testo completo dal riquadro.';paint();return;}
        run.applied=true;input.value=combined;input.dispatchEvent(new Event('input',{bubbles:true}));
      }
      let saved=false;try{saved=onDraft(input.value)===true;}catch(_){}
      if(!attached(run))return;
      if(saved){
        try{if(run.stored)await remove(run.row.id);memory.delete(run.row.id);archiveRows.delete(run.row.id);ignored.add(run.row.id);}
        catch(_){if(attached(run)){result.textContent='Trascrizione aggiunta alla bozza. La copia di recupero resta sul dispositivo.';paint();}return;}
        if(attached(run))end(run,'Trascrizione aggiunta alla bozza. Rileggi prima di inviare.');
      }else{result.textContent='Trascrizione aggiunta alla bozza. Il salvataggio della bozza non è riuscito; la trascrizione resta da recuperare.';paint();}
    }
    async function upload(run,manual){
      if(run.durable){
        const current=(await records()).find(row=>row.id===run.row.id);
        if(!current){memory.delete(run.row.id);archiveRows.delete(run.row.id);if(attached(run))end(run);return;}
        if(current.text){run.row=current;run.state='ready';if(attached(run))paint();return;}
        if(current.blocked&&!manual){run.row=current;queued(run);return;}
      }
      if(!canUpload(run)){if(attached(run))queued(run);return;}
      run.state='transcribing';run.abort=new AbortController();paint();
      try{
        const response=await transcribe(run.row.blob,{signal:run.abort.signal});
        if(run.cancelled)return;
        const text=typeof response?.text==='string'?response.text.trim():'';
        if(!text)throw Object.assign(new Error('Nessuna parola riconosciuta. Puoi riprovare o eliminare l’audio.'),{status:422});
        run.row={...run.row,text,blocked:false};
        const durable=await persist(run);
        if(!live(run))return;
        run.state='ready';paint();
        // Reloaded text is always offered explicitly, including after a crash
        // between saving the draft and removing this recovery record.
        if(durable)await apply(run);
        else result.textContent='Trascrizione pronta, ma il salvataggio locale non è riuscito. Recupera il testo dal riquadro.';
      }catch(error){
        if(run.cancelled||run.detached)return;
        const status=Number(error?.status);
        run.row.blocked=!!status&&!([408,429].includes(status)||status>=500);
        await persist(run);
        if(!live(run))return;
        queued(run,(run.durable?'Audio conservato su questo dispositivo. ':'Audio solo in memoria: scaricalo prima di chiudere. ')+(run.row.blocked?(error.message||'Trascrizione non riuscita'):'Connessione o trascrizione non disponibile; riproveremo.'));
        schedule(run);
      }
    }
    async function attempt(run,manual=false){
      if(run.inFlight||!run.row||run.row.text||(!manual&&run.row.blocked))return;
      if(!canUpload(run)){run.waitingForContext=true;return;}
      run.waitingForContext=false;
      run.inFlight=true;
      try{
        if(navigator.locks){
          await navigator.locks.request('session-board-audio:'+run.row.id,{ifAvailable:true},async lock=>{
            if(lock)await upload(run,manual);else{queued(run);schedule(run);}
          });
        }else if(manual||!run.restored)await upload(run,manual);
      }catch(_){if(attached(run)){queued(run);schedule(run);}}
      finally{run.inFlight=false;if(attached(run))paint();}
    }
    async function complete(run){
      clearTimers(run);stopTracks(run);
      if(run.cancelled)return;
      if(!run.bytes){if(attached(run))end(run,'La registrazione è vuota. Riprova.');return;}
      run.state='saving';if(attached(run))paint();
      run.row={id:crypto.randomUUID(),scope:stamp(run.target,SCOPE_FIELDS),target:Object.fromEntries(SCOPE_FIELDS.map(field=>[field,run.target[field]??null])),created:Date.now(),blob:new Blob(run.chunks,{type:run.recorder.mimeType||run.mime}),blocked:false};
      run.chunks=[];
      await persist(run);
      if(run.cancelled)return;
      if(run.detached){
        run.state='queued';
        if(stamp(context(),SCOPE_FIELDS)===run.row.scope){loadedTarget=null;update();}else paintArchive();
        return;
      }
      if(!live(run))return;
      if(canUpload(run))attempt(run);else queued(run);
    }
    function stop(run){
      if(!live(run)||run.state!=='recording')return;
      run.state='stopping';clearTimers(run);paint();
      try{run.recorder.stop();stopTracks(run);}catch(_){discard(run,'Registrazione non riuscita. Riprova.');}
    }
    async function start(){
      if(active||loading||!allowed())return;
      generation++;
      const run={target:{...context()},state:'requesting',chunks:[],bytes:0,attempts:0,abort:new AbortController()};
      active=run;result.textContent='';paint();
      try{
        run.stream=await navigator.mediaDevices.getUserMedia({audio:true});
        if(!live(run)){stopTracks(run);return;}
        run.mime=MIME_TYPES.find(type=>MediaRecorder.isTypeSupported?.(type))||'';
        run.recorder=new MediaRecorder(run.stream,run.mime?{mimeType:run.mime}:{});
        run.recorder.addEventListener('dataavailable',event=>{
          if(run.cancelled||!event.data?.size)return;
          run.bytes+=event.data.size;
          if(run.bytes>MAX_BYTES){discard(run,'La registrazione supera 15 MiB. Registra un messaggio più breve.');return;}
          run.chunks.push(event.data);
        });
        run.recorder.addEventListener('stop',()=>complete(run));
        run.recorder.addEventListener('error',()=>{if(attached(run))discard(run,'Registrazione non riuscita. Riprova.');});
        run.recorder.start(1000);run.state='recording';run.started=performance.now();
        run.ticker=setInterval(()=>{if(live(run))paint();},250);run.limit=setTimeout(()=>stop(run),MAX_SECONDS*1000);paint();
        Promise.resolve().then(()=>{if(attached(run)&&run.state==='recording')return prepare();}).catch(()=>{});
      }catch(error){
        if(!live(run)){stopTracks(run);return;}
        discard(run,error?.name==='NotAllowedError'?'Consenti l’accesso al microfono nel browser per registrare.':'Microfono non disponibile. Controlla il dispositivo e riprova.');
      }
    }
    record.addEventListener('click',()=>active?stop(active):start());
    cancel.addEventListener('click',()=>{if(active)discard(active);});
    button('delete').onclick=()=>{if(active)discard(active,'Audio eliminato dal dispositivo.');};
    button('retry').onclick=()=>{if(active){clearTimeout(active.retry);active.retry=null;active.attempts=0;attempt(active,true);}};
    button('apply').onclick=()=>{if(active)apply(active);};
    button('download').onclick=()=>{
      if(!active?.row)return;
      download(active.row);
    };
    button('copy').onclick=async()=>{
      if(!active?.row.text||recovery.hidden)return;
      const run=active,ticket=generation,text=run.row.text;recovered.focus();recovered.select();
      try{await navigator.clipboard.writeText(text);if(ticket===generation&&attached(run))result.textContent='Trascrizione copiata.';}
      catch(_){if(ticket===generation&&attached(run))result.textContent='Testo selezionato: usa Copia per recuperare la trascrizione.';}
    };
    window.addEventListener('online',()=>{if(active?.state==='queued'){clearTimeout(active.retry);active.retry=null;active.attempts=0;attempt(active);}else update();});
    document.addEventListener('visibilitychange',()=>{
      if(document.hidden){if(active?.state==='recording')stop(active);}
      else update();
    });
    window.addEventListener('pagehide',()=>{leaving=true;reset();});
    window.addEventListener('pageshow',()=>{leaving=false;update();});
    update();
    return {reset,update,get busy(){return busyRun(active);},getbusy:()=>busyRun(active)};
  }
  window.TerminalAudio={create};
})();
