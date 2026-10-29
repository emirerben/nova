"""KRI-1024: a flag emoji in the creator's own title failed the approved phone montage.

Plaza de Mayo M1 (prod dde04eb74, 2026-10-28 16:26Z, job 40684a25): "Curved title
"Plaza de Mayo 🇦🇷" on the palm trees". The draft was approved and the phone worker
refused it one second later: "The eligible fonts cannot draw all text. Choose another
font." No video; the creator had to drop the flag to get one.

Every new job carries a ``phone_text_style`` policy (``{"preferences": {"all_text": {}}}``
here, no font asked). With it, the plan step picks one curated phone font whose bundled
face draws every character of every text, and no bundled face has the regional-indicator
pair. The policy-free branch already exempted color emoji (the phone shapes them in the
system emoji face, the cloud renderer in Noto Color Emoji); the policy branch and the
shared role-scoped text pass did not. The pre-approval check (KRI-775) ran the same plan
without the policy, so it took the exempting branch and passed the draft.

``tests/fixtures/kri1024_plaza_m1_flag_title.json`` is that job's ``all_candidates`` and
``assembly_plan`` exactly as prod stored them. These tests replay them through the real
phone worker, the real text styling pass and the real phone recipe compiler; only the
database, storage and the two text-writing model steps are fixed values.

Negative control: a title character no phone font draws (Chinese "广场") must still be
refused, and now before the approval card, naming the text and the character.
"""

from __future__ import annotations

import contextlib
import copy
import json
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.media_sources import OriginalMediaDescriptor
from app.services.creator_render_contract import (
    CreatorRenderContract,
    CreatorRenderContractError,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri1024_plaza_m1_flag_title.json"
    ).read_text(encoding="utf-8")
)
CREATOR_ID = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")
FLAG_TITLE = "Plaza de Mayo \U0001f1e6\U0001f1f7"
# "Plaza de Mayo" in Chinese: no curated phone face has these, and they are not emoji.
UNDRAWABLE_TITLE = "Plaza de Mayo 广场"


def prod_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod settings this chat ran under (phone pilot, clip intents + facts, styling)."""
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)


def _with_title(value: object, title: str) -> object:
    if isinstance(value, dict):
        return {key: _with_title(item, title) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_title(item, title) for item in value]
    return title if value == FLAG_TITLE else value


def job_inputs(title: str = FLAG_TITLE) -> tuple[dict, dict]:
    """(assembly_plan, all_candidates) of job 40684a25, the creator's title swapped in.

    A swapped title re-seals the binding and contract digests the way approval would.
    """
    plan = copy.deepcopy(FIXTURE["assembly_plan"])
    candidates = copy.deepcopy(FIXTURE["all_candidates"])
    if title != FLAG_TITLE:
        plan, candidates = _with_title(plan, title), _with_title(candidates, title)
        raw = plan["creator_brief_binding"]
        raw["digest"] = BriefBinding._digest(
            raw["thread_id"],
            raw["state"],
            CreativeBrief.model_validate(raw["brief"]),
            raw["creator_request"],
            raw["media_snapshot"],
            [],
            [],
            None,
        )
        plan["creator_render_requirements"] = (
            CreatorRenderContract.model_validate(plan["creator_render_requirements"])
            .rebind()
            .model_dump(mode="json")
        )
    return plan, candidates


def bindings_for(media: dict) -> tuple[PhoneSourceBinding, ...]:
    return tuple(
        PhoneSourceBinding(
            media_id=row["media_id"],
            proxy_path=row["gcs_path"],
            generation=str(row["storage_generation"]),
            original=OriginalMediaDescriptor.model_validate(
                row["upload_contract"]["proxy"]["original"]
            ),
        )
        for row in media["clip_assignments"]
    )


def run_worker(monkeypatch: pytest.MonkeyPatch, title: str = FLAG_TITLE):
    """``_run_phone_unified_montage_job`` on the approved job, up to the pin.

    Returns the plan the worker pinned. Raises what the worker raises.
    """
    import app.services.clip_facts as clip_facts  # noqa: PLC0415
    import app.services.creation_text_composition as text_lane  # noqa: PLC0415
    import app.services.pipeline_trace as pipeline_trace  # noqa: PLC0415

    prod_flags(monkeypatch)
    plan, candidates = job_inputs(title)
    binding = BriefBinding.model_validate(plan["creator_brief_binding"])
    media = binding.media_snapshot
    sources = bindings_for(media)
    snapshot = {**plan, PHONE_SOURCES_FIELD: [s.model_dump(mode="json") for s in sources]}
    seen: dict = {}
    real_plan = gb._plan_phone_unified_montage

    def planned(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen["plan"] = real_plan(*args, **kwargs)
        return seen["plan"]

    def stops_at_the_pin(_db, _job_id):  # noqa: ANN001, ANN202
        seen["pinned"] = True
        return None

    monkeypatch.setattr(
        gb,
        "_load_unified_montage_inputs",
        lambda _job: (CREATOR_ID, list(media["clip_assignments"]), binding.resolve()),
    )
    monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: [])
    monkeypatch.setattr(clip_facts, "enrich_clip_facts", lambda pairs, **_k: list(pairs))
    monkeypatch.setattr(
        text_lane, "compose_creation_text", lambda plan_snapshot, **_k: plan_snapshot
    )
    monkeypatch.setattr(gb, "_plan_phone_unified_montage", planned)
    monkeypatch.setattr(gb, "_save_request_recovery", lambda *_a, **_k: True)
    monkeypatch.setattr(gb, "_sync_session", contextlib.nullcontext)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", stops_at_the_pin)
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", lambda *_a, **_k: None)
    result = gb._run_phone_unified_montage_job(
        FIXTURE["job_id"], snapshot, candidates, ownership_epoch=1
    )
    assert result is None and seen.get("pinned"), "the plan must reach the job lock"
    return seen["plan"], sources, plan["phone_text_style"]


def test_prod_job_was_refused_for_the_flag() -> None:
    """The fixture is the incident: the title carried the flag and prod refused it."""
    assert FIXTURE["refusal"] == "The eligible fonts cannot draw all text. Choose another font."
    plan, _ = job_inputs()
    assert plan["phone_text_style"] == {
        "version": 1,
        "requested": {},
        "preferences": {"all_text": {}},
    }
    texts = [row["text"] for row in plan["creator_render_requirements"]["exact_texts"]]
    assert texts == [FLAG_TITLE, "Casa Rosada", "Catedral"]


def test_worker_plans_the_flag_title_as_the_creator_wrote_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline.guided_story import compile_execution_plan  # noqa: PLC0415

    plan, _sources, _policy = run_worker(monkeypatch)
    pinned = compile_execution_plan(plan.guided_edit(), track=None)
    drawn = [row["text"] for row in pinned["text_elements"]]
    # The creator's title is the palm-trees clip's label (r5 scope clip:palms).
    assert FLAG_TITLE in drawn, drawn
    assert "Plaza de Mayo" not in drawn, "the flag must never be stripped from the words"
    assert {"Casa Rosada", "Catedral"} <= set(drawn)


def test_flag_title_styles_and_compiles_into_the_phone_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The steps after the pin: the role-scoped text pass and the phone recipe compile."""
    import app.services.phone_text_styling as styling  # noqa: PLC0415
    from app.pipeline.guided_story import (  # noqa: PLC0415
        GuidedStoryExecutionPlan,
        compile_execution_plan,
    )
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan  # noqa: PLC0415
    from app.schemas.phone_text_style import CURATED_PHONE_FONTS  # noqa: PLC0415

    plan, sources, policy = run_worker(monkeypatch)
    # The typography model step is transport: its own fallback (no pick) is replayed.
    monkeypatch.setattr(styling, "select_fonts", lambda *_a, **_k: {})
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    with styling.phone_text_policy_scope(policy):
        styled = GuidedStoryExecutionPlan.model_validate(
            styling.style_guided_generation(execution, (), job_id=FIXTURE["job_id"])
        )
    titles = [row for row in styled.text_elements if row.text == FLAG_TITLE]
    assert titles and all(row.font_family in CURATED_PHONE_FONTS for row in titles)

    recipe = compile_phone_guided_plan(styled, sources, landscape_fit="fit")
    layers = [
        layer
        for layer in recipe.text_layers
        if "".join(run.text for run in layer.runs).replace(" ", "") == FLAG_TITLE.replace(" ", "")
    ]
    assert layers, [["".join(r.text for r in layer.runs)] for layer in recipe.text_layers]
    flag_runs = [run for layer in layers for run in layer.runs if "\U0001f1e6" in run.text]
    # Shaped: the phone resolves the pair in the system emoji face, never by glyph id.
    assert flag_runs and all(run.shaped and run.glyphs is None for run in flag_runs)


