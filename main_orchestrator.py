#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STOCK WARRIOR — main_orchestrator.py  (the daemon)
===================================================
The autonomous brain. Runs forever and:

  • Brand CEO strategic review every 48 h  (Gemini connector)
  • Content Manager 7-day master calendar  (Gemini connector)
  • Executes scheduled tasks at their IST time:
        REEL_MAKER   → Gemini script → Flow/Veo videos → ElevenLabs voice → moviepy mux
        POST_MAKER   → Gemini banner JSON → ChatGPT image (single or carousel loop)
        PDF_MAKER    → Gemini markdown → Claude PDF
        TELEGRAM_POLL→ Gemini Telemanager → channel poll
  • News runs at settings.news_schedule slots (urgent → immediate post)
  • Growth Hacker Reddit/X scans every settings.growth_scan_hours
  • 30-day Prompt Evolution (manual approval gate)
  • Lifesaver Auto-Healer: selector failures → Gemini diagnosis → Telegram alert
  • Every piece of content goes through a Telegram APPROVAL CARD
    (telegram_bot.py owns the card + 1-minute auto-post timeout; we talk
    through the shared SQLite db `warrior.db`)

CONNECTOR-FIRST, ALWAYS: every LLM / image / video / voice / PDF call goes
through the browser bridge (localhost:5000). No AI API keys anywhere.
The ONLY APIs used are for PUBLISHING (Telegram / Meta Graph v25.0 / YouTube).

Run:   python main_orchestrator.py            (starts telegram_bot.py too)
       python main_orchestrator.py --no-bot   (orchestrator only)
       python main_orchestrator.py --once TASKTYPE "topic"   (fire one task now)
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import html
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # Python < 3.9
    ZoneInfo = None

import requests

# ---------------------------------------------------------------------------
# Paths / env / store
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STORE_PATH = BASE_DIR / "prompts_store.json"
DB_PATH = BASE_DIR / "warrior.db"
OUT_DIR = BASE_DIR / "output"
MUSIC_DIR = BASE_DIR / "music"
LOG_PATH = BASE_DIR / "orchestrator.log"
BRIDGE = "http://127.0.0.1:5000"

for d in (OUT_DIR, MUSIC_DIR):
    d.mkdir(exist_ok=True)


def load_env(path: Path = BASE_DIR / ".env") -> dict:
    """Tiny .env parser — no python-dotenv dependency."""
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    # IMGBB_/PEXELS_/IG_ were missing from this list, so setting them as real OS
    # environment variables silently did nothing.
    _prefixes = ("TELEGRAM_", "META_", "YT_", "REDDIT_", "TWITTER_",
                 "IMGBB_", "PEXELS_", "IG_")
    env.update({k: v for k, v in os.environ.items() if k.startswith(_prefixes)})
    return env


ENV = load_env()

TG_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
TG_ADMIN = ENV.get("TELEGRAM_ADMIN_ID", "")            # your personal chat id
TG_CHANNEL = ENV.get("TELEGRAM_CHANNEL_ID", "")        # @channel or -100…
META_TOKEN = ENV.get("META_ACCESS_TOKEN", "")
META_IG_ID = ENV.get("META_IG_ACCOUNT_ID", "")
IMGBB_API_KEY = ENV.get("IMGBB_API_KEY", "")
PEXELS_API_KEY = ENV.get("PEXELS_API_KEY", "")
GRAPH = "https://graph.facebook.com/v25.0"


def store() -> dict:
    """Hot-reload the prompt store on every read so Telegram edits apply live."""
    return json.loads(STORE_PATH.read_text(encoding="utf-8"))


def save_store(data: dict) -> None:
    STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def now_ist() -> datetime:
    tz = store()["settings"].get("timezone", "Asia/Kolkata")
    if ZoneInfo:
        return datetime.now(ZoneInfo(tz))
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _esc(text) -> str:
    """Escape for Telegram parse_mode=HTML. An unescaped & or < in an AI caption
    makes Telegram reject the whole message with a 400."""
    return html.escape(str(text), quote=False)


