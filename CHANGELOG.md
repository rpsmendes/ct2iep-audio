# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Output selector** (Card tab → *Output*): choose which playback devices
  receive the processed audio, detected automatically, several at once. The
  main card is JACK's own device; any other one (e.g. the FTDX-101D USB codec)
  is fed through a zita-j2a resampling bridge that the engine starts, stops and
  retries (card unplugged) by itself, with its state shown in the UI. Stored in
  `data/engine/outputs.txt`. Requires `docker compose up -d` (new volume for
  the engine).

### Changed
- **A/B and block bypass now really switch plugins off.** mod-host's own
  bypass keeps running the plugin every cycle, so CPU use did not drop. Now a
  bypassed block is unloaded and the signal is wired around it; A/B unloads
  every plugin and connects the input straight to the output. Switching a block
  (or the whole chain) back on loads it again in a fraction of a second, while
  the signal keeps flowing on the old path. Meters of switched-off blocks are
  greyed out.
- All repository content (code comments, UI text, configuration and
  documentation) is now written in English.

### Fixed
- **Empty EQ graph on Debian 13**: `lv2apply` crashed with the LSP plugins
  shipped by Debian 13 (1.2.21), which need their atom ports connected. The
  curve is now measured by a small built-in LV2 host (`app/measure_host.py`,
  separate low-priority process) that connects every port and provides the
  usual host features. As a bonus, Calf EQs and x42 fil4 now get a graph too.
  `lilv-utils` is no longer needed in the image.
- Bursts of XRuns every ~23 s with the UA22: USB clock drift between the card
  and the PC exhausted the 128×2 buffer. The default is now `PERIOD=256`,
  `NPERIODS=3` (as JACK recommends for USB cards); existing installations must
  change these two lines in their `.env`.
- Repeated XRuns at startup and while the EQ graph refreshes: the web
  service no longer competes with the real-time audio engine for CPU. It runs
  at a lower priority (`WEB_NICE`, default 10), the EQ measurement runs at the
  lowest priority and only loads the measured plugin's bundle, one at a time, and the plugin library is no longer scanned at startup (only when the
  **+** library is first opened).
- Plugins installed from the UI now live in `data/lv2/bundles` (libraries in
  `data/lv2/lib`, manifests in `data/lv2/packages`). Previously lilv also
  scanned the bookkeeping directories inside `data/lv2` and filled the log with
  harmless "Error loading file" messages. The old layout is migrated
  automatically at startup.

## [0.2.0] - 2026-11-14

### Added
- **Chain order**: drag a block by its ⋮⋮ handle (mouse or touch), or use the
  ◀ ▶ buttons in the block panel. Only the JACK connections that change are
  redone (a few milliseconds of silence). Presets now also store the order.
- **EQ frequency-response graph**: the curve is computed by the plugin itself
  (an impulse processed offline with `lv2apply`), so it is exact for every filter
  type and mode. The graph also shows the combined response of all active EQs
  and filters, with the typical SSB band shaded. Plugins that cannot be measured
  offline (e.g. Calf, x42 fil4) show a note instead.
- **Plugins on demand**:
  - the **+** button at the end of the chain opens a library of the ~300
    installed LV2 plugins; blocks can be added and removed;
  - the **Plugins** tab installs and removes Debian plugin packages without
    rebuilding the image (ZAM, SWH, MDA, FOMP, EQ10Q, Invada, abGate, or any
    other package by name). Missing shared libraries are resolved
    automatically. Installing or removing causes ~2 s without audio while the
    engine restarts mod-host: do not do it while transmitting.
- **Factory chain** button: restores order, blocks and values from
  `config/chain.yaml`.

### Changed
- **Sliders**: Attack and Release were on a linear scale where one pixel was
  worth ~15 ms; they now use a logarithmic scale (10 px of drag on Attack moves
  from 2.00 to 2.65 ms). Clicking no longer makes the value jump. Shift or
  "Fine" gives 10× finer control; arrow keys step ±0.5 dB on gains.
- Chain state is stored as an ordered list (`data/state.json` v2); existing
  state and presets are migrated automatically.

### Fixed
- **Card tab**: Docker hides `/proc/asound` inside containers, so the card list
  now comes from `aplay -l` / `arecord -l`. Added a card selector for the mixer
  (e.g. to see the FTDX-101D USB CODEC) and a note explaining that the UA22 has
  no software-controllable gains (only its physical knobs).
- **Factory EQ and low-pass values**: the high-pass (120 Hz) and low-pass
  (2.9 kHz) filters now use Butterworth (−3 dB at the cutoff frequency). With
  RLC x4 they cut −24 dB at the cutoff itself, and the low-pass was already
  −3.8 dB at 1 kHz, muffling the voice. The EQ bells had `q=0` (extremely wide)
  and now use `q=1`. An existing state keeps its old values: press
  **Reset values** on the EQ and Low-pass blocks (or **Factory chain**) to apply
  the fix.

## [0.1.0] - 2026-11-14

### Added
- Initial release: SSB transmit audio chain for the Yaesu FTDX-101D on a
  headless Debian mini PC. JACK + mod-host running LV2 plugins (LSP, Calf, x42,
  Dragonfly) equivalent to the previous Waveform chain, a web UI on port 8080
  (block strip, parameters, meters, presets, A/B bypass, ALSA mixer) and
  Docker Compose deployment.

### Fixed (during the first installation)
- `python3` added to the mod-host build stage.
- `JACK_NO_AUDIO_RESERVATION=1`: there is no D-Bus device reservation on a
  headless server.
- `.env.example` comments moved to their own lines (an inline comment was read
  as part of `JACK_EXTRA`, forcing 1 capture channel on the 2-channel UA22).
- `jack-example-tools` added (`jack_lsp` moved there in Debian 13).
- `liblilv-dev` added (`python3-lilv` loads the unversioned `liblilv-0.so`).
