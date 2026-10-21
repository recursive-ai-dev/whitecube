"""Exercise validation, transaction and failure reporting without authentication.

Only mount and guest execution are substituted. All project files are disposable.
"""
import json
import os
from unittest import mock
import unittest

from test_cube_editor import ProjectFixture
from engine import Project
from worker import Worker


@unittest.skipIf(os.getuid() == 0, 'Worker validation requires a desktop-owned project')
class WorkerLifecycleTests(ProjectFixture):
    def prepare(self):
        p = self.project
        (p.rootfs / 'etc').mkdir(parents=True)
        (p.rootfs / 'usr/sbin').mkdir(parents=True)
        p.iso_tree.mkdir()
        (p.directory / '.extracted').touch()
        (p.rootfs / 'etc/resolv.conf').write_text('original guest DNS')
        p.save(auto_snapshot=False, recipe={'commands': [':']}, squashfs_path='/filesystem.squashfs')

    def worker(self, action):
        worker = Worker(self.project, os.getuid(), os.getgid(), action)
        worker.user_ownership = mock.Mock()
        worker.ensure_extracted = mock.Mock()  # Fixture root is intentionally unprivileged.
        worker.mount = mock.Mock()
        worker.cleanup_mounts = mock.Mock()
        worker.check_storage = mock.Mock()
        return worker

    def execute(self, worker):
        with mock.patch('worker.signal.signal'), mock.patch('builtins.print'):
            result = worker.execute()
        self.assertTrue(worker.stop_watcher.is_set())
        worker.cleanup_mounts.assert_called()
        # The worker must release the transaction after success and failure.
        with Project.load(self.project.directory).lock():
            pass
        return result

    def events(self):
        return [json.loads(line) for line in (self.project.directory / 'events.jsonl').read_text().splitlines()]

    def test_offline_recovery_runs_through_worker_validation_and_transaction(self):
        self.prepare()
        p = self.project
        (p.rootfs / 'etc/resolv.conf').rename(p.directory / '.backup-etc-resolv.conf')
        (p.rootfs / 'etc/resolv.conf').write_text('temporary host DNS')
        (p.directory / '.guest-environment.json').write_text(json.dumps({'etc/resolv.conf': True}))
        p.save(state='error')
        self.source.unlink()
        self.assertEqual(self.execute(self.worker('recover')), 0)
        self.assertEqual((p.rootfs / 'etc/resolv.conf').read_text(), 'original guest DNS')
        self.assertFalse((p.directory / '.guest-environment.json').exists())
        self.assertEqual(Project.load(p.directory).data['state'], 'ready')
        self.assertTrue(self.events()[-1]['success'])

    def test_failed_environment_cleanup_reports_error_and_blocks_next_build(self):
        self.prepare()
        p = self.project
        worker = self.worker('customize')

        def guest_changes_dns(*args, **kwargs):
            destination = p.rootfs / 'etc/resolv.conf'
            destination.unlink()
            destination.mkdir()

        worker.guest = mock.Mock(side_effect=guest_changes_dns)
        self.assertEqual(self.execute(worker), 1)
        self.assertEqual(Project.load(p.directory).data['state'], 'error')
        self.assertEqual((p.directory / '.backup-etc-resolv.conf').read_text(), 'original guest DNS')
        self.assertTrue((p.directory / '.guest-environment.json').is_file())
        self.assertFalse(self.events()[-1]['success'])
        build = self.worker('build')
        build.run = mock.Mock()
        build.compress_tree = mock.Mock()
        self.assertEqual(self.execute(build), 1)
        build.run.assert_not_called()
        build.compress_tree.assert_not_called()
        self.assertFalse(self.events()[-1]['success'])


if __name__ == '__main__':
    unittest.main()
