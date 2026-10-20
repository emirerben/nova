"""KRI-756 suggestion-chip replay eval: free, offline, no network, no skia.

The engine under test is ``app.services.suggestion_chips.build_suggestion_set`` driven by
hand-authored fixtures (``tests/fixtures/agent_evals/suggestion_chips/``) plus the
``nermin-*`` replay fixtures (``tests/fixtures/kria_turns``). The planner dry-run witnesses are
the real ones (see ``suggestion_chips_harness``).

Gates (every fixture runs every gate; see tests/evals/README.md for the pass bars):

* G1 grounding            every media_id / ordinal / moment / text bar a chip names exists
* G2 capability matrix    no chip whose manifest capability / editor op family is unavailable
* G3 not already in edit  no chip for a title, captions, music, SFX, transition already there
* G4 diversity            <= 4 chips, 4 chips => 3+ kinds, no duplicate (kind, target)
* G5 chip #1              first prompt opens with ``default_edit`` / ``Suggest an edit.``
* G6 hygiene              length, language, no quotes, stoplist, label parity, grounded ``why``
* G7 closed template set  every message is in the deck; no input text leaks into a message
                          or a label (Gemini text may live only in ``why``)
* G8 determinism          same context in, byte-identical JSON out (flag-off: see below)
* G9 executability        recorded planner answer -> replay_v2_strategy / replay_v2_editor.
                          COMPILE PATH ONLY. It does not prove today's prompts choose that
                          answer; that is a paid live run (README TODO 1).
* G11 wire contract       the response obeys the fixed KRI-756 wire shape and state semantics

G10 (property/fuzz over the wording agent's output) is ``test_chip_suggester_evals.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services import suggestion_chips as sc

from . import suggestion_chips_harness as h

CASES = h.all_cases()
CASES_BY_NAME = {c.name: c for c in CASES}

# Fixtures that document a bug in the engine today (harness.KNOWN_GAPS). They are strict
# xfails so the day the engine is fixed the test flips to XPASS (fails) and the entry must go.
KNOWN_GAPS = h.KNOWN_GAPS


def _marks(name: str, gate: str):
    reason = KNOWN_GAPS.get((name, gate))
    return [pytest.mark.xfail(strict=True, reason=reason)] if reason else []


_OUT: dict[str, sc.SuggestionSetOut] = {}


def out_for(case: h.Case) -> sc.SuggestionSetOut:
    if case.name not in _OUT:
        _OUT[case.name] = h.run_case(case)
    return _OUT[case.name]


def _ids(case: h.Case) -> str:
    return case.name


# ── Inventory and coverage ───────────────────────────────────────────────────────────


def test_fixture_inventory_is_well_formed():
    names = [c.name for c in CASES]
    assert len(names) == len(set(names)), "duplicate fixture names"
    assert len(h.load_cases()) >= 40, "the hand-authored matrix shrank"
    for case in CASES:
        assert case.description.strip(), f"{case.name}: add a description"
        assert case.expect, f"{case.name}: add an expect block (a regression pin)"
        assert case.state in ("first_prompt", "ready", "none"), case.name
    nermin = [c for c in CASES if "nermin" in c.tags]
    assert {c.path.stem for c in nermin} == {
        p.stem for p in h.KRIA_TURNS_DIR.glob("nermin-*.json")
    }, "every nermin-* replay fixture must project into at least one chip case"


def test_state_and_footage_matrix_is_covered():
    """The matrix the ticket asks for, as data: every cell has at least one fixture."""
    tags = {t for c in CASES for t in c.tags}
    required = {
        "speech",
        "music",
        "silent_broll",
        "single_clip",
        "many_clips",
        "flag_off",
        "phone",
        "cloud",
        "tr",
        "title_present",
        "captions",
        "pending",
        "empty",
        "no_media",
        "duplicate_subject",
        "v1",
        "none",
        "ready",
        "first_prompt",
        "saturated",
        "photos",
        "witness",
        "sfx",
        "transition",
    }
    assert required <= tags, f"uncovered matrix cells: {sorted(required - tags)}"
    cells = {(c.state, out_for(c).understanding) for c in CASES}
    assert {
        ("first_prompt", "done"),
        ("first_prompt", "pending"),
        ("first_prompt", "empty"),
        ("ready", "done"),
        ("none", "done"),
        ("none", "empty"),
    } <= cells


def test_every_shippable_kind_is_emitted_somewhere_in_each_state_it_supports():
    emitted = {(c.kind, out_for(case).state) for case in CASES for c in out_for(case).chips}
    expected = {
        ("default_edit", "first_prompt"),
        ("hook", "first_prompt"),
        ("hook", "ready"),
        ("captions", "first_prompt"),
        ("captions", "ready"),
        ("sfx", "first_prompt"),
        ("sfx", "ready"),
        ("transition", "first_prompt"),
        ("transition", "ready"),
        ("music", "ready"),
        ("title_animation", "ready"),
    }
    assert expected <= emitted, f"no fixture proves: {sorted(expected - emitted)}"
    # opener_clip (CLIP_ORDINAL_PROVEN) and pinned_text (PINNED_TEXT_PROVEN) are dark.
    assert not {k for k, _ in emitted} & set(h.DARK_KINDS)


# ── Per-fixture expectation (the human-reviewed pin) ────────────────────────────────


@pytest.mark.parametrize(
    "case", [pytest.param(c, id=c.name, marks=_marks(c.name, "expect")) for c in CASES]
)
def test_fixture_expectation(case: h.Case):
    problems = h.expectation_failures(case, out_for(case))
    assert not problems, f"{case.name}: " + "; ".join(problems)


# ── G1..G8 over every fixture ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case,gate",
    [
        pytest.param(c, g, id=f"{c.name}|{g.split()[0]}", marks=_marks(c.name, g))
        for c in CASES
        for g in h.GATES
    ],
)
def test_gate(case: h.Case, gate: str):
    problems = h.GATES[gate](case, out_for(case))
    assert not problems, f"{case.name} fails {gate}: " + "; ".join(problems)


def test_gate_summary_pass_bars():
    """The numbers the ticket's pass bars are about, computed over the whole matrix.

    Known-gap fixtures are reported, not hidden: they are excluded from the bar only
    because each is a strict xfail above that fails loudly when the engine changes.
    """
    totals = {g: [0, 0] for g in h.GATES}
    chips = 0
    for case in CASES:
        out = out_for(case)
        chips += len(out.chips)
        for gate, fn in h.GATES.items():
            if (case.name, gate) in KNOWN_GAPS:
                continue
            totals[gate][1] += 1
            totals[gate][0] += 0 if fn(case, out) else 1
    failing = {g: f"{ok}/{n}" for g, (ok, n) in totals.items() if ok != n}
    assert not failing, failing
    assert chips >= 60, "matrix too thin to say anything about the chips"


# ── Negative controls: the gates must actually be able to fail ──────────────────────


def _chip(kind, label, message, *, rank=1, **anchor):
    return sc.SuggestionChipOut(
        id=f"sc_{kind}{rank}",
        kind=kind,
        label=label,
        message=message,
        rank=rank,
        default_edit=kind == "default_edit",
        anchor=sc.ChipAnchorOut(**anchor),
    )


def _bad(state, *chips):
    return sc.SuggestionSetOut(
        set_id="sg_bad",
        state=state,
        status="ok",
        wording="fallback",
        anchor=sc.SuggestionAnchorOut(media_digest="d", render_generation_id="g"),
        chips=list(chips),
    )


NEGATIVE_CONTROLS = [
    (
        "G1 grounding",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            _chip(
                "hook",
                "Add a hook to the opening",
                "Add a hook that matches the video",
                media_id="ghost",
                clip_ordinal=9,
                moment=sc.ChipMomentOut(start_s=99.0, end_s=100.0),
                text_bar_index=5,
            ),
        ),
    ),
    (
        "G2 capability matrix",
        "fp_sfx_flag_off",
        _bad("first_prompt", _chip("sfx", "Add sound effects", "Add sound effects")),
    ),
    (
        "G2 capability matrix",
        "rd_sfx_empty_catalog",
        _bad("ready", _chip("sfx", "Add sound effects", "Add sound effects")),
    ),
    (
        "G3 not already in edit",
        "rd_captions_already_on",
        _bad("ready", _chip("captions", "Add captions", "Add captions")),
    ),
    (
        "G3 not already in edit",
        "rd_music_quiet",
        _bad("ready", _chip("music", "Turn the music down", "Turn the music down")),
    ),
    (
        "G3 not already in edit",
        "rd_title_animated",
        _bad("ready", _chip("title_animation", "Animate the title", "Animate the title")),
    ),
    (
        "G3 not already in edit",
        "rd_transitions_already_set",
        _bad("ready", _chip("transition", "Add transitions", "Add transitions between the clips")),
    ),
    (
        "G4 diversity",
        "rd_plain_montage",
        _bad(
            "ready",
            _chip("hook", "Add a hook to the opening", "Add a hook that matches the video"),
            _chip("hook", "Add a hook to the opening", "Add a hook that matches the video", rank=2),
            _chip("sfx", "Add sound effects", "Add sound effects", rank=3),
            _chip("sfx", "Add sound effects", "Add sound effects", rank=4),
        ),
    ),
    (
        "G5 chip #1 is default_edit",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            _chip("hook", "Add a hook to the opening", "Add a hook that matches the video"),
        ),
    ),
    (
        "G6 hygiene",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            _chip("hook", "Make it pop", 'Add a "hook" that matches the video'),
        ),
    ),
    (
        "G6 hygiene",
        "fp_turkish_silent",
        _bad("first_prompt", _chip("captions", "Add captions", "Altyazı ekle")),
    ),
    (
        "G7 closed template set",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            _chip("hook", "Add a hook to the opening", "Add a hook about pouring matcha"),
        ),
    ),
    (
        # Gemini text may live only in `why`: a label that carries the clip subject is a leak
        "G7 closed template set",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            _chip(
                "hook", "Hook on pouring matcha into a glass", "Add a hook that matches the video"
            ),
        ),
    ),
    (
        "G2 capability matrix",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            _chip("captions", "Add captions", "Add captions"),
        ),
    ),
    (
        "G2 capability matrix",
        "rd_music_led_no_speech",
        _bad("ready", _chip("music", "Turn the music down", "Turn the music down")),
    ),
    (
        "G2 capability matrix",
        "fp_silent_broll_music",
        _bad("first_prompt", _chip("opener_clip", "Open on IMG_1", "Start with IMG_1")),
    ),
    (
        # an agent label that fails the label rules (digit, off-lexicon) must be caught
        "G6 hygiene",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            sc.SuggestionChipOut(
                id="sc_x",
                kind="hook",
                label="Add 3 killer hooks",
                message="Add a hook that matches the video",
                rank=1,
                label_source="agent",
            ),
        ),
    ),
    (
        # a `why` that shares no word with the footage is a hallucination
        "G6 hygiene",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            sc.SuggestionChipOut(
                id="sc_x",
                kind="hook",
                label="Add a hook to the opening",
                message="Add a hook that matches the video",
                rank=1,
                label_source="agent",
                why="skateboard tricks at sunset",
            ),
        ),
    ),
    (
        # `default_edit` is true only for the rank-1 default edit at first_prompt
        "G11 wire contract",
        "rd_plain_montage",
        _bad(
            "ready",
            sc.SuggestionChipOut(
                id="sc_h",
                kind="hook",
                label="Add a hook to the opening",
                message="Add a hook that matches the video",
                rank=1,
                default_edit=True,
            ),
        ),
    ),
    (
        "G11 wire contract",
        "fp_understanding_pending",
        sc.SuggestionSetOut(state="first_prompt", status="pending", chips=[]),
    ),
    (
        "G11 wire contract",
        "rd_plain_montage",
        _bad("ready", sc.SuggestionChipOut(id="sc_k", kind="telepathy", label="a", message="b")),
    ),
    (
        "G4 diversity",
        "fp_silent_broll_music",
        _bad(
            "first_prompt",
            _chip("default_edit", "Let Kria cut the first version", "Suggest an edit."),
            _chip("hook", "Add a hook to the opening", "Add a hook that matches the video", rank=2),
            _chip("hook", "Add a hook to the opening", "Add a hook that matches the video", rank=2),
        ),
    ),
]


@pytest.mark.parametrize(
    "gate,fixture,bad",
    [pytest.param(g, f, b, id=f"{g.split()[0]}-{f}") for g, f, b in NEGATIVE_CONTROLS],
)
def test_each_gate_catches_a_known_bad_set(gate, fixture, bad):
    assert h.GATES[gate](CASES_BY_NAME[fixture], bad), f"{gate} let a bad chip through"


def test_determinism_gate_catches_a_nondeterministic_engine(monkeypatch):
    import itertools

    counter = itertools.count()
    real = sc.build_suggestion_set

    def flaky(ctx, wording=None):
        out = real(ctx, wording)
        out.set_id = f"sg_{next(counter)}"
        return out

    monkeypatch.setattr(sc, "build_suggestion_set", flaky)
    assert h.check_determinism(CASES_BY_NAME["fp_silent_broll_music"])


# ── G5 specifics ─────────────────────────────────────────────────────────────────────


def test_chip_one_matches_the_ios_media_only_constant():
    """MEDIA_ONLY_MESSAGE must equal ios ChatSubmission.mediaOnlyMessage, byte for byte."""
    swift = h.REPO_ROOT / "src/apps/ios/Kria/Features/ChatTimeline.swift"
    if not swift.exists():
        pytest.skip("iOS sources not in this checkout")
    text = swift.read_text(encoding="utf-8")
    match = re.search(r'mediaOnlyMessage\s*=\s*"([^"]+)"', text)
    assert match, "ChatTimeline.swift no longer declares mediaOnlyMessage"
    assert match.group(1) == sc.MEDIA_ONLY_MESSAGE == "Suggest an edit."


# ── Witnesses and fail-closed behaviour ──────────────────────────────────────────────


def test_a_refusing_witness_drops_exactly_that_kind():
    case = CASES_BY_NAME["fp_silent_broll_music"]
    base = h.build_context(case)
    assert {c.kind for c in sc.build_suggestion_set(base).chips} >= {"hook", "sfx", "transition"}
    real = base.strategy_witness
    refused = {"sfx"}
    ctx = sc.SuggestionContext(
        **{
            **base.__dict__,
            "strategy_witness": lambda kind: None if str(kind) in refused else real(kind),
        }
    )
    kinds = [c.kind for c in sc.build_suggestion_set(ctx).chips]
    assert "sfx" not in kinds and "hook" in kinds and "transition" in kinds


def test_a_crashing_witness_never_reaches_the_client():
    case = CASES_BY_NAME["rd_plain_montage"]
    base = h.build_context(case)

    def boom(op):
        raise RuntimeError("compile exploded")

    ctx = sc.SuggestionContext(**{**base.__dict__, "editor_witness": boom})
    out = sc.build_suggestion_set(ctx)
    assert out.status == "ok" and out.chips == []


def test_same_target_collisions_are_deduped():
    """Two kinds that touch the same edit target never both ship (director-style dedupe)."""
    case = CASES_BY_NAME["rd_plain_montage"]
    base = h.build_context(case)
    ctx = sc.SuggestionContext(
        **{**base.__dict__, "editor_witness": lambda op: frozenset({"timeline:any"})}
    )
    out = sc.build_suggestion_set(ctx)
    assert len(out.chips) == 1, [c.kind for c in out.chips]


def test_engine_never_raises_on_garbage_context():
    for bad in (
        sc.SuggestionContext(state="ready", snapshot={"slots": "nope", "text_bars": 3}),
        sc.SuggestionContext(state="first_prompt", clips=[None, 3, "x"], understanding="complete"),
        sc.SuggestionContext(state="first_prompt", manifest={"capabilities": None}),
        sc.SuggestionContext(state="ready", snapshot={"allowed_op_families": None}),
        sc.SuggestionContext(state="bogus"),
    ):
        out = sc.build_suggestion_set(bad)
        assert out.chips == [] or all(c.message for c in out.chips)
        assert out.status in ("ok", "pending", "unavailable")


def test_unavailable_set_is_the_flag_off_body():
    out = sc.unavailable_set()
    assert out.model_dump(include={"schema_version", "status", "state", "chips"}) == {
        "schema_version": 1,
        "status": "unavailable",
        "state": "none",
        "chips": [],
    }


# ── Opener (gate (b)) ────────────────────────────────────────────────────────────────


def test_opener_clip_is_dark_everywhere_until_the_ordinal_is_proven():
    assert sc.CLIP_ORDINAL_PROVEN is False
    for case in CASES:
        assert "opener_clip" not in {c.kind for c in out_for(case).chips}, case.name


def test_flipping_clip_ordinal_proven_needs_a_proof_fixture_first():
    """Backend gate (b). The planner's "clip N" is the order the clips PLAY (main_creator.txt
    :267, :309), the model resolves creator references by filename or subject, and the media
    strip is upload order. Nothing links them. Whoever flips CLIP_ORDINAL_PROVEN must add a
    fixture proving 'Start with clip {n}' resolves to chip.anchor.media_id through the
    planner's own ordinal resolution, then delete this assertion."""
    assert sc.CLIP_ORDINAL_PROVEN is False, (
        "CLIP_ORDINAL_PROVEN was flipped without a proof fixture: add one that resolves "
        "'Start with clip {n}' to anchor.media_id via the planner, then update this test"
    )


