#!/usr/bin/env python3
"""CT2IEP - web control for the TX audio chain.

Talks to mod-host (LV2 host on JACK) over TCP, reads plugin metadata with
lilv to build the UI, stores state/presets as JSON and drives the ALSA mixer
of the sound cards with amixer.

The chain is an ordered list of blocks stored in data/state.json.
config/chain.yaml defines the "factory chain" (initial state and reset) and
how known blocks are presented (main parameters, meters).
"""
import asyncio
import base64
import copy
import json
import logging
import math
import os
import re
import unicodedata
from pathlib import Path

import yaml
from aiohttp import WSMsgType, web

import lv2meta
import packages
import response

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
STATE_VERSION = 2


def parse_value(v):
    """Accepts numbers or strings such as '-12dB' (converted to a linear coefficient)."""
    if isinstance(v, str):
        s = v.strip()
        if s.lower().endswith("db"):
            return 10 ** (float(s[:-2]) / 20.0)
        return float(s)
    return float(v)


def slugify(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t or "block")[:24].strip("-")


# ----------------------------------------------------------------------------
# Factory chain (chain.yaml)
# ----------------------------------------------------------------------------
class Factory:
    def __init__(self, path: Path):
        cfg = yaml.safe_load(path.read_text())
        io = cfg.get("io", {})
        self.inputs = io.get("inputs", ["system:capture_1"])
        self.outputs = io.get("outputs", ["system:playback_1", "system:playback_2"])
        self.specs = {}
        self.order = []
        for b in cfg["blocks"]:
            spec = {
                "id": b["id"], "uri": b["uri"], "name": b.get("name"),
                "replaces": b.get("replaces", ""), "main": b.get("main"),
                "meters": b.get("meters"), "graph": b.get("graph"),
                "bypass": bool(b.get("bypass", False)),
                "params": {s: parse_value(v) for s, v in (b.get("params") or {}).items()},
            }
            self.specs[b["id"]] = spec
            self.order.append(b["id"])

    def spec_for(self, block):
        s = self.specs.get(block["id"])
        return s if s and s["uri"] == block["uri"] else None

    def blocks(self):
        return [{"id": s["id"], "uri": s["uri"], "bypass": s["bypass"], "params": dict(s["params"])}
                for s in (self.specs[i] for i in self.order)]


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
def plugin_available(uri):
    try:
        lv2meta.describe(uri)
        return True
    except KeyError:
        return False


def defaults_for(app, block):
    meta = lv2meta.describe(block["uri"])
    d = {c["symbol"]: c["default"] for c in meta["controls"]}
    spec = app["factory"].spec_for(block)
    if spec:
        for s, v in spec["params"].items():
            if s in d:
                d[s] = v
    return d


def normalize_block(app, b):
    """Ensures valid parameters (unknown ones dropped, missing ones set to the default)."""
    out = {"id": str(b["id"]), "uri": str(b["uri"]), "bypass": bool(b.get("bypass", False)),
           "params": {}}
    if b.get("name"):
        out["name"] = str(b["name"])
    if not plugin_available(out["uri"]):
        out["params"] = {k: float(v) for k, v in (b.get("params") or {}).items()}
        return out
    defaults = defaults_for(app, out)
    saved = b.get("params") or {}
    for s, dv in defaults.items():
        try:
            out["params"][s] = float(saved[s]) if s in saved else dv
        except (TypeError, ValueError):
            out["params"][s] = dv
    return out


def migrate_v1(app, saved):
    """Old state/preset (blocks keyed by id, fixed chain.yaml order) -> v2."""
    blocks = app["factory"].blocks()
    old = saved.get("blocks") or {}
    for b in blocks:
        ob = old.get(b["id"])
        if ob:
            b["bypass"] = bool(ob.get("bypass", b["bypass"]))
            b["params"].update({k: v for k, v in (ob.get("params") or {}).items()})
    return {"version": STATE_VERSION, "chain_bypass": bool(saved.get("chain_bypass", False)),
            "blocks": blocks}


