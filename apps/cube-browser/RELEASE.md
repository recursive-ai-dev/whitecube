# Cube Browser 0.9.0

Released locally on 2026-10-09 after the eight-phase development sprint. The
existing launcher uses the new build. All source changes are checkpointed in Git;
`v0.9.0` identifies the release. No remote publishing or desktop restart is needed.

## Changes from 0.4

1. Reproducible versioning, engine modules and a ten-document rendering corpus.
2. Better HTML recovery and CSS selectors, variables, flex, grids, positioning
   and overflow clipping.
3. Landlock/seccomp worker restrictions, capability/environment cleanup,
   centralized origin rules, mixed-content refusal and supported CSP enforcement.
4. POST/password forms, cookies/login/logout, conditional cache, origin storage
   and independent private profiles.
5. A bounded QuickJS core behind the replaceable `cube-js` API.
6. DOM mutations/events, cancelled defaults, classic/deferred scripts,
   promises/timers, text/JSON fetch and browser storage APIs.
7. Native settings, per-site script choices, console/network diagnostics,
   downloads with progress/cancellation and scaling/recovery improvements.
8. Release regressions, sanitizer/mutation checks, native screenshots, corpus
   comparison, installer rollback, compatibility documentation and version 0.9.0.

Final review also fixed Origin headers, opaque-origin storage disclosure,
subresource cookie withholding, encoded storage quotas, fetch text boundaries,
304/CORS metadata merging, redirect diagnostics and clearing the back/forward trail.

## Evidence and limitations

The release passes 23/23 regression suites, 13 sanitizer component suites and
1,000 deterministic mutation cases. Ten corpus pages produce 20 validated
exports, and native Wayland workflows pass in an isolated compositor. The rebuilt
baseline/release corpus medians are 125.64/158.13 ms, a measured 25.9% slowdown.
The two public-site smoke checks complete; Debian still reports seven unavailable
resources. Detailed evidence and coverage limits are in [VALIDATION.md](VALIDATION.md).

This is an interactive-web preview. Modern applications may depend on unsupported
layout or APIs. See [COMPATIBILITY.md](COMPATIBILITY.md) before treating a site as
supported. Full conformance and the full sandbox design in `spec.md` are future work.

## Local artifacts

- `build/releases/cube-browser-0.9.0-src.tar.gz`: source at the release commit.
- `build/releases/cube-browser-0.9.0-validation.tar.gz`: logs, native frames,
  corpus reports/PNGs and public-site smoke observations.
- `build/releases/SHA256SUMS`: artifact integrity hashes.
- `build/releases/build-info.json`: version, source commit and platform.

The source archive excludes `.deps/` and build products. Use the pinned bootstrap
scripts and `BUILD.sh` after extraction. Executables in `build/` use private
libraries in this checkout and are not presented as portable binaries. Dependency
notices remain in the vendored files and `.deps/licenses/` after bootstrapping.

## Installation rollback

The installer prints a plan unless `--apply` is supplied. New installation
manifests record originals and installed-file fingerprints:

```sh
scripts/install-user.py --restore /absolute/path/to/manifest.json
scripts/install-user.py --restore /absolute/path/to/manifest.json --apply
```

Rollback restores saved originals, removes only entries created by that install,
and keeps the backup. It refuses the entire operation before writing if any
installed file was subsequently changed. Review those edits manually. Manifests
from before 0.9 lack fingerprints and require manual restoration from their saved
files. Browser profiles and downloaded files are not installer rollback targets.

## Returning to the preserved 0.4 source

The validated pre-sprint snapshot is Git commit `488443d` and
`build/before-0.9-sprint.tar.gz`. Create a separate checkout to inspect or rebuild it:

```sh
git worktree add --detach ../cube-browser-0.4 488443d
```

Prepare its dependencies according to that checkout's README, build there, and
run its own `cube-browser.sh`. To repoint the user launcher deliberately, run that
checkout's installer after reviewing its plan. Keep the 0.9 checkout and profile
backup until the old build is verified. This avoids discarding the sprint work;
no destructive reset or automatic profile downgrade is required.
