#!/usr/bin/env bash
# Runs copilot_model_scope.py in this tree and in a base ref's API tree, with the production
# defaults and with the Sonnet tools flip, and diffs the two trees per environment.
#   bash docs/reviews/kri-1384/scripts/compare_copilot_model_scope.sh [base-ref] [out-dir]
# Defaults must print no diff. The flip diff is the change: only the JSON-mode callers move.
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
base_ref="${1:-origin/main}"
out="${2:-$(mktemp -d)}"
probe="$repo/docs/reviews/kri-1384/scripts/copilot_model_scope.py"
python="$repo/src/apps/api/.venv/bin/python"

mkdir -p "$out/base"
git -C "$repo" archive "$base_ref" src/apps/api src/packages | tar -x -C "$out/base"
echo "base $(git -C "$repo" rev-parse "$base_ref") vs head $(git -C "$repo" rev-parse HEAD) (+ working tree)"

run() {
  local tree="$1" name="$2"
  shift 2
  (cd "$tree" && env "$@" "$python" "$probe" "$out/$name.json" > "$out/$name.log" 2>&1)
}

flip=(EDIT_COPILOT_MODEL=claude-sonnet-5-5 EDIT_COPILOT_THINKING_LEVEL=medium EDIT_COPILOT_OUTPUT_MODE=tools)
run "$out/base/src/apps/api" base-defaults EDIT_COPILOT_OUTPUT_MODE=json
run "$repo/src/apps/api" head-defaults EDIT_COPILOT_OUTPUT_MODE=json
run "$out/base/src/apps/api" base-flip "${flip[@]}"
run "$repo/src/apps/api" head-flip "${flip[@]}"

echo "== defaults: base vs head (empty = byte-identical)"
diff "$out/base-defaults.json" "$out/head-defaults.json" && echo "identical"
echo "== Sonnet tools flip: base vs head"
diff "$out/base-flip.json" "$out/head-flip.json" || true
echo "outputs in $out"
