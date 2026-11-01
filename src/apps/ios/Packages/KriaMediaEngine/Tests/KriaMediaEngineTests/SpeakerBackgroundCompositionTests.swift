#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// The real build path for a speaker background (KRI-1116): `AVPlayerPreviewComposer` ->
/// `RecipeVideoCompositor` -> `SpeakerMatte`, sampled through `AVAssetImageGenerator` (the preview) and
/// `AVFoundationLocalExporter` (the export). Vision is replaced by `GreenPersonSegmenter`, which segments
/// whatever frame the compositor hands it. Rows: `docs/reviews/speaker-background-failure-modes.md`.
final class SpeakerBackgroundCompositionTests: XCTestCase {
    private let canvas = Canvas(width: 120, height: 200)
    private let corners: [(Double, Double)] = [(0.04, 0.04), (0.96, 0.04), (0.04, 0.96), (0.96, 0.96)]
    private let personCenter = (0.5, 0.6)

    /// S15: the background shows around the speaker and the speaker stays, identically in the preview
    /// composition and in the exported file, under the shipping config and the device fallback.
    @MainActor func testSpeakerBackgroundInPreviewAndExport() async throws {
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        XCTAssertTrue(scene.recipe.effectiveCapabilities.contains(.speakerBackground))
        for (config, settings) in speakerMatteConfigsUnderTest {
            let segmenter = GreenPersonSegmenter()
            useSpeakerSegmenter(self, segmenter)
            useSpeakerConfig(self, settings)
            let preview = try await previewFrame(scene.recipe, urls: scene.urls, at: 1)
            let exported = try await fileFrame(try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: "export-\(config.count)"), at: 1)
            for (name, frame) in [("\(config) preview", preview), ("\(config) export", exported)] {
                for (fx, fy) in corners {
                    XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), SpeakerColor.background), "\(name) corner (\(fx), \(fy)) is the background: \(frame.rgb(fx: fx, fy: fy))")
                }
                XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person), "\(name) keeps the speaker")
            }
            for (fx, fy) in corners + [personCenter, (0.15, 0.5), (0.85, 0.5)] {
                let a = preview.rgb(fx: fx, fy: fy), b = exported.rgb(fx: fx, fy: fy)
                XCTAssertTrue(zip(a, b).allSatisfy { abs($0 - $1) <= 24 }, "\(config): preview \(a) and export \(b) differ at (\(fx), \(fy))")
            }
            XCTAssertGreaterThan(segmenter.calls, 0)
        }
    }

    /// Negative control: the same edit without a background renders the room and never builds a matte.
    @MainActor func testRecipeWithoutABackgroundRendersTheRoom() async throws {
        let segmenter = GreenPersonSegmenter()
        useSpeakerSegmenter(self, segmenter)
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: nil)
        let frame = try await previewFrame(scene.recipe, urls: scene.urls, at: 1)
        // H.264's 4:2:0 chroma lifts saturated magenta's green to about 73.
        for (fx, fy) in corners { XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), SpeakerColor.room, tolerance: 90), "corner (\(fx), \(fy)) is the room: \(frame.rgb(fx: fx, fy: fy))") }
        XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person))
        XCTAssertEqual(segmenter.calls, 0, "no background, no segmentation")
    }

    /// S12: the matte reads the speaker layer after its transform. Zoomed 2x (a face fill), the person
    /// spans 10% to 90% of the width; a point at 15% is inside the zoomed person but outside the raw clip's.
    @MainActor func testFaceFilledSpeakerIsMatted() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, transform: MediaTransform(scale: 2),
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        let frame = try await previewFrame(scene.recipe, urls: scene.urls, at: 1)
        XCTAssertTrue(isColor(frame.rgb(fx: 0.15, fy: 0.5), SpeakerColor.person), "the zoomed speaker is kept: \(frame.rgb(fx: 0.15, fy: 0.5))")
        XCTAssertTrue(isColor(frame.rgb(fx: 0.03, fy: 0.5), SpeakerColor.background), "outside the zoomed speaker is the background")
    }

    /// The ending clip shares the speaker track but is Visuals media, not the speaker: it plays whole,
    /// never cut out and never covered by the background.
    @MainActor func testEndingClipOnTheSpeakerTrackIsNotMatted() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let ending = try await writeSpeakerTestVideo(directory: directory, name: "ending", size: (canvas.width, canvas.height), seconds: 1) { context, _ in
            context.setFillColor(CGColor(red: 1, green: 0, blue: 0, alpha: 1))
            context.fill(CGRect(x: 0, y: 0, width: 120, height: 200))
        }
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                      endingClip: TimelineClip(id: "ending", sourceAssetID: "visual-ending", sourceDuration: 1, timelineStart: 2, volume: 0),
                                      visuals: ["visual-ending": (ending, .video)])
        let frame = try await previewFrame(scene.recipe, urls: scene.urls, at: 2.5)
        for (fx, fy) in corners + [personCenter] {
            XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), CGColor(red: 1, green: 0, blue: 0, alpha: 1)), "the ending clip fills (\(fx), \(fy))")
        }
    }

    /// Any change to the background (source start, loop, removal) is refused in place, so the editor
    /// takes the full rebuild that re-inserts the background track.
    @MainActor func testBackgroundChangeTakesTheFullRebuild() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let video = try await writeSpeakerTestVideo(directory: directory, name: "loop", size: (64, 64), seconds: 1) { context, _ in
            context.setFillColor(SpeakerColor.background)
            context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
        }
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: video, kind: .video))
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let layer = try XCTUnwrap(scene.recipe.speakerBackground)
        for changed in [SpeakerBackgroundLayer(assetID: layer.assetID, mediaKind: .video, sourceStart: 0.5),
                        SpeakerBackgroundLayer(assetID: layer.assetID, mediaKind: .video, loop: false), nil] {
            var next = scene.recipe
            next.speakerBackground = changed
            XCTAssertThrowsError(try live.updateText(recipe: next), "\(String(describing: changed)) must rebuild")
        }
        XCTAssertNoThrow(try live.updateText(recipe: scene.recipe), "an unchanged background stays in place")
    }

    /// S15 (live preview): an in-place editor update rebuilds the instructions and keeps the background
    /// layer and its matte.
    @MainActor func testInPlaceEditKeepsTheBackground() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        try live.updateText(recipe: scene.recipe)
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = live.preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let frame = try SpeakerTestFrame(try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image)
        for (fx, fy) in corners { XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), SpeakerColor.background), "corner (\(fx), \(fy)) after the update") }
        XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person))
    }
}
#endif
