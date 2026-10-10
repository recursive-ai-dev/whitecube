import importlib.util, os, tempfile, unittest
from PIL import Image

spec = importlib.util.spec_from_file_location("mbg", os.path.join(os.path.dirname(__file__), "make-boot-bg.py"))
mbg = importlib.util.module_from_spec(spec); spec.loader.exec_module(mbg)


def gradient(w, h, stamp=None, cell=1):
    img = Image.new("RGB", (w, h))
    img.putdata([(int(90 + 60 * (i % w) / w),) * 3 for i in range(w * h)])
    if stamp:
        x0, y0, x1, y1 = stamp
        for y in range(y0, y1):
            for x in range(x0, x1):
                img.putpixel((x, y), (255,) * 3 if ((x // cell) + (y // cell)) % 2 == 0 else (0,) * 3)
    return img


class Bg(unittest.TestCase):
    def test_clean_not_flagged(self):
        self.assertFalse(mbg.detect_watermark(gradient(320, 180))[0])

    def test_corner_stamp_flagged(self):
        flagged, _, side = mbg.detect_watermark(gradient(320, 180, (224, 155, 320, 180), cell=2))
        self.assertTrue(flagged); self.assertEqual(side, "bottom-right")

    def test_zoom_removes_watermark_band(self):
        img = gradient(1920, 1080, (1344, 930, 1920, 1080), cell=12)
        out, flagged, side, _ = mbg.build(img, (1920, 1080))
        self.assertTrue(flagged)
        # After 123% zoom anchored top-left the stamp region must be cropped out: bottom-right is smooth again.
        probe = out.resize((320, 180))
        self.assertFalse(mbg.detect_watermark(probe)[0])

    def test_cli_output_size(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "a.png"), os.path.join(d, "b.png")
            gradient(640, 480).save(src)
            os.system(f'python3 {os.path.join(os.path.dirname(__file__), "make-boot-bg.py")} --in {src} --out {dst} --size 800x450 >/dev/null')
            self.assertEqual(Image.open(dst).size, (800, 450))


if __name__ == "__main__":
    unittest.main()
