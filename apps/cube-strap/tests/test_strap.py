"""Cube Strap engine: plan validation, deb-strap configuration, commands, history."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import strap  # noqa: E402

DEBSTRAP = HERE / 'deb-strap.sh'


def plan(**values):
    defaults = dict(name='devbox', password='secret')
    defaults.update(values)
    return strap.Plan(**defaults)


def parse(text):
    return dict(re.match(r'(\w+) = "(.*)"\Z', line).groups() for line in text.splitlines() if not line.startswith('#'))


class Problems(unittest.TestCase):
    def test_default_plan_is_ready(self):
        self.assertEqual(strap.problems(plan()), [])

    def test_each_rule(self):
        cases = {
            'name': '-bad', 'kind': 'lxc', 'suite': 'potato', 'size_gib': 0, 'username': 'Root',
            'hostname': 'Bad_Host', 'password': 'ab', 'lifecycle': 'forever', 'mirror': 'ftp://x',
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self.assertTrue(strap.problems(plan(**{key: value})), key)

    def test_taken_name(self):
        self.assertIn('already exists', strap.problems(plan(), taken={'devbox'})[0])

    def test_password_characters_that_deb_strap_or_chpasswd_would_mangle(self):
        for secret in ('pa#ss', 'pa"ss', 'pa:ss', 'pa\\ss', ' pass', 'pass ', 'pa\nss'):
            with self.subTest(secret=secret):
                self.assertTrue(strap.problems(plan(password=secret)))
        self.assertEqual(strap.problems(plan(password="it's fine! $(no) `x`")), [])

    def test_vm_only_recipes_and_minimum_size(self):
        self.assertTrue(strap.problems(plan(kind='chroot', recipe='desktop')))
        self.assertEqual(strap.problems(plan(kind='vm', recipe='desktop', size_gib=12)), [])
        self.assertTrue(strap.problems(plan(kind='vm', size_gib=2)))
        self.assertEqual(strap.recipes_for('chroot'), ['minimal', 'tools', 'developer'])


class Config(unittest.TestCase):
    workdir = Path('/home/u/.cache/cube-strap/build-abc_123')

    def test_chroot_is_a_kernel_free_tarball_bootstrapped_outside_home(self):
        values = parse(strap.config_text(plan(recipe='developer'), self.workdir))
        self.assertEqual(values['target_mode'], 'tar')
        self.assertEqual(values['kernel'], '7')
        self.assertEqual(values['bootloader'], '5')
        self.assertEqual(values['target_dir'], '/var/lib/cube-strap/build-abc_123/rootfs')
        self.assertEqual(values['tar_output'], str(self.workdir / 'devbox.tar.xz'))
        self.assertEqual(values['opt_packages'], 'cli,dev')
        self.assertNotIn('root_password', values)

    def test_vm_is_a_raw_uefi_image(self):
        values = parse(strap.config_text(plan(kind='vm', recipe='server', size_gib=20), self.workdir))
        self.assertEqual(values['target_mode'], 'image')
        self.assertEqual(values['image_file'], str(self.workdir / 'devbox.img'))
        self.assertEqual(values['image_size'], '20G')
        self.assertEqual((values['layout'], values['bootloader'], values['kernel']), ('3', '1', '3'))
        self.assertEqual(values['enable_ssh'], 'y')

    def test_every_key_is_one_deb_strap_reads(self):
        source = DEBSTRAP.read_text()
        known = set(re.findall(r'ask(?:_yn|_choice)?\s+"([a-z_0-9]+)"', source)) | set(re.findall(r'CFG\[([a-z_0-9]+)\]', source))
        for kind in strap.KINDS:
            for recipe in strap.recipes_for(kind):
                keys = parse(strap.config_text(plan(kind=kind, recipe=recipe, size_gib=8), self.workdir))
                self.assertEqual(set(keys) - known, set(), (kind, recipe))

    def test_sid_has_no_security_suite_and_custom_mirror_switches_choice(self):
        values = parse(strap.config_text(plan(suite='sid', mirror='http://mirror.local/debian'), self.workdir))
        self.assertEqual((values['enable_security'], values['enable_updates']), ('n', 'n'))
        self.assertEqual((values['mirror_choice'], values['custom_mirror']), ('3', 'http://mirror.local/debian'))

    def test_rejects_directories_that_are_not_builds(self):
        with self.assertRaises(ValueError):
            strap.root_workdir(Path('/tmp/../etc'))

    def test_config_file_is_private(self):
        with tempfile.TemporaryDirectory(prefix='build-') as directory:
            path = strap.write_config(plan(), Path(directory))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertIn('user_password = "secret"', path.read_text())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                strap.write_config(plan(), Path(directory))
            self.assertEqual(os.listdir(directory), [])

    def test_deb_strap_accepts_the_chroot_config(self):
        """A dry run of the vendored deb-strap with a generated config, as this user."""
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / 'build-test'
            workdir.mkdir()
            text = strap.config_text(plan(recipe='tools'), workdir).replace(
                str(strap.ROOT_WORK / 'build-test'), str(workdir / 'root'))
            (workdir / 'cube.conf').write_text(text)
            done = subprocess.run(['bash', str(DEBSTRAP), '--config', str(workdir / 'cube.conf'), '--unattended',
                                   '--dry-run', '--log', str(workdir / 'log')], capture_output=True, text=True,
                                  env=dict(os.environ, TMPDIR=directory), timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn('Kernel:          none', done.stdout + done.stderr)
            self.assertIn('tar --one-file-system', done.stdout + done.stderr)


class Commands(unittest.TestCase):
    def test_build_argv_is_one_noninteractive_sudo(self):
        workdir = Path('/c/build-x')
        argv = strap.build_argv(plan(kind='vm'), workdir / 'cube.conf', workdir)
        self.assertEqual(argv[:3], ['sudo', '-n', '--'])
        self.assertIn('--kind', argv)
        self.assertEqual(argv[argv.index('--kind') + 1], 'vm')
        self.assertEqual(argv[argv.index('--owner') + 1], f'{os.getuid()}:{os.getgid()}')

    def test_import_and_run(self):
        self.assertEqual(strap.import_args(plan(size_gib=3, lifecycle='ephemeral'), Path('/a/devbox.tar.xz')),
                         ['--import', 'devbox', '/a/devbox.tar.xz', '3072', 'ephemeral'])
        with mock.patch.object(strap, 'whitecube_command', return_value='/usr/bin/whitecube'):
            self.assertEqual(strap.vm_import_argv(plan(), Path('/a/devbox.img'))[1:],
                             ['import', '--type', 'ext4', '/a/devbox.img', 'devbox'])
            argv = strap.vm_run_argv('devbox', 4096, forward_ssh=True)
        self.assertEqual(argv[1:3], ['run', 'devbox'])
        self.assertIn('--uefi', argv)
        self.assertEqual(argv[-2:], ['--forward', '2222:22'])

    def test_progress_from_banners(self):
        self.assertEqual(strap.stage_fraction(strap.clean_line('\x1b[1m═══ Exporting Rootfs to x ═══\x1b[0m')), .85)
        self.assertIsNone(strap.stage_fraction('[*] something'))


class Helper(unittest.TestCase):
    helper = HERE / 'cube-strap-build'

    def run_helper(self, *args):
        return subprocess.run(['bash', str(self.helper), *args], capture_output=True, text=True, timeout=30)

    def test_refuses_without_root_and_bad_arguments(self):
        self.assertIn('must run as root', self.run_helper('--kind', 'vm').stderr)
        self.assertIn('unknown argument', self.run_helper('--nope').stderr)

    def test_same_build_id_rule_as_engine(self):
        self.assertIn('^build-[A-Za-z0-9_]{1,32}$', self.helper.read_text())
        self.assertEqual(strap.BUILD_ID.pattern, r'build-[A-Za-z0-9_]{1,32}\Z')
        self.assertIn('ROOT_WORK=/var/lib/cube-strap', self.helper.read_text())


class History(unittest.TestCase):
    def test_round_trip_never_stores_passwords(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(strap, 'STATE', Path(directory)):
            strap.record(plan(password='hunter22'), 'built', 61)
            strap.record(plan(name='other', kind='vm'), 'failed', 3)
            strap.record(plan(password='again'), 'built', 5)
            items = strap.load_history()
            self.assertEqual([item['name'] for item in items], ['other', 'devbox'])
            self.assertNotIn('hunter22', (Path(directory) / 'cubes.json').read_text())
            strap.forget('other', 'vm')
            self.assertEqual([item['name'] for item in strap.load_history()], ['devbox'])
            strap.save_draft(plan(password='x' * 9))
            self.assertNotIn('password', strap.load_draft())
            self.assertEqual(strap.plan_from(dict(strap.load_draft(), size_gib='bad')).size_gib, 4)


if __name__ == '__main__':
    unittest.main()