def _opener_ctx(filenames):
    case = CASES_BY_NAME["rd_plain_montage"]
    ctx = h.build_context(case)
    clips = [
        {
            "media_id": f"clip-{i}",
            "ordinal": i,
            "kind": "video",
            "subject": subject,
            "filename": filenames[i - 1],
            "has_speech": False,
            "to_camera": False,
            "duration_s": 3.0,
            "notable_moments": [{"start_s": 0.5, "end_s": 1.5, "label": "moment"}],
        }
        for i, subject in enumerate(["morning coffee pour", "walking the dog", "evening run"], 1)
    ]
    return sc.SuggestionContext(**{**ctx.__dict__, "clips": clips})


def test_opener_wiring_is_correct_when_the_flag_is_forced_on(monkeypatch):
    """The dark path is unexercised in production; exercise it here so flipping the flag later
    does not reveal an untested kind. Editor path (ready) with the real reorder_clip witness.
    The message and label carry the creator-owned FILENAME (identically), never a Gemini
    subject; the chip must actually be emitted (this test is not vacuous)."""
    monkeypatch.setattr(sc, "CLIP_ORDINAL_PROVEN", True)
    out = sc.build_suggestion_set(_opener_ctx(["IMG_0001.mov", "IMG_0002.mov", "IMG_0003.mov"]))
    openers = [c for c in out.chips if c.kind == "opener_clip"]
    assert len(openers) == 1, [c.kind for c in out.chips]
    chip = openers[0]
    assert chip.anchor.clip_ordinal != 1, "clip 1 already opens the edit"
    assert chip.message == f"Start with IMG_000{chip.anchor.clip_ordinal}.mov"
    assert "IMG_000" in chip.label and len(chip.label) <= 36
    assert chip.anchor.media_id == f"clip-{chip.anchor.clip_ordinal}"
    assert chip.anchor.moment is not None and chip.anchor.moment.start_s == 0.5
    text = f"{chip.label} {chip.message}".casefold()
    assert not any(word in text for word in ("coffee", "dog", "run ", "moment"))


