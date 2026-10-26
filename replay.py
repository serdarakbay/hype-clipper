"""Replay a recorded Twitch chat JSONL file through the shared hype detector."""

import argparse
import json
from pathlib import Path

from detector import HypeDetector, PEAK_TIMEOUT


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
    args = parser.parse_args()

    if not args.path.is_file():
        raise SystemExit(f"File not found: {args.path}")
    messages = read_messages(args.path)
    if not messages:
        print("Total triggers: 0")
        print("Triggers per hour: 0.00")
        return

    started_at = messages[0][0]
    detector = HypeDetector(started_at, threshold=args.threshold)
    triggers = []
    for ts, user, text in messages:
        detector.add_message(ts, user, text)
        triggers.extend(detector.advance_to(ts))

    ended_at = messages[-1][0]
    triggers.extend(detector.advance_to(ended_at + PEAK_TIMEOUT))

    for trigger in triggers:
        offset = trigger.trigger_time - started_at
        print(
            f"TRIGGER offset={offset:.1f}s peak_rate={trigger.peak_rate:.2f} "
            f"top_words={','.join(trigger.top_words) or '(none)'}"
        )

    duration = ended_at - started_at
    per_hour = len(triggers) * 3600 / duration if duration > 0 else 0.0
    print(f"Total triggers: {len(triggers)}")
    print(f"Triggers per hour: {per_hour:.2f}")


if __name__ == "__main__":
    main()
