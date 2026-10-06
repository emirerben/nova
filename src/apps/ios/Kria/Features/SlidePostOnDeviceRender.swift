import ImageIO
import KriaMediaEngine
import SwiftUI
import UIKit

/// Renders an image-only slide post on the phone from the ORIGINAL photos: cover-fit centre-crop into the
/// profile canvas at 2x, then the same `SlidePostTextLayerView` the editor shows, drawn at real pixel size,
/// then the Kria watermark the server render also puts on every slide (KRI-472). No server render, no
/// polling. Anything it cannot reproduce faithfully (video, looks, unknown fonts, no photo URL) is
/// `supports == false` and takes the server path instead.
@MainActor enum SlidePostOnDeviceRender {
    typealias Fetch = SlidePostImageCache.Fetcher
    enum RenderError: Error { case undecodable, encodeFailed }

    static let outputScale: CGFloat = 2
    static let jpegQuality: CGFloat = 0.92

    /// Logical canvas in points (the server's output size at scale 1).
    static func canvas(for profile: String) -> CGSize {
        profile == "instagram_carousel" ? CGSize(width: 1080, height: 1350) : CGSize(width: 1080, height: 1920)
    }

    static func supports(_ draft: SlidePostDraft?, assets: [SlidePostAsset]) -> Bool {
        guard let draft, !draft.slides.isEmpty else { return false }
        let byID = Dictionary(assets.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        return draft.slides.allSatisfy { slide in
            guard slide.kind == "image", [nil, "none"].contains(slide.edits?.lookPreset),
                  let asset = byID[slide.assetID], asset.kind == "image", asset.sourceURL ?? asset.displayURL != nil else { return false }
            return (slide.edits?.effectiveTexts ?? []).allSatisfy {
                NativeFontCatalog.shared.ctFont($0.fontFamily, size: 12) != nil
            }
        }
    }

    /// One slide as JPEG data. Throws `SlidePostImageCache.LoadError.expired` for a stale signed URL so the
    /// caller can refresh the session once and retry.
    static func renderSlide(
        slide: SlidePostSlide, asset: SlidePostAsset, profile: String,
        scale: CGFloat = outputScale, fetch: Fetch = SlidePostImageCache.defaultFetch
    ) async throws -> Data {
        guard let url = asset.sourceURL ?? asset.displayURL else { throw RenderError.undecodable }
        let data = try await fetch(url)
        let canvas = canvas(for: profile)
        let pixels = CGSize(width: canvas.width * scale, height: canvas.height * scale)
        guard let photo = decode(data, maxPixel: max(pixels.width, pixels.height)) else { throw RenderError.undecodable }
        let texts = slide.edits?.effectiveTexts ?? []
        let overlay = texts.isEmpty ? nil : textLayer(texts, pixels: pixels)
        let mark = try watermark()

        let format = UIGraphicsImageRendererFormat()
        format.scale = 1; format.opaque = true; format.preferredRange = .standard
        let image = UIGraphicsImageRenderer(size: pixels, format: format).image { context in
            UIColor.black.setFill(); UIRectFill(CGRect(origin: .zero, size: pixels))
            photo.draw(in: coverRect(image: photo.size, in: pixels))
            overlay?.draw(in: CGRect(origin: .zero, size: pixels))
            // Last, so no text buries it: the stacking the video engine and the server render use.
            context.cgContext.interpolationQuality = .high
            mark.draw(in: KriaBranding.watermarkTileRect(canvas: pixels, tileSize: mark.size))
        }
        guard let jpeg = image.jpegData(compressionQuality: jpegQuality) else { throw RenderError.encodeFailed }
        return jpeg
    }

    /// The phone's default mark, with its opacity and shadow baked in. Missing from the bundle is a build
    /// defect, so it fails the export instead of saving unbranded slides (as `KriaBranding.preflight` does).
    private static func watermark() throws -> UIImage {
        let variant = KriaBranding.Variant.mist
        guard let url = KriaBranding.watermarkURL(variant), let image = UIImage(contentsOfFile: url.path) else {
            throw MediaEngineError.missingBrandingResource(KriaBranding.watermarkFileName(variant))
        }
        return image
    }

    /// Cover-fit, centre-crop: the photo scaled to fill `canvas`, centred (the overflow is clipped by the bitmap).
    static func coverRect(image: CGSize, in canvas: CGSize) -> CGRect {
        let factor = max(canvas.width / image.width, canvas.height / image.height)
        let size = CGSize(width: image.width * factor, height: image.height * factor)
        return CGRect(x: (canvas.width - size.width) / 2, y: (canvas.height - size.height) / 2, width: size.width, height: size.height)
    }

    /// EXIF-upright, sRGB, at most `maxPixel` on the long side (the original is never up-scaled).
    private static func decode(_ data: Data, maxPixel: CGFloat) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        ]
        guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        // Wide-gamut / P3 photos are converted so the JPEG matches what the server's sRGB output shows.
        guard let srgb = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(data: nil, width: cg.width, height: cg.height, bitsPerComponent: 8, bytesPerRow: 0, space: srgb,
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return UIImage(cgImage: cg) }
        context.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
        return context.makeImage().map { UIImage(cgImage: $0) } ?? UIImage(cgImage: cg)
    }

    /// The text layer rendered at the real pixel size (text scales by width / 1080, so rendering at the output
    /// size, not up-scaling a 1080 render, keeps glyph edges sharp).
    private static func textLayer(_ texts: [SlidePostTextElement], pixels: CGSize) -> UIImage? {
        let renderer = ImageRenderer(content: SlidePostTextLayerView(texts: texts, size: pixels))
        renderer.scale = 1
        renderer.isOpaque = false
        return renderer.uiImage
    }
}
