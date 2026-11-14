"""Capability parity: every human editor action has an agent op, or says why not (29 Ekim).

The creator asked Kria for things a person does by hand in the editor (show a title one word at a
time, shuffle the clips) and the agent refused or failed each one. The agent's ops had grown by
request, not by what the editor can do. This table is the lesson in structure: each public
function of the phone editor session (``NativeEditorSession.swift`` and the files that extend
it: ``NativeEditorRetiming.swift``, ``NativeTextEditing.swift``) belongs to exactly one group,
and a group is one of

* ``agent_ops``: the edit reaches the agent through these ops on an iPhone-rendered edit (names
  checked against the copilot's op set) and, when ``fields`` is set, through those fields of
  them (checked against what the copilot's parser accepts), or
* ``agent_unsupported``: a person can, the agent cannot yet, with the reason written down, or
* ``not_an_edit``: playback, selection, loading, saving and read-only queries.

``tests/kria/test_editor_capability_parity.py`` fails when the Swift file gains a public function
that no group names, when a group names one that is gone, and when an ``agent_ops`` group names an
op the copilot does not have or a field none of its ops accepts. Adding an editor action
therefore forces the question "can the agent do this?" in the same change. Name ``fields``
whenever the op is broader than the action: an op name alone once claimed caption animation
while ``set_caption_meta`` refused ``animation_phases``.

Names are not enough either (KRI-1388): seven groups once passed every check above and still
could not run from iPhone chat. The same test builds a device snapshot for every op of every
``agent_ops`` group, runs the real parser and compiler on it, and fails when one is refused. An
``agent_unsupported`` group with a phone probe must be refused there, so the table cannot
understate either.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParityGroup:
    kind: str  # "agent_ops" | "agent_unsupported" | "not_an_edit"
    actions: frozenset[str]
    ops: tuple[str, ...] = ()
    reason: str = ""
    fields: tuple[str, ...] = ()


def _g(
    kind: str,
    actions: str,
    ops: tuple[str, ...] = (),
    reason: str = "",
    fields: tuple[str, ...] = (),
) -> ParityGroup:
    return ParityGroup(kind, frozenset(actions.split()), ops, reason, fields)


PARITY_GROUPS: dict[str, ParityGroup] = {
    # ------------------------------------------------------------------ text
    "text_authoring": _g(
        "agent_ops",
        "addText addTextCard beginTextCreation cancelTextCreation finishTextCreation "
        "updatePendingText placeTextBlock textBlockPlacements updateText updateTextContent "
        "updateTextRaw setTextRaw deleteText textDeletion paste pasteText applyTextPreset "
        "transformText",
        ("add_text", "rewrite_text", "edit_text", "remove_text", "remove_texts", "set_title"),
    ),
    "text_look": _g(
        "agent_ops",
        "setTextAlignment setTextAnimation setTextAnimationSpeed setTextBehindSubject "
        "setTextColor setTextHighlightColor setTextPhase setTextPosition setTextShadow "
        "setTextSize setTextStroke setTextStyle setTextWidth",
        ("patch_text", "patch_text_style", "patch_text_appearance"),
    ),
    "text_timing": _g(
        "agent_ops", "setTextTiming updateTextTiming", ("set_text_timing", "set_texts_timing")
    ),
    # ---------------------------------------------------------------- captions
    "captions": _g(
        "agent_ops",
        "beginCaptionLineEdit endCaptionLineEdit updateCaptionCue setCaptionMeta setCaptionDisplay "
        "setCaptionEnabled toggleCaptions setCaptionStyle setCaptionFont setCaptionColor "
        "setCaptionHighlightColor setCaptionSize setCaptionPositionY setCaptionShadowEnabled "
        "setCaptionStrokeWidth setCaptionAppearance",
        # Not patch_text_appearance: the chat snapshot models text bars only, never caption
        # targets (`_text_appearance_inventory`), so caption looks go through set_caption_meta.
        (
            "edit_caption",
            "replace_caption_text",
            "set_caption_meta",
            "set_caption_timing",
            "set_caption_emphasis",
        ),
    ),
    # All captions only: one line's animation (`CaptionAnimationScope.line`) has no agent op.
    "caption_animation": _g(
        "agent_ops",
        "setCaptionAnimationSpeed setCaptionPhase",
        ("set_caption_meta",),
        fields=("animation_phases",),
    ),
    # KRI-1394: one line (`line_overrides[<cue id>]`) or every line, as writeCaptionStyle writes
    # it. A line is addressed by cue id, so captions kept as text rows (no cue list) are not
    # offered the family. finishCaptionGesture's "apply to all" is the same op with all: true.
    "caption_style_scope": _g(
        "agent_ops",
        "setCaptionGesturePosition setCaptionGestureSize finishCaptionGesture "
        "setCaptionStyleSize placeCaptions",
        ("patch_caption_line",),
        fields=("cue_id", "all", "size_px", "position_x_frac", "position_y_frac"),
    ),
    # --------------------------------------------------------------- timeline
    "clip_trim_and_move": _g(
        "agent_ops",
        "trimSelected beginTrim updateTrim endTrim moveSelected moveClip slideSourceWindow "
        "setClipTiming setClipSourceWindow updateClipSourceWindow setSourceWindow "
        "beginTimedEdgeTrim updateTimedEdgeTrim endTimedEdgeTrim beginTimedBodyMove "
        "updateTimedBodyMove endTimedBodyMove",
        (
            "set_clip_duration",
            "set_clip_in",
            "trim_clip_start",
            "trim_output_start",
            "reorder_clip",
            "reorder_clips_by",
            "set_total_duration",
            "patch_slots",
        ),
    ),
    "clip_add_remove": _g(
        "agent_ops",
        "removeClip restoreClip deleteSelectedClip deleteSelection addClip addDeviceTimelineMedia "
        "addLibraryVisual placeEditorSource removeVisualSelection splitSelectedClip",
        ("remove_clip", "add_unused_sources", "split_clip", "remove_visual_media"),
    ),
    "clip_transition": _g(
        "agent_ops", "setClipTransition setTransitionAcrossVideo", ("set_transition",)
    ),
    "clip_look": _g(
        "agent_unsupported",
        "setClipLookPreset updateClipLook setClipLookAdjustments",
        reason="A phone-rendered edit cannot carry a clip look: its capability map closes "
        "clips.looks (golden_hour compiles only on exact-canvas, unrotated sources) and the "
        "editor refuses one on device (NativeEditorSession.canSetClipLook). Chat's FFmpeg "
        "custom effects (apply_custom_effect) are cloud-only: the phone recipe has no field "
        "for them (phone_authored_timeline refuses custom_effects).",
    ),
    "clip_speed": _g(
        "agent_ops",
        "setFootagePlaybackRate",
        ("patch_slots",),
        fields=("playback_rate",),
    ),
    "motion_speed": _g(
        "agent_unsupported",
        "setMotionSpeed",
        reason="It sets motion_scenes[].motion.speed, and a phone edit has no motion scenes: "
        "validate_phone_pilot_recipe refuses them and the authored phone compile refuses "
        "motion_scenes (see visual_blocks_and_motion). Footage speed is clip_speed.",
    ),
    "clip_crop": _g(
        "agent_ops",
        "setFootageCrop",
        ("patch_slots",),
        fields=("source_crop",),
    ),
    "clip_duplicate": _g(
        "agent_ops", "duplicateSelectedClip", ("duplicate_clip",), fields=("slot_index",)
    ),
    "clip_source_replacement": _g(
        "agent_ops",
        "replaceSelectedClipSource",
        ("replace_clip_source",),
        fields=("slot_index", "clip_index"),
    ),
    "clip_tilt": _g(
        "agent_ops",
        "setClipTilt applyTiltBackdropToAllClips",
        ("patch_slots",),
        fields=("tilt",),
    ),
    "clip_audio": _g(
        "agent_ops",
        "toggleClipAudio setOriginalAudioLevel setOriginalMixLevel",
        ("set_mix",),
    ),
    "clip_source_gain": _g(
        "agent_ops",
        "setSelectedClipSourceVolume",
        ("patch_slots",),
        fields=("source_audio_volume",),
    ),
    "split_screen_create": _g("agent_ops", "createSplitScreen", ("create_split_screen",)),
    "split_screen_layout": _g(
        "agent_ops",
        "setSplitScreenDuration setSplitScreenPanelCount setSplitScreenPanelWeight "
        "setSplitScreenGutter setSplitScreenBackgroundColor beginSplitDividerDrag "
        "updateSplitDividerDrag endSplitDividerDrag cancelSplitDividerDrag setSplitCollapse",
        ("update_split_screen",),
        fields=(
            "duration_s",
            "panel_count",
            "panel_weights",
            "gutter",
            "background_color",
            "collapse_empty_panels",
        ),
    ),
    "split_screen_panel_order": _g("agent_ops", "swapSplitPanels", ("swap_split_panels",)),
    "split_screen_clip_order": _g("agent_ops", "moveSelectedSplitScreenClip", ("move_split_clip",)),
    "split_screen_section_order": _g(
        "agent_ops", "moveSelectedSplitScreenSection", ("move_split_screen",)
    ),
    "split_screen_clip_offset": _g(
        "agent_ops",
        "setSelectedSplitScreenOffset",
        ("assign_split_panel",),
        fields=("offset_s",),
    ),
    # KRI-1420: Add to panel places a source after the panel's last clip. Unlike the phone, chat
    # grows the section to hold it (grow_section false keeps the length and cuts the clip).
    "split_screen_panel_add": _g(
        "agent_ops",
        "addSplitPanelClip",
        ("add_split_panel_clip",),
        fields=("clip_index", "offset_s", "duration_s", "grow_section"),
    ),
    "split_screen_remove": _g(
        "agent_ops", "removeSplitScreen", ("remove_split_screen",), fields=("keep_panel",)
    ),
    # Absorbing the ordinary clips is the section growing to the whole video and each clip joining
    # the top panel at its place.
    "split_screen_whole_video": _g(
        "agent_ops",
        "extendSplitToWholeVideo",
        ("update_split_screen", "assign_split_panel"),
        fields=("duration_s", "slot_index", "offset_s"),
    ),
    # Each copy is one assign_split_panel duplicate placed after the panel's last clip.
    "split_screen_loop_fill": _g(
        "agent_ops",
        "loopFillSplitPanel",
        ("assign_split_panel",),
        fields=("duplicate", "offset_s"),
    ),
    # KRI-1394: the server port of splitFullScreenResult (app/kria/split_full_screen.py), equal to
    # the phone's on tests/fixtures/split_full_screen.json; at_s is the playhead and panel the
    # selected clip's panel. Refused on Talking source and narrated edits and where text the phone
    # re-times lies over the section.
    "split_screen_full_screen": _g(
        "agent_ops",
        "makeSplitFullScreen",
        ("make_split_full_screen",),
        fields=("section_id", "at_s", "side", "panel"),
    ),
    # KRI-1457: Fill with blank puts a colour block after the panel's last member (the phone fills
    # the free time under the playhead; chat has no playhead, so it uses the time after the last
    # member). Block colour recolours the selected block by its slot index.
    "split_screen_blank": _g(
        "agent_ops",
        "fillSplitPanelWithBlank setSelectedBlankColor",
        ("add_split_blank", "set_split_blank_color"),
        fields=("offset_s", "duration_s", "color", "grow_section"),
    ),
    "split_screen_queries": _g(
        "not_an_edit",
        "canExtendSplitToWholeVideo canAddToSplitPanel canMakeSplitFullScreen splitClipsRemoved "
        "canFillSplitPanelWithBlank",
        reason="Read-only checks the Split panel uses to decide what to offer.",
    ),
    # ---------------------------------------------------------------- audio
    "music": _g(
        "agent_ops",
        "removeMusic setMusicLevel setMusicVolume setBackgroundMusicLevel",
        ("remove_music", "set_mix"),
    ),
    "music_swap": _g(
        "agent_unsupported",
        "setMusic setMusicWindow setBackgroundMusic",
        reason="A phone-rendered edit keeps the song it was rendered with: the authored phone "
        "compile only re-plays the pinned music asset, a guided phone plan refuses a music lane "
        "(phone_guided_plan, musicBed) and the Talking and Voiceover Saves have no music "
        "section. Chat can change the music level or remove it (set_mix, remove_music).",
    ),
    # KRI-1394: the Sounds tab's Save section (`user_song`). A drag's move step writes the same
    # start or end as setUserSongStart / setUserSongEnd.
    "creator_song": _g(
        "agent_ops",
        "setUserSongVolume setUserSongStart setUserSongEnd moveSongStart moveSongEnd "
        "removeUserSong",
        ("set_user_song",),
        fields=("volume", "window_start_s", "window_end_s", "remove"),
    ),
    "creator_song_audition": _g(
        "not_an_edit",
        "beginSongStartDrag endSongStartDrag beginSongEndDrag endSongEndDrag "
        "previewLipsyncSongTrim",
        reason="Drag brackets (one undo step, the song audition) and the lip-sync trim preview "
        "change nothing until the move or apply they bracket.",
    ),
    "creator_song_lipsync_trim": _g(
        "agent_unsupported",
        "applyLipsyncSongTrim",
        reason="A lip-sync song's range is trimmed by cutting the video at the song-matched "
        "points the phone plans (NativeLipsyncSongTrim.plan); the Save locks a lip-sync window "
        "(user_song_lipsync_locked), so set_user_song refuses it and no op plans those cuts.",
    ),
    "sound_effects": _g(
        "agent_ops",
        "addSoundEffect removeSoundEffect setSoundEffectGain setSoundEffectTiming "
        "setSoundEffectTrim resetSoundEffectTrim",
        ("add_sfx", "patch_sfx", "remove_sfx"),
    ),
    # ----------------------------------------------------- overlays and motion
    "media_overlays": _g(
        "agent_ops",
        "removeMediaOverlay setMediaOverlayDisplayMode setMediaOverlayPosition "
        "setMediaOverlayScale setMediaOverlayTiming pasteImage",
        ("add_overlay", "patch_overlay", "remove_overlay", "set_overlay_display"),
    ),
    # KRI-1394: z orders peers of one renderer pass (setLayerZ writes a dense `z`): the picture
    # cards on top of a Talking speaker and the uploaded picture blocks. Text is its own pass
    # above both on the phone, so moving a text layer changes nothing there.
    "layer_order": _g(
        "agent_ops",
        "moveLayer reorderLayer moveVisualLayer setMediaOverlayZIndex",
        ("set_layer_order",),
        fields=("layer", "z", "direction"),
    ),
    "visual_blocks_and_motion": _g(
        "agent_unsupported",
        "addMotionComposition removeVisualBlock setVisualBlockDisplayMode "
        "setVisualBlockOverlayLayout setVisualBlockPreset setVisualBlockTiming "
        "setVisualBlockTransform setVisualEditorStyle setVisualTiming setMotionSceneTiming",
        reason="Motion scenes have no phone render lane: validate_phone_pilot_recipe refuses "
        "them pending device parity and the authored phone compile refuses motion_scenes, so "
        "the motion block ops are not in the phone's op families. Picture blocks are placed, "
        "timed and styled by hand; chat only removes one (remove_visual_media, clip_add_remove).",
    ),
    "camera_effects": _g(
        "agent_unsupported",
        "addCameraPulse setCameraEffectEasing setCameraEffectIntensity setCameraEffectTiming",
        reason="The phone renderer refuses camera pulses pending device parity "
        "(validate_phone_pilot_recipe, phone_authored_timeline) and a device edit closes "
        "camera_effects. A cloud-rendered edit still takes add/patch/remove_camera_effect.",
    ),
    "drawing": _g(
        "agent_unsupported",
        "addDrawing appendDrawingStrokes recolorDrawing setDrawingDrawDuration setDrawingEntrance "
        "setDrawingExit setDrawingTiming setDrawingTransform previewDrawingInPlace",
        reason="New strokes are finger paths a chat request cannot describe. Recolouring, "
        "timing, the entrance and exit and moving an existing drawing have no op yet: the "
        "snapshot shows drawings read-only (count and windows).",
    ),
    "carousel": _g(
        "agent_unsupported",
        "setCarouselPosition setCarouselMomentPosition removeCarouselMoment",
        reason="The phone cannot render a carousel moment: NativeEditorRenderCompiler throws "
        "unsupportedLane(carousel), and the authored and Voiceover phone compiles refuse "
        "carousel_moment. set_carousel_moment has no portable compile handler.",
    ),
    # The Talk-to-camera background (KRI-1117). The scanner counts only `func` declarations, so the
    # session's read helpers (`canEditSpeakerBackground`, `speakerBackground`,
    # `speakerBackgroundChoices` are computed properties; `backgroundChoices(from:)` is `static`)
    # need no row. The agent reaches the same Save section through set_background/remove_background
    # (`speaker_background` in the editor commit), only while the editor capability is open.
    "speaker_background": _g(
        "agent_ops",
        "setSpeakerBackground setSpeakerBackgroundColor removeSpeakerBackground "
        "setSpeakerBackgroundWindows addSpeakerBackgroundWindow removeSpeakerBackgroundWindow "
        "setSpeakerFrame clearSpeakerFrame",
        (
            "set_background",
            "set_background_color",
            "remove_background",
            "set_background_sequence",
            "set_speaker_frame",
            "clear_speaker_frame",
        ),
    ),
    # Qendresa item 16: the speaker full screen in their own take before the background / corner.
    # The planner writes it from the creator's first ask (`background_media.full_screen_opening`);
    # the copilot ops keep it through every background change. KRI-1394: chat takes the opening
    # alone away (the full-screen window starting at 0, as removeFullScreenOpening does).
    "speaker_full_opening": _g(
        "agent_ops", "removeFullScreenOpening", ("remove_full_screen_opening",)
    ),
    # Qendresa item 18: aim a screen-recording background at its text (left / centre / right and a
    # zoom). Chat reaches it with `set_background_focus`; the getter is a read-only query.
    "background_focus": _g("agent_ops", "setSpeakerBackgroundFocus", ("set_background_focus",)),
    "speaker_frame_by_hand": _g(
        "agent_ops",
        "setSpeakerFrameCenter setSpeakerFrameWidth resetSpeakerFrame",
        ("set_speaker_frame",),
        fields=("selector", "frame", "preset"),
        reason="Selected occurrences accept custom frame geometry and the inherit preset. "
        "The legacy canvas-global overload still accepts named presets only in chat.",
    ),
    # KRI-1394: the Save's `orientation` / `landscape_fit`, each axis where the editor opens it.
    "video_shape": _g(
        "agent_ops",
        "setVideoShape",
        ("set_video_shape",),
        fields=("orientation", "landscape_fit"),
    ),
    # --------------------------------------------------------------- history
    # KRI-1384: every chat edit turn stores its pre-edit lanes; undo_last_edit puts them back
    # while the editor still holds what the turn wrote (app.services.kria_editor_undo), offered
    # to app builds at chat_undo_min_client_protocol.
    "history": _g("agent_ops", "undo", ("undo_last_edit",)),
    "redo": _g(
        "agent_unsupported",
        "redo",
        reason="Redo after a chat undo is the editor's own redo stack; an undo turn stores no "
        "restore record, so chat cannot redo or undo twice. repeat_last_edit replays the web "
        "client's local op log, which iPhone chat does not have.",
    ),
    # The creator's approval of a generated image stays native (the preview review). Chat places
    # one the creator already accepted: `overlays.asset_pool` lists only those, and a Save pins the
    # image before it accepts the card (`generated_overlay_pool`).
    "generated_overlay_approval": _g(
        "agent_ops",
        "applyOverlayProposal",
        ("add_overlay",),
        fields=("asset_id",),
    ),
    "overlay_planning_and_queries": _g(
        "not_an_edit",
        "requestOverlayPlan containsOverlayProposal overlayBaseTime overlayOutputTime "
        "overlayDocumentFingerprint",
        reason="Read-only proposal planning, identity checks and editor clock conversion.",
    ),
    # ----------------------------------------------------------- not an edit
    "lifecycle_and_sync": _g(
        "not_an_edit",
        "load save reconcileOnOpen reconcilePlaybackState needsReload isBehindActiveJob "
        "loadForEditorOpen setObserversVisible "
        "openOrWarm claimWarm cancelWarm releaseWarmForMemoryPressure "
        "beginPreviewPresentation endPreviewPresentation previewFrameWasShown "
        "retainAdmittedPreview "
        "adoptLatestJob adoptActiveJobFromThread switchToLatestJob keepEditingCurrentJob "
        "synchronizePromptRevision resumeRefreshSync refreshDeviceRender retryRender "
        "retryDeviceRender retryPreview retryPreviewRefresh rebaseCleanDraft "
        "applyPreviewVariant exportEditorState exportEditorStateSyncingDraft "
        "exportDisplayedSourcePreview videoDownloadRoute showDeviceOutput useDeviceRendering "
        "useMediaUploads deviceRenderPresentationChanged installFinishedRenderPlayer "
        "handleMediaServicesReset handlePlayerItemFailure prepareSourcePreview "
        "prepareFixtureSourcePreview prepareGamedayFixture addGamedayFixtureClip "
        "saveGamedayFixture replaceDocumentForTesting originalsNeedingRelink relinkOriginal "
        "suspendEditorImports resumeEditorImports resumePendingEditorPlacements "
        "dismissPendingEditorImport retryPendingEditorImport refreshVisualLibrary "
        "pollVisualLibrary retryLibraryVisual loadSoundEffectCatalog end set "
        "auditPreviewWindow configureClipAudioForTesting configureReplacementSourcesForTesting "
        "discardPlaybackForNewJobForTesting finishScrubFramesForTesting "
        "setResolvedSourcesForTesting setPendingAddAdmissionForTesting previewSourceURLForTesting",
        reason="Loading, saving, rendering, importing and test fixtures change no creative lane.",
    ),
    "background_tool_navigation": _g(
        "not_an_edit",
        "requestBackgroundTool",
        reason="Opens the Background tool panel; the edit itself is the actions in it.",
    ),
    "playback_and_selection": _g(
        "not_an_edit",
        "togglePlayback pausePlayback seek select selectClip selectSplitPanel cycleSelection "
        "selectedObject inspectorSelection inspectorRoute previewSelectionBounds timelineTime "
        "timelineX "
        "beginTransaction endTransaction beginEditTransaction endEditTransaction "
        "beginDirectManipulation endDirectManipulation transactDocument markDirty isDirty "
        "cancelTimingGesture setTimelinePinching",
        reason="Playback, selection and the undo transaction brackets are not creative edits.",
    ),
    "capability_and_read_queries": _g(
        "not_an_edit",
        "canEdit canEditCaptionAnimation canEditOperation capability capabilityReason "
        "operationCapability operationCapabilityReason readOnlyReason cropBlockedReason "
        "cropSourceFrame speedBlockedReason sourceWindowBlockedReason sourceWindowContext "
        "sourceWindowLength sourceWindowSlot sourceWindowExplainsOnTap effectiveSourceRate "
        "canAdjustMotionSpeed "
        "motionRuntimeMismatchReason isMotionSceneReadOnly isClipAudioOn isCaptionUnitEdited "
        "captionAnimationPhases captionParkTime captionTimelineRange captionTimelineRanges "
        "cameraEffectEasing clipSlotClearValue drawing drawingCapMessage pasteUnavailableMessage "
        "soundEffectPreviewURL visualLayerSelections visualRaw footageCrop footagePlaybackRate "
        "textElement textAnchor captionPosition captionStyleSize clipTilt tiltBlockedReason "
        "tiltBackdropIsMissing "
        "canRotateVisual canStyleVisual speakerBackgroundFocus canEditSpeakerFrame "
        "speakerEditingFrame speakerFrame speakerFrameRect speakerSelection speakerViewport "
        "usesScopedSpeakerFrame ownsTimingGesture",
        reason="Read-only queries the UI uses to decide what to offer.",
    ),
}


def grouped_actions() -> dict[str, str]:
    """Action name to its group; raises when one name sits in two groups."""
    seen: dict[str, str] = {}
    for name, group in PARITY_GROUPS.items():
        for action in group.actions:
            if action in seen:
                raise ValueError(f"{action} is in both {seen[action]} and {name}")
            seen[action] = name
    return seen


__all__ = ["PARITY_GROUPS", "ParityGroup", "grouped_actions"]
