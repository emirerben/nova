"""Semantic caption chunker: contextual cue sizing (plan 011, Feature A).

`build_semantic_caption_cues` groups the word-timed transcript into presentation
cues. These tests pin the emphasis-span behavior the founder asked for — "number
one → Messi shows Messi alone" — while guaranteeing that with no spans the output
is byte-identical to the pre-feature chunker (the SMART_CAPTION_EMPHASIS_CUES
kill switch), and that semantics always win over presentation.
"""

from __future__ import annotations

import pytest

from app.smart_edit.captions import build_semantic_caption_cues
from app.smart_edit.presets import CaptionPolicy
from app.smart_edit.schemas import SmartWord


def _word(index: int, text: str, start_ms: int, end_ms: int) -> SmartWord:
    word_id = f"w{index:06d}"
    return SmartWord(
        word_id=word_id,
        spoken_text=text,
        display_text=text,
        normalized_text=text.lower(),
        start_ms=start_ms,
        end_ms=end_ms,
        timing_quality="aligned",
        display_alignment=[word_id],
    )


def _words(texts: list[str], *, step_ms: int = 1000, dur_ms: int = 500) -> list[SmartWord]:
    return [_word(i, t, i * step_ms, i * step_ms + dur_ms) for i, t in enumerate(texts)]


@pytest.fixture
def policy() -> CaptionPolicy:
    return CaptionPolicy(
        min_words=3,
        max_words=7,
        max_lines=2,
        max_chars=44,
        font_family="Montserrat Bold",
        font_size_px=64,
        y_frac=0.705,
        width_frac=0.88,
        color="&H00FFFFFF",
        stroke_color="&H00000000",
        stroke_width=6,
    )


def _roles(words: list[SmartWord]) -> dict[str, str]:
    return {word.word_id: "example" for word in words}


# ── kill switch: no spans is byte-identical to the pre-feature chunker ─────────


def test_no_spans_matches_baseline_and_adds_no_keys(policy: CaptionPolicy) -> None:
    words = _words(["Bir", "numara", "Messi", "iki", "numara", "Ronaldo", "harika"])
    cues = build_semantic_caption_cues(words, policy, role_by_word_id=_roles(words))
    assert [c["text"] for c in cues] == ["Bir numara Messi", "iki numara Ronaldo", "harika"]
    # No emphasis metadata is written when no spans are supplied — this is the
    # kill switch: a flag-off render is byte-identical.
    for cue in cues:
        assert "smart_emphasis" not in cue
        assert "smart_keep_together" not in cue


def test_empty_words_returns_empty(policy: CaptionPolicy) -> None:
    assert build_semantic_caption_cues([], policy, role_by_word_id={}) == []


# ── standalone: the flagship "Messi alone" behavior ───────────────────────────


@pytest.mark.parametrize(
    ("texts", "standalone_idx"),
    [
        (["Bir", "numara", "Messi", "iki", "numara", "Ronaldo"], 2),  # TR
        (["number", "one", "Messi", "number", "two", "Ronaldo"], 2),  # EN
    ],
)
def test_standalone_emphasis_word_renders_as_own_cue(
    policy: CaptionPolicy, texts: list[str], standalone_idx: int
) -> None:
    words = _words(texts)
    cues = build_semantic_caption_cues(
        words,
        policy,
        role_by_word_id=_roles(words),
        standalone_spans=[[words[standalone_idx].word_id]],
    )
    target = texts[standalone_idx]
    solo = [c for c in cues if c["text"] == target]
    assert len(solo) == 1, f"{target} must render as its own cue: {[c['text'] for c in cues]}"
    assert solo[0]["smart_emphasis"] is True


def test_standalone_min_hold_extends_short_cue_clamped_to_next(policy: CaptionPolicy) -> None:
    # "Messi" spoken 130-250ms (120ms) then a gap before the next cue at 900ms.
    words = [
        _word(0, "go", 0, 120),
        _word(1, "Messi", 130, 250),
        _word(2, "now", 900, 1200),
    ]
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=_roles(words), standalone_spans=[["w000001"]]
    )
    solo = next(c for c in cues if c["text"] == "Messi")
    # Extended toward the 0.5s floor but never past the next cue's start (0.9s).
    assert solo["end_s"] == pytest.approx(0.63, abs=1e-6)
    assert solo["end_s"] <= 0.9


