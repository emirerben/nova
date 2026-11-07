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
    # The provider enforces a 10s minimum even when our application timeout
    # cancels the optional naming task earlier.
    assert factory.call_args.kwargs["http_options"].timeout == 10_000
    assert titles.TITLE_TIMEOUT_S == 8
    request = models.generate_content.await_args.kwargs
    assert json.loads(request["contents"]) == {"first_user_prompt": prompt}
    assert "same language" in request["config"].system_instruction
    assert request["config"].response_mime_type == "application/json"
    client.__aexit__.assert_awaited_once()


# KRI-1262: the app's stock "Suggest an edit." must never become the chat's title.
#
# Failure modes (each is a test below):
# - the stock prompt (any case, punctuation or heading) is kept as, or summarized into, the title;
# - a real request that merely contains the words is deferred by mistake;
# - no plan ever arrives: the chat keeps the placeholder and nothing crashes;
# - the creator renames the chat before the plan, while it is queued, or while the model runs;
# - the provider times out or fails: the placeholder stays, never the stock prompt;
# - a Turkish plan is summarized in its own language;
# - two draft commits (concurrent turns) release the same chat twice.

STOCK = [
    "Suggest an edit.",
    "suggest an edit",
    "SUGGEST AN EDIT!",
    "Suggest an edit.\nSuggest an edit.",
]


@pytest.mark.parametrize("prompt", STOCK)
def test_the_stock_prompt_is_never_the_title_and_waits_for_a_plan(prompt: str) -> None:
    thread = _thread()

    assert titles.prepare_title(thread, prompt) is True
    assert thread.title == "Untitled video"
    assert thread.state == {"title_source": "first_prompt", "title_generation": "deferred"}


def test_a_real_request_containing_the_words_is_named_as_before() -> None:
    thread = _thread()

    titles.prepare_title(thread, "Suggest an edit for my Barcelona trip")

    assert thread.title == "Suggest an edit for my Barcelona trip"
    assert thread.state["title_generation"] == "pending"


@pytest.mark.asyncio
async def test_a_later_message_does_not_name_a_deferred_chat_from_the_prompt() -> None:
    from unittest.mock import AsyncMock

    thread = _thread()
    titles.prepare_title(thread, "Suggest an edit.")
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))

    await titles.prepare_message_title(db, thread, "Make it fast, Barcelona text")

    assert thread.title == "Untitled video"
    assert thread.state["title_generation"] == "deferred"


def test_a_plan_releases_a_deferred_chat_once() -> None:
    thread = _thread(state={"title_source": "first_prompt", "title_generation": "deferred"})

    assert titles.release_deferred_title(thread, "  Open on the whisk\nand end on the order.  ")
    assert thread.state == {
        "title_source": "first_prompt",
        "title_generation": "pending",
        "title_plan_text": "Open on the whisk and end on the order.",
    }
    assert titles.release_deferred_title(thread, "A second draft") is False
    assert thread.state["title_plan_text"] == "Open on the whisk and end on the order."


@pytest.mark.parametrize("plan", [None, "", "  \n "])
def test_no_plan_text_leaves_the_chat_waiting(plan: str | None) -> None:
    thread = _thread(state={"title_source": "first_prompt", "title_generation": "deferred"})

    assert titles.release_deferred_title(thread, plan) is False
    assert thread.state["title_generation"] == "deferred"
    assert thread.title == "Untitled video"


@pytest.mark.parametrize(
    "state",
    [
        {"title_source": "user", "title_generation": "deferred"},
        {"title_source": "first_prompt", "title_generation": "running"},
        {"title_source": "generated", "title_generation": "completed"},
        {},
    ],
)
def test_a_rename_or_any_other_state_is_never_released(state: dict) -> None:
    thread = _thread(title="My own name", state=dict(state))

    assert titles.release_deferred_title(thread, "Open on the whisk.") is False
    assert thread.state == state


def test_a_renamed_title_blocks_release_even_if_the_state_was_not_stamped() -> None:
    thread = _thread(
        title="My own name", state={"title_source": "first_prompt", "title_generation": "deferred"}
    )

    assert titles.release_deferred_title(thread, "Open on the whisk.") is False
    assert thread.title == "My own name"


