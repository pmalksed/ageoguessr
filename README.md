# ageoguessr

A tiny pastel web game for guessing a baby's age from random photos/videos.

- Frontend: buildless React (CDN) served by Flask
- Backend: Python + Flask
- Polling-based sync (1s), no websockets

## Quick start

1. Ensure Python 3.10+ is installed.
2. Put your media files into `media/` (images: .jpg/.jpeg/.png/.gif; videos: .mp4/.mov/.webm).
3. Optionally set environment variables:
   - `BIRTH_DATE` (YYYY-MM-DD, default `2024-01-01`) – used with file modified time to compute true age for scoring
   - `MEDIA_DIR` (defaults to `./media`)
   - `TURN_DURATION_SECONDS` (default `120`)
   - `TOTAL_ROUNDS` (default `50`)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Visit http://localhost:5000

## How it works

- One global game at a time; 50 rounds by default, each ~2 minutes.
- Server selects a random media file each round.
- Players type `newgame` anywhere on the page to start a fresh game (clobbers previous).
- Slider from 0–12 months (approx via 365 days). Hover/drag shows months + days approximation.
- Submitting a guess sends the selected day count to the server; guesses after the timer are ignored.
- Scoring: `points = max(0, 100 - |guessDays - trueDays|)`.
- Leaderboard always visible and updates as rounds resolve.

## Notes

- Age is derived from `BIRTH_DATE` to each file's modification time. For finer accuracy, keep your files' mtimes aligned with capture time.
- This is a cozy friends-only game; no auth, no persistence across restarts. 