from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace

import pytest

import app.services.creation_thread_titles as titles


def _thread(
    *,
    title: str = "Untitled video",
    state: dict | None = None,
    revision: int = 4,
    status: str = "active",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        title=title,
        state={} if state is None else state,
        revision=revision,
        status=status,
    )


class _Session:
    def __init__(self, thread: SimpleNamespace | None, prompt: str | None = "Make a reel") -> None:
        self.thread = thread
        self.prompt = prompt
        self.commits = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, *_: object, **__: object) -> SimpleNamespace | None:
        return self.thread

    async def scalar(self, *_: object, **__: object) -> str | None:
        return self.prompt

    async def commit(self) -> None:
        self.commits += 1


class _SessionFactory:
    def __init__(self, *sessions: _Session) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> _Session:
        return self.sessions.pop(0)


@pytest.mark.parametrize("prompt", [None, "", " \n\t "])
def test_prepare_title_ignores_blank_first_prompts(prompt: str | None) -> None:
    thread = _thread()

    assert titles.prepare_title(thread, prompt) is False
    assert thread.title == "Untitled video"
    assert thread.state == {}


def test_prepare_title_claims_once_and_persists_the_fallback() -> None:
    thread = _thread()

    assert titles.prepare_title(thread, "  İstanbul  gezimden\n kısa video yap  ") is True
    assert thread.title == "İstanbul gezimden kısa video yap"
    assert thread.state == {"title_source": "first_prompt", "title_generation": "pending"}

    assert titles.prepare_title(thread, "A later prompt must not rename this chat") is False
    assert thread.title == "İstanbul gezimden kısa video yap"
    assert thread.state["title_generation"] == "pending"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "Expecting value"),
        (json.dumps({}), "Missing title"),
        (json.dumps({"title": 7}), "Missing title"),
        (json.dumps({"title": "  "}), "Invalid title length"),
        (json.dumps({"title": "x" * 81}), "Invalid title length"),
    ],
)
def test_clean_title_rejects_invalid_or_oversize_provider_output(raw: str, message: str) -> None:
    with pytest.raises((ValueError, json.JSONDecodeError), match=message):
        titles._clean_title(raw)


def test_clean_title_keeps_generated_language_text_and_normalizes_spacing() -> None:
    title = titles._clean_title(json.dumps({"title": "  “İstanbul   Gezi  Videosu”  "}))

    assert title == "İstanbul Gezi Videosu"


@pytest.mark.asyncio
async def test_duplicate_worker_claim_does_not_call_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(state={"title_source": "first_prompt", "title_generation": "running"})
    first = _Session(thread)
    monkeypatch.setattr(titles, "AsyncSessionLocal", _SessionFactory(first))

    async def provider(_: str) -> str:
        raise AssertionError("a running claim must not be generated again")

    monkeypatch.setattr(titles, "_summarize", provider)

    await titles.generate_thread_title(thread.id)

    assert first.commits == 0
    assert thread.state["title_generation"] == "running"


@pytest.mark.asyncio
async def test_provider_timeout_keeps_durable_fallback_and_marks_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(
        title="Make a reel about my garden",
        state={"title_source": "first_prompt", "title_generation": "pending"},
    )
    first = _Session(thread, prompt="Make a reel about my garden")
    second = _Session(thread)
    monkeypatch.setattr(titles, "AsyncSessionLocal", _SessionFactory(first, second))

    async def timed_out(_: str) -> str:
        raise TimeoutError

    monkeypatch.setattr(titles, "_summarize", timed_out)

    await titles.generate_thread_title(thread.id)

    assert thread.title == "Make a reel about my garden"
    assert thread.state == {"title_source": "first_prompt", "title_generation": "failed"}
    assert first.commits == 1
    assert second.commits == 1


@pytest.mark.asyncio
async def test_late_manual_rename_with_the_same_fallback_text_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = "My garden reel"
    thread = _thread(
        title=fallback,
        state={"title_source": "first_prompt", "title_generation": "pending"},
    )
    first = _Session(thread, prompt=fallback)
    second = _Session(thread)
    monkeypatch.setattr(titles, "AsyncSessionLocal", _SessionFactory(first, second))

    async def provider(_: str) -> str:
        # Simulate a PATCH completing while provider I/O is outside the DB lock.
        thread.title = fallback
        thread.state = {"title_source": "user", "title_generation": "running"}
        return fallback

    monkeypatch.setattr(titles, "_summarize", provider)

    await titles.generate_thread_title(thread.id)

    assert thread.title == fallback
    assert thread.state == {"title_source": "user", "title_generation": "running"}
    assert second.commits == 0


