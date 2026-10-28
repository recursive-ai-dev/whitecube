#!/usr/bin/python3 -I
"""No real screen lock or suspend: exercise the protocol with disposable commands."""
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/whitecube-suspend'))


class SuspendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='whitecube-suspend-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.record = self.root / 'suspended'
        self.suspend = [sys.executable, '-c',
                        f'from pathlib import Path; Path({str(self.record)!r}).write_text("suspended")']

    def locker(self, body):
        path = self.root / 'locker.py'
        path.write_text('import os, sys, time\nfd = int(sys.argv[-1])\n' + body)
        return [sys.executable, str(path)]

    def test_suspends_only_after_acknowledgement(self):
        before = self.root / 'locked'
        locker = self.locker(f'from pathlib import Path\nPath({str(before)!r}).write_text("locked")\nos.write(fd, b"\\n")\n')
        self.suspend = [sys.executable, '-c', f'from pathlib import Path; assert Path({str(before)!r}).read_text() == "locked"; Path({str(self.record)!r}).touch()']
        MODULE['lock_then_suspend'](locker, self.suspend)
        self.assertTrue(self.record.exists())

    def test_clean_exit_without_acknowledgement_does_not_suspend(self):
        with self.assertRaisesRegex(MODULE['SuspendError'], 'Screen lock failed'):
            MODULE['lock_then_suspend'](self.locker(''), self.suspend)
        self.assertFalse(self.record.exists())

    def test_unsupported_ready_fd_or_lock_failure_does_not_suspend(self):
        with self.assertRaises(MODULE['SuspendError']):
            MODULE['lock_then_suspend'](self.locker('sys.stderr.write("locking unavailable\\n")\nsys.exit(2)\n'), self.suspend)
        self.assertFalse(self.record.exists())

    def test_failure_exit_even_with_acknowledgement_does_not_suspend(self):
        with self.assertRaisesRegex(MODULE['SuspendError'], 'failed after'):
            MODULE['lock_then_suspend'](self.locker('os.write(fd, b"\\n")\nsys.exit(2)\n'), self.suspend)
        self.assertFalse(self.record.exists())

    def test_timeout_does_not_suspend_or_kill_the_locker(self):
        # The child remains alive briefly so the test can observe it was not killed.
        survived = self.root / 'locker-survived'
        command = self.locker(f'time.sleep(.15)\nfrom pathlib import Path\nPath({str(survived)!r}).touch()\n')
        with self.assertRaisesRegex(MODULE['SuspendError'], 'timed out'):
            MODULE['lock_then_suspend'](command, self.suspend, timeout=.03)
        import time
        deadline = time.monotonic() + 2
        while not survived.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(survived.exists())
        self.assertFalse(self.record.exists())

    def test_denied_suspend_reports_that_session_stays_locked(self):
        with self.assertRaisesRegex(MODULE['SuspendError'], 'screen remains locked: permission denied'):
            MODULE['lock_then_suspend'](self.locker('os.write(fd, b"\\n")\n'),
                                       [sys.executable, '-c', 'import sys; sys.stderr.write("permission denied"); sys.exit(1)'])
        self.assertFalse(self.record.exists())

    def test_missing_locker_does_not_suspend(self):
        with self.assertRaisesRegex(MODULE['SuspendError'], 'Unable to lock'):
            MODULE['lock_then_suspend']([str(self.root / 'missing')], self.suspend)
        self.assertFalse(self.record.exists())


if __name__ == '__main__':
    unittest.main()
