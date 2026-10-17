"""KRI-681: a montage clip the creator names by a spoken line is cut on that line.

Incident (prod thread 133bdded, 2026-10-15, Tigre night boat kit M1): twelve clips, fast
cuts, 25 s. "End on the clip where I say tam 20 dakika sürdü, and let that sentence be
heard"; "keep the sound on the clip where the boat won't start". Every cut was ~2.2 s
from second 0. The closing clip says the line at ~12.3-13.6 s (measured on the source
audio), the boat clip says "tekne çalışmıyor" at ~3.6-5.5 s: neither was in the video,
and the review still said "Yapıldı: Özel klip sırası".

The clip data below is the incident's own (``tests/fixtures/kri681_tigre_night_m1_clips``):
the closing clip's only timed segment runs 3.78-13.64 s.

Ways this can fail, each pinned below:
1. The line is not in quote marks ("tam 20 dakika sürdü dediğim klip", quotes dropped by
   KRI-677), so nothing looks for it.
2. The ask carries words beyond the line ("dediğim klip"); a window is searched for the
   whole ask and never found.
3. The line sits inside one long timed segment: the "found" window is the whole 10 s
   segment, or the cut keeps only its first 2 s.
4. A word-timed pass that has the line is ignored, or one that misses it drops the line.
5. Music plays on the other clips (a song owns the length): the line is not held at all.
6. A clip described by what is said in it ("the boat-won't-start clip, keep its sound")
   still plays its first 2 s.
7. The opening clip named by a call ("Amigo Sergio diye seslendiğimiz") opens on silence.
8. A description no transcript says ("the river clip"), a single shared word, or one two
   clips say equally moves a window anyway.
9. The review reads only the ask's short title ("Özel klip sırası") and says met.
10. A line the clip never says (or nobody can time) is reported as met.
11. The length drifts off 25 s, or the phone verifier refuses the windows.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import check_requirement, plan_facts_from_rendered_montage
from app.kria.planner import _enrich_best_parts_snapshot
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.creator_render_contract import build_render_contract
from app.services.spoken_quotes import (
    closing_quote_notice,
    described_line_moments,
    ground_closing_quote,
    said_line,
)
from tests.pipeline.test_unified_montage import clip
from tests.tasks.test_selected_clip_dispatch import _verify

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri681_tigre_night_m1_clips.json"
CLIPS = json.loads(_FIXTURE.read_text(encoding="utf-8"))["clips"]
FIRST, BOAT, LAST = "m1_11", "m1_02", "m1_03"
LINE = "tam 20 dakika sürdü"
# Where the words are in the source audio (voice-band bursts over the engine noise).
LINE_SPAN = (12.3, 13.6)
BOAT_SPAN = (3.6, 5.5)
CALL_SPAN = (11.1, 12.0)
FIRST_ASK = "iskelede Amigo Sergio diye seslendiğimiz karanlık klip"
LAST_ASK = "tam 20 dakika sürdü dediğim klip"

R1 = BriefRequirement(id="r1", kind="text", scope="title", literal="Gece 23:00, Tigre")
R2 = BriefRequirement(
    id="r2",
    kind="order",
    scope="global",
    description="Özel klip sırası",
    facts={"first_clip": FIRST_ASK, "key": "narrative", "last_clip": LAST_ASK},
)
R3 = BriefRequirement(
    id="r3",
    kind="audio",
    scope="global",
    description="Teknenin çalışmadığı klipte ve en sondaki klipte orijinal ses açık kalacak, "
    "geri kalanlarda sakin bir müzik çalacak",
    facts={"preserve_source_audio": True},
)
R8 = BriefRequirement(
    id="r8", kind="timing", scope="global", description="25 saniye", facts={"duration_s": 25}
)


def _analysis(media_id: str, clips: list[dict] = CLIPS) -> dict:
    return next(c["analysis"] for c in clips if c["media_id"] == media_id)


def _snapshot(clips: list[dict] = CLIPS) -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": c["media_id"],
                "kind": "video",
                "gcs_path": f"users/u/analysis-proxy-{c['media_id']}.mp4",
                "storage_generation": "1",
                "duration_s": c["duration_s"],
                "analysis": c["analysis"],
            }
            for c in clips
        ]
    }


def _intent(position: str, media_id: str, attribute: str) -> dict:
    return {
        "op": "order",
        "status": "resolved",
        "intent_id": f"order_{position}",
        "attribute": attribute,
        "position": position,
        "assignments": [{"media_id": media_id, "confidence": 1.0, "evidence": "speech"}],
    }


def _strategy(**extra) -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "fast",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "opening_title": "Gece 23:00, Tigre",
        "target_duration_s": 25.0,
        "creator_duration_s": 25.0,
        "target_duration_requested": True,
        "video_reuse_policy": "once",
        "resolved_clip_intents": [
            _intent("first", FIRST, FIRST_ASK),
            _intent("last", LAST, LAST_ASK),
        ],
        **extra,
    }


def _brief(*reqs: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=5, requirements=list(reqs or (R1, R2, R3, R8)))


def _transcriber(words: list[dict], calls: list | None = None):
    def run(gcs_path: str, *, language: str | None, prompt: str | None):
        if calls is not None:
            calls.append((gcs_path, language, prompt))
        return words, language or ""

    return run


def _no_words(gcs_path: str, *, language: str | None, prompt: str | None):
    raise TimeoutError("whisper unavailable")


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


async def _plan(strategy: dict, brief: CreativeBrief, *, clips=CLIPS, transcribe=_no_words):
    snapshot = _snapshot(clips)
    quote = await ground_closing_quote(strategy, snapshot, transcribe=transcribe)
    return _enrich_best_parts_snapshot(strategy, snapshot, brief, closing_quote=quote), quote


def _render(strategy: dict, snapshot: dict, brief: CreativeBrief, clips=CLIPS):
    resolved = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-681"),
        brief,
        latest_message="Continue",
        media_snapshot=snapshot,
    ).resolve()
    unified = [
        dataclasses.replace(
            clip(i, duration=c["duration_s"]),
            media_id=c["media_id"],
            proxy_path=f"users/u/analysis-proxy-{c['media_id']}.mp4",
            analysis=c["analysis"],
            width=1080,
            height=1920,
        )
        for i, c in enumerate(clips)
    ]
    plan = plan_unified_montage(
        unified,
        brief_view(resolved),
        strategy=strategy,
        clip_intents_enabled=True,
        creator_duration_proven=True,
        approved_length_plan=snapshot["length_plan"],
    )
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=snapshot
    )
    return plan, contract, unified


def _finished(plan) -> dict:
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    return {"story_timeline": execution["story_timeline"], "source_audio_preserved": True}


def _holds(start: float, end: float, span: tuple[float, float]) -> bool:
    return start <= span[0] and end >= span[1]


def _window(snapshot: dict, media_id: str) -> tuple[float, float]:
    rows = [w for w in snapshot["length_plan"]["windows"] if w["media_id"] == media_id]
    assert len(rows) == 1, rows
    return rows[0]["source_start_s"], rows[0]["source_end_s"]


# -- the incident, end to end ----------------------------------------------------------


async def test_incident_ends_on_the_named_line_and_plays_the_described_lines() -> None:
    strategy, brief = _strategy(), _brief()
    snapshot, quote = await _plan(strategy, brief)

    # 1 + 2: the unquoted line grounds on the closing clip's own words.
    assert quote["status"] == "found" and quote["media_id"] == LAST
    assert quote["quote"] == LINE
    # 3 + 4: no word timings, so the line is placed inside the 10 s segment, not the
    # whole segment.
    assert quote["source"] == "segment_estimate"
    assert _holds(quote["start_s"], quote["end_s"], LINE_SPAN)
    assert quote["end_s"] - quote["start_s"] < 4.0

    assert not snapshot["length_plan"]["conflicts"]
    assert snapshot["length_plan"]["closing_quote"]["status"] == "found"
    # 6 + 7: the described clips start on their sentences.
    assert _holds(*_window(snapshot, BOAT), BOAT_SPAN)
    assert _holds(*_window(snapshot, FIRST), CALL_SPAN)

    plan, contract, clips = _render(strategy, snapshot, brief)
    cuts = plan.snapshot.fast_cuts
    assert len(cuts) == len(CLIPS)
    assert cuts[0].media_id == FIRST and _holds(
        cuts[0].source_start_s, cuts[0].source_end_s, CALL_SPAN
    )
    assert cuts[-1].media_id == LAST and _holds(
        cuts[-1].source_start_s, cuts[-1].source_end_s, LINE_SPAN
    )
    boat = next(c for c in cuts if c.media_id == BOAT)
    assert _holds(boat.source_start_s, boat.source_end_s, BOAT_SPAN)
    # 11: still 25 s, and the phone recipe verifies.
    assert plan.duration_s == pytest.approx(25.0, abs=0.05)
    assert _verify(contract, plan, clips)

    # 9: the finished video's review judges the line, and it is there.
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    assert facts.closing_speech is not None and facts.closing_speech.quote == LINE
    receipt = check_requirement(R2, facts)
    assert receipt.status == "met", receipt.reason
    assert LINE in (receipt.reason or "")


async def test_word_timings_place_the_line_when_the_word_pass_has_it() -> None:
    # 4: whisper's words for the end of the clip; the line at 12.4-13.4 s.
    rows = [("Yaklaşık", 11.3), ("20", 11.7), ("dakika", 11.9), ("sürdü.", 12.1)]
    rows += [("Tam", 12.4), ("20", 12.6), ("dakika", 12.8), ("sürdü", 13.1), ("ha.", 13.4)]
    words = [{"text": t, "start_s": s, "end_s": round(s + 0.25, 2)} for t, s in rows]
    calls: list = []
    _snap, quote = await _plan(_strategy(), _brief(), transcribe=_transcriber(words, calls))
    assert calls == [(f"users/u/analysis-proxy-{LAST}.mp4", "tr", LINE)]
    assert quote["source"] == "words"
    assert quote["start_s"] <= 12.4 and quote["end_s"] >= 13.35
    assert quote["end_s"] - quote["start_s"] < 3.0


async def test_music_on_the_other_clips_still_ends_on_the_line() -> None:
    # 5: the creator's first message: calm music on the rest, so a song section owns
    # the length (no creator length here).
    strategy = _strategy(
        audio_strategy="licensed_music",
        montage_audio={"preserve_source_audio": True, "source_media_ids": [BOAT, LAST]},
        selected_music_section={"start_s": 30.0, "end_s": 46.6},
        creator_duration_s=None,
        target_duration_s=16.6,
        target_duration_requested=False,
    )
    snapshot, quote = await _plan(strategy, _brief(R1, R2, R3))
    plan = snapshot["length_plan"]
    assert not plan["conflicts"]
    assert plan["closing_quote"]["status"] == "found"
    windows = plan["windows"]
    assert windows[-1]["media_id"] == LAST
    assert _holds(windows[-1]["source_start_s"], windows[-1]["source_end_s"], LINE_SPAN)
    assert _holds(*_window(snapshot, BOAT), BOAT_SPAN)
    total = sum(w["source_end_s"] - w["source_start_s"] for w in windows)
    assert total == pytest.approx(16.6, abs=0.05)


# -- a line that is not there is never "met" -------------------------------------------


def _clips_without_timed_line() -> list[dict]:
    """The untimed transcript has the line; the timed segment lost it."""
    out = json.loads(json.dumps(CLIPS))
    for row in out:
        if row["media_id"] == LAST:
            speech = row["analysis"]["understanding"]["speech"]
            speech["segments"] = [
                {"text": "Çocuklar parkta oynuyor arkadaşlar.", "start_s": 3.78, "end_s": 6.0}
            ]
    return out


async def test_a_line_nobody_can_time_is_said_and_never_reported_met() -> None:
    # 10: whisper misses it too.
    clips = _clips_without_timed_line()
    strategy, brief = _strategy(), _brief()
    snapshot, quote = await _plan(strategy, brief, clips=clips, transcribe=_transcriber([]))
    assert quote["status"] == "not_found" and quote["quote"] == LINE
    notice = closing_quote_notice(snapshot["length_plan"]["closing_quote"])
    assert notice and LINE in notice

    plan, contract, unified = _render(strategy, snapshot, brief, clips)
    assert _verify(contract, plan, unified)
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    receipt = check_requirement(R2, facts)
    assert receipt.status != "met"
    assert LINE in (receipt.reason or "")


async def test_a_closing_clip_that_never_says_the_line_is_not_met() -> None:
    # 9 + 10: the incident's own windows (no line found anywhere): the order is right,
    # but the review no longer calls the ending done.
    clips = json.loads(json.dumps(CLIPS))
    for row in clips:
        if row["media_id"] == LAST:
            row["analysis"]["transcript"] = ""
            row["analysis"]["understanding"]["speech"] = {"has_speech": False}
    strategy, brief = _strategy(), _brief()
    snapshot, quote = await _plan(strategy, brief, clips=clips)
    assert quote is None
    plan, _contract, _unified = _render(strategy, snapshot, brief, clips)
    assert plan.snapshot.fast_cuts[-1].media_id == LAST
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    assert check_requirement(R2, facts).status != "met"


# -- what must not move ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("ask", "media_id", "line"),
    [
        (LAST_ASK, LAST, LINE),
        ("en sonda tam 20 dakika sürdü dediğim klip olsun", LAST, LINE),
        (FIRST_ASK, FIRST, "Amigo Sergio"),
        # 8: one content word, or words the clip never says.
        ("Tigre klibi", LAST, None),
        ("the river clip at night", LAST, None),
        ("o klip", BOAT, None),
    ],
)
def test_said_line_is_only_the_words_the_clip_says(ask: str, media_id: str, line) -> None:
    assert said_line(ask, _analysis(media_id)) == line


def test_a_description_two_clips_say_equally_moves_nothing() -> None:
    # 8: both clips say "tekne çalışmıyor" once.
    clips = json.loads(json.dumps(CLIPS))
    for row in clips:
        if row["media_id"] == "m1_10":
            row["analysis"]["understanding"]["speech"] = {
                "has_speech": True,
                "segments": [{"text": "Tekne çalışmıyor.", "start_s": 1.0, "end_s": 2.0}],
            }
        if row["media_id"] == BOAT:
            row["analysis"]["understanding"]["speech"]["segments"] = [
                {"text": "Tekne çalışmıyor.", "start_s": 4.0, "end_s": 5.5}
            ]
    assert described_line_moments(["Teknenin çalışmadığı klipte ses açık"], _snapshot(clips)) == {}


async def test_an_edit_that_names_no_line_keeps_its_windows() -> None:
    # 8: nothing named, nothing described -> the selector's windows, byte for byte.
    plain = _strategy(resolved_clip_intents=[])
    brief = _brief(R1, R8)
    snapshot, quote = await _plan(plain, brief)
    assert quote is None
    unrelated = _brief(
        R1,
        R8,
        BriefRequirement(
            id="r9", kind="audio", scope="global", description="Sakin bir müzik olsun"
        ),
    )
    again, _quote = await _plan(plain, unrelated)
    assert again["length_plan"]["windows"] == snapshot["length_plan"]["windows"]
    assert _window(snapshot, LAST) == (0.0, pytest.approx(_window(snapshot, LAST)[1]))
