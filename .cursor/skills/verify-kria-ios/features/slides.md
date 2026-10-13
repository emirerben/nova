# Slide-post editing and export

Creators open a slide post, adjust its slides and visual style, save, and export
or share the resulting post without being routed into the video editor.

## Sub-features

- `slides-entry`: creation, recent-project drawer and Gallery reach the slide workspace.
- `slides-size`: Square/Fit and bar style reshape the preview and survive Save.
- `slides-edit`: text/look/drawing edits appear on the selected slide.
- `slides-export`: exporting an unsaved post saves it first, then reports progress.

## How to get to it (user POV)

- New chat → Slider opens the slide workspace.
- Open an existing slide project in the drawer, or Gallery → Open post.
- Choose a slide tile, then Text, Look, Size or Draw. Use Save, Export,
  Save to Photos or Share files. The Kria button opens the contextual editor.

## Driving it with XCUITest

Preconditions: Launch/Doctor. Selected tests use `-ui-testing-chat`, runtime v2,
and the existing slide/media/capability fixtures. They seed the Weekend trip
post for drawer/gallery paths and photos for editing/export.

```sh
"$KRIA_VERIFY_CONTROL" drive --run "$KRIA_VERIFY_RUN" --feature slides \
  SlidePostUITests/testSlidePostCreationIsTheRichLayoutThenCreateThenKriaPropose \
  SlidePostUITests/testRichLayoutFromTheDrawerWhenCapabilitiesFailToLoad \
  SlidePostUITests/testRichLayoutFromTheGalleryForEveryCapabilityState \
  SlidePostUITests/testSizeSheetSquareFitAllReshapesThePreviewAndSurvivesSave
```

- **Create/export.** `slidepost-preview` fits a 4:5 canvas. Tap
  `slidepost-export` → `slidepost-save-photos`; `slidepost-export-state` reports
  Saved 3 slides and `slidepost-subtitle` no longer says Unsaved changes.
- **Kria.** Tap `slidepost-openkria`, type `End on the view.` in Message Kria,
  send via `chat-send-message`, and tap `slidepost-apply` when offered.
- **Drawer/gallery.** Open Weekend trip or Gallery → Open post, including slow
  or failed capability fetches. The slide preview/tools appear; no video editor
  or Chat|Editor switch appears.
- **Size/save.** Tap `slidepost-tool-size` → `slidepost-size-chip-1x1` →
  `slidepost-size-fit-fit` → `slidepost-size-bars-white` →
  `slidepost-size-done`, then Save. The preview becomes square; the status line
  reopens the size sheet with Fit selected. Inspect its saved-state screenshot.
- **Look/drawing.** For these paths use a new run with
  `SlidePostUITests/testLooksAppearOnThePhotoBeforeSavingWhenDeviceCapabilityIsEnabled`
  or `SlidePostUITests/testDrawOnASlideLeavesInkThatSurvivesSaveAndSlideSwitching`.

## Gotchas

- The export test substitutes the Photos writer. Saved 3 slides proves its UI
  flow, not that three images exist in the real Photos library.
- Fixture save echoes the submitted draft; real account/server persistence,
  generated copy and AI reliability remain unverified.
- Creation, drawer and Gallery are distinct entry points. A passing direct
  workspace check does not cover the others or physical-device output.
- Missing slide/media capabilities must be recorded as unmet preconditions.
  Do not enable production flags or overwrite live account data to make a test pass.
