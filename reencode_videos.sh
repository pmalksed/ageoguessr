#!/usr/bin/env bash
# Re-encode every video in the media library to H.264/AAC MP4 so it plays on
# every phone, and cap its size so it can't stall the game.
#
# Why: Google Photos hands back most videos as VP9 (which iOS Safari can't play)
# and leaves Pixel originals as 20 Mbps HEVC (which many Android browsers can't
# play, and which are huge). H.264 + AAC in MP4 is the one combination that
# plays everywhere. The video round is only 20 seconds long and the player
# loops, so trimming to MAXLEN seconds costs nothing visible.
#
# Two modes:
#
#   In place (default)  Each video is replaced by its H.264 version in the same
#                       directory; the original is deleted once the replacement
#                       has been checked. Needs only a few MB of free disk, and
#                       the game keeps running throughout - it just sees .mov
#                       files turn into .mp4 files. Frees ~8 GB net.
#
#   Sibling             Set MEDIA_OUT to a different directory to keep the
#                       originals and swap the whole library in afterwards.
#                       Needs ~6 GB free.
#
# Safe to re-run: every output is tagged, tagged files are skipped, so an
# interrupted run resumes where it stopped.
#
#   nohup bash reencode_videos.sh > reencode.out 2>&1 &
#   tail -f reencode.out

set -u

MEDIA_SRC="${MEDIA_SRC:-/opt/ageoguessr/media}"
MEDIA_OUT="${MEDIA_OUT:-$MEDIA_SRC}"
MAXLEN="${MAXLEN:-45}"          # seconds kept from the start of each video
NICE="${NICE:-19}"              # stay out of the running game's way
TAG="ageoguessr-h264-v1"        # stamped into every output so we can recognise our own work

command -v ffmpeg >/dev/null || { echo "ffmpeg not found; install it first"; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not found; install ffmpeg first"; exit 1; }
[ -d "$MEDIA_SRC" ] || { echo "no such directory: $MEDIA_SRC"; exit 1; }
mkdir -p "$MEDIA_OUT"

INPLACE=0
[ "$(cd "$MEDIA_SRC" && pwd -P)" = "$(cd "$MEDIA_OUT" && pwd -P)" ] && INPLACE=1

# Partial outputs live outside the media dir so the game never indexes them.
WORK="$(dirname "$(cd "$MEDIA_OUT" && pwd -P)")/.reencode_tmp"
mkdir -p "$WORK"

is_video() {
  case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
    *.mov|*.mp4|*.webm|*.m4v) return 0 ;;
    *) return 1 ;;
  esac
}
creation_time() { ffprobe -v error -show_entries format_tags=creation_time -of default=nw=1:nk=1 "$1" 2>/dev/null | head -1; }
already_done()  { [ -s "$1" ] && ffprobe -v error -show_entries format_tags=comment -of default=nw=1:nk=1 "$1" 2>/dev/null | grep -qx "$TAG"; }
size_of()       { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }

# ---------------------------------------------------------------- videos ----
videos=()
while IFS= read -r -d '' f; do
  is_video "$f" && videos+=("$f")
done < <(find "$MEDIA_SRC" -maxdepth 1 -type f -not -name '.*' -print0 | sort -z)

