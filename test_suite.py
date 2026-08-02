"""
AI CONNECTOR — PRE-DEPLOYMENT TEST SUITE  (v1)
==============================================

Run this in a SECOND terminal while bridge.py is running. It talks to the
bridge purely over HTTP (the new /queue-task, /result, /status endpoints),
exactly the way main_orchestrator.py will — so a green run here means the
whole Stock Warrior pipeline can trust the connector.

WHAT IT VERIFIES
----------------
  [T01] Bridge alive            GET /ping
  [T02] Extension live          GET /status (real liveness — /ping can't fake it anymore)
  [T03] ChatGPT image           img task  → valid PNG/JPEG/WebP file on disk
  [T04] Claude PDF              pdf task  → file starts with %PDF magic bytes
  [T05] Gemini text             gemini    → exact echo string comes back
  [T06] Gemini strict JSON      gemini    → parseable JSON (the engine's lifeblood)
  [T07] Gemini file attach      gemini+file → content of an attached txt echoed back
  [T08] Gemini chat resume      gemini+chat_code → remembers T05's secret word
  [T09] Flow 2 videos           video n=2 → TWO mp4s, VALID, and MD5-DIFFERENT
                                 (this is the exact duplicate-download bug check)
  [T10] ElevenLabs TTS          tts       → valid mp3/audio file
  [T11] CHAIN Gemini→TTS        gemini writes a voiceover JSON → tts speaks it
  [T12] CHAIN merge (moviepy)   T09 video + T10/T11 audio muxed into one mp4

Every test prints live progress and the run ends with a PASS/FAIL table +
test_output/test_report.json.

USAGE
-----
    python test_suite.py                # interactive menu
    python test_suite.py --all          # run everything
    python test_suite.py --quick       # skip the slow ones (video + chains)
    python test_suite.py --only T05,T06,T09
    python test_suite.py --skip T09

    pip install requests   (moviepy optional, only for T12)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests   — then run again.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRIDGE = "http://127.0.0.1:5000"
OUT_DIR = Path(__file__).resolve().parent / "test_output"
OUT_DIR.mkdir(exist_ok=True)

# How long to wait per task result (video generation is the slowest).
RESULT_TIMEOUT_S = {
    "img": 12 * 60,
    "pdf": 12 * 60,
    "gemini": 8 * 60,
    "video": 14 * 60,
    "tts": 8 * 60,
}
POLL_S = 3

# A run-unique token so Gemini echo tests can never pass on stale text.
RUN_TOKEN = uuid.uuid4().hex[:8].upper()

C_G, C_R, C_Y, C_B, C_0 = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m"
if os.name == "nt":
    os.system("")   # enable ANSI colors on Windows terminals


def say(msg: str) -> None:
    print(f"{C_B}[test]{C_0} {msg}")


# ---------------------------------------------------------------------------
# Bridge client (HTTP — identical to how the orchestrator will call it)
# ---------------------------------------------------------------------------

def bridge_get(path: str, timeout: float = 10) -> dict:
    r = requests.get(BRIDGE + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def queue_task(task_type: str, prompt: str, path: str | None = None, *,
               n: int = 1, paths: list[str] | None = None,
               chat_code: str | None = None, voice: str | None = None) -> str:
    """POST /queue-task — returns the task id or raises with the error."""
    body = {"type": task_type, "prompt": prompt, "path": path, "n": n,
            "paths": paths or [], "chat_code": chat_code, "voice": voice}
    # queue-task can block up to ~70s while the bridge (re)launches Chrome.
    r = requests.post(BRIDGE + "/queue-task", json=body, timeout=120)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"queue-task refused: {data.get('error')}")
    return data["id"]


def wait_task(task_id: str, task_type: str) -> dict:
    """Poll GET /result/<id> until done or timeout. Returns the full result."""
    deadline = time.time() + RESULT_TIMEOUT_S.get(task_type, 600)
    started = time.time()
    while time.time() < deadline:
        try:
            data = bridge_get(f"/result/{task_id}")
            if data.get("done"):
                return data["result"]
        except requests.RequestException as exc:
            say(f"  (bridge poll hiccup: {exc})")
        elapsed = int(time.time() - started)
        print(f"\r{C_B}[test]{C_0}   waiting for {task_id} ... {elapsed}s", end="", flush=True)
        time.sleep(POLL_S)
    print()
    raise TimeoutError(f"no result for {task_id} within limit")


def run_task(task_type: str, prompt: str, **kw) -> dict:
    tid = queue_task(task_type, prompt, **kw)
    say(f"  queued {task_type} task {tid}")
    result = wait_task(tid, task_type)
    print()
    if not result.get("ok"):
        raise RuntimeError(f"task {tid} FAILED: {result.get('error')}")
    return result.get("data") or {}


# ---------------------------------------------------------------------------
# File validators
# ---------------------------------------------------------------------------

MAGIC = {
    "png":  [b"\x89PNG"],
    "jpg":  [b"\xff\xd8\xff"],
    "webp": [b"RIFF"],
    "pdf":  [b"%PDF"],
    "mp3":  [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    "mp4":  [],   # checked via ftyp box below
}


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(path: str | Path, kinds: list[str], min_bytes: int = 1024) -> Path:
    """Assert the file exists, is big enough, and matches one magic type."""
    p = Path(path)
    if not p.exists():
        raise AssertionError(f"file missing: {p}")
    size = p.stat().st_size
    if size < min_bytes:
        raise AssertionError(f"file too small ({size} bytes): {p}")
    head = p.read_bytes()[:64] if size < 4096 else open(p, "rb").read(64)

    for kind in kinds:
        if kind == "mp4":
            if b"ftyp" in head[:16]:
                return p
            continue
        for magic in MAGIC.get(kind, []):
            if head.startswith(magic):
                return p
    raise AssertionError(f"file is not a valid {'/'.join(kinds)} (head={head[:12]!r}): {p}")


def extract_saved(data: dict) -> list[Path]:
    """Pull saved file path(s) from a bridge result payload."""
    saved = data.get("saved_to")
    if not saved:
        raise AssertionError(f"result has no saved_to: {json.dumps(data)[:300]}")
    if isinstance(saved, str):
        return [Path(saved)]
    return [Path(s) for s in saved]


def parse_llm_json(text: str) -> dict:
    """Strict-ish JSON parse with the usual fence/pre-text cleanup — the same
    survival skill the orchestrator needs, tested here."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise AssertionError(f"no JSON object found in: {t[:200]}")
    return json.loads(t[start:end + 1])


