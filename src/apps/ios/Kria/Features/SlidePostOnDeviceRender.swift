import CoreImage
import ImageIO
import KriaMediaEngine
import SwiftUI
import UIKit

/// Renders photo slides on the phone from the ORIGINAL photos: cover-fit (framed by the slide's crop, centred by default) into the
/// slide's canvas at 2x, then the same `SlidePostTextLayerView` the editor shows, drawn at real pixel size,
/// then the slide's drawing (KRI-589), then its pasted pictures (KRI-460), then the Kria watermark the server render also puts on every slide (KRI-472). No server render, no
/// polling. The gated extension supports authored looks and delegates video slides to AVFoundation.
/// Unsupported profiles, looks, fonts, or sources take the server path.
@MainActor enum SlidePostOnDeviceRender {
    typealias Fetch = SlidePostImageCache.Fetcher
    enum RenderError: Error { case undecodable, encodeFailed }

    static let outputScale: CGFloat = 2
    static let jpegQuality: CGFloat = 0.92

    /// Logical canvas in points (the server's output size at scale 1): the post's size (KRI-606). No default
    /// for `aspect`, so a call site that forgets the draft's size fails to compile instead of exporting the
    /// platform default.
    static func canvas(profile: String, aspect: String?) -> CGSize { SlidePostSize.canvas(profile: profile, aspect: aspect) }

    /// A slide's export canvas: its own shape (KRI-44), else the post's size.
    static func canvas(for slide: SlidePostSlide, profile: String, aspect: String?) -> CGSize {
        SlidePostFraming.canvas(for: slide, profile: profile, aspect: aspect)
    }

