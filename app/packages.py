"""LV2 plugin packages installed on demand (without rebuilding the image).

A Debian package is downloaded (apt-get download) and unpacked, and only its
LV2 bundles are copied to /data/lv2, which is persistent and is in the
LV2_PATH of both the engine and the UI. Shared libraries the plugins need that
are not in the image go to /data/lv2/.lib (in LD_LIBRARY_PATH). Each package
has a manifest in /data/lv2/.packages/<package>.json so it can be removed.

After installing/removing, the engine restarts mod-host (.rescan file) so it
sees the new plugins; the chain is reloaded automatically.
"""
import asyncio
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

log = logging.getLogger("audio-web")

LV2_DIR = Path(os.environ.get("EXTRA_LV2_DIR", "/data/lv2"))
LIB_DIR = LV2_DIR / ".lib"
MANIFESTS = LV2_DIR / ".packages"
RESCAN = LV2_DIR / ".rescan"
SYSTEM_LV2 = [Path("/usr/lib/lv2"), Path("/usr/local/lib/lv2")]
SAFE_PKG = re.compile(r"^[a-z0-9][a-z0-9+.\-]{1,60}$")

# Packages already included in the image
BASE = {"lsp-plugins-lv2", "calf-plugins", "x42-plugins", "dragonfly-reverb-lv2"}

# Suggested catalog (aimed at voice/SSB). Any other Debian package with LV2
# plugins can also be installed by name.
CATALOG = [
    {"pkg": "lsp-plugins-lv2", "title": "LSP Plugins",
     "desc": "EQs, compressors, gates, limiters, filters (the base of the chain)."},
    {"pkg": "calf-plugins", "title": "Calf Studio Gear",
     "desc": "Exciter, de-esser, saturator, bass enhancer, EQs, reverbs."},
    {"pkg": "x42-plugins", "title": "x42 (Robin Gareus)",
     "desc": "fil4 EQ, dpl limiter, meters, tuner, utilities."},
    {"pkg": "dragonfly-reverb-lv2", "title": "Dragonfly Reverb",
     "desc": "Plate, room and hall reverb."},
    {"pkg": "zam-plugins", "title": "ZAM Plugins",
     "desc": "ZamComp, ZamGate, ZamEQ2, ZaMaximX2 (limiter), ZamAutoSat: lightweight dynamics and EQ."},
    {"pkg": "swh-lv2", "title": "SWH (Steve Harris)",
     "desc": "Classics: SC4 compressor, Fast Lookahead Limiter, Dyson compressor, filters."},
    {"pkg": "mda-lv2", "title": "MDA",
     "desc": "De-esser, Dynamics, Limiter, Combo, EQ and other classics."},
    {"pkg": "fomp", "title": "FOMP (Fons Adriaensen)",
     "desc": "Very clean parametric EQs and filters (1/2/4 bands, HP/LP)."},
    {"pkg": "eq10q", "title": "EQ10Q",
     "desc": "Parametric EQ with up to 10 bands, gate and compressor."},
    {"pkg": "invada-studio-plugins-lv2", "title": "Invada Studio",
     "desc": "Compressor, filters, tube distortion, delay."},
    {"pkg": "abgate", "title": "abGate", "desc": "Simple noise gate."},
]

_lock = asyncio.Lock()


class PackageError(Exception):
    def __init__(self, msg, log_lines=None):
        super().__init__(msg)
        self.log = log_lines or []


def _env():
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{LIB_DIR}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
    env["DEBIAN_FRONTEND"] = "noninteractive"
    return env


async def _run(args, logl, cwd=None, check=True, timeout=300):
    logl.append("$ " + " ".join(args))
    p = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=_env(),
                                             stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), timeout)
    text = out.decode(errors="replace")
    tail = [ln for ln in text.splitlines() if ln.strip()][-6:]
    logl.extend("  " + ln for ln in tail)
    if check and p.returncode != 0:
        raise PackageError(f"failed: {' '.join(args[:3])}…", logl)
    return p.returncode, text


def _manifest(pkg):
    f = MANIFESTS / f"{pkg}.json"
    return json.loads(f.read_text()) if f.exists() else None


