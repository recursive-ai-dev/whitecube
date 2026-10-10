project('whitecube', 'c', version: '0.2.0', default_options: ['c_std=c11', 'warning_level=2'])
add_project_arguments('-I' + meson.current_source_dir() / 'src', '-I' + meson.current_source_dir() / 'src/protocols', language: 'c')
wlroots = dependency('wlroots-0.18')
wayland_server = dependency('wayland-server')
xkbcommon = dependency('xkbcommon')
cairo = dependency('cairo')
pangocairo = dependency('pangocairo')
gio = dependency('gio-2.0')
libinput = dependency('libinput')
subdir('data/schemas')
m = meson.get_compiler('c').find_library('m')
compositor_args = ['-DWLR_USE_UNSTABLE', '-Wno-unused-parameter']
compositor_deps = [wlroots, wayland_server, xkbcommon, cairo, pangocairo, gio, libinput, m]
executable('whitecube', 'src/whitecube.c',
  c_args: compositor_args,
  dependencies: compositor_deps,
  install: true, install_dir: get_option('bindir'))
compositor_test = executable('compositor-test', 'tests/compositor-test.c',
  c_args: compositor_args, dependencies: compositor_deps)
test('compositor-config-and-bindings', compositor_test)
status_theme_test = executable('status-theme-test', 'tests/status-theme-test.c', dependencies: [m])
test('status-theme-palette', status_theme_test)

wayland_client = dependency('wayland-client')
wayland_cursor = dependency('wayland-cursor')
client_deps = [wayland_client, wayland_cursor, xkbcommon, cairo, pangocairo, gio, m]
settings_sources = ['src/protocols/xdg-shell-protocol.c', 'src/protocols/wlr-output-management-protocol.c']
executable('cube-settings', ['src/cube-settings.c'] + settings_sources,
  dependencies: client_deps, c_args: ['-Wno-unused-parameter'], install: true)
settings_test = executable('settings-test', ['tests/settings-test.c'] + settings_sources,
  dependencies: client_deps, c_args: ['-Wno-unused-parameter'])
test('settings-privacy-and-navigation', settings_test, depends: compiled_schema)
status = executable('cube-status', 'src/cube-status.c', 'src/protocols/wlr-layer-shell-protocol.c', 'src/protocols/xdg-shell-protocol.c',
  dependencies: client_deps, c_args: ['-Wno-unused-parameter'], install: true)
test('status-privacy-and-keyboard', status, args: ['--selftest'])
desktop_test = executable('desktop-state-test', 'tests/desktop-state-test.c')
test('private-desktop-state', desktop_test)
python = find_program('python3')
test('privacy-backend', python, args: ['-I', meson.current_source_dir() / 'tests/test-privacy.py'], timeout: 60)
install_data('bin/cube-privacy', install_dir: get_option('bindir'), install_mode: 'rwxr-xr-x')
test('per-user-install', python, args: ['-I', meson.current_source_dir() / 'tests/test-install.py'])

install_data('bin/cube-preferences', install_dir: get_option('bindir'), install_mode: 'rwxr-xr-x')
test('native-preference-validation', python, args: ['-I', meson.current_source_dir() / 'tests/test-preferences.py'],
  depends: compiled_schema, env: ['GSETTINGS_SCHEMA_DIR=' + meson.current_build_dir() / 'schemas'])