def normalize_state(app, saved):
    if not saved:
        saved = {"version": STATE_VERSION, "chain_bypass": False, "blocks": app["factory"].blocks()}
    elif isinstance(saved.get("blocks"), dict):
        saved = migrate_v1(app, saved)
    blocks, seen = [], set()
    for b in saved.get("blocks", []):
        if not b.get("id") or not b.get("uri") or b["id"] in seen:
            continue
        seen.add(b["id"])
        blocks.append(normalize_block(app, b))
    return {"version": STATE_VERSION, "chain_bypass": bool(saved.get("chain_bypass", False)),
            "blocks": blocks}


def find_block(app, bid):
    for b in app["rt"]["state"]["blocks"]:
        if b["id"] == bid:
            return b
    raise KeyError(f"unknown block: {bid}")


def control_of(block, sym):
    meta = lv2meta.describe(block["uri"])
    for c in meta["controls"]:
        if c["symbol"] == sym:
            return c
    raise KeyError(sym)


def clamp(block, sym, value):
    c = control_of(block, sym)
    v = min(max(float(value), c["min"]), c["max"])
    if c["integer"] or c["toggle"] or c["enum"]:
        v = float(round(v))
    return v


def unique_id(app, base):
    ids = {b["id"] for b in app["rt"]["state"]["blocks"]}
    if base not in ids:
        return base
    n = 2
    while f"{base}-{n}" in ids:
        n += 1
    return f"{base}-{n}"


# ----------------------------------------------------------------------------
# View for the UI
# ----------------------------------------------------------------------------
def resolve_meters(meta, specs):
    out = []
    for m in specs or []:
        o = meta["outputs"].get(m["symbol"])
        if not o:
            continue
        lin = m.get("lin", o["unit"] == "G" or (o["unit"] == "" and o["max"] <= 64 and o["min"] >= 0))
        out.append({"symbol": m["symbol"], "label": m.get("label", o["name"]),
                    "lin": bool(lin), "unit": o["unit"], "kind": m.get("kind", "level")})
    return out


def block_view(app, b):
    spec = app["factory"].spec_for(b)
    eng: Engine = app["engine"]
    v = {"id": b["id"], "uri": b["uri"], "bypass": b["bypass"], "params": b["params"],
         "error": eng.errors.get(b["id"]), "missing": False}
    try:
        meta = lv2meta.describe(b["uri"])
    except KeyError:
        v.update({"missing": True, "name": b.get("name") or b["uri"].rsplit("/", 1)[-1],
                  "plugin": b["uri"], "replaces": "", "class": "", "graph": False, "main": [],
                  "controls": [], "defaults": {}, "meters": []})
        return v
    syms = [c["symbol"] for c in meta["controls"]]
    if spec and spec["main"]:
        main = [s for s in spec["main"] if s in syms]
    else:
        main = syms if len(syms) <= 10 else syms[:8]
    meters = resolve_meters(meta, spec["meters"] if spec and spec["meters"] is not None
                            else lv2meta.auto_meters(meta))
    graph = meta["graph"] and b["uri"] not in response.BROKEN and \
        (spec["graph"] if spec and spec["graph"] is not None else True)
    v.update({"name": (spec and spec["name"]) or b.get("name") or meta["name"],
              "replaces": spec["replaces"] if spec else "", "plugin": meta["name"],
              "class": meta["class"], "graph": bool(graph), "main": main,
              "graph_unsupported": bool(meta["eq_like"] and not graph),
              "controls": meta["controls"], "defaults": defaults_for(app, b), "meters": meters,
              "stereo": len(meta["audio_in"]) > 1})
    return v


def chain_view(app):
    st = app["rt"]["state"]
    f: Factory = app["factory"]
    return {"chain_bypass": st["chain_bypass"], "online": app["engine"].online,
            "io": {"inputs": f.inputs, "outputs": f.outputs},
            "blocks": [block_view(app, b) for b in st["blocks"]]}


