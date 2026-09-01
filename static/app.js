/* App Launcher UI.
 *
 * One render path per page, fed by /api/status. Actions post to /api/action/*
 * and then re-render, so what you see is always the script's own verdict
 * rather than an optimistic guess about what the click did.
 *
 * The tiles page has two views: the grid, and a viewer that embeds one app.
 * Which one is showing comes from the hash (#/app/<name>), so a reload or a
 * copied link lands back on the same app.
 */

const PAGE = document.documentElement.dataset.page;
const POLL_MS = 8000;   // a status sweep walks every process on the box; don't churn
const RAIL_KEY = 'devapps.rail';
const VIEW_KEY = 'devapps.view';   // 'list' (default) or 'grid'
const FRAME_GRACE_MS = 6000;

let apps = [];
let busy = false;      // an action is running: pause polling so nothing flickers
let dragging = false;  // reordering the menu: a re-render would destroy the drag
let layout = 'list';   // which dashboard layout is showing
let framedName = null; // app whose URL is currently loaded in the iframe
let frameLoaded = false;
let frameTimer = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function initial() {
  const el = $('#initial-data');
  try { return el ? JSON.parse(el.textContent) : []; } catch { return []; }
}

function byName(name) {
  return apps.find(a => a.name === name) || null;
}

/* ------------------------------------------------------------------ routing */

/* '#/app/<name>' selects an app; anything else is the grid. */
function currentRoute() {
  const m = /^#\/app\/(.+)$/.exec(window.location.hash || '');
  return m ? { view: 'app', name: decodeURIComponent(m[1]) } : { view: 'grid' };
}

function openApp(name) {
  // The launcher's own "app" IS this dashboard. Framing it inside itself would
  // nest a copy that default-selects the launcher again, and again -- so
  // selecting it just shows the grid.
  const a = byName(name);
  if (a && a.is_self) return openGrid();
  if (PAGE !== 'apps') {
    // The viewer only exists on the tiles page; carry the selection there.
    window.location.href = '/#/app/' + encodeURIComponent(name);
    return;
  }
  window.location.hash = '#/app/' + encodeURIComponent(name);
}

function openGrid() {
  if (PAGE !== 'apps') { window.location.href = '/'; return; }
  // Replace, so Back doesn't bounce between grid and viewer forever.
  history.replaceState(null, '', window.location.pathname);
  render();
}

/* ------------------------------------------------------------------ rail */

function setRail(on) {
  document.documentElement.classList.toggle('rail', on);
  const btn = $('#toggle');
  const label = on ? 'Expand menu' : 'Collapse menu';
  btn.title = label;
  btn.setAttribute('aria-label', label);
  try { localStorage.setItem(RAIL_KEY, on ? '1' : '0'); } catch { /* not fatal */ }
}

$('#toggle').addEventListener('click',
  () => setRail(!document.documentElement.classList.contains('rail')));
// The inline head script set the class; sync the button's label to match.
setRail(document.documentElement.classList.contains('rail'));

/* ------------------------------------------------------------------ toast */

let toastTimer = null;
function toast(msg, kind) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'bad' ? 9000 : 4500);
}

function sheet(title, body) {
  $('#sheet-title').textContent = title;
  $('#sheet-body').textContent = body;
  $('#sheet').hidden = false;
}

/* ------------------------------------------------------------------ sidebar */

function renderSidebar(route) {
  const nav = $('#side-apps');
  nav.textContent = '';
  apps.forEach(a => {
    const item = document.createElement('a');
    const active = a.is_self
      ? route.view === 'grid'                       // the dashboard itself
      : route.view === 'app' && route.name === a.name;
    item.className = 'side-item' + (a.running ? '' : ' stopped')
                   + (active ? ' on' : '');
    item.href = a.is_self ? '/' : '/#/app/' + encodeURIComponent(a.name);
    // Rail mode hides the label, so the tooltip carries the name and state.
    item.title = a.name + ' — ' + (a.running ? 'running' : 'stopped');
    item.dataset.open = a.name;
    item.draggable = true;

    const icon = document.createElement('img');
    icon.className = 'ico';
    icon.src = a.icon;
    icon.alt = '';
    item.appendChild(icon);

    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = a.name;
    item.appendChild(label);

    const dot = document.createElement('span');
    dot.className = 'dot' + (a.running ? ' on' : '');
    item.appendChild(dot);

    nav.appendChild(item);
  });
}

