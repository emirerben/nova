"""``filler_words`` suggestion kind: replay eval over real engine output (no network, no skia).

Ways this kind could fail, written before the code:

- no speech in the footage: the preflight finds nothing (``no_findings``) or never ran, so there
  is nothing to count and no card;
- the preflight is queued, running or failed: no evidence, so never a guess from transcripts;
- the preflight row is stale (a deploy or a source change re-stamped the policy fingerprint);
- the plan holds retake cuts: enabling cleanup would also cut restarted sentences, which needs a
  consent the approval card owns, so the card stays away;
- cleanup is already on, or the creator already chose to keep the original speech (KRI-129: a
  card never argues with what they decided);
- Turkish fillers (ııı, eee, Iıı) are counted exactly like English ones, and a word that merely
  looks like a filler (Turkish ``o``/``ee``/``aa``/``şey``, English "like") is never counted;
- a word that is both a filler and content: whisper puts an "uh" over the start of a real word
  (the provolone takes). The engine drops it, so the card must too: the count comes from the
  engine's surviving findings, never from a second token scan;
- one stray "um": below the threshold, no card;
- ``SILENCE_CUT_ENABLED`` off, rollout disabled, a format the renderer cannot cut, a replacement
  voiceover, no committed clip;
- a multi-clip phone project (an analysis-proxy source with other than one clip): the phone has no
  way to cut it, and tapping would end in ``speech_cleanup_unavailable_on_phone``;
- the tap: the setting must flip exactly like the plan-item PATCH (same refusals, same fields),
  never twice, never for a card whose evidence has since gone, and a refusal must write nothing;
- the downstream render: the normal dispatch must read the flipped setting as ``required_v1``.

What this does NOT prove: that the seam's ``_complete_draft_turn`` routes a pinned ``item_setting``
card to ``apply_item_setting`` (the seam answers "no longer fits" for any non-editor action until
it does) or that a phone then renders the cut. The executor's contract and the dispatch contract
it feeds are proven here; the integration is listed in the lane's open issues.

Artifact: ``filler-kind-report.json`` in the pytest tmp dir (also written to ``$FILLER_KIND_OUT``
when set): the per-take table the threshold was chosen from.
"""

from __future__ import annotations

import itertools
import json
import os
import types
import uuid
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import PlanItem
from app.pipeline.silence_cut import is_filler_token
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    analyze_speech_cleanup,
)
from app.services.speech_cleanup import contract_for_item
from app.services.suggestions import registry
from app.services.suggestions.context import SuggestionContext
from app.services.suggestions.kinds import filler_words as kind_mod
from app.services.suggestions.kinds.filler_words import (
    KIND,
    MIN_FILLERS,
    MIN_SAVED_MS,
    FillerEvidence,
    apply_item_setting,
    evidence_for_item,
    evidence_lines,
    unoffered_reason,
)
from app.services.suggestions.models import EditorOpsAction, ItemSettingAction

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OWN = FIXTURES / "suggestion_kinds" / "filler_words"
FINGERPRINT = "policy-fp-1"
PROXY_PATH = "uploads/u1/analysis-proxy-clip.mp4"
CLOUD_PATH = "uploads/u1/clip.mp4"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# ── real engine output ───────────────────────────────────────────────────────────────


def run_engine(words: list[dict], duration_s: float, language: str = "en"):
    """The REAL analysis engine over a recorded transcript. Only the audio-bound calls (speech to
    text, silencedetect, the voice guard's PCM decode) are replaced."""
    transcript = types.SimpleNamespace(
        words=[
            types.SimpleNamespace(
                text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=0.99
            )
            for w in words
        ],
        language=language,
        low_confidence=False,
    )
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="replay",
            local_media_path="/replay/take.wav",
            duration_s=duration_s,
            mixed_gap_mode="off",
        ),
        transcribe_fn=lambda *_a, **_k: transcript,
        silence_detect_fn=lambda *_a, **_k: types.SimpleNamespace(status="ok", spans=[]),
        pcm_loader=lambda _path: np.zeros(16000 * (int(duration_s) + 1), dtype=np.float32),
    )


