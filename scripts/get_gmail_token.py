"""One-time helper: mint a Gmail API refresh token for invespend.

The weekly report / statements are delivered via the Gmail API (see
``src/invespend/emailer.py``), which needs three secrets:

    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN

The first two come from the OAuth *Desktop* client you create in Google Cloud
Console → APIs & Services → Credentials (download its JSON). This script
exchanges that client JSON for the long-lived refresh token by walking you
through the browser consent flow once.

Usage
-----
    pip install google-auth-oauthlib
    python scripts/get_gmail_token.py path/to/client_secret_xxx.json

If you omit the path it looks for ``client_secret.json`` in the current
directory. A browser tab opens; pick the Gmail account that will *send* the
reports, click through the "unverified app" warning (Advanced → Go to …), and
Allow. The three values you need are printed at the end — copy them into your
local ``.env`` and your GitHub Actions secrets.

Notes
-----
* ``access_type=offline`` + ``prompt=consent`` force Google to return a refresh
  token even if you have authorised this client before.
* If your OAuth consent screen is still in "Testing" mode the refresh token
  expires after 7 days — publish the app to "Production" so it lasts.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Only the send scope is needed — least privilege; this token cannot read mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def main(argv: list[str]) -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Missing dependency. Install it first:\n"
            "    pip install google-auth-oauthlib",
            file=sys.stderr,
        )
        return 1

    client_file = Path(argv[1]) if len(argv) > 1 else Path("client_secret.json")
    if not client_file.exists():
        print(
            f"Client secret JSON not found: {client_file}\n"
            "Download it from Google Cloud Console → Credentials → your Desktop "
            "OAuth client, then pass its path:\n"
            "    python scripts/get_gmail_token.py path/to/client_secret_xxx.json",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes=SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print(
            "No refresh token was returned. Re-run after revoking the app's access "
            "at https://myaccount.google.com/permissions (Google only issues a "
            "refresh token on first consent unless prompt=consent is forced).",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 70)
    print("Add these to your .env (local) and GitHub Actions secrets (CI):\n")
    print(f"GMAIL_CLIENT_ID={creds.client_id}")
    print(f"GMAIL_CLIENT_SECRET={creds.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
