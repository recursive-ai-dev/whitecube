# Cube Prompts Studio

A native GTK4 prompt library and model comparison workbench. Version 2.0 adds
persistent experiments, variable presets, cancellable comparisons, revision
diffs, and a standalone Debian package. Library editing works offline; model
requests go directly to the provider you select.

## Install the Debian package

On Debian 13+, Ubuntu 24.04+, or compatible systems:

```sh
sudo apt install ./dist/cube-prompts_2.0.0_all.deb
cube-prompts-studio
```

Find **Cube Prompts Studio** in the application menu. Apt installs GTK 4.10+,
libadwaita 1.5+, and the system Python bindings. No pip environment, Electron,
Whitecube session, or account is needed to use the library. Provider accounts
are needed where the selected service requires them.

The standalone package uses `/usr/lib/cube-prompts` and
`/usr/bin/cube-prompts-studio`, so it can coexist with Whitecube Desktop's
`cube-prompts` launcher. Both use your existing library and settings. Removing
the package leaves your data intact.

## Build from source

```sh
./packaging/build-deb.sh
```

The script runs package checks and creates `dist/cube-prompts_2.0.0_all.deb`
and its SHA-256 file without installing anything or needing root. Verify with:

```sh
cd dist
sha256sum -c cube-prompts_2.0.0_all.deb.sha256
```

See [packaging/README.md](packaging/README.md) for dependencies, reproducible
builds, and alternate output paths. To run the checkout with GTK installed:

```sh
/usr/bin/python3 -I app.py
```

## Workflows

| Shortcut | Page | Features |
|---|---|---|
| Ctrl 1 | Library | Full-text search; folder, tag, and favorite filters; four sort orders; resizable library pane; template editor and live preview. |
| Ctrl 2 | Run | Compare up to three models concurrently. Answers appear as each model finishes. Set temperature/output limits, cancel requests, copy answers, and compare time, tokens, and estimated cost. |
| Ctrl 3 | Templates | Save reusable starting points and create prompts from them. |
| Ctrl 4 | History | Saved comparisons with exact inputs, variables, output limits, answers, and failures. Export JSON/Markdown reports or create a new prompt from an old experiment. Compare and restore prompt revisions. |
| Ctrl 5 | Export | Six single-prompt export formats, complete JSON backup/restore, CSV import/export, and Markdown ZIP. |
| Ctrl 6 | Settings | Provider credentials, custom OpenAI-compatible/Ollama endpoints, and custom model IDs. |

Other shortcuts: Ctrl N new, Ctrl S save with a revision, Ctrl F or Ctrl K
search, Ctrl R run, Ctrl D duplicate, Ctrl Shift C copy the filled-in prompt.
Switching prompts and closing saves edits; if saving fails, the draft stays
open. Closing an active comparison cancels it and saves its snapshot first.

Template syntax: `{{name}}`, `{{name:default}}`, `{{name|multiline}}`,
`{{name|options:a,b,c}}`. Defaults apply to every use of the same variable,
including system instructions. Named presets retain variable values for a
prompt across sessions. Changing a variable's options or input type refreshes
its form. Fix syntax errors and fill required variables before running.

Supported providers are OpenAI, Anthropic, Google Gemini, OpenRouter,
Pollinations, and local Ollama. Add a custom `provider:model-id` in Settings to
use a model outside the bundled catalog, including tagged Ollama names such
as `ollama:qwen3:8b`. A custom OpenAI-compatible endpoint can be used without a
key when the server permits it. Catalog availability and pricing can change;
costs are estimates, and unknown custom-model costs are shown as unavailable.

Cancellation returns promptly and preserves completed answers. Requests
already sent may still finish and incur usage at the provider; late answers
are discarded. No live provider calls are made by the test suite.

## Data and backups

- Library, revisions, presets, comparisons: `$XDG_DATA_HOME/cube-prompts/prompts.db`
  (default `~/.local/share/cube-prompts/prompts.db`, SQLite with WAL and FTS5).
- Settings: `$XDG_CONFIG_HOME/cube-prompts/settings.json`
  (default `~/.config/cube-prompts/settings.json`).
- Credentials: OS keyring when available; otherwise
  `~/.config/cube-prompts/credentials.json`, mode 0600 in a private directory.
  Keys are sent only to the selected provider, including your configured
  endpoint for that provider.

JSON backups include prompts, folders, tags, templates, revisions, presets,
and comparison snapshots. Older backups remain supported. Imports merge
matching records, keep a revision before replacing prompt content, and roll
back the entire operation if validation or a write fails. Library exports use
private temporary files and atomic replacement. Credentials are excluded from
backups. CSV/Markdown exports do not contain the full experiment history.

Comparison history and exports contain the prompts and answers you tested;
delete individual comparisons in History when you no longer need them.

## Tests

Core and package tests:

```sh
uv run --no-project --with pytest --with keyring python -m pytest -q tests
```

Native GTK workflow checks use temporary data and fake model clients. Run with
the system Python on an isolated X server (install `xvfb` for `xvfb-run`):

```sh
xvfb-run -a /usr/bin/python3 -I tests/gui_smoke.py
```

Use `--screenshots /tmp/cube-prompts-previews` to capture every page. The same
checks can exercise an extracted package with `--app-dir PATH/usr/lib/cube-prompts`.
They cover editing, live forms, presets, revision diffs, failed saves, custom
models, progress, run snapshots, cancellation, and closing during a run.

Cube Prompts derives from the MIT-licensed Prompt Manager Studio core and
Whitecube's native GTK interface. See [NOTICE](NOTICE) and
[packaging/copyright](packaging/copyright).