/* Drag to reorder. Registry order is menu order, so a drop writes the new
   order straight to apps.json -- which also changes the grid and the logon
   start order. Polling is paused while dragging, or a re-render mid-drag would
   yank the row out from under the pointer. */
function wireDrag(nav) {
  let src = null;

  nav.addEventListener('dragstart', (ev) => {
    const item = ev.target.closest('.side-item');
    if (!item) return;
    src = item;
    dragging = true;
    item.classList.add('dragging');
    // Firefox needs data set or the drag never starts.
    ev.dataTransfer.setData('text/plain', item.dataset.open || '');
    ev.dataTransfer.effectAllowed = 'move';
  });

  nav.addEventListener('dragover', (ev) => {
    if (!src) return;
    ev.preventDefault();
    const over = ev.target.closest('.side-item');
    if (!over || over === src) return;
    const box = over.getBoundingClientRect();
    const after = ev.clientY > box.top + box.height / 2;
    nav.insertBefore(src, after ? over.nextSibling : over);
  });

  nav.addEventListener('dragend', async () => {
    if (!src) return;
    src.classList.remove('dragging');
    src = null;
    // Dedupe defensively: a mid-drag re-render can leave the same row twice.
    const seen = new Set();
    const order = $$('.side-item', nav)
      .map(i => i.dataset.open)
      .filter(n => n && !seen.has(n) && seen.add(n));
    dragging = false;
    try {
      const res = await fetch('/api/apps/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order }),
      });
      const data = await res.json();
      if (!data.ok) toast(data.error, 'bad');
    } catch (e) {
      toast('could not save the new order: ' + e.message, 'bad');
    }
    refresh();
  });
}

wireDrag($('#side-apps'));

/* ------------------------------------------------------------------ viewer */

function actionButton(label, action, name, cls) {
  const b = document.createElement('button');
  b.className = 'btn sm' + (cls ? ' ' + cls : '');
  b.textContent = label;
  b.dataset.action = action;
  b.dataset.name = name;
  return b;
}

/* Ask the server whether the app can be framed at all, rather than showing a
   blank frame and letting the reader guess. The browser gives the page no
   usable signal when a frame is refused, so this is the only honest answer. */
async function frameNote(a) {
  const note = $('#viewer-note');
  const link = '<a href="' + a.url + '" target="_blank" rel="noopener">Open it in a tab</a>';
  note.hidden = true;
  clearTimeout(frameTimer);

  let verdict = null;
  try {
    const res = await fetch('/api/framable/' + encodeURIComponent(a.name));
    verdict = await res.json();
  } catch { /* fall through to the timeout heuristic below */ }

  if (verdict && verdict.framable === false) {
    // Don't leave a frame that will never paint sitting there.
    showBlocked(a, verdict.reason);
    return;
  }
  if (verdict && verdict.warn) {
    note.innerHTML = verdict.warn.charAt(0).toUpperCase() + verdict.warn.slice(1)
                   + '. ' + link + '.';
    note.hidden = false;
    return;
  }
  frameTimer = setTimeout(() => {
    if (frameLoaded) return;
    note.innerHTML = 'This app has not rendered. ' + link + '.';
    note.hidden = false;
  }, FRAME_GRACE_MS);
}

