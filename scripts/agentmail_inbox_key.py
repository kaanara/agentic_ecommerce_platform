"""Create a least-privilege, inbox-scoped AgentMail key and store it in .env.

Asks for an org-level key with hidden input, uses it once, and writes only the new inbox key to .env.
Nothing secret is printed. Run in a real terminal:  uv run python scripts/agentmail_inbox_key.py
"""

from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
INBOX = "enrgy@agentmail.to"
PERMISSIONS = ["inbox_read", "message_read", "message_send", "message_update"]
API = "https://api.agentmail.to/v0"


def main() -> int:
    if not ENV.exists():
        print(f"{ENV} not found. Create it once with: cp -n .env.example .env && chmod 600 .env")
        return 1
    if not sys.stdin.isatty():
        print("This needs an interactive terminal for hidden input. Run it in Terminal.app.")
        return 1
    org_key = getpass.getpass("Paste an org-level AgentMail key (input is hidden), then Enter: ").strip()
    if not org_key:
        print("No key entered; nothing changed.")
        return 1

    r = httpx.post(f"{API}/inboxes/{INBOX}/api-keys", timeout=20,
                   headers={"Authorization": f"Bearer {org_key}"},
                   json={"name": "aecom-app-inbox", "permissions": {p: True for p in PERMISSIONS}})
    if r.status_code >= 400:
        print(f"AgentMail returned HTTP {r.status_code}: {r.text[:300]}")
        return 1
    new = r.json()

    text = ENV.read_text()
    line = "AGENTMAIL_API_KEY=" + new["api_key"]
    text = re.sub(r"^AGENTMAIL_API_KEY=.*$", line, text, flags=re.M) if re.search(r"^AGENTMAIL_API_KEY=", text, re.M) \
        else text.rstrip("\n") + "\n" + line + "\n"
    ENV.write_text(text)
    ENV.chmod(0o600)

    check = httpx.get(f"{API}/inboxes/{INBOX}", timeout=20, headers={"Authorization": f"Bearer {new['api_key']}"})
    print(f"saved key {new['prefix']}… | inbox: {new.get('inbox_id')} | permissions: {', '.join(PERMISSIONS)}")
    print("verified: new key can read the inbox" if check.status_code == 200 else f"warning: verification returned HTTP {check.status_code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