def row_from(receipt, **over):
    """A ``SpeechCleanupAnalysis`` as ``finalize_analysis_success`` stores this receipt."""
    row = dict(
        source_policy_fingerprint=FINGERPRINT,
        status="ready" if receipt.candidate_count > 0 else "no_findings",
        decision=None,
        category_counts=receipt.category_counts.model_dump(mode="json"),
        estimated_removed_ms=receipt.estimated_removed_ms,
        source_storage_path=CLOUD_PATH,
    )
    row.update(over)
    return types.SimpleNamespace(**row)


def item_of(edit_format="subtitled", *, clips=1, voiceover=False, enabled=False, **over):
    paths = [f"gs://b/{i}.mp4" for i in range(clips)]
    fields = dict(
        id=uuid.uuid4(),
        edit_format=edit_format,
        clip_gcs_paths=paths,
        clip_assignments=[{"gcs_path": p, "media_id": f"m{i}"} for i, p in enumerate(paths)],
        audio_mode="voiceover" if voiceover else None,
        voiceover_gcs_path="gs://b/vo.m4a" if voiceover else None,
        speech_cleanup_enabled=enabled,
        user_edited=False,
    )
    fields.update(over)
    return PlanItem(**fields)


@pytest.fixture(autouse=True)
def cleanup_switched_on(monkeypatch):
    for name, value in {
        "silence_cut_enabled": True,
        "subtitled_archetype_enabled": True,
        "edit_format_talking_head_enabled": True,
        "narrated_self_narration_enabled": True,
        "speech_cleanup_mode": "opt_in",
        "speech_cleanup_preflight_mode": "off",
        "kria_suggestion_filler_words_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)


CAFE = _load(FIXTURES / "kri970_cafe_late_stamps.json")
PROVOLONE = _load(FIXTURES / "kri970_provolone_takes.json")["takes"]
TURKISH = _load(OWN / "turkish_take.json")
LOOKALIKES = _load(OWN / "lookalike_words_take.json")
ONE_UM = _load(OWN / "single_um_take.json")

TAKES = {
    "cafe_real": (CAFE["words"], CAFE["duration_s"], "en"),
    "provolone_t1_real": (PROVOLONE["T1"]["words"], PROVOLONE["T1"]["duration_s"], "en"),
    "provolone_t3_real": (PROVOLONE["T3"]["words"], PROVOLONE["T3"]["duration_s"], "en"),
    "turkish_synthetic": (TURKISH["words"], TURKISH["duration_s"], "tr"),
    "lookalikes_synthetic": (LOOKALIKES["words"], LOOKALIKES["duration_s"], "en"),
    "single_um_synthetic": (ONE_UM["words"], ONE_UM["duration_s"], "en"),
}


def evidence_of(take: str, item=None, **row_over) -> FillerEvidence:
    words, duration, language = TAKES[take]
    receipt = run_engine(words, duration, language).public_receipt
    return evidence_for_item(
        item or item_of(), row_from(receipt, **row_over), current_fingerprint=FINGERPRINT
    )


def ctx_of(evidence: FillerEvidence | None) -> SuggestionContext:
    inputs = {} if evidence is None else {kind_mod.INPUTS_KEY: evidence}
    return SuggestionContext(state="ready", kind_inputs=inputs)


def offered(evidence: FillerEvidence | None) -> bool:
    ctx = ctx_of(evidence)
    return KIND.availability(ctx) and not KIND.already_present(ctx)


# ── the threshold, from data ─────────────────────────────────────────────────────────


def test_threshold_table_from_real_engine_output(tmp_path):
    rows = {}
    for name, (words, duration, language) in TAKES.items():
        receipt = run_engine(words, duration, language).public_receipt
        evidence = evidence_for_item(item_of(), row_from(receipt), current_fingerprint=FINGERPRINT)
        rows[name] = {
            "duration_s": duration,
            "tokens_matching_lexicon": sum(is_filler_token(w["text"]) for w in words),
            "engine_filler_sounds": receipt.category_counts.filler_sounds,
            "engine_removed_ms": receipt.estimated_removed_ms,
            "offered": offered(evidence),
            "reason": unoffered_reason(evidence),
        }
    report = {"min_fillers": MIN_FILLERS, "min_saved_ms": MIN_SAVED_MS, "takes": rows}
    text = json.dumps(report, indent=2, ensure_ascii=False)
    (tmp_path / "filler-kind-report.json").write_text(text)
    if out := os.environ.get("FILLER_KIND_OUT"):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "filler-kind-report.json").write_text(text)

    # The real cafe take: four fillers, a pattern a creator can hear.
    assert rows["cafe_real"]["engine_filler_sounds"] == 4
    assert rows["cafe_real"]["offered"]
    # Turkish elongations are counted by the same engine; real Turkish words are not.
    assert rows["turkish_synthetic"]["engine_filler_sounds"] == TURKISH["expect"]["filler_sounds"]
    assert rows["turkish_synthetic"]["offered"]
    # A word that is both filler and content: whisper's "uh," over "provo-" is in the transcript
    # (the lexicon sees it), the engine drops it, and so does the card.
    for take in ("provolone_t1_real", "provolone_t3_real"):
        assert rows[take]["tokens_matching_lexicon"] == 1
        assert rows[take]["engine_filler_sounds"] == 0
        assert rows[take]["reason"] == "analysis_no_findings"
    assert rows["lookalikes_synthetic"]["engine_filler_sounds"] == 0
    assert rows["lookalikes_synthetic"]["tokens_matching_lexicon"] == 0
    # One stray "um" gets no card, however the threshold is tuned.
    assert rows["single_um_synthetic"]["engine_filler_sounds"] == 1
    assert not rows["single_um_synthetic"]["offered"]
    # The saved-seconds floor is consistent with the filler floor on real data: the cafe take
    # saves MIN_FILLERS' worth of its per-filler average and still clears it.
    per_filler_ms = (
        rows["cafe_real"]["engine_removed_ms"] / rows["cafe_real"]["engine_filler_sounds"]
    )
    assert per_filler_ms * MIN_FILLERS >= MIN_SAVED_MS