def log(msg: str) -> None:
    line = f"[{now_ist().strftime('%d-%b %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SQLite (shared with telegram_bot.py — WAL mode, both processes safe)
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, execution_dt TEXT, agent TEXT, content_type TEXT,
            slides INTEGER DEFAULT 0, blueprint TEXT, urgent INTEGER DEFAULT 0,
            status TEXT DEFAULT 'scheduled', created TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS approvals(
            id TEXT PRIMARY KEY, kind TEXT, caption TEXT, files TEXT,
            payload TEXT, status TEXT DEFAULT 'pending',
            created TEXT, decided TEXT, decided_by TEXT);
        CREATE TABLE IF NOT EXISTS performance(
            id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT, platform TEXT,
            kind TEXT, topic TEXT, ref_id TEXT, metrics TEXT);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS healer(
            id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT, platform TEXT,
            error TEXT, suggestion TEXT, status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS evolution(
            id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT, prompt_name TEXT,
            payload TEXT, status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS botcmd(
            id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT, cmd TEXT,
            arg TEXT, status TEXT DEFAULT 'pending');
        """)
    # added later — existing databases won't have it
    with db() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(botcmd)")}
        if "retried" not in cols:
            c.execute("ALTER TABLE botcmd ADD COLUMN retried INTEGER DEFAULT 0")

    # (crash recovery now lives in recover_state(), called from main())

    # First-boot seeding: without this, every "every N days/hours" loop fires
    # immediately because its last-run timestamp is empty. CEO is allowed to run
    # once for an initial plan; growth + evolution wait for real data first.
    _boot = now_ist()
    if not get_state("last_growth_run"):
        set_state("last_growth_run", _boot.isoformat())
    if not get_state("last_evolution_run"):
        set_state("last_evolution_run", _boot.isoformat())


def get_state(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT INTO state(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------------------
# Bridge client (connector-first — the ONLY way we talk to any AI)
# ---------------------------------------------------------------------------

RESULT_TIMEOUT = {"gemini": 420, "img": 600, "pdf": 600, "video": 900, "tts": 420}


class BridgeError(RuntimeError):
    pass


def bridge_status() -> dict:
    try:
        return requests.get(f"{BRIDGE}/status", timeout=5).json()
    except Exception:
        return {"ok": False, "extension_live": False}


# The Chrome extension refuses a second task while one is running (its `busy`
# flag). Scheduler tasks and panel/Test-Lab tasks now run on different threads,
# so without this lock they would silently trample each other.
BRIDGE_LOCK = threading.RLock()


def bridge_task(task_type: str, prompt: str, path: str | None = None, *,
                n: int = 1, paths: list | None = None, chat_code: str | None = None,
                voice: str | None = None, timeout: int | None = None) -> dict:
    """Queue a task on the bridge and block until its result arrives."""
    with BRIDGE_LOCK:
        return _bridge_task_locked(task_type, prompt, path, n=n, paths=paths,
                                   chat_code=chat_code, voice=voice, timeout=timeout)


def _bridge_task_locked(task_type: str, prompt: str, path: str | None = None, *,
                        n: int = 1, paths: list | None = None, chat_code: str | None = None,
                        voice: str | None = None, timeout: int | None = None) -> dict:
    body = {"type": task_type, "prompt": prompt, "path": path, "n": n,
            "paths": paths or [], "chat_code": chat_code,
            "voice": voice or store()["brand"].get("voice_name", "banty")}
    r = requests.post(f"{BRIDGE}/queue-task", json=body, timeout=90)
    j = r.json()
    if not j.get("ok"):
        raise BridgeError(f"queue-task rejected: {j.get('error')}")
    tid = j["id"]
    deadline = time.time() + (timeout or RESULT_TIMEOUT.get(task_type, 600))
    while time.time() < deadline:
        jr = requests.get(f"{BRIDGE}/result/{tid}", timeout=30).json()
        if jr.get("done"):
            res = jr["result"]
            if not res.get("ok"):
                raise BridgeError(res.get("error") or "task failed")
            return res.get("data") or {}
        time.sleep(3)
    raise BridgeError(f"{task_type} task {tid} timed out")


def parse_llm_json(text: str) -> dict:
    """Strip markdown fences / stray prose and parse the JSON object."""
    text = (text or "").strip()
    # Log raw text on failure to diagnose UI changes or AI refusals
    raw_text = text
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            log(f"CRITICAL PARSE ERROR. Raw text received from bridge was: {repr(raw_text)}")
            raise
        return json.loads(m.group(0))


def ask_gemini(prompt: str, *, schema_key: str | None = None,
               chat_code: str | None = None, retries: int = 2) -> dict:
    """Gemini connector call → parsed + schema-checked JSON.
    THE lifeblood of the engine — every agent thinks through this."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            data = bridge_task("gemini", prompt, chat_code=chat_code)
            parsed = parse_llm_json(
                data.get("response") or data.get("text") or "")
            if schema_key:
                required = store()["schema_map"].get(schema_key, [])
                missing = [k for k in required if k not in parsed]
                if missing:
                    raise ValueError(f"schema {schema_key} missing keys: {missing}")
            parsed["_chat_code"] = data.get("chat_code")
            return parsed
        except BridgeError as exc:
            last_exc = exc
            on_bridge_failure("gemini", str(exc))
            time.sleep(10)
        except (json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            log(f"  [gemini] bad JSON (attempt {attempt + 1}): {exc}")
            prompt = (prompt + "\n\nREMINDER: your previous answer was not valid "
                      "parseable JSON matching the schema. Output ONLY the JSON object.")
    raise BridgeError(f"Gemini failed after retries: {last_exc}")


def render_prompt(name: str, **vars_) -> str:
    st = store()
    tmpl = st["prompts"][name]
    # {schema} is injected automatically from schema_shapes so a prompt's declared
    # output shape and the code's expectations come from ONE source. Drift between
    # these two is what killed the reel pipeline.
    vars_.setdefault("schema", st.get("schema_shapes", {}).get(name, ""))
    for k, v in vars_.items():
        tmpl = tmpl.replace("{" + k + "}", str(v))
    return tmpl


# ---------------------------------------------------------------------------
# Error decoder — turns a raw exception into a cause a human can act on
# ---------------------------------------------------------------------------

_ERROR_HINTS: list[tuple[str, str, str]] = [
    (r"KeyError: '?(\w+)'?",
     "The AI's JSON is missing a key the code reads directly.",
     "Check schema_shapes for this agent — prompt and code have drifted apart."),
    (r"schema (\w+) missing keys",
     "Gemini answered, but not in the shape this agent requires.",
     "Open the agent's prompt: its <output_schema> must match schema_shapes."),
    (r"Expecting value: line 1 column 1",
     "The bridge returned an EMPTY string instead of JSON.",
     "Usually the Gemini copy button failed, or the extension read the wrong key. "
     "Run Test Lab → Gemini and check the raw payload."),
    (r"queue-task rejected|Connection refused|Max retries|NewConnectionError",
     "browser_bridge.py is not reachable on 127.0.0.1:5000.",
     "Start the bridge, or use Test Lab → Bridge to confirm it's alive."),
    (r"timed out",
     "The connector took longer than its timeout.",
     "Usually Chrome lost focus, a login expired, or the site is slow. "
     "Check the automation Chrome window."),
    (r"selector|element|not found|click",
     "A DOM selector no longer matches the site.",
     "The site's UI changed. Check /healer for the suggested replacement selectors."),
    (r"nonexisting field",
     "The code asked Graph for a field that doesn't exist on this node.",
     "This is a bug in the request, NOT a permissions problem — the field name is wrong."),
    (r"upload host|could not host|serves an HTML page|truncated",
     "The video could not be given a public URL for Meta to download.",
     "Image posts use ImgBB and work; video uses an anonymous file host that is "
     "rate-limited. Set IMGBB_API_KEY, or host video on storage you control."),
    (r"processing ERROR|could not use it|processing never finished",
     "Meta downloaded the file but rejected the video itself.",
     "Reels need MP4/MOV, H.264 + AAC, 9:16, under 90s. Stories cap at 60s. "
     "Re-encode and retry."),
    (r"invalid_grant|refresh token is dead",
     "The YouTube OAuth refresh token is no longer valid.",
     "If the consent screen is in Testing mode, tokens die after 7 days. Publish "
     "the app or re-run make_yt_token.py."),
    (r"yt_token\.json is missing",
     "YouTube has never been authorised on this machine.",
     "Run `python make_yt_token.py` once with client_secret.json present."),
    (r"\(#?100\)|Unsupported post request|does not exist",
     "Meta Graph rejected the request.",
     "Usually the IG account isn't Business, the token expired, or "
     "instagram_content_publish permission is missing."),
    (r"\(#?190\)|OAuthException|Session has expired",
     "The Meta access token is expired or invalid.",
     "Regenerate it and update META_ACCESS_TOKEN via the panel's 🔑 Tokens screen."),
    (r"\(#?4\)|rate limit|too many|429",
     "You hit an API rate limit.",
     "Back off and retry later — this one usually resolves itself."),
    (r"moviepy|ffmpeg|No such file or directory: 'ffmpeg'",
     "Video assembly failed.",
     "moviepy needs ffmpeg. Install it, or turn off settings.news_clip_music."),
    (r"yt-dlp|yt_dlp",
     "The source video could not be downloaded.",
     "The link may be private, region-locked, or yt-dlp needs updating."),
    (r"no such table|database is locked",
     "SQLite problem on warrior.db.",
     "If it's locked, another process holds it — restart the engine."),
]


def explain_error(exc: BaseException | str, *, step: str = "") -> dict:
    """Decode a raw exception into {cause, fix, raw}. Used by every failure path
    so Telegram shows something actionable instead of a bare traceback line."""
    raw = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
    for pattern, cause, fix in _ERROR_HINTS:
        if re.search(pattern, raw, re.I):
            return {"cause": cause, "fix": fix, "raw": raw[:500], "step": step}
    return {"cause": "Unrecognised failure.", "fix": "Check orchestrator.log for the full traceback.",
            "raw": raw[:500], "step": step}


def report_failure(agent: str, exc: BaseException, *, step: str = "", topic: str = "") -> dict:
    """Log + decode + push a readable failure card to the admin."""
    info = explain_error(exc, step=step)
    log(f"❌ {agent}" + (f" @ {step}" if step else "") + f": {info['raw']}")
    tg_admin(
        f"❌ <b>{_esc(agent)} failed</b>" + (f" — step: <code>{_esc(step)}</code>" if step else "")
        + (f"\n📌 {_esc(topic[:120])}" if topic else "")
        + f"\n\n<b>What happened</b>\n{_esc(info['cause'])}"
        + f"\n\n<b>How to fix</b>\n{_esc(info['fix'])}"
        + f"\n\n<b>Raw</b>\n<code>{_esc(info['raw'][:300])}</code>")
    return info


# ---------------------------------------------------------------------------
# Lifesaver Auto-Healer  (heal=False → diagnosis + Telegram alert, no auto-apply)
# ---------------------------------------------------------------------------

_healing = False        # re-entrancy guard: healer must not heal itself


def on_bridge_failure(platform: str, error: str) -> None:
    """Selector-ish failures → ask Gemini for a diagnosis, store the suggestion,
    ping the admin. Auto-apply is OFF unless settings.healer_auto_apply."""
    global _healing
    if not re.search(r"selector|timed? ?out|not found|element|click", error, re.I):
        return
    if _healing:                      # the healer's own call failed -> stop.
        log("  [healer] already healing — not recursing")
        return
    log(f"  [healer] analysing {platform} failure: {error[:120]}")
    _healing = True
    try:
        sel_path = BASE_DIR / "selectors.json"
        current = sel_path.read_text(encoding="utf-8") if sel_path.exists() else "{}"
        prompt = render_prompt("auto_healer", platform=platform,
                               failed_step=error[:300], error_message=error[:500],
                               page_html="(not captured — diagnose from the error and current selectors)",
                               current_selectors=current[:4000])
        result = ask_gemini(prompt, schema_key="auto_healer", retries=0)
        with db() as c:
            c.execute("INSERT INTO healer(dt,platform,error,suggestion) VALUES(?,?,?,?)",
                      (now_ist().isoformat(), platform, error[:500],
                       json.dumps(result, ensure_ascii=False)))
        tg_admin(f"🛠 <b>Lifesaver Auto-Healer</b>\nPlatform: {platform}\n"
                 f"Diagnosis: {result.get('diagnosis')}\n"
                 f"Suggested selectors: <code>{result.get('replacement_selectors')}</code>\n"
                 f"Confidence: {result.get('confidence')}\n"
                 f"Review with /healer in the panel.")
    except Exception as exc:
        log(f"  [healer] could not analyse: {exc}")
    finally:
        _healing = False


# ---------------------------------------------------------------------------
# Telegram raw API (publishing + admin alerts — sync requests, no aiogram here)
# ---------------------------------------------------------------------------

def _tg(method: str, data: dict | None = None, files: dict | None = None,
        _retries: int = 3) -> dict:
    """Telegram API call with retry, flood-wait handling and a parse-mode fallback.

    Three failure modes used to lose messages silently:
      * 429 flood control  — we now honour retry_after and try again
      * transient 5xx      — exponential backoff
      * 400 "can't parse entities" — an unescaped < or & in an AI caption killed
        the whole message. We retry once as plain text rather than lose it.
    """
    if not TG_TOKEN:
        log("  [tg] no TELEGRAM_BOT_TOKEN — skipped")
        return {}
    data = dict(data or {})
    for attempt in range(_retries):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                              data=data, files=files, timeout=120)
            try:
                j = r.json()
            except ValueError:
                j = {"ok": False, "description": r.text[:200]}
            if j.get("ok"):
                return j
            desc = str(j.get("description", ""))
            if r.status_code == 429 or "Too Many Requests" in desc:
                wait = int((j.get("parameters") or {}).get("retry_after", 3))
                log(f"  [tg] flood control — waiting {wait}s")
                time.sleep(min(wait, 60) + 1)
                continue
            if "can't parse entities" in desc.lower() or "unsupported parse" in desc.lower():
                log("  [tg] parse error — retrying as plain text")
                data.pop("parse_mode", None)
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            log(f"  [tg] {method} failed: {desc[:200]}")
            return j
        except Exception as exc:
            log(f"  [tg] {method} error (attempt {attempt + 1}): {exc}")
            time.sleep(2 * (attempt + 1))
    return {}


def tg_admin(text: str) -> None:
    """Send to the admin, splitting anything over Telegram's 4096-char limit
    instead of letting the API reject the whole message."""
    if not TG_ADMIN:
        return
    for chunk in _split_message(text):
        _tg("sendMessage", {"chat_id": TG_ADMIN, "text": chunk, "parse_mode": "HTML"})


def _split_message(text: str, limit: int = 3900) -> list[str]:
    text = str(text)
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = ""
        # a single monstrous line still has to be cut somewhere
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        buf += line
    if buf:
        chunks.append(buf)
    return chunks


def tg_channel_text(text: str, pin: bool = False) -> None:
    j = _tg("sendMessage", {"chat_id": TG_CHANNEL, "text": text[:4000], "parse_mode": "Markdown"})
    if pin and j.get("result"):
        _tg("pinChatMessage", {"chat_id": TG_CHANNEL, "message_id": j["result"]["message_id"]})


def tg_channel_file(path: str, caption: str, kind: str = "document") -> None:
    method = {"photo": "sendPhoto", "video": "sendVideo",
              "voice": "sendVoice", "document": "sendDocument"}[kind]
    field = {"photo": "photo", "video": "video", "voice": "voice", "document": "document"}[kind]
    with open(path, "rb") as f:
        _tg(method, {"chat_id": TG_CHANNEL, "caption": caption[:1000], "parse_mode": "Markdown"},
            files={field: f})


def tg_channel_poll(question: str, options: list[str]) -> None:
    _tg("sendPoll", {"chat_id": TG_CHANNEL, "question": question[:290],
                     "options": json.dumps(options[:10]), "is_anonymous": True})


# ---------------------------------------------------------------------------
# Publishers (the ONLY API module — POSTING only, never content generation)
# ---------------------------------------------------------------------------

def _verify_public_url(url: str, expect_bytes: int) -> None:
    """Meta downloads the file from this URL on its own servers. If the host is
    slow, rate-limited or hands back an HTML page, Graph reports a vague media
    error and the real cause is invisible. Check it ourselves first."""
    try:
        r = requests.get(url, stream=True, timeout=60)
    except Exception as exc:
        raise PublishError(f"The upload host is unreachable at {url} ({exc}). "
                           "Meta cannot fetch the file either.") from exc
    ctype = r.headers.get("Content-Type", "")
    clen = int(r.headers.get("Content-Length") or 0)
    r.close()
    if r.status_code != 200:
        raise PublishError(f"The upload host returned HTTP {r.status_code} for {url}. "
                           "Meta cannot fetch the file.")
    if "text/html" in ctype:
        raise PublishError(f"{url} serves an HTML page, not the file itself. "
                           "The host changed its download URL format.")
    if clen and expect_bytes and clen < expect_bytes * 0.5:
        raise PublishError(f"{url} serves only {clen} bytes but the file is "
                           f"{expect_bytes}. The upload was truncated.")


def _host_file(path: str) -> str:
    """Meta Graph needs a PUBLIC url it can download from.

    Images go to ImgBB when a key exists. Video has no ImgBB equivalent, and a
    single anonymous host is a single point of failure — which is exactly why
    image posts succeed while Reels and Stories fail. Try several, and verify
    the result is really fetchable before handing it to Meta.
    """
    p = Path(path)
    if not p.exists():
        raise PublishError(f"Nothing to upload — {path} does not exist.")
    size = p.stat().st_size
    ext = p.suffix.lower().lstrip(".")
    is_image = ext in ("png", "jpg", "jpeg", "webp", "gif")

    if is_image and IMGBB_API_KEY:
        with open(path, "rb") as f:
            r = requests.post("https://api.imgbb.com/1/upload",
                              data={"key": IMGBB_API_KEY}, files={"image": f}, timeout=180)
        if r.status_code == 200:
            return r.json()["data"]["url"]
        log(f"  [host] ImgBB failed ({r.status_code}) — falling back")

    errors = []

    def _tmpfiles():
        with open(path, "rb") as f:
            r = requests.post("https://tmpfiles.org/api/v1/upload",
                              files={"file": f}, timeout=300)
        r.raise_for_status()
        return r.json()["data"]["url"].replace("tmpfiles.org/", "tmpfiles.org/dl/")

    def _catbox():
        with open(path, "rb") as f:
            r = requests.post("https://catbox.moe/user/api.php",
                              data={"reqtype": "fileupload"},
                              files={"fileToUpload": f}, timeout=300)
        r.raise_for_status()
        url = r.text.strip()
        if not url.startswith("http"):
            raise RuntimeError(url[:120])
        return url

    def _zerox():
        with open(path, "rb") as f:
            r = requests.post("https://0x0.st", files={"file": f},
                              headers={"User-Agent": "StockWarrior/1.0"}, timeout=300)
        r.raise_for_status()
        url = r.text.strip()
        if not url.startswith("http"):
            raise RuntimeError(url[:120])
        return url

    for name, fn in (("catbox.moe", _catbox), ("tmpfiles.org", _tmpfiles), ("0x0.st", _zerox)):
        try:
            url = fn()
            _verify_public_url(url, size)
            log(f"  [host] {p.name} ({size // 1024} KB) → {name}")
            return url
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:120]}")
            log(f"  [host] {name} failed — trying the next host")

    raise PublishError(
        "Could not host the file anywhere, so Meta has no URL to download from.\n"
        + "\n".join(errors)
        + "\nAdd IMGBB_API_KEY for images, or host video on your own storage.")


_IG_PRIVATE = None


def ig_private_client():
    """instagrapi client — uploads the file DIRECTLY to Instagram.

    This is the real answer to the video problem. The Graph API never receives a
    file: it downloads media from a public URL you supply. ImgBB hosts images
    only, and no free anonymous host is dependable for video, so Reels and
    Stories fail while image posts succeed.

    instagrapi talks to Instagram's private mobile API and POSTs the bytes, so
    no hosting is involved at all.

    Trade-off, stated plainly: it is unofficial. It needs your IG username and
    password, and Instagram may issue a login challenge or flag the account.
    That is why it is a FALLBACK — Graph is tried first, always.
    """
    global _IG_PRIVATE
    if _IG_PRIVATE is not None:
        return _IG_PRIVATE
    user, pwd = ENV.get("IG_USERNAME"), ENV.get("IG_PASSWORD")
    if not (user and pwd):
        return None
    try:
        from instagrapi import Client
    except ImportError:
        log("  [ig-direct] instagrapi not installed — `pip install instagrapi`")
        return None
    cl = Client()
    session = BASE_DIR / "ig_session.json"
    try:
        if session.exists():
            # Reusing the session avoids a fresh login every run, which is what
            # triggers Instagram's challenges.
            cl.load_settings(str(session))
            cl.login(user, pwd)
            cl.get_timeline_feed()                 # cheap call to prove it's alive
        else:
            cl.login(user, pwd)
            cl.dump_settings(str(session))
        log(f"  [ig-direct] logged in as @{user}")
        _IG_PRIVATE = cl
        return cl
    except Exception as exc:
        log(f"  [ig-direct] login failed: {exc}")
        session.unlink(missing_ok=True)            # a stale session is worse than none
        return None


def _ig_direct_upload(path: str, caption: str, kind: str) -> str | None:
    """kind: reel | story | photo. Returns a media id, or None if unavailable."""
    if not store()["settings"].get("instagrapi_fallback", True):
        return None
    cl = ig_private_client()
    if cl is None:
        return None
    try:
        p = Path(path)
        log(f"  [ig-direct] uploading {p.name} as {kind} (no hosting needed)")
        if kind == "reel":
            media = cl.clip_upload(p, caption[:2100])
        elif kind == "story":
            media = (cl.video_upload_to_story(p)
                     if p.suffix.lower() in (".mp4", ".mov")
                     else cl.photo_upload_to_story(p))
        else:
            media = cl.photo_upload(p, caption[:2100])
        mid = str(getattr(media, "pk", "") or getattr(media, "id", ""))
        log(f"  [ig-direct] ✅ uploaded ({mid})")
        return mid
    except Exception as exc:
        log(f"  [ig-direct] upload failed: {exc}")
        return None


def publish_instagram_image(path: str, caption: str) -> str | None:
    if not (META_TOKEN and META_IG_ID):
        log("  [meta] not configured — IG image skipped")
        return None
    try:
        url = _host_file(path)
        r1 = requests.post(f"{GRAPH}/{META_IG_ID}/media",
                           data={"image_url": url, "caption": caption[:2100],
                                 "access_token": META_TOKEN}, timeout=120)
        if r1.status_code != 200:
            raise PublishError(f"IG image create failed: {_graph_error(r1)}")
        cid = r1.json()["id"]
        time.sleep(8)
        r2 = requests.post(f"{GRAPH}/{META_IG_ID}/media_publish",
                           data={"creation_id": cid, "access_token": META_TOKEN}, timeout=120)
        if r2.status_code != 200:
            raise PublishError(f"IG image publish failed: {_graph_error(r2)}")
        return r2.json().get("id")
    except PublishError:
        if (mid := _ig_direct_upload(path, caption, "photo")):
            return mid
        raise


