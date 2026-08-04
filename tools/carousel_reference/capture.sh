#!/usr/bin/env bash
# capture.sh <effect> <out_dir>
#
# Drives the gstack browse daemon through one of the four reference pages
# (scale-sweep | cover-flow | cards | flipbook): loads the page at a fixed
# 1080x1920 viewport (scale 1, so on-disk pixels == CSS px, matching the
# harness's deterministic-clock/DPR-1 contract — see README.md), replays
# the scripted flick gesture one deterministic 30fps frame at a time via
# window.__step(), screenshots every frame, stops once window.__settled
# flips true (or a hard cap is hit), dumps the per-frame motion trace to
# trace.json, and muxes the frames into reference.mp4.
#
# NOT run as part of this lane — capture happens later in the parity loop,
# once the browse daemon + a display-capable Chromium are confirmed
# available in that environment. See README.md "Capturing" for prerequisites.
set -euo pipefail

EFFECT="${1:?usage: capture.sh <effect> <out_dir>}"
OUT_DIR="${2:?usage: capture.sh <effect> <out_dir>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAGE="$SCRIPT_DIR/${EFFECT}.html"

if [ ! -f "$PAGE" ]; then
  echo "error: no such effect page: $PAGE" >&2
  echo "expected one of: scale-sweep cover-flow cards flipbook" >&2
  exit 1
fi

# Resolve the browse CLI entrypoint the same way the SKILL.md preamble does:
# project-local vendored copy first, then the user-global install.
B=""
if [ -x "./.claude/skills/gstack/browse/dist/browse" ]; then
  B="./.claude/skills/gstack/browse/dist/browse"
elif [ -x "$HOME/.claude/skills/gstack/browse/dist/browse" ]; then
  B="$HOME/.claude/skills/gstack/browse/dist/browse"
fi
if [ -z "$B" ] || [ ! -x "$B" ]; then
  echo "error: browse CLI not found (checked ./.claude/skills/gstack/browse/dist/browse and ~/.claude/skills/gstack/browse/dist/browse)" >&2
  exit 1
fi

MAX_FRAMES=150 # hard cap: 5s @ 30fps of settle-polling after the 12-delta gesture

mkdir -p "$OUT_DIR"

echo "capture.sh: $EFFECT -> $OUT_DIR"

# Serve the page over a local HTTP server rather than navigating file://
# directly. Chromium treats file:// as an opaque "null" origin, and
# `<script type="module">` imports (used by all four pages to load the
# vendored Blossom ESM build) are fetched in CORS mode — file:// is not an
# allowed scheme for that fetch, so the import silently fails
# (net::ERR_FAILED, "Cross origin requests are only supported for protocol
# schemes: chrome, chrome-untrusted, data, http, https"), the custom
# element never registers, `customElements.whenDefined("blossom-carousel")`
# never resolves, and `#ready` never appears — capture.sh then times out in
# `wait "#ready"`. Classic scripts (harness.js) are unaffected, which is
# why the clock/rAF stubs installed fine and only the module import broke.
# harness.js's `fetch("./gesture-trace.json")` would hit the same file://
# restriction on the next step, so serving locally fixes both in one move.
# See README.md "Determinism contract" — serving statically over loopback
# HTTP does not introduce any real-time dependence into captured frames;
# it only changes how the already-static files are transported to the
# browser.
HTTP_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
python3 -m http.server "$HTTP_PORT" --bind 127.0.0.1 --directory "$SCRIPT_DIR" >/dev/null 2>&1 &
HTTP_PID=$!
trap 'kill "$HTTP_PID" 2>/dev/null || true; wait "$HTTP_PID" 2>/dev/null || true' EXIT

# Wait for the server to accept connections before handing off to browse.
for _ in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:${HTTP_PORT}/${EFFECT}.html" -o /dev/null; then
    break
  fi
  sleep 0.1
done

"$B" viewport 1080x1920 --scale 1
"$B" goto "http://127.0.0.1:${HTTP_PORT}/${EFFECT}.html"
"$B" wait "#ready"
"$B" js "window.__startGesture()"

FRAME=0
while [ "$FRAME" -lt "$MAX_FRAMES" ]; do
  "$B" js "window.__step()" >/dev/null
  PADDED=$(printf "%04d" "$FRAME")
  # --viewport (not full-page default): full-page screenshots came out
  # 1080x1921 (1px taller than the fixed 1080x1920 page geometry, likely
  # sub-pixel/scrollHeight rounding in Chromium's full-page capture path),
  # which fails libx264 (needs even height) and breaks the DPR-1 pixel
  # contract. Viewport-clipped screenshots are exactly 1080x1920.
  "$B" screenshot --viewport "$OUT_DIR/frame_${PADDED}.png"

  SETTLED=$("$B" js "window.__settled")
  FRAME=$((FRAME + 1))
  if [ "$SETTLED" = "true" ]; then
    echo "capture.sh: settled after $FRAME frames"
    break
  fi
done

if [ "$FRAME" -ge "$MAX_FRAMES" ]; then
  echo "capture.sh: warning: hit MAX_FRAMES=$MAX_FRAMES without settling" >&2
fi

"$B" js "window.__getTrace()" > "$OUT_DIR/trace.json"

ffmpeg -y -framerate 30 -i "$OUT_DIR/frame_%04d.png" \
  -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p \
  "$OUT_DIR/reference.mp4"

echo "capture.sh: done -> $OUT_DIR/reference.mp4 (+ trace.json, frame_*.png)"
