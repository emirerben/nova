"""KRI-796: a photo the creator places between the moments of ONE take sits there.

Incident (prod 6b689783a, thread EE7B4DCA, 2026-10-21, Merope café kit M2): one 68 s take and
one Cotopaxi photo (a Visual). "Make a montage of six moments from it, about 2 seconds each, in
this order: Caro arriving with the black tray, her hello wave, ... Caro's bye-bye wave. Put the
Cotopaxi photo right after the hello wave for 1 second." KRI-776 (#1711) cuts the take into six
2 s windows. The phone planner still laid out TAKE x6 then PHOTO (`_scatter` puts a Visual after
the one clip and the take's windows expand in place), the sequence outcome called the photo row
`visual` / not_possible ("I found no clips of it"), r1 order went partial + checked, and the
phone worker refused the render (`needs_creator_choice` -> UnsupportedPhonePlan).

The two strategies prod recorded hold the same ask in two shapes, both below: render 1 (job
417eecd4) one `order_sequence` row listing the six moments plus `order_cotopaxi` "the Cotopaxi
photo right after the hello wave"; render 2 (job 50f67a06, the recovery turn) one `order` row per
moment with "the Cotopaxi photo" listed between the hello wave and the plate.

Ways this can fail, each pinned below:

1. The planner never reads the anchor: the photo lands after every cut of the take (incident).
2. Seated, but the receipt still reads the clip-level order (`visual`, and in render 2's shape
   `contained` for moments 2-6), so r1 stays partial and the render is still refused.
3. The receipt echoes the planner's intent instead of reading the finished cuts: a photo that is
   not where it was asked would still read "met".
4. Wrong window: the anchor matched by a shared word ("wave" is in the hello wave AND the bye-bye
   wave), off by one ("after" seated before it), or "before" read as "after".
5. Guessing: an anchor that names no moment, or two, or a moment count that is not the window
   count (6 named, 5 cut), seats the photo anyway instead of staying an honest partial.
6. Over-reach: multi-clip montages, a take without approved windows, or clip intents off change
   their layout or receipts.
7. Seated, but downstream refuses it: the snapshot, the guided compiler or the phone compiler
   reject take -> photo -> take, the photo loses its "From Ecuador" text, or the hook / payoff
   roles move off the take.
8. The words are read with a new regex (KRI-667 ratchet: `services/clip_order_sequence.py`).

No model calls: every input is the recorded strategy, brief and media of the M2 run, and the six
windows #1711's length plan produces for this take (`_enrich_best_parts_snapshot` on the recorded
media snapshot, both renders: 12.04, 18.09, 30.59, 43.09, 50.04 and 57.44 s, 2 s each).
"""

from __future__ import annotations

import copy

import pytest

from app.config import settings
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_checks import build_receipts, needs_creator_choice
from app.kria.brief_checks import plan_facts_from_unified_montage as read_plan
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import (
    UnifiedClip,
    UnifiedMontagePlan,
    brief_view,
    plan_unified_montage,
)
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding
from tests.pipeline.test_unified_montage import clip, visual

# -- the incident's own media, words and windows ---------------------------------------------

TAKE = "analysis-proxy-ios-11CF148F-2374-4097-B299-FC3926E878BD.mp4"
TAKE_PATH = (
    "users/7b56dd6f-6b9a-4634-89c8-ce91bb50a50d/creation-threads/"
    f"ee7b4dca-b2e9-44f9-a073-c7a6a2e60e62/{TAKE}"
)
TAKE_GENERATION = "1792587128136153"
PHOTO_ROW = "6ae61e1a-96ff-4776-8f95-54ace669db23"
PHOTO = f"asset-{PHOTO_ROW}"
CAPTION = "From Ecuador 🇪🇨"
MOMENTS = (
    "Caro arriving with the black tray",
    "her hello wave",
    "the plate pushed close to the camera",
    "Atakan's first bite",
    "his thumbs-up",
    "Caro's bye-bye wave",
)
WINDOWS = [
    (12.04, 14.04),
    (18.09, 20.09),
    (30.59, 32.59),
    (43.09, 45.09),
    (50.04, 52.04),
    (57.44, 59.44),
]


def _row(intent_id: str, attribute: str, media_id: str, **extra: object) -> dict:
    return {
        "op": "order",
        "status": "resolved",
        "intent_id": intent_id,
        "attribute": attribute,
        "assignments": [{"media_id": media_id, "confidence": 0.9, "evidence": "recorded"}],
        **extra,
    }


