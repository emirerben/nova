"""Toroko (2026-11): an order the creator gave over GROUPS of clips never blocks on the order
INSIDE a group (`KRIA_UNSTATED_ORDER_DEFAULT_ENABLED`).

Prod threads 666b996f and de471354: a creator attached 19 clips and wrote, in Turkish, that the
video must stay under a minute, that the video showing the map is first, the train entry parts
second, the videos of friends inside the train next, and the scenery after. The draft was held
(`approval_requirements_not_ready`, `simplification_requires_choice`) on "you didn't say what
order the clips in <groups> should play in", with a "simpler version" card that dropped the whole
ordering. The rule now: what the creator says is kept exactly; what they did not say (the order
inside a named group, the clips nobody mentioned) is the order they ADDED the clips, said on the
card, and never a hold.

Ways this can fail, written before the fix (each is pinned below):

 F1 the incident shape (TR request, 19 clips, a first group and three described groups, no basis
    key): with the flag on the draft is still held, or still offers the "simpler version".
 F2 a second hold sits behind the first: with the group marker set, no render-contract pin and no
    readiness credit leave `_check_order` at "can't confirm", so r2 is `evidence_missing`
    (Leverkusen M1 run 2, KRI-1080). Stamping alone is not the fix.
 F3 the contract pins one order and the worker plays another (the KRI-503 dead end): the plan
    `preflight_montage_plan` builds (the worker's own planner) must play exactly the order the
    contract pins. The fixture's attachment order differs from filming order and from each
    intent's own assignment order, so agreeing by coincidence is impossible.
 F4 the flag off changes anything (the group is asked about, in plain words, exactly as before);
    with it on the same input is not asked.
 F5 a group the creator ordered inside (`ordered_members` true) is restamped or flattened.
 F6 a basis intent (`order_by`) or a one-clip group is stamped (the schema refuses the marker
    with `order_by`; a one-clip group has no inner order).
 F7 one clip asked first AND last: the earlier claim must stand and be said, not swallowed by the
    default.
 F8 a creator-placed clip swapped out of its seat reads `met` (negative control for the worker's
    own receipt).
 F9 a writer no phone verifier pins (not the phone montage), a "group first" answer, or an edit
    that shows Visuals gets credit for an order nothing checks.
 F10 a saved draft that still carries the unset marker is refused or re-asked after the flag is
    on, or a stored strategy is re-stamped (`ordered_members` is in the strategy digest).
 F11 a media id, UUID or file name reaches any creator-visible string.
 F12 the disclosure exists in English and Turkish only, or reads differently in a catalog
    language (German is checked where the catalog carries it).

Runs the REAL planner order hook (`brief_order_fields`), approval readiness (manifest, policy,
render contract, receipts) and the worker's own phone montage plan (`preflight_montage_plan`).
No model is called.

Recorded vs reconstructed: the creator's request (Turkish) and the shape of the outcome are
recorded from the two prod threads; real thread data is not in the repo. Everything else is
RECONSTRUCTED: 19 neutral clips (rows cloned from the Leverkusen M1 run 2 media snapshot, whose
clip analysis is itself recorded), their capture times (scrambled against the order they were
added), and which clips each described group holds. Group membership is interleaved on purpose
and the unmentioned clips are spread across the attachment list.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import (
    assess_readiness,
    readiness_matches,
    readiness_reply,
    with_defaulted_order_receipts,
)
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
)
from app.kria.planner import brief_order_fields
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.clip_order_sequence import unconfirmed_order_groups
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract
from app.services.phone_montage_preflight import preflight_montage_plan

LEV: dict[str, Any] = json.loads(
    Path(__file__).with_name("lev_m1_run2_filming_order.json").read_text()
)
CREATOR = uuid.UUID("00000000-0000-4000-8000-0000000070c0")
ITEM = "7e5a0a00-0000-4000-8000-000000000019"
THREAD = "00000000-0000-4000-8000-000000000666"

REQUEST_TR = (
    "Bir dakikayı geçmeyecek şekilde. Haritayı gösterdiğim video ilk video olacak. "
    "Trene giriş kısımları ikinci video. Tren içerisinde arkadaşlarımı gösterdiğim videolar "
    "ve sonrasında manzaralar olacak"
)

N = 19


def _media_id(index: int) -> str:
    return (
        f"analysis-proxy-ios-{str(uuid.uuid5(uuid.NAMESPACE_URL, f'toroko-{index}')).upper()}.mp4"
    )


# Attachment order is 0..18. The groups are interleaved in it, the unmentioned clips are spread.
MAP = (5,)
ENTRY = (2, 9, 14)
FRIENDS = (4, 7, 11, 17)
SCENERY = (1, 3, 8, 12, 16)
UNMENTIONED = tuple(i for i in range(N) if i not in {*MAP, *ENTRY, *FRIENDS, *SCENERY})
# Hand-written: first, then each group in the order they were listed with its clips in the order
# they were ADDED, then every clip nobody mentioned in the order it was added.
EXPECTED = [
    _media_id(i) for i in (5, 2, 9, 14, 4, 7, 11, 17, 1, 3, 8, 12, 16, 0, 6, 10, 13, 15, 18)
]
ATTACHMENT = [_media_id(i) for i in range(N)]

GROUPS = (
    ("order_map_first", "haritayı gösterdiğim video", "first", MAP, (5,)),
    ("order_train_entry", "trene giriş kısımları", None, ENTRY, (14, 2, 9)),
    (
        "order_friends",
        "tren içinde arkadaşlarımı gösterdiğim videolar",
        None,
        FRIENDS,
        (11, 17, 7, 4),
    ),
    ("order_scenery", "manzaralar", None, SCENERY, (12, 3, 16, 8, 1)),
)
GROUP_NAMES = [name for _id, name, _pos, _members, _assigned in GROUPS]

_IDS = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|analysis-proxy|asset-|\.mp4|\.mov|IMG_",
    re.IGNORECASE,
)

DISCLOSURE_EN = "where you didn't say, the clips play in the order you added them"
DISCLOSURE_TR = "söylemediğin yerlerde klipler eklediğin sırayla oynar"


def _snapshot() -> dict[str, Any]:
    template = copy.deepcopy(LEV["media_snapshot"]["clip_assignments"][0])
    rows, paths, sources, windows = [], [], [], []
    for index in range(N):
        row = copy.deepcopy(template)
        media_id = _media_id(index)
        path = f"users/u-toroko/creation-threads/{THREAD}/{media_id}"
        row.update(
            media_id=media_id,
            gcs_path=path,
            manifest_identity=media_id,
            source_filename=f"clip-{index:02d}.mov",
            storage_generation=str(1793385165153027 + index),
            library_asset_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"asset-{index}")),
            library_source_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"source-{index}")),
            duration_s=8.0,
            # Filming time runs against the order they were added, so a filming-order plan can
            # never be mistaken for an upload-order one.
            capture={"capture_time": f"2026-11-1{(N - index) // 10}T1{(N - index) % 10}:00:00Z"},
        )
        row["analysis"]["duration_s"] = 8.0
        row["upload_contract"]["proxy"]["duration_s"] = 8.0
        row["upload_contract"]["proxy"]["original"]["duration_s"] = 8.0
        # Distinct bytes: byte-identical uploads are collapsed into one before planning.
        row["upload_contract"]["proxy"]["original"]["sha256"] = f"{index:02x}" * 32
        rows.append(row)
        paths.append(path)
        sources.append(
            {
                "kind": "video",
                "gcs_path": path,
                "media_id": media_id,
                "duration_s": 8.0,
                "generation": None,
                "storage_generation": row["storage_generation"],
            }
        )
        windows.append(
            {
                "score": 7.0,
                "reason": "best_moment",
                "media_id": media_id,
                "source_start_s": 1.0,
                "source_end_s": 3.5,
            }
        )
    return {
        "clip_paths": paths,
        "clip_assignments": rows,
        "visuals": [],
        "length_plan": {
            "owner": "footage",
            "sources": sources,
            "version": 1,
            "windows": windows,
            "conflicts": [],
            "capacity_s": 8.0 * N,
            "natural_owner": "footage",
            "capacity_raw_s": 8.0 * N,
            "window_eligible": True,
            "max_source_count": 80,
            "target_duration_s": 2.5 * N,
            "natural_duration_s": None,
        },
        "best_parts": N,
        "best_parts_conflicts": 0,
        "best_parts_capacity_s": 8.0 * N,
    }


def _brief(**r2_facts: Any) -> CreativeBrief:
    turn = "78855290-993b-4696-ada2-dc121510ced2"
    return CreativeBrief.model_validate(
        {
            "version": 1,
            "requirements": [
                {
                    "id": "r1",
                    "kind": "timing",
                    "facts": {"duration_s": 60, "duration_bound": "at_most"},
                    "scope": "global",
                    "status": "open",
                    "literal": None,
                    "description": "Keep it under one minute",
                    "source_turn_id": turn,
                },
                {
                    "id": "r2",
                    "kind": "order",
                    "facts": r2_facts,
                    "scope": "global",
                    "status": "open",
                    "literal": None,
                    "description": (
                        "The map video first, then the train entry parts, then the videos of "
                        "friends inside the train, then the scenery"
                    ),
                    "source_turn_id": turn,
                },
            ],
        }
    )


def _intent(
    intent_id: str, name: str, position: str | None, assigned: tuple[int, ...], **extra: Any
) -> dict[str, Any]:
    return {
        "op": "order",
        "status": "resolved",
        "attribute": name,
        "intent_id": intent_id,
        **({"position": position} if position else {}),
        "assignments": [
            {"media_id": _media_id(i), "confidence": 0.9, "evidence": "described"} for i in assigned
        ],
        **extra,
    }


def _strategy_dict(intents: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pacing": "balanced",
        "direction": "guided_story",
        "voice_mode": "per_clip",
        "edit_format": "montage",
        "media_scope": "selected",
        "clip_intents": [
            {
                key: row[key]
                for key in ("op", "attribute", "intent_id", "position", "ordered_members")
                if key in row
            }
            for row in intents
        ],
        "caption_style": "auto",
        "audio_strategy": "original_audio",
        "render_program": "guided",
        "story_structure": ["The map", "The train", "The friends", "The scenery"],
        "target_duration_s": 2.5 * N,
        "selected_media_ids": list(ATTACHMENT),
        "resolved_clip_intents": intents,
        "video_reuse_policy": "once",
        "optional_treatments": [],
        "montage_audio": {
            "source_media_ids": [],
            "preview_source_beds": False,
            "preserve_source_audio": True,
        },
    }


def _drafted(brief: CreativeBrief, *, groups: tuple = GROUPS, **marks: Any) -> dict[str, Any]:
    """The turn's strategy as the planner writes it: the resolved intents through the planner's
    own order hook (`brief_order_fields`)."""
    raw = [_intent(i, n, p, assigned, **marks.get(i, {})) for i, n, p, _m, assigned in groups]
    intents = [ResolvedClipIntent.model_validate(row) for row in raw]
    intents, choice = brief_order_fields(intents, brief, None)
    strategy = _strategy_dict([intent.model_dump(mode="json") for intent in intents])
    if choice:
        strategy["ordering_choice"] = choice
    return strategy


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)


def _report(
    brief: CreativeBrief,
    strategy: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    media = snapshot if snapshot is not None else _snapshot()
    manifest = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
            for row in media["clip_assignments"]
        ],
    )
    return assess_readiness(brief, strategy, media=media, manifest=manifest, creator_id=CREATOR)


def _rows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["requirement_id"]: row for row in report["requirements"]}


def _planned(brief: CreativeBrief, strategy: dict[str, Any], snapshot: dict[str, Any]):
    planned = preflight_montage_plan(
        creator_id=CREATOR,
        edit_format="montage",
        brief=brief,
        strategy=strategy,
        media_snapshot=snapshot,
        visuals=[],
    )
    assert isinstance(planned, tuple), planned
    return planned[0]


# -- F1: the incident --------------------------------------------------------------------------


@pytest.mark.parametrize("r2_facts", [{}, {"key": "narrative"}], ids=["no-key", "narrative-key"])
def test_the_incident_is_held_today_and_ready_with_the_flag(
    r2_facts: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 + F4: flag off is the incident (held on the order of the groups); on, it is ready."""
    brief = _brief(**r2_facts)
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    held = _report(brief, _drafted(brief))
    assert held["ready"] is False
    assert _rows(held)["r2"]["disposition"] == "needs_choice"
    assert "You didn't say what order" in _rows(held)["r2"]["reason"]

    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)
    ready = _report(brief, _drafted(brief))
    assert ready["ready"] is True, ready["requirements"]
    assert _rows(ready)["r2"]["disposition"] == "executable"
    assert DISCLOSURE_EN in _rows(ready)["r2"]["reason"]
    assert ready["policy_problem"] is None and ready["policy_refusal"] is None


