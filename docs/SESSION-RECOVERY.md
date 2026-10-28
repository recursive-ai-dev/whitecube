# Starting and recovering a Whitecube session

After installing the Debian package, choose **Whitecube** in the login manager or
run `exec whitecube-session` from a text console. Run it as your normal account.

Before starting, `whitecube-session --check` reports the compositor path, runtime
directory, log directory, audio setting and recent startup failures as JSON. It
creates private runtime/state directories when needed and starts no applications.

The launcher requires an absolute `XDG_RUNTIME_DIR` owned by your account with
permissions `0700`; it rejects symlinks and shared directories. A normal login
manager supplies this directory. On machines without logind, the launcher tries
`/run/user/<uid>` and creates a private fallback under `${TMPDIR:-/tmp}` when
needed. It never changes permissions on a preexisting shared or foreign directory.
If you explicitly provide an invalid `XDG_RUNTIME_DIR`, correct that environment
setting before starting. Runtime ownership or permission errors appear directly
in the terminal or login manager's session output.

Session output goes to `${XDG_STATE_HOME:-~/.local/state}/whitecube/session.log`,
with the previous session retained as `session.log.old`. If the state directory
cannot be used privately, logs go under `$XDG_RUNTIME_DIR/whitecube-state`.
Logs and startup records have permissions `0600`. A session lock prevents an
accidental second launch from replacing the running session's logs or starting
another panel and audio stack.

After three failures within 60 seconds, the launcher prints the last log lines
and stops the restart loop. A text console offers an interactive recovery shell;
a login manager receives an error instead of an unusable shell. Intentional
termination signals and failures after a minute of running do not count as
startup failures. Immediate retries remain blocked until the recorded failures
are at least 60 seconds old, a session succeeds in safe mode, or you explicitly
reset the guard.

Use `whitecube-session --safe-mode` to bypass the restart guard and launch only
the compositor, without the panel, wallpaper, first-boot report or PipeWire
services. Existing settings still apply. Use **Alt+Enter** to open a terminal,
then run `whitecube-doctor` or inspect the session log. A successful session clears
the failure counter. `whitecube-session --reset-crash-guard` clears the counter
without launching the desktop.

Audio startup reads `/etc/whitecube/config` followed by
`${XDG_CONFIG_HOME:-~/.config}/whitecube/config`. With `audio=pipewire`, only
available PipeWire, WirePlumber and PulseAudio compatibility commands are started.
The compositor owns the startup process group and stops those session services
when it exits. Termination signals sent to the launcher are forwarded to the
compositor so its normal client cleanup can run.

For development inside another Wayland desktop, run
`WLR_BACKENDS=wayland whitecube-session --safe-mode`. This explicit backend keeps
the parent `WAYLAND_DISPLAY` available to wlroots; normal starts clear inherited
display sockets. The private wlroots build includes its nested Wayland and
headless backends. The session lock still prevents a second ordinary launch
using the same state directory.