@pytest.mark.asyncio
async def test_a_released_chat_is_named_from_its_plan_in_the_plan_language(monkeypatch) -> None:
    plan = "Mutfaktaki çırpıcıyla aç, sipariş paketiyle bitir."
    thread = _thread(
        state={
            "title_source": "first_prompt",
            "title_generation": "pending",
            "title_plan_text": plan,
        }
    )
    sessions = _SessionFactory(_Session(thread, prompt="Suggest an edit."), _Session(thread))
    seen: list[tuple[str, bool]] = []

    async def provider(text: str, *, from_plan: bool = False) -> str:
        seen.append((text, from_plan))
        return "Mutfak Günlüğü"

    async def append(_: _Session, target: SimpleNamespace, **__: object) -> None:
        target.revision += 1

    from app.routes import creation_threads

    monkeypatch.setattr(titles, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(titles, "_summarize", provider)
    monkeypatch.setattr(creation_threads, "_append", append)

    await titles.generate_thread_title(thread.id)

    assert seen == [(plan, True)]
    assert thread.title == "Mutfak Günlüğü"
    assert thread.state["title_generation"] == "completed"
    assert "title_plan_text" not in thread.state


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, RuntimeError])
async def test_a_failed_plan_title_keeps_the_placeholder_never_the_stock_prompt(
    monkeypatch, failure
) -> None:
    thread = _thread(
        state={
            "title_source": "first_prompt",
            "title_generation": "pending",
            "title_plan_text": "Open on the whisk.",
        }
    )
    monkeypatch.setattr(
        titles,
        "AsyncSessionLocal",
        _SessionFactory(_Session(thread, prompt="Suggest an edit."), _Session(thread)),
    )

    async def provider(*_: object, **__: object) -> str:
        raise failure

    monkeypatch.setattr(titles, "_summarize", provider)

    await titles.generate_thread_title(thread.id)

    assert thread.title == "Untitled video"
    assert thread.state["title_generation"] == "failed"


@pytest.mark.asyncio
async def test_a_deferred_chat_is_not_generated_before_it_is_released(monkeypatch) -> None:
    thread = _thread(state={"title_source": "first_prompt", "title_generation": "deferred"})
    first = _Session(thread, prompt="Suggest an edit.")
    monkeypatch.setattr(titles, "AsyncSessionLocal", _SessionFactory(first))

    async def provider(*_: object, **__: object) -> str:
        raise AssertionError("a deferred chat has nothing to summarize yet")

    monkeypatch.setattr(titles, "_summarize", provider)

    await titles.generate_thread_title(thread.id)

    assert first.commits == 0
    assert thread.state["title_generation"] == "deferred"


@pytest.mark.asyncio
async def test_a_rename_while_the_plan_is_summarized_wins(monkeypatch) -> None:
    thread = _thread(
        state={
            "title_source": "first_prompt",
            "title_generation": "pending",
            "title_plan_text": "Open on the whisk.",
        }
    )
    monkeypatch.setattr(
        titles, "AsyncSessionLocal", _SessionFactory(_Session(thread), _Session(thread))
    )

    async def provider(*_: object, **__: object) -> str:
        thread.title = "My own name"
        thread.state = {**thread.state, "title_source": "user"}
        return "Generated Name"

    monkeypatch.setattr(titles, "_summarize", provider)

    await titles.generate_thread_title(thread.id)

    assert thread.title == "My own name"
    assert thread.state["title_source"] == "user"


@pytest.mark.asyncio
async def test_the_plan_summary_is_sent_as_data_with_its_own_instruction(monkeypatch) -> None:
    from unittest.mock import AsyncMock, Mock

    from google import genai

    models = SimpleNamespace(
        generate_content=AsyncMock(
            return_value=SimpleNamespace(text=json.dumps({"title": "Whisk To Order"}))
        )
    )
    client = AsyncMock()
    client.__aenter__.return_value = SimpleNamespace(models=models)
    monkeypatch.setattr(genai, "Client", Mock(return_value=SimpleNamespace(aio=client)))

    assert await titles._summarize("Open on the whisk.", from_plan=True) == "Whisk To Order"

    request = models.generate_content.await_args.kwargs
    assert json.loads(request["contents"]) == {"plan": "Open on the whisk."}
    assert "plan" in request["config"].system_instruction
    assert "first user prompt" not in request["config"].system_instruction
