#!/usr/bin/python3 -I
"""Render or verify the whitecube boot theme (GRUB + Plymouth) in an output directory.

Same look as the desktop: black (000000), white isometric cubes, white ink.
Usage: gen-boot-theme.py OUT_DIR [WIDTH HEIGHT] [--seed N] [--scale N] [--verify] [--json]
Writes OUT_DIR/grub/*, OUT_DIR/plymouth/*. Needs Pillow to render; --verify is pure stdlib.
"""
import argparse
import json
import os
import random
import re
import struct
import sys
from pathlib import Path

VERSION = "0.13.0"
BG = (0, 0, 0)
INK = (255, 255, 255)
TOP, SIDE, FRONT = (230, 236, 248), (150, 160, 182), (190, 198, 216)  # cube_top / cube_side / front
SS = 4  # supersampling for anti-aliased cubes

REQUIRED_GRUB = (
    "background.png",
    *(f"select_{part}.png" for part in ("c", "n", "s", "e", "w", "ne", "nw", "se", "sw")),
    *(f"inbox_{part}.png" for part in ("c", "n", "s", "e", "w", "ne", "nw", "se", "sw")),
    "progress_bar_c.png",
    "progress_highlight_c.png",
)
REQUIRED_PLYMOUTH = (
    "logo.png",
    "cell-off.png",
    "cell-on.png",
    "blank.png",
    "entry.png",
    "bullet.png",
    "lock.png",
    "box.png",
)


def _require_pillow():
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow (python3-pil) is required to render boot theme images") from exc
    return Image, ImageDraw


def cube(size, alpha=255, lit=1.0):
    """Isometric-ish cube like cube_icon() in cube-status.c, on a transparent square."""
    Image, ImageDraw = _require_pillow()
    big = size * SS
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    o = big * 0.24
    f = big - o

    def col(c):
        return tuple(int(v * lit) for v in c) + (alpha,)

    d.polygon([(0, o), (o, 0), (o + f, 0), (f, o)], fill=col(TOP))
    d.polygon([(f, o), (f + o, 0), (f + o, f), (f, o + f)], fill=col(SIDE))
    d.rectangle([0, o, f, o + f], fill=col(FRONT))
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    return im.resize((size, size), resample)