# ----------------------------------------------------------------------------
# Engine (mod-host)
# ----------------------------------------------------------------------------
class Engine:
    def __init__(self, app):
        self.app = app
        self.reader = self.writer = None
        self.lock = asyncio.Lock()
        self.online = False
        self.inst = {}            # block id -> mod-host instance
        self.next_inst = 0
        self.links = set()        # JACK connections we made
        self.errors = {}          # block id -> load error
        self.meter_values = {}

    async def cmd(self, line: str, quiet=False) -> str:
        async with self.lock:
            if not self.writer:
                raise ConnectionError("mod-host not connected")
            self.writer.write(line.encode() + b"\0")
            await self.writer.drain()
            data = await asyncio.wait_for(self.reader.readuntil(b"\0"), 10)
            resp = data.rstrip(b"\0").decode(errors="replace")
            if resp.startswith("resp -") and not quiet:
                log.warning("mod-host: '%s' -> %s", line, resp)
            return resp

    def effective_bypass(self, b):
        return self.app["rt"]["state"]["chain_bypass"] or b["bypass"]

    async def load_block(self, b):
        self.errors.pop(b["id"], None)
        if not plugin_available(b["uri"]):
            self.errors[b["id"]] = "plugin not installed"
            return False
        i = self.next_inst
        self.next_inst += 1
        r = await self.cmd(f'add "{b["uri"]}" {i}')
        if r.startswith("resp -"):
            self.errors[b["id"]] = f"mod-host refused the plugin ({r[5:]})"
            return False
        self.inst[b["id"]] = i
        for s, v in b["params"].items():
            await self.cmd(f"param_set {i} {s} {v:.6g}", quiet=True)
        await self.cmd(f"bypass {i} {int(self.effective_bypass(b))}")
        meta = lv2meta.describe(b["uri"])
        for m in block_view(self.app, b)["meters"]:
            if m["symbol"] in meta["outputs"]:
                await self.cmd(f"monitor_output {i} {m['symbol']}")
        return True

    async def unload_block(self, bid):
        i = self.inst.pop(bid, None)
        self.errors.pop(bid, None)
        if i is None:
            return
        pref = f"effect_{i}:"
        self.links = {l for l in self.links if not (l[0].startswith(pref) or l[1].startswith(pref))}
        for k in [k for k in self.meter_values if k[0] == i]:
            del self.meter_values[k]
        await self.cmd(f"remove {i}")

    def desired_links(self):
        f: Factory = self.app["factory"]
        src = f.inputs[:1]
        links = set()
        for b in self.app["rt"]["state"]["blocks"]:
            i = self.inst.get(b["id"])
            if i is None:
                continue
            meta = lv2meta.describe(b["uri"])
            for s in src:
                for a in meta["audio_in"]:
                    links.add((s, f"effect_{i}:{a}"))
            src = [f"effect_{i}:{meta['audio_out'][0]}"]
        for s in src:
            for o in f.outputs:
                links.add((s, o))
        return links

    async def rewire(self):
        """Applies only the differences: disconnect first (brief silence), then connect."""
        new = self.desired_links()
        for a, b in sorted(self.links - new):
            await self.cmd(f'disconnect "{a}" "{b}"', quiet=True)
        for a, b in sorted(new - self.links):
            await self.cmd(f'connect "{a}" "{b}"')
        self.links = new

    async def build(self):
        await self.cmd("remove -1")
        self.inst, self.links, self.errors, self.next_inst = {}, set(), {}, 0
        for b in self.app["rt"]["state"]["blocks"]:
            await self.load_block(b)
        await self.rewire()
        await self.cmd("output_data_ready")
        log.info("chain loaded (%d blocks)", len(self.inst))

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
                # mod-host only starts processing commands after it has accepted
                # both connections (commands and feedback), in this order
                self.reader, self.writer = await asyncio.open_connection(MODHOST_HOST, MODHOST_PORT)
                fb_reader, fb_writer = await asyncio.open_connection(MODHOST_HOST, FEEDBACK_PORT)
                fb_task = asyncio.create_task(self.feedback(fb_reader))
                async with self.app["rt"]["oplock"]:
                    await self.build()
                self.online = True
                await broadcast(self.app, {"type": "chain", "chain": chain_view(self.app)})
                await fb_task                  # stays here until the connection drops
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if self.online:
                    log.warning("lost connection to mod-host: %r", e)
                else:
                    log.info("waiting for mod-host at %s:%d (%r)", MODHOST_HOST, MODHOST_PORT, e)
            finally:
                if fb_task:
                    fb_task.cancel()
                for w in (self.writer, fb_writer):
                    if w:
                        w.close()
            self.online = False
            self.meter_values.clear()
            self.inst, self.links = {}, set()
            self.reader = self.writer = None
            await broadcast(self.app, {"type": "status", "online": False})
            await asyncio.sleep(2)


