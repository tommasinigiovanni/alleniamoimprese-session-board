/* Bounded image drafts and one explicit send, shared by both terminal interfaces. */
(() => {
  'use strict';
  const TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  const MAX_BYTES = 10 * 1024 * 1024;
  const MAX_TOTAL_BYTES = 20 * 1024 * 1024, MAX_IMAGES = 5;

  function create({form, input, result, context, send, canSend=()=>true,
                   onBusy=()=>{}, onSent=()=>{}, onDelivery=()=>{}, successText='Messaggio inviato.'}) {
    const submit = form.querySelector('button[type="submit"]');
    const attachment = document.createElement('div');
    attachment.className = 'terminal-image-tools';
    attachment.innerHTML = '<div class="terminal-image-actions"><button type="button" class="terminal-image-add" title="Fino a 5 immagini, 10 MiB ciascuna e 20 MiB totali">Allega immagine</button><output class="terminal-image-count" aria-label="Immagini allegate" aria-live="polite"></output><span>Oppure incolla · fino a 5 immagini, 10 MiB ciascuna, 20 MiB totali</span><small>Le immagini inviate sono conservate per 7 giorni.</small><input class="terminal-image-file" type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif" aria-label="Seleziona immagini" hidden></div><div class="terminal-image-previews" aria-label="Anteprime immagini allegate" hidden></div>';
    form.insertBefore(attachment, submit);
    form.classList.add('terminal-image-composer');
    input.required = false;
    input.placeholder = 'Scrivi una riga (facoltativa con immagini)…';
    const picker = attachment.querySelector('input');
    const add = attachment.querySelector('.terminal-image-add');
    const previews = attachment.querySelector('.terminal-image-previews');
    const count = attachment.querySelector('.terminal-image-count');
    let images = [], generation = 0, revision = 0, pending = null;

    function sameTarget(target) {
      const current = context();
      return current && target && current.pane === target.pane &&
             current.identity === target.identity && current.session === target.session && current.host === target.host;
    }
    function update() {
      const active = !!context();
      submit.disabled = !active || !!pending || !canSend() || (!images.length && !input.value.trim());
      // Editing a draft during its own upload is safe: its revision is preserved.
      add.disabled = !active || (!canSend() && !pending);
      picker.disabled = add.disabled;
      for (const image of images) image.remove.disabled = add.disabled;
      previews.hidden = !images.length;
      count.textContent = images.length ? images.length + '/' + MAX_IMAGES : '';
      form.setAttribute('aria-busy', String(!!pending));
    }
    function dropImages() {
      for (const image of images) URL.revokeObjectURL(image.url);
      images = [];
      previews.replaceChildren();
      previews.hidden = true;
      picker.value = '';
    }
    function removeImage(image) {
      if (!images.includes(image) || !context() || (!canSend() && !pending)) return;
      images = images.filter(item => item !== image);
      URL.revokeObjectURL(image.url);
      image.preview.remove();
      revision++;
      result.textContent = '';
      update();
    }
    function select(files) {
      if (!context() || (!canSend() && !pending)) return;
      if (!files.length) return;
      if (images.length + files.length > MAX_IMAGES) {
        result.textContent = 'Allega al massimo 5 immagini per invio.';
        return;
      }
      if (files.some(file => !TYPES.has(file.type))) {
        result.textContent = 'Usa un’immagine PNG, JPEG, WebP o GIF.';
        return;
      }
      if (files.some(file => !file.size || file.size > MAX_BYTES)) {
        result.textContent = 'Ogni immagine deve essere non vuota e non superare 10 MiB.';
        return;
      }
      if ([...images.map(image => image.file), ...files].reduce((bytes, file) => bytes + file.size, 0) > MAX_TOTAL_BYTES) {
        result.textContent = 'Le immagini possono occupare al massimo 20 MiB totali.';
        return;
      }
      const added = [];
      try { for (const file of files) added.push({file, url:URL.createObjectURL(file)}); }
      catch {
        for (const image of added) URL.revokeObjectURL(image.url);
        result.textContent = 'Anteprima non disponibile. Seleziona di nuovo le immagini.';
        return;
      }
      for (const image of added) {
        image.preview = document.createElement('figure');
        image.preview.className = 'terminal-image-preview';
        const thumbnail = document.createElement('img'), caption = document.createElement('figcaption');
        thumbnail.alt = 'Anteprima immagine allegata';thumbnail.src = image.url;
        caption.textContent = image.file.name + ' · ' + Math.max(1, Math.ceil(image.file.size / 1024)) + ' KiB';
        image.remove = document.createElement('button');image.remove.type = 'button';
        image.remove.className = 'terminal-image-remove';image.remove.textContent = '×';
        image.remove.setAttribute('aria-label', 'Rimuovi immagine ' + image.file.name);
        image.remove.title = 'Rimuovi immagine ' + image.file.name;
        image.remove.onclick = () => removeImage(image);
        image.preview.append(thumbnail, caption, image.remove);previews.append(image.preview);
      }
      images.push(...added);
      revision++;
      result.textContent = '';
      update();
      added.at(-1).preview.scrollIntoView({block:'nearest', inline:'nearest'});
    }
    function reset() {
      generation++;
      revision++;
      pending = null;
      dropImages();
      input.value = '';
      result.textContent = '';
      onBusy(false);
      update();
    }
    input.addEventListener('input', () => { revision++; update(); });
    add.onclick = () => picker.click();
    picker.onchange = () => { const selected = [...picker.files]; picker.value = ''; if (selected.length) select(selected); };
    (form.closest('dialog') || form).addEventListener('paste', event => {
      if (form.hidden) return;
      const files = [...(event.clipboardData?.items || [])]
        .filter(item => item.kind === 'file').map(item => item.getAsFile()).filter(Boolean);
      if (!files.length) return; // Leave ordinary text paste to the browser.
      event.preventDefault();
      select(files);
    });
    form.onsubmit = async event => {
      event.preventDefault();
      const target = context(), text = input.value, selected = [...images];
      if (pending || !target || !canSend() || (!selected.length && !text.trim())) return;
      if (!sameTarget(target)) return;
      const operation = {generation, revision, id: Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)};
      pending = operation;
      const delivery = {target:{...target}, text, id:operation.id, hasImage:!!selected.length, imageCount:selected.length};
      onDelivery({...delivery, status:'pending'});
      result.textContent = selected.length > 1 ? 'Invio di ' + selected.length + ' immagini in corso…' : selected.length ? 'Invio immagine in corso…' : 'Invio in corso…';
      onBusy(true);
      update();
      let body;
      if (selected.length) {
        body = new FormData();
        body.append('text', text);
        body.append('identity', target.identity);
        for (const image of selected) body.append('image', image.file);
      } else body = {text, identity: target.identity};
      if (typeof target.chat_conversation === 'string') {
        if (body instanceof FormData) body.append('chat_conversation', target.chat_conversation);
        else body.chat_conversation = target.chat_conversation;
      }
      const current = () => pending === operation && generation === operation.generation && sameTarget(target);
      try {
        const response = await send(target.pane, body);
        onDelivery({...delivery, status:'confirmed', receipt:response?.chat_delivery});
        if (!current()) return;
        if (revision === operation.revision) { input.value = ''; dropImages(); revision++; }
        result.textContent = selected.length > 1 ? selected.length + ' immagini inviate.' : selected.length ? 'Immagine inviata.' : successText;
        onSent();
      } catch (error) {
        onDelivery({...delivery, status:error.definiteRejection===true?'failed':'uncertain'});
        if (current()) {
          const uncertain = ['AbortError', 'TimeoutError', 'TypeError'].includes(error.name);
          result.textContent = uncertain
            ? 'Invio non confermato. Controlla il terminale prima di riprovare; la bozza è conservata.'
            : error.message || 'Invio non riuscito. La bozza è conservata.';
        }
      } finally {
        if (pending === operation && generation === operation.generation) {
          pending = null;
          onBusy(false);
          update();
        }
      }
    };
    window.addEventListener('pagehide', reset);
    update();
    return {reset, update, get generation() { return generation; }, get sending() { return !!pending; }};
  }
  window.TerminalImages = {create};
})();
