#!/usr/bin/python3 -I
"""cube-chat checks: working directory handling, tools, undo, history trimming, input
helpers, and an end-to-end run against a fake streaming /chat/completions server.

Uses a scratch HOME; never touches the real ~/.config/cube-chat."""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin/cube-chat"
SCRATCH = tempfile.mkdtemp(prefix="cube-chat-test-")
atexit.register(shutil.rmtree, SCRATCH, True)
os.environ["HOME"] = SCRATCH
os.environ["XDG_STATE_HOME"] = os.path.join(SCRATCH, "state")
os.environ["NO_COLOR"] = "1"

loader = SourceFileLoader("cube_chat", str(SCRIPT))
chat = module_from_spec(spec_from_loader("cube_chat", loader))
loader.exec_module(chat)


class Args:
    api_key = endpoint = model = cwd = None
    no_confirm = False


class Base(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.dir = os.path.realpath(self.tmp.name)
        os.chdir(self.dir)
        self.cfg = chat.load_config(Args())

    def tearDown(self):
        os.chdir(self.old)
        self.tmp.cleanup()

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path


class WorkingDirectory(Base):
    def test_cd_changes_relative_paths_prompt_and_recent(self):
        os.mkdir("proj")
        self.write("proj/a.txt", "hello\n")
        messages = [{"role": "system", "content": chat.system_prompt()}]
        chat.set_workdir(self.cfg, "proj", messages)
        self.assertEqual(os.getcwd(), os.path.join(self.dir, "proj"))
        self.assertIn(os.path.join(self.dir, "proj"), messages[0]["content"])
        self.assertIn("1\thello", chat.tool_read_file({"path": "a.txt"}))
        self.assertEqual(self.cfg["recent_dirs"][0], os.path.join(self.dir, "proj"))
        with open(chat.CONF_PATH) as f:
            saved = json.load(f)
        self.assertEqual(saved["cwd"], os.path.join(self.dir, "proj"))
        self.assertEqual(os.stat(chat.CONF_PATH).st_mode & 0o777, 0o600)
        chat.set_workdir(self.cfg, "-")
        self.assertEqual(os.getcwd(), self.dir)

    def test_cd_rejects_files_and_missing(self):
        self.write("f", "")
        for bad in ("f", "nope"):
            with self.assertRaises(ValueError):
                chat.set_workdir(self.cfg, bad)
        self.assertEqual(os.getcwd(), self.dir)

    def test_initial_workdir_order(self):
        args = Args()
        args.cwd = "/tmp"
        self.assertEqual(chat.initial_workdir(args, self.cfg), "/tmp")
        self.assertEqual(chat.initial_workdir(Args(), self.cfg), self.dir)  # shell's dir wins
        os.chdir(chat.HOME)
        self.cfg["cwd"] = self.dir
        self.assertEqual(chat.initial_workdir(Args(), self.cfg), self.dir)  # launched from ~
        self.cfg["cwd"] = "/does/not/exist"
        self.assertEqual(chat.initial_workdir(Args(), self.cfg), chat.HOME)

    def test_picker_by_number(self):
        os.mkdir("alpha")
        os.mkdir("beta")
        import builtins
        orig = builtins.input
        builtins.input = lambda *_: "2"
        try:
            chat.pick_workdir({"recent_dirs": []}, None)
        finally:
            builtins.input = orig
        self.assertEqual(os.getcwd(), os.path.join(self.dir, "beta"))


class Tools(Base):
    def test_binary_and_non_utf8(self):
        with open("bin", "wb") as f:
            f.write(b"\x00\x01\x02")
        self.assertIn("binary file", chat.tool_read_file({"path": "bin"}))
        with open("latin", "wb") as f:
            f.write(b"caf\xe9\n")
        self.assertIn("caf", chat.tool_read_file({"path": "latin"}))

    def test_find_and_search(self):
        self.write("src/a.py", "def foo():\n    return 1\n")
        self.write("src/b.c", "int foo;\n")
        self.write(".git/x.py", "def foo(): pass\n")
        found = chat.tool_find_files({"pattern": "*.py"})
        self.assertIn("src/a.py", found)
        self.assertNotIn(".git", found)
        hits = chat.tool_search_files({"pattern": r"def\s+foo"})
        self.assertIn("src/a.py:1:", hits)
        self.assertNotIn("b.c", hits)
        self.assertIn("b.c:1:", chat.tool_search_files({"pattern": "FOO", "ignore_case": True, "glob": "*.c"}))
        with self.assertRaises(ValueError):
            chat.tool_search_files({"pattern": "("})

    def test_write_keeps_mode_follows_symlink_and_undo(self):
        path = self.write("run.sh", "old\n")
        os.chmod(path, 0o750)
        os.symlink("run.sh", "link")
        undo = []
        chat.tool_write_file({"path": "link", "content": "new\n"}, undo)
        self.assertTrue(os.path.islink("link"))
        self.assertEqual(open(path).read(), "new\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o750)
        chat.tool_write_file({"path": "fresh.txt", "content": "x"}, undo)
        chat.undo_last(undo)
        self.assertFalse(os.path.exists("fresh.txt"))
        chat.undo_last(undo)
        self.assertEqual(open(path).read(), "old\n")
        self.assertEqual([n for n in os.listdir() if n.startswith(".cube-chat-")], [])

    def test_edit_and_undo(self):
        self.write("f.txt", "a b a")
        undo = []
        with self.assertRaises(ValueError):
            chat.tool_edit_file({"path": "f.txt", "old_string": "a", "new_string": "z"}, undo)
        self.assertIn("2 replacements", chat.tool_edit_file(
            {"path": "f.txt", "old_string": "a", "new_string": "z", "replace_all": True}, undo))
        self.assertEqual(open("f.txt").read(), "z b z")
        chat.undo_last(undo)
        self.assertEqual(open("f.txt").read(), "a b a")

    def test_move_refuses_overwrite_moves_into_dir_and_undo(self):
        self.write("a", "A")
        self.write("b", "B")
        os.mkdir("d")
        undo = []
        with self.assertRaises(FileExistsError):
            chat.tool_move_file({"source": "a", "destination": "b"}, undo)
        self.assertEqual(open("b").read(), "B")
        chat.tool_move_file({"source": "a", "destination": "d"}, undo)
        self.assertTrue(os.path.exists("d/a"))
        chat.undo_last(undo)
        self.assertEqual(open("a").read(), "A")

    def test_run_command_in_workdir(self):
        out = chat.tool_run_command({"command": "pwd; exit 3"})
        self.assertIn("[exit 3]", out)
        self.assertIn(self.dir, out)
        self.assertIn("timed out", chat.tool_run_command({"command": "sleep 5", "timeout": 1}))

    def test_run_tool_errors(self):
        self.assertIn("unknown tool", chat.run_tool("nope", {}, {}))
        self.assertIn("missing argument", chat.run_tool("read_file", {}, {}))


class History(unittest.TestCase):
    def test_trim_drops_whole_turns_and_keeps_current_question(self):
        big = "x" * 40_000
        msgs = [{"role": "system", "content": "s"}]
        for i in range(3):
            msgs += [{"role": "user", "content": f"q{i}"},
                     {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": big}]
        chat.trim_history(msgs)
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "q1")
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        for i in range(4):
            msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": big}]
        chat.trim_history(msgs)
        self.assertEqual(msgs[1]["content"], "q")
        self.assertIn("trimmed", msgs[3]["content"])
        self.assertLessEqual(sum(chat.msg_size(m) for m in msgs), chat.MAX_HISTORY_CHARS)


class Input(Base):
    def test_mentions_attach_files(self):
        self.write("notes.md", "secret plan")
        out = chat.attach_mentions("look at @notes.md, and @missing")
        self.assertIn('<attached_file path="%s/notes.md">' % self.dir, out)
        self.assertIn("secret plan", out)
        self.assertNotIn("missing\"", out)

    def test_command_detection(self):
        self.assertTrue(chat.is_command("/cd foo"))
        self.assertTrue(chat.is_command("/undo"))
        self.assertFalse(chat.is_command("/usr/bin/foo is broken"))

    def test_multiline(self):
        import builtins
        lines = iter(['"""', "one", "two", '"""'])
        orig = builtins.input
        builtins.input = lambda *_: next(lines)
        try:
            self.assertEqual(chat.read_input("> "), "one\ntwo")
            lines = iter(["a\\", "b"])
            self.assertEqual(chat.read_input("> "), "a\nb")
        finally:
            builtins.input = orig

    def test_shell_output_queued_for_next_message(self):
        session = {}
        chat.shell("echo hi", session)
        self.assertIn("hi", session["context"][0])
        self.assertIn('command="echo hi"', session["context"][0])


# ---------- end to end against a fake endpoint ----------

def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


class Fake(BaseHTTPRequestHandler):
    requests = []
    script = []
    fail_first = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.requests.append(body)
        if Fake.fail_first:
            Fake.fail_first -= 1
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"busy"}}')
            return
        if not Fake.script:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"model nope not found"}}')
            return
        payload = Fake.script.pop(0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(payload.encode())


def tool_call(name, args):
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {
        "name": name, "arguments": json.dumps(args)}}]}}]}


