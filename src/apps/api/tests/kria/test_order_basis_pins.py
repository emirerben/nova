"""KRI-762: "this clip first, that clip last" is a clip pin, never an order-basis question.

Prod thread A9100235 (café M1, 2026-10-19): the creator asked for two pins ("Caro's tray clip
first; the clip where we wave 'bye bye' last"). The choice gate read the brief's key-less
`order` requirement as an ordering rule it cannot verify and asked "Klipler için belirli bir
sıra verdin… doğrulayamıyorum", RECOMMENDING upload order, which opens on the wrong clip. The
pins themselves are the clip-intent resolver's job (it resolves them or raises the clip picker).

Failure modes, written before the code (each is a test below):

* F1 a pin-only order requirement and nothing placed yet asks the basis question (the incident)
* F2 pins the server already placed ask nothing (existing behaviour, kept)
* F3 a real unverifiable rule ("clips 1, 2, 3 in that sequence") is STILL asked
* F4 a pin next to a real rule (a `by` fact, or a second requirement) does not hide the rule
* F5 the capture-time question with unplaced named pins recommends upload order
* F6 the same question with placed pins keeps recommending upload order (pins are seated on top)
* F7 no pins: the question is unchanged (upload order recommended, same options)
* F8 answer "unordered" erases the pin facts the server placed (unplaced pins: superseded as before)
* F9 answer "attachment order": the contract keeps the placed pins first / last
* F10 answer "unordered": the montage planner still seats the placed pins first / last
* F11 the Turkish connector "Maalesef" is followed by a lower-case reason
* F12 "you choose" under unplaced pins never picks upload order
* F13 pins nothing placed and no question: the render contract must name the clip it could not
  place (in the chat's language), not call it "an ordering rule I can't verify" (a second dead end)
* F14 the gate and the contract agree: no open question AND a specific, tappable-by-words reply
* F15 a key-less rule next to unplaced pins is still asked (the contract refuses it otherwise)
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.reply_language import reply_language_for
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent
from app.services import choice_questions as cq
from app.services.choice_questions import (
    ChoiceCapability,
    answered_brief,
    build_choice_question,
    choice_question_text,
    collect_conflicts,
    delegated_choice,
)
from app.services.creator_render_contract import build_render_contract
from app.tasks.kria_runtime import _unresolved_choice_plan
from tests.kria.test_choice_conflict_gate import (
    _asked,
    _brief,
    _gate,
    _order,
    _picked,
    _planned,
    _rows,
    _strategy,
)
from tests.pipeline.test_unified_montage import clip

FIRST_WORDS = "Caro'nun elinde siyah tepsiyle geldiği klip"
LAST_WORDS = 'el sallayıp "bye bye" dediğimiz klip'


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _pins(**facts: object) -> BriefRequirement:
    """The brief shape the Creator model writes for two pins and no basis (prod, KRI-677)."""
    return BriefRequirement(
        id="r3",
        kind="order",
        scope="global",
        description="Özel klip sırası",
        facts=facts or {"key": "narrative", "first_clip": FIRST_WORDS, "last_clip": LAST_WORDS},
    )


def _placed(position: str, media_id: str, *, status: str = "resolved") -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=f"order_{position}",
        op="order",
        attribute=FIRST_WORDS if position == "first" else LAST_WORDS,
        position=position,
        status=status,
        assignments=[ClipAssignment(media_id=media_id, evidence="x", confidence=0.9)]
        if status == "resolved" and media_id
        else [],
    )


def _conflicts(brief, rows, **strategy):
    return collect_conflicts(strategy, brief, {"clip_assignments": rows}, ChoiceCapability())


# ── F1 / F2: pins are not an order-basis question ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facts",
    [
        {"key": "narrative", "first_clip": FIRST_WORDS, "last_clip": LAST_WORDS},
        {"first_clip": FIRST_WORDS, "last_clip": LAST_WORDS},
        {"key": "custom", "last_clip": LAST_WORDS},
        {"key": "narrative", "first_clip": FIRST_WORDS},
    ],
    ids=["narrative", "keyless", "custom_last_only", "narrative_first_only"],
)
async def test_f1_two_pins_with_nothing_placed_ask_no_order_basis_question(
    monkeypatch, facts
) -> None:
    result = await _gate(monkeypatch, _planned(), rows=_rows(12), brief=_brief(_pins(**facts)))
    assert result.plan.mode == "act", result.plan.response


@pytest.mark.asyncio
async def test_f2_pins_the_server_placed_ask_nothing(monkeypatch) -> None:
    placed = [_placed("first", "c05"), _placed("last", "c02")]
    result = await _gate(
        monkeypatch, _planned(resolved=placed), rows=_rows(12), brief=_brief(_pins())
    )
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_f2b_pins_the_server_read_but_could_not_place_are_not_a_basis_question(
    monkeypatch,
) -> None:
    # "resolved" with no clip behind it: the draft says "I found no clips of ..." (KRI-458).
    # Upload order or no fixed order would not change what the resolver found.
    unplaced = [_placed("first", ""), _placed("last", "")]
    result = await _gate(
        monkeypatch, _planned(resolved=unplaced), rows=_rows(12), brief=_brief(_pins())
    )
    assert result.plan.mode == "act"


# ── F3 / F4: a real rule is still asked ───────────────────────────────────────


@pytest.mark.asyncio
async def test_f3_a_rule_the_contract_cannot_verify_is_still_asked(monkeypatch) -> None:
    rule = BriefRequirement(
        id="r2", kind="order", scope="global", description="clips 1, 2, 3 in that sequence"
    )
    result = await _gate(monkeypatch, _planned(), rows=_rows(8), brief=_brief(rule))
    assert result.plan.choice_question["kind"] == "order_basis"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requirements",
    [
        pytest.param(
            [
                BriefRequirement(
                    id="r2",
                    kind="order",
                    scope="global",
                    description="alphabetically",
                    facts={"by": "alphabetical", "first_clip": FIRST_WORDS},
                )
            ],
            id="pin_with_a_by_fact",
        ),
        pytest.param(
            [
                _pins(),
                BriefRequirement(
                    id="r4", kind="order", scope="global", description="in that sequence"
                ),
            ],
            id="pin_requirement_next_to_a_rule_requirement",
        ),
    ],
)
async def test_f4_a_pin_never_hides_a_real_rule(monkeypatch, requirements) -> None:
    result = await _gate(monkeypatch, _planned(), rows=_rows(8), brief=_brief(*requirements))
    assert result.plan.choice_question["kind"] == "order_basis"
    # The question covers the rule, never the pin requirement.
    found = _conflicts(_brief(*requirements), _rows(8))
    assert "r3" not in found[0].requirement_ids


# ── F5 / F6 / F7: never recommend upload order against a named first / last ────


def _options(found) -> dict[str, bool]:
    return {o.key: o.recommended for o in found[0].options}


def test_f5_named_pins_nothing_placed_do_not_recommend_upload_order() -> None:
    found = _conflicts(_brief(_order(), _pins()), _rows(12, dated=False))
    assert [c.kind for c in found] == ["order_basis"]
    assert _options(found) == {"attachment_order": False, "unordered": True}
    assert found[0].recommended().key == "unordered"


def test_f5b_one_pin_placed_one_not_still_does_not_recommend_upload_order() -> None:
    found = _conflicts(
        _brief(_order(), _pins()),
        _rows(12, dated=False),
        resolved_clip_intents=[_placed("last", "c02").model_dump(mode="json")],
    )
    assert _options(found)["attachment_order"] is False


def test_f6_placed_pins_keep_recommending_upload_order() -> None:
    # The planner seats placed pins on top of the basis, so upload order does not fight them.
    found = _conflicts(
        _brief(_order(), _pins()),
        _rows(12, dated=False),
        resolved_clip_intents=[
            _placed("first", "c05").model_dump(mode="json"),
            _placed("last", "c02").model_dump(mode="json"),
        ],
    )
    assert _options(found) == {"attachment_order": True, "unordered": False}


def test_f7_no_pins_the_question_is_unchanged() -> None:
    found = _conflicts(_brief(_order()), _rows(12, dated=False))
    assert _options(found) == {"attachment_order": True, "unordered": False}
    assert [o.key for o in found[0].options] == ["attachment_order", "unordered"]


def test_f12_you_choose_under_unplaced_pins_never_picks_upload_order() -> None:
    found = _conflicts(_brief(_order(), _pins()), _rows(12, dated=False))
    question = build_choice_question(found[0].candidate())
    assert delegated_choice(question, "you choose") == "unordered"
    assert delegated_choice(question, "sen seç") == "unordered"


# ── F8: the answer must not erase pins the server placed ──────────────────────


def _answer(brief, rows, option: str, **strategy):
    found = _conflicts(brief, rows, **strategy)
    assert found and found[0].kind == "order_basis"
    return {
        **strategy,
        "choice_answers": [
            {
                "conflict": "order_basis",
                "kind": "order_basis",
                "option": option,
                "input_digest": found[0].input_digest,
                "requirement_ids": list(found[0].requirement_ids),
                "source": "creator",
            }
        ],
    }


def test_f8_unordered_keeps_the_pins_the_server_placed_live() -> None:
    mixed = BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="chronological, first the tray clip, last the goodbye clip",
        facts={"key": "capture_time", "first_clip": FIRST_WORDS, "last_clip": LAST_WORDS},
    )
    brief, rows = _brief(mixed), _rows(12, dated=False)
    placed = [_placed("first", "c05").model_dump(mode="json")]
    strategy = _answer(brief, rows, "unordered", resolved_clip_intents=placed)
    kept = {r.id: r for r in answered_brief(brief, strategy).live()}
    assert kept["r2"].facts == {"first_clip": FIRST_WORDS, "last_clip": LAST_WORDS}


def test_f8b_unordered_with_no_placed_pin_still_clears_the_requirement() -> None:
    mixed = BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="chronological, first the tray clip",
        facts={"key": "capture_time", "first_clip": FIRST_WORDS},
    )
    brief, rows = _brief(mixed), _rows(12, dated=False)
    strategy = _answer(brief, rows, "unordered")
    assert answered_brief(brief, strategy).live() == []


# ── F9 / F10: pins stay pinned whatever basis is answered ─────────────────────


def _contract_ids(strategy: dict, brief, rows: list[dict]):
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(brief, strategy),
        media_snapshot={"clip_assignments": rows},
    )
    return contract


def test_f9_attachment_answer_keeps_placed_pins_first_and_last() -> None:
    mixed = BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="chronological, first the tray clip, last the goodbye clip",
        facts={"key": "capture_time", "first_clip": FIRST_WORDS, "last_clip": LAST_WORDS},
    )
    brief, rows = _brief(mixed), _rows(12, dated=False)
    placed = [
        _placed("first", "c05").model_dump(mode="json"),
        _placed("last", "c02").model_dump(mode="json"),
    ]
    strategy = {
        "edit_format": "montage",
        "direction": "guided_story",
        **_answer(brief, rows, "attachment_order", resolved_clip_intents=placed),
    }
    contract = _contract_ids(strategy, brief, rows)
    assert not contract.unresolved, contract.unresolved
    assert contract.order_basis == "attachment_order"
    assert contract.order_ids[0] == "c05" and contract.order_ids[-1] == "c02"
    assert len(contract.order_ids) == 12


def test_f10_unordered_answer_still_seats_placed_pins_in_the_montage() -> None:
    mixed = BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="chronological, first the tray clip, last the goodbye clip",
        facts={"key": "capture_time", "first_clip": FIRST_WORDS, "last_clip": LAST_WORDS},
    )
    clips = [clip(i, duration=4.0) for i in range(6)]
    placed = [
        _placed("first", "c4").model_dump(mode="json"),
        _placed("last", "c1").model_dump(mode="json"),
    ]
    brief = _brief(mixed)
    strategy = _answer(
        brief,
        [{"media_id": f"c{i}", "kind": "video"} for i in range(6)],
        "unordered",
        resolved_clip_intents=placed,
        edit_format="montage",
    )
    plan = plan_unified_montage(
        clips,
        brief_view(answered_brief(brief, strategy)),
        strategy=strategy,
        clip_intents_enabled=True,
    )
    assert plan.clip_ids[0] == "c4" and plan.clip_ids[-1] == "c1"


# ── F11: capitalisation after the Turkish connector ───────────────────────────


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Bu sırayı kliplerinle karşılaştırıp doğrulayamıyorum.", "bu sırayı"),
        ("Işık yetmiyor.", "ışık yetmiyor"),
        ("İlk klip eksik.", "ilk klip eksik"),
        ("41 klibin var.", "41 klibin var"),
        ("YouTube klibi yok.", "YouTube klibi yok"),
        ('"Bye bye" klibi yok.', '"Bye bye" klibi yok'),
    ],
)
def test_f11_turkish_reason_after_maalesef_is_lower_case(reason, expected) -> None:
    candidate = cq.ConflictCandidate(
        conflict_id="x",
        reason=reason,
        intro="Giriş.",
        options=(
            cq.ConflictOption(key="a", label="A", description="a"),
            cq.ConflictOption(key="b", label="B", description="b"),
        ),
        kind="order_basis",
        input_digest="d",
    )
    with reply_language_for("tr"):
        text = choice_question_text(candidate)
    assert f"Maalesef {expected}" in text


def test_f11b_the_incident_text_has_no_stray_capital() -> None:
    rule = BriefRequirement(
        id="r2", kind="order", scope="global", description="clips 1, 2, 3 in that sequence"
    )
    with reply_language_for("tr"):
        found = _conflicts(_brief(rule), _rows(8))
        text = choice_question_text(found[0].candidate())
    assert "Maalesef bu sırayı" in text and "Maalesef Bu" not in text


# ── the asked question's answer still resolves ────────────────────────────────


@pytest.mark.asyncio
async def test_a_basis_question_answered_with_upload_order_still_resolves(monkeypatch) -> None:
    rule = BriefRequirement(
        id="r2", kind="order", scope="global", description="clips 1, 2, 3 in that sequence"
    )
    rows = _rows(8)
    asked = _asked(await _gate(monkeypatch, _planned(), rows=rows, brief=_brief(rule)))
    strategy = _strategy(
        await _gate(
            monkeypatch,
            _planned(),
            rows=rows,
            brief=_brief(rule),
            events=(asked, _picked(asked[1]["choice_question"], "attachment_order")),
        )
    )
    assert strategy["choice_answers"][0]["option"] == "attachment_order"


# ── F13 / F14 / F15: no second dead end behind the missing question ───────────


def _contract(brief, rows, **strategy):
    return build_render_contract(
        {"edit_format": "montage", "direction": "guided_story", **strategy},
        generation_id="g",
        brief=brief,
        media_snapshot={"clip_assignments": rows},
    )


def test_f13_unplaced_pins_are_named_by_the_contract_not_filed_as_an_unverifiable_rule() -> None:
    contract = _contract(_brief(_pins()), _rows(12))
    assert list(contract.unresolved) == [
        f"I couldn't tell which clip \"{FIRST_WORDS}\" means, so I can't confirm the order.",
        f"I couldn't tell which clip \"{LAST_WORDS}\" means, so I can't confirm the order.",
    ]


def test_f13b_only_the_pin_the_server_could_not_place_is_named() -> None:
    contract = _contract(
        _brief(_pins()),
        _rows(12),
        resolved_clip_intents=[_placed("last", "c02").model_dump(mode="json")],
    )
    # One placed pin is the resolver's seating (KRI-510): nothing left to refuse here.
    assert not contract.unresolved


def test_f13c_a_pin_requirement_with_a_real_rule_keeps_the_generic_refusal() -> None:
    rule = BriefRequirement(
        id="r2", kind="order", scope="global", description="alphabetically", facts={"key": "abc"}
    )
    contract = _contract(_brief(rule), _rows(12))
    assert contract.unresolved[0] == "I can't verify this ordering rule from the approved media."


def test_f13d_the_named_clip_is_said_in_turkish_in_a_turkish_chat() -> None:
    with reply_language_for("tr"):
        contract = _contract(_brief(_pins(first_clip=FIRST_WORDS, key="narrative")), _rows(12))
    assert list(contract.unresolved) == [
        f'"{FIRST_WORDS}" derken hangi klibi kastettiğini anlayamadım, '
        "o yüzden sırayı doğrulayamıyorum."
    ]


def test_f14_gate_asks_nothing_and_the_creator_hears_which_clip_is_unclear() -> None:
    brief, rows = _brief(_pins()), _rows(12)
    strategy = {"edit_format": "montage", "direction": "guided_story"}
    with reply_language_for("tr"):
        assert cq.open_conflicts(strategy, brief, {"clip_assignments": rows}) == []
        blocked = _unresolved_choice_plan(
            strategy,
            brief,
            {"clip_assignments": rows},
            contract_brief=brief,
            has_draft=False,
        )
    assert blocked is not None
    plan, _reason = blocked
    assert plan.choice_question is None
    assert f'"{FIRST_WORDS}"' in plan.response and "sırayı doğrulayamıyorum" in plan.response
    assert "Klipleri eklediğin sırayı kullan" not in plan.response


def test_f15_a_keyless_rule_next_to_unplaced_pins_is_still_asked() -> None:
    rule = BriefRequirement(
        id="r4", kind="order", scope="global", description="in that sequence", facts={}
    )
    unplaced = [_placed("first", ""), _placed("last", "")]
    found = _conflicts(
        _brief(rule, _pins()),
        _rows(12),
        resolved_clip_intents=[i.model_dump(mode="json") for i in unplaced],
    )
    assert [c.kind for c in found] == ["order_basis"]
    assert found[0].requirement_ids == ("r4",)
