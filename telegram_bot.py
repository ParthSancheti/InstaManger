#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STOCK WARRIOR — telegram_bot.py  (the Unified Manager)
=======================================================
Your entire control panel inside Telegram. Self-contained on purpose
(own .env / prompts_store.json / warrior.db access — zero imports from
main_orchestrator.py, so no circular-import hell). The two processes meet
in SQLite:

    approvals  → orchestrator inserts, WE send the card + run the
                 1-minute auto-post timeout, then write the decision back
    botcmd     → panel actions the orchestrator must execute
                 (Text→Post, clones, run CEO now, …)
    healer     → Lifesaver suggestions waiting for your review
    evolution  → 30-day prompt-evolution proposals (manual gate)

Panel map (/start):
    📥 Approvals     — anything pending right now
    ⚡ Action Center — Text→Post • Make Reel • Make PDF • Poll •
                       IG Reel Clone • YT Short Clone • Parse Web Link
    🧪 Connector     — bridge status • quick Gemini test • RESTART CHROME
    ⚙️ Settings      — every settings.* field, live-editable
    🎨 Brand         — name / niche / audience / links / voice
    🔀 Routing       — per-content-type platform matrix toggles
    🎵 Music         — list / add reel background music (send me an mp3)
    🛠 /healer       — selector fixes waiting for review
    🧬 /evolve       — prompt evolution proposals
    🧙 /setup        — FULL NICHE TRANSMUTATION wizard

Run:  python telegram_bot.py     (main_orchestrator.py also auto-spawns me)
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests as rq
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.exceptions import TelegramBadRequest

BASE_DIR = Path(__file__).resolve().parent
STORE_PATH = BASE_DIR / "prompts_store.json"
DB_PATH = BASE_DIR / "warrior.db"
MUSIC_DIR = BASE_DIR / "music"
BRIDGE = "http://127.0.0.1:5000"
MUSIC_DIR.mkdir(exist_ok=True)


def load_env(path: Path = BASE_DIR / ".env") -> dict:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")})
    return env


ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
ADMIN = int(ENV.get("TELEGRAM_ADMIN_ID", "0") or 0)

if not TOKEN or not ADMIN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID in .env first.")


def store() -> dict:
    return json.loads(STORE_PATH.read_text(encoding="utf-8"))


def save_store(d: dict) -> None:
    STORE_PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def get_state(key: str, default: str = "") -> str:
    """Read the orchestrator's state table (shared warrior.db)."""
    with db() as c:
        row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def queue_cmd(cmd: str, arg: str = "") -> None:
    with db() as c:
        c.execute("INSERT INTO botcmd(dt,cmd,arg) VALUES(?,?,?)",
                  (datetime.now().isoformat(), cmd, arg))


def bridge_get(path: str) -> dict:
    try:
        return rq.get(f"{BRIDGE}{path}", timeout=8).json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def bridge_post(path: str, body: dict | None = None, timeout: int = 120) -> dict:
    try:
        return rq.post(f"{BRIDGE}{path}", json=body or {}, timeout=timeout).json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


bot = Bot(TOKEN)
dp = Dispatcher()
r = Router()
dp.include_router(r)

# Upgraded UI surface (friendly settings, schedule viewer, tokens, help).
import telegram_ui
dp.include_router(telegram_ui.init(store, save_store, db,
                                   BASE_DIR / ".env", ADMIN))

# Test Lab: run every step and every workflow on demand, no schedule waiting.
import telegram_testlab
dp.include_router(telegram_testlab.init(queue_cmd, ADMIN, get_state))


def admin_only(obj) -> bool:
    uid = obj.from_user.id if obj.from_user else 0
    return uid == ADMIN


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class Ask(StatesGroup):
    text_to_post = State()
    make_reel = State()
    make_pdf = State()
    make_poll = State()
    clone_url = State()
    parse_link = State()
    edit_caption = State()          # data: approval_id
    edit_brand = State()            # data: brand_key
    setup_niche = State()
    setup_brand = State()
    setup_audience = State()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

