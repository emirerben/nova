"""KRI-1561 PR 3: chosen sound effects for a phone Talking / Voiceover edit.

Failure modes, written before the tests:

1. A closed gate or missing speech still calls the model or the database.
2. A chosen effect lands within the spacing of a named cue, or on a time no word or pause owns.
3. An unplayable or wrong-prefixed catalogue row is offered to the agent, then dropped later.
4. The agent failing (schema, timeout) fails the render instead of leaving a receipt.
5. A receipt says "placed" for a row that was dropped, or loses the reason when none are left.
6. A blank or malformed ask is read as a request.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.sfx_intent import SfxAskIntent
from app.agents.sfx_placement import RawSfxSuggestion, SfxPlacementInput, SfxPlacementOutput
from app.services.phone_sfx_choice import (
    choose_for_phone,
    demote_receipt,
    read_sfx_ask,
)
from app.services.sfx_autoplace import MIN_SPACING_S
from app.services.sfx_chooser import SfxLimit

ASK = SfxAskIntent(request_text="Uygun yerlere ses efekti ekle")
WORDS = [{"text": f"w{i}", "start_s": float(i * 2), "end_s": i * 2 + 0.4} for i in range(0, 8)]
CATALOG = [
    {"id": "fx-pop", "name": "Pop", "audio_gcs_path": "sound-effects/fx-pop/a.wav"},
    {"id": "fx-bad", "name": "Odd", "audio_gcs_path": "sound-effects/fx-bad/a.ogg"},
    {"id": "fx-off", "name": "Off", "audio_gcs_path": "sound-effects/other/a.wav"},
]


class Spy:
    def __init__(self, *picks: tuple[str, float]) -> None:
        self.picks = picks
        self.payloads: list[SfxPlacementInput] = []
        self.catalog_calls = 0

    def catalog(self) -> list[dict]:
        self.catalog_calls += 1
        return CATALOG

    def run(self, payload: SfxPlacementInput) -> SfxPlacementOutput:
        self.payloads.append(payload)
        return SfxPlacementOutput(
            placements=[
                RawSfxSuggestion(effect_id=e, at_s=t, anchor="word_start") for e, t in self.picks
            ]
        )


def _choose(spy: Spy, **kw):
    args = {"limit": None, "words": WORDS, "duration_s": 20.0, "taken_at_s": ()}
    args.update(kw)
    return choose_for_phone(ASK, catalog_loader=spy.catalog, run_agent=spy.run, **args)


def test_a_closed_gate_or_no_speech_never_touches_the_catalog_or_the_model() -> None:
    spy = Spy(("fx-pop", 2.0))
    gated = _choose(spy, limit=SfxLimit.flag_off)
    assert gated.effects == () and gated.receipt["declined"] == "flag_off"
    silent = _choose(spy, words=[])
    assert silent.receipt["declined"] == "no_timed_evidence"
    assert spy.catalog_calls == 0 and spy.payloads == []


def test_only_playable_correctly_prefixed_rows_reach_the_agent() -> None:
    spy = Spy(("fx-pop", 2.0))
    _choose(spy)
    (payload,) = spy.payloads
    assert [e.effect_id for e in payload.effects] == ["fx-pop"]
    assert payload.request_text == ASK.request_text


def test_chosen_rows_sit_on_words_and_never_near_a_named_cue() -> None:
    spy = Spy(("fx-pop", 2.0), ("fx-pop", 4.0), ("fx-pop", 8.0))
    out = _choose(spy, taken_at_s=[4.5])
    times = [row.at_s for row in out.effects]
    assert times == [2.0, 8.0]
    assert all(abs(t - 4.5) >= MIN_SPACING_S for t in times)
    assert [row.id for row in out.effects] == ["ask-sfx-0", "ask-sfx-1"]
    assert out.receipt["declined"] is None
    assert [row["effect_id"] for row in out.receipt["placed"]] == ["fx-pop", "fx-pop"]


def test_everything_taken_by_named_cues_is_nothing_fit() -> None:
    out = _choose(Spy(("fx-pop", 2.0)), taken_at_s=[2.2])
    assert out.effects == () and out.receipt["declined"] == "nothing_fit"


def test_an_agent_failure_is_a_receipt_not_a_failed_render() -> None:
    def boom(_payload: SfxPlacementInput) -> SfxPlacementOutput:
        raise RuntimeError("model timed out")

    out = choose_for_phone(
        ASK,
        limit=None,
        words=WORDS,
        duration_s=20.0,
        catalog_loader=lambda: CATALOG,
        run_agent=boom,
    )
    assert out.effects == ()
    assert out.receipt["declined"] == "nothing_fit" and "timed out" in out.receipt["error"]


def test_a_database_error_is_not_swallowed() -> None:
    def down() -> list[dict]:
        raise ConnectionError("db down")

    with pytest.raises(ConnectionError):
        choose_for_phone(
            ASK,
            limit=None,
            words=WORDS,
            duration_s=20.0,
            catalog_loader=down,
            run_agent=Spy().run,
        )


def test_a_demoted_receipt_drops_the_rows_and_keeps_a_reason() -> None:
    out = _choose(Spy(("fx-pop", 2.0), ("fx-pop", 6.0)))
    half = demote_receipt(out.receipt, ["ask-sfx-0"])
    assert [row["id"] for row in half["placed"]] == ["ask-sfx-1"] and half["declined"] is None
    none_left = demote_receipt(half, ["ask-sfx-1"])
    assert none_left["placed"] == [] and none_left["declined"] == "nothing_fit"
    assert len(out.receipt["placed"]) == 2, "the original receipt is not mutated"


@pytest.mark.parametrize(
    "raw", [None, {}, {"request_text": "   "}, {"request_text": "x", "extra": 1}]
)
def test_a_blank_or_malformed_ask_is_no_ask(raw) -> None:
    assert read_sfx_ask({"sfx_ask": raw}) is None


def test_a_valid_ask_is_read_from_the_strategy_dict() -> None:
    ask = read_sfx_ask({"sfx_ask": {"request_text": " add effects ", "max_placements": 3}})
    assert ask is not None and ask.request_text == "add effects" and ask.max_placements == 3
    assert read_sfx_ask(None) is None
