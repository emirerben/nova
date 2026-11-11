import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
#endif

// Collapse mode failure modes (contract "Collapse mode"): a panel that outlives another keeps its old rect; a source
// clock restarts or jumps at a layout change; a transition fade replays per layout; audio drops or doubles at a
// layout change; a boundary frame shows the old layout, or preview and export change on different frames; a
// same-panel transition counts as two panels; a frame with no member shows a stale panel; a supporting card or a
// matted speaker keeps the old panel size; an in-place edit restores one rect per clip; a fixed section changes;
// the flag is lost, written when false, or rendered by a build without `splitCollapse`.
final class SplitScreenCollapseTests: XCTestCase {
    func testActivePanelsShareTheHeightAndOneFillsTheCanvas() {
        let section = SplitScreenSection(id: "s", start: 0, end: 4, panelWeights: [1, 2, 1], gutter: 0.1, collapseEmptyPanels: true)
        let canvas = CGRect(x: 0, y: 0, width: 100, height: 200)
        assertRect(section.panelRect(index: 0, canvas: canvas, activePanels: [0, 2]), CGRect(x: 0, y: 110, width: 100, height: 90))
        assertRect(section.panelRect(index: 2, canvas: canvas, activePanels: [0, 2]), CGRect(x: 0, y: 0, width: 100, height: 90))
        assertRect(section.panelRect(index: 1, canvas: canvas, activePanels: [1, 2]), CGRect(x: 0, y: 80, width: 100, height: 120))
        assertRect(section.panelRect(index: 2, canvas: canvas, activePanels: [1, 2]), CGRect(x: 0, y: 0, width: 100, height: 60))
        for index in 0..<3 {
            assertRect(section.panelRect(index: index, canvas: canvas, activePanels: [index]), canvas)
            XCTAssertEqual(section.panelRect(index: index, canvas: canvas, activePanels: [0, 1, 2]), section.panelRect(index: index, canvas: canvas))
        }
        XCTAssertEqual(section.panelRect(index: 1, canvas: canvas), CGRect(x: 0, y: 60, width: 100, height: 80))
    }

