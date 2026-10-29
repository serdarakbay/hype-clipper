"""Watch Twitch chat and create a clip when chat activity spikes."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import socket
import ssl
import threading
import time

import requests

from detector import HypeDetector, SENSITIVITY_RATIOS, chatter_ratio, clip_duration, tier_for_ratio
import overlay as obs_overlay
from twitch_auth import TwitchAuth


API = "https://api.twitch.tv/helix"
CHAT_HOST = "irc.chat.twitch.tv"
CHAT_PORT = 6697
STATUS_INTERVAL = 10


def get_broadcaster_id(channel, auth):
    response = auth.get(
        f"{API}/users", params={"login": channel}, timeout=30
    )
    response.raise_for_status()
    users = response.json().get("data", [])
    if not users:
        raise SystemExit(f"Channel not found: {channel}")
    return users[0]["id"]


def get_channel_game_name(broadcaster_id, auth):
    """Return the monitored channel's current game name, if Helix provides it."""
    try:
        response = auth.get(
            f"{API}/channels", params={"broadcaster_id": broadcaster_id}, timeout=30
        )
        response.raise_for_status()
        channels = response.json().get("data", [])
        if not channels:
            return None
        return (channels[0].get("game_name") or "").strip() or None
    except requests.RequestException as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        print(f"Could not look up channel category; omitting it from clip title: {detail}")
        return None


def make_clip_title(tier, clip_number, game_name, clip_datetime, upgrade=False):
    tier_label = f"{tier} (upgrade)" if upgrade else tier
    prefix = f"⚡ {tier_label} #{clip_number}"
    clock = clip_datetime.strftime("%H:%M")
    if not game_name:
        return f"{prefix} · {clock}"

    separator = f"{prefix} · "
    suffix = f" · {clock}"
    game_limit = max(0, 100 - len(separator) - len(suffix))
    if game_limit == 0:
        return f"{prefix} · {clock}"
    game = game_name.strip()
    if len(game) > game_limit:
        game = game[:game_limit - 1] + "…"
    return f"{separator}{game}{suffix}"


def get_authenticated_user(auth):
    """Get the channel owner represented by the saved user access token."""
    response = auth.get(f"{API}/users", timeout=30)
    response.raise_for_status()
    users = response.json().get("data", [])
    if not users:
        raise SystemExit("Twitch did not return the authenticated user")
    return users[0]


def send_chat_message(owner_id, message, auth, reply_parent_message_id=None):
    body = {"broadcaster_id": owner_id, "sender_id": owner_id, "message": message}
    if reply_parent_message_id:
        body["reply_parent_message_id"] = reply_parent_message_id
    response = auth.post(
        f"{API}/chat/messages",
        json=body,
        timeout=30,
    )
    if not response.ok:
        print(f"Chat reply failed (HTTP {response.status_code}): {response.text}")
        return
    body = response.json()
    if body.get("data") and not body["data"][0].get("is_sent", True):
        print(f"Chat reply was not sent: {json.dumps(body)}")


def send_announcement(owner_id, message, auth):
    response = auth.post(
        f"{API}/chat/announcements",
        params={"broadcaster_id": owner_id, "moderator_id": owner_id},
        json={"message": message, "color": "purple"},
        timeout=30,
    )
    if not response.ok:
        print(f"Announcement failed (HTTP {response.status_code}): {response.text}")


def create_clip(broadcaster_id, title, duration, auth):
    response = auth.post(
        f"{API}/clips",
        params={
            "broadcaster_id": broadcaster_id,
            "title": title,
            "duration": duration,
            "has_delay": "true",
        },
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"error": response.text}
    if not response.ok:
        return None, f"HTTP {response.status_code}: {json.dumps(body)}"
    data = body.get("data", [])
    if not data or not data[0].get("id"):
        return None, f"No clip ID in response: {json.dumps(body)}"
    return f"https://clips.twitch.tv/{data[0]['id']}", None


def parse_privmsg(line):
    """Return (channel, login, message, message_id) for an IRC PRIVMSG, or None."""
    tags = {}
    if line.startswith("@"):
        raw_tags, line = line.split(" ", 1)
        for pair in raw_tags[1:].split(";"):
            key, separator, value = pair.partition("=")
            if separator:
                tags[key] = value
    if " PRIVMSG " not in line:
        return None
    prefix, payload = line.split(" PRIVMSG ", 1)
    if " :" not in payload:
        return None
    channel = payload.split(" ", 1)[0].lstrip("#").casefold()
    user = prefix.split("!", 1)[0].lstrip(":")
    return channel, user, payload.split(" :", 1)[1], tags.get("id")


