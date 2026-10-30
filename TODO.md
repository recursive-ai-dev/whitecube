# Whitecube Engineering Improvements Roadmap (`TODO.md`)

Ordered strictly from **most negatively affecting the application** (memory corruption, deadlocks, UI freezes, and HiDPI layout errors) down to **minor edge-case hardening** (defensive parsing and total-function numerical bounds).

---

## Tier 1 — Critical Memory Safety, Deadlocks & UI Freezes

- [x] **1. [`src/cube-audio/cube-audio.c`] Prevent stack buffer overflow during high-ratio upsampling (`conv_push` / `cmd_play`) and resource leaks on allocation failure**
  - **Problem**: `cmd_play` passed a stack buffer `int16_t tmp[2 * 64]` (64 stereo frames) to `conv_push`, which had no output capacity bound. When upsampling across supported ALSA/WAV sample rates with a ratio $> 64\times$ (e.g., a $4000\text{ Hz}$ WAV played on a $384000\text{ Hz}$ PCM device where $\text{step} = 4000 / 384000 = 1/96$), `while (c->pos < 1.0)` emitted $96$ stereo frames per input frame, overflowing `tmp` by $32$ frames ($128\text{ bytes}$) on the stack. Additionally, if `malloc` failed for `in` or `out` in `cmd_play`, `w.f`, `pcm`, and any partial buffer allocation were leaked.
  - **Fix**: Bounded `conv_push` with `CONV_MAX_FRAMES = 128` (exceeding the maximum physical ratio $\lceil 384000 / 4000 \rceil = 96$), sized `tmp` and `out` capacity in `cmd_play` accordingly, cleaned up all resources (`in`, `out`, `pcm`, `w.f`) on allocation failure, and added unit tests in `src/cube-audio/test-cube-audio.c` covering extreme upsampling ($4\text{ kHz} \to 384\text{ kHz}$) and zero-rate inputs.

- [x] **2. [`src/cube-settings.c`] Fix `job_launch` truncated-`snprintf` out-of-bounds read, short pipe writes, synchronous `waitpid` UI freeze, pipe buffer deadlock, and missing job timeouts**
  - **Problem**:
    1. In `job_launch`, `n = snprintf(feed, sizeof(feed), ...)` and `n += snprintf(feed + n, sizeof(feed) - n, ...)` could accumulate `n >= sizeof(feed)` on truncation, causing `write(in[1], feed, (size_t)n)` to read past the stack buffer, and performed a single unlooped `write()`.
    2. In `job_read`, `j->obuf` grew without an upper bound or `realloc` NULL check.
    3. When `j->out < 0 && j->err < 0`, `waitpid(j->pid, &status, 0)` blocked the Wayland UI thread synchronously if a child closed stdout/stderr before exiting, and hung jobs had no timeout or process-group (`setsid()`) isolation.
  - **Fix**: Bounded `snprintf` accumulation safely, looped on `write` handling `EINTR`, scrubbed `feed` with `explicit_bzero`, bounded `j->obuf` at `MAX_JOB_OUT` ($4\text{ MiB}$) while draining `j->out` / `j->err` to EOF, isolated children with `setsid()` and clean signal masks, and implemented non-blocking `waitpid(..., WNOHANG)` with per-job deadlines and process-group `SIGKILL` cleanup. Verified in `tests/settings-test.c`.

- [x] **3. [`src/cube-audio/cube-audio.c`] Prevent `uint32_t` chunk-size wrap-around and past-EOF seeking in `wav_open`**
  - **Problem**: In `wav_open`, `fseek(w->f, (long)len + (len & 1), SEEK_CUR)` could wrap on 32-bit `long` or seek past EOF on a regular file without reporting truncation when a malformed chunk header specified `len = 0xffffffff`.
  - **Fix**: Computed 64-bit `skip = (int64_t)len + (len & 1)`, verified against regular file size via `fstat`/`ftello`, and sought with `fseeko`. Verified with a regression test in `src/cube-audio/test-cube-audio.c`.

---

## Tier 2 — Compositor Geometry, Process Supervision & Resource Hygiene

- [x] **4. [`src/whitecube.c`] Use logical effective output resolution (`wlr_output_effective_resolution`) in `t_commit` for initial window sizing and bounds**
  - **Problem**: While `wc_retile` and `wc_start_drag` compute workspace dimensions via `wlr_output_effective_resolution`, `t_commit` inspected raw mode dimensions (`out->wlr_output->width` and `out->wlr_output->height`). On HiDPI outputs (e.g., scale $2.0$) or rotated outputs ($90^\circ / 270^\circ$), newly mapped windows received initial `set_size` and `set_bounds` in raw physical pixels rather than logical coordinates.
  - **Fix**: Queried `wlr_output_effective_resolution` on the target output in `t_commit`, clamping to sane fallback dimensions ($1280 \times 800$) when no enabled output is present. Verified in `tests/compositor-test.c`.

- [x] **5. [`src/cube-ca.c`] Terminate and reap per-output wallpaper children on output removal and supervisor exit**
  - **Problem**: `registry_remove` did not signal the per-output child process when an output was removed in supervisor mode, and `supervisor_main` sent `SIGTERM` on shutdown without reaping children.
  - **Fix**: Sent `SIGTERM` to `o->pid` on output removal in supervisor mode and reaped terminated child processes cleanly in `supervisor_main` shutdown.

