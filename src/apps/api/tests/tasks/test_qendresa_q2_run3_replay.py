"""Qendresa Q2 run 3 (prod 2026-11-15, thread F04383A7, job 77c63496): the whole incident replayed.

One request ("Run 2 prompt"), one talking take, 6 screen recordings and a GitHub logo; her
recordings full screen behind her, she cut out small in the bottom-right corner after a
full-screen opening; protocol 51 (TestFlight 1997); server 136d82c9f. What the creator got:

- hold 1: "I couldn't tell which clip "1. Spec-kit" goes on, so I can't confirm that text." on
  all 14 asks, no buttons (the labels were filed per clip);
- after she answered, hold 2: r4 "No pop-in for what's hot this week." (she never says it) and
  r14 "Only my voice: ... mute the screen recordings." ("A Talking video can't do this");
- after "Make it without these": "4. Agents" inside "trading agents", no label 5, the ending
  block over her corner face, the grey caption box on her cheek, and a done-list cut mid-line.

Fixes replayed together: KRI-1300/1480/1481 (labels), KRI-1484 (label spot and plate),
KRI-1486 (a cue she never says), KRI-1472 (only my voice restated), KRI-1482 (ending off her
corner), KRI-1483 (captions beside her corner face), KRI-1485 (whole reply lines).

Real: the brief rows as filed (v1: labels per clip; v2: labels with their own name as trigger),
`assess_readiness`, the phone Talking worker (`_run_generative_job` -> `_run_phone_subtitled_job`
with the real text-styling environment) and the receipts and done-list on what it drew.
Fixture: transport only. Her words are the job's `overlay_transcript` (no audio, so no speech
cleanup: source seconds), her face is the job's sampled face band, the Visuals are synthetic
1920x1080 rows with a measurable text column. No model call, no private media.

- Case A: every fix on, the label plate on, both filings.
- Case B: what ships by default (the plate off), both filings.
- Case C, the wrong-outcome control: every new switch off. It must reproduce prod.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field

import pytest

import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as rgm
import app.services.phone_visuals as phone_visuals_mod
import app.services.pipeline_trace as pipeline_trace
from app.config import settings
from app.kria import brief_checks as bc
from app.kria.approval_readiness import APPROVABLE, assess_readiness, readiness_reply
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.contracts import RequirementReceipt
from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
from app.pipeline.phone_title_look import stamp_title_look
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.transcribe import Word
from app.services import creator_capabilities as capabilities
from app.services.background_text_focus import TextColumn
from app.services.client_protocol import bind_sending_build, clear_request_context
from app.services.device_render import device_status
from app.services.phone_sources import PhoneVisualBinding
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile
from tests.pipeline.test_phone_list_labels import Q1_RUN3_WORDS
from tests.tasks.test_caption_corner_face import _simulate_environment
from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

TAKE = "c0"  # the worker harness's speaker clip
PROXY_TAKE = "analysis-proxy-ios-C3242EA6.mp4"  # the draft names the take by its proxy
TAKE_S = 48.8  # the job's `silence_cut.original_duration_s`
PROTOCOL = 51  # protocol-literal-ok: `all_candidates.creator_client_protocol` of run 3
CANVAS_H = 1920.0
# The job's face band on the take (`opening_title_placement.face_band`, KRI-1483's fixture).
TAKE_FACE = NormalizedBox(0.2067, 0.2113, 0.8452, 0.6175)
CORNER = "small_bottom_right"
# Where each repo is first said, and where she counts it ("Number one," ... "And number five,").
SAID_AT = [8.48, 15.32, 22.82, 31.06, 39.32]
COUNTED_AT = [7.52, 14.48, 22.08, 30.52, 38.22]
LABELS = [
    "1. Spec-kit",
    "2. Impeccable",
    "3. TradingAgents",
    "4. Agents",
    "5. Claude code game studio",
]
NAMES = ["Spec-kit", "Impeccable", "TradingAgents", "Agents", "Claude code game studio"]
TRADING_AGENTS_AT = 23.08  # "Trading Agents." -- prod's label 4
TITLE = "5 PROJECTS / the AI builder community / can’t stop talking about!!!!"
ENDING = "comment REPO"
PLATE = "#141414"
UNSAID = "You don't say “what’s hot this week” in your take, so nothing is shown for it"
WHICH_CLIP = "I couldn't tell which clip \"1. Spec-kit\" goes on, so I can't confirm that text."
TALKING_CANT = "A Talking video can't do this, so it stays as filmed"
NO_POP_IN = "No pop-in for what’s hot this week."
CAPTION_ROW = (0.65, 0.92)  # the kit's "near the bottom"

# Her words: the job's `overlay_transcript`, the same take as Q1 run 3 but for two timings.
_Q2_TIMINGS = {("and", 41.94): (41.94, 42.26), ("specialists", 42.22): (42.26, 42.54)}
WORDS = [
    {
        **word,
        **dict(
            zip(
                ("start_s", "end_s"),
                _Q2_TIMINGS.get((word["text"], word["start_s"]), (word["start_s"], word["end_s"])),
                strict=True,
            )
        ),
    }
    for word in Q1_RUN3_WORDS
]
TRANSCRIPT = " ".join(word["text"] for word in WORDS)

T1 = "9bb944c3-4564-4a89-b5b2-a62ebdcf3cc7"  # her first message
T2 = "089730e8-90b5-46ee-8fba-1825c77202cf"  # "The five labels are not for a clip."


def _r(rid, kind, description, *, scope="global", literal=None, facts=None, turn=T1):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope=scope,
        description=description,
        literal=literal,
        facts=facts or {},
        source_turn_id=turn,
    )


def _brief(filing: str) -> CreativeBrief:
    """v1: hold 1's rows (labels filed per clip, as their receipts in event 6 prove; their facts
    are not recorded, so none). v2: the rows after her answer (job 77c63496's binding), with
    r4 and r14 still live, as hold 2 read them."""
    if filing == "v1":
        labels = [
            _r(
                f"r{5 + i}",
                "text",
                f"Add this label when the {name} section starts, replacing "
                + ("it when the next project starts." if i == 0 else "the previous project label."),
                scope="per_clip",
                literal=label,
            )
            for i, (label, name) in enumerate(zip(LABELS, NAMES, strict=True))
        ]
    else:
        labels = [
            _r(
                f"r{5 + i}",
                "text",
                "Show this label as text on screen when I start talking about that repo, not as a "
                "clip label; replace "
                + ("it when the next repo starts." if i == 0 else "the previous repo label."),
                literal=label,
                facts={"triggers": [name]},
                turn=T2,
            )
            for i, (label, name) in enumerate(zip(LABELS, NAMES, strict=True))
        ]
    return CreativeBrief(
        version=1 if filing == "v1" else 2,
        requirements=[
            _r(
                "r1",
                "text",
                "Opening title in bold caps, then serif, then handwritten.",
                scope="title",
                literal=TITLE,
            ),
            _r(
                "r2",
                "style",
                "Start with me full screen with the GitHub logo above the opening title.",
                facts={"speaker_full_opening": True},
            ),
            _r(
                "r3",
                "style",
                "After the opening, put my screen recordings full screen and me cut out with no "
                "background, small in the bottom right. Zoom into the recordings so the text is "
                "readable and show the matching recording when I discuss each repo.",
                facts={
                    "speaker_small": True,
                    "background_zoom": True,
                    "background_changes": True,
                    "speaker_background": True,
                },
            ),
            _r(
                "r4",
                "style",
                "Show the trending page when I talk about what’s hot this week.",
                facts={"triggers": ["what’s hot this week"]},
            ),
            *labels,
            _r(
                "r10",
                "style",
                "Captions word by word in a light grey box near the bottom.",
                facts={
                    "captions": "word_by_word",
                    "caption_box": "#E5E5E5",
                    "caption_position": "bottom",
                },
            ),
            _r(
                "r11",
                "style",
                "Cut all my pauses.",
                facts={"pauses": "tight", "speech_cleanup": True},
            ),
            _r(
                "r12",
                "text",
                "End with “comment REPO” and an arrow.",
                literal=ENDING,
                facts={"closing": ENDING},
            ),
            _r("r13", "audio", "Only my voice: no music.", facts={"audio_intent": "no_music"}),
            _r(
                "r14",
                "audio",
                "Only my voice: keep the speaker audible and mute the screen recordings.",
            ),
        ],
    )


def _without_the_held(brief: CreativeBrief) -> CreativeBrief:
    """ "Make it without these": r4 and r14 superseded (brief v3, the job's binding)."""
    return brief.model_copy(
        update={
            "version": 3,
            "requirements": [
                req.model_copy(update={"status": "superseded"}) if req.id in ("r4", "r14") else req
                for req in brief.requirements
            ],
        }
    )


# Her Visuals: Screen Recordings 1-6 (1920x1080, 18 s) and the GitHub logo. Synthetic rows.
_NS = uuid.UUID("00000000-0000-4000-8000-00000000077c")
RECORDINGS = [
    ("spec-kit", "Spec Kit", "spec kit: write the spec first, the agent builds it"),
    ("impeccable", "Impeccable", "impeccable gives a coding agent design taste"),
    ("tradingagents", "Trading Agents", "trading agents: a trading firm of AI agents"),
    ("agents", "The Agents Marketplace", "the agents marketplace of ready-made sub-agents"),
    ("game-studios", "Cloud Code Game Studios", "cloud code game studios directors and leads"),
    ("trending", None, "the github trending page this week"),
]
REC_IDS = [str(uuid.uuid5(_NS, key)) for key, _p, _s in RECORDINGS]
LOGO_ID = str(uuid.uuid5(_NS, "github-logo"))
RECORDING_S = 18.0

STRATEGY = {
    "pacing": "fast",
    "direction": "native",
    "rationale": "Open on her, then each matching recording behind her bottom-right cut-out.",
    "edit_format": "subtitled",
    "caption_style": "karaoke",
    "closing_title": ENDING,
    "opening_title": TITLE,
    "audio_strategy": "original_audio",
    "reaction_beats": [
        {
            "anchor": "title",
            "beat_id": "opening-github-logo",
            "trigger": "GitHub logo",
            "visual_id": f"asset-{LOGO_ID}",
            "occurrence": "first",
            "visual_role": "sticker",
        }
    ],
    "render_program": "native",
    "story_structure": [],
    "background_media": {
        "media_id": f"asset-{REC_IDS[0]}",
        "sequence": [
            {"media_id": f"asset-{media}", "spoken_phrase": phrase}
            for media, (_k, phrase, _s) in zip(REC_IDS, RECORDINGS, strict=True)
            if phrase is not None
        ],
        "speaker_frame": CORNER,
        "full_screen_opening": True,
    },
    "target_duration_s": 42,
    "selected_media_ids": [TAKE],
    "video_reuse_policy": "once",
    "optional_treatments": ["overlays"],
}

# The switches this branch (and #2360 under it) added or relies on, and the plate.
NEW_SWITCHES = (
    "list_label_example_ask_enabled",  # KRI-1471 / 1480 / 1481 / label spot
    "voice_only_ask_enabled",  # KRI-1472
    "keep_text_off_caption_spot_enabled",  # KRI-1476 / 1477
    "unsaid_cue_report_enabled",  # KRI-1486
    "speaker_frame_caption_faces_enabled",  # KRI-1482 / 1483 (an existing switch, reused)
    "kria_reply_line_fit_enabled",  # KRI-1485
)
PLATE_SWITCH = "list_label_plate_enabled"  # KRI-1484, off by default
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
    "speaker_background_enabled",
    "background_follows_talk_enabled",
    "speaker_full_opening_enabled",
    "background_focus_enabled",
    "kria_honest_reply_lines_enabled",
    "beat_phrase_timing_enabled",
    "phone_subtitled_rendering_enabled",
)
# Prod's verified list (tests/_prod_profile) plus the tokens this layout needs, `authoredText`
# (the plate) among them (not re-read from prod here).
VERIFIED = (
    *PROD_VERIFIED_FEATURES,
    "authoredText",
    "captionShape",
    "cardLayout",
    "freehandDrawing",
    "sourceCrop",
    "speakerBackground",
    "speakerBackgroundWindows",
    "speakerFrameWindows",
    "backgroundFocus",
)


def _switches(patch, *, fixes: bool, plate: bool) -> None:
    fields = type(settings).model_fields
    for name in PROD_ON:
        if name in fields:
            patch.setattr(settings, name, True)
    for name in NEW_SWITCHES:
        patch.setattr(settings, name, fixes)
    patch.setattr(settings, PLATE_SWITCH, plate)
    patch.setattr(settings, "phone_render_user_ids", [])
    patch.setattr(settings, "phone_render_verified_features", list(VERIFIED))


# ------------------------------------------------------------------ readiness (the draft turn)


def _readiness(monkeypatch, filing: str, *, fixes: bool) -> tuple[CreativeBrief, dict]:
    apply_prod_profile(monkeypatch)
    _switches(monkeypatch, fixes=fixes, plate=False)
    bind_sending_build(PROTOCOL)  # the app build that sent her message
    try:
        media = [{"media_id": PROXY_TAKE, "kind": "video", "duration_s": TAKE_S}]
        media += [
            {"media_id": f"asset-{m}", "kind": "video", "duration_s": RECORDING_S} for m in REC_IDS
        ]
        media += [{"media_id": f"asset-{LOGO_ID}", "kind": "image", "label": "GitHub logo.png"}]
        manifest = capabilities.resolve_creator_manifest(
            item_id="item-q2-run3",
            edit_format="subtitled",
            media=media,
            phone_source_media_ids=[PROXY_TAKE],
            phone_rendering_allowed=True,
        )
        brief = _brief(filing)
        report = assess_readiness(
            brief,
            {**STRATEGY, "selected_media_ids": [PROXY_TAKE]},
            # The draft's media snapshot carries the take's transcript (the clip record's).
            media={
                "clip_paths": [PROXY_TAKE],
                "clip_assignments": [
                    {
                        "kind": "video",
                        "media_id": PROXY_TAKE,
                        "analysis": {"transcript": TRANSCRIPT},
                    }
                ],
            },
            manifest=manifest,
            creator_id=uuid.UUID("00000000-0000-4000-8000-0000000011a1"),
            protected_regions=[],
            protected_marks=True,
        )
    finally:
        clear_request_context()
    return brief, report


def _held(report: dict) -> dict[str, str]:
    return {
        row["requirement_id"]: row["reason"]
        for row in report["requirements"]
        if row["disposition"] not in APPROVABLE
    }


# ------------------------------------------------------------------ the worker (after approval)


def _pool_rows() -> list[dict]:
    rows = [
        {
            "id": media,
            "gcs_path": f"users/u1/plan/item1/pool/{media}.mp4",
            "gcs_generation": "7",
            "kind": "video",
            "source_filename": f"Screen Recording {n} {key}.mp4",
            "duration_s": RECORDING_S,
            "aspect": 16 / 9,
            "user_context": "",
            "analysis": {"subject": subject},
            "capture": None,
        }
        for n, (media, (key, _p, subject)) in enumerate(zip(REC_IDS, RECORDINGS, strict=True), 1)
    ]
    rows.append(
        {
            "id": LOGO_ID,
            "gcs_path": f"users/u1/plan/item1/pool/{LOGO_ID}.png",
            "gcs_generation": "7",
            "kind": "image",
            "source_filename": "GitHub logo.png",
            "duration_s": None,
            "aspect": 1.0,
            "user_context": "",
            "analysis": {"subject": "github logo", "has_alpha": True},
            "capture": None,
        }
    )
    return rows


def _bind(_open, *, job_id, pins, measure_text=False):  # noqa: ARG001
    out = []
    for media in pins:
        video = media != LOGO_ID
        column = TextColumn(
            left=0.2, right=0.62, line_pitch=0.034, source_width=1920, source_height=1080
        )
        out.append(
            PhoneVisualBinding(
                media_id=media,
                gcs_path=f"users/u1/plan/item1/pool/{media}.{'mp4' if video else 'png'}",
                generation="7",
                sha256="b" * 64,
                byte_count=100,
                kind="video" if video else "image",
                **(
                    {
                        "duration_s": RECORDING_S,
                        "width": 1920,
                        "height": 1080,
                        "orientation_degrees": 0,
                    }
                    if video
                    else {}
                ),
            ).model_copy(update={"text_column": column if measure_text and video else None})
        )
    return tuple(out)


def _faces(_path, anchors, **_kwargs):
    regions = [ProtectedRegion(max(0.0, at - 0.5), at + 0.5, TAKE_FACE, "face") for at in anchors]
    return regions, {"attempted": len(anchors), "decoded": len(anchors), "detected": len(regions)}


def _job(patch, brief: CreativeBrief, *, fixes: bool, plate: bool):
    clear_request_context()
    take = [Word(confidence=1.0, **word) for word in WORDS]
    job, snapshot, _session, _binding = _setup_subtitled(
        patch, transcript_words=take, duration_s=TAKE_S
    )
    _enable_beats(patch)
    # The REAL text-styling environment, its face sampler and proxy download faked.
    captured = _simulate_environment(
        patch, (TAKE_FACE.left, TAKE_FACE.top, TAKE_FACE.right, TAKE_FACE.bottom)
    )
    _switches(patch, fixes=fixes, plate=plate)
    patch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
    patch.setattr(ogm.settings, "gemini_api_key", "", raising=False)
    patch.setattr(pipeline_trace, "record_pipeline_event", lambda *a, **k: None)
    patch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    patch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    patch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    patch.setattr(ogm, "sample_face_regions", _faces)
    patch.setattr(title_mod, "sample_face_regions", _faces)
    job.all_candidates.update(
        {
            "creator_request": (
                "Start with me full screen and the title “5 PROJECTS / the AI builder community "
                "/ can’t stop talking about!!!!” in bold caps, then serif, then handwritten, with "
                "the GitHub logo above it. Captions word by word in a light grey box near the "
                "bottom. End with “comment REPO” and an arrow. Only my voice."
            ),
            "creator_strategy": copy.deepcopy(STRATEGY),
            "creator_client_protocol": PROTOCOL,
            "voiceover_caption_style": "word",  # caption_style karaoke, as approved
            "landscape_fit": "fit",
        }
    )
    binding = BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)
    stamp_phone_text_style(job)
    job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
    stamp_title_look(job)
    return job, captured


