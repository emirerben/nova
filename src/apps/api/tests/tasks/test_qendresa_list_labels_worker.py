"""Qendresa item 19 (Q2): numbered list labels on a phone Talking screen walkthrough.

Her V5: five GitHub repos, five screen recordings taking turns behind her while she is drawn
small in the bottom-right corner. Her ask: 'Add a label when each one starts: "1. Spec-kit" ...'.
The approved brief holds each label as a global `text` requirement with the literal and NO spoken
trigger (`facts={}`). Behind `SPOKEN_LIST_LABELS_ENABLED` the worker adds one ordinary text row
per label, starting when that repo's part of the talk starts (where its background window opens),
never the first time the word is ever said.

Written before the code. The numbers map to `docs/reviews/qendresa-list-labels-failure-modes.md`
(Q2 list): 1 wrong hearing, 3 lead-in, 5 wrong pairing, 6 unanchored, 7 overlap, 8 her face,
9 captions, 11 swept-in texts, 12 double row, 13 receipts, 14 flag off.

No model calls, no network: `_run_generative_job` over a fake ready pool, synthetic timed words.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid

import pytest

import app.pipeline.phone_subtitled_title as title_mod
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.pipeline.phone_spoken_text import caption_boxes
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
from app.pipeline.phone_subtitled_title import _measure_title_box
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.transcribe import Word
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)

TAKE_S = 130.0
WORD_S = 0.15

# ------------------------------------------------------------------ her take (synthetic words)

# (start_s, sentence). Words run 0.15 s apart from the sentence start; gaps between sentences.
SENTENCES = [
    (0.0, "five github repositories the ai builder community cannot stop talking about right now"),
    (7.4, "first one is spec kit by github itself"),
    (12.7, "you write the spec and spec kit builds the plan"),
    (17.2, "then spec kit turns the plan into tasks"),
    (32.5, "next one is impeccable it has 96000 stars"),
    (54.3, "third one is trading agents a whole trading firm as agents for analysts"),
    (77.5, "fourth one is agents over 200 agents"),
    (95.0, "and last one is cloud code game studios 49 agents"),
    (118.0, "comment repo and i'll send you the file"),
]


def _words() -> list[Word]:
    out: list[Word] = []
    for start, sentence in SENTENCES:
        for i, text in enumerate(sentence.split()):
            out.append(
                Word(
                    text=text,
                    start_s=round(start + i * WORD_S, 3),
                    end_s=round(start + (i + 1) * WORD_S - 0.02, 3),
                    confidence=1.0,
                )
            )
    return out


WORDS = _words()


def _said_at(sentence_start: float, word: str) -> float:
    """The start of ``word`` inside the sentence that begins at ``sentence_start`` (from the
    word list itself, not a typed-in second)."""
    for w in WORDS:
        if abs(w.start_s - sentence_start) < 1e-6 and w.text == word:
            return w.start_s
    sentence = dict(SENTENCES)[sentence_start].split()
    return round(sentence_start + sentence.index(word) * WORD_S, 3)


# The introduction word of each repo (where the label belongs) and where it is also said earlier.
INTRO_AT = {
    "1. Spec-kit": _said_at(7.4, "spec"),
    "2. Impeccable": _said_at(32.5, "impeccable"),
    "3. TradingAgents": _said_at(54.3, "trading"),
    "4. Agents": _said_at(77.5, "agents"),
    "5. Claude code game studio": _said_at(95.0, "cloud"),
}
LABELS = list(INTRO_AT)
GHOST = "6. Ghost repo"
TITLE = "5 PROJECTS / the AI builder community / can't stop talking about!!!!"
CLOSING = "comment REPO"
# Words a naive "first hearing" would pick for label 4: inside "trading agents" and "as agents".
TRADING_AGENTS_AT = _said_at(54.3, "agents")
AS_AGENTS_AT = round(54.3 + SENTENCES[5][1].split().index("as") * WORD_S + WORD_S, 3)
TOLERANCE_S = 0.5
MIN_SHOWN_S = 1.5

POOL_IDS = [f"{n:032x}" for n in range(0xA1, 0xA6)]
PHRASES = [
    "spec kit",
    "impeccable",
    "trading agents",
    "fourth one is agents",
    "cloud code game studios",
]
SEQUENCE_BACKGROUND = {
    "media_id": f"asset-{POOL_IDS[0]}",
    "sequence": [
        {"media_id": f"asset-{media}", "spoken_phrase": phrase}
        for media, phrase in zip(POOL_IDS, PHRASES, strict=True)
    ],
    "speaker_frame": "small_bottom_right",
}

# Her full-frame face, and where it sits once drawn in the small bottom-right frame.
FULL_FRAME_FACE = NormalizedBox(0.35, 0.25, 0.65, 0.50)
CORNER_FACE = NormalizedBox(0.697, 0.695, 0.823, 0.80)


def _req(rid: str, text: str, *, scope: str = "global", facts=None) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="text",
        scope=scope,
        description=f'Label "{text}" for its repo' if scope == "global" else "The title",
        literal=text,
        facts=facts if facts is not None else {},
    )


LABEL_REQS = [_req(f"l{i}", text) for i, text in enumerate(LABELS, start=1)]
GHOST_REQ = _req("l6", GHOST)
TITLE_REQ = _req("t1", TITLE, scope="title")
CLOSING_REQ = _req("c1", CLOSING)


def _brief(*extra: BriefRequirement, labels=None) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[*(labels or LABEL_REQS), GHOST_REQ, TITLE_REQ, CLOSING_REQ, *extra],
    )


def _pool_rows():
    return [
        {
            "id": media,
            "gcs_path": f"users/u1/plan/item/pool/{media}.mp4",
            "gcs_generation": "7",
            "kind": "video",
            "source_filename": f"repo{i}.mp4",
            "duration_s": 20.0,
            "aspect": 0.5625,
            "user_context": "",
            "analysis": {},
            "capture": None,
        }
        for i, media in enumerate(POOL_IDS, start=1)
    ]


def _faces(monkeypatch, box: NormalizedBox = FULL_FRAME_FACE) -> None:
    """The title placer's face sampler returns her full-frame face for the whole take."""

    def sample(path, anchors, **kwargs):  # noqa: ARG001
        return [ProtectedRegion(0.0, TAKE_S, box, "face")], {
            "attempted": len(anchors),
            "decoded": len(anchors),
        }

    monkeypatch.setattr(title_mod, "sample_face_regions", sample)


