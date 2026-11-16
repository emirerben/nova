"""Çiğdem S1 (prod 2026-11-15, thread 51646943, job 4bb9842c): text over full-screen posts.

One talking take, four Visuals shown full screen on her words ("launched", "five years", "hinge",
"billboards"), two of them Instagram-post screenshots with a big headline and the account name
("vela") at the top. She asked: captions small, white, lowercase, in the middle, never on her
face; a small "Yorumla" label top left for the whole video. Server e74148c6c, protocol 51,
CAPTION_LEGIBILITY_ENABLED on.

What the phone drew: during both post screenshots the caption sat inside the post's headline
(KRI-1490) and "Yorumla" sat on the "vela" account name (KRI-1491). The legibility pass of
#2258 (KRI-1343/1344) never ran on this job: no `caption_legibility` trace event, stroke 0,
no shadow, no line moves.

Real: the brief exactly as filed (the job's binding), the strategy the planner approved, the
phone Talking worker (`_run_generative_job` -> `_run_phone_subtitled_job`: protected-face
captions, corner text, word-triggered full-screen beats, the compiler, the caption legibility
pass, recipe validation) and the recipe it pins. Fixture: transport only. Her words are the
job's `overlay_transcript`; her face is a box over the upper middle; the four pictures are
drawn here at the screenshots' 1170x2080 with their text where the real posts have it. No
speech cleanup (it needs her audio); no model call; no private media.

Ways the fix could fail (written before the fix):
1. The legibility pass is skipped because the face-protection step handed the compiler a copy
   of the policy look (identity check), so no pass runs on any compile of this route.
2. The pass runs but the pictures are not registered (beats arrive as lanes the pass ignores).
3. The pass runs, but on a later compile with a different look object, so its moves are lost.
4. A moved line lands on her face while the take is still up.
5. The creator's look changes: colour, size, case or her "middle" spot outside the pictures.
6. The corner label still sits on the post's account name while a post is up.
7. The corner label leaves its corner on the talking footage (it must stay top left there).
8. The corner label is moved on a calm picture (wrong-content control).
9. The label row is split or hidden in a way the brief check reads as "not the whole video".
10. The switch off changes anything.
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as rgm
import app.services.phone_visuals as phone_visuals_mod
import app.services.pipeline_trace as pipeline_trace
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.transcribe import Word
from app.services import phone_text_styling as styling
from app.services.client_protocol import clear_request_context
from app.services.device_render import device_status
from app.services.phone_sources import PhoneVisualBinding
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES
from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts"
TAKE_S = 40.122  # the job's `silence_cut.original_duration_s`
PROTOCOL = 51  # protocol-literal-ok: `all_candidates.creator_client_protocol` of the job
CANVAS_W, CANVAS_H = 1080.0, 1920.0
FACE = NormalizedBox(0.30, 0.22, 0.70, 0.54)  # her face and hair in the take (frame at 5 s)

# The job's `overlay_transcript` (whisper words, source seconds).
WORDS = [
    ["Okay,", 0.0, 0.46], ["starting.", 0.84, 1.26], ["Ten", 2.16, 2.28],
    ["minutes", 2.28, 2.62], ["after", 2.62, 2.98], ["Lumo", 2.98, 3.18],
    ["launched", 3.18, 3.58], ["its", 3.58, 3.82], ["first", 3.82, 4.24],
    ["foldable", 4.24, 4.56], ["phone,", 4.56, 4.98], ["Vela", 5.54, 5.58],
    ["posted", 5.58, 5.94], ["on", 5.94, 6.16], ["Instagram", 6.16, 6.6], ["in", 6.6, 6.94],
    ["pure", 6.94, 7.36], ["TikTok", 7.36, 7.82], ["language.", 7.82, 8.36],
    ["First,", 9.08, 9.1], ["Welcome,", 9.82, 9.82], ["Lumo.", 10.02, 10.3],
    ["We've", 10.48, 10.66], ["been", 10.66, 10.82], ["waiting", 10.82, 11.02],
    ["for", 11.02, 11.26], ["you", 11.26, 11.46], ["for", 11.46, 11.72], ["five", 11.72, 12.12],
    ["years.", 12.12, 12.54], ["Then,", 12.94, 13.38], ["A", 13.72, 13.92],
    ["hinge?", 13.92, 14.22], ["We", 14.58, 14.78], ["call", 14.78, 14.94],
    ["that", 14.94, 15.16], ["an", 15.16, 15.32], ["old", 15.32, 15.56],
    ["friend.", 15.56, 15.96], ["Rival", 16.88, 16.9], ["brand", 16.9, 17.26],
    ["banter", 17.26, 17.64], ["is", 17.64, 17.96], ["my", 17.96, 18.26],
    ["favorite.", 18.26, 18.66], ["Like", 18.78, 19.02], ["car", 19.02, 19.5],
    ["brands", 19.5, 19.78], ["fighting", 19.78, 20.08], ["on", 20.08, 20.52],
    ["bill...", 20.52, 20.88], ["Sorry,", 20.88, 21.16], ["let", 21.22, 21.42],
    ["me", 21.42, 21.52], ["start", 21.52, 21.66], ["that", 21.66, 21.84],
    ["again.", 21.84, 22.14], ["Rival", 22.9, 23.08], ["brand", 23.08, 23.42],
    ["banter", 23.42, 23.8], ["is", 23.8, 24.18], ["my", 24.18, 24.58],
    ["favorite.", 24.58, 25.0], ["Like", 25.06, 25.52], ["car", 25.52, 25.88],
    ["brands", 25.88, 26.14], ["fighting", 26.14, 26.46], ["on", 26.46, 26.76],
    ["billboards.", 26.76, 27.24], ["So,", 27.24, 28.06], ["how", 28.28, 28.36],
    ["do", 28.36, 28.46], ["I", 28.46, 28.64], ["read", 28.64, 28.84], ["this,", 28.84, 29.16],
    ["as", 29.28, 29.38], ["a", 29.38, 29.5], ["brand", 29.5, 29.68],
    ["consultant?", 29.68, 30.14], ["Vela", 30.96, 31.08], ["turned", 31.08, 31.52],
    ["Lumo's", 31.52, 31.96], ["big", 31.96, 32.24], ["launch", 32.24, 32.52],
    ["into", 32.52, 32.82], ["its", 32.82, 33.02], ["own", 33.02, 33.38],
    ["moment.", 33.38, 33.68], ["Humor", 34.26, 34.42], ["that", 34.42, 34.78],
    ["speaks", 34.78, 35.04], ["Gen", 35.04, 35.4], ["Z", 35.4, 35.68], ["beats", 35.68, 36.24],
    ["corporate", 36.24, 36.64], ["language.", 36.64, 37.14], ["So,", 37.58, 38.0],
    ["whose", 38.0, 38.12], ["side", 38.12, 38.32], ["are", 38.32, 38.42],
    ["you", 38.42, 38.64], ["on?", 38.64, 38.86], ["Tell", 39.1, 39.28], ["me", 39.28, 39.4],
    ["in", 39.4, 39.5], ["the", 39.5, 39.96], ["comments.", 39.96, 39.96],
]  # fmt: skip

_NS = uuid.UUID("00000000-0000-4000-8000-00000004bb98")
LAUNCH, POST1, POST2, BILLBOARD = (
    str(uuid.uuid5(_NS, name)) for name in ("launch", "post1", "post2", "billboard")
)
PICTURES = {LAUNCH: "IMG_7101.JPG", POST1: "IMG_7102.PNG", POST2: "IMG_7103.PNG"}
PICTURES[BILLBOARD] = "IMG_7107.JPG"

REQUEST = (
    "Yorumla episode 8: Vela's reply to Lumo. Use my talking video and cut the dead air and the "
    "sentence I restarted. When I mention the launch, Vela's 2 posts and the billboards, show "
    "that picture full screen while my voice keeps going. Captions small, white, lowercase, in "
    'the middle of the screen, never on my face. A small "Yorumla" label in the top left for the '
    "whole video. No music."
)

STRATEGY = {
    "pacing": "balanced",
    "direction": "native",
    "rationale": "Word-triggered full-screen pictures; small white lowercase middle captions.",
    "edit_format": "subtitled",
    "pinned_texts": [{"text": "Yorumla", "corner": "top_left"}],
    "caption_style": "clean",
    "audio_strategy": "original_audio",
    "reaction_beats": [
        {
            "beat_id": beat_id,
            "trigger": trigger,
            "visual_id": f"asset-{media_id}",
            "occurrence": "first",
            "visual_role": "photo",
            "display_mode": "fullscreen",
        }
        for beat_id, trigger, media_id in (
            ("launch", "launched", LAUNCH),
            ("first-post", "five years", POST1),
            ("second-post", "hinge", POST2),
            ("billboards", "billboards", BILLBOARD),
        )
    ],
    "render_program": "native",
    "story_structure": [],
    "target_duration_s": 30,
    "selected_media_ids": ["c0"],
    "video_reuse_policy": "once",
    "optional_treatments": [],
}


def _r(rid, kind, description, *, literal=None, facts=None):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope="global",
        description=description,
        literal=literal,
        facts=facts or {},
        source_turn_id="4bb9842c-5ffb-4b23-8d0b-00f6152a4c4b",
    )


def _beat_req(rid, what, word):
    return _r(
        rid,
        "style",
        f'Show {what} full screen when I say "{word}", while my voice keeps going.',
        facts={"triggers": [word], "display_mode": "fullscreen"},
    )


# Brief v4 as the job's binding holds it (live requirements only).
BRIEF = CreativeBrief(
    version=4,
    requirements=[
        _r(
            "r2",
            "style",
            "Cut the dead air and the sentence I restarted.",
            facts={"cut_restarts": True, "speech_cleanup": True},
        ),
        _beat_req("r3", "the launch photo", "launched"),
        _r(
            "r4",
            "style",
            "Captions small, white, lowercase, in the middle of the screen.",
            facts={
                "captions": "on",
                "caption_case": "lower",
                "caption_size": "small",
                "caption_color": "#FFFFFF",
                "caption_position": "middle",
            },
        ),
        _r(
            "r5",
            "style",
            "Captions never on my face.",
            facts={"keep_text_off": [{"kind": "face"}]},
        ),
        _r(
            "r6",
            "text",
            'A small "Yorumla" label in the top left for the whole video.',
            literal="Yorumla",
            facts={"size": "small", "position": "top_left"},
        ),
        _r("r8", "audio", "No music.", facts={"audio_intent": "no_music"}),
        _beat_req("r9", "the first Vela post", "five years"),
        _beat_req("r10", "the second Vela post", "hinge"),
        _beat_req("r11", "the billboard photo", "billboards"),
    ],
)

PROD_ON = (
    "phone_text_styling_enabled",
    "phone_caption_shape_enabled",
    "phone_subtitled_title_enabled",
    "phone_talking_corner_text_enabled",
    "title_look_enabled",
)
VERIFIED = (*PROD_VERIFIED_FEATURES, "captionShape", "cardLayout", "sourceCrop")


# ------------------------------------------------------------------ the pictures (drawn here)

# Ground truth for the judge: every text box drawn on each picture, canvas fractions (the
# pictures are 9:16-ish and cover-fit the 9:16 canvas, so picture fractions == canvas fractions
# to within the 1170x2080 vs 1080x1920 aspect difference, which is 0).
TEXT_BOXES: dict[str, list[tuple[float, float, float, float]]] = {}
NAME_BOXES: dict[str, tuple[float, float, float, float]] = {}


def _font(size: int):
    for name in ("Inter-Bold.ttf", "PlayfairDisplay-Bold.ttf"):
        path = next(FONT.rglob(name), None)
        if path is not None:
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _post(path: Path, media_id: str, headline: list[str], top: float, size: int = 46) -> None:
    """An Instagram post screenshot as hers: black page, the status bar, the avatar and the
    account name "vela" at 0.09, the headline in big white bold from ``top``, the likes and
    caption below."""
    w, h = 585, 1040
    image = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(image)
    boxes: list[tuple[float, float, float, float]] = []

    def text(xy, line, font_px, fill="white"):
        font = _font(font_px)
        x0, y0, x1, y1 = d.textbbox(xy, line, font=font)
        boxes.append((x0 / w, y0 / h, min(x1 / w, 1.0), y1 / h))
        d.text(xy, line, fill=fill, font=font)

    text((int(0.07 * w), int(0.02 * h)), "20:14", 20)
    d.ellipse((int(0.04 * w), int(0.085 * h), int(0.12 * w), int(0.13 * h)), fill=(40, 90, 200))
    text((int(0.14 * w), int(0.093 * h)), "vela", 24)
    NAME_BOXES[media_id] = boxes[-1]
    for index, line in enumerate(headline):
        text((int(0.06 * w), int(top * h + index * size * 1.25)), line, size)
    text((int(0.04 * w), int(0.86 * h)), "24.1K likes", 20)
    text((int(0.04 * w), int(0.89 * h)), "vela #VelaFlip", 20)
    text((int(0.04 * w), int(0.92 * h)), "12 minutes ago", 16, fill=(150, 150, 150))
    TEXT_BOXES[media_id] = boxes
    image.save(path)


def _photo(path: Path, media_id: str, colour: tuple[int, int, int]) -> None:
    """A calm product photo: a soft gradient, an object in the middle, no text."""
    w, h = 585, 1040
    image = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    image = Image.blend(image, Image.new("RGB", (w, h), colour), 0.7)
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((int(0.3 * w), int(0.35 * h), int(0.7 * w), int(0.6 * h)), 30, fill=colour)
    TEXT_BOXES[media_id] = []
    image.save(path)


def _pictures(directory: Path, *, posts: bool = True) -> dict[str, Path]:
    """Her four pictures; ``posts=False`` draws the two posts as calm photos (the control)."""
    files = {media_id: directory / f"{media_id}.png" for media_id in PICTURES}
    _photo(files[LAUNCH], LAUNCH, (70, 70, 80))
    _photo(files[BILLBOARD], BILLBOARD, (90, 120, 150))
    if posts:
        headline = ["Welcome, Lumo.", "We've been waiting for", "you for 5 years."]
        _post(files[POST1], POST1, headline, 0.56)
        _post(files[POST2], POST2, ["A hinge?", "We call that", "an old", "friend."], 0.24, 80)
    else:
        _photo(files[POST1], POST1, (20, 20, 24))
        _photo(files[POST2], POST2, (24, 20, 20))
    return files


# ------------------------------------------------------------------ the worker


def _pool_rows() -> list[dict]:
    return [
        {
            "id": media_id,
            "gcs_path": f"users/u1/plan/item1/pool/{media_id}.png",
            "gcs_generation": "7",
            "kind": "image",
            "source_filename": name,
            "duration_s": None,
            "aspect": 1170 / 2080,
            "user_context": "",
            "analysis": {"subject": name},
            "capture": None,
        }
        for media_id, name in PICTURES.items()
    ]


def _bind(_open, *, job_id, pins, measure_text=False):  # noqa: ARG001
    return tuple(
        PhoneVisualBinding(
            media_id=media_id,
            gcs_path=f"users/u1/plan/item1/pool/{media_id}.png",
            generation="7",
            sha256="b" * 64,
            byte_count=100,
            kind="image",
        )
        for media_id in pins
    )


def _faces(_path, anchors, **_kwargs):
    regions = [ProtectedRegion(max(0.0, at - 0.5), at + 0.5, FACE, "face") for at in anchors]
    return regions, {"attempted": len(anchors), "decoded": len(anchors), "detected": len(regions)}


def _run(monkeypatch, files: dict[str, Path], *, legibility: bool = True) -> dict:
    """The worker run; returns the variant and the pinned recipe (measured inside the patch)."""
    import app.storage as storage

    clear_request_context()
    words = [Word(text=t, start_s=a, end_s=b, confidence=1.0) for t, a, b in WORDS]
    job, snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=words, duration_s=TAKE_S
    )
    _enable_beats(monkeypatch)
    for name in PROD_ON:
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "caption_legibility_enabled", legibility)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(VERIFIED))
    monkeypatch.setattr(ogm.settings, "gemini_api_key", "", raising=False)
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((stage, event, data or {})),
    )
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(ogm, "sample_face_regions", _faces)
    monkeypatch.setattr(title_mod, "sample_face_regions", _faces)

    def download(remote, local, *args, **kwargs):  # noqa: ARG001
        for media_id, path in files.items():
            if media_id in str(remote):
                Path(local).write_bytes(path.read_bytes())
                return local
        raise FileNotFoundError(remote)

    monkeypatch.setattr(storage, "download_to_file", download)

    def environment(canvas):  # noqa: ARG001 - no take frames to sample contrast from
        policy = styling._POLICY.get()
        policy["_face_boxes"] = [
            {"box": (FACE.left, FACE.top, FACE.right, FACE.bottom), "start_s": 0.0, "end_s": TAKE_S}
        ]
        policy["_environment_sampled"] = True

    monkeypatch.setattr(styling, "_ensure_generation_environment", environment)
    job.all_candidates.update(
        {
            "creator_request": REQUEST,
            "creator_strategy": copy.deepcopy(STRATEGY),
            "creator_client_protocol": PROTOCOL,
            "landscape_fit": "fit",
        }
    )
    binding = BriefBinding.create(uuid.uuid4(), BRIEF).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)
    stamp_phone_text_style(job)
    job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return {
        "variant": job.assembly_plan["variants"][0],
        "recipe": device_status(job, "subtitled").request.recipe,
        "events": events,
    }


_RUNS: dict[str, dict] = {}


def _replay(case: str, tmp_path_factory) -> dict:
    """One worker run per case per process: "fixed" (her posts, the switch on), "calm" (the
    posts drawn as calm photos), "off" (her posts, CAPTION_LEGIBILITY_ENABLED off)."""
    if case not in _RUNS:
        files = _pictures(tmp_path_factory.mktemp(case), posts=case != "calm")
        with pytest.MonkeyPatch.context() as patch:
            _RUNS[case] = _run(patch, files, legibility=case != "off")
    return _RUNS[case]


# ------------------------------------------------------------------ the independent judge


def _layer_box(layer) -> tuple[float, float, float, float]:
    """A text layer's drawn block (canvas fractions), conservatively: each run from its pen x
    over 0.62 em per character, from 0.85 em above its baseline to 0.3 em below, plus its
    outline (the phone lays shaped runs out itself; this over-covers, never under-covers)."""
    xs, ys = [], []
    for run in layer.runs:
        bleed = (run.stroke_width or 0) / 2
        xs += [run.x - bleed, run.x + 0.62 * run.font_size * len(run.text) + bleed]
        ys += [
            run.baseline_y - 0.85 * run.font_size - bleed,
            run.baseline_y + 0.3 * run.font_size + bleed,
        ]
    return (min(xs) / CANVAS_W, min(ys) / CANVAS_H, max(xs) / CANVAS_W, max(ys) / CANVAS_H)


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _face() -> tuple[float, float, float, float]:
    return (FACE.left, FACE.top, FACE.right, FACE.bottom)


def _windows(recipe) -> dict[str, tuple[float, float]]:
    """Each full-screen picture's window on the recipe timeline, by media id."""
    return {
        clip.source_asset_id.removeprefix("visual-"): (
            clip.timeline_start,
            clip.timeline_start + clip.source_duration,
        )
        for track in recipe.tracks
        if track.kind == "overlay"
        for clip in track.clips
    }


