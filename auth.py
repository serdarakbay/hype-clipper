"""Authorize this application with Twitch and save its user tokens."""

import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv


REDIRECT_URI = "http://localhost:3000"
SCOPES = "clips:edit user:read:chat user:write:chat moderator:manage:announcements"
TOKEN_FILE = Path(__file__).with_name("tokens.json")


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env")

    state = secrets.token_urlsafe(24)
    result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if params.get("state", [None])[0] != state:
                result["error"] = "OAuth state did not match"
                status, message = 400, "Authorization state mismatch. You can close this page."
            elif "error" in params:
                result["error"] = params.get("error_description", params["error"])[0]
                status, message = 400, "Twitch authorization failed. You can close this page."
            elif "code" not in params:
                result["error"] = "Twitch did not return an authorization code"
                status, message = 400, "No authorization code received. You can close this page."
            else:
                result["code"] = params["code"][0]
                status, message = 200, "Authorization received. You can close this page."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))

        def log_message(self, _format, *_args):
            pass

    try:
        server = HTTPServer(("localhost", 3000), CallbackHandler)
        server.timeout = 1
    except OSError as exc:
        raise SystemExit(f"Could not start callback server on localhost:3000: {exc}") from exc

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )
    authorize_url = f"https://id.twitch.tv/oauth2/authorize?{query}"
    print("Waiting for Twitch authorization at http://localhost:3000 ...")
    threading.Timer(0.5, lambda: webbrowser.open(authorize_url)).start()
    try:
        while not result:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise SystemExit(result["error"])

    response = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if not response.ok:
        raise SystemExit(f"Token exchange failed ({response.status_code}): {response.text}")

    token_data = response.json()
    tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
    }
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    print(f"Tokens saved to {TOKEN_FILE}")


if __name__ == "__main__":
    main()
