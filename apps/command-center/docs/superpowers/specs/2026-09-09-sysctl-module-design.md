# Design: Real System Execution Foundation + Sysctl Module

## Context

The existing app (`App.tsx`, `execution.ts`, `storage.ts`, `components/`) is a
browser-only simulation. `execution.ts` generates deterministic *fake* output
from a hash of the command string; nothing it does ever touches the real
operating system. The user wants to grow this into a real Linux system
administration command center covering sysctl, udev, systemd, gsettings,
GRUB boot parameters, a boot password, network configuration, PAM, and
netfilter (iptables/ip6tables/ebtables/nftables/xtables).

That list is eight-plus independent subsystems, each with its own file
formats, risk profile, and failure modes. This spec does not design all of
them. It designs:

1. The shared privileged-execution foundation every subsystem module will
   sit on top of.
2. The first module built on that foundation: **sysctl** (chosen as the
   lowest-blast-radius subsystem — plain key/value runtime kernel
   parameters with a well-understood, easily reverted file format).

Later modules (systemd units, network config, firewall, PAM, udev,
gsettings, GRUB/boot password) are out of scope here and will each get
their own follow-up spec, reusing this foundation. Network config and
firewall in particular will need an additional safety mechanism (e.g.
auto-revert on unconfirmed change) before they're safe to build, since a
bad rule can cut off the very access used to fix it — that mechanism is
deferred to those modules' own specs.

This machine already runs a separate, hand-maintained hardening suite at
`~/Documents/airgap/` (`harden-sysctl.sh`, `harden-dev-block.sh`,
`airgap-kernel-cull/`) that locks it down for airgapped/untrusted-network
use. `harden-sysctl.sh` in particular already manages its own drop-in
(`/etc/sysctl.d/99-airgap-hardening.conf`) with its own backup/revert/
dry-run/status logic, and hard-codes safety invariants (`net.ipv4.ip_forward`
forced to `0` and guarded against ever being raised; `vm.mmap_min_addr`
guarded against being lowered to an unsafe value). This module must never
let its own writes silently override that baseline — see "Airgap
coexistence" below.

## Goals

- Real reads and real, root-level writes of sysctl values — no more
  simulation for this subsystem.
- A privileged-execution architecture (daemon + polkit + D-Bus) that later
  subsystem modules can reuse without re-architecting.
- Safety rails appropriate to a tool that can affect kernel behavior:
  confirmation before apply, OS-level authentication before apply, atomic
  writes, backups, and one-click revert.
- Reuse of the existing UI shell (Header, Sidebar, Terminal,
  ConfirmationModal) rather than a rebuild; the "scripts"/"shortcuts"
  concept already in the app is unrelated to system subsystem modules and
  is left as-is.

## Non-goals

- Building any subsystem other than sysctl in this pass.
- A GUI installer for the privileged daemon — the one-time root install is
  a documented manual script, not something the running app does to
  itself.
- Byte-level streaming of command output. `sysctl --system` completes in
  well under a second; the daemon returns its full captured output in one
  response. A future module with a genuinely long-running operation may
  revisit incremental streaming on its own merits.

## Architecture

Four pieces, three trust boundaries:

1. **Frontend** — the existing React/Vite app, unprivileged, runs in the
   browser. Adds a new "Sysctl" panel as a new Sidebar section. Talks only
   to the local relay over HTTP/WebSocket on `127.0.0.1`.

2. **Relay** (new, `relay/`, Node/Express) — runs as the normal user, no
   elevated privileges. Its only job is translating browser requests into
   System-Bus D-Bus calls to the daemon and returning results (including
   command output) to the frontend for display in the existing `Terminal`
   component. It cannot mutate system state itself. (Deliberately not
   named "bridge" — that word collides with real Linux network bridge
   interfaces/devices, which this has nothing to do with; the relay never
   touches network interface configuration.)

