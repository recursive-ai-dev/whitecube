"""Worker audit regressions using disposable files and substituted guest execution."""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import unittest
from unittest import mock

from test_cube_editor import ProjectFixture
from engine import Cancelled, CubeError, Project
from worker import Worker, rebuild_iso


class PublicationTests(ProjectFixture):
    def export(self, command):
        output = Path(command[command.index('-outdev') + 1])
        output.write_bytes(b'rebuilt ISO')

    def test_concurrent_iso_and_sidecar_are_never_replaced(self):
        output = self.project.output_path
        checksum = Path(str(output) + '.sha256')

        def competing_export(command):
            self.export(command)
            output.write_bytes(b'other release')
            checksum.write_text('other checksum')

        with self.assertRaises(CubeError):
            rebuild_iso(self.project, competing_export)
        self.assertEqual(output.read_bytes(), b'other release')
        self.assertEqual(checksum.read_text(), 'other checksum')
        self.assertFalse(Path(str(output) + '.release.json').exists())
        self.assertFalse(list(output.parent.glob('.*.building')))

    def test_iso_conflict_rolls_back_only_this_attempts_sidecars(self):
        output = self.project.output_path

        def competing_export(command):
            self.export(command)
            output.write_bytes(b'other release')

        with self.assertRaises(CubeError):
            rebuild_iso(self.project, competing_export)
        self.assertEqual(output.read_bytes(), b'other release')
        self.assertFalse(Path(str(output) + '.sha256').exists())
        self.assertFalse(Path(str(output) + '.release.json').exists())

    def test_rollback_preserves_a_replacement_for_an_owned_sidecar(self):
        output = self.project.output_path
        checksum = Path(str(output) + '.sha256')
        manifest = Path(str(output) + '.release.json')
        link = os.link

        def publish_with_competitor(source, destination, **kwargs):
            if Path(destination) == manifest:
                checksum.unlink()
                checksum.write_text('new owner checksum')
                manifest.write_text('new owner manifest')
            return link(source, destination, **kwargs)

        with mock.patch('worker.os.link', side_effect=publish_with_competitor):
            with self.assertRaises(CubeError):
                rebuild_iso(self.project, self.export)
        self.assertEqual(checksum.read_text(), 'new owner checksum')
        self.assertEqual(manifest.read_text(), 'new owner manifest')
        self.assertFalse(output.exists())

    def test_two_projects_sharing_destination_cannot_export_concurrently(self):
        other = Project.create(self.source, 'Other', self.path / 'projects', self.path / 'output')
        other.save(output_name=self.project.data['output_name'])
        rejected_run = mock.Mock()

        def competing_export(command):
            with self.assertRaisesRegex(CubeError, 'Another build'):
                rebuild_iso(other, rejected_run)
            self.export(command)

        rebuild_iso(self.project, competing_export)
        rejected_run.assert_not_called()
        self.assertEqual(self.project.output_path.read_bytes(), b'rebuilt ISO')

    def test_sidecars_are_published_before_iso_and_source_digest_is_recorded(self):
        output = self.project.output_path
        link = os.link
        published = []

        def inspect_publication(source, destination, **kwargs):
            published.append(Path(destination))
            if Path(destination) == output:
                self.assertTrue(Path(str(output) + '.sha256').is_file())
                self.assertTrue(Path(str(output) + '.release.json').is_file())
            return link(source, destination, **kwargs)

        with mock.patch('worker.os.link', side_effect=inspect_publication):
            release = rebuild_iso(self.project, self.export)
        self.assertEqual(published[-1], output)
        self.assertEqual(release['source_sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(json.loads(Path(str(output) + '.release.json').read_text()), release)
        self.assertFalse(list(output.parent.glob('.*.building')))

    def test_changed_source_with_preserved_size_and_timestamp_cannot_export(self):
        original = self.source.stat()
        self.project.save(source_sha256=hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.source.write_bytes(b'x' * original.st_size)
        os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns))
        run = mock.Mock()
        with self.assertRaisesRegex(CubeError, 'SHA256'):
            rebuild_iso(self.project, run)
        run.assert_not_called()
        self.assertFalse(self.project.output_path.exists())
        self.assertFalse(list(self.project.output_path.parent.glob('.*.building')))

    def test_export_reads_verified_copy_if_original_changes_after_verification(self):
        original = self.source.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        self.project.save(source_sha256=digest)

        def mutate_source(command):
            copy = Path(command[command.index('-indev') + 1])
            self.assertNotEqual(copy, self.source)
            self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o444)
            self.source.write_bytes(b'changed during export')
            self.assertEqual(copy.read_bytes(), original)
            self.export(command)

        release = rebuild_iso(self.project, mutate_source)
        self.assertEqual(release['source_sha256'], digest)
        self.assertEqual(self.project.output_path.read_bytes(), b'rebuilt ISO')

    def test_source_mutation_during_copy_fails_before_export(self):
        mutated = False

        def mutate():
            nonlocal mutated
            if not mutated:
                mutated = True
                self.source.write_bytes(b'mutated source')

        run = mock.Mock()
        with self.assertRaisesRegex(CubeError, 'changed while preparing'):
            rebuild_iso(self.project, run, mutate)
        run.assert_not_called()
        self.assertFalse(self.project.output_path.exists())

    def test_source_path_replacement_during_copy_is_detected(self):
        replaced = False

        def replace():
            nonlocal replaced
            if not replaced:
                replaced = True
                original = self.source.stat()
                replacement = self.source.with_suffix('.replacement')
                replacement.write_bytes(self.source.read_bytes())
                os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
                replacement.replace(self.source)

        with self.assertRaisesRegex(CubeError, 'changed while preparing'):
            rebuild_iso(self.project, self.export, replace)
        self.assertFalse(self.project.output_path.exists())

    def test_filesystem_without_hardlinks_uses_no_replace_rename(self):
        with mock.patch('worker.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'no hard links')):
            release = rebuild_iso(self.project, self.export)
        self.assertEqual(self.project.output_path.read_bytes(), b'rebuilt ISO')
        self.assertEqual(json.loads(Path(str(self.project.output_path) + '.release.json').read_text()), release)

    def test_no_replace_rename_fallback_preserves_competing_output(self):
        output = self.project.output_path

        def competing_export(command):
            self.export(command)
            output.write_bytes(b'other release')

        with mock.patch('worker.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'no hard links')):
            with self.assertRaises(CubeError):
                rebuild_iso(self.project, competing_export)
        self.assertEqual(output.read_bytes(), b'other release')
        self.assertFalse(Path(str(output) + '.sha256').exists())
        self.assertFalse(Path(str(output) + '.release.json').exists())

    def test_cancellation_rolls_back_rename_fallback_publication(self):
        output = self.project.output_path
        checksum = Path(str(output) + '.sha256')

        def cancel_after_first_sidecar():
            if checksum.exists():
                raise Cancelled('cancelled publication')

        with mock.patch('worker.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'no hard links')):
            with self.assertRaises(Cancelled):
                rebuild_iso(self.project, self.export, cancel_after_first_sidecar)
        self.assertFalse(output.exists())
        self.assertFalse(checksum.exists())
        self.assertFalse(Path(str(output) + '.release.json').exists())
        self.assertFalse(list(output.parent.glob('.*.building')))


class EnvironmentTests(ProjectFixture):
    def worker(self, action='customize'):
        p = self.project
        (p.rootfs / 'etc').mkdir(parents=True, exist_ok=True)
        (p.rootfs / 'usr/sbin').mkdir(parents=True, exist_ok=True)
        p.iso_tree.mkdir(exist_ok=True)
        (p.directory / '.extracted').touch()
        worker = Worker(p, os.getuid(), os.getgid(), action)
        worker.ensure_extracted = mock.Mock()
        worker.user_ownership = mock.Mock()
        worker.emit = mock.Mock()
        worker.mount = mock.Mock()
        worker.cleanup_mounts = mock.Mock()
        worker.check_storage = mock.Mock()
        return worker

    def test_changed_environment_file_directory_fails_and_retains_original(self):
        worker = self.worker()
        dns = self.project.rootfs / 'etc/resolv.conf'
        dns.write_text('original guest DNS')
        with self.assertRaisesRegex(CubeError, 'restoration is incomplete'):
            with worker.chroot_environment():
                dns.unlink()
                dns.mkdir()
        self.assertEqual((self.project.directory / '.backup-etc-resolv.conf').read_text(), 'original guest DNS')
        self.assertEqual(json.loads((self.project.directory / '.guest-environment.json').read_text()), {'etc/resolv.conf': True})
        worker.cleanup_mounts.assert_called_once()

    def test_changed_parent_inside_guest_fails_instead_of_restoring_wrong_directory(self):
        worker = self.worker()
        root = self.project.rootfs
        (root / 'etc/resolv.conf').write_text('original guest DNS')
        with self.assertRaisesRegex(CubeError, 'restoration is incomplete'):
            with worker.chroot_environment():
                (root / 'etc').rename(root / 'old-etc')
                (root / 'etc').mkdir()
                (root / 'etc/resolv.conf').write_text('new directory contents')
        self.assertEqual((root / 'etc/resolv.conf').read_text(), 'new directory contents')
        self.assertEqual((self.project.directory / '.backup-etc-resolv.conf').read_text(), 'original guest DNS')

    def test_normal_cleanup_restores_symlink_and_removes_absent_original_policy(self):
        worker = self.worker()
        dns = self.project.rootfs / 'etc/resolv.conf'
        dns.symlink_to('/run/guest-resolver.conf')
        with worker.chroot_environment():
            self.assertTrue(dns.is_file())
            self.assertFalse(dns.is_symlink())
        self.assertEqual(os.readlink(dns), '/run/guest-resolver.conf')
        self.assertFalse((self.project.rootfs / 'usr/sbin/policy-rc.d').exists())
        self.assertFalse((self.project.directory / '.guest-environment.json').exists())

    def test_cleanup_mount_error_preserves_environment_failure_and_journal(self):
        worker = self.worker()
        dns = self.project.rootfs / 'etc/resolv.conf'
        dns.write_text('original')
        worker.cleanup_mounts.side_effect = OSError('mount cleanup broke')
        with self.assertRaises(CubeError) as failure:
            with worker.chroot_environment():
                dns.unlink()
                dns.mkdir()
        self.assertIn('became a directory', str(failure.exception))
        self.assertIn('mount cleanup broke', str(failure.exception))
        self.assertTrue((self.project.directory / '.guest-environment.json').exists())

    def test_build_rejects_each_environment_artifact_including_dangling_links(self):
        worker = self.worker('build')
        worker.prepare_installer = mock.Mock()
        worker.compress_tree = mock.Mock()
        names = ('.guest-environment.json', '.backup-etc-resolv.conf', '.backup-usr-sbin-policy-rc.d')
        for name in names:
            for symlink in (False, True):
                with self.subTest(name=name, symlink=symlink):
                    artifact = self.project.directory / name
                    if symlink:
                        artifact.symlink_to('missing-original')
                    else:
                        artifact.write_text('{}')
                    try:
                        with self.assertRaisesRegex(CubeError, 'Recover project'):
                            worker.build()
                    finally:
                        artifact.unlink()
        worker.check_storage.assert_not_called()
        worker.prepare_installer.assert_not_called()
        worker.compress_tree.assert_not_called()

    def test_existing_backup_prevents_session_before_mounting(self):
        worker = self.worker()
        (self.project.directory / '.backup-etc-resolv.conf').symlink_to('missing-original')
        with self.assertRaisesRegex(CubeError, 'Recover project'):
            with worker.chroot_environment():
                self.fail('Should not enter guest session')
        worker.mount.assert_not_called()

    def test_missing_original_backup_keeps_recovery_journal_and_fails(self):
        worker = self.worker('recover')
        dns = self.project.rootfs / 'etc/resolv.conf'
        dns.write_text('temporary host DNS')
        journal = self.project.directory / '.guest-environment.json'
        journal.write_text(json.dumps({'etc/resolv.conf': True}))
        with self.assertRaisesRegex(CubeError, 'backup is missing'):
            worker.recover()
        self.assertEqual(dns.read_text(), 'temporary host DNS')
        self.assertTrue(journal.exists())
        with self.assertRaises(CubeError):
            self.worker('build').build()

    def test_recovery_restores_only_remaining_journal_entries(self):
        worker = self.worker('recover')
        dns = self.project.rootfs / 'etc/resolv.conf'
        dns.write_text('already restored DNS')
        policy = self.project.rootfs / 'usr/sbin/policy-rc.d'
        policy.write_text('temporary policy')
        (self.project.directory / '.guest-environment.json').write_text(json.dumps({'usr/sbin/policy-rc.d': False}))
        worker.recover()
        self.assertEqual(dns.read_text(), 'already restored DNS')
        self.assertFalse(policy.exists())
        self.assertFalse((self.project.directory / '.guest-environment.json').exists())

    def test_recovery_cannot_move_output_staging_while_publisher_holds_lock(self):
        worker = self.worker('recover')
        output = self.project.output_path
        output.parent.mkdir()
        staging = output.with_name('.' + output.name + '.building')
        staging.write_bytes(b'in progress output')
        lock = os.open(output.with_name('.' + output.name + '.publish.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(CubeError, 'Another project'):
                worker.recover()
            self.assertEqual(staging.read_bytes(), b'in progress output')
        finally:
            os.close(lock)
        worker.recover()
        self.assertFalse(staging.exists())
        retained = list((self.project.directory / 'recovery').glob(staging.name + '-*'))
        self.assertEqual(retained[0].read_bytes(), b'in progress output')

    def test_execute_holds_lock_through_child_cleanup_mount_cleanup_and_failure_save(self):
        worker = self.worker('recover')
        worker.validate = mock.Mock()
        worker.recover = mock.Mock(side_effect=CubeError('original operation failure'))
        other = Project.load(self.project.directory)
        checked = []

        def assert_locked(phase):
            checked.append(phase)
            with self.assertRaises(CubeError):
                with other.lock():
                    pass

        def unmount():
            assert_locked('unmount')
            raise OSError('additional cleanup failure')

        saved = worker.save

        def save(**updates):
            assert_locked('save')
            saved(**updates)

        worker.save = save
        worker.cleanup_mounts.side_effect = unmount
        child = mock.Mock(pid=43210)
        child.poll.return_value = None
        child.wait.side_effect = lambda: assert_locked('child wait')
        worker.child = child
        with mock.patch('worker.signal.signal'), mock.patch('worker.os.killpg'):
            self.assertEqual(worker.execute(), 1)
        self.assertEqual(checked, ['child wait', 'unmount', 'save'])
        self.assertEqual(Project.load(self.project.directory).data['last_error'], 'original operation failure')
        self.assertTrue(worker.stop_watcher.is_set())
        self.assertIsNone(worker.child)
        with other.lock():
            pass

    def test_execute_final_cleanup_failure_marks_operation_failed(self):
        worker = self.worker('recover')
        worker.validate = mock.Mock()
        worker.recover = mock.Mock()
        worker.cleanup_mounts.side_effect = OSError('cleanup failed')
        with mock.patch('worker.signal.signal'):
            self.assertEqual(worker.execute(), 1)
        self.assertEqual(Project.load(self.project.directory).data['state'], 'error')
        self.assertEqual(Project.load(self.project.directory).data['last_error'], 'cleanup failed')
        self.assertFalse(worker.emit.call_args.kwargs['success'])
        self.assertTrue(worker.stop_watcher.is_set())

    @unittest.skipIf(os.getuid() == 0, 'Worker validation requires a desktop-owned project')
    def test_source_independent_actions_validate_when_source_is_missing_or_changed(self):
        independent = ('recover', 'restore', 'snapshot', 'terminal', 'packages', 'customize')
        self.source.unlink()
        for action in independent:
            with self.subTest(action=action, source='missing'):
                self.worker(action).validate()
        for action in ('extract', 'build'):
            with self.subTest(action=action), self.assertRaisesRegex(CubeError, 'moved or deleted'):
                self.worker(action).validate()
        self.source.write_bytes(b'changed contents')
        for action in independent:
            with self.subTest(action=action, source='changed'):
                self.worker(action).validate()
        for action in ('extract', 'build'):
            with self.subTest(action=action), self.assertRaisesRegex(CubeError, 'changed since'):
                self.worker(action).validate()


if __name__ == '__main__':
    unittest.main()
