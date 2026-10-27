"""KRI-994: the finished video's review lists asks as "have a look" that the server can settle.

Prod thread 4C536AFE (Don Julio lunch M2, 2026-10-27, job 26556f5a). The brief held
r3 "Skip the clip where he cuts the cheese ...", r4 "Use my steak photo too" and r8
"Make the montage from the video clips only (no photos)". The creator tapped the cheese
clip out of the picker (`exclude_cheese_cut`, evidence "creator selected"), later wrote
"Forget the steak photo. Make the montage from the video clips only.", and the render
holds three video clips, none of them the cheese one. The review still read:

    Have a look at these in the video: ... Skip the clip where he cuts the cheese ...
    Use my steak photo too ... Make the montage from the video clips only (no photos)

The review is composed by ``_approved_generation_requirements_review``; these tests drive
the real function with the recorded brief, plan record, finished variant and strategy.

IMPORTANT (r4 / r8): the recorded prod rows were written with ``facts={}``. The typed facts
``only_media_kind`` / ``include_media_kind`` (Main Creator v100 / extractor v39) are what the
model is now ASKED to record; whether it complies needs a paid live run that is not done
here. The tests below therefore put the typed facts into the fixture brief by hand, and
``test_recorded_untyped_rows_stay_listed`` pins that untyped rows keep today's honest
"have a look" (the server never guesses a meaning from words).

Failure modes pinned: a tapped skip whose clip the render still plays is not met; no
resolved exclude, a resolver-only guess, an ambiguous or dead link, or an exclusion that
matched nothing claim nothing; a photo in the timeline never lets "clips only" read as met;
a partly overlapping later ask, a same-message pair and an untyped ask withdraw nothing;
Turkish wording works through the typed link, and through verbatim identity (never through
word lists).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.reply_language import reply_language_for
from app.kria.select_asks import exclusion_facts
from app.services.device_render import DEVICE_RENDER_FIELD
from app.tasks import kria_runtime

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri994_don_julio_m2_review.json"
_THREAD = uuid.UUID("4c536afe-60fd-41a5-be8b-a8e1425fa8b3")
_CHEESE = "analysis-proxy-ios-98E88D32-BFD6-45DD-8FB4-99389BB0CEB0.mp4"
_PHOTO = "asset-cfb745e0-513a-4ade-b2c0-8b1b34e54585"
_TURN_8 = "f5c77a83-ef50-4494-941d-f8a46f4f8d87"
_TYPED_PHOTO = {"include_media_kind": "photo"}
_TYPED_VIDEO = {"only_media_kind": "video"}


def _load() -> dict:
    return json.loads(_FIXTURE.read_text())


def _typed_brief(data: dict, *, r8_facts: dict | None = None) -> CreativeBrief:
    """The recorded brief as it would read had the model typed r4 and r8.

    r8 arrives through ``apply_updates`` (the real ledger path): its ``remove`` of r4, which
    the model never emitted, is what the typed supersede rule does.
    """
    rows = copy.deepcopy(data["brief"]["requirements"])
    prior = [r for r in rows if r["id"] != "r8"]
    for row in prior:
        if row["id"] == "r4":
            row["facts"] = dict(_TYPED_PHOTO)
    base = CreativeBrief.model_validate(
        {"version": data["brief"]["version"], "requirements": prior}
    )
    recorded = next(r for r in rows if r["id"] == "r8")
    return apply_updates(
        base,
        [
            BriefUpdate(
                operation="add",
                kind="select",
                scope="global",
                description=recorded["description"],
                facts=_TYPED_VIDEO if r8_facts is None else r8_facts,
            )
        ],
        source_turn_id=_TURN_8,
    )


def _review(
    data: dict | None = None,
    *,
    brief: CreativeBrief | None = None,
    language: str | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, dict[str, dict]]:
    """The real review text + receipts the chat would post for the recorded finished video."""
    data = data or _load()
    brief = brief or CreativeBrief.model_validate(data["brief"])
    variant = copy.deepcopy(data["variant"])
    record = copy.deepcopy(data["unified_montage"])
    generation = str(record["generation_id"])
    # A published phone export carries the phone's attempt id; the plan's generation is read
    # off its device record (`_plan_record_generations`).
    variant["render_generation_id"] = "29d59ee4-271a-4571-8167-00414f7b385b"
    job = SimpleNamespace(
        id=uuid.UUID("26556f5a-cf2a-4ec2-8e4c-321fc84f07f3"),
        assembly_plan={
            "unified_montage": record,
            "variants": [variant],
            DEVICE_RENDER_FIELD: {
                variant["variant_id"]: {
                    "published_attempt": variant["render_generation_id"],
                    "requirement_generation": generation,
                }
            },
        },
        all_candidates={"creator_strategy": copy.deepcopy(data["creator_strategy"])},
    )
    binding = BriefBinding.create(_THREAD, brief)
    execution = SimpleNamespace(
        result={"brief_binding": binding.model_dump(mode="json")}, target_draft_id=None
    )
    thread = SimpleNamespace(id=_THREAD)
    monkeypatch.setattr(kria_runtime, "_source_fingerprints", lambda _job: {})
    monkeypatch.setattr(kria_runtime, "_with_clip_sound_toggles", lambda _j, _v, facts, **_: facts)
    with reply_language_for(language):
        text, receipts = kria_runtime._approved_generation_requirements_review(
            None, thread, job, variant, execution, "The confirmed edit is ready."
        )
    return text, {row["requirement_id"]: row for row in receipts}


def _with_timeline_clip(data: dict, media_id: str, kind: str = "video") -> dict:
    """The recorded video with ``media_id`` added as one more cut (a clip that re-appears)."""
    data = copy.deepcopy(data)
    row = copy.deepcopy(data["variant"]["story_timeline"][0])
    row.update(media_id=media_id, beat_id="unified-cut-4", moment_id="unified-cut-4", kind=kind)
    data["variant"]["story_timeline"].append(row)
    data["unified_montage"]["clip_ids"].append(media_id)
    return data


def _look_section(text: str) -> str:
    return text.partition("Have a look")[2]


# ---------------------------------------------------------------- r3: a tapped skip


def test_r3_a_tapped_exclusion_the_timeline_honours_is_met(monkeypatch):
    """KRI-994 (1): the creator's picker tap left the cheese clip out and the render has none
    of its footage; the server holds both facts, so the ask is checked and met. The recorded
    intent carries no `requirement_id`: it links by verbatim identity of its attribute."""
    data = _load()
    assert data["picker_answer"]["answers"][0]["media_ids"] == [_CHEESE]
    assert _CHEESE not in [row["media_id"] for row in data["variant"]["story_timeline"]]
    assert all("requirement_id" not in row for row in data["creator_strategy"]["clip_intents"])

    text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["verification"] == "checked"
    assert receipts["r3"]["status"] == "met"
    assert "Done: Skip the clip where he cuts the cheese" in text
    assert "Skip the clip where he cuts the cheese" not in _look_section(text)


def test_r3_the_workers_plan_time_receipts_use_the_same_facts():
    """The render worker builds its receipts from the plan record plus `exclusion_facts`
    (`generative_build`, one shared helper with the review)."""
    data = _load()
    brief = CreativeBrief.model_validate(data["brief"])
    facts = dataclasses.replace(
        plan_facts_from_unified_montage(data["unified_montage"]),
        exclusions=exclusion_facts(data["creator_strategy"], brief),
    )

    receipts = {
        r.requirement_id: r
        for r in build_receipts(brief.live(), facts, include_unchecked=True, strict_order=True)
    }

    assert (receipts["r3"].status, receipts["r3"].verification) == ("met", "checked")
    assert receipts["r8"].verification == "unchecked"  # plan time has no finished timeline


def test_r3_stays_honest_when_the_excluded_clip_is_in_the_video(monkeypatch):
    """A pin / placement can keep a clip an exclude names (`clip_scope.excluded_clips`): the
    tap alone is never the proof, the finished timeline is."""
    data = _with_timeline_clip(_load(), _CHEESE)

    text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["status"] == "partial"
    assert receipts["r3"]["verification"] == "checked"
    assert "The clip you asked to skip is still in the video" in text


def test_r3_is_not_met_without_a_creator_exclusion(monkeypatch):
    data = _load()
    data["creator_strategy"]["resolved_clip_intents"] = [
        row for row in data["creator_strategy"]["resolved_clip_intents"] if row["op"] != "exclude"
    ]

    _text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["verification"] == "unchecked"


def test_r3_a_resolver_guess_is_not_the_creators_word(monkeypatch):
    """An exclusion the resolver matched (no tap, no file name) may have picked the wrong clip:
    the clip being absent proves nothing about the creator's ask."""
    data = _load()
    for row in data["creator_strategy"]["resolved_clip_intents"]:
        if row["op"] == "exclude":
            row["assignments"][0]["evidence"] = "waiter cuts the cheese"

    _text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["verification"] == "unchecked"