def _captions(recipe):
    return [layer for layer in recipe.text_layers if layer.id.startswith("caption-")]


def _label(recipe):
    [layer] = [
        layer
        for layer in recipe.text_layers
        if not layer.id.startswith("caption-") and layer.runs[0].text == "Yorumla"
    ]
    return layer


def _over(layer, window) -> bool:
    return layer.start < window[1] and window[0] < layer.end


def _events(run: dict, name: str) -> list[dict]:
    return [data for _stage, event, data in run["events"] if event == name]


# ------------------------------------------------------------------ KRI-1490: the captions


def test_the_legibility_pass_runs_on_captions_kept_off_her_face(tmp_path_factory):
    """1, 2, 3: her face ask moved the captions (a copy of the generated look), and the pass
    still judges the final compile with all four pictures; its receipt reaches the variant."""
    run = _replay("fixed", tmp_path_factory)

    receipts = _events(run, "caption_legibility")
    assert receipts, "the legibility pass never ran (prod job 4bb9842c)"
    assert receipts[-1]["pictures"] == 4
    assert run["variant"]["caption_legibility"] == receipts[-1]
    assert receipts[-1]["moved"], receipts[-1]
    assert set(run["variant"]["caption_editor_style"]["line_overrides"]) == {
        moved["cue"] for moved in receipts[-1]["moved"]
    }