# ── when the card must stay away ─────────────────────────────────────────────────────


def _base() -> FillerEvidence:
    return evidence_of("cafe_real")


# Built per test: the capability inside the evidence reads the live settings.
HIDDEN = {
    "no evidence loaded": (lambda: None, "no_evidence"),
    "preflight never ran": (lambda: replace(_base(), status="none"), "analysis_none"),
    "preflight queued": (lambda: replace(_base(), status="queued"), "analysis_queued"),
    "preflight running": (lambda: replace(_base(), status="running"), "analysis_running"),
    "preflight failed": (lambda: replace(_base(), status="failed"), "analysis_failed"),
    "no speech found": (lambda: replace(_base(), status="no_findings"), "analysis_no_findings"),
    "retake cuts in the plan": (lambda: replace(_base(), retake_count=2), "retakes_need_consent"),
    "cleanup already on": (lambda: replace(_base(), enabled=True), "already_enabled"),
    "creator kept the original": (
        lambda: replace(_base(), decision="keep_original"),
        "creator_kept_original",
    ),
    "creator already chose clean": (
        lambda: replace(_base(), decision="clean"),
        "already_decided_clean",
    ),
    "multi-clip phone source": (
        lambda: replace(_base(), phone_cannot_cut=True),
        "phone_cannot_cut",
    ),
    "two fillers": (lambda: replace(_base(), filler_count=MIN_FILLERS - 1), "too_few_fillers"),
    "fillers save too little": (
        lambda: replace(_base(), removed_ms=MIN_SAVED_MS - 1),
        "saves_too_little",
    ),
}


@pytest.mark.parametrize("label", list(HIDDEN))
def test_hidden_cases_never_reach_a_card(label):
    make, reason = HIDDEN[label]
    evidence = make()
    ctx = ctx_of(evidence)
    assert unoffered_reason(evidence) == reason
    assert not offered(evidence)
    assert KIND.build_action(ctx, [], None) is None
    good = ItemSettingAction(field="speech_cleanup_enabled", value=True)
    proven = KIND.witness(good, ctx)
    assert not proven.ok and proven.reason == reason


