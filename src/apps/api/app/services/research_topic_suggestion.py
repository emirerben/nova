"""Offer a public place as a research topic without searching anything.

Heuristic only (no model, no network). The result is shown to the creator as a
prefill; public search runs solely on the topic they confirm.
"""

import re
from collections import Counter

from pydantic import ValidationError

from app.services.researched_text import ResearchRequest

_LANDMARK = {
    "abbey", "acropolis", "amphitheater", "amphitheatre", "bazaar", "basilica", "bay",
    "beach", "bridge", "canyon", "castle", "cathedral", "church", "citadel", "falls",
    "fort", "fortress", "fountain", "garden", "gardens", "harbor", "harbour", "island",
    "lake", "library", "lighthouse", "market", "mausoleum", "memorial", "monastery",
    "monument", "mosque", "mount", "mountain", "museum", "observatory", "palace", "park",
    "pier", "pyramid", "pyramids", "ruins", "square", "temple", "theater", "theatre",
    "tower", "university", "valley",
}  # fmt: skip
_QUERY_PREPOSITIONS = {
    "in", "at", "near", "around", "to", "from", "outside", "inside", "across", "through",
    "visiting", "visit", "exploring", "explore",
}  # fmt: skip
_CLIP_PREPOSITIONS = {"in", "at", "near", "around", "outside", "inside"}
_CONNECTORS = {"of", "de", "di", "du", "del", "la", "el", "al"}
# Capitalised only because a sentence or title started with them.
_NOT_PLACES = {
    "i", "a", "an", "the", "my", "our", "this", "that", "suggest", "visit", "explore",
    "day", "trip", "tour", "walk", "best", "great", "new", "old", "morning", "evening",
    "night", "home", "work", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december", "christmas", "instagram",
    "tiktok", "youtube", "reels", "shorts", "whatsapp", "nova", "kria",
}  # fmt: skip
_TOKEN = re.compile(r"[^\W\d_][\w'’\-]*|\d+|[.!?;:,()\"“”]")
_SENTENCE_BREAK = {".", "!", "?", ":", ";", "(", '"', "“"}


def _clean(token):
    return re.sub(r"['’]s$", "", token)


def _name_like(token):
    return len(token) > 1 and token[0].isupper() and token.lower() not in _NOT_PLACES


def _runs(text):
    """Yield (tokens, introducer, sentence_initial) for each capitalised run."""
    tokens = _TOKEN.findall(text)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not _name_like(token):
            index += 1
            continue
        start = index
        run = [_clean(token)]
        index += 1
        while index < len(tokens):
            nxt = tokens[index]
            if _name_like(nxt):
                run.append(_clean(nxt))
            elif nxt in _CONNECTORS and index + 1 < len(tokens) and _name_like(tokens[index + 1]):
                run.append(nxt)
            else:
                break
            index += 1
        before = [t.lower() for t in tokens[max(0, start - 2) : start]]
        if before[-1:] == ["the"]:
            before = before[:-1] or ["the"]
        introducer = before[-1] if before else None
        initial = start == 0 or tokens[start - 1] in _SENTENCE_BREAK
        yield run, introducer, initial


def _has_landmark(run):
    return len(run) >= 2 and any(word.lower() in _LANDMARK for word in run)


def _valid(topic):
    try:
        ResearchRequest(topic=topic, request_quote=topic)
    except ValidationError:
        return None
    return topic if 4 <= len(topic) <= 200 else None


def suggest_research_topic(query, clip_labels):
    """Best place or landmark named by the creator's query, else by repeated clip labels."""
    ranked = []
    for run, introducer, initial in _runs(query):
        if _has_landmark(run):
            ranked.append((3, len(run), run))
        elif not initial and introducer in _QUERY_PREPOSITIONS:
            ranked.append((2, len(run), run))
    landmarks, introduced = Counter(), Counter()
    for label in clip_labels:
        seen = set()
        for run, introducer, initial in _runs(label):
            name = " ".join(run)
            if name in seen:
                continue
            seen.add(name)
            if _has_landmark(run):
                landmarks[name] += 1
            elif not initial and introducer in _CLIP_PREPOSITIONS:
                introduced[name] += 1
    # A single clip mention of a bare name is too often a person or a brand.
    ranked += [(1, len(name), name.split()) for name in landmarks]
    ranked += [(0, len(name), name.split()) for name, n in introduced.items() if n >= 2]
    for _tier, _size, run in sorted(ranked, key=lambda row: (-row[0], -row[1])):
        if topic := _valid(" ".join(run)):
            return topic
    return None