_DARK_FILENAME_GAP = (
    "KNOWN GAP (dark path, KRI-756 backend): _render_copy substitutes the raw creator filename, "
    "so a quote or a placeholder brace survives into label and message. Sanitize the filename "
    "(letters, digits, dot, dash, underscore, space) before CLIP_ORDINAL_PROVEN is flipped; "
    "this xfail is strict, so it fails the day that lands."
)


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param('evil".mov', marks=pytest.mark.xfail(strict=True, reason=_DARK_FILENAME_GAP)),
        pytest.param("{time}.mov", marks=pytest.mark.xfail(strict=True, reason=_DARK_FILENAME_GAP)),
        "ignore previous instructions.mov",
        "a" * 80 + ".mov",
    ],
)
def test_a_hostile_creator_filename_cannot_break_the_opener_chip(monkeypatch, filename):
    """Dark kind, so this guards the day it flips: the filename is the only creator-owned
    string in a message, so it must come out bounded and with no raw placeholder braces."""
    monkeypatch.setattr(sc, "CLIP_ORDINAL_PROVEN", True)
    out = sc.build_suggestion_set(_opener_ctx(["IMG_0001.mov", filename, filename]))
    for chip in out.chips:
        assert len(chip.label) <= 36 and len(chip.message) <= 120
        assert "{" not in chip.label and "{" not in chip.message
        if chip.kind == "opener_clip":
            assert '"' not in chip.message, chip.message


