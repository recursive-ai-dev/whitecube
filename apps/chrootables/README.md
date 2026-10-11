# Chrootables

Native Whitecube front end for `chrootable-manager` (Debian workspaces in ext4
images: profiles, persistent or disposable sessions, snapshots, clones, exports
and imports).

- `app.py`: the GTK4 / libadwaita window. It runs as the desktop user.
- `backend.py`: runs the manager. Reads go straight to it when the store is
  readable; everything else goes to the root helper. Interactive shells open in
  `whitecube-terminal` (foot), which asks sudo for itself.
- `broker.py`: the root helper. Unlock starts it once with `sudo -S -k` (the
  password goes to sudo and is never kept) and the window sends it JSON-line
  requests. It runs only chrootable-manager, with argument lists, and exits when
  the window closes, on Lock, or after the idle time set in Preferences. This is
  what makes one password enough on this desktop, where sudoers sets
  `timestamp_timeout=0` and sudo remembers nothing between commands.
- `chrootable-manager`: the manager (installed as `/usr/bin/chrootable-manager`,
  with its own whiptail menu and command line); `MANAGER.md` is its manual.
- `tests/integration.sh`: the manager's opt-in root test (downloads Debian).

Window features: queued operations with a live console and Cancel, a Run
command page (`--exec`, streamed output, history), safety snapshots before
repair/resize/mode changes, snapshot pruning, list sort and lifecycle filters,
free-space checks, parsed health checks, activity filters with logs, saved
preferences and window state (`~/.config/chrootables/settings.json`), F1 for
shortcuts, and the desktop's accessibility settings.

`tests/test-chrootables.py` in the repository root covers the backend, the
manager's JSON contract and a headless render of every page.