def test_r3_an_exclusion_that_matched_nothing_claims_nothing(monkeypatch):
    data = _load()
    for row in data["creator_strategy"]["resolved_clip_intents"]:
        if row["op"] == "exclude":
            row["assignments"] = []

    _text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["verification"] == "unchecked"


def _exclusion_data(
    attribute: str, description: str, **intent_fields
) -> tuple[dict, CreativeBrief]:
    """A one-ask brief and a tapped exclusion (the tap's creator evidence)."""
    data = _load()
    brief = CreativeBrief.model_validate(
        {
            "version": 3,
            "requirements": [
                {"id": "r3", "kind": "select", "scope": "global", "description": description},
                {"id": "r9", "kind": "select", "scope": "global", "description": "Başka bir istek"},
            ],
        }
    )
    for row in data["creator_strategy"]["resolved_clip_intents"]:
        if row["op"] == "exclude":
            row["attribute"] = attribute
            row.update(intent_fields)
    return data, brief


def test_r3_the_typed_link_does_not_need_the_words_to_match(monkeypatch):
    """The planner's `requirement_id` links an exclusion whose words the brief paraphrased
    (Turkish ask, English-free attribute): met, and the reason is Turkish when the chat is."""
    data, brief = _exclusion_data(
        "peynir klibi", "Peynir kesilen klibi kullanma, sadece biftek", requirement_id="r3"
    )

    _text, receipts = _review(data, brief=brief, language="tr", monkeypatch=monkeypatch)
    assert receipts["r3"]["status"] == "met"
    assert receipts["r3"]["verification"] == "checked"

    shown = _with_timeline_clip(data, _CHEESE)
    text, receipts = _review(shown, brief=brief, language="tr", monkeypatch=monkeypatch)
    assert receipts["r3"]["status"] == "partial"
    assert "Çıkarmanı istediğin klip hâlâ videoda" in text


