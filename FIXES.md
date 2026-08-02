# Stock Warrior — Complete Fix Set

## 🔴 BUG-0 — Gemini result key mismatch (this was your empty-string error)

`extension/content.js` returns the answer as **`response`**; `ask_gemini()` read
**`text`**. So `data.get("text","")` was always `""` -> `parse_llm_json("")` ->
`Expecting value: line 1 column 1 (char 0)`.

Present since the **initial commit**. It only surfaced now because every earlier
failure (CORS, clipboard, Chrome) short-circuited before the task could succeed.
`[bridge] OK task ... SUCCESS` + empty raw text is the signature.

```python
parsed = parse_llm_json(data.get("response") or data.get("text") or "")
```

Your prompt and Gemini output were never the problem — the sample you sent
validates cleanly against `schema_map["brand_ceo"]`.

## 🔴 BUG-1 — CEO retry storm
`last_ceo_run` was written only on success, so any CEO failure re-fired the CEO
every 30s forever, relaunching Chrome each cycle (the about:blank flood).
Now claims the slot before running, with a ~2h backoff on failure.

## 🔴 BUG-2 — unguarded index after a presence-only schema check
`result['executive_summary'].get(...)` crashed if the key held a non-dict.
Now `(result.get('executive_summary') or {}).get(...)`.

## 🟠 BUG-3 — anti-laziness retry never re-checked
<14 tasks retried once, then accepted silently. Now re-checks and warns admin.

## 🟠 BUG-4 — healer recursion
`ask_gemini -> on_bridge_failure -> ask_gemini`. Added a `_healing` re-entrancy flag.

## 🟡 BUG-5/6/7
Past-dated tasks rejected; capped tasks logged and surfaced; tasks stranded in
`running` after a crash are requeued at boot.

---

# Earlier rounds (all still in)

- **CORS** — content scripts are subject to CORS; the clipboard read now proxies
  through the service worker, plus `Access-Control-Allow-Origin` on the bridge.
- **/get-clipboard** — `import pyperclip` moved inside `try` (was returning a 500
  HTML page); PowerShell fallback on Windows.
- **Chrome relaunch cooldown** — 90s, stops the blank-page flood.
- **Bridge stdin** — spawned with `DEVNULL` + `--no-menu`; stray Ctrl+V into the
  shared console was hitting `except EOFError: shutdown()` and killing the bridge.
- **shutdown_route()** — un-shadowed the hard-exit `shutdown()`.
- **16 SQLite leaks** — all `with closing(_db())`, WAL + 30s timeout.
- **SELECTOR_SHA pin** + bundled fallback + CI drift check.
- **requirements.txt** — split by entry point. Main pipeline needs **flask,
  requests, aiogram**. The `openai`/`instagrapi` stack belonged to `course_bot.py`, which has been removed.

---

# Run it

```bash
pip install -r requirements.txt
python main_orchestrator.py
```

Drop in your `.env` and `chrome-profile/`. Nothing else to configure.

# Still not verified by me
Static analysis only — no Chrome, no keys, no live run here. Live DOM selectors,
Telegram callbacks, moviepy, Meta publish and IST scheduling are untested.
`test_suite.py` T01-T12 with the bridge live remains the gate.