/* Replace the frame with a plain explanation and a way out. */
function showBlocked(a, reason) {
  const frame = $('#viewer-frame');
  const empty = $('#viewer-empty');
  frame.src = 'about:blank';
  framedName = null;
  frame.hidden = true;
  $('#viewer-note').hidden = true;

  empty.textContent = '';
  empty.hidden = false;
  const big = document.createElement('div');
  big.className = 'big';
  big.textContent = a.name + ' cannot be embedded here';
  empty.appendChild(big);
  const sub = document.createElement('div');
  sub.textContent = reason ? reason.charAt(0).toUpperCase() + reason.slice(1) + '.' : '';
  empty.appendChild(sub);
  if (a.url) {
    const open = document.createElement('a');
    open.className = 'btn';
    open.href = a.url;
    open.target = '_blank';
    open.rel = 'noopener';
    open.textContent = 'Open ' + a.name + ' in a new tab ↗';
    empty.appendChild(open);
  }
}

/* Identity and per-app controls live in the topbar; the viewer below is just
   the frame. */
function setTopbar(a) {
  const icon = $('#tb-icon');
  if (a) {
    icon.src = a.icon;
    icon.hidden = false;
  } else {
    icon.hidden = true;
  }
  $('#tb-name').textContent = a ? a.name : (PAGE === 'status' ? 'Status' : 'Apps');
  $('#tb-meta').textContent = a
    ? (a.url || (a.port ? '127.0.0.1:' + a.port : 'no endpoint'))
      + (a.running ? '  ·  running · pid ' + a.pid + ' · via ' + a.via : '  ·  stopped')
    : '';
}

function renderViewer(a) {
  setTopbar(a);

  const actions = $('#topbar-actions');
  actions.textContent = '';
  if (a.url) {
    const open = document.createElement('a');
    open.className = 'btn sm';
    open.href = a.url;
    open.target = '_blank';
    open.rel = 'noopener';
    open.textContent = 'Open ↗';
    actions.appendChild(open);
  }
  if (a.running && a.url) actions.appendChild(actionButton('Reload', 'reframe', a.name));
  if (a.running) {
    if (!a.is_self) {
      actions.appendChild(actionButton('Restart', 'restart', a.name));
      actions.appendChild(actionButton('Stop', 'stop', a.name));
    }
  } else {
    actions.appendChild(actionButton('Start', 'start', a.name, 'primary'));
  }
  actions.appendChild(actionButton('Logs', 'logs', a.name));
  actions.appendChild(actionButton('Edit', 'edit', a.name));
  actions.appendChild(actionButton('All apps', 'grid', a.name, 'ghost'));

  const frame = $('#viewer-frame');
  const empty = $('#viewer-empty');

  if (!a.running || !a.url) {
    // Don't point the frame at a dead port: the browser's own error page is
    // less informative than saying what to do about it.
    if (framedName !== null) { frame.src = 'about:blank'; framedName = null; }
    frame.hidden = true;
    empty.hidden = false;
    empty.textContent = '';
    const big = document.createElement('div');
    big.className = 'big';
    big.textContent = !a.url ? a.name + ' has no URL to show'
                             : a.name + ' is not running';
    empty.appendChild(big);
    const sub = document.createElement('div');
    sub.textContent = !a.url
      ? 'It is registered without a port, so there is nothing to embed.'
      : 'Start it and the page will load here.';
    empty.appendChild(sub);
    if (!a.running && a.url) {
      empty.appendChild(actionButton('Start ' + a.name, 'start', a.name, 'primary'));
    }
    $('#viewer-note').hidden = true;
    clearTimeout(frameTimer);
    return;
  }

  empty.hidden = true;
  frame.hidden = false;
  // Only (re)load when the target app changes -- otherwise the status poll
  // would reload the embedded app every 8 seconds.
  if (framedName !== a.name) {
    framedName = a.name;
    frameLoaded = false;
    frame.src = a.url;
    frameNote(a);
  }
}