def test_r3_a_typed_link_naming_no_live_select_ask_is_dropped():
    """A made-up id, a superseded ask or another kind of requirement links nothing; the
    verbatim fallback then applies (the attribute is in none of them either)."""
    data, _ = _exclusion_data("peynir klibi", "unused")
    brief = CreativeBrief.model_validate(
        {
            "version": 3,
            "requirements": [
                {"id": "r1", "kind": "timing", "scope": "global", "description": "10 saniye"},
                {
                    "id": "r5",
                    "kind": "select",
                    "scope": "global",
                    "status": "superseded",
                    "description": "Peynir kesilen klibi kullanma",
                },
                {"id": "r6", "kind": "select", "scope": "global", "description": "Başka bir şey"},
            ],
        }
    )
    for dead in ("r77", "r5", "r1"):
        for row in data["creator_strategy"]["resolved_clip_intents"]:
            if row["op"] == "exclude":
                row["requirement_id"] = dead
        assert exclusion_facts(data["creator_strategy"], brief) == ()
    for row in data["creator_strategy"]["resolved_clip_intents"]:
        if row["op"] == "exclude":
            row["requirement_id"] = "r6"
    assert [f.requirement_id for f in exclusion_facts(data["creator_strategy"], brief)] == ["r6"]


def test_r3_verbatim_identity_links_turkish_text(monkeypatch):
    """The fallback is string identity (case and whitespace folded), so a Turkish attribute
    that appears verbatim in the one ask that holds it is linked, with no word list."""
    data, brief = _exclusion_data(
        "peynir  kesilen klip", "Skip: Peynir kesilen klip kullanılmasın, sadece biftek"
    )

    _text, receipts = _review(data, brief=brief, language="tr", monkeypatch=monkeypatch)

    assert receipts["r3"]["status"] == "met"
    assert receipts["r3"]["verification"] == "checked"


def test_r3_verbatim_identity_in_two_asks_links_neither(monkeypatch):
    data, brief = _exclusion_data("the cheese clip", "Skip the cheese clip")
    brief = CreativeBrief(
        version=3,
        requirements=[
            *brief.requirements[:1],
            brief.requirements[1].model_copy(
                update={"id": "r4", "description": "Never show the cheese clip"}
            ),
        ],
    )

    assert exclusion_facts(data["creator_strategy"], brief) == ()
    _text, receipts = _review(data, brief=brief, monkeypatch=monkeypatch)
    assert receipts["r3"]["verification"] == "unchecked"


