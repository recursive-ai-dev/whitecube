#!/usr/bin/python3 -I
"""Launch Cube Chrootables, Whitecube's bundled Qt workspace manager."""
from pathlib import Path
import sys

# Isolated Python still needs this private package directory on its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from chrootables.app import main
except ImportError as error:
    print(f"Could not start Cube Chrootables: {error}\n"
          "Install the desktop runtime: sudo apt install python3-pyside6.qtwidgets qt6-wayland\n"
          "On Arch Linux: sudo pacman -S pyside6 qt6-wayland", file=sys.stderr)
    raise SystemExit(1)
except RuntimeError as error:
    print(f"Could not start Cube Chrootables: {error}\n"
          "Run whitecube-chrootable-manager --doctor to check this installation.", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(main())
