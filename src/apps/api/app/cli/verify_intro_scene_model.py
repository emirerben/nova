"""KRI-1612: the model arm of the intro scene, through the same real Skia render check.

Part of `verify_intro_scenes` (`make verify-intro-scenes`). The model call is REPLAYED: a stub
transport answers the real agent with a hand-authored answer, so the agent's real prompt, parser,
`ask_for_fit`, `fit_intro_scene` (real `place_title_look`), ink pass, Skia draw and approval check
all run, and nothing is paid. Each case below is a cassette answer drawn on a synthetic plate; the
verdict measures the DRAWN pixels exactly as the seeded picks are measured. It proves the wiring
and the engine's rules hold for whatever the model picks; it does NOT show the model's taste is
good, which is Emir's blind compare (`app/cli/compare_intro_scenes.py`).

Controls that MUST end in today's title (no rows): a decline, a void answer (unissued id, a cue
with no sound, a split nobody issued, text smuggled in), a provider error, a model scene that
cannot sit clear.
Controls that MUST hold: a re-render of a pinned answer never calls the model again; an ink that
does not read on the plate is replaced by the engine; the approval check accepts every issued
entrance and still cannot read the handwriting ("pen") one, which is why it is not issued.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents._runtime import ModelClient, ModelInvocation, RunContext
from app.cli import verify_intro_scenes as base
from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick_mod
from app.pipeline.canvas import PORTRAIT


class ReplayClient(ModelClient):
    """The provider transport, replayed."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls = 0

    def invoke(self, **_kwargs: Any) -> ModelInvocation:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return ModelInvocation(raw_text=self.answer, tokens_in=0, tokens_out=0, raw_response=None)


def _ctx() -> RunContext:
    # No database in the CLI: keep the replayed run out of the agent_run table and Langfuse.
    return RunContext(extra={"skip_langfuse_trace": True, "skip_agent_run_persist": True})


def _ask(title: str, seed: str) -> dict[str, Any]:
    return {
        "version": pick_mod.INTRO_SCENE_VERSION,
        "seed": seed,
        "inks": pick_mod.ink_order(seed),
        "candidates": pick_mod.pick_candidates(title, seed=seed),
        pick_mod.MODEL_ASK_FIELD: {"version": pick_mod.MODEL_VERSION},
    }


def resolve(title: str, language: str, answer: str | Exception, *, brightness: str = "unknown"):
    """The real worker path up to the fit: ask the agent (replayed), pin, ask_for_fit."""
    client = ReplayClient(answer)
    pin = pick_mod.resolve_model_scene(
        title,
        language=language,
        footage_notes="a person talking to camera in front of a plain wall",
        brightness=brightness,
        job_id=None,
        client=client,
        ctx=_ctx(),
    )
    ask = {**_ask(title, "kri1612"), pick_mod.MODEL_PICK_FIELD: pin}
    fit_ask, info = pick_mod.ask_for_fit(ask)
    return client, pin, fit_ask, info


def fit_model_arm(title: str, language: str, answer: str | Exception, plate: str):
    """``(rows, receipt, info, pin)`` as the worker draws the model arm on ``plate``."""
    _client, pin, fit_ask, arm = resolve(
        title, language, answer, brightness="light" if plate == "light_wall" else "dark"
    )
    if fit_ask is None:
        return None, None, {**arm, "reason": "model_no_scene"}, pin
    rows, receipt, info = pick_mod.fit_intro_scene(
        fit_ask,
        title,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=pick_mod.language_of(title, language),
        place=lambda r, position: base._place(r, position),
    )
    if rows is not None:
        rows = pick_mod.intro_ink_pass(rows, base._take_frames(plate), canvas=PORTRAIT)
    return rows, receipt, {**info, **arm}, pin


