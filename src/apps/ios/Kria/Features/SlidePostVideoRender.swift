import AVFoundation
import CoreImage
import KriaMediaEngine
import SwiftUI
import UIKit

/// Native MP4 renderer for an Instagram carousel video slide. The source bytes stay on disk throughout:
/// a remote signed URL is downloaded with URLSession's file API, then AVFoundation reads and transcodes it.
@MainActor enum SlidePostVideoRender {
    typealias Download = @Sendable (URL) async throws -> URL

    enum RenderError: Error, Equatable {
        case unsupported
        case missingVideoTrack
        case invalidDuration
        case exportUnavailable
        case exportFailed
    }

    static let maximumDuration: Double = 90

    static func supports(slide: SlidePostSlide, asset: SlidePostAsset, profile: String) -> Bool {
        guard profile == "instagram_carousel", slide.kind == "video", asset.kind == "video",
              asset.sourceURL != nil || safeDisplayVideoURL(asset.displayURL) != nil else { return false }
        let preset = slide.edits?.lookPreset ?? "none"
        return SlidePostLookRenderer.supports(preset)
            && (slide.edits?.effectiveTexts ?? []).allSatisfy { NativeFontCatalog.shared.ctFont($0.fontFamily, size: 12) != nil }
    }

    /// Renders one source video to the exact 2160×2700 carousel canvas. The caller owns `outputURL`;
    /// this method removes it on cancellation or any export failure so an incomplete MP4 is never saved.
    static func renderSlide(
        slide: SlidePostSlide,
        asset: SlidePostAsset,
        profile: String,
        outputURL: URL,
        download: Download? = nil
    ) async throws {
        guard supports(slide: slide, asset: asset, profile: profile), outputURL.isFileURL else { throw RenderError.unsupported }
        try Task.checkCancellation()

        let sourceURL = try sourceURL(for: asset)
        let source = try await localFile(for: sourceURL, download: download)
        defer { if source.removeWhenFinished { try? FileManager.default.removeItem(at: source.url) } }

        do {
            try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: outputURL.path) { try FileManager.default.removeItem(at: outputURL) }
            let asset = AVURLAsset(url: source.url)
            let prepared = try await prepare(asset: asset, profile: profile, lookPreset: slide.edits?.lookPreset ?? "none")
            let overlay = try staticOverlay(texts: slide.edits?.effectiveTexts ?? [], pixels: prepared.pixels)
            let composition = videoComposition(prepared: prepared, overlay: overlay)
            guard let exporter = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetHighestQuality) else {
                throw RenderError.exportUnavailable
            }
            exporter.outputURL = outputURL
            exporter.outputFileType = .mp4
            exporter.shouldOptimizeForNetworkUse = true
            exporter.timeRange = CMTimeRange(start: .zero, duration: prepared.duration)
            exporter.videoComposition = composition