def _label_job(
    monkeypatch, *, brief: CreativeBrief | None = None, background=None, follows_talk: bool = True
):
    import app.services.phone_overlay_grounding as ogm
    import app.services.phone_reaction_grounding as rgm
    import app.services.phone_visuals as phone_visuals_mod
    from app.services.phone_sources import PhoneVisualBinding
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

    rows = {row["id"]: row for row in _pool_rows()}

    def bind(_open, *, job_id, pins, measure_text=False):  # noqa: ARG001 - item 18's measure
        return tuple(
            PhoneVisualBinding(
                media_id=media,
                gcs_path=rows[media]["gcs_path"],
                generation="7",
                sha256="a" * 64,
                byte_count=100,
                kind="video",
                duration_s=20.0,
                width=1080,
                height=1920,
                orientation_degrees=0,
            )
            for media in pins
        )

    job, snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=WORDS, duration_s=TAKE_S
    )
    _enable_beats(monkeypatch, extra_features=("visualVideos", "authoredText"))
    monkeypatch.setattr(gb.settings, "phone_subtitled_video_overlays_enabled", True)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    clear_request_context()
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    # Labels follow the recordings only with item 17 on (`follows_talk=False`: they ground on
    # their own words).
    monkeypatch.setattr(settings, "background_follows_talk_enabled", follows_talk)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            *gb.settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
            "positionedText",
            "animatedText",
        ],
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "opening_title": TITLE,
        "background_media": copy.deepcopy(background or SEQUENCE_BACKGROUND),
    }
    binding = BriefBinding.create(uuid.uuid4(), brief or _brief()).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)
    _faces(monkeypatch)
    return job


def _flag(monkeypatch, value: bool) -> None:
    """`SPOKEN_LIST_LABELS_ENABLED`. The setting is not declared yet (pydantic refuses
    `setattr` on an unknown field), so write the instance dict, undone at teardown."""
    monkeypatch.setitem(settings.__dict__, "spoken_list_labels_enabled", value)


def _run(monkeypatch, *, flag: bool, **kw):
    _flag(monkeypatch, flag)
    job = _label_job(monkeypatch, **kw)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job, job.assembly_plan["variants"][0]


def _label_rows(variant: dict) -> list[dict]:
    rows = [r for r in variant.get("text_elements") or [] if r["id"].startswith("list-label-")]
    return sorted(rows, key=lambda r: r["start_s"])


