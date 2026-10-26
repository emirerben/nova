"""KRI-847: a model-recorded ceiling ("at most 10 seconds") is not "exactly 10 s +/- 10 %".

Don Julio T2 (2026-10-21): the creator wrote "... Under 10 seconds." The Main Creator filed
``{"kind": "timing", "scope": "global", "facts": {"duration_s": 10}}`` and
``brief_checks._check_timing`` compared the draft with ``creator_duration_matches`` (10 s
+/- 10 %). A correct 7.5 s Talking draft (about 9.1 s with the phone outro) got "This draft is
about 7.5s; you asked for 10s."; a checked, non-format-limit partial is
``needs_creator_choice``, so it holds the draft behind the "simpler version?" question.

Two layers now answer it, and neither reads the creator's words for the new fact (KRI-667):

* EXCLUSIVE wording ("under N", "less than N", "shorter than N", TR "N saniyenin altında")
  is the merged KRI-629 strict cap (``length_policy.strict_duration_limit_s``): judged on the
  MEASURED complete export including the 1.6 s ending, "unchecked" until it is measured.
  ``duration_bound`` never changes that path.
* INCLUSIVE wording ("at most", "max", "no more than", "up to", TR "en fazla N saniye",
  "N saniyeyi geçmesin") has no strict cap. The model records
  ``facts["duration_bound"] == "at_most"`` beside ``duration_s`` and the server only removes
  the LOWER side of the existing +/- 10 % check: finite and 0 < actual <= N * 1.10. It can
  relax what exact already checked, never tighten it, so a wrong or stale fact cannot create
  a hard cap. Anything else, or a missing key, is the legacy exact check.

Tests marked "guard" pass on the current code on purpose: they pin behaviour the fix must keep.
"""

from __future__ import annotations

import pytest

from app.kria.brief import (
    BriefRequirement,
    BriefUpdate,
    CreativeBrief,
    apply_updates,
    bind_updates,
    parse_brief_updates,
)
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    needs_creator_choice,
    render_block_recovery,
)
from app.kria.reply_language import reply_language_for

BOUND_KEY = "duration_bound"
AT_MOST = "at_most"

SUFFIX_EN = "Your draft is saved. Should I try again or simplify this request?"

# The phone appends this after export; the plan's own length excludes it (brief_checks.py:278).
OUTRO_S = 1.6


def _req(description: str, *, req_id: str = "r1", **facts) -> BriefRequirement:
    return BriefRequirement(
        id=req_id, kind="timing", scope="global", description=description, facts=facts
    )


def _at_most_ten() -> BriefRequirement:
    """INCLUSIVE wording as the model stores it: no strict cap, so ``at_most`` decides."""
    return _req("At most 10 seconds.", duration_s=10, **{BOUND_KEY: AT_MOST})


def _under_ten() -> BriefRequirement:
    """DJ T2 as the model stores it: EXCLUSIVE wording, so main's strict cap decides."""
    return _req("Under 10 seconds.", duration_s=10, **{BOUND_KEY: AT_MOST})


def _talking(
    duration_s: float | None, outro_s: float = OUTRO_S, measured: float | None = None
) -> PlanFacts:
    """DJ T2: a Talking (talking_head) edit that keeps the creator's own audio."""
    return PlanFacts(
        edit_format="talking_head",
        audio_strategy="original_audio",
        duration_s=duration_s,
        outro_s=outro_s,
        measured_export_duration_s=measured,
    )


# --------------------------------------------- DJ T2: exclusive "Under 10 seconds" (KRI-629)


def test_dj_t2_before_the_export_is_measured_waits_instead_of_missing():
    """The bug: "This draft is about 7.5s; you asked for 10s." Now: nothing to hold the render."""
    [receipt] = build_receipts([_under_ten()], _talking(7.5), include_unchecked=True)

    assert receipt.verification == "unchecked"
    assert "10s" not in (receipt.reason or "")
    assert "you asked for" not in (receipt.reason or "")
    assert not needs_creator_choice(receipt)


def test_dj_t2_the_measured_export_with_its_ending_under_ten_is_met():
    receipt = check_requirement(_under_ten(), _talking(7.5, measured=9.1))

    assert receipt.status == "met"
    assert receipt.reason is None


