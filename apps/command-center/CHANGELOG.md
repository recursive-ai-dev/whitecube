# Changelog

## 2.0.1

- Renamed the product, desktop identity, launchers, icons, and Debian package to Whitecube Command Center.
- Relocated the source repository to `~/whitecube/command-center`, preserving Git history and existing changes.
- Kept the existing application profile, storage keys, backup format, and legacy command aliases.
- Added a 48-item improvement backlog with priorities and acceptance criteria.
- Added a checked user installation using a matching system sandbox helper when user namespaces are unavailable.

## 2.0.0

- Added storage capacity and inode monitoring with a hierarchical block-device map.
- Added searchable installed-package inventory, size sorting, and pagination.
- Added read-only diagnostics and report export; unavailable checks are marked unknown.
- Added persistent command activity with exit codes, duration, search, filtering, and confirmed replay.
- Added validated library backups, import preview, and collision-safe merging of commands and shortcuts.
- Added startup and online-feature preferences, window-size persistence, journal search/export, and a visible command palette button.
- Hardened IPC sender checks, navigation, permissions, command concurrency, UTF-8 output, and output limits.
- Fixed polling failures, stale service/journal filters, shortcut confirmation bypass, and kernel recovery backups before partial writes.
- Added accessible confirmation dialogs, crash recovery, visible keyboard focus, and reduced-motion support.
- Updated Electron to 44.7.0 and Tailwind to 4.3.3; dependency audit is clean.
- Added a self-contained Debian package, launchers, desktop entry, icons, and SHA-256 checksum.
