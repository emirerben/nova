"""KRI-707: a phone Talking edit shows the creator's text when they say its word.

Boat kit T1 (prod thread D2E99325, plan item fc838cf9, job 8e35335a, 2026-10-16):
"“Atakan” dediğimde ekrana büyük “ATAKAN” yazısı gelsin" on a phone Talking edit. The brief
held it as a text requirement with the literal "ATAKAN" and the spoken trigger "Atakan"
(``facts.triggers``), but nothing drew a text on a spoken word: the first receipt said "Bu tam
yazı bu taslakta yok", "simpler version" dropped the ask, and KRI-705 later turned it into a
plain "can't" -- while the approved contract still pinned "ATAKAN", which the phone pin check
refuses on a recipe without it.

The worker now places it itself, with no model: brief requirement (literal + trigger) -> the
take's own word timings -> one editable text row from the word's start, kept off the speaker's
face and the captions, on the text lane the phone already draws (KRI-467).

Ways this can go wrong (written before the code):

1. The text is timed on the source clock, so after the opening cut it lands seconds late.
2. Turkish suffixes hide the word: "Atakan'ı" is never matched to the trigger "Atakan".
3. The word is read from the caption cues, which caption correction can strip of word timings
   (T1's "Şimdi Atakan'ı alacağız değil mi?" cue had none), so nothing is placed.
4. The big text sits on the speaker's face (a close selfie: the face fills the upper frame)
   or on the caption shown at that moment.
5. The receipt still says "can't" / "isn't in this draft" when the text is on screen, or
   "met" when the word was never said.
6. The draft still holds behind "a simpler version?", or "simpler version" removes the ask.
7. A Turkish chat gets an English reason.
8. The kill switch off changes the job (row, receipt or recipe).
9. The row is a second title: it must be an ordinary text row the editor lists and saves.
10. The device pin refuses the render: the approved contract pins "ATAKAN" (on main the T1
    re-send was refused as "missing confirmed on-screen text"), even when the take never says
    the word.

Replayed from the real T1 brief, transcript and face band
(``~/Downloads/kria-boat-test/T1_deniz_neredesin_talking/runs/2026-10-16_D2E99325/``),
no model calls.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

import app.pipeline.phone_subtitled_title as title_mod
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.transcribe import Word
from app.services.clip_speech import SilenceDetectionResult
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.services.device_render import device_record, device_status
from app.services.length_policy_state import (
    LENGTH_POLICIES_FIELD,
    LENGTH_POLICY_FIELD,
    stamp_length_policy,
)
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

DURATION = 28.0
GENERATION = "generation"  # `_setup_subtitled`'s `creator_generation_id`

# The take's own words (job 8e35335a `overlay_transcript`), source seconds.
T1_WORDS = (
    ("Deniz'im,", 4.30, 5.74),
    ("neredesin?", 6.32, 7.34),
    ("Efendim?", 8.52, 8.94),
    ("Neredesin?", 9.22, 9.70),
    ("Kim,", 13.06, 13.78),
    ("Atakan'ı", 13.78, 14.24),
    ("alacağız", 14.24, 14.36),
    ("değil", 14.36, 14.54),
    ("mi", 14.54, 14.54),
    ("şimdi?", 14.54, 14.76),
    ("Geliyor.", 16.30, 16.86),
    ("Mesaj", 18.38, 18.38),
    ("açtın", 18.38, 18.78),
    ("mı", 18.78, 18.82),
    ("hiç?", 18.82, 19.10),
    ("Evet.", 19.46, 19.80),
    ("Az", 20.48, 20.62),
    ("mı", 20.62, 20.74),
    ("kalmış?", 20.74, 21.10),
    ("Evet", 21.36, 21.60),
    ("evet.", 21.60, 21.80),
    ("Araba", 21.98, 22.54),
    ("sararet", 22.54, 22.86),
    ("yapmıştı", 22.86, 23.36),
    ("ama", 23.36, 23.52),
    ("geliyormuş.", 23.52, 23.88),
)
ATAKAN_SOURCE_START_S = 13.78
# The speaker's face on the canvas (job 8e35335a `opening_title_placement.face_band`): a close
# selfie, so the face covers the whole upper frame.
T1_FACE = NormalizedBox(0.17796, 0.06542, 0.92019, 0.64667)

_TURN = "0f1a2dad-48bf-48bf-bbc8-ea9706ef60e9"


def _req(rid: str, kind: str, description: str, **extra) -> BriefRequirement:
    return BriefRequirement(
        id=rid, kind=kind, scope=extra.pop("scope", "global"), description=description, **extra
    )


R4 = _req(
    "r4",
    "text",
    "Ekrana büyük yazı gelsin, yüzü kapatmasın",
    literal="ATAKAN",
    facts={"triggers": ["Atakan"]},
)
# Turn 1's brief (v1) as the brief extractor wrote it, r4 still open.
T1_REQUIREMENTS = [
    _req(
        "r1",
        "timing",
        "Baştaki sessiz kısmı kes, 'Deniz'im' dendiği anda başlasın",
        facts={"triggers": ["Deniz'im"]},
    ),
    _req("r2", "timing", "İkinci 'Neredesin' sorusunu kes, bir kere sorulsun"),
    _req(
        "r3",
        "text",
        "Deniz'in arkadan verdiği cevaplar da yazılsın, yazılar yüzü kapatmasın",
        facts={"captions": "word_by_word", "caption_language": "tr"},
    ),
    R4,
    _req("r5", "audio", "Motor sesini biraz kıs, sesim öne çıksın"),
    _req(
        "r6",
        "text",
        "Başlık eklensin, yazılar yüzü kapatmasın",
        scope="title",
        literal="Gece 11, tekneyle Tigre'ye",
    ),
    # Prod's r7 was "20 saniyenin altında olsun" (duration_s 20). Since KRI-629 that is an
    # exclusive cap on the whole export, ending included, and this take runs ~25 s after the
    # opening cut, so the device pin refuses it unless the creator keeps the take's length.
    # Approval now asks that first (length vs. take: 28 s + the 1.6 s ending); this replay is
    # the creator choosing "Mevcut 29.6 saniyeyi kullan", which rewrites r7 as below
    # (`answered_brief`), so the render still reaches the phone as the prod T1 one did.
    _req(
        "r7",
        "timing",
        "29.6 saniye olsun (20 saniye yerine, eldeki çekimlerin uzunluğu)",
        facts={"duration_s": 29.6},
    ),
]
# The approved strategy (job 8e35335a `creator_strategy`), trimmed to what is read.
T1_STRATEGY = {
    "edit_format": "subtitled",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "caption_style": "karaoke",
    "opening_title": "Gece 11, tekneyle Tigre'ye",
    "pacing": "balanced",
    "direction": "native",
    "target_duration_s": 19,
    "selected_media_ids": ["c0"],
}


def _t1_brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements or T1_REQUIREMENTS))


def _words() -> list[Word]:
    return [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in T1_WORDS]


def _preflight_snapshot(storage_path: str) -> dict:
    """The checked preflight analysis of the take (its words ground the opening cut)."""
    fingerprint = "a" * 64
    analysis = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=fingerprint,
            local_media_path="preflight.wav",
            duration_s=DURATION,
            source_window_start_s=0.0,
            source_window_end_s=DURATION,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
        ),
        transcribe_fn=lambda *_a, **_k: SimpleNamespace(
            words=_words(), language="tr", low_confidence=False
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
    )
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": "c0",
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": DURATION,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def _faces(monkeypatch, box: NormalizedBox = T1_FACE) -> list:
    calls: list = []

    def sample(path, anchors, **kwargs):
        calls.append((path, list(anchors)))
        return [ProtectedRegion(0.0, DURATION, box, "face")], {
            "attempted": len(anchors),
            "decoded": len(anchors),
        }

    monkeypatch.setattr(title_mod, "sample_face_regions", sample)
    return calls


def _t1_job(monkeypatch, *, brief: CreativeBrief | None = None, lanes: bool = True):
    """The T1 take approved on main: cleanup off (`off_v1`), the checked preflight words on
    the job, karaoke captions, the hook title, the brief pinned and its contract stamped."""
    monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", lanes)
    job, snapshot, _session, binding = _setup_subtitled(
        monkeypatch, duration_s=DURATION, transcript_words=_words()
    )
    brief = brief or _t1_brief()
    job.all_candidates.update(
        {
            "language": "tr",
            "voiceover_caption_style": "word",
            "creator_strategy": copy.deepcopy(T1_STRATEGY),
            REQUIREMENT_VERSION_FIELD: 1,
        }
    )
    contract = build_render_contract(T1_STRATEGY, generation_id=GENERATION, brief=brief)
    assert contract is not None
    snapshot.update(
        {
            "speech_cleanup_contract": "off_v1",
            "creator_brief_binding": BriefBinding.create(uuid.uuid4(), brief).model_dump(
                mode="json"
            ),
            "_speech_cleanup_internal": {
                "preflight_snapshot": _preflight_snapshot(binding.proxy_path)
            },
            CONTRACT_FIELD: contract.model_dump(mode="json"),
        }
    )
    # As prod stamped it: the KRI-492 length check is logged, not enforced.
    snapshot.update(stamp_length_policy({}, T1_STRATEGY, contract, mode="log_only"))
    for key in (
        "speech_cleanup_contract",
        "creator_brief_binding",
        "_speech_cleanup_internal",
        CONTRACT_FIELD,
        LENGTH_POLICY_FIELD,
        LENGTH_POLICIES_FIELD,
    ):
        job.assembly_plan[key] = copy.deepcopy(snapshot[key])
    return job, snapshot


def _run(job, snapshot) -> dict:
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def _spoken_rows(variant: dict) -> list[dict]:
    return [row for row in variant.get("text_elements") or [] if row["text"] == "ATAKAN"]


def _receipt(variant: dict, req: BriefRequirement = R4):
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


# -- the incident, end to end ----------------------------------------------------------


@pytest.mark.parametrize("lanes", [True, False], ids=["media-lanes", "no-lanes"])
def test_t1_atakan_lands_on_the_word_off_the_face_and_is_met(monkeypatch, lanes):
    face_calls = _faces(monkeypatch)
    job, snapshot = _t1_job(monkeypatch, lanes=lanes)

    variant = _run(job, snapshot)

    cut = variant["opening_cut"]
    assert cut["status"] == "applied"
    start = ATAKAN_SOURCE_START_S - cut["start_s"]
    # 1-3: on the cut timeline, from the start of "Atakan'ı" (read from the words, not cues).
    [row] = _spoken_rows(variant)
    assert row["start_s"] == pytest.approx(start, abs=0.01)
    assert row["end_s"] > row["start_s"] + 1.0
    # 9: an ordinary, editable text row next to the title, never a second title.
    assert row["id"] not in {"opening-title", "closing-title"}
    assert not (row.get("source_params") or {}).get("read_only")
    assert [r["id"] for r in variant["text_elements"]][0] == "opening-title"
    assert variant["text_elements_user_edited"] is True
    # 4: the face fills the upper frame, so the text sits just under it (above the captions).
    placement = variant["spoken_text_placement"]["rows"][0]
    assert placement["status"] == "placed"
    assert placement["requirement_id"] == "r4"
    assert placement["placement"]["coverage"] <= 0.05
    assert placement["face_collision"] is False
    assert row["y_frac"] > T1_FACE.bottom
    assert any(call[0] == "/tmp/c0.mp4" for call in face_calls), "sampled on the take itself"
    # The phone draws it: a text layer on the compiled recipe, at the word.
    recipe = device_status(job, "subtitled").request.recipe
    [layer] = [
        layer
        for layer in recipe.text_layers
        if layer.id.startswith("text-") and "".join(run.text for run in layer.runs) == "ATAKAN"
    ]
    assert layer.start == pytest.approx(start, abs=0.01)
    assert layer.anchor_y / 1920 > T1_FACE.bottom
    # 10: the pin checked this recipe against the approved contract and kept it. The text
    # shows only where the take says the word, so its receipt answers it, not the pin.
    assert device_record(job, "subtitled").get("contract_digest")
    assert [t["text"] for t in job.assembly_plan[CONTRACT_FIELD]["exact_texts"]] == [
        "Gece 11, tekneyle Tigre'ye",
        "Gece 11, tekneyle Tigre'ye",
    ]
    # 5: the finished-video receipt reads it as done.
    assert _receipt(variant).status == "met"


def test_t1_atakan_clears_the_caption_on_screen(monkeypatch):
    _faces(monkeypatch)
    job, snapshot = _t1_job(monkeypatch)

    variant = _run(job, snapshot)

    placement = variant["spoken_text_placement"]["rows"][0]
    assert placement["placement"]["text_overlap"] is False


def test_a_word_never_said_places_nothing_and_says_so(monkeypatch):
    _faces(monkeypatch)
    never = R4.model_copy(update={"literal": "MEHMET", "facts": {"triggers": ["Mehmet"]}})
    job, snapshot = _t1_job(monkeypatch, brief=_t1_brief(*T1_REQUIREMENTS[:3], never))

    variant = _run(job, snapshot)

    assert not [row for row in variant["text_elements"] if row["text"] == "MEHMET"]
    [placement] = variant["spoken_text_placement"]["rows"]
    assert (placement["requirement_id"], placement["status"]) == ("r4", "not_heard")
    receipt = _receipt(variant, never)
    assert receipt.status == "partial"
    assert "Mehmet" in receipt.reason
    token = bind_reply_language("tr")
    try:
        receipt_tr = _receipt(variant, never)
    finally:
        release_reply_language(token)
    assert "Mehmet" in receipt_tr.reason
    assert "hear" not in receipt_tr.reason


def test_kill_switch_off_is_byte_identical(monkeypatch):
    _faces(monkeypatch)
    # The same job without the ask, flag on.
    plain_brief = _t1_brief(*[r for r in T1_REQUIREMENTS if r.id != "r4"])
    job_plain, snapshot_plain = _t1_job(monkeypatch, brief=plain_brief)
    plain = _run(job_plain, snapshot_plain)
    plain_recipe = device_status(job_plain, "subtitled").request.recipe

    monkeypatch.setattr(settings, "phone_subtitled_spoken_text_enabled", False)
    job, snapshot = _t1_job(monkeypatch, brief=plain_brief)
    off = _run(job, snapshot)

    assert "spoken_text_placement" not in off and "spoken_text_placement" not in plain
    assert off["text_elements"] == plain["text_elements"]
    assert device_status(job, "subtitled").request.recipe.text_layers == plain_recipe.text_layers


# -- before approval -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _phone_draws_text(monkeypatch):
    """The phone renders positioned, animated text (the Talking title's own features)."""
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )


