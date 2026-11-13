#!/usr/bin/env bash
# Use take A/B on the paired iPhone (KRI-1184): a build with the take prewarm (this tree) against a build without it (a checkout of
# main), at several dwell times in Review, interleaved so thermal drift hits every cell alike.
#
#   scripts/ios/talk-usetake-ab.sh --baseline /path/to/main-checkout --dwells 0,5,20 --runs 3 --device-id <udid>
#
# Each cell is one scripted Standard (Cinematic off) take through talk-bench.sh --use-take --use-take-dwell D, so the app taps Use take
# D seconds after Review opens. The scripted fixture has no backend that accepts the upload: the flow fails at reserve, which is
# fine, stageCopy / hash / proxyTranscode and the spinner are measured before and around it. Output: .talk-usetake/<label>/<build>-d<D>-r<N>/
# (summary.json, polled/ proxy and staged copies, xcodebuild.log) and a table from talk_usetake_ab_report.py.
set -euo pipefail

THIS="$(git rev-parse --show-toplevel)"
BASELINE=""; DWELLS="0,5,20"; RUNS=3; SECONDS_PER_TAKE=30; AFTER=12; DEVICE_ID="${KRIA_DEVICE_ID:-}"; LABEL=""; SKIP_BUILD=0; OFFSET=0; BUILDS="pr base"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --baseline) BASELINE="$2"; shift 2 ;;
    --dwells) DWELLS="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --seconds) SECONDS_PER_TAKE="$2"; shift 2 ;;
    --after) AFTER="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --run-offset) OFFSET="$2"; shift 2 ;;
    --builds) BUILDS="${2//,/ }"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -d "$BASELINE" && -n "$DEVICE_ID" ]] || { echo "Pass --baseline <checkout of main> and --device-id <udid>" >&2; exit 2; }
[[ -n "$LABEL" ]] || LABEL="$(date +%Y%m%d-%H%M%S)"
OUT="$THIS/.talk-usetake/$LABEL"
mkdir -p "$OUT"
IFS=',' read -ra DWELL_LIST <<<"$DWELLS"

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  for tree in "$THIS" "$BASELINE"; do
    (cd "$tree" && bash scripts/ios/generate-project.sh &&
      xcodebuild build-for-testing -project src/apps/ios/Kria.xcodeproj -scheme "Kria Hardware QA" -configuration Debug \
        -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath src/apps/ios/.derived-data -allowProvisioningUpdates -enableCodeCoverage NO >"$OUT/build-$(basename "$tree").log" 2>&1)
  done
fi

for run in $(seq 1 "$RUNS"); do
  for dwell in "${DWELL_LIST[@]}"; do
    for build in $BUILDS; do
      tree="$THIS"; [[ "$build" == base ]] && tree="$BASELINE"
      cell="$build-d$dwell-r$((run + OFFSET))"
      echo "talk-usetake: $cell"  # --run-offset N continues an interrupted label as runs N+1...
      (cd "$tree" && bash scripts/ios/talk-bench.sh --skip-build --runs 1 --scenarios none-cin0 --seconds "$SECONDS_PER_TAKE" \
        --use-take --use-take-dwell "$dwell" --use-take-after "$AFTER" --poll-files --device-id "$DEVICE_ID" --label "ab-$LABEL-$cell" || true)
      rm -rf "$OUT/$cell"; mkdir -p "$OUT/$cell"
      cp -R "$tree/.talk-bench/ab-$LABEL-$cell/none-cin0--run1"/. "$OUT/$cell"/ 2>/dev/null || echo "talk-usetake: no output for $cell" >&2
    done
  done
done
python3 "$THIS/scripts/ios/talk_usetake_ab_report.py" "$OUT" | tee "$OUT/table.md"
