#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// Phone Talking edits: a full-screen card is drawn BELOW the caption text layer, as in the server's recipe
/// (`overlayAboveText` unset), while the legacy cloud stack (`overlayAboveText == true`) still draws cards over
/// text. Also pins that unplaced device-order cards keep distinct stacking orders, so an in-place preview update
/// (the editor's Display / Layer controls) restacks instead of keeping a stale order.
final class OverlayCaptionLayeringTests: XCTestCase {
    private let width = 96, height = 160

    private struct Fixture {
        var directory: URL
        var urls: [String: URL]
        var assets: [MediaAsset]
        var manifest: RenderAssetManifest
        var font: URL
    }

    @MainActor private func fixture() async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let speaker = try await makeVideo(directory: directory, name: "speaker", color: CGColor(red: 0, green: 0, blue: 1, alpha: 1), seconds: 3)
        let red = try await makeVideo(directory: directory, name: "red", color: CGColor(red: 1, green: 0, blue: 0, alpha: 1), seconds: 2)
        let green = try await makeVideo(directory: directory, name: "green", color: CGColor(red: 0, green: 1, blue: 0, alpha: 1), seconds: 2)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let urls = ["speaker": speaker, "red": red, "green": green, "font": font]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map {
            MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value))
        }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        }
        return Fixture(directory: directory, urls: urls, assets: assets, manifest: RenderAssetManifest(assets: references), font: font)
    }

    private func recipe(_ f: Fixture, cards: [TimelineClip]) throws -> EditRecipe {
        let canvas = Canvas(width: width, height: height)
        let caption = try AuthoredTextLayout.compile(id: "caption", text: "MMMM", start: 0, end: 3,
            style: .init(fontAssetID: "font", size: 40, color: .init(red: 1, green: 1, blue: 1, alpha: 1)), fontURL: f.font, canvas: canvas)
        let r = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas, assets: f.assets,
            tracks: [TimelineTrack(id: "subtitled", kind: .video, clips: [TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceDuration: 3)]),
                     TimelineTrack(id: "overlays", kind: .overlay, clips: cards)],
            audio: AudioMixRecipe(originalVolume: 1), assetManifest: f.manifest, textLayers: [caption])
        try r.validate()
        return r
    }

    /// What the editor compiler emits for a full-screen card: identity transform, no placement.
    private func fullscreenCard(_ id: String, asset: String, aboveText: Bool?) -> TimelineClip {
        TimelineClip(id: id, sourceAssetID: asset, sourceDuration: 1.5, timelineStart: 0.5, transform: .identity, volume: 0,
                     holdDuration: 0, overlayAboveText: aboveText, overlayPopIn: false, overlayPreserveAlpha: false)
    }

    private func pixels(_ image: CGImage) throws -> (white: Int, corner: [UInt8]) {
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        var white = 0
        for i in 0..<(width * height) where bytes[i * 4] > 200 && bytes[i * 4 + 1] > 200 && bytes[i * 4 + 2] > 200 { white += 1 }
        return (white, Array(bytes[((4 * width + 4) * 4)..<((4 * width + 4) * 4 + 3)]))
    }

    @MainActor private func liveFrame(_ live: LivePreviewComposition, at seconds: Double = 1.5) async throws -> (white: Int, corner: [UInt8]) {
        let item = live.preview.playerItem
        let generator = AVAssetImageGenerator(asset: item.asset)
        generator.videoComposition = item.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        return try pixels(try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image)
    }

    /// Exported frames (the same compositor the preview uses): the caption survives over a card that is not
    /// above-text, and is hidden by one that is.
    @MainActor func testCaptionStaysOverAFullscreenCardUnlessTheCardIsAboveText() async throws {
        let f = try await fixture(); defer { try? FileManager.default.removeItem(at: f.directory) }
        func frame(aboveText: Bool?) async throws -> (white: Int, corner: [UInt8]) {
            let output = f.directory.appendingPathComponent("\(UUID().uuidString).mp4")
            let checkpoint = try await AVFoundationLocalExporter(
                stateStore: FileExportStateStore(directory: f.directory.appendingPathComponent("state-\(UUID().uuidString)")), branding: .none)
                .export(recipe: try recipe(f, cards: [fullscreenCard("overlay:card", asset: "red", aboveText: aboveText)]), assetURLs: f.urls, outputURL: output)
            XCTAssertEqual(checkpoint.status, .completed)
            let generator = AVAssetImageGenerator(asset: AVURLAsset(url: output))
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            return try pixels(try await generator.image(at: CMTime(seconds: 1.5, preferredTimescale: 600)).image)
        }
        let device = try await frame(aboveText: nil)
        XCTAssertGreaterThan(device.corner[0], 180, "the full-screen card covers the speaker")
        XCTAssertGreaterThan(device.white, 50, "device stack: the caption is painted over the card")
        let cloud = try await frame(aboveText: true)
        XCTAssertGreaterThan(cloud.corner[0], 180)
        XCTAssertLessThan(cloud.white, 5, "legacy cloud stack: the card still covers the caption")
    }

    /// Cards that are not above-text and carry no placement used to share one order (ties fall back to whatever
    /// order the previous composition left), so a restack could be ignored. Each now has its own, in track order,
    /// and the legacy above-text numbering is untouched.
    @MainActor func testDeviceCardsGetDistinctOrdersInTrackOrder() async throws {
        let f = try await fixture(); defer { try? FileManager.default.removeItem(at: f.directory) }
        let device = try recipe(f, cards: [fullscreenCard("overlay:a", asset: "red", aboveText: nil), fullscreenCard("overlay:b", asset: "green", aboveText: nil)])
        let orders = AVPlayerPreviewComposer.overlayOrders(in: device)
        XCTAssertEqual(orders, ["overlay:a": 2000, "overlay:b": 2001])
        let legacy = try recipe(f, cards: [fullscreenCard("overlay:a", asset: "red", aboveText: true), fullscreenCard("overlay:b", asset: "green", aboveText: true)])
        XCTAssertEqual(AVPlayerPreviewComposer.overlayOrders(in: legacy), ["overlay:a": 2000, "overlay:b": 2001])
        // Reordering the track is not an in-place edit (the clip list differs), so it takes a full rebuild, which
        // reads these orders fresh.
        let live = try await LivePreviewComposition(recipe: device, assetURLs: f.urls)
        XCTAssertThrowsError(try live.updateText(recipe: try recipe(f, cards: [fullscreenCard("overlay:b", asset: "green", aboveText: nil), fullscreenCard("overlay:a", asset: "red", aboveText: nil)])))
        let rebuilt = try await LivePreviewComposition(recipe: try recipe(f, cards: [fullscreenCard("overlay:b", asset: "green", aboveText: nil), fullscreenCard("overlay:a", asset: "red", aboveText: nil)]), assetURLs: f.urls)
        let frame = try await liveFrame(rebuilt)
        XCTAssertGreaterThan(frame.corner[0], 180, "the later card in the track (red) is on top")
        XCTAssertGreaterThan(frame.white, 50, "captions stay above both cards")
    }

    /// Flipping a card from the legacy above-text stack to the device stack lands in place too.
    @MainActor func testInPlaceFlipToDeviceStackPutsTheCaptionBackOnTop() async throws {
        let f = try await fixture(); defer { try? FileManager.default.removeItem(at: f.directory) }
        let live = try await LivePreviewComposition(recipe: try recipe(f, cards: [fullscreenCard("overlay:card", asset: "red", aboveText: true)]), assetURLs: f.urls)
        let before = try await liveFrame(live)
        XCTAssertLessThan(before.white, 5)
        try live.updateText(recipe: try recipe(f, cards: [fullscreenCard("overlay:card", asset: "red", aboveText: nil)]))
        let after = try await liveFrame(live)
        XCTAssertGreaterThan(after.white, 50)
    }

    @MainActor private func makeVideo(directory: URL, name: String, color: CGColor, seconds: Double, fps: Int32 = 30) async throws -> URL {
        let (w, h) = (width, height)
        let videoURL = directory.appendingPathComponent("\(name)-video.mp4")
        let writer = try AVAssetWriter(outputURL: videoURL, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: w, AVVideoHeightKey: h])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h])
        writer.add(input)
        XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<Int((seconds * Double(fps)).rounded(.up)) {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: w, height: h, bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(color); context.fill(CGRect(x: 0, y: 0, width: w, height: h))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: fps)))
        }
        input.markAsFinished(); await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        let audioURL = directory.appendingPathComponent("\(name)-audio.m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let pcm = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000)))
        pcm.frameLength = pcm.frameCapacity
        for index in 0..<Int(pcm.frameLength) { pcm.floatChannelData![0][index] = 0.2 * Float(sin(Double(index) * 2 * .pi * 440 / 48_000)) }
        do {
            let file = try AVAudioFile(forWriting: audioURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000,
                AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
            try file.write(from: pcm)
        }
        let composition = AVMutableComposition()
        for (url, kind) in [(videoURL, AVMediaType.video), (audioURL, AVMediaType.audio)] {
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: kind)
            let track = try XCTUnwrap(tracks.first)
            let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
            try destination.insertTimeRange(try await track.load(.timeRange), of: track, at: .zero)
        }
        let output = directory.appendingPathComponent("\(name).mov")
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
        exporter.outputURL = output; exporter.outputFileType = .mov
        await exporter.export()
        XCTAssertEqual(exporter.status, .completed)
        return output
    }
}
#endif