def test_standalone_no_extension_in_continuous_speech(policy: CaptionPolicy) -> None:
    # Back-to-back words with no gap: the floor never overruns the next word.
    words = [_word(0, "go", 0, 200), _word(1, "Messi", 200, 380), _word(2, "now", 380, 700)]
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=_roles(words), standalone_spans=[["w000001"]]
    )
    solo = next(c for c in cues if c["text"] == "Messi")
    assert solo["end_s"] <= 0.38  # clamped to the next word's start, no invented time


# ── keep_together: never split across cues, cap closes early ──────────────────


def test_keep_together_span_stays_in_one_cue(policy: CaptionPolicy) -> None:
    words = _words(["Bir", "numara", "Messi", "harika", "adam"])
    cues = build_semantic_caption_cues(
        words,
        policy,
        role_by_word_id=_roles(words),
        keep_together_spans=[["w000000", "w000001"]],
    )
    # "Bir numara" appears together in exactly one cue and is never split.
    holders = [c for c in cues if "Bir" in c["text"].split()]
    assert len(holders) == 1
    assert holders[0]["text"].split()[:2] == ["Bir", "numara"]
    assert holders[0]["smart_keep_together"] == [[0, 1]]


def test_keep_together_cap_closes_before_span_not_mid_span(policy: CaptionPolicy) -> None:
    # max_words=3 so the running cue is near the cap when the span arrives; the
    # span must not be split — the chunker closes BEFORE it.
    tight = CaptionPolicy(**{**policy.model_dump(), "min_words": 1, "max_words": 3})
    words = _words(["a", "b", "c", "one", "two"])
    cues = build_semantic_caption_cues(
        words,
        tight,
        role_by_word_id=_roles(words),
        keep_together_spans=[["w000003", "w000004"]],  # "one two"
    )
    holders = [c for c in cues if "one" in c["text"].split()]
    assert len(holders) == 1
    assert "two" in holders[0]["text"].split(), "span split across cues by the cap"


def test_role_change_beats_keep_together(policy: CaptionPolicy) -> None:
    words = _words(["Bir", "numara", "Messi", "harika"])
    roles = _roles(words)
    roles["w000001"] = "payoff"  # role changes at 'numara'
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles, keep_together_spans=[["w000000", "w000001"]]
    )
    together = [c for c in cues if {"Bir", "numara"} <= set(c["text"].split())]
    assert not together, "semantic role change must win over keep_together"


def test_boundary_beats_keep_together(policy: CaptionPolicy) -> None:
    words = _words(["Bir", "numara", "Messi", "harika"])
    cues = build_semantic_caption_cues(
        words,
        policy,
        role_by_word_id=_roles(words),
        boundary_after_word_ids={"w000000"},  # authored-title boundary after 'Bir'
        keep_together_spans=[["w000000", "w000001"]],
    )
    together = [c for c in cues if {"Bir", "numara"} <= set(c["text"].split())]
    assert not together, "authored-title boundary must win over keep_together"


def test_stale_span_word_id_is_ignored(policy: CaptionPolicy) -> None:
    words = _words(["Bir", "numara", "Messi"])
    # A word_id that does not exist must not raise and must not alter chunking.
    cues = build_semantic_caption_cues(
        words,
        policy,
        role_by_word_id=_roles(words),
        standalone_spans=[["w999999"]],
    )
    assert [c["text"] for c in cues] == ["Bir numara Messi"]
    assert all("smart_emphasis" not in c for c in cues)


# ── multi-word standalone + line-pair emission ────────────────────────────────


def test_multiword_standalone_is_one_cue_with_emphasis_and_line_pair(policy: CaptionPolicy) -> None:
    words = _words(["hey", "Lionel", "Messi", "scored", "again"])
    cues = build_semantic_caption_cues(
        words,
        policy,
        role_by_word_id=_roles(words),
        standalone_spans=[["w000001", "w000002"]],  # "Lionel Messi" together, alone
    )
    solo = [c for c in cues if c["text"] == "Lionel Messi"]
    assert len(solo) == 1
    assert solo[0]["smart_emphasis"] is True
    # A 2-word standalone also emits a cue-relative line pair so it never wraps apart.
    assert solo[0]["smart_keep_together"] == [[0, 1]]


# ── keep_together cap: the char half (not just max_words) ──────────────────────


