"""Failure modes of the "with animations" text ask (prompt fidelity, animation unit).

Written from the ways the feature could still hurt a creator, not from the implementation:
a phone that cannot play the entrance must never get a recipe it refuses; a row the composer or
creator already animated, a pinned corner text and the caption lane must not be touched; a
negated or quoted "animated" is not an ask; and none of it may hold approval back. The Assos
scorecard (`test_assos_request_to_recipe.py`, check 1) is the end-to-end proof that the ask
reaches the recipe.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.animation_ask import animation_ask, text_look_for
from app.kria.brief import BriefRequirement
from app.schemas.text_composition import TextLook
from app.services.creation_text_composition import apply_text_look, unanimated_look_rows


def _req(description: str = "", *, scope: str = "global", facts: dict | None = None, **kw):
    return BriefRequirement(
        id="r1", kind="text", scope=scope, description=description, facts=facts or {}, **kw
    )


@pytest.mark.parametrize(
    ("req", "expected"),
    [
        (_req("animated text", facts={"animation": "any"}), ("unnamed", None)),
        (_req("fade in the captions", facts={"animation": "fade"}), ("named", "fade")),
        (_req("t", facts={"animation": "Typewriter"}), ("named", "typewriter")),
        (_req("t", facts={"animation": "daktilo"}), ("named", "typewriter")),
        (_req("t", facts={"animation": True}), ("unnamed", None)),
        # an id the registry does not know is still an ask for SOME animation
        (_req("t", facts={"animation": "wobble"}), ("unnamed", None)),
        # not asks
        (_req("t", facts={"animation": "none"}), None),
        (_req("t", facts={"animation": "Static"}), None),
        (_req("t", facts={"animation": ""}), None),
        # structured only (KRI-667): the server never guesses from the creator's words
        (_req("animated on-screen text explaining why to visit"), None),
        (_req("explain why people should visit Assos"), None),
    ],
)
def test_only_a_recorded_animation_fact_is_an_ask(req, expected) -> None:
    ask = animation_ask(req)
    if expected is None:
        assert ask is None
    else:
        assert (ask is not None and ask.named, ask and ask.edge) == (
            expected[0] == "named",
            expected[1],
        )


def test_a_style_or_dead_requirement_is_not_a_text_animation_ask() -> None:
    style = BriefRequirement(
        id="r2", kind="style", scope="global", description="x", facts={"animation": "fade"}
    )
    assert animation_ask(style) is None


def _rows() -> list[dict]:
    def row(rid: str, **kw) -> dict:
        return {
            "id": rid,
            "text": f"text {rid}",
            "start_s": 1.0,
            "end_s": 3.0,
            "role": "generative_intro",
            "effect": "static",
            "animation_phases": None,
            **kw,
        }

    return [
        row("guided-title"),
        row("kria-explain-1"),
        row("kria-explain-2", animation_phases={"entrance": "pop", "exit": "none", "loop": "none"}),
        row("guided-pinned-0"),
        row("kria-explain-3", effect="typewriter"),
        row("narration-caption-1", role="caption"),
    ]


def _look(form: str = "phases", target: str = "all", entrance: str = "fade") -> TextLook:
    return TextLook.model_validate(
        {"form": form, "entries": [{"target": target, "entrance": entrance}]}
    )


def test_a_look_never_overwrites_pins_captions_or_rows_that_already_enter() -> None:
    out = {r["id"]: r for r in apply_text_look(_rows(), _look())}
    assert out["guided-title"]["animation_phases"]["entrance"] == "fade"
    assert out["kria-explain-1"]["animation_phases"]["entrance"] == "fade"
    assert out["kria-explain-2"]["animation_phases"]["entrance"] == "pop"  # composer's own
    assert out["kria-explain-3"]["animation_phases"] is None  # legacy effect kept
    assert out["guided-pinned-0"]["animation_phases"] is None  # the creator's pinned corner text
    assert out["narration-caption-1"]["animation_phases"] is None  # caption lane: own panel
    assert unanimated_look_rows(list(out.values()), _look()) == 0


def test_the_creators_named_edge_wins_and_the_specific_target_keeps_its_own() -> None:
    look = TextLook.model_validate(
        {
            "form": "phases",
            "entries": [
                {"target": "title", "entrance": "typewriter", "named": True},
                {"target": "all", "entrance": "fade"},
            ],
        }
    )
    out = {r["id"]: r for r in apply_text_look(_rows(), look)}
    assert out["guided-title"]["animation_phases"]["entrance"] == "typewriter"
    assert out["kria-explain-1"]["animation_phases"]["entrance"] == "fade"


def test_no_look_is_byte_identical() -> None:
    rows = _rows()
    assert apply_text_look(rows, None) is rows


def test_a_phone_without_authored_text_gets_the_legacy_effect_not_a_refused_recipe(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "phone_render_verified_features", ["animatedText"])
    look = TextLook.model_validate(
        text_look_for([_req("animated text", facts={"animation": "any"})])
    )
    assert look.form == "effect"
    out = apply_text_look(_rows(), look)
    assert all(r["animation_phases"] is None for r in out if r["id"] == "kria-explain-1")
    assert next(r for r in out if r["id"] == "kria-explain-1")["effect"] == "fade-in"


def test_a_phone_that_plays_no_entrance_emits_nothing_and_says_so(monkeypatch) -> None:
    monkeypatch.setattr(settings, "phone_render_verified_features", ["positionedText"])
    assert text_look_for([_req("animated text", facts={"animation": "any"})]) is None
    receipt = bc._animation_plan_receipt(_req("animated text", facts={"animation": "any"}))
    # A typed decline the creator reads before approval: unchecked, so it never blocks.
    assert (receipt.status, receipt.verification) == ("not_possible", "unchecked")
    assert "animat" in (receipt.reason or "")


def test_kill_switch_off_reads_no_ask(monkeypatch) -> None:
    monkeypatch.setattr(settings, "text_animation_ask_enabled", False)
    assert bc.asks_text_animation(_req("animated text", facts={"animation": "any"})) is False


def _prepared_research(req_id: str = "r1") -> dict:
    return {
        "version": 1,
        "status": "ready",
        "requirement_id": req_id,
        "topic": "why visit Assos",
        "request_quote": "explain why people should visit Assos",
        "basis_digest": "0" * 64,
        "texts": [
            {
                "text": "Assos has a sunken Aegean harbour",
                "media_ids": ["m1"],
                "start_s": 0.0,
                "end_s": 2.0,
                "sources": [{"title": "t", "url": "https://example.com/a"}],
                "facts": [
                    {
                        "id": "fact-0",
                        "text": "Assos has a harbour.",
                        "sources": [{"title": "t", "url": "https://example.com/a"}],
                    }
                ],
            }
        ],
    }


def _research_plus_animation_req():
    return _req(
        "with animations, explain why people should visit Assos",
        facts={
            "animation": "any",
            "research": {
                "topic": "why visit Assos",
                "request_quote": "explain why people should visit Assos",
            },
        },
    )


def test_research_plus_animation_never_reads_checked_before_the_animation_is_seen(
    monkeypatch,
) -> None:
    """The combined Assos ask: research met must not stand for an unchecked animation."""
    req = _research_plus_animation_req()
    facts = bc.PlanFacts(research=_prepared_research(), phone_text=True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["authoredText"])
    receipt = bc._check_requirement(req, facts)
    assert (receipt.status, receipt.verification) == ("met", "unchecked")
    # kill switch off: the research verdict stands alone, byte-identical to before
    monkeypatch.setattr(settings, "text_animation_ask_enabled", False)
    off = bc._check_requirement(req, facts)
    assert (off.status, off.verification) == ("met", "checked")


def test_research_plus_animation_on_a_phone_that_cannot_animate_says_so(monkeypatch) -> None:
    req = _research_plus_animation_req()
    monkeypatch.setattr(settings, "phone_render_verified_features", ["positionedText"])
    receipt = bc._check_requirement(
        req, bc.PlanFacts(research=_prepared_research(), phone_text=True)
    )
    assert receipt.status == "not_possible" and "animat" in (receipt.reason or "")


def test_a_literal_global_text_position_never_moves_the_labels() -> None:
    from app.kria.animation_ask import requested_text_position

    literal = _req("Assos 2026", literal="Assos 2026", facts={"position": "bottom_right"})
    assert requested_text_position([literal]) is None
    free = _req("labels bottom right", facts={"position": "bottom_right"})
    assert requested_text_position([free]) == "bottom_right"
