# Whitecube boot chain

Three layers, all using the same palette (`#04070d`, red `#ff3b3b`) and the same free wallpaper API
(Lorem Picsum, no key). Nothing here is installed automatically.

## Watermark rule: 123% canvas zoom
`detectWatermark` (`watermark.ts`, mirrored in `make-boot-bg.py`) looks for a dense cluster of hard edges
in a corner or bottom band that is much busier than the image centre. When it finds one, the canvas is
drawn at **123%**, anchored away from the mark so that side is cropped off. Clean images stay at 100%.
It is a heuristic; Picsum photos rarely carry watermarks, so it mostly matters if you point the fetcher elsewhere.

## 1. In the app (done, tested)
Startup shows a boot chain: Wayland gate, host, system, network (gateway probe), wallpaper, outside, updates.
A failing stage is reported and the app still starts; Esc skips. "Outside" (weather via ipwho.is + open-meteo)
is **off by default** because it sends your public IP to those services; enable it on the Overview card.
Ctrl+K > "New wallpaper" fetches another one.

## 2. Session chain
`whitecube-bootchain` runs: wayland socket, wallpaper refresh (cached, falls back to the last image), swaybg,
cube-ca, cube-status, Whitecube Command Center, logging each stage to `~/.local/state/whitecube/bootchain.log`.
To use it, add `~/whitecube/command-center/boot/whitecube-bootchain &` to the `clients` line in `~/whitecube/whitecube-session`
(then log out and back in; do not restart the compositor from inside the session).

## 3. Plymouth splash
`sudo boot/install-plymouth.sh` installs the theme, builds `bg.png` through the same watermark/123% logic and
runs `plymouth-set-default-theme -R whitecube`, which rebuilds the initramfs. The theme includes a passphrase
prompt, so an encrypted root still shows its unlock prompt. This was syntax-checked but **not booted**: I cannot
exercise Plymouth from inside a Wayland session. Keep the old theme name handy to revert.

## Tests
`python3 boot/test_make_boot_bg.py` and `npm test`.