CAPTION_INTENT = {
    "op": "caption",
    "status": "resolved",
    "intent_id": "caption_cotopaxi",
    "attribute": "the Cotopaxi photo",
    "assignments": [{"media_id": PHOTO, "confidence": 0.9, "evidence": "Snowy mountain"}],
    "caption_text": CAPTION,
    "creator_text": CAPTION,
    "caption_grounding": "creator_text",
}


def _strategy(intents: list[dict]) -> dict:
    """The recorded M2 strategy (render 1's fields) with ``intents`` as its clip intents."""
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "opening_title": "Colombian dessert, Ecuadorian hands",
        "target_duration_s": 14,
        "video_reuse_policy": "once",
        "resolved_clip_intents": intents,
    }


def one_list(anchor: str = "the Cotopaxi photo right after the hello wave") -> dict:
    """Render 1 (job 417eecd4): one row lists the six moments, one row anchors the photo."""
    return _strategy(
        [
            _row("order_sequence", ", ".join(MOMENTS), TAKE),
            _row("order_cotopaxi", anchor, PHOTO),
            CAPTION_INTENT,
        ]
    )


def row_per_moment() -> dict:
    """Render 2 (job 50f67a06, the recovery turn): one row per moment, the photo's row
    between the hello wave and the plate."""
    rows = [_row(f"order_{i}", words, TAKE) for i, words in enumerate(MOMENTS)]
    rows.insert(2, _row("order_cotopaxi_photo", "the Cotopaxi photo", PHOTO))
    rows.insert(3, CAPTION_INTENT)
    return _strategy(rows)


def m2_brief() -> CreativeBrief:
    """Brief v1 as recorded (r1-r7) plus KRI-776's moment requirement (r8)."""
    rows = [
        {
            "kind": "order",
            "scope": "global",
            "description": (
                "Caro arriving with tray, her hello wave, Cotopaxi photo, plate pushed close, "
                "Atakan's bite, his thumbs-up, Caro's bye-bye wave"
            ),
        },
        {
            "kind": "timing",
            "scope": f"clip:{PHOTO}",
            "description": "1 second",
            "facts": {"duration_s": 1},
        },
        {
            "kind": "timing",
            "scope": "global",
            "description": "Freeze the frame for one second on Atakan's bite",
            "facts": {"duration_s": 1, "freeze_frame": True},
        },
        {
            "kind": "audio",
            "scope": "global",
            "description": "Keep the original sound, no music",
            "facts": {"audio_intent": "source_unchanged"},
        },
        {"kind": "text", "scope": "title", "literal": "Colombian dessert, Ecuadorian hands"},
        {"kind": "text", "scope": f"clip:{PHOTO}", "literal": CAPTION},
        {
            "kind": "timing",
            "scope": "global",
            "description": "About 14 seconds",
            "facts": {"duration_s": 14},
        },
        {
            "kind": "select",
            "scope": "global",
            "description": (
                "Make a montage of six moments from it, about 2 seconds each, in this order"
            ),
            "facts": {"moment_count": 6, "moment_length_s": 2.0},
        },
    ]
    updates = [BriefUpdate(operation="add", **row) for row in rows]
    return apply_updates(None, updates, source_turn_id="t").model_copy(update={"version": 1})


def length_plan(
    windows: list[tuple[float, float]] = WINDOWS, *, strategy: dict | None = None
) -> dict:
    """The approved length plan #1711 builds for the take (version 1 envelope).

    KRI-723: with ``strategy``, the same windows as the creator pointed at them (envelope v2,
    one ``creator_answer`` moment per window in the strategy's named-moment order), the only
    plan whose windows are evidence of the named actions. Ordinal windows (v1) are not.
    """
    plan = _plain_length_plan(windows)
    if strategy is None:
        return plan
    from app.services.clip_order_sequence import sequence_rows  # noqa: PLC0415
    from app.services.named_moments import _listed  # noqa: PLC0415

    named = [
        (index, words)
        for index, (_pos, name, members, _status) in enumerate(
            sequence_rows(strategy["resolved_clip_intents"])
        )
        if members == [TAKE]
        for words in _listed(name)
    ]
    if len(named) != len(windows):
        return plan
    for window in plan["windows"]:
        window["reason"] = "creator_answer"
        window["score"] = 0.0
    plan["version"] = 2
    plan["moments"] = [
        {
            "moment_key": f"m{i}",
            "words": words,
            "row": row,
            "origin": "creator_answer",
            "media_id": TAKE,
            "source_start_s": start,
            "source_end_s": end,
            "window_index": i,
        }
        for i, ((row, words), (start, end)) in enumerate(zip(named, windows, strict=True))
    ]
    return plan


