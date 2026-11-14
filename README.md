# ct2iep-audio
SSB transmit audio processing chain on a headless Debian mini PC: LV2 plugins on JACK/mod-host, web UI, Docker Compose. Built for a Yaesu FTDX-101D (CT2IEP).

Cadeia de áudio TX para o FTDX-101D, equivalente à cadeia usada no Waveform, a correr
num servidor Linux sem ambiente gráfico e controlada pelo browser na rede local.

```
 Microfone ─► placa externa (entrada) ─► JACK ─► [cadeia LV2 no mod-host] ─► placa externa (saída) ─► FTDX-101D
                                                                         └► (opcional) USB do FTDX via zita-j2a
 Browser (LAN) ◄──► UI web :8080 ◄──► mod-host (comandos/medidores)
```

Tudo corre em Docker Compose. No Debian só ficam o kernel/ALSA e o Docker.

---

## 1. Plugins e ferramentas (dentro da imagem)

| No Waveform | Aqui (open source, LV2) | Estado inicial |
|---|---|---|
| C1 gate | **LSP Gate Mono** | ligado |
| MV360 | *(estava desligado)* | — |
| RBass | **Calf Bass Enhancer** | bypass |
| PuigTec MEQ5 + F6 + OneKnob Brighter | **LSP Parametric Equalizer x8 Mono** (HPF 120 Hz, corte 300 Hz, presença 1,8 kHz, hi-shelf 2,4 kHz) | ligado |
| GW ToneCentric (dinâmica) | **LSP Compressor Mono** | ligado |
| GW ToneCentric (cor) | **Calf Saturator** | bypass |
| Manny M Tone Shaper | *(estava desligado)* | — |
| La Petite Excite | **Calf Exciter** | ligado |
| DeEsser | **Calf Deesser** | ligado |
| Abbey Road Plates | **Dragonfly Plate Reverb** | bypass |
| *(novo)* | **LSP Filter Mono** — passa-baixos 2,9 kHz (largura de banda SSB) | ligado |
| L1 limiter | **LSP Limiter Mono** — teto −3 dBFS, lookahead 2 ms | ligado |
| PAZ Analyzer | medidores na UI: entrada, gate, comp, de-ess, limiter, saída | — |

Ferramentas: **jackd2** (servidor de áudio), **mod-host** (motor LV2 controlado por
TCP, compilado do código da MOD Audio), **zita-ajbridge** (ponte para uma 2.ª placa),
**alsa-utils** (mixer da placa). Pacotes Debian: `lsp-plugins-lv2`, `calf-plugins`,
`x42-plugins`, `dragonfly-reverb-lv2`. Qualquer outro plugin LV2 instalado pode ser
acrescentado ao `config/chain.yaml`.

Os valores iniciais são um ponto de partida para SSB; afina-os de ouvido.

---

## 2. Preparar o Debian (uma vez)

**Não instales PipeWire nem PulseAudio no host**: o JACK do contentor precisa da
placa só para ele.

```bash
# ver as placas e o nome a usar em AUDIO_CARD
cat /proc/asound/cards
sudo apt install -y alsa-utils && aplay -l && arecord -l

# desligar a suspensão USB e pôr o CPU em performance
sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 usbcore.autosuspend=-1"/' /etc/default/grub
sudo update-grub
sudo apt install -y linux-cpupower
( sudo crontab -l 2>/dev/null; echo '@reboot /usr/bin/cpupower frequency-set -g performance' ) | sudo crontab -
sudo reboot
```

---

## 3. Instalar e arrancar

Os projetos Docker do servidor ficam em `/home/opt` (dentro do `/home`, para
sobreviverem a uma reinstalação). Os dados do Docker ficam em `/home/docker`.

```bash
# no servidor, uma vez
sudo mkdir -p /home/opt && sudo chown pmendes:pmendes /home/opt

# obter o projeto
git clone https://github.com/rpsmendes/ct2iep-audio.git /home/opt/ct2iep-audio

# no servidor
cd /home/opt/ct2iep-audio
cp .env.example .env
nano .env                 # AUDIO_CARD e WEB_PASSWORD (ver abaixo)

docker compose build      # a 1.ª vez demora uns minutos (compila o mod-host)
docker compose up -d
docker compose logs -f engine   # "JACK pronto" e "mod-host a escutar em 5555/5556"
docker compose logs web         # "cadeia carregada (10 blocos)"
```

UI: **http://192.168.1.88:8080** (utilizador `ct2iep` e a password do `.env`).

### O ficheiro `.env`
É a configuração local da instalação; o Compose passa estes valores aos
contentores. Para começar só precisas de:

- **`AUDIO_CARD`**: nome da placa do microfone, entre `[ ]` em
  `cat /proc/asound/cards`. Na estação CT2IEP é **`UA22`**. O nome não muda se
  as placas USB forem ligadas por outra ordem; o número pode mudar.
- **`WEB_PASSWORD`**: password da UI.

Regras: uma variável por linha, `NOME=valor`, **sem comentários na mesma linha**
(o Compose pode ler o comentário como parte do valor). `JACK_EXTRA=` fica vazio
salvo necessidade. Depois de mudar o `.env`: `docker compose up -d`.

