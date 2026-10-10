import React, { useState } from 'react';
import { decodeBackup, encodeBackup, type Library } from '../backup';
import { isFailure, usePoll } from '../hooks';
import { Button, Notice, PanelHeading } from './Panel';
interface Props { library: Library; onImport: (library: Library) => void; webApis: boolean; onWeb: (v: boolean) => void; wallpaperEnabled: boolean; onWallpaper: (v: boolean) => void; bootEnabled: boolean; onBoot: (v: boolean) => void }
export default function SettingsPanel(p: Props) {
  const { data } = usePoll(() => window.lab.appInfo(), null);
  const [incoming, setIncoming] = useState<Library | null>(null), [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  const info = data && !isFailure(data) ? data : null;
  const exportBackup = async () => {
    setBusy(true); setMessage('');
    try { const r = await window.lab.saveFile('whitecube-command-center-backup.json', encodeBackup(p.library)); setMessage(isFailure(r) ? r.error : r.path ? `Backup saved to ${r.path}` : ''); }
    catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  };
  const importBackup = async () => {
    setBusy(true); setMessage(''); setIncoming(null);
    try { const r = await window.lab.importFile(); if (isFailure(r)) throw new Error(r.error); if (r.content) setIncoming(decodeBackup(r.content)); }
    catch (e) { setMessage(`Import failed: ${(e as Error).message}`); } finally { setBusy(false); }
  };
  return <div className="p-6 max-w-5xl mx-auto"><PanelHeading title="Settings & backups" description="Keep your command library portable and choose how the desktop app starts." />
    {message && <Notice>{message}</Notice>}
    <section className="glass-panel rounded-xl p-5 mb-4"><h3 className="font-medium mb-2">Command library</h3><p className="text-sm text-white/50 mb-4">{p.library.scripts.length} commands · {p.library.shortcutGroups.length} shortcut groups · {p.library.nautilusActions.length} file manager actions. Backups contain commands and paths; keep them private.</p><div className="flex gap-2"><Button disabled={busy} onClick={exportBackup}>Export backup</Button><Button disabled={busy} onClick={importBackup}>Import backup…</Button></div>
      {incoming && <div className="mt-4 p-4 border border-whitecube-accent/30 rounded-lg bg-whitecube-accent/5"><h4 className="text-sm font-medium mb-2">Review import</h4><p className="text-sm text-white/60">Add {incoming.scripts.length} commands, {incoming.shortcutGroups.length} groups, and {incoming.nautilusActions.length} actions. Existing items are preserved. Every imported command requires confirmation before execution.</p><ul className="text-xs text-white/50 max-h-40 overflow-auto my-3 space-y-1">{incoming.scripts.map(s => <li key={s.id} className="break-all"><strong>{s.name}</strong> · <code>{s.command}</code></li>)}</ul><div className="flex gap-2"><Button onClick={() => { p.onImport(incoming); setIncoming(null); setMessage('Backup merged into your library.'); }}>Merge into library</Button><Button onClick={() => setIncoming(null)}>Cancel</Button></div></div>}
    </section>
    <section className="glass-panel rounded-xl p-5 mb-4"><h3 className="font-medium mb-4">Startup & online features</h3><div className="space-y-5">{[{ label: 'Show startup checks', description: 'Run the boot sequence when opening the app. Escape skips it.', value: p.bootEnabled, change: p.onBoot }, { label: 'Online wallpapers', description: 'Load a photo from picsum.photos on startup. Off uses the local canvas background.', value: p.wallpaperEnabled, change: p.onWallpaper }, { label: 'Local weather', description: 'Use IP-based location from ipwho.is and weather from open-meteo.com.', value: p.webApis, change: p.onWeb }].map(item => <label key={item.label} className="flex items-start gap-3 cursor-pointer"><input className="mt-1 accent-red-500" type="checkbox" checked={item.value} onChange={e => item.change(e.target.checked)} /><span><span className="block text-sm">{item.label}</span><span className="text-xs text-white/40">{item.description}</span></span></label>)}</div></section>
    <section className="glass-panel rounded-xl p-5"><h3 className="font-medium mb-2">Whitecube Command Center {info ? `v${info.version}` : ''}</h3><p className="text-sm text-white/50">Wayland desktop tools for Ubuntu and Debian · {info ? `Electron ${info.electron}` : 'Loading runtime…'}</p><p className="font-mono text-xs text-white/40 my-3 break-all">{info?.dataDir}</p><Button onClick={async () => { const r = await window.lab.openData(); if (isFailure(r)) setMessage(r.error); }}>Open application data</Button><p className="text-xs text-white/40 mt-4">Ctrl+K: command palette · Alt+C: Core Actions · Ctrl+C: stop a terminal command · Ctrl+L: clear terminal</p></section>
  </div>;
}
