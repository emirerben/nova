"""Qendresa Q1 run 3 (prod 2026-11-14, thread 05aa3a87, job 3ef3cbe5): the whole incident replayed.

One request, one talking take, 10 Visuals (5 screenshots, 5 screen recordings), server 3feec88a9.
What the creator got:

- the first draft HELD: "None of the 11 clips got its own text in this draft." (r5, her numbered
  labels) and "A Talking video can't do this, so it stays as filmed" (r7, "Only my voice.");
- after "Make it without these" (brief v2): captions at y 0.86 instead of the middle, the "big
  title" at 0.72 of its size, the ending as one lime tag with no arrow, no labels, and her
  screen recordings dropped as "overlap".

The fixes replayed together: KRI-1471 (labels), KRI-1472 (only my voice), KRI-1475 (ending with
a face ask), KRI-1476/1477 (captions and title), KRI-1478 (strip cards, off by default).

Real: the brief rows exactly as the brief model filed them (the job's binding),
`assess_readiness`, the phone Talking worker (`_run_generative_job` ->
`_run_phone_subtitled_job`: title, caption, label, ending and card placement, the compiler and
recipe validation) and `build_receipts` on the variant it persisted. Fixture: transport only.
Her words are run 3's `overlay_transcript` (text and timings, no audio), her face is the union
of the six boxes the job's title placement recorded, and the Visuals are synthetic 1920x1080
rows with a measurable text column. No model call (no Gemini key: the heuristic card matcher
runs where prod ran the placement agent) and no speech cleanup (it needs her audio, so r1 is
not judged here); no private media.

- Case A: every fix on and the strip on (protocol 51).
- Case B, the wrong-outcome control: every new switch off. The draft is held with prod's two
  reasons, and the simpler-version brief renders prod's captions, title and cards. KRI-1475 has
  no switch, so its control is a plan that names an object to keep text off (today's tag).
- Case C: what ships by default (the strip off).

What only the combined output can show (the reason for this file): a label on a card, on a
caption line or on her face; a card under the title or the ending; captions kept on her chin;
two fixes disagreeing about what her face ask protects.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field

import pytest

import app.pipeline.phone_protected_text as protected_mod
import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as rgm
import app.services.phone_visuals as phone_visuals_mod
import app.services.pipeline_trace as pipeline_trace
from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness, readiness_reply
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    _TALKING_LIST_LABEL_PLAN,
    build_receipts,
    plan_facts_from_phone_variant,
)
from app.kria.contracts import RequirementReceipt
from app.pipeline.object_track import ObjectTrack, TrackAnchor
from app.pipeline.phone_captions import caption_look_from_variant
from app.pipeline.phone_spoken_text import caption_boxes
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
from app.pipeline.phone_title_look import stamp_title_look
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.transcribe import Word
from app.services import creator_capabilities as capabilities
from app.services import phone_text_styling as styling
from app.services.background_text_focus import TextColumn
from app.services.client_protocol import clear_request_context
from app.services.device_render import device_status
from app.services.phone_card_layout import WATERMARK_BOX
from app.services.phone_sources import PhoneVisualBinding
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile
from tests.pipeline.test_phone_list_labels import Q1_RUN3_LABELS, Q1_RUN3_WORDS
from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

TAKE = "c0"  # the worker harness's speaker clip
# The draft turn names a phone take by its proxy file (the job's is ...-3D5CE019-....mp4).
PROXY_TAKE = "analysis-proxy-ios-3D5CE019.mp4"
TAKE_S = 48.8  # the job's `silence_cut.original_duration_s`
PROTOCOL = 51  # protocol-literal-ok: `all_candidates.creator_client_protocol` of run 3
CANVAS_H = 1920.0
CANVAS_RATIO = 1080.0 / CANVAS_H
OVERLAY_TRACK = "subtitled-overlays"
# The union of the six boxes in the job's `opening_title_placement.face_boxes`.
FACE = NormalizedBox(0.2067, 0.2113, 0.8452, 0.6175)
# The product's own bar for "on a face" (the title, ending and label tests use it).
MAX_FACE_COVERAGE = 0.05
SAID_AT = [8.48, 15.32, 22.82, 31.06, 39.32]  # where each list item is first said
TITLE = "5 repos / I’d build with / this weekend!!"
ENDING = "comment REPO"
LIME = "#C5F82A"

REQUEST = (
    "Cut out all my pauses and mistakes so it’s tight. Captions word by word in the middle of "
    "the screen, white, two words at a time. Start with a big title “5 repos / I’d build with / "
    "this weekend!!” for the first 3 seconds. When I talk about something, show the matching "
    "screenshot or screen recording in the top or bottom part of the screen, not over my face. "
    "When I list the five repos, show each one as big text, like “1. Spec-kit”. End with "
    "“comment REPO” and an arrow for the last 3 seconds. Only my voice, no music."
)


def _r(rid, kind, description, *, scope="global", literal=None, facts=None):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope=scope,
        description=description,
        literal=literal,
        facts=facts or {},
        source_turn_id="c982f2b6-a9ca-483e-b940-72d89ac78124",
    )


# Brief v1 as the job's binding holds it (r5 and r7 back to "open", as turn 1 read them).
R1 = _r(
    "r1",
    "style",
    "Cut out all my pauses and mistakes so it’s tight.",
    facts={"pauses": "tight", "cut_restarts": True, "speech_cleanup": True},
)
R2 = _r(
    "r2",
    "style",
    "Captions word by word in the middle of the screen, white, two words at a time.",
    facts={
        "captions": "word_by_word",
        "caption_color": "#FFFFFF",
        "caption_words": 2,
        "caption_position": "middle",
    },
)
R3 = _r(
    "r3", "text", "Start with a big title for the first 3 seconds.", scope="title", literal=TITLE
)
R4 = _r(
    "r4",
    "style",
    "When I talk about something, show the matching screenshot or screen recording in the top or "
    "bottom part of the screen, not over my face.",
    facts={
        "keep_text_off": [{"kind": "face"}],
        "visual_position": "top_or_bottom",
        "keep_visuals_off_face": True,
        "match_visuals_to_speech": True,
    },
)
R5_FACTS = {"numbered": True, "text_size": "large", "show_when_mentioned": True}
R5 = _r(
    "r5",
    "text",
    "When I list the five repos, show each one as big text, like “1. Spec-kit”.",
    scope="per_clip",
    facts=R5_FACTS,
)
R6 = _r(
    "r6",
    "text",
    "End with “comment REPO” and an arrow for the last 3 seconds.",
    literal=ENDING,
    facts={"arrow": True, "closing": ENDING},
)
R7 = _r("r7", "audio", "Only my voice.", facts={"voice_only": True})
R8 = _r("r8", "audio", "No music.", facts={"audio_intent": "no_music"})


def _superseded(req: BriefRequirement) -> BriefRequirement:
    facts = {**req.facts, "superseded_by": "server"}
    return req.model_copy(update={"status": "superseded", "facts": facts})


BRIEF_V1 = CreativeBrief(version=1, requirements=[R1, R2, R3, R4, R5, R6, R7, R8])
# "Make it without these": the server superseded the two held asks (the job's brief v2).
BRIEF_V2 = CreativeBrief(
    version=2, requirements=[R1, R2, R3, R4, _superseded(R5), R6, _superseded(R7), R8]
)
HOLD_REASONS = {
    "r5": "None of the 11 clips got its own text in this draft.",
    "r7": "A Talking video can't do this, so it stays as filmed",
}

# Her Visuals: one screenshot and one screen recording per repo, all 1920x1080. Synthetic rows:
# her file names without the device prefix, and what the upload analysis reads on each page.
_NS = uuid.UUID("00000000-0000-4000-8000-0000000003ef")
REPOS = [
    # (key, the trigger the planner named, the page as the analysis describes it)
    ("spec-kit", "Spec Kit", "spec kit: write the spec first and the agent builds it"),
    ("impeccable", "Impeccable", "impeccable gives a coding agent real design taste"),
    ("tradingagents", "Trading Agents", "trading agents: a whole trading firm of AI agents"),
    ("agents", "The Agents Marketplace", "the agents marketplace of ready-made sub-agents"),
    ("gamestudios", "Cloud Code Game Studios", "cloud code game studios directors and leads"),
]
ASPECT = 16 / 9
RECORDING_S = 9.0


def _visual_id(kind: str, key: str) -> str:
    return str(uuid.uuid5(_NS, f"{kind}-{key}"))


SHOTS = [_visual_id("image", key) for key, _t, _d in REPOS]
RECORDINGS = [_visual_id("video", key) for key, _t, _d in REPOS]
VISUAL_KIND = {**dict.fromkeys(SHOTS, "image"), **dict.fromkeys(RECORDINGS, "video")}

# The strategy the planner approved (the job's `creator_strategy`), with our Visual ids.
STRATEGY = {
    "pacing": "fast",
    "direction": "native",
    "rationale": "Around 38 seconds; title, speech-triggered screenshots, two-word captions.",
    "edit_format": "subtitled",
    "caption_style": "karaoke",
    "closing_title": ENDING,
    "opening_title": TITLE,
    "audio_strategy": "original_audio",
    "reaction_beats": [
        {
            "beat_id": key,
            "trigger": trigger,
            "visual_id": f"asset-{shot}",
            "occurrence": "first",
            "visual_role": "photo",
        }
        for (key, trigger, _d), shot in zip(REPOS, SHOTS, strict=True)
    ],
    "render_program": "native",
    "story_structure": [],
    "target_duration_s": 38,
    "selected_media_ids": [TAKE],
    "video_reuse_policy": "once",
    "optional_treatments": ["overlays"],
    "opening_title_duration_s": 3.0,
}

# Every switch this PR adds that defaults on; Case B turns them all off.
NEW_SWITCHES = (
    "list_label_example_ask_enabled",  # KRI-1471
    "voice_only_ask_enabled",  # KRI-1472
    "keep_text_off_caption_spot_enabled",  # KRI-1476 / KRI-1477
    # Q1 run 4: a shrunk title retries the band above her face; a faint line gets an outline.
    "title_legibility_enabled",
)
STRIP_SWITCH = "talking_card_strip_enabled"  # KRI-1478, off by default
# The switches production has on for this path (all default off in code).
PROD_ON = (
    "tight_pause_cut_enabled",
    "title_look_enabled",
    "phone_caption_shape_enabled",
    "spoken_list_labels_enabled",
    "talking_card_layout_enabled",
    "creator_cta_ending_enabled",
    "freehand_drawing_enabled",
    "phone_text_styling_enabled",
    "phone_subtitled_title_enabled",
    "phone_subtitled_closing_title_enabled",
    "phone_subtitled_spoken_text_enabled",
    "phone_subtitled_video_overlays_enabled",
    "speech_cleanup_retakes_enabled",
)
# Production's verified list (tests/_prod_profile, 2026-10-02) plus the tokens verified since
# for these features (not re-read from prod here); `sourceCrop` is the strip's prerequisite.
VERIFIED = (*PROD_VERIFIED_FEATURES, "captionShape", "cardLayout", "freehandDrawing", "sourceCrop")


def _switches(patch, *, fixes: bool, strip: bool) -> None:
    for name in PROD_ON:
        patch.setattr(settings, name, True)
    for name in NEW_SWITCHES:
        patch.setattr(settings, name, fixes)
    patch.setattr(settings, STRIP_SWITCH, strip)


# ------------------------------------------------------------------ readiness (the draft turn)


def _manifest():
    media = [{"media_id": PROXY_TAKE, "kind": "video", "duration_s": TAKE_S}]
    media += [{"media_id": f"asset-{v}", "kind": "image", "label": "shot.png"} for v in SHOTS]
    media += [
        {"media_id": f"asset-{v}", "kind": "video", "duration_s": RECORDING_S} for v in RECORDINGS
    ]
    return capabilities.resolve_creator_manifest(
        item_id="item-q1-run3",
        edit_format="subtitled",
        media=media,
        phone_source_media_ids=[PROXY_TAKE],
        phone_rendering_allowed=True,
    )


def _readiness(monkeypatch, *, fixes: bool) -> dict:
    apply_prod_profile(monkeypatch)
    _switches(monkeypatch, fixes=fixes, strip=False)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(VERIFIED))
    return assess_readiness(
        BRIEF_V1,
        {**STRATEGY, "selected_media_ids": [PROXY_TAKE]},
        media={"clip_paths": [PROXY_TAKE]},
        manifest=_manifest(),
        creator_id=uuid.UUID("00000000-0000-4000-8000-0000000011a1"),
        protected_regions=[],
        protected_marks=True,
    )


def _held(report: dict) -> dict[str, str]:
    return {
        row["requirement_id"]: row["reason"]
        for row in report["requirements"]
        if row["disposition"] not in APPROVABLE
    }


# ------------------------------------------------------------------ the worker (after approval)


def _pool_rows() -> list[dict]:
    rows = []
    for number, (key, _trigger, page) in enumerate(REPOS, start=1):
        for kind, media_id in (("image", SHOTS[number - 1]), ("video", RECORDINGS[number - 1])):
            ext, name = ("png", "Screenshot") if kind == "image" else ("mp4", "Screen Recording")
            rows.append(
                {
                    "id": media_id,
                    "gcs_path": f"users/u1/plan/item1/pool/{media_id}.{ext}",
                    "gcs_generation": "7",
                    "kind": kind,
                    "source_filename": f"{name} {number} {key}.{ext}",
                    "duration_s": RECORDING_S if kind == "video" else None,
                    "aspect": ASPECT,
                    "user_context": "",
                    "analysis": {"subject": page},
                    "capture": None,
                }
            )
    return rows


def _visual(media_id: str, *, measured: bool) -> PhoneVisualBinding:
    kind = VISUAL_KIND[media_id]
    video = {"duration_s": RECORDING_S, "width": 1920, "height": 1080, "orientation_degrees": 0}
    # A page of text on the left 42 % of the picture: what a strip cuts the card to.
    column = TextColumn(
        left=0.2, right=0.62, line_pitch=0.034, source_width=1920, source_height=1080
    )
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=f"users/u1/plan/item1/pool/{media_id}.{'mp4' if kind == 'video' else 'png'}",
        generation="7",
        sha256="b" * 64,
        byte_count=100,
        kind=kind,
        **(video if kind == "video" else {}),
    ).model_copy(update={"text_column": column if measured else None})


def _bind(_open, *, job_id, pins, measure_text=False):  # noqa: ARG001
    return tuple(_visual(media_id, measured=measure_text) for media_id in pins)


def _faces(_path, anchors, **_kwargs):
    regions = [ProtectedRegion(max(0.0, at - 0.5), at + 0.5, FACE, "face") for at in anchors]
    return regions, {"attempted": len(anchors), "decoded": len(anchors), "detected": len(regions)}


# The laptop she marks in the control for KRI-1475 (an object to keep text off).
LAPTOP = NormalizedBox(0.30, 0.66, 0.70, 0.86)
_LAPTOP_MARK = {
    "label": "laptop",
    "media_id": TAKE,
    "source_t_s": 0.5,
    "box": [LAPTOP.left, LAPTOP.top, LAPTOP.right, LAPTOP.bottom],
    "generation": "1",
}
_OFF_FACE_AND_LAPTOP = [{"kind": "face"}, {"kind": "object", "label": "laptop"}]
_OBJECT_R4 = R4.model_copy(update={"facts": {**R4.facts, "keep_text_off": _OFF_FACE_AND_LAPTOP}})


def _laptop_track(_clip_path, **kw):
    count = int(round((kw["end_s"] - kw["start_s"]) / 0.1)) + 1
    return ObjectTrack(
        label=kw["label"],
        media_id=kw["media_id"],
        generation=kw["generation"],
        seed_t_s=kw["seed_t_s"],
        seed_box=kw["seed_box"],
        anchors=tuple(
            TrackAnchor(round(kw["start_s"] + i * 0.1, 3), LAPTOP, "tracked") for i in range(count)
        ),
        status="tracked",
        receipt={"anchors": count, "tracked": count, "lost": 0},
    )


def _job(patch, brief: CreativeBrief, *, fixes: bool, strip: bool, marks=None):
    clear_request_context()
    words = [Word(confidence=1.0, **word) for word in Q1_RUN3_WORDS]
    job, snapshot, _session, _binding = _setup_subtitled(
        patch, transcript_words=words, duration_s=TAKE_S
    )
    _enable_beats(patch)
    _switches(patch, fixes=fixes, strip=strip)
    patch.setattr(settings, "phone_render_verified_features", list(VERIFIED))
    # Transport: no Gemini key (the heuristic matcher), no trace DB, no proxy file on disk.
    patch.setattr(ogm.settings, "gemini_api_key", "", raising=False)
    patch.setattr(pipeline_trace, "record_pipeline_event", lambda *a, **k: None)
    patch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    patch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    patch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    patch.setattr(ogm, "sample_face_regions", _faces)
    patch.setattr(title_mod, "sample_face_regions", _faces)
    patch.setattr(protected_mod, "track_object", _laptop_track)

    def environment(canvas):  # noqa: ARG001 - nothing to sample contrast frames from
        styling._POLICY.get()["_face_boxes"] = []

    patch.setattr(styling, "_ensure_generation_environment", environment)
    job.all_candidates.update(
        {
            "creator_request": REQUEST,
            "creator_strategy": copy.deepcopy(STRATEGY),
            "creator_client_protocol": PROTOCOL,
            "voiceover_caption_style": "word",  # caption_style karaoke, as approved
            "landscape_fit": "fit",
        }
    )
    binding = BriefBinding.create(uuid.uuid4(), brief, protected_regions=marks).model_dump(
        mode="json"
    )
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)
    stamp_phone_text_style(job)
    job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
    stamp_title_look(job)
    return job


@dataclass
class Replay:
    """What one worker run drew, measured while its switches were still set."""

    variant: dict
    cards: list  # the recipe's card clips, in window order
    caption_centres: list[float]  # each caption layer's y, as a fraction of the height
    caption_lines: list[tuple[float, float, NormalizedBox]]  # (start_s, end_s, ink)
    ink: dict[str, NormalizedBox]  # text row id -> where its ink sits
    receipts: dict[str, RequirementReceipt]
    music_in_render: bool
    music_asset_id: str | None
    drawing_layers: list[str] = field(default_factory=list)

    def rows(self, source: str) -> list[dict]:
        return [
            row
            for row in self.variant.get("text_elements") or []
            if (row.get("source_params") or {}).get("source") == source and not row.get("removed")
        ]

    def labels(self) -> list[dict]:
        rows = self.variant.get("text_elements") or []
        return sorted(
            (r for r in rows if r["id"].startswith("list-label-")), key=lambda r: r["start_s"]
        )

    def captions_during(self, start_s: float, end_s: float) -> list[NormalizedBox]:
        return [box for a, b, box in self.caption_lines if a < end_s and start_s < b]


CASES: dict[str, dict] = {
    "A": {"brief": BRIEF_V1, "fixes": True, "strip": True},
    "C": {"brief": BRIEF_V1, "fixes": True, "strip": False},
    "B": {"brief": BRIEF_V2, "fixes": False, "strip": False},
    "B-object": {
        "brief": CreativeBrief(
            version=2,
            requirements=[R1, R2, R3, _OBJECT_R4, _superseded(R5), R6, _superseded(R7), R8],
        ),
        "fixes": False,
        "strip": False,
        "marks": [_LAPTOP_MARK],
    },
}
_RUNS: dict[str, Replay] = {}


def _replay(case: str) -> Replay:
    """The case's worker run, once per process (every measurement is taken inside the patch)."""
    if case in _RUNS:
        return _RUNS[case]
    spec = dict(CASES[case])
    brief = spec.pop("brief")
    with pytest.MonkeyPatch.context() as patch:
        job = _job(patch, brief, **spec)
        gb._run_generative_job(str(job.id))
        assert job.status == "awaiting_device", (job.status, job.error_detail)
        variant = job.assembly_plan["variants"][0]
        recipe = device_status(job, "subtitled").request.recipe
        look = caption_look_from_variant(variant)
        style = variant.get("voiceover_caption_style") or "word"
        lines = [
            (float(cue["start_s"]), float(cue["end_s"]), box)
            for cue in variant["caption_cues"]
            for box in caption_boxes(
                [cue],
                window=(float(cue["start_s"]), float(cue["end_s"])),
                caption_style=style,
                canvas=_STORY_CANVAS,
                look=look,
            )
        ]
        track = next((t for t in recipe.tracks if t.id == OVERLAY_TRACK), None)
        facts = plan_facts_from_phone_variant(variant)
        _RUNS[case] = Replay(
            variant=variant,
            cards=sorted(
                track.clips if track is not None else [],
                key=lambda clip: clip.visual_placement.window_start,
            ),
            caption_centres=[
                layer.anchor_y / CANVAS_H
                for layer in recipe.text_layers
                if layer.id.startswith("caption-")
            ],
            caption_lines=lines,
            ink={
                row["id"]: title_mod._measure_title_box(row, canvas=_STORY_CANVAS)
                for row in variant.get("text_elements") or []
                if not row.get("removed")
            },
            receipts={
                r.requirement_id: r
                for r in build_receipts(brief.live(), facts, include_unchecked=True)
            },
            music_in_render=facts.music_in_render,
            music_asset_id=recipe.audio.music_asset_id,
            drawing_layers=[layer.id for layer in recipe.drawing_layers],
        )
    return _RUNS[case]


