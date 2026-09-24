"""KRI-189: the clip fact layer (capture time / place / landmark, with provenance)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.schemas.clip_understanding import FACTS_KEY, ClipFact
from app.services import clip_facts as cf
from app.services import creator_sessions
from app.services.clip_understanding import clip_record

_CAPTURE = {
    "capture_time": "2026-09-20T07:31:02Z",
    "coarse_location": {"lat": 41.19, "lon": 28.74},
    "place": {"sub_locality": "Arnavutköy", "locality": "İstanbul", "country": "Türkiye"},
}


def _t(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 20, hour, minute, tzinfo=UTC)


# ── flag ──────────────────────────────────────────────────────────────────────


def _settings(**kwargs) -> Settings:
    return Settings(storage_bucket="test", **kwargs)


def test_flag_is_off_by_default() -> None:
    assert _settings().clip_facts_for(uuid.uuid4()) is False


def test_flag_on_enables_everyone() -> None:
    assert _settings(clip_facts_enabled=True).clip_facts_for(uuid.uuid4()) is True


def test_allowlist_enables_only_listed_accounts_while_the_flag_is_off() -> None:
    allowed = uuid.uuid4()
    settings = _settings(clip_facts_user_ids=[allowed])
    assert settings.clip_facts_for(allowed) is True
    assert settings.clip_facts_for(str(allowed)) is True
    assert settings.clip_facts_for(uuid.uuid4()) is False
    assert settings.clip_facts_for(None) is False


# ── capture -> facts ─────────────────────────────────────────────────────────


def test_capture_facts_carry_provenance() -> None:
    capture = cf.capture_from_assignment({"capture": _CAPTURE})
    facts = {(f.kind, f.provenance): f.value for f in cf.capture_facts(capture)}
    assert facts == {
        ("capture_time", "exif"): "2026-09-20T07:31:02Z",
        ("place", "geocode"): "Arnavutköy, İstanbul, Türkiye",
    }


def test_capture_read_falls_back_to_the_proxy_receipt() -> None:
    assignment = {
        "upload_contract": {
            "purpose": "analysis_proxy",
            "proxy": {
                "original": {
                    "sha256": "a" * 64,
                    "byte_count": 10,
                    "duration_s": 5,
                    "width": 10,
                    "height": 10,
                    "has_audio": True,
                    "capture": _CAPTURE,
                },
                "duration_s": 5,
                "width": 10,
                "height": 10,
                "frame_rate": 30,
            },
        }
    }
    capture = cf.capture_from_assignment(assignment)
    assert capture is not None and capture.place.locality == "İstanbul"


@pytest.mark.parametrize("bad", [None, {}, "x", {"capture_time": "garbage"}, {"unknown": 1}])
def test_malformed_stored_capture_is_no_capture(bad: object) -> None:
    assert cf.capture_from_assignment({"capture": bad}) is None
    assert cf.assignment_facts({"capture": bad}) == []


def test_assignment_facts_dedupe_stored_copies_of_capture_facts() -> None:
    assignment = cf.with_capture_facts({"capture": _CAPTURE, "generation": "1"})
    assignment = cf.with_landmark_fact(
        assignment,
        ClipFact(kind="landmark", value="Rumeli Hisarı", provenance="inferred", confidence=0.8),
    )
    facts = cf.assignment_facts(assignment)
    assert [(f.kind, f.provenance) for f in facts] == [
        ("capture_time", "exif"),
        ("place", "geocode"),
        ("landmark", "inferred"),
    ]


def test_with_capture_facts_is_idempotent_and_returns_the_same_entry() -> None:
    once = cf.with_capture_facts({"capture": _CAPTURE, "analysis": {"subject": "road"}})
    assert once["analysis"]["subject"] == "road"
    assert cf.with_capture_facts(once) is once


def test_with_capture_facts_without_capture_returns_the_entry_untouched() -> None:
    entry = {"media_id": "a", "analysis": {"subject": "road"}}
    assert cf.with_capture_facts(entry) is entry


def test_stored_facts_reach_every_clip_record_reader_both_shapes() -> None:
    facts = [
        {"kind": "place", "value": "Eminönü", "provenance": "geocode"},
        {"kind": "landmark", "value": "Galata Bridge", "provenance": "inferred", "confidence": 0.7},
    ]
    with_block = {"understanding": {"subject": "bridge", "summary": "s"}, FACTS_KEY: facts}
    legacy = {"subject": "bridge", "transcript": "hello there", FACTS_KEY: facts}

    block_record = clip_record(with_block)
    legacy_record = clip_record(legacy)

    assert [f.value for f in block_record.facts] == ["Eminönü", "Galata Bridge"]
    assert [f.value for f in legacy_record.facts] == ["Eminönü", "Galata Bridge"]
    # Storing facts must never disturb the legacy projection.
    assert legacy_record.speech.transcript == "hello there"


# ── ordering ─────────────────────────────────────────────────────────────────


def test_order_sorts_timed_clips_and_keeps_untimed_slots() -> None:
    ids = ["a", "b", "c", "d", "e"]
    times = {"a": _t(9), "c": _t(7), "d": _t(8), "e": _t(6)}  # b has no capture time
    result = cf.order_by_capture_time(ids, times)
    assert result.basis == "capture_time"
    assert result.ordered_ids == ["e", "b", "c", "d", "a"]
    assert result.fallback_ids == ["b"]
    assert result.diagnostics() == {
        "ordering_basis": "capture_time",
        "ordering_fallback_clip_ids": ["b"],
    }


def test_order_ties_break_by_attachment_order() -> None:
    result = cf.order_by_capture_time(["a", "b"], {"a": _t(7), "b": _t(7)})
    assert result.ordered_ids == ["a", "b"]


def test_order_with_fewer_than_two_timed_clips_is_attachment_order() -> None:
    none_timed = cf.order_by_capture_time(["a", "b"], {})
    assert (none_timed.basis, none_timed.ordered_ids, none_timed.fallback_ids) == (
        "attachment",
        ["a", "b"],
        ["a", "b"],
    )
    one_timed = cf.order_by_capture_time(["a", "b"], {"b": _t(7)})
    assert (one_timed.basis, one_timed.ordered_ids, one_timed.fallback_ids) == (
        "attachment",
        ["a", "b"],
        ["a"],
    )


def test_capture_time_from_facts_reads_the_iso_value() -> None:
    facts = [
        {"kind": "place", "value": "x"},
        {"kind": "capture_time", "value": "2026-09-20T07:31:02Z"},
    ]
    assert cf.capture_time_from_facts(facts) == datetime(2026, 9, 20, 7, 31, 2, tzinfo=UTC)
    assert cf.capture_time_from_facts([{"kind": "capture_time", "value": "nope"}]) is None
    assert cf.capture_time_from_facts([]) is None


# ── landmark enrichment ──────────────────────────────────────────────────────


def _result(media_id: str, *, kind: str = "video", **extra):
    entry = {"media_id": media_id, "gcs_path": f"p/{media_id}", "generation": "1", **extra}
    ref = SimpleNamespace(kind=kind, analysis=dict(entry.get("analysis") or {}))
    return entry, ref


def test_enrich_adds_capture_copies_and_a_landmark_guess(monkeypatch) -> None:
    calls: list[str] = []

    def guess(assignment, *, ctx):
        calls.append(assignment["media_id"])
        return ClipFact(
            kind="landmark", value="Rumeli Hisarı", provenance="inferred", confidence=0.8
        )

    monkeypatch.setattr(cf, "_guess_landmark", guess)
    updated: list[str] = []
    results = [_result("a", capture=_CAPTURE)]
    out = cf.enrich_clip_facts(
        results,
        make_ctx=lambda media_id: object(),
        on_updated=lambda entry, ref: updated.append(entry["media_id"]),
    )
    entry, _ref = out[0]
    kinds = {(f.kind, f.provenance) for f in cf.assignment_facts(entry)}
    assert kinds == {("capture_time", "exif"), ("place", "geocode"), ("landmark", "inferred")}
    assert calls == ["a"]
    # Checkpointed once for the capture copy, once for the landmark.
    assert updated == ["a", "a"]


def test_enrich_is_fail_open_per_clip(monkeypatch) -> None:
    def guess(assignment, *, ctx):
        if assignment["media_id"] == "bad":
            raise RuntimeError("provider down")
        return ClipFact(kind="landmark", value="Galata Bridge", provenance="inferred")

    monkeypatch.setattr(cf, "_guess_landmark", guess)
    out = cf.enrich_clip_facts(
        [_result("bad", capture=_CAPTURE), _result("ok", capture=_CAPTURE)],
        make_ctx=lambda m: object(),
    )
    by_id = {entry["media_id"]: entry for entry, _ in out}
    assert cf.landmark_fact_for_assignment(by_id["ok"]).value == "Galata Bridge"
    # A failed guess is NOT recorded as attempted, so the next attempt retries it.
    assert cf.landmark_fact_for_assignment(by_id["bad"]) is None
    assert not cf._landmark_attempted(by_id["bad"])


def test_enrich_never_asks_twice_for_the_same_generation(monkeypatch) -> None:
    calls: list[str] = []

    def guess(assignment, *, ctx):
        calls.append(assignment["media_id"])
        return None  # "unknown" is a real answer

    monkeypatch.setattr(cf, "_guess_landmark", guess)
    first = cf.enrich_clip_facts([_result("a", capture=_CAPTURE)], make_ctx=lambda m: object())
    assert cf._landmark_attempted(first[0][0])
    cf.enrich_clip_facts(first, make_ctx=lambda m: object())
    assert calls == ["a"]


def test_enrich_never_calls_the_landmark_agent_without_a_place_or_coordinate(monkeypatch) -> None:
    """Toggle off / no GPS / no Photos access: no where means no landmark guess (and no upload)."""
    monkeypatch.setattr(
        cf, "_guess_landmark", lambda *a, **k: pytest.fail("landmark agent ran with no place")
    )
    time_only = {"capture_time": "2026-09-20T07:31:02Z"}
    out = cf.enrich_clip_facts(
        [_result("none"), _result("time", capture=time_only)], make_ctx=lambda m: object()
    )
    for entry, _ref in out:
        assert cf.landmark_fact_for_assignment(entry) is None
        # Recorded as asked, so a retry or re-plan never re-evaluates it.
        assert cf._landmark_attempted(entry)


def test_enrich_stops_waiting_when_the_landmark_budget_is_spent(monkeypatch) -> None:
    import time

    def guess(assignment, *, ctx):
        if assignment["media_id"] == "slow":
            time.sleep(1.0)
        return ClipFact(kind="landmark", value="Galata Bridge", provenance="inferred")

    monkeypatch.setattr(cf, "_guess_landmark", guess)
    started = time.monotonic()
    out = cf.enrich_clip_facts(
        [_result("fast", capture=_CAPTURE), _result("slow", capture=_CAPTURE)],
        make_ctx=lambda m: object(),
        budget_s=0.2,
    )
    assert time.monotonic() - started < 0.9, "must not wait on the slow provider"
    by_id = {entry["media_id"]: entry for entry, _ in out}
    assert cf.landmark_fact_for_assignment(by_id["fast"]) is not None
    assert cf.landmark_fact_for_assignment(by_id["slow"]) is None
    assert not cf._landmark_attempted(by_id["slow"]), "a skipped clip is retried next time"


def test_landmark_attempt_is_keyed_on_storage_generation_too() -> None:
    entry = {"media_id": "a", "storage_generation": "7", "capture": _CAPTURE}
    recorded = cf.with_landmark_fact(entry, None)
    assert recorded["analysis"][cf.LANDMARK_GENERATION_KEY] == "7"
    assert cf._landmark_attempted(recorded)
    assert not cf._landmark_attempted({**recorded, "storage_generation": "8"})


def test_a_place_that_cleans_to_nothing_never_breaks_capture_facts() -> None:
    capture = cf.capture_from_assignment(
        {"capture": {"place": {"locality": "\x01\x02"}, "capture_time": "2026-09-20T07:31:02Z"}}
    )
    facts = cf.capture_facts(capture)
    assert [f.kind for f in facts] == ["capture_time"]


def test_enrich_skips_images(monkeypatch) -> None:
    monkeypatch.setattr(
        cf, "_guess_landmark", lambda *a, **k: pytest.fail("landmark agent ran for an image")
    )
    out = cf.enrich_clip_facts([_result("img", kind="image")], make_ctx=lambda m: object())
    assert cf.landmark_fact_for_assignment(out[0][0]) is None


def test_enrich_propagates_the_new_analysis_to_the_ref(monkeypatch) -> None:
    from app.schemas.edit_proposal import MediaRef

    monkeypatch.setattr(cf, "_guess_landmark", lambda *a, **k: None)
    entry = {"media_id": "a", "gcs_path": "p/a", "generation": "1", "capture": _CAPTURE}
    ref = MediaRef(lane="clip", media_id="a", gcs_path="p/a", generation="1", kind="video")
    out = cf.enrich_clip_facts([(entry, ref)], make_ctx=lambda m: object())
    assert [f.kind for f in clip_record(out[0][1].analysis).facts] == ["capture_time", "place"]


# ── Main Creator media_context ───────────────────────────────────────────────


async def _context(monkeypatch, *, enabled: bool, analysis=None):
    monkeypatch.setattr(creator_sessions.settings, "clip_facts_enabled", enabled)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=[
            {
                "media_id": "clip-1",
                "gcs_path": "users/u/1.mp4",
                "capture": _CAPTURE,
                **({"analysis": analysis} if analysis else {}),
            },
            {"media_id": "clip-2", "gcs_path": "users/u/2.mp4"},
        ],
    )
    persona = SimpleNamespace(user_id=uuid.uuid4())
    empty = MagicMock()
    empty.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [empty, empty, empty]
    _manifest, media_context = await creator_sessions.resolve_item_creator_context(
        db, item, persona=persona
    )
    return media_context


@pytest.mark.asyncio
async def test_media_context_shows_facts_with_provenance_when_enabled(monkeypatch) -> None:
    stored = {FACTS_KEY: [{"kind": "landmark", "value": "Rumeli Hisarı", "provenance": "inferred"}]}
    context = await _context(monkeypatch, enabled=True, analysis=stored)
    assert context[0]["facts"] == [
        {"kind": "capture_time", "value": "2026-09-20T07:31:02Z", "provenance": "exif"},
        {"kind": "place", "value": "Arnavutköy, İstanbul, Türkiye", "provenance": "geocode"},
        {"kind": "landmark", "value": "Rumeli Hisarı", "provenance": "inferred"},
    ]
    assert "facts" not in context[1]  # nothing known about clip-2


@pytest.mark.asyncio
async def test_media_context_is_unchanged_when_disabled(monkeypatch) -> None:
    stored = {FACTS_KEY: [{"kind": "landmark", "value": "Rumeli Hisarı", "provenance": "inferred"}]}
    context = await _context(monkeypatch, enabled=False, analysis=stored)
    assert all("facts" not in row for row in context)
    assert "facts" not in str(context[0]["analysis_only_not_copy"])
