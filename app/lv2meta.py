"""LV2 metadata (lilv): plugin descriptions, library and reloading.

The lilv world can be reloaded (after installing/removing plugin packages),
so everything goes through functions in this module instead of a directly
imported global.
"""
import logging
import os
import re
import threading

import lilv

log = logging.getLogger("audio-web")

# Control ports that only serve the plugins' native GUI (hidden in the UI)
HIDDEN = re.compile(
    r"^(enabled|bypass|pause|clear|zoom|react|shift|fsel|ife|ofe|frqs|listen|sc_listen"
    r"|insp_.*|hue_\d+|xs_\d+|xm_\d+|slv|elv|grv|ilv|olv|clv|igv|ogv|scgv|grgv)$"
)

# LV2 classes with a linear frequency response (they get a graph)
GRAPH_CLASSES = {
    "http://lv2plug.in/ns/lv2core#EQPlugin",
    "http://lv2plug.in/ns/lv2core#ParaEQPlugin",
    "http://lv2plug.in/ns/lv2core#MultiEQPlugin",
    "http://lv2plug.in/ns/lv2core#FilterPlugin",
    "http://lv2plug.in/ns/lv2core#LowpassPlugin",
    "http://lv2plug.in/ns/lv2core#HighpassPlugin",
    "http://lv2plug.in/ns/lv2core#BandpassPlugin",
    "http://lv2plug.in/ns/lv2core#AllpassPlugin",
}

# ---- Which plugins make sense in this chain ----
# The chain is one mono voice path: card input -> blocks -> radio. A plugin is
# offered by default only if it processes that signal by itself. The rules use
# the LV2 metadata (class, ports, file parameters) plus the name for families
# the metadata does not tell apart (meters, mixers…). Checked against every
# plugin of the image on 2026-11-25: all load in mod-host; meters left the
# test signal bit-identical; sidechain/mixer/generator plugins did not process it.
USABLE = {"voice", "effect"}
CATEGORY_LABEL = {
    "voice": "voice processing",
    "effect": "creative effect",
    "stereo": "stereo version: use the mono one",
    "visual": "visual only (meter / analyser)",
    "sidechain": "needs a sidechain signal",
    "file": "needs a file (impulse response, sample)",
    "multi": "needs several inputs",
    "routing": "stereo / multi-output utility",
    "generator": "makes its own sound (generator)",
    "midi": "needs MIDI",
    "instrument": "instrument (MIDI)",
}
_RX = {
    "visual": re.compile(r"meter|scope|tuner|spectr|analy[sz]|phase detector|histogram|phase.?wheel|surround level|pitch tools", re.I),
    "file": re.compile(r"convol|impulse|room builder|profiler", re.I),
    "multi": re.compile(r"mixer|a/b tester|x-fade|matrix|vocoder|envelope filter", re.I),
    "generator": re.compile(r"noise generator|oscillator", re.I),
    "midi": re.compile(r"filterclavier|trigger", re.I),
    "routing": re.compile(r"haas|spread|stereo routing|balance|x-over|crossover|rotary|delay compensator|delay line|compensation delay", re.I),
    "voice": re.compile(r"equali|\beq\b|filter|compress|limiter|gate|expander|dynamics|de-?esser|exciter|enhancer|saturat|clipper|autogain|loudness comp|phaserotate|surge|emphasis|tape|crusher|fil4|dpl|mono input", re.I),
}
_STEREO_WORDS = re.compile(r"\s*\b(stereo|mono|x2|leftright|midside|left/right|mid/side|l/r|m/s)\b", re.I)


