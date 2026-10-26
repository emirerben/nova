"""KRI-775: a phone montage draft the phone planner will refuse never reaches approval.

Merope café M2 (prod 6b689783a, 2026-10-21, thread EE7B4DCA): twice the draft and the
approval card went out ("I will cut the 6 exact moments in order ..."), the creator
approved, and ten seconds later ``_run_phone_unified_montage_job`` refused the render
(``phone_plan_unsupported``): "This draft is about 4.7s; you asked for 14s." That verdict
was knowable before approval: it is a deterministic plan over the very brief, strategy and
media snapshot approval pins. ``app.services.phone_montage_preflight`` runs that plan before
the approval card; the runtime then holds the draft back with the reason and the existing
"different approach, or this simpler version?" choice (runtime half:
``test_kri775_preflight_runtime_postgres.py``).

The ground truth is the worker itself on prod's own inputs
(``test_kri775_merope_m2_replay.py`` proves it judges them with prod's words; since KRI-129
the worker renders the approved draft and notes those misses instead of refusing it).

Ways this can fail, each pinned below:
1. The check passes a draft the worker refuses (both M2 renders).
2. It holds back a draft the worker builds (the "about 5 seconds" variant).
3. Its reason differs from the worker's, or is English in a Turkish chat.
4. It judges a text ask, which only the render's model-written text lane and landmark names
   settle: a false hold-back.
5. It runs where this planner is not the renderer: a cloud render, a voiceover lane, a creator
   song, the voice-behind-footage route, a Talking edit, an account with no brief binding.
6. It calls a model or the storage service (it must stay cheap and side-effect free).
7. An error inside it blocks the draft: it must fail open and log.
8. With the kill switch off it never plans.
9. An open length choice (the gate's question) is turned into a second hold-back.
"""

from __future__ import annotations

import copy

import pytest
from structlog.testing import capture_logs

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import reply_language_for
from app.pipeline import unified_montage
from app.services import phone_montage_preflight as preflight
from tests.kria.test_kri775_merope_m2_replay import (
    CREATOR_ID,
    RENDERS,
    TAKE,
    approved_media,
    m2_brief,
    m2_visual,
    prod_flags,
    run_worker,
)

CLOUD_PATH = "users/u/creation-threads/t/original-take.mp4"


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


def _blockers(
    render: dict,
    *,
    brief: CreativeBrief | None = None,
    media: dict | None = None,
    strategy: dict | None = None,
    edit_format: str = "montage",
):
    return preflight.montage_blockers(
        creator_id=CREATOR_ID,
        edit_format=edit_format,
        brief=brief if brief is not None else m2_brief(render),
        strategy=copy.deepcopy(strategy if strategy is not None else render["strategy"]),
        media_snapshot=media if media is not None else approved_media(render),
        visuals=[m2_visual()],
    )


def _rows(receipts) -> dict[str, tuple[str, str | None]]:  # noqa: ANN001
    return {
        (r.requirement_id if hasattr(r, "requirement_id") else r["requirement_id"]): (
            r.status if hasattr(r, "status") else r["status"],
            r.reason if hasattr(r, "reason") else r["reason"],
        )
        for r in receipts
    }


def _judged_kinds(brief: CreativeBrief) -> dict[str, str]:
    return {req.id: req.kind for req in brief.live() if req.kind in preflight.PREFLIGHT_KINDS}


# -- 1, 3: it holds back exactly what the worker refuses, in the worker's words ----------------


