"""Task-body tests for the overlay auto-placement matcher (review C2).

`_sync_session`-mock style, mirroring test_sfx_overlay_two_pass.py: a fake sync
session with __enter__/__exit__/get/execute/commit that always returns one shared
Job mock; flag_modified neutralised; the module-level symbols patched at their
source. These pin the state-machine transitions of
`match_overlay_suggestions(job_id, variant_id, user_id, auto_apply=False)` that the
feature's failure design depends on — the persisted `overlay_suggest_status` and
the run-once transcript key (`overlay_transcript`, NOT `transcript` — review C19).

`analyze_pool_asset` uses mocked media I/O here to pin its ownership/epoch fence
and image/video persistence transitions without downloading real footage.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.tasks.autoplace as ap
from app.services.content_plan_persona import require_plan_persona_owned

JOB_ID = "11111111-1111-1111-1111-111111111111"
VARIANT_ID = "original_text"
USER_ID = "22222222-2222-2222-2222-222222222222"


def _variant(**over) -> dict:
    v = {
        "variant_id": VARIANT_ID,
        "music_track_id": None,
        "output_duration_s": 12.0,
        "media_overlays": None,
    }
    v.update(over)
    return v


class _Job:
    def __init__(self, variant: dict):
        self.id = uuid.UUID(JOB_ID)
        self.status = "processing"
        self.content_plan_item_id = uuid.uuid4()
        self.assembly_plan = {"variants": [variant]}


class _Asset:
    def __init__(self, *, kind="image", analysis=None):
        self.id = uuid.uuid4()
        self.gcs_path = f"users/u/plan/i/pool/{self.id}.png"
        self.kind = kind
        self.source_filename = "x.png"
        self.duration_s = None
        self.aspect = 1.0
        # Current-version stub-shaped analysis so analysis_is_stale() never triggers the
        # background backfill dispatch (which would need a real broker).
        self.analysis = (
            analysis
            if analysis is not None
            else {
                "subject": "a dog",
                "source": "stub",
                "analysis_version": ap.ANALYSIS_VERSION,
            }
        )


class _Result:
    def __init__(self, rows: list):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Sess:
    """One shared job + a fixed asset list for every `select(PlanItemAsset)`.

    `on_locked_get` fires on each row-locked `db.get(Job, ..., with_for_update=True)`
    with the 1-based lock index, so a test can inject a concurrent mutation at the
    exact re-read boundary (e.g. clear suggestions right before auto-apply)."""

    def __init__(self, job: _Job, assets: list, *, state: dict, on_locked_get=None):
        self.job = job
        self.assets = assets
        self.commits = 0
        # `state` is shared across every session the task opens (each `with
        # _sync_session()` builds a fresh _Sess) so lock ordering is global.
        self._state = state
        self._on_locked_get = on_locked_get

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, model, pk, **kw):
        if kw.get("with_for_update"):
            self._state["locked_gets"] = self._state.get("locked_gets", 0) + 1
            if self._on_locked_get is not None:
                self._on_locked_get(self._state["locked_gets"])
        return self.job

    def execute(self, *a, **kw):
        # The matcher issues select(PlanItemAsset) (assets) and select(SoundEffect)
        # via _load_glossary. Route the asset select to our fixed list and the
        # glossary select to [] so no SoundEffect attrs are ever read.
        stmt = str(a[0]) if a else ""
        if "plan_item_assets" in stmt or "PlanItemAsset" in stmt:
            return _Result(self.assets)
        return _Result([])

    def commit(self):
        self.commits += 1


def _patch_common(monkeypatch, job, assets, *, gemini_key=None, on_locked_get=None):
    # The task imports `settings` locally (from app.config import settings), so
    # patch the shared settings singleton's attributes rather than a module ref.
    from app.config import settings as _settings

    state: dict = {}
    monkeypatch.setattr(
        ap, "_sync_session", lambda: _Sess(job, assets, state=state, on_locked_get=on_locked_get)
    )
    monkeypatch.setattr(_settings, "gemini_api_key", gemini_key, raising=False)
    monkeypatch.setattr(_settings, "autoplace_queue", "autoplace-jobs", raising=False)
    monkeypatch.setattr(_settings, "fullscreen_cutaways_enabled", False, raising=False)
    # pipeline_trace_for is a context manager used to wrap the whole body.
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda *a, **k: _NullCtx()
    )
    # _record's no-op patch MUST accept (stage, event, data=None) — review note.
    monkeypatch.setattr(
        "app.services.pipeline_trace.record_pipeline_event",
        lambda stage, event, data=None: None,
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda obj, key: None)


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PoolAsset:
    def __init__(self, *, kind: str = "image"):
        self.id = uuid.uuid4()
        self.plan_item_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.gcs_path = f"users/u/plan/i/pool/{self.id}.png"
        self.kind = kind
        self.source_filename = "alpha-card.png"
        self.duration_s = None
        self.aspect = None
        self.analysis = None
        self.status = "uploaded"
        self.error_code = None
        self.error_detail = None
        self.error_retryable = False
        self.analysis_attempt_token = None
        self.analysis_attempt_count = 0
        self.analysis_last_dispatched_at = None
        self.analysis_started_at = None


class _AnalyzeSess:
    def __init__(self, asset: _PoolAsset, item, plan):  # noqa: ANN001
        self.asset = asset
        self.item = item
        self.plan = plan
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, model, _pk, **_kw):
        from app.models import ContentPlan, PlanItem, PlanItemAsset

        return {
            ContentPlan: self.plan,
            PlanItem: self.item,
            PlanItemAsset: self.asset,
        }.get(model)

    def commit(self):
        self.commits += 1


def _patch_analyze_pool_common(
    monkeypatch,
    asset: _PoolAsset,
    *,
    gemini_key: str | None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    from PIL import Image

    from app.config import settings as _settings

    item = SimpleNamespace(id=asset.plan_item_id, content_plan_id=uuid.uuid4())
    plan = SimpleNamespace(
        id=item.content_plan_id,
        user_id=asset.user_id,
        persona_id=uuid.uuid4(),
        ownership_epoch=0,
        ownership_quarantined_at=None,
    )
    persona = SimpleNamespace(id=plan.persona_id, user_id=plan.user_id)
    monkeypatch.setattr(ap, "_sync_session", lambda: _AnalyzeSess(asset, item, plan))
    monkeypatch.setattr(
        ap,
        "load_owned_plan_persona_sync",
        lambda _db, current_plan, *, for_update=False: require_plan_persona_owned(
            current_plan, persona
        ),
    )
    monkeypatch.setattr(_settings, "gemini_api_key", gemini_key, raising=False)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda *a, **k: _NullCtx()
    )
    monkeypatch.setattr(
        "app.services.pipeline_trace.record_pipeline_event",
        lambda stage, event, data=None: None,
    )

    def _download(_gcs_path: str, local_path: str) -> None:
        Image.new("RGBA", (4, 2), (255, 0, 0, 128)).save(local_path)

    monkeypatch.setattr("app.storage.download_to_file", _download)
    monkeypatch.setattr("app.pipeline.image_clip.image_has_alpha", lambda _path: True)
    return plan, persona


def _variant_now(job: _Job) -> dict:
    return job.assembly_plan["variants"][0]


def test_cancelled_matcher_exits_before_transcript_or_agent(monkeypatch):
    job = _Job(_variant())
    job.status = "cancelled"
    before = dict(_variant_now(job))
    _patch_common(monkeypatch, job, [_Asset()], gemini_key="k")
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("cancelled transcript read")),
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.heuristic_match",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("cancelled matcher call")),
    )

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID, auto_apply=True)

    assert _variant_now(job) == before


def test_cancellation_during_match_rejects_final_locked_write(monkeypatch):
    job = _Job(_variant())
    assets = [_Asset()]

    def _cancel_before_result_persist(lock_idx: int) -> None:
        if lock_idx == 2:
            job.status = "cancelled"

    _patch_common(
        monkeypatch,
        job,
        assets,
        gemini_key=None,
        on_locked_get=_cancel_before_result_persist,
    )
    words = [{"word": "hello", "start_s": 0.0, "end_s": 0.5}]
    monkeypatch.setattr("app.services.transcript_source.words_from_variant", lambda _v: words)
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kw: (words, "hash-cancelled"),
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.heuristic_match",
        lambda *_a, **_kw: [{"asset_id": str(assets[0].id)}],
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.build_suggestions",
        lambda _raw, **_kw: [{"id": "must-not-persist"}],
    )

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID)

    variant = _variant_now(job)
    assert job.status == "cancelled"
    assert variant["overlay_suggest_status"] == "matching"
    assert "overlay_suggestions" not in variant
    assert "overlay_suggest_hash" not in variant


def test_superseded_match_attempt_rejects_late_locked_write(monkeypatch):
    attempt = "attempt-a"
    job = _Job(_variant(overlay_suggest_attempt_token=attempt))
    assets = [_Asset()]

    def _supersede_before_result_persist(lock_idx: int) -> None:
        if lock_idx == 2:
            _variant_now(job)["overlay_suggest_attempt_token"] = "attempt-b"

    _patch_common(
        monkeypatch,
        job,
        assets,
        gemini_key=None,
        on_locked_get=_supersede_before_result_persist,
    )
    words = [{"word": "hello", "start_s": 0.0, "end_s": 0.5}]
    monkeypatch.setattr("app.services.transcript_source.words_from_variant", lambda _v: words)
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kw: (words, "hash-stale"),
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.heuristic_match",
        lambda *_a, **_kw: [{"asset_id": str(assets[0].id)}],
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.build_suggestions",
        lambda _raw, **_kw: [{"id": "must-not-persist"}],
    )

    ap.match_overlay_suggestions(
        JOB_ID,
        VARIANT_ID,
        USER_ID,
        attempt_token=attempt,
    )

    variant = _variant_now(job)
    assert variant["overlay_suggest_attempt_token"] == "attempt-b"
    assert variant["overlay_suggest_status"] == "matching"
    assert "overlay_suggestions" not in variant
    assert "overlay_suggest_hash" not in variant


def test_cancelled_sfx_autoplace_exits_before_glossary_or_agent(monkeypatch):
    from app.config import settings as _settings

    job = _Job(_variant())
    job.status = "cancelled"
    before = dict(_variant_now(job))
    _patch_common(monkeypatch, job, [], gemini_key="k")
    monkeypatch.setattr(_settings, "sfx_autoplace_enabled", True, raising=False)
    glossary_calls: list[bool] = []
    monkeypatch.setattr(ap, "_load_glossary", lambda _db: glossary_calls.append(True) or [])

    ap.autoplace_sfx_suggestions(JOB_ID, VARIANT_ID)

    assert glossary_calls == []
    assert _variant_now(job) == before


# ── analyze_pool_asset image alpha persistence ────────────────────────────────


def test_pool_asset_fence_locks_plan_persona_item_asset_in_order(monkeypatch) -> None:
    from app.models import ContentPlan, PlanItem, PlanItemAsset

    plan = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        persona_id=uuid.uuid4(),
        ownership_epoch=3,
        ownership_quarantined_at=None,
    )
    item = SimpleNamespace(id=uuid.uuid4(), content_plan_id=plan.id)
    asset = SimpleNamespace(id=uuid.uuid4(), plan_item_id=item.id, user_id=plan.user_id)
    events: list[str] = []

    class _FenceSession:
        def get(self, model, _pk, **kwargs):  # noqa: ANN001
            assert kwargs == {"with_for_update": True}
            events.append(model.__name__)
            return {ContentPlan: plan, PlanItem: item, PlanItemAsset: asset}[model]

    monkeypatch.setattr(
        ap,
        "load_owned_plan_persona_sync",
        lambda *_a, **_kw: events.append("Persona") or object(),
    )

    assert ap._lock_owned_pool_asset(
        _FenceSession(),
        plan_id=plan.id,
        item_id=item.id,
        asset_id=asset.id,
        expected_epoch=3,
    ) == (plan, item, asset)
    assert events == ["ContentPlan", "Persona", "PlanItem", "PlanItemAsset"]


@pytest.mark.parametrize("fence", ["mismatch", "quarantine"])
def test_analyze_pool_asset_invalid_owner_exits_before_download_or_agent(
    monkeypatch,
    fence: str,
) -> None:
    asset = _PoolAsset(kind="image")
    plan, persona = _patch_analyze_pool_common(monkeypatch, asset, gemini_key="gemini-key")
    if fence == "mismatch":
        persona.user_id = uuid.uuid4()
    else:
        plan.ownership_quarantined_at = object()
    downloads: list[str] = []
    analyses: list[str] = []
    monkeypatch.setattr(
        "app.storage.download_to_file",
        lambda path, _local: downloads.append(path),
    )
    monkeypatch.setattr(
        ap,
        "_analyze_image",
        lambda *_a, **_kw: analyses.append("called") or (None, None, None, None),
    )

    ap.analyze_pool_asset.run(str(asset.id))

    assert downloads == []
    assert analyses == []
    assert asset.status == "uploaded"
    assert asset.analysis is None


def test_analyze_pool_asset_rejects_stale_attempt_token(monkeypatch) -> None:
    asset = _PoolAsset(kind="image")
    asset.status = "queued"
    asset.analysis_attempt_token = "current-attempt"
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key="gemini-key")
    downloads: list[str] = []
    monkeypatch.setattr(
        "app.storage.download_to_file",
        lambda path, _local: downloads.append(path),
    )

    ap.analyze_pool_asset.run(str(asset.id), False, "stale-attempt")

    assert downloads == []
    assert asset.status == "queued"


def test_analyze_pool_asset_persists_safe_retryable_failure(monkeypatch) -> None:
    asset = _PoolAsset(kind="image")
    asset.status = "queued"
    asset.analysis_attempt_token = "attempt-1"
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key="gemini-key")
    monkeypatch.setattr(
        "app.storage.download_to_file",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("private provider detail")),
    )

    ap.analyze_pool_asset.run(str(asset.id), False, "attempt-1")

    assert asset.status == "failed"
    assert asset.error_code == "analysis_temporarily_unavailable"
    assert asset.error_retryable is True
    assert "private provider detail" not in asset.error_detail


def test_analyze_pool_asset_downloads_verified_generation(monkeypatch) -> None:
    asset = _PoolAsset(kind="image")
    asset.status = "queued"
    asset.analysis_attempt_token = "attempt-1"
    asset.gcs_generation = "42"
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key=None)
    verified = MagicMock()
    legacy = MagicMock()
    monkeypatch.setattr("app.storage.download_generation_to_file", verified)
    monkeypatch.setattr("app.storage.download_to_file", legacy)
    monkeypatch.setattr(ap, "_analyze_image", lambda *_a: (None, 1.0, (100, 100), False))

    ap.analyze_pool_asset.run(str(asset.id), False, "attempt-1")

    verified.assert_called_once()
    assert verified.call_args.kwargs["generation"] == "42"
    legacy.assert_not_called()


def test_analyze_pool_asset_persists_timeout_then_propagates(monkeypatch) -> None:
    asset = _PoolAsset(kind="image")
    asset.status = "queued"
    asset.analysis_attempt_token = "attempt-1"
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key="gemini-key")
    monkeypatch.setattr("app.storage.download_to_file", lambda *_a: None)
    monkeypatch.setattr(
        ap,
        "_analyze_image",
        lambda *_a: (_ for _ in ()).throw(ap.SoftTimeLimitExceeded()),
    )

    with pytest.raises(ap.SoftTimeLimitExceeded):
        ap.analyze_pool_asset.run(str(asset.id), False, "attempt-1")

    assert asset.status == "failed"
    assert asset.error_code == "analysis_timed_out"
    assert asset.error_retryable is True


def test_analyze_pool_asset_marks_unreadable_nonretryable(monkeypatch) -> None:
    asset = _PoolAsset(kind="image")
    asset.status = "queued"
    asset.analysis_attempt_token = "attempt-1"
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key="gemini-key")
    monkeypatch.setattr("app.storage.download_to_file", lambda *_a: None)
    monkeypatch.setattr(
        ap,
        "_analyze_image",
        lambda *_a: (_ for _ in ()).throw(ap.AssetUnreadableError()),
    )

    ap.analyze_pool_asset.run(str(asset.id), False, "attempt-1")

    assert asset.status == "failed"
    assert asset.error_code == "analysis_unreadable"
    assert asset.error_retryable is False
    assert "Export it as" in asset.error_detail


@pytest.mark.parametrize("fence_change", ["epoch", "quarantine"])
def test_analyze_pool_asset_discards_output_when_fence_changes_during_analysis(
    monkeypatch,
    fence_change: str,
) -> None:
    asset = _PoolAsset(kind="image")
    asset.status = "ready"
    asset.analysis = {"subject": "old analysis"}
    plan, _persona = _patch_analyze_pool_common(monkeypatch, asset, gemini_key=None)

    def _pause_then_finish(*_args, **_kwargs):
        if fence_change == "epoch":
            plan.ownership_epoch += 1
        else:
            plan.ownership_quarantined_at = object()
        return ({"subject": "must not persist"}, 1.25, (100, 80), False)

    monkeypatch.setattr(ap, "_analyze_image", _pause_then_finish)

    ap.analyze_pool_asset.run(str(asset.id), refresh=True)

    assert asset.status == "ready"
    assert asset.analysis == {"subject": "old analysis"}
    assert asset.aspect is None


def test_analyze_pool_asset_image_gemini_success_persists_has_alpha(monkeypatch):
    asset = _PoolAsset(kind="image")
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key="gemini-key")

    class _Models:
        def generate_content(self, **_kwargs):
            return type(
                "Resp",
                (),
                {
                    "text": json.dumps(
                        {
                            "subject": "alpha card",
                            "description": "transparent sticker",
                            "on_screen_text": "",
                            "brands": ["Arçelik", "Çelik robot mascot"],
                            "kind_hint": "photo",
                        }
                    )
                },
            )()

    class _Client:
        models = _Models()

    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer._get_client", lambda: _Client())
    monkeypatch.setattr("app.pipeline.prompt_loader.load_prompt", lambda _name: "prompt")

    ap.analyze_pool_asset(str(asset.id))

    assert asset.status == "ready"
    assert asset.analysis["source"] == "image_metadata"
    assert asset.analysis["has_alpha"] is True
    assert asset.analysis["brands"] == ["Arçelik", "Çelik robot mascot"]
    assert asset.analysis["width"] == 4
    assert asset.analysis["height"] == 2


def test_analyze_pool_asset_image_no_gemini_key_persists_has_alpha_on_stub(monkeypatch):
    asset = _PoolAsset(kind="image")
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key=None)

    ap.analyze_pool_asset(str(asset.id))

    assert asset.status == "ready"
    assert asset.analysis["source"] == "stub"
    assert asset.analysis["has_alpha"] is True
    assert asset.analysis["width"] == 4
    assert asset.analysis["height"] == 2


def test_analyze_pool_asset_video_persists_brands(monkeypatch):
    asset = _PoolAsset(kind="video")
    _patch_analyze_pool_common(monkeypatch, asset, gemini_key="gemini-key")
    monkeypatch.setattr(
        ap,
        "_analyze_video",
        lambda *_a, **_kw: (
            {
                "subject": "appliance ad",
                "description": "Arçelik spot with a robot mascot",
                "on_screen_text": "",
                "brands": ["Arçelik", "Çelik robot mascot"],
                "source": "clip_metadata",
                "analysis_version": ap.ANALYSIS_VERSION,
            },
            1.7778,
            6.0,
            (1920, 1080),
        ),
    )

    ap.analyze_pool_asset(str(asset.id))

    assert asset.status == "ready"
    assert asset.analysis["brands"] == ["Arçelik", "Çelik robot mascot"]
    assert asset.analysis["width"] == 1920
    assert asset.analysis["height"] == 1080


# ── no transcript → failed ────────────────────────────────────────────────────


def test_no_transcript_persists_failed(monkeypatch):
    """Assets present but transcript_source returns None → status 'failed'."""
    job = _Job(_variant())
    assets = [_Asset()]
    _patch_common(monkeypatch, job, assets)
    monkeypatch.setattr("app.services.transcript_source.words_from_variant", lambda v: None)
    monkeypatch.setattr("app.services.transcript_source.transcript_source", lambda v, **kw: None)

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID)

    assert _variant_now(job)["overlay_suggest_status"] == "failed"


# ── zero ready assets mid-flight → zero ───────────────────────────────────────


def test_zero_ready_assets_persists_zero(monkeypatch):
    """Assets vanished between the route gate and the task read → status 'zero',
    overlay_suggestions None; the matcher never runs."""
    job = _Job(_variant())
    _patch_common(monkeypatch, job, assets=[])
    # Should short-circuit before any transcript lookup, but stub to be safe.
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda v, **kw: (_ for _ in ()).throw(AssertionError("transcript read after zero-asset")),
    )

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID)

    v = _variant_now(job)
    assert v["overlay_suggest_status"] == "zero"
    assert v["overlay_suggestions"] is None


# ── agent raises but heuristic returns placements → ready ─────────────────────


def test_agent_failure_falls_back_to_heuristic_ready(monkeypatch):
    """gemini key present, the agent import/run raises → heuristic_match runs and
    build_suggestions yields ≥1 → status 'ready' via the deterministic path."""
    job = _Job(_variant())
    assets = [_Asset()]
    _patch_common(monkeypatch, job, assets, gemini_key="k")

    words = [{"word": "hello", "start_s": 0.0, "end_s": 0.5}]
    monkeypatch.setattr("app.services.transcript_source.words_from_variant", lambda v: words)
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda v, **kw: (words, "hash-abc"),
    )
    # Force the agent branch to blow up (import inside the try). Patching the
    # symbol the branch imports raises at call time → caught → matcher=heuristic.
    import app.agents.overlay_placement as opa

    def _boom(*a, **kw):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(opa, "OverlayPlacementAgent", _boom)

    heur_calls: list = []
    monkeypatch.setattr(
        "app.services.overlay_autoplace.heuristic_match",
        lambda *a, **kw: heur_calls.append((a, kw)) or [{"asset_id": str(assets[0].id)}],
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.build_suggestions",
        lambda raw, **kw: [{"id": "sug-1"}],
    )

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID)

    assert heur_calls, "heuristic_match must run after the agent fails"
    v = _variant_now(job)
    assert v["overlay_suggest_status"] == "ready"
    assert v["overlay_suggestions"] == [{"id": "sug-1"}]
    assert v["overlay_suggest_hash"] == "hash-abc"


# ── Whisper ran → run-once transcript persisted under overlay_transcript ───────


def test_whisper_run_persists_overlay_transcript_key(monkeypatch):
    """had_persisted_words False (words_from_variant None) but transcript_source
    yields words (Whisper) → they are persisted to variants[i]['overlay_transcript'],
    NOT 'transcript' (review C19 cross-feature-collision guard)."""
    job = _Job(_variant())
    assets = [_Asset()]
    _patch_common(monkeypatch, job, assets, gemini_key=None)  # heuristic-only

    words = [{"word": "hi", "start_s": 0.0, "end_s": 0.4}]
    monkeypatch.setattr("app.services.transcript_source.words_from_variant", lambda v: None)
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda v, **kw: (words, "hash-w"),
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.heuristic_match",
        lambda *a, **kw: [{"asset_id": str(assets[0].id)}],
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.build_suggestions",
        lambda raw, **kw: [{"id": "sug-1"}],
    )

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID)

    v = _variant_now(job)
    assert v["overlay_transcript"] == words
    assert "transcript" not in v  # never the cross-feature key
    assert v["overlay_suggest_status"] == "ready"


# ── auto_apply=True but suggestions cleared concurrently → skipped ────────────


def test_auto_apply_skipped_when_suggestions_gone(monkeypatch):
    """auto_apply=True; suggestions built, but a concurrent dismiss clears them
    between persist and the fresh re-read → apply helper is NEVER called."""
    job = _Job(_variant())
    assets = [_Asset()]

    # Lock order: (1) persist "matching", (2) persist the suggestion set,
    # (3) the auto-apply re-read. A concurrent dismiss lands right before (3):
    # clear overlay_suggestions on the 3rd row-locked get so the fresh read is empty.
    def _clear_before_auto_apply(lock_idx: int):
        if lock_idx == 3:
            _variant_now(job)["overlay_suggestions"] = None

    _patch_common(monkeypatch, job, assets, gemini_key=None, on_locked_get=_clear_before_auto_apply)

    words = [{"word": "go", "start_s": 0.0, "end_s": 0.3}]
    monkeypatch.setattr("app.services.transcript_source.words_from_variant", lambda v: words)
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda v, **kw: (words, "hash-a"),
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.heuristic_match",
        lambda *a, **kw: [{"asset_id": str(assets[0].id)}],
    )
    monkeypatch.setattr(
        "app.services.overlay_autoplace.build_suggestions",
        lambda raw, **kw: [{"id": "sug-1"}],
    )

    apply_calls: list = []
    monkeypatch.setattr(
        "app.services.overlay_apply.apply_suggestions_to_variant",
        lambda *a, **kw: apply_calls.append((a, kw)) or {"applied": 0, "dropped": 0, "sfx": 0},
    )

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID, auto_apply=True)

    assert apply_calls == [], "apply helper must NOT run when suggestions were cleared"


# ── build_suggestions trace events flush AFTER the row lock releases ───────────


def test_build_suggestions_trace_deferred_until_after_persist_commit(monkeypatch):
    """Regression (2026-07-07 localhost E2E): build_suggestions' trace callback
    used to call record_pipeline_event while the persist session held the jobs
    row FOR UPDATE. record_pipeline_event opens its OWN connection and UPDATEs
    the same jobs row → the worker self-deadlocked on EVERY matcher run that
    produced ≥1 placement (zero-placement runs emit no trace events and sailed
    through — which is how this shipped). The trace events must flush only
    after the locked persist commits."""
    job = _Job(_variant())
    assets = [_Asset()]
    _patch_common(monkeypatch, job, assets, gemini_key="k")

    timeline: list[str] = []
    state: dict = {}

    class _TimelineSess(_Sess):
        def commit(self):
            super().commit()
            timeline.append("commit")

    monkeypatch.setattr(ap, "_sync_session", lambda: _TimelineSess(job, assets, state=state))
    monkeypatch.setattr(
        "app.services.pipeline_trace.record_pipeline_event",
        lambda stage, event, data=None: timeline.append(f"trace:{event}"),
    )

    words = [{"word": "hello", "start_s": 0.0, "end_s": 0.5}]
    monkeypatch.setattr("app.services.transcript_source.words_from_variant", lambda v: words)
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda v, **kw: (words, "hash-abc"),
    )
    import app.agents.overlay_placement as opa

    def _boom(*a, **kw):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(opa, "OverlayPlacementAgent", _boom)
    monkeypatch.setattr(
        "app.services.overlay_autoplace.heuristic_match",
        lambda *a, **kw: [{"asset_id": str(assets[0].id)}],
    )

    def _build_with_trace(raw, **kw):
        # What the real validator does per item: drop/demote/snap events fire
        # through the trace callback while the caller holds the row lock.
        kw["trace"]("suggestion_snap", from_s=1.0)
        return [{"id": "sug-1"}]

    monkeypatch.setattr("app.services.overlay_autoplace.build_suggestions", _build_with_trace)

    ap.match_overlay_suggestions(JOB_ID, VARIANT_ID, USER_ID)

    assert _variant_now(job)["overlay_suggest_status"] == "ready"
    snap = timeline.index("trace:suggestion_snap")
    last_commit = max(i for i, entry in enumerate(timeline) if entry == "commit")
    assert snap > last_commit, (
        "build_suggestions trace event fired before the locked persist "
        f"committed — self-deadlock regression. timeline={timeline}"
    )
