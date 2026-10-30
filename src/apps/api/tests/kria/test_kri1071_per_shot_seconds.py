"""KRI-1071: one shot's "for N seconds" is never the video's length.

Leverkusen kit M5 (2026-10-30, found on main d2fe95dc0 before a live run): "... Show the whole
line-up sheet for 3 seconds so people can read the names, don't crop it. Make it 20 seconds."
`recognize_total_duration_s` tried `(for|lasting) N seconds` before `make it N seconds`
anywhere in the message, so it returned 3. `MainCreatorAgent.parse` stamps that reading as
the creator's own length (`creator_duration_s`, `target_duration_requested`) and writes it over
the model's `target_duration_s`: the strategy said a 3-second video. Every reader that does not
get a single length from the brief falls back to that stamp.

End to end on main (tests below, offline): with the brief filing the sheet as a photo hold
the phone plan was still 20 s, but the saved draft strategy said 3, and approval dispatch
(`generative_jobs.py`, which re-reads the binding's request with the same reader) stamped 3
into the job the cloud builder sizes from. Without "Make it 20 seconds" the shot's 3 became the
proven length outright: length plan 3 s, render contract 3 s, a ~6 s planned video.

Fix: the reader no longer reads "for/lasting N seconds" at all. Narrowing it with more word
rules is what the KRI-667 language policy forbids (the guard counts them); the brief already
records a shot's hold apart from the video's length, so "make a video for 30 seconds" now
reaches the plan through the brief's `timing/global` requirement, like "about 30 seconds".

Ways this could go wrong, written down before the code (5 and 8 were restated when the fix
changed from narrowing the rule to removing it):

1. A per-shot or per-moment "for N seconds" (show / hold / keep on screen / put / play / freeze
   / slow motion / a title) with N in 3..240 is returned as the whole video's length (M5: 3).
2. Position does not matter: the "for" pattern ran over the whole text first, so the shot's
   seconds won whether they came before or after "make it N seconds".
3. A per-shot N below 3 short-circuits the reader to None, so the real length stated in the
   same message is lost ("hold the photo for 2 seconds ... make it 15 seconds" -> None).
4. Across turns (`length_policy.creator_duration`, newest message first): a newer per-shot ask
   replaces an older real length (turn 2 "show the sheet for 3 seconds" -> 3, not turn 1's
   20), or a short-circuited newer message hands the length to a stale older one (30, not the
   new 15).
5. A real whole-video "for N seconds" ("make a video for 30 seconds") stops being the video's
   length: the brief's total must still reach the length plan, the render contract and the
   planned montage, and the strategy must keep the model's own 30. The other phrasings
   ("make it N seconds", "an N-second video", "a total of N seconds", Turkish "toplam 30
   saniye olsun", "30 saniyelik video", "video 20 saniye olsun") must still be read.
6. The fix reads something new as a length: "the first 5 seconds", "every 2 seconds", "2
   seconds each", "about 20 seconds", "under 10 seconds" stay exactly as they were.
7. A Turkish or German per-shot ask becomes the length ("kadro kağıdını 3 saniye göster",
   "Zeig das Aufstellungsblatt 3 Sekunden lang"), or stops the real one in the same message.
8. A still-read but out-of-range length ("make it 2 seconds") becomes a number instead of None.
9. The server stamp still overrides what the model read: the strategy the approval card is
   built from says 3 while the creator asked for 20.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria.brief import BriefUpdate, apply_updates
from app.kria.brief_binding import BriefBinding
from app.kria.planner import _enrich_best_parts_snapshot
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.schemas.edit_proposal import recognize_total_duration_s
from app.services.creator_render_contract import build_render_contract
from app.services.length_policy import creator_duration_s
from tests.pipeline.test_unified_montage import clip, visual

# ~/Downloads/kria-leverkusen-test/M5_private_sound_and_lineup_sheet_montage/PROMPT.txt, verbatim.
M5 = (
    "Pre-match montage in this order: the train, the warm-up, the crowd, the light show, the "
    "line-up sheet, the away fans. The warm-up clip has a private conversation on it, so mute "
    "it completely. Keep the sound on the train announcement and on the stadium announcer "
    "reading the line-up; mute the rest. Show the whole line-up sheet for 3 seconds so people "
    "can read the names, don't crop it. Make it 20 seconds."
)


def _manifest() -> ResolvedCreatorManifest:
    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="item-m5",
        edit_format="montage",
        render_program="guided",
        media=[
            *(
                CreatorMediaRef(media_id=f"clip-v{index}", kind="video", duration_s=8.0)
                for index in range(1, 6)
            ),
            CreatorMediaRef(media_id="asset-p5-lineup", kind="image"),
        ],
        capabilities={
            "edit_format:montage": available,
            "draft_guided_proposal": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _propose(message: str, *, model_target_s: float, history: tuple[str, ...] = ()) -> dict:
    """Run the server's post-processing of a Main Creator answer (no model call)."""

    manifest = _manifest()
    agent_input = MainCreatorInput(
        user_message=message,
        conversation=[{"role": "user", "content": text} for text in history],
        media_context=[{"media_id": media.media_id} for media in manifest.media],
        capability_manifest=manifest,
        brief_enabled=True,
    )
    raw = json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "guided_story",
                    "edit_format": "montage",
                    "audio_strategy": "original_audio",
                    "render_program": "guided",
                    "selected_media_ids": [],
                    "target_duration_s": model_target_s,
                    "rationale": "The creator asked for this length.",
                },
                "summary": "A pre-match montage in the order you gave.",
            }
        }
    )
    output = MainCreatorAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]
    assert isinstance(output.action, ProposeStrategy)
    return output.action.strategy.model_dump(mode="json")