def _plain_length_plan(windows: list[tuple[float, float]]) -> dict:
    return {
        "version": 1,
        "owner": "footage",
        "natural_owner": "footage",
        "target_duration_s": None,
        "natural_duration_s": None,
        "capacity_raw_s": 68.37,
        "window_eligible": True,
        "capacity_s": 68.37,
        "max_source_count": 80,
        "sources": [
            {
                "media_id": TAKE,
                "generation": None,
                "storage_generation": TAKE_GENERATION,
                "gcs_path": TAKE_PATH,
                "kind": "video",
                "duration_s": 68.37,
            }
        ],
        "windows": [
            {
                "media_id": TAKE,
                "source_start_s": start,
                "source_end_s": end,
                "score": 6.0,
                "reason": "best_moment",
            }
            for start, end in windows
        ],
        "conflicts": [],
    }


def take() -> UnifiedClip:
    """The take as the phone worker builds it (original duration, not the proxy's)."""
    return UnifiedClip(
        media_id=TAKE,
        proxy_path=TAKE_PATH,
        generation=TAKE_GENERATION,
        duration_s=68.367,
        width=1080,
        height=1920,
    )


def photo() -> UnifiedClip:
    """The Cotopaxi photo as the worker's Visuals loader builds it (900x1600 JPEG)."""
    return UnifiedClip(
        media_id=PHOTO_ROW,
        proxy_path=f"users/7b56dd6f-6b9a-4634-89c8-ce91bb50a50d/plan-item-assets/{PHOTO_ROW}.jpg",
        generation="1",
        duration_s=0.0,
        analysis={"summary": "Snowy mountain at sunrise"},
        lane="asset",
        kind="image",
        manifest_id=PHOTO,
        aspect=0.5625,
        source_filename="M2_02_visual.jpg",
    )


def plan_m2(
    strategy: dict,
    *,
    windows: list[tuple[float, float]] | None = WINDOWS,
    evidenced: bool = True,
) -> UnifiedMontagePlan:
    """``evidenced`` (KRI-723): the windows are the creator's pointed moments (v2)."""
    return plan_unified_montage(
        [take()],
        brief_view(m2_brief()),
        strategy=copy.deepcopy(strategy),
        clip_intents_enabled=True,
        visuals=[photo()],
        approved_length_plan=(
            length_plan(windows, strategy=strategy if evidenced else None)
            if windows is not None
            else None
        ),
    )


def layout(plan: UnifiedMontagePlan) -> list[object]:
    """The finished cut order: a take window as its (start, end), the photo as "PHOTO"."""
    return [
        (cut.source_start_s, cut.source_end_s) if cut.media_id == TAKE else "PHOTO"
        for cut in plan.snapshot.fast_cuts
    ]


def receipts(plan: UnifiedMontagePlan) -> dict[str, dict]:
    """The phone worker's own judgment of the plan (`_run_phone_unified_montage_job`)."""
    rows = build_receipts(m2_brief().live(), read_plan(plan.record()), include_unchecked=True)
    return {row.requirement_id: row.model_dump(mode="json") for row in rows}


def order_rows(plan: UnifiedMontagePlan) -> list[dict]:
    return [row for row in plan.record()["intent_outcomes"] if row.get("op") == "order"]


SEATED = [*WINDOWS[:2], "PHOTO", *WINDOWS[2:]]
TODAY = [*WINDOWS, "PHOTO"]


@pytest.fixture(autouse=True)
def _prod_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


# -- 1, 2: both recorded shapes seat the photo after the hello wave, and r1 is met ------------


@pytest.mark.parametrize(
    "strategy", [one_list(), row_per_moment()], ids=["job-417eecd4", "job-50f67a06"]
)
def test_the_photo_plays_right_after_the_hello_wave_and_the_order_is_met(strategy: dict) -> None:
    plan = plan_m2(strategy)

    assert layout(plan) == SEATED
    assert all(row["status"] == "met" for row in order_rows(plan)), order_rows(plan)
    judged = receipts(plan)
    assert judged["r1"]["status"] == "met", judged["r1"]
    blocking = [rid for rid, row in judged.items() if needs_creator_choice(row)]
    assert "r1" not in blocking, judged["r1"]