def _hits(a: NormalizedBox, b: NormalizedBox) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def _card_box(clip) -> NormalizedBox:
    place, crop = clip.visual_placement, clip.source_crop
    shown = ASPECT if crop is None else ASPECT * crop.width / crop.height
    height = place.width_fraction * CANVAS_RATIO / shown
    return NormalizedBox(
        place.x_fraction - place.width_fraction / 2,
        place.y_fraction - height / 2,
        place.x_fraction + place.width_fraction / 2,
        place.y_fraction + height / 2,
    )


def _window(clip) -> tuple[float, float]:
    return clip.visual_placement.window_start, clip.visual_placement.window_end


def _kind(clip) -> str:
    return VISUAL_KIND[clip.source_asset_id.removeprefix("visual-")]


def _arrow_box(drawing: dict) -> NormalizedBox:
    points = [point for stroke in drawing["strokes"] for point in stroke["points"]]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return NormalizedBox(min(xs), min(ys), max(xs), max(ys))


FIXED = pytest.mark.parametrize("case", ["A", "C"])

# ============================== Cases A and C: the fixes on ====================================


def test_fixed_draft_is_ready_with_nothing_held_or_left_out(monkeypatch):
    """(1) The draft turn. The strip is a worker switch: readiness is the same for A and C."""
    report = _readiness(monkeypatch, fixes=True)

    assert report["ready"] is True
    assert _held(report) == {}
    rows = {row["requirement_id"]: row for row in report["requirements"]}
    # Nothing is "unsupported", so the simpler-version card has nothing to leave out.
    assert {row["disposition"] for row in rows.values()} <= {"executable", "unverified"}
    assert (rows["r5"]["disposition"], rows["r5"]["reason"]) == (
        "unverified",
        _TALKING_LIST_LABEL_PLAN,
    )
    assert (rows["r7"]["disposition"], rows["r8"]["disposition"]) == ("executable", "executable")


