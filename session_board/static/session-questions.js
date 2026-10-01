/* Verified CLI requests provide their own choices; the browser never infers them. */
(() => {
  'use strict';
  let instances=0;
  function create({container,context,fetchQuestion,answerQuestion,canAnswer=()=>true,onBusy=()=>{},onAnswered=()=>{},onChange=()=>{}}){
    const prefix='sq-'+(++instances);
    let generation=0,read=null,sending=null,target=null,question=null,expires=0;
    const locked=new Set();
    const same=(a,b)=>!!a&&!!b&&a.pane===b.pane&&a.identity===b.identity&&a.key===b.key;
    const node=(tag,className,text)=>{const item=document.createElement(tag);item.className=className;if(text!==undefined)item.textContent=text;return item;};
    const title=node('h3','sq-title'),previewLabel=node('p','sq-context-label','Operazione richiesta'),preview=node('pre','sq-context'),choices=node('div','sq-choices'),status=node('p','sq-status'),refreshButton=node('button','sq-refresh','Aggiorna richiesta');
    preview.setAttribute('aria-label','Operazione richiesta');previewLabel.hidden=true;preview.hidden=true;
    status.setAttribute('role','status');refreshButton.type='button';
    container.replaceChildren(title,previewLabel,preview,choices,status,refreshButton);container.hidden=true;
    const lockKey=(selected,challenge)=>JSON.stringify([selected?.pane,selected?.identity,selected?.key,challenge]);
    const awaiting=()=>question?.available===true&&same(target,context());
    const enabled=()=>awaiting()&&!sending&&canAnswer()&&question.allow_answer!==false&&typeof question.token==='string'&&question.token&&typeof question.challenge==='string'&&question.challenge&&Date.now()<expires&&!locked.has(lockKey(target,question.challenge));
    function update(){
      if(!same(target,context())){container.hidden=true;choices.querySelectorAll('button').forEach(button=>button.disabled=true);return;}
      choices.querySelectorAll('button').forEach(button=>button.disabled=!enabled());
      refreshButton.disabled=!!sending||!!read;
    }
    function reset(){
      generation++;read=null;question=null;target=null;expires=0;locked.clear();delete container.dataset.kind;
      const wasSending=!!sending;sending=null;container.hidden=true;choices.replaceChildren();preview.textContent='';preview.hidden=true;previewLabel.hidden=true;status.textContent='';
      if(wasSending)onBusy(false);onChange();
    }
    function renderChoice(button,choice,data){
      const multiple=data.kind==='choice';
      if(!multiple){
        if(button.textContent!==choice.label||button.dataset.kind==='choice')button.textContent=choice.label;
        button.dataset.kind='approval';
        for(const attribute of ['aria-labelledby','aria-describedby','aria-current'])button.removeAttribute(attribute);
        return;
      }
      const id=prefix+'-choice-'+choice.id;
      if(button.dataset.kind!=='choice'){
        const number=node('span','sq-choice-number'),label=node('span','sq-choice-label'),description=node('span','sq-choice-description'),current=node('span','sq-choice-current','Selezionata nel terminale');
        number.id=id+'-number';label.id=id+'-label';description.id=id+'-description';current.id=id+'-current';
        button.replaceChildren(number,label,description,current);
        button.dataset.kind='choice';
        button.setAttribute('aria-labelledby',number.id+' '+label.id);
      }
      button.querySelector('.sq-choice-number').textContent=choice.id+'.';
      button.querySelector('.sq-choice-label').textContent=choice.label;
      const description=button.querySelector('.sq-choice-description'),current=button.querySelector('.sq-choice-current');
      description.textContent=typeof choice.description==='string'?choice.description:'';
      description.hidden=!description.textContent;
      current.hidden=data.selected!==choice.id;
      const describedBy=[...(!description.hidden?[description.id]:[]),...(!current.hidden?[current.id]:[])];
      if(describedBy.length)button.setAttribute('aria-describedby',describedBy.join(' '));
      else button.removeAttribute('aria-describedby');
      if(!current.hidden)button.setAttribute('aria-current','true');else button.removeAttribute('aria-current');
    }
    function render(data,selected){
      const previous=question;
      question=data;target={...selected};
      expires=Date.now()+Math.max(0,Number(data.expires_in)||0)*1000;
      if(data.available!==true){
        delete container.dataset.kind;
        choices.replaceChildren();preview.textContent='';preview.hidden=true;previewLabel.hidden=true;title.textContent='Richiesta nel terminale';
        status.textContent=data.reason||'Apri Terminale per leggere e rispondere alla richiesta.';
        container.hidden=data.waiting!==true;update();onChange();return;
      }
      container.hidden=false;title.textContent=data.question||'La sessione richiede una risposta';
      container.dataset.kind=choices.dataset.kind=data.kind==='choice'?'choice':'approval';
      previewLabel.textContent=data.review===true?'Risposte da inviare':data.kind==='choice'?'Scelta richiesta':'Operazione richiesta';
      preview.setAttribute('aria-label',previewLabel.textContent);
      preview.textContent=typeof data.context==='string'?data.context:'';preview.hidden=!preview.textContent;previewLabel.hidden=preview.hidden;
      const keep=new Set(),existing=new Map([...choices.children].map(button=>[button.dataset.choice,button]));
      const changed=previous?.challenge!==data.challenge;
      for(const choice of Array.isArray(data.choices)?data.choices:[]){
        if(!Number.isInteger(choice.id)||typeof choice.label!=='string')continue;
        const id=String(choice.id);let button=changed?null:existing.get(id);
        if(!button){button=node('button','sq-choice');button.type='button';button.dataset.choice=id;button.onclick=()=>answer(choice.id);}
        renderChoice(button,choice,data);
        keep.add(button);if(button.parentNode!==choices)choices.append(button);
      }
      for(const button of [...choices.children])if(!keep.has(button))button.remove();
      if(locked.has(lockKey(target,data.challenge)))status.textContent='Risposta già tentata. Attendi la nuova richiesta o verifica Terminale.';
      else status.textContent=data.reason||(data.allow_answer===false?'Istanza in sola lettura.':Date.now()>=expires?'Richiesta scaduta. Aggiorna prima di rispondere.':'Richiesta verificata · scegli una risposta');
      update();onChange();
    }
    async function refresh(){
      const selected=context();
      if(!selected){update();return;}
      if(sending&&same(sending.target,selected)||read&&read.generation===generation&&same(read.target,selected))return;
      const operation={generation,target:{...selected}};read=operation;update();
      const current=()=>read===operation&&generation===operation.generation&&same(operation.target,context());
      try{const data=await fetchQuestion(operation.target);if(current())render(data,operation.target);}
      catch(error){if(current())render({available:false,waiting:true,reason:error.message||'Richiesta non verificata. Aggiorna o apri Terminale.'},operation.target);}
      finally{if(read===operation){read=null;update();}}
    }
    async function answer(choice){
      update();if(!enabled()||!question.choices.some(item=>item.id===choice))return;
      const selected={...target},captured={...question},operation={generation:++generation,target:selected};
      read=null;sending=operation;locked.add(lockKey(selected,captured.challenge));
      if(locked.size>100)locked.delete(locked.values().next().value);
      status.textContent='Invio della risposta…';update();onBusy(true);onChange();
      const current=()=>sending===operation&&generation===operation.generation&&same(selected,context());
      try{
        const result=await answerQuestion(selected,{identity:selected.identity,token:captured.token,challenge:captured.challenge,choice});
        if(current()){
          status.textContent=result.message||'Risposta consegnata al terminale.';
          onAnswered(result,{target:selected,question:captured,choice});
        }
      }catch(error){if(current())status.textContent=error.message||'Invio non confermato. Verifica Terminale prima di riprovare.';}
      finally{if(sending===operation){sending=null;update();onBusy(false);onChange();}}
    }
    refreshButton.onclick=()=>refresh();
    return {reset,refresh,update,get busy(){return !!sending&&same(sending.target,context());},get awaiting(){return awaiting();}};
  }
  window.SessionQuestions={create};
})();
