#if canImport(AVFoundation)
import Foundation
import CoreGraphics
import CoreImage

/// Skia-compatible device-space mask blur and byte-quantized tint.
enum NativeSkiaShadowPainter {
    private static let glyphTintKernel: Result<CIKernel, Error> = Result {
        let kernels = try CIKernel.kernels(withMetalString: """
        #include <CoreImage/CoreImage.h>
        using namespace metal;
        extern "C" { namespace coreimage {
        [[stitchable]] float4 kriaGlyphMaskTint(sampler mask, float4 colorBytes, destination dest) {
            float coverage = round(clamp(mask.sample(mask.transform(dest.coord())).a, 0.0, 1.0) * 255.0);
            return floor(colorBytes * (coverage + 1.0) / 256.0) / 255.0;
        }
        }}
        """)
        guard let kernel = kernels.first else { throw MediaEngineError.unsupportedCapability }
        return kernel
    }
    /// Skia's raster source-over truncates each destination product to bytes.
    /// Core Graphics rounds instead; many faint pen-path halos accumulate a
    /// visibly brighter result unless this step is preserved for every path.
    private static func composite(_ image: CGImage, in rect: CGRect, onto context: CGContext, bounds: CGRect) throws {
        guard let target = context.data,
              let data = image.dataProvider?.data,
              let source = CFDataGetBytePtr(data) else { throw MediaEngineError.exportFailed }
        let x = Int(rect.minX - bounds.minX), y = Int(bounds.maxY - rect.maxY)
        for row in 0..<Int(rect.height) {
            let src = UnsafeRawPointer(source.advanced(by: row * image.bytesPerRow))
            let dst = target.advanced(by: (y + row) * context.bytesPerRow + x * 4)
                .assumingMemoryBound(to: UInt32.self)
            for column in 0..<Int(rect.width) {
                let pixel = UInt32(littleEndian: src.loadUnaligned(fromByteOffset: column * 4, as: UInt32.self))
                let alpha = pixel >> 24
                if alpha == 0 { continue }
                let destination = UInt32(littleEndian: dst[column])
                let inverse = 256 - alpha
                // Multiply two byte lanes at once, with a spare byte between
                // them so products cannot carry into a neighbouring channel.
                let redBlue = (((destination & 0x00ff00ff) * inverse) >> 8) & 0x00ff00ff
                let greenAlpha = (((destination >> 8) & 0x00ff00ff) * inverse) & 0xff00ff00
                dst[column] = (pixel &+ redBlue &+ greenAlpha).littleEndian
            }
        }
    }

    static func draw(pathBounds: CGRect, blur: TextBlurLayer, context: CGContext,
        bounds: CGRect, rotation: CGAffineTransform, transform: CGAffineTransform,
        maxBitmapBytes: Int, opacity: Double, imageContext: CIContext, integerCompositing: Bool = false,
        drawMask: (CGContext) -> Void) throws {
        // SkBlurMaskFilterImpl::computeXformedSigma caps device-space sigma at 128.
        // This also bounds the off-canvas source mask needed for visible shadow pixels.
        let sigma = min(128, blur.sigma * hypot(transform.a, transform.b))
        // SkDraw::compute_mask_bounds caps the source-mask clip outset at 128.
        let window = max(1, Int(floor(sigma * 3 * sqrt(2 * Double.pi) / 4 + 0.5)))
        let border = window.isMultiple(of: 2) ? 3 * (window / 2) - 1 : 3 * ((window - 1) / 2)
        let padding = min(128, sigma >= 2 ? Double(border) : ceil(3 * sigma))
        let sourceClip = bounds.insetBy(dx: -padding, dy: -padding).integral
        let placement = CGAffineTransform(translationX: blur.dx, y: -blur.dy)
            .concatenating(rotation).concatenating(transform)
        let tint = TextInk(red: blur.color.red, green: blur.color.green, blue: blur.color.blue,
            alpha: floor(blur.color.alpha * pow(opacity, Double(blur.alphaPower)) * 255) / 255)
        // Skia blurs and blends each glyph mask separately. Combining the line
        // changes overlapping tails and accumulates different byte rounding.
        do {
            let maskBounds = pathBounds.applying(placement)
                .insetBy(dx: -0.5, dy: -0.5).integral.intersection(sourceClip)
            if maskBounds.isNull || maskBounds.isEmpty { return }
            guard maskBounds.width * maskBounds.height * 4 <= Double(maxBitmapBytes),
                  let mask = CGContext(data: nil, width: Int(maskBounds.width), height: Int(maskBounds.height),
                      bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { throw MediaEngineError.unsupportedCapability }
            mask.translateBy(x: -maskBounds.minX, y: -maskBounds.minY)
            mask.concatenate(placement)
            drawMask(mask)
            guard let image = mask.makeImage() else { throw MediaEngineError.exportFailed }
            var shadow = CIImage(cgImage: image)
            if sigma >= 2 {
                // CIBoxBlur uses a full kernel width, despite inputRadius's name.
                for size in [window, window, window.isMultiple(of: 2) ? window + 1 : window] {
                    shadow = shadow.applyingFilter("CIBoxBlur", parameters: ["inputRadius": Double(size)])
                }
            } else if sigma > 0 {
                shadow = shadow.applyingFilter("CIGaussianBlur", parameters: ["inputRadius": sigma])
            }
            let alphaByte = Int((tint.alpha * 255).rounded())
            func premultiplied(_ channel: Double) -> CGFloat {
                CGFloat((Int((channel * 255).rounded()) * alphaByte + 127) / 255)
            }
            let color = CIVector(x: premultiplied(tint.red), y: premultiplied(tint.green),
                z: premultiplied(tint.blue), w: CGFloat(alphaByte))
            guard let tinted = try Self.glyphTintKernel.get().apply(extent: shadow.extent,
                roiCallback: { _, rect in rect }, arguments: [shadow, color]) else { throw MediaEngineError.unsupportedCapability }
            shadow = tinted.transformed(by: CGAffineTransform(translationX: maskBounds.minX, y: maskBounds.minY))
            let crop = shadow.extent.intersection(bounds).integral
            if crop.isNull || crop.isEmpty { return }
            guard let output = imageContext.createCGImage(shadow, from: crop, format: .RGBA8,
                colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!) else { throw MediaEngineError.exportFailed }
            if integerCompositing {
                try composite(output, in: crop, onto: context, bounds: bounds)
            } else { context.draw(output, in: crop) }
        }
    }
}
#endif