@FIXED
def test_captions_are_two_white_words_in_the_middle_row(case):
    run = _replay(case)
    look = run.variant["caption_look_receipt"]

    assert (look["words"], look["color"], look["position"]) == (2, "#FFFFFF", "middle")
    assert 0.62 <= look["position_y_frac"] <= 0.75
    assert run.caption_centres and all(0.62 <= y <= 0.75 for y in run.caption_centres)


@FIXED
def test_captions_are_off_her_face(case):
    run = _replay(case)

    # The product's bar for "on a face", as for the title, the ending and the labels. One
    # wrapped two-row caption ("ready-made sub-agents") reaches 0.0026 of the frame (5 px)
    # into the detector's box, about 4 % of that block; every other line is fully clear.
    on_face = [
        (cue["text"], round(box.top, 4), round(box.coverage_by(FACE), 3))
        for cue in run.variant["caption_cues"]
        for start, _end, box in run.caption_lines
        if start == cue["start_s"] and box.coverage_by(FACE) > MAX_FACE_COVERAGE
    ]
    assert not on_face, f"(caption, ink top, share on her face box): {on_face}"


@FIXED
def test_the_title_is_big_on_three_lines_clear_of_the_captions(case):
    run = _replay(case)
    placement = run.variant["opening_title_placement"]
    rows = run.rows("opening_title")

    # Never prod's 0.72: at least the first shrink step, whatever the captions leave room for.
    assert placement["scale"] >= 0.85 and placement["lines"] == 3
    assert [row["text"] for row in rows] == ["5 repos", "I’d build with", "this weekend!!"]
    assert {(row["start_s"], row["end_s"]) for row in rows} == {(0.0, 3.0)}
    for row in rows:
        box = run.ink[row["id"]]
        assert box.coverage_by(FACE) <= MAX_FACE_COVERAGE, (row["text"], box)
        for caption in run.captions_during(0.0, 3.0):
            assert not _hits(box, caption), (row["text"], box, caption)