def test_dj_t2_a_measured_export_over_ten_is_a_miss_in_the_strict_wording():
    receipt = check_requirement(_under_ten(), _talking(7.5, measured=11.0))

    assert receipt.status == "partial"
    assert receipt.reason == (
        "The finished video is 11s including its ending; you asked for under 10s."
    )


@pytest.mark.parametrize(
    "wording", ["Under 10 seconds", "less than 10 seconds", "shorter than 10 s"]
)
def test_exclusive_wording_stays_the_strict_cap_whatever_the_bound_fact_says(wording):
    for facts in ({"duration_s": 10}, {"duration_s": 10, BOUND_KEY: AT_MOST}):
        req = _req(wording, **facts)
        assert check_requirement(req, _talking(7.5, measured=10.0)).status == "partial"
        assert check_requirement(req, _talking(7.5, measured=9.99)).status == "met"


# ----------------------------------------- inclusive wording: "at most 10 seconds" (the fact)


@pytest.mark.parametrize(
    "draft_s",
    [
        pytest.param(3.0, id="much-shorter"),
        pytest.param(5.0, id="half"),
        pytest.param(7.5, id="don-julio-t2"),
        pytest.param(8.9, id="just-below-the-old-10-percent-band"),
        pytest.param(9.0, id="edge-of-the-old-band-guard"),
        pytest.param(10.0, id="exactly-at-the-number-guard"),
        pytest.param(10.04, id="one-frame-over-guard"),
        # The upper side is the exact tolerance: 11.0 s is 10 s + 10 % and is met, as for exact.
        pytest.param(11.0, id="upper-boundary-same-tolerance-as-exact"),
    ],
)
def test_a_draft_at_or_below_the_stated_at_most_is_met(draft_s):
    receipt = check_requirement(_at_most_ten(), _talking(draft_s))

    assert receipt.status == "met", receipt.reason
    assert receipt.reason is None


def test_the_t2_shaped_draft_does_not_hold_the_render():
    """A 7.5 s draft must not be a ``needs_creator_choice`` failure (the render hold)."""
    [receipt] = build_receipts([_at_most_ten()], _talking(7.5), include_unchecked=True)

    assert receipt.verification == "checked"
    assert receipt.status == "met"
    assert not needs_creator_choice(receipt)


@pytest.mark.parametrize(
    ("edit_format", "audio_strategy"),
    [
        pytest.param("montage", "licensed_music", id="montage-footage-owner"),
        pytest.param("talking_head", "original_audio", id="talking-take-owner"),
        pytest.param("subtitled", "original_audio", id="phone-talking-take-owner"),
        pytest.param("narrated_planned", "voiceover", id="voiceover-owner"),
    ],
)
def test_an_at_most_holds_for_every_length_owner(edit_format, audio_strategy):
    facts = PlanFacts(edit_format=edit_format, audio_strategy=audio_strategy, duration_s=7.5)

    assert check_requirement(_at_most_ten(), facts).status == "met"


# --------------------------------------------- over the stated number is still a miss


@pytest.mark.parametrize(
    "draft_s",
    [
        pytest.param(11.5, id="just-past-the-exact-tolerance"),
        pytest.param(12.0, id="two-seconds-over"),
        pytest.param(25.0, id="far-over"),
    ],
)
def test_a_draft_over_the_at_most_tolerance_is_a_miss(draft_s):
    [receipt] = build_receipts([_at_most_ten()], _talking(draft_s), include_unchecked=True)

    assert receipt.status == "partial"
    assert receipt.verification == "checked"
    assert needs_creator_choice(receipt)


def test_the_at_most_miss_names_the_ceiling_not_an_exact_target():
    receipt = check_requirement(_at_most_ten(), _talking(12.0))

    reason = receipt.reason or ""
    assert "This draft is about 12s" in reason
    assert "you asked for at most 10s" in reason, reason
    assert "you asked for 10s" not in reason, reason
    assert f"plus a {OUTRO_S}s outro" in reason  # the outro note is unchanged


def test_the_at_most_miss_is_worded_in_turkish_in_a_turkish_chat():
    with reply_language_for("tr"):
        reason = check_requirement(_at_most_ten(), _talking(12.0)).reason or ""

    assert "Bu taslak yaklaşık 12 saniye" in reason
    assert "en fazla 10 saniye" in reason, reason
    assert "sen 10 saniye istedin" not in reason, reason


