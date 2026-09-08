# KriaMediaEngine

Renderer-neutral media contracts and native iOS composition seams for Kria.

The package keeps project recipes, timeline math, asset fingerprints, proxy metadata, thumbnails, and
waveforms independent of SwiftUI. On Apple platforms, `AVPlayerPreviewComposer` builds an
`AVMutableComposition` clocked by `AVPlayer`, including retimed cuts, crossfade opacity ramps, animated
text, and independent original/music audio. `AVFoundationLocalExporter` writes a recoverable local
export through AVFoundation; its default contract is vertical 1080×1920 H.264/AAC. Unsupported features
are identified by `CapabilityNegotiator` for cloud fallback.

Use `RecipeJSON.encode` and `RecipeJSON.decode` for API payloads; they pin snake_case keys (including
acronym fields) instead of depending on process-wide `JSONEncoder` settings.

Pixel buffers are intentionally private to AVFoundation/Core Animation. Public APIs exchange URLs,
metadata, recipe values, and scalar waveform levels only.

```sh
swift test
```

The package targets iOS 18 and macOS 13 so deterministic recipe, fingerprint, state, and capability
tests can run without a simulator. Device tests should provide licensed media fixtures to the
AVFoundation proxy, thumbnail, waveform, preview, and export implementations.
