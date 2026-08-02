# Stock Warrior — This Build

## New bugs fixed this round

- **BUG-8 — periodic loops fired on fresh boot.** Growth scan and prompt
  evolution ran immediately on a new DB because their last-run timestamp was
  empty. Now seeded at first init, so they wait their interval. (CEO still runs
  once on purpose — you want an initial plan.)
- **BUG-9 — evolution ran with zero data.** With no performance logs there's
  nothing to learn from, so it now skips and stamps the timestamp instead of
  burning 3 Gemini calls to reach a guaranteed NO_CHANGE.

**Note on your "3 attempts":** that wasn't retries — `run_prompt_evolution`
loops over 3 prompts (reel_maker, post_maker, tele_manager), one call each. With
BUG-9 it won't run at all until real data exists.

## New features

### 📆 Schedule viewer
New home button. Shows every upcoming task grouped by day with time, type icon,
and 🚨 for urgent. Plus a Failed-tasks screen with the error on each. Reads the
shared warrior.db — no new bridge endpoints.

### ⚙️ Friendly settings (replaces raw-JSON editing)
Every setting is now a toggle (🟢/🔴), a pick-list, or a guided prompt with an
example. No more "send me JSON". Includes a "What do these mean?" screen
explaining each one. Old handler retired (kept in code, trigger renamed).

### 🔑 Tokens & Keys manager (ported concept from course_bot)
Meta token, IG business ID, YouTube channel, IG sessionid, Reddit id/secret,
Twitter bearer. Stored to .env (upsert — never clobbers existing keys or
comments), shown masked (EAAn…2345), and your input message is auto-deleted so
the raw secret doesn't linger in chat.

### ❓ Help
Plain-language guide to every button.

## Architecture
All new UI lives in a separate `telegram_ui.py` router attached to the same
dispatcher, sharing the bot's real store()/db(). No collision with existing
callbacks (verified). Still connector-only for AI generation — no API keys
introduced. Meta/YouTube publishing use their existing Graph/OAuth paths as before.

## Tested here
- Schedule + failed queries run against the real tasks schema — render correct.
- .env upsert round-trip: replace + add, comments and unrelated keys preserved.
- No callback-data collisions between the two routers.
- New DB access wrapped in closing() — no connection leak (the bug I fixed in
  course_bot, avoided here).
- ruff clean, all Python compiles, both JS files pass node --check.

## NOT tested here
No live Telegram, no Chrome, no keys. Button wiring, FSM flows, card rendering,
and the actual Gemini round-trip need a live run. `test_suite.py` T01-T12 with
the bridge up remains the gate.

## Run
```
pip install -r requirements.txt
python main_orchestrator.py
```
Drop in .env + chrome-profile/. Tokens can now also be added from the panel.