def connect_chat(*channels):
    context = ssl.create_default_context()
    raw_sock = socket.create_connection((CHAT_HOST, CHAT_PORT), timeout=15)
    try:
        sock = context.wrap_socket(raw_sock, server_hostname=CHAT_HOST)
    except Exception:
        raw_sock.close()
        raise
    sock.settimeout(1)
    nick = f"justinfan{secrets.randbelow(900000) + 100000}"
    commands = ["CAP REQ :twitch.tv/tags", f"NICK {nick}"]
    commands.extend(f"JOIN #{channel}" for channel in dict.fromkeys(channels))
    sock.sendall(("\r\n".join(commands) + "\r\n").encode("utf-8"))
    return sock


def iso_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", help="Twitch channel login name")
    parser.add_argument("--dry-run", action="store_true", help="Detect hype without creating clips")
    parser.add_argument("--record", action="store_true", help="Append chat messages to a JSONL file")
    parser.add_argument("--overlay", action="store_true", help="Serve the OBS overlay on localhost:8765")
    parser.add_argument("--announce", action="store_true", help="Announce clip moments in your own channel")
    parser.add_argument(
        "--sensitivity", choices=tuple(SENSITIVITY_RATIOS), default="medium",
        help="Minimum chatter ratio for a trigger (default: medium)",
    )
    args = parser.parse_args()
    channel = args.channel.lstrip("#").lower()

    auth = TwitchAuth.from_env()
    auth.start_periodic_validation()
    try:
        owner = get_authenticated_user(auth)
    except requests.RequestException as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise SystemExit(f"Could not look up authenticated Twitch user: {detail}") from exc
    owner_id = owner["id"]
    owner_login = owner["login"].casefold()

    try:
        broadcaster_id = get_broadcaster_id(channel, auth)
    except requests.RequestException as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise SystemExit(f"Could not look up channel: {detail}") from exc

    record_file = None
    last_record_flush = time.monotonic()
    if args.record:
        data_dir = Path(__file__).with_name("data")
        data_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        record_file = (data_dir / f"chat_{channel}_{stamp}.jsonl").open("a", encoding="utf-8")

    def write_chat_record(ts, user, message):
        nonlocal last_record_flush
        record_file.write(
            json.dumps({"ts": ts, "user": user, "text": message}, ensure_ascii=False)
            + "\n"
        )
        now = time.monotonic()
        if now - last_record_flush >= 5:
            record_file.flush()
            last_record_flush = now

    started_at = time.time()
    detector = HypeDetector(started_at, min_ratio=SENSITIVITY_RATIOS[args.sensitivity])
    session_clips = []
    last_command_reply = {"clips": float("-inf"), "hype": float("-inf")}
    next_status = started_at + STATUS_INTERVAL
    clip_number = 0
    stop_status = threading.Event()
    status_thread = None
    pending = ""
    sock = None
    print("Watching #{}; press Ctrl+C to stop.".format(channel))
    if args.overlay:
        obs_overlay.start_server()
        print("OBS overlay: http://localhost:8765")

        def publish_status():
            while not stop_status.wait(1):
                sample = detector.latest
                obs_overlay.publish({
                    "type": "status",
                    "unique_chatters": sample.unique_chatters,
                    "mean": sample.mean,
                    "hype_score": sample.hype_score,
                })

        status_thread = threading.Thread(target=publish_status, name="overlay-status", daemon=True)
        status_thread.start()

    def finish_triggers(triggers):
        nonlocal clip_number
        for trigger in triggers:
            clip_number += 1
            game_name = get_channel_game_name(broadcaster_id, auth)
            clip_time = time.time()
            clip_datetime = datetime.fromtimestamp(clip_time).astimezone()
            duration = clip_duration(trigger.trigger_time, clip_time)
            clip_url = None
            clip_error = None
            tier = trigger.tier
            title = make_clip_title(
                tier, clip_number, game_name, clip_datetime, upgrade=trigger.upgrade
            )
            if not args.dry_run:
                try:
                    clip_url, clip_error = create_clip(broadcaster_id, title, duration, auth)
                except requests.RequestException as exc:
                    clip_error = exc.response.text if exc.response is not None else str(exc)
                if clip_url:
                    session_clips.append({"tier": tier, "url": clip_url})

            if clip_url or args.dry_run:
                detector.mark_clipped(trigger)

            if args.announce and clip_url:
                announcement = f"{title} {clip_url}"
                try:
                    send_announcement(owner_id, announcement, auth)
                except requests.RequestException as exc:
                    detail = exc.response.text if exc.response is not None else str(exc)
                    print(f"Announcement failed: {detail}")

            event = {
                "title": title,
                "session_id": trigger.session_id,
                "upgrade": trigger.upgrade,
                "trigger_time": iso_time(trigger.trigger_time),
                "clip_time": iso_time(clip_time),
                "duration": duration,
                "peak_rate": trigger.peak_rate,
                "trigger_median": trigger.trigger_median,
                "peak_ratio": trigger.peak_ratio,
                "peak_hype_score": trigger.peak_hype_score,
                "top_words": trigger.top_words,
                "clip_url": clip_url,
            }
            if clip_error:
                event["clip_error"] = clip_error
            with Path(__file__).with_name("clips.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
            if args.overlay and (clip_url or args.dry_run):
                obs_overlay.publish({
                    "type": "clip",
                    "title": event["title"],
                    "url": clip_url,
                })
            print(
                f"CLIP session_id={trigger.session_id} upgrade={str(trigger.upgrade).lower()} "
                f"tier={tier} trigger_time={event['trigger_time']} clip_time={event['clip_time']} "
                f"duration={duration:.1f}s "
                f"peak_rate={trigger.peak_rate:.2f} peak_hype_score={trigger.peak_hype_score:.2f} "
                f"peak_ratio={trigger.peak_ratio:.2f}x "
                f"clip_url={clip_url or 'null'}"
                + (f" error={clip_error}" if clip_error else ""),
                flush=True,
            )

    def handle_owner_command(message, message_id):
        command = message.strip().casefold()
        if command not in {"!clips", "!hype"}:
            return
        key = command[1:]
        now = time.monotonic()
        if now - last_command_reply[key] < 10:
            return
        last_command_reply[key] = now

        if key == "clips":
            recent = list(reversed(session_clips[-3:]))
            reply = " | ".join(f"{clip['tier']} {clip['url']}" for clip in recent) if recent else "No clips yet"
        else:
            sample = detector.latest
            ratio = chatter_ratio(sample.unique_chatters, sample.median)
            tier = tier_for_ratio(ratio)
            level = {"NICE": 1, "GREAT": 2, "INSANE": 3, "LEGENDARY": 4}.get(tier, 0)
            reply = f"LEVEL {level} {tier or 'Building up'} · ratio {ratio:.2f}x · hype_score {sample.hype_score:.2f}"
        try:
            send_chat_message(owner_id, reply, auth, message_id)
        except requests.RequestException as exc:
            detail = exc.response.text if exc.response is not None else str(exc)
            print(f"Chat reply failed: {detail}")

    try:
        sock = connect_chat(channel, owner_login)
        while True:
            now = time.time()
            completed = detector.advance_to(now)
            notices = detector.drain_notices()
            if args.overlay:
                for notice in notices:
                    obs_overlay.publish(notice)
            finish_triggers(completed)
            if now >= next_status:
                sample = detector.latest
                print(
                    f"unique_chatters={sample.unique_chatters} median={sample.median:.2f} "
                    f"hype_score={sample.hype_score:.2f}",
                    flush=True,
                )
                next_status = now + STATUS_INTERVAL

            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("Twitch chat disconnected")
            pending += data.decode("utf-8", errors="replace")
            lines = pending.split("\r\n")
            pending = lines.pop()
            for line in lines:
                if line.startswith("PING "):
                    sock.sendall(("PONG " + line[5:] + "\r\n").encode("utf-8"))
                    continue
                parsed = parse_privmsg(line)
                if not parsed:
                    continue
                chat_channel, user, message, message_id = parsed
                ts = time.time()
                if chat_channel == owner_login:
                    handle_owner_command(message, message_id)
                if chat_channel == channel:
                    detector.add_message(ts, user, message)
                if record_file is not None and chat_channel == channel:
                    write_chat_record(ts, user, message)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop_status.set()
        if status_thread is not None:
            status_thread.join(timeout=2)
        if sock is not None:
            sock.close()
        if record_file:
            record_file.flush()
            record_file.close()
        auth.close()


if __name__ == "__main__":
    main()