# -- 1, 2, 9: the reported M5 message ---------------------------------------------------


def test_m5_is_a_20_second_video() -> None:
    assert recognize_total_duration_s(M5) == 20
    assert creator_duration_s(creator_texts=[M5]) == 20


def test_m5_strategy_keeps_the_creators_20_seconds() -> None:
    # The model read 20; the server's stamp must agree, never overwrite it with the sheet's 3.
    strategy = _propose(M5, model_target_s=20)
    assert strategy["creator_duration_s"] == 20
    assert strategy["target_duration_s"] == 20
    assert strategy["target_duration_requested"] is True


PER_SHOT = [
    "Show the whole line-up sheet for 3 seconds so people can read the names",
    "Show the line-up sheet for 3 seconds",
    "Hold the title for 3 seconds",
    "Keep the bus photo on screen for 4 seconds",
    "Put the Cotopaxi photo right after the hello wave for 3 seconds",
    "Play the stadium announcer for 5 seconds",
    "Slow motion for 3 seconds on the jump",
    "Freeze the frame on the goal for 3 seconds",
    "Hold on the sunset for 3 seconds",
    "Open with the train for 4 seconds, then the crowd",
    "The line-up sheet for 3 seconds",
    "Show the bus video for 3 seconds",
    "Show my Casa Rosada photo full screen for 3 seconds with his voice still playing",
    "Open with '1 CITY. 7 DAYS. 7 VIDEOS.' for 3 seconds",
]


@pytest.mark.parametrize("ask", PER_SHOT)
def test_a_shots_seconds_are_never_the_videos_length(ask: str) -> None:
    assert recognize_total_duration_s(ask) is None
    assert recognize_total_duration_s(f"{ask}.") is None


@pytest.mark.parametrize("ask", PER_SHOT)
@pytest.mark.parametrize("length", ["Make it 20 seconds.", "I want a 20-second video."])
def test_the_real_length_wins_before_or_after_a_shots_seconds(ask: str, length: str) -> None:
    assert recognize_total_duration_s(f"{ask}. {length}") == 20
    assert recognize_total_duration_s(f"{length} {ask}.") == 20


# -- 3: a short per-shot number no longer hides the real length -------------------------