### Ligações de áudio para o FTDX-101D
`config/chain.yaml` → `io.inputs` é o canal do microfone (`system:capture_1` =
entrada 1 da UA22; usa `capture_2` se o microfone estiver na entrada 2) e
`io.outputs` são as saídas.

- **Opção A, cabo (por defeito):** saída da UA22 → ficha traseira do rádio
  (RTTY/DATA). Não se mexe no `.env`. Uma só placa, um só relógio, latência mais
  baixa.
- **Opção B, USB do rádio:**
  1. `cat /proc/asound/cards` com o rádio ligado por USB (normalmente `CODEC`).
  2. `.env`: `FTDX_CARD=CODEC` e `FTDX_DIR=playback` (a UA22 continua em `AUDIO_CARD`).
  3. `chain.yaml`: `outputs: ["ftdx_out:playback_1", "ftdx_out:playback_2"]`.
  4. `docker compose up -d --force-recreate`; `docker compose logs engine | grep -i zita`.

  O zita-j2a faz o resampling entre os relógios das duas placas (alguns ms a mais).

**No rádio:** a fonte de modulação em SSB tem de vir da traseira (REAR) e, na
opção B, a seleção da traseira tem de ser USB. Confirma os nomes exatos dos menus
no manual do FTDX-101D.

**PTT:** com o microfone na UA22, o PTT do microfone já não chega ao rádio. Usa
um pedal na ficha PTT traseira, VOX, ou PTT por CAT/USB (RTS/DTR).

---

## 4. Usar a UI

- **Barra de blocos** (como no Waveform): clica num bloco para ver os parâmetros;
  o botão ⏻ liga/desliga (bypass) esse bloco.
- **A/B · Bypass total**: desliga a cadeia inteira para comparar com o som original.
- **Parâmetros**: os principais aparecem logo; os restantes em *Avançado*.
  Ganhos em dB. Duplo clique num slider repõe o valor por defeito; também podes
  escrever o valor na caixa.
- **Presets**: *Guardar como…* grava o estado atual (ex.: "SSB DX", "SSB ragchew").
- **Placa**: ganhos e mutes do mixer ALSA da placa externa.
- O estado é gravado sozinho em `data/state.json` e reposto quando o motor reinicia.
  Vários browsers ao mesmo tempo ficam sincronizados.

## 5. Ajustes comuns

| Quero… | Onde |
|---|---|
| Mudar a ordem/plugins da cadeia | `config/chain.yaml` → `docker compose restart web` |
| Menos latência | `.env`: `PERIOD=64` (se houver estalos, volta a 128 ou `NPERIODS=3`) |
| Ver erros | `docker compose logs -f engine web` |
| Testar o Python/plugins na imagem | `docker compose run --rm --no-deps web python3 -c "import lilv; print('ok')"` |
| Atualizar | `docker compose build --pull && docker compose up -d` |

Latência esperada: 2 períodos × 128 amostras a 48 kHz ≈ 5,3 ms + buffers USB e
lookahead do limiter (2 ms) → cerca de 10–15 ms ida-e-volta.

## 6. Problemas resolvidos na instalação

| Mensagem | Causa | Solução (já incluída) |
|---|---|---|
| `env: 'python3': No such file or directory` no build | o build do mod-host usa Python | `python3` na fase de build |
| `Audio device hw:UA22 cannot be acquired` / `session bus for device reservation` | o JACK tenta reservar a placa via D-Bus, que não existe num servidor | `JACK_NO_AUDIO_RESERVATION=1` na imagem |
| `cannot set channel count to 1 for capture` | `JACK_EXTRA` com `-i 1` (comentário lido como valor); a UA22 só aceita 2 canais | `JACK_EXTRA=` vazio, comentários em linhas separadas |
| `[engine] jackd não arrancou` com o JACK a correr | no Debian 13 o `jack_lsp` está no `jack-example-tools` | pacote `jack-example-tools` |
| `OSError: liblilv-0.so: cannot open shared object file` | o `python3-lilv` procura a biblioteca sem versão, que só vem no `-dev` | pacote `liblilv-dev` |

Mensagens normais no arranque do motor: `jack_get_descriptor returns null for
'jack_intime.so'` (e semelhantes) e **um** XRun do `mod-monitor` enquanto os
plugins carregam. Só há problema se os XRuns continuarem com o áudio a correr:
`docker compose logs -f engine | grep -i xrun` → se repetirem, `NPERIODS=3` ou
`PERIOD=256`.

Se uma alteração ao Dockerfile parecer não ter efeito:
`docker compose build --no-cache && docker compose up -d --force-recreate`.

## 7. Notas
- O nível que entra no rádio define-se com o **ganho de saída** (mixer da placa
  ou `g_out` do limiter) olhando para o ALC do FTDX: o limiter garante que nunca
  passa de −3 dBFS.
- A UI usa autenticação básica se `WEB_PASSWORD` estiver definida. Não a exponhas
  à Internet sem passar pela VPN.
- Estrutura: `docker/` (imagem e arranque do motor), `app/` (backend Python +
  UI), `config/chain.yaml` (cadeia), `data/` (estado e presets).
