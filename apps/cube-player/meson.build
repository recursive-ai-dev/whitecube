project('cube-player', 'c', version: '1.0.0', default_options: ['c_std=c11', 'warning_level=2', 'buildtype=debugoptimized'])
cc = meson.get_compiler('c')
root = meson.project_source_root()
mpv_dir = root / '.deps/usr/lib/x86_64-linux-gnu'
install_libdir = get_option('private_libdir') != '' ? get_option('private_libdir') : mpv_dir
mpv = cc.find_library('mpv', dirs: mpv_dir)
deps = [mpv, dependency('wayland-client'), dependency('wayland-cursor'), dependency('wayland-egl'), dependency('egl'), dependency('glesv2'), dependency('xkbcommon'), dependency('pangocairo'), dependency('gio-2.0'), cc.find_library('m')]
protocols = dependency('wayland-protocols')
scanner = find_program('wayland-scanner')
xml = protocols.get_variable(pkgconfig: 'pkgdatadir') / 'stable/xdg-shell/xdg-shell.xml'
xdg_h = custom_target('xdg-shell-client', input: xml, output: 'xdg-shell-client.h', command: [scanner, 'client-header', '@INPUT@', '@OUTPUT@'])
xdg_c = custom_target('xdg-shell-code', input: xml, output: 'xdg-shell-protocol.c', command: [scanner, 'private-code', '@INPUT@', '@OUTPUT@'])
inc = include_directories('src', '.deps/usr/include')
player = executable('cube-player', ['src/main.c', 'src/player.c', 'src/state.c', 'src/platform.c', xdg_h, xdg_c], include_directories: inc, dependencies: deps, build_rpath: mpv_dir, install_rpath: install_libdir, install: true)
state_test = executable('test-state', ['tests/test_state.c', 'src/state.c'], include_directories: inc, dependencies: [dependency('gio-2.0'), cc.find_library('m')])
test('state-and-media-paths', state_test)
ui_test = executable('test-ui', ['tests/test_ui.c', 'src/player.c', 'src/state.c', 'src/platform.c', xdg_h, xdg_c], include_directories: inc, dependencies: deps, build_rpath: mpv_dir)
test('responsive-ui-and-playlists', ui_test)
python = find_program('python3')
test('desktop-integration', python, args: [files('tests/test_install.py')])
test('native-wayland-playback', python, args: [files('tests/test_wayland.py')], timeout: 120, suite: 'integration', is_parallel: false)
add_test_setup('default', env: {'CUBE_PLAYER_BUILD': meson.current_build_dir()}, is_default: true)