# What a model could answer for each title: a cassette per plate (hand-authored, not recorded).
ANSWERS: dict[tuple[str, str], dict[str, Any]] = {
    ("en", "light_wall"): {
        "decision": "compose",
        "voices": ["solid", "solid", "solid", "solid", "accent", "accent"],
        "font_pair": "grotesk_hand",
        "ink": "oxblood",
        "zone": "above_head",
        "entrance": "ink",
        "sound": "none",
    },
    ("en", "dark_room"): {
        "decision": "compose",
        "voices": ["solid", "solid", "solid", "solid", "solid", "accent"],
        "font_pair": "serif_italic",
        "ink": "cream",
        "zone": "above_head",
        "entrance": "fade",
        "sound": "none",
    },
    ("tr", "light_wall"): {
        "decision": "compose",
        "voices": ["solid"] * 6 + ["accent"] * 2,
        "font_pair": "grotesk_hand",
        "ink": "oxblood",
        "zone": "above_head",
        "entrance": "ink",
        "sound": "none",
    },
    ("tr", "dark_room"): {
        "decision": "compose",
        "voices": ["solid"] * 5 + ["accent"] * 3,
        "font_pair": "script_serif",
        "ink": "chartreuse",
        "zone": "above_head",
        "entrance": "fade",
        "sound": "none",
    },
}


def _model_case(language: str, title: str, plate: str, answer: dict[str, Any], *, control=None):
    base_case: dict[str, Any] = {
        "id": f"model-{language}-{plate.split('_')[0]}" + (f"-{control}" if control else ""),
        "name": "model pick (replayed)",
        "language": language,
        "control": None,
        "expressible": True,
        "missing_moves": [],
        "title": title,
        "rendered": True,
    }
    rows, receipt, info, pin = fit_model_arm(title, language, json.dumps(answer), plate)
    if rows is None or receipt is None:
        return base._case(
            {**base_case, "verdict": "FAIL", "reasons": [f"no fit: {info.get('reason')}"]}, None
        )
    chosen = info["chosen"]
    ink_name = next(
        n for n, hex_ in scene_mod.INKS.items() if hex_ == str(rows[0]["color"]).upper()
    )
    scene = scene_mod.IntroScene(
        decision="compose",
        voices=tuple(pin["scene"]["voices"]),
        font_pair=chosen["font_pair"],
        ink=ink_name,
        zone=chosen["zone"],
        entrance=pin["scene"]["entrance"],
        sound="none",
    )
    verdict, reasons, frame = base.verify_rows(
        rows, receipt, face=base.FACE_BOX, scene=scene, footage=plate
    )
    if not base.approval_matches(rows, title):
        verdict = "FAIL"
        reasons = [*reasons, "the approval check does not find her title in the drawn layers"]
    return base._case(
        {
            **base_case,
            "verdict": verdict,
            "reasons": reasons,
            "rows": base._row_summary(rows),
            "pick": {
                "source": chosen.get("source"),
                "asked_ink": pin["scene"]["ink"],
                "drawn_ink": ink_name,
                "font_pair": chosen["font_pair"],
                "asked_zone": pin["scene"]["zone"],
                "drawn_zone": chosen["zone"],
                "entrance": scene.entrance,
                "prompt_version": pin.get("prompt_version"),
            },
        },
        frame,
    )


def model_cases() -> list[dict[str, Any]]:
    cases = [
        _model_case(language, title, plate, ANSWERS[(language, plate)])
        for language, title in base.PICK_TITLES
        for plate in base.PICK_PLATES
    ]
    return cases