MAIN_KB = kb([
    [("📥 Approvals", "approvals"), ("📆 Schedule", "ui_schedule")],
    [("⚡ Action Center", "actions"), ("📊 Status", "status")],
    [("🧪 Test Lab", "tl_home")],
    [("⚙️ Settings", "ui_settings"), ("🔑 Tokens", "ui_tokens")],
    [("🎨 Brand", "brand"), ("🔀 Routing", "routing")],
    [("🎵 Music", "music"), ("🧪 Connector", "connector")],
    [("❓ Help", "ui_help")],
])


@r.message(CommandStart())
async def cmd_start(m: Message):
    if not admin_only(m):
        return await m.answer("Private engine. 🐺")
    b = store()["brand"]
    await m.answer(
        f"🐺 <b>{b['name']} — Unified Manager</b>\n"
        f"Niche: {b['niche']}\n\n"
        "Everything runs itself. This panel is your precision override.\n"
        "Extra commands: /setup /healer /evolve",
        parse_mode="HTML", reply_markup=MAIN_KB)


@r.callback_query(F.data == "home")
async def cb_home(q: CallbackQuery):
    await q.message.edit_text("🐺 <b>Unified Manager</b>", parse_mode="HTML",
                              reply_markup=MAIN_KB)
    await q.answer()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@r.callback_query(F.data == "status")
async def cb_status(q: CallbackQuery):
    st = bridge_get("/status")
    with db() as c:
        sched = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='scheduled'").fetchone()["n"]
        done = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='done'").fetchone()["n"]
        failed = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='failed'").fetchone()["n"]
        pend = c.execute("SELECT COUNT(*) n FROM approvals WHERE status='pending'").fetchone()["n"]
        rows = c.execute("SELECT key,value FROM state WHERE key LIKE 'last_%'").fetchall()
    lasts = "\n".join(f"  {r_['key'][5:]}: {r_['value'][:16]}" for r_ in rows) or "  (none yet)"
    await q.message.edit_text(
        "📊 <b>Engine status</b>\n\n"
        f"Bridge: {'🟢' if st.get('ok') else '🔴'}   "
        f"Extension: {'🟢' if st.get('extension_live') else '🔴'}   "
        f"Chrome: {'🟢' if st.get('chrome_launched') else '🔴'}\n"
        f"Tasks — scheduled: {sched}, done: {done}, failed: {failed}\n"
        f"Approvals pending: {pend}\n\nLast runs:\n{lasts}",
        parse_mode="HTML",
        reply_markup=kb([[("👑 Run CEO now", "run_ceo"), ("📅 Rebuild plan", "run_plan")],
                         [("📰 News run", "run_news"), ("🌱 Growth scan", "run_growth")],
                         [("⬅️ Back", "home")]]))
    await q.answer()


@r.callback_query(F.data.in_({"run_ceo", "run_plan", "run_news", "run_growth"}))
async def cb_run(q: CallbackQuery):
    queue_cmd(q.data)
    await q.answer("Queued — the orchestrator picks it up within 30s.", show_alert=True)


# ---------------------------------------------------------------------------
# Action Center (Manual)
# ---------------------------------------------------------------------------

@r.callback_query(F.data == "actions")
async def cb_actions(q: CallbackQuery):
    await q.message.edit_text(
        "⚡ <b>Manual Action Center</b>\nAll of these run through the full "
        "pipeline including the approval card.",
        parse_mode="HTML",
        reply_markup=kb([
            [("📝 Text → Post", "act_post"), ("🎬 Make Reel", "act_reel")],
            [("📄 Make PDF", "act_pdf"), ("📊 Poll", "act_poll")],
            [("📸 IG Reel Clone", "act_clone_ig"), ("▶️ YT Short Clone", "act_clone_yt")],
            [("🔗 Parse Web Link", "act_link")],
            [("⬅️ Back", "home")]]))
    await q.answer()


