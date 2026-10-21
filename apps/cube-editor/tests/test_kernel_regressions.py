"""Kernel propagation against tiny guest and ISO boot trees."""
import os
from unittest import mock
import unittest

from test_cube_editor import ProjectFixture
from engine import CubeError
from kernel import guest_path, kernel_inventory, refresh_kernel


class KernelRegressions(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.project.save(squashfs_path='/live/filesystem.squashfs')
        (self.project.rootfs / 'boot').mkdir(parents=True)
        (self.project.iso_tree / 'live').mkdir(parents=True)
        self.emit = mock.Mock()

    def guest_kernel(self, version):
        boot = self.project.rootfs / 'boot'
        (boot / ('vmlinuz-' + version)).write_text('kernel ' + version)
        (boot / ('initrd.img-' + version)).write_text('initrd ' + version)
        (self.project.rootfs / 'lib/modules' / version).mkdir(parents=True)

    def iso_pair(self, kernel='vmlinuz', initrd='initrd.img', directory='live'):
        parent = self.project.iso_tree / directory
        parent.mkdir(parents=True, exist_ok=True)
        paths = (parent / kernel, parent / initrd)
        paths[0].write_text('original kernel')
        paths[1].write_text('original initrd')
        return paths

    def refresh(self):
        refresh_kernel(self.project, self.emit)

    def test_live_initrd_img_and_guest_absolute_default_link(self):
        targets = self.iso_pair()
        self.guest_kernel('6.1.0-10-amd64')
        self.guest_kernel('6.0.0-30-amd64')
        (self.project.rootfs / 'vmlinuz').symlink_to('/boot/vmlinuz-6.1.0-10-amd64')
        os.utime(self.project.rootfs / 'boot/vmlinuz-6.0.0-30-amd64', (2000000000, 2000000000))
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1.0-10-amd64')
        self.assertEqual(targets[1].read_text(), 'initrd 6.1.0-10-amd64')

    def test_guest_default_wins_over_newest_installed_version(self):
        targets = self.iso_pair()
        self.guest_kernel('6.1')
        self.guest_kernel('6.2')
        (self.project.rootfs / 'boot/vmlinuz').symlink_to('vmlinuz-6.1')
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1')

    def test_default_under_guest_absolute_boot_symlink_is_honored(self):
        targets = self.iso_pair()
        self.guest_kernel('6.1')
        self.guest_kernel('6.2')
        (self.project.rootfs / 'usr').mkdir()
        (self.project.rootfs / 'boot').rename(self.project.rootfs / 'usr/boot')
        (self.project.rootfs / 'boot').symlink_to('/usr/boot')
        (self.project.rootfs / 'usr/boot/vmlinuz').symlink_to('vmlinuz-6.1')
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1')

    def test_initrd_alias_cannot_select_a_different_version(self):
        targets = self.iso_pair()
        self.guest_kernel('6.1')
        self.guest_kernel('6.0')
        (self.project.rootfs / 'boot/initrd.img-6.1').unlink()
        (self.project.rootfs / 'boot/initrd.img-6.1').symlink_to('initrd.img-6.0')
        with self.assertRaisesRegex(CubeError, 'initramfs symlink'):
            self.refresh()
        self.assertEqual(targets[0].read_text(), 'original kernel')

    def test_version_ordering_ignores_mtime_and_compares_numeric_abi(self):
        targets = self.iso_pair()
        for version in ('6.1.0-9-amd64', '6.1.0-10-amd64', '6.0.0-99-amd64'):
            self.guest_kernel(version)
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1.0-10-amd64')

    def test_release_candidate_sorts_before_final_kernel(self):
        targets = self.iso_pair()
        self.guest_kernel('6.1-rc9')
        self.guest_kernel('6.1')
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1')

    def test_grub_mapping_refreshes_actual_paths(self):
        targets = self.iso_pair('linux-image', 'ramdisk', 'boot')
        self.guest_kernel('6.1')
        (self.project.iso_tree / 'boot/grub.cfg').write_text('linux /boot/linux-image quiet\ninitrd /boot/ramdisk\n')
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1')
        self.assertEqual(targets[1].read_text(), 'initrd 6.1')

    def test_syslinux_append_mapping_and_unrelated_boot_entry(self):
        targets = self.iso_pair('linux-image', 'ramdisk')
        self.guest_kernel('6.1')
        (self.project.iso_tree / 'memtest').write_text('original memory test')
        (self.project.iso_tree / 'live/menu.cfg').write_text(
            'KERNEL linux-image\nAPPEND boot=live initrd=/live/ramdisk\nKERNEL /memtest\n')
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1')
        self.assertEqual((self.project.iso_tree / 'memtest').read_text(), 'original memory test')

    def test_missing_matching_initramfs_or_modules_preserves_iso(self):
        targets = self.iso_pair()
        self.guest_kernel('6.1')
        initrd = self.project.rootfs / 'boot/initrd.img-6.1'
        initrd.unlink()
        with self.assertRaisesRegex(CubeError, 'matching initramfs'):
            self.refresh()
        initrd.write_text('initrd 6.1')
        (self.project.rootfs / 'lib/modules/6.1').rmdir()
        with self.assertRaisesRegex(CubeError, 'module directory'):
            self.refresh()
        self.assertEqual(targets[0].read_text(), 'original kernel')
        self.assertEqual(targets[1].read_text(), 'original initrd')

    def test_ambiguous_common_initrd_names_fail_before_copy(self):
        targets = self.iso_pair()
        (self.project.iso_tree / 'live/initrd').write_text('other initrd')
        self.guest_kernel('6.1')
        with self.assertRaisesRegex(CubeError, 'ambiguous'):
            self.refresh()
        self.assertEqual(targets[0].read_text(), 'original kernel')

    def test_unsupported_mapping_allowed_only_for_unchanged_baseline(self):
        self.iso_pair('unrecognized-kernel', 'unrecognized-initrd')
        self.guest_kernel('6.1')
        self.project.save(kernel_baseline=kernel_inventory(self.project.rootfs))
        self.refresh()
        self.assertIn('unchanged', self.emit.call_args.args[1])
        (self.project.rootfs / 'boot/vmlinuz-6.1').write_text('changed in place')
        with self.assertRaisesRegex(CubeError, 'Unsupported'):
            self.refresh()

    def test_removed_kernel_cannot_retain_old_iso_kernel(self):
        self.iso_pair()
        self.guest_kernel('6.1')
        self.project.save(kernel_baseline=kernel_inventory(self.project.rootfs))
        (self.project.rootfs / 'boot/vmlinuz-6.1').unlink()
        with self.assertRaisesRegex(CubeError, 'No guest kernel'):
            self.refresh()

    def test_broken_default_link_is_a_kernel_change(self):
        self.iso_pair()
        self.guest_kernel('6.1')
        self.project.save(kernel_baseline=kernel_inventory(self.project.rootfs))
        (self.project.rootfs / 'vmlinuz').symlink_to('/boot/vmlinuz-missing')
        with self.assertRaisesRegex(CubeError, 'selected guest kernel is missing'):
            self.refresh()

    def test_unknown_boot_variables_fail_for_changed_or_legacy_guest(self):
        targets = self.iso_pair()
        self.guest_kernel('6.1')
        (self.project.iso_tree / 'live/grub.cfg').write_text(
            'linux /live/vmlinuz\ninitrd /live/initrd.img\nlinux $kernel\n')
        with self.assertRaisesRegex(CubeError, 'Unsupported ISO boot configuration'):
            self.refresh()
        self.assertEqual(targets[0].read_text(), 'original kernel')

    def test_guest_symlink_escape_and_loop_are_rejected(self):
        (self.project.rootfs / 'vmlinuz').symlink_to('../../outside')
        with self.assertRaises(CubeError):
            guest_path(self.project.rootfs, '/vmlinuz')
        (self.project.rootfs / 'vmlinuz').unlink()
        (self.project.rootfs / 'vmlinuz').symlink_to('/vmlinuz')
        with self.assertRaisesRegex(CubeError, 'symlink loop'):
            guest_path(self.project.rootfs, '/vmlinuz')

    def test_double_slash_guest_paths_cannot_resolve_against_host_root(self):
        host = self.path / 'host-sentinel'
        host.write_text('host data')
        relative = '/' + str(host)
        expected = self.project.rootfs / str(host).lstrip('/')
        self.assertEqual(guest_path(self.project.rootfs, relative), expected)
        (self.project.rootfs / 'vmlinuz').symlink_to(relative)
        self.assertEqual(guest_path(self.project.rootfs, '/vmlinuz'), expected)
        self.assertIsNone(kernel_inventory(self.project.rootfs)['files']['/vmlinuz']['sha256'])

    def test_symlinked_iso_target_is_rejected_without_host_write(self):
        self.guest_kernel('6.1')
        targets = self.iso_pair()
        host = self.path / 'host-kernel'
        host.write_text('host sentinel')
        targets[0].unlink()
        targets[0].symlink_to(host)
        with self.assertRaisesRegex(CubeError, 'symlink'):
            self.refresh()
        self.assertEqual(host.read_text(), 'host sentinel')

    def test_merged_usr_modules_and_boot_symlinks_use_guest_root(self):
        (self.project.rootfs / 'usr/lib/modules/6.1').mkdir(parents=True)
        (self.project.rootfs / 'lib').symlink_to('/usr/lib')
        (self.project.rootfs / 'boot/vmlinuz-6.1').write_text('kernel 6.1')
        (self.project.rootfs / 'boot/initrd.img-6.1').write_text('initrd 6.1')
        targets = self.iso_pair()
        self.refresh()
        self.assertEqual(targets[0].read_text(), 'kernel 6.1')
        self.assertEqual(kernel_inventory(self.project.rootfs)['module_versions'], ['6.1'])


if __name__ == '__main__':
    unittest.main()
