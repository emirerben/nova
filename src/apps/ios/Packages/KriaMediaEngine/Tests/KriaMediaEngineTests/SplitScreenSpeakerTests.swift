#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

final class SplitScreenSpeakerTests: XCTestCase {
    @MainActor func testRepeatedSubjectBackgroundAndFrameStayInOwnedPanels() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory: URL
        if let retained = ProcessInfo.processInfo.environment["KRIA_SPLIT_SCREEN_ARTIFACTS"] {
            directory = URL(fileURLWithPath: retained).appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            print("Split-screen speaker artifacts: \(directory.path)")
        } else { directory = try speakerTestDirectory(self) }
        let speaker = try await writeSpeakerClip(directory: directory, size: (120, 60), seconds: 3)
        let unrelated = try await writeEmptyRoomClip(directory: directory, size: (120, 60), seconds: 3)
        let background = directory.appendingPathComponent("striped.png")
        let bounds = CGRect(x: 0, y: 0, width: 120, height: 60)
        let stripes = CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 30, height: 60))
            .composited(over: CIImage(color: .blue).cropped(to: CGRect(x: 90, y: 0, width: 30, height: 60)))
            .composited(over: CIImage(color: CIColor(red: 1, green: 1, blue: 0)).cropped(to: bounds))
        try CIContext().writePNGRepresentation(of: stripes, to: background, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let windowURL = directory.appendingPathComponent("window.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: CIColor(red: 0, green: 1, blue: 1)).cropped(to: bounds),
            to: windowURL, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        var scene = try speakerRecipe(canvas: Canvas(width: 120, height: 180), speaker: speaker, duration: 3,
            background: SpeakerTestBackground(url: background), extraAssets: ["other": unrelated],
            windows: [.init(url: windowURL, start: 1, end: 2)])
        scene.recipe.tracks = [
            .init(id: "top", kind: .video, clips: [.init(id: "top-speaker", sourceAssetID: "speaker", sourceDuration: 3,
                splitScreen: .init(sectionID: "s", panelIndex: 0))]),
            .init(id: "middle", kind: .video, clips: [.init(id: "other", sourceAssetID: "other", sourceDuration: 1,
                splitScreen: .init(sectionID: "s", panelIndex: 1))]),
            .init(id: "bottom", kind: .video, clips: [.init(id: "repeat-speaker", sourceAssetID: "speaker", sourceStart: 1, sourceDuration: 2,
                splitScreen: .init(sectionID: "s", panelIndex: 2))])
        ]
        scene.recipe.splitScreenSections = [.init(id: "s", start: 0, end: 3, panelWeights: [1, 1, 1], backgroundColor: "#FFFFFF")]
        let windows = scene.recipe.speakerBackground?.windows?.map {
            SpeakerBackgroundWindow(assetID: $0.assetID, mediaKind: $0.mediaKind, sourceStart: $0.sourceStart,
                start: $0.start, end: $0.end, focus: .init(x: 0.5, y: 0.5, zoom: 0.5))
        }
        for framed in [false, true] {
            scene.recipe.speakerBackground = SpeakerBackgroundLayer(assetID: "visual-background", mediaKind: VisualMediaKind.image, windows: windows,
                speakerFrame: framed ? .init(widthFraction: 0.5, xFraction: 0.5, yFraction: 0.5) : nil,
                fullScreenWindows: [.init(start: 2, end: 3)], focus: .init(x: 0.5, y: 0.5, zoom: 0.5), subjectClipIDs: ["top-speaker", "repeat-speaker"])
            try scene.recipe.validate()
            let livePreview = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
            let preview = livePreview.preview
            if framed {
                let top = try XCTUnwrap(livePreview.speakerSelectionBounds(time: 0.5, clipID: "top-speaker"))
                let bottom = try XCTUnwrap(livePreview.speakerSelectionBounds(time: 0.5, clipID: "repeat-speaker"))
                XCTAssertEqual(top.centerY, 1.0 / 6, accuracy: 0.001)
                XCTAssertEqual(bottom.centerY, 5.0 / 6, accuracy: 0.001)
                XCTAssertEqual(top.height, 1.0 / 6, accuracy: 0.001)
                let viewport = try XCTUnwrap(livePreview.speakerViewport(time: 0.5, clipID: "repeat-speaker"))
                XCTAssertEqual(viewport.minY, 2.0 / 3, accuracy: 0.001)
                XCTAssertEqual(viewport.height, 1.0 / 3, accuracy: 0.001)
                XCTAssertNil(livePreview.speakerSelectionBounds(time: 0.5, clipID: "other"))
                let sliced = try livePreview.speakerInteractionLayers(time: 0.5, clipID: "top-speaker")
                let below = AVAssetImageGenerator(asset: preview.playerItem.asset)
                below.videoComposition = sliced.below
                let belowFrame = try await frame(below, at: 0.5)
                XCTAssertTrue(isColor(belowFrame.rgb(fx: 0.5, fy: 0.86), SpeakerColor.person), "Other subject remains below selected speaker")
            }
            let output = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: framed ? "framed" : "plain")
            let live = AVAssetImageGenerator(asset: preview.playerItem.asset)
            live.videoComposition = preview.playerItem.videoComposition
            for generator in [live, AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
                for y in [0.1, 0.85] {
                    let frame = try await frame(generator, at: 0.5)
                    XCTAssertTrue(isColor(frame.rgb(fx: 0.08, fy: y), CGColor(red: 1, green: 0, blue: 0, alpha: 1), tolerance: 70))
                    XCTAssertTrue(isColor(frame.rgb(fx: 0.92, fy: y), CGColor(red: 0, green: 0, blue: 1, alpha: 1), tolerance: 70))
                }
                let first = try await frame(generator, at: 0.5)
                for y in [0.2, 0.86] {
                    XCTAssertTrue(isColor(first.rgb(fx: 0.5, fy: y), SpeakerColor.person), "Repeated owned subjects must remain visible")
                }
                XCTAssertTrue(isColor(first.rgb(fx: 0.1, fy: 0.5), SpeakerColor.room, tolerance: 90), "Unowned source must keep its room")
                let gap = try await frame(generator, at: 1.5)
                XCTAssertTrue(isColor(gap.rgb(fx: 0.1, fy: 0.1), CGColor(red: 0, green: 1, blue: 1, alpha: 1)), "Timed background uses panel-local space")
                XCTAssertTrue(isColor(gap.rgb(fx: 0.1, fy: 0.5), SpeakerTestColor.white), "Background must not leak into another panel gap")
                let full = try await frame(generator, at: 2.5)
                XCTAssertTrue(isColor(full.rgb(fx: 0.1, fy: 0.1), SpeakerColor.room, tolerance: 90), "Full-screen window unmasks only the owning panel")
                XCTAssertTrue(isColor(full.rgb(fx: 0.1, fy: 0.85), SpeakerTestColor.white), "Expired repeated subject must not paint background")
                XCTAssertTrue(isColor(full.rgb(fx: 0.1, fy: 0.5), SpeakerTestColor.white))
            }
        }
    }

    func testScopedSpeakerOwnershipRoundtripsAndRejectsUnknownIDs() throws {
        let layer = SpeakerBackgroundLayer.color("#0000FF", subjectClipIDs: ["one", "repeat"])
        let data = try RecipeJSON.encoder().encode(layer)
        XCTAssertEqual(try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: data), layer)
        XCTAssertEqual(layer.replacingSpeakerFrame(.init(widthFraction: 0.5, xFraction: 0.5, yFraction: 0.5)).subjectClipIDs, ["one", "repeat"])
        XCTAssertThrowsError(try SpeakerBackgroundLayer.color("#0000FF", subjectClipIDs: ["one", "one"]).validate())
        var recipe = EditRecipe(schemaVersion: 2, tracks: [.init(id: "t", kind: .video, clips: [
            .init(id: "one", sourceAssetID: "a", sourceDuration: 1)
        ])], speakerBackground: layer)
        XCTAssertThrowsError(try recipe.validateSpeakerOwnership())
        recipe.speakerBackground = .color("#0000FF", subjectClipIDs: [])
        XCTAssertNoThrow(try recipe.validateSpeakerOwnership())
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.splitScreen))
    }

    @MainActor private func frame(_ generator: AVAssetImageGenerator, at time: Double) async throws -> SpeakerTestFrame {
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        return try SpeakerTestFrame(try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image)
    }
}
#endif
