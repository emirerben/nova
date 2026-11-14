import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

// KRI-1418: dragging a split divider changes only a section's `panelWeights` or `gutter`. That edit has to move
// the panels of the composition already on screen, with the rects an export of the same recipe draws. Failure
// modes: the in-place path refuses and a full rebuild runs per touch sample; a panel keeps its old rect or its old
// aspect fit; a collapse layout keeps the rects of the old weights; preview and export disagree on the new panel
// rects; an edit that changes more than the divider is applied in place and leaves stale layers.
extension SplitScreenMediaParityTests {
    private var red: [Int] { [255, 0, 0] }
    private var green: [Int] { [0, 255, 0] }
    private var white: [Int] { [255, 255, 255] }

    /// Red on top over [0, 4), green below over [0, 4). A 96x160 canvas, white where no panel draws.
    @MainActor private func twoPanelScene(_ directory: URL, collapse: Bool = false, bottomSeconds: Double = 4)
        async throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let top = try await SolidVideoFixture.make(directory: directory, name: "top", color: color(1, 0, 0), seconds: 4)
        let bottom = try await SolidVideoFixture.make(directory: directory, name: "bottom", color: color(0, 1, 0), seconds: 4)
        let urls = ["top": top, "bottom": bottom]
        var recipe = try makeRecipe(urls, [
            [TimelineClip(id: "top", sourceAssetID: "top", sourceDuration: 4, splitScreen: .init(sectionID: "s", panelIndex: 0))],
            [TimelineClip(id: "bottom", sourceAssetID: "bottom", sourceDuration: bottomSeconds, splitScreen: .init(sectionID: "s", panelIndex: 1))]
        ], end: 4)
        recipe.splitScreenSections = [.init(id: "s", start: 0, end: 4, panelWeights: [1, 1], backgroundColor: "#FFFFFF", collapseEmptyPanels: collapse)]
        try recipe.validate()
        return (recipe, urls)
    }

    /// What each instruction draws, so two compositions of one recipe compare layer by layer.
    private func drawn(_ composition: AVVideoComposition?) -> [String] {
        ((composition?.instructions as? [RecipeVideoInstruction]) ?? []).flatMap(\.layers).compactMap { layer -> String? in
            guard let id = layer.clipID else { return nil }
            return "\(id) \(layer.start)-\(layer.end) \(String(describing: layer.panelViewport)) \(layer.transform)"
        }.sorted()
    }

    @MainActor func testWeightsAndGutterChangeIsAppliedInPlaceWithTheExportsPanelRects() async throws {
        let directory = try artifacts()
        let (recipe, urls) = try await twoPanelScene(directory)
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let frames = generator(live.preview)
        try await assertPixel(frames, time: 1, y: 60, expected: green)

        var next = recipe
        next.splitScreenSections[0].panelWeights = [3, 1]
        next.splitScreenSections[0].gutter = 0.1
        try live.updateText(recipe: next)

        let fresh = try await AVPlayerPreviewComposer().makePreview(recipe: next, assetURLs: urls)
        XCTAssertEqual(drawn(live.preview.playerItem.videoComposition), drawn(fresh.playerItem.videoComposition),
                       "An in-place update draws the panels a fresh composition of the same recipe does")
        XCTAssertEqual(live.exportSnapshot().recipe, next)
        let output = try await export(next, urls, directory, "relayout")
        for source in [generator(live.preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            try await assertPixel(source, time: 1, y: 100, expected: red)
            try await assertPixel(source, time: 1, y: 60, expected: red)
            try await assertPixel(source, time: 1, y: 44, expected: white)
            try await assertPixel(source, time: 1, y: 20, expected: green)
        }
        let top = try XCTUnwrap(live.mediaSelectionBounds(id: "top", time: 1))
        XCTAssertEqual(top.height, 0.675, accuracy: 0.002, "The selection box follows the new panel")
    }

    @MainActor func testCollapseLayoutsFollowTheNewWeightsInPlace() async throws {
        let directory = try artifacts()
        let (recipe, urls) = try await twoPanelScene(directory, collapse: true, bottomSeconds: 1)
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        var next = recipe
        next.splitScreenSections[0].panelWeights = [3, 1]
        try live.updateText(recipe: next)

        let fresh = try await AVPlayerPreviewComposer().makePreview(recipe: next, assetURLs: urls)
        XCTAssertEqual(drawn(live.preview.playerItem.videoComposition), drawn(fresh.playerItem.videoComposition))
        let frames = generator(live.preview)
        try await assertPixel(frames, time: 0.5, y: 60, expected: red)
        try await assertPixel(frames, time: 0.5, y: 20, expected: green)
        try await assertPixel(frames, time: 2, y: 10, expected: red)
    }

    @MainActor func testAnythingBeyondTheDividerStillTakesTheFullRebuild() async throws {
        let directory = try artifacts()
        let (recipe, urls) = try await twoPanelScene(directory)
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        func refusal(_ change: (inout EditRecipe) -> Void, file: StaticString = #filePath, line: UInt = #line) -> String? {
            var next = recipe
            next.splitScreenSections[0].panelWeights = [3, 1]
            change(&next)
            do { try live.updateText(recipe: next); return nil }
            catch { return (error as? NativePreviewFeatureError)?.feature }
        }
        XCTAssertEqual(refusal { $0.splitScreenSections[0].backgroundColor = "#000000" }, "LivePreviewComposition-97", "Background is drawn at build time")
        XCTAssertEqual(refusal { $0.splitScreenSections[0].collapseEmptyPanels = true }, "LivePreviewComposition-97")
        XCTAssertEqual(refusal { $0.splitScreenSections[0].end = 5 }, "LivePreviewComposition-97")
        XCTAssertEqual(refusal { $0.splitScreenSections[0].panelWeights = [1, 1, 1] }, "LivePreviewComposition-97", "A third panel needs a new clip layout")
        XCTAssertEqual(refusal {
            $0.tracks[0].clips[0].splitScreen = .init(sectionID: "s", panelIndex: 1)
            $0.tracks[1].clips[0].splitScreen = .init(sectionID: "s", panelIndex: 0)
        }, "LivePreviewComposition-79", "A clip moving panels is a clip change")
        XCTAssertEqual(refusal { $0.tracks[1].clips[0].sourceDuration = 3 }, "LivePreviewComposition-79")
        XCTAssertEqual(live.exportSnapshot().recipe, recipe, "A refused update leaves the composition and its recipe as they were")
        let frames = generator(live.preview)
        try await assertPixel(frames, time: 1, y: 60, expected: green)
    }

    @MainActor func testASupportingCardIsRedrawnForItsPanelSoItsPanelResizeRebuilds() async throws {
        let directory = try artifacts()
        let photo = directory.appendingPathComponent("card.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 64, height: 128)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let video = try await SolidVideoFixture.make(directory: directory, name: "green", color: color(0, 1, 0), seconds: 2)
        let urls = ["card": photo, "green": video]
        var recipe = try makeRecipe(urls, [
            [TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, stillLayout: .supportingCard, splitScreen: .init(sectionID: "s", panelIndex: 0))],
            [TimelineClip(id: "video", sourceAssetID: "green", sourceDuration: 2, splitScreen: .init(sectionID: "s", panelIndex: 1))]
        ], end: 2)
        recipe.splitScreenSections = [.init(id: "s", start: 0, end: 2, panelWeights: [1, 1], backgroundColor: "#FFFFFF")]
        try recipe.validate()
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        var next = recipe
        next.splitScreenSections[0].panelWeights = [3, 1]
        XCTAssertThrowsError(try live.updateText(recipe: next)) {
            XCTAssertEqual(($0 as? NativePreviewFeatureError)?.feature, "LivePreviewComposition-split-relayout-card")
        }
    }

    @MainActor func testAMattedSpeakerPanelResizeRebuilds() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (120, 60), seconds: 3)
        let room = try await writeEmptyRoomClip(directory: directory, size: (120, 60), seconds: 3)
        var scene = try speakerRecipe(canvas: Canvas(width: 120, height: 180), speaker: speaker, duration: 3,
            background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)), extraAssets: ["other": room])
        scene.recipe.tracks = [
            .init(id: "top", kind: .video, clips: [.init(id: "speaker", sourceAssetID: "speaker", sourceDuration: 3,
                splitScreen: .init(sectionID: "s", panelIndex: 0))]),
            .init(id: "bottom", kind: .video, clips: [.init(id: "other", sourceAssetID: "other", sourceDuration: 3,
                splitScreen: .init(sectionID: "s", panelIndex: 1))])
        ]
        scene.recipe.splitScreenSections = [.init(id: "s", start: 0, end: 3, panelWeights: [1, 1], backgroundColor: "#FFFFFF")]
        scene.recipe.speakerBackground = SpeakerBackgroundLayer(assetID: "visual-background", mediaKind: VisualMediaKind.image,
            speakerFrame: nil, subjectClipIDs: ["speaker"])
        try scene.recipe.validate()
        let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        var next = scene.recipe
        next.splitScreenSections[0].panelWeights = [3, 1]
        XCTAssertThrowsError(try live.updateText(recipe: next)) {
            XCTAssertEqual(($0 as? NativePreviewFeatureError)?.feature, "LivePreviewComposition-split-relayout-speaker")
        }
    }
}
#endif
