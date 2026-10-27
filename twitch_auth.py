"""Shared Twitch user-token validation, refresh, and Helix request handling."""

import json
import os
from pathlib import Path
import threading

import requests
from dotenv import load_dotenv


VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TOKEN_FILE = Path(__file__).with_name("tokens.json")
VALIDATE_INTERVAL = 60 * 60


class TwitchAuth:
    def __init__(self, client_id, client_secret, token_file=TOKEN_FILE):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = Path(token_file)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._load_tokens()
        self.validate()

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).with_name(".env"))
        client_id = os.getenv("TWITCH_CLIENT_ID")
        client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SystemExit("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env")
        return cls(client_id, client_secret)

    def _load_tokens(self):
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
            self.access_token = data["access_token"]
            self.refresh_token = data["refresh_token"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SystemExit(f"Could not read access_token and refresh_token from {self.token_file}: {exc}") from exc

    def _save_tokens(self):
        data = {"access_token": self.access_token, "refresh_token": self.refresh_token}
        temporary = self.token_file.with_suffix(self.token_file.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.token_file)

    def _validate_locked(self):
        response = requests.get(
            VALIDATE_URL,
            headers={"Authorization": f"OAuth {self.access_token}"},
            timeout=30,
        )
        if response.status_code == 401:
            self._refresh_locked()
            response = requests.get(
                VALIDATE_URL,
                headers={"Authorization": f"OAuth {self.access_token}"},
                timeout=30,
            )
        response.raise_for_status()

    def validate(self):
        with self._lock:
            self._validate_locked()

    def _refresh_locked(self):
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        self.access_token = data["access_token"]
        # Twitch rotates refresh tokens; retain the current one if none is returned.
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self._save_tokens()

    def refresh(self):
        with self._lock:
            self._refresh_locked()

    def request(self, method, url, **kwargs):
        """Send a Helix request, refreshing and retrying once after HTTP 401."""
        supplied_headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(2):
            with self._lock:
                headers = dict(supplied_headers)
                headers["Authorization"] = f"Bearer {self.access_token}"
                headers["Client-Id"] = self.client_id
                response = requests.request(method, url, headers=headers, **kwargs)
                if response.status_code != 401 or attempt:
                    return response
                self._refresh_locked()
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def start_periodic_validation(self):
        if self._thread is not None:
            return

        def validate_periodically():
            while not self._stop.wait(VALIDATE_INTERVAL):
                try:
                    self.validate()
                except requests.RequestException as exc:
                    print(f"Twitch token validation failed: {exc}", flush=True)

        self._thread = threading.Thread(
            target=validate_periodically, name="twitch-token-validator", daemon=True
        )
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
