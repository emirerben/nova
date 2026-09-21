import Foundation

#if canImport(AVFoundation)
import AVFoundation
import CoreGraphics
import CoreImage
import CoreMedia
#endif

/// Kria brand furniture on every edit this engine renders: the wordmark in the
/// bottom-left for the whole timeline, and the brand outro after the last clip.
///
/// This lives in the composition builder rather than in a post-export pass
/// because `AVFoundationLocalExporter` exports the very composition
/// `AVPlayerPreviewComposer` builds. Adding it here means the preview a creator
/// scrubs and the file they publish agree, and neither needs a second encode.
///
/// The placement and the two grey tones come from `brand/social/` at the repo
/// root; the assets in `Resources/` are copied there by its `build.py`, which
/// also measures them. See that README before changing a number here.
public enum KriaBranding {
    /// Authoring reference. Every measurement below is in this frame and scales
    /// with the real canvas, so a square or landscape export gets a
    /// proportional mark rather than a mispositioned one.
    static let referenceWidth: CGFloat = 1080
    static let referenceHeight: CGFloat = 1920

    static let markLeft: CGFloat = 60
    static let markWidth: CGFloat = 133
    static let markHeight: CGFloat = 59
    /// Distance from the bottom of the frame to the bottom of the mark.
    ///
    /// The approved position pins the mark's BOTTOM edge at y=1475, because the
    /// constraint it was chosen against -- the platforms' caption block -- is
    /// measured up from the bottom of the frame. Stated as a constant rather
    /// than derived from a top edge so that resizing the mark moves its top
    /// edge and leaves the corner where it was signed off.
    static let markBottomInset: CGFloat = 445
    /// Transparent margin baked into the PNG for the mark's shadow.
    static let tilePad: CGFloat = 30

    /// Grey tones. `mist` is the default; `graphite` exists for footage bright
    /// enough that a light mark disappears into it.
    public enum Variant: String, Sendable, CaseIterable {
        case mist, graphite

        var resourceName: String { "kria-watermark-\(rawValue)-standard" }
    }

    public struct Options: Equatable, Sendable {
        public var watermark: Bool
        public var outro: Bool
        public var variant: Variant

        public init(watermark: Bool = true, outro: Bool = true, variant: Variant = .mist) {
            self.watermark = watermark
            self.outro = outro
            self.variant = variant
        }

        public static let standard = Options()
        public static let none = Options(watermark: false, outro: false)

        /// What the phone declares to the server's export verifier.
        ///
        /// Only the outro changes the file's DURATION, which is the single
        /// thing the server checks, so the watermark deliberately does not
        /// affect this value. The server owns the seconds each identifier is
        /// worth — see `BRAND_TAIL_SECONDS` in app/kria/device_render.py.
        ///
        /// This is computed from what was REQUESTED, and it is only honest
        /// because a requested tail that cannot be composited is fatal rather
        /// than skipped — see `preflight(_:)`. Let any branch drop the outro
        /// silently and this string starts over-declaring, which the creator
        /// meets as an opaque "export duration mismatch" when the finished
        /// file is rejected on upload, long after the real cause.
        public var contractTail: String { outro ? "standard" : "none" }
    }

    static let outroResourceName = "kria-outro-paper"

    /// Bundle file names, in one place, because they are what the error for a
    /// missing resource reports — a failure the creator forwards should name
    /// the file to go looking for.
    static var outroFileName: String { "\(outroResourceName).mp4" }
    static func watermarkFileName(_ variant: Variant) -> String { "\(variant.resourceName).png" }

    public static func outroURL() -> URL? {
        Bundle.module.url(forResource: outroResourceName, withExtension: "mp4")
    }

    public static func watermarkURL(_ variant: Variant) -> URL? {
        Bundle.module.url(forResource: variant.resourceName, withExtension: "png")
    }

