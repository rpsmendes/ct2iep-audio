#!/usr/bin/env python3
"""CT2IEP - controlo web da cadeia de áudio TX.

Fala com o mod-host (motor LV2 sobre JACK) por TCP, lê os metadados dos
plugins com lilv para gerar a UI, guarda estado/presets em JSON e controla
o mixer ALSA da placa com amixer.
"""
import asyncio
import base64
import json
import logging
import math
import os
import re
import time
from pathlib import Path

import lilv
import yaml
from aiohttp import WSMsgType, web

log = logging.getLogger("audio-web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MODHOST_HOST = os.environ.get("MODHOST_HOST", "127.0.0.1")
MODHOST_PORT = int(os.environ.get("MODHOST_PORT", "5555"))
FEEDBACK_PORT = int(os.environ.get("MODHOST_FEEDBACK_PORT", "5556"))
CHAIN_FILE = Path(os.environ.get("CHAIN_FILE", "/config/chain.yaml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).with_name("static")
MIXER_CARD = os.environ.get("MIXER_CARD") or os.environ.get("AUDIO_CARD", "")
WEB_USER = os.environ.get("WEB_USER", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))

# Portas de controlo que só servem a GUI nativa dos plugins (escondidas na UI)
HIDDEN = re.compile(
    r"^(enabled|bypass|pause|clear|zoom|react|shift|fsel|ife|ofe|frqs|listen|sc_listen"
    r"|insp_.*|hue_\d+|xs_\d+|xm_\d+|slv|elv|grv|ilv|olv|clv|igv|ogv|scgv|grgv)$"
)

# ----------------------------------------------------------------------------
# Metadados LV2
# ----------------------------------------------------------------------------
_world = lilv.World()
_world.load_all()
_N = {
    "audio": _world.new_uri(lilv.LILV_URI_AUDIO_PORT),
    "control": _world.new_uri(lilv.LILV_URI_CONTROL_PORT),
    "input": _world.new_uri(lilv.LILV_URI_INPUT_PORT),
    "output": _world.new_uri(lilv.LILV_URI_OUTPUT_PORT),
    "unit": _world.new_uri("http://lv2plug.in/ns/extensions/units#unit"),
    "usym": _world.new_uri("http://lv2plug.in/ns/extensions/units#symbol"),
    "log": _world.new_uri("http://lv2plug.in/ns/ext/port-props#logarithmic"),
    "enum": _world.new_uri("http://lv2plug.in/ns/lv2core#enumeration"),
    "toggled": _world.new_uri("http://lv2plug.in/ns/lv2core#toggled"),
    "integer": _world.new_uri("http://lv2plug.in/ns/lv2core#integer"),
}


def _f(node, fallback=0.0):
    if node is None:
        return fallback
    try:
        return float(str(node))
    except (TypeError, ValueError):
        return fallback


def describe_plugin(uri: str) -> dict:
    plugin = _world.get_all_plugins()[_world.new_uri(uri)]
    if plugin is None:
        raise RuntimeError(f"plugin não encontrado: {uri}")
    d = {"uri": uri, "name": str(plugin.get_name()), "audio_in": [], "audio_out": [],
         "controls": [], "outputs": {}}
    for i in range(plugin.get_num_ports()):
        p = plugin.get_port_by_index(i)
        sym = str(p.get_symbol())
        is_in = p.is_a(_N["input"])
        if p.is_a(_N["audio"]):
            (d["audio_in"] if is_in else d["audio_out"]).append(sym)
            continue
        if not p.is_a(_N["control"]):
            continue
        default, mn, mx = p.get_range()
        unit = ""
        u = p.get(_N["unit"])
        if u is not None:
            s = _world.get(u, _N["usym"], None)
            unit = str(s) if s is not None else ""
        info = {
            "symbol": sym, "name": str(p.get_name()),
            "min": _f(mn), "max": _f(mx, 1.0), "default": _f(default, _f(mn)),
            "unit": unit, "log": p.has_property(_N["log"]),
            "toggle": p.has_property(_N["toggled"]),
            "integer": p.has_property(_N["integer"]),
            "enum": p.has_property(_N["enum"]),
            "points": [],
        }
        pts = p.get_scale_points() or []
        if info["enum"] and pts:
            info["points"] = sorted(
                ({"label": str(x.get_label()), "value": float(str(x.get_value()))} for x in pts),
                key=lambda x: x["value"])
        if is_in:
            if not HIDDEN.match(sym):
                d["controls"].append(info)
        else:
            d["outputs"][sym] = info
    return d


def parse_value(v):
    """Aceita números ou strings '-12dB' (convertidas para coeficiente linear)."""
    if isinstance(v, str):
        s = v.strip()
        if s.lower().endswith("db"):
            return 10 ** (float(s[:-2]) / 20.0)
        return float(s)
    return float(v)


# ----------------------------------------------------------------------------
# Cadeia e estado
# ----------------------------------------------------------------------------
class Chain:
    def __init__(self, path: Path):
        cfg = yaml.safe_load(path.read_text())
        self.inputs = cfg.get("io", {}).get("inputs", ["system:capture_1"])
        self.outputs = cfg.get("io", {}).get("outputs", ["system:playback_1", "system:playback_2"])
        self.blocks = []
        for b in cfg["blocks"]:
            meta = describe_plugin(b["uri"])
            ctrl = {c["symbol"]: c for c in meta["controls"]}
            defaults = {s: c["default"] for s, c in ctrl.items()}
            for s, v in (b.get("params") or {}).items():
                if s not in ctrl:
                    log.warning("%s: parâmetro desconhecido '%s' ignorado", b["id"], s)
                    continue
                defaults[s] = parse_value(v)
            main = [s for s in b.get("main", []) if s in ctrl]
            meters = []
            for m in b.get("meters", []):
                out = meta["outputs"].get(m["symbol"])
                if not out:
                    log.warning("%s: meter desconhecido '%s'", b["id"], m["symbol"])
                    continue
                lin = m.get("lin", out["unit"] == "G" or (out["unit"] == "" and out["max"] <= 64 and out["min"] >= 0))
                meters.append({"symbol": m["symbol"], "label": m.get("label", out["name"]),
                               "lin": bool(lin), "unit": out["unit"],
                               "kind": m.get("kind", "level")})
            self.blocks.append({
                "id": b["id"], "name": b.get("name", meta["name"]), "replaces": b.get("replaces", ""),
                "uri": b["uri"], "plugin": meta["name"], "main": main,
                "controls": meta["controls"], "audio_in": meta["audio_in"],
                "audio_out": meta["audio_out"], "defaults": defaults,
                "default_bypass": bool(b.get("bypass", False)), "meters": meters,
            })
        self.by_id = {b["id"]: b for b in self.blocks}

    def default_state(self):
        return {"chain_bypass": False,
                "blocks": {b["id"]: {"bypass": b["default_bypass"], "params": dict(b["defaults"])}
                           for b in self.blocks}}

    def merge(self, saved: dict) -> dict:
        """Estado gravado por cima dos defaults (ignora blocos/parâmetros que já não existem)."""
        st = self.default_state()
        st["chain_bypass"] = bool(saved.get("chain_bypass", False))
        for bid, bs in (saved.get("blocks") or {}).items():
            if bid not in st["blocks"]:
                continue
            tgt = st["blocks"][bid]
            tgt["bypass"] = bool(bs.get("bypass", tgt["bypass"]))
            for s, v in (bs.get("params") or {}).items():
                if s in tgt["params"]:
                    tgt["params"][s] = float(v)
        return st

    def clamp(self, bid, sym, value):
        c = next((c for c in self.by_id[bid]["controls"] if c["symbol"] == sym), None)
        if c is None:
            raise KeyError(sym)
        v = min(max(float(value), c["min"]), c["max"])
        if c["integer"] or c["toggle"] or c["enum"]:
            v = float(round(v))
        return v


# ----------------------------------------------------------------------------
# Cliente mod-host
# ----------------------------------------------------------------------------
class ModHost:
    def __init__(self, app):
        self.app = app
        self.reader = self.writer = None
        self.lock = asyncio.Lock()
        self.online = False
        self.meter_values = {}

    async def cmd(self, line: str) -> str:
        async with self.lock:
            if not self.writer:
                raise ConnectionError("mod-host desligado")
            self.writer.write(line.encode() + b"\0")
            await self.writer.drain()
            data = await asyncio.wait_for(self.reader.readuntil(b"\0"), 10)
            resp = data.rstrip(b"\0").decode(errors="replace")
            if resp.startswith("resp -"):
                log.warning("mod-host: '%s' -> %s", line, resp)
            return resp

    async def build(self):
        chain: Chain = self.app["chain"]
        st = self.app["rt"]["state"]
        await self.cmd("remove -1")
        for idx, b in enumerate(chain.blocks):
            r = await self.cmd(f'add "{b["uri"]}" {idx}')
            if r.startswith("resp -"):
                raise RuntimeError(f"falhou a carregar {b['id']} ({r})")
        for idx, b in enumerate(chain.blocks):
            for s, v in st["blocks"][b["id"]]["params"].items():
                await self.cmd(f'param_set {idx} {s} {v:.6g}')
            await self.cmd(f"bypass {idx} {int(self.effective_bypass(b['id']))}")
        # ligações: saída[0] do bloco anterior -> todas as entradas do seguinte
        src = chain.inputs[:1]
        for idx, b in enumerate(chain.blocks):
            for s in src:
                for a in b["audio_in"]:
                    await self.cmd(f'connect "{s}" "effect_{idx}:{a}"')
            src = [f"effect_{idx}:{b['audio_out'][0]}"]
        for s in src:
            for o in chain.outputs:
                await self.cmd(f'connect "{s}" "{o}"')
        for idx, b in enumerate(chain.blocks):
            for m in b["meters"]:
                await self.cmd(f"monitor_output {idx} {m['symbol']}")
        await self.cmd("output_data_ready")
        log.info("cadeia carregada (%d blocos)", len(chain.blocks))

    def effective_bypass(self, bid):
        st = self.app["rt"]["state"]
        return st["chain_bypass"] or st["blocks"][bid]["bypass"]

    async def feedback(self, r):
        while True:
            data = await r.readuntil(b"\0")
            msg = data.rstrip(b"\0").decode(errors="replace")
            if msg.startswith("output_set"):
                _, inst, sym, val = msg.split(" ", 3)
                self.meter_values[(int(inst), sym)] = float(val)
            elif msg.startswith("data_finish"):
                await self.cmd("output_data_ready")

    async def run(self):
        while True:
            fb_writer = fb_task = None
            try:
                # o mod-host só começa a processar comandos depois de aceitar
                # as duas ligações (comandos e feedback), por esta ordem
                self.reader, self.writer = await asyncio.open_connection(MODHOST_HOST, MODHOST_PORT)
                fb_reader, fb_writer = await asyncio.open_connection(MODHOST_HOST, FEEDBACK_PORT)
                fb_task = asyncio.create_task(self.feedback(fb_reader))
                await self.build()
                self.online = True
                await broadcast(self.app, {"type": "status", "online": True})
                await fb_task                  # fica aqui até a ligação cair
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if self.online:
                    log.warning("ligação ao mod-host perdida: %r", e)
                else:
                    log.info("à espera do mod-host em %s:%d (%r)", MODHOST_HOST, MODHOST_PORT, e)
            finally:
                if fb_task:
                    fb_task.cancel()
                for w in (self.writer, fb_writer):
                    if w:
                        w.close()
            self.online = False
            self.meter_values.clear()
            self.reader = self.writer = None
            await broadcast(self.app, {"type": "status", "online": False})
            await asyncio.sleep(2)


# ----------------------------------------------------------------------------
# Mixer ALSA (amixer)
# ----------------------------------------------------------------------------
async def run_cmd(*args):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode != 0:
        raise RuntimeError(err.decode().strip() or f"{args[0]} falhou")
    return out.decode()


def parse_scontents(text):
    controls = []
    for chunk in re.split(r"^Simple mixer control ", text, flags=re.M)[1:]:
        m = re.match(r"'(.*)',(\d+)", chunk)
        if not m:
            continue
        name, index = m.group(1), int(m.group(2))
        caps_m = re.search(r"Capabilities:(.*)", chunk)
        caps = caps_m.group(1).split() if caps_m else []
        lines = chunk.splitlines()
        if "enum" in caps or "penum" in caps or "cenum" in caps:
            items = re.findall(r"'([^']*)'", (re.search(r"Items:(.*)", chunk) or [None, ""])[1])
            cur = re.search(r"Item0: '([^']*)'", chunk)
            controls.append({"id": f"{name},{index}", "name": name, "kind": "enum",
                             "items": items, "value": cur.group(1) if cur else None})
            continue
        for direction, vcap, scap in (("playback", "pvolume", "pswitch"), ("capture", "cvolume", "cswitch")):
            has_vol = vcap in caps or (direction == "playback" and "volume" in caps)
            has_sw = scap in caps or (direction == "playback" and "switch" in caps)
            if not (has_vol or has_sw):
                continue
            key = "Playback" if direction == "playback" else "Capture"
            other = "Capture" if key == "Playback" else "Playback"
            pct = sw = db = None
            for ln in lines:
                if ln.strip().startswith(("Capabilities", "Playback channels", "Capture channels", "Limits")):
                    continue
                i = ln.find(key)
                if i < 0:
                    continue
                seg = ln[i:]
                j = seg.find(other)
                if j > 0:
                    seg = seg[:j]               # linha com Playback e Capture juntos
                p = re.search(r"\[(\d+)%\]", seg)
                sw_m = re.search(r"\[(on|off)\]", seg)
                d = re.search(r"\[(-?[\d.]+dB)\]", seg)
                pct = int(p.group(1)) if p else pct
                sw = (sw_m.group(1) == "on") if sw_m else sw
                db = d.group(1) if d else db
                if pct is not None or sw is not None:
                    break
            controls.append({"id": f"{name},{index}", "name": name, "kind": "volume",
                             "direction": direction, "has_volume": has_vol, "has_switch": has_sw,
                             "percent": pct, "on": sw, "db": db})
    return controls


async def mixer_list():
    if not MIXER_CARD:
        return []
    return parse_scontents(await run_cmd("amixer", "-M", "-c", MIXER_CARD, "scontents"))


# ----------------------------------------------------------------------------
# Estado, presets e broadcast
# ----------------------------------------------------------------------------
SAFE_NAME = re.compile(r"^[\w\- .]{1,40}$")


def presets_dir():
    d = DATA_DIR / "presets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_state(app):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(app["rt"]["state"], indent=1))
    tmp.replace(DATA_DIR / "state.json")


def schedule_save(app):
    h = app["rt"].get("save_handle")
    if h:
        h.cancel()
    app["rt"]["save_handle"] = asyncio.get_running_loop().call_later(1.0, save_state, app)


async def broadcast(app, msg, exclude=None):
    data = json.dumps(msg)
    for ws in list(app["sockets"]):
        if ws is exclude or ws.closed:
            continue
        try:
            await ws.send_str(data)
        except Exception:  # noqa: BLE001
            app["sockets"].discard(ws)


async def apply_param(app, bid, sym, value, origin=None):
    chain: Chain = app["chain"]
    v = chain.clamp(bid, sym, value)
    app["rt"]["state"]["blocks"][bid]["params"][sym] = v
    mh: ModHost = app["modhost"]
    if mh.online:
        idx = chain.blocks.index(chain.by_id[bid])
        await mh.cmd(f"param_set {idx} {sym} {v:.6g}")
    schedule_save(app)
    await broadcast(app, {"type": "param", "block": bid, "symbol": sym, "value": v}, exclude=origin)
    return v


async def apply_all_bypass(app):
    chain: Chain = app["chain"]
    mh: ModHost = app["modhost"]
    if mh.online:
        for idx, b in enumerate(chain.blocks):
            await mh.cmd(f"bypass {idx} {int(mh.effective_bypass(b['id']))}")


async def apply_state(app, new_state):
    """Aplica um estado completo (preset) sem recarregar os plugins."""
    chain: Chain = app["chain"]
    app["rt"]["state"] = chain.merge(new_state)
    mh: ModHost = app["modhost"]
    if mh.online:
        for idx, b in enumerate(chain.blocks):
            for s, v in app["rt"]["state"]["blocks"][b["id"]]["params"].items():
                await mh.cmd(f"param_set {idx} {s} {v:.6g}")
        await apply_all_bypass(app)
    schedule_save(app)
    await broadcast(app, {"type": "state", "state": app["rt"]["state"]})


async def meter_ticker(app):
    chain: Chain = app["chain"]
    while True:
        await asyncio.sleep(0.08)
        mh: ModHost = app["modhost"]
        if not mh.online or not app["sockets"] or not mh.meter_values:
            continue
        out = {}
        for idx, b in enumerate(chain.blocks):
            for m in b["meters"]:
                v = mh.meter_values.get((idx, m["symbol"]))
                if v is None:
                    continue
                if m["lin"]:
                    v = 20 * math.log10(v) if v > 1e-6 else -120.0
                out[f"{b['id']}.{m['symbol']}"] = round(v, 1)
        if out:
            await broadcast(app, {"type": "meters", "values": out})


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
@web.middleware
async def auth_mw(request, handler):
    if WEB_PASSWORD:
        expected = "Basic " + base64.b64encode(f"{WEB_USER}:{WEB_PASSWORD}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="CT2IEP Audio"'})
    return await handler(request)


async def h_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def h_chain(request):
    app = request.app
    chain: Chain = app["chain"]
    blocks = [{k: b[k] for k in ("id", "name", "replaces", "uri", "plugin", "main", "controls",
                                 "defaults", "meters")} for b in chain.blocks]
    return web.json_response({"blocks": blocks, "state": app["rt"]["state"],
                              "online": app["modhost"].online,
                              "io": {"inputs": chain.inputs, "outputs": chain.outputs}})


async def h_presets(request):
    names = sorted(p.stem for p in presets_dir().glob("*.json"))
    return web.json_response({"presets": names})


async def h_preset_save(request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not SAFE_NAME.match(name):
        return web.json_response({"error": "nome inválido"}, status=400)
    (presets_dir() / f"{name}.json").write_text(json.dumps(request.app["rt"]["state"], indent=1))
    return web.json_response({"ok": True})


async def h_preset_load(request):
    name = request.match_info["name"]
    f = presets_dir() / f"{name}.json"
    if not SAFE_NAME.match(name) or not f.exists():
        return web.json_response({"error": "preset não existe"}, status=404)
    await apply_state(request.app, json.loads(f.read_text()))
    return web.json_response({"ok": True})


async def h_preset_delete(request):
    name = request.match_info["name"]
    f = presets_dir() / f"{name}.json"
    if SAFE_NAME.match(name) and f.exists():
        f.unlink()
    return web.json_response({"ok": True})


async def h_reset(request):
    """Repõe os valores do chain.yaml (um bloco ou todos)."""
    app = request.app
    chain: Chain = app["chain"]
    bid = request.query.get("block")
    st = json.loads(json.dumps(app["rt"]["state"]))
    d = chain.default_state()
    if bid:
        st["blocks"][bid] = d["blocks"][bid]
    else:
        st = d
    await apply_state(app, st)
    return web.json_response({"ok": True})


async def h_mixer(request):
    try:
        return web.json_response({"card": MIXER_CARD, "controls": await mixer_list()})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"card": MIXER_CARD, "controls": [], "error": str(e)})


async def h_mixer_set(request):
    b = await request.json()
    cid = str(b["id"])
    args = ["amixer", "-M", "-c", MIXER_CARD, "-q", "sset", cid]
    if b.get("kind") == "enum":
        args.append(str(b["value"]))
    else:
        args.append(b.get("direction", "playback"))
        if "percent" in b:
            args.append(f"{max(0, min(100, int(b['percent'])))}%")
        if "on" in b:
            args.append("on" if b["on"] else "off")
    try:
        await run_cmd(*args)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True})


