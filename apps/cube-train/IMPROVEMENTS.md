# Whitecube Trainer 0.9.0: notebook export and 16 upgrades

Tests and documentation are not counted.

## Notebook workflow — pipeline/export_notebook.py, engine.py, app.py, bin/whitecube-train

1. Export any checkpoint (Best, Latest or a step snapshot) as a Jupyter notebook (nbformat 4.5).
   Eight sections: load, curves, generation, next-byte probabilities, validation loss,
   continued training in a copy, safetensors export with a round-trip check.
2. Self-contained bundle: model code (import closure), checkpoint copy, run records and an
   optional prepared corpus; or a reference notebook that points at the run in place.
   Staged in a private folder and published atomically; cancellation and failures leave nothing.
3. The same cells as a `# %%` percent-format script for VS Code, Spyder and Jupytext.
4. Open in Jupyter (JupyterLab, Notebook or VS Code), registering the training Python once as
   the `whitecube-train` kernel; **Copy load snippet**; `whitecube-train --export-notebook`.

## Runs page — app.py

5. Master–detail layout: the run list sits beside the selected run's details, tiles and tabs
   (Overview, Preview, Export) instead of one long page below the list.
6. Rows are built once and updated in place. Previously the whole list was rebuilt every second
   while training, and rows held selectable labels that swallowed clicks; the selected row was
   also not visibly highlighted under the boxed-list style.
7. Run actions menu on right-click, long-press, Menu and Shift+F10; double-click or Enter opens
   Progress; F2 renames; Delete moves to Trash; `Ctrl+Shift+E` exports a notebook.
8. Row chips (phase, Best, Latest, snapshots) and a loss sparkline per run.
9. Checkpoint list with step, validation loss, size and time for Best, Latest and every step
   snapshot; preview, notebook, weights export or Trash for each, including snapshots.
10. **Clean up failed runs…** for failed or interrupted runs that saved no data or weights.

## Progress and metrics — app.py, engine.py

11. Bits per byte (and perplexity in notebooks) beside nats/byte loss in tiles, readouts and exports.
12. Compare another run's validation curve on the loss chart, with a pointer readout.
13. Live stat tiles: step, training loss, best validation, bits/byte, tokens/s, remaining, GPU, CPU.

## Train page and runtime — app.py, engine.py

14. Text field probe and picker for JSONL/Parquet after Inspect (run in the training Python):
    shows what automatic selection reads per file, lists fields, and blocks a start when some
    files would fail. This morning's 11 failed runs were text-field overrides missing from files.
15. Recent corpora menu (eight, saved on launch) and drag-and-drop of a corpus file or folder.
16. **Check runtime** performs one CPU optimizer step and reports a pip repair command for a
    missing module; training is blocked while that check fails.

## Whitecube theme

Black navigation rail with the cube mark and a live job card (replacing the tab switcher),
square cube-style dialogs, stat tiles with the hard offset shadow, monochrome chips, a black
terminal-style log and preview with red errors, a monochrome loss chart with a red validation
line and square best marker. Headings are no longer selectable, so page titles are not shown
highlighted when a page takes focus.

# Whitecube Trainer 0.8.0: 53 upgrades

The 0.7.0 inventory follows below. Tests and documentation are not counted.

## Training plan and run controls — train_folder.py, train_local.py, tensegrity_nb_lib.py

1. Peak learning rate for new runs (`--learning-rate`).
2. Final learning rate as a fraction of the peak (`--min-lr-ratio`).
3. Explicit warmup steps, with the previous automatic warmup as default.
4. AdamW weight decay (`--weight-decay`).
5. Gradient clipping threshold, including off (`--grad-clip 0`).
6. Validation/checkpoint interval (`--eval-interval`).
7. Seed for weights, batches and the corpus split (`--seed`).
8. Validation share of documents (`--val-fraction`).
9. Warmup-stable-decay schedule as an alternative to cosine (`--schedule wsd`).
10. Early stopping after N evaluations without a new best validation loss.
11. Wall-clock time limit that saves and stops (`--max-minutes`).
12. Step-numbered checkpoint snapshots with pruning of the oldest (`--keep-snapshots`).
13. Thermal pause with hysteresis and a 15-minute cap, keeping the hard stop limits.
14. Resume reuses the saved plan, rejects conflicting plan flags and keeps seed/split for new runs only.
15. `result.json` records why training ended: completed, Stop, early stop, time limit or temperature.
16. `result.json` records final training/validation loss, elapsed and cumulative training time, finish time and peak VRAM.
17. `run.json` records the creation time and complete plan; the log prints a plan summary.

