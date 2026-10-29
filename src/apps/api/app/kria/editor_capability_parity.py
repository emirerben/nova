"""Capability parity: every human editor action has an agent op, or says why not (29 Ekim).

The creator asked Kria for things a person does by hand in the editor (show a title one word at a
time, shuffle the clips) and the agent refused or failed each one. The agent's ops had grown by
request, not by what the editor can do. This table is the lesson in structure: each public
function of the phone editor session (``NativeEditorSession.swift``) belongs to exactly one group,
and a group is one of

* ``agent_ops``: the edit reaches the agent through these ops (names checked against the copilot's
  op set), or
* ``agent_unsupported``: a person can, the agent cannot yet, with the reason written down, or
* ``not_an_edit``: playback, selection, loading, saving and read-only queries.

``tests/kria/test_editor_capability_parity.py`` fails when the Swift file gains a public function
that no group names, when a group names one that is gone, and when an ``agent_ops`` group names an
op the copilot does not have. Adding an editor action therefore forces the question "can the
agent do this?" in the same change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParityGroup:
    kind: str  # "agent_ops" | "agent_unsupported" | "not_an_edit"
    actions: frozenset[str]
    ops: tuple[str, ...] = ()
    reason: str = ""


def _g(kind: str, actions: str, ops: tuple[str, ...] = (), reason: str = "") -> ParityGroup:
    return ParityGroup(kind, frozenset(actions.split()), ops, reason)


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
        "setCaptionStrokeWidth setCaptionAppearance setCaptionAnimationSpeed setCaptionPhase",
        (
            "edit_caption",
            "replace_caption_text",
            "set_caption_meta",
            "set_caption_timing",
            "set_caption_emphasis",
            "patch_text_appearance",
        ),
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
        "addLibraryVisual placeEditorSource removeVisualSelection",
        ("remove_clip", "add_unused_sources", "split_clip", "remove_visual_media"),
    ),
    "clip_look_and_transition": _g(
        "agent_ops",
        "setClipLookPreset updateClipLook setClipLookAdjustments setClipTransition "
        "setTransitionAcrossVideo",
        ("set_look_preset", "set_transition", "apply_custom_effect"),
    ),
    "clip_speed": _g(
        "agent_unsupported",
        "setMotionSpeed",
        reason="Playback speed of a clip has no agent op; the phone montage plays natural speed.",
    ),
    "clip_audio": _g(
        "agent_ops",
        "toggleClipAudio setOriginalAudioLevel setOriginalMixLevel",
        ("set_mix",),
    ),
    # ---------------------------------------------------------------- audio
    "music": _g(
        "agent_ops",
        "setMusic removeMusic setMusicWindow setMusicLevel setMusicVolume setBackgroundMusic "
        "setBackgroundMusicLevel",
        ("swap_music", "remove_music", "set_mix"),
    ),
    "creator_song": _g(
        "agent_unsupported",
        "setUserSongVolume setUserSongStart setUserSongEnd beginSongStartDrag moveSongStart "
        "endSongStartDrag beginSongEndDrag moveSongEnd endSongEndDrag previewLipsyncSongTrim "
        "applyLipsyncSongTrim removeUserSong",
        reason="The creator's own song window and volume are set on the phone; the agent has "
        "only the licensed-music ops (swap_music, set_mix).",
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
    "layer_order": _g(
        "agent_unsupported",
        "moveLayer reorderLayer moveVisualLayer setMediaOverlayZIndex",
        reason="Stacking order of overlapping layers has no agent op.",
    ),
    "visual_blocks_and_motion": _g(
        "agent_ops",
        "addMotionComposition removeVisualBlock setVisualBlockDisplayMode "
        "setVisualBlockOverlayLayout setVisualBlockPreset setVisualBlockTiming "
        "setVisualBlockTransform setVisualEditorStyle setVisualTiming setMotionSceneTiming",
        ("add_motion_block", "patch_motion_block", "remove_motion_block"),
    ),
    "camera_effects": _g(
        "agent_ops",
        "addCameraPulse setCameraEffectEasing setCameraEffectIntensity setCameraEffectTiming",
        ("add_camera_effect", "patch_camera_effect", "remove_camera_effect"),
    ),
    "drawing": _g(
        "agent_unsupported",
        "addDrawing appendDrawingStrokes recolorDrawing setDrawingDrawDuration setDrawingEntrance "
        "setDrawingExit setDrawingTiming setDrawingTransform previewDrawingInPlace",
        reason="Freehand strokes are drawn by hand; the agent has no stroke authoring op.",
    ),
    "carousel": _g(
        "agent_ops",
        "setCarouselPosition setCarouselMomentPosition removeCarouselMoment",
        ("set_carousel_moment",),
    ),
    "video_shape": _g(
        "agent_unsupported",
        "setVideoShape",
        reason="Portrait/landscape of a finished video is chosen on the phone; set_post_aspect "
        "covers slide posts only.",
    ),
    # --------------------------------------------------------------- history
    "history": _g("agent_ops", "undo redo", ("undo_last_edit", "repeat_last_edit")),
    # ----------------------------------------------------------- not an edit
    "lifecycle_and_sync": _g(
        "not_an_edit",
        "load save reconcileOnOpen reconcilePlaybackState needsReload isBehindActiveJob "
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
        "auditPreviewWindow",
        reason="Loading, saving, rendering, importing and test fixtures change no creative lane.",
    ),
    "playback_and_selection": _g(
        "not_an_edit",
        "togglePlayback pausePlayback seek select selectClip cycleSelection selectedObject "
        "inspectorSelection inspectorRoute previewSelectionBounds timelineTime timelineX "
        "beginTransaction endTransaction beginEditTransaction endEditTransaction "
        "beginDirectManipulation endDirectManipulation transactDocument markDirty isDirty",
        reason="Playback, selection and the undo transaction brackets are not creative edits.",
    ),
    "capability_and_read_queries": _g(
        "not_an_edit",
        "canEdit canEditCaptionAnimation canEditOperation capability capabilityReason "
        "operationCapability operationCapabilityReason readOnlyReason cropBlockedReason "
        "cropSourceFrame speedBlockedReason sourceWindowBlockedReason sourceWindowContext "
        "sourceWindowLength sourceWindowSlot effectiveSourceRate canAdjustMotionSpeed "
        "motionRuntimeMismatchReason isMotionSceneReadOnly isClipAudioOn isCaptionUnitEdited "
        "captionAnimationPhases captionParkTime captionTimelineRange captionTimelineRanges "
        "cameraEffectEasing clipSlotClearValue drawing drawingCapMessage pasteUnavailableMessage "
        "soundEffectPreviewURL visualLayerSelections visualRaw",
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
