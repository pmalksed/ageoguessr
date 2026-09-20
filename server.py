from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
import re
import subprocess

from flask import Flask, jsonify, request, send_from_directory, render_template, make_response

from config import (
    AGE_GRACE_DAYS,
    BIRTH_DATE,
    DAYS_PER_MONTH,
    MAX_AGE_DAYS,
    MAX_AGE_MONTHS,
    MEDIA_DIR,
    MEDIA_INDEX_PATH,
    MEDIA_PROBE_WORKERS,
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
# How many future media to prepare
TARGET_PENDING = 3


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
    # Pending next round pick and duration (first item of queue)
    pending_pick: Optional[Tuple[str, str, int]] = None
    pending_duration_seconds: int = TURN_DURATION_SECONDS_IMAGE
    # Queue of future picks [(rel, type, age_days), ...]
    pending_queue: List[Tuple[str, str, int]] = field(default_factory=list)
    # Whether a background task is preparing pending picks
    pending_preparing: bool = False
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
        self.pending_queue = []
        self.pending_preparing = False


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
    """Try good capture-time methods first; fall back to parsing filename date if needed.

    May be negative for anything shot before the birth date; the media index
    decides what is close enough to count.
    """
    dt = _capture_datetime_via_good_methods(path)
    if not dt:
        # Fallback to filename-based parsing
        dt = _parse_datetime_from_filename(str(path))
    if not dt:
        return None
    delta = dt - BIRTH_DATE
    return int(delta.total_seconds() // 86400)


# ---------------------------------------------------------------------------
# Media index
#
# Dating a file means reading EXIF or shelling out to ffprobe, which is far too
# slow to redo on every pick. We keep an in-memory index of every usable file
# and back it with a small on-disk cache keyed by (mtime, size) so a restart
# only re-probes files that actually changed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaEntry:
    rel: str
    media_type: str
    age_days: int
    month: int  # which month-of-life bucket this falls into, 0-based


# How long the index is trusted before we re-stat the media directory
MEDIA_INDEX_TTL_SECONDS = 30.0

_index_lock = threading.Lock()  # guards the probe cache and the built index
_index_build_lock = threading.Lock()  # serializes (slow) rebuilds
# rel path -> (mtime, size, age_days or None if the file could not be dated)
_probe_cache: Dict[str, Tuple[float, int, Optional[int]]] = {}
_probe_cache_loaded = False
_index_entries: List[MediaEntry] = []
_index_scanned_at: float = 0.0
_index_skipped: Dict[str, List[str]] = {"undatable": [], "out_of_range": []}


def _rel_str(p: Path) -> str:
    return str(p.relative_to(MEDIA_DIR)).replace(os.sep, "/")


def _month_of_life(age_days: int) -> int:
    return max(0, min(MAX_AGE_MONTHS - 1, int(age_days // DAYS_PER_MONTH)))


def _load_probe_cache_locked() -> None:
    global _probe_cache_loaded
    if _probe_cache_loaded:
        return
    _probe_cache_loaded = True
    try:
        with open(MEDIA_INDEX_PATH, "r") as f:
            raw = json.load(f)
    except Exception:
        return
    # Ages are relative to the birth date, so a changed birth date voids the cache
    if not isinstance(raw, dict) or raw.get("birth_date") != BIRTH_DATE.isoformat():
        return
    for rel, rec in (raw.get("files") or {}).items():
        try:
            age = rec.get("age_days")
            _probe_cache[rel] = (float(rec["mtime"]), int(rec["size"]), None if age is None else int(age))
        except Exception:
            continue


def _save_probe_cache_locked() -> None:
    payload = {
        "birth_date": BIRTH_DATE.isoformat(),
        "files": {
            rel: {"mtime": mtime, "size": size, "age_days": age}
            for rel, (mtime, size, age) in _probe_cache.items()
        },
    }
    tmp_path = f"{MEDIA_INDEX_PATH}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, MEDIA_INDEX_PATH)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def _media_index(force: bool = False) -> List[MediaEntry]:
    """Every datable file inside the game's age range, cached and refreshed lazily."""
    with _index_lock:
        fresh = _index_scanned_at and (time.monotonic() - _index_scanned_at) < MEDIA_INDEX_TTL_SECONDS
        if fresh and not force:
            return _index_entries

    with _index_build_lock:
        with _index_lock:
            fresh = _index_scanned_at and (time.monotonic() - _index_scanned_at) < MEDIA_INDEX_TTL_SECONDS
            if fresh and not force:
                return _index_entries
            _load_probe_cache_locked()
            known = dict(_probe_cache)

        # Stat everything, then probe only what the cache doesn't already cover
        found: List[Tuple[str, Path, float, int]] = []
        for path in _list_media_files():
            try:
                st = path.stat()
            except OSError:
                continue
            found.append((_rel_str(path), path, st.st_mtime, st.st_size))

        stale = [
            (rel, path, mtime, size)
            for (rel, path, mtime, size) in found
            if rel not in known or known[rel][0] != mtime or known[rel][1] != size
        ]
        probed: Dict[str, Tuple[float, int, Optional[int]]] = {}
        if stale:
            workers = max(1, min(MEDIA_PROBE_WORKERS, len(stale)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                ages = pool.map(_age_in_days_for_media_with_fallback, [path for (_r, path, _m, _s) in stale])
                for (rel, _path, mtime, size), age in zip(stale, ages):
                    probed[rel] = (mtime, size, age)

        with _index_lock:
            _probe_cache.update(probed)
            live = {rel for (rel, _p, _m, _s) in found}
            dropped = [rel for rel in _probe_cache if rel not in live]
            for rel in dropped:
                del _probe_cache[rel]

            entries: List[MediaEntry] = []
            skipped: Dict[str, List[str]] = {"undatable": [], "out_of_range": []}
            for rel, path, _mtime, _size in found:
                age = _probe_cache.get(rel, (0.0, 0, None))[2]
                if age is None:
                    skipped["undatable"].append(rel)
                    continue
                # A little slop at either end absorbs timezone edges and party-day
                # shots; anything further out isn't a picture of this age range.
                if not (-AGE_GRACE_DAYS <= age <= MAX_AGE_DAYS + AGE_GRACE_DAYS):
                    skipped["out_of_range"].append(rel)
                    continue
                age = min(max(age, 0), MAX_AGE_DAYS)
                entries.append(MediaEntry(rel, _media_type_for(path), age, _month_of_life(age)))

            _set_index_locked(entries, skipped)
            if probed or dropped:
                _save_probe_cache_locked()
            return _index_entries


def _set_index_locked(entries: List[MediaEntry], skipped: Dict[str, List[str]]) -> None:
    global _index_entries, _index_scanned_at, _index_skipped
    _index_entries = entries
    _index_skipped = skipped
    _index_scanned_at = time.monotonic()


def _month_targets(months_with_media: Iterable[int]) -> Dict[int, float]:
    """How much of the game each month-of-life should get.

    Every month in [0, MAX_AGE_MONTHS) asks for an equal slice. Months we have
    no media for can't be served, so they hand their slice to the nearest
    month(s) that do have some - a gap at month 7 nudges months 6 and 8 up
    rather than quietly biasing the game toward whichever month we shot most.
    """
    available = sorted(set(months_with_media))
    if not available:
        return {}
    targets = {month: 0.0 for month in available}
    slice_size = 1.0 / MAX_AGE_MONTHS
    for month in range(MAX_AGE_MONTHS):
        closest = min(abs(month - other) for other in available)
        nearest = [other for other in available if abs(month - other) == closest]
        for other in nearest:
            targets[other] += slice_size / len(nearest)
    return targets


# Compute a random eligible media candidate WITHOUT mutating global state
# Returns (rel_path, media_type, age_days) or None
def _compute_random_media_candidate(used_rel_paths: set[str]) -> Optional[Tuple[str, str, int]]:
    entries = _media_index()
    if not entries:
        return None

    available = [e for e in entries if e.rel not in used_rel_paths]
    if not available:
        # Every file has been shown; start allowing repeats rather than stalling
        available = entries

    by_month: Dict[int, List[MediaEntry]] = defaultdict(list)
    for entry in available:
        by_month[entry.month].append(entry)
    targets = _month_targets(by_month.keys())

    # Pick the month that is furthest behind its target share so far. Weighting
    # by the shortfall (rather than by the target itself) keeps the months that
    # have plenty of media from clumping up in a single game.
    by_rel = {e.rel: e for e in entries}
    shown = Counter(by_rel[rel].month for rel in used_rel_paths if rel in by_rel)
    picks_so_far = sum(shown.values())
    months = list(by_month.keys())
    weights = [max(0.0, targets[m] * (picks_so_far + 1) - shown[m]) for m in months]
    if sum(weights) <= 0:
        weights = [targets[m] for m in months]

    month = random.choices(months, weights=weights, k=1)[0]
    entry = random.choice(by_month[month])
    return (entry.rel, entry.media_type, entry.age_days)


def _pick_random_media() -> Optional[Tuple[str, str, int]]:
    # Wrapper to preserve legacy call sites; does not mutate used list
    return _compute_random_media_candidate(STATE.used_media)


def _start_pending_pick_background():
    # Called under lock to ensure we do not start multiple workers
    if STATE.pending_preparing:
        return
    STATE.pending_preparing = True
    snapshot_game_id = STATE.game_id

    def worker():
        # A library smaller than the queue keeps handing back files we already
        # hold; give up after a few of those rather than spinning.
        wasted_attempts = 0
        try:
            while wasted_attempts < 20:
                with STATE.lock:
                    if STATE.game_id != snapshot_game_id:
                        STATE.pending_preparing = False
                        return
                    # Stop when queue filled
                    if len(STATE.pending_queue) >= TARGET_PENDING:
                        STATE.pending_preparing = False
                        return
                    # Build reserved set = used + queued + current
                    reserved: set[str] = set(STATE.used_media)
                    reserved.update([r for (r, _t, _a) in STATE.pending_queue])
                    if STATE.current_round and STATE.current_round.media_filename:
                        reserved.add(STATE.current_round.media_filename)
                # Compute candidate outside lock
                pick = _compute_random_media_candidate(reserved)
                if pick is None:
                    with STATE.lock:
                        if STATE.game_id == snapshot_game_id:
                            STATE.pending_preparing = False
                        return
                # Append under lock if still relevant and not duplicate
                with STATE.lock:
                    if STATE.game_id != snapshot_game_id:
                        STATE.pending_preparing = False
                        return
                    rel = pick[0]
                    if any(rel == r for (r, _t, _a) in STATE.pending_queue):
                        wasted_attempts += 1
                    else:
                        wasted_attempts = 0
                        STATE.pending_queue.append(pick)
                        # Maintain first pending_pick for backward compatibility
                        first_rel, first_type, _first_age = STATE.pending_queue[0]
                        STATE.pending_pick = (first_rel, first_type, _first_age)
                        STATE.pending_duration_seconds = (
                            TURN_DURATION_SECONDS_VIDEO if first_type == "video" else TURN_DURATION_SECONDS_IMAGE
                        )
                    # Loop to continue filling until TARGET_PENDING
        finally:
            with STATE.lock:
                if STATE.game_id == snapshot_game_id:
                    STATE.pending_preparing = False

    t = threading.Thread(target=worker, name="pending-queue-worker", daemon=True)
    t.start()


def _ensure_pending_queue_locked():
    # Non-blocking: schedule async preparation if needed
    if not STATE.pending_preparing and len(STATE.pending_queue) < TARGET_PENDING:
        _start_pending_pick_background()


def _start_next_round_locked():
    global STATE
    # Determine duration based on media type once we know it
    duration_seconds = TURN_DURATION_SECONDS_IMAGE

    STATE.current_round_index += 1
    STATE.rounds_remaining = max(0, STATE.total_rounds - STATE.current_round_index)

    # Use pending queue if available; otherwise pick now (synchronous) – only happens at new game start
    if STATE.pending_queue:
        filename, media_type, age_days = STATE.pending_queue.pop(0)
        duration_seconds = TURN_DURATION_SECONDS_VIDEO if media_type == "video" else TURN_DURATION_SECONDS_IMAGE
        # Update the public next pending (first of queue)
        if STATE.pending_queue:
            next_rel, next_type, _next_age = STATE.pending_queue[0]
            STATE.pending_pick = (next_rel, next_type, _next_age)
            STATE.pending_duration_seconds = TURN_DURATION_SECONDS_VIDEO if next_type == "video" else TURN_DURATION_SECONDS_IMAGE
        else:
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

    # If we have a filename, mark it as used now (commit)
    if filename:
        STATE.used_media.add(filename)

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
    # Kick off background filling for subsequent rounds
    _ensure_pending_queue_locked()


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
        _ensure_pending_queue_locked()
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
        # Start preparing next picks asynchronously so clients can prefetch
        _ensure_pending_queue_locked()
        return
    # Transition reveal -> next round or wait for pending
    if rnd.phase == "reveal" and rnd.reveal_ends_at and _now() >= rnd.reveal_ends_at:
        # If pending not ready yet, extend reveal slightly and ensure background picker is running
        if len(STATE.pending_queue) == 0:
            _ensure_pending_queue_locked()
            rnd.reveal_ends_at = _now() + timedelta(seconds=0.5)
            return
        # Pending ready – start next round
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


@app.route("/api/endgame", methods=["POST"])
def end_game():
    """Stop the current game where it stands. Scores stay on the leaderboard
    until the next newgame; the round and its media disappear immediately."""
    with STATE.lock:
        STATE.active = False
        STATE.current_round = None
        STATE.rounds_remaining = 0
        # A fresh game_id makes any background queue worker exit on its next check
        STATE.game_id = uuid.uuid4().hex
        STATE.pending_queue = []
        STATE.pending_pick = None
        STATE.pending_preparing = False
    return jsonify({"ok": True})


@app.route("/api/newgame", methods=["POST"]) 
def new_game():
    # Synchronously prepare the first TARGET_PENDING items so early rounds never wait
    with STATE.lock:
        STATE.reset()
        STATE.active = True
    # Build queue outside lock to avoid blocking other endpoints for long
    wasted_attempts = 0
    while wasted_attempts < 20:
        with STATE.lock:
            if len(STATE.pending_queue) >= TARGET_PENDING:
                break
            snapshot_game_id = STATE.game_id
            # Build reserved set snapshot
            reserved: set[str] = set(STATE.used_media)
            reserved.update([r for (r, _t, _a) in STATE.pending_queue])
            if STATE.current_round and STATE.current_round.media_filename:
                reserved.add(STATE.current_round.media_filename)
        pick = _compute_random_media_candidate(reserved)
        if pick is None:
            break
        with STATE.lock:
            if STATE.game_id != snapshot_game_id:
                break
            rel = pick[0]
            if any(rel == r for (r, _t, _a) in STATE.pending_queue):
                wasted_attempts += 1
            else:
                wasted_attempts = 0
                STATE.pending_queue.append(pick)
                # Maintain first pending_pick for backward compatibility
                first_rel, first_type, _first_age = STATE.pending_queue[0]
                STATE.pending_pick = (first_rel, first_type, _first_age)
                STATE.pending_duration_seconds = (
                    TURN_DURATION_SECONDS_VIDEO if first_type == "video" else TURN_DURATION_SECONDS_IMAGE
                )
    # Start the first round now that we have a prepared queue (or as many as possible)
    with STATE.lock:
        _start_next_round_locked()
        # Begin background filling of pending queue for subsequent rounds
        _ensure_pending_queue_locked()
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
        guess_days = max(0, min(MAX_AGE_DAYS, guess_days))
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
        # Keep the pending queue filling in the background (newgame builds its own)
        if STATE.active:
            _ensure_pending_queue_locked()
        rnd = STATE.current_round
        if rnd and rnd.media_filename:
            version = f"{STATE.game_id}-{STATE.current_round_index}"
            media_url = f"/media/{rnd.media_filename}?v={version}"
        else:
            media_url = None
        # Pending payload for client prefetch (up to TARGET_PENDING items)
        pending_payload = None
        if STATE.pending_queue:
            pending_list = []
            for idx, (pr_filename, pr_type, _pr_age) in enumerate(STATE.pending_queue, start=1):
                pr_version = f"{STATE.game_id}-{STATE.current_round_index + idx}"
                pending_list.append({
                    "media_url": f"/media/{pr_filename}?v={pr_version}",
                    "media_type": pr_type,
                })
            # Backward compatible first item
            first = pending_list[0]
            pending_payload = {
                "media_url": first["media_url"],
                "media_type": first["media_type"],
                "turn_duration_seconds": STATE.pending_duration_seconds,
                "ready": True,
                "list": pending_list[:TARGET_PENDING],
            }
        elif STATE.pending_preparing:
            pending_payload = {
                "ready": False,
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
            "max_age_days": MAX_AGE_DAYS,
            "max_age_months": MAX_AGE_MONTHS,
        }
        return jsonify(response)


@app.route("/api/media_stats")
def media_stats():
    """How the library is spread across the age range, for pre-party sanity checks."""
    entries = _media_index(force=request.args.get("refresh") == "1")
    with _index_lock:
        skipped = {kind: list(rels) for kind, rels in _index_skipped.items()}
    counts = Counter(e.month for e in entries)
    targets = _month_targets(counts.keys())
    return jsonify({
        "total_usable": len(entries),
        "max_age_months": MAX_AGE_MONTHS,
        "months": [
            {
                "month": m,
                "count": counts.get(m, 0),
                "share_of_rounds": round(targets.get(m, 0.0), 4),
            }
            for m in range(MAX_AGE_MONTHS)
        ],
        "skipped": {
            "undatable": {"count": len(skipped["undatable"]), "examples": sorted(skipped["undatable"])[:20]},
            "out_of_range": {"count": len(skipped["out_of_range"]), "examples": sorted(skipped["out_of_range"])[:20]},
        },
    })


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


def _warm_media_index() -> None:
    """Probe the library up front so the first game doesn't wait on ffprobe."""
    def worker():
        try:
            entries = _media_index(force=True)
            print(f"[ageoguessr] media index ready: {len(entries)} files across {MAX_AGE_MONTHS} months")
        except Exception as exc:
            print(f"[ageoguessr] media index warm-up failed: {exc}")

    threading.Thread(target=worker, name="media-index-warmup", daemon=True).start()


_warm_media_index()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True) 