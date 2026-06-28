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

# Set once the native window exists; lets the /pick-folder route use the OS-native dialog.
WINDOW = None

# Bumped each release to match the GitHub tag (e.g. tag v1.2.0 -> APP_VERSION "1.2.0").
# The app compares this to the latest release to offer an in-app update notice.
APP_VERSION = "1.2.0"
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


# --------------------------------------------------------------------------------------
# Core pipeline - generator that yields progress dicts (consumed by the SSE route)
# --------------------------------------------------------------------------------------
def process(folder, trim, offset, length, bitrate, color, audio_only=False):
    files = list_audio(folder)
    if not files:
        yield {"error": "No audio files found in that folder."}
        return

    work = Path(folder) / "_djprep_tmp"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # Audio-only is delivered as uncompressed WAV; video keeps the small AAC track.
    ext = ".wav" if audio_only else ".mp4"
    seg_ext = ".wav" if audio_only else ".m4a"
    out_name = (Path(folder).name or "songs") + "_copyright" + ext
    out_path = str(Path(folder) / out_name)

    try:
        segments = []
        total = len(files)
        for i, name in enumerate(files):
            src = os.path.join(folder, name)
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


@app.route("/pick-folder", methods=["POST"])
def pick_folder():
    """Open a native folder picker. Uses the pywebview window's own dialog when running
    as the desktop app (pywebview marshals it to the GUI thread for us); falls back to a
    tkinter dialog when running in a plain browser for development."""
    folder = None
    try:
        if WINDOW is not None:
            import webview
            # FileDialog.FOLDER on newer pywebview; FOLDER_DIALOG on older.
            folder_mode = getattr(getattr(webview, "FileDialog", None), "FOLDER",
                                  getattr(webview, "FOLDER_DIALOG", 2))
            result = WINDOW.create_file_dialog(folder_mode)
            folder = result[0] if result else None
        else:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(title="Choose your song folder")
            root.destroy()
    except Exception as e:
        return jsonify({"error": f"Could not open folder picker: {e}"}), 500

    if not folder:
        return jsonify({"cancelled": True})
    files = list_audio(folder)
    return jsonify({"path": folder, "files": files, "count": len(files)})


@app.route("/process")
def process_route():
    folder = request.args.get("path", "")
    trim = request.args.get("trim", "false") == "true"
    offset = float(request.args.get("offset", 30))
    length = float(request.args.get("length", 20))
    bitrate = int(request.args.get("bitrate", 64))
    color = "0x161619"  # fixed dark frame - colour never mattered for detection
    audio_only = request.args.get("audio_only", "false") == "true"

    if not folder or not os.path.isdir(folder):
        return Response("data: " + json.dumps({"error": "Folder not found."}) + "\n\n",
                        mimetype="text/event-stream")

    def stream():
        for evt in process(folder, trim, offset, length, bitrate, color, audio_only):
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
.folderbox{display:flex;align-items:center;gap:12px}
.path{flex:1;min-width:0;font-size:12.5px;color:var(--text-dim);word-break:break-all}
.count{color:var(--accent);font-weight:600}
.files{margin-top:10px;max-height:150px;overflow:auto;border:1px solid var(--border);
  border-radius:8px;padding:6px;display:none}
.files.show{display:block}
.file{padding:4px 8px;font-size:12.5px;color:var(--text);border-radius:5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file:nth-child(odd){background:var(--bg-elev2)}
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
    <div class="label">1 · Song folder</div>
    <div class="folderbox">
      <button id="pick" onclick="pick()">📁 Choose folder</button>
      <div class="path" id="path">No folder selected</div>
    </div>
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
let folder = null, outPath = null, outputMode = 'video', updInfo = null;

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

async function pick(){
  const btn = document.getElementById('pick');
  btn.disabled = true; btn.textContent = 'Opening…';
  try{
    const d = await (await fetch('/pick-folder', {method:'POST'})).json();
    if(d.cancelled){ return; }
    if(d.error){ setStatus(d.error, true); return; }
    folder = d.path;
    document.getElementById('path').innerHTML =
      d.path + ' · <span class="count">' + d.count + ' song' + (d.count==1?'':'s') + '</span>';
    const fl = document.getElementById('files');
    fl.innerHTML = d.files.map(f => '<div class="file">'+escapeHtml(f)+'</div>').join('');
    fl.classList.toggle('show', d.count>0);
    document.getElementById('go').disabled = d.count===0;
    if(d.count===0) setStatus('No audio files in that folder.', true); else setStatus('');
  } finally {
    btn.disabled = false; btn.textContent = '📁 Choose folder';
  }
}

function go(){
  if(!folder) return;
  const trim = document.getElementById('trim').checked;
  const q = new URLSearchParams({
    path: folder, trim: trim,
    offset: document.getElementById('offset').value || 30,
    length: document.getElementById('length').value || 20,
    bitrate: document.getElementById('bitrate').value,
    audio_only: (outputMode === 'audio'),
  });
  document.getElementById('go').disabled = true;
  document.getElementById('pick').disabled = true;
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
  document.getElementById('go').disabled = false;
  document.getElementById('pick').disabled = false;
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