def test_worker_still_refuses_text_no_phone_font_draws_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: the exemption is color emoji only, never any missing glyph."""
    with pytest.raises(CreatorRenderContractError) as refused:
        run_worker(monkeypatch, UNDRAWABLE_TITLE)
    message = str(refused.value)
    assert "广场" in message and UNDRAWABLE_TITLE in message, message
    assert "another font" not in message.casefold(), "no font can draw it: don't send them looking"


def _preflight(monkeypatch: pytest.MonkeyPatch, title: str):
    """The KRI-775 pre-approval check on the draft the creator approved at 16:26:22."""
    from app.services import phone_montage_preflight  # noqa: PLC0415

    prod_flags(monkeypatch)
    plan, candidates = job_inputs(title)
    binding = plan["creator_brief_binding"]
    brief = CreativeBrief.model_validate(binding["brief"])
    with contextlib.ExitStack() as stack:
        from app.kria.reply_language import reply_language_for  # noqa: PLC0415

        stack.enter_context(reply_language_for("en"))
        return brief, phone_montage_preflight.montage_blockers(
            creator_id=CREATOR_ID,
            edit_format=candidates["edit_format"],
            brief=brief,
            strategy=candidates["creator_strategy"],
            media_snapshot=binding["media_snapshot"],
            visuals=[],
            client_protocol=candidates["creator_client_protocol"],
        )


def test_preflight_passes_the_flag_title(monkeypatch: pytest.MonkeyPatch) -> None:
    _brief, blockers = _preflight(monkeypatch, FLAG_TITLE)
    assert blockers is not None, "the draft renders on the unified phone montage"
    assert [r.requirement_id for r in blockers if r.requirement_id == "r5"] == []


def test_preflight_names_undrawable_text_before_the_approval_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief, blockers = _preflight(monkeypatch, UNDRAWABLE_TITLE)
    assert [req.literal for req in brief.live() if req.id == "r5"] == [UNDRAWABLE_TITLE]
    by_id = {receipt.requirement_id: receipt for receipt in blockers or []}
    receipt = by_id.get("r5")
    assert receipt is not None, blockers
    assert receipt.status == "not_possible" and receipt.verification == "checked"
    assert "广场" in (receipt.reason or "") and UNDRAWABLE_TITLE in (receipt.reason or "")