# ----------------------------------------------------------------------------
# Chain operations
# ----------------------------------------------------------------------------
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


async def chain_changed(app):
    schedule_save(app)
    await broadcast(app, {"type": "chain", "chain": chain_view(app)})


async def op_param(app, bid, sym, value, origin=None):
    b = find_block(app, bid)
    v = clamp(b, sym, value)
    b["params"][sym] = v
    eng: Engine = app["engine"]
    i = eng.inst.get(bid)
    if eng.online and i is not None:
        await eng.cmd(f"param_set {i} {sym} {v:.6g}")
    schedule_save(app)
    await broadcast(app, {"type": "param", "block": bid, "symbol": sym, "value": v}, exclude=origin)


async def apply_bypass_all(app):
    eng: Engine = app["engine"]
    if eng.online:
        for b in app["rt"]["state"]["blocks"]:
            i = eng.inst.get(b["id"])
            if i is not None:
                await eng.cmd(f"bypass {i} {int(eng.effective_bypass(b))}")


async def op_move(app, order):
    async with app["rt"]["oplock"]:
        st = app["rt"]["state"]
        cur = {b["id"]: b for b in st["blocks"]}
        if sorted(order) != sorted(cur):
            raise ValueError("the new order does not contain the same blocks")
        st["blocks"] = [cur[i] for i in order]
        if app["engine"].online:
            await app["engine"].rewire()
    await chain_changed(app)


async def op_add(app, uri, at=None):
    async with app["rt"]["oplock"]:
        meta = lv2meta.describe(uri)          # KeyError if it does not exist
        st = app["rt"]["state"]
        b = {"id": unique_id(app, slugify(meta["name"])), "uri": uri, "bypass": False,
             "name": meta["name"], "params": {}}
        b = normalize_block(app, b)
        b["name"] = meta["name"]
        pos = len(st["blocks"]) if at is None else max(0, min(int(at), len(st["blocks"])))
        st["blocks"].insert(pos, b)
        eng: Engine = app["engine"]
        if eng.online:
            await eng.load_block(b)
            await eng.rewire()
    await chain_changed(app)
    return b["id"]


async def op_remove(app, bid):
    async with app["rt"]["oplock"]:
        st = app["rt"]["state"]
        find_block(app, bid)
        eng: Engine = app["engine"]
        if eng.online:
            await eng.unload_block(bid)
        st["blocks"] = [b for b in st["blocks"] if b["id"] != bid]
        if eng.online:
            await eng.rewire()
    await chain_changed(app)


async def op_apply_state(app, new_state):
    """Applies a full state (preset, reset). Only reloads the plugins that change."""
    async with app["rt"]["oplock"]:
        new = normalize_state(app, new_state)
        eng: Engine = app["engine"]
        old = {b["id"]: b["uri"] for b in app["rt"]["state"]["blocks"]}
        newmap = {b["id"]: b["uri"] for b in new["blocks"]}
        app["rt"]["state"] = new
        if eng.online:
            for bid, uri in old.items():
                if newmap.get(bid) != uri:
                    await eng.unload_block(bid)
            for b in new["blocks"]:
                i = eng.inst.get(b["id"])
                if i is None:
                    await eng.load_block(b)
                    continue
                for s, v in b["params"].items():
                    await eng.cmd(f"param_set {i} {s} {v:.6g}", quiet=True)
            await apply_bypass_all(app)
            await eng.rewire()
    await chain_changed(app)


