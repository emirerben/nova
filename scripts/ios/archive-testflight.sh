#!/usr/bin/env bash

# Archive the production iOS target for App Store Connect. This script accepts
# configuration only through the environment so credentials and account values
# never need to be committed to the repository.
set -euo pipefail

die() {
  echo "error: $*" >&2
  exit 2
}

require_value() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "$name must be set for a TestFlight archive"
}

for required in \
  KRIA_DEVELOPMENT_TEAM \
  KRIA_PROVISIONING_PROFILE_SPECIFIER \
  KRIA_GOOGLE_CLIENT_ID \
  KRIA_GOOGLE_REDIRECT_SCHEME \
  KRIA_MARKETING_VERSION \
  KRIA_BUILD_NUMBER; do
  require_value "$required"
done

[[ "$KRIA_DEVELOPMENT_TEAM" =~ ^[A-Z0-9]{10}$ ]] || die "KRIA_DEVELOPMENT_TEAM must be a 10-character Apple team ID"
[[ "$KRIA_GOOGLE_CLIENT_ID" == *.apps.googleusercontent.com ]] || die "KRIA_GOOGLE_CLIENT_ID must be an iOS Google OAuth client ID"
[[ "$KRIA_GOOGLE_REDIRECT_SCHEME" =~ ^[A-Za-z][A-Za-z0-9+.-]*$ ]] || die "KRIA_GOOGLE_REDIRECT_SCHEME must be a URL scheme without ://"
[[ "$KRIA_MARKETING_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "KRIA_MARKETING_VERSION must use major.minor.patch"
[[ "$KRIA_BUILD_NUMBER" =~ ^[1-9][0-9]*$ ]] || die "KRIA_BUILD_NUMBER must be a positive integer"

repo_root="$(git rev-parse --show-toplevel)"
ios_root="$repo_root/src/apps/ios"
archive_path="${KRIA_ARCHIVE_PATH:-$repo_root/build/Kria.xcarchive}"
export_path="${KRIA_EXPORT_PATH:-$repo_root/build/testflight-export}"
export_options="$(mktemp "${TMPDIR:-/tmp}/kria-testflight-export-options.XXXXXX.plist")"
trap 'rm -f "$export_options"' EXIT

command -v xcodegen >/dev/null 2>&1 || die "xcodegen is required"
command -v xcodebuild >/dev/null 2>&1 || die "xcodebuild is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

python3 - \
  "$repo_root/scripts/ios/testflight-export-options.plist" \
  "$export_options" \
  "$KRIA_DEVELOPMENT_TEAM" \
  "$KRIA_PROVISIONING_PROFILE_SPECIFIER" <<'PY'
import plistlib
import sys

template, destination, team_id, profile_specifier = sys.argv[1:]
with open(template, "rb") as source:
    options = plistlib.load(source)
options["teamID"] = team_id
options["provisioningProfiles"]["com.emirerben.kria"] = profile_specifier
with open(destination, "wb") as output:
    plistlib.dump(options, output)
PY

cd "$ios_root"
xcodegen generate --spec project.yml

xcodebuild \
  -project Kria.xcodeproj \
  -scheme Kria \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -skipPackagePluginValidation \
  -archivePath "$archive_path" \
  DEVELOPMENT_TEAM="$KRIA_DEVELOPMENT_TEAM" \
  KRIA_DEVELOPMENT_TEAM="$KRIA_DEVELOPMENT_TEAM" \
  KRIA_PROVISIONING_PROFILE_SPECIFIER="$KRIA_PROVISIONING_PROFILE_SPECIFIER" \
  KRIA_GOOGLE_CLIENT_ID="$KRIA_GOOGLE_CLIENT_ID" \
  KRIA_GOOGLE_REDIRECT_SCHEME="$KRIA_GOOGLE_REDIRECT_SCHEME" \
  MARKETING_VERSION="$KRIA_MARKETING_VERSION" \
  CURRENT_PROJECT_VERSION="$KRIA_BUILD_NUMBER" \
  archive

xcodebuild \
  -exportArchive \
  -archivePath "$archive_path" \
  -exportOptionsPlist "$export_options" \
  -exportPath "$export_path"

ipa_path="$(find "$export_path" -maxdepth 1 -type f -name '*.ipa' -print -quit)"
[[ -n "$ipa_path" ]] || die "xcodebuild did not export an IPA"
printf 'KRIA_IPA_PATH=%s\n' "$ipa_path"
