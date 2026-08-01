# 🐺 STOCK WARRIOR — SETUP GUIDE (Windows)

From zero to a fully autonomous media empire in ~20 minutes.

```
D:\PSG\                        ← or any folder you like
├── main_orchestrator.py       ← THE DAEMON (brain + scheduler + publishers)
├── telegram_bot.py            ← Unified Manager (your Telegram control panel)
├── browser_bridge.py          ← connector server (was bridge.py — now v5)
├── prompts_store.json         ← all agent prompts + settings (NO secrets)
├── test_suite.py              ← run BEFORE deploying, every time
├── selectors.json             ← push to your GitHub for hot-patching
├── .env                       ← ALL your secrets (copy from .env.example)
├── extension\                 ← unpacked Chrome extension (v5)
│   ├── manifest.json
│   ├── background.js
│   └── content.js
├── music\                     ← reel background mp3s (add via Telegram panel)
├── output\                    ← everything the engine creates
└── warrior.db                 ← created automatically (state + approvals)
```

---

## STEP 1 — Python packages

```bat
pip install flask pyautogui requests psutil pyperclip aiogram praw moviepy yt-dlp
pip install google-api-python-client google-auth google-auth-oauthlib
```
The last line is only needed for YouTube Shorts publishing.

## STEP 2 — Secrets (.env)

Copy `.env.example` → `.env` and fill it in:

1. **TELEGRAM_BOT_TOKEN** — talk to @BotFather → `/newbot` → copy the token.
2. **TELEGRAM_ADMIN_ID** — message @userinfobot, it replies with your numeric id.
3. **TELEGRAM_CHANNEL_ID** — add your bot to your channel as an **admin** with
   post permission. Use `@channelname` (public) or the `-100…` id (private —
   forward a channel post to @userinfobot to get it).
4. **META_ACCESS_TOKEN / META_IG_ACCOUNT_ID** *(optional)* — Meta developer
   app with `instagram_content_publish`; a long-lived token and your IG
   Business account id. Leave blank to skip IG auto-posting (content still
   arrives on Telegram + saved in `output\`).
5. **REDDIT_**\* *(optional)* — https://www.reddit.com/prefs/apps → create a
   "script" app. Leave blank to skip the Growth Hacker.
6. YouTube *(optional)* — place your OAuth user credentials as
   `yt_token.json` in the folder (same file the old bot used).

Nothing else in the system ever asks for an API key. **All content
generation is connector-only** — Gemini, ChatGPT, Claude, Flow and
ElevenLabs run through the real web UIs in an automated Chrome.

## STEP 3 — Extension & first Chrome launch

```bat
python browser_bridge.py
```
First run launches a **dedicated automation Chrome profile** with the
extension pre-loaded. In that Chrome window, log in ONCE to each site:

- gemini.google.com  (Google account with Flow access)
- chatgpt.com
- claude.ai
- labs.google/fx/tools/flow
- elevenlabs.io

Logins persist in the profile — you never do this again.

Optional but recommended: edit `SELECTOR_URL` at the top of
`extension/background.js` to point at YOUR GitHub raw `selectors.json`,
then upload the included `selectors.json` there. That lets you hot-fix
selectors from your phone if a site updates its UI (see /healer below).

## STEP 4 — TEST BEFORE DEPLOY (mandatory ritual)

Keep `browser_bridge.py` running. In a **second terminal**:

```bat
python test_suite.py
```

Choose **FULL** the first time (~20–30 min, generates real videos).
Every deploy after that, **QUICK** is enough. The suite must say
**"SAFE TO DEPLOY"** — if any test fails, the report tells you exactly
which connector broke and why. Do not start the engine on a failing suite.

## STEP 5 — IGNITION 🚀

```bat
python main_orchestrator.py
```

That's it. One command starts the daemon **and** the Telegram panel.
On first boot the engine immediately:

1. Runs the **Brand CEO** (48 h strategic loop),
2. The **Content Manager** builds a 7-day calendar (≥14 tasks),
3. Tasks fire at their IST times → content → **approval card** in your
   Telegram → 60 s later it auto-posts unless you Reject/Edit,
4. News runs at your `news_schedule` slots (default 09:30 / 13:30 / 18:30),
5. Growth Hacker scans Reddit every 6 h,
6. Every 30 days the Prompt Evolver proposes upgrades (never auto-applied).

Useful variants:

```bat
python main_orchestrator.py --no-bot          run bot separately
python telegram_bot.py                        (the separate bot)
python main_orchestrator.py --once post "SEBI's new F&O rules explained"
python main_orchestrator.py --once reel "Liquidity sweep trap on NIFTY"
```

## THE TELEGRAM PANEL

Send `/start` to your bot:

- **📥 Approvals** — resend any pending cards
- **⚡ Action Center** — Text→Post, Make Reel/PDF/Poll, **IG Reel Clone**,
  **YT Short Clone** (yt-dlp → fresh Gemini caption → approval →
  `routing["cloned"]`), **Parse Web Link** (article URL → post)
- **🧪 Connector** — live bridge/extension status, quick Gemini test,
  **RESTART CHROME** button
- **⚙️ Settings** — every engine setting editable as JSON, applies live
- **🎨 Brand** / **🔀 Routing** — identity fields + per-content platform matrix
- **🎵 Music** — send the bot an mp3, it lands in `music\` and mixes under
  reels at 12 % volume
- **/healer** — when a selector breaks mid-task, Gemini diagnoses it and the
  suggestion waits here (auto-apply is OFF by design)
- **/evolve** — review/apply prompt-evolution and transmutation proposals
- **/setup** — 🧙 the **Niche Transmutation Wizard**: 3 questions, then Gemini
  rewrites all 7 agent prompts for a completely new niche (fitness, UPSC,
  crypto…). Every rewritten prompt waits in /evolve for your approval.

## HOW APPROVALS WORK

Every reel / post / carousel / PDF / Reddit reply produces a card with
✅ Approve, ❌ Reject, ✏️ Edit caption. **1 minute of silence = auto-post**
(the "silence is consent" model — change `approval_timeout_seconds` or
`auto_publish_on_timeout` in Settings). Pressing ✏️ pauses the clock until
you send the new caption.

## TROUBLESHOOTING

| Symptom | Fix |
|---|---|
| Orchestrator refuses to start | `browser_bridge.py` isn't running — Terminal 1 first |
| "extension not polling" | Open the automation Chrome → `chrome://extensions` → reload; or panel → 🧪 → RESTART CHROME |
| Ctrl+V lands in wrong window | It can't anymore — bridge forces OS focus first. If focus test fails: bridge menu option 11 |
| Flow downloads the same video twice | It can't anymore — run `test_suite.py` → preset 4 (T09) to prove it |
| A site changed its UI | /healer shows Gemini's diagnosis → paste the new selector into your GitHub selectors.json → hot-patched, no reload |
| IG posting fails | Check META_ACCESS_TOKEN expiry; content is still in `output\` + Telegram |
| Want a different niche | /setup — 3 questions, approve in /evolve |

## THE FILES YOU'RE ALLOWED TO TOUCH

- `.env` — secrets
- `prompts_store.json` — prompts, settings, routing (or edit via the panel)
- `selectors.json` on your GitHub — UI hot-patches

Everything else is the machine. Let it run. 🐺