## Previews and export — preview.py, export_model.py, app.py

18. Preview length control.
19. Preview temperature, including greedy decoding at 0.
20. Preview top-k control.
21. Nucleus (top-p) sampling in the app and `preview.py --top-p`.
22. Preview seed control for repeatable samples.
23. Best and Latest previews side by side.
24. Generation statistics: new bytes, time and sampling settings.
25. Save previews with a run; recent saves are listed and included in reports.
26. Export Best or Latest weights as safetensors plus the model configuration.
27. Generated model card with size, steps, validation loss, corpus tokens, plan and training time.

## Runs page — app.py and engine.py

28. Sort runs by newest, oldest, name, best validation loss or model size.
29. Pin runs; pinned runs sort first and have their own filter.
30. Per-run notes (up to 2,000 characters), included in reports.
31. Run rows show validation loss and which checkpoints exist.
32. Disk use of the selected run and of all runs, measured in the background.
33. Run details summarize device, batch × accumulation, peak rate, schedule, seed and corpus.
34. Run details show creation time, cumulative training time, peak VRAM and snapshot count.
35. Run details and finish notifications explain the stop reason.
36. Copy the equivalent `whitecube-train --resume` command.

## Progress page — app.py

37. Optional smoothed training loss.
38. Pointer readout of the nearest step, training/validation loss and learning rate.
39. Gridlines and a labelled midpoint on the loss axis.
40. Best validation loss marker.
41. Optional learning-rate overlay.
42. Highlighted errors, warnings/thermal events and improvements in the log.
43. Show only log lines matching the search.

## Train page — app.py and engine.py

44. Plan presets: smoke test, short, standard and long.
45. Plan estimate: training tokens, approximate corpus passes and expected duration from earlier runs.
46. Reset training settings without touching the corpus, Python environment or run name.
47. Runtime check reports free/total GPU memory and bf16 support.
48. Starting or resuming on CUDA is blocked when the checked environment has no CUDA GPU.
49. Corpus inspection lists files per format and the largest file.

## Application and command line

50. The window size is remembered across launches.
51. A desktop notification announces finished jobs when the window is not focused.
52. Shortcuts dialog (F1 or the header button), plus Ctrl+P to preview and Ctrl+E to export a report.
53. `whitecube-train --list-runs` and `--status` summarize runs without PyTorch or the training lock.

## 0.8.0 verification

| Check | Result |
| --- | --- |
| Training interpreter: all trainer tests | 140 run, all passed (21 GTK tests skipped there) |
| System Python: engine tests | 43 passed |
| System Python: GTK presentation tests | 17 passed |
| Native Wayland UI smoke, including the new controls | Passed |
| Packaging/install-tree checks | 22 passed |

New coverage: `tests/test_engine_upgrades.py` (settings, commands, pins, notes,
previews, exports, disk use, throughput, CLI summaries, presentation helpers)
and `tests/test_training_upgrades.py` (early stopping, snapshots, stop reasons,
thermal pause, time limits, a real CPU CLI run with every new flag, resume
rejection, safetensors export equality and repeatable top-p previews). No GPU
hardware training was run for 0.8.0.

# Whitecube Trainer 0.7.0: 90 improvements

This inventory groups related fixes into reviewable behavior changes. Tests and
documentation are additional work, not counted as separate improvements.

## Desktop workflow — app.py

