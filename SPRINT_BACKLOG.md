# Whitecube Autonomous Sprint Backlog

## Phase 1: Core Compositor & Desktop Daemons (`src/`, `libexec/`, `bin/`)
- [x] `src/whitecube.c` & `src/tab-panel.h` (Compositor window/output/protocol hardening & extensions)
- [x] `src/cube-status.c` & `src/cube-ca.c` (Status bar & living-cube wallpaper lifecycle/features)
- [x] `src/cube-settings.c` & `libexec/cube-settings-helper` (Settings UI & privileged helper IPC/features)
- [x] `src/cube-audio/` (Audio controls, validation, and mixer enhancements)
- [x] `bin/` utilities (`whitecube-session`, `whitecube-doctor`, `whitecube-preferences`, `whitecube-terminal`, etc.)

## Phase 2: Networking & Boot (`net/`, `boot/`)
- [x] `net/cube-net`, `net-off`, `net-on` (Wi-Fi/DHCP controller, takeover, and firewall/switch resilience)
- [x] `boot/` (GRUB / Plymouth / tty1 black cube theme & recovery polish)

## Phase 3: Workspace & Applications (`workspace/`, `apps/`)
- [x] `workspace/` (`whitecube-web` Python workspace suite)
- [x] `apps/cube-browser` (Native browser features, security, and DOM/rendering extensions)
- [x] `apps/cube-player` (Media playback, playlist parsing, and stream handling)
- [ ] `apps/cube-editor` (Linux ISO studio, project guards, and QEMU workflow)
- [ ] `apps/cube-train` (GTK4 trainer, pipeline, checkpoint benchmarking, and cooling/resume)
- [ ] `apps/cube-strap` (`whitecube-vm` UEFI VM builder and management)
- [ ] `apps/chrootables` (`cube-chrootables` & `whitecube-chrootable-manager`)
- [ ] `apps/cube-prompts` (SQLite prompt library, templates, and multi-model runner)
- [ ] `apps/desktop-chat` (Electron chat, local model server bridge, search/drafts)
- [ ] `apps/wc-suite` & `apps/cube-apps` (CLI suite, completions, and native file utilities)

## Phase 4: Packaging & Release Readiness (`packaging/`, `tools/`)
- [ ] `packaging/` (`install-tree.sh`, `check-tree.py`, `debian/`, ABI & conffile verification)
- [ ] `tools/` (`whitecube-migrate` & developer scripts)