@dataclass
class Replay:
    """What one worker run drew, measured while its switches were still set."""

    brief: CreativeBrief
    variant: dict
    recipe: object
    caption_boxes: list[dict]  # every caption block as placed (`remember_caption_bounds`)
    ink: dict[str, NormalizedBox]
    receipts: dict[str, RequirementReceipt]
    reply: str
    music_in_render: bool
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
            (r for r in rows if r["id"].startswith(("list-label-", "spoken-text-"))),
            key=lambda r: r["start_s"],
        )

    def captions_during(self, start_s: float, end_s: float) -> list[NormalizedBox]:
        return [
            NormalizedBox(*b["box"])
            for b in self.caption_boxes
            if b.get("kind", "caption") == "caption"
            and b["start_s"] < end_s
            and start_s < b["end_s"]
        ]


CASES: dict[str, dict] = {
    "A-v1": {"filing": "v1", "fixes": True, "plate": True},
    "A-v2": {"filing": "v2", "fixes": True, "plate": True},
    "B-v1": {"filing": "v1", "fixes": True, "plate": False},
    "B-v2": {"filing": "v2", "fixes": True, "plate": False},
    # Prod rendered brief v3: the v2 filing without the two held asks.
    "C": {"filing": "v3", "fixes": False, "plate": False},
}
_RUNS: dict[str, Replay] = {}