async def h_cards(request):
    try:
        return web.Response(text=Path("/proc/asound/cards").read_text())
    except OSError as e:
        return web.Response(text=str(e))


async def h_ws(request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    app["sockets"].add(ws)
    await ws.send_str(json.dumps({"type": "status", "online": app["modhost"].online}))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
                t = m.get("type")
                if t == "param":
                    await apply_param(app, m["block"], m["symbol"], m["value"], origin=ws)
                elif t == "bypass":
                    app["rt"]["state"]["blocks"][m["block"]]["bypass"] = bool(m["value"])
                    await apply_all_bypass(app)
                    schedule_save(app)
                    await broadcast(app, m, exclude=ws)
                elif t == "chain_bypass":
                    app["rt"]["state"]["chain_bypass"] = bool(m["value"])
                    await apply_all_bypass(app)
                    schedule_save(app)
                    await broadcast(app, m, exclude=ws)
            except Exception as e:  # noqa: BLE001
                log.warning("mensagem ws inválida: %s (%s)", msg.data[:200], e)
                await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
    finally:
        app["sockets"].discard(ws)
    return ws


async def on_startup(app):
    app["tasks"] = [asyncio.create_task(app["modhost"].run()),
                    asyncio.create_task(meter_ticker(app))]


async def on_shutdown(app):
    for ws in list(app["sockets"]):
        await ws.close()
    for t in app["tasks"]:
        t.cancel()
    await asyncio.gather(*app["tasks"], return_exceptions=True)
    save_state(app)


def make_app():
    app = web.Application(middlewares=[auth_mw])
    chain = Chain(CHAIN_FILE)
    app["chain"] = chain
    saved = {}
    sf = DATA_DIR / "state.json"
    if sf.exists():
        try:
            saved = json.loads(sf.read_text())
        except ValueError:
            log.warning("state.json inválido, a usar defaults")
    app["rt"] = {"state": chain.merge(saved) if saved else chain.default_state()}
    app["sockets"] = set()
    app["modhost"] = ModHost(app)
    app.router.add_get("/", h_index)
    app.router.add_get("/api/chain", h_chain)
    app.router.add_get("/api/presets", h_presets)
    app.router.add_post("/api/presets", h_preset_save)
    app.router.add_post("/api/presets/{name}/load", h_preset_load)
    app.router.add_delete("/api/presets/{name}", h_preset_delete)
    app.router.add_post("/api/reset", h_reset)
    app.router.add_get("/api/mixer", h_mixer)
    app.router.add_post("/api/mixer", h_mixer_set)
    app.router.add_get("/api/cards", h_cards)
    app.router.add_get("/ws", h_ws)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=HTTP_PORT, access_log=None, shutdown_timeout=2)
