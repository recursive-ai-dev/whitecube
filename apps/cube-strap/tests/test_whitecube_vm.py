"""Tests for the whitecube-vm CLI lifecycle, locking, and QEMU/OVMF invocation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / 'bin/whitecube-vm'
STRAP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STRAP_DIR))
import strap


class WhitecubeVmCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='whitecube-vm-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.ovmf = self.root / 'ovmf'
        self.ovmf.mkdir()
        (self.ovmf / 'OVMF_CODE.fd').write_bytes(b'CODE')
        (self.ovmf / 'OVMF_VARS.fd').write_bytes(b'VARS')
        self.qemu = self.bin / 'qemu-system-x86_64'
        self.qemu.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$@" > "${TEST_ARGS_OUT}"\n'
            'if [ -n "${TEST_LOCK_READY:-}" ]; then\n'
            '  touch "${TEST_LOCK_READY}"\n'
            '  sleep 5\n'
            'fi\n'
        )
        self.qemu.chmod(0o755)
        self.env = {
            'PATH': f'{self.bin}:/usr/bin:/bin',
            'HOME': str(self.root / 'home'),
            'XDG_STATE_HOME': str(self.root / 'state'),
            'WHITECUBE_HOME': str(self.root / 'store'),
            'WHITECUBE_OVMF_CODE': str(self.ovmf / 'OVMF_CODE.fd'),
            'WHITECUBE_OVMF_VARS': str(self.ovmf / 'OVMF_VARS.fd'),
            'TEST_ARGS_OUT': str(self.root / 'qemu.args'),
        }
        (self.root / 'home').mkdir()

    def run_cli(self, *args, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run([sys.executable, '-I', str(CLI), *args],
                              env=env, capture_output=True, text=True, timeout=15)

    def sample_disk(self, name='demo.img'):
        path = self.root / name
        with open(path, 'wb') as f:
            f.write(b'boot' * 256)
            f.seek(4 * 1024 * 1024 - 512)
            f.write(b'end' * 64)
        return path

    def test_check_import_list_info_dry_run_and_remove(self):
        checked = self.run_cli('check', '--json')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        report = json.loads(checked.stdout)
        self.assertTrue(report['ready'])
        self.assertEqual(report['qemu'], str(self.qemu))

        missing = self.run_cli('check', '--json', extra_env={'PATH': str(self.root / 'empty-bin'), 'WHITECUBE_OVMF_CODE': str(self.root / 'absent')})
        self.assertEqual(missing.returncode, 1)
        self.assertEqual([item['package'] for item in json.loads(missing.stdout)['missing']], ['qemu-system-x86', 'ovmf'])

        img = self.sample_disk()
        imp = self.run_cli('import', str(img), 'trixie-vm', '--suite', 'trixie', '--recipe', 'server', '--memory', '3072', '--cpus', '3')
        self.assertEqual(imp.returncode, 0, imp.stderr)

        listed = json.loads(self.run_cli('list', '--json').stdout)
        self.assertEqual([item['name'] for item in listed], ['trixie-vm'])
        self.assertEqual(listed[0]['memory_mib'], 3072)
        self.assertFalse(listed[0]['running'])

        info = json.loads(self.run_cli('info', 'trixie-vm', '--json').stdout)
        self.assertTrue(info['boot_ready'])
        self.assertEqual(info['cpus'], 3)
        self.assertIn('trixie-vm', info['vars_path'])

        dry = self.run_cli('run', 'trixie-vm', '--dry-run', '--ssh', '--ssh-port', '2223', '--headless', '--snapshot')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn('hostfwd=tcp:127.0.0.1:2223-:22', dry.stdout)
        self.assertIn('-display none', dry.stdout)
        self.assertIn('snapshot=on', dry.stdout)
        self.assertFalse((self.root / 'qemu.args').exists())

        with mock.patch.object(strap, 'STATE', self.root / 'state/cube-strap'):
            strap.save_history([{'name': 'trixie-vm', 'kind': 'vm', 'status': 'built'}])
            rm = self.run_cli('remove', 'trixie-vm')
            self.assertEqual(rm.returncode, 0, rm.stderr)
            self.assertEqual(strap.load_history(), [])
        self.assertEqual(json.loads(self.run_cli('list', '--json').stdout), [])

    def test_running_vm_blocks_concurrent_run_and_remove(self):
        self.assertEqual(self.run_cli('import', str(self.sample_disk()), 'lockedvm').returncode, 0)
        ready = self.root / 'ready'
        proc = subprocess.Popen(
            [sys.executable, '-I', str(CLI), 'run', 'lockedvm', '--ssh'],
            env=dict(self.env, TEST_LOCK_READY=str(ready)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), 'fake QEMU did not start')

            dup = self.run_cli('run', 'lockedvm')
            self.assertNotEqual(dup.returncode, 0)
            self.assertIn('already running', dup.stderr)

            rm = self.run_cli('remove', 'lockedvm')
            self.assertNotEqual(rm.returncode, 0)
            self.assertIn('currently running', rm.stderr)

            listed = json.loads(self.run_cli('list', '--json').stdout)
            self.assertTrue(listed[0]['running'])
        finally:
            proc.terminate()
            proc.wait(timeout=5)
