"""KRI-1102: a creator label on a Visuals photo passes the phone check where it is drawn.

Prod job 3d28fa04 (Leverkusen M2 run 3, 2026-10-31, phone Montage): two goal clips plus two
Visuals photos, a selfie and the 0:0 scoreboard. The creator asked to "Label the selfie
'Taha 🤝 me'" and to play selfie → scoreboard → the TOR clip → the other goal clip. The plan
put the label on the selfie, the selfie was the first shot, and the compiled phone recipe drew
"Taha 🤝 me" over exactly that shot. The render still failed: ``TextPlacementError`` on
``shot_labels[]`` ("This edit put confirmed text in the wrong place."). The phone verifier
looked for the label's shot among camera-roll clips only, and a Visuals photo is a
``VisualRenderAsset``, so every label on a photo was refused whatever the plan did.

Ways this can fail, written before the fix (each is pinned below):

1. A label drawn on the photo's shot is refused (the incident).
2. The fix waves through a label drawn on the wrong shot: the other photo, or a clip.
3. The photo is named by the wrong id: the contract says ``asset-<uuid>``, the manifest
   says the bare ``visual_id``; matching the bare id, or any id, would pass a label on any
   photo or refuse every one.
4. Text the creator shares between a photo and the clip after it (one caption, two shots)
   is refused because the photo's shot is missing from the spans, or passes while it only
   covers one of them.
5. A label on a camera-roll clip, the photo order (KRI-1069) or anything else the
   verifier already checked changes behavior.

Everything runs the REAL contract builder, montage planner, guided + phone compilers and
phone verifier. No model call. ``tests/fixtures/kri1102_m2_selfie_label.json`` is the prod
record: the pinned contract, the plan's label and what the job's own plan compiled to.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creator_render_contract import (
    CreatorRenderContractError,
    TextPlacementError,
    build_render_contract,
    read_render_contract,
    verify_phone_recipe,
)
from app.services.length_policy_state import read_length_policy
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding
from app.tasks.generative_build import unified_visual

PROD = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri1102_m2_selfie_label.json").read_text()
)
LABEL = "Taha 🤝 me"
# The prod media ids. (media id, file name, seconds, capture time UTC) per goal clip.
TOR_CLIP = (
    "analysis-proxy-ios-DABF1B6F-03AC-4937-A1D3-C556EBCC21CD.mp4",
    "IMG_1039.MOV",
    18.3,
    "2026-10-28T21:35:40Z",
)
GOAL_CLIP = (
    "analysis-proxy-ios-49F3233F-92C5-4EC1-A239-AA4CC837F688.mp4",
    "IMG_1037.MOV",
    9.84,
    "2026-10-28T20:25:46Z",
)
SELFIE = uuid.UUID("71843110-fb7d-4124-90a2-99861d248a2a")
SCOREBOARD = uuid.UUID("223f68d8-edf8-48c4-a92a-80913b1d0409")


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _asset(row_id: uuid.UUID) -> str:
    return f"asset-{row_id}"


def _photo(row_id: uuid.UUID, name: str, description: str) -> SimpleNamespace:
    """A ready ``PlanItemAsset`` photo, as the worker and the turn snapshot read it."""
    return SimpleNamespace(
        id=row_id,
        kind="image",
        gcs_path=f"users/u/plan/i/pool/{row_id}.jpg",
        gcs_generation="9",
        duration_s=None,
        analysis={"description": description},
        aspect=0.563,
        source_filename=name,
        user_context="",
        content_hash=None,
        capture={"capture_time": "2026-10-30T12:29:12Z"},
    )


PHOTOS = [
    _photo(SELFIE, "M2_03_visual.jpg", "A close-up selfie of three men"),
    _photo(SCOREBOARD, "M2_04_visual.jpg", "A stadium scoreboard showing 0:0"),
]


def _item() -> SimpleNamespace:
    rows = [
        {
            "media_id": media_id,
            "kind": "video",
            "duration_s": seconds,
            "gcs_path": f"users/u/clips/{name}",
            "source_filename": name,
            "capture": {"capture_time": captured},
        }
        for media_id, name, seconds, captured in (GOAL_CLIP, TOR_CLIP)
    ]
    return SimpleNamespace(clip_gcs_paths=[row["gcs_path"] for row in rows], clip_assignments=rows)


def _order_intent(index: int, attribute: str, *media_ids: str) -> dict:
    return {
        "op": "order",
        "status": "resolved",
        "intent_id": f"order_sequence_{index}",
        "attribute": attribute,
        "position": None,
        "assignments": [{"media_id": media_id, "confidence": 1.0} for media_id in media_ids],
    }


def _strategy(*, captions: list[dict] | None = None) -> dict:
    """The approved strategy of prod job 3d28fa04: the creator's sequence as order pins."""
    return {
        "edit_format": "montage",
        "media_scope": "all",
        "selected_media_ids": [],
        "target_duration_s": 12,
        "resolved_clip_intents": [
            _order_intent(1, "our selfie", _asset(SELFIE)),
            _order_intent(2, "the 0:0 scoreboard", _asset(SCOREBOARD)),
            _order_intent(3, 'the "TOR" on the big screen', TOR_CLIP[0]),
            _order_intent(4, "Taha celebrating", GOAL_CLIP[0]),
            *(captions or []),
        ],
    }