def _by_text(variant: dict) -> dict[str, dict]:
    return {r["text"]: r for r in _label_rows(variant)}


@pytest.fixture
def on(monkeypatch):
    return _run(monkeypatch, flag=True)


# ------------------------------------------------------------------ flag on


def test_01_five_labels_start_when_each_repo_is_introduced(on):
    """FM 1, 3, 5: one row per label, id `list-label-<req id>`, text = the literal; the start is
    the introduction, not the first time the word is said, and not the opening lead-in."""
    _job, variant = on
    rows = _label_rows(variant)
    assert [r["text"] for r in rows] == LABELS
    assert [r["id"] for r in rows] == [f"list-label-l{i}" for i in range(1, 6)]
    by_text = _by_text(variant)
    for text, intro in INTRO_AT.items():
        assert abs(by_text[text]["start_s"] - intro) <= TOLERANCE_S, (text, by_text[text], intro)
    four = by_text["4. Agents"]["start_s"]
    assert abs(four - TRADING_AGENTS_AT) > 1.0 and abs(four - AS_AGENTS_AT) > 1.0
    assert by_text["1. Spec-kit"]["start_s"] > 5.0, "not on the opening lead-in at 0 s"


def test_02_labels_last_at_least_1_5s_never_overlap_and_stay_in_the_take(on):
    """FM 7, 18: >= 1.5 s or until the next label starts; never two at once; none past the end."""
    _job, variant = on
    rows = _label_rows(variant)
    assert len(rows) == 5
    for row, nxt in zip(rows, [*rows[1:], None], strict=True):
        shown = row["end_s"] - row["start_s"]
        assert shown >= MIN_SHOWN_S - 1e-6 or (
            nxt and row["end_s"] == pytest.approx(nxt["start_s"])
        )
        if nxt:
            assert row["end_s"] <= nxt["start_s"] + 1e-6, "two labels at once"
        assert row["end_s"] <= TAKE_S + 1e-6


def test_03_the_device_recipe_draws_the_labels_at_the_same_times(on):
    """FM 16: ordinary text layers on the compiled recipe (no new phone capability)."""
    job, variant = on
    recipe = _recipe(job)
    layers = {}
    for layer in recipe.text_layers:
        # A long label wraps onto several runs (one per line): read them as one line of text.
        text = " ".join(run.text for run in layer.runs)
        if text in LABELS:
            layers[text] = layer
    assert sorted(layers) == sorted(LABELS)
    rows = _by_text(variant)
    for text, layer in layers.items():
        assert layer.start == pytest.approx(rows[text]["start_s"], abs=0.02)
        assert layer.end == pytest.approx(rows[text]["end_s"], abs=0.02)


def _overlap(a: NormalizedBox, b: NormalizedBox) -> float:
    return a.intersection_area(b)


def test_04_labels_stay_off_her_corner_face(on):
    """FM 8: the placer's full-frame face is not where she is drawn; the corner face is."""
    _job, variant = on
    rows = _label_rows(variant)
    assert len(rows) == 5
    for row in rows:
        box = _measure_title_box(row, canvas=_STORY_CANVAS)
        assert box.right <= 1.0 + 1e-6 and box.left >= -1e-6, ("off the canvas", row["text"], box)
        assert _overlap(box, CORNER_FACE) == 0.0, (row["text"], box, CORNER_FACE)


def test_05_labels_stay_off_the_captions(on):
    """FM 9: no label box overlaps a caption box on screen in the label's window."""
    _job, variant = on
    rows = _label_rows(variant)
    assert len(rows) == 5
    cues = variant["caption_cues"]
    # The worker's default: "word" only when the candidate asks `voiceover_caption_style == "word"`.
    style = "sentence"
    for row in rows:
        window = (row["start_s"], row["end_s"])
        label_box = _measure_title_box(row, canvas=_STORY_CANVAS)
        for box in caption_boxes(cues, window=window, caption_style=style, canvas=_STORY_CANVAS):
            assert _overlap(label_box, box) == 0.0, (row["text"], label_box, box)


