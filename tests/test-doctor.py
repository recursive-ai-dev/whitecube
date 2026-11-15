"""Tests for whitecube-doctor against fake filesystem roots. Run: python3 -I test-doctor.py"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("doctor", str(HERE / "bin" / "whitecube-doctor"))
doctor = importlib.util.module_from_spec(importlib.util.spec_from_loader("doctor", loader))
loader.exec_module(doctor)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root, self.home, self.project = base / "root", base / "home", base / "project"
        for d in (self.root, self.home, self.project):
            d.mkdir()
        self.env = doctor.Env(self.project, self.root, self.home)

    def put(self, path, text, base=None):
        target = (base or self.root) / str(path).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def levels(self, findings):
        return {f["name"]: f["level"] for f in findings}


class FirewallTests(Fixture):
    def test_enabled_with_dropped_output_is_a_failure_with_the_remedy(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="DROP"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=yes\n")
        found = doctor.check_firewall(self.env)[0]
        self.assertEqual(found["level"], "FAIL")
        self.assertIn("ufw default allow outgoing", found["fix"])

    def test_disabled_with_dropped_output_is_a_warning_because_enabling_would_cut_the_network(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="REJECT"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=no\n")
        self.assertEqual(doctor.check_firewall(self.env)[0]["level"], "WARN")

    def test_allowed_output_is_fine_and_missing_ufw_is_only_informational(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="ACCEPT"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=yes\n")
        self.assertEqual(doctor.check_firewall(self.env)[0]["level"], "ok")
        (self.root / "etc/default/ufw").unlink()
        self.assertEqual(doctor.check_firewall(self.env)[0]["level"], "info")


class NetworkTests(Fixture):
    def net(self):
        return mock.Mock(status=mock.Mock(return_value={"diagnostic_code": "online"}))

    def status(self, **kw):
        data = {"association_state": "COMPLETED", "signal_dbm": -55, "ipv4_address": "10.0.0.2", "dns_ok": True,
                "connectivity_ok": True, "updated_epoch": time.time(), **kw}
        self.put("run/whitecube-net/status.json", json.dumps(data))

    def test_a_healthy_link(self):
        self.status()
        found = doctor.check_network(self.env, self.net())[0]
        self.assertEqual(found["level"], "ok")
        self.assertIn("online", found["detail"])

    def test_no_status_file_means_the_service_is_not_running(self):
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "WARN")

    def test_a_stale_status_file_is_a_failure(self):
        self.status(updated_epoch=time.time() - 300)
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "FAIL")

    def test_connected_but_no_internet_is_a_warning(self):
        self.status(connectivity_ok=False)
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "WARN")

    def test_not_associated_is_a_warning(self):
        self.status(association_state="SCANNING")
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "WARN")

    def test_a_crashing_status_call_does_not_hide_the_link_state(self):
        self.status()
        broken = mock.Mock(status=mock.Mock(side_effect=ValueError))
        self.assertEqual(doctor.check_network(self.env, broken)[0]["level"], "ok")

    def test_without_cube_net_it_says_so(self):
        self.assertEqual(doctor.check_network(self.env, None)[0]["level"], "WARN")


class FolderHandlerTests(Fixture):
    def handler(self, mimeapps=None, desktop=None, binary=False):
        if mimeapps is not None:
            (self.home / '.config').mkdir(parents=True, exist_ok=True)
            (self.home / '.config/mimeapps.list').write_text(mimeapps)
        if desktop:
            folder = self.root / 'usr/share/applications'; folder.mkdir(parents=True, exist_ok=True)
            (folder / desktop).write_text('[Desktop Entry]\nExec=thunar %U\n')
        if binary:
            (self.root / 'usr/bin').mkdir(parents=True, exist_ok=True); (self.root / 'usr/bin/thunar').write_text('')
        return doctor.check_folder_handler(self.env)[0]

    def test_no_default_is_a_warning_with_the_command_to_set_one(self):
        result = self.handler()
        self.assertEqual(result['level'], 'WARN'); self.assertIn('xdg-mime default', result['fix'])

    def test_a_default_whose_program_is_missing_is_named(self):
        result = self.handler('[Default Applications]\ninode/directory=thunar.desktop\n', 'thunar.desktop')
        self.assertEqual(result['level'], 'WARN'); self.assertIn('thunar', result['detail'])

    def test_an_installed_default_is_fine(self):
        result = self.handler('[Default Applications]\ninode/directory=thunar.desktop\n', 'thunar.desktop', binary=True)
        self.assertEqual((result['level'], result['detail']), ('ok', 'folders open with thunar.desktop'))
        (self.root / 'usr/share/applications/thunar.desktop').write_text('[Desktop Entry]\nExec=env GTK_THEME=Adwaita "/usr/bin/thunar" %F\n')
        result = doctor.check_folder_handler(self.env)[0]
        self.assertEqual((result['level'], result['detail']), ('ok', 'folders open with thunar.desktop'))


class OtherChecks(Fixture):
    def test_rival_managers_are_named_and_dhcpcd_is_not_a_rival(self):
        net = mock.Mock(competing_processes=mock.Mock(return_value=["dhcpcd", "NetworkManager"]))
        found = doctor.check_competitors(self.env, net)[0]
        self.assertEqual(found["level"], "WARN")
        self.assertIn("NetworkManager", found["detail"])
        self.assertNotIn("dhcpcd", found["detail"])
        net.competing_processes.return_value = ["dhcpcd"]
        self.assertEqual(doctor.check_competitors(self.env, net)[0]["level"], "ok")

    def test_older_copies_are_reported_only_once_the_package_is_installed(self):
        self.put('/usr/local/bin/cube-status', 'old')
        self.assertEqual(doctor.check_shadowing(self.env), [])
        self.put('/usr/bin/whitecube-compositor', 'new')
        found = doctor.check_shadowing(self.env)[0]
        self.assertEqual(found['level'], 'WARN')
        self.assertIn('whitecube-migrate', found['fix'])
        (self.root / 'usr/local/bin/cube-status').unlink()
        self.assertEqual(doctor.check_shadowing(self.env)[0]['level'], 'ok')

    def test_leftover_wpa_supplicant_is_advice_not_an_alarm(self):
        self.assertEqual(doctor.check_wpa_leftovers(self.env)[0]["level"], "ok")
        self.put("usr/sbin/wpa_supplicant", "")
        found = doctor.check_wpa_leftovers(self.env)[0]
        self.assertEqual(found["level"], "info")
        self.assertIn("apt purge wpasupplicant", found["fix"])

    def test_drift_reports_stale_and_missing_installed_copies_and_ignores_unbuilt_sources(self):
        self.put("net/cube-net", "new", self.project)
        self.put("usr/bin/cube-net", "old")
        self.put("net/cube-net-dns", "same", self.project)
        self.put("usr/libexec/whitecube/cube-net-dns", "same")
        self.put("data/foot.ini", "x", self.project)                 # source present, nothing installed
        found = doctor.check_drift(self.env)[0]
        self.assertEqual(found["level"], "WARN")
        self.assertIn("differs from source: cube-net", found["detail"])
        self.assertIn("not installed: foot.ini", found["detail"])
        self.assertNotIn("cube-net-dns", found["detail"])
        self.assertNotIn("whitecube,", found["detail"])         # build/ outputs do not exist, so they are not compared

    def test_matching_installs_are_ok(self):
        self.put("net/cube-net", "same", self.project)
        self.put("usr/bin/cube-net", "same")
        self.assertEqual(doctor.check_drift(self.env)[0]["level"], "ok")

    def test_the_drift_check_never_installs_anything(self):
        self.put("net/cube-net", "new", self.project)
        doctor.check_drift(self.env)
        self.assertFalse((self.root / "usr/bin/cube-net").exists())

    def test_group_wide_passwordless_sudo_is_pointed_out(self):
        self.put("data/sudoers/whitecube-settings", "%sudo ALL=(root) NOPASSWD: CUBE_FREE\n", self.project)
        self.assertEqual(doctor.check_sudoers(self.env)[0]["level"], "info")
        self.put("data/sudoers/whitecube-settings", "%sudo ALL=(root) /usr/local/libexec/x\n", self.project)
        self.assertEqual(doctor.check_sudoers(self.env)[0]["level"], "ok")

    def test_x11_libraries_in_installed_binaries_are_flagged_with_the_fix(self):
        for name in ("whitecube", "cube-status"):
            self.put(f".local/bin/{name}", "", self.home)
        for text, level in (("libxcb.so.1 => /lib/libxcb.so.1\n", "WARN"), ("libwayland-client.so.0 => /lib/x\n", "ok")):
            with mock.patch.object(doctor.subprocess, "run", return_value=mock.Mock(stdout=text, returncode=0)):
                found = doctor.check_xfree(self.env)[0]
            self.assertEqual(found["level"], level)
        with mock.patch.object(doctor.subprocess, "run", return_value=mock.Mock(stdout="libX11.so.6 => /lib/x\n", returncode=0)):
            self.assertIn("build-xfree-cairo.sh", doctor.check_xfree(self.env)[0]["fix"])

    def test_no_installed_binaries_means_nothing_to_flag(self):
        self.assertEqual(doctor.check_xfree(self.env)[0]["level"], "ok")

    def test_x11_in_the_environment_is_flagged(self):
        with mock.patch.dict(doctor.os.environ, {"DISPLAY": ":0"}):
            self.assertEqual(doctor.check_session(self.env)[0]["level"], "WARN")


class PackageChecks(Fixture):
    def package(self):
        executables = ('usr/bin/whitecube-compositor', 'usr/bin/whitecube-session',
                       'usr/bin/cube-status', 'usr/bin/cube-settings', 'usr/bin/cube-ca',
                       'usr/bin/cube-audio', 'usr/bin/whitecube-terminal', 'usr/bin/desktop-chat',
                       'usr/libexec/whitecube/cube-settings-helper',
                       'usr/lib/whitecube/desktop-chat-runtime/electron')
        data = ('usr/share/wayland-sessions/whitecube.desktop',
                'usr/share/whitecube/schemas/gschemas.compiled', 'etc/whitecube/config',
                'usr/share/whitecube/desktop-chat/main.js')
        for name in executables + data:
            target = self.put(name, '')
            if name in executables:
                target.chmod(0o755)

    def test_a_complete_package_has_no_failure(self):
        self.package()
        with mock.patch.object(doctor.subprocess, 'run', return_value=mock.Mock(stdout='libc.so.6 => /lib/libc.so.6', returncode=0)):
            found = doctor.check_package(self.env)
        self.assertEqual([item['level'] for item in found], ['ok'])

    def test_partial_package_reports_missing_compositor_and_unexecutable_panel(self):
        self.package()
        (self.root / 'usr/bin/whitecube-compositor').unlink()
        (self.root / 'usr/bin/cube-status').chmod(0o644)
        with mock.patch.object(doctor.subprocess, 'run', return_value=mock.Mock(stdout='', returncode=0)):
            found = doctor.check_package(self.env)[0]
        self.assertEqual(found['level'], 'FAIL')
        self.assertIn('whitecube-compositor', found['detail'])
        self.assertIn('cube-status', found['detail'])

    def test_missing_transitive_libraries_name_binary_and_soname(self):
        self.package()
        with mock.patch.object(doctor.subprocess, 'run', return_value=mock.Mock(stdout='libwlroots.so.12 => not found\n', returncode=0)):
            found = doctor.check_package(self.env)[-1]
        self.assertEqual(found['level'], 'FAIL')
        self.assertIn('whitecube-compositor: libwlroots.so.12', found['detail'])

    def test_chat_runtime_dependencies_are_checked(self):
        self.package()

        def inspect(command, **kwargs):
            output = 'libasound.so.2 => not found\n' if command[1].endswith('/electron') else 'libc.so.6 => /lib/libc.so.6\n'
            return mock.Mock(stdout=output, returncode=0)

        with mock.patch.object(doctor.subprocess, 'run', side_effect=inspect):
            findings = doctor.check_package(self.env)
        self.assertEqual(findings[-1]['level'], 'FAIL')
        self.assertIn('desktop-chat: libasound.so.2', findings[-1]['detail'])

    def test_failed_ldd_probes_are_reported_as_unknown_instead_of_healthy(self):
        self.package()
        for failure in (mock.Mock(stdout='', returncode=1), OSError('ldd unavailable')):
            patch = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(failure=failure), mock.patch.object(doctor.subprocess, 'run', **patch):
                libraries = doctor.check_xfree(self.env)
                package = doctor.check_package(self.env)
            self.assertEqual(libraries[0]['level'], 'WARN')
            self.assertIn('could not inspect', libraries[0]['detail'])
            self.assertTrue(any(item['level'] == 'WARN' and item['name'] == 'runtime libraries' for item in package))

    def test_packaged_drift_uses_system_paths_and_preserves_user_config(self):
        self.put('usr/bin/whitecube-compositor', 'new')
        self.put('build/whitecube', 'new', self.project)
        self.put('build/cube-status', 'new', self.project)
        self.put('usr/bin/cube-status', 'old')
        self.put('net/whitecube-network.service', 'unit', self.project)
        self.put('usr/lib/systemd/system/whitecube-network.service', 'unit')
        self.put('data/foot.ini', 'default', self.project)
        found = doctor.check_drift(self.env)[0]
        self.assertEqual(found['level'], 'WARN')
        self.assertIn('cube-status', found['detail'])
        self.assertNotIn('whitecube-compositor', found['detail'])
        self.assertNotIn('whitecube-network', found['detail'])
        self.assertNotIn('foot.ini', found['detail'])
        self.assertIn('Debian package', found['fix'])
        self.assertNotIn('INSTALL-USER', found['fix'])

    def test_unavailable_source_tree_does_not_claim_to_match(self):
        found = doctor.check_drift(self.env)[0]
        self.assertEqual(found['level'], 'info')
        self.assertIn('skipped', found['detail'])

    def test_packaged_library_check_inspects_system_binaries(self):
        self.put('usr/bin/whitecube-compositor', '')
        with mock.patch.object(doctor.subprocess, 'run', return_value=mock.Mock(stdout='libX11.so => /lib/libX11.so', returncode=0)) as run:
            found = doctor.check_xfree(self.env)[0]
        self.assertEqual(found['level'], 'WARN')
        self.assertEqual(run.call_args.args[0][1], str(self.root / 'usr/bin/whitecube-compositor'))
        self.assertIn('Debian package', found['fix'])

    def test_package_does_not_confuse_uninstalled_sudoers_example_with_active_rule(self):
        self.put('usr/bin/whitecube-compositor', '')
        self.put('data/sudoers/whitecube-settings', '%sudo ALL=(root) NOPASSWD: CUBE_FREE\n', self.project)
        found = doctor.check_sudoers(self.env)[0]
        self.assertEqual(found['detail'], 'sudoers rule file not found')


class SessionChecks(Fixture):
    def test_non_desktop_shell_is_informational(self):
        with mock.patch.dict(doctor.os.environ, {}, clear=True):
            self.assertEqual(doctor.check_session(self.env)[0]['level'], 'info')

    def test_wayland_without_runtime_is_a_failure(self):
        with mock.patch.dict(doctor.os.environ, {'XDG_SESSION_TYPE': 'wayland'}, clear=True):
            self.assertEqual(doctor.check_session(self.env)[0]['level'], 'FAIL')

    def test_world_accessible_runtime_is_a_failure(self):
        runtime = self.home / 'runtime'
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)  # Exercise permissions independently of the builder's umask.
        with mock.patch.dict(doctor.os.environ, {'XDG_RUNTIME_DIR': str(runtime)}, clear=True):
            self.assertEqual(doctor.check_session(self.env)[0]['level'], 'FAIL')

    def test_runtime_symlink_is_rejected_like_the_session_launcher(self):
        runtime = self.home / 'runtime'
        runtime.mkdir(mode=0o700)
        alias = self.home / 'runtime-alias'
        alias.symlink_to(runtime, target_is_directory=True)
        with mock.patch.dict(doctor.os.environ, {'XDG_RUNTIME_DIR': str(alias)}, clear=True):
            found = doctor.check_session(self.env)[0]
        self.assertEqual(found['level'], 'FAIL')
        self.assertIn('no symlinks', found['detail'])

    def test_active_socket_and_private_runtime_are_healthy(self):
        runtime = self.home / 'runtime'
        runtime.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(runtime / 'wayland-0'))
            with mock.patch.dict(doctor.os.environ, {'XDG_RUNTIME_DIR': str(runtime),
                                 'XDG_SESSION_TYPE': 'wayland', 'WAYLAND_DISPLAY': 'wayland-0'}, clear=True):
                self.assertEqual([item['level'] for item in doctor.check_session(self.env)], ['ok', 'ok'])

    def test_regular_file_is_not_accepted_as_a_wayland_socket(self):
        runtime = self.home / 'runtime'
        runtime.mkdir(mode=0o700)
        (runtime / 'wayland-0').touch()
        with mock.patch.dict(doctor.os.environ, {'XDG_RUNTIME_DIR': str(runtime),
                             'WAYLAND_DISPLAY': 'wayland-0'}, clear=True):
            self.assertEqual(doctor.check_session(self.env)[-1]['level'], 'WARN')


class RunTests(Fixture):
    def test_one_crashing_check_does_not_hide_the_others(self):
        with mock.patch.object(doctor, "check_firewall", side_effect=RuntimeError("boom")):
            results = doctor.run_checks(self.env)
        names = self.levels(results)
        self.assertEqual(names["doctor"], "WARN")
        self.assertIn("installed copies", names)

    def test_exit_status_and_json(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="DROP"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=yes\n")
        with mock.patch.object(doctor.Path, "home", return_value=self.home), \
                mock.patch.object(doctor, "Env", lambda project: self.env), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = doctor.main(["--json", "--project", str(self.project)])
        self.assertEqual(code, 1)
        self.assertTrue(any(f["level"] == "FAIL" for f in json.loads(out.getvalue())))

    def test_selected_checks_skip_loading_network_code(self):
        with mock.patch.object(doctor, 'load_cube_net', side_effect=AssertionError('must not import')):
            results = doctor.run_checks(self.env, ['drift'])
        self.assertEqual([item['name'] for item in results], ['installed copies'])

    def test_broken_network_module_does_not_hide_other_checks(self):
        with mock.patch.object(doctor, 'load_cube_net', side_effect=SyntaxError('broken module')):
            names = self.levels(doctor.run_checks(self.env))
        self.assertIn('installed copies', names)
        self.assertEqual(names['wifi controller'], 'WARN')

    def test_strict_mode_changes_exit_status_and_preserves_json_list_contract(self):
        rows = [doctor.finding('WARN', 'folders', 'missing default')]
        for flags, expected in (([], 0), (['--strict'], 1)):
            with mock.patch.object(doctor, 'run_checks', return_value=rows), contextlib.redirect_stdout(io.StringIO()) as out:
                code = doctor.main(['--json', '--check', 'folders'] + flags)
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(out.getvalue()), rows)

    def test_list_checks_is_json_without_running_any_probe(self):
        with mock.patch.object(doctor, 'run_checks', side_effect=AssertionError('must not probe')), contextlib.redirect_stdout(io.StringIO()) as out:
            code = doctor.main(['--json', '--list-checks'])
        self.assertEqual(code, 0)
        groups = json.loads(out.getvalue())
        self.assertIn('package', groups)
        self.assertIn('audio', groups)

    def test_audio_check_reports_backend_and_device_permissions(self):
        self.put('usr/bin/cube-audio', '')
        (self.root / 'usr/bin/cube-audio').chmod(0o755)
        self.put('dev/snd/controlC0', '')
        findings = doctor.check_audio(self.env)
        self.assertEqual([item['level'] for item in findings], ['ok', 'ok'])
        (self.root / 'etc/whitecube').mkdir(parents=True, exist_ok=True)
        (self.root / 'etc/whitecube/config').write_text('audio = pipewire\n')
        pw_findings = doctor.check_audio(self.env)
        self.assertEqual(pw_findings[-1]['level'], 'WARN')
        self.assertIn('pipewire', pw_findings[-1]['detail'])
        with mock.patch.object(doctor.os, 'access', side_effect=lambda p, m: False if 'controlC0' in str(p) else True):
            denied = doctor.check_audio(self.env)
        self.assertEqual(denied[0]['level'], 'WARN')
        self.assertIn('usermod -aG audio', denied[0]['fix'])

    def test_dns_lease_and_lockdown_checks(self):
        self.put('run/whitecube-net/status.json', json.dumps({
            'association_state': 'COMPLETED', 'ipv4_address': '10.0.0.2', 'updated_epoch': time.time(),
        }))
        missing_dns = doctor.check_dns_lease(self.env)
        self.assertEqual(missing_dns[0]['level'], 'WARN')
        self.put('run/whitecube-net/resolv.conf', '# wlan0 via dhcpcd (cube-net-dns)\nnameserver 1.1.1.1\n')
        ok_dns = doctor.check_dns_lease(self.env)
        self.assertEqual(ok_dns[0]['level'], 'ok')
        self.assertIn('1.1.1.1', ok_dns[0]['detail'])
        self.assertEqual(doctor.check_lockdown(self.env), [])
        self.put('var/lib/whitecube/net-lockdown.json', json.dumps({'version': 1, 'allow_loopback': False}))
        locked = doctor.check_lockdown(self.env)
        self.assertEqual(locked[0]['level'], 'WARN')
        self.assertIn('loopback blocked', locked[0]['detail'])

    def test_version_flag_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
            doctor.main(['--version'])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn('whitecube-doctor', out.getvalue())

    def test_the_command_is_read_only_and_uses_the_isolated_interpreter(self):
        source = (HERE / "bin" / "whitecube-doctor").read_text()
        self.assertEqual(source.splitlines()[0], "#!/usr/bin/python3 -I")
        for forbidden in ("write_text", "unlink(", "rmtree", "os.remove", ".chmod(", "shutil.copy", "sudo systemctl restart"):
            self.assertNotIn(forbidden, source.split("def main")[0].replace("'sudo systemctl status", ""), forbidden)

    def test_browser_check_reports_staged_dev_and_json_health(self):
        self.assertEqual(doctor.check_browser(self.env)[0]['level'], 'info')
        self.put('usr/bin/whitecube-browser', '')
        self.assertEqual(doctor.check_browser(self.env)[0]['level'], 'FAIL')
        for exe in ('cube-browser', 'cube-renderd', 'cube-netd'):
            p = self.put(f'usr/libexec/whitecube/cube-browser/{exe}', '')
            p.chmod(0o755)
        self.assertEqual(doctor.check_browser(self.env)[0]['level'], 'ok')

        fake_bin = self.home / '.local/bin/cube-browser'
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text('#!/bin/sh\n')
        fake_bin.chmod(0o755)
        live_env = doctor.Env(self.home / 'no-proj', root=Path('/'), home=self.home)
        ok_proc = mock.Mock(returncode=0, stdout=json.dumps({
            'version': '0.12.0', 'workers': {'cube-renderd': True, 'cube-netd': True},
            'ca_bundle': True, 'landlock_abi': 5, 'ok': True,
        }))
        fail_proc = mock.Mock(returncode=1, stdout=json.dumps({
            'version': '0.12.0', 'workers': {'cube-renderd': True, 'cube-netd': False},
            'ca_bundle': False, 'landlock_abi': 5, 'ok': False,
        }))
        if not Path('/usr/bin/whitecube-browser').exists():
            with mock.patch.object(doctor.subprocess, 'run', return_value=ok_proc):
                self.assertEqual(doctor.check_browser(live_env)[0]['level'], 'ok')
            with mock.patch.object(doctor.subprocess, 'run', return_value=fail_proc):
                failed = doctor.check_browser(live_env)[0]
                self.assertEqual(failed['level'], 'FAIL')
                self.assertIn('cube-netd', failed['detail'])
                self.assertIn('ca-certificates', failed['detail'])

    def test_editor_check_reports_staged_and_doctor_health(self):
        self.assertIn('editor', doctor.CHECK_GROUPS)
        self.assertEqual(doctor.check_editor(self.env)[0]['level'], 'info')
        launcher = self.put('usr/bin/cube-editor', '#!/bin/sh\n')
        launcher.chmod(0o755)
        self.assertEqual(doctor.check_editor(self.env)[0]['level'], 'FAIL')
        for mod in ('app.py', 'engine.py', 'worker.py', 'kernel.py', 'diagnostics.py'):
            self.put(f'usr/share/whitecube/cube-editor/{mod}', '')
        self.assertEqual(doctor.check_editor(self.env)[0]['level'], 'ok')

        fake_bin = self.home / '.local/bin/cube-editor'
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text('#!/bin/sh\n')
        fake_bin.chmod(0o755)
        live_env = doctor.Env(self.home / 'no-proj', root=Path('/'), home=self.home)
        ok_proc = mock.Mock(returncode=0, stdout=json.dumps({
            'format': 'cube-editor-diagnostics', 'ready': True,
            'checks': [{'id': 'tool-xorriso', 'status': 'pass'}, {'id': 'uefi', 'status': 'warning'}],
        }))
        fail_proc = mock.Mock(returncode=1, stdout=json.dumps({
            'format': 'cube-editor-diagnostics', 'ready': False,
            'checks': [{'id': 'gtk-runtime', 'status': 'fail'}],
        }))
        if not Path('/usr/bin/cube-editor').exists():
            with mock.patch.object(doctor.subprocess, 'run', return_value=ok_proc):
                res = doctor.check_editor(live_env)
                self.assertEqual(res[0]['level'], 'ok')
                self.assertIn('uefi', res[0]['detail'])
            with mock.patch.object(doctor.subprocess, 'run', return_value=fail_proc):
                failed = doctor.check_editor(live_env)[0]
                self.assertEqual(failed['level'], 'FAIL')
                self.assertIn('gtk-runtime', failed['detail'])

    def test_trainer_check_reports_staged_and_doctor_health(self):
        self.assertIn('trainer', doctor.CHECK_GROUPS)
        self.assertEqual(doctor.check_trainer(self.env)[0]['level'], 'info')
        for bin_name in ('cube-train', 'whitecube-train'):
            p = self.put(f'usr/bin/{bin_name}', '#!/bin/sh\n')
            p.chmod(0o755)
        self.assertEqual(doctor.check_trainer(self.env)[0]['level'], 'FAIL')
        for mod in ('app.py', 'cli.py', 'engine.py', 'diagnostics.py', 'worker.py', 'pipeline/train_supervisor.py'):
            self.put(f'usr/share/whitecube/cube-train/{mod}', '')
        self.assertEqual(doctor.check_trainer(self.env)[0]['level'], 'ok')

        fake_bin = self.home / '.local/bin/whitecube-train'
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text('#!/bin/sh\n')
        fake_bin.chmod(0o755)
        live_env = doctor.Env(self.home / 'no-proj', root=Path('/'), home=self.home)
        ok_proc = mock.Mock(returncode=1, stdout=json.dumps({
            'version': '1.1.1', 'ok': False,
            'checks': [
                {'id': 'desktop', 'status': 'ok'},
                {'id': 'output', 'status': 'ok'},
                {'id': 'corpus', 'status': 'warning'},
            ],
        }))
        fail_proc = mock.Mock(returncode=1, stdout=json.dumps({
            'version': '1.1.1', 'ok': False,
            'checks': [{'id': 'desktop', 'status': 'error'}, {'id': 'output', 'status': 'ok'}],
        }))
        if not Path('/usr/bin/whitecube-train').exists():
            with mock.patch.object(doctor.subprocess, 'run', return_value=ok_proc):
                res = doctor.check_trainer(live_env)
                self.assertEqual(res[0]['level'], 'ok')
                self.assertIn('corpus', res[0]['detail'])
            with mock.patch.object(doctor.subprocess, 'run', return_value=fail_proc):
                failed = doctor.check_trainer(live_env)[0]
                self.assertEqual(failed['level'], 'FAIL')
                self.assertIn('desktop', failed['detail'])

    def test_vm_check_reports_staged_and_qemu_readiness(self):
        self.assertIn('vm', doctor.CHECK_GROUPS)
        self.assertEqual(doctor.check_vm(self.env)[0]['level'], 'info')
        for bin_path in (
            'usr/bin/cube-strap',
            'usr/bin/whitecube-vm',
            'usr/libexec/whitecube/cube-strap-build',
        ):
            p = self.put(bin_path, '#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        self.assertEqual(doctor.check_vm(self.env)[0]['level'], 'FAIL')
        for mod in ('app.py', 'strap.py', 'deb-strap.sh'):
            self.put(f'usr/share/whitecube/cube-strap/{mod}', '')
        ok = doctor.check_vm(self.env)[0]
        self.assertEqual(ok['level'], 'ok')
        self.assertIn('staged', ok['detail'])

        (self.root / 'usr/share/whitecube/cube-strap/deb-strap.sh').unlink()
        bad = doctor.check_vm(self.env)[0]
        self.assertEqual(bad['level'], 'FAIL')
        self.assertIn('deb-strap.sh', bad['detail'])

        fake_bin = self.home / '.local/bin/whitecube-vm'
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text('#!/bin/sh\nexit 0\n')
        fake_bin.chmod(0o755)
        live_env = doctor.Env(self.home / 'no-proj', root=Path('/'), home=self.home)
        ready_proc = mock.Mock(
            returncode=0,
            stdout='{"ready": true, "kvm": false, "missing": []}',
            stderr='',
        )
        warn_proc = mock.Mock(
            returncode=1,
            stdout='{"ready": false, "kvm": false, "missing": [{"component": "OVMF", "package": "ovmf"}]}',
            stderr='',
        )
        if not Path('/usr/bin/whitecube-vm').exists():
            with mock.patch.object(doctor.subprocess, 'run', return_value=ready_proc):
                live_ok = doctor.check_vm(live_env)[0]
                self.assertEqual(live_ok['level'], 'ok')
                self.assertIn('software emulation', live_ok['detail'])
            with mock.patch.object(doctor.subprocess, 'run', return_value=warn_proc):
                live_warn = doctor.check_vm(live_env)[0]
                self.assertEqual(live_warn['level'], 'WARN')
                self.assertIn('ovmf', live_warn['detail'])

    def test_chrootables_check_reports_staged_and_manager_readiness(self):
        self.assertIn('chrootables', doctor.CHECK_GROUPS)
        self.assertEqual(doctor.check_chrootables(self.env)[0]['level'], 'info')
        for bin_path in (
            'usr/bin/cube-chrootables',
            'usr/bin/whitecube-chrootable-manager',
            'usr/libexec/whitecube/chrootable-manager',
        ):
            p = self.put(bin_path, '#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        self.assertEqual(doctor.check_chrootables(self.env)[0]['level'], 'FAIL')
        for mod in (
            'app.py', 'backend.py', 'broker.py', 'assets/demo.json',
            'chrootables/__init__.py', 'chrootables/app.py', 'chrootables/authenticate.py',
            'chrootables/backend.py', 'chrootables/broker.py', 'chrootables/capture.py',
            'chrootables/client.py', 'chrootables/paths.py', 'chrootables/style.py',
            'chrootables/terminal.py',
        ):
            self.put(f'usr/share/whitecube/chrootables/{mod}', '')
        ok = doctor.check_chrootables(self.env)[0]
        self.assertEqual(ok['level'], 'ok')
        self.assertIn('staged', ok['detail'])

        (self.root / 'usr/share/whitecube/chrootables/chrootables/terminal.py').unlink()
        bad = doctor.check_chrootables(self.env)[0]
        self.assertEqual(bad['level'], 'FAIL')
        self.assertIn('terminal.py', bad['detail'])

        fake_bin = self.home / '.local/bin/whitecube-chrootable-manager'
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text('#!/bin/sh\nexit 0\n')
        fake_bin.chmod(0o755)
        live_env = doctor.Env(self.home / 'no-proj', root=Path('/'), home=self.home)
        ok_proc = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                'schema_version': 1, 'version': '1.2.1', 'ready': True,
                'checks': [{'id': 'filesystem:overlay', 'status': 'warning'}],
            }),
            stderr='',
        )
        warn_proc = mock.Mock(
            returncode=1,
            stdout=json.dumps({
                'schema_version': 1, 'version': '1.2.1', 'ready': False,
                'checks': [{'id': 'command:debootstrap', 'status': 'error'}],
            }),
            stderr='',
        )
        if not Path('/usr/bin/whitecube-chrootable-manager').exists():
            with mock.patch.object(doctor.subprocess, 'run', return_value=ok_proc):
                live_ok = doctor.check_chrootables(live_env)[0]
                self.assertEqual(live_ok['level'], 'ok')
                self.assertIn('overlay', live_ok['detail'])
            with mock.patch.object(doctor.subprocess, 'run', return_value=warn_proc):
                live_warn = doctor.check_chrootables(live_env)[0]
                self.assertEqual(live_warn['level'], 'WARN')
                self.assertIn('command:debootstrap', live_warn['detail'])

    def test_prompts_check_reports_staged_and_cli_readiness(self):
        self.assertIn('prompts', doctor.CHECK_GROUPS)
        self.assertEqual(doctor.check_prompts(self.env)[0]['level'], 'info')
        p = self.put('usr/bin/cube-prompts', '#!/bin/sh\nexit 0\n')
        p.chmod(0o755)
        self.assertEqual(doctor.check_prompts(self.env)[0]['level'], 'FAIL')
        for mod in (
            'app.py',
            'style.css',
            'prompt_manager/__init__.py',
            'prompt_manager/cli.py',
            'prompt_manager/config.py',
            'prompt_manager/storage/database.py',
        ):
            self.put(f'usr/share/whitecube/cube-prompts/{mod}', '')
        ok = doctor.check_prompts(self.env)[0]
        self.assertEqual(ok['level'], 'ok')
        self.assertIn('staged', ok['detail'])

        (self.root / 'usr/share/whitecube/cube-prompts/prompt_manager/cli.py').unlink()
        bad = doctor.check_prompts(self.env)[0]
        self.assertEqual(bad['level'], 'FAIL')
        self.assertIn('cli.py', bad['detail'])

        fake_bin = self.home / '.local/bin/cube-prompts'
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text('#!/bin/sh\nexit 0\n')
        fake_bin.chmod(0o755)
        live_env = doctor.Env(self.home / 'no-proj', root=Path('/'), home=self.home)
        ok_proc = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                'format': 'cube-prompts-diagnostics', 'version': '2.0.0',
                'sqlite_version': '3.46.1', 'ready': True, 'checks': [],
            }),
            stderr='',
        )
        fail_proc = mock.Mock(
            returncode=1,
            stdout=json.dumps({
                'format': 'cube-prompts-diagnostics', 'version': '2.0.0',
                'sqlite_version': '3.46.1', 'ready': False,
                'checks': [{'id': 'database', 'status': 'error'}],
            }),
            stderr='',
        )
        if not Path('/usr/bin/cube-prompts').exists():
            with mock.patch.object(doctor.subprocess, 'run', return_value=ok_proc):
                live_ok = doctor.check_prompts(live_env)[0]
                self.assertEqual(live_ok['level'], 'ok')
                self.assertIn('SQLite', live_ok['detail'])
            with mock.patch.object(doctor.subprocess, 'run', return_value=fail_proc):
                live_fail = doctor.check_prompts(live_env)[0]
                self.assertEqual(live_fail['level'], 'FAIL')
                self.assertIn('database', live_fail['detail'])

    def test_chat_check_reports_staged_and_cli_readiness(self):
        info = doctor.check_chat(self.env)[0]
        self.assertEqual(info['level'], 'info')

        self.put('usr/bin/desktop-chat', '#!/bin/sh\n').chmod(0o755)
        self.put('usr/lib/whitecube/desktop-chat-runtime/electron', '#!/bin/sh\n').chmod(0o755)
        for mod in (
            'desktop-chat.sh',
            'main.js',
            'src/cli.js',
            'src/config.js',
            'src/history.js',
            'src/tools.js',
        ):
            self.put(f'usr/share/whitecube/desktop-chat/{mod}', '')
        ok = doctor.check_chat(self.env)[0]
        self.assertEqual(ok['level'], 'ok')
        self.assertIn('staged', ok['detail'])

        (self.root / 'usr/share/whitecube/desktop-chat/src/cli.js').unlink()
        bad = doctor.check_chat(self.env)[0]
        self.assertEqual(bad['level'], 'FAIL')
        self.assertIn('cli.js', bad['detail'])

        fake_bin = self.home / '.local/bin/desktop-chat'
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text('#!/bin/sh\nexit 0\n')
        fake_bin.chmod(0o755)
        live_env = doctor.Env(self.home / 'no-proj', root=Path('/'), home=self.home)
        ok_proc = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                'ok': True, 'version': '1.0.0',
                'config': {'present': True, 'error': None},
                'conversations': {'count': 2, 'error': None},
                'checkpoints': {'count': 1},
            }),
            stderr='',
        )
        fail_proc = mock.Mock(
            returncode=1,
            stdout=json.dumps({
                'ok': False, 'version': '1.0.0',
                'config': {'present': True, 'error': 'Config root must be a JSON object.'},
                'conversations': {'count': 0, 'error': None},
                'checkpoints': {'count': 0},
            }),
            stderr='',
        )
        if not Path('/usr/bin/desktop-chat').exists():
            with mock.patch.object(doctor.subprocess, 'run', return_value=ok_proc):
                live_ok = doctor.check_chat(live_env)[0]
                self.assertEqual(live_ok['level'], 'ok')
                self.assertIn('2 conversations', live_ok['detail'])
            with mock.patch.object(doctor.subprocess, 'run', return_value=fail_proc):
                live_fail = doctor.check_chat(live_env)[0]
                self.assertEqual(live_fail['level'], 'FAIL')
                self.assertIn('JSON object', live_fail['detail'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