1. Search runs by name, ID, model size and phase, without case sensitivity.
2. Filter by checkpoints, prepared state or failures, with matching counts and empty-result feedback.
3. Select an available run automatically and clear stale details when a run disappears.
4. Reuse a saved run's settings as a new draft, validating metadata before changing controls.
5. Open a selected run in the file manager and surface launch errors.
6. Validate resume targets immediately and display additional optimizer steps.
7. Restore draft run names across app launches and enforce the name-length limit while typing.
8. Show effective sequences/tokens per optimizer step and the configured output directory.
9. Preview either Best or Latest, with checkpoint-aware controls and Enter-to-preview.
10. Copy generated preview text directly.
11. Discard stale preview results and prevent conflicting preview/training/deletion actions.
12. Search displayed logs, wrap to the next match and show match/no-match feedback.
13. Scroll immediately to the latest output when Follow is re-enabled.
14. Export the displayed log tail as text with an explicit scope.
15. Export plotted metrics as CSV with all observed columns and spreadsheet-safe text cells.
16. Export reports, metrics and logs in the background with private atomic destination replacement.
17. Cache metrics by file identity attributes to avoid reparsing unchanged history every second.
18. Plot malformed/constant/large loss series safely, preserve extrema and endpoints, and display a legend.
19. Display throughput, processed tokens, gradient norm and human-readable ETA.
20. Display corpus preparation phases and preserve progress loss before log metrics arrive.
21. Reject empty corpus inspection paths, prevent duplicate scans and discard stale inspection results.
22. Invalidate runtime checks when the selected Python changes and discard obsolete probe results.
23. Start file choosers at the current corpus location and explain unsupported nonlocal selections.
24. Make Stop and Trash submissions idempotent while their operations are pending.
25. Clean up window timers/callbacks; add contextual Ctrl+Enter/Ctrl+F and job-completion notifications.

## Job management — engine.py and worker.py

26. Bound metadata reads and recover from malformed encoding, deep JSON and nonfinite numbers.
27. Recover corrupt preference types/ranges/enums and exclude unknown preference keys.
28. Isolate runtime probes and explain interpreter startup failures, timeouts and invalid responses.
29. Validate PID identities so dead/missing/reused process identifiers cannot masquerade as active jobs.
30. Release completed child registrations instead of accumulating them in memory.
31. Sanitize run display metadata and ignore invalid job paths without breaking neighboring runs.
32. Reconcile stale training status while respecting the shared CLI training lock.
33. Reject fractional/boolean numeric settings and validate toggles, field paths, names and source paths.
34. Canonicalize dataset/run paths so persistent jobs match the correct directories.
35. Serialize desktop launches across windows until the worker has registered its identity.
36. Record worker-launch failures and registration timeouts as diagnosable terminal jobs.
37. Handle process-exit races during Stop and install worker signal handlers before registration.
38. Manage training process groups, clean up descendants and bound unresponsive cancellation.
39. Record child identities, exit/signal reasons and forced-stop outcomes; export failure/progress/status context.
40. Sort corpus inspection results, report format/readability information and tolerate malformed metric/telemetry records.

## Corpus preparation — prepare_folder.py, prepare_parquet.py, corpus_split.py, files.py

41. Validate typed preparation parameters before creating output and report missing/unsupported paths clearly.
42. Preserve complete UTF-8 characters when truncating to the token cap.
43. Accept UTF-8 BOMs in JSONL, TXT and Markdown without emitting BOM tokens.
44. Report JSONL syntax/encoding locations and enforce its line limit in bytes.
45. Resolve literal dotted JSON field names as well as nested field paths.
46. Skip null, empty and whitespace-only documents while recording scanned/skipped counts.
47. Share typed Parquet column selection across both preparation entry points.
48. Preserve parent-struct nulls while extracting nested Parquet text.
49. Reject empty Parquet shards during preflight, including shards beyond the cap, and close readers deterministically.
50. Improve HTML extraction for headings, sections, lists, tables and self-closing tags; remove non-content elements.
51. Group normalized text-file BOM/newline variants together to prevent train/validation leakage.
52. Validate folder splits against a configurable model context size.
53. Support configurable Parquet batch sizes in folder ingestion.
54. Publish structured preparation callbacks with phase, token and document progress.
55. Check cancellation during hashing, encoding, split copying and immediately before publication.
56. Detect source file changes during preparation and reject inconsistent output.
57. Validate split offsets, counts, hashes and exact binary lengths before copying data.
58. Stage and publish complete corpus directories, preserve existing output and clean up failed staging.
59. Record versioned token format, artifact sizes/checksums, source provenance, duplicate groups and actual split statistics.
60. Expose deterministic Parquet split seeds, report file shuffling accurately and durably sync metadata/corpus publication.

