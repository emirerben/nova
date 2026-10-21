"""KRI-667: one net for English text reaching a Turkish Kria chat.

English kept slipping into Turkish chats one phone test at a time (KRI-592, KRI-630):
every chat event has many writers and no single choke point. ``turkish_chat_net`` wraps
the chat event writers (``_append_sync_event``, ``runtime._append_event``,
``drafts._append_event``, v1 ``creation_threads._append``) and backstops direct
``CreationThreadEvent(...)`` construction with a SQLAlchemy ``init`` listener. For every
non-user event written while the chat is Turkish it classifies the content and the
creator-facing payload text, records each English segment, and fails the test at teardown
(it never raises inside a writer: the best-effort writers swallow exceptions, which would
hide the leak).

Failure modes, written before the code:

* a creator literal (a quoted title, the creator's own message words, a brief ``literal``,
  a shot label, a file name like ``whisking.mov``, an id or a number) is flagged as English;
* intentional English is flagged: the "Render this draft: " card prefix, the client stock
  messages, quoted app button names, payload machine values, catalog song names, look
  style names, the model-written post caption, ``inferred_labels`` (vision subjects);
* a Turkish sentence with a loan word ("Format: Günlük vlog", "montaj") is flagged;
* a short English label ("Not used", "Lip-sync", "Background") is missed because it has
  fewer than two English words;
* a writer path is missed: a module that builds ``CreationThreadEvent`` itself, a lazily
  imported writer, or a test that patches the writer after the net is installed;
* the language is unknown where the event is written (a render worker binds none), so the
  chat's own stored language must be read from the thread (or a thread seen earlier);
* creator-facing payload text (choice options, receipts, approval summaries, plan blocks,
  change chips) is not scanned, only ``content``;
* the net raises inside a best-effort writer, the writer swallows it, the test passes;
* an English chat is checked (English stays byte-identical and is never flagged).
"""

from __future__ import annotations

import contextvars
import re
import unicodedata
import weakref
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import event as sa_event

from app.kria.reply_language import (
    _EN_WORDS,
    _scores,
    current_reply_language,
    detect_chat_language,
    thread_reply_language,
)
from app.models import CreationThread, CreationThreadEvent

# -- what is allowed to stay English ---------------------------------------------------
#
# docs/pipelines/kria-agent-runtime.md "Never localized": these are contracts with the
# apps or the creator's own taps, and they are English on purpose in every chat.
ALLOWED_ENGLISH: tuple[str, ...] = (
    # The approval card's consequence prefix; iOS parses it (kria_runtime / plan_review).
    "Render this draft: ",
    # reply_language._CLIENT_STOCK_MESSAGES: sentences the apps send for a tap.
    "Retry preparing my clips.",
    "Keep the same plan with my current footage",
    # App button names quoted in Turkish copy (they are English in the app UI).
    "Refresh project",
    "Visuals",
    "Retry",
)

# Events whose content is the creator's own words even though the role is not "user".
_CREATOR_WORDS_EVENTS = frozenset({"thread_created"})

# English words the server copy uses that ``reply_language._EN_WORDS`` (built to read
# creator messages) does not list. Only words no Turkish sentence uses as a word.
_SERVER_EN_WORDS = frozenset(
    """
    background lip sync voiceover overlay overlays effect effects sound used render rendered
    draft approved nothing restored previous remembered future videos updated stopped
    preference left words revise corner without kind yet length choice waiting option
    offered footage selected continue update seconds extended added requested ends before
    voice plays ended using under other tell rebuild requirement relax ask try record pick
    choose find speech sure order nobody something couldn isn wasn doesn won
    """.split()
)

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’|«[^»\n]*»")
_FILE_NAME = re.compile(
    r"[\w\-]+\.(?:mov|mp4|m4v|m4a|mp3|wav|aac|caf|heic|heif|jpe?g|png|gif|webp)\b", re.I
)
_URL = re.compile(r"https?://\S+")
_HEX_ID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\b[0-9a-f]{16,}\b", re.I)
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
# Sentence ends, newlines, ";", parentheses, and a label's colon ("Yapıldı: Keep 24 s").
_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+|;|[()]|:\s")