    func testFlagIsWrittenOnlyWhenSetAndRequiresSplitCollapse() throws {
        let fixed = SplitScreenSection(id: "s", start: 0, end: 2, panelWeights: [1, 1])
        let fixedWire = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(fixed)) as? [String: Any])
        XCTAssertEqual(Set(fixedWire.keys), ["id", "start", "end", "panel_weights", "gutter", "background_color"])
        var collapsing = fixed
        collapsing.collapseEmptyPanels = true
        let wire = try RecipeJSON.encoder().encode(collapsing)
        XCTAssertEqual((try JSONSerialization.jsonObject(with: wire) as? [String: Any])?["collapse_empty_panels"] as? Bool, true)
        XCTAssertEqual(try RecipeJSON.decoder().decode(SplitScreenSection.self, from: wire), collapsing)
        let base = ##"{"id":"s","start":0,"end":2,"panel_weights":[1,1],"gutter":0,"background_color":"#000000""##
        XCTAssertEqual(try RecipeJSON.decoder().decode(SplitScreenSection.self, from: Data((base + "}").utf8)), fixed)
        XCTAssertEqual(try RecipeJSON.decoder().decode(SplitScreenSection.self, from: Data((base + #","collapse_empty_panels":false}"#).utf8)), fixed)
        for invalid in [#","collapse_empty_panels":"true"}"#, #","collapse_empty_panel":true}"#] {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(SplitScreenSection.self, from: Data((base + invalid).utf8)), invalid)
        }

        var recipe = EditRecipe(schemaVersion: 2, tracks: [.init(id: "v", kind: .video, clips: [
            .init(id: "a", sourceAssetID: "a", sourceDuration: 1, splitScreen: .init(sectionID: "s", panelIndex: 0))
        ])], splitScreenSections: [fixed])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.splitScreen))
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.splitCollapse))
        recipe.splitScreenSections = [collapsing]
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.splitScreen, .splitCollapse]))
        let withoutCollapse = DefaultRendererCapabilities(capabilities: recipe.effectiveCapabilities.subtracting([.splitCollapse]))
        XCTAssertEqual(CapabilityNegotiator(provider: withoutCollapse).decide(for: recipe).missingCapabilities, [.splitCollapse])
    }

    func testBackendShapedCollapseRecipeDecodesAndRoundTrips() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "split-screen-collapse-recipe", withExtension: "json", subdirectory: "Fixtures"))
        let recipe = try RecipeJSON.decode(Data(contentsOf: url))
        try recipe.validate()
        XCTAssertEqual(recipe.splitScreenSections.map(\.collapseEmptyPanels), [true])
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.splitScreen, .splitCollapse]))
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 3)
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(recipe)), recipe)
    }

    func testEveryFrameDrawsOneLayoutWhenAComputedEndMeetsALiteralStart() {
        let clips = [
            TimelineClip(id: "a", sourceAssetID: "a", sourceDuration: 0.2, timelineStart: 0.1, splitScreen: .init(sectionID: "s", panelIndex: 0)),
            TimelineClip(id: "b", sourceAssetID: "a", sourceDuration: 0.4, timelineStart: 0.3, splitScreen: .init(sectionID: "s", panelIndex: 1)),
            TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 0.7, splitScreen: .init(sectionID: "s", panelIndex: 2))
        ]
        XCTAssertGreaterThan(clips[0].timelineStart + clips[0].duration, 0.3, "0.1 + 0.2 ends an ulp after the frame b starts on")
        let section = SplitScreenSection(id: "s", start: 0, end: 0.7, panelWeights: [1, 1, 1], collapseEmptyPanels: true)
        let recipe = EditRecipe(schemaVersion: 2, tracks: clips.map { TimelineTrack(id: $0.id, kind: .video, clips: [$0]) }, splitScreenSections: [section])
        let canvas = CGRect(x: 0, y: 0, width: 1, height: 1)
        let placements = clips.map { recipe.panelPlacements(of: $0, start: $0.timelineStart, end: $0.timelineStart + $0.duration, canvas: canvas) }
        for frame in 0..<21 {
            let time = Double(2_000 * frame) / 60_000
            let drawn = zip(clips, placements).compactMap { clip, list in
                list.first { time >= $0.start && time < $0.end }.map { (panel: clip.splitScreen!.panelIndex, viewport: $0.viewport) }
            }
            for panel in drawn {
                XCTAssertEqual(panel.viewport, section.panelRect(index: panel.panel, canvas: canvas, activePanels: drawn.map(\.panel)), "frame \(frame)")
            }
            if frame == 9 { XCTAssertEqual(drawn.map(\.panel), [1, 2], "At 0.3 s a has ended and b has begun") }
        }
    }

    func testSharedPythonLayoutSamplesMatchTheEnginePlacements() throws {
        struct Fixture: Decodable { let canvasYOrigin: String; let cases: [Case] }
        struct Case: Decodable {
            let name: String; let slots: [Slot]; let splitScreenSections: [Layout]
            let expectedWindows: [Window]; let expectedRecipeSections: [Span]; let canvasSamples: [Sample]
        }
        struct Slot: Decodable { let slotId: String; let splitSectionId: String?; let splitPanelIndex: Int? }
        struct Layout: Decodable { let id: String; let panelWeights: [Double]; let gutter: Double }
        struct Window: Decodable { let slotId: String; let startS: Double; let endS: Double }
        struct Span: Decodable { let id: String; let start: Double; let end: Double }
        struct Sample: Decodable { let sectionTimeS: Double; let activePanels: [Int]; let rects: [Rect] }
        struct Rect: Decodable { let panelIndex: Int; let y: Double; let height: Double }
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let url = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/tests/fixtures/split_screen_collapse_timeline.json")
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let fixture = try decoder.decode(Fixture.self, from: Data(contentsOf: url))
        XCTAssertEqual(fixture.canvasYOrigin, "top")
        XCTAssertFalse(fixture.cases.isEmpty)
        let unit = CGRect(x: 0, y: 0, width: 1, height: 1)
        for item in fixture.cases {
            let layouts = Dictionary(uniqueKeysWithValues: item.splitScreenSections.map { ($0.id, $0) })
            let sections = try item.expectedRecipeSections.map { span in
                let layout = try XCTUnwrap(layouts[span.id])
                return SplitScreenSection(id: span.id, start: span.start, end: span.end, panelWeights: layout.panelWeights,
                                          gutter: layout.gutter, collapseEmptyPanels: true)
            }
            XCTAssertEqual(sections.count, 1, "Samples name no section")
            let windows = Dictionary(uniqueKeysWithValues: item.expectedWindows.map { ($0.slotId, $0) })
            let members = item.slots.compactMap { slot -> TimelineClip? in
                guard let section = slot.splitSectionId, let panel = slot.splitPanelIndex, let window = windows[slot.slotId] else { return nil }
                return TimelineClip(id: slot.slotId, sourceAssetID: "a", sourceDuration: window.endS - window.startS, timelineStart: window.startS,
                                    splitScreen: .init(sectionID: section, panelIndex: panel))
            }
            let recipe = EditRecipe(schemaVersion: 2, tracks: members.map { TimelineTrack(id: $0.id, kind: .video, clips: [$0]) }, splitScreenSections: sections)
            try recipe.validateSplitScreen()
            for sample in item.canvasSamples {
                let time = sections[0].start + sample.sectionTimeS
                let playing = members.filter { $0.timelineStart <= time && time < $0.timelineStart + $0.duration }
                XCTAssertEqual(Set(playing.compactMap(\.splitScreen?.panelIndex)), Set(sample.activePanels), "\(item.name) at \(sample.sectionTimeS)")
                for rect in sample.rects {
                    let clip = try XCTUnwrap(playing.first { $0.splitScreen?.panelIndex == rect.panelIndex }, "\(item.name) panel \(rect.panelIndex)")
                    let placement = try XCTUnwrap(recipe.panelPlacements(of: clip, start: clip.timelineStart, end: clip.timelineStart + clip.duration, canvas: unit)
                        .first { $0.start <= time && time < $0.end })
                    let viewport = try XCTUnwrap(placement.viewport)
                    XCTAssertEqual(1 - viewport.maxY, rect.y, accuracy: 0.000_01, "\(item.name) at \(sample.sectionTimeS), panel \(rect.panelIndex) top")
                    XCTAssertEqual(viewport.height, rect.height, accuracy: 0.000_01, "\(item.name) at \(sample.sectionTimeS), panel \(rect.panelIndex) height")
                }
            }
        }
    }

    private func assertRect(_ actual: CGRect, _ expected: CGRect, file: StaticString = #filePath, line: UInt = #line) {
        for (a, e) in zip([actual.minX, actual.minY, actual.width, actual.height], [expected.minX, expected.minY, expected.width, expected.height]) {
            XCTAssertEqual(a, e, accuracy: 0.000_001, "\(actual) != \(expected)", file: file, line: line)
        }
    }
}

