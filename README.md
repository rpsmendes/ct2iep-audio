# ct2iep-audio
SSB transmit audio processing chain on a headless Debian mini PC: LV2 plugins on JACK/mod-host, web UI, Docker Compose. Built for a Yaesu FTDX-101D (CT2IEP).

TX audio chain for the FTDX-101D, equivalent to the chain previously used in
Waveform, running on a Linux server without a desktop and controlled from a
browser on the local network.

```
 Microphone ─► external card (input) ─► JACK ─► [LV2 chain in mod-host] ─► external card (output) ─► FTDX-101D
                                                                       └► (optional) FTDX USB via zita-j2a
 Browser (LAN) ◄──► web UI :8080 ◄──► mod-host (commands/meters)
```

Everything runs in Docker Compose. The Debian host only needs the kernel/ALSA
and Docker.

See [CHANGELOG.md](CHANGELOG.md) for the history of changes.

---

## 1. Plugins and tools (inside the image)

| In Waveform | Here (open source, LV2) | Initial state |
|---|---|---|
| C1 gate | **LSP Gate Mono** | on |
| MV360 | *(was switched off)* | — |
| RBass | **Calf Bass Enhancer** | bypassed |
| PuigTec MEQ5 + F6 + OneKnob Brighter | **LSP Parametric Equalizer x8 Mono** (Butterworth HPF 120 Hz, −3 dB @ 300 Hz, +3 dB @ 1.8 kHz, high shelf 2.4 kHz) | on |
| GW ToneCentric (dynamics) | **LSP Compressor Mono** | on |
| GW ToneCentric (colour) | **Calf Saturator** | bypassed |
| Manny M Tone Shaper | *(was switched off)* | — |
| La Petite Excite | **Calf Exciter** | on |
| DeEsser | **Calf Deesser** | on |
| Abbey Road Plates | **Dragonfly Plate Reverb** | bypassed |
| *(new)* | **LSP Filter Mono**: Butterworth low-pass at 2.9 kHz (SSB bandwidth) | on |
| L1 limiter | **LSP Limiter Mono**: ceiling −3 dBFS, 2 ms lookahead | on |
| PAZ Analyzer | meters in the UI: input, gate, comp, de-ess, limiter, output | — |

Tools: **jackd2** (audio server), **mod-host** (LV2 host controlled over TCP,
built from the MOD Audio source), **zita-ajbridge** (bridge to a second card),
**alsa-utils** (mixer and card list). Debian packages: `lsp-plugins-lv2`, `calf-plugins`, `x42-plugins`,
`dragonfly-reverb-lv2`. Any other installed LV2 plugin can be added from the UI
or to `config/chain.yaml`.

The initial values are a starting point for SSB; tune them by ear.

---

## 2. Prepare Debian (once)

**Do not install PipeWire or PulseAudio on the host**: the container's JACK
needs exclusive access to the sound card.

```bash
# list the cards and find the name to use in AUDIO_CARD
cat /proc/asound/cards
sudo apt install -y alsa-utils && aplay -l && arecord -l

# disable USB autosuspend and set the CPU governor to performance
sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 usbcore.autosuspend=-1"/' /etc/default/grub
sudo update-grub
sudo apt install -y linux-cpupower
( sudo crontab -l 2>/dev/null; echo '@reboot /usr/bin/cpupower frequency-set -g performance' ) | sudo crontab -
sudo reboot
```

---

## 3. Install and start

On the CT2IEP server, Docker projects live in `/home/opt` (inside `/home`, so
they survive an OS reinstall) and Docker's data lives in `/home/docker`.

```bash
# on the server, once
sudo mkdir -p /home/opt && sudo chown "$USER:$USER" /home/opt

# get the project
git clone https://github.com/rpsmendes/ct2iep-audio.git /home/opt/ct2iep-audio
cd /home/opt/ct2iep-audio
cp .env.example .env
nano .env                 # AUDIO_CARD and WEB_PASSWORD (see below)

docker compose build      # the first build takes a few minutes (compiles mod-host)
docker compose up -d
docker compose logs -f audio-engine   # "JACK ready" and "mod-host listening on 5555/5556"
docker compose logs audio-web         # "chain loaded (10 blocks)"
```

UI: **http://&lt;server-ip&gt;:8080** (user `ct2iep` and the password from `.env`).