def test_r3_a_paraphrase_is_not_linked_by_overlap(monkeypatch):
    """Shared words are not identity: no token overlap, no stemming."""
    data, brief = _exclusion_data("the clip where he cuts the cheese", "Skip the cheese cutting")

    assert exclusion_facts(data["creator_strategy"], brief) == ()


# ------------------------------------------------------- r4 / r8: typed select facts


def test_recorded_untyped_rows_stay_listed(monkeypatch):
    """The recorded prod rows had `facts={}`: nothing typed, so r4 and r8 keep today's honest
    "have a look" (and r2 / r6, which KRI-993 owns, stay listed too)."""
    text, receipts = _review(monkeypatch=monkeypatch)

    for req_id in ("r2", "r4", "r6", "r8"):
        assert receipts[req_id]["verification"] == "unchecked"
        assert receipts[req_id]["status"] != "met"
    look = _look_section(text)
    for ask in ("only about the steak", "Use my steak photo too", "Quick cuts", "clips only"):
        assert ask in look


def test_r8_clips_only_is_met_when_the_timeline_has_no_photo(monkeypatch):
    """KRI-994 (3): every cut in the finished timeline is a video, so "no photos" is proven."""
    data = _load()
    assert {row["kind"] for row in data["variant"]["story_timeline"]} == {"video"}

    text, receipts = _review(data, brief=_typed_brief(data), monkeypatch=monkeypatch)

    assert receipts["r8"]["verification"] == "checked"
    assert receipts["r8"]["status"] == "met"
    assert "Done: Make the montage from the video clips only" in text


def test_r8_is_not_met_when_a_photo_is_in_the_timeline(monkeypatch):
    data = _with_timeline_clip(_load(), _PHOTO, kind="image")

    text, receipts = _review(data, brief=_typed_brief(data), monkeypatch=monkeypatch)

    assert receipts["r8"]["status"] == "partial"
    assert "The video still has a photo in it" in text


def test_r8_is_not_judged_without_a_finished_timeline(monkeypatch):
    data = _load()
    data["variant"]["story_timeline"] = []

    _text, receipts = _review(data, brief=_typed_brief(data), monkeypatch=monkeypatch)

    assert receipts["r8"]["verification"] == "unchecked"


def test_r4_a_photo_ask_the_creator_withdrew_is_superseded_not_listed(monkeypatch):
    """KRI-994 (2): the typed clips-only ask r8 supersedes the earlier typed photo ask r4 (the
    model did not `remove` it), so the review does not list r4 as something to check."""
    data = _load()
    brief = _typed_brief(data)
    by_id = {req.id: req for req in brief.requirements}
    assert by_id["r4"].status == "superseded"
    assert by_id["r5"].status == "superseded"  # the recorded model `remove` is untouched

    text, receipts = _review(data, brief=brief, monkeypatch=monkeypatch)

    assert "Use my steak photo too" not in text
    assert "r4" not in receipts


def test_supersede_needs_both_asks_typed_and_an_earlier_turn():
    data = _load()

    # The later ask is not typed (the recorded row): nothing is withdrawn by its words.
    assert {r.id: r.status for r in _typed_brief(data, r8_facts={}).requirements}["r4"] == "open"
    # "Also use the steak photo, the rest can stay video": typed photo, no clips-only fact.
    partial = _typed_brief(data, r8_facts=_TYPED_PHOTO)
    assert {r.id: r.status for r in partial.requirements}["r4"] == "open"
    # The earlier ask is not typed.
    rows = copy.deepcopy(data["brief"]["requirements"])
    prior = CreativeBrief.model_validate(
        {"version": 3, "requirements": [r for r in rows if r["id"] != "r8"]}
    )
    update = BriefUpdate(
        operation="add", kind="select", scope="global", description="clips only", facts=_TYPED_VIDEO
    )
    out = apply_updates(prior, [update], source_turn_id=_TURN_8)
    assert {r.id: r.status for r in out.requirements}["r4"] == "open"
    # Same message: a photo ask and a clips-only ask in one batch withdraw neither.
    both = apply_updates(
        CreativeBrief(version=0),
        [
            BriefUpdate(
                operation="add",
                kind="select",
                scope="global",
                description="photo too",
                facts=_TYPED_PHOTO,
            ),
            update,
        ],
        source_turn_id=_TURN_8,
    )
    assert [r.status for r in both.requirements] == ["open", "open"]
    # An earlier ask from this very turn (a retried batch) is never withdrawn by it either.
    typed_prior = CreativeBrief.model_validate(
        {
            "version": 1,
            "requirements": [
                {
                    "id": "r1",
                    "kind": "select",
                    "scope": "global",
                    "description": "photo too",
                    "facts": _TYPED_PHOTO,
                    "source_turn_id": _TURN_8,
                }
            ],
        }
    )
    again = apply_updates(typed_prior, [update], source_turn_id=_TURN_8)
    assert [r.status for r in again.requirements] == ["open", "open"]


