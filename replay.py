"""Replay a recorded Twitch chat JSONL file through the shared hype detector."""

import argparse
import json
from pathlib import Path
import time

from detector import HypeDetector, PEAK_HARD_TIMEOUT, SENSITIVITY_RATIOS, WINDOW_SECONDS, clip_duration
import overlay as obs_overlay


def read_messages(path):
    messages = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                ts = float(message["ts"])
                user = message["user"]
                text = message["text"]
            except (ValueError, KeyError, TypeError) as exc:
                raise SystemExit(f"Invalid JSONL record on line {line_number}: {exc}") from exc
            if messages and ts < messages[-1][0]:
                raise SystemExit(f"Message timestamps go backwards on line {line_number}")
            messages.append((ts, user, text))
    return messages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Recorded chat JSONL file")
    parser.add_argument("--threshold", type=float, default=3.0, help="Hype score threshold (default: 3.0)")
    parser.add_argument("--start", type=float, default=0.0, help="Start at this many seconds into the recording")
    parser.add_argument("--speed", type=float, help="Replay at this multiple of recorded time")
    parser.add_argument("--overlay", action="store_true", help="Serve and update the OBS overlay")
    parser.add_argument(
        "--sensitivity", choices=tuple(SENSITIVITY_RATIOS), default="medium",
        help="Minimum chatter ratio for a trigger (default: medium)",
    )
    args = parser.parse_args()

    if args.speed is not None and args.speed <= 0:
        parser.error("--speed must be greater than zero")
    if args.start < 0:
        parser.error("--start must be zero or greater")
    if args.overlay:
        obs_overlay.start_server()
        print("OBS overlay: http://localhost:8765")
    if not args.path.is_file():
        raise SystemExit(f"File not found: {args.path}")
    messages = read_messages(args.path)
    if not messages:
        print("Total sessions: 0")
        print("Total clips: 0")
        print("Clips per hour: 0.00")
        return

    started_at = messages[0][0]
    detector = HypeDetector(
        started_at,
        threshold=args.threshold,
        min_ratio=SENSITIVITY_RATIOS[args.sensitivity],
    )
    ended_at = messages[-1][0]
    replay_started_at = started_at + args.start
    next_message = 0
    while next_message < len(messages) and messages[next_message][0] < replay_started_at:
        ts, user, text = messages[next_message]
        detector.add_message(ts, user, text)
        next_message += 1
    if args.start > 0:
        detector.advance_to(replay_started_at, detect=False)

    replay_end = ended_at + WINDOW_SECONDS + PEAK_HARD_TIMEOUT
    replay_clock = time.monotonic()
    next_status = replay_started_at
    session_count = 0
    clip_count = 0
    while True:
        candidates = [replay_end, detector.next_sample]
        if next_message < len(messages):
            candidates.append(messages[next_message][0])
        if args.overlay:
            candidates.append(next_status)
        target = min(candidates)

        if args.speed is not None:
            target_real = replay_clock + (target - replay_started_at) / args.speed
            delay = target_real - time.monotonic()
            if delay > 0:
                time.sleep(delay)

        while next_message < len(messages) and messages[next_message][0] <= target:
            ts, user, text = messages[next_message]
            detector.add_message(ts, user, text)
            next_message += 1

        completed = detector.advance_to(target)
        notices = detector.drain_notices()
        for notice in notices:
            if notice["type"] == "session_start":
                session_count += 1
                offset = notice["time"] - started_at
                print(f"SESSION start session_id={notice['session_id']} offset={offset:.1f}s")
            elif notice["type"] == "session_end":
                offset = notice["time"] - started_at
                print(
                    f"SESSION end session_id={notice['session_id']} offset={offset:.1f}s "
                    f"max_ratio={notice['max_ratio']:.2f}x "
                    f"max_tier={notice['max_tier'] or '(none)'}"
                )
        if args.overlay:
            for notice in notices:
                obs_overlay.publish(notice)
            if target >= next_status:
                sample = detector.latest
                obs_overlay.publish({
                    "type": "status",
                    "unique_chatters": detector.current_unique_chatters(target),
                    "mean": sample.mean,
                    "hype_score": sample.hype_score,
                })
                next_status += 1

        for trigger in completed:
            clip_count += 1
            tier = trigger.tier
            title = f"{tier} moment"
            detector.mark_clipped(trigger)
            if args.overlay:
                obs_overlay.publish({"type": "clip", "title": title, "url": None})
            wave_start_offset = trigger.trigger_time - started_at
            clip_offset = trigger.clip_time - started_at
            duration = clip_duration(trigger.trigger_time, trigger.clip_time)
            print(
                f"CLIP session_id={trigger.session_id} upgrade={str(trigger.upgrade).lower()} "
                f"wave_start_offset={wave_start_offset:.1f}s clip_offset={clip_offset:.1f}s tier={tier} "
                f"duration={duration:.1f}s "
                f"peak_rate={trigger.peak_rate:.2f} "
                f"peak_ratio={trigger.peak_ratio:.2f}x "
                f"top_words={','.join(trigger.top_words) or '(none)'}"
            )
        if target >= replay_end or (
            next_message >= len(messages)
            and target >= ended_at + WINDOW_SECONDS
            and detector.pending is None
            and not detector.session_active
        ):
            break

    duration = max(0.0, ended_at - replay_started_at)
    per_hour = clip_count * 3600 / duration if duration > 0 else 0.0
    print(f"Total sessions: {session_count}")
    print(f"Total clips: {clip_count}")
    print(f"Clips per hour: {per_hour:.2f}")


if __name__ == "__main__":
    main()