# -- F2: the second hold ------------------------------------------------------------------------


def test_the_marker_alone_leaves_the_second_hold_so_the_pin_and_credit_are_needed(
    flag_on: None,
) -> None:
    """F2: with the groups already settled (the creator answered "any order"), r2 must still not
    read "I can't confirm" (`evidence_missing`)."""
    brief = _brief()
    settled = {i: {"ordered_members": False} for i, *_rest in GROUPS}
    report = _report(brief, _drafted(brief, **settled))
    assert _rows(report)["r2"]["disposition"] == "executable", report["requirements"]


# -- F3: the worker plays what the contract pins ----------------------------------------------


def test_the_contract_pins_the_attachment_order_and_the_worker_plays_it(flag_on: None) -> None:
    brief = _brief()
    strategy = _drafted(brief)
    snapshot = _snapshot()
    contract = build_render_contract(
        strategy, generation_id="toroko", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None and not contract.unresolved
    assert contract.order_required and contract.order_basis == "attachment_order"
    assert list(contract.order_ids) == EXPECTED
    assert list(contract.order_ids) != ATTACHMENT
    plan = _planned(brief, strategy, snapshot)
    facts = plan_facts_from_unified_montage(plan.record())
    assert list(facts.clip_ids) == list(contract.order_ids)
    assert not facts.sequence_unconfirmed
    [r2] = build_receipts([brief.live()[1]], facts, include_unchecked=True)
    assert (r2.status, r2.verification) == ("met", "checked"), r2


def test_a_welcome_clip_among_the_unmentioned_does_not_move_against_the_pin(
    flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: the worker's spoken-welcome reorder (KRI-585, `speech_order_approved`) moves an order
    nobody chose; the pin is a chosen one, so a "welcome" clip among the unmentioned stays put.
    The control proves the reorder is live in this fixture: without the pin it moves."""
    from app.services import creator_render_contract as contract_module

    brief = _brief()
    # Camera sound for every clip (no voice mode): the one shape the spoken order applies to.
    strategy = {**_drafted(brief), "voice_mode": None}
    snapshot = _snapshot()
    welcome_id = _media_id(13)
    words = "Hoş geldiniz arkadaşlar"
    welcome = snapshot["clip_assignments"][13]["analysis"]
    welcome["transcript"] = words
    welcome["understanding"]["speech"] = {
        "has_speech": True,
        "to_camera": True,
        "transcript": words,
    }
    snapshot["length_plan"]["spoken_order"] = {"roles": {welcome_id: "opening"}}
    contract = build_render_contract(
        strategy, generation_id="toroko", brief=brief, media_snapshot=snapshot
    )
    plan = _planned(brief, strategy, snapshot)
    assert list(plan_facts_from_unified_montage(plan.record()).clip_ids) == list(contract.order_ids)

    monkeypatch.setattr(contract_module, "_unstated_order_defaults_to_attachment", lambda *a: False)
    unpinned = _planned(brief, strategy, snapshot)
    assert list(plan_facts_from_unified_montage(unpinned.record()).clip_ids) != list(
        contract.order_ids
    )


# -- F4: flag off is today -------------------------------------------------------------------


def test_flag_off_pins_nothing_and_stamps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    brief = _brief()
    strategy = _drafted(brief)
    assert all("ordered_members" not in i for i in strategy["resolved_clip_intents"])
    assert len(unconfirmed_order_groups(strategy["resolved_clip_intents"])) == 3
    contract = build_render_contract(
        strategy, generation_id="toroko", brief=brief, media_snapshot=_snapshot()
    )
    assert contract is not None and not contract.order_required and not contract.order_ids


def test_with_the_flag_the_multi_clip_groups_are_marked_unordered_and_nothing_else(
    flag_on: None,
) -> None:
    strategy = _drafted(_brief())
    marks = {i["intent_id"]: i.get("ordered_members") for i in strategy["resolved_clip_intents"]}
    assert marks == {
        "order_map_first": None,  # one clip: no inner order
        "order_train_entry": False,
        "order_friends": False,
        "order_scenery": False,
    }
    assert not unconfirmed_order_groups(strategy["resolved_clip_intents"])


# -- F5 / F6: what the default leaves alone ------------------------------------------------------


def test_a_group_the_creator_ordered_inside_keeps_its_order(flag_on: None) -> None:
    strategy = _drafted(_brief(), **{"order_train_entry": {"ordered_members": True}})
    marks = {i["intent_id"]: i.get("ordered_members") for i in strategy["resolved_clip_intents"]}
    assert marks["order_train_entry"] is True
    brief = _brief()
    contract = build_render_contract(
        strategy, generation_id="toroko", brief=brief, media_snapshot=_snapshot()
    )
    # the creator's listed order inside the entry group (14, 2, 9) is the seating, not upload order
    assert [m for m in contract.order_ids if m in {_media_id(i) for i in ENTRY}] == [
        _media_id(i) for i in (14, 2, 9)
    ]


def test_a_basis_intent_and_a_one_clip_group_are_never_stamped(flag_on: None) -> None:
    brief = _brief()
    basis = {
        "op": "order",
        "status": "resolved",
        "attribute": "everything",
        "intent_id": "order_basis",
        "order_by": "capture_time",
        "assignments": [
            {"media_id": _media_id(i), "confidence": 1.0, "evidence": "filming order"}
            for i in (0, 1, 2)
        ],
    }
    intents = [ResolvedClipIntent.model_validate(basis)]
    stamped, _choice = brief_order_fields(intents, brief, None)
    assert stamped[0].ordered_members is None
    one = _drafted(brief)["resolved_clip_intents"]
    assert (
        next(i for i in one if i["intent_id"] == "order_map_first").get("ordered_members") is None
    )


# -- F7: one clip asked first and last -----------------------------------------------------------


def test_a_clip_asked_first_and_last_keeps_the_earlier_claim_and_says_so(flag_on: None) -> None:
    brief = _brief()
    groups = (
        ("order_open", "haritayı gösterdiğim video", "first", (5, 9), (5, 9)),
        ("order_close", "trenden çıkış", "last", (9, 5), (9, 5)),
    )
    strategy = _drafted(brief, groups=groups)
    row = _rows(_report(brief, strategy))["r2"]
    assert row["disposition"] != "executable", row
    assert "same clip in two places" in row["reason"] or "iki ayrı yere" in row["reason"]


# -- F8: negative control on the worker's own receipt ------------------------------------------


def test_a_creator_placed_clip_in_the_wrong_seat_does_not_read_met(
    flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import unified_montage

    seat = unified_montage._apply_sequence

    def swapped(ordered, rows):
        result = list(seat(ordered, rows))
        result[0], result[-1] = result[-1], result[0]
        return result

    monkeypatch.setattr(unified_montage, "_apply_sequence", swapped)
    brief = _brief()
    plan = _planned(brief, _drafted(brief), _snapshot())
    facts = plan_facts_from_unified_montage(plan.record())
    [r2] = build_receipts([brief.live()[1]], facts, include_unchecked=True)
    assert r2.status != "met", r2


# -- F9: no credit where nothing checks the order ----------------------------------------------


def test_no_phone_route_gets_no_credit(flag_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    brief = _brief()
    row = _rows(_report(brief, _drafted(brief)))["r2"]
    assert row["disposition"] != "executable", row


def test_a_group_first_answer_gets_no_credit(flag_on: None) -> None:
    brief = _brief()
    strategy = {**_drafted(brief), "ordering_choice": "group_first"}
    assert _rows(_report(brief, strategy))["r2"]["disposition"] != "executable"


def test_an_edit_showing_photos_is_not_pinned_to_an_order_the_photos_break(
    flag_on: None,
) -> None:
    brief = _brief()
    strategy = _drafted(brief)
    snapshot = _snapshot()
    strategy["selected_media_ids"] = [
        *strategy["selected_media_ids"],
        "asset-00000000-0000-4000-8000-0000000000aa",
    ]
    snapshot["visuals"] = [
        {
            "kind": "image",
            "capture": {},
            "media_id": "asset-00000000-0000-4000-8000-0000000000aa",
            "source_filename": "photo.jpeg",
        }
    ]
    contract = build_render_contract(
        strategy, generation_id="toroko", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None and contract.order_basis != "attachment_order"


# -- F10: saved drafts -------------------------------------------------------------------------


def test_a_saved_draft_with_the_unset_marker_is_ready_and_never_restamped(
    flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strategy saved before the flag (marker unset, never stamped) reads as defaulted, and
    the report a fresh assessment gives matches the one saved with it."""
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    brief = _brief()
    saved_strategy = _drafted(brief)
    assert all("ordered_members" not in i for i in saved_strategy["resolved_clip_intents"])
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)
    before = copy.deepcopy(saved_strategy)
    fresh = _report(brief, saved_strategy)
    assert saved_strategy == before
    assert fresh["ready"] is True
    assert not unconfirmed_order_groups(saved_strategy["resolved_clip_intents"])
    assert readiness_matches(copy.deepcopy(fresh), _report(brief, saved_strategy))
    # the old card's saved report (held) never matches: it is not an approval to inherit
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    old = _report(brief, saved_strategy)
    assert old["ready"] is False and not readiness_matches(old, fresh)


# -- F11 / F12: what a creator reads -----------------------------------------------------------


def _visible(brief: CreativeBrief, report: dict[str, Any]) -> list[str]:
    return [
        readiness_reply(brief, report),
        *(str(r["reason"] or "") for r in report["requirements"]),
    ]


@pytest.mark.parametrize(("language", "disclosure"), [("en", DISCLOSURE_EN), ("tr", DISCLOSURE_TR)])
def test_the_disclosure_is_plain_words_in_the_creators_language(
    flag_on: None, language: str, disclosure: str
) -> None:
    brief = _brief()
    with reply_language_for(language):
        report = _report(brief, _drafted(brief))
        strings = _visible(brief, report)
    assert disclosure in _rows(report)["r2"]["reason"]
    assert not [text for text in strings if _IDS.search(text)], strings


def test_german_reads_the_catalog_disclosure(
    flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    brief = _brief()
    with reply_language_for("de"):
        report = _report(brief, _drafted(brief))
    reason = _rows(report)["r2"]["reason"]
    assert DISCLOSURE_EN not in reason and "Reihenfolge" in reason
    assert not _IDS.search(reason)


def test_the_flag_off_question_names_the_groups_in_the_creators_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4 + F11: unchanged wording, still no id or file name."""
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    brief = _brief()
    report = _report(brief, _drafted(brief))
    reason = _rows(report)["r2"]["reason"]
    assert all(f'"{name}"' in reason for name in GROUP_NAMES[1:])
    assert not _IDS.search(reason)


def test_a_pin_only_brief_is_asked_without_the_flag_and_not_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4 inverted (the Leverkusen pin, `test_without_filming_order_the_group_is_still_asked_in_
    plain_words`): "start with the map video" names no basis and no inner order."""
    brief = _brief(first_clip="the video where I show the map")
    groups = (
        ("order_map_first", "the video where I show the map", "first", (5, 9, 14), (14, 5, 9)),
    )
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    asked = _rows(_report(brief, _drafted(brief, groups=groups)))["r2"]
    assert asked["disposition"] == "needs_choice" and "You didn't say what order" in asked["reason"]
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)
    settled = _rows(_report(brief, _drafted(brief, groups=groups)))["r2"]
    assert settled["disposition"] == "executable", settled


def test_a_saved_legacy_list_row_reads_as_defaulted_with_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPEN DECISION (owner), pinned not endorsed: the cost of "an unset marker is defaulted"
    (F10). KRI-941's Don Julio row ("in this order: the door, the butcher, ...") was stored as ONE
    group with the marker unset, and
    the marker is the only thing that tells a list from a group (wording is never parsed,
    KRI-667). Flag off it asks; flag on it plays upload order inside the row, which the model's
    own marker (`ordered_members` true, written when the creator lists an order) still beats."""
    from tests.pipeline.test_kri941_listed_order_replay import (
        _plan,
        _receipts,
        _recorded_strategy,
    )

    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    assert _receipts(_plan(_recorded_strategy()))["r2"].status == "partial"
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)
    assert _receipts(_plan(_recorded_strategy()))["r2"].status == "met"
    listed = _recorded_strategy()
    for intent in listed["resolved_clip_intents"]:
        if intent["intent_id"] == "order_sequence_between":
            intent["ordered_members"] = True
    assert _plan(listed).clip_ids != _plan(_recorded_strategy()).clip_ids


def _draft_receipts(brief: CreativeBrief, strategy: dict[str, Any]) -> list[dict[str, Any]]:
    """The receipts the draft turn records for its own strategy (`plan_facts_from_strategy`)."""
    media = _snapshot()
    manifest = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
            for row in media["clip_assignments"]
        ],
    )
    facts = plan_facts_from_strategy(
        strategy, clip_ids=[row["media_id"] for row in media["clip_assignments"]], manifest=manifest
    )
    return [
        receipt.model_dump(mode="json")
        for receipt in build_receipts(
            brief.live(), facts, include_unchecked=True, strict_order=True
        )
    ]


@pytest.mark.parametrize(("language", "disclosure"), [("en", DISCLOSURE_EN), ("tr", DISCLOSURE_TR)])
def test_the_draft_turns_own_receipt_carries_the_disclosure(
    flag_on: None, language: str, disclosure: str
) -> None:
    """F1 + F11 + F12 on what the draft turn records: its own receipt for r2 reads "I can't
    confirm the order this draft uses" (a strategy draft has no ordering basis), so the ready
    readiness row's reason replaces it, in the creator's language, with no id or file name."""
    brief = _brief()
    strategy = _drafted(brief)
    with reply_language_for(language):
        receipts = _draft_receipts(brief, strategy)
        r2_before = next(r for r in receipts if r["requirement_id"] == "r2")
        assert (r2_before["status"], r2_before["verification"]) == ("partial", "unchecked")
        report = _report(brief, strategy)
        shown = with_defaulted_order_receipts(receipts, report)
    r2 = next(r for r in shown if r["requirement_id"] == "r2")
    assert (r2["status"], r2["verification"], r2["reason"]) == ("met", "checked", disclosure)
    r1 = next(r for r in shown if r["requirement_id"] == "r1")
    assert r1 == next(r for r in receipts if r["requirement_id"] == "r1")
    assert not _IDS.search(json.dumps(shown, ensure_ascii=False))


def test_a_held_report_or_the_flag_off_leaves_the_draft_receipts_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = _brief()
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    strategy = _drafted(brief)
    held = _report(brief, strategy)
    receipts = _draft_receipts(brief, strategy)
    assert with_defaulted_order_receipts(receipts, held) == receipts
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)
    assert with_defaulted_order_receipts(receipts, held) == receipts  # still not ready


def test_a_single_clip_pin_is_held_today_and_ready_with_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured, not assumed: "start with the map video" (one clip, the rest unmentioned) is
    held today on "I can't confirm this draft does it yet" (`evidence_missing`), the second
    hold with no group involved, so the default covers the clips nobody mentioned too."""
    brief = _brief()
    groups = (("order_map_first", "the map video", "first", (5,), (5,)),)
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", False)
    held = _rows(_report(brief, _drafted(brief, groups=groups)))["r2"]
    assert held["disposition"] == "evidence_missing"
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)
    ready = _rows(_report(brief, _drafted(brief, groups=groups)))["r2"]
    assert ready["disposition"] == "executable" and DISCLOSURE_EN in ready["reason"]
    contract = build_render_contract(
        _drafted(brief, groups=groups),
        generation_id="toroko",
        brief=brief,
        media_snapshot=_snapshot(),
    )
    assert list(contract.order_ids) == [_media_id(i) for i in (5, *[i for i in range(N) if i != 5])]
