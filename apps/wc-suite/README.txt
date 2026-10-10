Whitecube command suite 2.0

Start with wc-help. Use wc-help COMMAND or wc-COMMAND --help for complete
usage, examples, and dependencies. wc-help --all prints the whole reference;
wc-help --search TEXT and wc-ls TEXT search names, groups, and descriptions.
wc-ls --json provides a machine-readable inventory. Additional executable
wc-* files in ~/.local/bin are discovered automatically without executing them.

Existing command names are preserved. The former wc-help keyboard reference
is now wc-keys (also wc-help --keys). Application wrappers still pass their
arguments through, including wc-settings --page privacy. Use -- before an
application's --help or --version to request its help instead of suite help.

Applications: browser, chat, settings, lab, term, binsys, launch, project,
open, files, gparted. wc-term uses Whitecube's terminal preferences in every
directory. wc-project accepts a name or unique substring, --root, and --list.

Desktop: config, wall, bright, display, shot. Brightness accepts 0-100 and
up/down with --step; the applied minimum is 5% (at least one hardware unit).
wc-bright --list shows devices; --device chooses one. Screenshots need grim;
--region needs slurp, --copy needs wl-copy. Existing screenshot files are
never overwritten. The compositor must support a capture protocol that grim
implements; Whitecube builds without screencopy cannot use this command.
Default output: ~/Pictures/Screenshots (XDG Pictures
directory if xdg-user-dir is available).

Audio: audio, vol, mic. Auto mode first probes the live PipeWire sink, then
uses ALSA (amixer), then cube-audio if installed. --backend selects explicitly;
--card and --control select ALSA. No failed audio write is retried on another
backend. Levels stay in 0-100. mute toggles, off mutes, on/unmute unmutes.
The microphone uses ALSA capture switches (cap/nocap).

Network: net, wifi, bt. wc-net reads local information by default; --probe
checks ICMP reachability. A failed ping can mean filtering. wc-wifi uses the
installed Whitecube cube-net manager; manage/scan/reconnect use sudo. Radio
on/off needs rfkill. wc-bt requires bluetoothctl and a Bluetooth service.

System: sys, bat, temp, disk, top, services, logs, check, doctor. sys/bat/temp
and doctor support JSON. wc-top --cpu sorts by CPU. wc-logs reads the native
session log by default; --service or --journal reads the journal, --suite
lists application launch logs. wc-doctor identifies missing optional tools
without installing packages or changing services. --strict additionally
checks core tools and PATH. Missing optional tools do not fail the default
doctor report. wc-check preserves the installed Whitecube health report.

Power: lock, suspend, off, reboot, logout, inhibit. Power-off and reboot
perform clean systemctl operations, with confirmation; --yes / -y skips it.
Noninteractive use requires --yes. --dry-run previews power operations.
Logout targets only this user's Whitecube processes. Suspend waits for
swaylock -f to return successfully before invoking suspend. wc-inhibit uses
systemd-inhibit and needs logind, which some Whitecube sessions do not run.

Utilities: notify, timer, clip. Timers accept seconds or s/m/h/d units,
including 1h30m and fractional units, up to 365 days. Timers detach, retain
IDs and status, and can be listed or cancelled. --prune removes completed,
cancelled, failed, and interrupted records while preserving running timers.
Workers poll cancellation
without sending signals to recorded PIDs. Clock changes do not alter running
timers. Timers interrupted by reboot or a killed worker are identified when
listed; they do not automatically resume. Delivery needs a working notify-send
session with a notification daemon on the session bus; failed notification
delivery is recorded. Timer history is retained.
wc-clip paste/copy/clear operates on the Wayland clipboard, including piped
binary input. It does not keep clipboard history.

All suite commands support --help and --version without performing their
normal action. Application options are forwarded as argument lists, not
shell code. EDITOR, VISUAL, and PAGER may contain quoted arguments; they are
split with shlex and never evaluated by a shell. Usage errors return 2,
missing executables 127, operational failures 1, interrupted commands 130.
Commands passing through to a native tool preserve its exit status.

Launch errors are visible and logs are private under
${XDG_STATE_HOME:-~/.local/state}/wc-suite/logs. Launch logs rotate at 1 MiB.
Timers and installation backups also use that state directory. Shared code,
installer, tests, and this reference live in ~/.local/lib/wc-suite.
Python 3.9+ is the only mandatory language runtime; optional programs are
checked at use time. No third-party Python packages are needed.

Shell completion: Bash completions are installed in the standard per-user
bash-completion directory and load on demand in new shells. For the current
shell: source <(wc-completion bash). Zsh: add ~/.local/share/zsh/site-functions
to fpath before compinit, or source <(wc-completion zsh) after compinit.
Fish: wc-completion fish | source, or save its output as a conf.d script.
Completions cover commands, options, choices, and filesystem paths.

Install/reinstall: python3 install.py (from this source directory).
Review destinations: python3 install.py --dry-run.
Every changed destination is backed up before installation. The installer
prints the backup and rollback command. Rollback checks every installed
file before changing any, so later edits are preserved rather than silently
overwritten. Existing wrapper permissions and original symlinks are retained
in backups. A failed install restores the destinations it already changed.

Verify: python3 -m unittest discover -s ~/.local/lib/wc-suite -p test_suite.py -v
Tests use mocks or isolated temporary homes; no actual power action, screen
lock, radio change, notification, or audio change is performed.