def model_wiring() -> list[dict[str, Any]]:
    """Contract checks for the model arm; each names what it expects and what it saw."""
    language, title = base.PICK_TITLES[0]
    out: list[dict[str, Any]] = []

    def check(name: str, expected: str, observed: str, ok: bool) -> None:
        out.append({"name": name, "expected": expected, "observed": observed, "ok": bool(ok)})

    good = ANSWERS[("en", "light_wall")]
    refusals = {
        "a_decline_is_todays_title": {"decision": "decline", "decline_reason": "title_unsuited"},
        "an_unissued_ink_is_void": {**good, "ink": "hot_pink"},
        "a_cue_without_a_sound_is_void": {**good, "sound_cue": "accent"},
        "smuggled_text_is_void": {**good, "text": "HACKED"},
        "a_split_nobody_issued_is_void": {**good, "voices": ["accent"] * 6},
        "the_pen_entrance_is_never_issued": {**good, "entrance": "pen"},
    }
    for name, answer in refusals.items():
        rows, _receipt, info, pin = fit_model_arm(title, language, json.dumps(answer), "light_wall")
        check(
            name,
            "no rows (today's title)",
            f"rows={'none' if rows is None else len(rows)}; pin={pin.get('outcome')}",
            rows is None and pin.get("outcome") in ("decline", "failed"),
        )
    for name, error in (("a_provider_error_is_todays_title", RuntimeError("503")),):
        rows, _receipt, _info, pin = fit_model_arm(title, language, error, "light_wall")
        check(name, "no rows", f"rows={'none' if rows is None else len(rows)}", rows is None)

    # A model scene that cannot sit clear on the real placement: today's title, never the seed.
    _c, _p, fit_ask, _arm = resolve(title, language, json.dumps(good))
    assert fit_ask is not None

    def unclean(rows, _position):
        return rows, {"status": "best_effort", "face_collision": True}

    none_rows, _r, info = pick_mod.fit_intro_scene(
        fit_ask,
        title,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=None,
        place=unclean,
    )
    check(
        "an_unclean_model_scene_is_todays_title",
        "no rows, only the model's pair tried",
        f"rows={'none' if none_rows is None else len(none_rows)}; pairs "
        f"{sorted({t['font_pair'] for t in info['tried']})}",
        none_rows is None and {t["font_pair"] for t in info["tried"]} == {"grotesk_hand"},
    )

    # A pinned answer is never asked again.
    ask = {**_ask(title, "pinned"), pick_mod.MODEL_PICK_FIELD: _p}
    again = ReplayClient(RuntimeError("must not be asked"))
    plan, new = pick_mod.resolve_for_render(
        {pick_mod.INTRO_SCENE_ASK_FIELD: ask},
        title,
        applies=True,
        language=None,
        footage_notes="",
        frames=[],
        job_id=None,
        client=again,
    )
    check(
        "a_pinned_answer_is_not_asked_again",
        "0 calls, nothing new to pin",
        f"{again.calls} calls, new={'none' if new is None else new.get('outcome')}",
        again.calls == 0 and new is None,
    )

    # The engine still owns legibility: oxblood asked on a dark plate is replaced.
    dark_oxblood = {**good, "ink": "oxblood"}
    rows, _r, _i, _pin = fit_model_arm(title, language, json.dumps(dark_oxblood), "dark_room")
    drawn = (
        next((n for n, hx in scene_mod.INKS.items() if hx == str(rows[0]["color"]).upper()), None)
        if rows
        else None
    )
    check(
        "an_unreadable_model_ink_is_replaced_by_the_engine",
        "an ink other than oxblood on the dark plate",
        f"drawn {drawn}",
        rows is not None and drawn not in (None, "oxblood"),
    )

    # The approval check reads every ink-wipe/pop/fade/rise entrance of every issued pair, and
    # still cannot read the handwriting ("pen") one: that is why it is not issued to the model.
    refused: list[str] = []
    pen_read = None
    for lang, pair_title in base.PICK_TITLES:
        menu = scene_mod.issue_menu(pair_title)
        for pair in menu.font_pairs:
            for entrance in menu.entrances:
                if entrance == "pen" and scene_mod.FONT_PAIRS[pair].accent.family != "Patrick Hand":
                    continue
                scene = scene_mod.IntroScene(
                    decision="compose",
                    voices=tuple(pick_mod.accent_splits(pair_title)[0]),
                    font_pair=pair,
                    ink="white",
                    zone="above_head",
                    entrance=entrance,
                    sound="none",
                )
                compiled = scene_mod.compile_intro_scene(
                    scene, pair_title, canvas=PORTRAIT, timeline_duration_s=30.0, language=lang
                )
                if compiled.rows is None:
                    continue
                placed, _receipt = base._place(compiled.rows, compiled.position)
                accepted = bool(base.approval_matches(placed, pair_title))
                if entrance == "pen":
                    pen_read = accepted if pen_read is None else (pen_read and accepted)
                elif not accepted:
                    refused.append(f"{lang}:{pair}:{entrance}")
    check(
        "approval_reads_every_issued_entrance",
        "none refused",
        ", ".join(refused) or "none refused",
        not refused,
    )
    check(
        "approval_cannot_read_the_pen_entrance_so_it_is_not_issued",
        "pen refused by approval (flip this when pen can be issued)",
        f"pen accepted={pen_read}",
        pen_read is False,
    )
    return out
