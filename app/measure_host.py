#!/usr/bin/env python3
"""Minimal offline LV2 host used to measure a plugin's frequency response.

Runs as a separate process (so a crashing plugin cannot take the web service
down): reads a JSON request on stdin, runs an impulse through the plugin and
prints the magnitude in dB at the requested frequencies as JSON on stdout.

Unlike lv2apply it connects *every* port (audio, control, CV and atom, the
latter with the buffer size each plugin declares) and provides the features
plugins commonly need: urid:map/unmap, options and boundedBlockLength.

Request:  {"uri": str, "params": {symbol: value}, "rate": int, "n": int,
           "block": int, "points": [Hz, ...]}
Response: {"db": [...]}  or  {"error": "..."}
"""
import ctypes
import json
import sys
from ctypes import (CFUNCTYPE, POINTER, Structure, c_char_p, c_float, c_int32,
                    c_uint32, c_void_p, cast, pointer, sizeof)

import lilv
import numpy as np

LV2 = "http://lv2plug.in/ns/"
URI = {
    "map": LV2 + "ext/urid#map",
    "unmap": LV2 + "ext/urid#unmap",
    "options": LV2 + "ext/options#options",
    "bounded": LV2 + "ext/buf-size#boundedBlockLength",
    "minBlock": LV2 + "ext/buf-size#minBlockLength",
    "maxBlock": LV2 + "ext/buf-size#maxBlockLength",
    "nominalBlock": LV2 + "ext/buf-size#nominalBlockLength",
    "seqSize": LV2 + "ext/buf-size#sequenceSize",
    "sampleRate": LV2 + "ext/parameters#sampleRate",
    "updateRate": LV2 + "extensions/ui#updateRate",
    "Int": LV2 + "ext/atom#Int",
    "Float": LV2 + "ext/atom#Float",
    "Sequence": LV2 + "ext/atom#Sequence",
    "Chunk": LV2 + "ext/atom#Chunk",
    "AtomPort": LV2 + "ext/atom#AtomPort",
    "minimumSize": LV2 + "ext/resize-port#minimumSize",
}


# ---------------------------------------------------------------- URID map
class URIDMap(Structure):
    _fields_ = [("handle", c_void_p), ("map", CFUNCTYPE(c_uint32, c_void_p, c_char_p))]


class URIDUnmap(Structure):
    _fields_ = [("handle", c_void_p), ("unmap", CFUNCTYPE(c_void_p, c_void_p, c_uint32))]


class Option(Structure):
    _fields_ = [("context", c_uint32), ("subject", c_uint32), ("key", c_uint32),
                ("size", c_uint32), ("type", c_uint32), ("value", c_void_p)]


_ids, _strings = {}, {}


def urid(uri):
    if isinstance(uri, bytes):
        uri = uri.decode()
    if uri not in _ids:
        _ids[uri] = len(_ids) + 1
        _strings[_ids[uri]] = ctypes.create_string_buffer(uri.encode())
    return _ids[uri]


@CFUNCTYPE(c_uint32, c_void_p, c_char_p)
def _map_cb(_handle, uri):
    return urid(uri)


@CFUNCTYPE(c_void_p, c_void_p, c_uint32)
def _unmap_cb(_handle, uid):
    buf = _strings.get(uid)
    return ctypes.addressof(buf) if buf is not None else None


def build_features(rate, block):
    keep = []                                   # keep ctypes objects alive
    umap = URIDMap(None, _map_cb)
    uunmap = URIDUnmap(None, _unmap_cb)
    ints = {k: c_int32(block) for k in ("minBlock", "maxBlock", "nominalBlock")}
    ints["seqSize"] = c_int32(65536)
    floats = {"sampleRate": c_float(rate), "updateRate": c_float(30.0)}
    opts = (Option * (len(ints) + len(floats) + 1))()
    i = 0
    for k, v in ints.items():
        opts[i] = Option(0, 0, urid(URI[k]), sizeof(c_int32), urid(URI["Int"]), ctypes.addressof(v))
        i += 1
    for k, v in floats.items():
        opts[i] = Option(0, 0, urid(URI[k]), sizeof(c_float), urid(URI["Float"]), ctypes.addressof(v))
        i += 1
    keep += [umap, uunmap, opts, ints, floats]
    feats = [
        lilv.LV2_Feature(URI["map"].encode(), cast(pointer(umap), c_void_p)),
        lilv.LV2_Feature(URI["unmap"].encode(), cast(pointer(uunmap), c_void_p)),
        lilv.LV2_Feature(URI["options"].encode(), cast(opts, c_void_p)),
        lilv.LV2_Feature(URI["bounded"].encode(), None),
    ]
    keep += feats
    arr = (POINTER(lilv.LV2_Feature) * (len(feats) + 1))(*[pointer(f) for f in feats], None)
    keep.append(arr)
    return arr, keep


