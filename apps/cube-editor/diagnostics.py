"""Read-only readiness checks and portable release evidence. Apache-2.0.

Importing this module never imports GTK or starts a process. Reports rehash the
selected release, and distinguish recorded provenance from present evidence.
"""
from __future__ import annotations

import copy
import fcntl
import html
import importlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import tempfile

import engine


BOOT_CHECKS = ('BIOS live', 'UEFI live', 'BIOS installer', 'UEFI installer')


def _gtk_runtime():
    """Inspect loaded library versions without initializing a display or GTK.

    Delay imports until the explicit diagnostics action, keeping headless CLI
    imports independent of GI. Merely finding the gi package does not establish
    that the GTK/Adwaita typelibs and their native libraries are available.
    """
    gi = importlib.import_module('gi')
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    gtk = importlib.import_module('gi.repository.Gtk')
    adwaita = importlib.import_module('gi.repository.Adw')
    versions = {'gtk': [gtk.get_major_version(), gtk.get_minor_version(), gtk.get_micro_version()],
                'adwaita': [adwaita.get_major_version(), adwaita.get_minor_version(), adwaita.get_micro_version()]}
    if tuple(versions['gtk'][:1]) < (4,):
        raise ValueError('GTK 4 or newer is required')
    if tuple(versions['adwaita'][:2]) < (1, 5):
        version = '.'.join(map(str, versions['adwaita']))
        raise ValueError(f'libadwaita 1.5 or newer is required (found {version})')
    return versions


