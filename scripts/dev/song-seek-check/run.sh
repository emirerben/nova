#!/usr/bin/env bash
# Measures how far AVFoundation lands from the requested start when a song is inserted into a
# composition at `start` seconds, with and without precise timing (KRI-374 song-offset fix).
# Needs ffmpeg, swiftc and python3 with numpy.   usage: run.sh song.mp3 [start_s]
# A plain AVURLAsset put the founder's VBR MP3 at 77.29 s when asked for 78.23 s; precise timing: 78.23.
set -euo pipefail
song=${1:?song file}; start=${2:-78.23}
work=$(mktemp -d); here=$(cd "$(dirname "$0")" && pwd)
swiftc -O "$here/seek.swift" -o "$work/seek" 2>/dev/null
ffmpeg -v error -y -i "$song" -ac 1 -ar 8000 -f f32le "$work/song.f32"
for precise in 0 1; do
  "$work/seek" "$song" "$start" 12 "$precise" "$work/out_$precise.m4a" >/dev/null
  ffmpeg -v error -y -i "$work/out_$precise.m4a" -ac 1 -ar 8000 -f f32le "$work/out_$precise.f32"
done
python3 "$here/locate.py" "$work/song.f32" "$start" "$work/out_0.f32" "$work/out_1.f32"