@pytest.mark.asyncio
async def test_generated_title_is_committed_and_survives_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread(
        title="Create a 30-second reel from Barcelona clips",
        state={"title_source": "first_prompt", "title_generation": "pending"},
    )
    first = _Session(thread, prompt=thread.title)
    second = _Session(thread)
    monkeypatch.setattr(titles, "AsyncSessionLocal", _SessionFactory(first, second))

    async def provider(_: str) -> str:
        return "Barcelona Trip Reel"

    async def append(_: _Session, target: SimpleNamespace, **__: object) -> None:
        target.revision += 1

    from app.routes import creation_threads

    monkeypatch.setattr(titles, "_summarize", provider)
    monkeypatch.setattr(creation_threads, "_append", append)

    await titles.generate_thread_title(thread.id)

    reopened = _thread(
        title=thread.title,
        state=copy.deepcopy(thread.state),
        revision=thread.revision,
    )
    assert reopened.title == "Barcelona Trip Reel"
    assert reopened.state["title_source"] == "generated"
    assert reopened.state["title_generation"] == "completed"
    assert reopened.state["title_generated_revision"] == reopened.revision
    assert second.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["archived", "failed"])
async def test_deleted_or_archived_threads_do_not_generate_titles(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    thread = _thread(
        status=status,
        state={"title_source": "first_prompt", "title_generation": "pending"},
    )
    first = _Session(thread)
    monkeypatch.setattr(titles, "AsyncSessionLocal", _SessionFactory(first))

    async def provider(_: str) -> str:
        raise AssertionError("inactive threads must not call the provider")

    monkeypatch.setattr(titles, "_summarize", provider)

    await titles.generate_thread_title(thread.id)

    assert first.commits == 0
    assert thread.state["title_generation"] == "pending"


@pytest.mark.asyncio
async def test_deleted_thread_does_not_generate_a_title(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _Session(None)
    monkeypatch.setattr(titles, "AsyncSessionLocal", _SessionFactory(first))

    async def provider(_: str) -> str:
        raise AssertionError("a deleted thread must not call the provider")

    monkeypatch.setattr(titles, "_summarize", provider)

    await titles.generate_thread_title(uuid.uuid4())

    assert first.commits == 0


@pytest.mark.parametrize(
    ("revision", "state", "expected_revision", "matches"),
    [
        (7, {}, 7, True),
        (8, {"title_generated_revision": 8}, 7, True),
        (8, {}, 7, False),
        (8, {"title_generated_revision": 7}, 7, False),
        (9, {"title_generated_revision": 9}, 7, False),
    ],
)
def test_matches_conversation_revision_allows_only_one_title_delta(
    revision: int, state: dict, expected_revision: int, matches: bool
) -> None:
    assert (
        titles.matches_conversation_revision(
            _thread(revision=revision, state=state), expected_revision
        )
        is matches
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_message", [None, "existing-user-event"])
async def test_message_admission_only_names_the_first_submitted_prompt(prior_message):
    from unittest.mock import AsyncMock

    thread = _thread()
    db = SimpleNamespace(scalar=AsyncMock(return_value=prior_message))
    await titles.prepare_message_title(db, thread, "Barcelona trip clips")
    assert thread.title == ("Barcelona trip clips" if prior_message is None else "Untitled video")
    assert (thread.state.get("title_generation") == "pending") is (prior_message is None)


@pytest.mark.asyncio
async def test_hung_provider_is_cancelled_without_changing_fallback(monkeypatch):
    import asyncio

    thread = _thread(
        title="Barcelona clips",
        state={
            "title_source": "first_prompt",
            "title_generation": "pending",
        },
    )
    sessions = _SessionFactory(_Session(thread), _Session(thread))
    cancelled = asyncio.Event()

    async def provider(_: str) -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(titles, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(titles, "TITLE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(titles, "_summarize", provider)
    await titles.generate_thread_title(thread.id)
    assert cancelled.is_set()
    assert thread.title == "Barcelona clips"
    assert thread.state["title_generation"] == "failed"


@pytest.mark.asyncio
async def test_summarizer_sends_only_the_first_prompt_as_data(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from google import genai

    models = SimpleNamespace(
        generate_content=AsyncMock(
            return_value=SimpleNamespace(
                text=json.dumps({"title": "Barcelona Trip Reel"}),
            )
        )
    )
    client = AsyncMock()
    client.__aenter__.return_value = SimpleNamespace(models=models)
    factory = Mock(return_value=SimpleNamespace(aio=client))
    monkeypatch.setattr(genai, "Client", factory)
    prompt = "Create a 30-second reel from my Barcelona trip clips"

    assert await titles._summarize(prompt) == "Barcelona Trip Reel"
    request = models.generate_content.await_args.kwargs
    assert json.loads(request["contents"]) == {"first_user_prompt": prompt}
    assert "same language" in request["config"].system_instruction
    assert request["config"].response_mime_type == "application/json"
    client.__aexit__.assert_awaited_once()
