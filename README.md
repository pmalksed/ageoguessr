# ageoguessr

A tiny pastel web game for guessing a baby's age from random photos/videos.

- Frontend: buildless React (CDN) served by Flask
- Backend: Python + Flask
- Polling-based sync (1s), no websockets

## Quick start

1. Ensure Python 3.10+ is installed.
2. Put your media files into `media/` (images: .jpg/.jpeg/.png/.gif; videos: .mp4/.mov/.webm).
3. Optionally set environment variables:
   - `BIRTH_DATE` (YYYY-MM-DD, default `2024-09-05`) – used with each file's capture time to compute true age for scoring
   - `MEDIA_DIR` (defaults to `./media`)
   - `MAX_AGE_MONTHS` (default `24`) – how far the guess slider reaches; media older than this is skipped
   - `TURN_DURATION_SECONDS` (default `120`)
   - `TOTAL_ROUNDS` (default `50`)
   - `GAME_PASSWORD` (default empty = no login) – one shared password for the whole site; see below

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Visit http://localhost:5000

## How it works

- One global game at a time; 50 rounds by default, each ~2 minutes.
- Server picks a media file each round, spread evenly over the age range (see below).
- Players type `newgame` anywhere on the page to start a fresh game (clobbers previous).
- Slider from 0–24 months (approx via 730 days). Hover/drag shows months + days approximation.
- Submitting a guess sends the selected day count to the server; guesses after the timer are ignored.
- Scoring: `points = max(0, 100 - |guessDays - trueDays|)`.
- Leaderboard always visible and updates as rounds resolve.

## Password

A server on a public IP gets found by port scanners whether or not anyone
links to it, and `/api/state` hands the current media URL to anyone who asks.
Set `GAME_PASSWORD` and everything except the login page sits behind one
shared password, remembered in a cookie for 60 days.

The easiest way to let guests in is a one-tap link that logs them in and then
drops the password from the address bar:

    http://your-host:5000/?pw=THE_PASSWORD

Type `endgame` on the page to stop a game in progress; `newgame` starts one.

## Even coverage of the age range

Most people shoot far more of a newborn than of an 18-month-old, so picking a
file uniformly at random would make the game mostly about month 0. Instead each
month of life gets an equal share of the rounds no matter how many files it
holds, and a month with no media at all donates its share to the nearest months
that do have some. Within a game, a month that has already had its share sits
out until the others catch up, so 50 rounds cover the two years fairly evenly.

Visit `/api/media_stats` to see how many usable files you have per month, plus
any files that were skipped for having no readable date or for falling outside
the age range.

## Notes

- Age is derived from `BIRTH_DATE` to each file's capture time (EXIF for photos,
  `ffprobe` for videos, falling back to a date parsed out of the filename).
  Files with no discoverable date are skipped; `/api/media_stats` lists them.
- Capture times are cached in `.media_index.json`, keyed by size and mtime, so
  restarts only re-probe files that actually changed.
- This is a cozy friends-only game; no auth, no persistence across restarts. 