def _replay(case: str) -> Replay:
    """The case's worker run, once per process (every measurement taken inside the patch)."""
    if case in _RUNS:
        return _RUNS[case]
    spec = dict(CASES[case])
    filing = spec.pop("filing")
    brief = _without_the_held(_brief("v2")) if filing == "v3" else _brief(filing)
    with pytest.MonkeyPatch.context() as patch:
        job, captured = _job(patch, brief, **spec)
        gb._run_generative_job(str(job.id))
        assert job.status == "awaiting_device", (job.status, job.error_detail)
        variant = job.assembly_plan["variants"][0]
        recipe = device_status(job, "subtitled").request.recipe
        facts = bc.plan_facts_from_phone_variant(variant)
        receipts = bc.build_receipts(brief.live(), facts, include_unchecked=True)
        reply, _lead = bc.reply_and_lead_from_receipts(brief, receipts)
        _RUNS[case] = Replay(
            brief=brief,
            variant=variant,
            recipe=recipe,
            caption_boxes=[dict(b) for b in captured.get("boxes") or []],
            ink={
                row["id"]: title_mod._measure_title_box(row, canvas=_STORY_CANVAS)
                for row in variant.get("text_elements") or []
                if not row.get("removed")
            },
            receipts={r.requirement_id: r for r in receipts},
            reply=reply,
            music_in_render=facts.music_in_render,
            drawing_layers=[layer.id for layer in recipe.drawing_layers],
        )
    return _RUNS[case]


