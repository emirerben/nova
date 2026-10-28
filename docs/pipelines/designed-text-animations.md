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

Animation speed changes motion without changing text timing. Edges use at most half of a short text window. Word Relay uses the available text window to show every word. It changes the displayed word without changing or deleting the stored text.

Caption motion applies to complete caption lines. Choosing it selects sentence display and turns spoken-word highlighting off. Choosing word display or turning highlighting back on removes that caption motion. Guided captions group their source word records into one render-only sentence so the entrance does not restart on every word. Source records and editing times remain unchanged.

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