def test_no_caption_line_sits_on_a_post_screenshots_own_text(tmp_path_factory):
    """5: while each post is up, every caption line is clear of every text the post shows."""
    run = _replay("fixed", tmp_path_factory)
    windows = _windows(run["recipe"])

    for post in (POST1, POST2):
        shown = [layer for layer in _captions(run["recipe"]) if _over(layer, windows[post])]
        assert shown, post
        for layer in shown:
            box = _layer_box(layer)
            on = [text for text in TEXT_BOXES[post] if _overlaps(box, text)]
            assert not on, (post, layer.id, layer.runs[0].text, box, on)


def test_moved_lines_keep_off_her_face_and_her_look_is_kept(tmp_path_factory):
    """4, 5: white, 42 px, lowercase everywhere; a line drawn over her take (fully or in part)
    never touches her face; a line no picture covers stays at her spot."""
    run = _replay("fixed", tmp_path_factory)
    windows = list(_windows(run["recipe"]).values())
    spot = run["variant"]["caption_editor_style"]["position_y_frac"]
    assert run["variant"]["caption_text_color"] == "#FFFFFF"
    assert run["variant"]["caption_size_px"] == 42

    for layer in _captions(run["recipe"]):
        for item in layer.runs:
            assert (item.fill.red, item.fill.green, item.fill.blue) == (1.0, 1.0, 1.0)
            assert item.font_size == 42.0
            assert item.text == item.text.lower()
        covered = sum(
            max(0.0, min(layer.end, end) - max(layer.start, start)) for start, end in windows
        )
        if covered < layer.end - layer.start - 0.05:
            assert not _overlaps(_layer_box(layer), _face()), (layer.id, _layer_box(layer))
        if covered == 0:
            centre = (_layer_box(layer)[1] + _layer_box(layer)[3]) / 2
            assert abs(centre - spot) <= 0.04, (layer.id, centre, spot)


