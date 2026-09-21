import os
from datetime import datetime, timezone


def parse_birth_date(value: str) -> datetime:
    try:
        # Expect YYYY-MM-DD
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        # Fallback to a fixed date if misconfigured
        return datetime(2024, 1, 1, tzinfo=timezone.utc)


BIRTH_DATE = parse_birth_date(os.getenv("BIRTH_DATE", "2024-09-05"))

# Where your photos and videos live
MEDIA_DIR = os.getenv("MEDIA_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "media")))

# Age range the game covers: the guess slider spans it and media outside it is skipped
MAX_AGE_MONTHS = int(os.getenv("MAX_AGE_MONTHS", "24"))
DAYS_PER_MONTH = 365.0 / 12.0
MAX_AGE_DAYS = int(round(MAX_AGE_MONTHS * DAYS_PER_MONTH))
# Media captured a little past the top of the range (e.g. at the party itself)
# still counts; it just gets clamped down to MAX_AGE_DAYS.
AGE_GRACE_DAYS = int(os.getenv("AGE_GRACE_DAYS", "14"))

# Remembers each file's capture time so restarts don't re-probe every video
MEDIA_INDEX_PATH = os.getenv(
    "MEDIA_INDEX_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), ".media_index.json"))
)
# Parallelism for the (slow) EXIF/ffprobe pass over new files
MEDIA_PROBE_WORKERS = int(os.getenv("MEDIA_PROBE_WORKERS", "8"))

# Game settings
# Backwards compatibility: if TURN_DURATION_SECONDS is set, use it for video by default
TURN_DURATION_SECONDS_VIDEO = int(os.getenv("TURN_DURATION_SECONDS", os.getenv("TURN_DURATION_SECONDS_VIDEO", "20")))
TURN_DURATION_SECONDS_IMAGE = int(os.getenv("TURN_DURATION_SECONDS_IMAGE", "10"))
TOTAL_ROUNDS = int(os.getenv("TOTAL_ROUNDS", "50"))

# Alias for any legacy imports
TURN_DURATION_SECONDS = TURN_DURATION_SECONDS_IMAGE

# Allowed file extensions
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}

# Display name for the baby used in UI prompts
BABY_NAME = os.getenv("BABY_NAME", "Joy")

# One shared password for the whole site (game, API, and media). Leave empty
# to run wide open, e.g. for local development.
GAME_PASSWORD = os.getenv("GAME_PASSWORD", "")
# Signs the login cookie. Defaults to something derived from the password so
# cookies survive a restart; set explicitly if you rotate the password often.
SECRET_KEY = os.getenv("SECRET_KEY", "")