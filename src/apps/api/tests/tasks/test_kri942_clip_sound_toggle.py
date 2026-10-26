"""KRI-942: clip sound on and off where the creator says, through the real phone montage writer.

Incident (prod thread 33BA8AFB, job b3820ddc, 2026-10-25, Don Julio walk-in M1): "Clip sound
off except that last line." The correction turn ("add the title") rewrote the strategy's
`montage_audio.source_media_ids` to `[]`, the unified montage built ONE global
`original_volume`, and every clip played its own sound; the brief's r7 had no facts, so the
receipt said "I can't verify this one automatically yet".

The approved execution plan and the clip records are the incident's own
(``tests/fixtures/kri942_donjulio_walkin_m1.json``, ids renamed to the kit's upload names).
Everything below runs the real dispatcher (`_run_generative_job` -> `_run_phone_guided_job`),
the real line grounding, the phone compiler and the pilot check; only storage and the database
are fixed values. Atakan's "Super excited." is 5.86-6.44 s of m1_04 (the kit README, from the
source video: 5.9-6.7 s); render 2 cut m1_04 at 5.71-6.74 s, the last cut (16.972-18.002 s).

Ways this can fail (docs/reviews/kri-942-clip-sound-toggle.md), each pinned below:
1. The ask is lost when the strategy changes on a correction turn.
2. "Last" lands on the attachment order's last clip instead of the edit's.
3. A window lands off the compiled clock.
4. A kept line is clipped at an edge.
6. The receipt says "met" while the recipe plays the sound (or plays the wrong clip).
7. A phone that can't mute part of a clip fails the render or degrades silently.
8. A line that isn't in the clip gets a guessed window.
9. A window past the video's end.
10. Two asks conflict without a fixed winner.
11. A route that doesn't build the asks is judged from its own recipe.
13. Wording decides: a Turkish ask behaves differently.
"""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import PlanFacts, check_requirement, plan_facts_from_phone_variant
from app.kria.device_render import recipe_digest
from app.kria.draft_sound_carry import carry_draft_sound, sound_asks_unchanged
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes import TimelineClip, TimelineTrack
from app.kria.reply_language import reply_language_for
from app.services.creator_render_contract import build_render_contract
from app.services.device_render import device_status
from app.services.phone_clip_sound_toggle import TOGGLE_RECEIPT_FIELD, rendered_toggle_receipt
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri942_donjulio_walkin_m1.json"
DATA = json.loads(_FIXTURE.read_text(encoding="utf-8"))
ATAKAN, BUTCHER, GRILL, FLOWERS = "m1_04", "m1_05", "m1_08", "m1_07"
CUTS = [m["moment_id"] for m in DATA["execution_plan"]["story_timeline"]]
LAST = CUTS[-1]  # unified-cut-7, Atakan, 16.972-18.002 s
# Where "Super excited." plays on the edit's clock: 16.972 + (5.86 - 5.71) .. + (6.44 - 5.71).
LINE_ON_TIMELINE = (17.122, 17.702)
_FEATURES = [
    "basicComposition",
    "local1080Export",
    "audioMix",
    "positionedText",
    "animatedText",
    "authoredText",
]
# "Clip sound off except that last line." as the brief now records it.
OFF_EXCEPT_LINE = {
    "clip_sound": "off",
    "sound_except": [{"sound_clip": "last", "sound_line": "Super excited!"}],
}


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    return tuple(
        PhoneSourceBinding(
            media_id=clip["media_id"],
            proxy_path=f"users/u/analysis-proxy-{clip['media_id']}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=f"{index:x}".rjust(64, "c"),
                byte_count=10_000 + index,
                duration_s=clip["duration_s"],
                width=clip["width"] or 1080,
                height=clip["height"] or 1920,
                has_audio=clip["has_audio"],
            ),
        )
        for index, clip in enumerate(DATA["clips"])
    )