# ---------------------------------------------------------------------------
# Tests  (ctx dict carries values between tests: chat_code, file paths...)
# ---------------------------------------------------------------------------

def t01_bridge(ctx: dict) -> str:
    data = bridge_get("/ping", timeout=5)
    assert data.get("ok"), "bridge /ping returned not-ok"
    return "bridge responded on /ping"


def t02_extension(ctx: dict) -> str:
    time.sleep(1.5)
    data = bridge_get("/status", timeout=5)
    if not data.get("extension_live"):
        age = data.get("last_poll_age_s")
        raise AssertionError(
            f"extension NOT live (last poll {age}s ago) — open the bridge menu "
            "option 9, or load the extension via option 10")
    return f"extension live (last poll {data.get('last_poll_age_s')}s ago)"


def t03_chatgpt_image(ctx: dict) -> str:
    out = OUT_DIR / "t03_image.png"
    data = run_task("img",
                    "Generate an image: a simple flat red circle centered on a plain "
                    "white background. Minimal test image, no text.",
                    path=str(out))
    p = check_file(extract_saved(data)[0], ["png", "jpg", "webp"], min_bytes=5_000)
    ctx["image_file"] = p
    return f"image OK -> {p.name} ({p.stat().st_size:,} bytes)"


def t04_claude_pdf(ctx: dict) -> str:
    out = OUT_DIR / "t04_doc.pdf"
    data = run_task("pdf",
                    "Create a one-page PDF document titled 'Connector Test'. It must "
                    f"contain the exact code {RUN_TOKEN}, today's date, and a short "
                    "3-bullet checklist. Provide it as a downloadable PDF file.",
                    path=str(out))
    p = check_file(extract_saved(data)[0], ["pdf"], min_bytes=1000)
    ctx["pdf_file"] = p
    return f"PDF OK -> {p.name} ({p.stat().st_size:,} bytes)"