def _drawn(box: NormalizedBox) -> NormalizedBox:
    """Where the phone draws a full-frame ``box`` with her in the corner frame."""
    frame = SPEAKER_FRAME_PRESETS[CORNER]
    w, cx, cy = frame["width_fraction"], frame["x_fraction"], frame["y_fraction"]
    return NormalizedBox(
        cx + (box.left - 0.5) * w,
        cy + (box.top - 0.5) * w,
        cx + (box.right - 0.5) * w,
        cy + (box.bottom - 0.5) * w,
    )


CORNER_FACE = _drawn(TAKE_FACE)


def _cutout_body() -> NormalizedBox:
    frame = SPEAKER_FRAME_PRESETS[CORNER]
    half = frame["width_fraction"] / 2
    return NormalizedBox(frame["x_fraction"] - half, CORNER_FACE.top, frame["x_fraction"] + half, 1)


def _arrow_box(drawing: dict) -> NormalizedBox:
    points = [point for stroke in drawing["strokes"] for point in stroke["points"]]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return NormalizedBox(min(xs), min(ys), max(xs), max(ys))


def _opening_end(run: Replay) -> float:
    background = run.recipe.speaker_background
    spans = [float(w.end) for w in background.full_screen_windows or []]
    return max(spans) if spans else 0.0