@FIXED
def test_the_ending_is_her_comment_repo_with_one_arrow(case):
    run = _replay(case)
    lead, keyword = run.rows("closing_title")

    assert run.variant["closing_title_placement"]["status"] == "cta_ending"
    assert (lead["text"], keyword["text"]) == ("comment", "REPO")
    assert keyword["size_px"] >= 1.5 * lead["size_px"]
    assert {(row["start_s"], row["end_s"]) for row in (lead, keyword)} == {(45.8, 48.8)}
    [arrow] = run.variant["drawings"]
    assert arrow["id"] == "closing-arrow" and run.drawing_layers == ["closing-arrow"]
    for box in (run.ink[lead["id"]], run.ink[keyword["id"]], _arrow_box(arrow)):
        assert box.coverage_by(FACE) <= MAX_FACE_COVERAGE, box
        assert not any(_hits(box, _card_box(c)) for c in run.cards if _window(c)[1] > 45.8)


@FIXED
def test_five_labels_in_her_words_each_when_she_says_its_item(case):
    run = _replay(case)
    labels = run.labels()

    assert [" ".join(row["text"].split()) for row in labels] == Q1_RUN3_LABELS
    for row, said in zip(labels, SAID_AT, strict=True):
        assert abs(row["start_s"] - said) <= 1.5, (row["text"], row["start_s"], said)
    for row, nxt in zip(labels, labels[1:], strict=False):
        assert row["end_s"] <= nxt["start_s"] + 1e-6, "two labels at once"
    placed = run.variant["list_label_placement"]["rows"]
    assert placed[0]["list_found"] == 5 and {r["status"] for r in placed} == {"placed"}


