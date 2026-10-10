"""Private bounded UI preferences. Restoring these files never executes commands."""
import json
import math
import os
from pathlib import Path
from workspace_data import atomic_json

class DesktopStore:
    def __init__(self, state):
        self.state = Path(state)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state.is_symlink() or self.state.stat().st_uid != os.getuid():
            raise ValueError('Unsafe desktop state directory')
    def _read(self, name, default):
        path = self.state/name
        if not path.exists():
            return default
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_size > 1048576:
            raise ValueError('Unsafe or oversized desktop state')
        return json.loads(path.read_text())
    def _save(self, name, value):
        if len(json.dumps(value).encode()) > 1048576:
            raise ValueError('Desktop state exceeds 1 MiB')
        path = self.state/name
        if path.is_symlink():
            raise ValueError('Refusing symlink state')
        atomic_json(path, value)
        return value
    def get_settings(self):
        return self._read('accessibility.json', {'ui_scale':1, 'terminal_scale':1, 'contrast':'normal', 'reduced_motion':False})
    def save_settings(self, data):
        if not isinstance(data, dict):
            raise ValueError('Settings must be an object')
        result = {}
        for key, low, high in [('ui_scale', .8, 1.6), ('terminal_scale', .8, 2)]:
            value = data.get(key, 1)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Invalid '+key)
            result[key] = value
        result['contrast'] = data.get('contrast', 'strong' if data.get('high_contrast') else 'normal')
        if result['contrast'] not in ('normal', 'strong') or not isinstance(data.get('reduced_motion', False), bool):
            raise ValueError('Invalid accessibility preference')
        result['reduced_motion'] = data.get('reduced_motion', False)
        return self._save('accessibility.json', result)
    def get_layout(self):
        return self._read('desktop-layout.json', {})
    def save_layout(self, data):
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('workspaces'), list) or len(data['workspaces']) != 4:
            raise ValueError('Layout requires version 1 and four workspaces')
        # Accept UI fields only. No commands, credentials, or tokens are restorable.
        result = {'version':1, 'workspace':data.get('workspace', 0), 'workspaces':[]}
        if type(result['workspace']) is not int or not 0 <= result['workspace'] < 4:
            raise ValueError('Invalid workspace selection')
        for item in data['workspaces']:
            if not isinstance(item, dict):
                raise ValueError('Invalid workspace layout')
            clean = {k:v for k,v in item.items() if k in ('name','active','focusedPane','maximizedPane','terminalVisible','terminalMaximized','appVisible','appMaximized','project','panes','axis','sizes','terminalBounds','appBounds')}
            if not isinstance(clean.get('name', ''), str) or len(clean.get('name', '')) > 120:
                raise ValueError('Invalid workspace name')
            panes = clean.get('panes', [])
            if not isinstance(panes, list) or len(panes) > 8 or any(not isinstance(p, dict) or set(p)-{'id','session'} for p in panes):
                raise ValueError('Invalid panes')
            project = clean.get('project')
            if project is not None:
                if not isinstance(project, dict):
                    raise ValueError('Invalid selected project')
                clean['project'] = {k:v for k,v in project.items() if k in ('id','name','url')}
            result['workspaces'].append(clean)
        return self._save('desktop-layout.json', result)
    def get_seeds(self):
        return self._read('ca-seeds.json', [])
    def save_seeds(self, data):
        if not isinstance(data, (list, dict)):
            raise ValueError('Seeds must be a JSON array or object')
        return self._save('ca-seeds.json', data)
