"""Terminal emulation, key mapping, and pty round-trip tests. No root required."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest

from chrootables.terminal import (
    BOLD, DIM, STRIKE, UNDERLINE,
    QT_KEY_BACKSPACE, QT_KEY_DELETE, QT_KEY_DOWN, QT_KEY_END,
    QT_KEY_ENTER, QT_KEY_HOME, QT_KEY_LEFT, QT_KEY_PAGEUP, QT_KEY_RIGHT,
    QT_KEY_TAB, QT_KEY_UP, PtyProcess, TerminalPane, TerminalWindow, VtScreen,
    key_to_bytes, MAX_CELL_CHARACTERS,
)


def pump(application, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        application.processEvents()
        time.sleep(0.005)
    return True


class VtScreenTests(unittest.TestCase):
    def test_repeated_combining_marks_are_bounded_without_moving_cursor(self):
        for base in ('e', '界'):
            with self.subTest(base=base):
                screen = VtScreen(3, 6)
                screen.feed(base + '\u0301' * 100000)
                self.assertEqual(len(screen.lines[0][0][0]), MAX_CELL_CHARACTERS)
                self.assertTrue(screen.lines[0][0][0].startswith(base + '\u0301'))
                self.assertEqual(screen.column, 2 if base == '界' else 1)
                screen.feed('X')
                self.assertEqual(screen.lines[0][2 if base == '界' else 1][0], 'X')

    def test_plain_text_cursor_and_wrap(self):
        screen = VtScreen(4, 10)
        screen.feed("hello")
        self.assertEqual(screen.plain().splitlines()[0], "hello")
        self.assertEqual((screen.row, screen.column), (0, 5))
        screen.feed("x" * 12)
        self.assertEqual((screen.row, screen.column), (1, 7))

    def test_pending_wrap_survives_formatting_queries_and_every_chunk_boundary(self):
        for sequence in ("\x1b[31m", "\x1b[0m", "\x1b[6n", "\x1b[5n", "\x1b[c",
                         "\x1b[?25l", "\x1b[?1h", "\x1b[4h", "\x1b(0\x1b(B"):
            text = "abc" + sequence + "d"
            for split in range(len(text) + 1):
                with self.subTest(sequence=sequence, split=split):
                    screen = VtScreen(3, 3)
                    screen.feed(text[:split])
                    screen.feed(text[split:])
                    self.assertEqual(screen.plain().splitlines()[:2], ["abc", "d"])
                    self.assertEqual((screen.row, screen.column), (1, 1))

    def test_right_margin_control_movement_cancels_pending_wrap(self):
        for sequence, expected in (("\r", ["Xbc", ""]), ("\b", ["aXc", ""]),
                                   ("\x1b[D", ["aXc", ""]), ("\n", ["abc", "  X"]),
                                   ("\x1b[K", ["abX", ""])):
            with self.subTest(sequence=sequence):
                screen = VtScreen(3, 3)
                screen.feed("abc" + sequence + "X")
                self.assertEqual(screen.plain().splitlines()[:2], expected)

    def test_repeat_and_saved_cursor_preserve_right_margin_wrap(self):
        screen = VtScreen(3, 3)
        screen.feed("a\x1b[2bX")
        self.assertEqual(screen.plain().splitlines()[:2], ["aaa", "X"])
        for save, restore in (("\x1b7", "\x1b8"), ("\x1b[s", "\x1b[u")):
            screen = VtScreen(3, 3)
            screen.feed("abc" + save + "\r" + restore + "X")
            self.assertEqual(screen.plain().splitlines()[:2], ["abc", "X"])

    def test_unicode_combining_and_wide_characters_keep_cell_positions(self):
        screen = VtScreen(3, 6)
        screen.feed("Ae")
        screen.feed("\u0301界B")
        self.assertEqual(screen.lines[0][1][0], "e\u0301")
        self.assertEqual(screen.lines[0][2][0], "界")
        self.assertEqual(screen.lines[0][3][0], "")
        self.assertEqual(screen.column, 5)
        screen.feed("界")
        self.assertEqual((screen.row, screen.column), (1, 2))
        self.assertEqual(screen.plain().splitlines()[:2], ["Ae\u0301界B", "界"])

    def test_wide_overwrite_erase_and_edit_do_not_leave_half_characters(self):
        screen = VtScreen(3, 20)
        screen.feed("A界B\x1b[1;3Hx")
        self.assertEqual(screen.plain().splitlines()[0], "A xB")
        screen.feed("\rA界B\x1b[1;3H\x1b[X")
        self.assertEqual(screen.plain().splitlines()[0], "A  B")
        screen.feed("\rA界B\x1b[1;3H\x1b[@")
        self.assertNotIn("界", screen.plain())
        screen.feed("\rA界B\x1b[1;3H\x1b[P")
        self.assertNotIn("界", screen.plain())

    def test_resize_clips_entire_wide_character_and_preserves_combining_text(self):
        screen = VtScreen(3, 30)
        screen.feed("e\u0301" + "x" * 18 + "界")
        screen.resize(3, 20)
        self.assertEqual(screen.lines[0][0][0], "e\u0301")
        self.assertEqual(screen.lines[0][-1][0], " ")
        self.assertNotIn("界", screen.plain())

    def test_carriage_return_line_feed_and_backspace(self):
        screen = VtScreen(3, 20)
        screen.feed("one\r\ntwo")
        self.assertEqual(screen.plain().splitlines()[:2], ["one", "two"])
        screen.feed("\b\b!")
        self.assertEqual(screen.plain().splitlines()[1], "t!o")

    def test_tab_moves_to_next_stop(self):
        screen = VtScreen(2, 40)
        screen.feed("a\tb")
        self.assertEqual(screen.plain().splitlines()[0], "a       b")

    def test_scrolling_drops_top_line(self):
        screen = VtScreen(3, 20)
        for line in range(5):
            screen.feed(f"L{line}\r\n")
        self.assertEqual(screen.plain().splitlines(), ["L3", "L4"])

    def test_scrollback_is_bounded_and_viewport_preserves_order(self):
        screen = VtScreen(3, 20, scrollback=4)
        for line in range(8):
            screen.feed(f"L{line}\r\n")
        self.assertEqual(len(screen.history), 4)
        self.assertEqual(screen.history_count, 6)
        visible = lambda offset: ["".join(cell[0] for cell in line).rstrip()
                                  for line in screen.viewport(offset)]
        self.assertEqual(visible(0), ["L6", "L7", ""])
        self.assertEqual(visible(2), ["L4", "L5", "L6"])
        self.assertEqual(visible(999), ["L2", "L3", "L4"])

    def test_scrollback_excludes_alt_screen_and_partial_scroll_regions(self):
        screen = VtScreen(3, 20)
        screen.feed("original\r\n\r\n\r\n")
        self.assertEqual(len(screen.history), 1)
        screen.feed("\x1b[?1049h" + "alternate\r\n" * 6)
        self.assertEqual(len(screen.history), 1)
        self.assertIs(screen.viewport(1), screen.lines)
        screen.feed("\x1b[?1049l\x1b[1;2r\x1b[2;1H\n\n")
        self.assertEqual(len(screen.history), 1)

    def test_history_resizes_for_display_and_clear_scrollback_keeps_screen(self):
        screen = VtScreen(3, 40)
        screen.feed("x" * 35 + "\r\nkept\r\n\r\n")
        screen.resize(3, 20)
        self.assertTrue(all(len(line) == 20 for line in screen.viewport(1)))
        screen.resize(3, 50)
        self.assertEqual("".join(cell[0] for cell in screen.viewport(1)[0]).rstrip(), "x" * 35)
        before = screen.plain()
        screen.feed("\x1b[3J")
        self.assertFalse(screen.history)
        self.assertEqual(screen.plain(), before)

    def test_bracketed_paste_modes_and_reset(self):
        screen = VtScreen(3, 20)
        self.assertFalse(screen.bracketed_paste)
        screen.feed("\x1b[?2004h")
        self.assertTrue(screen.bracketed_paste)
        screen.feed("\x1b[?2004l")
        self.assertFalse(screen.bracketed_paste)
        screen.feed("\x1b[?2004h\x1b[?1049htext\x1bc")
        self.assertFalse(screen.bracketed_paste)
        self.assertFalse(screen._alt_active)
        self.assertIsNone(screen._primary)

    def test_cursor_addressing_and_erase(self):
        screen = VtScreen(4, 20)
        screen.feed("abcdef\r\x1b[K")
        self.assertEqual(screen.plain().splitlines()[0], "")
        screen.feed("one two\x1b[1G")
        self.assertEqual((screen.row, screen.column), (0, 0))
        screen.feed("\x1b[2;3Hxyz")
        self.assertEqual((screen.row, screen.column), (1, 5))
        self.assertTrue(screen.plain().splitlines()[1].startswith("  xyz"))
        screen.feed("\x1b[2J")
        self.assertEqual(screen.plain(), "\n\n\n")

    def test_arrow_movement_stops_at_edges(self):
        screen = VtScreen(4, 10)
        screen.feed("\x1b[A\x1b[D")
        self.assertEqual((screen.row, screen.column), (0, 0))

    def test_colors_and_attributes_apply_and_reset(self):
        screen = VtScreen(2, 20)
        screen.feed("\x1b[1;31mred\x1b[0m plain")
        line = screen.lines[0]
        self.assertEqual((line[0][1].fg, line[0][1].flags & 1), (1, 1))
        self.assertEqual(line[4][1].fg, None)

    def test_dim_strikethrough_sgr22_and_rep_csi_b(self):
        screen = VtScreen(2, 20)
        screen.feed("\x1b[1;2;9mA\x1b[22mB\x1b[29mC\x1b[3b")
        line = screen.lines[0]
        self.assertEqual(line[0][1].flags & (BOLD | DIM | STRIKE), BOLD | DIM | STRIKE)
        self.assertEqual(line[1][1].flags & (BOLD | DIM | STRIKE), STRIKE)
        self.assertEqual(line[2][1].flags & (BOLD | DIM | STRIKE), 0)
        self.assertEqual(screen.plain().splitlines()[0], "ABCCCC")

    def test_extended_256_color(self):
        screen = VtScreen(2, 20)
        screen.feed("\x1b[38;5;196mX")
        self.assertEqual(screen.lines[0][0][1].fg, 196)

    def test_true_color_channels_stay_within_renderable_range(self):
        screen = VtScreen(2, 20)
        screen.feed("\x1b[38;2;99999999999999999999;128;0mX")
        self.assertEqual(screen.lines[0][0][1].fg, (255, 128, 0))

    def test_alt_screen_saves_and_restores_primary(self):
        screen = VtScreen(4, 20)
        screen.feed("primary text")
        screen.feed("\x1b[?1049h")
        self.assertEqual(screen.plain(), "\n\n\n")
        screen.feed("alternate")
        screen.feed("\x1b[?1049l")
        self.assertEqual(screen.plain().splitlines()[0], "primary text")

    def test_scroll_region_confines_scrolling(self):
        screen = VtScreen(5, 20)
        screen.feed("\x1b[1;2r")
        screen.feed("a\r\nb\r\nc")
        self.assertEqual(screen.plain().splitlines()[:3], ["b", "c", ""])

    def test_insert_and_delete_lines(self):
        screen = VtScreen(4, 20)
        screen.feed("1\r\n2\r\n3")
        screen.feed("\x1b[1;1H\x1b[1L")
        self.assertEqual(screen.plain().splitlines()[:3], ["", "1", "2"])
        screen.feed("\x1b[1;1H\x1b[2M")
        self.assertEqual(screen.plain().splitlines()[:3], ["2", "3", ""])

    def test_insert_delete_lines_obey_both_scroll_margins(self):
        for operation in ("L", "M"):
            for row in (1, 5):
                with self.subTest(operation=operation, row=row):
                    screen = VtScreen(5, 8)
                    screen.feed("one\r\ntwo\r\nthree\r\nfour\r\nfive")
                    original = screen.plain()
                    screen.feed(f"\x1b[2;4r\x1b[{row};1H\x1b[99{operation}")
                    self.assertEqual(screen.plain(), original)
            for count in (1, 99):
                with self.subTest(operation=operation, count=count):
                    screen = VtScreen(5, 8)
                    screen.feed("one\r\ntwo\r\nthree\r\nfour\r\nfive")
                    screen.feed(f"\x1b[2;4r\x1b[3;1H\x1b[{count}{operation}")
                    expected = (["", "three"] if operation == "L" else ["four", ""]) if count == 1 else ["", ""]
                    self.assertEqual(screen.plain().splitlines(), ["one", "two", *expected, "five"])

    def test_osc_titles_and_unknown_sequences_are_ignored(self):
        screen = VtScreen(2, 40)
        screen.feed("\x1b]0;window title\x07after\x1b[9999999ztail")
        self.assertEqual(screen.plain().splitlines()[0], "aftertail")

    def test_partial_escape_sequence_kept_across_feeds(self):
        screen = VtScreen(2, 20)
        screen.feed("a\x1b[")
        screen.feed("2D")
        # 2D moves the cursor left twice; it does not erase.
        self.assertEqual(screen.plain().splitlines()[0], "a")
        self.assertEqual((screen.row, screen.column), (0, 0))

    def test_resize_updates_every_row_width_and_accepts_output(self):
        screen = VtScreen(3, 20)
        screen.feed("kept")
        for rows, columns in ((5, 40), (2, 25)):
            screen.resize(rows, columns)
            self.assertEqual(len(screen.lines), rows)
            self.assertTrue(all(len(line) == columns for line in screen.lines))
            screen.feed(f"\x1b[{rows};{columns}HX")
            self.assertEqual(screen.lines[-1][-1][0], "X")
        # The cursor was on the last of five rows, so shrinking keeps that row
        # visible and moves the rows above it to scrollback.
        self.assertTrue("".join(cell[0] for cell in screen.history[0]).startswith("kept"))

    def test_resize_alternate_screen_also_resizes_saved_primary(self):
        for rows, columns in ((5, 40), (2, 20)):
            with self.subTest(rows=rows, columns=columns):
                screen = VtScreen(3, 30)
                screen.feed("primary\x1b[?1049h")
                screen.resize(rows, columns)
                screen.feed("\x1b[?1049l")
                self.assertEqual(len(screen.lines), rows)
                self.assertTrue(all(len(line) == columns for line in screen.lines))
                self.assertTrue(screen.plain().startswith("primary"))
                screen.feed(f"\x1b[{rows};1H\nend")
                self.assertIn("end", screen.plain())

    def test_resize_preserves_text(self):
        screen = VtScreen(4, 20)
        screen.feed("hello")
        screen.resize(6, 30)
        self.assertEqual(screen.plain().splitlines()[0], "hello")
        self.assertEqual((screen.rows, screen.columns), (6, 30))

    def test_saved_cursor_restore_after_shrink(self):
        for save, restore in (("\x1b7", "\x1b8"), ("\x1b[s", "\x1b[u")):
            with self.subTest(restore=restore):
                screen = VtScreen(8, 40)
                screen.feed("\x1b[8;40H" + save)
                screen.resize(2, 20)
                screen.feed(restore + "X")
                self.assertEqual(screen.lines[-1][-1][0], "X")

    def test_unsupported_csi_is_consumed_without_changing_screen(self):
        for params, final in (("?2", "J"), ("1?2", "A"), ("²", "A"),
                              ("9" * 5000, "A"),
                              ("9" * 5000, "m"), ("?" + "9" * 5000, "h")):
            with self.subTest(params=params[:30], final=final):
                screen = VtScreen(2, 20)
                screen.feed("kept\x1b[")
                # Exercise parameters split across PTY reads too.
                for start in range(0, len(params), 100):
                    screen.feed(params[start:start + 100])
                    self.assertLessEqual(len(screen._params), 256)
                screen.feed(final + "tail")
                self.assertEqual(screen.plain().splitlines()[0], "kepttail")
                self.assertIsNone(screen.lines[0][4][1].fg)

    def test_colon_form_sgr_colors_and_underline(self):
        screen = VtScreen(2, 20)
        screen.feed("\x1b[38:2::1:2:3;48:5:9;4:3mA\x1b[4:0mB")
        style = screen.lines[0][0][1]
        self.assertEqual((style.fg, style.bg), ((1, 2, 3), 9))
        self.assertTrue(style.flags & UNDERLINE)
        self.assertFalse(screen.lines[0][1][1].flags & UNDERLINE)
        screen.feed("\x1b[0;38:2:1:2:3mC")
        self.assertEqual(screen.lines[0][2][1].fg, (1, 2, 3))

    def test_mixed_color_parameters_do_not_interrupt_following_output(self):
        for color in ("38;5;1:2", "48;5;1:2", "38;2;1:2;2;3",
                      "48;2;1;2:3;4", "38;2;1;2;3:4", "38;2:1;4"):
            with self.subTest(color=color):
                screen = VtScreen(2, 20)
                screen.feed("before\x1b[" + color + "mX\x1b[32mY")
                self.assertEqual(screen.plain().splitlines()[0], "beforeXY")
                self.assertEqual(screen.lines[0][7][1].fg, 2)

    def test_dec_special_graphics_draws_boxes_and_shift_selects_g1(self):
        screen = VtScreen(3, 20)
        screen.feed("\x1b(0lqk\x1b(Bq")
        self.assertEqual(screen.plain().splitlines()[0], "\u250c\u2500\u2510q")
        screen.feed("\r\n\x1b)0a\x0eq\x0fq")
        self.assertEqual(screen.plain().splitlines()[1], "a\u2500q")

    def test_string_sequences_and_two_byte_escapes_are_not_printed(self):
        for sequence in ("\x1bP+q544e\x1b\\", "\x1b_apc\x1b\\", "\x1b^pm\x1b\\", "\x1bXsos\x1b\\",
                         "\x1bPabort\x18", "\x1b#8", "\x1b%G", "\x1b F"):
            with self.subTest(sequence=sequence):
                screen = VtScreen(2, 20)
                screen.feed("a" + sequence + "b")
                self.assertEqual(screen.plain().splitlines()[0], "ab")

    def test_status_queries_queue_replies(self):
        screen = VtScreen(4, 20)
        screen.feed("ab\x1b[6n\x1b[5n\x1b[c\x1b[0c\x1b[>c\x1b[?6n")
        self.assertEqual(screen.responses, ["\x1b[1;3R", "\x1b[0n", "\x1b[?62;22c",
                                            "\x1b[?62;22c", "\x1b[>0;0;0c"])
        screen = VtScreen(2, 20)
        screen.feed("x" * 20 + "\x1b[6n")
        self.assertEqual(screen.responses, ["\x1b[1;20R"])

    def test_insert_mode_autowrap_and_application_cursor_modes(self):
        screen = VtScreen(2, 10)
        screen.feed("abc\r\x1b[4hZ\x1b[4l")
        self.assertEqual(screen.plain().splitlines()[0], "Zabc")
        screen = VtScreen(2, 5)
        screen.feed("\x1b[?7lhello world")
        self.assertEqual(screen.plain().splitlines()[0], "helld")
        screen.feed("\x1b[?7h")
        self.assertTrue(screen._autowrap)
        self.assertFalse(screen.application_cursor)
        screen.feed("\x1b[?1h")
        self.assertTrue(screen.application_cursor)
        screen.feed("\x1bc")
        self.assertFalse(screen.application_cursor)

    def test_saved_cursor_includes_style_and_charset(self):
        screen = VtScreen(2, 20)
        screen.feed("\x1b[31m\x1b(0\x1b7\x1b[0m\x1b(B\x1b8q")
        cell = screen.lines[0][0]
        self.assertEqual((cell[0], cell[1].fg), ("\u2500", 1))

    def test_shrinking_keeps_the_cursor_line_and_moves_rows_to_history(self):
        screen = VtScreen(4, 20)
        screen.feed("one\r\ntwo\r\nthree\r\nprompt$ ")
        screen.resize(2, 20)
        self.assertEqual(screen.plain().splitlines(), ["three", "prompt$"])
        self.assertEqual(screen.row, 1)
        self.assertEqual(["".join(cell[0] for cell in line).rstrip() for line in screen.history],
                         ["one", "two"])
        self.assertEqual(screen.history_count, 2)

    def test_erase_to_start_of_display_clears_previous_rows(self):
        screen = VtScreen(4, 20)
        screen.feed("first\r\nsecond\r\nthird\r\nlast")
        screen.feed("\x1b[3;3H\x1b[1J")
        self.assertEqual(screen.plain().splitlines(), ["", "", "   rd", "last"])

    def test_cancelled_csi_recovers_for_text_and_new_sequences(self):
        for params in ("12", "9" * 5000):
            for ending in ("\x18", "\x1a", "\x1b[0m"):
                with self.subTest(length=len(params), ending=ending):
                    screen = VtScreen(2, 20)
                    screen.feed("kept\x1b[" + params)
                    screen.feed(ending + "tail")
                    self.assertEqual(screen.plain().splitlines()[0], "kepttail")


class KeyMappingTests(unittest.TestCase):
    def test_common_keys(self):
        self.assertEqual(key_to_bytes(0x51, "q", 0), b"q")
        self.assertEqual(key_to_bytes(QT_KEY_ENTER, "", 0), b"\r")
        self.assertEqual(key_to_bytes(QT_KEY_TAB, "", 0), b"\t")
        self.assertEqual(key_to_bytes(QT_KEY_BACKSPACE, "", 0), b"\x7f")
        self.assertEqual(key_to_bytes(QT_KEY_UP, "", 0), b"\x1b[A")
        self.assertEqual(key_to_bytes(QT_KEY_DOWN, "", 0), b"\x1b[B")
        self.assertEqual(key_to_bytes(QT_KEY_RIGHT, "", 0), b"\x1b[C")
        self.assertEqual(key_to_bytes(QT_KEY_LEFT, "", 0), b"\x1b[D")
        self.assertEqual(key_to_bytes(QT_KEY_HOME, "", 0), b"\x1b[H")
        self.assertEqual(key_to_bytes(QT_KEY_END, "", 0), b"\x1b[F")
        self.assertEqual(key_to_bytes(QT_KEY_DELETE, "", 0), b"\x1b[3~")
        self.assertEqual(key_to_bytes(QT_KEY_PAGEUP, "", 0), b"\x1b[5~")

    def test_modifiers(self):
        self.assertEqual(key_to_bytes(0x43, "c", ctrl=True), b"\x03")
        self.assertEqual(key_to_bytes(0x41, "a", alt=True), b"\x1ba")
        self.assertEqual(key_to_bytes(QT_KEY_LEFT, "", alt=True), b"\x1b[1;3D")
        self.assertEqual(key_to_bytes(QT_KEY_LEFT, "", ctrl=True), b"\x1b[1;5D")
        self.assertEqual(key_to_bytes(QT_KEY_RIGHT, "", ctrl=True), b"\x1b[1;5C")
        self.assertEqual(key_to_bytes(QT_KEY_UP, "", ctrl=True), b"\x1b[1;5A")
        self.assertEqual(key_to_bytes(QT_KEY_DOWN, "", ctrl=True), b"\x1b[1;5B")


class ExtendedKeyMappingTests(unittest.TestCase):
    def test_application_cursor_mode_uses_ss3_only_without_modifiers(self):
        self.assertEqual(key_to_bytes(QT_KEY_UP, "", application_cursor=True), b"\x1bOA")
        self.assertEqual(key_to_bytes(QT_KEY_HOME, "", application_cursor=True), b"\x1bOH")
        self.assertEqual(key_to_bytes(QT_KEY_UP, "", ctrl=True, application_cursor=True), b"\x1b[1;5A")

    def test_function_keys_backtab_and_modified_editing_keys(self):
        f1 = Qt.Key.Key_F1.value
        expected = [b"\x1bOP", b"\x1bOQ", b"\x1bOR", b"\x1bOS", b"\x1b[15~", b"\x1b[17~",
                    b"\x1b[18~", b"\x1b[19~", b"\x1b[20~", b"\x1b[21~", b"\x1b[23~", b"\x1b[24~"]
        self.assertEqual([key_to_bytes(f1 + index, "") for index in range(12)], expected)
        self.assertEqual(key_to_bytes(f1, "", shift=True), b"\x1b[1;2P")
        self.assertEqual(key_to_bytes(f1 + 4, "", ctrl=True), b"\x1b[15;5~")
        self.assertEqual(key_to_bytes(Qt.Key.Key_Backtab.value, ""), b"\x1b[Z")
        self.assertEqual(key_to_bytes(QT_KEY_DELETE, "", ctrl=True), b"\x1b[3;5~")
        self.assertEqual(key_to_bytes(QT_KEY_BACKSPACE, "", ctrl=True), b"\x08")
        self.assertEqual(key_to_bytes(Qt.Key.Key_Escape.value, "\x1b"), b"\x1b")

    def test_control_symbols_match_xterm(self):
        for key, value in ((" ", 0), ("@", 0), ("2", 0), ("[", 27), ("\\", 28), ("]", 29),
                           ("^", 30), ("_", 31), ("?", 127)):
            with self.subTest(key=key):
                self.assertEqual(key_to_bytes(ord(key), "", ctrl=True), bytes([value]))
        self.assertEqual(key_to_bytes(0x43, "", ctrl=True, alt=True), b"\x1b\x03")


class SessionTerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_session(self, command, input_text=None, seconds=8):
        chunks = []
        process = PtyProcess(command)
        self.addCleanup(process.terminate)
        process.output.connect(chunks.append)
        done = []
        process.finished.connect(done.append)
        if input_text is not None:
            QTimer.singleShot(300, lambda: process.write(input_text))
        deadline = time.monotonic() + seconds
        while not done and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(done, "session did not finish: " + "".join(chunks))
        return "".join(chunks), done[0]

    @unittest.skipUnless(shutil.which("setsid"), "setsid is required")
    def test_session_owns_the_pty_as_controlling_terminal(self):
        script = 'import os\nprint("TTY", os.tcgetpgrp(0) == os.getpgrp(), os.getsid(0) == os.getpid(), flush=True)\n'
        output, code = self.run_session([sys.executable, "-I", "-S", "-c", script])
        self.assertEqual(code, 0, output)
        self.assertIn("TTY True True", output)

    @unittest.skipUnless(shutil.which("setsid"), "setsid is required")
    def test_ctrl_c_interrupts_the_foreground_process(self):
        script = ('import signal, sys, time\nprint("READY", flush=True)\n'
                  'try:\n    time.sleep(30)\nexcept KeyboardInterrupt:\n    print("INTERRUPTED", flush=True)\n')
        output, code = self.run_session([sys.executable, "-I", "-S", "-c", script], "\x03")
        self.assertEqual(code, 0, output)
        self.assertIn("INTERRUPTED", output)

    def test_unsignalable_session_group_hangs_up_and_finishes(self):
        process = PtyProcess(["sleep", "30"])
        finished = []
        process.finished.connect(finished.append)
        with patch("chrootables.terminal.os.killpg", side_effect=PermissionError):
            process.terminate()
        deadline = time.monotonic() + 6
        while not finished and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(finished)
        self.assertIsNone(process.master)

    def test_session_ignoring_sigterm_is_hung_up_after_grace_period(self):
        process = PtyProcess([sys.executable, "-I", "-S", "-c",
                              "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                              "signal.signal(signal.SIGHUP, signal.SIG_IGN)\ntime.sleep(30)\n"])
        finished = []
        process.finished.connect(finished.append)
        with patch("chrootables.terminal.TERMINATE_GRACE_MS", 100), patch("chrootables.terminal.HANGUP_GRACE_MS", 300):
            time.sleep(0.3)
            process.terminate()
            deadline = time.monotonic() + 5
            while not finished and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertTrue(finished)


class TerminalWindowKeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = TerminalWindow(["sleep", "30"], "test", external=lambda: None)
        self.addCleanup(self.window.process.terminate)
        self.sent = []
        self.window.process.write = self.sent.append
        self.window.show()
        self.app.processEvents()

    def test_pane_starts_with_focus_and_no_button_can_be_triggered_by_enter(self):
        self.assertIs(self.window.focusWidget(), self.window.pane)
        for control in self.window.findChildren(QPushButton):
            self.assertFalse(control.autoDefault(), control.text())

    def test_tab_and_backtab_reach_the_session(self):
        QTest.keyClick(self.window.pane, Qt.Key.Key_Tab)
        QTest.keyClick(self.window.pane, Qt.Key.Key_Backtab)
        self.assertEqual(self.sent, [b"\t", b"\x1b[Z"])
        self.assertIs(self.window.focusWidget(), self.window.pane)

    def test_status_query_reply_is_written_back_to_the_session(self):
        self.window.pane._on_output("\x1b[6n")
        self.assertEqual(self.sent, ["\x1b[1;1R"])
        self.assertEqual(self.window.pane.screen.responses, [])

    def test_find_maps_columns_when_lowercasing_changes_length(self):
        self.window.pane._on_output("\u0130\u0130 needle")
        self.assertTrue(self.window.pane.find_text("needle"))
        self.assertEqual(self.window.pane._selection_start[1], 3)
        self.assertEqual(self.window.pane._selection_end[1], 9)

    def test_wheel_on_alternate_screen_sends_arrows(self):
        self.window.pane._on_output("\x1b[?1049h\x1b[?1h")
        event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.window.pane.wheelEvent(event)
        self.assertEqual(self.sent, [b"\x1bOA" * 3])


class PtyProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_failed_start_closes_both_pty_descriptors(self):
        for failure in ("spawn", "window_size"):
            with self.subTest(failure=failure):
                master, slave = os.openpty()
                try:
                    failing_call = ("chrootables.terminal.subprocess.Popen" if failure == "spawn"
                                    else "chrootables.terminal.PtyProcess._set_window_size")
                    with patch("chrootables.terminal.os.openpty", return_value=(master, slave)), \
                            patch(failing_call, side_effect=OSError("launch failed")):
                        with self.assertRaisesRegex(OSError, "launch failed"):
                            PtyProcess(["missing-command"])
                    for descriptor in (master, slave):
                        with self.assertRaises(OSError):
                            os.fstat(descriptor)
                finally:
                    for descriptor in (master, slave):
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass

    def test_eof_reaps_without_waiting_and_invalidates_descriptor(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = AssertionError("GUI thread must not wait")
        with patch("chrootables.terminal.subprocess.Popen", return_value=child):
            process = PtyProcess(["fake-child"])
        finished = []
        output = []
        process.finished.connect(finished.append)
        process.output.connect(output.append)
        process._decoder.decode(b"\xe2")
        process._readable()  # The slave is closed, but the child has not exited.
        self.assertIsNone(process.master)
        self.assertEqual(finished, [])
        self.assertEqual(output, ["\ufffd"])
        with patch("chrootables.terminal.os.write") as write, \
                patch.object(process, "_set_window_size") as resize:
            process.write("late input")
            process.resize(24, 80)
        write.assert_not_called()
        resize.assert_not_called()
        child.poll.return_value = 7
        self.assertTrue(pump(self.app, lambda: bool(finished)))
        process._readable()
        process._check_finished()
        self.assertEqual(finished, [7])
        child.wait.assert_not_called()

    def test_round_trip_and_finish(self):
        collected = []
        process = PtyProcess(["/bin/sh", "-c", "echo READY; cat"])
        process.output.connect(collected.append)
        code = []
        process.finished.connect(code.append)
        self.assertTrue(pump(self.app, lambda: "READY" in "".join(collected)))
        process.write("ping\n")
        self.assertTrue(pump(self.app, lambda: "ping" in "".join(collected)))
        process.terminate()
        self.assertTrue(pump(self.app, lambda: bool(code)))

    def test_exited_leader_finishes_when_a_descendant_ignores_hangup(self):
        script = ("import signal,subprocess; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
                  "subprocess.Popen(['/bin/sleep','3']); print('final héllo',flush=True)")
        process = PtyProcess([sys.executable, '-I', '-S', '-c', script])
        output, finished = [], []
        process.output.connect(output.append)
        process.finished.connect(finished.append)
        self.assertTrue(pump(self.app, lambda: bool(finished), timeout=1))
        self.assertEqual(finished, [0])
        self.assertIn('final héllo', ''.join(output))
        self.assertIsNone(process.master)
        self.assertFalse(process._reaper.isActive())
        process._check_finished()
        self.assertEqual(finished, [0])

    def test_partial_writes_and_backpressure_retain_input_in_order(self):
        child = Mock()
        child.poll.return_value = None
        with patch("chrootables.terminal.subprocess.Popen", return_value=child):
            process = PtyProcess(["fake-child"])
        with patch("chrootables.terminal.os.write", side_effect=[2, BlockingIOError(), 4]) as write:
            process.write(b"abc")
            self.assertEqual(process._pending_input, b"c")
            process.write(b"def")
            self.assertEqual(process._pending_input, b"cdef")
            process._flush_input()
            self.assertEqual(process._pending_input, b"")
            self.assertEqual([bytes(call.args[1]) for call in write.call_args_list],
                             [b"abc", b"cdef", b"cdef"])
            self.assertFalse(process._write_notifier.isEnabled())
        process._readable()  # No slave: close the mock session's descriptor.
        process._reaper.stop()

    def test_large_paste_does_not_block_gui_when_guest_stops_reading(self):
        process = PtyProcess(["/bin/sh", "-c", "printf READY; sleep 30"])
        output = []
        process.output.connect(output.append)
        self.assertTrue(pump(self.app, lambda: "READY" in "".join(output)))
        started = time.monotonic()
        process.write(b"x" * 500000)
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(process._pending_input)
        process.terminate()
        self.assertTrue(pump(self.app, lambda: process._finished))
        self.assertFalse(process._pending_input)

    def test_split_utf8_output_is_reassembled(self):
        collected = []
        payload = "héllo"
        process = PtyProcess(["/bin/sh", "-c", "printf h\\xc3\\xa9llo; sleep 1"])
        process.output.connect(collected.append)
        finished = []
        process.finished.connect(finished.append)
        # Feed the process output in two chunks to the decoder directly.
        raw = payload.encode("utf-8")
        process.output.emit(process._decoder.decode(raw[:2]))
        process.output.emit(process._decoder.decode(raw[2:]))
        self.assertEqual("".join(collected), "héllo")
        process.terminate()
        self.assertTrue(pump(self.app, lambda: bool(finished)))


class TerminalPaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pane_renders_output_and_forwards_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "typed.txt")
            pane = TerminalPane()
            pane.resize(400, 300)
            process = PtyProcess(["/bin/sh", "-c", f"printf 'PROMPT> '; cat > '{target}'"])
            pane.attach(process)
            pane.show()
            self.assertTrue(pump(self.app,
                                 lambda: "PROMPT>" in pane.screen.plain()))
            QTest.keyClicks(pane, "hello")
            QTest.keyClick(pane, Qt.Key.Key_Return)
            self.assertTrue(pump(self.app,
                                 lambda: os.path.exists(target) and
                                 Path(target).read_text().strip() == "hello"))
            process.terminate()
            self.assertTrue(pump(self.app, lambda: process._finished))
            pane.close()

    def test_256_color_palette_and_hidden_cursor_are_rendered(self):
        pane = TerminalPane()
        self.addCleanup(pane.close)
        self.assertEqual(pane._brush(0, "#ffffff").getRgb()[:3], (0, 0, 0))
        self.assertEqual(pane._brush(196, "#ffffff").getRgb()[:3], (255, 0, 0))
        self.assertEqual(pane._brush(46, "#ffffff").getRgb()[:3], (0, 255, 0))
        self.assertEqual(pane._brush(232, "#ffffff").getRgb()[:3], (8, 8, 8))
        self.assertEqual(pane._brush(255, "#ffffff").getRgb()[:3], (238, 238, 238))
        pane.resize(400, 100)
        pane.show()
        self.app.processEvents()
        pane.process = Mock(running=True)
        pane._cursor_shown = True
        pane.screen.feed("abc")
        x = pane.screen.column * pane.cell_width
        self.assertEqual(pane.grab().toImage().pixelColor(x, 0).name(), "#e6e6e2")
        pane.screen.feed("\x1b[?25l")
        self.assertEqual(pane.grab().toImage().pixelColor(x, 0).name(), "#1c1c1c")
        pane.screen.feed("\r\x1b[7m ")
        self.assertEqual(pane.grab().toImage().pixelColor(0, 0).name(), "#e6e6e2")

    def test_attributes_reach_painter_and_wide_selection_copies_once(self):
        pane = TerminalPane()
        self.addCleanup(pane.close)
        pane.screen.feed("\x1b[1;2;3;4;9m界e\u0301")
        with patch("chrootables.terminal.QPainter") as painter:
            pane.paintEvent(None)
        fonts = [call.args[0] for call in painter.return_value.setFont.call_args_list]
        self.assertTrue(any(font.bold() and font.italic() and font.underline() and font.strikeOut() for font in fonts))
        pens = [call.args[0] for call in painter.return_value.setPen.call_args_list]
        self.assertTrue(any(pen.red() < 230 for pen in pens))
        pane._selection_start, pane._selection_end = (0, 0), (0, 3)
        self.assertEqual(pane.selected_text(), "界e\u0301")
        pane._selection_start, pane._selection_end = (0, 1), (0, 2)
        self.assertEqual(pane.selected_text(), "界")

    def test_scrollback_search_and_clear_scrollback(self):
        pane = TerminalPane()
        self.addCleanup(pane.close)
        pane.screen = VtScreen(3, 20, scrollback=10)
        pane._on_output("alpha needle\r\nbeta\r\ngamma\r\ndelta\r\nepsilon needle\r\n")
        self.assertTrue(pane.find_text("needle", forward=True))
        self.assertGreater(pane.scroll_offset, 0)
        self.assertEqual(pane.selected_text(), "needle")
        self.assertTrue(pane.find_text("needle", forward=True))
        self.assertEqual(pane.scroll_offset, 0)
        self.assertEqual(pane.selected_text(), "needle")
        self.assertFalse(pane.find_text("nonexistent"))
        pane.clear_scrollback()
        self.assertEqual(len(pane.screen.history), 0)
        self.assertEqual(pane.scroll_offset, 0)

    def test_history_navigation_after_exit_and_anchor_during_output(self):
        pane = TerminalPane()
        pane.screen = VtScreen(3, 20, scrollback=5)
        pane._on_output("".join(f"L{index}\r\n" for index in range(6)))
        QTest.keyClick(pane, Qt.Key.Key_PageUp, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(pane.scroll_offset, 2)
        before = pane.screen.viewport(pane.scroll_offset)[0]
        pane._on_output("next\r\n")
        self.assertEqual(pane.scroll_offset, 3)
        self.assertEqual(pane.screen.viewport(pane.scroll_offset)[0], before)
        QTest.keyClick(pane, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(pane.scroll_offset, 1)
        pane._on_output("\x1b[?1049h")
        self.assertEqual(pane.scroll_offset, 0)
        pane.scroll(5)
        self.assertEqual(pane.scroll_offset, 0)
        pane.close()

    def test_selection_copy_and_shortcuts_preserve_ctrl_c(self):
        pane = TerminalPane()
        pane.screen = VtScreen(3, 20)
        pane._on_output("first line\r\nsecond line")
        start = QPoint(0, pane.cell_height // 2)
        end = QPoint(6 * pane.cell_width, pane.cell_height + pane.cell_height // 2)
        QTest.mousePress(pane, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(pane, end)
        QTest.mouseRelease(pane, Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(pane.selected_text(), "first line\nsecond")
        QTest.keyClick(pane, Qt.Key.Key_C,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.app.clipboard().text(), "first line\nsecond")
        process = Mock(running=True)
        pane.process = process
        QTest.keyClick(pane, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        process.write.assert_called_once_with(b"\x03")
        pane.close()

    def test_bracketed_paste_and_clipboard_newline_normalization(self):
        pane = TerminalPane()
        process = Mock(running=True)
        pane.process = process
        pane.paste("one\r\ntwo\n")
        process.write.assert_called_with(b"one\rtwo\r")
        pane.screen.feed("\x1b[?2004h")
        self.app.clipboard().setText("héllo\nworld\x1b[201~")
        QTest.keyClick(pane, Qt.Key.Key_V,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        process.write.assert_called_with(b"\x1b[200~h\xc3\xa9llo\rworld[201~\x1b[201~")
        pane.close()


class TerminalWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_external_handoff_waits_for_reaping_and_only_launches_once(self):
        handoffs = []
        window = TerminalWindow(["/bin/sh", "-c", "printf READY; sleep 30"],
                                "session", external=lambda: handoffs.append(
                                    (window.process._finished, window.process.master)))
        window.show()
        self.assertTrue(pump(self.app, lambda: "READY" in window.pane.screen.plain()))
        window._open_external()
        window._open_external()
        self.assertFalse(handoffs)
        self.assertTrue(pump(self.app, lambda: bool(handoffs)))
        self.assertEqual(handoffs, [(True, None)])
        self.assertFalse(window.isVisible())
        self.assertFalse(window.pane._blink.isActive())
        window._open_external()
        self.assertEqual(len(handoffs), 1)

    def test_close_or_escape_cancels_pending_external_handoff(self):
        for escape in (False, True):
            with self.subTest(escape=escape):
                external = Mock()
                window = TerminalWindow(["/bin/sh", "-c", "printf READY; sleep 30"],
                                        "session", external=external)
                window.show()
                self.assertTrue(pump(self.app, lambda: "READY" in window.pane.screen.plain()))
                window._open_external()
                if escape:
                    window.reject()
                    window.reject()
                else:
                    window.close()
                    window.close()
                self.assertTrue(pump(self.app, lambda: window.process._finished))
                external.assert_not_called()
                self.assertFalse(window.isVisible())

    def test_external_handoff_after_exit_and_failure_remains_visible(self):
        external = Mock(side_effect=RuntimeError("terminal unavailable"))
        window = TerminalWindow(["/bin/sh", "-c", "printf READY"], "session", external=external)
        window.show()
        self.assertTrue(pump(self.app, lambda: window.process._finished))
        with patch("chrootables.terminal.QMessageBox.warning") as warning:
            window._open_external()
        external.assert_called_once_with()
        warning.assert_called_once()
        self.assertTrue(window.isVisible())
        self.assertFalse(window._closing)
        external.side_effect = None
        window._open_external()
        self.assertEqual(external.call_count, 2)
        self.assertFalse(window.isVisible())

    def test_close_terminates_children_after_session_leader_exits(self):
        script = "import subprocess; subprocess.Popen(['/bin/sleep', '30']); print('READY', flush=True)"
        window = TerminalWindow([sys.executable, "-c", script], "session")
        window.show()
        self.assertTrue(pump(self.app, lambda: "READY" in window.pane.screen.plain()
                             and window.process.process.poll() is not None))
        self.assertTrue(pump(self.app, lambda: window.process._finished))
        window.close()
        self.assertTrue(pump(self.app, lambda: window.process._finished))
        self.assertFalse(window.isVisible())

    def test_close_and_escape_keep_window_owned_until_child_is_reaped(self):
        script = ("import os, signal, time; "
                  "signal.signal(signal.SIGTERM, lambda *args: (time.sleep(.15), os._exit(0))); "
                  "print('READY', flush=True); time.sleep(30)")
        for escape in (False, True, False):
            with self.subTest(escape=escape):
                window = TerminalWindow([sys.executable, "-c", script], "session")
                window.show()
                finished = []
                window.finished.connect(finished.append)
                self.assertTrue(pump(self.app, lambda: "READY" in window.pane.screen.plain()))
                if escape:
                    QTest.keyClick(window, Qt.Key.Key_Escape)
                else:
                    window.close()
                self.assertEqual(finished, [])
                self.assertTrue(window.isVisible())
                self.assertTrue(pump(self.app, lambda: bool(finished)))
                self.assertFalse(window.isVisible())
                self.assertTrue(window.process._finished)
                self.assertIsNone(window.process.master)
                self.assertEqual(window.process.process.returncode, 0)
                self.assertEqual(len(finished), 1)

    def test_window_hosts_session_and_close_terminates_it(self):
        window = TerminalWindow(["/bin/sh", "-c", "printf 'READY'; sleep 30"],
                                "Chrootables · lab")
        self.assertEqual(window.windowTitle(), "Chrootables · lab")
        window.show()
        self.assertTrue(pump(self.app, lambda: "READY" in window.pane.screen.plain()))
        self.assertFalse(window.search_bar.isVisible())
        QTest.keyClick(window.pane, Qt.Key.Key_F,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(window.search_bar.isVisible())
        window.search_input.setText("READY")
        self.assertTrue(window.find_match(True))
        self.assertEqual(window.pane.selected_text(), "READY")
        self.assertTrue(window.process.running)
        window.close()
        self.assertTrue(pump(self.app, lambda: window.process._finished and not window.isVisible()))


if __name__ == "__main__":
    unittest.main()
