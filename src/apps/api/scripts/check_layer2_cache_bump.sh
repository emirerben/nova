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

# As of the content-hash migration (PR "T1 content-hash cache invalidation"),
# TEXT_OVERLAY_VERSION_V2 is NO LONGER a hand-edited string literal. It is
# derived by `compute_text_overlay_version()` from the SHA-256 of every Layer-2
# prompt file + schema module + the Layer-2 agents' AgentSpec.prompt_version
# strings. So a change to any prompt or schema file in LAYER2_PATHS now
# invalidates the cache AUTOMATICALLY — no manual bump exists to check for.
#
# What is still NOT auto-covered: edits to the Layer-2 agent `.py` source
# (text_alignment.py / text_classification.py) that change behavior WITHOUT
# bumping the agent's `AgentSpec.prompt_version`. That is the one residual gap
# the content hash can't see, so the guard still enforces a prompt_version bump
# for those two files.

# Detect the migration: literal constant gone AND the deriving function present.
if grep -qE '^TEXT_OVERLAY_VERSION_V2 = ' "$CACHE_FILE"; then
  # Legacy literal still present (migration reverted?) — keep the old check.
  base_line="$(git show "$BASE:$CACHE_FILE" 2>/dev/null | grep -E '^TEXT_OVERLAY_VERSION_V2 = ' | head -1 || true)"
  head_line="$(grep -E '^TEXT_OVERLAY_VERSION_V2 = ' "$CACHE_FILE" | head -1 || true)"
  if [ "$base_line" = "$head_line" ]; then
    cat >&2 <<EOF
Layer-2 cache guard: FAIL

These Layer-2 files changed:
$(printf '  %s\n' $layer2_hits)

but the TEXT_OVERLAY_VERSION_V2 literal in $CACHE_FILE was not bumped:
  $head_line

Fix: bump the value, or migrate to the content-hash mechanism
(compute_text_overlay_version). If this edit genuinely cannot change Layer-2
output, add [skip-layer2-cache-bump] to a commit message.
EOF
    exit 1
  fi
  echo "Layer-2 cache guard: TEXT_OVERLAY_VERSION_V2 literal bumped alongside Layer-2 changes — OK."
  echo "  $base_line  ->  $head_line"
  exit 0
fi

if ! grep -qE 'def compute_text_overlay_version' "$CACHE_FILE"; then
  echo "Layer-2 cache guard: neither the TEXT_OVERLAY_VERSION_V2 literal nor compute_text_overlay_version() found in $CACHE_FILE — refusing to pass." >&2
  exit 1
fi

# Content-hash mode. Prompt + schema changes are auto-absorbed by the hash.
# The only residual gap is agent .py behavior changes with no prompt_version
# bump. If the only Layer-2 files touched are those two agent modules, require
# a prompt_version bump in the same diff.
agent_only=1
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    src/apps/api/app/agents/text_alignment.py|src/apps/api/app/agents/text_classification.py) ;;
    *) agent_only=0 ;;
  esac
done <<EOF2
$layer2_hits
EOF2

if [ "$agent_only" -eq 1 ]; then
  # Did any prompt_version change in the agent files?
  pv_changed=0
  for f in src/apps/api/app/agents/text_alignment.py src/apps/api/app/agents/text_classification.py; do
    printf '%s\n' "$layer2_hits" | grep -qxF "$f" || continue
    base_pv="$(git show "$BASE:$f" 2>/dev/null | grep -E 'prompt_version' || true)"
    head_pv="$(grep -E 'prompt_version' "$f" 2>/dev/null || true)"
    if [ "$base_pv" != "$head_pv" ]; then
      pv_changed=1
    fi
  done
  if [ "$pv_changed" -eq 0 ]; then
    cat >&2 <<EOF3
Layer-2 cache guard: FAIL

Only Layer-2 agent source changed:
$(printf '  %s\n' $layer2_hits)

The cache version is now content-hashed from prompts + schemas +
AgentSpec.prompt_version, so prompt/schema edits invalidate automatically.
But agent .py behavior changes are NOT hashed — bump the agent's
AgentSpec.prompt_version so the content hash (and therefore the cache key)
changes. If this edit cannot change Layer-2 output, add
[skip-layer2-cache-bump] to a commit message.
EOF3
    exit 1
  fi
  echo "Layer-2 cache guard: agent prompt_version bumped alongside agent-source change — OK (content-hash absorbs it)."
  exit 0
fi

echo "Layer-2 cache guard: Layer-2 prompt/schema files changed — content hash (compute_text_overlay_version) invalidates the cache automatically. OK."
echo "Changed Layer-2 files:"
printf '  %s\n' $layer2_hits
exit 0