def test_the_photo_row_says_which_moment_it_follows() -> None:
    photo_row = next(row for row in order_rows(plan_m2(one_list())) if "Cotopaxi" in row["name"])
    assert (photo_row["status"], photo_row["code"], photo_row["moment"]) == ("met", "landed", 2)


# -- 3: the receipt is read off the finished cuts ---------------------------------------------


def test_a_photo_the_cuts_do_not_hold_where_asked_is_never_met() -> None:
    from app.pipeline.unified_montage import _sequence_outcomes  # noqa: PLC0415
    from app.services.clip_order_sequence import moment_seats, sequence_rows  # noqa: PLC0415

    rows = sequence_rows(one_list()["resolved_clip_intents"])
    seats = moment_seats(rows, windows={TAKE: 6}, visuals={PHOTO})
    assert seats is not None and seats.after == {PHOTO: 2}
    ordered = [take(), photo()]
    # The cuts as they were before this fix: the photo after all six windows.
    stale = _sequence_outcomes(
        rows,
        ordered,
        seats=seats,
        cuts=[(TAKE, start) for start, _end in WINDOWS] + [(PHOTO, 0.0)],
        spans=[(TAKE, start, end) for start, end in WINDOWS] + [(PHOTO, 0.0, 1.0)],
        moment_record=length_plan(strategy=one_list())["moments"],
    )
    assert [row["status"] for row in stale] == ["met", "partial"]
    assert stale[1]["code"] == "misplaced"


# -- 4: the anchor names one moment, by its own words, before or after ------------------------


@pytest.mark.parametrize(
    ("anchor", "after"),
    [
        ("the Cotopaxi photo right after her hello wave", 2),
        ("the Cotopaxi photo right after the hello wave", 2),
        ("the Cotopaxi photo right after the hello wave for 1 second", 2),
        ("the photo after his thumbs-up", 5),
        ("the photo right before Atakan's first bite", 3),
        ("the photo just before the plate pushed close", 2),
        ("the Cotopaxi photo after Caro's bye-bye wave", 6),
        ("the photo after the black tray", 1),
    ],
)
def test_the_anchor_picks_its_moment(anchor: str, after: int) -> None:
    plan = plan_m2(one_list(anchor))
    assert layout(plan) == [*WINDOWS[:after], "PHOTO", *WINDOWS[after:]]
    assert receipts(plan)["r1"]["status"] == "met"


# -- 5: no guess when the words or the counts don't settle it --------------------------------


@pytest.mark.parametrize(
    "anchor",
    [
        "the Cotopaxi photo right after the wave",  # the hello wave or the bye-bye wave
        "the Cotopaxi photo right after the sunset",  # no such moment
        "the Cotopaxi photo right before Caro arriving with the black tray",  # would open the video
        "the Cotopaxi photo",  # no anchor at all
    ],
)
def test_an_anchor_that_does_not_name_one_moment_stays_an_honest_partial(anchor: str) -> None:
    plan = plan_m2(one_list(anchor))
    assert layout(plan) == TODAY
    photo_row = next(row for row in order_rows(plan) if row["name"].endswith(anchor))
    assert (photo_row["status"], photo_row["code"]) == ("not_possible", "visual")
    assert receipts(plan)["r1"]["status"] == "partial"


@pytest.mark.parametrize("strategy", [one_list(), row_per_moment()], ids=["one-list", "row-each"])
def test_six_named_moments_over_five_windows_are_not_guessed(strategy: dict) -> None:
    plan = plan_m2(strategy, windows=WINDOWS[:5])
    assert layout(plan) == [*WINDOWS[:5], "PHOTO"]
    assert receipts(plan)["r1"]["status"] == "partial"


# -- KRI-723: ordinal windows are no evidence of the named actions ----------------------------


@pytest.mark.parametrize("strategy", [one_list(), row_per_moment()], ids=["one-list", "row-each"])
def test_ordinal_windows_seat_the_photo_but_never_claim_the_moments(strategy: dict) -> None:
    """The v1 layout is unchanged (KRI-796's seat), but nobody located those actions: an honest
    format limit on r1 that does not block the render, never "met"."""
    plan = plan_m2(strategy, evidenced=False)
    assert layout(plan) == SEATED
    assert {row["code"] for row in order_rows(plan)} == {"not_located"}
    judged = receipts(plan)
    assert judged["r1"]["status"] == "partial", judged["r1"]
    assert not needs_creator_choice(judged["r1"]), judged["r1"]


