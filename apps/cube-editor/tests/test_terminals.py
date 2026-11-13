"""Desktop terminal selection and authenticated-worker lifecycle coverage."""
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from test_cube_editor import ProjectFixture


class TerminalSelectionTests(unittest.TestCase):
    def choose(self, available, environment=None, command=None):
        with mock.patch.dict(os.environ, environment or {}, clear=True), \
             mock.patch('engine.shutil.which', side_effect=lambda name: '/usr/bin/' + name if name in available else None):
            return engine.terminal_command(command or ['sudo', '--', 'worker'], 'Image operation')

    def test_foot_requires_wayland_and_x11_uses_qterminal(self):
        self.assertEqual(self.choose({'foot', 'qterminal'})[:2], ['/usr/bin/qterminal', '-e'])
        self.assertEqual(self.choose({'foot', 'qterminal'}, {'WAYLAND_DISPLAY': 'wayland-0'})[:3],
                         ['/usr/bin/foot', '--title', 'Image operation'])

    def test_server_based_terminals_wait_for_dedicated_worker(self):
        for name, flag in [('gnome-terminal', '--wait'), ('konsole', '--separate'),
                           ('xfce4-terminal', '--disable-server')]:
            with self.subTest(name=name):
                self.assertIn(flag, self.choose({name}))

    def test_arguments_with_spaces_and_shell_characters_are_kept_literal(self):
        command = ['sudo', '--', '/path with spaces/worker', '--project', '/tmp/$(false); space']
        for name in engine.TERMINALS:
            with self.subTest(name=name):
                argv = self.choose({name}, {'WAYLAND_DISPLAY': 'wayland-0'}, command)
                self.assertEqual(argv[-len(command):], command)

    def test_explicit_terminal_is_used_without_silent_fallback(self):
        result = self.choose({'qterminal', 'xterm'}, {'CUBE_EDITOR_TERMINAL': 'xterm'})
        self.assertEqual(result[0], '/usr/bin/xterm')
        for override in ('missing', 'sh -c evil', 'foot'):
            with self.subTest(override=override), self.assertRaisesRegex(engine.CubeError, 'CUBE_EDITOR_TERMINAL'):
                self.choose({'foot', 'qterminal'}, {'CUBE_EDITOR_TERMINAL': override})

    def test_missing_terminal_is_actionable_and_visible_in_readiness(self):
        with self.assertRaisesRegex(engine.CubeError, 'Install QTerminal'):
            self.choose(set())
        with mock.patch('engine.shutil.which', return_value=None):
            checks = engine.dependencies()
        terminal = next(item for item in checks if item['name'] == 'terminal')
        self.assertFalse(terminal['available'])
        self.assertTrue(terminal['required'])

    def test_builtin_terminal_is_opt_in_and_checks_vte_availability(self):
        import cube_terminal
        with mock.patch.object(cube_terminal, 'builtin_available', return_value=False):
            with self.assertRaisesRegex(engine.CubeError, 'gir1.2-vte-3.91'):
                self.choose({'foot', 'qterminal'}, {'CUBE_EDITOR_TERMINAL': 'builtin'})
        with mock.patch.object(cube_terminal, 'builtin_available', return_value=True):
            argv = self.choose(set(), {'CUBE_EDITOR_TERMINAL': 'builtin'})
            self.assertEqual(argv[:2], ['__CUBE_TERMINAL__', 'Image operation'])


class TerminalLifecycleTests(ProjectFixture):
    def test_each_terminal_requires_worker_completion_even_on_zero_exit(self):
        for name in (*engine.TERMINALS, 'x-terminal-emulator'):
            terminal = self.path / name
            terminal.symlink_to(sys.executable)
            with self.subTest(name=name), self.assertRaisesRegex(engine.CubeError, 'Worker did not finish'):
                engine.Engine(self.project).run([str(terminal), '-c', 'pass'])

    def test_cancellation_allows_authenticated_worker_to_clean_up(self):
        terminal = self.path / 'qterminal'
        terminal.symlink_to(sys.executable)
        api = engine.Engine(self.project)
        events = self.project.directory / 'events.jsonl'
        cancel = self.project.directory / '.cancel'
        cleaned = self.project.directory / 'cleaned'
        started = {'type': 'started', 'job': api.job_id, 'pid': os.getpid()}
        finished = {'type': 'finished', 'job': api.job_id, 'success': False, 'cancelled': True}
        script = ('from pathlib import Path; import time\n'
                  f'Path({str(events)!r}).write_text({json.dumps(started) + chr(10)!r})\n'
                  f'while not Path({str(cancel)!r}).exists(): time.sleep(.01)\n'
                  f'Path({str(cleaned)!r}).touch()\n'
                  f'with Path({str(events)!r}).open("a") as log: log.write({json.dumps(finished) + chr(10)!r})\n')
        timer = threading.Timer(.3, api.cancel)
        timer.start()
        try:
            with self.assertRaises(engine.Cancelled):
                api.run([str(terminal), '-c', script])
        finally:
            timer.cancel()
        self.assertTrue(cleaned.exists(), 'Cancellation killed the worker before cleanup')
        self.assertIsNone(api.process)

    def test_builtin_terminal_tails_events_including_final_line(self):
        import cube_terminal
        api = engine.Engine(self.project)
        events = self.project.directory / 'events.jsonl'
        seen = []
        api.on_event = seen.append

        def fake_run(command, title, cancel_event=None, worker_started_cb=None):
            events.write_text(json.dumps({'type': 'started', 'job': api.job_id}) + '\n')
            events.write_text(
                json.dumps({'type': 'started', 'job': api.job_id}) + '\n'
                + json.dumps({'type': 'finished', 'job': api.job_id, 'success': True})
            )
            return 0

        with mock.patch.object(cube_terminal, 'run_in_terminal', side_effect=fake_run):
            result = api.run(['__CUBE_TERMINAL__', 'Auth', 'echo', 'ok'])
        self.assertTrue(result['success'])
        self.assertTrue(any(ev.get('type') == 'started' for ev in seen))

    def test_builtin_terminal_exit_without_finished_fails(self):
        import cube_terminal
        api = engine.Engine(self.project)
        with mock.patch.object(cube_terminal, 'run_in_terminal', return_value=0):
            with self.assertRaisesRegex(engine.CubeError, 'Worker did not finish'):
                api.run(['__CUBE_TERMINAL__', 'Auth', 'echo', 'ok'])


if __name__ == '__main__':
    unittest.main()