def test_the_block_message_says_the_ceiling():
    [receipt] = build_receipts(
        [_at_most_ten()], _talking(12.0, outro_s=0.0), include_unchecked=True
    )

    message = render_block_recovery([receipt.model_dump(mode="json")]).message

    assert message == f"This draft is about 12s; you asked for at most 10s. {SUFFIX_EN}"


def test_the_draft_length_is_still_said_once_beside_a_second_length_ask():
    """KRI-778 guard: the "at most" sentence must still match ``_DRAFT_LENGTH_RE`` so a brief
    with two global length asks states the draft's length once (the last ask's)."""
    asks = [
        _at_most_ten(),
        _req("About 14 seconds", req_id="r2", duration_s=14),
    ]
    rows = [
        receipt.model_dump(mode="json")
        for receipt in build_receipts(asks, _talking(12.0, outro_s=0.0), include_unchecked=True)
    ]

    message = render_block_recovery(rows).message

    assert message.count("This draft is about") == 1, message
    assert message == f"This draft is about 12s; you asked for 14s. {SUFFIX_EN}"


def test_the_turkish_at_most_sentence_is_also_said_once():
    asks = [_at_most_ten(), _req("Yaklaşık 14 saniye", req_id="r2", duration_s=14)]
    with reply_language_for("tr"):
        rows = [
            receipt.model_dump(mode="json")
            for receipt in build_receipts(asks, _talking(12.0, outro_s=0.0), include_unchecked=True)
        ]
        message = render_block_recovery(rows).message

    assert message.count("Bu taslak yaklaşık") == 1, message
    assert "14 saniye istedin" in message and "en fazla" not in message


# ------------------------------------------------------ exact lengths keep +/- 10 % (guards)


def test_guard_an_exact_ten_second_ask_with_a_short_draft_is_still_a_miss():
    exact = _req("make it 10 seconds", duration_s=10)

    receipt = check_requirement(exact, _talking(7.5, outro_s=0.0))

    assert receipt.status == "partial"
    assert receipt.reason == "This draft is about 7.5s; you asked for 10s."


def test_guard_an_exact_ask_marked_exact_is_the_same_as_no_bound():
    explicit = _req("make it 10 seconds", duration_s=10, **{BOUND_KEY: "exact"})

    assert check_requirement(explicit, _talking(7.5, outro_s=0.0)).status == "partial"
    assert check_requirement(explicit, _talking(10.9, outro_s=0.0)).status == "met"
    assert check_requirement(explicit, _talking(11.1, outro_s=0.0)).status == "partial"


@pytest.mark.parametrize("junk", ["at_least", "AT_MOST ", "", None, 1, True, ["at_most"]])
def test_guard_an_unknown_bound_value_fails_closed_to_exact(junk):
    req = _req("At most 10 seconds.", duration_s=10, **{BOUND_KEY: junk})

    assert check_requirement(req, _talking(7.5, outro_s=0.0)).status == "partial"


def test_guard_a_legacy_requirement_without_a_bound_keeps_the_exact_tolerance():
    """Old persisted briefs (no ``duration_bound``) behave exactly as before this ticket.

    The ceiling is never inferred from the description's words ("At most 10 seconds."), so
    tests/kria/test_kri777_freeze_frame_ask.py (`("15 seconds at most", 15)`) keeps its meaning.
    """
    legacy = _req("At most 10 seconds.", duration_s=10)

    assert check_requirement(legacy, _talking(7.5, outro_s=0.0)).status == "partial"
    assert check_requirement(legacy, _talking(10.9, outro_s=0.0)).status == "met"


def test_guard_an_unknown_draft_length_cannot_be_confirmed_for_an_at_most():
    receipt = check_requirement(_at_most_ten(), _talking(None))

    assert receipt.status == "partial"
    assert receipt.reason == "I can't confirm this draft's length yet."


def test_guard_a_moment_length_is_never_a_total_ceiling():
    """ "Freeze ... at most 1 second" is a moment, not the video (KRI-777): it never gets a
    "you asked for ..." verdict on the total length, ceiling or not."""
    freeze = _req(
        "Freeze the frame for at most 1 second on the bite",
        duration_s=1,
        **{BOUND_KEY: AT_MOST},
    )

    receipt = check_requirement(freeze, _talking(7.5, outro_s=0.0))

    assert "you asked for" not in (receipt.reason or "")


# ------------------------------------------- a later exact ask replaces an earlier ceiling


