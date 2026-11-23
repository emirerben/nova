"""KRI-1584 on the Kyoto Torokko fixture (prod job 3930c822): one font decision per role, made by
one owner that sees what each font is and what the footage looks like.

Ways this fails, written before the change:
1. The flag-off path changes: the agent's prompt for an unstamped job differs from main's.
2. The agent sees bare font names, with no category or vibe, so it cannot tell a script from a
   grotesk.
3. The mood is the edit type (`plan.direction` = "guided_story", or a style set id), not the
   footage.
4. Favourites are a hard filter: with a Big Curls favourite the only eligible title font is Big
   Curls, a script, on long Turkish fact lines.
5. A font an earlier writer chose is lost instead of reaching the one choice as a hint.
6. A row whose font a chosen design (or the creator) set is still sent to the agent, or loses
   its font to the agent's pick.
7. The reason for the pick exists only in the agent's redacted I/O, so the trace has no reason.
8. An excluded font is offered.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.pipeline.informative_text import POLICY_KEY, apply_design_values
from app.pipeline.text_designs import TEXT_DESIGNS, design_level_values
from app.services import phone_text_styling as styling

pytestmark = pytest.mark.journey("kri-1585-informative-text")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1585_kyoto"
# sha256 of the font agent's prompt for the unstamped Kyoto generation (`_prompts`), computed on
# origin/main 9164c0b6 before this change.
FLAG_OFF_PROMPT_SHA256 = "ab2ec6913f124c93c5cc45e4575ff5a3a1437ee37b059e6428653bba36025c50"


def _load(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _snapshot(*, stamped: bool, proposal_font: str | None = None) -> dict:
    plan = _load("plan.json")
    if proposal_font is not None:
        plan["approved_proposal"]["font_family"] = proposal_font
    style = plan["variant"][styling.PHONE_TEXT_STYLE_FIELD]
    policy = {key: copy.deepcopy(style[key]) for key in ("version", "preferences", "requested")}
    if stamped:
        policy[POLICY_KEY] = True
    return {
        styling.PHONE_TEXT_STYLE_FIELD: policy,
        "guided_edit": {"approved_proposal": plan["approved_proposal"]},
    }


class _Recorded(ModelClient):
    def __init__(self, raw_text: str):
        self.raw_text = raw_text
        self.prompts: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs) -> ModelInvocation:
        self.prompts.append(prompt)
        return ModelInvocation(raw_text=self.raw_text)


def _generate(
    monkeypatch, rows: list[dict], *, stamped: bool, answer: dict, proposal_font=None
) -> tuple:
    """One phone generation over ``rows``, the way the guided writer styles its text."""
    from app.agents import _model_client
    from app.services import pipeline_trace

    client = _Recorded(json.dumps(answer))
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(_model_client, "default_client", lambda: client)
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((event, data or {})),
    )

    @styling.phone_text_generation
    def writer(job_id, snapshot):
        return styling.generation_style_rows(rows, job_id=job_id, mood="guided_story")

    styled = writer("3930c822", _snapshot(stamped=stamped, proposal_font=proposal_font))
    return styled, client.prompts, events


def _rows() -> list[dict]:
    return copy.deepcopy(_load("text_rows.json")["text_elements"])


def _without_reason(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "reason"}


def _payload(prompt: str) -> dict:
    return json.loads(prompt[prompt.index("\n{") + 1 :])


def test_flag_off_font_prompt_is_byte_identical_to_main(monkeypatch):
    _styled, prompts, _events = _generate(
        monkeypatch, _rows(), stamped=False, answer={"fonts": {"title": "Big Curls"}}
    )
    assert len(prompts) == 1
    digest = hashlib.sha256(prompts[0].encode("utf-8")).hexdigest()
    assert digest == FLAG_OFF_PROMPT_SHA256


def test_one_owner_sees_font_traits_footage_mood_and_ranked_favourites(monkeypatch):
    answer = {
        "fonts": {"research": "Unbounded"},
        "reasons": {"research": "Long factual lines over a train ride read best in a grotesk."},
    }
    styled, prompts, events = _generate(monkeypatch, _rows(), stamped=True, answer=answer)

    assert len(prompts) == 1
    sent = _payload(prompts[0])
    research = sent["roles"]["research"]
    # Her favourites are ranked first and marked, but the curated fonts stay choosable. Big
    # Curls, also a favourite, cannot draw the Turkish lines, so it is not offered.
    assert research["eligible_fonts"][:2] == ["Inter", "Montserrat"]
    assert research["preferred_fonts"] == ["Inter", "Montserrat"]
    assert "Big Curls" not in research["eligible_fonts"]
    assert {"Unbounded", "Inter Medium", "Pacifico"} <= set(research["eligible_fonts"])
    assert "Fraunces" not in research["eligible_fonts"]
    # Kyoto's rows carry no writer-chosen font, so there is no hint.
    assert "hint" not in research
    assert sent["fonts"]["Pacifico"]["category"] == "script"
    assert sent["fonts"]["Inter Medium"] == {"category": "sans", "vibe": "clean_captions"}
    assert set(sent["fonts"]) == set(research["eligible_fonts"])
    # The mood is the footage, not the edit type.
    assert "train" in sent["mood"] and "guided_story" not in sent["mood"]
    assert "very short, common words" in prompts[0]

    # A non-favourite pick now survives the resolver (main offered only her favourites).
    assert {row["font_family"] for row in styled} == {"Unbounded"}
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert decision["roles"]["research"] == {
        "font": "Unbounded",
        "source": "agent",
        "reason": answer["reasons"]["research"],
    }
    assert decision["mood"] == sent["mood"]


# The B1 guide line's face, from the design registry (KRI-1581 set it from the mock-ups).
_B1_FONT = TEXT_DESIGNS["B1"].detail.font_family


def test_kyoto_b1_design_font_bypasses_the_agent(monkeypatch):
    rows = [apply_design_values(row, design_level_values("B1", "detail"), "B1") for row in _rows()]
    styled, prompts, events = _generate(
        monkeypatch, rows, stamped=True, answer={"fonts": {"research": "Big Curls"}}
    )

    assert prompts == []
    assert {row["font_family"] for row in styled} == {_B1_FONT}
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert decision["roles"]["research"] == {"fonts": [_B1_FONT], "source": "design"}


def test_a_creator_font_on_every_row_is_hers_not_the_agents(monkeypatch):
    rows = _rows()
    for row in rows:
        row["font_family"] = "Montserrat Bold"
        row["source_params"]["creator_style_fields"] = ["font_family"]
    styled, prompts, events = _generate(
        monkeypatch, rows, stamped=True, answer={"fonts": {"research": "Big Curls"}}
    )

    assert prompts == []
    assert {row["font_family"] for row in styled} == {"Montserrat Bold"}
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert decision["roles"]["research"]["source"] == "creator"


def test_a_failed_pick_is_traced_and_falls_back_to_the_ranked_pool(monkeypatch):
    styled, prompts, events = _generate(
        monkeypatch, _rows(), stamped=True, answer={"fonts": {"research": "Fraunces"}}
    )

    assert len(prompts) == 1
    (decision,) = [data for event, data in events if event == "font_decision"]
    # Still one decision: her first drawable favourite, not the resolver's swap of Big Curls
    # for another script (Great Vibes) on long fact lines.
    assert _without_reason(decision["roles"]["research"]) == {"font": "Inter", "source": "fallback"}
    assert {row["font_family"] for row in styled} == {"Inter"}


def test_an_add_text_writers_font_is_the_hint_and_its_default_is_not(monkeypatch):
    rows = _rows()
    # A chat add_text row records its defaults; its font is the writer's own choice here.
    rows[0]["font_family"] = "Unbounded"
    rows[0]["source_params"]["default_style_fields"] = ["size_px"]
    # The add_text Playfair default stays a default, and the proposal's font is no hint.
    for row in rows[1:]:
        row["font_family"] = "Montserrat"
        row["source_params"]["default_style_fields"] = ["font_family"]
    _styled, prompts, _events = _generate(
        monkeypatch,
        rows,
        stamped=True,
        answer={"fonts": {"research": "Unbounded"}},
        proposal_font="Inter",
    )
    assert _payload(prompts[0])["roles"]["research"]["hint"] == "Unbounded"

    _styled, prompts, _events = _generate(
        monkeypatch,
        rows[1:],
        stamped=True,
        answer={"fonts": {"research": "Unbounded"}},
        proposal_font="Inter",
    )
    assert "hint" not in _payload(prompts[0])["roles"]["research"]


def test_a_failed_pick_with_no_favourite_or_hint_leaves_the_resolver_order(monkeypatch):
    def no_favourites(*, stamped, proposal_font=None, _real=_snapshot):
        snapshot = _real(stamped=stamped, proposal_font=proposal_font)
        prefs = snapshot[styling.PHONE_TEXT_STYLE_FIELD]["preferences"]
        for rule in prefs.values():
            rule.pop("favorite_fonts", None)
        return snapshot

    monkeypatch.setattr(f"{__name__}._snapshot", no_favourites)
    _styled, prompts, events = _generate(
        monkeypatch, _rows(), stamped=True, answer={"fonts": {"research": "Fraunces"}}
    )

    assert len(prompts) == 1
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert _without_reason(decision["roles"]["research"]) == {"font": None, "source": "fallback"}


# Round-1 failure modes, written before the fix:
# 9. Under a phone policy the planner's `_fit_typography` (unified and lip-sync routes) writes a
#    provisional face, the first curated font that draws the text, onto the proposal, and the
#    guided writer copies it onto every row. That placeholder reaches the agent as a "hint" and,
#    when the pick fails, beats her favourites.
# 10. Captions receive the proposal's font as a hint and as their fallback, over her caption
#     favourites and the caption face.
# 11. The fallback puts a script favourite (Big Curls) on long Latin-script fact lines.
# 12. A malformed `reasons` value rejects the whole pick.
# 13. Unstamped agent runs log the new prompt_version though they send the old prompt.
# 14. A role whose rows all carry a design font is traced `saved_rule` when an account rule
#     names a font.

_ENGLISH = [
    "The Sagano line follows the Hozu river gorge for about seven kilometres",
    "The open carriages were built from old freight wagons in the nineteen nineties",
    "Autumn colours peak along the gorge in late November every year",
]


def _english_rows(font: str | None = None) -> list[dict]:
    rows = _rows()
    for row, text in zip(rows, _ENGLISH, strict=True):
        row["text"] = text
        row["source_params"]["research"]["original_text"] = text
        if font is not None:
            row["font_family"] = font
    return rows


@pytest.mark.parametrize("module_name", ["unified_montage", "lipsync_montage"])
def test_a_provisional_planner_font_is_never_a_hint(monkeypatch, module_name):
    import importlib

    from app.pipeline.phone_text_style import _covers

    module = importlib.import_module(f"app.pipeline.{module_name}")
    policy = _snapshot(stamped=True)[styling.PHONE_TEXT_STYLE_FIELD]
    with styling.phone_text_policy_scope(policy):
        family, *_ = module._fit_typography(_covers, None, "Montage", None, {})
    # The guided writer copies the proposal's face onto its rows (`snapshot.font_family`).
    _styled, prompts, events = _generate(
        monkeypatch,
        _english_rows(font=family),
        stamped=True,
        answer={"fonts": {"research": "Fraunces"}},
        proposal_font=family,
    )

    research = _payload(prompts[0])["roles"]["research"]
    assert "hint" not in research
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert decision["roles"]["research"]["font"] != family
    assert {row["font_family"] for row in _styled} != {family}


def test_captions_never_take_the_proposal_font(monkeypatch):
    from app.agents import _model_client
    from app.services import pipeline_trace

    events: list[tuple[str, dict]] = []
    looks = {}
    for stamped in (False, True):
        client = _Recorded(json.dumps({"fonts": {"caption": "Fraunces"}}))
        monkeypatch.setattr(_model_client, "default_client", lambda client=client: client)
        monkeypatch.setattr(
            pipeline_trace,
            "record_pipeline_event",
            lambda stage, event, data=None: events.append((event, data or {})),
        )

        @styling.phone_text_generation
        def writer(job_id, snapshot):
            return styling.generation_caption_look(
                [{"text": "we took the slow train", "start_s": 0.0, "end_s": 1.5}]
            )

        looks[stamped] = writer("3930c822", _snapshot(stamped=stamped, proposal_font="Unbounded"))
        if stamped:
            assert "hint" not in _payload(client.prompts[0])["roles"]["caption"]
    assert looks[True].font_family == looks[False].font_family != "Unbounded"


def test_a_failed_pick_never_puts_a_script_on_long_lines(monkeypatch):
    styled, _prompts, events = _generate(
        monkeypatch, _english_rows(), stamped=True, answer={"fonts": {"research": "Fraunces"}}
    )

    # Big Curls, her first favourite, is a script and draws English; long facts get Inter.
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert _without_reason(decision["roles"]["research"]) == {"font": "Inter", "source": "fallback"}
    assert {row["font_family"] for row in styled} == {"Inter"}


def test_a_failed_pick_with_only_script_favourites_takes_a_readable_curated_font(monkeypatch):
    from app.schemas.phone_text_style import CURATED_PHONE_FONTS

    def script_only(*, stamped, proposal_font=None, _real=_snapshot):
        snapshot = _real(stamped=stamped, proposal_font=proposal_font)
        prefs = snapshot[styling.PHONE_TEXT_STYLE_FIELD]["preferences"]
        prefs["all_text"]["favorite_fonts"] = ["Big Curls"]
        return snapshot

    monkeypatch.setattr(f"{__name__}._snapshot", script_only)
    styled, _prompts, events = _generate(
        monkeypatch, _english_rows(), stamped=True, answer={"fonts": {"research": "Fraunces"}}
    )

    (decision,) = [data for event, data in events if event == "font_decision"]
    assert _without_reason(decision["roles"]["research"]) == {
        "font": CURATED_PHONE_FONTS[0],
        "source": "fallback",
    }
    assert {row["font_family"] for row in styled} == {CURATED_PHONE_FONTS[0]}


@pytest.mark.parametrize(
    "reasons",
    [{"research": None}, ["x"], {f"r{index}": "x" for index in range(9)}, {"research": 5}],
)
def test_a_malformed_reason_never_costs_the_pick(monkeypatch, reasons):
    styled, _prompts, events = _generate(
        monkeypatch,
        _rows(),
        stamped=True,
        answer={"fonts": {"research": "Unbounded"}, "reasons": reasons},
    )

    assert {row["font_family"] for row in styled} == {"Unbounded"}
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert decision["roles"]["research"]["source"] == "agent"


def test_unstamped_runs_log_the_prompt_version_they_send(monkeypatch):
    from app.agents import _runtime

    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        _runtime.Agent,
        "project_output_for_observability",
        lambda self, output_dict: logged.append((self.spec.prompt_version, output_dict)),
    )
    for stamped, role in ((False, "title"), (True, "research")):
        _generate(
            monkeypatch,
            _rows(),
            stamped=stamped,
            answer={"fonts": {role: "Montserrat"}, "reasons": {role: "Reads at length."}},
        )
    assert [version for version, _output in logged] == ["2026-10-13-v2", "2026-11-22-v3"]
    # The unstamped run's recorded output keeps main's shape; only the stamped one has reasons.
    assert logged[0][1] == {"fonts": {"title": "Montserrat"}}
    assert logged[1][1]["reasons"] == {"research": "Reads at length."}


def test_a_design_font_is_traced_as_the_design_over_a_saved_rule(monkeypatch):
    def with_rule(*, stamped, proposal_font=None, _real=_snapshot):
        snapshot = _real(stamped=stamped, proposal_font=proposal_font)
        snapshot[styling.PHONE_TEXT_STYLE_FIELD]["requested"] = {
            "all_text": {"font_family": "Montserrat"}
        }
        return snapshot

    monkeypatch.setattr(f"{__name__}._snapshot", with_rule)
    rows = [apply_design_values(row, design_level_values("B1", "detail"), "B1") for row in _rows()]
    styled, prompts, events = _generate(
        monkeypatch, rows, stamped=True, answer={"fonts": {"research": "Big Curls"}}
    )

    assert prompts == []
    assert {row["font_family"] for row in styled} == {_B1_FONT}
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert decision["roles"]["research"] == {"fonts": [_B1_FONT], "source": "design"}


# Round-2 failure modes, written before the fix:
# 15. A failed pick on a role with a hint but no drawable favourite crashes the writer
#     (`preferred_fonts` is set only when a favourite is drawable): no favourites at all, or
#     only a favourite that cannot draw the wording (Big Curls on the Turkish rows).
# 16. The same crash when the hint itself is a script on long lines and there is no favourite.


def _hinted_rows(font: str) -> list[dict]:
    rows = _rows()
    rows[0]["font_family"] = font
    rows[0]["source_params"]["default_style_fields"] = ["size_px"]
    return rows


def _favourites(monkeypatch, favourites: list[str] | None) -> None:
    def patched(*, stamped, proposal_font=None, _real=_snapshot):
        snapshot = _real(stamped=stamped, proposal_font=proposal_font)
        prefs = snapshot[styling.PHONE_TEXT_STYLE_FIELD]["preferences"]
        for rule in prefs.values():
            rule.pop("favorite_fonts", None)
        if favourites is not None:
            prefs.setdefault("all_text", {})["favorite_fonts"] = favourites
        return snapshot

    monkeypatch.setattr(f"{__name__}._snapshot", patched)


@pytest.mark.parametrize("favourites", [None, ["Big Curls"]], ids=["none", "undrawable"])
def test_a_failed_pick_with_a_hint_and_no_drawable_favourite_takes_the_hint(
    monkeypatch, favourites
):
    _favourites(monkeypatch, favourites)
    styled, prompts, events = _generate(
        monkeypatch,
        _hinted_rows("Unbounded"),
        stamped=True,
        answer={"fonts": {"research": "Fraunces"}},
    )

    research = _payload(prompts[0])["roles"]["research"]
    assert research["hint"] == "Unbounded" and "preferred_fonts" not in research
    (decision,) = [data for event, data in events if event == "font_decision"]
    assert decision["roles"]["research"]["font"] == "Unbounded"
    assert decision["roles"]["research"]["source"] == "fallback"
    assert {row["font_family"] for row in styled} == {"Unbounded"}


def test_a_failed_pick_with_a_script_hint_on_long_lines_takes_a_readable_font(monkeypatch):
    from app.pipeline.font_coverage import _registry

    _favourites(monkeypatch, None)
    styled, prompts, events = _generate(
        monkeypatch,
        _hinted_rows("Pacifico"),
        stamped=True,
        answer={"fonts": {"research": "Fraunces"}},
    )

    assert _payload(prompts[0])["roles"]["research"]["hint"] == "Pacifico"
    (decision,) = [data for event, data in events if event == "font_decision"]
    font = decision["roles"]["research"]["font"]
    assert decision["roles"]["research"]["source"] == "fallback"
    assert font and _registry()["fonts"][font]["category"] not in {"script", "handwriting"}
    assert {row["font_family"] for row in styled} == {font}


# 17. The voiceover montage writer's snapshot has no approved proposal, so on a stamped job its
#     font pick gets no mood at all (main sent its style set id).
def test_the_voiceover_writer_gives_its_own_clip_analysis_as_the_mood(monkeypatch):
    import inspect

    from app.pipeline.agents.gemini_analyzer import ClipMeta
    from app.tasks import generative_build

    source = inspect.getsource(generative_build._run_phone_voiceover_montage_job)
    assert source.index("register_generation_footage(clip_metas)") < source.index(
        "generation_style_rows("
    )

    metas = [
        ClipMeta(
            clip_id="a",
            transcript="",
            hook_text="",
            hook_score=0.0,
            best_moments=[],
            detected_subject="red and yellow train",
            setting="autumn river gorge",
            activity="riding an open carriage",
        )
    ]
    sent = {}
    for stamped in (False, True):
        client = _Recorded(json.dumps({"fonts": {"research": "Unbounded"}}))
        from app.agents import _model_client

        monkeypatch.setattr(_model_client, "default_client", lambda client=client: client)

        @styling.phone_text_generation
        def writer(job_id, snapshot):
            styling.register_generation_footage(metas)
            return styling.generation_style_rows(_rows(), job_id=job_id, mood="clean_editorial")

        snapshot = _snapshot(stamped=stamped)
        snapshot.pop("guided_edit")
        writer("3930c822", snapshot)
        sent[stamped] = _payload(client.prompts[0])["mood"]
    assert sent[False] == "clean_editorial"
    assert sent[True] == "autumn river gorge; red and yellow train; riding an open carriage"


# 18. A generation's font decision is split over two events (rows, then the caption pick), and
#     a fallback entry says neither why the pick failed nor who decides when it names no font.
def test_the_last_font_event_is_the_whole_generation_and_fallbacks_say_why(monkeypatch):
    from app.agents import _model_client
    from app.services import pipeline_trace

    def timeout():
        raise TimeoutError("typography timed out")

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(_model_client, "default_client", timeout)
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((event, data or {})),
    )

    @styling.phone_text_generation
    def writer(job_id, snapshot):
        styling.generation_style_rows(_rows(), job_id=job_id)
        return styling.generation_caption_look(
            [{"text": "we took the slow train", "start_s": 0.0, "end_s": 1.5}]
        )

    writer("3930c822", _snapshot(stamped=True))
    decisions = [data for event, data in events if event == "font_decision"]
    assert len(decisions) == 2
    last = decisions[-1]["roles"]
    assert set(last) == {"research", "caption"}
    assert last["research"]["font"] == "Inter"
    assert "TimeoutError" in last["research"]["reason"]
    assert last["caption"]["font"] is None
    assert "TimeoutError" in last["caption"]["reason"] and "order" in last["caption"]["reason"]
