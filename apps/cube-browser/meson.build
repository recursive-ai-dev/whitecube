project('cube-browser', 'c', version: files('VERSION'), default_options: ['c_std=c11', 'warning_level=2', 'buildtype=debugoptimized'])
cc = meson.get_compiler('c')
add_project_arguments('-D_FILE_OFFSET_BITS=64', language: 'c')
root = meson.project_source_root()
local_lib = root / '.deps/prefix/lib'
install_libdir = get_option('private_libdir') != '' ? get_option('private_libdir') : local_lib
inc = include_directories('src', '.deps/include')
wayland = dependency('wayland-client', version: '>=1.18')
cursor = dependency('wayland-cursor')
xkb = dependency('xkbcommon', version: '>=0.5')
cairo = dependency('cairo')
pango = dependency('pangocairo')
glib = dependency('glib-2.0')
sqlite = dependency('sqlite3', static: true)
png = dependency('libpng')
jpeg = dependency('libjpeg', static: true)
expat = dependency('expat')
math = cc.find_library('m')
protocols = dependency('wayland-protocols')
scanner = find_program('wayland-scanner')
xml = protocols.get_variable(pkgconfig: 'pkgdatadir') / 'stable/xdg-shell/xdg-shell.xml'
xdg_h = custom_target('xdg-shell-client', input: xml, output: 'xdg-shell-client.h', command: [scanner, 'client-header', '@INPUT@', '@OUTPUT@'])
xdg_c = custom_target('xdg-shell-code', input: xml, output: 'xdg-shell-protocol.c', command: [scanner, 'private-code', '@INPUT@', '@OUTPUT@'])
policy = static_library('cube-policy', ['src/policy.c','src/http.c'], include_directories: inc)
ipc = static_library('cube-ipc', ['src/ipc.c', 'src/sandbox.c'], include_directories: inc)
python = find_program('python3')
suffix_data = custom_target('suffix-data', input: 'data/public_suffix_list.dat', output: 'suffix-data.h', command: [python, files('scripts/suffix-data.py'), '@INPUT@', '@OUTPUT@'])
session = static_library('cube-session', ['src/session.c','src/broker.c', suffix_data], dependencies: [sqlite, glib], include_directories: inc)
storage = static_library('cube-storage', 'src/storage.c', link_with: policy, dependencies: [sqlite, math], include_directories: inc)
image = static_library('cube-image', ['src/image.c', 'src/svg.c'], dependencies: [cairo, png, jpeg, glib, expat, math], include_directories: inc)
forms = static_library('cube-forms', 'src/forms.c', dependencies: glib, include_directories: inc)
media = static_library('cube-media', 'src/media.c', dependencies: [glib, math], include_directories: inc)
quickjs_name = get_option('b_sanitize') == 'address,undefined' ? 'quickjs-core-sanitize' : 'quickjs-core'
quickjs = cc.find_library(quickjs_name, dirs: root / '.deps/lib', static: true)
js = static_library('cube-js', 'src/js.c', include_directories: inc, dependencies: [quickjs, math])
web_bootstrap = custom_target('web-bootstrap', input: 'src/web.js', output: 'web-bootstrap.h', command: [python, files('scripts/embed.py'), '@INPUT@', '@OUTPUT@'])
dom = static_library('cube-dom', ['src/dom.c', web_bootstrap], include_directories: inc, dependencies: [glib, pango, cairo, math])
engine = static_library('cube-engine', ['src/engine.c', 'src/image-document.c', 'src/style.c', 'src/layout.c', 'src/flex.c', 'src/interaction.c'], dependencies: [cairo, pango, glib, math], include_directories: inc)
browser = executable('cube-browser', ['src/main.c','src/download.c', 'src/platform.c', 'src/url.c', xdg_h, xdg_c],
    dependencies: [wayland, cursor, xkb, cairo, pango, glib, sqlite, math], link_with: [ipc, storage, policy, session],
    include_directories: inc, build_rpath: local_lib, install_rpath: install_libdir, install: true)
renderer = executable('cube-renderd', 'src/render-main.c', dependencies: [quickjs, cairo, pango, glib, math, png, jpeg, expat],
    link_with: [engine, image, forms, media, ipc, policy, dom, js], include_directories: inc, build_rpath: local_lib, install_rpath: install_libdir, install: true)