def _prior_brief_with_ceiling() -> CreativeBrief:
    return CreativeBrief(version=1, requirements=[_at_most_ten()])


def test_a_partial_change_to_an_exact_length_drops_the_old_ceiling():
    """ "At most 10 seconds" then "make it 15 seconds". The model changes r1 with a bare
    ``{"duration_s": 15}``; ``_complete_change`` (KRI-805) merges the old facts, so the stale
    ``duration_bound`` would turn the new exact 15 s into a ceiling."""
    prior = _prior_brief_with_ceiling()
    update = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        description="Make it 15 seconds",
        facts={"duration_s": 15},
    )

    after = apply_updates(prior, bind_updates(prior, [update]), source_turn_id="t2")

    [live] = [req for req in after.live() if req.kind == "timing"]
    assert live.facts["duration_s"] == 15
    assert live.facts.get(BOUND_KEY) != AT_MOST, live.facts


def test_guard_a_complete_change_to_an_exact_length_has_no_ceiling():
    prior = _prior_brief_with_ceiling()
    update = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        kind="timing",
        scope="global",
        description="Make it 15 seconds",
        facts={"duration_s": 15},
    )

    after = apply_updates(prior, bind_updates(prior, [update]), source_turn_id="t2")

    [live] = [req for req in after.live() if req.kind == "timing"]
    assert live.facts == {"duration_s": 15}


def test_a_partial_change_that_restates_the_ceiling_keeps_it():
    prior = _prior_brief_with_ceiling()
    update = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        description="At most 8 seconds",
        facts={"duration_s": 8, BOUND_KEY: AT_MOST},
    )

    after = apply_updates(prior, bind_updates(prior, [update]), source_turn_id="t2")

    [live] = [req for req in after.live() if req.kind == "timing"]
    assert live.facts == {"duration_s": 8, BOUND_KEY: AT_MOST}


# --------------------------------------- the phone Talking receipt before a render (KRI-705)


def _phone_talking_receipt(req: BriefRequirement, **changes):
    facts = PlanFacts(edit_format="subtitled", phone_talking=True, **changes)
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt


def test_a_long_take_under_an_at_most_says_the_ceiling_in_the_format_limit_receipt():
    """The take (68.4 s) is longer than the ask: the honest "can't" names the ceiling."""
    receipt = _phone_talking_receipt(_at_most_ten(), take_duration_s=68.4)

    assert receipt.status == "partial"
    assert receipt.reason == (
        "A Talking edit keeps your whole take, so its length follows your clip; "
        "your take is 68.4s and you asked for at most 10s"
    )
    assert is_format_limit(receipt.reason)  # still a limit of the format, never the question
    assert not needs_creator_choice(receipt)


def test_guard_the_exact_version_of_that_receipt_is_unchanged():
    receipt = _phone_talking_receipt(
        _req("make it 10 seconds", duration_s=10), take_duration_s=68.4
    )

    assert receipt.reason == (
        "A Talking edit keeps your whole take, so its length follows your clip; "
        "your take is 68.4s and you asked for 10s"
    )


def test_that_receipt_is_worded_in_turkish_in_a_turkish_chat():
    with reply_language_for("tr"):
        receipt = _phone_talking_receipt(_at_most_ten(), take_duration_s=68.4)

    assert (receipt.reason or "").endswith("çekimin 68.4 saniye, sen en fazla 10 saniye istedin")


def test_a_take_inside_the_at_most_has_no_length_complaint():
    receipt = _phone_talking_receipt(_at_most_ten(), take_duration_s=7.5)

    assert "you asked for" not in (receipt.reason or "")


# ------------- end to end: the model's JSON -> the real parser -> the ledger -> the receipt
#
# No model call: these are the `brief_updates` the NEW prompt tells the model to write for each
# ceiling phrasing (tests/agents/test_duration_bound_prompt_kri847.py pins that instruction).
# They prove the field survives the real parse and merge path, so the gate can read it.

INCLUSIVE_PHRASINGS = [
    pytest.param("at most 10 seconds", id="at-most"),
    pytest.param("max 10 s", id="max"),
    pytest.param("no more than 10 seconds", id="no-more-than"),
    pytest.param("up to 10 seconds", id="up-to"),
    pytest.param("10 seconds or less", id="or-less"),
    pytest.param("en fazla 10 saniye", id="en-fazla"),
    pytest.param("10 saniyeyi geçmesin", id="gecmesin"),
]
EXCLUSIVE_PHRASINGS = [
    pytest.param("Under 10 seconds", id="under"),
    pytest.param("less than 10 seconds", id="less-than"),
    pytest.param("shorter than 10 seconds", id="shorter-than"),
    pytest.param("10 saniyenin altında", id="altinda"),
]


