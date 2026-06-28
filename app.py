"""
DJ Copyright Prep - one small local app that turns a folder of songs into a single,
ultra-small, low-res MP4 whose audio is all the songs combined. Small enough to upload
fast, clear enough for YouTube Content ID to recognise. Optionally trims each song to a
short clip taken from partway in (skips intros -> better detection).

Replaces the two old .bat scripts (merge files.bat + Generator.bat) with one UI.

Run during development:   pip install flask  &&  python app.py
Build a standalone .exe:  build.bat   ->   dist/DJCopyrightPrep.exe
"""

import os
import re
import sys
import json
import time
import socket
import shutil
import logging
import tempfile
import threading
import subprocess
import webbrowser
from pathlib import Path

from flask import Flask, request, Response, jsonify

# --------------------------------------------------------------------------------------
# Paths (works both as a plain script and as a PyInstaller --onefile exe)
# --------------------------------------------------------------------------------------
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _bin(name):
    """Locate a bundled binary (bin/ffmpeg[.exe]), falling back to one on PATH.

    Windows binaries are named ffmpeg.exe; macOS/Linux use no extension. Drop the
    matching platform build into bin/ when packaging; otherwise a system install is used.
    """
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = os.path.join(BASE, "bin", exe)
    if os.path.exists(bundled):
        return bundled
    return shutil.which(name) or bundled


FFMPEG = _bin("ffmpeg")

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".aiff", ".aif")

app = Flask(__name__)
# Keep the embedded server quiet - there is no console to print to in the shipped app.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# Set once the native window exists; lets the batch routes use the OS-native dialogs.
WINDOW = None

# The current batch of tracks to combine: a list of absolute file paths (some are real
# source files from the picker, some are dropped files we staged to a temp dir).
BATCH = []
SEEN = set()            # de-dupe keys (lowercased name, size) already in the batch
BATCH_LOCK = threading.Lock()   # guards BATCH/SEEN against overlapping add requests
STAGING = None          # temp dir holding dropped files
OUT_DIR = None          # where to write the result (a picked folder, else the Desktop)
OUT_BASE = ""           # optional prefix for the output file (e.g. a folder's name)

# Bumped each release to match the GitHub tag (e.g. tag v1.2.0 -> APP_VERSION "1.2.0").
# The app compares this to the latest release to offer an in-app update notice.
APP_VERSION = "1.3.0"
GITHUB_REPO = "JustBaneIsFine/copyright-checker"

# Hide the console windows ffmpeg/ffprobe would otherwise pop up on Windows.
_NOWINDOW = 0x08000000 if os.name == "nt" else 0


def reveal(path):
    """Open the containing folder in the OS file manager (Explorer/Finder/xdg)."""
    folder = os.path.dirname(path) if os.path.isfile(path) else path
    if not os.path.isdir(folder):
        return
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        elif os.name == "nt":
            os.startfile(folder)  # noqa: Windows only
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception:
        pass