def _strip(text: str, literals: Iterable[str]) -> str:
    out = unicodedata.normalize("NFC", text)
    for phrase in ALLOWED_ENGLISH:
        out = out.replace(phrase, " ")
    for literal in sorted(
        {lit for lit in literals if lit and len(lit) >= 2}, key=len, reverse=True
    ):
        out = re.sub(re.escape(unicodedata.normalize("NFC", literal)), " ", out, flags=re.I)
    for pattern in (_QUOTED, _URL, _FILE_NAME, _HEX_ID, _NUMBER):
        out = pattern.sub(" ", out)
    return out


def is_english_segment(segment: str) -> bool:
    """True when one stripped segment reads as English, not Turkish."""
    if detect_chat_language(segment) == "en":
        return True
    tr, _en, _count = _scores(segment)
    words = [w.lower() for w in _WORD.findall(segment)]
    english = sum(1 for w in words if w in _EN_WORDS or w in _SERVER_EN_WORDS)
    if tr:
        # An English clause glued to one Turkish word ("Tamam, I kept the order you set").
        return english >= 3 and english > 2 * tr
    return english >= 2 or (english >= 1 and len(words) <= 2)


def english_segments(text: str | None, literals: Iterable[str] = ()) -> list[str]:
    """The English segments of ``text`` once creator literals and allowed English are out."""
    if not text:
        return []
    stripped = _strip(text, literals)
    return [
        " ".join(seg.split())
        for seg in _SPLIT.split(stripped)
        if seg.strip() and is_english_segment(seg)
    ]


# -- what an event shows the creator ---------------------------------------------------


def _block_texts(index: int, entry: Mapping[str, Any]) -> Iterator[tuple[str, str, list[str]]]:
    """Plan-block summary/detail the server wrote (titles, songs, styles are literals)."""
    section = str(entry.get("section_id") or "")
    payload = entry.get("payload") if isinstance(entry.get("payload"), Mapping) else {}
    summary, detail = entry.get("summary"), entry.get("detail")
    literals = [str(payload.get(k)) for k in ("title", "artist", "text") if payload.get(k)]
    if entry.get("skipped"):
        if isinstance(summary, str):
            yield f"blocks[{index}].summary", summary, literals
        return
    # The title and post caption are the creator's / model's on-video words, the look is
    # a style name, and a catalog song is "Title · Artist": none of it is chat copy.
    if section in {"title", "post_caption", "look"}:
        return
    if isinstance(summary, str) and not (section == "music" and " · " in summary):
        yield f"blocks[{index}].summary", summary, literals
    if isinstance(detail, str):
        yield f"blocks[{index}].detail", detail, literals


def creator_facing_texts(
    event_type: str, content: str | None, payload: Mapping[str, Any] | None
) -> Iterator[tuple[str, str, list[str]]]:
    """(path, text, extra literals) for every creator-visible string of one event.

    Machine keys (code, recovery, turn_value, next_actions, decline_reason, field_path,
    status, verification, stage, *_id, conflict, kind, input_digest, option aliases,
    ``inferred`` / ``inferred_labels``) are never read.
    """
    if isinstance(content, str) and content:
        yield "content", content, []
    data = payload if isinstance(payload, Mapping) else {}
    question = data.get("choice_question")
    if isinstance(question, Mapping):
        for i, option in enumerate(question.get("options") or []):
            if isinstance(option, Mapping):
                for key in ("label", "description"):
                    if isinstance(option.get(key), str):
                        yield f"choice_question.options[{i}].{key}", option[key], []
    clip_question = data.get("clip_question")
    if isinstance(clip_question, Mapping):
        for i, category in enumerate(clip_question.get("categories") or []):
            if isinstance(category, Mapping) and isinstance(category.get("label"), str):
                yield f"clip_question.categories[{i}].label", category["label"], []
    for i, receipt in enumerate(data.get("requirement_receipts") or []):
        if isinstance(receipt, Mapping) and isinstance(receipt.get("reason"), str):
            inferred = [str(x) for x in receipt.get("inferred") or []]
            yield f"requirement_receipts[{i}].reason", receipt["reason"], inferred
    for key in ("consequence_summary", "cost_summary"):
        if isinstance(data.get(key), str):
            yield key, data[key], []
    if event_type == "plan_update_summary" and isinstance(data.get("text"), str):
        yield "text", data["text"], []
    for i, entry in enumerate(data.get("blocks") or []):
        if isinstance(entry, Mapping):
            yield from _block_texts(i, entry)
    for i, change in enumerate(data.get("changes") or []):
        if isinstance(change, str):
            yield f"changes[{i}]", change, []