def publish_instagram_carousel(paths: list[str], caption: str) -> str | None:
    if not (META_TOKEN and META_IG_ID):
        log("  [meta] not configured — IG carousel skipped")
        return None
    children, failed = [], []
    for p in paths:
        url = _host_file(p)
        r = requests.post(f"{GRAPH}/{META_IG_ID}/media",
                          data={"image_url": url, "is_carousel_item": "true",
                                "access_token": META_TOKEN}, timeout=120)
        if r.status_code == 200:
            children.append(r.json()["id"])
        else:
            # Silently dropping slides used to turn a 5-slide carousel into a
            # 2-slide one with no warning at all.
            failed.append(f"{Path(p).name}: {_graph_error(r)}")
    if failed:
        log(f"  [meta] {len(failed)} carousel slide(s) rejected: {failed[0][:160]}")
    if len(children) < 2:
        raise PublishError("A carousel needs at least 2 slides and only "
                           f"{len(children)} uploaded. " + " | ".join(failed[:2]))
    r1 = requests.post(f"{GRAPH}/{META_IG_ID}/media",
                       data={"media_type": "CAROUSEL", "children": ",".join(children),
                             "caption": caption[:2100], "access_token": META_TOKEN}, timeout=120)
    if r1.status_code != 200:
        raise PublishError(f"IG carousel create failed: {_graph_error(r1)}")
    time.sleep(8)
    r2 = requests.post(f"{GRAPH}/{META_IG_ID}/media_publish",
                       data={"creation_id": r1.json()["id"], "access_token": META_TOKEN},
                       timeout=120)
    if r2.status_code != 200:
        raise PublishError(f"IG carousel publish failed: {_graph_error(r2)}")
    return r2.json().get("id")


def publish_instagram_reel(path: str, caption: str) -> str | None:
    if not (META_TOKEN and META_IG_ID):
        log("  [meta] not configured — IG reel skipped")
        return None
    try:
        return _publish_reel_graph(path, caption)
    except PublishError as exc:
        log(f"  [meta] Graph reel failed ({str(exc)[:120]}) — trying direct upload")
        if (mid := _ig_direct_upload(path, caption, "reel")):
            return mid
        raise


def _publish_reel_graph(path: str, caption: str) -> str | None:
    url = _host_file(path)
    cid = None
    for attempt in range(3):                                       # transient 5xx retry
        r1 = requests.post(f"{GRAPH}/{META_IG_ID}/media",
                           data={"media_type": "REELS", "video_url": url,
                                 "caption": caption[:2100], "access_token": META_TOKEN},
                           timeout=120)
        if r1.status_code == 200:
            cid = r1.json()["id"]
            break
        if r1.status_code >= 500:
            log(f"  [meta] 5xx on reel create (attempt {attempt + 1}) — retrying")
            time.sleep(5)
            continue
        raise PublishError(f"IG reel create failed: {_graph_error(r1)} (video_url was {url})")
    if not cid:
        raise PublishError("IG reel create failed after 3 attempts (Meta kept returning 5xx)")
    for _ in range(18):                                            # wait for processing
        time.sleep(10)
        j = requests.get(f"{GRAPH}/{cid}",
                         params={"fields": "status_code,status", "access_token": META_TOKEN},
                         timeout=60).json()
        if j.get("status_code") == "FINISHED":
            break
        if j.get("status_code") == "ERROR":
            raise PublishError("IG reel processing ERROR — Meta downloaded the file but "
                               f"could not use it. {j.get('status', '')} (url: {url})")
    else:
        raise PublishError("IG reel processing never finished (3 min timeout)")
    r2 = requests.post(f"{GRAPH}/{META_IG_ID}/media_publish",
                       data={"creation_id": cid, "access_token": META_TOKEN}, timeout=120)
    if r2.status_code != 200:
        raise PublishError(f"IG reel publish failed: {_graph_error(r2)}")
    return r2.json().get("id")


def publish_youtube_short(path: str, title: str, description: str) -> str | None:
    """Needs yt_token.json (OAuth user creds) + google-api-python-client."""
    token = BASE_DIR / "yt_token.json"
    if not token.exists():
        raise PublishError(
            "yt_token.json is missing. Run `python make_yt_token.py` once — it needs "
            "client_secret.json downloaded from Google Cloud Console "
            "(OAuth client, type: Desktop app, YouTube Data API v3 enabled).")
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GARequest
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise PublishError(
            "google-api-python-client / google-auth-oauthlib are not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib") from exc
    try:
        creds = Credentials.from_authorized_user_info(
            json.loads(token.read_text()),
            ["https://www.googleapis.com/auth/youtube.upload"])
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(GARequest())
            token.write_text(creds.to_json())
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {"snippet": {"title": title[:95], "description": description[:4900],
                            "tags": ["StockWarrior", "Trading", "Shorts"], "categoryId": "22"},
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
        media = MediaFileUpload(path, chunksize=1024 * 1024, resumable=True, mimetype="video/*")
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp.get("id")
    except PublishError:
        raise
    except Exception as exc:
        detail = str(exc)
        if "invalid_grant" in detail or "Token has been expired" in detail:
            raise PublishError(
                "The YouTube refresh token is dead (revoked, or the OAuth consent "
                "screen is still in Testing mode — those tokens expire after 7 days). "
                "Delete yt_token.json and run `python make_yt_token.py` again.") from exc
        if "quotaExceeded" in detail or "uploadLimitExceeded" in detail:
            raise PublishError("YouTube daily upload quota is exhausted. The default "
                               "API quota allows only a handful of uploads per day.") from exc
        raise PublishError(f"YouTube upload failed: {detail[:400]}") from exc


def publish_instagram_story(path: str, *, is_video: bool | None = None) -> str | None:
    """IG Stories via Graph API. Requires an Instagram BUSINESS account with
    instagram_content_publish. Images and video both supported."""
    if not (META_TOKEN and META_IG_ID):
        log("  [meta] not configured — IG story skipped")
        return None
    if is_video is None:
        is_video = path.lower().endswith((".mp4", ".mov"))
    try:
        return _publish_story_graph(path, is_video)
    except PublishError as exc:
        log(f"  [meta] Graph story failed ({str(exc)[:120]}) — trying direct upload")
        if (mid := _ig_direct_upload(path, "", "story")):
            return mid
        raise


def _publish_story_graph(path: str, is_video: bool) -> str | None:
    url = _host_file(path)
    data = {"media_type": "STORIES", "access_token": META_TOKEN}
    data["video_url" if is_video else "image_url"] = url
    r1 = requests.post(f"{GRAPH}/{META_IG_ID}/media", data=data, timeout=180)
    if r1.status_code != 200:
        raise PublishError(f"IG story create failed: {_graph_error(r1)} "
                           f"(media_url was {url})")
    cid = r1.json()["id"]
    if is_video:                                    # video stories need processing
        for _ in range(18):
            time.sleep(10)
            j = requests.get(f"{GRAPH}/{cid}",
                             params={"fields": "status_code,status",
                                     "access_token": META_TOKEN}, timeout=60).json()
            code = j.get("status_code")
            if code == "FINISHED":
                break
            if code == "ERROR":
                raise PublishError("IG story processing ERROR — Meta could not use the "
                                   f"video. {j.get('status', '')} (url: {url})")
        else:
            raise PublishError("IG story processing never finished (3 min timeout)")
    r2 = requests.post(f"{GRAPH}/{META_IG_ID}/media_publish",
                       data={"creation_id": cid, "access_token": META_TOKEN}, timeout=120)
    if r2.status_code != 200:
        raise PublishError(f"IG story publish failed: {_graph_error(r2)}")
    log("  [meta] ✅ story published")
    return r2.json().get("id")


def twitter_ready() -> tuple[bool, str]:
    """TWITTER_BEARER_TOKEN is app-only auth and CANNOT post — it is read-only.
    Posting needs OAuth 1.0a user context. Report exactly what's missing."""
    need = ["TWITTER_API_KEY", "TWITTER_API_SECRET",
            "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"]
    missing = [k for k in need if not ENV.get(k)]
    if missing:
        return False, ("Twitter posting needs OAuth 1.0a keys — missing: "
                       + ", ".join(missing)
                       + ". (A bearer token alone is read-only and cannot post.)")
    return True, ""


def publish_twitter(text: str, media_paths: list[str] | None = None) -> str | None:
    """Post to X/Twitter. BUILT BUT DARK: stays inert until the four OAuth 1.0a
    keys are in .env and settings.twitter_enabled is on. Wiring, routing and the
    Test Lab button are all live so switching it on is only a credentials step."""
    if not store()["settings"].get("twitter_enabled"):
        log("  [twitter] settings.twitter_enabled is off — skipped")
        return None
    ready, why = twitter_ready()
    if not ready:
        log(f"  [twitter] {why}")
        return None
    try:
        from requests_oauthlib import OAuth1
    except ImportError:
        log("  [twitter] requests_oauthlib not installed — skipped")
        return None
    auth = OAuth1(ENV["TWITTER_API_KEY"], ENV["TWITTER_API_SECRET"],
                  ENV["TWITTER_ACCESS_TOKEN"], ENV["TWITTER_ACCESS_SECRET"])
    media_ids: list[str] = []
    # A tweet carries at most 4 images, so a longer carousel is truncated.
    for mp in (media_paths or [])[:4]:
        try:
            with open(mp, "rb") as f:
                up = requests.post("https://upload.twitter.com/1.1/media/upload.json",
                                   auth=auth, files={"media": f}, timeout=180)
            if up.status_code in (200, 201):
                media_ids.append(str(up.json()["media_id_string"]))
            else:
                log(f"  [twitter] media upload failed: {up.text[:160]}")
        except Exception as exc:
            log(f"  [twitter] media upload error: {exc}")
    payload: dict = {"text": text[:280]}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    r = requests.post("https://api.twitter.com/2/tweets", auth=auth, json=payload, timeout=120)
    if r.status_code not in (200, 201):
        log(f"  [twitter] post failed {r.status_code}: {r.text[:200]}")
        return None
    tid = (r.json().get("data") or {}).get("id")
    log(f"  [twitter] ✅ posted {tid}")
    return tid


def pexels_video(query: str, out_path: str, min_seconds: int = 15) -> str | None:
    """Fetch a vertical stock video from Pexels.

    NOTE: Pexels is a *source* of footage, not a host for your own renders — it
    cannot solve the "Meta needs a public URL" problem. What it IS good for is
    giving the news story clip a moving background instead of a still banner.
    """
    if not PEXELS_API_KEY:
        log("  [pexels] no PEXELS_API_KEY — skipped")
        return None
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": PEXELS_API_KEY},
                         params={"query": query, "orientation": "portrait",
                                 "size": "medium", "per_page": 15}, timeout=60)
        if r.status_code != 200:
            log(f"  [pexels] search failed {r.status_code}: {r.text[:160]}")
            return None
        vids = r.json().get("videos", [])
        # Only clips long enough to cover the story without looping.
        usable = [v for v in vids if (v.get("duration") or 0) >= min_seconds] or vids
        if not usable:
            log(f"  [pexels] nothing found for {query!r}")
            return None
        import random
        chosen = random.choice(usable[:8])
        files = [f for f in chosen.get("video_files", []) if f.get("width")]
        # Prefer a portrait file near 1080 wide; fall back to the largest.
        portrait = [f for f in files if f["height"] > f["width"]]
        pool = portrait or files
        pool.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
        link = pool[0]["link"]
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with requests.get(link, stream=True, timeout=300) as dl:
            dl.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in dl.iter_content(1 << 16):
                    fh.write(chunk)
        size = Path(out_path).stat().st_size
        if size < 10_000:
            log("  [pexels] download looks truncated")
            return None
        log(f"  [pexels] ✅ {chosen.get('duration')}s clip by "
            f"{(chosen.get('user') or {}).get('name', '?')} ({size // 1024} KB)")
        return out_path
    except Exception as exc:
        log(f"  [pexels] failed: {exc}")
        return None


def _pexels_query(topic: str) -> str:
    """Turn a topic blueprint into something Pexels actually has footage of."""
    t = (topic or "").lower()
    for keys, q in ((("crypto", "bitcoin", "btc", "ethereum"), "cryptocurrency trading"),
                    (("rupee", "rbi", "inflation", "economy", "gdp"), "indian economy finance"),
                    (("gold", "silver", "commodity"), "gold bars finance"),
                    (("psychology", "mindset", "discipline", "fear"), "stressed person laptop"),
                    (("bank", "loan", "credit"), "bank building finance")):
        if any(k in t for k in keys):
            return q
    return "stock market trading chart"


