"""Resposta em frequência de um plugin, medida com o próprio plugin.

Passa um impulso pelo plugin offline (lv2apply, do pacote lilv-utils) com os
parâmetros atuais e calcula a magnitude por FFT. Assim a curva mostrada é a
real para qualquer tipo/modo de filtro, sem reimplementar as fórmulas.
Só faz sentido para plugins lineares (EQ e filtros).
"""
import asyncio
import hashlib
import json
import logging
import os
import struct
import tempfile
from collections import OrderedDict

import numpy as np

log = logging.getLogger("audio-web")

SR = 48000
N = 32768                     # ~0,7 s: cobre a latência dos modos FIR/FFT do LSP
POINTS = np.geomspace(20, 20000, 240)
_cache = OrderedDict()
BROKEN = set()                # plugins com que o lv2apply rebentou (não voltar a tentar)
_sem = asyncio.Semaphore(2)


def _write_wav_f32(path, x, channels=1):
    data = np.repeat(x.astype("<f4")[:, None], channels, axis=1).tobytes()
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 3, channels, SR, SR * 4 * channels, 4 * channels, 32)
    hdr += b"data" + struct.pack("<I", len(data))
    with open(path, "wb") as f:
        f.write(hdr + data)


def _read_wav(path):
    with open(path, "rb") as f:
        b = f.read()
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        raise ValueError("WAV inválido")
    pos, fmt, ch, bits, data = 12, None, 1, 32, None
    while pos + 8 <= len(b):
        cid, size = b[pos:pos + 4], struct.unpack("<I", b[pos + 4:pos + 8])[0]
        body = b[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt, ch = struct.unpack("<HH", body[:4])
            bits = struct.unpack("<H", body[14:16])[0]
            if fmt == 0xFFFE:               # WAVE_FORMAT_EXTENSIBLE
                fmt = struct.unpack("<H", body[24:26])[0]
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if data is None:
        raise ValueError("WAV sem dados")
    if fmt == 3 and bits == 32:
        y = np.frombuffer(data, "<f4").astype(np.float64)
    elif fmt == 3 and bits == 64:
        y = np.frombuffer(data, "<f8")
    elif fmt == 1 and bits == 16:
        y = np.frombuffer(data, "<i2") / 32768.0
    elif fmt == 1 and bits == 32:
        y = np.frombuffer(data, "<i4") / 2147483648.0
    else:
        raise ValueError(f"formato WAV não suportado ({fmt}/{bits})")
    return y.reshape(-1, ch)[:, 0]


def _key(uri, params):
    raw = json.dumps([uri, sorted((k, round(float(v), 6)) for k, v in params.items())])
    return hashlib.sha1(raw.encode()).hexdigest()


async def measure(uri: str, params: dict, channels: int = 1) -> list:
    """Magnitude (dB) nos POINTS para o plugin com estes parâmetros.

    channels = n.º de entradas de áudio do plugin (o lv2apply exige o mesmo
    n.º de canais no ficheiro)."""
    if "lsp-plug.in" in uri and "equalizer" in uri and "mode" in params:
        # FIR/FFT/SPM do LSP têm a mesma magnitude do IIR (só muda a fase) e
        # precisam de mais blocos para estabilizar: mede-se em IIR
        params = dict(params, mode=0)
    k = _key(uri, params) + f":{channels}"
    if k in _cache:
        _cache.move_to_end(k)
        return _cache[k]
    async with _sem:
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "in.wav"), os.path.join(d, "out.wav")
            x = np.zeros(N, dtype=np.float32)
            x[0] = 1.0
            _write_wav_f32(src, x, channels)
            args = ["lv2apply", "-i", src, "-o", dst]
            for s, v in params.items():
                args += ["-c", s, f"{float(v):.9g}"]
            args.append(uri)
            p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(p.communicate(), 15)
            if p.returncode < 0 or p.returncode >= 128:
                BROKEN.add(uri)
            if p.returncode != 0 or not os.path.exists(dst):
                raise RuntimeError("lv2apply falhou: " + err.decode(errors="replace").strip()[-300:])
            y = _read_wav(dst)
    H = np.abs(np.fft.rfft(y, N))
    freqs = np.fft.rfftfreq(N, 1 / SR)
    db = 20 * np.log10(np.maximum(np.interp(POINTS, freqs, H), 1e-9))
    out = [round(float(v), 2) for v in db]
    _cache[k] = out
    while len(_cache) > 256:
        _cache.popitem(last=False)
    return out


def freqs() -> list:
    return [round(float(f), 2) for f in POINTS]
