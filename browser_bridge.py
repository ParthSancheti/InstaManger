"""
Universal AI Automation Connector — Local Python Bridge  (v5)
=============================================================

Pairs with the custom Chrome extension in ./extension (Manifest V3).
Drives ChatGPT, Claude, Gemini, Google Flow and ElevenLabs.

v5 CHANGES
----------
1. OS-LEVEL WINDOW FOCUS (the big fix): before ANY Ctrl+V, Python now
   physically forces the automation Chrome window into the Windows
   foreground (ctypes + EnumWindows matched by the dedicated profile's
   PIDs, with the ALT-tap trick to defeat focus-stealing protection).
   chrome.windows.update({focused:true}) alone was NOT enough.
2. /force-paste-submit now accepts the prompt TEXT and puts it on the OS
   clipboard itself — the page-side navigator.clipboard needs focus,
   which was exactly what was broken.
3. /ping no longer refreshes the extension-liveness timestamp (it made
   any health check FAKE a live extension). New GET /status endpoint.
4. New HTTP task API for external tools (test_suite.py, the future
   main_orchestrator.py):  POST /queue-task   GET /result/<id>

ENDPOINTS
---------
GET  /ping                       bridge health check (does NOT touch liveness)
GET  /status                     bridge + extension + queue status
GET  /get-task?wait=25           extension long-polls for work
POST /submit-result              extension reports success/failure + payload
POST /request-paste              Gemini file paste (focus + clipboard + Ctrl+V)
POST /force-paste-submit         Flow prompt paste (focus + clipboard + Ctrl+V + Enter)
POST /queue-task                 external clients queue a task -> {id}
GET  /result/<task_id>           poll a task result -> {done, result}

INSTALL
-------
    pip install flask pyautogui psutil
    (psutil is optional but makes window-focus matching much more reliable)
"""

import base64
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 5000
BASE_DIR = Path(__file__).resolve().parent
EXTENSION_DIR = BASE_DIR / "extension"          # the unpacked MV3 extension
PROFILE_DIR = BASE_DIR / "chrome-profile"       # DEDICATED automation profile
DOWNLOADS_DIR = Path.home() / "Downloads"       # fallback scan location only
RESULT_TIMEOUT_S = 15 * 60

# The extension long-polls every ~25s, so anything inside this window = alive.
LIVE_WINDOW_S = 35
# How long to wait for the extension after launching Chrome.
CONNECT_TIMEOUT_S = 60

# Which site each task type needs (the EXTENSION opens these, not Python).
PLATFORM_URLS = {
    "img":    "https://chatgpt.com/",
    "pdf":    "https://claude.ai/new",
    "gemini": "https://gemini.google.com/app",
    "video":  "https://labs.google/fx/tools/flow",
    "tts":    "https://elevenlabs.io/app/speech-synthesis/text-to-speech",
}
DEFAULT_VOICE = "banty"          # ElevenLabs voice searched in the Explore tab
ALL_URLS = list(dict.fromkeys(PLATFORM_URLS.values()))
IDLE_URL = "about:blank"             # what a launched Chrome shows

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

app = Flask(__name__)


@app.after_request
def _cors(resp):
    """Allow the extension's CONTENT SCRIPTS to call the bridge.

    Content scripts run in the page's origin (e.g. gemini.google.com), so a
    fetch to localhost:5000 is cross-origin and Chrome blocks it unless the
    server opts in. host_permissions does NOT cover this case.
    """
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/<path:_any>", methods=["OPTIONS"])
def _preflight(_any):
    return ("", 204)

# Silence werkzeug's per-request lines — they interleave with input() prompts
# and make the menu unusable. The [bridge] prints below stay.
logging.getLogger("werkzeug").disabled = True
logging.getLogger("werkzeug").setLevel(logging.CRITICAL)
app.logger.disabled = True

_tasks: "queue.Queue[dict]" = queue.Queue()     # single global queue —
_results: dict[str, dict] = {}                  # the extension routes by type
_results_lock = threading.Lock()
_last_poll = 0.0                                # last time the EXTENSION asked for work
_chrome_procs: list = []                        # launched Chrome processes
_LAUNCH_COOLDOWN_S = 90                         # min gap between Chrome launches
_last_launch_ts: float = 0.0

