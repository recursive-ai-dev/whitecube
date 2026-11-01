# Whitecube Desktop 0.10.2

Whitecube 0.10.2 adds drag and drop between applications. Earlier compositors ignored
every client's request to start a drag, so dragging a file out of a file manager or
into a browser did nothing. The supported package target is Debian 13, amd64.

## Drag and drop

Files and other content can now be dragged between Wayland windows, for example from
PCManFM-Qt onto an upload area in Firefox. The dragged icon follows the pointer, and the
window under the pointer receives the drop when the button is released.

A drag can start only from the mouse press that the application is currently holding. A
request with any other serial is refused and its application told so. Locking the screen
cancels a drag in progress, and desktop clicks do nothing until the drag ends. Touch drags
are not supported, because the compositor does not handle touch input.

## File dialogs

Applications open their own file dialogs, and those dialogs need no change. Firefox's
snap opens its GTK chooser without a portal, as a focused dialog above the Firefox window.
The session still sets no D-Bus address and installs no portal.

## Limits

Cube Browser still cannot upload files: its forms accept only URL-encoded bodies up to
16 KiB, and its pages have no file APIs. It shows a notice instead of a file dialog. Use
Firefox for uploads. Cube Browser also ignores files dropped on its window.

## Included from unpublished versions

This is the first published package since 0.9.2 with the changes from 0.9.3 through
0.10.1, including floating windows and `sudo cube-net takeover`. Their notes are in
`docs/RELEASE-0.9.3.md` through `docs/RELEASE-0.10.1.md`.

Log out and back in after upgrading to load the new compositor.

Build with `bash tools/build-debian.sh`. The result is
`packaging/out/whitecube-desktop_0.10.2_amd64.deb` with its checksum and validation manifest.
