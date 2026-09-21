import Foundation
#if canImport(CoreImage)
import CoreGraphics
import CoreImage

/// Main-track stills are prepared once, on encoded sRGB values, so they match
/// the cloud's guided image moments (`guided_story._render_image_moment`).
public enum StillFrame {
    /// Transparent photos are matted over opaque black, exactly as the cloud
    /// flattens them before FFmpeg. Core Graphics blends the encoded values; a
    /// Core Image composite in the compositor's linear working space would
    /// brighten every soft edge, and an unflattened still would let the outgoing
    /// clip show through its transparent areas during a crossfade.
    public static func flattened(_ image: CGImage) throws -> CGImage {
        switch image.alphaInfo {
        case .none, .noneSkipFirst, .noneSkipLast: return image
        default: break
        }
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(data: nil, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: 0,
                                      space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { throw MediaEngineError.exportUnavailable }
        let bounds = CGRect(x: 0, y: 0, width: image.width, height: image.height)
        context.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
        context.fill(bounds)
        context.draw(image, in: bounds)
        guard let flat = context.makeImage() else { throw MediaEngineError.exportUnavailable }
        return flat
    }

    /// The guided `supporting_card`: the whole photo fitted inside a black card
    /// 82% of the canvas wide and 72% tall, centered over a blurred cover of the
    /// same photo. Rendered once to a canvas-sized bitmap so the blur never runs
    /// per frame. `image` is upright with its origin at zero.
    public static func supportingCard(_ image: CIImage, canvas: CGSize) throws -> CIImage {
        let frame = CGRect(origin: .zero, size: canvas)
        let size = image.extent.size
        guard size.width > 0, size.height > 0, canvas.width > 0, canvas.height > 0 else { throw MediaEngineError.exportUnavailable }
        func placed(scale: CGFloat, in rect: CGRect) -> CIImage {
            image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
                .transformed(by: CGAffineTransform(translationX: rect.midX - size.width * scale / 2, y: rect.midY - size.height * scale / 2))
        }
        // FFmpeg's boxblur=30:2 is two 61-tap boxes: a Gaussian of sigma 24.9.
        let background = placed(scale: max(canvas.width / size.width, canvas.height / size.height), in: frame)
            .cropped(to: frame).clampedToExtent()
            .applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: 24.9]).cropped(to: frame)
        // The cloud overlays at even pixel offsets from the top-left.
        let cardSize = CGSize(width: (canvas.width * 0.82).rounded(.down), height: (canvas.height * 0.72).rounded(.down))
        let left = CGFloat(Int((canvas.width - cardSize.width) / 2) & ~1), top = CGFloat(Int((canvas.height - cardSize.height) / 2) & ~1)
        let card = CGRect(x: left, y: canvas.height - top - cardSize.height, width: cardSize.width, height: cardSize.height)
        let photo = placed(scale: min(card.width / size.width, card.height / size.height), in: card).cropped(to: card)
        let composed = photo.composited(over: CIImage(color: .black).cropped(to: card)).composited(over: background)
        // Blur and blend on encoded values, as FFmpeg does.
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let rendered = CIContext(options: [.workingColorSpace: space]).createCGImage(composed, from: frame, format: .RGBA8, colorSpace: space) else {
            throw MediaEngineError.exportUnavailable
        }
        return CIImage(cgImage: rendered)
    }
}
#endif
