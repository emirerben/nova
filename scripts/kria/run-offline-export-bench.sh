#!/usr/bin/env bash
# Run from a bootstrapped Nova checkout with a local iPhone simulator.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 SIMULATOR_UDID NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi

cd "$(git rev-parse --show-toplevel)"
python_run="${KRIA_BENCH_PYTHON:-src/apps/api/.venv/bin/python}"
mkdir "$2"
output_dir="$(cd "$2" && pwd -P)"
start=$SECONDS
"$python_run" scripts/ios/duration-text-preservation-fixtures.py --output "$output_dir/duration-text.json"
cmp src/apps/ios/Tests/Fixtures/DurationTextPreservation.json "$output_dir/duration-text.json"
"$python_run" scripts/ios/phone-montage-render-e2e.py "$output_dir/inputs"
"$python_run" scripts/ios/phone-montage-render-e2e.py "$output_dir/repeated"
bash scripts/ios/generate-project.sh

package_args=(-skipPackageUpdates)
if [[ -n "${KRIA_BENCH_PACKAGE_CACHE:-}" ]]; then
  package_args+=(-clonedSourcePackagesDirPath "$KRIA_BENCH_PACKAGE_CACHE")
fi
tests=()
for test_name in \
  testVoiceoverNarrationRendersOnTheIPhone \
  testSubtitledSentenceCaptionsRenderOnTheIPhone \
  testSubtitledWordCaptionsRenderOnTheIPhone \
  testSubtitledLandscapeLetterboxRendersOnTheIPhone \
  testSubtitledFullscreenVisualsRenderOnTheIPhone \
  testTalkingHeadCutawayRendersOnTheIPhone \
  testNarratedWalkthroughRendersOnTheIPhone; do
  tests+=("-only-testing:KriaTests/DeviceMontageRenderE2ETests/$test_name")
done
render_status=0
TEST_RUNNER_KRIA_E2E_DIR="$output_dir/inputs" xcodebuild \
  -project src/apps/ios/Kria.xcodeproj -scheme Kria "${package_args[@]}" \
  -skipPackagePluginValidation -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO \
  -derivedDataPath "${KRIA_BENCH_BUILD_DIR:-$output_dir/build}" \
  -destination "platform=iOS Simulator,id=$1" -resultBundlePath "$output_dir/native.xcresult" \
  "${tests[@]}" test > "$output_dir/xcode.log" 2>&1 || render_status=$?
score_status=0
"$python_run" scripts/kria/score_offline_exports.py "$output_dir/inputs" "$output_dir/repeated" \
  "$output_dir/scores.json" || score_status=$?
printf 'Elapsed seconds %d. Xcode exit %d. Scorer exit %d. Results in %s\n' \
  "$((SECONDS-start))" "$render_status" "$score_status" "$output_dir" | tee "$output_dir/run.txt"
(( render_status == 0 && score_status == 0 ))
