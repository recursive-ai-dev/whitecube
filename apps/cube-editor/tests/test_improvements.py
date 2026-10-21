"""Regression coverage for recovery, portable recipes and release provenance.

Filesystem operations use tiny local fixtures; no mounts, sudo or live images.
"""
import contextlib
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from worker import Worker, copy_payload
from test_cube_editor import ProjectFixture


class ImprovementsTests(ProjectFixture):
    def worker(self, action='snapshot'):
        worker = Worker(self.project, os.getuid(), os.getgid(), action)
        worker.ensure_extracted = mock.Mock()
        worker.user_ownership = mock.Mock()
        worker.emit = mock.Mock()
        return worker

    def trees(self):
        self.project.rootfs.mkdir()
        self.project.iso_tree.mkdir()
        (self.project.rootfs / 'config').write_text('original')
        (self.project.iso_tree / 'boot.cfg').write_text('original boot')
        (self.project.directory / '.extracted').touch()

    def test_file_payload_cannot_follow_a_child_link_inside_a_destination_directory(self):
        self.trees()
        source = self.path / 'payload'; source.write_text('new')
        destination = self.project.rootfs / 'etc'; destination.mkdir()
        host = self.path / 'host-file'; host.write_text('keep')
        (destination / 'payload').symlink_to(host)
        with self.assertRaises(engine.CubeError): copy_payload(source, destination, self.project.rootfs)
        self.assertEqual(host.read_text(), 'keep')

    def test_hashing_detects_mutation_during_read(self):
        path = self.path / 'hash-input'; path.write_bytes(b'x' * (5 * 1024**2))
        changed = False
        def mutate(done, total):
            nonlocal changed
            if not changed:
                changed = True
                with path.open('r+b') as stream:
                    stream.seek(-1, 2); stream.write(b'y')
        with self.assertRaises(engine.CubeError): engine.sha256_file(path, mutate)

    def test_recovery_does_not_delete_regular_files_listed_as_whiteouts(self):
        self.trees()
        (self.project.directory / '.whiteouts.json').write_text(json.dumps(['config']))
        self.worker('recover').recover()
        self.assertEqual((self.project.rootfs / 'config').read_text(), 'original')
        self.assertFalse((self.project.directory / '.whiteouts.json').exists())

    def test_cli_previews_and_portable_recipe_commands(self):
        executable = [sys.executable, str(Path(engine.__file__))]
        recipe_file = self.path / 'portable.json'
        commands = [
            ['export-recipe', str(self.project.directory), str(recipe_file)],
            ['import-recipe', str(self.project.directory), str(recipe_file)],
            ['recovery', str(self.project.directory)],
            ['releases', str(self.project.directory)],
            ['plan', str(self.project.directory), 'build'],
        ]
        for arguments in commands:
            with self.subTest(arguments=arguments):
                result = subprocess.run(executable + arguments, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                if arguments[0] == 'plan':
                    self.assertTrue(value['dry_run']); self.assertIn('storage', value)

    def test_multiline_recipe_keeps_loops_and_heredoc_intact(self):
        script = "for item in a b; do\n  echo \"$item\"\ndone\ncat <<'END'\nhello\nEND\n"
        self.project.save(recipe={'commands': [script]})
        plan = engine.Engine(self.project).plan('customize')
        self.assertEqual(plan[-1][-1], script)
        result = subprocess.run(['bash', '-euc', engine.recipe_scripts(self.project.data['recipe'])[0]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'a\nb\nhello\n')
        self.assertEqual(engine.recipe_scripts({'commands': ['echo old'], 'script': 'echo new'}), ['echo old', 'echo new'])

    def test_recipe_roundtrip_and_missing_file_diagnostics(self):
        recipe = {'install_packages': ['curl'], 'commands': ['echo hi'], 'payloads': [
            {'source': str(self.path / 'missing'), 'destination': '/etc/config', 'overwrite': 'skip'}]}
        self.project.save(recipe=recipe)
        path = self.path / 'recipe.json'
        engine.export_recipe(self.project, path)
        restored, missing = engine.import_recipe(path)
        self.assertEqual(restored, recipe)
        self.assertEqual(missing, [str(self.path / 'missing')])
        with self.assertRaises(engine.CubeError): engine.validate_recipe(restored, check_files=True)

    def test_recipe_rejects_duplicate_destinations_and_conflicting_packages(self):
        for recipe in (
            {'install_packages': ['curl'], 'remove_packages': ['curl']},
            {'payloads': [{'source': '/one', 'destination': '/etc/x'}, {'source': '/two', 'destination': '/etc//x'}]},
            {'payloads': [{'source': '/one', 'destination': '/../escape'}]},
            {'payloads': [{'source': 'relative', 'destination': '/etc/x'}]},
            {'commands': [123]}, {'script': []}, {'payloads': {}},
        ):
            with self.subTest(recipe=recipe), self.assertRaises(engine.CubeError): engine.validate_recipe(recipe)

    def test_payload_skip_and_fail_preserve_existing_files(self):
        self.trees()
        source = self.path / 'payload'; source.write_text('replacement')
        destination = self.project.rootfs / 'config'
        copy_payload(source, destination, self.project.rootfs, 'skip')
        self.assertEqual(destination.read_text(), 'original')
        with self.assertRaises(engine.CubeError): copy_payload(source, destination, self.project.rootfs, 'error')
        self.assertEqual(destination.read_text(), 'original')
        copy_payload(source, destination, self.project.rootfs)
        self.assertEqual(destination.read_text(), 'replacement')

    def test_storage_groups_project_and_output_on_same_device(self):
        self.trees()
        with mock.patch('engine.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)):
            report = engine.storage_preflight(self.project, 'build')
        self.assertFalse(report['sufficient'])
        self.assertEqual(len(report['checks']), 1)
        self.assertEqual(len(report['checks'][0]['reasons']), 2)
        self.assertGreater(report['checks'][0]['required_bytes'], 1024**3)

    def test_low_storage_stops_snapshot_before_creating_staging(self):
        self.trees()
        worker = self.worker()
        with mock.patch('worker.storage_preflight', return_value={'sufficient': False, 'checks': []}):
            with self.assertRaises(engine.CubeError): worker.snapshot()
        self.assertFalse((self.project.directory / '.restore-point.building').exists())

    def test_restore_point_restores_both_trees_and_retains_replaced_files(self):
        self.trees()
        worker = self.worker()
        worker.snapshot()
        (self.project.rootfs / 'config').write_text('changed')
        (self.project.iso_tree / 'boot.cfg').write_text('changed boot')
        (self.project.rootfs / 'new').write_text('added')
        worker.restore()
        self.assertEqual((self.project.rootfs / 'config').read_text(), 'original')
        self.assertEqual((self.project.iso_tree / 'boot.cfg').read_text(), 'original boot')
        self.assertFalse((self.project.rootfs / 'new').exists())
        retained = list((self.project.directory / 'recovery').glob('rootfs-*'))
        self.assertEqual((retained[0] / 'config').read_text(), 'changed')
        self.assertTrue(engine.recovery_report(self.project)['restore_available'])

    def test_restore_copy_failure_keeps_current_trees(self):
        self.trees(); worker = self.worker(); worker.snapshot()
        (self.project.rootfs / 'config').write_text('current')
        worker.run = mock.Mock(side_effect=engine.CubeError('copy failed'))
        with self.assertRaises(engine.CubeError): worker.restore()
        self.assertEqual((self.project.rootfs / 'config').read_text(), 'current')
        self.assertEqual((self.project.iso_tree / 'boot.cfg').read_text(), 'original boot')

    def test_restore_promotion_failure_rolls_back_both_trees(self):
        self.trees(); worker = self.worker(); worker.snapshot()
        (self.project.rootfs / 'config').write_text('current root')
        (self.project.iso_tree / 'boot.cfg').write_text('current boot')
        rename = Path.rename
        def fail(path, target):
            if path == self.project.directory / '.restore.building/iso': raise OSError('promotion failed')
            return rename(path, target)
        with mock.patch.object(Path, 'rename', fail):
            with self.assertRaises(OSError): worker.restore()
        self.assertEqual((self.project.rootfs / 'config').read_text(), 'current root')
        self.assertEqual((self.project.iso_tree / 'boot.cfg').read_text(), 'current boot')

    def test_partial_extraction_recovery_quarantines_instead_of_deleting(self):
        self.project.rootfs.mkdir(); (self.project.rootfs / 'partial').write_text('keep')
        self.project.iso_tree.mkdir(); self.project.save(state='extracting')
        report = engine.recovery_report(self.project)
        self.assertTrue(report['interrupted']); self.assertFalse(report['extracted'])
        self.worker('recover').recover()
        self.assertFalse(self.project.rootfs.exists())
        kept = list((self.project.directory / 'recovery').glob('rootfs-*/partial'))
        self.assertEqual(kept[0].read_text(), 'keep')
        self.assertEqual(self.project.data['state'], 'new')

    def test_active_operation_is_not_reported_as_interrupted(self):
        self.project.save(state='building')
        with self.project.lock():
            report = engine.recovery_report(engine.Project.load(self.project.directory))
        self.assertTrue(report['active_operation']); self.assertFalse(report['interrupted'])

    def test_source_hash_match_and_mismatch(self):
        worker = engine.Engine(self.project)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = worker.verify_source(digest + '  source.iso')
        self.assertTrue(result['matched_supplied_checksum'])
        self.assertEqual(self.project.data['source_sha256'], digest)
        with self.assertRaises(engine.CubeError): worker.verify_source('0' * 64)
        self.assertEqual(self.project.data['source_sha256'], digest)

    def test_release_checks_use_actual_iso_hash_and_preserve_stale_updates(self):
        output = self.project.output_path; output.parent.mkdir(); output.write_bytes(b'first')
        api = engine.Engine(self.project)
        stale = engine.Engine(engine.Project.load(self.project.directory))
        first = api.record_check('BIOS live', 'passed', 'Desktop reached')
        output.write_bytes(b'changed')
        with self.assertRaises(engine.CubeError): stale.record_check('checksum', 'passed', expected_hash=first['sha256'])
        second = stale.record_check('UEFI installer', 'failed')
        checks = engine.Project.load(self.project.directory).data['release_checks']
        self.assertEqual(len(checks), 2); self.assertNotEqual(first['sha256'], second['sha256'])

    def test_previous_release_verification_does_not_use_current_output(self):
        previous = self.path / 'previous.iso'; previous.write_bytes(b'old release')
        Path(str(previous) + '.sha256').write_text(hashlib.sha256(previous.read_bytes()).hexdigest() + '  previous.iso\n')
        self.assertTrue(engine.Engine(self.project).verify_release(previous)['success'])

    def test_resource_validation_rejects_invalid_levels_and_options(self):
        options = engine.build_resources({'compression': 'zstd', 'processors': 4, 'memory_mb': 512, 'compression_level': 10})
        self.assertIn('512M', options); self.assertIn('10', options)
        for values in ({'processors': 0}, {'processors': '2'}, {'memory_mb': 12},
                       {'compression': 'xz', 'compression_level': 1}, {'compression': 'gzip', 'compression_level': 10}):
            with self.assertRaises(engine.CubeError): engine.build_resources(values)

    def test_complete_plan_explains_installer_cleanup_and_payloads(self):
        self.project.save(recipe={'payloads': [{'source': '/example', 'destination': '/etc/example'}]})
        api = engine.Engine(self.project)
        self.assertIn('payload-copy', api.plan('customize')[-1])
        steps = '\n'.join(api.detailed_plan('build')['steps'])
        self.assertIn('installer', steps); self.assertIn('deletion markers', steps); self.assertIn('sidecars', steps)

    @unittest.skipUnless(shutil.which('qemu-img'), 'qemu-img unavailable')
    def test_real_sparse_installer_disk_and_boot_order(self):
        output = self.project.output_path; output.parent.mkdir(); output.write_bytes(b'fixture')
        api = engine.Engine(self.project)
        disk = api.create_test_disk(8)
        info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', str(disk)]))
        self.assertEqual(info['virtual-size'], 8 * 1024**3)
        with mock.patch('engine.shutil.which', return_value='/usr/bin/qemu-system-x86_64'):
            installer = api.qemu_command(disk=disk)
            installed = api.qemu_command(disk=disk, installed=True)
        self.assertEqual(installer[installer.index('-boot') + 1], 'd')
        self.assertEqual(installed[installed.index('-boot') + 1], 'c')
        self.assertNotIn('-snapshot', installer)

    def test_recovery_restores_dns_symlink_and_removes_temporary_policy(self):
        self.trees()
        (self.project.rootfs / 'etc').mkdir()
        (self.project.rootfs / 'usr/sbin').mkdir(parents=True)
        (self.project.rootfs / 'etc/resolv.conf').write_text('temporary host DNS')
        (self.project.rootfs / 'usr/sbin/policy-rc.d').write_text('temporary policy')
        (self.project.directory / '.backup-etc-resolv.conf').symlink_to('/run/systemd/resolve/stub-resolv.conf')
        (self.project.directory / '.guest-environment.json').write_text(json.dumps({'etc/resolv.conf': True, 'usr/sbin/policy-rc.d': False}))
        self.worker('recover').recover()
        self.assertEqual(os.readlink(self.project.rootfs / 'etc/resolv.conf'), '/run/systemd/resolve/stub-resolv.conf')
        self.assertFalse((self.project.rootfs / 'usr/sbin/policy-rc.d').exists())
        self.assertFalse((self.project.directory / '.guest-environment.json').exists())

    @unittest.skipUnless(shutil.which('mksquashfs') and shutil.which('unsquashfs'), 'squashfs tools unavailable')
    def test_real_compression_resources_and_tool_progress(self):
        self.trees()
        self.project.save(compression='zstd', compression_level=3, processors=1, memory_mb=128)
        worker = self.worker('build')
        output = self.project.iso_tree / 'filesystem.squashfs'
        with contextlib.redirect_stdout(io.StringIO()): worker.compress_tree(output)
        # Production compression retains metadata; unprivileged fixture
        # extraction must not try to apply this host's security labels.
        subprocess.run(['unsquashfs', '-user-xattrs', '-no-progress', '-processors', '1', '-d', str(self.path / 'unpacked'), str(output)], check=True, capture_output=True)
        self.assertEqual((self.path / 'unpacked/config').read_text(), 'original')
        events = [c for c in worker.emit.call_args_list if c.args[0] == 'progress']
        self.assertTrue(events, 'Compression did not report tool progress')
        self.assertEqual(events[-1].kwargs['progress'], 1)
        self.assertIn('eta_seconds', events[-1].kwargs)

    @unittest.skipUnless(shutil.which('mksquashfs') and shutil.which('unsquashfs'), 'squashfs tools unavailable')
    def test_real_compression_preserves_guest_metadata(self):
        self.trees()
        config = self.project.rootfs / 'config'
        try:
            os.setxattr(config, 'user.cube-editor-test', b'guest metadata')
        except OSError as error:
            self.skipTest(f'Fixture filesystem does not support user xattrs: {error}')
        config.chmod(0o751)
        (self.project.rootfs / 'config-link').symlink_to('config')
        self.project.save(compression='gzip', processors=1, memory_mb=128)
        output = self.project.iso_tree / 'filesystem.squashfs'
        with contextlib.redirect_stdout(io.StringIO()):
            self.worker('build').compress_tree(output)
        unpacked = self.path / 'metadata-unpacked'
        subprocess.run(['unsquashfs', '-user-xattrs', '-no-progress', '-processors', '1',
                        '-d', str(unpacked), str(output)], check=True, capture_output=True)
        self.assertEqual(os.getxattr(unpacked / 'config', 'user.cube-editor-test'), b'guest metadata')
        self.assertEqual((unpacked / 'config').stat().st_mode & 0o777, 0o751)
        self.assertEqual(os.readlink(unpacked / 'config-link'), 'config')

    @unittest.skipUnless(Path('/usr/share/OVMF/OVMF_CODE_4M.fd').is_file(), 'OVMF unavailable')
    def test_uefi_installer_reuses_nvram_for_installed_reboot(self):
        output = self.project.output_path; output.parent.mkdir(); output.write_bytes(b'fixture')
        disk = self.path / 'test.qcow2'; disk.touch()
        api = engine.Engine(self.project)
        with mock.patch('engine.shutil.which', return_value='qemu-system-x86_64'):
            api.qemu_command(uefi=True, disk=disk)
            nvram = Path(str(disk) + '.vars.fd')
            nvram.write_bytes(b'recorded variables')
            command = api.qemu_command(uefi=True, disk=disk, installed=True)
        self.assertEqual(nvram.read_bytes(), b'recorded variables')
        self.assertIn(f'if=pflash,format=raw,file={nvram}', command)

    def test_build_appends_release_history(self):
        self.trees()
        self.project.save(releases=[{'path': '/old.iso', 'sha256': 'old'}], squashfs_path='/filesystem.squashfs')
        worker = self.worker('build')
        worker.run = mock.Mock(); worker.prepare_installer = mock.Mock(return_value=None)
        worker.refresh_metadata = mock.Mock(); worker.deletion_whiteouts = mock.Mock(return_value=[])
        worker.compress_tree = mock.Mock()
        release = {'path': str(self.project.output_path), 'sha256': 'new'}
        with mock.patch('worker.checksum_tree'), mock.patch('worker.rebuild_iso', return_value=release): worker.build()
        self.assertEqual(len(self.project.data['releases']), 2)
        self.assertEqual(self.project.data['last_build'], release)

    def test_package_simulation_does_not_apply_or_snapshot(self):
        self.project.save(recipe={'install_packages': ['curl'], 'remove_packages': ['nano']})
        worker = self.worker('packages')
        worker.chroot_environment = lambda: contextlib.nullcontext()
        worker.guest = mock.Mock(return_value='Inst curl\nRemv nano\n')
        worker.snapshot = mock.Mock()
        worker.packages()
        self.assertTrue(all('--simulate' in call.args[0] or ('--print-uris' in call.args[0] and '--download-only' in call.args[0]) for call in worker.guest.call_args_list))
        worker.snapshot.assert_not_called()
        self.assertIn('current apt indexes', (self.project.directory / 'package-preview.txt').read_text())


if __name__ == '__main__': unittest.main()
