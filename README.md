# 🐺 Stock Warrior

An autonomous content engine for an Indian trading/finance brand. It plans a
7-day calendar, generates reels, carousels, PDFs and polls, routes every piece
through a Telegram approval card, and publishes to Instagram, YouTube Shorts
and Telegram.

**Connector-first:** every LLM / image / video / voice / PDF call is driven
through a real Chrome session by a local bridge. There are **no AI API keys
anywhere**. The only API credentials in `.env` are for *publishing*
(Telegram, Meta Graph v25.0, YouTube OAuth) and two optional media helpers.

---

## Architecture

```
main_orchestrator.py   the daemon - 30s IST scheduler, agent pipelines,
                       publishers, auto-healer. Spawns the other two.
      |
      +-- browser_bridge.py    Flask on 127.0.0.1:5000. Launches a dedicated
      |        |               Chrome profile, handles OS-level window focus
      |        |               and clipboard paste.
      |        +-- extension/  MV3 extension. Long-polls /get-task, drives
      |                        ChatGPT / Claude / Gemini / Flow / ElevenLabs,
      |                        POSTs back to /submit-result.
      |
      +-- telegram_bot.py      control panel + approval cards
               +-- telegram_ui.py   settings / schedule / tokens / help

           warrior.db (SQLite, WAL)  <- shared by orchestrator and bot
           prompts_store.json        <- brand / settings / routing / prompts
           selectors.json            <- DOM selectors, mirrored into extension/
```

### Agents

| Agent | Trigger | Produces |
|---|---|---|
| `BRAND_CEO` | every 48h | strategic pillars, urgent interruptions |
| `CONTENT_MANAGER` | after each CEO run | 7-day calendar (>=14 tasks) into `tasks` |
| `REEL_MAKER` | scheduled | Gemini script -> one Flow/Veo video -> IG Reel + YT Short + TG |
| `POST_MAKER` | scheduled | Gemini banner JSON -> ChatGPT image(s) -> IG post/carousel + TG |
| `PDF_MAKER` | scheduled | Gemini markdown -> Claude PDF -> TG drop + voice note |
| `TELEGRAM_POLL` | scheduled | channel poll + psychology follow-up |
| `NEWS` | `settings.news_schedule` slots | headlines; MAJOR ones fire an urgent post |
| `GROWTH_HACKER` | every `growth_scan_hours` | Reddit replies (needs `praw` + creds) |
| `PROMPT_EVOLVER` | every 30 days | prompt proposals, **manual approval only** |

Every generated asset goes through an approval card with an auto-post timeout.

---

## Run

```bash
pip install -r requirements.txt
python main_orchestrator.py            # starts the bridge + bot too
python main_orchestrator.py --no-bot   # orchestrator only
python main_orchestrator.py --once reel "topic"   # fire one pipeline now
```

Drop in `.env` (copy from `.env.example`) and a `chrome-profile/` folder.
Tokens can also be added from the Telegram panel's Tokens screen.

Windows first-time setup: `setup.bat` (as Administrator).
Full walkthrough: **[SETUP_GUIDE.md](SETUP_GUIDE.md)**.

```bash
# headless equivalents of the Test Lab buttons
python main_orchestrator.py --preflight          # is the config ready? spends nothing
python main_orchestrator.py --dry reel            # real Gemini, stubbed media, no publish
python main_orchestrator.py --test gemini         # one step in isolation
python main_orchestrator.py --test cheap          # every test that spends nothing
python main_orchestrator.py --test all            # full suite (generates + publishes)
```

---

## Stopping and restarting

Closing the console window does **not** kill `browser_bridge.py` and
`telegram_bot.py` — they are separate processes. The old bridge keeps port 5000
and the old bot keeps the Telegram long-poll, so the next start looks dead.
That is why stopping used to mean deleting the database.

Every boot now does this automatically:

1. kills child processes left by the previous run (tracked in `.warrior_pids.json`)
2. calls `deleteWebhook(drop_pending_updates)` to free a stale Telegram poll
3. requeues tasks stranded in `running`, retries interrupted panel commands once,
   expires orphaned approval cards
4. restarts the bridge if it is up but has no Chrome behind it

Ctrl-C and SIGTERM also take the children down with them.

If something still looks stuck:

```bash
python main_orchestrator.py --reset     # never delete warrior.db again
```

`--reset` keeps your calendar, history and performance data.

---

## Test Lab — stop waiting for the schedule

The panel's **Test Lab** runs anything on demand; nothing waits for a slot.

