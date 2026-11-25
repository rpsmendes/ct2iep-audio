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
PRESETS_FILE = Path(os.environ.get("PRESETS_FILE", "") or CHAIN_FILE.with_name("presets.yaml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).with_name("static")
MAIN_CARD = os.environ.get("AUDIO_CARD", "")
MIXER_CARD = os.environ.get("MIXER_CARD") or MAIN_CARD
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


def slugify(text, maxlen=24):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t or "block")[:maxlen].strip("-")


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


def load_builtin(factory: Factory, path: Path):
    """Built-in presets (presets.yaml): each block starts from the factory block
    with the same id and only overrides the parameters it lists."""
    if not path.exists():
        return []
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        log.warning("invalid %s: %s", path, e)
        return []
    out = []
    for p in cfg.get("presets", []):
        blocks = []
        try:
            for b in p["blocks"]:
                spec = factory.specs.get(b["id"])
                uri = b.get("uri") or (spec and spec["uri"])
                if not uri:
                    raise ValueError(f"block {b['id']} has no uri")
                params = dict(spec["params"]) if spec and spec["uri"] == uri else {}
                params.update({k: parse_value(v) for k, v in (b.get("params") or {}).items()})
                blk = {"id": b["id"], "uri": uri, "bypass": bool(b.get("bypass", False)), "params": params}
                if b.get("name"):
                    blk["name"] = b["name"]
                blocks.append(blk)
        except (KeyError, TypeError, ValueError) as e:
            log.warning("built-in preset %r skipped: %s", p.get("name"), e)
            continue
        out.append({"slug": slugify(p["name"], 60), "name": p["name"], "mode": p.get("mode", ""),
                    "radio": p.get("radio", ""), "description": p.get("description", ""),
                    "state": {"version": STATE_VERSION, "chain_bypass": False, "blocks": blocks}})
    return out


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
            "levels": dict(app["rt"]["levels"], available=bool(app["engine"].trims) or not app["engine"].online,
                           min_db=TRIM_MIN_DB, max_db=TRIM_MAX_DB),
            "io": {"inputs": f.inputs, "outputs": f.outputs},
            "blocks": [block_view(app, b) for b in st["blocks"]]}


# ----------------------------------------------------------------------------
# Outputs: where the processed audio goes
# ----------------------------------------------------------------------------
# data/engine/outputs.txt has one "CARD DEVICE" line per selected playback
# device. The main card (AUDIO_CARD, device 0) is JACK's own device; any other
# device gets a zita-j2a bridge (adaptive resampling) started by engine.sh,
# whose JACK client is called out_<CARD>_<DEVICE>. engine.sh reports the
# bridges in data/engine/bridges.status.
def engine_dir():
    d = DATA_DIR / "engine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_outputs():
    f = DATA_DIR / "engine" / "outputs.txt"
    out = []
    if f.exists():
        for ln in f.read_text().splitlines():
            parts = ln.split()
            if parts and SAFE_CARD.match(parts[0]):
                dev = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                if (parts[0], dev) not in out:
                    out.append((parts[0], dev))
    return out or [(MAIN_CARD, 0)]


def save_outputs(outs):
    f = engine_dir() / "outputs.txt"
    tmp = f.with_suffix(".tmp")
    tmp.write_text("".join(f"{c} {d}\n" for c, d in outs))
    tmp.replace(f)


# Virtual output "ZEUS 0": the chain's audio goes to Zeus (Square SDR) as TCI TX
# audio (engine.sh starts app/zeus_tci.py, JACK client out_ZEUS_0, same ports as
# a bridge). Shown in the UI unless ZEUS_TCI_URL is set empty in .env.
ZEUS_TCI_URL = os.environ.get("ZEUS_TCI_URL", "ws://zeus:40001").strip()
VIRTUAL_OUTPUTS = [
    {"card": "ZEUS", "device": 0, "label": "Zeus · Square SDR (TCI)",
     "sub": f"TX audio to Zeus over TCI ({ZEUS_TCI_URL}), used whenever Zeus is in TX"}
] if ZEUS_TCI_URL else []


