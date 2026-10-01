/* A device preference: switch layout without replacing live session cards. */
(() => {
  'use strict';
  const key='session-board:session-layout:v1';
  for(const control of document.querySelectorAll('[data-session-layout-target]')){
    const target=document.querySelector(control.dataset.sessionLayoutTarget);
    if(!target)continue;
    function apply(mode){
      const list=mode==='list';
      target.classList.toggle('session-list',list);
      for(const button of control.querySelectorAll('[data-layout-mode]'))button.setAttribute('aria-pressed',String(button.dataset.layoutMode===(list?'list':'cards')));
    }
    let initial='cards';try{initial=localStorage.getItem(key);}catch{/* Private browsing may disable storage. */}
    apply(initial);
    control.addEventListener('click',event=>{
      const button=event.target.closest('[data-layout-mode]');
      if(!button||!control.contains(button))return;
      const mode=button.dataset.layoutMode;if(!['cards','list'].includes(mode))return;
      apply(mode);try{localStorage.setItem(key,mode);}catch{/* The current page still switches. */}
    });
  }
})();
