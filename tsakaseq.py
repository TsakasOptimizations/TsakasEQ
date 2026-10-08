"""TsakasEQ: system-wide EQ for music, movies and games, on top of a bundled, hidden Equalizer APO.

Run: python tsakaseq.py            UI (Edge app window)
     python tsakaseq.py --selftest  filter-math checks
     TsakasEQ.exe --setup LOG       elevated one-time setup (the UI launches this itself)
"""
import csv
import ctypes
import io
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "2.5.0"
REPO = "TsakasOptimizations/TsakasEQ"
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APO_INSTALLER = BUNDLE / "vendor" / "EqualizerAPO-x64-1.4.2.exe"
DATA = Path(os.environ["APPDATA"]) / "TsakasEQ"
STATE = DATA / "state.json"
OUR_CONFIG = "tsakaseq.txt"
NO_WINDOW = 0x08000000
# Relaunching our own exe must not inherit PyInstaller's internal vars, or the new process thinks it is
# our onefile child and fails its parent check ("Security validation failure ...").
FRESH_ENV = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
FS = 48000
FREQS = [20 * 1000 ** (i / 239) for i in range(240)]

# Spotify's six bands. Gains are relative to your headphones' stock sound; starting points, tweak by ear.
BANDS = (("LSC", 60, 0.7), ("PK", 150, 1.0), ("PK", 400, 1.0), ("PK", 1000, 1.0), ("PK", 2400, 0.8), ("HSC", 15000, 0.5))


def preset(*gains):
    return [[k, f, float(g), q] for (k, f, q), g in zip(BANDS, gains)]


BUILTIN = {
    #                      60Hz 150Hz 400Hz 1KHz 2.4KHz 15KHz
    "FPS Games":    preset(-6,  -3,   -1,   1,   4,     3),    # cut explosions/gun boom; footsteps, utility, direction up
    "Music":        preset(3,   -1.5, -1,   0,   1.5,   2),    # 808 sub, less mid-bass smear, vocal bite, air
    "Movies":       preset(2.5, -1,   -1.5, 1,   2,     1.5),  # rumble, less boom on dialogue, clearer voices
    "Flat":         preset(0,   0,    0,    0,   0,     0),
}


# ---------------------------------------------------------------- filter math (RBJ cookbook, same as APO)

def biquad(kind, f0, gain, q, fs=FS):
    A = 10 ** (gain / 40)
    w0 = 2 * math.pi * f0 / fs
    c, al = math.cos(w0), math.sin(w0) / (2 * q)
    s = 2 * math.sqrt(A) * al
    if kind == "PK":
        return (1 + al * A, -2 * c, 1 - al * A), (1 + al / A, -2 * c, 1 - al / A)
    if kind == "LSC":
        return ((A * ((A + 1) - (A - 1) * c + s), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - s)),
                ((A + 1) + (A - 1) * c + s, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - s))
    return ((A * ((A + 1) + (A - 1) * c + s), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - s)),
            ((A + 1) - (A - 1) * c + s, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - s))