def bridge_name(card, dev):
    return f"out_{card}_{dev}"


def is_main(card, dev):
    return card == MAIN_CARD and dev == 0


def output_ports(app, card, dev):
    if is_main(card, dev):
        return app["factory"].outputs
    n = bridge_name(card, dev)
    return [f"{n}:playback_1", f"{n}:playback_2"]


def bridge_status():
    f = DATA_DIR / "engine" / "bridges.status"
    st = {}
    try:
        for ln in f.read_text().splitlines():
            parts = ln.split(" ", 2)
            if len(parts) >= 2:
                st[parts[0]] = {"state": parts[1], "detail": parts[2] if len(parts) > 2 else ""}
    except OSError:
        pass
    return st


# ----------------------------------------------------------------------------
# Input/output trims: fixed gain stages outside the chain
# ----------------------------------------------------------------------------
# Card input -> [input trim] -> chain -> [output trim] -> outputs. They are always
# loaded (also with A/B: the trims set the levels, not the sound) and they are
# not part of presets: they match the hardware (card, radio input) and are
# stored in data/levels.json. Calf Mono Input: mono in, L/R out, gain +-36 dB,
# peak meters and a mute per output channel ("left channel only").
TRIM_URI = "http://calf.sourceforge.net/plugins/MonoInput"
TRIM_MIN_DB, TRIM_MAX_DB = -36.0, 12.0
LEVELS_DEFAULT = {"in_db": 0.0, "out_db": 0.0, "left_only": False}


def load_levels():
    lv = dict(LEVELS_DEFAULT)
    try:
        lv.update(json.loads((DATA_DIR / "levels.json").read_text()))
    except (OSError, ValueError):
        pass
    return clean_levels(lv)


def clean_levels(lv):
    out = {}
    for k in ("in_db", "out_db"):
        try:
            out[k] = round(min(TRIM_MAX_DB, max(TRIM_MIN_DB, float(lv.get(k, 0.0)))), 1)
        except (TypeError, ValueError):
            out[k] = 0.0
    out["left_only"] = bool(lv.get("left_only", False))
    return out


def save_levels(lv):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = DATA_DIR / "levels.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(lv))
    tmp.replace(f)


