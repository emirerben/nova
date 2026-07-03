"""Tests for transcript_source (review C3) — the single source of truth for a
variant's words + the staleness hash that drives DESTRUCTIVE read-time clearing
of suggestions. A malformed word record must not silently wipe a user's pending
set, and the matcher's dedicated `overlay_transcript` key must be read without
colliding with the editorial-sequence `transcript` key (review C19).
"""

from __future__ import annotations

from unittest.mock import patch

from app.services.transcript_source import (
    compute_transcript_hash,
    persisted_hash_is_stale,
    transcript_source,
    words_from_variant,
)

_WORDS = [
    {"word": "hello", "start_s": 0.0, "end_s": 0.4},
    {"word": "world", "start_s": 0.4, "end_s": 0.9},
]


class TestComputeTranscriptHash:
    def test_deterministic(self):
        assert compute_transcript_hash(_WORDS, 30.0) == compute_transcript_hash(_WORDS, 30.0)

    def test_sensitive_to_word_text(self):
        other = [{**_WORDS[0], "word": "goodbye"}, _WORDS[1]]
        assert compute_transcript_hash(_WORDS, 30.0) != compute_transcript_hash(other, 30.0)

    def test_sensitive_to_timing(self):
        other = [{**_WORDS[0], "start_s": 0.1}, _WORDS[1]]
        assert compute_transcript_hash(_WORDS, 30.0) != compute_transcript_hash(other, 30.0)

    def test_sensitive_to_duration(self):
        assert compute_transcript_hash(_WORDS, 30.0) != compute_transcript_hash(_WORDS, 31.0)

    def test_len32(self):
        assert len(compute_transcript_hash(_WORDS, 30.0)) == 32


class TestWordsFromVariant:
    def test_reads_transcript_key(self):
        assert words_from_variant({"transcript": _WORDS}) == _WORDS

    def test_reads_overlay_transcript_fallback(self):
        # Review C19: the matcher persists under the dedicated key.
        assert words_from_variant({"overlay_transcript": _WORDS}) == _WORDS

    def test_transcript_key_takes_precedence(self):
        seq = [{"word": "spoken", "start_s": 0.0, "end_s": 0.5}]
        out = words_from_variant({"transcript": seq, "overlay_transcript": _WORDS})
        assert out == seq

    def test_empty_list_is_none(self):
        assert words_from_variant({"transcript": []}) is None

    def test_missing_is_none(self):
        assert words_from_variant({}) is None

    def test_non_dict_entry_invalidates_whole_set(self):
        assert words_from_variant({"transcript": [_WORDS[0], "garbage"]}) is None

    def test_blank_word_is_skipped_not_fatal(self):
        out = words_from_variant(
            {"transcript": [_WORDS[0], {"word": "  ", "start_s": 1, "end_s": 2}]}
        )
        assert out == [_WORDS[0]]

    def test_non_floatable_timing_invalidates(self):
        out = words_from_variant({"transcript": [{"word": "x", "start_s": "nope", "end_s": 1}]})
        assert out is None


class TestTranscriptSource:
    def test_allow_whisper_false_never_transcribes(self):
        with patch("app.services.transcript_source.transcribe_variant_video") as m:
            out = transcript_source({}, allow_whisper=False)
        assert out is None
        m.assert_not_called()

    def test_persisted_words_return_hash(self):
        out = transcript_source({"transcript": _WORDS, "duration_s": 30.0}, allow_whisper=False)
        assert out is not None
        words, h = out
        assert words == _WORDS
        assert h == compute_transcript_hash(_WORDS, 30.0)


class TestPersistedHashIsStale:
    def test_no_stored_hash_is_not_stale(self):
        assert persisted_hash_is_stale({"transcript": _WORDS}) is False

    def test_stored_hash_no_words_is_stale(self):
        # Merge cleared the words but the hash lingers → stale by definition.
        assert persisted_hash_is_stale({"overlay_suggest_hash": "abc"}) is True

    def test_matching_hash_is_not_stale(self):
        h = compute_transcript_hash(_WORDS, 30.0)
        v = {"transcript": _WORDS, "duration_s": 30.0, "overlay_suggest_hash": h}
        assert persisted_hash_is_stale(v) is False

    def test_changed_words_is_stale(self):
        h = compute_transcript_hash(_WORDS, 30.0)
        changed = [{"word": "different", "start_s": 0.0, "end_s": 0.5}]
        v = {"transcript": changed, "duration_s": 30.0, "overlay_suggest_hash": h}
        assert persisted_hash_is_stale(v) is True

    def test_malformed_word_does_not_falsely_wipe_when_no_hash(self):
        # A malformed record makes words_from_variant None, but with NO stored
        # hash there is nothing matched → not stale → the GET clear never fires.
        v = {"transcript": [{"word": "x", "start_s": None, "end_s": 1}]}
        assert persisted_hash_is_stale(v) is False
