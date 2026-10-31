# Whitecube Desktop 0.9.6

Whitecube 0.9.6 includes Cube Browser 0.10.1 with expanded image handling and
native configuration. The supported package target is Debian 13, amd64.

The browser opens PNG, JPEG and static SVG URLs and local files directly, and
renders bounded base64 or percent-encoded inline images. Remote resources stay
behind the existing broker and worker boundaries; inline images obey Content
Security Policy and image preferences. SVG scripts and external references stay
inert. GIF, WebP, AVIF, animation and responsive image selection remain unsupported.

Open Menu, press Ctrl+comma, enter `cube:settings` or use `--settings` for the
General, Content, Privacy, Downloads and Advanced sections. Editable home/search
URLs, 50–300% zoom and an absolute download folder are validated before saving.
Switches control images, JavaScript, cookies, origin storage, cache, history,
clear-on-exit and HTTPS-only requests. Per-site script exceptions remain available.
Advanced also reloads configuration from disk and restores defaults. Private
settings stay in memory. Clear browsing data preserves bookmarks and downloads.

Deterministic tests inspect actual rendered image pixels, direct and inline
image requests, malformed inputs, local-file boundaries and CSP. Native Wayland
tests exercise editing, validation/cancellation, saved settings after restart,
image blocking and repainting, custom-folder downloads, history controls,
clear-on-exit and private-window nonpersistence. The regular desktop build runs
its existing native, application, Python and Node checks before packaging.

Build with `bash tools/build-debian.sh`. The result is
`packaging/out/whitecube-desktop_0.9.6_amd64.deb` with its checksum and validation
manifest. Building does not install the package or restart the desktop.