def trim_params(stage, lv):
    """Gain on level_out: the plugin's input meter then shows the level before
    the trim (for the input stage: the card's own level, to spot clipping)."""
    g = 10 ** (lv["in_db" if stage == "in" else "out_db"] / 20)
    return {"level_in": 1.0, "level_out": g, "softclip": 0.0, "mutel": 0.0,
            "muter": 1.0 if (stage == "out" and lv["left_only"]) else 0.0}


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
        self.failed = {}          # block id -> uri that failed to load (not retried)
        self.meter_values = {}
        self.trims = {}           # "in"/"out" -> mod-host instance (always loaded)

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

    def wanted(self, b):
        """Only blocks that are switched on run in mod-host. A bypassed block is
        unloaded and the signal is wired around it: mod-host's own bypass keeps
        calling the plugin every cycle, so it would still use CPU."""
        return not self.app["rt"]["state"]["chain_bypass"] and not b["bypass"]

    async def load_block(self, b):
        self.errors.pop(b["id"], None)
        if not plugin_available(b["uri"]):
            self.errors[b["id"]] = "plugin not installed"
            self.failed[b["id"]] = b["uri"]
            return False
        i = self.next_inst
        self.next_inst += 1
        r = await self.cmd(f'add "{b["uri"]}" {i}')
        if r.startswith("resp -"):
            self.errors[b["id"]] = f"mod-host refused the plugin ({r[5:]})"
            self.failed[b["id"]] = b["uri"]
            return False
        self.inst[b["id"]] = i
        for s, v in b["params"].items():
            await self.cmd(f"param_set {i} {s} {v:.6g}", quiet=True)
        meta = lv2meta.describe(b["uri"])
        for m in block_view(self.app, b)["meters"]:
            if m["symbol"] in meta["outputs"]:
                await self.cmd(f"monitor_output {i} {m['symbol']}")
        return True

    async def unload_block(self, bid):
        i = self.inst.pop(bid, None)
        self.errors.pop(bid, None)
        self.failed.pop(bid, None)
        if i is None:
            return
        pref = f"effect_{i}:"
        self.links = {l for l in self.links if not (l[0].startswith(pref) or l[1].startswith(pref))}
        for k in [k for k in self.meter_values if k[0] == i]:
            del self.meter_values[k]
        await self.cmd(f"remove {i}")

    async def load_trims(self):
        self.trims = {}
        if not plugin_available(TRIM_URI):
            log.warning("input/output trims unavailable: %s not installed", TRIM_URI)
            return
        lv = self.app["rt"]["levels"]
        for stage in ("in", "out"):
            i = self.next_inst
            self.next_inst += 1
            r = await self.cmd(f'add "{TRIM_URI}" {i}')
            if r.startswith("resp -"):
                log.warning("input/output trims unavailable: mod-host refused the plugin (%s)", r[5:])
                return
            self.trims[stage] = i
            for sym, v in trim_params(stage, lv).items():
                await self.cmd(f"param_set {i} {sym} {v:.6g}", quiet=True)
            for sym in ("meter_in", "meter_outL", "clip_in", "clip_outL"):
                await self.cmd(f"monitor_output {i} {sym}", quiet=True)

    async def apply_levels(self):
        lv = self.app["rt"]["levels"]
        for stage, i in self.trims.items():
            for sym, v in trim_params(stage, lv).items():
                await self.cmd(f"param_set {i} {sym} {v:.6g}", quiet=True)

    def desired_links(self):
        f: Factory = self.app["factory"]
        src = f.inputs[:1]
        links = set()
        if "in" in self.trims:
            i = self.trims["in"]
            for s in src:
                links.add((s, f"effect_{i}:in_l"))
            src = [f"effect_{i}:out_l"]
        for b in self.app["rt"]["state"]["blocks"]:
            i = self.inst.get(b["id"])
            if i is None or not self.wanted(b):
                continue
            meta = lv2meta.describe(b["uri"])
            for s in src:
                for a in meta["audio_in"]:
                    links.add((s, f"effect_{i}:{a}"))
            src = [f"effect_{i}:{meta['audio_out'][0]}"]
        if "out" in self.trims:
            i = self.trims["out"]
            for s in src:
                links.add((s, f"effect_{i}:in_l"))
            # L -> first port, R -> second (R muted with "left channel only")
            for card, dev in self.app["rt"]["outputs"]:
                for n, o in enumerate(output_ports(self.app, card, dev)):
                    links.add((f"effect_{i}:{'out_l' if n % 2 == 0 else 'out_r'}", o))
            return links
        for card, dev in self.app["rt"]["outputs"]:
            for s in src:
                for o in output_ports(self.app, card, dev):
                    links.add((s, o))
        return links

    async def rewire(self):
        """Applies only the differences: disconnect first (brief silence), then connect."""
        new = self.desired_links()
        for a, b in sorted(self.links - new):
            await self.cmd(f'disconnect "{a}" "{b}"', quiet=True)
        for a, b in sorted(new - self.links):
            # bridge ports may not exist yet: keep_bridges() retries them
            await self.cmd(f'connect "{a}" "{b}"', quiet=b.startswith("out_"))
        self.links = new

    async def keep_bridges(self):
        """Output bridges come and go (started late, card unplugged and back):
        reissue their connections; connect is a no-op when already connected."""
        for a, b in sorted(self.links):
            if b.startswith("out_"):
                await self.cmd(f'connect "{a}" "{b}"', quiet=True)

    async def sync(self):
        """Brings mod-host in line with the state: loads the blocks that must run,
        rewires, and only then unloads the blocks that must not run (bypassed or
        removed), so the signal never stops while plugins are being loaded."""
        blocks = self.app["rt"]["state"]["blocks"]
        for b in blocks:
            if self.wanted(b) and b["id"] not in self.inst and self.failed.get(b["id"]) != b["uri"]:
                await self.load_block(b)
        await self.rewire()
        keep = {b["id"] for b in blocks if self.wanted(b)}
        for bid in [k for k in self.inst if k not in keep]:
            await self.unload_block(bid)

    async def build(self):
        await self.cmd("remove -1")
        self.inst, self.links, self.errors, self.failed, self.next_inst = {}, set(), {}, {}, 0
        await self.load_trims()
        await self.sync()
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


