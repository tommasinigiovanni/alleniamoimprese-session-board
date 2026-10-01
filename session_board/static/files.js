'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name=csrf]').content;
  const allowWrite = document.querySelector('.files-page').dataset.allowWrite === '1';
  const HIDDEN_KEY = 'session-board-files-hidden';
  let current = null, loadId = 0, uploading = false;
  const queue = [];

  function node(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
  function size(bytes) {
    if (bytes === null || bytes === undefined) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return (unit ? value.toFixed(value < 10 ? 1 : 0) : value) + ' ' + units[unit];
  }
  function when(seconds) {
    if (!seconds) return '';
    return new Date(seconds * 1000).toLocaleString('it-IT', {day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'});
  }
  function join(dir, name) { return dir ? dir + '/' + name : name; }
  function showError(message) { $('files-error').textContent = message || ''; $('files-error').hidden = !message; }
  function pathFromHash() {
    try { return decodeURIComponent(location.hash.replace(/^#\/?/, '')); } catch { return ''; }
  }
  function hashFor(path) { return '#/' + path.split('/').map(encodeURIComponent).join('/'); }
  function go(path) { location.hash = hashFor(path); }

  async function api(path, options = {}) {
    const response = await fetch(path, {...options, headers: {'X-CSRF-Token': csrf, ...options.headers}});
    if (response.status === 401) { location.href = '/login'; throw new Error('Accesso scaduto'); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(data.error || 'Richiesta non riuscita'), {status: response.status});
    return data;
  }

  function renderCrumbs(data) {
    const crumbs = $('crumbs');
    crumbs.replaceChildren();
    const rootLink = node('a', '', data.root);
    rootLink.href = '#/';
    crumbs.append(rootLink);
    let acc = '';
    for (const part of data.path ? data.path.split('/') : []) {
      acc = join(acc, part);
      const link = node('a', '', part);
      link.href = hashFor(acc);
      crumbs.append(node('span', '', '/'), link);
    }
  }

  function render() {
    const data = current;
    const showHidden = $('files-hidden').checked;
    const body = $('files-list');
    body.replaceChildren();
    const entries = data.entries.filter((e) => showHidden || !e.name.startsWith('.'));
    if (!entries.length) {
      const row = node('tr'), cell = node('td', 'muted', 'Cartella vuota');
      cell.colSpan = 3; row.append(cell); body.append(row);
    }
    for (const entry of entries) {
      const row = node('tr', entry.type), name = node('td');
      const path = join(data.path, entry.name);
      if (entry.type === 'dir') {
        const link = node('a', '', '📁 ' + entry.name);
        link.href = hashFor(path);
        name.append(link);
      } else if (entry.type === 'file') {
        const link = node('a', '', '📄 ' + entry.name);
        link.href = '/api/files/download?path=' + encodeURIComponent(path);
        link.setAttribute('download', entry.name);
        link.title = 'Scarica ' + entry.name;
        name.append(link);
      } else {
        name.textContent = (entry.type === 'broken' ? '⚠ ' : '◇ ') + entry.name;
      }
      if (entry.link) name.append(' ', node('span', 'link-mark', '↪ link'));
      row.append(name, node('td', 'num', size(entry.size)), node('td', 'num', when(entry.mtime)));
      body.append(row);
    }
    const hidden = data.entries.length - entries.length;
    $('files-status').textContent = `${data.absolute} · ${entries.length} elementi` +
      (hidden ? ` (${hidden} nascosti)` : '') + (data.truncated ? ' · elenco troncato' : '') +
      ` · spazio libero ${size(data.free_bytes)}` + (allowWrite && !data.writable ? ' · cartella non scrivibile' : '');
    $('files-up').disabled = data.parent === null;
  }

  async function load() {
    const id = ++loadId, path = pathFromHash();
    try {
      const data = await api('/api/files/list?path=' + encodeURIComponent(path));
      if (id !== loadId) return;
      current = data;
      showError('');
      renderCrumbs(data);
      render();
    } catch (error) {
      if (id !== loadId) return;
      showError(error.message);
      if (!current && path) go('');
    }
  }

  function uploadRow(file) {
    const row = node('div', 'files-upload'), label = node('strong', '', file.name), state = node('span', '', 'In coda');
    const bar = document.createElement('progress');
    bar.max = file.size || 1; bar.value = 0;
    row.append(label, state, bar);
    $('files-uploads').hidden = false;
    $('files-uploads').prepend(row);
    return {row, state, bar};
  }

  function send(item, overwrite) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const query = new URLSearchParams({dir: item.dir, name: item.file.name});
      if (overwrite) query.set('overwrite', '1');
      xhr.open('PUT', '/api/files/upload?' + query);
      xhr.setRequestHeader('X-CSRF-Token', csrf);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        item.ui.bar.value = event.loaded;
        item.ui.state.textContent = `${Math.floor(event.loaded / event.total * 100)}% · ${size(event.loaded)} di ${size(event.total)}`;
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
        if (xhr.status === 401) { location.href = '/login'; return; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(Object.assign(new Error(data.error || 'Caricamento non riuscito'), {status: xhr.status}));
      };
      xhr.onerror = () => reject(new Error('Connessione interrotta durante il caricamento'));
      xhr.send(item.file);
    });
  }

  async function pump() {
    if (uploading) return;
    uploading = true;
    while (queue.length) {
      const item = queue.shift();
      item.ui.state.textContent = 'Caricamento…';
      try {
        if (item.file.size > item.limit) throw new Error(`Supera il limite di ${size(item.limit)}`);
        let overwrite = false;
        if (item.exists) {
          overwrite = confirm(`"${item.file.name}" esiste già in questa cartella. Vuoi sostituirlo?`);
          if (!overwrite) { item.ui.row.dataset.state = 'error'; item.ui.state.textContent = 'Saltato'; continue; }
        }
        await send(item, overwrite);
        item.ui.bar.value = item.ui.bar.max;
        item.ui.row.dataset.state = 'done';
        item.ui.state.textContent = `Caricato · ${size(item.file.size)}`;
      } catch (error) {
        item.ui.row.dataset.state = 'error';
        item.ui.state.textContent = error.message;
      }
      if (current && item.dir === current.path) await load();
    }
    uploading = false;
  }

  function enqueue(files) {
    if (!allowWrite || !current) return;
    const names = new Set(current.entries.map((e) => e.name));
    for (const file of files) {
      queue.push({file, dir: current.path, exists: names.has(file.name), limit: current.max_upload_bytes, ui: uploadRow(file)});
    }
    pump();
  }

  $('files-up').addEventListener('click', () => { if (current && current.parent !== null) go(current.parent); });
  $('files-refresh').addEventListener('click', load);
  try { $('files-hidden').checked = localStorage.getItem(HIDDEN_KEY) === '1'; } catch { /* default off */ }
  $('files-hidden').addEventListener('change', () => {
    try { localStorage.setItem(HIDDEN_KEY, $('files-hidden').checked ? '1' : '0'); } catch { /* not persisted */ }
    if (current) render();
  });

  if (allowWrite) {
    $('files-input').addEventListener('change', (event) => { enqueue([...event.target.files]); event.target.value = ''; });
    $('files-mkdir').addEventListener('click', async () => {
      const name = prompt('Nome della nuova cartella');
      if (!name || !current) return;
      try {
        const data = await api('/api/files/mkdir', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({dir: current.path, name: name.trim()})});
        go(data.path);
      } catch (error) { showError(error.message); }
    });
    const drop = $('files-drop');
    let depth = 0;
    const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');
    drop.addEventListener('dragenter', (event) => { if (!hasFiles(event)) return; event.preventDefault(); depth++; drop.classList.add('dragging'); });
    drop.addEventListener('dragover', (event) => { if (hasFiles(event)) event.preventDefault(); });
    drop.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) drop.classList.remove('dragging'); });
    drop.addEventListener('drop', (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault(); depth = 0; drop.classList.remove('dragging');
      const files = [...event.dataTransfer.files].filter((file, index) => {
        const entry = event.dataTransfer.items?.[index]?.webkitGetAsEntry?.();
        return !entry || entry.isFile;
      });
      if (files.length < event.dataTransfer.files.length) showError('Le cartelle non si possono trascinare: carica i file al loro interno.');
      enqueue(files);
    });
  }
  window.addEventListener('beforeunload', (event) => { if (uploading) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('hashchange', load);
  load();
})();
