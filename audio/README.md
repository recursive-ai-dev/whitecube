# cube-audio

Whitecube's own audio layer. It talks to the kernel sound drivers through `/dev/snd/*`
with ioctls from the kernel UAPI headers (`<sound/asound.h>`, `<sound/tlv.h>`, shipped by
`linux-libc-dev`). It links libc and libm only: **no alsa-lib, no PipeWire, no PulseAudio,
no WirePlumber.** The kernel driver (snd_hda_intel, snd-usb-audio, ...) is still what
drives the hardware; nothing in userspace sits between cube-audio and it.

## Status

| Piece | State |
|---|---|
| PCM playback (S16 stereo, hw/sw params, underrun recovery, drain, SIGINT) | done, verified on card 0 (ALC256) |
| Mixer: volume (dB-aware percent), mute, capture variants | done, verified read+write on card 0 |
| WAV player (PCM 8/16/24/32, float32, any channel count, any rate, linear resample) | done |
| `selftest` (silent 250 ms through the real hardware clock) | done |
| Mixing daemon so several programs can play at once | **not started** |
| A way for existing apps (Firefox, mpv) to reach it | **not started** |

Not verified: anything audible. Every test so far plays silence, with the Master switch muted.

## Commands

    cube-audio cards | status | volume [--input] [N|+N|-N] | mute [--input] [on|off|toggle]
    cube-audio play [-D card,dev] FILE.wav | selftest [-D card,dev]

`mute` with no argument only reports. The default device is `$CUBE_AUDIO_DEVICE` (`card,dev`)
or the first card that has both a playback volume control and a playback PCM.

## Build and test

    audio/build.sh        # builds audio/build/cube-audio, runs ASan/UBSan unit tests
    audio/build/cube-audio selftest     # needs the audio group, no root

## Design notes

* Volume percent is linear in dB over at most the top 60 dB (when the driver publishes a
  TLV), so 50% is a sensible loudness; a nonzero percent never lands on the mute step.
* Playback is always negotiated as S16_LE stereo and everything else is converted, which is
  the one format every codec accepts.
* Without a daemon only one program can open a PCM at a time (the kernel returns EBUSY).
  That is the same limit as the existing "ALSA direct" mode.

## Open decisions (see the conversation)

1. A mixing daemon (`cube-audiod`) owning the PCM, with a private socket protocol.
2. How apps reach it. Firefox only speaks PulseAudio, ALSA or JACK; a native protocol means
   either implementing a subset of the PulseAudio native protocol or an LD_PRELOAD shim.
3. Wiring `cube-settings-helper`, `cube-status` quick settings and `cube-privacy`
   (currently `amixer`/`wpctl`; `amixer` is not even installed here) to `cube-audio`.