import atexit
def _cleanup_chrome_on_exit():
    for p in _chrome_procs:
        try:
            p.terminate()
        except Exception:
            pass
atexit.register(_cleanup_chrome_on_exit)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def clean_path(raw) -> str:
    """Strip surrounding quotes (Windows 'Copy as path' adds them) + whitespace.
    Without this, D:\\PSG\\a.png pasted as "D:\\PSG\\a.png" silently breaks."""
    if raw is None:
        return ""
    return str(raw).strip().strip('"').strip("'").strip()


def shutdown() -> None:
    """Hard-stop: kills the Flask thread with the process, no hanging."""
    print("bye")
    sys.stdout.flush()
    os._exit(0)          # bypasses thread joins / atexit hooks entirely


# ---------------------------------------------------------------------------
# Chrome lifecycle (dedicated automation profile)
# ---------------------------------------------------------------------------

def find_chrome() -> str | None:
    """Locate chrome.exe / google-chrome on this machine."""
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    # Windows fallback: ask the registry where Chrome lives.
    if sys.platform == "win32":
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(
                        root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                    path = winreg.QueryValue(key, None)
                    if path and Path(path).exists():
                        return path
                except OSError:
                    continue
        except Exception:
            pass
    return None


def extension_live() -> bool:
    """True only if the extension polled us within one long-poll cycle.
    This is the ONLY reliable signal that Chrome + extension are both up.
    v5: ONLY /get-task and /submit-result refresh the timestamp — /ping
    used to refresh it too, which let any health check fake liveness."""
    return (time.time() - _last_poll) < LIVE_WINDOW_S


def chrome_process_alive() -> bool:
    """Best-effort: is a Chrome process we launched still running?
    (Only a hint — Chrome may have been started outside this script.)"""
    return any(p.poll() is None for p in _chrome_procs)


def launch_chrome(first_time: bool = False, urls: list[str] | None = None) -> bool:
    """Launch automation Chrome with the dedicated profile + extension.

    If Chrome is already running on this profile, the OS just opens the URL
    as a new tab in that window and this process exits immediately — which is
    fine and harmless.

    Returns False only when Chrome itself couldn't be found/started.
    """
    global _last_launch_ts
    _now = time.time()
    if chrome_process_alive() and (_now - _last_launch_ts) < _LAUNCH_COOLDOWN_S:
        print(f"[chrome] launch suppressed - already running "
              f"({int(_now - _last_launch_ts)}s ago, {_LAUNCH_COOLDOWN_S}s cooldown)")
        return True
    _last_launch_ts = _now

    time.sleep(4)  # give a just-closed Chrome a moment to release the profile
    chrome = find_chrome()
    if not chrome:
        print("[chrome] ERROR: Chrome not found.")
        print("         Edit CHROME_CANDIDATES at the top of bridge.py with your")
        print("         chrome.exe path, then try again.")
        return False

    if not EXTENSION_DIR.exists():
        print(f"[chrome] ERROR: extension folder missing: {EXTENSION_DIR}")
        print("         The 'extension' folder must sit next to bridge.py.")
        return False

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        chrome,
        f"--user-data-dir={PROFILE_DIR}",           # isolation from your other profiles
        f"--load-extension={EXTENSION_DIR}",        # auto-load the unpacked extension
        # Recent Chrome silently IGNORES --load-extension unless this is set.
        # Without it the browser opens fine but the extension never appears.
        "--disable-features=DisableLoadExtensionCommandLineSwitch",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session=false",
    ]

    if urls:
        args += urls
    elif first_time:
        # First run only: extensions page + every platform so you can log in.
        args += ["chrome://extensions/"] + ALL_URLS
    else:
        # Normal start: just Chrome. The extension opens task tabs itself.
        args += [IDLE_URL]

    try:
        _chrome_procs.append(subprocess.Popen(args))
    except Exception as exc:
        print(f"[chrome] ERROR: could not start Chrome: {exc}")
        return False

    print(f"[chrome] launched automation Chrome (profile: {PROFILE_DIR})")

    if first_time:
        print(
            "\n================ FIRST-TIME SETUP ================\n"
            "1. If the extension icon is NOT in the toolbar:\n"
            "   chrome://extensions -> enable 'Developer mode' (top right)\n"
            f"   -> 'Load unpacked' -> select: {EXTENSION_DIR}\n"
            "2. Log in to ChatGPT, Claude, Gemini, Flow and ElevenLabs.\n"
            "3. Check menu option 9 — it should say the extension is LIVE.\n"
            "==================================================\n"
        )
    return True