To update after changes in the repository:

```bash
cd /home/opt/ct2iep-audio && git pull && docker compose build && docker compose up -d
```

### The `.env` file
Local configuration of the installation; Compose passes these values to the
containers. To get started you only need:

- **`AUDIO_CARD`**: name of the microphone's card, between `[ ]` in
  `cat /proc/asound/cards`. At CT2IEP it is **`UA22`**. The name does not
  change if USB devices are plugged in a different order; the number can.
- **`WEB_PASSWORD`**: password for the UI.

Rules: one variable per line, `NAME=value`, **no comments on the same line**
(Compose may read the comment as part of the value). Leave `JACK_EXTRA=` empty
unless needed. After changing `.env`: `docker compose up -d`.

### Audio connections to the FTDX-101D
In `config/chain.yaml`, `io.inputs` is the microphone channel
(`system:capture_1` = UA22 input 1; use `capture_2` if the microphone is on
input 2) and `io.outputs` are the main card's output ports. **Which cards
receive the processed audio is chosen in the UI** (Card tab → *Output*).

- **Option A, cable (default):** UA22 output → the radio's rear socket
  (RTTY/DATA). No `.env` changes. One card, one clock, lowest latency.
- **Option B, the radio's USB:** connect the radio over USB; in the UI, Card
  tab → *Output*, tick the radio's card (usually `CODEC`) and untick the UA22
  if you do not want both. The UA22 stays the clock master (`AUDIO_CARD`); the
  radio's card is fed through a zita-j2a bridge that resamples between the two
  clocks (a few ms more latency). The bridge is started by the engine
  (`docker compose logs audio-engine | grep bridge`) and restarted if the radio is
  switched off and on again.

- **Option C, Zeus (Square SDR) over TCI:** Card tab → *Output*, tick
  *Zeus · Square SDR (TCI)*. The chain's (left channel) audio goes to Zeus as TCI
  TX audio over the station's Docker network (`ZEUS_TCI_URL`, default
  `ws://zeus:40001`; 48 kHz mono float32, 20 ms frames), so you can transmit with
  the UA22 mic while operating Zeus from any browser. The bridge
  (`app/zeus_tci.py`, JACK client `out_ZEUS_0`) never keys the radio: Zeus uses
  the audio whenever it is in TX, and ignores the browser mic meanwhile. Needs the
  `ZEUS_TCI_TX_AUDIO` patch and TCI enabled in Zeus (bind 0.0.0.0, port 40001):
  see ct2iep-hl2remote, `docs/hl2remote.md`, *TCI TX audio from ct2iep-audio*.
  At key-down it sends `ZEUS_TCI_PREFILL_MS` (default 100 ms) of silence so Zeus
  keeps a lead of audio (without it: micro-cuts on the air).
  Untick it when operating Zeus away from the station mic.

**On the radio:** the SSB modulation source must be the rear input (REAR) and,
for option B, the rear input must be set to USB. Check the exact menu names in
the FTDX-101D manual.

**PTT:** with the microphone on the UA22, the microphone's PTT no longer reaches
the radio. Use a foot switch on the rear PTT socket, VOX, or PTT over CAT/USB
(RTS/DTR).

---

## 4. Using the UI

The UI follows the same layout and colours as the other ct2iep-* apps
(ct2iep-amp, ct2iep-station). The header shows the engine state (**ENGINE ON**),
**BYPASS** while A/B is on and a blinking **CLIP** while the card input or the
output to the radio clips; *Keep screen on* and *Full screen* work as in
ct2iep-amp (keep screen on needs HTTPS on Android: open it through
ct2iep-station, port 7080).

### Chain
- **Input trim / Output trim** (top of the tab): fixed gain stages before and
  after the chain (−36 to +12 dB), with peak meters before and after each trim
  and a CLIP light. They stay active with **A/B** and are not part of presets
  (they match the hardware; stored in `data/levels.json`). How to set them:
  1. card input gain knob so the *Card* meter peaks around −12…−6 dB and never
     shows CLIP (a digital trim cannot undo clipping in the card);
  2. *Output trim* down until the radio's ALC barely moves on voice peaks. A
     radio's USB input usually needs far less than digital full scale: −15 dB
     or more is normal.
  The chain is mono and goes to both channels of each output. If the radio adds
  both channels of its USB input, **Left channel only** lowers the level by
  about 6 dB.
