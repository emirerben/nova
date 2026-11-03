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
    private let green = UIColor(red: 0, green: 1, blue: 0, alpha: 1)
    private let yellow = UIColor(red: 1, green: 1, blue: 0, alpha: 1)

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

    /// A timed background of the Green 3 shape: a pool photo of one colour that takes the fill's place from `start` to `end`.
    private typealias TimedBackground = (mediaID: String, color: UIColor, start: Double, end: Double)

    /// A speaker clip of `seconds` over the blue fill. `windows` add timed pool photos behind the speaker and
    /// `speakerFrame` draws the speaker small; with neither it is the plain full-size, fill-only edit.
    private func scene(seconds: Double = 3, windows: [TimedBackground] = [], speakerFrame: EditorSpeakerFrame? = nil) async throws -> Scene {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }

        let speaker = directory.appendingPathComponent("speaker.mp4")
        try await writeSolidVideo(speaker, color: room, seconds: seconds)
        let picture = directory.appendingPathComponent("background.png")
        try writeSolidPicture(backdrop, to: picture)

        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "speaker.mp4", fingerprint: try SHA256Fingerprinter().fingerprint(file: speaker), duration: seconds),
            url: speaker)
        let background = ResolvedEditorSource(clipIndex: -1, mediaID: "1a2b",
            asset: MediaAsset(id: EditorSpeakerBackground.assetID(forMediaID: "1a2b"), relativePath: "background.png",
                              fingerprint: try SHA256Fingerprinter().fingerprint(file: picture)),
            url: picture, visualGeneration: "7")
        var mediaSources = [EditorSpeakerBackground.sourceKey(forMediaID: "1a2b"): background]
        var blocks: [EditorSpeakerBackgroundWindow] = []
        for (index, window) in windows.enumerated() {
            let file = directory.appendingPathComponent("window-\(index).png")
            try writeSolidPicture(window.color, to: file)
            mediaSources[EditorSpeakerBackground.sourceKey(forMediaID: window.mediaID)] = ResolvedEditorSource(
                clipIndex: -1, mediaID: window.mediaID,
                asset: MediaAsset(id: EditorSpeakerBackground.assetID(forMediaID: window.mediaID), relativePath: "window-\(index).png",
                                  fingerprint: try SHA256Fingerprinter().fingerprint(file: file)),
                url: file, visualGeneration: "7")
            blocks.append(EditorSpeakerBackgroundWindow(localID: EditorSpeakerBackground.localID(index), mediaID: window.mediaID,
                                                        kind: .image, startS: window.start, endS: window.end))
        }
        var document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: seconds)])
        document.speakerBackground = EditorSpeakerBackground(mediaID: "1a2b", kind: .image, windows: blocks, speakerFrame: speakerFrame)
        let clipID = UUID()
        let clip = EditorClip(id: clipID, assetID: clipID, sourceClipIndex: 0, start: 0, end: seconds, trimIn: 0, trimOut: seconds,
                              sourceDuration: seconds, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let program = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                           mediaSources: mediaSources)
        return Scene(recipe: program.recipe, urls: program.assetURLs)
    }

    /// The Green 3 edit (KRI-1179): a 4 s speaker over the blue fill, a green window for the first 1.5 s, a yellow one
    /// for the next 1.5 s (the last second is uncovered, so the fill shows), and the speaker drawn small bottom-right.
    private func greenThreeScene() async throws -> Scene {
        try await scene(seconds: 4, windows: [(greenThreeWindowIDs[0], green, 0, 1.5), (greenThreeWindowIDs[1], yellow, 1.5, 3.0)],
                        speakerFrame: EditorSpeakerFrame.preset("small_bottom_right"))
    }

    /// Pool media ids of the two windows (the fill is "1a2b").
    private let greenThreeWindowIDs = ["3c4d", "5e6f"]

    /// What must be behind the speaker at each sampled second of the Green 3 edit.
    private var greenThreeFrames: [(time: Double, background: UIColor)] { [(0.75, green), (2.25, yellow), (3.5, backdrop)] }

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
        let movie = try await exportLikeDownload(live)

        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: movie))
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let image = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image
        let corner = pixel(image, fx: 0.04, fy: 0.04), center = pixel(image, fx: 0.5, fy: 0.5)
        XCTAssertTrue(isNear(corner, backdrop), "the exported corner is the background, got \(corner)")
        XCTAssertTrue(isNear(center, room), "the exported speaker stays in the middle, got \(center)")
    }

    /// The Green 3 shape (KRI-1179: a fill, timed windows and a `small_bottom_right` speaker). The creator opens such an
    /// edit and the editor preview must show each background in its own seconds with the speaker small in the corner,
    /// not the room with a full-size speaker. Each moment is checked by three pixels: the top-left corner is that
    /// moment's background, the canvas middle is that background too (the speaker is not full size), and the corner
    /// frame holds the speaker.
    func testTheEditorPreviewOfACornerSpeakerWithTimedBackgroundsShowsEachBackground() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await greenThreeScene()
        try assertGreenThreeStructure(of: scene)

        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = live.preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        for frame in greenThreeFrames {
            let image = try await generator.image(at: CMTime(seconds: frame.time, preferredTimescale: 600)).image
            assertCornerSpeaker(image, at: frame.time, over: frame.background, in: "preview")
        }
    }

    /// The same Green 3 edit (KRI-1179) leaving through the editor's Download and Share route: the exported movie
    /// keeps the timed backgrounds and the small corner speaker the preview shows.
    func testExportingTheEditorPreviewOfACornerSpeakerWithTimedBackgroundsDrawsThem() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await greenThreeScene()
        try assertGreenThreeStructure(of: scene)

        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let movie = try await exportLikeDownload(live)
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: movie))
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        for frame in greenThreeFrames {
            let image = try await generator.image(at: CMTime(seconds: frame.time, preferredTimescale: 600)).image
            assertCornerSpeaker(image, at: frame.time, over: frame.background, in: "export")
        }
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
    /// Real time on purpose (about 7 s): the in-place update while an AVPlayer is rendering is the path the creator hit,
    /// and sampling a composition with an image generator does not exercise a playing player. The frame-exact checks
    /// above are the deterministic ones.
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

    /// The report that reaches the server for the original bug (a speaker track the background is not bound to) is built
    /// from a real composition's trace and carries only what the server's `trace` pattern allows.
    func testTheHiddenBackgroundReportOfAMisnamedTrackCarriesOnlyFixedLabelsAndCounts() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await scene()
        var misnamed = scene.recipe
        misnamed.tracks[0].id = "video"
        let live = try await LivePreviewComposition(recipe: misnamed, assetURLs: scene.urls)
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = live.preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        for step in 0..<3 { _ = try await generator.image(at: CMTime(seconds: 0.5 + Double(step) / 30, preferredTimescale: 600)) }

        let trace = try XCTUnwrap(live.speakerTrace)
        XCTAssertEqual(trace.snapshot().speakerClips, 0, "nothing could be cut out")
        let report = NativePreviewDiagnostics.speakerBackgroundHidden(.noSpeakerClip, snapshot: trace.snapshot(), sourceState: "ready")
        XCTAssertEqual(report.errorDomain, "preview.speaker_background.no_speaker_clip")
        XCTAssertEqual(report.errorCode, trace.snapshot().longestHiddenRun)
        XCTAssertGreaterThanOrEqual(report.errorCode, 3, "every sampled frame was drawn without the background")
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(report)) as? [String: Any])
        let line = try XCTUnwrap(json["trace"] as? String)
        XCTAssertTrue(line.hasPrefix("clips=0 hidden_run="), line)
        XCTAssertTrue(line.contains("no_speaker_layer="), line)
        XCTAssertLessThanOrEqual(line.count, 600)
        XCTAssertNotNil(line.range(of: "^[a-z0-9_.=@: -]*$", options: .regularExpression), "fixed labels and counts only: \(line)")

        let plain = NativePreviewDiagnostics.previewFailure(error: MediaEngineError.exportFailed, sourceState: "ready")
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(plain)) as? [String: Any])
        XCTAssertNil(wire["trace"], "a report without a trace is byte-identical to before")
    }

    /// A failure is explained by the trace of the composition that failed, never by another one the editor still holds.
    func testOnlyTheCompositionsOwnFailedItemCarriesItsTrace() async throws {
        useSegmenter(CenterBoxSegmenter())
        let scene = try await scene()
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let other = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        XCTAssertTrue(live.speakerTrace(forFailedItem: live.preview.playerItem) === live.speakerTrace)
        XCTAssertNil(live.speakerTrace(forFailedItem: other.preview.playerItem), "another composition's item")
        XCTAssertNil(live.speakerTrace(forFailedItem: nil), "a failure that produced no item")
    }

    // MARK: - Helpers

    /// The compiled Green 3 recipe is bound the way the engine needs: the speaker track is named after the layer, both windows
    /// and the corner frame survived `EditorSpeakerBackground.layer`, and the fill and every window have bytes in the recipe.
    private func assertGreenThreeStructure(of scene: Scene, file: StaticString = #filePath, line: UInt = #line) throws {
        let layer = try XCTUnwrap(scene.recipe.speakerBackground, "the recipe has a background layer", file: file, line: line)
        XCTAssertTrue(scene.recipe.tracks.contains { $0.id == layer.subjectTrackID && $0.kind == .video },
                      "tracks: \(scene.recipe.tracks.map(\.id)), background bound to \(layer.subjectTrackID)", file: file, line: line)
        XCTAssertFalse(AVPlayerPreviewComposer.speakerClipIDs(in: scene.recipe).isEmpty, "no clip would be cut out", file: file, line: line)
        XCTAssertEqual(layer.windows?.count, 2, "both windows reach the layer", file: file, line: line)
        XCTAssertEqual(layer.windows?.map(\.assetID), greenThreeWindowIDs.map(EditorSpeakerBackground.assetID(forMediaID:)),
                       "each window names its own photo", file: file, line: line)
        XCTAssertEqual(layer.windows?.map(\.start), [0, 1.5], "window starts", file: file, line: line)
        XCTAssertEqual(layer.windows?.map(\.end), [1.5, 3.0], "window ends", file: file, line: line)
        XCTAssertEqual(layer.speakerFrame, SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80),
                       "the corner frame reaches the layer", file: file, line: line)
        let manifestIDs = Set((scene.recipe.assetManifest?.assets ?? []).map(\.id))
        // `compactMap`: KRI-1173 (#2088) makes the fill's asset id optional (a colour fill has none); this fill is a picture.
        let fillIDs = [layer.assetID].compactMap { $0 }
        XCTAssertEqual(fillIDs, [EditorSpeakerBackground.assetID(forMediaID: "1a2b")], "the picture fill reaches the layer", file: file, line: line)
        for assetID in fillIDs + (layer.windows ?? []).map(\.assetID) {
            XCTAssertNotNil(scene.urls[assetID], "\(assetID) has no file in assetURLs", file: file, line: line)
            XCTAssertTrue(scene.recipe.assets.contains { $0.id == assetID }, "\(assetID) is not in the recipe's assets", file: file, line: line)
            XCTAssertTrue(manifestIDs.contains(assetID), "\(assetID) is not in the recipe's asset manifest", file: file, line: line)
        }
    }

    /// One Green 3 frame: the top-left corner and the canvas middle both show `background` (so the speaker is not full
    /// size), and the matted speaker, magenta room colour, is drawn in the `small_bottom_right` frame.
    /// (0.76, 0.80) is that frame's centre, and the centred person box of `CenterBoxSegmenter` (the middle half of the
    /// canvas, cut out at full size and only then scaled by 0.42 and moved into the frame, `RecipeVideoCompositor.framed`)
    /// has its own centre there, with 0.1 of the canvas to spare on every side, so no mask edge or encode blur reaches it.
    private func assertCornerSpeaker(_ image: CGImage, at time: Double, over background: UIColor, in name: String,
                                     file: StaticString = #filePath, line: UInt = #line) {
        let corner = pixel(image, fx: 0.04, fy: 0.04), middle = pixel(image, fx: 0.5, fy: 0.5), speaker = pixel(image, fx: 0.76, fy: 0.80)
        XCTAssertTrue(isNear(corner, background), "\(name) at \(time) s: the top-left corner is that second's background, got \(corner)", file: file, line: line)
        XCTAssertTrue(isNear(middle, background), "\(name) at \(time) s: the middle is the background, so the speaker is small, got \(middle)", file: file, line: line)
        XCTAssertTrue(isNear(speaker, room), "\(name) at \(time) s: the speaker is drawn bottom-right, got \(speaker)", file: file, line: line)
    }

    /// What the editor's Download and Share route exports: the live preview's frozen recipe through the real exporter.
    private func exportLikeDownload(_ live: LivePreviewComposition) async throws -> URL {
        let snapshot = live.exportSnapshot()
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("export-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let movie = directory.appendingPathComponent("current-preview.mp4")
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none
        ).export(recipe: snapshot.recipe, assetURLs: snapshot.assetURLs, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed)
        return movie
    }

    private func writeSolidPicture(_ color: UIColor, to url: URL) throws {
        try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 64, height: 64)).image { context in
            color.setFill(); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
        }.pngData()).write(to: url)
    }

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