    /// Fail before compositing when a resource `options` asks for is not in
    /// the bundle, instead of quietly exporting without it.
    ///
    /// Branding is meant to be on every file that leaves the phone, so a
    /// missing asset is a build defect, not a condition to degrade through:
    /// dropping the watermark ships an unbranded edit, and dropping the outro
    /// makes `Options.contractTail` over-declare and the upload is rejected
    /// for a duration mismatch that names nothing useful.
    ///
    /// Reachable without a code change: an incremental `xcodebuild` can leave
    /// a stale `KriaMediaEngine_KriaMediaEngine.bundle` inside `Kria.app` that
    /// predates these assets while the freshly built top-level bundle carries
    /// them (hit 2026-09-22; deleting `Kria.app` and rebuilding cleared it).
    ///
    /// The lookups are parameters only so a test can exercise the missing case
    /// without tampering with `Bundle.module`.
    static func preflight(_ options: Options,
                          resolveWatermark: (Variant) -> URL? = KriaBranding.watermarkURL,
                          resolveOutro: () -> URL? = KriaBranding.outroURL) throws {
        if options.watermark, resolveWatermark(options.variant) == nil {
            throw MediaEngineError.missingBrandingResource(watermarkFileName(options.variant))
        }
        if options.outro, resolveOutro() == nil {
            throw MediaEngineError.missingBrandingResource(outroFileName)
        }
    }

#if canImport(AVFoundation)
    /// Where the padded PNG's own origin lands, in Core Image's bottom-left
    /// coordinate space, for a canvas of this size.
    static func tileTransform(canvas: CGSize) -> CGAffineTransform {
        // ONE scale for both the mark and its insets, taken from whichever axis
        // is more constrained. Scaling the mark by width while insetting it by
        // height silently diverges the moment the canvas is not 9:16 — the mark
        // grows with width on a short frame while its inset shrinks with height,
        // so it ends up oversized and creeping toward the edge. The canvas comes
        // from a server snapshot (recipes.py defaults to 1080x1920 but does not
        // enforce it), so that is reachable, not hypothetical.
        //
        // On 1080x1920 this is exactly 1 and the placement is unchanged.
        let scale = min(canvas.width / referenceWidth, canvas.height / referenceHeight)
        // The tile's own origin sits `tilePad` left of and below the mark's,
        // because the PNG carries that margin for its shadow.
        return CGAffineTransform(scaleX: scale, y: scale)
            .concatenating(CGAffineTransform(translationX: (markLeft - tilePad) * scale,
                                             y: (markBottomInset - tilePad) * scale))
    }

    /// - Returns: nil only for an empty time range. A PNG that is absent or
    ///   unreadable throws instead, because silently dropping the mark ships
    ///   an unbranded export — see `preflight(_:)`.
    static func watermarkLayer(canvas: CGSize, start: Double, end: Double,
                               variant: Variant) throws -> RecipeVideoLayer? {
        guard end > start else { return nil }
        guard let url = watermarkURL(variant), let image = CIImage(contentsOf: url) else {
            throw MediaEngineError.missingBrandingResource(watermarkFileName(variant))
        }
        // Drawn above text so the mark is never buried by a caption, and with
        // its alpha preserved so the shadow stays soft instead of matting to a
        // black box.
        return RecipeVideoLayer(
            trackID: nil, image: image, transform: tileTransform(canvas: canvas),
            start: start, end: end, fadeIn: 0, isPrimary: false,
            clipID: "kria-watermark", naturalSize: image.extent.size,
            overlayAboveText: true, overlayPreserveAlpha: true, visualOrder: 9_000)
    }

    /// Fill `canvas` with a source of `naturalSize`, matching how a clip covers
    /// the frame. The outro is authored 9:16, so this only does real work when
    /// the export canvas is not.
    static func coverTransform(naturalSize: CGSize, preferred: CGAffineTransform,
                               canvas: CGSize) -> CGAffineTransform {
        let rect = CGRect(origin: .zero, size: naturalSize).applying(preferred)
        let scale = max(canvas.width / max(abs(rect.width), 1),
                        canvas.height / max(abs(rect.height), 1))
        return preferred
            .concatenating(CGAffineTransform(translationX: -rect.minX, y: -rect.minY))
            .concatenating(CGAffineTransform(scaleX: scale, y: scale))
            .concatenating(CGAffineTransform(translationX: -abs(rect.width) * scale / 2,
                                             y: -abs(rect.height) * scale / 2))
            .concatenating(CGAffineTransform(translationX: canvas.width / 2,
                                             y: canvas.height / 2))
    }
#endif
}
