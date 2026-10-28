project('whitecube', 'c', version: files('packaging/VERSION'),
  default_options: ['c_std=c11', 'warning_level=2', 'buildtype=debugoptimized', 'b_pie=true', 'b_asneeded=true'])

cc = meson.get_compiler('c')
add_project_arguments('-I' + meson.current_source_dir() / 'src', '-I' + meson.current_source_dir() / 'src/protocols',
  '-ffile-prefix-map=' + meson.current_source_dir() + '=.', '-Wno-unused-parameter', '-fstack-protector-strong', '-D_FORTIFY_SOURCE=3', '-Wformat', '-Werror=format-security', language: 'c')
add_project_link_arguments('-Wl,-z,relro,-z,now', language: 'c')
if get_option('b_sanitize').contains('undefined')
  add_project_arguments('-fno-sanitize-recover=undefined', '-fno-omit-frame-pointer', language: 'c')
endif
wlroots = dependency('wlroots-0.18')
wayland_server = dependency('wayland-server')
wayland_client = dependency('wayland-client')
wayland_cursor = dependency('wayland-cursor')
xkbcommon = dependency('xkbcommon')
cairo = dependency('cairo')
pangocairo = dependency('pangocairo')
gio = dependency('gio-2.0')
libinput = dependency('libinput')
m = cc.find_library('m')
python = find_program('python3')
subdir('data/schemas')
schema_env = environment()
schema_env.set('GSETTINGS_SCHEMA_DIR', meson.current_build_dir() / 'data/schemas')
native_rpath = ':'.join([wlroots.get_variable(pkgconfig: 'libdir'), cairo.get_variable(pkgconfig: 'libdir'), '/usr/lib/whitecube'])
compositor_deps = [wlroots, wayland_server, xkbcommon, cairo, pangocairo, gio, libinput, m]
client_deps = [wayland_client, wayland_cursor, xkbcommon, cairo, pangocairo, gio, m]
xdg = 'src/protocols/xdg-shell-protocol.c'
layer = 'src/protocols/wlr-layer-shell-protocol.c'
settings_sources = [xdg, 'src/protocols/wlr-output-management-protocol.c']

# These definitions are shared by direct Meson builds and BUILD.sh. The source
# compatibility name build/whitecube is staged by BUILD.sh; installs use the
# namespaced public command to coexist with the unrelated whitecube image tool.
compositor = executable('whitecube-compositor', 'src/whitecube.c',
  c_args: ['-DWLR_USE_UNSTABLE'], dependencies: compositor_deps,
  build_rpath: native_rpath, install_rpath: '/usr/lib/whitecube', install: true)
ca = executable('cube-ca', ['src/cube-ca.c', layer, xdg], dependencies: [wayland_client, m],
  build_rpath: native_rpath, install_rpath: '/usr/lib/whitecube', install: true)
settings = executable('cube-settings', ['src/cube-settings.c'] + settings_sources, dependencies: client_deps,
  build_rpath: native_rpath, install_rpath: '/usr/lib/whitecube', install: true)
status = executable('cube-status', ['src/cube-status.c', layer, xdg], dependencies: client_deps,
  build_rpath: native_rpath, install_rpath: '/usr/lib/whitecube', install: true)

compositor_test = executable('compositor-test', 'tests/compositor-test.c', c_args: ['-DWLR_USE_UNSTABLE'],
  dependencies: compositor_deps, build_rpath: native_rpath)
test('compositor-config-and-bindings', compositor_test, suite: 'core')
hotplug_test = executable('hotplug-test', 'tests/hotplug-test.c', c_args: ['-DWLR_USE_UNSTABLE'],
  dependencies: compositor_deps, build_rpath: native_rpath)
test('output-hotplug', hotplug_test, env: ['WLR_RENDERER=pixman'], suite: 'core')
status_theme_test = executable('status-theme-test', 'tests/status-theme-test.c', dependencies: [m])
test('status-theme-palette', status_theme_test, suite: 'core')
settings_test = executable('settings-test', ['tests/settings-test.c'] + settings_sources,
  dependencies: client_deps, build_rpath: native_rpath)
test('settings-privacy-and-navigation', settings_test, depends: compiled_schema, env: schema_env, suite: 'core')
status_apps_test = executable('status-apps-test', ['tests/status-apps-test.c', layer, xdg],
  dependencies: client_deps, build_rpath: native_rpath)
test('status-applications', status_apps_test, env: schema_env, suite: 'core')
status_jobs_test = executable('status-jobs-test', ['tests/status-jobs-test.c', layer, xdg],
  dependencies: client_deps, build_rpath: native_rpath)
test('status-background-jobs', status_jobs_test, env: schema_env, suite: 'core')
native_input_test = executable('native-input-test', 'tests/native-input-test.c', dependencies: [xkbcommon])
test('native-input-compose', native_input_test, suite: 'core')
test('status-privacy-and-keyboard', status, args: ['--selftest'], env: schema_env, suite: 'core')
desktop_test = executable('desktop-state-test', 'tests/desktop-state-test.c')
test('private-desktop-state', desktop_test, suite: 'core')
workspace_source = custom_target('workspace-test-source', input: 'tests/workspace-test.py', output: 'workspace-test.c',
  command: [python, '@INPUT@'], capture: true, depend_files: 'src/whitecube.c')
workspace_test = executable('workspace-test', workspace_source)
test('workspace-state', workspace_test, suite: 'core')
test('wallpaper-state', ca, args: ['--self-test'], suite: 'core')
test('wallpaper-preview', ca, args: ['--preview', meson.current_build_dir() / 'cube-space.ppm'], suite: 'core')
test('status-preview', status, args: ['--render', meson.current_build_dir()], env: schema_env, suite: 'core')

foreach item : [['privacy-backend', 'test-privacy.py'], ['per-user-install', 'test-install.py'],
                ['native-preference-validation', 'test-preferences.py'],
                ['settings-transactions', 'test-settings-transactions.py'],
                ['suspend-lock', 'test-suspend.py'], ['build-tools', 'test-build-tools.py'],
                ['builder-cache', 'test-builder.py'], ['package-lifecycle', 'test-lifecycle.py']]
  test(item[0], python, args: ['-I', meson.current_source_dir() / 'tests' / item[1]],
    depends: compiled_schema, env: schema_env, timeout: 120, suite: 'python')
endforeach
install_data('bin/cube-privacy', 'bin/cube-preferences', 'bin/whitecube-suspend', install_dir: get_option('bindir'), install_mode: 'rwxr-xr-x')
