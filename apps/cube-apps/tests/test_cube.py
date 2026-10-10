import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pikepdf  # noqa: E402

import cube_hash as ch  # noqa: E402
import cube_lock as cl  # noqa: E402
import cube_pdf as cp  # noqa: E402
import cube_rename as cr  # noqa: E402
import cube_theme as ct  # noqa: E402


def make_pdf(path: Path, pages: int) -> Path:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page()
    pdf.save(path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()


class TestTheme(Base):
    def test_unique_path(self):
        a = self.d / "x.pdf"
        self.assertEqual(ct.unique_path(a), a)
        a.write_text("1")
        self.assertEqual(ct.unique_path(a).name, "x-2.pdf")
        (self.d / "x-2.pdf").write_text("1")
        self.assertEqual(ct.unique_path(a).name, "x-3.pdf")


class TestPdf(Base):
    def test_ranges(self):
        self.assertEqual(cp.parse_ranges("1-3, 5, 8-", 10), [(1, 3), (5, 5), (8, 10)])
        for bad in ("0", "3-2", "11", "a", "", "1-x"):
            with self.assertRaises(ValueError, msg=bad):
                cp.parse_ranges(bad, 10)

    def test_merge_order_and_no_overwrite(self):
        a, b = make_pdf(self.d / "a.pdf", 2), make_pdf(self.d / "b.pdf", 3)
        out = cp.merge([b, a], self.d / "m.pdf")
        with pikepdf.open(out) as pdf:
            self.assertEqual(len(pdf.pages), 5)
        self.assertEqual(cp.merge([a, b], self.d / "m.pdf").name, "m-2.pdf")

    def test_merge_refuses_locked(self):
        a = make_pdf(self.d / "a.pdf", 1)
        locked = self.d / "l.pdf"
        with pikepdf.open(a) as p:
            p.save(locked, encryption=pikepdf.Encryption(user="x", owner="x", R=6))
        with self.assertRaisesRegex(ValueError, "password-protected"):
            cp.merge([a, locked], self.d / "m.pdf")

    def test_split(self):
        src = make_pdf(self.d / "s.pdf", 6)
        made = cp.split(src, [(1, 2), (5, 5)], self.d / "out")
        self.assertEqual([m.name for m in made], ["s-p1-2.pdf", "s-p5.pdf"])
        counts = []
        for m in made:
            with pikepdf.open(m) as pdf:
                counts.append(len(pdf.pages))
        self.assertEqual(counts, [2, 1])

    def test_lock_unlock_roundtrip(self):
        src = make_pdf(self.d / "a.pdf", 2)
        locked = self.d / "a-locked.pdf"
        cl.encrypt_pdf(src, locked, "pw", "", True, True, False)
        with self.assertRaises(pikepdf.PasswordError):
            pikepdf.open(locked)
        with pikepdf.open(locked, password="pw") as pdf:
            self.assertEqual(pdf.encryption.bits, 256)
        with self.assertRaisesRegex(ValueError, "wrong password"):
            cp.unlock(locked, "nope", self.d / "u.pdf")
        out = cp.unlock(locked, "pw", self.d / "u.pdf")
        with pikepdf.open(out) as pdf:  # opens with no password
            self.assertFalse(pdf.is_encrypted)
            self.assertEqual(len(pdf.pages), 2)

    def test_remove_original_only_after_verified(self):
        src = make_pdf(self.d / "a.pdf", 3)
        trashed = []
        note = cl.lock_one(src, self.d / "a-locked.pdf", "pw", "", True, True, False,
                           remove_original=True, trash=trashed.append)
        self.assertEqual((note, trashed), ("", [src]))
        # default keeps the original
        src2 = make_pdf(self.d / "b.pdf", 1)
        kept = []
        cl.lock_one(src2, self.d / "b-locked.pdf", "pw", "", True, True, False, trash=kept.append)
        self.assertEqual(kept, [])

    def test_original_kept_when_trash_or_verify_fails(self):
        src = make_pdf(self.d / "a.pdf", 2)

        def boom(_p):
            raise OSError("no trash here")
        note = cl.lock_one(src, self.d / "a-locked.pdf", "pw", "", True, True, False,
                           remove_original=True, trash=boom)
        self.assertIn("original kept", note)
        self.assertTrue(src.exists())
        # a locked copy that does not match the password must never free the original
        src2 = make_pdf(self.d / "b.pdf", 2)
        dest = self.d / "b-locked.pdf"
        cl.encrypt_pdf(src2, dest, "right", "", True, True, False)
        with self.assertRaises(Exception):
            cl.verify_locked(src2, dest, "wrong")
        # an unencrypted "locked" copy fails verification
        plain = make_pdf(self.d / "plain.pdf", 2)
        with self.assertRaisesRegex(ValueError, "without a password"):
            cl.verify_locked(src2, plain, "x")

    def test_lock_output_name(self):
        src = self.d / "a.pdf"
        src.write_text("x")
        self.assertEqual(cl.output_path(src, None).name, "a-locked.pdf")


class TestHash(Base):
    def test_hash_matches_hashlib(self):
        f = self.d / "f.bin"
        f.write_bytes(b"cube" * 400000)
        for label, algo in ch.ALGOS:
            self.assertEqual(ch.hash_file(f, algo), hashlib.new(algo, f.read_bytes()).hexdigest(), label)

    def test_progress_reaches_one(self):
        f = self.d / "f.bin"
        f.write_bytes(b"x" * (3 << 20))
        seen = []
        ch.hash_file(f, "md5", seen.append)
        self.assertEqual(seen[-1], 1.0)

    def test_clean_expected(self):
        h = "ab" * 32
        for text in (h, h.upper(), f"  {h}  file.iso", f"sha256: {h}"):
            self.assertEqual(ch.clean_expected(text), h)
        self.assertEqual(ch.clean_expected("  "), "")


class TestRename(Base):
    def touch(self, *names):
        out = []
        for n in names:
            p = self.d / n
            p.write_text(n)
            out.append(p)
        return out

    def test_rules(self):
        ps = self.touch("IMG one.JPG", "IMG two.JPG")
        o = cr.Opts(find="IMG ", replace="trip-", number=True, start=7, digits=3, case=1)
        rows = cr.plan(ps, o)
        self.assertEqual([r[1].name for r in rows], ["trip-one-007.JPG", "trip-two-008.JPG"])
        self.assertTrue(all(r[2] is None for r in rows))

    def test_regex_and_bad_pattern(self):
        ps = self.touch("a1.txt")
        self.assertEqual(cr.plan(ps, cr.Opts(find=r"\d", replace="#", regex=True))[0][1].name, "a#.txt")
        self.assertIn("bad pattern", cr.plan(ps, cr.Opts(find="(", regex=True))[0][2])

    def test_conflicts(self):
        a, b, _z = self.touch("a.txt", "b.txt", "z.txt")
        # a becomes b.txt while b keeps its name: two files, one target
        rows = cr.plan([a, b], cr.Opts(find="a", replace="b"))
        self.assertEqual([r[2] for r in rows], ["duplicate name", "duplicate name"])
        # a would land on a file outside the batch
        self.assertEqual(cr.plan([a], cr.Opts(find="a", replace="z"))[0][2], "already exists")
        # distinct targets are fine
        rows = cr.plan([a, b], cr.Opts(suffix="x"))
        self.assertEqual([r[1].name for r in rows], ["ax.txt", "bx.txt"])
        self.assertTrue(all(r[2] is None for r in rows))
        # numbering that collapses names is caught
        c1, c2 = self.touch("c1.txt", "c2.txt")
        rows = cr.plan([c1, c2], cr.Opts(find=r"\d", replace="", regex=True))
        self.assertEqual([r[2] for r in rows], ["duplicate name", "duplicate name"])

    def test_invalid_names(self):
        ps = self.touch("a.txt")
        self.assertEqual(cr.plan(ps, cr.Opts(find="a", replace="x/y"))[0][2], "invalid name")

    def test_apply_swap_and_undo(self):
        a, b = self.touch("a.txt", "b.txt")
        a.write_text("A")
        b.write_text("B")
        rows = [(a, b, None), (b, a, None)]  # a swap needs the two-phase rename
        undo = cr.apply(rows)
        self.assertEqual((self.d / "a.txt").read_text(), "B")
        self.assertEqual((self.d / "b.txt").read_text(), "A")
        cr.apply([(cur, orig, None) for cur, orig in undo])
        self.assertEqual((self.d / "a.txt").read_text(), "A")
        self.assertEqual((self.d / "b.txt").read_text(), "B")
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ["a.txt", "b.txt"])

    def test_apply_chain(self):
        a, b = self.touch("a.txt", "b.txt")
        cr.apply([(a, self.d / "b.txt", None), (b, self.d / "c.txt", None)])
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ["b.txt", "c.txt"])
        self.assertEqual((self.d / "b.txt").read_text(), "a.txt")


if __name__ == "__main__":
    unittest.main()
