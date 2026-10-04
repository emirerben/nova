"""Shared contracts for creator-uploaded songs on phone montages (KRI-374).

A creator attaches their own song to a montage. The prompt decides the mode:

* ``background`` -- the song is the music bed of a normal montage; cuts snap to
  its beats.
* ``lipsync``   -- the song is the fixed master timeline. Each raw take (filmed
  while the song played on another phone) is located in the song and placed at
  its matched song time; camera audio is muted.

Every lane (aligner, ingest, intent, planners/compiler, song-order question,
iOS) builds on these types, so they stay small, additive and free of I/O.

Time convention (the one rule that must never drift): for a take,
``song_time = take_time + delta_s``. A take that starts 12.5 s into the song has
``delta_s == 12.5``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bump when the analysis / alignment shape or algorithm changes so cached rows
# on PlanItem recompute instead of being trusted.
SONG_ANALYSIS_VERSION = 1
SONG_ALIGNMENT_VERSION = 1

SongSync = Literal["background", "lipsync"]
TakeStatus = Literal["confident", "ambiguous", "unmatched"]
SongAnalysisStatus = Literal["pending", "ready", "failed"]

# Wire names shared with the iOS app and the render-asset manifest.
USER_SONG_MEDIA_ROLE = "song"
USER_SONG_AUDIO_MODE = "song"
USER_SONG_AUDIO_STRATEGY = "user_song"
USER_SONG_ASSET_KIND = "song"
USER_SONG_TRACK_ID = "song"
SONG_ORDER_QUESTION_KEY = "song_order_question"
SONG_ORDER_ANSWER_KEY = "song_order"

# Device capabilities the song lane needs; both are already verified in prod.
USER_SONG_REQUIRED_CAPABILITIES = frozenset({"musicBed", "audioMix"})

_STRICT = ConfigDict(extra="forbid")


class SongWord(BaseModel):
    model_config = _STRICT

    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)


class SongLine(BaseModel):
    model_config = _STRICT

    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str


class SongAnalysis(BaseModel):
    """What we learn about the uploaded song once, at attach time."""

    model_config = _STRICT

    version: int = SONG_ANALYSIS_VERSION
    generation: int
    status: SongAnalysisStatus = "pending"
    duration_s: float = Field(default=0.0, ge=0)
    beats_s: list[float] = Field(default_factory=list)
    words: list[SongWord] = Field(default_factory=list)
    lines: list[SongLine] = Field(default_factory=list)
    error: str | None = None

    @property
    def has_lyrics(self) -> bool:
        return bool(self.words)


class AlignmentAlternate(BaseModel):
    model_config = _STRICT

    delta_s: float
    score: float


class TakeAlignment(BaseModel):
    """Where one take sits in the song. ``song_time = take_time + delta_s``."""

    model_config = _STRICT

    media_id: str
    # Generation of the analysis proxy that was aligned, so a re-uploaded clip
    # invalidates its row.
    proxy_generation: int | None = None
    status: TakeStatus
    delta_s: float | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    text_score: float = 0.0
    peak_z: float = 0.0
    peak_ratio: float = 0.0
    alternates: list[AlignmentAlternate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _confident_has_delta(self) -> TakeAlignment:
        if self.status == "confident" and self.delta_s is None:
            raise ValueError("a confident take must carry delta_s")
        return self


class SongAlignment(BaseModel):
    model_config = _STRICT

    version: int = SONG_ALIGNMENT_VERSION
    song_generation: int
    takes: dict[str, TakeAlignment] = Field(default_factory=dict)


class UserSongTake(BaseModel):
    """A take's pinned song offset inside an approved plan."""

    model_config = _STRICT

    delta_s: float
    status: TakeStatus = "confident"
    # True when the creator, not the aligner, chose this position.
    confirmed_by_creator: bool = False


class UserSongPlan(BaseModel):
    """The song portion of an approved edit plan (guided plan / snapshot)."""

    model_config = _STRICT

    mode: SongSync
    plan_item_id: str
    generation: int
    duration_s: float = Field(gt=0)
    window_start_s: float = Field(ge=0)
    window_end_s: float = Field(gt=0)
    takes: dict[str, UserSongTake] = Field(default_factory=dict)
    # The creator's editor volume (KRI-428). Omitted when 1.0 so every plan and
    # approval hash written before this field existed keeps its exact shape.
    volume: float = Field(default=1.0, ge=0, le=1, exclude_if=lambda value: value == 1.0)

    @model_validator(mode="after")
    def _window_inside_song(self) -> UserSongPlan:
        if self.window_end_s <= self.window_start_s:
            raise ValueError("song window must have positive length")
        if self.window_end_s > self.duration_s + 1e-3:
            raise ValueError("song window runs past the end of the song")
        return self

    @property
    def window_duration_s(self) -> float:
        return self.window_end_s - self.window_start_s


class SongOrderItem(BaseModel):
    model_config = _STRICT

    media_id: str
    status: TakeStatus
    # Where we think it sits; None when unmatched.
    song_start_s: float | None = None
    alternates: list[AlignmentAlternate] = Field(default_factory=list)


class SongOrderQuestion(BaseModel):
    """Shown in chat when some takes could not be placed confidently."""

    model_config = _STRICT

    question_id: str
    # Media ids in the order we propose, earliest song position first.
    proposed_order: list[str]
    items: list[SongOrderItem]
    # The song generation the question was asked about. An answer to a question for
    # another generation is ignored (the song changed, so the positions it chose are
    # meaningless) and the creator is asked again. Omitted from the wire when unset so
    # questions written before this field existed serialize byte-identically.
    song_generation: int | None = Field(default=None, exclude_if=lambda value: value is None)


class SongOrderAnswerIn(BaseModel):
    """The creator's answer: the order they confirmed."""

    model_config = _STRICT

    question_id: str
    ordered_media_ids: list[str] = Field(min_length=1)
