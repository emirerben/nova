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
    }

    static let outroResourceName = "kria-outro-paper"

    public static func outroURL() -> URL? {
        Bundle.module.url(forResource: outroResourceName, withExtension: "mp4")
    }

    public static func watermarkURL(_ variant: Variant) -> URL? {
        Bundle.module.url(forResource: variant.resourceName, withExtension: "png")
    }

#if canImport(AVFoundation)
    /// Where the padded PNG's own origin lands, in Core Image's bottom-left
    /// coordinate space, for a canvas of this size.
    static func tileTransform(canvas: CGSize) -> CGAffineTransform {
        let scale = canvas.width / referenceWidth
        // The tile's bottom edge sits `tilePad` below the mark's bottom edge,
        // so the inset shrinks by the same padding the PNG carries.
        let bottomGap = (markBottomInset - tilePad) * (canvas.height / referenceHeight)
        return CGAffineTransform(scaleX: scale, y: scale)
            .concatenating(CGAffineTransform(translationX: (markLeft - tilePad) * scale,
                                             y: bottomGap))
    }

    static func watermarkLayer(canvas: CGSize, start: Double, end: Double,
                               variant: Variant) -> RecipeVideoLayer? {
        guard end > start, let url = watermarkURL(variant),
              let image = CIImage(contentsOf: url) else { return nil }
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
