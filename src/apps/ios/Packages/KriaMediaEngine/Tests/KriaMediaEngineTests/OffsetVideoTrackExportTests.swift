#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// KRI-811 (Troya Talking, F02): an original whose picture starts ~21 ms after its sound (a leading
/// empty edit in the video track, audio at 0) failed every native export with `missingAsset("1")`.
/// "1" is the composition track, not an asset: a clip at sourceStart 0 copied the empty head into
/// composition track 1, so the compositor found no frame at t = 0. A normalized copy rendered.
///
/// These run the real `AVFoundationLocalExporter` over a synthetic original with that exact shape
/// and over a normalized control. Set `KRI811_KEEP_DIR` to keep sources and exported MP4s for
/// ffprobe evidence.
@MainActor final class OffsetVideoTrackExportTests: XCTestCase {
    private static let offset = 0.021029
    private var directory: URL!
    private var keepDirectory: URL? {
        ProcessInfo.processInfo.environment["KRI811_KEEP_DIR"].map { URL(fileURLWithPath: $0) }
    }

    override func setUp() async throws {
        directory = keepDirectory?.appendingPathComponent(name.components(separatedBy: " ").last!.trimmingCharacters(in: CharacterSet(charactersIn: "]")))
            ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.removeItem(at: directory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        if keepDirectory == nil { try? FileManager.default.removeItem(at: directory) }
    }

    private func original(offset: Double = OffsetVideoTrackExportTests.offset, name: String = "offset-source") async throws -> URL {
        try await SolidVideoFixture.make(directory: directory, name: name, color: CGColor(red: 1, green: 0, blue: 0, alpha: 1),
                                         seconds: 1.5, videoStartOffset: offset)
    }

    private func recipe(_ clips: [TimelineClip]) -> EditRecipe {
        EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "source", relativePath: "source.mov")],
                   tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)])
    }

    private func export(_ recipe: EditRecipe, source: URL, name: String) async throws -> URL {
        let output = directory.appendingPathComponent("\(name).mp4")
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: ["source": source], outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed)
        return output
    }

    /// Where the first real video sample of `url` lands, in seconds.
    private func firstVideoSample(_ url: URL) async throws -> Double {
        let tracks = try await AVURLAsset(url: url).loadTracks(withMediaType: .video)
        let segments = try await XCTUnwrap(tracks.first).load(.segments)
        let segment = try XCTUnwrap(segments.first(where: { !$0.isEmpty }))
        return segment.timeMapping.target.start.seconds
    }

    private func assertBrightRed(_ asset: AVAsset, composition: AVVideoComposition?, at seconds: [Double], file: StaticString = #filePath, line: UInt = #line) async throws {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.videoComposition = composition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        for second in seconds {
            let image = try await generator.image(at: CMTime(seconds: second, preferredTimescale: 600)).image
            var pixel = [UInt8](repeating: 0, count: 4)
            pixel.withUnsafeMutableBytes { bytes in
                let context = CGContext(data: bytes.baseAddress, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                        space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
                context.draw(image, in: CGRect(x: 0, y: 0, width: 1, height: 1))
            }
            XCTAssertGreaterThan(pixel[0], 150, "no source picture at \(second) s", file: file, line: line)
        }
    }

    /// Video segments of the built composition as (source start, target start, target duration).
    private func videoSegments(_ preview: PreviewComposition) async throws -> [(source: Double, target: Double, duration: Double)] {
        let tracks = try await preview.playerItem.asset.loadTracks(withMediaType: .video)
        return try await XCTUnwrap(tracks.first).load(.segments).filter { !$0.isEmpty }.map {
            ($0.timeMapping.source.start.seconds, $0.timeMapping.target.start.seconds, $0.timeMapping.target.duration.seconds)
        }
    }

    /// F1 + F4 + F9 + F10: clip at sourceStart 0 over the whole original, claiming two frames past
    /// its end. Exports, the first frame is the original's first picture, the picture is held (not
    /// slid earlier) so from the first real sample on video and audio share one clock, and the tail
    /// is not over-held because the offset was counted twice.
    func testOffsetOriginalExportsWithItsFirstFrameAndAudioOnItsOwnClock() async throws {
        let source = try await original()
        let videoStart = try await firstVideoSample(source)
        XCTAssertGreaterThan(videoStart, 0.01, "fixture must reproduce the leading empty edit")
        let sourceTracks = try await AVURLAsset(url: source).loadTracks(withMediaType: .video)
        let trackEnd = try await XCTUnwrap(sourceTracks.first).load(.timeRange).end.seconds
        let claimed = trackEnd + 2.0 / 30
        let recipe = recipe([TimelineClip(id: "talk", sourceAssetID: "source", sourceDuration: claimed)])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let segments = try await videoSegments(preview)
        // Held first frame over [0, videoStart), then real samples at their own source time.
        XCTAssertEqual(segments.first?.target ?? -1, 0, accuracy: 0.0005)
        let moving = try XCTUnwrap(segments.first(where: { abs($0.target - videoStart) < 0.002 }), "\(segments)")
        XCTAssertEqual(moving.source, videoStart, accuracy: 0.002, "picture slid against its sound: \(segments)")
        // Only the two claimed-but-missing frames are held at the end, not the head offset again.
        let last = try XCTUnwrap(segments.last)
        XCTAssertEqual(last.target + last.duration, claimed, accuracy: 0.002)
        XCTAssertEqual(last.duration, 2.0 / 30, accuracy: 0.002, "\(segments)")

        let output = try await export(recipe, source: source, name: "offset-export")
        let asset = AVURLAsset(url: output)
        let start = try await firstVideoSample(output)
        XCTAssertEqual(start, 0, accuracy: 0.0005)
        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        let audioSegments = try await XCTUnwrap(audioTracks.first).load(.segments)
        let audioStart = try XCTUnwrap(audioSegments.first(where: { !$0.isEmpty })).timeMapping.target.start.seconds
        XCTAssertEqual(audioStart, 0, accuracy: 0.0005, "speech must keep its own start")
        let duration = try await asset.load(.duration).seconds
        XCTAssertEqual(duration, claimed, accuracy: 1.0 / 30 + 0.002)
        try await assertBrightRed(asset, composition: nil, at: [0, 0.01, videoStart + 0.005, claimed - 0.05])
    }

    /// F2: a trim landing inside the empty head (sourceStart 0.010 < 0.021).
    func testTrimStartingInsideTheEmptyHeadExports() async throws {
        let source = try await original()
        let videoStart = try await firstVideoSample(source)
        let recipe = recipe([TimelineClip(id: "talk", sourceAssetID: "source", sourceStart: 0.010, sourceDuration: 1.0)])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let segments = try await videoSegments(preview)
        let moving = try XCTUnwrap(segments.last(where: { abs($0.source - videoStart) < 0.002 }), "\(segments)")
        XCTAssertEqual(segments.first?.source ?? -1, videoStart, accuracy: 0.002, "the head holds the first real frame: \(segments)")
        XCTAssertEqual(moving.target, videoStart - 0.010, accuracy: 0.002, "timeline t must show source 0.010 + t")
        let output = try await export(recipe, source: source, name: "inside-head-export")
        try await assertBrightRed(AVURLAsset(url: output), composition: nil, at: [0, 0.5, 0.95])
    }

    /// F3: a trim already past the offset builds exactly what it built before (one moving segment).
    func testTrimPastTheOffsetIsUnchanged() async throws {
        let source = try await original()
        let recipe = recipe([TimelineClip(id: "talk", sourceAssetID: "source", sourceStart: 1.0, sourceDuration: 0.4)])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let segments = try await videoSegments(preview)
        XCTAssertEqual(segments.count, 1, "\(segments)")
        XCTAssertEqual(segments[0].source, 1.0, accuracy: 0.0005)
        XCTAssertEqual(segments[0].target, 0, accuracy: 0.0005)
        XCTAssertEqual(segments[0].duration, 0.4, accuracy: 0.0005)
        _ = try await export(recipe, source: source, name: "past-offset-export")
    }

    /// F5 + F6: speech-cleanup cuts on one reused track. The first keep starts at 0 (audio crossfade
    /// handles at the join); the second clip must not inherit the head hold.
    func testCleanupCutsOnAnOffsetOriginalKeepEachClipOnItsOwnSource() async throws {
        let source = try await original()
        let recipe = recipe([
            TimelineClip(id: "keep-1", sourceAssetID: "source", sourceStart: 0, sourceDuration: 0.5, timelineStart: 0),
            TimelineClip(id: "keep-2", sourceAssetID: "source", sourceStart: 0.8, sourceDuration: 0.6, timelineStart: 0.5),
        ])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let segments = try await videoSegments(preview)
        let second = try XCTUnwrap(segments.first(where: { abs($0.target - 0.5) < 0.002 }), "\(segments)")
        XCTAssertEqual(second.source, 0.8, accuracy: 0.002)
        XCTAssertEqual(second.duration, 0.6, accuracy: 0.002)
        let output = try await export(recipe, source: source, name: "cuts-export")
        let duration = try await AVURLAsset(url: output).load(.duration).seconds
        XCTAssertEqual(duration, 1.1, accuracy: 1.0 / 30 + 0.002)
        try await assertBrightRed(AVURLAsset(url: output), composition: nil, at: [0, 0.49, 0.5, 1.05])
    }

    /// F8: speed change with a head gap. The hold is in timeline time (gap / rate).
    func testSpeedChangeOnAnOffsetOriginalScalesTheHeadHoldInTimelineTime() async throws {
        let source = try await original()
        let videoStart = try await firstVideoSample(source)
        let recipe = recipe([TimelineClip(id: "fast", sourceAssetID: "source", sourceDuration: 1.0, rate: 2)])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let segments = try await videoSegments(preview)
        let moving = try XCTUnwrap(segments.last(where: { abs($0.source - videoStart) < 0.002 }), "\(segments)")
        XCTAssertEqual(segments.first?.source ?? -1, videoStart, accuracy: 0.002, "the head holds the first real frame: \(segments)")
        XCTAssertEqual(moving.target, videoStart / 2, accuracy: 0.002)
        let output = try await export(recipe, source: source, name: "rate2-export")
        let duration = try await AVURLAsset(url: output).load(.duration).seconds
        XCTAssertEqual(duration, 0.5, accuracy: 1.0 / 30 + 0.002)
        try await assertBrightRed(AVURLAsset(url: output), composition: nil, at: [0, 0.45])
    }

    /// F11 + F12: the editor's live player gets a valid composition with a picture at t = 0, and a
    /// retry/reopen rebuild from the same recipe admits the source identically.
    func testLivePreviewOfAnOffsetOriginalShowsItsFirstFrameAndRebuildsIdentically() async throws {
        let source = try await original()
        let recipe = recipe([TimelineClip(id: "talk", sourceAssetID: "source", sourceDuration: 1.2)])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let asset = preview.playerItem.asset
        let composition = try XCTUnwrap(preview.playerItem.videoComposition)
        let valid = try await composition.isValid(for: asset, timeRange: CMTimeRange(start: .zero, duration: try await asset.load(.duration)), validationDelegate: nil)
        XCTAssertTrue(valid, "AVPlayer silently renders black for an invalid video composition (KRI-128)")
        try await assertBrightRed(asset, composition: composition, at: [0, 0.01, 0.6])
        let rebuilt = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let first = try await videoSegments(preview), second = try await videoSegments(rebuilt)
        XCTAssertEqual(first.map { [$0.source, $0.target, $0.duration] }, second.map { [$0.source, $0.target, $0.duration] })
    }

    /// F13: a truly missing original still fails the export; nothing is held in its place.
    func testDeletedOriginalStillFailsInsteadOfHoldingAFrame() async throws {
        let source = try await original()
        let gone = directory.appendingPathComponent("gone.mov")
        try FileManager.default.moveItem(at: source, to: gone)
        try FileManager.default.removeItem(at: gone)
        let recipe = recipe([TimelineClip(id: "talk", sourceAssetID: "source", sourceDuration: 1.0)])
        do {
            _ = try await export(recipe, source: gone, name: "missing-export")
            XCTFail("a deleted original must not export")
        } catch MediaEngineError.missingAsset(let id) {
            XCTAssertEqual(id, "source", "relink needs the asset id, not a composition track id")
        } catch {
            XCTAssertFalse(String(describing: error).contains("missingAsset"), "unexpected track-level failure: \(error)")
            XCTAssertFalse(error is NativePreviewFeatureError, "a deleted file is not an unsupported recipe: \(error)")
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.appendingPathComponent("missing-export.mp4").path))
        do {
            _ = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: [:])
            XCTFail("an unresolved original must not build")
        } catch MediaEngineError.missingAsset(let id) {
            XCTAssertEqual(id, "source", "relink needs the asset id, not a composition track id")
        }
    }

    /// A freeze authored on a clip shorter than the empty head keeps its whole slot on the held
    /// first frame instead of rendering black after the gap.
    func testHoldOnAClipInsideTheEmptyHeadKeepsItsSlot() async throws {
        let source = try await original()
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: source)
        let manifest = try RenderAssetManifest(assets: [RenderAssetReference(id: "source", fingerprint: RenderFingerprint(fingerprint), source: .original(mediaID: "source"))])
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160),
            assets: [MediaAsset(id: "source", relativePath: "source", fingerprint: fingerprint)],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [
                TimelineClip(id: "freeze", sourceAssetID: "source", sourceDuration: 0.015, holdDuration: 1.0)])], assetManifest: manifest)
        let output = try await export(recipe, source: source, name: "head-freeze-export")
        let duration = try await AVURLAsset(url: output).load(.duration).seconds
        XCTAssertEqual(duration, 1.015, accuracy: 1.0 / 30 + 0.002)
        try await assertBrightRed(AVURLAsset(url: output), composition: nil, at: [0, 0.5, 0.95])
    }

    /// F16: a normalized control (video at 0) keeps its single moving segment and exports.
    func testNormalizedControlIsUnchanged() async throws {
        let source = try await original(offset: 0, name: "control-source")
        let recipe = recipe([TimelineClip(id: "talk", sourceAssetID: "source", sourceDuration: 1.2)])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": source])
        let segments = try await videoSegments(preview)
        XCTAssertEqual(segments.count, 1, "\(segments)")
        XCTAssertEqual(segments[0].source, 0, accuracy: 0.0005)
        XCTAssertEqual(segments[0].duration, 1.2, accuracy: 0.0005)
        let output = try await export(recipe, source: source, name: "control-export")
        try await assertBrightRed(AVURLAsset(url: output), composition: nil, at: [0, 1.1])
    }
}
#endif
