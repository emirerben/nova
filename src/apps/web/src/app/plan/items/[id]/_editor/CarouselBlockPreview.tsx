"use client";

/**
 * CarouselBlockPreview — the editor-canvas mount point for the carousel-
 * moment block's window (Lane C, carousel-blocks train).
 *
 * `useVirtualPreview` gates BOTH video decks to a pause whenever the
 * playhead is inside a spliced carousel entry (see `showMapping`'s
 * `mapping.entry.kind !== "clip"` branch in useVirtualPreview.ts) — there is
 * no deck source for that window, only this component's render. EditorCanvas
 * mounts it on top of the (paused) decks whenever
 * `mapVirtualTime(virtualPreview.timeline, currentTimeS).entry.kind ===
 * "carousel"`.
 *
 * This is a thin re-export of `carousel-preview-impl/CarouselBlockPreviewImpl`
 * (Lane B's live CSS-3D renderer, built independently against this exact
 * props contract — see that file's docblock: "fixed by Lane C's placeholder
 * — do not rename"). Keeping this file as the stable import path means Lane
 * B's internals can keep changing without EditorCanvas/EditorShell ever
 * needing to know the implementation moved.
 *
 * `isPlaying` (the props contract's real transport-state flag — see
 * `video-sync.ts`) passes straight through: EditorCanvas already has the
 * editor's actual play/pause state in scope via its own `playing` prop, so
 * it's supplied there, not inferred here.
 */

export {
  default,
  type CarouselBlockPreviewImplProps as CarouselBlockPreviewProps,
} from "./carousel-preview-impl/CarouselBlockPreviewImpl";
