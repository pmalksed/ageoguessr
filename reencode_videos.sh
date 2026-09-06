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
# Writes into a SEPARATE directory (MEDIA_OUT) and never touches the originals.
# Photos are hardlinked across (no extra disk). When it finishes it prints the
# commands to swap the new library into place.
#
# Safe to re-run: finished outputs are skipped, so an interrupted run resumes.
#
#   nohup bash reencode_videos.sh > reencode.out 2>&1 &
#   tail -f reencode.out

set -u

MEDIA_SRC="${MEDIA_SRC:-/opt/ageoguessr/media}"
MEDIA_OUT="${MEDIA_OUT:-/opt/ageoguessr/media_h264}"
MAXLEN="${MAXLEN:-45}"          # seconds kept from the start of each video
NICE="${NICE:-19}"              # stay out of the running game's way

command -v ffmpeg >/dev/null || { echo "ffmpeg not found; install it first (apt install ffmpeg)"; exit 1; }
[ -d "$MEDIA_SRC" ] || { echo "no such directory: $MEDIA_SRC"; exit 1; }
mkdir -p "$MEDIA_OUT"

is_video() {
  case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
    *.mov|*.mp4|*.webm|*.m4v) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- videos ----
videos=()
while IFS= read -r -d '' f; do
  is_video "$f" && videos+=("$f")
done < <(find "$MEDIA_SRC" -maxdepth 1 -type f -print0 | sort -z)

total=${#videos[@]}
echo "$(date '+%H:%M:%S')  $total videos in $MEDIA_SRC -> $MEDIA_OUT (first ${MAXLEN}s of each)"
echo

failed=()
done_count=0
started=$(date +%s)

for f in "${videos[@]}"; do
  base=$(basename "$f")
  out="$MEDIA_OUT/${base%.*}.mp4"
  done_count=$((done_count + 1))

  if [ -s "$out" ]; then
    continue                      # already converted on a previous run
  fi

  # Scale so the long side is at most 1280 (720p), keeping aspect and even
  # dimensions. ffmpeg auto-applies rotation metadata before scaling, so
  # portrait phone video comes out physically portrait with no rotation tag,
  # which is what browsers handle best. Force limited-range 8-bit yuv420p.
  if nice -n "$NICE" ffmpeg -hide_banner -loglevel error -nostdin -y \
      -i "$f" -t "$MAXLEN" \
      -map 0:v:0 -map '0:a:0?' -map_metadata 0 \
      -vf "scale=w=1280:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=2:out_range=tv" \
      -r 30 \
      -c:v libx264 -preset veryfast -crf 24 -maxrate 3M -bufsize 6M \
      -pix_fmt yuv420p -profile:v high -level 4.0 \
      -c:a aac -b:a 128k -ac 2 \
      -movflags +faststart \
      "$out.part.mp4" \
     && mv -f "$out.part.mp4" "$out"
  then
    # The game dates videos from this tag; make sure it made it across.
    src_ct=$(ffprobe -v error -show_entries format_tags=creation_time -of default=nw=1:nk=1 "$f" 2>/dev/null | head -1)
    out_ct=$(ffprobe -v error -show_entries format_tags=creation_time -of default=nw=1:nk=1 "$out" 2>/dev/null | head -1)
    note=""
    if [ -n "$src_ct" ] && [ "$src_ct" != "$out_ct" ]; then
      note="   !! creation_time changed: '$src_ct' -> '$out_ct'"
    fi
    in_mb=$(( $(stat -c %s "$f" 2>/dev/null || stat -f %z "$f") / 1048576 ))
    out_mb=$(( $(stat -c %s "$out" 2>/dev/null || stat -f %z "$out") / 1048576 ))
    elapsed=$(( $(date +%s) - started ))
    remaining=$(( (total - done_count) * elapsed / done_count ))
    printf '%s  [%3d/%3d]  %-40s %5d MB -> %3d MB   ~%dm left%s\n' \
      "$(date '+%H:%M:%S')" "$done_count" "$total" "$base" "$in_mb" "$out_mb" "$((remaining / 60))" "$note"
  else
    rm -f "$out.part.mp4"
    failed+=("$base")
    echo "$(date '+%H:%M:%S')  [$done_count/$total]  FAILED: $base"
  fi
done

# ---------------------------------------------------------------- photos ----
# Everything that isn't a video comes across as a hardlink: same bytes, same
# mtime, zero extra disk, and the game's capture-time cache still matches.
linked=0
while IFS= read -r -d '' f; do
  is_video "$f" && continue
  base=$(basename "$f")
  ln -f "$f" "$MEDIA_OUT/$base" 2>/dev/null || cp -p "$f" "$MEDIA_OUT/$base"
  linked=$((linked + 1))
done < <(find "$MEDIA_SRC" -maxdepth 1 -type f -print0)

# --------------------------------------------------------------- summary ----
echo
echo "================================================================"
echo "videos converted: $(( total - ${#failed[@]} )) / $total"
echo "photos linked:    $linked"
echo "output size:      $(du -sh "$MEDIA_OUT" | cut -f1)   (originals: $(du -sh "$MEDIA_SRC" | cut -f1))"
if [ ${#failed[@]} -gt 0 ]; then
  echo
  echo "FAILED (${#failed[@]}) - these will be missing from the new library:"
  printf '   %s\n' "${failed[@]}"
fi
echo
echo "Spot-check a couple of files in $MEDIA_OUT, then swap the library in:"
echo
echo "   mv '$MEDIA_SRC' '${MEDIA_SRC}_original' && mv '$MEDIA_OUT' '$MEDIA_SRC'"
echo
echo "The game re-scans its media directory every 30s, so it will pick up the"
echo "new files on its own; restarting the service just makes it immediate."
echo "Once you're happy, '${MEDIA_SRC}_original' is safe to delete."
