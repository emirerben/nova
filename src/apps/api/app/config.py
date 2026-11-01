import json
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

GUIDED_STORY_RENDERER_READY = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Admission-only rollout fence.  Rendering remains controlled by its own
    # phone capability gates so this can safely retire web creation first.
    ios_device_only_mode: bool = False
    # Native-only rollout fence. Unlike IOS_DEVICE_ONLY_MODE this never retires
    # web creation: it binds only projects created through the iPhone client to
    # the device renderer, regardless of the legacy phone allowlist.
    ios_native_device_only_enabled: bool = False
    kria_minimum_client_protocol: int = 2
    # Executor behavior is intentionally owned by the render pipeline; this
    # setting only declares the rollout control plane default.
    cloud_render_execution_enabled: bool = True
    # Frozen into each newly approved generation; workers do not read this flag.
    creator_duration_check_mode: Literal["log_only", "enforce"] = "log_only"

    # Phone rendering remains off until a capability group has device evidence.
    phone_rendering_enabled: bool = False
    # Enroll supported NEW phone requests in measured actual-output verification.
    # The per-request stamp survives flag changes and exact retries.
    phone_actual_export_verification_enabled: bool = False
    # New editor imports require device qualification; saved receipts remain readable.
    phone_editor_media_enabled: bool = False
    phone_render_verified_features: list[str] = Field(default_factory=list)
    phone_render_user_ids: list[UUID] = Field(default_factory=list)
    # Kill switch for the 2026-09-18 font/effect qualification relaxation in
    # `app/services/phone_rollout.py`. False (default): any bundled-registry
    # font (assets/fonts/font-registry.json) with correct variation
    # coordinates, on any of the 17 native-supported text effects, qualifies —
    # this is what makes an ordinary guided story (static Fraunces title +
    # Inter-Bold static context labels) actually compile for the pilot. True
    # restores the original narrow per-instance gate (only the exact
    # Fraunces-Bold.ttf/DMSans-Bold.ttf byte/coordinate match on plain
    # `fade-in`, no giant title) byte-identically. Rollback:
    # `fly secrets set PHONE_FONT_QUALIFICATION_STRICT=true --app nova-video`
    # + `fly machine restart <id>` (api + worker). See
    # docs/runbooks/phone-rendering.md and
    # docs/reviews/kri-29/capability-matrix.md ("Font/authored-text
    # qualification is per-instance").
    phone_font_qualification_strict: bool = False
    # KRI-132: montage/day_vlog/single_hero items with a recorded voiceover
    # resolve to the "voiceover" archetype (`_resolve_archetype` in
    # generative_build.py) same as on the cloud, and phone rendering compiles
    # a narration audio track for them (`compile_phone_voiceover_montage_plan`) instead
    # of failing closed with "Phone rendering does not yet support voiceover
    # edits". True (default since the KRI-132 rollout): the pilot cohort's
    # montage-family voiceover edits render on the device. False: byte-identical
    # to pre-KRI-132 -- the dispatch gate (`content_plan_build.py`) fails closed
    # early with a typed `phone_voiceover_unavailable` reason instead of ever
    # queuing the job.
    # Also requires "narrationAudio" in `phone_render_verified_features` (see
    # `app.services.phone_rollout.validate_phone_pilot_recipe`) -- this flag
    # alone does not skip that device-parity gate. This flag is ALSO a
    # co-requirement for the narrated family's own voiceover render (see
    # `phone_narrated_rendering_enabled` below) -- narrated reuses the same
    # narrationAudio capability and the same device-verified narration-track
    # compile path. It is ALSO a co-requirement (alongside
    # `phone_guided_narration_rendering_enabled` below) for the guided-story
    # `.narration` plan lane -- see that flag's comment for what gates that
    # lane specifically. Rollback:
    # `fly secrets set PHONE_NARRATION_RENDERING_ENABLED=false --app nova-video`
    # + `fly machine restart <id>` (api + worker). See
    # docs/runbooks/phone-rendering.md and
    # docs/reviews/kri-29/capability-matrix.md ("narrationAudio").
    phone_narration_rendering_enabled: bool = True
    # KRI-132 phone-voiceover-gate follow-up: a guided-story plan whose
    # `.narration` field is set (the voiceover-timed "guided story" lane,
    # execution contract `guided_voiceover_v1` -- the only lane that combines
    # Visuals-pool media with a recorded voiceover) compiles through
    # `app.pipeline.phone_guided_plan.compile_phone_guided_plan`'s narration
    # branch (`app.tasks.generative_build._run_phone_guided_job` resolves the
    # pinned voiceover bed via `_resolve_phone_voiceover_bed`) instead of
    # failing closed unconditionally. True (default): the pilot cohort's
    # voiceover-timed guided stories render on the device with their Visuals
    # photos. False: byte-identical to before this flag existed -- the
    # dispatch gate (`content_plan_build.py`) fails closed early with a typed
    # `guided_voiceover_unavailable` reason instead of ever queuing the job.
    # Also requires `phone_narration_rendering_enabled` (this lane reuses the
    # same narration-audio device capability the montage-family voiceover
    # render uses) AND "narrationAudio" in `phone_render_verified_features` --
    # `app.services.phone_rollout.phone_guided_narration_supported()` is the
    # single source of truth ANDing all three; every call site (the
    # capability resolver, the dispatch gate, the worker) MUST go through it
    # rather than re-deriving the rule. Rollback:
    # `fly secrets set PHONE_GUIDED_NARRATION_RENDERING_ENABLED=false --app
    # nova-video` + `fly machine restart <id>` (api + worker). See
    # docs/runbooks/phone-rendering.md.
    phone_guided_narration_rendering_enabled: bool = True
    # KRI-132 (talking-to-camera): a `subtitled` item (exactly one portrait
    # clip, its own audio transcribed into editable captions) compiles through
    # `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan`
    # (`app.tasks.generative_build._run_phone_subtitled_job`) instead of
    # failing closed with "analysis proxies cannot render 'subtitled' on
    # iPhone yet". True (default): the pilot cohort's talking-to-camera edits
    # render on the device. False: byte-identical to before this flag existed
    # -- the dispatch gate (`content_plan_build.py`) fails closed early with a
    # typed `unsupported_format` reason instead of ever queuing the job. Also
    # requires `settings.subtitled_archetype_enabled` (the format itself must
    # be enabled at all, phone or not) -- see
    # `app.services.phone_rollout.phone_render_supported_formats`. Rollback:
    # `fly secrets set PHONE_SUBTITLED_RENDERING_ENABLED=false --app nova-video`
    # + `fly machine restart <id>` (api + worker).
    phone_subtitled_rendering_enabled: bool = True
    # Requires processedAudio on a verified protocol-18 client cohort.
    phone_engine_noise_reduction_enabled: bool = False
    phone_engine_noise_model_path: str = ""
    phone_engine_noise_model_sha256: str = ""
    # KRI-174 Phase 1: a server-owned `_phone_subtitled_lanes_v1` request on
    # the job's assembly_plan (overlay sticker/photo cards pinned from the
    # Visuals pool, catalog sound effects on an `sfx` track, a muted ending
    # clip) is honoured by `_run_phone_subtitled_job`
    # (`app.tasks.generative_build`), which resolves each lane (Visuals via
    # `bind_phone_visual_assets`, sound effects via the new
    # `_resolve_phone_sound_effect`), compiles them through
    # `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan`'s
    # `lanes=`/`visuals=` kwargs, and persists `overlay_transcript` (the raw
    # pre-correction Whisper words) + `phone_lane_receipt` (applied/dropped
    # lanes) on the variant. False (default): the field is ignored entirely
    # -- `_run_phone_subtitled_job` executes the exact same statements as
    # before this flag existed and persists the exact same variant dict, with
    # no `overlay_transcript`/`phone_lane_receipt` keys and no Visuals-binder
    # call, even when the snapshot carries the lanes field. Rollback:
    # `fly secrets set PHONE_SUBTITLED_MEDIA_LANES_ENABLED=false --app
    # nova-video` + `fly machine restart <id>` (api + worker).
    #
    # KRI-176 (PiP cards grounded from the transcript): when this flag AND
    # `media_overlays_enabled` are both true AND all four of
    # `app.services.phone_rollout.PHONE_SUBTITLED_OVERLAY_FEATURES`
    # (`stillImages`, `visualBlocks`, `alphaOverlay`, `audioMix`) are verified
    # -- i.e. `phone_rollout.phone_subtitled_overlays_supported()` is True --
    # the planner advertises `media_overlays` on a phone `subtitled` manifest
    # instead of the blanket `unsupported_on_phone` refusal, and
    # `_run_phone_subtitled_job` grounds the creator's Visuals into
    # picture-in-picture overlay cards from the transcript
    # (`app.services.phone_overlay_grounding`), persisting a creator-safe
    # `phone_overlay_receipt` on the variant. The same rollback command above
    # switches this lane off too (no separate flag).
    phone_subtitled_media_lanes_enabled: bool = False
    # KRI-178 (reaction beats): when this flag AND
    # `phone_subtitled_media_lanes_enabled` AND `sound_effects_enabled` are
    # all true AND every feature in
    # `app.services.phone_rollout.PHONE_SUBTITLED_SFX_FEATURES`
    # (`soundEffects`, `audioMix`) is verified -- i.e.
    # `phone_rollout.phone_subtitled_reaction_beats_supported()` is True --
    # the planner advertises the `reaction_beats` capability on a phone
    # `subtitled` manifest, the creator agent may author `reaction_beats`/
    # `closing_media` on its strategy (name-triggered photo/sticker pop-ins
    # and sound effects timed to the transcript, plus a held closing shot),
    # and `_run_phone_subtitled_job` grounds those beats against the
    # transcript instead of the generic KRI-176 overlay grounding, persisting
    # a creator-safe `phone_beat_receipt` on the variant. False (default):
    # byte-identical to before this flag existed -- `reaction_beats`/
    # `closing_media` are stripped from any stored strategy with a plain
    # notice, the manifest falls back to the existing `unsupported_on_phone`/
    # `phone_talking_only` refusal for this capability, and the worker never
    # grounds beats. Rollback: `fly secrets set
    # PHONE_SUBTITLED_REACTION_BEATS_ENABLED=false --app nova-video` + `fly
    # machine restart <id>` (api + worker).
    phone_subtitled_reaction_beats_enabled: bool = False
    # KRI-181 follow-up: on a phone-rendered Talking edit, a sound effect whose
    # window overlaps a spoken word plays at
    # `phone_subtitled_plan.SFX_SPEECH_DUCK_GAIN` of its requested volume so
    # the beat never masks the speaker; effects in pauses keep full volume.
    # Server-only: it changes existing `TimelineClip.volume` values, so any
    # installed app build honours it with no new capability. False (default):
    # byte-identical recipes. Read at compile time, so a flip affects the next
    # render. Apply: `fly secrets set PHONE_SFX_SPEECH_DUCK_ENABLED=true
    # --app nova-video` + `fly machine restart <id>` (worker).
    phone_sfx_speech_duck_enabled: bool = False
    # KRI-182 step 1 (editable phone Talking edits): the native editor's
    # generic `sound_effects`/`media_overlays` editor-commit sections apply
    # to a phone-rendered `subtitled` (Talking to camera) variant --
    # `prepare_phone_editor_commit` (`app.services.phone_editor`) recompiles
    # `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan` from
    # the committed sections instead of unconditionally 422ing
    # `unsupported_phone_edit` (subtitled variants carry no guided-story
    # execution plan to fall back to). Requires
    # `phone_subtitled_media_lanes_enabled` (the underlying KRI-174 lane
    # compiler must already be live) AND every feature in
    # `app.services.phone_rollout.PHONE_SUBTITLED_EDITOR_FEATURES`
    # (`stillImages`, `visualBlocks`, `alphaOverlay`, `audioMix`,
    # `soundEffects`) verified in `phone_render_verified_features` -- see
    # `phone_rollout.phone_subtitled_editor_lanes_supported()`, the single
    # source of truth. False (default): byte-identical to before this flag
    # existed -- every subtitled device-variant Save still 422s
    # `unsupported_phone_edit`, and `_clamp_phone_editor_capabilities`
    # continues to close `sfx`/`overlays` for every device variant regardless
    # of archetype. Rollback: `fly secrets set
    # PHONE_SUBTITLED_EDITOR_LANES_ENABLED=false --app nova-video` + `fly
    # machine restart <id>` (api + worker).
    phone_subtitled_editor_lanes_enabled: bool = False
    # KRI-281 (editable phone Voiceover edits): the sound-effects and Visuals
    # (media overlay) editor lanes for a phone-rendered `narrated` (recorded
    # voiceover over a script) or montage `voiceover` variant, mirroring
    # KRI-182 for Talking. `phone_rollout.phone_voiceover_editor_lanes_supported()`
    # is the single source of truth: this flag AND `sound_effects_enabled` AND
    # `media_overlays_enabled` AND every feature in
    # `phone_rollout.PHONE_VOICEOVER_EDITOR_FEATURES` verified AND the
    # requesting app declares `X-Kria-Client-Protocol` >=
    # `phone_voiceover_editor_min_client_protocol` (older builds never hydrate
    # the source clips these lanes need, so they stay closed). Capabilities and
    # Save flip together. False (default): byte-identical, `sfx`/`overlays`
    # stay closed on every device variant and Save 422s those sections.
    # Rollback: `fly secrets set PHONE_VOICEOVER_EDITOR_LANES_ENABLED=false
    # --app nova-video` + `fly machine restart <id>` (api + worker).
    phone_voiceover_editor_lanes_enabled: bool = False
    # Client protocol (`X-Kria-Client-Protocol`) the app build must declare for
    # the KRI-281 lanes to open. The iOS build that hydrates narrated/voiceover
    # source clips bumps its protocol to this value; builds that predate it
    # send a lower one (or none) and keep the lanes closed.
    phone_voiceover_editor_min_client_protocol: int = 3
    # KRI-608: sound effects on a phone-rendered guided / montage story. The
    # guided phone compiler used to reject `editor_sound_effects` /
    # `licensed_sfx_intent`; with this flag on it compiles them to an `sfx`
    # audio track (the same track Talking and Voiceover already play).
    # `phone_rollout.phone_guided_sfx_supported()` is the single source of
    # truth: this flag AND `sound_effects_enabled` AND `soundEffects` +
    # `audioMix` in `phone_render_verified_features`. Capabilities, chat,
    # Save, the worker and creation all consult it, so none advertises what
    # another would refuse. False (default): byte-identical, guided phone
    # edits stay SFX-free and a Save carrying SFX 422s.
    # Rollback: `fly secrets set PHONE_GUIDED_SFX_ENABLED=false --app
    # nova-video` + `fly machine restart <id>` (api + worker).
    phone_guided_sfx_enabled: bool = False
    # KRI-519 kill switch: KRI-178 reaction beats ("when I say the medal, show
    # my medal photo") on a phone Voiceover (`narrated*` + recorded voiceover)
    # edit. Only takes effect while
    # `phone_rollout.phone_subtitled_reaction_beats_supported()` AND the
    # KRI-281 Voiceover lanes gate hold (`phone_voiceover_reaction_beats_
    # supported()`): the manifest advertises `reaction_beats` on that edit and
    # `_run_phone_narrated_job` grounds the beats against the voiceover's own
    # words into the Voiceover overlay / sound-effect lanes. False: the
    # capability stays `phone_talking_only` there, byte-identical to before.
    # Rollback: `fly secrets set PHONE_VOICEOVER_REACTION_BEATS_ENABLED=false
    # --app nova-video` + `fly machine restart <id>` (api + worker + light).
    phone_voiceover_reaction_beats_enabled: bool = True
    # Exact-timed small cards: "show the menu photo as a small card at 3 seconds for 2
    # seconds" / "put the receipt in the corner during the second clip" on a
    # phone-rendered Montage (guided / unified montage, montage-family voiceover) or
    # narrated Voiceover edit. `phone_rollout.phone_timed_cards_supported()` is the
    # single source of truth: this flag AND `media_overlays_enabled` AND every feature in
    # `phone_rollout.PHONE_TIMED_CARD_FEATURES` (`stillImages`, `visualBlocks`,
    # `alphaOverlay`, `audioMix`) verified. When it holds the manifest advertises
    # `timed_cards` on those phone edits, the creator agent may author
    # `strategy.timed_cards` (`TimedCard`: visual + `at_s` or `clip_id`/`offset_s` +
    # `hold_s`), the workers ground them (`app.pipeline.phone_timed_cards`) into the
    # existing `subtitled-overlays` lane (`_compile_overlay_track`) and persist a
    # creator-safe `phone_timed_card_receipt` + `phone_timed_cards` rows on the variant.
    # A video card additionally needs `visualVideos` verified. False (default):
    # byte-identical to before -- the manifest carries no `timed_cards` key (hashes
    # unchanged), `media_overlays` keeps its `unsupported_on_phone` refusal on these
    # edits, a strategy's `timed_cards` are stripped with a plain notice, and the
    # compilers emit no lane. Apply: `fly secrets set PHONE_TIMED_CARDS_ENABLED=true
    # --app nova-video` + `fly machine restart <id>` (api + worker); rollback with
    # `=false` the same way.
    phone_timed_cards_enabled: bool = False
    # KRI-942: lets a phone montage turn off PART of a clip's own sound ("no sound from 5 to
    # 8 seconds", "keep only that line"). The worker then sends recipe `mute_windows`, which
    # the engine ramps at each edge; `phone_rollout.phone_clip_sound_windows_supported()`
    # also needs `visualBlocks` verified (the capability mute windows require). Whole-clip
    # on/off (`source_audio_muted`, clip volume 0) never needs this flag. False (default):
    # a part-of-a-clip "off" is not built and a part-of-a-clip "on" keeps that whole clip
    # audible; the receipt says so. Apply: `fly secrets set PHONE_CLIP_SOUND_WINDOWS_ENABLED=
    # true --app nova-video` + restart api + worker; rollback with `=false` the same way.
    phone_clip_sound_windows_enabled: bool = False
    # KRI-183 (video Visuals as PiP): when this flag AND
    # `phone_rollout.phone_subtitled_overlays_supported()` hold AND
    # "visualVideos" is in `phone_render_verified_features` -- i.e.
    # `phone_rollout.phone_subtitled_video_overlays_supported()` is True --
    # `_run_phone_subtitled_job`'s transcript grounding
    # (`app.services.phone_overlay_grounding`) also binds VIDEO Visuals as
    # muted picture-in-picture cards trimmed to their spoken window, and
    # `compile_phone_subtitled_plan` accepts `kind="video"` overlay cards
    # (adding `visualVideos` to the recipe's required capabilities). False
    # (default): byte-identical to before this flag existed -- a video
    # Visual is reported on the receipt as `video_not_supported` and the
    # compiler keeps rejecting video overlay cards. Rollback: `fly secrets
    # set PHONE_SUBTITLED_VIDEO_OVERLAYS_ENABLED=false --app nova-video` +
    # `fly machine restart <id>` (api + worker).
    phone_subtitled_video_overlays_enabled: bool = False
    # KRI-297 kill switch: lets a phone `subtitled` (Talking) edit show ALL its
    # Visuals as a full-screen cutaway sequence when the creator asks for it
    # ("use all overlays as full screen"). Gates the `media_overlays:fullscreen`
    # capability; a strategy that asks for it while this is off is refused
    # honestly, and a stale job's `all_candidates["overlay_display"]` is the
    # worker's concern. Rollback: `fly secrets set
    # PHONE_SUBTITLED_FULLSCREEN_OVERLAYS_ENABLED=false --app nova-video` +
    # `fly machine restart <id>` (api + worker).
    phone_subtitled_fullscreen_overlays_enabled: bool = True
    # KRI-547 kill switch: when the approved brief asks for a vertical / 9:16 /
    # full-screen / "keep my face in frame" video and the phone Talking speaker
    # clip is landscape, `_run_phone_subtitled_job` samples the face across the
    # take and cover-fills a static crop shifted onto it
    # (`app.pipeline.phone_speaker_framing`) instead of letterboxing, and
    # persists `variant["speaker_framing"]` for the brief receipt. Edits without
    # such an ask are byte-identical either way. False: no ask is read, no
    # receipt is written, landscape speakers keep `landscape_fit` exactly as
    # before. Read per job (a worker restart picks up a flip). Rollback: `fly
    # secrets set PHONE_SPEAKER_FACE_FILL_ENABLED=false --app nova-video` +
    # `fly machine restart <id>` (worker). Docs: docs/pipelines/generative.md.
    phone_speaker_face_fill_enabled: bool = True
    # KRI-136: a self-narrated (`narrated*`, no recorded voiceover) item with
    # 2+ clips that resolves to the `talking_head` archetype renders on the
    # device: the speech clip stays the main track (its audio runs the whole
    # way, with editable captions) and the other clips cover the picture as
    # muted full-frame cutaways (`compile_phone_subtitled_plan(cutaways=...)`).
    # Also needs `narrated_self_narration_enabled`, phone `subtitled`, and the
    # `visualBlocks`/`visualVideos`/`audioMix` device features -- see
    # `app.services.phone_rollout.phone_talking_head_supported`. False
    # (default): byte-identical, the dispatch gate refuses with
    # `self_narration_multi_clip`. Apply: `fly secrets set
    # PHONE_TALKING_HEAD_RENDERING_ENABLED=true --app nova-video` +
    # `fly machine restart <id>` (api + worker). No NEXT_PUBLIC twin.
    phone_talking_head_rendering_enabled: bool = False
    # KRI-719: a declared Talking-to-camera (`subtitled`) item may carry up to
    # `TALKING_MULTI_CLIP_MAX_CLIPS` clips on the phone. Clips with speech play
    # back to back on the main track (each with its own audio and captions);
    # the rest cover the picture as muted full-frame cutaways. Also needs phone
    # `subtitled` and the `PHONE_TALKING_HEAD_FEATURES` device features -- see
    # `app.services.phone_rollout.phone_talking_multi_clip_supported`. False
    # (default): byte-identical, Talking stays capped at one clip. Keep off until
    # the cloud renderer uses the extra clips too (plans/027). Apply: `fly secrets
    # set TALKING_MULTI_CLIP_ENABLED=true --app nova-video` + restart api + worker.
    talking_multi_clip_enabled: bool = False
    # KRI-132 (narrated walkthrough): a `narrated`/`narrated_planned`/
    # `narrated_ready` item WITH a recorded voiceover compiles through
    # `app.pipeline.phone_narrated_plan.compile_phone_narrated_plan`
    # (`app.tasks.generative_build._run_phone_narrated_job`) instead of failing
    # closed. True (default): the pilot cohort's narrated-walkthrough edits
    # render on the device. False: byte-identical to before this flag existed
    # -- the dispatch gate fails closed early with a typed
    # `narrated_voiceover_unavailable` reason. Also requires
    # `phone_narration_rendering_enabled` + "narrationAudio" in
    # `phone_render_verified_features` (the narrated family reuses the same
    # narration-audio device capability the montage-family voiceover render
    # uses) AND `settings.narrated_archetype_enabled` -- see
    # `app.services.phone_rollout.phone_render_supported_formats`, the single
    # source of truth for "which formats can render on the phone right now".
    # Does NOT cover a narrated item with NO voiceover resolving to
    # self-narration (see that function's docstring for the 1-clip exception).
    # Rollback: `fly secrets set PHONE_NARRATED_RENDERING_ENABLED=false
    # --app nova-video` + `fly machine restart <id>` (api + worker).
    phone_narrated_rendering_enabled: bool = True
    # KRI-280 (editable phone Narrated captions): the native editor's
    # `caption_cues` (line text/timing) and `caption_meta` (Style + Settings)
    # sections apply to a phone-rendered `narrated` (recorded voiceover)
    # variant -- `prepare_phone_editor_commit` swaps the pinned recipe's
    # caption layers (`phone_narrated_plan.replace_narrated_captions`) and the
    # capability map opens both, the same caption editing phone Talking has.
    # Also requires "positionedText" + "animatedText" in
    # `phone_render_verified_features` --
    # see `app.services.phone_rollout.phone_narrated_caption_edits_supported`,
    # the single source of truth. False: byte-identical to before this flag
    # existed -- both sections stay closed and a Save 422s. Rollback: `fly
    # secrets set PHONE_NARRATED_CAPTION_EDITS_ENABLED=false --app nova-video`
    # + `fly machine restart <id>` (api). No NEXT_PUBLIC twin (the iOS editor
    # follows the server's capability map).
    phone_narrated_caption_edits_enabled: bool = True
    # KRI-455 (titles on phone Voiceover edits): a confirmed `opening_title` on
    # a phone-rendered `narrated` edit burns like the cloud narrated intro
    # (`phone_narrated_plan`, "Opening title") instead of the planner asking to
    # drop it. Also requires "positionedText" + "animatedText" in
    # `phone_render_verified_features` -- see
    # `app.services.phone_rollout.phone_narrated_title_supported`. False: the
    # planner asks to make the edit without the title again, as before; an
    # already-approved title still renders. Rollback: `fly secrets set
    # PHONE_NARRATED_TITLE_ENABLED=false --app nova-video` + `fly machine
    # restart <id>` (api).
    phone_narrated_title_enabled: bool = True
    # Client protocol (`X-Kria-Client-Protocol`) an app build must declare to
    # see that title in its editor preview: the status route adds the
    # variant's `narrated_title_text_elements` to `text_elements` only for
    # builds that keep a `read_only` element out of every editing control
    # (older builds would let it be deleted, and that Save 422s).
    phone_narrated_title_preview_min_client_protocol: int = 4
    # KRI-467 (titles on phone Talking edits): a confirmed `opening_title` on a
    # phone-rendered `subtitled` edit (one clip, or the multi-clip Talking head)
    # renders as an editable text row instead of the planner asking to drop it
    # (`app.pipeline.phone_subtitled_title`): the worker writes it to the
    # variant's `text_elements`, the recipe draws it under the captions, and
    # the editor opens a text lane (`text_elements` capability + Save section)
    # for phone Talking edits. Also requires "positionedText" + "animatedText"
    # in `phone_render_verified_features` -- see
    # `app.services.phone_rollout.phone_subtitled_title_supported`. False: the
    # planner asks to make the edit without the title again and the editor's
    # text lane closes, as before; a title already on a variant keeps
    # rendering and survives Saves. No app build needed (the iOS editor follows
    # the capability map). Rollback: `fly secrets set
    # PHONE_SUBTITLED_TITLE_ENABLED=false --app nova-video` + `fly machine
    # restart <id>` (api + worker).
    phone_subtitled_title_enabled: bool = True
    # KRI-514 (closing text on phone Talking edits): a confirmed `closing_title`
    # ("end on the toast photo with a 'MY PICK' badge") on a phone-rendered
    # `subtitled` edit renders as an editable text row instead of the planner
    # asking to drop it (`app.pipeline.phone_subtitled_title.
    # talking_closing_element`): a lime tag on the closing photo, or near the
    # top off the speaker's face when there is none, held to the end. It rides
    # the KRI-467 text lane, so it also needs `phone_subtitled_title_supported`
    # and "authoredText" (the tag's background) in
    # `phone_render_verified_features` -- see
    # `app.services.phone_rollout.phone_subtitled_closing_title_supported`.
    # False: the planner asks to make the edit without that text again; text
    # already on a variant keeps rendering and survives Saves. Rollback: `fly
    # secrets set PHONE_SUBTITLED_CLOSING_TITLE_ENABLED=false --app nova-video`
    # + `fly machine restart <id>` (api + worker).
    phone_subtitled_closing_title_enabled: bool = True
    # KRI-707 (text on a spoken word on phone Talking edits): a brief text ask with
    # its spoken cue ("big 'ATAKAN' when I say 'Atakan'": literal + `facts.triggers`)
    # renders as an editable text row from the moment the take says the word, off the
    # speaker's face and the captions (`app.pipeline.phone_spoken_text`). The worker
    # reads the pinned brief, so no prompt or strategy field is involved. It rides the
    # KRI-467 text lane, so it also needs `phone_subtitled_title_supported` -- see
    # `app.services.phone_rollout.phone_subtitled_spoken_text_supported`. False: the
    # draft says "a Talking video can't show text at a word you say yet" (KRI-705)
    # and no row is placed; text already on a variant keeps rendering and survives
    # Saves. Rollback: `fly secrets set PHONE_SUBTITLED_SPOKEN_TEXT_ENABLED=false
    # --app nova-video` + `fly machine restart <id>` (api + worker).
    phone_subtitled_spoken_text_enabled: bool = True
    # New Voiceover authoring; existing saved recipes remain playable when off.
    phone_voiceover_spoken_text_enabled: bool = False
    phone_voiceover_source_audio_enabled: bool = False
    phone_voiceover_source_audio_min_client_protocol: int = 17
    # KRI-465 (editable title on phone Voiceover edits): the opening title the
    # status route shows (`narrated_title_text_elements`) opens as an ordinary
    # editable text element: the capability map opens `text_elements` and a
    # text Save recompiles the pinned recipe's `title-` layers
    # (`phone_narrated_plan.replace_narrated_title`). Requires the same
    # "positionedText" + "animatedText" device features as the captions and
    # an app build at `phone_narrated_title_preview_min_client_protocol` -- see
    # `app.services.phone_rollout.phone_narrated_title_edits_supported`. It
    # does not depend on `phone_narrated_title_enabled` (the planner's gate):
    # an already-rendered title stays editable. False: byte-identical to
    # KRI-455 -- the title is shown `read_only`, `text_elements` stays closed
    # and a `text_elements` Save 422s. Rollback: `fly secrets set
    # PHONE_NARRATED_TITLE_EDITS_ENABLED=false --app nova-video` + `fly machine
    # restart <id>` (api). No NEXT_PUBLIC twin (the iOS editor follows the
    # server's capability map and the title's `read_only` marker).
    phone_narrated_title_edits_enabled: bool = True
    # KRI-290 (free clip edits on phone Voiceover videos): a phone `narrated`
    # or montage `voiceover` variant's timeline opens (trim, extend, reorder,
    # split, delete) instead of being locked to the voiceover. A Save swaps the
    # pinned recipe's video track (`app.pipeline.phone_voiceover_cut`); the
    # video owns the clock, so shorter footage cuts the voice at the video's
    # end and longer footage plays on after it. See
    # `app.services.phone_voiceover_cut_editor`. False: byte-identical to
    # before -- `timeline` stays `locked_to_voiceover`/`voiceover_bed_fit` and
    # a timeline Save 422s. Rollback: `fly secrets set
    # PHONE_VOICEOVER_TIMELINE_EDITS_ENABLED=false --app nova-video` +
    # `fly machine restart <id>` (api). No NEXT_PUBLIC twin (the iOS editor
    # follows the server's capability map).
    phone_voiceover_timeline_edits_enabled: bool = True
    # A device recipe with no delivery (no poll, no upload) for this long is
    # presumed abandoned (app crashed, app deleted, notification never seen).
    # The reaper (app/tasks/device_render_reaper.py) flips it to
    # needs_attention so the item page can surface a retry instead of polling
    # forever. Default: 24h.
    device_render_stale_after_s: int = 86400

    def clip_facts_for(self, user_id: object) -> bool:
        """Global flag OR per-account allowlist (KRI-189); off by default."""
        if self.clip_facts_enabled:
            return True
        return user_id is not None and str(user_id) in {str(u) for u in self.clip_facts_user_ids}

    def phone_rendering_for(self, user_id: object) -> bool:
        """Apply the rollout cohort, or enroll every account in device-only mode.

        Device-only admission must not depend on a second flag being flipped in
        exactly the same deploy. Once web/cloud creation is retired, every
        authenticated native account is a phone-rendering account by definition.
        """
        return user_id is not None and (
            self.ios_device_only_mode
            or self.phone_rendering_enabled
            and (
                not self.phone_render_user_ids
                or str(user_id) in map(str, self.phone_render_user_ids)
            )
        )

    def kria_runtime_v2_phone_for(self, user_id: object) -> bool:
        """Runtime v2 cohort, or every authenticated account in device-only mode."""
        return user_id is not None and (
            self.ios_device_only_mode
            or self.kria_runtime_v2_phone_enabled
            and (
                not self.kria_runtime_v2_phone_user_ids
                or str(user_id) in map(str, self.kria_runtime_v2_phone_user_ids)
            )
        )

    # Storage
    storage_bucket: str
    storage_provider: str = "gcs"
    # Local filesystem storage is available only for deterministic E2E/dev
    # fixtures.  Production keeps STORAGE_PROVIDER=gcs; callers must also set
    # E2E_FIXTURES=true before the adapter or its HTTP route will activate.
    local_storage_root: str = ""
    local_storage_base_url: str = "http://127.0.0.1:8000/dev-qa/storage"
    e2e_fixtures: bool = False
    gcloud_project: str = ""
    google_application_credentials: str = ""
    google_service_account_json: str = ""

    # Redis / Celery
    redis_url: str = "redis://localhost:6379"

    # Database
    database_url: str

    @field_validator("database_url")
    @classmethod
    def normalize_postgres_scheme(cls, v: str) -> str:
        """Fly Postgres (and Heroku) provide postgres:// URLs, but
        SQLAlchemy 1.4+ only recognises the postgresql:// scheme."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql://", 1)
        return v

    @property
    def asyncpg_database_url(self) -> str:
        """Return a postgresql+asyncpg:// URL suitable for asyncpg.

        asyncpg does not understand libpq-specific query params like
        ``sslmode``.  We translate ``sslmode`` to asyncpg's ``ssl``
        param and swap the scheme to ``postgresql+asyncpg://``.
        """
        url = self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        # Translate libpq sslmode → asyncpg ssl
        sslmode_vals = qs.pop("sslmode", None)
        if sslmode_vals and "ssl" not in qs:
            qs["ssl"] = sslmode_vals  # e.g. ["disable"]
        cleaned_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=cleaned_query))

    # OpenAI (Whisper fallback)
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    creator_comparison_capture_enabled: bool = False
    creator_comparison_capture_dir: str = ""
    creator_comparison_holdout_capture_dir: str = ""
    creator_comparison_holdout_token: str = ""
    creator_comparison_capture_user_ids: list[str] = []

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"  # "gemini-2.5-flash" | "gemini-2.5-pro"
    # Editor agents intentionally bypass the fleet-wide `gemini_model` setting:
    # both need to compose edits while preserving accumulated constraints.
    # Keep the interactive executor and proactive director independently reversible.
    main_creator_model: str = "gemini-3.1-pro-preview"
    main_creator_thinking_level: str = "high"
    # KRI-1037 kill switch: a Main Creator provider deadline is answered by a lighter call
    # (thinking "medium", then the fallback model) inside the turn budget. Off = the old
    # behavior, where the deadline ends the turn. Read at import, so restart api + worker.
    main_creator_deadline_ladder_enabled: bool = True
    main_creator_output_mode: Literal["json", "native", "json_guarded"] = "json"
    edit_copilot_model: str = "gemini-3.1-pro-preview"
    edit_copilot_thinking_level: str = "high"
    edit_copilot_output_mode: Literal["json", "native", "json_guarded"] = "json"
    edit_director_model: str = "gemini-3.1-pro-preview"
    # Eval-only comparison model. It is never contacted by the production
    # Director endpoint, whose explicit review is capped at one paid request.
    edit_director_eval_comparison_model: str = "gemini-3.6-flash"
    edit_omni_model: str = "gemini-omni-flash-preview"

    # Central paid-AI reservation ledger. The code ships dark so the expand
    # migration can land before enforcement. Production enables it only after
    # split-project API keys and alert budgets have been verified.
    ai_cost_control_enabled: bool = False
    credit_quality_review_enabled: bool = False
    credit_quality_review_billing_verified: bool = False
    credit_quality_review_approved_run_id: str = ""
    credit_quality_review_max_run_usd: float = Field(default=0, ge=0, le=2)
    generated_images_enabled: bool = False
    generated_narration_enabled: bool = False
    generated_media_live_approved: bool = False
    generated_media_billing_organization: str = ""
    generated_media_price_verified_at: str = ""
    generated_media_max_request_usd: float = Field(default=0, ge=0, le=2.0)
    ai_openai_monthly_budget_usd: float = Field(default=0, ge=0, le=1000)
    ai_anthropic_monthly_budget_usd: float = Field(default=0, ge=0, le=1000)
    ai_openai_evaluation_budget_usd: float = Field(default=0, ge=0, le=1000)
    ai_openai_image_budget_usd: float = Field(default=0, ge=0, le=1000)
    ai_openai_narration_budget_usd: float = Field(default=0, ge=0, le=1000)
    ai_anthropic_evaluation_budget_usd: float = Field(default=0, ge=0, le=1000)
    ai_usage_environment: Literal["production", "development", "lab", "test"] = "development"
    # Optional operator-approved envelope for a local development manual-QA
    # session. It fills attribution for background Celery work that cannot
    # inherit HTTP headers. All three values are required; production ignores
    # them. Clear approval immediately after the session.
    ai_manual_qa_test_run_id: str = ""
    ai_manual_qa_max_cost_usd: float | None = Field(default=None, gt=0, le=2.0)
    ai_manual_qa_reservation_approved: bool = False
    # Provider usage is metered in USD even when Cloud Billing alerts are
    # configured in GBP. These conservative USD limits correspond to the
    # £22/£2/£2 allocations with exchange-rate headroom; operators may tighten
    # them without a deploy.
    ai_production_monthly_budget_usd: float = Field(default=28.0, gt=0, le=1000)
    ai_development_monthly_budget_usd: float = Field(default=25.0, gt=0, le=1000)
    ai_omni_lab_monthly_budget_usd: float = Field(default=2.5, gt=0, le=1000)
    ai_release_canary_monthly_budget_usd: float = Field(default=2.5, gt=0, le=1000)
    ai_weekly_smoke_max_cost_usd: float = Field(default=0.20, gt=0, le=2.0)
    # Official Omni video output is $0.10/s (2026-09-04 pricing); reserve a
    # small input/headroom allowance so the confirmation is conservative.
    ai_omni_cost_per_second_usd: float = Field(default=0.11, gt=0, le=5.0)
    # Stable operator-owned budget scope for all browser-confirmed Omni work in
    # one lab session/month. The browser never chooses this identity or cap.
    ai_omni_lab_test_run_id: str = ""
    ai_omni_lab_run_max_cost_usd: float | None = Field(default=None, gt=0, le=2.5)
    ai_omni_lab_reservation_approved: bool = False
    ai_reservation_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    ai_unknown_reservation_ttl_seconds: int = Field(default=86_400, ge=900, le=604_800)
    ai_paid_test_attribution_required: bool = True
    edit_director_daily_paid_limit: int = Field(default=3, ge=0, le=20)
    edit_director_cache_ttl_days: int = Field(default=90, ge=1, le=365)
    # Creator-scoped PostgreSQL ClipMeta reuse window. The Redis hot tier stays
    # capped at 24 hours so account deletion leaves at most a short-lived cache
    # copy; PostgreSQL rows are removed immediately by the creator FK cascade.
    media_analysis_cache_ttl_days: int = Field(default=90, ge=1, le=365)
    # KRI-384 rollout controls. The additive ownership contract ships before
    # upload/library clients; reuse and proposals activate independently only
    # after their API and physical-device evidence is complete.
    media_library_enabled: bool = False
    media_collection_proposals_enabled: bool = False
    media_collection_cache_only: bool = False
    # Per-account allowlist; ["*"] admits everyone (the ENABLED kill switch still applies).
    # Admission alone personalizes nothing: a creator must confirm a profile first.
    creator_editorial_taste_enabled: bool = False
    creator_editorial_taste_user_ids: list[str] = []
    # Independent kill switch for what ACTS on idea feedback: similarity ranking of saved
    # judgments, the repeat-of-refused-idea filter, and wrong-facts research exclusion.
    # False keeps profile/rules/examples and the two newest judgments (pre-feedback-loop).
    creator_editorial_taste_feedback_signals_enabled: bool = True

    def creator_editorial_taste_for(self, user_id: object) -> bool:
        if not self.creator_editorial_taste_enabled:
            return False
        allowed = {value.strip() for value in self.creator_editorial_taste_user_ids}
        return "*" in allowed or str(user_id) in allowed

    # Daily comparison of delayed Standard Usage Cost export with Nova's
    # settled reservation ledger. Targets and project attribution stay JSON so
    # both billing accounts can be added without shipping account IDs in git.
    billing_reconciliation_enabled: bool = False
    billing_export_targets_json: str = ""
    billing_project_environment_map_json: str = ""
    billing_reconciliation_delay_days: int = Field(default=3, ge=1, le=14)
    billing_reconciliation_threshold_pct: float = Field(default=0.10, gt=0, le=1)
    billing_reconciliation_pubsub_topic: str = ""
    storage_retention_enabled: bool = False
    storage_retention_delete_enabled: bool = False
    storage_retention_warning_days: int = Field(default=83, ge=1, le=364)
    storage_retention_inactive_days: int = Field(default=90, ge=2, le=3650)
    storage_retention_final_days: int = Field(default=365, ge=30, le=3650)
    storage_retention_superseded_days: int = Field(default=7, ge=1, le=90)
    storage_retention_report_only_days: int = Field(default=7, ge=1, le=30)
    storage_retention_scan_jobs: int = Field(default=100, ge=1, le=1000)

    # Admin
    admin_api_key: str = ""

    # Internal server-to-server auth (Next.js plan proxy → API).
    # Must match INTERNAL_API_KEY in the Next.js environment.
    internal_api_key: str = ""

    # Native mobile authentication.  These are deliberately separate from the
    # server-to-server key above: mobile access tokens are user credentials and
    # must never be accepted as the web proxy credential.
    mobile_jwt_secret: str = ""
    mobile_access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    mobile_refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)
    mobile_link_max_auth_age_seconds: int = Field(default=300, ge=60, le=900)
    # A rotated refresh token presented again within this window is treated
    # as a benign race (two client instances / a relaunch replaying the token
    # the other one just rotated) and answered 401 refresh_superseded WITHOUT
    # revoking the family; the client re-reads its store and retries. Reuse
    # outside the window still revokes the whole family (replay boundary).
    mobile_refresh_reuse_grace_seconds: int = Field(default=30, ge=0, le=300)
    # Temporary native uploads are covered by the bucket's one-day lifecycle
    # rules; never let a receipt advertise a deadline beyond that backstop.
    mobile_upload_retention_hours: int = Field(default=24, ge=1, le=24)
    mobile_google_client_ids: list[str] = []
    mobile_apple_client_ids: list[str] = []
    # Apple client-secret signing material used only to revoke credentials
    # during account erasure. Ordinary mobile sign-in never reads these.
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: str = ""

    # Apple Beta App Review demo account (KRI-111). A single fixed
    # email/password pair the App Review team can sign in with — Apple
    # rejects TestFlight submissions that require a real account. The hash
    # is never the plaintext password; see app/services/reviewer_login.py
    # for the scrypt format and app/cli/reviewer_login.py to mint one.
    # Rollback: `fly secrets set REVIEWER_LOGIN_ENABLED=false --app nova-video`
    # + `fly machine restart <id>` (api + worker).
    reviewer_login_enabled: bool = False
    reviewer_login_email: str = ""
    reviewer_login_password_hash: str = ""

    # HMAC key for pseudonymous creator/plan-item dataset groups. Exports fail
    # closed when unset or too short; it must not reuse an auth credential.
    training_dataset_split_secret: str = ""
    training_export_ttl_hours: int = Field(default=24, ge=1, le=168)

    # Split-rollout gate for the authenticated creation flow. False keeps the
    # old VoiceRecorder's validated voiceover-uploads/* paths working while the
    # API lands ahead of Vercel. After the owned direct uploader is deployed,
    # set true on Fly to require voiceover-uploads/direct/{user_id}/ exactly.
    generative_direct_voiceover_strict_enabled: bool = False

    # Runtime-v2 mutation gate. Existing CreationThread routes and every
    # runtime_version=1 project remain available when this is false; the new
    # durable turn/approval endpoints deliberately fail closed as 404.
    kria_runtime_v2_enabled: bool = False
    # KRI-520: Kria replies in the chat's language (Turkish or English). The chat's
    # language comes from the creator's own messages, then the device language; server
    # copy and the Main Creator/copilot prompts follow it. Default TRUE; kill switch =
    # false + restart (api + worker): every reply is English and prompts are
    # byte-identical to before.
    kria_reply_language_enabled: bool = True
    # Chat copilot continues on the editor's CURRENT UNSAVED state: the client may
    # send `editor_state` with a turn (EditorStateIn) and the planner + draft compiler
    # build on it instead of the saved variant / stale head. Off (default) = the
    # field is accepted and silently dropped, nothing stored, byte-identical to before.
    kria_editor_state_turns_enabled: bool = False
    # KRI-282: thumbnail clip-picker questions. The server attaches `clip_question` to a
    # "couldn't verify clips for X" question and honours the app's `clip_selection`
    # answer as authoritative. Default TRUE; kill switch = false + restart (the field
    # is then dropped, questions are text-only, byte-identical to before).
    kria_clip_selection_questions_enabled: bool = True
    # KRI-962 kill switch: in a Voiceover edit, a named shot two or more free footage clips
    # match equally well (Don Julio N1: the window sign and Atakan at the plaque, both 0.9)
    # is asked with the clip picker before the draft, the tied clips pre-ticked
    # (`app.kria.clip_ties`). Needs the picker flag above. False: byte-identical to before --
    # nothing is asked and the narrated alignment places the tied clips as it did. Apply:
    # `fly secrets set KRIA_VOICEOVER_TIE_QUESTIONS_ENABLED=false --app nova-video` +
    # `fly machine restart <id>` (api).
    kria_voiceover_tie_questions_enabled: bool = True
    # KRI-374: creator-uploaded song on phone montages (background music bed, or
    # lip-sync takes placed by song time). Default TRUE (no users yet); kill switch
    # = false + restart (api + worker): capabilities hide `media.song`, the policy
    # refuses `audio_strategy="user_song"`, byte-identical to before. Apply:
    # `fly secrets set USER_SONG_MONTAGE_ENABLED=false --app nova-video` +
    # `fly machine restart <id>`. Needs `musicBed` + `audioMix` in
    # PHONE_RENDER_VERIFIED_FEATURES (both already verified in prod).
    user_song_montage_enabled: bool = True
    # KRI-589: freehand finger drawings over a device-rendered edit (own `drawings` editor
    # lane -> `EditRecipeV2.drawing_layers`). Default TRUE; needs `freehandDrawing` in
    # PHONE_RENDER_VERIFIED_FEATURES (fly secret) and an app build declaring
    # `X-Kria-Client-Protocol` >= `freehand_drawing_min_client_protocol`. False: capabilities
    # close drawings.add/edit/trim and Save 422s `drawings_disabled` for new/changed
    # drawings; stored drawings still compile and play, and drawings.delete stays open.
    # Apply: `fly secrets set FREEHAND_DRAWING_ENABLED=false` (api + worker) + restart.
    freehand_drawing_enabled: bool = True
    freehand_drawing_min_client_protocol: int = 6
    # KRI-646: text "Bend" (curved/rainbow text, `bend` in [-100, 100]) drawn ON DEVICE.
    # The server only stores it, passes it into the phone recipe's text layers
    # (`PortableTextLayer.bend`) and gates old builds; server renderers never draw it.
    # Default TRUE; needs `textBend` in PHONE_RENDER_VERIFIED_FEATURES (fly secret) and
    # an app build declaring `X-Kria-Client-Protocol` >= `text_bend_min_client_protocol`.
    # False: capability `text_bend` is not advertised and newly authored bend is refused
    # by the app; stored values still round-trip. Apply:
    # `fly secrets set TEXT_BEND_ENABLED=false` (api + worker) + restart.
    text_bend_enabled: bool = True
    text_bend_min_client_protocol: int = 9
    # KRI-1007: caption entrance/exit/loop/speed (`animation_phases` on a caption row, or
    # the all-captions default `caption_meta.animation_phases`). Drawn on device from the
    # recipe's existing `animationPhases`. Default TRUE; the editor capability
    # `caption_animation` needs `authoredText` in PHONE_RENDER_VERIFIED_FEATURES and an app
    # build declaring `X-Kria-Client-Protocol` >= `caption_animation_min_client_protocol`;
    # an older build's Save that carries (or would drop) caption phases gets 426. False:
    # the capability is hidden and captions compile as before (stored phases are kept).
    # Apply: `fly secrets set CAPTION_ANIMATION_ENABLED=false` (api + worker) + restart.
    caption_animation_enabled: bool = True
    caption_animation_min_client_protocol: int = 24
    # Prompt fidelity (animation): "with animations" / "animated text" reaches the rendered text.
    # Default TRUE. brief_view reads an animation ask at any text scope, an unnamed ask gets one
    # tasteful entrance (`animation_ask.DEFAULT_TEXT_ENTRANCE`), the creation text pass applies
    # and verifies it (never raises), and the receipt checker judges it. False: byte-identical to
    # before (title-scope asks only, unnamed asks stay prose, no verification pass).
    # Apply: `fly secrets set TEXT_ANIMATION_ASK_ENABLED=false` (api + worker) + restart.
    text_animation_ask_enabled: bool = True
    # Text behind people ON THE PHONE (the device twin of `text_behind_subject_enabled`
    # below, which is the cloud Skia lane). When True, `compile_text_overlay` stamps
    # `behind_subject` on a phone recipe text layer whose text element asks for it, and
    # `KriaMediaEngine` occludes that layer with a per-frame Vision person mask
    # (`NativeSubjectMatte`). Also needs `behindSubject` in PHONE_RENDER_VERIFIED_FEATURES
    # (fly secret, set after a physical-device preview + export check) and, inside an app
    # request, a build declaring `X-Kria-Client-Protocol` >= the min protocol (21: the first
    # build that decodes the key). When any of the three is missing the compile STRIPS the
    # flag and the layer renders as plain text (trace `text_behind_subject_phone_fallback`,
    # the cloud's "falls back to plain text" wording) -- the recipe is byte-identical to
    # today's. A pinned recipe that already carries the flag answers 426
    # `client_update_required` (`text_behind_subject`) to an older build, which cannot
    # decode the key (mirrors `text_bend`). Default FALSE (dark ship). Apply:
    # `fly secrets set TEXT_BEHIND_SUBJECT_PHONE_ENABLED=true --app nova-video` +
    # `fly machine restart <id>` (api + worker); rollback = `...=false` + restart.
    text_behind_subject_phone_enabled: bool = False
    text_behind_subject_phone_min_client_protocol: int = 21
    # Talk-to-camera green screen ON THE PHONE (KRI-1115): `speaker_background` on a V2
    # recipe names a photo or looping video that `KriaMediaEngine` composites behind the
    # speaker clip's Vision person mask. Also needs `speakerBackground` in
    # PHONE_RENDER_VERIFIED_FEATURES (fly secret, set after a physical-device preview +
    # export check) and, inside an app request, a build declaring
    # `X-Kria-Client-Protocol` >= the min protocol (27: the first build that decodes the
    # key). When any of the three is missing the compile STRIPS the lane and the edit
    # renders with the creator's own background -- the recipe is byte-identical to
    # today's. A pinned recipe that already carries it answers 426
    # `client_update_required` (`speaker_background`) to an older build, which cannot
    # decode the key. The same gate hides the capability (`editor_capabilities.
    # speaker_background`, device-rendered Talk-to-camera variants only; thread capability
    # `speaker_background`) and refuses an editor Save that SETS one (422
    # `speaker_background_disabled`, 426 below the min protocol). Clearing a background
    # always works, so a rollback never traps one on an edit. Default FALSE (dark ship).
    # Apply: `fly secrets set SPEAKER_BACKGROUND_ENABLED=true --app nova-video` +
    # `fly machine restart <id>` (api + worker), AFTER `speakerBackground` is in
    # PHONE_RENDER_VERIFIED_FEATURES (set that first: with the flag on and the token
    # missing every request is stripped). Rollback = kill switch, never a code revert:
    # `fly secrets set SPEAKER_BACKGROUND_ENABLED=false` + restart (api + worker); recipes
    # already pinned keep their background, new compiles strip it. Runbook:
    # docs/pipelines/speaker-background.md.
    speaker_background_enabled: bool = False
    speaker_background_min_client_protocol: int = 27
    # KRI-821: Sounds > Original audio on a phone Voiceover edit (narrated, or a montage
    # `voiceover`). The capability `original_audio` is advertised to app builds declaring
    # `X-Kria-Client-Protocol` >= this; older builds hydrate the row only for guided edits,
    # so they never receive the key (the row would open at a wrong level).
    voiceover_original_audio_min_client_protocol: int = 15
    # KRI-828: a creator's freeze frame on a phone montage moment (a main-track
    # `hold_duration` longer than one frame, capability `freezeFrame`). Planned only when
    # `freezeFrame` is in PHONE_RENDER_VERIFIED_FEATURES (device parity, set after a
    # physical-device preview + export check) and the app build the creator acted from
    # declared `X-Kria-Client-Protocol` >= this (KRI-898: stamped on the message at submit
    # for the draft, and on the Job as `creator_client_protocol` at approval for the render;
    # no stamp = no hold). Without it the plan carries no hold, the receipt keeps KRI-777's
    # honest "Couldn't" and the rest of the video renders. A held recipe later opened on an
    # older build still gets 426 client_update_required (`freeze_frame`) from device_render
    # / phone_editor instead of an undecodable recipe.
    freeze_frame_min_client_protocol: int = 16
    # KRI-742: voice dictation in the chat composers. `POST /dictation/transcribe` (Whisper) and
    # the `voice_dictation` capability the iOS mic button reads. Default TRUE; this is the kill
    # switch only. False: the route 404s and the capability is not advertised, so the app hides
    # the mic. Audio is never stored. Apply: `fly secrets set VOICE_DICTATION_ENABLED=false`
    # (api) + restart.
    voice_dictation_enabled: bool = True
    # One word at a time for caption `Display: Word` (`karaoke.single_word`, capability
    # `singleWordText`): the layer's runs are standalone-centred words and only the active one
    # is drawn. Emitted only when `singleWordText` is in PHONE_RENDER_VERIFIED_FEATURES (fly
    # secret) AND the compile knows the app build declares `X-Kria-Client-Protocol` >=
    # `single_word_text_min_client_protocol` (an HTTP Save, or a variant an earlier qualifying
    # Save stamped); otherwise captions compile to the full-line karaoke sweep, byte-identical
    # to before. Rollback: remove `singleWordText` from PHONE_RENDER_VERIFIED_FEATURES (+ restart
    # api and worker). Builds below the protocol get 426 `client_update_required` (feature
    # `single_word_text`) instead of a recipe they cannot decode.
    single_word_text_min_client_protocol: int = 11
    # KRI-561: the song-order question is answered on a horizontal song timeline (play the
    # song, drop clips onto song time) instead of the vertical reorder list. Kill switch:
    # `fly secrets set SONG_ORDER_TIMELINE_ENABLED=false` + restart (api): capabilities drop
    # `song_order_placements` and the app falls back to the reorder card. Only effective
    # while USER_SONG_MONTAGE_ENABLED holds.
    song_order_timeline_enabled: bool = True
    # KRI-580: the creator can say which stretch of the song they filmed in; takes are then matched
    # only there (a repeated verse elsewhere stops splitting the group). Kill switch:
    # `fly secrets set SONG_FILMING_RANGE_ENABLED=false` + restart (api + worker): the capability
    # drops, the setter 404s and any stored range is ignored (the whole song is matched, as before).
    song_filming_range_enabled: bool = True
    # KRI-569: "add captions" on a creator-song montage shows the song's lyrics as the
    # standard caption lane (same style/font/text controls as narration captions); without
    # a song the chat asks for it instead of failing the render. Kill switch:
    # `fly secrets set SONG_CAPTIONS_ENABLED=false` + restart (api + worker): no lyric
    # captions are planned and no ask is made, byte-identical to before. Recompiles of
    # already-approved plans do not read it. Only effective while USER_SONG_MONTAGE_ENABLED holds.
    song_captions_enabled: bool = True
    # A creator-song montage nobody asked captions for still pins its lyrics, hidden, so
    # the editor's Show captions toggle can reveal them. Kill switch:
    # `fly secrets set SONG_CAPTIONS_AVAILABLE_BY_DEFAULT=false` + restart (api + worker):
    # new montages without a captions ask carry no lyric track, as before.
    song_captions_available_by_default: bool = True
    # KRI-443: live plan block feed. After Create on a v2 thread, the render emits
    # `plan_block` events into CreationThreadEvent (read via GET /delta), and
    # POST /creation-threads/{id}/turns/{turn}/cancel-render is enabled. Default
    # FALSE (dark). Off = no events, no job metadata, capability false, byte-identical.
    # Kill switch = false + restart (api + worker). Contract: docs/pipelines/live-plan-blocks.md.
    live_plan_review_enabled: bool = False
    # Longest song accepted (seconds); a song-owned montage is capped at 120 s.
    user_song_max_duration_s: float = Field(default=600.0, gt=0)
    # How long a planner turn waits for song analysis / take alignment before it
    # answers "still checking your clips".
    song_alignment_turn_deadline_s: float = Field(default=45.0, ge=0)
    # Aligner thresholds (see app/pipeline/song_alignment.py). A take is
    # confident only when peak_z >= confident_peak_z AND peak_ratio >=
    # confident_peak_ratio AND the drift check passes AND (the text anchor agrees
    # within song_align_text_agree_s OR the audio peak alone is strong).
    song_align_confident_peak_z: float = 8.0
    song_align_confident_peak_ratio: float = 1.5
    song_align_strong_peak_z: float = 12.0
    song_align_strong_peak_ratio: float = 2.0
    song_align_text_agree_s: float = 0.15
    song_align_drift_tolerance_s: float = 0.04
    # A take is "ambiguous" (could sit at several places) only when the song itself
    # repeats there: the song segments at the best placement and at an alternate must
    # correlate at least this much (0 = unrelated, 1 = identical; an exact loop is
    # ~1, a chorus with 85% shared material ~0.85). Alternates below this are
    # coincidental peaks and are dropped; a take left with none is "unmatched".
    song_align_repeat_similarity_min: float = Field(default=0.5, ge=0, le=1)
    # KRI-466: a take sung over earbuds carries the creator's voice but not the song,
    # so no audio peak exists. A lyrics-only placement is accepted when the take's words
    # match the song's words closely: enough words, tightly consistent per-word offsets,
    # and most of the take's words inside the matched span. Kill switch = false.
    song_align_lyrics_enabled: bool = True
    # KRI-471: lyric matches are *likelihood* evidence, never a gate. min_words is only
    # the least matched words that emit a candidate; the scales below shape the score
    # in song_alignment._lyric_likelihood.
    song_align_lyrics_min_words: int = Field(default=3, ge=3)
    song_align_lyrics_words_scale: float = Field(default=3.0, gt=0)
    song_align_lyrics_spread_scale_s: float = Field(default=0.35, gt=0)
    # DEPRECATED no-ops (hard gates before KRI-471); kept one release so old env
    # overrides still load. Remove after the next release.
    song_align_lyrics_min_density: float = Field(default=0.6, ge=0, le=1)
    song_align_lyrics_max_spread_s: float = Field(default=0.35, gt=0)
    song_align_lyrics_confidence: float = Field(default=0.5, ge=0, le=1)
    # KRI-471 likelihood placement. Candidates scoring below the floor are dropped
    # ("evidence exists"), nothing else is a hard cut. A take asks the creator when
    # its margin over the runner-up is below ask_margin or its likelihood below
    # ask_likelihood; candidates within tie_ratio of the best are interchangeable.
    song_align_candidate_floor: float = Field(default=0.05, ge=0, le=1)
    song_align_ask_likelihood: float = Field(default=0.35, ge=0, le=1)
    song_align_ask_margin: float = Field(default=0.15, ge=0, le=1)
    song_align_tie_ratio: float = Field(default=0.85, ge=0, le=1)
    song_align_max_overlap_s: float = Field(default=1.0, ge=0)
    song_align_assign_enabled: bool = True
    song_align_stack_unasked: bool = False
    song_align_candidates_max: int = Field(default=6, ge=1)
    # Padding (seconds) around the matched words when trimming a take to its match.
    song_align_match_pad_s: float = Field(default=0.5, ge=0)
    # Fixed offset (seconds) added to every aligned delta to absorb a measured
    # analysis-proxy vs original audio timing difference (AAC priming / edit
    # lists). 0.0 until the device fixture measures one.
    song_alignment_proxy_offset_s: float = 0.0
    # KRI-282: conflict-choice questions. When the creator's instructions conflict (e.g.
    # "chronological" + "group by sport" over mixed-filmed clips) the server asks one
    # focused question (`choice_question`) and follows the app's `choice_selection`.
    # Default TRUE; kill switch = false + restart (the field is dropped, no question is
    # asked, the montage behaves exactly as before).
    kria_choice_questions_enabled: bool = True
    # Prompt fidelity, errors unit: a provider/transport failure on a follow-up is answered
    # with the honest "try again" reply (after ONE bounded extractor retry) instead of "I
    # couldn't reliably read every requested change"; sensitive agents keep the error CLASS
    # (never text) in their logged error; the "Animate the title" chip is offered only when
    # the editor can resolve a title. Default TRUE; kill switch = false + restart (api +
    # worker) -> byte-identical to the previous behaviour on all three.
    kria_error_honesty_enabled: bool = True
    # KRI-724: on a one-take phone Talking draft, ask the creator which time a spoken word /
    # line is meant ("the first time SHE says Merope"; a line to cut said twice) before
    # approval, and render exactly that occurrence. Default false; on = Fly secret
    # SPEECH_OCCURRENCE_QUESTION_ENABLED=true (api + worker) + restart. Off: the question is
    # never asked and renders read the first match exactly as before (answers only exist
    # where it was asked). Rollout + order: docs/pipelines/creator-render-contract.md.
    speech_occurrence_question_enabled: bool = False
    # KRI-802: an app build declaring `X-Kria-Client-Protocol` >= this answers a reply's
    # choice card and clip picker on one turn, so the planner asks a title-wording question
    # and a clip picker in the same reply. Lower builds and the web keep one question per
    # turn. Kill switch: `fly secrets set KRIA_QUESTION_BATCH_MIN_CLIENT_PROTOCOL=999`
    # (api + worker) + restart; messages sent after it get one question per turn again.
    kria_question_batch_min_client_protocol: int = 14
    # KRI-901: an app build declaring `X-Kria-Client-Protocol` >= this shows the "Mark the
    # plate" card, so a phone Talking edit whose brief keeps text off an unmarked object asks
    # for the mark before approval. Lower builds and the web are never asked (approval as
    # before; the receipt says `no_selection`). Kill switch: `fly secrets set
    # PROTECTED_REGION_MARK_MIN_CLIENT_PROTOCOL=999` (api + worker) + restart.
    protected_region_mark_min_client_protocol: int = 19
    # KRI-723: an app build declaring `X-Kria-Client-Protocol` >= this answers a
    # `moment_order_question` ("put your moments in order and show me where each one is"),
    # asked before approval when the clip record cannot place a moment the creator named.
    # Lower builds are never asked and get no pins: today's v1 windows, byte-identical, with
    # an honest "couldn't find it" receipt. No model call either way. 20 = the first iOS build
    # with the moment card (`MomentOrderCard`, `KriaClientProtocolContract.version` 20); never
    # reuse a number another feature already took. Deploy order (worker reads v2 envelopes
    # before any api writes them) is met: workers have read v2 since #1808. Kill switch:
    # `fly secrets set MOMENT_ORDER_MIN_CLIENT_PROTOCOL=999` (api + worker) + restart turns
    # off the question AND every v2 pin.
    moment_order_min_client_protocol: int = 20
    # Camera-roll idea `open_questions`: an app build declaring `X-Kria-Client-Protocol` >=
    # this renders the questions and is held by the 409 `story_open_questions` gate on
    # select. Lower builds never see the list and are never gated (they could neither answer
    # nor explain the 409). 22 = the first iOS build with the question UI. Kill switch:
    # `OPEN_QUESTIONS_MIN_CLIENT_PROTOCOL=999` (api) + restart.
    open_questions_min_client_protocol: int = 22
    # Camera-roll idea extras: `content_plan.form` and `content_plan.open_questions` on stored
    # stories. Default TRUE. False: new ideas are stored in the legacy shape (no form, no
    # questions) and `select` never gates on questions. Rollback of the deploy that introduced
    # them: set this false FIRST (api + worker, restart) so no new rows carry keys the previous
    # image's `extra="forbid"` plan rejects, then strip the keys from existing rows
    # (docs/pipelines/media-library.md). Apply:
    # `fly secrets set CAMERA_ROLL_STORY_EXTRAS_ENABLED=false`.
    camera_roll_story_extras_enabled: bool = True
    # KRI-815: with footage and no voiceover, Kria chat writes a footage-grounded voiceover
    # script (`voiceover_script` on the reply) instead of a narrated draft that can only fail at
    # approval. Independent of TRANSCRIPT_HELPER_ENABLED. Kill switch: false + restart (api +
    # worker): a narrated draft without a voiceover still never reaches approval; Kria asks the
    # creator to record or import one, with no script.
    kria_voiceover_script_enabled: bool = True
    # KRI-470: "one plan, obeyed everywhere". Evaluated ONCE, when a creator
    # render contract is stamped onto a new job (`creator_plan_authority_version`
    # in `Job.all_candidates`); workers branch on that stamp, never on this live
    # flag, so a running job cannot change behaviour. False = new jobs are
    # unstamped = legacy behaviour. Default TRUE; kill switch = false + restart
    # api and worker (affects new jobs only).
    kria_plan_authority_enabled: bool = True
    # KRI-722: mint one immutable prepared execution snapshot at approve and stamp
    # it on the approval, execution and Job. Read ONCE at approve; workers branch on
    # the stamp (`creator_prepared_execution`), never on this flag. False = new
    # approvals are unstamped = legacy. Kill switch: fly secret false + restart
    # api and worker (already-stamped work keeps being verified).
    kria_prepared_execution_enabled: bool = True
    # KRI-189 (KRI-185 P3): clip facts -- capture time, place name and a
    # best-guess landmark per clip, each with provenance. Gates SERVER
    # CONSUMPTION only: the landmark agent run, exposing facts to the Main
    # Creator / edit planner prompts, capture-time ordering, the by_capture_time
    # / by_route order intents. The API always accepts the additive optional
    # attach fields. False (default) with no allowlist match is byte-identical
    # to before. `clip_facts_user_ids` (JSON list of account UUIDs) enables the
    # feature for those accounts even while the global flag is false, so the
    # device check can run in prod. Rollback: `fly secrets set
    # CLIP_FACTS_ENABLED=false CLIP_FACTS_USER_IDS=[] --app nova-video` +
    # `fly machine restart <id>` (api + worker).
    clip_facts_enabled: bool = False
    clip_facts_user_ids: list[UUID] = Field(default_factory=list)
    # KRI-187: phone-rendering accounts are offered runtime v2 (`GET
    # /creation-threads/capabilities` -> `runtime_versions: [1, 2]`) and a v2
    # strategy approval may dispatch a device job. False (default) keeps
    # phone accounts on [1], byte-identically. `kria_runtime_v2_phone_user_ids`
    # narrows the flag to named accounts (empty = every phone account), the
    # same shape as `phone_render_user_ids`, so a device check can run in prod.
    # `runtime_version` is fixed at thread creation: existing v1 threads stay
    # v1. Rollback: `fly secrets set KRIA_RUNTIME_V2_PHONE_ENABLED=false` +
    # restart (api + worker); phone accounts are offered [1] again.
    kria_runtime_v2_phone_enabled: bool = False
    kria_runtime_v2_phone_user_ids: list[UUID] = Field(default_factory=list)
    kria_turn_lease_seconds: int = Field(default=15, ge=10, le=120)
    # KRI-709: a turn whose planner stopped on a time budget ("still checking your
    # clips") is re-run by the server instead of asking the creator to say "go ahead".
    # Caps the runs one turn may take (each can pay for a Main Creator call plus vision),
    # counted by `lease_epoch`, so a turn that waited queued is not shortchanged. Past
    # it the old "come back later" reply is sent (the AI really is stuck). 0 or 1 = off,
    # byte-identical to the pre-fix behaviour.
    # Apply: fly secret KRIA_TURN_AUTO_CONTINUE_MAX_RUNS=1 + restart (api + worker).
    kria_turn_auto_continue_max_runs: int = Field(default=4, ge=0, le=8)
    # KRI-552: an identical re-send of the creator's latest message (the chat looked stuck) is
    # answered in one line instead of being queued and planned a second time. False restores
    # the old behaviour: the copy queues behind the running turn.
    # Apply: fly secret KRIA_DUPLICATE_RESEND_ENABLED=false + restart (api).
    kria_duplicate_resend_enabled: bool = True
    # KRI-188: thread-level Creative Brief ledger + deterministic scope router +
    # per-requirement receipts on runtime-v2 turns. False (default): the planner,
    # router, draft validation, dispatch request and replies are byte-identical
    # to before. `KRIA_CREATIVE_BRIEF_USER_IDS` (comma-separated or JSON list of
    # creator user ids) turns it on for just those accounts while the global flag
    # is still off, so a device check can run in prod. Apply:
    # `fly secrets set KRIA_CREATIVE_BRIEF_ENABLED=true --app nova-video`
    # + restart api + worker. Rollback: set it false + restart.
    kria_creative_brief_enabled: bool = False
    # Deploy readers everywhere before enabling snapshot writers.
    kria_brief_binding_enabled: bool = False
    kria_brief_binding_user_ids: Annotated[list[str], NoDecode] = []
    # KRI-775: before the approval card, run the phone montage plan the worker will run
    # (`app.services.phone_montage_preflight`) and hold back a draft it would refuse,
    # with the reason and the "simpler version?" choice. Deterministic, fail-open.
    # Default TRUE; kill switch = false + restart (api + worker): approval is offered
    # exactly as before and the worker's own check is the only one.
    kria_phone_montage_preflight_enabled: bool = True
    # KRI-881: research a phone montage's typed research ask before approval, show the
    # sourced wording + citations, and pin them to the approval (`app.kria.research_preview`).
    # false => the render researches after approval exactly as before. Restart api + worker.
    kria_research_preview_enabled: bool = True
    # Prompt fidelity (disclosure): before approval, say what the route that will render the
    # draft does not read (unread strategy fields, asks like animated text, research after
    # approval), show the length the plan renders, and approve (with that disclosure) an ask
    # whose checker cannot decide before render instead of holding the draft. Never blocks.
    # Default TRUE; false + restart (api + worker) = byte-identical to before
    # (`app.kria.route_disclosure`, agents/DECISIONS.md "Route disclosure").
    kria_route_disclosure_enabled: bool = True
    # KRI-1097 wave 2 (prompt fidelity on every route). ROUTE_REGISTRY_ENABLED: each render
    # route reads its own capability table (`app.kria.route_capabilities`), so voiceover,
    # Talking, Narrated, creator-song, spoken-excerpt and slide drafts get their own honest
    # card lines, and the lines and route declines show for every account (not only approval
    # readiness ones). The per-lane switches below each gate one route's follow-the-prompt
    # change. Every default TRUE; false + restart (api + worker) = byte-identical to before
    # that change (agents/DECISIONS.md "Prompt fidelity wave 2").
    route_registry_enabled: bool = True
    voiceover_route_follows_prompt_enabled: bool = True
    talking_head_route_follows_prompt_enabled: bool = True
    narrated_route_follows_prompt_enabled: bool = True
    lipsync_route_follows_prompt_enabled: bool = True
    speech_excerpt_route_follows_prompt_enabled: bool = True
    slide_route_follows_prompt_enabled: bool = True
    planner_boundary_honest_enabled: bool = True
    copilot_direction_follows_request_enabled: bool = True
    creator_jobs_no_catalog_song_freeze_enabled: bool = True
    # KRI-721: enroll new approval writers only after API/worker readers deploy.
    # Existing stamped assessments remain mandatory when enrollment is disabled.
    kria_approval_readiness_enabled: bool = False
    kria_approval_readiness_user_ids: Annotated[list[str], NoDecode] = []
    # NoDecode: pydantic-settings would otherwise JSON-decode the env string
    # before the validator below runs, crashing boot on a bare id or CSV.
    kria_creative_brief_user_ids: Annotated[list[str], NoDecode] = []
    # KRI-756: footage-aware suggestion chips (GET /creation-threads/{id}/suggestions).
    # Default off => the endpoint answers status "unavailable", capabilities carry
    # `suggestion_chips: false`, nothing is written or stamped. The user-id list
    # (comma-separated or JSON) turns it on for single accounts while the global flag is
    # off (canary). Clients gate on the capabilities bool: no NEXT_PUBLIC twin.
    # Apply: `fly secrets set -a nova-video KRIA_SUGGESTION_CHIPS_ENABLED=true` + restart
    # api + worker. docs/pipelines/suggestion-chips.md.
    kria_suggestion_chips_enabled: bool = False
    kria_suggestion_chips_user_ids: Annotated[list[str], NoDecode] = []
    # KRI-756: bounds on the chip-wording agent (`nova.chat.suggester`). NOT flags: the one
    # flag above gates the whole feature. 0 runs/thread disables only the agent (chips fall
    # back to template labels): the cost kill lever without a second flag. The daily limit
    # counts paid model ATTEMPTS (a retry is a second one), not chip runs, from
    # `ai_cost_reservations` by feature name (ai_cost_control.py).
    kria_suggester_max_runs_per_thread: int = Field(default=12, ge=0, le=100)
    kria_suggester_daily_limit_per_user: int = Field(default=20, ge=0, le=500)

    @field_validator(
        "kria_creative_brief_user_ids",
        "kria_brief_binding_user_ids",
        "kria_approval_readiness_user_ids",
        "kria_suggestion_chips_user_ids",
        mode="before",
    )
    @classmethod
    def parse_creative_brief_user_ids(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return value
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def voice_behind_footage_enabled(self) -> bool:
        """Is the continuous-voice route actually available to NEW jobs? (KRI-479)

        It renders only for plan-authority (stamped) jobs and needs the camera-audio renderer.
        Questions, the prompt section and `voice_mode` repairs advertise the shape only while
        both hold, so no flag combination promises a render that cannot happen.
        """
        return bool(self.kria_plan_authority_enabled and self.speech_excerpt_montage_enabled)

    def brief_binding_for(self, user_id: object) -> bool:
        """Writer cohort only; readers always honor previously saved bindings."""
        return self.kria_brief_binding_enabled or (
            user_id is not None and str(user_id) in set(self.kria_brief_binding_user_ids)
        )

    def approval_readiness_for(self, user_id: object) -> bool:
        return self.brief_binding_for(user_id) and (
            self.kria_approval_readiness_enabled
            or str(user_id) in self.kria_approval_readiness_user_ids
        )

    def suggestion_chips_for(self, user_id: object) -> bool:
        """Global flag OR the per-account allowlist (allowlist only ever adds)."""
        if self.kria_suggestion_chips_enabled:
            return True
        return user_id is not None and str(user_id) in {
            str(uid) for uid in self.kria_suggestion_chips_user_ids
        }

    def creative_brief_for(self, user_id: object) -> bool:
        """Global flag OR the per-account allowlist (allowlist only ever adds)."""
        if self.kria_creative_brief_enabled:
            return True
        return user_id is not None and str(user_id) in {
            str(uid) for uid in self.kria_creative_brief_user_ids
        }

    # GET /creation-threads/{id} degrades a thread whose render-graph edge
    # (PlanItem/CreatorAgentSession/Job ownership) has drifted incoherent,
    # instead of 404ing the whole project and its intact chat transcript.
    # False reproduces the pre-fix fail-closed 404 byte-identically. See
    # KRI-26 / agents/DECISIONS.md.
    creation_thread_degraded_projection_enabled: bool = True
    # Live speech-cleanup rollout state. New jobs always receive an explicit
    # required_v1/off_v1 contract; legacy_auto is historical-job compatibility
    # only and is intentionally rejected as a live setting.
    speech_cleanup_mode: Literal["opt_in", "disabled"] = "opt_in"
    # Chat-first preflight is deployed dark, then advanced independently from
    # the legacy render-time switch.  ``shadow`` persists bounded analysis but
    # does not block creation; ``enforce`` exposes the decision contract.
    speech_cleanup_preflight_mode: Literal["off", "shadow", "enforce"] = "off"
    speech_cleanup_preflight_rollout_percent: int = Field(default=0, ge=0, le=100)
    # Preflight downloads/transcription must never queue behind video renders or
    # run on the small maintenance machine.
    speech_cleanup_analysis_queue: str = "speech-analysis"
    # When the plan item carries no Gemini clip transcript (Kria v2 never saves
    # one), the preflight worker asks Gemini to transcribe the narration's first
    # 30 s so whisper's detected language is still cross-checked before the cut
    # plan is built (a mixed-language take also gets the rest of the take heard,
    # see `speech_cleanup_mixed_relisten_enabled`). Best-effort; False skips the
    # call (item transcript only).
    speech_cleanup_gemini_reference_enabled: bool = True
    # KRI-843: a take whose Gemini reference speaks two or more languages (Don Julio T1:
    # an English question, a Spanish answer, Turkish chatter) has no single right whisper
    # language. The preflight worker transcribes it once more per extra language (at most 3
    # extra whisper-1 passes, mixed takes only, ~$0.006/min each) and takes, stretch by stretch,
    # the pass the reference confirms; the first pass stands everywhere else. false = the EN/TR
    # cross-check alone (re-run the whole take in one language), byte-identical.
    # Apply: fly secret + worker restart.
    speech_cleanup_mixed_language_enabled: bool = True
    # KRI-1016: whisper-1 decodes a whole file in ONE language, so on a mixed take (Plaza T2:
    # English and Turkish, 48 s) the language it picks is written for every stretch, and a
    # stretch in the other language comes back as a translation or as no words at all. With the
    # mixed-language lane on, a mixed take gets two more things: Gemini also hears the take past
    # its first 30 s (one more call, only when the 30 s sample already names two or more
    # languages), so the reference covers the whole take; and every stretch of 1 s or more that
    # the transcript left wordless but the reference heard is cut out and transcribed ALONE, in
    # each of the take's languages (whisper-1 on those stretches only, at most 6 short calls, a
    # few cents), then kept only where the reference confirms the words. A single-language take
    # makes no extra call. false = byte-identical to KRI-843 (30 s reference, no re-listen).
    # Apply: fly secret + worker restart.
    speech_cleanup_mixed_relisten_enabled: bool = True

    # yt-dlp cookies for admin URL imports. Use YTDLP_COOKIES_B64 in hosted
    # environments (secret-safe, decoded into a short-lived 0600 temp file) or
    # YTDLP_COOKIES_PATH for local development / mounted secret files.
    ytdlp_cookies_b64: str = ""
    ytdlp_cookies_path: str = ""

    # Transcription backend
    transcriber_backend: str = "gemini"  # "gemini" | "whisper"

    # Template
    default_template_id: str = ""

    # Whisper
    whisper_backend: str = "openai-api"  # "openai-api" | "local"
    whisper_model: str = "base.en"
    # Narrated-voiceover transcription model override (local backend only). The
    # narration becomes burned + editable captions, so accuracy matters more here
    # than for clip analysis — a larger model means fewer words to hand-correct.
    # Empty → fall back to `whisper_model`. Kill switch: set to "base.en" to revert
    # the accuracy bump (smaller/faster, the pre-bump default). Slower + more RAM
    # than base.en; only affects the local backend (openai-api uses whisper-1).
    narrated_whisper_model: str = "small.en"

    # Hard ceiling on lyric extraction wall time (LRCLIB search + Whisper
    # transcription + alignment). Beat analysis caps at 300s; the lyric task
    # runs after it so we keep a tight budget. LRCLIB itself needs no API
    # key — see app/services/lrclib_client.py.
    lyrics_extraction_timeout_s: float = 90.0

    # CORS — comma-separated or JSON array in env:
    # ALLOWED_ORIGINS=https://usekria.com,http://localhost:3000
    # ALLOWED_ORIGINS='["https://usekria.com","http://localhost:3000"]'
    allowed_origins: list[str] = ["http://localhost:3000"]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v: object) -> object:
        """Support both JSON-array and comma-separated ALLOWED_ORIGINS values."""
        if not isinstance(v, str):
            return v
        raw = v.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return v
        return [part.strip() for part in raw.split(",") if part.strip()]

    # Waitlist admin
    waitlist_admin_secret: str = "changeme"

    # Resend (transactional email) — leave empty to skip confirmation emails
    resend_api_key: str = ""

    # Eval harness — gates per-slot GCS upload for visual comparison
    eval_harness_enabled: bool = False

    # Single-pass FFmpeg encode — collapses stages 1-6 (reframe, pre-burn,
    # curtain, join, text overlay) into one filter_complex invocation. Default
    # OFF; flip after empirical SSIM/VMAF parity is proven on REF jobs. Can
    # also be forced per-job via the `force_single_pass` kwarg on
    # orchestrate_template_job.
    single_pass_encode_enabled: bool = False

    # Kill switch for the format-aware talking-head archetype (Lane C/D). When
    # False, a job whose plan declares edit_format="talking_head" renders as the
    # default montage — the assembler module exists but the dispatch never routes
    # to it. Lane D (generative_build dispatch) is the consumer; Lane C only
    # defines the flag so the kill switch ships alongside the feature. Mirrors the
    # LYRIC_DYNAMIC_CROSSFADE_ENABLED rollback pattern: flip the Fly secret +
    # restart workers, no redeploy. Default OFF until the assembler is wired +
    # verified end-to-end.
    edit_format_talking_head_enabled: bool = False

    # KRI-127 kill switch for open-vocabulary clip intents: the chat agent emits
    # generic label/group/order/include intents, a resolver matches them to clips
    # from the shared understanding record (with a capped vision re-query), and
    # on-screen labels must pass the grounding fence in app/schemas/clip_intents.py.
    # False (default) disables visual clip intents. Transcript label intents
    # retain their separate pinned-narration materializer. No sport-regex fallback.
    # See docs/pipelines/clip-understanding.md. Apply: `fly secrets set
    # CLIP_INTENTS_ENABLED=true --app nova-video` + restart api + worker.
    clip_intents_enabled: bool = False
    kria_prompt_moment_inspection_enabled: bool = False
    kria_prompt_moment_inspection_allow_provider: bool = False
    # KRI-680: measure each analysed video's brightness (mean luma + share of near-black
    # frames, subprocess FFmpeg on the analysis copy; app/services/clip_exposure.py) and
    # put it in the clip record as words ("very dark, almost nothing visible (...)") so
    # "skip the very dark clips" can be matched. False (default): nothing is measured and
    # stored measurements are ignored, byte-identical to before. Clips analysed while it
    # was off have no measurement (no backfill). Apply: `fly secrets set
    # CLIP_EXPOSURE_FACT_ENABLED=true --app nova-video` + restart api + worker.
    clip_exposure_fact_enabled: bool = False
    # KRI-151: durable semantic preparation before the first footage-based plan.
    creator_clip_preparation_enabled: bool = False
    # Chat-turn budget for the on-demand vision re-query (download + File API
    # upload per clip). Cacheable overflow continues in Celery (KRI-154);
    # technical failures stay distinct from visual uncertainty (KRI-151).
    clip_intents_max_vision_requeries: int = Field(default=4, ge=0, le=12)
    clip_intents_vision_deadline_s: float = Field(default=25.0, gt=0, le=60)
    # KRI-282: the Kria v2 chat turn has no background vision lane for chat
    # clips (no asset_id to dispatch against), so a 47-clip montage that needs
    # ~17 per-clip vision answers (measured) used to stall at the 4-call cap and answer
    # "try again shortly" forever. Kria turns may spend more foreground vision
    # (answers are cached per clip, so every turn only moves forward).
    kria_clip_intents_max_vision_requeries: int = Field(default=18, ge=0, le=24)
    kria_clip_intents_vision_deadline_s: float = Field(default=40.0, gt=0, le=60)
    # KRI-667 kill switch: the clip-intent planner may state "leave this clip out"
    # ("Skip the quad bike clip", "çamaşır klibini kullanma") as an `exclude` intent,
    # in any language. The resolver keeps a clip for it only at confidence >= 0.8 and
    # never asks the creator about it; `app.kria.clip_scope.apply_clip_exclusions`
    # then removes those clips from the pick (never a clip another intent names, never
    # the last own clip). False: byte-identical to before -- the planner prompt has no
    # `exclude` op, an exclusion the model writes is dropped silently, nothing is
    # removed. Strategies already holding an `exclude` intent still load. Rollback:
    # `fly secrets set CLIP_INTENT_EXCLUDE_ENABLED=false --app nova-video` +
    # `fly machine restart <id>` (api + worker).
    clip_intent_exclude_enabled: bool = True
    # KRI-585 kill switch: on the phone route that plays every clip's own sound, what
    # the clips say may order a montage nobody ordered (a welcome right after the
    # creator's opening shot, a sign-off at the end) and seat its windows (the
    # welcome's whole line, a quiet ending on a pinned clip that only introduces
    # someone), and the reply says so (`app.kria.narration_notes`). The planner records
    # the decision on the approved length plan (`spoken_order`); the phone worker
    # reorders only when that record is there. False: byte-identical to before -- no
    # seats, no record, no reorder, no reply note. Already-approved plans carrying the
    # record keep their order unless this is off when the worker runs. Rollback:
    # `fly secrets set SPOKEN_ORDER_ENABLED=false --app nova-video` +
    # `fly machine restart <id>` (api + worker).
    spoken_order_enabled: bool = True

    # Kill switch for the narrated walkthrough archetype. When False, a job
    # whose plan declares edit_format="narrated" follows the existing voiceover
    # or montage path. When True, eligible voiceover + filming-guide jobs align
    # each shot's script to the recorded narration and render one cut per step,
    # burn the transcribed narration as synced captions, and reflow short clips
    # to fill their step so the visuals never end before the voice.
    # Flipped on with the narrated captions + no-freeze work (PR1). The flag
    # stays the rollback lever: set False to revert to the voiceover-montage path.
    narrated_archetype_enabled: bool = True
    # Transcript-grounded visual matching and editable narrative overlays for
    # recorded voiceover edits. Off preserves the legacy upload-order path.
    narrated_storyboard_enabled: bool = False
    # KRI-456 kill switch. Phone narrated edits with no filming guide let the
    # `nova.compose.narrated_clip_alignment` agent choose the voiceover word at
    # which each clip comes on screen, so a clip plays when the voice describes
    # it. Legacy unbound jobs retain their bucket fallback. Bound requests
    # require an explicit choice before losing alignment. Keep the existing
    # KRI-456 rollout default; request-binding writers are gated separately.
    narrated_clip_alignment_enabled: bool = True

    # KRI-889: cut the lines a creator named ("cut the line 'with bread, no'") out of a
    # narrated edit's recorded voiceover, phone and cloud, and promise it on the draft.
    # False => byte-identical to before: the voiceover plays whole and the receipt says
    # the line can't be cut yet. Read by the worker AND brief_checks, so promise and
    # render agree. Apply: fly secret + restart api + worker.
    narrated_named_cuts_enabled: bool = True

    # Kill switch for the terminal-job stuck-variant watchdog
    # (`reconcile_stuck_variants`). This sweep WRITES user-visible state — it
    # rewrites assembly_plan and can move a job to variants_ready_partial — and
    # it was dead in production for months behind a malformed jsonpath that
    # `worker_ready` swallowed as non-fatal. Set False + restart the worker to
    # halt it without a code revert.
    reconcile_stuck_variants_enabled: bool = True

    # Kill switch for the subtitled single-clip talking-head archetype. When False,
    # a job whose plan declares edit_format="subtitled" falls back to montage (so an
    # unimplemented/rolled-back token never renders half a feature). When True, a
    # single talk-to-camera clip keeps its own audio, is transcribed (whisper-1 +
    # language hint, Turkish + English), and renders editable sentence-block captions
    # over a cached caption-free base. Default OFF until the render path + picker card
    # are wired and verified end-to-end; the frontend picker card gates on this too.
    subtitled_archetype_enabled: bool = False

    # Creator-style Smart Captions master gate.  This controls availability only;
    # per-video intent is persisted separately and creator preset assignment is
    # resolved server-side.  Availability also requires the subtitled base
    # renderer gate. Default OFF for controlled creator rollout.
    smart_captions_enabled: bool = False

    # Fleet-wide default Smart Captions preset for users WITHOUT a
    # creator_style_assignments row. New unassigned creators default to the
    # reviewed v3 style once both master gates are enabled. An existing row
    # always wins, including explicit v1/v2 pins and enabled=false opt-outs.
    # Rollback: set both secrets to empty + restart; assigned creators remain
    # pinned to their stored versions.
    smart_captions_default_preset_id: str = "cigdem"
    smart_captions_default_preset_version: str = "v3"

    # The word→visual matching brain (nova.compose.scene_matcher): one Gemini
    # call per v2 render pairing pool assets with the exact spoken word and
    # tagging chapters/roles language-agnostically. Fail-open advisory — any
    # failure falls back to the deterministic vocab heuristics. Kill switch:
    # false = pre-agent behavior byte-identically.
    smart_scene_matcher_enabled: bool = True

    # Smarter captions (plans 011 + 012). Four flags: the two plan-011 kill
    # switches (emphasis, layout) default OFF so a merge is a no-op until each is
    # flipped Fly-first; the two plan-012 behavior flags (section heading,
    # transcript cache) default ON — they refine pre-existing behavior and act as
    # rollback levers rather than opt-in gates.
    #
    # EMPHASIS: gates the scene-matcher emphasis prompt block AND consumption of
    # emphasis_spans in the caption chunker — contextual words-per-cue ("number
    # one → Messi" shows Messi alone) plus emphasis-derived keep-together pairs.
    # Off ⇒ the model never sees the emphasis task and cue chunking is
    # byte-identical (the prompt block renders to ""); it is a real rollback, not
    # a consumption veto. Requires smart_scene_matcher_enabled to do anything.
    smart_caption_emphasis_cues_enabled: bool = False
    # LAYOUT: gates the deterministic line-layout additions in measure_caption —
    # the digit+word keep-together adjacency rule and the single-word widow
    # penalty. Fully deterministic (no LLM), so it is verifiable offline and can
    # ship ahead of the emphasis eval train. Off ⇒ line wrapping byte-identical
    # for cues with no persisted emphasis keep-together pairs; a job planned
    # while the emphasis flag was on keeps honoring its persisted pairs on reburn
    # regardless of THIS flag (that is the emphasis dimension, not the layout one).
    smart_caption_layout_balance_enabled: bool = False
    # SECTION HEADING: the "number N" + keyword overlay lane the scene matcher
    # emits for list items (plan 012 P1-3). Default ON (pre-existing behavior);
    # rollback lever if the section overlay crowds the caption band. Off ⇒ the
    # compiler emits no section number/keyword text elements; captions unaffected.
    smart_caption_section_heading_enabled: bool = True
    # TRANSCRIPT CACHE: content-addressed cache of the whisper transcript keyed by
    # clip content hash (plan 012 P1-4). whisper-1 is non-deterministic, so this
    # makes every re-render of the SAME clip reuse the identical word list —
    # killing the "two renders of one clip caption differently" symptom. Fully
    # fail-open (any GCS/hash error falls through to a live transcribe). Default
    # ON; set false to always re-transcribe.
    smart_caption_transcript_cache_enabled: bool = True
    # FACE PLACEMENT: computes ONE static caption y_frac per video from sampled
    # face boxes (plan 011 Feature C) so captions never sit on the speaker's face,
    # then persists it so reburns/edits stay stable. Default OFF ⇒ current geometry,
    # anchor set, and receipts byte-identical (render-only, no NEXT_PUBLIC twin —
    # the position UI keeps working through the mirrored caption_margin_v). First
    # render only; reburns/re-transcribes read the persisted policy and never
    # recompute. Requires smart_scene_matcher_enabled (the master kill switch) to
    # do anything, but is independent of the emphasis/layout flags.
    smart_caption_face_placement_enabled: bool = False

    # GUIDED-STORY FACE PLACEMENT (KRI-116): same face-sampling primitives as
    # smart_caption_face_placement_enabled above, applied to the guided-story
    # opening title / chapter lines / closing card instead of captions. Resolved
    # once at render time against the composited base video and persisted onto
    # each TextElement's y_frac, so every renderer (Skia burn, phone/native-editor
    # recipe) reads the same chosen position — see
    # render_geometry.choose_guided_text_y_frac. Default OFF ⇒ byte-identical to
    # today's fixed y_frac placement.
    guided_text_face_placement_enabled: bool = False
    # KRI-140: the same decision for a PHONE-rendered guided story. There is no
    # assembled video on the server, so `_run_phone_guided_job` samples faces
    # from each footage moment's analysis proxy (mapped through the moment's
    # crop + the engine's cover-fit) and bakes the chosen `y_frac` into the
    # variant's text rows before the recipe compiles
    # (`app.pipeline.phone_guided_text_placement`). Independent of the cloud
    # flag above; server-only (no app build needed). False (default) leaves the
    # authored position: byte-identical. Read at plan time, so a flip affects
    # the next phone render. Apply: `fly secrets set
    # PHONE_GUIDED_TEXT_FACE_PLACEMENT_ENABLED=true --app nova-video` + `fly
    # machine restart <id>` (worker).
    phone_guided_text_face_placement_enabled: bool = False

    # Kill switch for authored TextElements on subtitled variants. When False,
    # subtitled remains captions-only and the text-element routes/capabilities
    # reject it. When True, user-authored text is burned onto the caption-free
    # base before captions, so captions stay topmost.
    subtitled_text_lane_enabled: bool = False

    # Text Motion v2 evaluator + Smooth Type renderer. Persisted configs are
    # retained while disabled; Smooth Type renders as settled static text.
    text_motion_v2_enabled: bool = False

    # Subtitled caption correction: after whisper, an LLM fixes each cue's spelling /
    # grammar / case-endings (whisper mishears Turkish morphology) while preserving cue
    # timing. Best-effort — a failure leaves cues untouched. Kill switch: set False to
    # burn the raw whisper transcript. gpt-4o is the default: gpt-4o-mini proved
    # UNRELIABLE on Turkish contextual grammar (missed 'nereye'->'nereyi' 4/4 runs; gpt-4o
    # fixed it 2/2). ~$0.005/render. Set to gpt-4o-mini to trade accuracy for cost.
    subtitled_caption_correction_enabled: bool = True
    caption_correction_model: str = "gpt-4o"

    # Caption translation (KRI-838): a whisper language hint does not reliably translate (a
    # Turkish hint on English speech wrote the English again in local tests), so when the
    # creator asks for captions in another language and the hinted text is not in it, one
    # structured model call translates the caption lines (app/pipeline/caption_translate.py).
    # Fail-open: any failure keeps the spoken-language words and the variant is stamped
    # with the language the text really is. Kill switch: set False and restart api +
    # worker (the pre-approval promise reads the same flag). Empty model = the caption
    # correction model above. ~$0.005/render, only on renders that ask for a language.
    caption_translation_enabled: bool = True
    caption_translation_model: str = ""

    # Caption punctuation restoration: whisper-1's word-level timestamps strip
    # punctuation/capitalization (it lives only in the full-text transcript).
    # align_punctuated_text() in transcribe.py restores it onto the timed word
    # stream at the end of _transcribe_openai (fail-open — any residual
    # alignment mismatch bails for the WHOLE transcript and returns the
    # original unpunctuated words; never ships a half-aligned transcript).
    # _transcribe_local (faster-whisper) already emits punctuation attached to
    # words, so this flag only affects the openai-api backend.
    # Kill switch: `fly secrets set CAPTION_PUNCTUATION_ENABLED=false --app nova-video`
    # + worker restart — byte-identical to pre-feature behavior.
    caption_punctuation_enabled: bool = True

    # Self-narration for narrated walkthroughs: a NARRATED_EDIT_FORMATS item with NO
    # recorded voiceover may still generate when its footage carries the voice — the
    # clip audio IS the narration. Resolution: 1 clip with speech → subtitled (editable
    # captions from its own audio); 2+ clips → talking_head (highest-speech spine +
    # B-roll); no speech anywhere → montage fallback, reason persisted on
    # assembly_plan["archetype_fallback"] and surfaced on the item page. This flag is
    # the SOLE gate for the branch — it deliberately does NOT consult
    # edit_format_talking_head_enabled / subtitled_archetype_enabled (those gate
    # plan-DECLARED formats, not resolution outcomes) so rollback is one switch.
    # INCIDENT NOTE: if either assembler's own kill switch is flipped off because
    # that render path is broken, ALSO flip this flag off — self-narration would
    # otherwise keep routing narrated items into the disabled assembler.
    # Mirror flag NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED gates the frontend
    # Generate button; flip Fly first, then Vercel. Default OFF.
    narrated_self_narration_enabled: bool = False

    silence_cut_enabled: bool = Field(
        default=False,
        description="Automatic silence/filler cutting for speech render paths "
        "(plans/010): subtitled today, talking_head when T6 lands. When true, the "
        "clip's own audio is transcribed verbatim (whisper bias prompt), silences "
        "are detected, and dead air + filler vocalizations are cut inside the "
        "reframe (alternating punch-in jump cuts); captions are built from the "
        "remapped transcript minus filler tokens. Historical legacy_auto jobs "
        "fail open to uncut timing; required_v1 jobs surface typed analysis/apply/"
        "unsafe-plan failures; off_v1 skips cleanup entirely. Per-item "
        "opt-out: POST /admin/jobs/{job_id}/silence-cut-disable (takes effect on "
        "the next FULL re-render only). "
        "Kill switch: `fly secrets set SILENCE_CUT_ENABLED=false --app nova-video` "
        "+ worker restart — byte-identical to pre-feature behavior.",
    )

    speech_cleanup_budget_clamp_enabled: bool = Field(
        default=True,
        description="Explicit-consent removal clamp for required_v1 speech cleanup "
        "(plans/019 addendum). When a creator turns Speech cleanup ON and the "
        "proposed removal exceeds the auto-path safety rail, the plan is clamped "
        "to MAX_REMOVAL_FRAC_REQUIRED (largest removals first, MIN_OUTPUT_S "
        "floor) instead of hard-failing the render with unsafe_plan — filler-"
        "heavy clips are the feature's target and must produce a cleaned video. "
        "legacy_auto/off_v1 paths are untouched. Emergency rollback ONLY (restores "
        "the deterministic unsafe_plan render failure for over-budget clips): "
        "`fly secrets set SPEECH_CLEANUP_BUDGET_CLAMP_ENABLED=false --app "
        "nova-video` + worker restart.",
    )

    speech_cleanup_max_removal_frac_required: float = Field(
        default=1.0,
        # gt, not ge: SpeechCleanupAnalysisInput rejects 0, so accepting it here
        # would boot cleanly and then fail every analysis with a user-visible
        # error. "Turn cleanup off" is SILENCE_CUT_ENABLED, not this knob.
        gt=0.0,
        le=1.0,
        description="Fraction-of-runtime cap on explicit-consent (required_v1) speech "
        "cleanup. 1.0 (default, 2026-09-08) means no fraction cap: the clamp budget is "
        "bound only by the MIN_OUTPUT_S floor, so every filler and pause the detector "
        "found is removed. The previous 0.55 rail declined cuts on exactly the clips "
        "the feature exists for — a 10s clip needing ~65% removal shipped with a second "
        "of dead air left in. Does NOT affect the auto/legacy path, which keeps its own "
        "MAX_REMOVAL_FRAC bailout rail. The value is part of the source policy "
        "fingerprint, so flipping it re-analyzes affected items. Rollback: "
        "`fly secrets set SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED=0.55 --app "
        "nova-video` + api and worker restart (the fingerprint is computed in "
        "the API process, the plan in the worker).",
    )

    guided_voiceover_speech_cleanup_enabled: bool = Field(
        default=False,
        description="Clean up speech for guided narrated stories. When a creator chooses "
        "'Clean up speech' on a guided voiceover, the draft task applies the confirmed "
        "preflight CutPlan to the pinned voiceover once, stores the cleaned WAV under "
        "users/{owner}/plan/{item}/speech-cleanup/, and plans, captions and renders "
        "against that derivative (NarrationTrack.speech_cleanup records its provenance). "
        "Gates the writer only: readers always accept the provenance field so mixed "
        "deploys stay safe. Also requires SILENCE_CUT_ENABLED. Deploy with it off and "
        "enable only once every api and worker machine runs a build that reads the "
        "provenance field. Off: a clean choice fails closed with the non-retryable "
        "speech_cleanup_disabled failure and never renders the uncut voiceover. "
        "`fly secrets set GUIDED_VOICEOVER_SPEECH_CLEANUP_ENABLED=true --app nova-video` "
        "+ machine restart. Once on, never roll the image back past this change: "
        "older builds reject the provenance field on proposals already written, and "
        "turning the flag off does not repair those rows.",
    )

    speech_cleanup_filler_check_enabled: bool = Field(
        default=True,
        description="Speech-cleanup filler check (KRI-970 follow-up). whisper-1 can stamp a whole "
        "stretch of words late (Merope café: 0.3-0.5 s), so a filler cut lands on the next words "
        "and the real filler stays. On: the preflight analysis re-times English takes that have "
        "lexicon fillers with a forced aligner (facebook/wav2vec2-base-960h, baked into the image "
        "at speech_cleanup_aligner_model_dir) before planning, then re-listens around every filler "
        "cut with the cleanup transcriber (two short whisper-1 calls per cut, at most "
        "RELISTEN_MAX_CUTS) and drops a cut that takes a real word with it. Missing model or a "
        "transcription error keeps today's cut. Part of the source policy fingerprint, so a flip "
        "re-analyzes. Kill switch: `fly secrets set SPEECH_CLEANUP_FILLER_CHECK_ENABLED=false "
        "--app nova-video` + api and worker restart (the fingerprint is computed in the API).",
    )

    speech_cleanup_aligner_model_dir: str = Field(
        default="/opt/models/wav2vec2-base-960h",
        description="Directory holding the pinned forced-aligner model (config.json, vocab.json, "
        "model.safetensors). The production Dockerfile downloads it there; without it the filler "
        "check keeps whisper's stamps and only re-listens.",
    )

    speech_cleanup_mixed_gap_mode: Literal["off", "shadow", "apply"] = Field(
        default="off",
        description="Required-v1 mixed-gap filler detector rollout. off preserves the "
        "legacy detector byte-for-byte; shadow evaluates and records V2 while rendering "
        "the baseline; apply may render a validated V2 candidate for sources inside the "
        "stable rollout bucket. legacy_auto and off_v1 always ignore this setting.",
    )

    speech_cleanup_mixed_gap_rollout_percent: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Stable source-instance percentage eligible for mixed-gap apply mode. "
        "Missing or invalid source identity downgrades apply to shadow.",
    )

    retake_cut_enabled: bool = Field(
        default=False,
        description="Retake/restart cutting inside the silence-cut stage "
        "(plans/010): the retake_detector agent flags abandoned takes and their "
        "spans merge into the same CutPlan. Independent of SILENCE_CUT_ENABLED "
        "(silence cutting ships and validates first); only meaningful when that "
        "flag is also on. Detector failure degrades to zero retake cuts "
        "(`retake_detector_failed` event) — never blocks silence/filler cutting. "
        "Kill switch: `fly secrets set RETAKE_CUT_ENABLED=false --app nova-video` "
        "+ worker restart.",
    )
    speech_cleanup_retakes_enabled: bool = Field(
        default=False,
        description="Explicit-consent preflight retake detection. This remains off until every "
        "client can present retake-aware consent; effective only with RETAKE_CUT_ENABLED. "
        "Kill switch: `fly secrets set SPEECH_CLEANUP_RETAKES_ENABLED=false --app "
        "nova-video` + worker restart.",
    )

    # "Get a transcript" helper for narrated-walkthrough voiceovers. When False,
    # the transcript routes (POST/GET /plan-items/{id}/transcript/*) return 404 and
    # the frontend entry link is hidden (mirror flag NEXT_PUBLIC_TRANSCRIPT_HELPER_ENABLED
    # in Vercel — keep Fly + Vercel in sync). Default OFF until the flow ships behind
    # the flag. Gates only the optional helper; the record/upload voiceover bar is
    # unaffected. See plans/on-the-narrated-walkthrough-*.md.
    transcript_helper_enabled: bool = False

    # Layer-2 text-overlay extraction pipeline. When False, the existing
    # single-call `nova.compose.template_text` Gemini agent runs unchanged.
    # When True, the OCR + grouping + transcript-alignment pipeline replaces
    # the agent's body (same input/output schema, different implementation).
    # Default OFF until PR 2 lands the pipeline behind the flag and PR 3
    # validates output quality on staging templates. See
    # plans/template-text-overlay-layer-2-architecture.md.
    text_overlay_v2_enabled: bool = False

    # Auto-music mode (Phase 3 of the auto-music feature). When False, the
    # `orchestrate_auto_music_job` Celery task and the (yet-to-land Phase 4)
    # POST /auto-music-jobs route are unreachable from user input — the
    # orchestrator early-exits with `processing_failed` if it's somehow
    # invoked. Existing template-mode + manual music-mode flows are
    # unaffected either way. Flip to True when the music library has
    # enough labeled tracks for the matcher to do real work (~15+).
    enable_auto_music_mode: bool = False

    # Skia text renderer for agentic templates + music lyrics. When True
    # (default), agentic-template and music-job text overlays render via
    # `app.pipeline.text_overlay_skia` instead of Pillow + libass. Classic
    # non-music templates are unaffected either way. Flip to False on Fly
    # (`fly secrets set TEXT_RENDERER_SKIA_ENABLED=false --app nova-video`
    # then restart workers) to revert agentic + music jobs to the Pillow +
    # libass path instantly. The kill switch is read per render call —
    # no in-flight job is mid-rendered with a switched-on flag.
    text_renderer_skia_enabled: bool = True

    # HDR→SDR tonemap highlight-desaturation knee (`_ZSCALE_SDR_PIPELINE` in
    # reframe.py). FFmpeg's `tonemap` filter defaults `desat=2`, a soft-knee
    # that pushes bright+saturated pixels toward white to tame super-highlights.
    # A sunlit blue sky is bright AND saturated, so every HLG/HDR10 clip with a
    # sky in frame got its sky bleached toward white/gray — measured on a real
    # job (2cfb57f1, clip IMG_5169.MOV): sky blue chroma (U) dropped from 139.5
    # (source) to 130.9 (tonemapped) while luma stayed ~204, the signature of
    # luma-gated desaturation, not a grade or a gamut clamp. `desat=0` disables
    # the knee. `2.0` restores the pre-fix FFmpeg-default behavior byte-
    # identically — the rollback value if a future clip shows colored fringing
    # on specular highlights (the risk `desat` exists to prevent). Apply:
    # `fly secrets set HDR_TONEMAP_DESAT=2 --app nova-video` + restart api +
    # worker — `_ZSCALE_SDR_PIPELINE` bakes this into a module-level filter
    # string at worker-process import time, not per render call, so a
    # restart is required for a changed value to take effect. Bounded to
    # FFmpeg's documented `tonemap` desat range so a malformed env value
    # (e.g. a typo'd negative number or "nan") fails loudly at Settings load
    # instead of silently corrupting every HDR render's filtergraph.
    hdr_tonemap_desat: float = Field(default=0.0, ge=0.0, le=20.0)

    # Heavy-source downscale guard (2026-07-21 OOM incident, job e8173a25):
    # a 170MB high-bitrate clip OOM-killed the worker mid-reframe. SDR sources
    # whose SHORT edge exceeds `source_downscale_short_edge_max` are re-encoded
    # once at ingest (bounded decoder threads, h264, never upscaled past the
    # 1080x1920 cover scale) so every downstream per-slot reframe decodes a
    # bounded intermediate instead of native 4K HEVC. HDR sources are excluded —
    # the existing `_pretonemap_hdr_clips` pass already downscales those inside
    # its zscale chain. Kill switch: SOURCE_DOWNSCALE_GUARD_ENABLED=false +
    # worker restart → byte-identical to pre-guard behavior.
    source_downscale_guard_enabled: bool = True
    source_downscale_short_edge_max: int = 1920
    # Decoder/encoder thread cap for the guard's own ffmpeg pass. Frame-threaded
    # HEVC decode memory scales with thread count — the whole point of the guard
    # is bounding peak RSS, so its own decode must not spike either.
    source_downscale_ffmpeg_threads: int = 2

    # Celery prefork child recycling (same incident): analyze_pool_asset bursts
    # leave CLIP/torch/Whisper residency in the single long-lived child, which
    # then stacks under the next render's ffmpeg peak. When a child's RSS
    # exceeds this many KB after a task completes, Celery replaces it (fork from
    # the parent keeps the prewarmed CLIP singleton via copy-on-write, so the
    # replacement is cheap). 0 disables. Measured in KB (Celery convention).
    worker_max_memory_per_child_kb: int = 3_145_728  # 3GB

    # Deploy dead time (KRI-294): end Celery's 240s soft-shutdown wait as soon
    # as the tasks in flight at SIGTERM finish, and skip it when idle
    # (app/services/celery_soft_shutdown.py). False restores the stock
    # full-window wait on every shutdown. Read on the OLD machine at stop time,
    # so a flip takes effect from the deploy after it.
    celery_soft_shutdown_early_exit_enabled: bool = True

    # Render heartbeat (same incident, user-visible half): the orchestrator
    # ticks jobs.worker_heartbeat_at every `interval` seconds; the status route
    # reports `retrying: true` while a non-terminal job's heartbeat is older
    # than `stale_after` (worker died silently; acks_late redelivery pending).
    # stale_after = 5 missed beats — tolerates one slow/failed DB write without
    # flapping the UI.
    render_heartbeat_interval_s: int = 30
    render_heartbeat_stale_after_s: int = 150

    # Dynamic crossfade scheduling for the line-style lyric overlay path
    # (`app.pipeline.lyric_injector._inject_line`). When True (default), the
    # scheduler matches per-pair crossfade durations, anchors actual emitted
    # overlap to the matched window, and tags the outgoing overlay with
    # `fade_out_curve="sqrt"` so both renderers use mirror-symmetric fade
    # curves during inter-line crossfades — α_L_N + α_L_N+1 = 1 at every t,
    # so no two consecutive lyric overlays render readably simultaneously.
    # When False, the scheduler reproduces pre-fix behavior byte-identically:
    # legacy `dynamic_max_overlap = min(max_overlap_s, fade_in_s + fade_out_s)`
    # cap, no `fade_out_curve` key, no dynamic duration matching. Flip to
    # False on Fly (`fly secrets set LYRIC_DYNAMIC_CROSSFADE_ENABLED=false
    # --app nova-video` then restart workers) to roll back music + agentic
    # lyric scheduling instantly. The kill switch is read inside
    # `inject_lyric_overlays`, so every job picks up the current value at
    # scheduling time.
    lyric_dynamic_crossfade_enabled: bool = True

    # Text-behind-subject occlusion. When True, generative-edit overlays the
    # OverlayFormatMatcherAgent (or a user-authored TextElement) flags with
    # `behind_subject: True` are burned with a per-frame person-segmentation
    # matte (`app.pipeline.subject_matte`) so a moving subject occludes the
    # text instead of sitting on top of it. Default OFF: no matte is ever
    # computed, no burn dict carries `behind_subject`, and no extra GCS
    # object is written — `_resolve_intro_overlay_params` (generative_build.py)
    # is the single chokepoint that ANDs the resolved decision with this flag,
    # so flipping it off mid-flight degrades every in-flight job to plain text
    # instead of failing it. Flip to False on Fly
    # (`fly secrets set TEXT_BEHIND_SUBJECT_ENABLED=false --app nova-video`
    # then restart workers) to disable instantly.
    text_behind_subject_enabled: bool = False

    # Subject-matte segmentation backbone selector. When True (default), the
    # matte engine uses RobustVideoMatting (onnxruntime CPU, recurrent —
    # temporally stable; assets/models/rvm_mobilenetv3_fp32.onnx). When False
    # or when onnxruntime / the model file is unavailable, it falls back to
    # the MediaPipe selfie-segmenter path automatically (the pre-RVM
    # behavior). Kill switch for the backbone only — TEXT_BEHIND_SUBJECT_ENABLED
    # above still gates the whole effect. Flip on Fly
    # (`fly secrets set MATTE_RVM_ENABLED=false --app nova-video` then
    # restart workers); read lazily per matte compute, no deploy needed.
    matte_rvm_enabled: bool = True

    # Depth-based scene-occluder backbone (non-person subjects — landmarks,
    # objects, scenery with no person in frame). When True, the subject-matte
    # engine (`app.pipeline.subject_matte`) attempts a second pass with a
    # monocular depth model (Depth Anything V2 small, onnxruntime CPU) after
    # the person pass finds nothing, so "text behind subject" can occlude
    # against a non-person foreground (e.g. a hill/landmark against sky).
    # Default OFF (dark-ship): no depth inference ever runs, no extra GCS
    # scratch object is written, output is byte-identical to before this
    # flag existed. Flip on Fly (`fly secrets set
    # MATTE_DEPTH_OCCLUDER_ENABLED=true --app nova-video` then restart
    # workers); read lazily per matte compute (`_depth_occluder_enabled()`),
    # no deploy needed. Rollback = flip back to False + restart.
    matte_depth_occluder_enabled: bool = False

    # Linear LRCLIB re-anchor for synced lyrics. When True (default), the
    # alignment layer can fit a small per-time drift curve before falling
    # back to the existing uniform median / single-L0 paths. This catches
    # official-video cuts whose vocals diverge progressively from the album
    # recording indexed by LRCLIB. When False, the linear path is skipped and
    # the old uniform-only behavior is preserved.
    lyric_linear_reanchor_enabled: bool = True

    # Post-snap re-anchor for karaoke + per-word-pop lyric overlays
    # (`app.pipeline.lyric_word_resync`). When True (default), each music-job
    # render rewrites karaoke/popup overlay `start_s`/`end_s` so the per-word
    # highlight (karaoke) and per-stage arrival (pop-in) stay glued to the
    # vocal even after beat-snap shifts the slot's cumulative position. When
    # False, falls back to pre-fix behavior byte-identically: overlays render
    # at their pre-snap slot-relative positions and may drift up to one
    # beat-interval (~250 ms on a 2.4 BPS track) against the audio.
    # Flip to False on Fly
    # (`fly secrets set LYRIC_WORD_RESYNC_ENABLED=false --app nova-video`
    # then restart workers) to roll back instantly if the re-anchor pass
    # itself ships a regression. The flag is read inside
    # `_collect_absolute_overlays` so every job picks up the current value
    # at render-collect time. Line-style overlays NEVER participate in this
    # pass — they don't carry the `section_anchor_s` stamp.
    lyric_word_resync_enabled: bool = True

    # Synced-anchor health check for the lyrics agent
    # (`app.agents.lyrics.LyricsExtractionAgent.compute`). When True (default),
    # if `align_with_line_anchors` returns confidence < 0.20, the agent
    # treats the LRCLIB syncedLyrics as being from a different recording of
    # the same song and falls back to plain_lyrics+whisper (or whisper_only)
    # so Whisper's timestamps drive the line bounds. Pairs with the
    # `duration` query param on `search_lrclib` as a two-layer defense
    # against the "Hawai" version-mismatch bug (synced anchors at 4.59s,
    # actual audio at 22s). Flip to False on Fly
    # (`fly secrets set LYRIC_SYNCED_ANCHOR_FALLBACK_ENABLED=false
    # --app nova-video` then restart workers) for emergency rollback if the
    # threshold turns out to be too aggressive in prod and starts demoting
    # legitimate synced extractions to plain+whisper. Read once per
    # extraction inside `compute()`. Existing cached extractions are not
    # touched by either value.
    lyric_synced_anchor_fallback_enabled: bool = True

    # Universal text-overlay constraint pass. When True (default), every
    # overlay collected by `_collect_absolute_overlays` (agentic templates +
    # music lyrics + generative edits — NOT classic templates, which never
    # carry the canonical overlay dict through this path) is run through
    # `app.pipeline.overlay_constraints.apply_overlay_constraints`: text is
    # shrunk/wrapped to fit a max line count and repositioned into the 9:16
    # safe zone. The pass only rewrites `text_size_px` / `position_*_frac`
    # (fields both renderers already honor), so it is renderer-parity-safe and
    # can only improve overflowing text. Flip to False to disable the guarantee
    # (e.g. to isolate a layout regression) without redeploying.
    style_constraints_enabled: bool = True

    GENERATIVE_FAST_REBURN_ENABLED: bool = Field(
        default=True,
        description="Enable fast reburn path for generative variant edits (font/text/size). "
        "Set to false to fall back to full re-render on every edit (slower but safe). "
        "Layer-1 text persistence stays active regardless.",
    )

    # Transcript-synced editorial typographic sequence for generative edits
    # (the "Editorial" layout auto-upgrade, D6/D16). When True (default), an
    # agent_text variant whose intro layout resolves to "cluster" AND whose
    # final audio keeps the montage's original speech audible is transcribed
    # (Whisper, pre-mix `assembled_path` — D11) and rendered as phrase-by-phrase
    # styled scenes (`app/pipeline/phrase_sequence.py` + intro_cluster
    # EDITORIAL_STYLE); ineligible/failed variants fall back to a STYLED static
    # cluster. When False, behavior is byte-identical legacy: no transcription,
    # no styled cluster (compute_cluster_blocks gets style=None), no sequence
    # overlays. Read at render time inside the burn step, so flipping it
    # affects queued jobs and re-renders after a worker restart. Kill switch:
    # `fly secrets set EDITORIAL_SEQUENCE_ENABLED=false --app nova-video`
    # + `fly machine restart <id>` — no deploy needed.
    editorial_sequence_enabled: bool = True

    GENERATIVE_CLUSTER_INTRO_ENABLED: bool = Field(
        default=True,
        description="Allow the editorial word-cluster intro layout for generative edits "
        "(overlay_format_matcher layout='cluster' → app/pipeline/intro_cluster.py). "
        "Set to false to force every intro to the linear layout (kill switch: "
        "`fly secrets set GENERATIVE_CLUSTER_INTRO_ENABLED=false --app nova-video` "
        "+ worker restart). Persisted cluster variants re-render as linear too.",
    )

    NARRATIVE_CLIP_ORDER_ENABLED: bool = Field(
        default=True,
        description="Order plan-item edits by the filming guide's shot sequence "
        "(narrative mode in template_matcher.match). Read at render time, so "
        "flipping it affects queued jobs and re-renders after a worker restart. "
        "Set to false to fall back to pure greedy clip-to-slot matching.",
    )

    GENERATIVE_TIMELINE_EDITOR_ENABLED: bool = Field(
        default=True,
        description="Enable the post-generation clip timeline editor: durable "
        "per-job source copies, ai_timeline persistence on each montage variant, "
        "and the user_timeline override path on re-renders. Read at render time, "
        "so flipping it affects queued jobs and re-renders after a worker "
        "restart. Set to false to fall back to fresh clip matching on every "
        "re-render (timelines neither written nor honored). Kill switch: "
        "`fly secrets set GENERATIVE_TIMELINE_EDITOR_ENABLED=false --app "
        "nova-video` + `fly machine restart <id>` — no deploy needed.",
    )

    edit_wide_looks_enabled: bool = Field(
        default=False,
        description="Advertise Golden Hour and Faded Analog as edit-wide look "
        "controls after compatible API, worker, and web code is deployed. The "
        "renderer continues honoring already-persisted values when this is false. "
        "Activate with `fly secrets set EDIT_WIDE_LOOKS_ENABLED=true --app "
        "nova-video` + API restart; set false to hide new writes without breaking "
        "existing edits.",
    )

    GENERATIVE_PARALLEL_VARIANTS_ENABLED: bool = Field(
        default=False,
        description="Render a generative job's variants concurrently (bounded by "
        "GENERATIVE_PARALLEL_VARIANTS_MAX) instead of one-at-a-time. ONLY safe on "
        "a dedicated-CPU worker — on shared CPUs concurrent FFmpeg encodes starve "
        "each other (the concurrency=2 incident, job d018d1c3, ballooned a 14s "
        "encode to >600s). Default off; flip on after the worker is on a "
        "performance VM. Kill switch: "
        "`fly secrets set GENERATIVE_PARALLEL_VARIANTS_ENABLED=false` + restart.",
    )

    GENERATIVE_PARALLEL_VARIANTS_MAX: int = Field(
        default=2,
        description="Max variants rendered concurrently when "
        "GENERATIVE_PARALLEL_VARIANTS_ENABLED is on. Keep ≤ the worker's "
        "dedicated vCPU count so each concurrent FFmpeg encode gets a real core.",
    )

    PLAN_SYNC_DISPATCH_ENABLED: bool = Field(
        default=True,
        description="POST /plan-items/{id}/generate mints the render Job "
        "synchronously in-request (plans/014) so the item flips to "
        "'generating' immediately, instead of waiting for the single-slot "
        "render worker to run the dispatch task (observed: ~8 min of frozen "
        "'Starting…' behind an in-flight render, 2026-08-04). Also makes a "
        "duplicate Generate an idempotent 200 (FOR-UPDATE re-check in "
        "dispatch_item_render_for). false → legacy async dispatch via "
        "generate_plan_item_videos.delay() with the 409 double-generate "
        "guard — byte-identical route contract to pre-014. Apply: "
        "`fly secrets set PLAN_SYNC_DISPATCH_ENABLED=false --app nova-video` "
        "+ `fly machine restart <id>` (api).",
    )

    RENDER_AUTOSTOP_ENABLED: bool = Field(
        default=False,
        description="Stop the `worker` Fly machine when render_worker_idle() "
        "(app/services/queue_state.py) reports idle for RENDER_IDLE_GRACE_MIN, "
        "and start it back up on a wake signal (before_task_publish signal in "
        "worker.py) or the periodic backstop in tasks.manage_render_worker_lifecycle "
        "(app/tasks/maintenance.py). Default off: byte-identical to today "
        "(worker stays always-on, fly.toml auto_stop_machines=false). Requires "
        "FLY_API_TOKEN + FLY_APP_NAME to be set — the lifecycle task and the "
        "wake hook both no-op (log + skip) if either is missing, never raise. "
        "Kill switch: `fly secrets set RENDER_AUTOSTOP_ENABLED=false` + restart "
        "(restores always-on immediately; does not require `fly machine start` "
        "since a stopped machine's next real job wakes it via the backstop "
        "within RENDER_LIFECYCLE_POLL_INTERVAL_S regardless of this flag, but "
        "flipping the flag off stops any FURTHER autostop decisions).",
    )

    RENDER_IDLE_GRACE_MIN: int = Field(
        default=10,
        description="Minutes render_worker_idle() must report idle=True, "
        "continuously, before tasks.manage_render_worker_lifecycle stops the "
        "worker machine. Absorbs brief gaps between back-to-back jobs (e.g. a "
        "user submitting a render, then immediately swapping the song) without "
        "flapping the machine stopped/started. Only consulted when "
        "RENDER_AUTOSTOP_ENABLED is true.",
    )

    RENDER_WAKE_DEBOUNCE_S: int = Field(
        default=60,
        description="TTL (seconds) of the Redis debounce key the before_task_publish "
        "wake-hook signal (worker.py) sets after asking Fly to start the render "
        "machine. Prevents one Fly Machines API start call per dispatch during a "
        "burst of edits — at most one call per idle-to-active transition. Purely "
        "an optimization: if the debounce key is stale or Redis is briefly "
        "unavailable, worst case is one redundant (harmless) start call, not a "
        "missed wake — Fly's start-a-started-machine call is a no-op.",
    )

    RENDER_START_WAIT_S: int = Field(
        default=300,
        ge=0,
        le=600,
        description="Retry-window seconds for a render worker caught in Fly state stopping.",
    )

    RENDER_AUTOSTOP_ACTIVITY_GUARD_ENABLED: bool = Field(
        default=True,
        description="Defer idle autostop for recent render dispatches or active creator work.",
    )

    FLY_API_TOKEN: str = Field(
        default="",
        description="Fly.io API token used by app/services/fly_machines.py to "
        "start/stop the render worker machine when RENDER_AUTOSTOP_ENABLED is on. "
        "SCOPE NARROWLY: an app-scoped deploy token for nova-video only, never a "
        "full personal/org token — this credential is reachable from the `api` "
        "process (the wake hook fires from FastAPI request handlers), so if that "
        "internet-facing process is ever compromised through some unrelated "
        "vulnerability, the blast radius should be 'control of this one app's "
        "machines,' not 'full Fly org access.' Empty by default; the lifecycle "
        "task and wake hook both log a warning and no-op (never raise) if unset.",
    )

    FLY_APP_NAME: str = Field(
        default="nova-video",
        description="Fly app name app/services/fly_machines.py targets when "
        "resolving the render worker machine by metadata.fly_process_group.",
    )

    BEAT_HEARTBEAT_STALE_AFTER_MIN: int = Field(
        default=10,
        description="Minutes since the last successful Beat-scheduled task "
        "before GET /health/beat reports unhealthy. Pinged from OUTSIDE this "
        "app by .github/workflows/beat-health.yml (15-min cron) — the only "
        "mechanism that can detect Celery Beat being fully dead, since any "
        "check that is itself Beat-scheduled shares Beat's exact blind spot. Matters "
        "specifically because RENDER_AUTOSTOP_ENABLED makes Beat's health a "
        "prerequisite for the render worker ever restarting on a missed "
        "wake-hook call. 5x the fastest Beat interval (2 min, the render- "
        "worker lifecycle poll) so one or two missed ticks don't false-alarm.",
    )

    lyrics_optional_enabled: bool = Field(
        default=False,
        description="Lyrics become an optional capability of track-backed song_text: "
        "the generated intro stays, lyrics are not baked, and "
        "the variant stamps "
        "lyrics_baked=False + lyrics_enabled=False. The editor's Lyrics toggle "
        "(default OFF) then instantly materializes beat-synced lyric lines as "
        "ordinary editable `role=lyric_line` TextElements via GET "
        ".../lyric-seeds; saving burns them through the normal fast text "
        "reburn. Read by API capability discovery and at render time inside "
        "_render_generative_variant, so flipping it affects editor responses, "
        "queued jobs, and re-renders after an API and worker restart. "
        "Off = byte-identical current (bake-in) behavior; legacy "
        "variants (lyrics_baked absent) always keep baked behavior regardless "
        "of this flag. Kill switch: `fly secrets set "
        "LYRICS_OPTIONAL_ENABLED=false --app nova-video` + "
        "`fly machine restart <id>` for the API and worker — no deploy needed.",
    )

    guided_edit_capability_enabled: bool = Field(
        default=False,
        description="Expose the Plan edit draft/edit/approve API. This is the backend "
        "capability switch; enable only after the review UI and strict story renderer "
        "are deployed. Read by the API, so changing it requires an API restart.",
    )
    guided_edit_conversation_enabled: bool = Field(
        default=False,
        description="Allow the conversational Plan edit endpoint and advertise it to "
        "the web app. Enable only after every API and worker can parse the briefing "
        "proposal state. Read by the API; changing it requires an API restart.",
    )
    guided_edit_enforcement_enabled: bool = Field(
        default=False,
        description="Require a current approved guided-edit proposal before Generate. "
        "Independent from capability so rollout can deploy endpoints first. Read by "
        "the API and synchronous dispatch worker; changing it requires API and worker "
        "restarts. Startup also requires the code-owned strict-renderer readiness pin.",
    )
    guided_edit_direction_confirmation_enabled: bool = Field(
        default=False,
        description=(
            "Expose the optional planner's inferred-direction confirmation for compatibility. "
            "The primary automatic Generate path never waits for this confirmation. Read by "
            "the API and worker; changing it requires API and worker restarts."
        ),
    )
    edit_proposal_semantic_enabled: bool = Field(
        default=False,
        description=(
            "KRI-133: plan new edits with semantic intent and the deterministic frame scheduler. "
            "Enable only after every API/worker supports compiler v8. Disabling affects new "
            "drafts only; approved v8 schedules remain readable and renderable."
        ),
    )
    guided_auto_design_enabled: bool = Field(
        default=True,
        description=(
            "AI-designs-by-default: when the guided capability is available and the item already "
            "has media, silently reserve + draft + auto-approve a proposal (approval_mode='auto') "
            "and dispatch the render instead of blocking the creator on an explicit review step — "
            "one-click Generate always works when media exists. Kill switch: off restores strict "
            "enforcement (existing 409s) for new Generate calls — NOT byte-identical to "
            "pre-auto-design rollback, since the proposal_failed 409 mapping "
            "(services/edit_proposals.py) "
            "is unconditional regardless of this flag, and proposals an earlier auto-design "
            "attempt already approved stay approved (never retroactively un-approved) — see "
            "docs/runbooks/conversational-edit-rollback.md. Read by the API and sync dispatch "
            "worker; changing it requires API and worker restarts. Fly: `fly secrets set "
            "GUIDED_AUTO_DESIGN_ENABLED=false --app nova-video` + `fly machine restart <id>` "
            "for the api and worker process groups."
        ),
    )
    guided_render_recovery_enabled: bool = Field(
        default=True,
        description=(
            "Persist an actionable, version-scoped reason on the edit-proposal envelope "
            "when an APPROVED guided story fails to render, and refuse to re-dispatch the "
            "same approved version once it has failed non-transiently (or 3 times). Kill "
            "switch: off restores today's behavior exactly -- the job fails, nothing is "
            "persisted on the item, and Generate re-mints against the same pinned plan. "
            "Read by the worker and the sync dispatch path; changing it requires api + "
            "worker restarts. Apply: `fly secrets set GUIDED_RENDER_RECOVERY_ENABLED=false "
            "--app nova-video` + `fly machine restart <id>` for the api and worker process "
            "groups."
        ),
    )
    kria_guided_timeline_ops: bool = Field(
        default=False,
        description=(
            "KRI-219: let the Kria chat copilot reorder/trim/retime/split/remove clips and "
            "set transitions on guided-native (story) variants. Per-clip label bars follow "
            "their segment through the edit (services/kria_editor_timeline.py). Off keeps "
            "guided variants text-only in chat, byte-identical to before."
        ),
    )
    guided_story_editor_v2_enabled: bool = Field(
        default=False,
        description=(
            "Enable the story-native guided-story editor revision contract. "
            "The legacy guided renderer remains byte-compatible while this is off; "
            "persisted revisions remain readable/renderable after a rollback."
        ),
    )

    # Main Creator Agent. The four independent switches let us expose a
    # read-only conversation/strategy preview before the agent is allowed to
    # mutate a PlanItem or spend render capacity. Execution always requires an
    # explicit user confirmation; automatic iteration remains a later-stage
    # capability with its own kill switch.
    # New plans only; persisted legacy narrated jobs retain their renderer.
    creator_prompt_fidelity_enabled: bool = False
    main_creator_agent_enabled: bool = False
    main_creator_agent_execution_enabled: bool = False
    main_creator_agent_review_enabled: bool = False
    # Quality-review child gate.  This stays separate from the existing editor
    # Director flag: the Creator review consumes an exact rendered generation and
    # has its own cost/latency rollout.
    main_creator_agent_quality_review_enabled: bool = False
    main_creator_agent_auto_iteration_enabled: bool = False
    # Future format and workspace gates.  These are intentionally independent of
    # the Main Creator conversation gate so capability manifests fail closed
    # until their render/ownership contracts are live.
    edit_format_day_vlog_enabled: bool = Field(
        default=False,
        description=(
            "Enable the strict day_vlog guided renderer. It requires a multi-shot "
            "filming guide, preserves first-appearance chronology, caps transitions "
            "at 0.2s, and fails visibly when media is insufficient; it never silently "
            "downgrades to montage. Read by API capability resolution and workers, so "
            "flip Fly + restart both API and worker processes."
        ),
    )
    edit_format_single_hero_enabled: bool = Field(
        default=False,
        description=(
            "Enable the strict single_hero guided renderer. It requires one "
            "dominant source clip plus bounded supporting cutaways, enforces the "
            "duration/hero-share policy, and fails visibly when media is insufficient; "
            "it never silently downgrades to montage. Read by API and workers, so "
            "flip Fly + restart both API and worker processes."
        ),
    )
    # Mixed-media "slide post" output (ordered images + videos — TikTok photo mode /
    # Instagram carousel), plans/024. Ships default ON per policy — unlike
    # edit_format_day_vlog_enabled above, there is no staged rollout for this one.
    # Off: the slide-post routes 404 and edit_format="slides" coerces to montage
    # (coerce_edit_format falls back on anything it doesn't recognize as active).
    # Read by API and workers; flip Fly + restart both, then rebuild Vercel for the
    # NEXT_PUBLIC_SLIDE_POSTS_ENABLED twin. Rollback:
    #   fly secrets set SLIDE_POSTS_ENABLED=false --app nova-video (api + worker)
    slide_posts_enabled: bool = True
    # Camera-roll idea forms (`content_plan.form`). Off: parse neither requires, validates
    # nor stores a form (legacy-shaped ideas; wording edits drop a stored form), so new rows
    # stay readable by an older API image. Flip BEFORE rolling the API image back; see
    # agents/DECISIONS.md "Idea forms". Apply: fly secrets set STORY_IDEA_FORMS_ENABLED=false
    # (api + worker) + restart.
    story_idea_forms_enabled: bool = True
    # KRI-298/299: rich per-slide text (styled `SlideEdits.texts`, rendered as Pillow PNG
    # overlays). Off: the renderer keeps the legacy drawtext path using the mirrored
    # `text` (texts[0]); saved `texts` are preserved. Dual-flag with the iOS capability
    # `slide_post_rich_text`. Apply: `fly secrets set SLIDE_POST_RICH_TEXT_ENABLED=true
    # --app nova-video` + restart (api + worker).
    slide_post_rich_text_enabled: bool = False
    # KRI-298/301: chat editing of slide posts through the edit copilot
    # (`POST /plan-items/{id}/slide-post/chat-edit`, read-only staging). Off: the route
    # 404s. Capability `slide_post_chat_edit`. Apply: `fly secrets set
    # SLIDE_POST_CHAT_EDIT_ENABLED=true --app nova-video` + restart (api).
    slide_post_chat_edit_enabled: bool = False
    # KRI-44: per-slide framing (crop + Vertical/Landscape). Off: the slide-post PUT
    # keeps each slide's stored `framing` and ignores incoming values, the chat op
    # `set_slide_framing` is unavailable, and capability `slide_post_framing` is
    # false. Dual-flag with the iOS capability. Apply: `fly secrets set
    # SLIDE_POST_FRAMING_ENABLED=true --app nova-video` (api + worker) + restart.
    slide_post_framing_enabled: bool = False
    # KRI-606: post-wide size (9:16/4:5/3:4/1:1/1.91:1) and per-photo Fit with
    # bars. Needs `slide_post_framing_enabled` too (Fit lives in the Crop workspace).
    # Off: the slide-post PUT keeps the stored `aspect`/`fit` and ignores incoming
    # values, the chat ops are unavailable, and capability `slide_post_sizes` is
    # false. The renderer always honours stored values. Roll back by flipping the
    # flag only: draft models drop unknown keys, so rolling the CODE back after
    # sizes exist would erase them on the next save. Apply: `fly secrets set
    # SLIDE_POST_SIZES_ENABLED=true --app nova-video` (api + worker) + restart.
    slide_post_sizes_enabled: bool = False
    # Chat ops that write the size/Fit need an app that round-trips them (protocol 5).
    slide_post_sizes_min_client_protocol: int = 5
    # KRI-589 PR2: static freehand drawings on slides. Code default ON (kill switch):
    # off, the slide-post PUT keeps each slide's stored `drawing` and ignores incoming
    # values, and capability `slide_post_drawing` is false; stored drawings still render.
    # The capability additionally needs a build declaring protocol >= the floor below
    # (older builds cannot decode or paint `drawing`). Never roll the CODE back once
    # drawings exist (draft models drop unknown keys); flip the flag instead. Apply:
    # `fly secrets set SLIDE_POST_DRAWING_ENABLED=false --app nova-video` (api + worker) + restart.
    slide_post_drawing_enabled: bool = True
    slide_post_drawing_min_client_protocol: int = 7
    # KRI-647 / KRI-460: pasted image stickers on slides (SlideEdits.stickers). Code default
    # ON (kill switch): off, the slide-post PUT keeps each slide's stored `stickers` and
    # ignores incoming values and capability `slide_post_stickers` is false; stored stickers
    # still render. A save carrying stickers from a build below the floor gets 426
    # client_update_required. Apply: `fly secrets set SLIDE_POST_STICKERS_ENABLED=false
    # --app nova-video` (api + worker) + restart. Never roll the CODE back once stickers exist.
    slide_post_stickers_enabled: bool = True
    # Prompt-fidelity honesty (a): an approval body that carries a shape is not proof the
    # creator picked it (the app echoes the preselected default). On => the plan says "The
    # creator selected this output format." only for an explicit pick. Off => byte-identical
    # to before. Apply: `fly secrets set RENDER_SHAPE_ORIGIN_HONEST_ENABLED=false` (api +
    # worker) + restart.
    render_shape_origin_honest_enabled: bool = True
    # Prompt-fidelity honesty (b): the live "look" chip names the font the phone actually
    # burned, not the plan's pre-typography default. Off => the pre-fix chip.
    look_chip_burned_font_enabled: bool = True
    # Prompt-fidelity honesty (c): a phone export whose on-screen text check was rejected
    # (it still ships) leaves a creator-facing render note. Off => no note.
    export_text_note_enabled: bool = True
    slide_post_sticker_min_client_protocol: int = 8
    # KRI-557: provider-marked Gemini thought summaries for native creation and
    # slide-post calls. Deploy dark; enable after production canary with
    # THOUGHT_SUMMARIES_ENABLED=true for API and Celery processes after canary.
    thought_summaries_enabled: bool = False
    # KRI-482: keep extended phone slide exports off until physical-device parity passes.
    # False routes video/look slides through the existing server renderer.
    slide_post_extended_device_export_enabled: bool = False
    main_creator_agent_freeform_uploads_enabled: bool = False
    main_creator_agent_workspace_enabled: bool = False
    main_creator_agent_rollout_percent: int = Field(default=0, ge=0, le=100)
    # KRI-118: story-shape capability flag consumed by the Main Creator agent
    # (a separate lane) to gate offering montage "shapes" as a creator-facing
    # concept. Not wired to anything in this module or its callers; added
    # here so that lane can read it without a second config PR. Ships
    # default ON per that lane's rollout plan.
    creator_montage_shapes_enabled: bool = True
    # KRI-118 item 5: Main Creator session budgets, moved off hardcoded
    # literals in routes/creator_agent.py (`CreatorAgentSession` creation).
    creator_question_budget: int = 2
    creator_max_render_attempts: int = 2

    @model_validator(mode="after")
    def reject_guided_edit_before_strict_renderer(self) -> "Settings":
        """Make the guided-edit rollout sequence structurally impossible to skip."""

        if self.guided_edit_enforcement_enabled and not self.guided_edit_capability_enabled:
            raise ValueError("guided edit enforcement requires guided edit capability")
        if self.guided_edit_conversation_enabled and not self.guided_edit_capability_enabled:
            raise ValueError("guided edit conversation requires guided edit capability")
        if self.guided_edit_enforcement_enabled and not GUIDED_STORY_RENDERER_READY:
            raise ValueError("guided edit enforcement requires the strict story renderer")
        if self.main_creator_agent_execution_enabled and not self.main_creator_agent_enabled:
            raise ValueError("main creator execution requires the main creator agent")
        if self.main_creator_agent_review_enabled and not self.main_creator_agent_enabled:
            raise ValueError("main creator review requires the main creator agent")
        if (
            self.main_creator_agent_quality_review_enabled
            and not self.main_creator_agent_review_enabled
        ):
            raise ValueError("main creator quality review requires review")
        if (
            self.main_creator_agent_auto_iteration_enabled
            and not self.main_creator_agent_review_enabled
        ):
            raise ValueError("main creator auto iteration requires review")
        if (
            self.main_creator_agent_auto_iteration_enabled
            and not self.main_creator_agent_quality_review_enabled
        ):
            raise ValueError("main creator auto iteration requires quality review")
        if (
            self.main_creator_agent_auto_iteration_enabled
            and not self.main_creator_agent_execution_enabled
        ):
            raise ValueError("main creator auto iteration requires execution")
        return self

    # Agent/session retention (days). Job- and creator-session-scoped agent
    # runs plus creator sessions/events/receipts older than this are deleted
    # by `tasks.cleanup_agent_runs`. Template- and track-scoped rows remain.
    agent_run_retention_days: int = 30

    # Security
    token_encryption_key: str = ""

    # OAuth
    instagram_client_id: str = ""
    instagram_client_secret: str = ""
    instagram_redirect_uri: str = "http://localhost:8000/auth/instagram/callback"

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = "http://localhost:8000/auth/youtube/callback"

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "http://localhost:8000/tiktok/oauth/callback"
    tiktok_web_app_url: str = "http://localhost:3000/library"
    tiktok_media_base_url: str = "http://localhost:8000/tiktok/media"
    tiktok_publishing_enabled: bool = False
    # Rollout guard for the distinct Upload API consent path. Keep this off
    # during the code deploy, then enable it only after every worker runs the
    # delivery-mode-aware task implementation.
    tiktok_draft_upload_enabled: bool = False
    tiktok_content_posting_audited: bool = False
    tiktok_performance_sync_enabled: bool = False
    tiktok_publishing_beta_user_ids: list[str] = []

    @field_validator("tiktok_publishing_beta_user_ids", mode="before")
    @classmethod
    def parse_tiktok_beta_user_ids(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return value
        return [part.strip() for part in raw.split(",") if part.strip()]

    # Deep TikTok profile analysis (enriched fetch + LLM distillation).
    # When true, scrape_tiktok_profile chains analyze_tiktok_profile after the
    # flat fetch, enriching the persona/plan/hooks with the creator's own proven
    # style. Set to false for a quick kill switch (no deploy needed):
    #   fly secrets set TIKTOK_DEEP_ANALYSIS_ENABLED=false --app nova-video
    #   + fly machine restart <id>
    tiktok_deep_analysis_enabled: bool = True

    # Vision-based style ingest (Creator Agent style pipeline). When True,
    # scrape_tiktok_profile chains analyze_tiktok_style which downloads the
    # creator's own videos, runs StyleObservationAgent, and persists the aggregate
    # to persona.tiktok_profile["style_observations"]. Expensive (~30 Flash vision
    # calls + MP4 downloads per user). Ships OFF; flip after cost/latency bake:
    #   fly secrets set TIKTOK_STYLE_VISION_ENABLED=true --app nova-video
    #   + fly machine restart <id>
    # Separate from user_style_enabled (render gate) so ingest and render can be
    # dark-launched independently.
    tiktok_style_vision_enabled: bool = False

    # Per-user style entity (Creator Agent M1). When True, a UserStyle is derived
    # from the persona after generation and applied to every generative render:
    # style-set pin bypasses the per-render AgenticStyleSelectorAgent; knob overrides
    # win over the curated set's values inside _resolve_intro_overlay_params.
    # Ships OFF (False) so zero renders are affected on deploy; enable after live-eval
    # validation of the style_derivation agent:
    #   fly secrets set USER_STYLE_ENABLED=true --app nova-video + restart workers.
    # When False OR when personas.style is NULL: all_candidates carries no
    # "user_style" key → renders are byte-identical to pre-M1 output.
    user_style_enabled: bool = False

    # Account-wide creator memory/direction control plane. Storage and owner
    # routes are additive; prompt/render extraction remains separately wired.
    # KRI-215: explicit cohort AND current training eligibility are required.
    request_following_collection_enabled: bool = False
    request_following_report_token: str = ""
    request_following_all_users: bool = False
    request_following_user_ids: list[str] = []
    request_following_thread_ids: list[str] = []
    request_following_holdout_thread_ids: list[str] = []

    creator_memory_enabled: bool = False
    # KRI-648: stamped only at dispatch; legacy generations are never restyled.
    phone_text_styling_enabled: bool = False
    # KRI-336: all three rollout conditions precede owner opt-in. No public cohort.
    creator_edit_learning_enabled: bool = False
    creator_edit_learning_privacy_approved: bool = False
    creator_edit_learning_user_ids: list[str] = []

    # Conversational style agent (Creator Agent M2). When True, the
    # POST /personas/agent/start and POST /personas/agent/turn routes are live.
    # Ships OFF — enable after live-eval validation of StyleIntentAgent quality:
    #   fly secrets set STYLE_AGENT_ENABLED=true --app nova-video + restart workers.
    # When False: both agent routes return 404 — byte-identical to pre-M2 behavior.
    style_agent_enabled: bool = False

    # Song rule (legal): only a creator-uploaded song ever plays on a Kria creator job. true = the
    # render-time auto-matcher (`_match_best_track`) is never run for a creator job (a phone job,
    # or a cloud/guided job stamped with the creator render contract), so no catalog track is
    # picked for it; a track the creator approved (frozen licensed music) is a separate path and
    # is unchanged. false = byte-identical to before. Rollback: `fly secrets set
    # CREATOR_JOBS_NEVER_AUTO_MATCH_CATALOG_SONG_ENABLED=false` (api + worker) + restart.
    creator_jobs_never_auto_match_catalog_song_enabled: bool = True

    # Chat-based full-editor copilot. When True,
    # POST /plan-items/{item}/variants/{variant}/copilot/turn is live and returns
    # proposed draft edit ops; it never writes Job/PlanItem rows. Frontend twin:
    # NEXT_PUBLIC_EDIT_COPILOT_ENABLED gates the Nova drawer. Default off until
    # localhost QA validates the local-op applier and save parity.
    edit_copilot_enabled: bool = False
    # KRI-186: chat-edit replies are built deterministically from what was
    # applied + what was rejected/unmet, and the copilot sees prior turns and
    # the thread's original request. Kill switch: false restores the legacy
    # "<Op>. Everything else is unchanged." reply and stateless copilot turn.
    copilot_honest_replies_enabled: bool = True
    # 29 Ekim lane E (KRI-1039): an ask the editor ops can express is composed with them
    # instead of refused, asked about or re-planned: a word-by-word title is one
    # `replace_text_sequence` (weighted windows, `cumulative` mode), an edit-shaped follow-up
    # after a failed render goes to the copilot, and a missing text sequence is composed rather
    # than asked about. false = the previous routing, replies and ops, byte-identical.
    kria_compose_unmet_with_ops_enabled: bool = False
    # KRI-219 latency: serve short in-place text/label/order edits with the fast copilot
    # BEFORE the pro-model requirement extraction (which then runs off the critical
    # path). false = the previous extract-first order for every post-render turn.
    kria_copilot_first_enabled: bool = True
    # Analyse a thread's clips in the background after attach (KRI-219) so the copilot
    # knows what the footage shows (`analysis["understanding"]`). Needs a Gemini key.
    # false = no analysis; chat edits then clarify instead of describing clips.
    kria_clip_understanding_enabled: bool = True
    # How long a chat turn whose clip instructions need that analysis waits for it
    # (re-reading every 2 s) before replying "I'm still checking some of your clips".
    # Also capped by the turn's own time limit. 0 = one fresh re-read, no waiting.
    kria_clip_understanding_wait_s: float = Field(default=45.0, ge=0, le=90)
    # KRI-282: prompt-directed spoken-excerpt montage. A phone montage whose
    # footage includes a talking-to-camera clip may play chosen excerpts of that
    # speech over other footage, cut to the speaker, and return to fast montage
    # (app/pipeline/phone_speech_montage_plan.py). true = on for everyone (the
    # planner agent only acts when the creator asks for it); false = the legacy
    # montage path, byte-identical, and timed transcript segments are neither
    # stored nor shown to any prompt. Emergency rollback: `fly secrets set
    # SPEECH_EXCERPT_MONTAGE_ENABLED=false --app nova-video` + restart api+worker.
    speech_excerpt_montage_enabled: bool = True
    # Pace unit (prompt fidelity): the planner's `pacing` (relaxed / balanced / fast) sets how
    # long each clip is held in a phone montage, and the plan's snapshot carries the strategy's
    # pace instead of the constant "fast". true = pacing drives cut lengths (shared rule
    # `schemas.edit_proposal.pace_hold_bounds`, used by approval-time length planning and by
    # the render); false = every cut length and the snapshot pace are byte-identical to before.
    # A creator-stated length always wins over pace and no clip is ever dropped for it.
    # Rollback: `fly secrets set PACE_DRIVES_CUTS_ENABLED=false --app nova-video` + restart
    # api+worker.
    pace_drives_cuts_enabled: bool = True
    # Text-explainer unit (prompt fidelity): a planner direction of `text_explainer` or
    # `guided_story` is built as a story on the phone montage route (story beats from
    # `story_structure`, the text step's captions as the beat text, every moment held at least
    # the direction's own minimum, crossfades) instead of always a hard-cut fast montage. true =
    # built where the footage and the creator's asks allow it; anything else (a per-clip text
    # ask, a freeze, a stated length the holds cannot fit, a text step that fails) is cut as the
    # fast montage and the plan says so. Never refuses a render. false = byte-identical to
    # before. Rollback: `fly secrets set STORY_DIRECTIONS_ON_MONTAGE_ENABLED=false --app
    # nova-video` + restart api+worker.
    story_directions_on_montage_enabled: bool = True
    # KRI-808: a clip's timed words are kept only when whisper's language agrees with the
    # clip's Gemini transcript; a pass that disagrees (whisper TRANSLATED accented speech) is
    # re-transcribed in the language Gemini or the chat heard, else stored as no words. Also
    # drops whisper's silence-time placeholders, credits and loops. Cost: at most two extra
    # whisper-1 calls on a disagreeing clip (~$0.0003 each on a 3 s clip). false = the
    # pre-KRI-808 first pass, stored as is. Apply: fly secret + worker restart.
    clip_speech_language_check_enabled: bool = True
    # Owner-safe "Nova steps" activity feed projected from pipeline_trace +
    # phase_log + AgentRun (app/services/nova_steps.py) onto the generative
    # job status response. Ships OFF -- `steps` stays None (byte-identical
    # response) until enabled. Frontend twin (future PR):
    # NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED.
    nova_steps_feed_enabled: bool = False
    # Proactive, read-only editorial suggestions. The backend and frontend each
    # have a gate; accepting a suggestion still changes only the local draft.
    edit_director_enabled: bool = False
    # Per-boundary transition operations authored by the editor/copilot.
    edit_transitions_enabled: bool = False
    # Async Gemini Omni short-video generation. Deliberately separate from the
    # Director: the Director returns structured ops; Omni returns video bytes.
    omni_generated_video_enabled: bool = False

    # Creator Agent M4: ConformanceFeedbackAgent at clip-attach time (best-effort).
    # Ships OFF — fires async after attach_clips, never blocks the 200 response.
    # Enable after live-eval validation of the conformance_feedback agent:
    #   fly secrets set CONFORMANCE_FEEDBACK_ENABLED=true --app nova-video + restart workers.
    # When False: analyze_item_conformance task is a no-op; item.conformance stays NULL.
    conformance_feedback_enabled: bool = False

    # Media-overlay cards (slice 1): timed, positioned image/video "cards"
    # composited on top of a finished plan-item variant via a post-pass ffmpeg
    # encode. Additive + kill-switched; when False all variant bytes are
    # byte-identical to pre-slice-1 output. Enable after browser QA:
    #   fly secrets set MEDIA_OVERLAYS_ENABLED=true --app nova-video + restart workers.
    # When False: upload-urls + set-media-overlays routes return 404; the
    # worker apply-pass branch never fires.
    media_overlays_enabled: bool = False
    # Selects the resumable pool-backed protocol for manual overlay uploads.
    # Keep the legacy presign/PUT/confirm path available during rollout so a
    # mixed frontend/backend deploy never strands an in-flight direct PUT.
    reliable_overlay_uploads_enabled: bool = False
    # Alpha-preserving image overlays for the media-overlay lane. When False,
    # image cards still flatten through JPEG exactly as before.
    media_overlay_alpha_enabled: bool = False

    # First-class visual replacement blocks (montage + interstitial text cards).
    # Blocks render below authored text/captions and are additive: with the flag
    # off, no existing render path or editor capability changes.
    visual_blocks_enabled: bool = False
    # Separate quality gate for zero-click visual-treatment planning. Manual
    # authoring can launch first while planner evals are still running.
    visual_block_autoplan_enabled: bool = False

    # Curated CanvasKit motion-preset lane. The API/worker gate is flipped
    # before NEXT_PUBLIC_MOTION_SCENES_ENABLED so rolling deploys fail closed.
    # Advertise only after all API/worker instances support appearance persistence/rendering.
    text_appearance_enabled: bool = False
    motion_scenes_enabled: bool = False
    # Exposure gate for new Evolving Type insertions and motion-control edits.
    # Persisted instances continue to validate and render while disabled.
    evolving_type_enabled: bool = False

    # KRI-7 contextual zoom-in emphasis. The renderer and the editor lane are
    # NOT gated by this flag — creators can always author a zoom by hand. What
    # it gates is the ONE `camera_emphasis` LLM call that picks the moments
    # automatically. Kill switch: CAMERA_EMPHASIS_AI_ENABLED=false → renders
    # fall back to the deterministic Smart-preset camera picks (the pre-KRI-7
    # behavior) with no other change. Already-placed effects keep rendering.
    # Apply: fly secrets set CAMERA_EMPHASIS_AI_ENABLED=false --app nova-video
    # + machine restart (worker).
    camera_emphasis_ai_enabled: bool = True

    # Sound-effects glossary + user placement (PR-1 foundation). Admin-curated
    # SFX + user uploads placed at arbitrary timestamps in a plan-item variant.
    # Kill switch: SOUND_EFFECTS_ENABLED=false → sfx-upload-urls + sound-effects
    # routes return 404; the render apply-pass branch never fires.
    sound_effects_enabled: bool = False

    # Smart Captions v2 licensed music bed. Deliberately independent of
    # SOUND_EFFECTS_ENABLED (that flag gates the user SFX lane; the music bed
    # is agent-selected). Kill switch: SMART_MUSIC_BED_ENABLED=false → new
    # renders resolve no music treatment and reburns skip re-mixing the bed
    # (persisted treatments are preserved for re-enable, never deleted).
    # Apply: fly secrets set SMART_MUSIC_BED_ENABLED=false --app nova-video
    # + machine restart (worker).
    smart_music_bed_enabled: bool = True

    # KRI-20: the v2 bed used to auto-match a track on EVERY subtitled
    # (talking-to-camera) render with no creator intent involved — an
    # unrequested edit to the user's audio. Default True: a bed is now only
    # ever created via the explicit editor background-music picker; a
    # render never invents one, and a persisted (user-selected) treatment is
    # preserved verbatim across reburns/re-renders instead of being
    # re-matched or dropped. False restores the pre-fix auto-match.
    # Apply: fly secrets set SMART_MUSIC_BED_REQUIRES_REQUEST_ENABLED=false
    # --app nova-video + machine restart (worker).
    smart_music_bed_requires_request_enabled: bool = True

    # Overlay auto-placement (plan 005, PR0+). Gates the plan-item asset-pool
    # routes (upload-urls / register / list / delete) and, in later PRs, the
    # matcher + suggestion routes. Frontend twin: NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED
    # in Vercel — keep in sync (dual-flag trap, see CLAUDE.md).
    # Kill switch: OVERLAY_AUTOPLACE_ENABLED=false → all pool/suggestion routes 404.
    overlay_autoplace_enabled: bool = False

    # General overlay matcher/planner queue. These tasks can use transcript
    # fallback and stay on the media-safe render worker in production.
    autoplace_queue: str = "celery"
    # Upload-time image/video metadata analysis is isolated from renders in Fly.
    # Roll back by setting POOL_ASSET_ANALYSIS_QUEUE=celery.
    pool_asset_analysis_queue: str = "celery"
    # Creator-facing analyze_pool_asset dispatches (Visuals photos/videos) go here
    # instead of pool_asset_analysis_queue, so they never wait FIFO behind footage
    # analysis. Empty = pool_asset_analysis_queue (pre-change routing). Set
    # VISUALS_ANALYSIS_QUEUE=visuals-analysis only after the deployed autoplace
    # process consumes that queue (fly.toml); unset it to roll back.
    visuals_analysis_queue: str = ""

    @property
    def pool_asset_visuals_queue(self) -> str:
        """Queue for creator-facing analyze_pool_asset runs (see visuals_analysis_queue)."""
        return self.visuals_analysis_queue or self.pool_asset_analysis_queue

    # Keep response compatibility during the backend-first rollout. Set true
    # only after both queued-aware upload surfaces are deployed.
    pool_asset_queued_status_enabled: bool = False

    poster_ondemand_repair_enabled: bool = Field(
        default=False,
        description="On-demand library poster repair. When true, "
        "POST /me/jobs/posters/refresh stops being a pure re-signer: for owned "
        "ready jobs whose preview has no poster it stamps a `_poster_repair` "
        "marker and enqueues `tasks.repair_job_poster`, which extracts + uploads "
        "the missing poster and persists the RELATIVE key into the same field the "
        "render path writes. Default false (repo rollout culture: "
        "default-false-then-flip) — already-deployed frontends poll this endpoint "
        "in bursts, so a default-true ship would be a zero-canary activation. Off "
        "is byte-identical to the pre-feature endpoint: no marker writes, no "
        "dispatch, and any already-queued task re-checks this flag and no-ops. "
        "Enable AFTER setting POSTER_REPAIR_QUEUE: `fly secrets set "
        "POSTER_ONDEMAND_REPAIR_ENABLED=true --app nova-video` + machine restart "
        "(api + worker). Rollback: `fly secrets set "
        "POSTER_ONDEMAND_REPAIR_ENABLED=false --app nova-video` + restart.",
    )
    poster_repair_queue: str = Field(
        default="celery",
        description="Celery queue for `tasks.repair_job_poster`. The code default "
        "keeps local `dev-auto.sh` working (it consumes no autoplace queue). In "
        "production set POSTER_REPAIR_QUEUE=autoplace-jobs BEFORE flipping "
        "POSTER_ONDEMAND_REPAIR_ENABLED so full-MP4 downloads land on the 2GB "
        "autoplace machine — never on the 1GB `light`/Beat machine (the "
        "2026-08-02 OOM class) and never head-of-line-blocking the concurrency=1 "
        "render worker. Roll back with `fly secrets set POSTER_REPAIR_QUEUE=celery "
        "--app nova-video` + machine restart (api + worker).",
    )

    # Zero-click auto-apply (plan 007, decision D2-B + G3-A): after a plan-item
    # generate render finalizes, matched visuals are burned in WITHOUT review on
    # speech-bearing variants. Dedicated kill switch — turning THIS off in prod
    # never kills manual suggest (OVERLAY_AUTOPLACE_ENABLED). When
    # MEDIA_OVERLAYS_ENABLED is off, auto-apply degrades to suggest-only + trace.
    overlay_autoapply_enabled: bool = False

    # SFX auto-suggestions (word-level sound design): after a plan-item render
    # finalizes, an agent proposes sound-effect placements anchored to spoken
    # words / pauses / clip moments, persisted as ADVISORY
    # `pending_sfx_suggestions` on the variant (stale-filtered on read; nothing
    # renders until the user or the copilot realizes one as an ordinary
    # placement). Suggest-only by design — there is deliberately no autoapply
    # twin (cut at plan review: SFX are taste-heavy, catalog is thin).
    # Kill switch: SFX_AUTOPLACE_ENABLED=false → chain never dispatches.
    sfx_autoplace_enabled: bool = False

    # Full-screen cutaway takeovers (plan 009). Gates ONLY the AI suggestion
    # branch (build_suggestions slot "full" → display_mode="fullscreen"); the
    # manual popover toggle is ungated, and render rollback is covered by
    # MEDIA_OVERLAYS_ENABLED. Default False per the family rollout pattern
    # (eng review E2): flip on Fly only AFTER the T4/T5 web surfaces are live
    # on Vercel — otherwise auto-applied takeovers preview as small pip tiles
    # (the #296/#297 parity incident class).
    fullscreen_cutaways_enabled: bool = False

    # Blossom-style carousel moments (scale-sweep / cover-flow / cards-stack /
    # flipbook) inserted into generative montage variants. Additive +
    # kill-switched: when False no segment is inserted and no carousel code
    # runs — output stays byte-identical to pre-feature. Default False pending
    # parity-gate green.
    carousel_effects_enabled: bool = False

    # Authoring policy that attaches `spec["carousel_moment"]` to ONE eligible
    # montage-variant spec per job (see `_author_carousel_moments` in
    # generative_build.py) so the render hook above actually fires on real
    # jobs instead of only on manually-crafted specs. Requires
    # `carousel_effects_enabled` at render time — authoring without the render
    # flag would silently do nothing anyway, but this is checked separately
    # (both flags, for clarity at the call site). Default False pending E2E.
    carousel_auto_author_enabled: bool = False

    # Per-item "Ask Kria" advisor (plan dogfood feedback #2): conversational,
    # read-only advice about which clip fits which shot. Additive + auth'd; it
    # never writes state (the re-read offer goes through the clip-note PATCH).
    # Kill switch: PLAN_ITEM_ADVISOR_ENABLED=false → route returns 404.
    plan_item_advisor_enabled: bool = True

    # Idea-centric plan redesign (2026-06-17): PlanItem is the spine; no auto-generate
    # on create; ideas added directly as PlanItems; AI generation is opt-in.
    # Kill switch: IDEA_CENTRIC_PLAN_ENABLED=false → create_plan auto-generates (old behavior).
    idea_centric_plan_enabled: bool = True

    # Sandboxed effect-language validator + execution (PR5 lands the validator
    # at app/pipeline/custom_effects.py inert; PR6 wires the `apply_custom_effect`
    # copilot op, the edit_copilot.py family gate, and the
    # app/tasks/custom_effects_render.py execution task behind this flag).
    # Defaults false. Dual-flag with NEXT_PUBLIC_CUSTOM_EFFECTS_ENABLED
    # (Vercel — gates the frontend op-family exposure) — Fly first, then
    # Vercel, per this repo's dual-flag convention.
    custom_effects_enabled: bool = False

    # Scoring weights (named constants — change here only)
    hook_weight: float = 0.65
    engagement_weight: float = 0.35

    # Pipeline limits
    max_upload_bytes: int = 4 * 1024 * 1024 * 1024  # 4GB
    max_duration_s: float = 1800.0  # 30 min
    max_concurrent_jobs_per_user: int = 5
    max_jobs_per_hour_per_user: int = 20

    # Output spec
    output_width: int = 1080
    output_height: int = 1920
    output_fps: int = 30
    # Capped-CRF ceiling, NOT an ABR target — see reframe._encoding_args. CRF 18
    # is the quality driver; this bounds peak bitrate (bufsize = 2× this). Raised
    # 4M→8M after the 4M ceiling macroblocked the dark sky on a 10-bit HLG iPhone
    # sunset (lisbon1.MOV, 2026-05-25). Raised 8M→16M (2026-05-26) after a dark
    # HLG night clip (job 792f2d52) still pinned to the 8M ceiling and macroblocked:
    # at 16M the same footage uses ~16 Mbps, i.e. CRF 18 genuinely wanted ~2× what
    # 8M allowed. Local A/B (SSIM vs clean master) showed the ceiling bump alone cut
    # dark-region distortion ~31%. CRF still governs, so easy/bright clips stay small.
    output_video_bitrate: str = "16M"
    output_audio_bitrate: str = "192k"
    output_min_duration_s: float = 45.0
    output_max_duration_s: float = 59.0
    output_target_lufs: float = -14.0


settings = Settings()