#if canImport(AVFoundation)
extension SplitScreenMediaParityTests {
    private var red: [Int] { [255, 0, 0] }
    private var green: [Int] { [0, 255, 0] }
    private var yellow: [Int] { [255, 255, 0] }
    private var gray: [Int] { [128, 128, 128] }
    private var white: [Int] { [255, 255, 255] }
    private var black: [Int] { [0, 0, 0] }

    /// Three panels on a 96x160 canvas over [0, 4). Top plays source "a" from 0.5 s for 3 s (red before source 2 s,
    /// green after, so timeline 1.5 s); middle plays yellow "b" over [0, 1); bottom plays gray "c" over [0.5, 2); nothing
    /// after 3 s. Saturated blue and magenta lose more than the tolerance in the H.264 round trip.
    @MainActor private func collapseScene(_ directory: URL) async throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let a = try await SolidVideoFixture.make(directory: directory, name: "a", color: color(1, 0, 0), seconds: 4,
            frameColor: { $0 < 60 ? self.color(1, 0, 0) : self.color(0, 1, 0) }, audioFrequency: 431)
        let b = try await SolidVideoFixture.make(directory: directory, name: "b", color: color(1, 1, 0), seconds: 2, audioFrequency: 719)
        let c = try await SolidVideoFixture.make(directory: directory, name: "c", color: color(0.5, 0.5, 0.5), seconds: 2, audioFrequency: 997)
        let urls = ["a": a, "b": b, "c": c]
        var recipe = try makeRecipe(urls, [
            [TimelineClip(id: "top", sourceAssetID: "a", sourceStart: 0.5, sourceDuration: 3, splitScreen: .init(sectionID: "s", panelIndex: 0))],
            [TimelineClip(id: "middle", sourceAssetID: "b", sourceDuration: 1, splitScreen: .init(sectionID: "s", panelIndex: 1))],
            [TimelineClip(id: "bottom", sourceAssetID: "c", sourceDuration: 1.5, timelineStart: 0.5, splitScreen: .init(sectionID: "s", panelIndex: 2))]
        ], end: 4)
        recipe.splitScreenSections = [.init(id: "s", start: 0, end: 4, panelWeights: [1, 1, 1], backgroundColor: "#FFFFFF", collapseEmptyPanels: true)]
        try recipe.validate()
        return (recipe, urls)
    }

    @MainActor func testCollapsingPanelsReflowOnTheSameFrameWithContinuousClocksAndAudio() async throws {
        let directory = try artifacts()
        let (recipe, urls) = try await collapseScene(directory)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = try await export(recipe, urls, directory, "collapse")
        let expected: [(time: Double, y: Int, rgb: [Int])] = [
            (0.25, 150, red), (0.25, 90, red), (0.25, 70, yellow), (0.25, 10, yellow),
            (0.75, 150, red), (0.75, 80, yellow), (0.75, 10, gray),
            (1.25, 90, red), (1.25, 70, gray),
            (1.75, 150, green), (1.75, 10, gray),
            (2.5, 150, green), (2.5, 80, green), (2.5, 10, green),
            (3.5, 150, white), (3.5, 80, white), (3.5, 10, white),
            (0.5, 10, gray), (29.0 / 30, 80, yellow), (1.0, 90, red), (1.0, 70, gray),
            (59.0 / 30, 10, gray), (2.0, 10, green), (89.0 / 30, 80, green), (3.0, 80, white)
        ]
        for frames in [generator(preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            for point in expected { try await assertPixel(frames, time: point.time, y: point.y, expected: point.rgb) }
        }

        var reference: [Double: Double] = [:]
        for (id, frequency) in [("a", 431.0), ("b", 719), ("c", 997)] {
            reference[frequency] = try amplitude(try await decode(AVURLAsset(url: urls[id]!), nil), 0.6, frequency)
        }
        let heard: [(frequency: Double, playing: [Double], silent: [Double])] = [
            (431, [0.1, 0.93, 1.3, 1.93, 2.6], [3.3]),
            (719, [0.1, 0.6], [1.3, 2.6]),
            (997, [0.7, 1.3], [0.1, 2.3])
        ]
        for (asset, mix) in [(preview.playerItem.asset, preview.playerItem.audioMix), (AVURLAsset(url: output) as AVAsset, nil)] {
            let pcm = try await decode(asset, mix)
            for tone in heard {
                for time in tone.playing {
                    XCTAssertEqual(try amplitude(pcm, time, tone.frequency), reference[tone.frequency]! * 0.4, accuracy: 0.012, "\(tone.frequency) Hz at \(time)")
                }
                for time in tone.silent { XCTAssertLessThan(try amplitude(pcm, time, tone.frequency), 0.006, "\(tone.frequency) Hz at \(time)") }
            }
        }

        var fixed = recipe
        fixed.splitScreenSections[0].collapseEmptyPanels = false
        let fixedPreview = try await AVPlayerPreviewComposer().makePreview(recipe: fixed, assetURLs: urls)
        let fixedFrames = generator(fixedPreview)
        try await assertPixel(fixedFrames, time: 0.25, y: 90, expected: yellow)
        try await assertPixel(fixedFrames, time: 0.25, y: 10, expected: white)
        try await assertPixel(fixedFrames, time: 2.5, y: 150, expected: green)
        try await assertPixel(fixedFrames, time: 2.5, y: 10, expected: white)
        let instructions = try XCTUnwrap(fixedPreview.playerItem.videoComposition?.instructions as? [RecipeVideoInstruction])
        let canvas = CGRect(x: 0, y: 0, width: 96, height: 160)
        for (index, clip) in fixed.tracks.flatMap(\.clips).enumerated() {
            let drawn = instructions.flatMap(\.layers).filter { $0.clipID == clip.id }
            XCTAssertEqual(Set(drawn.map(\.start)), [clip.timelineStart], "A fixed panel is one layer over its whole window")
            XCTAssertTrue(drawn.allSatisfy { $0.panelViewport == fixed.splitScreenSections[0].panelRect(index: index, canvas: canvas) }, clip.id)
        }
    }

    @MainActor func testSamePanelTransitionCountsOnceAndItsFadeRunsThroughTheReflow() async throws {
        let directory = try artifacts()
        var urls: [String: URL] = [:]
        for (id, rgb) in [("red", color(1, 0, 0)), ("blue", color(0, 0, 1)), ("green", color(0, 1, 0))] {
            urls[id] = try await SolidVideoFixture.make(directory: directory, name: id, color: rgb, seconds: 3)
        }
        var recipe = try makeRecipe(urls, [
            [TimelineClip(id: "a", sourceAssetID: "red", sourceDuration: 2, splitScreen: .init(sectionID: "s", panelIndex: 0)),
             TimelineClip(id: "b", sourceAssetID: "blue", sourceDuration: 2, timelineStart: 1,
                transition: .init(kind: .crossfade, duration: 1), splitScreen: .init(sectionID: "s", panelIndex: 0))],
            [TimelineClip(id: "c", sourceAssetID: "green", sourceDuration: 1.5, splitScreen: .init(sectionID: "s", panelIndex: 1))]
        ], end: 3)
        recipe.splitScreenSections = [.init(id: "s", start: 0, end: 3, panelWeights: [1, 1], backgroundColor: "#FFFFFF", collapseEmptyPanels: true)]
        try recipe.validate()
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = try await export(recipe, urls, directory, "collapse-transition")
        for frames in [generator(preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            try await assertPixel(frames, time: 0.5, y: 90, expected: red)
            try await assertPixel(frames, time: 0.5, y: 70, expected: green)
            try await assertPixel(frames, time: 1.25, y: 70, expected: green)
            for (time, alpha, rows) in [(1.25, 0.25, [150, 90]), (1.5, 0.5, [150, 80, 10]), (1.75, 0.75, [150, 80, 10]), (2.5, 1, [150, 10])] {
                for y in rows {
                    let blend = try await pixel(frames, time: time, x: 48, y: y)
                    XCTAssertEqual(Int(blend[0]), Int(255 * (1 - alpha)), accuracy: 40, "red at \(time), \(y): \(blend)")
                    XCTAssertEqual(Int(blend[2]), Int(255 * alpha), accuracy: 40, "blue at \(time), \(y): \(blend)")
                    XCTAssertLessThan(blend[1], 90, "time \(time), \(y): \(blend)")
                }
            }
        }
    }

    @MainActor func testSupportingCardIsRedrawnForEachPanelSize() async throws {
        let directory = try artifacts()
        let photo = directory.appendingPathComponent("card.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 64, height: 128)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let video = try await SolidVideoFixture.make(directory: directory, name: "green", color: color(0, 1, 0), seconds: 2)
        let urls = ["card": photo, "green": video]
        var recipe = try makeRecipe(urls, [
            [TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, stillLayout: .supportingCard, splitScreen: .init(sectionID: "s", panelIndex: 0))],
            [TimelineClip(id: "video", sourceAssetID: "green", sourceDuration: 1, splitScreen: .init(sectionID: "s", panelIndex: 1))]
        ], end: 2)
        recipe.splitScreenSections = [.init(id: "s", start: 0, end: 2, panelWeights: [1, 1], backgroundColor: "#FFFFFF", collapseEmptyPanels: true)]
        try recipe.validate()
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = try await export(recipe, urls, directory, "collapse-card")
        for frames in [generator(preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            try await assertPixel(frames, time: 0.5, x: 48, y: 120, expected: red)
            try await assertPixel(frames, time: 0.5, x: 20, y: 120, expected: black)
            try await assertPixel(frames, time: 0.5, x: 48, y: 40, expected: green)
            try await assertPixel(frames, time: 1.5, x: 48, y: 40, expected: red)
            try await assertPixel(frames, time: 1.5, x: 13, y: 80, expected: black)
            try await assertPixel(frames, time: 1.5, x: 4, y: 80, expected: red)
        }
    }

    @MainActor func testInPlaceEditKeepsEachLayoutsPanel() async throws {
        let directory = try artifacts()
        let (recipe, urls) = try await collapseScene(directory)
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        XCTAssertEqual(try XCTUnwrap(live.mediaSelectionBounds(id: "top", time: 0.75)).height, 1.0 / 3, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(live.mediaSelectionBounds(id: "top", time: 2.5)).height, 1, accuracy: 0.001)
        var next = recipe
        next.tracks[0].clips[0].transform = MediaTransform(scale: 0.5)
        try live.updateText(recipe: next)
        let frames = generator(live.preview)
        try await assertPixel(frames, time: 0.25, x: 48, y: 150, expected: red)
        try await assertPixel(frames, time: 0.25, x: 10, y: 150, expected: white)
        try await assertPixel(frames, time: 2.5, x: 48, y: 110, expected: green)
        try await assertPixel(frames, time: 2.5, x: 48, y: 130, expected: white)
        let full = try XCTUnwrap(live.mediaSelectionBounds(id: "top", time: 2.5))
        XCTAssertEqual(full.centerY, 0.5, accuracy: 0.001)
        XCTAssertEqual(full.height, 0.5, accuracy: 0.001)
    }

    @MainActor func testMattedSpeakerFollowsItsPanelThroughTheReflow() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (120, 60), seconds: 3)
        let room = try await writeEmptyRoomClip(directory: directory, size: (120, 60), seconds: 3)
        var scene = try speakerRecipe(canvas: Canvas(width: 120, height: 180), speaker: speaker, duration: 3,
            background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)), extraAssets: ["other": room])
        scene.recipe.tracks = [
            .init(id: "top", kind: .video, clips: [.init(id: "speaker", sourceAssetID: "speaker", sourceDuration: 3,
                splitScreen: .init(sectionID: "s", panelIndex: 0))]),
            .init(id: "bottom", kind: .video, clips: [.init(id: "other", sourceAssetID: "other", sourceDuration: 1.5,
                splitScreen: .init(sectionID: "s", panelIndex: 1))])
        ]
        scene.recipe.splitScreenSections = [.init(id: "s", start: 0, end: 3, panelWeights: [1, 1], backgroundColor: "#FFFFFF", collapseEmptyPanels: true)]
        for framed in [true, false] {
            scene.recipe.speakerBackground = SpeakerBackgroundLayer(assetID: "visual-background", mediaKind: VisualMediaKind.image,
                speakerFrame: framed ? .init(widthFraction: 0.5, xFraction: 0.5, yFraction: 0.5) : nil, subjectClipIDs: ["speaker"])
            try scene.recipe.validate()
            let live = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
            if framed {
                let half = try XCTUnwrap(live.speakerViewport(time: 0.75, clipID: "speaker"))
                XCTAssertEqual(half.minY, 0, accuracy: 0.001)
                XCTAssertEqual(half.height, 0.5, accuracy: 0.001)
                XCTAssertEqual(try XCTUnwrap(live.speakerViewport(time: 2.25, clipID: "speaker")).height, 1, accuracy: 0.001)
                continue
            }
            let output = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: "collapse-speaker")
            let player = generator(live.preview)
            for frames in [player, AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
                let split = try await speakerFrame(frames, at: 0.75)
                XCTAssertTrue(isColor(split.rgb(fx: 0.5, fy: 0.35), SpeakerColor.person), "Speaker in the top half")
                XCTAssertTrue(isColor(split.rgb(fx: 0.05, fy: 0.35), SpeakerColor.background), "Background in panel space")
                XCTAssertTrue(isColor(split.rgb(fx: 0.5, fy: 0.75), SpeakerColor.room, tolerance: 90), "Unowned clip keeps its room")
                let alone = try await speakerFrame(frames, at: 2.25)
                XCTAssertTrue(isColor(alone.rgb(fx: 0.1, fy: 0.75), SpeakerColor.person), "Speaker fills the canvas")
                XCTAssertTrue(isColor(alone.rgb(fx: 0.5, fy: 0.1), SpeakerColor.background), "Background fills the canvas")
            }
        }
    }

    @MainActor private func speakerFrame(_ generator: AVAssetImageGenerator, at time: Double) async throws -> SpeakerTestFrame {
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        return try SpeakerTestFrame(try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image)
    }
}
#endif