$('#viewer-frame').addEventListener('load', () => {
  if ($('#viewer-frame').src === 'about:blank') return;
  frameLoaded = true;
  clearTimeout(frameTimer);
});

function reframe(name) {
  const a = byName(name);
  if (!a || !a.url) return;
  const frame = $('#viewer-frame');
  frameLoaded = false;
  // Same URL assigned twice is not a navigation, so go via about:blank.
  frame.src = 'about:blank';
  setTimeout(() => { frame.src = a.url; frameNote(a); }, 30);
}

/* ------------------------------------------------------------------ tiles */

function endpoint(a) {
  if (a.url) {
    try {
      const u = new URL(a.url);
      return u.host + (u.pathname || '');
    } catch { return a.url; }
  }
  return a.port ? '127.0.0.1:' + a.port : 'no endpoint';
}

function tile(a) {
  const el = document.createElement('article');
  el.className = 'tile' + (a.running ? '' : ' down') + (a.path_exists ? '' : ' missing');
  el.id = 'app-' + a.name;
  // Clicking the card body opens the app; the buttons stop propagation below.
  el.dataset.open = a.name;
  el.title = 'Open ' + a.name + ' here';

  const head = document.createElement('div');
  head.className = 'tile-head';

  const badge = document.createElement('div');
  badge.className = 'tile-badge';
  const icon = document.createElement('img');
  icon.src = a.icon;
  icon.alt = '';
  badge.appendChild(icon);
  head.appendChild(badge);

  const name = document.createElement('span');
  name.className = 'tile-name';
  name.textContent = a.name;
  head.appendChild(name);

  const dot = document.createElement('span');
  dot.className = 'dot' + (a.running ? ' on' : '');
  head.appendChild(dot);

  const spacer = document.createElement('span');
  spacer.className = 'spacer';
  head.appendChild(spacer);

  const tag = document.createElement('span');
  tag.className = 'tag' + (a.autostart ? ' auto' : '');
  tag.textContent = a.autostart ? 'logon' : 'manual';
  head.appendChild(tag);
  el.appendChild(head);

  const desc = document.createElement('p');
  desc.className = 'tile-desc';
  desc.textContent = a.description || '';
  el.appendChild(desc);

  const ep = document.createElement('div');
  ep.className = 'endpoint' + (a.url ? '' : ' none');
  ep.textContent = endpoint(a);
  el.appendChild(ep);

  const meta = document.createElement('div');
  meta.className = 'tile-meta';
  meta.textContent = a.running
    ? 'running · pid ' + a.pid + ' · via ' + a.via
    : 'stopped';
  el.appendChild(meta);

  if (!a.path_exists) {
    const warn = document.createElement('div');
    warn.className = 'tile-warn';
    warn.textContent = 'folder not found: ' + a.path;
    el.appendChild(warn);
  }

  const actions = document.createElement('div');
  actions.className = 'tile-actions';
  const add = (label, action, cls) => {
    const b = actionButton(label, action, a.name, cls);
    actions.appendChild(b);
    return b;
  };

  if (a.running) {
    if (!a.is_self) { add('Restart', 'restart'); add('Stop', 'stop'); }
    else { add('Stop', 'stop').disabled = true; }
  } else {
    add('Start', 'start', 'primary');
  }
  add('Logs', 'logs');
  add('Edit', 'edit');
  if (!a.is_self) add('Remove', 'remove', 'danger');

  el.appendChild(actions);
  return el;
}

/* The management list: every app with the options you reach for -- open,
   start/stop, restart, edit, logs, delete -- plus a clickable autostart pill. */