def _manifests():
    return [json.loads(f.read_text()) for f in sorted(MANIFESTS.glob("*.json"))] if MANIFESTS.exists() else []


async def _dpkg_installed(pkg):
    p = await asyncio.create_subprocess_exec("dpkg-query", "-W", "-f=${Status}", pkg,
                                             stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return p.returncode == 0 and b"install ok installed" in out


async def _ensure_lists(logl):
    lists = glob.glob("/var/lib/apt/lists/*_Packages*")
    fresh = lists and (time.time() - max(os.path.getmtime(f) for f in lists) < 12 * 3600)
    if not fresh:
        await _run(["apt-get", "update", "-qq"], logl, timeout=180)


def _bundles_in(root):
    found = []
    for pattern in ("usr/lib/lv2/*.lv2", "usr/lib/*/lv2/*.lv2", "usr/local/lib/lv2/*.lv2"):
        found += [Path(p) for p in glob.glob(os.path.join(root, pattern)) if os.path.isdir(p)]
    return found


async def _missing_libs(paths, logl):
    """Missing libraries in the plugins' .so files (GUI .so files are ignored)."""
    missing = set()
    for so in paths:
        name = so.name.lower()
        if "_ui" in name or "gui" in name or name.endswith("ui.so"):
            continue
        _, text = await _run(["ldd", str(so)], [], check=False)
        for ln in text.splitlines():
            if "not found" in ln:
                missing.add(ln.split("=>")[0].strip())
    return missing


async def _depends(pkg, logl):
    _, text = await _run(["apt-cache", "depends", "--no-recommends", "--no-suggests",
                          "--no-conflicts", "--no-breaks", "--no-replaces", "--no-enhances", pkg],
                         [], check=False)
    deps = []
    for ln in text.splitlines():
        m = re.match(r"\s*\|?(?:Pre)?Depends:\s*<?([a-z0-9][a-z0-9+.\-]+)>?", ln)
        if m:
            deps.append(m.group(1))
    return deps


async def status(used_uris=()):
    import lv2meta  # avoids a circular import at startup
    installed = {m["pkg"]: m for m in _manifests()}
    in_use = {}
    for uri in used_uris:
        try:
            bdir = os.path.basename(lv2meta.bundle_dirs_of(uri).rstrip("/"))
        except KeyError:
            continue
        for m in installed.values():
            if bdir in m["bundles"]:
                in_use.setdefault(m["pkg"], []).append(uri)
    items = []
    names = [c["pkg"] for c in CATALOG] + [p for p in installed if p not in {c["pkg"] for c in CATALOG}]
    by_name = {c["pkg"]: c for c in CATALOG}
    for pkg in names:
        c = by_name.get(pkg, {"pkg": pkg, "title": pkg, "desc": "Installed manually."})
        m = installed.get(pkg)
        state = "base" if pkg in BASE else ("installed" if m else "available")
        items.append({**c, "state": state, "version": m["version"] if m else None,
                      "bundles": m["bundles"] if m else [], "missing_libs": m.get("missing_libs", []) if m else [],
                      "in_use": bool(in_use.get(pkg))})
    return {"packages": items}


async def install(pkg):
    if not SAFE_PKG.match(pkg):
        raise PackageError("invalid package name")
    if pkg in BASE or await _dpkg_installed(pkg):
        raise PackageError(f"{pkg} is already included in the image")
    if _manifest(pkg):
        raise PackageError(f"{pkg} is already installed")
    async with _lock:
        logl = []
        await _ensure_lists(logl)
        _, pol = await _run(["apt-cache", "policy", pkg], logl, check=False)
        cand = re.search(r"Candidate:\s*(\S+)", pol)
        if not cand or cand.group(1) == "(none)":
            raise PackageError(f"package {pkg} does not exist in this Debian release", logl)
        version = cand.group(1)
        LV2_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=LV2_DIR, prefix=".tmp-") as tmp:
            dl = Path(tmp) / "deb"
            dl.mkdir()
            await _run(["apt-get", "download", "-o", "APT::Sandbox::User=root", pkg], logl, cwd=dl)
            x = Path(tmp) / "x"
            for deb in dl.glob("*.deb"):
                await _run(["dpkg-deb", "-x", str(deb), str(x)], logl)
            bundles = _bundles_in(x)
            if not bundles:
                raise PackageError(f"package {pkg} does not contain LV2 plugins", logl)
            copied, skipped = [], []
            for b in bundles:
                if any((d / b.name).exists() for d in SYSTEM_LV2):
                    skipped.append(b.name)
                    continue
                dest = LV2_DIR / b.name
                if dest.exists():
                    owner = next((m["pkg"] for m in _manifests() if b.name in m["bundles"]), None)
                    raise PackageError(f"bundle {b.name} already exists (package {owner or '?'})", logl)
                shutil.copytree(b, dest, symlinks=True)
                copied.append(b.name)
            if not copied:
                raise PackageError("all plugins in this package are already in the image", logl)
            logl.append(f"installed bundles: {', '.join(copied)}")

            # missing shared libraries: try to resolve them from the direct dependencies
            sos = [p for n in copied for p in (LV2_DIR / n).rglob("*.so")]
            missing = await _missing_libs(sos, logl)
            libs = []
            todo, done = [pkg], set()
            rounds = 0
            while missing and todo and rounds < 3:
                rounds += 1
                logl.append(f"missing libraries: {', '.join(sorted(missing))}")
                nxt = []
                for parent in todo:
                    for dep in await _depends(parent, logl):
                        if dep in done or await _dpkg_installed(dep):
                            continue
                        done.add(dep)
                        dd = Path(tmp) / f"dep-{dep}"
                        dd.mkdir()
                        rc, _ = await _run(["apt-get", "download", "-o", "APT::Sandbox::User=root", dep],
                                           logl, cwd=dd, check=False)
                        if rc != 0:
                            continue
                        dx = Path(tmp) / f"dx-{dep}"
                        for deb in dd.glob("*.deb"):
                            await _run(["dpkg-deb", "-x", str(deb), str(dx)], logl)
                        LIB_DIR.mkdir(parents=True, exist_ok=True)
                        for so in list(dx.glob("usr/lib/*/*.so*")) + list(dx.glob("usr/lib/*.so*")) + \
                                list(dx.glob("lib/*/*.so*")):
                            if so.is_file() or so.is_symlink():
                                target = LIB_DIR / so.name
                                if not target.exists():
                                    shutil.copy2(so, target, follow_symlinks=False)
                                    libs.append(so.name)
                        nxt.append(dep)
                todo = nxt
                missing = await _missing_libs(sos, logl)
            if missing:
                logl.append(f"WARNING: still missing: {', '.join(sorted(missing))}")
        MANIFESTS.mkdir(parents=True, exist_ok=True)
        manifest = {"pkg": pkg, "version": version, "bundles": copied, "skipped": skipped,
                    "libs": libs, "missing_libs": sorted(missing), "installed_at": int(time.time())}
        (MANIFESTS / f"{pkg}.json").write_text(json.dumps(manifest, indent=1))
        log.info("package %s %s installed: %s", pkg, version, copied)
        return {"ok": True, "manifest": manifest, "log": logl}


async def remove(pkg, used_uris=()):
    if not SAFE_PKG.match(pkg):
        raise PackageError("invalid package name")
    m = _manifest(pkg)
    if not m:
        raise PackageError(f"{pkg} was not installed from here" + (" (it is part of the base image)" if pkg in BASE else ""))
    st = await status(used_uris)
    if any(p["pkg"] == pkg and p["in_use"] for p in st["packages"]):
        raise PackageError(f"blocks in the chain use plugins from {pkg}: remove them first")
    async with _lock:
        for b in m["bundles"]:
            shutil.rmtree(LV2_DIR / b, ignore_errors=True)
        others = {lib for o in _manifests() if o["pkg"] != pkg for lib in o.get("libs", [])}
        for lib in m.get("libs", []):
            if lib not in others:
                try:
                    (LIB_DIR / lib).unlink()
                except FileNotFoundError:
                    pass
        (MANIFESTS / f"{pkg}.json").unlink()
        log.info("package %s removed", pkg)
        return {"ok": True, "log": [f"removed: {', '.join(m['bundles'])}"]}


def request_rescan():
    LV2_DIR.mkdir(parents=True, exist_ok=True)
    RESCAN.write_text(str(time.time()))