def _brief(*texts: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description='Order: our selfie, the 0:0 scoreboard, the "TOR", then Taha',
                facts={"key": "capture_time"},
            ),
            *texts,
        ],
    )


def _label_on(media_id: str) -> BriefRequirement:
    return BriefRequirement(
        id="r2", kind="text", scope=f"clip:{media_id}", literal=LABEL, description="Label it"
    )


def _world(strategy: dict, brief: CreativeBrief):
    """The contract dispatch pins (through the approval binding) and the REAL phone recipe."""
    item = _item()
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-1102"),
        brief,
        latest_message=f"Label the selfie '{LABEL}'",
        media_snapshot=snapshot_media(item, visuals=PHOTOS),
    )
    contract = build_render_contract(
        strategy,
        generation_id="gen",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
    )
    clips = [
        UnifiedClip(
            media_id=row["media_id"],
            proxy_path=f"users/u/threads/t/{row['media_id']}",
            generation="1",
            duration_s=row["duration_s"],
            width=1080,
            height=1920,
            capture_time=datetime.fromisoformat(
                row["capture"]["capture_time"].replace("Z", "+00:00")
            ),
            source_filename=row["source_filename"],
        )
        for row in item.clip_assignments
    ]
    plan = plan_unified_montage(
        clips,
        brief_view(brief),
        strategy=strategy,
        clip_intents_enabled=True,
        visuals=[unified_visual(row) for row in PHOTOS],
    )
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
        for clip in clips
    )
    visuals = tuple(
        PhoneVisualBinding(
            media_id=str(row.id),
            gcs_path=row.gcs_path,
            generation=row.gcs_generation,
            sha256="c" * 64,
            byte_count=2048,
        )
        for row in PHOTOS
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(execution), bindings, visuals
    )
    return contract, recipe


def _shots(recipe) -> list[tuple[str, float, float]]:  # noqa: ANN001
    """Each picture shot as (contract id, start, end), read from the manifest by kind,
    independently of the verifier: a photo is ``asset-<visual_id>``."""
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    rows = []
    for track in recipe.tracks:
        if track.kind != "video":
            continue
        for clip in track.clips:
            asset = manifest[clip.source_asset_id]
            name = asset.media_id if asset.kind == "original" else f"asset-{asset.visual_id}"
            end = clip.timeline_start + clip.source_duration / clip.rate + (clip.hold_duration or 0)
            rows.append((name, clip.timeline_start, end))
    return rows


def _labels(recipe) -> list:  # noqa: ANN001
    return [
        layer for layer in recipe.text_layers if "".join(run.text for run in layer.runs) == LABEL
    ]


def _moved(recipe, media_id: str):  # noqa: ANN001
    """The same recipe with the label (same words, same length) moved onto ``media_id``."""
    (start, end) = next((s, e) for name, s, e in _shots(recipe) if name == media_id)
    (layer,) = _labels(recipe)
    length = min(layer.end - layer.start, end - start)
    moved = layer.model_copy(update={"start": start, "end": start + length})
    return recipe.model_copy(
        update={"text_layers": [moved if row is layer else row for row in recipe.text_layers]}
    )


def _verify(contract, recipe, *, length_policy=None):  # noqa: ANN001
    sources = {TOR_CLIP[0]: True, GOAL_CLIP[0]: True}
    return verify_phone_recipe(contract, recipe, source_audio=sources, length_policy=length_policy)


# -- the incident --------------------------------------------------------------


