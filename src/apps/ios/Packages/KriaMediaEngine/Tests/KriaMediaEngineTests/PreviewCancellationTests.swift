#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// The editor cancels a superseded full rebuild (every volume-slider sample
/// is one since KRI-241). The build must stop at its next suspension with
/// `CancellationError`, never finish a composition nothing will play.
final class PreviewCancellationTests: XCTestCase {
    override func tearDown() {
        RenderProfiler.enabled = false
        RenderProfiler.reset()
        super.tearDown()
    }

    @MainActor func testACancelledBuildThrowsBeforeLoadingAnyClip() async throws {
        let (recipe, urls, directory) = try await talkingFixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        profileLayouts()
        let build = Task { @MainActor in try await AVPlayerPreviewComposer(branding: .standard).makePreview(recipe: recipe, assetURLs: urls) }
        build.cancel()
        let result = await build.result
        XCTAssertThrowsError(try result.get()) { XCTAssertTrue($0 is CancellationError, "\($0)") }
        XCTAssertEqual(captionLayouts(), 0)
    }

    /// A slider sample lands while the rebuild is loading its cuts: the build
    /// throws instead of laying out a single caption or returning a player item.
    @MainActor func testABuildSupersededWhileLoadingStopsBeforeTheCaptions() async throws {
        let (recipe, urls, directory) = try await talkingFixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        profileLayouts()
        _ = try await LivePreviewComposition(recipe: recipe, assetURLs: urls, branding: .standard)
        XCTAssertEqual(captionLayouts(), Double(recipe.textLayers.count), "an uncancelled build lays out every caption")

        RenderProfiler.reset()
        let build = Task { @MainActor in try await LivePreviewComposition(recipe: recipe, assetURLs: urls, branding: .standard) }
        // Main-actor jobs run in order: the build runs up to its first asset
        // load and suspends there, then this test resumes and supersedes it.
        await Task.yield()
        build.cancel()
        let result = await build.result
        XCTAssertThrowsError(try result.get()) { XCTAssertTrue($0 is CancellationError, "\($0)") }
        XCTAssertEqual(captionLayouts(), 0, "a superseded build stops before the caption layout")
    }

    /// The caption layout runs off the main actor and checks between layers.
    @MainActor func testOffMainCaptionLayoutMatchesTheInPlaceStoreAndStopsWhenCancelled() async throws {
        let (recipe, urls, directory) = try await talkingFixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        let canvas = CGSize(width: recipe.canvas.width, height: recipe.canvas.height)
        let inPlace = try NativeTextLayerStore(layers: recipe.textLayers, assetURLs: urls, canvas: canvas)
        let offMain = try await NativeTextLayerStore.make(layers: recipe.textLayers, assetURLs: urls, canvas: canvas, maxBitmapBytes: 64 * 1024 * 1024)
        for layer in recipe.textLayers {
            let time = (layer.start + layer.end) / 2
            XCTAssertEqual(offMain.selectionBounds(id: layer.id, at: time), inPlace.selectionBounds(id: layer.id, at: time), layer.id)
            XCTAssertNotNil(offMain.selectionBounds(id: layer.id, at: time), layer.id)
        }

        profileLayouts()
        let layout = Task { try await NativeTextLayerStore.make(layers: recipe.textLayers, assetURLs: urls, canvas: canvas, maxBitmapBytes: 64 * 1024 * 1024) }
        layout.cancel()
        let result = await layout.result
        XCTAssertThrowsError(try result.get()) { XCTAssertTrue($0 is CancellationError, "\($0)") }
        XCTAssertEqual(captionLayouts(), 0)
    }

    /// A device render cancelled while it composes reports `.cancelled`, not a
    /// failure the coordinator would surface as needing attention.
    @MainActor func testAnExportCancelledWhileComposingIsRecordedAsCancelled() async throws {
        let (recipe, urls, directory) = try await talkingFixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = FileExportStateStore(directory: directory.appendingPathComponent("state"))
        let output = directory.appendingPathComponent("out.mp4")
        let export = Task { @MainActor in
            try await AVFoundationLocalExporter(stateStore: store, branding: .standard)
                .export(recipe: recipe, assetURLs: urls, outputURL: output, exportID: "cancelled")
        }
        await Task.yield()
        export.cancel()
        let result = await export.result
        XCTAssertThrowsError(try result.get()) { XCTAssertTrue($0 is CancellationError, "\($0)") }
        XCTAssertEqual(try store.load(exportID: "cancelled")?.status, .cancelled)
        XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
    }

    private func profileLayouts() {
        RenderProfiler.enabled = true
        RenderProfiler.reset()
    }

    private func captionLayouts() -> Double { RenderProfiler.snapshot()["text.selectionGeometry"]?["count"] ?? 0 }

    /// One take cut into six segments under twelve caption cues and a music
    /// lane: a speech-cleanup Talking edit at a small canvas.
    @MainActor private func talkingFixture() async throws -> (EditRecipe, [String: URL], URL) {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let talk = try await mux(try await blackVideo(directory), try tone(directory), directory.appendingPathComponent("talk.mov"))
        let urls = ["talk": talk, "music": try tone(directory, name: "music", frequency: 660), "font": try fontURL()]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        }
        let cuts = (0..<6).map { TimelineClip(id: "cut-\($0)", sourceAssetID: "talk", sourceStart: Double($0) * 0.75, sourceDuration: 0.5, timelineStart: Double($0) * 0.5) }
        let white = TextInk(red: 1, green: 1, blue: 1, alpha: 1), black = TextInk(red: 0, green: 0, blue: 0, alpha: 1)
        let captions = (0..<12).map { index in
            PortableTextLayer(id: "caption-\(index)", start: Double(index) * 0.25, end: Double(index + 1) * 0.25, anchorX: 48, anchorY: 80, rotationDegrees: 0,
                runs: [PositionedTextRun(text: "cue \(index)", fontAssetID: "font", fontSize: 18, x: 10, baselineY: 80, letterSpacing: 0,
                                         shaped: true, fill: white, stroke: black, strokeWidth: 0)])
        }
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: cuts),
                     TimelineTrack(id: "music", kind: .audio, clips: [TimelineClip(id: "bed", sourceAssetID: "music", sourceDuration: 3, volume: 0.25)])],
            audio: AudioMixRecipe(originalVolume: 1), assetManifest: RenderAssetManifest(assets: references), textLayers: captions)
        return (recipe, urls, directory)
    }

    private func fontURL() throws -> URL {
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        return URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
    }

    private func tone(_ directory: URL, name: String = "voice", frequency: Double = 431) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 5 * 48_000)); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * frequency / 48_000)) }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func blackVideo(_ directory: URL) async throws -> URL {
        let url = directory.appendingPathComponent("picture.mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<150 { while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }; var raw: CVPixelBuffer?; CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw); let pixel = try XCTUnwrap(raw); CVPixelBufferLockBaseAddress(pixel, []); memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel)); CVPixelBufferUnlockBaseAddress(pixel, []); XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30))) }
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed); return url
    }

    @MainActor private func mux(_ video: URL, _ audio: URL, _ output: URL) async throws -> URL {
        let composition = AVMutableComposition()
        for (url, kind) in [(video, AVMediaType.video), (audio, AVMediaType.audio)] {
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: kind)
            let track = try XCTUnwrap(tracks.first)
            let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
            try destination.insertTimeRange(try await track.load(.timeRange), of: track, at: .zero)
        }
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough)); exporter.outputURL = output; exporter.outputFileType = .mov; await exporter.export(); XCTAssertEqual(exporter.status, .completed, "\(String(describing: exporter.error))"); return output
    }
}
#endif