def response_db(bands, f, fs=FS):
    z = complex(math.cos(2 * math.pi * f / fs), -math.sin(2 * math.pi * f / fs))
    h = 1
    for kind, f0, g, q in bands:
        b, a = biquad(kind, f0, g, q, fs)
        h *= (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * math.log10(abs(h))


HEADROOM = 6.0  # fixed preamp: volume stays put while dragging; boosts above +6 dB can clip (UI warns)


def config_text(bands, on=True, outputs=None, headroom=HEADROOM):
    """outputs: endpoint GUIDs the EQ applies to (APO's Device command); None = every output."""
    lines = ["# TsakasEQ (written by the app, edits get overwritten)"]
    if outputs:
        lines.append("Device: " + "; ".join(outputs))
    lines.append(f"Preamp: {-headroom:.1f} dB")  # also when off: on/off stays a level-matched comparison
    if on:
        lines += [f"Filter: ON {k} Fc {f:.0f} Hz Gain {g:.1f} dB Q {q:.2f}" for k, f, g, q in bands]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Equalizer APO (port of DeviceSelector's DeviceAPOInfo)

HKLM = winreg.HKEY_LOCAL_MACHINE
APO_KEY = r"SOFTWARE\EqualizerAPO"
RENDER = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
FX = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},%d"
LFX, GFX, SFX, MFX, EFX = (FX % i for i in (1, 2, 5, 6, 7))
SLOTS = (LFX, GFX, SFX, MFX, EFX)
COMPOSITE = tuple(FX % i for i in (13, 14, 15))
MODES = {SFX: "{d3993a3f-99c2-4402-b5ec-a92a0367664b},5", MFX: "{d3993a3f-99c2-4402-b5ec-a92a0367664b},6",
         EFX: "{d3993a3f-99c2-4402-b5ec-a92a0367664b},7"}
DEFAULT_MODE = "{C18E2F7E-933D-4965-B7D1-1EEF228D2AF3}"
DISABLE_ENHANCEMENTS = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5"
COMBINED_DEVICE = "{b3f8fa53-0004-438e-9003-51a46e139bfc},41"
CONNECTION_NAME, DEVICE_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2", "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
NOVALUE = "!VALUE"


def reg_get(key, name, default=None):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def subkeys(key):
    i = 0
    while True:
        try:
            yield winreg.EnumKey(key, i)
        except OSError:
            return
        i += 1


def apo_config_dir():
    try:
        with winreg.OpenKey(HKLM, APO_KEY) as k:
            path = Path(winreg.QueryValueEx(k, "ConfigPath")[0])
        return path if path.is_dir() else None
    except OSError:
        return None


APO_PRE, APO_POST = "{EACD2258-FCAC-4FF4-B36D-419E924A6D79}", "{EC1CC9CE-FAED-4822-828A-82A81A6F018F}"


def apo_guids():
    """(pre-mix, post-mix) CLSIDs of Equalizer APO, checked to be registered."""
    for g in (APO_PRE, APO_POST):
        try:
            winreg.OpenKey(HKLM, rf"SOFTWARE\Classes\AudioEngine\AudioProcessingObjects\{g}").Close()
        except OSError:
            raise RuntimeError("Equalizer APO is not registered correctly (reinstall it).")
    return APO_PRE, APO_POST


def render_devices(guids):
    """[(device guid, display name, has APO)] for present, enabled playback devices."""
    ours = {g.lower() for g in guids}
    out = []
    with winreg.OpenKey(HKLM, RENDER) as root:
        for g in subkeys(root):
            try:
                with winreg.OpenKey(root, g) as dev:
                    state = reg_get(dev, "DeviceState", 4)
                if state & 4 or state & 2 or state & 0x10000000:  # not present / disabled (APO's own rule)
                    continue
                with winreg.OpenKey(root, g + r"\Properties") as p:
                    name = f"{reg_get(p, CONNECTION_NAME, '?')} ({reg_get(p, DEVICE_NAME, '?')})"
                with winreg.OpenKey(root, g + r"\FxProperties") as fx:
                    has = any(str(reg_get(fx, s, "")).lower() in ours for s in SLOTS)
            except OSError:  # no FxProperties: DeviceSelector calls these "experimental"; skipped
                continue
            out.append((g, name, has))
    return out


def install_device(guid, pre, post):
    """Same registry changes DeviceSelector makes, so Equalizer APO's own uninstaller reverts them."""
    with winreg.OpenKey(HKLM, rf"{RENDER}\{guid}\Properties") as p:
        combined = reg_get(p, COMBINED_DEVICE) is not None
    access = winreg.KEY_READ | winreg.KEY_SET_VALUE
    with winreg.OpenKey(HKLM, rf"{RENDER}\{guid}\FxProperties", 0, access) as fx, \
            winreg.CreateKey(HKLM, rf"{APO_KEY}\Child APOs\{guid}") as child:
        orig = {s: reg_get(fx, s, NOVALUE) for s in SLOTS}
        present = {s for s in SLOTS + COMPOSITE if reg_get(fx, s) is not None}
        for s, v in orig.items():
            winreg.SetValueEx(child, s, 0, winreg.REG_SZ, v)

        none = lambda a, b: orig[a] == NOVALUE and orig[b] == NOVALUE
        if present and present <= {LFX, GFX}:      # driver ships only legacy LFX/GFX
            pre_slot, post_slot, drop = LFX, GFX, (SFX, MFX, EFX)
            pre_child = orig[SFX] if none(LFX, GFX) else orig[LFX]
            post_child = orig[MFX] if none(LFX, GFX) else orig[GFX]
        elif combined:                              # Win11 combined bluetooth: EFX doesn't work
            pre_slot, post_slot, drop = SFX, MFX, (LFX, GFX)
            pre_child = orig[LFX] if none(SFX, MFX) else orig[SFX]
            post_child = orig[GFX] if none(SFX, MFX) else orig[MFX]
        else:
            pre_slot, post_slot, drop = SFX, EFX, (LFX, GFX)
            pre_child = orig[LFX] if none(SFX, EFX) else orig[SFX]
            post_child = orig[GFX] if none(SFX, EFX) else orig[EFX]

        for name, v in (("PreMixChild", pre_child), ("PostMixChild", post_child),
                        ("AllowSilentBufferModification", "false"), ("Version", "2")):
            winreg.SetValueEx(child, name, 0, winreg.REG_SZ, "" if v == NOVALUE else v)
        for s in drop:
            if s in present:
                winreg.DeleteValue(fx, s)
        for slot, clsid in ((pre_slot, pre), (post_slot, post)):
            winreg.SetValueEx(fx, slot, 0, winreg.REG_SZ, clsid)
            if slot in MODES and reg_get(fx, MODES[slot]) is None:
                winreg.SetValueEx(fx, MODES[slot], 0, winreg.REG_MULTI_SZ, [DEFAULT_MODE])
        if reg_get(fx, DISABLE_ENHANCEMENTS) is not None:
            winreg.DeleteValue(fx, DISABLE_ENHANCEMENTS)


def setup(log_path):
    """Elevated: install APO silently, enable it on every playback device, hand config to the app."""
    try:
        if not apo_config_dir():
            p = subprocess.Popen([str(APO_INSTALLER), "/S"])
            while p.poll() is None:  # installer pops DeviceSelector even when silent; we register devices ourselves
                subprocess.run(["taskkill", "/f", "/im", "DeviceSelector.exe"], capture_output=True, creationflags=NO_WINDOW)
                time.sleep(0.5)
        pre, post = apo_guids()
        cfg = apo_config_dir()
        for guid, _, has in render_devices((pre, post)):
            if not has:
                backup = cfg / f"backup_{guid.strip('{}')}.reg"
                if not backup.exists():
                    subprocess.run(["reg", "export", rf"HKLM\{RENDER}\{guid}\FxProperties", str(backup), "/y"],
                                   capture_output=True, creationflags=NO_WINDOW)
                install_device(guid, pre, post)

        ours = cfg / OUR_CONFIG
        if not ours.exists():
            ours.write_text(config_text([], False), encoding="utf-8")
        # Users may write this file so the UI never needs admin (APO's installer already grants this on config\)
        subprocess.run(["icacls", str(ours), "/grant", "*S-1-5-32-545:M"], capture_output=True, creationflags=NO_WINDOW)
        main_cfg = cfg / "config.txt"
        backup = cfg / "config.txt.tsakaseq.bak"
        if main_cfg.exists() and not backup.exists():
            backup.write_bytes(main_cfg.read_bytes())
        main_cfg.write_text(f"Include: {OUR_CONFIG}\n", encoding="utf-8")

        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Restart-Service AudioEndpointBuilder -Force; Start-Service Audiosrv"],
                       capture_output=True, creationflags=NO_WINDOW)
        Path(log_path).write_text("ok", encoding="utf-8")
    except Exception as e:
        Path(log_path).write_text(f"{type(e).__name__}: {e}", encoding="utf-8")


