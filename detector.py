"""Shared time-windowed chat hype detection for live watching and replay."""

from collections import Counter, deque
from dataclasses import dataclass
import re
import statistics


SAMPLE_INTERVAL = 2
WINDOW_SECONDS = 10
BASELINE_SECONDS = 5 * 60
WARMUP_SECONDS = 3 * 60
WARMUP_SAMPLES = WARMUP_SECONDS // SAMPLE_INTERVAL
TRIGGER_SCORE = 3.0
TRIGGER_COOLDOWN = 90
PEAK_DROP_RATIO = 0.70
PEAK_TIMEOUT = 20
RELATIVE_MAD_FLOOR = 0.1
ABSOLUTE_MAD_FLOOR = 1.0
SESSION_END_SCORE = 1.0
BOT_USERS = frozenset({"nightbot", "streamelements", "fossabot", "moobot", "streamlabs"})
TIER_RATIO_THRESHOLDS = ((2.0, "NICE"), (3.0, "GREAT"), (4.0, "INSANE"), (6.0, "LEGENDARY"))
SENSITIVITY_RATIOS = {"low": 3.0, "medium": 2.5, "high": 2.0}


def chatter_ratio(unique_chatters, median):
    """Return an activity ratio; a zero baseline has no meaningful ratio."""
    return unique_chatters / median if median > 0 else 0.0


def tier_for_ratio(ratio):
    tier = None
    for minimum_ratio, name in TIER_RATIO_THRESHOLDS:
        if ratio >= minimum_ratio:
            tier = name
    return tier


def top_words(messages):
    """Return the three most frequent word/emote tokens in (ts, user, text) rows."""
    counts = Counter()
    for _ts, _user, text in messages:
        counts.update(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.casefold(), flags=re.UNICODE))
    return [word for word, _count in counts.most_common(3)]


@dataclass(frozen=True)
class Sample:
    time: float
    unique_chatters: int
    median: float
    hype_score: float

    @property
    def mean(self):
        """Compatibility alias for the baseline statistic in older status consumers."""
        return self.median

    @property
    def rate(self):
        """Compatibility name: activity is now unique chatters per 10-second window."""
        return self.unique_chatters


@dataclass(frozen=True)
class Trigger:
    trigger_time: float
    clip_time: float
    peak_rate: float
    trigger_median: float
    peak_ratio: float
    peak_hype_score: float
    top_words: list
    tier: str


class HypeDetector:
    """Sample message activity and resolve hype triggers at their peak."""

    def __init__(self, start_time, threshold=TRIGGER_SCORE, min_ratio=SENSITIVITY_RATIOS["medium"]):
        self.start_time = float(start_time)
        self.threshold = threshold
        self.min_ratio = max(float(min_ratio), TIER_RATIO_THRESHOLDS[0][0])
        self.next_sample = self.start_time
        self.messages = deque()  # (unix timestamp, user, text)
        self.baseline = deque()  # (timestamp, unique chatters in the 10-second window)
        self.last_trigger_time = float("-inf")
        self.pending = None
        self.session_active = False
        self.latest = Sample(self.start_time, 0.0, 0.0, 0.0)
        self.notices = deque()

    def add_message(self, ts, user, text):
        """Add a timestamped message. Call advance_to() to process due samples."""
        if text.startswith("!") or user.casefold() in BOT_USERS:
            return
        self.messages.append((float(ts), user, text))

    def _sample(self, now):
        cutoff = now - WINDOW_SECONDS
        while self.messages and self.messages[0][0] <= cutoff:
            self.messages.popleft()
        active = [row for row in self.messages if cutoff < row[0] <= now]
        unique_chatters = len({row[1].casefold() for row in active})

        while self.baseline and now - self.baseline[0][0] > BASELINE_SECONDS:
            self.baseline.popleft()
        if self.baseline:
            values = [value for _ts, value in self.baseline]
            median = statistics.median(values)
            mad = statistics.median(abs(value - median) for value in values)
            mad_floor = max(mad, RELATIVE_MAD_FLOOR * median, ABSOLUTE_MAD_FLOOR)
            score = (unique_chatters - median) / (1.4826 * mad_floor)
        else:
            median, score = 0.0, 0.0
        sample = Sample(now, unique_chatters, median, score)
        self.latest = sample

        session_was_active = self.session_active
        if session_was_active and score < SESSION_END_SCORE:
            self.session_active = False

        if self.pending is None:
            if not session_was_active:
                warmed_up = (
                    len(self.baseline) >= WARMUP_SAMPLES
                    and self.baseline[-1][0] - self.baseline[0][0] >= WARMUP_SECONDS
                )
                cooldown_done = now - self.last_trigger_time >= TRIGGER_COOLDOWN
                current_ratio = chatter_ratio(unique_chatters, median)
                ratio_gate = (
                    unique_chatters >= self.min_ratio * median
                    and current_ratio >= self.min_ratio
                )
                if warmed_up and cooldown_done and score >= self.threshold and ratio_gate:
                    self.pending = {
                        "trigger_time": now,
                        "peak_rate": unique_chatters,
                        "trigger_median": median,
                        "peak_ratio": current_ratio,
                        "peak_hype_score": score,
                        "top_words": top_words(active),
                        "tier": tier_for_ratio(current_ratio) or "NICE",
                    }
                    self.session_active = True
                    self.notices.append({"type": "trigger", "tier": self.pending["tier"]})
        else:
            pending = self.pending
            if unique_chatters > pending["peak_rate"]:
                pending["peak_rate"] = unique_chatters
                pending["top_words"] = top_words(active)
            pending["peak_ratio"] = max(
                pending["peak_ratio"],
                chatter_ratio(unique_chatters, pending["trigger_median"]),
            )
            pending["peak_hype_score"] = max(pending["peak_hype_score"], score)
            current_tier = tier_for_ratio(pending["peak_ratio"])
            tier_order = [name for _ratio, name in TIER_RATIO_THRESHOLDS]
            old_tier_rank = tier_order.index(pending["tier"]) if pending["tier"] else -1
            if current_tier and tier_order.index(current_tier) > old_tier_rank:
                pending["tier"] = current_tier
                self.notices.append({"type": "tier_up", "tier": current_tier})
            fell_from_peak = unique_chatters < pending["peak_rate"] * PEAK_DROP_RATIO
            timed_out = now - pending["trigger_time"] >= PEAK_TIMEOUT
            if fell_from_peak or timed_out:
                trigger = Trigger(
                    trigger_time=pending["trigger_time"],
                    clip_time=now,
                    peak_rate=pending["peak_rate"],
                    trigger_median=pending["trigger_median"],
                    peak_ratio=pending["peak_ratio"],
                    peak_hype_score=pending["peak_hype_score"],
                    top_words=pending["top_words"],
                    tier=pending["tier"],
                )
                self.pending = None
                self.last_trigger_time = pending["trigger_time"]
                if not self.session_active:
                    self.baseline.append((now, unique_chatters))
                return sample, trigger

        if not self.session_active:
            self.baseline.append((now, unique_chatters))
        return sample, None

    def current_unique_chatters(self, now):
        cutoff = float(now) - WINDOW_SECONDS
        return len({user.casefold() for ts, user, _text in self.messages if cutoff < ts <= now})

    def drain_notices(self):
        notices = list(self.notices)
        self.notices.clear()
        return notices

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
