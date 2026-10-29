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
PEAK_SMOOTHING_SAMPLES = 3
SESSION_TIMEOUT = 20
PEAK_TIMEOUT = 8
PEAK_HARD_TIMEOUT = 60
MAX_CLIPS_PER_SESSION = 2
UPGRADE_MIN_STEPS = 2
PRE_ROLL_SECONDS = 20.0
MIN_CLIP_DURATION = 5.0
MAX_CLIP_DURATION = 60.0
MIN_TYPE_MESSAGE_RATIO = 0.10
MIN_TYPE_MESSAGES = 3
RELATIVE_MAD_FLOOR = 0.1
ABSOLUTE_MAD_FLOOR = 1.0
SESSION_END_SCORE = 1.0
BOT_USERS = frozenset({"nightbot", "streamelements", "fossabot", "moobot", "streamlabs"})
TIER_RATIO_THRESHOLDS = ((2.0, "NICE"), (3.0, "GREAT"), (4.0, "INSANE"), (6.0, "LEGENDARY"))
SENSITIVITY_RATIOS = {"low": 3.0, "medium": 2.5, "high": 2.0}
MOMENT_TYPE_PATTERNS = {
    "FUNNY": (
        re.compile(r"\bkekw\b", re.IGNORECASE),
        re.compile(r"\blulw?\b", re.IGNORECASE),
        re.compile(r"\bomegalul\b", re.IGNORECASE),
        re.compile(r"\bicant\b", re.IGNORECASE),
        re.compile(r"\blm+f*a+o+\b", re.IGNORECASE),
        re.compile(r"\bxd+\b", re.IGNORECASE),
        re.compile(r"\blol+\b", re.IGNORECASE),
        re.compile(r"(ha){2,}", re.IGNORECASE),
        re.compile(r"(he){2,}", re.IGNORECASE),
        re.compile(r"(ja){2,}", re.IGNORECASE),
        re.compile(r"\bk{4,}\b", re.IGNORECASE),
        re.compile(r"\bw{3,}\b", re.IGNORECASE),
        re.compile(r"\bmdr\b", re.IGNORECASE),
        re.compile(r"(ха){2,}", re.IGNORECASE),
        re.compile(r"ахах+", re.IGNORECASE),
        re.compile(r"(xa){2,}", re.IGNORECASE),
        re.compile(r"ㅋ{3,}", re.IGNORECASE),
        re.compile(r"哈{2,}", re.IGNORECASE),
        re.compile(r"😂|🤣|💀", re.IGNORECASE),
        re.compile(r"\b(?P<mash>[asdfghjklşi]{6,})\b", re.IGNORECASE),
    ),
    "HYPE": (
        re.compile(r"\bpog\w*\b", re.IGNORECASE),
        re.compile(r"\bpoggers\b", re.IGNORECASE),
        re.compile(r"\blets+\s?go+\b", re.IGNORECASE),
        re.compile(r"\bez\b", re.IGNORECASE),
        re.compile(r"🔥", re.IGNORECASE),
        re.compile(r"\bgoal\b", re.IGNORECASE),
    ),
    "SHOCK": (
        re.compile(r"\bmonkas\b", re.IGNORECASE),
        re.compile(r"\bwtf\b", re.IGNORECASE),
        re.compile(r"\bD:", re.IGNORECASE),
        re.compile(r"\bno\s?way\b", re.IGNORECASE),
        re.compile(r"\bwaytoodank\b", re.IGNORECASE),
        re.compile(r"😱|😳", re.IGNORECASE),
    ),
}
def chatter_ratio(unique_chatters, median):
    """Return an activity ratio; a zero baseline has no meaningful ratio."""
    return unique_chatters / median if median > 0 else 0.0


def tier_for_ratio(ratio):
    tier = None
    for minimum_ratio, name in TIER_RATIO_THRESHOLDS:
        if ratio >= minimum_ratio:
            tier = name
    return tier


def clip_duration(wave_start, clip_time):
    """Return requested wave length plus pre-roll, clamped to Twitch limits."""
    requested = float(clip_time) - float(wave_start) + PRE_ROLL_SECONDS
    return round(max(MIN_CLIP_DURATION, min(MAX_CLIP_DURATION, requested)), 1)