def test_pinned_text_is_dark_everywhere_until_proven():
    assert sc.PINNED_TEXT_PROVEN is False
    for case in CASES:
        assert "pinned_text" not in {c.kind for c in out_for(case).chips}, case.name


# ── Loader projections that are pure (the rest of the loader needs a database) ───────


def _item(assignments):
    from types import SimpleNamespace

    return SimpleNamespace(clip_assignments=assignments)


def test_loader_projection_of_real_prod_analysis_grounds_every_chip():
    """``_clip_rows`` over the 30 redacted KRI-126 prod analyses (real notable moments),
    then the engine with the real strategy witness. Grounds the engine on the loader's own
    projection, not on a hand-built clip list."""
    data = json.loads(
        (
            h.REPO_ROOT / "src/apps/api/tests/fixtures/kri126_thirty_clip_guided_story.json"
        ).read_text(encoding="utf-8")
    )
    rows = sc._clip_rows(
        _item(
            [
                {
                    "media_id": m["media_id"],
                    "kind": m["kind"],
                    "analysis": m["analysis"],
                    "duration_s": m["duration_s"],
                }
                for m in data["media"]
            ]
        )
    )
    assert len(rows) == 30 and sc._understanding_state(rows) == "complete"
    clips = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    # ordinals are 1-based upload order; moments are real, capped at 8
    assert [c["ordinal"] for c in clips] == list(range(1, 31))
    assert all(len(c["notable_moments"]) <= 8 for c in clips)
    assert any(c["notable_moments"] for c in clips) and any(c["has_speech"] for c in clips)
    raw = {
        "name": "loader_projection_kri126",
        "context": {
            "thread_id": "t-kri126",
            "state": "first_prompt",
            "understanding": "complete",
            "runtime_version": 2,
            "media_digest": "d-kri126",
            "edit_format": "montage",
            "clips": clips,
        },
        "expect": {"kinds_include": ["default_edit"]},
    }
    case = h.Case(
        name=raw["name"], path=Path("-"), description="", raw=raw, expect=raw["expect"], tags=[]
    )
    out = h.run_case(case)
    assert out.chips[0].kind == "default_edit"
    for gate, fn in h.GATES.items():
        assert not fn(case, out), gate


