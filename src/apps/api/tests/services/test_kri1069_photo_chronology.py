"""KRI-1069: a dated Visuals photo in a filming-order montage is dated, seated and verified.

Prod thread 4144da86 (2026-10-29, Leverkusen-Nürnberg DFB-Pokal, phone Montage): 9 iPhone
clips attached out of filming order, plus Visuals photos, one of them the printed lineup
sheet (IMG_1038, taken 20:33:20Z). The creator asked for "chronological order from arrival
to the final whistle" and to "open on the lineup sheet". Every clip and every photo carried
a capture time, yet turn 3 answered "I need capture times for every selected clip to
verify chronological order." The render contract only counted clip rows, so the selected
``asset-`` photo looked like a clip with no time, and the photo's own capture time was
never read.

Ways this can fail, written before the test (each is pinned below):

1. A selected, dated photo still reads as "capture times missing" (the incident).
2. The photo's date is ignored: the planner spreads it evenly between the clips while the
   contract pins capture order (or the reverse), so the phone verifier refuses the render
   after approval.
3. The phone verifier cannot name a Visuals photo in the picture, so every ordered montage
   with a photo is refused ("unverified picture source").
4. A first pin on the photo ("open on the lineup sheet") is seated by the contract but not
   by the planner (the sequence ran before the photos joined), or the plan's own receipt
   judges a photo group as "no clips of it" and blocks the render.
5. An undated photo or clip is not named, or the line still says "every selected clip".
6. A snapshot that lists no Visuals (written before this fix), or a format that never orders
   photos by time, falls back to a false "capture times" line.
7. With no explicit selection the planner places every Visual, but the contract pins only
   the clips (or the reverse).
8. A Turkish chat gets the English line, or a refusal over the line loses its order choice.

Everything runs the REAL snapshot builder, contract builder, chat gate, montage planner,
guided + phone compilers, plan receipts and phone verifier. No model call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.reply_language import reply_language_for
from app.pipeline import unified_montage
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creator_render_contract import (
    REQUIREMENT_FIELD_PATHS,
    CreatorRenderContractError,
    build_render_contract,
    unresolved_field_path,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding
from app.tasks.generative_build import unified_visual
from app.tasks.kria_runtime import _unresolved_choice_plan

# (file name, capture time UTC), in the order the creator attached them: 1043 first, 1012 last.
CLIPS = [
    ("IMG_1043.MOV", "21:47:37"),
    ("IMG_1039.MOV", "21:35:12"),
    ("IMG_1029.MOV", "20:02:41"),
    ("IMG_1037.MOV", "20:31:05"),
    ("IMG_1021.MOV", "19:30:02"),
    ("IMG_1033.MOV", "20:15:19"),
    ("IMG_1015.MOV", "18:44:10"),
    ("IMG_1024.MOV", "19:47:55"),
    ("IMG_1012.MOV", "18:21:28"),
]
LINEUP = uuid.UUID("00000000-0000-4000-8000-000000001038")
# The other Visuals of the item, in upload order: selfie, VIP-box view, night photo.
OTHER_PHOTOS = [
    (uuid.UUID("00000000-0000-4000-8000-000000000001"), "IMG_0995.JPG", "18:55:00"),
    (uuid.UUID("00000000-0000-4000-8000-000000001019"), "IMG_1019.HEIC", "19:20:00"),
    (uuid.UUID("00000000-0000-4000-8000-000000000002"), "FullSizeRender.jpg", "21:10:00"),
]
NO_TIME = object()


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _stamp(clock: str) -> str:
    return f"2026-10-28T{clock}Z"


def _clip_id(name: str) -> str:
    return f"m-{name.split('.')[0]}"


def _asset(row_id: uuid.UUID) -> str:
    return f"asset-{row_id}"


def _photo(row_id: uuid.UUID, name: str, clock: object) -> SimpleNamespace:
    """A ready ``PlanItemAsset`` row, as the worker and the turn snapshot read it."""
    return SimpleNamespace(
        id=row_id,
        kind="image",
        gcs_path=f"users/u/plan/i/pool/{row_id}.jpg",
        gcs_generation="9",
        duration_s=None,
        analysis={},
        aspect=4 / 3,
        source_filename=name,
        user_context="",
        content_hash=None,
        capture=None if clock is NO_TIME else {"capture_time": _stamp(str(clock))},
    )


def _lineup(clock: object = "20:33:20") -> SimpleNamespace:
    return _photo(LINEUP, "IMG_1038.HEIC", clock)


def _item(*, undated_clip: str | None = None) -> SimpleNamespace:
    rows = []
    for name, clock in CLIPS:
        row = {
            "media_id": _clip_id(name),
            "kind": "video",
            "duration_s": 8.0,
            "gcs_path": f"users/u/clips/{name}",
            "source_filename": name,
        }
        if name != undated_clip:
            row["capture"] = {"capture_time": _stamp(clock)}
        rows.append(row)
    return SimpleNamespace(clip_gcs_paths=[row["gcs_path"] for row in rows], clip_assignments=rows)


def _brief(*, open_on_lineup: bool = False) -> CreativeBrief:
    facts: dict = {"key": "capture_time"}
    if open_on_lineup:
        facts["first_clip"] = "the lineup sheet"
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r2",
                kind="order",
                scope="global",
                description="Chronological order from arrival. Open on the lineup sheet.",
                facts=facts,
            )
        ],
    )


def _strategy(*, selected: list[str] | None = None, open_on_lineup: bool = False, **extra) -> dict:
    strategy: dict = {
        "edit_format": "montage",
        "media_scope": "selected" if selected else "all",
        "selected_media_ids": list(selected or []),
        "target_duration_s": 45,
        **extra,
    }
    if open_on_lineup:
        strategy["resolved_clip_intents"] = [
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "order_first_lineup",
                "attribute": "the lineup sheet",
                "position": "first",
                "assignments": [{"media_id": _asset(LINEUP), "confidence": 0.9}],
            }
        ]
    return strategy


def _selected_with_lineup() -> list[str]:
    return [*(_clip_id(name) for name, _clock in CLIPS), _asset(LINEUP)]


def _world(strategy: dict, brief: CreativeBrief, item: SimpleNamespace, visuals: list | None):
    """The turn's snapshot as ``plan_live_turn`` writes it, pinned through the approval
    binding exactly as dispatch reads it, and the contract built from that binding."""
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-1069"),
        brief,
        latest_message="Keep it in chronological order. Open on the lineup sheet.",
        media_snapshot=snapshot_media(item, visuals=visuals),
    )
    contract = build_render_contract(
        strategy,
        generation_id="gen",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
    )
    return binding, contract


def _planner_clips(item: SimpleNamespace) -> list[UnifiedClip]:
    clips = []
    for row in item.clip_assignments:
        capture = row.get("capture")
        clips.append(
            UnifiedClip(
                media_id=row["media_id"],
                proxy_path=f"users/u/analysis-proxy-{row['media_id']}.mp4",
                generation="1",
                duration_s=row["duration_s"],
                width=1080,
                height=1920,
                capture_time=(
                    datetime.fromisoformat(capture["capture_time"].replace("Z", "+00:00"))
                    if capture
                    else None
                ),
                source_filename=row["source_filename"],
            )
        )
    return clips


def _plan(strategy: dict, brief: CreativeBrief, item: SimpleNamespace, photos: list):
    return plan_unified_montage(
        _planner_clips(item),
        brief_view(brief),
        strategy=strategy,
        clip_intents_enabled=True,
        visuals=[unified_visual(row) for row in photos],
    )


def _shown_order(plan) -> tuple[str, ...]:  # noqa: ANN001
    """The plan's picture order under manifest ids (a photo is ``asset-<row id>``)."""
    refs = {ref.media_id: ref for ref in plan.snapshot.media}
    order: list[str] = []
    for cut in plan.snapshot.fast_cuts:
        media_id = (
            _asset(uuid.UUID(cut.media_id)) if refs[cut.media_id].lane == "asset" else cut.media_id
        )
        if not order or order[-1] != media_id:
            order.append(media_id)
    return tuple(order)