function renderList() {
  const list = $('#list');
  list.textContent = '';

  apps.forEach(a => {
    const row = document.createElement('div');
    row.className = 'applist-row' + (a.path_exists ? '' : ' missing');

    const open = document.createElement('a');
    open.className = 'row-open';
    open.href = a.is_self ? '/' : '/#/app/' + encodeURIComponent(a.name);
    open.dataset.open = a.name;
    open.title = a.is_self ? 'The dashboard you are on' : 'Open ' + a.name + ' here';
    const icon = document.createElement('img');
    icon.src = a.icon;
    icon.alt = '';
    open.appendChild(icon);
    const text = document.createElement('div');
    text.className = 'row-text';
    const nm = document.createElement('span');
    nm.className = 'row-name';
    nm.textContent = a.name;
    text.appendChild(nm);
    const desc = document.createElement('span');
    desc.className = 'row-desc';
    desc.textContent = a.description || '';
    text.appendChild(desc);
    open.appendChild(text);
    row.appendChild(open);

    if (a.url) {
      const ep = document.createElement('a');
      ep.className = 'row-endpoint';
      ep.href = a.url;
      ep.target = '_blank';
      ep.rel = 'noopener';
      ep.textContent = endpoint(a);
      row.appendChild(ep);
    } else {
      const ep = document.createElement('span');
      ep.className = 'row-endpoint none';
      ep.textContent = endpoint(a);
      row.appendChild(ep);
    }

    const state = document.createElement('span');
    state.className = 'row-state';
    const dot = document.createElement('span');
    dot.className = 'dot' + (a.running ? ' on' : '');
    state.appendChild(dot);
    state.appendChild(document.createTextNode(
      a.running ? 'running \u00b7 pid ' + a.pid : 'stopped'));
    row.appendChild(state);

    // Autostart is the one registry field worth flipping without the form.
    const pill = document.createElement('button');
    pill.className = 'pill' + (a.autostart ? ' on' : '');
    pill.textContent = a.autostart ? 'logon' : 'manual';
    pill.dataset.action = 'autostart';
    pill.dataset.name = a.name;
    pill.title = a.autostart
      ? 'Starts at logon \u2014 click to make it manual'
      : 'Manual only \u2014 click to start it at logon';
    row.appendChild(pill);

    const actions = document.createElement('div');
    actions.className = 'row-actions';
    const add = (label, action, cls) =>
      actions.appendChild(actionButton(label, action, a.name, cls));

    if (a.running) {
      if (!a.is_self) { add('Restart', 'restart'); add('Stop', 'stop'); }
    } else {
      add('Start', 'start', 'primary');
    }
    add('Edit', 'edit');
    add('Logs', 'logs');
    if (!a.is_self) add('Delete', 'remove', 'danger');
    row.appendChild(actions);

    list.appendChild(row);
  });
}

function setLayout(next) {
  layout = next === 'grid' ? 'grid' : 'list';
  try { localStorage.setItem(VIEW_KEY, layout); } catch { /* not fatal */ }
  $$('[data-view]').forEach(b => b.classList.toggle('on', b.dataset.view === layout));
  render();
}

function renderGrid() {
  const grid = $('#grid');
  grid.textContent = '';
  apps.forEach(a => grid.appendChild(tile(a)));

  const add = document.createElement('article');
  add.className = 'tile add';
  add.innerHTML = '<div class="plus">+</div><div>Register an app</div>';
  add.addEventListener('click', () => openForm(null));
  grid.appendChild(add);
}

/* ------------------------------------------------------------------ status */

