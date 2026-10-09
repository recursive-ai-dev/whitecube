#!/usr/bin/env python3
"""Generate 'infinite white cubes floating in endless space' wallpapers.
Free API: pollinations.ai (no key). Procedural PIL fallback (offline-safe).
Usage: gen-cube-space.py [--count 6] [--out DIR] [--width 1920] [--height 1080]
"""
import argparse, os, random, sys, urllib.parse
try:
    import requests
except ImportError:
    print("need python3-requests", file=sys.stderr); sys.exit(1)
try:
    from PIL import Image, ImageDraw, ImageEnhance
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

PROMPTS = [
    "infinite white cubes floating in endless white space, minimal 3d render, soft shadows, bright high-key",
    "endless field of glossy white cubes floating in bright void, minimal abstract, soft light",
    "isometric white cubes drifting in infinite luminous space, subtle grid horizon, minimal",
    "giant white cube monoliths floating in endless foggy white space, minimal architectural render",
    "tiny white cubes scattered across infinite white desert to horizon, minimal, soft shadows",
    "white wireframe cubes floating in bright endless space, delicate gray lines on white, minimal",
]

def whiten(img, amount=0.25):
    if not HAVE_PIL: return img
    img = img.convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(1.12)
    white = Image.new("RGB", img.size, (255, 255, 255))
    return Image.blend(img, white, amount)

def procedural_cube_space(w, h, seed):
    """Offline fallback: white void + perspective grid + isometric floating cubes."""
    rng = random.Random(seed)
    img = Image.new("RGB", (w, h), (250, 250, 250))
    d = ImageDraw.Draw(img)
    # endless-space perspective grid converging to center horizon
    cx, cy = w // 2, int(h * 0.42)
    for gx in range(0, w + 1, 64):
        d.line([(gx, h), (cx, cy)], fill=(225, 225, 225), width=1)
    for i in range(12):
        t = i / 11
        y = int(cy + (h - cy) * (t ** 2))
        d.line([(0, y), (w, y)], fill=(228, 228, 228), width=1)
    d.line([(0, cy), (w, cy)], fill=(210, 210, 210), width=2)
    # floating isometric cubes: far = small/pale, near = big/bright w/ border
    for _ in range(40):
        depth = rng.random()  # 0 far .. 1 near
        s = int(8 + depth * 110)
        x = rng.randint(0, w - s)
        y = rng.randint(0, h - s)
        shade = int(205 + depth * 50)
        face = (255, 255, 255)
        side = (shade - 12, shade - 12, shade - 12)
        top = (min(255, shade + 8),) * 3
        ox, oy = s // 3, s // 4  # iso offset
        pts_top = [(x, y), (x + ox, y - oy), (x + s + ox, y - oy), (x + s, y)]
        pts_side = [(x + s, y), (x + s + ox, y - oy), (x + s + ox, y + s - oy), (x + s, y + s)]
        for pts, col in ((pts_top, top), ([(x, y), (x + s, y), (x + s, y + s), (x, y + s)], face), (pts_side, side)):
            d.polygon(pts, fill=col, outline=(60, 60, 60) if depth > 0.75 else (150, 150, 150))
    return img

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--out", default="/usr/share/whitecube/wallpapers/cube-space")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    made = 0
    for i in range(a.count):
        prompt = PROMPTS[i % len(PROMPTS)]
        seed = 1000 + i * 77
        url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
               + f"?width={a.width}&height={a.height}&nologo=true&seed={seed}&model=flux")
        fn = os.path.join(a.out, f"cube-space-{i+1:02d}.jpg")
        try:
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if "image" not in ctype and len(r.content) < 20000:
                raise RuntimeError(f"unexpected response {ctype} {len(r.content)}b")
            if HAVE_PIL:
                from io import BytesIO
                img = Image.open(BytesIO(r.content))
                img = whiten(img)
                img.save(fn, "JPEG", quality=90)
            else:
                with open(fn, "wb") as f: f.write(r.content)
            print(f"saved {fn} (ai seed {seed})")
            made += 1
        except Exception as e:
            print(f"AI failed #{i+1} ({e}), procedural fallback", file=sys.stderr)
            if HAVE_PIL:
                procedural_cube_space(a.width, a.height, seed).save(fn, "JPEG", quality=90)
                print(f"saved {fn} (procedural)")
                made += 1
    print(f"done: {made}/{a.count} in {a.out}")

if __name__ == "__main__":
    main()