| Tier | What it does | Cost |
|---|---|---|
| **Dry run** | Gemini really answers, so real schema bugs surface. Media is stubbed, nothing is published. | Seconds, free |
| **Steps** | One connector or one publisher alone (Gemini, image, PDF, video, TTS, clip, IG Story/post/Reel, YouTube, Twitter, TG text/photo/poll) | One generation |
| **Live** | The full pipeline, real generation, real publish, right now | Full |
| **Preflight** | Config readiness report: tokens, bridge, prompts, schemas, routing | Free |

Results are stored, so you never have to re-run the whole suite to check a fix:

* **Re-run failed only** — retries exactly what failed last time, nothing else.
* **Pick tests to run** — a toggle grid; anything that failed last run is marked
  with an X. Bulk select All / None / Failed, then run just those.

Partial runs *merge* into the stored results, so re-testing one item does not
erase the other outstanding failures.

```bash
python main_orchestrator.py --test failed              # only last run's failures
python main_orchestrator.py --test ig_reel,youtube     # a specific list
```

Use **Dry run** while fixing bugs. It walks the exact same code path, so a
missing key blows up in seconds instead of at 6pm three days later.

Every failure — anywhere in the engine — comes back decoded:
**what happened**, **how to fix it**, and the raw error, instead of a traceback.

---

## Routing

| Content | Instagram | YouTube | Twitter | Telegram |
|---|---|---|---|---|
| News | **Story** (banner over stock video + music, 15s) + feed | - | yes* | yes |
| Carousel | Feed | - | yes* (first 4 slides) | yes |
| Reel | Reel | Short | off | yes (Telemanager context copy) |
| PDF | - | - | - | **always** |
| Poll | - | - | - | yes |

\* Twitter is **built but dark**. `TWITTER_BEARER_TOKEN` is app-only auth and is
read-only; posting needs the four OAuth 1.0a keys (`TWITTER_API_KEY`,
`TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_SECRET`) plus
`settings.twitter_enabled = true`. Everything else is already wired.

Instagram Stories require an Instagram **Business** account with
`instagram_content_publish`.

### Media hosting (important)

Meta does not accept an upload — it downloads the file from a public URL you
give it. Images use **ImgBB** when `IMGBB_API_KEY` is set. Video has no ImgBB
equivalent, so it tries **catbox.moe -> tmpfiles.org -> 0x0.st** in order and
verifies the resulting URL is really fetchable before handing it to Meta. This
is why an image post can succeed while a Reel or Story fails: they take
different hosting paths.

`PEXELS_API_KEY` supplies stock footage for the story background. It is a
*source* of b-roll, not a host — it cannot serve your own renders to Meta.

### Direct upload — the actual fix for video

ImgBB hosts **images only**. There is no equally reliable free host for video,
which is exactly why a photo post can succeed in the same run where a Reel and a
Story both fail.

Set `IG_USERNAME` and `IG_PASSWORD` and install `instagrapi`. Reels and Stories
then upload **straight to Instagram's private mobile API** whenever Graph fails —
the file is POSTed directly and no public URL exists at any point.

Order is always: **Graph first, direct upload only as a fallback.** If both
fail, the original Graph error is what you see, not the fallback's.

Caveats, stated plainly: instagrapi is unofficial, it needs your real password,
and Instagram may raise a login challenge (approve it on your phone). The
session is cached in `ig_session.json` to avoid repeated logins, which is what
usually triggers challenges. Turn it off with `settings.instagrapi_fallback`.
Test it with **Test Lab → 🔧 Steps → IG direct upload login**.

Edit routing in `prompts_store.json` -> `routing`.

---

Verify before deploying: `python test_suite.py` - T01-T13 with the bridge live
is the gate.

---

## Selector hot-patching

`selectors.json` is bundled in the extension *and* fetched from GitHub at a
pinned commit (`SELECTOR_SHA` in `extension/background.js`).

> If you edit `selectors.json`, commit it **and** bump `SELECTOR_SHA` to the new
> commit hash. Otherwise the extension keeps fetching the old file and your fix
> silently does nothing.

---

## Changelog

See **[FIXES.md](FIXES.md)** (bug history) and **[WHATS_NEW.md](WHATS_NEW.md)**
(feature history).

## Repo layout note

This repository previously also contained `course_bot.py` - an unrelated,
API-key-based earlier bot with its own `warrior_manager.db` and its own Telegram
polling loop on the *same* bot token. It was orphaned (nothing imported or
spawned it) and has been removed, along with `index.html` (its Telegram WebApp
editor) and four already-applied one-shot patch scripts (`add_upload.py`,
`update_dl.py`, `update_selectors.py`, `migrate.py`).
