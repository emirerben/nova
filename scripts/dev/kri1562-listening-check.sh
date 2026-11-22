#!/usr/bin/env bash
# KRI-1562 listening check, one command: prepare -> native export -> measure.
#
#   scripts/dev/kri1562-listening-check.sh [--takes DIR] [--song PATH] [--model PATH]
#   scripts/dev/kri1562-listening-check.sh --synthetic
#
# DIR (default .dev/kri-1562-takes) holds one talking clip or more in quiet/ and in noisy/.
# Writes .dev/kri-1562-listening/<run-id>/report.{json,md}; exits 1 when a criterion fails.
# Details: scripts/dev/kri1562_listening_check.py.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/src/apps/api/.venv/bin/python"
DRIVER="$REPO/scripts/dev/kri1562_listening_check.py"
export PATH="/opt/homebrew/bin:$PATH"

RUN_ID="$(date +%Y%m%d-%H%M%S)"
for arg in "$@"; do [ "$arg" = "--synthetic" ] && RUN_ID="$RUN_ID-synthetic"; done
RUN="$REPO/.dev/kri-1562-listening/$RUN_ID"

"$PY" "$DRIVER" prepare "$RUN" "$@"
(
  cd "$REPO/src/apps/ios/Packages/KriaMediaEngine"
  # The opt-in package test renders every prepared case dir through the production exporter.
  KRIA_AUDIO_PARITY_DIR="$RUN" swift test --filter AudioParityFixtureTests \
    --skip "SpeakerBackground|SpeakerFrame|SpeakerMask|SpeakerMatte|ClipTransitionComposition"
)
"$PY" "$DRIVER" measure "$RUN"