- [x] **6. [`src/cube-status.c` & `src/cube-settings.c`] Close duplicate `/dev/null` file descriptor (`null > 2`) in child process setup**
  - **Problem**: Both `job_start` in `src/cube-status.c` and `spawn_detached` in `src/cube-settings.c` opened `/dev/null` and called `dup2(null, 0)` without closing `null` when `null > 2`, leaking an extra open file descriptor into child helpers and launched desktop applications.
  - **Fix**: Closed `null` when `null > 2` after `dup2(null, 0)` (matching `spawn_cmd` in `src/whitecube.c`).

---

## Tier 3 — Input Parsing, Configuration & State Validation

- [x] **7. [`src/desktop-state.h`] Reject non-finite (`NaN`/`Inf`) or trailing-junk accessibility scales and truncated lines in `wc_state_load`**
  - **Problem**: `wc_json_number` accepted trailing non-numeric characters (e.g., `1.2junk`), `wc_json_true` matched prefix `true` without checking token delimiters, and `wc_state_load` did not check whether `fgets` truncated an overlong line.
  - **Fix**: Required valid JSON delimiters after numbers and `true` tokens in `wc_json_number` and `wc_json_true`, and rejected truncated lines in `wc_state_load`. Verified in `tests/desktop-state-test.c`.

- [x] **8. [`src/cube-cosmos.h`] Drain overlong configuration lines in `cosmos_config` so line tails cannot forge `cosmos=1` directives**
  - **Problem**: `fgets(line, sizeof(line), f)` in `cosmos_config` used a 128-byte buffer without draining the remainder of lines longer than 127 bytes, allowing a substring `cosmos=1` at offset 128 of a comment or unrelated setting to be misparsed as a new line.
  - **Fix**: Detected truncated lines (`!strchr(line, '\n')`) and drained characters up to `\n` or `EOF`. Verified in `src/cube-ca.c` (`cosmos_self_test`).

- [x] **9. [`src/cube-status.c`] Harden `app_argv` for trailing unescaped `%` and deprecated Desktop Entry Specification field codes**
  - **Problem**: In `app_argv`, a token ending in a single unescaped `%` bypassed `if (*c == '%' && c[1])` and was emitted literally instead of being rejected, and standalone deprecated field codes (`%d`, `%D`, `%n`, `%N`, `%v`, `%m`) were not stripped before token inspection.
  - **Fix**: Rejected trailing unescaped `%` in `app_argv`, ignored standalone deprecated field codes per the Desktop Entry Specification, and added unit test coverage in `tests/status-apps-test.c`.

- [x] **10. [`src/cube-ca.c`] Verify rejection of `speed nan` in `cosmos_self_test`**
  - **Problem**: `cosmos_self_test` verified `zoom nan` rejection but did not assert `speed nan` rejection.
  - **Fix**: Added `cosmos_control(&c, "speed", "nan")` check to `cosmos_self_test` in `src/cube-ca.c`.

---

## Tier 4 — Mathematical Rigor, Numerical Stability & Script Hardening

- [x] **11. [`src/cube-field.h`] Validate `rotate` control direction argument strictly**
  - **Problem**: `apply_control` in `src/cube-field.h` accepted any argument (or no argument) to `rotate` and treated anything other than `"left"` as `"right"`.
  - **Fix**: Required `arg` to be `"left"` or `"right"` in `apply_control` and verified in `field_self_test`.

- [x] **12. [`src/bar-layout.h` & `src/tab-panel.h`] Make layout geometry functions total over all `int` and IEEE 754 `double` inputs**
  - **Problem**: `wc_bar_layout(int width, double scale)` divided by `scale` without guarding against `scale <= 0` or `NaN`, and `wc_tab_capacity` did not guard against negative `gap` or large `tab_w` integer overflow.
  - **Fix**: Clamped `scale` to $[0.5, 4.0]$ (defaulting `NaN` to $1.0$) in `wc_bar_layout` and bounded `tab_w` and `gap` in `wc_tab_capacity`.

- [x] **13. [`libexec/cube-settings-helper`] Harden sysfs/procfs and `cube-net` parsing against malformed or empty driver files**
  - **Problem**: `power()` called `int(read(...))` without catching `ValueError`, `system()` indexed `read("/proc/uptime", "0").split()[0]` without guarding against empty output or non-numeric `/proc/meminfo` fields, and `net_scan()` assumed `signal_dbm` and `security` were always present.
  - **Fix**: Added defensive parsing and `.get()` defaults in `libexec/cube-settings-helper`.

- [x] **14. [`bin/whitecube-doctor`] Parse `Exec=` lines in `check_folder_handler` with `shlex.split` and strip `env` prefixes**
  - **Problem**: `check_folder_handler` extracted the command via `line[5:].split()[0]`, which retained surrounding quotes on `Exec="/usr/bin/thunar" %F` or reported `env` on `Exec=env VAR=1 thunar`.
  - **Fix**: Parsed `line[5:]` using `shlex.split` (falling back to `.split()` on malformed quoting), skipped leading `env` assignments, and verified in `tests/test-doctor.py`.

- [x] **15. [`bin/whitecube-terminal`] Stop scanning `foot` options at `-e` in `terminal_argv`**
  - **Problem**: `terminal_argv` scanned `args` for `-c` / `--config` until `--` or a non-flag argument, but `foot` also accepts `-e` to begin the child command argv.
  - **Fix**: Treated `-e` like `--` as terminating option scanning for `foot` itself, and added a unit test in `tests/test-desktop-improvements.py`.

- [x] **16. [`src/status-theme.h`] Guard `status_theme_color` against `NULL` input pointers**
  - **Problem**: `status_theme_color` dereferenced `value` before checking for `NULL`.
  - **Fix**: Added a `NULL` guard in `status_theme_color` and verified in `tests/status-theme-test.c`.