# -- the net -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Leak:
    writer: str
    event_type: str
    path: str
    segment: str
    text: str

    def __str__(self) -> str:
        return f"{self.writer} {self.event_type}.{self.path}: {self.segment!r} in {self.text!r}"


# Set while a wrapped writer runs, so the construction backstop does not count it twice.
_INSIDE_WRITER: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "turkish_chat_net_inside_writer", default=False
)


@dataclass
class TurkishChatNet:
    leaks: list[Leak] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    literals: set[str] = field(default_factory=set)
    checked: int = 0
    _threads: list[weakref.ref] = field(default_factory=list)
    _strong: list[Any] = field(default_factory=list)

    def add_literals(self, *texts: str | None) -> None:
        """Creator words the chat may repeat as written (titles, labels, brief literals)."""
        self.literals.update(t for t in texts if t)

    def remember_thread(self, thread: Any) -> None:
        try:
            self._threads.append(weakref.ref(thread))
        except TypeError:  # SimpleNamespace etc. cannot be weak-referenced
            self._strong.append(thread)

    def _thread_by_id(self, thread_id: Any) -> Any:
        if thread_id is None:
            return None
        for thread in [*(ref() for ref in self._threads), *self._strong]:
            if thread is not None and getattr(thread, "id", None) == thread_id:
                return thread
        return None

    def language(self, thread: Any = None, thread_id: Any = None) -> str | None:
        bound = current_reply_language()
        if bound is not None:
            return bound
        thread = thread if thread is not None else self._thread_by_id(thread_id)
        return thread_reply_language(thread) if thread is not None else None

    def inspect(
        self,
        *,
        writer: str,
        thread: Any = None,
        thread_id: Any = None,
        role: str | None,
        event_type: str | None,
        content: str | None,
        payload: Mapping[str, Any] | None,
    ) -> None:
        """Record English in a Turkish chat's event. Never raises."""
        try:
            event_type = str(event_type or "")
            if role == "user" or event_type in _CREATOR_WORDS_EVENTS:
                self.add_literals(content)
                return
            if self.language(thread, thread_id) != "tr":
                return
            self.checked += 1
            for path, text, extra in creator_facing_texts(event_type, content, payload):
                for segment in english_segments(text, [*self.literals, *extra]):
                    self.leaks.append(Leak(writer, event_type, path, segment, text))
        except Exception as exc:  # noqa: BLE001 - a writer must never see the net fail
            self.errors.append(f"{writer}: {type(exc).__name__}: {exc}")

    # -- wrappers ---------------------------------------------------------------------

    def wrap_sync(self, original: Any, name: str = "_append_sync_event") -> Any:
        net = self

        def _append_sync_event(*args: Any, **kwargs: Any) -> Any:
            thread = args[1] if len(args) > 1 else kwargs.get("thread")
            net.remember_thread(thread)
            net.inspect(
                writer=name,
                thread=thread,
                role=kwargs.get("role"),
                event_type=kwargs.get("event_type"),
                content=kwargs.get("content"),
                payload=kwargs.get("payload"),
            )
            token = _INSIDE_WRITER.set(True)
            try:
                return original(*args, **kwargs)
            finally:
                _INSIDE_WRITER.reset(token)

        _append_sync_event.__wrapped__ = original  # type: ignore[attr-defined]
        return _append_sync_event

    def wrap_async(self, original: Any, name: str = "_append_event") -> Any:
        net = self

        async def _append_event(*args: Any, **kwargs: Any) -> Any:
            thread = args[1] if len(args) > 1 else kwargs.get("thread")
            net.remember_thread(thread)
            net.inspect(
                writer=name,
                thread=thread,
                role=kwargs.get("role"),
                event_type=kwargs.get("event_type"),
                content=kwargs.get("content"),
                payload=kwargs.get("payload"),
            )
            token = _INSIDE_WRITER.set(True)
            try:
                return await original(*args, **kwargs)
            finally:
                _INSIDE_WRITER.reset(token)

        _append_event.__wrapped__ = original  # type: ignore[attr-defined]
        return _append_event

    # -- SQLAlchemy backstop ------------------------------------------------------------

    def _on_event_init(self, _target: Any, _args: Any, kwargs: Mapping[str, Any]) -> None:
        if _INSIDE_WRITER.get():
            return
        self.inspect(
            writer="CreationThreadEvent()",
            thread_id=kwargs.get("thread_id"),
            role=kwargs.get("role"),
            event_type=kwargs.get("event_type"),
            content=kwargs.get("content"),
            payload=kwargs.get("payload"),
        )

    def _on_thread(self, target: Any, *_rest: Any) -> None:
        self.remember_thread(target)

    def report(self) -> str:
        lines = [str(leak) for leak in self.leaks] + [f"guard error: {e}" for e in self.errors]
        return f"{len(self.leaks)} English segment(s) in a Turkish chat:\n" + "\n".join(lines)

    def assert_clean(self) -> None:
        """Fail on what was recorded so far; cleared, so teardown reports only what follows."""
        report, dirty = self.report(), bool(self.leaks or self.errors)
        self.leaks.clear()
        self.errors.clear()
        assert not dirty, report


