# Chrootable Manager

`chrootable-manager` is a single Bash script that manages Debian workspaces
stored as ext4 image files. Each workspace is bootstrapped with
`debootstrap --variant=minbase` plus an optional profile, and opened as a root
shell inside private mount, UTS, IPC and PID namespaces. It has a terminal menu
(whiptail) and a scriptable command line with JSON output.

```bash
sudo ./chrootable-manager                      # menu
sudo ./chrootable-manager --create compiler 4096 persistent build
sudo ./chrootable-manager --enter compiler
./chrootable-manager --demo                    # sample workspaces, no root
```

## Workspaces

**Profiles** add a focused package set to minbase: `minimal`, `build`
(C/C++ toolchain, Git), `python` (Python 3, venv, pip) and `network` (DNS,
ping, curl, SSH client). `--profiles` lists suggested and minimum sizes; an
image below the minimum is refused before anything is downloaded.

**Lifecycles.** *Persistent* sessions write to the image. *Ephemeral* sessions
mount it read-only under an OverlayFS whose writes live in memory and vanish on
exit (`CHROOTABLE_EPHEMERAL_SIZE` caps that memory). `--try` runs a disposable
session of any workspace.

**Sessions** get `/proc`, `/dev`, a private `/run`, the host's DNS servers
(loopback stubs such as systemd-resolved's are replaced by fallbacks), and a
`TERM`/`LANG` the guest can actually use. `--isolated` adds an empty network
namespace. `--exec NAME -- COMMAND` runs one command; its exit status is
returned as-is.

**Recovery and maintenance:** snapshots (`--snapshot`, `--restore` saves the
current state first, so a restore can be undone), clones and renames (reflinks
when the filesystem supports them; free space is checked otherwise), capacity
expansion (preallocated, never shrinking), forced ext4 repair, exports
(`.tar.gz` plus a `.sha256` file) and imports of exports or any root
filesystem tarball (`--import`, checksum verified when present). `--cleanup`
finds files left behind by operations that were killed, and removes them with
`--yes`.

Every image keeps adjacent metadata: suite, release, architecture, profile,
size, description, creation and last-use times, clone or import origin.

## Command line

Read-only commands work without root when the store is readable:

```bash
chrootable-manager --dashboard
chrootable-manager --list --json
chrootable-manager --inspect compiler --json
chrootable-manager --snapshots compiler --json
chrootable-manager --exports --json
chrootable-manager --activity 50 --json
chrootable-manager --doctor
```

Everything else needs root. `--help` lists every command; an invalid call
prints the usage line of the command that was meant.

## Storage and configuration

The store defaults to `/var/lib/chrootable-manager` (`images/`, `snapshots/`,
`exports/`, `logs/`, plus an `activity.tsv` journal). New directories are
private; existing ones keep their mode as long as they are owned by the user and
not writable by group or others, so an administrator can make the store
readable for root-free browsing.

| Variable | Default | Purpose |
|---|---|---|
| `CHROOTABLE_HOME` | `/var/lib/chrootable-manager` | Store location (absolute path) |
| `CHROOTABLE_DEBIAN_SUITE` | `stable` | Suite passed to debootstrap |
| `CHROOTABLE_DEBIAN_MIRROR` | `http://deb.debian.org/debian` | `http(s)://`, `file://` or `ssh://` mirror |
| `CHROOTABLE_AUTO_INSTALL` | `1` | `0` fails instead of installing missing host packages |
| `CHROOTABLE_FALLBACK_DNS` | `1.1.1.1 8.8.8.8` | Resolvers when the host only has a local stub |
| `CHROOTABLE_EPHEMERAL_SIZE` | kernel default (50% of RAM) | tmpfs size for ephemeral writes, e.g. `4G` |

Write operations install missing host tools with `apt-get` (log:
`/var/log/chrootable-manager-dependencies.log`); `--setup` does only that.

## Isolation

Sessions run as host root and expose host devices through `/dev`; `/sys` is
not mounted. This is a convenient development environment, not a security
sandbox.

## Tests

`tests/integration.sh` is an opt-in end-to-end test. It downloads two minimal
Debian roots, so run it only as root on a machine where loop mounts and
temporary 1 GiB images are acceptable.
