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

    /// The trace tells a bug report apart: an edit whose background names a track that has no clips reports
    /// zero speaker clips and `no_speaker_layer` frames, instead of silently drawing the room.
    @MainActor func testABackgroundBoundToAMissingTrackIsTraced() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        var misnamed = scene.recipe
        misnamed.tracks[0].id = "video"
        for (recipe, expectedClips) in [(scene.recipe, 1), (misnamed, 0)] {
            let live = try await LivePreviewComposition(recipe: recipe, assetURLs: scene.urls)
            let trace = try XCTUnwrap(live.speakerTrace)
            XCTAssertEqual(trace.snapshot().speakerClips, expectedClips)
            let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
            generator.videoComposition = live.preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            _ = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600))
            let counts = trace.snapshot().counts
            XCTAssertEqual(counts["no_speaker_layer"] ?? 0 > 0, expectedClips == 0, "\(counts)")
            XCTAssertEqual(counts["cutout"] ?? 0 > 0, expectedClips == 1, "\(counts)")
        }
    }

    /// The ending clip plays with no speaker on screen, by design (S12). That is not a background that failed to
    /// draw, so it never counts toward the hidden run that gets reported.
    @MainActor func testTheEndingClipIsNotCountedAsAHiddenBackground() async throws {
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
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = live.preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        for seconds in [1.0, 2.5] { _ = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)) }
        let snapshot = try XCTUnwrap(live.speakerTrace).snapshot()
        XCTAssertEqual(snapshot.speakerClips, 1)
        XCTAssertNil(snapshot.counts["no_speaker_layer"], snapshot.summary)
        XCTAssertEqual(snapshot.longestHiddenRun, 0)
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

    /// The editor names its speaker track after the background, so removing the background also renames that track.
    /// The refusal must say it was the background (the log reads "speaker-background"), not "LivePreviewComposition-79".
    @MainActor func testRemovingTheBackgroundIsRefusedAsABackgroundChangeEvenWhenItRenamesTheTrack() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        var next = scene.recipe
        next.speakerBackground = nil
        next.tracks[0].id = "video"
        XCTAssertThrowsError(try live.updateText(recipe: next)) { error in
            XCTAssertEqual((error as? NativePreviewFeatureError)?.feature, "LivePreviewComposition-speaker-background")
        }
    }

    /// A corner speaker with no usable mask is drawn small over the background (`.original` in the compositor), so
    /// the background IS on screen. Counting those frames as hidden reported a failure that was not one.
    @MainActor func testACornerSpeakerWithoutAMaskIsNotCountedAsAHiddenBackground() async throws {
        useSpeakerSegmenter(self, ScriptedSegmenter { _ in throw MediaEngineError.exportFailed })
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let background = SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory))
        for (frame, hiddenRun) in [(nil, true), (SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80), false)] as [(SpeakerFrame?, Bool)] {
            let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: background, speakerFrame: frame)
            let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
            let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
            generator.videoComposition = live.preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            for step in 0..<5 { _ = try await generator.image(at: CMTime(seconds: 0.5 + Double(step) / 30, preferredTimescale: 600)) }
            let snapshot = try XCTUnwrap(live.speakerTrace).snapshot()
            XCTAssertGreaterThan(snapshot.counts["original.error"] ?? 0, 0, "the matte did fail: \(snapshot.summary)")
            XCTAssertEqual(snapshot.longestHiddenRun > 0, hiddenRun, "frame \(String(describing: frame)): \(snapshot.summary)")
        }
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

    // MARK: - KRI-1173: a solid-colour fill. Rows S51-S53 in `docs/reviews/speaker-background-color-failure-modes.md`.

    /// S51: the requested colour shows around the speaker, identically (within 2/255) in the preview
    /// composition and in the exported file, and the speaker stays in front of it.
    @MainActor func testColourFillShowsTheRequestedColourBehindTheSpeaker() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        // White and green sit on the channel extremes, which the Rec.709 video round trip leaves exact (2/255). A
        // mid-tone is lifted by about 7-10/255 by that round trip in this harness (a photo fill is lifted more), the
        // same in the preview and the export, so it is held to a wider band plus preview == export (3/255).
        for (hex, rgb, tolerance) in [("#00FF00", [0, 255, 0], 2), ("#FFFFFF", [255, 255, 255], 2), ("#336699", [0x33, 0x66, 0x99], 16)] {
            let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: nil, colorBackground: hex)
            XCTAssertTrue(scene.recipe.effectiveCapabilities.isSuperset(of: [.speakerBackground, .speakerBackgroundColor]))
            XCTAssertFalse(scene.recipe.effectiveCapabilities.contains(.speakerBackgroundWindows))
            XCTAssertEqual(scene.urls.keys.sorted(), ["speaker"], "a colour needs no asset")
            let preview = try await previewFrame(scene.recipe, urls: scene.urls, at: 1)
            let exported = try await fileFrame(try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: "colour-\(hex.dropFirst())"), at: 1)
            for (fx, fy) in corners {
                let a = preview.rgb(fx: fx, fy: fy), b = exported.rgb(fx: fx, fy: fy)
                XCTAssertTrue(zip(a, b).allSatisfy { abs($0 - $1) <= 3 }, "\(hex): preview \(a) and export \(b) differ at (\(fx), \(fy))")
            }
            for (name, frame) in [("preview", preview), ("export", exported)] {
                for (fx, fy) in corners + [(0.15, 0.5), (0.85, 0.5)] {
                    let pixel = frame.rgb(fx: fx, fy: fy)
                    XCTAssertTrue(zip(pixel, rgb).allSatisfy { abs($0 - $1) <= tolerance }, "\(hex) \(name) at (\(fx), \(fy)) is \(pixel), wanted \(rgb)")
                }
                // A solid person green on a green fill cannot be told apart; the other fills prove the cut-out.
                if hex != "#00FF00" {
                    XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person), "\(hex) \(name) keeps the speaker")
                }
            }
        }
    }

    /// S52: a photo window draws over a colour fill in its span (1-3 s); the colour shows before and after it.
    @MainActor func testWindowsDrawOverAColourFill() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let red = CGColor(red: 1, green: 0, blue: 0, alpha: 1)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 6)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 6, background: nil,
                                      windows: [SpeakerTestWindow(url: try solidSpeakerTestImage(directory: directory, name: "a", color: red), start: 1, end: 3)],
                                      colorBackground: "#FFFFFF")
        XCTAssertTrue(scene.recipe.effectiveCapabilities.isSuperset(of: [.speakerBackground, .speakerBackgroundColor, .speakerBackgroundWindows]))
        let exported = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory)
        for (time, expected) in [(0.5, SpeakerTestColor.white), (2.0, red), (4.0, SpeakerTestColor.white)] {
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

    /// S53 (S3 fallback): a matte that never produces a mask keeps its documented behaviour for a colour fill, as for
    /// a photo: with no speaker frame the plain footage renders and the fill is not drawn; with a speaker frame the
    /// plain speaker layer sits small in the frame and the colour is composed underneath. The speaker is zoomed out
    /// (0.5x) so the area around it shows what is under the layer.
    @MainActor func testColourFillWhenNoMaskIsProduced() async throws {
        useSpeakerSegmenter(self, ScriptedSegmenter(map: nil))
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let plain = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, transform: MediaTransform(scale: 0.5),
                                      background: nil, colorBackground: "#FFFFFF")
        let original = try await previewFrame(plain.recipe, urls: plain.urls, at: 1)
        for (fx, fy) in corners { XCTAssertFalse(isColor(original.rgb(fx: fx, fy: fy), SpeakerTestColor.white, tolerance: 30), "no mask, no frame: the fill is not drawn at (\(fx), \(fy))") }
        XCTAssertTrue(isColor(original.rgb(fx: personCenter.0, fy: 0.5), SpeakerColor.person) || isColor(original.rgb(fx: 0.5, fy: 0.5), SpeakerColor.person),
                      "the plain speaker layer is drawn: \(original.rgb(fx: 0.5, fy: 0.5))")

        let framed = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: nil,
                                       speakerFrame: SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80), colorBackground: "#FFFFFF")
        let withFrame = try await previewFrame(framed.recipe, urls: framed.urls, at: 1)
        for (fx, fy) in corners.dropLast() + [(0.5, 0.3), (0.2, 0.7)] {
            XCTAssertTrue(isColor(withFrame.rgb(fx: fx, fy: fy), SpeakerTestColor.white), "no mask, with a frame: the colour is underneath at (\(fx), \(fy)): \(withFrame.rgb(fx: fx, fy: fy))")
        }
    }

    /// S51 (live preview + trace): the editor's live composition draws a colour fill through the same matte and trace as a
    /// photo: the speaker is cut out frame by frame, nothing is counted as a hidden background, and the colour shows.
    @MainActor func testLivePreviewDrawsAColourFillAndTracesACutout() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: nil, colorBackground: "#FFFFFF")
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        try live.updateText(recipe: scene.recipe)
        let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
        generator.videoComposition = live.preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let frame = try SpeakerTestFrame(try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image)
        for (fx, fy) in corners { XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), SpeakerTestColor.white), "corner (\(fx), \(fy)): \(frame.rgb(fx: fx, fy: fy))") }
        XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person))
        let snapshot = try XCTUnwrap(live.speakerTrace).snapshot()
        XCTAssertEqual(snapshot.speakerClips, 1)
        XCTAssertGreaterThan(snapshot.counts["cutout"] ?? 0, 0, snapshot.summary)
        XCTAssertNil(snapshot.counts["no_speaker_layer"], snapshot.summary)
        XCTAssertEqual(snapshot.longestHiddenRun, 0)
    }

    /// S51 (live preview): a colour fill survives an in-place editor update, and changing the colour takes the full rebuild.
    @MainActor func testColourChangeTakesTheFullRebuild() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2, background: nil, colorBackground: "#FFFFFF")
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        XCTAssertNoThrow(try live.updateText(recipe: scene.recipe), "an unchanged colour stays in place")
        var next = scene.recipe
        next.speakerBackground = SpeakerBackgroundLayer.color("#00FF00")
        XCTAssertThrowsError(try live.updateText(recipe: next), "a new colour must rebuild")
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

    // MARK: - Qendresa item 16: a full-screen opening. Rows S71 and S81 in
    // `docs/reviews/speaker-background-windows-failure-modes.md`.

    private struct SwitchFailure: Error {}
    private let cornerFrame = SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80)
    /// Where the green person sits inside the `small_bottom_right` box (see `testSpeakerFramePutsTheCutOutInTheCorner`).
    private var personInCorner: (Double, Double) { (0.76, 0.80 + 0.21 - 0.5 * 0.75 * 0.42) }

    /// Builds a 2 s edit with a blue photo background, the bottom-right corner frame and a full-screen opening of `0..<opening`.
    @MainActor private func openingScene(opening: Double) async throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        let scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                      speakerFrame: cornerFrame,
                                      fullScreenWindows: [SpeakerFullScreenWindow(start: 0, end: opening)])
        try scene.recipe.validate()
        XCTAssertTrue(scene.recipe.effectiveCapabilities.contains(.speakerFrameWindows))
        return scene
    }

    /// S70/S71: inside the opening the speaker's own take is the whole frame (the magenta room is visible, the blue
    /// background is not, and nobody is cut out), and the segmenter is never asked. After it the background shows and
    /// the speaker sits in the corner.
    @MainActor func testFullScreenWindowDrawsTheRawTakeWithoutMasking() async throws {
        let segmenter = GreenPersonSegmenter()
        useSpeakerSegmenter(self, segmenter)
        let scene = try await openingScene(opening: 1)
        let opening = try await previewFrame(scene.recipe, urls: scene.urls, at: 0.5)
        XCTAssertEqual(segmenter.calls, 0, "no person mask is computed inside the opening")
        XCTAssertTrue(isColor(opening.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person), "the speaker's own colour, full-frame: \(opening.rgb(fx: personCenter.0, fy: personCenter.1))")
        for (fx, fy) in corners {
            XCTAssertTrue(isColor(opening.rgb(fx: fx, fy: fy), SpeakerColor.room), "the room is untouched at (\(fx), \(fy)): \(opening.rgb(fx: fx, fy: fy))")
            XCTAssertFalse(isColor(opening.rgb(fx: fx, fy: fy), SpeakerColor.background), "no background inside the opening at (\(fx), \(fy))")
        }
        let after = try await previewFrame(scene.recipe, urls: scene.urls, at: 1.5)
        XCTAssertGreaterThan(segmenter.calls, 0, "outside the opening the matte runs as before")
        for (fx, fy) in corners.dropLast() + [personCenter] {
            XCTAssertTrue(isColor(after.rgb(fx: fx, fy: fy), SpeakerColor.background), "the background after the opening at (\(fx), \(fy)): \(after.rgb(fx: fx, fy: fy))")
        }
        XCTAssertTrue(isColor(after.rgb(fx: personInCorner.0, fy: personInCorner.1), SpeakerColor.person), "the speaker sits in the corner after the opening")
    }

    /// S81: the decision is per frame by time. At 30 fps the frame before `end` (29/30 s) is still the raw take and the
    /// frame AT `end` (1.0 s) is already the corner, never a frame of background or corner early nor of raw late.
    @MainActor func testFullScreenWindowEdgesAreFrameExact() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let scene = try await openingScene(opening: 1)
        let last = try await previewFrame(scene.recipe, urls: scene.urls, at: 29.0 / 30.0)
        XCTAssertTrue(isColor(last.rgb(fx: 0.04, fy: 0.04), SpeakerColor.room), "the last frame before end is raw: \(last.rgb(fx: 0.04, fy: 0.04))")
        XCTAssertTrue(isColor(last.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.person))
        let first = try await previewFrame(scene.recipe, urls: scene.urls, at: 1.0)
        XCTAssertTrue(isColor(first.rgb(fx: 0.04, fy: 0.04), SpeakerColor.background), "the frame at end is the corner layout: \(first.rgb(fx: 0.04, fy: 0.04))")
        XCTAssertTrue(isColor(first.rgb(fx: personInCorner.0, fy: personInCorner.1), SpeakerColor.person))
        XCTAssertTrue(isColor(first.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.background))
        let start = try await previewFrame(scene.recipe, urls: scene.urls, at: 0)
        XCTAssertTrue(isColor(start.rgb(fx: 0.04, fy: 0.04), SpeakerColor.room), "the very first frame is raw")
    }

    /// S71: the mask fails on the first corner frame (the switch). The fallback is today's `.original` answer: the plain
    /// take small in the corner over the background. It is never hidden, and the room is never drawn full-frame.
    @MainActor func testAFailedMaskAtTheSwitchShowsThePlainSpeakerInTheCorner() async throws {
        let segmenter = ScriptedSegmenter { _ in throw SwitchFailure() }
        useSpeakerSegmenter(self, segmenter)
        let scene = try await openingScene(opening: 1)
        let frame = try await previewFrame(scene.recipe, urls: scene.urls, at: 1.0)
        XCTAssertEqual(segmenter.calls, 1, "the switch frame is the first frame the matte sees")
        XCTAssertTrue(isColor(frame.rgb(fx: 0.04, fy: 0.04), SpeakerColor.background), "background behind the corner: \(frame.rgb(fx: 0.04, fy: 0.04))")
        XCTAssertTrue(isColor(frame.rgb(fx: personCenter.0, fy: personCenter.1), SpeakerColor.background), "not full-frame")
        XCTAssertTrue(isColor(frame.rgb(fx: personInCorner.0, fy: personInCorner.1), SpeakerColor.person), "the speaker is visible in the corner")
        // Unmasked, so the room around the person inside the box is drawn too.
        XCTAssertTrue(isColor(frame.rgb(fx: 0.76 - 0.42 * 0.4, fy: personInCorner.1), SpeakerColor.room), "the plain take, not a cut-out: \(frame.rgb(fx: 0.76 - 0.42 * 0.4, fy: personInCorner.1))")
    }

    // MARK: - Qendresa item 18: an aimed background. F15 in `docs/reviews/background-focus-failure-modes.md`.

    /// F15: a looping video window aimed at its left edge draws the same picture in the preview composition and in the
    /// exported file (one placement function feeds both), and the unaimed neighbour stays the centre crop.
    @MainActor func testAimedVideoWindowMatchesInPreviewAndExport() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let blue = CGColor(red: 0, green: 0, blue: 1, alpha: 1), red = CGColor(red: 1, green: 0, blue: 0, alpha: 1), white = CGColor(red: 1, green: 1, blue: 1, alpha: 1)
        let recording = try await writeSpeakerTestVideo(directory: directory, name: "thirds", size: (320, 180), seconds: 1) { context, _ in
            for (index, color) in [blue, red, white].enumerated() {
                context.setFillColor(color)
                context.fill(CGRect(x: Double(index) * 320 / 3, y: 0, width: 320 / 3 + 1, height: 180))
            }
        }
        let room = try await writeEmptyRoomClip(directory: directory, size: (canvas.width, canvas.height), seconds: 3)
        let scene = try speakerRecipe(canvas: canvas, speaker: room, duration: 3, background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                      windows: [SpeakerTestWindow(url: recording, kind: .video, start: 0, end: 1.5,
                                                                  focus: SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 1)),
                                                SpeakerTestWindow(url: recording, kind: .video, start: 1.5, end: 3)])
        XCTAssertTrue(scene.recipe.effectiveCapabilities.contains(.backgroundFocus))
        let preview = try await previewFrame(scene.recipe, urls: scene.urls, at: 0.75)
        let exportedFile = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory)
        let exported = try await fileFrame(exportedFile, at: 0.75)
        for (name, frame) in [("preview", preview), ("export", exported)] {
            for (fx, fy) in [(0.1, 0.1), (0.5, 0.5), (0.9, 0.9)] {
                XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), blue), "\(name) aimed left at (\(fx), \(fy)): \(frame.rgb(fx: fx, fy: fy))")
            }
        }
        let unaimedPreview = try await previewFrame(scene.recipe, urls: scene.urls, at: 2.25)
        let unaimedExport = try await fileFrame(exportedFile, at: 2.25)
        for (name, frame) in [("preview", unaimedPreview), ("export", unaimedExport)] {
            XCTAssertTrue(isColor(frame.rgb(fx: 0.5, fy: 0.5), red), "\(name) unaimed window keeps the centre crop: \(frame.rgb(fx: 0.5, fy: 0.5))")
            XCTAssertTrue(isColor(frame.rgb(fx: 0.05, fy: 0.5), red), "\(name) unaimed left edge is still red: \(frame.rgb(fx: 0.05, fy: 0.5))")
        }
    }
}
#endif
