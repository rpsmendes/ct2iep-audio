#!/bin/sh
# Audio engine: jackd (main card) + optional zita bridges + mod-host.
# If jackd or a bridge dies, the container exits and Docker restarts it.
# When the UI installs/removes plugin packages it writes /data/lv2/.rescan:
# mod-host is restarted (JACK keeps running) to load the new plugins.
set -eu

: "${AUDIO_CARD:?Set AUDIO_CARD in .env (see 'aplay -l' on the host)}"
RATE="${RATE:-48000}"
PERIOD="${PERIOD:-128}"
NPERIODS="${NPERIODS:-2}"
JACK_PRIO="${JACK_PRIO:-80}"
JACK_EXTRA="${JACK_EXTRA:-}"
RESCAN_FILE="${RESCAN_FILE:-/data/lv2/.rescan}"

echo "[engine] jackd on hw:${AUDIO_CARD} ${RATE} Hz, ${PERIOD}x${NPERIODS}"
# shellcheck disable=SC2086
jackd -R -P "${JACK_PRIO}" -t 5000 -d alsa -d "hw:${AUDIO_CARD}" \
      -r "${RATE}" -p "${PERIOD}" -n "${NPERIODS}" ${JACK_EXTRA} &
JACK_PID=$!

# wait until JACK is ready
i=0
until jack_lsp >/dev/null 2>&1; do
    i=$((i+1)); [ $i -gt 50 ] && { echo "[engine] jackd did not start"; exit 1; }
    kill -0 "$JACK_PID" 2>/dev/null || { echo "[engine] jackd exited"; exit 1; }
    sleep 0.2
done
echo "[engine] JACK ready"

PIDS="$JACK_PID"

# Optional bridge to the FTDX-101D USB card (independent clock; zita does
# adaptive resampling). Ports: ftdx_out:playback_N / ftdx_in:capture_N
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
    echo "[engine] mod-host listening on 5555/5556"
}

mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }

start_modhost
LAST_RESCAN=$(mtime "$RESCAN_FILE")

while :; do
    for p in $PIDS; do
        kill -0 "$p" 2>/dev/null || { echo "[engine] process $p exited"; kill $PIDS $MH_PID 2>/dev/null || true; exit 1; }
    done
    kill -0 "$MH_PID" 2>/dev/null || { echo "[engine] mod-host exited"; kill $PIDS 2>/dev/null || true; exit 1; }
    NOW=$(mtime "$RESCAN_FILE")
    if [ "$NOW" != "$LAST_RESCAN" ]; then
        LAST_RESCAN=$NOW
        echo "[engine] plugins changed: restarting mod-host"
        kill "$MH_PID" 2>/dev/null || true
        wait "$MH_PID" 2>/dev/null || true
        start_modhost
    fi
    sleep 2
done