def _draft_receipts(*reqs: BriefRequirement, phone_talking: bool = True):
    import dataclasses

    facts = dataclasses.replace(plan_facts_from_strategy(T1_STRATEGY), phone_talking=phone_talking)
    return build_receipts(list(reqs), facts, include_unchecked=True)


def test_the_t1_draft_promises_the_text_and_never_asks_for_a_simpler_version():
    [receipt] = _draft_receipts(R4)

    # 5 + 6: a plain plan the render keeps, not "can't" / "isn't in this draft", and not
    # an omission the creator must rule on (so "simpler version" never removes it).
    assert receipt.status != "not_possible"
    assert receipt.verification == "checked"
    assert "can't" not in (receipt.reason or "")
    assert "isn't in this draft" not in (receipt.reason or "")
    assert not needs_creator_choice(receipt)


def test_the_t1_draft_reason_is_turkish_in_a_turkish_chat():
    token = bind_reply_language("tr")
    try:
        [receipt] = _draft_receipts(R4)
    finally:
        release_reply_language(token)
    # 7
    assert receipt.reason and "word" not in receipt.reason and "kelime" in receipt.reason


def test_without_a_spoken_word_or_off_the_phone_the_answer_is_unchanged():
    # No trigger fact: the server can't know which word, so it still says it can't.
    no_trigger = R4.model_copy(update={"facts": {}, "description": "'Atakan' dediğimde yazı"})
    [receipt] = _draft_receipts(no_trigger)
    assert receipt.status == "not_possible"
    # A cloud-rendered Talking edit has no such lane (KRI-705's plain "can't").
    [cloud] = _draft_receipts(R4, phone_talking=False)
    assert (cloud.status, cloud.reason) == (
        "not_possible",
        "A Talking video can't show text at a word you say yet",
    )