def measure(req):
    world = lilv.World()
    world.load_all()
    plugin = world.get_all_plugins()[world.new_uri(req["uri"])]
    if plugin is None:
        raise RuntimeError("plugin not found")
    rate, n, block = int(req.get("rate", 48000)), int(req.get("n", 32768)), int(req.get("block", 512))
    params = req.get("params", {})

    n_audio = world.new_uri(lilv.LILV_URI_AUDIO_PORT)
    n_ctrl = world.new_uri(lilv.LILV_URI_CONTROL_PORT)
    n_cv = world.new_uri(lilv.LILV_URI_CV_PORT)
    n_in = world.new_uri(lilv.LILV_URI_INPUT_PORT)
    n_atom = world.new_uri(URI["AtomPort"])
    n_min = world.new_uri(URI["minimumSize"])

    feats, keep = build_features(rate, block)
    inst = lilv.Instance(plugin, rate, feats)
    if not inst or not inst.get_handle():
        raise RuntimeError("could not instantiate the plugin (missing host feature?)")

    audio_in, audio_out, atom_out, bufs = [], [], [], []
    for i in range(plugin.get_num_ports()):
        p = plugin.get_port_by_index(i)
        is_in = p.is_a(n_in)
        if p.is_a(n_audio) or p.is_a(n_cv):
            buf = np.zeros(block, dtype=np.float32)
            if p.is_a(n_audio):
                (audio_in if is_in else audio_out).append(buf)
        elif p.is_a(n_ctrl):
            default, _mn, _mx = p.get_range()
            sym = str(p.get_symbol())
            val = params.get(sym)
            if val is None:
                val = float(str(default)) if default is not None else 0.0
            buf = np.array([val], dtype=np.float32)
        elif p.is_a(n_atom):
            size = 8192
            m = p.get(n_min)
            if m is not None:
                size = max(size, int(float(str(m))))
            buf = np.zeros(size // 4 + 4, dtype=np.uint32)
            if is_in:                                   # empty sequence
                buf[0], buf[1] = 8, urid(URI["Sequence"])
            else:
                atom_out.append(buf)
        else:
            raise RuntimeError(f"unsupported port type on {p.get_symbol()}")
        bufs.append(buf)
        inst.connect_port(i, buf)
    if not audio_in or not audio_out:
        raise RuntimeError("plugin has no audio input/output")

    # half a second of silence first: lets plugins that smooth parameter
    # changes (e.g. Calf) settle before the impulse
    warm = (int(rate * float(req.get("settle", 0.5))) // block) * block
    x = np.zeros(warm + n, dtype=np.float32)
    x[warm] = 1.0
    y = np.zeros(warm + n, dtype=np.float32)
    inst.activate()
    for start in range(0, warm + n, block):
        for a in audio_in:
            a[:] = x[start:start + block]
        for a in atom_out:                              # capacity for the plugin to write into
            a[0], a[1] = a.nbytes - 8, urid(URI["Chunk"])
        inst.run(block)
        y[start:start + block] = audio_out[0]
    inst.deactivate()
    del inst, bufs, keep

    y = y[warm:]
    if not np.all(np.isfinite(y)):
        raise RuntimeError("the plugin produced invalid output (NaN/Inf)")
    H = np.abs(np.fft.rfft(y.astype(np.float64)))
    freqs = np.fft.rfftfreq(n, 1 / rate)
    pts = np.asarray(req["points"], dtype=np.float64)
    db = 20 * np.log10(np.maximum(np.interp(pts, freqs, H), 1e-9))
    return [round(float(v), 2) for v in db]


if __name__ == "__main__":
    try:
        out = {"db": measure(json.load(sys.stdin))}
    except Exception as e:  # noqa: BLE001
        out = {"error": str(e)}
    sys.stdout.write(json.dumps(out))