## Training and supervision — train_local.py, tensegrity_lm.py, tensegrity_nb_lib.py, train_folder.py, train_supervisor.py, pacing.py, preview.py, tensegrity_serve.py

61. Reject empty/odd-length binary corpora and invalid token IDs before local model allocation.
62. Accept the exact minimum context-plus-target corpus length.
63. Include the final valid sampling window in mixed-corpus batches.
64. Validate split names, integer batch/context sizes and finite nonnegative corpus weights.
65. Bound the memory-map cache and refresh replaced or modified token files.
66. Verify corpus checksums, byte counts and token format before training/resume, retaining legacy compatibility.
67. Save local model configuration atomically.
68. Select supported training precision automatically, including fp16 fallback.
69. Validate step/evaluation/schedule settings and required EWC anchors before training work.
70. Persist batching settings and reject incompatible resume batch/accumulation changes.
71. Reject backward resume targets and invalid saved optimizer learning rates.
72. Write checkpoints to unique private temporary files and preserve old checkpoints if staging fails.
73. Sync checkpoint directory entries after atomic replacement.
74. Validate checkpoint structure, state mappings, step and loss fields with useful errors.
75. Preserve Python RNG alongside NumPy/PyTorch, without initializing CUDA during CPU RNG capture.
76. Resume CPU/bf16 checkpoints with an enabled fp16 scaler without loading an empty scaler state.
77. Reject nonfinite gradients before changing model/optimizer state.
78. Recover fp16 scale overflows with bounded retries and no optimizer update or completed-step inflation.
79. Schedule periodic evaluation by completed steps and reuse an unchanged final evaluation.
80. Reject nonfinite validation losses before replacing checkpoints.
81. Record token throughput, processed tokens, ETA and gradient norm using monotonic elapsed time.
82. Preserve final training loss/learning rate and append a final validation/completion history event.
83. Add deterministic preview seeds and configurable generation length, temperature and top-k.
84. Handle empty prompts with BOS, validate generation controls/token IDs/logits and use float32 sampling arithmetic.
85. Check the hottest GPU reading and reject malformed or implausible temperatures.
86. Isolate bad CPU sensors so other valid readings remain available.
87. Reject invalid initial thermal probes before launching training.
88. Keep partial run reports and telemetry/report-write failures from disabling watchdog shutdown.
89. Bound forwarded-signal shutdown, tolerate exit races and restore training signal handlers afterward.
90. Validate pacing, honor Stop outside cooling, restore cooling state after errors and publish throttled preparation status.

## Verification

Validation uses temporary corpora and CPU training; live user corpora and runs are
not test fixtures. Tests cover actual 2M/5M/10M models, bit-identical interrupted
versus uninterrupted resume, persistent-worker cooling/stop/resume/preview,
corpus integrity, injected write failures and native GTK interactions. GPU
overflow and watchdog edge cases use controlled tests rather than hardware runs.

Commands and prerequisites are in [README.md](README.md). Final validation on
2026-10-10:

| Check | Result |
| --- | --- |
| Pipeline, corpus, engine and worker regression tests | 98 passed on the training interpreter |
| GTK presentation/async/export/plotting tests | 17 passed on system Python |
| Native Wayland UI smoke | Passed; all three pages rendered and exercised |
| Packaging/install-tree checks | 22 passed |
| Python compilation, CLI help/version, diff whitespace | Passed |
| Real persistent-worker CPU smoke | Passed earlier; final rerun stopped at its thermal preflight |

