# Whitecube Trainer 1.0.0

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
and Parquet rows are documents. TXT/Markdown streaming preserves whitespace and
writes one EOS per file. Supply at least two distinct retained documents, with
enough tokens in each split for the model context. Matching document hashes stay
in the same split. The cap counts UTF-8 bytes and EOS tokens, not words.

Expand **Corpus selection and filtering** to remove exact duplicates, shuffle
source files using the seed, skip short documents, cap each document's length,
or change the Parquet batch size. These controls are optional and saved with the
run. Deduplication compares normalized source text and retained text across
formats, before counting accepted tokens against the cap. Length limits count
UTF-8 bytes, exclude EOS, and preserve complete characters. Long documents keep
a prefix; a document limit lets preparation continue to later rows. Minimum
length applies to the retained text. Shuffling changes file selection under the
cap; rows within each file still follow source order.

```sh
whitecube-train ~/train-in --5m --prepare-only --device cpu \
  --deduplicate --shuffle-files --min-document-bytes 200 \
  --max-document-bytes 32768 --batch-rows 16
```

Preparation stores document metadata, duplicate lookups and split sorting in a
temporary SQLite index with a bounded cache. A checksummed `documents.jsonl`
records each retained document's source path and row (physical JSONL line,
Parquet row, or 1 for a text file), split, token offset/count, retained/source
SHA256 hashes, split group identifier and truncation flag. Matching source hashes
and identical retained prefixes stay together, including when the corpus cap
shortens only the final copy of a repeated document. The manifest includes duplicate,
short-document and document/corpus-limit truncation counts. Many tiny documents
need extra disk space for their index and provenance.

To train a fresh model on identical data, select **Reuse prepared corpus** on
Runs, or choose an earlier run's `data` directory as the corpus. The CLI accepts
the same directory as its dataset argument:

```sh
whitecube-train ~/train-out/previous-run/data --10m --device cpu --steps 4000
```

Reuse copies and verifies the prepared artifacts into the new run without
reading the original sources. It preserves the saved selection, token cap and
split seed/share; corpus filtering settings in the new draft do not change it.
Copies are independent files, so deleting or modifying the old run cannot change
the new corpus. Existing corpora/checkpoints remain usable; new corpus manifests
use schema version 3 and include the document index checksum.

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

## Notebook export

Exports select an installed Jupyter kernel whose Python matches the training
environment, including existing kernels with custom names. A stale kernel for
another environment is ignored. The setup cell prints its Python path and gives
a specific kernel registration command when PyTorch or NumPy cannot import.
Keep the companion folders with the notebook when moving a bundle; the setup
cell finds parent folders and accepts an explicit `BUNDLE_DIR` override. The
optional safetensors section skips cleanly if that package is absent.

**Export notebook…** (Runs page, the run's right-click menu, any row of the
checkpoint list, or `Ctrl+Shift+E`) writes a runnable Jupyter notebook for Best,
Latest or a step snapshot. The default is a self-contained folder:

```
<name>.ipynb   <name>.py (# %% cells)   README.md
whitecube_pipeline/   model code the notebook imports
checkpoint/           a copy of the chosen checkpoint
run/                  run.json, result.json, metrics, previews, model config
data/                 prepared corpus (optional)
```

The cells load the checkpoint, report loss in nats and bits per byte, plot the
curves, generate text with the preview settings, show next-byte probabilities,
re-measure validation loss, continue training in `continued/` (switch on
`CONTINUE_TRAINING`; the original run is never modified) and export
safetensors with a round-trip check. Without the corpus, evaluation and
continued training read the original run's `data` folder. Unticking
*Self-contained* writes only the notebook, pointing at the run in place.

*Open in Jupyter afterwards* uses JupyterLab, Jupyter Notebook or VS Code, and
registers the training Python once as the `whitecube-train` kernel
(`python -m ipykernel install --user`). The same export is available as:

```sh
whitecube-train --export-notebook ~/train-out/<run> --output ~/Documents/my-notebook \
  [--checkpoint best|latest|step00001234] [--with-corpus] [--reference] [--no-script]
```

## Training plan and run controls

**Training precision** selects automatic, float32, bfloat16, or CUDA float16.
The actual compute precision is saved in checkpoints, progress and results;
resume preserves it and rejects conflicts. Resuming a float16 checkpoint requires
CUDA. Automatic selection on older checkpoints retains the original behavior.
The Train page exposes **Validation batches per evaluation** (20 by default,
eight sequences per batch) and **Record progress every** (10 steps by default).
Validation uses a fixed seed so checkpoint losses remain comparable. Its batch
budget and interval belong to the saved plan; logging frequency can change on
resume.

**Minimum loss improvement for early stopping** sets the absolute decrease
needed to reset patience. Small improvements still save Best. Evaluation counts,
patience and the reference loss survive resume; changing the minimum improvement
threshold resets that history. Existing checkpoints without counters start with
a fresh history.