ACTION_PROMPTS = {
    "act_post": (Ask.text_to_post, "Send me the TOPIC / text for the post:"),
    "act_reel": (Ask.make_reel, "Send me the TOPIC for the reel:"),
    "act_pdf": (Ask.make_pdf, "Send me the TOPIC for the PDF guide:"),
    "act_poll": (Ask.make_poll, "Send me the TOPIC for the community poll:"),
    "act_clone_ig": (Ask.clone_url, "Send me the Instagram Reel URL to clone:"),
    "act_clone_yt": (Ask.clone_url, "Send me the YouTube Short URL to clone:"),
    "act_link": (Ask.parse_link, "Send me the article URL to turn into a post:"),
}


@r.callback_query(F.data.in_(set(ACTION_PROMPTS)))
async def cb_action(q: CallbackQuery, state: FSMContext):
    st, prompt = ACTION_PROMPTS[q.data]
    await state.set_state(st)
    await q.message.answer(prompt)
    await q.answer()


@r.message(Ask.text_to_post)
async def st_post(m: Message, state: FSMContext):
    queue_cmd("text_to_post", m.text or "")
    await state.clear()
    await m.answer("📝 Queued. The approval card will arrive when the banner is ready.")


@r.message(Ask.make_reel)
async def st_reel(m: Message, state: FSMContext):
    queue_cmd("make_reel", m.text or "")
    await state.clear()
    await m.answer("🎬 Queued — video generation takes several minutes.")


@r.message(Ask.make_pdf)
async def st_pdf(m: Message, state: FSMContext):
    queue_cmd("make_pdf", m.text or "")
    await state.clear()
    await m.answer("📄 Queued.")


@r.message(Ask.make_poll)
async def st_poll(m: Message, state: FSMContext):
    queue_cmd("make_poll", m.text or "")
    await state.clear()
    await m.answer("📊 Queued.")


@r.message(Ask.clone_url)
async def st_clone(m: Message, state: FSMContext):
    queue_cmd("clone", json.dumps({"url": (m.text or "").strip()}))
    await state.clear()
    await m.answer("🧪 Clone queued: download → fresh caption → approval → routing['cloned'].")


@r.message(Ask.parse_link)
async def st_link(m: Message, state: FSMContext):
    queue_cmd("parse_link", (m.text or "").strip())
    await state.clear()
    await m.answer("🔗 Queued — I'll parse the article and build a post from it.")


# ---------------------------------------------------------------------------
# Connector panel
# ---------------------------------------------------------------------------

@r.callback_query(F.data == "connector")
async def cb_connector(q: CallbackQuery):
    st = bridge_get("/status")
    await q.message.edit_text(
        "🧪 <b>Connector</b>\n\n"
        f"Bridge: {'🟢 up' if st.get('ok') else '🔴 DOWN — start browser_bridge.py'}\n"
        f"Extension: {'🟢 live' if st.get('extension_live') else '🔴 not polling'}\n"
        f"Poll age: {st.get('last_poll_age_s')}s   Queue: {st.get('pending_tasks')}",
        parse_mode="HTML",
        reply_markup=kb([[("⚡ Quick Gemini test", "test_gemini")],
                         [("🔄 RESTART CHROME", "restart_chrome")],
                         [("⬅️ Back", "home")]]))
    await q.answer()


@r.callback_query(F.data == "test_gemini")
async def cb_test_gemini(q: CallbackQuery):
    await q.answer("Running… (up to 2 min)")
    msg = await q.message.answer("⏳ Gemini echo test…")

    def _run():
        j = bridge_post("/queue-task", {"type": "gemini",
                                        "prompt": "Reply with exactly: CONNECTOR OK"})
        if not j.get("ok"):
            return f"queue failed: {j.get('error')}"
        tid = j["id"]
        end = time.time() + 150
        while time.time() < end:
            jr = bridge_get(f"/result/{tid}")
            if jr.get("done"):
                res = jr["result"]
                return (res.get("data", {}).get("text", "")[:200] if res.get("ok")
                        else f"task failed: {res.get('error')}")
            time.sleep(3)
        return "timed out"

    out = await asyncio.to_thread(_run)
    await msg.edit_text(f"🧪 Gemini says:\n<code>{out}</code>", parse_mode="HTML")


