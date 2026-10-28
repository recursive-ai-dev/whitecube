# Desktop Chat

A Linux desktop AI workspace for OpenAI-compatible APIs and local Whitecube training checkpoints. Version 1.0 adds a searchable conversation library, persistent transcripts and drafts, Markdown exports, generation controls, and a redesigned keyboard-accessible interface.

## Install the Debian package

On an amd64 Debian or Ubuntu desktop, from the build output directory:

```sh
sudo apt install ./whitecube-desktop-chat_1.0.0_amd64.deb
whitecube-chat
```

The application appears as **Desktop Chat (Standalone)** in the application menu. Electron is bundled: the installed app needs neither Node.js nor npm. Package dependencies are resolved by apt. The separate `whitecube-chat` command and package name allow this build to coexist with the original Whitecube desktop package and its `desktop-chat` launcher. Quit one before using the other; both intentionally share the existing `desktop-chat` settings and history.

```sh
whitecube-chat --settings
whitecube-chat --toggle
whitecube-chat --hide
whitecube-chat --show
whitecube-chat --quit
whitecube-chat --help
whitecube-chat --version
```

The launcher detects X11 or Wayland. Set `DESKTOP_CHAT_OZONE_PLATFORM=x11` or `wayland` to override it. Under Wayland, compositor restrictions may prevent the global shortcut; bind `whitecube-chat --toggle` in your desktop's keyboard settings. The default shortcut is Ctrl+Shift+T. Closing the panel hides it; use the tray's Quit action or `whitecube-chat --quit` to exit.

## Connect a model

Open Settings and enter the base URL, exact model ID, and API key for an OpenAI-compatible **Chat Completions** service. Use Test before saving. A local service such as Ollama or LM Studio normally uses a URL like `http://localhost:11434/v1` and needs no key. Non-loopback services require a key. The application does not include a cloud subscription or model weights.

Settings let you control temperature, maximum reply tokens, filesystem tools, destructive-operation confirmations, and the global shortcut. File tools can read anywhere your account can read. Writes, edits, and moves ask for approval with a preview by default; disable tools entirely when they are unnecessary. Providers receive the conversation and any tool results used in that conversation.

Local checkpoints are discovered under `~/train-out` (override with `WHITECUBE_TRAIN_OUT`). Pick a checkpoint in **Talk to** or launch with `--checkpoint RUN/ckpt_best.pt`. This optional feature requires the Whitecube Trainer/Tensegrity Python environment; PyTorch and training code are not bundled in the chat package. Set `WHITECUBE_MODEL_PYTHON` to an absolute Python interpreter and `WHITECUBE_TENSEGRITY_DIR` to a directory containing `tensegrity_serve.py` if automatic discovery cannot find them. Checkpoint generation runs on CPU with a 120-token reply limit and no file tools.

## Conversations and privacy

Chats are saved automatically, including partial replies when stopped. Search covers conversation titles and message text. Rename or delete through the conversation header; deletion requires confirmation. Export saves a Markdown file through the native file picker. The preload API also supports JSON export. Switching models starts a new conversation and preserves the old one. When reopening an API chat, its model is restored only for the same configured endpoint. If the endpoint has changed, start a new conversation to avoid sending an old transcript to a different service.

Settings and history live in Electron's `desktop-chat` user-data directory (normally `~/.config/desktop-chat`, or under `XDG_CONFIG_HOME`). History files have private permissions and are written atomically; they are not encrypted. API keys are stored separately in settings using the OS keyring when available. Settings disclose when a private plaintext fallback is used. Drafts are kept locally per conversation. Uninstalling the package leaves your user data intact.

Useful shortcuts:

| Shortcut | Action |
| --- | --- |
| Enter / Shift+Enter | Send / new line |
| Ctrl+N | New conversation |
| Ctrl+B | Toggle history sidebar |
| Ctrl+F | Find in current conversation |
| Ctrl+L | Focus composer |
| Ctrl+, | Open Settings |
| Ctrl+Shift+C | Copy conversation as Markdown |
| Alt+Up | Reuse the last user message in an empty composer |
| Escape | Close a dialog/search, otherwise hide panel |

## Develop and build

Requirements: Linux, Node.js 22.12 or newer, npm, and `dpkg-deb`/`dpkg` for Debian packaging.

```sh
npm ci
node node_modules/electron/install.js
npm test
npm start
npm run deb
npm run verify:deb -- dist/whitecube-desktop-chat_1.0.0_amd64.deb
```

The build runs the regression suite, stages only runtime files, bundles pinned Electron 45.1.0 and its licenses, and verifies package contents, checksums, launcher behavior and sandbox permissions. Output is in `dist/`, with a `.sha256` file. `SOURCE_DATE_EPOCH` can fix the build timestamp. The runtime's ELF architecture determines amd64 or arm64 output; use `ELECTRON_DIST=/path/to/exact/pinned/distribution` to supply another architecture. The builder creates the package without root and does not install it.

An optional real Electron integration check uses Playwright and an X11 display (Xvfb works). It launches with a disposable profile and a loopback mock API, makes no paid requests, and does not touch existing settings:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright DISPLAY=:99 node test/gui-smoke.cjs
```

To check an extracted package, also set `CHAT_APP_PATH` to its `usr/lib/whitecube-desktop-chat/app` directory and `CHAT_ELECTRON_PATH` to `usr/lib/whitecube-desktop-chat/runtime/electron`. The check covers settings, streaming, Markdown/code copy, saved chats, search, export, denied tools, stop/restart, drafts, compact layout and deletion.

Licensed under Apache-2.0. Bundled Electron and Chromium retain their own licenses.