def _drawn_captions(run: Replay) -> list[tuple[float, float, str, NormalizedBox]]:
    """Every caption as the phone draws it: (start, end, words, its grey box) off the recipe."""
    out = []
    for layer in run.recipe.text_layers:
        if not layer.id.startswith("caption-") or layer.background is None:
            continue
        bg = layer.background
        box = NormalizedBox(
            bg.left / 1080,
            bg.top / CANVAS_H,
            (bg.left + bg.width) / 1080,
            (bg.top + bg.height) / CANVAS_H,
        )
        out.append(
            (float(layer.start), float(layer.end), " ".join(r.text for r in layer.runs), box)
        )
    return out


def _captions_during(run: Replay, start_s: float, end_s: float) -> list[NormalizedBox]:
    return [box for a, b, _w, box in _drawn_captions(run) if a < end_s and start_s < b]


def _whole_lines(text: str) -> None:
    for line in text.splitlines():
        assert not line.rstrip().endswith("…") or line.startswith("- …and "), line


FIXED = pytest.mark.parametrize("case", ["A-v1", "A-v2", "B-v1", "B-v2"])
FILINGS = pytest.mark.parametrize("filing", ["v1", "v2"])

# ============================== Cases A and B: every fix on ===================================


@FILINGS
def test_the_draft_is_ready_with_nothing_held_or_left_out(monkeypatch, filing):
    _brief_, report = _readiness(monkeypatch, filing, fixes=True)
    rows = {row["requirement_id"]: row for row in report["requirements"]}

    assert report["ready"] is True and _held(report) == {}
    assert report.get("policy_problem") is None
    # Nothing "unsupported": the simpler-version card has nothing to leave out.
    assert {row["disposition"] for row in rows.values()} <= {"executable", "unverified"}
    assert (rows["r4"]["disposition"], rows["r4"]["reason"]) == ("unverified", UNSAID)
    assert rows["r14"]["disposition"] == rows["r13"]["disposition"] == "executable"
    for rid in ("r5", "r6", "r7", "r8", "r9"):
        assert rows[rid]["reason"] == bc._TALKING_LIST_LABEL_PLAN, rid