- **Blocks**: a grid that wraps (no horizontal scrolling; two columns on a
  phone). The number on each block is its place in the signal path. Each block
  shows its own meters (gain reduction **GR**, input/output level). Click a
  block to see its parameters (on a phone the page scrolls to them); the ⏻
  button switches that block on/off. A switched-off block is unloaded from the
  engine and the signal goes around it, so it uses no CPU.
- **Change the order**: drag a block by its **⋮⋮** handle to another position
  (also to another row),
  or use **◀ ▶** in the block panel. Only the JACK connections that change are
  redone (a few milliseconds of silence).
- **Add / remove**: **+** at the end of the strip opens the library of all
  installed LV2 plugins (search by name, author, type or package, filter by
  type and source, browse page by page); the plugin is inserted after the
  selected block. The same browser is in the Plugins tab. **Remove** in the panel takes the block out of the
  chain. The chain is mono: stereo plugins get the same signal on both inputs
  and only the left output continues; sidechain inputs are left unconnected.
- **A/B · Bypass all**: takes every plugin out (unloaded, no CPU use) and
  connects the input straight to the output, to compare with the original
  sound. Switching back loads the plugins again in a fraction of a second.
- **Sliders**: drag to adjust (clicking does not change the value, so there are
  no jumps while transmitting). **Shift** or **Fine** = 10× finer. On a focused
  slider: arrow keys (gains ±0.5 dB, ±0.1 dB with Shift), PageUp/PageDown,
  Home/End. Double click restores the default; the box next to it takes an
  exact value. Times and frequencies use a logarithmic scale.
- **EQ graph**: EQs and filters show their frequency response (solid line =
  this block; dashed = sum of all active EQs/filters), with the typical SSB band
  shaded. The curve is computed by the plugin itself (an impulse processed
  offline by a small built-in LV2 host, `app/measure_host.py`, in a separate
  low-priority process), so it is exact for every filter type and mode. Works
  with LSP, Calf and x42 EQs; plugins that cannot be measured show a note.
- **Presets**: *Save as…* stores the complete chain: order, blocks and values
  (e.g. "SSB DX", "SSB ragchew"). Older presets still load. The list also has
  **built-in presets** (read-only; load one, adjust it and save your own copy),
  each with a description and the matching radio settings:

  | Preset | Audio band (EQ + low-pass) | Character | On the radio |
  |---|---|---|---|
  | SSB DX / Contest | ~250 Hz – 2.6 kHz, +7 dB at 2.1 kHz | punchy, heavy compression | TX BPF 300-2700 or 400-2600 |
  | SSB Ragchew | ~120 Hz – 2.9 kHz | natural, light compression | TX BPF 100-2900 or 200-2800 |
  | ESSB Broadcast (wide) | ~60 Hz – 3.2 kHz | "presenter", warm + presence | TX BPF 50-3050 (or TTBF) |
  | AM Broadcast (4.5 kHz) | ~80 Hz – 4.5 kHz | full, dense, 9 kHz channel | widest AM TX audio |
  | FM Narrowband | ~300 Hz – 3 kHz, flat | clean, no treble boost (pre-emphasis) | normal deviation |
  | Natural (minimal processing) | 80 Hz – full band | almost transparent | reference / FreeDV |

  They are starting points: adjust them by ear on a second receiver or a
  WebSDR, and turn the radio's own speech processor and TX EQ off so they do
  not stack. Their definitions (and the sources of the curves) are in
  `config/presets.yaml`.

  A second group, **Broadcast SSB**, has three characters, each in 3, 4 and
  5 kHz versions (same chain, the EQ top end, de-esser and low-pass follow the
  bandwidth). The chain: 48 dB/oct high-pass → soft gate → phase rotator
  (x42, 120°, makes the voice peaks symmetric) → look-ahead leveler (AGC) →
  EQ → bass enhancer → 4-band compressor (160 / 800 / 2400 Hz) → de-esser →
  steep low-pass → look-ahead limiter at −1 dBFS (2× oversampled).
  Needs the LSP, Calf and x42 plugins of the image.

  | Preset | Character | Bass |
  |---|---|---|
  | Broadcast Deep | big, round voice; mud (280 Hz) and box (650 Hz) cut | shelf at 90 Hz + harmonics, slow bass band |
  | Broadcast Punch | forward and dense, cuts through the band | tight bump at 140 Hz, high-pass 20 Hz higher, faster bands |
  | Broadcast Warm | soft, relaxed, lightest compression | wide shelf at 160 Hz + light harmonics |

  Measured on a test voice: peak to loud-speech RMS from 17 dB (raw) to
  12–14 dB, i.e. 3–5 dB more talk power at the same peak power; the limiter
  works ~2 % of the time, out-of-band energy −45 to −55 dB.

  | Bandwidth | FTDX-101D | Square SDR (Zeus) |
  |---|---|---|
  | 3 kHz | SSB TX BPF 50-3050 | Settings → TX Filter 50-3000 |
  | 4 kHz | only with the TTBF option (50 Hz – 4 kHz) | TX Filter 50-4000 |
  | 5 kHz | not possible | TX Filter 50-5000 (only where the band has room) |

  On the radio: speech processor, TX EQ and (Zeus) leveler / compressor / CFC
  off; set the level with the **Output trim** until the ALC barely moves.
  With a dynamic mic (e.g. FDUCE SL40X) the depth comes from the proximity
  effect: 3–5 cm, always at the same distance, slightly off-axis, with a
  pop filter.