```sh
whitecube-train ~/train-in --5m --device cuda --dtype bfloat16 \
  --eval-iters 40 --eval-interval 100 --log-interval 5 \
  --early-stop-patience 8 --early-stop-min-delta 0.005
```

New runs can set the peak learning rate, final-rate fraction, warmup, weight
decay, gradient clipping, evaluation interval, seed, validation share and a
cosine or warmup-stable-decay (WSD) schedule. These are saved in `run.json`.
Resume restores the optimizer and schedule from the checkpoint and rejects
conflicting plan flags. Plan presets fill the step count; the estimate shows the
token budget, approximate corpus passes and, after earlier runs with the same
size and device, an expected duration.

Run controls may change on resume: early stopping after N evaluations without a
new best validation loss, a wall-clock time limit, step-numbered checkpoint
snapshots (`ckpt_step00001234.pt`, oldest pruned) and a thermal pause that waits
5 °C below a stop limit until temperatures fall 10 °C below it. A pause longer
than 15 minutes saves and stops. `result.json` records the stop reason, final
losses, elapsed and cumulative training time and peak VRAM.

```sh
whitecube-train --5m --steps 4000 --learning-rate 5e-4 --schedule wsd \
  --early-stop-patience 6 --max-minutes 90 --keep-snapshots 3 --thermal-pause
whitecube-train --list-runs     # * pinned, B/L saved checkpoints
whitecube-train --status        # the running desktop job
```

## Checkpoint benchmarks

The Runs page's **Benchmark** tab compares Best and Latest, or evaluates any
saved snapshot. Set the validation batch count, sequences per batch and seed,
then enter up to eight prompts. Every selected checkpoint receives the same
sampled held-out windows and greedy generation settings on CPU. Training and
preview controls wait while a desktop benchmark runs.

Reports are saved in the run's `benchmarks/` folder, shown newest first, and
included in the run report. Export the most recent 20 as JSON or a comparison
CSV. They record measured loss, perplexity, throughput, sample text, actual step
count, runtime, checkpoint and corpus SHA256 hashes, and a hash of the sampled
batches. Compare results using the same corpus and evaluation settings. Sampled
windows may overlap; the token count is the measured budget, not unique coverage.
Benchmark bits per byte excludes special token targets; the existing training
chart conversion includes those targets and can differ slightly.

```sh
whitecube-train --benchmark ~/train-out/<run> --checkpoint best \
  --output ~/Documents/best-benchmark.json --eval-iters 20 \
  --batch-size 4 --seed 2026 --prompt 'Once upon a time'
```

Benchmarking checks prepared corpus integrity and rejects input changes during
evaluation. It writes only a new report and refuses to replace an existing file.

## Runs page

Runs sit in a list beside the selected run, so one click shows its details:
stat tiles (parameters, steps, best validation, bits per byte, training time,
disk use), the main actions and Overview / Preview / Benchmark / Export tabs. Rows update
in place while a job trains and carry a phase chip, Best/Latest/snapshot chips
and a loss sparkline. Double-click or Enter opens the run's progress;
right-click, Menu or Shift+F10 opens every run action; F2 renames; Delete moves
to Trash. The checkpoint list previews, exports or trashes any step snapshot.
**Clean up failed runs…** moves failed runs that saved no data or weights to
Trash (pinned and running runs are never included).

The Train page lists Recent corpora, accepts a dropped file or folder, and after
**Inspect** reads the JSONL/Parquet text fields with the training Python. The
Text field picker shows what automatic selection will read per file and blocks a
start when some files have no usable field. **Check runtime** now performs one
CPU optimizer step, so a damaged environment is caught before a run, with a pip
repair command for a missing module. The Progress page shows live stat tiles and
can overlay another run's validation curve.

## Runs, previews and export

Sort runs by date, name, validation loss or size; pin runs and filter to pins;
keep notes with a run. Details include the plan, timing, stop reason, snapshots
and disk use. **Copy resume command** produces the equivalent CLI command.
Previews accept length, temperature, top-k, top-p and seed, can compare Best and
Latest, and can be saved with the run; reports include notes and saved
previews. **Export weights…** writes `model.safetensors`, `model.config.json` and
a `README.md` model card to a new folder.

The loss graph can smooth training loss, overlay the learning rate, mark the best
validation point and read values under the pointer. Log errors, warnings and
improvements are highlighted, and the log can show only matching lines.

`Ctrl+1`, `Ctrl+2`, `Ctrl+3` change pages; `Ctrl+N` starts a new draft;
`Ctrl+F` focuses run/log search; `Ctrl+Shift+E` exports a notebook; `F2` renames; `F5` refreshes. `Ctrl+Enter` starts on Train or
resumes on Runs. It does not launch from Activity or an open dialog. `Ctrl+P`
previews, `Ctrl+E` exports the report and `F1` lists all shortcuts.

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