function renderTable() {
  const body = $('#status-table tbody');
  body.textContent = '';
  apps.forEach(a => {
    const tr = document.createElement('tr');
    tr.id = 'app-' + a.name;

    const cell = (html) => {
      const td = document.createElement('td');
      td.innerHTML = html;
      tr.appendChild(td);
      return td;
    };
    const text = (value, cls) => {
      const td = document.createElement('td');
      if (cls) td.className = cls;
      td.textContent = value;
      tr.appendChild(td);
      return td;
    };

    const appCell = document.createElement('td');
    appCell.className = 'app';
    const icon = document.createElement('img');
    icon.src = a.icon;
    icon.alt = '';
    appCell.appendChild(icon);
    const link = document.createElement('a');
    link.href = '/#/app/' + encodeURIComponent(a.name);
    link.textContent = a.name;
    link.dataset.open = a.name;
    appCell.appendChild(link);
    tr.appendChild(appCell);

    cell('<span class="state"><span class="dot' + (a.running ? ' on' : '') + '"></span>' +
         (a.running ? 'running' : 'stopped') + '</span>');
    cell('<span class="via">' + (a.running ? a.via : '—') + '</span>');
    text(a.running ? String(a.pid) : '—', 'mono');
    text(a.port ? String(a.port) : '—', 'mono');

    if (a.url) {
      const td = document.createElement('td');
      const out = document.createElement('a');
      out.href = a.url;
      out.target = '_blank';
      out.rel = 'noopener';
      out.textContent = a.url;
      td.appendChild(out);
      tr.appendChild(td);
    } else {
      text('—', 'no');
    }

    text(a.autostart ? 'yes' : 'no');
    const path = text(a.path, 'path');
    if (!a.path_exists) {
      path.textContent = a.path + '  (missing)';
      path.style.color = 'var(--warn)';
    }

    const td = document.createElement('td');
    const b = actionButton(a.running ? 'Stop' : 'Start', a.running ? 'stop' : 'start', a.name);
    if (a.is_self && a.running) b.disabled = true;
    td.appendChild(b);
    tr.appendChild(td);

    body.appendChild(tr);
  });
}

/* ------------------------------------------------------------------ render */

function render() {
  const route = currentRoute();

  if (PAGE === 'status') {
    renderTable();
    renderSidebar(route);
    setTopbar(null);
    return;
  }

  let selected = route.view === 'app' ? byName(route.name) : null;
  if (selected && selected.is_self) { openGrid(); return; }
  if (route.view === 'app' && !selected) {
    // Hash names an app that is no longer registered.
    openGrid();
    return;
  }

  document.body.classList.toggle('viewing', !!selected);
  $('#dash').hidden = !!selected;
  $('#viewer').hidden = !selected;
  // Draw whichever layout is showing. (Forgetting this call is exactly how the
  // dashboard ended up blank after the viewer was added.)
  $('#list').hidden = !!selected || layout !== 'list';
  $('#grid').hidden = !!selected || layout !== 'grid';
  if (!selected) {
    if (layout === 'list') renderList(); else renderGrid();
    const up = apps.filter(a => a.running).length;
    $('#dash-count').textContent = up + ' of ' + apps.length + ' running';
  }

  if (selected) {
    renderViewer(selected);
  } else {
    setTopbar(null);
    $('#topbar-actions').textContent = '';
    framedName = null;
    // Guarded: render() runs on every poll, and reassigning src unconditionally
    // would fire a needless navigation each time.
    const frame = $('#viewer-frame');
    if (!frame.src.endsWith('about:blank')) frame.src = 'about:blank';
  }
  renderSidebar(route);
}

/* ------------------------------------------------------------------ data */

async function refresh() {
  if (busy || dragging) return;
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    // Re-check: a status sweep takes ~2s, so a poll that started before the
    // drag can land in the middle of it. Re-rendering then replaces the row
    // being dragged, and the dragover handler re-inserts the stale node --
    // leaving a duplicate in the list and a rejected reorder.
    if (dragging) return;
    apps = data.apps;
    render();
    $('#tick').textContent = new Date().toLocaleTimeString();
  } catch {
    $('#tick').textContent = 'offline';
  }
}

/* ------------------------------------------------------------------ actions */