def open_extensions_page() -> None:
    """Open chrome://extensions in the automation profile so the extension
    can be loaded manually."""
    launch_chrome(urls=["chrome://extensions/"])
    print("[chrome] chrome://extensions opened in the automation profile.")
    print("         Developer mode -> 'Load unpacked' -> select:")
    print(f"         {EXTENSION_DIR}")


def ensure_chrome(timeout_s: int = CONNECT_TIMEOUT_S, quiet: bool = False) -> bool:
    """Guarantee automation Chrome + extension are live.

    1. Already live?            -> return True immediately.
    2. Not live                 -> launch Chrome, then wait for the extension.
    3. Still not live in time   -> print exactly what to fix, return False.
    """
    if extension_live():
        return True

    if not quiet:
        print("[bridge] extension not connected -> launching automation Chrome ...")

    if not launch_chrome(first_time=not PROFILE_DIR.exists()):
        return False        # Chrome itself is missing — message already printed

    deadline = time.time() + timeout_s
    waited = 0
    while time.time() < deadline:
        if extension_live():
            print("[bridge] extension connected OK                    ")
            return True
        time.sleep(1)
        waited += 1
        if not quiet and waited % 5 == 0:
            print(f"[bridge]   waiting for extension ... {waited}s", end="\r", flush=True)

    print("\n[bridge] ERROR: Chrome is up but the extension never connected.")
    print("        Most likely the extension isn't loaded in that profile.")
    print("        Fix it once:")
    print("          chrome://extensions -> Developer mode (top right)")
    print(f"          -> 'Load unpacked' -> select: {EXTENSION_DIR}")
    print("        The extension icon then shows a green dot badge.")
    print("        (Menu option 10 opens that page for you.)")
    return False


# ---------------------------------------------------------------------------
# v5: OS-LEVEL WINDOW FOCUS  (the paste-focus fix)
# ---------------------------------------------------------------------------