@pytest.mark.parametrize(
    "ask",
    [
        "Hold the photo for 2 seconds and make it 15 seconds.",
        "Freeze the frame for 1 second on the bite. Make it 15 seconds.",
        "Make it 15 seconds. Show the ticket for 2 seconds.",
        "Keep the beach photo on screen for 1.5 seconds, make it 15 seconds",
    ],
)
def test_a_short_shot_hold_does_not_hide_the_length(ask: str) -> None:
    assert recognize_total_duration_s(ask) == 15


# -- 4: across turns -------------------------------------------------------------------


def test_a_newer_shot_hold_keeps_the_earlier_length() -> None:
    turns = ["Make it 20 seconds.", "Now show the line-up sheet for 3 seconds."]
    assert creator_duration_s(creator_texts=turns) == 20
    strategy = _propose(turns[1], model_target_s=20, history=(turns[0],))
    assert strategy["creator_duration_s"] == 20
    assert strategy["target_duration_s"] == 20


def test_a_newer_length_beside_a_short_hold_replaces_the_old_one() -> None:
    turns = ["Make it 30 seconds.", "Hold the photo for 2 seconds and make it 15 seconds."]
    assert creator_duration_s(creator_texts=turns) == 15


def test_a_shot_hold_alone_leaves_the_length_to_the_model() -> None:
    strategy = _propose("Show the line-up sheet for 3 seconds.", model_target_s=18)
    assert strategy.get("creator_duration_s") is None
    assert strategy.get("target_duration_requested") is None
    assert strategy["target_duration_s"] == 18


# -- 5, 8: a real whole-video length ------------------------------------------------


@pytest.mark.parametrize(
    ("ask", "seconds"),
    [
        ("Show the line-up sheet for 3 seconds. Make it 30 seconds.", 30),
        ("Make it 20 seconds", 20),
        ("Make a 3-second edit and alternate every 1 second", 3),
        ("I want a 10-second video", 10),
        ("A total of 40 seconds", 40),
        ("Make it 120 seconds", 120),
        ("toplam 30 saniye olsun", 30),
        ("30 saniyelik video yap", 30),
        ("video 20 saniye olsun", 20),
    ],
)
def test_a_whole_video_length_is_still_read(ask: str, seconds: float) -> None:
    assert recognize_total_duration_s(ask) == seconds


@pytest.mark.parametrize("ask", ["Make it 2 seconds", "make it 300 seconds"])
def test_an_out_of_range_length_is_none(ask: str) -> None:
    assert recognize_total_duration_s(ask) is None


# -- 6: nothing new becomes a length ---------------------------------------------------


@pytest.mark.parametrize(
    "ask",
    [
        "Add a hook title in the first 5 seconds",
        "Show the title for the first 5 seconds",
        "Alternate the clips every 3 seconds",
        "Six moments, about 3 seconds each",
        "About 20 seconds",
        "Keep it under 10 seconds",
        "Use the first 5 seconds of the train clip",
    ],
)
def test_other_seconds_stay_unread(ask: str) -> None:
    assert recognize_total_duration_s(ask) is None


# -- 7: Turkish and German -------------------------------------------------------------


@pytest.mark.parametrize(
    "ask",
    [
        "Kadro kağıdını 3 saniye göster",
        "Fotoğrafı 4 saniye ekranda tut",
        "Golde kareyi 3 saniye dondur",
        "Zeig das ganze Aufstellungsblatt 3 Sekunden lang",
        "Halte das Foto für 3 Sekunden",
    ],
)
def test_turkish_and_german_shot_seconds_are_not_the_length(ask: str) -> None:
    assert recognize_total_duration_s(ask) is None
    assert recognize_total_duration_s(f"{ask}. Make it 20 seconds.") == 20


def test_turkish_shot_seconds_beside_a_turkish_length() -> None:
    assert (
        recognize_total_duration_s("Kadro kağıdını 3 saniye göster. Video 20 saniye olsun.") == 20
    )


# -- 9, end to end: draft -> length plan -> approval dispatch -> contract -> phone plan ---

