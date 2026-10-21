"""KRI-667 guard: no new regexes in the code that reads what a creator (or a model) meant.

Every regex that guesses meaning from words needs a twin per language (KRI-520 added ~170
Turkish ones in a week) and still misses paraphrases. The model that reads the message
records the meaning in a structured field instead (brief `facts`, the clip-intent `exclude`
op, the copilot's `claimed_ops`), and the server checks that field.

This is a ratchet: the count of `re.*` calls per module may only go down, and the baseline
must follow it down. A regex that reads no creator or model text (ids, file names, ffmpeg
output) is exempt with `# kri667: <why>` on the line of the call.
"""

from __future__ import annotations

import ast
from pathlib import Path

_API = Path(__file__).resolve().parents[2]
_APP = _API / "app"
_RE_FUNCS = frozenset(
    {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
)
_EXEMPT = "# kri667:"

# Modules outside app/kria that read creator or model wording. Every app/kria module is
# scanned too; a new one starts at zero.
_OTHER_MODULES = (
    "agents/_schemas/creator_policy.py",
    "agents/brief_extractor.py",
    "agents/clip_intent_planner.py",
    "agents/clip_request_resolver.py",
    "agents/edit_copilot.py",
    "agents/main_creator.py",
    "agents/spoken_script.py",
    "routes/_copilot.py",
    "routes/creator_agent.py",
    "schemas/edit_proposal.py",
    "services/choice_questions.py",
    "services/clip_intent_resolution.py",
    # KRI-796: reads where a creator seated a Visual among named moments, by folded words.
    "services/clip_order_sequence.py",
    "services/clip_selection.py",
    "services/creative_copy_decisions.py",
    "services/creator_direction.py",
    "services/creator_memory_learning.py",
    "services/kria_editor_ops_diff.py",
    "services/sfx_catalog.py",
    "services/spoken_quotes.py",
    # KRI-585: reads the clips' own speech for its spoken role, by folded tokens only.
    "services/spoken_roles.py",
    "services/visual_treatment_planner.py",
    "tasks/kria_runtime.py",
)

# Counted on the KRI-667 branch (475 calls, unchanged from main). Lower a number when you
# delete regexes; never raise one.
BASELINE: dict[str, int] = {
    "agents/_schemas/creator_policy.py": 8,
    "agents/clip_intent_planner.py": 19,
    "agents/clip_request_resolver.py": 3,
    "agents/edit_copilot.py": 26,
    "agents/main_creator.py": 4,
    "agents/spoken_script.py": 11,
    "kria/brief.py": 27,
    "kria/brief_checks.py": 70,
    "kria/brief_route.py": 4,
    "kria/clip_scope.py": 20,
    "kria/editor_receipts.py": 48,
    "kria/language.py": 1,
    "kria/opening_cut_ask.py": 8,
    "kria/planner.py": 12,
    "kria/reply_language.py": 8,
    "kria/speaker_framing_ask.py": 29,
    "kria/strategy_policy.py": 4,
    "kria/style_asks.py": 25,
    "kria/voiceover_clips.py": 12,
    "routes/_copilot.py": 8,
    "routes/creator_agent.py": 39,
    "schemas/edit_proposal.py": 63,
    "services/choice_questions.py": 2,
    "services/clip_intent_resolution.py": 2,
    "services/clip_selection.py": 1,
    "services/creator_direction.py": 6,
    "services/kria_editor_ops_diff.py": 3,
    "services/sfx_catalog.py": 2,
    "services/spoken_quotes.py": 2,
    "services/visual_treatment_planner.py": 8,
}


def _modules() -> list[Path]:
    kria = sorted((_APP / "kria").rglob("*.py"))
    return [*kria, *(_APP / rel for rel in _OTHER_MODULES)]


def _regex_calls(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    count = 0
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in _RE_FUNCS
        ):
            continue
        if _EXEMPT in lines[node.lineno - 1]:
            continue
        count += 1
    return count


def current_counts() -> dict[str, int]:
    return {str(path.relative_to(_APP)): _regex_calls(path) for path in _modules() if path.exists()}


def test_every_scanned_module_exists():
    missing = [rel for rel in _OTHER_MODULES if not (_APP / rel).exists()]
    assert not missing, f"update _OTHER_MODULES: {missing}"


def test_no_module_gains_a_regex_that_reads_meaning_from_words():
    grew = {
        rel: (count, BASELINE.get(rel, 0))
        for rel, count in current_counts().items()
        if count > BASELINE.get(rel, 0)
    }
    assert not grew, (
        "KRI-667: these modules gained regexes (now, baseline): "
        f"{grew}. Don't guess what the creator meant from words; have the model that reads "
        "the message record it in a structured field (brief `facts`, clip-intent op, "
        "`claimed_ops`) and check that field. A regex that reads no creator or model text "
        "can carry `# kri667: <why>` on its line."
    )


def test_the_baseline_follows_removed_regexes_down():
    counts = current_counts()
    stale = {
        rel: (counts.get(rel, 0), base)
        for rel, base in BASELINE.items()
        if counts.get(rel, 0) < base
    }
    assert not stale, f"regexes were removed; lower BASELINE to the new counts: {stale}"
