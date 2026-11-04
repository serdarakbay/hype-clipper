# Hype Clipper

A Twitch chat hype detector that auto-clips the best moments of a stream.

## The problem

Streamers are busy playing and can miss their best moments. Finding and clipping those moments manually takes attention away from the stream.

## How it works

The detector measures unique chatters in a rolling 10-second window and compares that activity with a rolling median/MAD baseline. It waits for a 3-minute baseline warmup before triggering. A hype session ends after the score stays below 1.0 for 20 seconds.

```text
Twitch chat -> unique chatter activity -> baseline and hype score
            -> session and tier -> wait for wave peak -> clip
```

Tiers are NICE, GREAT, INSANE, and LEGENDARY. A session can create up to two clips: its first clip and at most one upgrade, which requires a tier at least two steps above the best tier already clipped. Peak waiting uses a 3-sample smoothed rate and clips after a 70% drop or at most 8 seconds without a new peak, with a 60-second hard cap. Clip duration is the time from wave start to the Create Clip call plus 20 seconds of pre-roll, clamped to Twitch's 5-60 second range; the computed duration is logged for each clip. Clips are created with has_delay enabled, so Twitch captures them as viewers saw the stream, including the broadcaster's stream delay.

Each clip also gets a moment type, separate from its tier, using regex patterns over messages in that clip's own window. Patterns cover multilingual laughter, including Turkish keyboard-mash, Russian, Korean, Spanish and Portuguese forms, plus emojis. Each message counts once per group. A group needs at least 10% of window messages and at least 3 messages to win; otherwise the type is HYPE.

## Features

- Creates Twitch clips with type-prefixed titles, e.g. `😂 FUNNY INSANE #2 · ...` and `🔥 GREAT #1 · ...`; SHOCK uses `😱 SHOCK`. Titles include the current category and stream uptime, with `(upgrade)` on upgrade clips. The category is shortened as needed to keep titles within 100 characters, and omitted when unavailable. If Helix `started_at` is unavailable, titles use local `HH:MM`. The same title is used in clip logs, `!clips` replies, chat announcements, and overlay events. Clips use 20 seconds of pre-roll and `has_delay=true`.
- Serves a transparent OBS overlay.
- `--announce` posts an announcement in the authenticated user's own channel when a clip is created.
- `!clips` replies with recent clips from the current run; `!hype` replies with the current level and score. These commands are read from the authenticated user's channel.
- Validates and refreshes OAuth tokens on startup and after Helix 401 responses; the live watcher revalidates hourly.
- `replay.py` replays recorded chat through the same detector without connecting to Twitch.
- `--dry-run` runs detection without calling Create Clip.

## Results

On a recorded 77-minute Jynxzi stream (Rainbow Six Siege, about 30,000 viewers): 6 hype sessions, 7 clips, 5.5 clips per hour, and one NICE -> INSANE upgrade.

Live tests after timing fixes across 4 channels (PGL and ESLCS for CS2 esports, RavshanN and rayasianboy for IRL): 15 of 16 clips contained the moment chat reacted to. Moment type was correct in 7 of 8 clearly classifiable clips. The upgrade mechanism fired live 3 times (e.g. NICE -> INSANE, NICE -> LEGENDARY).

Current replay across all 6 recordings in `data/`: 5 FUNNY, 25 HYPE, and 0 SHOCK clips (30 total).

## Setup

Use Python 3.8 or newer.

1. Register a Twitch application with redirect URL `http://localhost:3000`, category **Chat Bot**, and type **Confidential**.
2. Copy `.env.example` to `.env` and set `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.
3. Install dependencies and authorize the application:

   ```sh
   python -m pip install -r requirements.txt
   python auth.py
   ```

`auth.py` opens Twitch authorization in a browser and saves the access and refresh tokens to `tokens.json`. Keep `.env` and `tokens.json` private; both are ignored by `.gitignore`.

## Usage

Watch a channel and create clips:

```sh
python hype_clipper.py <channel> [--dry-run] [--record] [--overlay] [--announce] [--sensitivity low|medium|high]
```

For example, on your own channel:

```sh
python hype_clipper.py your_channel --overlay --announce
```

`--record` writes chat messages to `data/chat_<channel>_<YYYYMMDD_HHMM>.jsonl`. `--sensitivity` defaults to `medium`; `low` requires a 3.0x chatter ratio, `medium` 2.5x, and `high` 2.0x. All modes keep the default hype-score threshold of 3.0.

With `--overlay`, the tool serves the OBS overlay at `http://localhost:8765`. Add an OBS **Browser Source** with that URL and set its width to **1920** and height to **1080**. Optional URL parameters include `?pos=top-left` (also `top-center`, `top-right`, `bottom-left`, or `bottom-right`) and `&scale=1`. For the built-in preview, open `http://localhost:8765/?demo=1` while the server is running.

Replay a recording:

```sh
python replay.py data/chat_jynxzi_20261027_0142.jsonl [--start SECONDS] [--threshold 3.0] [--sensitivity low|medium|high] [--speed N] [--overlay]
```

Replay runs as fast as possible unless `--speed N` is supplied; for example, `--speed 2` replays at twice real time. `--start SECONDS` starts at an offset into the recording while using earlier messages to build the detector baseline instantly. It also works with `--speed` and `--overlay`. `--overlay` starts and updates the same OBS server.

Other utility commands:

```sh
python test_chat.py <channel>
python test_clip.py <channel>
```

`test_chat.py` prints read-only chat for 60 seconds. `test_clip.py` attempts to create a clip for the specified live channel.

## Built with OpenAI Codex

The entire codebase was written through OpenAI Codex in VS Code, task by task, with each result tested on live Twitch chat or recorded replays before the next task.

- Built the OAuth flow, chat reader and clip test scripts as the first working slice.
- Wrote the shared detector and replay tool, so algorithm changes could be tested against the same recorded streams.
- Added hype sessions and tier upgrades after replay results showed separate triggers for one long moment.
- Diagnosed and fixed real bugs found during testing: expired tokens (401) with automatic refresh, and a background flush error that stopped recording.
- Fixed clip timing after live tests showed clips missing the moment: added a 20-second pre-roll and enabled has_delay so clips match what viewers saw.
- Built the OBS overlay, chat announcements and chat commands.

The commit history shows the project being built during the hackathon period.

## Known limitations

- Bot raids, where many new accounts post the same text, can inflate unique chatter counts and trigger false hype sessions.
- The channel category comes from Twitch stream metadata and may not match what the streamer is actually doing.
- Moment types depend on chat using recognizable patterns; channel-specific memes are classified as HYPE.
- Twitch must allow clips for the monitored channel, and the channel must be live. The clip duration parameter is limited to 60 seconds.
- The overlay server listens on localhost, so it is intended for a Browser Source on the same computer.
- Clips include 20 seconds of pre-roll because chat reacts 5-15 seconds after the event. The moment is inside the clip, but the end can contain chat-reaction time with less action.
- The live accuracy test covers 16 clips on 4 channels; thresholds may still need tuning for different channels and stream sizes.
- Chat asking the streamer the same question in bulk can trigger a session with no real event (1 false positive in live tests).
- At very low baselines, farewell spam ("bye") can be labeled FUNNY.
- Mixed reactions (laughing and "wtf" together) fall back to HYPE when no single group passes the 10% threshold.