CLIPS = [clip(index, duration=9.0) for index in range(5)]
SHEET = visual(1)
# What the brief extractor records for a one-photo length (KRI-824's recorded model shape).
SHEET_HOLD = BriefUpdate(
    kind="timing",
    scope=f"clip:{SHEET.manifest_id}",
    description="Show the whole line-up sheet for 3 seconds",
    facts={"duration_s": 3},
)
TOTAL = BriefUpdate(
    kind="timing", scope="global", description="Make it 20 seconds", facts={"duration_s": 20}
)


def _approve(message: str, updates: list[BriefUpdate], *, model_target_s: float) -> dict:
    proposed = _propose(message, model_target_s=model_target_s)
    strategy = {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "fast",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        **{
            key: proposed[key]
            for key in ("creator_duration_s", "target_duration_s", "target_duration_requested")
            if proposed.get(key) is not None
        },
    }
    brief = apply_updates(None, updates, source_turn_id="t1", creator_text=message)
    brief = brief.model_copy(update={"version": 1})
    snapshot = _enrich_best_parts_snapshot(
        strategy,
        {
            "clip_assignments": [
                {
                    "media_id": c.media_id,
                    "kind": "video",
                    "gcs_path": c.proxy_path,
                    "storage_generation": c.generation,
                    "duration_s": c.duration_s,
                    "analysis": {},
                }
                for c in CLIPS
            ]
        },
        brief,
        visual_photo_ids=[SHEET.manifest_id],
    )
    binding = BriefBinding.create(
        uuid.uuid4(),
        brief,
        latest_message="Continue",
        full_creator_request=message,
        media_snapshot=snapshot,
    )
    resolved = binding.resolve()
    plan = plan_unified_montage(
        CLIPS,
        brief_view(resolved),
        strategy=strategy,
        visuals=[SHEET],
        creator_duration_proven=creator_duration_s(brief=resolved) is not None,
        approved_length_plan=snapshot.get("length_plan"),
    )
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=snapshot
    )
    return {
        "strategy": strategy,
        "length_plan": snapshot.get("length_plan") or {},
        # Approval dispatch (`services/generative_jobs.py`) re-reads the bound request.
        "dispatch_s": recognize_total_duration_s(binding.creator_request),
        "contract_s": contract.duration_s if contract is not None else None,
        "planned_s": plan.duration_s,
    }


def test_m5_is_20_seconds_from_draft_to_dispatch_and_plan() -> None:
    approved = _approve(M5, [SHEET_HOLD, TOTAL], model_target_s=20)
    assert approved["strategy"]["creator_duration_s"] == 20
    assert approved["strategy"]["target_duration_s"] == 20
    assert approved["dispatch_s"] == 20
    assert approved["contract_s"] == pytest.approx(20.0)
    assert approved["planned_s"] == pytest.approx(20.0, abs=1 / 30)


@pytest.mark.parametrize(
    "message",
    [
        "Pre-match montage of these clips. Make a video for 30 seconds.",
        "A montage lasting 30 seconds, in the order I filmed it.",
    ],
)
def test_a_whole_video_for_n_seconds_is_still_the_videos_length(message: str) -> None:
    # The brief records it as the video's length; nothing on the way drops it, and the plan
    # is the one "Make it 30 seconds." gets.
    def approve(text: str) -> dict:
        total = TOTAL.model_copy(update={"description": text, "facts": {"duration_s": 30}})
        return _approve(text, [total], model_target_s=30)

    approved, make_it = approve(message), approve("Make it 30 seconds.")
    assert approved["strategy"]["target_duration_s"] == 30
    assert approved["length_plan"] == make_it["length_plan"]
    assert approved["contract_s"] == make_it["contract_s"] == pytest.approx(30.0)
    assert approved["planned_s"] == make_it["planned_s"] == pytest.approx(30.0, abs=1 / 30)


def test_a_shot_hold_alone_never_plans_a_3_second_video() -> None:
    message = M5.replace(" Make it 20 seconds.", "")
    approved = _approve(message, [SHEET_HOLD], model_target_s=18)
    assert approved["strategy"].get("creator_duration_s") is None
    assert approved["dispatch_s"] is None
    assert approved["length_plan"].get("target_duration_s") is None
    assert approved["contract_s"] is None
    assert approved["planned_s"] > 6.0