@FIXED
def test_labels_are_clear_of_the_title_her_face_and_her_captions(case):
    run = _replay(case)

    for row in run.labels():
        box = run.ink[row["id"]]
        assert row["start_s"] >= 3.0, "inside the title's 3 seconds"
        assert box.coverage_by(FACE) <= MAX_FACE_COVERAGE, (row["text"], box)
        for caption in run.captions_during(row["start_s"], row["end_s"]):
            assert not _hits(box, caption), (row["text"], box, caption)


@FIXED
def test_no_label_is_drawn_over_a_card(case):
    run = _replay(case)

    covered = [
        (row["text"], _kind(card), round(run.ink[row["id"]].coverage_by(_card_box(card)), 2))
        for row in run.labels()
        for card in run.cards
        if _window(card)[0] < row["end_s"]
        and row["start_s"] < _window(card)[1]
        and _hits(run.ink[row["id"]], _card_box(card))
    ]
    assert not covered, f"(label, card kind, share of the label on the card): {covered}"


@FIXED
def test_cards_stay_off_her_face_her_captions_the_title_and_each_other(case):
    run = _replay(case)

    assert len(run.cards) >= 5
    for card in run.cards:
        box, (start, end) = _card_box(card), _window(card)
        assert 0.0 <= box.left and box.right <= 1.0 and 0.0 <= box.top and box.bottom <= 1.0
        assert not _hits(box, FACE), (card.id, box)
        assert start >= 3.0, "under the title"
        for caption in run.captions_during(start, end):
            assert not _hits(box, caption), (card.id, box, caption)
    for i, a in enumerate(run.cards):
        for b in run.cards[i + 1 :]:
            together = _window(a)[0] < _window(b)[1] and _window(b)[0] < _window(a)[1]
            assert not together, (a.id, _window(a), b.id, _window(b))


