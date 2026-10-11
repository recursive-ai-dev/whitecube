# Whitecube Trainer 0.7.0

Native GTK4 controls and a local small-language-model training pipeline. The
desktop process uses the system Python and never imports PyTorch. Preparation,
training and CPU previews run in the Python environment selected in the app.

## Start from this checkout

From the repository root:

```sh
./bin/cube-train
./bin/whitecube-train --help
```

Select a Python interpreter with PyTorch and NumPy, then use **Check runtime**.
Parquet input also needs PyArrow. Choose CPU or CUDA, a corpus, a model size and
total optimizer steps. **Prepare corpus only** validates and prepares data before
training. The run stays available when the window closes.

TXT and Markdown files are whole source documents for split assignment; JSONL
and Parquet rows are documents. Supply at least two distinct documents, with
enough tokens in each split for the model context. Matching document hashes stay
in the same split. The cap counts UTF-8 bytes and EOS tokens, not words.

## Desktop workflow

- Search and filter runs, reuse a run's settings, or open its directory.
- Resume to a **total** step target. The UI shows the additional steps and reuses
  the saved architecture, corpus, batch size and gradient accumulation.
- Preview either Best or Latest on CPU and copy the output.
- Inspect training/validation losses, throughput, processed tokens, gradient
  norm, estimated remaining time, thermal readings and preparation phases.
- Search the displayed log, follow new output, or export its visible tail.
- Export plotted metrics to CSV, or export a JSON report with configuration,
  corpus provenance, progress, failure details and telemetry. Log and metrics
  exports cover the displayed/recent data; the complete history remains in the
  run directory.

`Ctrl+1`, `Ctrl+2`, `Ctrl+3` change pages; `Ctrl+N` starts a new draft;
`Ctrl+F` focuses run/log search; `F5` refreshes. `Ctrl+Enter` starts on Train or
resumes on Runs. It does not launch from Activity or an open dialog.

## Data, checkpoints and jobs

Runs default to `~/train-out`; `WHITECUBE_TRAIN_OUT` selects another location.
Preferences use `XDG_CONFIG_HOME/whitecube/trainer.json` and desktop jobs use
`XDG_STATE_HOME/whitecube/trainer`, with the standard home-directory fallbacks.
The desktop and CLI retain the shared `~/.local/state/tensegrity/train.lock` so
existing CLI jobs remain protected.

New corpus manifests include source size/mtime, document-group and skipped-row
counts, truncation counts, the actual validation fraction, token format, artifact
sizes and SHA256 checksums. Preparation stages and syncs the corpus before
publication. Training verifies recorded checksums and token IDs before model
allocation. Existing corpora without integrity records remain usable.

Checkpoints preserve model/optimizer/scaler state, Python/NumPy/PyTorch RNGs,
batching settings and the learning-rate schedule. Resume keeps the saved schedule
unless a new `--schedule-steps` is explicitly provided. Writes use private unique
temporary files and atomic replacement. A failure before replacement retains
the previous checkpoint.

Stop requests allow the current optimizer step and checkpoint save to finish.
The worker tracks the training process group and escalates an unresponsive stop
after 60 seconds; the CUDA watchdog has its own shorter grace period. Forced
stops are recorded explicitly, and recovery uses the last successfully saved
checkpoint. Moving a run or checkpoint to Trash remains recoverable.

CPU preview controls are also available directly:

```sh
"$TRAIN_PYTHON" apps/cube-train/pipeline/preview.py \
  --checkpoint /path/to/run/ckpt_best.pt --prompt 'Once upon a time' \
  --seed 1337 --max-new-tokens 120 --temperature 0.85 --top-k 40
```

Set `TRAIN_PYTHON` to the same interpreter selected in the app. Preview generation
is repeatable for the same checkpoint, prompt, controls and runtime.

## Verification

Run from the repository root. The system interpreter provides GTK, while the
training interpreter provides NumPy, PyTorch and PyArrow:

```sh
/usr/bin/python3 -m unittest discover -s apps/cube-train/tests -p 'test_engine*.py' -v
/usr/bin/python3 -m unittest discover -s apps/cube-train/tests -p test_app.py -v
"$TRAIN_PYTHON" -m unittest discover -s apps/cube-train/tests -v
/usr/bin/python3 -I tests/test-cube-train-ui.py --screenshots build/trainer-checks
mkdir -p build/trainer-test-temp
TMPDIR="$PWD/build/trainer-test-temp" /usr/bin/python3 -I tests/test-packaging.py
```

GTK tests may skip under the training interpreter; the separate system-Python
command exercises them. The native UI smoke test needs `build/whitecube` and
runs an isolated headless Wayland compositor. Packaging needs the repository's
built runtime artifacts and an executable temporary filesystem; `/tmp` may be
mounted `noexec`.

Tests use temporary fixtures and short CPU runs. They cover whole-document split
isolation, corpus corruption, deterministic interrupted/resumed training, real
2M/5M/10M architectures, worker shutdown, cooling, previews, exports and native
UI interactions. CUDA precision/watchdog edge cases use controlled fixtures;
GPU hardware training is not part of this validation.

See [the numbered improvement inventory](IMPROVEMENTS.md) for the implementation
scope and verification results.
