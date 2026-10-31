"""Save window captures atomically and report filesystem or encoder failures."""
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile


def save_screenshot(window, destination):
    """Commit a complete PNG, keeping an existing destination intact on failure."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = QSaveFile(str(destination))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(f"Cannot open screenshot {destination}: {output.errorString()}")
    try:
        if not window.grab().save(output, "PNG"):
            raise OSError(f"Could not encode screenshot {destination} as PNG.")
        if not output.commit():
            raise OSError(f"Cannot save screenshot {destination}: {output.errorString()}")
    except BaseException:
        output.cancelWriting()
        raise
