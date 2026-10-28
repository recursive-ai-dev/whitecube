# Settings profiles

Save explicit desktop preference overrides to a private JSON file:

```sh
cube-preferences export ~/whitecube-settings.json
cube-preferences check ~/whitecube-settings.json
cube-preferences import ~/whitecube-settings.json
```

Export replaces the named file atomically with permissions `0600`. Omit the
filename, or use `-`, to export to stdout or read a profile from stdin. For
example, `cube-preferences export | cube-preferences check` validates an export
without changing settings. Exported command preferences remain literal strings.

Import merges the exported overrides into the current account's preferences.
Keys absent from the profile keep their current values. An empty profile is a
no-op. Use `cube-preferences reset KEY` to restore inheritance for one setting.
Export contains only explicit overrides, so it does not capture system defaults,
wallpaper files, app libraries, credentials, or the contents of config files.

The versioned profile retains JSON boolean, integer and string types. Unknown
keys, duplicate keys (including underscore/hyphen aliases), unsupported versions,
invalid values and profiles over 64 KiB are rejected. All supplied values and
the resulting keyboard layout are validated before one dconf changeset is
committed; saved values are then read back for verification. `check` performs
validation without starting a writer. Import uses a fresh D-Bus writer and the
same per-user lock as ordinary preference changes.