            // AVAssetExportSession does not observe Task cancellation on its own.
            nonisolated(unsafe) let cancellable = exporter
            await withTaskCancellationHandler {
                await exporter.export()
            } onCancel: {
                cancellable.cancelExport()
            }
            if Task.isCancelled { exporter.cancelExport(); throw CancellationError() }
            guard exporter.status == .completed else { throw exporter.error ?? RenderError.exportFailed }
        } catch {
            try? FileManager.default.removeItem(at: outputURL)
            throw error
        }
    }

    /// The preview uses the same orientation, cover crop, and look as export. Text and branding stay out
    /// of this composition because the workspace draws its existing SwiftUI overlay above the player.
    static func previewComposition(asset: AVAsset, profile: String, lookPreset: String) async throws -> AVVideoComposition {
        guard profile == "instagram_carousel", SlidePostLookRenderer.supports(lookPreset) else { throw RenderError.unsupported }
        let prepared = try await prepare(asset: asset, profile: profile, lookPreset: lookPreset)
        return videoComposition(prepared: prepared, overlay: nil)
    }

    private struct Prepared {
        let asset: AVAsset
        let pixels: CGSize
        let duration: CMTime
        let lookPreset: String
    }

    private static func prepare(asset: AVAsset, profile: String, lookPreset: String) async throws -> Prepared {
        guard profile == "instagram_carousel", SlidePostLookRenderer.supports(lookPreset),
              let track = try await asset.loadTracks(withMediaType: .video).first else {
            throw RenderError.missingVideoTrack
        }
        let loadedDuration = try await asset.load(.duration)
        let seconds = loadedDuration.seconds
        guard seconds.isFinite, seconds > 0 else { throw RenderError.invalidDuration }
        let naturalSize = try await track.load(.naturalSize)
        guard naturalSize.width.isFinite, naturalSize.height.isFinite, naturalSize.width > 0, naturalSize.height > 0 else {
            throw RenderError.missingVideoTrack
        }
        let pixels = CGSize(width: SlidePostOnDeviceRender.canvas(for: profile).width * SlidePostOnDeviceRender.outputScale,
                            height: SlidePostOnDeviceRender.canvas(for: profile).height * SlidePostOnDeviceRender.outputScale)
        return Prepared(asset: asset,
                        pixels: pixels,
                        duration: CMTime(seconds: min(seconds, maximumDuration), preferredTimescale: 60_000),
                        lookPreset: lookPreset)
    }

    private static func videoComposition(prepared: Prepared, overlay: CIImage?) -> AVMutableVideoComposition {
        let pixels = prepared.pixels
        let preset = prepared.lookPreset
        let context = SlidePostOnDeviceRender.lookContext
        // The modern asynchronous factory requires sending AVAsset across its completion boundary;
        // AVAsset remains Objective-C non-Sendable in the iOS SDK. This synchronous framework factory
        // keeps the immutable asset on this actor and is safe for the read-only composition setup.
        let composition = AVMutableVideoComposition(asset: prepared.asset, applyingCIFiltersWithHandler: { request in
            // AVFoundation has already applied the track's preferred transform to sourceImage. Applying
            // it again rotates portrait phone footage a second time, so cover-fit from this upright extent.
            let covered = cover(request.sourceImage, into: pixels)
            do {
                let graded = try SlidePostLookRenderer.apply(covered, preset: preset)
                request.finish(with: overlay.map { $0.composited(over: graded) } ?? graded,
                               context: context)
            } catch {
                // A requested look must never quietly become neutral footage, and it must never crash
                // the export process. AVFoundation marks this export failed and the caller removes output.
                request.finish(with: error)
            }
        })
        composition.renderSize = pixels
        composition.frameDuration = CMTime(value: 1, timescale: 30)
        composition.colorPrimaries = AVVideoColorPrimaries_ITU_R_709_2
        composition.colorTransferFunction = AVVideoTransferFunction_ITU_R_709_2
        composition.colorYCbCrMatrix = AVVideoYCbCrMatrix_ITU_R_709_2
        return composition
    }

    /// Fills `canvas` from the already-upright Core Image frame and centres the crop.
    nonisolated private static func cover(_ image: CIImage, into canvas: CGSize) -> CIImage {
        let extent = image.extent
        let scale = max(canvas.width / max(extent.width, 1), canvas.height / max(extent.height, 1))
        let scaledSize = CGSize(width: extent.width * scale, height: extent.height * scale)
        return image
            .transformed(by: .init(translationX: -extent.minX, y: -extent.minY))
            .transformed(by: .init(scaleX: scale, y: scale))
            .transformed(by: .init(translationX: (canvas.width - scaledSize.width) / 2,
                                  y: (canvas.height - scaledSize.height) / 2))
            .cropped(to: CGRect(origin: .zero, size: canvas))
    }

    private static func staticOverlay(texts: [SlidePostTextElement], pixels: CGSize) throws -> CIImage {
        let text = texts.isEmpty ? nil : SlidePostOnDeviceRender.textLayer(texts, pixels: pixels)
        // A failed SwiftUI raster must fail export rather than silently omit a
        // caption the user sees in the editor.
        guard texts.isEmpty || text != nil else { throw RenderError.exportFailed }
        let mark = try SlidePostOnDeviceRender.watermark()
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1; format.opaque = false; format.preferredRange = .standard
        let image = UIGraphicsImageRenderer(size: pixels, format: format).image { context in
            text?.draw(in: CGRect(origin: .zero, size: pixels))
            context.cgContext.interpolationQuality = .high
            mark.draw(in: KriaBranding.watermarkTileRect(canvas: pixels, tileSize: mark.size))
        }
        guard let cgImage = image.cgImage else { throw RenderError.exportFailed }
        // CIImage(cgImage:) retains the CGImage's normal display orientation. Applying a second
        // coordinate flip here inverts the UIKit-rendered text and watermark in the exported frame.
        return CIImage(cgImage: cgImage)
    }

    private static func sourceURL(for asset: SlidePostAsset) throws -> URL {
        if let source = asset.sourceURL { return source }
        if let display = safeDisplayVideoURL(asset.displayURL) { return display }
        throw RenderError.unsupported
    }

    /// A display rendition is only a video fallback when its URL names a container AVFoundation can open.
    /// Image posters share the same field and must never be downloaded as an MP4 source.
    private static func safeDisplayVideoURL(_ url: URL?) -> URL? {
        guard let url else { return nil }
        let suffix = url.pathExtension.lowercased()
        return ["mp4", "mov", "m4v"].contains(suffix) ? url : nil
    }

    private static func localFile(for url: URL, download: Download?) async throws -> (url: URL, removeWhenFinished: Bool) {
        if url.isFileURL { return (url, false) }
        if let download { return (try await download(url), true) }
        let (temporary, response) = try await URLSession.shared.download(from: url)
        do {
            if let http = response as? HTTPURLResponse {
                if [401, 403, 410].contains(http.statusCode) { throw SlidePostImageCache.LoadError.expired }
                guard (200..<300).contains(http.statusCode) else { throw RenderError.exportFailed }
            }
            let destination = FileManager.default.temporaryDirectory.appending(path: "slidepost-video-\(UUID().uuidString).\(url.pathExtension.isEmpty ? "mp4" : url.pathExtension)")
            try FileManager.default.moveItem(at: temporary, to: destination)
            return (destination, true)
        } catch {
            try? FileManager.default.removeItem(at: temporary)
            throw error
        }
    }
}
