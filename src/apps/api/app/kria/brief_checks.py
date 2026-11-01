"""Deterministic per-requirement checks and receipt-driven replies (KRI-188).

No model is involved. Each checker compares one Creative Brief requirement with
facts read from the drafted plan (a strategy or an editor payload) and returns a
``RequirementReceipt``. A requirement nothing could judge (no checker, or the
facts to judge it were missing) gets no receipt: it stays ``open`` and the reply
says nothing about it, so the reply never claims what the server did not check
and never labels an unchecked ask "Partly".

Checks implemented: per-clip text coverage, ordering vs the requested key,
duration within +/-10%, literal on-screen text, "keep my whole take" on a
single-clip subtitled edit, word-triggered pop-ins (reaction beats) plus the
closing shot on a phone Talking edit, and (KRI-546) a finished phone montage's
held closing line and repeated video files.
"""

from __future__ import annotations

import dataclasses
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_args

from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.agents._schemas.text_animation_phases import TEXT_ANIMATION_VOCAB, TextAnimationEdge
from app.kria.animation_ask import animation_ask
from app.kria.asset_holds import HOLD_TOLERANCE_S, hold_seconds
from app.kria.brief import BriefRequirement, CreativeBrief, normalize_style_intent
from app.kria.brief_route import (
    END_KEYS,
    START_KEYS,
    chapter_list,
    first_text,
    fold_text,
    loose_text,
    wants_filming_time_text,
    wants_hour_only_text,
)
from app.kria.clip_sound_toggle_ask import SoundTarget, SoundToggleAsk, requirement_sound_toggle
from app.kria.compose_with_ops import compose_unmet_with_ops
from app.kria.contracts import InferredLabel, RequirementReceipt
from app.kria.freeze_ask import (
    asks_freeze_frame,
    freeze_spec,
    planned_freeze_ids,
    times_one_moment,
)
from app.kria.narrated_clip_sound_ask import AFTER_VOICE, ClipSoundAsk, requirement_clip_sound
from app.kria.noise_reduction_ask import wants_engine_noise_reduction
from app.kria.order_replacement_receipts import VerifiedOrderExecution
from app.kria.reply_language import current_reply_language, say
from app.kria.route_disclosure import is_route_decline_reason
from app.kria.select_asks import ExclusionFact, only_video_ask
from app.kria.speech_cut_ask import SpeechCutAsk, requirement_speech_cut
from app.kria.speed_ask import asks_clip_speed
from app.kria.spoken_text_ask import SPOKEN_TEXT_CUE_KEYS, _fact_speaker
from app.kria.style_asks import (
    MOMENT_SHORT_MAX_S,
    MomentAsk,
    clip_length_ask,
    derive_style_ask,
    moment_ask,
    text_look_facts,
    value_matches,
)
from app.kria.text_shape_ask import CURVED, text_shape
from app.kria.unclear_word_ask import asks_if_unclear
from app.pipeline.caption_language import (
    ASK_END,
    LANGUAGE_NAMES,
    LANGUAGE_WORDS,
    can_deliver_caption_language,
    caption_language_decision,
    caption_language_verdict,
)
from app.pipeline.caption_spelling import extract_spelling_asks, is_mishearing
from app.pipeline.explicit_speech_cut import speech_cut_tokens
from app.pipeline.sequence_text_evidence import complete_sequences, sequence_lineage
from app.pipeline.song_captions import mentions_lipsync
from app.schemas.clip_intents import PLACEHOLDER_LABEL_TEXT
from app.schemas.text_style_intent import (
    LABEL_ANCHORS,
    normalize_label_position,
    normalize_title_animation,
)
from app.services.clip_facts import CAPTURE_ORDER_KEYS
from app.services.clip_order_sequence import (
    row_relations,
    sequence_rows,
    unconfirmed_order_groups,
    unconfirmed_order_reason,
)
from app.services.creator_omissions import creator_omission_outcomes
from app.services.kria_editor_ops_diff import effective_entrance
from app.services.length_policy import (
    creator_duration_satisfied,
    duration_bound,
    length_owner,
    natural_length_owner,
)
from app.services.order_mode import order_mode, seeded_permutation
from app.services.phone_noise_reduction import engine_noise_reduction_available
from app.text_fold import capitalize, uses_turkish_case
from app.text_fold import lower as text_lower

if TYPE_CHECKING:
    from app.agents._schemas.creator_agent import ResolvedCreatorManifest
    from app.kria.opening_cut_ask import TakeWindowEstimate

DURATION_TOLERANCE = 0.10
MAX_REPLY_CHARS = 1200
_CAPTURE_ORDER_KEYS = CAPTURE_ORDER_KEYS  # shared with the render contract and planner
_CAPTURE_BASES = {"capture_time", "route", "capture_order"}


_fold = fold_text

_BRACKETED = re.compile(r"^\s*[\[(<{]\s*(.+?)\s*[\])>}]\s*$")


def _stand_in_core(literal: str | None) -> str | None:
    """The word inside a bracketed stand-in ("[Name]" -> "name"), else None.

    A bracketed literal is a placeholder the creator will replace, not text that is
    printed with its brackets; the server prints the fixed ``PLACEHOLDER_LABEL_TEXT``.
    """
    match = _BRACKETED.match(literal or "")
    return _fold(match.group(1)) if match else None


def _contains_text(haystack: str, wanted: str) -> bool:
    """``wanted`` (already folded) appears in ``haystack`` as whole words.

    Substring matching would call "Go" met by "logo" or "Google Sans".
    """
    if not wanted:
        return False
    return re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", _fold(haystack)) is not None


@dataclass(frozen=True)
class BeatFact:
    """One reaction beat that survives approval's repair (its visual resolved)."""

    trigger: str
    visual_id: str | None = None
    sound: str | None = None
    # Seconds into the render where the pop-in shows (a rendered variant's receipt);
    # None for a draft, which has not been laid out yet.
    at_s: float | None = None
    display_mode: str = "pip"
    following_clip_id: str | None = None
    # None is an approved request, not execution evidence. Only the compiled
    # sequence receipt can prove that the original video actually follows.
    sequence_realized: bool | None = None


@dataclass(frozen=True)
class TimedCardFact:
    """One exact-timed small card: as approval keeps it (a draft, ``status`` "planned") or as
    the phone render placed it (``phone_timed_card_receipt``: "placed" / "unplaced")."""

    card_id: str
    label: str | None = None
    # Seconds into the output where the card shows; None for a clip-anchored draft card.
    at_s: float | None = None
    end_s: float | None = None
    hold_s: float | None = None
    clip_id: str | None = None
    status: str = "planned"
    # The worker's stable reason when unplaced (`past_end`, `face_collision`, ...).
    reason: str | None = None
    timeline_end_s: float | None = None


@dataclass(frozen=True)
class SpokenTextFact:
    """One text-on-a-spoken-word ask as a phone Talking render handled it (KRI-707)."""

    requirement_id: str
    # "placed" | "not_heard" | "no_time" | "error"
    status: str
    text: str = ""
    # The word it waited for: the one heard, else the first one asked for.
    trigger: str = ""
    face_collision: bool = False
    # KRI-724: shown on (or looked for at) the occurrence the creator picked before approval.
    pinned: bool = False


@dataclass(frozen=True)
class ExplicitSpeechCutFact:
    """The phone worker's source-grounded result for one requested spoken phrase cut."""

    requirement_ids: tuple[str, ...]
    quote: str
    occurrence: int | None
    status: str  # "applied" | "not_applied"
    reason: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    word_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class EngineNoiseReductionFact:
    """The phone worker's outcome for an explicit engine-noise treatment."""

    requirement_ids: tuple[str, ...]
    status: str  # "applied" | "not_applied"
    reason: str | None = None


@dataclass(frozen=True)
class SpeechSectionFact:
    """One section of a spoken-excerpt montage as compiled (KRI-282)."""

    kind: str  # "speech" | "montage"
    visual: str = ""  # speech only: "speaker" | "cutaways"
    quote: str = ""
    media_id: str = ""


@dataclass(frozen=True)
class EndpointFact:
    """Where the first or last clip of the plan was filmed (place and landmark texts)."""

    label: str = ""
    places: tuple[str, ...] = ()
    media_id: str = ""


@dataclass(frozen=True)
class NarratedStepFact:
    """One clip of a phone Voiceover edit as the worker placed it (KRI-533).

    ``labels`` are the creator's own words for the shot ("the balloons"), ``placed`` the
    first/last seats a resolved order intent gave it, and ``text`` what the voiceover says
    while the clip is on screen. ``out_of_sequence``: the clip is part of a described
    sequence the voiceover placed in another order (KRI-590).
    """

    media_id: str = ""
    labels: tuple[str, ...] = ()
    placed: tuple[tuple[str, str], ...] = ()  # (spot, name)
    start_s: float = 0.0
    end_s: float = 0.0
    text: str = ""
    out_of_sequence: bool = False


@dataclass(frozen=True)
class TextStyleRow:
    """One non-caption on-screen text row's style as saved (KRI-543).

    ``kind`` is the editor's own title/label/text classification. A style field is None
    when the saved row does not say (an unset font or color falls back to a renderer
    default we do not read), so a check on it stays "can't verify" instead of guessing.
    """

    id: str
    kind: str = "text"
    entrance: str | None = None
    alignment: str | None = None
    text_case: str | None = None
    font_family: str | None = None
    color: str | None = None
    # KRI-558: where the row sits and what it says, so a label-corner or "the Lisbon text"
    # ask can be judged. ``position`` is the named spot; x/y only mean something for "custom".
    size_px: float | None = None
    position: str | None = None
    x_frac: float | None = None
    y_frac: float | None = None
    clip_id: str | None = None
    text: str = ""
    heading_level: int | None = None
    stack_id: str | None = None
    layout_row: dict | None = None
    canvas_width: int = 1080
    canvas_height: int = 1920
    # KRI-1029: the row's saved ``bend`` (KRI-646); None = drawn straight.
    bend: float | None = None


@dataclass(frozen=True)
class ClosingSpeechFact:
    """The spoken line a finished phone montage holds whole on its closing clip (KRI-546).

    Read off the plan record (`unified_montage.closing_speech`, recorded only when the
    creator's own "last" ask seated the clip, it speaks and the camera audio is kept) and
    confirmed against the finished timeline: the clip is last and its cut covers the line.
    """

    media_id: str
    start_s: float
    end_s: float
    # KRI-597: the creator's quoted words, when the held line is a line they quoted.
    quote: str = ""


@dataclass(frozen=True)
class NamedLineFact:
    """A spoken line the creator wanted heard whole, and what the montage did with it (KRI-760).

    ``status`` is "held" (the clip that says it is in the edit and its cut covers the line),
    "not_held" (it is not, or the cut is shorter than the line), "ambiguous" (two clips say it
    equally) or "not_found" (no clip's words have it).
    """

    quote: str
    status: str
    media_id: str = ""
    start_s: float | None = None
    end_s: float | None = None


@dataclass(frozen=True)
class EndingClipFact:
    """The video a phone Talking edit ends on, with its own sound (KRI-841).

    ``status`` is "planned" (a draft whose repaired strategy keeps it), "applied" (the finished
    video really ends on it: ``volume`` is read back from the compiled recipe) or "dropped"
    (the server repair or the phone worker could not append it; ``reason`` is a machine code).
    """

    status: str
    keep_audio: bool = True
    volume: float | None = None
    reason: str | None = None
    # Whether the ending's source has an audio track at all (the worker's `has_audio`):
    # False means "its own sound" is silence; None = the render did not say.
    has_audio: bool | None = None


@dataclass(frozen=True)
class PlanFacts:
    """What a drafted plan verifiably contains. Missing facts stay None/empty."""

    clip_ids: tuple[str, ...] = ()
    title: str | None = None
    per_clip_text: dict[str, str] = field(default_factory=dict)
    inferred_text: dict[str, str] = field(default_factory=dict)
    positional_labels: tuple[str, ...] = ()
    # KRI-296: the clips the creator described and gave text for. When set, "text
    # on each shot" is judged against these clips, not every clip in the plan.
    label_scope_clip_ids: tuple[str, ...] = ()
    # KRI-1110: before a render, the clips each creator-worded shot text was placed on (its
    # settled label/caption intent), keyed by `_placement_key` of the words. A dictated shot
    # text is judged on its own placement, never on every upload.
    shot_text_clip_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    duration_s: float | None = None
    ordering_basis: str | None = None
    ordering_fallback_clip_ids: tuple[str, ...] = ()
    # KRI-282: the creator's answer to a chronological-vs-grouped conflict, if any.
    ordering_choice: str | None = None
    # 29 Ekim: a "random order" plan records the seed it used and the clips it shuffled, so a
    # receipt can prove the result is a permutation of exactly the selected clips.
    random_order_seed: int | None = None
    random_order_source_ids: tuple[str, ...] = ()
    # KRI-458: how each described group ("start with the football, end at the pub")
    # landed in a unified montage: one status per sequence intent. Empty = none asked.
    sequence_statuses: tuple[str, ...] = ()
    # KRI-503: the groups of that stated sequence that did NOT land where the creator said,
    # as the creator's own words and the spot ("the video that is blue (first)"). Read off
    # the finished order like `sequence_statuses`; empty = every stated group landed.
    sequence_unmet: tuple[str, ...] = ()
    sequence_unconfirmed: tuple[str, ...] = ()
    # ... and the stated groups none of whose clips are in the edit (deselected, or no match):
    # absent, not misplaced. A group made only of clips an earlier group already seated, or of
    # Visuals-pool items, is neither (the planner outcome's ``code`` says which).
    sequence_absent: tuple[str, ...] = ()
    # KRI-764: stated first / last groups that name a clip an earlier group already seated at
    # the OTHER spot (one clip asked to be first and last). The earlier claim stands and the
    # edit still renders, so this is a note (partial), never a failure. Known at draft time.
    sequence_conflicts: tuple[str, ...] = ()
    # KRI-723: the stated moments the plan cannot show are those actions (no evidence: an app
    # that could not ask), or that the creator left to Kria / said are not in their clips, by
    # the outcome's ``code`` (``not_located`` / ``delegated`` / ``skipped``). Honest limits the
    # creator already knows about: noted (partial), never a failure.
    sequence_unlocated: tuple[str, ...] = ()
    sequence_unlocated_intent_ids: tuple[str | None, ...] = ()
    verified_order_execution: VerifiedOrderExecution | None = None
    # KRI-522: the spots ("first" / "last" / "then") whose stated sequence group landed,
    # read off the same outcomes. None = this plan cannot say (not a unified montage), so a
    # stated first/last clip stays unjudged there instead of failing a plan that seated it.
    sequence_spots_met: tuple[str, ...] | None = None
    texts: tuple[str, ...] = ()
    # 29 Ekim: the whole text of every complete fragment sequence on screen (a title shown a
    # piece at a time), so a `text_sequence` ask is judged by how the words are SHOWN, not by
    # the sentence appearing somewhere. Empty = none, or this facts source never has it.
    sequence_texts: tuple[str, ...] = ()
    # KRI-945: pinned contract texts the render puts on screen whatever the plan holds
    # (`phone_plan_repair.contract_text_drawn`). Empty = no such writer, or not known.
    contract_drawn_texts: tuple[str, ...] = ()
    # KRI-945: the research done before approval (`PreparedResearch` as JSON), pinned to the
    # approval with it. None = none was done (or this facts source never has it).
    research: Mapping[str, Any] | None = None
    # Where the title came from: "creator" (their words), "brief" (written from the
    # brief's facts), "default" (nothing to title with), None = unknown.
    title_source: str | None = None
    # Clips whose label was left off because it repeated the previous one (KRI-210).
    repeat_label_clip_ids: tuple[str, ...] = ()
    # The route the creator stated and where the first/last clips were filmed (KRI-208).
    route_start: str | None = None
    route_end: str | None = None
    first_endpoint: EndpointFact | None = None
    last_endpoint: EndpointFact | None = None
    # Seconds the phone appended after the edit (the Kria outro) when the record says so;
    # never in the plan's own length: a 28.0s plan is a ~29.6s file (KRI-210). 0 = unknown.
    outro_s: float = 0.0
    # Measured complete exported file, not the recipe's edit duration plus an estimate.
    measured_export_duration_s: float | None = None
    # KRI-190: clips whose label is on a cut shorter than its reading time (the
    # clip itself is too short). A label the viewer cannot read is not "met".
    unreadable_label_clip_ids: tuple[str, ...] = ()
    # KRI-219: what grounded each clip's label ("capture_time", "place", "landmark",
    # ...; absent = unknown), and the zone filming hours were printed in ("" = none).
    per_clip_label_kinds: dict[str, str] = field(default_factory=dict)
    label_timezone: str = ""
    label_timezone_basis: str = ""
    # True when the facts come from an editor payload, which carries literal
    # on-screen text only (no per-clip structure, order or duration).
    editor: bool = False
    # True when the facts describe a plan the server actually rendered (a unified or
    # spoken-excerpt montage record), not a draft that has not been laid out yet. A
    # required order a rendered plan cannot show is a failure; a draft's is pending.
    rendered_output: bool = False
    # KRI-541: True when the facts were read off a finished phone render
    # (`plan_facts_from_phone_variant`). `editor` stays True for its text lanes, but the
    # edit format, caption style and speech cleanup below are what the render did, so the
    # captions and cleanup checkers judge them instead of answering "can't check".
    rendered_variant: bool = False
    # True only where the order requirement is bound to an authority that can verify it
    # (a contract-stamped job, or a brief-binding cohort: `build_receipts` sets it from the
    # writer's binding). Legacy / unbound jobs keep the original, softer order verdicts.
    strict_order: bool = False
    # KRI-218: True when the editor facts carry a per-clip text diff (`per_clip_text`,
    # `clip_ids` filled from what the turn actually changed), so per-clip text can be
    # judged for real instead of "can't verify".
    has_clip_structure: bool = False
    # True when an editor payload carries restyled/edited on-screen text elements.
    editor_text_edited: bool = False
    # Compiled text spans, when the editor payload includes timing and identity.
    # Each row is (element id, role, start_s, end_s); an empty tuple means timing
    # was not available and must remain unchecked.
    text_spans: tuple[tuple[str, str, float, float], ...] = ()
    text_timing_incomplete: bool = False
    # Per-row style of the saved non-caption text rows (KRI-543). None means the text lane
    # was not available (a draft or render), which keeps a style ask unchecked.
    text_styles: tuple[TextStyleRow, ...] | None = None
    # KRI-558: on an editor turn, the ids of the text rows the creator's ops changed. A style
    # ask that names no target ("…to all of them") is judged on exactly these rows. None =
    # not an editor turn (a draft or a render judges every row).
    anaphora_rows: tuple[str, ...] | None = None
    # KRI-558: each clip's output length in screen order (removed clips left out). None =
    # unknown, which keeps a per-clip length ask unjudged (never a false failure).
    clip_output_durations: tuple[float, ...] | None = None
    # The strategy's edit format, and how many video clips it renders (None when
    # the footage list was not available to count).
    edit_format: str | None = None
    video_clip_count: int | None = None
    # KRI-705: True when this draft / finished video is a phone Talking edit, which draws only
    # a title, captions, a start cut and speech cleanup; an ask outside that set is answered
    # with a plain "can't" before approval and after render (`_talking_honest_receipt`).
    phone_talking: bool = False
    phone_voiceover_spoken_text: bool = False
    # KRI-793: True when this draft renders through the phone Talking writer, which captions
    # the take, spells the creator's listed words their way and records every caption line
    # (`defers_caption_words_to_phone_render`). A captions ask about language or spelling
    # then gets a pre-approval line saying what the render will do, never silence.
    caption_words_at_render: bool = False
    # KRI-1024: True when this draft renders on the phone, in any format
    # (`defers_text_to_phone`). A creator literal no phone font draws is then a checked
    # "Couldn't" naming the characters before approval (`undrawable_phone_text`).
    phone_text: bool = False
    # Seconds of footage the draft would keep whole (the selected video clips' source length);
    # None = unknown, so the length line never states a number it was not given.
    take_duration_s: float | None = None
    # The item's opt-in Speech cleanup toggle (None = unknown): when on, a render
    # may cut pauses and retakes out of the take.
    speech_cleanup_enabled: bool | None = None
    # Whether approving this draft offers "Clean up speech" (the preflight check is
    # enforced and this project is in its cohort). None = unknown, never assumed.
    speech_cleanup_offered: bool | None = None
    # KRI-849: what a phone Talking draft / finished video does about the unclear word the
    # creator asked to be asked about (``facts["ask_if_unclear"]``): ``{"state": "agree" |
    # "not_comparable" | "applied", "spans": [{start_s, end_s, action, text?}], "finished"?}``
    # (``unclear_word_ask.draft_state``, ``variant["caption_unclear"]``). None = no ask or not
    # a phone Talking edit. Never holds the heard words.
    unclear_words: Mapping[str, Any] | None = None
    # What speech cleanup did on a rendered variant: "applied" (pauses were cut),
    # "no_change" (it ran and found nothing to cut), "not_run" (the render kept the whole
    # take). None = unknown or a draft.
    speech_cleanup_outcome: str | None = None
    # KRI-??? Explicit source phrase removals.  Empty means the finished variant recorded no
    # receipt; that absence is never evidence a phrase was removed.
    explicit_speech_cuts: tuple[ExplicitSpeechCutFact, ...] = ()
    engine_noise_reduction: EngineNoiseReductionFact | None = None
    # The strategy's caption style ("none", "clean", "kinetic", "karaoke",
    # "editorial", "auto"); None when the draft carries no strategy.
    caption_style: str | None = None
    # Reaction beats (KRI-178) as approval will keep them: resolved against the
    # creator's owned images by the same repair the compiler runs. None = not
    # checked (no manifest was supplied), so nothing about beats is claimed.
    reaction_beats_available: bool | None = None
    reaction_beats: tuple[BeatFact, ...] | None = None
    # Triggers of beats approval drops because their photo/sticker didn't resolve.
    dropped_beat_triggers: tuple[str, ...] = ()
    # KRI-537: triggers the render never heard in the creator's voice (a phone variant's
    # `phone_beat_receipt.unplaced[]`), so "never said" is told apart from "no room".
    unheard_beat_triggers: tuple[str, ...] = ()
    # KRI-550: (trigger, placed, heard) for each "every time I say X" beat the render placed
    # on only some of the times it was said (`unplaced[]` reason "occurrence_cap").
    capped_beat_hits: tuple[tuple[str, int, int], ...] = ()
    # The strategy's audio strategy ("voiceover", "original_audio", ...); None = unknown.
    audio_strategy: str | None = None
    # KRI-983: how a `user_song` strategy plays the creator's song ("lipsync" / "background"),
    # and whether their song is attached to the draft's pinned media. None = unknown.
    song_sync: str | None = None
    user_song_attached: bool | None = None
    # The strategy's render program ("native", "guided"); None = unknown. KRI-889: a
    # guided story's voiceover is not cut around named lines.
    render_program: str | None = None
    # How loud the footage's own sound plays under the creator's voice (0..1; 1.0 = full
    # volume). None = unknown, never guessed.
    voiceover_bed_level: float | None = None
    # KRI-821: how the creator can change the footage's own sound level on THIS variant:
    # "editor" = the live capability map for this app build opens Sounds > Original audio
    # (the chat edit works too); "chat" = a phone Voiceover edit whose level a chat edit
    # recompiles (`set_mix original_level`), app build unknown; None = no control (cloud
    # render, unknown). Never guessed: a receipt names only the control recorded here.
    original_audio_control: str | None = None
    # KRI-994: the clips each live `select` ask's creator-settled exclusions left out
    # (`select_asks.exclusion_facts`). Empty = none linked, so no exclusion is judged.
    exclusions: tuple[ExclusionFact, ...] = ()
    # KRI-994: the media ids of every non-video cut in the finished timeline; () = none (all
    # cuts are videos). None = no finished timeline here, so "clips only" is not judged.
    timeline_non_video_ids: tuple[str, ...] | None = None
    closing_requested: bool = False
    closing_visual_id: str | None = None
    closing_badge_requested: bool = False
    closing_badge_id: str | None = None
    # KRI-282: the spoken-excerpt montage as the compiler laid it out, in order.
    # None = this plan is not a spoken-excerpt montage (nothing about speech is claimed).
    speech_sections: tuple[SpeechSectionFact, ...] | None = None
    # Quotes the planner chose that were not found in the clip's speech.
    speech_dropped_quotes: tuple[str, ...] = ()
    # KRI-533: a phone Voiceover edit as the worker laid it out, clips in screen order.
    # None = this plan is not a rendered Voiceover edit (nothing about it is claimed).
    narrated_steps: tuple[NarratedStepFact, ...] | None = None
    # The language the rendered captions are in, and the language that was spoken.
    caption_language: str | None = None
    spoken_language: str | None = None
    # KRI-1015: the spoken language a phone Talking render translated its captions out of
    # (`variant["caption_translated_from"]`, written only when it did). None = not translated.
    caption_translated_from: str | None = None
    # KRI-1015: another live requirement asked for this caption language, so the translation is
    # no miss on a captions ask that names none (the language row says where it came from).
    caption_translation_asked: bool = False
    # KRI-547: how a phone Talking render framed a sideways speaker clip for a
    # "vertical / keep my face in frame" ask (`variant["speaker_framing"]`): "face_fill",
    # "letterbox" or "centre_fill", and why. None = no framing receipt (no such ask, a
    # draft, or a render that does not record one): nothing about framing is claimed.
    speaker_framing: str | None = None
    speaker_framing_reason: str | None = None
    # The share (0..1) of the take the speaker's face was found in: `faces.presence` (receipt
    # v2), else `faces.used / faces.decoded` (v1). None = the receipt did not say.
    speaker_framing_face_share: float | None = None
    # Seconds of the kept take with no speaker face in view, out of shot or turned away
    # (`faces.absent_s`, receipt v2). None = not recorded.
    speaker_framing_absent_s: float | None = None
    # KRI-1027: how a finished phone montage framed its sideways clips for a bars/crop ask
    # (`variant["landscape_framing"]`, `phone_montage_framing`) and the bars/crop the variant
    # renders now (an editor Save can change it). None = no receipt: nothing is claimed.
    landscape_framing: Mapping[str, Any] | None = None
    variant_landscape_fit: str | None = None
    # KRI-628: whether a phone Talking render started at the line the creator quoted
    # (`variant["opening_cut"]`): "applied" or "not_applied", and why not. None = no
    # receipt (no such ask, a draft, or an older render): nothing is claimed.
    # KRI-821: a phone Talking render's named-line cuts (`variant["named_cuts"]`, one receipt
    # per line the creator named). None = no such record: nothing about a named cut is claimed.
    named_cuts: tuple[Mapping[str, Any], ...] | None = None
    opening_cut: str | None = None
    opening_cut_reason: str | None = None
    opening_cut_quote: str | None = None
    # KRI-790: the same for the line the creator quoted to end on (`variant["end_cut"]`).
    end_cut: str | None = None
    end_cut_reason: str | None = None
    end_cut_quote: str | None = None
    # KRI-790: where a one-take phone Talking draft's quoted start / end lines land in the take
    # (`opening_cut_ask.estimate_take_window`). None = no such ask, or not estimated.
    take_window: TakeWindowEstimate | None = None
    # KRI-706: True when the rendered opening title still sits on the speaker's face
    # (`variant["opening_title_placement"]["face_collision"]`). None = no such receipt.
    title_face_collision: bool | None = None
    # KRI-827: per requirement that carries "no text on <face|object>", what a phone Talking
    # render's protected-text receipt says (`variant["protected_text_regions"]`):
    # (requirement_id, verdict, labels). None = no such receipt: nothing is claimed.
    protected_text: tuple[tuple[str, str, tuple[str, ...]], ...] | None = None
    # KRI-707: how a phone Talking render handled each text-on-a-spoken-word ask
    # (`variant["spoken_text_placement"]`). None = no such receipt (no such ask, a draft,
    # or an older render): nothing is claimed.
    spoken_texts: tuple[SpokenTextFact, ...] | None = None
    # KRI-724: the creator's server-owned ``speech_occurrence`` answers on the draft's strategy
    # (which time a spoken word was meant), so the draft says the picked time.
    speech_occurrence_answers: tuple[Mapping[str, Any], ...] = ()
    # KRI-547: pop-ins the render dropped for lack of room, as (trigger, "no_room") when
    # no spot cleared the face and captions, or (trigger, "overlap") when another pop-in
    # held the spot at that moment (`phone_beat_receipt.unplaced[]`).
    beat_room_drops: tuple[tuple[str, str], ...] = ()
    # KRI-549: the caption lines a finished phone render shows (its `caption_cues` text, in
    # order); () = it shows none. None = unknown (a draft, an editor edit, a cloud render).
    caption_texts: tuple[str, ...] | None = None
    # KRI-546: a finished phone unified montage, judged from its plan record and the render
    # together (`plan_facts_from_rendered_montage`). Only there do the closing-line and
    # duplicate-video checks below judge anything; every other plan keeps today's answers.
    rendered_montage: bool = False
    # KRI-828: the creator's freeze as the plan carries it, per requirement id:
    # {status ("held" / "not_grounded" / "dropped"), hold_s, hold_stated, frame_source_s}.
    # Empty when no plan could carry a freeze (the KRI-777 "Couldn't" stands).
    freeze_frames: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # The closing clip's whole spoken line, held by the plan and confirmed on the finished
    # timeline. None = no line was held (or the render does not show it held).
    closing_speech: ClosingSpeechFact | None = None
    # False when the render records that the clips' own sound was dropped; None = unknown.
    source_audio_kept: bool | None = None
    # KRI-597: what became of the line the creator quoted to end on ("found" / "not_found"
    # / "too_long" / "not_held"), with their words. None = no quoted ending was asked.
    closing_quote_status: str | None = None
    closing_quote_text: str = ""
    # KRI-596: every main-picture cut's length, in screen order. None = unknown.
    cut_lengths_s: tuple[float, ...] | None = None
    # KRI-624: the clip each of those cuts plays, in the same order, and each footage clip's
    # own length (media id, seconds). None = unknown.
    cut_clip_ids: tuple[str, ...] | None = None
    source_durations_s: tuple[tuple[str, float], ...] | None = None
    # KRI-624: the plan lays a creator's song under the clips (their own sound is not alone).
    song_bed: bool = False
    # Every clip the creator added to this edit (footage and Visuals), whether or not the
    # edit used it, so "that clip isn't in the edit" is told apart from an unknown clip id.
    source_clip_ids: tuple[str, ...] = ()
    # The finished edit's clips (1-based, in screen order) that play the same original file
    # (same upload fingerprint), one group per file shown more than once; and how many extra
    # copies the creator's own uploads held. None = some clip had no fingerprint, so
    # nothing about duplicates is claimed.
    duplicate_clip_positions: tuple[tuple[int, ...], ...] | None = None
    source_duplicate_copies: int | None = None
    # KRI-569: a song montage that carries the creator's song lyrics as captions
    # ("placed"), and how they show ("word" one at a time, "sentence" whole lyric lines).
    # None = this plan placed no lyric captions (nothing about them is claimed).
    montage_captions: str | None = None
    montage_caption_style: str | None = None
    # KRI-760: the lines the creator asked to hear whole, as the plan record holds them and the
    # finished timeline confirms. None = this plan recorded none (nothing is claimed).
    named_lines: tuple[NamedLineFact, ...] | None = None
    # KRI-824: the photos the creator gave their own seconds, as the plan record holds them
    # ({requirement_id, media_id, hold_s, rendered_s}); a finished timeline's own still
    # lengths replace ``rendered_s``. None = no montage record to read (nothing is claimed).
    asset_holds: tuple[Mapping[str, Any], ...] | None = None
    # KRI-689: True when the plan was laid out by a phone unified montage that could not retime
    # a clip (a record written before speed asks were planned: no ``clip_speeds`` key). A
    # clip-speed ask is then a known miss; any other plan claims nothing about speed.
    speed_unsupported: bool = False
    # KRI-1029: the phone montage plan's curved-text outcomes (record ``text_shapes``), as
    # (folded text, role, reason): reason None = drawn curved, "phone" = this build / server
    # can't bend text, "animation" = the title's entrance can't bend. None = no record row
    # to read (nothing is claimed).
    text_shapes: tuple[tuple[str, str, str | None], ...] | None = None
    # KRI-689: the plan record's clip-speed rows (``clip_speeds``), keyed by requirement id:
    # {status ("applied" / "locked" / "not_grounded" / "photo" / "ramp" / "replaced" /
    # "dropped"), rate, rate_requested, rate_stated, cut_ids, cut_positions, media_ids,
    # grounding, reason, footage_limited, replaced_by}. None = no record to read (nothing is
    # claimed); a finished video's timeline confirms each applied row (``_rendered_speeds``).
    clip_speeds: Mapping[str, Mapping[str, Any]] | None = None
    # KRI-840: the plan shows the creator's Visuals full screen at evenly spaced slots across the
    # talk (`overlay_display` "fullscreen*", or a render's overlay receipt of that layout), not
    # at a spoken word. A word-timed full-screen ask that only this covers is a "partial".
    fullscreen_sequence: bool = False
    # KRI-841: a video appended after the talk. ``ending_clip_known`` is True where the facts
    # can speak to the ending at all (a draft read against its manifest, or a finished Talking
    # render); only then does None mean "the edit does not end on a video". The draft reads
    # the REPAIRED strategy, the render its own `phone_ending_clip` receipt, never the strategy.
    ending_clip: EndingClipFact | None = None
    ending_clip_known: bool = False
    # KRI-842: a draft that renders through the phone Voiceover compiler, the only writer that
    # can play ONE clip's own sound under the voice or after it; the approved media rows its
    # clip-sound asks are grounded in; and the clip its order intents seat last (None = none
    # pinned). Only a draft sets them; nothing about clip sound is claimed without them.
    phone_narrated: bool = False
    media_snapshot: Mapping[str, Any] | None = None
    last_clip_id: str | None = None
    # KRI-842: a finished phone Voiceover's per-clip sound receipt (`variant["clip_sound"]`,
    # `phone_clip_sound`): what its pinned recipe plays for each clip-sound ask. None = no
    # receipt (older render, or no such ask): nothing is claimed.
    clip_sound: Mapping[str, Any] | None = None
    # KRI-842: a finished phone montage Voiceover (`resolved_archetype == "voiceover"`), which
    # cannot play one clip's sound under or after the voice.
    voiceover_edit: bool = False
    # KRI-942: a draft the phone montage writer will render (`defers_to_unified_montage`), the
    # writer that turns clips' own sound on and off where the creator says; and the on/off
    # asks judged against the recipe a finished phone render pinned (`variant[
    # "clip_sound_toggles"]`, re-judged by the render-ready review). None = nothing claimed.
    phone_montage: bool = False
    clip_sound_toggles: Mapping[str, Any] | None = None
    # Exact-timed small cards (flag PHONE_TIMED_CARDS_ENABLED): whether the edit can draw one
    # at all (None = not checked), and the cards as approval keeps them (a draft, repaired
    # against the manifest) or as the phone render placed them (`phone_timed_card_receipt`).
    timed_cards_available: bool | None = None
    timed_cards: tuple[TimedCardFact, ...] | None = None

    @property
    def reaction_beat_count(self) -> int | None:
        return None if self.reaction_beats is None else len(self.reaction_beats)

    @property
    def beat_visual_ids(self) -> tuple[str, ...]:
        return tuple(b.visual_id for b in self.reaction_beats or () if b.visual_id)

    @property
    def beat_sound_triggers(self) -> tuple[str, ...]:
        return tuple(b.trigger for b in self.reaction_beats or () if b.sound)


def _beat_facts(strategy: Mapping[str, Any], manifest: ResolvedCreatorManifest) -> dict[str, Any]:
    """Reaction-beat and closing-shot facts, resolved exactly as approval will.

    The draft strategy carries the model's raw references; approval runs
    ``repair_creator_reaction_beats`` against this same manifest and drops any
    beat whose photo/sticker doesn't resolve (or all of them when the capability
    is off). Running that repair here keeps the receipt honest about what renders.
    """
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.services.creator_capabilities import (  # noqa: PLC0415
        CAPABILITY_REACTION_BEATS,
        ending_clip_problem,
        repair_creator_ending_clip,
        repair_creator_reaction_beats,
    )

    capability = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
    available = bool(capability is not None and capability.available)
    try:
        parsed = CreativeStrategy.model_validate(dict(strategy))
    except ValueError:
        return {"reaction_beats_available": available}
    repaired, _notices = repair_creator_reaction_beats(manifest, parsed)
    # A beat the creator named but the beats repair dropped (its photo didn't resolve).
    kept_ids = {beat.beat_id for beat in repaired.reaction_beats or []}
    # KRI-841: the ending video is repaired after the beats (and takes its own video out of
    # them), exactly as `compile_strategy_to_plan` orders it.
    repaired, _ending_notices = repair_creator_ending_clip(manifest, repaired)
    ending: EndingClipFact | None = None
    if repaired.ending_clip is not None:
        ending = EndingClipFact("planned", keep_audio=repaired.ending_clip.keep_audio)
    elif parsed.ending_clip is not None:
        ending = EndingClipFact("dropped", reason=ending_clip_problem(manifest, parsed))
    kept = list(repaired.reaction_beats or [])
    raw_closing = parsed.closing_media
    closing = repaired.closing_media
    return {
        "reaction_beats_available": available,
        "reaction_beats": tuple(
            BeatFact(
                trigger=b.trigger,
                visual_id=b.visual_id,
                sound=b.sound,
                display_mode=b.display_mode,
                following_clip_id=b.following_clip_id,
            )
            for b in kept
        ),
        "dropped_beat_triggers": tuple(
            dict.fromkeys(
                b.trigger for b in parsed.reaction_beats or [] if b.beat_id not in kept_ids
            )
        ),
        "closing_requested": raw_closing is not None,
        "closing_visual_id": closing.visual_id if closing is not None else None,
        "closing_badge_requested": bool(raw_closing and raw_closing.badge_visual_id),
        "closing_badge_id": closing.badge_visual_id if closing is not None else None,
        "ending_clip": ending,
        "ending_clip_known": True,
    }


def _timed_card_facts(strategy: Mapping[str, Any], manifest: ResolvedCreatorManifest) -> dict:
    """Exact-timed small cards as approval will keep them: the same repair
    `compile_strategy_to_plan` runs (unavailable capability -> none; unresolved visual or
    clip -> dropped), so the draft receipt never promises a card the render cannot draw."""
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.services.creator_capabilities import (  # noqa: PLC0415
        CAPABILITY_TIMED_CARDS,
        repair_creator_timed_cards,
    )

    capability = manifest.capabilities.get(CAPABILITY_TIMED_CARDS)
    available = bool(capability is not None and capability.available)
    try:
        parsed = CreativeStrategy.model_validate(dict(strategy))
    except ValueError:
        return {"timed_cards_available": available}
    if not parsed.timed_cards:
        return {"timed_cards_available": available, "timed_cards": ()}
    repaired, _notices = repair_creator_timed_cards(manifest, parsed)
    kept = {card.card_id: card for card in repaired.timed_cards or []}
    facts: list[TimedCardFact] = []
    for card in parsed.timed_cards:
        survivor = kept.get(card.card_id)
        if survivor is None:
            facts.append(
                TimedCardFact(
                    card_id=card.card_id,
                    label=card.label,
                    at_s=card.at_s,
                    hold_s=card.hold_s,
                    clip_id=card.clip_id,
                    status="unplaced",
                    reason="lane_disabled" if not available else "visual_not_found",
                )
            )
            continue
        facts.append(
            TimedCardFact(
                card_id=survivor.card_id,
                label=survivor.label,
                at_s=survivor.at_s,
                hold_s=survivor.hold_s,
                clip_id=survivor.clip_id,
                status="planned",
            )
        )
    return {"timed_cards_available": available, "timed_cards": tuple(facts)}


def _timed_card_receipt_facts(receipt: object) -> dict[str, Any]:
    """Cards off a rendered phone variant's ``phone_timed_card_receipt`` (``{}`` when none)."""
    if not isinstance(receipt, Mapping) or receipt.get("matcher") not in ("timed", "disabled"):
        return {}
    end = _finite_number(receipt.get("timeline_end_s"))
    facts: list[TimedCardFact] = []
    for entry in receipt.get("placed") if isinstance(receipt.get("placed"), list) else []:
        if not isinstance(entry, Mapping) or not entry.get("card_id"):
            continue
        facts.append(
            TimedCardFact(
                card_id=str(entry["card_id"]),
                label=str(entry.get("label") or "") or None,
                at_s=_finite_number(entry.get("at_s")),
                end_s=_finite_number(entry.get("end_s")),
                hold_s=_finite_number(entry.get("hold_s")),
                status="placed",
                timeline_end_s=end,
            )
        )
    for entry in receipt.get("unplaced") if isinstance(receipt.get("unplaced"), list) else []:
        if not isinstance(entry, Mapping) or not entry.get("card_id"):
            continue
        facts.append(
            TimedCardFact(
                card_id=str(entry["card_id"]),
                label=str(entry.get("label") or "") or None,
                at_s=_finite_number(entry.get("at_s")),
                status="unplaced",
                reason=str(entry.get("reason") or "") or None,
                timeline_end_s=_finite_number(entry.get("timeline_end_s")) or end,
            )
        )
    return {
        "timed_cards_available": receipt.get("matcher") != "disabled",
        "timed_cards": tuple(facts),
    }


def _draft_fullscreen_sequence(
    strategy: Mapping[str, Any], manifest: ResolvedCreatorManifest
) -> bool:
    """KRI-840: whether approving this draft lays the Visuals out full screen in even slots."""
    from app.agents._schemas.creator_agent import FULLSCREEN_OVERLAY_DISPLAYS  # noqa: PLC0415
    from app.services.creator_capabilities import (  # noqa: PLC0415
        CAPABILITY_MEDIA_OVERLAY_FULLSCREEN,
    )

    capability = manifest.capabilities.get(CAPABILITY_MEDIA_OVERLAY_FULLSCREEN)
    return bool(
        capability is not None
        and capability.available
        and strategy.get("overlay_display") in FULLSCREEN_OVERLAY_DISPLAYS
    )


def _video_clip_count(strategy: Mapping[str, Any], manifest: ResolvedCreatorManifest) -> int:
    videos = [m.media_id for m in manifest.media if m.kind == "video"]
    selected = {str(x) for x in strategy.get("selected_media_ids") or []}
    if selected and strategy.get("media_scope") != "all":
        videos = [media_id for media_id in videos if media_id in selected]
    return len(videos)


def _selected_video_seconds(
    strategy: Mapping[str, Any], manifest: ResolvedCreatorManifest
) -> float | None:
    """Source seconds of the video clips the strategy uses; None when any length is unknown."""
    selected = {str(x) for x in strategy.get("selected_media_ids") or []}
    scoped = bool(selected) and strategy.get("media_scope") != "all"
    videos = [
        m
        for m in manifest.media
        if m.kind == "video" and not (scoped and m.media_id not in selected)
    ]
    if not videos or any(m.duration_s is None for m in videos):
        return None
    return float(sum(m.duration_s for m in videos))


def _pinned_text_values(pins: object) -> list[str]:
    """KRI-523: the exact lines of whole-video corner text (strategy or unified record)."""
    return [
        str(pin["text"])
        for pin in (pins if isinstance(pins, list) else [])
        if isinstance(pin, Mapping) and pin.get("text")
    ]


def _placement_key(text: object) -> str:
    # The render contract's own text match (`creator_render_contract._normal`).
    return " ".join(unicodedata.normalize("NFC", str(text)).split())


def _shot_text_placements(resolved_intents: object) -> dict[str, tuple[str, ...]]:
    """KRI-1110: the clips a resolved label/caption intent prints each set of words on.

    The same placement the render contract pins for a dictated shot text (its label intent,
    else its creator-worded caption), so the draft's receipt judges the clips the phone will.
    """
    from app.services.creator_render_contract import (  # noqa: PLC0415 - imports this module
        _caption_intent_media_ids,
        _label_intent_media_ids,
    )

    words: list[object] = []
    for intent in resolved_intents if isinstance(resolved_intents, list) else ():
        if not isinstance(intent, Mapping):
            continue
        if intent.get("op") == "caption":
            words.append(intent.get("caption_text"))
        elif intent.get("op") == "label":
            words.append(intent.get("creator_text"))
            words.extend(
                row.get("value")
                for row in intent.get("assignments") or []
                if isinstance(row, Mapping)
            )
    placements: dict[str, tuple[str, ...]] = {}
    for text in words:
        if not isinstance(text, str) or not text.strip():
            continue
        key = _placement_key(text)
        if key not in placements:
            placed = _label_intent_media_ids(resolved_intents, text) or _caption_intent_media_ids(
                resolved_intents, text
            )
            if placed:
                placements[key] = placed
    return placements


def plan_facts_from_strategy(
    strategy: Mapping[str, Any] | None,
    *,
    clip_ids: Iterable[str] = (),
    manifest: ResolvedCreatorManifest | None = None,
    speech_cleanup_enabled: bool | None = None,
    speech_cleanup_offered: bool | None = None,
) -> PlanFacts:
    """Read verifiable facts off a serialized ``CreativeStrategy``.

    ``manifest`` is the turn's resolved creator manifest. Without it the beat,
    closing-shot and clip-count facts stay unknown (never guessed).
    ``speech_cleanup_offered`` is whether approval will offer "Clean up speech"
    (the caller's cohort check); None keeps the cleanup receipt from promising it.
    """
    strategy = strategy or {}
    per_clip_parts: dict[str, list[str]] = {}
    inferred_parts: dict[str, list[str]] = {}
    label_texts: list[str] = []
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("status", "resolved") != "resolved":
            continue
        if intent.get("op") not in {"label", "caption"}:
            continue
        for assignment in intent.get("assignments") or []:
            if not isinstance(assignment, Mapping):
                continue
            media_id = str(assignment.get("media_id") or "")
            value = (
                intent.get("caption_text")
                if intent.get("op") == "caption"
                else assignment.get("value")
            )
            grounding = (
                intent.get("caption_grounding")
                if intent.get("op") == "caption"
                else assignment.get("grounding")
            )
            if not media_id or not value:
                continue
            per_clip_parts.setdefault(media_id, []).append(str(value))
            if intent.get("op") == "label":
                label_texts.append(str(value))
            # Only the creator's own words are "read"; everything the server
            # matched from footage or the record is reported as inferred.
            if grounding not in {"creator_text", "placeholder"}:
                inferred_parts.setdefault(media_id, []).append(str(value))
    per_clip = {media: "\n".join(dict.fromkeys(parts)) for media, parts in per_clip_parts.items()}
    inferred = {media: "\n".join(dict.fromkeys(parts)) for media, parts in inferred_parts.items()}
    labels = tuple(str(x) for x in (strategy.get("shot_labels") or []) if str(x).strip())
    title = strategy.get("opening_title")
    texts = [
        str(value)
        for value in (
            title,
            strategy.get("closing_title"),
            *labels,
            *label_texts,
            *_pinned_text_values(strategy.get("pinned_texts")),
        )
        if value
    ]
    basis = strategy.get("ordering_basis")
    if (
        basis is None
        and strategy.get("archetype") == "day_vlog"
        and "ordering_fallback_clip_ids" in strategy
    ):
        # Capture order is only assumed when the planner also recorded which
        # clips fell back; otherwise nothing here can be verified.
        basis = "capture_order"
    extra: dict[str, Any] = {}
    if manifest is not None:
        extra = _beat_facts(strategy, manifest)
        extra.update(_timed_card_facts(strategy, manifest))
        extra["fullscreen_sequence"] = _draft_fullscreen_sequence(strategy, manifest)
        extra["video_clip_count"] = _video_clip_count(strategy, manifest)
        extra["take_duration_s"] = _selected_video_seconds(strategy, manifest)
        spoken_cap = manifest.capabilities.get("phone_voiceover_spoken_text")
        extra["phone_voiceover_spoken_text"] = bool(spoken_cap and spoken_cap.available)
    edit_format = strategy.get("edit_format")
    caption_style = strategy.get("caption_style")
    audio_strategy = strategy.get("audio_strategy")
    last_seats = [
        members[0]
        for position, _name, members, status in sequence_rows(strategy.get("resolved_clip_intents"))
        if position == "last" and status == "resolved" and len(members) == 1
    ]
    return PlanFacts(
        last_clip_id=str(last_seats[-1]) if last_seats else None,
        clip_ids=tuple(str(c) for c in clip_ids),
        title=str(title) if title else None,
        per_clip_text=per_clip,
        inferred_text=inferred,
        positional_labels=labels,
        shot_text_clip_ids=_shot_text_placements(strategy.get("resolved_clip_intents")),
        # A strategy target is a proposal, even when a creator proved the target.
        # Only a rendered output or a separately validated execution plan supplies
        # duration evidence for a receipt.
        duration_s=None,
        ordering_basis=str(basis) if basis else None,
        ordering_fallback_clip_ids=tuple(
            str(c) for c in (strategy.get("ordering_fallback_clip_ids") or [])
        ),
        sequence_conflicts=_strategy_sequence_conflicts(strategy),
        sequence_unlocated=tuple(row["code"] for row in creator_omission_outcomes(strategy)),
        sequence_unlocated_intent_ids=tuple(
            row["intent_id"] for row in creator_omission_outcomes(strategy)
        ),
        sequence_unconfirmed=tuple(
            sorted(
                name
                for _position, name, _members in unconfirmed_order_groups(
                    strategy.get("resolved_clip_intents")
                )
            )
        ),
        texts=tuple(texts),
        edit_format=str(edit_format) if edit_format else None,
        speech_cleanup_enabled=speech_cleanup_enabled,
        speech_cleanup_offered=speech_cleanup_offered,
        caption_style=str(caption_style) if caption_style else None,
        audio_strategy=str(audio_strategy) if audio_strategy else None,
        song_sync=str(strategy["song_sync"]) if strategy.get("song_sync") else None,
        speech_occurrence_answers=tuple(
            dict(a)
            for a in strategy.get("choice_answers") or []
            if isinstance(a, Mapping) and a.get("kind") == "speech_occurrence"
        ),
        render_program=(
            str(strategy.get("render_program")) if strategy.get("render_program") else None
        ),
        **extra,
    )


def _endpoint_fact(raw: object) -> EndpointFact | None:
    if not isinstance(raw, Mapping):
        return None
    places = tuple(
        str(row["text"])
        for row in raw.get("places") or []
        if isinstance(row, Mapping) and row.get("text")
    )
    label = str(raw.get("label") or "")
    media_id = str(raw.get("media_id") or "")
    return (
        EndpointFact(label=label, places=places, media_id=media_id) if (label or places) else None
    )


def _declared_outro_s(record: Mapping[str, Any]) -> float:
    """The outro the record says the finished video carries, else 0 (unknown).

    The phone declares its brand tail only when it exports, after the plan and its receipts
    exist, so a plan-time record has none and the outro is never mentioned on an assumption.
    """
    from app.kria.device_render import BRAND_TAIL_SECONDS  # noqa: PLC0415

    declared = record.get("brand_tail")
    if isinstance(declared, str) and declared in BRAND_TAIL_SECONDS:
        return BRAND_TAIL_SECONDS[declared]
    stored = record.get("brand_tail_s")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool) and stored > 0:
        return float(stored)
    return 0.0


_SPOT_TR = {"first": "ilk", "last": "son", "then": "sonra"}
# What an ordering basis / edit format is called in Turkish copy; an unknown value
# is shown as written (underscores spaced), exactly as the English copy does.
_BASIS_TR = {
    "attachment": "klipleri eklediğin sıra",
    "attachment_order": "klipleri eklediğin sıra",
    "creator_order": "senin verdiğin sıra",
    "song_time": "şarkının zamanı",
    "confirmed": "onayladığın sıra",
    "editor": "editördeki sıra",
    "route": "rota",
}
_FORMAT_TR = {
    "montage": "montaj",
    "day_vlog": "günlük vlog",
    "single_hero": "tek kahraman",
    "talking_head": "konuşmalı",
    "subtitled": "konuşmalı",
    "narrated": "seslendirmeli",
    "narrated_planned": "seslendirmeli",
    "narrated_ready": "seslendirmeli",
    "slides": "slayt",
}


def _sequence_label(name: str) -> str:
    """``first: the video that is blue`` (the planner's outcome name) -> ``the video that is
    blue (first)``, the way a creator would say it back."""
    spot, _, words = name.partition(": ")
    if words and spot in ("first", "last", "then"):
        return say(en=f"{words} ({spot})", tr=f"{words} ({_SPOT_TR[spot]})")
    return name or say(en="the order you described", tr="tarif ettiğin sıra")


def _sequence_problems(record: Mapping[str, Any], codes: tuple[str | None, ...]) -> tuple[str, ...]:
    """The stated-sequence groups of a unified-montage record that did not land, by outcome
    ``code`` (KRI-503). A ``then`` group wholly inside an earlier group, a first / last group
    that repeats an earlier one (KRI-764: the creator said it twice, the earlier row carries
    the verdict) and a Visuals-only group are not "a described group missed its place"; a row
    without a code (an older or hand-built record) counts as misplaced, exactly as before."""
    out: list[str] = []
    for row in record.get("intent_outcomes") or []:
        if not isinstance(row, Mapping) or row.get("op") != "order" or row.get("status") == "met":
            continue
        code = row.get("code")
        if code in ("visual", "repeat") or (code == "contained" and row.get("position") == "then"):
            continue
        if code in codes:
            out.append(_sequence_label(str(row.get("name") or "")))
    return tuple(out)


def _strategy_sequence_conflicts(strategy: Mapping[str, Any]) -> tuple[str, ...]:
    """Draft-time twin of ``_sequence_problems(record, ("conflict",))`` (KRI-764).

    A strategy never records its clip order, but a clip asked to be first AND last is
    readable off the resolved intents alone (it is about ids, not about the layout), so the
    creator hears it before approving instead of after the render."""
    rows = sequence_rows(strategy.get("resolved_clip_intents"))
    return tuple(
        _sequence_label(f"{position or 'then'}: {name}" if name else "")
        for (position, name, _members, _status), relation in zip(
            rows, row_relations(rows), strict=True
        )
        if relation == "conflict"
    )


def _random_order_record(record: Mapping[str, Any]) -> tuple[int | None, tuple[str, ...]]:
    raw = record.get("random_order")
    if not isinstance(raw, Mapping):
        return None, ()
    seed = raw.get("seed")
    ids = raw.get("source_ids")
    return (
        seed if isinstance(seed, int) and not isinstance(seed, bool) else None,
        tuple(str(i) for i in ids) if isinstance(ids, list) else (),
    )


def plan_facts_from_unified_montage(record: Mapping[str, Any] | None) -> PlanFacts:
    """Read verifiable facts off a unified montage plan record (KRI-190).

    ``record`` is ``UnifiedMontagePlan.record()``: what the server actually put
    in the plan (the ordered clips, the labels it could ground, the title, the
    total length and how the order was decided), never what a model claimed.
    """
    record = record or {}
    labels = [row for row in record.get("labels") or [] if isinstance(row, Mapping)]
    per_clip = {str(row["media_id"]): str(row["text"]) for row in labels if row.get("text")}
    inferred = {
        str(row["media_id"]): str(row["text"])
        for row in labels
        if row.get("inferred") and row.get("text")
    }
    title = record.get("title")
    title_source = record.get("title_source")
    shown = record.get("title_shown_as_label")
    if not title and isinstance(shown, str) and per_clip.get(shown):
        # KRI-1030: the opening title is the first clip's own label (same creator words,
        # drawn once from 0 s), so a title ask reads that label as the title.
        title, title_source = per_clip[shown], "creator"
    duration = record.get("duration_s")
    basis = record.get("ordering_basis")
    route = record.get("route") if isinstance(record.get("route"), Mapping) else {}
    endpoints = (
        record.get("endpoint_places") if isinstance(record.get("endpoint_places"), Mapping) else {}
    )
    reasons = record.get("dropped_label_reasons")
    repeats = (
        tuple(str(k) for k, v in reasons.items() if v == "repeat")
        if isinstance(reasons, Mapping)
        else ()
    )
    return PlanFacts(
        clip_ids=tuple(str(c) for c in record.get("clip_ids") or []),
        title=str(title) if title else None,
        title_source=str(title_source) if title_source else None,
        repeat_label_clip_ids=repeats,
        route_start=str(route["start"]) if route.get("start") else None,
        route_end=str(route["end"]) if route.get("end") else None,
        first_endpoint=_endpoint_fact(endpoints.get("first")),
        last_endpoint=_endpoint_fact(endpoints.get("last")),
        outro_s=_declared_outro_s(record),
        per_clip_text=per_clip,
        inferred_text=inferred,
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        rendered_output=True,
        ordering_basis=str(basis) if basis else None,
        ordering_fallback_clip_ids=tuple(
            str(c) for c in record.get("ordering_fallback_clip_ids") or []
        ),
        ordering_choice=str(record["ordering_choice"]) if record.get("ordering_choice") else None,
        random_order_seed=_random_order_record(record)[0],
        random_order_source_ids=_random_order_record(record)[1],
        sequence_statuses=tuple(
            str(row.get("status"))
            for row in record.get("intent_outcomes") or []
            if isinstance(row, Mapping) and row.get("op") == "order"
        ),
        sequence_spots_met=tuple(
            str(row.get("position") or "then")
            for row in record.get("intent_outcomes") or []
            if isinstance(row, Mapping) and row.get("op") == "order" and row.get("status") == "met"
        ),
        sequence_unmet=_sequence_problems(record, ("misplaced", None, "contained")),
        sequence_unconfirmed=_sequence_problems(record, ("member_order_unknown",)),
        sequence_absent=_sequence_problems(record, ("absent", "unresolved")),
        sequence_conflicts=_sequence_problems(record, ("conflict",)),
        sequence_unlocated=tuple(
            str(row.get("code"))
            for row in record.get("intent_outcomes") or []
            if isinstance(row, Mapping)
            and row.get("op") == "order"
            and row.get("code") in _MOMENT_LIMIT_CODES
        ),
        sequence_unlocated_intent_ids=tuple(
            str(row["intent_id"]) if row.get("intent_id") else None
            for row in record.get("intent_outcomes") or []
            if isinstance(row, Mapping)
            and row.get("op") == "order"
            and row.get("code") in _MOMENT_LIMIT_CODES
        ),
        texts=tuple(
            str(text)
            for text in (
                title,
                record.get("closing_title"),
                *per_clip.values(),
                *_pinned_text_values(record.get("pinned_texts")),
            )
            if text
        ),
        label_scope_clip_ids=tuple(str(c) for c in record.get("label_scope_clip_ids") or []),
        unreadable_label_clip_ids=tuple(str(c) for c in record.get("short_label_clip_ids") or []),
        per_clip_label_kinds={
            str(row["media_id"]): str(row["fact_kind"])
            for row in labels
            if row.get("text") and row.get("fact_kind")
        },
        label_timezone=str(record.get("label_timezone") or ""),
        label_timezone_basis=str(record.get("label_timezone_basis") or ""),
        **_closing_quote_facts(record.get("closing_quote")),
        named_lines=_named_line_facts(record.get("named_lines")),
        cut_lengths_s=_window_lengths(record.get("best_parts")),
        cut_clip_ids=_window_media_ids(record.get("best_parts")),
        source_durations_s=_source_durations(record.get("clip_durations_s")),
        song_bed=bool(record.get("user_song")),
        # A record without the key was written by a planner that could not retime (KRI-689's
        # original "Couldn't"); a speed-aware planner always writes a row per speed ask.
        speed_unsupported="clip_speeds" not in record,
        clip_speeds=_speed_facts(record.get("clip_speeds")),
        freeze_frames=_freeze_facts(record.get("freeze_frames"), record.get("freeze_frame")),
        asset_holds=tuple(
            row for row in record.get("asset_holds") or [] if isinstance(row, Mapping)
        ),
        text_shapes=_text_shape_facts(record.get("text_shapes")),
        **_montage_caption_facts(record.get("captions")),
    )


def _text_shape_facts(rows: object) -> tuple[tuple[str, str, str | None], ...] | None:
    """The plan record's ``text_shapes`` rows (KRI-1029) as PlanFacts.text_shapes."""
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("text"), str):
            continue
        reason = row.get("reason")
        if row.get("bend") is None and reason is None:
            reason = "phone"  # straight with no recorded why: never claim it curved
        out.append(
            (
                _fold(row["text"]),
                str(row.get("role") or ""),
                None if row.get("bend") is not None else str(reason),
            )
        )
    return tuple(out)


def _freeze_facts(rows: object, legacy: object = None) -> dict[str, Mapping[str, Any]]:
    """KRI-828 / KRI-899: the plan record's freeze rows, keyed by every requirement id each
    answers. A row also answers the earlier asks it corrected (``requirement_ids``); those
    carry ``corrected_by`` so their receipt says the later ask changed them."""
    if not isinstance(rows, list):
        rows = [legacy]
    facts: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("requirement_id"), str):
            continue
        own = str(row["requirement_id"])
        ids = row.get("requirement_ids")
        for rid in ids if isinstance(ids, list) else [own]:
            if isinstance(rid, str):
                facts[rid] = dict(row) if rid == own else {**row, "corrected_by": own}
        facts.setdefault(own, dict(row))
    return facts


def _rendered_freezes(
    planned: Mapping[str, Mapping[str, Any]], variant: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    """A planned hold counts on a finished video only when its timeline still carries one."""
    rows = variant.get("story_timeline")
    if not isinstance(rows, list):
        return dict(planned)
    held = any(
        isinstance(row, Mapping) and (_finite_number(row.get("freeze_hold_s")) or 0) > 0
        for row in rows
    )
    return {
        key: (value if value.get("status") != "held" or held else {**value, "status": "dropped"})
        for key, value in planned.items()
    }


def _speed_facts(rows: object) -> dict[str, Mapping[str, Any]] | None:
    """KRI-689: the plan record's clip-speed rows keyed by requirement id; None without the
    key (a record from before speed asks were planned)."""
    if not isinstance(rows, list):
        return None
    return {
        str(row["requirement_id"]): dict(row)
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("requirement_id"), str)
    }


def _rendered_speeds(
    planned: Mapping[str, Mapping[str, Any]] | None, variant: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]] | None:
    """An applied rate counts on a finished video only when its timeline still carries it.

    The variant's ``story_timeline`` rows are the plan's moments (``moment_id`` = cut id,
    ``playback_rate``). A row for an applied cut without the rate marks the ask "dropped";
    a timeline without ``moment_id`` (older variants) cannot contradict the plan.
    """
    if planned is None:
        return None
    rows = variant.get("story_timeline")
    if not isinstance(rows, list):
        return dict(planned)
    by_moment = {
        str(row["moment_id"]): row
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("moment_id"), str)
    }
    if not by_moment:
        return dict(planned)
    out: dict[str, Mapping[str, Any]] = {}
    for key, value in planned.items():
        if value.get("status") != "applied":
            out[key] = value
            continue
        rate = _finite_number(value.get("rate"))
        kept = True
        for cut_id in value.get("cut_ids") or []:
            row = by_moment.get(str(cut_id))
            if row is None:
                continue
            played = _finite_number(row.get("playback_rate"))
            if played is None or rate is None or abs(played - rate) > 0.01:
                kept = False
        out[key] = value if kept else {**value, "status": "dropped"}
    return out


def _named_line_facts(raw: object) -> tuple[NamedLineFact, ...] | None:
    """The record's whole-line asks (KRI-760); None when the plan recorded none."""
    if not isinstance(raw, list):
        return None
    return tuple(
        NamedLineFact(
            quote=str(row["quote"]),
            status=str(row["status"]),
            media_id=str(row.get("media_id") or ""),
            start_s=_finite_number(row.get("source_start_s")),
            end_s=_finite_number(row.get("source_end_s")),
        )
        for row in raw
        if isinstance(row, Mapping) and row.get("quote") and row.get("status")
    )


def _confirm_named_lines(
    lines: tuple[NamedLineFact, ...] | None,
    cuts: Sequence[tuple[str, float | None, float | None]],
) -> tuple[NamedLineFact, ...] | None:
    """A line the plan held is held only if the finished timeline's cut still covers it."""
    if lines is None or not cuts:
        return lines
    out: list[NamedLineFact] = []
    for line in lines:
        if line.status == "held" and line.start_s is not None and line.end_s is not None:
            covered = any(
                media_id == line.media_id
                and start is not None
                and end is not None
                and start <= line.start_s + _HELD_LINE_TOLERANCE_S
                and end >= line.end_s - _HELD_LINE_TOLERANCE_S
                for media_id, start, end in cuts
            )
            if not covered:
                line = dataclasses.replace(line, status="not_held")
        out.append(line)
    return tuple(out)


def _closing_quote_facts(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not raw.get("status"):
        return {}
    return {
        "closing_quote_status": str(raw["status"]),
        "closing_quote_text": str(raw.get("quote") or ""),
    }


def _window_lengths(rows: object) -> tuple[float, ...] | None:
    lengths = []
    for row in rows if isinstance(rows, list) else []:
        start = _finite_number(row.get("source_start_s")) if isinstance(row, Mapping) else None
        end = _finite_number(row.get("source_end_s")) if isinstance(row, Mapping) else None
        if start is None or end is None or end <= start:
            return None
        lengths.append(round(end - start, 3))
    return tuple(lengths) or None


def _window_media_ids(rows: object) -> tuple[str, ...] | None:
    ids = [
        row["media_id"] if isinstance(row, Mapping) and isinstance(row.get("media_id"), str) else ""
        for row in (rows if isinstance(rows, list) else [])
    ]
    return tuple(ids) if ids and all(ids) else None


def _source_durations(raw: object) -> tuple[tuple[str, float], ...] | None:
    if not isinstance(raw, Mapping):
        return None
    pairs = tuple(
        (str(media_id), float(seconds))
        for media_id, seconds in raw.items()
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0
    )
    return pairs or None


def with_planned_cuts(facts: PlanFacts, media_snapshot: Mapping[str, Any] | None) -> PlanFacts:
    """``facts`` with the cuts the approved length plan will make (KRI-776).

    A strategy draft has no cuts of its own, so "six moments, about 2 seconds each" was never
    judged before approval and "I will cut the 6 exact moments" reached the card over a
    one-cut plan. The length plan's source windows are what the render plays (the phone plan
    reads them back as its approved windows), so a moment ask is checked against them. A plan
    that is not window-eligible, still has conflicts or holds no valid window leaves ``facts``
    unchanged.
    """
    envelope = media_snapshot.get("length_plan") if isinstance(media_snapshot, Mapping) else None
    if (
        not isinstance(envelope, Mapping)
        or envelope.get("window_eligible") is not True
        or envelope.get("conflicts")
    ):
        return facts
    windows = envelope.get("windows")
    lengths = _window_lengths(windows)
    if lengths is None:
        return facts
    sources = envelope.get("sources")
    return dataclasses.replace(
        facts,
        cut_lengths_s=lengths,
        cut_clip_ids=_window_media_ids(windows),
        source_durations_s=_source_durations(
            {
                row.get("media_id"): row.get("duration_s")
                for row in (sources if isinstance(sources, list) else [])
                if isinstance(row, Mapping) and isinstance(row.get("media_id"), str)
            }
        ),
    )


def _montage_caption_facts(raw: object) -> dict[str, str]:
    """The ``captions`` entry of a unified montage record (KRI-569), or nothing."""
    if not isinstance(raw, Mapping) or raw.get("status") != "placed":
        return {}
    style = "word" if raw.get("style") == "word" else "sentence"
    return {"montage_captions": "placed", "montage_caption_style": style}


def plan_facts_from_speech_montage(record: Mapping[str, Any] | None) -> PlanFacts:
    """Read verifiable facts off a spoken-excerpt montage record (KRI-282).

    ``record`` is what ``phone_speech_montage_job`` stored: the sections the
    compiler actually put on the timeline (each excerpt already grounded to
    word timings) and the quotes that could not be grounded.
    """
    record = record or {}
    sections = tuple(
        SpeechSectionFact(
            kind=str(row.get("kind") or ""),
            visual=str(row.get("visual") or ""),
            quote=str(row.get("quote") or ""),
            media_id=str(row.get("media_id") or ""),
        )
        for row in record.get("sections") or []
        if isinstance(row, Mapping)
    )
    planned = record.get("planned") if isinstance(record.get("planned"), Mapping) else {}
    dropped = tuple(
        str(row.get("quote"))
        for row in (planned or {}).get("dropped") or []
        if isinstance(row, Mapping) and row.get("quote")
    )
    duration = record.get("duration_s")
    basis = record.get("ordering_basis")
    return PlanFacts(
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        speech_sections=sections,
        speech_dropped_quotes=dropped,
        ordering_basis=str(basis) if basis else None,
        # KRI-527: corner text the writer actually drew (a pin with no window is not claimed).
        texts=tuple(_pinned_text_values(record.get("pinned_texts"))),
        rendered_output=True,
    )


# Where the phone Voiceover worker keeps the evidence behind these receipts.
NARRATED_ALIGNMENT_FIELD = "narrated_alignment"

# Orderings whose clip times were set by the creator's words (a scripted guide or the
# alignment agent), so "while I talk about X" can be read off the step windows.
NARRATED_ALIGNED_BASES = frozenset({"spoken_word_alignment", "guide_script_alignment"})


def plan_facts_from_narrated_alignment(record: Mapping[str, Any] | None) -> PlanFacts:
    """Read verifiable facts off a phone Voiceover render record (KRI-533).

    ``record`` is ``assembly_plan["narrated_alignment"]``: the steps the worker pinned
    (each clip's creator labels, window and spoken words) and the caption language it
    actually burned, never what a model claimed.
    """
    record = record or {}
    steps: list[NarratedStepFact] = []
    for row in record.get("steps") or []:
        if not isinstance(row, Mapping):
            continue
        placed = tuple(
            (str(p["spot"]), str(p["name"]))
            for p in row.get("placed") or []
            if isinstance(p, Mapping) and p.get("spot") and p.get("name")
        )
        start, end = row.get("start_s"), row.get("end_s")
        steps.append(
            NarratedStepFact(
                media_id=str(row.get("media_id") or ""),
                labels=tuple(str(x) for x in row.get("labels") or [] if str(x).strip()),
                placed=placed,
                start_s=float(start) if isinstance(start, (int, float)) else 0.0,
                end_s=float(end) if isinstance(end, (int, float)) else 0.0,
                text=" ".join(str(row.get("text") or "").split()),
                out_of_sequence=row.get("out_of_sequence") is True,
            )
        )
    basis = record.get("ordering_basis")
    language = record.get("caption_language")
    spoken = record.get("spoken_language")
    return PlanFacts(
        clip_ids=tuple(step.media_id for step in steps),
        ordering_basis=str(basis) if basis else None,
        rendered_output=True,
        narrated_steps=tuple(steps),
        caption_language=str(language) if language else None,
        spoken_language=str(spoken) if spoken else None,
    )


def _phone_output_duration(payload: Mapping[str, Any]) -> float | None:
    from app.services.authored_text_fit import _segments

    slots = payload.get("timeline_slots")
    if not isinstance(slots, list) or not slots:
        timeline = payload.get("user_timeline") or payload.get("ai_timeline") or {}
        slots = timeline.get("slots") if isinstance(timeline, Mapping) else None
    if not isinstance(slots, list) or not slots:
        revision = payload.get("guided_edit_revision") or {}
        slots = revision.get("segments") if isinstance(revision, Mapping) else None
    if not isinstance(slots, list) or not slots:
        story = payload.get("story_timeline")
        if not isinstance(story, list):
            return None
        ends = []
        for row in story:
            if not isinstance(row, Mapping) or row.get("lane", "clip") != "clip":
                continue
            start, end = (
                _finite_number(row.get("output_start_s")),
                _finite_number(row.get("output_end_s")),
            )
            if start is None or end is None or end <= start or start < 0:
                return None
            ends.append(end)
        return max(ends) if ends else None
    for slot in slots:
        if not isinstance(slot, Mapping):
            return None
        if slot.get("removed"):
            continue
        duration = _finite_number(slot.get("duration_s"))
        if duration is None or duration <= 0:
            return None
        for key in ("playback_rate", "transition_duration_s"):
            if slot.get(key) is not None:
                value = _finite_number(slot[key])
                if value is None or value < 0 or (key == "playback_rate" and value == 0):
                    return None
    segments = _segments(slots)
    total = max((row["output_end_s"] for row in segments), default=0.0)
    return total if math.isfinite(total) and total > 0 else None


def _editor_payload_duration(payload: Mapping[str, Any]) -> float | None:
    """Output length implied by the payload's timeline slots, or None when unprovable.

    Only exact-second slots count (beat-sized slots need the audio grid) and a
    speed change divides the window. Crossfade overlap is ignored, which the
    tolerance absorbs.
    """
    slots = payload.get("timeline_slots") if isinstance(payload, Mapping) else None
    if not isinstance(slots, list) or not slots:
        for key in ("edit_duration_s", "total_duration_s", "duration_s"):
            duration = payload.get(key)
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(float(duration))
                and duration > 0
            ):
                return float(duration)
        return None
    total = 0.0
    for slot in slots:
        if not isinstance(slot, Mapping):
            return None
        if slot.get("removed"):
            continue
        duration = slot.get("duration_s")
        if not isinstance(duration, (int, float)) or duration <= 0:
            return None
        rate = slot.get("playback_rate")
        total += float(duration) / (
            float(rate) if isinstance(rate, (int, float)) and rate > 0 else 1.0
        )
    return total or None


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ENTRANCES = frozenset(get_args(TextAnimationEdge))
_TEXT_CASES = frozenset({"none", "upper", "lower", "title"})
_ALIGNMENTS = frozenset({"left", "center", "right"})


def _row_entrance(row: Mapping[str, Any]) -> str | None:
    """The entrance a saved row plays: explicit phases win, else the legacy effect's."""
    entrance = effective_entrance(row)
    return entrance if entrance in _ENTRANCES else None


def _row_choice(
    row: Mapping[str, Any], key: str, allowed: frozenset[str], default: str
) -> str | None:
    value = row.get(key)
    if value is None:
        return default
    return value if value in allowed else None


def _row_clip_id(row: Mapping[str, Any]) -> str | None:
    """The clip a label row belongs to: its ``clip_id``, else the media id in its row id."""
    clip = row.get("clip_id")
    if isinstance(clip, str) and clip:
        return clip
    row_id = str(row.get("id") or "")
    prefix = "clip-label-media-"
    return row_id[len(prefix) :] or None if row_id.startswith(prefix) else None


def _text_style_rows(
    rows: Iterable[Any], *, orientation: str | None = None
) -> tuple[TextStyleRow, ...]:
    """Style of the editor's live, non-caption text rows, typed by the editor's own classifier."""
    from app.pipeline.canvas import canvas_for_orientation  # noqa: PLC0415
    from app.pipeline.text_stack_layout import row_canvas  # noqa: PLC0415
    from app.services.kria_editor_ops import is_caption_text_bar  # noqa: PLC0415
    from app.services.kria_editor_ops_text import classify  # noqa: PLC0415

    live = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("id"), str)
        and isinstance(row.get("text"), str)
        and row["text"].strip()
        and not row.get("removed")
        and row.get("role") != "lyric_line"
        and not is_caption_text_bar(dict(row))
    ]
    bars = []
    for index, row in enumerate(live):
        params = row.get("source_params")
        source = row.get("sequence_source_id")
        if not isinstance(source, str) and isinstance(params, Mapping):
            source = params.get("sequence_source_id")
        clip = row.get("clip_id")
        start = row.get("start_s")
        bars.append(
            {
                "index": index,
                "id": row["id"],
                "text": str(row.get("text") or ""),
                "role": row.get("role"),
                "sequence_source_id": source if isinstance(source, str) else None,
                "clip_id": clip if isinstance(clip, str) and clip else None,
                "removed": False,
                "start_s": float(start)
                if isinstance(start, (int, float)) and not isinstance(start, bool)
                else None,
                "caption": False,
            }
        )
    kinds = classify(bars)
    out = []
    for row in live:
        canvas = canvas_for_orientation(orientation) if orientation else row_canvas(row)
        color = row.get("color")
        font = row.get("font_family")
        out.append(
            TextStyleRow(
                id=row["id"],
                kind=kinds.get(row["id"], "text"),
                entrance=_row_entrance(row),
                alignment=_row_choice(row, "alignment", _ALIGNMENTS, "center"),
                text_case=_row_choice(row, "text_case", _TEXT_CASES, "none"),
                font_family=font if isinstance(font, str) and font else None,
                color=color.upper() if isinstance(color, str) and _HEX_COLOR.match(color) else None,
                size_px=_finite_number(row.get("size_px")),
                position=row.get("position") if isinstance(row.get("position"), str) else None,
                x_frac=_finite_number(row.get("x_frac")),
                y_frac=_finite_number(row.get("y_frac")),
                clip_id=_row_clip_id(row),
                text=str(row.get("text") or ""),
                heading_level=((row.get("source_params") or {}).get("text_stack_v1") or {}).get(
                    "heading_level"
                ),
                stack_id=((row.get("source_params") or {}).get("text_stack_v1") or {}).get(
                    "stack_id"
                ),
                layout_row=row,
                canvas_width=canvas.width,
                canvas_height=canvas.height,
                bend=_finite_number(row.get("bend")) or None,
            )
        )
    return tuple(out)


def plan_facts_from_editor_payload(
    payload: Mapping[str, Any] | None,
    text_diff: Iterable[Mapping[str, Any]] | None = None,
    changes: Iterable[str] | None = None,
    *,
    canonical_duration_s: float | None = None,
) -> PlanFacts:
    """Editor drafts expose literal on-screen text, plus (KRI-218) the turn's text diff.

    ``text_diff`` is the compiler's ``[{id, clip_id, role, before, after}]``. Clip-linked
    entries fill ``per_clip_text`` / ``clip_ids`` (only the clips this turn touched), so a
    per-clip requirement is checked against what was really written.
    """
    if not payload:
        return PlanFacts()
    timing_rows = payload.get("text_elements")
    timing_rows = timing_rows if isinstance(timing_rows, list) else ()
    if canonical_duration_s is None and any(
        isinstance(row, Mapping)
        and isinstance(row.get("source_params"), Mapping)
        and "sequence_timing" in row["source_params"]
        for row in timing_rows
    ):
        canonical_duration_s = _phone_output_duration(payload)
    # Do not walk every payload value: source requests, paths, operation names,
    # and arbitrary metadata may repeat the creator's literal without placing it
    # on screen. Only explicit output text lanes count as visible evidence.
    strings: list[str] = []
    per_clip: dict[str, str] = {}
    title: str | None = None
    text_spans: list[tuple[str, str, float, float]] = []
    sequence_fragments: list[Mapping[str, Any]] = []
    text_timing_incomplete = False

    def take_text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    raw_title = payload.get("title") if isinstance(payload, Mapping) else None
    if isinstance(raw_title, Mapping):
        title = take_text(raw_title.get("text"))
    else:
        title = take_text(raw_title)
    if title:
        strings.append(title)

    for lane_name in ("text_elements", "bars"):
        rows = payload.get(lane_name) if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or row.get("removed"):
                continue
            text = take_text(row.get("text"))
            if text is None:
                continue
            strings.append(text)
            row_id = row.get("id")
            start, end = row.get("start_s"), row.get("end_s")
            if (
                isinstance(row_id, str)
                and isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and not isinstance(start, bool)
                and not isinstance(end, bool)
                and math.isfinite(float(start))
                and math.isfinite(float(end))
                and end > start >= 0
            ):
                text_spans.append(
                    (row_id, str(row.get("role") or "text"), float(start), float(end))
                )
                if not row.get("removed") and sequence_lineage(row) is not None:
                    sequence_fragments.append(row)
            elif isinstance(row_id, str) and str(row.get("role") or "text") in {
                "title",
                "generative_intro",
            }:
                text_timing_incomplete = True
            clip = row.get("clip_id")
            if isinstance(clip, str) and clip:
                per_clip[clip] = text
    # KRI-560: a phrase the compiler split into one-word-at-a-time fragments (a word-by-word
    # hook) is on screen as the whole phrase, but no single row says it. Only a complete,
    # contiguous, same-source run counts; a missing or removed word leaves the phrase unseen.
    composed_sequences = tuple(
        composed["text"] for composed in complete_sequences(sequence_fragments)
    )
    strings.extend(composed_sequences)
    for entry in text_diff or ():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("after"), str):
            continue
        clip = entry.get("clip_id")
        if isinstance(clip, str) and clip:
            per_clip[clip] = entry["after"]
            strings.append(entry["after"])
        elif entry.get("role") == "title":
            title = entry["after"]
            strings.append(title)
    # The compiler's own change list says the timeline was re-sorted by filming time.
    ordered_by_capture = any(
        str(change).startswith("Order clips by filming time") for change in changes or ()
    )
    return PlanFacts(
        ordering_basis=(
            "capture_time"
            if ordered_by_capture
            else "random"
            if any(str(change).startswith("Shuffle clips") for change in changes or ())
            else None
        ),
        duration_s=(
            canonical_duration_s
            if canonical_duration_s is not None
            else _editor_payload_duration(payload)
        ),
        editor_text_edited=bool(
            isinstance(payload, Mapping)
            and isinstance(payload.get("text_elements"), list)
            and payload.get("text_elements")
        ),
        texts=tuple(strings),
        sequence_texts=composed_sequences,
        editor=True,
        has_clip_structure=bool(per_clip),
        per_clip_text=per_clip,
        clip_ids=tuple(per_clip),
        title=title,
        text_spans=tuple(text_spans),
        text_timing_incomplete=text_timing_incomplete,
        text_styles=(
            _text_style_rows(
                payload["text_elements"],
                orientation=payload.get("output_orientation") or payload.get("orientation"),
            )
            if isinstance(payload, Mapping) and isinstance(payload.get("text_elements"), list)
            else None
        ),
        clip_output_durations=_editor_clip_durations(payload),
    )


def _editor_clip_durations(payload: Mapping[str, Any]) -> tuple[float, ...] | None:
    """Each kept clip's output length in order, or None when any slot cannot say (KRI-558)."""
    slots = payload.get("timeline_slots") if isinstance(payload, Mapping) else None
    if not isinstance(slots, list) or not slots:
        return None
    out: list[float] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            return None
        if slot.get("removed"):
            continue
        duration = _finite_number(slot.get("duration_s"))
        if duration is None or duration <= 0:
            return None
        rate = _finite_number(slot.get("playback_rate"))
        out.append(duration / (rate if rate and rate > 0 else 1.0))
    return tuple(out) or None


def _rendered_clip_durations(variant: Mapping[str, Any]) -> tuple[float, ...] | None:
    """Each main-picture clip's length on the finished timeline, in screen order."""
    rows = variant.get("story_timeline")
    spans: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("lane", "clip") != "clip":
            continue
        start, end = (
            _finite_number(row.get("output_start_s")),
            _finite_number(row.get("output_end_s")),
        )
        if start is None or end is None or end <= start:
            return None
        spans.append((start, end - start))
    return tuple(length for _start, length in sorted(spans)) or None


# Receipt reasons that mean the spoken trigger itself never played in the creator's voice
# (as opposed to "heard, but no safe room to show it"): see `beat_miss_sentence`.
_NEVER_HEARD_REASONS = frozenset({"never_heard", "after_not_heard", "no_spoken_match"})


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _capped_beat_hits(placed: object, unplaced: object) -> tuple[tuple[str, int, int], ...]:
    """KRI-550: (trigger, placed, heard) for every beat a receipt capped, in receipt order.

    ``phone_reaction_grounding`` writes one ``"occurrence_cap"`` entry per hit of an
    "every time I say X" beat past its caps, only for a beat that placed something. Such a
    beat's other ``unplaced`` entries are hits demoted after grounding, so its placed plus
    unplaced entries count every time the word was said. Counted per beat: a photo beat
    and a sound beat on the same word are two beats over the same hits.
    """
    placed_rows = [e for e in placed if isinstance(e, Mapping)] if isinstance(placed, list) else []
    missed_rows = (
        [e for e in unplaced if isinstance(e, Mapping)] if isinstance(unplaced, list) else []
    )
    capped = {
        row.get("beat_id"): row["trigger"]
        for row in missed_rows
        if row.get("reason") == "occurrence_cap" and isinstance(row.get("trigger"), str)
    }
    hits: list[tuple[str, int, int]] = []
    for beat_id, trigger in capped.items():
        placed_n = sum(1 for row in placed_rows if row.get("beat_id") == beat_id)
        if placed_n:
            missed_n = sum(1 for row in missed_rows if row.get("beat_id") == beat_id)
            hits.append((trigger, placed_n, placed_n + missed_n))
    return tuple(hits)


def _phone_receipt_facts(receipt: object, *, timed: bool) -> dict[str, Any]:
    """Beat and closing facts read off a variant's persisted ``phone_beat_receipt``.

    ``timed`` says the receipt's ``at_s`` is on the output timeline. A Talking render remaps
    its lanes with the cut plan but leaves the receipt on the source take's timeline, so
    only a Voiceover render (whose timeline is the voice) can quote times.
    """
    # A manual lane, a missing matcher or a failed matcher says nothing about the pop-ins.
    if not isinstance(receipt, Mapping) or receipt.get("matcher") in ("manual", "failed", None):
        return {}
    beats: list[BeatFact] = []
    placed = receipt.get("placed")
    for entry in placed if isinstance(placed, list) else []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("trigger"), str):
            continue
        at_s = _finite_number(entry.get("at_s")) if timed else None
        beats.append(
            BeatFact(
                trigger=entry["trigger"],
                visual_id=str(entry.get("visual_label") or "") or None,
                sound=str(entry.get("sound_label") or "") or None,
                at_s=at_s,
                display_mode=str(entry.get("display_mode") or "pip"),
                following_clip_id=entry.get("following_clip_id"),
                sequence_realized=(
                    entry.get("sequence_realized") is True
                    and entry.get("display_mode") == "fullscreen"
                    and _finite_number(entry.get("following_at_s")) is not None
                    and entry.get("following_at_s") == entry.get("end_s")
                    if entry.get("following_clip_id")
                    else None
                ),
            )
        )
    unheard: list[str] = []
    unplaced = receipt.get("unplaced")
    for entry in unplaced if isinstance(unplaced, list) else []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("trigger"), str):
            continue
        reason = entry.get("reason")
        if not reason or reason in _NEVER_HEARD_REASONS:
            unheard.append(entry["trigger"])
    facts: dict[str, Any] = {
        "reaction_beats_available": True,
        "reaction_beats": tuple(beats),
        "unheard_beat_triggers": tuple(dict.fromkeys(unheard)),
        "capped_beat_hits": _capped_beat_hits(placed, unplaced),
    }
    closing = receipt.get("closing")
    if isinstance(closing, Mapping):
        if closing.get("status") == "placed":
            facts["closing_visual_id"] = str(closing.get("visual_label") or "") or "closing"
        elif closing.get("status") == "unplaced":
            facts["closing_requested"] = True
        if closing.get("badge") == "placed":
            facts["closing_badge_requested"] = True
            facts["closing_badge_id"] = "badge"
        elif closing.get("badge") == "unplaced":
            facts["closing_badge_requested"] = True
    return facts


def _rendered_bed_level(variant: Mapping[str, Any]) -> float | None:
    """How loud the footage's own sound plays under the voice on a rendered phone variant.

    KRI-821: an editor Save (``original_audio_level``, ``original_audio_level_source ==
    "editor"``) is what the device recipe plays (`phone_authored_timeline` /
    `phone_editor._apply_original_level`), so it wins over the render's
    ``voiceover_bed_level``. A montage ``voiceover`` variant records only the former.
    """
    archetype = variant.get("resolved_archetype")
    saved = _finite_number(variant.get("original_audio_level"))
    if saved is not None and (
        archetype == "voiceover"
        or (archetype == "narrated" and variant.get("original_audio_level_source") == "editor")
    ):
        return saved
    return _finite_number(variant.get("voiceover_bed_level"))


def _ending_clip_receipt_fact(receipt: object) -> EndingClipFact | None:
    """The worker's ``phone_ending_clip`` receipt as a fact, or None when there is no usable one.

    "applied" is written only from the COMPILED recipe (the last main-track clip really is that
    media, at the volume the recipe holds), never copied from the strategy.
    """
    if not isinstance(receipt, Mapping):
        return None
    status = receipt.get("status")
    if status not in ("applied", "dropped"):
        return None
    reason = receipt.get("reason")
    return EndingClipFact(
        status,
        keep_audio=receipt.get("keep_audio") is not False,
        volume=_finite_number(receipt.get("volume")),
        reason=reason if isinstance(reason, str) and reason else None,
        has_audio=receipt["has_audio"] if isinstance(receipt.get("has_audio"), bool) else None,
    )


def _with_narrated_text_rows(variant: Mapping[str, Any]) -> Mapping[str, Any]:
    """``variant`` with its phone Voiceover text rows as ordinary text lanes (KRI-963).

    A phone Narrated render keeps the title it drew (and text placed at a spoken word, or
    added in the editor) in ``narrated_title_text_elements``, the one store an editor Save
    rewrites, never in ``text_elements``. A row the creator removed is not on screen.
    """
    rows = variant.get("narrated_title_text_elements")
    if not isinstance(rows, list) or not rows:
        return variant
    current = variant.get("text_elements")
    lanes = list(current) if isinstance(current, list) else []
    shown = {row.get("id") for row in lanes if isinstance(row, Mapping)}
    extra = [
        row
        for row in rows
        if isinstance(row, Mapping) and not row.get("removed") and row.get("id") not in shown
    ]
    return {**variant, "text_elements": lanes + extra} if extra else variant


def plan_facts_from_phone_variant(variant: Mapping[str, Any] | None) -> PlanFacts:
    """Facts off a rendered phone variant (KRI-537).

    Starts from the editor-payload facts (on-screen text lanes, ``editor=True``) and adds
    what the render itself recorded: the ``phone_beat_receipt`` (where each pop-in landed,
    which words were never heard, the closing shot) and ``voiceover_bed_level`` (how loud
    the footage's own sound plays under the voice). Anything the variant does not carry
    stays unknown; nothing is guessed.
    """
    if not isinstance(variant, Mapping):
        return PlanFacts()
    base = plan_facts_from_editor_payload(
        _with_narrated_text_rows(variant), canonical_duration_s=_phone_output_duration(variant)
    )
    changes = _phone_receipt_facts(
        variant.get("phone_beat_receipt"), timed=variant.get("resolved_archetype") == "narrated"
    )
    measured = _finite_number(variant.get("measured_export_duration_s"))
    if (
        measured is not None
        and measured > 0
        and variant.get("ok") is True
        and variant.get("render_status") == "ready"
        and variant.get("measured_export_generation_id")
        and variant.get("measured_export_generation_id") == variant.get("render_generation_id")
    ):
        changes["measured_export_duration_s"] = measured
    changes["outro_s"] = _declared_outro_s(variant)
    bed = _rendered_bed_level(variant)
    if bed is not None:
        changes["voiceover_bed_level"] = max(0.0, bed)
        changes["audio_strategy"] = "voiceover"
    elif variant.get("resolved_archetype") in {"narrated", "voiceover"}:
        changes["audio_strategy"] = "voiceover"
    changes.update(_rendered_speech_facts(variant))
    changes.update(_explicit_speech_cut_facts(variant))
    changes.update(_engine_noise_reduction_facts(variant))
    changes.update(_speaker_framing_facts(variant))
    changes.update(_landscape_framing_facts(variant))
    changes.update(_opening_cut_facts(variant))
    named = variant.get("named_cuts")
    if isinstance(named, list):
        changes["named_cuts"] = tuple(row for row in named if isinstance(row, Mapping))
    changes.update(_title_face_facts(variant))
    changes.update(_spoken_text_facts(variant))
    changes.update(_protected_text_facts(variant))
    changes.update(_beat_room_facts(variant.get("phone_beat_receipt")))
    changes.update(_timed_card_receipt_facts(variant.get("phone_timed_card_receipt")))
    changes.update(_overlay_sequence_facts(variant.get("phone_overlay_receipt")))
    changes.update(_rendered_caption_facts(variant))
    receipt = variant.get("clip_sound")
    if isinstance(receipt, Mapping):
        changes["clip_sound"] = receipt  # KRI-842
    toggles = variant.get("clip_sound_toggles")
    if isinstance(toggles, Mapping):
        changes["clip_sound_toggles"] = toggles  # KRI-942: the worker's own judgement
    if variant.get("resolved_archetype") == "voiceover":
        changes["voiceover_edit"] = True  # KRI-842
    changes.update(_unclear_word_facts(variant))
    rendered = _rendered_clip_durations(variant)
    if rendered is not None:
        changes["clip_output_durations"] = rendered
    if changes.get("edit_format") == "subtitled":
        changes["phone_talking"] = True  # KRI-705
        # KRI-841: a finished Talking render says whether it really ended on the video.
        changes["ending_clip_known"] = True
        changes["ending_clip"] = _ending_clip_receipt_fact(variant.get("phone_ending_clip"))
    return dataclasses.replace(base, **changes) if changes else base


# KRI-541: the speech-spined archetypes a phone render records, as the brief's edit format.
# A multi-clip phone Talking edit keeps `resolved_archetype == "subtitled"` too.
_RENDERED_EDIT_FORMATS = {"narrated": "narrated", "subtitled": "subtitled"}


def _rendered_speech_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """Edit format, caption style and speech cleanup off a rendered Voiceover/Talking variant.

    Both phone writers persist ``caption_cues``, ``voiceover_caption_style`` ("sentence"
    or "word") and, only when cleanup ran, ``silence_cut_outcome``. A device render
    always writes its cues, so no cues there means no captions and no outcome means the
    take was kept whole; a cloud render fills those fields later, so their absence stays
    unknown. Any other archetype adds nothing (the checkers keep today's answers).
    """
    edit_format = _RENDERED_EDIT_FORMATS.get(str(variant.get("resolved_archetype") or ""))
    if edit_format is None:
        return {}
    device = variant.get("render_destination") == "device"
    facts: dict[str, Any] = {"rendered_variant": True, "edit_format": edit_format}
    cues = variant.get("caption_cues")
    if isinstance(cues, list) and cues:
        facts["caption_style"] = (
            "karaoke" if variant.get("voiceover_caption_style") == "word" else "clean"
        )
    elif device:
        facts["caption_style"] = "none"
    outcome = variant.get("silence_cut_outcome")
    if outcome in ("applied", "no_change"):
        facts["speech_cleanup_enabled"] = True
        facts["speech_cleanup_outcome"] = outcome
    elif outcome is None and device:
        facts["speech_cleanup_enabled"] = False
        facts["speech_cleanup_outcome"] = "not_run"
    return facts


def _explicit_speech_cut_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """Read only complete worker receipts; malformed rows cannot prove a spoken cut."""
    raw = variant.get("explicit_speech_cuts")
    if not isinstance(raw, list):
        return {}
    receipts: list[ExplicitSpeechCutFact] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        ids = row.get("requirement_ids")
        quote = row.get("quote")
        occurrence = row.get("occurrence")
        status = row.get("status")
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(req_id, str) and req_id for req_id in ids)
            or not isinstance(quote, str)
            or not quote.strip()
            or (
                occurrence is not None
                and (
                    not isinstance(occurrence, int)
                    or isinstance(occurrence, bool)
                    or occurrence < 1
                )
            )
            or status not in {"applied", "not_applied"}
        ):
            continue
        reason = row.get("reason")
        source_span = row.get("source_span")
        word_indexes = row.get("word_indexes")
        start_s = end_s = None
        indexes: tuple[int, ...] = ()
        if isinstance(source_span, Mapping):
            start_s = _finite_number(source_span.get("start_s"))
            end_s = _finite_number(source_span.get("end_s"))
        if isinstance(word_indexes, list) and all(
            isinstance(index, int) and not isinstance(index, bool) and index >= 0
            for index in word_indexes
        ):
            indexes = tuple(word_indexes)
        # An "applied" claim needs the exact source evidence the worker cut.  Top-level
        # convenience fields are not a substitute for its source_span contract.
        if status == "applied" and (
            start_s is None or end_s is None or end_s <= start_s or not indexes
        ):
            continue
        receipts.append(
            ExplicitSpeechCutFact(
                requirement_ids=tuple(ids),
                quote=" ".join(quote.split()),
                occurrence=occurrence,
                status=status,
                reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
                start_s=start_s,
                end_s=end_s,
                word_indexes=indexes,
            )
        )
    return {"explicit_speech_cuts": tuple(receipts)}


def _engine_noise_reduction_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """Read the explicit treatment receipt; a filter's presence never implies success."""
    raw = variant.get("engine_noise_reduction")
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        return {}
    ids = raw.get("requirement_ids")
    status = raw.get("status")
    method = raw.get("method")
    if (
        not isinstance(ids, list)
        or not ids
        or not all(isinstance(req_id, str) and req_id for req_id in ids)
        or status not in {"applied", "not_applied"}
        or method != "stationary_noise_reduction"
    ):
        return {}
    reason = raw.get("reason")
    allowed_reasons = {
        "reduced_not_isolated",
        "unavailable",
        "multiple_sources",
        "processing_failed",
    }
    if reason not in allowed_reasons:
        return {}
    return {
        "engine_noise_reduction": EngineNoiseReductionFact(
            requirement_ids=tuple(ids), status=status, reason=reason
        )
    }


def _overlay_sequence_facts(receipt: object) -> dict[str, Any]:
    """KRI-840: ``fullscreen_sequence`` off a variant's ``phone_overlay_receipt``.

    The "use all my Visuals full screen" layout spaces its cutaways evenly across the talk
    (``matcher: "sequence"``); a receipt that placed any of them full screen says so.
    """
    if not isinstance(receipt, Mapping) or receipt.get("matcher") != "sequence":
        return {}
    placed = receipt.get("placed")
    if any(
        isinstance(entry, Mapping) and entry.get("display") == "fullscreen"
        for entry in (placed if isinstance(placed, list) else [])
    ):
        return {"fullscreen_sequence": True}
    return {}


def _rendered_caption_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """The caption lines and their language off a rendered Voiceover/Talking variant (KRI-549).

    Both phone writers persist ``caption_cues`` (one ``text`` per line) and
    ``caption_language``. A device render with no cues shows no captions; a cloud render
    fills them later, so their absence stays unknown. Captions the creator turned off
    (``captions_enabled: false``) are not on screen, so nothing is read from them.
    """
    if _RENDERED_EDIT_FORMATS.get(str(variant.get("resolved_archetype") or "")) is None:
        return {}
    if variant.get("captions_enabled") is False:
        return {}
    cues = variant.get("caption_cues")
    texts = tuple(
        " ".join(str(cue.get("text") or "").split())
        for cue in (cues if isinstance(cues, list) else ())
        if isinstance(cue, Mapping)
    )
    texts = tuple(text for text in texts if text)
    if not texts:
        return {"caption_texts": ()} if variant.get("render_destination") == "device" else {}
    facts: dict[str, Any] = {"caption_texts": texts}
    language = variant.get("caption_language")
    if isinstance(language, str) and language.strip():
        facts["caption_language"] = language.strip()
    translated = variant.get("caption_translated_from")
    if isinstance(translated, str) and translated.strip():
        facts["caption_translated_from"] = translated.strip().casefold()
    return facts


def _unclear_word_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """KRI-849: what the phone Talking writer did about the unclear word (``caption_unclear``)."""
    receipt = variant.get("caption_unclear")
    if not isinstance(receipt, Mapping) or receipt.get("state") not in {
        "agree",
        "not_comparable",
        "applied",
        "error",
    }:
        return {}
    spans = [dict(row) for row in receipt.get("spans") or [] if isinstance(row, Mapping)]
    return {"unclear_words": {"state": receipt["state"], "spans": spans, "finished": True}}


# KRI-546: the order facts a unified montage's plan record holds, laid over a finished
# variant's facts so an order ask is judged at render-ready exactly as the plan was.
_MONTAGE_ORDER_FIELDS = (
    "ordering_basis",
    "ordering_fallback_clip_ids",
    "ordering_choice",
    "sequence_statuses",
    "sequence_unmet",
    "sequence_unconfirmed",
    "sequence_absent",
    "sequence_conflicts",
    "sequence_unlocated",
    "sequence_unlocated_intent_ids",
    "sequence_spots_met",
    "route_start",
    "route_end",
    "first_endpoint",
    "last_endpoint",
)
# How far a finished cut may start after / end before the held line and still hold it: the
# timeline is rounded to milliseconds and one frame at 30 fps is ~0.033 s.
_HELD_LINE_TOLERANCE_S = 0.05


def _rendered_clip_cuts(
    variant: Mapping[str, Any],
) -> list[tuple[str, float | None, float | None]]:
    """The finished edit's main-picture cuts in screen order: (media id, source start, end)."""
    rows = variant.get("story_timeline")
    cuts: list[tuple[float, int, str, float | None, float | None]] = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, Mapping) or row.get("lane", "clip") != "clip":
            continue
        media_id = row.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            continue
        at = _finite_number(row.get("output_start_s"))
        cuts.append(
            (
                at if at is not None else math.inf,
                index,
                media_id,
                _finite_number(row.get("source_start_s")),
                _finite_number(row.get("source_end_s")),
            )
        )
    cuts.sort(key=lambda cut: (cut[0], cut[1]))
    return [(media_id, start, end) for _at, _index, media_id, start, end in cuts]


def _rendered_non_video_ids(variant: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The media ids of the finished timeline's non-video cuts (KRI-994); None = unknown.

    Every lane counts: a photo shown as a card is still a photo in the video. A timeline
    with no rows, or a row that does not say what it is, says nothing.
    """
    rows = variant.get("story_timeline")
    if not isinstance(rows, list) or not rows:
        return None
    photos: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("kind"), str):
            return None
        if row["kind"] != "video":
            photos.append(str(row.get("media_id") or row["kind"]))
    return tuple(dict.fromkeys(photos))


def _rendered_holds(
    holds: tuple[Mapping[str, Any], ...] | None, variant: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...] | None:
    """KRI-824: each held photo's seconds as the finished timeline plays them."""
    rows = variant.get("story_timeline")
    if not holds or not isinstance(rows, list):
        return holds
    played: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("kind") != "image":
            continue
        start = _finite_number(row.get("output_start_s"))
        end = _finite_number(row.get("output_end_s"))
        media_id = row.get("media_id")
        if isinstance(media_id, str) and start is not None and end is not None and end > start:
            played[media_id] = played.get(media_id, 0.0) + (end - start)
    return tuple(
        {**hold, "rendered_s": round(played[hold["media_id"]], 4)}
        if hold.get("media_id") in played
        else hold
        for hold in holds
    )


def _same_file_positions(
    ids: Sequence[str], fingerprints: Mapping[str, str]
) -> tuple[tuple[int, ...], ...] | None:
    """1-based positions in ``ids`` that play the same original file, one group per file
    shown more than once (two cuts of one clip count too); None when a clip is unhashed."""
    if not ids or any(not fingerprints.get(media_id) for media_id in ids):
        return None
    by_file: dict[str, list[int]] = {}
    for position, media_id in enumerate(ids, start=1):
        by_file.setdefault(fingerprints[media_id], []).append(position)
    return tuple(tuple(group) for group in by_file.values() if len(group) > 1)


def _held_closing_line(
    record: Mapping[str, Any],
    clip_ids: Sequence[str],
    last_cut: tuple[str, float | None, float | None] | None,
) -> ClosingSpeechFact | None:
    """The record's held closing line when the finished edit really ends on it, else None."""
    speech = record.get("closing_speech")
    if not isinstance(speech, Mapping) or not clip_ids:
        return None
    media_id = str(speech.get("media_id") or "")
    start = _finite_number(speech.get("source_start_s"))
    end = _finite_number(speech.get("source_end_s"))
    if not media_id or start is None or end is None or end <= start or clip_ids[-1] != media_id:
        return None
    quote = str(speech.get("quote") or "")
    if last_cut is not None:
        # A finished timeline must show the closing cut covering the whole line.
        cut_start, cut_end = last_cut[1], last_cut[2]
        if (
            cut_start is None
            or cut_end is None
            or cut_start > start + _HELD_LINE_TOLERANCE_S
            or cut_end < end - _HELD_LINE_TOLERANCE_S
        ):
            return None
    return ClosingSpeechFact(media_id=media_id, start_s=start, end_s=end, quote=quote)


def plan_facts_from_rendered_montage(
    variant: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None,
    *,
    fingerprints: Mapping[str, str] | None = None,
) -> PlanFacts:
    """Facts off a finished phone unified montage (KRI-546).

    The variant's own facts (`plan_facts_from_phone_variant`: on-screen text, length) plus
    what only the plan record and the finished timeline together show: how the clips were
    ordered, whether the closing clip's spoken line plays whole in its own sound, and which
    clips came from the same original file. ``fingerprints`` maps each media id the creator
    added to the sha256 of its original upload; without it nothing about duplicates is
    claimed. Pass only the record of the generation this variant rendered.
    """
    facts = plan_facts_from_phone_variant(variant)
    if not isinstance(variant, Mapping) or not isinstance(record, Mapping):
        return facts
    plan = plan_facts_from_unified_montage(record)
    cuts = _rendered_clip_cuts(variant)
    clip_ids = tuple(media_id for media_id, _start, _end in cuts) or plan.clip_ids
    prints = {
        str(media_id): str(sha)
        for media_id, sha in (fingerprints or {}).items()
        if isinstance(media_id, str) and media_id and isinstance(sha, str) and sha
    }
    kept = variant.get("source_audio_preserved")
    return dataclasses.replace(
        facts,
        **{name: getattr(plan, name) for name in _MONTAGE_ORDER_FIELDS},
        clip_ids=clip_ids,
        rendered_output=True,
        rendered_montage=True,
        closing_speech=_held_closing_line(record, clip_ids, cuts[-1] if cuts else None),
        closing_quote_status=plan.closing_quote_status,
        closing_quote_text=plan.closing_quote_text,
        named_lines=_confirm_named_lines(plan.named_lines, cuts),
        cut_lengths_s=tuple(
            round(end - start, 3)
            for _media_id, start, end in cuts
            if start is not None and end is not None and end > start
        )
        or plan.cut_lengths_s,
        cut_clip_ids=tuple(media_id for media_id, _start, _end in cuts) or plan.cut_clip_ids,
        source_durations_s=plan.source_durations_s,
        asset_holds=_rendered_holds(plan.asset_holds, variant),
        song_bed=plan.song_bed,
        speed_unsupported=plan.speed_unsupported,
        clip_speeds=_rendered_speeds(plan.clip_speeds, variant),
        text_shapes=plan.text_shapes,
        timeline_non_video_ids=_rendered_non_video_ids(variant),
        freeze_frames=_rendered_freezes(plan.freeze_frames, variant),
        source_audio_kept=kept if isinstance(kept, bool) else None,
        source_clip_ids=tuple(prints),
        duplicate_clip_positions=_same_file_positions(clip_ids, prints) if prints else None,
        source_duplicate_copies=len(prints) - len(set(prints.values())) if prints else None,
    )


def _receipt(
    req: BriefRequirement,
    status: str,
    reason: str | None,
    inferred: Iterable[str] = (),
    labels: Iterable[InferredLabel] = (),
    *,
    verification: str | None = None,
    stage: str | None = None,
) -> RequirementReceipt:
    target_media_ids = [req.scope.split(":", 1)[1]] if req.scope.startswith("clip:") else []
    return RequirementReceipt(
        requirement_id=req.id,
        status=status,  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        target_media_ids=target_media_ids,
        reason=_loc(reason)[:300] if reason else None,
        inferred=list(dict.fromkeys(str(x) for x in inferred))[:24],
        inferred_labels=list(labels)[:24],
    )


def _guess_labels(facts: PlanFacts, only: str | None = None) -> list[InferredLabel]:
    """The guessed names with the clip each belongs to (plan order first)."""
    ids = list(facts.clip_ids)
    order = [c for c in ids if c in facts.inferred_text] or list(facts.inferred_text)
    if only is not None:
        order = [c for c in order if c == only]
    return [
        InferredLabel(
            text=facts.inferred_text[clip][:120],
            media_id=clip,
            clip_index=ids.index(clip) if clip in ids else None,
        )
        for clip in order
    ]


_CANT_CHECK_EDITOR_CLIP_TEXT = "I can't verify per-clip text on an editor edit."
_CANT_CHECK_TARGET_CLIP_TEXT = "I can't verify text for that target clip in this draft."


def _plan_clip_id(scoped: str, ids: Sequence[str]) -> str | None:
    """The plan clip a ``clip:<id>`` scope names, else None.

    A brief names a Visuals photo by its manifest id (``asset-<uuid>``); a unified montage
    record names it by the bare pool id, so the label on that photo is still its label.
    """
    if scoped in ids:
        return scoped
    bare = scoped.removeprefix("asset-")
    return bare if bare != scoped and bare in ids else None


def _check_per_clip_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if (
        req.scope == "per_clip"
        and wants_hour_only_text(req.description, req.literal)
        and facts.per_clip_text
    ):
        # "Just the hour, no minutes" is a FORMAT: met only when every label the plan
        # actually carries is a bare hour, never because the labels merely exist.
        bad = [c for c, t in facts.per_clip_text.items() if not re.fullmatch(r"\d{1,2}", t.strip())]
        if bad:
            return _receipt(
                req,
                "partial",
                say(
                    en=(
                        f"{len(bad)} of {len(facts.per_clip_text)} labels still show more than "
                        "the hour. A re-render can't reformat them; ask me again to change "
                        "them in the editor."
                    ),
                    tr=(
                        f"{len(facts.per_clip_text)} etiketin {len(bad)} tanesi hâlâ saatten "
                        "fazlasını gösteriyor. Videoyu yeniden oluşturmak bunları düzeltmez; "
                        "editörde değiştirmem için tekrar iste."
                    ),
                ),
            )
    wanted = _fold(req.literal or "")
    core = _stand_in_core(req.literal)
    if core is not None:
        # KRI-282: "[Name]" is judged by the stand-in the plan actually printed.
        stand_in = _fold(PLACEHOLDER_LABEL_TEXT)
        printed = any(_fold(t) == stand_in for t in facts.per_clip_text.values())
        wanted = stand_in if printed else core
    if facts.editor and not facts.has_clip_structure:
        # An editor payload's loose text list cannot prove which clip owns a
        # caption. In particular it must never fulfill `clip:<id>` because an
        # unrelated label contains the same literal.
        if req.scope.startswith("clip:") or (
            wanted and any(_contains_text(t, wanted) for t in facts.texts)
        ):
            return _receipt(req, "partial", _CANT_CHECK_EDITOR_CLIP_TEXT)
        if wanted:
            return _receipt(
                req,
                "partial",
                say(
                    en="That exact text isn't in this edit.",
                    tr="Bu tam yazı bu düzenlemede yok.",
                ),
            )
        return _receipt(req, "partial", _CANT_CHECK_EDITOR_CLIP_TEXT)
    if facts.editor and wanted and _wants_exact_text(req):
        # "just say X" / "X only": every clip this turn touched must read exactly X.
        off = [c for c, t in facts.per_clip_text.items() if _fold(t) != wanted]
        if off:
            total = len(facts.per_clip_text)
            return _receipt(
                req,
                "partial",
                say(
                    en=(
                        f"{len(off)} of {total} text{'s' if total != 1 else ''} "
                        f"didn't end up reading exactly \u201c{req.literal}\u201d."
                    ),
                    tr=(
                        f"{total} yazının {len(off)} tanesi tam olarak "
                        f"\u201c{req.literal}\u201d olmadı."
                    ),
                ),
            )
    if (
        not facts.editor
        and wants_filming_time_text(req.kind, req.scope, req.literal, req.description, req.facts)
        and facts.per_clip_text
    ):
        judged = _check_filming_time_text(req, facts)
        if judged is not None:
            return judged
    ids = facts.clip_ids

    def text_for(index: int, clip: str) -> str | None:
        if clip in facts.per_clip_text:
            return facts.per_clip_text[clip]
        if index < len(facts.positional_labels):
            return facts.positional_labels[index]
        return None

    if req.scope.startswith("clip:"):
        clip = _plan_clip_id(req.scope.split(":", 1)[1], ids)
        if clip is None:
            return _receipt(req, "partial", _CANT_CHECK_TARGET_CLIP_TEXT)
        index = ids.index(clip) if clip in ids else len(ids)
        value = text_for(index, clip)
        if value is None and clip in facts.repeat_label_clip_ids:
            return _receipt(
                req,
                "partial",
                say(
                    en="That clip's label repeated the clip before it, so I left it off.",
                    tr=("O klibin etiketi önceki klibin etiketiyle aynıydı, o yüzden koymadım."),
                ),
            )
        if value is None:
            return _receipt(
                req,
                "not_possible",
                say(
                    en="That clip didn't get its own text in this draft.",
                    tr="O klibe bu taslakta kendi yazısı gelmedi.",
                ),
            )
        if wanted and not _contains_text(value, wanted):
            return _receipt(
                req,
                "partial",
                say(
                    en="That clip's text isn't the exact text you gave.",
                    tr="O klibin yazısı verdiğin yazıyla aynı değil.",
                ),
            )
        guessed = _guess_labels(facts, only=clip)
        return _receipt(req, "met", None, [g.text for g in guessed], guessed)

    scope = set(facts.label_scope_clip_ids)
    if not scope and req.is_shot_text:
        # KRI-1110: words dictated for described shots belong on the clips they were placed
        # on, not on every upload. A rule for every clip (no words) keeps judging them all.
        scope = set(facts.shot_text_clip_ids.get(_placement_key(req.literal), ()))
    judged_ids = [(i, clip) for i, clip in enumerate(ids) if not scope or clip in scope]
    total = len(judged_ids)
    if ids:
        count = sum(1 for i, clip in judged_ids if text_for(i, clip) is not None)
    else:
        count = max(len(facts.per_clip_text), len(facts.positional_labels))
    guessed = _guess_labels(facts)
    inferred = [g.text for g in guessed]
    if wanted and count:
        given = [*facts.per_clip_text.values(), *facts.positional_labels]
        if not any(_contains_text(t, wanted) for t in given):
            return _receipt(
                req,
                "partial",
                say(
                    en="The clips don't carry the exact text you gave.",
                    tr="Kliplerde verdiğin tam yazı yok.",
                ),
                inferred,
                guessed,
            )
    if total and count >= total:
        if facts.unreadable_label_clip_ids:
            n = len(facts.unreadable_label_clip_ids)
            return _receipt(
                req,
                "partial",
                say(
                    en=(
                        f"{n} clip{'s are' if n != 1 else ' is'} too short for its text to stay "
                        "on screen long enough to read."
                    ),
                    tr=f"{n} klip, yazısının okunacak kadar ekranda kalması için çok kısa.",
                ),
                inferred,
                guessed,
            )
        return _receipt(req, "met", None, inferred, guessed)
    if count == 0:
        return _receipt(
            req,
            "not_possible",
            say(
                en=(
                    "No clip got its own text in this draft."
                    if not total
                    else f"None of the {total} clips got its own text in this draft."
                ),
                tr=(
                    "Bu taslakta hiçbir klibe kendi yazısı gelmedi."
                    if not total
                    else f"Bu taslakta {total} klibin hiçbirine kendi yazısı gelmedi."
                ),
            ),
        )
    reason = say(
        en=(f"Text landed on {count} of {total} clips." if total else f"Text on {count} clips."),
        tr=(
            f"Yazı {total} klibin {count} tanesine geldi." if total else f"{count} klipte yazı var."
        ),
    )
    repeats = len(facts.repeat_label_clip_ids)
    if repeats:
        # Left off on purpose: the same name twice in a row adds nothing to the viewer.
        reason = say(
            en=(
                f"{reason[:-1]}; {repeats} more repeated the label before, so I left "
                f"{'them' if repeats != 1 else 'it'} off."
            ),
            tr=(f"{reason[:-1]}; {repeats} tanesi önceki etiketin aynısıydı, o yüzden koymadım."),
        )
    return _receipt(req, "partial", reason, inferred, guessed)


def _check_filming_time_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """ "Add the hour to each video": place names or nothing are NOT the hour (KRI-219)."""
    kinds = facts.per_clip_label_kinds
    if not kinds:
        return None  # grounding unknown: fall through to the coverage check
    total = len(facts.clip_ids) or len(facts.per_clip_text)
    timed = [c for c in facts.per_clip_text if kinds.get(c) == "capture_time"]
    if not timed:
        return _receipt(
            req,
            "not_possible",
            say(
                en="The labels are place names, not the hour each clip was filmed.",
                tr="Etiketler yer adı, klibin çekildiği saat değil.",
            ),
        )
    if len(timed) < total:
        missing = total - len(timed)
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"Filming hour on {len(timed)} of {total} clips; "
                    f"{missing} {'have' if missing != 1 else 'has'} no filming time."
                ),
                tr=(
                    f"Çekim saati {total} klibin {len(timed)} tanesinde var; "
                    f"{missing} klibin çekim saati yok."
                ),
            ),
        )
    if facts.label_timezone:
        # Delivered, but the creator must know which zone the hours are in.
        from app.services.clip_facts import timezone_note  # noqa: PLC0415

        return _receipt(req, "met", timezone_note(facts.label_timezone, facts.label_timezone_basis))
    return None


_NAME_IN_REASON_CHARS = 32


def _short(name: str) -> str:
    text = " ".join(name.split())
    return text if len(text) <= _NAME_IN_REASON_CHARS else text[: _NAME_IN_REASON_CHARS - 1] + "…"


def _names_place(endpoint: EndpointFact | None, name: str) -> bool:
    """True when the creator's ``name`` is one of the places recorded for ``endpoint``.

    Matches whole words either way round, ignoring case and Turkish diacritics, against
    the full geocode and each comma-separated part of it ("Fatih" in "Eminönü, Fatih,
    İstanbul"). A one-word name never matches inside a longer word.
    """
    if endpoint is None:
        return False
    wanted = loose_text(name)
    if not wanted:
        return False
    for place in (*endpoint.places, endpoint.label):
        for part in (place, *(p for p in place.split(",") if p.strip())):
            seen = loose_text(part)
            if not seen:
                continue
            if seen == wanted:
                return True
            for hay, needle in ((seen, wanted), (wanted, seen)):
                if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay):
                    return True
    return False


def _route_reversed_reason(req: BriefRequirement, facts: PlanFacts) -> str | None:
    """A plain-language reason when the clips were filmed in the opposite direction.

    The plan follows filming order (capture time). If the creator stated a route and the
    first/last clips' places say it was walked the other way, the receipt says what was
    seen and what was done and asks which to follow. It never silently reorders, and
    stays quiet unless a side positively matches the reverse and nothing matches forward.
    """
    if facts.ordering_basis not in _CAPTURE_BASES:
        return None
    start = first_text(req.facts, START_KEYS) or facts.route_start
    end = first_text(req.facts, END_KEYS) or facts.route_end
    if not start or not end or facts.first_endpoint is None or facts.last_endpoint is None:
        return None
    first, last = facts.first_endpoint, facts.last_endpoint
    untimed = set(facts.ordering_fallback_clip_ids)
    if (first.media_id and first.media_id in untimed) or (
        last.media_id and last.media_id in untimed
    ):
        # An endpoint without a capture time sits in an attachment slot, not where it was
        # filmed: it says nothing about the direction the route was walked.
        return None
    forward = _names_place(first, start) or _names_place(last, end)
    backward = _names_place(first, end) or _names_place(last, start)
    if forward or not backward:
        return None
    saw_first, saw_last = _short(first.label or end), _short(last.label or start)
    return say(
        en=(
            f"Your clips were filmed starting at {saw_first} and ending at {saw_last}, the "
            f"reverse of the route you gave ({_short(start)} → {_short(end)}). I kept filming "
            "order; tell me if you want your route order instead."
        ),
        tr=(
            f"Klipler şöyle çekilmiş: başlangıç {saw_first}, bitiş {saw_last}. Bu, verdiğin "
            f"rotanın ({_short(start)} → {_short(end)}) tersi. Çekim sırasını korudum; rota "
            "sırasını istersen söyle."
        ),
    )


# A strategy draft never records its clip order (only a unified montage plan
# does), so a draft-time order check can only say it couldn't tell.
_CANT_CONFIRM_ORDER = "I can't confirm the order this draft uses."
_CANT_CHECK_ORDER_RULE = "I can't verify this ordering automatically."
_ARRIVAL_BASES = frozenset({"attachment", "creator_order"})
_ORDER_NOT_APPLIED = (
    "I couldn't match your description to the clips, so they stay in the order you attached them."
)
# KRI-778: an order the creator listed in their own words that the plan only partly placed,
# said in their terms (what they listed), never the planner's "groups".
_ORDER_LIST_NOT_PLACED = "I couldn't place everything you listed in the order you gave."
# KRI-723: named moments the plan cannot show are those actions. An app that cannot answer the
# "show me where each moment is" card gets windows nobody located; the creator hears it.
_MOMENTS_NOT_LOCATED = (
    "I couldn't find every moment you named in your clip, so I can't confirm the ones playing "
    "are those moments"
)
# ... and the moments the creator left to Kria or said are not in their clips.
_MOMENTS_CREATOR_CALL = (
    "Some moments are your call: ones you said aren't in your clips, or let me pick"
)
_MOMENT_LIMIT_CODES = frozenset({"not_located", "delegated", "skipped"})
# KRI-470 PR-G: what a required order says when it was not met.
_ORDER_NOT_RECORDED = "I couldn't confirm the order your edit used."
_ORDER_RULE_NOT_APPLIED = (
    "I can't verify this ordering rule, and the clips are not in an order I can show matches it."
)
_ORDER_VERIFIED_ELSEWHERE = frozenset({"song_time", "confirmed", "editor", "attachment_order"})
_PREFERENCE_STRENGTHS = frozenset({"preference", "prefer", "optional", "soft", "nice_to_have"})


def _order_is_required(req: BriefRequirement) -> bool:
    """An order the creator asked for is REQUIRED, exactly as the render contract pins it
    (`order_required`). Only an order explicitly marked as a preference stays optional."""
    strength = str(req.facts.get("strength") or req.facts.get("priority") or "").casefold()
    return req.facts.get("required") is not False and strength not in _PREFERENCE_STRENGTHS


_ORDER_ANCHOR_FACTS = (("first_clip", "first", "start with"), ("last_clip", "last", "end with"))


def _unplaced_order_anchor(req: BriefRequirement, facts: PlanFacts) -> str | None:
    """Why a stated first/last clip is unconfirmed, in the creator's own words, else None."""
    if facts.sequence_spots_met is None or not facts.rendered_output:
        return None
    said: list[str] = []
    for key, spot, verb in _ORDER_ANCHOR_FACTS:
        words = req.facts.get(key)
        if isinstance(words, str) and words.strip() and spot not in facts.sequence_spots_met:
            said.append(f"{verb} {words.strip()}")
    if not said:
        return None
    return f"You asked me to {' and '.join(said)}, but I can't confirm that clip is placed there."


def _listed_order_miss(facts: PlanFacts) -> str:
    """KRI-778: an order described in the creator's words that the plan only partly placed.

    Whole sentences naming what missed, as the creator said it, so the blocked render's
    message reads "... Caro's bye-bye wave (last) isn't where you asked." instead of the old
    lowercase fragment about "groups".
    """
    said = [say(en=_ORDER_LIST_NOT_PLACED, tr=_REASON_TR[_ORDER_LIST_NOT_PLACED])]
    if facts.sequence_unmet:
        missed = _and_names(facts.sequence_unmet)
        verb = "isn't" if len(dict.fromkeys(facts.sequence_unmet)) == 1 else "aren't"
        said.append(
            say(en=f"{missed} {verb} where you asked.", tr=f"{missed} istediğin yerde değil.")
        )
    if facts.sequence_absent:
        said.append(
            say(
                en=f"I found no clips for {_and_names(facts.sequence_absent)}.",
                tr=f"{_and_names(facts.sequence_absent)} için klip bulamadım.",
            )
        )
    return " ".join(said)


_RANDOM_ORDER_NOT_APPLIED = "I couldn't confirm the clips are in a random order."
_RANDOM_ORDER_OVERRIDDEN = "You moved the clips yourself, so your own order is kept."


def _check_random_order(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A random order is met when the plan's order IS the stored seed's permutation.

    A unified montage records the seed and the clips it shuffled, so the check recomputes
    ``seeded_permutation(source, seed)`` and compares it with the order the plan used (an
    explicit start/end sequence outranks it, so there only "a permutation, nothing lost or
    repeated" can be promised). The editor op only says "Shuffle clips", which can prove the
    permutation but not the seed. A creator's own later hand reorder displaces the ask.
    """
    required = facts.strict_order and _order_is_required(req)
    if not facts.ordering_basis:
        if required and facts.rendered_output:
            return _receipt(req, "not_possible", _ORDER_NOT_RECORDED)
        return _receipt(req, "partial", _CANT_CONFIRM_ORDER)
    if facts.ordering_basis == "creator_order":
        return _receipt(
            req, "met", say(en=_RANDOM_ORDER_OVERRIDDEN, tr=_REASON_TR[_RANDOM_ORDER_OVERRIDDEN])
        )
    unmet = "not_possible" if required else "partial"
    reason = say(en=_RANDOM_ORDER_NOT_APPLIED, tr=_REASON_TR[_RANDOM_ORDER_NOT_APPLIED])
    if facts.ordering_basis != "random":
        return _receipt(req, unmet, reason)
    source = facts.random_order_source_ids
    if source:
        wanted = set(source)
        used = [c for c in facts.clip_ids if c in wanted]
        if sorted(used) != sorted(source):
            return _receipt(req, unmet, reason)
        if (
            facts.random_order_seed is not None
            and not facts.sequence_statuses
            and used != seeded_permutation(source, facts.random_order_seed)
        ):
            return _receipt(req, unmet, reason)
    return _receipt(req, "met", None)


def _check_order(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A required order is met, partly met (an honest fallback), or FAILED. Neutral
    (unchecked) is only for an optional preference, and for a draft that has not been
    laid out yet (its order is judged when it renders)."""
    if req.order_replacement is not None:
        proof = req.order_replacement
        evidence = facts.verified_order_execution
        if type(evidence) is not VerifiedOrderExecution or not evidence.matches(proof):
            return _receipt(req, "partial", _CANT_CONFIRM_ORDER)
        if (
            facts.sequence_unconfirmed
            or facts.sequence_conflicts
            or facts.sequence_unmet
            or facts.sequence_absent
            or len(facts.sequence_unlocated) != len(facts.sequence_unlocated_intent_ids)
            or any(
                code != "skipped" or intent_id != proof.proposal.rejected_intent_id
                for code, intent_id in zip(
                    facts.sequence_unlocated, facts.sequence_unlocated_intent_ids, strict=True
                )
            )
        ):
            return _receipt(req, "partial", _CANT_CONFIRM_ORDER)
        return _receipt(req, "met", None)
    if facts.narrated_steps is not None:
        return _check_narrated_order(req, facts)
    if facts.sequence_unconfirmed:
        return _receipt(req, "partial", unconfirmed_order_reason(facts.sequence_unconfirmed))
    if facts.sequence_conflicts and not (facts.sequence_unmet or facts.sequence_absent):
        # KRI-764: one clip asked to be first AND last. The earlier ask stands and the edit
        # still renders, so say so (before approval too) instead of refusing the render.
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    "You asked for the same clip in two places: "
                    f"{', '.join(facts.sequence_conflicts)}. I kept the first place you named."
                ),
                tr=(
                    "Aynı klibi iki ayrı yere istedin: "
                    f"{', '.join(facts.sequence_conflicts)}. İlk söylediğin yeri korudum."
                ),
            ),
        )
    mode = order_mode(req)
    if mode == "any":
        if facts.sequence_unlocated:
            reason = (
                _MOMENTS_NOT_LOCATED
                if "not_located" in facts.sequence_unlocated
                else _MOMENTS_CREATOR_CALL
            )
            return _receipt(req, "partial", say(en=reason, tr=_REASON_TR[reason]))
        return _receipt(req, "met", None)  # any order is the order they asked for
    if mode == "random":
        return _check_random_order(req, facts)
    key = str(req.facts.get("key") or req.facts.get("by") or "").casefold()
    required = facts.strict_order and _order_is_required(req)
    unmet = "not_possible" if required else "partial"
    if not facts.ordering_basis:
        if required and facts.rendered_output:
            return _receipt(req, "not_possible", _ORDER_NOT_RECORDED)
        return _receipt(req, "partial", _CANT_CONFIRM_ORDER)
    basis = facts.ordering_basis
    reversed_reason = _route_reversed_reason(req, facts)
    if reversed_reason is not None:
        return _receipt(req, "partial", reversed_reason)
    if key in _CAPTURE_ORDER_KEYS:
        if basis not in _CAPTURE_BASES:
            return _receipt(
                req,
                unmet,
                say(
                    en=(
                        f"This draft is ordered by {basis.replace('_', ' ')}, not the order "
                        "you asked for."
                    ),
                    tr=(
                        "Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: "
                        f"{_BASIS_TR.get(basis, basis.replace('_', ' '))}."
                    ),
                ),
            )
    elif facts.sequence_statuses:
        # The creator described the order in their own words; the plan placed (or
        # failed to place) each group and recorded which (KRI-458).
        landed = sum(status == "met" for status in facts.sequence_statuses)
        honest = len(facts.sequence_unlocated)
        if landed + honest < len(facts.sequence_statuses):
            return _receipt(
                req,
                "partial" if landed or not required else "not_possible",
                _listed_order_miss(facts),
            )
        if honest:
            # KRI-723: placed as asked, but not every named moment is evidenced as itself.
            reason = (
                _MOMENTS_NOT_LOCATED
                if "not_located" in facts.sequence_unlocated
                else _MOMENTS_CREATOR_CALL
            )
            return _receipt(req, "partial", say(en=reason, tr=_REASON_TR[reason]))
    elif basis in _ARRIVAL_BASES and key != basis:
        # An order only the creator's words describe, and nothing in the plan applied
        # it: say so, never let a silent attachment order read as "done" (KRI-458).
        return _receipt(req, unmet, _ORDER_NOT_APPLIED)
    elif not key or key != basis:
        # Nothing here can confirm an ordering this checker has no rule for. When the
        # order is required, "can't confirm" is not a pass: the plan is in some other
        # order than the one asked for. Except where another authority owns the order and
        # verifies it: a lip-sync montage's song placement (the contract skips a creator's
        # "use this order" answer there, #1451, and the lip-sync receipts verify the
        # takes) and the contract's own confirmed / editor / answered order ids. A rule
        # the checker has no key for stays unjudged there instead of blocking a render
        # that placed it.
        if required and basis not in _ORDER_VERIFIED_ELSEWHERE:
            return _receipt(req, "not_possible", _ORDER_RULE_NOT_APPLIED)
        return _receipt(req, "partial", _CANT_CHECK_ORDER_RULE)
    if (
        key in _CAPTURE_ORDER_KEYS
        and facts.strict_order
        and (facts.sequence_unmet or facts.sequence_absent)
    ):
        # KRI-503: "chronological order, starting with the blue video". The basis matching is
        # not enough: the stated clip must really be where the creator put it. The render
        # contract pins that same seating, so a plan that misses it would be refused after
        # the render; say so before.
        said = []
        if facts.sequence_unmet:
            said.append(
                say(
                    en=f"these aren't where you asked: {', '.join(facts.sequence_unmet)}",
                    tr=f"bunlar istediğin yerde değil: {', '.join(facts.sequence_unmet)}",
                )
            )
        if facts.sequence_absent:
            said.append(
                say(
                    en=f"I found no clips for: {', '.join(facts.sequence_absent)}",
                    tr=f"şunlar için klip bulamadım: {', '.join(facts.sequence_absent)}",
                )
            )
        return _receipt(
            req,
            unmet,
            say(
                en=f"Your clips are in filming order, but {'; '.join(said)}.",
                tr=f"Klipler çekim sırasında, ama {'; '.join(said)}.",
            ),
        )
    missing_anchor = _unplaced_order_anchor(req, facts)
    if missing_anchor is not None:
        # KRI-522: "chronological, starting with the blue video" lives in the brief as
        # `first_clip`. A plan that recorded no placed first/last group cannot claim it.
        # Partial, not a block: the edit is still the creator's filming order, and saying
        # so beats failing a render that is otherwise right.
        return _receipt(req, "partial", missing_anchor)
    if facts.ordering_fallback_clip_ids:
        n = len(facts.ordering_fallback_clip_ids)
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"{n} clip{'s' if n != 1 else ''} had no capture time, so I kept "
                    f"{'their' if n != 1 else 'its'} attachment order."
                ),
                tr=(f"{n} klibin çekim zamanı yoktu, o yüzden bunları eklediğin sırada bıraktım."),
            ),
        )
    if key in _CAPTURE_ORDER_KEYS and facts.ordering_choice == "group_first":
        # Honest: the creator picked grouping over strict filming order, so the order
        # holds inside each group only.
        return _receipt(
            req,
            "met",
            say(
                en="you chose grouping first, so it's in filming order inside each group",
                tr="önce gruplamayı seçtin, o yüzden her grubun içinde çekim sırasında",
            ),
        )
    return _receipt(req, "met", None)


# ------------------------------------------------ requirement shape recognisers
#
# The brief extractor has no dedicated kind for "keep my whole take" or "pop up X
# when I say Y": the first arrives as a `timing` requirement with no number, the
# second as `style`/`audio`/`select`. These deterministic recognisers read the
# creator's framing (description/literal, Turkish-aware fold) and `facts` keys.

_WHOLE_TAKE_RE = re.compile(
    r"\b(whole|full|entire|complete|original) (take|clip|video|recording|footage)\b"
    r"|\b(exactly )?as (i )?(recorded|filmed|shot) it\b|\bexactly as (i )?(recorded|filmed)\b"
    r"|\bas recorded\b|\bstart to finish\b|\bbeginning to (the )?end\b"
    r"|\b(don'?t|do not|never|no) (cut|trim|shorten)|\buncut\b|\bfull[- ]length\b"
    r"|\bbaştan sona\b|\bolduğu gibi\b|\bkesmeden\b|\bhiç kesme|\btamam[iı]n[iı]\b"
)
# Who says the trigger: the creator in the first or second person ("when I say ..."), or,
# because the brief rewrites asks into third person, their voice ("when the voiceover
# says ...", "when the narration mentions ...").
_CUE_SPEAKER = (
    r"(?:(?:i|you|we)"
    r"|(?:my |the |our |this |her |his )?"
    r"(?:voice[- ]?over|narration|narrator|recording|audio|script|voice|speech|commentary))"
)
_CUE_VERB = (
    r"(?:say|says|mention|mentions|hear|hears|name|names|talk(?:s)? about|read(?:s)?"
    r"|bring(?:s)? up|get(?:s)? to)"
)
_CUE_OPENER = r"(?:when|whenever|as soon as|the moment|the second|every time)"
# The creator's voice is the trigger ("when I say ...", "dediğimde"). Without a named word,
# only this counts as a pop-in ask; "pop up" and "sticker" alone do not.
_SPOKEN_CUE_RE = re.compile(
    rf"\b{_CUE_OPENER} {_CUE_SPEAKER} {_CUE_VERB}\b|\bwhen you hear\b"
    r"|\bat (the |each |every )?(exact |spoken |specific )?words?\b|\bword[- ]triggered\b"
    r"|dedi[ğg]i[mn]de|deyince|denince|dendi[ğg]inde|s[öo]yledi[ğg]i[mn]de|bahsetti[ğg]i[mn]de"
    r"|ge[çc]ti[ğg]i[mn]de|ge[çc]ince"
    # KRI-540: the passive / spoken-word forms the Kadıköy brief used ("'İlk durak'
    # dendiğinde", "her 'kahve' kelimesinde"). Written against `_fold` output
    # (ı -> i), tolerant of ASCII-typed ğ/ç/ş/ö/ü. Turkish "every X" ("her 'X'de",
    # "her X'te") REQUIRES a quote or apostrophe suffix so English "her" never matches.
    r"|\b(?:dend[ıi][ğg]inde|denince|denild[ıi][ğg]inde|derken|ded[ıi][ğg]inde"
    r"|s[öo]ylend[ıi][ğg]inde|s[öo]yleyince|ge[çc]t[ıi][ğg]inde|ge[çc]ince|ge[çc]erken"
    r"|duyuld[uü][ğg]unda|duyunca)\b"
    r"|\bkelimesi(?:nde|ni)\b|\bs[öo]zc[üu][ğg][üu]nde\b|\bs[öo]z[üu]nde\b"
    r"|\bher\s+[\"'\u201c\u201d\u2018\u2019\u00ab\u00bb][^\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]{1,60}"
    r"[\"'\u201c\u201d\u2018\u2019\u00ab\u00bb](?:d[ea]|t[ea])\b"
    r"|\bher\s+[^\s\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]{1,40}['\u2019](?:d[ea]|t[ea])\b"
)
_BEAT_CUE_RE = re.compile(
    rf"{_SPOKEN_CUE_RE.pattern}|\bpop(s|ping)?[- ]?(up|in|ups|ins)\b|\b(sticker|stamp)s?\b"
    r"|\b[çc]ikartma"
)
# KRI-791: a text on a spoken word is also asked for "the first time ..." and in someone
# else's voice ("... the first time she says Merope"). Only the trigger reader below knows
# these: the spoken-cue recogniser the pop-in, whole-line and cut asks share is unchanged, so
# "keep the whole line when she says it" or "a ding every time someone says cheers" read as
# before. Someone else's voice counts only with a say-verb ("when they get to the summit" is
# something seen, not said).
_TRIGGER_CUE_OPENER = rf"(?:{_CUE_OPENER}|the (?:first|1st) time)"
_OTHER_CUE_SPEAKER = r"(?:she|he|they|someone|somebody|anyone|anybody|everyone)"
_SAY_VERB = r"(?:say|says|said|mention|mentions)"
# The words after the cue verb ("... when voiceover says the medal, show ..."): the trigger
# the creator named without quoting it.
_CUE_TRIGGER_RE = re.compile(
    rf"\b{_TRIGGER_CUE_OPENER} (?:{_CUE_SPEAKER} {_CUE_VERB}"
    rf"|(?P<other>{_OTHER_CUE_SPEAKER}) {_SAY_VERB})\s+"
    r"(?:the (?:words?|phrase|name) )?(?P<trigger>.+)",
    re.IGNORECASE,
)
# The trigger ends at punctuation or at ", and show ..." (a conjunction and an action verb).
_CUE_TRIGGER_END_RE = re.compile(
    r"[,;.!?:]|\s+(?:and|then|so|but)\s+(?:then\s+)?(?:show|put|pop|display|add|play|flash|insert"
    r"|overlay|throw|cut|bring)\b",
    re.IGNORECASE,
)
# A described kind of word ("any food name") is not a spoken phrase to look for.
_GENERIC_TRIGGER_RE = re.compile(
    r"\b(?:any|every|each|all|some|something|anything|whatever|whichever|names?|words?|things?"
    r"|numbers?|phrases?|places?|people|persons?|countries|country|cities|city|them|it|that"
    r"|those|these|her|hepsi\w*|herhangi)\b",
    re.IGNORECASE,
)
_TRIGGER_STOPWORDS = frozenset(
    "a an the it its we i you he she they me my our your his her their this that these those "
    "to of and or but so then is are was were be been in on at for with from by as if".split()
)
_CLOSING_RE = re.compile(
    r"\b(finish|end|close|wrap up|wrap) (on|with)\b|\bending (on|with|shot)\b"
    r"|\bclosing (shot|photo|image|picture|frame)\b|\blast (shot|frame)\b"
    r"|\bbitir|\bkapan[iı]ş"
)
_SOUND_RE = re.compile(
    r"\b(sound|sounds|sfx|buzzer|ding|beep|whoosh|swoosh|boing|horn|applause)\b|\bses(?:i|ini|iyle|ler|leri|lerini)?\b|\befekt"
)
_VISUAL_RE = re.compile(
    r"\b(sticker|stamp|badge|photo|picture|image|pic|flag|logo|emoji)s?\b"
    r"|fotoğraf|resim|görsel|çikartma|bayrak"
)
# "Cut out the long pauses / the retake / the ums": a speech-cleanup ask is a removal
# verb plus something speech cleanup removes (or a named retake). The brief extractor
# files it as `timing` (often with the length the creator also named), `audio`,
# `style` or `select`; the ask itself is read from the creator's words. Patterns are
# written against `_fold` output: lower case, every Turkish ı folded to i.
_CLEANUP_NOUN_RE = re.compile(
    r"\b(pauses?|silences?|dead air|dead space|retakes?|fillers?|filler words?|stumbles?"
    r"|false starts?)\b|\b(um+s?|uh+s?|er+s?|hmm+s?)\b"
    r"|\bduraklama|\bsessizlik|\bdolgu|\btak[iı]lma"
)
_CLEANUP_VERB_RE = re.compile(
    r"\b(cut|remove|trim|take out|get rid of|drop|delete|skip|lose|edit out|without|no)\b"
    r"|\bkes|\bçikar|\bsil\b|\bolmasin|\bolmadan"
)
# A specific stretch of speech named for removal ("the part where I say ...", a retake,
# a quoted line): a generic cleanup record cannot confirm that this exact spoken line was removed.
# KRI-792: a slip, stumble, mistake or half-finished question the creator says to cut names
# spoken words too. Only right after the cut verb (Turkish: right before it), so "captions
# with no spelling mistakes" stays a captions ask. The fallback for a brief without
# `facts.cut_lines` (see `_wants_named_cuts`).
_NAMED_CUT_RE = re.compile(
    r"\bretakes?\b|\brestart(s|ed|ing)?\b|\bfalse starts?\b"
    r"|\b(the )?(part|bit|place|section|moment) where\b"
    r"|\bwhere i (say|said|start|stumble)|\blet me (start|try|say|do) (that|it|this|again|over|one)"
    r"|\bstart (that|it|this|one) (again|over)\b|\bstart (again|over)\b"
    r"|\btry (that|it|this) again\b"
    r"|\bscratch that\b|\bwhere was i\b"
    r"|\b(cut|remove|trim|drop|delete|lose|take out|edit out|get rid of)( out)?"
    r" (the |my |her |his |their |our |that |this |those |these |any |all |a )*"
    r"(slips?|slip[- ]?ups?|stumbles?|mistakes?|flubs?|misspoken"
    r"|half[- ]?(questions?|sentences?|lines?|words?)|unfinished (questions?|sentences?)"
    r"|(bit|part|line|sentence|question|phrase) about)\b"
    r"|\btekrar(dan)? (baştan|söyle)|\bnerede kalmistim|\bdediğim (yer|kisim)|\bbaştan (al|başla)"
    r"|\b(dil )?sürçme\w* (\w+ )?(kes|çikar|sil)"
    r"|\byarim (kalan |kalmiş )?(soru|cümle|kelime)\w* (\w+ )?(kes|çikar|sil)"
    r"|\bhata(yi|lari) (\w+ )?(kes|çikar|sil)"
)
# The opposite ask: the pauses are wanted. Never read as cleanup.
_KEEP_PAUSES_RE = re.compile(
    r"\b(don'?t|do not|never|without) (remove|cut|cutting|trim|touch|take out|delete|skip"
    r"|tighten|clean)\b.{0,40}\b(pauses?|silences?|ums?|breaths?|gaps?)\b"
    r"|\b(keep|leave|preserve|maintain) (my |the |all |all the |some |natural |those |a )*"
    r"(pauses?|silences?|ums?|breaths?|rhythm|dramatic pause)\b"
    r"|\bduraklamalar[iı]? kalsin|\bdokunma"
)
_CAPTION_RE = re.compile(r"\b(captions?|subtitles?|karaoke)\b|\baltyazi")
_WORD_CAPTION_RE = re.compile(
    r"\bkaraoke\b|\bword[- ]by[- ]word\b|\b(one )?word at a time\b"
    r"|\bhighlight(ed|ing|s)? (the |each |every )?(key |spoken |current |said )?words?\b"
    r"|\bwords? (highlighted|light(s|ing)? up|lit up)\b|\bkelime kelime\b|\bvurgula"
)
_NO_CAPTION_RE = re.compile(
    r"\b(no|without|remove|drop|turn off|hide|skip) (the )?(captions?|subtitles?)\b"
    r"|\b(captions?|subtitles?) off\b|\baltyazisiz\b|\baltyazi (olmasin|istemiyorum|yok)"
)
# A captions ask about look, place or language is not judged by the style alone.
_CAPTION_DETAIL_RE = re.compile(
    r"\b(colou?rs?|yellow|white|red|blue|green|black|pink|orange|purple|lime|font|bold|italic"
    r"|sizes?|bigger|big|small|smaller|larger|huge|tiny|top|bottom|middle|cent(er|re)|left|right"
    r"|higher|lower|outline|shadow|stroke|uppercase|lowercase|caps|language|translat\w*"
    r"|turkish|english|german|spanish|french|italian|arabic|dutch|portuguese)\b"
    r"|\brenk|\bsari\b|\bbeyaz\b|\bbüyük|\bküçük|\büst|\balt(ta|a)\b|\btürkçe|\bingilizce"
    r"|\byazi tipi|\bkalin"
)
_TRIGGER_FACT_KEYS = (
    "triggers",
    "trigger",
    "trigger_words",
    "words",
    "cue_words",
    "cues",
    "at_words",
    "when_i_say",
    "spoken_words",
    "keywords",
)
_BEAT_KINDS = frozenset({"style", "audio", "select"})
# The brief files "show the medal photo when the voiceover says the medal" as `timing`
# as often as `style`/`audio`/`select` (KRI-537); only the pop-in check reads that kind.
_BEAT_ASK_KINDS = _BEAT_KINDS | {"timing"}
_CLEANUP_KINDS = frozenset({"timing", "style", "audio", "select"})
# Edits spined by the creator's own speech: captions come from it, and speech
# cleanup (pauses, filler sounds) is how it gets tightened.
_SPEECH_FORMATS = frozenset({"subtitled", "talking_head", *NARRATED_EDIT_FORMATS})
_QUOTES = '"\u201c\u201d\u00ab\u00bb'
_QUOTED_RE = re.compile(rf"[{_QUOTES}]([^{_QUOTES}]{{1,80}})[{_QUOTES}]")
_MAX_NAMED_IN_REASON = 6


def _req_text(req: BriefRequirement) -> str:
    return _fold(" ".join(x for x in (req.description, req.literal) if x))


# KRI-667: what an ask means, as the model that read the creator's message recorded it in
# `facts` (English keys, language-neutral values; prompt: main_creator._BRIEF_PROMPT_SECTION).
# A recorded value decides; the word recognisers above only run when it is absent or
# malformed, so a Turkish ask no longer depends on a Turkish twin of every pattern.
_CAPTION_FACTS = frozenset({"on", "off", "word_by_word"})


def _fact_flag(req: BriefRequirement, key: str) -> bool | None:
    """``facts[key]`` as yes/no, or None when the model recorded nothing usable."""
    value = req.facts.get(key)
    if isinstance(value, bool | int):
        return bool(value)
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in {"true", "yes"}:
            return True
        if folded in {"false", "no"}:
            return False
    return None


def _caption_fact(req: BriefRequirement) -> str | None:
    """``facts["captions"]``: "on", "off" or "word_by_word", else None."""
    value = req.facts.get("captions")
    if isinstance(value, bool):
        return "on" if value else "off"
    if not isinstance(value, str):
        return None
    folded = "_".join(value.strip().casefold().replace("-", " ").split())
    return folded if folded in _CAPTION_FACTS else None


def _caption_language_fact(req: BriefRequirement) -> str | None:
    """``facts["caption_language"]`` as a language code ("tr-TR", "Turkish" -> "tr")."""
    value = req.facts.get("caption_language")
    if not isinstance(value, str) or not value.strip():
        return None
    code = value.strip().casefold().replace("_", "-").split("-")[0]
    if code in LANGUAGE_NAMES:
        return code
    return LANGUAGE_WORDS.get(loose_text(value).strip())


def _wants_whole_take(req: BriefRequirement) -> bool:
    # Source-preservation is a global (or explicitly clip-scoped) timing ask.
    # Title-scoped timing requirements describe a text element's hold window;
    # treating their words (for example, "whole video") as source language
    # incorrectly routes them through the whole-take checker.
    if req.kind != "timing" or req.scope == "title" or _has_duration_target(req):
        return False
    stated = _fact_flag(req, "keep_whole_take")
    if stated is not None:
        return stated
    return bool(_WHOLE_TAKE_RE.search(_req_text(req)))


_WHOLE_TEXT_RE = re.compile(
    r"\b(texts?|titles?|labels?|captions?)\b.{0,40}\b(whole|full|entire)\s+"
    r"(video|edit|take)\b|\b(whole|full|entire)\s+(video|edit|take)\b.{0,40}"
    r"\b(texts?|titles?|labels?|captions?)\b"
)


def _wants_whole_text_span(req: BriefRequirement) -> bool:
    """Whether a title timing ask requests persistence for the full output."""
    if req.kind != "timing" or req.scope != "title" or _has_duration_target(req):
        return False
    return bool(
        req.facts.get("persistent")
        or req.facts.get("keep_visible")
        or _WHOLE_TEXT_RE.search(_req_text(req))
    )


def _fact_triggers(req: BriefRequirement) -> list[str]:
    found: list[str] = []

    def take(value: Any) -> None:
        if isinstance(value, str):
            found.extend(part for part in re.split(r"\s*[,;/]\s*", value) if part.strip())
        elif isinstance(value, Mapping):
            words = [value[k] for k in ("trigger", "word", "phrase") if k in value]
            if words:
                take(words)  # [{"trigger": "pasta", "visual": ...}, ...]
            else:
                # {"pasta": "photo", ...}: the keys are the spoken words.
                found.extend(str(key) for key in value if isinstance(key, str))
        elif isinstance(value, list):
            for item in value:
                take(item)

    for key in _TRIGGER_FACT_KEYS:
        if key in req.facts:
            take(req.facts[key])
    return found


def _cue_trigger_match(
    req: BriefRequirement, fields: Sequence[str | None] | None = None
) -> tuple[list[str], str | None, bool]:
    """The words right after a cue verb, someone else the cue says speaks them, and whether
    any cue was found.

    "... the first time she says Merope, placed ..." -> (["Merope"], "she", True). The
    speaker is read even when the words name nothing to look for.
    """
    speaker: str | None = None
    matched = False
    for field_value in (req.description, req.literal) if fields is None else fields:
        match = _CUE_TRIGGER_RE.search(field_value or "")
        if match is None:
            continue
        matched = True
        other = (match.group("other") or "").lower()
        speaker = speaker or (other if other in _NAMED_OTHER_SPEAKERS else None)
        words = _CUE_TRIGGER_END_RE.split(match.group("trigger"), maxsplit=1)[0]
        words = " ".join(words.split()).strip(" \"'\u201c\u201d")
        tokens = [t for t in re.split(r"\W+", _fold(words)) if t]
        # A described kind of word ("any food name") names nothing to look for.
        if (
            2 < len(words) <= 80
            and len(tokens) <= 8
            and not all(t in _TRIGGER_STOPWORDS for t in tokens)
            and not _GENERIC_TRIGGER_RE.search(words)
        ):
            return [words], speaker, matched
    return [], speaker, matched


def _cue_triggers(req: BriefRequirement) -> list[str]:
    """The words right after a cue verb ("when voiceover says the medal, show ...").

    A guess at what the creator named without quoting it, in their own spelling. It only
    ever CONFIRMS (picks which pop-ins an ask is about, recognises an unheard word); a
    guess that matches nothing never becomes a "No pop-in for X" verdict.
    """
    return _cue_trigger_match(req)[0]


# Someone else in the take a cue names as the speaker ("the first time she says ..."). "I",
# "the voiceover" or "anyone" filter nothing the take's words could not already tell.
_NAMED_OTHER_SPEAKERS = frozenset({"she", "he", "they"})
# Punctuation around one spoken word ("Merope," / "“Merope”").
_WORD_EDGE = "\"'.,;:!?()[]{}\u201c\u201d\u2018\u2019\u00ab\u00bb"


def _text_head(words: str, literal: str | None) -> str:
    """The guessed words cut to the shown text when they start with it: "Show MEROPE ... she
    says Merope in big letters" waits for "Merope", not "Merope in big letters"."""
    wanted = _fold(literal or "").split()
    tokens = words.split()
    if not wanted or len(tokens) <= len(wanted):
        return words
    head = [token.strip(_WORD_EDGE) for token in tokens[: len(wanted)]]
    return " ".join(head) if _fold(" ".join(head)).split() == wanted else words


def worded_spoken_cue(req: BriefRequirement) -> tuple[tuple[str, ...], str | None]:
    """KRI-791: the spoken words a text ask waits for, and who says them, as its own wording
    names them, for when the model left `facts.triggers` / `facts.speaker` out.

    Only an ask worded as a spoken cue counts ("... the first time she says Merope",
    "“Atakan” dediğimde ..."): the words after the cue verb (cut to the shown text when they
    start with it), else the quoted words that are not the text itself. A described word
    ("the café's name") or a plain text ask ("Show SALE in big letters") names no word, so
    nothing is guessed. A guess the take never says places nothing, and the finished
    video's receipt names the word it didn't hear.
    """
    if req.scope == "title":
        return (), None
    # The ask's own words only: the shown text is copy, never a cue ("WHEN SHE SAYS HI").
    triggers, speaker, matched = _cue_trigger_match(req, (req.description,))
    if not (matched or _worded_spoken_cue(req)):
        return (), None
    triggers = [_text_head(words, req.literal) for words in triggers]
    if not triggers:
        literal = (req.literal or "").strip()
        triggers = [
            quoted
            for quoted in (" ".join(q.split()) for q in _quoted_triggers(req))
            if quoted and quoted != literal
        ]
    return tuple(_dedupe_names(triggers)), speaker


def _dedupe_names(named: Iterable[str]) -> list[str]:
    out: dict[str, str] = {}
    for name in named:
        cleaned = " ".join(str(name).split())
        if cleaned and len(cleaned) <= 80:
            out.setdefault(_fold(cleaned), cleaned)
    return list(out.values())


def _quoted_triggers(req: BriefRequirement) -> list[str]:
    found: list[str] = []
    for field_value in (req.description, req.literal):
        found.extend(_QUOTED_RE.findall(field_value or ""))
    return found


def _named_triggers(req: BriefRequirement) -> list[str]:
    """The spoken words the creator named for certain: from `facts`, else quoted."""
    return _dedupe_names(_fact_triggers(req) or _quoted_triggers(req))


# Without a named word, a cue like "when I say ..." is a pop-in ask only when something is
# shown or played on it: a photo, sticker, sound, video card, the pop-in itself, or a
# show/put/play verb. "When I say go, cut to the next clip" is a cut (its own timing,
# unverified here), not a pop-in.
_TIMING_POP_IN_RE = re.compile(
    rf"{_VISUAL_RE.pattern}|{_SOUND_RE.pattern}"
    r"|\b(?:videos?|clips?|footage|cards?|overlays?|pop[- ]?(?:ups?|ins?)|on[- ]screen)\b"
    r"|\b(?:show|shows|put|puts|pop|pops|display|displays|flash|flashes|overlay|overlays"
    r"|add|adds|play|plays|bring|brings|insert|inserts|place|places)\b"
    r"|\bg[öo]rsel|\bvideo|\bkli[pb]|\bg[öo]ster|\b[çc][ıi]k(?:s[ıi]n|ar|)\b|\bekle|\bkoy"
    # KRI-540 Turkish sound beats: "zil çal" (ring a bell), "ses çalsın" (play a sound).
    r"|\bzil\b|\b[çc]al(?:s[ıi]n)?\b|\bduyulsun\b"
)
# Cut and edit verbs: a sentence that has one is an edit instruction, not a pop-in.
_TIMING_CUT_RE = re.compile(
    r"\b(?:cut|jump|switch|transition|move|go|skip|end|stop|start|begin|trim|speed|slow|fade"
    r"|pause|freeze|zoom|rewind|remove|delete)(?:s|es|ing)?\b"
    r"|\b(?:ge[çc]|atla|kes|dur|bitir|ba[şs]la|yava[şs]la|h[ıi]zlan)(?:sin|sun|s[üu]n)?\b"
)
# An explicit "show a picture / pop up / sticker" marker: the ask is about visuals even if
# it also has a cut verb ("pop up a sticker when I say pause, cut the rest").
_EXPLICIT_POP_RE = re.compile(
    rf"\bpop(?:s|ping)?[- ]?(?:up|in|ups|ins)\b|{_VISUAL_RE.pattern}"
    r"|\bfotograf|\bgorsel|\b[çc]ikartma"
)


def _cleanup_wording(req: BriefRequirement) -> bool:
    """Removal verb + something speech cleanup removes ("cut the pauses", "remove the ums")."""
    stated = _fact_flag(req, "speech_cleanup")
    if stated is not None:
        return stated
    text = _req_text(req)
    if _KEEP_PAUSES_RE.search(text) or not _CLEANUP_VERB_RE.search(text):
        return False
    return bool(_CLEANUP_NOUN_RE.search(text) or _NAMED_CUT_RE.search(text))


def _round_s(value: float) -> str:
    """Seconds for creator copy: one decimal at most (37.9765 -> "38")."""
    return f"{round(float(value), 1):g}"


# KRI-760: a named spoken line the creator wants heard whole ("\"Hi, I'm Karolina\" dediği
# cümle baştan sona duyulsun", "let the line \"...\" be heard in full") is a named-line-window
# ask (KRI-681), never a pop-in: nothing is shown or played on it. The model that read the
# message records it as `keep_whole_take` (and, as often, files the line under the pop-in key
# `triggers`), so that recorded flag decides (KRI-667); no wording is re-read here.


def _line_names(req: BriefRequirement) -> list[str]:
    """The lines a whole-line ask names, each in one piece: a `triggers` item is one phrase
    even with commas in it ("Hi, I'm Karolina, I'm from Ecuador"), else the quoted words."""
    named: list[str] = []
    for key in _TRIGGER_FACT_KEYS:
        value = req.facts.get(key)
        named.extend(
            item
            for item in (value if isinstance(value, list) else [value])
            if isinstance(item, str)
        )
    return _dedupe_names(named or _quoted_triggers(req))


def _keeps_named_line_sound(req: BriefRequirement) -> bool:
    """KRI-803: "keep his name exchange audible ("What's your name?" "Lucas.")" is filed as the
    typed source-sound intent with the spoken lines under `triggers` (no `keep_whole_take`).
    Only lines the model recorded as spoken count: a quoted clip name is not a line."""
    return (
        req.kind == "audio"
        and req.facts.get("audio_intent") == "source_unchanged"
        and bool(_fact_triggers(req))
    )


def _wants_whole_line(req: BriefRequirement) -> bool:
    """A named spoken line recorded as `keep_whole_take`, or as its clip's own sound kept
    (KRI-803), with nothing shown or played on it.

    A photo, sticker or pop-in word, or a word-triggered cue ("when I say ...", "dediğimde"),
    keeps the ask a pop-in even when the model also flagged the take as kept whole.
    """
    if req.kind not in _BEAT_ASK_KINDS or req.scope == "title" or _has_duration_target(req):
        return False
    whole = _fact_flag(req, "keep_whole_take")
    if whole is False or not ((whole is True and _line_names(req)) or _keeps_named_line_sound(req)):
        return False
    text = _req_text(req)
    return not (_EXPLICIT_POP_RE.search(text) or _SPOKEN_CUE_RE.search(text))


def wants_whole_line(req: BriefRequirement) -> bool:
    return _wants_whole_line(req)


def whole_line_quotes(req: BriefRequirement) -> list[str]:
    """The lines a whole-line ask names, in the creator's words; [] for any other ask."""
    return _line_names(req) if _wants_whole_line(req) else []


def _wants_beats(req: BriefRequirement) -> bool:
    if req.kind not in _BEAT_ASK_KINDS:
        return False
    if _wants_whole_line(req):
        return False
    if req.kind == "timing" and (
        # Timing asks about length or a text's hold window keep their own checkers.
        req.scope == "title"
        or _has_duration_target(req)
        or _wants_whole_take(req)
        or _wants_whole_text_span(req)
    ):
        return False
    text = _req_text(req)
    if _fact_triggers(req):
        return True
    spoken = _SPOKEN_CUE_RE.search(text)
    if _quoted_triggers(req):
        # A named word with a cue is a pop-in ask (a timing ask needs the spoken cue).
        return bool(spoken if req.kind == "timing" else _BEAT_CUE_RE.search(text))
    if not spoken:
        # The original rule for a style/audio/select ask: an explicit pop-in or sticker word
        # is enough ("add stickers for each food", "pop up the pasta sticker"). A timing
        # ask needs the spoken cue, so "make the text pop up quickly" stays a timing ask.
        return req.kind != "timing" and bool(_BEAT_CUE_RE.search(text))
    # No named word: a spoken cue alone also starts cuts, speed changes and cleanup
    # ("every time I say um, cut it out"). Only a visual/sound pop-in object counts.
    if _EXPLICIT_POP_RE.search(text):
        return True
    return bool(
        _TIMING_POP_IN_RE.search(text)
        and not _TIMING_CUT_RE.search(text)
        and not _cleanup_wording(req)
    )


def _wants_closing(req: BriefRequirement) -> bool:
    if req.kind not in _BEAT_KINDS:
        return False
    closing_fact = req.facts.get("closing") or req.facts.get("closing_media")
    return bool(closing_fact or _CLOSING_RE.search(_req_text(req)))


def _ending_names_picture(req: BriefRequirement) -> bool:
    """The ending is said to be a photo, sticker or badge: in the model's `closing` fact, or
    in a sentence of the ask that says how the edit ENDS (per sentence, so a sticker named
    elsewhere in a glued requirement does not count)."""
    if req.facts.get("closing_media"):
        return True  # a creator-owned picture by definition
    said = req.facts.get("closing")
    text = _req_text(req)
    for mark in ("!", "?", ";", "\n"):
        text = text.replace(mark, ". ")
    sentences = [s for s in text.split(". ") if _CLOSING_RE.search(s)]
    if isinstance(said, str):
        sentences.append(_fold(said))
    return any(_VISUAL_RE.search(s) for s in sentences)


def _closing_photo_ask(req: BriefRequirement, facts: PlanFacts) -> bool:
    """A closing photo, sticker or badge ask, as opposed to "end on the arrival clip" (KRI-688).

    The words "End on ..." start the closing-shot check (`closing_media`, a creator-owned
    picture placed last), but they are just as often an ordering ask about a CLIP, which the
    `order` checker owns. The check therefore stays on only where the model recorded a
    `closing` fact, the sentence names a picture, or a closing picture can exist at all:
    the edit's capabilities say so (only a Talking edit places one; ``reaction_beats_available``
    is False on every other format) and the model did not record the ending as a `last_clip`.
    """
    if not _wants_closing(req):
        return False
    if req.facts.get("closing") or _ending_names_picture(req):
        return True
    return facts.reaction_beats_available is not False and not req.facts.get("last_clip")


# KRI-841: "At the very end, add the cheers video with its own sound". The Main Creator's
# `ending_clip` strategy field carries it; the brief extractor records it as the requirement's
# `ending_clip` fact (the creator's words for the video) and `keep_audio` (false only when the
# video should be silent). No wording is read here (KRI-667): the fact routes the ask, and the
# older shape of the same ask (`order` + `last_clip`) is claimed only where the PLAN has an
# ending video to judge.
_TALKING_FORMATS = frozenset({"subtitled", "talking_head"})


def _ending_clip_fact(req: BriefRequirement) -> bool:
    return bool(req.facts.get("ending_clip"))


def _asks_ending_clip(req: BriefRequirement, facts: PlanFacts) -> bool:
    if facts.edit_format not in _TALKING_FORMATS:
        return False  # a montage "end on clip X" is an ordering ask: `_check_order` owns it
    if _ending_clip_fact(req):
        return True
    # The old shape of the ask (an `order` requirement whose `last_clip` names the video) is
    # judged against the ending video only when the plan (or the render) carries one. Without
    # it the ask may be about the talk itself ("end on me saying thank you"), so it keeps its
    # old verdict. A `closing` ask (a photo held over the last seconds) is never claimed here:
    # `_closing_photo_ask` owns it, and the extractor records video endings as `ending_clip`.
    return bool(
        facts.ending_clip_known
        and facts.ending_clip is not None
        and req.kind == "order"
        and req.facts.get("last_clip")
        and not (req.facts.get("first_clip") or req.facts.get("key") or req.facts.get("by"))
    )


# Why the video could not be put at the end, in the creator's words. The keys are the server
# repair's codes (`creator_capabilities.ENDING_CLIP_NOTICES`) and the phone worker's receipt.
def _ending_clip_why(code: str | None) -> str:
    if code == "not_talking":
        return say(
            en="Only a talking-to-camera edit on your iPhone can end on another video",
            tr="Başka bir videoyla yalnızca iPhone'daki konuşmalı düzenleme bitebilir",
        )
    if code == "not_found":
        return say(
            en="I couldn't find that video among your clips and Visuals",
            tr="O videoyu kliplerin ve Visuals arasında bulamadım",
        )
    if code == "not_video":
        return say(
            en="That is a photo, not a video, so it has no sound of its own",
            tr="Bu bir fotoğraf, video değil; kendi sesi yok",
        )
    if code in ("visual_videos_off", "visual_videos_not_verified"):
        return say(
            en="This iPhone can't play videos from Visuals yet",
            tr="Bu iPhone Visuals'taki videoları henüz oynatamıyor",
        )
    if code == "only_clip":
        return say(
            en="It is the only clip in this edit",
            tr="Bu düzenlemedeki tek klip bu",
        )
    if code in ("multi_clip_off", "talking_multi_off"):
        return say(
            en=(
                "Ending on another of your clips needs a Talking edit with several clips, "
                "which isn't available yet"
            ),
            tr=(
                "Başka bir klibinle bitirmek için birden çok klipli konuşmalı düzenleme "
                "gerekiyor, o da henüz yok"
            ),
        )
    if code == "media_lanes_disabled":
        return say(
            en="This iPhone render can't place pictures and videos around your talk yet",
            tr="Bu iPhone oluşturması konuşmanın çevresine resim ve video koyamıyor henüz",
        )
    if code == "audio_mix_not_verified":
        return say(
            en="This iPhone can't mix its sound into the video yet",
            tr="Bu iPhone sesini videoya karıştıramıyor henüz",
        )
    if code == "muted_footage_ending_unsupported":
        return say(
            en="One of your own clips can only end the video with its own sound, not silent",
            tr="Kendi kliplerinden biri videoyu ancak kendi sesiyle bitirebilir, sessiz değil",
        )
    if code == "not_selected":
        return say(
            en="You asked to leave that clip out of the edit",
            tr="O klibi düzenlemeden çıkarmamı istemiştin",
        )
    if code == "over_length_limit":
        return say(
            en="Adding it would make the video longer than the length you asked for",
            tr="Eklersem video istediğin süreden uzun olur",
        )
    if code == "not_a_video":
        return say(
            en="That isn't a video, so it has no sound of its own",
            tr="Bu bir video değil, kendi sesi yok",
        )
    if code == "too_long":
        return say(
            en="That clip is too long to add at the end",
            tr="Bu klip sona eklemek için çok uzun",
        )
    if code == "is_speaker":
        return say(
            en="That is the clip you speak in, so it can't also be added at the end",
            tr="Konuştuğun klip bu, o yüzden sona ayrıca eklenemez",
        )
    if code == "lane_request_ending_wins":
        return say(
            en="Another request for the end of the video took its place",
            tr="Videonun sonu için başka bir istek onun yerini aldı",
        )
    if code == "bind_failed":
        return say(
            en="I couldn't prepare that video on your iPhone",
            tr="O videoyu iPhone'unda hazırlayamadım",
        )
    return say(en="I couldn't add it at the end", tr="Sona ekleyemedim")


# The volume under which a video that is "at the end" is still muted (the recipe holds 0.0).
_MUTED_VOLUME = 0.01


def _check_ending_clip(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Does the edit end on the video the creator named, with (or without) its own sound?

    A draft judges the repaired strategy, the finished video its own `phone_ending_clip`
    receipt; "Done" is never read off the strategy once a render exists.
    """
    if not facts.ending_clip_known:
        return _receipt(req, "partial", _NO_CHECKER)
    flag = _fact_flag(req, "keep_audio")
    wants_sound = True if flag is None else flag
    ending = facts.ending_clip
    rendered = facts.rendered_variant
    if ending is None:
        return _receipt(
            req,
            "not_possible",
            say(
                en=(
                    "The finished video doesn't end on that video"
                    if rendered
                    else "This draft doesn't end on that video"
                ),
                tr=(
                    "Biten video o videoyla bitmiyor"
                    if rendered
                    else "Bu taslak o videoyla bitmiyor"
                ),
            )
            + ".",
        )
    if ending.status == "dropped":
        return _receipt(req, "not_possible", _ending_clip_why(ending.reason) + ".")
    if ending.status == "planned":
        if ending.keep_audio == wants_sound:
            return _receipt(req, "met", None)
        return _receipt(
            req,
            "not_possible",
            say(
                en=(
                    "This draft ends on that video but muted, not with its own sound"
                    if wants_sound
                    else "This draft ends on that video with its own sound, not muted"
                ),
                tr=(
                    "Bu taslak o videoyla bitiyor ama sessiz, kendi sesiyle değil"
                    if wants_sound
                    else "Bu taslak o videoyla kendi sesiyle bitiyor, sessiz değil"
                ),
            )
            + ".",
        )
    # applied: the compiled recipe ends on the video; judge the volume it holds.
    if wants_sound and ending.has_audio is False:
        # The recipe plays the clip at full volume, but its file has no audio track.
        return _receipt(
            req,
            "not_possible",
            say(
                en="The finished video ends on that video, but that video has no sound of its own",
                tr="Biten video o videoyla bitiyor ama o videonun kendi sesi yok",
            )
            + ".",
        )
    audible = ending.volume is not None and ending.volume > _MUTED_VOLUME
    if wants_sound and not audible:
        return _receipt(
            req,
            "not_possible",
            say(
                en="The finished video ends on that video but plays muted, without its own sound",
                tr="Biten video o videoyla bitiyor ama sessiz çalıyor, kendi sesi yok",
            )
            + ".",
        )
    if not wants_sound and audible:
        return _receipt(
            req,
            "not_possible",
            say(
                en="The finished video ends on that video with its own sound, not muted",
                tr="Biten video o videoyla kendi sesiyle bitiyor, sessiz değil",
            )
            + ".",
        )
    return _receipt(req, "met", None)


def _wants_cleanup(req: BriefRequirement) -> bool:
    """A speech-cleanup ask (pauses, retakes, filler) in the creator's own words."""
    if req.kind not in _CLEANUP_KINDS or _wants_whole_take(req) or _wants_whole_line(req):
        return False
    if _wants_beats(req) or _wants_closing(req) or _wants_speech(req):
        return False
    return _cleanup_wording(req)


def _cut_lines(req: BriefRequirement) -> list[str]:
    """KRI-792: ``facts["cut_lines"]``, the spoken words the creator named to cut out, in their
    own words and language; a filler sound on its own ("um") is cleanup, not a named line."""
    value = req.facts.get("cut_lines")
    items = value if isinstance(value, list) else [value]
    lines = (" ".join(item.split()) for item in items if isinstance(item, str))
    # Any length: a long described stretch is still a cut speech cleanup won't make.
    return [
        line
        for line in lines
        if line and not _CLEANUP_NOUN_RE.fullmatch(_fold(line).strip(" .,!?…"))
    ]


# KRI-821: the phone Talking worker cuts these lines (`opening_cut_ask.brief_named_cuts`).
named_cut_lines = _cut_lines


# Single quote marks the brief writer swaps in for the creator's double quotes (KRI-792:
# 'with bread, no,'). The curly closing one is also the apostrophe iOS types.
_SINGLE_QUOTES = "'‘’"


def _single_quotes_as_double(text: str) -> str:
    """``text`` with single quote marks read as double ones, so `_QUOTED_RE` finds
    'a line' as it finds "a line". An apostrophe inside a word (I'm, Karolina's, Atakan'ı,
    I’m) has a letter or digit on both sides and stays as it is."""
    return "".join(
        '"'
        if ch in _SINGLE_QUOTES
        and not (0 < i < len(text) - 1 and text[i - 1].isalnum() and text[i + 1].isalnum())
        else ch
        for i, ch in enumerate(text)
    )


def _wants_named_cuts(req: BriefRequirement) -> bool:
    """The ask names a stretch of speech to remove, not just pauses and sounds.

    KRI-792: the model records each such stretch in ``facts["cut_lines"]``; a recorded line
    decides. The fact only ever adds a named cut: without it the words still decide, since a
    missed named cut is a "Done" for a cut that never happens.
    """
    if _cut_lines(req):
        return True
    text = _req_text(req)
    if _NAMED_CUT_RE.search(text):
        return True
    # A quoted line is a spoken stretch; a quoted "um" is a filler sound.
    return any(
        not _CLEANUP_NOUN_RE.search(_fold(quoted))
        for quoted in _QUOTED_RE.findall(_single_quotes_as_double(req.description or ""))
    )


def _wants_captions(req: BriefRequirement) -> bool:
    """A captions ask ("add captions", "karaoke captions", "no captions")."""
    if req.kind in _BEAT_KINDS:
        if _wants_beats(req) or _wants_closing(req) or _wants_speech(req):
            return False
    elif not (req.kind == "text" and req.scope == "global" and not req.literal):
        return False
    if (
        _caption_fact(req) is not None
        or _caption_language_fact(req) is not None
        or _spellings_fact(req)  # KRI-793: the words to spell in the captions
    ):
        return True
    return bool(_CAPTION_RE.search(_req_text(req)))


def _wants_no_captions(req: BriefRequirement) -> bool:
    stated = _caption_fact(req)
    if stated is not None:
        return stated == "off"
    return bool(_NO_CAPTION_RE.search(_req_text(req)))


def _wants_word_captions(req: BriefRequirement) -> bool:
    if req.kind == "text_sequence":
        return False  # 29 Ekim: the creator's words shown in pieces are text, never captions
    stated = _caption_fact(req)
    if stated is not None:
        return stated == "word_by_word"
    return bool(_WORD_CAPTION_RE.search(_req_text(req)))


def text_asks_for_word_captions(text: str) -> bool:
    """The words ask for karaoke / word-by-word captions."""
    return bool(_WORD_CAPTION_RE.search(_fold(text or "")))


# "Keep the crowd noise quiet under my voiceover": how loud the footage's own sound plays
# beneath the creator's voice (KRI-537). Patterns run on `_fold` output.
_SOUND_NOUN = r"(?:noise|noises|sound|sounds|audio|chatter|ambience|volume)"
# Names the footage's own sound. Music is never footage sound here (a licensed track or a
# "background music" ask is judged elsewhere).
_FOOTAGE_SOUND_STRICT = (
    rf"(?:(?:crowd|ambient|street|room|wind|traffic|natural|original|raw|footage|clip|clips"
    rf"|video|camera) {_SOUND_NOUN}"
    r"|(?:sound|audio|noise)s? (?:of|from|in) the (?:footage|clips?|video|crowd)"
    r"|kalabalik\w*|orijinal ses\w*|ortam ses\w*|[çc]ekim\w* ses\w*"
    r"|klip\w* ses\w*|video\w* ses\w*|kamera\w* ses\w*)"
)
# "background noise" counts for a keep-it-quiet ask, not for a mute (that may mean
# noise reduction on the voice recording).
_FOOTAGE_SOUND_LOOSE = (
    rf"(?:{_FOOTAGE_SOUND_STRICT}|(?:background|bg) {_SOUND_NOUN}"
    r"|arka ?plan\w* (?:ses|g[üu]r[üu]lt[üu])\w*)"
)
_BED_NOISE_RE = re.compile(rf"\b{_FOOTAGE_SOUND_LOOSE}\b|\bg[üu]r[üu]lt[üu]")
_BED_NAMED_RE = re.compile(rf"\b{_FOOTAGE_SOUND_STRICT}\b")
# Quiet wording about the bed. Bare adjectives ("soft", "calm") only count with
# under/beneath, which are in the list themselves.
# The `pos` group marks position words ("under my voice"), which say where the bed plays,
# not how loud: "mute the crowd noise under my voice" is still a mute (KRI-821).
_BED_QUIET_RE = re.compile(
    r"\b(?:quiet(?:er)?|low(?:er)?|down|(?P<pos>under|beneath|below|underneath)"
    r"|duck(?:ed|ing)?|in the background|turned down)\b"
    r"|\bkisik|\bkis(?:il|ili|in)|\bd[üu][şs][üu]k|\b(?P<pos_tr>alt[iı]nda)|\barka planda|\bsessiz"
)
# The opposite direction ("raise the crowd noise", "not too quiet").
_BED_UP_RE = re.compile(
    r"\b(?:raise|louder|up|increase|boost|amplify|higher|too quiet|so quiet)\b"
    r"|\byukselt|\byükselt|\bart[iı]r|\bdaha y[üu]ksek"
)
_BED_MUTE_RE = re.compile(
    r"\b(?:mute|muted|silence|silenced|kill|strip|remove|delete|drop|get rid of|cut out|turn off"
    r"|disable|no|without)\b|\bkapat|\bkaldir|\bsil\b|\bolmasin|\bolmadan"
)


# KRI-821: the typed mute the model records for "mute the camera / crowd sound"
# (main_creator._BRIEF_PROMPT_SECTION). A recorded `audio_intent` decides; the wording
# recognisers below only read briefs written before it (KRI-667).
SOURCE_MUTED_INTENT = "source_muted"


def _typed_audio_intent(req: BriefRequirement) -> str | None:
    value = req.facts.get("audio_intent") if isinstance(req.facts, Mapping) else None
    return value if isinstance(value, str) else None


def _wants_bed_under_voice(req: BriefRequirement) -> bool:
    """Keep the footage's own sound (crowd, ambience, original audio) low under the voice."""
    if req.kind not in _BEAT_KINDS or _wants_beats(req) or _wants_cleanup(req):
        return False
    if _typed_audio_intent(req) is not None:
        return False
    text = _req_text(req)
    return bool(
        _BED_NOISE_RE.search(text) and _BED_QUIET_RE.search(text) and not _BED_UP_RE.search(text)
    )


def _asks_bed_level(text: str) -> bool:
    """Level wording about the bed ("lower", "quiet"), not just where it plays ("under")."""
    return any(
        m.group("pos") is None and m.group("pos_tr") is None for m in _BED_QUIET_RE.finditer(text)
    )


def _wants_bed_muted(req: BriefRequirement) -> bool:
    """Mute or remove the footage's own sound, with no keep / lower / raise wording.

    KRI-821: the typed ``source_muted`` is the brief writer's reading, never enough alone:
    the approved requirement text must itself name the footage sound and say mute/remove,
    and must not ask to keep it, lower it or raise it. Untyped (pre-v77) briefs are read by
    the same words for their receipt only; `footage_mute_ask` renders a mute from typed
    asks alone.
    """
    if req.kind not in _BEAT_KINDS or _wants_beats(req) or _wants_cleanup(req):
        return False
    intent = _typed_audio_intent(req)
    if intent is not None and (req.kind != "audio" or intent != SOURCE_MUTED_INTENT):
        return False
    text = _req_text(req)
    return bool(
        _BED_NAMED_RE.search(text)
        and _BED_MUTE_RE.search(text)
        and not _KEEP_VERB_RE.search(text)
        and not _BED_UP_RE.search(text)
        and not _asks_bed_level(text)
    )


def renders_bed_mute(req: BriefRequirement) -> bool:
    """KRI-821: this live requirement makes the render play the footage at 0 under the voice.

    Only a typed ``source_muted`` the approved text corroborates (`_wants_bed_muted`); a
    wording-only reading never decides the render.
    """
    return req.live and _typed_audio_intent(req) == SOURCE_MUTED_INTENT and _wants_bed_muted(req)


# KRI-821: public names for the readers that decide what the render does with the bed
# (`app.kria.footage_mute_ask`), so render and receipt share one recogniser.
wants_bed_muted = _wants_bed_muted
wants_bed_under_voice = _wants_bed_under_voice


def _has_duration_target(req: BriefRequirement) -> bool:
    target = req.facts.get("duration_s")
    return isinstance(target, (int, float)) and not isinstance(target, bool) and target > 0


def _trigger_heard(named: str, spoken: str) -> bool:
    a, b = _fold(named), _fold(spoken)
    return a == b or _contains_text(spoken, a) or _contains_text(named, b)


def _names(values: Iterable[str]) -> str:
    items = list(dict.fromkeys(values))
    shown = ", ".join(items[:_MAX_NAMED_IN_REASON])
    extra = len(items) - _MAX_NAMED_IN_REASON
    if extra <= 0:
        return shown
    return say(en=f"{shown} and {extra} more", tr=f"{shown} ve {extra} tane daha")


# Reasons that mean "the facts to judge this were not available": neutral in the
# reply (like a requirement with no checker), never a failure notice. The last
# two come from requirements with no checker; listing them keeps `is_judged`
# right even if `_has_checker` and `check_requirement` drift apart.
_CANT_CHECK_BEATS = "I can't check the pop-ins on this draft yet."
_CANT_CHECK_SPEECH = "I can't check the spoken parts on this draft yet."
_CANT_CHECK_TAKE = "I can't confirm this draft keeps your whole take."
_CANT_CHECK_TITLE = "I can't confirm where this draft's title came from."
_NO_TITLE = "I didn't add a title because no creator text or grounded brief facts were available."
NO_TITLE_REASON = _NO_TITLE  # stored on a blocked render's receipts (see `render_block_recovery`)

_CANT_CONFIRM_LENGTH = "I can't confirm this draft's length yet."
_TALKING_KEEPS_WHOLE_TAKE = "A Talking edit keeps your whole take, so its length follows your clip"
_VOICEOVER_SETS_LENGTH = "A voiceover edit runs as long as your voiceover"
_CANT_CHECK_TIMING = "I can't verify this timing automatically."
_CANT_CHECK_CLEANUP = "I can't check the speech cleanup on this draft yet."
_CANT_CHECK_CAPTIONS = "I can't check the captions on this draft yet."
_NO_CHECKER = "I can't verify this one automatically yet."
_CANT_CHECK_STYLE = "I can't check this text style automatically."
_CANT_CHECK_MIX = "I can't check the sound mix on this draft yet."
# KRI-689: the phone montage plays every clip at its natural speed. Said before approval
# (`clip_speed_notice`) and again, as the reason, on the finished video's receipt.
_NO_CLIP_SPEED = "I can't change clip speed on your iPhone yet, so the clips play at normal speed"
# KRI-689 (chat speed at creation): the cases no plan can retime, each a limit of the edit, and
# the two the creator must settle (which clip, how fast) -- those are not format limits.
_NO_CLIP_SPEED_SONG = (
    "I can't change clip speed on this edit because the clips follow your song, "
    "so they play at normal speed"
)
_NO_SPEED_ON_FREEZE = (
    "I can't change the speed of a clip that holds a freeze frame, so it plays at normal speed"
)
_NO_SPEED_RAMP = "I can't ramp speed inside a clip yet, so it plays at normal speed"
_NO_PHOTO_SPEED = "A photo has no speed to change, so it stays as it is"
_SPEED_DROPPED = "The finished video plays this clip at normal speed"
_SPEED_NOT_GROUNDED = (
    "I couldn't tell which clip you meant for the speed change, so the clips play at "
    "normal speed. Tell me which clip"
)
_SPEED_NO_FOOTAGE = (
    "I can't speed these clips up: the video already shows each one whole, so there's no "
    "footage left to speed through"
)
# KRI-983: a lip-sync ask the draft does not carry out.
_SONG_NOT_ATTACHED = "Add your song first and I'll use it."
_SONG_NOT_USED = "This draft doesn't use your song."
_SONG_IN_BACKGROUND = (
    "This draft plays your song in the background; your clips aren't matched to its words."
)
# Start of the reason on a "mute the footage sound" ask the voiceover mix cannot fully meet
# (the footage sound is a quiet bed under the voice, never off): a limit of the format.
_BED_STILL_PLAYS = "The footage sound still plays"
# Speech cleanup (KRI-467 follow-up): what the chosen format does with the ask.
_CLEANUP_PLANNED = "Speech cleanup cuts the long pauses"
_CLEANUP_AT_APPROVAL = "Choose Clean up speech when you approve and the long pauses are cut"
_CLEANUP_IF_OFFERED = (
    "If Clean up speech is offered when you approve, choose it to cut the long pauses"
)
_CLEANUP_UNAVAILABLE = "Speech cleanup isn't available for this project yet, so the pauses stay"
# KRI-846: said as a plain limit, never as editor work: no editor action cuts one exact spoken
# line out (a talking-source window can be trimmed or moved since KRI-852, but there is no
# split and no per-line cut, and a Narrated editor trims pictures, not the voice).
# KRI-551: a consented retake cleanup may have removed the line, and a generic cleanup record
# (even one with a retake) cannot prove it, so it says "may still be", never "stays". One
# static string for every speech format: `_loc` translates exact keys only and the
# review-note dedupe compares text.
_NAMED_CUTS_STAY = (
    "I can't cut out one exact line yet or confirm it's gone, so it may still be in your video"
)
# KRI-821: a named line the phone Talking render could not cut (its receipt says why).
_NAMED_CUT_STILL_IN = "Still in your video"
# KRI-821: a phone Talking draft cuts each named line the render finds said once.
_NAMED_CUT_AT_RENDER = (
    "I'll cut the line you named out of your take if I find it said once, and tell you after"
)
# Neutral: the speaker may be someone the creator filmed (KRI-1008).
_CAPTIONS_WORD_BY_WORD = "words light up as they're spoken"
# KRI-541: what speech cleanup did on a finished render.
_CLEANUP_DONE = "Speech cleanup cut the long pauses"
_CLEANUP_NOTHING_TO_CUT = "Speech cleanup found no long pauses to cut"
_CLEANUP_NOT_RUN = "Speech cleanup didn't run on this video, so the long pauses stay"
# KRI-700: no draft can brighten or darken the footage (see `wants_light_change`).
_LIGHT_UNAVAILABLE = (
    "I can't change how bright or dark clips look in this edit yet, so they keep their own light"
)
# KRI-777: no edit can hold a frame (see `kria.freeze_ask`). Said on the draft, before the
# approval card, and again on the finished video's receipt.
_NO_FREEZE_FRAME = "I can't freeze a frame in this edit yet, so that moment plays normally"
# KRI-1029: the creator asked a text curved and this edit draws it straight: the app build
# that approved (or the server) can't bend text, or the title's typewriter entrance can't
# curve. Said on the draft and on the finished video. ``_TEXT_CAME_OUT_STRAIGHT`` is a
# finished video whose saved text has no bend although the plan bent it.
_TEXT_STAYS_STRAIGHT = "I can't curve text in this edit on your iPhone yet, so it stays straight"
_TYPEWRITER_STAYS_STRAIGHT = "A typewriter title can't curve, so it stays straight"
_TEXT_CAME_OUT_STRAIGHT = "The text is straight in the finished video, not curved"
# KRI-1029: said beside "Done", since a rewritten ask may no longer say "curved" itself.
_TEXT_CURVED = "curved"
# KRI-994: the clip the creator asked to skip, or a photo in a "video clips only" edit, is
# in the finished video after all (a pin or the plan's own placement kept it).
_SKIPPED_CLIP_PLAYS = "The clip you asked to skip is still in the video"
_PHOTO_IN_CLIPS_ONLY = "The video still has a photo in it"
# KRI-828: the freeze is planned, but no analysed moment of the clips matches the creator's
# words for it (or two do), so no frame is guessed.
_FREEZE_NOT_GROUNDED = (
    "I couldn't tell which moment to freeze, so that moment plays normally. "
    "Tell me what happens in the moment to hold, like who is in it and what they do"
)
# KRI-900: the moment is known but nobody picked the instant in it, so nothing is held. The
# receipt goes on with the moment's seconds and asks for the second (`_check_freeze`).
_FREEZE_NEEDS_FRAME = "I found that moment"
# KRI-900: "freeze the first frame / at the start": a hold sits where a cut ends, so the
# opening frame is never held, and the end of the first cut is not what was asked.
_FREEZE_FIRST_FRAME = (
    "A freeze holds the frame where a shot ends, so I can't hold the opening frame, and it "
    'plays normally. Tell me the second of the clip to freeze on, like "freeze at 2 seconds"'
)

# KRI-842: one clip's own sound on a phone Voiceover edit (see `kria.narrated_clip_sound_ask`).
_CLIP_SOUND_UNDER_VOICE_MET = "That clip's own sound plays softly under your voice"
_CLIP_PLAYS_ON_MET = "That clip plays on with its own sound after your voice ends"
_ONLY_LAST_CLIP_PLAYS_ON = (
    "Only the last clip can play on after your voice, and that isn't the last clip in this edit"
)
_ONLY_ONE_CLIP_PLAYS_ON = "Only one clip can play on after your voice"
_CLIP_SOUND_NOT_BUILT = "I couldn't play that clip's own sound in this edit"
_CLIP_NOT_IN_EDIT = (
    "That clip isn't one of the video clips in this edit, so I couldn't play its own sound"
)
_CLIP_HAS_NO_SOUND = "That clip has no sound of its own to play"
_CLIP_SOUND_NO_ROOM = (
    "That clip has no footage left after the part under your voice, so nothing plays on after it"
)
_CLIP_LINE_TOO_LONG = "That line is too long to play on after your voice (6 seconds at most)"
_CLIP_PLAYS_ON_PAST_LENGTH = (
    "Playing that clip on after your voice would take the video past the length you asked for"
)
_CLIP_SOUND_EDITED = "You changed the cut in the editor, so that clip no longer plays like this"
_CLIP_LINE_TOO_EARLY = "That line is said too early in that clip to come after your voice"
_CLIP_LINE_PAST_END = "That line runs past the end of that clip, so I couldn't play it whole"
_CLIP_SOUND_REFUSED = (
    "Your iPhone couldn't build that clip's own sound, so the video plays without it"
)
# A clip's own sound under or after the voice is built only by the phone Voiceover compiler.
_CLIP_SOUND_NEEDS_PHONE_VOICEOVER = (
    "Playing one clip's own sound under or after your voice only works in a voiceover edit "
    "made on your iPhone, not in this one"
)
# A clip-scoped ask for the footage sound under the voice, without a recorded `clip_sound`:
# the voiceover mix is one bed for every clip, never one clip's.
_EVERY_CLIP_SOUND_UNDER_VOICE = (
    "Every clip's own sound plays softly under your voice, not just this one"
)
_NO_CLIP_SOUND_UNDER_VOICE = "No clip's own sound plays under your voice"

# KRI-705: what a phone Talking edit says, before approval and after render, about an ask
# outside what it can draw. Static strings: `is_judged` / `is_format_limit` read them by prefix.
_TALKING_NO_SUCH_EDIT = "A Talking video can't do this, so it stays as filmed"
_TALKING_NO_SPOKEN_TEXT = "A Talking video can't show text at a word you say yet"
_TALKING_OPENING_CUT_PLAN = (
    "I'll start the video at that line if I find it in your take; if not, it opens at the start"
)
# KRI-790: the same plan naming the creator's start and/or end line (and where each lands in
# the take when the draft's timed speech holds it). A static prefix; the lines follow it.
_TALKING_WINDOW_PLAN = "I'll keep your take "
# KRI-790: a Talking edit with quoted start / end lines is as long as the part they keep.
_TALKING_KEEPS_QUOTED_PART = (
    "A Talking edit keeps only the part of your take you asked for, so its length follows that part"
)
# KRI-707: the phone Talking worker shows the text from the word (`phone_spoken_text`).
_VOICEOVER_SPOKEN_TEXT_PLAN = (
    "I'll place that text on the matching words in your recording. If they repeat, I'll need "
    "the occurrence you mean."
)
_TALKING_SPOKEN_TEXT_PLAN = (
    "I'll show it the moment you say that word in your take; if I don't hear the word, it "
    "won't appear"
)
# KRI-793: what a phone Talking draft says about a captions ask naming a language or words
# to spell: the writer captions everything said in the take (an off-camera voice too), and
# the reason goes on to name the language and the words (`_caption_words_plan`).
_TALKING_CAPTIONS_EVERY_VOICE = "Every voice in your take gets captions, off-camera ones too"
# KRI-791: the take's words don't say who speaks, so "the first time SHE says it" is shown
# the first time anyone says the word -- said before approval and on the finished video.
_TALKING_SPOKEN_TEXT_ANY_VOICE = (
    "I'll show it the first time that word is said in your take; I can't tell who's "
    "speaking, so it may appear when someone else says it"
)
# KRI-724: the creator picked which time the word is said; a static prefix (word and time
# follow it, `_pinned_spoken_text_plan`).
_TALKING_SPOKEN_TEXT_PINNED = "I'll show it on the "
_SPOKEN_TEXT_FIRST_HEARD = (
    "I can't tell who's speaking, so it shows the first time anyone says that word"
)
# KRI-791: a text asked for at a spoken word whose word the ask doesn't name ("when she says
# the café's name"): the phone could show it, the server can't tell which word.
_TALKING_SPOKEN_TEXT_NO_WORD = (
    "I couldn't tell which spoken word to wait for, so tell me the exact word to show it on"
)
# KRI-724: a phone Talking edit cuts the Nth time a quoted phrase is said (`speech_remove`)
# and filters steady engine noise (`noise_reduction`), both read from brief facts.
_SPEECH_CUT_DRAFT_PLAN = (
    "Will locate the requested occurrence in the original speech before cutting; no guess"
)
_SPEECH_CUT_UNPROVEN = "Couldn't verify that exact spoken phrase occurrence was cut"
_NOISE_REDUCTION_PLANNED = (
    "Will reduce steady engine noise before rendering; engine and speech are not "
    "independently isolated"
)
_NOISE_REDUCTION_APPLIED = (
    "Applied noise reduction; engine and speech are not independently isolated"
)
_NOISE_REDUCTION_UNAVAILABLE = "Engine noise reduction isn't available for this edit"
_NOISE_REDUCTION_UNSUPPORTED = (
    "Engine noise reduction is only available for a single-source Talking edit"
)
_NOISE_REDUCTION_MISSED = "Couldn't apply engine noise reduction"
# KRI-849: what a phone Talking edit says about the word the creator asked to be asked about
# ("if you can't tell what he says, ask me"). Static prefixes: ``is_judged`` / ``is_format_limit``
# read them; the times and the creator's own words follow. Never the heard words.
_UNCLEAR_AGREE = "Both listens heard the same words, so I didn't find an unclear word"
_UNCLEAR_UNSURE = (
    "I couldn't double-check the speech on this clip, so I can't spot the unclear word"
)
_UNCLEAR_OMIT_PLAN = "I'll leave out the word I couldn't make out"
_UNCLEAR_OMIT_DONE = "I left out the word I couldn't make out"
_UNCLEAR_WRITE_PLAN = "I'll write the word you gave me"
_UNCLEAR_WRITE_DONE = "I wrote the word you gave me"
# The step that checks it failed on this render: never "met", never a claim about what it did.
_UNCLEAR_ERROR = "I couldn't check the unclear word in this video"
_UNCLEAR_PREFIXES: tuple[str, ...] = (
    _UNCLEAR_ERROR,
    _UNCLEAR_AGREE,
    _UNCLEAR_UNSURE,
    _UNCLEAR_OMIT_PLAN,
    _UNCLEAR_OMIT_DONE,
    _UNCLEAR_WRITE_PLAN,
    _UNCLEAR_WRITE_DONE,
)
_TALKING_HONEST_EN: tuple[str, ...] = (
    *_UNCLEAR_PREFIXES,
    _TALKING_NO_SUCH_EDIT,
    _TALKING_NO_SPOKEN_TEXT,
    _TALKING_OPENING_CUT_PLAN,
    _TALKING_WINDOW_PLAN,
    _TALKING_SPOKEN_TEXT_PLAN,
    _TALKING_CAPTIONS_EVERY_VOICE,
    _TALKING_SPOKEN_TEXT_ANY_VOICE,
    _TALKING_SPOKEN_TEXT_PINNED,
    _SPOKEN_TEXT_FIRST_HEARD,
    _TALKING_SPOKEN_TEXT_NO_WORD,
)

# The Turkish twin of every static reason above (KRI-520). Reasons are written into
# receipts when a turn runs, in that turn's language, so everything that tells one
# reason from another (neutral, format limit, wordless title) accepts both texts;
# `_loc` also turns a reason a worker stored in English into Turkish for a Turkish reply.
_REASON_TR: dict[str, str] = {
    _CANT_CHECK_EDITOR_CLIP_TEXT: "Editör düzenlemesinde klip başına yazıyı doğrulayamıyorum.",
    _CANT_CHECK_TARGET_CLIP_TEXT: "Bu taslakta o klibin yazısını doğrulayamıyorum.",
    _CANT_CONFIRM_ORDER: "Bu taslağın kullandığı sırayı doğrulayamıyorum.",
    _RANDOM_ORDER_NOT_APPLIED: "Kliplerin rastgele sırada olduğunu doğrulayamadım.",
    _RANDOM_ORDER_OVERRIDDEN: "Klipleri kendin taşıdığın için senin sıran korundu.",
    _CANT_CHECK_ORDER_RULE: "Bu sıralamayı otomatik olarak doğrulayamıyorum.",
    _ORDER_NOT_APPLIED: (
        "Anlattığını kliplerle eşleştiremedim, o yüzden klipler eklediğin sırada kaldı."
    ),
    _ORDER_LIST_NOT_PLACED: "Saydığın her şeyi verdiğin sıraya yerleştiremedim.",
    _MOMENTS_NOT_LOCATED: (
        "Saydığın her anı klipte bulamadım, o yüzden oynayanların o anlar olduğunu doğrulayamıyorum"
    ),
    _MOMENTS_CREATOR_CALL: (
        "Bazı anlar senin kararın: kliplerinde olmadığını söylediklerin ya da bana bıraktıkların"
    ),
    _ORDER_NOT_RECORDED: "Düzenlemenin kullandığı sırayı doğrulayamadım.",
    _ORDER_RULE_NOT_APPLIED: (
        "Bu sıralama kuralını doğrulayamıyorum ve klipler kurala uyduğunu gösterebileceğim "
        "bir sırada değil."
    ),
    _CANT_CHECK_BEATS: "Bu taslakta sözlerine göre çıkan görselleri henüz kontrol edemiyorum.",
    _CANT_CHECK_SPEECH: "Bu taslaktaki konuşma bölümlerini henüz kontrol edemiyorum.",
    _CANT_CHECK_TAKE: "Bu taslağın çekimini baştan sona koruduğunu doğrulayamıyorum.",
    _CANT_CHECK_TITLE: "Bu taslaktaki başlığın nereden geldiğini doğrulayamıyorum.",
    _NO_TITLE: (
        "Başlık eklemedim, çünkü kullanabileceğim bir yazın ya da isteğinde dayanabileceğim "
        "bir bilgi yoktu."
    ),
    _CANT_CONFIRM_LENGTH: "Bu taslağın uzunluğunu henüz doğrulayamıyorum.",
    _TALKING_KEEPS_WHOLE_TAKE: (
        "Konuşmalı düzenleme çekimini baştan sona korur, o yüzden uzunluğu klibini izler"
    ),
    _TALKING_KEEPS_QUOTED_PART: (
        "Konuşmalı düzenleme çekiminin yalnızca istediğin kısmını korur, o yüzden uzunluğu o "
        "kısmı izler"
    ),
    _VOICEOVER_SETS_LENGTH: "Seslendirmeli düzenleme seslendirmen kadar sürer",
    _CANT_CHECK_TIMING: "Bu süreyi otomatik olarak doğrulayamıyorum.",
    _CANT_CHECK_CLEANUP: "Bu taslakta konuşma temizliğini henüz kontrol edemiyorum.",
    _CANT_CHECK_CAPTIONS: "Bu taslaktaki altyazıları henüz kontrol edemiyorum.",
    _NO_CHECKER: "Bunu henüz otomatik olarak doğrulayamıyorum.",
    _CANT_CHECK_STYLE: "Bu yazı stilini otomatik olarak kontrol edemiyorum.",
    _CANT_CHECK_MIX: "Bu taslakta ses karışımını henüz kontrol edemiyorum.",
    _NO_CLIP_SPEED: (
        "iPhone'unda klip hızını henüz değiştiremiyorum, o yüzden klipler normal hızda oynuyor"
    ),
    _NO_CLIP_SPEED_SONG: (
        "Bu düzenlemede klip hızını değiştiremiyorum çünkü klipler şarkını takip ediyor, "
        "o yüzden normal hızda oynuyorlar"
    ),
    _SONG_NOT_ATTACHED: "Önce şarkını ekle, ben de onu kullanayım.",
    _SONG_NOT_USED: "Bu taslak şarkını kullanmıyor.",
    _SONG_IN_BACKGROUND: (
        "Bu taslak şarkını arka planda çalıyor; klipler sözlerine göre yerleştirilmedi."
    ),
    _NO_SPEED_ON_FREEZE: (
        "Donmuş kare tutan bir klibin hızını değiştiremiyorum, o yüzden normal hızda oynuyor"
    ),
    _NO_SPEED_RAMP: (
        "Bir klibin içinde hızı henüz kademeli değiştiremiyorum, o yüzden normal hızda oynuyor"
    ),
    _NO_PHOTO_SPEED: "Bir fotoğrafın değişecek hızı yok, o yüzden olduğu gibi kalıyor",
    _SPEED_DROPPED: "Bitmiş video bu klibi normal hızda oynatıyor",
    _SPEED_NOT_GROUNDED: (
        "Hız değişikliği için hangi klibi kastettiğini anlayamadım, o yüzden klipler normal "
        "hızda oynuyor. Hangi klip olduğunu yaz"
    ),
    _SPEED_NO_FOOTAGE: (
        "Bu klipleri hızlandıramıyorum: video her birini zaten baştan sona gösteriyor, "
        "hızlanacak görüntü kalmadı"
    ),
    _TEXT_STAYS_STRAIGHT: (
        "Bu düzenlemede iPhone'unda yazıyı henüz kavisli yapamıyorum, o yüzden düz kalıyor"
    ),
    _TYPEWRITER_STAYS_STRAIGHT: "Daktilo efektli başlık kavisli olamıyor, o yüzden düz kalıyor",
    _TEXT_CAME_OUT_STRAIGHT: "Bitmiş videoda yazı kavisli değil, düz",
    _TEXT_CURVED: "kavisli",
    _SKIPPED_CLIP_PLAYS: "Çıkarmanı istediğin klip hâlâ videoda",
    _PHOTO_IN_CLIPS_ONLY: "Videoda hâlâ bir fotoğraf var",
    _NO_FREEZE_FRAME: (
        "Bu düzenlemede henüz bir kareyi donduramıyorum, o yüzden o an normal akıyor"
    ),
    _FREEZE_NOT_GROUNDED: (
        "Hangi anı donduracağımı anlayamadım, o yüzden o an normal akıyor. "
        "Hangi klipte kaçıncı saniyeyi tutayım, yaz"
    ),
    _CLIP_SOUND_UNDER_VOICE_MET: "O klibin kendi sesi seslendirmenin altında kısık çalıyor",
    _CLIP_PLAYS_ON_MET: "O klip, seslendirme bitince kendi sesiyle devam ediyor",
    _ONLY_LAST_CLIP_PLAYS_ON: (
        "Seslendirmeden sonra yalnızca son klip devam edebilir ve bu düzenlemede o klip son değil"
    ),
    _ONLY_ONE_CLIP_PLAYS_ON: "Seslendirmeden sonra yalnızca tek bir klip devam edebilir",
    _CLIP_SOUND_NOT_BUILT: "O klibin kendi sesini bu düzenlemede çalamadım",
    _CLIP_NOT_IN_EDIT: (
        "O klip bu düzenlemedeki video kliplerinden biri değil, o yüzden kendi sesini çalamadım"
    ),
    _CLIP_HAS_NO_SOUND: "O klibin çalacak kendi sesi yok",
    _CLIP_SOUND_NO_ROOM: (
        "O klipte seslendirmenin altında kalan kısımdan sonra görüntü kalmadı, o yüzden "
        "sonrasında bir şey çalmıyor"
    ),
    _CLIP_LINE_TOO_LONG: ("Bu cümle seslendirmeden sonra çalmak için çok uzun (en fazla 6 saniye)"),
    _CLIP_PLAYS_ON_PAST_LENGTH: (
        "O klibi seslendirmeden sonra çalmak videoyu istediğin uzunluğun dışına çıkarırdı"
    ),
    _CLIP_LINE_TOO_EARLY: (
        "Bu cümle o klipte seslendirmeden sonra gelemeyecek kadar erken söyleniyor"
    ),
    _CLIP_LINE_PAST_END: "Bu cümle o klibin sonundan taşıyor, o yüzden tam çalamadım",
    _CLIP_SOUND_REFUSED: (
        "iPhone'un o klibin kendi sesini oluşturamadı, o yüzden video o ses olmadan çalıyor"
    ),
    _CLIP_SOUND_NEEDS_PHONE_VOICEOVER: (
        "Bir klibin kendi sesini seslendirmenin altında ya da sonrasında çalmak yalnızca "
        "iPhone'unda yapılan seslendirmeli düzenlemede mümkün, bu düzenlemede değil"
    ),
    _CLIP_SOUND_EDITED: "Editörde kesimi değiştirdin, o yüzden o klip artık böyle çalmıyor",
    _EVERY_CLIP_SOUND_UNDER_VOICE: (
        "Her klibin kendi sesi seslendirmenin altında kısık çalıyor, yalnızca bu klibinki değil"
    ),
    _NO_CLIP_SOUND_UNDER_VOICE: "Hiçbir klibin kendi sesi seslendirmenin altında çalmıyor",
    _FREEZE_NEEDS_FRAME: "O anı buldum",
    _FREEZE_FIRST_FRAME: (
        "Donma bir çekimin bittiği karede olur, o yüzden açılış karesini tutamıyorum ve normal "
        'oynuyor. Klibin hangi saniyesinde donsun, yaz; örneğin "2. saniyede dondur"'
    ),
    _BED_STILL_PLAYS: "Çekim sesi hâlâ çalıyor",
    _CLEANUP_PLANNED: "Konuşma temizliği uzun duraklamaları keser",
    _CLEANUP_AT_APPROVAL: (
        "Onaylarken \u201cKonuşmayı temizle\u201d seçeneğini seç, uzun duraklamalar kesilir"
    ),
    _CLEANUP_IF_OFFERED: (
        "Onaylarken \u201cKonuşmayı temizle\u201d çıkarsa, uzun duraklamaları kesmek için onu seç"
    ),
    _CLEANUP_UNAVAILABLE: (
        "Konuşma temizliği bu proje için henüz yok, o yüzden duraklamalar kalıyor"
    ),
    _NAMED_CUT_STILL_IN: "Videonda hâlâ var",
    _NAMED_CUT_AT_RENDER: (
        "Söylediğin cümleyi çekiminde bir kez geçiyorsa keseceğim ve sonra sana söyleyeceğim"
    ),
    _NAMED_CUTS_STAY: (
        "belirli bir cümleyi henüz tam olarak kesemiyorum ve çıkarıldığını doğrulayamıyorum, "
        "o yüzden videoda kalmış olabilir"
    ),
    _CAPTIONS_WORD_BY_WORD: "kelimeler söylendikçe yanıyor",
    _CLEANUP_DONE: "Konuşma temizliği uzun duraklamaları kesti",
    _CLEANUP_NOTHING_TO_CUT: "Konuşma temizliği kesilecek uzun bir duraklama bulmadı",
    _CLEANUP_NOT_RUN: ("Konuşma temizliği bu videoda çalışmadı, o yüzden uzun duraklamalar kaldı"),
    _LIGHT_UNAVAILABLE: (
        "Bu düzenlemede kliplerin ne kadar aydınlık ya da karanlık göründüğünü henüz "
        "değiştiremiyorum, o yüzden kendi ışıklarıyla kalıyorlar"
    ),
    _TALKING_NO_SUCH_EDIT: "Konuşmalı videoda bunu yapamıyorum, çekimde olduğu gibi kalır",
    _TALKING_NO_SPOKEN_TEXT: "Konuşmalı video söylediğin bir kelimede yazı gösteremiyor",
    _TALKING_OPENING_CUT_PLAN: (
        "Videoyu o cümlenin başladığı yerden başlatırım; çekiminde bulamazsam baştan açılır"
    ),
    _TALKING_WINDOW_PLAN: "Çekiminin ",
    _VOICEOVER_SPOKEN_TEXT_PLAN: (
        "Yazıyı kaydındaki eşleşen sözlerde göstereceğim. Sözler tekrarlanırsa hangi tekrarı "
        "istediğini bilmem gerekecek."
    ),
    _SPEECH_CUT_DRAFT_PLAN: (
        "Kesmeden önce istediğin tekrarı orijinal konuşmada bulacağım; tahmin etmeyeceğim"
    ),
    _SPEECH_CUT_UNPROVEN: "O konuşulan ifadenin tam o tekrarının kesildiğini doğrulayamadım",
    _NOISE_REDUCTION_PLANNED: (
        "Renderdan önce sabit motor gürültüsünü azaltacağım; motor sesiyle konuşmayı "
        "birbirinden bağımsız ayıramam"
    ),
    _NOISE_REDUCTION_APPLIED: (
        "Gürültü azaltma uygulandı; motor sesiyle konuşma birbirinden bağımsız ayrılmadı"
    ),
    _NOISE_REDUCTION_UNAVAILABLE: "Motor gürültüsü azaltma bu düzenlemede kullanılamıyor",
    _NOISE_REDUCTION_UNSUPPORTED: (
        "Motor gürültüsünü azaltma yalnızca tek klipli Konuşmalı düzenlemede yapılabiliyor"
    ),
    _NOISE_REDUCTION_MISSED: "Motor gürültüsünü azaltamadım",
    _TALKING_SPOKEN_TEXT_PLAN: (
        "Çekiminde o kelimeyi söylediğin anda yazıyı gösteririm; kelimeyi duymazsam yazı çıkmaz"
    ),
    _TALKING_CAPTIONS_EVERY_VOICE: (
        "Çekimindeki her ses altyazıya girer, kamera dışındaki sesler de"
    ),
    _UNCLEAR_AGREE: "İki dinleme de aynı kelimeleri duydu, o yüzden belirsiz bir kelime bulmadım",
    _UNCLEAR_UNSURE: (
        "Bu klipteki konuşmayı iki kez karşılaştıramadım, o yüzden belirsiz kelimeyi bulamıyorum"
    ),
    _UNCLEAR_OMIT_PLAN: "Anlayamadığım kelimeyi çıkarırım",
    _UNCLEAR_OMIT_DONE: "Anlayamadığım kelimeyi çıkardım",
    _UNCLEAR_WRITE_PLAN: "Verdiğin kelimeyi yazarım",
    _UNCLEAR_WRITE_DONE: "Verdiğin kelimeyi yazdım",
    _UNCLEAR_ERROR: "Videodaki belirsiz kelimeyi kontrol edemedim",
    _TALKING_SPOKEN_TEXT_ANY_VOICE: (
        "Yazıyı çekiminde o kelime ilk söylendiğinde gösteririm; kimin konuştuğunu ayırt "
        "edemiyorum, o yüzden başka biri söylediğinde çıkabilir"
    ),
    _TALKING_SPOKEN_TEXT_PINNED: "Yazıyı seçtiğin ",
    _SPOKEN_TEXT_FIRST_HEARD: (
        "Kimin konuştuğunu ayırt edemiyorum, o yüzden yazı o kelime ilk söylendiğinde çıkıyor"
    ),
    _TALKING_SPOKEN_TEXT_NO_WORD: (
        "Hangi kelimeyi beklemem gerektiğini anlayamadım; yazının çıkacağı kelimeyi tam olarak "
        "söyle"
    ),
    "Some requested text ends before the video does.": "Bazı yazılar video bitmeden kayboluyor.",
}


def _loc(reason: str) -> str:
    """A static English reason in the chat's language; any other text comes back as is."""
    return say(en=reason, tr=_REASON_TR.get(reason, reason))


# NO_TITLE_REASON is the English text; a receipt written in a Turkish turn carries the
# Turkish twin, so compare with `is_no_title_reason`.
_NO_TITLE_REASONS = frozenset({_NO_TITLE, _REASON_TR[_NO_TITLE]})


def is_no_title_reason(reason: object) -> bool:
    """True for the receipt reason of a title requirement that came with no words."""
    return isinstance(reason, str) and reason in _NO_TITLE_REASONS


_NEUTRAL_EN = frozenset(
    {
        _CANT_CHECK_BEATS,
        _CANT_CHECK_SPEECH,
        _CANT_CHECK_TAKE,
        _CANT_CHECK_TITLE,
        _CANT_CHECK_EDITOR_CLIP_TEXT,
        _CANT_CHECK_TARGET_CLIP_TEXT,
        _CANT_CONFIRM_ORDER,
        _CANT_CHECK_ORDER_RULE,
        _CANT_CONFIRM_LENGTH,
        _CANT_CHECK_TIMING,
        _CANT_CHECK_CLEANUP,
        _CANT_CHECK_CAPTIONS,
        _CANT_CHECK_MIX,
        _CANT_CHECK_STYLE,
        _NO_CHECKER,
    }
)
_NEUTRAL_REASONS = _NEUTRAL_EN | frozenset(_REASON_TR[reason] for reason in _NEUTRAL_EN)

# Reasons that describe what the format the creator chose does with an ask, not a
# simplification Kria made instead of it: a Talking edit's length follows the take,
# speech cleanup is chosen at approval and cuts pauses, never a named line, and a phone
# montage plays every clip at natural speed (KRI-689, an honest "Couldn't"). The
# receipt never turns a first draft into the "should I make a simpler version?"
# question (`needs_creator_choice`), which only makes sense when there is a different,
# simpler plan to choose, and never blocks an approved render.
_FORMAT_LIMIT_EN: tuple[str, ...] = (
    _TALKING_KEEPS_WHOLE_TAKE,
    _TALKING_KEEPS_QUOTED_PART,
    _VOICEOVER_SETS_LENGTH,
    _CLEANUP_PLANNED,
    _CLEANUP_AT_APPROVAL,
    _CLEANUP_IF_OFFERED,
    _CLEANUP_UNAVAILABLE,
    _CLEANUP_DONE,
    _CLEANUP_NOTHING_TO_CUT,
    _CLEANUP_NOT_RUN,
    _NAMED_CUT_STILL_IN,
    _NAMED_CUT_AT_RENDER,
    _BED_STILL_PLAYS,
    _NO_CLIP_SPEED,
    _NO_CLIP_SPEED_SONG,
    _NO_SPEED_ON_FREEZE,
    _NO_SPEED_RAMP,
    _NO_PHOTO_SPEED,
    _SPEED_DROPPED,
    _SPEED_NO_FOOTAGE,
    _NO_FREEZE_FRAME,
    _FREEZE_NOT_GROUNDED,
    _TEXT_STAYS_STRAIGHT,
    _TYPEWRITER_STAYS_STRAIGHT,
    _MOMENTS_NOT_LOCATED,
    _MOMENTS_CREATOR_CALL,
    _FREEZE_NEEDS_FRAME,
    _FREEZE_FIRST_FRAME,
    _LIGHT_UNAVAILABLE,
    # KRI-724: the planned source cut / noise filter, and what the noise filter can't do.
    _SPEECH_CUT_DRAFT_PLAN,
    _NOISE_REDUCTION_PLANNED,
    _NOISE_REDUCTION_APPLIED,
    _NOISE_REDUCTION_UNAVAILABLE,
    _NOISE_REDUCTION_UNSUPPORTED,
    # KRI-842: how a Voiceover edit plays one clip's sound is the format's, not a simpler plan.
    _ONLY_LAST_CLIP_PLAYS_ON,
    _ONLY_ONE_CLIP_PLAYS_ON,
    _EVERY_CLIP_SOUND_UNDER_VOICE,
    _NO_CLIP_SOUND_UNDER_VOICE,
    _CLIP_SOUND_NEEDS_PHONE_VOICEOVER,
    *_TALKING_HONEST_EN,
    _VOICEOVER_SPOKEN_TEXT_PLAN,
)
_FORMAT_LIMIT_REASON_PREFIXES: tuple[str, ...] = (
    *_FORMAT_LIMIT_EN,
    *(_REASON_TR[reason] for reason in _FORMAT_LIMIT_EN),
)


def is_format_limit(reason: str | None) -> bool:
    """True for a receipt reason the creator cannot plan around (see above)."""
    return bool(reason) and (
        any(str(reason).startswith(prefix) for prefix in _FORMAT_LIMIT_REASON_PREFIXES)
        # The route that will render this draft does not read the ask (route_disclosure).
        or is_route_decline_reason(reason)
    )


def needs_creator_choice(receipt: RequirementReceipt | Mapping[str, Any]) -> bool:
    """A checked receipt the draft fell short on, that the creator should rule on
    before it replaces their work: not met, and not a limit of the chosen format."""
    row = receipt.model_dump() if isinstance(receipt, RequirementReceipt) else receipt
    return (
        row.get("verification") == "checked"
        and row.get("status") != "met"
        and not is_format_limit(row.get("reason"))
    )


# "labels just say X" / "X only" / "sadece X": the creator wants the text to BE the literal,
# not merely contain it.
_EXACT_TEXT_RE = re.compile(
    r"\b(?:just|only|exactly|simply|solely|sadece|yaln[i\u0131]zca|sade)\b", re.IGNORECASE
)


def _wants_exact_text(req: BriefRequirement) -> bool:
    return bool(_EXACT_TEXT_RE.search(req.description or ""))


def _talking_uses_every_clip(video_clip_count: int) -> bool:
    from app.services.phone_rollout import (  # noqa: PLC0415
        TALKING_MULTI_CLIP_MAX_CLIPS,
        phone_talking_multi_clip_supported,
    )

    if phone_talking_multi_clip_supported():
        return 1 <= video_clip_count <= TALKING_MULTI_CLIP_MAX_CLIPS
    return video_clip_count == 1


def _check_whole_take(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A single-clip subtitled edit renders its one clip full-length."""
    if facts.edit_format is None or facts.editor:
        return _receipt(req, "partial", _CANT_CHECK_TAKE)
    if facts.edit_format != "subtitled":
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"This {facts.edit_format.replace('_', ' ')} edit cuts your footage down; "
                    "a Talking edit keeps the whole take."
                ),
                tr=(
                    f"Bu düzenleme ({_FORMAT_TR.get(facts.edit_format, facts.edit_format)}) "
                    "çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur."
                ),
            ),
        )
    if facts.video_clip_count is None:
        return _receipt(req, "partial", _CANT_CHECK_TAKE)
    # KRI-719: with multi-clip Talking live, every attached clip is a speaker or a
    # cutaway of the one edit, so more than one clip is not a dropped take.
    if not _talking_uses_every_clip(facts.video_clip_count):
        return _receipt(
            req,
            "partial",
            say(
                en="This edit uses one of your clips, not every take.",
                tr="Bu düzenleme kliplerinden yalnızca birini kullanıyor, her çekimi değil.",
            ),
        )
    if facts.speech_cleanup_enabled:
        return _receipt(
            req,
            "partial",
            say(
                # KRI-551: consented cleanup cuts pauses and filler sounds, never a retake.
                en="Speech cleanup is on, so some long pauses or filler sounds may be cut.",
                tr=(
                    "Konuşma temizliği açık, o yüzden bazı uzun duraklamalar ya da dolgu "
                    "sesleri kesilebilir."
                ),
            ),
        )
    return _receipt(req, "met", None)


def _unheard_for(facts: PlanFacts, names: Sequence[str]) -> list[str]:
    """The never-heard triggers this ask is about: only the ones it names.

    An ask that names nothing is not blamed for another beat's unheard word.
    """
    return [t for t in facts.unheard_beat_triggers if any(_trigger_heard(n, t) for n in names)]


def _asks_about_other_speaker(req: BriefRequirement) -> bool:
    """True when the ask names someone other than the creator as the one who says the words:
    a ``speaker`` fact that is not the creator or anyone ("Maximo"), or a cue worded "when he
    mentions ..." (KRI-1019)."""
    return _fact_speaker(req) is not None or _cue_trigger_match(req)[1] is not None


def _never_heard_problem(triggers: Iterable[str], *, other_speaker: bool = False) -> str:
    names = _names(triggers)
    if other_speaker:
        # The words were meant for someone else's mouth: "your voice" would blame the creator.
        return say(
            en=f"I never heard {names} in the take",
            tr=f"Videoda {names} sözünü duymadım",
        )
    return say(
        en=f"I never heard {names} in your voice",
        tr=f"Sesinde {names} sözünü duymadım",
    )


# KRI-547: why a pop-in that WAS heard still didn't show. Card geometry drops a card
# with "no_safe_spot" when no spot clears the face and the captions, and the beat lane
# with "overlap" (or arbitration with "duplicate") when another pop-in holds the spot.
_NO_ROOM_REASONS = frozenset({"no_safe_spot"})
_OVERLAP_REASONS = frozenset({"overlap", "duplicate", "fullscreen_overlap"})


def _beat_room_facts(receipt: object) -> dict[str, Any]:
    """``beat_room_drops`` off a variant's ``phone_beat_receipt`` (``{}`` when none)."""
    if not isinstance(receipt, Mapping) or receipt.get("matcher") in ("manual", "failed", None):
        return {}
    drops: list[tuple[str, str]] = []
    unplaced = receipt.get("unplaced")
    for entry in unplaced if isinstance(unplaced, list) else []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("trigger"), str):
            continue
        reason = entry.get("reason")
        if reason in _NO_ROOM_REASONS:
            drops.append((entry["trigger"], "no_room"))
        elif reason in _OVERLAP_REASONS:
            drops.append((entry["trigger"], "overlap"))
    return {"beat_room_drops": tuple(dict.fromkeys(drops))} if drops else {}


def _beat_room_drop(facts: PlanFacts, name: str) -> str | None:
    """``no_room`` / ``overlap`` when the render dropped ``name``'s pop-in for lack of room."""
    return next(
        (kind for trigger, kind in facts.beat_room_drops if _trigger_heard(name, trigger)), None
    )


def _beat_room_problem(req: BriefRequirement, facts: PlanFacts, names: Sequence[str]) -> str | None:
    """The honest sentence for this ask's pop-ins that had no room (``None`` if none).

    A pop-in is this ask's when it matches one of ``names``, or -- for an ask whose
    trigger words could not be named ("'İlk durak' dediğinde ...") -- when its trigger
    appears in the requirement's own words."""
    text = _req_text(req)
    drops = [
        (trigger, kind)
        for trigger, kind in facts.beat_room_drops
        if (
            any(_trigger_heard(n, trigger) for n in names)
            if names
            else _contains_text(text, _fold(trigger))
        )
    ]
    if not drops:
        return None
    parts: list[str] = []
    no_room = [trigger for trigger, kind in drops if kind == "no_room"]
    overlap = [trigger for trigger, kind in drops if kind == "overlap"]
    if no_room:
        parts.append(
            say(
                en=(
                    f"There was no room on screen for {_names(no_room)} without covering "
                    "a face or the captions"
                ),
                tr=(
                    f"{_names(no_room)} için bir yüzü ya da altyazıları kapatmadan "
                    "ekranda yer yoktu"
                ),
            )
        )
    if overlap:
        parts.append(
            say(
                en=f"{_names(overlap)} would have landed on another pop-in at the same moment",
                tr=f"{_names(overlap)} aynı anda başka bir görselin üstüne denk gelirdi",
            )
        )
    return "; ".join(parts)


# Any quote mark, apostrophes included: "her 'kahve' kelimesinde" quotes with them.
_CAPPED_WORD_QUOTES = "\"'“”‘’«»"


def _ask_is_about_capped_word(req: BriefRequirement, names: Sequence[str], trigger: str) -> bool:
    """Whether the ask is about ``trigger``: one of its named words, or (naming none, like
    T3's "her 'kahve' kelimesinde ...") the word quoted or after Turkish "her" (every)."""
    if names:
        return any(_trigger_heard(n, trigger) for n in names)
    word = re.escape(_fold(trigger))
    quotes = _CAPPED_WORD_QUOTES
    pattern = rf"[{quotes}]{word}[{quotes}]|\bher\s+[{quotes}]?{word}(?!\w)"
    return re.search(pattern, _req_text(req)) is not None


def _capped_beat_problems(
    req: BriefRequirement, facts: PlanFacts, names: Sequence[str]
) -> list[str]:
    """KRI-550: a word this ask is about that the render marked on only some of the times
    it was said. Another ask's capped word is not this ask's problem."""
    problems: list[str] = []
    for trigger, placed, heard in facts.capped_beat_hits:
        if not _ask_is_about_capped_word(req, names, trigger):
            continue
        problems.append(
            say(
                en=f'I added it on only {placed} of the {heard} times you said "{trigger}"',
                tr=f'"{trigger}" dediğin {heard} yerden yalnızca {placed} tanesine ekleyebildim',
            )
        )
    # A photo beat and a sound beat on one word read once.
    return list(dict.fromkeys(problems))


def _placement_reason(placements: Sequence[BeatFact]) -> str | None:
    """Where the met pop-ins landed, in time order; None when the render gave no times."""
    timed = sorted((b for b in placements if b.at_s is not None), key=lambda b: b.at_s or 0.0)
    if not timed:
        return None
    shown = timed[:_MAX_NAMED_IN_REASON]
    more = len(timed) - len(shown)
    items_en = [f'"{b.trigger}" at {b.at_s:.1f} s' for b in shown]
    if more > 0:
        items_en.append(f"{more} more")
    joined_en = (
        items_en[0] if len(items_en) == 1 else ", ".join(items_en[:-1]) + " and " + items_en[-1]
    )
    parts_tr = [f'"{b.trigger}" {b.at_s:.1f} sn\'de' for b in shown]
    tail_tr = f" ve {more} tane daha" if more > 0 else ""
    return say(
        en=f"{'Pop-in' if len(timed) == 1 else 'Pop-ins'} on {joined_en}",
        tr=f"{', '.join(parts_tr)}{tail_tr} çıkıyor",
    )


# KRI-840: the recorded ``facts["display_mode"]`` -> how the ask wants its photos shown.
_DISPLAY_ASKS = {"fullscreen": "fullscreen", "pip": "pip", "corner": "pip"}


def _display_ask(req: BriefRequirement) -> str | None:
    """How the model recorded the photo should show: "fullscreen" or "pip", else None.

    Read from the recorded fact only (KRI-667): an ask that names no display gets no display
    verdict, however it is worded.
    """
    value = req.facts.get("display_mode")
    if not isinstance(value, str):
        return None
    return _DISPLAY_ASKS.get("".join(ch for ch in value.casefold() if ch.isalnum()))


def _display_problem(asked: str, triggers: Iterable[str]) -> str:
    names = _names(f'"{trigger}"' for trigger in triggers)
    if asked == "fullscreen":
        return say(
            en=f"For {names}, the photo shows as a small card in the corner, not full screen",
            tr=f"{names} için fotoğraf tam ekran değil, köşede küçük bir kart olarak çıkıyor",
        )
    return say(
        en=f"For {names}, the photo shows full screen, not as a small card in the corner",
        tr=f"{names} için fotoğraf köşede küçük bir kart değil, tam ekran çıkıyor",
    )


def _sequence_problem(req: BriefRequirement, facts: PlanFacts) -> str | None:
    """A full-screen ask at a spoken word that only the even-slot layout covers (KRI-840)."""
    if not facts.fullscreen_sequence or _display_ask(req) != "fullscreen" or not _wants_beats(req):
        return None
    return say(
        en=(
            "Your Visuals play full screen, but spread evenly across the talk, "
            "not timed to the word you said"
        ),
        tr=(
            "Görsellerin tam ekran çıkıyor ama konuşmaya eşit aralıklarla dağılıyor, "
            "söylediğin kelimeye göre zamanlanmıyor"
        ),
    )


def _timed_card_ask(req: BriefRequirement) -> Mapping[str, Any] | None:
    """The model's recorded ``timed_card`` fact (KRI-667: never a new word regex here):
    ``{"visual": ..., "at_s": ..., "clip": ..., "hold_s": ...}``, else None."""
    fact = req.facts.get("timed_card")
    return fact if isinstance(fact, Mapping) else None


def _cards_for_ask(ask: Mapping[str, Any], cards: Sequence[TimedCardFact]) -> list[TimedCardFact]:
    """The cards this ask is about: the one nearest its second (within 0.6 s), else the ones
    whose label shares a word with the visual it named, else every card (one ask, one lane)."""
    at_s = _finite_number(ask.get("at_s"))
    if at_s is not None:
        near = [c for c in cards if c.at_s is not None and abs(c.at_s - at_s) <= 0.6]
        if near:
            return near
    visual = _fold(str(ask.get("visual") or ""))
    words = {w for w in visual.split() if len(w) > 2}
    if words:
        named = [c for c in cards if c.label and words & set(_fold(c.label).split())]
        if named:
            return named
    return list(cards)


def _timed_card_name(card: TimedCardFact) -> str:
    return card.label or say(en="the photo", tr="fotoğraf")


def _timed_card_met(card: TimedCardFact) -> str:
    name = _timed_card_name(card)
    hold = card.hold_s if card.hold_s is not None else 0.0
    if card.at_s is None:
        return say(
            en=f"Small card of {name} during the clip you named for {hold:.1f} s",
            tr=f"{name} söylediğin klipte {hold:.1f} sn küçük kart olarak",
        )
    return say(
        en=f"Small card of {name} at {card.at_s:.1f} s for {hold:.1f} s",
        tr=f"{name} {card.at_s:.1f} sn'de {hold:.1f} sn küçük kart olarak",
    )


def _timed_card_problem(card: TimedCardFact) -> str:
    name = _timed_card_name(card)
    at = f"{card.at_s:.1f} s" if card.at_s is not None else None
    reason = card.reason or ""
    if reason == "past_end":
        end = card.timeline_end_s
        return say(
            en=(
                f"{at} is past the end of the video"
                + (f" ({end:.1f} s)" if end is not None else "")
                + f", so the small card of {name} isn't shown"
            ),
            tr=(
                f"{at} videonun sonundan sonra"
                + (f" ({end:.1f} sn)" if end is not None else "")
                + f", o yüzden {name} küçük kartı gösterilmiyor"
            ),
        )
    if reason == "face_collision":
        return say(
            en=f"I couldn't place the small card of {name} without covering a face",
            tr=f"{name} küçük kartını bir yüzü kapatmadan yerleştiremedim",
        )
    if reason == "overlaps_card":
        return say(
            en=f"The small card of {name} would overlap another card, so it isn't shown",
            tr=f"{name} küçük kartı başka bir kartla çakışıyor, o yüzden gösterilmiyor",
        )
    if reason == "video_not_supported":
        return say(
            en=f"Videos can't show as small cards in this edit yet, so {name} is left out",
            tr=f"Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor, {name} koymadım",
        )
    if reason in ("lane_disabled", "unavailable"):
        return _timed_cards_unavailable()
    if reason in ("visual_not_found", "visual_not_bound"):
        return say(
            en=f"I couldn't find {name} in your Visuals for the small card",
            tr=f"Küçük kart için {name} görsellerinde bulamadım",
        )
    if reason == "clip_not_in_edit":
        return say(
            en=f"The clip the small card of {name} should follow isn't in this edit",
            tr=f"{name} küçük kartının takip edeceği klip bu düzenlemede yok",
        )
    return say(
        en=f"The small card of {name} couldn't be drawn on this render",
        tr=f"{name} küçük kartı bu videoya çizilemedi",
    )


def _timed_cards_unavailable() -> str:
    # A function, not a constant: `say` reads the chat's language at call time.
    return say(
        en="Small cards at an exact time aren't available for this edit yet",
        tr="Tam zamanlı küçük kartlar bu düzenleme için henüz yok",
    )


def _cant_check_timed_card() -> str:
    return say(
        en="I can't check the small card on this draft yet.",
        tr="Bu taslakta küçük kartı henüz kontrol edemiyorum.",
    )


def _check_timed_cards(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """An exact-timed small card ask, judged against the repaired draft or the render's own
    `phone_timed_card_receipt` -- "met" names the second and hold the card really has."""
    ask = _timed_card_ask(req) or {}
    if facts.timed_cards_available is False:
        return _receipt(req, "not_possible", _timed_cards_unavailable() + ".")
    if facts.timed_cards is None:
        return _receipt(req, "partial", _cant_check_timed_card())
    cards = _cards_for_ask(ask, facts.timed_cards)
    if not cards:
        return _receipt(
            req,
            "not_possible",
            say(
                en="This draft doesn't place a small card at that moment.",
                tr="Bu taslak o anda küçük bir kart koymuyor.",
            ),
        )
    shown = [c for c in cards if c.status in ("planned", "placed")]
    missing = [c for c in cards if c.status == "unplaced"]
    if shown and not missing:
        reason = _names_join([_timed_card_met(c) for c in shown]) + "."
        verification = "checked" if any(c.status == "placed" for c in shown) else None
        return _receipt(req, "met", reason, verification=verification)
    problems = [_timed_card_problem(c) for c in missing]
    if shown:
        problems.insert(0, _names_join([_timed_card_met(c) for c in shown]))
        return _receipt(req, "partial", "; ".join(problems) + ".")
    return _receipt(req, "not_possible", "; ".join(problems) + ".")


def _names_join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + say(en=" and ", tr=" ve ") + items[-1]


def _check_reaction_beats(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Word-triggered pop-ins and the closing shot, from the repaired strategy."""
    wants_beats = _wants_beats(req)
    wants_closing = _closing_photo_ask(req, facts)
    asked_display = _display_ask(req) if wants_beats else None
    sequence_problem = _sequence_problem(req, facts)
    if facts.reaction_beats is None:
        if sequence_problem:
            return _receipt(req, "partial", sequence_problem + ".")
        return _receipt(req, "partial", _CANT_CHECK_BEATS)
    beats = facts.reaction_beats
    problems: list[str] = []
    delivered = False
    placements: list[BeatFact] = []
    by_name = True
    if wants_beats:
        # Words the creator named for certain (facts, quotes) can be "missing"; words only
        # guessed from the sentence pick which pop-ins the ask is about, never a verdict.
        named = _named_triggers(req)
        names = named or _cue_triggers(req)
        if not facts.reaction_beats_available:
            problems.append(
                say(
                    en="Photo and sound pop-ins timed to your words aren't available for this "
                    "edit yet",
                    tr="Sözlerine göre çıkan fotoğraf ve ses efektleri bu düzenleme için henüz yok",
                )
            )
        elif not beats:
            # Nothing was placed at all: every unheard word is this ask's problem.
            unheard = list(facts.unheard_beat_triggers) if not names else _unheard_for(facts, names)
            room = _beat_room_problem(req, facts, names)
            if unheard:
                problems.append(
                    _never_heard_problem(unheard, other_speaker=_asks_about_other_speaker(req))
                )
            elif room:
                problems.append(room)
            else:
                problems.append(
                    say(
                        en=(
                            "I couldn't find the photos or stickers for "
                            f"{_names(facts.dropped_beat_triggers)}"
                        ),
                        tr=(
                            "Şunlar için fotoğraf ya da çıkartma bulamadım: "
                            f"{_names(facts.dropped_beat_triggers)}"
                        ),
                    )
                    if facts.dropped_beat_triggers
                    else sequence_problem
                    or say(
                        en="This draft has no pop-ins timed to your words",
                        tr="Bu taslakta sözlerine göre çıkan görsel yok",
                    )
                )
                delivered = bool(sequence_problem) and not facts.dropped_beat_triggers
        else:
            unheard = _unheard_for(facts, names)
            matched = [b for b in beats if any(_trigger_heard(n, b.trigger) for n in names)]
            by_name = True
            if not names:
                placements = list(beats)
            elif matched:
                placements = matched
            elif all(
                any(_trigger_heard(n, u) for u in unheard) or _beat_room_drop(facts, n)
                for n in names
            ):
                # Everything this ask is about was never said, or had no room on
                # screen (KRI-547): nothing of it landed.
                placements = []
            else:
                # The sentence's own words matched no beat: judge the pop-ins as a whole
                # (and quote no times, which would belong to other words).
                placements = list(beats)
                by_name = False
            delivered = bool(placements)
            # A word the render never heard is told as that, not as a missing pop-in.
            missing = [
                n
                for n in named
                if not any(_trigger_heard(n, b.trigger) for b in beats)
                and not any(_trigger_heard(n, u) for u in unheard)
            ]
            if unheard:
                problems.append(
                    _never_heard_problem(unheard, other_speaker=_asks_about_other_speaker(req))
                )
            # Named words: only the ones that didn't land; unnamed: the ask's own words.
            room = None if named and not missing else _beat_room_problem(req, facts, missing)
            if room:
                problems.append(room)
                missing = [n for n in missing if not _beat_room_drop(facts, n)]
            unresolved = [
                t
                for t in facts.dropped_beat_triggers
                if not any(_trigger_heard(t, b.trigger) for b in beats)
                and not any(_trigger_heard(t, n) for n in missing)
            ]
            if missing:
                problems.append(
                    sequence_problem
                    or say(
                        en=f"No pop-in for {_names(missing)}",
                        tr=f"Şunlar için çıkan görsel yok: {_names(missing)}",
                    )
                )
                delivered = delivered or bool(sequence_problem)
            if unresolved:
                problems.append(
                    say(
                        en=f"I couldn't find the photo or sticker for {_names(unresolved)}",
                        tr=(f"Şunlar için fotoğraf ya da çıkartma bulamadım: {_names(unresolved)}"),
                    )
                )
            problems.extend(_capped_beat_problems(req, facts, names))
            text = _req_text(req)
            if placements:
                if any(b.following_clip_id and b.sequence_realized is False for b in placements):
                    problems.append(
                        say(
                            en="The full-screen photo followed by its video has not been verified",
                            tr="Tam ekran fotoğrafın ardından videonun geldiği doğrulanmadı",
                        )
                    )
                if _SOUND_RE.search(text) and not any(b.sound for b in placements):
                    problems.append(
                        say(
                            en="None of the pop-ins plays a sound",
                            tr="Çıkan görsellerin hiçbiri ses çalmıyor",
                        )
                    )
                if (
                    _VISUAL_RE.search(text)
                    and not any(b.visual_id for b in placements)
                    and not room  # KRI-547: the dropped card is the reason, told above
                ):
                    problems.append(
                        say(
                            en="None of the pop-ins shows a photo or sticker",
                            tr="Çıkan görsellerin hiçbiri fotoğraf ya da çıkartma göstermiyor",
                        )
                    )
                if asked_display is not None:
                    shown = [b for b in placements if b.visual_id]
                    wrong = [b for b in shown if b.display_mode != asked_display]
                    # Pop-ins matched to the ask's own words must all show as asked; judged as
                    # a whole (no word matched), one that does is enough.
                    if wrong and ((names and by_name) or len(wrong) == len(shown)):
                        problems.append(_display_problem(asked_display, [b.trigger for b in wrong]))
    if wants_closing:
        if facts.closing_visual_id is None:
            if facts.closing_requested:
                problems.append(
                    say(
                        en="I couldn't find the closing photo you named",
                        tr="Söylediğin kapanış fotoğrafını bulamadım",
                    )
                )
            elif _ending_names_picture(req):
                problems.append(
                    say(
                        en="This draft doesn't end on the photo you asked for",
                        tr="Bu taslak istediğin fotoğrafla bitmiyor",
                    )
                )
            else:
                # KRI-688: nothing says the ending is a picture; never call it a photo.
                problems.append(
                    say(
                        en="This draft doesn't end the way you asked",
                        tr="Bu taslak istediğin gibi bitmiyor",
                    )
                )
        else:
            delivered = True
            if facts.closing_badge_requested and facts.closing_badge_id is None:
                problems.append(
                    say(
                        en="I couldn't find the closing badge you named",
                        tr="Söylediğin kapanış rozetini bulamadım",
                    )
                )
    if not problems:
        return _receipt(req, "met", _placement_reason(placements) if by_name else None)
    status = "partial" if delivered else "not_possible"
    return _receipt(req, status, "; ".join(problems) + ".")


# KRI-783: each pattern carries its Turkish alternatives too, written Turkish-folded
# (diacritics stripped); `_speech_cue` also runs it on the `loose_text` form, so
# "KONUŞMAMI", "konuşmamı" and "konusmami" read alike. Only first-person Turkish forms
# ("my speech", "what I said"): bare "konuşma"/"ses" also mean "don't talk"/"sound", and a
# genitive ("konuşmamın yazısı", the text of my speech) is a captions ask, not this one.
_SPEECH_EXCERPT_RE = re.compile(
    r"\b(?:excerpts?|speech|spoken|my voice|my words|what i (?:say|said|talk)|"
    r"(?:play|use|hear|keep)\s+(?:my|the)\s+(?:\w+\s+){0,3}"
    r"(?:line|lines|sentence|sentences|quote|quotes|"
    r"talking|speech|voice)|"
    r"(?:konusma|konusmalari|sesi|sozleri|cumleleri)m(?:i|la|le|dan|den)?|"
    r"(?:konustugum|soyledigim|dedigim)(?:u|i)?|"
    r"(?:konustuklarim|soylediklerim|dediklerim)(?:i|dan|den)?|alinti\w*)\b"
)
_SPEECH_OVER_RE = re.compile(
    r"\b(?:over|b-?roll|other (?:footage|clips|videos)|while|on top of|under|"
    r"ustune|ustunde|uzerine|uzerinde|altinda|arkasinda|"
    r"diger\s+(?:klip|video|cekim|goruntu)\w*)\b"
)
_SPEAKER_RE = re.compile(
    r"\b(?:cut to me|show me|back to me|cut to the speaker|me talking|on camera|me saying|"
    r"bana\s+(?:kes|gec|don|geri)\w*|beni\s+(?:de\s+)?goster\w*|konusurken\s+beni|"
    r"kameraya\s+konus\w*|(?:yuzumu|kendimi)\s+(?:de\s+)?goster\w*)\b"
)
# Turkish also puts the back-word after the noun ("montaja geri dön") and says "between
# my lines" with a postposition ("cümlelerimin arasına hızlı kesimler koy").
_BACK_TO_MONTAGE_RE = re.compile(
    r"\b(?:back to|return to|then|sonra|tekrar|geri)\b.{0,30}"
    r"\b(?:montage|cuts|clips|montaj\w*|kesim\w*|klip\w*)\b"
    r"|\b(?:montaj|kesim|klip)\w*\s+(?:geri|tekrar)\s+(?:don|gec)\w*"
    r"|\baras(?:ina|inda)\b.{0,30}\b(?:kesim|montaj)\w*"
)


def _speech_cue(req_text: str, pattern: re.Pattern[str]) -> bool:
    """The bilingual cue on the folded ask, or on its diacritic-free form."""
    return bool(pattern.search(req_text) or pattern.search(loose_text(req_text)))


def _records_caption_ask(req: BriefRequirement) -> bool:
    """The model recorded this requirement as a captions ask (``captions``,
    ``caption_language`` or ``spellings``) and recorded no spoken triggers."""
    return (
        _caption_fact(req) is not None
        or _caption_language_fact(req) is not None
        or bool(_spellings_fact(req))
    ) and not _fact_triggers(req)


def _wants_speech(req: BriefRequirement) -> bool:
    # KRI-961: the recorded captions facts decide (KRI-667). "English captions on my voice"
    # names the voice the captions are on; read as a speech-excerpt ask it lost its captions
    # check and the spellings the render applies (`brief_caption_spellings`).
    if req.kind not in _BEAT_KINDS or _records_caption_ask(req):
        return False
    return _speech_cue(_req_text(req), _SPEECH_EXCERPT_RE)


def _check_speech_excerpts(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Spoken excerpts grounded, played over other footage where asked, back to the speaker."""
    sections = facts.speech_sections
    if sections is None or facts.editor:
        return _receipt(req, "partial", _CANT_CHECK_SPEECH)
    text = _req_text(req)
    speech = [s for s in sections if s.kind == "speech"]
    problems: list[str] = []
    missing = _names(f"“{q}”" for q in facts.speech_dropped_quotes)
    if not speech:
        return _receipt(
            req,
            "not_possible",
            say(
                en="None of your lines made it in",
                tr="Cümlelerinden hiçbiri videoya girmedi",
            )
            + (f" ({missing})" if missing else ""),
        )
    if missing:
        problems.append(
            say(
                en=f"I couldn't find {missing} in your clip",
                tr=f"Klibinde şunu bulamadım: {missing}",
            )
        )
    if _speech_cue(text, _SPEECH_OVER_RE) and not any(s.visual == "cutaways" for s in speech):
        problems.append(
            say(
                en="Your words never play over the other footage",
                tr="Sözlerin diğer çekimlerin üzerinde hiç çalmıyor",
            )
        )
    if _speech_cue(text, _SPEAKER_RE) and not any(s.visual == "speaker" for s in speech):
        problems.append(
            say(
                en="It never cuts to you talking",
                tr="Hiçbir yerde konuşurken sana geçmiyor",
            )
        )
    # "Return to the speaker": a speaker shot that comes AFTER something that is not the speaker.
    first_other = next(
        (i for i, s in enumerate(sections) if s.kind == "montage" or s.visual == "cutaways"), None
    )
    if (
        first_other is not None
        and _speech_cue(text, _SPEAKER_RE)
        and not any(s.visual == "speaker" for s in sections[first_other + 1 :])
    ):
        problems.append(
            say(
                en="It doesn't come back to you after the other footage",
                tr="Diğer çekimlerden sonra sana geri dönmüyor",
            )
        )
    if _speech_cue(text, _BACK_TO_MONTAGE_RE) and not any(s.kind == "montage" for s in sections):
        problems.append(
            say(
                en="There are no fast cuts between your lines",
                tr="Cümlelerin arasında hızlı kesimler yok",
            )
        )
    if not problems:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "; ".join(problems) + ".")


_NAMED_CUT_MISSED = {
    "not_found": ("I couldn't find it in your take", "çekiminde bulamadım"),
    "said_more_than_once": (
        "you say it more than once, so tell me which one to cut: the first, second or last",
        "bunu birden fazla kez söylüyorsun, hangisini keseyim söyle: ilkini, ikincisini ya da"
        " sonuncusunu",
    ),
    "occurrence_not_found": (
        "you don't say it that many times",
        "bunu o kadar çok kez söylemiyorsun",
    ),
    # KRI-724: the occurrence the creator picked is never swapped for another one.
    "pinned_occurrence_missing": (
        "I couldn't find the one you picked in your take",
        "seçtiğin anı çekiminde bulamadım",
    ),
    "too_short": (
        "cutting it would leave too little video",
        "kesersem çok az video kalırdı",
    ),
    "cleanup_off": (
        "your take has no word timings to find it in",
        "çekiminde onu bulacak kelime zamanlaması yok",
    ),
    # KRI-889: the line was found, but cutting the voiceover file failed.
    "cut_failed": (
        "cutting it out of your voiceover failed",
        "seslendirmenden kesmek başarısız oldu",
    ),
}


def _named_cut_notes(req: BriefRequirement, facts: PlanFacts) -> tuple[list[str], bool] | None:
    """KRI-821: what a phone Talking render did with each line this ask named to cut.

    ``(notes, all_cut)`` off the render's ``named_cuts`` receipts for this requirement;
    None when the render carries no such record (another format: `_NAMED_CUTS_STAY`).
    """
    if facts.named_cuts is None:
        return None
    rows = [row for row in facts.named_cuts if req.id in (row.get("requirement_ids") or [])]
    if not rows:
        return None
    notes: list[str] = []
    for row in rows:
        quote = str(row.get("quote") or "")
        if row.get("status") == "applied":
            removed = _finite_number(row.get("removed_s")) or 0.0
            notes.append(
                say(
                    en=f'Cut "{quote}" ({_round_s(removed)}s)',
                    tr=f'"{quote}" kesildi ({_round_s(removed)} sn)',
                )
            )
            continue
        en, tr = _NAMED_CUT_MISSED.get(str(row.get("reason") or ""), _NAMED_CUT_MISSED["not_found"])
        notes.append(
            say(
                en=f'{_NAMED_CUT_STILL_IN}: "{quote}" ({en})',
                tr=f'{_REASON_TR[_NAMED_CUT_STILL_IN]}: "{quote}" ({tr})',
            )
        )
    return notes, all(row.get("status") == "applied" for row in rows)


def _cuts_named_voiceover_lines(facts: PlanFacts) -> bool:
    """KRI-889: a narrated render cuts named lines out of the recorded voiceover.

    Phone and cloud both do, behind ``NARRATED_NAMED_CUTS_ENABLED`` (read here too, so the
    draft never promises a cut the worker won't make). A guided story is not cut.
    """
    from app.config import settings  # noqa: PLC0415

    return (
        _is_voiceover_edit(facts)
        and facts.render_program != "guided"
        and bool(settings.narrated_named_cuts_enabled)
    )


def _check_speech_cleanup(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Pauses, retakes and filler the creator asked to cut, against the draft.

    Speech cleanup (the preflight check the creator confirms at approval) cuts long
    pauses and non-word sounds out of a Talking or voiceover edit. A retake count or
    other generic cleanup record cannot confirm that a requested spoken line was removed,
    so a retake or "the bit where I say X" stays partial, with the limit stated plainly
    (KRI-846: never as editor work, since no editor action cuts a spoken line).
    A length the same sentence names ("keep it under 45 s") follows the cut take.
    Every honest outcome here is a limit of the format, not a simplification.
    """
    fmt = facts.edit_format
    if fmt is None or (facts.editor and not facts.rendered_variant) or fmt not in _SPEECH_FORMATS:
        # Only a speech-spined edit is judged here; a montage's cut is its own
        # planner's to report, so the ask stays "can't verify" there, as before.
        return _receipt(req, "partial", _CANT_CHECK_CLEANUP)
    if facts.rendered_variant:
        return _merge_cleanup_timing(req, facts, _check_rendered_cleanup(req, facts))
    if facts.speech_cleanup_enabled:
        lead, status = _CLEANUP_PLANNED, "met"
    elif facts.speech_cleanup_offered is True:
        lead, status = _CLEANUP_AT_APPROVAL, "partial"
    elif facts.speech_cleanup_offered is False:
        lead, status = _CLEANUP_UNAVAILABLE, "partial"
    else:
        # Unknown whether approval offers the choice: never promise it.
        lead, status = _CLEANUP_IF_OFFERED, "partial"
    notes = [_loc(lead)]
    if _wants_named_cuts(req):
        if (facts.phone_talking or _cuts_named_voiceover_lines(facts)) and named_cut_lines(req):
            # KRI-821: the phone Talking render cuts each named line it finds said once;
            # KRI-889: so does a narrated render, out of the recorded voiceover.
            # Not certain before the render finds the line, so never "Done" (KRI-792).
            notes.append(_loc(_NAMED_CUT_AT_RENDER))
            status = "partial"
        else:
            notes.append(_loc(_NAMED_CUTS_STAY))
            status = "partial"
    cleanup = _receipt(req, status, "; ".join(notes) if status == "partial" else None)
    return _merge_cleanup_timing(req, facts, cleanup)


def _check_rendered_cleanup(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """KRI-541: the cleanup ask against what a finished render's speech cleanup did."""
    outcome = facts.speech_cleanup_outcome
    named = _named_cut_notes(req, facts) if _wants_named_cuts(req) else None
    if named is not None and not _CLEANUP_NOUN_RE.search(_req_text(req)):
        # KRI-821: an ask that only names lines to cut is answered by the render's own
        # per-line receipts, whatever speech cleanup did (it never cuts a named line).
        return _receipt(req, "met" if named[1] else "partial", "; ".join(named[0]))
    if outcome == "applied":
        lead, status = _CLEANUP_DONE, "met"
    elif outcome == "no_change":
        lead, status = _CLEANUP_NOTHING_TO_CUT, "met"
    elif outcome == "not_run":
        lead, status = _CLEANUP_NOT_RUN, "partial"
    else:
        if named is not None:
            # KRI-821: cleanup off (no cleanup record), but the named lines were cut on their own.
            return _receipt(req, "met" if named[1] else "partial", "; ".join(named[0]))
        return _receipt(req, "partial", _CANT_CHECK_CLEANUP)
    notes = [_loc(lead)]
    if _wants_named_cuts(req):
        if named is not None:
            # KRI-821: the render's own per-line receipt says whether the line is gone.
            notes.extend(named[0])
            if not named[1]:
                status = "partial"
            elif outcome == "applied":
                return _receipt(req, "met", "; ".join(named[0]))
        else:
            # The generic cut record cannot prove that this requested line was the removed span.
            notes.append(_loc(_NAMED_CUTS_STAY))
            status = "partial"
    if status == "met" and outcome == "applied":
        return _receipt(req, "met", None)
    return _receipt(req, status, "; ".join(notes))


def _cleanup_counts(variant: Mapping[str, Any]) -> tuple[int, int, int, float | None] | None:
    """Pauses, filler sounds and retakes a variant's cut record removed, and their length.

    Read off ``silence_cut.removed`` (one ``reason`` per span). ``None`` when the variant
    carries no cut record, so the note states no number it was not given. The seconds are
    summed over those cleanup rows only: the record also holds the quoted-line trim
    (``opening_cut`` / ``end_cut``), which ``time_saved_s`` counts too (KRI-978: "1 filler
    sound (41.4 s)" for a 0.32 s cut).
    """
    record = variant.get("silence_cut")
    rows = record.get("removed") if isinstance(record, Mapping) else None
    if not isinstance(rows, list) or not rows:
        return None
    pauses = fillers = retakes = 0
    seconds = 0.0
    for row in rows:
        reason = row.get("reason") if isinstance(row, Mapping) else None
        if reason == "silence":
            pauses += 1
        elif reason in ("filler_lexical", "filler_acoustic"):
            fillers += 1
        elif reason == "retake":
            retakes += 1
        else:
            continue
        start, end = row.get("start_s"), row.get("end_s")
        if seconds is not None and all(
            isinstance(value, int | float) and not isinstance(value, bool) for value in (start, end)
        ):
            seconds += max(0.0, float(end) - float(start))
        else:
            seconds = None
    if not (pauses or fillers or retakes):
        return None
    return pauses, fillers, retakes, seconds if seconds else None


def speech_cleanup_review_note(
    variant: Mapping[str, Any], *, text: str = "", retake_cut_possible: bool = False
) -> str | None:
    """What a finished Voiceover/Talking render's speech cleanup cut, and what it left (KRI-551).

    Read off the variant's own cut record, never off a brief, so the reply is honest even when
    the render's brief binding was lost (prod thread ddc6e71b: the creator asked to cut "the part
    where I restart the kilometer thirty sentence", the cleanup cut 14 silences and a filler,
    the restart stayed, and no receipt said so). A generic cleanup count cannot prove whether a
    requested spoken line was removed, so that remains a partial receipt even when it records a
    retake. The unbound-review limit is stated unless a retake cut really could have run
    (``retake_cut_possible``) or the record shows one. ``text`` is the reply built so far: a
    requirement receipt that already reports the limit is not repeated.
    ``None`` for a variant that is not a speech edit or whose cleanup did not run.
    """
    if variant.get("resolved_archetype") not in _RENDERED_EDIT_FORMATS:
        return None
    outcome = variant.get("silence_cut_outcome")
    if outcome not in ("applied", "no_change"):
        return None
    counts = _cleanup_counts(variant)
    retakes = counts[2] if counts else 0
    if outcome == "no_change":
        sentences = [_loc(_CLEANUP_NOTHING_TO_CUT) + "."]
    elif counts is None:
        sentences = [_loc(_CLEANUP_DONE) + "."]
    else:
        pauses, fillers, retakes, seconds = counts
        parts_en: list[str] = []
        parts_tr: list[str] = []
        for count, one, many, tr in (
            (pauses, "long pause", "long pauses", "uzun duraklama"),
            (fillers, "filler sound", "filler sounds", "dolgu sesi"),
            (retakes, "restarted sentence", "restarted sentences", "baştan başlanan cümle"),
        ):
            if count:
                parts_en.append(f"{count} {one if count == 1 else many}")
                parts_tr.append(f"{count} {tr}")
        joined_en = parts_en[0] if len(parts_en) == 1 else ", ".join(parts_en[:-1])
        if len(parts_en) > 1:
            joined_en += f" and {parts_en[-1]}"
        sentences = [
            say(
                en=(
                    f"Speech cleanup cut what it heard as {joined_en}"
                    + (f" ({seconds:.1f} s)" if seconds is not None else "")
                    + "."
                ),
                tr=(
                    f"Konuşma temizliği duyduğu şu sesleri kesti: {', '.join(parts_tr)}"
                    + (f" ({seconds:.1f} sn)".replace(".", ",") if seconds is not None else "")
                    + "."
                ),
            )
        ]
    if not retakes and not retake_cut_possible and _loc(_NAMED_CUTS_STAY) not in text:
        sentences.append(
            say(
                # KRI-970: what it looks for, never a promise of what it cut. Its filler
                # cuts trust the transcript's timing, which can sit over a real word.
                en="It looks only for pauses and filler sounds, so a sentence you restarted "
                "stays in.",
                tr=(
                    "Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir "
                    "cümle videoda kalır."
                ),
            )
        )
    return " ".join(sentences)


def _merge_cleanup_timing(
    req: BriefRequirement, facts: PlanFacts, cleanup: RequirementReceipt
) -> RequirementReceipt:
    """Retain cleanup facts while an explicit creator target judges actual duration."""
    if not _has_duration_target(req) or times_one_moment(req):
        return cleanup
    timing = _check_timing(req, facts)
    if timing.status == "met":
        return cleanup
    if cleanup.status == "met" or not cleanup.reason:
        return _receipt(req, "partial", timing.reason, verification=timing.verification)
    if not timing.reason or timing.reason == cleanup.reason:
        return cleanup
    return _receipt(req, "partial", f"{cleanup.reason}; {timing.reason}")


def _check_captions(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Captions on / off / word-by-word, against the strategy's caption style.

    Only what the style settles is judged: an ask about a caption's look, place or
    language stays "can't check", and so does ``"auto"`` (the item's own style,
    unknown here) for an on/off or word-by-word ask.
    """
    if facts.caption_language and not facts.editor:
        asked = _requested_caption_language(req)
        if asked is not None:
            return _check_caption_language(req, facts, asked)
    fmt = facts.edit_format
    text = _req_text(req)
    rendered = facts.rendered_variant
    if facts.montage_captions == "placed" and not (facts.editor or _CAPTION_DETAIL_RE.search(text)):
        # KRI-569: a song montage that carries the song's lyrics as captions.
        if _wants_no_captions(req):
            return _receipt(
                req,
                "partial",
                say(
                    en="Captions are still on in this draft.",
                    tr="Bu taslakta altyazılar hâlâ açık.",
                ),
            )
        if _wants_word_captions(req) and facts.montage_caption_style != "word":
            return _receipt(
                req,
                "partial",
                say(
                    en="Captions are on as full sentences, not word by word.",
                    tr="Altyazılar kelime kelime değil, tam cümleler olarak açık.",
                ),
            )
        return _receipt(req, "met", None)
    # KRI-1015: a captions ask that names no language is held to the spoken one. Translated
    # captions are said so (not "can't check"); two languages kept as spoken are never "met".
    translated = _translated_from(facts, str(facts.caption_language or "").casefold().split("-")[0])
    if translated and not facts.caption_translation_asked:
        rendered_name = _language_name(str(facts.caption_language).casefold().split("-")[0])
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"I translated the {_language_name(translated)} speech into "
                    f"{rendered_name} captions."
                ),
                tr=(f"{_language_name(translated)} konuşmayı {rendered_name} altyazıya çevirdim."),
            ),
        )
    if (
        facts.caption_style is None
        or (facts.editor and not rendered)
        or fmt not in _SPEECH_FORMATS
        or _CAPTION_DETAIL_RE.search(text)
        or _keeps_spoken_languages(req)
    ):
        return _receipt(req, "partial", _CANT_CHECK_CAPTIONS)
    style = facts.caption_style
    if _wants_no_captions(req):
        if style == "none":
            return _receipt(req, "met", None)
        if style == "auto":
            return _receipt(req, "partial", _CANT_CHECK_CAPTIONS)
        return _receipt(
            req,
            "partial",
            say(
                en="Captions are on in this video."
                if rendered
                else "Captions are still on in this draft.",
                tr="Bu videoda altyazılar açık."
                if rendered
                else "Bu taslakta altyazılar hâlâ açık.",
            ),
        )
    if style == "none":
        return _receipt(
            req,
            "partial",
            say(
                en="This video has no captions." if rendered else "Captions are off in this draft.",
                tr="Bu videoda altyazı yok." if rendered else "Bu taslakta altyazılar kapalı.",
            ),
        )
    if _wants_word_captions(req):
        if style in {"karaoke", "kinetic"}:
            return _receipt(req, "met", _CAPTIONS_WORD_BY_WORD)
        if style == "auto":
            return _receipt(req, "partial", _CANT_CHECK_CAPTIONS)
        return _receipt(
            req,
            "partial",
            say(
                en="Captions are on as full sentences, not word by word.",
                tr="Altyazılar kelime kelime değil, tam cümleler olarak açık.",
            ),
        )
    return _receipt(req, "met", None)


# A footage bed under this fraction of full volume counts as "quiet under the voice".
_BED_QUIET_BELOW = 0.5

# What a clip-sound ask that was not built says, by `phone_clip_sound` reason code (KRI-842).
_CLIP_SOUND_MISSED = {
    "not_in_edit": _CLIP_NOT_IN_EDIT,
    "no_sound": _CLIP_HAS_NO_SOUND,
    "not_last_clip": _ONLY_LAST_CLIP_PLAYS_ON,
    "no_room": _CLIP_SOUND_NO_ROOM,
    "line_too_long": _CLIP_LINE_TOO_LONG,
    "one_tail_only": _ONLY_ONE_CLIP_PLAYS_ON,
    "length_target": _CLIP_PLAYS_ON_PAST_LENGTH,
    "line_too_early": _CLIP_LINE_TOO_EARLY,
    "line_past_end": _CLIP_LINE_PAST_END,
    "render_refused": _CLIP_SOUND_REFUSED,
    "edited": _CLIP_SOUND_EDITED,
}


def _clip_sound_missed(
    req: BriefRequirement, code: str | None, quote: str | None = None
) -> RequirementReceipt:
    """A clip-sound ask that cannot be (or was not) played, with the plain reason."""
    if code == "line_too_early" and quote:
        return _receipt(
            req,
            "not_possible",
            say(
                en=f'"{quote}" is said too early in that clip to come after your voice',
                tr=f'"{quote}" o klipte seslendirmeden sonra gelemeyecek kadar erken söyleniyor',
            ),
        )
    if code in ("line_not_found", "line_ambiguous") and quote:
        found = (
            say(
                en=f'I couldn\'t find "{quote}" in that clip',
                tr=f'"{quote}" sözünü o klipte bulamadım',
            )
            if code == "line_not_found"
            else say(
                en=f'I couldn\'t tell exactly where "{quote}" is said in that clip',
                tr=f'"{quote}" sözünün o klipte tam nerede söylendiğini anlayamadım',
            )
        )
        tail = say(
            en=", so nothing plays on after your voice",
            tr=", o yüzden seslendirmeden sonra bir şey çalmıyor",
        )
        return _receipt(req, "not_possible", f"{found}{tail}")
    return _receipt(req, "not_possible", _CLIP_SOUND_MISSED.get(code or "", _CLIP_SOUND_NOT_BUILT))


def _clip_sound_met(
    req: BriefRequirement, ask: ClipSoundAsk, line: str | None
) -> RequirementReceipt:
    if ask.mode != AFTER_VOICE:
        return _receipt(req, "met", _CLIP_SOUND_UNDER_VOICE_MET)
    if not line:
        return _receipt(req, "met", _CLIP_PLAYS_ON_MET)
    return _receipt(
        req,
        "met",
        say(
            en=f'{_CLIP_PLAYS_ON_MET}, so "{line}" is heard',
            tr=f'{_REASON_TR[_CLIP_PLAYS_ON_MET]}, böylece "{line}" duyuluyor',
        ),
    )


def _check_narrated_clip_sound(
    req: BriefRequirement, ask: ClipSoundAsk, facts: PlanFacts
) -> RequirementReceipt:
    """One clip's own sound under the voice, or playing on after it (KRI-842).

    A finished phone Voiceover is judged from its own `clip_sound` receipt (what the pinned
    recipe plays), never from the global bed level; a draft that will render there is judged
    from the same planning rules the worker runs. Anywhere else (a cloud render, an editor
    turn, the worker's own mid-compile record) nothing is claimed.
    """
    if facts.rendered_variant:
        if facts.edit_format != "narrated":
            return _receipt(req, "partial", _NO_CHECKER)
        record = facts.clip_sound if isinstance(facts.clip_sound, Mapping) else {}
        rows = [
            *(record.get("under_voice") or []),
            record.get("after_voice"),
            *(record.get("not_built") or []),
        ]
        row = next(
            (r for r in rows if isinstance(r, Mapping) and r.get("requirement_id") == req.id),
            None,
        )
        if row is None:
            return _clip_sound_missed(req, None)
        if row.get("status") == "built":
            return _clip_sound_met(req, ask, row.get("line"))
        return _clip_sound_missed(req, row.get("reason_code"), row.get("line") or None)
    if facts.voiceover_edit:
        # A finished phone montage Voiceover: it has no per-clip gains or tail to build.
        return _receipt(req, "not_possible", _CLIP_SOUND_NEEDS_PHONE_VOICEOVER)
    if facts.editor or facts.rendered_output:
        return _receipt(req, "partial", _NO_CHECKER)
    if facts.phone_narrated:
        return _draft_clip_sound(req, ask, facts)
    if facts.audio_strategy == "voiceover":
        # A voiceover draft the phone Voiceover compiler will not render (a montage with a
        # voiceover, a cloud render): said before approval, not silently dropped.
        return _receipt(req, "not_possible", _CLIP_SOUND_NEEDS_PHONE_VOICEOVER)
    return _receipt(req, "partial", _NO_CHECKER)


def _draft_clip_sound(
    req: BriefRequirement, ask: ClipSoundAsk, facts: PlanFacts
) -> RequirementReceipt:
    from app.pipeline.phone_clip_sound import (  # noqa: PLC0415
        ground_trigger_line,
        media_aliases,
        resolve_ask_media,
        video_media_ids,
    )

    snapshot = facts.media_snapshot
    videos = video_media_ids(snapshot) or list(facts.clip_ids)
    media, why = resolve_ask_media(ask, videos, snapshot)
    if media is None:
        return _clip_sound_missed(req, why)
    if ask.mode != AFTER_VOICE:
        return _clip_sound_met(req, ask, None)
    last = facts.last_clip_id or (videos[-1] if videos else None)
    if last is None:
        return _receipt(req, "partial", _NO_CHECKER)
    if last not in media_aliases(snapshot, media):
        return _clip_sound_missed(req, "not_last_clip")
    if not ask.triggers:
        return _clip_sound_met(req, ask, None)
    quote = ask.triggers[0]
    grounded = ground_trigger_line(ask.triggers, media, snapshot)
    if grounded.status == "found":
        return _clip_sound_met(req, ask, grounded.quote)
    if grounded.status == "unknown":
        # Its speech is not analysed yet: the render finds the line, the draft cannot.
        return _receipt(
            req,
            "partial",
            say(
                en=f'{_CLIP_PLAYS_ON_MET}, but I can\'t hear "{quote}" in it yet',
                tr=f'{_REASON_TR[_CLIP_PLAYS_ON_MET]}, ama "{quote}" sözünü henüz duyamıyorum',
            ),
        )
    return _clip_sound_missed(
        req, "line_ambiguous" if grounded.status == "ambiguous" else "line_not_found", quote
    )


# KRI-942: clip sound turned on / off by clip, by seconds or around a spoken line (see
# `kria.clip_sound_toggle_ask`). Judged on the recipe the phone renders, never the plan's claim.
def _sound_toggle_needs_phone_montage() -> str:
    return say(
        en="Turning clip sound on or off by clip or by seconds works on a montage made on your "
        "iPhone, not in this edit",
        tr="Klip sesini klip ya da saniye bazında açıp kapatmak yalnızca iPhone'unda yapılan "
        "montajda çalışıyor, bu düzenlemede değil",
    )


def _toggle_where(target: SoundTarget) -> tuple[str, str]:
    """Where a part of an on/off ask applies, in plain words (EN, TR)."""
    en: list[str] = []
    tr: list[str] = []
    if target.clip_position == "last":
        en.append("the last clip")
        tr.append("son klip")
    elif target.clip_position == "first":
        en.append("the first clip")
        tr.append("ilk klip")
    elif target.media_ref is not None:
        en.append("that clip")
        tr.append("o klip")
    if target.has_window:
        start = f"{target.from_s or 0:g}"
        if target.to_s is not None:
            en.append(f"{start}-{target.to_s:g} s")
            tr.append(f"{start}-{target.to_s:g}. saniyeler")
        else:
            en.append(f"from {start} s")
            tr.append(f"{start}. saniyeden itibaren")
    if target.lines:
        en.append(f'where "{target.lines[0]}" is said')
        tr.append(f'"{target.lines[0]}" denen yer')
    if not en:
        return "the whole video", "tüm video"
    return ", ".join(en), ", ".join(tr)


def _toggle_summary(ask: SoundToggleAsk) -> str:
    """ "Clip sound off except the last clip, where "Super excited!" is said", in the reply's
    language."""
    off = ask.state == "off"
    head_en = "Clip sound off" if off else "Clip sound on"
    head_tr = "Klip sesi kapalı" if off else "Klip sesi açık"
    where_en, where_tr = _toggle_where(ask.target)
    en = head_en if ask.target.whole_video else f"{head_en}: {where_en}"
    tr = head_tr if ask.target.whole_video else f"{head_tr}: {where_tr}"
    if ask.exceptions:
        parts = [_toggle_where(part) for part in ask.exceptions]
        en += " except " + "; ".join(p[0] for p in parts)
        tr += ", şunlar hariç: " + "; ".join(p[1] for p in parts)
    return say(en=en, tr=tr)


def _kept_line_rows(req: BriefRequirement, facts: PlanFacts) -> list[Mapping[str, Any]]:
    """The draft's length-plan records of clips this ask keeps for a named line (KRI-1028)."""
    snapshot = facts.media_snapshot if isinstance(facts.media_snapshot, Mapping) else {}
    plan = snapshot.get("length_plan")
    return [
        row
        for row in (plan.get("kept_lines") if isinstance(plan, Mapping) else None) or []
        if isinstance(row, Mapping) and row.get("requirement_id") == req.id and row.get("line")
    ]


def _kept_line_whole(summary: str, line: str, span: str | None = None) -> str:
    """``summary`` plus the named line the kept clip plays whole (KRI-1028)."""
    if span:
        return say(
            en=f'{summary}; "{line}" plays whole ({span} of that clip)',
            tr=f'{summary}; "{line}" baştan sona duyuluyor (o klibin {span} kısmı)',
        )
    return say(
        en=f'{summary}; "{line}" plays whole',
        tr=f'{summary}; "{line}" baştan sona duyulacak',
    )


def _toggle_seconds(windows: object) -> str:
    rows = [w for w in windows or [] if isinstance(w, (list, tuple)) and len(w) == 2]
    return ", ".join(f"{float(a):.1f}-{float(b):.1f} s" for a, b in rows)


def _toggle_missed(ask: SoundToggleAsk, row: Mapping[str, Any]) -> str:
    """Why the finished video doesn't (fully) play the sound as asked, plainly."""
    code = row.get("reason_code")
    line = row.get("line") or next((p.lines[0] for p in ask.exceptions if p.lines), None)
    line = line or (ask.target.lines[0] if ask.target.lines else None)
    if code == "line_not_found" and line:
        return say(
            en=f'I couldn\'t find "{line}" in that clip, so it plays like the rest',
            tr=f'"{line}" sözünü o klipte bulamadım, o yüzden diğerleri gibi çalıyor',
        )
    if code == "line_ambiguous" and line:
        return say(
            en=f'"{line}" is said in more than one clip; tell me which clip you mean',
            tr=f'"{line}" birden fazla klipte söyleniyor; hangi klibi kastettiğini söyle',
        )
    if code == "line_unknown":
        return say(
            en="I can't hear the speech in that clip yet",
            tr="O klipteki konuşmayı henüz duyamıyorum",
        )
    if code == "line_not_in_edit" and line:
        return say(
            en=f'The part where "{line}" is said isn\'t in the edit',
            tr=f'"{line}" denen kısım düzenlemede yok',
        )
    if code == "line_partly_in_edit" and line:
        return say(
            en=f'Only part of "{line}" is in the edit',
            tr=f'"{line}" sözünün sadece bir kısmı düzenlemede',
        )
    reasons = {
        "partial_unsupported": (
            "I can't turn off just part of a clip on your iPhone yet, so a clip that is only "
            "partly in that stretch keeps its sound",
            "iPhone'unda bir klibin sadece bir kısmının sesini henüz kapatamıyorum, o yüzden o "
            "aralıkta kısmen kalan klip sesini koruyor",
        ),
        "not_in_edit": (
            "That clip isn't in this edit",
            "O klip bu düzenlemede yok",
        ),
        "clip_mismatch": (
            "That clip isn't in that spot in this edit",
            "O klip bu düzenlemede o sırada değil",
        ),
        "window_past_end": (
            "Those seconds are past the end of the video",
            "O saniyeler videonun bitişinden sonra",
        ),
        "no_sound": (
            "That clip has no sound of its own",
            "O klibin kendi sesi yok",
        ),
        "contract_conflict": (
            "That would change which clips keep their sound in the edit you approved; approve "
            "the change and I'll rebuild it",
            "Bu, onayladığın düzenlemede hangi kliplerin sesini koruduğunu değiştirirdi; "
            "değişikliği onaylarsan yeniden hazırlarım",
        ),
        "render_refused": (
            "Your iPhone couldn't build that, so those clips keep their sound",
            "iPhone'un bunu hazırlayamadı, o yüzden o klipler seslerini koruyor",
        ),
        "overridden": (
            "A later request changed this",
            "Sonraki bir istek bunu değiştirdi",
        ),
    }
    en, tr = reasons.get(
        str(code),
        (
            "The finished video doesn't play the clip sound that way",
            "Bitmiş video klip sesini bu şekilde çalmıyor",
        ),
    )
    return say(en=en, tr=tr)


def _check_sound_toggle(
    req: BriefRequirement, ask: SoundToggleAsk, facts: PlanFacts
) -> RequirementReceipt:
    """Clip sound turned on / off where the creator said (KRI-942).

    A finished phone render is judged from its on/off record: the asks re-judged against the
    recipe the phone renders (`phone_clip_sound_toggle.rendered_toggle_receipt`, or the
    worker's own record). A draft the phone montage writer will render says what it will do,
    and "Couldn't" before approval for a clip that isn't there or a line no clip says. A
    voiceover or Talking edit says plainly that this works on a montage. Elsewhere (a plan
    record mid-render, an editor turn) nothing is claimed.
    """
    record = facts.clip_sound_toggles if isinstance(facts.clip_sound_toggles, Mapping) else None
    row = next(
        (
            r
            for r in (record or {}).get("rows") or []
            if isinstance(r, Mapping) and r.get("requirement_id") == req.id
        ),
        None,
    )
    if row is not None:
        status = str(row.get("status"))
        if status == "met":
            seconds = _toggle_seconds(
                row.get("on_windows") if ask.state == "off" else row.get("off_windows")
            )
            summary = _toggle_summary(ask)
            if ask.exceptions and seconds:
                summary = say(en=f"{summary} ({seconds})", tr=f"{summary} ({seconds})")
            kept = (
                row.get("line") if not any(p.lines for p in (ask.target, *ask.exceptions)) else None
            )
            if kept:
                span = _toggle_seconds([[row.get("line_start_s"), row.get("line_end_s")]])
                summary = _kept_line_whole(summary, str(kept), span)
            return _receipt(req, "met", summary)
        return _receipt(
            req, "partial" if status == "partial" else "not_possible", _toggle_missed(ask, row)
        )
    if facts.voiceover_edit or facts.phone_narrated or facts.phone_talking:
        return _receipt(req, "not_possible", _sound_toggle_needs_phone_montage())
    if facts.editor or facts.rendered_output or facts.rendered_variant:
        return _receipt(req, "partial", _NO_CHECKER)
    if facts.phone_montage:
        return _draft_sound_toggle(req, ask, facts)
    if facts.audio_strategy == "voiceover" or facts.edit_format in _SPEECH_FORMATS:
        return _receipt(req, "not_possible", _sound_toggle_needs_phone_montage())
    return _receipt(req, "partial", _NO_CHECKER)


def _draft_sound_toggle(
    req: BriefRequirement, ask: SoundToggleAsk, facts: PlanFacts
) -> RequirementReceipt:
    """What a phone montage draft will do with an on/off ask, before approval."""
    from app.pipeline.clip_sound_toggle import _media_match  # noqa: PLC0415
    from app.pipeline.phone_clip_sound import ground_trigger_line, video_media_ids  # noqa: PLC0415
    from app.services.phone_rollout import phone_clip_sound_windows_supported  # noqa: PLC0415

    snapshot = facts.media_snapshot
    videos = video_media_ids(snapshot) or list(facts.clip_ids)
    for part in (ask.target, *ask.exceptions):
        media = _media_match(part.media_ref, videos) if part.media_ref is not None else None
        if part.media_ref is not None and media is None:
            return _receipt(
                req, "not_possible", _toggle_missed(ask, {"reason_code": "not_in_edit"})
            )
        if not part.lines:
            continue
        pool = (
            [media]
            if media is not None
            else (
                [facts.last_clip_id]
                if part.clip_position == "last" and facts.last_clip_id
                else videos
            )
        )
        found = [m for m in pool if ground_trigger_line(part.lines, m, snapshot).status == "found"]
        if not found:
            statuses = {ground_trigger_line(part.lines, m, snapshot).status for m in pool}
            code = "line_unknown" if statuses <= {"unknown"} else "line_not_found"
            row = {"reason_code": code, "line": part.lines[0]}
            return _receipt(req, "not_possible", _toggle_missed(ask, row))
    silences_part = (ask.state == "off" and (ask.target.has_window or ask.target.lines)) or (
        ask.state == "on" and any(p.has_window or p.lines for p in ask.exceptions)
    )
    if silences_part and not phone_clip_sound_windows_supported():
        return _receipt(req, "partial", _toggle_missed(ask, {"reason_code": "partial_unsupported"}))
    kept = _kept_line_rows(req, facts)
    short = next((row for row in kept if row.get("held") is False), None)
    if short is not None:
        line = str(short["line"])
        return _receipt(
            req,
            "partial",
            say(
                en=f"\"{line}\" doesn't fit whole in the video's length, so part of it is cut",
                tr=f'"{line}" videonun süresine tam sığmıyor, bu yüzden bir kısmı kesiliyor',
            ),
        )
    if kept:
        return _receipt(req, "met", _kept_line_whole(_toggle_summary(ask), str(kept[0]["line"])))
    return _receipt(req, "met", _toggle_summary(ask))


def _bed_fix(facts: PlanFacts) -> tuple[str, str]:
    """How the creator lowers the footage bed on THIS edit (KRI-821), EN and TR.

    Names the editor's Sounds > Original audio row only when the live capability map said
    it is editable for this variant and app build, and the chat edit only on a phone
    Voiceover edit whose level Save recompiles (`PlanFacts.original_audio_control`);
    anything else (cloud render, unknown) states the plain limit, never a control the
    creator cannot use.
    """
    if facts.original_audio_control == "editor":
        return (
            "turn it down in the editor under Sounds > Original audio, or tell me to mute it",
            "editörde Sesler > Orijinal ses bölümünden kıs ya da bana kapatmamı söyle",
        )
    if facts.original_audio_control == "chat":
        return (
            "tell me to mute it and I'll take it out",
            "kapatmamı söylersen çıkarırım",
        )
    return (
        "this edit has no control to turn it down yet",
        "bu düzenlemede onu kısacak bir ayar henüz yok",
    )


def _check_voice_bed(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """ "Keep the crowd noise quiet under my voice" / "mute the crowd noise".

    A voiceover edit always mixes the footage's own sound as a bed under the voice (a
    fraction of full volume), so a "keep it low" ask is met by the mix itself and a "mute
    it" ask can only be partly met: the bed stays, quietly. That limit belongs to the
    format, so it never turns the draft into the simpler-version question. Any other
    audio strategy is judged by its own receipts and stays unchecked here.
    """
    level = facts.voiceover_bed_level
    voiceover = facts.audio_strategy == "voiceover"
    if level is None and not voiceover:
        return _receipt(req, "partial", _CANT_CHECK_MIX)
    if req.scope.startswith("clip:") and not (
        _wants_bed_muted(req) and not _wants_bed_under_voice(req)
    ):
        # KRI-842: the voiceover mix is one bed for every clip. A clip-scoped keep / under / low
        # ask is met only by a recorded `clip_sound` (`_check_narrated_clip_sound`), never by the
        # global level. A mute ask keeps its answer below: with no bed at all every clip is
        # silent under the voice, which is true of that clip too.
        return _receipt(
            req,
            "partial",
            _NO_CLIP_SOUND_UNDER_VOICE
            if level is not None and level <= 0
            else _EVERY_CLIP_SOUND_UNDER_VOICE,
        )
    pct = None if level is None else round(level * 100)
    if _wants_bed_muted(req) and not _wants_bed_under_voice(req):
        if level is not None and level <= 0:
            return _receipt(req, "met", None)
        if level is None and renders_bed_mute(req):
            # KRI-821: an approved typed mute renders the bed at 0 on every voiceover lane
            # (`footage_mute_ask.approved_bed_level`; a montage voiceover fully ducks it).
            # A wording-only (pre-v77) mute renders nothing, so it is never claimed here.
            return _receipt(
                req,
                "met",
                say(
                    en="Your footage sound is muted under your voice",
                    tr="Çekim sesi sesinin altında kapalı",
                ),
            )
        fix_en, fix_tr = _bed_fix(facts)
        if level is not None and level >= 1.0:
            return _receipt(
                req,
                "partial",
                say(
                    en=f"{_BED_STILL_PLAYS} at full volume alongside your voice; {fix_en}",
                    tr=f"{_REASON_TR[_BED_STILL_PLAYS]}, sesinle aynı seviyede; {fix_tr}",
                ),
            )
        about_en = "" if pct is None else f" (about {pct}%)"
        about_tr = "" if pct is None else f" (yaklaşık %{pct})"
        return _receipt(
            req,
            "partial",
            say(
                en=f"{_BED_STILL_PLAYS} softly under your voice{about_en}; {fix_en}",
                tr=f"{_REASON_TR[_BED_STILL_PLAYS]}, sesinin altında kısık{about_tr}; {fix_tr}",
            ),
        )
    if level is None:
        return _receipt(
            req,
            "met",
            say(
                en="Your footage sound plays under your voice",
                tr="Çekim sesi sesinin altında çalıyor",
            ),
        )
    if level >= 1.0:
        return _receipt(
            req,
            "partial",
            say(
                en="The footage sound plays at full volume alongside your voice",
                tr="Çekim sesi sesinle aynı seviyede, tam sesle çalıyor",
            ),
        )
    if level >= _BED_QUIET_BELOW:
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"The footage sound plays at about {pct}% alongside your voice, "
                    "not far under it"
                ),
                tr=(
                    f"Çekim sesi sesinin yanında yaklaşık %{pct} seviyesinde çalıyor, "
                    "pek altında değil"
                ),
            ),
        )
    return _receipt(
        req,
        "met",
        say(
            en=f"Your footage sound sits under your voice at about {pct}% volume",
            tr=f"Çekim sesi, sesinin altında yaklaşık %{pct} seviyesinde",
        ),
    )


_STYLE_FIELD_NAMES = {
    "entrance": ("entrance animation", "giriş animasyonu"),
    "alignment": ("alignment", "hizalama"),
    "text_case": ("letter case", "harf biçimi"),
    "font_family": ("font", "yazı tipi"),
    "color": ("color", "renk"),
}


def _style_intent(req: BriefRequirement) -> dict[str, Any] | None:
    """The well-formed structured style intent of a style requirement, else None (KRI-543)."""
    if req.kind != "style":
        return None
    return normalize_style_intent(req.facts.get("style_intent"))


_TARGET_KIND = {"title": "title", "labels": "label"}


def _row_value(row: TextStyleRow, field_name: str) -> str | None:
    return getattr(row, field_name, None)


def _check_style(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    # A changed text lane proves a mutation, not that the requested fields, targets, or
    # animation relationships were satisfied (KRI-524). A value is compared only when the
    # ask resolves to a typed value (`derive_style_ask`: the structured intent, the facts the
    # extractor filled, or the creator's own unambiguous words), and only against rows whose
    # saved value is known.
    ask = derive_style_ask(req)
    if ask is None or facts.text_styles is None:
        return _receipt(req, "partial", _NO_CHECKER)
    rows = [
        row
        for row in facts.text_styles
        if ask.target in (None, "all_text") or row.kind == _TARGET_KIND[ask.target]
    ]
    if ask.target is None and facts.anaphora_rows is not None:
        # "…to all of them" on an editor turn means the texts this turn touched.
        touched = set(facts.anaphora_rows)
        rows = [row for row in rows if row.id in touched]
    verdicts = [
        value_matches(name, value, _row_value(row, name))
        for row in rows
        for name, value in ask.wanted.items()
    ]
    if not rows or None in verdicts:
        return _receipt(req, "partial", _CANT_CHECK_STYLE)
    off = [
        row
        for row in rows
        if any(value_matches(n, v, _row_value(row, n)) is False for n, v in ask.wanted.items())
    ]
    if not off:
        return _receipt(req, "met", None)
    if ask.target is None:
        # "…to all of them" may mean a subset: a mismatch is not evidence of a miss.
        return _receipt(req, "partial", _CANT_CHECK_STYLE)
    names_en = ", ".join(_STYLE_FIELD_NAMES[f][0] for f in ask.wanted)
    names_tr = ", ".join(_STYLE_FIELD_NAMES[f][1] for f in ask.wanted)
    return _receipt(
        req,
        "partial",
        say(
            en=f"{len(off)} of {len(rows)} texts don't have the requested {names_en} yet",
            tr=f"{len(rows)} metinden {len(off)} tanesinde istenen {names_tr} henüz yok",
        ),
    )


def _row_at(row: TextStyleRow, x: float, y: float) -> bool | None:
    """Whether a label row sits at an anchor; None when the row does not say where it is."""
    if row.position == "custom" and row.x_frac is not None and row.y_frac is not None:
        return abs(row.x_frac - x) <= 0.06 and abs(row.y_frac - y) <= 0.08
    named = {0.12: "top", 0.5: "middle", 0.78: "bottom"}.get(y)
    if row.position in ("top", "middle", "bottom") and named is not None:
        return row.position == named and abs(x - 0.5) <= 0.06
    return None


def _look_rows(req: BriefRequirement, facts: PlanFacts) -> list[TextStyleRow]:
    rows = list(facts.text_styles or ())
    if req.scope == "title":
        return [row for row in rows if row.kind == "title"]
    if req.scope == "per_clip":
        return [row for row in rows if row.kind == "label"]
    if req.scope.startswith("clip:"):
        target = _scope_clip_id(req, [row.clip_id for row in rows if row.clip_id])
        return [row for row in rows if row.kind == "label" and target and row.clip_id == target]
    if req.literal:
        wanted = _fold(req.literal)
        return [row for row in rows if _contains_text(row.text, wanted)]
    return rows


def asks_text_animation(req: BriefRequirement) -> bool:
    """A live text ask for animation, named or not ("with animations"), with the switch on."""
    from app.config import settings  # noqa: PLC0415

    return bool(settings.text_animation_ask_enabled and animation_ask(req) is not None)


_ANIMATION_PLANNED = "the text will animate in as it appears; I'll check it in the finished video"
_ANIMATION_UNAVAILABLE = (
    "animated text isn't available on this phone build yet, so the text will appear "
    "without animation"
)
_REASON_TR[_ANIMATION_PLANNED] = (
    "yazı belirirken animasyonlu girecek; bitmiş videoda kontrol edeceğim"
)
_REASON_TR[_ANIMATION_UNAVAILABLE] = (
    "animasyonlu yazı bu telefon sürümünde henüz yok, bu yüzden yazı animasyonsuz görünecek"
)


def _animation_plan_receipt(req: BriefRequirement) -> RequirementReceipt:
    """What a draft can honestly say about an animation ask before the text is drawn.

    The text lane does not exist yet (the creation text pass lays it out at render), so this
    states what the render WILL do, or that this phone build cannot, and leaves the verdict to
    the finished video. ``unchecked`` on purpose: it is an answer for the creator to read, never
    a block on approval, and never a claim the animation is already on screen.
    """
    from app.kria.animation_ask import text_entrance_form  # noqa: PLC0415

    ask = animation_ask(req)
    named = ask.edge if ask is not None and ask.named else None
    if text_entrance_form() is None:
        return _receipt(req, "not_possible", _ANIMATION_UNAVAILABLE, verification="unchecked")
    if named:
        label = TEXT_ANIMATION_VOCAB[named].label  # type: ignore[index]
        reason = say(
            en=f"the text will play the {label} entrance; I'll check it in the finished video",
            tr=f"yazı {label} girişiyle oynayacak; bitmiş videoda kontrol edeceğim",
        )
    else:
        reason = _ANIMATION_PLANNED
    return _receipt(req, "met", reason, verification="unchecked")


def _check_text_animation(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A whole-video "with animations" ask: judged on the finished text lane, else planned."""
    if facts.text_styles is None:
        if facts.phone_text or facts.phone_montage:
            return _animation_plan_receipt(req)
        return _receipt(req, "partial", _NO_CHECKER)
    judged = check_text_look(req, facts)
    return judged if judged is not None else _receipt(req, "partial", _NO_CHECKER)


def _research_with_animation(
    req: BriefRequirement, facts: PlanFacts, researched: RequirementReceipt
) -> RequirementReceipt:
    """One text ask that carries researched facts AND "with animations" (the Assos shape).

    The research verdict alone must not stand for the animation nobody checked: the receipt
    keeps the research met but is only ``checked`` when the finished text lane also shows an
    entrance. Before that it is ``unchecked`` and says what the render will do (or that this
    phone cannot); a miss or decline on the animation is reported as such. Report-only.
    """
    animation = _check_text_animation(req, facts)
    if animation.status == "met" and animation.verification == "checked":
        return researched
    if animation.status in ("met", "partial"):
        return _receipt(
            req,
            "met",
            animation.reason if animation.status == "met" else None,
            verification="unchecked",
            stage=researched.stage,
        )
    return animation


def check_text_look(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """Judge a TEXT requirement's look facts (title animation, label corner, font, colour).

    None when there is nothing to compare or a row does not say, so a record or draft that
    has no text lane never judges (and never blocks a render) on a look it cannot see.
    """
    look = text_look_facts(req)
    if asks_text_animation(req):
        # An unnamed ask has no fact to read; the typed ask carries the edge or the sentinel.
        look = {**look, "animation": animation_ask(req).value}  # type: ignore[union-attr]
    if not look or facts.text_styles is None:
        return None
    rows = _look_rows(req, facts)
    if not rows:
        return None
    misses: list[str] = []
    judged = 0
    undecided = False  # a fact we cannot read never hides a miss we already found
    for key, value in look.items():
        if key in ("size_px", "heading_level", "stack_id"):
            haves = [getattr(row, key) for row in rows]
            label = ("text hierarchy", "yazı hiyerarşisi")
            fails = [h != value for h in haves]
        elif key in ("smaller_than", "below_text"):
            sources = [row for row in facts.text_styles if _fold(row.text) == _fold(str(value))]
            if len(sources) != 1 or len(rows) != 1:
                return _receipt(req, "partial", _NO_CHECKER)
            source, target = sources[0], rows[0]
            if key == "smaller_than":
                if source.size_px is None or target.size_px is None:
                    return _receipt(req, "partial", _NO_CHECKER)
                haves = [target.size_px < source.size_px]
                label = ("smaller text", "daha küçük yazı")
            else:
                if (
                    source.y_frac is None
                    or target.y_frac is None
                    or source.layout_row is None
                    or target.layout_row is None
                ):
                    return _receipt(req, "partial", _NO_CHECKER)
                from app.pipeline.text_stack_layout import (  # noqa: PLC0415
                    measure_text_block,
                    stack_gap,
                )

                source_height = measure_text_block(source.layout_row, width=source.canvas_width)[
                    "height"
                ]
                target_height = measure_text_block(target.layout_row, width=target.canvas_width)[
                    "height"
                ]
                if source.canvas_height != target.canvas_height:
                    return _receipt(req, "partial", _NO_CHECKER)
                gap = (target.y_frac - source.y_frac) * source.canvas_height - (
                    source_height + target_height
                ) / 2
                expected = stack_gap(source.layout_row, target.layout_row)
                same_axis = (
                    source.alignment == target.alignment
                    and source.x_frac is not None
                    and target.x_frac is not None
                    and abs(source.x_frac - target.x_frac) <= 0.005
                )
                haves = [same_axis and abs(gap - expected) <= 1]
                label = ("text grouping", "yazı grubu")
            fails = [not h for h in haves]
        elif key == "animation":
            wanted = normalize_title_animation(value)
            haves = [row.entrance for row in rows]
            label = ("animation", "animasyon")
            fails = [h != wanted for h in haves] if wanted else []
            if not wanted and asks_text_animation(req):
                # "Animated text" with no edge named: any entrance is the ask.
                fails = [h == "none" for h in haves]
        elif key == "position":
            spot = normalize_label_position(value)
            if spot is None:
                continue
            x, y, _align = LABEL_ANCHORS[spot]
            anchors = []
            for row in rows:
                group = [
                    candidate
                    for candidate in facts.text_styles
                    if row.stack_id is not None and candidate.stack_id == row.stack_id
                ]
                if group and all(candidate.y_frac is not None for candidate in group):
                    row = (min if y < 0.5 else max)(group, key=lambda candidate: candidate.y_frac)
                anchors.append(row)
            haves = [_row_at(row, x, y) for row in anchors]
            label = ("position", "konum")
            fails = [h is False for h in haves]
        elif key in ("font_family", "text_color"):
            ask = derive_style_ask(
                BriefRequirement(
                    id=req.id, kind="style", scope="global", description="", facts={key: value}
                )
            )
            if ask is None:
                continue
            name, wanted_value = next(iter(ask.wanted.items()))
            verdicts = [value_matches(name, wanted_value, _row_value(row, name)) for row in rows]
            haves = verdicts
            label = ("font", "yazı tipi") if key == "font_family" else ("colour", "renk")
            fails = [v is False for v in verdicts]
        else:
            continue
        if not fails or any(h is None for h in haves):
            undecided = True
            continue
        judged += 1
        if any(fails):
            misses.append(
                say(
                    en=f"{sum(fails)} of {len(rows)} don't have the requested {label[0]}",
                    tr=f"{len(rows)} yazıdan {sum(fails)} tanesinde istenen {label[1]} yok",
                )
            )
    if misses:
        return _receipt(req, "partial", "; ".join(misses))
    if not judged or undecided:
        return None
    return _receipt(req, "met", None)


_STATUS_RANK = {"met": 0, "partial": 1, "not_possible": 2}


def combine_receipts(first: RequirementReceipt, second: RequirementReceipt) -> RequirementReceipt:
    """The weaker of two verdicts on one requirement, with both reasons (KRI-558)."""
    worst = max((first, second), key=lambda r: _STATUS_RANK[r.status])
    reasons = [r.reason for r in (first, second) if r.reason and r.status != "met"]
    return worst.model_copy(update={"reason": "; ".join(dict.fromkeys(reasons))[:300] or None})


def check_text_shape(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """Judge a curved-text ask (KRI-1029, ``text_shape_ask``); None when nothing says.

    The phone montage plan records why a curved text stays straight (``facts.text_shapes``):
    that reason wins, on the draft and on the finished video. Otherwise the saved rows decide
    (a finished video or an editor turn: every row with the words must carry a ``bend``),
    and a plan that bent the text is a "Done" on the draft. Matched by the requirement's
    words; without a literal, a title-scope ask reads the title and a per-clip ask the labels.
    """
    if text_shape(req) != CURVED:
        return None
    wanted = _fold(req.literal) if req.literal else None
    role = {"title": "title", "per_clip": "label"}.get(req.scope)
    planned = [
        reason
        for text, row_role, reason in facts.text_shapes or ()
        if (text == wanted if wanted is not None else row_role == role)
    ]
    if "animation" in planned:
        return _receipt(req, "not_possible", _TYPEWRITER_STAYS_STRAIGHT)
    if any(reason is not None for reason in planned):
        return _receipt(req, "not_possible", _TEXT_STAYS_STRAIGHT)
    rows = [
        row
        for row in facts.text_styles or ()
        if (_fold(row.text) == wanted if wanted is not None else row in _look_rows(req, facts))
    ]
    if rows:
        if all(row.bend for row in rows):
            return _receipt(req, "met", _TEXT_CURVED)
        return _receipt(req, "not_possible", _TEXT_CAME_OUT_STRAIGHT)
    if planned:
        return _receipt(req, "met", _TEXT_CURVED)
    return None


def _with_text_look(
    req: BriefRequirement, facts: PlanFacts, base: RequirementReceipt
) -> RequirementReceipt:
    """Add the look check to a text requirement's presence check (KRI-558)."""
    base = _with_title_face(req, facts, base)
    base = _with_spoken_text_face(req, facts, base)
    if (text_look_facts(req) or asks_text_animation(req)) and base.status == "met":
        look = check_text_look(req, facts)
        # The words are there; whether they look as asked is not visible here (a record or
        # a draft). A presence check alone never certifies the look (KRI-524).
        base = (
            _receipt(req, "partial", _NO_CHECKER) if look is None else combine_receipts(base, look)
        )
    if text_shape(req) == CURVED and base.status == "met":
        # KRI-1029: "curved title": the same rule, the words alone never certify the curve.
        shape = check_text_shape(req, facts)
        if shape is None:
            base = _receipt(req, "partial", _NO_CHECKER)
        elif shape.status == "met" and not base.reason:
            base = base.model_copy(update={"reason": shape.reason})  # "Done: … (curved)"
        else:
            base = combine_receipts(base, shape)
    return _with_spoken_text_speaker(req, facts, base)


def text_words_met(req: BriefRequirement, facts: PlanFacts) -> bool:
    """KRI-1113: ``req``'s words, its look set aside, are checked and met on ``facts``.

    `_with_text_look` keeps a text ask unchecked while its look (an entrance, a corner, a font,
    a colour) can't be read, and a strategy draft has no text lane to read it from. This tells
    "the words are there; only the finished video shows the look" from "the words are not".
    """
    look = text_look_facts(req)
    words = req.model_copy(update={"facts": {k: v for k, v in req.facts.items() if k not in look}})
    return any(
        receipt.status == "met" and receipt.verification == "checked"
        for receipt in build_receipts([words], facts, include_unchecked=True)
    )


def _check_clip_lengths(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """ "Make all clips 1 second except the first and last": judged on the clip lengths."""
    ask = clip_length_ask(req)
    durations = facts.clip_output_durations
    if ask is None or not durations:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    seconds, skip_first, skip_last = ask
    rows = list(durations)
    if skip_first:
        rows = rows[1:]
    if skip_last:
        rows = rows[:-1]
    if not rows:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    tolerance = max(0.1, 0.1 * seconds)
    off = [length for length in rows if abs(length - seconds) > tolerance]
    if not off:
        return _receipt(req, "met", None)
    shown = ", ".join(f"{length:.1f}s" for length in off[:3])
    return _receipt(
        req,
        "partial",
        say(
            en=f"{len(off)} of {len(rows)} clips aren't {seconds:g}s ({shown})",
            tr=f"{len(rows)} klipten {len(off)} tanesi {seconds:g} sn değil ({shown})",
        ),
    )


def describe_text_look(req: BriefRequirement, facts: PlanFacts) -> str | None:
    """What the video's texts actually use, for an ask no value check could decide."""
    ask = derive_style_ask(req)
    if ask is None or not facts.text_styles:
        return None
    rows = [
        row
        for row in facts.text_styles
        if ask.target in (None, "all_text") or row.kind == _TARGET_KIND[ask.target]
    ]
    parts = []
    for name in ask.wanted:
        counts: dict[str, int] = {}
        for row in rows:
            value = _row_value(row, name)
            if value:
                counts[value] = counts.get(value, 0) + 1
        if counts:
            shown = ", ".join(f"{v} ×{n}" if n > 1 else v for v, n in counts.items())
            field_name = say(en=_STYLE_FIELD_NAMES[name][0], tr=_STYLE_FIELD_NAMES[name][1])
            parts.append(f"{field_name}: {shown}")
    return "; ".join(parts)[:300] or None


# KRI-547: "I filmed it sideways but want a vertical video, keep my face in frame".
# The phone Talking worker reads the same words off the approved brief
# (`app.kria.speaker_framing_ask`) and records how it framed the speaker on the
# variant (`phone_speaker_framing`); these judge the ask from that receipt. The copy names
# "the speaker", never "you", because the creator may be filming someone else (KRI-1008).
_FRAMING_RECEIPT_MODES = frozenset({"face_fill", "letterbox", "centre_fill"})
# Below this a face out of view is a blink, not footage without it; KRI-1020 face_fill keeps
# the picture centred while the speaker is away or turned (`faces.absent_s` counts both).
_FRAMING_ABSENT_NOTE_S = 1.0


def _wants_speaker_framing(req: BriefRequirement) -> bool:
    """A style ask for a vertical / full-screen / face-in-frame video, and nothing a
    more specific checker owns (captions, pop-ins, the closing shot, speech cleanup)."""
    from app.kria.speaker_framing_ask import requirement_asks_vertical_framing  # noqa: PLC0415

    return requirement_asks_vertical_framing(req) and not (
        _wants_captions(req) or _wants_beats(req) or _wants_closing(req) or _wants_cleanup(req)
    )


def _speaker_framing_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """``speaker_framing`` / ``_reason`` / ``_face_share`` / ``_absent_s`` off a variant's
    framing receipt, reading both the v1 (frame counts) and v2 (presence, absent seconds) forms."""
    receipt = variant.get("speaker_framing")
    if not isinstance(receipt, Mapping) or receipt.get("mode") not in _FRAMING_RECEIPT_MODES:
        return {}
    reason = receipt.get("reason")
    faces = receipt.get("faces")
    faces = faces if isinstance(faces, Mapping) else {}
    presence = faces.get("presence")
    used = faces.get("used")
    decoded = faces.get("decoded")
    absent = faces.get("absent_s")
    share: float | None = None
    if (
        isinstance(presence, (int, float))
        and not isinstance(presence, bool)
        and math.isfinite(presence)
        and 0 <= presence <= 1
    ):
        share = float(presence)
    elif (
        isinstance(used, int)
        and not isinstance(used, bool)
        and isinstance(decoded, int)
        and not isinstance(decoded, bool)
        and decoded > 0
        and 0 <= used <= decoded
    ):
        share = used / decoded
    absent_s: float | None = None
    if (
        isinstance(absent, (int, float))
        and not isinstance(absent, bool)
        and math.isfinite(absent)
        and absent >= 0
    ):
        absent_s = float(absent)
    return {
        "speaker_framing": str(receipt["mode"]),
        "speaker_framing_reason": str(reason) if isinstance(reason, str) and reason else None,
        "speaker_framing_face_share": share,
        "speaker_framing_absent_s": absent_s,
    }


def _framing_fallback_reason(
    reason: str | None, *, cropped: bool, face_share: float | None = None
) -> str:
    """Why the face crop was not used, and what the creator sees instead."""
    shown = (
        say(
            en="so the video is cropped to the middle and the speaker's face can leave the frame",
            tr="o yüzden video ortadan kırpıldı ve konuşan kişinin yüzü kadrajdan çıkabilir",
        )
        if cropped
        else say(
            en="so the whole frame shows with black bars above and below",
            tr="o yüzden görüntünün tamamı üstte ve altta siyah bantlarla görünüyor",
        )
    )
    if reason == "face_moves_too_much":
        why = say(
            en="The speaker's face moves too far for one vertical crop to hold it",
            tr="Konuşan kişinin yüzü tek bir dikey kadraja sığmayacak kadar hareket ediyor",
        )
    elif reason == "face_under_captions":
        why = say(
            en="A vertical crop would put the captions over the speaker's face",
            tr="Dikey kırpma altyazıları konuşan kişinin yüzünün üstüne getirirdi",
        )
    elif reason == "creator_chose_bars":
        return say(
            en="You chose black bars in the editor, so the whole frame shows",
            tr="Editörde siyah bantları seçtin, o yüzden görüntünün tamamı görünüyor",
        )
    elif reason == "creator_chose_crop":
        return say(
            en=(
                "You chose crop in the editor, so the video is cropped to the middle "
                "and the speaker's face can leave the frame"
            ),
            tr=(
                "Editörde kırpmayı seçtin, o yüzden video ortadan kırpıldı ve konuşan "
                "kişinin yüzü kadrajdan çıkabilir"
            ),
        )
    elif reason == "face_unconfirmed":
        why = say(
            en="I couldn't check where the speaker's face is in the clip",
            tr="Klipte konuşan kişinin yüzünün nerede olduğunu kontrol edemedim",
        )
    elif reason == "faces_too_far_apart":
        why = say(
            en="Two people on camera are too far apart for one vertical crop to hold both",
            tr="Kameradaki iki kişi tek bir dikey kadraja sığmayacak kadar birbirinden uzak",
        )
    elif reason == "no_face" and face_share is not None and face_share > 0:
        why = say(
            en=(
                "I could only find the speaker's face in part of the clip, not enough "
                "to fit one vertical crop around it"
            ),
            tr=(
                "Konuşan kişinin yüzünü klibin yalnızca bir kısmında bulabildim, tek bir "
                "dikey kadrajı ona göre ayarlamaya yetmedi"
            ),
        )
    elif reason == "no_face" and face_share == 0:
        why = say(
            en="I couldn't find the speaker's face anywhere in the clip",
            tr="Klipte konuşan kişinin yüzünü hiç bulamadım",
        )
    elif reason == "no_face":
        why = say(
            en="I couldn't find the speaker's face reliably in the clip",
            tr="Klipte konuşan kişinin yüzünü güvenle bulamadım",
        )
    else:
        why = say(
            en="I couldn't keep the speaker's face in one vertical crop",
            tr="Konuşan kişinin yüzünü tek bir dikey kadrajda tutamadım",
        )
    return f"{why}, {shown}"


def _check_speaker_framing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    mode = facts.speaker_framing
    if mode is None:
        reason = (
            say(
                en="This video didn't record how it framed the speaker, so I can't check it.",
                tr=(
                    "Bu video konuşan kişiyi nasıl kadrajladığını kaydetmedi, "
                    "o yüzden kontrol edemiyorum."
                ),
            )
            if facts.rendered_variant
            else say(
                en="I'll check the framing on the finished video.",
                tr="Kadrajı video hazır olunca kontrol edeceğim.",
            )
        )
        return _receipt(req, "partial", reason, verification="unchecked")
    if mode == "face_fill":
        absent = facts.speaker_framing_absent_s
        if absent is not None and absent >= _FRAMING_ABSENT_NOTE_S:
            return _receipt(
                req,
                "partial",
                say(
                    en=(
                        "The video fills the vertical frame and follows the speaker's face, "
                        f"but their face isn't in view for about {absent:.0f} s of the clip"
                    ),
                    tr=(
                        "Video dikey ekranı dolduruyor ve konuşan kişinin yüzünü takip ediyor, "
                        f"ama klibin yaklaşık {absent:.0f} sn'sinde konuşan kişinin yüzü görünmüyor"
                    ),
                ),
            )
        return _receipt(
            req,
            "met",
            say(
                en=(
                    "The video fills the vertical frame and the speaker's face stays in it "
                    "throughout"
                ),
                tr=(
                    "Video dikey ekranı dolduruyor ve konuşan kişinin yüzü baştan sona "
                    "kadrajda kalıyor"
                ),
            ),
        )
    if facts.speaker_framing_reason == "landscape_canvas":
        # The creator picked a landscape video, so the vertical/face framing they also asked
        # for had nothing to do; say so rather than "met" or a face-search failure.
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    "You chose a landscape video, so there was no vertical framing to do "
                    "and your clip shows in the full wide frame"
                ),
                tr=(
                    "Yatay video seçtin, o yüzden yapılacak bir dikey kadraj yoktu "
                    "ve klibin geniş kadrajda tam görünüyor"
                ),
            ),
        )
    if facts.speaker_framing_reason == "not_landscape":
        return _receipt(
            req,
            "met",
            say(
                en="Your clip is already vertical, so it fills the frame",
                tr="Klibin zaten dikey, o yüzden ekranı dolduruyor",
            ),
        )
    return _receipt(
        req,
        "partial",
        _framing_fallback_reason(
            facts.speaker_framing_reason,
            cropped=mode == "centre_fill",
            face_share=facts.speaker_framing_face_share,
        ),
    )


# KRI-628: "start the video at '...'" (and KRI-790: "...and end right after '...'") is cut by
# the phone Talking worker (`app.pipeline.phone_opening_cut`), which records where the video
# starts (`variant["opening_cut"]`) and ends (`variant["end_cut"]`).
def _wants_opening_cut(req: BriefRequirement) -> bool:
    from app.kria.opening_cut_ask import wants_take_window  # noqa: PLC0415

    return wants_take_window(req)


def _take_window_lines(req: BriefRequirement) -> tuple[str | None, str | None]:
    from app.kria.opening_cut_ask import (  # noqa: PLC0415
        requirement_end_quote,
        requirement_opening_quote,
    )

    return requirement_opening_quote(req), requirement_end_quote(req)


def _opening_cut_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("opening_cut", "end_cut"):
        receipt = variant.get(key)
        if not isinstance(receipt, Mapping) or receipt.get("status") not in {
            "applied",
            "not_applied",
        }:
            continue
        reason = receipt.get("reason")
        quote = receipt.get("quote")
        out[key] = str(receipt["status"])
        out[f"{key}_reason"] = str(reason) if isinstance(reason, str) and reason else None
        out[f"{key}_quote"] = str(quote) if isinstance(quote, str) and quote else None
    return out


def _clock(seconds: float) -> str:
    whole = int(round(float(seconds)))
    return f"{whole // 60}:{whole % 60:02d}"


def _take_window_plan(req: BriefRequirement, estimate: TakeWindowEstimate | None) -> str:
    """KRI-790: what a phone Talking draft will do with this ask's quoted start / end lines,
    naming each (and where it lands in the take when the draft's timed speech holds it)."""
    start, end = _take_window_lines(req)

    def _at(line: str | None, side: str) -> str:
        quoted = f"\u201c{line}\u201d"
        if estimate is None:
            return quoted
        seconds = estimate.start_s if side == "start" else estimate.end_s
        same = (estimate.start_quote if side == "start" else estimate.end_quote) == line
        return f"{quoted} ({_clock(seconds)})" if same and seconds is not None else quoted

    if start and end:
        return say(
            en=f"{_TALKING_WINDOW_PLAN}from {_at(start, 'start')} to right after "
            f"{_at(end, 'end')}, if I find those lines",
            tr=f"{_REASON_TR[_TALKING_WINDOW_PLAN]}{_at(start, 'start')} cümlesinden "
            f"{_at(end, 'end')} cümlesinin hemen sonrasına kadar olan kısmını korurum, bu "
            "cümleleri bulursam",
        )
    if start:
        return say(
            en=f"{_TALKING_WINDOW_PLAN}from {_at(start, 'start')} on, if I find that line; if "
            "not, it opens at the start",
            tr=f"{_REASON_TR[_TALKING_WINDOW_PLAN]}{_at(start, 'start')} cümlesinden sonrasını "
            "korurum, o cümleyi bulursam; bulamazsam video baştan açılır",
        )
    return say(
        en=f"{_TALKING_WINDOW_PLAN}up to right after {_at(end, 'end')}, if I find that line; if "
        "not, it runs to the end",
        tr=f"{_REASON_TR[_TALKING_WINDOW_PLAN]}{_at(end, 'end')} cümlesinin hemen sonrasına "
        "kadar olan kısmını korurum, o cümleyi bulursam; bulamazsam video sonuna kadar sürer",
    )


def _spoken_text_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    receipt = variant.get("spoken_text_placement")
    rows = receipt.get("rows") if isinstance(receipt, Mapping) else None
    if not isinstance(rows, list):
        return {}
    facts: list[SpokenTextFact] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("requirement_id"), str):
            continue
        triggers = row.get("triggers")
        first = triggers[0] if isinstance(triggers, list) and triggers else ""
        facts.append(
            SpokenTextFact(
                requirement_id=row["requirement_id"],
                status=str(row.get("status") or "error"),
                text=str(row.get("text") or ""),
                trigger=str(row.get("trigger") or first or ""),
                face_collision=row.get("face_collision") is True,
                pinned=isinstance(row.get("pin"), Mapping),
            )
        )
    return {"spoken_texts": tuple(facts)}


def _spoken_text_fact(facts: PlanFacts, req: BriefRequirement) -> SpokenTextFact | None:
    return next((f for f in facts.spoken_texts or () if f.requirement_id == req.id), None)


def _spoken_text_miss(req: BriefRequirement, fact: SpokenTextFact) -> RequirementReceipt:
    """KRI-707: why a rendered phone Talking edit shows no text for this ask."""
    text = f"“{fact.text or req.literal or ''}”"
    word = f"“{fact.trigger}”" if fact.trigger else say(en="that word", tr="o kelimeyi")
    if fact.status == "ambiguous":
        return _receipt(
            req,
            "partial",
            say(
                en=f"I heard {word} more than once. Choose which occurrence should show {text}.",
                tr=(
                    f"{word} birden fazla kez geçiyor. {text} yazısının hangi tekrarda "
                    "görüneceğini seç."
                ),
            ),
        )
    if fact.status == "not_heard":
        reason = say(
            en=f"I didn't hear you say {word} in your take, so {text} isn't shown",
            tr=f"Çekiminde {word} dediğini duymadım, o yüzden {text} yazısı çıkmadı",
        )
    elif fact.status == "no_time":
        reason = say(
            en=f"You say {word} right at the end, so there was no time to show {text}",
            tr=f"{word} en sonda söylüyorsun, o yüzden {text} yazısına yer kalmadı",
        )
    elif fact.status == "pinned_occurrence_missing":
        # KRI-724: the creator's pick is never swapped for another time the word is said.
        reason = say(
            en=f"I couldn't find the {word} you picked in your take, so {text} isn't shown",
            tr=f"Seçtiğin {word} anını çekiminde bulamadım, o yüzden {text} yazısı çıkmadı",
        )
    elif fact.status == "pinned_occurrence_cut":
        reason = say(
            en=f"The {word} you picked was cut out of your video, so {text} isn't shown",
            tr=f"Seçtiğin {word} anı videodan kesildi, o yüzden {text} yazısı çıkmadı",
        )
    else:
        reason = say(
            en=f"I couldn't place {text} on this video",
            tr=f"{text} yazısını bu videoya yerleştiremedim",
        )
    return _receipt(req, "partial", reason)


def _with_spoken_text_face(
    req: BriefRequirement, facts: PlanFacts, base: RequirementReceipt
) -> RequirementReceipt:
    """KRI-707: text never covers a face, so a text on a spoken word the render could not
    move off the face is not "met" (the title's rule, KRI-706)."""
    fact = _spoken_text_fact(facts, req) if base.status == "met" else None
    if fact is None or fact.status != "placed" or not fact.face_collision:
        return base
    text = f"“{fact.text}”"
    return _receipt(
        req,
        "partial",
        say(
            en=f"{text} lands on a face; I couldn't find a spot clear of it",
            tr=f"{text} bir yüzün üstüne denk geliyor; yüzü açıkta bırakan bir yer bulamadım",
        ),
    )


def _pinned_spoken_text_plan(ask: Any, facts: PlanFacts) -> str | None:
    """KRI-724: the draft's promise for a text the creator pinned to one spoken occurrence."""
    from app.kria.spoken_text_ask import with_answered_pins  # noqa: PLC0415

    if not facts.speech_occurrence_answers:
        return None
    [pinned] = with_answered_pins([ask], facts.speech_occurrence_answers) or [None]
    pin = getattr(pinned, "pin", None)
    if pin is None or pin.approx_s is None:
        return None
    whole = int(pin.approx_s + 0.5)
    clock = f"{whole // 60}:{whole % 60:02d}"
    word = f"“{ask.triggers[0]}”"
    return say(
        en=f"{_TALKING_SPOKEN_TEXT_PINNED}{word} you picked, at {clock} in your take; if I "
        "can't find that one, it won't appear",
        tr=f"{_REASON_TR[_TALKING_SPOKEN_TEXT_PINNED]}{word} anında, çekiminin {clock} "
        "noktasında gösteririm; o anı bulamazsam yazı çıkmaz",
    )


def _with_spoken_text_speaker(
    req: BriefRequirement, facts: PlanFacts, base: RequirementReceipt
) -> RequirementReceipt:
    """KRI-791: "the first time SHE says it" was shown the first time anyone said the word,
    so the finished video says so (the take's words don't say who speaks)."""
    fact = _spoken_text_fact(facts, req)
    if fact is None or fact.status != "placed" or fact.pinned:
        # KRI-724: placed on the occurrence the creator picked: that IS who says it.
        return base
    from app.kria.spoken_text_ask import requirement_spoken_text  # noqa: PLC0415

    ask = requirement_spoken_text(req)
    if ask is None or not ask.speaker:
        return base
    return combine_receipts(base, _receipt(req, "partial", _SPOKEN_TEXT_FIRST_HEARD))


def _protected_text_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    from app.kria.protected_targets import (  # noqa: PLC0415
        PROTECTED_TEXT_FIELD,
        protected_text_fingerprint,
        stale_protected_text,
    )

    receipt = variant.get(PROTECTED_TEXT_FIELD)
    if (
        isinstance(receipt, Mapping)
        and receipt.get("measured_fingerprint")
        and receipt["measured_fingerprint"] != protected_text_fingerprint(variant)
    ):
        # The text, captions or bars changed after the render measured them (an editor
        # Save, a text edit): the old verdict describes a video the creator no longer has.
        receipt = stale_protected_text(receipt, "edited_text")
    targets = receipt.get("targets") if isinstance(receipt, Mapping) else None
    if not isinstance(targets, list):
        return {}
    by_req: dict[str, list[tuple[str, str]]] = {}
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        label = "face" if target.get("kind") == "face" else str(target.get("label") or "")
        for req_id in target.get("requirement_ids") or []:
            by_req.setdefault(str(req_id), []).append(
                (label, str(target.get("status") or "unverified"))
            )
    rank = {
        "clear": 0,
        "unverified": 1,
        "stale": 2,
        "no_selection": 2,
        "invalidated": 2,
        "other_source": 2,
    }
    rows = []
    for req_id, entries in by_req.items():
        verdict = max((status for _, status in entries), key=lambda s: rank.get(s, 3))
        labels = tuple(label for label, status in entries if status != "clear")
        rows.append((req_id, verdict, labels))
    return {"protected_text": tuple(rows)}


def _with_protected_text(
    req: BriefRequirement, facts: PlanFacts, base: RequirementReceipt
) -> RequirementReceipt:
    """KRI-827: "no text on her face or on the plate" is met only when the render measured
    every text clear of every named target over each text's whole time on screen, against
    an object the tracker actually saw. Anything else says what is missing."""
    row = next((r for r in facts.protected_text or () if r[0] == req.id), None)
    if row is None or row[1] == "clear":
        return base
    _, verdict, labels = row
    # The object in the creator's own words; a face target is "your face" in either language.
    named = ", ".join("your face" if label == "face" else f"the {label}" for label in labels)
    named = named or "it"
    named_tr = ", ".join("yüzün" if label == "face" else label for label in labels) or "o"
    if verdict == "no_selection":
        reason = say(
            en=f"Mark {named} on a frame so I can keep the text off it",
            tr=f"Yazıyı uzak tutabilmem için {named_tr} bir karede işaretle",
        )
    elif verdict == "invalidated":
        reason = say(
            en=f"The clip changed since you marked {named}; mark it again",
            tr=f"{named_tr} işaretlediğinden beri klip değişti; tekrar işaretle",
        )
    elif verdict == "stale":
        reason = say(
            en=f"You changed the edit after I placed the text, so I can't confirm it still "
            f"stays off {named}",
            tr=f"Yazıyı yerleştirdikten sonra düzenlemeyi değiştirdin; hâlâ {named_tr} "
            "üstünde olmadığını doğrulayamıyorum",
        )
    elif verdict == "other_source":
        reason = say(
            en=f"You marked {named} on a different clip than the one I put the text on; "
            "mark it on the speaking clip",
            tr=f"{named_tr} yazının olduğu klipte değil başka bir klipte işaretledin; "
            "konuşan klipte işaretle",
        )
    elif verdict == "unverified":
        reason = say(
            en=f"I kept the text away from where {named} was, but lost sight of it for "
            "part of the video, so I can't confirm it stays clear",
            tr=f"Yazıyı {named_tr} olduğu yerden uzak tuttum ama videonun bir kısmında onu "
            "göremedim, açıkta kaldığını doğrulayamıyorum",
        )
    else:
        reason = say(
            en=f"Some text still covers {named}; there was no spot clear of it",
            tr=f"Bazı yazılar hâlâ {named_tr} üstünde; açıkta bir yer kalmadı",
        )
    return combine_receipts(base, _receipt(req, "partial", reason))


def _title_face_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    placement = variant.get("opening_title_placement")
    if isinstance(placement, Mapping) and isinstance(placement.get("face_collision"), bool):
        return {"title_face_collision": placement["face_collision"]}
    return {}


def _with_title_face(
    req: BriefRequirement, facts: PlanFacts, base: RequirementReceipt
) -> RequirementReceipt:
    """KRI-706: text never covers a face (a product rule, asked for or not), so a title
    the render could not move off the face is not "met"; say so instead of certifying
    the words alone."""
    if req.scope != "title" or base.status != "met" or not facts.title_face_collision:
        return base
    return _receipt(
        req,
        "partial",
        say(
            en="The title lands on a face; I couldn't find a spot clear of it",
            tr="Başlık bir yüzün üstüne denk geliyor; yüzü açıkta bırakan bir yer bulamadım",
        ),
    )


def _opening_cut_miss(reason: str | None, quote: str | None) -> str:
    line = f"“{quote}”" if quote else "that line"
    if reason == "cleanup_off":
        return say(
            en="Speech cleanup was off, so I couldn't cut the start and the video opens at the "
            "beginning of your take",
            tr="Konuşma temizliği kapalıydı, o yüzden başı kesemedim ve video çekimin "
            "başından açılıyor",
        )
    if reason in {"implausible", "too_short"}:
        return say(
            en=f"I found {line} but cutting to it would leave too little video, so the video "
            "still opens at the beginning of your take",
            tr=f"{line} cümlesini buldum ama oraya kesmek çok az video bırakırdı, bu yüzden "
            "video hâlâ çekimin başından açılıyor",
        )
    return say(
        en=f"I couldn't find {line} in your take, so the video still opens at the beginning",
        tr=f"{line} cümlesini çekiminde bulamadım, bu yüzden video hâlâ başından açılıyor",
    )


def _end_cut_miss(reason: str | None, quote: str | None) -> str:
    line = f"\u201c{quote}\u201d" if quote else "that line"
    if reason == "cleanup_off":
        return say(
            en="Speech cleanup was off, so I couldn't cut the end and the video runs to the end "
            "of your take",
            tr="Konuşma temizliği kapalıydı, o yüzden sonu kesemedim ve video çekimin sonuna "
            "kadar sürüyor",
        )
    if reason == "too_short":
        return say(
            en=f"I found {line} but ending there would leave too little video, so the video "
            "runs to the end of your take",
            tr=f"{line} cümlesini buldum ama orada bitirmek çok az video bırakırdı, bu yüzden "
            "video çekimin sonuna kadar sürüyor",
        )
    if reason == "before_start":
        return say(
            en=f"You say {line} only before your start line, so the video runs to the end of "
            "your take",
            tr=f"{line} cümlesini yalnızca başlangıç cümlenden önce söylüyorsun, bu yüzden video "
            "çekimin sonuna kadar sürüyor",
        )
    return say(
        en=f"I couldn't find {line} in your take, so the video runs to the end",
        tr=f"{line} cümlesini çekiminde bulamadım, bu yüzden video sonuna kadar sürüyor",
    )


def _check_opening_cut(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    from app.kria.opening_cut_ask import unread_line_sides  # noqa: PLC0415

    start_line, end_line = _take_window_lines(req)
    # KRI-837: a stored line with no words in it cannot be cut at, but it is still asked: that
    # side stays unchecked, so the other side's cut never certifies the whole ask.
    unread_start, unread_end = unread_line_sides(req)
    ends_asked = bool(end_line) or unread_end
    if not facts.rendered_variant:
        return _receipt(
            req,
            "partial",
            say(
                en="I'll check where the video starts and ends on the finished video.",
                tr="Videonun nerede başlayıp bittiğini video hazır olunca kontrol edeceğim.",
            )
            if ends_asked
            else say(
                en="I'll check where the video starts on the finished video.",
                tr="Videonun nereden başladığını video hazır olunca kontrol edeceğim.",
            ),
            verification="unchecked",
        )
    sides = [
        (facts.opening_cut, facts.opening_cut_reason, facts.opening_cut_quote, _opening_cut_miss)
        if start_line
        else None,
        (facts.end_cut, facts.end_cut_reason, facts.end_cut_quote, _end_cut_miss)
        if end_line
        else None,
    ]
    asked = [side for side in sides if side is not None]
    if unread_start or unread_end or any(status is None for status, *_rest in asked):
        return _receipt(
            req,
            "partial",
            say(
                en="This video didn't record where it starts, so I can't check it.",
                tr="Bu video nereden başladığını kaydetmedi, o yüzden kontrol edemiyorum.",
            )
            if not ends_asked
            else say(
                en="This video didn't record where it starts and ends, so I can't check it.",
                tr="Bu video nerede başlayıp bittiğini kaydetmedi, o yüzden kontrol edemiyorum.",
            ),
            verification="unchecked",
        )
    misses = list(
        dict.fromkeys(
            miss(reason, quote) for status, reason, quote, miss in asked if status != "applied"
        )
    )
    if misses:
        return _receipt(req, "partial", "; ".join(misses))
    if _cleanup_wording(req) and facts.speech_cleanup_outcome not in {"applied", "no_change"}:
        # "...and cut the long pauses" in the same sentence is judged by the cleanup too.
        return _receipt(req, "partial", _CLEANUP_NOT_RUN)
    return _receipt(req, "met", None)


def _hold_row(req: BriefRequirement, facts: PlanFacts) -> Mapping[str, Any] | None:
    """KRI-824: the plan's applied-hold row for ``req``; only the item's photos have one."""
    if req.authority is None or hold_seconds(req) is None or not facts.asset_holds:
        return None
    return next((r for r in facts.asset_holds if r.get("requirement_id") == req.id), None)


def _check_asset_hold(req: BriefRequirement, row: Mapping[str, Any]) -> RequirementReceipt:
    """KRI-824: "keep the beach photo on screen for 1 second", judged on that photo."""
    asked = hold_seconds(req) or 0.0
    rendered = _finite_number(row.get("rendered_s"))
    if rendered is None:
        return _receipt(
            req,
            "partial",
            say(en="This photo isn't in the video.", tr="Bu fotoğraf videoda yok."),
        )
    if abs(rendered - asked) <= HOLD_TOLERANCE_S:
        return _receipt(req, "met", None)
    return _receipt(
        req,
        "partial",
        say(
            en=f"This photo plays {_round_s(rendered)}s; you asked for {asked:g}s.",
            tr=f"Bu fotoğraf {_round_s(rendered)} saniye görünüyor; sen {asked:g} saniye istedin.",
        ),
    )


def _check_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    hold_row = _hold_row(req, facts)
    if hold_row is not None:
        # A creator-stamped photo length the plan applied (KRI-824). Any other clip-scoped
        # length (a video clip, an unstamped model number) keeps the clip paths below.
        return _check_asset_hold(req, hold_row)
    from app.services.length_policy import (  # noqa: PLC0415
        strict_duration_limit_s,
        strict_duration_matches,
    )

    maximum = strict_duration_limit_s(req)
    if maximum is not None:
        measured = facts.measured_export_duration_s
        if measured is None:
            return _receipt(
                req,
                "partial",
                say(
                    en="I'll check the full video length, including its ending, after export.",
                    tr="Kapanış dahil tam video süresini dışa aktardıktan sonra kontrol edeceğim.",
                ),
                verification="unchecked",
            )
        if strict_duration_matches(measured, maximum):
            return _receipt(req, "met", None)
        return _receipt(
            req,
            "partial",
            say(
                en=f"The finished video is {_round_s(measured)}s including its ending; "
                f"you asked for under {_round_s(maximum)}s.",
                tr=f"Kapanışı dahil bitmiş video {_round_s(measured)} saniye; "
                f"{_round_s(maximum)} saniyenin altında istemiştin.",
            ),
        )
    if clip_length_ask(req) is not None:
        # KRI-558: a per-clip length is judged on the clips, never against the total length.
        return _check_clip_lengths(req, facts)
    if _wants_whole_take(req):
        return _check_whole_take(req, facts)
    target = req.facts.get("duration_s")
    if _wants_whole_text_span(req):
        if not facts.editor or not facts.text_spans or facts.duration_s is None:
            return _receipt(req, "partial", _CANT_CHECK_TIMING)
        target_ids = req.facts.get("target_ids")
        if isinstance(target_ids, list) and target_ids:
            wanted = {str(value) for value in target_ids}
            spans = [
                span
                for span in facts.text_spans
                if span[0] in wanted and span[1] in {"title", "generative_intro"}
            ]
            if len(spans) != len(wanted):
                return _receipt(req, "partial", _CANT_CHECK_TIMING)
        else:
            if facts.text_timing_incomplete:
                return _receipt(req, "partial", _CANT_CHECK_TIMING)
            spans = [span for span in facts.text_spans if span[1] in {"title", "generative_intro"}]
        if not spans:
            return _receipt(req, "partial", _CANT_CHECK_TIMING)
        tolerance = 0.05
        if all(
            start <= tolerance and end >= facts.duration_s - tolerance for _, _, start, end in spans
        ):
            return _receipt(req, "met", None)
        return _receipt(req, "partial", "Some requested text ends before the video does.")
    if not isinstance(target, (int, float)) or target <= 0:
        if facts.narrated_steps is not None:
            return _check_narrated_timing(req, facts)
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    if req.scope != "global" or times_one_moment(req):
        # The total edit length cannot establish a clip or text duration. Until
        # that requirement has resolved target windows, report missing evidence
        # instead of rejecting a valid edit (or falsely passing an equal total).
        # KRI-777: nor a moment's or an effect's seconds filed as global ("freeze the frame
        # for one second on Atakan's bite" is not a one-second video).
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    strategy = {
        "edit_format": facts.edit_format or "montage",
        "audio_strategy": facts.audio_strategy or "licensed_music",
    }
    try:
        natural_owner = natural_length_owner(strategy)
        owner = length_owner(strategy, creator_duration_proven=True)
    except ValueError:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    if owner != "creator" or natural_owner not in {"song", "voiceover", "take", "footage"}:
        # Keep the owner matrix authoritative if a future policy changes it.
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    if facts.duration_s is None:
        return _receipt(req, "partial", _CANT_CONFIRM_LENGTH)
    # KRI-847: "at most 10 seconds" is the same +/- 10 % check without its lower side.
    bound = duration_bound(req.facts)
    if creator_duration_satisfied(facts.duration_s, float(target), bound):
        return _receipt(req, "met", None)
    if (
        natural_owner == "take"
        and facts.speech_cleanup_outcome == "applied"
        and facts.duration_s < float(target)
    ):
        # The target is the take's natural length (accepted as such, or implied by the
        # brief); cleanup cutting pauses legitimately brings it under that (KRI-629).
        return _receipt(req, "met", None)
    # The edit's own length is what is compared: the phone adds its outro after the edit,
    # so the file runs `outro_s` longer than this (28.0s of edit is a ~29.6s video).
    outro = ""
    if facts.outro_s:
        outro = say(
            en=f" (plus a {_round_s(facts.outro_s)}s outro on the finished video)",
            tr=(
                f" (bitmiş videoda buna ek olarak {_round_s(facts.outro_s)} saniyelik "
                "bir kapanış var)"
            ),
        )
    asked = _round_s(float(target))
    asked_en = f"at most {asked}s" if bound == "at_most" else f"{asked}s"
    asked_tr = f"en fazla {asked} saniye" if bound == "at_most" else f"{asked} saniye"
    return _receipt(
        req,
        "partial",
        say(
            en=(
                f"This draft is about {_round_s(facts.duration_s)}s{outro}; "
                f"you asked for {asked_en}."
            ),
            tr=(
                f"Bu taslak yaklaşık {_round_s(facts.duration_s)} saniye{outro}; sen "
                f"{asked_tr} istedin."
            ),
        ),
    )


# ------------------------------------------- phone Voiceover render receipts (KRI-533)
#
# A phone Voiceover edit is laid out by the worker (`_run_phone_narrated_job`), which
# records every clip's window, the creator's labels for it and the words spoken over it
# (`assembly_plan["narrated_alignment"]`). These checkers read only that record, like the
# unified montage's do: an order anchor ("end on the sunset valley"), a timing ask that
# names a labelled clip group ("show the balloons while talking about the balloons") and
# the language of the captions. Anything the record cannot settle stays unchecked.

_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an|my|our|that|this|these|those|some|bu)\s+")
_EXCERPT_CHARS = 110

# The language table (names and every creator spelling) is shared with the dispatcher and the
# worker: app/pipeline/caption_language.py (KRI-838).
_LANG_ALT = "|".join(sorted(LANGUAGE_WORDS, key=len, reverse=True))
_CAPTION_WORD = r"(?:sub\s*titles?|captions?|altyazi\w*)"
# Most specific first: "translate the captions to English" names the target outright.
_CAPTION_LANGUAGE_PATTERNS = (
    re.compile(rf"\btranslat\w*\s+(?:\w+\s+){{0,3}}?(?:in|into|to)\s+({_LANG_ALT})\b"),
    re.compile(rf"\b({_LANG_ALT})\s+{_CAPTION_WORD}"),
    re.compile(rf"\b{_CAPTION_WORD}\s+(?:in|into|to|as)\s+({_LANG_ALT})\b"),
    # "altyazılar Türkçe", and (KRI-838) the bare English wordings the patterns above miss:
    # "make the subtitles Spanish", "subtitles should be Spanish". Only when the language
    # closes the ask (`ASK_END`): "captions Spanish food intro", "subtitles Italian
    # restaurant style" and "captions Dutch oven" name a topic, not the captions' language.
    re.compile(
        rf"\b{_CAPTION_WORD}\s+(?:(?:\w+\s+){{0,2}}?(?:be|are|is)\s+)?({_LANG_ALT})\b{ASK_END}"
    ),
    re.compile(rf"\b({_LANG_ALT})\s+(?:olarak|cevir\w*)"),
)
_SPELLING_RE = re.compile(r"\b(?:spell\w*|exact\w*|yazim\w*|dogru yaz\w*)\b")


def _loose_req(req: BriefRequirement) -> str:
    """The requirement's words, diacritic-free and punctuation-free, for name matching."""
    return " ".join(re.sub(r"[^\w\s]", " ", loose_text(_req_text(req))).split())


def _label_core(value: str) -> str:
    """A creator's name for a group ("The Balloons!") without case, accents or article."""
    text = " ".join(re.sub(r"[^\w\s]", " ", loose_text(value or "")).split())
    return _LEADING_ARTICLE_RE.sub("", text).strip()


def _same_group(label: str, words: str) -> bool:
    a, b = _label_core(label), _label_core(words)
    return bool(a and b) and (a == b or _contains_text(a, b) or _contains_text(b, a))


def _sentence_case(value: str) -> str:
    # KRI-781: Turkish-aware first letter ("ırmak" -> "Irmak", not "Irmak" via "I").
    return capitalize(value.strip())


def _step_names(step: NarratedStepFact) -> tuple[str, ...]:
    return (*step.labels, *(name for _spot, name in step.placed))


def _narrated_anchors(
    req: BriefRequirement, steps: Sequence[NarratedStepFact]
) -> list[tuple[str, str]]:
    """The (spot, words) first/last seats an order requirement asks for.

    Stated outright (``first_clip`` / ``last_clip``) or implied: a resolved ``first`` /
    ``last`` order intent whose name the requirement's own words contain.
    """
    anchors: list[tuple[str, str]] = []
    for key, spot, _verb in _ORDER_ANCHOR_FACTS:
        words = req.facts.get(key)
        if isinstance(words, str) and words.strip():
            anchors.append((spot, words.strip()))
    seated = {spot for spot, _words in anchors}
    wording = _loose_req(req)
    for step in steps:
        for spot, name in step.placed:
            core = _label_core(name)
            if spot in ("first", "last") and spot not in seated and core:
                if _contains_text(wording, core):
                    anchors.append((spot, name))
                    seated.add(spot)
    return anchors


def _voice_moved_names(req: BriefRequirement, steps: Sequence[NarratedStepFact]) -> list[str]:
    """The creator's names, in screen order, for the clips of a described sequence that the
    voiceover placed in another order (KRI-590) and that this requirement's words mention."""
    wording = _loose_req(req)
    names: list[str] = []
    for step in steps:
        if not step.out_of_sequence:
            continue
        for name in step.labels:
            core = _label_core(name)
            if (
                core
                and _contains_text(wording, core)
                and not any(_same_group(name, n) for n in names)
            ):
                names.append(name)
    return names


def _followed_voice_reason(names: Sequence[str]) -> str:
    def joined(conjunction: str) -> str:
        if len(names) == 1:
            return names[0]
        return f"{', '.join(names[:-1])} {conjunction} {names[-1]}"

    return say(
        en=(
            f"Your voiceover mentions {joined('and')} in a different order, so I followed "
            "the voiceover."
        ),
        tr=(
            f"Seslendirmende {joined('ve')} farklı bir sırayla geçiyor, ben de "
            "seslendirmeyi takip ettim."
        ),
    )


def _check_narrated_order(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A stated first / last clip is met when that clip really opens / closes the edit.

    A described sequence the voiceover overrode (KRI-590: the worker kept the narration's
    order) is never "met": an order ask that names those clips reads partly, with why.
    """
    steps = facts.narrated_steps or ()
    anchors = _narrated_anchors(req, steps)
    moved = _voice_moved_names(req, steps)
    if not anchors or not steps:
        if moved:
            return _receipt(req, "partial", _followed_voice_reason(moved))
        # No seat to judge: the voiceover sets this edit's order, so any other ordering
        # rule stays unverified, exactly as before the render recorded anything.
        return _receipt(req, "partial", _CANT_CHECK_ORDER_RULE)
    # Same strictness as the montage branch: only a bound / contract-stamped job turns a
    # missed seat into a failure; an unbound one keeps the softer "partly".
    unmet = "not_possible" if facts.strict_order and _order_is_required(req) else "partial"
    problems: list[str] = []
    unknown = False
    for spot, words in anchors:
        members = [
            index
            for index, step in enumerate(steps)
            if any(_same_group(name, words) for name in _step_names(step))
        ]
        if not members:
            unknown = True
            continue
        want = len(steps) - 1 if spot == "last" else 0
        if want in members:
            continue
        where = members[-1] if spot == "last" else members[0]
        problems.append(
            say(
                en=f"{_sentence_case(words)} is clip {where + 1} of {len(steps)}, "
                f"not the {spot} one.",
                tr=f"{capitalize(words.strip(), 'tr')} {len(steps)} klipten {where + 1}. "
                f"sırada, {_SPOT_TR[spot]} değil.",
            )
        )
    if problems:
        return _receipt(req, unmet, " ".join(problems))
    if moved:
        return _receipt(req, "partial", _followed_voice_reason(moved))
    if unknown:
        return _receipt(req, "partial", _CANT_CONFIRM_ORDER)
    return _receipt(req, "met", None)


def _excerpt(text: str) -> str:
    """The first and last words of a narration, joined with an ellipsis when long."""
    text = " ".join(text.replace('"', "'").split())
    if len(text) <= _EXCERPT_CHARS:
        return text
    head = text[: int(_EXCERPT_CHARS * 0.55)].rsplit(" ", 1)[0]
    tail = text[-int(_EXCERPT_CHARS * 0.4) :].split(" ", 1)[-1]
    return f"{head} … {tail}"


def _wants_clip_timing(req: BriefRequirement) -> bool:
    """A timing ask with no number, no title and no "keep my take": it can only be about
    when something plays, which a rendered Voiceover record may be able to answer.
    A pop-in ask ("show my photo when voiceover says X", KRI-537) is the beat checker's,
    judged at draft from the strategy and at render from the variant's beat receipt, so
    it is neither deferred nor read off the clip windows here."""
    return (
        req.kind == "timing"
        and req.scope != "title"
        and not _has_duration_target(req)
        and not _wants_whole_take(req)
        and not _wants_beats(req)
    )


def _check_narrated_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """ "Show the balloons while talking about the balloons": report what the voiceover
    says over the clips the creator named. Only clips placed by the spoken words count."""
    steps = facts.narrated_steps or ()
    if facts.ordering_basis not in NARRATED_ALIGNED_BASES or not steps:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    wording = _loose_req(req)
    named: list[str] = []
    for step in steps:
        for name in _step_names(step):
            core = _label_core(name)
            if (
                core
                and _contains_text(wording, core)
                and not any(_same_group(name, n) for n in named)
            ):
                named.append(name)
    if not named:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    members = [
        index
        for index, step in enumerate(steps)
        if any(_same_group(name, wanted) for name in _step_names(step) for wanted in named)
    ]
    first, last = members[0], members[-1]
    if last - first + 1 != len(members):
        # The named clips are split apart: one window would claim the clips between them.
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    excerpt = _excerpt(" ".join(step.text for step in steps[first : last + 1] if step.text))
    if not excerpt:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    start, end = steps[first].start_s, steps[last].end_s
    en_label = _sentence_case(" and ".join(named))
    tr_label = capitalize(" ve ".join(named).strip(), "tr")
    return _receipt(
        req,
        "met",
        say(
            en=f'{en_label} clips play from {start:.1f} s to {end:.1f} s, under: "{excerpt}"',
            tr=f"{tr_label} klipleri {start:.1f} sn ile {end:.1f} sn arasında oynuyor, "
            f'şu sözlerle: "{excerpt}"',
        ),
    )


def _requested_caption_language(req: BriefRequirement) -> str | None:
    """The language a captions ask wants ("English subtitles", "captions in Turkish"), as
    a code, else None. "English subtitles translated from the Turkish voiceover" is English."""
    stated = _caption_language_fact(req)
    if stated is not None:
        return stated
    if _keeps_spoken_languages(req):
        return None
    wording = loose_text(_req_text(req))
    for pattern in _CAPTION_LANGUAGE_PATTERNS:
        found = pattern.search(wording)
        if found:
            return LANGUAGE_WORDS[found.group(1)]
    return None


def _language_name(code: str) -> str:
    english, turkish = LANGUAGE_NAMES.get(code, (code.upper(), code.upper()))
    return say(en=english, tr=turkish)


def _keeps_spoken_languages(req: BriefRequirement) -> bool:
    """KRI-1015: the requirement's own words keep every language as spoken ("English captions
    for the English parts, Turkish captions for the Turkish parts"): it asks for no one language,
    so the first language name in it is no ask (`caption_language_decision`)."""
    return caption_language_decision(_req_text(req)).keep_spoken


def _translated_from(facts: PlanFacts, rendered: str) -> str:
    """KRI-1015: the spoken language (a code) the rendered captions were translated out of,
    or "" when the render did not translate them or the stamp is no news."""
    source = str(facts.caption_translated_from or "").casefold().split("-")[0]
    return source if rendered and source != rendered else ""


def _read_caption_language(
    lines: Sequence[str], asked: str, names: Sequence[str] = ()
) -> str | None:
    """The language the caption TEXT is in when it settles the ask, else None (KRI-838).

    A render's ``caption_language`` is a label the worker stamped; the lines are what the
    creator sees. whisper hinted to Turkish on English speech wrote English, and the label
    said "tr", so a label-only check called English captions Turkish ("Done"). The text
    outranks the label: the asked language when the lines are clearly in it, the language
    they are clearly in otherwise, None when they are too short or mixed to say (the label
    then stands). The creator's own spelling targets carry no language evidence.
    """
    verdict, detected = caption_language_verdict(lines, asked, ignore_terms=tuple(names))
    if verdict == "confirmed":
        return asked
    if verdict == "contradicted":
        return detected
    return None


def _check_caption_language(
    req: BriefRequirement, facts: PlanFacts, asked: str
) -> RequirementReceipt:
    """The captions' language against the one the creator asked for. Spelling of names
    stays unchecked, and the reason says so when the ask was about spelling."""
    rendered = str(facts.caption_language or "").casefold().split("-")[0]
    if facts.caption_texts:
        rendered = (
            _read_caption_language(facts.caption_texts, asked, _spelled_names(req) or ())
            or rendered
        )
    note = ""
    if _SPELLING_RE.search(loose_text(_req_text(req))):
        note = " " + say(
            en="I haven't checked how the names are spelled.",
            tr="İsimlerin yazımını kontrol etmedim.",
        )
    if rendered != asked:
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"The captions are in {_language_name(rendered)}, not {_language_name(asked)}."
                ),
                tr=(
                    f"Altyazılar {_language_name(asked)} değil, {_language_name(rendered)} "
                    "olarak çıktı."
                ),
            )
            + note,
        )
    spoken = str(facts.spoken_language or "").casefold().split("-")[0]
    if spoken and spoken != rendered:
        reason = say(
            en=(
                f"The captions are in {_language_name(rendered)}, translated from the "
                f"{_language_name(spoken)} voiceover."
            ),
            tr=(
                f"Altyazılar {_language_name(rendered)}, {_language_name(spoken)} "
                "seslendirmeden çevrildi."
            ),
        )
    else:
        reason = _captions_in(rendered, _translated_from(facts, rendered))
    return _receipt(req, "met", reason + note)


def _captions_in(rendered: str, translated_from: str = "") -> str:
    """Which language the captions are in and, KRI-1015, where the words came from when the
    render translated them (``translated_from`` is the spoken language, "" = it did not)."""
    if translated_from:
        return say(
            en=(
                f"The captions are in {_language_name(rendered)}, translated from the "
                f"{_language_name(translated_from)} speech."
            ),
            tr=(
                f"Altyazılar {_language_name(rendered)}, {_language_name(translated_from)} "
                "konuşmadan çevrildi."
            ),
        )
    return say(
        en=f"The captions are in {_language_name(rendered)}.",
        tr=f"Altyazılar {_language_name(rendered)}.",
    )


def asks_caption_language(req: BriefRequirement) -> bool:
    """True for a captions ask that names a language (judged on the rendered captions)."""
    return _wants_captions(req) and _requested_caption_language(req) is not None


def asks_caption_spelling(req: BriefRequirement) -> bool:
    """KRI-961: True for a captions ask that lists words to spell. Only the caption lines
    can settle it, so a record without them (the Voiceover alignment record) leaves it to
    the finished captions instead of answering for its language alone."""
    return _wants_captions(req) and not _wants_no_captions(req) and bool(_spelled_names(req))


# ------------------------------------------------------------- caption words (KRI-549)
# "altyazılar Türkçe olsun; Moda, Bahariye, Yeldeğirmeni ve Kadıköy doğru yazılsın" on a
# finished phone render: the language is judged against the variant's `caption_language`
# and each name the creator asked to be spelled right against the caption lines it shows.
# A name is read only from a capitalized list right next to a spelling cue (the creator's
# own spelling). Anything less certain stays unchecked: a false "Done" is worse than
# "can't check".

# Wordings the stricter `_CAPTION_LANGUAGE_PATTERNS` miss ("captions should be in English",
# "altyazılar da Türkçe", "altyazıları Türkçeye çevir"); run on `loose_text`.
_LOOSE_CAPTION_LANGUAGE_PATTERNS = (
    re.compile(rf"\b{_CAPTION_WORD}\s+(?:\w+\s+){{0,3}}?(?:in|into)\s+({_LANG_ALT})\b"),
    re.compile(rf"\baltyazi\w*\s+(?:\w+\s+){{0,2}}?({_LANG_ALT})\w*"),
)
# A spelling cue in the creator's own words (case kept, so the names stay readable). After
# "spell"/"spelling (of)" the names follow ("spell A and B right"); after any other cue
# they come first ("A ve B doğru yazılsın", "A and B spelled right"). A colon list
# ("spelling the names exactly: A, B") always follows.
_SPELL_CUE_RE = re.compile(
    r"\b(?P<after>spell(?:ings?)?(?:\s+of)?)\b"
    r"|\b(?:spelled|spelt|spells)\b"
    r"|\b(?:do[gğ]ru|d[uü]zg[uü]n|hatas[ıi]z)\s+(?:bir\s+)?yaz\w*"
    r"|\byaz[ıi]m\w*|\bimla\w*",
    re.IGNORECASE,
)
_SPELL_CLAUSE_SPLIT_RE = re.compile(r"[;\n!?]+|\.(?=\s|$)")
# Words (inner apostrophes and hyphens kept: "Kadıköy'ü", "Yel-Değirmeni") and list commas.
_NAME_TOKEN_RE = re.compile(r"[^\W_](?:[\w'’\-]*[^\W_])?|[,&]")
_NAME_JOINERS = frozenset({"and", "ve", "ile", "&", "plus"})
_NAME_PARTICLES = frozenset(
    {"de", "da", "del", "della", "di", "du", "la", "le", "van", "von", "der", "den", "al", "el"}
)
# Capitalized words that open a sentence or name the captions, never a name to spell.
_NOT_NAMES = frozenset(
    "make please ensure also and the all keep write check use i my names"
    " lutfen ayrica ve tum butun ben benim yaz isimler isimleri".split()
)
_MAX_NAME_GAP = 3  # words allowed between a name list and its cue ("A and B are spelled")
_MAX_SPELLED_NAMES = 12
# Caption look, size or place (`_CAPTION_DETAIL_RE` without the language words and without a
# bare "right", which "spell it right" uses): such an ask stays with `_check_captions`.
_CAPTION_LOOK_RE = re.compile(
    r"\b(colou?rs?|yellow|white|red|blue|green|black|pink|orange|purple|lime|font|bold|italic"
    r"|sizes?|bigger|big|small|smaller|larger|huge|tiny|top|bottom|middle|cent(er|re)"
    r"|higher|lower|outline|shadow|stroke|uppercase|lowercase|caps)\b"
    r"|\b(on|to|at) the (left|right)\b|\b(left|right)[- ](side|aligned|corner)\b"
    r"|\brenk|\bsari\b|\bbeyaz\b|\bbüyük|\bküçük|\büst|\balt(ta|a)\b|\bsol(da|a)\b"
    r"|\bsağ(da|a)\b|\byazi tipi|\bkalin"
)
# Caption words: letters/digits joined by apostrophes ("Kadıköy'de"); hyphens and spaces split.
_CAPTION_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_APOSTROPHE_RE = re.compile(r"['’]")
# A Turkish case/possessive ending glued onto a name without its apostrophe ("Kadıköyde",
# "Modanın"), on `loose_text` letters. Only checked on a capitalized word of a long enough
# name, so "modası" (fashion) or "Adana" never reads as a misspelled Moda or Ada.
_GLUED_SUFFIX_RE = re.compile(r"[nys]?(?:[dt][ae](?:n|ki)?|[ae]|[iu]n?|l[ae]r\w{0,4}|l[iu]|l[ae])")
_MIN_GLUED_NAME = 4


def _asked_caption_language(req: BriefRequirement) -> str | None:
    """`_requested_caption_language`, plus a few looser wordings of the same ask."""
    found = _requested_caption_language(req)
    if found is not None:
        return found
    if _keeps_spoken_languages(req):
        return None
    wording = loose_text(_req_text(req))
    for pattern in _LOOSE_CAPTION_LANGUAGE_PATTERNS:
        match = pattern.search(wording)
        if match:
            return LANGUAGE_WORDS[match.group(1)]
    return None


def _capitalized(token: str) -> bool:
    return token[:1].isupper()


def _name_lists(text: str) -> tuple[list[str], list[tuple[int, int, list[str]]]]:
    """``text``'s tokens and its capitalized name lists as (first token, last token, names).

    A name is a run of capitalized words (a lowercase particle may join two: "Rio de
    Janeiro"); names joined by commas, "and", "ve", "ile" or "&" form one list.
    """
    tokens = _NAME_TOKEN_RE.findall(text)
    chunks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(tokens):
        if not _capitalized(tokens[i]):
            i += 1
            continue
        words, j = [tokens[i]], i + 1
        # A word with an apostrophe ends its name ("Kadıköy'ü Moda" is two names).
        while j < len(tokens) and not _APOSTROPHE_RE.search(words[-1]):
            if _capitalized(tokens[j]):
                words.append(tokens[j])
                j += 1
            else:
                # One or more lowercase particles may join two capitalized words
                # ("Rio de Janeiro", "Oblea de la Casa").
                k = j
                while k < len(tokens) and tokens[k].casefold() in _NAME_PARTICLES:
                    k += 1
                if k == j or k >= len(tokens) or not _capitalized(tokens[k]):
                    break
                words.extend(tokens[j : k + 1])
                j = k + 1
        chunks.append((i, j - 1, " ".join(words)))
        i = j
    lists: list[tuple[int, int, list[str]]] = []
    for start, end, name in chunks:
        between = [token.casefold() for token in tokens[lists[-1][1] + 1 : start]] if lists else []
        joined = bool(lists) and (
            between == [","]
            or (
                len(between) in (1, 2)
                and between[-1] in _NAME_JOINERS
                and between[:-1] in ([], [","])
            )
        )
        if joined:
            lists[-1] = (lists[-1][0], end, [*lists[-1][2], name])
        else:
            lists.append((start, end, [name]))
    return tokens, lists


def _word_count(tokens: Sequence[str]) -> int:
    return sum(1 for token in tokens if token not in {",", "&"})


def _list_before(text: str, max_gap: int = _MAX_NAME_GAP) -> list[str]:
    tokens, lists = _name_lists(text)
    if lists and _word_count(tokens[lists[-1][1] + 1 :]) <= max_gap:
        return lists[-1][2]
    return []


def _list_after(text: str, max_gap: int = _MAX_NAME_GAP) -> list[str]:
    tokens, lists = _name_lists(text)
    if lists and _word_count(tokens[: lists[0][0]]) <= max_gap:
        return lists[0][2]
    return []


def _clean_name(name: str) -> str | None:
    """The name without a case ending ("Kadıköy'ü" -> "Kadıköy"), or None for a non-name."""
    words = _APOSTROPHE_RE.split(name, maxsplit=1)[0].split()
    while words and loose_text(words[0]) in _NOT_NAMES:
        words = words[1:]
    if not words:
        return None
    core = loose_text(" ".join(words))
    if core in LANGUAGE_WORDS or _CAPTION_RE.search(core):
        return None
    return " ".join(words)


_MAX_LISTED_WORDS = 4  # words in one item of a colon list ("Oblea de la Casa")


def _colon_list(text: str) -> list[str]:
    """The items of the list after a spelling cue's colon, lowercase ones too (KRI-793).

    "correct spellings: Oblea de la Casa, dulce de leche, leche condensada, Merope": the
    colon makes the creator's own list explicit, so "dulce de leche" counts although it is
    not capitalized. Items are split on commas and "and" / "ve" / "ile" / "&". An item is a
    name ("Oblea de la Casa": capitalized words, lowercase particles between them) or, in a
    list of two or more, a short lowercase phrase ("dulce de leche"). The list ends at the
    first item that is neither ("Merope not Meropa", "keep it short", "names must be ...").
    """
    items: list[list[str]] = [[]]
    for token in _NAME_TOKEN_RE.findall(text):
        if token in {",", "&"} or token.casefold() in _NAME_JOINERS:
            if items[-1]:
                items.append([])
            continue
        items[-1].append(token)
    names: list[str] = []
    lowercase: list[bool] = []
    for words in (item for item in items if item):
        named = _capitalized(words[0]) and _capitalized(words[-1])
        named = named and all(
            _capitalized(word) or word.casefold() in _NAME_PARTICLES for word in words
        )
        phrase = (
            not any(_capitalized(word) for word in words)
            and len(words) < _MAX_LISTED_WORDS
            and loose_text(words[0]) not in _NOT_NAMES
        )
        if len(words) > _MAX_LISTED_WORDS or not (named or phrase):
            break
        names.append(" ".join(words))
        lowercase.append(phrase)
    if len(names) == 1 and lowercase[0]:
        return []
    return names


def _names_near_cue(clause: str, cue: re.Match[str]) -> list[str]:
    before, after = clause[: cue.start()], clause[cue.end() :]
    found: list[str] = []
    if ":" in after:
        listed = after.split(":", 1)[1]
        found = _colon_list(listed) or _list_after(listed, max_gap=1)
    if not found:
        order = (
            ((_list_after, after), (_list_before, before))
            if cue.group("after")
            else ((_list_before, before), (_list_after, after))
        )
        for pick, text in order:
            found = pick(text)
            if found:
                break
    return [name for name in map(_clean_name, found) if name]


def _spellings_fact(req: BriefRequirement) -> tuple[str, ...]:
    """``facts["spellings"]``: the words the model recorded the creator wants spelled their
    way in the captions (KRI-793), cleaned; () when absent or unusable."""
    raw = req.facts.get("spellings")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for value in raw:
        if not isinstance(value, str) or len(value) > 80:
            continue
        # Words only, as `_clean_name` reads a listed name: no edge punctuation ("Merope."),
        # no Turkish case ending ("Kadıköy'ü" -> "Kadıköy").
        words = [
            token
            for token in _NAME_TOKEN_RE.findall(unicodedata.normalize("NFC", value))
            if token not in {",", "&"}
        ]
        name = _clean_name(" ".join(words)) if words else None
        if name:
            names.append(name)
    return tuple(dict.fromkeys(names))[:_MAX_SPELLED_NAMES]


def _spelled_names(req: BriefRequirement) -> tuple[str, ...] | None:
    """The names a requirement asks to be spelled right, as the creator wrote them.

    The model's ``facts["spellings"]`` decides when present (KRI-667: the server checks the
    recorded fact, not the words). Otherwise None when the wording has no spelling cue;
    () when it has one but no capitalized name list (or colon list) sits next to it (then
    nothing about spelling can be claimed).
    """
    recorded = _spellings_fact(req)
    if recorded:
        return recorded
    text = unicodedata.normalize("NFC", " ".join(x for x in (req.description, req.literal) if x))
    cued = False
    names: list[str] = []
    for clause in _SPELL_CLAUSE_SPLIT_RE.split(text):
        for cue in _SPELL_CUE_RE.finditer(clause):
            cued = True
            names.extend(_names_near_cue(clause, cue))
    # KRI-631: quoted words to write as given ("community" ve "cringe" İngilizce yazılsın).
    quoted = extract_spelling_asks(text)
    cued = cued or bool(quoted)
    names.extend(quoted)
    if not cued:
        return None
    return tuple(dict.fromkeys(names))[:_MAX_SPELLED_NAMES]


def _asks_spelling(req: BriefRequirement) -> bool:
    return _spelled_names(req) is not None or bool(_SPELLING_RE.search(_loose_req(req)))


def _caption_words_ask(req: BriefRequirement) -> bool:
    """A captions ask about their language and/or how names are spelled, and nothing about
    their look or place (`_check_captions` keeps those)."""
    if _wants_no_captions(req):
        return False
    if _asked_caption_language(req) is None and not _asks_spelling(req):
        return False
    look = _req_text(req)
    for name in _spelled_names(req) or ():
        look = look.replace(_fold(name), " ")
    return not _CAPTION_LOOK_RE.search(look)


def _judges_caption_words(req: BriefRequirement, facts: PlanFacts) -> bool:
    """True when a finished render's caption lines can settle this captions ask."""
    return facts.caption_texts is not None and _wants_captions(req) and _caption_words_ask(req)


def _spelling_key(value: str, *, turkish: bool) -> str:
    """Case-free, whitespace-collapsed text that keeps every letter ("İ" -> "i"; in Turkish
    "I" -> "ı"), so "KADIKÖY" equals "Kadıköy" but "Kadikoy" does not.

    KRI-781: lower-cased with the shared Turkish-aware casing, which also applies Turkish
    i rules when the text itself has a Turkish-only letter, whatever the flag says.
    """
    text = text_lower(unicodedata.normalize("NFC", value), "tr" if turkish else None)
    return " ".join(text.casefold().split())


def _squash(value: str) -> str:
    """Letters only, diacritic-free: "Yel-Değirmeni" == "yeldegirmeni"."""
    return re.sub(r"[\W_]+", "", loose_text(value))


def _caption_spelling(name: str, captions: str, *, turkish: bool) -> tuple[bool, str | None]:
    """(``name`` appears as written, the first other spelling of it in ``captions``).

    A Turkish ending after an apostrophe is allowed ("Kadıköy'de"). Another spelling is the
    same letters with other accents, spacing or hyphens ("Kadikoy", "yel değirmeni"), or
    the name with an ending glued on without its apostrophe ("Kadıköyde").
    """
    want = _squash(name)
    if not want:
        return False, None
    # KRI-781: a name with a Turkish-only letter is Turkish even when the job language is
    # not, so "KIRMIZI" in the captions still equals the wanted "kırmızı".
    turkish = turkish or uses_turkish_case(name)
    want_key = _spelling_key(name, turkish=turkish)
    name_words = len(re.split(r"[\s\-]+", name.strip()))
    tokens = list(_CAPTION_TOKEN_RE.finditer(captions))
    exact = False
    other: str | None = None
    for i in range(len(tokens)):
        for size in range(1, name_words + 2):
            window = tokens[i : i + size]
            if len(window) < size:
                break
            if size > 1:
                # A name never runs past a case ending or punctuation ("Moda'da, deniz").
                gap = captions[window[-2].end() : window[-1].start()]
                if _APOSTROPHE_RE.search(window[-2].group()) or not re.fullmatch(r"[\s\-]+", gap):
                    break
            last = window[-1].group()
            base = _APOSTROPHE_RE.split(last, maxsplit=1)[0]
            shown = captions[window[0].start() : window[-1].start() + len(base)]
            squashed = _squash(shown)
            if squashed == want:
                if _spelling_key(shown, turkish=turkish) == want_key:
                    exact = True
                elif other is None:
                    other = shown
            elif (
                other is None
                and size == name_words
                and base == last
                and len(want) >= _MIN_GLUED_NAME
                and _capitalized(shown)
                and squashed.startswith(want)
                and _GLUED_SUFFIX_RE.fullmatch(squashed[len(want) :])
            ):
                other = shown
            elif other is None and abs(size - name_words) <= 1 and is_mishearing(shown, name):
                # KRI-793: the same name heard another way ("Meropa" for Merope, "Ole de
                # la Casa" for Oblea de la Casa), KRI-961 with a word more or fewer
                # ("Olloda bife" for ojo de bife): the rule the render's respelling uses.
                other = shown
    return exact, other


def _and_names(names: Sequence[str]) -> str:
    items = list(dict.fromkeys(names))
    if len(items) <= 1 or len(items) > _MAX_NAMED_IN_REASON:
        return _names(items)
    head = ", ".join(items[:-1])
    return say(en=f"{head} and {items[-1]}", tr=f"{head} ve {items[-1]}")


def _check_caption_words(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A captions ask about their language and the spelling of names, on a finished render.

    The language is judged against the render's ``caption_language``, each name against
    the caption lines. Met only when every part holds; a wrong language or a different
    spelling is an honest miss naming it, a name the captions never show is "Partly"
    with that reason, and a part with nothing to judge it by leaves the ask unchecked.
    """
    lines = facts.caption_texts or ()
    if not lines:
        return _receipt(
            req, "partial", say(en="This video has no captions.", tr="Bu videoda altyazı yok.")
        )
    done: list[str] = []
    misses: list[str] = []
    unsure: list[str] = []
    rendered = str(facts.caption_language or "").casefold().split("-")[0]
    asked = _asked_caption_language(req)
    names = _spelled_names(req)
    if asked is not None:
        # The lines outrank the label (KRI-838): English words under a "tr" stamp are not Turkish.
        rendered = _read_caption_language(lines, asked, names or ()) or rendered
        if not rendered:
            unsure.append(
                say(
                    en="I couldn't check which language the captions are in.",
                    tr="Altyazıların dilini kontrol edemedim.",
                )
            )
        elif rendered != asked:
            misses.append(
                say(
                    en=(
                        f"The captions are in {_language_name(rendered)}, "
                        f"not {_language_name(asked)}."
                    ),
                    tr=(
                        f"Altyazılar {_language_name(asked)} değil, {_language_name(rendered)} "
                        "olarak çıktı."
                    ),
                )
            )
        else:
            done.append(_captions_in(rendered, _translated_from(facts, rendered)))
    if _wants_word_captions(req) and facts.caption_style not in {"karaoke", "kinetic"}:
        misses.append(
            say(
                en="Captions are on as full sentences, not word by word.",
                tr="Altyazılar kelime kelime değil, tam cümleler olarak açık.",
            )
        )
    if _asks_spelling(req):
        if not names:
            unsure.append(
                say(
                    en="I couldn't tell which words to check the spelling of.",
                    tr="Yazımını kontrol edeceğim kelimeleri ayırt edemedim.",
                )
            )
        else:
            captions = " ".join(lines)
            turkish = rendered == "tr" or (not rendered and bool(re.search("[ıİşŞğĞ]", captions)))
            spelled: list[str] = []
            wrong: list[tuple[str, str]] = []
            absent: list[str] = []
            for name in names:
                exact, other = _caption_spelling(name, captions, turkish=turkish)
                if other is not None:
                    wrong.append((name, other))
                elif exact:
                    spelled.append(name)
                else:
                    absent.append(name)
            # KRI-849: words the creator chose for an unclear word are theirs, never a miss.
            chosen = {
                str(row.get("text") or "").casefold()
                for row in (facts.unclear_words or {}).get("spans") or []
                if isinstance(row, Mapping) and row.get("text")
            }
            wrong = [(name, other) for name, other in wrong if other.casefold() not in chosen]
            if wrong:
                misses.append(
                    say(
                        en="The captions write "
                        + ", ".join(f'"{other}" for {name}' for name, other in wrong)
                        + ". You can fix that in the editor.",
                        tr="Altyazılarda "
                        + ", ".join(f'{name} yerine "{other}"' for name, other in wrong)
                        + " yazıyor. Bunu editörde düzeltebilirsin.",
                    )
                )
            if absent:
                one = len(absent) == 1
                misses.append(
                    say(
                        en=(
                            f"{_and_names(absent)} {'doesn' if one else 'don'}'t appear in "
                            f"the captions, so I couldn't check {'its' if one else 'their'} "
                            "spelling."
                        ),
                        tr=(
                            f"Altyazılarda {_and_names(absent)} geçmiyor, o yüzden "
                            f"{'yazımını' if one else 'yazımlarını'} kontrol edemedim."
                        ),
                    )
                )
            if spelled:
                one = len(spelled) == 1
                done.append(
                    say(
                        en=(
                            f"{_and_names(spelled)} {'is' if one else 'are'} spelled as you "
                            f"wrote {'it' if one else 'them'}."
                        ),
                        tr=f"{_and_names(spelled)} yazdığın gibi yazılmış.",
                    )
                )
    if misses:
        return _receipt(req, "partial", " ".join([*misses, *unsure, *done]))
    if unsure:
        return _receipt(req, "partial", " ".join([*unsure, *done]), verification="unchecked")
    return _receipt(req, "met", " ".join(done) or None)


def _caption_words_plan(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """What a phone Talking draft will do with a captions ask about language or spelling
    (KRI-793).

    The draft has no captions to read yet (the render-ready review judges them, KRI-549), but
    saying nothing left "Captions for both voices, ensuring correct spellings: Oblea de la
    Casa, ..." silent through a held draft and two approval cards. The line says who gets
    captions (everything said in the take, an off-camera voice too), the language, and the
    words the render will spell the creator's way, then checks in the finished video. Its
    reason starts with a Talking plan prefix, so it is an answer, never the "simpler
    version?" question. Sentence captions for a word-by-word ask are a real miss and say so.
    """
    style = facts.caption_style
    # The phone Talking writer captions every take whatever the strategy's style says (the
    # style only picks word or sentence lines), so a "none" style is no reason to say "off".
    if _wants_word_captions(req) and style not in {None, "auto", "none", "karaoke", "kinetic"}:
        return _receipt(
            req,
            "partial",
            say(
                en="Captions are on as full sentences, not word by word.",
                tr="Altyazılar kelime kelime değil, tam cümleler olarak açık.",
            ),
        )
    parts = [_loc(_TALKING_CAPTIONS_EVERY_VOICE)]
    asked = _asked_caption_language(req)
    names = _spelled_names(req)
    if asked is not None:
        language = _language_name(asked)
        if not can_deliver_caption_language(asked):
            # KRI-838: Nova cannot write captions in this language yet. Say so before the
            # creator approves, never promise it and render the spoken language.
            parts.append(
                say(
                    en=f"I can't make {language} captions yet, so they'll be in the language "
                    "that's spoken",
                    tr=f"Altyazıları şimdilik {language} yapamıyorum, konuşulan dilde olacaklar",
                )
            )
        else:
            parts.append(
                say(en=f"they'll be in {language}", tr=f"altyazılar {language} olacak")
                if names or _asks_spelling(req)
                else say(
                    en=f"they'll be in {language}, and I'll check that in the finished video",
                    tr=f"altyazılar {language} olacak, bunu bitmiş videoda kontrol edeceğim",
                )
            )
    if names:
        one = len(names) == 1
        parts.append(
            say(
                en=(
                    f"I'll spell {_and_names(names)} the way you wrote "
                    f"{'it' if one else 'them'} and check {'it' if one else 'them'} in the "
                    "finished video"
                ),
                tr=(
                    f"{'şunu' if one else 'şunları'} senin yazdığın gibi yazıp bitmiş videoda "
                    f"kontrol edeceğim: {_and_names(names)}"
                ),
            )
        )
    elif _asks_spelling(req):
        parts.append(
            say(
                en="write out the words to spell and I'll spell them your way",
                tr="yazımına dikkat edeceğim kelimeleri tek tek yaz, senin yazdığın gibi yazayım",
            )
        )
    return _receipt(req, "partial", "; ".join(parts))


def _plans_caption_words(req: BriefRequirement, facts: PlanFacts) -> bool:
    """A phone Talking draft's captions ask about language or spelling (KRI-793)."""
    return (
        facts.caption_words_at_render
        and not facts.rendered_variant
        and not facts.editor
        and _wants_captions(req)
        and _caption_words_ask(req)
    )


def brief_caption_spellings(brief: CreativeBrief | None) -> tuple[str, ...]:
    """The words the creator wants spelled their way in the captions, from every live
    captions ask of the approved brief, in order (KRI-793).

    The phone Talking writer applies them to its caption cues (`apply_spelling_to_cues`)
    and the render-ready review checks the same list against the caption lines.
    """
    if brief is None:
        return ()
    names: list[str] = []
    for req in brief.live():
        if _wants_captions(req) and not _wants_no_captions(req):
            names.extend(_spelled_names(req) or ())
    return tuple(dict.fromkeys(names))[:_MAX_SPELLED_NAMES]


_TITLE_SOURCES_THE_CREATOR_OWNS = frozenset({"creator", "brief"})


def _check_title(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A title requirement with no exact text: met when the title is the creator's own
    words or was written from what their brief said (never a default or a model hook)."""
    if not facts.title:
        return _receipt(req, "partial", _NO_TITLE)
    if facts.title_source is None:
        return _receipt(req, "partial", _CANT_CHECK_TITLE)
    if facts.title_source in _TITLE_SOURCES_THE_CREATOR_OWNS:
        return _receipt(req, "met", None)
    return _receipt(
        req,
        "partial",
        say(
            en="I used a plain default title because none was given.",
            tr="Başlık vermediğin için sade bir varsayılan başlık kullandım.",
        ),
    )


# The render-time block of the unified phone montage (`_run_phone_unified_montage_job`):
# no video exists when a checked receipt is not met, so the creator-facing copy must not
# read like a draft summary ("I didn't add a title ...") and a title with no words has one
# typed way forward.
NO_TITLE_BLOCKED = "I couldn't make the video yet: you asked for a title but gave no words."
TITLE_WORDS_ALTERNATIVE = 'Tell me the words for the title, or say "continue without a title".'
_TITLE_WAY_FORWARD_MIXED = 'For the title, tell me the words or say "continue without a title".'
_RENDER_BLOCK_SUFFIX = "Your draft is saved. Should I try again or simplify this request?"


# The phone Voiceover worker's question when it could not place the clips on the spoken
# words (`_run_phone_narrated_job`). The worker binds no chat language, so the stored copy
# is this English text; the chat observer re-words it for a Turkish chat (KRI-592), the
# same way it re-words `render_block_recovery`.
NARRATED_ALIGNMENT_RECOVERY = (
    "I couldn't match the narration to the actions reliably. Your draft is saved. "
    "Should I try again or use a simpler clip sequence?"
)


def narrated_alignment_recovery() -> str:
    """`NARRATED_ALIGNMENT_RECOVERY` in the bound chat language."""
    return say(
        en=NARRATED_ALIGNMENT_RECOVERY,
        tr=(
            "Seslendirmeyi kliplerle güvenilir bir şekilde eşleştiremedim. Taslağın "
            "kaydedildi. Tekrar mı deneyeyim, yoksa daha basit bir klip sırası mı kullanayım?"
        ),
    )


def narrated_alignment_repair_note() -> str:
    """The "What Kria changed" line when the approved narrated edit renders without
    word-level alignment (KRI-129: it renders and says so, never refuses after approval)."""
    return say(
        en=(
            "I couldn't line each clip up with the words you say, so the clips play in order "
            "and share your voiceover evenly"
        ),
        tr=(
            "Her klibi söylediğin kelimelerle eşleştiremedim, bu yüzden klipler sırayla "
            "oynuyor ve seslendirmeni eşit paylaşıyor"
        ),
    )


# KRI-778: `_check_timing`'s sentence for a total length the draft missed. Every global
# length ask is judged against the same draft, so a brief with two of them (café M2: a freeze
# the brief scoped as a length, and "about 14 seconds") states the draft's length twice.
_DRAFT_LENGTH_RE = re.compile(  # kri667: our own length sentence, never creator words
    r"^(?:This draft is about \S+s\b.*; you asked for (?:at most )?\S+s\."
    r"|Bu taslak yaklaşık \S+ saniye\b.*; sen (?:en fazla )?\S+ saniye istedin\.)$"
)
# A sentence already ends: its stop, then any closing quotes or brackets.
_SENTENCE_END_RE = re.compile(  # kri667: punctuation only, reads no meaning
    r"[.!?…][\"'”’»)\]]*$"
)


def _as_sentence(reason: str) -> str:
    """A receipt reason as one sentence of the block message: capitalised, with its stop.

    A draft summary lists reasons one per line, where a fragment reads fine; run together in
    one message, a fragment runs into the next sentence (KRI-778). "iPhone" stays as is.
    """
    text = reason.strip().rstrip(",;:").rstrip()
    if not text:
        return text
    first = text[0]
    if first.islower() and not (len(text) > 1 and text[1].isupper()):
        dotted = first == "i" and current_reply_language() == "tr"
        text = ("İ" if dotted else first.upper()) + text[1:]
    if not _SENTENCE_END_RE.search(text):
        text += "."
    return text


def _block_sentences(reasons: Sequence[str]) -> list[str]:
    """The blocking reasons as sentences, each said once, and the draft's length said once:
    with the last length ask, the latest the creator stated (a brief keeps asks in the order
    they were said)."""
    lengths = [reason for reason in reasons if _DRAFT_LENGTH_RE.match(reason.strip())]
    kept = [
        reason
        for reason in reasons
        if reason in lengths[-1:] or not _DRAFT_LENGTH_RE.match(reason.strip())
    ]
    return [text for text in dict.fromkeys(_as_sentence(reason) for reason in kept) if text]


@dataclass(frozen=True)
class RenderBlockRecovery:
    """What the creator is told when checked receipts block a render, and the typed decline."""

    message: str
    # Set only when every blocker is one the creator resolves with a single answer.
    decline_reason: str | None = None
    field_path: str | None = None
    alternative: str | None = None


def render_block_recovery(failures: Sequence[Mapping[str, Any]]) -> RenderBlockRecovery:
    """Creator copy (+ typed decline) for the receipts that block a unified-montage render.

    A title with no words is the one blocker the creator answers directly (a
    ``needs_choice`` decline whose alternative is the typed way forward); every other
    blocker keeps the generic "try again or simplify" copy, untyped. Receipt semantics
    are untouched: only the wording shown at the block changes.
    """

    blocked = say(
        en=NO_TITLE_BLOCKED,
        tr="Videoyu henüz oluşturamadım: başlık istedin ama kelimelerini vermedin.",
    )
    alternative = say(
        en=TITLE_WORDS_ALTERNATIVE,
        tr='Başlığın kelimelerini söyle ya da "başlık olmasın" yaz.',
    )
    fallback = say(en="A requested change is missing.", tr="İstediğin bir değişiklik eksik.")
    reasons = [str(row.get("reason") or fallback) for row in failures]
    if reasons and all(is_no_title_reason(reason) for reason in reasons):
        saved = say(en="Your draft is saved.", tr="Taslağın kaydedildi.")
        return RenderBlockRecovery(
            message=f"{blocked} {saved} {alternative}",
            decline_reason="needs_choice",
            field_path="opening_title",
            alternative=alternative,
        )
    shown = " ".join(
        _block_sentences([blocked if is_no_title_reason(r) else _loc(r) for r in reasons])
    )
    suffix = say(
        en=_RENDER_BLOCK_SUFFIX,
        tr="Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa isteği sadeleştireyim mi?",
    )
    message = f"{shown} {suffix}"
    if any(is_no_title_reason(r) for r in reasons):
        # Untyped (two blockers), but the title's way forward must not be lost.
        message = f"{message} " + say(
            en=_TITLE_WAY_FORWARD_MIXED,
            tr='Başlık için kelimeleri söyle ya da "başlık olmasın" yaz.',
        )
    return RenderBlockRecovery(message=message)


def render_repair_notes(
    failures: Sequence[Mapping[str, Any]], *, skip_requirement_ids: Iterable[str] = ()
) -> list[str]:
    """ "What Kria changed" lines for the checked receipts an approved draft fell short on.

    KRI-129 (founder rule): once the creator approved the draft, Kria renders it and says
    exactly what it could not do, instead of refusing after approval with "should I try
    again or simplify?" (prod, 2026-10: 10 of 30 pre-render refusals). The questions belong
    before approval (``phone_montage_preflight``). Each line is the receipt's own reason,
    localized; a title with no words says the way forward. ``skip_requirement_ids`` are
    asks a later stage repairs and reports itself (confirmed text, the video's length).
    """

    skipped = set(skip_requirement_ids)
    reasons: list[str] = []
    for row in failures:
        if str(row.get("requirement_id")) in skipped:
            continue
        reason = str(row.get("reason") or "")
        if not reason:
            continue
        if is_no_title_reason(reason):
            reasons.append(
                say(
                    en="I left the title off because you didn't give me its words. "
                    "Tell me what it should say and I'll add it",
                    tr="Başlığı koymadım çünkü kelimelerini vermedin. "
                    "Ne yazmasını istediğini söyle, ekleyeyim",
                )
            )
        else:
            reasons.append(_loc(reason))
    return _block_sentences(reasons)


def _check_literal_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    # A rendered spoken-text receipt must prove timing, not just matching caption copy.
    if _is_voiceover_edit(facts) and _is_spoken_text_cue(req) and facts.rendered_variant:
        spoken = _spoken_text_fact(facts, req)
        if spoken is None:
            return _receipt(req, "partial", _NO_CHECKER)
        if spoken.status != "placed":
            return _spoken_text_miss(req, spoken)
        # The receipt is the only evidence that this particular text appeared at
        # the spoken cue. Captions and opening-title copy can contain the same
        # words without satisfying the timed request.
        if _contains_text(spoken.text, _fold(req.literal or "")):
            # Placement proves the word anchor, not extra duration/sequence promises.
            if set(req.facts or {}) & {
                "segmentation",
                "sequence",
                "timing",
                "overlap",
                "animation_phases",
                "duration_s",
            }:
                return _receipt(req, "partial", _NO_CHECKER)
            return _receipt(req, "met", None)
        return _receipt(req, "partial", _NO_CHECKER)
    wanted = _fold(req.literal or "")
    if req.scope == "title" and facts.title:
        # A title written from the brief may add the route to the creator's words
        # ("20k run · Arnavutköy → Eminönü"), so it only has to contain them.
        found = (
            _contains_text(facts.title, wanted)
            if facts.title_source == "brief"
            else _fold(facts.title) == wanted
        )
    else:
        found = any(_contains_text(t, wanted) for t in facts.texts) or _drawn_by_contract(
            req, facts
        )
    # Chapter lists are satisfied by their separate labels, not a joined title.
    if found or chapter_list(req.literal, facts.texts) is not None:
        # Copy presence cannot certify independently requested visual/temporal behavior.
        # Keep these explicit constraints unverified until their actual lane evidence is checked.
        # (animation, position, font_family and text_color are judged by `check_text_look`.)
        if set(req.facts or {}) & {
            "segmentation",
            "sequence",
            "timing",
            "overlap",
            "animation_phases",
            "duration_s",
        }:
            return _receipt(req, "partial", _NO_CHECKER)
        return _receipt(req, "met", None)
    if _is_spoken_text_cue(req) and facts.edit_format in {"subtitled", "talking_head"}:
        spoken = _spoken_text_fact(facts, req)
        if spoken is not None:
            # KRI-707: the phone Talking render recorded why this text isn't on screen.
            return _spoken_text_miss(req, spoken)
        if facts.phone_talking and _talking_spoken_text_lane():
            from app.kria.spoken_text_ask import requirement_spoken_text  # noqa: PLC0415

            ask = requirement_spoken_text(req)
            if ask is None and req.scope == "global":
                # KRI-791: the phone could show it; what's missing is which word to wait for.
                return _receipt(req, "not_possible", _TALKING_SPOKEN_TEXT_NO_WORD)
            if ask is not None and not facts.rendered_variant:
                # KRI-707: the phone Talking worker places it on the word itself, so the draft
                # says what it will do (like the start cut), never "a simpler version?".
                # KRI-791: and that it can't tell one voice from another.
                plan = _TALKING_SPOKEN_TEXT_ANY_VOICE if ask.speaker else _TALKING_SPOKEN_TEXT_PLAN
                pinned = _pinned_spoken_text_plan(ask, facts)
                if pinned is not None:
                    return _receipt(req, "partial", pinned)
                return _receipt(req, "partial", plan)
        # KRI-705: a Talking edit has the same gap (a pinned line has no trigger field), and the
        # reply says so plainly instead of holding the draft behind "a simpler version?".
        return _receipt(req, "not_possible", _TALKING_NO_SPOKEN_TEXT)
    if _is_spoken_text_cue(req) and _is_voiceover_edit(facts):
        spoken = _spoken_text_fact(facts, req)
        if spoken is not None:
            return _spoken_text_miss(req, spoken)
        if facts.phone_voiceover_spoken_text and not facts.rendered_variant:
            return _receipt(req, "partial", _VOICEOVER_SPOKEN_TEXT_PLAN)
        # KRI-699: a voiceover edit draws its title, captions and word-timed photos/sounds,
        # never a creator text on a spoken word, so a retry can't add it. Still the
        # creator's call (held draft), not a format limit.
        return _receipt(
            req,
            "not_possible",
            say(
                en="A voiceover edit can't show text at a word you say yet.",
                tr="Seslendirmeli düzenleme henüz söylediğin bir kelimede yazı gösteremiyor.",
            ),
        )
    return _receipt(
        req,
        "partial",
        say(en="That exact text isn't in this draft.", tr="Bu tam yazı bu taslakta yok."),
    )


def _drawn_by_contract(req: BriefRequirement, facts: PlanFacts) -> bool:
    """KRI-945: whole-video words the render's contract repair puts over the opening.

    Only a plain ask: one with a fact of its own (a position, a clip, a moment, a length;
    anything but the research ask it came from) or a spoken-word cue is not what the repair
    draws, so it keeps its own verdict.
    """
    if not facts.contract_drawn_texts:
        return False
    from app.kria.research_preview import RESEARCH_FACT  # noqa: PLC0415

    wanted = _fold(req.literal or "")
    return (
        req.scope == "global"
        and bool(wanted)
        and not _is_spoken_text_cue(req)
        and set(req.facts) <= {RESEARCH_FACT}
        and any(_fold(text) == wanted for text in facts.contract_drawn_texts)
    )


def _check_research(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """KRI-945: a researched-facts ask, judged by the research done before approval.

    Met only when the sourced explanations researched for exactly this ask are pinned; a
    research limit is the precise ``not_possible`` the creator rules on. ``None`` when
    nothing was researched for it (no evidence either way).
    """
    if facts.research is None:
        return None
    from app.kria import research_preview  # noqa: PLC0415
    from app.schemas.research import PreparedResearch  # noqa: PLC0415

    found = research_preview.research_requirement(CreativeBrief(requirements=[req]))
    if found is None:
        return None
    try:
        prepared = PreparedResearch.model_validate(facts.research)
    except ValueError:
        return None
    request = found[1]
    if (prepared.requirement_id, prepared.topic, prepared.request_quote) != (
        req.id,
        request.topic,
        request.request_quote,
    ):
        return None
    if prepared.status == "failed":
        return research_preview.blocker_receipt(prepared, req)
    if not prepared.texts:
        return None
    return _receipt(req, "met", None, verification="checked", stage="applied")


# Fact keys that name the spoken words a text line waits for (one list with the phone
# Talking worker, `spoken_text_ask`).
_SPOKEN_TEXT_CUE_KEYS = SPOKEN_TEXT_CUE_KEYS


def _is_spoken_text_cue(req: BriefRequirement) -> bool:
    """A text line asked to appear when the creator says something (KRI-699).

    The cue is read from the ask, never from the text itself ("Meet at the words" is copy).
    """
    if req.scope == "title":
        return False
    return (
        any(req.facts.get(key) for key in _SPOKEN_TEXT_CUE_KEYS)
        or _worded_spoken_cue(req)
        # KRI-791: "Show MEROPE the first time she says Merope" (whole-video text only, so a
        # shot label "the shot when she says hi" keeps its own lane).
        or (
            req.kind == "text"
            and req.scope == "global"
            and _cue_trigger_match(req, (req.description,))[2]
        )
    )


def _worded_spoken_cue(req: BriefRequirement) -> bool:
    """The ask's own description words a spoken cue ("when I say", "dediğimde")."""
    return bool(_SPOKEN_CUE_RE.search(_fold(req.description or "")))


def spoken_text_literals(brief: CreativeBrief | None) -> tuple[str, ...]:
    """KRI-692: the exact text of each live ask to show a line when the creator says a word.

    The clip planner also reads such an ask as a `caption` clip intent, which is a question
    with no clip to answer it; the planner uses these to keep it out of the clip inventory.
    """
    if brief is None:
        return ()
    return tuple(
        dict.fromkeys(
            req.literal
            for req in brief.live()
            if req.kind == "text" and req.literal and _is_spoken_text_cue(req)
        )
    )


def is_spoken_text_cue(req: BriefRequirement) -> bool:
    """A whole-video text line asked to appear when someone says something, whether or not
    its word could be read (KRI-791: a Talking render contract never pins one)."""
    return (
        req.kind == "text"
        and req.scope == "global"
        and bool(req.literal)
        and _is_spoken_text_cue(req)
    )


def _talking_spoken_text_lane() -> bool:
    """KRI-707: the phone Talking worker places texts on spoken words (rolled out)."""
    from app.services.phone_rollout import phone_subtitled_spoken_text_supported  # noqa: PLC0415

    return phone_subtitled_spoken_text_supported()


def _is_voiceover_edit(facts: PlanFacts) -> bool:
    return facts.edit_format in NARRATED_EDIT_FORMATS and facts.audio_strategy == "voiceover"


# KRI-546: two asks a finished phone montage can settle from its own evidence. Patterns run
# on `_loose_req` output (folded, no diacritics, punctuation as spaces: "don't" -> "don t").
_MEDIA_NOUN = (
    r"(?:videos?|clips?|shots?|footage|files?|uploads?|video\w*|klip\w*|cekim\w*|dosya\w*)"
)
# "If the same video is there twice, use one" / "aynı videodan iki tane varsa birini kullan":
# a copy is named AND only one should stay, so "use the same clip at the start and the end"
# never reads as a duplicate ask.
_SAME_FILE_RE = re.compile(
    r"\bduplicat\w*|\bde ?dup\w*|\bkopya\w*|\byinelen\w*|\btekrar ?(?:eden|lanan)\b"
    rf"|\bsame {_MEDIA_NOUN}|\b(?:two|2) of the same\b"
    rf"|\b{_MEDIA_NOUN} (?:that )?(?:is|are) the same\b|\bayni {_MEDIA_NOUN}"
)
_KEEP_ONE_RE = re.compile(
    r"\b(?:remove|drop|skip|delete|cut|avoid|exclude|leave out|get rid of|no|without|don t"
    r"|do not|never|once|single)\b|\b(?:only|just|keep|use|leave) (?:one|a single)\b"
    r"|\bone (?:of (?:them|each|the)|copy)\b"
    r"|\bbiri(?:ni|sini)?\b|\btek\b|\bsadece\b|\byalnizca\b|\bcikar(?:t|in|tin|sin|tsin)?\b"
    r"|\bsil(?:in|sin)?\b|\bkullanma(?:yin|sin)?\b|\bolmasin\b|\bbir (?:kere|kez)\b"
)
# "End on Elif's sentence to the camera, in her own voice" / "en sonda Elif'in kameraya
# söylediği cümleyi kendi sesiyle kullan": the closing clip's own spoken line.
_ENDING_RE = re.compile(
    r"\b(?:finish|end|close|wrap up|wrap) (?:on|with)\b|\bending (?:on|with)\b"
    r"|\bat the (?:very )?end\b|\bin the end\b|\blast\b|\bclosing\b"
    # KRI-760: "baştan sona" is "from start to finish", not an ending.
    r"|\ben son\w*|\bsonda\b|\bsonunda\b|(?<!bastan )\bsona\b|\bkapanis\w*|\bbitir\w*"
    r"|\bbitsin\b"
)
_OWN_SPEECH_RE = re.compile(
    r"\bown (?:voice|sound|audio|words)\b|\b(?:her|his|their|my|your) voice\b"
    r"|\b(?:sentence|say|says|said|saying|speaks|speaking|talks|talking)\b|\bto (?:the )?camera\b"
    r"|\bkendi ses\w*|\bcumle\w*|\bsoyledi\w*|\bsoyler\w*|\bkonus\w*|\bdedi\w*|\bkameraya\b"
    r"|\bdiyor\w*|\bderken\b"
)
# A closing photo, sticker or song, a muted ending or a voiceover is a different ask.
_NOT_OWN_SPEECH_RE = re.compile(
    r"\bmute\w*|\bsilent\b|\bno sound\b|\bwithout (?:sound|audio)\b|\bsessiz\w*|\bkapat\w*"
    r"|\bvoice ?over\b|\bnarrat\w*|\bseslendirme\w*|\bdis ses\w*"
    r"|\bmusic\b|\bsong\b|\bmuzik\w*|\bsarki\w*"
    r"|\b(?:photo|picture|image|pic|sticker|stamp|badge|logo|emoji)s?\b"
    r"|\bfotograf\w*|\bresim\w*|\bgorsel\w*|\bcikartma\w*"
)


def _check_whole_line(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Judge "let the line '...' be heard in full" on a finished phone montage (KRI-760).

    Met only when the clip that says each named line is in the edit and its cut covers the
    line, with the clips' own sound kept. A draft, or a plan that never looked for the
    line, claims nothing; a line no clip says, two clips say equally, or a cut too short
    for it is "partly", with the line named.
    """
    from app.services.phone_reaction_grounding import fold_tokens  # noqa: PLC0415

    recorded = facts.named_lines
    if not facts.rendered_montage or recorded is None:
        return _receipt(req, "partial", _NO_CHECKER)
    rows: list[NamedLineFact] = []
    for name in _line_names(req):
        row = next((r for r in recorded if fold_tokens(r.quote) == fold_tokens(name)), None)
        if row is None:
            return _receipt(req, "partial", _NO_CHECKER)
        rows.append(row)
    if not rows:
        return _receipt(req, "partial", _NO_CHECKER)
    problems: list[str] = []
    for row in rows:
        quote = f"“{row.quote}”"
        if row.status == "not_found":
            problems.append(
                say(
                    en=f"I couldn't find where you say {quote} in your clips",
                    tr=f"{quote} dediğin yeri kliplerinde bulamadım",
                )
            )
        elif row.status == "ambiguous":
            problems.append(
                say(
                    en=f"More than one clip says {quote}, so I couldn't tell which to keep whole",
                    tr=f"{quote} birden fazla klipte geçiyor, hangisini bütün bırakacağımı "
                    "anlayamadım",
                )
            )
        elif row.status == "not_held":
            problems.append(
                say(
                    en=f"The cut doesn't hold the whole line {quote}",
                    tr=f"Kesit {quote} cümlesinin tamamını içermiyor",
                )
            )
        elif row.status != "held":
            return _receipt(req, "partial", _NO_CHECKER)
    if problems:
        return _receipt(req, "partial", "; ".join(problems) + ".")
    quotes = _names(f"“{row.quote}”" for row in rows)
    if facts.source_audio_kept is False:
        return _receipt(
            req,
            "partial",
            say(
                en=f"{quotes} is in the cut, but the clips' own sound is off",
                tr=f"{quotes} kesitte var ama kliplerin kendi sesi kapalı",
            ),
        )
    return _receipt(
        req,
        "met",
        say(
            en=f"{quotes} plays whole, in the clip's own sound",
            tr=f"{quotes} baştan sona, klibin kendi sesiyle çalıyor",
        ),
    )


def _wants_one_of_duplicates(req: BriefRequirement) -> bool:
    """ "If the same video is there twice, use one": keep a single copy of a repeated file."""
    if req.kind not in ("select", "style"):
        return False
    text = _loose_req(req)
    return bool(_SAME_FILE_RE.search(text) and _KEEP_ONE_RE.search(text))


def _wants_closing_speech(req: BriefRequirement) -> bool:
    """ "End on X's sentence in her own voice": the last clip plays its own spoken line."""
    if req.kind not in (*_BEAT_KINDS, "order"):
        return False
    text = _loose_req(req)
    last_clip = req.facts.get("last_clip")
    if isinstance(last_clip, str) and last_clip.strip():
        # KRI-681: "Özel klip sırası" with last_clip "tam 20 dakika sürdü dediğim klip":
        # the line lives in the recorded last clip, not the ask's short title.
        named = req.model_copy(update={"description": last_clip, "literal": None})
        text = f"{text} {_loose_req(named)}"
    if _NOT_OWN_SPEECH_RE.search(text) or not _OWN_SPEECH_RE.search(text):
        return False
    return bool(
        _ENDING_RE.search(text)
        or req.facts.get("last_clip")
        or str(req.facts.get("position") or "").casefold() == "last"
    )


def judged_at_render(req: BriefRequirement) -> bool:
    """True for an ask only a finished phone montage's evidence settles (KRI-546).

    A unified montage's plan record cannot see these: a closing line is held while the
    plan is laid out, and a duplicate file is told by the upload fingerprints. The
    render-ready review judges them again even when the record carries a receipt.
    """
    return (
        _wants_one_of_duplicates(req)
        or _wants_closing_speech(req)
        or _wants_whole_line(req)
        # KRI-558: a look (title animation, label corner, font, colour) and a per-clip length
        # are read off the finished text lane and timeline, which a plan record never has.
        or bool(text_look_facts(req))
        # Prompt fidelity: "with animations" is a look too, named or not.
        or asks_text_animation(req)
        or clip_length_ask(req) is not None
        # KRI-689: a planned rate counts only when the finished timeline still plays it
        # (`_rendered_speeds`), and the review re-words it in the chat's language.
        or asks_clip_speed(req)
        # KRI-942: clip sound on/off is read off the recipe the phone renders.
        or requirement_sound_toggle(req) is not None
        # KRI-1027: bars/crop of the sideways clips is read off the render's framing receipt.
        or _asks_landscape_fit(req)
    )


def _asks_landscape_fit(req: BriefRequirement) -> bool:
    from app.kria.landscape_fit_ask import requirement_landscape_fit  # noqa: PLC0415

    return requirement_landscape_fit(req) is not None


_CLIP_ID_PART_RE = re.compile(r"[^0-9A-Za-z]+")


def _scope_clip_id(req: BriefRequirement, ids: Iterable[str]) -> str | None:
    """The clip a ``clip:<id>`` scope names among ``ids``, else None.

    The brief may name a clip by a short form of its id ("F0CECCF8" for
    "analysis-proxy-ios-F0CECCF8-5871-....mp4"): a whole id part of 6+ letters and digits
    that exactly one clip carries counts; anything ambiguous names no clip.
    """
    if not req.scope.startswith("clip:"):
        return None
    token = req.scope.split(":", 1)[1].strip()
    known = list(dict.fromkeys(ids))
    if token in known:
        return token
    if len(token) < 6 or not token.isalnum():
        return None
    folded = token.casefold()
    hits = [
        media_id
        for media_id in known
        if folded in {part.casefold() for part in _CLIP_ID_PART_RE.split(media_id)}
    ]
    return hits[0] if len(hits) == 1 else None


def _clip_numbers(positions: Sequence[int]) -> str:
    """[2, 3] -> "clips 2 and 3" / "2. ve 3. klipler"."""
    shown = [str(p) for p in positions]
    head, tail = shown[:-1], shown[-1]
    return say(
        en=f"clips {', '.join(head)} and {tail}" if head else f"clip {tail}",
        tr=(f"{', '.join(f'{p}.' for p in head)} ve {tail}. klipler" if head else f"{tail}. klip"),
    )


def _check_duplicates(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Judge "keep one of a repeated video" on the finished edit's clips (KRI-546).

    Met only when no two clips of the edit come from the same original file (same upload
    fingerprint). Without a fingerprint for every clip nothing is claimed.
    """
    groups = facts.duplicate_clip_positions
    if not facts.rendered_montage or groups is None:
        return _receipt(req, "partial", _NO_CHECKER)
    if not groups:
        return _receipt(
            req,
            "met",
            say(
                en="No two clips in the edit come from the same video file",
                tr="Düzenlemedeki hiçbir klip aynı video dosyasından gelmiyor",
            ),
        )
    where = "; ".join(_clip_numbers(group) for group in groups)
    extra = sum(len(group) - 1 for group in groups)
    return _receipt(
        req,
        # Some uploaded copies were left out but not all: partly done. None were: not done.
        "partial" if (facts.source_duplicate_copies or 0) > extra else "not_possible",
        say(
            en=f"The same video is in the edit more than once: {where}",
            tr=f"Aynı video düzenlemede birden fazla kez var: {where}",
        ),
    )


def _check_closing_speech(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Judge "end on X's spoken line in her own voice" on a finished montage (KRI-546).

    Met only when the clip the creator named (or, unnamed, the clip their own "last" ask
    seated) ends the edit and the plan held its whole spoken line with the camera audio
    kept, as the finished timeline shows.
    """
    ids = facts.clip_ids
    if not facts.rendered_montage or not ids:
        return _receipt(req, "partial", _NO_CHECKER)
    if req.scope.startswith("clip:"):
        target = _scope_clip_id(req, (*ids, *facts.source_clip_ids))
        if target is None:
            return _receipt(req, "partial", _NO_CHECKER)
        if target not in ids:
            return _receipt(
                req,
                "not_possible",
                say(en="That clip isn't in the edit", tr="O klip düzenlemede yok"),
            )
        if ids[-1] != target:
            return _receipt(
                req,
                "partial",
                say(
                    en="That clip is in the edit, but not at the end",
                    tr="O klip düzenlemede var ama sonda değil",
                ),
            )
    elif facts.closing_speech is None and "last" not in (facts.sequence_spots_met or ()):
        # Unnamed, and no "last" ask of the creator's landed: nothing says which clip.
        return _receipt(req, "partial", _NO_CHECKER)
    if facts.source_audio_kept is False:
        return _receipt(
            req,
            "partial",
            say(
                en="The video ends on that clip, but the clips' own sound is off",
                tr="Video o klipte bitiyor ama kliplerin kendi sesi kapalı",
            ),
        )
    quote = _quoted_ending(req, facts)
    if quote:
        # KRI-597: "end on the moment I say '...'" is met only by that line, held whole.
        return _check_quoted_ending(req, facts, quote)
    if facts.closing_speech is None or facts.closing_speech.media_id != ids[-1]:
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    "The video ends on that clip, but I couldn't confirm its whole spoken "
                    "line plays in its own sound"
                ),
                tr=(
                    "Video o klipte bitiyor ama cümlenin tamamının klibin kendi sesiyle "
                    "duyulduğunu doğrulayamadım"
                ),
            ),
        )
    return _receipt(
        req,
        "met",
        say(
            en="The video ends on that clip's whole spoken line, in its own sound",
            tr="Video, o klipteki cümlenin tamamıyla ve klibin kendi sesiyle bitiyor",
        ),
    )


def _quoted_ending(req: BriefRequirement, facts: PlanFacts) -> str:
    """The line the creator named for their ending, "" when they named none.

    Their quoted words; else (KRI-681, "tam 20 dakika sürdü dediğim klip" with no quote
    marks) the line the planner found the closing clip saying, which the plan records.
    """
    from app.services.spoken_quotes import quoted_phrases  # noqa: PLC0415

    for text in (req.description, req.facts.get("last_clip"), req.literal):
        quotes = quoted_phrases(text if isinstance(text, str) else None)
        if quotes:
            return quotes[0]
    return facts.closing_quote_text if facts.closing_quote_status else ""


def _check_quoted_ending(req: BriefRequirement, facts: PlanFacts, quote: str) -> RequirementReceipt:
    from app.services.phone_reaction_grounding import fold_tokens  # noqa: PLC0415

    held = facts.closing_speech
    if held is not None and held.quote and fold_tokens(held.quote) == fold_tokens(quote):
        return _receipt(
            req,
            "met",
            say(
                en=f"The video ends on “{quote}”, in its own sound",
                tr=f"Video “{quote}” cümlesiyle, klibin kendi sesiyle bitiyor",
            ),
        )
    if facts.closing_quote_status == "not_found":
        reason = say(
            en=f"I couldn't find where you say “{quote}”, so the video doesn't end on it",
            tr=f"“{quote}” dediğin yeri bulamadım, bu yüzden video o anda bitmiyor",
        )
    else:
        reason = say(
            en=f"The video doesn't end on “{quote}”: the closing cut doesn't hold that line",
            tr=f"Video “{quote}” cümlesiyle bitmiyor: son kesit o cümleyi içermiyor",
        )
    return _receipt(req, "partial", reason)


# KRI-624: "the long takes" are the creator's clips at least this long. Shorter ones are the
# short clips a "from the long takes" ask rules out.
LONG_TAKE_MIN_S = 10.0


def _moment_source_problem(ask: MomentAsk, facts: PlanFacts) -> str | None:
    """Why a "from the long takes" / "skip the ..." ask is not shown met, else None.

    Names the extra clips when the cuts include short ones; says so plainly when the
    cuts or clip lengths are unknown, and when the exclusion ("skip the dark ones") is
    something no receipt can see in the footage.
    """
    if ask.long_takes_only:
        lengths = dict(facts.source_durations_s or ())
        used = list(dict.fromkeys(facts.cut_clip_ids or ()))
        if not used or not lengths or any(media_id not in lengths for media_id in used):
            return say(
                en="I can't tell which clips these moments came from",
                tr="Bu anların hangi kliplerden geldiğini söyleyemiyorum",
            )
        if all(seconds < LONG_TAKE_MIN_S for seconds in lengths.values()):
            return say(
                en="I can't tell which of your clips are the long takes",
                tr="Hangi kliplerin uzun çekim olduğunu söyleyemiyorum",
            )
        short = [media_id for media_id in used if lengths[media_id] < LONG_TAKE_MIN_S]
        if short:
            every = "all " if len(used) == len(lengths) else ""
            return say(
                en=(
                    f"I used {every}{len(used)} clips, including the {len(short)} short "
                    f"{'one' if len(short) == 1 else 'ones'}"
                ),
                tr=(
                    f"{len(used)} klibin {'hepsini' if every else 'tamamını'} kullandım, "
                    f"{len(short)} kısa klip de dahil"
                ),
            )
    if ask.excludes:
        return say(
            en="I can't check which clips you wanted left out",
            tr="Hangi klipleri dışarıda bırakmak istediğini kontrol edemiyorum",
        )
    return None


def _check_moments(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """KRI-596: "the best six moments, each very short", against the finished cuts."""
    ask = moment_ask(req)
    lengths = facts.cut_lengths_s
    if ask is None or not lengths:
        return _receipt(req, "partial", _NO_CHECKER)
    longest = max(lengths)
    if ask.count is not None and len(lengths) != ask.count:
        moments = "moment" if len(lengths) == 1 else "moments"
        return _receipt(
            req,
            "partial",
            say(
                en=f"The video has {len(lengths)} {moments}, not {ask.count}",
                tr=f"Videoda {ask.count} değil {len(lengths)} an var",
            ),
        )
    if ask.short and longest > MOMENT_SHORT_MAX_S + 0.05:
        return _receipt(
            req,
            "partial",
            say(
                en=f"The longest moment runs {longest:.1f} s",
                tr=f"En uzun an {longest:.1f} sn sürüyor",
            ),
        )
    # KRI-686: "about 2 seconds each" tolerates a stretch to fit the creator's total length
    # (8 x 2 s in 20 s runs 2.5 s each), not a moment of twice that.
    if ask.each_s is not None and longest > ask.each_s * 1.5 + 0.05:
        return _receipt(
            req,
            "partial",
            say(
                en=f"The longest moment runs {longest:.1f} s, not about {ask.each_s:g} s",
                tr=f"En uzun an {longest:.1f} sn sürüyor, yaklaşık {ask.each_s:g} sn değil",
            ),
        )
    problem = _moment_source_problem(ask, facts)
    if problem:
        return _receipt(req, "partial", problem)
    return _receipt(
        req,
        "met",
        say(
            en=f"{len(lengths)} moments, the longest {longest:.1f} s",
            tr=f"{len(lengths)} an, en uzunu {longest:.1f} sn",
        ),
    )


# KRI-624: "keep the original sound" - the clips' own audio plays, with no song laid over it.
_KEEP_VERB_RE = re.compile(r"\b(?:keep|kept|preserve|retain|leave|use)\b|\btut\b|\bkoru")


def _wants_source_sound_kept(req: BriefRequirement) -> bool:
    if req.kind not in _BEAT_KINDS or _wants_beats(req) or _wants_cleanup(req):
        return False
    # A model-extracted instruction has semantic intent regardless of the creator's
    # language or whether its description repeats a keep/use verb. An unknown typed
    # intent must never fall through to the historical wording recognizer.
    if "audio_intent" in req.facts:
        return req.kind == "audio" and req.facts["audio_intent"] == "source_unchanged"
    text = _req_text(req)
    return bool(
        _BED_NAMED_RE.search(text)
        and _KEEP_VERB_RE.search(text)
        and not (_BED_QUIET_RE.search(text) or _BED_UP_RE.search(text) or _BED_MUTE_RE.search(text))
    )


def wants_lipsync(req: BriefRequirement) -> bool:
    """KRI-983: "lipsync to the uploaded song", read by `song_captions.mentions_lipsync`.

    Asks another audio reader already owns (a typed `audio_intent`, the footage's own sound, a
    voice bed, beats, speech, captions) keep their checker."""
    if (
        req.kind not in _BEAT_KINDS
        or "audio_intent" in req.facts
        or _wants_captions(req)
        or _wants_source_sound_kept(req)
        or _wants_beats(req)
        or _wants_closing(req)
        or _wants_speech(req)
        or _wants_bed_under_voice(req)
        or _wants_bed_muted(req)
        or _wants_cleanup(req)
        or wants_engine_noise_reduction(req)
        or requirement_clip_sound(req) is not None
        or requirement_sound_toggle(req) is not None
    ):
        return False
    return mentions_lipsync(" ".join(x for x in (req.description, req.literal) if x))


def _check_lipsync(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if facts.user_song_attached is None:
        return _receipt(req, "partial", _NO_CHECKER)
    if not facts.user_song_attached:
        return _receipt(req, "not_possible", _loc(_SONG_NOT_ATTACHED))
    if facts.audio_strategy != "user_song":
        return _receipt(req, "not_possible", _loc(_SONG_NOT_USED))
    if facts.song_sync != "lipsync":
        return _receipt(req, "not_possible", _loc(_SONG_IN_BACKGROUND))
    return _receipt(req, "met", None)


# KRI-700: brightening or darkening the footage itself. No draft can carry it: a strategy has
# no exposure field, and the phone plans only take the warm `golden_hour` look, which adds
# contrast and so makes near-black night clips darker, not brighter. The model that read the
# ask records it as `facts["footage_light"]` ("brighter" / "darker",
# main_creator._BRIEF_PROMPT_SECTION); the words are never re-read here (KRI-667).
_FOOTAGE_LIGHT_FACTS = frozenset({"brighter", "darker"})


def wants_light_change(req: BriefRequirement) -> bool:
    """True when the creator asks to brighten or darken the footage itself (KRI-700)."""
    stated = req.facts.get("footage_light")
    return isinstance(stated, str) and stated.strip().casefold() in _FOOTAGE_LIGHT_FACTS


def _check_light_change(req: BriefRequirement) -> RequirementReceipt:
    # A limit of every draft, not a simplification: reported as "Couldn't", never the
    # "should I make a simpler version?" question (`is_format_limit`).
    return _receipt(req, "not_possible", _LIGHT_UNAVAILABLE)


def _check_source_sound(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if facts.source_audio_kept is None or not facts.rendered_montage:
        return _receipt(req, "partial", _CANT_CHECK_MIX)
    if facts.source_audio_kept is False:
        return _receipt(
            req,
            "partial",
            say(
                en="The clips' own sound is off in this video",
                tr="Bu videoda kliplerin kendi sesi kapalı",
            ),
        )
    if facts.song_bed or facts.audio_strategy == "voiceover":
        return _receipt(
            req,
            "partial",
            say(
                en="The clips' own sound plays, but other sound plays over it",
                tr="Kliplerin kendi sesi çalıyor ama üstüne başka bir ses de çalıyor",
            ),
        )
    return _receipt(
        req,
        "met",
        say(
            en="The video keeps the clips' own sound, with nothing laid over it",
            tr="Video kliplerin kendi sesini koruyor, üstüne başka ses eklenmedi",
        ),
    )


def _same_speech_cut_quote(left: str, right: str) -> bool:
    return tuple(speech_cut_tokens(left)) == tuple(speech_cut_tokens(right))


def _explicit_speech_cut_reason(reason: str | None) -> str:
    """Translate worker-only failure codes without exposing pipeline vocabulary to creators."""
    code = str(reason or "").strip().casefold()
    if code in {"ambiguous_phrase_occurrence", "ambiguous_occurrence", "multiple_matches"}:
        return say(
            en="I found more than one matching phrase, so tell me which occurrence to cut",
            tr="Birden fazla eşleşen ifade buldum; hangisini kesmem gerektiğini söyle",
        )
    if code in {"missing_target", "target_not_found", "phrase_not_found", "not_found"}:
        return say(
            en="I couldn't find that spoken phrase in the original audio",
            tr="O konuşulan ifadeyi orijinal seste bulamadım",
        )
    if code in {"uncertain_timestamps", "overlap", "overlapping_timestamps"}:
        return say(
            en="I couldn't confidently place that phrase in the original speech",
            tr="O ifadeyi orijinal konuşmada güvenle yerleştiremedim",
        )
    if code in {"multiple_sources", "multiple_speakers"}:
        return say(
            en="Choose one speaker clip before I cut that phrase",
            tr="Bu ifadeyi kesmeden önce tek bir konuşmacı klibi seç",
        )
    if code in {"too_short", "would_remove_entire_take", "entire_take"}:
        return say(
            en="That phrase is too short to cut without removing the whole take",
            tr="Bu ifade, çekimin tamamını kaldırmadan kesilemeyecek kadar kısa",
        )
    return _loc(_SPEECH_CUT_UNPROVEN)


def _check_explicit_speech_cut(
    req: BriefRequirement, ask: SpeechCutAsk, facts: PlanFacts
) -> RequirementReceipt:
    """Only an exact, requirement-bound applied worker receipt proves the removal."""
    if facts.phone_talking and not facts.rendered_variant:
        return _receipt(req, "partial", _loc(_SPEECH_CUT_DRAFT_PLAN))
    if not facts.rendered_variant:
        return _receipt(req, "partial", _NO_CHECKER)
    matches = [
        receipt
        for receipt in facts.explicit_speech_cuts
        if req.id in receipt.requirement_ids
        and _same_speech_cut_quote(receipt.quote, ask.quote)
        and receipt.occurrence == ask.occurrence
    ]
    if any(receipt.status == "applied" for receipt in matches):
        return _receipt(req, "met", None)
    reason = next((receipt.reason for receipt in matches if receipt.reason), None)
    return _receipt(req, "not_possible", _explicit_speech_cut_reason(reason))


def _check_engine_noise_reduction(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A stationary-noise filter is helpful but never proof of isolated speech."""
    if not facts.phone_talking:
        return _receipt(req, "not_possible", _loc(_NOISE_REDUCTION_UNSUPPORTED))
    if not facts.rendered_variant:
        if engine_noise_reduction_available():
            return _receipt(req, "partial", _loc(_NOISE_REDUCTION_PLANNED))
        return _receipt(req, "not_possible", _loc(_NOISE_REDUCTION_UNAVAILABLE))
    receipt = facts.engine_noise_reduction
    if receipt is None or req.id not in receipt.requirement_ids:
        return _receipt(req, "not_possible", _loc(_NOISE_REDUCTION_MISSED))
    if receipt.status == "applied" and receipt.reason == "reduced_not_isolated":
        return _receipt(req, "partial", _loc(_NOISE_REDUCTION_APPLIED))
    return _receipt(req, "not_possible", _loc(_NOISE_REDUCTION_MISSED))


# KRI-1027: "make it all vertical" / "keep the black bars" on a montage, answered from the
# phone montage's framing receipt (`app.kria.landscape_fit_ask.judge_landscape_fit`).
_MONTAGE_FIT_FORMATS = frozenset({"montage", "day_vlog", "single_hero"})


def _landscape_framing_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    receipt = variant.get("landscape_framing")
    if not isinstance(receipt, Mapping) or not isinstance(receipt.get("clips"), list):
        return {}
    fit = variant.get("landscape_fit")
    return {
        "landscape_framing": receipt,
        "variant_landscape_fit": fit if fit in ("fit", "fill") else None,
    }


def _montage_landscape_fit(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """The receipt for a montage's bars/crop ask, or None when another check owns it
    (a Talking edit's framing, an editor turn's own diff)."""
    from app.kria.landscape_fit_ask import (  # noqa: PLC0415
        judge_landscape_fit,
        requirement_landscape_fit,
    )

    fit = requirement_landscape_fit(req)
    if fit is None or facts.phone_talking:
        return None
    rendered = facts.rendered_montage
    if facts.landscape_framing is None:
        if facts.rendered_variant or (facts.editor and not rendered):
            # A finished Talking/Voiceover render, or an editor turn (its diff names
            # what changed): their own checks answer.
            return None
        if not rendered and facts.edit_format not in _MONTAGE_FIT_FORMATS:
            return None
    verdict = judge_landscape_fit(
        fit,
        receipt=facts.landscape_framing,
        rendered=rendered,
        variant_fit=facts.variant_landscape_fit,
    )
    return _receipt(
        req,
        verdict.status,
        verdict.reason,
        verification=None if verdict.checked else "unchecked",
    )


def _has_checker(req: BriefRequirement) -> bool:
    """True when ``check_requirement`` can actually verify this requirement."""
    from app.kria.landscape_fit_ask import requirement_landscape_fit  # noqa: PLC0415

    if (
        wants_engine_noise_reduction(req)
        or requirement_speech_cut(req) is not None
        or _wants_speaker_framing(req)
        or requirement_landscape_fit(req) is not None
        or wants_light_change(req)
        or asks_freeze_frame(req)
    ):
        return True
    if req.kind == "timing" and isinstance(req.facts.get("sequence_timing"), dict):
        return True
    if _ending_clip_fact(req):
        return True  # KRI-841: judged against the plan's / render's ending video
    if requirement_clip_sound(req) is not None:
        return True  # KRI-842
    if asks_if_unclear(req):
        return True  # KRI-849: the unclear-word line (``_with_unclear_word``)
    if _timed_card_ask(req) is not None:
        return True  # an exact-timed small card, judged against the draft or the render
    if asks_clip_speed(req):
        # KRI-689: judged wherever the plan knows it cannot retime (the phone montage); a plan
        # that does not say gets the neutral `_NO_CHECKER`, which `is_judged` treats as unjudged.
        return True
    if _wants_cleanup(req) or _wants_captions(req):
        return True
    if judged_at_render(req):
        # Neutral (`_NO_CHECKER`) anywhere but a finished phone montage (KRI-546).
        return True
    if req.kind == "text_sequence" and compose_unmet_with_ops():
        return True  # 29 Ekim: judged against the fragment sequences on screen
    if req.kind == "text":
        return bool(
            req.scope in ("per_clip", "title") or req.scope.startswith("clip:") or req.literal
        )
    if req.kind == "timing":
        # "Fast but readable" has no number to check: that is "can't verify"
        # (neutral in the reply), not a failed requirement. "Keep my whole take"
        # is checkable against the edit format and clip count.
        return (
            _has_duration_target(req)
            or _wants_whole_take(req)
            or _wants_whole_text_span(req)
            or _wants_beats(req)
            or _wants_clip_timing(req)
        )
    if derive_style_ask(req) is not None:
        return True
    if req.kind == "select" and moment_ask(req) is not None:
        return True
    if _wants_source_sound_kept(req):
        return True
    if wants_lipsync(req):
        return True  # KRI-983
    if req.kind in _BEAT_KINDS:
        return (
            _wants_beats(req)
            or _wants_closing(req)
            or _wants_speech(req)
            or _wants_bed_under_voice(req)
            or _wants_bed_muted(req)
        )
    return req.kind == "order"


def _freeze_where(freeze: Mapping[str, Any], at: str) -> tuple[str, str]:
    """KRI-900: where the held frame is, said by what chose it (``(en, tr)``).

    Only a frame the creator named (the second they picked, the cut they named) is said as
    theirs; a frame inside a moment they did not pin down says who chose it.
    """
    grounding = freeze.get("grounding")
    if grounding == "creator_frame":
        return (
            f"at the second you picked ({at}s into the clip)",
            f"seçtiğin saniyede (klibin {at}. saniyesi)",
        )
    if grounding == "cut_position":
        return "on the last frame of the last shot", "son çekimin son karesinde"
    if grounding == "creator_moment":
        return (
            f"on the moment you picked, at its middle ({at}s into the clip)",
            f"seçtiğin anın ortasında (klibin {at}. saniyesi)",
        )
    if grounding == "delegated_frame":
        return (
            f"on that moment ({at}s into the clip; you left the exact frame to me)",
            f"o anda (klibin {at}. saniyesi; tam kareyi bana bıraktın)",
        )
    # A record from before KRI-900 (the moment's middle, never pinned down).
    return f"on that moment ({at}s into the clip)", f"o anda (klibin {at}. saniyesi)"


def _clip_list(positions: Sequence[int], *, every: bool) -> tuple[str, str]:
    """("Clips 1, 2 and 3", "1., 2. ve 3. klipler") for the receipt's subject."""
    numbers = [str(int(position)) for position in positions]
    if every or not numbers:
        return "Every video clip", "Her video klibi"
    if len(numbers) == 1:
        return f"Clip {numbers[0]}", f"{numbers[0]}. klip"
    en = ", ".join(numbers[:-1]) + f" and {numbers[-1]}"
    tr = ", ".join(f"{number}." for number in numbers[:-1]) + f" ve {numbers[-1]}."
    return f"Clips {en}", f"{tr} klipler"


def _check_clip_speed(req: BriefRequirement, row: Mapping[str, Any]) -> RequirementReceipt:
    """KRI-689: what the phone montage plan did with "speed the river shots up 2x".

    "met" only for an applied row (read off the plan record and, on a finished video, its
    timeline); the reason names the clips and the rate. A rate held at the native bound, or
    lowered because the footage ran out, is "partial" with both numbers: never "met" for a
    different speed. The locked cases are plain limits; a clip nobody could name is the
    creator's call.
    """
    status = str(row.get("status") or "")
    rate = _finite_number(row.get("rate"))
    if status in {"applied", "replaced"} and rate is not None:
        positions = [
            int(position)
            for position in row.get("cut_positions") or []
            if isinstance(position, (int, float)) and not isinstance(position, bool)
        ]
        subject, subject_tr = _clip_list(positions, every=row.get("grounding") == "all_clips")
        speed = f"{rate:g}x"
        plural = len(positions) != 1 and row.get("grounding") != "all_clips"
        plays = "play" if plural else "plays"
        if status == "replaced":
            return _receipt(
                req,
                "met",
                say(
                    en=f"{subject} {plays} at {speed}, as you asked later",
                    tr=f"{subject_tr} sonra istediğin gibi {speed} hızda oynuyor",
                ),
                verification="checked",
            )
        requested = _finite_number(row.get("rate_requested"))
        if requested is not None and abs(requested - rate) > 0.001:
            if row.get("footage_limited"):
                reason = say(
                    en=(
                        f"{subject} {plays} at {speed}, not the {requested:g}x you asked for: "
                        "the clip ran out of footage"
                    ),
                    tr=(
                        f"{subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: "
                        "klibin görüntüsü yetmedi"
                    ),
                )
            else:
                faster = requested > rate
                reason = say(
                    en=(
                        f"{subject} {plays} at {speed}, not the {requested:g}x you asked for: "
                        f"a clip plays {'at most' if faster else 'at least'} {speed}"
                    ),
                    tr=(
                        f"{subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: "
                        f"bir klip en {'fazla' if faster else 'az'} {speed} hızda oynayabiliyor"
                    ),
                )
            return _receipt(req, "partial", reason, verification="checked")
        reason = say(en=f"{subject} {plays} at {speed}", tr=f"{subject_tr} {speed} hızda oynuyor")
        if not row.get("rate_stated"):
            reason = say(
                en=f"{reason}; you didn't say how fast, so I used {speed}",
                tr=f"{reason}; ne kadar hızlı söylemediğin için {speed} kullandım",
            )
        return _receipt(req, "met", reason, verification="checked")
    if status == "locked":
        reason = str(row.get("reason") or "")
        if reason == "user_song":
            return _receipt(req, "not_possible", _NO_CLIP_SPEED_SONG)
        if reason == "frozen":
            return _receipt(req, "not_possible", _NO_SPEED_ON_FREEZE)
        return _receipt(req, "not_possible", _NO_CLIP_SPEED)
    if status == "ramp":
        return _receipt(req, "not_possible", _NO_SPEED_RAMP)
    if status == "photo":
        return _receipt(req, "not_possible", _NO_PHOTO_SPEED)
    if status == "dropped":
        return _receipt(req, "not_possible", _SPEED_DROPPED)
    if status == "no_footage":
        return _receipt(req, "not_possible", _SPEED_NO_FOOTAGE)
    return _receipt(req, "not_possible", _SPEED_NOT_GROUNDED)


def _check_freeze(req: BriefRequirement, freeze: Mapping[str, Any]) -> RequirementReceipt:
    """KRI-828: "met" only when the plan holds a frame for this ask (``status == "held"``,
    read off the compiled plan record and, on a finished video, its timeline). KRI-900: the
    reason says who chose the held frame, and a moment nobody pinned down holds nothing."""
    status = freeze.get("status")
    hold = _finite_number(freeze.get("hold_s"))
    frame = _finite_number(freeze.get("frame_source_s"))
    if status == "held" and hold is not None and frame is not None:
        seconds = f"{hold:g}"
        where, where_tr = _freeze_where(freeze, f"{frame:.1f}")
        basis = freeze.get("hold_basis")
        previous = _finite_number(freeze.get("previous_hold_s"))
        if freeze.get("corrected_by") and not freeze.get("hold_requested_s"):
            # KRI-899: a later ask changed this freeze; its receipt names what now holds.
            reason = say(
                en=f"Freezes the frame for {seconds}s {where}, as you asked later",
                tr=f"Kare {seconds} sn donuyor, {where_tr}, sonra istediğin gibi",
            )
            return _receipt(req, "met", reason, verification="checked")
        if basis in {"longer", "shorter"} and previous is not None:
            longer = basis == "longer"
            reason = say(
                en=(
                    f"Freezes the frame for {seconds}s {where}; "
                    f"you didn't say how long, so I made it {'longer' if longer else 'shorter'}"
                    f" than {previous:g}s"
                ),
                tr=(
                    f"Kare {seconds} sn donuyor, {where_tr}; süre "
                    f"söylemediğin için {previous:g} sn'den "
                    f"{'uzun' if longer else 'kısa'} tuttum"
                ),
            )
            return _receipt(req, "met", reason, verification="checked")
        if freeze.get("hold_stated"):
            reason = say(
                en=f"Freezes the frame for {seconds}s {where}",
                tr=f"Kare {seconds} sn donuyor, {where_tr}",
            )
        else:
            reason = say(
                en=(
                    f"Freezes the frame for {seconds}s {where}; "
                    "you didn't say how long, so I held it for one second"
                ),
                tr=(
                    f"Kare {seconds} sn donuyor, {where_tr}; "
                    "süre söylemediğin için bir saniye tuttum"
                ),
            )
        requested = _finite_number(freeze.get("hold_requested_s"))
        if requested is not None and abs(requested - hold) > 0.001:
            # The creator's number was outside what a freeze can hold: say so, never "met"
            # for a different length.
            reason = say(
                en=(
                    f"Freezes the frame for {seconds}s {where}, "
                    f"not the {requested:g}s you asked for: a freeze holds "
                    f"{'at most' if requested > hold else 'at least'} {seconds}s"
                ),
                tr=(
                    f"Kare {seconds} sn donuyor, {where_tr}, istediğin "
                    f"{requested:g} sn değil: bir donma en "
                    f"{'fazla' if requested > hold else 'az'} {seconds} sn sürebiliyor"
                ),
            )
            return _receipt(req, "partial", reason, verification="checked")
        return _receipt(req, "met", reason, verification="checked")
    if status == "declined":
        # KRI-900: the creator answered "No freeze" before approval.
        return _receipt(
            req,
            "met",
            say(en="No freeze, as you chose", tr="Seçtiğin gibi donma yok"),
            verification="checked",
        )
    if status == "needs_frame" and freeze.get("position") == "first":
        return _receipt(req, "not_possible", _FREEZE_FIRST_FRAME)
    moment = freeze.get("moment_source_s")
    if (
        status == "needs_frame"
        and isinstance(moment, list)
        and len(moment) == 2
        and all(_finite_number(v) is not None for v in moment)
    ):
        start, end = (f"{float(v):.1f}" for v in moment)
        middle = f"{(float(moment[0]) + float(moment[1])) / 2:.1f}"
        return _receipt(
            req,
            "not_possible",
            say(
                en=(
                    f"{_FREEZE_NEEDS_FRAME} ({start}–{end}s into the clip) but not the exact "
                    "frame to hold, so it plays normally. Tell me the second to freeze on, "
                    f'like "freeze at {middle} seconds"'
                ),
                tr=(
                    f"{_REASON_TR[_FREEZE_NEEDS_FRAME]} (klibin {start}–{end}. saniyeleri) ama "
                    "tutulacak tam kareyi bilmiyorum, o yüzden normal oynuyor. Hangi saniyede "
                    f'donsun, söyle; örneğin "{middle}. saniyede dondur"'
                ),
            ),
        )
    if status in {"not_grounded", "needs_frame"}:
        return _receipt(req, "not_possible", _FREEZE_NOT_GROUNDED)
    return _receipt(req, "not_possible", _NO_FREEZE_FRAME)


def _check_select_typed(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """A `select` ask the finished timeline proves or disproves (KRI-994); None = not judged.

    ``stage="checked"`` marks the receipt as judged (see ``is_judged``): there is no
    word-based checker behind a plain `select`, only the typed link and the timeline.
    """
    if only_video_ask(req):
        if facts.timeline_non_video_ids is None:
            return None
        if facts.timeline_non_video_ids:
            return _receipt(
                req,
                "partial",
                say(en=_PHOTO_IN_CLIPS_ONLY, tr=_REASON_TR[_PHOTO_IN_CLIPS_ONLY]),
                verification="checked",
                stage="checked",
            )
        return _receipt(req, "met", None, verification="checked", stage="checked")
    fact = next((f for f in facts.exclusions if f.requirement_id == req.id), None)
    if fact is None or not fact.complete or not fact.media_ids or not facts.clip_ids:
        return None
    if any(media_id in facts.clip_ids for media_id in fact.media_ids):
        return _receipt(
            req,
            "partial",
            say(en=_SKIPPED_CLIP_PLAYS, tr=_REASON_TR[_SKIPPED_CLIP_PLAYS]),
            verification="checked",
            stage="checked",
        )
    return _receipt(req, "met", None, verification="checked", stage="checked")


def _check_requirement_with_protection(
    req: BriefRequirement, facts: PlanFacts
) -> RequirementReceipt:
    if facts.protected_text is not None and any(r[0] == req.id for r in facts.protected_text):
        # KRI-827: "no text on her face or on the plate" is judged by the render's own
        # measurement of every text against every named target. Any other ask on the same
        # requirement ("brighten the clips and no text on the plate") keeps its own checker;
        # the protected-text verdict folds into it, never replaces it.
        from app.kria.protected_targets import KEEP_TEXT_OFF_KEY  # noqa: PLC0415

        rest = req.model_copy(
            update={"facts": {k: v for k, v in req.facts.items() if k != KEEP_TEXT_OFF_KEY}}
        )
        base = _check_requirement(rest, facts) if _has_checker(rest) else _receipt(req, "met", None)
        return _with_protected_text(req, facts, base)
    return _check_requirement(req, facts)


def _unclear_clock(rows: Sequence[Mapping[str, Any]]) -> str:
    times = [
        _clock(float(row["start_s"])) for row in rows if isinstance(row.get("start_s"), int | float)
    ]
    return say(en=" and ".join(dict.fromkeys(times)), tr=" ve ".join(dict.fromkeys(times)))


def _unclear_word_line(state: Mapping[str, Any]) -> str | None:
    """The one line a Talking draft / finished video says about the word the creator asked to be
    asked about (KRI-849), from ``PlanFacts.unclear_words``. It never quotes a heard word: only
    the times, and the words the creator themselves gave."""
    kind = state.get("state")
    if kind == "agree":
        return _loc(_UNCLEAR_AGREE)
    if kind == "not_comparable":
        return _loc(_UNCLEAR_UNSURE) + say(
            en=": tell me the word, or say leave it out",
            tr=": kelimeyi söyle ya da çıkar de",
        )
    if kind == "error":
        return _loc(_UNCLEAR_ERROR) + say(
            en=", so have a look at the captions", tr=", o yüzden altyazılara bir göz at"
        )
    if kind != "applied":
        return None
    finished = state.get("finished") is True
    rows = [row for row in state.get("spans") or [] if isinstance(row, Mapping)]
    left_out = [row for row in rows if row.get("action") == "omitted"]
    written = [row for row in rows if row.get("action") == "written"]
    parts: list[str] = []
    if left_out:
        prefix = _UNCLEAR_OMIT_DONE if finished else _UNCLEAR_OMIT_PLAN
        times = _unclear_clock(left_out)
        # Before approval the time is the SOURCE clip's; after render it is the edited video's
        # (an opening or speech cut moves the captions).
        parts.append(
            say(
                en=f"{prefix} at {times} in the video"
                if finished
                else f"{prefix} around {times} in your clip",
                tr=f"{_REASON_TR[prefix]} (videoda {times})"
                if finished
                else f"{_REASON_TR[prefix]} (klibinde yaklaşık {times})",
            )
        )
    for row in written:
        prefix = _UNCLEAR_WRITE_DONE if finished else _UNCLEAR_WRITE_PLAN
        text, times = str(row.get("text") or ""), _unclear_clock([row])
        parts.append(
            say(
                en=f"{prefix}, \u201c{text}\u201d, at {times} in the video"
                if finished
                else f"{prefix}, \u201c{text}\u201d, around {times} in your clip",
                tr=f"{_REASON_TR[prefix]}: \u201c{text}\u201d (videoda {times})"
                if finished
                else f"{_REASON_TR[prefix]}: \u201c{text}\u201d (klibinde yaklaşık {times})",
            )
        )
    return "; ".join(parts) or None


def _with_unclear_word(
    req: BriefRequirement, facts: PlanFacts, receipt: RequirementReceipt
) -> RequirementReceipt:
    """``receipt`` with the unclear-word line (KRI-849) on the requirement that carries the ask.

    A captions row that is already judged (language / spellings) keeps its verdict and gains the
    sentence; a row that only holds the ask (no checker of its own) gets the line as its reason,
    never the placeholder "I can't verify this one automatically yet."
    """
    line = _unclear_word_line(facts.unclear_words or {})
    if line is None:
        return receipt
    if (facts.unclear_words or {}).get("state") == "error":
        # Never "met". When nothing else is wrong the line leads (its registered prefix keeps the
        # receipt a limit, not a "simpler version?" miss); a real miss keeps its place and verdict.
        if receipt.status != "met" and receipt.reason and receipt.reason not in _NEUTRAL_REASONS:
            return receipt.model_copy(update={"reason": f"{receipt.reason}; {line}"[:300]})
        rest = receipt.reason if receipt.reason and receipt.reason not in _NEUTRAL_REASONS else ""
        return _receipt(req, "partial", f"{line}; {rest}" if rest else line)
    if receipt.reason and receipt.reason not in _NEUTRAL_REASONS:
        sep = " " if receipt.reason.endswith(".") else "; "
        return receipt.model_copy(update={"reason": f"{receipt.reason}{sep}{line}"[:300]})
    state = facts.unclear_words or {}
    met = facts.rendered_variant and state.get("state") != "not_comparable"
    return _receipt(req, "met" if met else "partial", line)


def _check_sequence_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    from app.kria.editor_sequence_timing import sequence_timing_digest

    policy = req.facts["sequence_timing"]
    ids = policy.get("target_ids")
    if not facts.editor or not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    by_id = {}
    for identifier, _role, start, end in facts.text_spans:
        if identifier in ids:
            if identifier in by_id:
                return _receipt(req, "partial", _CANT_CHECK_TIMING)
            by_id[identifier] = (start, end)
    if any(identifier not in by_id for identifier in ids):
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    windows = [by_id[identifier] for identifier in ids]
    if policy.get("mode") == "absolute":
        digest = sequence_timing_digest((identifier, *by_id[identifier]) for identifier in ids)
        return _receipt(
            req,
            "met" if digest == policy.get("timing_digest") else "partial",
            None if digest == policy.get("timing_digest") else _CANT_CHECK_TIMING,
        )
    gaps = [right[0] - left[1] for left, right in zip(windows, windows[1:])]
    gap_policy = policy.get("gap_policy", "scale")
    gaps_valid = gap_policy in {"scale", "close", "fixed"}
    if gap_policy == "close":
        gaps_valid = all(abs(gap) <= 0.000002 for gap in gaps)
    elif gap_policy == "fixed":
        expected_gaps = policy.get("gaps_s")
        gaps_valid = (
            isinstance(expected_gaps, list)
            and len(expected_gaps) == len(gaps)
            and all(
                isinstance(expected, (int, float))
                and not isinstance(expected, bool)
                and math.isfinite(expected)
                and abs(gap - expected) <= 0.000002
                for gap, expected in zip(gaps, expected_gaps)
            )
        )
    total = facts.duration_s
    bounded = policy.get("mode") == "range"
    start = policy.get("start_s") if bounded else 0.0
    end = policy.get("end_s") if bounded else total
    bounds_valid = all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in (start, end)
    )
    valid = (
        gaps_valid
        and policy.get("mode") in {"fill", "range"}
        and total is not None
        and bounds_valid
        and 0 <= start < end <= total + 0.000002
        and abs(windows[0][0] - start) <= 0.000002
        and abs(windows[-1][1] - end) <= 0.000002
        and all(right - left >= 0.25 - 0.000002 for left, right in windows)
        and all(left[1] <= right[0] + 0.000002 for left, right in zip(windows, windows[1:]))
    )
    reason = (
        say(
            en=(
                f"Retimed {len(ids)} words in sequence across {total:g}s, "
                "keeping their wording and appearance."
            ),
            tr=(
                f"{len(ids)} kelimeyi metin ve görünümünü koruyarak "
                f"{total:g} saniyeye sırayla yaydım."
            ),
        )
        if valid
        else _CANT_CHECK_TIMING
    )
    if valid and bounded:
        reason = say(
            en=f"Retimed {len(ids)} words in sequence from {start:g}s to {end:g}s.",
            tr=f"{len(ids)} kelimeyi {start:g} ile {end:g} saniye arasında sırayla yaydım.",
        )
    return _receipt(req, "met" if valid else "partial", reason)


def check_requirement(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if req.kind == "timing" and isinstance(req.facts.get("sequence_timing"), dict):
        return _check_sequence_timing(req, facts)
    if facts.phone_text and (undrawable := undrawable_phone_text(req)) is not None:
        # KRI-1024: no phone font draws it, whatever else the plan does with this text.
        return undrawable
    receipt = _check_requirement_with_protection(req, facts)
    if facts.unclear_words is not None and asks_if_unclear(req):
        return _with_unclear_word(req, facts, receipt)
    return receipt


def _check_text_sequence(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """The creator's words are shown a piece at a time (29 Ekim).

    Met only when a complete fragment sequence on screen reads as exactly those words. The
    sentence sitting in one title is NOT the ask. A draft that has not composed the sequence
    yet says nothing (neutral, never a question): the editor ops compose it, the creator is
    not asked to settle for a static title.
    """
    wanted = _fold(req.literal or "")
    if wanted and any(_fold(text) == wanted for text in facts.sequence_texts):
        return _receipt(req, "met", None)
    return _receipt(req, "partial", _NO_CHECKER)


def text_sequence_shown(req: BriefRequirement, facts: PlanFacts) -> bool:
    """Whether a complete fragment sequence on screen reads as the requirement's words.

    The same test as the receipt above, without the compose flag: the creation text pass
    composes a live ``text_sequence`` whatever the flag says, so it never skips one that only
    sits on screen as a static title.
    """
    return _check_text_sequence(req, facts).status == "met"


def _check_requirement(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if req.kind == "text_sequence" and compose_unmet_with_ops():
        return _check_text_sequence(req, facts)
    if wants_engine_noise_reduction(req):
        # This owns only the explicit engine-noise request.  Bed/mix controls keep their
        # existing checker and cannot be satisfied by a noise filter.
        return _check_engine_noise_reduction(req, facts)
    if (facts.phone_talking or facts.explicit_speech_cuts) and (
        speech_cut := requirement_speech_cut(req)
    ) is not None:
        # This must own a timing/select row before generic timing sees its ordinal as ordinary
        # timing.  A receipt is source-space evidence, not a claim based on captions.
        return _check_explicit_speech_cut(req, speech_cut, facts)
    clip_sound = requirement_clip_sound(req)
    if clip_sound is not None:
        # KRI-842: a recorded fact owns the ask, whatever words it is worded in.
        return _check_narrated_clip_sound(req, clip_sound, facts)
    sound_toggle = requirement_sound_toggle(req)
    if sound_toggle is not None:
        # KRI-942: "clip sound off except that last line": the recorded on/off facts own it.
        return _check_sound_toggle(req, sound_toggle, facts)
    if _wants_opening_cut(req) and facts.edit_format in {"subtitled", "talking_head"}:
        # "Cut the intro, start the video at '...'": a quoted start line owns the sentence
        # (and any pauses it names); every other speech edit keeps its usual path.
        return _check_opening_cut(req, facts)
    if (
        _wants_cleanup(req)
        and facts.edit_format in _SPEECH_FORMATS
        and (not facts.editor or facts.rendered_variant)
    ):
        # "Cut out the long pauses ... keep it under 45 s" on a Talking or voiceover
        # edit: the cleanup ask owns the sentence; the length it names is judged
        # inside it. Elsewhere the sentence takes its kind's usual path.
        return _check_speech_cleanup(req, facts)
    montage_fit = _montage_landscape_fit(req, facts)
    if montage_fit is not None:
        # KRI-1027: a montage's "make it all vertical" / "keep the black bars"; a Talking
        # edit's framing ask keeps its own check below.
        return montage_fit
    if _wants_speaker_framing(req):
        return _check_speaker_framing(req, facts)
    if (
        asks_freeze_frame(req)
        and req.id in facts.freeze_frames
        and (facts.rendered_variant or not facts.editor)
    ):
        # KRI-828: the plan carried this freeze (or says why it could not place it).
        return _check_freeze(req, facts.freeze_frames[req.id])
    if _asks_ending_clip(req, facts):
        # KRI-841: "add the X video at the very end with its own sound" is judged against the
        # ending video the plan (or the finished render) holds, before the order or
        # closing-photo checks can mistake it for either.
        return _check_ending_clip(req, facts)
    if _ending_clip_fact(req) and req.kind != "order":
        # An ending video exists only on a Talking edit (claimed above). `_has_checker` says
        # yes to the fact, so every other format answers neutral here instead of lending the
        # ask to the closing-photo check.
        return _receipt(req, "partial", _NO_CHECKER)
    if asks_freeze_frame(req) and (facts.rendered_variant or not facts.editor):
        # KRI-777: a draft or a finished render never holds a frame, so the draft's reply and
        # approval card say "Couldn't" instead of the model's summary promising it (KRI-700's
        # rule), and its seconds are never judged as the video's length. An editor turn keeps
        # its own receipts (the before/after diff names what its ops changed).
        return _receipt(req, "not_possible", _NO_FREEZE_FRAME)
    if (
        asks_clip_speed(req)
        and facts.clip_speeds is not None
        and req.id in facts.clip_speeds
        and (facts.rendered_variant or facts.rendered_montage or not facts.editor)
    ):
        # KRI-689: the phone montage plan carried this speed ask (or says why it could not).
        return _check_clip_speed(req, facts.clip_speeds[req.id])
    if facts.speed_unsupported and asks_clip_speed(req):
        # KRI-689: a plan that could not retime (a record from before speed asks were planned,
        # or a song montage's). The finished video's receipt says "Couldn't", never "have a
        # look" at a speed that is not there.
        return _receipt(
            req, "not_possible", _NO_CLIP_SPEED_SONG if facts.song_bed else _NO_CLIP_SPEED
        )
    if wants_light_change(req) and (facts.rendered_variant or not facts.editor):
        # KRI-700: a draft or a finished render never brightens the footage. An editor turn
        # keeps its own receipts (the before/after diff names what its ops changed).
        return _check_light_change(req)
    if facts.rendered_montage:
        # KRI-546: a finished phone montage shows which files repeat and whether the
        # closing clip's own line plays whole; every other plan keeps its usual path.
        if _wants_one_of_duplicates(req):
            return _check_duplicates(req, facts)
        if _wants_closing_speech(req):
            if req.kind == "order":
                # "End on Elif saying it": the seat is an order verdict first.
                seated = _check_order(req, facts)
                if seated.status != "met":
                    return seated
            return _check_closing_speech(req, facts)
    if req.kind == "select":
        typed = _check_select_typed(req, facts)
        if typed is not None:
            return typed
    if _timed_card_ask(req) is not None and (facts.timed_cards is not None or not facts.editor):
        # An exact-timed small card: the recorded fact owns the ask (a draft read against its
        # manifest, or a finished phone render's own `phone_timed_card_receipt`); an editor
        # turn with no such receipt keeps its before/after diff.
        return _check_timed_cards(req, facts)
    if _wants_whole_line(req):
        return _check_whole_line(req, facts)
    if req.kind == "text":
        researched = None if req.literal else _check_research(req, facts)
        if researched is not None:
            if researched.status == "met" and asks_text_animation(req):
                return _research_with_animation(req, facts, researched)
            return researched
        if req.scope == "per_clip" or req.scope.startswith("clip:"):
            return _with_text_look(req, facts, _check_per_clip_text(req, facts))
        if req.literal:
            return _with_text_look(req, facts, _check_literal_text(req, facts))
        if req.scope == "title":
            return _with_text_look(req, facts, _check_title(req, facts))
        if asks_text_animation(req):
            # "With animations, explain ...": a whole-video text ask with no words to match.
            return _check_text_animation(req, facts)
    elif req.kind == "order":
        return _check_order(req, facts)
    elif req.kind == "timing":
        if _wants_beats(req):
            return _check_reaction_beats(req, facts)
        return _check_timing(req, facts)
    elif (
        req.kind == "style"
        and facts.editor
        # A rendered variant keeps `editor=True` for its text lanes but also carries the
        # render's own pop-in receipt and mix level (`plan_facts_from_phone_variant`): judge
        # those asks.
        and not (
            (facts.reaction_beats is not None or _sequence_problem(req, facts) is not None)
            and (_wants_beats(req) or _closing_photo_ask(req, facts))
        )
        and not (_wants_bed_under_voice(req) or _wants_bed_muted(req))
        # KRI-541: "clean captions" on a rendered variant is judged against its captions.
        and not (facts.rendered_variant and _wants_captions(req))
    ):
        return _check_style(req, facts)
    elif wants_lipsync(req):
        return _check_lipsync(req, facts)
    elif req.kind in _BEAT_KINDS and _wants_speech(req) and facts.speech_sections is not None:
        return _check_speech_excerpts(req, facts)
    elif req.kind in _BEAT_KINDS and (_wants_beats(req) or _closing_photo_ask(req, facts)):
        return _check_reaction_beats(req, facts)
    elif req.kind in _BEAT_KINDS and (_wants_bed_under_voice(req) or _wants_bed_muted(req)):
        return _check_voice_bed(req, facts)
    elif req.kind == "select" and moment_ask(req) is not None:
        return _check_moments(req, facts)
    elif _wants_source_sound_kept(req):
        return _check_source_sound(req, facts)
    if _plans_caption_words(req, facts):  # KRI-793: say before approval what the render does
        return _caption_words_plan(req, facts)
    if _judges_caption_words(req, facts):  # KRI-549: language / names on a finished render
        return _check_caption_words(req, facts)
    if _wants_captions(req):
        return _check_captions(req, facts)
    return _receipt(req, "partial", _NO_CHECKER)


# KRI-190: requirement kinds the unified montage planner settles at render time. Its
# receipts are built from the plan it actually made, so a draft-time check of these is
# premature: the strategy draft has no per-clip text, order or timing yet, and would
# report "None of the 14 clips got its own text" for a video that then gets 14 labels.
UNIFIED_SETTLED_KINDS = frozenset({"text", "order", "timing"})


# Approval turns `audio_strategy` into the item's audio mode: only these leave the
# voiceover lane, and the worker takes the unified planner only outside it. KRI-374:
# `user_song` (audio_mode "song") also runs through the unified montage planner.
_NON_VOICEOVER_AUDIO = frozenset({"original_audio", "licensed_music", "user_song"})


def defers_to_unified_montage(
    *,
    creator_id: object,
    edit_format: object,
    audio_strategy: object,
    clip_paths: Iterable[object] = (),
) -> bool:
    """True when this draft will render through the unified phone montage planner.

    Mirrors the worker's own choice so the two cannot leave a requirement judged nowhere:
    a phone job (some clip is a phone analysis proxy and the account is enrolled), a
    montage-family format, and no voiceover lane (KRI-220: no flag). Approval
    maps `audio_strategy` to the audio mode (`original_audio`/`licensed_music` leave the
    voiceover lane; anything else is the voiceover lane or a `voiceover_required` refusal),
    so an absent or voiceover-ish strategy is conservatively not deferred.
    """
    from app.agents._schemas.edit_format import GUIDED_EDIT_FORMATS  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415

    return bool(
        str(audio_strategy or "") in _NON_VOICEOVER_AUDIO
        and str(edit_format or "") in GUIDED_EDIT_FORMATS
        and any(is_analysis_proxy_path(str(path)) for path in clip_paths or ())
        and settings.phone_rendering_for(creator_id)
    )


def defers_to_narrated_render(
    *,
    creator_id: object,
    edit_format: object,
    audio_strategy: object,
    clip_paths: Iterable[object] = (),
) -> bool:
    """True when this draft will render through the phone Voiceover compiler (KRI-533).

    Mirrors the worker's dispatch: a narrated-family format with the voiceover lane
    (`audio_strategy == "voiceover"`, i.e. a recorded voiceover; self-narration goes to
    the subtitled / Talking writers), phone clips, an enrolled account and a phone
    deployment that renders narrated edits now. That writer records
    `assembly_plan["narrated_alignment"]` and judges first/last order, clip timing and
    caption language from it, so the text-free draft must not.
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415
    from app.services.phone_rollout import phone_render_supported_formats  # noqa: PLC0415

    fmt = str(edit_format or "")
    return bool(
        str(audio_strategy or "") == "voiceover"
        and fmt in NARRATED_EDIT_FORMATS
        and any(is_analysis_proxy_path(str(path)) for path in clip_paths or ())
        and settings.phone_rendering_for(creator_id)
        and fmt in phone_render_supported_formats()
    )


def _settled_by_narrated_render(req: BriefRequirement, strategy: Mapping[str, Any]) -> bool:
    """The asks a rendered Voiceover record can judge and a draft cannot (see above)."""
    if req.kind == "text" and _is_spoken_text_cue(req):
        from app.services.phone_rollout import (
            phone_voiceover_spoken_text_supported,  # noqa: PLC0415
        )

        return phone_voiceover_spoken_text_supported(require_client=False)
    if req.kind == "order":
        # A strategy that already names its ordering basis can judge a basis ask itself.
        return not strategy.get("ordering_basis")
    if req.kind == "timing":
        return _wants_clip_timing(req)
    return req.kind == "style" and asks_caption_language(req)


def defers_text_to_phone(*, creator_id: object, clip_paths: Iterable[object] = ()) -> bool:
    """True when this draft renders on the phone, whatever its format (KRI-1024).

    The dispatch gate's own test (a phone analysis proxy and an enrolled account), so every
    phone writer counts: montage, Talking, Narrated and a voiceover montage all style their
    text through the shared phone pass and draw it with the phone's fonts.
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415

    return bool(
        any(is_analysis_proxy_path(str(path)) for path in clip_paths or ())
        and settings.phone_rendering_for(creator_id)
    )


def undrawable_phone_text(req: BriefRequirement) -> RequirementReceipt | None:
    """KRI-1024: a checked "Couldn't" for a creator literal no curated phone font draws.

    Names the text and the characters. Color emoji are the phone's own emoji face and never
    count. The phone's text pass refuses such text after approval, and the phone painter
    accepts no fallback face but Apple Color Emoji, so the draft says it here instead.
    """
    if req.kind not in {"text", "text_sequence"} or not req.literal:
        return None
    from app.pipeline.phone_text_style import undrawable_message, undrawable_text  # noqa: PLC0415

    found = undrawable_text([req.literal])
    if found is None:
        return None
    return _receipt(
        req, "not_possible", undrawable_message(*found), verification="checked", stage="checked"
    )


# KRI-549: formats the phone Talking writer (`_run_phone_subtitled_job`) renders when the
# creator's own speech is the spine (a self-narrated Voiceover item lands there too).
_PHONE_CAPTION_FORMATS = frozenset({"subtitled", "talking_head", *NARRATED_EDIT_FORMATS})


def defers_caption_words_to_phone_render(
    *,
    creator_id: object,
    edit_format: object,
    audio_strategy: object,
    clip_paths: Iterable[object] = (),
) -> bool:
    """True when this draft renders through the phone Talking writer (KRI-549).

    That writer persists every caption line and the captions' language on the variant, so
    the render-ready review judges a caption-language or name-spelling ask a text-free draft
    cannot. Mirrors the dispatch gate: phone clips, an enrolled account, phone `subtitled`
    rendering on, and no voiceover lane (a recorded voiceover goes to the Voiceover writer).
    The draft itself says what that writer will do with such an ask (KRI-793,
    `PlanFacts.caption_words_at_render`).
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415
    from app.services.phone_rollout import phone_render_supported_formats  # noqa: PLC0415

    return bool(
        str(edit_format or "") in _PHONE_CAPTION_FORMATS
        and str(audio_strategy or "") != "voiceover"
        and any(is_analysis_proxy_path(str(path)) for path in clip_paths or ())
        and settings.phone_rendering_for(creator_id)
        and "subtitled" in phone_render_supported_formats()
    )


def requirements_to_check_at_draft(
    requirements: Iterable[BriefRequirement],
    *,
    creator_id: object,
    strategy: Mapping[str, Any] | None,
    item_edit_format: object,
    clip_paths: Iterable[object] = (),
    client_protocol: int | None = None,
) -> list[BriefRequirement]:
    """The requirements a strategy draft can honestly be checked against.

    When the unified planner will settle a requirement at render time (`text`, `order`,
    `timing`) it is left out, so the draft reply is the plain summary instead of a
    premature failure notice; the render's own receipts (met / partial / not possible,
    plus guessed names) follow. A phone Voiceover draft likewise leaves its first/last
    order, clip-timing and caption-language asks to the render's record (KRI-533).
    A phone Talking draft keeps its caption-language / name-spelling asks: the render
    judges them (KRI-549), and the draft says what it will do (KRI-793,
    `PlanFacts.caption_words_at_render`). A freeze-frame ask stays unless the phone montage
    can carry it (`freeze_plannable`, KRI-828) for ``client_protocol``, the app build that
    sent the message (KRI-898; ``None`` = unknown, so no hold is planned): otherwise its
    "Couldn't" belongs before approval (KRI-777). Otherwise this is the unchanged, full list.
    """
    strategy = strategy or {}
    clip_paths = list(clip_paths or ())
    edit_format = strategy.get("edit_format") or item_edit_format
    defers = defers_to_unified_montage(
        creator_id=creator_id,
        edit_format=edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    )
    narrated = not defers and defers_to_narrated_render(
        creator_id=creator_id,
        edit_format=edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    )
    requirements = list(requirements)
    # KRI-899: the freezes the plan will carry, read over the whole brief, so a correction
    # ("make it 2 seconds instead") is deferred exactly when the render answers it.
    planned = planned_freeze_ids(requirements)
    return [
        req
        for req in requirements
        if not (
            defers
            and req.kind in UNIFIED_SETTLED_KINDS
            and (
                not asks_freeze_frame(req)
                or freeze_plannable(req, strategy, planned, client_protocol=client_protocol)
            )
        )
        and not (narrated and _settled_by_narrated_render(req, strategy))
    ]


def freeze_plannable(
    req: BriefRequirement,
    strategy: Mapping[str, Any] | None,
    planned: frozenset[str] | None = None,
    *,
    client_protocol: int | None,
) -> bool:
    """KRI-828: will the phone montage plan carry this freeze ask?

    Only a corroborated freeze ask (`freeze_spec`, or with ``planned`` -- the brief's
    `planned_freeze_ids` -- a correction of one, KRI-899), on a montage with no creator
    song, where the phone can hold a frame for the app build that asked
    (`freeze_frame_planned_for`, KRI-898: ``client_protocol`` is required so every caller
    decides which build it plans for). The render then settles it (met, or which moment it
    could not find); everywhere else KRI-777's "Couldn't" stays on the draft.
    """
    from app.services.phone_rollout import freeze_frame_planned_for  # noqa: PLC0415

    return (
        (req.id in planned if planned is not None else freeze_spec(req) is not None)
        and str((strategy or {}).get("audio_strategy") or "") != "user_song"
        and freeze_frame_planned_for(client_protocol)
    )


def clip_speed_plannable(strategy: Mapping[str, Any] | None) -> bool:
    """KRI-689: will the phone montage plan carry a clip-speed ask for this draft?

    Only where the renderer retimes (``variableSpeed`` verified, `clip_speed_planned_for`)
    and the cuts do not follow a creator's song (a song montage sits on the song's beats and
    lyric captions, so every speed ask is locked there). Everywhere else the plan records the
    ask as locked and the draft keeps the honest heads-up (`clip_speed_notice`).
    """
    from app.services.phone_rollout import clip_speed_planned_for  # noqa: PLC0415

    return clip_speed_planned_for() and str((strategy or {}).get("audio_strategy") or "") != (
        "user_song"
    )


def clip_speed_notice(
    requirements: Iterable[BriefRequirement],
    *,
    creator_id: object,
    strategy: Mapping[str, Any] | None,
    item_edit_format: object,
    clip_paths: Iterable[object] = (),
    judged_ids: Iterable[str] = (),
) -> str | None:
    """The plain heads-up, before approval, that this draft cannot change clip speed (KRI-689).

    "Speed the river shots up 2x" on a phone montage whose plan cannot retime (no verified
    ``variableSpeed``, or the clips follow the creator's song): without this line the draft
    would be approved with the ask silently dropped and only the finished video's review would
    admit it. Returned only for a draft that will render through the unified planner (the
    same gate `requirements_to_check_at_draft` uses); a cloud render, a voiceover lane and the
    other phone formats get ``None``. Where the plan CAN carry the speed (`clip_speed_plannable`)
    the plan's own receipts speak (the pre-approval plan, then the render), so this is
    ``None``; likewise when every live speed ask is already in ``judged_ids`` (the receipts
    the pre-approval plan returned).
    """
    strategy = strategy or {}
    asked = [req.id for req in requirements if req.live and asks_clip_speed(req)]
    if not asked:
        return None
    if not defers_to_unified_montage(
        creator_id=creator_id,
        edit_format=strategy.get("edit_format") or item_edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=list(clip_paths or ()),
    ):
        return None
    judged = {str(rid) for rid in judged_ids}
    if all(rid in judged for rid in asked):
        return None
    if clip_speed_plannable(strategy):
        return None
    if str(strategy.get("audio_strategy") or "") == "user_song":
        return _loc(_NO_CLIP_SPEED_SONG) + "."
    return _loc(_NO_CLIP_SPEED) + "."


def is_judged(req: BriefRequirement | None, receipt: RequirementReceipt) -> bool:
    """True when a checker had what it needed to decide ``req``.

    With no checker, or with a neutral reason (the facts were missing), nothing
    was checked. Such a receipt read "Partly: add captions (I can't verify this
    one automatically yet)", a half-done claim on nearly every reply. Receipts
    stored before these stopped being written still exist, so every reader of
    stored receipts filters through this too.
    """
    if receipt.status == "not_possible" and is_route_decline_reason(receipt.reason):
        # An ask the render route does not read is a plain answer ("I can't animate..."),
        # though no checker owns it (route_disclosure).
        return True
    if (
        req is not None
        and receipt.status in {"partial", "not_possible"}
        and receipt.reason
        and any(
            str(receipt.reason).startswith(prefix)
            for prefix in (*_TALKING_HONEST_EN, *(_REASON_TR[r] for r in _TALKING_HONEST_EN))
        )
    ):
        # KRI-705: a phone Talking edit's plain "can't" is an answer, though no checker owns it.
        return True
    if (
        req is not None
        and req.kind == "select"
        and receipt.verification == "checked"
        and receipt.stage == "checked"
    ):
        # KRI-994: a `select` ask the typed link / finished timeline settled.
        return receipt.reason not in _NEUTRAL_REASONS
    if req is not None and receipt.verification == "checked" and receipt.stage == "applied":
        # KRI-558: a change proven by the before/after edit diff, not by a value check.
        return receipt.reason not in _NEUTRAL_REASONS
    return (
        req is not None
        and receipt.verification != "unchecked"
        and _has_checker(req)
        and receipt.reason not in _NEUTRAL_REASONS
    )


def _talking_honest_receipt(
    req: BriefRequirement, receipt: RequirementReceipt, facts: PlanFacts
) -> RequirementReceipt:
    """What a phone Talking edit can honestly say about every ask it has no checker for (KRI-705).

    Talking draws a title, captions, a start cut, speech cleanup and word-triggered pop-ins;
    everything else the brief holds (a single repeated word to cut, quieter engine noise) is
    never done. Left alone an unjudged ask is silent before approval, so the approval text runs
    ahead of what the plan holds, and it is "have a look at these" after render. This turns it
    into a plain "can't" (a limit of the format, never the simpler-version question). Which
    asks count is read from the row's `kind` and the checkers that already own an ask, never
    from its words (KRI-667).
    """
    if not facts.phone_talking or (facts.editor and not facts.rendered_variant):
        return receipt
    if wants_engine_noise_reduction(req):
        return receipt
    if requirement_speech_cut(req) is not None:
        # The explicit checker above is the only route that can promise this edit.  Preserve
        # its pre-approval source-resolution notice instead of replacing it with the generic
        # Talking-format refusal.
        return receipt
    if _wants_opening_cut(req):
        if facts.rendered_variant:
            return receipt  # the render's own `opening_cut` / `end_cut` receipts judge it
        return _receipt(req, "partial", _take_window_plan(req, facts.take_window))
    if is_judged(req, receipt):
        return receipt
    if _has_duration_target(req):
        target = float(req.facts["duration_s"])
        take = facts.take_duration_s
        # KRI-790: with quoted start / end lines the part they keep sets the length, not the
        # whole take; a line the draft cannot place leaves only the take's length to say.
        window = facts.take_window
        kept = window.kept_s if window is not None else None
        prefix = _TALKING_KEEPS_QUOTED_PART if window is not None else _TALKING_KEEPS_WHOLE_TAKE
        if kept is not None:
            if kept <= target + 0.5:
                return receipt
            en_tail = f"that part is about {_round_s(kept)}s"
            tr_tail = f"o kısım yaklaşık {_round_s(kept)} saniye"
        else:
            if take is None or take <= target + 0.5:
                return receipt
            en_tail = f"your take is {_round_s(take)}s"
            tr_tail = f"çekimin {_round_s(take)} saniye"
        # KRI-847: a ceiling is worded as one ("at most 10s"), never as an exact length.
        ceiling = duration_bound(req.facts) == "at_most"
        asked_en = f"at most {_round_s(target)}s" if ceiling else f"{_round_s(target)}s"
        asked_tr = (
            f"en fazla {_round_s(target)} saniye" if ceiling else f"{_round_s(target)} saniye"
        )
        return _receipt(
            req,
            "partial",
            say(
                en=f"{prefix}; {en_tail} and you asked for {asked_en}",
                tr=f"{_REASON_TR[prefix]}; {tr_tail}, sen {asked_tr} istedin",
            ),
        )
    # Footage sound level; KRI-842: one clip's sound is as much a mix change a Talking edit lacks.
    bed_ask = (
        _wants_bed_under_voice(req)
        or _wants_bed_muted(req)
        or requirement_clip_sound(req) is not None
    )
    if req.kind not in {"audio", "timing", "select"} and not bed_ask:
        return receipt
    # A speech-excerpt reading ("sesim öne çıksın", "so my voice comes through") only has a
    # checker on a plan with speech sections; a Talking plan has none, so that reading must
    # not keep an engine-noise ask silent.
    speech_unjudgeable = _wants_speech(req) and facts.speech_sections is None
    if _has_checker(req) and not bed_ask and not speech_unjudgeable:
        return receipt
    return _receipt(req, "not_possible", _TALKING_NO_SUCH_EDIT)


def _live_ask_for_caption_language(req: BriefRequirement, code: str) -> bool:
    """KRI-1015: ``req`` is a live captions ask for the language ``code``."""
    return req.live and asks_caption_language(req) and _requested_caption_language(req) == code


def build_receipts(
    requirements: Iterable[BriefRequirement],
    facts: PlanFacts,
    *,
    include_unchecked: bool = False,
    strict_order: bool | None = None,
) -> list[RequirementReceipt]:
    """Build receipts without changing legacy omission or serialization by default.

    Bound writers opt in with ``include_unchecked=True``. That emits every
    live requirement: determinate receipts are ``checked`` at the ``checked``
    stage, while unavailable evidence is ``unchecked`` at ``understood`` (or
    ``matched`` when a clip target is known).

    ``strict_order`` (default: ``include_unchecked``, i.e. the writer is bound) turns on the
    stricter verdicts for a required order (see ``_check_order``). A contract-stamped job
    passes it explicitly; a legacy / unbound job keeps the original semantics.
    """
    if (include_unchecked if strict_order is None else strict_order) and not facts.strict_order:
        facts = dataclasses.replace(facts, strict_order=True)
    requirements = list(requirements)
    rendered_language = str(facts.caption_language or "").casefold().split("-")[0]
    if (
        rendered_language
        and facts.caption_translated_from
        and any(_live_ask_for_caption_language(req, rendered_language) for req in requirements)
    ):
        # KRI-1015: another requirement asked for this caption language.
        facts = dataclasses.replace(facts, caption_translation_asked=True)
    checked = (
        (req, _talking_honest_receipt(req, check_requirement(req, facts), facts))
        for req in requirements
        if req.live
    )
    receipts: list[RequirementReceipt] = []
    for req, receipt in checked:
        if is_judged(req, receipt):
            receipts.append(
                receipt.model_copy(update={"verification": "checked", "stage": "checked"})
                if include_unchecked
                else receipt
            )
        elif include_unchecked:
            receipts.append(
                receipt.model_copy(
                    update={
                        "verification": "unchecked",
                        "stage": "matched" if receipt.target_media_ids else "understood",
                    }
                )
            )
    return receipts


# ------------------------------------------------------------------------ reply

_LABEL = {
    "met": "Done",
    "partial": "Partly",
    "not_possible": "Couldn't",
    # KRI-282: the creator was asked and chose the other side of a conflict.
    "chosen": "As you chose",
}
_LABEL_TR = {
    "met": "Yapıldı",
    "partial": "Kısmen",
    "not_possible": "Yapamadım",
    "chosen": "Senin seçimin",
}


def _label(status: str) -> str:
    return say(en=_LABEL[status], tr=_LABEL_TR[status])


# What the render says about each requested group, label or chapter text
# (`unified_montage._intent_outcomes`) is English; a Turkish reply reads these known
# phrases in Turkish and leaves anything else (the creator's own words) as written.
_OUTCOME_REASON_TR = {
    "I found no clips of it": "Bununla ilgili klip bulamadım",
    "I couldn't put your own words on it": "Senin sözlerini üstüne koyamadım",
    "no clip got one": "hiçbir klibe gelmedi",
    "I couldn't tell which clips": "Hangi kliplerden söz ettiğini anlayamadım",
    "its clips are already in an earlier group": "klipleri zaten önceki bir grupta",
    "its clips are already placed at another spot": "klipleri zaten başka bir yerde",
    "the clips did not end up there": "klipler oraya gelmedi",
    "you didn't say what order its clips play in": (
        "kliplerinin hangi sırayla oynayacağını söylemedin"
    ),
    # KRI-723 (``unified_montage.MOMENT_*``).
    "I couldn't find this moment in your clip, so I can't confirm it's the one playing": (
        "Bu anı klipte bulamadım, o yüzden oynayanın o an olduğunu doğrulayamıyorum"
    ),
    "you let me pick this moment, so I chose it myself": "bu anı seçmeyi bana bıraktın, ben seçtim",
    "you told me this moment isn't in your clips": "bu anın kliplerinde olmadığını söyledin",
    "you chose grouping first, each group in the order you filmed it": (
        "önce gruplamayı seçtin, her grup çektiğin sırada"
    ),
}
_OUTCOME_STRETCH_PREFIX_TR = (
    ("you chose strictly chronological order, so ", "kesin kronolojik sırayı seçtin, o yüzden "),
    ("clips stay in the order you filmed them, so ", "klipler çektiğin sırada kalıyor, o yüzden "),
)
_OUTCOME_STRETCHES = re.compile(r"^(.+) is in (\d+) stretches$")


def _outcome_name(row: Mapping[str, Any]) -> str:
    name = str(row["name"])
    if current_reply_language() != "tr":
        return name
    op = row.get("op")
    if op == "order":
        position = str(row.get("position") or "")
        if position in _SPOT_TR and name.startswith(f"{position}: "):
            return f"{_SPOT_TR[position]}: {name[len(position) + 2 :]}"
        if name == "the order you described":
            return "tarif ettiğin sıra"
    elif op == "group" and name.startswith("group by "):
        return f"gruplama: {name[len('group by ') :]}"
    elif op == "caption" and name.startswith("text for "):
        return f"{name[len('text for ') :]} için yazı"
    elif op == "label" and name.startswith("the ") and name.endswith(" name on its clips"):
        return f"kliplerindeki {name[len('the ') : -len(' name on its clips')]} adı"
    return name


def _outcome_reason(reason: str) -> str:
    if current_reply_language() != "tr":
        return reason
    known = _OUTCOME_REASON_TR.get(reason)
    if known is not None:
        return known
    for prefix, prefix_tr in _OUTCOME_STRETCH_PREFIX_TR:
        if reason.startswith(prefix):
            parts = []
            for part in reason[len(prefix) :].split("; "):
                match = _OUTCOME_STRETCHES.match(part)
                parts.append(
                    f"{match.group(1)} {match.group(2)} parçaya bölünüyor" if match else part
                )
            return prefix_tr + "; ".join(parts)
    return _loc(reason)


def keeps_model_summary(
    brief: CreativeBrief, receipts: Iterable[RequirementReceipt | Mapping[str, Any]]
) -> bool:
    """True when ``reply_from_receipts`` keeps the model's free-text summary (KRI-700).

    It is kept only when every judged requirement is met and none is left to look at, the
    same rule as the reply's own branches. Anything else the draft says about itself (its
    change list, the approval card) must follow the reply, or a summary that promised
    "brightened footage" reaches the card the reply already corrected.
    """
    by_id = {req.id: req for req in brief.requirements}
    for raw in receipts:
        receipt = (
            raw
            if isinstance(raw, RequirementReceipt)
            else RequirementReceipt.model_validate(
                {k: v for k, v in raw.items() if k in RequirementReceipt.model_fields}
            )
        )
        req = by_id.get(receipt.requirement_id)
        if req is None:
            continue
        if receipt.verification == "unchecked":
            return False
        if is_judged(req, receipt) and receipt.status != "met":
            return False
    return True


def reply_from_receipts(
    brief: CreativeBrief,
    receipts: list[RequirementReceipt],
    *,
    summary: str | None = None,
    notices: Sequence[str] = (),
    outcomes: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Compose the creator-facing reply from receipts only.

    The model's free-text summary is kept only when every judged requirement is
    met; otherwise the reply is exactly what was checked, so it cannot overclaim.
    ``notices`` are the server's own repair notes (KRI-142). The summary already
    carries them, so they are added back only when the summary is replaced.
    An unjudged receipt (stored before ``build_receipts`` dropped them) gets no
    line and never turns the reply into a failure notice.

    Requirements no checker could judge (``unchecked``) are never reported as a failure
    (KRI-558): the reply lists them under "Have a look at these in the video", and an
    editor turn never reaches that branch because its changes are named from the edit diff
    (`kria.editor_receipts`).
    """
    by_id = {req.id: req for req in brief.requirements}
    judged = [r for r in receipts if is_judged(by_id.get(r.requirement_id), r)]
    unchecked = [
        r
        for r in receipts
        if r.verification == "unchecked" and by_id.get(r.requirement_id) is not None
    ]
    lines: list[str] = []
    guesses: list[str] = []
    # KRI-282: what the render did for each requested group / label / chapter text,
    # read from the finished plan, listed whether or not the brief ledger judged it.
    failed = False
    for row in outcomes:
        status = str(row.get("status") or "")
        if status not in _LABEL or not row.get("name"):
            continue
        failed = failed or status not in {"met", "chosen"}
        line = f"{_label(status)}: {_outcome_name(row)}"
        if row.get("reason"):
            line += f" ({_outcome_reason(str(row['reason'])).rstrip('.')})"
        lines.append(line)
    for receipt in judged:
        line = f"{_label(receipt.status)}: {by_id[receipt.requirement_id].text()}"
        if receipt.reason:
            line += f" ({_loc(receipt.reason).rstrip('.')})"
        lines.append(line)
        guesses.extend(receipt.inferred)
    if guesses:
        shown = ", ".join(dict.fromkeys(guesses))
        lines.append(
            say(
                en=(
                    f"I guessed these, tell me if any is wrong: {shown} "
                    "(text I took from the footage, not from your words)"
                ),
                tr=(
                    f"Şunları tahmin ettim, yanlış olan varsa söyle: {shown} "
                    "(senin sözlerinden değil, çekimlerden aldığım yazılar)"
                ),
            )
        )
    body = "\n".join(f"- {line}" for line in lines)
    if unchecked:
        look_lines = []
        for receipt in unchecked:
            line = f"- {by_id[receipt.requirement_id].text()}"
            if receipt.reason and receipt.reason not in _NEUTRAL_REASONS:
                line += f" ({_loc(receipt.reason).rstrip('.')})"
            look_lines.append(line)
        header = say(en="Have a look at these in the video:", tr="Bunlara videoda bir göz at:")
        text = "\n".join(part for part in (body, header, "\n".join(look_lines)) if part)
        if notices:
            text += "\n" + " ".join(notices)
    elif failed or any(r.status != "met" for r in judged):
        text = (
            say(
                en="Not everything you asked for made it in:\n",
                tr="İstediklerinin hepsi videoya giremedi:\n",
            )
            + body
        )
        if notices:
            text += "\n" + " ".join(notices)
    else:
        text = "\n".join(part for part in ((summary or "").strip(), body) if part)
    if len(text) > MAX_REPLY_CHARS and judged and not (outcomes or guesses or unchecked):
        # Keep every requested action/status visible when its labels fit. A long
        # explanation must not consume the final action's display budget. Stored
        # receipt reasons remain complete; ellipses mark shortened display only.
        labels = [f"{_label(r.status)}: {by_id[r.requirement_id].text()}" for r in judged]
        prefix, suffix = text.rsplit(body, 1)
        label_body = "\n".join(f"- {line}" for line in labels)
        available = MAX_REPLY_CHARS - len(prefix + label_body + suffix + "\n…")
        if available >= 0:
            reason_limit = max(0, available // len(judged) - 3)
            compact = []
            for label, receipt in zip(labels, judged, strict=True):
                reason = _loc(receipt.reason).rstrip(".") if receipt.reason else ""
                if reason and reason_limit >= 2:
                    if len(reason) > reason_limit:
                        reason = reason[: reason_limit - 1].rstrip() + "…"
                    label += f" ({reason})"
                compact.append(f"- {label}")
            text = prefix + "\n".join(compact) + suffix + "\n…"
    if len(text) > MAX_REPLY_CHARS:
        text = text[: MAX_REPLY_CHARS - 1].rstrip() + "…"
    return text


__all__ = [
    "UNIFIED_SETTLED_KINDS",
    "BeatFact",
    "ClosingSpeechFact",
    "EngineNoiseReductionFact",
    "ExplicitSpeechCutFact",
    "EndingClipFact",
    "NO_TITLE_REASON",
    "NARRATED_ALIGNMENT_RECOVERY",
    "RenderBlockRecovery",
    "NARRATED_ALIGNED_BASES",
    "NARRATED_ALIGNMENT_FIELD",
    "NarratedStepFact",
    "asks_caption_language",
    "asks_caption_spelling",
    "brief_caption_spellings",
    "keeps_model_summary",
    "wants_light_change",
    "defers_caption_words_to_phone_render",
    "defers_to_narrated_render",
    "defers_to_unified_montage",
    "requirements_to_check_at_draft",
    "PlanFacts",
    "build_receipts",
    "check_requirement",
    "clip_speed_notice",
    "clip_speed_plannable",
    "is_format_limit",
    "is_judged",
    "is_no_title_reason",
    "is_spoken_text_cue",
    "judged_at_render",
    "text_words_met",
    "narrated_alignment_recovery",
    "needs_creator_choice",
    "SpeechSectionFact",
    "plan_facts_from_editor_payload",
    "plan_facts_from_narrated_alignment",
    "plan_facts_from_phone_variant",
    "plan_facts_from_rendered_montage",
    "plan_facts_from_speech_montage",
    "plan_facts_from_strategy",
    "plan_facts_from_unified_montage",
    "render_block_recovery",
    "reply_from_receipts",
    "speech_cleanup_review_note",
    "text_sequence_shown",
    "wants_lipsync",
]