async def meter_ticker(app):
    while True:
        await asyncio.sleep(0.08)
        eng: Engine = app["engine"]
        if not eng.online or not app["sockets"] or not eng.meter_values:
            continue
        out = {}
        for b in app["rt"]["state"]["blocks"]:
            i = eng.inst.get(b["id"])
            if i is None:
                continue
            try:
                meters = block_view(app, b)["meters"]
            except Exception:  # noqa: BLE001
                continue
            for m in meters:
                v = eng.meter_values.get((i, m["symbol"]))
                if v is None:
                    continue
                if m["lin"]:
                    v = 20 * math.log10(v) if v > 1e-6 else -120.0
                out[f"{b['id']}.{m['symbol']}"] = round(v, 1)
        if out:
            await broadcast(app, {"type": "meters", "values": out})


# ----------------------------------------------------------------------------
# Sound cards (aplay/arecord/amixer)
# ----------------------------------------------------------------------------
async def run_cmd(*args, env=None):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE, env=env)
    out, err = await p.communicate()
    if p.returncode != 0:
        raise RuntimeError(err.decode().strip() or f"{args[0]} failed")
    return out.decode()


CARD_LINE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[(.*?)\]")


async def list_cards():
    """Cards via aplay -l / arecord -l (Docker hides /proc/asound)."""
    cards = {}
    raw = []
    for tool, kind in (("aplay", "playback"), ("arecord", "capture")):
        try:
            text = await run_cmd(tool, "-l")
        except Exception as e:  # noqa: BLE001
            text = f"{tool}: {e}"
        raw.append(text.strip())
        for ln in text.splitlines():
            m = CARD_LINE.match(ln)
            if not m:
                continue
            num, cid, cname, dev, _, dname = m.groups()
            c = cards.setdefault(cid, {"index": int(num), "id": cid, "name": cname,
                                       "playback": [], "capture": []})
            c[kind].append({"device": int(dev), "name": dname})
    return sorted(cards.values(), key=lambda c: c["index"]), "\n\n".join(raw)


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
                    seg = seg[:j]               # line with both Playback and Capture
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


SAFE_CARD = re.compile(r"^[\w\-]{1,32}$")


def card_param(request, body=None):
    card = (body or {}).get("card") or request.query.get("card") or MIXER_CARD
    if card and not SAFE_CARD.match(str(card)):
        raise web.HTTPBadRequest(text="invalid card")
    return str(card)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
SAFE_NAME = re.compile(r"^[\w\- .]{1,40}$")


def presets_dir():
    d = DATA_DIR / "presets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


