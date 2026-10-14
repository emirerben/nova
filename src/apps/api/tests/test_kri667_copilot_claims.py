"""KRI-667: the copilot honesty guard reads the model's structured claim.

The model states in ``claimed_ops`` which ops its reply calls done; parse restates it
whenever the server writes the reply itself; ``_honest_outcome`` compares it with the
ops that survived. The EN/TR wording regexes are only the fallback when the field is
missing. Failure modes pinned here:

- a missing or malformed field silently trusted (a claim slips through as "no claim");
- an honest reply whose wording sounds like a claim still swapped for a canned line;
- a claim the wording check cannot read ("New hook is in") left on a proposal;
- server-written replies judged by the model's claim about text it never saw: the
  Turkish partial-bundle limitation was dropped, a no-op reorder reply let through;
- a claimed op dropped by the parser while a sibling survives, with no reason given,
  or with parser jargon ("operation values are invalid") shown to the creator;
- a renamed op (set_title -> edit_text) counted as an unbacked claim;
- the canned line no longer first, so the phone planner cannot swap it;
- the retry prompt and the contract not asking for the field.
"""

from __future__ import annotations

import json

import pytest
from structlog.testing import capture_logs

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput, EditCopilotOutput
from app.kria.planner import _PHONE_STAGED_REPLY, _PHONE_STAGED_REPLY_TR, _phone_editor_reply
from app.kria.reply_language import reply_language_for
from app.routes._copilot import _honest_outcome
from tests.services.test_kria_capture_time_ops import (  # noqa: F401
    ASK_DESCRIBE,
    _add,
    _facts,
    _snapshot,
    guided,
)
from tests.test_edit_copilot import _full_snapshot
from tests.test_edit_copilot import _snapshot as _basic_snapshot

CANNED_EN = "I prepared this edit for the editor to validate and stage."
CANNED_TR = "Bu düzenlemeyi hazırladım, editör kontrol edip uygulayacak."
_UNSET = object()


def _parse(
    reply: str,
    ops: list[dict],
    *,
    claimed: object = _UNSET,
    snapshot: dict | None = None,
    intent: str = "edit",
    utterance: str = "x",
    **extra: object,
) -> EditCopilotOutput:
    payload: dict = {"intent": intent, "ops": ops, "confidence": 0.9, "reply": reply, **extra}
    if claimed is not _UNSET:
        payload["claimed_ops"] = claimed
    return EditCopilotAgent(ModelClient()).parse(
        json.dumps(payload),
        EditCopilotInput(utterance=utterance, variant_snapshot=snapshot or _basic_snapshot()),
    )


def _route(output: EditCopilotOutput, message: str = "") -> tuple[str, str]:
    """What `run_copilot_turn` does: ops ride only genuine edit turns."""
    ops = [] if (output.needs_clarification or output.intent != "edit") else output.ops
    return _honest_outcome(output, ops, supports_proposed=True, message=message)


EDIT_HOOK = {"op": "edit_text", "bar_index": 0, "text": "new hook"}


# ── fallback: no structured claim ─────────────────────────────────────────────


def test_missing_field_falls_back_to_the_wording_check() -> None:
    claim = _parse("Kancayı değiştirdim.", [EDIT_HOOK])
    honest = _parse("Daha merak uyandıran bir kanca iyi olur.", [EDIT_HOOK])
    assert claim.claimed_ops is None and honest.claimed_ops is None
    with reply_language_for("tr"):
        assert _route(claim) == ("proposed", CANNED_TR)
        assert _route(honest) == ("proposed", "Daha merak uyandıran bir kanca iyi olur.")
    assert _route(_parse("Changed the hook.", [EDIT_HOOK])) == ("proposed", CANNED_EN)


