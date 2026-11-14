#!/bin/sh
# Motor de áudio: jackd (placa principal) + pontes zita opcionais + mod-host.
# Se o jackd ou uma ponte morrer, o contentor termina e o Docker reinicia-o.
# Quando a UI instala/remove pacotes de plugins, escreve /data/lv2/.rescan:
# o mod-host é reiniciado (o JACK continua) para carregar os plugins novos.
set -eu

: "${AUDIO_CARD:?Define AUDIO_CARD no .env (ver 'aplay -l' no host)}"
RATE="${RATE:-48000}"
PERIOD="${PERIOD:-128}"
NPERIODS="${NPERIODS:-2}"
JACK_PRIO="${JACK_PRIO:-80}"
JACK_EXTRA="${JACK_EXTRA:-}"
RESCAN_FILE="${RESCAN_FILE:-/data/lv2/.rescan}"

echo "[engine] jackd em hw:${AUDIO_CARD} ${RATE} Hz, ${PERIOD}x${NPERIODS}"
# shellcheck disable=SC2086
jackd -R -P "${JACK_PRIO}" -t 5000 -d alsa -d "hw:${AUDIO_CARD}" \
      -r "${RATE}" -p "${PERIOD}" -n "${NPERIODS}" ${JACK_EXTRA} &
JACK_PID=$!

# esperar que o JACK esteja pronto
i=0
until jack_lsp >/dev/null 2>&1; do
    i=$((i+1)); [ $i -gt 50 ] && { echo "[engine] jackd não arrancou"; exit 1; }
    kill -0 "$JACK_PID" 2>/dev/null || { echo "[engine] jackd terminou"; exit 1; }
    sleep 0.2
done
echo "[engine] JACK pronto"

PIDS="$JACK_PID"

# Ponte opcional para a placa USB do FTDX-101D (relógio independente,
# o zita faz resampling adaptativo). Portas: ftdx_out:playback_N / ftdx_in:capture_N
if [ -n "${FTDX_CARD:-}" ]; then
    case "${FTDX_DIR:-playback}" in
        playback|both)
            zita-j2a -j ftdx_out -d "hw:${FTDX_CARD}" -r "${RATE}" -p 256 -n 3 -c 2 &
            PIDS="$PIDS $!";;
    esac
    case "${FTDX_DIR:-playback}" in
        capture|both)
            zita-a2j -j ftdx_in -d "hw:${FTDX_CARD}" -r "${RATE}" -p 256 -n 3 -c 2 &
            PIDS="$PIDS $!";;
    esac
fi

start_modhost() {
    mod-host -n -p 5555 -f 5556 &
    MH_PID=$!
    echo "[engine] mod-host a escutar em 5555/5556"
}

mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }

start_modhost
LAST_RESCAN=$(mtime "$RESCAN_FILE")

while :; do
    for p in $PIDS; do
        kill -0 "$p" 2>/dev/null || { echo "[engine] processo $p terminou"; kill $PIDS $MH_PID 2>/dev/null || true; exit 1; }
    done
    kill -0 "$MH_PID" 2>/dev/null || { echo "[engine] mod-host terminou"; kill $PIDS 2>/dev/null || true; exit 1; }
    NOW=$(mtime "$RESCAN_FILE")
    if [ "$NOW" != "$LAST_RESCAN" ]; then
        LAST_RESCAN=$NOW
        echo "[engine] plugins alterados: a reiniciar o mod-host"
        kill "$MH_PID" 2>/dev/null || true
        wait "$MH_PID" 2>/dev/null || true
        start_modhost
    fi
    sleep 2
done