def build_news_clip(image_path: str, out_path: str, seconds: int | None = None,
                    topic: str = "") -> str:
    """Banner + music -> a vertical clip for Instagram Stories.

    With PEXELS_API_KEY set, the banner is composited over moving stock footage
    instead of sitting on a black card, which performs far better on Stories.

    Works on BOTH moviepy generations. moviepy 2.x deleted `moviepy.editor` and
    renamed every method (set_duration -> with_duration, resize -> resized,
    volumex -> with_volume_scaled, subclip -> subclipped, set_audio -> with_audio),
    so pinning to either one alone breaks on the other machine.
    """
    s = store()["settings"]
    seconds = seconds or int(s.get("news_clip_seconds", 15))
    try:
        try:                                        # moviepy 1.x
            from moviepy.editor import (AudioFileClip, ColorClip, CompositeVideoClip,
                                        ImageClip, VideoFileClip)
        except ModuleNotFoundError:                 # moviepy 2.x
            from moviepy import (AudioFileClip, ColorClip, CompositeVideoClip,
                                 ImageClip, VideoFileClip)
    except ImportError as exc:
        raise RuntimeError(
            "moviepy is required for the news story clip. `pip install moviepy` "
            "(it also needs ffmpeg on PATH), or turn off settings.news_clip_music."
        ) from exc

    def call(obj, names: tuple[str, ...], *a, **kw):
        """Call whichever of these method names this moviepy actually has."""
        for n in names:
            fn = getattr(obj, n, None)
            if callable(fn):
                return fn(*a, **kw)
        raise RuntimeError(f"moviepy clip has none of {names} — unsupported version")

    W, H = 1080, 1920
    layers, bg_video = [], None

    if s.get("news_clip_stock_bg", True) and PEXELS_API_KEY:
        stock = pexels_video(_pexels_query(topic or image_path),
                             str(Path(out_path).with_name("stock_bg.mp4")), seconds)
        if stock:
            try:
                bg_video = VideoFileClip(stock)
                bg_video = call(bg_video, ("subclipped", "subclip"),
                                0, min(seconds, int(bg_video.duration)))
                bg_video = call(bg_video, ("resized", "resize"), height=H)
                if bg_video.w > W:                  # centre-crop to 9:16
                    x = (bg_video.w - W) / 2
                    bg_video = call(bg_video, ("cropped", "crop"),
                                    x1=x, y1=0, x2=x + W, y2=H)
                bg_video = call(bg_video, ("without_audio", "without_audio"))
                layers.append(bg_video)
            except Exception as exc:
                log(f"  [clip] stock background unusable ({exc}) — plain card")
                bg_video = None
    if bg_video is None:
        base = ColorClip(size=(W, H), color=(8, 10, 18))
        layers.append(call(base, ("with_duration", "set_duration"), seconds))

    banner = ImageClip(image_path)
    banner = call(banner, ("with_duration", "set_duration"), seconds)
    banner = call(banner, ("resized", "resize"), width=int(W * 0.88))
    banner = call(banner, ("with_position", "set_position"), ("center", "center"))
    layers.append(banner)

    clip = CompositeVideoClip(layers, size=(W, H))
    clip = call(clip, ("with_duration", "set_duration"), seconds)

    track = None
    if s.get("news_clip_music", True):
        tracks = sorted(MUSIC_DIR.glob("*.mp3"))
        library = store().get("music_library", [])
        pool = [t for t in tracks if t.name in library] or tracks
        if pool:
            import random
            track = random.choice(pool)
    if track:
        try:
            audio = AudioFileClip(str(track))
            audio = call(audio, ("subclipped", "subclip"), 0, min(seconds, int(audio.duration)))
            try:
                audio = call(audio, ("with_volume_scaled", "volumex"), 0.7)
            except RuntimeError:
                pass                                # volume control is optional
            clip = call(clip, ("with_audio", "set_audio"), audio)
            log(f"  [clip] music: {track.name}")
        except Exception as exc:
            log(f"  [clip] music failed ({exc}) — silent clip")
    else:
        log("  [clip] no music in music/ — silent clip")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"fps": 24, "codec": "libx264", "audio_codec": "aac", "logger": None}
    try:
        clip.write_videofile(out_path, **kwargs)
    except TypeError:                               # 1.x also accepts verbose=
        clip.write_videofile(out_path, verbose=False, **kwargs)
    clip.close()
    log(f"  [clip] ✅ {seconds}s story clip "
        f"({'stock background' if bg_video else 'plain card'}) → {out_path}")
    return out_path


class PublishError(RuntimeError):
    """Carries the platform's OWN error text upward.

    The publishers used to log the Graph error and return None, so the caller
    only knew "returned no id" and the actual reason never left the log file.
    """


def _graph_error(resp) -> str:
    try:
        e = resp.json().get("error", {})
        parts = [e.get("message", ""), e.get("error_user_title", ""),
                 e.get("error_user_msg", "")]
        detail = " | ".join(p for p in parts if p)
        return detail or resp.text[:300]
    except Exception:
        return resp.text[:300]


PID_FILE = BASE_DIR / ".warrior_pids.json"
_CHILDREN: list[subprocess.Popen] = []


