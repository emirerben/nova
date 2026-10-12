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

/// How a photo sits in the post canvas (KRI-606). nil = fill: the (cropped) photo covers the canvas and its edges
/// are trimmed. A value shows the whole (cropped) photo and fills the rest with bars of this kind. Mirrors
/// `SlideEdits.fit` on the server; it is a sibling of `framing`, not part of it, so a build that predates it
/// leaves the key out and the server's "missing means keep" rule protects the value.
enum SlidePostFit: String, Codable, CaseIterable, Sendable {
    case blur, black, white
    var label: String { rawValue.capitalized }

    /// Full-resolution Gaussian sigma of the blur bars, in canvas pixels. The server blurs a quarter-size copy
    /// with sigma 20 (`FIT_BLUR_SIGMA_PX = 80`); pinned by `fit_blur_sigma_px` in the parity fixture.
    static let blurSigmaPx: CGFloat = 80
}

extension SlidePostFraming {
    static let landscape = CGSize(width: 1920, height: 1080)

    /// Only TikTok photo posts may carry a landscape slide; Instagram keeps one shape for every slide.
    static func allowsLandscape(profile: String) -> Bool { profile == "tiktok_photo" }

    /// The pixel canvas (at scale 1) a slide exports to: its own shape (KRI-44), else the post's size (KRI-606).
    static func canvas(for slide: SlidePostSlide, profile: String, aspect: String?) -> CGSize {
        canvas(orientation: slide.edits?.framing?.orientation ?? .portrait, profile: profile, aspect: aspect)
    }

    static func canvas(orientation: Orientation, profile: String, aspect: String?) -> CGSize {
        if orientation == .landscape, allowsLandscape(profile: profile) { return landscape }
        return SlidePostSize.canvas(profile: profile, aspect: aspect)
    }

    /// The crop the slide actually renders with. Video slides are never cropped in this version.
    static func effectiveCrop(for slide: SlidePostSlide) -> NormalizedSourceRect? {
        slide.kind == "image" ? slide.edits?.framing?.sourceCrop : nil
    }

    /// The bars treatment the slide actually renders with. Photos only: a video slide always fills the frame.
    static func effectiveFit(for slide: SlidePostSlide) -> SlidePostFit? {
        slide.kind == "image" ? slide.edits?.fit : nil
    }

    /// Where the FULL image is drawn so the crop region cover-fills the canvas (top-left canvas pixels), or, with
    /// `fit`, sits whole inside it. `crop == nil` is the plain centred placement. Pinned against
    /// `tests/fixtures/slide-framing-parity/v1.json` and `cover_placement` / `fit_placement` in `app/kria/framing.py`.
    static func placement(imageSize: CGSize, crop: NormalizedSourceRect?, canvas: CGSize, fit: Bool) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0, canvas.width > 0, canvas.height > 0 else {
            return CGRect(origin: .zero, size: canvas)
        }
        let region = crop ?? NormalizedSourceRect(x: 0, y: 0, width: 1, height: 1)
        let regionW = region.width * Double(imageSize.width), regionH = region.height * Double(imageSize.height)
        guard regionW > 0, regionH > 0 else { return CGRect(origin: .zero, size: canvas) }
        let xScale = Double(canvas.width) / regionW, yScale = Double(canvas.height) / regionH
        let scale = fit ? min(xScale, yScale) : max(xScale, yScale)
        let w = Double(imageSize.width) * scale, h = Double(imageSize.height) * scale
        let centerX = (region.x + region.width / 2) * w, centerY = (region.y + region.height / 2) * h
        return CGRect(x: Double(canvas.width) / 2 - centerX, y: Double(canvas.height) / 2 - centerY, width: w, height: h)
    }
}