def test_06_title_closing_and_the_unheard_label_get_no_label_row(on):
    """FM 6, 11, 13: only the five get rows; "6. Ghost repo" is reported, not placed."""
    _job, variant = on
    assert len(_label_rows(variant)) == 5
    texts = [r["text"] for r in variant["text_elements"]]
    assert GHOST not in texts and CLOSING not in texts
    assert texts.count(TITLE) <= 1
    receipt = variant["list_label_placement"]
    assert receipt["version"] == 1
    by_req = {r["requirement_id"]: r for r in receipt["rows"]}
    assert set(by_req) == {"l1", "l2", "l3", "l4", "l5", "l6"}, "title/closing are not labels"
    assert by_req["l6"]["status"] != "placed"
    assert by_req["l6"]["status"] == "not_heard"
    for rid in ("l1", "l2", "l3", "l4", "l5"):
        assert by_req[rid]["status"] == "placed"
        assert by_req[rid]["start_s"] is not None and by_req[rid]["end_s"] is not None


def _after(variant: dict, req: BriefRequirement):
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


def test_07_after_render_receipts_five_met_ghost_not(on):
    """FM 13: finished-video receipts."""
    _job, variant = on
    for req in LABEL_REQS:
        assert _after(variant, req).status == "met", req.literal
    ghost = _after(variant, GHOST_REQ)
    assert ghost.status != "met"
    assert "met" not in (ghost.reason or "").lower().split()
    assert "done" not in (ghost.reason or "").lower()


def _draft(req: BriefRequirement, monkeypatch, *, flag: bool):
    import dataclasses

    _flag(monkeypatch, flag)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )
    strategy = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "background_media": copy.deepcopy(SEQUENCE_BACKGROUND),
    }
    facts = dataclasses.replace(plan_facts_from_strategy(strategy), phone_talking=True)
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt


def test_08_draft_receipt_for_a_label_with_a_planned_sequence(monkeypatch):
    """FM 13: with the flag on the draft no longer says the text is missing, and does not claim
    it is done before the render. Flag off: exactly today's receipt."""
    off = _draft(LABEL_REQS[0], monkeypatch, flag=False)
    assert "That exact text isn't in this draft" in (off.reason or ""), (off.status, off.reason)
    on_receipt = _draft(LABEL_REQS[0], monkeypatch, flag=True)
    assert "isn't in this draft" not in (on_receipt.reason or "")
    assert on_receipt.status != "met"
    again_off = _draft(LABEL_REQS[0], monkeypatch, flag=False)
    assert (again_off.status, again_off.reason) == (off.status, off.reason)


# ------------------------------------------------------------------ flag off


# Frozen on today's code (HEAD 2777c6f02, no list-label code in the tree): `_run(flag=False)` run
# twice, on a clean `git archive HEAD` copy and on the worktree, printed `_digests(...)`; same
# values every time. If these change, flag-off output changed. The whole recipe's digest is not
# frozen: its text geometry differs between this Mac and CI's Linux, so the recipe is compared
# within one run instead (`test_09`).
TODAY_TEXT_ELEMENTS_SHA = "f9423740063d4648215a3e57eabc5ebfcac610bcbf5048f3b44906e1f775ec0a"
TODAY_RECEIPTS_SHA = "0d637b0d2774c1365fbb35ab5ce7b142c95ca9b2365b72e2738aa941a5148572"


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _digests(job, variant) -> dict[str, str]:
    recipe = _recipe(job)
    dumped = recipe.model_dump(mode="json")
    # The required-capability list comes out of a set: its order changes between processes.
    dumped["required_capabilities"] = sorted(dumped["required_capabilities"])
    receipts = [
        (r.id if hasattr(r, "id") else None, r.status, r.reason)
        for r in build_receipts(
            [*LABEL_REQS, GHOST_REQ, TITLE_REQ, CLOSING_REQ],
            plan_facts_from_phone_variant(variant),
            include_unchecked=True,
        )
    ]
    return {
        "recipe": _sha(dumped),
        "text_elements": _sha(variant.get("text_elements")),
        "receipts": _sha(receipts),
    }


def _item19_never_runs(monkeypatch) -> None:
    """With the flag off no list-label code may run at all (the worker, receipts or contract)."""
    import app.pipeline.phone_list_labels as labels_mod

    def ran(*_a, **_k):
        raise AssertionError("list-label code ran with SPOKEN_LIST_LABELS_ENABLED off")

    for name in ("list_label_asks", "list_label_rows", "find_spoken_list", "match_labels"):
        monkeypatch.setattr(labels_mod, name, ran)
    monkeypatch.setattr(gb, "_phone_talking_list_label_rows", ran)