def test_kill_switch_off_keeps_the_plain_cant(monkeypatch):
    monkeypatch.setattr(settings, "phone_subtitled_spoken_text_enabled", False)
    [receipt] = _draft_receipts(R4)
    assert (receipt.status, receipt.reason) == (
        "not_possible",
        "A Talking video can't show text at a word you say yet",
    )


# -- the editor ------------------------------------------------------------------------


def test_the_editor_is_served_atakan_and_a_save_keeps_it_on_the_word(monkeypatch):
    """9: the status route serves the row as ordinary editable text (the iOS editor lists it
    under Text and previews it from the same row), and a text Save recompiles it unchanged."""
    from app.routes import generative_jobs as gj
    from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor

    _faces(monkeypatch)
    job, snapshot = _t1_job(monkeypatch)
    variant = _run(job, snapshot)
    [row] = _spoken_rows(variant)
    _enable_subtitled_editor(monkeypatch)

    (served,) = gj._variants_for_response(job)
    assert served["editor_capabilities"]["text_elements"] is True
    [shown] = [r for r in served["text_elements"] if r["text"] == "ATAKAN"]
    assert (shown["id"], shown["start_s"], shown["y_frac"]) == (
        row["id"],
        row["start_s"],
        row["y_frac"],
    )
    assert not (shown.get("source_params") or {}).get("read_only")
    assert (shown.get("source_params") or {}).get("source") != "caption_cue"

    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(job.assembly_plan["variants"][0]),
            text_elements=copy.deepcopy(served["text_elements"]),
        ),
        user_id="owner",
        plan_item_id="item",
    )

    saved = device_status(job, "subtitled").request
    assert saved.identity.recipe_revision == 2, "the Save recompiled the phone recipe"
    recipe = saved.recipe
    [layer] = [
        layer
        for layer in recipe.text_layers
        if layer.id.startswith("text-") and "".join(run.text for run in layer.runs) == "ATAKAN"
    ]
    assert layer.start == pytest.approx(row["start_s"], abs=0.01)
