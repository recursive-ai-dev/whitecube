#!/usr/bin/env python3
"""Keyboard, forms and anchors through the real renderer's descriptor protocol."""
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
RENDERER = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else ROOT/'build/cube-renderd'

# The shared worker harness accepts positional binaries at import time.
arguments = sys.argv
try:
    sys.argv = [sys.argv[0]]
    import test_workers as protocol
finally:
    sys.argv = arguments

INTERACT, INTERACTION, ANCHOR, ANCHOR_RESULT = 17, 18, 19, 20
CLICK, TEXT, KEY, NEXT, PREV, BLUR = range(1, 7)
NONE, UPDATE, NAVIGATE, NOTICE, SUBMIT, COPY = range(6)
NO_FOCUS, LINK, EDITABLE, CONTROL = range(4)
UNKNOWN_Y = 0xffffffff


def frame(sock, width=480, height=360, scroll=0):
    protocol.send(sock, protocol.LAYOUT, width, height, scroll, text='1')
    result = protocol.receive(sock)
    assert result['type'] == protocol.FRAME and result['fd'] is not None, result
    try:
        size = width * height * 4
        pixels = os.pread(result['fd'], size, 0)
        assert len(pixels) == size
        return pixels, result
    finally:
        os.close(result['fd'])


def interact(sock, action, text='', x=0, y=0):
    protocol.send(sock, INTERACT, action, x, y, text=text)
    result = protocol.receive(sock)
    assert result['type'] == INTERACTION and result['fd'] is None, result
    assert result['a'] in range(6) and result['b'] in range(4), result
    return result


def anchor(sock, fragment):
    protocol.send(sock, ANCHOR, text=fragment)
    result = protocol.receive(sock)
    assert result['type'] == ANCHOR_RESULT and result['fd'] is None, result
    return result