@web.middleware
async def auth_mw(request, handler):
    if WEB_PASSWORD:
        expected = "Basic " + base64.b64encode(f"{WEB_USER}:{WEB_PASSWORD}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="CT2IEP Audio"'})
    return await handler(request)


async def h_index(request):
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def h_chain(request):
    return web.json_response(chain_view(request.app))


async def h_order(request):
    body = await request.json()
    try:
        await op_move(request.app, [str(x) for x in body["order"]])
    except (ValueError, KeyError) as e:
        return err(str(e))
    return web.json_response({"ok": True})


async def h_add(request):
    body = await request.json()
    try:
        bid = await op_add(request.app, str(body["uri"]), body.get("at"))
    except KeyError as e:
        return err(str(e).strip("'"))
    b = find_block(request.app, bid)
    e = request.app["engine"].errors.get(bid)
    return web.json_response({"ok": True, "id": bid, "error": e, "name": b.get("name")})


async def h_remove(request):
    try:
        await op_remove(request.app, request.match_info["id"])
    except KeyError as e:
        return err(str(e).strip("'"), 404)
    return web.json_response({"ok": True})


async def h_reset(request):
    """Resets one block's values, or the whole factory chain."""
    app = request.app
    bid = request.query.get("block")
    st = copy.deepcopy(app["rt"]["state"])
    if bid:
        for b in st["blocks"]:
            if b["id"] == bid:
                b["params"] = defaults_for(app, b)
                spec = app["factory"].spec_for(b)
                b["bypass"] = spec["bypass"] if spec else False
    else:
        st = {"version": STATE_VERSION, "chain_bypass": False, "blocks": app["factory"].blocks()}
    await op_apply_state(app, st)
    return web.json_response({"ok": True})


async def h_response(request):
    """Block curve (measured with the real plugin) and combined curve of all graphable blocks."""
    app = request.app
    try:
        target = find_block(app, request.query["block"])
    except KeyError as e:
        return err(str(e).strip("'"), 404)
    st = app["rt"]["state"]
    views = {b["id"]: block_view(app, b) for b in st["blocks"]}
    if not views[target["id"]]["graph"]:
        return err("this block has no graph")
    parts = [b for b in st["blocks"] if views[b["id"]]["graph"] and not b["bypass"]
             and not views[b["id"]]["missing"]]

    def chans(b):
        return max(1, len(lv2meta.describe(b["uri"])["audio_in"]))

    try:
        curve = await response.measure(target["uri"], target["params"], chans(target))
    except Exception as e:  # noqa: BLE001
        log.warning("frequency response of %s: %s", target["id"], e)
        return err(f"could not compute the curve: {e}", 500)
    total, used = None, []
    if not st["chain_bypass"] and parts:
        res = await asyncio.gather(*(response.measure(b["uri"], b["params"], chans(b)) for b in parts),
                                   return_exceptions=True)
        ok = [(b, c) for b, c in zip(parts, res) if not isinstance(c, Exception)]
        if ok:
            total = [round(sum(c[k] for _, c in ok), 2) for k in range(len(curve))]
            used = [views[b["id"]]["name"] for b, _ in ok]
    return web.json_response({"freqs": response.freqs(), "block": curve,
                              "bypassed": target["bypass"] or st["chain_bypass"],
                              "total": total, "parts": used})


async def h_library(request):
    lib = await asyncio.get_running_loop().run_in_executor(None, lv2meta.library)
    return web.json_response({"plugins": lib})


async def h_presets(request):
    names = sorted(p.stem for p in presets_dir().glob("*.json"))
    return web.json_response({"presets": names})


async def h_preset_save(request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not SAFE_NAME.match(name):
        return err("invalid name")
    (presets_dir() / f"{name}.json").write_text(json.dumps(request.app["rt"]["state"], indent=1))
    return web.json_response({"ok": True})


async def h_preset_load(request):
    name = request.match_info["name"]
    f = presets_dir() / f"{name}.json"
    if not SAFE_NAME.match(name) or not f.exists():
        return err("preset does not exist", 404)
    await op_apply_state(request.app, json.loads(f.read_text()))
    return web.json_response({"ok": True})


async def h_preset_delete(request):
    name = request.match_info["name"]
    f = presets_dir() / f"{name}.json"
    if SAFE_NAME.match(name) and f.exists():
        f.unlink()
    return web.json_response({"ok": True})


async def h_cards(request):
    cards, raw = await list_cards()
    return web.json_response({"cards": cards, "raw": raw, "default": MIXER_CARD})


async def h_mixer(request):
    card = card_param(request)
    if not card:
        return web.json_response({"card": "", "controls": []})
    try:
        text = await run_cmd("amixer", "-M", "-c", card, "scontents")
        return web.json_response({"card": card, "controls": parse_scontents(text)})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"card": card, "controls": [], "error": str(e)})


async def h_mixer_set(request):
    b = await request.json()
    card = card_param(request, b)
    args = ["amixer", "-M", "-c", card, "-q", "sset", str(b["id"])]
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
        return err(str(e))
    return web.json_response({"ok": True})


async def h_packages(request):
    used = {b["uri"] for b in request.app["rt"]["state"]["blocks"]}
    return web.json_response(await packages.status(used))


