#!/usr/bin/python3 -I
"""whitecube-migrate against a fake root and home: dry run, apply, exact rollback, and the failure paths."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader('migrate', str(ROOT / 'tools' / 'whitecube-migrate'))
spec = importlib.util.spec_from_loader('migrate', loader)
migrate = importlib.util.module_from_spec(spec)
loader.exec_module(migrate)


class Runner:
    """Stands in for dpkg-query / systemctl: records every call."""
    def __init__(self, installed='0.3.0', enabled='enabled'):
        self.calls, self.installed, self.enabled = [], installed, enabled

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out, code = '', 0
        if argv[0] == 'dpkg-query':
            out, code = (self.installed, 0) if self.installed else ('', 1)
        elif argv[:2] == ['systemctl', 'is-enabled']:
            out = self.enabled
        return subprocess.CompletedProcess(argv, code, out, '')


def snapshot(*bases):
    seen = {}
    for base in bases:
        for path in sorted(Path(base).rglob('*')):
            if path.is_symlink():
                seen[str(path)] = ('link', os.readlink(path))
            elif path.is_file():
                seen[str(path)] = ('file', path.read_bytes(), path.stat().st_mode & 0o7777)
            else:
                seen[str(path)] = ('dir',)
    return seen


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.home = base / 'root', base / 'home'
        self.put('usr/lib/systemd/system/whitecube-network.service', '[Service]\nExecStart=/usr/bin/cube-net run\n')
        self.put('etc/systemd/system/whitecube-network.service', '[Service]\nExecStart=/usr/local/bin/cube-net run\n')
        self.put('etc/systemd/system/whitecube-network.service.d/hardening.conf', '[Service]\nPrivateDevices=yes\n')
        for name in ('whitecube', 'cube-net', 'cube-status', 'whitecube.bak-20261007', 'cube-status.round2', 'ollama', 'cube-unrelated'):
            self.put(f'usr/local/bin/{name}', f'old {name}\n', 0o755)
        self.put('usr/local/libexec/cube-settings-helper', 'old helper\n', 0o755)
        self.put('usr/local/libexec/cube-net-dns', 'old dns\n', 0o755)
        self.put('usr/local/share/whitecube-workspace/server.py', 'old server\n')
        self.put('usr/local/share/applications/whitecube-workspace.desktop', 'entry\n')
        self.put('etc/sudoers.d/whitecube-settings',
                 'Cmnd_Alias CUBE = /usr/local/libexec/cube-settings-helper services\nwoez ALL=(root) NOPASSWD: CUBE\n', 0o440)
        for name in ('whitecube', 'cube-status', 'cube-settings', 'whitecube-session', 'desktop-chat', 'claude', 'cube-lock'):
            self.put(f'../home/.local/bin/{name}', f'user {name}\n', 0o755)
        self.put('../home/.local/share/whitecube/schemas/gschemas.compiled', 'schema')
        self.put('../home/.local/share/applications/cube-settings.desktop', 'entry')
        self.put('../home/.local/share/applications/someone-elses.desktop', 'entry')
        self.put('../home/.local/share/icons/hicolor/scalable/apps/cube-lock.svg', '<svg/>')
        self.put('../home/.bash_profile', '. ~/.bashrc\nif [ "$(tty)" = /dev/tty1 ]; then\n  exec "$HOME/.local/bin/whitecube-session"\nfi\n')
        self.put('../home/.config/whitecube/config', 'terminal=/usr/local/bin/whitecube-terminal\nsettings_cmd=/home/woez/.local/bin/cube-settings\nlauncher=fuzzel\n', 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, text, mode=0o644):
        path = (self.root / rel).resolve() if rel.startswith('..') else self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def migration(self, runner=None, visudo=None, wait=None):
        self.runner = runner or Runner()
        return migrate.Migration(self.root, self.home, 'woez', run=self.runner, visudo=visudo or (lambda p: True), wait=wait or (lambda s: True))

    def tree(self):
        return snapshot(self.root, self.home)


class DryRunAndPlan(Base):
    def test_a_dry_run_changes_nothing_and_lists_the_steps(self):
        before = self.tree()
        out = self.run_main(['--user', 'woez'])
        self.assertEqual(before, self.tree())
        self.assertIn('Dry run', out)
        for expected in ('whitecube-network.service', 'whitecube.bak-20261007', 'cube-settings-helper', 'whitecube-workspace',
                         '~/.local/bin/whitecube-session', '~/.bash_profile', 'sudoers'):
            self.assertIn(expected, out)

    def run_main(self, argv):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = migrate.main(argv + ['--root', str(self.root)], self.migration())
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_without_the_package_it_refuses_and_says_how_to_get_it(self):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = migrate.main(['--root', str(self.root)], self.migration(Runner(installed=None)))
        self.assertEqual(code, 1)
        self.assertIn('not installed', buffer.getvalue())
        self.assertIn('build-deb.sh', buffer.getvalue())

    def test_unrelated_files_are_never_part_of_the_plan(self):
        text = ' | '.join(d for d, _ in self.migration().plan())
        for innocent in ('ollama', 'cube-unrelated', '/claude', 'someone-elses'):
            self.assertNotIn(innocent, text)


class ApplyAndRollback(Base):
    def test_apply_moves_old_copies_and_rewrites_the_dotfiles(self):
        m = self.migration()
        done = m.apply()
        self.assertGreaterEqual(len(done), 15)
        self.assertFalse((self.root / 'usr/local/bin/whitecube').exists())
        self.assertFalse((self.root / 'usr/local/bin/whitecube.bak-20261007').exists())
        self.assertFalse((self.root / 'usr/local/libexec/cube-settings-helper').exists())
        self.assertFalse((self.root / 'usr/local/share/whitecube-workspace').exists())
        self.assertTrue((self.root / 'usr/local/bin/ollama').exists(), 'unrelated programs stay')
        self.assertTrue((self.root / 'usr/local/bin/cube-unrelated').exists())
        self.assertFalse((self.home / '.local/bin/whitecube').exists())
        self.assertTrue((self.home / '.local/bin/claude').exists(), "other tools in ~/.local/bin stay")
        self.assertTrue((self.home / '.local/share/applications/someone-elses.desktop').exists())
        self.assertFalse((self.root / 'etc/systemd/system/whitecube-network.service').exists())
        self.assertTrue((self.root / 'etc/systemd/system/whitecube-network.service.d/hardening.conf').exists(), 'drop-ins stay')
        sudoers = (self.root / 'etc/sudoers.d/whitecube-settings').read_text()
        self.assertIn('/usr/libexec/whitecube/cube-settings-helper services', sudoers)
        self.assertIn('woez ALL=(root) NOPASSWD: CUBE', sudoers)
        self.assertEqual((self.root / 'etc/sudoers.d/whitecube-settings').stat().st_mode & 0o7777, 0o440)
        self.assertIn('exec /usr/bin/whitecube-session', (self.home / '.bash_profile').read_text())
        config = (self.home / '.config/whitecube/config').read_text()
        self.assertIn('terminal=/usr/bin/whitecube-terminal', config)
        self.assertIn('settings_cmd=/usr/bin/cube-settings', config)
        self.assertEqual((self.home / '.config/whitecube/config').stat().st_mode & 0o7777, 0o600)

    def test_rollback_restores_every_file_exactly(self):
        before = self.tree()
        m = self.migration()
        m.apply()
        self.assertNotEqual(before, self.tree())
        backup = m.backup
        restored = self.migration().rollback(backup)
        self.assertTrue(restored)
        after = {k: v for k, v in self.tree().items() if '/var/backups' not in k and not k.endswith('/root/var')}   # the backup itself stays
        self.assertEqual({k: v for k, v in before.items()}, after)

    def test_the_network_service_is_not_restarted_unless_asked(self):
        m = self.migration()
        m.apply()
        self.assertNotIn(['systemctl', 'restart', 'whitecube-network.service'], self.runner.calls)
        self.assertIn(['systemctl', 'daemon-reload'], self.runner.calls)
        self.assertIn(['systemctl', 'enable', 'whitecube-network.service'], self.runner.calls)

    def test_a_disabled_unit_stays_disabled(self):
        m = self.migration(Runner(enabled='disabled'))
        m.apply()
        self.assertNotIn(['systemctl', 'enable', 'whitecube-network.service'], self.runner.calls)

    def test_a_restart_that_does_not_bring_wifi_back_restores_the_old_unit(self):
        m = self.migration(wait=lambda seconds: False)
        with self.assertRaises(RuntimeError):
            m.apply(restart_network=True)
        unit = self.root / 'etc/systemd/system/whitecube-network.service'
        self.assertIn('/usr/local/bin/cube-net run', unit.read_text())
        self.assertEqual(self.runner.calls.count(['systemctl', 'restart', 'whitecube-network.service']), 2)
        self.assertTrue((m.backup / 'manifest.json').exists(), 'the manifest is written even when a step fails')

    def test_a_sudoers_rule_that_does_not_parse_is_never_installed(self):
        original = (self.root / 'etc/sudoers.d/whitecube-settings').read_text()
        m = self.migration(visudo=lambda path: False)
        with self.assertRaises(RuntimeError):
            m.apply()
        self.assertEqual((self.root / 'etc/sudoers.d/whitecube-settings').read_text(), original)
        self.assertFalse(list((self.root / 'etc/sudoers.d').glob('*.new-*')), 'no temporary file is left behind')

    def test_running_it_twice_has_nothing_left_to_do(self):
        self.migration().apply()
        again = self.migration()
        self.assertEqual(again.plan(), [])


class Rewrites(unittest.TestCase):
    def test_config_rewrite_handles_each_old_form(self):
        text = ('terminal=/usr/local/bin/whitecube-terminal\n'
                'a=/home/anyone/.local/bin/cube-settings\nb=$HOME/.local/bin/cube-status\nc=~/.local/bin/desktop-chat --toggle\n'
                'd=/home/anyone/.local/bin/claude\ne=fuzzel\n')
        out = migrate.Migration.rewrite_config(text)
        self.assertIn('terminal=/usr/bin/whitecube-terminal', out)
        self.assertIn('a=/usr/bin/cube-settings', out)
        self.assertIn('b=/usr/bin/cube-status', out)
        self.assertIn('c=/usr/bin/desktop-chat --toggle', out)
        self.assertIn('d=/home/anyone/.local/bin/claude', out, 'tools that are not Whitecube are left alone')
        wc_out = migrate.Migration.rewrite_config('check=~/.local/bin/wc-doctor\n')
        self.assertEqual(wc_out, 'check=/usr/bin/wc-doctor\n')

    def test_newer_apps_and_wc_suite_are_migrated_and_json_cli_works(self):
        import io
        from contextlib import redirect_stdout
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / 'root'
        home = Path(tmp.name) / 'home'
        (root / 'usr/bin').mkdir(parents=True)
        (root / 'usr/bin/wc-ls').write_text('#!/bin/sh\n')
        (home / '.local/bin').mkdir(parents=True)
        (home / '.local/bin/cube-prompts').write_text('#!/bin/sh\n')
        (home / '.local/bin/wc-ls').write_text('#!/bin/sh\nexec python3 "$HOME/.local/lib/wc-suite/wc_suite.py" "ls"\n')
        (home / '.local/bin/wc-custom').write_text('#!/bin/sh\necho user-tool\n')
        (home / '.local/lib/wc-suite').mkdir(parents=True)
        (home / '.local/lib/wc-suite/wc_suite.py').write_text('# stub\n')
        (home / '.local/share/bash-completion/completions').mkdir(parents=True)
        (home / '.local/share/bash-completion/completions/wc-ls').write_text('# comp\n')
        (home / '.local/share/zsh/site-functions').mkdir(parents=True)
        (home / '.local/share/zsh/site-functions/_wc').write_text('#compdef _wc\n')
        (home / '.config/fish/completions').mkdir(parents=True)
        (home / '.config/fish/completions/wc-ls.fish').write_text('complete -c wc-ls\n')
        (home / '.local/share/applications').mkdir(parents=True)
        (home / '.local/share/applications/cube-prompts.desktop').write_text('[Desktop Entry]\n')
        # FIFO / symlink dotfile should be skipped cleanly with a warning instead of hanging.
        os.mkfifo(home / '.bash_profile')

        runner = Runner()
        migration = migrate.Migration(root, home, 'woez', run=runner)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = migrate.main(['--json', '--root', str(root)], migration)
        self.assertEqual(code, 0)
        plan_payload = json.loads(buf.getvalue())
        self.assertEqual(plan_payload['mode'], 'dry-run')
        self.assertTrue(any('cube-prompts' in s for s in plan_payload['steps']))
        self.assertTrue(any('wc-suite' in s for s in plan_payload['steps']))
        self.assertTrue(any('wc-ls' in s for s in plan_payload['steps']))
        self.assertTrue(plan_payload['warnings'])

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = migrate.main(['--apply', '--json', '--root', str(root)], migration)
        self.assertEqual(code, 0)
        apply_payload = json.loads(buf.getvalue())
        self.assertTrue(apply_payload['ok'])
        self.assertFalse((home / '.local/bin/cube-prompts').exists())
        self.assertFalse((home / '.local/bin/wc-ls').exists())
        self.assertFalse((home / '.local/lib/wc-suite').exists())
        self.assertTrue((home / '.local/bin/wc-custom').exists(), 'unrelated wc-* user script stays')

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = migrate.main(['--rollback', apply_payload['backup'], '--json', '--root', str(root)], migration)
        self.assertEqual(code, 0)
        rb_payload = json.loads(buf.getvalue())
        self.assertEqual(rb_payload['mode'], 'rollback')
        self.assertTrue((home / '.local/bin/cube-prompts').exists())
        self.assertTrue((home / '.local/lib/wc-suite/wc_suite.py').exists())

    def test_rollback_rejects_path_traversal_outside_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            home = Path(tmp) / 'home'
            backup = Path(tmp) / 'backup'
            root.mkdir()
            home.mkdir()
            (backup / 'files').mkdir(parents=True)
            stolen = backup / 'files/evil'
            stolen.write_text('x')
            (backup / 'manifest.json').write_text(json.dumps({
                'log': [{'moved': str(Path(tmp) / 'outside/target'), 'to': str(stolen)}]
            }))
            migration = migrate.Migration(root, home, 'woez')
            with self.assertRaisesRegex(RuntimeError, 'outside target tree'):
                migration.rollback(backup)

    def test_version_flag_outputs_package_version(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(buf):
            migrate.main(['--version'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('whitecube-migrate ', buf.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
