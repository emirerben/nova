#!/usr/bin/env bash
set -euo pipefail

# Playwright downloads Chromium directly; API tests do not use Chrome.
# Keep an unrelated Google index outage from blocking Ubuntu dependency setup.
# Moving the source preserves it for inspection and keeps apt hash checks intact.
sources_dir="${1:-/etc/apt/sources.list.d}"
for source in "$sources_dir"/*.list "$sources_dir"/*.sources; do
  [[ -f "$source" ]] || continue
  if grep -Eq 'https?://dl\.google\.com/linux/chrome(-stable)?/deb([/[:space:]]|$)' "$source"; then
    mv -- "$source" "$source.disabled"
    printf 'Disabled unused Chrome apt source: %s\n' "${source##*/}"
  fi
done
