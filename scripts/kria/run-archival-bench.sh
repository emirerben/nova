#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 3 ]]; then
  echo "Usage: $0 SIMULATOR_UDID RAW_SOURCE_DIRECTORY_OR_- NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi

cd "$(git rev-parse --show-toplevel)"
python_run="${KRIA_BENCH_PYTHON:-src/apps/api/.venv/bin/python}"
start=$SECONDS
"$python_run" scripts/kria/archival_bench.py prepare "$2" "$3"
output="$(cd "$3" && pwd -P)"
bash scripts/ios/generate-project.sh
packages=(-skipPackageUpdates)
if [[ -n "${KRIA_BENCH_PACKAGE_CACHE:-}" ]]; then
  packages+=(-clonedSourcePackagesDirPath "$KRIA_BENCH_PACKAGE_CACHE")
fi
TEST_RUNNER_KRIA_E2E_DIR="$output" xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria \
  "${packages[@]}" -skipPackagePluginValidation -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO \
  -derivedDataPath "${KRIA_BENCH_BUILD_DIR:-$output/build}" -destination "platform=iOS Simulator,id=$1" \
  -resultBundlePath "$output/native.xcresult" \
  -only-testing:KriaTests/DeviceMontageRenderE2ETests/testArchivalMontagesRenderOnTheIPhone \
  test > "$output/xcode.log" 2>&1
"$python_run" scripts/kria/archival_bench.py score "$2" "$output"
printf 'Elapsed %d seconds; provider cost USD 0\n' "$((SECONDS-start))" | tee "$output/run.txt"
