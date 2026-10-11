import CoreGraphics
import Foundation
import KriaMediaEngine

/// A slide's shape and crop (KRI-44). Mirrors the server's `SlideFraming`
/// (`app/schemas/slide_post.py`, math in `app/kria/framing.py`).
///
/// `sourceCrop` is normalized, top-left origin, in the UPRIGHT frame of the source (EXIF or
/// `preferredTransform` already applied) so the phone and the cloud crop the same pixels.
struct SlidePostFraming: Codable, Equatable, Sendable {
    enum Orientation: String, Codable, Sendable { case portrait, landscape }

    var orientation: Orientation = .portrait
    var sourceCrop: NormalizedSourceRect? = nil

    enum CodingKeys: String, CodingKey { case orientation; case sourceCrop = "source_crop" }

    init(orientation: Orientation = .portrait, sourceCrop: NormalizedSourceRect? = nil) {
        self.orientation = orientation; self.sourceCrop = sourceCrop
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // Tolerant: a shape this build does not know falls back to portrait, like the server's coercion.
        orientation = (try? c.decodeIfPresent(Orientation.self, forKey: .orientation)) ?? .portrait
        sourceCrop = try c.decodeIfPresent(NormalizedSourceRect.self, forKey: .sourceCrop)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(orientation, forKey: .orientation)
        try c.encodeIfPresent(sourceCrop, forKey: .sourceCrop)
    }

    /// True when it says nothing the default does not, so it can be stored as "no framing".
    var isDefault: Bool { orientation == .portrait && sourceCrop == nil }
}

extension SlidePostFraming {
    static let portraitTikTok = CGSize(width: 1080, height: 1920)
    static let portraitInstagram = CGSize(width: 1080, height: 1350)
    static let landscape = CGSize(width: 1920, height: 1080)

    /// Only TikTok photo posts may carry a landscape slide; Instagram keeps one shape for every slide.
    static func allowsLandscape(profile: String) -> Bool { profile == "tiktok_photo" }

    /// The pixel canvas (at scale 1) a slide exports to.
    static func canvas(for slide: SlidePostSlide, profile: String) -> CGSize {
        canvas(orientation: slide.edits?.framing?.orientation ?? .portrait, profile: profile)
    }

    static func canvas(orientation: Orientation, profile: String) -> CGSize {
        if orientation == .landscape, allowsLandscape(profile: profile) { return landscape }
        return profile == "instagram_carousel" ? portraitInstagram : portraitTikTok
    }

    /// The crop the slide actually renders with. Video slides are never cropped in this version.
    static func effectiveCrop(for slide: SlidePostSlide) -> NormalizedSourceRect? {
        slide.kind == "image" ? slide.edits?.framing?.sourceCrop : nil
    }

    /// Where the FULL image is drawn so the crop region cover-fills the canvas (top-left canvas pixels).
    /// `crop == nil` is the plain centred cover. Pinned against `tests/fixtures/slide-framing-parity/v1.json`
    /// and `cover_placement` in `app/kria/framing.py`.
    static func placement(imageSize: CGSize, crop: NormalizedSourceRect?, canvas: CGSize) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0, canvas.width > 0, canvas.height > 0 else {
            return CGRect(origin: .zero, size: canvas)
        }
        let region = crop ?? NormalizedSourceRect(x: 0, y: 0, width: 1, height: 1)
        let regionW = region.width * Double(imageSize.width), regionH = region.height * Double(imageSize.height)
        guard regionW > 0, regionH > 0 else { return CGRect(origin: .zero, size: canvas) }
        let scale = max(Double(canvas.width) / regionW, Double(canvas.height) / regionH)
        let w = Double(imageSize.width) * scale, h = Double(imageSize.height) * scale
        let centerX = (region.x + region.width / 2) * w, centerY = (region.y + region.height / 2) * h
        return CGRect(x: Double(canvas.width) / 2 - centerX, y: Double(canvas.height) / 2 - centerY, width: w, height: h)
    }
}
