"""
make_yt_token.py — run this ONCE to authorise YouTube uploads.
==============================================================

You need exactly TWO files, and only the first one comes from Google:

  1. client_secret.json   <- YOU download this from Google Cloud Console
  2. yt_token.json        <- THIS SCRIPT creates it. Never download it.

How to get client_secret.json
-----------------------------
  1. console.cloud.google.com  ->  create (or pick) a project
  2. APIs & Services -> Library -> enable "YouTube Data API v3"
  3. APIs & Services -> OAuth consent screen
       - User type: External
       - Add YOUR OWN Google account under "Test users"
       - IMPORTANT: while the app is in "Testing", refresh tokens expire after
         7 days and uploads start failing with invalid_grant. To stop that,
         press "Publish app" once you have it working.
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID
       - Application type: **Desktop app**   (not Web, not TV)
  5. Download the JSON, rename it to `client_secret.json`, and put it in this
     folder next to main_orchestrator.py

Then run:
    pip install google-api-python-client google-auth-oauthlib
    python make_yt_token.py

A browser opens, you approve, and yt_token.json is written here. Done —
the orchestrator picks it up automatically.

If uploads later fail with invalid_grant: delete yt_token.json and run this
again. That means the refresh token expired (usually the Testing-mode 7-day cap).
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CLIENT = BASE / "client_secret.json"
TOKEN = BASE / "yt_token.json"

# youtube.upload is enough to post Shorts. We ask for nothing else.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    if not CLIENT.exists():
        print("❌ client_secret.json not found in", BASE)
        print("\n   This is the file YOU download from Google Cloud Console:")
        print("   Credentials -> Create credentials -> OAuth client ID -> Desktop app")
        print("   Rename the download to exactly 'client_secret.json' and put it here.")
        return 1

    try:
        data = json.loads(CLIENT.read_text())
    except json.JSONDecodeError:
        print("❌ client_secret.json is not valid JSON — re-download it.")
        return 1
    if not ({"installed", "web"} & set(data)):
        print("❌ That doesn't look like an OAuth client file.")
        print("   Expected a top-level 'installed' key (Desktop app type).")
        return 1
    if "web" in data and "installed" not in data:
        print("⚠️  This is a WEB client. Create a *Desktop app* client instead —")
        print("   the web type will reject the local redirect this script uses.")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("❌ Missing dependency. Run:")
        print("   pip install google-api-python-client google-auth-oauthlib")
        return 1

    if TOKEN.exists():
        print("ℹ️  yt_token.json already exists — replacing it.")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    print("\n🌐 Opening your browser… approve access for the channel you post to.")
    print("   (Pick the right Google account — whichever you choose is where "
          "Shorts get uploaded.)\n")
    try:
        creds = flow.run_local_server(port=0, prompt="consent")
    except Exception as exc:
        print(f"❌ OAuth flow failed: {exc}")
        print("   If a browser can't open here, run this on a desktop machine "
              "and copy yt_token.json across.")
        return 1

    TOKEN.write_text(creds.to_json())
    print(f"\n✅ Wrote {TOKEN}")
    if not creds.refresh_token:
        print("⚠️  No refresh token was issued — uploads will stop after ~1 hour.")
        print("   Revoke access at myaccount.google.com/permissions and re-run.")
        return 1

    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        me = yt.channels().list(part="snippet", mine=True).execute()
        items = me.get("items") or []
        if items:
            print(f"✅ Authorised channel: {items[0]['snippet']['title']}")
    except Exception:
        # youtube.upload alone can't always read channel info — not a failure.
        print("✅ Token written. (Channel name unavailable with upload-only scope.)")

    print("\nNow test it:  Telegram panel -> 🧪 Test Lab -> 🔧 Steps -> ▶️ YouTube")
    return 0


if __name__ == "__main__":
    sys.exit(main())
