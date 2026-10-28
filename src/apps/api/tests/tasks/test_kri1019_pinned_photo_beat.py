"""KRI-1019: a Visual the creator pinned to a reaction beat is never placed anywhere else.

Real run "maximo 2" (job e46baf42, phone Talking, ~/Downloads/kria-arjantindji-test/
T2_translate_1810_mixed_language_talking): the creator asked "When he says 'on the other side
of the square', show my Casa Rosada photo full screen for about two seconds with his voice
still playing, then come back to him." The approved strategy carried one fullscreen reaction
beat for the photo.

The stored transcript past ~30 s was a Turkish translation of the English line (KRI-1016, not
this ticket), so the beat was reported `unplaced: never_heard`. The generic overlay pass then
handed the SAME photo to the Gemini overlay agent, which placed it as a corner card at
39.3-44.3 s, silently. `used_media_ids` was built from PLACED beat cards only, so an unplaced
beat reserved nothing.

The rule these tests pin: a Visual the creator named on a beat (or as the closing photo or
badge) is placed by the beat lane as asked, or reported and left out. No other lane places it
elsewhere or in another form - whether the beat was placed, unplaced for any reason, grounding
raised, a lane request was set, or beats are switched off.

They drive the REAL chain `_run_generative_job` -> `_run_phone_subtitled_job` -> beat + overlay
grounding -> compiler -> recipe validation. Only transport is replaced (Whisper words, the DB
session, the Visuals pool rows, Visuals binding, face sampling, trace writes). The overlay
placement agent is REPLAYED at its lowest level (`OverlayPlacementAgent.run`): it returns the
answer recorded in the incident and notes which assets it was offered, while the real shortlist,
`build_suggestions`, card arbitration and compiler run on top of it.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as rgm
import app.services.phone_visuals as phone_visuals_mod
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant, reply_from_receipts
from app.kria.reply_language import reply_language_for
from app.pipeline.phone_subtitled_lanes import (
    PHONE_SUBTITLED_LANES_FIELD,
    PhoneSubtitledLanes,
    SubtitledOverlayCard,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.transcribe import Word
from app.services.device_render import device_status
from app.services.phone_sources import PhoneVisualBinding
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_subtitled_plan import _binding
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _closing_dict,
    _enable_beats,
    _lane_request,
    _make_fake_bind,
    _setup_subtitled,
)

# --------------------------------------------------------------------------- recorded state

CLIP_S = 48.0
CASA = "012544bd-8842-4f3c-ad73-c8b9581d3607"  # the creator's Casa Rosada photo (pool row id)
CASA_RECIPE_ID = f"visual-{CASA}"
OTHER = "7f1c2b3a-5d4e-4a8b-9c0d-1e2f3a4b5c6d"  # an extra Visual nobody pinned
OTHER_RECIPE_ID = f"visual-{OTHER}"
TRIGGER = "on the other side of the square"

# The 42 words the overlay agent was sent (`job.agent_runs[nova.compose.overlay_placement]
# .input_json.words`): Whisper's transcript of Maximo's take. Past ~30 s it is a Turkish
# translation of what he said in English (KRI-1016).
_RECORDED_WORDS = [
    ("Okay,", 0.0, 0.72),
    ("I'll", 1.06, 1.58),
    ("translate", 1.58, 1.94),
    ("it", 1.94, 2.2),
    ("to", 2.2, 2.22),
    ("Turkish.", 2.22, 2.6),
    ("So,", 3.22, 3.34),
    ("what", 3.56, 3.94),
    ("was", 3.94, 4.1),
    ("your", 4.1, 4.24),
    ("name", 4.24, 4.38),
    ("again,", 4.38, 4.58),
    ("sorry?", 4.68, 4.96),
    ("Maximo.", 6.14, 6.34),
    ("Maximo.", 7.0, 7.54),
    ("Why", 14.46, 15.22),
    ("and", 15.22, 15.98),
    ("when", 15.98, 16.22),
    ("what?", 16.22, 18.38),
    ("You", 18.94, 19.38),
    ("gained", 19.38, 19.7),
    ("independence.", 19.7, 20.38),
    ("The", 22.44, 22.44),
    ("year", 22.44, 22.56),
    ("1810.", 22.56, 23.4),
    ("215", 24.88, 25.64),
    ("years", 25.64, 26.0),
    ("ago.", 26.0, 26.38),
    ("Neden", 31.68, 33.08),
    ("bu", 33.08, 34.12),
    ("binalarda?", 34.12, 34.24),
    ("Çünkü...", 35.16, 36.32),
    ("...bu", 36.32, 37.12),
    ("yer...", 37.12, 37.12),
    ("...Buenos", 39.3, 40.7),
    ("Aires'in...", 40.7, 40.92),
    ("Diğer", 42.54, 42.7),
    ("tarafta...", 42.7, 43.08),
    ("...şehir", 43.88, 45.28),
    ("hükümeti", 45.28, 45.78),
    ("vardı.", 45.78, 45.78),
    ("Bütün...", 46.2, 46.76),
]

# What Maximo actually said from 39 s on (truth/source_words_en.csv, ms -> s): the cue is
# "On the other side of the square" with "On" at 41.0 s.
_TRUTH_TAIL = [
    ("of", 40.0, 40.15),
    ("Buenos", 40.15, 40.61),
    ("Aires.", 40.61, 41.0),
    ("On", 41.0, 41.19),
    ("the", 41.19, 41.48),
    ("other", 41.48, 41.96),
    ("side", 41.96, 42.34),
    ("of", 42.34, 42.54),
    ("the", 42.54, 42.82),
    ("square", 42.82, 43.39),
    ("you", 43.39, 43.68),
    ("had", 43.68, 44.0),
    ("the", 44.0, 44.35),
    ("real", 44.35, 44.82),
]


def _words(rows) -> list[Word]:
    return [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in rows]


RECORDED = _words(_RECORDED_WORDS)
HEARD = _words([row for row in _RECORDED_WORDS if row[1] < 39.0] + _TRUTH_TAIL)

# What the overlay agent answered in the incident (`output_json`): the photo, top slot.
RECORDED_AGENT_OUTPUT = {
    "wishlist": [],
    "placements": [
        {
            "slot": "top",
            "end_s": 44.3,
            "reason": (
                'You say "Buenos Aires\'in" which directly references the location shown in '
                "this photograph of Plaza de Mayo in Buenos Aires."
            ),
            "start_s": 39.3,
            "asset_id": CASA,
            "sfx_intent": "pop_in",
            "confidence_tier": "confident",
            "transcript_anchor": "Buenos Aires'in",
        }
    ],
}
# The same agent answering for an UNPINNED Visual: it pops in when Maximo says his name.
OTHER_AGENT_OUTPUT = {
    "wishlist": [],
    "placements": [
        {
            "slot": "top",
            "start_s": 7.0,
            "end_s": 9.0,
            "reason": 'You say "Maximo" right here.',
            "asset_id": OTHER,
            "sfx_intent": "pop_in",
            "confidence_tier": "confident",
            "transcript_anchor": "Maximo",
        }
    ],
}

# The beat in the approved strategy, verbatim.
INCIDENT_BEAT = {
    "hold_s": 2.0,
    "beat_id": "casa-rosada",
    "trigger": TRIGGER,
    "visual_id": f"asset-{CASA}",
    "occurrence": "first",
    "visual_role": "photo",
    "display_mode": "fullscreen",
}


def _pool_row(asset_id: str, *, name: str, width: int, height: int, subject: str) -> dict:
    return {
        "id": asset_id,
        "gcs_path": f"users/u1/plan/item1/pool/{asset_id}.jpg",
        "gcs_generation": "7",
        "kind": "image",
        "source_filename": name,
        "duration_s": None,
        "aspect": round(width / height, 4),
        "user_context": "",
        "analysis": {
            "subject": subject,
            "description": f"A photograph: {subject}.",
            "on_screen_text": "",
            "brands": [],
            "width": width,
            "height": height,
        },
        "capture": None,
    }


CASA_ROW = _pool_row(
    CASA, name="casa_rosada.jpg", width=1920, height=1080, subject="Plaza de Mayo, Casa Rosada"
)
OTHER_ROW = _pool_row(OTHER, name="stamp.jpg", width=1080, height=1350, subject="A postage stamp")


# --------------------------------------------------------------------------- the check


def _placements(recipe, recipe_asset_id: str) -> list[tuple[str, object]]:
    return [
        (track.id, clip)
        for track in recipe.tracks
        for clip in track.clips
        if clip.source_asset_id == recipe_asset_id
    ]


def assert_pinned_photo_honest(
    recipe, variant, *, photo_asset_id, heard_window=None, beat_id="casa-rosada", beat_unplaced=True
):
    """The photo the creator pinned to a beat is where the beat lane put it - or nowhere.

    ``heard_window=None`` (the cue was never heard): the photo appears in NO recipe track, clip
    or overlay, the beat receipt lists the beat as unplaced (``beat_unplaced``: it records no
    such entry when beat grounding raised, a lane request was set, or beats are off), and the
    generic overlay receipt never mentions the photo.

    ``heard_window=(start_s, hold_s)`` (the cue was heard): the photo appears exactly once, full
    screen, starting within 0.3 s of the heard cue, for about ``hold_s``, and nowhere else.
    """
    recipe_asset_id = f"visual-{photo_asset_id}"
    placements = _placements(recipe, recipe_asset_id)
    beat_receipt = variant.get("phone_beat_receipt") or {}
    placed_beats = [entry.get("beat_id") for entry in beat_receipt.get("placed") or []]
    unplaced_beats = [entry.get("beat_id") for entry in beat_receipt.get("unplaced") or []]
    overlay_receipt = variant.get("phone_overlay_receipt") or {}
    overlay_mentions = [
        entry
        for key in ("placed", "unplaced")
        for entry in overlay_receipt.get(key) or []
        if entry.get("media_id") == photo_asset_id
    ]
    if heard_window is None:
        assert not placements, (
            f"the pinned photo {photo_asset_id} was placed in the recipe although its beat was "
            "not heard: "
            + ", ".join(
                f"{track_id} {clip.timeline_start:.2f}-"
                f"{clip.timeline_start + clip.source_duration:.2f}s"
                for track_id, clip in placements
            )
        )
        assert beat_id not in placed_beats
        if beat_unplaced:
            assert beat_id in unplaced_beats, (
                f"the beat receipt does not list {beat_id} as unplaced: {beat_receipt}"
            )
        assert not overlay_mentions, (
            f"the generic overlay pass was offered the pinned photo: {overlay_mentions}"
        )
        return
    start_s, hold_s = heard_window
    assert len(placements) == 1, (
        f"the pinned photo must appear exactly once; found {len(placements)}: "
        + ", ".join(f"{track_id}@{clip.timeline_start:.2f}" for track_id, clip in placements)
    )
    track_id, clip = placements[0]
    assert track_id == "subtitled-fullscreen", f"the photo is on {track_id}, not full screen"
    assert clip.visual_placement.width_fraction is None, "the photo is a corner card"
    assert abs(clip.timeline_start - start_s) <= 0.3, (
        f"the photo starts at {clip.timeline_start:.2f}s, not at the heard cue ({start_s:.2f}s)"
    )
    shown_s = clip.visual_placement.window_end - clip.visual_placement.window_start
    assert abs(shown_s - hold_s) <= 0.3, f"the photo is up for {shown_s:.2f}s, not {hold_s}s"
    assert beat_id in placed_beats, f"the beat receipt does not list {beat_id} as placed"
    assert beat_id not in unplaced_beats
    assert not overlay_mentions, (
        f"the generic overlay pass was offered the pinned photo: {overlay_mentions}"
    )


# --------------------------------------------------------------------------- harness


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # No real video to sample faces from.
    monkeypatch.setattr(ogm, "sample_face_regions", lambda *a, **k: ([], {"attempted": 0}))
    # Pipeline-trace persistence tries a (nonexistent) local Postgres.
    import app.services.pipeline_trace as pt

    monkeypatch.setattr(pt, "record_pipeline_event", lambda *a, **k: None)


@pytest.fixture
def agent(monkeypatch):
    """Replays the overlay placement agent at its lowest level and notes what it was offered."""
    import app.agents._model_client as model_client
    import app.agents.overlay_placement as placement_agent

    spy = SimpleNamespace(offered=[], output=copy.deepcopy(RECORDED_AGENT_OUTPUT))
    monkeypatch.setattr(ogm.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(model_client, "default_client", lambda: object())

    def _run(self, input, *, ctx=None):  # noqa: A002, ARG001
        spy.offered.append([asset.asset_id for asset in input.assets])
        return placement_agent.OverlayPlacementOutput.model_validate(spy.output)

    monkeypatch.setattr(placement_agent.OverlayPlacementAgent, "run", _run)
    spy.never_offered = lambda asset_id: all(asset_id not in ids for ids in spy.offered)
    return spy


def _job(
    monkeypatch,
    *,
    words=RECORDED,
    beats=(INCIDENT_BEAT,),
    closing=None,
    pool=(CASA_ROW,),
    overlay_display=None,
    lane_request=None,
):
    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch, transcript_words=words, duration_s=CLIP_S
    )
    _enable_beats(monkeypatch, extra_features=("authoredText",))
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    rows = list(pool)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: copy.deepcopy(rows))
    monkeypatch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: copy.deepcopy(rows))
    monkeypatch.setattr(rgm, "_load_sfx_entries", lambda *a, **k: [])
    strategy: dict = {"edit_format": "subtitled", "audio_strategy": "original_audio"}
    if beats:
        strategy["reaction_beats"] = [copy.deepcopy(beat) for beat in beats]
    if closing is not None:
        strategy["closing_media"] = closing
    job.all_candidates["creator_strategy"] = strategy
    if overlay_display:
        job.all_candidates["overlay_display"] = overlay_display
    if lane_request is not None:
        job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = lane_request
    return job


def _render(job):
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    return device_status(job, "subtitled").request.recipe, job.assembly_plan["variants"][0]


def _boom(*args, **kwargs):
    raise RuntimeError("beat grounding exploded")


def _without_pin_exclusion(monkeypatch):
    """The overlay pass as it ran before KRI-1019: nothing is reserved for it."""
    real = ogm.ground_phone_subtitled_overlays

    def _unreserved(*args, **kwargs):
        kwargs["used_media_ids"] = frozenset()
        return real(*args, **kwargs)

    monkeypatch.setattr(ogm, "ground_phone_subtitled_overlays", _unreserved)


# --------------------------------------------------------------------------- the incident


def test_incident_replay_the_unheard_beats_photo_is_not_handed_to_the_overlay_agent(
    monkeypatch, agent
):
    job = _job(monkeypatch)
    recipe, variant = _render(job)

    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA)
    assert agent.never_offered(CASA), f"the overlay agent was offered the photo: {agent.offered}"
    # The beat lane did report it, for the right reason.
    [unplaced] = variant["phone_beat_receipt"]["unplaced"]
    assert (unplaced["beat_id"], unplaced["reason"]) == ("casa-rosada", "never_heard")
    assert variant["phone_beat_receipt"]["placed"] == []


# --------------------------------------------------------------------------- the check itself


def test_control_the_check_rejects_the_pre_fix_outcome_a_corner_card_at_39_seconds(
    monkeypatch, agent
):
    """Run the incident with the new reservation removed: the agent places the photo as a
    corner card at 39.3-44.3 s, exactly as it did on the phone."""
    _without_pin_exclusion(monkeypatch)
    job = _job(monkeypatch)
    recipe, variant = _render(job)

    assert agent.offered == [[CASA]], "the replay no longer reproduces the incident"
    [(track_id, clip)] = _placements(recipe, CASA_RECIPE_ID)
    assert track_id == "subtitled-overlays"  # a corner card
    assert clip.timeline_start == pytest.approx(39.3, abs=0.05)
    assert clip.timeline_start + clip.source_duration == pytest.approx(44.3, abs=0.05)
    with pytest.raises(AssertionError, match="placed in the recipe although its beat"):
        assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA)


def _photo_binding() -> PhoneVisualBinding:
    return PhoneVisualBinding(
        media_id=CASA,
        gcs_path=CASA_ROW["gcs_path"],
        generation="7",
        sha256="a" * 64,
        byte_count=100,
        kind="image",
    )


def _card(card_id: str, *, start_s: float, end_s: float, display_mode: str) -> SubtitledOverlayCard:
    return SubtitledOverlayCard(
        id=card_id,
        media_id=CASA,
        gcs_path=CASA_ROW["gcs_path"],
        generation="7",
        start_s=start_s,
        end_s=end_s,
        kind="image",
        display_mode=display_mode,
    )


def _compile(*cards: SubtitledOverlayCard):
    return compile_phone_subtitled_plan(
        (_binding("c0", duration_s=CLIP_S),),
        caption_cues=[],
        visuals=(_photo_binding(),),
        lanes=PhoneSubtitledLanes(overlays=list(cards)),
    )


def _placed_receipt() -> dict:
    return {"phone_beat_receipt": {"placed": [{"beat_id": "casa-rosada"}], "unplaced": []}}


def test_control_the_check_rejects_a_full_screen_photo_at_the_wrong_time():
    recipe = _compile(_card("fs", start_s=39.3, end_s=41.3, display_mode="fullscreen"))
    with pytest.raises(AssertionError, match="not at the heard cue"):
        assert_pinned_photo_honest(
            recipe, _placed_receipt(), photo_asset_id=CASA, heard_window=(41.0, 2.0)
        )


def test_control_the_check_rejects_a_heard_photo_that_is_also_a_corner_card_elsewhere():
    recipe = _compile(
        _card("fs", start_s=41.0, end_s=43.0, display_mode="fullscreen"),
        _card("pip", start_s=39.3, end_s=44.3, display_mode="pip"),
    )
    with pytest.raises(AssertionError, match="exactly once"):
        assert_pinned_photo_honest(
            recipe, _placed_receipt(), photo_asset_id=CASA, heard_window=(41.0, 2.0)
        )


def test_control_the_check_rejects_a_heard_photo_shown_as_a_corner_card():
    recipe = _compile(_card("pip", start_s=41.0, end_s=43.0, display_mode="pip"))
    with pytest.raises(AssertionError, match="not full screen"):
        assert_pinned_photo_honest(
            recipe, _placed_receipt(), photo_asset_id=CASA, heard_window=(41.0, 2.0)
        )


def test_control_the_check_accepts_the_photo_full_screen_at_the_heard_cue():
    recipe = _compile(_card("fs", start_s=41.0, end_s=43.0, display_mode="fullscreen"))
    assert_pinned_photo_honest(
        recipe, _placed_receipt(), photo_asset_id=CASA, heard_window=(41.0, 2.0)
    )


# --------------------------------------------------------------------------- fulfilment


def test_when_the_cue_is_heard_the_photo_is_full_screen_for_two_seconds_and_nowhere_else(
    monkeypatch, agent
):
    job = _job(monkeypatch, words=HEARD)
    recipe, variant = _render(job)

    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA, heard_window=(41.0, 2.0))
    assert agent.never_offered(CASA)
    # Maximo's voice keeps playing under the photo: the speaker is still the main track.
    main = next(track for track in recipe.tracks if track.id == "subtitled")
    assert main.clips[0].volume == 1


# --------------------------------------------------------------------------- the pin survives


def test_grounding_raising_does_not_free_the_photo_for_the_overlay_agent(monkeypatch, agent):
    monkeypatch.setattr(rgm, "ground_phone_reaction_beats", _boom)
    job = _job(monkeypatch)
    recipe, variant = _render(job)

    assert variant["phone_beat_receipt"]["matcher"] == "failed"
    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA, beat_unplaced=False)
    assert agent.never_offered(CASA), agent.offered


def test_a_lane_request_without_overlays_does_not_free_the_photo(monkeypatch, agent):
    job = _job(monkeypatch, lane_request=_lane_request())
    recipe, variant = _render(job)

    assert variant["phone_beat_receipt"]["matcher"] == "manual"
    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA, beat_unplaced=False)
    assert agent.never_offered(CASA), agent.offered


def test_switched_off_reaction_beats_do_not_free_the_photo(monkeypatch, agent):
    job = _job(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_reaction_beats_enabled", False)
    recipe, variant = _render(job)

    assert "phone_beat_receipt" not in variant
    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA, beat_unplaced=False)
    assert agent.never_offered(CASA), agent.offered


@pytest.mark.parametrize("layout", ["fullscreen", "fullscreen_images"])
def test_the_full_screen_sequence_layout_does_not_place_the_pinned_photo_either(
    monkeypatch, agent, layout
):
    """The same call drives the KRI-840 even-slot sequence: it must not take the photo."""
    job = _job(monkeypatch, overlay_display=layout)
    recipe, variant = _render(job)

    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA)
    assert agent.never_offered(CASA), agent.offered


def test_the_sequence_layout_still_cuts_to_an_unpinned_visual(monkeypatch, agent):
    job = _job(monkeypatch, overlay_display="fullscreen_images", pool=(CASA_ROW, OTHER_ROW))
    recipe, variant = _render(job)

    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA)
    [(track_id, _clip)] = _placements(recipe, OTHER_RECIPE_ID)
    assert track_id == "subtitled-fullscreen"
    assert [entry["media_id"] for entry in variant["phone_overlay_receipt"]["placed"]] == [OTHER]


# --------------------------------------------------------------------------- closing media


@pytest.mark.parametrize("field", ["visual_id", "badge_visual_id"])
@pytest.mark.parametrize("how", ["grounding_raises", "beats_off"])
def test_an_unplaced_closing_photo_or_badge_is_not_freed_for_the_overlay_agent(
    monkeypatch, agent, field, how
):
    closing = _closing_dict(f"asset-{CASA}") if field == "visual_id" else {field: f"asset-{CASA}"}
    job = _job(monkeypatch, beats=(), closing=closing)
    if how == "grounding_raises":
        monkeypatch.setattr(rgm, "ground_phone_reaction_beats", _boom)
    else:
        monkeypatch.setattr(gb.settings, "phone_subtitled_reaction_beats_enabled", False)
    recipe, variant = _render(job)

    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA, beat_unplaced=False)
    assert agent.never_offered(CASA), agent.offered


# --------------------------------------------------------------------------- over-exclusion


def test_an_unpinned_visual_still_reaches_the_overlay_agent_and_can_be_placed(monkeypatch, agent):
    agent.output = copy.deepcopy(OTHER_AGENT_OUTPUT)
    job = _job(monkeypatch, pool=(CASA_ROW, OTHER_ROW))
    recipe, variant = _render(job)

    assert agent.offered == [[OTHER]], "only the unpinned Visual goes to the overlay agent"
    assert_pinned_photo_honest(recipe, variant, photo_asset_id=CASA)
    [(track_id, clip)] = _placements(recipe, OTHER_RECIPE_ID)
    assert track_id == "subtitled-overlays"
    assert clip.timeline_start == pytest.approx(7.0, abs=0.3)
    assert [entry["media_id"] for entry in variant["phone_overlay_receipt"]["placed"]] == [OTHER]


# --------------------------------------------------------------------------- malformed strategy


@pytest.mark.parametrize(
    "beats",
    [
        # sound only: no visual to reserve
        [{"beat_id": "ding", "trigger": "Maximo", "sound": "sfx-ding"}],
        # not an object
        ["on the other side of the square", None, 7],
        # a visual_id that is not a string
        [{"beat_id": "b", "trigger": "Maximo", "visual_id": None}],
        [{"beat_id": "b", "trigger": "Maximo", "visual_id": 12}],
        [{"beat_id": "b", "trigger": "Maximo", "visual_id": ["asset-x"]}],
        [{"beat_id": "b", "trigger": "Maximo", "visual_id": "   "}],
    ],
    ids=["sound_only", "non_dict", "visual_none", "visual_int", "visual_list", "visual_blank"],
)
def test_a_malformed_beat_neither_crashes_nor_reserves_anything(monkeypatch, agent, beats):
    agent.output = copy.deepcopy(OTHER_AGENT_OUTPUT)
    job = _job(monkeypatch, beats=beats, pool=(OTHER_ROW,))
    recipe, _variant = _render(job)

    assert agent.offered == [[OTHER]]  # nothing was reserved, so the Visual is still offered
    assert _placements(recipe, OTHER_RECIPE_ID)


@pytest.mark.parametrize("closing", [{"visual_id": None}, {"badge_visual_id": 5}, {}])
def test_a_malformed_closing_neither_crashes_nor_reserves_anything(monkeypatch, agent, closing):
    agent.output = copy.deepcopy(OTHER_AGENT_OUTPUT)
    job = _job(monkeypatch, beats=(), closing=closing, pool=(OTHER_ROW,))
    _recipe, _variant = _render(job)

    assert agent.offered == [[OTHER]]


@pytest.mark.parametrize(
    "spelling",
    [CASA, f"asset-{CASA}", f"ASSET-{CASA.upper()}", f"  asset-{CASA}  ", CASA.upper()],
    ids=["bare", "asset_prefix", "upper_case", "padded", "bare_upper"],
)
def test_every_spelling_of_the_pinned_id_reserves_the_photo(monkeypatch, agent, spelling):
    job = _job(monkeypatch, beats=({**INCIDENT_BEAT, "visual_id": spelling},))
    recipe, variant = _render(job)

    assert agent.never_offered(CASA), agent.offered
    assert not _placements(recipe, CASA_RECIPE_ID), variant["phone_beat_receipt"]


# --------------------------------------------------------------------------- the review copy

ASK = (
    "Show the Casa Rosada photo full screen for about two seconds when he mentions the other "
    "side of the square"
)


def _ask(description: str = ASK, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r4",
        kind="style",
        scope="global",
        description=description,
        facts=facts or {"display_mode": "fullscreen", "hold_s": 2.0, "triggers": [TRIGGER]},
    )


def _review(variant: dict, ask: BriefRequirement, language: str = "en"):
    brief = CreativeBrief(version=1, requirements=[ask])
    with reply_language_for(language):
        receipts = build_receipts(
            brief.live(), plan_facts_from_phone_variant(variant), include_unchecked=True
        )
        reply = reply_from_receipts(brief, receipts, summary="Your video is ready.")
    return {r.requirement_id: r for r in receipts}["r4"], reply


def _incident_variant(monkeypatch, agent, *, trigger: str = TRIGGER) -> dict:
    job = _job(monkeypatch, beats=({**INCIDENT_BEAT, "trigger": trigger},))
    return _render(job)[1]


def test_the_review_does_not_blame_the_creators_voice_for_what_maximo_never_said(
    monkeypatch, agent
):
    variant = _incident_variant(monkeypatch, agent)

    receipt, reply = _review(variant, _ask())
    assert receipt.status == "not_possible"
    assert receipt.reason == "I never heard on the other side of the square in the take."
    assert "Couldn't" in reply
    for text in (receipt.reason, reply):
        assert "your voice" not in text
        assert "you say" not in text.lower()


def test_the_review_copy_is_turkish_when_the_reply_is_turkish(monkeypatch, agent):
    variant = _incident_variant(monkeypatch, agent)

    receipt, reply = _review(variant, _ask(), "tr")
    assert receipt.reason == "Videoda on the other side of the square sözünü duymadım."
    assert "Sesinde" not in reply


@pytest.mark.parametrize(
    "ask",
    [
        # a named speaker in the facts
        _ask(
            display_mode="fullscreen",
            hold_s=2.0,
            triggers=[TRIGGER],
            speaker="Maximo",
        ),
        # "he" only in the wording
        _ask(display_mode="fullscreen", hold_s=2.0, triggers=[TRIGGER]),
    ],
    ids=["speaker_fact", "he_in_wording"],
)
def test_a_named_other_speaker_is_never_told_in_your_voice(monkeypatch, agent, ask):
    variant = _incident_variant(monkeypatch, agent)
    receipt, _reply = _review(variant, ask)
    assert receipt.reason == "I never heard on the other side of the square in the take."


@pytest.mark.parametrize(
    "ask",
    [
        # the creator's own cue, no speaker named
        _ask("Show the Casa Rosada photo full screen when I say the medal", triggers=["the medal"]),
        # the creator named as the speaker
        _ask(
            "Show the Casa Rosada photo full screen when the medal comes up",
            triggers=["the medal"],
            speaker="me",
        ),
        # no speaker at all
        _ask("Show the Casa Rosada photo at the medal", triggers=["the medal"]),
    ],
    ids=["i_say", "speaker_is_me", "no_speaker"],
)
def test_the_creators_own_cue_still_says_in_your_voice(monkeypatch, agent, ask):
    variant = _incident_variant(monkeypatch, agent, trigger="the medal")
    receipt, _reply = _review(variant, ask)
    assert receipt.reason == "I never heard the medal in your voice."