def _automation_chrome_pids() -> set[int]:
    """PIDs of Chrome processes running on OUR dedicated profile.
    psutil (if installed) matches by command line — bulletproof even with
    other Chrome profiles open. Fallback: the processes we launched."""
    pids: set[int] = set()
    try:
        import psutil
        marker = str(PROFILE_DIR).lower()
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (p.info["name"] or "").lower()
                if "chrome" not in name:
                    continue
                cmd = " ".join(p.info["cmdline"] or []).lower()
                if marker in cmd:
                    pids.add(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except ImportError:
        pass
    if not pids:
        pids = {p.pid for p in _chrome_procs if p.poll() is None}
    return pids


def focus_automation_chrome(settle_s: float = 0.6) -> bool:
    """FORCE the automation Chrome window into the OS foreground.

    Windows actively blocks background processes from stealing focus
    (SetForegroundWindow silently fails), so we:
      1. Find the top-level window belonging to our profile's Chrome PIDs.
      2. Restore it if minimized (SW_RESTORE).
      3. Tap ALT via keybd_event — the classic trick that makes Windows
         treat us as "the last input process" and allow the focus switch.
      4. SetForegroundWindow + BringWindowToTop.
      5. Verify GetForegroundWindow actually changed.

    Returns True when the window is verified to be in the foreground.
    On non-Windows platforms this is a no-op returning True (the
    extension-side chrome.windows.update focus is sufficient there).
    """
    if sys.platform != "win32":
        return True

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pids = _automation_chrome_pids()

        hwnds: list[int] = []
        chrome_titled: list[int] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            
            # Chrome spawns multiple "visible" windows that are actually 0x0 hidden helper windows.
            # We must ignore them so we target the real browser window.
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if (rect.right - rect.left) < 100 or (rect.bottom - rect.top) < 100:
                return True

            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                hwnds.append(hwnd)
            elif title.endswith("Google Chrome"):
                chrome_titled.append(hwnd)      # last-resort fallback pool
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)

        target = hwnds[0] if hwnds else (chrome_titled[0] if chrome_titled else None)
        if target is None:
            print("[focus] no automation Chrome window found")
            return False

        SW_RESTORE = 9
        VK_MENU = 0x12                      # ALT
        KEYEVENTF_KEYUP = 0x0002

        if user32.IsIconic(target):         # minimized → restore first
            user32.ShowWindow(target, SW_RESTORE)
            time.sleep(0.3)

        # ALT tap: unlocks SetForegroundWindow from a background process.
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)

        user32.SetForegroundWindow(target)
        user32.BringWindowToTop(target)
        time.sleep(settle_s)

        ok = user32.GetForegroundWindow() == target
        if not ok:
            # One stubborn retry with a minimize→restore bounce.
            SW_MINIMIZE = 6
            user32.ShowWindow(target, SW_MINIMIZE)
            time.sleep(0.25)
            user32.ShowWindow(target, SW_RESTORE)
            time.sleep(0.25)
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.SetForegroundWindow(target)
            time.sleep(settle_s)
            ok = user32.GetForegroundWindow() == target

        print(f"[focus] automation Chrome foreground: {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as exc:
        print(f"[focus] error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send(task_type: str, prompt: str, path: str | None = None, *,
         n: int = 1, paths: list[str] | None = None, chat_code: str | None = None,
         voice: str | None = None) -> str:
    """Queue a task and return its id.

    Blocks until automation Chrome + extension are confirmed live, so a task
    can never sit in the queue with nothing listening.
    Raises RuntimeError if the extension can't be brought up.
    """
    if task_type not in PLATFORM_URLS:
        raise ValueError(
            f"Unknown task type: {task_type!r} (expected img/pdf/gemini/video/tts)")

    # Chrome check FIRST — don't queue work nobody will pick up.
    if not ensure_chrome():
        raise RuntimeError("Automation Chrome/extension not available — task NOT queued")

    task = {
        "id": uuid.uuid4().hex[:10],
        "type": task_type,
        "prompt": prompt,
        "path": clean_path(path) if path else None,
        "n": n,
        "paths": [clean_path(p) for p in (paths or []) if clean_path(p)][:3],
        "chat_code": chat_code.strip() if chat_code else None,
        "voice": (voice or DEFAULT_VOICE).strip(),      # ElevenLabs only
        "queued_at": time.time(),
    }

    _tasks.put(task)
    print(f"[bridge] queued {task_type} task {task['id']}")
    return task["id"]


def wait_result(task_id: str, timeout: float = RESULT_TIMEOUT_S) -> dict:
    """Block until the extension reports a result for task_id (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _results_lock:
            if task_id in _results:
                return _results.pop(task_id)
        time.sleep(0.5)
    return {"id": task_id, "ok": False,
            "error": f"No result within {timeout}s (extension connected? menu option 9)"}


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@app.get("/ping")
def ping():
    # v5: does NOT refresh _last_poll — that made health checks fake a live
    # extension. Liveness comes ONLY from the extension's own calls.
    return jsonify(ok=True, ts=time.time())

@app.post("/shutdown")
def shutdown_route():   # renamed: was shadowing the module-level shutdown()
    """Kill all managed Chrome instances and gracefully shut down the bridge."""
    import os
    import threading
    for p in _chrome_procs:
        try:
            p.terminate()
        except Exception:
            pass
    
    def hard_exit():
        time.sleep(1)
        os._exit(0)
    threading.Thread(target=hard_exit, daemon=True).start()
    return jsonify(ok=True, msg="Shutting down")


@app.get("/status")
def status():
    """Bridge + extension + queue status for external tools (test suite)."""
    age = time.time() - _last_poll if _last_poll else None
    return jsonify(
        ok=True,
        extension_live=extension_live(),
        last_poll_age_s=round(age, 1) if age is not None else None,
        pending_tasks=_tasks.qsize(),
        chrome_launched=chrome_process_alive(),
        profile_dir=str(PROFILE_DIR),
    )


@app.get("/get-task")
def get_task():
    """Long-poll: hold the request open up to `wait` seconds until a task
    arrives. Keeps the extension's service worker alive and makes pickup
    nearly instant. Every call also refreshes the liveness timestamp."""
    global _last_poll
    _last_poll = time.time()
    wait_s = min(float(request.args.get("wait", 0) or 0), 30.0)
    try:
        task = _tasks.get(timeout=wait_s) if wait_s > 0 else _tasks.get_nowait()
        print(f"[bridge] task {task['id']} claimed by extension")
        return jsonify(task=task)
    except queue.Empty:
        return jsonify(task=None)


@app.post("/submit-result")
def submit_result():
    global _last_poll
    _last_poll = time.time()
    payload = request.get_json(force=True, silent=True) or {}
    task_id = payload.get("id", "unknown")
    try:
        if payload.get("ok"):
            payload = _post_process(payload)
            print(f"[bridge] OK  task {task_id} SUCCESS")
        else:
            print(f"[bridge] ERR task {task_id} FAILED: {payload.get('error')}")
    except Exception as exc:                    # post-processing must never lose a result
        payload["ok"] = False
        payload["error"] = f"post-process failed: {exc}"
        print(f"[bridge] ERR task {task_id} post-process error: {exc}")
    with _results_lock:
        _results[task_id] = payload
    return jsonify(ok=True)


@app.post("/close-gracefully")
def close_gracefully():
    """Tells the extension to close all tabs one by one, gracefully exiting Chrome."""
    global _last_poll
    task_id = os.urandom(5).hex()
    _tasks.put({
        "id": task_id,
        "type": "close_browser",
        "prompt": ""
    })
    # Reset liveness immediately so the next normal task natively re-launches Chrome
    _last_poll = 0
    return jsonify(ok=True)



@app.post("/restart-chrome")
def restart_chrome_http():
    """Kill the automation Chrome and relaunch it (used by the Telegram panel)."""
    global _chrome_procs
    try:
        for pid in list(_automation_chrome_pids()):
            try:
                import psutil
                psutil.Process(pid).kill()
            except Exception:
                pass
    except Exception:
        pass
    for p in _chrome_procs:
        try:
            p.kill()
        except Exception:
            pass
    _chrome_procs = []
    time.sleep(2.0)
    ok = ensure_chrome(quiet=True)
    return jsonify(ok=ok, extension_live=extension_live())



@app.post("/upload-direct")
def upload_direct():
    import base64
    payload = request.json
    if not payload:
        return jsonify(ok=False, error="No payload")
    
    filename = payload.get("filename", "video.mp4")
    b64 = payload.get("b64", "")
    if "," in b64:
        b64 = b64.split(",", 1)[1]
        
    target_path = payload.get("target_path")
    if target_path:
        out = Path(clean_path(target_path))
        if payload.get("is_multiple") and payload.get("download_index"):
            idx = payload.get("download_index")
            out = out.with_name(f"{out.stem}_{idx}{out.suffix}")
    else:
        out = DOWNLOADS_DIR / filename
        
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(b64))
    print(f"[bridge] DIRECT UPLOAD SUCCESS -> {out}")
    
    return jsonify(ok=True, path=str(out))

@app.post("/queue-task")
def queue_task_http():
    """v5: external clients (test_suite.py / main_orchestrator.py) queue a
    task over HTTP instead of importing this module."""
    payload = request.get_json(force=True, silent=True) or {}
    try:
        tid = send(
            payload.get("type", ""),
            payload.get("prompt", ""),
            payload.get("path"),
            n=int(payload.get("n", 1) or 1),
            paths=payload.get("paths") or [],
            chat_code=payload.get("chat_code"),
            voice=payload.get("voice"),
        )
        return jsonify(ok=True, id=tid)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.get("/result/<task_id>")
def result_http(task_id: str):
    """v5: non-blocking result poll for external clients."""
    with _results_lock:
        if task_id in _results:
            return jsonify(done=True, result=_results.pop(task_id))
    return jsonify(done=False)


@app.post("/request-paste")
def request_paste():
    """Gemini file handshake: copy `file` to OS clipboard, FORCE the
    automation Chrome window into the OS foreground, then Ctrl+V."""
    global _last_poll
    _last_poll = time.time()
    payload = request.get_json(force=True, silent=True) or {}
    file_path = Path(clean_path(payload.get("file", "")))
    if not file_path.exists():
        return jsonify(ok=False, error=f"file not found: {file_path}")
    try:
        _copy_file_to_clipboard(file_path)
        time.sleep(0.5)
        focus_automation_chrome()               # v5: OS-level focus BEFORE paste
        _send_ctrl_v()
        return jsonify(ok=True, file=str(file_path))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc))


@app.post("/force-paste-submit")
def force_paste_submit():
    """Flow prompt handshake (v5):
      1. If `text` was sent, put it on the OS clipboard from PYTHON — the
         page-side navigator.clipboard silently fails without focus.
      2. FORCE the automation Chrome window into the OS foreground.
      3. Physically press Ctrl+V, then Enter.
    """
    global _last_poll
    _last_poll = time.time()
    payload = request.get_json(force=True, silent=True) or {}
    text = payload.get("text")
    try:
        if text:
            _copy_text_to_clipboard(str(text))
            time.sleep(0.3)

        focused = focus_automation_chrome()     # v5: THE focus fix
        if not focused:
            print("[bridge] WARNING: could not verify Chrome focus — pasting anyway")

        import pyautogui
        pyautogui.FAILSAFE = False
        # Tap ESC to close any window menus that the ALT focus hack might have opened
        pyautogui.press('esc')
        time.sleep(0.1)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.1)
        pyautogui.press('enter')
        return jsonify(ok=True, focused=focused)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc))


@app.get("/get-clipboard")
def get_clipboard():
    """Return the current OS clipboard text to the extension.

    Always answers with JSON. The import lives inside the try because a missing
    pyperclip used to raise before Flask could serialise anything, handing the
    extension a 500 HTML page that res.json() choked on.
    """
    try:
        import pyperclip
        return jsonify(ok=True, text=pyperclip.paste() or "")
    except ImportError:
        try:                                    # Windows fallback, no deps
            if sys.platform == "win32":
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                    capture_output=True, text=True, timeout=15)
                return jsonify(ok=True, text=out.stdout or "")
        except Exception as exc:
            return jsonify(ok=False, error=f"clipboard fallback failed: {exc}")
        return jsonify(ok=False, error="pyperclip not installed (pip install pyperclip)")
    except Exception as exc:
        return jsonify(ok=False, error=str(exc))


# ---------------------------------------------------------------------------
# Result post-processing
# ---------------------------------------------------------------------------

def _post_process(payload: dict) -> dict:
    data = payload.get("data") or {}

    # 1) ChatGPT image: base64 -> file at the requested path.
    if data.get("image_b64"):
        out = Path(clean_path(data.get("path")) or (DOWNLOADS_DIR / f"chatgpt_{payload['id']}.png"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(data.pop("image_b64")))
        data["saved_to"] = str(out)
        print(f"[bridge]   image saved -> {out}")

    # 2) Claude PDF / Flow videos: the extension reports EXACT absolute paths
    #    via chrome.downloads — just move them to the target.
    if data.get("move_from_downloads"):
        exact = [Path(p) for p in data.get("download_files") or [] if p]
        if exact:
            data["saved_to"] = _move_files(exact, data.get("path"))
        else:  # fallback: newest matching file in Downloads
            data["saved_to"] = _move_latest_downloads(
                data.get("expect_ext", ""), data.get("path"),
                count=data.get("downloaded_count", 1))
        print(f"[bridge]   moved -> {data['saved_to']}")

    payload["data"] = data
    return payload


def _move_files(files: list, target: str | None) -> list[str]:
    moved: list[str] = []
    target_path = Path(clean_path(target)) if target else None
    for i, f in enumerate(files):
        f = Path(f)
        if not f.exists():
            print(f"[bridge]   warning: reported file missing: {f}")
            continue
        if target_path is None:
            moved.append(str(f))
            continue
        if len(files) == 1 and target_path.suffix:          # exact file path given
            dest = target_path
        elif target_path.suffix:                            # many files, one path -> number them
            dest = target_path.with_name(f"{target_path.stem}_{i + 1}{target_path.suffix}")
        else:                                               # directory given
            dest = target_path / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        moved.append(str(dest))
    return moved


def _move_latest_downloads(ext: str, target: str | None, count: int = 1,
                           max_age_s: int = 15 * 60, settle_timeout_s: int = 120) -> list[str]:
    """Fallback when chrome.downloads didn't report paths: grab the newest
    recent file(s) with the right extension from the Downloads folder."""
    deadline = time.time() + settle_timeout_s
    while time.time() < deadline:                       # wait for partial downloads
        if not (list(DOWNLOADS_DIR.glob("*.crdownload")) + list(DOWNLOADS_DIR.glob("*.part"))):
            break
        time.sleep(2)
    now = time.time()
    candidates = sorted(
        (f for f in DOWNLOADS_DIR.glob(f"*{ext}") if now - f.stat().st_mtime < max_age_s),
        key=lambda f: f.stat().st_mtime, reverse=True)[:max(1, count)]
    if not candidates:
        raise FileNotFoundError(f"No recent *{ext} file found in {DOWNLOADS_DIR}")
    return _move_files(candidates, target)


# ---------------------------------------------------------------------------
# Clipboard + keystroke helpers
# ---------------------------------------------------------------------------

def _copy_file_to_clipboard(file_path: Path) -> None:
    """Put an actual FILE (not text) on the OS clipboard."""
    if sys.platform == "win32":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f'Set-Clipboard -Path "{file_path}"'], check=True, timeout=15)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        f'set the clipboard to (POSIX file "{file_path}")'], check=True, timeout=15)
    else:
        subprocess.run(["xclip", "-selection", "clipboard", "-t", "text/uri-list"],
                       input=f"file://{file_path}".encode(), check=True, timeout=15)


def _copy_text_to_clipboard(text: str) -> None:
    """Put TEXT on the OS clipboard — quote/emoji/multiline safe.
    Tries pyperclip; falls back to a temp-file + PowerShell pipe (Windows),
    pbcopy (macOS) or xclip (Linux). Never inlines the text into a shell
    command, so no escaping bugs ever."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return
    except Exception:
        pass

    tmp = Path(tempfile.gettempdir()) / f"ai_connector_clip_{os.getpid()}.txt"
    tmp.write_text(text, encoding="utf-8")
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f'Get-Content -Raw -Encoding UTF8 "{tmp}" | Set-Clipboard'],
                check=True, timeout=15)
        elif sys.platform == "darwin":
            with open(tmp, "rb") as fh:
                subprocess.run(["pbcopy"], stdin=fh, check=True, timeout=15)
        else:
            with open(tmp, "rb") as fh:
                subprocess.run(["xclip", "-selection", "clipboard"],
                               stdin=fh, check=True, timeout=15)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _send_ctrl_v() -> None:
    """Ctrl+V into the currently focused window (the automation Chrome)."""
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.hotkey("ctrl", "v")
    except ImportError as exc:
        raise RuntimeError("pyautogui not installed — pip install pyautogui") from exc