@pytest.mark.parametrize("index", [0, 1], ids=["job-417eecd4", "job-50f67a06"])
def test_it_holds_back_exactly_what_the_phone_worker_refuses(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    render = RENDERS[index]
    worker = run_worker(monkeypatch, render)
    # KRI-129: the worker now renders an approved draft and notes the misses instead of
    # refusing it; the preflight still asks before approval about exactly those misses.
    assert worker.refusal is None
    assert "This draft is about 4.7s; you asked for 14s." in worker.notes
    kinds = _judged_kinds(m2_brief(render))
    expected = {k: v for k, v in _rows(worker.failures).items() if k in kinds}
    blockers = _blockers(render)
    assert _rows(blockers) == expected
    assert "r7" in expected and expected["r7"][1] == "This draft is about 4.7s; you asked for 14s."
    for receipt in blockers:
        assert receipt.verification == "checked" and receipt.status != "met"


def test_the_turkish_chat_gets_the_workers_turkish_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    render = RENDERS[1]
    worker = run_worker(monkeypatch, render, reply_language="tr")
    with reply_language_for("tr"):
        blockers = _blockers(render)
    kinds = _judged_kinds(m2_brief(render))
    assert _rows(blockers) == {k: v for k, v in _rows(worker.failures).items() if k in kinds}
    assert all("you asked" not in (r.reason or "") for r in blockers)


# -- 2: a draft the worker builds goes to approval untouched -------------------------------------


def _five_second_brief(render: dict) -> CreativeBrief:
    """The M2 brief without the order list and the freeze, asking for about 5 seconds."""
    brief = m2_brief(render)
    kept = []
    for req in brief.requirements:
        if req.id in {"r1", "r3"}:
            continue
        if req.id == "r7":
            req = req.model_copy(
                update={"description": "About 5 seconds", "facts": {"duration_s": 5}}
            )
        kept.append(req)
    return brief.model_copy(update={"requirements": kept})


def test_a_draft_the_worker_builds_is_not_held_back(monkeypatch: pytest.MonkeyPatch) -> None:
    render = RENDERS[0]
    brief = _five_second_brief(render)
    worker = run_worker(monkeypatch, render, brief=brief)
    assert worker.refusal is None
    assert not [row for row in worker.failures if row["requirement_id"] in {"r1", "r7"}]
    assert _blockers(render, brief=brief) == []


# -- 4: text asks stay with the render -----------------------------------------------------------


def test_a_text_ask_the_plan_misses_is_left_to_the_render(monkeypatch: pytest.MonkeyPatch) -> None:
    render = RENDERS[0]
    brief = _five_second_brief(render)
    # A title the strategy never wrote: the deterministic plan alone fails it, the render's
    # text lane may still write it, so only the render judges it.
    brief = brief.model_copy(
        update={
            "requirements": [
                req.model_copy(update={"literal": "Oblea at Merope"}) if req.id == "r5" else req
                for req in brief.requirements
            ]
        }
    )
    worker = run_worker(monkeypatch, render, brief=brief)
    assert any(row["requirement_id"] == "r5" for row in worker.failures), worker.receipts
    assert _blockers(render, brief=brief) == []


# -- 5: only where the unified phone montage renders ----------------------------------------


def _never_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):  # noqa: ANN202
        raise AssertionError("the preflight planned a draft it does not own")

    monkeypatch.setattr(unified_montage, "plan_unified_montage", boom)


