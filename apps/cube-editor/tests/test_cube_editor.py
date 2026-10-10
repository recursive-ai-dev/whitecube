"""Unprivileged tests: real synthetic squashfs/ISO exports, no host mounts."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Cancelled, CubeError, Engine, Project, atomic_text, guest_destination, inspect_iso, package_list
from worker import Worker, checksum_tree, copy_payload, rebuild_iso


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cube-editor-test-")
        self.path = Path(self.temp.name)
        self.home = mock.patch("engine.Path.home", return_value=self.path / "home")
        self.home.start()
        self.source = self.path / "source.iso"
        self.source.write_bytes(b"test source")
        self.project = Project.create(self.source, "Review Cube", self.path / "projects", self.path / "output")

    def tearDown(self):
        self.home.stop()
        self.temp.cleanup()


class ProjectTests(ProjectFixture):

    def test_atomic_project_roundtrip_and_lock(self):
        self.project.save(compression="zstd")
        self.assertEqual(Project.load(self.project.directory).data["compression"], "zstd")
        with self.project.lock():
            with self.assertRaises(CubeError):
                with Project.load(self.project.directory).lock():
                    pass
            with self.assertRaises(CubeError):
                Project.load(self.project.directory).save(name="conflicting window")
            self.project.save(name="owning worker")
        with self.project.lock():
            pass

    def test_reject_package_options_and_payload_symlink_escape(self):
        self.assertEqual(package_list("vim curl libgtk-4-1:amd64"), ["vim", "curl", "libgtk-4-1:amd64"])
        for package in ("--allow-unauthenticated", "$(touch pwn)", "vim;reboot"):
            with self.assertRaises(CubeError):
                package_list([package])
        root = self.project.rootfs
        root.mkdir()
        (root / "etc").symlink_to(self.path)
        with self.assertRaises(CubeError):
            guest_destination(root, "/etc/unsafe")
        with self.assertRaises(CubeError):
            guest_destination(root, "/../unsafe")

    def test_nested_payload_destination_symlink_is_rejected(self):
        source = self.path / "payload"
        source.mkdir()
        (source / "sub").mkdir()
        (source / "sub/config").write_text("new configuration")
        root = self.project.rootfs
        (root / "etc").mkdir(parents=True)
        (root / "etc/sub").symlink_to(self.path)
        with self.assertRaises(CubeError):
            copy_payload(source, root / "etc", root)
        self.assertFalse((self.path / "config").exists())

    def test_never_overwrite_source_or_existing_sidecar(self):
        self.project.save(output_dir=str(self.source.parent), output_name=self.source.name)
        with self.assertRaises(CubeError):
            self.project.output_path
        self.project.save(output_dir=str(self.path / "output"), output_name="release.iso")
        self.project.output_path.parent.mkdir()
        Path(str(self.project.output_path) + ".sha256").symlink_to(self.source)
        with self.assertRaises(CubeError):
            rebuild_iso(self.project, lambda command: self.fail("Must reject before executing xorriso"))
        self.assertEqual(self.source.read_bytes(), b"test source")

    def test_integrity_lists_renew_and_boot_offsets_are_excluded(self):
        tree = self.project.iso_tree
        (tree / "boot/grub/i386-pc").mkdir(parents=True)
        (tree / "boot/grub/i386-pc/eltorito.img").write_bytes(b"boot changes during export")
        (tree / "data").write_bytes(b"payload")
        (tree / "SHA256SUMS").write_text("stale")
        checksum_tree(tree)
        self.assertIn(hashlib.sha256(b"payload").hexdigest(), (tree / "SHA256SUMS").read_text())
        self.assertNotIn("eltorito", (tree / "md5sum.txt").read_text())
        self.assertNotIn("SHA256SUMS", (tree / "md5sum.txt").read_text())

    def test_dotted_layer_manifest_retains_lower_layer_metadata(self):
        p = self.project
        (p.iso_tree / "casper").mkdir(parents=True)
        (p.rootfs / "usr/bin").mkdir(parents=True)
        (p.rootfs / "usr/bin/dpkg-query").touch()
        (p.iso_tree / "casper/minimal.standard.manifest").write_text("original lower layer")
        p.save(squashfs_path="/casper/minimal.standard.live.squashfs")
        worker = Worker(p, os.getuid(), os.getgid(), "build")
        worker.guest = lambda *args, **kwargs: "cube-editor 1.0\n"
        worker.refresh_metadata()
        self.assertEqual((p.iso_tree / "casper/minimal.standard.manifest").read_text(), "original lower layer")
        self.assertEqual((p.iso_tree / "casper/minimal.standard.live.manifest").read_text(), "cube-editor 1.0\n")

    def test_original_hidden_paths_and_new_deletions_get_whiteouts(self):
        p = self.project
        (p.rootfs / "etc").mkdir(parents=True)
        (p.rootfs / "symlink").symlink_to(self.path)
        p.save(inspection={"layer_chain": ["base.squashfs", "base.live.squashfs"]})
        (p.directory / "baseline-paths.json").write_text(json.dumps(["etc/hidden-original", "etc/removed", "symlink/host-path"]))
        worker = Worker(p, os.getuid(), os.getgid(), "build")
        created = []
        with mock.patch("worker.os.mknod", side_effect=lambda path, *args: created.append(str(path.relative_to(p.rootfs)))):
            worker.deletion_whiteouts()
        self.assertEqual(set(created), {"etc/hidden-original", "etc/removed"})
        (p.directory / "baseline-paths.json").write_text(json.dumps(["../../unsafe-host-device"]))
        with mock.patch("worker.os.mknod") as mknod:
            with self.assertRaises(CubeError):
                worker.deletion_whiteouts()
            mknod.assert_not_called()

    def test_metadata_rejects_nested_iso_symlink_before_writing(self):
        p = self.project
        p.iso_tree.mkdir()
        (p.iso_tree / "casper").symlink_to(self.path)
        p.rootfs.mkdir()
        p.save(squashfs_path="/casper/minimal.standard.live.squashfs")
        worker = Worker(p, os.getuid(), os.getgid(), "build")
        with self.assertRaises(CubeError):
            worker.refresh_metadata()
        self.assertFalse((self.path / "minimal.standard.live.manifest").exists())

    def test_atomic_metadata_does_not_follow_leaf_symlink(self):
        destination = self.path / "metadata"
        destination.symlink_to(self.source)
        atomic_text(destination, "new metadata")
        self.assertEqual(self.source.read_bytes(), b"test source")
        self.assertFalse(destination.is_symlink())

    def test_multilayer_plan_shows_real_overlay_steps(self):
        self.project.save(inspection={"layer_chain": ["/casper/minimal.squashfs", "/casper/minimal.live.squashfs"]})
        plan = Engine(self.project).plan("extract")
        self.assertEqual(sum(command[0] == "unsquashfs" for command in plan), 2)
        self.assertTrue(any(command[0] == "mount" and "overlay" in command for command in plan))

    def test_sha_verification_and_cli_failure(self):
        output = self.project.output_path
        output.parent.mkdir()
        output.write_bytes(b"release")
        Path(str(output) + ".sha256").write_text(hashlib.sha256(b"release").hexdigest() + "  " + output.name)
        self.assertTrue(Engine(self.project).verify_release()["success"])
        output.write_bytes(b"corrupt release")
        self.assertFalse(Engine(self.project).verify_release()["success"])
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "engine.py"), "verify", str(self.project.directory)], capture_output=True)
        self.assertEqual(result.returncode, 1)
        Path(str(output) + ".sha256").write_text("")
        with self.assertRaises(CubeError):
            Engine(self.project).verify_release()

    def test_stale_project_save_preserves_latest_worker_state(self):
        stale = Project.load(self.project.directory)
        self.project.save(state="built", last_build={"sha256": "release"})
        stale.save(name="renamed cube")
        updated = Project.load(self.project.directory)
        self.assertEqual(updated.data["state"], "built")
        self.assertEqual(updated.data["last_build"]["sha256"], "release")

    def test_terminal_exit_without_worker_success_is_failure(self):
        foot = self.path / "foot"
        foot.symlink_to(sys.executable)
        with self.assertRaises(CubeError):
            Engine(self.project).run([str(foot), "-c", "pass"])

    def test_cancel_before_authentication_closes_terminal_prompt(self):
        foot = self.path / "foot"
        foot.symlink_to(sys.executable)
        engine = Engine(self.project)
        timer = threading.Timer(0.2, engine.cancel)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                engine.run([str(foot), "-c", "import time; time.sleep(60)"])
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_calamares_layered_installer_uses_clean_custom_root_and_matching_initramfs(self):
        import yaml
        p = self.project
        configs = p.rootfs / "etc/calamares/modules"
        configs.mkdir(parents=True)
        (configs / "unpackfs.conf").write_text('''unpack:
    -   source: "/cdrom/casper/minimal.squashfs"
        sourcefs: "squashfs"
        destination: ""
        condition: true
    -   source: "/cdrom/casper/minimal.standard.squashfs"
        sourcefs: "squashfs"
        destination: ""
        condition: "installation_data.unpack_regular"
    -   source: "/cdrom/extra.txt"
        sourcefs: "file"
        destination: "/tmp/extra.txt"
''')
        settings = {"branding": "lubuntu", "sequence": [{"show": ["welcome", "partition"]},
                    {"exec": ["partition", "mount", "unpackfs", "fstab", "dracutlukscfg", "dracut", "bootloader", "packages", "umount"]},
                    {"show": ["finished"]}], "instances": []}
        (configs.parent / "settings.conf").write_text(yaml.safe_dump(settings))
        (configs / "packages.conf").write_text("backend: apt\noperations:\n  - try_install: [language-pack-$LOCALE]\n")
        tools = p.rootfs / "usr/sbin"
        tools.mkdir(parents=True)
        (tools / "update-initramfs").touch()
        for module in ("initramfs", "initramfscfg", "shellprocess"):
            path = p.rootfs / "usr/lib/x86_64-linux-gnu/calamares/modules" / module
            path.mkdir(parents=True)
            (path / "module.desc").touch()
        dpkg = p.rootfs / "var/lib/dpkg"
        dpkg.mkdir(parents=True)
        (dpkg / "status").write_text("\n\n".join(f"Package: {package}\nStatus: install ok installed\n" for package in ("casper", "calamares", "cryptsetup-initramfs")))
        p.iso_tree.mkdir()
        chain = ["/casper/minimal.squashfs", "/casper/minimal.standard.squashfs", "/casper/minimal.standard.live.squashfs"]
        p.save(inspection={"layer_chain": chain})
        worker = Worker(p, os.getuid(), os.getgid(), "build")
        worker.emit = lambda *args, **kwargs: None
        installer = worker.prepare_installer()
        self.assertEqual(installer.relative_to(p.iso_tree), Path("cube-editor/install.squashfs"))
        rewritten = yaml.safe_load((configs / "unpackfs.conf").read_text())
        self.assertEqual(rewritten["unpack"][0]["source"], "/cdrom/cube-editor/install.squashfs")
        self.assertTrue(rewritten["unpack"][0]["condition"])
        self.assertEqual(rewritten["unpack"][1]["source"], "/cdrom/extra.txt")
        migrated = yaml.safe_load((configs.parent / "settings.conf").read_text())
        jobs = migrated["sequence"][1]["exec"]
        self.assertIn("initramfscfg", jobs)
        self.assertIn("initramfs", jobs)
        self.assertNotIn("dracut", jobs)
        self.assertEqual(migrated["branding"], "lubuntu")
        self.assertLess(jobs.index("shellprocess@cube-editor-install-prepare"), jobs.index("packages"))
        self.assertLess(jobs.index("packages"), jobs.index("shellprocess@cube-editor-install-finalize"))
        self.assertLess(jobs.index("shellprocess@cube-editor-install-finalize"), jobs.index("umount"))
        preparation = yaml.safe_load((configs / "cube-editor-install-prepare.conf").read_text())
        self.assertIn("cryptsetup-initramfs", preparation["script"][0])
        cleanup = yaml.safe_load((configs / "packages.conf").read_text())["operations"]
        self.assertIn({"remove": ["calamares", "casper"]}, cleanup)
        self.assertTrue((p.directory / "calamares-original-unpackfs.conf").is_file())
        # Rebuilding a later release is idempotent and preserves originals.
        worker.prepare_installer()
        rerun = yaml.safe_load((configs.parent / "settings.conf").read_text())
        self.assertEqual(rerun["sequence"][1]["exec"].count("shellprocess@cube-editor-install-finalize"), 1)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("xorriso", "mksquashfs", "unsquashfs")), "ISO tools unavailable")
class ISOIntegrationTests(ProjectFixture):
    def make_iso(self, chained=False, bootable=False):
        tree = self.path / "original-tree"
        (tree / "casper").mkdir(parents=True)
        filesystem = self.path / "guest"
        (filesystem / "etc").mkdir(parents=True)
        (filesystem / "etc/os-release").write_text("PRETTY_NAME=\"Cube Editor Test\"\n")
        names = ["minimal.squashfs", "minimal.standard.squashfs", "minimal.standard.live.squashfs"] if chained else ["filesystem.squashfs"]
        for name in names:
            run(["mksquashfs", str(filesystem), str(tree / "casper" / name), "-noappend", "-processors", "1", "-quiet"])
        command = ["xorriso", "-as", "mkisofs", "-o", str(self.source), "-V", "CUBE_SYNTHETIC"]
        if bootable:
            (tree / "boot").mkdir()
            (tree / "boot/eltorito.img").write_bytes(b"\0" * 4096)
            efi = self.path / "efi-partition.img"
            efi.write_bytes(b"\0" * (512 * 1024))
            command += ["-b", "boot/eltorito.img", "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
                        "-eltorito-alt-boot", "-e", "--interval:appended_partition_2:all::", "-no-emul-boot",
                        "-append_partition", "2", "0xef", str(efi), "-appended_part_as_gpt"]
        command.append(str(tree))
        run(command)
        self.project.save(source_size=self.source.stat().st_size, source_mtime_ns=self.source.stat().st_mtime_ns)
        return tree

    def test_real_layer_chain_inspection_and_selected_root(self):
        self.make_iso(chained=True)
        info = inspect_iso(self.source)
        self.assertEqual(info["squashfs_path"], "/casper/minimal.standard.live.squashfs")
        self.assertEqual(len(info["layer_chain"]), 3)
        self.assertTrue(info["can_rebuild"])
        selected = inspect_iso(self.source, "/casper/minimal.standard.squashfs")
        self.assertEqual(len(selected["layer_chain"]), 2)

    def test_real_iso_roundtrip_retains_hidden_efi_and_bios(self):
        self.make_iso(bootable=True)
        original = inspect_iso(self.source)
        self.assertTrue(original["bios"] and original["uefi"])
        run(["xorriso", "-osirrox", "on", "-indev", str(self.source), "-extract", "/", str(self.project.iso_tree)])
        # Test actual content changes and root filesystem recompression entirely
        # as the desktop user; no mount, sudo, chroot or real source is touched.
        extracted = self.path / "extracted-root"
        squash = self.project.iso_tree / "casper/filesystem.squashfs"
        run(["unsquashfs", "-d", str(extracted), str(squash)])
        (extracted / "etc/cube-editor").write_text("customized\n")
        squash.unlink()
        run(["mksquashfs", str(extracted), str(squash), "-noappend", "-processors", "1", "-quiet"])
        self.project.save(inspection=original, squashfs_path=original["squashfs_path"])
        checksum_tree(self.project.iso_tree, original["boot_image_paths"] + [original["boot_catalog"]])
        release = rebuild_iso(self.project, run)
        exported = inspect_iso(self.project.output_path)
        self.assertTrue(exported["bios"] and exported["uefi"])
        self.assertEqual(exported["volume_id"], "CUBE_EDITOR")
        self.assertTrue(Engine(self.project).verify_release()["success"])
        self.assertEqual(release["sha256"], hashlib.sha256(self.project.output_path.read_bytes()).hexdigest())
        export_tree = self.path / "exported-tree"
        run(["xorriso", "-osirrox", "on", "-indev", str(self.project.output_path), "-extract", "/casper", str(export_tree)])
        contents = run(["unsquashfs", "-cat", str(export_tree / "filesystem.squashfs"), "etc/cube-editor"])
        self.assertEqual(contents, "customized\n")


if __name__ == "__main__":
    unittest.main()