async def after_packages_change(app):
    await asyncio.get_running_loop().run_in_executor(None, lv2meta.reload)
    packages.request_rescan()           # the engine restarts mod-host and the chain is reloaded
    await chain_changed(app)


async def h_package_install(request):
    body = await request.json()
    try:
        result = await packages.install(str(body.get("pkg", "")).strip())
    except packages.PackageError as e:
        return web.json_response({"error": str(e), "log": e.log}, status=400)
    await after_packages_change(request.app)
    return web.json_response(result)


async def h_package_remove(request):
    body = await request.json()
    pkg = str(body.get("pkg", "")).strip()
    used = {b["uri"] for b in request.app["rt"]["state"]["blocks"]}
    try:
        result = await packages.remove(pkg, used)
    except packages.PackageError as e:
        return web.json_response({"error": str(e), "log": e.log}, status=400)
    await after_packages_change(request.app)
    return web.json_response(result)


async def h_ws(request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    app["sockets"].add(ws)
    await ws.send_str(json.dumps({"type": "status", "online": app["engine"].online}))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
                t = m.get("type")
                if t == "param":
                    await op_param(app, m["block"], m["symbol"], m["value"], origin=ws)
                elif t == "bypass":
                    find_block(app, m["block"])["bypass"] = bool(m["value"])
                    await apply_bypass_all(app)
                    schedule_save(app)
                    await broadcast(app, m, exclude=ws)
                elif t == "chain_bypass":
                    app["rt"]["state"]["chain_bypass"] = bool(m["value"])
                    await apply_bypass_all(app)
                    schedule_save(app)
                    await broadcast(app, m, exclude=ws)
            except Exception as e:  # noqa: BLE001
                log.warning("invalid ws message: %s (%s)", msg.data[:200], e)
                await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
    finally:
        app["sockets"].discard(ws)
    return ws


async def on_startup(app):
    loop = asyncio.get_running_loop()
    app["tasks"] = [asyncio.create_task(app["engine"].run()),
                    asyncio.create_task(meter_ticker(app))]
    loop.run_in_executor(None, lv2meta.library)      # preload the library


async def on_shutdown(app):
    for ws in list(app["sockets"]):
        await ws.close()
    for t in app["tasks"]:
        t.cancel()
    await asyncio.gather(*app["tasks"], return_exceptions=True)
    save_state(app)


def make_app():
    lv2meta.reload()
    app = web.Application(middlewares=[auth_mw])
    app["factory"] = Factory(CHAIN_FILE)
    saved = {}
    sf = DATA_DIR / "state.json"
    if sf.exists():
        try:
            saved = json.loads(sf.read_text())
        except ValueError:
            log.warning("invalid state.json, using the factory chain")
    app["rt"] = {"oplock": asyncio.Lock()}
    app["rt"]["state"] = normalize_state(app, saved)
    app["sockets"] = set()
    app["engine"] = Engine(app)
    r = app.router
    r.add_get("/", h_index)
    r.add_get("/api/chain", h_chain)
    r.add_post("/api/chain/order", h_order)
    r.add_post("/api/chain/blocks", h_add)
    r.add_delete("/api/chain/blocks/{id}", h_remove)
    r.add_post("/api/reset", h_reset)
    r.add_get("/api/response", h_response)
    r.add_get("/api/library", h_library)
    r.add_get("/api/presets", h_presets)
    r.add_post("/api/presets", h_preset_save)
    r.add_post("/api/presets/{name}/load", h_preset_load)
    r.add_delete("/api/presets/{name}", h_preset_delete)
    r.add_get("/api/cards", h_cards)
    r.add_get("/api/mixer", h_mixer)
    r.add_post("/api/mixer", h_mixer_set)
    r.add_get("/api/packages", h_packages)
    r.add_post("/api/packages/install", h_package_install)
    r.add_post("/api/packages/remove", h_package_remove)
    r.add_get("/ws", h_ws)
    r.add_static("/static", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=HTTP_PORT, access_log=None, shutdown_timeout=2)
