"""The raw OpenAI calls (caption correction, caption translation, lyric Whisper) reach the ledger.

Failure modes, written before the code (each has a test below):

  1. reservation refused (budget exhausted)       -> provider never contacted; the caller
                                                      degrades (captions stay uncorrected)
  2. provider 4xx after the start fence (429)      -> reservation released, costs nothing
  3. provider 5xx after the start fence            -> reservation `unknown`, estimate retained
  4. connection dropped mid-call                   -> `unknown`, not released
  5. settlement cannot price the answer            -> response kept, settled at the estimate
  6. usage block missing from the answer           -> settled at the estimate, never at zero
  7. the same text corrected twice in one task     -> two calls, two rows, neither refused
  8. ledger switched off (local dev)               -> call runs, no row is written
  9. SDK retries                                   -> the reservation covers every allowed attempt
 10. Whisper bills by audio minute, from the provider's reported duration
 11. Whisper refused                               -> WhisperLyricsError, never an unmetered call
 12. the lyrics agent's run context reaches the Whisper reservation

The only fake is api.openai.com: a local HTTP server the real ``openai`` SDK talks to, so status
codes, retries and dropped connections come from the SDK. The ledger runs on PostgreSQL.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import openai
import pytest
from sqlalchemy import text

from app.agents._runtime import AiBudgetExceededError, RunContext
from app.config import settings
from app.database import sync_engine, sync_session
from app.models import Job, User
from app.pipeline import caption_translate
from app.pipeline.caption_correct import correct_caption_cues
from app.pipeline.caption_translate import _llm_translate_lines as real_translate
from app.pipeline.caption_translate import ensure_caption_language
from app.services import ai_cost_control as costs
from app.services import openai_metering as metering
from app.services.whisper_lyrics import WhisperLyricsError, transcribe_for_lyrics
from tests.services.fake_openai_server import FakeOpenAI
from tests.services.test_ai_cost_control import ledger  # noqa: F401

GPT4O_IN, GPT4O_OUT = 2.50e-6, 10.00e-6


@pytest.fixture
def server():
    fake = FakeOpenAI(prompt_tokens=1_000, completion_tokens=200)
    yield fake
    fake.close()


@pytest.fixture
def client(server):
    return openai.OpenAI(api_key="sk-test", base_url=server.url)


@pytest.fixture
def prod(ledger, monkeypatch, server):  # noqa: F811
    """Production attribution, an isolated ledger period, and a creator with one job."""
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setenv("OPENAI_BASE_URL", server.url)
    period = costs._month_start(None)
    user_id, job_id = uuid.uuid4(), uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{ledger}:{user_id}@example.test"))
        db.flush()
        db.add(Job(id=job_id, user_id=user_id, status="done", raw_storage_path="test/x.mp4"))
        db.commit()
    yield SimpleNamespace(job_id=str(job_id), user_id=user_id, period=period)
    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai_cost_reservations WHERE period_start = :p"), {"p": period}
        )
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": str(job_id)})
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(user_id)})


def _rows(prod) -> list[dict]:
    with sync_engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT feature, provider, requested_model, resolved_model, status, "
                    "estimated_cost_usd, settled_cost_usd, creator_id, usage_json, price_version "
                    "FROM ai_cost_reservations WHERE period_start = :p ORDER BY created_at"
                ),
                {"p": prod.period},
            ).mappings()
        ]


def _chat(client, prod, *, attempts=3):
    return metering.metered_chat_completion(
        client,
        feature=metering.CAPTION_CORRECTION_FEATURE,
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "fix spelling"},
            {"role": "user", "content": '{"lines": ["fix teh text"]}'},
        ],
        job_id=prod.job_id,
        attempts=attempts,
        temperature=0,
        response_format={"type": "json_object"},
    )


def test_a_settled_call_books_provider_usage_against_the_creator(prod, client, server):
    response = _chat(client, prod)

    assert response.choices[0].message.content
    (row,) = _rows(prod)
    assert (row["feature"], row["provider"], row["status"]) == (
        "caption_correction",
        "openai",
        "settled",
    )
    assert row["requested_model"] == "gpt-4o"
    assert row["price_version"] == metering.PRICE_VERSION
    assert row["resolved_model"] == "gpt-4o-2024-08-06"
    assert row["creator_id"] == prod.user_id
    assert float(row["settled_cost_usd"]) == pytest.approx(1_000 * GPT4O_IN + 200 * GPT4O_OUT)
    assert float(row["estimated_cost_usd"]) > float(row["settled_cost_usd"])


def test_the_provider_request_is_unchanged_and_the_reservation_is_priced_for_its_output(
    prod, client, server
):
    import json

    _chat(client, prod, attempts=1)

    sent = json.loads(server.requests[0]["body"])
    assert set(sent) == {"model", "messages", "temperature", "response_format"}
    prompt = "fix spelling\n" + '{"lines": ["fix teh text"]}'
    (row,) = _rows(prod)
    priced = costs.estimate_call_cost_usd(
        model="gpt-4o",
        prompt=prompt,
        max_output_tokens=metering.output_bound_tokens(prompt),
        media_mime=None,
    )
    assert float(row["estimated_cost_usd"]) == pytest.approx(priced)


def test_a_refused_reservation_never_reaches_the_provider_or_fails_the_caller(
    prod, client, server, monkeypatch
):
    monkeypatch.setattr(settings, "ai_openai_monthly_budget_usd", 0.0)

    with pytest.raises(AiBudgetExceededError):
        _chat(client, prod)
    assert server.requests == []

    cues = [{"text": "fix teh text", "start_s": 0.0, "end_s": 1.0}]
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    assert correct_caption_cues(cues, "en", job_id=prod.job_id) == cues
    assert server.requests == []


def test_a_429_after_the_start_fence_releases_the_reservation(prod, client, server):
    server.behaviors.extend([("status", 429)] * 3)

    with pytest.raises(openai.RateLimitError):
        _chat(client, prod)

    (row,) = _rows(prod)
    assert row["status"] == "released"
    assert len(server.requests) == 3


def test_a_5xx_keeps_the_estimate_as_unknown_liability(prod, client, server):
    server.behaviors.extend([("status", 503)] * 3)

    with pytest.raises(openai.InternalServerError):
        _chat(client, prod)

    (row,) = _rows(prod)
    assert row["status"] == "unknown"
    assert float(row["estimated_cost_usd"]) > 0


def test_a_dropped_connection_is_unknown_not_released(prod, client, server):
    server.behaviors.extend([("drop",)] * 3)

    with pytest.raises(openai.APIConnectionError):
        _chat(client, prod)

    assert [row["status"] for row in _rows(prod)] == ["unknown"]


def test_a_retry_that_succeeds_settles_once_under_a_reservation_for_every_attempt(
    prod, client, server
):
    server.behaviors.append(("status", 503))

    _chat(client, prod, attempts=3)

    (row,) = _rows(prod)
    assert row["status"] == "settled" and len(server.requests) == 2
    single = costs.estimate_call_cost_usd(
        model="gpt-4o",
        prompt="fix spelling\n" + '{"lines": ["fix teh text"]}',
        max_output_tokens=1_024,
        media_mime=None,
    )
    assert float(row["estimated_cost_usd"]) == pytest.approx(3 * single)


def test_an_answer_the_ledger_cannot_price_is_returned_and_settled_at_the_estimate(
    prod, client, server
):
    server.resolved_model = "o9-unregistered"

    response = _chat(client, prod)

    assert response.choices[0].message.content
    (row,) = _rows(prod)
    assert row["status"] == "settled"
    assert float(row["settled_cost_usd"]) == pytest.approx(float(row["estimated_cost_usd"]))
    assert row["usage_json"]["settled_at_estimate"] is True


def test_an_answer_without_usage_settles_at_the_estimate_not_zero(prod, server):
    class NoUsage:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    return SimpleNamespace(
                        choices=[], model="gpt-4o", id="x", usage=None, kwargs=kwargs
                    )

    metering.metered_chat_completion(
        NoUsage,
        feature=metering.CAPTION_TRANSLATION_FEATURE,
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        job_id=prod.job_id,
    )

    (row,) = _rows(prod)
    assert row["usage_json"]["usage_missing"] is True
    assert float(row["settled_cost_usd"]) == pytest.approx(float(row["estimated_cost_usd"]))


def test_the_same_text_twice_in_one_task_is_two_calls_not_a_refusal(prod, client, server):
    ctx = RunContext(job_id=prod.job_id, request_id="celery-task-1")
    for _ in range(2):
        metering.run_metered(
            feature="caption_correction",
            model="gpt-4o",
            estimate_usd=lambda: 0.01,
            attempts=1,
            ctx=ctx,
            identity="identical cues",
            operation=lambda: client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": '{"lines": []}'}]
            ),
            settle=lambda receipt, response: costs.settle_paid_call_cost(
                receipt, cost_usd=0.001, resolved_model="gpt-4o"
            ),
        )

    assert [row["status"] for row in _rows(prod)] == ["settled", "settled"]


def test_with_the_ledger_off_the_call_runs_and_writes_no_row(prod, client, server, monkeypatch):
    monkeypatch.setattr(settings, "ai_cost_control_enabled", False)

    response = _chat(client, prod)

    assert response.choices[0].message.content
    assert len(server.requests) == 1
    assert _rows(prod) == []


def test_translation_is_a_metered_caption_translation_row(prod, server, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(caption_translate, "_llm_translate_lines", real_translate)
    words = [
        SimpleNamespace(text=w, start_s=i * 0.4, end_s=i * 0.4 + 0.3, confidence=1.0)
        for i, w in enumerate("la casa es muy grande y bonita hoy".split())
    ]

    result = ensure_caption_language(
        words, requested="en", fallback_language="es", enabled=True, job_id=prod.job_id
    )

    assert result.outcome in {"translated", "unmet"}
    (row,) = _rows(prod)
    assert (row["feature"], row["status"]) == ("caption_translation", "settled")


def _audio(tmp_path):
    path = tmp_path / "song.m4a"
    path.write_bytes(b"not really audio")
    return str(path)


def test_whisper_bills_the_providers_reported_minutes(prod, server, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    ctx = RunContext(job_id="track:" + str(uuid.uuid4()), usage_purpose="optional_background")

    result = transcribe_for_lyrics(_audio(tmp_path), ctx=ctx)

    assert [w.text for w in result.words] == ["hello", "there"]
    (row,) = _rows(prod)
    assert (row["feature"], row["requested_model"], row["status"]) == (
        "lyrics_whisper",
        "whisper-1",
        "settled",
    )
    assert float(row["settled_cost_usd"]) == pytest.approx(125.0 / 60 * 0.006)
    assert row["usage_json"]["duration_source"] == "provider"
    assert row["creator_id"] is None


def test_a_refused_whisper_reservation_fails_extraction_without_calling_openai(
    prod, server, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "ai_openai_monthly_budget_usd", 0.0)

    with pytest.raises(WhisperLyricsError):
        transcribe_for_lyrics(_audio(tmp_path), ctx=RunContext(usage_purpose="optional_background"))

    assert server.requests == []


def test_the_lyrics_agents_run_context_attributes_its_whisper_reservation(
    prod, server, tmp_path, monkeypatch
):
    from app.agents import lyrics

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(lyrics, "_lookup_via_get", lambda *a, **k: None)
    monkeypatch.setattr(lyrics, "_lookup_via_search", lambda *a, **k: None)

    output = lyrics.LyricsExtractionAgent(model_client=None).run(
        lyrics.LyricsInput(audio_path=_audio(tmp_path), track_title="Song", artist="Artist"),
        ctx=RunContext(
            job_id="track:not-a-uuid",
            usage_purpose="optional_background",
            extra={"skip_agent_run_persist": True},
        ),
    )

    assert output.source == "whisper_only"
    (row,) = _rows(prod)
    assert row["feature"] == "lyrics_whisper" and row["status"] == "settled"
    assert costs.is_internal_creator(None) is False