def _active_operation(directory):
    """Inspect an existing lock without creating files or refreshing a project."""
    try:
        with (directory / '.operation.lock').open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def diagnose(project=None):
    """Return JSON-ready readiness checks without changing project metadata.

    Missing optional VM facilities and an unextracted project produce warnings;
    missing workflow tools, unreadable inputs and insufficient space are failures.
    Storage estimates inspect extracted trees and can take time on large images.
    """
    checks = []

    def add(key, label, status, detail):
        checks.append({'id': key, 'label': label, 'status': status, 'detail': str(detail)})

    for tool in engine.dependencies():
        status = 'pass' if tool['available'] else ('fail' if tool['required'] else 'warning')
        add('tool-' + tool['name'], tool['name'], status,
            tool['purpose'] + (' — available' if tool['available'] else ' — not found; install the corresponding distribution package'))
    for module, label, package in (('gi', 'Python GTK bindings', 'python3-gi'), ('yaml', 'Installer configuration support', 'python3-yaml')):
        try:
            available = importlib.util.find_spec(module) is not None
        except (ValueError, ImportError):
            available = False
        add('python-' + module, label, 'pass' if available else 'fail',
            f'{package}: ' + ('available' if available else 'not available in this Python interpreter'))
    versions = {}
    try:
        versions = _gtk_runtime()
        add('gtk-runtime', 'GTK and libadwaita runtime', 'pass',
            'GTK ' + '.'.join(map(str, versions['gtk'])) + '; libadwaita ' + '.'.join(map(str, versions['adwaita'])))
    except (ImportError, ValueError, OSError, AttributeError) as error:
        add('gtk-runtime', 'GTK and libadwaita runtime', 'fail',
            'Install GTK 4 and libadwaita 1.5 or newer with their GI typelibs: ' + str(error))
    graphical = bool(os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY') or os.environ.get('BROADWAY_DISPLAY'))
    add('display', 'Graphical session', 'pass' if graphical else 'warning',
        'Graphical display configured' if graphical else 'No display configured; the CLI can inspect projects, but desktop operations need a graphical session')
    kvm = os.access('/dev/kvm', os.R_OK | os.W_OK)
    add('kvm', 'VM acceleration', 'pass' if kvm else 'warning',
        'KVM is accessible' if kvm else 'KVM is unavailable to this account; QEMU can use slower software emulation')
    firmware = next((path for path in (Path('/usr/share/OVMF/OVMF_CODE_4M.fd'), Path('/usr/share/OVMF/OVMF_CODE.fd'))
                     if path.is_file() and path.with_name(path.name.replace('CODE', 'VARS')).is_file()), None)
    add('uefi', 'UEFI test firmware', 'pass' if firmware else 'warning',
        str(firmware) if firmware else 'Install ovmf for UEFI boot tests; BIOS tests do not require it')
    if project is not None:
        data = copy.deepcopy(project.data)
        try:
            active = _active_operation(project.directory)
            add('operation', 'Project availability', 'warning' if active else 'pass',
                'Another operation is using this project; this report is a snapshot' if active else 'No operation holds the project lock')
        except OSError as error:
            add('operation', 'Project availability', 'fail', error)
            active = True
        source = Path(data['source_iso']).expanduser()
        add('source', 'Source image', 'pass' if source.is_file() else 'fail',
            str(source) if source.is_file() else f'Source is missing: {source}. Recovery and editing may still be available.')
        extracted = (project.directory / '.extracted').is_file() and project.rootfs.is_dir() and project.iso_tree.is_dir()
        add('extracted', 'Editable filesystem', 'pass' if extracted else 'warning',
            'Root filesystem and ISO tree are available' if extracted else 'Extract the source image before applying recipes or building')
        interrupted = data.get('state') in ('extracting', 'customizing', 'terminal', 'building', 'snapshotting', 'restoring', 'error', 'cancelled')
        pending = any((project.directory / name).exists() or (project.directory / name).is_symlink()
                      for name in ('.whiteouts.json', '.guest-environment.json', '.backup-etc-resolv.conf', '.backup-usr-sbin-policy-rc.d'))
        add('recovery', 'Recovery state', 'warning' if pending or (interrupted and not active) else 'pass',
            'Review Recover project before the next build: ' + (data.get('last_error') or data.get('state', 'unknown'))
            if pending or (interrupted and not active) else 'No interrupted operation or guest environment journal detected')
        restore = (project.directory / 'restore-point/manifest.json').is_file()
        add('restore', 'Restore point', 'pass' if restore else 'warning',
            'A saved restore point is available' if restore else 'No restore point saved yet')
        try:
            missing = engine.validate_recipe(data.get('recipe', {}))
            add('recipe', 'Recipe inputs', 'warning' if missing else 'pass',
                'Update missing local files: ' + ', '.join(missing) if missing else 'Recipe structure and local inputs are available')
        except (engine.CubeError, OSError, ValueError) as error:
            add('recipe', 'Recipe inputs', 'fail', error)
        action = 'build' if extracted else 'extract'
        try:
            storage = engine.storage_preflight(project, action)
            add('storage', 'Storage estimate', 'pass' if storage['sufficient'] else 'fail',
                json.dumps(storage, ensure_ascii=False))
        except (OSError, ValueError, engine.CubeError) as error:
            add('storage', 'Storage estimate', 'fail', error)
    return {'format': 'cube-editor-diagnostics', 'version': 1, 'generated_at': engine.utcnow(),
            'ready': not any(check['status'] == 'fail' for check in checks), 'checks': checks,
            'system': {'os': platform.platform(), 'architecture': platform.machine(),
                       'python': platform.python_version(), 'session': 'wayland' if os.environ.get('WAYLAND_DISPLAY') else
                       ('x11' if os.environ.get('DISPLAY') else ('broadway' if os.environ.get('BROADWAY_DISPLAY') else 'none')),
                       'libraries': versions},
            'project': str(project.directory) if project is not None else None}


def _release_record(project, release):
    history = list(project.data.get('releases', [])) + [project.data.get('last_build', {})]
    if isinstance(release, dict):
        if not release.get('path'):
            raise engine.CubeError('The selected release has no ISO path')
        return Path(release['path']).expanduser(), copy.deepcopy(release)
    selected = Path(release).expanduser() if release else Path(project.data.get('last_build', {}).get('path') or project.output_path)
    for record in reversed(history):
        if record.get('path') and Path(record['path']).expanduser().resolve() == selected.resolve():
            return selected, copy.deepcopy(record)
    return selected, {}


def release_report(project, release=None):
    """Hash the ISO and collect evidence without saving checks to the project."""
    selected, record = _release_record(project, release)
    if not selected.is_file():
        raise engine.CubeError(f'The selected release ISO is missing: {selected}')
    try:
        before = selected.stat()
        actual = engine.sha256_file(selected)
        after = selected.stat()
    except OSError as error:
        raise engine.CubeError(f'Cannot read release ISO: {error}') from error
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if identity(before) != identity(after):
        raise engine.CubeError('Release ISO changed while preparing the report; try again')
    sidecar = Path(str(selected) + '.sha256')
    expected = ''
    sidecar_status, sidecar_detail = 'missing', 'SHA256 sidecar is missing'
    if sidecar.exists():
        try:
            fields = sidecar.read_text(encoding='utf-8').split()
            if not fields or not re.fullmatch(r'[a-fA-F0-9]{64}', fields[0]):
                raise ValueError('SHA256 sidecar is empty or malformed')
            expected = fields[0].lower()
            sidecar_status = 'passed' if expected == actual else 'failed'
            sidecar_detail = 'ISO matches its SHA256 sidecar' if sidecar_status == 'passed' else 'ISO does not match its SHA256 sidecar'
        except (OSError, ValueError) as error:
            sidecar_status, sidecar_detail = 'failed', str(error)
    recorded = record.get('sha256', '').lower()
    recorded_status = ('passed' if actual == recorded else 'failed') if recorded else 'missing'
    checks = []
    latest = {}
    for check in project.data.get('release_checks', []):
        item = copy.deepcopy(check)
        item['applies_to_current_contents'] = item.get('sha256', '').lower() == actual
        checks.append(item)
        if item['applies_to_current_contents']:
            latest[item['kind']] = item
    source = {'build_source_iso': record.get('source_iso', ''), 'build_source_sha256': record.get('source_sha256', ''),
              'project_source_iso': project.data['source_iso'], 'project_recorded_sha256': project.data.get('source_sha256', ''),
              'project_expected_sha256': project.data.get('source_expected_sha256', ''),
              'project_verified_at': project.data.get('source_verified_at', ''),
              'note': 'Source hashes are recorded provenance. This report rehashes the release ISO only; it does not rehash or authenticate the source.'}
    return {'format': 'cube-editor-release-report', 'version': 1, 'generated_at': engine.utcnow(),
            'project': {key: project.data.get(key, '') for key in ('id', 'name')},
            'release': record,
            'verification': {'path': str(selected.resolve()), 'size_bytes': after.st_size, 'actual_sha256': actual,
                             'sidecar_sha256': expected, 'sidecar_status': sidecar_status, 'sidecar_detail': sidecar_detail,
                             'recorded_sha256': recorded, 'recorded_status': recorded_status,
                             'success': sidecar_status == 'passed' and recorded_status != 'failed'},
            'source': source, 'checks': checks, 'latest_checks': latest,
            'boot_summary': {kind: latest.get(kind, {}).get('outcome', 'not tested') for kind in BOOT_CHECKS}}


def _report_html(report):
    escape = lambda value: html.escape(str(value), quote=True)
    verification = report['verification']
    rows = lambda pairs: '<table>' + ''.join('<tr><th>' + escape(key) + '</th><td>' + escape(value) + '</td></tr>' for key, value in pairs) + '</table>'
    status = 'Checksum verified' if verification['success'] else 'Verification needs attention'
    boot_rows = [(kind, outcome + ' (recorded manually)' if outcome != 'not tested' else outcome)
                 for kind, outcome in report['boot_summary'].items()]
    history = ''.join('<tr>' + ''.join('<td>' + escape(value) + '</td>' for value in
                      (check.get('kind', ''), check.get('outcome', ''), check.get('checked_at', ''),
                       'Current ISO contents' if check['applies_to_current_contents'] else 'Historical — different ISO contents',
                       check.get('note', ''))) + '</tr>' for check in report['checks'])
    provenance = escape(json.dumps(report['release'], indent=2, ensure_ascii=False))
    source = escape(json.dumps(report['source'], indent=2, ensure_ascii=False))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Cube Editor release report — {escape(report['project']['name'])}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:1080px;margin:48px auto;padding:0 24px;background:#f5f6f8;color:#20222a}}
h1{{font-size:2.3rem;line-height:1.2}}h2{{margin-top:2rem}}.badge{{display:inline-block;border-radius:8px;padding:8px 16px;background:#e1e8ef;font-weight:700}}
table{{width:100%;border-collapse:collapse;background:white}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #dde1e7;vertical-align:top;overflow-wrap:anywhere}}
th{{font-weight:600}}pre{{background:white;border:1px solid #dde1e7;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere}}
.help{{color:#505665}}@media print{{body{{background:white;margin:0;max-width:none}}h2{{break-after:avoid}}tr{{break-inside:avoid}}}}</style></head>
<body><p class="help">CUBE EDITOR · RELEASE EVIDENCE</p><h1>{escape(report['project']['name'])}</h1>
<p class="badge">{escape(status)}</p><p>Generated {escape(report['generated_at'])}. Evidence describes the ISO contents at that time.</p>
<h2>Release integrity</h2>{rows([('ISO', verification['path']), ('Size (bytes)', verification['size_bytes']), ('Actual SHA256', verification['actual_sha256']), ('Sidecar result', verification['sidecar_status'] + ': ' + verification['sidecar_detail']), ('Sidecar SHA256', verification['sidecar_sha256'] or 'Not available'), ('Build record match', verification['recorded_status']), ('Recorded release SHA256', verification['recorded_sha256'] or 'Not available')])}
<h2>Boot and installer checks</h2><p>These outcomes were recorded by a person for this SHA256. Launching QEMU does not establish a passing result. A checksum does not certify a bootable or safe image.</p>{rows(boot_rows)}
<h2>Recorded check history</h2><table><thead><tr><th>Check</th><th>Outcome</th><th>Recorded at</th><th>Applicability</th><th>Notes</th></tr></thead><tbody>{history or '<tr><td colspan="5">No checks recorded.</td></tr>'}</tbody></table>
<h2>Build provenance and recipe</h2><p>The following settings were saved with the selected release; they may differ from the current project recipe.</p><pre>{provenance}</pre>
<h2>Source provenance</h2><pre>{source}</pre><p class="help">This self-contained report contains local paths and recipe commands. Retain it alongside the ISO and checksum when sharing release evidence.</p></body></html>\n'''


def export_release_report(project, path, release=None):
    """Write a new standalone HTML report, never replacing an existing file.

    Reports cannot be saved into an editable image or restore point. An HTML
    filename is required, protecting ISO and project metadata paths by design.
    """
    destination = Path(path).expanduser()
    if destination.suffix.lower() not in ('.html', '.htm'):
        raise engine.CubeError('Choose an .html filename for the release report')
    resolved = destination.resolve()
    if any(resolved.is_relative_to((project.directory / part).resolve()) for part in ('rootfs', 'iso', 'layers', 'restore-point', 'recovery')):
        raise engine.CubeError('Save the release report outside editable filesystems and recovery copies')
    protected = [Path(project.data['source_iso']), project.directory / 'cube-editor.json',
                 Path(project.data['output_dir']).expanduser() / project.data.get('output_name', 'cube-editor.iso')]
    protected += [Path(record['path']) for record in project.data.get('releases', []) if record.get('path')]
    if project.data.get('last_build', {}).get('path'):
        protected.append(Path(project.data['last_build']['path']))
    if isinstance(release, dict) and release.get('path'):
        protected.append(Path(release['path']))
    elif release:
        protected.append(Path(release))
    if any(resolved == value.expanduser().resolve() for value in protected):
        raise engine.CubeError('The release report cannot replace a project input or output')
    recipe = project.data.get('recipe', {})
    if any(resolved.is_relative_to(Path(item['source']).expanduser().resolve())
           for item in recipe.get('payloads', []) if item.get('source')):
        raise engine.CubeError('Save the release report outside recipe payload inputs')
    if destination.exists() or destination.is_symlink():
        raise engine.CubeError('The report file already exists; choose a new filename')
    report = release_report(project, release)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.cube-release-report-', dir=destination.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(_report_html(report))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        # Linking a completed file publishes atomically and fails on any existing
        # destination, including a symlink created while the ISO was being read.
        os.link(temporary, destination)
    except OSError as error:
        raise engine.CubeError(f'Cannot save release report: {error}') from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return report