def test_stale_row_counts_for_nothing():
    words, duration, language = TAKES["cafe_real"]
    receipt = run_engine(words, duration, language).public_receipt
    for current in ("another-fingerprint", None):
        stale = evidence_for_item(item_of(), row_from(receipt), current_fingerprint=current)
        assert stale.status == "stale" and stale.filler_count == 0
        assert not offered(stale)


def test_phone_proxy_source_needs_exactly_one_clip():
    words, duration, language = TAKES["cafe_real"]
    receipt = run_engine(words, duration, language).public_receipt
    proxy = row_from(receipt, source_storage_path=PROXY_PATH)
    for clips, hidden in ((1, False), (2, True), (3, True)):
        ev = evidence_for_item(
            item_of("talking_head", clips=clips), proxy, current_fingerprint=FINGERPRINT
        )
        assert ev.phone_cannot_cut is hidden
        assert offered(ev) is (not hidden)
    # The same two clips from a cloud source are cuttable.
    cloud = evidence_for_item(
        item_of("talking_head", clips=2), row_from(receipt), current_fingerprint=FINGERPRINT
    )
    assert offered(cloud)


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (item_of("montage"), "unavailable:unsupported_format"),
        (item_of("subtitled", voiceover=True), "unavailable:replacement_voiceover"),
        (item_of("subtitled", clips=0), "unavailable:no_committed_clip"),
    ],
)
def test_format_the_renderer_cannot_cut_is_hidden(item, reason):
    ev = evidence_of("cafe_real", item=item)
    assert unoffered_reason(ev) == reason
    assert not offered(ev)


def test_switches_hide_the_kind(monkeypatch):
    ev = evidence_of("cafe_real")
    assert offered(ev)
    monkeypatch.setattr(settings, "silence_cut_enabled", False)
    assert unoffered_reason(ev) == "engine_disabled"
    assert not offered(ev)
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    # Rollout disabled is a capability answer, taken when the evidence is loaded.
    monkeypatch.setattr(settings, "speech_cleanup_mode", "disabled")
    assert unoffered_reason(evidence_of("cafe_real")) == "unavailable:rollout_disabled"


def test_kind_flag_gates_the_registry(monkeypatch):
    names = lambda: {k.kind for k in registry.kinds_for("ready", "phone")}  # noqa: E731
    assert "filler_words" in names()
    monkeypatch.setattr(settings, "kria_suggestion_filler_words_enabled", False)
    assert "filler_words" not in names()
    assert not any(k.kind == "filler_words" for k in registry.kinds_for("first_prompt", "phone"))


# ── the card ─────────────────────────────────────────────────────────────────────────


def test_a_measured_card_builds_and_proves():
    ev = evidence_of("cafe_real")
    ctx = ctx_of(ev)
    assert offered(ev)
    action = KIND.build_action(ctx, [], None)
    assert action == ItemSettingAction(field="speech_cleanup_enabled", value=True)
    proven = KIND.witness(action, ctx)
    assert proven.ok and proven.tier == "setting" and proven.touched
    assert registry.problems([KIND]) == []
    assert KIND.states == frozenset({"ready"}) and KIND.surfaces == frozenset({"phone"})


def test_witness_refuses_any_other_action():
    ctx = ctx_of(evidence_of("cafe_real"))
    other = EditorOpsAction(ops=[{"op": "set_mix"}], base_digest="d")
    assert KIND.witness(other, ctx).reason == "not_cleanup_setting"
    off = ItemSettingAction(field="speech_cleanup_enabled", value=False)
    assert KIND.witness(off, ctx).reason == "not_turning_on"


def test_evidence_lines_are_measured_and_closed():
    ev = evidence_of("cafe_real")
    assert evidence_lines(ev, "en") == ["4 filler sounds", "Saves 1.8 s"]
    assert evidence_lines(ev, "tr") == ["4 dolgu sesi", "1,8 sn kısalır"]
    for line in evidence_lines(ev, "en") + evidence_lines(ev, "tr"):
        assert len(line) <= 48
        # No transcript word can be in a line built only from two numbers.
        assert not any(
            w["text"].strip(".,").casefold() in line.casefold()
            for w in CAFE["words"]
            if len(w["text"]) > 3
        )


