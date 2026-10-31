# Floating stacking windows

Whitecube keeps ordinary windows visible together on each workspace. New windows
choose their preferred initial size within the work area and cascade across it.
Large initial sizes are capped to leave a desktop margin; clients without an
initial preference use a bounded fallback. Focusing a
window raises its family without rearranging the grouped window list. Dialogs
remain above their parents; selecting a modal parent focuses its deepest dialog.

- Click a visible window to focus and raise it.
- Drag a supported client title bar or resize border normally. Move/resize
  requests require the current pointer grab and a valid input serial.
- Hold Alt and left-drag anywhere in a window to move it.
- Hold Alt and right-drag to resize from the bottom-right corner.
- Alt+Shift+arrows moves the focused window; Alt+Ctrl+arrows resizes it.
- Alt+Tab / Alt+Shift+Tab cycles windows. Alt+G opens the grouped window list.
- Maximize uses the work area. Restoring returns to the floating geometry.
- Alt+F toggles fullscreen. Fullscreen hides unrelated windows until focus leaves
  that fullscreen family or fullscreen is restored.
- Alt+V shows the desktop and restores the workspace's windows.

Placement stays inside the logical work area. Client minimum and maximum sizes
are constrained to the available output, and corner resizing anchors the opposite
edges. Pointer deltas saturate before integer conversion; non-finite input is
ignored. Output removal, workspace changes, client closure and locking cancel
interactive grabs and balance their pressed-button state. Layout persistence
continues to honor the privacy settings and never launches saved commands.

The compositor keeps its existing workspace/output policy. This change does not
restart a running desktop; use the rebuilt distribution after logging out and
back in. Regression coverage exercises real scene-node visibility and stacking,
maximized/fullscreen restoration, workspace isolation, pointer-serial refusal,
client move/resize handoff, corner constraints, non-finite deltas and output
hotplug. Native tests run in the isolated Debian builder.
