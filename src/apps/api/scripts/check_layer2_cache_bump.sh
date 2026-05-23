#!/usr/bin/env bash
# check_layer2_cache_bump.sh
#
# CI guard: if a PR changes the SEMANTICS of the Layer-2 text-overlay pipeline
# (any stage under text_overlay_v2/, the Stage E/F agents or their schemas, or
# the Stage E/F prompts) it MUST also bump
# `app/pipeline/template_cache.py:TEXT_OVERLAY_VERSION_V2` in the same PR.
#
# Why: TEXT_OVERLAY_VERSION_V2 is the ONLY thing in the Layer-2 cache key that
# invalidates cached recipes. The Stage E/F agent `prompt_version`s are NOT in
# the key. So a deployed Layer-2 change with no version bump is invisible in
# prod — every access cache-hits a pre-change recipe. This is the "looks right
# locally, stale in prod" class. Same conscious-decision gate as the
# encoder-policy allow-list (tests/test_encoder_policy.py).
#
# Escape hatch for genuinely non-semantic edits (comments, log strings,
# refactors that can't change output): put [skip-layer2-cache-bump] in any
# commit message on the PR.
#
# Local use:  BASE_SHA=origin/main bash src/apps/api/scripts/check_layer2_cache_bump.sh

set -euo pipefail

# Resolve the comparison range. CI passes BASE_SHA (the PR base); locally we
# fall back to origin/main. HEAD is the checked-out tip (a merge ref in CI).
BASE="${BASE_SHA:-origin/main}"
HEAD_REF="${HEAD_SHA:-HEAD}"

CACHE_FILE="src/apps/api/app/pipeline/template_cache.py"

# Files whose change implies Layer-2 output semantics may have shifted.
LAYER2_PATHS=(
  "src/apps/api/app/pipeline/text_overlay_v2/"
  "src/apps/api/app/agents/text_alignment.py"
  "src/apps/api/app/agents/text_classification.py"
  "src/apps/api/app/agents/_schemas/text_alignment.py"
  "src/apps/api/app/agents/_schemas/text_classification.py"
  "src/apps/api/prompts/align_overlay_to_transcript.txt"
  "src/apps/api/prompts/classify_overlay.txt"
)

changed_files="$(git diff --name-only "$BASE" "$HEAD_REF" 2>/dev/null || true)"
if [ -z "$changed_files" ]; then
  echo "Layer-2 cache guard: no diff against $BASE — nothing to check."
  exit 0
fi

layer2_hits="$(printf '%s\n' "$changed_files" | grep -F -f <(printf '%s\n' "${LAYER2_PATHS[@]}") || true)"
if [ -z "$layer2_hits" ]; then
  echo "Layer-2 cache guard: no Layer-2 stage/agent/prompt files changed — OK."
  exit 0
fi

# Escape hatch: a commit on the PR opts out for non-semantic edits.
commit_msgs="$(git log --format='%B' "$BASE..$HEAD_REF" 2>/dev/null || true)"
if printf '%s' "$commit_msgs" | grep -qF '[skip-layer2-cache-bump]'; then
  echo "Layer-2 cache guard: [skip-layer2-cache-bump] found in a commit message — bypassing."
  echo "Changed Layer-2 files:"
  printf '  %s\n' $layer2_hits
  exit 0
fi

# Compare the constant's assignment line between base and head. A real bump
# changes the string value, not just the line position.
base_line="$(git show "$BASE:$CACHE_FILE" 2>/dev/null | grep -E '^TEXT_OVERLAY_VERSION_V2 = ' | head -1 || true)"
head_line="$(grep -E '^TEXT_OVERLAY_VERSION_V2 = ' "$CACHE_FILE" | head -1 || true)"

if [ -z "$head_line" ]; then
  echo "Layer-2 cache guard: could not find TEXT_OVERLAY_VERSION_V2 in $CACHE_FILE — refusing to pass." >&2
  exit 1
fi

if [ "$base_line" = "$head_line" ]; then
  cat >&2 <<EOF
Layer-2 cache guard: FAIL

These Layer-2 files changed:
$(printf '  %s\n' $layer2_hits)

but TEXT_OVERLAY_VERSION_V2 in $CACHE_FILE was not bumped:
  $head_line

That constant is the only thing that invalidates Layer-2 cached recipes — the
Stage E/F agent prompt_versions are NOT in the cache key. Shipping this change
without a bump means prod keeps serving pre-change recipes (cache-hit), so the
deploy is invisible. See CLAUDE.md "Layer-2 cache namespace".

Fix: bump the value (e.g. append a dated suffix like "v2-2026-05-23d-...").
If this edit genuinely cannot change Layer-2 output (comment / log / pure
refactor), add [skip-layer2-cache-bump] to a commit message.
EOF
  exit 1
fi

echo "Layer-2 cache guard: TEXT_OVERLAY_VERSION_V2 bumped alongside Layer-2 changes — OK."
echo "  $base_line  ->  $head_line"
exit 0
