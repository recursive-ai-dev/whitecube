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

            cl = self.run_cli('clone', 'lockedvm', 'clone-fail')
            self.assertNotEqual(cl.returncode, 0)
            self.assertIn('currently running', cl.stderr)

            tr = self.run_cli('trim', 'lockedvm')
            self.assertNotEqual(tr.returncode, 0)
            self.assertIn('currently running', tr.stderr)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_clone_trim_cdrom_and_serial_log_options(self):
        img = self.sample_disk('golden.img')
        self.assertEqual(self.run_cli('import', str(img), 'golden', '--suite', 'trixie', '--memory', '2048', '--cpus', '2').returncode, 0)
        # Mutate source vars.fd to confirm clones receive clean per-VM UEFI NVRAM.
        (self.root / 'store/images/golden/vars.fd').write_bytes(b'DIRTY-VARS')

        cloned = self.run_cli('clone', 'golden', 'golden-copy', '--memory', '4096', '--cpus', '4', '--json')
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        meta = json.loads(cloned.stdout)
        self.assertEqual(meta['name'], 'golden-copy')
        self.assertEqual(meta['cloned_from'], 'golden')
        self.assertEqual(meta['memory_mib'], 4096)
        self.assertEqual(meta['cpus'], 4)
        self.assertEqual((self.root / 'store/images/golden-copy/vars.fd').read_bytes(), b'VARS')
        self.assertEqual(
            (self.root / 'store/images/golden-copy/disk.img').read_bytes(),
            (self.root / 'store/images/golden/disk.img').read_bytes(),
        )
        dup = self.run_cli('clone', 'golden', 'golden-copy')
        self.assertNotEqual(dup.returncode, 0)

        # Write a zero-filled block into the copy's disk and compact it.
        copy_disk = self.root / 'store/images/golden-copy/disk.img'
        with open(copy_disk, 'r+b') as fh:
            fh.seek(1024 * 1024)
            fh.write(b'\x00' * (2 * 1024 * 1024))
        trimmed = self.run_cli('trim', 'golden-copy', '--json')
        self.assertEqual(trimmed.returncode, 0, trimmed.stderr)
        trim_report = json.loads(trimmed.stdout)
        self.assertEqual(trim_report['name'], 'golden-copy')
        self.assertGreaterEqual(trim_report['before_bytes'], trim_report['after_bytes'])

        iso = self.root / 'installer.iso'
        iso.write_bytes(b'ISO9660')
        serial_log = self.root / 'logs/boot.serial.log'
        dry = self.run_cli('run', 'golden-copy', '--dry-run', '--headless', '--cdrom', str(iso), '--serial-log', str(serial_log))
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f'file={iso},media=cdrom,readonly=on', dry.stdout)
        self.assertIn('-boot order=dc', dry.stdout)
        self.assertIn(f'-serial file:{serial_log}', dry.stdout)

        bad_serial = self.run_cli('run', 'golden-copy', '--dry-run', '--serial', '--serial-log', str(serial_log))
        self.assertNotEqual(bad_serial.returncode, 0)
        missing_iso = self.run_cli('run', 'golden-copy', '--dry-run', '--cdrom', str(self.root / 'absent.iso'))
        self.assertNotEqual(missing_iso.returncode, 0)