def test_turkish_typed_path_end_to_end(monkeypatch):
    """The typed facts hold whatever language the creator wrote: a Turkish photo ask and a
    Turkish clips-only ask, the review in Turkish."""
    data = _load()
    rows = copy.deepcopy(data["brief"]["requirements"])
    for row in rows:
        if row["id"] == "r4":
            row["description"] = "Biftek fotoğrafını da kullan"
            row["facts"] = dict(_TYPED_PHOTO)
    prior = CreativeBrief.model_validate(
        {"version": 3, "requirements": [r for r in rows if r["id"] != "r8"]}
    )
    brief = apply_updates(
        prior,
        [
            BriefUpdate(
                operation="add",
                kind="select",
                scope="global",
                description="Sadece video klipleri kullan, fotoğraf olmasın",
                facts=_TYPED_VIDEO,
            )
        ],
        source_turn_id=_TURN_8,
    )

    text, receipts = _review(data, brief=brief, language="tr", monkeypatch=monkeypatch)

    assert receipts["r8"]["status"] == "met"
    assert "r4" not in receipts
    assert "Biftek fotoğrafını da kullan" not in text
    assert "Yapıldı: Sadece video klipleri kullan" in text

    shown = _with_timeline_clip(data, _PHOTO, kind="image")
    text, _receipts = _review(shown, brief=brief, language="tr", monkeypatch=monkeypatch)
    assert "Videoda hâlâ bir fotoğraf var" in text


def test_asks_the_server_cannot_settle_never_claim_met(monkeypatch):
    """r2 (only about the steak, KRI-993) and r6 (quick cuts) have no checker: they keep
    being listed under typed r4/r8 too (the honesty rule behind KRI-700 / KRI-558)."""
    data = _load()

    text, receipts = _review(data, brief=_typed_brief(data), monkeypatch=monkeypatch)

    for req_id in ("r2", "r6"):
        assert receipts[req_id]["verification"] == "unchecked"
        assert receipts[req_id]["status"] != "met"
    assert "only about the steak" in _look_section(text)


# ------------------------------------------------- the planner's typed link (parse only)


def _plan(op: str, *, exclusions: bool, **extra: object):
    request = "Quad klibini kullanma. Pub klipleri ilk sırada olsun."
    quote = "Quad klibini kullanma" if op == "exclude" else "Pub klipleri ilk sırada olsun"
    attribute = "quad klibi" if op == "exclude" else "pub klipleri"
    row = {"intent_id": "i1", "op": op, "attribute": attribute, "source_quote": quote, **extra}
    if op == "order":
        row["position"] = "first"
    raw = json.dumps({"intents": [row], "question": None}, ensure_ascii=False)
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]
    return agent.parse(
        raw,
        ClipIntentPlannerInput(
            creator_request=request,
            latest_user_message=request,
            clip_facts=True,
            clip_exclusions=exclusions,
        ),
    )


def test_the_planner_keeps_a_declared_requirement_id_on_an_exclude_only():
    kept = _plan("exclude", exclusions=True, requirement_id=" r3 ")
    assert kept.intents[0].requirement_id == "r3"
    # An order never carries it.
    assert _plan("order", exclusions=True, requirement_id="r3").intents[0].requirement_id is None
    # Unset: omitted from the dump, so every stored intent re-dumps byte-identically.
    unset = _plan("exclude", exclusions=True).intents[0]
    assert unset.requirement_id is None
    assert "requirement_id" not in unset.model_dump(mode="json")