def space(w, h, seed=7):
    """Background: black, faint stars, and a glider walking in the corner (the cube-ca wallpaper)."""
    Image, _ = _require_pillow()
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), BG)
    px = im.load()
    for _ in range(w * h // 1800):
        x, y = rnd.randrange(w), rnd.randrange(h)
        v = rnd.randrange(30, 90)
        px[x, y] = (v, v, v)
    cell = max(10, w // 96)
    c = cube(cell)
    glider = [(1, 0), (2, 1), (0, 2), (1, 2), (2, 2)]
    block = [(0, 0), (1, 0), (0, 1), (1, 1)]
    for pat, gx, gy in ((glider, 6, h // cell - 8), (block, w // cell - 9, 5), (glider, w // cell - 14, h // cell - 6)):
        for x, y in pat:
            im.paste(c, ((gx + x) * cell, (gy + y) * cell), c)
    return im


def solid(path, color, size=(8, 8)):
    Image, _ = _require_pillow()
    Image.new("RGBA", size, color).save(path)


def frame_tile(path, part, fill, edge, size=4):
    """Draw a 9-slice edge/corner tile with a crisp 1px outer hairline."""
    Image, _ = _require_pillow()
    im = Image.new("RGBA", (size, size), fill)
    if part == "c":
        im.save(path)
        return
    px = im.load()
    if "n" in part:
        for x in range(size):
            px[x, 0] = edge
    if "s" in part:
        for x in range(size):
            px[x, size - 1] = edge
    if "w" in part:
        for y in range(size):
            px[0, y] = edge
    if "e" in part:
        for y in range(size):
            px[size - 1, y] = edge
    im.save(path)


def png_dimensions(path):
    """Read (width, height) from a PNG header without requiring Pillow."""
    data = Path(path).read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"Invalid PNG header: {path}")
    return struct.unpack(">II", data[16:24])


def verify_output(out_dir, boot_dir=None):
    """Validate generated GRUB and Plymouth assets and cross-check script image references."""
    out = Path(out_dir)
    boot_dir = Path(boot_dir) if boot_dir else Path(__file__).resolve().parent
    missing = []
    sizes = {"grub": {}, "plymouth": {}}
    for sub, names in (("grub", REQUIRED_GRUB), ("plymouth", REQUIRED_PLYMOUTH)):
        for name in names:
            target = out / sub / name
            if not target.is_file():
                missing.append(f"{sub}/{name}")
                continue
            w, h = png_dimensions(target)
            if w < 1 or h < 1:
                raise ValueError(f"Empty PNG image {target}: {w}x{h}")
            sizes[sub][name] = [w, h]
    script = boot_dir / "whitecube.script"
    if script.is_file():
        referenced = sorted(set(re.findall(r'Image\(\s*"([^"]+\.png)"\s*\)', script.read_text())))
        for name in referenced:
            if name not in sizes["plymouth"]:
                missing.append(f"plymouth/{name} (referenced by whitecube.script)")
    if missing:
        raise FileNotFoundError("Missing boot theme asset(s): " + ", ".join(missing))
    return sizes


def render_theme(out_dir, width=1920, height=1080, seed=7, scale=1):
    if not (320 <= width <= 7680) or not (240 <= height <= 4320):
        raise ValueError(f"Dimensions out of supported bounds (320..7680 x 240..4320): {width}x{height}")
    if not (1 <= scale <= 4):
        raise ValueError(f"Scale must be between 1 and 4, got {scale}")
    Image, ImageDraw = _require_pillow()
    out = Path(out_dir)
    g = out / "grub"
    p = out / "plymouth"
    g.mkdir(parents=True, exist_ok=True)
    p.mkdir(parents=True, exist_ok=True)

    # ---- GRUB: background, selection bar, terminal box, progress ----
    space(width, height, seed=seed).save(g / "background.png")
    bar = (255, 255, 255, 34)
    edge = (255, 255, 255, 140)
    inbox_fill = (0, 0, 0, 235)
    inbox_edge = (255, 255, 255, 96)
    for part in ("c", "n", "s", "e", "w", "ne", "nw", "se", "sw"):
        frame_tile(g / f"select_{part}.png", part, bar, edge, size=4)
        frame_tile(g / f"inbox_{part}.png", part, inbox_fill, inbox_edge, size=4)
    solid(g / "progress_bar_c.png", (42, 42, 42, 255), (4, 8))
    solid(g / "progress_highlight_c.png", INK + (255,), (4, 8))

    # ---- Plymouth: logo cube, boot cubes (dim/lit), passphrase dialog ----
    cube(160 * scale).save(p / "logo.png")
    cube(28 * scale, 255, 0.28).save(p / "cell-off.png")
    cube(28 * scale).save(p / "cell-on.png")
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(p / "blank.png")
    ew, eh = 380 * scale, 44 * scale
    entry = Image.new("RGBA", (ew, eh), (0, 0, 0, 255))
    ImageDraw.Draw(entry).rectangle([0, 0, ew - 1, eh - 1], outline=INK + (255,), width=max(2, 2 * scale))
    entry.save(p / "entry.png")
    cube(20 * scale).save(p / "bullet.png")
    cube(36 * scale).save(p / "lock.png")
    Image.new("RGBA", (ew + 90 * scale, 120 * scale), (0, 0, 0, 0)).save(p / "box.png")

    return verify_output(out)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Render or verify the Whitecube GRUB + Plymouth boot theme.")
    parser.add_argument("out_dir", help="Target directory containing or receiving grub/ and plymouth/")
    parser.add_argument("width", nargs="?", type=int, default=1920, help="Background image width (default: 1920)")
    parser.add_argument("height", nargs="?", type=int, default=1080, help="Background image height (default: 1080)")
    parser.add_argument("--seed", type=int, default=7, help="Deterministic starfield RNG seed (default: 7)")
    parser.add_argument("--scale", type=int, default=1, help="Integer HiDPI scale 1..4 for Plymouth sprites (default: 1)")
    parser.add_argument("--verify", action="store_true", help="Verify existing rendered assets without writing")
    parser.add_argument("--json", action="store_true", help="Emit JSON asset summary")
    parser.add_argument("-v", "--version", action="version", version=f"whitecube-boot-theme {VERSION}")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.verify:
        assets = verify_output(args.out_dir)
        bg_w, bg_h = assets["grub"]["background.png"]
        payload = {"ok": True, "mode": "verify", "out_dir": str(args.out_dir), "width": bg_w, "height": bg_h, "assets": assets}
    else:
        assets = render_theme(args.out_dir, width=args.width, height=args.height, seed=args.seed, scale=args.scale)
        payload = {
            "ok": True,
            "mode": "render",
            "out_dir": str(args.out_dir),
            "width": args.width,
            "height": args.height,
            "scale": args.scale,
            "seed": args.seed,
            "assets": assets,
        }
    if args.json:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, FileNotFoundError, OSError) as exc:
        print(f"gen-boot-theme: {exc}", file=sys.stderr)
        sys.exit(1)