def _brief(r7_facts: dict | None, *, description: str | None = None, extra=()) -> CreativeBrief:
    rows = copy.deepcopy(DATA["brief"]["requirements"])
    for row in rows:
        if row["id"] == "r7":
            row["facts"] = dict(r7_facts or {})
            if description is not None:
                row["description"] = description
    return CreativeBrief(
        version=DATA["brief"]["version"],
        requirements=[BriefRequirement(**row) for row in rows] + list(extra),
    )


def _binding(brief: CreativeBrief) -> dict:
    return BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-942"),
        brief,
        latest_message="add the title",
        media_snapshot={"clip_assignments": copy.deepcopy(DATA["clips"])},
    ).model_dump(mode="json")


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    r7_facts: dict | None = OFF_EXCEPT_LINE,
    brief: CreativeBrief | None = None,
    strategy: dict | None = None,
    windows: bool = False,
):
    """Run the incident's approved plan through the real phone writer; return the job."""
    bindings = _bindings()
    brief = brief if brief is not None else _brief(r7_facts)
    snapshot = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": "generation",
        "guided_edit": {"approved": True},
        "creator_brief_binding": _binding(brief),
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=copy.deepcopy(snapshot),
        status="queued",
        all_candidates={"creator_strategy": strategy if strategy is not None else {}},
        error_detail=None,
        failure_reason=None,
    )
    session = Mock()

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
    monkeypatch.setattr(
        gb,
        "_guided_execution_plan",
        Mock(return_value=(copy.deepcopy(DATA["execution_plan"]), None)),
    )
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_clip_sound_windows_enabled", windows)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", [*_FEATURES, "visualBlocks"])
    monkeypatch.setattr(
        gb, "_run_guided_story_job", Mock(side_effect=AssertionError("entered the cloud renderer"))
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.error_detail, job.failure_reason)
    return job


def _recipe(job):
    return device_status(job, "guided_story").request.recipe


def _volumes(job) -> dict[str, float]:
    recipe = _recipe(job)
    story = next(t for t in recipe.tracks if t.id == "story")
    gain = recipe.audio.original_volume
    return {clip.id: round(clip.volume * gain, 3) for clip in story.clips}


def _windows(job) -> list[tuple[float, float, tuple[str, ...]]]:
    return [
        (round(w.start, 3), round(w.end, 3), tuple(w.clip_ids))
        for w in _recipe(job).audio.mute_windows
    ]


def _rows(job) -> dict[str, dict]:
    record = job.assembly_plan["variants"][0][TOGGLE_RECEIPT_FIELD]
    return {row["requirement_id"]: row for row in record["rows"]}


def _judge(job, brief: CreativeBrief, recipe=None) -> dict[str, dict]:
    """What the finished-video receipt reads: the asks re-judged on the pinned recipe."""
    assembly = {**job.assembly_plan, "creator_brief_binding": _binding(brief)}
    variant = job.assembly_plan["variants"][0]
    receipt = rendered_toggle_receipt(assembly, variant, recipe or _recipe(job))
    return {row["requirement_id"]: row for row in receipt["rows"]}


# -- the incident ----------------------------------------------------------------------


def test_incident_only_the_closing_line_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _run(monkeypatch)
    volumes = _volumes(job)
    # Every clip silent except Atakan's closing cut, which plays whole (no mute window: the
    # padded line covers the cut to within MIN_RUN_S, 4).
    assert {cut: volumes[cut] for cut in CUTS[:-1]} == dict.fromkeys(CUTS[:-1], 0.0)
    assert volumes[LAST] == 1.0
    assert _windows(job) == []
    row = _rows(job)["r7"]
    assert row["status"] == "met", row
    assert row["line"] == "Super excited!" and row["line_media_id"] == ATAKAN
    assert (row["line_start_s"], row["line_end_s"]) == (5.86, 6.44)
    (on,) = row["on_windows"]
    assert on[0] <= LINE_ON_TIMELINE[0] and on[1] >= LINE_ON_TIMELINE[1]
    # The editor reads the moments: the six silent cuts are muted occurrences there too.
    timeline = job.assembly_plan["variants"][0]["story_timeline"]
    assert [bool(m.get("source_audio_muted")) for m in timeline] == [True] * 6 + [False]


