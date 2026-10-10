(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const {Automaton, VolumeAutomaton, RULES, performanceBudget} = window.WhitecubeCA;
  const canvas = $('universe'), ctx = canvas.getContext('2d', {alpha: false});
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let reducedMotion=window.WhitecubeDesktop?.getSettings().reduced_motion ?? reduced.matches;
  let automaton = new Automaton(60, 36), running = !reducedMotion, edited = false;
  automaton.seed();
  let initial = automaton.snapshot(), width = innerWidth, height = innerHeight, lastStep = performance.now(), lastFrame = 0;
  let stepsPerSecond = 5, theta = 35 * Math.PI / 180, slice = 6, dragging = null, paintedCell = '';
  let repeated = 0, seedCount = 0, system = {}, seedLibrary = [];
  const hashes = [], stars = [];
  try {
    const raw = localStorage.getItem('whitecube.seeds.v1') || '[]';
    if (raw.length < 3000000) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) seedLibrary = parsed.slice(0, 12).filter(entry => {
        try { if (typeof entry.name !== 'string' || entry.name.length > 40 || typeof entry.id !== 'string') return false; Automaton.restore(entry.seed); return true; } catch (_) { return false; }
      });
    }
  } catch (_) { /* Private browsing may not offer storage. */ }
  let rng = 72931;
  const random = () => { rng = (Math.imul(rng, 1664525) + 1013904223) >>> 0; return rng / 4294967296; };
  for (let i = 0; i < 150; i++) stars.push({x: random(), y: random(), r: random() < .91 ? .65 : 1.3, phase: random() * 6, a: random() * .4 + .12});
  const notice = message => window.WhitecubeDesktop?.toast(message);
  function resize() {
    width = innerWidth; height = innerHeight; const dpr = Math.min(2, devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  addEventListener('resize', resize); resize();
  function refresh() {
    const volume = automaton.depth > 1;
    $('population').textContent = automaton.population().toLocaleString();
    $('generation-label').textContent = `GEN ${String(automaton.generation).padStart(6, '0')}`;
    $('rule-label').textContent = RULES[automaton.rule].name; $('simulation-rule').textContent = RULES[automaton.rule].label;
    $('life-state').replaceChildren(); const dot = document.createElement('i'); $('life-state').append(dot, document.createTextNode(running ? ' LIVE' : ' PAUSED'));
    $('life-state').style.color = running ? '' : '#8a9bad';
    $('pause-icon').textContent = running ? 'Ⅱ' : '▷'; $('pause-text').textContent = running ? 'Pause' : 'Resume';
    $('volume-tools').hidden = !volume;
    for (const [id, selected] of [['mode-plane', !volume], ['mode-volume', volume]]) { $(id).classList.toggle('selected', selected); $(id).setAttribute('aria-pressed', String(selected)); }
    for (const button of document.querySelectorAll('[data-rule]')) { button.disabled = volume; const selected = button.dataset.rule === automaton.rule; button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected)); }
    $('slice').max = String(automaton.depth - 1); slice = Math.min(slice, automaton.depth - 1); $('slice').value = String(slice); $('slice-value').textContent = String(slice);
    $('cell-x').max=String(automaton.width-1);$('cell-y').max=String(automaton.height-1);
  }
  function resetCycle() { hashes.length = 0; repeated = 0; lastStep = performance.now(); }
  function recordInitial() { initial = automaton.snapshot(); resetCycle(); }
  function reseed(automatic = false) {
    automaton.seed(); edited = false; recordInitial(); seedCount++;
    $('field-note').textContent = automatic ? `New field · cycle ${seedCount + 1}` : 'A real simulation · edges wrap through space'; refresh();
  }
  function advance() {
    automaton.step(); const hash = automaton.hash(); repeated = hashes.includes(hash) ? repeated + 1 : 0;
    hashes.push(hash); if (hashes.length > 24) hashes.shift();
    if (!edited && $('auto-loop').checked && (!automaton.population() || repeated > 22 || automaton.generation >= 900)) reseed(true);
    else refresh();
  }
  $('pause-button').onclick = () => { if(reducedMotion){running=false;refresh();return notice('Reduced motion is enabled. Use Step, or turn it off in Display to resume evolution.');}running = !running; lastStep = performance.now(); refresh(); };
  $('reseed-button').onclick = () => reseed();
  $('step-button').onclick = () => { running = false; advance(); refresh(); };
  $('speed').oninput = event => { stepsPerSecond = Number(event.target.value); $('speed-value').textContent = `${stepsPerSecond} / s`; };
  for (const button of document.querySelectorAll('[data-rule]')) button.onclick = () => { if (automaton.depth > 1) return; automaton.rule = button.dataset.rule; reseed(); };
  function changeDimension(volume) { automaton = volume ? new VolumeAutomaton() : new Automaton(60, 36); slice = volume ? 6 : 0; $('paint-tool').value = 'orbit'; reseed(); }
  $('mode-plane').onclick = () => changeDimension(false); $('mode-volume').onclick = () => changeDimension(true);
  $('slice').oninput = event => { slice = Number(event.target.value); $('slice-value').textContent = String(slice); };
  $('orbit-angle').oninput = event => { theta = Number(event.target.value) * Math.PI / 180; };
  $('paint-tool').onchange = () => { canvas.style.cursor = $('paint-tool').value === 'orbit' ? 'grab' : 'crosshair'; };
  $('clear-field').onclick = () => { automaton.clear(); edited = true; running = false; recordInitial(); refresh(); };
  $('replay-button').onclick = () => { automaton = Automaton.restore(initial); edited = true; resetCycle(); running = !reducedMotion; refresh(); $('field-note').textContent = 'Replaying the captured seed · deterministic evolution'; };
  function renderSeeds() {
    $('saved-seeds').replaceChildren(); const option = document.createElement('option'); option.value = ''; option.textContent = 'Saved seeds…'; $('saved-seeds').append(option);
    for (const entry of seedLibrary) { const item = document.createElement('option'); item.value = entry.id; item.textContent = `${entry.name} · ${entry.seed.depth > 1 ? '3D' : '2D'}`; $('saved-seeds').append(item); }
  }
  function storeSeeds() {
    try { localStorage.setItem('whitecube.seeds.v1', JSON.stringify(seedLibrary)); syncSeeds().catch(()=>notice('Seed saved locally. Service sync is pending; reconnect before backing up.')); return true; }
    catch (_) { notice('Seed storage is full or unavailable. Existing saved seeds are unchanged.'); return false; }
  }
  $('seed-save').onclick = () => {
    const name = $('seed-name').value.trim().slice(0, 40) || `Field ${seedLibrary.length + 1}`;
    if (seedLibrary.length >= 12) return notice('Twelve seeds are saved. Delete one before saving another.');
    const entry = {id: `${Date.now()}-${Math.floor(Math.random() * 1000000)}`, name, seed: automaton.snapshot()};
    seedLibrary.push(entry); if (!storeSeeds()) { seedLibrary.pop(); return; }
    initial = entry.seed; edited = true; renderSeeds(); $('saved-seeds').value = entry.id; notice(`Saved “${name}”. Replay starts from this generation.`);
  };
  $('seed-load').onclick = () => {
    const entry = seedLibrary.find(s => s.id === $('saved-seeds').value); if (!entry) return notice('Choose a saved seed first.');
    try { automaton = Automaton.restore(entry.seed); initial = automaton.snapshot(); running = false; edited = true; resetCycle(); refresh(); $('field-note').textContent = `Loaded ${entry.name} · resume or replay to evolve`; }
    catch (error) { notice(error.message); }
  };
  $('seed-delete').onclick = () => {
    const id = $('saved-seeds').value; if (!id) return;
    const before = seedLibrary; seedLibrary = seedLibrary.filter(s => s.id !== id); if (!storeSeeds()) seedLibrary = before; renderSeeds();
  };
  async function syncSeeds() {return window.WhitecubeDesktop?.api('/api/seeds',{method:'POST',body:JSON.stringify({seeds:seedLibrary})});}
  async function restoreSeeds() {
    try {const result=await window.WhitecubeDesktop?.api('/api/seeds');
      if(result?.exists===true || Array.isArray(result?.seeds) && result.seeds.length){seedLibrary=(Array.isArray(result.seeds)?result.seeds:[]).filter(entry=>{try{if(typeof entry.id!=='string'||typeof entry.name!=='string'||entry.name.length>40)return false;Automaton.restore(entry.seed);return true;}catch(_){return false;}}).slice(0,12);localStorage.setItem('whitecube.seeds.v1',JSON.stringify(seedLibrary));renderSeeds();}
      else if(seedLibrary.length)await syncSeeds();
    }catch(_){/* Local seeds remain available when the workspace service is offline. */}
  }
  function editCell(alive){const x=Number($('cell-x').value),y=Number($('cell-y').value),z=automaton.depth>1?slice:0;if(!Number.isInteger(x)||!Number.isInteger(y)||x<0||x>=automaton.width||y<0||y>=automaton.height)return notice(`Use X 0–${automaton.width-1} and Y 0–${automaton.height-1}.`);automaton.set(x,y,z,alive);running=false;edited=true;recordInitial();refresh();}
  $('cell-plant').onclick=()=>editCell(true);$('cell-erase').onclick=()=>editCell(false);
  function geometry() {
    const volume = automaton.depth > 1, mobile = width < 700;
    const unit = volume ? Math.min(width * .020, height * .036) : mobile ? width * .0115 : Math.min(width * .0086, height * .018);
    return {volume, unit, cx: width * (mobile ? .50 : .59), cy: height * (volume ? .50 : .57)};
  }
  function project(x, y, z = 0) {
    const {volume, unit, cx, cy} = geometry();
    x -= automaton.width / 2; y -= automaton.height / 2;
    if (!volume) return [cx + (x - y) * unit, cy + (x + y) * unit * .49, x + y];
    const rx = x * Math.cos(theta) - y * Math.sin(theta), ry = x * Math.sin(theta) + y * Math.cos(theta);
    return [cx + rx * unit, cy + (ry * .47 - (z - automaton.depth / 2) * .84) * unit, ry + z * .08];
  }
  function unproject(px, py) {
    const {volume, unit, cx, cy} = geometry();
    if (!volume) { const u = (px - cx) / unit, v = (py - cy) / (.49 * unit); return [Math.round((u + v) / 2 + automaton.width / 2), Math.round((v - u) / 2 + automaton.height / 2), 0]; }
    const rx = (px - cx) / unit, ry = ((py - cy) / unit + (slice - automaton.depth / 2) * .84) / .47;
    return [Math.round(rx * Math.cos(theta) + ry * Math.sin(theta) + automaton.width / 2), Math.round(-rx * Math.sin(theta) + ry * Math.cos(theta) + automaton.height / 2), slice];
  }
  function paint(event) {
    const tool = $('paint-tool').value, [x, y, z] = unproject(event.clientX, event.clientY), key = `${x},${y},${z}`;
    if (tool === 'orbit' || paintedCell === key || x < 0 || y < 0 || x >= automaton.width || y >= automaton.height) return;
    paintedCell = key; running = false; edited = true;
    if (tool === 'draw' || tool === 'erase') automaton.set(x, y, z, tool === 'draw'); else automaton.stamp(tool, x, y, z);
    recordInitial(); refresh(); $('field-note').textContent = 'Your edited seed is ready · Resume or Replay to evolve';
  }
  canvas.onpointerdown = event => { if (event.button !== 0) return; dragging = {x: event.clientX, theta}; paintedCell = ''; canvas.setPointerCapture?.(event.pointerId); paint(event); };
  canvas.onpointermove = event => {
    if (!dragging) return;
    if ($('paint-tool').value === 'orbit' && automaton.depth > 1) { theta = dragging.theta + (event.clientX - dragging.x) * .008; $('orbit-angle').value = String(((theta * 180 / Math.PI + 540) % 360) - 180); }
    else if (['draw', 'erase'].includes($('paint-tool').value)) paint(event);
  };
  canvas.onpointerup = canvas.onpointercancel = () => { dragging = null; paintedCell = ''; };
  function polygon(points, fill, stroke) {
    ctx.beginPath(); ctx.moveTo(...points[0]); for (let i = 1; i < points.length; i++) ctx.lineTo(...points[i]); ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); } if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = .5; ctx.stroke(); }
  }
  function cube(x, y, size, tall, alpha = 1, aged = false) {
    const half = size * .5, edge = 'rgba(245,250,255,.14)'; ctx.globalAlpha = alpha;
    polygon([[x-size,y-tall],[x,y-half-tall],[x+size,y-tall],[x,y+half-tall]],aged ? '#c4d5e1' : '#f5f8fa',edge);
    polygon([[x-size,y-tall],[x,y+half-tall],[x,y+half],[x-size,y]],aged ? '#71889d' : '#9aaec1',edge);
    polygon([[x,y+half-tall],[x+size,y-tall],[x+size,y],[x,y+half]],aged ? '#96abbc' : '#d6e1e9',edge); ctx.globalAlpha = 1;
  }
  function draw(time) {
    const t = reducedMotion ? 0 : time / 1000, {volume, unit} = geometry();
    ctx.fillStyle = '#080c13'; ctx.fillRect(0, 0, width, height);
    const glow = ctx.createRadialGradient(width*.61,height*.51,0,width*.61,height*.51,width*.55);
    glow.addColorStop(0,'#1b2b3b'); glow.addColorStop(.43,'#111c29'); glow.addColorStop(1,'#080c13'); ctx.fillStyle=glow; ctx.fillRect(0,0,width,height);
    for (const star of stars) { ctx.globalAlpha=star.a*(running ? .76+.24*Math.sin(t*.28+star.phase):.85); ctx.fillStyle='#c5dbe9'; ctx.fillRect(star.x*width,star.y*height,star.r,star.r); } ctx.globalAlpha=1;
    const layer = volume ? slice : 0, editing = $('paint-tool').value !== 'orbit', gridStep = editing ? 1 : volume ? 4 : 6;
    ctx.strokeStyle = editing ? '#a9f1da25' : '#8faac214'; ctx.lineWidth=.5;
    for(let x=0;x<=automaton.width;x+=gridStep) { const a=project(x,0,layer), b=project(x,automaton.height,layer); ctx.beginPath(); ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); ctx.stroke(); }
    for(let y=0;y<=automaton.height;y+=gridStep) { const a=project(0,y,layer), b=project(automaton.width,y,layer); ctx.beginPath(); ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); ctx.stroke(); }
    const points=[];
    for(let z=0;z<automaton.depth;z++) {
      if(volume && $('slice-only').checked && z!==slice) continue;
      for(let y=0;y<automaton.height;y++) for(let x=0;x<automaton.width;x++) {
        const i=automaton.index(x,y,z); if(!automaton.cells[i]) continue;
        const p=project(x,y,z); points.push({p,age:automaton.ages[i],z});
      }
    }
    points.sort((a,b)=>a.p[2]-b.p[2]);
    for(const {p,age,z} of points) {
      const alpha=volume && editing && z!==slice ? .22 : volume ? .80 : .65;
      cube(p[0],p[1],unit*(volume?.39:.88),unit*(volume?.68:.55+Math.min(age,15)*.055),alpha,age>9);
    }
    if(!volume) for(const [i,s] of [[.71,.245,22,.6],[.89,.39,11,.42],[.455,.32,10,.45],[.39,.73,33,.85],[.78,.76,17,.55]].entries()) {
      const [x,y,size,a]=s, offset=running?Math.sin(t*.19+i*1.9)*7:0, scale=width<700?.6:Math.min(1.25,width/1440); cube(width*x,height*y+offset,size*scale,size*scale*1.2,a);
    }
  }
  function frame(time) {
    requestAnimationFrame(frame);
    const fullScreen = ['terminal-panel','app-panel'].some(id => !$(id).hidden && $(id).classList.contains('maximized'));
    const budget=performanceBudget({hidden:document.hidden,reducedMotion,fullScreen,temperature:system.thermal_throttled?100:system.temperature_c,load:system.load_per_cpu||0});
    $('performance-note').textContent=budget.reason+(typeof system.temperature_c==='number'?` · ${Math.round(system.temperature_c)}°C`:'');
    if(!budget.fps) { lastStep=time; return; }
    const interval=1000/Math.min(stepsPerSecond,budget.steps);
    // Simulation steps are discrete and exact; drawing and system load only alter cadence.
    if(running && !reducedMotion && time-lastStep>=interval) { advance(); lastStep=time; }
    if(time-lastFrame<1000/budget.fps) return; lastFrame=time; draw(time);
  }
  document.addEventListener('visibilitychange',()=>{ lastStep=performance.now(); });
  if(reduced.addEventListener) reduced.addEventListener('change',()=>{ reducedMotion=reduced.matches || window.WhitecubeDesktop?.getSettings().reduced_motion===true;if(reducedMotion)running=false;refresh(); });
  window.WhitecubeWallpaper = {setSystem: value => { system=value||{}; }, snapshot:()=>automaton.snapshot(), syncSeeds, reloadSeeds:restoreSeeds, setReducedMotion:value=>{reducedMotion=value===true || reduced.matches;if(reducedMotion)running=false;refresh();}};
  restoreSeeds();
  renderSeeds(); refresh(); requestAnimationFrame(frame);
})();