# ---------------------------------------------------------------------------
# Interactive test menu
# ---------------------------------------------------------------------------

MENU = """
==================== AI CONNECTOR - TEST MENU (v5) ====================
 1) ChatGPT image      send(img, prompt, path)
 2) Claude PDF         send(pdf, prompt, path)
 3) Gemini prompt      code(n, prompt [, paths...])
 4) Gemini resume      code(chat_code, prompt [, paths...])
 5) Flow video         code(n, prompt, path)
 6) ElevenLabs voice   code(text, path [, voice])
 7) Show pending queue size
 8) (Re)launch automation Chrome
 9) Check extension connection (auto-fixes)
10) Open chrome://extensions to load the extension
11) Test window focus (v5 focus fix check)
 0) Exit
=======================================================================
  TIP: run  python test_suite.py  in another terminal for the full
       automated interconnectivity test before deploying.
"""


def _connection_report() -> None:
    """Option 9: don't just complain — try to fix it."""
    if extension_live():
        print(f"[check] extension is LIVE (last poll {time.time() - _last_poll:.1f}s ago)")
        return

    print("[check] not connected -> launching Chrome and waiting ...")
    if ensure_chrome():
        print("[check] extension is LIVE now — you can run tasks.")
    else:
        print("[check] FAILED — the extension is not loaded in that Chrome profile.")
        print("        Use menu option 10, then 'Load unpacked' the extension folder.")


