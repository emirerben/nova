import Foundation
#if canImport(CoreImage)
import CoreImage
#endif

/// What shows behind a main-track clip where the clip does not cover the canvas: the corners a tilt
/// uncovers, or the bars around a letterboxed "fit" clip. Mirrors `app.kria.recipes.ClipBackdrop`
/// (`{"kind": "blank" | "color" | "blur", "color": "#RRGGBB"}`, `color` only for `color`).
public enum ClipBackdrop: Codable, Equatable, Sendable {
    /// Black, like the bars around a letterboxed clip. Drawn as nothing.
    case blank
    case color(String)
    /// A blurred cover-fit copy of the same clip frame.
    case blur

    private enum CodingKeys: String, CodingKey { case kind, color }
    private enum Kind: String, Codable { case blank, color, blur }

    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["kind", "color"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let hasColor = c.contains(.color)
        switch try c.decode(Kind.self, forKey: .kind) {
        case .blank where !hasColor: self = .blank
        case .blur where !hasColor: self = .blur
        case .color: self = .color(try c.decode(String.self, forKey: .color))
        default: throw RecipeError.invalidTimeline
        }
        try validate()
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .blank: try c.encode(Kind.blank, forKey: .kind)
        case .blur: try c.encode(Kind.blur, forKey: .kind)
        case .color(let hex): try c.encode(Kind.color, forKey: .kind); try c.encode(hex, forKey: .color)
        }
    }

    public func validate() throws {
        if case .color(let hex) = self, Self.components(hex) == nil { throw RecipeError.invalidTimeline }
    }

    /// Straight sRGB channels in 0...1 for `#RRGGBB`, or nil for any other string.
    static func components(_ hex: String) -> (red: Double, green: Double, blue: Double)? {
        let digits = Array(hex.utf8)
        guard digits.count == 7, digits[0] == UInt8(ascii: "#"),
              digits[1...].allSatisfy({ ($0 >= 48 && $0 <= 57) || ($0 >= 65 && $0 <= 70) || ($0 >= 97 && $0 <= 102) }),
              let value = UInt32(String(decoding: digits[1...], as: UTF8.self), radix: 16) else { return nil }
        return (Double((value >> 16) & 0xFF) / 255, Double((value >> 8) & 0xFF) / 255, Double(value & 0xFF) / 255)
    }
}

#if canImport(AVFoundation)
extension RecipeVideoCompositor {
    /// A blur is drawn from a quarter-size copy so a 1080x1920 frame costs a 270x480 blur.
    private static let backdropBlurScale: CGFloat = 0.25
    /// Matches `StillFrame.supportingCard`'s blur (FFmpeg `boxblur=30:2`, sigma 24.9 at 1080 wide).
    private static let backdropBlurSigma1080: CGFloat = 24.9

    /// The canvas-sized image drawn under the clip, or nil when nothing is drawn.
    static func backdropImage(_ backdrop: ClipBackdrop, source: CIImage, layer: RecipeVideoLayer,
                              canvas: CGRect, time: Double) -> CIImage? {
        switch backdrop {
        case .blank:
            return nil
        case .color(let hex):
            guard let rgb = ClipBackdrop.components(hex),
                  let space = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
            return CIImage(color: CIColor(red: rgb.red, green: rgb.green, blue: rgb.blue, alpha: 1, colorSpace: space) ?? .black)
                .cropped(to: canvas)
        case .blur:
            guard let size = layer.naturalSize else { return nil }
            var cover = layer
            cover.transform = AVPlayerPreviewComposer.transform(naturalSize: size, preferred: layer.preferredTransform,
                                                                canvas: canvas.size, media: .identity)
            cover.visualPlacement = nil
            let fitted = positionedSource(source, layer: cover, canvas: canvas, time: time).cropped(to: canvas)
            let small = fitted.transformed(by: CGAffineTransform(scaleX: backdropBlurScale, y: backdropBlurScale))
            let sigma = backdropBlurSigma1080 * canvas.width / 1080 * backdropBlurScale
            let blurred = small.clampedToExtent()
                .applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: sigma])
                .cropped(to: small.extent)
            return blurred.transformed(by: CGAffineTransform(scaleX: canvas.width / small.extent.width,
                                                             y: canvas.height / small.extent.height))
                .cropped(to: canvas)
        }
    }
}
#endif
