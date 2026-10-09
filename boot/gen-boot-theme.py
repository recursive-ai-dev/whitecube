#!/usr/bin/env python3
"""Render the whitecube boot theme (GRUB + Plymouth) into an output directory.

Same look as the desktop: black (000000), white isometric cubes, white ink.
Usage: gen-boot-theme.py OUT_DIR [WIDTH HEIGHT]
Writes OUT_DIR/grub/*, OUT_DIR/plymouth/*. Needs Pillow; grub fonts are made by install-boot.sh.
"""
import os, random, sys
from PIL import Image, ImageDraw

BG = (0, 0, 0)
INK = (255, 255, 255)
TOP, SIDE, FRONT = (230, 236, 248), (150, 160, 182), (190, 198, 216)  # cube_top / cube_side / front
SS = 4  # supersampling for anti-aliased cubes


def cube(size, alpha=255, lit=1.0):
    """Isometric-ish cube like cube_icon() in cube-status.c, on a transparent square."""
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
    return im.resize((size, size), Image.LANCZOS)


def space(w, h, seed=7):
    """Background: black, faint stars, and a glider walking in the corner (the cube-ca wallpaper)."""
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
    Image.new("RGBA", size, color).save(path)


def main():
    out = sys.argv[1]
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 1920
    h = int(sys.argv[3]) if len(sys.argv) > 3 else 1080
    g = os.path.join(out, "grub")
    p = os.path.join(out, "plymouth")
    os.makedirs(g, exist_ok=True)
    os.makedirs(p, exist_ok=True)

    # ---- GRUB: background, selection bar, terminal box, progress ----
    space(w, h).save(os.path.join(g, "background.png"))
    bar = (255, 255, 255, 34)
    edge = (255, 255, 255, 120)
    for name in ("c", "n", "s", "e", "w", "ne", "nw", "se", "sw"):
        # a faint white fill with a crisp left/right/top/bottom hairline on the edge pieces
        px = {"c": bar, "n": edge, "s": edge, "e": edge, "w": edge}.get(name, edge)
        solid(os.path.join(g, f"select_{name}.png"), px, (4, 4) if name in ("ne", "nw", "se", "sw") else (4, 4))
        solid(os.path.join(g, f"inbox_{name}.png"), (0, 0, 0, 235), (4, 4))
    solid(os.path.join(g, "progress_bar_c.png"), (42, 42, 42, 255), (4, 8))
    solid(os.path.join(g, "progress_highlight_c.png"), INK + (255,), (4, 8))

    # ---- Plymouth: logo cube, boot cubes (dim/lit), passphrase dialog ----
    cube(160).save(os.path.join(p, "logo.png"))
    cube(28, 255, 0.28).save(os.path.join(p, "cell-off.png"))
    cube(28).save(os.path.join(p, "cell-on.png"))
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(os.path.join(p, "blank.png"))
    # passphrase entry: hairline box, a small cube as bullet
    ew, eh = 380, 44
    entry = Image.new("RGBA", (ew, eh), (0, 0, 0, 255))
    ImageDraw.Draw(entry).rectangle([0, 0, ew - 1, eh - 1], outline=INK + (255,), width=2)
    entry.save(os.path.join(p, "entry.png"))
    cube(20).save(os.path.join(p, "bullet.png"))
    cube(36).save(os.path.join(p, "lock.png"))
    Image.new("RGBA", (ew + 90, 120), (0, 0, 0, 0)).save(os.path.join(p, "box.png"))


if __name__ == "__main__":
    main()