async function act(action, name, btn) {
  // Start/stop shells out to PowerShell and can take a while (start waits for
  // the port to bind), so the button reports that it is working.
  const label = btn ? btn.textContent : null;
  busy = true;
  if (btn) { btn.classList.add('busy'); btn.disabled = true; btn.textContent = '…'; }
  try {
    const res = await fetch('/api/action/' + action + (name ? '/' + name : ''), { method: 'POST' });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || (action + ' failed'), 'bad');
      if (data.output) sheet(action + ' output', data.output);
    } else {
      toast((name || 'all apps') + ': ' + action + ' done');
      if (data.output && /FAILED|SKIPPED|nothing on port|could not stop/.test(data.output)) {
        sheet(action + ' output', data.output);
      }
    }
  } catch (e) {
    toast('request failed: ' + e.message, 'bad');
  } finally {
    busy = false;
    if (btn) { btn.classList.remove('busy'); btn.disabled = false; btn.textContent = label; }
    // A restart means a new process behind the same URL, so drop the frame's
    // memory of what it loaded and let render() point it at the app again.
    if (action === 'restart' || action === 'stop') framedName = null;
    refresh();
  }
}

async function toggleAutostart(name) {
  const a = byName(name);
  if (!a) return;
  const res = await fetch('/api/apps/' + encodeURIComponent(name) + '/autostart', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ autostart: !a.autostart }),
  });
  const data = await res.json();
  if (!data.ok) return void toast(data.error, 'bad');
  toast(name + (data.autostart ? ' will start at logon' : ' is now manual only'));
  refresh();
}

async function showLogs(name) {
  const res = await fetch('/api/logs/' + name);
  const data = await res.json();
  sheet(name + ' logs', data.text || data.error || '(nothing)');
}

async function remove(name) {
  if (!confirm('Delete "' + name + '" from the launcher?\n\n'
               + 'It is removed from apps.json (a .bak is kept). The app folder and '
               + 'its files are untouched, and a running process keeps running.')) return;
  const res = await fetch('/api/apps/' + name, { method: 'DELETE' });
  const data = await res.json();
  if (data.ok) toast(name + ' unregistered'); else toast(data.error, 'bad');
  if (currentRoute().name === name) openGrid();
  refresh();
}

document.addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-action]');
  if (btn) {
    // Buttons live inside clickable cards; don't also open the app.
    ev.preventDefault();
    ev.stopPropagation();
    const { action, name } = btn.dataset;
    if (action === 'logs') return void showLogs(name);
    if (action === 'remove') return void remove(name);
    if (action === 'grid') return void openGrid();
    if (action === 'edit') return void openForm(byName(name));
    if (action === 'autostart') return void toggleAutostart(name);
    if (action === 'reframe') return void reframe(name);
    return void act(action, name, btn);
  }

  // A real link inside a card (the endpoint, an Open ↗) keeps its own job.
  if (ev.target.closest('a[target="_blank"]')) return;

  const opener = ev.target.closest('[data-open]');
  if (opener) {
    ev.preventDefault();
    openApp(opener.dataset.open);
  }
});

window.addEventListener('hashchange', render);

// These live in the launcher's dashboard, which the status page doesn't have.
const refreshBtn = $('#refresh');
if (refreshBtn) refreshBtn.addEventListener('click', refresh);
const dashAdd = $('#dash-add');
if (dashAdd) dashAdd.addEventListener('click', () => openForm(null));
$$('[data-view]').forEach(b => b.addEventListener('click', () => setLayout(b.dataset.view)));
if ($('#list')) {
  // Restore the stored layout before the first render, not after.
  try { layout = localStorage.getItem(VIEW_KEY) === 'grid' ? 'grid' : 'list'; } catch { /* default */ }
  $$('[data-view]').forEach(b => b.classList.toggle('on', b.dataset.view === layout));
}
$('#sheet-close').addEventListener('click', () => { $('#sheet').hidden = true; });
$('#sheet').addEventListener('click', (ev) => {
  if (ev.target.id === 'sheet') $('#sheet').hidden = true;
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') $('#sheet').hidden = true;
});

/* ------------------------------------------------------------------ add form */

const dialog = $('#add-dialog');
let editing = null;     // the app being edited, or null when registering