tls = cc.find_library('mbedtls', dirs: root / '.deps/lib', static: true)
x509 = cc.find_library('mbedx509', dirs: root / '.deps/lib', static: true)
crypto = cc.find_library('mbedcrypto', dirs: root / '.deps/lib', static: true)
net = executable('cube-netd', ['src/net-main.c', 'src/net.c'], dependencies: [tls, x509, crypto],
    link_with: [ipc, policy], include_directories: inc, install: true)
engine_test = executable('test-engine', 'tests/test_engine.c', link_with: [engine, image, forms, media, policy], include_directories: inc,
    dependencies: [cairo, pango, glib, math, png, jpeg, expat], build_rpath: local_lib)
image_test = executable('test-image', 'tests/test_image.c', link_with: image, include_directories: inc,
    dependencies: [cairo, png, jpeg, glib, expat, math], build_rpath: local_lib)
storage_test = executable('test-storage', 'tests/test_storage.c', link_with: storage, include_directories: inc,
    dependencies: [sqlite, math])
core_test = executable('test-core', ['tests/test_core.c', 'src/url.c'], link_with: ipc, include_directories: inc, dependencies: glib)
test('engine', engine_test, workdir: root)
layout_test = executable('test-layout', 'tests/test_layout.c', link_with: [engine, image, forms, media, policy], include_directories: inc,
    dependencies: [cairo, pango, glib, math, png, jpeg, expat], build_rpath: local_lib)
test('layout-compatibility', layout_test)
test('images', image_test, workdir: root)
test('storage', storage_test)
test('ipc-and-address', core_test)
sandbox_test = executable('test-sandbox', 'tests/test_sandbox.c', link_with: ipc, include_directories: inc)
test('sandbox', sandbox_test)
policy_test = executable('test-policy', 'tests/test_policy.c', link_with: policy, include_directories: inc)
test('web-policy', policy_test)
forms_test = executable('test-forms', 'tests/test_forms.c', link_with: forms, include_directories: inc, dependencies: glib)
test('forms', forms_test)
media_test = executable('test-media', 'tests/test_media.c', link_with: media, include_directories: inc, dependencies: [glib, math])
test('media', media_test)
test('x-free-link-closure', find_program('scripts/check-links.sh'), args: [browser, renderer, net])
python = find_program('python3')
test('version', python, args: [files('scripts/version.py'), '--check'])
test('network', python, args: [files('tests/test_net.py'), net])
test('workers', python, args: [files('tests/test_workers.py'), renderer, net])
test('interactions', python, args: [files('tests/test_interactions.py'), renderer])
test('cli-and-installer', python, args: [files('tests/test_app.py'), browser])
test('subresources', python, args: [files('tests/test_subresources.py'), browser], timeout: 60)
test('wayland-e2e', python, args: [files('scripts/e2e.py'), '--browser', browser.full_path()], timeout: 120, suite: 'integration', is_parallel: false)

session_test = executable('test-session', 'tests/test_session.c', link_with: [session, storage, policy, ipc], include_directories: inc, dependencies: [sqlite, glib, math])
test('sessions', session_test)

js_test_args = get_option('b_sanitize') == 'address,undefined' ? ['-DCB_COMPONENT_SANITIZER'] : []
js_test = executable('test-js', 'tests/test_js.c', link_with: [js, ipc], include_directories: inc, dependencies: [quickjs, math], c_args: js_test_args)
# These exercise the production 100 ms wall-clock JS turn limit. Running them
# beside render/fuzz suites can consume that deadline through CPU contention;
# keep the real limits and reserve the test runner for each scripted suite.
test('javascript-runtime', js_test, is_parallel: false)

test('web-apis', python, args: [files('tests/test_web.py')], timeout: 60, is_parallel: false)

download_test = executable('test-download', ['tests/test_download.c','src/download.c'], link_with: ipc, include_directories: inc)
test('downloads', download_test)

dom_test = executable('test-dom', 'tests/test_dom.c', link_with: [dom, js, engine, image, forms, media, policy, ipc], include_directories: inc,
    dependencies: [quickjs, cairo, pango, glib, math, png, jpeg, expat], build_rpath: local_lib)
test('dom-lifecycle', dom_test, is_parallel: false)
fuzz_test = executable('test-fuzz', 'tests/test_fuzz.c', link_with: [engine, image, forms, media, policy, ipc], include_directories: inc,
    dependencies: [cairo, pango, glib, math, png, jpeg, expat], build_rpath: local_lib)
test('mutation-smoke', fuzz_test, timeout: 120)
add_test_setup('default', env: {'CUBE_BROWSER_BUILD': meson.current_build_dir()}, is_default: true)
