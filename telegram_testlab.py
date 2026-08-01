"""
TEST LAB — Telegram router
==========================
One button for every step and every workflow, all runnable ON DEMAND.

The whole point: you should never again fix a bug, wait for a scheduled slot,
and discover the next bug hours later. Everything here fires within ~2 seconds
(the orchestrator's botcmd worker polls that fast) and reports back with a
decoded error, not a traceback.

Three tiers:
  🧪 DRY RUN   real Gemini, stubbed media + publish. Seconds. Costs nothing.
               This is the one you use while fixing bugs.
  🔧 STEP      one real connector or one real publish, in isolation.
  🚀 LIVE      the full pipeline, real generation, real publish.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()

_queue_cmd = None       # injected from telegram_bot.py
_guard_admin = 0


def init(queue_cmd_fn, admin_id: int) -> Router:
    global _queue_cmd, _guard_admin
    _queue_cmd, _guard_admin = queue_cmd_fn, admin_id
    return router


def _ok(obj) -> bool:
    return bool(obj.from_user and obj.from_user.id == _guard_admin)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


class TestStates(StatesGroup):
    dry_topic = State()
    live_topic = State()


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "tl_home")
async def tl_home(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    await q.message.edit_text(
        "🧪 <b>Test Lab</b>\n\n"
        "Nothing here waits for a schedule — everything runs now.\n\n"
        "🧪 <b>Dry run</b> — real Gemini, fake media, nothing published.\n"
        "   Seconds, costs nothing. Use this while fixing bugs.\n"
        "🔧 <b>Steps</b> — test one connector or one publisher alone.\n"
        "🚀 <b>Live</b> — the real thing, real generation, really posts.\n"
        "🚦 <b>Preflight</b> — is the config ready to go live?",
        parse_mode="HTML",
        reply_markup=kb([
            [("🚦 Preflight check", "tl_pre")],
            [("🧪 Dry runs", "tl_dry"), ("🔧 Steps", "tl_steps")],
            [("🚀 Live workflows", "tl_live")],
            [("▶️ Run ALL tests (cheap)", "tl_all_cheap")],
            [("💥 Run ALL tests (full, spends generations)", "tl_all_full")],
            [("⬅️ Back", "home")]]))
    await q.answer()


@router.callback_query(F.data == "tl_pre")
async def tl_pre(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    _queue_cmd("preflight")
    await q.answer("🚦 Running preflight…", show_alert=False)
    await q.message.answer("🚦 Preflight queued — the report lands in a moment.")


@router.callback_query(F.data.in_({"tl_all_cheap", "tl_all_full"}))
async def tl_all(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    cheap = q.data == "tl_all_cheap"
    _queue_cmd("test_cheap" if cheap else "test_all")
    await q.answer("Queued.")
    await q.message.answer(
        ("▶️ Running all cheap tests (no generations spent)."
         if cheap else
         "💥 Running the FULL suite — this generates real media and really posts. "
         "Expect several minutes."))


# ---------------------------------------------------------------------------
# Dry runs
# ---------------------------------------------------------------------------

DRY_KINDS = [
    ("🎬 Reel (UGC)", "reel"), ("🎭 Reel (Animated)", "reel_animated"),
    ("🖼 Single post", "post"), ("🎠 Carousel", "carousel"),
    ("📰 News", "news"), ("📄 PDF", "pdf"),
    ("📊 Poll", "poll"), ("👑 CEO", "ceo"),
    ("📅 Calendar", "plan"),
]


@router.callback_query(F.data == "tl_dry")
async def tl_dry(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    rows = [[(label, f"tl_d:{k}")] for label, k in DRY_KINDS]
    rows.append([("▶️ Dry-run EVERY pipeline", "tl_d:__all__")])
    rows.append([("⬅️ Back", "tl_home")])
    await q.message.edit_text(
        "🧪 <b>Dry runs</b>\n\n"
        "Gemini really answers, so real schema bugs surface — but no image, "
        "video, voice or PDF is generated and nothing is published.\n\n"
        "This is the fastest way to prove a fix worked.",
        parse_mode="HTML", reply_markup=kb(rows))
    await q.answer()


@router.callback_query(F.data.startswith("tl_d:"))
async def tl_dry_go(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    kind = q.data.split(":", 1)[1]
    kinds = [k for _, k in DRY_KINDS] if kind == "__all__" else [kind]
    for k in kinds:
        _queue_cmd(f"dry:{k}", "")
    await q.answer("Queued.")
    await q.message.answer(f"🧪 Dry run queued: <b>{', '.join(kinds)}</b>\n"
                           "Result arrives in a few seconds.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------

STEP_ROWS = [
    [("🔌 Bridge", "bridge"), ("🧠 Gemini", "gemini")],
    [("🖼 ChatGPT image", "image"), ("📄 Claude PDF", "pdf")],
    [("🎬 Flow video", "video"), ("🎤 ElevenLabs", "tts")],
    [("🎞 15s news clip", "clip"), ("🔑 Meta token", "meta_token")],
    [("📸 IG Story", "ig_story"), ("🖼 IG post", "ig_post")],
    [("🎬 IG Reel", "ig_reel"), ("▶️ YouTube", "youtube")],
    [("🐦 Twitter", "twitter"), ("💬 TG text", "tg_text")],
    [("🖼 TG photo", "tg_photo"), ("📊 TG poll", "tg_poll")],
]


@router.callback_query(F.data == "tl_steps")
async def tl_steps(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    rows = [[(label, f"tl_s:{name}") for label, name in row] for row in STEP_ROWS]
    rows.append([("⬅️ Back", "tl_home")])
    await q.message.edit_text(
        "🔧 <b>Steps</b> — test one thing in isolation.\n\n"
        "Media steps really generate. Publish steps really post to your accounts.\n"
        "Start with 🔌 Bridge and 🧠 Gemini — everything else depends on them.",
        parse_mode="HTML", reply_markup=kb(rows))
    await q.answer()


@router.callback_query(F.data.startswith("tl_s:"))
async def tl_step_go(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    name = q.data.split(":", 1)[1]
    _queue_cmd(f"test:{name}")
    await q.answer("Running…")
    await q.message.answer(f"🔧 Test queued: <code>{name}</code>", parse_mode="HTML")


# ---------------------------------------------------------------------------
# Live workflows
# ---------------------------------------------------------------------------

LIVE_KINDS = [
    ("🎬 Reel", "make_reel"), ("🖼 Post", "text_to_post"),
    ("🎠 Carousel", "make_carousel"), ("📄 PDF", "make_pdf"),
    ("📊 Poll", "make_poll"), ("📰 News run", "run_news"),
    ("👑 CEO", "run_ceo"), ("📅 Calendar", "run_plan"),
    ("🌱 Growth scan", "run_growth"), ("🧬 Prompt evolution", "run_evolve"),
]

NEEDS_TOPIC = {"make_reel", "text_to_post", "make_carousel", "make_pdf", "make_poll"}


@router.callback_query(F.data == "tl_live")
async def tl_live(q: CallbackQuery):
    if not _ok(q):
        return await q.answer()
    rows = [[(label, f"tl_l:{c}")] for label, c in LIVE_KINDS]
    rows.append([("⬅️ Back", "tl_home")])
    await q.message.edit_text(
        "🚀 <b>Live workflows</b>\n\n"
        "⚠️ These generate real media and publish for real, right now — "
        "no waiting for a scheduled slot.\n\n"
        "If you're mid-bugfix, use 🧪 Dry runs instead.",
        parse_mode="HTML", reply_markup=kb(rows))
    await q.answer()


@router.callback_query(F.data.startswith("tl_l:"))
async def tl_live_go(q: CallbackQuery, state: FSMContext):
    if not _ok(q):
        return await q.answer()
    cmd = q.data.split(":", 1)[1]
    if cmd in NEEDS_TOPIC:
        await state.set_state(TestStates.live_topic)
        await state.update_data(cmd=cmd)
        await q.message.answer("Send me the TOPIC (or send <code>-</code> for a default):",
                               parse_mode="HTML")
    else:
        _queue_cmd(cmd)
        await q.message.answer(f"🚀 Queued: <code>{cmd}</code> — running now.",
                               parse_mode="HTML")
    await q.answer()


@router.message(TestStates.live_topic)
async def tl_live_topic(m: Message, state: FSMContext):
    if not _ok(m):
        return
    data = await state.get_data()
    cmd = data.get("cmd", "text_to_post")
    topic = (m.text or "").strip()
    if topic == "-":
        topic = "Nifty ne aaj naya all-time high banaya — retail traders ka FOMO"
    await state.clear()
    _queue_cmd(cmd, topic)
    await m.answer(f"🚀 Queued <code>{cmd}</code>.\nApproval card arrives when it's ready.",
                   parse_mode="HTML")