def run_setup_elevated():
    log = Path(tempfile.gettempdir()) / f"tsakaseq-setup-{secrets.token_hex(4)}.log"
    exe, args = (sys.executable, ["--setup", str(log)]) if getattr(sys, "frozen", False) \
        else (sys.executable, [str(Path(__file__).resolve()), "--setup", str(log)])
    arglist = ",".join(f"'{a}'" for a in args)
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Start-Process -FilePath '{exe}' -ArgumentList {arglist} -Verb RunAs -Wait -WindowStyle Hidden"],
                       capture_output=True, text=True, creationflags=NO_WINDOW, env=FRESH_ENV)
    if not log.exists():
        return "Setup was cancelled (admin permission is needed once)." if r.returncode else "Setup did not run."
    result = log.read_text(encoding="utf-8")
    log.unlink(missing_ok=True)
    return None if result == "ok" else result


def status():
    cfg = apo_config_dir()
    if not cfg:
        return {"ready": False, "missing": [], "devices": []}
    try:
        devices = render_devices(apo_guids())
    except (OSError, RuntimeError):
        return {"ready": False, "missing": [], "devices": []}
    try:
        with open(cfg / OUR_CONFIG, "a"):
            pass
        linked = OUR_CONFIG in (cfg / "config.txt").read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        linked = False
    return {"ready": linked, "missing": [n for _, n, h in devices if not h],
            "devices": [{"guid": g, "name": n} for g, n, h in devices if h]}


