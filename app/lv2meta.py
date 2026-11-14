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
            "optional": w.new_uri("http://lv2plug.in/ns/lv2core#connectionOptional"),
            "notonui": w.new_uri("http://lv2plug.in/ns/ext/port-props#notOnGUI"),
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
                # mandatory atom/event/CV ports: lv2apply does not support them
                if not p.has_property(_N["optional"]):
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
        extra_root = os.environ.get("EXTRA_LV2_DIR", "/data/lv2")
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
                out.append({
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
        out.sort(key=lambda x: (x["name"].lower(), x["uri"]))
        _library_cache = out
        return out


def bundle_dirs_of(uri: str) -> str:
    with _lock:
        p = _plugin(uri)
        return p.get_bundle_uri().get_path() or ""
