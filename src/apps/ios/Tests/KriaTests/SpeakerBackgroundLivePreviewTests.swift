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

    private func scene() async throws -> Scene {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }

        let speaker = directory.appendingPathComponent("speaker.mp4")
        try await writeSolidVideo(speaker, color: room, seconds: 3)
        let picture = directory.appendingPathComponent("background.png")
        try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 64, height: 64)).image { context in
            backdrop.setFill(); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
        }.pngData()).write(to: picture)

        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "speaker.mp4", fingerprint: try SHA256Fingerprinter().fingerprint(file: speaker), duration: 3),
            url: speaker)
        let background = ResolvedEditorSource(clipIndex: -1, mediaID: "1a2b",
            asset: MediaAsset(id: EditorSpeakerBackground.assetID(forMediaID: "1a2b"), relativePath: "background.png",
                              fingerprint: try SHA256Fingerprinter().fingerprint(file: picture)),
            url: picture, visualGeneration: "7")
        var document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3)])
        document.speakerBackground = EditorSpeakerBackground(mediaID: "1a2b", kind: .image)
        let clipID = UUID()
        let clip = EditorClip(id: clipID, assetID: clipID, sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: 3,
                              sourceDuration: 3, slotID: "slot")
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

    // MARK: - Helpers

    private func pixel(_ image: CGImage, fx: Double, fy: Double) -> [Int] {
        var bytes = [UInt8](repeating: 0, count: 4)
        let context = CGContext(data: &bytes, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        let x = Double(image.width) * fx, y = Double(image.height) * fy
        context.draw(image, in: CGRect(x: -x, y: y - Double(image.height) + 1, width: Double(image.width), height: Double(image.height)))
        return bytes.prefix(3).map(Int.init)
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