async def op_levels(app, m):
    """Input/output trims and "left channel only" (live, saved right away)."""
    lv = clean_levels({**app["rt"]["levels"], **{k: m[k] for k in ("in_db", "out_db", "left_only") if k in m}})
    app["rt"]["levels"] = lv
    eng: Engine = app["engine"]
    if eng.online:
        async with app["rt"]["oplock"]:
            await eng.apply_levels()
    save_levels(lv)


async def op_bypass(app, bid, value):
    """Block bypass (bid) or A/B (bid None): loads/unloads plugins accordingly."""
    async with app["rt"]["oplock"]:
        if bid is None:
            app["rt"]["state"]["chain_bypass"] = bool(value)
        else:
            find_block(app, bid)["bypass"] = bool(value)
        if app["engine"].online:
            await app["engine"].sync()
    schedule_save(app)


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
            await eng.sync()
    await chain_changed(app)
    return b["id"]


async def op_remove(app, bid):
    async with app["rt"]["oplock"]:
        st = app["rt"]["state"]
        find_block(app, bid)
        eng: Engine = app["engine"]
        st["blocks"] = [b for b in st["blocks"] if b["id"] != bid]
        if eng.online:
            await eng.sync()
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
                if i is not None:
                    for s, v in b["params"].items():
                        await eng.cmd(f"param_set {i} {s} {v:.6g}", quiet=True)
            await eng.sync()
    await chain_changed(app)


async def bridge_keeper(app):
    while True:
        await asyncio.sleep(3)
        eng: Engine = app["engine"]
        if eng.online and any(not is_main(c, d) for c, d in app["rt"]["outputs"]):
            try:
                async with app["rt"]["oplock"]:
                    await eng.keep_bridges()
            except Exception as e:  # noqa: BLE001
                log.debug("bridge keeper: %r", e)


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
        for stage, i in eng.trims.items():
            for sym, key in (("meter_in", "pre"), ("meter_outL", "post")):
                v = eng.meter_values.get((i, sym))
                if v is not None:
                    out[f"_{stage}.{key}"] = round(20 * math.log10(v), 1) if v > 1e-6 else -120.0
            for sym, key in (("clip_in", "clip_pre"), ("clip_outL", "clip_post")):
                v = eng.meter_values.get((i, sym))
                if v is not None:
                    out[f"_{stage}.{key}"] = 1 if v > 0.5 else 0
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