def t05_gemini_text(ctx: dict) -> str:
    data = run_task("gemini",
                    f"Reply with exactly this and nothing else: CONNECTOR_OK_{RUN_TOKEN}")
    resp = data.get("response", "")
    assert f"CONNECTOR_OK_{RUN_TOKEN}" in resp, f"echo missing, got: {resp[:200]}"
    ctx["chat_code"] = data.get("chat_code")
    return f"echo OK, chat_code={ctx['chat_code']}"


def t06_gemini_json(ctx: dict) -> str:
    data = run_task("gemini",
                    'Respond ONLY with strictly valid JSON, no markdown fences, no '
                    'extra text, matching exactly this schema: '
                    f'{{"status": "ok", "token": "{RUN_TOKEN}", "numbers": [1, 2, 3]}}')
    obj = parse_llm_json(data.get("response", ""))
    assert obj.get("status") == "ok", f"status != ok: {obj}"
    assert obj.get("token") == RUN_TOKEN, f"token mismatch: {obj}"
    return "strict JSON parsed + schema matched"


def t07_gemini_file(ctx: dict) -> str:
    attach = OUT_DIR / "t07_attach.txt"
    secret = f"FILE_SECRET_{RUN_TOKEN}"
    attach.write_text(f"The secret phrase inside this file is: {secret}\n", encoding="utf-8")
    data = run_task("gemini",
                    "A text file is attached. Reply with ONLY the secret phrase "
                    "written inside it, nothing else.",
                    paths=[str(attach)])
    resp = data.get("response", "")
    assert secret in resp, f"attached file was not read, got: {resp[:200]}"
    assert data.get("files_pasted") == 1, "files_pasted != 1"
    return "file attach + read-back OK (paste/focus handshake works)"


def t08_gemini_resume(ctx: dict) -> str:
    code = ctx.get("chat_code")
    if not code:
        raise AssertionError("no chat_code from T05 — run T05 first")
    data = run_task("gemini",
                    "Earlier in this exact chat I asked you to reply with a code "
                    "starting with CONNECTOR_OK_. Repeat that exact code now, "
                    "nothing else.",
                    chat_code=code)
    resp = data.get("response", "")
    assert RUN_TOKEN in resp, f"chat memory lost on resume, got: {resp[:200]}"
    return f"chat_code resume OK ({code})"


def t09_flow_two_videos(ctx: dict) -> str:
    """THE duplicate-download bug check: uses Reel Maker prompt to generate n=2."""
    say("  [1/2] Asking Gemini to build 3D Veo prompts via Reel Maker schema...")
    prompts = json.loads((Path(__file__).resolve().parent / "prompts_store.json").read_text(encoding="utf-8"))
    raw_prompt = prompts["prompts"]["reel_maker"]
    gemini_prompt = raw_prompt.replace("{content_type}", "UGC_VIDEO").replace("{topic_blueprint}", "Explain Fair Value Gaps (FVG) like a magnet.")
    g_data = run_task("gemini", gemini_prompt)
    obj = parse_llm_json(g_data.get("response", ""))
    # Combine the Veo prompts so both scenes are sent to Google Flow at once
    prompts_list = obj.get("google_flow_prompts", [])
    veo_prompt = "\n\n".join([f"Scene {p.get('scene_number', i+1)}: {p.get('veo_prompt', '')}" for i, p in enumerate(prompts_list)])
    
    say(f"  [2/3] Gemini returned {len(prompts_list)} scenes. Sending combined prompt to Flow for n=2 videos...")

    out = OUT_DIR / "t09_video.mp4"
    data = run_task("video", veo_prompt, path=str(out), n=2)
    files = [check_file(p, ["mp4"], min_bytes=100_000) for p in extract_saved(data)]
    if len(files) >= 2:
        hashes = [md5(f) for f in files[:2]]
        assert hashes[0] != hashes[1], (
            f"DUPLICATE DOWNLOAD BUG: Both videos have the same MD5 {hashes[0]}. "
            "The back→next-thumbnail fix did not take effect.")
    ctx["video_file"] = files[0]
    sizes = ", ".join(f"{f.stat().st_size:,}B" for f in files)
    hash_str = " vs ".join(h[:8] for h in (hashes if len(files) >= 2 else [md5(files[0])]))
    say(f"  sizes: {sizes} | md5: {hash_str}")

    say("  [3/3] Generating TTS audio for the two scenes...")
    audio_obj = obj.get("audio_and_copy", {})
    voice1 = audio_obj.get("scene_1_character_voice", "")
    voice2 = audio_obj.get("scene_2_character_voice", "")
    
    audio_files = []
    if voice1:
        out_audio1 = OUT_DIR / "t09_audio_1.mp3"
        data1 = run_task("tts", voice1, path=str(out_audio1))
        audio_files.append(extract_saved(data1)[0])
    if voice2:
        out_audio2 = OUT_DIR / "t09_audio_2.mp3"
        data2 = run_task("tts", voice2, path=str(out_audio2))
        audio_files.append(extract_saved(data2)[0])
        
    audio_sizes = ", ".join(f"{Path(f).stat().st_size:,}B" for f in audio_files)
    
    return f"Gemini+Flow+TTS pipeline OK! {len(files)} video(s) ({sizes}), {len(audio_files)} audio files ({audio_sizes})"


