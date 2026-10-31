# Whitecube Desktop 0.9.5

Whitecube 0.9.5 reduces heat from an idle desktop. The supported binary release is
Debian 13 (Trixie), amd64.

## Thermal improvements

- **Discrete GPU can sleep (`whitecube-session`)**: wlroots opens every GPU, which
  holds an NVIDIA GPU awake even when it drives no display. When an NVIDIA or Nouveau
  GPU has no connected display and another GPU does, the session now sets
  `WLR_DRM_DEVICES` to the other GPUs. A display plugged into the sleeping GPU appears
  after the next login. Set `gpu_power_save=0` to open every GPU. An existing
  `WLR_DRM_DEVICES`, `WLR_BACKENDS` and `--safe-mode` all leave the choice alone.
- **Stepped thermal response (`cube-ca`)**: the field runs at half speed from 65 C and
  keeps the 2 ticks/s limit from 75 C. Speeding back up takes 5 C of cooling, so the rate
  does not flutter around a threshold.
- **Idle freeze (`whitecube-compositor`, `cube-ca`)**: after 2 minutes with no input the
  compositor publishes a private `whitecube-idle` hint and the field stops simulating and
  drawing. Input resumes it within about 3 seconds. Idle inhibitors, such as a playing
  video, count as activity. An open field editor is not frozen.

`whitecube-session --check` now reports the selected `drm_devices`.

## Validation scope

Automated tests cover GPU selection, its overrides, and the thermal steps. Power draw
on real hardware depends on the machine and is not part of the test suite.