def _categorize(plugin, name, n_in, n_sc):
    classes = set(_class_chain(plugin))
    if "http://lv2plug.in/ns/lv2core#InstrumentPlugin" in classes:
        return "instrument"
    if not re.search(r"loudness compensator", name, re.I) and (
            "http://lv2plug.in/ns/lv2core#AnalyserPlugin" in classes or _RX["visual"].search(name)):
        return "visual"
    if _world.ask(plugin.get_uri(), _N["patch_writable"], None) or _RX["file"].search(name):
        return "file"
    if n_sc or re.search(r"sidechain", name, re.I):
        return "sidechain"
    if n_in > 2 or _RX["multi"].search(name):
        return "multi"
    for cat in ("generator", "midi", "routing"):
        if _RX[cat].search(name):
            return cat
    return "voice" if _RX["voice"].search(name) else "effect"


# Meters recognised automatically in blocks added from the UI
AUTO_METERS = [("grlm", "gr"), ("rlm", "gr"), ("compression", "gr")]

_lock = threading.RLock()
_world = None
_N = {}
_desc_cache = {}
_library_cache = None


def _f(node, fallback=0.0):
    if node is None:
        return fallback
    try:
        return float(str(node))
    except (TypeError, ValueError):
        return fallback


def reload():
    """(Re)loads every LV2 bundle in LV2_PATH."""
    global _world, _N, _library_cache
    with _lock:
        w = lilv.World()
        w.load_all()
        _N = {
            "audio": w.new_uri(lilv.LILV_URI_AUDIO_PORT),
            "control": w.new_uri(lilv.LILV_URI_CONTROL_PORT),
            "input": w.new_uri(lilv.LILV_URI_INPUT_PORT),
            "output": w.new_uri(lilv.LILV_URI_OUTPUT_PORT),
            "unit": w.new_uri("http://lv2plug.in/ns/extensions/units#unit"),
            "usym": w.new_uri("http://lv2plug.in/ns/extensions/units#symbol"),
            "log": w.new_uri("http://lv2plug.in/ns/ext/port-props#logarithmic"),
            "enum": w.new_uri("http://lv2plug.in/ns/lv2core#enumeration"),
            "toggled": w.new_uri("http://lv2plug.in/ns/lv2core#toggled"),
            "integer": w.new_uri("http://lv2plug.in/ns/lv2core#integer"),
            "sidechain": w.new_uri("http://lv2plug.in/ns/lv2core#isSideChain"),
            "atom": w.new_uri("http://lv2plug.in/ns/ext/atom#AtomPort"),
            "cv": w.new_uri(lilv.LILV_URI_CV_PORT),
            "optional": w.new_uri("http://lv2plug.in/ns/lv2core#connectionOptional"),
            "notonui": w.new_uri("http://lv2plug.in/ns/ext/port-props#notOnGUI"),
            "patch_writable": w.new_uri("http://lv2plug.in/ns/ext/patch#writable"),
        }
        _world = w
        _desc_cache.clear()
        _library_cache = None
        log.info("LV2: %d plugins loaded", len(w.get_all_plugins()))


def _plugin(uri):
    if _world is None:
        reload()
    p = _world.get_all_plugins()[_world.new_uri(uri)]
    if p is None:
        raise KeyError(f"LV2 plugin not found: {uri}")
    return p


def _class_chain(plugin):
    """URIs of the plugin class and its parent classes."""
    out = []
    classes = _world.get_plugin_classes()
    cls = plugin.get_class()
    for _ in range(6):
        if cls is None:
            break
        out.append(str(cls.get_uri()))
        parent = cls.get_parent_uri()
        if parent is None:
            break
        out.append(str(parent))
        try:
            cls = classes[parent]
        except KeyError:
            break
    return out


