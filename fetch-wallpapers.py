#!/usr/bin/env python3
"""Fetch white/light wallpapers via free image API (Lorem Picsum, no key).
Post-process to high-key white-cube aesthetic with PIL.
Usage: fetch-wallpapers.py [--count N] [--out DIR] [--width W] [--height H]
"""
import argparse, os, sys
try:
    import requests
    from PIL import Image, ImageEnhance
    from io import BytesIO
except ImportError as e:
    print(f"missing dep: {e} (apt install python3-requests python3-pil)", file=sys.stderr)
    sys.exit(1)

API = "https://picsum.photos/v2/list"

def fetch_list(count):
    r = requests.get(API, params={"page": 1, "limit": max(count * 2, 20)}, timeout=30)
    r.raise_for_status()
    return r.json()

def whiten(img):
    # high-key: boost brightness + blend toward white for cube look
    img = img.convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(1.25)
    white = Image.new("RGB", img.size, (255, 255, 255))
    img = Image.blend(img, white, 0.35)
    img = ImageEnhance.Contrast(img).enhance(0.9)
    return img

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--out", default="/usr/share/whitecube/wallpapers")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    try:
        items = fetch_list(a.count)
    except Exception as e:
        print(f"API list failed ({e}), falling back to direct picsum IDs", file=sys.stderr)
        items = [{"id": str(1015 + i)} for i in range(a.count)]
    got = 0
    for it in items:
        if got >= a.count:
            break
        pid = it.get("id", "0")
        author = it.get("author", "unknown").replace("/", "_").replace(" ", "_")
        url = f"https://picsum.photos/id/{pid}/{a.width}/{a.height}"
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content))
            img = whiten(img)
            fn = os.path.join(a.out, f"cube-{pid}-{author}.jpg")
            img.save(fn, "JPEG", quality=90)
            print(f"saved {fn} ({url})")
            got += 1
        except Exception as e:
            print(f"skip id {pid}: {e}", file=sys.stderr)
    # always ensure at least one procedural fallback exists
    if got == 0:
        img = Image.new("RGB", (a.width, a.height), (250, 250, 250))
        fn = os.path.join(a.out, "cube-fallback.jpg")
        img.save(fn, "JPEG", quality=90)
        print(f"saved fallback {fn}")
    print(f"done: {got} wallpapers in {a.out}")

if __name__ == "__main__":
    main()
