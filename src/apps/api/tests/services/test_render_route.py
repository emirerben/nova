"""KRI-470 PR-D: the one route resolver, the route stamp, and the raw-text guards.

Failure modes written down before the code:

1. The resolver reads request text (chat, ``creator_request``, brief prose): rewording a
   message would change the route.  Guarded by invariance tests AND a source guard.
2. An unsupported combination silently gets a route instead of a typed refusal with an
   alternative (camera audio + recorded voice; camera audio + creator song; ...).
3. The stamp lands on a job that never carried the plan-authority stamp, or a resolver
   fault stops a job from being created.
4. A stored v1 contract stops reading once the route field exists.
5. A route exists on one platform's table but the resolver cannot produce it.

Everything runs through persisted job dicts (the shape dispatch reads), not hand-fed
resolver inputs, so the job adapter is under test as well.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.services import render_route
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    PLAN_AUTHORITY_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
    read_render_contract,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.services.render_route import (
    Route,
    RouteCapabilities,
    resolve_route,
    route_inputs_from_job,
    stamp_route,
)


def _strategy(**fields) -> dict:
    return CreativeStrategy.model_validate(fields).model_dump(mode="json", exclude_none=True)


def _job(
    platform: str,
    *,
    edit_format: str = "montage",
    strategy: dict | None = None,
    contract: dict | None = None,
    voiceover: bool = False,
    song: dict | None = None,
    clips: int = 3,
    guided: bool = False,
    speech: list[bool | None] | None = None,
    stamped: bool = True,
) -> tuple[dict, dict]:
    """(assembly_plan, all_candidates) as a stamped job persists them."""
    strategy = strategy if strategy is not None else _strategy(edit_format=edit_format)
    built = build_render_contract(strategy, generation_id="gen-1", has_voiceover=voiceover)
    assert built is not None
    built = built.rebind(**(contract or {}))
    assembly: dict = {
        CONTRACT_FIELD: built.model_dump(mode="json"),
        "creator_generation_id": "gen-1",
    }
    if platform == "phone":
        assembly[PHONE_SOURCES_FIELD] = []
    if guided:
        assembly["guided_edit"] = {"approved_proposal": {}}
    if speech is not None:
        assembly["creator_brief_binding"] = {
            "media_snapshot": {
                "clip_assignments": [
                    {"media_id": f"c{i}"}
                    if flag is None
                    else {
                        "media_id": f"c{i}",
                        "analysis": {
                            "understanding": {
                                "speech": {"has_speech": flag, "transcript": "hi" if flag else ""}
                            }
                        },
                    }
                    for i, flag in enumerate(speech)
                ]
            }
        }
    candidates: dict = {
        REQUIREMENT_VERSION_FIELD: 1,
        "edit_format": edit_format,
        "creator_strategy": strategy,
        "clip_paths": [f"clip-{i}" for i in range(clips)],
    }
    if stamped:
        candidates[PLAN_AUTHORITY_FIELD] = 1
    if voiceover:
        candidates["voiceover_gcs_path"] = "voiceover/u/voice.m4a"
    if song is not None:
        candidates["user_song"] = {"gcs_path": "song/u/s.mp3", "generation": 1, **song}
    return assembly, candidates


def _resolve(platform: str, *, capabilities: RouteCapabilities | None = None, **job):
    assembly, candidates = _job(platform, **job)
    inputs = route_inputs_from_job(
        assembly, candidates, platform=platform, capabilities=capabilities
    )
    assert inputs is not None
    return resolve_route(inputs)


_VOICE = {"audio_strategy": "voiceover"}
_SPEECH_IDS = {
    "audio_strategy": "original_audio",
    "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["c0"]},
}


# --- Every route is reachable from a plan ------------------------------------------------

ROUTE_TABLE = [
    # (platform, job kwargs, expected route)
    ("phone", {}, Route.UNIFIED_MONTAGE),
    ("phone", {"edit_format": "day_vlog"}, Route.UNIFIED_MONTAGE),
    ("phone", {"edit_format": "single_hero"}, Route.UNIFIED_MONTAGE),
    ("phone", {"strategy": _strategy(**_VOICE), "voiceover": True}, Route.VOICEOVER_MONTAGE),
    ("phone", {"strategy": _strategy(**_SPEECH_IDS)}, Route.SPEECH_MONTAGE),
    (
        "phone",
        {"strategy": _strategy(audio_strategy="user_song"), "song": {"sync": "background"}},
        Route.USER_SONG_MONTAGE,
    ),
    (
        "phone",
        {
            "strategy": _strategy(audio_strategy="user_song", song_sync="lipsync"),
            "song": {"sync": "lipsync"},
        },
        Route.LIPSYNC_MONTAGE,
    ),
    ("phone", {"guided": True}, Route.GUIDED_STORY),
    ("phone", {"edit_format": "subtitled", "clips": 1}, Route.SUBTITLED),
    ("phone", {"edit_format": "narrated_ready", "clips": 1}, Route.SUBTITLED),
    ("phone", {"edit_format": "narrated_planned", "clips": 3}, Route.TALKING_HEAD),
    (
        "phone",
        {"edit_format": "narrated", "strategy": _strategy(**_VOICE), "voiceover": True},
        Route.NARRATED,
    ),
    ("cloud", {}, Route.MONTAGE),
    ("cloud", {"strategy": _strategy(**_VOICE), "voiceover": True}, Route.VOICEOVER),
    ("cloud", {"edit_format": "day_vlog"}, Route.DAY_VLOG),
    ("cloud", {"edit_format": "single_hero"}, Route.SINGLE_HERO),
    ("cloud", {"edit_format": "subtitled", "clips": 1}, Route.SUBTITLED),
    ("cloud", {"edit_format": "talking_head", "speech": [True, False]}, Route.TALKING_HEAD),
    ("cloud", {"edit_format": "narrated_ready", "clips": 1}, Route.SUBTITLED),
    ("cloud", {"edit_format": "narrated_ready", "clips": 4}, Route.TALKING_HEAD),
    (
        "cloud",
        {"edit_format": "narrated", "strategy": _strategy(**_VOICE), "voiceover": True},
        Route.NARRATED,
    ),
    ("cloud", {"edit_format": "slides", "clips": 0}, Route.SLIDES),
    ("cloud", {"strategy": _strategy(render_program="guided"), "guided": True}, Route.GUIDED_STORY),
]


@pytest.mark.parametrize(
    "platform,job,expected",
    ROUTE_TABLE,
    ids=[f"{p}-{r.value}-{i}" for i, (p, _, r) in enumerate(ROUTE_TABLE)],
)
def test_the_approved_plan_resolves_to_its_route(platform, job, expected) -> None:
    resolution = _resolve(platform, **job)
    assert resolution.outcome == "route", resolution
    assert resolution.route is expected
    assert resolution.drivers, "a route names the plan fields that drove it"


def test_every_route_is_produced_by_the_table() -> None:
    assert {route for _, _, route in ROUTE_TABLE} == set(Route)


# --- Typed refusals with alternatives, mirroring check_phone_dispatch_contract -----------

REFUSALS = [
    # camera audio + recorded voice
    (
        "phone",
        {
            "strategy": _strategy(**_SPEECH_IDS),
            "contract": {"require_voiceover": True},
            "voiceover": True,
        },
        "requirement_conflict",
        "montage_audio.source_media_ids[]",
    ),
    # required speech + creator song
    (
        "phone",
        {
            "strategy": _strategy(**_SPEECH_IDS, song_sync="background")
            | {"audio_strategy": "user_song"},
            "song": {"sync": "background"},
        },
        "requirement_conflict",
        "montage_audio.source_media_ids[]",
    ),
    # a guided story cannot keep a named camera-audio set
    (
        "phone",
        {"strategy": _strategy(**_SPEECH_IDS), "guided": True},
        "capability_unavailable",
        "montage_audio.source_media_ids[]",
    ),
    ("phone", {"edit_format": "slides"}, "capability_unavailable", "edit_format"),
    (
        "phone",
        {"edit_format": "subtitled", "strategy": _strategy(**_VOICE), "voiceover": True},
        "requirement_conflict",
        "edit_format",
    ),
    (
        "cloud",
        {"strategy": _strategy(**_SPEECH_IDS)},
        "capability_unavailable",
        "montage_audio.source_media_ids[]",
    ),
    (
        "cloud",
        {"strategy": _strategy(audio_strategy="user_song"), "song": {"sync": "background"}},
        "capability_unavailable",
        "audio_strategy",
    ),
    (
        "cloud",
        {"contract": {"order_required": True, "order_ids": ("c0",)}},
        "capability_unavailable",
        "ordering_choice",
    ),
    (
        "cloud",
        {"edit_format": "subtitled", "strategy": _strategy(**_VOICE), "voiceover": True},
        "requirement_conflict",
        "edit_format",
    ),
    (
        "cloud",
        {"guided": True, "strategy": _strategy(render_program="native"), "edit_format": "montage"},
        "requirement_conflict",
        "edit_format",
    ),
    (
        "cloud",
        {"edit_format": "narrated_ready", "clips": 2, "speech": [False, False]},
        "capability_unavailable",
        "edit_format",
    ),
    (
        "cloud",
        {"edit_format": "talking_head", "speech": [False]},
        "capability_unavailable",
        "edit_format",
    ),
]


@pytest.mark.parametrize("platform,job,reason,field_path", REFUSALS)
def test_unsupported_combinations_refuse_typed_with_an_alternative(
    platform, job, reason, field_path
) -> None:
    resolution = _resolve(platform, **job)
    assert resolution.outcome == "refusal", resolution
    assert (resolution.reason, resolution.field_path) == (reason, field_path)
    assert resolution.alternative, "a refusal always offers the creator something to do"
    assert resolution.route is None


def test_a_rollout_flag_off_is_a_typed_refusal_not_a_silent_montage() -> None:
    off = RouteCapabilities(subtitled=False)
    resolution = _resolve("cloud", edit_format="subtitled", clips=1, capabilities=off)
    assert (resolution.outcome, resolution.reason) == ("refusal", "capability_unavailable")
    assert "capabilities.subtitled" in resolution.drivers


NEEDS_CHOICE = [
    ("phone", {"contract": {"unresolved": ("I need capture times.",)}}, "unresolved_requirement"),
    ("cloud", {"contract": {"unresolved": ("I need capture times.",)}}, "unresolved_requirement"),
    ("phone", {"strategy": _strategy(**_VOICE)}, "voiceover_recording"),
    (
        "phone",
        {"strategy": _strategy(audio_strategy="user_song")},
        "song_upload",
    ),
]


@pytest.mark.parametrize("platform,job,kind", NEEDS_CHOICE)
def test_open_decisions_come_back_as_a_typed_choice_not_a_route(platform, job, kind) -> None:
    resolution = _resolve(platform, **job)
    assert resolution.outcome == "needs_choice"
    assert resolution.choice_kind == kind
    assert resolution.reason == "needs_choice" and resolution.route is None


def test_speech_facts_only_refuse_when_every_clip_was_analysed_and_none_speaks() -> None:
    unknown = _resolve("cloud", edit_format="narrated_ready", clips=2, speech=[None, False])
    assert unknown.outcome == "route" and unknown.route is Route.TALKING_HEAD
    spoken = _resolve("cloud", edit_format="narrated_ready", clips=2, speech=[True, False])
    assert spoken.route is Route.TALKING_HEAD


# --- Raw request text is never an input ----------------------------------------------------

FORBIDDEN_NAMES = (
    "creator_request",
    "user_message",
    "latest_message",
    "first_user_message",
    "mentions_speech",
    "speech_montage_possible",
    "chat",
)


def test_the_resolver_module_never_names_request_text_helpers() -> None:
    """A source guard: render_route.py may not import or reference anything that carries
    the creator's words. Docstrings/comments may explain the rule; code may not use it."""
    source = Path(render_route.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    seen: list[tuple[str, int]] = []
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.alias):
            names.append(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                names.append(node.value)
        for name in names:
            if any(bad in name.casefold() for bad in FORBIDDEN_NAMES):
                seen.append((name, getattr(node, "lineno", 0)))
    assert not seen, f"render_route.py references request-text helpers: {seen}"


def test_the_resolver_inputs_carry_no_free_text_field() -> None:
    from dataclasses import fields

    from app.services.render_route import RouteInputs

    free_text = [
        f.name
        for f in fields(RouteInputs)
        if f.type in ("str", "str | None") and f.name != "edit_format"
    ]
    assert not free_text, f"RouteInputs gained string fields: {free_text}"


def _wording_variants(job: tuple[dict, dict]) -> list[tuple[dict, dict]]:
    """The same approved plan with every raw-text carrier rewritten, shuffled or blanked."""
    assembly, candidates = job
    out = []
    for text in (
        "",
        "make it fast",
        "USE MY VOICE and do NOT use the clip audio",
        "ünïcode — 20k 🏃",
    ):
        a = json.loads(json.dumps(assembly))
        c = json.loads(json.dumps(candidates))
        c["creator_request"] = text
        c["brief"] = {"creator_request": text, "description": text}
        a["creator_brief_binding"] = {
            **(a.get("creator_brief_binding") or {}),
            "creator_request": text,
            "latest_message": text,
        }
        out.append((a, c))
    return out


@pytest.mark.parametrize(
    "platform,job,expected",
    ROUTE_TABLE,
    ids=[f"{p}-{r.value}-{i}" for i, (p, _, r) in enumerate(ROUTE_TABLE)],
)
def test_rewording_the_request_never_changes_the_route(platform, job, expected) -> None:
    baseline = _resolve(platform, **job)
    for assembly, candidates in _wording_variants(_job(platform, **job)):
        inputs = route_inputs_from_job(assembly, candidates, platform=platform)
        assert resolve_route(inputs) == baseline


# --- Stamp ---------------------------------------------------------------------------------


def test_a_plan_authority_job_gets_its_route_and_platform_stamped() -> None:
    assembly, candidates = _job("phone", strategy=_strategy(**_SPEECH_IDS))
    stamped = stamp_route(assembly, candidates)
    contract = read_render_contract(stamped)
    assert contract is not None
    assert (contract.route, contract.route_platform) == ("speech_montage", "phone")
    # the integrity digest covers the stamp: it reads back, and tampering is rejected
    tampered = json.loads(json.dumps(stamped))
    tampered[CONTRACT_FIELD]["route"] = "montage"
    with pytest.raises(Exception, match="changed"):
        read_render_contract(tampered)


def test_an_unstamped_job_is_returned_untouched() -> None:
    assembly, candidates = _job("phone", stamped=False)
    before = json.dumps(assembly, sort_keys=True)
    assert stamp_route(assembly, candidates) is assembly
    assert json.dumps(assembly, sort_keys=True) == before
    # no new key at all: an older worker's extra="forbid" reader still accepts the contract
    assert "route" not in assembly[CONTRACT_FIELD]
    assert "route_platform" not in assembly[CONTRACT_FIELD]


def test_a_refusal_or_open_choice_stamps_no_route() -> None:
    assembly, candidates = _job("cloud", strategy=_strategy(**_SPEECH_IDS))
    stamped = stamp_route(assembly, candidates)
    assert stamped == assembly
    assembly, candidates = _job("phone", contract={"unresolved": ("x",)})
    assert read_render_contract(stamp_route(assembly, candidates)).route is None  # type: ignore[union-attr]


def test_the_stamp_never_raises_into_job_creation(monkeypatch) -> None:
    assembly, candidates = _job("phone")

    def boom(_inputs):
        raise RuntimeError("resolver fault")

    monkeypatch.setattr(render_route, "resolve_route", boom)
    assert stamp_route(assembly, candidates) is assembly


def test_a_job_without_a_contract_is_left_alone() -> None:
    assembly, candidates = _job("phone")
    assembly.pop(CONTRACT_FIELD)
    assert stamp_route(assembly, candidates) is assembly