@r.callback_query(F.data == "restart_chrome")
async def cb_restart(q: CallbackQuery):
    await q.answer("Restarting Chrome…")
    j = await asyncio.to_thread(bridge_post, "/restart-chrome", {}, 150)
    await q.message.answer("🔄 Chrome restart: "
                           + ("✅ up again" if j.get("ok") else f"❌ {j.get('error')}"))


# ---------------------------------------------------------------------------
# Settings / Brand / Routing editors
# ---------------------------------------------------------------------------

BRAND_KEYS = ["name", "niche", "target_audience", "telegram_channel_link",
              "instagram_handle", "voice_name", "disclaimer"]


@r.callback_query(F.data == "brand")
async def cb_brand(q: CallbackQuery):
    b = store()["brand"]
    rows = [[(f"{k}: {str(b.get(k))[:30]}", f"brand:{k}")] for k in BRAND_KEYS]
    rows.append([("⬅️ Back", "home")])
    await q.message.edit_text("🎨 <b>Brand editor</b> — tap to change:",
                              parse_mode="HTML", reply_markup=kb(rows))
    await q.answer()


@r.callback_query(F.data.startswith("brand:"))
async def cb_brand_edit(q: CallbackQuery, state: FSMContext):
    key = q.data[6:]
    await state.set_state(Ask.edit_brand)
    await state.update_data(key=key)
    await q.message.answer(f"Send the new value for <code>{key}</code>:", parse_mode="HTML")
    await q.answer()


@r.message(Ask.edit_brand)
async def st_brand(m: Message, state: FSMContext):
    data = await state.get_data()
    d = store()
    d["brand"][data["key"]] = m.text.strip()
    save_store(d)
    await state.clear()
    await m.answer("✅ Brand updated.")


@r.callback_query(F.data == "routing")
async def cb_routing(q: CallbackQuery):
    rt = store()["routing"]
    rows = []
    for kind, plats in rt.items():
        for plat, on in plats.items():
            rows.append([(f"{'🟢' if on else '🔴'} {kind} → {plat}", f"rt:{kind}:{plat}")])
    rows.append([("⬅️ Back", "home")])
    await q.message.edit_text("🔀 <b>Platform routing matrix</b> — tap to toggle:",
                              parse_mode="HTML", reply_markup=kb(rows))
    await q.answer()


@r.callback_query(F.data.startswith("rt:"))
async def cb_rt_toggle(q: CallbackQuery):
    _, kind, plat = q.data.split(":")
    d = store()
    d["routing"][kind][plat] = not d["routing"][kind][plat]
    save_store(d)
    await cb_routing(q)


# ---------------------------------------------------------------------------
# Music manager
# ---------------------------------------------------------------------------

@r.callback_query(F.data == "music")
async def cb_music(q: CallbackQuery):
    news_dir = MUSIC_DIR / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(f.name for f in news_dir.glob("*.mp3"))
    lib = store().get("music_library", [])
    listing = "\n".join(f"{'🎵' if f in lib else '▫️'} {f}" for f in files) or "(empty)"
    try:
        await q.message.edit_text(
            "🎵 <b>Music</b> — reel background tracks.\n"
            "Send me an .mp3 file to add one. 🎵 = active in library.\n\n" + listing,
            parse_mode="HTML",
            reply_markup=kb([[("♻️ Sync library = all files", "music_sync")],
                             [("⬅️ Back", "home")]]))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await q.answer()


@r.callback_query(F.data == "music_sync")
async def cb_music_sync(q: CallbackQuery):
    d = store()
    news_dir = MUSIC_DIR / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    d["music_library"] = sorted(f.name for f in news_dir.glob("*.mp3"))
    save_store(d)
    await q.answer(f"Library = {len(d['music_library'])} tracks", show_alert=True)
    await cb_music(q)