    static func supports(_ draft: SlidePostDraft?, assets: [SlidePostAsset], extended: Bool = false) -> Bool {
        guard let draft, !draft.slides.isEmpty else { return false }
        let byID = Dictionary(assets.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        return draft.slides.allSatisfy { slide in
            guard let asset = byID[slide.assetID] else { return false }
            // A pasted picture needs its asset and a URL for the original; without them the server renders the slide.
            let stickersReady = (slide.edits?.effectiveStickers ?? []).allSatisfy { sticker in
                byID[sticker.assetID].map { $0.kind == "image" && ($0.sourceURL ?? $0.displayURL) != nil } == true
            }
            guard stickersReady else { return false }
            if slide.kind == "video" {
                return extended && SlidePostVideoRender.supports(slide: slide, asset: asset, profile: draft.platformProfile)
            }
            let preset = slide.edits?.lookPreset ?? "none"
            guard slide.kind == "image", asset.kind == "image", asset.sourceURL ?? asset.displayURL != nil,
                  preset == "none" || (extended && SlidePostLookRenderer.supports(preset)) else { return false }
            return (slide.edits?.effectiveTexts ?? []).allSatisfy {
                NativeFontCatalog.shared.ctFont($0.fontFamily, size: 12) != nil
            }
        }
    }

    /// One slide as JPEG data. Throws `SlidePostImageCache.LoadError.expired` for a stale signed URL so the
    /// caller can refresh the session once and retry.
    static func renderSlide(
        slide: SlidePostSlide, asset: SlidePostAsset, profile: String, aspect: String?,
        stickers: [SlidePostPreparedSticker] = [],
        scale: CGFloat = outputScale, fetch: Fetch = SlidePostImageCache.defaultFetch
    ) async throws -> Data {
        guard let url = asset.sourceURL ?? asset.displayURL else { throw RenderError.undecodable }
        let data = try await fetch(url)
        let canvas = canvas(for: slide, profile: profile, aspect: aspect)
        let pixels = CGSize(width: canvas.width * scale, height: canvas.height * scale)
        let crop = SlidePostFraming.effectiveCrop(for: slide)
        let fit = SlidePostFraming.effectiveFit(for: slide)
        guard let photo = decode(data, maxPixel: decodeLongSide(data, crop: crop, pixels: pixels, fit: fit != nil)) else { throw RenderError.undecodable }
        let preset = slide.edits?.lookPreset ?? "none"
        let tilt = SlidePostFraming.effectiveTilt(for: slide)
        // Bars and a turn need the Core Image composite; a plain fill keeps the cheap UIKit draw.
        let graded = preset == "none" && fit == nil && tilt == nil ? nil
            : try lookedImage(photo, pixels: pixels, preset: preset, crop: crop, fit: fit, blurSigma: SlidePostFit.blurSigmaPx * scale, tilt: tilt)
        let texts = slide.edits?.effectiveTexts ?? []
        let overlay = texts.isEmpty ? nil : textLayer(texts, pixels: pixels)
        let mark = try watermark()

        let format = UIGraphicsImageRendererFormat()
        format.scale = 1; format.opaque = true; format.preferredRange = .standard
        let image = UIGraphicsImageRenderer(size: pixels, format: format).image { context in
            UIColor.black.setFill(); UIRectFill(CGRect(origin: .zero, size: pixels))
            if let graded { graded.draw(in: CGRect(origin: .zero, size: pixels)) }
            else { photo.draw(in: SlidePostFraming.placement(imageSize: photo.size, crop: crop, canvas: pixels, fit: false)) }
            overlay?.draw(in: CGRect(origin: .zero, size: pixels))
            // KRI-589: the slide's ink goes over the text and under the watermark (the server's stacking), painted from
            // its strokes at the real pixel size, so it is as sharp as the text and needs no extra raster.
            slide.edits?.drawing?.paint(in: context.cgContext, canvas: pixels)
            // KRI-460: pasted pictures over the ink, in list order (the last is on top), drawn from the same geometry the editor uses.
            for sticker in stickers { sticker.draw(in: context.cgContext, canvas: pixels) }
            // Last, so no text buries it: the stacking the video engine and the server render use.
            context.cgContext.interpolationQuality = .high
            mark.draw(in: KriaBranding.watermarkTileRect(canvas: pixels, tileSize: mark.size))
        }
        guard let jpeg = image.jpegData(compressionQuality: jpegQuality) else { throw RenderError.encodeFailed }
        return jpeg
    }

    /// Frame the media in the canvas (crop, then fill or fit with bars), then grade the whole frame, before graphics.
    /// The preview uses the same geometry. `blurSigma` is in `pixels` (80 at scale 1; the export passes 80 x scale).
    nonisolated static let lookContext = CIContext(options: [.workingColorSpace: NSNull()])
    nonisolated static func lookedImage(
        _ photo: UIImage, pixels: CGSize, preset: String, crop: NormalizedSourceRect? = nil,
        fit: SlidePostFit? = nil, blurSigma: CGFloat = SlidePostFit.blurSigmaPx, tilt: SlidePostTilt? = nil
    ) throws -> UIImage {
        // Cache thumbnails retain their source color profile; export decoding
        // already normalizes to sRGB. Normalize both before encoded-RGB LUTs.
        guard let cg = photo.cgImage, let srgb = CGColorSpace(name: CGColorSpace.sRGB),
              pixels.width > 0, pixels.height > 0 else { throw RenderError.undecodable }
        let normalized: CGImage
        if cg.colorSpace?.name == CGColorSpace.sRGB { normalized = cg }
        else {
            guard let context = CGContext(data: nil, width: cg.width, height: cg.height, bitsPerComponent: 8, bytesPerRow: 0,
                                          space: srgb, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { throw RenderError.undecodable }
            context.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
            guard let image = context.makeImage() else { throw RenderError.undecodable }
            normalized = image
        }
        let source = CIImage(cgImage: normalized, options: [.colorSpace: NSNull()])
        let extent = CGRect(origin: .zero, size: pixels)
        let framed = framedBase(source, crop: crop, fit: fit, pixels: pixels, blurSigma: blurSigma, tilt: tilt)
        let looked = preset == "none" ? framed : try SlidePostLookRenderer.apply(framed, preset: preset)
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let output = lookContext.createCGImage(looked, from: extent, format: .RGBA8, colorSpace: colorSpace) else { throw RenderError.encodeFailed }
        return UIImage(cgImage: output)
    }

    /// The photo placed in the canvas. Fill covers it (edges trimmed); a fit shows the whole cropped region and
    /// fills the rest with black, white or a blurred cover-fill of the same region -- the server's `_frame_filter`.
    /// With a `tilt` the media turns about the canvas centre over the tilt's backdrop instead (`tiltedBase`).
    nonisolated static func framedBase(_ source: CIImage, crop: NormalizedSourceRect?, fit: SlidePostFit?, pixels: CGSize, blurSigma: CGFloat,
                                       tilt: SlidePostTilt? = nil) -> CIImage {
        if let tilt, !tilt.isUpright {
            return tiltedBase(source, crop: crop, fit: fit != nil, pixels: pixels, blurSigma: blurSigma, tilt: tilt)
        }
        let extent = CGRect(origin: .zero, size: pixels)
        let cover = SlidePostFraming.placement(imageSize: source.extent.size, crop: crop, canvas: pixels, fit: false)
        guard let fit else { return placed(source, in: cover, pixels: pixels).cropped(to: extent) }
        let whole = SlidePostFraming.placement(imageSize: source.extent.size, crop: crop, canvas: pixels, fit: true)
        var photo = placed(source, in: whole, pixels: pixels)
        if let crop {
            // Only the cropped region is the photo: the rest of the source must never show up in the bars (the
            // server crops first).
            photo = photo.cropped(to: regionRect(crop, in: whole, pixels: pixels))
        }
        photo = photo.cropped(to: extent)
        let bars: CIImage
        switch fit {
        case .black: bars = CIImage(color: .black).cropped(to: extent)
        case .white: bars = CIImage(color: .white).cropped(to: extent)
        case .blur: bars = placed(source, in: cover, pixels: pixels).clampedToExtent().applyingGaussianBlur(sigma: Double(blurSigma)).cropped(to: extent)
        }
        return photo.composited(over: bars)
    }

    /// The source drawn at `rect` (top-left canvas pixels). Core Image's origin is bottom-left, so the y offset is flipped.
    nonisolated private static func placed(_ source: CIImage, in rect: CGRect, pixels: CGSize) -> CIImage {
        source.transformed(by: CGAffineTransform(translationX: -source.extent.minX, y: -source.extent.minY))
            .transformed(by: CGAffineTransform(scaleX: rect.width / source.extent.width, y: rect.height / source.extent.height))
            .transformed(by: CGAffineTransform(translationX: rect.minX, y: pixels.height - rect.maxY))
    }

    /// The crop region of the photo placed at `whole`, as a Core Image (bottom-left origin) rectangle.
    nonisolated private static func regionRect(_ crop: NormalizedSourceRect, in whole: CGRect, pixels: CGSize) -> CGRect {
        let region = CGRect(x: whole.minX + crop.x * whole.width, y: whole.minY + crop.y * whole.height,
                            width: crop.width * whole.width, height: crop.height * whole.height)
        return CGRect(x: region.minX, y: pixels.height - region.maxY, width: region.width, height: region.height)
    }

    /// KRI-1138: the (cropped) region scaled like the slide frames it, turned about the canvas centre, over the tilt's
    /// backdrop; the server's `_tilt_frame_filter`. The media is NOT trimmed to the canvas before it turns, so the pixels
    /// a Fill photo has beyond the frame rotate into view. The Fit bars do not draw: the backdrop is what shows.
    nonisolated static func tiltedBase(_ source: CIImage, crop: NormalizedSourceRect?, fit: Bool, pixels: CGSize, blurSigma: CGFloat,
                                       tilt: SlidePostTilt) -> CIImage {
        let whole = SlidePostFraming.placement(imageSize: source.extent.size, crop: crop, canvas: pixels, fit: fit)
        var media = placed(source, in: whole, pixels: pixels)
        if let crop { media = media.cropped(to: regionRect(crop, in: whole, pixels: pixels)) }
        let cover = SlidePostFraming.placement(imageSize: source.extent.size, crop: crop, canvas: pixels, fit: false)
        return turned(media, over: placed(source, in: cover, pixels: pixels), tilt: tilt, pixels: pixels, blurSigma: blurSigma)
    }

    /// `media` (already placed in canvas pixels, any size) turned clockwise by `tilt` about the canvas centre over the
    /// tilt's backdrop, cropped to the canvas. `cover` is the frame a blur backdrop softens (a cover-fit copy of the same
    /// media). Shared by photo slides and, per frame, by the exported video slides.
    nonisolated static func turned(_ media: CIImage, over cover: CIImage, tilt: SlidePostTilt, pixels: CGSize, blurSigma: CGFloat) -> CIImage {
        let extent = CGRect(origin: .zero, size: pixels)
        let centre = CGPoint(x: pixels.width / 2, y: pixels.height / 2)
        let turn = CGAffineTransform(translationX: -centre.x, y: -centre.y)
            .concatenating(CGAffineTransform(rotationAngle: SlidePostTiltGeometry.coreImageAngle(degrees: tilt.rotationDeg)))
            .concatenating(CGAffineTransform(translationX: centre.x, y: centre.y))
        let under: CIImage
        switch tilt.backdrop {
        case .blank, .media:
            under = CIImage(color: .black).cropped(to: extent)
        case .color(let hex):
            guard let rgb = SlidePostTilt.rgb(hex) else { under = CIImage(color: .black).cropped(to: extent); break }
            under = CIImage(color: CIColor(red: rgb.red, green: rgb.green, blue: rgb.blue)).cropped(to: extent)
        case .blur:
            under = cover.clampedToExtent().applyingGaussianBlur(sigma: Double(blurSigma)).cropped(to: extent)
        }
        return media.transformed(by: turn).composited(over: under).cropped(to: extent)
    }

    /// The phone's default mark, with its opacity and shadow baked in. Missing from the bundle is a build
    /// defect, so it fails the export instead of saving unbranded slides (as `KriaBranding.preflight` does).
    static func watermark() throws -> UIImage {
        let variant = KriaBranding.Variant.mist
        guard let url = KriaBranding.watermarkURL(variant), let image = UIImage(contentsOfFile: url.path) else {
            throw MediaEngineError.missingBrandingResource(KriaBranding.watermarkFileName(variant))
        }
        return image
    }

    /// The long side to decode at: enough pixels for the cropped region to fill the canvas sharply, never more
    /// than the original has and never beyond a memory-safe ceiling.
    nonisolated static func decodeLongSide(_ data: Data, crop: NormalizedSourceRect?, pixels: CGSize, fit: Bool) -> CGFloat {
        let base = max(pixels.width, pixels.height)
        guard let crop, let source = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.doubleValue,
              let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.doubleValue, width > 0, height > 0 else { return base }
        let turned = [5, 6, 7, 8].contains((properties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1)
        let placed = SlidePostFraming.placement(imageSize: turned ? CGSize(width: height, height: width) : CGSize(width: width, height: height), crop: crop, canvas: pixels, fit: fit)
        return min(max(base, max(placed.width, placed.height)), 6144)
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
    static func textLayer(_ texts: [SlidePostTextElement], pixels: CGSize) -> UIImage? {
        let renderer = ImageRenderer(content: SlidePostTextLayerView(texts: texts, size: pixels))
        renderer.scale = 1
        renderer.isOpaque = false
        return renderer.uiImage
    }
}
