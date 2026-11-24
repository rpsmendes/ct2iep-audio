#!/usr/bin/env python3
"""JACK -> Zeus (Square SDR) TX audio over TCI.

Started by engine.sh for the virtual output "ZEUS 0" (Card tab -> Output). It is
a JACK client called out_ZEUS_0 with the same ports as a zita output bridge
(playback_1, playback_2), so the web backend wires the chain to it like any
other output. The left channel (the chain is mono; "Left channel only" mutes
the right one) is sent to Zeus as TCI TX audio: 48 kHz float32 mono, 960
samples (20 ms) per binary frame, 64-byte TCI stream header (stream type 2,
TX audio).

The bridge never keys the radio: it does not send trx/MOX commands. Zeus uses
the audio only while it is in TX, whoever keyed it, which needs the
ZEUS_TCI_TX_AUDIO=always patch in Zeus (ct2iep-hl2remote). Outside TX Zeus
drops the frames.

Status for the UI: one line in <state file>, "running", "starting ..." or
"failed <reason>"; engine.sh copies it into bridges.status.
"""
import asyncio
import os
import struct
import sys
import time

import aiohttp
import jack
import numpy as np

NAME = sys.argv[1] if len(sys.argv) > 1 else "out_ZEUS_0"
STATE = sys.argv[2] if len(sys.argv) > 2 else f"/tmp/bridges/{NAME}.state"
URL = os.environ.get("ZEUS_TCI_URL", "ws://zeus:40001").strip() or "ws://zeus:40001"
RATE = 48000
BLOCK = 960                       # 20 ms, Zeus' own TX block size
HEADER = struct.Struct("<8I32x")  # receiver, rate, sample type, codec, crc, length, stream type, reserved
FLOAT32, TX_AUDIO = 3, 2
RETRY_S = 3
# Silence sent at key-down so Zeus has a lead of audio: its TX IQ ring has no
# prefill and with a frame arriving "just in time" every 20 ms it ran empty
# between blocks (micro-cuts). Needs the MOX broadcast of the Zeus patch
# (trx:0,true;); without it nothing is sent and nothing changes.
try:
    PREFILL_MS = max(0, min(300, int(os.environ.get("ZEUS_TCI_PREFILL_MS", "100"))))
except ValueError:
    PREFILL_MS = 100
PREFILL_FRAMES = -(-PREFILL_MS * RATE // 1000 // BLOCK)   # round up to whole 20 ms frames


def frame(samples):
    hdr = HEADER.pack(0, RATE, FLOAT32, 0, 0, len(samples), TX_AUDIO, 0)
    return hdr + samples.astype("<f4", copy=False).tobytes()


def set_state(text):
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            f.write(text + "\n")
        os.replace(tmp, STATE)
    except OSError:
        pass
    print(f"[zeus-tci] {text}", flush=True)


def main():
    client = jack.Client(NAME, no_start_server=True)
    if client.samplerate != RATE:
        set_state(f"failed JACK runs at {client.samplerate} Hz, Zeus TCI needs {RATE} Hz")
        sys.exit(1)
    left = client.inports.register("playback_1")
    client.inports.register("playback_2")        # accepted, not sent (mono chain)
    rb = jack.RingBuffer(4 * RATE)               # 1 s of float32

    @client.set_process_callback
    def process(frames):                          # JACK RT thread: copy only
        rb.write(left.get_buffer())

    with client:
        asyncio.run(pump(rb))


async def pump(rb):
    need = BLOCK * 4
    connected_once = False
    while True:
        set_state(f"starting connecting to {URL}")
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(URL, heartbeat=5, max_msg_size=0) as ws:
                    # heartbeat: ping every 5 s, a dead Zeus is noticed in ~8 s
                    await ws.send_str("audio_stream_channels:1;")
                    rb.read(rb.read_space)            # start with fresh audio
                    set_state("running")
                    if not connected_once:
                        print(f"[zeus-tci] connected to {URL}", flush=True)
                    connected_once = True
                    keyed = asyncio.Event()
                    reader = asyncio.create_task(drain(ws, keyed))
                    try:
                        while not reader.done():
                            if keyed.is_set():
                                keyed.clear()
                                silence = np.zeros(BLOCK, dtype="<f4")
                                for _ in range(PREFILL_FRAMES):
                                    await asyncio.wait_for(ws.send_bytes(frame(silence)), 2)
                            while rb.read_space >= need:
                                buf = np.frombuffer(rb.read(need), dtype="<f4")
                                await asyncio.wait_for(ws.send_bytes(frame(buf)), 2)
                            await asyncio.sleep(0.005)
                    finally:
                        reader.cancel()
                    set_state(f"failed Zeus closed the TCI connection ({URL})")
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            set_state(f"failed Zeus TCI not reachable at {URL}: {e.__class__.__name__}. "
                      "Is TCI on in Zeus (Settings, bind 0.0.0.0, port 40001)?")
        # while disconnected keep the ring empty, retry
        t_end = time.monotonic() + RETRY_S
        while time.monotonic() < t_end:
            rb.read(rb.read_space)
            await asyncio.sleep(0.1)


async def drain(ws, keyed):
    """Read what Zeus sends until it closes; only MOX on (trx:0,true;) matters."""
    async for msg in ws:
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            break
        if msg.type == aiohttp.WSMsgType.TEXT:
            for cmd in msg.data.split(";"):
                if cmd.strip().lower().replace(" ", "") == "trx:0,true":
                    keyed.set()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except jack.JackError as e:
        set_state(f"failed JACK: {e}")
        sys.exit(1)
