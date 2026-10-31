# Cube Chrootables manager

Whitecube bundles Chrootables 1.2.1's Bash image engine privately at
`/usr/libexec/whitecube/chrootable-manager`. The public Whitecube command is
`whitecube-chrootable-manager`. It manages the existing
`/var/lib/chrootable-manager` store; setting `CHROOTABLE_HOME` selects another
store. The standalone package's command is independent.

Run `cube-chrootables` for the Qt desktop. Its **Unlock store** action opens a
terminal for `sudo`; no graphical password prompt is needed. Run read-only
manager commands as your normal user when permissions allow, and use `sudo`
for image changes. For example:

```bash
whitecube-chrootable-manager --doctor --json
sudo whitecube-chrootable-manager --list --json
```

The engine's command reference follows. This documentation and the help's shared
folder example are adapted for Whitecube; see `UPSTREAM.md` for provenance.

```text
Chrootable Manager 1.2.1 - Debian environment workbench

Usage: whitecube-chrootable-manager [COMMAND]
No command opens the terminal dashboard (root required).

Explore (no root required when the store is readable)
  --dashboard                      Workspace overview and storage usage
  --list [--json]                   Inventory; TSV by default
  --inspect NAME [--json]           Image details and metadata
  --profiles                       Available creation profiles
  --capabilities --json             Versioned profiles, releases and limits
  --operations [--json]             Operation IDs, stages and interrupted work
  --snapshots NAME                  List recovery points, even after deletion
  --activity                       Recent operation history
  --logs NAME                      Last completed operation log (200 lines)
  --doctor [--json]                Read-only host readiness and storage checks
  --demo [--dashboard]              Explore sample workspaces without root
  --help | --version

Create and use (root required)
  --create NAME SIZE_MIB persistent|ephemeral [PROFILE]
  --import NAME ARCHIVE.tar.gz [SIZE_MIB] [persistent|ephemeral] [PROFILE]
                                   Rehydrate a .tar.gz export into a workspace.
                                   An empty SIZE_MIB fits capacity to the archive
  --archive-sync SUITE [PROFILE...]
                                   Download everything creates of SUITE need
                                   (all profiles by default) so they work
                                   offline; rerun to refresh
  --enter NAME [--share HOST_DIR] [--read-only] [--restricted-dev]
                                   Open a root shell
  --enter-isolated NAME [--share HOST_DIR] [--read-only] [--restricted-dev]
                                   Open a shell without host network access
  --exec NAME [--isolated] [--share HOST_DIR] [--read-only] [--restricted-dev] -- COMMAND [ARG...]
  --try NAME [--isolated] [--share HOST_DIR] [--read-only] [--restricted-dev] [-- COMMAND [ARG...]]
                                   Disposable session, regardless of image mode
  --clone NAME NEW_NAME             Independent copy (reflink when available)
  --mode NAME persistent|ephemeral  Change the default session lifecycle
  --resize NAME SIZE_MIB            Expand only; no shrinking
  --export NAME                    Export a .tar.gz; print its path

Recovery and maintenance (root required)
  --snapshot NAME LABEL            Save an offline recovery point
  --restore NAME LABEL --yes        Save current state, then restore snapshot
  --drop-snapshot NAME LABEL --yes  Permanently delete one snapshot
  --repair NAME --yes              Forced ext4 repair
  --delete NAME --yes              Delete image; keep snapshots and exports
  --delete-export FILENAME --yes   Delete a retained .tar.gz export archive
  --prune-logs [KEEP_COUNT] --yes  Remove old completed logs (default: keep 20)
  --setup                          Install missing host dependencies
  --recover-operation ID --yes      Finish an interrupted staged publication
  --discard-operation ID --yes      Abandon interrupted work and remove its staged
                                   files, or set aside a damaged operation record

Profiles: minimal (default), build, python, network, rust, go, web.
Sizes are MiB, from 512 to 1048576 (1 TiB). Images are sparse; capacity is not
reserved. Create, expansion and copies check free space before starting.
Shared folders appear at /shared for that session. HOST_DIR must be an existing
absolute directory other than /. Add --read-only to prevent ordinary writes.
Host folder changes persist, including in disposable sessions.

Examples
  sudo whitecube-chrootable-manager --create compiler 4096 persistent build
  sudo whitecube-chrootable-manager --snapshot compiler clean
  sudo whitecube-chrootable-manager --try compiler -- bash -lc "apt-get update"
  sudo whitecube-chrootable-manager --exec compiler -- gcc --version
  sudo whitecube-chrootable-manager --enter compiler --share /path/to/project
  sudo whitecube-chrootable-manager --restore compiler clean --yes

Configuration
  sudo CHROOTABLE_HOME=/srv/chrootables whitecube-chrootable-manager
  CHROOTABLE_DEBIAN_SUITE=stable
  CHROOTABLE_DEBIAN_MIRROR=https://deb.debian.org/debian
  CHROOTABLE_AUTO_INSTALL=0         Fail instead of installing missing tools

These are host-root development environments, not security sandboxes.
Ephemeral sessions discard image writes but still expose host devices and any
writable shared folder. Guest root can undo read-only mounts. Add
--restricted-dev to give a session a minimal /dev without host device nodes.
```
