#!/usr/bin/python3 -I
"""The Arch image must never gain an X server, and its scripts must stay valid."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
ARCH = ROOT / 'packaging/arch'


class ArchPackagingTest(unittest.TestCase):
    def audit(self, *files):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'usr/bin').mkdir(parents=True)
            for name in files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('')
            return subprocess.run(['bash', str(ARCH / 'audit-no-x.sh'), str(root)], capture_output=True, text=True)

    def test_clean_root_passes(self):
        result = self.audit('usr/bin/foot', 'var/lib/pacman/local/xorg-xauth-1.1-1/desc')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_any_x_server_or_capture_tool_fails(self):
        for name in ('usr/bin/Xorg', 'usr/bin/Xwayland', 'usr/bin/grim',
                     'var/lib/pacman/local/xorg-server-21.1.0-1/desc',
                     'var/lib/pacman/local/xorg-server-xwayland-24.1-1/desc'):
            with self.subTest(name):
                self.assertEqual(self.audit(name).returncode, 1)

    def test_missing_root_is_an_error_not_a_pass(self):
        result = subprocess.run(['bash', str(ARCH / 'audit-no-x.sh'), '/nonexistent'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_scripts_parse_and_image_lists_no_x_server(self):
        for name in ('PKGBUILD', 'build-iso.sh', 'build-in-container.sh', 'audit-no-x.sh'):
            subprocess.run(['bash', '-n', str(ARCH / name)], check=True)
        packages = (ARCH / 'packages.extra').read_text().split('\n')
        self.assertFalse([p for p in packages if p.startswith('xorg-server')])
        self.assertIn('xorg-server', (ARCH / 'PKGBUILD').read_text())


if __name__ == '__main__':
    unittest.main()