def test_09_flag_off_is_byte_identical_to_today(monkeypatch):
    """FM 14: flag off, nothing of item 19 runs, the rows and receipts are today's (frozen), and
    the recipe is the one the same job renders with no label in its brief (today a label with no
    spoken trigger draws nothing)."""
    _item19_never_runs(monkeypatch)
    # Item 17 off, as when the digests were frozen (the fixture's default is on).
    job, variant = _run(monkeypatch, flag=False, follows_talk=False)
    assert not _label_rows(variant)
    assert "list_label_placement" not in variant
    digests = _digests(job, variant)
    assert {key: digests[key] for key in ("text_elements", "receipts")} == {
        "text_elements": TODAY_TEXT_ELEMENTS_SHA,
        "receipts": TODAY_RECEIPTS_SHA,
    }
    plain = CreativeBrief(version=1, requirements=[TITLE_REQ, CLOSING_REQ])
    plain_job, plain_variant = _run(monkeypatch, flag=False, follows_talk=False, brief=plain)
    assert _digests(plain_job, plain_variant)["recipe"] == digests["recipe"]


def test_10_a_label_with_a_spoken_trigger_stays_kri707_and_is_drawn_once(monkeypatch):
    """FM 12: `facts.triggers` = KRI-707 draws it on the word; the list labels skip it."""
    triggered = _req("l2", "2. Impeccable", facts={"triggers": ["impeccable"]})
    labels = [LABEL_REQS[0], triggered, *LABEL_REQS[2:]]
    _job, variant = _run(monkeypatch, flag=True, brief=_brief(labels=labels))
    rows = [r for r in variant["text_elements"] if r["text"] == "2. Impeccable"]
    assert len(rows) == 1, [r["id"] for r in rows]
    assert rows[0]["id"].startswith("spoken-text-")
    assert not any(r["id"] == "list-label-l2" for r in _label_rows(variant))
    assert len(_label_rows(variant)) == 4


def _contract_run(monkeypatch, *, flag: bool):
    """The label job with the approved render contract stamped on the job as prod stamps it."""
    from app.services.creator_render_contract import CONTRACT_FIELD, build_render_contract
    from app.services.length_policy_state import stamp_length_policy

    _flag(monkeypatch, flag)
    # No closing text: this fixture draws none, so pinning it would refuse the render on its own.
    brief = CreativeBrief(version=1, requirements=[*LABEL_REQS, GHOST_REQ, TITLE_REQ])
    job = _label_job(monkeypatch, brief=brief)
    strategy = job.all_candidates["creator_strategy"]
    contract = build_render_contract(strategy, generation_id="generation", brief=brief)
    assert contract is not None
    stamped = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    stamped.update(stamp_length_policy({}, strategy, contract, mode="log_only"))
    job.assembly_plan.update(copy.deepcopy(stamped))
    gb._run_generative_job(str(job.id))
    return job, contract


def test_11_contract_does_not_pin_the_labels_so_an_unheard_one_is_not_refused(monkeypatch):
    """The approved contract leaves list labels to their own receipt: with the never-said
    "6. Ghost repo" the render still reaches the device, five labels drawn, contract kept."""
    from app.services.device_render import device_record

    job, contract = _contract_run(monkeypatch, flag=True)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    variant = job.assembly_plan["variants"][0]
    assert [r["text"] for r in _label_rows(variant)] == LABELS
    pinned = [t.text for t in contract.exact_texts]
    assert not set(pinned) & {*LABELS, GHOST}
    assert TITLE in pinned
    assert device_record(job, "subtitled").get("contract_digest")


def test_12_flag_off_the_labels_stay_pinned_and_the_pin_refuses_the_render(monkeypatch):
    """Flag off is today's behaviour, observed: the contract pins every label, nothing draws them,
    and the device pin refuses the whole render ("missing confirmed on-screen text") -- the
    reason the contract leaves them out when the lane is on."""
    job, contract = _contract_run(monkeypatch, flag=False)
    pinned = [t.text for t in contract.exact_texts]
    assert set(pinned) >= {*LABELS, GHOST}
    # Refused, never awaiting_device.
    assert job.status == "queued", (job.status, job.error_detail)
    from app.services.device_render import device_record

    with pytest.raises(KeyError):  # never pinned: the job has no device recipe
        device_record(job, "subtitled")


# ------------------------------------------------------------------ review round


