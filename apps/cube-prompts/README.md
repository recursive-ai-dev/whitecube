# Cube Prompts

Cube Prompts is Whitecube's prompt library and test bench. It is a native GTK4
window around the core of Prompt Manager Studio (`~/Projects/prompt-manager`,
commit 7882c40), copied here as `prompt_manager/`. The PyQt6 interface, its
themes and the offline licence gating were left behind; everything is enabled.

## Run and install

```sh
./bin/cube-prompts                  # from the repository
python3 -I tests/test-cube-prompts.py   # unit checks + headless GUI smoke run
```

The desktop package installs the window to `/usr/share/whitecube/cube-prompts`
and the launcher to `/usr/bin/cube-prompts`. It appears in the Apps menu as
**Cube Prompts**.

## Pages

| Ctrl | Page | What it does |
|---|---|---|
| 1 | Library | Search, filter by favorites/folder/tag, edit title, folder, target model, tags, system instruction and template. Variables become a form; the preview fills them in live with character, word and token counts. |
| 2 | Run | Sends the filled-in prompt to up to three models at once and compares answer, time, tokens and estimated cost. |
| 3 | Templates | Reusable starting points; *Save as template* in the Library adds one. |
| 4 | History | Every Ctrl S keeps the version it replaces; restore one into the editor. |
| 5 | Export | Plain text, OpenAI / Anthropic request JSON, Markdown, LangChain, LlamaIndex; whole-library JSON/CSV backup and import, Markdown ZIP. |
| 6 | Settings | Provider keys and endpoints (OpenAI-compatible base URL, Ollama URL). |

Other keys: Ctrl N new, Ctrl S save with revision, Ctrl F search, Ctrl R run,
Ctrl D duplicate, Ctrl Shift C copy the filled-in prompt. Switching prompts or
closing the window saves edits without a revision.

Template syntax: `{{name}}`, `{{name:default}}`, `{{name|multiline}}`,
`{{name|options:a,b,c}}`. A default given once applies to every use of the name.

## Data

- Library: `~/.local/share/cube-prompts/prompts.db` (SQLite, FTS5 search). A new
  library starts with three example prompts.
- Settings: `~/.config/cube-prompts/settings.json`.
- Keys: the OS keyring when one runs (python3-keyring + Secret Service).
  Whitecube normally has none, so keys go to
  `~/.config/cube-prompts/credentials.json`, mode 0600 in a 0700 directory, the
  same protection cube-chat gives its key. Keys are sent only to their own
  provider.

Claude models: Opus 5.5, Sonnet 5.5 and Haiku 5.5. These models reject sampling
parameters, so requests and Anthropic exports leave out temperature.

## Tests

`tests/` holds the core's original pytest suite (125 tests). It needs pytest,
which the system lacks:

```sh
cd apps/cube-prompts && TMPDIR=~/.cache/wc-tmp uv run --no-project --with pytest --with keyring python -m pytest tests
```

`tests/test-cube-prompts.py` in the repository root is the unittest + GUI check
BUILD.sh runs.
