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


def test_the_resolver_inputs_are_a_typed_projection_without_prose() -> None:
    """Every string-typed field of the resolver's inputs is enumerated or an opaque id: a new
    free-text field has to be added to this allowlist on purpose."""
    from dataclasses import fields

    from app.services.render_route import ContractFacts, PlanFacts, RouteInputs

    allowed = {
        # enumerated (coerced to a known vocabulary before the resolver sees it)
        ("RouteInputs", "edit_format"),
        ("RouteInputs", "platform"),
        ("PlanFacts", "audio_strategy"),
        ("PlanFacts", "song_sync"),
        ("PlanFacts", "render_program"),
        ("ContractFacts", "original_audio"),
        # a matrix field path derived from the exact text's ROLE, never its content
        ("ContractFacts", "text_field_path"),
        # an opaque digest, only echoed into stamps/events
        ("RouteInputs", "contract_digest"),
    }
    seen = {
        (cls.__name__, f.name)
        for cls in (RouteInputs, PlanFacts, ContractFacts)
        for f in fields(cls)
        if "str" in str(f.type) or "Literal" in str(f.type) or "Platform" in str(f.type)
    }
    assert seen == allowed, f"resolver inputs changed: {sorted(seen ^ allowed)}"


def test_the_resolver_never_reads_the_contract_digest() -> None:
    import dataclasses

    assembly, candidates = _job("phone", strategy=_strategy(**_SPEECH_IDS))
    inputs = route_inputs_from_job(assembly, candidates, platform="phone")
    assert resolve_route(inputs) == resolve_route(dataclasses.replace(inputs, contract_digest="x"))


def test_every_free_text_strategy_field_is_classified_for_the_rewording_tests() -> None:
    from tests.services.route_prose import unclassified_string_fields

    assert unclassified_string_fields() == [], (
        "a CreativeStrategy field can hold prose but the invariance rewrite does not cover it"
    )


@pytest.mark.parametrize(
    "platform,job,expected",
    ROUTE_TABLE,
    ids=[f"{p}-{r.value}-{i}" for i, (p, _, r) in enumerate(ROUTE_TABLE)],
)
def test_rewording_everything_a_creator_or_model_wrote_never_changes_the_route(
    platform, job, expected
) -> None:
    """Request text, brief prose, strategy prose (rationale, titles, labels, story), the
    contract's exact texts, unresolved messages and clip transcripts all rewritten."""
    from tests.services.route_prose import reword_job

    baseline = _resolve(platform, **job)
    assembly, candidates = _job(platform, **job)
    for text in (
        "",
        "make it fast",
        "USE MY VOICE and do NOT use the clip audio",
        "ünï 20k 🏃",
        "talking head narrated subtitled voiceover song speech chronological captions slides",
    ):
        a, c = reword_job(assembly, candidates, text)
        inputs = route_inputs_from_job(a, c, platform=platform)
        assert resolve_route(inputs) == baseline, text


def test_the_rewording_actually_rewrites_something() -> None:
    """Guards the guard: the variants must differ from the original in the places that matter."""
    from tests.services.route_prose import reword_job

    strategy = _strategy(opening_title="Exact title", shot_labels=["One", "Two"])
    assembly, candidates = _job("cloud", strategy=strategy)
    a, c = reword_job(assembly, candidates, "different")
    assert c["creator_strategy"]["opening_title"] != "Exact title"
    assert c["creator_strategy"]["shot_labels"] != ["One", "Two"]
    assert c["creator_strategy"]["rationale"] and c["creator_strategy"]["story_structure"]
    original = read_render_contract(assembly).exact_texts
    changed = read_render_contract(a).exact_texts
    assert [t.text for t in changed] != [t.text for t in original]
    assert [t.role for t in changed] == [t.role for t in original]


# --- Phone formats come from the settings-aware set ----------------------------------------


def test_a_phone_format_flag_off_refuses_instead_of_routing(monkeypatch) -> None:
    from app.config import settings
    from app.services.render_route import route_capabilities_from_settings

    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", False)
    caps = route_capabilities_from_settings()
    assert "subtitled" not in caps.phone_supported_formats
    off = _resolve("phone", edit_format="subtitled", clips=1, capabilities=caps)
    assert (off.outcome, off.reason, off.field_path) == (
        "refusal",
        "capability_unavailable",
        "edit_format",
    )
    assert "capabilities.phone_supported_formats" in off.drivers
    # the same plan routes when the capability is on
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    on = _resolve(
        "phone", edit_format="subtitled", clips=1, capabilities=route_capabilities_from_settings()
    )
    assert on.route is Route.SUBTITLED


# --- Speech facts ----------------------------------------------------------------------------


def _legacy_shaped_rows(count: int) -> dict:
    # Analysis with no `understanding` block: what older analyses stored.
    return {
        "creator_brief_binding": {
            "media_snapshot": {
                "clip_assignments": [
                    {"media_id": f"c{i}", "analysis": {"description": "a run", "subject": "x"}}
                    for i in range(count)
                ]
            }
        }
    }


