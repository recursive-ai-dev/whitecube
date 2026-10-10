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
      onClick={() => onSelectView(id as ViewMode)}
      className={`w-full flex items-center px-3 py-2 rounded-md text-sm transition-all duration-200 group ${
        activeView === id ? 'bg-whitecube-accent text-white shadow-md' : 'text-white/70 hover:bg-white/5 hover:text-white'
      }`}
    >
      <div className="flex items-center gap-3">
        <span className={`material-symbols-outlined text-[20px] ${activeView === id ? 'text-white' : 'text-white/50 group-hover:text-white'}`}>{icon}</span>
        <span className="font-medium">{label}</span>
      </div>
    </button>
  );

  return (
    <aside className="w-64 bg-[#090c12]/80 border-r border-white/5 flex flex-col shrink-0">
      <div className="p-4 flex-1 overflow-y-auto">

        <div className="text-xs font-bold text-white/40 uppercase tracking-wider mb-4 px-2">Monitor</div>
        <nav className="space-y-1 mb-8">{monitor.map((m) => navButton(m.id, m.icon, m.label))}</nav>

        {/* Library Section */}
        <div className="text-xs font-bold text-white/40 uppercase tracking-wider mb-4 px-2">Library</div>
        <nav className="space-y-1 mb-8">
          {categories.map((cat) => (
            <button
              key={cat.id}
              onClick={() => {
                onSelectView('Dashboard');
                onSelectCategory(cat.id);
              }}
              className={`w-full flex items-center justify-between px-3 py-2 rounded-md text-sm transition-all duration-200 group ${
                activeView === 'Dashboard' && activeCategory === cat.id
                  ? 'bg-whitecube-accent text-white shadow-md'
                  : 'text-white/70 hover:bg-white/5 hover:text-white'
              }`}
            >
              <div className="flex items-center gap-3">
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
        <div className="text-xs font-bold text-white/40 uppercase tracking-wider mb-4 px-2">Configuration</div>
        <nav className="space-y-1">{tools.map((t) => navButton(t.id, t.icon, t.label))}</nav>
      </div>

      <div className="mt-auto p-4 border-t border-white/5">
        <div className="bg-gradient-to-br from-[#0b1018] to-black p-4 rounded-lg border border-white/10">
          <h4 className="text-white text-sm font-bold mb-1">Shortcut Tip</h4>
          <p className="text-white/60 text-xs leading-relaxed">
            Press <span className="text-whitecube-accent font-bold">Alt+C</span> to open the Core Actions group.
          </p>
        </div>
      </div>
    </aside>
  );
};

export default Sidebar;