def _model_json(description: str, **facts) -> list[dict]:
    return [
        {
            "operation": "add",
            "kind": "timing",
            "scope": "global",
            "description": description,
            "facts": {"duration_s": 10, **facts},
        }
    ]


def _ledger(raw: list[dict]) -> CreativeBrief:
    updates = parse_brief_updates(raw)
    return apply_updates(None, bind_updates(None, updates), source_turn_id="turn-1")


@pytest.mark.parametrize("description", INCLUSIVE_PHRASINGS)
def test_a_modelled_inclusive_ceiling_reaches_the_receipt_as_a_ceiling(description):
    brief = _ledger(_model_json(description, duration_bound="at_most"))
    [req] = [r for r in brief.live() if r.kind == "timing"]

    assert req.facts == {"duration_s": 10, "duration_bound": "at_most"}
    assert check_requirement(req, _talking(7.5)).status == "met"
    assert check_requirement(req, _talking(10.0)).status == "met"
    # The boundary is the exact tolerance, 10 s + 10 %: 11.0 is met, 11.5 is the first miss.
    assert check_requirement(req, _talking(11.0)).status == "met"
    over = check_requirement(req, _talking(11.5))
    assert over.status == "partial"
    assert "This draft is about 11.5s" in (over.reason or "")
    assert "you asked for at most 10s" in (over.reason or "")
    with reply_language_for("tr"):
        assert "sen en fazla 10 saniye istedin" in (
            check_requirement(req, _talking(11.5)).reason or ""
        )


@pytest.mark.parametrize("description", EXCLUSIVE_PHRASINGS)
def test_a_modelled_exclusive_ceiling_is_judged_by_the_strict_cap_on_the_measured_export(
    description,
):
    brief = _ledger(_model_json(description, duration_bound="at_most"))
    [req] = [r for r in brief.live() if r.kind == "timing"]

    waiting = check_requirement(req, _talking(7.5))
    assert waiting.verification == "unchecked"  # not "you asked for 10s"
    assert check_requirement(req, _talking(7.5, measured=9.1)).status == "met"
    assert check_requirement(req, _talking(7.5, measured=11.0)).status == "partial"


@pytest.mark.parametrize("description", [*INCLUSIVE_PHRASINGS, *EXCLUSIVE_PHRASINGS])
def test_a_modelled_ceiling_is_rendered_back_to_the_model_on_the_next_turn(description):
    from app.kria.brief import brief_context

    brief = _ledger(_model_json(description, duration_bound="at_most"))

    shown = " ".join(batch.text for batch in brief_context(brief).batches)
    assert "duration_bound: at_most" in shown


def test_guard_the_same_json_without_the_fact_stays_the_old_exact_target():
    """A model that omits ``duration_bound`` (or an old row) is read exactly as before."""
    brief = _ledger(_model_json("at most 10 seconds"))
    [req] = [r for r in brief.live() if r.kind == "timing"]

    assert "duration_bound" not in req.facts
    assert check_requirement(req, _talking(7.5, outro_s=0.0)).status == "partial"
    assert check_requirement(req, _talking(11.0, outro_s=0.0)).status == "met"


def test_a_later_turn_changing_the_length_with_the_models_json_drops_the_ceiling():
    """Turn 1 "at most 10 seconds", turn 2 "make it 15 seconds": the model changes r1 with just
    ``{"duration_s": 15}``; the merged requirement must be an exact 15."""
    first = _ledger(_model_json("at most 10 seconds", duration_bound="at_most"))
    change = parse_brief_updates(
        [
            {
                "operation": "change",
                "target_requirement_id": "r1",
                "description": "Make it 15 seconds",
                "facts": {"duration_s": 15},
            }
        ]
    )

    second = apply_updates(first, bind_updates(first, change), source_turn_id="turn-2")

    [req] = [r for r in second.live() if r.kind == "timing"]
    assert req.facts == {"duration_s": 15}
    assert check_requirement(req, _talking(7.5, outro_s=0.0)).status == "partial"