def test_with_the_strip_every_card_is_cut_to_its_text_with_no_fade():
    run = _replay("A")

    assert run.cards
    for card in run.cards:
        place = card.visual_placement
        assert card.source_crop is not None and card.source_crop.width < 0.5, card.id
        assert (place.fade_in, place.fade_out) == (False, False), card.id


def test_with_the_strip_her_recordings_share_the_mention_with_the_screenshot():
    run = _replay("A")
    shots = [c for c in run.cards if _kind(c) == "image"]
    recordings = [c for c in run.cards if _kind(c) == "video"]

    assert len(shots) == len(recordings) == 5
    for shot, recording, said in zip(shots, recordings, SAID_AT, strict=True):
        assert _window(shot)[0] == pytest.approx(said, abs=0.11)
        assert _window(recording)[0] == pytest.approx(_window(shot)[1])  # one after the other
    assert run.variant["phone_overlay_receipt"]["unplaced"] == []


def test_with_the_strip_every_card_is_as_wide_as_its_band_allows():
    """Full width in a free band; in the bottom band it is clear of the Kria wordmark (since
    Q1 run 4 below it, centred; beside it only when the band under the mark is too short)."""
    run = _replay("A")

    assert run.cards
    for card in run.cards:
        box, width = _card_box(card), card.visual_placement.width_fraction
        beside_the_mark = box.top < WATERMARK_BOX.bottom and WATERMARK_BOX.top < box.bottom
        assert width >= (0.75 if beside_the_mark else 0.9), (card.id, width, box)
        assert not _hits(box, WATERMARK_BOX), (card.id, box)