@pytest.mark.parametrize(
    "assignments,expected",
    [
        ([], "n/a"),
        ([{"media_id": "m1", "kind": "video", "analysis": None}], "pending"),
        (
            [
                {
                    "media_id": "m1",
                    "kind": "video",
                    "analysis": {"understanding_attempts": 3},
                }
            ],
            "empty",
        ),
        (
            [
                {
                    "media_id": "m1",
                    "kind": "video",
                    "analysis": {"subject": "a dog", "source": "clip_metadata"},
                }
            ],
            "complete",
        ),
        # an unanalysed PHOTO is never "still being looked at": attach enqueues videos only
        ([{"media_id": "p1", "kind": "image", "analysis": None}], "empty"),
    ],
)
def test_understanding_state_cells(assignments, expected):
    rows = sc._clip_rows(_item(assignments))
    assert sc._understanding_state(rows) == expected


@pytest.mark.parametrize(
    "events,expected",
    [
        ([], False),
        ([("assistant_question", {})], True),
        ([("format_prompt", {})], True),
        ([("approval_requested", {})], True),
        ([("assistant_strategy", {})], True),
        # a v2 reply that is a question
        ([("assistant_response", {"turn_value": "question"})], True),
        # bookkeeping after the question does not close it
        ([("plan_block", {}), ("status_update", {}), ("assistant_question", {})], True),
        # an answer (decision) after the question closes it
        ([("assistant_response", {"turn_value": "decision"}), ("assistant_question", {})], False),
        ([("generation_ready", {}), ("assistant_question", {})], False),
        # a payload carrying a question field counts as open
        ([("assistant_response", {"choice_question": {"id": "q1"}})], True),
    ],
)
def test_open_question_detection(events, expected):
    """Newest first. An open question means state ``none``: chips would hijack the answer."""
    assert sc._open_question(events) is expected