@pytest.mark.parametrize(
    "case",
    ["cloud", "voiceover", "song", "talking", "no_binding", "voice_behind_footage", "no_brief"],
)
def test_it_only_runs_where_the_unified_phone_montage_renders(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    render = RENDERS[0]
    media = approved_media(render)
    strategy = copy.deepcopy(render["strategy"])
    brief: CreativeBrief | None = m2_brief(render)
    edit_format = "montage"
    if case == "cloud":
        for key in ("clip_paths",):
            media[key] = [CLOUD_PATH]
        media["clip_assignments"][0]["gcs_path"] = CLOUD_PATH
    elif case == "voiceover":
        strategy["audio_strategy"] = "voiceover"
        media["voiceover_path"] = "users/u/voiceover.m4a"
    elif case == "song":
        media["song_path"] = "users/u/song.m4a"
    elif case == "talking":
        strategy["edit_format"] = edit_format = "subtitled"
    elif case == "no_binding":
        monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    elif case == "voice_behind_footage":
        strategy["voice_mode"] = "continuous"
        strategy["montage_audio"] = {
            "preserve_source_audio": True,
            "source_media_ids": [TAKE],
            "preview_source_beds": False,
        }
    else:
        brief = None
    _never_plans(monkeypatch)
    assert (
        preflight.montage_blockers(
            creator_id=CREATOR_ID,
            edit_format=edit_format,
            brief=brief,
            strategy=strategy,
            media_snapshot=media,
            visuals=[m2_visual()],
        )
        is None
    )


# -- 6: no model, no storage ---------------------------------------------------------------------


def test_it_never_calls_a_model_or_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.clip_facts as clip_facts  # noqa: PLC0415
    import app.services.creation_text_composition as text_lane  # noqa: PLC0415
    from app import storage  # noqa: PLC0415
    from app.agents import _runtime  # noqa: PLC0415

    def forbidden(*_a, **_k):  # noqa: ANN202
        raise AssertionError("the preflight must stay deterministic and local")

    monkeypatch.setattr(clip_facts, "enrich_clip_facts", forbidden)
    monkeypatch.setattr(text_lane, "compose_creation_text", forbidden)
    monkeypatch.setattr(storage, "object_metadata", forbidden)
    monkeypatch.setattr(_runtime, "run_agent", forbidden, raising=False)
    held = {r.requirement_id for r in _blockers(RENDERS[0])}
    assert "r7" in held and held <= {"r1", "r3", "r7"}, held


# -- 7, 8: fail open, kill switch -----------------------------------------------------------------


class _Item:
    id = "9fecc54d-16dd-474a-95b4-6df626baaade"
    edit_format = "montage"


def test_an_error_fails_open_and_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    render = RENDERS[0]

    def broken(*_a, **_k):  # noqa: ANN202
        raise RuntimeError("planner bug")

    monkeypatch.setattr(unified_montage, "plan_unified_montage", broken)
    monkeypatch.setattr(preflight, "_ready_visuals", lambda *_a, **_k: [m2_visual()])
    with capture_logs() as logs:
        result = preflight.phone_montage_blockers(
            None,
            creator_id=CREATOR_ID,
            item=_Item(),
            brief=m2_brief(render),
            strategy=copy.deepcopy(render["strategy"]),
            media_snapshot=approved_media(render),
        )
    assert result == []
    assert any(row["event"] == "kria_phone_montage_preflight_failed" for row in logs), logs


def test_the_kill_switch_never_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    render = RENDERS[0]
    monkeypatch.setattr(settings, "kria_phone_montage_preflight_enabled", False)
    _never_plans(monkeypatch)
    monkeypatch.setattr(preflight, "_ready_visuals", lambda *_a, **_k: [m2_visual()])
    assert (
        preflight.phone_montage_blockers(
            None,
            creator_id=CREATOR_ID,
            item=_Item(),
            brief=m2_brief(render),
            strategy=copy.deepcopy(render["strategy"]),
            media_snapshot=approved_media(render),
        )
        == []
    )


def test_a_held_back_draft_is_logged_with_what_held_it(monkeypatch: pytest.MonkeyPatch) -> None:
    render = RENDERS[0]
    monkeypatch.setattr(preflight, "_ready_visuals", lambda *_a, **_k: [m2_visual()])
    with capture_logs() as logs:
        result = preflight.phone_montage_blockers(
            None,
            creator_id=CREATOR_ID,
            item=_Item(),
            brief=m2_brief(render),
            strategy=copy.deepcopy(render["strategy"]),
            media_snapshot=approved_media(render),
        )
    held = [r.requirement_id for r in result]
    assert "r7" in held, held
    [row] = [row for row in logs if row["event"] == "kria_phone_montage_preflight"]
    assert row["blocked_requirement_ids"] == held
    assert isinstance(row["elapsed_ms"], int)


# -- 9: an open length choice belongs to the gate ------------------------------------------------


def test_an_open_length_choice_is_the_gates_question_not_a_hold_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = RENDERS[0]
    plan = copy.deepcopy(render["prod_length_plan"])
    plan["conflicts"] = [{"code": "duration_exceeds_capacity", "message": "too long"}]
    _never_plans(monkeypatch)
    assert _blockers(render, media=approved_media(render, length_plan=plan)) is None


def test_a_brief_with_nothing_to_judge_here_is_not_planned(monkeypatch: pytest.MonkeyPatch) -> None:
    render = RENDERS[0]
    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="r5", kind="text", scope="title", literal="Oblea")],
    )
    _never_plans(monkeypatch)
    assert _blockers(render, brief=brief) is None
