"""Shared title formatting for live clips and replay previews."""

MAX_TITLE_LENGTH = 100
MOMENT_TITLE_PREFIXES = {
    "FUNNY": "😂 FUNNY ",
    "SHOCK": "😱 SHOCK ",
    "HYPE": "🔥 ",
}


def make_clip_title(
    tier,
    clip_number,
    game_name,
    clip_datetime,
    clip_timestamp,
    stream_started_at=None,
    upgrade=False,
    moment_type="HYPE",
):
    moment_prefix = MOMENT_TITLE_PREFIXES.get(moment_type, MOMENT_TITLE_PREFIXES["HYPE"])
    tier_label = f"{tier} (upgrade)" if upgrade else tier
    fixed_prefix = f"{moment_prefix}{tier_label} #{clip_number}"
    if stream_started_at is None:
        clock = clip_datetime.strftime("%H:%M")
    else:
        uptime = max(0, int(clip_timestamp - stream_started_at))
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        clock = f"{hours}:{minutes:02}:{seconds:02}"

    time_suffix = f" · {clock}"
    if not game_name:
        return f"{fixed_prefix}{time_suffix}"

    category_separator = " · "
    category_limit = MAX_TITLE_LENGTH - len(fixed_prefix) - len(category_separator) - len(time_suffix)
    if category_limit <= 0:
        return f"{fixed_prefix}{time_suffix}"

    category = game_name.strip()
    if len(category) > category_limit:
        category = category[:category_limit - 1] + "…"
    return f"{fixed_prefix}{category_separator}{category}{time_suffix}"
