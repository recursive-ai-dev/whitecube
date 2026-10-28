# Whitecube diagnostics

Run `whitecube-doctor` after installing or upgrading the desktop. It reports
problems and suggests commands; it never changes files, services or settings and
does not need sudo. The final line gives the number of failed checks, warnings,
passed checks and informational findings.

Use `whitecube-doctor --check package --check libraries` before logging into the
new desktop. The package checks detect missing or unexecutable startup programs,
missing settings schemas, missing Chat runtime files, unresolved shared libraries
and older user installations that can shadow the package. Library checks inspect
the actual system desktop binaries when the Debian package is installed.
Runtime dependency checks include Chat's bundled Electron executable. Failed
library inspection reports a warning so an unavailable or unsuccessful `ldd`
cannot produce a false healthy result; unresolved libraries produce a failure.

Inside the desktop, `whitecube-doctor --check session` checks the runtime
directory's ownership and private 0700 permissions and verifies that
`WAYLAND_DISPLAY` names a socket. An ordinary shell outside a desktop gets an
informational finding when `XDG_RUNTIME_DIR` is absent; a Wayland session without
that directory is a failure.

`whitecube-doctor --list-checks` lists the available groups: `package`, `network`,
`drift`, `security`, `libraries`, `session` and `folders`. Repeat `--check` to
select several groups. With no selection, every group runs. A broken network
module or individual check cannot prevent the other checks from reporting.

For scripts, `whitecube-doctor --json` prints the existing JSON list format. Every
item contains `level`, `name`, `detail` and `fix`. Levels are `ok`, `info`, `WARN`
and `FAIL`. Exit status is 1 when any finding is `FAIL`, otherwise 0. Add `--strict`
to also return 1 for `WARN`; invalid command arguments return 2. JSON output
contains no human summary line.

To compare an installed package with a source checkout:

```sh
whitecube-doctor --project /path/to/whitecube --check drift
```

Package comparisons use `/usr/bin/whitecube-compositor` and the packaged systemd
unit path. They ignore account preferences and system configuration files that
may have been intentionally changed. If no source files are available, the report
says that comparison was skipped. Source comparisons do not establish that a
package is corrupt; locally rebuilt sources may intentionally differ.