def test_f5_without_item_17_no_label_follows_a_recording(monkeypatch):
    """Item 17 off: lay_out anchors "agents" inside "trading agents", so the labels ground on
    their own words instead (nothing is `background`)."""
    _job, variant = _run(monkeypatch, flag=True, follows_talk=False)
    rows = variant["list_label_placement"]["rows"]
    assert rows and all(r.get("basis") != "background" for r in rows)


def test_f5_with_item_17_the_labels_follow_the_recordings(monkeypatch):
    _job, variant = _run(monkeypatch, flag=True)
    bases = {r["requirement_id"]: r.get("basis") for r in variant["list_label_placement"]["rows"]}
    assert all(bases[f"l{i}"] == "background" for i in range(1, 6))


def _lanes(*media_ids):
    from types import SimpleNamespace

    windows = [SimpleNamespace(media_id=m) for m in media_ids]
    return SimpleNamespace(background=SimpleNamespace(windows=windows))


def _layout():
    from app.pipeline.phone_list_labels import LabelAnchor

    return {
        "anchors": [LabelAnchor(0, "a", 1.0, 1.0), LabelAnchor(1, "b", 5.0, 5.0)],
        "phrases": [(0, "a"), (1, "b"), (2, "c")],
        "entry_media": {0: "m0", 1: "m1", 2: "m2"},
    }


def test_f6_an_anchor_whose_windows_were_dropped_is_not_followed():
    anchors, phrases = gb._phone_list_label_follow(_layout(), _lanes("m0"), follows_talk=True)
    assert [a.index for a in anchors] == [0]
    assert phrases == [(0, "a"), (1, "b"), (2, "c")]  # every phrased entry still pairs


@pytest.mark.parametrize("lanes", [None, _lanes()])
def test_f6_no_lanes_or_no_windows_leave_no_anchor(lanes):
    anchors, _ = gb._phone_list_label_follow(_layout(), lanes, follows_talk=True)
    assert anchors == []


def test_f6_item_17_off_passes_neither():
    assert gb._phone_list_label_follow(_layout(), _lanes("m0", "m1"), follows_talk=False) == (
        [],
        None,
    )


LONG_LABEL_1 = (
    "1. Spec-kit by GitHub: you write the spec, it writes the plan, then it turns that plan "
    "into tasks"
)


def test_f9_a_long_label_is_drawn_whole_and_its_receipt_reads_the_drawn_row(monkeypatch):
    """Review of #2116: a requested label past 80 characters was cut mid-word on the video while
    the receipt kept the whole text. It is drawn whole (wrapped and shrunk to the label band),
    the phone recipe carries every word, and the finished-video receipt reads the drawn row."""
    long_req = _req("l1", LONG_LABEL_1)
    assert len(LONG_LABEL_1) > 80
    job, variant = _run(monkeypatch, flag=True, brief=_brief(labels=[long_req, *LABEL_REQS[1:]]))
    [row] = [r for r in _label_rows(variant) if r["id"] == "list-label-l1"]
    assert row["text"] == LONG_LABEL_1
    box = _measure_title_box(row, canvas=_STORY_CANVAS)
    assert box.right - box.left <= 0.9 + 1e-6 and box.bottom - box.top <= 0.14 + 1e-6
    drawn = [" ".join(run.text for run in layer.runs) for layer in _recipe(job).text_layers]
    assert LONG_LABEL_1 in drawn
    assert _after(variant, long_req).status == "met"
    # A label row that does not read her words is never "met", whatever else is on screen.
    cut = copy.deepcopy(variant)
    for element in cut["text_elements"]:
        if element["id"] == "list-label-l1":
            element["text"] = LONG_LABEL_1[:80]
    cut["text_elements"].append({"id": "other", "text": LONG_LABEL_1})
    receipt = _after(cut, long_req)
    assert receipt.status == "partial" and "not your exact words" in (receipt.reason or "")


def test_f6_list_labels_join_the_protected_text_placements():
    receipt = {
        "rows": [
            {"requirement_id": "l1", "status": "placed", "placement": {"status": "preset"}},
            {
                "requirement_id": "l1",
                "list_item": 2,
                "status": "placed",
                "placement": {"status": "moved"},
            },
            {"requirement_id": "l2", "status": "not_heard"},
        ]
    }
    assert gb._list_label_protected_placements(receipt) == {
        "list:l1-1": {"status": "preset"},
        "list:l1-2": {"status": "moved"},
    }
    assert gb._list_label_protected_placements(None) == {}