# ── Executability (G9) ───────────────────────────────────────────────────────────────

CASSETTES = h.load_cassettes()

# Kinds whose claim the compile path cannot check, by design. title_animation is NOT in this
# set: patch_text with animation_phases compiles ("Restyle 1 text"), so it is proven below.
# (opener_clip is dark and has no fixture.) Nothing is excused silently.
UNPROVABLE_BY_COMPILE: set[str] = set()

# What a successful tap must change, per (kind, engine). The hook row is deliberately weaker
# and labelled: it reaches wording approval with a candidate, it does not execute an op.
CHIP_KIND_TO_EXPECTS = {
    "default_edit": "a draft strategy that survives check_strategy_for_runtime_v2",
    "hook": "reaches wording approval with a candidate (NOT an executed op)",
    "captions": "caption_style=clean survives (strategy) / Set caption meta (editor)",
    "sfx": "'sfx' in optional_treatments after the check (strategy) / Add sfx (editor)",
    "transition": "'transitions' in optional_treatments after the check / Set transition",
    "music": "Set mix",
    "title_animation": "Restyle 1 text (entrance animation via patch_text)",
}


def test_every_chip_kind_has_an_expectation_row():
    kinds = {k for k in sc.ChipKind if str(k) not in h.DARK_KINDS}
    assert {str(k) for k in kinds} == set(CHIP_KIND_TO_EXPECTS)
    # the engine's own spec row states what a tap must change; the eval row must exist too
    for kind in kinds:
        assert sc.CHIP_KIND_GATE[kind].expects.strip()