def test_as_recorded_brief_changes_nothing_and_the_judge_rejects_that_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control: r7 as it was recorded (no facts) builds today's recipe, every clip
    audible; judged against the ask, that recipe is NOT met."""
    job = _run(monkeypatch, r7_facts={})
    assert set(_volumes(job).values()) == {1.0}
    assert TOGGLE_RECEIPT_FIELD not in job.assembly_plan["variants"][0]
    verdict = _judge(job, _brief(OFF_EXCEPT_LINE))["r7"]
    assert verdict["status"] != "met"
    assert verdict["mismatch_s"] > 10  # 17 s of camera sound where it should be silent


def test_wrong_clip_audible_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrong-content control (6): same count, length and order, but the flowers clip plays and
    Atakan's line is silent. The judge must refuse it."""
    job = _run(monkeypatch)
    recipe = _recipe(job)
    story = next(t for t in recipe.tracks if t.id == "story")
    swapped = [
        clip.model_copy(update={"volume": 1.0 if clip.source_asset_id == FLOWERS else 0.0})
        for clip in story.clips
    ]
    wrong = recipe.model_copy(
        update={
            "tracks": [
                t.model_copy(update={"clips": swapped}) if t.id == "story" else t
                for t in recipe.tracks
            ]
        }
    )
    verdict = _judge(job, _brief(OFF_EXCEPT_LINE), wrong)["r7"]
    assert verdict["status"] == "partial"
    assert verdict["mismatch_s"] > 2.5  # the flowers cut (2.1 s) + the silent line (~1 s)


@pytest.mark.parametrize("source_ids", [[], [ATAKAN]])
def test_correction_turn_strategy_rewrite_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, source_ids: list[str]
) -> None:
    """1: the ask lives in the brief; turn 1's `[m1_04]` and turn 2's `[]` render alike."""
    baseline = _run(monkeypatch)
    job = _run(
        monkeypatch,
        strategy={"montage_audio": {"source_media_ids": source_ids, "preserve_source_audio": True}},
    )
    assert recipe_digest(_recipe(job)) == recipe_digest(_recipe(baseline))
    assert _rows(job)["r7"]["status"] == "met"


def test_named_strategy_source_alone_mutes_every_other_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue's second cause: with `source_media_ids` named and no on/off fact, the
    unified route played every clip. Only the named source keeps its sound now."""
    job = _run(
        monkeypatch,
        r7_facts={},
        strategy={"montage_audio": {"source_media_ids": [ATAKAN], "preserve_source_audio": True}},
    )
    volumes = _volumes(job)
    assert volumes[LAST] == 1.0
    assert all(volumes[cut] == 0.0 for cut in CUTS[:-1])
    assert job.assembly_plan["variants"][0][TOGGLE_RECEIPT_FIELD]["rows"] == []


# -- the other ways to ask -------------------------------------------------------------


def test_mute_the_last_clip_is_the_edits_last_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    """2: the last clip of the EDIT (Atakan), not the attachment order's last (m1_08)."""
    job = _run(monkeypatch, r7_facts={"clip_sound": "off", "sound_clip": "last"})
    volumes = _volumes(job)
    assert volumes[LAST] == 0.0
    assert all(volumes[cut] == 1.0 for cut in CUTS[:-1])
    assert _rows(job)["r7"]["status"] == "met"