def test_keep_together_closes_early_on_char_cap(policy: CaptionPolicy) -> None:
    # min_words=1 and a tight char cap; the running cue is under max_words but the
    # span would breach max_chars, so the chunker must still close BEFORE it.
    tight = CaptionPolicy(
        **{**policy.model_dump(), "min_words": 1, "max_words": 7, "max_chars": 20}
    )
    words = _words(["aaaa", "bbbb", "cccc", "Ronaldo", "Messi"])
    cues = build_semantic_caption_cues(
        words,
        tight,
        role_by_word_id=_roles(words),
        keep_together_spans=[["w000003", "w000004"]],  # "Ronaldo Messi" (14 chars)
    )
    holders = [c for c in cues if "Ronaldo" in c["text"].split()]
    assert len(holders) == 1
    assert "Messi" in holders[0]["text"].split(), "char cap must not split the span"


# ── min-hold: last-cue branch + word timings stay truthful ────────────────────


def test_standalone_min_hold_last_cue_extends_fully_and_keeps_word_truthful(
    policy: CaptionPolicy,
) -> None:
    # Standalone as the FINAL cue: no next-cue clamp, extend fully to start + 0.5.
    words = [_word(0, "so", 0, 300), _word(1, "Messi", 400, 520)]
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=_roles(words), standalone_spans=[["w000001"]]
    )
    solo = next(c for c in cues if c["text"] == "Messi")
    assert solo["end_s"] == pytest.approx(0.9, abs=1e-6)  # 0.4 start + 0.5 floor
    # The word's spoken end stays truthful — only the caption display was extended.
    assert solo["words"][-1]["end_s"] == pytest.approx(0.52, abs=1e-6)


# ── P0-1 deterministic emphasis floor (plan 012) ──────────────────────────────


def test_floor_emphasizes_isolated_entity_name(policy: CaptionPolicy) -> None:
    # "Messi" isolated by a role change (NOT a standalone span) but confirmed as a
    # scene-matcher entity anchor -> the floor gives it emphasis anyway.
    words = _words(["the", "best", "player", "Messi", "he", "is"])
    roles = _roles(words)
    roles["w000003"] = "payoff"  # isolate "Messi"
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles, entity_anchor_word_ids={"w000003"}
    )
    solo = next(c for c in cues if c["text"] == "Messi")
    assert solo["smart_emphasis"] is True


def test_floor_requires_anchor_and_skips_function_words(policy: CaptionPolicy) -> None:
    words = _words(["the", "best", "player", "Messi", "he", "is"])
    roles = _roles(words)
    roles["w000003"] = "payoff"
    # No anchor supplied -> floor never fires.
    for cue in build_semantic_caption_cues(words, policy, role_by_word_id=roles):
        assert "smart_emphasis" not in cue
    # A mis-anchored lowercase function word is NOT a name -> not promoted.
    roles2 = _roles(words)
    roles2["w000004"] = "payoff"  # isolate "he"
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles2, entity_anchor_word_ids={"w000004"}
    )
    # "he" may fold into a neighbor (lone-marker merge); whichever cue holds it
    # must not be emphasized.
    he_cue = next(c for c in cues if "he" in c["text"].split())
    assert not he_cue.get("smart_emphasis")


def test_floor_emphasizes_multiword_name(policy: CaptionPolicy) -> None:
    words = _words(["so", "Lionel", "Messi", "scored"])
    roles = _roles(words)
    roles["w000001"] = roles["w000002"] = "payoff"  # isolate "Lionel Messi"
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles, entity_anchor_word_ids={"w000002"}
    )
    solo = next(c for c in cues if c["text"] == "Lionel Messi")
    assert solo["smart_emphasis"] is True


# ── P0-2 marker merge-back (plan 012) ─────────────────────────────────────────


def test_merge_back_folds_lone_marker_before_standalone(policy: CaptionPolicy) -> None:
    # The whisper-split "number one" -> lone "number" case: "number" is stranded
    # right before the isolated name and must fold into its previous neighbor.
    words = _words(["intro", "words", "here", "number", "Messi", "he"])
    roles = _roles(words)
    roles["w000003"] = "list_item"  # chapter/list-marker isolates "number"
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles, standalone_spans=[["w000004"]]
    )
    assert not [c for c in cues if c["text"] == "number"], "lone 'number' cue must be gone"
    assert any("number" in c["text"].split() for c in cues), "number preserved in a neighbor"
    assert any(c["text"] == "Messi" for c in cues), "Messi still isolated"


