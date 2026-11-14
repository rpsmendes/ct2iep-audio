"""Frequency response of a plugin, measured with the plugin itself.

An impulse is run through the plugin offline with the current parameters and
the magnitude is computed with an FFT, so the curve shown is the real one for
any filter type/mode, without reimplementing the formulas. Only meaningful for
linear plugins (EQs and filters).

The measurement runs in measure_host.py, a minimal LV2 host in a separate,
lowest-priority process: a plugin that crashes cannot take the web service
down, and the work never competes with the real-time audio engine.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

import numpy as np

log = logging.getLogger("audio-web")

SR = 48000
N = 32768                     # ~0.7 s: covers the latency of the LSP FIR/FFT modes
POINTS = np.geomspace(20, 20000, 240)
HOST = Path(__file__).with_name("measure_host.py")
_cache = OrderedDict()
BROKEN = set()                # plugins that cannot be measured (do not retry)
_sem = asyncio.Semaphore(1)


def _key(uri, params):
    raw = json.dumps([uri, sorted((k, round(float(v), 6)) for k, v in params.items())])
    return hashlib.sha1(raw.encode()).hexdigest()


async def measure(uri: str, params: dict, bundle: str = "") -> list:
    """Magnitude (dB) at POINTS for the plugin with these parameters.

    bundle = the plugin's bundle directory: the host then only loads that
    bundle instead of scanning every installed plugin."""
    if "lsp-plug.in" in uri and "equalizer" in uri and "mode" in params:
        # LSP FIR/FFT/SPM modes have the same magnitude as IIR (only the phase
        # differs) and need more blocks to settle: measure in IIR
        params = dict(params, mode=0)
    k = _key(uri, params)
    if k in _cache:
        _cache.move_to_end(k)
        return _cache[k]
    req = json.dumps({"uri": uri, "params": params, "rate": SR, "n": N,
                      "points": [float(f) for f in POINTS]})
    async with _sem:
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ)
            if bundle:
                lv2dir = os.path.join(d, "lv2")
                os.mkdir(lv2dir)
                os.symlink(bundle.rstrip("/"), os.path.join(lv2dir, os.path.basename(bundle.rstrip("/"))))
                env["LV2_PATH"] = lv2dir
            # lowest CPU priority: never compete with the audio engine
            p = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", sys.executable, str(HOST),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
            try:
                out, err = await asyncio.wait_for(p.communicate(req.encode()), 20)
            except asyncio.TimeoutError:
                p.kill()
                BROKEN.add(uri)
                raise RuntimeError("the measurement timed out")
    try:
        res = json.loads(out.decode() or "{}")
    except ValueError:
        res = {}
    if p.returncode != 0 or "db" not in res:
        # crash (e.g. a segfault in the plugin) or a plugin the host cannot
        # run: stop showing a graph for it
        BROKEN.add(uri)
        why = res.get("error") or (f"the plugin crashed (exit code {p.returncode})" if p.returncode
                                   else err.decode(errors="replace").strip()[-300:] or "no result")
        raise RuntimeError(why)
    _cache[k] = res["db"]
    while len(_cache) > 256:
        _cache.popitem(last=False)
    return res["db"]


def freqs() -> list:
    return [round(float(f), 2) for f in POINTS]
