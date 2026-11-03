import AVFoundation
import UIKit
import XCTest
@testable import Kria
@testable import KriaMediaEngine

/// The editor's live preview of a Talk-to-camera edit with a green-screen background. The creator opened such
/// an edit, saw the background on the finished video, and lost it the moment the editable preview replaced it.
/// These tests build that preview the way the editor does (`NativeEditorRenderCompiler` -> `LivePreviewComposition`)
/// and look at its pixels, which the engine's own tests (a hand-built recipe) never did.
@MainActor final class SpeakerBackgroundLivePreviewTests: XCTestCase {
    private let room = UIColor(red: 1, green: 0, blue: 1, alpha: 1)
    private let backdrop = UIColor(red: 0, green: 0, blue: 1, alpha: 1)

    /// A centred rectangle is "the person"; everything else is the room.
    private final class CenterBoxSegmenter: NativePersonSegmenting {
        func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? {
            let side = 64
            var values = [Float](repeating: 0.02, count: side * side)
            for y in 16..<48 { for x in 16..<48 { values[y * side + x] = 1 } }
            return SubjectConfidenceMap(width: side, height: side, values: values)
        }
    }

    private func useSegmenter(_ segmenter: NativePersonSegmenting) {
        let previous = SpeakerMatte.segmenterFactory
        SpeakerMatte.segmenterFactory = { segmenter }
        addTeardownBlock { SpeakerMatte.segmenterFactory = previous }
    }

    private struct Scene {
        let recipe: KriaMediaEngine.EditRecipe
        let urls: [String: URL]
    }

    private func scene(seconds: Double = 3) async throws -> Scene {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }

        let speaker = directory.appendingPathComponent("speaker.mp4")
        try await writeSolidVideo(speaker, color: room, seconds: seconds)
        let picture = directory.appendingPathComponent("background.png")
        try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 64, height: 64)).image { context in
            backdrop.setFill(); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
        }.pngData()).write(to: picture)

        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "speaker.mp4", fingerprint: try SHA256Fingerprinter().fingerprint(file: speaker), duration: seconds),
            url: speaker)
        let background = ResolvedEditorSource(clipIndex: -1, mediaID: "1a2b",
            asset: MediaAsset(id: EditorSpeakerBackground.assetID(forMediaID: "1a2b"), relativePath: "background.png",
                              fingerprint: try SHA256Fingerprinter().fingerprint(file: picture)),
            url: picture, visualGeneration: "7")
        var document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: seconds)])
        document.speakerBackground = EditorSpeakerBackground(mediaID: "1a2b", kind: .image)
        let clipID = UUID()
        let clip = EditorClip(id: clipID, assetID: clipID, sourceClipIndex: 0, start: 0, end: seconds, trimIn: 0, trimOut: seconds,
                              sourceDuration: seconds, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let program = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                           mediaSources: [EditorSpeakerBackground.sourceKey(forMediaID: "1a2b"): background])
        return Scene(recipe: program.recipe, urls: program.assetURLs)
    }

    /// The editor names its speaker track after the background's `subjectTrackID`; the engine cuts out only the
    /// clips of that track, so any other name draws the edit with no background at all and no error.
    func testTheCompiledRecipeHasTheTrackTheBackgroundIsBoundTo() async throws {
        let scene = try await scene()
        let layer = try XCTUnwrap(scene.recipe.speakerBackground)
        XCTAssertTrue(scene.recipe.tracks.contains { $0.id == layer.subjectTrackID && $0.kind == .video },
                      "tracks: \(scene.recipe.tracks.map(\.id)), background bound to \(layer.subjectTrackID)")
        XCTAssertFalse(AVPlayerPreviewComposer.speakerClipIDs(in: scene.recipe).isEmpty, "no clip would be cut out")
    }

    /// What the creator sees: the room around the speaker is replaced by the background, and the speaker stays.
    func testTheLivePreviewShowsTheBackgroundAroundTheSpeaker() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await scene()
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = live.preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let image = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image
        let corner = pixel(image, fx: 0.04, fy: 0.04), center = pixel(image, fx: 0.5, fy: 0.5)
        XCTAssertTrue(isNear(corner, backdrop), "the corner is the background, got \(corner)")
        XCTAssertTrue(isNear(center, room), "the speaker stays in the middle, got \(center)")
    }

    /// The editor's Download and Share route exports the compiled preview recipe through the real exporter
    /// (`exportDisplayedSourcePreview`), so an unsaved edit leaves the phone with the background it shows.
    func testExportingTheCompiledPreviewRecipeDrawsTheBackground() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await scene()
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let snapshot = live.exportSnapshot()
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("export-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let movie = directory.appendingPathComponent("current-preview.mp4")
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none
        ).export(recipe: snapshot.recipe, assetURLs: snapshot.assetURLs, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed)

        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: movie))
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let image = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image
        let corner = pixel(image, fx: 0.04, fy: 0.04), center = pixel(image, fx: 0.5, fy: 0.5)
        XCTAssertTrue(isNear(corner, backdrop), "the exported corner is the background, got \(corner)")
        XCTAssertTrue(isNear(center, room), "the exported speaker stays in the middle, got \(center)")
    }

    /// An editor update that changes nothing about the background (a caption keystroke, a chat draft) stays in
    /// place and keeps both the background and the trace of the composition it belongs to.
    func testAnInPlaceUpdateKeepsTheBackgroundAndItsTrace() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await scene()
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let trace = try XCTUnwrap(live.speakerTrace)
        XCTAssertEqual(trace.snapshot().speakerClips, 1)
        try live.updateText(recipe: scene.recipe, assetURLs: scene.urls)
        XCTAssertTrue(live.speakerTrace === trace, "the matte, and so its trace, carry over")
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = live.preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let image = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image
        XCTAssertTrue(isNear(pixel(image, fx: 0.04, fy: 0.04), backdrop), "the corner is still the background")
        XCTAssertGreaterThan(trace.snapshot().counts["cutout"] ?? 0, 0)
        XCTAssertNil(trace.snapshot().counts["no_speaker_layer"])
    }

    /// The creator's report: the background was there for the first seconds and gone after about four. Plays the live
    /// preview for real past six seconds on an AVPlayer, applies an unrelated editor update mid-playback (the
    /// in-place path every caption keystroke and chat draft takes), and samples the frames the player shows: every
    /// one has the background, the player item is never replaced, and the trace saw no frame without it.
    func testPlayingPastSixSecondsKeepsTheBackgroundOnEveryFrame() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await scene(seconds: 8)
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let item = live.preview.playerItem
        let output = AVPlayerItemVideoOutput(pixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        item.add(output)
        let player = AVPlayer(playerItem: item)
        player.isMuted = true
        player.play()
        defer { player.pause() }

        var samples: [(time: Double, corner: [Int], center: [Int])] = []
        var updated = false
        let deadline = Date().addingTimeInterval(40)
        while Date() < deadline, player.currentTime().seconds < 6.5 {
            try await Task.sleep(for: .milliseconds(60))
            let now = player.currentTime().seconds
            if !updated, now > 2 {
                try live.updateText(recipe: scene.recipe, assetURLs: scene.urls)
                updated = true
            }
            let time = item.currentTime()
            guard output.hasNewPixelBuffer(forItemTime: time), let buffer = output.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: nil) else { continue }
            samples.append((now, bgraPixel(buffer, fx: 0.04, fy: 0.04), bgraPixel(buffer, fx: 0.5, fy: 0.5)))
        }
        XCTAssertTrue(updated, "playback got past two seconds")
        XCTAssertGreaterThan(player.currentTime().seconds, 6, "and past six")
        XCTAssertTrue(player.currentItem === item, "the player item was never replaced")
        XCTAssertGreaterThan(samples.filter { $0.time > 4.5 }.count, 5, "frames after the fourth second were sampled")
        let wrong = samples.filter { !isNear($0.corner, backdrop) || !isNear($0.center, room) }
        XCTAssertTrue(wrong.isEmpty, "frames without the background: \(wrong.prefix(5).map { ($0.time, $0.corner) })")
        let snapshot = try XCTUnwrap(live.speakerTrace).snapshot()
        XCTAssertEqual(snapshot.longestHiddenRun, 0, snapshot.summary)
        XCTAssertNil(snapshot.counts["original.error"], snapshot.summary)
        XCTAssertGreaterThan(snapshot.counts["cutout"] ?? 0, 30, snapshot.summary)
    }

    /// The report that reaches the server names why the background was not drawn, by fixed labels and counts only.
    func testTheHiddenBackgroundReportCarriesOnlyFixedLabelsAndCounts() throws {
        let trace = SpeakerBackgroundTrace()
        trace.noteBuilt(speakerClips: 0)
        for index in 0..<3 { trace.record(.noSpeakerLayer, at: Double(index)) }
        let report = NativePreviewDiagnostics.speakerBackgroundHidden(.noSpeakerClip, snapshot: trace.snapshot(), sourceState: "ready")
        XCTAssertEqual(report.errorDomain, "preview.speaker_background.no_speaker_clip")
        XCTAssertEqual(report.errorCode, 3, "the longest run of frames without the background")
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(report)) as? [String: Any])
        let line = try XCTUnwrap(json["trace"] as? String)
        XCTAssertTrue(line.hasPrefix("clips=0 hidden_run=3 no_speaker_layer=3"), line)
        XCTAssertLessThanOrEqual(line.count, 600)

        let plain = NativePreviewDiagnostics.previewFailure(error: MediaEngineError.exportFailed, sourceState: "ready")
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(plain)) as? [String: Any])
        XCTAssertNil(wire["trace"], "a report without a trace is byte-identical to before")
    }

    // MARK: - Helpers

    private func pixel(_ image: CGImage, fx: Double, fy: Double) -> [Int] {
        var bytes = [UInt8](repeating: 0, count: 4)
        let context = CGContext(data: &bytes, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        let x = Double(image.width) * fx, y = Double(image.height) * fy
        context.draw(image, in: CGRect(x: -x, y: y - Double(image.height) + 1, width: Double(image.width), height: Double(image.height)))
        return bytes.prefix(3).map(Int.init)
    }

    private func bgraPixel(_ buffer: CVPixelBuffer, fx: Double, fy: Double) -> [Int] {
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer)?.assumingMemoryBound(to: UInt8.self) else { return [-999, -999, -999] }
        let x = min(CVPixelBufferGetWidth(buffer) - 1, Int(fx * Double(CVPixelBufferGetWidth(buffer))))
        let y = min(CVPixelBufferGetHeight(buffer) - 1, Int(fy * Double(CVPixelBufferGetHeight(buffer))))
        let offset = y * CVPixelBufferGetBytesPerRow(buffer) + x * 4
        return [Int(base[offset + 2]), Int(base[offset + 1]), Int(base[offset])]
    }

    private func isNear(_ pixel: [Int], _ color: UIColor, tolerance: Int = 70) -> Bool {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        color.getRed(&r, green: &g, blue: &b, alpha: &a)
        return zip(pixel, [r, g, b]).allSatisfy { abs($0 - Int($1 * 255)) <= tolerance }
    }

    private func writeSolidVideo(_ url: URL, color: UIColor, seconds: Double) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 90, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
                kCVPixelBufferWidthKey as String: 90, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<Int(seconds * 30) {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer); CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 90, height: 160, bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(color.cgColor); context.fill(CGRect(x: 0, y: 0, width: 90, height: 160))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished(); await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
    }
}