@pytest.mark.parametrize(
    "malformed",
    ["edit_text", {"op": "edit_text"}, [1], ["edit_text", None], None, 0],
)
def test_malformed_field_is_treated_as_missing(malformed: object) -> None:
    # A half-read field must never count as "claims nothing": the wording check runs.
    output = _parse("Başlığı büyüttüm.", [EDIT_HOOK], claimed=malformed)
    assert output.claimed_ops is None
    with reply_language_for("tr"):
        assert _route(output) == ("proposed", CANNED_TR)


def test_claim_list_is_bounded_trimmed_and_deduplicated() -> None:
    output = _parse("Done.", [EDIT_HOOK], claimed=[" edit_text ", "EDIT_TEXT", "", *["x"] * 99])
    assert output.claimed_ops == ["edit_text", "x"]


# ── structured claim, proposed path ───────────────────────────────────────────


def test_claim_the_wording_check_misses_still_gets_the_canned_line() -> None:
    # Neither reply has an edit-verb form the regexes know; the model says it is done.
    tr = _parse("Yeni kanca hazır: 'new hook'.", [EDIT_HOOK], claimed=["edit_text"])
    en = _parse("New hook is in: 'new hook'.", [EDIT_HOOK], claimed=["edit_text"])
    with reply_language_for("tr"):
        assert _route(tr) == ("proposed", CANNED_TR)
    assert _route(en) == ("proposed", CANNED_EN)


@pytest.mark.parametrize(
    ("language", "reply"),
    [
        ("tr", "Başlığı büyüttüm."),
        ("tr", "Daha sıkı bir açılış hazırladım."),
        ("en", "Made the title bigger."),
        (None, "Trimmed openings like this keep viewers watching."),
    ],
)
def test_empty_claim_keeps_wording_that_sounds_like_a_claim(language, reply) -> None:
    output = _parse(reply, [EDIT_HOOK], claimed=[])
    with reply_language_for(language):
        assert _route(output) == ("proposed", reply)


def test_claimed_op_dropped_while_a_sibling_survives_names_the_reason() -> None:
    # swap_music is outside the allowed families: the parser rejects it, edit_text survives.
    snapshot = _basic_snapshot(allowed=["text"])
    raw = dict(
        ops=[EDIT_HOOK, {"op": "swap_music", "track_id": "t1"}],
        claimed=["edit_text", "swap_music"],
        snapshot=snapshot,
    )
    english = _parse("New hook, new song.", **raw)
    assert [op["op"] for op in english.ops] == ["edit_text"]
    with capture_logs() as logs:
        assert _route(english) == ("proposed", f"{CANNED_EN} I can't change that on this edit yet.")
    check = next(e for e in logs if e["event"] == "edit_copilot.claim_check")
    assert check["claimed"] == ["edit_text", "swap_music"]
    assert check["kept"] == ["edit_text"]
    assert check["unbacked"] == ["swap_music"]
    with reply_language_for("tr"):
        turkish = _parse("Yeni kanca, yeni şarkı.", **raw)
        outcome, reply = _route(turkish)
        assert (outcome, reply) == (
            "proposed",
            f"{CANNED_TR} Bunu bu düzenlemede henüz değiştiremiyorum.",
        )
        # The canned line stays first, so the phone planner still swaps it.
        assert _phone_editor_reply(reply) == (
            f"{_PHONE_STAGED_REPLY_TR} Bunu bu düzenlemede henüz değiştiremiyorum."
        )
    assert _phone_editor_reply(_route(english)[1]) == (
        f"{_PHONE_STAGED_REPLY} I can't change that on this edit yet."
    )


@pytest.mark.parametrize(
    "reason",
    [
        {"op": "set_clip_duration", "reason": "invalid_value", "detail": "values are invalid"},
        {"op": "set_clip_duration", "reason": "missing_required", "detail": "missing: bar_index"},
    ],
)
def test_unbacked_claim_never_shows_parser_jargon(reason) -> None:
    output = EditCopilotOutput(
        intent="edit",
        ops=[EDIT_HOOK],
        reply="Retitled and retimed.",
        rejection_reasons=[reason],
        claimed_ops=["edit_text", "set_clip_duration"],
    )
    assert _route(output) == ("proposed", CANNED_EN)


