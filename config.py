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