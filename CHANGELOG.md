# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Noise suppression (RNNoise)**: werman/noise-suppression-for-voice LV2
  (mono) built in a separate Docker stage (pinned commit; the image is still
  built if this step fails). Added to the Broadcast SSB presets after the
  high-pass, **off** by default. JUCE plugins with numeric patch parameters are
  no longer classified as "needs a file".
- Numeric LV2 patch parameters (atom messages, e.g. RNNoise's voice detection
  threshold, hold and pre-roll) are shown and set like ordinary controls
  (mod-host `patch_set`).
- Blocks can carry a short description (`info`) and per-control help
  (`help`), shown in the block's panel: every factory block (`chain.yaml`) and
  the Broadcast SSB blocks (high-pass, phase rotator, leveler, multiband
  compressor, noise suppression) now explain what they do and how to set
  their main controls.
- **Test tone** (Chain tab, under the Output trim): 1 kHz or two-tone
  700 + 1900 Hz at the limiter's −1 dBFS peak, instead of the chain and before
  the Output trim, to set the level into the radio by its power/ALC meters and
  to check its intermodulation. LSP Oscillator in mod-host, header chip TONE,
  off by itself after 60 s.
- **Broadcast SSB** built-in presets (own group in the preset list): Deep,
  Punch and Warm, each in 3, 4 and 5 kHz versions, for the FTDX-101D and the
  Square SDR (Zeus). Steep high-pass, phase rotator, look-ahead leveler, EQ,
  bass enhancer, 4-band compressor, de-esser, steep low-pass and a look-ahead
  limiter at −1 dBFS: deep, dense bass without clipping. Radio settings and mic
  technique in the README and `config/presets.yaml`.
- Built-in presets can have a `group` (shown as a separate group in the preset
  list), and blocks that are not in `chain.yaml` can set their main controls,
  control `labels` and meters. Blocks with several gain-reduction meters show
  them with their labels.
- **Zeus · Square SDR (TCI)** output (Card tab → *Output*): sends the chain's
  audio to Zeus as TCI TX audio (`app/zeus_tci.py`, JACK client `out_ZEUS_0`,
  48 kHz mono float32), to transmit with the UA22 mic while Zeus is operated
  from any browser. Never keys the radio; reconnects by itself, state shown in
  the UI. `ZEUS_TCI_URL` (default `ws://zeus:40001`, empty hides it). Needs
  `python3-jack-client` (image rebuild) and the Zeus patch from ct2iep-hl2remote.
- Zeus output: sends `ZEUS_TCI_PREFILL_MS` (default 100 ms) of silence when Zeus
  reports MOX on (`trx:0,true;`, needs the updated Zeus patch), so Zeus keeps a lead
  of audio; without it the air audio had micro-cuts.
- **Input trim and Output trim**: fixed gain stages before and after the chain
  (−36 to +12 dB, Calf Mono Input), always active, also with A/B, and not part
  of presets (stored in `data/levels.json`). Each shows peak meters before and
  after the trim with a CLIP light, so card clipping and the level sent to the
  radio are visible. *Left channel only* sends the (mono) chain only to the
  left channel of the outputs.
- **Output selector** (Card tab → *Output*): choose which playback devices
  receive the processed audio, detected automatically, several at once. The
  main card is JACK's own device; any other one (e.g. the FTDX-101D USB codec)
  is fed through a zita-j2a resampling bridge that the engine starts, stops and
  retries (card unplugged) by itself, with its state shown in the UI. Stored in
  `data/engine/outputs.txt`. Requires `docker compose up -d` (new volume for
  the engine).
- **Built-in presets** for different transmission modes (`config/presets.yaml`,
  read-only in the UI, with a description and the matching radio settings):
  SSB DX / Contest, SSB Ragchew, ESSB Broadcast (wide), AM Broadcast (4.5 kHz),
  FM Narrowband and Natural (minimal processing, also for FreeDV). They reuse
  the factory blocks and only change the values that matter for each mode.
- **Plugin browser with pages**: the **+** dialog and a new *Installed plugins*
  section in the Plugins tab list every LV2 plugin with search, type and source
  filters (in the image / installed from the UI) and pages of 20/50/100.

### Changed
- **Broadcast SSB** presets: gate at −40 dB with look-ahead and a 200 Hz
  detector high-pass (room noise now ~55 dB below the voice in the pauses on the
  test, was ~20 dB); less 250–400 Hz (deeper mud cuts, less bass-enhancer
  harmonics, lower 160–800 Hz band makeup); a low-amount Calf Exciter after the
  multiband compressor for clarity.
- **Same look as the other ct2iep-* apps** (ct2iep-amp, ct2iep-station): same
  header (brand, state chips, *Keep screen on* and *Full screen* buttons, tabs
  after a divider; icons only on phones), same colour tokens, every area in a
  card with a title line (dot, name, summary) and tiles inside, same toast
  (green, red for errors) and a footer line. Header chips: **ENGINE ON / OFF /
  NO LINK**, **BYPASS** while A/B is on, **CLIP** (blinking) while the card input
  or the output to the radio clips.
- **Plugin lists show only the plugins usable in the chain** by default
  (~70 of ~300): meters/analysers, sidechain, file-based, multi-input, MIDI,
  generator and stereo-only plugins, and stereo versions of plugins that exist
  in mono, are hidden; *All plugins* shows them with the reason. Classified from
  the LV2 metadata (`lv2meta._categorize`); checked on 2026-11-25 against every
  plugin of the image (all load in mod-host; meters leave the signal unchanged).
  Package cards show "N of M plugins usable in the chain".
- **Blocks in a grid instead of a horizontal strip**: they wrap in rows (two
  columns on a phone), numbered in signal order; each block shows its own
  meters (GR, input/output level) instead of a separate row of meters; dragging
  by ⋮⋮ works across rows; on a phone, choosing a block scrolls to its panel.
- `/static/` files (CSS, JS) are sent with `Cache-Control: no-cache`: browsers
  revalidate them on every load, so a new UI shows up after an update without a
  forced reload.
- **Compose services renamed** `engine` → `audio-engine` and `web` →
  `audio-web` (same as the container names), so this file can be included in a
  central compose file with other stacks. Commands such as
  `docker compose logs web` become `docker compose logs audio-web`.
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
- **Gate never closed again once open**: its hysteresis threshold (`ht`) is
  relative to the open threshold, and the factory chain had −56 dB, so it only
  closed below −106 dBFS and let fans and power supplies through in the pauses.
  Now −6 dB (factory chain and Broadcast SSB presets).
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