- **Reset values** (on a block) goes back to the `chain.yaml` values, or to the
  plugin defaults for blocks added from the UI. **Factory chain** restores the
  whole chain from `chain.yaml`.
- The state is saved automatically to `data/state.json` and restored when the
  engine restarts. Several browsers at the same time stay in sync.

### Plugins (packages on demand)

**Which plugins are offered.** The chain is one mono voice path, so by default
the plugin lists (the **+** dialog and *Installed plugins*) only show plugins
that can do something useful in it: voice processing (EQ, filters, dynamics,
de-esser, exciter, saturation, limiter…) and creative effects (reverb, delay,
modulation). *All plugins* in the list's filter shows the rest, each with the
reason it is not offered:

| Reason | Examples |
|---|---|
| visual only (meter / analyser) | x42 meters, LSP spectrum analyzer, tuner: the sound passes unchanged and the display needs the plugin's own GUI |
| needs a sidechain signal | LSP *Sidechain* …, Calf Sidechain …: the chain leaves that input empty |
| needs a file | convolvers, impulse reverbs, Room Builder, Profiler |
| needs several inputs / stereo utility | mixers, A/B tester, crossovers, Haas, balance, delay lines |
| generator, MIDI, instrument | noise generator, oscillator, trigger, samplers |
| stereo version: use the mono one | LSP/x42 *Stereo*, *LeftRight*, *MidSide* versions when a mono one exists |

The rules are in `app/lv2meta.py` (`_categorize`) and use the LV2 metadata, so
packages installed from the UI are classified too. Each package card shows how
many of its plugins are usable in the chain.
The **Plugins** tab installs and removes Debian packages of LV2 plugins
**without rebuilding the image**: a suggested catalog for voice (ZAM, SWH, MDA,
FOMP, EQ10Q, Invada, abGate) plus a field for any other package. The package is
downloaded from the Debian repository and only its LV2 bundles are copied to
`data/lv2/bundles` (persistent; libraries they need go to `data/lv2/lib`). After
installing, use **+** in the Chain tab to add the new plugins.

- Installing or removing restarts mod-host (~2 s without audio; JACK keeps
  running): **do not do it while transmitting**. The chain reloads by itself.
- A package whose plugins are in use in the chain cannot be removed.
- If a package does not exist in Debian 13, the UI says so straight away; the
  log of the operation is shown at the bottom.

### Card
**Output**: tick the playback devices that receive the processed audio
(detected automatically; several at once is fine). The main card is JACK's own
device; any other one goes through a resampling bridge, with its state shown
next to it (running / failed with the reason, e.g. card unplugged; it is
retried every 10 s). *Zeus · Square SDR (TCI)* is not a card but a virtual
output (TX audio to Zeus over TCI, option C above): *connected* / *not
connected* with the reason; hidden when `ZEUS_TCI_URL=` is empty in `.env`. The choice is stored in `data/engine/outputs.txt` and is
not part of presets.