@r.message(F.audio | F.document)
async def on_audio(m: Message):
    if not admin_only(m):
        return
    obj = m.audio or m.document
    name = (obj.file_name or f"track_{obj.file_unique_id}.mp3")
    if not name.lower().endswith(".mp3"):
        return await m.answer("Only .mp3 please.")
    news_dir = MUSIC_DIR / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    dest = news_dir / name
    await bot.download(obj, destination=dest)
    d = store()
    if name not in d["music_library"]:
        d["music_library"].append(name)
        save_store(d)
    await m.answer(f"🎵 Added <code>{name}</code> to the library.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# Approvals — the card + 1-minute auto-post timeout
# ---------------------------------------------------------------------------

_card_tasks: dict[str, asyncio.Task] = {}


async def approval_watcher():
    """Poll the shared db for fresh pending approvals and send cards."""
    seen: set[str] = set()
    while True:
        try:
            with db() as c:
                rows = c.execute("SELECT * FROM approvals WHERE status='pending' "
                                 "ORDER BY created").fetchall()
            for row in rows:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                asyncio.create_task(send_card(dict(row)))
            if len(seen) > 300:
                live = {r_["id"] for r_ in rows}
                seen &= live
        except Exception as e:
            print("[watcher]", e)
        await asyncio.sleep(4)


async def send_card(row: dict):
    aid = row["id"]
    timeout = store()["settings"].get("approval_timeout_seconds", 60)
    files = json.loads(row["files"] or "[]")
    cap = (f"📥 <b>APPROVAL — {row['kind'].upper()}</b>\n"
           f"⏱ Auto-posts in <b>{timeout}s</b> unless you act.\n\n"
           f"{(row['caption'] or '')[:800]}")
    markup = kb([[("✅ Approve", f"ap:{aid}"), ("❌ Reject", f"rj:{aid}")],
                 [("✏️ Edit caption", f"ed:{aid}")]])
    try:
        f0 = files[0] if files else None
        if f0 and f0.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            await bot.send_photo(ADMIN, FSInputFile(f0), caption=cap,
                                 parse_mode="HTML", reply_markup=markup)
        elif f0 and f0.lower().endswith(".mp4"):
            await bot.send_video(ADMIN, FSInputFile(f0), caption=cap,
                                 parse_mode="HTML", reply_markup=markup)
        elif f0:
            await bot.send_document(ADMIN, FSInputFile(f0), caption=cap,
                                    parse_mode="HTML", reply_markup=markup)
        else:
            await bot.send_message(ADMIN, cap, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        print("[card]", e)
        await bot.send_message(ADMIN, cap, parse_mode="HTML", reply_markup=markup)
    _card_tasks[aid] = asyncio.create_task(auto_post_timer(aid, timeout))


async def auto_post_timer(aid: str, timeout: int):
    await asyncio.sleep(timeout)
    with db() as c:
        row = c.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()
        if row and row["status"] == "pending":
            c.execute("UPDATE approvals SET status='auto', decided=?, decided_by='timeout' "
                      "WHERE id=?", (datetime.now().isoformat(), aid))
            await bot.send_message(ADMIN, f"⏱ <code>{aid}</code> auto-posted (no action in "
                                          f"{timeout}s).", parse_mode="HTML")


@r.callback_query(F.data.startswith(("ap:", "rj:")))
async def cb_decide(q: CallbackQuery):
    action, aid = q.data.split(":")
    status = "approved" if action == "ap" else "rejected"
    with db() as c:
        c.execute("UPDATE approvals SET status=?, decided=?, decided_by='admin' "
                  "WHERE id=? AND status IN ('pending','auto')",
                  (status, datetime.now().isoformat(), aid))
    t = _card_tasks.pop(aid, None)
    if t:
        t.cancel()
    await q.answer("✅ Approved — publishing." if status == "approved" else "❌ Rejected.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@r.callback_query(F.data.startswith("ed:"))
async def cb_edit(q: CallbackQuery, state: FSMContext):
    aid = q.data[3:]
    t = _card_tasks.pop(aid, None)          # editing pauses the timeout
    if t:
        t.cancel()
    # Tell the orchestrator too — it runs its own independent countdown and
    # would otherwise auto-publish the original caption while you type.
    with db() as c:
        c.execute("UPDATE approvals SET decided_by='editing' WHERE id=? AND status='pending'",
                  (aid,))
    await state.set_state(Ask.edit_caption)
    await state.update_data(aid=aid)
    await q.message.answer("✏️ Send the new caption (posting is PAUSED until you do):")
    await q.answer()


@r.message(Ask.edit_caption)
async def st_caption(m: Message, state: FSMContext):
    data = await state.get_data()
    aid = data["aid"]
    with db() as c:
        row = c.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()
        payload = json.loads(row["payload"] or "{}") if row else {}
        payload["caption"] = m.text
        c.execute("UPDATE approvals SET payload=?, caption=?, status='approved', "
                  "decided=?, decided_by='admin-edit' WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), m.text,
                   datetime.now().isoformat(), aid))
    await state.clear()
    await m.answer("✅ Caption updated → approved → publishing.")


@r.callback_query(F.data == "approvals")
async def cb_approvals(q: CallbackQuery):
    with db() as c:
        rows = c.execute("SELECT * FROM approvals WHERE status='pending'").fetchall()
    if not rows:
        await q.answer("Nothing pending ✅", show_alert=True)
        return
    for row in rows:
        await send_card(dict(row))
    await q.answer(f"{len(rows)} card(s) resent.")


# ---------------------------------------------------------------------------
# /healer and /evolve review
# ---------------------------------------------------------------------------

@r.message(Command("healer"))
async def cmd_healer(m: Message):
    if not admin_only(m):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM healer WHERE status='pending' "
                         "ORDER BY id DESC LIMIT 5").fetchall()
    if not rows:
        return await m.answer("🛠 No healer suggestions pending.")
    for row in rows:
        s = json.loads(row["suggestion"])
        await m.answer(
            f"🛠 <b>Healer #{row['id']}</b> — {row['platform']}\n"
            f"Error: <code>{row['error'][:200]}</code>\n"
            f"Diagnosis: {s.get('diagnosis')}\n"
            f"Selectors: <code>{json.dumps(s.get('replacement_selectors'))[:300]}</code>\n"
            f"Confidence: {s.get('confidence')}\n\n"
            "To apply: paste the selector into your GitHub <code>selectors.json</code> "
            "(hot-patches without reloading) — then mark done.",
            parse_mode="HTML",
            reply_markup=kb([[("✅ Mark done", f"hz:{row['id']}"),
                              ("🗑 Discard", f"hx:{row['id']}")]]))


@r.callback_query(F.data.startswith(("hz:", "hx:")))
async def cb_healer(q: CallbackQuery):
    act, hid = q.data.split(":")
    with db() as c:
        c.execute("UPDATE healer SET status=? WHERE id=?",
                  ("done" if act == "hz" else "discarded", hid))
    await q.answer("Updated.")
    await q.message.edit_reply_markup(reply_markup=None)


@r.message(Command("evolve"))
async def cmd_evolve(m: Message):
    if not admin_only(m):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM evolution WHERE status='pending' "
                         "ORDER BY id DESC LIMIT 3").fetchall()
    if not rows:
        return await m.answer("🧬 No evolution proposals pending.")
    for row in rows:
        p = json.loads(row["payload"])
        changes = "\n".join(f"• {c_.get('what')}" for c_ in p.get("changes", [])[:6])
        await m.answer(
            f"🧬 <b>Evolution #{row['id']}</b> — <code>{row['prompt_name']}</code>\n"
            f"{changes or '(see payload)'}\n\n"
            "Approving REPLACES the live prompt.",
            parse_mode="HTML",
            reply_markup=kb([[("✅ Apply", f"ez:{row['id']}"),
                              ("🗑 Discard", f"ex:{row['id']}")]]))