@web.middleware
async def static_cache_mw(request, handler):
    """CSS/JS are revalidated on every load (304 when unchanged), so a new
    version of the UI shows up after an update without a forced reload."""
    resp = await handler(request)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


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

    def bundle(b):
        return lv2meta.bundle_dirs_of(b["uri"])

    try:
        curve = await response.measure(target["uri"], target["params"], bundle(target))
    except Exception as e:  # noqa: BLE001
        log.warning("frequency response of %s: %s", target["id"], e)
        return err(f"could not compute the curve: {e}", 500)
    total, used = None, []
    if not st["chain_bypass"] and parts:
        res = await asyncio.gather(*(response.measure(b["uri"], b["params"], bundle(b)) for b in parts),
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
    builtin = [{k: p[k] for k in ("slug", "name", "mode", "radio", "description")}
               for p in request.app["builtin"]]
    return web.json_response({"presets": names, "builtin": builtin})


async def h_builtin_load(request):
    slug = request.match_info["slug"]
    for p in request.app["builtin"]:
        if p["slug"] == slug:
            await op_apply_state(request.app, copy.deepcopy(p["state"]))
            return web.json_response({"ok": True})
    return err("preset does not exist", 404)


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


async def h_outputs(request):
    app = request.app
    st = bridge_status()
    out = []
    for c, d in app["rt"]["outputs"]:
        item = {"card": c, "device": d, "main": is_main(c, d)}
        if not item["main"]:
            b = st.get(bridge_name(c, d), {"state": "starting", "detail": ""})
            item.update(b)
        out.append(item)
    return web.json_response({"main": MAIN_CARD, "selected": out, "virtual": VIRTUAL_OUTPUTS})


async def h_outputs_set(request):
    app = request.app
    body = await request.json()
    outs = []
    for o in body.get("selected") or []:
        c, d = str(o.get("card", "")), o.get("device", 0)
        if not SAFE_CARD.match(c) or not isinstance(d, int) or not 0 <= d < 64:
            return err("invalid output")
        if (c, d) not in outs:
            outs.append((c, d))
    if not outs:
        return err("select at least one output")
    async with app["rt"]["oplock"]:
        app["rt"]["outputs"] = outs
        save_outputs(outs)
        if app["engine"].online:
            await app["engine"].rewire()
    log.info("outputs: %s", ", ".join(f"{c},{d}" for c, d in outs))
    return await h_outputs(request)


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
                    await op_bypass(app, m["block"], m["value"])
                    await broadcast(app, m, exclude=ws)
                    await broadcast(app, {"type": "chain", "chain": chain_view(app)})
                elif t == "levels":
                    await op_levels(app, m)
                    await broadcast(app, {"type": "levels", "levels": app["rt"]["levels"]}, exclude=ws)
                elif t == "chain_bypass":
                    await op_bypass(app, None, m["value"])
                    await broadcast(app, m, exclude=ws)
                    await broadcast(app, {"type": "chain", "chain": chain_view(app)})
            except Exception as e:  # noqa: BLE001
                log.warning("invalid ws message: %s (%s)", msg.data[:200], e)
                await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
    finally:
        app["sockets"].discard(ws)
    return ws


async def on_startup(app):
    # The plugin library is built lazily on the first /api/library request:
    # scanning ~300 plugins takes a couple of CPU seconds and must not compete
    # with mod-host while it loads the chain at startup.
    app["tasks"] = [asyncio.create_task(app["engine"].run()),
                    asyncio.create_task(meter_ticker(app)),
                    asyncio.create_task(bridge_keeper(app))]


async def on_shutdown(app):
    for ws in list(app["sockets"]):
        await ws.close()
    for t in app["tasks"]:
        t.cancel()
    await asyncio.gather(*app["tasks"], return_exceptions=True)
    save_state(app)


def make_app():
    packages.migrate_layout()
    lv2meta.reload()
    app = web.Application(middlewares=[auth_mw, static_cache_mw])
    app["factory"] = Factory(CHAIN_FILE)
    app["builtin"] = load_builtin(app["factory"], PRESETS_FILE)
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
    app["rt"]["outputs"] = load_outputs()
    app["rt"]["levels"] = load_levels()
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
    r.add_post("/api/presets/builtin/{slug}/load", h_builtin_load)
    r.add_post("/api/presets/{name}/load", h_preset_load)
    r.add_delete("/api/presets/{name}", h_preset_delete)
    r.add_get("/api/cards", h_cards)
    r.add_get("/api/outputs", h_outputs)
    r.add_post("/api/outputs", h_outputs_set)
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
    # The web UI, the EQ graph measurements and package installs must never take
    # CPU time away from the real-time audio engine: run them all at a lower
    # priority (children inherit it).
    try:
        os.nice(int(os.environ.get("WEB_NICE", "10")))
    except (OSError, ValueError) as e:
        log.warning("could not lower the web process priority: %s", e)
    web.run_app(make_app(), host="0.0.0.0", port=HTTP_PORT, access_log=None, shutdown_timeout=2)
