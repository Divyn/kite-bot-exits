"""
Kite Connect Authentication Setup
----------------------------------
1. Put API_KEY and API_SECRET in a .env file next to this script
2. Run this script: python3 auth.py
3. A browser window will open — log in with your Zerodha credentials
4. After login, the access_token is saved to access_token.txt automatically
5. Your trading bot can read access_token.txt each morning
"""

import hashlib
import json
import os
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
import requests

load_dotenv()

# ─── CONFIGURE THESE ───────────────────────────────────────────────────────────
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
REDIRECT_URL = "http://127.0.0.1:5000"
# ────────────────────────────────────────────────────────────────────────────────

TOKEN_FILE = "access_token.txt"
LOGIN_URL  = f"https://kite.zerodha.com/connect/login?v=3&api_key={API_KEY}"


def compute_checksum(api_key: str, request_token: str, api_secret: str) -> str:
    raw = api_key + request_token + api_secret
    return hashlib.sha256(raw.encode()).hexdigest()


def exchange_token(request_token: str) -> str:
    checksum = compute_checksum(API_KEY, request_token, API_SECRET)
    response = requests.post(
        "https://api.kite.trade/session/token",
        data={
            "api_key": API_KEY,
            "request_token": request_token,
            "checksum": checksum,
        },
        headers={"X-Kite-Version": "3"},
    )
    data = response.json()
    if response.status_code != 200 or "data" not in data:
        raise RuntimeError(f"Token exchange failed: {data}")
    return data["data"]["access_token"]


def save_token(access_token: str):
    payload = {
        "access_token": access_token,
        "date": str(date.today()),
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[✓] Access token saved to {TOKEN_FILE}")


def load_token() -> str | None:
    """Call this from your bot to get today's token."""
    try:
        with open(TOKEN_FILE) as f:
            payload = json.load(f)
        if payload.get("date") == str(date.today()):
            return payload["access_token"]
        print("[!] Token is from a previous day — re-authenticate.")
        return None
    except FileNotFoundError:
        return None


# ─── Local HTTP server to catch the redirect ────────────────────────────────────

class CallbackHandler(BaseHTTPRequestHandler):
    access_token = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "request_token" not in params:
            self._respond(400, "Missing request_token in redirect.")
            return

        request_token = params["request_token"][0]
        print(f"[✓] Got request_token: {request_token}")

        try:
            access_token = exchange_token(request_token)
            save_token(access_token)
            CallbackHandler.access_token = access_token
            self._respond(200, "Authentication successful! You can close this tab.")
        except Exception as e:
            self._respond(500, f"Error: {e}")

    def _respond(self, code: int, message: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, format, *args):
        pass  # suppress default server logs


def authenticate() -> str:
    """
    Opens browser for Zerodha login, waits for redirect,
    exchanges token, and returns the access_token.
    """
    print(f"[→] Opening Zerodha login in your browser...")
    webbrowser.open(LOGIN_URL)

    print(f"[→] Waiting for redirect on {REDIRECT_URL} ...")
    server = HTTPServer(("127.0.0.1", 5000), CallbackHandler)
    server.handle_request()  # handles exactly one request then stops

    token = CallbackHandler.access_token
    if not token:
        raise RuntimeError("Authentication failed — no token received.")

    print(f"[✓] Authenticated successfully.")
    return token


# ─── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Try loading a saved token first (avoids re-login if run twice in a day)
    token = load_token()
    if token:
        print(f"[✓] Using saved token from today: {token[:10]}...")
    else:
        token = authenticate()

    print(f"\nYour access_token (first 10 chars): {token[:10]}...")
    print("You're ready to trade. Import load_token() in your bot.")