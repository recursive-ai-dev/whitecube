"""Failed UI captures must not truncate previous captures or hang automation."""
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication
from chrootables.capture import save_screenshot


class ScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_capture_creates_complete_png_under_missing_parent(self):
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor("#74d2aa"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new/capture.png"
            save_screenshot(Mock(grab=Mock(return_value=pixmap)), path)
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_encoder_failure_keeps_previous_file_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.png"
            path.write_bytes(b"previous capture")
            window = Mock()
            window.grab.return_value.save.return_value = False
            with self.assertRaisesRegex(OSError, "encode"):
                save_screenshot(window, path)
            self.assertEqual(path.read_bytes(), b"previous capture")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_open_and_commit_failures_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            for phase in ("open", "commit"):
                with self.subTest(phase=phase), patch("chrootables.capture.QSaveFile") as factory:
                    output = factory.return_value
                    output.open.return_value = phase != "open"
                    output.commit.return_value = False
                    output.errorString.return_value = "simulated full filesystem"
                    window = Mock()
                    window.grab.return_value.save.return_value = True
                    with self.assertRaisesRegex(OSError, "simulated full filesystem"):
                        save_screenshot(window, Path(directory) / "capture.png")
                    if phase == "commit":
                        output.cancelWriting.assert_called_once()
                    else:
                        window.grab.assert_not_called()

    def test_parent_creation_failure_does_not_grab_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "regular-file"
            parent.write_bytes(b"unchanged")
            window = Mock()
            with self.assertRaises(OSError):
                save_screenshot(window, parent / "capture.png")
            window.grab.assert_not_called()
            self.assertEqual(parent.read_bytes(), b"unchanged")

    def test_cli_capture_failure_exits_with_diagnostic(self):
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            parent = fixture / "regular-file"
            parent.write_bytes(b"keep this file")
            result = subprocess.run(
                [str(root / "bin/cube-chrootables"), "--demo", "--screenshot", str(parent / "capture.png")],
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "XDG_CONFIG_HOME": str(fixture / "config")},
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Screenshot failed", result.stderr)
            self.assertEqual(parent.read_bytes(), b"keep this file")


if __name__ == "__main__":
    unittest.main()
