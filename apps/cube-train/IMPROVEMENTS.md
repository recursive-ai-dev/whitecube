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
