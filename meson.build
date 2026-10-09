project('whitecube', 'c', version: '0.1.0', default_options: ['c_std=c11', 'warning_level=2'])
wlroots = dependency('wlroots-0.18')
wayland_server = dependency('wayland-server')
xkbcommon = dependency('xkbcommon')
cairo = dependency('cairo')
pangocairo = dependency('pangocairo')
m = meson.get_compiler('c').find_library('m')
compositor_args = ['-DWLR_USE_UNSTABLE', '-Wno-unused-parameter']
compositor_deps = [wlroots, wayland_server, xkbcommon, cairo, pangocairo, m]
executable('whitecube', 'whitecube.c',
  c_args: compositor_args,
  dependencies: compositor_deps,
  install: true, install_dir: get_option('bindir'))
compositor_test = executable('compositor-test', 'compositor-test.c',
  c_args: compositor_args, dependencies: compositor_deps)
test('compositor-config-and-bindings', compositor_test)
status_theme_test = executable('status-theme-test', 'status-theme-test.c', dependencies: [m])
test('status-theme-palette', status_theme_test)

wayland_client = dependency('wayland-client')
wayland_cursor = dependency('wayland-cursor')
client_deps = [wayland_client, wayland_cursor, xkbcommon, cairo, pangocairo, m]
settings_sources = ['xdg-shell-protocol.c', 'wlr-output-management-protocol.c']
executable('cube-settings', ['cube-settings.c'] + settings_sources,
  dependencies: client_deps, c_args: ['-Wno-unused-parameter'], install: true)
settings_test = executable('settings-test', ['settings-test.c'] + settings_sources,
  dependencies: client_deps, c_args: ['-Wno-unused-parameter'])
test('settings-privacy-and-navigation', settings_test)
status = executable('cube-status', 'cube-status.c', 'wlr-layer-shell-protocol.c', 'xdg-shell-protocol.c',
  dependencies: client_deps, c_args: ['-Wno-unused-parameter'], install: true)
test('status-privacy-and-keyboard', status, args: ['--selftest'])
desktop_test = executable('desktop-state-test', 'desktop-state-test.c')
test('private-desktop-state', desktop_test)
python = find_program('python3')
test('privacy-backend', python, args: ['-I', meson.current_source_dir() / 'test-privacy.py'], timeout: 60)
install_data('cube-privacy', install_dir: get_option('bindir'), install_mode: 'rwxr-xr-x')
test('per-user-install', python, args: ['-I', meson.current_source_dir() / 'test-install.py'])