def install(net: TurkishChatNet, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap the v2 writers and listen for direct construction; undone by ``uninstall``."""
    import app.kria.drafts as drafts
    import app.kria.runtime as runtime
    import app.routes.creation_threads as creation_threads
    import app.tasks.kria_runtime as kria_runtime

    monkeypatch.setattr(
        kria_runtime, "_append_sync_event", net.wrap_sync(kria_runtime._append_sync_event)
    )
    monkeypatch.setattr(runtime, "_append_event", net.wrap_async(runtime._append_event))
    monkeypatch.setattr(
        drafts, "_append_event", net.wrap_async(drafts._append_event, "drafts._append_event")
    )
    # KRI-784: the v1 chat writer (creation_threads and creation_editor_actions).
    monkeypatch.setattr(
        creation_threads,
        "_append",
        net.wrap_async(creation_threads._append, "creation_threads._append"),
    )
    sa_event.listen(CreationThreadEvent, "init", net._on_event_init)
    sa_event.listen(CreationThread, "init", net._on_thread)
    sa_event.listen(CreationThread, "load", net._on_thread)


def uninstall(net: TurkishChatNet) -> None:
    sa_event.remove(CreationThreadEvent, "init", net._on_event_init)
    sa_event.remove(CreationThread, "init", net._on_thread)
    sa_event.remove(CreationThread, "load", net._on_thread)


@pytest.fixture(name="turkish_chat_net")
def turkish_chat_net_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[TurkishChatNet]:
    """The runtime net for one test: fails at teardown on any English in a Turkish chat.

    Import ``turkish_chat_net_fixture`` into a test module and ask for ``turkish_chat_net``.
    """
    net = TurkishChatNet()
    install(net, monkeypatch)
    try:
        yield net
    finally:
        uninstall(net)
    net.assert_clean()