def _run(cmd):
    """Run a command silently, return (ok, stderr)."""
    p = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=_NOWINDOW,
    )
    return p.returncode == 0, p.stderr.decode("utf-8", "replace")


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(path):
    """Return track duration in seconds (float), or 0.0 if unknown.

    Parsed from ffmpeg's own stderr so we don't need to ship a separate ffprobe.
    (`ffmpeg -i <file>` with no output exits non-zero but still prints Duration.)
    """
    try:
        p = subprocess.run(
            [FFMPEG, "-i", path, "-hide_banner"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NOWINDOW,
        )
        m = _DUR_RE.search(p.stderr.decode("utf-8", "replace"))
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def list_audio(folder):
    try:
        names = sorted(
            f for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS
            and os.path.isfile(os.path.join(folder, f))
        )
    except OSError:
        return []
    return names


def is_audio(name):
    return os.path.splitext(name)[1].lower() in AUDIO_EXTS


def _desktop_dir():
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def _ensure_staging():
    global STAGING
    if STAGING is None or not os.path.isdir(STAGING):
        STAGING = tempfile.mkdtemp(prefix="djprep_drop_")
    return STAGING


def _batch_payload():
    return {"files": [os.path.basename(p) for p in BATCH], "count": len(BATCH)}


def _batch_key(path):
    """Identity used to catch duplicates: same filename + same byte size."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = -1
    return (os.path.basename(path).lower(), size)


def _add_path(path):
    """Append a track if it isn't already in the batch. Returns True if added.
    Holds BATCH_LOCK so overlapping drops/picks can't double-add or race."""
    with BATCH_LOCK:
        key = _batch_key(path)
        if key in SEEN:
            return False
        BATCH.append(path)
        SEEN.add(key)
        return True


def _rebuild_seen():
    with BATCH_LOCK:
        SEEN.clear()
        SEEN.update(_batch_key(p) for p in BATCH)


def _open_folder_dialog():
    try:
        if WINDOW is not None:
            import webview
            mode = getattr(getattr(webview, "FileDialog", None), "FOLDER",
                           getattr(webview, "FOLDER_DIALOG", 2))
            res = WINDOW.create_file_dialog(mode)
            return res[0] if res else None
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        f = filedialog.askdirectory(title="Choose a song folder"); root.destroy()
        return f or None
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Core pipeline - generator that yields progress dicts (consumed by the SSE route)
# --------------------------------------------------------------------------------------
def process(files, out_dir, out_base, trim, offset, length, bitrate, color, audio_only=False):
    if not files:
        yield {"error": "No songs added yet. Drag tracks or a folder onto the window."}
        return

    work = Path(tempfile.mkdtemp(prefix="djprep_work_"))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        pass

    # Audio-only is delivered as uncompressed WAV; video keeps the small AAC track.
    ext = ".wav" if audio_only else ".mp4"
    seg_ext = ".wav" if audio_only else ".m4a"
    out_name = ((out_base + "_") if out_base else "") + "copyright" + ext
    out_path = str(Path(out_dir) / out_name)

    try:
        segments = []
        total = len(files)
        for i, src in enumerate(files):
            name = os.path.basename(src)
            seg = str(work / f"seg_{i:04d}{seg_ext}")

            cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
            if trim:
                dur = probe_duration(src)
                # Grab `length` seconds starting at `offset`. For songs shorter than the
                # offset, slide the window back so we still get audio instead of silence.
                start = min(offset, max(0.0, dur - length)) if dur else offset
                cmd += ["-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", src]
            else:
                cmd += ["-i", src]
            cmd += ["-vn", "-ac", "1", "-ar", "44100"]
            if audio_only:
                cmd += ["-c:a", "pcm_s16le", seg]   # WAV PCM (bitrate not applicable)
            else:
                cmd += ["-b:a", f"{bitrate}k", seg]

            yield {"index": i, "total": total, "name": name, "stage": "clip"}
            ok, err = _run(cmd)
            if not ok or not os.path.exists(seg):
                yield {"warn": f"Skipped (encode failed): {name}"}
                continue
            segments.append(seg)

        if not segments:
            yield {"error": "Every file failed to encode, so there is nothing to combine."}
            return

        # Concat all uniform segments (stream copy is safe now: identical codec/params).
        # Audio-only output is the combined track itself; otherwise concat to a temp file
        # that we then mux under a still image.
        listfile = work / "list.txt"
        listfile.write_text(
            "".join(f"file '{s.replace(chr(92), '/')}'\n" for s in segments),
            encoding="utf-8",
        )
        combined = out_path if audio_only else str(work / "combined.m4a")
        yield {"stage": "concat", "total": total, "index": total}
        ok, err = _run([
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-c", "copy", combined,
        ])
        if not ok:
            yield {"error": "Failed to combine the audio.\n" + err[-400:]}
            return

        if not audio_only:
            # Mux into a tiny still-image video with an auto-generated solid colour frame.
            # Bound the (otherwise infinite) colour source to the audio length - with a low
            # framerate, -shortest alone overshoots, padding the video with extra seconds.
            yield {"stage": "video", "total": total, "index": total}
            combined_dur = probe_duration(combined)
            color_src = f"color=c={color}:s=320x180:r=1"
            if combined_dur > 0:
                color_src += f":d={combined_dur:.3f}"
            ok, err = _run([
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", color_src,
                "-i", combined,
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-pix_fmt", "yuv420p", "-shortest", out_path,
            ])
            if not ok or not os.path.exists(out_path):
                yield {"error": "Failed to build the video.\n" + err[-400:]}
                return

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        yield {"done": True, "output": out_path, "size_mb": round(size_mb, 2),
               "count": len(segments), "audio_only": audio_only}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.route("/")
def index():
    return Response(PAGE.replace("__VERSION__", APP_VERSION), mimetype="text/html")


# ----- Batch: build a list of tracks via the picker or drag-and-drop ------------------
@app.route("/batch")
def batch_get():
    return jsonify(_batch_payload())


@app.route("/batch/add-folder", methods=["POST"])
def batch_add_folder():
    folder = _open_folder_dialog()
    if not folder:
        return jsonify({"cancelled": True, "added": 0, "skipped": 0, **_batch_payload()})
    global OUT_DIR, OUT_BASE
    was_empty = not BATCH
    added = skipped = 0
    for n in list_audio(folder):
        if _add_path(os.path.join(folder, n)):
            added += 1
        else:
            skipped += 1
    # If this folder is the whole batch, write the result next to it and name it after it.
    if was_empty and added == len(BATCH) and added:
        OUT_DIR = folder
        OUT_BASE = os.path.basename(os.path.normpath(folder)) or ""
    else:
        OUT_DIR = OUT_DIR or _desktop_dir()
    return jsonify({"added": added, "skipped": skipped, **_batch_payload()})


@app.route("/batch/add-file", methods=["POST"])
def batch_add_file():
    """A dropped file, uploaded as multipart (the WebView hides the real path).
    Returns status added | duplicate | ignored so the page can tally drops."""
    global OUT_DIR
    f = request.files.get("file")
    if not f or not is_audio(f.filename or ""):
        return jsonify({"status": "ignored", **_batch_payload()})

    staging = _ensure_staging()
    name = os.path.basename((f.filename or "track").replace("\\", "/"))
    base, ext = os.path.splitext(name)
    dest = os.path.join(staging, name)
    k = 1
    while os.path.exists(dest):          # avoid clobbering a same-named file on disk
        dest = os.path.join(staging, f"{base}_{k}{ext}")
        k += 1
    f.save(dest)

    # Dedupe by (original name, size); _add_path uses the saved file's basename, so key
    # on the original name explicitly to catch the same track dropped twice.
    with BATCH_LOCK:
        key = (name.lower(), os.path.getsize(dest))
        if key in SEEN:
            status = "duplicate"
        else:
            BATCH.append(dest)
            SEEN.add(key)
            status = "added"
    if status == "duplicate":
        try:
            os.remove(dest)
        except OSError:
            pass
    if OUT_DIR is None:
        OUT_DIR = _desktop_dir()
    return jsonify({"status": status, **_batch_payload()})


@app.route("/batch/base", methods=["POST"])
def batch_base():
    """Optional: name the output after a dropped folder, if we don't have one yet."""
    global OUT_BASE
    name = (request.json or {}).get("name", "")
    name = re.sub(r"[^\w\- ]+", "", name).strip()[:60]
    if name and not OUT_BASE:
        OUT_BASE = name
    return jsonify({"ok": True})


@app.route("/batch/remove", methods=["POST"])
def batch_remove():
    i = (request.json or {}).get("index", -1)
    with BATCH_LOCK:
        if isinstance(i, int) and 0 <= i < len(BATCH):
            p = BATCH.pop(i)
            if STAGING and os.path.abspath(p).startswith(os.path.abspath(STAGING)):
                try:
                    os.remove(p)
                except OSError:
                    pass
    _rebuild_seen()
    return jsonify(_batch_payload())


@app.route("/batch/clear", methods=["POST"])
def batch_clear():
    global BATCH, STAGING, OUT_DIR, OUT_BASE
    with BATCH_LOCK:
        BATCH = []
        SEEN.clear()
        if STAGING and os.path.isdir(STAGING):
            shutil.rmtree(STAGING, ignore_errors=True)
        STAGING = None
        OUT_DIR = None
        OUT_BASE = ""
    return jsonify(_batch_payload())


@app.route("/process")
def process_route():
    trim = request.args.get("trim", "false") == "true"
    offset = float(request.args.get("offset", 30))
    length = float(request.args.get("length", 20))
    bitrate = int(request.args.get("bitrate", 64))
    color = "0x161619"  # fixed dark frame - colour never mattered for detection
    audio_only = request.args.get("audio_only", "false") == "true"

    files = list(BATCH)
    out_dir = OUT_DIR or _desktop_dir()
    out_base = OUT_BASE

    def stream():
        for evt in process(files, out_dir, out_base, trim, offset, length, bitrate,
                           color, audio_only):
            yield "data: " + json.dumps(evt) + "\n\n"

    return Response(stream(), mimetype="text/event-stream")


@app.route("/open-folder", methods=["POST"])
def open_folder():
    reveal(request.json.get("path", ""))
    return jsonify({"ok": True})


def _version_gt(a, b):
    """True if version string a is newer than b (e.g. '1.2.0' > '1.1.5')."""
    def parts(v):
        out = []
        for p in str(v).split("."):
            digits = "".join(c for c in p if c.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def _update_asset():
    """The release zip filename for the machine we're running on. (Only Apple Silicon
    Macs are built now - GitHub retired the free Intel macOS runners.)"""
    if sys.platform == "darwin":
        return "DJCopyrightPrep_mac_apple-silicon.zip"
    return "DJCopyrightPrep_windows.zip"


@app.route("/check-update")
def check_update():
    """Ask GitHub for the latest release and report whether it is newer than us."""
    info = {"update": False, "current": APP_VERSION}
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "DJCopyrightPrep"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
        latest = (data.get("tag_name") or "").lstrip("v")
        if latest:
            info.update({
                "update": _version_gt(latest, APP_VERSION),
                "latest": latest,
                "changelog": data.get("body") or "",
                "page": data.get("html_url") or "",
                "download": f"https://github.com/{GITHUB_REPO}/releases/latest/download/{_update_asset()}",
            })
    except Exception as e:
        info["error"] = str(e)
    return jsonify(info)


@app.route("/open-update", methods=["POST"])
def open_update():
    """Open an update link in the real browser. Restricted to our own release URLs."""
    url = (request.json or {}).get("url", "")
    if url.startswith(f"https://github.com/{GITHUB_REPO}/"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return jsonify({"ok": True})


# --------------------------------------------------------------------------------------
# Inlined UI - themed to match the reference frontend/css/app.css (dark, teal accent)
# --------------------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DJ Copyright Prep</title>
<style>
:root{
  --bg:#0e0e10; --bg-elev:#161619; --bg-elev2:#1d1d21; --border:#2a2a30;
  --text:#d4d4d4; --text-dim:#888; --accent:#1ed760; --accent-dim:#11913f;
  --danger:#e0556a; --warn:#e0b84a;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
  font-family:var(--sans);font-size:14px;user-select:none}
.wrap{min-height:100vh;padding:0}
.card{width:100%;min-height:100vh;background:var(--bg-elev);border:none;
  border-radius:0;padding:20px 24px;box-shadow:none}
.brand{font-weight:700;letter-spacing:.3px;color:var(--accent);font-size:20px;margin-bottom:12px}
.howto{background:var(--bg-elev2);border:1px solid var(--border);border-radius:8px;padding:11px 14px;margin-bottom:18px}
.howto-title{color:var(--accent);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.howto ol{margin:0;padding-left:17px;font-size:12.5px;color:var(--text);line-height:1.55}
.howto-note{margin-top:8px;font-size:11.5px;color:var(--text-dim)}
button{font-family:inherit;font-size:13px;color:var(--text);background:var(--bg-elev2);
  border:1px solid var(--border);border-radius:7px;padding:9px 14px;cursor:pointer}
button:hover{border-color:var(--accent-dim)}
button.primary{background:var(--accent-dim);border-color:var(--accent-dim);color:#04211e;font-weight:600}
button.primary:hover{background:var(--accent)}
button:disabled{opacity:.4;cursor:default}
input,select{font-family:inherit;font-size:13px;color:var(--text);background:var(--bg);
  border:1px solid var(--border);border-radius:6px;padding:7px 9px}
input:focus,select:focus{outline:none;border-color:var(--accent-dim)}
.section{margin-top:15px;border-top:1px solid var(--border);padding-top:14px}
.label{color:var(--text-dim);text-transform:uppercase;font-size:11px;letter-spacing:.6px;margin-bottom:9px}
.drop{border:1.5px dashed var(--border);border-radius:10px;padding:18px;text-align:center;
  transition:border-color .15s, background .15s}
.drop-hint{color:var(--text-dim);font-size:13px;margin-bottom:10px}
.drop-btns{display:flex;gap:8px;justify-content:center}
body.dragging .drop{border-color:var(--accent);background:rgba(30,215,96,.07)}
body.dragging .drop-hint{color:var(--accent)}
.batchhead{display:none;align-items:center;margin-top:12px;font-size:12.5px;color:var(--text-dim)}
.batchhead.show{display:flex}
.batchhead b{color:var(--accent)}
.files{margin-top:8px;max-height:170px;overflow:auto;border:1px solid var(--border);
  border-radius:8px;padding:5px;display:none}
.files.show{display:block}
.file{display:flex;align-items:center;gap:8px;padding:5px 8px;font-size:12.5px;
  color:var(--text);border-radius:5px}
.file:nth-child(odd){background:var(--bg-elev2)}
.file .fn{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file .rm{cursor:pointer;color:var(--text-dim);padding:0 4px;flex:none}
.file .rm:hover{color:var(--danger)}
body.busy .drop{opacity:.55}
body.busy .drop-btns button{cursor:default}
body.busy .file .rm{pointer-events:none;opacity:.35}
.row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.row label{flex:1;color:var(--text)}
.row .num{width:90px}
.switch{display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:14px}
.switch input{width:16px;height:16px;accent-color:var(--accent)}
.opts{opacity:1;transition:opacity .15s}
.opts.off{opacity:.4;pointer-events:none}
.prog{height:8px;background:var(--bg-elev2);border-radius:6px;overflow:hidden;margin:14px 0 8px}
.prog>div{height:100%;width:0;background:var(--accent);transition:width .25s}
.status{font-size:12.5px;color:var(--text-dim);min-height:18px}
.result{margin-top:16px;padding:14px;border:1px solid var(--accent-dim);border-radius:10px;
  background:var(--accent-soft,rgba(30,215,96,.08));display:none}
.result.show{display:block}
.result.err{border-color:var(--danger)}
.result b{color:var(--accent)}
.foot{display:flex;gap:10px;margin-top:22px}
.foot .grow{flex:1}
.swatch{width:30px;height:30px;border-radius:6px;border:1px solid var(--border);padding:1px;background:var(--bg)}
.seg{display:flex;border:1px solid var(--border);border-radius:7px;overflow:hidden}
.seg button{border:none;border-radius:0;background:var(--bg);padding:7px 12px;font-size:12.5px}
.seg button.on{background:var(--accent-dim);color:#04211e;font-weight:600}
.seg button+button{border-left:1px solid var(--border)}
.seg button:hover{border-color:transparent}
.brandrow{display:flex;align-items:baseline;gap:8px;margin-bottom:12px}
.brandrow .brand{margin-bottom:0}
.ver{font-size:12px;color:var(--text-dim)}
.updbar{display:none;border:1px solid var(--accent-dim);background:rgba(30,215,96,.08);
  border-radius:9px;padding:11px 13px;margin-bottom:16px}
.updbar.show{display:block}
.updhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.updhead .grow{flex:1}
.updhead b{color:var(--accent)}
button.mini{padding:5px 10px;font-size:12px}
.changelog{display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--border);
  font-size:12.5px;line-height:1.55;color:var(--text);max-height:200px;overflow:auto}
.changelog.show{display:block}
.changelog b{color:var(--text)}
.updsteps{margin-top:10px;font-size:12px;color:var(--text-dim);line-height:1.5}
.hidden{display:none}
</style></head>
<body>
<div class="wrap"><div class="card">

  <div class="updbar" id="updbar">
    <div class="updhead">
      <span>⬆ Update available: <b id="updver"></b></span>
      <span class="grow"></span>
      <button class="mini" onclick="toggleChangelog()">What's new</button>
      <button class="primary mini" onclick="getUpdate()">Download</button>
      <button class="mini ghost" onclick="dismissUpd()">✕</button>
    </div>
    <div class="changelog" id="changelog"></div>
    <div class="updsteps" id="updsteps" style="display:none">
      Your download is opening in the browser. When it finishes: unzip it, quit this app,
      replace your old <b>DJ Copyright Prep</b> folder with the new one, then open it.
    </div>
  </div>

  <div class="brandrow">
    <div class="brand">◐ DJ Copyright Prep</div>
    <span class="ver">v__VERSION__</span>
  </div>
  <div class="howto">
    <div class="howto-title">How to use</div>
    <ol>
      <li>Choose a folder of your songs.</li>
      <li>Generate one combined file (video or audio-only).</li>
      <li>Upload it <b>privately / unlisted</b> to the platform and let it analyse the songs.</li>
    </ol>
    <div class="howto-note">This app only prepares the file. It does not check copyright itself.</div>
  </div>

  <div class="section" style="border-top:none;padding-top:0;margin-top:0">
    <div class="label">1 · Songs</div>
    <div class="drop" id="drop">
      <div class="drop-hint">Drag tracks or folders here</div>
      <div class="drop-btns">
        <button class="mini" onclick="addFolder()">📁 Add folder</button>
      </div>
    </div>
    <div class="batchhead" id="batchhead">
      <span><b id="batchcount">0</b> <span id="batchword">tracks</span> in this batch</span>
      <span class="grow"></span>
      <button class="mini ghost" onclick="clearBatch()">Clear all</button>
    </div>
    <div class="status" id="batchstatus"></div>
    <div class="files" id="files"></div>
  </div>

  <div class="section">
    <div class="label">2 · Options</div>
    <div class="row"><label>Output</label>
      <div class="seg" id="seg">
        <button type="button" class="on" data-mode="video" onclick="setMode('video')">🎬 Video + Audio</button>
        <button type="button" data-mode="audio" onclick="setMode('audio')">🎵 Audio only</button>
      </div>
    </div>
    <label class="switch"><input type="checkbox" id="trim" checked onchange="toggleTrim()">
      <span>Trim each song to a short clip <span style="color:var(--text-dim)">(smaller file)</span></span></label>
    <div class="opts" id="opts">
      <div class="row"><label>Start offset (seconds in)</label>
        <input class="num" type="number" id="offset" value="30" min="0" step="1"></div>
      <div class="row"><label>Clip length (seconds)</label>
        <input class="num" type="number" id="length" value="20" min="3" step="1"></div>
    </div>
    <div class="row"><label>Audio quality (mono)</label>
      <select id="bitrate">
        <option value="64" selected>64 kbps · recommended</option>
        <option value="96">96 kbps</option>
        <option value="128">128 kbps · safest</option>
      </select></div>
  </div>

  <div class="section">
    <div class="label">3 · Generate</div>
    <button class="primary" id="go" onclick="go()" disabled style="width:100%">Generate video</button>
    <div class="prog hidden" id="progwrap"><div id="bar"></div></div>
    <div class="status" id="status"></div>
    <div class="result" id="result">
      <div id="resmsg"></div>
      <div class="foot">
        <span class="grow"></span>
        <button id="openbtn" onclick="openFolder()">📂 Open folder</button>
      </div>
    </div>
  </div>
</div></div>

<script>
let outPath = null, outputMode = 'video', updInfo = null, batchCount = 0;

// --- Update notice (Tier 1) ---
function mdLite(s){
  s = escapeHtml(s || '');
  s = s.replace(/^#{1,6}\s*(.*)$/gm, '<b>$1</b>');     // headings -> bold
  s = s.replace(/^\s*[-*]\s+(.*)$/gm, '• $1');          // bullets
  s = s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');         // **bold**
  s = s.replace(/`([^`]+)`/g, '$1');                    // strip code ticks
  return s.replace(/\n/g, '<br>');
}
async function checkUpdate(){
  try{
    const d = await (await fetch('/check-update')).json();
    if(d.update){
      updInfo = d;
      document.getElementById('updver').textContent = 'v' + d.latest;
      document.getElementById('changelog').innerHTML =
        mdLite(d.changelog) || 'See the release page for details.';
      document.getElementById('updbar').classList.add('show');
    }
  }catch(e){ /* offline or rate-limited: just skip the notice */ }
}
function toggleChangelog(){ document.getElementById('changelog').classList.toggle('show'); }
async function getUpdate(){
  if(!updInfo) return;
  await fetch('/open-update', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: updInfo.download})});
  document.getElementById('updsteps').style.display = 'block';
  document.getElementById('changelog').classList.add('show');
}
function dismissUpd(){ document.getElementById('updbar').classList.remove('show'); }

function toggleTrim(){
  document.getElementById('opts').classList.toggle('off', !document.getElementById('trim').checked);
}

function setMode(m){
  outputMode = m;
  document.querySelectorAll('#seg button').forEach(b => b.classList.toggle('on', b.dataset.mode===m));
  document.getElementById('go').textContent = (m==='audio') ? 'Generate audio file' : 'Generate video';
}

// --- Batch building (drag-drop + pickers) ---
// uploadQueue holds dropped files waiting to be sent; one drain loop processes them so
// overlapping drops (drop 50, then 50 more mid-upload) just extend the same queue.
let uploadQueue = [], uploading = false, generating = false;
let addTally = 0, dupTally = 0, errTally = 0;

function isBusy(){ return uploading || generating; }
function setBusy(){
  const busy = isBusy();
  document.querySelectorAll('#drop .drop-btns button').forEach(b => b.disabled = busy);
  document.getElementById('go').disabled = busy || batchCount === 0;
  document.body.classList.toggle('busy', busy);
}
function renderBatch(d){
  batchCount = d.count;
  const fl = document.getElementById('files');
  fl.innerHTML = d.files.map((f,i) =>
    '<div class="file"><span class="fn">'+escapeHtml(f)+'</span>'
    + '<span class="rm" onclick="removeItem('+i+')" title="Remove">✕</span></div>').join('');
  fl.classList.toggle('show', d.count>0);
  document.getElementById('batchhead').classList.toggle('show', d.count>0);
  document.getElementById('batchcount').textContent = d.count;
  document.getElementById('batchword').textContent = d.count===1 ? 'track' : 'tracks';
  setBusy();
}
async function refreshBatch(){ renderBatch(await (await fetch('/batch')).json()); }
function setBatchStatus(s){ document.getElementById('batchstatus').textContent = s || ''; }
function summarise(){
  let parts = [];
  if(addTally) parts.push('Added ' + addTally);
  if(dupTally) parts.push('skipped ' + dupTally + ' duplicate' + (dupTally>1?'s':''));
  if(errTally) parts.push(errTally + ' failed');
  return parts.length ? parts.join(', ') + '.' : '';
}

async function addFolder(){
  if(isBusy()) return;
  setBatchStatus('Opening…');
  const d = await (await fetch('/batch/add-folder', {method:'POST'})).json();
  renderBatch(d);
  setBatchStatus(d.cancelled ? '' :
    (d.added===0 && d.skipped===0 ? 'No audio files in that folder.'
     : 'Added ' + d.added + (d.skipped?(', skipped ' + d.skipped + ' duplicate' + (d.skipped>1?'s':'')):'') + '.'));
}
async function removeItem(i){
  if(isBusy()) return;
  renderBatch(await (await fetch('/batch/remove', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({index:i})})).json());
}
async function clearBatch(){
  if(isBusy()) return;
  setBatchStatus('');
  renderBatch(await (await fetch('/batch/clear', {method:'POST'})).json());
}

// Drag-and-drop: the WebView hides real paths, so we read the dropped files (walking into
// any dropped folders) and upload their bytes to the local server.
function isAudioName(n){ return /\.(mp3|wav|flac|m4a|aac|ogg|wma|aiff|aif)$/i.test(n); }
function readEntries(reader){
  return new Promise((resolve) => {
    const all = [];
    const step = () => reader.readEntries(
      ents => { if(!ents.length) resolve(all); else { all.push(...ents); step(); } },
      () => resolve(all));
    step();
  });
}
async function collectFiles(entry, out){
  if(entry.isFile){
    if(isAudioName(entry.name)) out.push(await new Promise((r,j)=>entry.file(r,j)));
  } else if(entry.isDirectory){
    for(const e of await readEntries(entry.createReader())) await collectFiles(e, out);
  }
}
function enqueueUploads(files){
  for(const f of files) uploadQueue.push(f);
  if(!uploading) drainQueue();
}
async function drainQueue(){
  uploading = true; addTally = dupTally = errTally = 0; setBusy();
  let done = 0;
  while(uploadQueue.length){
    const file = uploadQueue.shift();
    done++;
    setBatchStatus('Adding ' + done + ' / ' + (done + uploadQueue.length) + '…');
    try{
      const fd = new FormData(); fd.append('file', file, file.name);
      const r = await (await fetch('/batch/add-file', {method:'POST', body: fd})).json();
      if(r.status === 'added') addTally++; else if(r.status === 'duplicate') dupTally++;
      renderBatch(r);                 // grow the list live
    }catch(_){ errTally++; }
  }
  uploading = false; setBusy();
  setBatchStatus(summarise());
}
async function handleDrop(e){
  if(generating){ setBatchStatus('Please wait for the current export to finish.'); return; }
  const items = e.dataTransfer.items, entries = [];
  let folderName = '';
  for(const it of items){
    const en = it.webkitGetAsEntry && it.webkitGetAsEntry();
    if(en){ entries.push(en); if(en.isDirectory && !folderName) folderName = en.name; }
  }
  if(!uploading) setBatchStatus('Reading dropped items…');
  const files = [];
  if(entries.length){ for(const en of entries) await collectFiles(en, files); }
  else if(e.dataTransfer.files){ for(const f of e.dataTransfer.files) if(isAudioName(f.name)) files.push(f); }
  if(!files.length){ if(!uploading) setBatchStatus('No audio files in what you dropped.'); return; }
  if(folderName) fetch('/batch/base', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: folderName})});
  enqueueUploads(files);
}
let dragDepth = 0;
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('dragenter', e => { e.preventDefault(); dragDepth++;
  if(!generating) document.body.classList.add('dragging'); });
window.addEventListener('dragleave', e => { if(--dragDepth<=0){ dragDepth=0; document.body.classList.remove('dragging'); } });
window.addEventListener('drop', async e => {
  e.preventDefault(); dragDepth=0; document.body.classList.remove('dragging');
  await handleDrop(e);
});

function go(){
  if(batchCount===0 || isBusy()) return;
  generating = true; setBusy();
  const trim = document.getElementById('trim').checked;
  const q = new URLSearchParams({
    trim: trim,
    offset: document.getElementById('offset').value || 30,
    length: document.getElementById('length').value || 20,
    bitrate: document.getElementById('bitrate').value,
    audio_only: (outputMode === 'audio'),
  });
  document.getElementById('result').classList.remove('show','err');
  document.getElementById('progwrap').classList.remove('hidden');
  setBar(0); setStatus('Starting…');

  const es = new EventSource('/process?' + q.toString());
  es.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if(d.error){ finish(d.error, true); es.close(); return; }
    if(d.warn){ setStatus(d.warn); return; }
    if(d.done){
      outPath = d.output;
      const kind = d.audio_only ? 'audio file' : 'video';
      finish('✓ Done. <b>' + d.count + '</b> songs combined into a <b>'
        + d.size_mb + ' MB</b> ' + kind + '.<br><span style="color:var(--text-dim);font-size:12px">'
        + escapeHtml(d.output) + '</span>', false);
      es.close(); return;
    }
    if(d.stage === 'clip'){
      setBar(Math.round(85 * (d.index)/d.total));
      setStatus('Encoding clip ' + (d.index+1) + ' / ' + d.total + ': ' + escapeHtml(d.name));
    } else if(d.stage === 'concat'){
      setBar(90); setStatus('Combining audio…');
    } else if(d.stage === 'video'){
      setBar(96); setStatus('Building video…');
    }
  };
  es.onerror = () => { es.close(); finish('Connection lost.', true); };
}

function finish(msg, isErr){
  setBar(100);
  generating = false; setBusy();
  const res = document.getElementById('result');
  res.classList.add('show'); res.classList.toggle('err', isErr);
  document.getElementById('resmsg').innerHTML = msg;
  document.getElementById('openbtn').style.display = isErr ? 'none' : '';
  setStatus('');
}
async function openFolder(){
  if(!outPath) return;
  await fetch('/open-folder', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({path: outPath})});
}
function setBar(p){ document.getElementById('bar').style.width = p + '%'; }
function setStatus(s, err){ const el=document.getElementById('status');
  el.innerHTML = s; el.style.color = err ? 'var(--danger)' : 'var(--text-dim)'; }
function escapeHtml(s){ return (s+'').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

checkUpdate();
refreshBatch();
</script>
</body></html>"""


# --------------------------------------------------------------------------------------
# Native window - pywebview wraps the local server in a real OS window (no browser chrome,
# no console). The page talks to Python over plain HTTP (no js_api bridge - that triggers
# a fatal attribute-recursion bug in pywebview's frozen WinForms/WebView2 backend).
# --------------------------------------------------------------------------------------
def _webview2_present():
    """True if the Edge WebView2 runtime is installed (Windows only)."""
    try:
        import winreg
    except ImportError:
        return True  # not Windows -> WebKit is built in, nothing to check
    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # Evergreen runtime client id
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + guid),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + guid),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + guid),
    ]
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def ensure_webview2():
    """First-run safety net: if the WebView2 runtime is missing, install it silently
    from the bundled bootstrapper. No-op on macOS/Linux and on machines that already
    have it."""
    if os.name != "nt" or _webview2_present():
        return
    setup = os.path.join(BASE, "redist", "MicrosoftEdgeWebview2Setup.exe")
    if not os.path.exists(setup):
        return
    try:
        subprocess.run([setup, "/silent", "/install"], creationflags=_NOWINDOW)
    except Exception:
        pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(port):
    app.run(host="127.0.0.1", port=port, threaded=True)


def _wait_for_server(port, timeout=15.0):
    """Block until the local server responds, so the window never loads before the
    page is being served. Returns True if it came up, False on timeout."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def log_path():
    """Path to the error/crash log the user can send us. Kept stable across runs."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    logdir = os.path.join(base, "DJCopyrightPrep")
    try:
        os.makedirs(logdir, exist_ok=True)
    except OSError:
        logdir = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(logdir, "app.log")


def _setup_logging():
    """Log to a rolling file, give stdout/stderr a real destination in the windowed
    build, and record EVERY error - uncaught exceptions, background-thread crashes, and
    hard interpreter faults - so a tester can just send us the one log file.

    (A PyInstaller --noconsole exe has sys.stdout/stderr == None when launched from
    Explorer; a stray write would otherwise raise and take the app down.)"""
    from logging.handlers import RotatingFileHandler
    logpath = log_path()
    # Keep history across runs (so a crash isn't erased by the next launch), but bounded.
    handler = RotatingFileHandler(logpath, maxBytes=512 * 1024, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("pywebview").setLevel(logging.DEBUG)

    logstream = open(logpath, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = logstream
    if sys.stderr is None:
        sys.stderr = logstream

    crash = logging.getLogger("crash")

    def _excepthook(et, ev, tb):
        crash.critical("Uncaught exception", exc_info=(et, ev, tb))
    sys.excepthook = _excepthook

    try:  # background-thread crashes (Python 3.8+)
        def _threadhook(args):
            crash.critical("Uncaught thread exception",
                           exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = _threadhook
    except Exception:
        pass

    try:  # hard crashes (segfaults etc.) dump a native traceback to the same file
        import faulthandler
        faulthandler.enable(file=logstream)
    except Exception:
        pass

    logging.getLogger("djprep").info("==== session start ====")
    return logpath


def _center_pos(w, h):
    """Best-effort screen-centered (x, y) for the window. Empty dict if we can't tell,
    letting pywebview use its default placement."""
    try:
        if os.name == "nt":
            import ctypes
            u = ctypes.windll.user32
            sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            if sw and sh:
                # Sit a touch above true-center - looks more natural than dead-centre.
                return {"x": max(0, (sw - w) // 2), "y": max(0, (sh - h) // 2 - 30)}
    except Exception:
        pass
    return {}


def main():
    _setup_logging()
    log = logging.getLogger("djprep")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log.info("starting; port=%s", port)

    try:
        import webview
    except ImportError:
        # Dev fallback: no pywebview installed -> open in the default browser.
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        _serve(port)
        return

    # Make sure the native web runtime exists before we try to open a window.
    ensure_webview2()
    log.info("webview2 present=%s; launching server + window", _webview2_present())

    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    # Wait until the local server actually answers before opening the window. Without
    # this, the WebView can navigate to the URL before Flask is listening and just show
    # a blank page (it does not auto-retry). This is the usual "blank window" cause.
    ready = _wait_for_server(port)
    log.info("server ready=%s", ready)

    win_w, win_h = 600, 748
    pos = _center_pos(win_w, win_h)
    global WINDOW
    try:
        WINDOW = webview.create_window(
            "DJ Copyright Prep", url,
            width=win_w, height=win_h, resizable=False, **pos,
        )
        log.info("window created %sx%s pos=%s; starting GUI loop", win_w, win_h, pos)
        webview.start()
        log.info("GUI loop ended")
    except Exception:
        log.exception("fatal error creating/starting window")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            logging.getLogger("crash").critical("fatal error in main", exc_info=True)
        except Exception:
            pass
        raise
