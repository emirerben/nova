import CoreGraphics
import Foundation
import KriaMediaEngine

/// A slide's photo or video turned to any angle about the canvas centre (KRI-1138). Mirrors the server's
/// `SlideTilt` (`app/schemas/slide_post.py`, math in `app/pipeline/slide_post/tilt.py`).
///
/// `rotationDeg` is clockwise-positive in -180...180 (the clip tilt, sticker and text convention). Only the media
/// turns: text, ink, stickers and the watermark stay upright above it. What the turn uncovers shows `backdrop`,
/// the engine's `ClipBackdrop` (blank is black, `color` a `#RRGGBB`, `blur` a blurred cover-fit copy of the same
/// media). It is a SIBLING of `framing` in `SlidePostEdits`: a build that predates it leaves the key out and the
/// server's "missing means keep" rule protects the stored value. Upright is stored as no tilt at all.
struct SlidePostTilt: Codable, Equatable, Sendable {
    static let maxDegrees = 180.0

    var rotationDeg: Double
    var backdrop: ClipBackdrop

    enum CodingKeys: String, CodingKey { case rotationDeg = "rotation_deg"; case backdrop }

    init(rotationDeg: Double, backdrop: ClipBackdrop = .blank) {
        self.rotationDeg = Self.wrapped(rotationDeg); self.backdrop = backdrop
    }
    /// Strict, like the drawing: dropping a tilt this build cannot read would make the next save send `null` and
    /// erase a turn the server still holds. The server validates it, so a failure here means a contract break.
    init(from decoder: Decoder) throws {
        // The server's model is `extra="forbid"`: a key this build does not know is a newer contract, and decoding around it
        // would drop it on the next save.
        let every = try decoder.container(keyedBy: AnyKey.self)
        guard every.allKeys.allSatisfy({ ["rotation_deg", "backdrop"].contains($0.stringValue) }) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unknown slide tilt field"))
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let degrees = try c.decode(Double.self, forKey: .rotationDeg)
        guard degrees.isFinite, abs(degrees) <= Self.maxDegrees else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Invalid slide tilt"))
        }
        rotationDeg = degrees
        backdrop = try c.decodeIfPresent(ClipBackdrop.self, forKey: .backdrop) ?? .blank
    }

    private struct AnyKey: CodingKey {
        let stringValue: String
        var intValue: Int? { nil }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    /// A turn of 0 draws nothing, so it is stored as "no tilt" (the server does the same).
    var isUpright: Bool { rotationDeg == 0 }

    /// Any angle folded into -180...180 (190 is -170).
    static func wrapped(_ degrees: Double) -> Double {
        guard degrees.isFinite else { return 0 }
        var folded = degrees.truncatingRemainder(dividingBy: 360)
        if folded > 180 { folded -= 360 } else if folded < -180 { folded += 360 }
        return folded
    }

    /// `#RRGGBB` as straight sRGB 0...1, or nil for any other string.
    static func rgb(_ hex: String) -> (red: Double, green: Double, blue: Double)? {
        let digits = Array(hex.utf8)
        guard digits.count == 7, digits[0] == UInt8(ascii: "#"),
              digits[1...].allSatisfy({ ($0 >= 48 && $0 <= 57) || ($0 >= 65 && $0 <= 70) || ($0 >= 97 && $0 <= 102) }),
              let value = UInt32(String(decoding: digits[1...], as: UTF8.self), radix: 16) else { return nil }
        return (Double((value >> 16) & 0xFF) / 255, Double((value >> 8) & 0xFF) / 255, Double(value & 0xFF) / 255)
    }
}

/// The geometry the phone shares with the cloud, pinned by `tests/fixtures/slide-tilt-parity/v1.json`.
///
/// The canvas is the slide's output canvas in px (origin top-left, y down). The MEDIA is the photo's (cropped) region
/// scaled the way the slide frames it (cover for Fill, whole for Fit), centred on the canvas; for Fill it is larger
/// than the canvas on one axis, so a turned photo keeps showing the pixels that rotate into view.
enum SlidePostTiltGeometry {
    static func mediaSize(imageSize: CGSize, crop: NormalizedSourceRect?, canvas: CGSize, fit: Bool) -> CGSize {
        guard imageSize.width > 0, imageSize.height > 0, canvas.width > 0, canvas.height > 0 else { return canvas }
        let region = crop ?? NormalizedSourceRect(x: 0, y: 0, width: 1, height: 1)
        let regionW = region.width * Double(imageSize.width), regionH = region.height * Double(imageSize.height)
        guard regionW > 0, regionH > 0 else { return canvas }
        let xScale = Double(canvas.width) / regionW, yScale = Double(canvas.height) / regionH
        let scale = fit ? min(xScale, yScale) : max(xScale, yScale)
        return CGSize(width: regionW * scale, height: regionH * scale)
    }

    /// Axis-aligned box of a `size` rectangle turned by `degrees` (ffmpeg's `rotw` / `roth`).
    static func rotatedBounds(_ size: CGSize, degrees: Double) -> CGSize {
        let radians = degrees * .pi / 180
        let cosine = abs(cos(radians)), sine = abs(sin(radians))
        return CGSize(width: size.width * cosine + size.height * sine, height: size.width * sine + size.height * cosine)
    }

    /// Canvas-px positions of the media's top-left, top-right, bottom-right, bottom-left corners after a clockwise
    /// turn about the canvas centre. y points down, so the plain rotation matrix with a positive angle turns clockwise.
    static func rotatedCorners(_ size: CGSize, degrees: Double, canvas: CGSize) -> [CGPoint] {
        let radians = degrees * .pi / 180
        let cosine = cos(radians), sine = sin(radians)
        let cx = Double(canvas.width) / 2, cy = Double(canvas.height) / 2
        let w = Double(size.width) / 2, h = Double(size.height) / 2
        return [(-w, -h), (w, -h), (w, h), (-w, h)].map { dx, dy in
            CGPoint(x: cx + dx * cosine - dy * sine, y: cy + dx * sine + dy * cosine)
        }
    }

    /// True when the turned media still covers every canvas pixel, so no backdrop shows.
    static func coversCanvas(_ size: CGSize, degrees: Double, canvas: CGSize) -> Bool {
        let radians = degrees * .pi / 180
        let cosine = cos(radians), sine = sin(radians)
        let epsilon = 1e-6
        for (x, y) in [(0.0, 0.0), (Double(canvas.width), 0.0), (Double(canvas.width), Double(canvas.height)), (0.0, Double(canvas.height))] {
            let dx = x - Double(canvas.width) / 2, dy = y - Double(canvas.height) / 2
            let mx = dx * cosine + dy * sine, my = -dx * sine + dy * cosine
            if abs(mx) > Double(size.width) / 2 + epsilon || abs(my) > Double(size.height) / 2 + epsilon { return false }
        }
        return true
    }

    /// Core Image's y axis points up, so a clockwise-on-screen angle is negated there.
    static func coreImageAngle(degrees: Double) -> CGFloat { CGFloat(-degrees * .pi / 180) }
}

extension SlidePostFraming {
    /// The tilt the slide actually renders with. A turn of 0 is no tilt.
    static func effectiveTilt(for slide: SlidePostSlide) -> SlidePostTilt? {
        guard let tilt = slide.edits?.tilt, !tilt.isUpright else { return nil }
        return tilt
    }
}
