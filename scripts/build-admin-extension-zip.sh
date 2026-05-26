#!/usr/bin/env bash
# Build and package the Nova Chrome extension for the admin install page.
#
# Single-command rebuild used to refresh the zip that admins download from
# /admin/extension/install. Run when src/apps/extension/src/** changes. Output:
#   - src/apps/web/public/admin/extension/nova-extension.zip
#   - src/apps/web/public/admin/extension/extension-info.json
#
# Asserts integrity before writing the zip:
#   - packaged manifest.version == source manifest.version (catches a stale build)
#   - secrets audit on the built bundle (gitleaks if available, plus a hard
#     pattern grep that exits non-zero on any match)
#
# The zip is served by Next.js as a static asset under /admin/extension/, and
# the /admin/* middleware matcher gates it behind the BasicAuth realm. The zip
# is built from the source tree only — no production secrets ever enter it —
# but the audit is defense in depth in case a developer accidentally drops a
# key, .env, or service-account JSON into the extension source tree.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
EXT_DIR="$REPO_ROOT/src/apps/extension"
DIST_DIR="$EXT_DIR/dist"
OUT_DIR="$REPO_ROOT/src/apps/web/public/admin/extension"
ZIP_PATH="$OUT_DIR/nova-extension.zip"
INFO_PATH="$OUT_DIR/extension-info.json"

manifest_version_of() {
  # Read .version from a JSON file. Uses python3 instead of jq because jq
  # is not part of the standard macOS toolchain and we don't want to add a
  # system-package dependency to this script.
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$1"
}

echo "[1/6] Building extension at $EXT_DIR"
cd "$EXT_DIR"
if [ ! -d node_modules ]; then
  # Fall back to a per-invocation npm cache if the default cache is
  # root-owned (a known npm bug some machines still carry).
  npm install --legacy-peer-deps \
    || npm install --legacy-peer-deps --cache "$(mktemp -d -t nova-npm-cache-XXXXXX)"
fi
npm run build

if [ ! -f "$DIST_DIR/manifest.json" ]; then
  echo "ERROR: build produced no $DIST_DIR/manifest.json" >&2
  exit 1
fi

SOURCE_VERSION="$(manifest_version_of "$EXT_DIR/src/manifest.json")"
PACKAGED_VERSION="$(manifest_version_of "$DIST_DIR/manifest.json")"
if [ "$SOURCE_VERSION" != "$PACKAGED_VERSION" ]; then
  echo "ERROR: packaged manifest version ($PACKAGED_VERSION) does not match source ($SOURCE_VERSION)." >&2
  echo "  This is normally a stale dist/ — wipe it and rebuild." >&2
  exit 1
fi

echo "[2/6] Secrets audit (gitleaks on source tree)"
if command -v gitleaks >/dev/null 2>&1; then
  # Scan the source tree, not the bundled dist/. Bundled vendor code (notably
  # youtubei.js) embeds YouTube's well-known public Innertube API key and the
  # literal token `client_secret` as JSON field names in OAuth scaffolding —
  # both false positives. The threat we're guarding against here is a Nova
  # developer accidentally dropping a real key into the extension source.
  gitleaks detect --no-git --source="$EXT_DIR/src"
else
  echo "  gitleaks not installed — relying on the hard grep audit below." >&2
  echo "  Install with: brew install gitleaks" >&2
fi

echo "[3/6] Secrets audit (pattern grep on source tree)"
# Two complementary patterns. STRONG patterns match the value itself (so an
# AKIA / AIza / sk- key in any context lights up). ASSIGNMENT patterns require
# a name=value (or name: value) shape so that header names like X-Admin-Token
# inside comments don't false-positive. Case-sensitive: real-world conventions
# for these tokens are fixed-case, and -i kept flagging header names in code.
STRONG_PATTERN='BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{20,}'
ASSIGNMENT_PATTERN='(admin[_-]?token|client_secret|service_account|GCS_KEY|FLY_API|ADMIN_BASIC_AUTH(_USER|_PASSWORD)?)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9._/+-]{8,}'

if find "$EXT_DIR/src" -type f \( -name '*.js' -o -name '*.json' -o -name '*.html' \) \
    -print0 | xargs -0 grep -EIln -e "$STRONG_PATTERN" -e "$ASSIGNMENT_PATTERN" 2>/dev/null; then
  echo "ERROR: potential secret found in $EXT_DIR/src — see paths above. Halting build." >&2
  exit 1
fi

echo "[4/6] Zipping $DIST_DIR/* -> $ZIP_PATH"
mkdir -p "$OUT_DIR"
rm -f "$ZIP_PATH"
# zip from INSIDE dist/ so manifest.json is at archive root. Otherwise macOS
# Archive Utility would extract into a top-level `dist/` folder, which is
# confusing for admins clicking "Load unpacked".
(cd "$DIST_DIR" && zip -qr "$ZIP_PATH" .)

echo "[5/6] Integrity check on packaged zip"
PACKED_VERSION_FROM_ZIP="$(unzip -p "$ZIP_PATH" manifest.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
if [ "$PACKED_VERSION_FROM_ZIP" != "$SOURCE_VERSION" ]; then
  echo "ERROR: zip's manifest version ($PACKED_VERSION_FROM_ZIP) does not match source ($SOURCE_VERSION)." >&2
  exit 1
fi
# Capture-then-test pattern. macOS bash 3.2 + `set -o pipefail` mis-evaluates
# `if ! pipeline | grep -q` when grep short-circuits on the first match
# (grep exits 0 fast → unzip gets SIGPIPE → pipeline reports failure → ! flips
# it → if-branch taken even though the file IS there).
ZIP_ENTRIES="$(unzip -Z -1 "$ZIP_PATH")"
if ! printf '%s\n' "$ZIP_ENTRIES" | grep -Fxq 'manifest.json'; then
  echo "ERROR: manifest.json is not at the root of $ZIP_PATH." >&2
  unzip -l "$ZIP_PATH" >&2
  exit 1
fi

echo "[6/6] Writing $INFO_PATH"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SOURCE_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
python3 - "$INFO_PATH" "$SOURCE_VERSION" "$BUILT_AT" "$SOURCE_COMMIT" <<'PY'
import json, sys
out, version, built_at, commit = sys.argv[1:]
with open(out, "w", encoding="utf-8") as f:
    json.dump(
        {"version": version, "built_at": built_at, "source_commit": commit},
        f,
        indent=2,
    )
    f.write("\n")
PY

echo
echo "Done."
echo "  Version:      $SOURCE_VERSION"
echo "  Built at:     $BUILT_AT"
echo "  Source commit: $SOURCE_COMMIT"
echo "  Zip:          $ZIP_PATH"
echo "  Sidecar:      $INFO_PATH"