The final combined training-interpreter run discovered 114 tests: 98 passed,
15 GTK tests were skipped because that interpreter lacks GTK bindings, and the
real-machine smoke test failed its environment precondition because the CPU did
not cool below 85°C within 60 seconds (the observed temperature was 95°C). The
system interpreter subsequently passed all 17 GTK tests, including the two final
plotting/CSV regressions. The temperature guard was preserved; no existing
workload was stopped to force the test to pass.

There are 115 passing trainer tests across the two interpreters, plus 22 passing
packaging checks and the native UI smoke. Earlier successful end-to-end coverage
included persistent training, cooling, graceful Stop, saved checkpoints, resume
and CPU preview. Final worker regressions additionally verify descendant cleanup
after supervisor failure, stubborn grandchildren and PID reuse.

Local logs and screenshots are under `build/trainer-checks/` at the repository
root. No GPU hardware training was performed.

## Corpus handling update — 2026-10-11

- Move document records, duplicate lookups, grouping and split sorting to a
  disposable SQLite index with a 4 MiB cache and disk-backed sorting.
- Add optional exact deduplication across formats before counting accepted tokens
  against the corpus cap. Hashes cover normalized source and retained text.
- Expose seeded file shuffling, retained minimum length, per-document maximum
  length and Parquet batch size in the desktop and CLI; persist them with runs.
- Preserve complete UTF-8 characters under both caps, and continue to later
  documents after applying a per-document cap.
- Stream TXT/Markdown as a single document with one EOS, preserving whitespace
  across internal read boundaries.
- Union matching source and retained-text hashes into validation groups. Repeated
  sources with different final prefix lengths and identical prefixes from
  different sources stay in the same split.
- Publish a checksummed `documents.jsonl` with source paths/physical rows, split
  offsets/counts, source/retained/group hashes and truncation flags. New manifests
  use schema version 3 and report duplicate, short and limit-truncation counts.
- Reuse prepared folders for fresh model runs with independent, verified copies,
  preserving selection and splits without needing the original sources. Add
  **Reuse prepared corpus** on Runs and disable irrelevant selection controls
  after inspecting a prepared folder.
- Share streaming integrity/token validation across reuse and training, retain
  legacy corpus support, and roll back staging on cancellation, corruption or
  source mutation.

The scaling fixture prepares 10,000 and 100,000 distinct documents (910,000 and
9,100,000 tokens) with deduplication. Python peak allocations measured by
`tracemalloc` were 2.021 and 2.025 MiB; process maximum RSS was 75.1 and 79.1 MiB.
SQLite/native buffers are included in RSS, not in `tracemalloc`. The check is a
memory-scaling measurement; its timings include tracing and concurrent tests.
It uses temporary synthetic data, not the user's live corpus.

Logs, the repeatable scaling script and native screenshots for this update are
under `build/corpus-*` and `build/benchmark-corpus-scaling.py`. The CLI regression
uses a controlled healthy temperature probe; Monitor/watchdog tests separately
exercise thermal stops and pauses. The live-machine worker smoke was excluded
because the host had already reached its 95 °C stop limit. Runtime limits remain
unchanged, and no GPU hardware training was performed.

Final checks for this update:

| Check | Result |
| --- | --- |
| Training-interpreter regression suite | 137 passed; 22 GTK tests skipped |
| System-Python GTK/presentation tests | 22 passed (18 app tests plus 4 presentation tests in the engine suite) |
| Unique trainer tests across both interpreters | 159 passed |
| Native Wayland UI smoke | Passed, including corpus controls, saved preferences, reset and reuse action |
| Packaging/install-tree checks | 22 passed |
| 10K/100K document memory check | Passed; Python peak allocations stayed about 2.02 MiB |
| Python compilation and diff whitespace | Passed |

The 159-test training-interpreter discovery excluded the live-machine worker
smoke and skipped the 22 GTK tests; those GTK tests passed separately on system
Python. Changes are in the checkout; the installed system copy was not replaced.