@FIXED
def test_the_title_in_three_looks_with_the_logo_above_it(case):
    run = _replay(case)
    rows = run.rows("opening_title")
    assert [(r["text"], r["source_params"]["title_look"]["style"]) for r in rows] == [
        ("5 PROJECTS", "bold"),
        ("the AI builder community", "serif"),
        ("can’t stop talking about!!!!", "handwritten"),
    ]
    assert {(r["start_s"], r["end_s"]) for r in rows} == {(0.0, 3.0)}
    title_top = min(run.ink[r["id"]].top for r in rows)
    [logo] = [
        clip
        for track in run.recipe.tracks
        for clip in track.clips
        if clip.source_asset_id == f"visual-{LOGO_ID}"
    ]
    place = logo.visual_placement
    assert (place.window_start, place.window_end) == (0.0, 3.0)
    assert place.y_fraction + place.width_fraction * (1080 / CANVAS_H) / 2 <= title_top
    for row in rows:
        assert run.ink[row["id"]].coverage_by(TAKE_FACE) <= 0.05, row["text"]
        for caption in _captions_during(run, 0.0, 3.0):
            assert not run.ink[row["id"]].intersection_area(caption), (row["text"], caption)


@FIXED
def test_full_screen_then_the_corner_each_recording_on_its_repo(case):
    run = _replay(case)
    background = run.recipe.speaker_background
    [opening] = background.full_screen_windows
    assert opening.start == 0.0 and 3.0 < opening.end < SAID_AT[0]
    assert run.variant["phone_lane_receipt"]["background"]["speaker_frame"] == CORNER
    windows = sorted(background.windows, key=lambda w: w.start)
    assert [w.asset_id for w in windows] == [f"visual-{m}" for m in REC_IDS[:5]]
    assert windows[0].start == pytest.approx(opening.end)  # she switches on her first repo
    for window, said in zip(windows[1:], SAID_AT[1:], strict=True):
        assert window.start == pytest.approx(said, abs=0.3), (window.asset_id, said)


