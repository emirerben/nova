#!/usr/bin/env bash
# Generate production-shape reference clips for the single-pass parity gate.
#
# The earlier testsrc / smptebars / color lavfi sources produced
# noise-free pixel-clean clips that exercised the encoder very differently
# from real user footage. This script generates three clips with the
# encoder-stressing characteristics that matter:
#
#   clip1.mp4 — smooth gradient + film grain + slow zoom. Mirrors a
#               landscape / b-roll shot. Tests low-frequency macroblock
#               behavior under grain.
#   clip2.mp4 — rotating high-detail testsrc2 + film grain + drawtext.
#               Mirrors an action / hook shot with on-screen caption.
#               Tests high-frequency motion and overlay alpha mixing.
#   clip3.mp4 — dark color field + temporal luma drift + grain + drawtext.
#               Mirrors night / low-light footage. This is the brazil.mp4
#               macroblocking class (smooth dark gradient + grain) that
#               PRs #102/#105/#116 fought.
#
# Why three: the parity gate must demonstrate the single-pass and
# multi-pass paths produce visually-equivalent output across the encoder
# stress modes (gradient, high-frequency motion, dark gradient). Three
# clips × four fixtures (2-3 slots each) exercises every code path
# without ballooning runtime.
#
# Usage:
#   tests/scripts/gen_real_shape_clips.sh /tmp/real-shape-clips
#   PARITY_CLIPS_DIR=/tmp/real-shape-clips pytest tests/quality/
#
# Note: these are NOT actual Nova-user uploads. They're production-shape
# synthetic content with encoder-stressing characteristics. For a true
# production-grade parity sweep you still need real user-shot footage
# staged in GCS — see tests/benchmarks/single_pass_rollout_runbook.md.
set -euo pipefail

OUTDIR="${1:-/tmp/real-shape-clips}"
mkdir -p "$OUTDIR"

echo "Generating production-shape clips into $OUTDIR..."

# Clip 1: gradient + grain + slow zoom.
ffmpeg -y -nostdin -loglevel error \
  -f lavfi -i "gradients=size=1080x1920:duration=5:speed=0.05:n=4:type=linear" \
  -f lavfi -i "sine=frequency=440:duration=5" \
  -filter_complex "[0:v]noise=alls=12:allf=t+u,zoompan=z='1+0.1*sin(in/15)':d=150:s=1080x1920:fps=30,format=yuv420p[v]" \
  -map "[v]" -map 1:a \
  -c:v libx264 -preset ultrafast -crf 22 -pix_fmt yuv420p \
  -c:a aac -b:a 96k -ar 44100 -ac 2 -shortest "$OUTDIR/clip1.mp4" &

# Clip 2: high-frequency motion + grain + caption.
ffmpeg -y -nostdin -loglevel error \
  -f lavfi -i "testsrc2=size=1920x1080:duration=5:rate=30" \
  -f lavfi -i "sine=frequency=880:duration=5" \
  -filter_complex "[0:v]noise=alls=15:allf=t+u,rotate='PI*sin(t/2)/8':c=black:ow=1080:oh=1920,drawtext=text='REAL MOTION':x='(w-text_w)/2':y='h*0.8+50*sin(t)':fontsize=72:fontcolor=white:box=1:boxcolor=black@0.5,format=yuv420p[v]" \
  -map "[v]" -map 1:a \
  -c:v libx264 -preset ultrafast -crf 22 -pix_fmt yuv420p \
  -c:a aac -b:a 96k -ar 44100 -ac 2 -shortest "$OUTDIR/clip2.mp4" &

# Clip 3: dark gradient + temporal luma drift + caption (brazil.mp4 class).
ffmpeg -y -nostdin -loglevel error \
  -f lavfi -i "color=c=0x1a2030:size=1080x1920:duration=5:rate=30" \
  -f lavfi -i "sine=frequency=220:duration=5" \
  -filter_complex "[0:v]noise=alls=8:allf=t+u,geq=lum='lum(X,Y) + 10*sin(2*PI*T/3)':cb='cb(X,Y)':cr='cr(X,Y)',drawtext=text='DARK GRADIENT':x=(w-text_w)/2:y=h/2:fontsize=64:fontcolor=0x6080a0,format=yuv420p[v]" \
  -map "[v]" -map 1:a \
  -c:v libx264 -preset ultrafast -crf 22 -pix_fmt yuv420p \
  -c:a aac -b:a 96k -ar 44100 -ac 2 -shortest "$OUTDIR/clip3.mp4" &

wait
ls -lh "$OUTDIR"/*.mp4
echo "Done. Run: PARITY_CLIPS_DIR=$OUTDIR pytest tests/quality/single_pass_parity.py"
