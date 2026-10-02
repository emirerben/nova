#!/usr/bin/env bash
set -euo pipefail

# Install apt packages on a GitHub runner without letting a stalled mirror hang
# the job. apt's own Acquire timeouts do not catch every stall: CI run
# 36888283982 sat in `apt-get update` for 6 h after the azure mirror failed
# over to archive.ubuntu.com. Each network phase therefore runs under a hard
# `timeout` and is retried. Downloading is split from installing so a kill
# never interrupts dpkg, and finished .debs stay cached for the next attempt.
#
# Usage: sudo bash scripts/ci/apt-install.sh <package>...

if (($# == 0)); then
  echo "usage: $0 <package>..." >&2
  exit 2
fi

attempts="${APT_ATTEMPTS:-4}"
update_timeout="${APT_UPDATE_TIMEOUT:-180}"
download_timeout="${APT_DOWNLOAD_TIMEOUT:-600}"
retry_delay="${APT_RETRY_DELAY:-10}"

# Same values the x64 runner image ships (actions/runner-images#14594): one
# retry and a 15s timeout fail a stalled mirror over to the next one in
# seconds. Pinned here so an image change cannot loosen them.
acquire=(-o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)

retry() {
  local label=$1 limit=$2
  shift 2
  local n status
  for ((n = 1; n <= attempts; n++)); do
    status=0
    timeout --kill-after=10 "$limit" "$@" || status=$?
    if ((status == 0)); then
      return 0
    fi
    if ((status == 124 || status == 137)); then
      echo "::warning::$label attempt $n/$attempts timed out after ${limit}s"
    else
      echo "::warning::$label attempt $n/$attempts failed (exit $status)"
    fi
    if ((n < attempts)); then
      sleep $((n * retry_delay))
    fi
  done
  echo "::error::$label failed after $attempts attempts"
  return 1
}

retry "apt-get update" "$update_timeout" apt-get "${acquire[@]}" update
retry "apt-get download" "$download_timeout" \
  apt-get "${acquire[@]}" install -y --download-only "$@"
apt-get install -y --no-download "$@"