@FIXED
def test_five_labels_at_their_own_items_up_top_one_at_a_time(case):
    run = _replay(case)
    labels = run.labels()

    assert [r["text"] for r in labels] == LABELS
    assert all(r["id"].startswith("list-label-") for r in labels)
    for row, counted in zip(labels, COUNTED_AT, strict=True):
        assert abs(row["start_s"] - counted) <= 1.5, (row["text"], row["start_s"], counted)
    assert abs(labels[3]["start_s"] - TRADING_AGENTS_AT) > 1.5  # not "trading agents"
    for row, nxt in zip(labels, [*labels[1:], None], strict=True):
        held = row["end_s"] - row["start_s"]
        assert held == pytest.approx(3.0) or (nxt and row["end_s"] == pytest.approx(nxt["start_s"]))
        if nxt:
            assert row["end_s"] <= nxt["start_s"] + 1e-6, "two labels at once"
    for row in labels:
        ink = run.ink[row["id"]]
        assert row["y_frac"] == 0.15 and ink.bottom <= 0.25, (row["text"], ink)
        assert row.get("background_color") == (PLATE if case.startswith("A") else None)
        assert not ink.intersection_area(CORNER_FACE), (row["text"], ink)
        for caption in _captions_during(run, row["start_s"], row["end_s"]):
            assert not ink.intersection_area(caption), (row["text"], caption)
    ending_start = min(r["start_s"] for r in run.rows("closing_title"))
    assert max(r["end_s"] for r in labels) <= ending_start


@FIXED
def test_captions_in_the_asked_bottom_row_and_off_her_corner_face(case):
    run = _replay(case)
    opening_end = run.recipe.speaker_background.full_screen_windows[0].end
    drawn = _drawn_captions(run)

    assert drawn and run.variant["caption_look_receipt"]["box"] == "#E5E5E5"
    for _a, _b, words, box in drawn:
        centre = (box.top + box.bottom) / 2
        assert CAPTION_ROW[0] <= centre <= CAPTION_ROW[1], (words, centre)
    on_face = [
        (start, words, round(box.right, 4))
        for start, _end, words, box in drawn
        if start >= opening_end - 1e-6 and box.intersection_area(CORNER_FACE)
    ]
    assert not on_face, f"corner face x>={CORNER_FACE.left:.4f}: {on_face}"
    assert run.variant["caption_look_receipt"]["over_face"] is False


