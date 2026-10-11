import hashlib
import sys
import tempfile
import unittest
import os
import threading
from unittest.mock import patch
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


class TestLastFolder(Base):
    def test_pickers_reopen_where_the_last_pick_happened(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.d / "state")}):
            self.assertIsNone(ct.last_folder())
            ct.remember_folder(str(self.d))
            self.assertEqual(ct.last_folder(), str(self.d))
            saved = self.d / "state" / "cube-apps" / "last-folder"
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            ct.remember_folder(str(self.d / "missing"))       # ignored
            self.assertEqual(ct.last_folder(), str(self.d))
            saved.write_text("relative/path\n")              # never trusted
            self.assertIsNone(ct.last_folder())


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


class ImprovementRegressions(Base):
    def test_output_is_atomic_and_never_overwrites(self):
        target = self.d / 'result.txt'
        target.write_text('original')
        def failed(path):
            path.write_text('partial')
            raise OSError('disk full')
        with self.assertRaises(OSError):
            ct.write_output(target, failed)
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(list(self.d.iterdir()), [target])
        result = ct.write_output(target, lambda p: p.write_text('complete'))
        self.assertEqual(result.read_text(), 'complete')
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)

    def test_concurrent_outputs_have_distinct_names(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda i: ct.write_output(self.d / 'x.txt', lambda p: p.write_text(str(i))), range(8)))
        self.assertEqual(len(set(results)), 8)
        self.assertEqual({p.read_text() for p in results}, {str(i) for i in range(8)})

    def test_dangling_symlink_counts_as_an_existing_output(self):
        path = self.d / 'output.pdf'
        path.symlink_to(self.d / 'missing')
        self.assertEqual(ct.unique_path(path).name, 'output-2.pdf')
        actual = ct.write_output(path, lambda p: p.write_text('safe'))
        self.assertTrue(path.is_symlink())
        self.assertFalse((self.d / 'missing').exists())
        self.assertEqual(actual.name, 'output-2.pdf')

    def test_hash_empty_file_and_cancellation(self):
        path = self.d / 'empty'; path.touch()
        progress = []
        self.assertEqual(ch.hash_file(path, progress=progress.append), hashlib.sha256(b'').hexdigest())
        self.assertEqual(progress[-1], 1)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(InterruptedError):
            ch.hash_file(path, cancel=cancel)

    def test_hash_rejects_fifo_without_blocking(self):
        path = self.d / 'fifo'; os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            ch.hash_file(path)

    def test_hash_reports_source_changes(self):
        path = self.d / 'file'; path.write_bytes(b'a' * (2 << 20))
        def progress(_fraction):
            with path.open('ab') as stream:
                stream.write(b'change')
        with self.assertRaisesRegex(ValueError, 'changed'):
            ch.hash_file(path, progress=progress)

    def test_bsd_checksum_paste(self):
        self.assertEqual(ch.clean_expected('SHA256 (an input file.iso) = ' + 'AB' * 32), 'ab' * 32)

    def test_new_rename_rules_and_natural_order(self):
        paths = [self.d / n for n in ('image10.JPG', 'image2.JPG', 'image1.JPG')]
        for path in paths: path.touch()
        rows = cr.plan(paths, cr.Opts(order=3, number=True, extension_case=1))
        self.assertEqual([p.name for p, _, _ in rows], ['image1.JPG', 'image2.JPG', 'image10.JPG'])
        self.assertEqual(rows[0][1].name, 'image1-01.jpg')
        options = cr.Opts(find='a', replace='z', first_only=True, trim=True, collapse_spaces=True)
        self.assertEqual(cr.new_stem('  aa   bb  ', 0, options), 'za bb')

    def test_rename_rejects_control_and_oversized_names(self):
        path = self.d / 'a.txt'; path.touch()
        for suffix in ('\n', '\t', '\x7f', 'é' * 130):
            self.assertEqual(cr.plan([path], cr.Opts(suffix=suffix))[0][2], 'invalid name')
        self.assertEqual(cr.plan([path], cr.Opts(digits=100000))[0][2], 'invalid numbering')

    def test_rename_rejects_duplicate_sources_and_conflicted_preview(self):
        path = self.d / 'a'; path.write_text('original')
        with self.assertRaises(ValueError):
            cr.apply([(path, self.d / 'b', None), (path, self.d / 'c', None)])
        with self.assertRaises(ValueError):
            cr.apply([(path, self.d / 'b', 'conflict')])
        self.assertEqual(path.read_text(), 'original')

    def test_rename_rolls_back_both_staging_and_commit_failures(self):
        for fail_at in (2, 5, 6):
            paths = [self.d / n for n in ('a', 'b', 'c')]
            for p in paths: p.write_text(p.name)
            rows = [(paths[0], paths[1], None), (paths[1], paths[2], None), (paths[2], self.d / 'd', None)]
            real = cr.move_no_replace
            calls = 0
            def failing(source, destination):
                nonlocal calls
                calls += 1
                if calls == fail_at: raise OSError('injected failure')
                real(source, destination)
            with patch.object(cr, 'move_no_replace', failing), self.assertRaises(OSError):
                cr.apply(rows)
            self.assertEqual({p.name:p.read_text() for p in self.d.iterdir()}, {'a':'a','b':'b','c':'c'})

    def test_rename_will_not_overwrite_an_undo_conflict(self):
        current, original = self.d / 'renamed', self.d / 'original'
        current.write_text('renamed data'); original.write_text('new data')
        with self.assertRaises(FileExistsError): cr.apply([(current, original, None)])
        self.assertEqual(current.read_text(), 'renamed data')
        self.assertEqual(original.read_text(), 'new data')

    def test_pdf_extended_ranges_and_invalid_split_have_no_output(self):
        self.assertEqual(cp.parse_ranges('-3, 5–6, 8-', 10), [(1,3),(5,6),(8,10)])
        self.assertEqual(cp.parse_ranges('all', 10), [(1,10)])
        for text in ('1,,3', ',1', '1,'):
            with self.assertRaises(ValueError): cp.parse_ranges(text, 10)
        src = make_pdf(self.d / 'a.pdf', 3)
        folder = self.d / 'out'
        with self.assertRaises(ValueError): cp.split(src, [(1,1), (3,4)], folder)
        self.assertFalse(folder.exists())
        with self.assertRaises(ValueError): cp.merge([], self.d / 'empty.pdf')

    def test_pdf_rotation_only_once_per_selected_page(self):
        src = make_pdf(self.d / 'a.pdf', 3)
        out = cp.rotate(src, 90, '1-2, 2', self.d / 'rotated.pdf')
        with pikepdf.open(out) as pdf:
            self.assertEqual([int(page.obj.get('/Rotate', 0)) for page in pdf.pages], [90,90,0])
        with pikepdf.open(src) as pdf:
            self.assertEqual(int(pdf.pages[0].obj.get('/Rotate', 0)), 0)

    def test_pdf_lock_preserves_dotted_names_and_existing_files(self):
        src = make_pdf(self.d / 'report.v1.pdf', 1)
        dest = cl.output_path(src, None)
        self.assertEqual(dest.name, 'report.v1-locked.pdf')
        dest.write_text('existing')
        actual = cl.encrypt_pdf(src, dest, 'password', '', True, True, False)
        self.assertEqual(dest.read_text(), 'existing')
        cl.verify_locked(src, actual, 'password')
        self.assertNotEqual(actual, dest)
        for password in ('', 'é' * 64):
            with self.assertRaises(ValueError): cl.encrypt_pdf(src, self.d / 'bad.pdf', password, '', True, True, False)


if __name__ == "__main__":
    unittest.main()
