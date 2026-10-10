# Chrootables

Native Whitecube front end for `chrootable-manager` (Debian workspaces in ext4
images: profiles, persistent or disposable sessions, snapshots, clones, exports
and imports).

- `app.py`: the GTK4 / libadwaita window. It runs as the desktop user.
- `backend.py`: runs the manager. Reads go straight to it when the store is
  readable; everything else goes through sudo. A password typed into the window
  is handed to `sudo -S -v` once and never kept; sudo's own timestamp covers the
  `sudo -n` calls that follow. Interactive shells open in `whitecube-terminal`
  (foot), which asks sudo for itself.
- `chrootable-manager`: the manager (installed as `/usr/bin/chrootable-manager`,
  with its own whiptail menu and command line); `MANAGER.md` is its manual.
- `tests/integration.sh`: the manager's opt-in root test (downloads Debian).

`tests/test-chrootables.py` in the repository root covers the backend, the
manager's JSON contract and a headless render of every page.