total=${#videos[@]}
if [ "$total" -eq 0 ]; then echo "no videos found in $MEDIA_SRC"; exit 0; fi

if [ $INPLACE -eq 1 ]; then
  echo "$(date '+%H:%M:%S')  $total videos in $MEDIA_SRC, converting IN PLACE (first ${MAXLEN}s of each)"
else
  echo "$(date '+%H:%M:%S')  $total videos in $MEDIA_SRC -> $MEDIA_OUT (first ${MAXLEN}s of each)"
fi
echo

failed=()
converted=0
skipped=0
freed=0
n=0
started=$(date +%s)

for f in "${videos[@]}"; do
  base=$(basename "$f")
  stem="${base%.*}"
  out="$MEDIA_OUT/$stem.mp4"
  n=$((n + 1))

  # Already one of our outputs (in-place resume): nothing to do.
  if already_done "$f"; then
    skipped=$((skipped + 1))
    continue
  fi
  # The output name is taken by a *different* original (e.g. IMG_1.mov and
  # IMG_1.mp4 both exist): don't clobber it, give this one its own name.
  if [ "$out" != "$f" ] && [ -e "$out" ] && ! already_done "$out"; then
    out="$MEDIA_OUT/${stem}_${base##*.}.mp4"
  fi
  # Converted on an earlier run but the source was left behind (interrupted
  # between the rename and the delete): finish the job.
  if [ "$out" != "$f" ] && already_done "$out"; then
    if [ $INPLACE -eq 1 ]; then
      freed=$((freed + $(size_of "$f")))
      rm -f "$f"
    fi
    skipped=$((skipped + 1))
    continue
  fi

  tmp="$WORK/$stem.part.mp4"
  # Scale so the long side is at most 1280 (720p), keeping aspect and even
  # dimensions. ffmpeg applies rotation metadata before scaling, so portrait
  # phone video comes out physically portrait with no rotation tag, which is
  # what browsers handle best. Force limited-range 8-bit yuv420p.
  if nice -n "$NICE" ffmpeg -hide_banner -loglevel error -nostdin -y \
      -i "$f" -t "$MAXLEN" \
      -map 0:v:0 -map '0:a:0?' -map_metadata 0 -metadata comment="$TAG" \
      -vf "scale=w=1280:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=2:out_range=tv" \
      -r 30 \
      -c:v libx264 -preset veryfast -crf 24 -maxrate 3M -bufsize 6M \
      -pix_fmt yuv420p -profile:v high -level 4.0 \
      -c:a aac -b:a 128k -ac 2 \
      -movflags +faststart \
      "$tmp"
  then
    # The game dates videos from this tag; refuse to replace anything that lost it.
    src_ct=$(creation_time "$f")
    out_ct=$(creation_time "$tmp")
    if [ -n "$src_ct" ] && [ "$src_ct" != "$out_ct" ]; then
      mv -f "$tmp" "$WORK/$stem.badtime.mp4"
      failed+=("$base   (creation_time changed: '$src_ct' -> '$out_ct'; original kept, output parked in $WORK)")
      echo "$(date '+%H:%M:%S')  [$n/$total]  KEPT ORIGINAL (creation_time changed): $base"
      continue
    fi

    in_bytes=$(size_of "$f")
    out_bytes=$(size_of "$tmp")
    mv -f "$tmp" "$out"                              # same-name .mp4: replaces the original atomically
    if [ $INPLACE -eq 1 ]; then
      [ "$out" != "$f" ] && rm -f "$f"
      freed=$((freed + in_bytes - out_bytes))
    fi
    converted=$((converted + 1))

    elapsed=$(( $(date +%s) - started ))
    remaining=$(( (total - n) * elapsed / n ))
    printf '%s  [%3d/%3d]  %-40s %5d MB -> %3d MB   freed so far %d.%d GB   ~%dm left\n' \
      "$(date '+%H:%M:%S')" "$n" "$total" "$base" "$((in_bytes / 1048576))" "$((out_bytes / 1048576))" \
      "$((freed / 1073741824))" "$((freed % 1073741824 * 10 / 1073741824))" "$((remaining / 60))"
  else
    rm -f "$tmp"
    failed+=("$base   (ffmpeg failed; original kept)")
    echo "$(date '+%H:%M:%S')  [$n/$total]  FAILED: $base"
  fi
done

# ---------------------------------------------------------------- photos ----
# Sibling mode only: everything that isn't a video comes across as a hardlink
# (same bytes, same mtime, zero extra disk).
linked=0
if [ $INPLACE -eq 0 ]; then
  while IFS= read -r -d '' f; do
    is_video "$f" && continue
    base=$(basename "$f")
    ln -f "$f" "$MEDIA_OUT/$base" 2>/dev/null || cp -p "$f" "$MEDIA_OUT/$base"
    linked=$((linked + 1))
  done < <(find "$MEDIA_SRC" -maxdepth 1 -type f -not -name '.*' -print0)
fi

rmdir "$WORK" 2>/dev/null   # only succeeds if nothing got parked there

# --------------------------------------------------------------- summary ----
echo
echo "================================================================"
echo "converted this run:   $converted"
echo "already done/skipped: $skipped"
echo "failed:               ${#failed[@]}"
if [ $INPLACE -eq 1 ]; then
  echo "disk freed:           $((freed / 1073741824)).$((freed % 1073741824 * 10 / 1073741824)) GB"
  echo "library now:          $(du -sh "$MEDIA_SRC" | cut -f1)"
else
  echo "photos linked:        $linked"
  echo "output size:          $(du -sh "$MEDIA_OUT" | cut -f1)   (originals: $(du -sh "$MEDIA_SRC" | cut -f1))"
fi
if [ ${#failed[@]} -gt 0 ]; then
  echo
  echo "These were NOT replaced and are still in their original format:"
  printf '   %s\n' "${failed[@]}"
fi
echo
if [ $INPLACE -eq 1 ]; then
  echo "Nothing more to do: the game re-scans its media directory every 30s and"
  echo "has been picking up the new .mp4 files as they landed. Check with:"
  echo "   curl -s localhost:5000/api/media_stats | python3 -m json.tool | head -20"
else
  echo "Spot-check a couple of files in $MEDIA_OUT, then swap the library in:"
  echo "   mv '$MEDIA_SRC' '${MEDIA_SRC}_original' && mv '$MEDIA_OUT' '$MEDIA_SRC'"
  echo "The game re-scans every 30s and will pick the new files up on its own."
  echo "Once you're happy, '${MEDIA_SRC}_original' is safe to delete."
fi
