"""
One-time Gmail OAuth2 setup — run this locally once to get a refresh token.

Usage:
    .venv/bin/python scripts/gmail_auth.py

Reads GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET from .env.
Opens a browser for you to log in and grant access.
Prints the refresh token — copy it into .env and GitHub Secrets.
"""

import os
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

client_id = os.getenv("GMAIL_CLIENT_ID")
client_secret = os.getenv("GMAIL_CLIENT_SECRET")

if not client_id or not client_secret:
    raise SystemExit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in your .env first.")

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
creds = flow.run_local_server(port=0)

print("\n" + "=" * 60)
print("SUCCESS — add these to your .env and GitHub Secrets:")
print("=" * 60)
print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
print("=" * 60)