def test_renamed_op_claim_matches_the_op_it_became() -> None:
    snapshot = _basic_snapshot(allowed=["text"])
    snapshot["text_bars"][0]["id"] = "guided-title"
    output = _parse(
        "Title is 'new hook' now.",
        [{"op": "set_title", "title": "new hook"}],
        claimed=["set_title"],
        snapshot=snapshot,
    )
    assert [op["op"] for op in output.ops] == ["edit_text"]
    assert output.claimed_ops == ["edit_text"]
    with capture_logs() as logs:
        assert _route(output) == ("proposed", CANNED_EN)
    check = next(e for e in logs if e["event"] == "edit_copilot.claim_check")
    assert check["unbacked"] == []


# ── structured claim, every other outcome branch ──────────────────────────────


def _out(claimed: list[str], reply: str, **kwargs) -> EditCopilotOutput:
    return EditCopilotOutput(
        **{"intent": "edit", "ops": [], "reply": reply, "claimed_ops": claimed, **kwargs}
    )


@pytest.mark.parametrize(
    ("output", "en", "tr"),
    [
        # no op survived: a claim is replaced, exactly as the wording check did
        (
            _out(["patch_text_style"], "Yeni görünüm hazır."),
            "That change is already reflected in the draft.",
            "Bu değişiklik taslakta zaten var.",
        ),
        (
            _out(
                ["patch_text_style"],
                "Yeni görünüm hazır.",
                unmet_requests=[{"request": "bigger", "reason": "No text bar matched."}],
            ),
            "No text bar matched.",
            "No text bar matched.",
        ),
        (
            _out(
                ["patch_text_style"],
                "Yeni görünüm hazır.",
                rejection_reasons=[{"op": "patch_text_style", "reason": "no_op", "detail": ""}],
            ),
            "I couldn't make that change on this edit.",
            "Bu değişikliği bu düzenlemede yapamadım.",
        ),
        (
            _out(["edit_text"], "Yeni kanca hazır. Hangisi?", intent="clarify", ops=[EDIT_HOOK]),
            "I need one detail before changing the draft.",
            "Taslağı değiştirmeden önce bir ayrıntıya ihtiyacım var.",
        ),
        (
            _out(["swap_music"], "Yeni şarkı hazır.", intent="reject"),
            "That kind of edit isn't available for this draft yet.",
            "Bu tür bir düzenleme bu taslakta henüz yapılamıyor.",
        ),
    ],
)
def test_claim_without_a_surviving_op_is_replaced(output, en, tr) -> None:
    assert _route(output)[1] == en
    with reply_language_for("tr"):
        assert _route(output)[1] == tr


@pytest.mark.parametrize(
    ("output", "outcome"),
    [
        # Each would trip the wording check; the structured "nothing done" keeps it.
        (_out([], "Başlık bir önceki adımda büyütüldü; şu an olduğu gibi kalıyor."), "no_effect"),
        (_out([], "Changing the song needs a fresh edit.", intent="reject"), "unsupported"),
        (_out([], "Shall I make it bigger, or keep it as I set it?", intent="clarify"), None),
    ],
)
def test_empty_claim_keeps_an_honest_reply_on_every_branch(output, outcome) -> None:
    expected = outcome or "clarification"
    assert _route(output) == (expected, output.reply)


# ── server-written replies restate the claim ──────────────────────────────────