3. **Privileged daemon** (new, `command-center-helperd`, Go, compiled to a
   single static binary) — a systemd system service, D-Bus-activatable,
   owning the bus name `ca.smddev.CommandCenter1` on the *system* bus. It
   is the only piece of this stack that runs as root. Every mutating
   D-Bus method calls `org.freedesktop.PolicyKit1.Authority.CheckAuthorization`
   before doing anything, keyed to a specific polkit action id. Go was
   chosen for a single static binary with no runtime/interpreter
   dependency, keeping the privileged surface minimal and easy to audit.

4. **Polkit action definitions** (new, XML under
   `/usr/share/polkit-1/actions/`) — one action id per capability (e.g.
   `ca.smddev.commandcenter.sysctl.apply`), each independently configurable
   (e.g. "remembered for the session" vs. "required every time"), so later
   riskier modules can demand stricter re-authentication than sysctl does.
   The existing desktop's polkit authentication agent (already running
   under GNOME/KDE) supplies the native password/fingerprint dialog — no
   new auth UI is built by this app.

**Defense in depth beyond polkit:** the D-Bus system-bus policy file
(`/usr/share/dbus-1/system.d/*.conf`) restricts which Unix user may call
the daemon's interface at all, independent of the polkit authorization
check on mutating calls.

**One-time install:** the daemon binary, its `.service` unit, the D-Bus
policy file, and the polkit `.policy` file all require root to install.
This is a single documented `sudo scripts/install-daemon.sh`, run once,
separate from the normal `npm run dev` app workflow. The app cannot
bootstrap this itself (installing root-owned files requires root, which is
exactly the privilege the app doesn't have before this step runs).

## Sysctl Module

### D-Bus interface — `ca.smddev.CommandCenter1.Sysctl`

- **`List()`** → every key under `/proc/sys`, each with its current
  runtime value, which file (if any) sets it at boot, and whether that
  file is the app's own managed file. Read-only; no polkit check (viewing
  values isn't sensitive on its own), gated only by the D-Bus policy
  file's user restriction.

- **`SetManaged(entries: [{key, value}])`** → structured input, *not* raw
  file text — the frontend/relay never send file content directly; the
  daemon formats `/etc/sysctl.d/98-command-center.conf` itself (see
  "Airgap coexistence" for why `98-`, not `99-`). Requires polkit
  authorization (`ca.smddev.commandcenter.sysctl.apply`). Before writing
  anything, validates every key actually exists under `/proc/sys`
  (rejecting typos or attempts to target nonexistent paths) and rejects
  any entry that violates a hard-coded invariant guard (below) —
  validation failures reject the entire batch; nothing is written
  partially. On success: backs up the current managed file to
  `.bak.<unix-timestamp>` (keeping the last 10, pruning older), writes the
  new managed file via temp-file-then-atomic-rename, runs
  `sysctl --system`, and returns its captured stdout/stderr.

- **`ListBackups()`** → available backups with timestamp and a diff
  summary against the current managed file.

- **`RestoreBackup(timestamp)`** → reads the named backup's entries and
  replays them through the same `SetManaged` path, so a restore gets the
  same validation, locking, and fresh backup-of-the-restore as any other
  apply. Restores are never a silent bypass of the safety flow.

Concurrency: the daemon holds an internal lock around `SetManaged` so two
overlapping apply calls can't interleave writes.

### Airgap coexistence

Two independent defenses, not just one, so a bug in either doesn't undo
this machine's existing hardening:

1. **File order.** The managed file is named `98-command-center.conf`,
   which sorts — and therefore loads — *before*
   `99-airgap-hardening.conf` alphabetically. `sysctl --system` applies
   files in lexical order, later files overriding earlier ones for the
   same key, so on any key both files set, `harden-sysctl.sh`'s value
   always wins, never ours.
2. **Hard-coded guards in the daemon.** `SetManaged` rejects, independent
   of file order, any entry that would set `net.ipv4.ip_forward` above
   `0` or `vm.mmap_min_addr` below `4096` — mirroring the same two
   invariants `harden-sysctl.sh`'s `essentials_ok` check already
   enforces. This means even if the file-naming defense were ever
   defeated (e.g. a future module renaming files), the daemon itself
   still refuses to weaken these two invariants.

Everything else in `harden-sysctl.sh`'s `DESIRED` map (kptr_restrict,
dmesg_restrict, ptrace_scope, the IPv4/IPv6 redirect/forwarding/rp_filter
settings, etc.) remains freely editable through this module — it is not
read-only — but on conflict with `harden-sysctl.sh`, the hardening script
still wins by file order per point 1.

