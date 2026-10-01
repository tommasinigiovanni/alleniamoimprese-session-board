/* Composer shortcuts delegate to the existing microphone and form controls. */
(() => {
  'use strict';
  let nextHintId = 0;

  function create({container, form, input, context=()=>null, isEnabled=()=>false,
                   recordButton=()=>form.querySelector('.terminal-audio-record'),
                   submitButton=()=>form.querySelector('button[type="submit"]')}) {
    const hint = document.createElement('small');
    hint.className = 'chat-shortcuts-hint';
    hint.id = 'chat-shortcuts-hint-' + (++nextHintId);
    const sendHint = 'Cmd/Ctrl+Invio invia';
    const recordHint = ' · Cmd/Ctrl+Maiusc+Spazio avvia o ferma la dettatura';
    form.appendChild(hint);
    const describedBy = new Set((input.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
    describedBy.add(hint.id);
    input.setAttribute('aria-describedby', [...describedBy].join(' '));
    const metadata = new Map();
    let destroyed = false;

    function visible(element) {
      return !!element?.isConnected && !element.closest('[hidden], [inert]') &&
             !!element.getClientRects().length;
    }
    function active() {
      const dialog = form.closest('dialog');
      return !destroyed && !!context() && isEnabled() && (!dialog || dialog.open) &&
             visible(container) && visible(form) && visible(input) &&
             !input.readOnly && !input.matches(':disabled');
    }
    function resolve(button) { return typeof button === 'function' ? button() : button; }
    function available(button) {
      return visible(button) && button.form === form && !button.matches(':disabled') &&
             button.getAttribute('aria-disabled') !== 'true';
    }
    function annotate(button, shortcut) {
      if (!button) return;
      if (!metadata.has(button)) metadata.set(button, {previous:button.getAttribute('aria-keyshortcuts'), shortcut});
      button.setAttribute('aria-keyshortcuts', shortcut);
    }
    function clearMetadata() {
      for (const [button, value] of metadata) {
        if (button.getAttribute('aria-keyshortcuts') !== value.shortcut) continue;
        if (value.previous === null) button.removeAttribute('aria-keyshortcuts');
        else button.setAttribute('aria-keyshortcuts', value.previous);
      }
      metadata.clear();
    }
    function update() {
      if (destroyed) return;
      const enabled = active();
      hint.hidden = !enabled;
      clearMetadata();
      if (enabled) {
        annotate(resolve(submitButton), 'Control+Enter Meta+Enter');
        const microphone = resolve(recordButton);
        hint.textContent = sendHint + (visible(microphone) ? recordHint : '');
        if (visible(microphone)) annotate(microphone, 'Control+Shift+Space Meta+Shift+Space');
      }
    }
    function keydown(event) {
      if (event.defaultPrevented || event.repeat || event.isComposing || event.keyCode === 229 ||
          event.altKey || event.ctrlKey === event.metaKey || !active()) return;
      const target = event.target;
      if (!(target instanceof Element) || !container.contains(target)) return;
      const editor = target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])');
      if (editor && editor !== input) return;
      const send = event.key === 'Enter' && !event.shiftKey;
      const record = event.shiftKey && (event.code === 'Space' || event.key === ' ');
      if (!send && !record) return;
      const button = resolve(send ? submitButton : recordButton);
      if (!available(button)) return;
      event.preventDefault();
      event.stopPropagation();
      // Synthetic clicks and requestSubmit preserve the focused editor and its
      // selection. Availability and identity are checked again by the handlers.
      if (send) form.requestSubmit(button);
      else button.click();
    }
    function destroy() {
      if (destroyed) return;
      destroyed = true;
      container.removeEventListener('keydown', keydown);
      clearMetadata();
      hint.remove();
      const remaining = (input.getAttribute('aria-describedby') || '').split(/\s+/)
        .filter(value => value && value !== hint.id);
      if (remaining.length) input.setAttribute('aria-describedby', remaining.join(' '));
      else input.removeAttribute('aria-describedby');
    }
    container.addEventListener('keydown', keydown);
    update();
    return {update, destroy};
  }
  window.ChatShortcuts = {create};
})();