/* One form for both jobs. Registering and editing validate identically on the
   server, so they should look identical here too -- the only difference is that
   an existing app's name is fixed (renaming would orphan its log and pid
   files). */
function openForm(app) {
  if (!dialog) {
    // The form only lives on the tiles page; from /status, go there and open it.
    window.location.href = app ? '/#edit/' + encodeURIComponent(app.name) : '/#add';
    return;
  }
  const form = $('#add-form');
  editing = app || null;
  $('#add-err').hidden = true;
  form.reset();

  if (editing) {
    $('#form-title').textContent = 'Edit ' + editing.name;
    $('#form-hint').innerHTML = 'Saved to <code>apps.json</code>. '
      + 'Restart the app for a command or path change to take effect.';
    $('#form-submit').textContent = 'Save changes';
    $('#name-help').textContent = 'Renaming moves its log, pid and icon files to '
      + 'match, and relaunches the app if it is running.';
    form.name.value = editing.name;
    form.name.readOnly = false;
    form.dir.value = editing.dir || '';
    form.command.value = editing.command || 'python';
    form.args.value = (editing.args || []).join(' ');
    form.port.value = editing.port == null ? '' : editing.port;
    form.url.value = editing.url || '';
    form.description.value = editing.description || '';
    form.match.value = editing.match || '';
    form.autostart.checked = !!editing.autostart;
  } else {
    $('#form-title').textContent = 'Register an app';
    $('#form-hint').innerHTML = 'Written to <code>apps.json</code>, so it starts at '
      + 'logon too.';
    $('#form-submit').textContent = 'Add app';
    $('#name-help').textContent = 'Lowercase. Used for the log filenames and the CLI.';
    form.command.value = 'python';
    form.autostart.checked = true;
  }
  dialog.showModal();
}

$('#side-add').addEventListener('click', () => openForm(null));

if (dialog) {
  const form = $('#add-form');
  const err = $('#add-err');

  $('#add-cancel').addEventListener('click', () => dialog.close());

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    const fd = new FormData(form);
    const payload = {
      name: fd.get('name'),
      dir: fd.get('dir'),
      command: fd.get('command'),
      args: fd.get('args'),
      port: fd.get('port'),
      url: fd.get('url'),
      description: fd.get('description'),
      match: fd.get('match'),
      autostart: fd.get('autostart') === 'on',
    };
    const target = editing ? '/api/apps/' + encodeURIComponent(editing.name) : '/api/apps';
    const res = await fetch(target, {
      method: editing ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) {
      err.textContent = data.error;
      err.hidden = false;
      return;
    }
    const renamedFrom = data.renamed_from;
    const verb = !editing ? 'added to apps.json'
               : renamedFrom ? 'renamed from ' + renamedFrom : 'saved';
    const extra = data.warning || data.note;
    dialog.close();
    editing = null;
    toast(extra ? payload.name + ' ' + verb + ' — ' + extra
                : payload.name + ' ' + verb,
          data.warning ? 'warn' : null);
    // A rename changes the route's name, so a viewer open on the old one would
    // bounce to the grid on the next render.
    if (renamedFrom && currentRoute().name === renamedFrom) {
      history.replaceState(null, '', '/#/app/' + encodeURIComponent(data.app.name));
    }
    refresh();
  });

}

/* Deep links from the status page (#add, #edit/<name>). Runs after the first
   load of `apps` below, since #edit has to look the app up. */
function handleFormLink() {
  if (!dialog) return;
  const hash = window.location.hash;
  if (hash !== '#add' && !hash.startsWith('#edit/')) return;
  // Clear it, or a reload reopens the form.
  history.replaceState(null, '', window.location.pathname);
  if (hash === '#add') return openForm(null);
  const found = byName(decodeURIComponent(hash.slice(6)));
  if (found) openForm(found);
}

apps = initial();
render();
handleFormLink();
setInterval(refresh, POLL_MS);
refresh();
