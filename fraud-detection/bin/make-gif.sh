#!/usr/bin/env bash
# Convert the recorded correction video into assets a blog post can use.
#
#   bun bin/record-correction.ts   # writes docs/correction.webm
#   ./bin/make-gif.sh                     # -> docs/correction.gif + docs/correction.mp4
#
# Two outputs on purpose: mp4 is a third the size and much sharper, but a GIF drops
# into anything without a player. Prefer the mp4 where the venue allows it.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="${1:-docs/correction.webm}"
[ -f "$SRC" ] || { echo "no $SRC — run: bun bin/record-correction.ts" >&2; exit 1; }

WIDTH="${WIDTH:-1000}"
FPS="${FPS:-12}"

echo "== mp4"
ffmpeg -v error -y -i "$SRC" \
  -vf "scale=${WIDTH}:-2:flags=lanczos" \
  -c:v libx264 -pix_fmt yuv420p -crf 23 -movflags +faststart \
  docs/correction.mp4

echo "== gif (two-pass palette)"
PAL="$(mktemp -t palette.XXXXXX).png"
ffmpeg -v error -y -i "$SRC" \
  -vf "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos,palettegen=stats_mode=diff" "$PAL"
ffmpeg -v error -y -i "$SRC" -i "$PAL" \
  -lavfi "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3" \
  docs/correction.gif
rm -f "$PAL"

ls -lh docs/correction.mp4 docs/correction.gif