def test_without_the_strip_the_cards_are_todays_whole_pictures():
    """Case C: the same screenshots at the same words, drawn whole with today's fades; her
    recordings are dropped as "overlap", as in prod."""
    run, strip = _replay("C"), _replay("A")
    shots = [c for c in strip.cards if _kind(c) == "image"]

    assert [_kind(c) for c in run.cards] == ["image"] * 5
    for card, shot in zip(run.cards, shots, strict=True):
        place = card.visual_placement
        assert card.source_crop is None
        assert (place.fade_in, place.fade_out) == (True, True)
        assert _window(card)[0] == _window(shot)[0]
        whole = (place.x_fraction, place.y_fraction, place.width_fraction)
        strip_place = shot.visual_placement
        cut = (strip_place.x_fraction, strip_place.y_fraction, strip_place.width_fraction)
        if cut == whole:
            continue  # the strip keeps the box of the card it replaces
        # Q1 run 4: the whole card sat off-centre beside the Kria wordmark (prod: x 0.21-0.97);
        # its strip is the one box that may differ: centred, full width, below the wordmark.
        beside = _card_box(card)
        assert place.x_fraction != 0.5, card.id
        assert beside.top < WATERMARK_BOX.bottom and WATERMARK_BOX.top < beside.bottom, card.id
        box = _card_box(shot)
        assert (strip_place.x_fraction, strip_place.width_fraction) == (0.5, 0.94), shot.id
        assert not _hits(box, WATERMARK_BOX), (shot.id, box)
        assert box.top >= WATERMARK_BOX.bottom and box.bottom <= 0.98 + 1e-6, (shot.id, box)
    unplaced = run.variant["phone_overlay_receipt"]["unplaced"]
    assert [(u["kind"], u["reason"]) for u in unplaced] == [("video", "overlap")] * 5


@FIXED
def test_only_her_voice_no_music(case):
    run = _replay(case)

    assert run.music_in_render is False and run.music_asset_id is None
    assert not [key for key in run.variant if "music" in key or "song" in key]


@FIXED
def test_the_finished_video_says_done_only_for_what_it_drew(case):
    run = _replay(case)

    statuses = {rid: (r.status, r.verification) for rid, r in run.receipts.items()}
    done = {rid for rid, status in statuses.items() if status == ("met", "checked")}
    # Q1 run 4: without the strip her five recordings are dropped as "overlap" (case C), and
    # the done-list says so instead of "Done" (prod said Done with two recordings unshown).
    unshown = run.variant["phone_overlay_receipt"]["unplaced"]
    assert bool(unshown) == (case == "C")
    assert done == {"r2", "r3", "r5", "r6", "r7", "r8"} | ({"r4"} if not unshown else set()), (
        statuses
    )
    if unshown:
        assert statuses["r4"] == ("partial", "checked")
        assert run.receipts["r4"].reason.startswith("Not shown: ")
        assert "another card was already on screen then" in run.receipts["r4"].reason
    # r1 (her pauses) is the one ask this replay cannot judge: cleanup needs her audio.
    assert statuses["r1"] == ("partial", "checked")
    # Each "Done" has its thing in the output.
    assert max(len(cue["words"]) for cue in run.variant["caption_cues"]) <= 2  # r2
    assert len(run.rows("opening_title")) == 3  # r3
    beats = run.variant["phone_beat_receipt"]
    assert len(beats["placed"]) == 5 and beats["unplaced"] == []  # r4
    assert len(run.labels()) == 5  # r5
    assert [d["id"] for d in run.variant["drawings"]] == ["closing-arrow"]  # r6
    assert run.receipts["r7"].reason == run.receipts["r8"].reason == "No music, as asked."


# ============================== Case B: every new switch off ===================================


def test_control_the_draft_is_held_with_prods_two_reasons(monkeypatch):
    report = _readiness(monkeypatch, fixes=False)

    assert report["ready"] is False
    assert _held(report) == HOLD_REASONS
    reply = readiness_reply(BRIEF_V1, report)
    for rid, reason in HOLD_REASONS.items():
        req = next(r for r in BRIEF_V1.requirements if r.id == rid)
        # KRI-1465: an ask is listed without its closing full stop (prod read ".:").
        assert f"- {req.listed()}: {reason}" in reply