The tab also lists the audio interfaces (via `aplay -l`/`arecord -l`) and shows the ALSA
mixer of the selected card (by default the chain's card). The UA22 has no
software-controllable gains (it only exposes "MIDI Input Mode"): input gain and
volume are set with the card's own knobs. The FTDX-101D USB CODEC, for example,
has `PCM` and `Mic`.

### Running it together with other stacks
The services are named `audio-engine` and `audio-web` so this file can be
included in a central compose file, e.g. `/home/opt/compose.yaml`:

```yaml
name: station
include:
  - ct2iep-audio/docker-compose.yml
  - portainer/compose.yaml
```

Relative paths (`./data`, `./config`, `.env`) keep resolving from this
folder. Then everything is managed from `/home/opt` with `docker compose ...`.

## 5. Common adjustments

| I want to… | Where |
|---|---|
| Change the order / add / remove blocks | in the UI (Chain tab) |
| Too much level into the radio (distortion, ALC) | *Output trim* in the Chain tab (also works with A/B) |
| Change the factory chain | `config/chain.yaml` → `docker compose restart audio-web` → *Factory chain* in the UI |
| Change / add built-in presets | `config/presets.yaml` → `docker compose restart audio-web` |
| Get more plugins | Plugins tab (or `docker compose exec audio-web ls /data/lv2/bundles`) |
| Lower the latency | `.env`: `PERIOD=128` (watch the log for XRuns; USB cards usually need `NPERIODS=3`) |
| See errors | `docker compose logs -f audio-engine audio-web` |
| Test Python/plugins in the image | `docker compose run --rm --no-deps audio-web python3 -c "import lilv; print('ok')"` |
| Update (also for UI changes: `app/` is copied into the image, `restart` is not enough) | `git pull && docker compose build && docker compose up -d` |

Expected latency: 3 periods × 256 samples at 48 kHz ≈ 16 ms of buffer + USB
buffers and the limiter lookahead (2 ms) → about 20–25 ms round trip, which is
not noticeable on SSB.

## 6. Troubleshooting (issues solved during installation)

| Message | Cause | Fix (already included) |
|---|---|---|
| `env: 'python3': No such file or directory` during the build | the mod-host build uses Python | `python3` in the build stage |
| `Audio device hw:UA22 cannot be acquired` / `session bus for device reservation` | JACK tries to reserve the card via D-Bus, which does not exist on a server | `JACK_NO_AUDIO_RESERVATION=1` in the image |
| `cannot set channel count to 1 for capture` | `JACK_EXTRA` containing `-i 1` (a comment read as the value); the UA22 only accepts 2 channels | empty `JACK_EXTRA=`, comments on their own lines |
| `[engine] jackd did not start` while JACK is running | in Debian 13 `jack_lsp` is in `jack-example-tools` | `jack-example-tools` package |
| `OSError: liblilv-0.so: cannot open shared object file` | `python3-lilv` looks for the unversioned library, which only ships in `-dev` | `liblilv-dev` package |

Normal messages when the engine starts: `jack_get_descriptor returns null for
'jack_intime.so'` (and similar) and **one** XRun from `mod-monitor` while the
plugins load. It is only a problem if XRuns keep happening while audio is
running: `docker compose logs -f -t audio-engine | grep -i xrun`. Bursts at a regular
interval (e.g. every ~23 s) point to USB clock drift between the card and the
PC: use a bigger buffer (`PERIOD=256`, `NPERIODS=3`, the default).

If a Dockerfile change seems to have no effect:
`docker compose build --no-cache && docker compose up -d --force-recreate`.

## 7. Notes
- The level going into the radio is set with the **output gain** (card mixer or
  the limiter's `g_out`) while watching the FTDX ALC: the limiter guarantees it
  never exceeds −3 dBFS.
- The UI uses basic authentication when `WEB_PASSWORD` is set. Do not expose it
  to the Internet without a VPN.
- Layout:
  - `docker/`: image and engine startup (`engine.sh`: JACK, zita bridges,
    mod-host, mod-host restart when plugins change)
  - `app/server.py`: API and mod-host connection; `app/lv2meta.py`: LV2
    metadata and library; `app/response.py` + `app/measure_host.py`: EQ curves; `app/packages.py`:
    plugin packages on demand
  - `app/static/`: UI (`app.js` core; `chain-edit.js`, `graph.js`,
    `plugins.js`, `card.js` one per feature)
  - `config/chain.yaml`: factory chain
  - `data/`: state, presets and plugins installed from the UI (not in Git)
