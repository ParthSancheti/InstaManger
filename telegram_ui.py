"""
telegram_ui.py — Upgraded control-panel surface for Stock Warrior.

A self-contained aiogram Router that the main telegram_bot.py attaches. It adds
what the old panel was missing, without touching the existing handlers:

  • 📆 Schedule viewer   — see every upcoming task, grouped by day
  • ⚙️ Friendly settings — typed editors (toggles/pickers), no raw JSON
  • 🔑 Tokens & Keys     — Meta / YouTube / Instagram / Reddit, writes to .env
  • ❓ Help              — plain-language guide to every button

Design rules followed here:
  - Every value the user can change is either a toggle (🟢/🔴), a pick-list,
    or a single guided text prompt with an example — never "send me JSON".
  - Every screen has a Back button and explains what it does in one line.
  - Tokens are stored to .env and shown masked; never echoed in full.

It reads the SAME store() / save_store() / warrior.db as the rest of the bot,
passed in via init() so there's a single source of truth.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
import os
import sys

from contextlib import closing

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()

# Wired up by init() so this module shares the bot's real state + db.
_store = None
_save = None
_db = None
_env_path: Path | None = None
_admin_id: int = 0


def init(store_fn, save_fn, db_fn, env_path: Path, admin_id: int) -> Router:
    """Called once from telegram_bot.py. Returns the router to include."""
    global _store, _save, _db, _env_path, _admin_id
    _store, _save, _db, _env_path, _admin_id = store_fn, save_fn, db_fn, env_path, admin_id
    return router


def _guard(obj) -> bool:
    uid = obj.from_user.id if obj.from_user else 0
    return uid == _admin_id


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


# ===========================================================================
#  SETTINGS — friendly, typed. Each setting declares HOW it is edited.
# ===========================================================================
# kind: "toggle" (bool) | "choice" (fixed options) | "int" | "time_list" | "text_list"
SETTING_SPECS = {
    "auto_publish_on_timeout": {
        "label": "Auto-publish on timeout",
        "help": "If ON, a card with no decision auto-posts when the timer ends. OFF = it waits for you.",
        "kind": "toggle"},
    "healer_auto_apply": {
        "label": "Auto-apply healer fixes",
        "help": "If ON, selector fixes apply automatically. Recommend OFF — review them first.",
        "kind": "toggle"},
    "twitter_enabled": {
        "label": "Twitter/X posting",
        "help": "Enable the X publishing path (needs a token in Tokens).",
        "kind": "toggle"},
    "approval_timeout_seconds": {
        "label": "Approval timer",
        "help": "Seconds a card waits before the auto-decision.",
        "kind": "choice", "options": [30, 60, 120, 300, 600],
        "fmt": lambda v: f"{v}s"},
    "max_daily_tasks": {
        "label": "Max tasks per day",
        "help": "Hard cap on how many posts get scheduled for any single day.",
        "kind": "choice", "options": [2, 4, 6, 8, 10, 12]},
    "ceo_loop_hours": {
        "label": "CEO review interval",
        "help": "How often the Brand CEO re-plans strategy.",
        "kind": "choice", "options": [24, 48, 72, 168],
        "fmt": lambda v: f"{v}h ({v // 24}d)" if v >= 24 else f"{v}h"},
    "growth_scan_hours": {
        "label": "Growth scan interval",
        "help": "How often the Growth Hacker scans for reply opportunities.",
        "kind": "choice", "options": [3, 6, 12, 24],
        "fmt": lambda v: f"{v}h"},
    "prompt_evolution_days": {
        "label": "Prompt evolution interval",
        "help": "How often prompts are reviewed against real performance.",
        "kind": "choice", "options": [7, 14, 30, 60],
        "fmt": lambda v: f"{v}d"},
    "reel_scene_count": {
        "label": "Reel scene count",
        "help": "How many scenes each reel is built from.",
        "kind": "choice", "options": [1, 2, 3, 4]},
    "news_schedule": {
        "label": "News post times (IST)",
        "help": "Daily times the news pipeline fires. Send times like: 09:30, 13:30, 18:30",
        "kind": "time_list"},
    "subreddits": {
        "label": "Growth subreddits",
        "help": "Subreddits the Growth Hacker scans. Send names comma-separated, no r/.",
        "kind": "text_list"},
}


class UIStates(StatesGroup):
    edit_int = State()
    edit_time_list = State()
    edit_text_list = State()
    edit_token = State()


@router.callback_query(F.data == "ui_settings")
async def settings_home(q: CallbackQuery):
    if not _guard(q):
        return await q.answer()
    s = _store()["settings"]
    rows = []
    for key, spec in SETTING_SPECS.items():
        val = s.get(key)
        if spec["kind"] == "toggle":
            badge = "🟢 ON" if val else "🔴 OFF"
        elif spec["kind"] in ("time_list", "text_list"):
            badge = f"{len(val or [])} set"
        else:
            fmt = spec.get("fmt", str)
            badge = fmt(val)
        rows.append([(f"{spec['label']}  ·  {badge}", f"ui_set:{key}")])
    rows.append([("❓ What do these mean?", "ui_set_help"), ("⬅️ Back", "home")])
    await q.message.edit_text(
        "⚙️ <b>Settings</b>\n"
        "Tap any row to change it. Toggles flip instantly; the rest guide you.",
        parse_mode="HTML", reply_markup=kb(rows))
    await q.answer()


@router.callback_query(F.data == "ui_set_help")
async def settings_help(q: CallbackQuery):
    if not _guard(q):
        return await q.answer()
    lines = ["⚙️ <b>What each setting does</b>\n"]
    for spec in SETTING_SPECS.values():
        lines.append(f"• <b>{spec['label']}</b> — {spec['help']}")
    await q.message.edit_text("\n".join(lines), parse_mode="HTML",
                              reply_markup=kb([[("⬅️ Back", "ui_settings")]]))
    await q.answer()


@router.callback_query(F.data.startswith("ui_set:"))
async def setting_edit(q: CallbackQuery, state: FSMContext):
    if not _guard(q):
        return await q.answer()
    key = q.data.split(":", 1)[1]
    spec = SETTING_SPECS[key]
    s = _store()["settings"]

    # Toggle: flip and redraw, no prompt.
    if spec["kind"] == "toggle":
        d = _store()
        d["settings"][key] = not d["settings"].get(key)
        _save(d)
        await q.answer(f"{spec['label']}: {'ON' if d['settings'][key] else 'OFF'}")
        return await settings_home(q)

    # Choice: show the options as buttons.
    if spec["kind"] == "choice":
        cur = s.get(key)
        fmt = spec.get("fmt", str)
        rows, line = [], []
        for opt in spec["options"]:
            mark = "✅ " if opt == cur else ""
            line.append((f"{mark}{fmt(opt)}", f"ui_pick:{key}:{opt}"))
            if len(line) == 3:
                rows.append(line); line = []
        if line:
            rows.append(line)
        rows.append([("⬅️ Back", "ui_settings")])
        await q.message.edit_text(f"⚙️ <b>{spec['label']}</b>\n{spec['help']}\n\nPick a value:",
                                  parse_mode="HTML", reply_markup=kb(rows))
        return await q.answer()

    # Text-driven kinds: prompt once, with an example.
    st = {"time_list": UIStates.edit_time_list,
          "text_list": UIStates.edit_text_list}[spec["kind"]]
    await state.set_state(st)
    await state.update_data(key=key)
    cur = ", ".join(str(x) for x in (s.get(key) or []))
    await q.message.answer(
        f"⚙️ <b>{spec['label']}</b>\n{spec['help']}\n\n"
        f"Current: <code>{cur or '(empty)'}</code>\n\nSend the new value:",
        parse_mode="HTML")
    await q.answer()


@router.callback_query(F.data.startswith("ui_pick:"))
async def setting_pick(q: CallbackQuery):
    if not _guard(q):
        return await q.answer()
    _, key, raw = q.data.split(":", 2)
    d = _store()
    d["settings"][key] = int(raw)
    _save(d)
    await q.answer(f"{SETTING_SPECS[key]['label']} set")
    # Redraw the choice screen so the ✅ moves.
    q.data = f"ui_set:{key}"
    await setting_edit(q, None)  # state unused for choice kind


@router.message(UIStates.edit_time_list)
async def save_time_list(m: Message, state: FSMContext):
    if not _guard(m):
        return
    key = (await state.get_data())["key"]
    parts = [p.strip() for p in m.text.replace(";", ",").split(",") if p.strip()]
    good = []
    for p in parts:
        try:
            hh, mm = p.split(":")
            h, mi = int(hh), int(mm)
            assert 0 <= h < 24 and 0 <= mi < 60
            good.append(f"{h:02d}:{mi:02d}")
        except Exception:
            await state.clear()
            return await m.answer(f"❌ <code>{p}</code> isn't a valid HH:MM time. "
                                  "Nothing changed — try the button again.", parse_mode="HTML")
    d = _store(); d["settings"][key] = good; _save(d)
    await state.clear()
    await m.answer(f"✅ {SETTING_SPECS[key]['label']} → <code>{', '.join(good)}</code>\n"
                   "Applies on the next tick.", parse_mode="HTML")


@router.message(UIStates.edit_text_list)
async def save_text_list(m: Message, state: FSMContext):
    if not _guard(m):
        return
    key = (await state.get_data())["key"]
    items = [p.strip().lstrip("r/").strip() for p in m.text.replace(";", ",").split(",") if p.strip()]
    d = _store(); d["settings"][key] = items; _save(d)
    await state.clear()
    await m.answer(f"✅ {SETTING_SPECS[key]['label']} → {len(items)} entries.\n"
                   f"<code>{', '.join(items)}</code>", parse_mode="HTML")


# ===========================================================================
#  SCHEDULE VIEWER — upcoming + recent tasks, grouped by day
# ===========================================================================
_AGENT_ICON = {"REEL_MAKER": "🎬", "POST_MAKER": "🖼", "PDF_MAKER": "📄",
               "TELEGRAM_POLL": "📊"}


@router.callback_query(F.data == "ui_schedule")
async def schedule_view(q: CallbackQuery):
    if not _guard(q):
        return await q.answer()
    with closing(_db()) as c, c:
        upcoming = c.execute(
            "SELECT * FROM tasks WHERE status='scheduled' "
            "ORDER BY execution_dt LIMIT 25").fetchall()
        running = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='running'").fetchone()["n"]
        done = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='done'").fetchone()["n"]
        failed = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='failed'").fetchone()["n"]

    if not upcoming:
        body = ("📆 <b>Schedule</b>\n\nNo upcoming tasks queued.\n"
                "The Content Manager builds a 7-day plan after each CEO review — "
                "run one from Status if you want to fill the calendar now.")
    else:
        by_day: dict[str, list] = {}
        for t in upcoming:
            try:
                dt = datetime.fromisoformat(t["execution_dt"])
            except Exception:
                continue
            day = dt.strftime("%a %d %b")
            when = dt.strftime("%H:%M")
            icon = _AGENT_ICON.get(t["agent"], "•")
            urgent = "🚨 " if t["urgent"] else ""
            bp = (t["blueprint"] or "")[:48]
            by_day.setdefault(day, []).append(f"  {when}  {icon} {urgent}{bp}")
        chunks = [f"📆 <b>Upcoming — next {len(upcoming)} tasks</b>\n"]
        for day, items in by_day.items():
            chunks.append(f"\n<b>{day}</b>")
            chunks.extend(items)
        chunks.append(f"\n\n<i>running {running} · done {done} · failed {failed}</i>")
        body = "\n".join(chunks)

    await q.message.edit_text(
        body, parse_mode="HTML",
        reply_markup=kb([[("🔄 Refresh", "ui_schedule")],
                         [("⚠️ Failed tasks", "ui_failed"), ("⬅️ Back", "home")]]))
    await q.answer()


@router.callback_query(F.data == "ui_failed")
async def failed_view(q: CallbackQuery):
    if not _guard(q):
        return await q.answer()
    with closing(_db()) as c, c:
        rows = c.execute("SELECT * FROM tasks WHERE status='failed' "
                         "ORDER BY execution_dt DESC LIMIT 10").fetchall()
    if not rows:
        body = "✅ <b>No failed tasks.</b>"
    else:
        lines = ["⚠️ <b>Recent failures</b>\n"]
        for t in rows:
            icon = _AGENT_ICON.get(t["agent"], "•")
            lines.append(f"{icon} {(t['blueprint'] or '')[:40]}\n   <code>{(t['error'] or '')[:80]}</code>")
        body = "\n".join(lines)
    await q.message.edit_text(body, parse_mode="HTML",
                              reply_markup=kb([[("⬅️ Back", "ui_schedule")]]))
    await q.answer()


# ===========================================================================
#  TOKENS & KEYS — writes to .env (upsert, never clobbers existing keys)
# ===========================================================================
TOKEN_SPECS = {
    "META_ACCESS_TOKEN": {
        "label": "Meta (IG/FB) access token",
        "help": "Graph API token for Instagram + Facebook publishing. From Meta for Developers.",
        "icon": "📸"},
    "IG_BUSINESS_ID": {
        "label": "Instagram business ID",
        "help": "Numeric IG business account ID the token posts to.",
        "icon": "📸"},
    "YT_CHANNEL_ID": {
        "label": "YouTube channel ID",
        "help": "Target channel for Shorts. (OAuth client_secret.json still lives as a file.)",
        "icon": "▶️"},
    "IG_SESSIONID": {
        "label": "Instagram sessionid cookie",
        "help": "Used by the downloader for reel cloning. From your logged-in IG cookies.",
        "icon": "🍪"},
    "REDDIT_CLIENT_ID": {
        "label": "Reddit client ID",
        "help": "From reddit.com/prefs/apps (script type). Needed for growth scans.",
        "icon": "🟠"},
    "REDDIT_CLIENT_SECRET": {
        "label": "Reddit client secret",
        "help": "The secret paired with the client ID above.",
        "icon": "🟠"},
    "TWITTER_BEARER_TOKEN": {
        "label": "Twitter/X bearer token (READ ONLY)",
        "help": "App-only auth. Used for trend capture. It CANNOT post — posting "
                "needs the four OAuth 1.0a keys below.",
        "icon": "🐦"},
    "TWITTER_API_KEY": {
        "label": "Twitter/X API key",
        "help": "OAuth 1.0a consumer key. Needed to POST. From the X developer portal.",
        "icon": "🐦"},
    "TWITTER_API_SECRET": {
        "label": "Twitter/X API secret",
        "help": "OAuth 1.0a consumer secret.",
        "icon": "🐦"},
    "TWITTER_ACCESS_TOKEN": {
        "label": "Twitter/X access token",
        "help": "OAuth 1.0a user access token, with Read+Write permission.",
        "icon": "🐦"},
    "TWITTER_ACCESS_SECRET": {
        "label": "Twitter/X access token secret",
        "help": "Paired secret. Once all four are set, turn on settings.twitter_enabled.",
        "icon": "🐦"},
}


def _read_env() -> dict:
    out: dict[str, str] = {}
    if _env_path and _env_path.exists():
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _write_env(key: str, value: str) -> None:
    """Upsert one key in .env, preserving everything else + comments."""
    lines = []
    found = False
    if _env_path and _env_path.exists():
        lines = _env_path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                found = True
                break
    if not found:
        lines.append(f"{key}={value}")
    _env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mask(v: str) -> str:
    if not v:
        return "— not set —"
    if len(v) <= 8:
        return "•" * len(v)
    return v[:4] + "…" + v[-4:]


@router.callback_query(F.data == "ui_tokens")
async def tokens_home(q: CallbackQuery):
    if not _guard(q):
        return await q.answer()
    env = _read_env()
    rows = []
    for key, spec in TOKEN_SPECS.items():
        set_badge = "🟢" if env.get(key) else "🔴"
        rows.append([(f"{set_badge} {spec['icon']} {spec['label']}", f"ui_tok:{key}")])
    rows.append([("⬅️ Back", "home")])
    await q.message.edit_text(
        "🔑 <b>Tokens &amp; Keys</b>\n"
        "🟢 = set · 🔴 = missing. Tap to add or replace. Values are stored in "
        "your <code>.env</code> and shown masked.\n"
        "<i>Changes to tokens apply when the affected process next restarts.</i>",
        parse_mode="HTML", reply_markup=kb(rows))
    await q.answer()


@router.callback_query(F.data.startswith("ui_tok:"))
async def token_edit(q: CallbackQuery, state: FSMContext):
    if not _guard(q):
        return await q.answer()
    key = q.data.split(":", 1)[1]
    spec = TOKEN_SPECS[key]
    env = _read_env()
    await state.set_state(UIStates.edit_token)
    await state.update_data(key=key)
    await q.message.answer(
        f"{spec['icon']} <b>{spec['label']}</b>\n{spec['help']}\n\n"
        f"Current: <code>{_mask(env.get(key, ''))}</code>\n\n"
        "Send the new value, or /cancel to keep it.",
        parse_mode="HTML")
    await q.answer()


@router.message(UIStates.edit_token)
async def token_save(m: Message, state: FSMContext):
    if not _guard(m):
        return
    if (m.text or "").strip().lower() == "/cancel":
        await state.clear()
        return await m.answer("Kept as-is.")
    key = (await state.get_data())["key"]
    _write_env(key, (m.text or "").strip())
    await state.clear()
    # Delete the message so the raw secret doesn't linger in chat history.
    try:
        await m.delete()
    except Exception:
        pass
    await m.answer(f"✅ <b>{TOKEN_SPECS[key]['label']}</b> saved to .env "
                   "(your message was deleted for safety).\n"
                   "Restart the engine for it to take effect.", parse_mode="HTML")


# ===========================================================================
#  HELP
# ===========================================================================
@router.callback_query(F.data == "ui_help")
async def help_view(q: CallbackQuery):
    if not _guard(q):
        return await q.answer()
    await q.message.edit_text(
        "❓ <b>Guide</b>\n\n"
        "📥 <b>Approvals</b> — pending cards waiting on you.\n"
        "📆 <b>Schedule</b> — every upcoming task, grouped by day.\n"
        "⚡ <b>Action Center</b> — generate a post/reel/pdf/poll on demand.\n"
        "⚙️ <b>Settings</b> — timers, intervals, toggles. Plain-language.\n"
        "🔑 <b>Tokens</b> — API keys/cookies, stored in .env, shown masked.\n"
        "🎨 <b>Brand</b> — name, niche, audience, handles.\n"
        "🔀 <b>Routing</b> — which platforms each content type posts to.\n"
        "🎵 <b>Music</b> — reel background tracks.\n"
        "🧪 <b>Connector</b> — test the Gemini bridge / restart Chrome.\n"
        "📊 <b>Status</b> — health + manual run triggers.\n\n"
        "Slash: /setup /healer /evolve /update",
        parse_mode="HTML", reply_markup=kb([[("⬅️ Back", "home")]]))
    await q.answer()


# ===========================================================================
#  UPDATE
# ===========================================================================
@router.message(F.text == "/update")
async def cmd_update(m: Message):
    if not _guard(m):
        return
    await m.answer("🔄 <b>Auto-Update triggered.</b>\nShutting down and pulling latest code...", parse_mode="HTML")
    
    # Spawn the batch script completely detached
    if os.name == "nt":
        subprocess.Popen(["cmd.exe", "/c", "start", "update.bat"], creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(["bash", "update.sh"], start_new_session=True)
        
    # Commit suicide so files can be replaced
    sys.exit(0)