def top_words(messages):
    """Return the three most frequent word/emote tokens in (ts, user, text) rows."""
    counts = Counter()
    for _ts, _user, text in messages:
        counts.update(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.casefold(), flags=re.UNICODE))
    return [word for word, _count in counts.most_common(3)]


def message_matches_moment_group(text, group):
    for pattern in MOMENT_TYPE_PATTERNS[group]:
        for match in pattern.finditer(text):
            if "mash" in pattern.groupindex:
                mash = match.group("mash").casefold()
                vowel_count = sum(character in "ai" for character in mash)
                minimum_distinct = 2 if vowel_count == 0 else 3
                if (
                    len(set(mash)) < minimum_distinct
                    or vowel_count / len(mash) > 0.40
                ):
                    continue
            return True
    return False


def moment_group_counts(messages):
    """Count each eligible message at most once in every matching group."""
    counts = {group: 0 for group in MOMENT_TYPE_PATTERNS}
    for _ts, _user, text in messages:
        for group in MOMENT_TYPE_PATTERNS:
            if message_matches_moment_group(text, group):
                counts[group] += 1
    return counts


def classify_moment(counts, window_message_count):
    winning_count = max(counts.values(), default=0)
    if (
        winning_count < MIN_TYPE_MESSAGES
        or not window_message_count
        or winning_count / window_message_count < MIN_TYPE_MESSAGE_RATIO
    ):
        return "HYPE"
    return max(MOMENT_TYPE_PATTERNS, key=lambda group: counts[group])


def format_moment_counts(counts):
    ordered = sorted(MOMENT_TYPE_PATTERNS, key=lambda group: -counts.get(group, 0))
    return ",".join(f"{group}:{counts.get(group, 0)}" for group in ordered)


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
    moment_type: str
    type_counts: dict
    window_message_count: int
    top_repeated_message: str
    top_repeated_count: int
    session_id: str
    upgrade: bool


