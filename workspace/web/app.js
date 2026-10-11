(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const encoder = new TextEncoder();
  let token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
  try {
    if (token) sessionStorage.setItem('whitecube.access', token);
    else token = sessionStorage.getItem('whitecube.access') || '';
    if (location.hash && /^https?:$/.test(location.protocol)) window.history.replaceState(null, '', location.pathname + location.search);
  } catch (_) { /* Keep the fragment token if session storage is unavailable. */ }
  const state = {catalog: {projects: [], scripts: [], network: {}}, sessions: [], active: null, creating: false, projectStarting:false, connected: false, history: [], labs: [], workspace: 0, workflows: [], jobs: []};
  const workspaceNames = ['Coding', 'Play', 'Monitor', 'Browse'];
  const workspaces = workspaceNames.map(name => ({name, active: null, terminalVisible: false, terminalMaximized: false, appVisible: false, appMaximized: false, project: null, frame: null, panes: [{id:'pane-1', session:null}], axis:'horizontal', sizes:[1], focusedPane:'pane-1', maximizedPane:null}));
  workspaces[0].frame = $('app-frame');
  function validWorkspace(value) { return Number.isInteger(value) && value >= 0 && value < 4 ? value : 0; }
  let savedSessions = [];
  try {
    const saved = JSON.parse(sessionStorage.getItem('whitecube.console.sessions') || '[]');
    if (Array.isArray(saved)) savedSessions = saved.filter(s => typeof s.id === 'string' && /^[a-f0-9]{32}$/.test(s.id) && typeof s.name === 'string').slice(0, 8);
  } catch (_) { /* Storage is optional in a file preview. */ }
  let toastTimer, layoutTimer, restoringLayout = true, selectedProject = null, lastProjectSession = null, drawerOpener = null;
  let settings = {ui_scale:1,terminal_scale:1,contrast:'normal',reduced_motion:matchMedia('(prefers-reduced-motion: reduce)').matches};
  let retainedSessionsReady = Promise.resolve();
  function toast(message) {
    $('toast').textContent = message; $('toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6500);
  }
  async function api(path, options = {}) {
    if (!/^https?:$/.test(location.protocol)) throw new Error('Desktop preview — start the local Whitecube server to use projects and Bash.');
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 12000);
    let response;
    try {
      response = await fetch(path, {...options, signal: controller.signal, headers: {
        'Content-Type': 'application/json', 'X-Whitecube-Token': token, ...(options.headers || {})
      }});
      let result = {};
      if (response.status !== 204) {
        try { result = await response.json(); }
        catch (error) { if (error.name === 'AbortError') throw error; throw new Error('The desktop service returned invalid JSON. Try again.'); }
      }
      if (!response.ok) {
        const error = new Error(result.error || (response.status === 403 ? 'This desktop link is missing its access token. Open the link from the Whitecube launcher.' : `Desktop service returned ${response.status}.`));
        error.status = response.status; throw error;
      }
      return result;
    } catch (error) {
      if (error.status || (response && error.name !== 'AbortError')) throw error;
      throw new Error(error.name === 'AbortError' ? 'The local desktop service timed out.' : 'The local desktop service is unavailable. Start Whitecube and try again.');
    } finally { clearTimeout(timeout); }
  }
  function connection(connected, label) {
    state.connected = connected;
    $('connection-status').textContent = label || (connected ? 'LOCAL SYSTEM ONLINE' : 'SERVICE OFFLINE');
    $('connection-dot').className = `status-dot ${connected ? 'connected' : 'offline'}`;
  }
  function closeDrawers() {
    const restore = drawerOpener && document.activeElement?.closest('.side-panel');
    for (const id of ['library-panel', 'scripts-panel', 'network-panel', 'labs-panel', 'health-panel', 'recovery-panel', 'settings-panel']) $(id).hidden = true;
    for (const id of ['library-button', 'games-button', 'network-button', 'labs-button', 'health-button', 'recovery-button', 'settings-button']) { $(id).classList.remove('active'); $(id).setAttribute('aria-expanded','false'); }
    if (restore) drawerOpener.focus();
  }
  function drawer(id, button) {
    const opening = $(id).hidden; closeDrawers();
    $(id).hidden = !opening; $(button).classList.toggle('active', opening);
    $(button).setAttribute('aria-expanded',String(opening));
    if (opening) drawerOpener = $(button);
    if (opening && id === 'library-panel') $('project-search').focus();
    else if (opening && id === 'scripts-panel') $('script-search').focus();
    else if (opening) ($(id).querySelector?.('button,input,select,summary') || $(id)).focus();
  }
  $('library-button').onclick = () => drawer('library-panel', 'library-button');
  $('games-button').onclick = () => drawer('scripts-panel', 'games-button');
  $('network-button').onclick = () => { drawer('network-panel', 'network-button'); if (!$('network-panel').hidden) refreshNetwork(); };
  $('labs-button').onclick = () => { drawer('labs-panel', 'labs-button'); if (!$('labs-panel').hidden) refreshLabs(); };
  $('desktop-labs-button').onclick=()=>{const button=$('desktop-labs-button');button.classList.add('lab-launching');setTimeout(()=>button.classList.remove('lab-launching'),900);if($('labs-panel').hidden)drawer('labs-panel','labs-button');refreshLabs();};
  $('health-button').onclick = () => { drawer('health-panel', 'health-button'); if (!$('health-panel').hidden) refreshHealth(); };
  $('recovery-button').onclick = () => { drawer('recovery-panel','recovery-button'); if (!$('recovery-panel').hidden) refreshBackups(); };
  $('settings-button').onclick = () => { drawer('settings-panel','settings-button'); $('workspace-name').value=workspaces[state.workspace].name; };
  for (const element of document.querySelectorAll('.close-panel')) element.onclick = closeDrawers;
  $('home-button').onclick = event => { event.preventDefault(); closeDrawers(); $('app-panel').hidden = true; workspaces[state.workspace].appVisible = false; hideTerminal(); scheduleLayout(); };
  $('help-button').onclick = () => $('help-dialog').showModal();
  $('help-close').onclick = () => $('help-dialog').close();
  function card(item, index, action, script = false) {
    const button = document.createElement('button'); button.className = 'project-card';
    const icon = document.createElement('span'); icon.className = 'project-icon'; icon.textContent = script ? '>_' : ['◇','✣','◉','▦','⌘'][index % 5];
    const copy = document.createElement('span'); copy.className = 'project-copy';
    const name = document.createElement('strong'); name.textContent = item.name;
    const description = document.createElement('small'); description.textContent = item.description || (script ? 'Launch in your Bash console' : 'Open your local browser project');
    const arrow = document.createElement('span'); arrow.className = 'project-arrow'; arrow.textContent = '↗';
    copy.append(name, description); button.append(icon, copy, arrow);
    button.onclick = () => action(item); return button;
  }
  function renderProjects() {
    const needle = $('project-search').value.toLowerCase().trim();
    $('project-list').replaceChildren();
    const projects = state.catalog.projects.filter(p => matchesSearch(`${p.name} ${p.id} ${p.description || ''} ${(Array.isArray(p.tags) ? p.tags : []).join(' ')}`, needle));
    $('project-count').textContent = `${projects.length} projects`;
    projects.forEach((project, i) => {
      const entry=card(project,i,item=>{configureProject(item); if(item.url || item.kind==='command') openProject(item);});
      if (Object.keys(project.commands || {}).length || project.build || project.test || project.preview) {
        const wrapper=document.createElement('div'); wrapper.className='project-entry';
        wrapper.append(entry,smallButton('Build / test / preview…',()=>configureProject(project))); $('project-list').append(wrapper);
      } else $('project-list').append(entry);
    });
    if (!projects.length) empty('project-list', state.connected ? 'No projects match this search.' : 'Projects become available when the local desktop service is running.');
  }
  function empty(id, message) { const p = document.createElement('p'); p.className = 'empty-state'; p.textContent = message; $(id).replaceChildren(p); }
  $('project-search').oninput = renderProjects;
  function matchesSearch(text, query) {
    const folded = String(text).normalize('NFKC').toLocaleLowerCase();
    return query.normalize('NFKC').toLocaleLowerCase().split(/\s+/).filter(Boolean).every(word => folded.includes(word));
  }
  for (const [search, list] of [['project-search', 'project-list'], ['script-search', 'script-list']]) {
    $(search).addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') { event.preventDefault(); $(list).querySelector('.project-card')?.focus(); }
      if (event.key === 'Escape' && $(search).value) { event.preventDefault(); event.stopPropagation(); $(search).value = ''; $(search).dispatchEvent(new Event('input')); }
    });
    $(list).addEventListener('keydown', event => {
      const cards = [...$(list).querySelectorAll('.project-card')], index = cards.indexOf(document.activeElement);
      if (index < 0) return;
      const next = {ArrowDown:index + 1, ArrowUp:index - 1, Home:0, End:cards.length - 1}[event.key];
      if (next === undefined) return;
      event.preventDefault(); if (next < 0) $(search).focus(); else cards[Math.min(next, cards.length - 1)]?.focus();
    });
  }
  async function openProject(project, restoreView = false) {
    if (!restoreView && (project.kind === 'command' || project.launch?.kind === 'command' || project.launch?.type === 'command')) {
      try {
        const result = await api(`/api/project/${encodeURIComponent(project.id)}/launch`, {method: 'POST', body: '{}'});
        if (result.id) await attachAction(result, project.name);
        if (!result.url) return;
        project = {...project, url: result.url}; hideTerminal(); toast(`${project.name} is starting. Use Reload if its page is not ready yet.`);
      } catch (error) { return toast(error.message); }
    }
    let url;
    try { url = new URL(project.url, location.href); } catch (_) { return toast('This project has no valid local address.'); }
    if (!['http:', 'https:'].includes(url.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.origin === location.origin) {
      return toast('Projects need the separate local app server. Restart the desktop launcher.');
    }
    closeDrawers(); $('app-title').textContent = project.name;
    const workspace = workspaces[state.workspace];
    if (!workspace.frame) { workspace.frame = document.createElement('iframe'); workspace.frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads allow-pointer-lock'); $('app-panel').append(workspace.frame); }
    for (const item of workspaces) if (item.frame) item.frame.hidden = item !== workspace;
    workspace.project = {...project, url: url.href}; workspace.appVisible = true;
    $('app-external').href = url.href; workspace.frame.title = `${project.name} workspace`;
    workspace.frame.src = url.href; $('app-panel').hidden = false;
    scheduleLayout();
  }
  $('app-close').onclick = () => { $('app-panel').hidden = true; const workspace = workspaces[state.workspace]; workspace.appVisible = false; workspace.project = null; if (workspace.frame) workspace.frame.src = 'about:blank'; scheduleLayout(); };
  $('app-reload').onclick = () => { const workspace = workspaces[state.workspace]; if (workspace.frame && workspace.project) workspace.frame.src = workspace.project.url; };
  $('app-maximize').onclick = () => {
    const expanded = $('app-panel').classList.toggle('maximized');
    $('app-maximize').textContent = expanded ? '▣' : '□';
    $('app-maximize').setAttribute('aria-label', expanded ? 'Restore project size' : 'Maximize project');
    scheduleLayout();
  };
  function renderNetwork() {
    const network = state.catalog.network || {}, list = document.createElement('dl');
    const device = typeof network.device === 'object' && network.device ? network.device : {};
    const iface = typeof network.interface === 'object' && network.interface ? network.interface : {};
    const interfaceName = iface.interface || (typeof network.interface === 'string' ? network.interface : network.iface);
    const byteCount = bytes => typeof bytes === 'number' ? `${(bytes / 1048576).toFixed(2)} MiB` : undefined;
    const rows = [
      ['Interface', interfaceName], ['Adapter', network.adapter || network.model],
      ['USB identity', device.usb_id || iface.usb_id || network.usb_id || network.usbId], ['Driver', device.driver || iface.driver || network.driver],
      ['Device', typeof network.present === 'boolean' ? network.present ? 'Attached' : 'Not attached' : undefined],
      ['Link state', iface.operstate || network.state || network.status], ['Association', network.association_state],
      ['Carrier', typeof iface.carrier === 'boolean' ? iface.carrier ? 'Detected' : 'Not detected' : undefined],
      ['Address', network.address || network.ipv4], ['Signal', typeof network.signal_dbm === 'number' ? `${network.signal_dbm} dBm` : 'Not reported'], ['Reconnects', typeof (network.reconnect_count ?? network.reconnects) === 'number' ? (network.reconnect_count ?? network.reconnects) : 'Not reported'], ['Controller restarts', network.service_restart_count], ['MAC', iface.mac || device.mac],
      ['Received', byteCount(iface.rx_bytes)], ['Sent', byteCount(iface.tx_bytes)],
      ['Wi-Fi profile', typeof network.credentials_configured === 'boolean' ? network.credentials_configured ? 'Configured' : 'Not configured' : undefined],
      ['Controller', network.controller || network.stack], ['Modules', network.modules]
    ];
    for (const [name, raw] of rows) {
      if (raw === undefined || raw === null || raw === '') continue;
      const row = document.createElement('div'), dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = name; dd.textContent = Array.isArray(raw) ? raw.join(', ') : typeof raw === 'object' ? JSON.stringify(raw) : String(raw);
      row.append(dt, dd); list.append(row);
    }
    $('network-details').replaceChildren(list);
    $('network-note').textContent = network.note || network.message || (list.children.length ? 'Live interface details from your local system.' : 'Interface information is not available yet.');
    const label = interfaceName;
    $('network-label').textContent = label ? String(label).toUpperCase() : 'LOCAL WORKSPACE';
    renderProfiles(network);
    renderDiagnostics(network);
    if (network.last_error) $('network-note').textContent += ` ${network.last_error}`;
    if (Array.isArray(network.errors) && network.errors.length) $('network-note').textContent += ` ${network.errors.join(' · ')}`;
  }
  async function loadCatalog() {
    try {
      const catalog = await api('/api/catalog');
      state.catalog = {projects: Array.isArray(catalog.projects) ? catalog.projects : [], scripts: Array.isArray(catalog.scripts) ? catalog.scripts : [], network: catalog.network || {}};
      connection(true); renderProjects(); renderNetwork(); renderScripts();
    } catch (error) {
      connection(false, location.protocol === 'file:' ? 'DESKTOP PREVIEW' : 'SERVICE OFFLINE');
      empty('project-list', error.message); empty('script-list', error.message);
      $('network-note').textContent = error.message;
    }
  }
  function toBase64(text) { const bytes = encoder.encode(text); let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte); return btoa(binary); }
  function fromBase64(text) { const binary = atob(text); return Uint8Array.from(binary, c => c.charCodeAt(0)); }
  function setTerminalState(text, retry = false) { $('terminal-state').textContent = text; $('terminal-reconnect').hidden = !retry; }
  function terminalError(message) { $('terminal-empty').textContent = message; $('terminal-empty').hidden = false; setTerminalState('UNAVAILABLE', true); }
  function renderSessionPicker() {
    const picker = $('terminal-sessions'); picker.replaceChildren();
    for (const session of state.sessions.filter(s => s.workspace === state.workspace)) { const option = document.createElement('option'); option.value = session.id; option.textContent = session.name + (session.exited ? ' · exited' : ''); picker.append(option); }
    picker.hidden = state.sessions.filter(s => s.workspace === state.workspace).length < 2;
    if (state.active) picker.value = state.active.id;
    $('terminal-title').hidden = !picker.hidden;
    $('terminal-running').hidden = !state.sessions.some(s => !s.exited);
    try { sessionStorage.setItem('whitecube.console.sessions', JSON.stringify(state.sessions.map(s => ({id: s.id, name: s.name, workspace: s.workspace})))); } catch (_) { /* Optional persistence. */ }
  }
  function selectSession(session) {
    state.active = session; const workspace=workspaces[state.workspace]; workspace.active = session.id;
    const existingPane=workspace.panes.find(p=>p.session===session.id);
    const pane=existingPane || workspace.panes.find(p=>p.id===workspace.focusedPane) || workspace.panes[0];
    if (!existingPane) pane.session=session.id;
    if(workspace.maximizedPane)workspace.maximizedPane=pane.id;
    workspace.focusedPane=pane.id; renderPanes();
    $('terminal-empty').hidden = true; $('terminal-title').textContent = session.name;
    renderSessionPicker(); setTerminalState(session.exited ? 'SESSION EXITED' : 'LIVE PTY', session.exited);
    requestAnimationFrame(() => { fitTerminal(); session.term.focus(); });
    scheduleLayout();
  }
  $('terminal-sessions').onchange = event => { const session = state.sessions.find(s => s.id === event.target.value); if (session) selectSession(session); };
  async function createSession(script, existing) {
    if (state.creating) return null;
    if(!existing && state.projectStarting){toast('A project command is starting. Wait for its console before opening another session.');return null;}
    if (state.sessions.length >= 8) { toast('Eight console sessions are open. Select and close a finished session to open another.'); return null; }
    if (!window.Terminal || !window.FitAddon?.FitAddon) { terminalError('The local terminal files are missing. Run the Whitecube installer to restore its bundled terminal assets.'); return null; }
    state.creating = true; setTerminalState('CONNECTING');
    let createdId = null, pendingTerm = null, pendingElement = null;
    try {
      const result = existing || await api('/api/terminal', {method: 'POST', body: JSON.stringify(script ? {script: script.id, ...(Array.isArray(script.args) ? {args: script.args} : {})} : {})});
      if (!result.id) throw new Error('The desktop service did not return a terminal session.');
      if (!existing) createdId = String(result.id);
      const element = document.createElement('div'); element.className = 'terminal-session'; $('terminal').append(element);
      pendingElement = element;
      const term = new window.Terminal({cursorBlink: !settings.reduced_motion, screenReaderMode:true, fontFamily: '"DejaVu Sans Mono", "Liberation Mono", monospace', fontSize: 13*settings.terminal_scale, lineHeight: 1.22, scrollback: 4000, allowProposedApi: false, theme: {background: '#0c111a', foreground: '#d6e2ec', cursor: '#a9f1da', selectionBackground: '#385166', black: '#18222f', red: '#ed939b', green: '#a9dbc1', yellow: '#dfcc9d', blue: '#91b5e0', magenta: '#c5a6db', cyan: '#9fd2d7', white: '#e4edf4', brightBlack: '#657c8e', brightRed: '#ffb4bb', brightGreen: '#c1f5da', brightYellow: '#fbe4af', brightBlue: '#b3d4fc', brightMagenta: '#e0c2f5', brightCyan: '#c0f5f5', brightWhite: '#ffffff'}});
      pendingTerm = term;
      const fit = new window.FitAddon.FitAddon(); term.loadAddon(fit); term.open(element);
      term.attachCustomKeyEventHandler(event => !(event.ctrlKey && (event.code === 'Backquote' || (event.shiftKey && ['Enter', 'KeyP'].includes(event.code)) || (event.altKey && /^Digit[1-4]$/.test(event.code)))));
      const session = {workspace: existing ? validWorkspace(existing.workspace ?? state.workspace) : state.workspace, id: String(result.id), name: existing ? existing.name : script ? script.name : `Bash ${state.sessions.length + 1}`, term, fit, element, offset: 0, exited: false, stopped: false, inputQueue: Promise.resolve(), lastSize: '',projectAction:existing?.projectAction,project:existing?.project,previewURL:existing?.previewURL};
      term.onData(data => { session.inputQueue = session.inputQueue.then(() => api(`/api/terminal/${encodeURIComponent(session.id)}/input`, {method: 'POST', body: JSON.stringify({data: toBase64(data)})})).catch(error => toast(error.message)); });
      state.sessions.push(session);
      if(existing?.background){const workspace=workspaces[session.workspace];if(!workspace.panes.some(p=>p.session===session.id)){const free=workspace.panes.find(p=>!p.session);if(free)free.session=session.id;}if(!workspace.active)workspace.active=session.id;session.element.hidden=true;renderPanes();renderSessionPicker();}
      else if (session.workspace === state.workspace) selectSession(session);
      else { session.element.hidden = true; renderSessionPicker(); }
      poll(session); return session;
    } catch (error) {
      if (createdId) {
        try { await api(`/api/terminal/${encodeURIComponent(createdId)}`, {method: 'DELETE'}); } catch (_) { /* Server cleanup is also bounded by its own lifecycle. */ }
        state.sessions = state.sessions.filter(s => s.id !== createdId);
      }
      if (pendingTerm) pendingTerm.dispose(); if (pendingElement) pendingElement.remove();
      terminalError(error.message); return null;
    }
    finally { state.creating = false; }
  }
  async function poll(session) {
    if (session.stopped || session.exited) return;
    let delay = document.hidden ? 3000 : state.active === session && !$('terminal-panel').hidden ? 110 : 700;
    try {
      const result = await api(`/api/terminal/${encodeURIComponent(session.id)}?offset=${session.offset}`);
      if (result.truncated) { session.term.reset(); toast(`${session.name}: earlier output was trimmed; showing retained terminal output.`); }
      if (result.output) session.term.write(fromBase64(result.output));
      if (Number.isSafeInteger(result.offset) && result.offset >= 0) session.offset = result.offset;
      session.exited = !!result.exited;
      session.exitCode=result.job_exit_code ?? result.exit_code;
      if (session.projectAction) {
        $('project-run-status').textContent=`${session.name}: ${result.job_status || 'running'}${session.exitCode !== undefined && session.exitCode !== null ? ` · exit ${session.exitCode}`:''}`;
        if (session.projectAction==='preview' && result.preview_ready === true && !session.previewOpened) {
          session.previewOpened=true; openProject({...session.project,url:result.preview_url || session.previewURL},true);
        }
      }
      if (result.job_status && result.job_status !== session.jobStatus) {
        session.jobStatus = result.job_status;
        if (['finished', 'interrupted', 'failed', 'exited', 'completed'].includes(result.job_status)) {
          refreshHistory(); if (session.action) { refreshLabs(); refreshNetwork(); refreshHealth(); }
        }
      }
      if (state.active === session) setTerminalState(session.exited ? 'SESSION EXITED' : 'LIVE PTY', session.exited);
      if (session.exited) { renderSessionPicker(); refreshHistory(); if (session.action) { refreshLabs(); refreshNetwork(); refreshHealth(); } return; }
    } catch (error) {
      if (state.active === session) setTerminalState(error.status === 404 ? 'SESSION EXPIRED' : 'RECONNECTING', true);
      if (error.status === 404 || error.status === 410) { session.exited = true; renderSessionPicker(); return; }
      delay = 2500;
    }
    setTimeout(() => poll(session), delay);
  }
  function fitTerminal() {
    if ($('terminal-panel').hidden) return;
    for (const session of state.sessions.filter(s=>s.workspace===state.workspace && !s.element.hidden)) fitSession(session);
  }
  function fitSession(session) {
    try {
      session.fit.fit(); const cols = Math.max(2, Math.min(500, session.term.cols)), rows = Math.max(1, Math.min(200, session.term.rows));
      if (cols !== session.term.cols || rows !== session.term.rows) session.term.resize(cols, rows);
      if (state.active===session) $('terminal-size').textContent = `${cols} × ${rows} / BASH`;
      const size = `${cols},${rows}`;
      if (session.lastSize !== size && !session.exited) {
        session.lastSize = size;
        api(`/api/terminal/${encodeURIComponent(session.id)}/resize`, {method: 'POST', body: JSON.stringify({cols, rows})}).catch(error => { session.lastSize = ''; toast(error.message); });
      }
    } catch (_) { /* Fit waits until the terminal is attached and visible. */ }
  }
  async function showTerminal() {
    await retainedSessionsReady;
    closeDrawers(); $('terminal-panel').hidden = false; $('terminal-button').classList.add('active');
    if (!state.active && savedSessions.length) {
      const restore = savedSessions; savedSessions = [];
      for (const session of restore) {
        try { await api(`/api/terminal/${encodeURIComponent(session.id)}?offset=0`); await createSession(null, session); }
        catch (_) { /* The local server may have restarted since the last page load. */ }
      }
    }
    if (!state.active) await createSession(); else selectSession(state.active);
    scheduleLayout();
  }
  function hideTerminal() { $('terminal-panel').hidden = true; $('terminal-button').classList.remove('active'); scheduleLayout(); }
  function maximizeTerminal() {
    const expanded = $('terminal-panel').classList.toggle('maximized');
    $('terminal-maximize').textContent = expanded ? '▣' : '□';
    $('terminal-maximize').setAttribute('aria-label', expanded ? 'Restore console size' : 'Maximize console');
    requestAnimationFrame(fitTerminal); state.active?.term.focus();
    scheduleLayout();
  }
  async function runScript(script) {
    if (!state.active && savedSessions.length) await showTerminal();
    closeDrawers(); $('terminal-panel').hidden = false; $('terminal-button').classList.add('active');
    $('terminal-panel').classList.add('maximized'); $('terminal-maximize').textContent = '▣';
    $('terminal-maximize').setAttribute('aria-label', 'Restore console size');
    const session = await createSession(script); if (session) { toast(`${script.name} started in its own console.`); refreshHistory(); }
  }
  $('terminal-button').onclick = () => $('terminal-panel').hidden ? showTerminal() : hideTerminal();
  $('terminal-hide').onclick = hideTerminal; $('terminal-maximize').onclick = maximizeTerminal;
  $('terminal-new').onclick = () => createSession(); $('terminal-reconnect').onclick = async () => { await reconnectSessions(); renderPanes(); if(state.active && !state.active.exited) setTerminalState('RECONNECTING'); };
  $('terminal-split-horizontal').onclick=()=>splitPane('horizontal'); $('terminal-split-vertical').onclick=()=>splitPane('vertical');
  $('terminal-close-session').onclick = async () => {
    const session = state.active; if (!session) return;
    if (!session.exited && !confirm(`Close “${session.name}”? Any running shell command or game in this session will stop. Hide the panel instead to keep it running.`)) return;
    try { await api(`/api/terminal/${encodeURIComponent(session.id)}`, {method: 'DELETE'}); }
    catch (error) { if (![404, 410].includes(error.status)) { toast(error.message); return; } }
    session.stopped = true; session.term.dispose(); session.element.remove(); state.sessions = state.sessions.filter(s => s !== session);
    for (const workspace of workspaces) for(const pane of workspace.panes) if(pane.session===session.id) pane.session=null;
    const available = state.sessions.filter(s => s.workspace === state.workspace);
    if (available.length) selectSession(available[available.length - 1]);
    else { state.active = null; renderSessionPicker(); $('terminal-empty').hidden = false; $('terminal-empty').textContent = 'Console closed. Press + to open a new Bash session.'; setTerminalState('NO SESSION'); renderPanes(); }
    scheduleLayout();
  };
  new ResizeObserver(() => requestAnimationFrame(fitTerminal)).observe($('terminal'));
  document.addEventListener('keydown', event => {
    if (document.querySelector('dialog[open]')) return;
    if (event.ctrlKey && event.code === 'Backquote') { event.preventDefault(); $('terminal-panel').hidden ? showTerminal() : hideTerminal(); }
    else if (event.ctrlKey && event.shiftKey && event.code === 'Enter') { event.preventDefault(); if ($('terminal-panel').hidden) showTerminal(); maximizeTerminal(); }
    else if (event.ctrlKey && event.shiftKey && event.code === 'KeyP') { event.preventDefault(); drawer('library-panel', 'library-button'); }
    else if (event.ctrlKey && event.altKey && /^Digit[1-4]$/.test(event.code)) { event.preventDefault(); switchWorkspace(Number(event.code.slice(-1)) - 1); }
    else if (event.key === 'Escape' && !event.target.closest('#terminal')) { event.preventDefault(); closeDrawers(); }
  });
  function switchWorkspace(index, restore = false) {
    index = validWorkspace(index); if (index === state.workspace && !restore) return;
    const current = workspaces[state.workspace];
    if(!restore){current.active = state.active?.id || null; current.terminalVisible = !$('terminal-panel').hidden;
      current.terminalMaximized = $('terminal-panel').classList.contains('maximized');
      current.appVisible = !$('app-panel').hidden; current.appMaximized = $('app-panel').classList.contains('maximized');}
    state.workspace = index; const next = workspaces[index]; closeDrawers();
    state.active = state.sessions.find(session => session.id === next.active && session.workspace === index) || null;
    renderPanes();
    $('terminal-panel').hidden = !next.terminalVisible; $('terminal-panel').classList.toggle('maximized', next.terminalMaximized);
    $('terminal-button').classList.toggle('active', next.terminalVisible);
    $('terminal-maximize').textContent = next.terminalMaximized ? '▣' : '□';
    $('terminal-maximize').setAttribute('aria-label', next.terminalMaximized ? 'Restore console size' : 'Maximize console');
    if (state.active) selectSession(state.active); else { renderSessionPicker(); $('terminal-title').textContent = 'Bash console'; $('terminal-empty').hidden = false; $('terminal-empty').textContent = 'Open a shell in this workspace.'; setTerminalState('READY WHEN YOU ARE'); }
    for (const workspace of workspaces) if (workspace.frame) workspace.frame.hidden = workspace !== next;
    $('app-panel').hidden = !next.appVisible; $('app-panel').classList.toggle('maximized', next.appMaximized);
    $('app-maximize').textContent = next.appMaximized ? '▣' : '□';
    if (next.project) { $('app-title').textContent = next.project.name; $('app-external').href = next.project.url; }
    for (const button of document.querySelectorAll('[data-workspace]')) { const selected = Number(button.dataset.workspace) === index; button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected)); }
    $('workspace-label').textContent = `WORKSPACE 0${index + 1} / ${next.name.toUpperCase()}`;
    $('workspace-purpose').textContent = [`CODING / build something that lasts`, `PLAY / leave room for discovery`, `MONITOR / keep your systems in sight`, `BROWSE / follow your curiosity`][index];
    toast(`${next.name} · workspace ${index + 1}`);
    applyBounds('terminal',next.terminalBounds); applyBounds('app',next.appBounds); $('workspace-name').value=next.name; scheduleLayout();
  }
  for (const button of document.querySelectorAll('[data-workspace]')) button.onclick = () => switchWorkspace(Number(button.dataset.workspace));
  function smallButton(label, callback) { const button = document.createElement('button'); button.className = 'small-button'; button.textContent = label; button.onclick = callback; return button; }
  function textElement(tag, value, className = '') { const element = document.createElement(tag); element.textContent = value; element.className = className; return element; }
  let selectedScript = null;
  function configureScript(script) {
    selectedScript = script; $('script-dialog-title').textContent = script.name; $('script-dialog-description').textContent = script.description || 'Run this script in its own console.';
    const definitions = Array.isArray(script.arguments) ? script.arguments : [];
    $('script-argument-help').textContent = script.argument_help || (typeof script.arguments === 'string' ? script.arguments : definitions.filter(arg => typeof arg === 'string' || (arg && typeof arg === 'object')).map(arg => typeof arg === 'string' ? arg : `${arg.name || ''}${arg.required ? ' (required)' : ''}: ${arg.description || ''}`).join(' · ')) || 'Add optional arguments exactly as you would pass them to this script.';
    $('script-arguments').value = Array.isArray(script.args) ? script.args.join('\n') : '';
    $('script-dialog').showModal(); $('script-arguments').focus();
  }
  function renderScripts() {
    const needle = $('script-search').value.trim().toLowerCase(); $('script-list').replaceChildren();
    const scripts = state.catalog.scripts.filter(s => matchesSearch(`${s.name} ${s.id} ${s.description || ''} ${(Array.isArray(s.tags) ? s.tags : []).join(' ')}`, needle));
    $('script-count').textContent = `${scripts.length} scripts`;
    for (const [i, script] of scripts.entries()) {
      const entry = document.createElement('div'); entry.className = 'script-entry';
      entry.append(card(script, i, runScript, true), smallButton('Arguments…', () => configureScript(script))); $('script-list').append(entry);
    }
    if (!scripts.length) empty('script-list', 'No scripts match this search.');
  }
  $('script-search').oninput = () => { renderScripts(); renderHistory(); };
  $('script-dialog-close').onclick = () => $('script-dialog').close();
  $('script-run').onclick = async () => {
    if (!selectedScript) return;
    const raw = $('script-arguments').value, args = raw === '' ? [] : raw.split(/\r?\n/);
    if (args.length > 64 || args.some(arg => arg.length > 4096 || arg.includes('\0'))) return toast('Use at most 64 arguments, each up to 4,096 characters.');
    $('script-dialog').close(); await runScript({...selectedScript, args}); await refreshHistory();
  };
  $('scripts-tab').onclick = () => { $('script-list').hidden = false; $('run-history').hidden = true; $('scripts-tab').classList.add('selected'); $('history-tab').classList.remove('selected'); };
  $('history-tab').onclick = () => { $('script-list').hidden = true; $('run-history').hidden = false; $('history-tab').classList.add('selected'); $('scripts-tab').classList.remove('selected'); refreshHistory(); };
  function renderHistory() {
    $('run-history').replaceChildren(); const needle = $('script-search').value.trim().toLowerCase();
    const runs = state.history.filter(run => `${run.name || run.script_id} ${(run.args || []).join(' ')}`.toLowerCase().includes(needle));
    for (const run of runs.slice(0, 100)) {
      const entry = document.createElement('article'); entry.className = 'history-entry';
      const status = run.exit_code !== null && run.exit_code !== undefined ? `EXIT ${run.exit_code}` : (run.status || 'running').toUpperCase();
      entry.append(textElement('strong', run.name || run.script_id), textElement('span', status, `run-status ${run.exit_code === 0 ? 'pass' : run.exit_code ? 'fail' : ''}`));
      const rawDate = run.started_at, date = new Date(typeof rawDate === 'number' ? rawDate * 1000 : rawDate);
      entry.append(textElement('small', `${Number.isNaN(date.getTime()) ? '' : date.toLocaleString()}${run.args?.length ? ` · ${run.args.join(' · ')}` : ''}`));
      const script = state.catalog.scripts.find(s => s.id === run.script_id);
      if (script) entry.append(smallButton('Run again…', () => configureScript({...script, args: run.args || []})));
      const session = state.sessions.find(s => s.id === run.terminal_id);
      if (session) entry.append(smallButton('Open console', () => { switchWorkspace(session.workspace); $('terminal-panel').hidden = false; closeDrawers(); selectSession(session); }));
      $('run-history').append(entry);
    }
    if (!runs.length) empty('run-history', 'Your script launches and exit statuses will appear here.');
  }
  async function refreshHistory() { try { const result = await api('/api/history'); state.history = Array.isArray(result.runs) ? result.runs : []; renderHistory(); } catch (error) { empty('run-history', error.message); } }
  async function attachAction(result, name) {
    closeDrawers(); $('terminal-panel').hidden = false; $('terminal-button').classList.add('active');
    const session = await createSession(null, {...result, name: result.name || name, workspace: state.workspace});
    if (session) session.action = true; return session;
  }
  async function runAction(action, fields = {}, name = 'System control') {
    if (state.creating || state.projectStarting || state.sessions.length >= 8) { toast('Wait for the starting console, or close a finished console before opening another system action.'); return; }
    try { const result = await api('/api/action', {method: 'POST', body: JSON.stringify({action, ...fields})}); return await attachAction(result, name); }
    catch (error) { toast(error.message); }
  }
  async function refreshHealth() {
    $('health-refresh').disabled = true;
    try {
      const result = await api('/api/health'); const checks = Array.isArray(result.checks) ? result.checks : [];
      $('health-checks').replaceChildren();
      for (const check of checks) {
        const entry = document.createElement('article'), status = ['pass','warn','fail','pending'].includes(check.state) ? check.state : 'pending'; entry.className = `health-check ${status}`;
        entry.append(textElement('span', {pass:'✓',warn:'!',fail:'×',pending:'○'}[status], 'check-mark'), textElement('strong', check.label || check.id), textElement('small', check.detail || status)); $('health-checks').append(entry);
      }
      const passes = checks.filter(c => c.state === 'pass').length;
      $('health-summary').textContent = typeof result.summary === 'string' ? result.summary : `${passes} of ${checks.length} checks passed. Graphics, networking, shells and lab readiness are checked here.`;
      if (!checks.length) empty('health-checks', 'System check details are not available yet.');
      $('health-button').classList.toggle('needs-attention', checks.some(c => c.state === 'fail' || c.state === 'warn'));
      if (checks.length && !sessionStorage.getItem('whitecube.first-check.v1')) {
        sessionStorage.setItem('whitecube.first-check.v1','done');
        if (checks.some(c => c.state === 'fail' || c.state === 'warn')) toast('System checks found items to review. Open System in the dock.');
      }
    } catch (error) { $('health-summary').textContent = error.message; }
    finally { $('health-refresh').disabled = false; }
  }
  $('health-refresh').onclick = refreshHealth;
  function renderLabs() {
    $('lab-list').replaceChildren();
    for (const lab of state.labs) {
      const entry = document.createElement('article'); entry.className = 'lab-card';
      entry.append(textElement('strong', lab.name || lab.id), textElement('small', `${lab.state || 'Unknown'} · ${lab.size_bytes ? (lab.size_bytes / 1073741824).toFixed(0) : lab.disk_gib || 8} GiB disk`, 'lab-state'));
      const controls = document.createElement('div'); controls.className = 'action-row';
      controls.append(smallButton('Open shell', () => runAction('lab-enter', {lab: lab.id}, `${lab.name || lab.id} lab`)));
      const network = document.createElement('select'); network.setAttribute('aria-label', `${lab.name || lab.id} network`);
      for (const [value,label] of [['shared','Shared network'],['offline','Offline']]) { const option = document.createElement('option'); option.value=value; option.textContent=label; network.append(option); }
      network.value = lab.network_mode || 'shared'; network.onchange = () => { const mode=network.value; network.value=lab.network_mode||'shared'; runAction('lab-network',{lab:lab.id,mode},`${lab.name || lab.id} network`); }; controls.append(network); entry.append(controls);
      const row=document.createElement('div'); row.className='seed-row'; const name=document.createElement('input'); name.placeholder='Snapshot name'; name.maxLength=48; name.setAttribute('aria-label',`${lab.name || lab.id} snapshot name`);
      row.append(name,smallButton('Save snapshot',()=>{
        const value=name.value.trim(); if(!/^[a-z0-9][a-z0-9_-]{0,47}$/.test(value)) return toast('Use 1–48 lowercase letters, digits, underscores or dashes for a snapshot name.');
        runAction('lab-snapshot',{lab:lab.id,name:value},`${lab.name || lab.id} snapshot`);
      })); entry.append(row);
      const allSnapshots=Array.isArray(lab.snapshots)?lab.snapshots:[];
      const snapshots=allSnapshots.filter(snapshot => !snapshot.state || snapshot.state === 'ready');
      if(snapshots.length) {
        const restoreRow=document.createElement('div'); restoreRow.className='seed-row'; const select=document.createElement('select'); select.setAttribute('aria-label',`${lab.name || lab.id} snapshot to restore`);
        for(const snapshot of snapshots) { const option=document.createElement('option'); option.value=snapshot.name; option.textContent=snapshot.name; if(snapshot.created_utc || snapshot.created_at) option.title=String(snapshot.created_utc || snapshot.created_at); select.append(option); }
        restoreRow.append(select,smallButton('Restore…',()=>runAction('lab-restore',{lab:lab.id,name:select.value},`${lab.name || lab.id} restore`))); entry.append(restoreRow);
      } else entry.append(textElement('p','No snapshots saved yet.','field-note'));
      if(allSnapshots.length > snapshots.length) entry.append(textElement('p',`${allSnapshots.length - snapshots.length} incomplete or invalid snapshot(s) need inspection.`, 'field-note'));
      if(lab.detail || lab.note) entry.append(textElement('p',lab.detail || lab.note,'field-note'));
      const resources=lab.resources || lab.limits || {}, usage=lab.usage || {};
      const limits=document.createElement('div'); limits.className='resource-controls';
      const cpu=document.createElement('input'); cpu.type='number'; cpu.min='.1'; cpu.max='128'; cpu.step='.1'; cpu.value=String(resources.cpu ?? lab.cpu ?? 2); cpu.setAttribute('aria-label',`${lab.name || lab.id} CPU cores`);
      const memory=document.createElement('input'); memory.type='number'; memory.min='128'; memory.max='65536'; memory.step='128'; memory.value=String(resources.memory_mib ?? lab.memory_mib ?? 2048); memory.setAttribute('aria-label',`${lab.name || lab.id} memory MiB`);
      limits.append(textElement('label','CPU cores'),cpu,textElement('label','RAM MiB'),memory,smallButton('Apply limits',()=>{
        const cores=Number(cpu.value),mib=Number(memory.value); if(!Number.isFinite(cores)||cores<.1||cores>128||!Number.isInteger(mib)||mib<128||mib>65536) return toast('Choose 0.1–128 CPU cores and 128–65,536 MiB RAM.');
        return runAction('lab-limits',{lab:lab.id,cpu:cores,memory_mib:mib},`${lab.name || lab.id} resource limits`);
      })); entry.append(limits);
      const memoryBytes=usage.memory_current_bytes ?? usage.memory_current ?? usage.memory_bytes, cpuPercent=usage.cpu_percent;
      entry.append(textElement('p',`Live usage: ${typeof cpuPercent==='number'?`${cpuPercent.toFixed(1)}% CPU`:'CPU not available'} · ${typeof memoryBytes==='number'?`${(memoryBytes/1048576).toFixed(1)} MiB RAM`:'RAM not available'}`,'field-note'));
      if(usage.oom_kill || usage.oom_kills || usage.oom || usage.out_of_memory || lab.oom) entry.append(textElement('p',`Out of memory: ${usage.oom_kill || usage.oom_kills || 'a process was killed'}. Increase RAM or reduce the workload before restarting it.`,'fail field-note'));
      const cube=smallButton('◇ Enter lab',async()=>{
        cube.classList.add('lab-launching'); cube.disabled=true; cube.setAttribute('aria-busy','true');
        try { const session=await runAction('lab-enter',{lab:lab.id},`${lab.name || lab.id} lab`); if(session) toast(`${lab.name || lab.id} launch requested. The console shows authentication and runtime status.`); }
        finally { cube.classList.remove('lab-launching'); cube.disabled=false; cube.setAttribute('aria-busy','false'); }
      }); cube.classList.add('lab-cube-button'); entry.append(cube);
      entry.append(textElement('p', 'A saved snapshot uses 8 GiB. Network changes apply on the next launch.', 'field-note')); $('lab-list').append(entry);
    }
    if(!state.labs.length) empty('lab-list','Labs are not provisioned yet. System checks show the next setup step.');
  }
  async function refreshLabs() { $('labs-refresh').disabled=true; try { const result=await api('/api/labs'); state.labs=Array.isArray(result.labs)?result.labs:[]; renderLabs(); } catch(error) { empty('lab-list',error.message); } finally { $('labs-refresh').disabled=false; } }
  $('labs-refresh').onclick=refreshLabs;
  function renderProfiles(network) {
    $('network-profiles').replaceChildren();
    const profiles=Array.isArray(network.profiles)?network.profiles:Array.isArray(network.saved_profiles)?network.saved_profiles:[];
    for(const profile of profiles) {
      const row=document.createElement('div'); row.className='profile-row'; row.append(textElement('span',profile.name || profile.ssid || 'Saved Wi-Fi'));
      row.append(smallButton('Connect',()=>runAction('network-connect',{profile:profile.id},'Connect Wi-Fi')),smallButton('Remove…',()=>runAction('network-remove',{profile:profile.id},'Remove Wi-Fi profile'))); $('network-profiles').append(row);
    }
    if(!profiles.length) empty('network-profiles','No saved networks are available. Add Wi-Fi to configure this dongle.');
  }
  async function refreshNetwork() { try { const result=await api('/api/network'); state.catalog.network=result.network || result; renderNetwork(); } catch(error) { $('network-note').textContent=error.message; } }
  $('network-refresh').onclick=refreshNetwork;
  $('network-scan').onclick=()=>runAction('network-scan',{},'Scan nearby Wi-Fi');
  $('network-manage').onclick=()=>runAction('network-manage',{},'Dongle control');
  $('network-add').onclick=()=>runAction('network-add',{},'Add Wi-Fi profile');
  $('network-reconnect').onclick=()=>runAction('network-reconnect',{},'Reconnect scoped dongle');
  function renderDiagnostics(network) {
    const diagnosis=network.diagnostics || network.diagnosis || {}, code=diagnosis.state || diagnosis.code || network.diagnostic_code || network.diagnostic_state;
    const labels={missing_dongle:'Dongle missing',driver_problem:'Driver problem',association_failed:'Wi-Fi association failed',dhcp_failed:'DHCP failed',dns_failed:'DNS failed',connectivity_lost:'Connectivity lost',online:'Connected',missing:'Dongle missing',driver:'Driver problem',association:'Wi-Fi association failed',dhcp:'DHCP failed',dns:'DNS failed',connectivity:'Connectivity lost'};
    $('network-diagnostics').replaceChildren();
    if(code) $('network-diagnostics').append(textElement('strong',labels[code] || String(code).replaceAll('_',' ').replaceAll('-',' ')));
    if(diagnosis.detail || diagnosis.message || network.diagnostic_summary) $('network-diagnostics').append(textElement('p',diagnosis.detail || diagnosis.message || network.diagnostic_summary,'panel-description'));
    const actions=diagnosis.actions || diagnosis.recovery_actions || network.recovery_actions || [];
    for(const action of actions) {
      const description=typeof action==='string'?action:action.description || action.label || action.detail;
      if(description) $('network-diagnostics').append(textElement('p',description,'field-note'));
    }
    if(network.recovery_attempts !== undefined || network.retry_budget !== undefined || diagnosis.retry_budget !== undefined) $('network-diagnostics').append(textElement('p',`Bounded reconnect budget: ${network.retry_budget ?? diagnosis.retry_budget ?? 3}; attempts: ${network.recovery_attempts ?? network.reconnect_count ?? 0}${network.recovery_exhausted?' · paused until a deliberate reconnect':''}.`,'field-note'));
  }
  function configureProject(project) {
    selectedProject=project; $('project-workflow').hidden=false; $('project-workflow-title').textContent=project.name;
    for(const action of ['build','test','preview']) $( `project-${action}`).disabled=!(project.commands?.[action] || project[action]);
    $('project-run-status').textContent=project.project_mode==='rw' ? project.sharing_scope || 'Writable access: this entire project is shared at /project. Other projects are excluded.' : 'Project sharing is read-only. Set project_mode to rw in this project’s manifest to deliberately permit writes.';
    if($('library-panel').hidden) drawer('library-panel','library-button');
    $('project-lab').focus();
  }
  async function runProject(action) {
    if(!selectedProject) return;
    if(state.creating || state.projectStarting || state.sessions.length>=8)return toast('Wait for the starting console, or terminate a finished session before launching this project command.');
    state.projectStarting=true;
    const project=selectedProject,lab=$('project-lab').value;
    try {
      $('project-run-status').textContent=`${action}: starting…`;
      const result=await api('/api/project/run',{method:'POST',body:JSON.stringify({id:project.id,action,...(lab?{lab}: {})})});
      const session=await attachAction({...result,projectAction:action,project,previewURL:result.url},`${project.name} · ${action}`);
      if(session) {
        session.projectAction=action; session.project=project; session.previewURL=result.url; lastProjectSession=session;
        $('project-open-log').hidden=false; $('project-run-status').textContent=`${action}: running · logs in ${session.name}`;
        if(action==='preview' && result.preview_ready===true) { session.previewOpened=true; await openProject({...project,url:result.url},true); }
      }
    } catch(error) { $('project-run-status').textContent=`${action}: ${error.message}`; toast(error.message); }
    finally{state.projectStarting=false;}
  }
  for(const action of ['build','test','preview']) $(`project-${action}`).onclick=()=>runProject(action);
  $('project-open-log').onclick=()=>{if(lastProjectSession){switchWorkspace(lastProjectSession.workspace); $('terminal-panel').hidden=false; selectSession(lastProjectSession); closeDrawers(); scheduleLayout();}};

  // Panes only reparent an existing xterm; resizing, hiding and maximizing never launch a command.
  function renderPanes() {
    const workspace=workspaces[state.workspace],root=$('terminal'); root.replaceChildren();
    root.className=`pane-grid ${workspace.axis}`;
    for(const session of state.sessions) session.element.hidden=true;
    workspace.panes.forEach((pane,index)=>{
      const element=document.createElement('section'); element.className='terminal-pane'; element.dataset.pane=pane.id;
      element.style.flex=String(workspace.sizes[index] || 1); element.hidden=!!workspace.maximizedPane && workspace.maximizedPane!==pane.id;
      element.classList.toggle('focused',pane.id===workspace.focusedPane);
      const header=document.createElement('div'); header.className='pane-bar';
      const focus=smallButton(`Pane ${index+1}`,()=>focusPane(pane.id)); focus.setAttribute('aria-pressed',String(pane.id===workspace.focusedPane));
      const picker=document.createElement('select'); picker.setAttribute('aria-label',`Move session to pane ${index+1}`);
      const blank=document.createElement('option'); blank.value=''; blank.textContent='Choose session…'; picker.append(blank);
      for(const session of state.sessions) { const option=document.createElement('option'); option.value=session.id; option.textContent=`${session.name}${session.workspace!==state.workspace?' · other workspace':''}`; picker.append(option); }
      picker.value=pane.session || ''; picker.onchange=()=>moveSession(picker.value,pane.id);
      header.append(focus,picker,smallButton(workspace.maximizedPane===pane.id?'Restore':'Maximize',()=>maximizePane(pane.id)));
      if(workspace.panes.length>1) header.append(smallButton('Remove pane',()=>removePane(pane.id)));
      element.append(header);
      const session=state.sessions.find(s=>s.id===pane.session && s.workspace===state.workspace);
      if(session) { session.element.hidden=element.hidden; element.append(session.element); session.element.onpointerdown=()=>focusPane(pane.id,false); }
      else element.append(smallButton('Open Bash here',()=>{workspace.focusedPane=pane.id; return createSession();}));
      root.append(element);
      if(index<workspace.panes.length-1 && !workspace.maximizedPane) root.append(paneDivider(index));
    });
    requestAnimationFrame(fitTerminal);
  }
  function focusPane(id,focus=true) {
    const workspace=workspaces[state.workspace],pane=workspace.panes.find(p=>p.id===id); if(!pane) return;
    workspace.focusedPane=id; const session=state.sessions.find(s=>s.id===pane.session);
    if(session){state.active=session;workspace.active=session.id;$('terminal-title').textContent=session.name;renderSessionPicker();setTerminalState(session.exited?'SESSION EXITED':'LIVE PTY',session.exited);if(focus)session.term.focus();}
    else {state.active=null;renderSessionPicker();}
    // Keep the pointer target attached while clicking inside a running game.
    if(focus){renderPanes();session?.term.focus();}
    else for(const element of $('terminal').children)if(element.dataset.pane){element.classList.toggle('focused',element.dataset.pane===id);element.children[0]?.children[0]?.setAttribute('aria-pressed',String(element.dataset.pane===id));}
    scheduleLayout();
  }
  function splitPane(axis) {
    const workspace=workspaces[state.workspace]; if(workspace.panes.length>=4) return toast('Four panes are open. Remove a pane before splitting again.');
    workspace.axis=axis; let serial=1; while(workspace.panes.some(p=>p.id===`pane-${serial}`))serial++;
    workspace.panes.push({id:`pane-${serial}`,session:null});workspace.sizes=workspace.panes.map(()=>1);workspace.maximizedPane=null;workspace.focusedPane=`pane-${serial}`;
    renderPanes();scheduleLayout();
  }
  function moveSession(id,paneId) {
    const workspace=workspaces[state.workspace],pane=workspace.panes.find(p=>p.id===paneId),session=state.sessions.find(s=>s.id===id);
    if(!pane) return;
    for(const item of workspaces)for(const existing of item.panes)if(existing.session===id)existing.session=null;
    pane.session=session?.id || null;workspace.focusedPane=paneId;
    if(session){session.workspace=state.workspace;selectSession(session);}else renderPanes();scheduleLayout();
  }
  function removePane(id) {
    const workspace=workspaces[state.workspace];if(workspace.panes.length<2)return;
    const index=workspace.panes.findIndex(p=>p.id===id);if(index<0)return;
    workspace.panes.splice(index,1);workspace.sizes.splice(index,1);workspace.maximizedPane=null;workspace.focusedPane=workspace.panes[0].id;
    renderPanes();focusPane(workspace.focusedPane);toast('Pane removed. Its session remains available in the session picker.');scheduleLayout();
  }
  function maximizePane(id) {
    const workspace=workspaces[state.workspace];
    if(workspace.maximizedPane===id){workspace.maximizedPane=null;$('terminal-panel').classList.toggle('maximized',!!workspace.beforePaneMaximized);}
    else{workspace.beforePaneMaximized=$('terminal-panel').classList.contains('maximized');workspace.maximizedPane=id;$('terminal-panel').classList.add('maximized');}
    renderPanes();focusPane(id);scheduleLayout();
  }
  function resizePane(index,delta) {
    const workspace=workspaces[state.workspace],total=(workspace.sizes[index]||1)+(workspace.sizes[index+1]||1);
    const first=Math.max(total*.15,Math.min(total*.85,(workspace.sizes[index]||1)+delta));workspace.sizes[index]=first;workspace.sizes[index+1]=total-first;
    renderPanes();$('terminal').children[index*2+1]?.focus();scheduleLayout();
  }
  function paneDivider(index) {
    const divider=document.createElement('div'),workspace=workspaces[state.workspace];divider.className='pane-divider';divider.tabIndex=0;
    divider.setAttribute('role','separator');divider.setAttribute('aria-label',`Resize panes ${index+1} and ${index+2}`);divider.setAttribute('aria-orientation',workspace.axis==='horizontal'?'vertical':'horizontal');
    divider.setAttribute('aria-valuemin','15');divider.setAttribute('aria-valuemax','85');divider.setAttribute('aria-valuenow',String(Math.round(100*workspace.sizes[index]/(workspace.sizes[index]+workspace.sizes[index+1]))));
    divider.onkeydown=event=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();resizePane(index,['ArrowRight','ArrowDown'].includes(event.key)?.1:-.1);}};
    let previous=null;
    divider.onpointerdown=event=>{previous=workspace.axis==='horizontal'?event.clientX:event.clientY;divider.setPointerCapture?.(event.pointerId);};
    divider.onpointermove=event=>{if(previous===null)return;const current=workspace.axis==='horizontal'?event.clientX:event.clientY,extent=workspace.axis==='horizontal'?$('terminal').clientWidth:$('terminal').clientHeight;const delta=(current-previous)/Math.max(200,extent || 800)*workspace.sizes.reduce((a,b)=>a+b,0);previous=current;
      const total=workspace.sizes[index]+workspace.sizes[index+1],first=Math.max(total*.15,Math.min(total*.85,workspace.sizes[index]+delta));workspace.sizes[index]=first;workspace.sizes[index+1]=total-first;
      const panes=Array.from($('terminal').children).filter(e=>e.dataset.pane);if(panes[index])panes[index].style.flex=String(first);if(panes[index+1])panes[index+1].style.flex=String(total-first);requestAnimationFrame(fitTerminal);};
    divider.onpointerup=divider.onpointercancel=()=>{previous=null;renderPanes();scheduleLayout();};return divider;
  }
  async function reconnectSessions() {
    try {
      const result=await api('/api/sessions'),entries=Array.isArray(result.sessions)?result.sessions:[];
      for(const entry of entries.slice(0,8))if(typeof entry.id==='string' && /^[a-f0-9]{32}$/.test(entry.id) && !state.sessions.some(s=>s.id===entry.id)){
        const saved=savedSessions.find(s=>s.id===entry.id),owner=workspaces.findIndex(w=>w.panes.some(p=>p.session===entry.id));
        const session=await createSession(null,{...entry,name:entry.name || saved?.name || 'Retained Bash',background:true,workspace:owner>=0?owner:saved?.workspace ?? entry.workspace ?? 0});
        if(session && owner>=0)workspaces[owner].panes.find(p=>p.session===entry.id).session=entry.id;
      }
      savedSessions=[];renderPanes();renderSessionPicker();
    }catch(error){setTerminalState('SERVICE OFFLINE',true);if(!restoringLayout)toast(error.message);}
  }

  function captureWorkspace() {
    const current=workspaces[state.workspace];current.active=state.active?.id || null;current.terminalVisible=!$('terminal-panel').hidden;current.terminalMaximized=$('terminal-panel').classList.contains('maximized');current.appVisible=!$('app-panel').hidden;current.appMaximized=$('app-panel').classList.contains('maximized');
  }
  function layoutSnapshot() {
    captureWorkspace();return {version:1,workspace:state.workspace,workspaces:workspaces.map(({frame,beforePaneMaximized,...workspace})=>({...workspace,panes:workspace.panes.map(p=>({...p})),sizes:[...workspace.sizes],project:workspace.project?{id:workspace.project.id,name:workspace.project.name,url:workspace.project.url}:null}))};
  }
  function scheduleLayout() {
    if(restoringLayout)return;const layout=layoutSnapshot();try{localStorage.setItem('whitecube.layout.v1',JSON.stringify(layout));}catch(_){}
    clearTimeout(layoutTimer);layoutTimer=setTimeout(()=>saveLayout(),450);
  }
  async function saveLayout() {
    const layout=layoutSnapshot();try{localStorage.setItem('whitecube.layout.v1',JSON.stringify(layout));await api('/api/layout',{method:'POST',body:JSON.stringify(layout)});$('layout-status').textContent='Layout saved. Commands will not run when it is restored.';return true;}catch(error){$('layout-status').textContent=error.message;return false;}
  }
  function applyLayout(layout) {
    if(!layout || layout.version!==1 || !Array.isArray(layout.workspaces))return;
    layout.workspaces.slice(0,4).forEach((saved,index)=>{
      if(!saved || typeof saved!=='object')return;const workspace=workspaces[index];
      if(typeof saved.name==='string' && saved.name.trim())workspace.name=saved.name.trim().slice(0,40);
      for(const key of ['terminalVisible','terminalMaximized','appVisible','appMaximized'])workspace[key]=saved[key]===true;
      workspace.active=typeof saved.active==='string'?saved.active:null;workspace.project=saved.project && typeof saved.project.id==='string'?saved.project:null;
      workspace.axis=saved.axis==='vertical'?'vertical':'horizontal';
      if(Array.isArray(saved.panes) && saved.panes.length){const ids=new Set();workspace.panes=saved.panes.slice(0,4).filter(p=>p && typeof p.id==='string' && !ids.has(p.id) && ids.add(p.id)).map(p=>({id:p.id.slice(0,40),session:typeof p.session==='string'?p.session:null}));}
      if(!workspace.panes.length)workspace.panes=[{id:'pane-1',session:null}];
      workspace.sizes=workspace.panes.map((_,i)=>Number.isFinite(saved.sizes?.[i])?Math.max(.1,Math.min(100,saved.sizes[i])):1);
      workspace.focusedPane=workspace.panes.some(p=>p.id===saved.focusedPane)?saved.focusedPane:workspace.panes[0].id;
      workspace.maximizedPane=workspace.panes.some(p=>p.id===saved.maximizedPane)?saved.maximizedPane:null;
      for(const key of ['terminalBounds','appBounds'])if(saved[key])workspace[key]=validatedBounds(saved[key]);
      for(const pane of workspace.panes){const session=state.sessions.find(s=>s.id===pane.session);if(session)session.workspace=index;}
    });state.workspace=validWorkspace(layout.workspace);renderWorkspaceNames();
  }
  function validatedBounds(bounds) {const width=Math.max(25,Math.min(100,Number(bounds.width)||80)),height=Math.max(20,Math.min(90,Number(bounds.height)||60));return {x:Math.max(0,Math.min(100-width,Number(bounds.x)||0)),y:Math.max(8,Math.min(100-height,Number(bounds.y)||10)),width,height};}
  function applyBounds(prefix,bounds) {
    const panel=$(`${prefix}-panel`);for(const key of ['left','top','width','height','right','bottom'])panel.style[key]='';
    if(bounds){panel.style.left=`${bounds.x}vw`;panel.style.top=`${bounds.y}vh`;panel.style.width=`${bounds.width}vw`;panel.style.height=`${bounds.height}vh`;panel.style.right='auto';panel.style.bottom='auto';}
  }
  function renderWorkspaceNames(){for(const button of document.querySelectorAll('[data-workspace]')){const index=Number(button.dataset.workspace);const label=button.querySelector?.('span');if(label)label.textContent=workspaces[index].name;button.setAttribute('aria-label',`Workspace ${index+1}: ${workspaces[index].name}`);}$('workspace-label').textContent=`WORKSPACE 0${state.workspace+1} / ${workspaces[state.workspace].name.toUpperCase()}`;}
  function windowPlacement(prefix) {
    const bar=$(`${prefix}-window-bar`),key=`${prefix}Bounds`;let drag=null;
    const bounds=()=>workspaces[state.workspace][key] || {x:prefix==='terminal'?2:4,y:prefix==='terminal'?35:12,width:prefix==='terminal'?96:92,height:60};
    const place=value=>{workspaces[state.workspace][key]=validatedBounds(value);applyBounds(prefix,workspaces[state.workspace][key]);requestAnimationFrame(fitTerminal);scheduleLayout();};
    bar.onkeydown=event=>{if(event.target!==bar || !event.altKey || !['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();const value={...bounds()},direction=['ArrowRight','ArrowDown'].includes(event.key)?2:-2;if(event.shiftKey)value[['ArrowLeft','ArrowRight'].includes(event.key)?'width':'height']+=direction;else value[['ArrowLeft','ArrowRight'].includes(event.key)?'x':'y']+=direction;place(value);};
    bar.onpointerdown=event=>{if(event.button!==0 || event.target.closest?.('button,a,select'))return;drag={x:event.clientX,y:event.clientY,bounds:{...bounds()}};bar.setPointerCapture?.(event.pointerId);};
    bar.onpointermove=event=>{if(drag)place({...drag.bounds,x:drag.bounds.x+(event.clientX-drag.x)/innerWidth*100,y:drag.bounds.y+(event.clientY-drag.y)/innerHeight*100});};bar.onpointerup=bar.onpointercancel=()=>{drag=null;};
  }
  windowPlacement('terminal');windowPlacement('app');
  $('layout-save').onclick=saveLayout;
  $('workspace-rename').onclick=()=>{const name=$('workspace-name').value.trim().slice(0,40);if(name){workspaces[state.workspace].name=name;renderWorkspaceNames();scheduleLayout();}};
  function applySettings(value) {
    settings={ui_scale:Math.max(.8,Math.min(1.5,Number(value.ui_scale)||1)),terminal_scale:Math.max(.8,Math.min(2,Number(value.terminal_scale)||1)),contrast:value.contrast==='strong'?'strong':'normal',reduced_motion:value.reduced_motion===true};
    document.body?.style.setProperty('--ui-scale',String(settings.ui_scale));document.body?.classList.toggle('strong-contrast',settings.contrast==='strong');document.body?.classList.toggle('reduced-motion',settings.reduced_motion);
    $('ui-scale').value=String(Math.round(settings.ui_scale*100));$('ui-scale-value').textContent=`${Math.round(settings.ui_scale*100)}%`;$('terminal-scale').value=String(Math.round(settings.terminal_scale*100));$('terminal-scale-value').textContent=`${Math.round(settings.terminal_scale*100)}%`;$('strong-contrast').checked=settings.contrast==='strong';$('reduced-motion').checked=settings.reduced_motion;
    for(const session of state.sessions){if(session.term.options){session.term.options.fontSize=13*settings.terminal_scale;session.term.options.cursorBlink=!settings.reduced_motion;}}window.WhitecubeWallpaper?.setReducedMotion(settings.reduced_motion);requestAnimationFrame(fitTerminal);
  }
  async function saveSettings() {
    applySettings({ui_scale:Number($('ui-scale').value)/100,terminal_scale:Number($('terminal-scale').value)/100,contrast:$('strong-contrast').checked?'strong':'normal',reduced_motion:$('reduced-motion').checked});
    try{localStorage.setItem('whitecube.settings.v1',JSON.stringify(settings));await api('/api/settings',{method:'POST',body:JSON.stringify(settings)});}catch(error){toast(error.message);}
  }
  for(const id of ['ui-scale','terminal-scale','strong-contrast','reduced-motion'])$(id).onchange=saveSettings;
  $('ui-scale').oninput=()=>{$('ui-scale-value').textContent=`${$('ui-scale').value}%`;};$('terminal-scale').oninput=()=>{$('terminal-scale-value').textContent=`${$('terminal-scale').value}%`;};
  async function restoreDesktop() {
    let layout=null;try{layout=JSON.parse(localStorage.getItem('whitecube.layout.v1')||'null');applySettings(JSON.parse(localStorage.getItem('whitecube.settings.v1')||'null')||settings);}catch(_){}
    const results=await Promise.allSettled([api('/api/layout'),api('/api/settings')]);if(results[0].status==='fulfilled' && results[0].value.version===1)layout=results[0].value;if(results[1].status==='fulfilled' && results[1].value.ui_scale)applySettings(results[1].value);
    applyLayout(layout);const wanted=state.workspace,activeIds=workspaces.map(w=>w.active),visibility=workspaces.map(w=>w.terminalVisible);switchWorkspace(wanted,true);
    await reconnectSessions();
    // Saved browser URLs are restored as views. A manifest launch is never called here.
    for(let index=0;index<4;index++){const workspace=workspaces[index];if(workspace.project?.url && workspace.appVisible){const old=state.workspace;state.workspace=index;await openProject(workspace.project,true);state.workspace=old;}}
    workspaces.forEach((workspace,index)=>{workspace.active=activeIds[index];workspace.terminalVisible=visibility[index];});
    const current=workspaces[wanted];switchWorkspace(wanted,true);state.active=state.sessions.find(s=>s.id===current.active) || state.sessions.find(s=>s.workspace===wanted) || null;
    if(state.active)selectSession(state.active);$('terminal-panel').hidden=!current.terminalVisible;renderPanes();restoringLayout=false;
  }

  let restoreReview=null;
  async function refreshBackups(){try{const result=await api('/api/backups');$('backup-retention').value=String(result.retention || 10);$('backup-list').replaceChildren();for(const backup of (Array.isArray(result)?result:result.backups || [])){const entry=document.createElement('article');entry.className='history-entry';entry.append(textElement('strong',backup.label || backup.id),textElement('small',`${backup.created_at || backup.created_utc || (backup.created?new Date(backup.created*1000).toLocaleString():'')} · ${backup.verified?'verified':backup.status || 'verification required'}${backup.size_bytes?` · ${(backup.size_bytes/1024).toFixed(1)} KiB`:''}`),smallButton('Verify',async()=>{try{const report=await api('/api/backups/verify',{method:'POST',body:JSON.stringify({id:backup.id})});toast(report.message || (report.verified===false?'Backup verification failed.':'Backup verification completed.'));await refreshBackups();}catch(error){toast(error.message);}}),smallButton('Review restore…',()=>reviewRestore(backup.id)));$('backup-list').append(entry);}if(!$('backup-list').children.length)empty('backup-list','No desktop backups saved yet.');}catch(error){empty('backup-list',error.message);}}
  async function reviewRestore(id){restoreReview=null;$('restore-apply').disabled=true;$('restore-review').textContent='Reading and verifying backup…';$('restore-status').textContent='';$('restore-dialog').showModal();try{const review=await api('/api/backups/review',{method:'POST',body:JSON.stringify({id})});restoreReview={id,review_token:review.review_token};$('restore-review').textContent=JSON.stringify(review,null,2);$('restore-apply').disabled=!review.review_token;}catch(error){$('restore-status').textContent=error.message;}}
  $('backup-refresh').onclick=refreshBackups;$('backup-create').onclick=async()=>{const retention=Number($('backup-retention').value);if(!Number.isInteger(retention)||retention<1||retention>50)return toast('Retention must be 1–50 backups.');$('backup-create').disabled=true;try{if(!await saveLayout())throw new Error('Save the desktop layout successfully before creating a backup.');await window.WhitecubeWallpaper?.syncSeeds();const backup=await api('/api/backups',{method:'POST',body:JSON.stringify({label:$('backup-label').value.trim(),retention})});await refreshBackups();toast(`Desktop backup saved.${backup.excluded?.length?` ${backup.excluded.length} credential-like or unsupported file(s) excluded; review the backup details.`:''}`);}catch(error){toast(error.message);}finally{$('backup-create').disabled=false;}};
  $('restore-close').onclick=()=>{$('restore-dialog').close();restoreReview=null;};$('restore-apply').onclick=async()=>{if(!restoreReview)return;$('restore-apply').disabled=true;clearTimeout(layoutTimer);restoringLayout=true;try{const result=await api('/api/backups/restore',{method:'POST',body:JSON.stringify(restoreReview)});await restoreDesktop();await window.WhitecubeWallpaper?.reloadSeeds();$('restore-status').textContent=result.message || 'Desktop state restored. Views, settings and seeds are refreshed; commands are not restarted.';restoreReview=null;await refreshBackups();}catch(error){restoringLayout=false;$('restore-status').textContent=error.message;$('restore-apply').disabled=false;}};

  function showScriptTab(tab){$('script-list').hidden=tab!=='library';$('run-history').hidden=tab!=='history';$('jobs-view').hidden=tab!=='jobs';for(const [id,name] of [['scripts-tab','library'],['history-tab','history'],['jobs-tab','jobs']])$(id).classList.toggle('selected',tab===name);if(tab==='history')refreshHistory();if(tab==='jobs')refreshJobs();}
  $('scripts-tab').onclick=()=>showScriptTab('library');$('history-tab').onclick=()=>showScriptTab('history');$('jobs-tab').onclick=()=>showScriptTab('jobs');
  function configureWorkflow(workflow){$('workflow-id').value=workflow.id;$('workflow-name').value=workflow.name;$('workflow-steps').value=JSON.stringify(workflow.steps,null,2);$('workflow-stop').checked=workflow.stop_on_failure!==false;$('workflow-notify').checked=workflow.notify===true;}
  const notifiedJobs=new Set();
  async function refreshJobs(){const results=await Promise.allSettled([api('/api/workflows'),api('/api/jobs')]);if(results[0].status==='fulfilled')state.workflows=results[0].value.workflows || [];if(results[1].status==='fulfilled')state.jobs=results[1].value.jobs || [];
    $('workflow-list').replaceChildren();for(const workflow of state.workflows){const entry=document.createElement('article');entry.className='history-entry';entry.append(textElement('strong',workflow.name || workflow.id),smallButton('Queue',()=>queueWorkflow(workflow)),smallButton('Edit',()=>configureWorkflow(workflow)));$('workflow-list').append(entry);}
    $('job-list').replaceChildren();for(const job of state.jobs){const entry=document.createElement('article');entry.className='history-entry';entry.append(textElement('strong',job.name || job.id),textElement('span',String(job.status || 'queued').toUpperCase(),'run-status'));for(const step of job.steps || []){const row=document.createElement('div');row.className='job-step';row.append(textElement('small',`${step.id}: ${step.status}${step.exit_code!==null && step.exit_code!==undefined?` · exit ${step.exit_code}`:''}`));const session=state.sessions.find(s=>s.id===step.session_id);if(session)row.append(smallButton('Live console',()=>{switchWorkspace(session.workspace);$('terminal-panel').hidden=false;closeDrawers();selectSession(session);}));if(step.log || step.error){const details=document.createElement('details');details.append(textElement('summary',`${step.id} logs`),textElement('pre',step.log || step.error,'job-log'));row.append(details);}entry.append(row);}if(['queued','running','pending'].includes(job.status))entry.append(smallButton('Cancel',async()=>{try{await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`,{method:'POST',body:'{}'});await refreshJobs();}catch(error){toast(error.message);}}));$('job-list').append(entry);
      if(job.notify && ['finished','completed','failed','cancelled'].includes(job.status) && !notifiedJobs.has(job.id)){notifiedJobs.add(job.id);const message=`${job.name || 'Workflow'}: ${job.status}`;toast(message);if(window.Notification?.permission==='granted')new window.Notification('Whitecube workflow',{body:message});}}
    if(!state.jobs.length)empty('job-list','Queued workflows and step statuses appear here.');if(results.every(r=>r.status==='rejected'))empty('job-list',results[0].reason.message);
  }
  async function queueWorkflow(workflow){try{if(workflow.notify && window.Notification?.permission==='default')await window.Notification.requestPermission();await api('/api/workflows/run',{method:'POST',body:JSON.stringify({id:workflow.id})});await refreshJobs();await reconnectSessions();toast(`${workflow.name || workflow.id} queued.`);}catch(error){toast(error.message);}}
  $('workflow-save').onclick=async()=>{let steps;try{steps=JSON.parse($('workflow-steps').value);if(!Array.isArray(steps)||!steps.length)throw new Error('Steps must be a nonempty JSON array.');}catch(error){return toast(error.message);}const definition={id:$('workflow-id').value.trim(),name:$('workflow-name').value.trim(),steps,stop_on_failure:$('workflow-stop').checked,notify:$('workflow-notify').checked};try{await api('/api/workflows',{method:'POST',body:JSON.stringify(definition)});await refreshJobs();toast('Workflow saved. Select Queue to run it.');}catch(error){toast(error.message);}};$('jobs-refresh').onclick=refreshJobs;
  async function refreshSystem() { try { const system=await api('/api/system'); window.WhitecubeWallpaper?.setSystem(system); } catch (_) { /* Animation has conservative defaults when telemetry is unavailable. */ } }
  function updateClock() { const now = new Date(); $('clock').textContent = now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false}); $('clock').title = now.toLocaleDateString([], {weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'}); }
  updateClock(); setInterval(updateClock, 15000); loadCatalog(); setInterval(loadCatalog, 30000);
  refreshHealth(); refreshLabs(); refreshHistory(); refreshSystem();
  setInterval(() => { if (!document.hidden) refreshSystem(); }, 10000);
  setInterval(() => { if (!document.hidden) refreshHistory(); }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { updateClock(); refreshSystem(); } });
  window.WhitecubeDesktop = {toast,api,getSettings:()=>({...settings}),saveLayout,reconnectSessions};
  retainedSessionsReady=restoreDesktop();
  setInterval(()=>{if(!$('labs-panel').hidden)refreshLabs();if(!$('jobs-view').hidden)refreshJobs();},5000);
  setInterval(()=>{if(!state.creating)reconnectSessions();},15000);

})();