def _pid_alive_python(pid: int) -> bool:
    """Is this PID still alive AND still a python process? PIDs get recycled, so
    killing a bare remembered number could hit something unrelated."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout.lower()
            return "python" in out
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"python" in f.read().lower()
    except Exception:
        return False


def _kill_pid(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=20)
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True
    except Exception as exc:
        log(f"  [recover] could not kill pid {pid}: {exc}")
        return False


def kill_orphans() -> int:
    """Kill children left behind by a previous run.

    Closing the console window does NOT kill browser_bridge.py or
    telegram_bot.py — they keep running. On the next start the old bridge still
    owns port 5000 and the old bot still holds the Telegram long-poll, so the new
    run looks completely dead. That is why stopping used to mean deleting the DB.
    """
    if not PID_FILE.exists():
        return 0
    try:
        old = json.loads(PID_FILE.read_text())
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return 0
    killed = 0
    for entry in old.get("children", []):
        pid, name = entry.get("pid"), entry.get("name", "?")
        if pid and _pid_alive_python(int(pid)):
            log(f"♻️ killing orphaned {name} from a previous run (pid {pid})")
            if _kill_pid(int(pid)):
                killed += 1
    PID_FILE.unlink(missing_ok=True)
    if killed:
        time.sleep(2)                     # let the port and the poll actually release
    return killed


def _record_child(proc: subprocess.Popen, name: str) -> None:
    _CHILDREN.append(proc)
    data = {"parent": os.getpid(), "started": now_ist().isoformat(), "children": []}
    if PID_FILE.exists():
        try:
            data = json.loads(PID_FILE.read_text())
        except Exception:
            pass
    data.setdefault("children", []).append({"pid": proc.pid, "name": name})
    data["parent"] = os.getpid()
    PID_FILE.write_text(json.dumps(data, indent=2))


def shutdown_children(*_a) -> None:
    """Take the children down with us on Ctrl-C, SIGTERM or a normal exit."""
    for proc in _CHILDREN:
        try:
            if proc.poll() is None:
                _kill_pid(proc.pid)
        except Exception:
            pass
    PID_FILE.unlink(missing_ok=True)


def release_telegram_poll() -> None:
    """Free a long-poll session held by a previous bot process.

    Two processes calling getUpdates on one token gives 409 Conflict and the
    panel goes silent. deleteWebhook(drop_pending_updates) forces a clean slate.
    """
    if not TG_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/deleteWebhook",
                      data={"drop_pending_updates": "true"}, timeout=30)
        log("♻️ released any stale Telegram poll session")
    except Exception as exc:
        log(f"  [recover] deleteWebhook failed (harmless): {exc}")


def recover_state() -> None:
    """Unstick every row a hard kill can strand. Runs on every boot.

    Without this, work that was mid-flight when the process died stays in a
    state nothing ever clears, so it neither runs nor retries — and wiping the
    database looks like the only way out.
    """
    fixed = []
    with db() as c:
        n = c.execute("UPDATE tasks SET status='scheduled' WHERE status='running'").rowcount
        if n:
            fixed.append(f"{n} task(s) requeued")
        # A panel command interrupted mid-flight is worth exactly one retry.
        n = c.execute("UPDATE botcmd SET status='pending' "
                      "WHERE status='running' AND (retried IS NULL OR retried=0)").rowcount
        if n:
            c.execute("UPDATE botcmd SET retried=1 WHERE status='pending' AND retried IS NULL")
            fixed.append(f"{n} panel command(s) retried")
        n = c.execute("UPDATE botcmd SET status='failed' WHERE status='running'").rowcount
        if n:
            fixed.append(f"{n} panel command(s) abandoned")
        # Nobody is waiting on these any more — the process that was died.
        n = c.execute("UPDATE approvals SET status='expired' WHERE status='pending'").rowcount
        if n:
            fixed.append(f"{n} orphaned approval card(s) expired")
    global _healing
    _healing = False
    if fixed:
        log("♻️ crash recovery: " + ", ".join(fixed))
    else:
        log("♻️ crash recovery: nothing stranded, clean boot")


def full_reset() -> None:
    """`--reset`: everything a restart should fix, without deleting warrior.db.
    History, calendar and performance data all survive."""
    log("🧹 FULL RESET")
    log(f"  killed {kill_orphans()} orphaned process(es)")
    release_telegram_poll()
    init_db()
    recover_state()
    with db() as c:
        c.execute("DELETE FROM botcmd WHERE status IN ('done','failed')")
        c.execute("DELETE FROM approvals WHERE status!='pending'")
    log("🧹 reset complete — safe to start normally now")


# ---------------------------------------------------------------------------
# Approval flow (card lives in telegram_bot.py; we wait on the shared db)
# ---------------------------------------------------------------------------

def request_approval(kind: str, caption: str, files: list[str],
                     payload: dict | None = None) -> tuple[bool, dict]:
    """Insert an approval row → telegram_bot sends the card with a 1-minute
    auto-post timeout → we block until a decision lands. If the bot is down,
    settings.auto_publish_on_timeout decides after a grace period."""
    aid = uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("INSERT INTO approvals(id,kind,caption,files,payload,created) VALUES(?,?,?,?,?,?)",
                  (aid, kind, caption, json.dumps(files),
                   json.dumps(payload or {}, ensure_ascii=False), now_ist().isoformat()))
    log(f"  [approval] {aid} ({kind}) waiting for Telegram decision…")
    s = store()["settings"]
    grace = s.get("approval_timeout_seconds", 60) + 120
    deadline = time.time() + grace
    while time.time() < deadline:
        with db() as c:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if row and row["status"] != "pending":
            edited = json.loads(row["payload"] or "{}")
            approved = row["status"] in ("approved", "auto")
            log(f"  [approval] {aid} → {row['status']}")
            return approved, edited
        # The card promises "posting is PAUSED" while the admin types a new
        # caption. Without this the orchestrator kept its own countdown and
        # auto-published the ORIGINAL caption mid-edit.
        if row and row["decided_by"] == "editing":
            deadline = max(deadline, time.time() + grace)
        time.sleep(4)
    fallback = bool(s.get("auto_publish_on_timeout", True))
    with db() as c:
        c.execute("UPDATE approvals SET status=?, decided=? WHERE id=? AND status='pending'",
                  ("auto" if fallback else "rejected", now_ist().isoformat(), aid))
    log(f"  [approval] {aid} bot silent → {'AUTO-POST' if fallback else 'rejected'}")
    return fallback, payload or {}


def log_performance(platform: str, kind: str, topic: str, ref_id: str | None) -> None:
    with db() as c:
        c.execute("INSERT INTO performance(dt,platform,kind,topic,ref_id,metrics) VALUES(?,?,?,?,?,?)",
                  (now_ist().isoformat(), platform, kind, topic[:200], ref_id or "", "{}"))


# moviepy assembly removed as per God Prompt refactor

# ---------------------------------------------------------------------------
# AGENT PIPELINES
# ---------------------------------------------------------------------------

def run_ceo() -> None:
    """Brand CEO 48-hour strategic loop → directives stored for the Content Manager."""
    log("👑 BRAND CEO strategic review starting")
    news = get_state("latest_news", "No breaking news captured.")
    trends = get_state("latest_trends", "No social trends captured.")
    with db() as c:
        rows = c.execute("SELECT * FROM performance ORDER BY id DESC LIMIT 40").fetchall()
    perf = "\n".join(f"{r['dt'][:10]} {r['platform']} {r['kind']}: {r['topic']}"
                     for r in rows) or "No performance data yet (first run)."
    prompt = render_prompt("brand_ceo",
                           current_date=now_ist().strftime("%Y-%m-%d"),
                           breaking_news=news, social_trends=trends,
                           performance_logs=perf)
    result = ask_gemini(prompt, schema_key="brand_ceo")
    set_state("ceo_directives", json.dumps(result, ensure_ascii=False))
    set_state("last_ceo_run", now_ist().isoformat())
    tg_admin("👑 <b>Brand CEO review complete</b>\n"
             f"Health: {(result.get('executive_summary') or {}).get('brand_health_assessment')}\n"
             f"Mood: {(result.get('executive_summary') or {}).get('market_mood')}\n"
             f"Pillars: {', '.join(result.get('strategic_pillars', []))}")
    urgent = result.get("urgent_interruptions", {})
    if urgent.get("has_breaking_news"):
        log("🚨 CEO issued an URGENT INTERRUPTION")
        schedule_task(now_ist() + timedelta(minutes=2), "POST_MAKER", "SINGLE_POST",
                      urgent.get("interruption_directive", ""), urgent=True)
    run_content_manager()


def run_content_manager() -> None:
    """Content Manager → 7-day master calendar → tasks table."""
    log("📅 CONTENT MANAGER building the 7-day calendar")
    directives = get_state("ceo_directives", "{}")
    prompt = render_prompt("content_manager",
                           current_datetime=now_ist().strftime("%Y-%m-%d %H:%M"),
                           brand_ceo_json=directives)
    result = ask_gemini(prompt, schema_key="content_manager")
    tasks = result.get("scheduled_tasks", [])
    if len(tasks) < 14:
        log(f"  [cm] anti-laziness violated ({len(tasks)} tasks) — one retry")
        prompt += ("\n\nCRITICAL FAILURE NOTICE: you returned fewer than 14 tasks. "
                   "Regenerate the FULL 7-day calendar with ≥2 tasks EVERY day (≥14 total).")
        result = ask_gemini(prompt, schema_key="content_manager")
        tasks = result.get("scheduled_tasks", tasks)
        if len(tasks) < 14:
            log(f"  [cm] STILL under-delivering after retry ({len(tasks)} tasks)")
            tg_admin(f"⚠️ <b>Content Manager under-delivered</b>\n"
                     f"Only {len(tasks)} tasks for 7 days (expected ≥14).\n"
                     f"The week's calendar will be sparse.")
    added = 0
    capped = 0
    cap = store()["settings"].get("max_daily_tasks", 8)
    per_day: dict[str, int] = {}
    for t in tasks:
        try:
            dt = datetime.strptime(f"{t['execution_date']} {t['execution_time']}", "%Y-%m-%d %H:%M")
            if ZoneInfo:
                dt = dt.replace(tzinfo=ZoneInfo(store()["settings"].get("timezone", "Asia/Kolkata")))
            day = t["execution_date"]
            if per_day.get(day, 0) >= cap:
                capped += 1
                continue
            schedule_task(dt, t.get("assigned_agent", "POST_MAKER"),
                          t.get("content_type", "SINGLE_POST"),
                          t.get("topic_blueprint", ""),
                          slides=int(t.get("carousel_slide_count") or 0),
                          urgent=bool(t.get("is_urgent_news")))
            per_day[day] = per_day.get(day, 0) + 1
            added += 1
        except Exception as exc:
            log(f"  [cm] skipped malformed task: {exc}")
    if capped:
        log(f"  [cm] {capped} task(s) dropped by the {cap}/day cap")
    set_state("last_plan_run", now_ist().isoformat())
    tg_admin(f"📅 <b>New 7-day calendar</b>: {added} tasks scheduled"
             + (f" ({capped} over daily cap)" if capped else "") + ".\n"
             f"{result.get('schedule_overview', '')}")


def schedule_task(dt: datetime, agent: str, content_type: str, blueprint: str,
                  slides: int = 0, urgent: bool = False) -> None:
    # Reject past-dated tasks: an LLM date slip would otherwise dump the whole
    # backlog into the next tick (3 at a time) and burn the day's quota.
    if dt.tzinfo is not None and dt < now_ist() - timedelta(minutes=5):
        log(f"  [sched] rejected past-dated task {dt.isoformat()} ({agent})")
        return
    with db() as c:
        c.execute("INSERT INTO tasks(id,execution_dt,agent,content_type,slides,blueprint,urgent,created) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex[:10], dt.isoformat(), agent, content_type,
                   slides, blueprint, int(urgent), now_ist().isoformat()))


def run_reel_task(blueprint: str, content_type: str) -> None:
    """REEL_MAKER: Gemini (Reel Wala) → 1× Flow/Veo video (God Prompt)
    → approval card → IG Reel + YT Short + TG."""
    log(f"🎬 REEL_MAKER: {blueprint[:80]}")
    # The Content Manager speaks UGC_VIDEO / TALKING_OBJECT; the reel_maker prompt
    # speaks UGC / ANIMATED. Translate here so the prompt stays exactly as written
    # and neither side has to change its vocabulary.
    ct = {"UGC_VIDEO": "UGC", "UGC": "UGC",
          "TALKING_OBJECT": "ANIMATED", "ANIMATED": "ANIMATED"}.get(
              (content_type or "").upper(), "ANIMATED")
    log(f"  [reel] content_type {content_type!r} -> {ct}")
    cta_keyword = "GUIDE" if any(
        w in blueprint.lower() for w in ["pdf", "guide", "playbook", "course"]) else "WARRIOR"

    # Use the CEO's live strategic pillars instead of a hardcoded string, so the
    # 48h strategy review actually reaches the reel writer.
    try:
        pillars = json.loads(get_state("ceo_directives", "{}")).get("strategic_pillars") or []
    except json.JSONDecodeError:
        pillars = []
    pillar = ", ".join(pillars[:3]) if pillars else "Trading"

    plan = ask_gemini(render_prompt("reel_maker",
                                    content_type=ct,
                                    topic_blueprint=blueprint,
                                    pillar=pillar),
                      schema_key="reel_maker")
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    work = OUT_DIR / f"reel_{stamp}"
    work.mkdir(exist_ok=True)
    
    veo_prompt = plan.get("combined_veo_prompt", "")
    log("🎬 Sending God Prompt to Flow...")
    
    final_path = str(work / "final_reel.mp4")
    vid = bridge_task("video", veo_prompt, final_path, n=1)
    
    saved_vids = vid.get("saved_to", [])
    if not isinstance(saved_vids, list):
        saved_vids = [saved_vids]
    final = saved_vids[0] if saved_vids else final_path
    
    # The reel_maker schema is FLAT (story_planning / combined_veo_prompt /
    # instagram_caption). An older revision nested these under "audio_and_copy",
    # and the hard [] lookup crashed AFTER the Veo video was already generated —
    # burning a full generation on every reel. Accept both shapes.
    copy_block = plan.get("audio_and_copy") or plan
    caption = copy_block.get("instagram_caption") or blueprint[:120]
    ok, edited = request_approval("reel", caption, [final],
                                  {"caption": caption, "topic": blueprint,
                                   "dm_keyword": copy_block.get("auto_dm_keyword")
                                   or cta_keyword})
    if not ok:
        return
    caption = edited.get("caption", caption)
    routing = store()["routing"].get("reel", {})
    log(f"  [route] reel -> {[k for k, v in routing.items() if v]}")
    # Each destination is isolated: a Meta outage must not cost you the YouTube
    # upload or the Telegram drop.
    if routing.get("instagram_reel"):
        try:
            rid = publish_instagram_reel(final, caption)
            log_performance("instagram", "reel", blueprint, rid)
        except Exception as exc:
            report_failure("REEL_MAKER", exc, step="instagram_reel", topic=blueprint)
    if routing.get("youtube_short"):
        try:
            yid = publish_youtube_short(final, blueprint[:90], caption)
            log_performance("youtube", "short", blueprint, yid)
        except Exception as exc:
            report_failure("REEL_MAKER", exc, step="youtube_short", topic=blueprint)
    if routing.get("twitter"):
        try:
            publish_twitter(caption, [final])
        except Exception as exc:
            report_failure("REEL_MAKER", exc, step="twitter", topic=blueprint)
    if routing.get("telegram"):
        # The reel ALWAYS reaches Telegram, and it carries the Telemanager's
        # context copy rather than the bare Instagram caption.
        try:
            tele = ask_gemini(render_prompt("tele_manager", trigger_type="SCHEDULED_REEL",
                                            topic_blueprint=blueprint,
                                            current_time=now_ist().strftime("%H:%M")),
                              schema_key="tele_manager")
            context_copy = tele["telegram_message"]["text_body"].replace("[LINK HERE]", "")
        except Exception as exc:
            log(f"  [reel] Telemanager copy failed ({exc}) — using the IG caption")
            tele, context_copy = {}, caption
        tg_channel_file(final, context_copy, "video")

        vn = tele.get("voice_note", {})
        if vn.get("is_required") and vn.get("hinglish_script", "NONE") != "NONE":
            try:
                tts = bridge_task("tts", vn["hinglish_script"],
                                  str(work / f"vn_{stamp}.mp3"))
                sv = tts.get("saved_to")
                tg_channel_file(sv[0] if isinstance(sv, list) else sv,
                                "🎤 Founder note", "voice")
            except Exception as exc:
                log(f"  [reel] voice note skipped: {exc}")
                
        log_performance("telegram", "reel", blueprint, None)
    log("🎬 reel pipeline complete ✅")


def run_post_task(blueprint: str, content_type: str, slides: int = 0) -> None:
    """POST_MAKER: Gemini (Banner) → ChatGPT image(s) → approval → IG + TG."""
    log(f"🖼 POST_MAKER ({content_type}): {blueprint[:80]}")
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    work = OUT_DIR / f"post_{stamp}"
    work.mkdir(exist_ok=True)
    count = max(1, slides) if content_type == "CAROUSEL" else 1
    images, caption = [], ""
    
    note = f"\n\nFORMAT INSTRUCTION: Generate exactly {count} highly descriptive image prompts. If count > 1, design them as a cohesive swipe sequence (Carousel) where each prompt builds on the narrative but has a distinct visual."
    
    banner = ask_gemini(render_prompt("post_maker",
                                      topic_blueprint=blueprint + note,
                                      current_date=now_ist().strftime("%d %b %Y")),
                        schema_key="post_maker")
    
    prompts = banner.get("chatgpt_image_prompts", [])
    if isinstance(prompts, str):
        prompts = [prompts]
        
    # Fallback if AI didn't follow the plural key
    if not prompts and "chatgpt_image_prompt" in banner:
        p = banner["chatgpt_image_prompt"]
        prompts = p if isinstance(p, list) else [p]
        
    # Pad or truncate prompts to match count
    while len(prompts) < count:
        prompts.append(prompts[-1] if prompts else "finance trading background")
    prompts = prompts[:count]

    chatgpt_chat_code = None
    for i, p in enumerate(prompts):
        img = bridge_task("img", p,
                          str(work / f"slide_{i + 1}.png"), chat_code=chatgpt_chat_code)
        chatgpt_chat_code = img.get("chat_code") or chatgpt_chat_code
        images.append(img.get("saved_to"))

    copy = banner.get("instagram_copy", {})
    caption = ("\n".join("• " + b for b in copy.get("bullet_points", []))
               + "\n\n" + copy.get("cta_question", "")
               + "\n\n" + copy.get("hashtags", ""))
    ok, edited = request_approval("carousel" if count > 1 else "post",
                                  caption, images, {"caption": caption, "topic": blueprint})
    if not ok:
        return
    caption = edited.get("caption", caption)
    is_news = bool(re.search(r"urgent news|breaking", blueprint, re.I))
    key = "news" if is_news else ("carousel" if count > 1 else "post")
    routing = store()["routing"].get(key, {})
    log(f"  [route] {key} → {[k for k, v in routing.items() if v]}")

    # --- Instagram Story: banner + music as a short vertical clip -------------
    if routing.get("instagram_story"):
        try:
            clip = build_news_clip(images[0], str(work / "story_clip.mp4"), topic=blueprint)
            sid = publish_instagram_story(clip, is_video=True)
            log_performance("instagram", "story", blueprint, sid)
        except Exception as exc:
            report_failure("POST_MAKER", exc, step="instagram_story", topic=blueprint)
            # a story failure must not stop the feed post
            try:
                sid = publish_instagram_story(images[0], is_video=False)
                log_performance("instagram", "story", blueprint, sid)
                log("  [route] fell back to a static image story")
            except Exception as exc2:
                log(f"  [route] static story fallback also failed: {exc2}")

    # --- Instagram feed ------------------------------------------------------
    if routing.get("instagram_post"):
        try:
            rid = (publish_instagram_carousel(images, caption) if count > 1
                   else publish_instagram_image(images[0], caption))
            log_performance("instagram", key, blueprint, rid)
        except Exception as exc:
            report_failure("POST_MAKER", exc, step="instagram_post", topic=blueprint)

    # --- Twitter (dark until OAuth 1.0a keys exist) --------------------------
    if routing.get("twitter"):
        try:
            tid = publish_twitter(caption, images)
            if tid:
                log_performance("twitter", key, blueprint, tid)
        except Exception as exc:
            report_failure("POST_MAKER", exc, step="twitter", topic=blueprint)

    # --- Telegram ------------------------------------------------------------
    if routing.get("telegram"):
        try:
            tg_channel_file(images[0], caption, "photo")
            log_performance("telegram", key, blueprint, None)
        except Exception as exc:
            report_failure("POST_MAKER", exc, step="telegram", topic=blueprint)
    log("🖼 post pipeline complete ✅")


def run_pdf_task(blueprint: str) -> None:
    """PDF_MAKER: Gemini (PDF Wala) markdown → Claude connector builds the PDF
    → approval → Telegram drop with Telemanager hype + optional voice note."""
    log(f"📄 PDF_MAKER: {blueprint[:80]}")
    doc = ask_gemini(render_prompt("pdf_maker", topic_blueprint=blueprint),
                     schema_key="pdf_maker")
    md = doc["pdf_markdown_content"]
    title = doc["document_metadata"].get("title", "Stock Warrior Guide")
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    pdf_prompt = ("Create a beautifully formatted, premium-looking PDF document from the "
                  "following Markdown. Preserve all headers, tables, blockquotes and code "
                  "boxes. Do not add commentary — produce ONLY the PDF file for download.\n\n"
                  + md)
    pdf = bridge_task("pdf", pdf_prompt, str(OUT_DIR / f"pdf_{stamp}.pdf"))
    saved = pdf.get("saved_to")
    pdf_path = saved[0] if isinstance(saved, list) else saved
    tele = ask_gemini(render_prompt("tele_manager", trigger_type="SCHEDULED_PDF",
                                    topic_blueprint=blueprint,
                                    current_time=now_ist().strftime("%H:%M")),
                      schema_key="tele_manager")
    msg = tele["telegram_message"]["text_body"].replace("[LINK HERE]", "")
    ok, edited = request_approval("pdf", msg, [pdf_path],
                                  {"caption": msg, "topic": blueprint, "title": title})
    if not ok:
        return
    msg = edited.get("caption", msg)
    if store()["routing"]["pdf"].get("telegram"):
        tg_channel_file(pdf_path, msg, "document")
        vn = tele.get("voice_note", {})
        if vn.get("is_required") and vn.get("hinglish_script", "NONE") != "NONE":
            try:
                tts = bridge_task("tts", vn["hinglish_script"],
                                  str(OUT_DIR / f"vn_{stamp}.mp3"))
                sv = tts.get("saved_to")
                tg_channel_file(sv[0] if isinstance(sv, list) else sv,
                                "🎙 Founder note", "voice")
            except Exception as exc:
                log(f"  [pdf] voice note skipped: {exc}")
        log_performance("telegram", "pdf", blueprint, None)
    log("📄 pdf pipeline complete ✅")


def run_poll_task(blueprint: str) -> None:
    """TELEGRAM_POLL: Telemanager → channel poll + psychology message."""
    log(f"📊 TELEGRAM_POLL: {blueprint[:80]}")
    tele = ask_gemini(render_prompt("tele_manager", trigger_type="RANDOM_ENGAGEMENT",
                                    topic_blueprint=blueprint,
                                    current_time=now_ist().strftime("%H:%M")),
                      schema_key="tele_manager")
    tmsg = tele["telegram_message"]
    tg_channel_text(tmsg["text_body"], pin=bool(tmsg.get("pin_message")))
    poll = tele.get("interactive_poll", {})
    if poll.get("is_required"):
        tg_channel_poll(poll["question"], poll.get("options", []))
        expl = poll.get("explanation_text")
        if expl:
            # daemon=True: a plain Timer is non-daemon and keeps the process
            # alive for a full hour after shutdown is requested.
            t = threading.Timer(3600, tg_channel_text, args=(f"🧠 *Poll answer:*\n{expl}",))
            t.daemon = True
            t.start()
    log_performance("telegram", "poll", blueprint, None)
    log("📊 poll complete ✅")


def run_news_check() -> None:
    """News slot: Gemini (with its own web access) fetches Indian market news →
    stored for the CEO → if truly major, an urgent post fires immediately."""
    log("📰 NEWS RUN")
    prompt = (
        "You are the market news scout for an Indian trading brand. Search your knowledge "
        "of TODAY'S Indian stock market and global macro news. Output STRICTLY valid JSON, "
        "no markdown fences, schema: {\"headlines\": [{\"headline\": \"...\", "
        "\"why_it_matters\": \"...\", \"magnitude\": \"ROUTINE | NOTABLE | MAJOR\"}], "
        "\"social_trends\": [\"trend 1\", \"trend 2\"]}. "
        f"Today is {now_ist().strftime('%A %d %B %Y')}. Max 5 headlines, Indian-market first.")
    try:
        news = ask_gemini(prompt)
    except Exception as exc:
        log(f"  [news] failed: {exc}")
        return
    heads = news.get("headlines", [])
    set_state("latest_news", json.dumps(heads, ensure_ascii=False))
    set_state("latest_trends", json.dumps(news.get("social_trends", []), ensure_ascii=False))
    major = [h for h in heads if h.get("magnitude") == "MAJOR"]
    if major:
        h = major[0]
        log(f"🚨 MAJOR news: {h['headline']}")
        schedule_task(now_ist() + timedelta(minutes=1), "POST_MAKER", "SINGLE_POST",
                      f"URGENT NEWS POSTER: {h['headline']} — {h['why_it_matters']}",
                      urgent=True)


def run_growth_scan() -> None:
    """Growth Hacker: scan Reddit loss-porn/questions → empathetic reply →
    approval card → post via PRAW. X scan optional via bearer token."""
    log("🌱 GROWTH HACKER scan")
    s = store()["settings"]
    try:
        import praw
    except ImportError:
        log("  [growth] praw not installed — skipped")
        return
    cid, csec = ENV.get("REDDIT_CLIENT_ID"), ENV.get("REDDIT_CLIENT_SECRET")
    user, pwd = ENV.get("REDDIT_USERNAME"), ENV.get("REDDIT_PASSWORD")
    if not (cid and csec and user and pwd):
        log("  [growth] Reddit credentials missing in .env — skipped")
        return
    reddit = praw.Reddit(client_id=cid, client_secret=csec, username=user,
                         password=pwd, user_agent="stockwarrior-growth/1.0")
    replied = set(json.loads(get_state("growth_replied", "[]")))
    hits = 0
    for sub in s.get("subreddits", []):
        try:
            for post in reddit.subreddit(sub).new(limit=15):
                if hits >= 2 or post.id in replied:
                    continue
                text = (post.title + "\n" + (post.selftext or ""))[:1500]
                if not re.search(r"loss|blew|blown|stop ?loss|help|mistake|wiped|debt|revenge",
                                 text, re.I):
                    continue
                pkg = ask_gemini(render_prompt("growth_hacker", platform_name="Reddit",
                                               user_post_text=text),
                                 schema_key="growth_hacker")
                rp = pkg.get("reply_package", {})
                if not rp.get("is_safe_to_reply"):
                    continue
                ok, edited = request_approval(
                    "growth_reply", rp.get("reply_text", ""), [],
                    {"caption": rp.get("reply_text", ""), "topic": post.title,
                     "url": f"https://reddit.com{post.permalink}"})
                if ok:
                    post.reply(edited.get("caption", rp["reply_text"]))
                    replied.add(post.id)
                    hits += 1
                    log_performance("reddit", "reply", post.title, post.id)
        except Exception as exc:
            log(f"  [growth] r/{sub} failed: {exc}")
    set_state("growth_replied", json.dumps(list(replied)[-500:]))
    set_state("last_growth_run", now_ist().isoformat())
    if s.get("twitter_enabled") and ENV.get("TWITTER_BEARER_TOKEN"):
        log("  [growth] X scan enabled (read-only trend capture)")
        try:
            r = requests.get("https://api.twitter.com/2/tweets/search/recent",
                             params={"query": "(trading loss OR stoploss hit) lang:en -is:retweet",
                                     "max_results": 10},
                             headers={"Authorization": f"Bearer {ENV['TWITTER_BEARER_TOKEN']}"},
                             timeout=30)
            if r.status_code == 200:
                texts = [t["text"][:120] for t in r.json().get("data", [])][:5]
                set_state("latest_trends", json.dumps(texts, ensure_ascii=False))
        except Exception as exc:
            log(f"  [growth] X scan failed: {exc}")


def run_prompt_evolution() -> None:
    """Every 30 days: feed real logs to the Prompt Evolver → proposal stored →
    MANUAL approval in the Telegram panel (never auto-applied)."""
    log("🧬 PROMPT EVOLUTION cycle")
    with db() as c:
        rows = c.execute("SELECT * FROM performance ORDER BY id DESC LIMIT 100").fetchall()
    if not rows:
        log("  [evolve] no performance data yet — skipping (nothing to learn from)")
        set_state("last_evolution_run", now_ist().isoformat())
        return
    perf = "\n".join(f"{r['dt'][:10]} {r['platform']} {r['kind']}: {r['topic']}"
                     for r in rows)
    for name in ("reel_maker", "post_maker", "tele_manager"):
        try:
            res = ask_gemini(render_prompt("prompt_evolver", prompt_name=name,
                                           current_prompt=store()["prompts"][name],
                                           performance_logs=perf),
                             schema_key="prompt_evolver")
            if res.get("verdict") == "EVOLVE" and res.get("evolved_prompt"):
                with db() as c:
                    c.execute("INSERT INTO evolution(dt,prompt_name,payload) VALUES(?,?,?)",
                              (now_ist().isoformat(), name,
                               json.dumps(res, ensure_ascii=False)))
                tg_admin(f"🧬 <b>Prompt evolution proposal</b> for <code>{name}</code>\n"
                         f"Changes: {len(res.get('changes', []))} — review with /evolve "
                         f"in the panel. NOT applied until you approve.")
        except Exception as exc:
            log(f"  [evolve] {name} failed: {exc}")
    set_state("last_evolution_run", now_ist().isoformat())


# ---------------------------------------------------------------------------
# TEST LAB — every step and every workflow, runnable on demand from Telegram
# ---------------------------------------------------------------------------

TEST_DIR = OUT_DIR / "_testlab"


def _t_ok(name: str, detail: str = "") -> dict:
    return {"name": name, "ok": True, "detail": detail}


def _t_fail(name: str, exc: BaseException) -> dict:
    info = explain_error(exc, step=name)
    return {"name": name, "ok": False, "detail": info["cause"], "fix": info["fix"],
            "raw": info["raw"]}


def _test_bridge() -> dict:
    st = bridge_status()
    if not st.get("ok"):
        raise BridgeError("bridge not reachable on 127.0.0.1:5000")
    if not st.get("extension_live"):
        raise BridgeError("bridge is up but the Chrome extension has not polled in")
    return _t_ok("bridge", "bridge + extension live")


def _test_gemini() -> dict:
    d = bridge_task("gemini", 'Reply with STRICT JSON only, no fences: {"pong": true}')
    raw = d.get("response") or d.get("text") or ""
    if not raw.strip():
        raise BridgeError("Gemini returned an EMPTY payload (BUG-0 signature)")
    parse_llm_json(raw)
    return _t_ok("gemini", f"{len(raw)} chars, valid JSON")


def _test_image() -> dict:
    p = TEST_DIR / "test_image.png"
    d = bridge_task("img", "A simple dark blue square, minimal, no text.", str(p))
    got = _first(d.get("saved_to")) or str(p)
    _assert_file(got, 500)
    return _t_ok("chatgpt_image", got)


def _test_pdf() -> dict:
    p = TEST_DIR / "test_doc.pdf"
    d = bridge_task("pdf", "Create a one-page PDF containing only the heading 'Test'.", str(p))
    got = _first(d.get("saved_to")) or str(p)
    _assert_file(got, 100)
    if Path(got).read_bytes()[:4] != b"%PDF":
        raise RuntimeError("file is not a PDF (missing %PDF magic bytes)")
    return _t_ok("claude_pdf", got)


def _test_video() -> dict:
    p = TEST_DIR / "test_video.mp4"
    d = bridge_task("video", "STRICT INSTRUCTION: Directly start video generation. "
                             "A single candlestick chart candle rising. "
                             "Format: 9:16 vertical, 4K.", str(p), n=1)
    got = _first(d.get("saved_to")) or str(p)
    _assert_file(got, 1000)
    return _t_ok("flow_video", got)


def _test_tts() -> dict:
    p = TEST_DIR / "test_voice.mp3"
    d = bridge_task("tts", "Namaste, yeh ek test hai.", str(p))
    got = _first(d.get("saved_to")) or str(p)
    _assert_file(got, 500)
    return _t_ok("elevenlabs_tts", got)


def _test_clip() -> dict:
    src = TEST_DIR / "test_image.png"
    if not src.exists():
        _test_image()
    out = build_news_clip(str(src), str(TEST_DIR / "test_clip.mp4"),
                          topic="Nifty stock market update")
    _assert_file(out, 1000)
    return _t_ok("news_clip", f"{store()['settings'].get('news_clip_seconds', 15)}s clip built")


def _test_ig_direct() -> dict:
    if not (ENV.get("IG_USERNAME") and ENV.get("IG_PASSWORD")):
        return {"name": "ig_direct", "ok": None,
                "detail": "IG_USERNAME / IG_PASSWORD not set — direct upload disabled"}
    cl = ig_private_client()
    if cl is None:
        raise PublishError("instagrapi login failed — check the log. A challenge may be "
                           "pending; open Instagram on your phone and approve it.")
    return _t_ok("ig_direct", f"logged in as @{ENV.get('IG_USERNAME')}")


def _test_pexels() -> dict:
    if not PEXELS_API_KEY:
        return {"name": "pexels", "ok": None, "detail": "PEXELS_API_KEY not set"}
    out = pexels_video("stock market trading chart", str(TEST_DIR / "test_stock.mp4"), 10)
    if not out:
        raise RuntimeError("Pexels returned no usable video — check the key and quota")
    _assert_file(out, 10_000)
    return _t_ok("pexels", f"{Path(out).stat().st_size // 1024} KB stock clip")


def _test_ig_story() -> dict:
    src = TEST_DIR / "test_clip.mp4"
    if not src.exists():
        _test_clip()
    sid = publish_instagram_story(str(src), is_video=True)
    if not sid:
        raise PublishError("Meta accepted the story but returned no media id.")
    return _t_ok("instagram_story", f"story id {sid}")


def _test_ig_post() -> dict:
    src = TEST_DIR / "test_image.png"
    if not src.exists():
        _test_image()
    rid = publish_instagram_image(str(src), "Test post — Stock Warrior Test Lab.")
    if not rid:
        raise PublishError("Meta accepted the post but returned no media id.")
    return _t_ok("instagram_post", f"media id {rid}")


def _test_ig_reel() -> dict:
    src = TEST_DIR / "test_video.mp4"
    if not src.exists():
        _test_video()
    rid = publish_instagram_reel(str(src), "Test reel — Stock Warrior Test Lab.")
    if not rid:
        raise PublishError("Meta accepted the reel but returned no media id.")
    return _t_ok("instagram_reel", f"media id {rid}")


def _test_youtube() -> dict:
    src = TEST_DIR / "test_video.mp4"
    if not src.exists():
        _test_video()
    yid = publish_youtube_short(str(src), "Test Short", "Stock Warrior Test Lab.")
    if not yid:
        raise PublishError("YouTube accepted the upload but returned no video id.")
    return _t_ok("youtube_short", f"video id {yid}")


def _test_twitter() -> dict:
    ready, why = twitter_ready()
    if not ready:
        return {"name": "twitter", "ok": None, "detail": why}      # None = not configured
    tid = publish_twitter("Test post from Stock Warrior Test Lab.")
    if not tid:
        raise RuntimeError("Twitter post returned no id")
    return _t_ok("twitter", f"tweet id {tid}")


def _test_meta_token() -> dict:
    if not (META_TOKEN and META_IG_ID):
        return {"name": "meta_token", "ok": None, "detail": "META_ACCESS_TOKEN / META_IG_ACCOUNT_ID not set"}
    # NOTE: `account_type` does NOT exist on the IG Business node — asking for it
    # returns "(#100) Tried accessing nonexisting field", which looks like a
    # permissions failure but is just a bad field name.
    r = requests.get(f"{GRAPH}/{META_IG_ID}",
                     params={"fields": "username,followers_count,media_count",
                             "access_token": META_TOKEN}, timeout=60)
    if r.status_code != 200:
        raise PublishError(f"Graph rejected the token: {_graph_error(r)}")
    j = r.json()
    return _t_ok("meta_token",
                 f"@{j.get('username')} — {j.get('followers_count', '?')} followers, "
                 f"{j.get('media_count', '?')} posts")


def _test_tg_text() -> dict:
    if not TG_CHANNEL:
        return {"name": "telegram_text", "ok": None, "detail": "TELEGRAM_CHANNEL_ID not set"}
    tg_channel_text("🧪 Test Lab: channel text OK.")
    return _t_ok("telegram_text", "sent to channel")


def _test_tg_photo() -> dict:
    src = TEST_DIR / "test_image.png"
    if not src.exists():
        _test_image()
    tg_channel_file(str(src), "🧪 Test Lab: channel photo OK.", "photo")
    return _t_ok("telegram_photo", "sent to channel")


def _test_tg_poll() -> dict:
    tg_channel_poll("🧪 Test Lab poll — is this working?", ["Yes", "No"])
    return _t_ok("telegram_poll", "poll sent to channel")


def _first(v):
    return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else None)


def _assert_file(path: str | None, min_bytes: int) -> None:
    if not path or not Path(path).exists():
        raise RuntimeError(f"expected a file at {path} — nothing was saved")
    size = Path(path).stat().st_size
    if size < min_bytes:
        raise RuntimeError(f"file is only {size} bytes — generation likely failed")


# name -> (label, callable, is_expensive)
TESTS: dict[str, tuple] = {
    "bridge":       ("Bridge + extension", _test_bridge, False),
    "gemini":       ("Gemini text/JSON", _test_gemini, False),
    "image":        ("ChatGPT image", _test_image, True),
    "pdf":          ("Claude PDF", _test_pdf, True),
    "video":        ("Flow/Veo video", _test_video, True),
    "tts":          ("ElevenLabs voice", _test_tts, True),
    "pexels":       ("Pexels stock video", _test_pexels, False),
    "ig_direct":    ("IG direct upload login", _test_ig_direct, False),
    "clip":         ("15s news clip (moviepy)", _test_clip, False),
    "meta_token":   ("Meta token + account", _test_meta_token, False),
    "ig_story":     ("Instagram Story", _test_ig_story, True),
    "ig_post":      ("Instagram feed post", _test_ig_post, True),
    "ig_reel":      ("Instagram Reel", _test_ig_reel, True),
    "youtube":      ("YouTube Short", _test_youtube, True),
    "twitter":      ("Twitter/X post", _test_twitter, False),
    "tg_text":      ("Telegram text", _test_tg_text, False),
    "tg_photo":     ("Telegram photo", _test_tg_photo, False),
    "tg_poll":      ("Telegram poll", _test_tg_poll, False),
}


def run_test(name: str) -> dict:
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    label, fn, _ = TESTS[name]
    log(f"🧪 TEST {name} — {label}")
    try:
        res = fn()
        res["label"] = label
        return res
    except Exception as exc:
        res = _t_fail(name, exc)
        res["label"] = label
        return res


def last_failed_tests() -> list[str]:
    """Names that failed on the most recent run — powers 'retry only failed'."""
    try:
        prev = json.loads(get_state("last_test_results", "[]"))
    except json.JSONDecodeError:
        return []
    return [r["name"] for r in prev if r.get("ok") is False and r.get("name") in TESTS]


def run_test_all(cheap_only: bool = False, only: list[str] | None = None) -> None:
    """Run a set of tests and post one scoreboard to Telegram.

    `only` lets you re-run just the ones you care about — in practice, just the
    ones that failed last time, instead of burning the whole suite again.
    """
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    if only is not None:
        names = [n for n in only if n in TESTS]
        if not names:
            tg_admin("🧪 Nothing to re-run — no failures recorded from the last run. ✅")
            return
        tg_admin(f"🔁 <b>Re-running {len(names)} test(s)</b>\n"
                 + "\n".join(f"• {_esc(TESTS[n][0])}" for n in names))
    else:
        names = [n for n, (_, _, expensive) in TESTS.items() if not (cheap_only and expensive)]
        tg_admin(f"🧪 <b>Test Lab: running {len(names)} tests</b>"
                 + (" (cheap only)" if cheap_only else " (full — this costs generations)"))
    results = [run_test(n) for n in names]
    try:
        prev = {r["name"]: r for r in json.loads(get_state("last_test_results", "[]"))}
    except json.JSONDecodeError:
        prev = {}
    prev.update({r["name"]: {"name": r["name"], "label": r.get("label"),
                             "ok": r["ok"], "detail": str(r.get("detail"))[:200]}
                 for r in results})
    set_state("last_test_results", json.dumps(list(prev.values())))
    set_state("last_test_run", now_ist().isoformat())

    passed = sum(1 for r in results if r["ok"] is True)
    skipped = sum(1 for r in results if r["ok"] is None)
    failed = [r for r in results if r["ok"] is False]
    lines = []
    for r in results:
        icon = "✅" if r["ok"] else ("⚪" if r["ok"] is None else "❌")
        lines.append(f"{icon} <b>{_esc(r['label'])}</b> — {_esc(str(r.get('detail'))[:90])}")
    body = "\n".join(lines)
    tg_admin(f"🧪 <b>Test Lab result: {passed} passed, {len(failed)} failed, "
             f"{skipped} not configured</b>\n\n{body}"
             + ("\n\n🔁 Test Lab → <b>Re-run failed only</b> to retry just the "
                f"{len(failed)} failure(s)." if failed else ""))
    for r in failed:
        tg_admin(f"❌ <b>{_esc(r['label'])}</b>\n\n<b>What happened</b>\n{_esc(r['detail'])}"
                 f"\n\n<b>How to fix</b>\n{_esc(r.get('fix', ''))}"
                 f"\n\n<b>Raw</b>\n<code>{_esc(str(r.get('raw', ''))[:300])}</code>")
    log(f"🧪 Test Lab: {passed} passed, {len(failed)} failed, {skipped} skipped")


def run_preflight() -> None:
    """Config sanity check — no generations spent. Answers 'can we go live?'."""
    s, brand, routing = store()["settings"], store()["brand"], store()["routing"]
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Telegram bot token", bool(TG_TOKEN), "TELEGRAM_BOT_TOKEN in .env"))
    checks.append(("Telegram admin id", bool(TG_ADMIN), "TELEGRAM_ADMIN_ID in .env"))
    checks.append(("Telegram channel", bool(TG_CHANNEL), "TELEGRAM_CHANNEL_ID in .env"))
    checks.append(("Meta token + IG id", bool(META_TOKEN and META_IG_ID), "🔑 Tokens screen"))
    checks.append(("YouTube OAuth file", (BASE_DIR / "yt_token.json").exists(), "yt_token.json"))
    checks.append(("Twitter OAuth 1.0a", twitter_ready()[0], "4 OAuth keys — currently dark"))
    checks.append(("Music for story clips", any(MUSIC_DIR.glob("*.mp3")),
                   "send an .mp3 to the panel's 🎵 Music screen"))
    st = bridge_status()
    checks.append(("Bridge", bool(st.get("ok")), "python browser_bridge.py"))
    checks.append(("Chrome extension", bool(st.get("extension_live")), "load the unpacked extension"))
    # every prompt referenced by the code must exist and have a schema shape
    for name in ("brand_ceo", "content_manager", "reel_maker", "post_maker",
                 "pdf_maker", "tele_manager"):
        has = name in store()["prompts"] and name in store().get("schema_shapes", {})
        checks.append((f"Prompt+schema: {name}", has, "prompts_store.json"))
    for key in ("reel", "post", "carousel", "news", "pdf"):
        checks.append((f"Routing: {key}", key in routing, "prompts_store.json routing"))
    checks.append(("Brand configured", bool(brand.get("name") and brand.get("niche")),
                   "🎨 Brand screen"))
    checks.append(("Timezone", bool(s.get("timezone")), "settings.timezone"))

    ok = [c for c in checks if c[1]]
    bad = [c for c in checks if not c[1]]
    body = "\n".join(f"{'✅' if good else '⚠️'} {_esc(label)}"
                     + ("" if good else f" — <i>{_esc(hint)}</i>")
                     for label, good, hint in checks)
    tg_admin(f"🚦 <b>Preflight: {len(ok)}/{len(checks)} ready</b>\n\n{body}"
             + ("\n\n<b>Not blocking:</b> Twitter is intentionally dark until you add keys."
                if not twitter_ready()[0] else ""))
    log(f"🚦 preflight: {len(ok)}/{len(checks)} ok, {len(bad)} need attention")


def run_dry(kind: str, topic: str) -> None:
    """DRY RUN — walk a pipeline's real logic end to end, but stub every
    expensive connector call and every publish. This is what turns the
    'fix a bug, wait for the schedule, fail again' loop into a 10-second check:
    the AI still thinks, every key read still happens, nothing gets spent."""
    topic = topic or "Dry run: Nifty ne aaj naya high banaya"
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    log(f"🧪 DRY RUN [{kind}] {topic[:60]}")

    stub_png = TEST_DIR / "dry.png"
    stub_mp4 = TEST_DIR / "dry.mp4"
    stub_pdf = TEST_DIR / "dry.pdf"
    stub_mp3 = TEST_DIR / "dry.mp3"
    stub_png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
        "0557bfabd40000000049454e44ae426082"))
    stub_mp4.write_bytes(b"\\x00\\x00\\x00\\x18ftypmp42" + b"\\x00" * 2048)
    stub_pdf.write_bytes(b"%PDF-1.4\\n% dry run stub\\n")
    stub_mp3.write_bytes(b"ID3" + b"\\x00" * 2048)

    real_bridge, real_approval = bridge_task, request_approval
    real_pubs = {n: globals()[n] for n in
                 ("publish_instagram_image", "publish_instagram_carousel",
                  "publish_instagram_reel", "publish_instagram_story",
                  "publish_youtube_short", "publish_twitter",
                  "tg_channel_file", "tg_channel_text", "tg_channel_poll",
                  "build_news_clip")}
    touched: list[str] = []

    def fake_bridge(task_type, prompt, path=None, **kw):
        # Gemini still runs for real — that is where the schema bugs live.
        if task_type == "gemini":
            return real_bridge(task_type, prompt, path, **kw)
        stub = {"img": stub_png, "video": stub_mp4, "pdf": stub_pdf, "tts": stub_mp3}[task_type]
        target = Path(path) if path else stub
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(stub.read_bytes())
        touched.append(f"bridge:{task_type} (stubbed)")
        return {"saved_to": [str(target)] if task_type == "video" else str(target),
                "chat_code": "dry"}

    def fake_approval(kind_, caption, files, payload=None):
        touched.append(f"approval:{kind_} ({len(files)} file/s)")
        return True, (payload or {})

    def make_stub(name):
        def _stub(*a, **k):
            touched.append(f"publish:{name}")
            return f"dry-{name}"
        return _stub

    globals()["bridge_task"] = fake_bridge
    globals()["request_approval"] = fake_approval
    for n in real_pubs:
        globals()[n] = make_stub(n)
    globals()["build_news_clip"] = lambda img, out, seconds=None, topic="": (
        touched.append("build_news_clip (stubbed)") or str(stub_mp4))
    try:
        {"reel": lambda: run_reel_task(topic, "UGC_VIDEO"),
         "reel_animated": lambda: run_reel_task(topic, "TALKING_OBJECT"),
         "post": lambda: run_post_task(topic, "SINGLE_POST"),
         "carousel": lambda: run_post_task(topic, "CAROUSEL", slides=3),
         "news": lambda: run_post_task("URGENT NEWS POSTER: " + topic, "SINGLE_POST"),
         "pdf": lambda: run_pdf_task(topic),
         "poll": lambda: run_poll_task(topic),
         "ceo": run_ceo,
         "plan": run_content_manager}[kind]()
        steps = "\n".join(f"• {_esc(t)}" for t in touched) or "• (no steps recorded)"
        tg_admin(f"✅ <b>Dry run passed — {_esc(kind)}</b>\n"
                 f"Every key read succeeded. Nothing was generated or published.\n\n{steps}")
        log(f"🧪 DRY RUN [{kind}] PASSED")
    except Exception as exc:
        steps = "\n".join(f"• {_esc(t)}" for t in touched) or "• (failed before the first step)"
        info = explain_error(exc, step=f"dry:{kind}")
        tg_admin(f"❌ <b>Dry run FAILED — {_esc(kind)}</b>\n\n"
                 f"<b>What happened</b>\n{_esc(info['cause'])}\n\n"
                 f"<b>How to fix</b>\n{_esc(info['fix'])}\n\n"
                 f"<b>Got this far</b>\n{steps}\n\n"
                 f"<b>Raw</b>\n<code>{_esc(info['raw'][:300])}</code>")
        log(f"🧪 DRY RUN [{kind}] FAILED: {info['raw']}")
    finally:
        globals()["bridge_task"] = real_bridge
        globals()["request_approval"] = real_approval
        for n, fn in real_pubs.items():
            globals()[n] = fn


# ---------------------------------------------------------------------------
# Bot command inbox (panel actions that must run in THIS process)
# ---------------------------------------------------------------------------

def process_bot_commands() -> None:
    with db() as c:
        rows = c.execute("SELECT * FROM botcmd WHERE status='pending' ORDER BY id").fetchall()
    for r in rows:
        with db() as c:
            c.execute("UPDATE botcmd SET status='running' WHERE id=?", (r["id"],))
        cmd, arg = r["cmd"], r["arg"] or ""
        log(f"⚙️ panel command: {cmd} {arg[:60]}")
        try:
            if cmd == "run_ceo":
                run_ceo()
            elif cmd == "run_plan":
                run_content_manager()
            elif cmd == "run_news":
                run_news_check()
            elif cmd == "run_growth":
                run_growth_scan()
            elif cmd == "text_to_post":
                run_post_task(arg, "SINGLE_POST")
            elif cmd == "make_reel":
                run_reel_task(arg, "TALKING_OBJECT")
            elif cmd == "make_pdf":
                run_pdf_task(arg)
            elif cmd == "make_poll":
                run_poll_task(arg)
            elif cmd == "clone":
                run_clone(json.loads(arg))
            elif cmd == "parse_link":
                run_link_parse(arg)
            elif cmd == "make_carousel":
                run_post_task(arg, "CAROUSEL", slides=3)
            elif cmd == "run_evolve":
                run_prompt_evolution()
            elif cmd == "preflight":
                run_preflight()
            elif cmd == "test_all":
                run_test_all(cheap_only=False)
            elif cmd == "test_cheap":
                run_test_all(cheap_only=True)
            elif cmd == "test_failed":
                run_test_all(only=last_failed_tests())
            elif cmd.startswith("test_pick:"):
                run_test_all(only=cmd.split(":", 1)[1].split(","))
            elif cmd.startswith("test:"):
                name = cmd.split(":", 1)[1]
                if name not in TESTS:
                    raise ValueError(f"unknown test {name}")
                res = run_test(name)
                icon = "✅" if res["ok"] else ("⚪" if res["ok"] is None else "❌")
                msg = f"{icon} <b>Test: {_esc(res['label'])}</b>\n{_esc(str(res.get('detail')))}"
                if res["ok"] is False:
                    msg += (f"\n\n<b>How to fix</b>\n{_esc(res.get('fix', ''))}"
                            f"\n\n<b>Raw</b>\n<code>{_esc(str(res.get('raw', ''))[:300])}</code>")
                tg_admin(msg)
            elif cmd.startswith("dry:"):
                run_dry(cmd.split(":", 1)[1], arg)
            with db() as c:
                c.execute("UPDATE botcmd SET status='done' WHERE id=?", (r["id"],))
        except Exception as exc:
            with db() as c:
                c.execute("UPDATE botcmd SET status='failed' WHERE id=?", (r["id"],))
            report_failure(f"Panel command {cmd}", exc, step=cmd, topic=arg[:120])


def run_clone(spec: dict) -> None:
    """IG Reel Clone / YT Short Clone: yt-dlp download → Gemini fresh caption →
    approval → routing['cloned']."""
    url = spec.get("url", "")
    log(f"🧪 CLONE pipeline: {url}")
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    out = OUT_DIR / f"clone_{stamp}.mp4"
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "-f",
                        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                        "-o", str(out), url],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"yt-dlp failed: {r.stderr[-300:]}")
    cap = ask_gemini(
        "Write a FRESH, original Hinglish Instagram caption (trading niche, trading terms "
        "in English) for a cloned video about this link: " + url +
        "\nOutput STRICT JSON only, no fences: {\"caption\": \"...\", \"hashtags\": \"...\"}")
    caption = cap.get("caption", "") + "\n\n" + cap.get("hashtags", "")
    ok, edited = request_approval("cloned", caption, [str(out)],
                                  {"caption": caption, "topic": url})
    if not ok:
        return
    caption = edited.get("caption", caption)
    routing = store()["routing"]["cloned"]
    if routing.get("instagram_reel"):
        rid = publish_instagram_reel(str(out), caption)
        log_performance("instagram", "cloned", url, rid)
    if routing.get("youtube_short"):
        publish_youtube_short(str(out), caption[:90], caption)
    if routing.get("telegram"):
        tg_channel_file(str(out), caption, "video")


def run_link_parse(url: str) -> None:
    """Web Link Parsing: fetch article text → Gemini summarises → post pipeline."""
    log(f"🔗 LINK PARSE: {url}")
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", r.text, flags=re.S)
    text = re.sub(r"\s+", " ", text)[:6000]
    summ = ask_gemini(
        "Summarise this article into ONE punchy trading-news topic blueprint "
        "(1-2 sentences with the key numbers). STRICT JSON only, no fences: "
        "{\"topic_blueprint\": \"...\"}\n\nARTICLE TEXT:\n" + text)
    run_post_task(summ.get("topic_blueprint", url), "SINGLE_POST")


# ---------------------------------------------------------------------------
# Scheduler main loop
# ---------------------------------------------------------------------------

def execute_task(row: sqlite3.Row) -> None:
    agent, ctype = row["agent"], row["content_type"]
    blueprint = row["blueprint"] or ""
    try:
        if agent == "REEL_MAKER":
            run_reel_task(blueprint, ctype)
        elif agent == "POST_MAKER":
            run_post_task(blueprint, ctype, slides=row["slides"] or 0)
        elif agent == "PDF_MAKER":
            run_pdf_task(blueprint)
        elif agent == "TELEGRAM_POLL":
            run_poll_task(blueprint)
        else:
            raise ValueError(f"unknown agent {agent}")
        with db() as c:
            c.execute("UPDATE tasks SET status='done' WHERE id=?", (row["id"],))
    except Exception as exc:
        log(f"❌ task {row['id']} FAILED: {exc}")
        traceback.print_exc()
        with db() as c:
            c.execute("UPDATE tasks SET status='failed', error=? WHERE id=?",
                      (str(exc)[:500], row["id"]))
        tg_admin(f"❌ <b>Task failed</b> ({agent} / {ctype})\n{blueprint[:150]}\n"
                 f"<code>{str(exc)[:300]}</code>")


def botcmd_worker() -> None:
    """Panel + Test Lab commands run HERE, on their own thread, polled every 2s.

    Previously they ran inside the 30s scheduler tick, so pressing a button meant
    waiting up to 30s and a 10-minute reel froze every other due task. BRIDGE_LOCK
    keeps this thread and the scheduler from using the connector at the same time.
    """
    log("⚡ panel command worker ONLINE (2s poll)")
    while True:
        try:
            process_bot_commands()
        except Exception as exc:
            log(f"⚠️ botcmd worker error: {exc}")
        time.sleep(2)


def scheduler_loop() -> None:
    log("🚀 Stock Warrior orchestrator ONLINE (IST scheduler, 30s tick)")
    tg_admin("🚀 <b>Stock Warrior engine started</b>")
    fired_news_slots: set[str] = set()
    while True:
        try:
            s = store()["settings"]
            now = now_ist()

            # 1) panel commands are handled by botcmd_worker on its own thread

            # 2) due scheduled tasks
            with db() as c:
                due = c.execute(
                    "SELECT * FROM tasks WHERE status='scheduled' AND execution_dt<=? "
                    "ORDER BY urgent DESC, execution_dt LIMIT 3",
                    (now.isoformat(),)).fetchall()
            for row in due:
                with db() as c:
                    c.execute("UPDATE tasks SET status='running' WHERE id=?", (row["id"],))
                execute_task(row)

            # 3) CEO 48h loop
            last_ceo = get_state("last_ceo_run")
            ceo_gap = timedelta(hours=s.get("ceo_loop_hours", 48))
            if (not last_ceo or
                    now - datetime.fromisoformat(last_ceo) >= ceo_gap):
                # Claim the slot BEFORE running — same pattern as the growth
                # scan below. Without this, a failing CEO re-fires every 30s
                # forever and each cycle relaunches Chrome.
                set_state("last_ceo_run", now.isoformat())
                try:
                    run_ceo()
                except Exception as exc:
                    # Back off ~2h rather than losing the full 48h cycle.
                    set_state("last_ceo_run",
                              (now - ceo_gap + timedelta(hours=2)).isoformat())
                    log(f"⚠️ CEO run failed — retrying in ~2h: {exc}")

            # 4) news slots (fire once per slot per day)
            slot_key_prefix = now.strftime("%Y-%m-%d ")
            for slot in s.get("news_schedule", []):
                key = slot_key_prefix + slot
                if key in fired_news_slots:
                    continue
                try:
                    sh, sm = map(int, slot.split(":"))
                except ValueError:
                    continue
                slot_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
                if timedelta(0) <= now - slot_dt < timedelta(minutes=10):
                    fired_news_slots.add(key)
                    run_news_check()
            if len(fired_news_slots) > 50:
                fired_news_slots = {k for k in fired_news_slots
                                    if k.startswith(slot_key_prefix)}

            # 5) growth scans
            last_g = get_state("last_growth_run")
            if (not last_g or
                    now - datetime.fromisoformat(last_g) >=
                    timedelta(hours=s.get("growth_scan_hours", 6))):
                set_state("last_growth_run", now.isoformat())
                run_growth_scan()

            # 6) prompt evolution (30 days)
            last_e = get_state("last_evolution_run")
            if (not last_e or
                    now - datetime.fromisoformat(last_e) >=
                    timedelta(days=s.get("prompt_evolution_days", 30))):
                set_state("last_evolution_run", now.isoformat())
                run_prompt_evolution()

        except KeyboardInterrupt:
            log("bye")
            return
        except Exception as exc:
            log(f"⚠️ scheduler tick error: {exc}")
            traceback.print_exc()
        time.sleep(30)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Stock Warrior orchestrator")
    ap.add_argument("--no-bot", action="store_true",
                    help="don't spawn telegram_bot.py (run it yourself)")
    ap.add_argument("--once", nargs=2, metavar=("KIND", "TOPIC"),
                    help="fire ONE pipeline now: reel|post|pdf|poll|news TOPIC")
    ap.add_argument("--dry", nargs="?", const="post", metavar="KIND",
                    help="dry-run a pipeline (real Gemini, stubbed media + publish): "
                         "reel|reel_animated|post|carousel|news|pdf|poll|ceo|plan")
    ap.add_argument("--topic", default="", help="topic to use with --dry")
    ap.add_argument("--test", metavar="NAME",
                    help="one Test Lab check, a comma-separated list, or "
                         "'all' / 'cheap' / 'failed'")
    ap.add_argument("--preflight", action="store_true",
                    help="config readiness report, spends nothing")
    ap.add_argument("--reset", action="store_true",
                    help="kill orphans, free the Telegram poll, unstick the DB. "
                         "Use this instead of deleting warrior.db")
    args = ap.parse_args()

    init_db()

    if args.reset:
        full_reset()
        return

    # ---- BOOT RECOVERY -----------------------------------------------------
    # Order matters: kill leftovers, free the Telegram poll, then unstick the DB.
    # After this a restart always works, whatever way the last run was killed.
    kill_orphans()
    release_telegram_poll()
    recover_state()
    signal.signal(signal.SIGINT, lambda *a: (shutdown_children(), sys.exit(0)))
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGTERM, lambda *a: (shutdown_children(), sys.exit(0)))
    atexit.register(shutdown_children)

    st = bridge_status()
    if st.get("ok") and not st.get("extension_live"):
        # A bridge left running by a dead session usually has no live Chrome
        # behind it. Restart it rather than waiting forever on a ghost.
        log("🌐 bridge is up but no extension behind it — restarting it")
        with contextlib.suppress(Exception):
            requests.post(f"{BRIDGE}/shutdown", timeout=5)
        time.sleep(2)
        st = bridge_status()
    if not st.get("ok"):
        bridge_file = BASE_DIR / "browser_bridge.py"
        log("🌐 browser_bridge.py offline. Spawning it natively in the background...")
        proc = subprocess.Popen([sys.executable, str(bridge_file), "--no-menu"],
                                stdin=subprocess.DEVNULL)
        _record_child(proc, "browser_bridge")
        time.sleep(5)  # Wait for Flask and Chrome to boot
    if not st.get("extension_live"):
        print("⚠️ bridge is up but the Chrome extension has not polled yet —")
        print("   it will connect as soon as the automation Chrome finishes loading.")

    if args.preflight:
        run_preflight()
        return

    if args.test:
        if args.test == "all":
            run_test_all(cheap_only=False)
        elif args.test == "cheap":
            run_test_all(cheap_only=True)
        elif args.test == "failed":
            run_test_all(only=last_failed_tests())
        elif "," in args.test:
            run_test_all(only=args.test.split(","))
        else:
            res = run_test(args.test)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    if args.dry:
        run_dry(args.dry, args.topic)
        return

    if args.once:
        kind, topic = args.once
        {"reel": lambda: run_reel_task(topic, "TALKING_OBJECT"),
         "post": lambda: run_post_task(topic, "SINGLE_POST"),
         "pdf": lambda: run_pdf_task(topic),
         "poll": lambda: run_poll_task(topic),
         "news": lambda: run_news_check()}[kind]()
        return

    if not args.no_bot:
        bot_file = BASE_DIR / "telegram_bot.py"
        if TG_TOKEN and bot_file.exists():
            _record_child(subprocess.Popen([sys.executable, str(bot_file)]),
                          "telegram_bot")
            log("🤖 telegram_bot.py spawned")
        else:
            log("⚠️ telegram_bot.py not started (missing token or file)")

    threading.Thread(target=botcmd_worker, daemon=True).start()
    scheduler_loop()


if __name__ == "__main__":
    main()
