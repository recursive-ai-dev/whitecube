# Changelog

## 2.1.0

- Redesigned the overview and navigation with resource meters, health and update checks, pinned actions, recent activity, and compact layouts.
- Added CPU, memory and network trends with 2/5/10-minute windows, history retained across navigation, and explicit missing-sample gaps.
- Added persistent monitoring cadence and pause/refresh controls, including process-table pause and refresh.
- Retained cached data on inspection failure, exposed timestamps and errors, and repaired initially hidden one-shot loading.
- Added command pins, tags, preview text, filtering, sorting and backup compatibility.
- Fixed palette execution callbacks retaining obsolete commands or working directories; added accessible search and scrolling keyboard selection.
- Unified modal focus trapping, background isolation, Escape dismissal and focus restoration; added native working-directory selection.
- Added terminal focus shortcut, elapsed time, stopping feedback, automatic expansion on execution and command-draft recall.
- Added palette access to shortcut groups and migrated the default Core Actions binding to Ctrl+Shift+G to avoid Whitecube’s native Alt+C action.
- Rejected duplicate, desktop-reserved and unsupported shortcut bindings; imported conflicts remain unbound for editing.
- Made activity and window-state persistence atomic with file and directory flushes.
- Expanded regression tests and isolated Wayland UI checks; made the test harness work on noexec temporary filesystems.

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
