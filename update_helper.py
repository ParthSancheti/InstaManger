import sys
import os
import requests

def main():
    if not os.path.exists(".env"):
        print("No .env found, skipping Telegram notification.")
        return
        
    # Manual dotenv parsing to avoid python-dotenv requirement issues in raw bat
    env_vars = {}
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip("'").strip('"')

    token = env_vars.get("TELEGRAM_BOT_TOKEN")
    chat_id = env_vars.get("TELEGRAM_ADMIN_ID")
    
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_ADMIN_ID in .env")
        return
        
    git_code = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    
    log = "No log found."
    if os.path.exists("update_log.txt"):
        with open("update_log.txt", "r", encoding="utf-8") as f:
            log = f.read().strip()
            
    if git_code == 0:
        msg = f"✅ <b>Auto-Update Successful</b>\n<pre>{log}</pre>\n\n<i>Restarting orchestrator...</i>"
    else:
        msg = f"❌ <b>Auto-Update Failed</b>\n<pre>{log}</pre>\n\nPlease connect to the VPS to resolve the git conflict manually."

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        print(f"Telegram notification sent. Status: {r.status_code}")
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")

if __name__ == "__main__":
    main()