@pytest.mark.parametrize("claimed", [_UNSET, ["patch_text_style"], []])
def test_turkish_partial_bundle_limitation_survives_the_proposed_path(claimed) -> None:
    snapshot = {
        "editor_ops_version": 2,
        "allowed_op_families": ["text"],
        "total_duration_s": 5,
        "text_bars": [{"id": "title", "text": "Merhaba", "start_s": 0, "end_s": 5}],
    }
    unmet = [{"request": "Konuşmamı değiştir", "reason": "Ses değiştirme desteklenmiyor."}]
    style = {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FFFF00"}}
    with reply_language_for("tr"):
        output = _parse(
            "Hepsini yaptım.", [style], claimed=claimed, snapshot=snapshot, unmet_requests=unmet
        )
        assert output.claimed_ops == []
        outcome, reply = _route(output)
        assert (outcome, reply) == (
            "proposed",
            "Desteklenen değişiklikleri hazırladım. Ses değiştirme desteklenmiyor.",
        )
        assert _phone_editor_reply(reply) == reply
    english = _parse("All done.", [style], claimed=claimed, snapshot=snapshot, unmet_requests=unmet)
    assert _route(english) == (
        "proposed",
        "I prepared the supported changes. Ses değiştirme desteklenmiyor.",
    )


def test_noop_reorder_server_reply_still_counts_as_a_claim(guided) -> None:  # noqa: F811
    job, variant, _rev = guided
    facts = {f"m{i}": _facts(f"2026-09-20T0{i}:00:00Z") for i in range(4)}
    ops = [
        {"op": "reorder_clips_by", "criterion": "capture_time"},
        {
            "op": "label_each_clip",
            "source": "facts",
            "label_from": "capture_time",
            "mode": "append",
        },
    ]
    # The model even says it claimed nothing; the server's "Added the filming hour…"
    # reply does claim the surviving op, so a proposal still gets the prepared line.
    output = _parse("Hazır.", ops, claimed=[], snapshot=_snapshot(job, variant, facts))
    assert [op["op"] for op in output.ops] == ["label_each_clip"]
    assert output.reply.startswith("Added the filming hour to")
    assert output.claimed_ops == ["label_each_clip"]
    outcome, reply = _route(output)
    assert outcome == "proposed"
    assert reply.startswith(CANNED_EN) and "already in chronological order" in reply


def test_server_written_no_change_replies_claim_nothing() -> None:
    # An empty model reply becomes the server's "Got it" line, whatever the model claimed.
    empty = _parse("", [], claimed=["edit_text"], intent="describe")
    assert empty.claimed_ops == []
    assert _route(empty) == ("no_effect", "Got it. What else should we change?")
    # A same-value trim: the server's "already starts at…" line replaces the model's
    # claim, so the explanation survives even though the model said it trimmed.
    snapshot = _full_snapshot()
    snapshot["slots"][0]["output_start_s"] = 1.0
    with reply_language_for("tr"):
        same = _parse(
            "Başlangıcı kırptım.",
            [{"op": "trim_output_start", "start_s": 1}],
            claimed=["trim_output_start"],
            snapshot=snapshot,
        )
        assert same.ops == [] and same.claimed_ops == []
        assert _route(same) == (
            "no_effect",
            "Taslak zaten 1 saniyede başlıyor, o yüzden bir şey değiştirmedim.",
        )


def test_notes_only_reply_is_judged_by_the_notes_not_the_models_claim(guided) -> None:  # noqa: F811
    # No clip was seen, so the caption is dropped and the server's note replaces the
    # model's reply. The model's claim was about its own discarded text: it must not
    # turn the honest note into "That change is already reflected in the draft."
    job, variant, _rev = guided
    output = _parse(
        "Added a caption to each clip.",
        [_add("Airport pickup", 0, 2)],
        claimed=["add_text"],
        snapshot=_snapshot(job, variant),
        utterance=ASK_DESCRIBE,
    )
    assert output.ops == [] and output.claimed_ops is None
    assert _route(output) == ("no_effect", output.reply)
    assert "can't tell what those clips show" in output.reply


# ── contract ──────────────────────────────────────────────────────────────────


def test_prompt_and_retry_ask_for_the_field() -> None:
    agent = EditCopilotAgent(ModelClient())
    prompt = agent.render_prompt(EditCopilotInput(utterance="x", variant_snapshot={}))
    assert '"claimed_ops": []' in prompt
    assert "claimed_ops" in agent.schema_clarification()