# ── the tap's executor ───────────────────────────────────────────────────────────────

TAP = ItemSettingAction(field="speech_cleanup_enabled", value=True)


@pytest.fixture
def served(monkeypatch):
    """A tap's world: the item, the row the card was served from, and the preflight scheduler."""
    words, duration, language = TAKES["cafe_real"]
    holder = types.SimpleNamespace(
        row=row_from(run_engine(words, duration, language).public_receipt), scheduled=[]
    )
    from app.services import speech_cleanup_preflight as preflight

    monkeypatch.setattr(preflight, "current_analysis_sync", lambda _db, _id: holder.row)
    monkeypatch.setattr(kind_mod, "_fingerprint", lambda _item: FINGERPRINT)

    def schedule(_db, item):
        holder.scheduled.append(bool(item.speech_cleanup_enabled))
        return holder.row.analysis_id if hasattr(holder.row, "analysis_id") else None

    monkeypatch.setattr(preflight, "schedule_item_preflight_sync", schedule)
    return holder


def test_tap_flips_the_setting_and_the_normal_dispatch_reads_it(served):
    item = item_of("subtitled")
    before = contract_for_item(item, mode="opt_in", engine_enabled=True, renderer_enabled=True)
    assert before == "off_v1"

    outcome = apply_item_setting(object(), item, TAP)

    assert outcome.applied and outcome.reason is None
    assert item.speech_cleanup_enabled is True and item.user_edited is True
    # Scheduled after the flip, like the PATCH, so a new source policy is analysed.
    assert served.scheduled == [True]
    # The normal approval/render flow stamps the cleanup contract from the setting.
    after = contract_for_item(item, mode="opt_in", engine_enabled=True, renderer_enabled=True)
    assert after == "required_v1"
    # A card served earlier and tapped again cannot flip it twice.
    again = apply_item_setting(object(), item, TAP)
    assert not again.applied and again.reason == "already_enabled"
    assert KIND.already_present(
        ctx_of(evidence_for_item(item, served.row, current_fingerprint=FINGERPRINT))
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda h: setattr(h, "row", None),
        lambda h: setattr(h.row, "status", "running"),
        lambda h: setattr(h.row, "source_policy_fingerprint", "replaced-source"),
        lambda h: setattr(h.row, "decision", "keep_original"),
        lambda h: setattr(h.row, "category_counts", {"filler_sounds": 1}),
        lambda h: setattr(h.row, "category_counts", {"filler_sounds": 5, "retakes": 1}),
    ],
)
def test_a_card_whose_evidence_has_gone_writes_nothing(served, change):
    change(served)
    item = item_of("subtitled")
    outcome = apply_item_setting(object(), item, TAP)
    assert not outcome.applied and outcome.reason
    assert item.speech_cleanup_enabled is False and item.user_edited is False
    assert served.scheduled == []


def test_a_tap_the_real_scheduler_undoes_is_not_reported_as_applied(monkeypatch):
    """The real scheduler re-opens a row Gemini contradicts and clears the setting."""
    from app.pipeline import caption_language
    from app.services import plan_item_media as media
    from app.services import speech_cleanup_preflight as preflight

    words, duration, language = TAKES["cafe_real"]
    row = row_from(
        run_engine(words, duration, language).public_receipt,
        id=uuid.uuid4(),
        analysis_payload={"language": "en"},
        engine_version=preflight.SPEECH_CLEANUP_ENGINE_VERSION,
    )
    source = types.SimpleNamespace(
        source_policy_fingerprint=FINGERPRINT,
        source_kind="clip",
        media_id="m0",
        storage_path=CLOUD_PATH,
        generation="g1",
        window_start_s=0.0,
        window_end_s=10.0,
    )
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    monkeypatch.setattr(preflight, "current_analysis_sync", lambda _db, _id, for_update=False: row)
    monkeypatch.setattr(kind_mod, "_fingerprint", lambda _item: FINGERPRINT)
    monkeypatch.setattr(
        media, "resolve_item_narration", lambda *_a, **_k: types.SimpleNamespace(source=source)
    )
    monkeypatch.setattr(media, "current_detector_policy", lambda: "p")
    monkeypatch.setattr(preflight, "preflight_enabled_for_source", lambda *_a, **_k: True)
    monkeypatch.setattr(preflight, "reference_transcript_for_source", lambda *_a, **_k: "merhaba")
    monkeypatch.setattr(caption_language, "crosscheck_detected_language", lambda *_a, **_k: "tr")

    item = item_of("subtitled")
    outcome = apply_item_setting(types.SimpleNamespace(flush=lambda: None), item, TAP)

    assert not outcome.applied and outcome.reason == "analysis_redone"
    assert item.speech_cleanup_enabled is False and item.user_edited is False
    assert outcome.publish_analysis_id == row.id and row.status == "queued"