def describe(uri: str) -> dict:
    """Ports and metadata of a plugin (cached)."""
    with _lock:
        if uri in _desc_cache:
            return _desc_cache[uri]
        plugin = _plugin(uri)
        d = {"uri": uri, "name": str(plugin.get_name()), "audio_in": [], "sidechain_in": [],
             "audio_out": [], "controls": [], "outputs": {},
             "class": str(plugin.get_class().get_label()),
             "eq_like": bool(GRAPH_CLASSES.intersection(_class_chain(plugin))),
             "measurable": True}
        for i in range(plugin.get_num_ports()):
            p = plugin.get_port_by_index(i)
            sym = str(p.get_symbol())
            is_in = p.is_a(_N["input"])
            if p.is_a(_N["audio"]):
                if not is_in:
                    d["audio_out"].append(sym)
                elif p.has_property(_N["sidechain"]) or sym.startswith("sc"):
                    d["sidechain_in"].append(sym)
                else:
                    d["audio_in"].append(sym)
                continue
            if not p.is_a(_N["control"]):
                # atom and CV ports are fine for the measurement host;
                # anything else (e.g. old event ports) is not
                if not (p.is_a(_N["atom"]) or p.is_a(_N["cv"])):
                    d["measurable"] = False
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
            if pts and (info["enum"] or info["integer"]):
                info["enum"] = True
                info["points"] = sorted(
                    ({"label": str(x.get_label()), "value": float(str(x.get_value()))} for x in pts),
                    key=lambda x: x["value"])
            if is_in:
                if not HIDDEN.match(sym) and not p.has_property(_N["notonui"]):
                    d["controls"].append(info)
            else:
                d["outputs"][sym] = info
        d["graph"] = d["eq_like"] and d["measurable"]
        _desc_cache[uri] = d
        return d


def auto_meters(meta: dict) -> list:
    for sym, kind in AUTO_METERS:
        if sym in meta["outputs"]:
            return [{"symbol": sym, "label": meta["name"][:18], "kind": kind}]
    return []


def library() -> list:
    """Plugins usable in an audio chain (≥1 audio input and ≥1 audio output)."""
    global _library_cache
    with _lock:
        if _library_cache is not None:
            return _library_cache
        if _world is None:
            reload()
        out = []
        extra_root = os.environ.get("EXTRA_LV2_ROOT", "/data/lv2")
        for plugin in _world.get_all_plugins():
            try:
                n_in = n_out = n_sc = 0
                for i in range(plugin.get_num_ports()):
                    p = plugin.get_port_by_index(i)
                    if not p.is_a(_N["audio"]):
                        continue
                    if p.is_a(_N["input"]):
                        if p.has_property(_N["sidechain"]) or str(p.get_symbol()).startswith("sc"):
                            n_sc += 1
                        else:
                            n_in += 1
                    else:
                        n_out += 1
                if n_in < 1 or n_out < 1:
                    continue
                bundle = plugin.get_bundle_uri().get_path() or ""
                author = plugin.get_author_name()
                name = str(plugin.get_name())
                out.append({
                    "category": _categorize(plugin, name, n_in, n_sc),
                    "uri": str(plugin.get_uri()),
                    "name": str(plugin.get_name()),
                    "class": str(plugin.get_class().get_label()),
                    "author": str(author) if author is not None else "",
                    "ins": n_in, "outs": n_out, "sidechain": n_sc,
                    "bundle": os.path.basename(bundle.rstrip("/")),
                    "extra": bundle.startswith(extra_root),
                })
            except Exception as e:  # noqa: BLE001
                log.debug("library: skipped %s (%s)", plugin.get_uri(), e)
        # stereo / L-R / M-S versions of a plugin that also exists in mono:
        # the chain is mono, the mono one does the same with half the work
        monos = {(p["bundle"], _STEREO_WORDS.sub("", p["name"]).strip().lower())
                 for p in out if p["ins"] == 1}
        for p in out:
            if p["ins"] == 2 and p["category"] in USABLE and \
                    (p["bundle"], _STEREO_WORDS.sub("", p["name"]).strip().lower()) in monos:
                p["category"] = "stereo"
        for p in out:
            p["usable"] = p["category"] in USABLE
            p["category_label"] = CATEGORY_LABEL[p["category"]]
        out.sort(key=lambda x: (x["name"].lower(), x["uri"]))
        _library_cache = out
        return out


def bundle_dirs_of(uri: str) -> str:
    with _lock:
        p = _plugin(uri)
        return p.get_bundle_uri().get_path() or ""