### Frontend flow

The Sysctl panel lists all keys from `List()`, searchable/filterable, each
row showing its current value and source file. A small curated JSON
catalog bundled with the app (~40 well-known keys — e.g.
`net.ipv4.ip_forward`, `kernel.dmesg_restrict`, `vm.overcommit_memory`,
`fs.protected_hardlinks`, `kernel.yama.ptrace_scope`) overlays a
human-readable description and a risk tier onto matching rows. Keys not in
the catalog remain fully visible and editable, just shown as "unreviewed"
with no description — the panel is a browsable view of the whole
namespace, not limited to the curated list.

Editing a value doesn't call anything yet — it marks that row "pending"
with an old → new diff, purely client-side. Clicking **Apply** shows all
pending changes as a diff in the existing `ConfirmationModal` (risk-tagged
keys get an added stability/security warning), and only on confirmation
does the frontend call the relay, which calls `SetManaged` over D-Bus,
which triggers the native polkit dialog. The daemon's returned output
lands as one log entry in the existing `Terminal` component.

A "History" section in the same panel lists backups from `ListBackups()`
with a **Restore** button per entry, routed through the same
confirm → polkit → apply path as a normal edit.

### Error handling & safety rails

- **Auth denied/cancelled** → daemon returns an authorization error;
  nothing is written; Terminal shows a clear "authorization denied" line.
- **Validation failure** → entire batch rejected before any disk write;
  error names the offending entry.
- **Crash-safety** → temp-file-then-atomic-rename means a crash mid-write
  can never leave a half-written managed file.
- **Audit trail** → the daemon runs under systemd, so its stdout/stderr
  (every apply, old → new content, timestamp) is already in the journal;
  `journalctl -u command-center-helperd` gives a full history with no
  additional logging code.
- **Daemon unreachable** → the relay detects the D-Bus name is absent and
  surfaces a specific "daemon not installed — run
  `sudo scripts/install-daemon.sh`" message rather than a generic
  connection error.

## Testing

- **Daemon (Go) unit tests** — validation, formatting, and backup/prune
  logic run against a fake `/proc/sys`-like fixture tree (base path
  overridable via flag/env, defaulting to the real path). No root needed,
  real system never touched.
- **Daemon integration tests** — a test build with polkit checks stubbed
  (compile-time flag, never present in the installed binary) drives real
  D-Bus calls against the fixture tree, exercising the full
  apply/backup/restore path.
- **Relay tests** — standard HTTP tests against a mocked D-Bus client,
  covering request validation and error-message propagation.
- **Frontend tests** — extend the existing patterns in `TESTING.md` to the
  new panel: diff rendering, confirmation gating, catalog overlay — against
  a mocked relay, no real backend required.
- **Manual smoke test (documented, not automated)** — after running the
  installer once, apply one low-risk key end-to-end on a real machine,
  confirm with `sysctl <key>` in a separate terminal, then restore from
  backup and reconfirm. Recommended first on a disposable VM/container
  rather than a daily-driver machine, since this is new privileged code
  touching real kernel parameters.

## Open questions for later subsystem specs

- Network config and firewall modules need an auto-revert-on-timeout (or
  equivalent) safety mechanism before they're safe to build, since a bad
  rule can cut off the access needed to fix it. Not designed here.
- Whether later modules (e.g. firewall) warrant the daemon exposing a
  signal-based progress channel instead of synchronous request/response,
  once one of them has an operation that's actually long-running.
