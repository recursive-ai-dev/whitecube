#!/usr/bin/python3 -I
"""Offline tests for the workspace suite (workspace/server.py, health.py, jobs.py, recovery.py, desktop_state.py)."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO / "workspace"
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import desktop_state  # noqa: E402
import health  # noqa: E402
import jobs  # noqa: E402
import recovery  # noqa: E402
import server  # noqa: E402


class WorkspaceCliAndSafeFileTests(unittest.TestCase):
    def test_cli_version_and_strict_check(self):
        for script, expected in ((WORKSPACE / "server.py", "whitecube-web"), (WORKSPACE / "health.py", "cube-check")):
            done = subprocess.run([sys.executable, "-I", str(script), "--version"], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(expected, done.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            projects.mkdir()
            clean = subprocess.run([sys.executable, "-I", str(WORKSPACE / "server.py"), "check", "--projects", str(projects), "--strict"],
                                   capture_output=True, text=True)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            data = json.loads(clean.stdout)
            self.assertEqual(data["manifest_errors"], [])

            bad = projects / "broken-app"
            bad.mkdir()
            (bad / "whitecube.project.json").write_text("{not valid json")
            strict = subprocess.run([sys.executable, "-I", str(WORKSPACE / "server.py"), "check", "--projects", str(projects), "--strict"],
                                    capture_output=True, text=True)
            self.assertEqual(strict.returncode, 1)
            self.assertTrue(json.loads(strict.stdout)["manifest_errors"])

    def test_safe_file_and_catalog_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dist"
            outside = Path(tmp) / "secret.txt"
            outside.write_text("secret")
            root.mkdir()
            (root / "index.html").write_text("<h1>ok</h1>")
            (root / ".env").write_text("TOKEN=1")
            (root / "escape.html").symlink_to(outside)

            self.assertEqual(server.safe_file(root, "index.html"), (root / "index.html").resolve())
            for bad in ("", ".", "..", ".env", "../secret.txt", "a\\b", "a\x00b", "escape.html", "/etc/passwd"):
                with self.subTest(bad=bad), self.assertRaises(server.Problem):
                    server.safe_file(root, bad)

            projects_dir = Path(tmp) / "projects"
            app_dir = projects_dir / "demo/dist"
            app_dir.mkdir(parents=True)
            (app_dir / "index.html").write_text("<title>Demo</title>")
            (projects_dir / "demo/whitecube.project.json").write_text(json.dumps({
                "version": 1, "id": "demo", "name": "Demo", "dist": "dist", "project_mode": "rw",
                "commands": {"build": {"command": ["make"], "container": "build"}},
            }))
            public = server.Catalog(projects_dir).public(8788)
            demo = next(p for p in public["projects"] if p["id"] == "demo")
            self.assertEqual(demo["url"], "http://127.0.0.1:8788/apps/demo/index.html")
            self.assertNotIn("path", demo)
            self.assertNotIn("command", demo["commands"]["build"])
            self.assertIn("sharing_scope", demo)


class DesktopWorkflowRecoveryTests(unittest.TestCase):
    def test_desktop_store_validation_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            store = desktop_state.DesktopStore(state)
            self.assertEqual(store.save_settings({"ui_scale": 1.25, "high_contrast": True})["contrast"], "strong")
            with self.assertRaises(ValueError):
                store.save_settings({"ui_scale": 9.0})
            layout = {
                "version": 1, "workspace": 0,
                "workspaces": [{"name": f"W{i}", "panes": [{"id": "p1", "session": None}], "sizes": [1],
                                "terminalBounds": {"x": 2, "y": 10, "width": 80, "height": 60}} for i in range(4)],
            }
            self.assertEqual(store.save_layout(layout)["workspace"], 0)
            layout["workspaces"][0]["name"] = "bad\x00name"
            with self.assertRaises(ValueError):
                store.save_layout(layout)
            (state / "desktop-layout.json").unlink()
            (state / "desktop-layout.json").symlink_to(Path(tmp) / "evil")
            with self.assertRaises(ValueError):
                store.get_layout()

    def test_workflow_store_dag_ordering_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = jobs.WorkflowStore(Path(tmp))
            wf = store.save({
                "id": "build-test", "name": "Build and test",
                "steps": [
                    {"id": "test", "script": "system-status", "depends_on": ["build"]},
                    {"id": "build", "script": "system-status", "depends_on": []},
                ],
            })
            self.assertEqual([s["id"] for s in wf["steps"]], ["build", "test"])
            with self.assertRaises(ValueError):
                jobs.validate_workflow({"id": "cyc", "steps": [
                    {"id": "a", "script": "system-status", "depends_on": ["b"]},
                    {"id": "b", "script": "system-status", "depends_on": ["a"]},
                ]})
            self.assertEqual(store.delete("build-test"), {"id": "build-test", "deleted": True})
            self.assertEqual(store.list(), [])
            with self.assertRaises(KeyError):
                store.delete("build-test")

    def test_recovery_console_create_verify_review_restore_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            projects = Path(tmp) / "projects"
            projects.mkdir()
            store = desktop_state.DesktopStore(state)
            store.save_settings({"ui_scale": 1.1, "contrast": "strong"})
            (state / "ca-seeds.json").write_text(json.dumps({"api_token": "secret-token-value"}))
            console = recovery.RecoveryConsole(projects=projects, state=state)
            created = console.create("baseline", retention=5)
            self.assertTrue(any(e["path"] == "state/ca-seeds.json" for e in created["excluded"]))
            store.save_settings({"ui_scale": 0.9, "contrast": "normal"})
            review = console.review(created["id"])
            restored = console.restore(created["id"], review["review_token"])
            self.assertGreaterEqual(restored["restored"], 1)
            self.assertEqual(store.get_settings()["contrast"], "strong")
            self.assertEqual(console.verify(created["id"])["verified"], True)
            self.assertEqual(console.delete(created["id"]), {"id": created["id"], "deleted": True})


class HealthAndNetworkFallbackTests(unittest.TestCase):
    def test_health_network_checks_support_generic_wifi_adapters(self):
        fake_check = {"dependencies_ready": True}
        fake_status = {
            "present": True,
            "interface": {"interface": "wlp2s0", "bus": "pci", "driver": "iwlwifi", "mac": "aa:bb:cc:dd:ee:01"},
            "device": {"mode": "auto"},
            "association_state": "COMPLETED",
            "ipv4_address": "192.168.1.50",
        }
        with mock.patch.object(health, "helper_json", side_effect=[fake_check, fake_status]):
            rows = health.network_checks()
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["wifi-dependencies"]["state"], "pass")
        self.assertEqual(by_id["wifi-adapter"]["state"], "pass")
        self.assertIn("iwlwifi", by_id["wifi-adapter"]["detail"])
        self.assertIn("pci", by_id["wifi-adapter"]["detail"])
        self.assertIn("192.168.1.50", by_id["wifi-connection"]["detail"])
        with mock.patch.object(server, "BASE", Path("/nonexistent-whitecube-dir")), \
                mock.patch.object(Path, "is_file", return_value=False):
            fb = server.network_status()
        self.assertEqual(fb["state"], "unknown")
        self.assertNotIn("2001:3329", json.dumps(fb))
        self.assertNotIn("rtw88_8822cu", json.dumps(fb))

    def test_network_status_script_and_frontend_syntax(self):
        sh_res = subprocess.run(["bash", "-n", str(WORKSPACE / "bin/network-status.sh")], capture_output=True, text=True)
        self.assertEqual(sh_res.returncode, 0, sh_res.stderr)
        self.assertNotIn("2001:3329", (WORKSPACE / "catalog.json").read_text())
        node = os.environ.get("NODE") or shutil.which("node")
        if node:
            js_res = subprocess.run([node, "--check", str(WORKSPACE / "web/app.js")], capture_output=True, text=True)
            self.assertEqual(js_res.returncode, 0, js_res.stderr)


class HttpApiServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir()
        app_dist = self.projects / "sample/dist"
        app_dist.mkdir(parents=True)
        (app_dist / "index.html").write_text("<html><script src=\"/assets/app.js\"></script></html>")
        (self.projects / "sample/whitecube.project.json").write_text(json.dumps({"version": 1, "id": "sample", "name": "Sample", "path": "sample/dist"}))
        self.env_patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.tmp.name) / "xdg")})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.work = server.Workspace(self.projects, port=0, apps_port=0, token="t" * 43, persistent=False)
        self.main_srv = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.apps_srv = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.work.port = self.main_srv.server_address[1]
        self.work.apps_port = self.apps_srv.server_address[1]
        self.main_srv.workspace, self.main_srv.apps = self.work, False
        self.apps_srv.workspace, self.apps_srv.apps = self.work, True
        for srv in (self.main_srv, self.apps_srv):
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
        self.addCleanup(self.work.close)
        self.addCleanup(lambda: (self.main_srv.shutdown(), self.main_srv.server_close(),
                                 self.apps_srv.shutdown(), self.apps_srv.server_close()))

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = {"Host": f"127.0.0.1:{port}", "X-Whitecube-Token": self.work.token}
        if body is not None:
            raw = json.dumps(body) if isinstance(body, dict) else body
            hdrs["Content-Type"] = "application/json"
        else:
            raw = None
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def test_auth_headers_settings_workflows_backups_and_apps_origin(self):
        # Auth & origin protections
        status, _, _ = self.request(self.work.port, "GET", "/api/catalog", headers={"X-Whitecube-Token": "wrong"})
        self.assertEqual(status, 401)
        status, _, _ = self.request(self.work.port, "GET", "/api/catalog", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request(self.work.port, "GET", "/api/catalog", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request(self.work.port, "GET", "/api/catalog", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

        # Static index & headers
        status, hdrs, _ = self.request(self.work.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs.get("X-Frame-Options"), "DENY")

        # Catalog + Settings
        with mock.patch.object(server, "network_status", return_value={"state": "unknown", "device": {"mode": "auto"}}):
            status, _, data = self.request(self.work.port, "GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["network"]["device"]["mode"], "auto")
        status, _, data = self.request(self.work.port, "POST", "/api/settings", {"ui_scale": 1.2})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["ui_scale"], 1.2)

        # Workflows save & delete
        wf = {"id": "smoke", "name": "Smoke", "steps": [{"id": "s1", "script": "system-status", "depends_on": []}]}
        status, _, _ = self.request(self.work.port, "POST", "/api/workflows", wf)
        self.assertEqual(status, 200)
        status, _, data = self.request(self.work.port, "POST", "/api/workflows/delete", {"id": "smoke"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["deleted"])
        status, _, _ = self.request(self.work.port, "POST", "/api/workflows/delete", {"id": "smoke"})
        self.assertEqual(status, 404)

        # Backups create, verify, delete, and 404 on nonexistent ID
        status, _, data = self.request(self.work.port, "POST", "/api/backups", {"label": "b1", "retention": 5})
        self.assertEqual(status, 200)
        bid = json.loads(data)["id"]
        status, _, data = self.request(self.work.port, "POST", "/api/backups/verify", {"id": bid})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["verified"])
        status, _, _ = self.request(self.work.port, "POST", "/api/backups/delete", {"id": bid})
        self.assertEqual(status, 200)
        status, _, _ = self.request(self.work.port, "POST", "/api/backups/verify", {"id": bid})
        self.assertEqual(status, 404)

        # Separate app origin asset serving rewrites /assets/ cleanly
        status, hdrs, data = self.request(self.work.apps_port, "GET", "/apps/sample/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b'"/apps/sample/assets/app.js"', data)
        self.assertIn(f"http://127.0.0.1:{self.work.port}", hdrs.get("Content-Security-Policy", ""))

        # Action routing for network-devices and invalid action rejection
        status, _, _ = self.request(self.work.port, "POST", "/api/action", {"action": "network-connect", "profile": "bad!"})
        self.assertEqual(status, 400)
        with mock.patch.object(self.work, "new_terminal", return_value={"id": "0" * 32, "run_id": None, "name": "Network Devices"}) as mocked:
            status, _, _ = self.request(self.work.port, "POST", "/api/action", {"action": "network-devices"})
        self.assertEqual(status, 200)
        self.assertEqual(mocked.call_args.kwargs["command"], ["/usr/bin/cube-net", "devices"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