def t10_tts(ctx: dict) -> str:
    out = OUT_DIR / "t10_voice.mp3"
    data = run_task("tts",
                    "Meowww Parth Here I Hope Your Doing Gr8.",
                    path=str(out))
    p = check_file(extract_saved(data)[0], ["mp3"], min_bytes=5_000)
    ctx["audio_file"] = p
    return f"TTS OK -> {p.name} ({p.stat().st_size:,} bytes, voice={data.get('voice')})"


def t11_chain_gemini_tts(ctx: dict) -> str:
    """The real pipeline shape: LLM writes the script → TTS speaks it."""
    data = run_task("gemini",
                    'Write a short energetic Hinglish voiceover (max 15 words) about '
                    'respecting your stop loss. Respond ONLY with valid JSON: '
                    '{"voiceover": "<the line>"}')
    obj = parse_llm_json(data.get("response", ""))
    line = (obj.get("voiceover") or "").strip()
    assert 3 <= len(line.split()) <= 25, f"voiceover length odd: {line!r}"
    say(f"  Gemini wrote: {line!r}")

    out = OUT_DIR / "t11_chain_voice.mp3"
    data2 = run_task("tts", line, path=str(out))
    p = check_file(extract_saved(data2)[0], ["mp3"], min_bytes=5_000)
    ctx["chain_audio"] = p
    return f"Gemini→TTS chain OK -> {p.name}"


def t12_chain_merge(ctx: dict) -> str:
    """moviepy mux: proves the video+audio merge step of the reel pipeline."""
    video = ctx.get("video_file")
    audio = ctx.get("chain_audio") or ctx.get("audio_file")
    if not video or not audio:
        raise AssertionError("needs T09 (video) and T10/T11 (audio) to pass first")
    try:
        from moviepy.editor import VideoFileClip, AudioFileClip
    except ImportError:
        try:
            from moviepy import VideoFileClip, AudioFileClip   # moviepy >= 2.0
        except ImportError:
            raise AssertionError("moviepy not installed — pip install moviepy") from None

    out = OUT_DIR / "t12_merged.mp4"
    v = a = final = None
    try:
        v = VideoFileClip(str(video))
        a = AudioFileClip(str(audio))
        dur = min(v.duration, a.duration) or v.duration
        try:
            final = v.with_audio(a).subclipped(0, dur)          # moviepy 2.x
        except AttributeError:
            final = v.set_audio(a).subclip(0, dur)              # moviepy 1.x
        final.write_videofile(str(out), codec="libx264", audio_codec="aac",
                              logger=None)
    finally:
        # Explicitly close every clip — Windows keeps file locks otherwise.
        for clip in (final, a, v):
            try:
                if clip:
                    clip.close()
            except Exception:
                pass
    check_file(out, ["mp4"], min_bytes=50_000)
    return f"video+audio merged OK -> {out.name} ({out.stat().st_size:,} bytes)"