def test_incident_the_selfie_label_drawn_on_the_selfie_passes() -> None:
    pinned = read_render_contract(PROD)  # the prod contract, digest checked
    assert pinned is not None
    contract, recipe = _world(_strategy(), _brief(_label_on(_asset(SELFIE))))

    # This world pins what prod pinned: the label on the selfie, the creator's order.
    assert contract.exact_texts == pinned.exact_texts
    assert contract.order_ids == pinned.order_ids
    shots = _shots(recipe)
    assert [name for name, _s, _e in shots] == [
        row["contract_id"] for row in PROD["replayed_phone_recipe"]["picture"]
    ]
    # Independent of the verifier: the words sit inside the selfie's shot, as in prod.
    (layer,) = _labels(recipe)
    (_name, start, end) = shots[0]
    assert start <= layer.start and layer.end <= end + 1 / recipe.frame_rate
    # As the pin does it (`device_render._verify_contract_pin`): the job's own length policy.
    policy = read_length_policy(PROD, pinned)
    assert _verify(pinned, recipe, length_policy=policy)  # was: TextPlacementError
    assert _verify(contract, recipe)


# -- the wrong shot still fails -------------------------------------------------


@pytest.mark.parametrize(
    "elsewhere", [_asset(SCOREBOARD), TOR_CLIP[0]], ids=["other-photo", "a-clip"]
)
def test_the_selfie_label_on_another_shot_is_refused(elsewhere: str) -> None:
    contract, recipe = _world(_strategy(), _brief(_label_on(_asset(SELFIE))))

    with pytest.raises(TextPlacementError, match="wrong place") as refused:
        _verify(contract, _moved(recipe, elsewhere))
    assert refused.value.field_path == "shot_labels[]"


def test_a_label_on_the_scoreboard_is_not_met_by_the_selfie() -> None:
    """Each photo is its own shot: the scoreboard's label is checked on the scoreboard."""
    contract, recipe = _world(_strategy(), _brief(_label_on(_asset(SCOREBOARD))))

    (layer,) = _labels(recipe)
    (_name, start, end) = _shots(recipe)[1]
    assert start <= layer.start and layer.end <= end + 1 / recipe.frame_rate
    assert _verify(contract, recipe)
    with pytest.raises(TextPlacementError, match="wrong place"):
        _verify(contract, _moved(recipe, _asset(SELFIE)))


# -- one caption shared by a photo and the clip after it --------------------------


def _shared_caption_world():
    caption = {
        "op": "caption",
        "status": "resolved",
        "intent_id": "caption_shared",
        "attribute": "the scoreboard and the TOR",
        "assignments": [
            {"media_id": _asset(SCOREBOARD), "confidence": 1.0},
            {"media_id": TOR_CLIP[0], "confidence": 1.0},
        ],
        "caption_text": LABEL,
        "creator_text": LABEL,
        "caption_grounding": "creator_text",
    }
    shared = BriefRequirement(
        id="r2",
        kind="text",
        scope="per_clip",
        literal=LABEL,
        description="the scoreboard and the TOR",
    )
    return _world(_strategy(captions=[caption]), _brief(shared))


def test_a_caption_shared_by_a_photo_and_a_clip_passes() -> None:
    contract, recipe = _shared_caption_world()

    assert {t.media_id for t in contract.exact_texts} == {_asset(SCOREBOARD), TOR_CLIP[0]}
    assert _labels(recipe)
    assert _verify(contract, recipe)


def test_a_shared_caption_that_leaves_the_photo_is_refused() -> None:
    contract, recipe = _shared_caption_world()
    (_tor, start, end) = next(row for row in _shots(recipe) if row[0] == TOR_CLIP[0])
    clipped = recipe.model_copy(
        update={
            "text_layers": [
                layer.model_copy(update={"start": start, "end": end})
                if layer in _labels(recipe)
                else layer
                for layer in recipe.text_layers
            ]
        }
    )

    with pytest.raises(CreatorRenderContractError, match="wrong place"):
        _verify(contract, clipped)


# -- neighbours that must not change ------------------------------------------------


def test_a_label_on_a_camera_roll_clip_is_unchanged() -> None:
    contract, recipe = _world(_strategy(), _brief(_label_on(TOR_CLIP[0])))

    assert _verify(contract, recipe)
    with pytest.raises(TextPlacementError, match="wrong place"):
        _verify(contract, _moved(recipe, _asset(SELFIE)))


def test_the_photo_order_is_still_checked() -> None:
    contract, recipe = _world(_strategy(), _brief(_label_on(_asset(SELFIE))))
    track = next(track for track in recipe.tracks if track.kind == "video")
    first, second = track.clips[0], track.clips[1]
    swapped = track.model_copy(
        update={
            "clips": [
                second.model_copy(update={"timeline_start": first.timeline_start}),
                first.model_copy(update={"timeline_start": second.timeline_start}),
                *track.clips[2:],
            ]
        }
    )
    reordered = recipe.model_copy(
        update={"tracks": [swapped if row is track else row for row in recipe.tracks]}
    )

    with pytest.raises(CreatorRenderContractError):
        _verify(contract, reordered)