# ---------------------------------------------------------------- state + updates

write_lock = threading.Lock()


def load_state():
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("presets", {})
    freqs = lambda bs: [b[1] for b in bs]
    for k, v in BUILTIN.items():  # add missing built-ins; reset ones saved on an older band layout
        if k not in s["presets"] or freqs(s["presets"][k]) != freqs(v):
            s["presets"][k] = [b[:] for b in v]
    s.setdefault("active", "FPS Games")
    for old, new in (("Valorant", "FPS Games"), ("Music - Rap", "Music")):  # renamed built-ins
        s["presets"].pop(old, None)
        if s["active"] == old:
            s["active"] = new
    s["presets"] = {**{k: s["presets"][k] for k in BUILTIN}, **s["presets"]}  # built-ins first, in order
    s.setdefault("on", True)
    s.setdefault("outputs", None)
    s.setdefault("auto_volume", False)
    if s["active"] not in s["presets"]:
        s["active"] = next(iter(s["presets"]))
    return s


def save_state(s):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


headroom_now = HEADROOM  # dB the whole signal is lowered by right now


def level_for(bands, on):
    """Auto volume: lower only as much as this curve's biggest boost needs; otherwise the fixed 6 dB."""
    if not (state and state.get("auto_volume")):
        return HEADROOM
    if not on:
        return headroom_now  # EQ off keeps the on-level, so the switch stays a fair comparison
    return math.ceil(max(0.0, max(response_db(bands, f) for f in FREQS)) * 10) / 10


def write_eq(bands, on, settle=False):
    """settle=True re-levels the volume (preset switch, Apply). Dragging never does, so volume stays put."""
    global headroom_now
    cfg = apo_config_dir()
    if not cfg:
        return False
    with write_lock:
        if settle or not (state and state.get("auto_volume")):
            headroom_now = level_for(bands, on)
        outputs = state.get("outputs") if state else None
        (cfg / OUR_CONFIG).write_text(config_text(bands, on, outputs, headroom_now), encoding="utf-8")
    return True


def vtuple(v):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


def check_update():
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "TsakasEQ", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"newer": False, "latest": VERSION, "message": "No releases published yet."}
        raise
    exe = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
    newer = vtuple(rel["tag_name"]) > vtuple(VERSION) and exe is not None
    return {"newer": newer, "latest": rel["tag_name"].lstrip("v"), "url": exe and exe["browser_download_url"]}


