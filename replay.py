"""Replay a recorded Twitch chat JSONL file through the shared hype detector."""

import argparse
import json
from pathlib import Path
import time

from detector import HypeDetector, PEAK_TIMEOUT, SENSITIVITY_RATIOS
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
    parser.add_argument("--speed", type=float, help="Replay at this multiple of recorded time")
    parser.add_argument("--overlay", action="store_true", help="Serve and update the OBS overlay")
    parser.add_argument(
        "--sensitivity", choices=tuple(SENSITIVITY_RATIOS), default="medium",
        help="Minimum chatter ratio for a trigger (default: medium)",
    )
    args = parser.parse_args()

    if args.speed is not None and args.speed <= 0:
        parser.error("--speed must be greater than zero")
    if args.overlay:
        obs_overlay.start_server()
        print("OBS overlay: http://localhost:8765")
    if not args.path.is_file():
        raise SystemExit(f"File not found: {args.path}")
    messages = read_messages(args.path)
    if not messages:
        print("Total triggers: 0")
        print("Triggers per hour: 0.00")
        return

    started_at = messages[0][0]
    detector = HypeDetector(
        started_at,
        threshold=args.threshold,
        min_ratio=SENSITIVITY_RATIOS[args.sensitivity],
    )
    ended_at = messages[-1][0]
    replay_end = ended_at + PEAK_TIMEOUT
    replay_clock = time.monotonic()
    next_message = 0
    next_status = started_at
    trigger_count = 0
    while True:
        candidates = [replay_end, detector.next_sample]
        if next_message < len(messages):
            candidates.append(messages[next_message][0])
        if args.overlay:
            candidates.append(next_status)
        target = min(candidates)

        if args.speed is not None:
            target_real = replay_clock + (target - started_at) / args.speed
            delay = target_real - time.monotonic()
            if delay > 0:
                time.sleep(delay)

        while next_message < len(messages) and messages[next_message][0] <= target:
            ts, user, text = messages[next_message]
            detector.add_message(ts, user, text)
            next_message += 1

        completed = detector.advance_to(target)
        notices = detector.drain_notices()
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
            trigger_count += 1
            tier = trigger.tier
            title = f"{tier} moment"
            if args.overlay:
                obs_overlay.publish({"type": "clip", "title": title, "url": None})
            offset = trigger.trigger_time - started_at
            print(
                f"TRIGGER offset={offset:.1f}s tier={tier} "
                f"peak_rate={trigger.peak_rate:.2f} "
                f"peak_ratio={trigger.peak_ratio:.2f}x "
                f"top_words={','.join(trigger.top_words) or '(none)'}"
            )
        if target >= replay_end:
            break

    duration = ended_at - started_at
    per_hour = trigger_count * 3600 / duration if duration > 0 else 0.0
    print(f"Total triggers: {trigger_count}")
    print(f"Triggers per hour: {per_hour:.2f}")


if __name__ == "__main__":
    main()