def _run_and_report(task_id: str) -> None:
    """send() already confirmed the extension is live, so no blind sleep here."""
    print(f"[menu] waiting for result of {task_id} ...")
    result = wait_result(task_id)
    print("\n---------------- RESULT ----------------")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("----------------------------------------\n")


def _ask_paths(limit: int = 3) -> list[str]:
    raw = input(f"File paths to attach (comma-separated, up to {limit}, blank for none): ").strip()
    if not raw:
        return []
    return [clean_path(p) for p in raw.split(",") if clean_path(p)][:limit]


def menu_loop() -> None:
    while True:
        print(MENU)
        choice = input("Select> ").strip()
        try:
            if choice == "1":
                prompt = input("Image prompt: ").strip()
                path = clean_path(input("Save image to (full path .png): "))
                _run_and_report(send("img", prompt, path))
            elif choice == "2":
                prompt = input("PDF prompt: ").strip()
                path = clean_path(input("Save PDF to (full path .pdf): "))
                _run_and_report(send("pdf", prompt, path))
            elif choice == "3":
                n = int(input("n: ").strip() or "1")
                prompt = input("Prompt: ").strip()
                _run_and_report(send("gemini", prompt, n=n, paths=_ask_paths()))
            elif choice == "4":
                chat_code = input("chat_code: ").strip()
                prompt = input("Prompt: ").strip()
                _run_and_report(send("gemini", prompt, chat_code=chat_code, paths=_ask_paths()))
            elif choice == "5":
                n = int(input("How many videos (n): ").strip() or "1")
                prompt = input("Video prompt: ").strip()
                path = clean_path(input("Save video(s) to (file path .mp4 or folder): "))
                _run_and_report(send("video", prompt, path, n=n))
            elif choice == "6":
                text = input("Text to speak: ").strip()
                path = clean_path(input("Save audio to (full path .mp3): "))
                voice = input(f"Voice name (blank = {DEFAULT_VOICE}): ").strip() or None
                _run_and_report(send("tts", text, path, voice=voice))
            elif choice == "7":
                print(f"  pending tasks : {_tasks.qsize()}")
                print(f"  extension live: {extension_live()}")
            elif choice == "8":
                launch_chrome(first_time=False)
            elif choice == "9":
                _connection_report()
            elif choice in ("10", "l", "L"):
                open_extensions_page()
            elif choice == "11":
                print("[menu] switch to ANY other window now — focusing Chrome in 4s ...")
                time.sleep(4)
                ok = focus_automation_chrome()
                print(f"[menu] focus result: {'SUCCESS — Chrome is in front' if ok else 'FAILED'}")
            elif choice == "0":
                shutdown()
            else:
                print("Unknown option.")
        except (KeyboardInterrupt, EOFError):
            print("\n(cancelled)")
        except Exception as exc:
            print(f"[menu] error: {exc}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = threading.Thread(
        target=lambda: app.run(host=HOST, port=PORT, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True,
    )
    server.start()
    print(f"[bridge] listening on http://{HOST}:{PORT}")
    print(f"[bridge] extension dir: {EXTENSION_DIR}")
    time.sleep(0.8)

    # ALWAYS launch automation Chrome at startup — only Chrome, nothing else.
    first_run = not PROFILE_DIR.exists()
    if first_run:
        print("[bridge] first run detected -> full setup launch")
    launch_chrome(first_time=first_run)

    # Give it a moment, then confirm the extension actually connected.
    time.sleep(5)
    if ensure_chrome(timeout_s=45):
        print("[bridge] ready — extension connected.")
    else:
        print("[bridge] NOT ready — load the extension (menu option 10), then option 9.")

    interactive = ("--no-menu" not in sys.argv) and sys.stdin and sys.stdin.isatty()
    if not interactive:
        print("[bridge] non-interactive mode - menu disabled, serving until terminated.")
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, EOFError):
            shutdown()
    else:
        try:
            menu_loop()
        except (KeyboardInterrupt, EOFError):
            shutdown()