@r.callback_query(F.data.startswith(("ez:", "ex:")))
async def cb_evolve(q: CallbackQuery):
    act, eid = q.data.split(":")
    with db() as c:
        row = c.execute("SELECT * FROM evolution WHERE id=?", (eid,)).fetchone()
        if not row:
            return await q.answer("Gone.")
        if act == "ez":
            p = json.loads(row["payload"])
            key = "transmuted_prompt" if "transmuted_prompt" in p else "evolved_prompt"
            new_prompt = p.get(key, "")
            if new_prompt:
                d = store()
                d["prompts"][row["prompt_name"]] = new_prompt
                save_store(d)
        c.execute("UPDATE evolution SET status=? WHERE id=?",
                  ("applied" if act == "ez" else "discarded", eid))
    await q.answer("Applied ✅" if act == "ez" else "Discarded.")
    await q.message.edit_reply_markup(reply_markup=None)


# ---------------------------------------------------------------------------
# /setup — niche transmutation wizard
# ---------------------------------------------------------------------------

@r.message(Command("setup"))
async def cmd_setup(m: Message, state: FSMContext):
    if not admin_only(m):
        return
    await state.set_state(Ask.setup_niche)
    await m.answer(
        "🧙 <b>NICHE TRANSMUTATION WIZARD</b>\n"
        "I'll rebuild every agent prompt for a brand-new niche. Nothing is applied "
        "without your approval.\n\n<b>Step 1/3</b> — What's the new NICHE?\n"
        "(e.g. <i>fitness &amp; muscle building</i>, <i>UPSC preparation</i>, "
        "<i>crypto DeFi</i>)", parse_mode="HTML")