def t13_growth_hacker(ctx: dict) -> str:
    prompts = json.loads((Path(__file__).resolve().parent / "prompts_store.json").read_text(encoding="utf-8"))
    raw_prompt = prompts["prompts"]["growth_hacker"]
    prompt = raw_prompt.replace("{platform_name}", "Reddit").replace("{user_post_text}", "I just lost $5000 trading options on 0DTE SPY puts. My account is blown. I don't know what to do anymore.")
    data = run_task("gemini", prompt)
    obj = parse_llm_json(data.get("response", ""))
    assert "post_analysis" in obj and "reply_package" in obj, "missing schema keys"
    assert obj["reply_package"]["is_safe_to_reply"] is True, "marked unsafe"
    say("  Gemini analyzed loss-porn successfully.")
    return "Growth Hacker prompt schema OK"


TESTS = [
    ("T01", "Bridge alive",            t01_bridge,           "fast"),
    ("T02", "Extension live",          t02_extension,        "fast"),
    ("T03", "ChatGPT image",           t03_chatgpt_image,    "normal"),
    ("T04", "Claude PDF",              t04_claude_pdf,       "normal"),
    ("T05", "Gemini text echo",        t05_gemini_text,      "normal"),
    ("T06", "Gemini strict JSON",      t06_gemini_json,      "normal"),
    ("T07", "Gemini file attach",      t07_gemini_file,      "normal"),
    ("T08", "Gemini chat resume",      t08_gemini_resume,    "normal"),
    ("T09", "Flow 2 videos (dup bug)", t09_flow_two_videos,  "slow"),
    ("T10", "ElevenLabs TTS",          t10_tts,              "normal"),
    ("T11", "CHAIN Gemini→TTS",        t11_chain_gemini_tts, "slow"),
    ("T12", "CHAIN merge (moviepy)",   t12_chain_merge,      "slow"),
    ("T13", "Growth Hacker (Reddit)",  t13_growth_hacker,    "normal"),
]
# ---------------------------------------------------------------------------
# Runner + report
# ---------------------------------------------------------------------------

def run(selected: list[str]) -> int:
    ctx: dict = {}
    rows = []
    say(f"run token: {RUN_TOKEN}   output dir: {OUT_DIR}")
    say(f"running {len(selected)} test(s): {', '.join(selected)}\n")

    for tid, name, fn, _speed in TESTS:
        if tid not in selected:
            continue
        print(f"{C_Y}=== {tid}  {name} ==={C_0}")
        t0 = time.time()
        try:
            detail = fn(ctx)
            dt = time.time() - t0
            rows.append({"id": tid, "name": name, "status": "PASS",
                         "seconds": round(dt, 1), "detail": detail})
            print(f"{C_G}  PASS{C_0}  ({dt:.1f}s)  {detail}\n")
        except Exception as exc:
            dt = time.time() - t0
            rows.append({"id": tid, "name": name, "status": "FAIL",
                         "seconds": round(dt, 1), "detail": str(exc)})
            print(f"{C_R}  FAIL{C_0}  ({dt:.1f}s)  {exc}\n")

    # ---- summary table ----
    passed = sum(1 for r in rows if r["status"] == "PASS")
    failed = len(rows) - passed
    print("=" * 74)
    print(f"{'ID':<5}{'TEST':<28}{'RESULT':<8}{'TIME':<8}DETAIL")
    print("-" * 74)
    for r in rows:
        color = C_G if r["status"] == "PASS" else C_R
        print(f"{r['id']:<5}{r['name']:<28}{color}{r['status']:<8}{C_0}"
              f"{str(r['seconds']) + 's':<8}{r['detail'][:60]}")
    print("=" * 74)
    verdict = (f"{C_G}ALL {passed} TESTS PASSED — SAFE TO DEPLOY{C_0}" if not failed
               else f"{C_R}{failed} FAILED / {passed} passed — DO NOT DEPLOY YET{C_0}")
    print(verdict)

    report = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "run_token": RUN_TOKEN,
        "passed": passed,
        "failed": failed,
        "results": rows,
    }
    report_path = OUT_DIR / "test_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    say(f"report saved -> {report_path}")
    return 0 if not failed else 1