# -- 6: everything else lays out and judges exactly as before ---------------------------------


def test_a_take_without_approved_windows_keeps_todays_layout() -> None:
    plan = plan_m2(one_list(), windows=None)
    assert [cut.media_id for cut in plan.snapshot.fast_cuts] == [TAKE, PHOTO_ROW]
    assert [row["code"] for row in order_rows(plan)] == ["landed", "visual"]


def test_clip_intents_off_keeps_todays_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_unified_montage(
        [take()],
        brief_view(m2_brief()),
        strategy=one_list(),
        clip_intents_enabled=False,
        visuals=[photo()],
        approved_length_plan=length_plan(),
    )
    assert layout(plan) == TODAY
    assert plan.record().get("intent_outcomes", []) == []


def test_a_multi_clip_montage_keeps_its_photo_scatter_and_receipts() -> None:
    clips = [clip(i) for i in range(4)]
    pool = visual(1)
    strategy = {
        "resolved_clip_intents": [
            _row("order_rest", "c0, c1, c2, c3", "c0"),
            _row("order_photo", "the photo right after c1", pool.manifest_id),
        ]
    }
    plan = plan_unified_montage(clips, strategy=strategy, clip_intents_enabled=True, visuals=[pool])
    assert [cut.media_id for cut in plan.snapshot.fast_cuts] == [
        "c0",
        "c1",
        pool.media_id,
        "c2",
        "c3",
    ]
    assert [row["code"] for row in order_rows(plan)] == ["landed", "visual"]
    moved = plan_unified_montage(
        clips,
        strategy={
            "resolved_clip_intents": [
                _row("order_rest", "c0, c1, c2, c3", "c0"),
                _row("order_photo", "the photo right after c3", pool.manifest_id),
            ]
        },
        clip_intents_enabled=True,
        visuals=[pool],
    )
    assert [cut.media_id for cut in moved.snapshot.fast_cuts] == [
        "c0",
        "c1",
        pool.media_id,
        "c2",
        "c3",
    ]


# -- 7: the seated plan survives every compiler and keeps the photo's text --------------------


def test_the_seated_plan_compiles_for_the_phone_with_the_photo_after_the_hello_wave() -> None:
    plan = plan_m2(one_list())
    cuts = plan.snapshot.fast_cuts
    assert (cuts[0].media_id, cuts[0].role) == (TAKE, "hook")
    assert (cuts[-1].media_id, cuts[-1].role) == (TAKE, "payoff")
    assert [
        label.text for label in plan.snapshot.clip_labels or [] if label.media_id == PHOTO_ROW
    ] == [CAPTION]
    snapshot = EditProposalSnapshot.model_validate(plan.snapshot.model_dump(mode="json"))
    assert [cut.media_id for cut in snapshot.fast_cuts] == [cut.media_id for cut in cuts]
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    timeline = [moment["media_id"] for moment in execution["story_timeline"]]
    assert timeline == [TAKE, TAKE, PHOTO_ROW, TAKE, TAKE, TAKE, TAKE]

    take_binding = PhoneSourceBinding(
        media_id=TAKE,
        proxy_path=TAKE_PATH,
        generation=TAKE_GENERATION,
        original=OriginalMediaDescriptor(
            sha256="66b42816bfdab0cb46b935a14a7d08bddd5ef85b6b9b911328d6d3306af0ae99",
            byte_count=55291210,
            duration_s=68.367,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )
    photo_binding = PhoneVisualBinding(
        media_id=PHOTO_ROW,
        gcs_path=photo().proxy_path,
        generation="1",
        sha256="b" * 64,
        byte_count=2048,
    )
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(execution), (take_binding,), (photo_binding,)
    )
    picture = sorted(
        (c for track in recipe.tracks if track.kind == "video" for c in track.clips),
        key=lambda c: c.timeline_start,
    )
    assert len(picture) == 7
    still = picture[2]
    assert still.source_asset_id != picture[1].source_asset_id
    assert still.timeline_start == pytest.approx(4.0, abs=1 / 30)
    assert [
        round(c.source_start, 2) for c in picture if c.source_asset_id == picture[0].source_asset_id
    ] == [start for start, _end in WINDOWS]