@pytest.mark.parametrize("cassette", [pytest.param(c, id=c["id"]) for c in CASSETTES])
def test_cassette_message_matches_engine(cassette):
    """A recorded answer is only valid for the exact message it answers. If the copy deck
    changes the answer is stale, and this fails until it is re-authored (or re-recorded)."""
    case = CASES_BY_NAME[cassette["fixture"]]
    chips = {c.kind: c for c in out_for(case).chips}
    if cassette["kind"] not in chips:
        # the strategy cassettes borrow a fixture's footage; the kind must still be offered
        # in that STATE by some fixture, and the message comes from the deck
        pytest.fail(f"{case.name} does not emit {cassette['kind']}: {list(chips)}")
    assert chips[cassette["kind"]].message == cassette["message"]


def test_every_emitted_kind_and_state_has_a_cassette():
    covered = {(c["kind"], c["state"]) for c in CASSETTES}
    emitted = {(c.kind, out_for(case).state) for case in CASES for c in out_for(case).chips}
    missing = sorted(k for k in emitted if k not in covered and k[0] not in UNPROVABLE_BY_COMPILE)
    assert not missing, f"emitted without an executability cassette: {missing}"


@pytest.mark.parametrize("cassette", [pytest.param(c, id=c["id"]) for c in CASSETTES])
def test_executability_replay(cassette):
    problems = h.cassette_failures(cassette, CASES_BY_NAME)
    assert not problems, f"{cassette['id']}: " + "; ".join(problems)


