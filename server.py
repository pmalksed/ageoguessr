from __future__ import annotations

import os
import random
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import re
import subprocess

from flask import Flask, jsonify, request, send_from_directory, render_template, make_response

from config import (
    BIRTH_DATE,
    MEDIA_DIR,
    TURN_DURATION_SECONDS_IMAGE,
    TURN_DURATION_SECONDS_VIDEO,
    TOTAL_ROUNDS,
    ALLOWED_IMAGE_EXTENSIONS,
    ALLOWED_VIDEO_EXTENSIONS,
    BABY_NAME,
)

# Optional: EXIF for images
try:
    from PIL import Image, ExifTags  # type: ignore
    _PIL_AVAILABLE = True
    _EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
except Exception:
    _PIL_AVAILABLE = False
    _EXIF_TAGS = {}

app = Flask(__name__, static_folder="static", template_folder="templates")


# Reveal phase duration in seconds
REVEAL_SECONDS = 5


@dataclass
class Player:
    player_id: str
    username: str
    score: int = 0


@dataclass
class RoundInfo:
    round_index: int
    media_filename: Optional[str] = None
    media_type: Optional[str] = None  # "image" or "video"
    media_age_days: Optional[int] = None  # integer days
    ends_at: Optional[datetime] = None  # end of guessing phase
    reveal_ends_at: Optional[datetime] = None  # end of reveal phase
    phase: str = "guessing"  # "guessing" or "reveal"


@dataclass
class GameState:
    active: bool = False
    game_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    total_rounds: int = TOTAL_ROUNDS
    # Duration for the CURRENT round only; set when round starts
    current_turn_duration_seconds: int = TURN_DURATION_SECONDS_IMAGE
    current_round_index: int = 0
    rounds_remaining: int = TOTAL_ROUNDS
    current_round: Optional[RoundInfo] = None
    # Mapping player_id -> Player
    players: Dict[str, Player] = field(default_factory=dict)
    # players who joined the current game
    active_players: set[str] = field(default_factory=set)
    # misses in consecutive rounds for active players
    misses: Dict[str, int] = field(default_factory=dict)
    # guesses[round_index][player_id] = guess_days
    guesses: Dict[int, Dict[str, int]] = field(default_factory=dict)
    # results[round_index][player_id] = {"guess_days": int, "diff": int, "points": int}
    results: Dict[int, Dict[str, Dict[str, int]]] = field(default_factory=dict)
    # Per-round readiness: ready[round_index] = set(player_id)
    ready: Dict[int, set[str]] = field(default_factory=dict)
    # Track used media relative paths in current game to avoid repeats
    used_media: set[str] = field(default_factory=set)
    # Pending next round pick and duration
    pending_pick: Optional[Tuple[str, str, int]] = None
    pending_duration_seconds: int = TURN_DURATION_SECONDS_IMAGE
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self):
        self.active = False
        self.game_id = uuid.uuid4().hex
        self.total_rounds = TOTAL_ROUNDS
        self.current_turn_duration_seconds = TURN_DURATION_SECONDS_IMAGE
        self.current_round_index = 0
        self.rounds_remaining = TOTAL_ROUNDS
        self.current_round = None
        # Keep players but reset scores
        for p in self.players.values():
            p.score = 0
        self.active_players = set()
        self.misses = {}
        self.guesses = {}
        self.results = {}
        self.ready = {}
        self.used_media.clear()
        self.pending_pick = None
        self.pending_duration_seconds = TURN_DURATION_SECONDS_IMAGE


STATE = GameState()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _list_media_files() -> List[Path]:
    media_root = Path(MEDIA_DIR)
    media_root.mkdir(parents=True, exist_ok=True)
    if not media_root.exists():
        return []
    all_files: List[Path] = []
    for p in media_root.rglob("*"):
        if p.is_file():
            suffix = p.suffix.lower()
            if suffix in ALLOWED_IMAGE_EXTENSIONS or suffix in ALLOWED_VIDEO_EXTENSIONS:
                all_files.append(p)
    return all_files