class Interactions(unittest.TestCase):
    def test_tab_shift_tab_wrapped_links_focus_pixels_and_click(self):
        html = ('<body style="margin:0"><a href="/hidden" style="display:none">Hidden</a>'
                '<button disabled>Disabled</button><input type="hidden" name="hidden">'
                '<a href="/first" style="display:block">First link with enough words to wrap over several lines.</a>'
                '<a href="/second" style="display:block">Second link</a></body>')
        with protocol.worker(RENDERER) as (_, sock):
            self.assertEqual(protocol.load(sock, html), [])
            baseline, _ = frame(sock, width=180)
            first = interact(sock, NEXT)
            self.assertEqual((first['a'], first['b']), (UPDATE, LINK))
            self.assertNotEqual(first['c'], UNKNOWN_Y)
            focused, _ = frame(sock, width=180)
            self.assertNotEqual(focused, baseline, 'keyboard focus must be visible in actual raster pixels')
            second = interact(sock, NEXT)
            self.assertEqual(second['b'], LINK, 'wrapped fragments must occupy one tab stop')
            self.assertEqual(interact(sock, KEY, 'enter')['text'], 'https://example.org/second')
            self.assertEqual(interact(sock, PREV)['b'], LINK)
            self.assertEqual(interact(sock, KEY, 'enter')['text'], 'https://example.org/first')
            self.assertEqual(interact(sock, NEXT)['b'], LINK)
            self.assertEqual(interact(sock, NEXT)['b'], LINK)
            self.assertEqual(interact(sock, KEY, 'enter')['text'], 'https://example.org/first')
            result = interact(sock, BLUR)
            self.assertEqual(result['b'], NO_FOCUS)
            unfocused, _ = frame(sock, width=180)
            self.assertEqual(unfocused, baseline)
            clicked = interact(sock, CLICK, x=5, y=first['c'] + 5)
            self.assertEqual((clicked['a'], clicked['text']), (NAVIGATE, 'https://example.org/first'))

    def test_unicode_edits_selection_clipboard_and_exact_get_submission(self):
        html = ('<body style="margin:0"><form action="/search?discard=old" method="get">'
                '<input name="q" value="seed"><input type="hidden" name="token" value="a&amp;b">'
                '<button name="go" value="Run">Send</button></form></body>')
        with protocol.worker(RENDERER) as (_, sock):
            protocol.load(sock, html)
            before, _ = frame(sock)
            self.assertEqual(interact(sock, NEXT)['b'], EDITABLE)
            interact(sock, KEY, 'select-all')
            interact(sock, TEXT, 'Aé猫Z')
            interact(sock, KEY, 'left')
            interact(sock, KEY, 'backspace')  # Deletes one Unicode code point, not one byte.
            interact(sock, KEY, 'home')
            interact(sock, KEY, 'delete')
            interact(sock, KEY, 'end')
            interact(sock, TEXT, ' &+')
            interact(sock, KEY, 'select-all')
            copied = interact(sock, KEY, 'copy')
            self.assertEqual((copied['a'], copied['text']), (COPY, 'éZ &+'))
            cut = interact(sock, KEY, 'cut')
            self.assertEqual((cut['a'], cut['text']), (COPY, 'éZ &+'))
            interact(sock, TEXT, cut['text'])
            after, _ = frame(sock)
            self.assertNotEqual(after, before, 'edited Unicode values must reach raster pixels')
            self.assertEqual(interact(sock, NEXT)['b'], CONTROL)
            result = interact(sock, KEY, 'enter')
            self.assertEqual(result['a'], SUBMIT)
            self.assertEqual(result['text'], 'https://example.org/search?q=%C3%A9Z+%26%2B&token=a%26b&go=Run')

    def test_get_successful_controls_checkbox_radio_select_and_disabled_fields(self):
        html = ('<body><form action="/submit" method="get">'
                '<input type="hidden" name="token" value="hidden">'
                '<input type="checkbox" name="added" value="yes">'
                '<input type="checkbox" name="removed" checked>'
                '<input type="radio" name="choice" value="a" checked>'
                '<input type="radio" name="choice" value="b">'
                '<select name="kind"><option value="one">One</option><option value="two" selected>Two</option></select>'
                '<input name="disabled" value="secret" disabled><input value="unnamed">'
                '<button name="go" value="run">Submit</button></form></body>')
        with protocol.worker(RENDERER) as (_, sock):
            protocol.load(sock, html)
            frame(sock)
            for _ in range(2):
                self.assertEqual(interact(sock, NEXT)['b'], CONTROL)
                interact(sock, KEY, 'space')
            self.assertEqual(interact(sock, NEXT)['b'], CONTROL)
            self.assertEqual(interact(sock, NEXT)['b'], CONTROL)
            interact(sock, KEY, 'space')
            self.assertEqual(interact(sock, NEXT)['b'], CONTROL)
            interact(sock, KEY, 'up')
            self.assertEqual(interact(sock, NEXT)['b'], EDITABLE)
            self.assertEqual(interact(sock, NEXT)['b'], CONTROL)
            result = interact(sock, KEY, 'enter')
            self.assertEqual(result['a'], SUBMIT)
            self.assertEqual(urlsplit(result['text']).path, '/submit')
            self.assertEqual(parse_qsl(urlsplit(result['text']).query),
                             [('token', 'hidden'), ('added', 'yes'), ('choice', 'b'), ('kind', 'one'), ('go', 'run')])

    def test_edits_survive_stylesheet_arrival_and_relayout(self):
        html = ('<link rel="stylesheet" href="/late.css"><form action="/submit">'
                '<input name="q" value="old"><button>Submit</button></form>')
        with protocol.worker(RENDERER) as (_, sock):
            self.assertEqual(len(protocol.load(sock, html)), 1)
            frame(sock)
            interact(sock, NEXT)
            interact(sock, KEY, 'select-all')
            interact(sock, TEXT, 'kept 猫')
            self.assertEqual(protocol.put_resource(sock, 0, 1, 'input{width:260px}body{color:#334455}', 'text/css'), 1)
            frame(sock, width=320)
            interact(sock, NEXT)
            result = interact(sock, KEY, 'enter')
            self.assertEqual(result['a'], SUBMIT)
            self.assertEqual(parse_qsl(urlsplit(result['text']).query), [('q', 'kept 猫')])

    def test_password_mask_and_post_body_are_separate_from_url(self):
        with protocol.worker(RENDERER) as (_, sock):
            protocol.load(sock, '<form method="post" action="/login?mode=web"><input type="password" name="secret"><button>Login</button></form>')
            frame(sock)
            interact(sock, NEXT)
            interact(sock, TEXT, 'first猫')
            first, _ = frame(sock)
            interact(sock, KEY, 'select-all')
            interact(sock, KEY, 'copy')
            interact(sock, TEXT, 'other犬')
            second, _ = frame(sock)
            self.assertEqual(first, second, 'equal-length passwords must paint identical masks')
            self.assertNotIn('other', protocol.get_text(sock))
            interact(sock, NEXT)
            protocol.send(sock, INTERACT, KEY, text='enter')
            result = protocol.receive(sock)
            self.assertEqual(result['a'], 6)
            self.assertEqual(result['text'], 'https://example.org/login?mode=web')
            self.assertIsNotNone(result['fd'])
            try:
                body = os.pread(result['fd'], 16385, 0).decode()
                self.assertEqual(parse_qsl(body), [('secret', 'other犬')])
            finally:
                os.close(result['fd'])

    def test_unsupported_submissions_produce_notice_without_navigation(self):
        variants = [('put', 'text'), ('get', 'password'), ('get', 'file')]
        for method, kind in variants:
            with self.subTest(method=method, kind=kind), protocol.worker(RENDERER) as (_, sock):
                html = (f'<form method="{method}" action="/unsupported"><input type="{kind}" name="value">'
                        '<button>Submit</button></form><p>Document survives</p>')
                protocol.load(sock, html)
                frame(sock)
                interact(sock, NEXT)
                interact(sock, NEXT)
                result = interact(sock, KEY, 'enter')
                self.assertEqual(result['a'], NOTICE)
                self.assertTrue(result['text'])
                self.assertIn('Document survives', protocol.get_text(sock))

    def test_anchors_find_ids_named_targets_and_missing_fragments(self):
        html = ('<body style="margin:0"><div style="min-height:700px">Top</div>'
                '<h2 id="target">Target section</h2><a name="legacy">Named target</a>'
                '<div style="min-height:500px">After</div></body>')
        with protocol.worker(RENDERER) as (_, sock):
            protocol.load(sock, html)
            frame(sock)
            target = anchor(sock, 'target')
            self.assertEqual(target['a'], 1)
            self.assertGreaterEqual(target['b'], 700)
            named = anchor(sock, 'legacy')
            self.assertEqual(named['a'], 1)
            self.assertGreaterEqual(named['b'], target['b'])
            self.assertEqual(anchor(sock, 'missing')['a'], 0)
            self.assertEqual((anchor(sock, '')['a'], anchor(sock, '')['b']), (1, 0))

    def test_invalid_interaction_descriptor_is_rejected_without_losing_page(self):
        with protocol.worker(RENDERER) as (_, sock):
            protocol.load(sock, '<p>Still alive</p><input name="q">')
            frame(sock)
            with protocol.document('unexpected descriptor') as fd:
                protocol.send(sock, INTERACT, NEXT, fd=fd)
                result = protocol.receive(sock)
                self.assertEqual(result['type'], protocol.ERROR)
                self.assertIsNone(result['fd'])
            self.assertEqual(interact(sock, NEXT)['b'], EDITABLE)
            self.assertIn('Still alive', protocol.get_text(sock))


if __name__ == '__main__':
    unittest.main()
