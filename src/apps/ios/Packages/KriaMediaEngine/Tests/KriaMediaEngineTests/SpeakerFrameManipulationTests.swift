#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// Moving and resizing the corner speaker by hand in the editor. Rows SF2, SF4, SF5, SF7, SF8 and SF10 in
/// `docs/reviews/speaker-frame-direct-manipulation-failure-modes.md`. The real build path, sampled through
/// `AVAssetImageGenerator` as the editor samples it; Vision is replaced by `GreenPersonSegmenter`.
final class SpeakerFrameManipulationTests: XCTestCase {
    private let canvas = Canvas(width: 120, height: 200)
    /// The `small_bottom_right` preset: the scaled picture spans x 0.55...0.97 and y 0.59...1.01 of the canvas.
    private let corner = SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80)
    private let red = CGColor(red: 1, green: 0, blue: 0, alpha: 1)

    /// Where the person stands inside a frame: the speaker clip's person sits at (0.5, 0.6) of the full picture.
    private func person(in frame: SpeakerFrame) -> (Double, Double) {
        (frame.xFraction, frame.yFraction + frame.widthFraction * (0.6 - 0.5))
    }

    /// The speaker in a corner over a blue background, with one red card at the top-left drawn after the speaker.
    @MainActor private func scene(frame: SpeakerFrame?, directory: URL) async throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let card = try solidSpeakerTestImage(directory: directory, name: "card", color: red)
        // Scale 0.3, centred 30 px from the left edge and 40 px from the top (`positionY` counts down from the centre).
        let cardClip = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2,
                                    transform: MediaTransform(scale: 0.3, positionX: -30, positionY: -60), volume: 0)
        return try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                 background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                 extraTracks: [TimelineTrack(id: "overlays", kind: .overlay, clips: [cardClip])],
                                 extraAssets: ["card": card], speakerFrame: frame)
    }

    @MainActor private func still(_ live: LivePreviewComposition, _ composition: AVVideoComposition, at seconds: Double) async throws -> CGImage {
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = composition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        return try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image
    }

    /// Premultiplied RGBA at a canvas fraction (0, 0 = top-left).
    private func rgba(_ image: CGImage, fx: Double, fy: Double) throws -> [Int] {
        let width = image.width, height = image.height
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let x = min(width - 1, Int(fx * Double(width))), y = min(height - 1, Int(fy * Double(height)))
        return (0..<4).map { Int(bytes[(y * width + x) * 4 + $0]) }
    }

    /// SF4, SF7, SF8: the still under the speaker, the framed speaker alone and the still above it, composited back,
    /// are the frame the player shows; under a working mask (cut-out) and with no mask at all (the plain speaker drawn
    /// small, S27b). The still under the speaker never holds the speaker, the speaker still is clear outside its box,
    /// and the card drawn after the speaker is only in the still above.
    @MainActor func testSpeakerSlicesRecomposeTheWholeFrame() async throws {
        let cases: [(String, NativePersonSegmenting)] = [
            ("mask", GreenPersonSegmenter()),
            ("no mask", ScriptedSegmenter { _ in throw MediaEngineError.exportFailed }),
        ]
        for (name, segmenter) in cases {
            useSpeakerSegmenter(self, segmenter)
            let directory = try speakerTestDirectory(self)
            let built = try await scene(frame: corner, directory: directory)
            let live = try await LivePreviewComposition(recipe: built.recipe, assetURLs: built.urls)
            let layers = try live.speakerInteractionLayers(time: 1)
            XCTAssertEqual(layers.rect.minX, 0.55, accuracy: 1e-9); XCTAssertEqual(layers.rect.minY, 0.59, accuracy: 1e-9)
            XCTAssertEqual(layers.rect.width, 0.42, accuracy: 1e-9); XCTAssertEqual(layers.rect.height, 0.42, accuracy: 1e-9)
            let whole = try await still(live, try XCTUnwrap(live.preview.playerItem.videoComposition), at: 1)
            let below = try await still(live, layers.below, at: 1)
            let speaker = try await still(live, layers.speaker, at: 1)
            let above = try await still(live, layers.above, at: 1)
            let rebuilt = CIImage(cgImage: above).composited(over: CIImage(cgImage: speaker).composited(over: CIImage(cgImage: below)))
            let recomposed = try XCTUnwrap(CIContext().createCGImage(rebuilt, from: CGRect(x: 0, y: 0, width: whole.width, height: whole.height)))
            for gy in stride(from: 0.03, to: 1, by: 0.06) {
                for gx in stride(from: 0.03, to: 1, by: 0.06) {
                    let a = try rgba(whole, fx: gx, fy: gy).prefix(3), b = try rgba(recomposed, fx: gx, fy: gy).prefix(3)
                    XCTAssertTrue(zip(a, b).allSatisfy { abs($0 - $1) <= 16 }, "\(name): whole \(Array(a)) and recomposed \(Array(b)) differ at (\(gx), \(gy))")
                }
            }
            let standing = person(in: corner)
            XCTAssertTrue(isColor(Array(try rgba(whole, fx: standing.0, fy: standing.1).prefix(3)), SpeakerColor.person), "\(name): the speaker is in the corner")
            XCTAssertTrue(isColor(Array(try rgba(below, fx: standing.0, fy: standing.1).prefix(3)), SpeakerColor.background), "\(name): no speaker under the speaker")
            XCTAssertTrue(isColor(Array(try rgba(speaker, fx: standing.0, fy: standing.1).prefix(3)), SpeakerColor.person), "\(name): the speaker still holds the speaker")
            XCTAssertEqual(try rgba(speaker, fx: 0.2, fy: 0.3)[3], 0, "\(name): the speaker still is clear outside the frame box")
            XCTAssertTrue(isColor(Array(try rgba(whole, fx: 0.25, fy: 0.2).prefix(3)), red), "\(name): the card is drawn")
            XCTAssertTrue(isColor(Array(try rgba(above, fx: 0.25, fy: 0.2).prefix(3)), red), "\(name): the card is above the speaker")
            XCTAssertTrue(isColor(Array(try rgba(below, fx: 0.25, fy: 0.2).prefix(3)), SpeakerColor.background), "\(name): the card is not under the speaker")
            XCTAssertEqual(try rgba(above, fx: standing.0, fy: standing.1)[3], 0, "\(name): nothing above the speaker where it stands")
        }
    }

    /// SF5: moving or resizing an existing frame stays in place (no new player item, the matte carries over) and the
    /// next rendered frame draws the speaker at the new place, the old corner showing the background.
    @MainActor func testMovingTheFrameUpdatesInPlace() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let built = try await scene(frame: corner, directory: directory)
        let live = try await LivePreviewComposition(recipe: built.recipe, assetURLs: built.urls)
        let item = live.preview.playerItem
        let moved = SpeakerFrame(widthFraction: 0.6, xFraction: 0.35, yFraction: 0.55)
        var next = built.recipe
        next.speakerBackground = try XCTUnwrap(next.speakerBackground).replacingSpeakerFrame(moved)
        XCTAssertNoThrow(try live.updateText(recipe: next))
        XCTAssertTrue(live.preview.playerItem === item, "the same player item")
        XCTAssertEqual(live.recipe.speakerBackground?.speakerFrame, moved)
        let frame = try SpeakerTestFrame(try await still(live, try XCTUnwrap(item.videoComposition), at: 1))
        let standing = person(in: moved)
        XCTAssertTrue(isColor(frame.rgb(fx: standing.0, fy: standing.1), SpeakerColor.person), "the speaker moved: \(frame.rgb(fx: standing.0, fy: standing.1))")
        let old = person(in: corner)
        XCTAssertTrue(isColor(frame.rgb(fx: old.0, fy: old.1), SpeakerColor.background), "the old corner is the background: \(frame.rgb(fx: old.0, fy: old.1))")
        let bounds = try XCTUnwrap(live.speakerSelectionBounds(time: 1))
        XCTAssertEqual(bounds.centerX, 0.35, accuracy: 1e-9); XCTAssertEqual(bounds.centerY, 0.55, accuracy: 1e-9)
        XCTAssertEqual(bounds.width, 0.6, accuracy: 1e-9)
    }

    /// SF4: a hand-placed frame (not a preset; bigger, left of centre) is exported exactly where the live preview drew it
    /// after the in-place move: the speaker at the new place, the background at the old corner, preview = export.
    @MainActor func testAHandPlacedFrameExportsWhereThePreviewDrawsIt() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let built = try await scene(frame: corner, directory: directory)
        let live = try await LivePreviewComposition(recipe: built.recipe, assetURLs: built.urls)
        let placed = SpeakerFrame(widthFraction: 0.63, xFraction: 0.38, yFraction: 0.57)
        var next = built.recipe
        next.speakerBackground = try XCTUnwrap(next.speakerBackground).replacingSpeakerFrame(placed)
        try live.updateText(recipe: next)
        let preview = try SpeakerTestFrame(try await still(live, try XCTUnwrap(live.preview.playerItem.videoComposition), at: 1))
        let snapshot = live.exportSnapshot()
        let exported = try await fileFrame(try await exportSpeakerRecipe(snapshot.recipe, urls: snapshot.assetURLs, directory: directory), at: 1)
        let standing = person(in: placed), old = person(in: corner)
        for (name, frame) in [("preview", preview), ("export", exported)] {
            XCTAssertTrue(isColor(frame.rgb(fx: standing.0, fy: standing.1), SpeakerColor.person), "\(name): the speaker is where the finger left them")
            XCTAssertTrue(isColor(frame.rgb(fx: old.0 + 0.1, fy: old.1 + 0.1), SpeakerColor.background), "\(name): the old corner is the background")
        }
        // Points away from every edge (H.264 4:2:0 softens edges in the file): canvas corners, where the speaker stands,
        // the old corner, the card above the speaker, open background.
        for (fx, fy) in [(0.96, 0.04), (0.96, 0.96), (0.04, 0.96), standing, (old.0 + 0.1, old.1 + 0.1), (0.25, 0.2), (0.85, 0.5)] {
            let a = preview.rgb(fx: fx, fy: fy), b = exported.rgb(fx: fx, fy: fy)
            XCTAssertTrue(zip(a, b).allSatisfy { abs($0 - $1) <= 24 }, "preview \(a) and export \(b) differ at (\(fx), \(fy))")
        }
        XCTAssertTrue(isColor(exported.rgb(fx: 0.25, fy: 0.2), red), "the card stays above the speaker in the file")
    }

    /// SF5: adding or removing the frame (Full <-> corner) is not a move and still takes the full rebuild; so does a
    /// moved frame that also changes anything else about the background.
    @MainActor func testAddingOrRemovingTheFrameTakesTheFullRebuild() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let framed = try await scene(frame: corner, directory: directory)
        let live = try await LivePreviewComposition(recipe: framed.recipe, assetURLs: framed.urls)
        let layer = try XCTUnwrap(framed.recipe.speakerBackground)
        let moved = SpeakerFrame(widthFraction: 0.5, xFraction: 0.3, yFraction: 0.3)
        for changed in [layer.replacingSpeakerFrame(nil),
                        SpeakerBackgroundLayer(assetID: try XCTUnwrap(layer.assetID), mediaKind: .image, loop: false, speakerFrame: moved)] {
            var next = framed.recipe
            next.speakerBackground = changed
            XCTAssertThrowsError(try live.updateText(recipe: next)) { error in
                XCTAssertEqual((error as? NativePreviewFeatureError)?.feature, "LivePreviewComposition-speaker-background")
            }
        }
        let full = try await scene(frame: nil, directory: try speakerTestDirectory(self))
        let fullLive = try await LivePreviewComposition(recipe: full.recipe, assetURLs: full.urls)
        var cornered = full.recipe
        cornered.speakerBackground = try XCTUnwrap(cornered.speakerBackground).replacingSpeakerFrame(corner)
        XCTAssertThrowsError(try fullLive.updateText(recipe: cornered), "full -> corner rebuilds")
    }

    /// SF2, SF10: the selection box is the scaled speaker picture clipped to the canvas, only while a frame is set and
    /// a speaker clip is drawn: none for a full-frame speaker, none over the ending clip.
    @MainActor func testSpeakerSelectionBoundsFollowTheFrameAndTheSpeakerClip() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let ending = try await writeSpeakerTestVideo(directory: directory, name: "ending", size: (canvas.width, canvas.height), seconds: 1) { context, _ in
            context.setFillColor(CGColor(red: 1, green: 0, blue: 0, alpha: 1))
            context.fill(CGRect(x: 0, y: 0, width: 120, height: 200))
        }
        let background = SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory))
        let endingClip = TimelineClip(id: "ending", sourceAssetID: "visual-ending", sourceDuration: 1, timelineStart: 2, volume: 0)
        let built = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: background, endingClip: endingClip,
                                      visuals: ["visual-ending": (ending, .video)], speakerFrame: corner)
        let live = try await LivePreviewComposition(recipe: built.recipe, assetURLs: built.urls)
        let bounds = try XCTUnwrap(live.speakerSelectionBounds(time: 1))
        // x 0.55...0.97 fits; y 0.59...1.01 is clipped at the bottom edge.
        XCTAssertEqual(bounds.width, 0.42, accuracy: 1e-9)
        XCTAssertEqual(bounds.height, 0.41, accuracy: 1e-9)
        XCTAssertEqual(bounds.centerX, 0.76, accuracy: 1e-9)
        XCTAssertEqual(bounds.centerY, 0.795, accuracy: 1e-9)
        XCTAssertEqual(bounds.rotationDegrees, 0)
        XCTAssertNil(live.speakerSelectionBounds(time: 2.5), "the ending clip has no speaker to select")
        XCTAssertThrowsError(try live.speakerInteractionLayers(time: 2.5))
        let full = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: background)
        let fullLive = try await LivePreviewComposition(recipe: full.recipe, assetURLs: full.urls)
        XCTAssertNil(fullLive.speakerSelectionBounds(time: 1), "a full-frame speaker is not selectable")
    }

    /// S84, S85 (`docs/reviews/speaker-background-windows-failure-modes.md`): with a full-screen opening (Qendresa
    /// item 16) the corner speaker is selectable only after the switch, and moving the frame by hand stays the in-place
    /// update and keeps the opening: still full-frame inside it, the speaker at the new place after it.
    @MainActor func testAnOpeningIsNotSelectableAndSurvivesAMovedFrame() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let opening = [SpeakerFullScreenWindow(start: 0, end: 0.8)]
        let built = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                      speakerFrame: corner, fullScreenWindows: opening)
        let live = try await LivePreviewComposition(recipe: built.recipe, assetURLs: built.urls)
        XCTAssertNil(live.speakerSelectionBounds(time: 0.4), "no corner box while she is full screen")
        XCTAssertThrowsError(try live.speakerInteractionLayers(time: 0.4), "no drag split inside the opening")
        XCTAssertNotNil(live.speakerSelectionBounds(time: 1.2), "the corner speaker is selectable after the switch")
        XCTAssertNoThrow(try live.speakerInteractionLayers(time: 1.2))

        let moved = SpeakerFrame(widthFraction: 0.6, xFraction: 0.35, yFraction: 0.55)
        let layer = try XCTUnwrap(built.recipe.speakerBackground)
        XCTAssertEqual(layer.replacingSpeakerFrame(moved).fullScreenWindows, opening, "a moved frame keeps the opening")
        var next = built.recipe
        next.speakerBackground = layer.replacingSpeakerFrame(moved)
        let item = live.preview.playerItem
        XCTAssertNoThrow(try live.updateText(recipe: next), "a moved frame with an opening is still only a move")
        XCTAssertTrue(live.preview.playerItem === item, "the same player item")
        XCTAssertEqual(live.recipe.speakerBackground?.fullScreenWindows, opening)
        let composition = try XCTUnwrap(item.videoComposition)
        let inside = try SpeakerTestFrame(try await still(live, composition, at: 0.4))
        let old = person(in: corner)
        // The raw take is decoded H.264 here, so its room is checked as the opening's own tests check it.
        for (fx, fy) in [(0.04, 0.04), (old.0 + 0.1, old.1 + 0.1)] {
            XCTAssertTrue(isColor(inside.rgb(fx: fx, fy: fy), SpeakerColor.room, tolerance: 90),
                          "inside the opening her own room fills the frame at (\(fx), \(fy)): \(inside.rgb(fx: fx, fy: fy))")
        }
        let after = try SpeakerTestFrame(try await still(live, composition, at: 1.2))
        let standing = person(in: moved)
        XCTAssertTrue(isColor(after.rgb(fx: standing.0, fy: standing.1), SpeakerColor.person), "after the switch the speaker is at the new place")
        XCTAssertTrue(isColor(after.rgb(fx: old.0 + 0.1, fy: old.1 + 0.1), SpeakerColor.background), "and the old corner is the background")
    }
}
#endif