def test_strategy_treatment_is_dropped_when_the_capability_is_off():
    """The before/after diff catches what the refusal path misses: with sound_effects off the
    policy keeps the draft and silently drops 'sfx'. This is why the engine diffs."""
    cassette = next(c for c in CASSETTES if c["id"] == "sfx-first_prompt")
    off = CASES_BY_NAME["fp_sfx_flag_off"]
    replay = h.replay_strategy(off, cassette["message"], cassette["model_output"])
    assert "sfx" in replay.strategy_before.optional_treatments
    assert "sfx" not in replay.checked.strategy.optional_treatments


# ── Contract examples for the web and iOS lanes ──────────────────────────────────────

CONTRACT = h.CONTRACT


@pytest.mark.parametrize("filename,fixture", sorted(CONTRACT.items()))
def test_contract_examples_are_what_the_engine_emits(filename, fixture):
    path = h.FIXTURE_DIR / filename
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    # round-trips through the wire model: clients can rely on exactly these keys
    assert sc.SuggestionSetOut.model_validate(on_disk).model_dump(mode="json") == on_disk
    assert on_disk == json.loads(out_for(CASES_BY_NAME[fixture]).model_dump_json()), (
        f"{filename} drifted from the engine; regenerate with "
        "`python scripts/suggestion_chip_report.py --write-contract`"
    )


def test_contract_unavailable_example_is_the_flag_off_body():
    path = h.FIXTURE_DIR / "contract_example_unavailable.json"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(
        sc.unavailable_set().model_dump_json()
    )


# ── Report plumbing ──────────────────────────────────────────────────────────────────


def test_old_chip_source_is_pinned_to_the_ios_hardcoded_strings():
    """The OLD side of the report: the iOS ReadyStage strings on origin/main."""
    from scripts.suggestion_chip_report import old_chips  # noqa: PLC0415

    assert old_chips("ready") == ["Try a stronger opening", "Make it warmer"]
    assert old_chips("first_prompt") == []
    assert old_chips("none") == []


def test_rubric_file_covers_the_deterministic_stoplist():
    """The judged rubric and the free lexical stoplist must not drift apart."""
    rubric = (Path(__file__).parent / "rubrics" / "suggestion_chips.md").read_text(encoding="utf-8")
    low = rubric.casefold()
    for phrase in h.STOPLIST:
        assert phrase in low, f"rubric never mentions stoplisted phrase {phrase!r}"
    for dimension in (
        "everyday_voice",
        "no_burden",
        "specificity_to_this_footage",
        "honest_label",
        "non_redundant_set",
        "grounded_why",
    ):
        assert dimension in rubric
    assert "Pass threshold: avg >= 4.0" in rubric
