"""Create a Twitch clip for a channel (the channel must be live)."""

import argparse
import json

import requests
from twitch_auth import TwitchAuth


API = "https://api.twitch.tv/helix"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", help="Twitch channel login name")
    args = parser.parse_args()

    auth = TwitchAuth.from_env()

    try:
        user_response = auth.get(
            f"{API}/users", params={"login": args.channel}, timeout=30
        )
        user_response.raise_for_status()
        users = user_response.json().get("data", [])
        if not users:
            print(json.dumps({"error": f"Channel not found: {args.channel}"}, indent=2))
            return
        clip_response = auth.post(
            f"{API}/clips",
            params={"broadcaster_id": users[0]["id"], "has_delay": "true"},
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
    finally:
        auth.close()


if __name__ == "__main__":
    main()