def text(t):
    return {"choices": [{"delta": {"content": t}}]}


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_chat(self, cwd, *extra, stdin=None):
        env = dict(os.environ, HOME=SCRATCH, NO_COLOR="1")
        return subprocess.run([sys.executable, str(SCRIPT), "--endpoint", self.url, "--model", "m",
                               "--no-confirm", "--cwd", cwd, *extra], input=stdin,
                              capture_output=True, text=True, env=env, timeout=30)

    def test_tool_loop_in_working_dir(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
            Fake.requests.clear()
            Fake.fail_first = 1
            Fake.script = [sse(tool_call("write_file", {"path": "out.txt", "content": "made here"})),
                           sse(text("done"))]
            p = self.run_chat(d, "--prompt", "make a file")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(open(os.path.join(d, "out.txt")).read(), "made here")
            self.assertIn("retrying", p.stdout)
            self.assertIn("done", p.stdout)
            self.assertIn(os.path.realpath(d), Fake.requests[-1]["messages"][0]["content"])
            self.assertEqual(Fake.requests[-1]["messages"][-1]["role"], "tool")
            names = {t["function"]["name"] for t in Fake.requests[-1]["tools"]}
            self.assertTrue({"find_files", "search_files", "run_command"} <= names)

    def test_error_body_is_shown(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
            Fake.script = []
            p = self.run_chat(d, "--prompt", "-", stdin="hi")
            self.assertEqual(p.returncode, 1)
            self.assertIn("model nope not found", p.stdout)

    def test_interactive_cd_and_unknown_command(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
            os.mkdir(os.path.join(d, "sub"))
            Fake.script = [sse(text("ok"))]
            p = self.run_chat(d, stdin="/cd sub\n/pwd\n/bogus\nhello\n/status\n/quit\n")
            self.assertIn(os.path.join(os.path.realpath(d), "sub"), p.stdout)
            self.assertIn("unknown command /bogus", p.stdout)
            self.assertIn("context 2 messages", p.stdout)
            last = json.load(open(os.path.join(SCRATCH, "state/cube-chat/last.json")))
            self.assertEqual(last["cwd"], os.path.join(os.path.realpath(d), "sub"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