def test_legacy_shaped_analysis_is_unknown_speech_not_no_speech() -> None:
    assembly, candidates = _job("phone", edit_format="narrated_ready", clips=2)
    assembly.update(_legacy_shaped_rows(2))
    inputs = route_inputs_from_job(assembly, candidates, platform="phone")
    assert inputs.clip_has_speech == (None, None)
    resolution = resolve_route(inputs)
    assert resolution.outcome == "route" and resolution.route is Route.TALKING_HEAD


def test_a_real_no_speech_analysis_still_refuses() -> None:
    resolution = _resolve("phone", edit_format="narrated_ready", clips=2, speech=[False, False])
    assert (resolution.outcome, resolution.reason) == ("refusal", "capability_unavailable")
    assert "media.speech" in resolution.drivers


# --- Stamp ---------------------------------------------------------------------------------


def test_a_plan_authority_job_gets_a_sibling_route_stamp_and_an_untouched_contract() -> None:
    from app.services.render_route import ROUTE_STAMP_FIELD, read_route_stamp

    assembly, candidates = _job("phone", strategy=_strategy(**_SPEECH_IDS))
    stamped = stamp_route(assembly, candidates)
    contract = read_render_contract(stamped)
    assert contract is not None
    # the strict contract dict is byte-identical: older workers read it unchanged
    assert stamped[CONTRACT_FIELD] == assembly[CONTRACT_FIELD]
    assert stamped[ROUTE_STAMP_FIELD] == {
        "route": "speech_montage",
        "platform": "phone",
        "contract_digest": contract.digest,
    }
    assert read_route_stamp(stamped, contract.digest) == stamped[ROUTE_STAMP_FIELD]


def test_a_stamp_for_another_contract_is_stale_and_ignored() -> None:
    from app.services.render_route import read_route_stamp

    assembly, candidates = _job("phone", strategy=_strategy(**_SPEECH_IDS))
    stamped = stamp_route(assembly, candidates)
    rebound = read_render_contract(stamped).rebind(duration_s=15)  # e.g. an editor revision
    assert read_route_stamp(stamped, rebound.digest) is None
    assert read_route_stamp({}, rebound.digest) is None
    assert read_route_stamp({"creator_route": "junk"}, rebound.digest) is None


def test_a_job_stamped_by_new_code_is_readable_by_the_unchanged_strict_contract() -> None:
    """The reviewer's rolling-deploy scenario: main's strict model, new code's stamped job."""
    import json
    from pathlib import Path

    from app.services.creator_render_contract import CreatorRenderContract

    golden = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "creator_render_contract.schema.json").read_text()
    )
    assembly, candidates = _job("phone", strategy=_strategy(**_SPEECH_IDS))
    stamped = stamp_route(assembly, candidates)
    assert "creator_route" in stamped
    assert set(stamped[CONTRACT_FIELD]) <= set(golden["properties"])  # no key main's model lacks
    assert golden["additionalProperties"] is False
    assert CreatorRenderContract.model_json_schema() == golden
    assert read_render_contract(stamped) is not None


def test_an_unstamped_job_is_returned_untouched() -> None:
    assembly, candidates = _job("phone", stamped=False)
    before = json.dumps(assembly, sort_keys=True)
    assert stamp_route(assembly, candidates) is assembly
    assert json.dumps(assembly, sort_keys=True) == before
    assert "creator_route" not in assembly


def test_a_refusal_or_open_choice_stamps_nothing() -> None:
    assembly, candidates = _job("cloud", strategy=_strategy(**_SPEECH_IDS))
    assert stamp_route(assembly, candidates) is assembly
    assembly, candidates = _job("phone", contract={"unresolved": ("x",)})
    assert stamp_route(assembly, candidates) is assembly


def test_a_stamp_that_no_longer_resolves_is_removed_not_kept() -> None:
    """Build-time inputs said montage; once a guided snapshot lands on a native plan the
    resolver refuses -- the old route must not survive."""
    assembly, candidates = _job("cloud", strategy=_strategy(render_program="native"))
    first = stamp_route(assembly, candidates)
    assert first["creator_route"]["route"] == "montage"
    first["guided_edit"] = {"approved_proposal": {}}
    second = stamp_route(first, candidates)
    assert "creator_route" not in second
    assert second[CONTRACT_FIELD] == first[CONTRACT_FIELD]


def test_a_creator_song_attached_after_the_first_look_is_in_the_final_stamp() -> None:
    song_plan = _strategy(audio_strategy="user_song")
    assembly, candidates = _job("phone", strategy=song_plan)
    assert stamp_route(assembly, candidates) is assembly  # no song yet: song_upload choice
    candidates["user_song"] = {"gcs_path": "song/u/s.mp3", "generation": 1, "sync": "background"}
    assert stamp_route(assembly, candidates)["creator_route"]["route"] == "user_song_montage"


def test_the_stamp_never_raises_into_dispatch(monkeypatch) -> None:
    assembly, candidates = _job("phone")

    def boom(_inputs):
        raise RuntimeError("resolver fault")

    monkeypatch.setattr(render_route, "resolve_route", boom)
    assert stamp_route(assembly, candidates) is assembly


def test_a_job_without_a_contract_is_left_alone() -> None:
    assembly, candidates = _job("phone")
    assembly.pop(CONTRACT_FIELD)
    assert stamp_route(assembly, candidates) is assembly