def test_only_the_butcher_clip_keeps_its_sound(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _run(
        monkeypatch,
        r7_facts={"clip_sound": "off", "sound_except": [{"sound_media": BUTCHER}]},
    )
    volumes = _volumes(job)
    story = DATA["execution_plan"]["story_timeline"]
    assert {m["moment_id"] for m in story if volumes[m["moment_id"]] > 0} == {
        m["moment_id"] for m in story if m["media_id"] == BUTCHER
    }
    assert _rows(job)["r7"]["status"] == "met"


def test_a_stretch_of_seconds_mutes_part_of_two_clips(monkeypatch: pytest.MonkeyPatch) -> None:
    """3: "no sound from 5 to 8 seconds" lands on the compiled clock as two mute windows."""
    job = _run(
        monkeypatch,
        r7_facts={"clip_sound": "off", "sound_from_s": 5, "sound_to_s": 8},
        windows=True,
    )
    assert set(_volumes(job).values()) == {1.0}
    assert _windows(job) == [(5.0, 6.014, (CUTS[1],)), (6.014, 8.0, (CUTS[2],))]
    assert "visualBlocks" in _recipe(job).required_capabilities
    row = _rows(job)["r7"]
    assert row["status"] == "met" and row["off_windows"] == [[5.0, 8.0]]
    # The plan keeps the spans in source seconds (a later trim keeps the same footage silent).
    timeline = job.assembly_plan["variants"][0]["story_timeline"]
    assert timeline[1]["source_audio_mute_spans"] == [[5.986, 7.0]]


def test_part_of_a_clip_without_the_feature_renders_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7: windows off, the video still renders with every clip's sound, and the receipt says
    why instead of "met"."""
    job = _run(monkeypatch, r7_facts={"clip_sound": "off", "sound_from_s": 5, "sound_to_s": 8})
    assert _windows(job) == []
    assert set(_volumes(job).values()) == {1.0}
    row = _rows(job)["r7"]
    assert row["status"] == "not_possible"
    assert row["reason_code"] == "partial_unsupported"


def test_the_first_two_seconds_of_a_named_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _run(
        monkeypatch,
        r7_facts={"clip_sound": "off", "sound_media": GRILL, "sound_from_s": 0, "sound_to_s": 2},
        windows=True,
    )
    (window,) = _windows(job)
    assert window == (8.846, 10.846, (CUTS[3],))
    assert _rows(job)["r7"]["status"] == "met"


def test_a_line_that_is_not_in_the_clip_is_never_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """8: "bye bye" isn't said in Atakan's clip: nothing is kept on a guess, the receipt says so."""
    job = _run(
        monkeypatch,
        r7_facts={
            "clip_sound": "off",
            "sound_except": [{"sound_clip": "last", "sound_line": "bye bye"}],
        },
    )
    assert set(_volumes(job).values()) == {0.0}
    row = _rows(job)["r7"]
    assert row["status"] == "partial" and row["reason_code"] == "line_not_found"


def test_a_window_past_the_end_paints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """9."""
    job = _run(monkeypatch, r7_facts={"clip_sound": "off", "sound_from_s": 25, "sound_to_s": 30})
    assert set(_volumes(job).values()) == {1.0}
    row = _rows(job)["r7"]
    assert row["status"] == "not_possible" and row["reason_code"] == "window_past_end"


def test_a_targeted_ask_wins_over_a_whole_video_ask_in_any_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10: whole-video asks set the base, targeted ones come after, whatever the brief order."""
    first_on = BriefRequirement(
        id="r9",
        kind="audio",
        scope="global",
        description="keep the sound on the first clip",
        facts={"clip_sound": "on", "sound_clip": "first"},
    )
    rows = [r for r in _brief({"clip_sound": "off"}).requirements if r.id != "r7"]
    brief = CreativeBrief(
        version=1,
        requirements=[
            first_on,  # the targeted ask comes FIRST in the brief
            *rows,
            BriefRequirement(
                id="r7",
                kind="audio",
                scope="global",
                description="Clip sound off.",
                facts={"clip_sound": "off"},
            ),
        ],
    )
    job = _run(monkeypatch, brief=brief)
    volumes = _volumes(job)
    assert volumes[CUTS[0]] == 1.0
    assert all(volumes[cut] == 0.0 for cut in CUTS[1:])
    receipts = _rows(job)
    assert receipts["r7"]["status"] == "met" and receipts["r9"]["status"] == "met"


def test_turkish_wording_records_the_same_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    """13: the server reads the facts, never the words."""
    english = _run(monkeypatch)
    turkish = _run(
        monkeypatch,
        brief=_brief(OFF_EXCEPT_LINE, description="Son cümle hariç klip sesi kapalı olsun."),
    )
    assert recipe_digest(_recipe(turkish)) == recipe_digest(_recipe(english))


def test_another_route_is_judged_from_its_own_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """11: a speech-excerpt-shaped recipe (every picture clip silent, Atakan's line on an audio
    track) meets the ask without this lane ever running on it."""
    job = _run(monkeypatch, r7_facts={})
    recipe = _recipe(job)
    story = next(t for t in recipe.tracks if t.id == "story")
    last = next(c for c in story.clips if c.id == LAST)
    speech = TimelineTrack(
        id="speech",
        kind="audio",
        clips=[
            TimelineClip(
                id="excerpt-1",
                source_asset_id=ATAKAN,
                source_start=last.source_start,
                source_duration=last.source_duration,
                timeline_start=last.timeline_start,
                rate=1,
            )
        ],
    )
    silent_story = story.model_copy(
        update={"clips": [c.model_copy(update={"volume": 0.0}) for c in story.clips]}
    )
    speech_route = recipe.model_copy(update={"tracks": [silent_story, speech]})
    assert _judge(job, _brief(OFF_EXCEPT_LINE), speech_route)["r7"]["status"] == "met"


# -- what the creator reads ------------------------------------------------------------


def _r7(facts: dict = OFF_EXCEPT_LINE) -> BriefRequirement:
    return next(r for r in _brief(facts).requirements if r.id == "r7")


def test_finished_video_receipt_is_met_with_the_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """6: the reply reads the re-judged record, never "I can't verify"."""
    job = _run(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    facts = plan_facts_from_phone_variant(variant)
    receipt = check_requirement(_r7(), facts)
    assert receipt.status == "met"
    assert receipt.reason.startswith("Clip sound off except the last clip, where")
    assert "(17.0-17.9 s)" in receipt.reason  # the padded line; the 0.1 s after it snaps on
    with reply_language_for("tr"):
        turkish = check_requirement(_r7(), facts)
    assert turkish.reason.startswith("Klip sesi kapalı, şunlar hariç: son klip")


def test_finished_video_that_plays_everything_says_couldnt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6 (negative): today's recipe re-judged against the ask: "Couldn't", with the reason."""
    job = _run(monkeypatch, r7_facts={})
    variant = job.assembly_plan["variants"][0]
    rows = _judge(job, _brief(OFF_EXCEPT_LINE))
    facts = plan_facts_from_phone_variant(variant)
    facts = facts.__class__(
        **{**facts.__dict__, "clip_sound_toggles": {"rows": list(rows.values())}}
    )
    receipt = check_requirement(_r7(), facts)
    assert receipt.status in {"partial", "not_possible"}
    assert receipt.reason == "The finished video doesn't play the clip sound that way"


def _draft_facts(**overrides) -> PlanFacts:
    return PlanFacts(
        phone_montage=True,
        media_snapshot={"clip_assignments": copy.deepcopy(DATA["clips"])},
        clip_ids=tuple(c["media_id"] for c in DATA["clips"]),
        **overrides,
    )


def test_draft_says_what_it_will_do(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = check_requirement(_r7(), _draft_facts(last_clip_id=ATAKAN))
    assert receipt.status == "met"
    assert receipt.reason == ('Clip sound off except the last clip, where "Super excited!" is said')


def test_draft_says_couldnt_before_approval_for_a_line_no_clip_says() -> None:
    req = _r7({"clip_sound": "off", "sound_except": [{"sound_line": "bye bye"}]})
    receipt = check_requirement(req, _draft_facts())
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith('I couldn\'t find "bye bye" in that clip')


def test_draft_warns_when_part_of_a_clip_cant_be_silenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gb.settings, "phone_clip_sound_windows_enabled", False)
    req = _r7({"clip_sound": "off", "sound_from_s": 5, "sound_to_s": 8})
    receipt = check_requirement(req, _draft_facts())
    assert receipt.status == "partial"
    assert receipt.reason.startswith("I can't turn off just part of a clip on your iPhone yet")


def test_a_voiceover_draft_says_it_works_on_a_montage() -> None:
    receipt = check_requirement(_r7(), PlanFacts(phone_narrated=True))
    assert receipt.status == "not_possible"
    assert "works on a montage made on your iPhone" in receipt.reason


# -- the correction turn (1) ------------------------------------------------------------

TURN_1, TURN_2 = DATA["turn_strategies"]["a3e6811b"], DATA["turn_strategies"]["055a33ea"]


def _contract(strategy: dict, brief: CreativeBrief):
    binding = BriefBinding.model_validate(_binding(brief)).resolve()
    return build_render_contract(
        strategy,
        generation_id="g",
        brief=binding,
        media_snapshot={"clip_assignments": copy.deepcopy(DATA["clips"])},
    )


def test_add_the_title_turn_keeps_turn_1s_sound_choice_and_route() -> None:
    """The incident's own two strategies: turn 2 dropped `[m1_04]` and switched to "excerpts";
    its brief updates touched no sound ask, so the draft's choice (and route) stay."""
    brief = _brief(OFF_EXCEPT_LINE)
    assert sound_asks_unchanged(brief, brief)
    carried = carry_draft_sound(CreativeStrategy.model_validate(TURN_2), TURN_1)
    assert carried.montage_audio.source_media_ids == [ATAKAN]
    assert carried.voice_mode is None
    # The pinned contract now asks for the same camera sound turn 1 approved.
    turn_1 = _contract(TURN_1, brief)
    turn_2 = _contract(carried.model_dump(mode="json"), brief)
    assert turn_2.audio_source_ids == turn_1.audio_source_ids == (ATAKAN,)
    # Uncarried, the model's rewrite pins nothing (the incident).
    assert _contract(TURN_2, brief).audio_source_ids == ()


def test_a_turn_that_changes_a_sound_ask_lets_the_new_choice_stand() -> None:
    before = _brief(OFF_EXCEPT_LINE)
    after = _brief({"clip_sound": "on"}, description="Turn the clip sound back on everywhere.")
    assert not sound_asks_unchanged(before, after)
    assert not sound_asks_unchanged(None, before)  # a first turn has nothing to keep


def test_a_named_clip_that_left_the_edit_is_not_carried() -> None:
    selected = [m for m in TURN_2["selected_media_ids"] if m != ATAKAN]
    new = CreativeStrategy.model_validate({**TURN_2, "selected_media_ids": selected})
    assert carry_draft_sound(new, TURN_1) is new
    other_lane = CreativeStrategy.model_validate({**TURN_2, "audio_strategy": "licensed_music"})
    assert carry_draft_sound(other_lane, TURN_1) is other_lane


class _Db:
    def __init__(self, snapshot: dict | None) -> None:
        self.snapshot = snapshot
        self.rollbacks = 0

    async def execute(self, _statement):  # noqa: ANN001, ANN202
        return SimpleNamespace(scalar_one_or_none=lambda: self.snapshot)

    async def rollback(self) -> None:
        self.rollbacks += 1


async def test_draft_strategy_read_releases_the_transaction() -> None:
    db = _Db({"kind": "strategy", "strategy": TURN_1})
    assert await planner._load_draft_strategy(db, uuid.uuid4()) == TURN_1
    assert db.rollbacks == 1
    assert await planner._load_draft_strategy(_Db(None), uuid.uuid4()) is None
    assert await planner._load_draft_strategy(_Db({"kind": "editor"}), uuid.uuid4()) is None