# ------------------------------------------------------------------ KRI-1491: the corner label


def test_the_label_keeps_its_top_left_corner_and_leaves_the_account_name(tmp_path_factory):
    """6, 7, 9: one "Yorumla" row for the whole video, still top left (inside the brief's
    tolerance of its anchor), off her face, and on neither post's account name nor any of their
    text while they are up."""
    run = _replay("fixed", tmp_path_factory)
    recipe = run["recipe"]
    label = _label(recipe)
    windows = _windows(recipe)
    box = _layer_box(label)

    assert label.start == 0.0 and label.end >= max(c.end for c in _captions(recipe)) - 1e-6
    for post in (POST1, POST2):
        assert _over(label, windows[post])
        assert not _overlaps(box, NAME_BOXES[post]), (post, box, NAME_BOXES[post])
        assert not any(_overlaps(box, text) for text in TEXT_BOXES[post]), (post, box)
    assert not _overlaps(box, _face())
    [row] = [
        r
        for r in run["variant"]["text_elements"]
        if (r.get("source_params") or {}).get("source") == "corner_text"
    ]
    assert row["source_params"]["whole_video"] is True and row["alignment"] == "left"
    assert row["x_frac"] == 0.08 and 0.12 <= row["y_frac"] <= 0.20
    [entry] = run["variant"]["corner_text_placement"]["rows"]
    assert entry["status"] == "placed" and entry["picture_text"] == 0
    assert entry["moved_off"] == ["picture_text"]


