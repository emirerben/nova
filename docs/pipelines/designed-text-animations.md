# Designed text animations

The iPhone editor offers six animations on editable video text. They preserve the creator's content, font, size, color, placement and time window. The Animation tab selects an entrance or exit independently. Captions offer the same six choices in the caption Animation tab (All captions or one line, KRI-1007), which replaced Style > Text motion. A caption entrance stored by Text motion in `caption_meta.appearance.animation_phases` still renders and shows as the All captions choice until the creator changes it.

| Picker name | Wire value | Motion |
| --- | --- | --- |
| Silk Reveal | `silkReveal` | Lines lift through their own reveal masks. |
| Elastic Condensed | `elasticCondensed` | Shaped character regions expand with a small overshoot. |
| Focus Pull | `focusPull` | Blurred text settles into sharp focus. |
| Word Relay | `wordRelay` | Words occupy the same anchor one at a time. |
| Ribbon Wipe | `ribbonWipe` | Contrast ribbons reveal each line. |
| Echo Stack | `echoStack` | Outlined copies converge on the title. |

## Contracts

`app/agents/_schemas/text_animation_phases.py` owns the edge values. `generate_native_text_contract.py` generates the native enum. The mobile OpenAPI and shared generated types derive from the same schema.

Protocol 23 is required to read a phone recipe containing a designed edge. A protocol-22 reader receives `client_update_required`, rather than failing while decoding an unknown enum value.

`NativeDesignedTextPainter` prepares shaped text ink through `PortableTextVectorPainter`. The output-clock sampler serves native preview and export. It preserves the existing bitmap memory limit. The picker uses that same painter through `DesignedTextAnimationPreview`. The existing preview clock handles pause, lifecycle and Reduce Motion.

Every entrance and exit, designed or not, lasts `TEXT_ANIMATION_EDGE_SECONDS` (0.4 s) divided by the animation speed. The constant lives in `text_animation_phases.py` and is generated into the native `TextAnimationPhases.edgeSeconds`, so preview, on-device export and the Pillow and Skia renderers share one value. Designed edges ease with `cubic-bezier(0.23, 1, 0.32, 1)`, so they reach 90% of their travel by 0.15 s, before the 0.21 s a cubic pop takes. Picker cards replay every edge on one cycle: entrance, a 0.9 s hold, exit. Animation speed changes motion without changing text timing. Edges use at most half of a short text window. Word Relay uses the available text window to show every word. It changes the displayed word without changing or deleting the stored text.

Caption motion applies to complete caption lines under sentence display: the line's first interval enters, its last exits. Under word display every word is its own caption, so each word enters, exits and loops on its own window. Guided captions stamp every word row (`guided_caption_presentation`); a cue caption shown one word at a time keeps one `karaoke.single_word` layer and the engine plays its phases per word (`NativeSingleWordPainter.window`). Switching display or spoken-word highlighting never clears the choice. A Style > Text motion entrance, which only draws whole lines, moves into the all-captions default when word display or highlighting turns on. Source records and editing times remain unchanged.

Still-photo slide posts export images. Their text panel continues to hide animation controls. Read-only text retains its existing editing restrictions. These options animate content inside a video, rather than application labels or buttons.

## Renderers

The native compositor, Pillow renderer and Skia renderer recognize all six edge values. The backend renderers share `designed_text_motion.py` and retain their existing font shaping. They do not silently replace an unsupported edge with static text.

## Verification

Run from the task worktree:

```sh
swift test --package-path src/apps/ios/Packages/KriaMediaEngine --filter DesignedTextAnimationTests
bash scripts/check-api.sh test tests/routes/test_phone_editor_commit.py tests/routes/test_phone_subtitled_editor_commit.py tests/pipeline/test_phone_captions.py tests/pipeline/test_guided_caption_presentation.py -q
bash scripts/check-api.sh test tests/pipeline/test_text_overlay_skia.py -k designed_text_animations -q
make verify-overlays
```

Set `KRIA_TEXT_MOTION_EVIDENCE` to retain native frames and `six-text-animations.mp4`. `DesignedTextAnimationTests` compares decoded export frames with the native preview compositor, checks backward seeking and rejects static output as a motion control.

`NativeEditorInspectorUITests.testConnectedTextPresetAndAnimationPreviewControls` selects every designed treatment in the real iPhone picker. The overlay fixture `designed_text_animations.json` covers all six in the production-image verifier.