def test_tap_refuses_other_settings_and_a_pulled_kind(served, monkeypatch):
    item = item_of("subtitled")
    off = ItemSettingAction(field="speech_cleanup_enabled", value=False)
    assert apply_item_setting(object(), item, off).reason == "unsupported_setting"
    monkeypatch.setattr(settings, "kria_suggestion_filler_words_enabled", False)
    assert apply_item_setting(object(), item, TAP).reason == "kind_disabled"
    assert item.speech_cleanup_enabled is False


# ── same gate as PATCH /plan-items/{id} ──────────────────────────────────────────────


class _Db:
    async def commit(self):
        return None


async def _patch(monkeypatch, item):
    """Run the REAL plan-item PATCH route for ``item``; its answer is 'applied' or its 409 code."""
    from app.routes import plan_items
    from app.services import smart_captions
    from app.services import speech_cleanup_preflight as preflight

    async def owned(*_a, **_k):
        return item

    async def none(*_a, **_k):
        return None

    async def level(*_a, **_k):
        return "standard"

    async def capability(*_a, **_k):
        return types.SimpleNamespace(available=True, reason=None)

    monkeypatch.setattr(plan_items, "_load_owned_item", owned)
    monkeypatch.setattr(plan_items, "_get_instruction_level", level)
    monkeypatch.setattr(plan_items, "_get_content_mode", level)
    monkeypatch.setattr(plan_items, "plan_item_response", lambda *_a, **_k: "ok")
    monkeypatch.setattr(preflight, "mutation_current_analysis_async", none)
    monkeypatch.setattr(preflight, "schedule_item_preflight_async", none)
    monkeypatch.setattr(smart_captions, "resolve_smart_captions_capability", capability)
    try:
        await plan_items.edit_plan_item(
            "item",
            plan_items.PlanItemEdit(speech_cleanup_enabled=True),
            types.SimpleNamespace(id=uuid.uuid4()),
            _Db(),
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        return str(exc.detail).removeprefix("speech_cleanup_unavailable:")
    return "applied"


SHAPES = list(
    itertools.product(
        ("subtitled", "talking_head", "narrated", "narrated_ready", "montage"),
        (0, 1, 2),
        (False, True),  # replacement voiceover
        (False, True),  # silence cut engine
        (False, True),  # that format's renderer flag
        ("opt_in", "disabled"),
    )
)


@pytest.mark.parametrize(("fmt", "clips", "voiceover", "engine", "renderer", "mode"), SHAPES)
async def test_capability_matches_the_patch_route(
    monkeypatch, fmt, clips, voiceover, engine, renderer, mode
):
    monkeypatch.setattr(settings, "silence_cut_enabled", engine)
    monkeypatch.setattr(settings, "speech_cleanup_mode", mode)
    for flag in (
        "subtitled_archetype_enabled",
        "edit_format_talking_head_enabled",
        "narrated_self_narration_enabled",
    ):
        monkeypatch.setattr(settings, flag, renderer)

    route_item = item_of(fmt, clips=clips, voiceover=voiceover)
    answer = await _patch(monkeypatch, route_item)

    mine = evidence_for_item(
        item_of(fmt, clips=clips, voiceover=voiceover), None, current_fingerprint=None
    )
    assert (answer == "applied") is mine.capability
    if answer != "applied":
        assert answer == mine.capability_reason
    else:
        # The route's other write is the one the executor makes.
        assert route_item.speech_cleanup_enabled is True and route_item.user_edited is True
