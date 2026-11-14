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
docker compose logs -f engine   # "JACK ready" and "mod-host listening on 5555/5556"
docker compose logs web         # "chain loaded (10 blocks)"
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
input 2) and `io.outputs` are the outputs.

- **Option A, cable (default):** UA22 output → the radio's rear socket
  (RTTY/DATA). No `.env` changes. One card, one clock, lowest latency.
- **Option B, the radio's USB:**
  1. `cat /proc/asound/cards` with the radio connected over USB (usually `CODEC`).
  2. `.env`: `FTDX_CARD=CODEC` and `FTDX_DIR=playback` (the UA22 stays in `AUDIO_CARD`).
  3. `chain.yaml`: `outputs: ["ftdx_out:playback_1", "ftdx_out:playback_2"]`.
  4. `docker compose up -d --force-recreate`; `docker compose logs engine | grep -i zita`.

  zita-j2a resamples between the two cards' clocks (adds a few ms).

**On the radio:** the SSB modulation source must be the rear input (REAR) and,
for option B, the rear input must be set to USB. Check the exact menu names in
the FTDX-101D manual.

**PTT:** with the microphone on the UA22, the microphone's PTT no longer reaches
the radio. Use a foot switch on the rear PTT socket, VOX, or PTT over CAT/USB
(RTS/DTR).

---

## 4. Using the UI

### Chain
- **Block strip** (like in Waveform): click a block to see its parameters; the
  ⏻ button switches that block on/off (bypass).
- **Change the order**: drag a block by its **⋮⋮** handle to another position,
  or use **◀ ▶** in the block panel. Only the JACK connections that change are
  redone (a few milliseconds of silence).
- **Add / remove**: **+** at the end of the strip opens the library of all
  installed LV2 plugins (search by name, author or type); the plugin is inserted
  after the selected block. **Remove** in the panel takes the block out of the
  chain. The chain is mono: stereo plugins get the same signal on both inputs
  and only the left output continues; sidechain inputs are left unconnected.
- **A/B · Bypass all**: switches the whole chain off to compare with the
  original sound.
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
  (e.g. "SSB DX", "SSB ragchew"). Older presets still load.
- **Reset values** (on a block) goes back to the `chain.yaml` values, or to the
  plugin defaults for blocks added from the UI. **Factory chain** restores the
  whole chain from `chain.yaml`.
- The state is saved automatically to `data/state.json` and restored when the
  engine restarts. Several browsers at the same time stay in sync.

### Plugins (packages on demand)
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
Lists the audio interfaces (via `aplay -l`/`arecord -l`) and shows the ALSA
mixer of the selected card (by default the chain's card). The UA22 has no
software-controllable gains (it only exposes "MIDI Input Mode"): input gain and
volume are set with the card's own knobs. The FTDX-101D USB CODEC, for example,
has `PCM` and `Mic`.

## 5. Common adjustments

| I want to… | Where |
|---|---|
| Change the order / add / remove blocks | in the UI (Chain tab) |
| Change the factory chain | `config/chain.yaml` → `docker compose restart web` → *Factory chain* in the UI |
| Get more plugins | Plugins tab (or `docker compose exec web ls /data/lv2/bundles`) |
| Lower the latency | `.env`: `PERIOD=128` (watch the log for XRuns; USB cards usually need `NPERIODS=3`) |
| See errors | `docker compose logs -f engine web` |
| Test Python/plugins in the image | `docker compose run --rm --no-deps web python3 -c "import lilv; print('ok')"` |
| Update | `git pull && docker compose build && docker compose up -d` |

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
running: `docker compose logs -f -t engine | grep -i xrun`. Bursts at a regular
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
