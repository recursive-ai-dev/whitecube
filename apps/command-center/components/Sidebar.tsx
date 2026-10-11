import React from 'react';
import { Category, ViewMode } from '../types';

interface SidebarProps {
  activeView: ViewMode;
  activeCategory: Category | 'All';
  onSelectView: (view: ViewMode) => void;
  onSelectCategory: (cat: Category | 'All') => void;
  counts: Record<string, number>;
}

const Sidebar: React.FC<SidebarProps> = ({
  activeView,
  activeCategory,
  onSelectView,
  onSelectCategory,
  counts
}) => {
  const categories: { id: Category | 'All'; icon: string; label: string }[] = [
    { id: 'All', icon: 'dashboard', label: 'All Commands' },
    { id: 'System', icon: 'settings_system_daydream', label: 'System' },
    { id: 'Files', icon: 'folder', label: 'Files' },
    { id: 'Network', icon: 'lan', label: 'Network' },
    { id: 'Development', icon: 'code', label: 'Development' },
    { id: 'Custom', icon: 'tune', label: 'Custom' },
  ];

  const monitor = [
    { id: 'Overview', icon: 'monitor_heart', label: 'Overview' },
    { id: 'Processes', icon: 'memory', label: 'Processes' },
    { id: 'Storage', icon: 'storage', label: 'Storage' },
    { id: 'Network', icon: 'lan', label: 'Network' },
    { id: 'Services', icon: 'settings_system_daydream', label: 'Services' },
    { id: 'Journal', icon: 'article', label: 'Journal' },
    { id: 'Updates', icon: 'system_update', label: 'Updates' },
    { id: 'Packages', icon: 'inventory_2', label: 'Installed Packages' },
    { id: 'Diagnostics', icon: 'health_and_safety', label: 'Diagnostics' },
    { id: 'Display', icon: 'desktop_windows', label: 'Display Session' },
  ];

  const tools = [
    { id: 'Activity', icon: 'history', label: 'Command Activity' },
    { id: 'Settings', icon: 'settings', label: 'Settings & Backups' },
    { id: 'Sysctl', icon: 'tune', label: 'Kernel Settings' },
    { id: 'Nautilus', icon: 'folder_special', label: 'File Manager Menus' },
    { id: 'Shortcuts', icon: 'keyboard_command_key', label: 'Shortcut Groups' },
  ];

  const navButton = (id: string, icon: string, label: string) => (
    <button
      key={id}
      aria-current={activeView === id ? 'page' : undefined}
      title={label}
      onClick={() => onSelectView(id as ViewMode)}
      className={`w-full flex items-center px-3 py-2 rounded-lg text-[13px] transition-all duration-200 group ${
        activeView === id ? 'bg-whitecube-accent/12 text-red-200 ring-1 ring-inset ring-whitecube-accent/20' : 'text-white/70 hover:bg-white/5 hover:text-white'
      }`}
    >
      <div className="flex items-center gap-3">
        <span className={`material-symbols-outlined text-[20px] ${activeView === id ? 'text-white' : 'text-white/50 group-hover:text-white'}`}>{icon}</span>
        <span className="font-medium">{label}</span>
      </div>
    </button>
  );

  return (
    <aside className="wc-sidebar w-64 bg-[#090c12]/90 border-r border-white/5 flex flex-col shrink-0">
      <div className="p-4 flex-1 overflow-y-auto">

        <div className="text-[10px] font-semibold text-white/40 uppercase tracking-[.16em] mb-3 px-3">Monitor</div>
        <nav aria-label="Monitoring" className="space-y-0.5 mb-6">{monitor.map((m) => navButton(m.id, m.icon, m.label))}</nav>

        {/* Library Section */}
        <div className="text-[10px] font-semibold text-white/40 uppercase tracking-[.16em] mb-3 px-3">Library</div>
        <nav aria-label="Command library" className="space-y-0.5 mb-6">
          {categories.map((cat) => (
            <button
              key={cat.id}
              title={cat.label}
              aria-current={activeView === 'Dashboard' && activeCategory === cat.id ? 'page' : undefined}
              onClick={() => {
                onSelectView('Dashboard');
                onSelectCategory(cat.id);
              }}
              className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-[13px] transition-all duration-200 group ${
                activeView === 'Dashboard' && activeCategory === cat.id
                  ? 'bg-whitecube-accent/12 text-red-200 ring-1 ring-inset ring-whitecube-accent/20'
                  : 'text-white/70 hover:bg-white/5 hover:text-white'
              }`}
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className={`material-symbols-outlined text-[20px] ${activeView === 'Dashboard' && activeCategory === cat.id ? 'text-white' : 'text-white/50 group-hover:text-white'}`}>
                  {cat.icon}
                </span>
                <span className="font-medium">{cat.label}</span>
              </div>
              {counts[cat.id] > 0 && (
                <span className={`text-[10px] px-2 py-0.5 rounded-full ${
                  activeView === 'Dashboard' && activeCategory === cat.id ? 'bg-black/20 text-white' : 'bg-white/10 text-white/50'
                }`}>
                  {counts[cat.id]}
                </span>
              )}
            </button>
          ))}
        </nav>

        {/* Tools Section */}
        <div className="text-[10px] font-semibold text-white/40 uppercase tracking-[.16em] mb-3 px-3">Configuration</div>
        <nav aria-label="Configuration" className="space-y-1">{tools.map((t) => navButton(t.id, t.icon, t.label))}</nav>
      </div>

      <div className="mt-auto p-4 border-t border-white/5">
        <div className="bg-white/3 p-3 rounded-lg border border-white/10">
          <h4 className="text-white text-sm font-bold mb-1">Make it quick</h4>
          <p className="text-white/60 text-xs leading-relaxed">
            Press <span className="text-whitecube-accent font-bold">Ctrl+Shift+G</span> to open the Core Actions group.
          </p>
        </div>
      </div>
    </aside>
  );
};

export default Sidebar;