class HypeDetector:
    """Sample message activity and resolve hype triggers at their peak."""

    def __init__(self, start_time, threshold=TRIGGER_SCORE, min_ratio=SENSITIVITY_RATIOS["medium"]):
        self.start_time = float(start_time)
        self.threshold = threshold
        self.min_ratio = max(float(min_ratio), TIER_RATIO_THRESHOLDS[0][0])
        self.next_sample = self.start_time
        self.messages = deque()  # (unix timestamp, user, text)
        self.message_history = deque()  # Eligible chat retained for full clip windows.
        self.baseline = deque()  # (timestamp, unique chatters in the 10-second window)
        self.baseline_primed = False
        self.rate_samples = deque(maxlen=PEAK_SMOOTHING_SAMPLES)
        self.clips_by_session = {}
        self.last_trigger_time = float("-inf")
        self.pending = None
        self.previous_score = 0.0
        self.session_active = False
        self.session_low_since = None
        self.session_id = None
        self.session_median = None
        self.session_best_tier = None
        self.clipped_tier_by_session = {}
        self.session_peak_tier = None
        self.session_max_ratio = 0.0
        self.session_max_tier = None
        self.latest = Sample(self.start_time, 0.0, 0.0, 0.0)
        self.notices = deque()

    def add_message(self, ts, user, text):
        """Add a timestamped message. Call advance_to() to process due samples."""
        if text.startswith("!") or user.casefold() in BOT_USERS:
            return
        row = (float(ts), user, text)
        self.messages.append(row)
        self.message_history.append(row)

    def _sample(self, now, detect=True):
        history_cutoff = now - MAX_CLIP_DURATION
        while self.message_history and self.message_history[0][0] < history_cutoff:
            self.message_history.popleft()
        cutoff = now - WINDOW_SECONDS
        while self.messages and self.messages[0][0] <= cutoff:
            self.messages.popleft()
        active = [row for row in self.messages if cutoff < row[0] <= now]
        unique_chatters = len({row[1].casefold() for row in active})
        self.rate_samples.append(unique_chatters)
        smoothed_rate = sum(self.rate_samples) / len(self.rate_samples)

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

        if not detect:
            self.baseline.append((now, unique_chatters))
            self.previous_score = score
            return sample, None

        session_was_active = self.session_active
        if session_was_active:
            session_ratio = chatter_ratio(smoothed_rate, self.session_median)
            self._record_session_ratio(session_ratio)

        if self.session_active:
            if score < SESSION_END_SCORE:
                if self.session_low_since is None:
                    self.session_low_since = now
                elif now - self.session_low_since >= SESSION_TIMEOUT:
                    ended_session_id = self.session_id
                    max_ratio = self.session_max_ratio
                    max_tier = self.session_max_tier
                    self.session_active = False
                    self.session_id = None
                    self.session_median = None
                    self.session_low_since = None
                    self.session_best_tier = None
                    self.session_peak_tier = None
                    self.session_max_ratio = 0.0
                    self.session_max_tier = None
                    self.notices.append({
                        "type": "session_end", "session_id": ended_session_id, "time": now,
                        "max_ratio": max_ratio, "max_tier": max_tier,
                    })
            else:
                self.session_low_since = None

        crossed_up = score >= self.threshold and self.previous_score < self.threshold
        start_wave = crossed_up or (not self.session_active and score >= self.threshold)
        completed_trigger = None
        ratio_median = self.session_median if self.session_active else median
        current_ratio = chatter_ratio(smoothed_rate, ratio_median)
        if not self.session_active and self.pending is None and start_wave:
            warmed_up = self.baseline_primed or (
                len(self.baseline) >= WARMUP_SAMPLES
                and self.baseline[-1][0] - self.baseline[0][0] >= WARMUP_SECONDS
            )
            cooldown_done = now - self.last_trigger_time >= TRIGGER_COOLDOWN
            ratio_gate = (
                unique_chatters >= self.min_ratio * median
                and current_ratio >= self.min_ratio
            )
            if warmed_up and cooldown_done and ratio_gate:
                self.session_id = f"{int(now * 1000)}"
                self.session_active = True
                self.session_median = median
                self.session_low_since = None
                self.session_best_tier = None
                self.session_peak_tier = None
                self.session_max_ratio = 0.0
                self.session_max_tier = None
                self.clipped_tier_by_session[self.session_id] = None
                self.clips_by_session[self.session_id] = 0
                self.notices.append({"type": "session_start", "session_id": self.session_id, "time": now})
                initial_tier = tier_for_ratio(current_ratio) or "NICE"
                self.pending = {
                    "trigger_time": now,
                    "last_peak_time": now,
                    "session_id": self.session_id,
                    "peak_rate": smoothed_rate,
                    "trigger_median": self.session_median,
                    "peak_ratio": current_ratio,
                    "peak_hype_score": score,
                    "top_words": top_words(active),
                    "tier": initial_tier,
                }
                self._update_session_peak_tier(initial_tier)
                self.notices.append({"type": "trigger", "tier": initial_tier, "session_id": self.session_id})

        if self.session_active:
            current_tier = tier_for_ratio(current_ratio)
            if not session_was_active:
                self._record_session_ratio(current_ratio)

            best_tier = self.clipped_tier_by_session.get(self.session_id)
            pending_tier = self.pending["tier"] if self.pending and self.pending["session_id"] == self.session_id else None
            candidate_is_upgrade = current_tier is not None and (
                best_tier is not None
                and self.clips_by_session.get(self.session_id, 0) < MAX_CLIPS_PER_SESSION
                and self._tier_rank(current_tier) - self._tier_rank(best_tier) >= UPGRADE_MIN_STEPS
            )
            if candidate_is_upgrade and pending_tier is None:
                self.pending = {
                    "trigger_time": now,
                    "last_peak_time": now,
                    "session_id": self.session_id,
                    "peak_rate": smoothed_rate,
                    "trigger_median": self.session_median,
                    "peak_ratio": current_ratio,
                    "peak_hype_score": score,
                    "top_words": top_words(active),
                    "tier": current_tier,
                    "upgrade_wave": True,
                }
                self.notices.append({"type": "tier_up", "tier": current_tier, "session_id": self.session_id})

        if self.pending is not None:
            pending = self.pending
            if smoothed_rate > pending["peak_rate"]:
                pending["peak_rate"] = smoothed_rate
                pending["last_peak_time"] = now
                pending["top_words"] = top_words(active)
            pending["peak_ratio"] = max(
                pending["peak_ratio"],
                chatter_ratio(smoothed_rate, pending["trigger_median"]),
            )
            pending["peak_hype_score"] = max(pending["peak_hype_score"], score)
            peak_tier = tier_for_ratio(pending["peak_ratio"])
            if peak_tier and self._tier_rank(peak_tier) > self._tier_rank(pending["tier"]):
                pending["tier"] = peak_tier
                if pending["session_id"] == self.session_id:
                    self._update_session_peak_tier(peak_tier)
                self.notices.append({"type": "tier_up", "tier": peak_tier, "session_id": pending["session_id"]})
            fell_from_peak = smoothed_rate < pending["peak_rate"] * PEAK_DROP_RATIO
            timed_out = now - pending["last_peak_time"] >= PEAK_TIMEOUT
            hard_timed_out = now - pending["trigger_time"] >= PEAK_HARD_TIMEOUT
            if fell_from_peak or timed_out or hard_timed_out:
                clipped_tier = self.clipped_tier_by_session.get(pending["session_id"])
                best_rank = self._tier_rank(clipped_tier) if clipped_tier else -1
                clip_length = clip_duration(pending["trigger_time"], now)
                classification_start = now - clip_length
                clip_messages = [
                    row for row in self.message_history
                    if classification_start <= row[0] <= now
                ]
                type_counts = moment_group_counts(clip_messages)
                moment_type = classify_moment(type_counts, len(clip_messages))
                repeated = Counter(row[2] for row in clip_messages).most_common(1)
                top_repeated_message, top_repeated_count = repeated[0] if repeated else ("", 0)
                trigger = Trigger(
                    trigger_time=pending["trigger_time"],
                    clip_time=now,
                    peak_rate=pending["peak_rate"],
                    trigger_median=pending["trigger_median"],
                    peak_ratio=pending["peak_ratio"],
                    peak_hype_score=pending["peak_hype_score"],
                    top_words=pending["top_words"],
                    tier=pending["tier"],
                    moment_type=moment_type,
                    type_counts=dict(type_counts),
                    window_message_count=len(clip_messages),
                    top_repeated_message=top_repeated_message,
                    top_repeated_count=top_repeated_count,
                    session_id=pending["session_id"],
                    upgrade=clipped_tier is not None,
                )
                self.pending = None
                self.last_trigger_time = pending["trigger_time"]
                if self._tier_rank(trigger.tier) > best_rank:
                    completed_trigger = trigger

        self.previous_score = score
        if completed_trigger is not None:
            if not self.session_active:
                self.baseline.append((now, unique_chatters))
            return sample, completed_trigger

        if not self.session_active:
            self.baseline.append((now, unique_chatters))
        return sample, None

    def current_unique_chatters(self, now):
        cutoff = float(now) - WINDOW_SECONDS
        return len({user.casefold() for ts, user, _text in self.messages if cutoff < ts <= now})

    def _update_session_peak_tier(self, tier):
        if self.session_peak_tier is None or self._tier_rank(tier) > self._tier_rank(self.session_peak_tier):
            self.session_peak_tier = tier

    def _record_session_ratio(self, ratio):
        self.session_max_ratio = max(self.session_max_ratio, ratio)
        tier = tier_for_ratio(ratio)
        if tier:
            self._update_session_peak_tier(tier)
            if self.session_max_tier is None or self._tier_rank(tier) > self._tier_rank(self.session_max_tier):
                self.session_max_tier = tier

    @staticmethod
    def _tier_rank(tier):
        return next(index for index, (_ratio, name) in enumerate(TIER_RATIO_THRESHOLDS) if name == tier)

    def mark_clipped(self, trigger):
        """Remember a successfully created clip to gate later session upgrades."""
        best = self.clipped_tier_by_session.get(trigger.session_id)
        if best is None or self._tier_rank(trigger.tier) > self._tier_rank(best):
            self.clipped_tier_by_session[trigger.session_id] = trigger.tier
            self.clips_by_session[trigger.session_id] = self.clips_by_session.get(trigger.session_id, 0) + 1
            if trigger.session_id == self.session_id:
                self.session_best_tier = trigger.tier

    def drain_notices(self):
        notices = list(self.notices)
        self.notices.clear()
        return notices

    def advance_to(self, now, *, detect=True):
        """Process sample boundaries through now; optionally build baseline only."""
        completed = []
        now = float(now)
        while self.next_sample <= now:
            _sample, trigger = self._sample(self.next_sample, detect=detect)
            if trigger is not None:
                completed.append(trigger)
            self.next_sample += SAMPLE_INTERVAL
        if not detect:
            self.baseline_primed = True
        return completed
