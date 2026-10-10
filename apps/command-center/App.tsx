import React, { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import Header from './components/Header';
import Sidebar from './components/Sidebar';
import ScriptCard from './components/ScriptCard';
import Terminal from './components/Terminal';
import ConfigModal from './components/ConfigModal';
import ConfirmationModal from './components/ConfirmationModal';
import NautilusEditor from './components/NautilusEditor';
import ShortcutGroupsEditor from './components/ShortcutGroupsEditor';
import ShortcutOverlay from './components/ShortcutOverlay';
import Overview from './components/Overview';
import ServicesPanel from './components/ServicesPanel';
import JournalPanel from './components/JournalPanel';
import UpdatesPanel from './components/UpdatesPanel';
import DisplayPanel from './components/DisplayPanel';
import ProcessesPanel from './components/ProcessesPanel';
import NetworkPanel from './components/NetworkPanel';
import StoragePanel from './components/StoragePanel';
import PackagesPanel from './components/PackagesPanel';
import DiagnosticsPanel from './components/DiagnosticsPanel';
import ActivityPanel from './components/ActivityPanel';
import SettingsPanel from './components/SettingsPanel';
import { mergeLibrary, type Library } from './backup';
import CommandPalette, { PaletteItem } from './components/CommandPalette';
import WallpaperCanvas, { prepareWallpaper, WallpaperState } from './components/WallpaperCanvas';
import BootScreen, { BootStage } from './components/BootScreen';
import SysctlPanel from './components/SysctlPanel';
import { LOG_HISTORY_LIMIT } from './constants';
import { createId } from './id';
import { cleanOutput } from './format';
import { isFailure } from './hooks';
import { applySysctlEntries, fetchSysctlBackups, restoreSysctlBackup } from './sysctlClient';
import { loadState, saveState } from './storage';
import { formatTimestamp } from './time';
import type { HostInfo, RunEvent } from './lab.d';
import { ScriptItem, TerminalLog, ScriptStatus, Category, ViewMode, NautilusAction, ShortcutGroup } from './types';

interface ActiveJob { id: string; scriptId?: string; cwd: string; out: string; err: string; fromTerminal: boolean }
type Confirm = { title: string; message: string; command: string; run: () => void };

const PRIVILEGED = /\b(sudo|pkexec)\b/;
const preference = (key: string, fallback = false) => { try { const value = localStorage.getItem(key); return value === null ? fallback : value === '1'; } catch { return fallback; } };
const storePreference = (key: string, value: boolean) => { try { localStorage.setItem(key, value ? '1' : '0'); } catch { /* unavailable storage */ } };

const App: React.FC = () => {
  const initialState = useMemo(() => loadState(), []);

  // Data State
  const [scripts, setScripts] = useState<ScriptItem[]>(() => initialState.scripts);
  const [logs, setLogs] = useState<TerminalLog[]>(() => initialState.logs);
  const [nautilusActions, setNautilusActions] = useState<NautilusAction[]>(() => initialState.nautilusActions);
  const [shortcutGroups, setShortcutGroups] = useState<ShortcutGroup[]>(() => initialState.shortcutGroups);

  // UI State
  const [activeView, setActiveView] = useState<ViewMode>('Overview');
  const [activeCategory, setActiveCategory] = useState<Category | 'All'>('All');
  const [searchQuery, setSearchQuery] = useState('');
  const [host, setHost] = useState<HostInfo | null>(null);
  const [session, setSession] = useState('');
  const [currentDirectory, setCurrentDirectory] = useState('~');
  const [running, setRunning] = useState(false);
  const [sysctlRefresh, setSysctlRefresh] = useState(0);
  const [persistSysctl, setPersistSysctl] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [bootEnabled, setBootEnabled] = useState(() => preference('cc:boot', true));
  const [booting, setBooting] = useState(bootEnabled);
  const [wallpaperEnabled, setWallpaperEnabled] = useState(() => preference('cc:wallpaper'));
  const [wallpaper, setWallpaper] = useState<WallpaperState | null>(null);
  const [webApis, setWebApis] = useState(() => { try { return localStorage.getItem('cc:web-apis') === '1'; } catch { return false; } });

  // Modal State
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingScript, setEditingScript] = useState<ScriptItem | undefined>(undefined);
  const [confirming, setConfirming] = useState<Confirm | null>(null);
  const [activeOverlayGroup, setActiveOverlayGroup] = useState<ShortcutGroup | null>(null);

  const persistTimerRef = useRef<number | null>(null);
  const jobRef = useRef<ActiveJob | null>(null);
  const earlyEvents = useRef(new Map<string, RunEvent[]>());
  const homeRef = useRef('');
  const cwdRef = useRef('~');
  cwdRef.current = currentDirectory;
  const stateRef = useRef({ scripts, logs, nautilusActions, shortcutGroups });
  stateRef.current = { scripts, logs, nautilusActions, shortcutGroups };
  useEffect(() => {
    const flush = () => saveState(stateRef.current);
    window.addEventListener('beforeunload', flush);
    return () => window.removeEventListener('beforeunload', flush);
  }, []);

  useEffect(() => {
    window.lab.host().then((h) => { if (!isFailure(h)) { setHost(h); homeRef.current = h.home; } });
    window.lab.display().then((d) => { if (!isFailure(d)) setSession(d.sessionType); });
  }, []);

  // Key Listener for Shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.ctrlKey && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (e.altKey && !e.ctrlKey && !e.shiftKey) {
        const combo = `ALT+${e.key.toUpperCase()}`;
        const group = shortcutGroups.find((g) => g.shortcut.toUpperCase() === combo);
        if (group) {
          e.preventDefault();
          setActiveOverlayGroup(group);
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [shortcutGroups]);

  useEffect(() => {
    if (persistTimerRef.current !== null) window.clearTimeout(persistTimerRef.current);
    persistTimerRef.current = window.setTimeout(() => {
      saveState({ scripts, logs, nautilusActions, shortcutGroups });
    }, 250);
    return () => {
      if (persistTimerRef.current !== null) window.clearTimeout(persistTimerRef.current);
    };
  }, [scripts, logs, nautilusActions, shortcutGroups]);

  const loadWallpaper = useCallback(async (): Promise<string> => {
    const w = await window.lab.wallpaper();
    if (isFailure(w)) throw new Error(w.error);
    const prepared = await prepareWallpaper(w.dataUrl);
    setWallpaper(prepared);
    return `${w.source} #${w.id}${prepared.zoomed ? ` · watermark ${prepared.side}, canvas 123%` : ''}`;
  }, []);

  // Boot chain: each stage does real work and a failure never blocks startup.
  const bootStages = useMemo<BootStage[]>(() => [
    { id: 'wayland', label: 'Wayland gate', run: async () => {
      const d = await window.lab.display();
      if (isFailure(d)) throw new Error(d.error);
      if (d.sessionType !== 'wayland') throw new Error(`session is ${d.sessionType}`);
      return `${d.waylandDisplay} · DISPLAY ${d.x11Display ? 'set' : 'hidden'}`;
    } },
    { id: 'host', label: 'Host', run: async () => {
      const h = await window.lab.host();
      if (isFailure(h)) throw new Error(h.error);
      return `${h.hostname} · ${h.os}`;
    } },
    { id: 'system', label: 'System', run: async () => {
      const r = await window.lab.snapshot();
      if (isFailure(r)) throw new Error(r.error);
      return `${r.cores} cores · ${(r.mem.used / r.mem.total * 100).toFixed(0)}% memory`;
    } },
    { id: 'network', label: 'Network', run: async () => {
      const n = await window.lab.network();
      if (isFailure(n)) throw new Error(n.error);
      if (!n.gateway) throw new Error('no default route');
      const p = await window.lab.probe([n.gateway.ip]);
      if (isFailure(p)) throw new Error(p.error);
      return p[0].loss >= 100 ? `gateway ${n.gateway.ip} unreachable` : `gateway ${n.gateway.ip} · ${p[0].avg?.toFixed(0)} ms`;
    } },
    { id: 'wallpaper', label: 'Wallpaper (picsum)', run: async () => wallpaperEnabled ? loadWallpaper() : 'skipped: online wallpapers off' },
    { id: 'outside', label: 'Outside (open-meteo)', run: async () => {
      if (!webApis) return 'skipped: off (Overview > enable)';
      const o = await window.lab.outside();
      if (isFailure(o)) throw new Error(o.error);
      return `${o.city}, ${o.country} · ${o.tempC.toFixed(0)} °C`;
    } },
    { id: 'updates', label: 'Updates', run: async () => {
      const u = await window.lab.updates();
      if (isFailure(u)) throw new Error(u.error);
      return u.packages.length ? `${u.packages.length} upgradable` : 'up to date';
    } },
  ], [loadWallpaper, webApis, wallpaperEnabled]);

  const shortDir = useCallback((dir: string) => {
    const home = homeRef.current;
    return home && (dir === home || dir.startsWith(`${home}/`)) ? `~${dir.slice(home.length)}` : dir;
  }, []);

  // Helper to add logs
  const addLogs = useCallback((entries: { content: string; type?: TerminalLog['type']; cwd?: string }[]) => {
    if (!entries.length) return;
    const stamp = formatTimestamp();
    setLogs((prev) => [
      ...prev,
      ...entries.map((e) => ({ id: createId(), timestamp: stamp, content: e.content, type: e.type ?? 'info', cwd: e.cwd ?? cwdRef.current })),
    ].slice(-LOG_HISTORY_LIMIT));
  }, []);

  const addLog = useCallback((content: string, type: TerminalLog['type'] = 'info', cwd?: string) => {
    addLogs([{ content, type, cwd }]);
  }, [addLogs]);

  // --- Real execution -------------------------------------------------------

  const flushLines = (job: ActiveJob, stream: 'out' | 'err', final: boolean) => {
    const lines = cleanOutput(job[stream]).split('\n');
    job[stream] = final ? '' : lines.pop() ?? '';
    if (job[stream].length > 65536) { lines.push(job[stream].slice(0, 65536)); job[stream] = ''; }
    if (final && lines.length && lines[lines.length - 1] === '') lines.pop();
    addLogs(lines.map((content) => ({ content, type: stream === 'err' ? 'error' as const : 'info' as const, cwd: job.cwd })));
  };

  const handleRunEvent = useCallback((ev: RunEvent) => {
    const job = jobRef.current;
    if (!job || job.id !== ev.id) {
      if (!job || job.id) return;
      const list = earlyEvents.current.get(ev.id) ?? [];
      list.push(ev);
      earlyEvents.current.set(ev.id, list);
      return;
    }
    if (ev.kind === 'out' || ev.kind === 'err') {
      job[ev.kind] += ev.text ?? '';
      flushLines(job, ev.kind, false);
      return;
    }
    flushLines(job, 'out', true);
    flushLines(job, 'err', true);
    const ok = ev.code === 0;
    const secs = ((ev.ms ?? 0) / 1000).toFixed(1);
    const how = ev.error ? `could not start: ${ev.error}` : ev.signal ? `stopped (${ev.signal})` : `exit ${ev.code}`;
    const stopped = !!ev.signal && !ev.error;
    addLog(`[${ok ? 'done' : stopped ? 'stopped' : 'failed'}] ${how} in ${secs}s`, ok ? 'success' : stopped ? 'info' : 'error', job.cwd);
    if (job.fromTerminal && ev.cwd) setCurrentDirectory(shortDir(ev.cwd));
    if (job.scriptId) {
      setScripts((prev) => prev.map((s) => s.id === job.scriptId ? { ...s, status: ok ? ScriptStatus.SUCCESS : ScriptStatus.ERROR, lastRun: new Date().toISOString() } : s));
    }
    jobRef.current = null;
    setRunning(false);
  }, [addLog, addLogs, shortDir]);

  useEffect(() => window.lab.onRun(handleRunEvent), [handleRunEvent]);

  const startCommand = async (command: string, cwd: string, opts: { scriptId?: string; fromTerminal: boolean }) => {
    if (jobRef.current) {
      addLog('A command is already running. Stop it first (Ctrl+C).', 'error');
      return;
    }
    // Reserve the slot before awaiting so a second click cannot start another job.
    const job: ActiveJob = { id: '', scriptId: opts.scriptId, cwd, out: '', err: '', fromTerminal: opts.fromTerminal };
    jobRef.current = job;
    setRunning(true);
    addLog(command, 'command', cwd);
    if (opts.scriptId) setScripts((prev) => prev.map((s) => s.id === opts.scriptId ? { ...s, status: ScriptStatus.RUNNING } : s));
    const r = await window.lab.run(command, cwd).catch((e) => ({ error: String(e.message || 'Command failed to start') }));
    if (isFailure(r)) {
      addLog(r.error, 'error', cwd);
      if (opts.scriptId) setScripts((prev) => prev.map((s) => s.id === opts.scriptId ? { ...s, status: ScriptStatus.ERROR } : s));
      jobRef.current = null;
      setRunning(false);
      return;
    }
    job.id = r.id;
    job.cwd = r.cwd;
    const queued = earlyEvents.current.get(r.id) ?? [];
    earlyEvents.current.delete(r.id);
    queued.forEach(handleRunEvent);
  };

  const interrupt = () => { if (jobRef.current?.id) window.lab.kill(jobRef.current.id); };

  const requestRun = (command: string, run: () => void, title = 'Privileged Execution') => {
    if (PRIVILEGED.test(command)) {
      setConfirming({
        title,
        message: 'This command uses elevated privileges (sudo). Ensure you understand the operation before proceeding. There is no password prompt here: it only works if passwordless sudo is configured for it.',
        command,
        run,
      });
    } else {
      run();
    }
  };

  const executeScript = (id: string) => {
    const script = scripts.find((s) => s.id === id);
    if (!script) return;
    startCommand(script.command, script.path || currentDirectory, { scriptId: id, fromTerminal: false });
  };

  const handleRunRequest = (id: string) => {
    const script = scripts.find((s) => s.id === id);
    if (!script) return;
    if (script.requiresConfirmation || PRIVILEGED.test(script.command)) {
      setConfirming({
        title: 'Privileged Execution',
        message: 'This command requires confirmation. It may involve system-level changes or elevated privileges (sudo). Ensure you understand the operation before proceeding.',
        command: script.command,
        run: () => executeScript(id),
      });
    } else {
      executeScript(id);
    }
  };

  const runInTerminal = (command: string) => {
    requestRun(command, () => startCommand(command, currentDirectory, { fromTerminal: true }));
  };

  const handleToggleConfirmation = (id: string, value: boolean) => {
    setScripts((prev) => prev.map((s) => s.id === id ? { ...s, requiresConfirmation: value } : s));
    addLog(value ? `[System] Script ${id} now requires confirmation.` : `[System] Confirmation requirement removed for ${id}.`, 'info');
  };

  const handleTerminalCommand = (cmd: string) => {
    const trimmed = cmd.trim();
    if (!trimmed) return;
    const tokens = trimmed.match(/(?:[^\s"]+|"[^"]*")+/g)?.map((t) => t.replace(/"/g, '')) ?? [];
    const base = tokens[0]?.toLowerCase() ?? '';
    const args = tokens.slice(1);

    if (base === 'clear') { setLogs([]); return; }

    if (base === 'help') {
      addLog(trimmed, 'command');
      addLogs([
        { content: 'Everything else runs in a real bash. Built-ins:' },
        { content: '  run [id|name]  - Execute a saved script by ID or name' },
        { content: '  list           - List all configured scripts' },
        { content: '  clear          - Clear terminal output (Ctrl+L)' },
        { content: 'Ctrl+C stops the running command; Up/Down browse history.' },
      ]);
      return;
    }

    if (base === 'list') {
      addLog(trimmed, 'command');
      if (scripts.length === 0) addLog('No scripts configured. Use the dashboard to add one.');
      else addLogs(scripts.map((s) => ({ content: `[${s.id}] ${s.name} (${s.command})` })));
      return;
    }

    if (base === 'run') {
      const lookup = args.join(' ').toLowerCase();
      const script = scripts.find((s) => s.id.toLowerCase() === lookup || s.name.toLowerCase() === lookup);
      if (!script) {
        addLog(trimmed, 'command');
        addLog(`No script found for "${lookup || 'unknown'}".`, 'error');
        return;
      }
      handleRunRequest(script.id);
      return;
    }

    requestRun(trimmed, () => startCommand(trimmed, currentDirectory, { fromTerminal: true }));
  };

  // --- Kernel settings ---------------------------------------------------------

  const requestSysctlApply = (pending: { key: string; value: string }[]) => {
    const persist = persistSysctl;
    const command = `sysctl -w ${pending.map((p) => `${p.key}=${p.value}`).join(' ')}${persist ? '  # + /etc/sysctl.d/90-command-center.conf' : ''}`;
    setConfirming({
      title: 'Apply Kernel Settings',
      message: persist ? 'These values are applied live and written to /etc/sysctl.d so they survive reboots. The previous live values are backed up.' : 'These values are applied live and are not persisted across reboots. The previous values are backed up so you can restore them.',
      command,
      run: async () => {
        try {
          const out = await applySysctlEntries(pending, persist);
          addLog(`[sysctl] applied ${pending.length} setting(s)${out ? `: ${out}` : ''}`, 'success');
          setSysctlRefresh((n) => n + 1);
        } catch (err) {
          addLog(`[sysctl] ${(err as Error).message}`, 'error');
        }
      },
    });
  };

  const restoreLatestSysctl = async () => {
    try {
      const [latest] = await fetchSysctlBackups();
      if (!latest) { addLog('[sysctl] no backups to restore', 'info'); return; }
      setConfirming({
        title: 'Restore kernel settings',
        message: `Restore ${latest.entries.length} live values from ${new Date(latest.timestamp).toLocaleString()}? Persisted settings in /etc/sysctl.d are unaffected.`,
        command: latest.entries.map(e => `${e.key}=${e.value}`).join('\n'),
        run: async () => { try { await restoreSysctlBackup(latest.timestamp); addLog('[sysctl] backup restored', 'success'); setSysctlRefresh(n => n + 1); } catch (e) { addLog(`[sysctl] ${(e as Error).message}`, 'error'); } },
      });
    } catch (err) {
      addLog(`[sysctl] ${(err as Error).message}`, 'error');
    }
  };

  const paletteItems = useMemo<PaletteItem[]>(() => [
    ...(['Overview', 'Processes', 'Storage', 'Network', 'Services', 'Journal', 'Updates', 'Packages', 'Diagnostics', 'Activity', 'Settings', 'Display', 'Sysctl', 'Nautilus', 'Shortcuts', 'Dashboard'] as ViewMode[]).map((v) => ({
      id: `view-${v}`, label: `Go to ${v === 'Display' ? 'Display Session' : v === 'Sysctl' ? 'Kernel Settings' : v === 'Dashboard' ? 'Commands' : v}`, hint: 'view', icon: 'arrow_forward', run: () => setActiveView(v),
    })),
    ...scripts.map((sc) => ({ id: `run-${sc.id}`, label: `Run ${sc.name}`, hint: sc.command, icon: sc.icon, run: () => handleRunRequest(sc.id) })),
    { id: 'wallpaper', label: 'New wallpaper', hint: 'free API: picsum.photos', icon: 'wallpaper', run: () => { loadWallpaper().then((d) => addLog(`[wallpaper] ${d}`, 'success')).catch((e) => addLog(`[wallpaper] ${e.message}`, 'error')); } },
    { id: 'clear', label: 'Clear terminal', hint: 'Ctrl+L', icon: 'delete_sweep', run: () => setLogs([]) },
  ], [scripts, loadWallpaper]); // eslint-disable-line react-hooks/exhaustive-deps

  // Filter Scripts
  const filteredScripts = useMemo(() => {
    let result = activeCategory === 'All' ? scripts : scripts.filter((s) => s.category === activeCategory);
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter((s) => `${s.name} ${s.description} ${s.command}`.toLowerCase().includes(q));
    }
    return result;
  }, [scripts, activeCategory, searchQuery]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { All: scripts.length };
    scripts.forEach((s) => { c[s.category] = (c[s.category] || 0) + 1; });
    return c;
  }, [scripts]);

  // --- CRUD Handlers ---

  const handleSaveScript = (data: Omit<ScriptItem, 'lastRun' | 'status'>) => {
    const requiresConfirmation = data.requiresConfirmation ?? editingScript?.requiresConfirmation ?? PRIVILEGED.test(data.command);
    if (editingScript) {
      setScripts(prev => prev.map(s => s.id === editingScript.id ? { ...s, ...data, requiresConfirmation } : s));
      addLog(`[Config] Configuration updated for '${data.name}'.`, 'info');
    } else {
      const newScript: ScriptItem = {
        ...data,
        id: createId(),
        lastRun: 'Not run',
        status: ScriptStatus.IDLE,
        requiresConfirmation
      };
      setScripts(prev => [...prev, newScript]);
      addLog(`[Config] New command sequence '${data.name}' created.`, 'success');
    }
  };

  const handleDeleteScript = (id: string) => {
    if (confirm('Are you sure you want to remove this configuration?')) {
      setScripts(prev => prev.filter(s => s.id !== id));
      setShortcutGroups(prev => prev.map(g => ({ ...g, scriptIds: g.scriptIds.filter(sid => sid !== id) })));
      addLog(`[Config] Configuration ${id} removed.`, 'error');
    }
  };

  // Nautilus Actions Handlers
  const handleAddNautilus = (action: Omit<NautilusAction, 'id'>) => {
    setNautilusActions(prev => [...prev, { ...action, id: createId() }]);
    addLog(`[Nautilus] Added context menu action: ${action.name}`, 'success');
  };
  
  const handleEditNautilus = (action: NautilusAction) => {
    setNautilusActions(prev => prev.map(a => a.id === action.id ? action : a));
    addLog(`[Nautilus] Updated context menu action: ${action.name}`, 'info');
  };

  const handleDeleteNautilus = (id: string) => {
    setNautilusActions(prev => prev.filter(a => a.id !== id));
    addLog(`[Nautilus] Removed context menu action.`, 'info');
  };

  // Shortcut Group Handlers
  const handleAddGroup = (group: Omit<ShortcutGroup, 'id'>) => {
    setShortcutGroups(prev => [...prev, { ...group, id: createId() }]);
    addLog(`[Shortcuts] Created new group: ${group.name}`, 'success');
  };

  const handleUpdateGroup = (group: ShortcutGroup) => {
    setShortcutGroups(prev => prev.map(g => g.id === group.id ? group : g));
    addLog(`[Shortcuts] Updated group: ${group.name}`, 'info');
  };

  const handleDeleteGroup = (id: string) => {
    setShortcutGroups(prev => prev.filter(g => g.id !== id));
    addLog(`[Shortcuts] Deleted group.`, 'info');
  };

  // --- Modal Helpers ---

  const openAddModal = () => {
    setEditingScript(undefined);
    setIsModalOpen(true);
  };

  const openEditModal = (script: ScriptItem) => {
    setEditingScript(script);
    setIsModalOpen(true);
  };

  const importLibrary = (library: Library) => {
    const merged = mergeLibrary({ scripts, nautilusActions, shortcutGroups }, library);
    setScripts(merged.scripts); setNautilusActions(merged.nautilusActions); setShortcutGroups(merged.shortcutGroups);
    saveState({ ...merged, logs });
  };
  const toggleWeb = (value: boolean) => { setWebApis(value); storePreference('cc:web-apis', value); };

  return (
    <div className="flex flex-col h-screen overflow-hidden text-white/90 font-sans">
      <WallpaperCanvas wallpaper={wallpaper} />
      {booting && <BootScreen stages={bootStages} onDone={() => setBooting(false)} />}
      <Header host={host} session={session} onPalette={() => setPaletteOpen(true)} />
      
      <div className="flex flex-1 overflow-hidden">
        <Sidebar 
          activeView={activeView}
          activeCategory={activeCategory} 
          onSelectView={setActiveView}
          onSelectCategory={setActiveCategory}
          counts={counts}
        />
        
        <main className="flex-1 flex flex-col min-w-0 bg-[#04070d]/40 relative">
          
          {/* Main Content Area based on View */}
          <div className="flex-1 overflow-y-auto relative">
             
             {activeView === 'Overview' && <Overview host={host} webApis={webApis} onToggleWeb={toggleWeb} onNavigate={setActiveView} />}
             {activeView === 'Storage' && <StoragePanel />}
             {activeView === 'Packages' && <PackagesPanel />}
             {activeView === 'Diagnostics' && <DiagnosticsPanel />}
             {activeView === 'Activity' && <ActivityPanel onRepeat={r => setConfirming({ title: 'Repeat command', message: `Run this command again in ${r.cwd}?`, command: r.command, run: () => startCommand(r.command, r.cwd, { fromTerminal: true }) })} />}
             {activeView === 'Settings' && <SettingsPanel library={{ scripts, nautilusActions, shortcutGroups }} onImport={importLibrary} webApis={webApis} onWeb={toggleWeb} wallpaperEnabled={wallpaperEnabled} onWallpaper={v => { setWallpaperEnabled(v); storePreference('cc:wallpaper', v); if (!v) setWallpaper(null); else loadWallpaper().catch(e => addLog(e.message, 'error')); }} bootEnabled={bootEnabled} onBoot={v => { setBootEnabled(v); storePreference('cc:boot', v); }} />}
             {activeView === 'Processes' && <ProcessesPanel onLog={addLog} />}
             {activeView === 'Network' && <NetworkPanel />}
             {activeView === 'Services' && <ServicesPanel onLog={addLog} />}
             {activeView === 'Journal' && <JournalPanel />}
             {activeView === 'Updates' && <UpdatesPanel onRunCommand={runInTerminal} />}
             {activeView === 'Display' && <DisplayPanel />}
             {activeView === 'Sysctl' && (
               <div className="h-full flex flex-col">
                 <div className="px-4 pt-3 flex items-center justify-between text-xs text-white/50">
                   <label className="flex items-center gap-2"><input type="checkbox" checked={persistSysctl} onChange={(e) => setPersistSysctl(e.target.checked)} /> Persist across reboots (writes /etc/sysctl.d, needs sudo)</label>
                   <button onClick={restoreLatestSysctl} className="px-3 py-1 rounded bg-white/10 hover:bg-white/15 text-white/80">Restore last backup</button>
                 </div>
                 <div className="flex-1 min-h-0"><SysctlPanel onRequestApply={requestSysctlApply} refreshKey={sysctlRefresh} /></div>
               </div>
             )}

             {/* DASHBOARD VIEW */}
             {activeView === 'Dashboard' && (
               <div className="p-6">
                 <div className="max-w-7xl mx-auto">
                    {/* Toolbar */}
                    <div className="flex flex-col md:flex-row items-start md:items-center justify-between mb-8 gap-4">
                      <div>
                        <h2 className="text-2xl font-light text-white mb-1">
                          {activeCategory === 'All' ? 'Command library' : activeCategory}
                        </h2>
                        <p className="text-white/50 text-sm">
                          {filteredScripts.length} active configurations available
                        </p>
                      </div>
                      
                      <div className="flex items-center gap-4 w-full md:w-auto">
                        <div className="relative group flex-1 md:w-64">
                          <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-white/30 group-focus-within:text-whitecube-accent transition-colors">search</span>
                          <input 
                            type="text"
                            placeholder="Search commands..."
                            className="w-full bg-black/20 border border-white/10 rounded-full pl-10 pr-4 py-2 text-sm text-white focus:ring-1 focus:ring-whitecube-accent focus:border-whitecube-accent outline-none transition-all"
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                          />
                        </div>
                        <button 
                          onClick={openAddModal}
                          className="flex items-center gap-2 px-4 py-2 bg-whitecube-accent hover:bg-red-600 rounded shadow-md transition-all text-sm font-bold text-white whitespace-nowrap"
                        >
                          <span className="material-symbols-outlined text-[20px]">add</span>
                          <span className="hidden sm:inline">New Command</span>
                        </button>
                      </div>
                    </div>

                    {filteredScripts.length === 0 ? (
                      <div className="flex flex-col items-center justify-center h-64 border-2 border-dashed border-white/10 rounded-xl text-white/30">
                        <span className="material-symbols-outlined text-4xl mb-2">search_off</span>
                        <p>No matching commands found.</p>
                      </div>
                    ) : (
                      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4 pb-10">
                        {filteredScripts.map(script => (
                          <div key={script.id} className="h-48">
                            <ScriptCard 
                              script={script} 
                              onRun={handleRunRequest}
                              onEdit={openEditModal}
                              onDelete={handleDeleteScript}
                              onToggleConfirmation={handleToggleConfirmation}
                            />
                          </div>
                        ))}
                      </div>
                    )}
                 </div>
               </div>
             )}

             {/* NAUTILUS EDITOR VIEW */}
             {activeView === 'Nautilus' && (
                <div className="h-full">
                  <NautilusEditor 
                    actions={nautilusActions}
                    onAdd={handleAddNautilus}
                    onEdit={handleEditNautilus}
                    onDelete={handleDeleteNautilus}
                  />
                </div>
             )}

             {/* SHORTCUTS EDITOR VIEW */}
             {activeView === 'Shortcuts' && (
               <div className="h-full">
                 <ShortcutGroupsEditor 
                   groups={shortcutGroups}
                   availableScripts={scripts}
                   onAddGroup={handleAddGroup}
                   onUpdateGroup={handleUpdateGroup}
                   onDeleteGroup={handleDeleteGroup}
                 />
               </div>
             )}

          </div>

          {/* Terminal at bottom (always visible) */}
          <Terminal
            logs={logs}
            onCommand={handleTerminalCommand}
            onInterrupt={interrupt}
            onClear={() => setLogs([])}
            cwd={currentDirectory}
            prompt={host ? `${host.user}@${host.hostname}` : 'user@host'}
            running={running}
          />
        </main>
      </div>

      {/* Script Config Modal */}
      <ConfigModal 
        isOpen={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        onSave={handleSaveScript}
        initialData={editingScript}
      />

      {/* Execution Confirmation Modal */}
      <ConfirmationModal
        isOpen={!!confirming}
        title={confirming?.title ?? ''}
        message={confirming?.message ?? ''}
        command={confirming?.command ?? ''}
        onCancel={() => setConfirming(null)}
        onConfirm={() => { const c = confirming; setConfirming(null); c?.run(); }}
      />

      <CommandPalette open={paletteOpen} items={paletteItems} onClose={() => setPaletteOpen(false)} />

      {/* Shortcut HUD Overlay */}
      {activeOverlayGroup && (
        <ShortcutOverlay 
          group={activeOverlayGroup}
          scripts={scripts}
          onClose={() => setActiveOverlayGroup(null)}
          onExecute={handleRunRequest}
        />
      )}
    </div>
  );
};

export default App;