def test_control_the_simpler_version_renders_as_prod_did():
    """Brief v2 through the worker with the switches off: the job's own numbers."""
    run = _replay("B")
    variant = run.variant

    # Captions thrown to the lower third (the job: 0.86, margin 269, ink 0.843-0.88).
    assert variant["caption_look_receipt"]["position_y_frac"] == 0.86
    assert variant["caption_margin_v"] == 269
    assert all(y > 0.80 for y in run.caption_centres)
    # The "big title" squeezed between her chin and them (the job: 0.72, 0.6275-0.81865).
    title = variant["opening_title_placement"]
    assert (title["scale"], title["status"], title["lines"]) == (0.72, "shrunk", 3)
    assert (title["block"]["top"], title["block"]["bottom"]) == (0.6275, 0.81865)
    # No labels (r5 was left out), her recordings dropped as "overlap", small cards up top.
    assert "list_label_placement" not in variant and run.labels() == []
    unplaced = variant["phone_overlay_receipt"]["unplaced"]
    assert [(u["kind"], u["reason"]) for u in unplaced] == [("video", "overlap")] * 5
    assert [_kind(c) for c in run.cards] == ["image"] * 5
    assert all(c.source_crop is None and _card_box(c).bottom < FACE.top for c in run.cards)
    # The done-list of that video: the captions are "Partly".
    assert run.receipts["r2"].status == "partial"
    assert "lower than you asked" in run.receipts["r2"].reason
    assert set(run.receipts) == {"r1", "r2", "r3", "r4", "r6", "r8"}


def test_control_the_ending_fix_has_no_switch():
    """KRI-1475 is not behind a switch, so even the control draws her ending for a face ask."""
    run = _replay("B")

    assert run.variant["closing_title_placement"]["status"] == "cta_ending"
    assert [d["id"] for d in run.variant["drawings"]] == ["closing-arrow"]


def test_control_an_object_to_keep_text_off_keeps_todays_plain_tag():
    """What prod drew for run 3 is still what a plan naming an OBJECT gets: one lime tag, no
    arrow (the ending is placed around faces and captions only)."""
    run = _replay("B-object")
    [row] = run.rows("closing_title")

    assert (row["text"], row["background_color"]) == (ENDING, LIME)
    assert not run.variant.get("drawings") and run.drawing_layers == []
    placement = run.variant["closing_title_placement"]
    assert placement["status"] != "cta_ending"
    assert [o["label"] for o in placement["objects"]] == ["laptop"]


# ============================== Q1 run 4 (2026-11-17): the ending held =========================
# Same prompt, same build of this pipeline, but the Main Creator's strategy came back WITHOUT
# `closing_title` and the brief filed r6 with only `{closing}` (no `arrow` fact). Prod held the
# draft on r6 alone: "That exact text isn't in this draft." Ways the fix could go wrong, written
# first: (a) the draft turns ready but the render draws no ending (a checker that only accepts);
# (b) the ending is drawn twice; (c) switching the fix off does not bring prod's hold back, so
# this test would pass on anything. The single-function cases (picture endings, pins, two
# endings, superseded rows, routes without closing text, TR/DE) are in
# tests/kria/test_brief_closing_title_fill.py.

R6_RUN4 = _r(
    "r6",
    "text",
    'End with "comment REPO" and an arrow for the last 3 seconds.',
    literal=ENDING,
    facts={"closing": ENDING},
)
BRIEF_RUN4 = CreativeBrief(version=1, requirements=[R1, R2, R3, R4, R5, R6_RUN4, R7, R8])
STRATEGY_RUN4 = {key: value for key, value in STRATEGY.items() if key != "closing_title"}


def _run4_draft(monkeypatch, *, fill: bool) -> tuple[dict, dict]:
    """(the strategy the draft carries after the planner's backstop, its readiness report)."""
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.kria.planner import _with_brief_closing_title  # noqa: PLC0415

    apply_prod_profile(monkeypatch)
    _switches(monkeypatch, fixes=True, strip=False)
    monkeypatch.setattr(settings, "brief_closing_title_fill_enabled", fill)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(VERIFIED))
    planned = CreativeStrategy.model_validate({**STRATEGY_RUN4, "selected_media_ids": [PROXY_TAKE]})
    filled = _with_brief_closing_title(planned, BRIEF_RUN4, _manifest())
    strategy = (filled or planned).model_dump(mode="json", exclude_none=True)
    report = assess_readiness(
        BRIEF_RUN4,
        strategy,
        media={"clip_paths": [PROXY_TAKE]},
        manifest=_manifest(),
        creator_id=uuid.UUID("00000000-0000-4000-8000-0000000011a1"),
        protected_regions=[],
        protected_marks=True,
    )
    return strategy, report


def test_run4_an_ending_the_planner_left_out_is_drawn_from_her_brief(monkeypatch):
    strategy, report = _run4_draft(monkeypatch, fill=True)

    assert report["ready"] is True and _held(report) == {}
    # Ready because the render will draw it: the strategy the worker gets is run 3's, whose
    # worker output `test_the_ending_is_her_comment_repo_with_one_arrow` pins (her "comment" /
    # "REPO" rows once, one closing arrow).
    assert strategy["closing_title"] == ENDING == STRATEGY["closing_title"]
    assert [strategy.get(key) for key in ("opening_title", "reaction_beats")] == [
        STRATEGY["opening_title"],
        STRATEGY["reaction_beats"],
    ]
    rows = {row["requirement_id"]: row for row in report["requirements"]}
    assert rows["r6"]["disposition"] in APPROVABLE


def test_run4_control_without_the_fill_the_draft_is_held_as_prod_was(monkeypatch):
    strategy, report = _run4_draft(monkeypatch, fill=False)

    assert "closing_title" not in strategy
    assert report["ready"] is False
    assert _held(report) == {"r6": "That exact text isn't in this draft."}