@FIXED
def test_the_ending_is_comment_repo_with_one_arrow_off_her_corner(case):
    run = _replay(case)
    lead, keyword = run.rows("closing_title")
    [arrow] = run.variant["drawings"]

    assert run.variant["closing_title_placement"]["status"] == "cta_ending"
    assert (lead["text"], keyword["text"]) == ("comment", "REPO")
    assert keyword["size_px"] >= 1.5 * lead["size_px"]
    assert run.drawing_layers == ["closing-arrow"] == [arrow["id"]]
    for part in (run.ink[lead["id"]], run.ink[keyword["id"]], _arrow_box(arrow)):
        assert not part.intersection_area(CORNER_FACE), part
        assert not part.intersection_area(_cutout_body()), part
        for caption in _captions_during(run, lead["start_s"], lead["end_s"]):
            assert not part.intersection_area(caption), (part, caption)


@FIXED
def test_only_her_voice_no_music(case):
    run = _replay(case)
    assert run.music_in_render is False and run.recipe.audio.music_asset_id is None


@FIXED
def test_the_done_list_says_done_only_for_what_was_drawn_and_keeps_every_other_line(case):
    run = _replay(case)
    statuses = {rid: r.status for rid, r in run.receipts.items()}

    done = {rid for rid, status in statuses.items() if status == "met"}
    assert done == {f"r{n}" for n in (1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14)}, statuses
    # r4: nothing is shown and the reply says why; r11 cannot be judged here (no audio).
    assert run.receipts["r4"].reason == UNSAID
    assert not [c for t in run.recipe.tracks for c in t.clips if REC_IDS[5] in c.source_asset_id]
    assert statuses["r11"] == "partial"
    assert len(run.labels()) == 5 and run.drawing_layers == ["closing-arrow"]
    # Every row that is not Done is in the list whole, and the list fits.
    for rid, receipt in run.receipts.items():
        if receipt.status != "met":
            line = f"({receipt.reason.rstrip('.')})"
            assert line in run.reply, (rid, line)
    _whole_lines(run.reply)
    assert len(run.reply) <= bc.MAX_REPLY_CHARS


# ============================== Case C: every new switch off ==================================


def test_control_hold_1_on_the_per_clip_filing(monkeypatch):
    brief, report = _readiness(monkeypatch, "v1", fixes=False)
    assert report["ready"] is False and report["policy_problem"] == WHICH_CLIP
    assert _held(report) == {f"r{n}": WHICH_CLIP for n in range(1, 15)}
    assert readiness_reply(brief, report).splitlines()[1] == WHICH_CLIP


def test_control_hold_2_on_the_triggered_filing(monkeypatch):
    _brief_, report = _readiness(monkeypatch, "v2", fixes=False)
    assert report["ready"] is False
    assert _held(report) == {"r4": NO_POP_IN, "r14": TALKING_CANT}


def test_control_the_video_prod_made():
    run = _replay("C")
    labels = {r["text"]: r for r in run.labels()}

    # Label 4 inside "Trading Agents", under her face's chin line; label 5 never heard.
    assert all(r["id"].startswith("spoken-text-") for r in labels.values())
    assert labels["4. Agents"]["start_s"] == pytest.approx(TRADING_AGENTS_AT, abs=0.05)
    assert "5. Claude code game studio" not in labels
    assert run.receipts["r9"].status == "partial"
    # The ending block over her corner face.
    keyword = next(r for r in run.rows("closing_title") if r["text"] == "REPO")
    assert run.ink[keyword["id"]].intersection_area(CORNER_FACE) > 0
    # Captions centred on her: the grey box over her corner face.
    opening_end = run.recipe.speaker_background.full_screen_windows[0].end
    drawn = [(s, box) for s, _e, _w, box in _drawn_captions(run) if s >= opening_end - 1e-6]
    on_face = [box for _s, box in drawn if box.intersection_area(CORNER_FACE)]
    assert run.variant["caption_look_receipt"]["position_x_frac"] == 0.5
    assert len(on_face) >= len(drawn) * 0.7, (len(on_face), len(drawn))
    # The done-list cut mid-line at the cap.
    assert len(run.reply) == bc.MAX_REPLY_CHARS
    assert run.reply.endswith("…") and not run.reply.splitlines()[-1].startswith("- …and ")
