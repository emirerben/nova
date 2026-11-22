"""KRI-1561: one chooser. Failure modes first.

1. Beats with no speech and no visual moments must decline, never place evenly.
2. Every placement sits on a supplied word, pause or moment mark, spaced and capped.
3. An empty or voice-only catalogue declines with its own reason.
4. The agent's invented times are dropped, and "nothing fit" is its own decline.
5. The request text and beats reach the agent; absent, the prompt is the pre-v3 text.
"""

from __future__ import annotations

from app.agents.sfx_placement import RawSfxSuggestion, SfxPlacementInput, SfxPlacementOutput
from app.services.sfx_autoplace import MARK_EPSILON_S, MIN_SPACING_S
from app.services.sfx_chooser import (
    Declined,
    Placed,
    SfxAsk,
    SfxEvidence,
    SfxLimit,
    choose_sfx,
)

CATALOG = [
    {"id": "fx_whoosh", "name": "Whoosh", "audio_gcs_path": "s/a.wav", "role_tags": ["motion"]},
    {"id": "fx_click", "name": "Click", "audio_gcs_path": "s/b.wav", "role_tags": ["ui"]},
]
WORDS = tuple({"word": f"w{i}", "start_s": float(i), "end_s": i + 0.4} for i in range(0, 20, 2))
ASK = SfxAsk(request_text="Uygun yerlere ses efekti ekle")


def _agent(*raw: tuple[str, float]):
    seen: list[SfxPlacementInput] = []

    def run(payload: SfxPlacementInput) -> SfxPlacementOutput:
        seen.append(payload)
        return SfxPlacementOutput(
            placements=[RawSfxSuggestion(effect_id=e, at_s=t, anchor="word_start") for e, t in raw]
        )

    run.seen = seen  # type: ignore[attr-defined]
    return run


def test_beats_alone_decline_and_never_call_the_agent() -> None:
    agent = _agent(("fx_click", 2.0))
    beats = tuple(i * 0.5 for i in range(40))
    out = choose_sfx(ASK, SfxEvidence(beats_s=beats, duration_s=20.0), CATALOG, run_agent=agent)
    assert out == Declined(SfxLimit.no_timed_evidence)
    assert agent.seen == []


def test_empty_and_voice_only_catalogs_decline() -> None:
    agent = _agent(("fx_click", 2.0))
    ev = SfxEvidence(words=WORDS, duration_s=20.0)
    assert choose_sfx(ASK, ev, [], run_agent=agent) == Declined(SfxLimit.empty_catalog)
    voice = [{**CATALOG[0], "contains_voice": True}]
    assert choose_sfx(ASK, ev, voice, run_agent=agent) == Declined(SfxLimit.empty_catalog)
    no_audio = [{**CATALOG[0], "audio_gcs_path": None}]
    assert choose_sfx(ASK, ev, no_audio, run_agent=agent) == Declined(SfxLimit.empty_catalog)
    assert agent.seen == []


def test_placements_land_on_marks_and_are_spaced() -> None:
    raw = [("fx_click", t) for t in (0.0, 0.4, 2.0, 2.4, 4.0, 4.4)]
    ev = SfxEvidence(words=WORDS, duration_s=20.0)
    out = choose_sfx(ASK, ev, CATALOG, run_agent=_agent(*raw))
    assert isinstance(out, Placed)
    times = [p.at_s for p in out.placements]
    assert times == [0.0, 2.0, 4.0]
    assert all(b - a >= MIN_SPACING_S for a, b in zip(times, times[1:], strict=False))
    marks = {float(w["start_s"]) for w in WORDS} | {float(w["end_s"]) for w in WORDS}
    assert all(min(abs(t - m) for m in marks) <= MARK_EPSILON_S for t in times)


def test_max_placements_in_the_ask_caps_the_result() -> None:
    ev = SfxEvidence(words=WORDS, duration_s=20.0)
    raw = [("fx_click", float(t)) for t in (0, 2, 4, 6)]
    out = choose_sfx(SfxAsk("x", max_placements=2), ev, CATALOG, run_agent=_agent(*raw))
    assert isinstance(out, Placed) and len(out.placements) == 2


def test_invented_times_and_unknown_effects_become_nothing_fit() -> None:
    ev = SfxEvidence(words=WORDS, duration_s=20.0)
    out = choose_sfx(
        ASK, ev, CATALOG, run_agent=_agent(("fx_click", 1.0), ("fx_nope", 2.0), ("fx_click", 19.9))
    )
    assert out == Declined(SfxLimit.nothing_fit)


def test_a_beat_time_is_not_a_mark() -> None:
    ev = SfxEvidence(words=WORDS, beats_s=(1.0, 3.0, 5.0), duration_s=20.0)
    out = choose_sfx(ASK, ev, CATALOG, run_agent=_agent(("fx_click", 1.0), ("fx_click", 3.0)))
    assert out == Declined(SfxLimit.nothing_fit)


def test_moment_with_no_description_is_not_evidence() -> None:
    ev = SfxEvidence(moments=({"start_s": 1.0, "end_s": 3.0, "description": " "},), duration_s=20.0)
    out = choose_sfx(ASK, ev, CATALOG, run_agent=_agent(("fx_click", 1.0)))
    assert out == Declined(SfxLimit.no_timed_evidence)


def test_request_and_beats_reach_the_agent() -> None:
    agent = _agent()
    ev = SfxEvidence(words=WORDS, beats_s=(0.5, 1.0), duration_s=20.0)
    choose_sfx(ASK, ev, CATALOG, run_agent=agent)
    (payload,) = agent.seen
    assert payload.request_text == ASK.request_text
    assert payload.beats_s == [0.5, 1.0]


def test_prompt_is_the_old_text_without_request_or_beats() -> None:
    from app.agents.sfx_placement import SfxCatalogEffect, SfxPlacementAgent

    base = {
        "words": [{"word": "hi", "start_s": 1.0, "end_s": 1.4}],
        "effects": [SfxCatalogEffect(effect_id="fx_click", name="Click")],
        "duration_s": 10.0,
    }
    agent = SfxPlacementAgent.__new__(SfxPlacementAgent)
    plain = agent.render_prompt(SfxPlacementInput(**base))
    rich = agent.render_prompt(
        SfxPlacementInput(**base, request_text="add effects", beats_s=[0.5, 1.0])
    )
    assert "CREATOR REQUEST" not in plain and "BEAT TIMES" not in plain
    assert "$request_block" not in plain and "$beats_block" not in plain
    assert '"add effects"' in rich and "[0.5, 1.0]" in rich