@r.message(Ask.setup_niche)
async def st_niche(m: Message, state: FSMContext):
    await state.update_data(niche=m.text.strip())
    await state.set_state(Ask.setup_brand)
    await m.answer("<b>Step 2/3</b> — New BRAND NAME?", parse_mode="HTML")


@r.message(Ask.setup_brand)
async def st_setup_brand(m: Message, state: FSMContext):
    await state.update_data(brand=m.text.strip())
    await state.set_state(Ask.setup_audience)
    await m.answer("<b>Step 3/3</b> — TARGET AUDIENCE? (one line)", parse_mode="HTML")


@r.message(Ask.setup_audience)
async def st_audience(m: Message, state: FSMContext):
    data = await state.get_data()
    niche, brand, audience = data["niche"], data["brand"], m.text.strip()
    await state.clear()
    msg = await m.answer("🧙 Transmuting all agent prompts via Gemini — this takes a few "
                         "minutes. Proposals will appear under /evolve.")

    def _run():
        d = store()
        template = d["prompts"]["meta_transmuter"]
        done, failed = 0, 0
        for name in ("brand_ceo", "content_manager", "reel_maker", "post_maker",
                     "pdf_maker", "tele_manager", "growth_hacker"):
            try:
                prompt = (template
                          .replace("{new_niche}", niche)
                          .replace("{new_brand_name}", brand)
                          .replace("{target_audience}", audience)
                          .replace("{prompt_name}", name)
                          .replace("{original_prompt}", d["prompts"][name]))
                j = bridge_post("/queue-task", {"type": "gemini", "prompt": prompt})
                if not j.get("ok"):
                    raise RuntimeError(j.get("error"))
                tid = j["id"]
                end = time.time() + 420
                text = ""
                while time.time() < end:
                    jr = bridge_get(f"/result/{tid}")
                    if jr.get("done"):
                        res = jr["result"]
                        if not res.get("ok"):
                            raise RuntimeError(res.get("error"))
                        # BUG-0: extension/content.js reports the answer under
                        # "response". Reading "text" here always yielded "" and
                        # every transmutation died in json.loads.
                        d_ = res.get("data") or {}
                        text = d_.get("response") or d_.get("text") or ""
                        break
                    time.sleep(4)
                import re as _re
                text = _re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=_re.S)
                parsed = json.loads(text)
                with db() as c:
                    c.execute("INSERT INTO evolution(dt,prompt_name,payload) VALUES(?,?,?)",
                              (datetime.now().isoformat(), name,
                               json.dumps(parsed, ensure_ascii=False)))
                done += 1
            except Exception as e:
                print("[setup]", name, e)
                failed += 1
        d = store()
        d["brand"]["niche"] = niche
        d["brand"]["name"] = brand
        d["brand"]["target_audience"] = audience
        save_store(d)
        return done, failed

    done, failed = await asyncio.to_thread(_run)
    await msg.edit_text(
        f"🧙 Transmutation complete: {done} proposals ready, {failed} failed.\n"
        f"Brand identity updated to <b>{brand}</b> / {niche}.\n"
        "Review and apply each prompt with /evolve.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main():
    print("🤖 Unified Manager online")
    asyncio.create_task(approval_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