def _recipe(plan, item: SimpleNamespace, photos: list):  # noqa: ANN001
    """The REAL guided compiler + REAL phone compiler, with the photos bound as Visuals."""
    bindings = tuple(
        PhoneSourceBinding(
            media_id=clip.media_id,
            proxy_path=clip.proxy_path,
            generation=clip.generation,
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=clip.duration_s,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for clip in _planner_clips(item)
    )
    visuals = tuple(
        PhoneVisualBinding(
            media_id=str(row.id),
            gcs_path=row.gcs_path,
            generation=row.gcs_generation,
            sha256="c" * 64,
            byte_count=2048,
        )
        for row in photos
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    return compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(execution), bindings, visuals
    )


def _source_audio(item: SimpleNamespace) -> dict[str, bool]:
    return {row["media_id"]: True for row in item.clip_assignments}


def _filmed(*extra: tuple[str, str]) -> list[str]:
    """Every clip id plus ``extra`` (id, clock) pairs, sorted by capture time."""
    timed = [(clock, _clip_id(name)) for name, clock in CLIPS] + [(c, i) for i, c in extra]
    return [media_id for _clock, media_id in sorted(timed)]


# -- the incident --------------------------------------------------------------


def test_incident_a_selected_dated_photo_is_dated_not_missing() -> None:
    item, brief = _item(), _brief()
    strategy = _strategy(selected=_selected_with_lineup())
    binding, contract = _world(strategy, brief, item, [_lineup()])

    assert contract.unresolved == ()  # was: ("I need capture times for every selected clip...",)
    assert contract.order_basis == "capture_time"
    assert list(contract.order_ids) == _filmed((_asset(LINEUP), "20:33:20"))
    # The chat gate (the turn-3 reply) lets the draft through.
    assert (
        _unresolved_choice_plan(strategy, brief, binding.media_snapshot, contract_brief=brief)
        is None
    )


def test_the_planner_seats_the_photo_by_its_time_and_the_phone_verifies_it() -> None:
    item, brief, photos = _item(), _brief(), [_lineup()]
    strategy = _strategy(selected=_selected_with_lineup())
    _binding, contract = _world(strategy, brief, item, photos)
    plan = _plan(strategy, brief, item, photos)

    assert plan.ordering_basis == "capture_time"
    assert plan.ordering_fallback_clip_ids == []
    assert _shown_order(plan) == contract.order_ids
    receipts = verify_phone_recipe(
        contract, _recipe(plan, item, photos), source_audio=_source_audio(item)
    )
    assert receipts


def test_open_on_the_lineup_sheet_leads_then_filming_order() -> None:
    item, brief, photos = _item(), _brief(open_on_lineup=True), [_lineup()]
    strategy = _strategy(selected=_selected_with_lineup(), open_on_lineup=True)
    _binding, contract = _world(strategy, brief, item, photos)
    plan = _plan(strategy, brief, item, photos)

    assert contract.unresolved == ()
    assert list(contract.order_ids) == [_asset(LINEUP), *_filmed()]
    assert _shown_order(plan) == contract.order_ids
    order = next(
        r
        for r in build_receipts(
            brief.live(), plan_facts_from_unified_montage(plan.record()), include_unchecked=True
        )
        if r.requirement_id == "r2"
    )
    assert (order.status, order.verification) == ("met", "checked")
    assert verify_phone_recipe(
        contract, _recipe(plan, item, photos), source_audio=_source_audio(item)
    )


def test_the_phone_refuses_a_photo_out_of_its_pinned_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verifier really checks the photo: the planner's pre-fix order (the photo spread
    between the clips, the pin not applied to it) is refused, not waved through."""
    item, brief, photos = _item(), _brief(open_on_lineup=True), [_lineup()]
    strategy = _strategy(selected=_selected_with_lineup(), open_on_lineup=True)
    _binding, contract = _world(strategy, brief, item, photos)
    monkeypatch.setattr(unified_montage, "_apply_sequence", lambda ordered, rows: list(ordered))
    plan = _plan(strategy, brief, item, photos)

    assert _shown_order(plan)[0] != _asset(LINEUP)
    with pytest.raises(CreatorRenderContractError, match="confirmed clip order"):
        verify_phone_recipe(contract, _recipe(plan, item, photos), source_audio=_source_audio(item))


def test_with_no_selection_every_dated_visual_is_in_the_chronology() -> None:
    item, brief = _item(), _brief()
    photos = [
        _photo(*OTHER_PHOTOS[0]),
        _photo(*OTHER_PHOTOS[1]),
        _lineup(),
        _photo(*OTHER_PHOTOS[2]),
    ]
    strategy = _strategy()
    _binding, contract = _world(strategy, brief, item, photos)
    plan = _plan(strategy, brief, item, photos)

    assert contract.unresolved == ()
    assert list(contract.order_ids) == _filmed(
        *((_asset(row_id), clock) for row_id, _name, clock in OTHER_PHOTOS),
        (_asset(LINEUP), "20:33:20"),
    )
    assert _shown_order(plan) == contract.order_ids
    assert verify_phone_recipe(
        contract, _recipe(plan, item, photos), source_audio=_source_audio(item)
    )


# -- an item that really has no time is named ------------------------------------


def test_an_undated_photo_is_named() -> None:
    item, brief = _item(), _brief()
    strategy = _strategy(selected=_selected_with_lineup())
    binding, contract = _world(strategy, brief, item, [_lineup(NO_TIME)])

    line = '"IMG_1038" has no filming time, so I can\'t put it in the order you filmed.'
    assert contract.unresolved == (line,)
    assert unresolved_field_path(line) == REQUIREMENT_FIELD_PATHS["order_required"]
    blocked = _unresolved_choice_plan(strategy, brief, binding.media_snapshot, contract_brief=brief)
    assert blocked is not None
    assert line in blocked[0].response


def test_an_undated_clip_is_named_by_its_file_name() -> None:
    item, brief = _item(undated_clip="IMG_1029.MOV"), _brief()
    _binding, contract = _world(
        _strategy(selected=_selected_with_lineup()), brief, item, [_lineup()]
    )

    assert contract.unresolved == (
        '"IMG_1029" has no filming time, so I can\'t put it in the order you filmed.',
    )


def test_a_turkish_chat_gets_the_named_line_in_turkish() -> None:
    item, brief = _item(), _brief()
    with reply_language_for("tr"):
        _binding, contract = _world(
            _strategy(selected=_selected_with_lineup()), brief, item, [_lineup(NO_TIME)]
        )
    line = '"IMG_1038" için çekim saati yok, o yüzden onu çektiğin sıraya koyamıyorum.'
    assert contract.unresolved == (line,)
    assert unresolved_field_path(line) == REQUIREMENT_FIELD_PATHS["order_required"]


@pytest.mark.parametrize(
    ("language", "line"),
    [
        ("en", "Your clips and photos have no filming time, so I can't put them in the order"),
        ("tr", "Kliplerinin ve fotoğraflarının hiçbiri için çekim saati yok, o yüzden onları"),
    ],
)
def test_when_nothing_is_dated_the_line_says_so_once(language: str, line: str) -> None:
    item = SimpleNamespace(
        clip_gcs_paths=_item().clip_gcs_paths,
        clip_assignments=[
            {k: v for k, v in row.items() if k != "capture"} for row in _item().clip_assignments
        ],
    )
    with reply_language_for(language):
        _binding, contract = _world(
            _strategy(selected=_selected_with_lineup()), _brief(), item, [_lineup(NO_TIME)]
        )
    (unresolved,) = contract.unresolved
    assert unresolved.startswith(line)
    assert unresolved_field_path(unresolved) == REQUIREMENT_FIELD_PATHS["order_required"]


# -- never a false "capture times" line -------------------------------------------


@pytest.mark.parametrize(
    ("visuals", "extra"),
    [
        (None, {}),  # a snapshot written before KRI-1069 lists no Visuals
        ([_lineup()], {"audio_strategy": "voiceover"}),  # a narrated montage orders none by time
    ],
    ids=["older-snapshot", "narrated-format"],
)
def test_a_photo_the_order_cannot_place_is_not_called_undated(visuals, extra) -> None:  # noqa: ANN001
    item, brief = _item(), _brief()
    _binding, contract = _world(
        _strategy(selected=_selected_with_lineup(), **extra), brief, item, visuals
    )
    assert contract.unresolved == (
        "I need to know which clips are in the edit to verify their order.",
    )


def test_clip_only_chronology_is_unchanged() -> None:
    """No Visuals listed, every clip dated: the pure filming order, exactly as before."""
    item, brief = _item(), _brief()
    strategy = _strategy(selected=[_clip_id(name) for name, _clock in CLIPS])
    _binding, with_key = _world(strategy, brief, item, [])
    _binding, without_key = _world(strategy, brief, item, None)

    assert with_key.unresolved == without_key.unresolved == ()
    assert list(with_key.order_ids) == list(without_key.order_ids) == _filmed()
    assert with_key.order_basis == without_key.order_basis == "capture_time"


def test_the_turn_snapshot_keeps_each_photo_capture() -> None:
    snapshot = snapshot_media(_item(), visuals=[_lineup()])
    assert snapshot["visuals"] == [
        {
            "media_id": _asset(LINEUP),
            "kind": "image",
            "capture": {"capture_time": "2026-10-28T20:33:20Z"},
            "source_filename": "IMG_1038.HEIC",
        }
    ]
    assert "visuals" not in snapshot_media(_item())
    assert unified_visual(_lineup()).capture_time == datetime(2026, 10, 28, 20, 33, 20, tzinfo=UTC)
