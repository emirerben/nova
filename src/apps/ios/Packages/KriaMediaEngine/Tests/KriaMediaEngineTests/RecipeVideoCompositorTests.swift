#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

final class RecipeVideoCompositorTests: XCTestCase {
    /// Speaker background S8: a full-frame cutaway sits above the matted speaker and covers the speaker AND
    /// the background. It is never handed to the matte, so it is never cut out like a person.
    @MainActor func testCutawayCoversSpeakerAndBackground() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let canvas = Canvas(width: 120, height: 200)
        let red = CGColor(red: 1, green: 0, blue: 0, alpha: 1)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 3)
        let broll = try await writeSpeakerTestVideo(directory: directory, name: "broll", size: (200, 120), seconds: 1) { context, _ in
            context.setFillColor(red)
            context.fill(CGRect(x: 0, y: 0, width: 200, height: 120))
        }
        let scene = try speakerRecipe(
            canvas: canvas, speaker: speaker, duration: 3,
            background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
            extraTracks: [TimelineTrack(id: "talking-head-cutaways", kind: .overlay, clips: [
                TimelineClip(id: "cutaway-0", sourceAssetID: "broll", sourceDuration: 1, timelineStart: 1.5, volume: 0,
                             visualPlacement: VisualMediaPlacement(order: 1, windowStart: 1.5, windowEnd: 2.5)),
            ])],
            extraAssets: ["broll": broll])
        let points: [(Double, Double)] = [(0.04, 0.04), (0.96, 0.04), (0.04, 0.96), (0.96, 0.96), (0.5, 0.6)]

        let before = try await previewFrame(scene.recipe, urls: scene.urls, at: 0.75)
        for (fx, fy) in points.dropLast() { XCTAssertTrue(isColor(before.rgb(fx: fx, fy: fy), SpeakerColor.background), "background at (\(fx), \(fy))") }
        XCTAssertTrue(isColor(before.rgb(fx: 0.5, fy: 0.6), SpeakerColor.person), "speaker before the cutaway")

        let during = try await previewFrame(scene.recipe, urls: scene.urls, at: 2)
        for (fx, fy) in points {
            XCTAssertTrue(isColor(during.rgb(fx: fx, fy: fy), red), "the cutaway covers (\(fx), \(fy)): \(during.rgb(fx: fx, fy: fy))")
        }
    }
}
#endif