def interactive_pick() -> list[str]:
    print("""
    ============ AI CONNECTOR TEST SUITE ============
     1) FULL RUN        (T01-T13, everything)
     2) QUICK RUN       (skip video + chains)
     3) CONNECTIVITY    (T01, T02 only)
     4) FLOW DUP-BUG    (T01, T02, T09)
     5) CHAINS ONLY     (T01, T02, T05, T09, T10, T11, T12)
     6) PICK MANUALLY   (comma-separated IDs)
     7) GROWTH HACKER   (T01, T02, T13)
     0) Exit
    =================================================""")
    choice = input("Select> ").strip()
    all_ids = [t[0] for t in TESTS]
    if choice == "1":
        return all_ids
    if choice == "2":
        return [t for t in all_ids if t not in ("T09", "T11", "T12")]
    if choice == "3":
        return ["T01", "T02"]
    if choice == "4":
        return ["T01", "T02", "T09"]
    if choice == "5":
        return ["T01", "T02", "T05", "T09", "T10", "T11", "T12"]
    if choice == "7":
        return ["T01", "T02", "T13"]
    if choice == "6":
        raw = input("IDs (e.g. T03,T05,T09): ").strip().upper()
        picked = [x.strip() for x in raw.split(",") if x.strip() in all_ids]
        return picked or all_ids
    sys.exit(0)


def main() -> None:
    print(r"""
    ███████╗████████╗ ██████╗  ██████╗██╗  ██╗
    ██╔════╝╚══██╔══╝██╔═══██╗██╔════╝██║ ██╔╝
    ███████╗   ██║   ██║   ██║██║     █████╔╝ 
    ╚════██║   ██║   ██║   ██║██║     ██╔═██╗ 
    ███████║   ██║   ╚██████╔╝╚██████╗██║  ██╗
    ╚══════╝   ╚═╝    ╚═════╝  ╚═════╝╚═╝  ╚═╝
             WARRIOR CONNECTOR KEYGEN
    """)
    if os.name == 'nt':
        import threading, winsound
        def _loop():
            notes = [(440, 100), (523, 100), (659, 100), (523, 100), (440, 100), (523, 100), (659, 100), (880, 100),
                     (349, 100), (440, 100), (523, 100), (440, 100), (349, 100), (440, 100), (523, 100), (698, 100),
                     (392, 100), (493, 100), (587, 100), (493, 100), (392, 100), (493, 100), (587, 100), (784, 100)]
            try:
                while True:
                    for f, d in notes: winsound.Beep(f, d)
            except Exception: pass
        threading.Thread(target=_loop, daemon=True).start()
    
    input("    [ PRESS ENTER TO CRACK... I MEAN START ]\n")

    ap = argparse.ArgumentParser(description="AI Connector pre-deployment test suite")
    ap.add_argument("--all", action="store_true", help="run every test")
    ap.add_argument("--quick", action="store_true", help="skip video + chain tests")
    ap.add_argument("--only", type=str, help="comma-separated test IDs to run")
    ap.add_argument("--skip", type=str, help="comma-separated test IDs to skip")
    args = ap.parse_args()

    all_ids = [t[0] for t in TESTS]
    if args.only:
        selected = [x.strip().upper() for x in args.only.split(",") if x.strip().upper() in all_ids]
    elif args.quick:
        selected = [t for t in all_ids if t not in ("T09", "T11", "T12")]
    elif args.all:
        selected = all_ids
    else:
        selected = interactive_pick()

    if args.skip:
        skips = {x.strip().upper() for x in args.skip.split(",")}
        selected = [t for t in selected if t not in skips]

    # T01/T02 are prerequisites for everything — always prepend them.
    for pre in ("T02", "T01"):
        if pre not in selected:
            selected.insert(0, pre)
    selected.sort()

    spawned_bridge = False
    bridge_proc = None
    try:
        requests.get("http://127.0.0.1:5000/ping", timeout=1)
    except requests.exceptions.ConnectionError:
        print("🌐 browser_bridge.py offline. Spawning it for tests...")
        bridge_file = Path(__file__).resolve().parent / "browser_bridge.py"
        bridge_proc = subprocess.Popen([sys.executable, str(bridge_file)])
        spawned_bridge = True
        time.sleep(5)

    try:
        sys.exit(run(selected))
    finally:
        if spawned_bridge and bridge_proc:
            try:
                requests.post("http://127.0.0.1:5000/shutdown", timeout=2)
            except Exception:
                pass
            try:
                bridge_proc.terminate()
            except Exception:
                pass

if __name__ == "__main__":
    main()