def _media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in ALLOWED_IMAGE_EXTENSIONS:
        return "image"
    if suffix in ALLOWED_VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def _parse_datetime_from_filename(name: str) -> Optional[datetime]:
    base = os.path.basename(name)
    # Common patterns: PXL_YYYYMMDD_HHMMSS, IMG_YYYYMMDD_HHMMSS, YYYYMMDD_HHMMSS
    m = re.search(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})", base)
    if m:
        y, mo, d, hh, mm, ss = map(int, m.groups())
        try:
            return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
        except Exception:
            pass
    # Date-only: 2025-01-28 or 20250128
    m2 = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", base)
    if m2:
        y, mo, d = map(int, m2.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _read_exif_datetime_if_available(path: Path) -> Optional[datetime]:
    if not _PIL_AVAILABLE:
        return None
    try:
        with Image.open(path) as img:
            exif = img._getexif() or {}
            if not exif:
                return None
            # Prefer DateTimeOriginal, then DateTimeDigitized, then DateTime
            dt_str = None
            for tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                tag_id = _EXIF_TAGS.get(tag_name)
                if tag_id and tag_id in exif:
                    dt_str = exif.get(tag_id)
                    if dt_str:
                        break
            if not dt_str or not isinstance(dt_str, str):
                return None
            # EXIF format: YYYY:MM:DD HH:MM:SS
            try:
                dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None
    except Exception:
        return None


def _parse_possible_datetime_strings(value: str) -> Optional[datetime]:
    s = (value or "").strip()
    if not s:
        return None
    # Normalize Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Try ISO8601
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # Try common ffprobe formats
    for fmt in [
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _read_video_creation_datetime_via_ffprobe(path: Path) -> Optional[datetime]:
    try:
        # Query both format and stream tags for creation_time
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
            "-of", "default=nw=1:nk=1",
            str(path),
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if out.returncode != 0:
            return None
        # ffprobe may output one or more lines; take the first parseable
        for line in out.stdout.splitlines():
            dt = _parse_possible_datetime_strings(line)
            if dt:
                return dt
        return None
    except Exception:
        return None


def _capture_datetime_via_good_methods(path: Path) -> Optional[datetime]:
    media_type = _media_type_for(path)
    if media_type == "image":
        dt = _read_exif_datetime_if_available(path)
        if dt:
            return dt
        return None
    if media_type == "video":
        return _read_video_creation_datetime_via_ffprobe(path)
    return None


def _age_in_days_for_media_good_only(path: Path) -> Optional[int]:
    dt = _capture_datetime_via_good_methods(path)
    if not dt:
        return None
    delta = dt - BIRTH_DATE
    return max(0, int(delta.total_seconds() // 86400))


def _age_in_days_for_media_with_fallback(path: Path) -> Optional[int]:
    """Try good capture-time methods first; fall back to parsing filename date if needed."""
    dt = _capture_datetime_via_good_methods(path)
    if not dt:
        # Fallback to filename-based parsing
        dt = _parse_datetime_from_filename(str(path))
    if not dt:
        return None
    delta = dt - BIRTH_DATE
    return max(0, int(delta.total_seconds() // 86400))


def _pick_random_media() -> Optional[Tuple[str, str, int]]:
    files = _list_media_files()
    if not files:
        return None
    # Filter to only those with a determinable age
    eligible: List[Tuple[Path, str, int]] = []
    for f in files:
        media_type = _media_type_for(f)
        age_days = _age_in_days_for_media_with_fallback(f)
        if age_days is not None and media_type in ("image", "video"):
            eligible.append((f, media_type, age_days))
    if not eligible:
        return None
    # Avoid repeats if possible within the game
    used = STATE.used_media
    # Build list of relative paths
    def rel_str(p: Path) -> str:
        return str(p.relative_to(MEDIA_DIR)).replace(os.sep, "/")

    not_used = [(p, t, a) for (p, t, a) in eligible if rel_str(p) not in used]
    candidates = not_used if not_used else eligible
    chosen = random.choice(candidates)
    path, media_type, age_days = chosen
    rel = rel_str(path)
    # Track as used for this game
    STATE.used_media.add(rel)
    return (rel, media_type, age_days)


def _ensure_pending_pick_locked():
    if STATE.pending_pick is None:
        pick = _pick_random_media()
        if pick is not None:
            rel, mtype, _age = pick
            duration = TURN_DURATION_SECONDS_VIDEO if mtype == "video" else TURN_DURATION_SECONDS_IMAGE
            STATE.pending_pick = pick
            STATE.pending_duration_seconds = duration


def _start_next_round_locked():
    global STATE
    # Determine duration based on media type once we know it
    duration_seconds = TURN_DURATION_SECONDS_IMAGE

    STATE.current_round_index += 1
    STATE.rounds_remaining = max(0, STATE.total_rounds - STATE.current_round_index)

    # Use pending pick if available; otherwise pick now
    if STATE.pending_pick is not None:
        filename, media_type, age_days = STATE.pending_pick
        duration_seconds = STATE.pending_duration_seconds
        STATE.pending_pick = None
    else:
        pick = _pick_random_media()
        if pick is None:
            filename = None
            media_type = None
            age_days = None
            duration_seconds = TURN_DURATION_SECONDS_IMAGE
        else:
            filename, media_type, age_days = pick
            duration_seconds = TURN_DURATION_SECONDS_VIDEO if media_type == "video" else TURN_DURATION_SECONDS_IMAGE

    STATE.current_turn_duration_seconds = duration_seconds
    guess_ends_at = _now() + timedelta(seconds=duration_seconds)

    if filename is None:
        # No media, still advance the timer so clients can see countdown; media fields None
        STATE.current_round = RoundInfo(
            round_index=STATE.current_round_index,
            media_filename=None,
            media_type=None,
            media_age_days=None,
            ends_at=guess_ends_at,
            reveal_ends_at=None,
            phase="guessing",
        )
    else:
        STATE.current_round = RoundInfo(
            round_index=STATE.current_round_index,
            media_filename=filename,
            media_type=media_type,
            media_age_days=age_days,
            ends_at=guess_ends_at,
            reveal_ends_at=None,
            phase="guessing",
        )
    STATE.guesses.setdefault(STATE.current_round_index, {})
    STATE.ready[STATE.current_round_index] = set()


def _finalize_round_locked():
    global STATE
    rnd = STATE.current_round
    if not rnd or rnd.media_age_days is None:
        return
    guesses = STATE.guesses.get(rnd.round_index, {})
    round_results: Dict[str, Dict[str, int]] = {}
    for pid, guess_days in guesses.items():
        diff = abs(int(guess_days) - int(rnd.media_age_days))
        points = max(0, 100 - diff)  # 1 point per day off; 0 minimum
        player = STATE.players.get(pid)
        if player:
            player.score += int(points)
        round_results[pid] = {"guess_days": int(guess_days), "diff": int(diff), "points": int(points)}
    STATE.results[rnd.round_index] = round_results
    # Update misses and kick inactive players
    active_now = set(STATE.active_players)
    for pid in list(active_now):
        if pid in guesses:
            STATE.misses[pid] = 0
        else:
            STATE.misses[pid] = STATE.misses.get(pid, 0) + 1
            if STATE.misses[pid] >= 2:
                # kick from active players for this game
                STATE.active_players.discard(pid)


def _finalize_round_early_if_all_ready_locked():
    """If all active players have marked ready, end the round now."""
    rnd = STATE.current_round
    if not rnd or rnd.phase != "guessing":
        return
    active_set = set(STATE.active_players)
    if not active_set:
        return
    ready_set = STATE.ready.get(rnd.round_index, set())
    if active_set.issubset(ready_set):
        _finalize_round_locked()
        rnd.phase = "reveal"
        rnd.reveal_ends_at = _now() + timedelta(seconds=REVEAL_SECONDS)
        _ensure_pending_pick_locked()
        return


def _advance_if_needed_locked():
    global STATE
    if not STATE.active or STATE.current_round is None:
        return
    rnd = STATE.current_round
    # Transition guessing -> reveal
    if rnd.phase == "guessing" and rnd.ends_at and _now() >= rnd.ends_at:
        _finalize_round_locked()
        rnd.phase = "reveal"
        rnd.reveal_ends_at = _now() + timedelta(seconds=REVEAL_SECONDS)
        # Pick pending next round now so clients can prefetch
        _ensure_pending_pick_locked()
        return
    # Transition reveal -> next round or end
    if rnd.phase == "reveal" and rnd.reveal_ends_at and _now() >= rnd.reveal_ends_at:
        if STATE.current_round_index >= STATE.total_rounds:
            STATE.active = False
            return
        _start_next_round_locked()
        return


def _public_leaderboard() -> List[Dict]:
    players = list(STATE.players.values())
    players.sort(key=lambda p: (-p.score, p.username.lower()))
    return [
        {"player_id": p.player_id, "username": p.username, "score": p.score}
        for p in players
    ]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/media/<path:filename>")
def media(filename: str):
    # Security: prevent escaping the media dir
    safe_root = os.path.abspath(MEDIA_DIR)
    full_path = os.path.abspath(os.path.join(MEDIA_DIR, filename))
    if not full_path.startswith(safe_root):
        return ("Not found", 404)
    if not os.path.exists(full_path):
        return ("Not found", 404)
    directory = os.path.dirname(full_path)
    basename = os.path.basename(full_path)
    resp = make_response(send_from_directory(directory, basename))
    # Strong caching: versioned URLs via query param make each round unique
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/api/register", methods=["POST"]) 
def register():
    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id")
    desired_username = data.get("desired_username")

    with STATE.lock:
        if player_id and player_id in STATE.players:
            player = STATE.players[player_id]
            if desired_username:
                player.username = _sanitize_username(desired_username)
        else:
            player_id = uuid.uuid4().hex
            username = _generate_username()
            player = Player(player_id=player_id, username=username)
            STATE.players[player_id] = player

    return jsonify({
        "player_id": player.player_id,
        "username": player.username,
    })


@app.route("/api/username", methods=["POST"]) 
def change_username():
    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id")
    username = data.get("username")
    if not player_id or not username:
        return ("Missing fields", 400)

    with STATE.lock:
        player = STATE.players.get(player_id)
        if not player:
            return ("Unknown player", 404)
        player.username = _sanitize_username(username)
        lb = _public_leaderboard()
    return jsonify({"ok": True, "leaderboard": lb})


@app.route("/api/newgame", methods=["POST"]) 
def new_game():
    with STATE.lock:
        STATE.reset()
        STATE.active = True
        _start_next_round_locked()
    return jsonify({"ok": True, "game_id": STATE.game_id})


@app.route("/api/guess", methods=["POST"]) 
def guess():
    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id")
    guess_days = data.get("guess_days")
    if player_id is None or guess_days is None:
        return ("Missing fields", 400)

    try:
        guess_days = int(guess_days)
        guess_days = max(0, min(365, guess_days))
    except Exception:
        return ("Invalid guess", 400)

    with STATE.lock:
        if not STATE.active or STATE.current_round is None:
            return jsonify({"accepted": False, "reason": "no_active_game"})
        _advance_if_needed_locked()
        rnd = STATE.current_round
        if rnd.phase == "reveal":
            return jsonify({"accepted": False, "reason": "reveal"})
        if rnd.ends_at and _now() > rnd.ends_at:
            return jsonify({"accepted": False, "reason": "round_over"})
        if player_id not in STATE.players:
            # auto-register with generated name if unknown
            STATE.players[player_id] = Player(player_id=player_id, username=_generate_username())
        STATE.guesses.setdefault(rnd.round_index, {})[player_id] = guess_days
    return jsonify({"accepted": True})


@app.route("/api/join", methods=["POST"]) 
def join_game():
    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id")
    if not player_id:
        return ("Missing fields", 400)
    with STATE.lock:
        # ensure player exists
        if player_id not in STATE.players:
            STATE.players[player_id] = Player(player_id=player_id, username=_generate_username())
        # mark as active for the current game
        STATE.active_players.add(player_id)
        STATE.misses[player_id] = 0
    return jsonify({"ok": True})


@app.route("/api/ready", methods=["POST"]) 
def set_ready():
    data = request.get_json(silent=True) or {}
    player_id = data.get("player_id")
    if not player_id:
        return ("Missing fields", 400)
    with STATE.lock:
        if not STATE.active or STATE.current_round is None:
            return jsonify({"accepted": False, "reason": "no_active_game"})
        _advance_if_needed_locked()
        rnd = STATE.current_round
        if rnd.phase == "reveal":
            return jsonify({"accepted": False, "reason": "reveal"})
        if rnd.ends_at and _now() > rnd.ends_at:
            return jsonify({"accepted": False, "reason": "round_over"})
        if player_id not in STATE.players:
            STATE.players[player_id] = Player(player_id=player_id, username=_generate_username())
        STATE.ready.setdefault(rnd.round_index, set()).add(player_id)
        _finalize_round_early_if_all_ready_locked()
    return jsonify({"accepted": True})


@app.route("/api/state") 
def get_state():
    with STATE.lock:
        _advance_if_needed_locked()
        rnd = STATE.current_round
        if rnd and rnd.media_filename:
            version = f"{STATE.game_id}-{STATE.current_round_index}"
            media_url = f"/media/{rnd.media_filename}?v={version}"
        else:
            media_url = None
        # Pending payload for client prefetch
        pending_payload = None
        if STATE.pending_pick is not None:
            pr_filename, pr_type, _pr_age = STATE.pending_pick
            pr_version = f"{STATE.game_id}-{STATE.current_round_index + 1}"
            pending_payload = {
                "media_url": f"/media/{pr_filename}?v={pr_version}",
                "media_type": pr_type,
                "turn_duration_seconds": STATE.pending_duration_seconds,
            }
        # compute next deadline for countdown depending on phase
        next_deadline = None
        if rnd:
            if rnd.phase == "guessing":
                next_deadline = rnd.ends_at
            elif rnd.phase == "reveal":
                next_deadline = rnd.reveal_ends_at
        # include reveal results when in reveal phase
        reveal_payload = None
        if rnd and rnd.phase == "reveal":
            reveal_payload = {
                "true_age_days": rnd.media_age_days,
                "results": STATE.results.get(rnd.round_index, {}),
                "reveal_ends_at_ms": int(rnd.reveal_ends_at.timestamp() * 1000) if rnd.reveal_ends_at else None,
            }
        # readiness info
        ready_payload = None
        if rnd and rnd.phase == "guessing":
            active_set = set(STATE.active_players)
            ready_set = STATE.ready.get(rnd.round_index, set())
            ready_payload = {
                "count": len(active_set.intersection(ready_set)),
                "total": len(active_set),
                "ready_player_ids": list(ready_set),
                "active_player_ids": list(active_set),
            }
        response = {
            "server_time_ms": int(_now().timestamp() * 1000),
            "game": {
                "active": STATE.active,
                "game_id": STATE.game_id,
                "round_number": STATE.current_round_index,
                "total_rounds": STATE.total_rounds,
                "rounds_remaining": STATE.rounds_remaining,
                "turn_duration_seconds": STATE.current_turn_duration_seconds,
                "turn_ends_at_ms": int(next_deadline.timestamp() * 1000) if next_deadline else None,
                "media_url": media_url,
                "media_type": rnd.media_type if rnd else None,
                "phase": rnd.phase if rnd else None,
                "reveal": reveal_payload,
                "ready": ready_payload,
                "pending": pending_payload,
            },
            "leaderboard": _public_leaderboard(),
            "baby_name": BABY_NAME,
        }
        return jsonify(response)


def _sanitize_username(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return _generate_username()
    # trim to 24 characters
    return name[:24]


def _generate_username() -> str:
    adjectives = [
        "Goofy",
        "Bouncy",
        "Sunny",
        "Rosy",
        "Wobbly",
        "Tiny",
        "Giggle",
        "Fuzzy",
        "Peachy",
        "Zany",
        "Sparkly",
        "Bubbly",
    ]
    animals = [
        "Giraffe",
        "Panda",
        "Koala",
        "Bunny",
        "Otter",
        "Duckling",
        "Kitten",
        "Puppy",
        "Lamb",
        "Chick",
        "Fawn",
        "Cub",
    ]
    return f"{random.choice(adjectives)}{random.choice(animals)}{random.randint(10, 99)}"


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True) 