def test_calm_pictures_leave_the_label_exactly_where_it_was(tmp_path_factory):
    """8 (wrong-content control): the same four windows with no text in the pictures: the label
    is placed once, at its corner, as before."""
    run = _replay("calm", tmp_path_factory)

    placements = _events(run, "talking_corner_text_placement")
    assert len(placements) == 1
    [row] = [
        r
        for r in run["variant"]["text_elements"]
        if (r.get("source_params") or {}).get("source") == "corner_text"
    ]
    assert (row["x_frac"], row["y_frac"]) == (0.08, 0.12)
    assert "picture_text" not in run["variant"]["corner_text_placement"]["rows"][0]


def test_switch_off_keeps_prods_render(tmp_path_factory):
    """10: CAPTION_LEGIBILITY_ENABLED off: no pass, no outline, no moved lines, the label placed
    once at its corner (what production drew for job 4bb9842c)."""
    run = _replay("off", tmp_path_factory)

    assert not _events(run, "caption_legibility")
    assert len(_events(run, "talking_corner_text_placement")) == 1
    assert run["variant"]["caption_stroke_width"] == 0
    assert "line_overrides" not in run["variant"]["caption_editor_style"]
    assert "caption_legibility" not in run["variant"]
    [row] = [
        r
        for r in run["variant"]["text_elements"]
        if (r.get("source_params") or {}).get("source") == "corner_text"
    ]
    assert row["y_frac"] == 0.12
