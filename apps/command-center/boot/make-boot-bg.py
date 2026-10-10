#!/usr/bin/env python3
"""Build the whitecube boot background: fetch (or load) a wallpaper, detect a watermark,
zoom the canvas to 123% anchored away from it, then darken to the whitecube background.

Mirrors watermark.ts. Free API: Lorem Picsum (no key).
Usage: make-boot-bg.py --out bg.png [--in photo.jpg | --fetch] [--size 1920x1080]
"""
import argparse, io, json, random, sys, urllib.request
from PIL import Image

ZOOM = 1.23
EDGE = 60
BG = (4, 7, 13)


def luma(px):
    r, g, b = px[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def edge_map(img):
    w, h = img.size
    L = [luma(p) for p in img.getdata()]
    e = [0] * (w * h)
    for y in range(h - 1):
        for x in range(w - 1):
            i = y * w + x
            if abs(L[i] - L[i + 1]) > EDGE or abs(L[i] - L[i + w]) > EDGE:
                e[i] = 1
    return e


def density(e, w, x0, y0, x1, y1):
    n = sum(e[y * w + x] for y in range(y0, y1) for x in range(x0, x1))
    return n / max(1, (x1 - x0) * (y1 - y0))


def detect_watermark(img):
    """Returns (flagged, score, side). Same regions and thresholds as watermark.ts."""
    w, h = img.size
    e = edge_map(img)
    cw, ch = int(w * 0.3), int(h * 0.14)
    centre = density(e, w, int(w * 0.3), int(h * 0.3), int(w * 0.7), int(h * 0.7))
    regions = [
        ("bottom-left", density(e, w, 0, h - ch, cw, h)),
        ("bottom-right", density(e, w, w - cw, h - ch, w, h)),
        ("bottom", density(e, w, (w - cw) // 2, h - ch, (w + cw) // 2, h)),
        ("top-left", density(e, w, 0, 0, cw, ch)),
        ("top-right", density(e, w, w - cw, 0, w, ch)),
    ]
    side, best = max(regions, key=lambda r: r[1])
    score = best / (centre + 0.005)
    flagged = best > 0.08 and score > 2.5
    return flagged, score, side if flagged else None


def cover(img, dw, dh, zoom=1.0, side=None):
    sw, sh = img.size
    scale = max(dw / sw, dh / sh) * zoom
    vw, vh = dw / scale, dh / scale
    ax = 1 if side in ("bottom-left", "top-left") else 0 if side in ("bottom-right", "top-right") else 0.5
    ay = 1 if side in ("top-left", "top-right") else 0 if side else 0.5
    sx, sy = (sw - vw) * ax, (sh - vh) * ay
    return img.resize((dw, dh), Image.LANCZOS, box=(sx, sy, sx + vw, sy + vh))


def fetch(size):
    api = f"https://picsum.photos/v2/list?page={random.randint(1, 10)}&limit=30"
    with urllib.request.urlopen(api, timeout=10) as r:
        items = [p for p in json.load(r) if p["width"] > p["height"]]
    pick = random.choice(items)
    url = f"https://picsum.photos/id/{int(pick['id'])}/{size[0]}/{size[1]}.jpg"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = r.read(8 * 1024 * 1024)
    return Image.open(io.BytesIO(data)).convert("RGB"), f"picsum #{pick['id']}"


def build(img, size):
    probe = img.resize((320, max(1, round(320 * img.height / img.width))))
    flagged, score, side = detect_watermark(probe)
    out = cover(img, size[0], size[1], ZOOM if flagged else 1.0, side)
    out = Image.blend(out, Image.new("RGB", size, BG), 0.7)  # smoked glass, same as the app canvas
    return out, flagged, side, score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--in", dest="src")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--size", default="1920x1080")
    a = ap.parse_args()
    size = tuple(int(v) for v in a.size.lower().split("x"))
    if a.src:
        img, label = Image.open(a.src).convert("RGB"), a.src
    elif a.fetch:
        img, label = fetch(size)
    else:
        ap.error("need --in or --fetch")
    out, flagged, side, score = build(img, size)
    out.save(a.out)
    print(f"{label}: watermark={'yes (' + side + ')' if flagged else 'no'} score={score:.1f} zoom={ZOOM if flagged else 1.0} -> {a.out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # network or decode failure: caller keeps the previous background
        print(f"make-boot-bg: {e}", file=sys.stderr)
        sys.exit(1)
