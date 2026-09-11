# KRI-29 implementation coverage ledger

Rollout: **disabled**. No row has passed its full release gate. An iPhone 13 Pro
[effect pilot](iphone13pro-effects.md) has passed preview-frame requests and
six-second exports for 18 text cases. The production group baseline
is `capability-baseline.json`; this ledger expands the source catalogs at the
implementation base into concrete work. A native primitive or unit fixture is
not a completed creator flow. Every row needs initial creation, editor revision,
backward seeking, preview/export parity, interruption/recovery, and physical-device
measurement. Admin-only templates and disabled experiments remain excluded.

## Text

All shared editor effect names now have native implementations and compiler paths:
`bounce`, `dissolve-out`, `fade-in`, `handwriting`, `ink-reveal`, `karaoke-line`, `none`, `pop-in`, `scale-up`, `slide-down`, `slide-in`, `slide-up`, `smooth-type`, `staggered-slice`, `static`, `stream-in`, `typewriter`.

Font layout, anchor/rotation, tracking, line spacing, case conversion, solid fills,
linear gradients, outline, shadow, and glow have staged native support. Full
font/language/style comparisons remain open. Sources:
`app/agents/_schemas/text_element.py`, `app/pipeline/portable_text_layout.py`,
and `KriaMediaEngine/PortableTextDrawing.swift`.

| Additional text treatment | Current boundary | Required next coverage |
| --- | --- | --- |
| `lyric-line` | Native fade envelope and positioned text compiled | Full style and language comparisons |
| `pop_animated_suffix` | Compiles exact settled prefix/suffix geometry; invalid suffix retains normal pop. Passed iPhone export pilot | Full lyric sequences, styles and language coverage |
| `generative_sequence` | Fade-in/static/none/handwriting/ink-reveal compiled with fade tails and stable top-layer ordering | Other sequence effects, long windows, full scene comparisons |
| `giant-title-wipe` | Native vector camera/opacity for static, none, fade-in, scale-up, slide-up/down/in, pop-in, bounce, ink-reveal, lyric-line, typewriter, stream-in, karaoke-line, smooth-type and staggered-slice; 348 cloud frames plus preview/export/backward-seek tests pass | Handwriting and dissolve combinations; [physical pilot](giant-title.md) has successful exports with intermittent AVFoundation interruptions |
| Emoji prefix | Rejected | Licensed local image asset, placement, fallback |
| `highlight_word` / spans | Informational highlight metadata accepted without changing pixels, matching cloud; spans rejected | Per-span layout and paint semantics |
| Long windows | Not fully compared | Cloud frame ceilings, hold paths, seams and overlaps |

## Source catalogs and creator lanes

| Group | Exact source values / pointer | Native boundary |
| --- | --- | --- |
| Curated style sets | `ai_answer`, `aurora`, `bold_statement`, `candy_pop`, `default`, `film_mono`, `grotesk_clean`, `handwritten_rascal`, `high_fashion`, `iridescent`, `lifestyle_clean`, `lyric_karaoke_bold`, `lyric_line_calm`, `lyric_word_pop_punchy`, `ocean_drift`, `retro_90s`, `script_curls`, `sunset_gradient`, `travel_editorial`, `typewriter`, `word_reveal` (`assets/style_sets/style-sets.json`) | Ingredients staged; each applicable role/style combination still needs comparison |
| Looks | `faded_analog`, `golden_hour`, `none`, `olive_film`, `smoky_split_tone`, `stadium_diffusion` (`pipeline/look_presets.py`) | Guided compiler only allows `none` |
| Transitions | `crossfade`, `fade_black`, `fade_white`, `wipe_left`, `wipe_right` (`pipeline/transitions.py`), plus semantic hard/match cuts, speed ramps and curtain-close | Basic overlapping crossfade exists; guided planner currently rejects transitions |
| Semantic camera | `semantic_crop_pulse`, `sine_pulse` (`pipeline/camera_effects.py`) | Not ported |
| Custom filters | `boxblur`, `chromashift`, `colorbalance`, `colorchannelmixer`, `crop`, `curves`, `eq`, `fade`, `gblur`, `hflip`, `hue`, `noise`, `rotate`, `scale`, `setpts`, `tblend`, `unsharp`, `vflip`, `vignette`, `zoompan` (`pipeline/custom_effects.py`) | Not ported; combinations of up to six and timeline semantics required |
| Media cards | Image/video, pip/fullscreen, `pop_in`, `dissolve-out`, rotation/position/scale (`agents/_schemas/media_overlay.py`) | Not compiled; text dissolve does not implement card dissolve |
| Visual blocks | `montage`, `text_card`, `media`; cut/fade; solid/gradient/blur_previous/asset backgrounds; zoom/pan (`agents/_schemas/visual_block.py`) | Not compiled |
| Carousel | `cards_stack`, `cover_flow`, `flipbook`, `scale_sweep` (`pipeline/carousel/effects.py`) | Not ported |
| Motion presets | `card_stack`, `cloud_break`, `donut_text`, `evolving_type`, `film_strip`, `flow_field`, `kinetic_word`, `offer_swap`, `tag_stack` (`src/packages/motion-runtime/creator-blocks.catalog.json`) | Not ported; current and compatible persisted versions need coverage |
| Speech/captions | Subtitled, recorded narration, self-narration/talking head; caption styles and corrections (`pipeline/captions.py`, `services/smart_captions.py`) | Analysis/planning split and native caption compiler outstanding |
| Audio | Original, licensed music/bed, narration, SFX; gain, trimming, ducking, mixes | Basic gains/mix supported; planner/catalog/source-role integration outstanding |
| Mixed-media posts | Ordered image/video slide posts (`pipeline/slide_post/`) | Full creation/export contract outstanding |

## Cross-cutting combinations and release gates

- Guided source trims + every text effect/style + simultaneous context/narration labels.
- Captions + styled text + SFX + music + media overlays, including re-burn equivalents.
- Visual blocks + embedded/grouped text + media/audio policies.
- Looks + transitions + variable speed + source audio; portrait/landscape and phone orientation.
- Motion scenes/carousels + user media + all exposed controls and persisted versions.
- Local original bindings for footage, visual pool, overlays, and recorded narration;
  explicit cloud-recovery consent and relinking after missing files or another device.
- Final poster/publication side effects, retention/account deletion, upload renewal,
  Gallery/cross-device viewing, and revision-fenced retry/recovery.
- 30-fps preview, seek p95 <=250 ms, 60-second export <=120 seconds on iPhone 13
  and a current iPhone, recording memory/thermal state. No capability is enabled
  before its measurements; all in-scope rows must pass for KRI-29 completion.