def install_update():
    """Download new exe next to ours, swap it in after we exit, relaunch."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Updating only works from the .exe")
    info = check_update()  # URL always comes from GitHub's API, never from the page
    if not info["newer"]:
        raise RuntimeError("Already up to date.")
    url = info["url"]
    me = Path(sys.executable)
    new = me.with_suffix(".new")
    req = urllib.request.Request(url, headers={"User-Agent": "TsakasEQ"})
    with urllib.request.urlopen(req, timeout=60) as r, open(new, "wb") as f:
        f.write(r.read())
    bat = Path(tempfile.gettempdir()) / "tsakaseq-update.bat"
    bat.write_text(f'@echo off\nset n=0\n:retry\ntimeout /t 1 /nobreak >nul\nset /a n+=1\n'
                   f'move /y "{new}" "{me}" >nul 2>&1 || if %n% lss 30 goto retry\n'
                   f'start "" "{me}"\ndel "%~f0"\n', encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=NO_WINDOW | 0x00000008, env=FRESH_ENV)  # DETACHED_PROCESS


# ---------------------------------------------------------------- AI profiles from AutoEq lab measurements

AUTOEQ = "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master/results/"
AI_PRESETS = {"AI FPS": "FPS Games", "AI Music": "Music", "AI Movies": "Movies"}
_autoeq_index = None


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TsakasEQ"}), timeout=20) as r:
        return r.read().decode("utf-8")


def autoeq_index():
    """[(model, path, measured by)] for ~8,800 headphones; downloaded once per run."""
    global _autoeq_index
    if _autoeq_index is None:
        _autoeq_index = re.findall(r"^- \[(.+?)\]\(\./(.+?)\) by (.+)$", fetch(AUTOEQ + "INDEX.md"), re.M)
    return _autoeq_index


def autoeq_search(q):
    words = q.lower().split()
    hits = [h for h in autoeq_index() if words and all(w in h[0].lower() for w in words)]
    return [{"name": n, "path": p, "by": b} for n, p, b in sorted(hits, key=lambda h: len(h[0]))[:8]]


def fit_six(target):
    """Gains for the six bands whose combined curve hits `target` (dB list, one per band) at each center."""
    g = list(target)
    for _ in range(40):  # bands overlap, so iterate
        g = [max(-12.0, min(12.0, x + t - response_db(preset(*g), f)))
             for x, t, (_, f, _) in zip(g, target, BANDS)]
    return g


def tame(gains):
    """Shrink boosts until the curve fits the fixed headroom (no distortion)."""
    for _ in range(40):
        if max(response_db(preset(*gains), f) for f in FREQS) <= HEADROOM:
            break
        gains = [g * 0.9 if g > 0 else g for g in gains]
    return [round(g, 1) for g in gains]


def ai_profiles(path):
    """Correct the headphone toward AutoEq's neutral target, then add each built-in flavour on top."""
    match = next((h for h in autoeq_index() if h[1] == path), None)
    if not match:  # only paths from the index, never arbitrary URLs
        raise ValueError("Unknown model")
    rows = list(csv.DictReader(io.StringIO(fetch(f"{AUTOEQ}{path}/{path.rsplit('/', 1)[-1]}.csv"))))
    freqs = [float(r["frequency"]) for r in rows]
    eq = [float(r["equalization"]) for r in rows]
    at = lambda f: eq[min(range(len(freqs)), key=lambda i: abs(math.log(freqs[i] / f)))]
    # measurements above ~10 kHz are rig resonances more than headphone, so trust that band less
    limits = (6, 6, 6, 6, 6, 3)
    correction = [max(-l, min(l, c)) for c, l in zip(fit_six([at(f) for _, f, _ in BANDS]), limits)]
    return match[0], {ai: preset(*tame([c + b[2] for c, b in zip(correction, BUILTIN[base])]))
                      for ai, base in AI_PRESETS.items()}


# ---------------------------------------------------------------- web UI