def test_merge_back_respects_boundary(policy: CaptionPolicy) -> None:
    # An authored-title boundary between the marker and its neighbor must NOT be
    # crossed by the merge-back.
    words = _words(["intro", "words", "here", "number", "Messi"])
    roles = _roles(words)
    roles["w000003"] = "list_item"
    cues = build_semantic_caption_cues(
        words,
        policy,
        role_by_word_id=roles,
        boundary_after_word_ids={"w000002"},  # boundary after "here"
        standalone_spans=[["w000004"]],
    )
    # With the boundary, "number" cannot merge back across it; it stays its own
    # cue (correct — we never cross an authored boundary).
    assert any(c["text"] == "number" for c in cues)


def test_merge_back_kills_lone_marker_before_floor_name(policy: CaptionPolicy) -> None:
    # The real render case: a lone "number" precedes a name that is emphasized by
    # the entity FLOOR (not an LLM standalone span). The lone marker must still
    # fold away, and the name stays emphasized.
    words = _words(["talk", "about", "the", "player", "number", "Messi", "he", "is"])
    roles = _roles(words)
    roles["w000004"] = "list_item"  # isolate "number"
    roles["w000005"] = "payoff"  # isolate "Messi"
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles, entity_anchor_word_ids={"w000005"}
    )
    assert not [c for c in cues if c["text"].strip().casefold() == "number"]
    assert next(c for c in cues if c["text"] == "Messi").get("smart_emphasis") is True


def test_merge_back_never_glues_marker_into_floor_name(policy: CaptionPolicy) -> None:
    # Red-team case: a lone marker directly between two entity-FLOOR names (the LLM
    # missed BOTH standalone spans, so only the floor promotes them). The marker
    # must NOT fold into a floor name — doing so would strip that name's emphasis
    # and glue a stray "number" onto it, defeating the floor. Floor names get the
    # same merge-back protection as real standalone spans; both keep their own
    # emphasized cue (the wedged marker itself stays put, same as the standalone
    # wedge — the priority is never losing a name's emphasis).
    words = _words(["Messi", "number", "Ronaldo"])
    roles = _roles(words)
    roles["w000000"] = "payoff"
    roles["w000001"] = "list_item"  # isolate "number"
    roles["w000002"] = "chapter"  # distinct role isolates "Ronaldo"
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles, entity_anchor_word_ids={"w000000", "w000002"}
    )
    messi = next(c for c in cues if c["text"] == "Messi")
    ronaldo = next(c for c in cues if c["text"] == "Ronaldo")
    assert messi.get("smart_emphasis") is True, "floor name must not be merged away"
    assert ronaldo.get("smart_emphasis") is True
    # the marker never glued onto a name
    assert not any(
        "number" in c["text"].split() and any(n in c["text"].split() for n in ("Messi", "Ronaldo"))
        for c in cues
    )


def test_lone_marker_leads_forward_when_backward_blocked(policy: CaptionPolicy) -> None:
    # "and" after an isolated name (backward blocked) leads into the next normal
    # cue instead of standing alone; the name keeps its emphasis.
    #
    # A role change on "and" forces it to chunk as its OWN single word (the
    # `is_lone_marker` case), so backward-merge into the "France" standalone is
    # genuinely blocked and the ONLY way to avoid a lone "and" cue is the
    # forward-lead (`pending_prefix`) branch. Without the role isolation "and"
    # would group naturally into "and I'll..." and the branch would never run.
    words = _words(["ok", "France", "and", "I'll", "talk", "about", "it"])
    roles = _roles(words)
    roles["w000002"] = "list_item"  # isolate "and" into its own chunk
    cues = build_semantic_caption_cues(
        words, policy, role_by_word_id=roles, standalone_spans=[["w000001"]]
    )
    assert not [c for c in cues if c["text"].strip().casefold() == "and"], (
        "backward-merge is blocked by the France standalone, so a lone 'and' cue "
        "here proves the forward-lead branch did not run"
    )
    france = next(c for c in cues if c["text"] == "France")
    assert france.get("smart_emphasis") is True
    # "and" led forward INTO the following cue — the marker and the words after it
    # share one cue (pending_prefix), not two separate cues.
    lead = next(c for c in cues if c["text"].startswith("and "))
    assert lead["text"].split()[:2] == ["and", "I'll"]
