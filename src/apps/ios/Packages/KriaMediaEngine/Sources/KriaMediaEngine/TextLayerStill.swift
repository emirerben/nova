#if canImport(AVFoundation)
import Foundation
import CoreImage

/// KRI-1582: one still of text layers over a picture, drawn the way an export draws them.
///
/// The text is the compositor's own text pass (`RecipeVideoCompositor.paintText`) in its own working space
/// (linear sRGB in, sRGB out), so a design's soft shadow, scrim and sRGB blend look in the still exactly as in
/// the video. `background` is fitted the way a full-canvas clip is (aspect fill, centred); without one the
/// text sits on black.
public enum TextLayerStill {
    private static let context = CIContext(options: [
        .cacheIntermediates: false,
        .workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!,
        .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
    ])

    /// `fontURLs` maps each run's `fontAssetID` to its font file. `width` is the returned image's pixel width;
    /// its height keeps the canvas' aspect.
    public static func render(layers: [PortableTextLayer], fontURLs: [String: URL], over background: CGImage?,
                              canvas: Canvas, time: Double, width: Int) throws -> CGImage {
        let size = CGSize(width: canvas.width, height: canvas.height)
        let frame = CGRect(origin: .zero, size: size)
        guard size.width > 0, size.height > 0, width > 0 else { throw MediaEngineError.exportUnavailable }
        var base = CIImage(color: .black).cropped(to: frame)
        if let background {
            let picture = CIImage(cgImage: background)
            let scale = max(size.width / picture.extent.width, size.height / picture.extent.height)
            let fitted = picture.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            let centred = fitted.transformed(by: CGAffineTransform(
                translationX: (size.width - fitted.extent.width) / 2 - fitted.extent.minX,
                y: (size.height - fitted.extent.height) / 2 - fitted.extent.minY))
            base = centred.cropped(to: frame).composited(over: base)
        }
        let active = try layers.filter { time >= $0.start && time < $0.end }
            .map { try RecipeTextLayer.make($0, assetURLs: fontURLs, canvas: size) }
        let painted = try RecipeVideoCompositor.paintText(active, over: base, canvas: frame, time: time)
        let shrink = CGFloat(width) / size.width
        let output = painted.cropped(to: frame).applyingFilter("CILanczosScaleTransform", parameters: [
            kCIInputScaleKey: shrink, kCIInputAspectRatioKey: 1,
        ])
        let extent = CGRect(x: 0, y: 0, width: CGFloat(width), height: (size.height * shrink).rounded(.down))
        guard let image = context.createCGImage(output, from: extent, format: .RGBA8,
                                                colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!) else {
            throw MediaEngineError.exportFailed
        }
        return image
    }
}
#endif
