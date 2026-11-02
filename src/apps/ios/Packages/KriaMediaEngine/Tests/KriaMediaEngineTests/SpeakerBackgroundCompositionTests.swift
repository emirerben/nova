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

    // MARK: - KRI-1162: timed backgrounds and the speaker frame. Rows S27-S28 in
    // `docs/reviews/speaker-background-windows-failure-modes.md`.

    /// S28: window A shows from 0 to 2 s, window B from 2 to 4 s, and the fill everywhere else, identically in
    /// the preview and in the exported file. The speaker stays in front of every one of them.
    @MainActor func testEachWindowShowsItsOwnBackground() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let red = CGColor(red: 1, green: 0, blue: 0, alpha: 1), yellow = CGColor(red: 1, green: 1, blue: 0, alpha: 1)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 6)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 6,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                      windows: [SpeakerTestWindow(url: try solidSpeakerTestImage(directory: directory, name: "a", color: red), start: 0, end: 2),
                                                SpeakerTestWindow(url: try solidSpeakerTestImage(directory: directory, name: "b", color: yellow), start: 2, end: 4)])
        XCTAssertTrue(scene.recipe.effectiveCapabilities.contains(.speakerBackgroundWindows))
        let exported = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory)
        for (time, expected) in [(1.0, red), (3.0, yellow), (5.0, SpeakerColor.background)] {
            let preview = try await previewFrame(scene.recipe, urls: scene.urls, at: time)
            let file = try await fileFrame(exported, at: time)
            for (name, frame) in [("preview", preview), ("export", file)] {
                for (fx, fy) in corners {
                    XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), expected), "\(name) at \(time) s, corner (\(fx), \(fy)): \(frame.rgb(fx: fx, fy: fy))")
                }
                XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person), "\(name) at \(time) s keeps the speaker")
            }
        }
    }

    /// S27: the speaker is cut out at full size, THEN drawn small in the bottom-right corner (the
    /// `small_bottom_right` preset: width 0.42, centre 0.76 / 0.80). The speaker colour appears only inside
    /// the frame box and the old person centre shows the background, in the preview and in the export; the
    /// room around the speaker never comes along. With no frame the same edit is unchanged.
    @MainActor func testSpeakerFramePutsTheCutOutInTheCorner() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let background = SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory))
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: background,
                                      speakerFrame: SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80))
        XCTAssertTrue(scene.recipe.effectiveCapabilities.isSuperset(of: [.speakerBackground, .speakerBackgroundWindows]))
        let exported = try await fileFrame(try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory), at: 1)
        let preview = try await previewFrame(scene.recipe, urls: scene.urls, at: 1)
        // The frame box: 0.42 of the width and (uniform scale) of the height, centred at (0.76, 0.80) from the top-left.
        // Inside it the person stands from 30% to 70% of the box width and up to 75% of its height from the bottom.
        let boxWidth = 0.42
        let personInFrame = (0.76, 0.80 + 0.21 - 0.5 * 0.75 * 0.42)
        for (name, frame) in [("preview", preview), ("export", exported)] {
            XCTAssertTrue(isColor(frame.rgb(fx: personInFrame.0, fy: personInFrame.1), SpeakerColor.person), "\(name): the speaker is in the corner: \(frame.rgb(fx: personInFrame.0, fy: personInFrame.1))")
            XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.background), "\(name): the old person centre is the background: \(frame.rgb(fx: personCenter.0, fy: personCenter.1))")
            // Magenta room inside the frame box (left of the person, still within the box) is cut away, not drawn.
            XCTAssertTrue(isColor(frame.rgb(fx: 0.76 - boxWidth * 0.4, fy: personInFrame.1), SpeakerColor.background), "\(name): the room inside the box is the background")
            for (fx, fy) in corners.dropLast() + [(0.5, 0.3), (0.2, 0.7), (0.3, 0.9)] {
                XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), SpeakerColor.background), "\(name): (\(fx), \(fy)) is the background: \(frame.rgb(fx: fx, fy: fy))")
            }
            // Speaker colour appears nowhere outside the box: scan the whole frame.
            for gy in stride(from: 0.02, to: 1, by: 0.04) {
                for gx in stride(from: 0.02, to: 1, by: 0.04) {
                    let insideBox = gx >= 0.55 && gx <= 0.97 && gy >= 0.58
                    if !insideBox { XCTAssertFalse(isColor(frame.rgb(fx: gx, fy: gy), SpeakerColor.person, tolerance: 40), "\(name): speaker colour outside the box at (\(gx), \(gy))") }
                }
            }
        }
        for (fx, fy) in corners + [personInFrame, personCenter, (0.15, 0.5)] {
            let a = preview.rgb(fx: fx, fy: fy), b = exported.rgb(fx: fx, fy: fy)
            XCTAssertTrue(zip(a, b).allSatisfy { abs($0 - $1) <= 24 }, "preview \(a) and export \(b) differ at (\(fx), \(fy))")
        }
    }

    /// A frame without windows keeps the single whole-edit background behind the small speaker, and windows
    /// without a frame keep the speaker full-frame.
    @MainActor func testFrameAndWindowsAreIndependent() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let red = CGColor(red: 1, green: 0, blue: 0, alpha: 1)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let windowOnly = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                           background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                           windows: [SpeakerTestWindow(url: try solidSpeakerTestImage(directory: directory, name: "a", color: red), start: 0, end: 1)])
        let early = try await previewFrame(windowOnly.recipe, urls: windowOnly.urls, at: 0.5)
        XCTAssertTrue(isColor(early.rgb(fx: 0.04, fy: 0.04), red))
        XCTAssertTrue(isColor(early.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person), "no frame: full-frame speaker")
        let late = try await previewFrame(windowOnly.recipe, urls: windowOnly.urls, at: 1.5)
        XCTAssertTrue(isColor(late.rgb(fx: 0.04, fy: 0.04), SpeakerColor.background), "the fill after the window")
    }
}
#endif
