schema = configure_file(input: 'org.whitecube.desktop.gschema.xml', output: 'org.whitecube.desktop.gschema.xml', copy: true)
compiled_schema = custom_target('whitecube-schema', input: schema, output: 'gschemas.compiled', command: [find_program('glib-compile-schemas'), '--strict', meson.current_build_dir()], build_by_default: true, install: true, install_dir: get_option('datadir') / 'whitecube/schemas')
install_data('org.whitecube.desktop.gschema.xml', install_dir: get_option('datadir') / 'whitecube/schemas')
