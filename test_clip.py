"""Create a Twitch clip for a channel (the channel must be live)."""

import argparse
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv


API = "https://api.twitch.tv/helix"
TOKEN_FILE = Path(__file__).with_name("tokens.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", help="Twitch channel login name")
    args = parser.parse_args()

    if not TOKEN_FILE.exists():
        raise SystemExit("tokens.json not found; run auth.py first")
    access_token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["access_token"]

    # Twitch's Helix API requires the OAuth app's client ID alongside the token.
    load_dotenv(Path(__file__).with_name(".env"))
    client_id = os.getenv("TWITCH_CLIENT_ID")
    if not client_id:
        raise SystemExit("Set TWITCH_CLIENT_ID in .env")
    headers = {"Authorization": f"Bearer {access_token}", "Client-Id": client_id}

    try:
        user_response = requests.get(
            f"{API}/users", headers=headers, params={"login": args.channel}, timeout=30
        )
        user_response.raise_for_status()
        users = user_response.json().get("data", [])
        if not users:
            print(json.dumps({"error": f"Channel not found: {args.channel}"}, indent=2))
            return
        clip_response = requests.post(
            f"{API}/clips",
            headers=headers,
            params={"broadcaster_id": users[0]["id"]},
            timeout=30,
        )
        try:
            body = clip_response.json()
            print(json.dumps(body, indent=2))
        except ValueError:
            print(clip_response.text)
        if not clip_response.ok:
            raise SystemExit(1)
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            print(f"HTTP {response.status_code}: {response.text}")
        else:
            print(f"Request failed: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
