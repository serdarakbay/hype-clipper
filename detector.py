"""Shared time-windowed chat hype detection for live watching and replay."""

from collections import Counter, deque
from dataclasses import dataclass
import re
import statistics


SAMPLE_INTERVAL = 2
WINDOW_SECONDS = 10
BASELINE_SECONDS = 5 * 60
WARMUP_SECONDS = 2 * 60
TRIGGER_SCORE = 3.0
TRIGGER_COOLDOWN = 90
PEAK_DROP_RATIO = 0.70
PEAK_TIMEOUT = 20
MIN_STDDEV = 0.01


def top_words(messages):
    """Return the three most frequent word/emote tokens in (ts, user, text) rows."""
    counts = Counter()
    for _ts, _user, text in messages:
        counts.update(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.casefold(), flags=re.UNICODE))
    return [word for word, _count in counts.most_common(3)]


@dataclass(frozen=True)
class Sample:
    time: float
    rate: float
    mean: float
    hype_score: float


@dataclass(frozen=True)
class Trigger:
    trigger_time: float
    clip_time: float
    peak_rate: float
    peak_hype_score: float
    top_words: list


class HypeDetector:
    """Sample message activity and resolve hype triggers at their peak."""

    def __init__(self, start_time, threshold=TRIGGER_SCORE):
        self.start_time = float(start_time)
        self.threshold = threshold
        self.next_sample = self.start_time
        self.messages = deque()  # (unix timestamp, user, text)
        self.baseline = deque()  # (timestamp, rate in messages per second)
        self.last_trigger_time = float("-inf")
        self.pending = None
        self.latest = Sample(self.start_time, 0.0, 0.0, 0.0)

    def add_message(self, ts, user, text):
        """Add a timestamped message. Call advance_to() to process due samples."""
        self.messages.append((float(ts), user, text))

    def _sample(self, now):
        cutoff = now - WINDOW_SECONDS
        while self.messages and self.messages[0][0] <= cutoff:
            self.messages.popleft()
        active = [row for row in self.messages if cutoff < row[0] <= now]
        rate = len(active) / WINDOW_SECONDS

        while self.baseline and now - self.baseline[0][0] > BASELINE_SECONDS:
            self.baseline.popleft()
        if self.baseline:
            mean = statistics.fmean(value for _ts, value in self.baseline)
            stddev = statistics.pstdev(value for _ts, value in self.baseline)
            score = (rate - mean) / max(stddev, MIN_STDDEV)
        else:
            mean, score = 0.0, 0.0
        sample = Sample(now, rate, mean, score)
        self.latest = sample

        if self.pending is None:
            warmed_up = now - self.start_time >= WARMUP_SECONDS
            cooldown_done = now - self.last_trigger_time >= TRIGGER_COOLDOWN
            if warmed_up and cooldown_done and score > self.threshold:
                self.pending = {
                    "trigger_time": now,
                    "peak_rate": rate,
                    "peak_hype_score": score,
                    "top_words": top_words(active),
                }
        else:
            pending = self.pending
            if rate > pending["peak_rate"]:
                pending["peak_rate"] = rate
                pending["top_words"] = top_words(active)
            pending["peak_hype_score"] = max(pending["peak_hype_score"], score)
            fell_from_peak = rate < pending["peak_rate"] * PEAK_DROP_RATIO
            timed_out = now - pending["trigger_time"] >= PEAK_TIMEOUT
            if fell_from_peak or timed_out:
                trigger = Trigger(
                    trigger_time=pending["trigger_time"],
                    clip_time=now,
                    peak_rate=pending["peak_rate"],
                    peak_hype_score=pending["peak_hype_score"],
                    top_words=pending["top_words"],
                )
                self.pending = None
                self.last_trigger_time = pending["trigger_time"]
                self.baseline.append((now, rate))
                return sample, trigger

        self.baseline.append((now, rate))
        return sample, None

    def advance_to(self, now):
        """Process all 2-second sample boundaries through the given timestamp."""
        completed = []
        now = float(now)
        while self.next_sample <= now:
            _sample, trigger = self._sample(self.next_sample)
            if trigger is not None:
                completed.append(trigger)
            self.next_sample += SAMPLE_INTERVAL
        return completed
