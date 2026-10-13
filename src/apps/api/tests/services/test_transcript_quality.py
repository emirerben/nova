"""Degenerate-transcript guard (KRI-653).

Gemini returned "Hello." x1000 and Whisper "Bye! Bye! Bye!" for a take whose microphone
recorded digital silence. A repetition loop is a decoder artifact, never speech. The
guard must also leave real talk alone in English AND Turkish (Unicode tokenization).
"""

from __future__ import annotations

from app.agents.clip_metadata import ClipMetadataAgent, ClipMetadataInput
from app.services.clip_understanding import clip_record, understanding_payload
from app.services.transcript_quality import is_degenerate_transcript

_REAL_EN = (
    "You know sometimes you're working on something and you realize that you're hitting a "
    "wall. Like it's not going anywhere. It's almost going backwards and it's frustrating "
    "and it's tough and you don't know what to do. The best advice I can give you is take a "
    "step back. Take a deep breath. Evaluate. See what's working, what's not working."
)
_REAL_TR = (
    "Günaydın çocuklar. Milli Takım Öğretmenleri kim olacak, biz de bilmiyoruz. Bugün size "
    "yeni bir şey göstereceğim, çünkü geçen hafta çok soru geldi ve hepsine tek tek cevap "
    "vermek yerine hep birlikte bakalım."
)


class TestIsDegenerateTranscript:
    def test_the_incident_gemini_transcript(self) -> None:
        assert is_degenerate_transcript("Hello. " * 1000)

    def test_the_incident_whisper_words(self) -> None:
        assert is_degenerate_transcript("I've got to go. " + "Bye! " * 30)

    def test_alternating_loop_with_tiny_vocabulary(self) -> None:
        assert is_degenerate_transcript("yes no yes no yes no " * 12)

    def test_real_english_talk_is_not_degenerate(self) -> None:
        assert not is_degenerate_transcript(_REAL_EN)

    def test_real_turkish_talk_is_not_degenerate(self) -> None:
        assert not is_degenerate_transcript(_REAL_TR)

    def test_short_repetition_is_below_the_bar(self) -> None:
        assert not is_degenerate_transcript("no no no no no")  # 5 tokens: could be a real shout

    def test_empty_and_non_strings(self) -> None:
        assert not is_degenerate_transcript("")
        assert not is_degenerate_transcript(None)
        assert not is_degenerate_transcript(12345)  # type: ignore[arg-type]


class TestHallucinatedTranscriptNeverClaimsSpeech:
    def test_parse_drops_the_transcript(self) -> None:
        import json

        raw = json.dumps(
            {
                "transcript": "Hello. " * 1000,
                "hook_text": "",
                "hook_score": 1.0,
                "best_moments": [{"start_s": 0, "end_s": 15, "energy": 3, "description": "x"}],
            }
        )
        out = ClipMetadataAgent(model_client=None).parse(raw, ClipMetadataInput(file_uri="files/x"))
        assert out.transcript == ""

    def test_parse_keeps_a_real_transcript(self) -> None:
        import json

        raw = json.dumps(
            {
                "transcript": _REAL_EN,
                "hook_text": "",
                "hook_score": 5.0,
                "best_moments": [{"start_s": 0, "end_s": 6, "energy": 5, "description": "x"}],
            }
        )
        out = ClipMetadataAgent(model_client=None).parse(raw, ClipMetadataInput(file_uri="files/x"))
        assert out.transcript == _REAL_EN

    def test_understanding_payload_has_no_speech(self) -> None:
        class Meta:
            transcript = "Hello. " * 1000
            speaks_to_camera = True

        speech = understanding_payload(Meta())["speech"]
        assert speech["has_speech"] is False
        assert speech["transcript"] == ""

    def test_legacy_stored_analysis_is_projected_without_speech(self) -> None:
        # Analyses stored before the parse-time guard existed.
        record = clip_record({"transcript": "Hello. " * 1000, "subject": "man"})
        assert record.speech.has_speech is False