TOKEN = secrets.token_urlsafe(16)
state = None
browser = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        if self.headers.get("X-Token") != TOKEN:  # other local web pages can't drive the app
            self.send(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if self.path.split("?")[0] == "/":
            return self.send(200, (BUNDLE / "ui.html").read_bytes(), "text/html; charset=utf-8")
        if not self.authed():
            return
        if self.path == "/api/state":
            return self.send(200, {**state, "defaults": BUILTIN, "version": VERSION, "status": status(),
                                   "startup": startup_enabled(), "can_startup": FROZEN, "headroom": headroom_now})
        if self.path == "/api/update":
            try:
                return self.send(200, check_update())
            except Exception as e:
                return self.send(200, {"newer": False, "message": f"Couldn't reach GitHub: {e}"})
        if self.path.startswith("/api/autoeq?"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("q", [""])[0]
            try:
                return self.send(200, {"results": autoeq_search(q)})
            except Exception as e:
                return self.send(200, {"error": f"Couldn't reach the AutoEq database: {e}"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.authed():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/live":  # what you hear while dragging, not saved
            ok = write_eq(body["bands"], body["on"], settle=bool(body.get("settle")))
            return self.send(200, {"ok": ok, "headroom": headroom_now})
        if self.path == "/api/save":  # Apply
            state.update(presets=body["presets"], active=body["active"], on=body["on"],
                         outputs=body.get("outputs") or None)
            save_state(state)
            ok = write_eq(state["presets"][state["active"]], state["on"], settle=True)
            return self.send(200, {"ok": ok, "headroom": headroom_now})
        if self.path == "/api/autoeq":
            try:
                model, presets = ai_profiles(body["path"])
            except Exception as e:
                return self.send(200, {"error": f"Couldn't build profiles: {e}"})
            state["ai_model"] = model
            save_state(state)
            return self.send(200, {"model": model, "presets": presets})
        if self.path == "/api/autovolume":
            state["auto_volume"] = bool(body["on"])
            save_state(state)
            return self.send(200, {"on": state["auto_volume"]})
        if self.path == "/api/startup":
            try:
                set_startup(bool(body["on"]))
            except (OSError, RuntimeError) as e:
                return self.send(200, {"on": startup_enabled(), "error": str(e)})
            return self.send(200, {"on": startup_enabled()})
        if self.path == "/api/setup":
            err = run_setup_elevated()
            if not err:
                write_eq(state["presets"][state["active"]], state["on"], settle=True)
            return self.send(200, {"error": err, "status": status()})
        if self.path == "/api/update":
            try:
                install_update()
            except Exception as e:
                return self.send(200, {"error": str(e)})
            self.send(200, {"ok": True})
            threading.Timer(0.5, shutdown).start()
            return
        self.send(404, {"error": "not found"})


def shutdown():
    if state:  # drop un-applied tweaks: what's applied is what plays while the app is closed
        write_eq(state["presets"][state["active"]], state["on"], settle=True)
    if browser and browser.poll() is None:
        browser.terminate()
    os._exit(0)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
FROZEN = getattr(sys, "frozen", False)


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "TsakasEQ")
        return True
    except OSError:
        return False


def set_startup(on):
    """Per-user Run entry (no admin), like Discord/Steam. Only the exe can register itself."""
    if not FROZEN:
        raise RuntimeError("Open at startup only works from the .exe")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "TsakasEQ", 0, winreg.REG_SZ, f'"{sys.executable}"')
        elif startup_enabled():
            winreg.DeleteValue(k, "TsakasEQ")


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base and (p := Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe").exists():
            return p
    return None


def main():
    global state, browser
    if "--setup" in sys.argv:
        return setup(sys.argv[sys.argv.index("--setup") + 1])
    if "--selftest" in sys.argv:
        return selftest()
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "TsakasEQ.single")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    state = load_state()
    if FROZEN and startup_enabled():
        set_startup(True)  # keep the entry pointing here if the exe was moved
    if status()["ready"]:
        write_eq(state["presets"][state["active"]], state["on"], settle=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/?t={TOKEN}"
    edge = find_edge()
    if not edge:  # ponytail: no Edge = default browser, app runs until killed
        webbrowser.open(url)
        threading.Event().wait()
    browser = subprocess.Popen([str(edge), f"--app={url}", f"--user-data-dir={DATA / 'window'}",
                                "--window-size=1120,900", "--no-first-run", "--no-default-browser-check",
                                "--disable-features=msEdgeFirstRunExperience,Translate"])
    browser.wait()
    del mutex
    shutdown()


def selftest():
    global state
    state = {"auto_volume": True}
    assert level_for(BUILTIN["Flat"], True) == 0
    fps_peak = max(response_db(BUILTIN["FPS Games"], f) for f in FREQS)
    assert fps_peak <= level_for(BUILTIN["FPS Games"], True) < fps_peak + 0.11
    assert level_for(BUILTIN["FPS Games"], False) == headroom_now  # off keeps the on-level
    state = None
    assert level_for(BUILTIN["FPS Games"], True) == HEADROOM
    assert abs(response_db([["PK", 1000, 6, 1]], 1000) - 6) < 0.01
    assert abs(response_db([["LSC", 100, -6, 0.7]], 20) + 6) < 0.3
    assert abs(response_db([["HSC", 5000, 4, 0.7]], 18000) - 4) < 0.5
    t = config_text([["PK", 1000, 6.0, 1.0]])
    assert config_text([["PK", 1000, -3.0, 1.0]]).count("Preamp: -6.0 dB") == 1
    assert "Preamp: -6.0 dB" in t and "Filter: ON PK Fc 1000 Hz Gain 6.0 dB Q 1.00" in t, t
    off = config_text([["PK", 1000, 6, 1]], on=False)
    assert "Filter" not in off and "Preamp: -6.0 dB" in off
    assert "Device" not in t
    assert "Device: {a}; {b}\n" in config_text([["PK", 1000, 6, 1]], outputs=["{a}", "{b}"])
    assert vtuple("v1.10.0") > vtuple("1.9.3")
    assert all(abs(g) < 1e-6 for g in fit_six([0] * 6))
    fitted = fit_six([3, -2, 0, 1, 4, 2])
    assert all(abs(response_db(preset(*fitted), f) - t) < 0.1 for (_, f, _), t in zip(BANDS, [3, -2, 0, 1, 4, 2]))
    assert max(response_db(preset(*tame([12] * 6)), f) for f in FREQS) <= HEADROOM
    for name, bands in BUILTIN.items():
        assert max(response_db(bands, f) for f in FREQS) <= HEADROOM, name
    print("selftest ok")


if __name__ == "__main__":
    main()
