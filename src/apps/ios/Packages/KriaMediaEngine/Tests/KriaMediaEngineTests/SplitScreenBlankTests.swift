import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
#endif

// KRI-1421 blank block (`SplitScreenSection.blanks`, protocol 51, `splitBlankPanel`). Failure modes: a section without a
// blank changes its bytes; a blank is written without the capability or an old build draws the panel empty; a malformed
// blank (colour, panel, window, overlap with a clip in its panel, a section of only blanks) passes validation and fails
// later at render; a blank does not keep a collapsing panel on screen; the colour differs between preview and export, is
// drawn outside its panel or window, or covers the speaker, text or a neighbouring panel.
final class SplitScreenBlankTests: XCTestCase {
    private func section(_ blanks: [SplitScreenBlank] = [], collapse: Bool = false) -> SplitScreenSection {
        SplitScreenSection(id: "s", start: 0, end: 4, panelWeights: [1, 1], collapseEmptyPanels: collapse, blanks: blanks)
    }

    private func recipe(_ section: SplitScreenSection, panelOneClip: Bool = false) -> EditRecipe {
        var clips = [TimelineClip(id: "top", sourceAssetID: "a", sourceDuration: 4, splitScreen: .init(sectionID: "s", panelIndex: 0))]
        if panelOneClip { clips.append(TimelineClip(id: "low", sourceAssetID: "a", sourceDuration: 1, timelineStart: 2, splitScreen: .init(sectionID: "s", panelIndex: 1))) }
        return EditRecipe(schemaVersion: 2, tracks: clips.map { TimelineTrack(id: $0.id, kind: .video, clips: [$0]) }, splitScreenSections: [section])
    }

    func testBlanksAreWrittenOnlyWhenPresentAndRequireSplitBlankPanel() throws {
        let plain = section()
        let plainWire = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(plain)) as? [String: Any])
        XCTAssertEqual(Set(plainWire.keys), ["id", "start", "end", "panel_weights", "gutter", "background_color"])

        let blank = SplitScreenBlank(id: "fill", panelIndex: 1, start: 1, end: 3, color: "#FF0000")
        let filled = section([blank])
        let wire = try RecipeJSON.encoder().encode(filled)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: wire) as? [String: Any])
        let rows = try XCTUnwrap(object["blanks"] as? [[String: Any]])
        XCTAssertEqual(rows.first?["panel_index"] as? Int, 1)
        XCTAssertEqual(rows.first?["color"] as? String, "#FF0000")
        XCTAssertEqual(try RecipeJSON.decoder().decode(SplitScreenSection.self, from: wire), filled)

        let withBlank = recipe(filled)
        XCTAssertTrue(withBlank.effectiveCapabilities.isSuperset(of: [.splitScreen, .splitBlankPanel]))
        XCTAssertFalse(recipe(plain).effectiveCapabilities.contains(.splitBlankPanel))
        let old = DefaultRendererCapabilities(capabilities: withBlank.effectiveCapabilities.subtracting([.splitBlankPanel]))
        XCTAssertEqual(CapabilityNegotiator(provider: old).decide(for: withBlank).missingCapabilities, [.splitBlankPanel])
    }

    func testBlankDecodingIsStrict() throws {
        let base = ##"{"id":"s","start":0,"end":4,"panel_weights":[1,1],"gutter":0,"background_color":"#000000","blanks":["##
        let good = ##"{"id":"f","panel_index":1,"start":1,"end":3,"color":"#FF0000""##
        XCTAssertNoThrow(try RecipeJSON.decoder().decode(SplitScreenSection.self, from: Data((base + good + "}]}").utf8)))
        for bad in [good + #","extra":1}"#, good.replacingOccurrences(of: "\"color\":\"#FF0000\"", with: "\"color\":null") + "}",
                    good.replacingOccurrences(of: "#FF0000", with: "red") + "}", good.replacingOccurrences(of: "\"id\":\"f\",", with: "") + "}"] {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(SplitScreenSection.self, from: Data((base + bad + "]}").utf8)), bad)
        }
    }

    func testBackendShapedBlankRecipeDecodesValidatesAndRoundTrips() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "split-screen-blank-recipe", withExtension: "json", subdirectory: "Fixtures"))
        let recipe = try RecipeJSON.decode(Data(contentsOf: url))
        try recipe.validate()
        XCTAssertEqual(recipe.splitScreenSections[0].blanks, [SplitScreenBlank(id: "fill", panelIndex: 1, start: 1, end: 3, color: "#FF0000")])
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.splitScreen, .splitBlankPanel]))
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 4)
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(recipe)), recipe)
    }

    func testValidationRefusesABlankThatCannotRender() throws {
        let ok = SplitScreenBlank(id: "f", panelIndex: 1, start: 1, end: 3, color: "#FF0000")
        XCTAssertNoThrow(try recipe(section([ok])).validate())
        let cases: [(String, SplitScreenSection, Bool)] = [
            ("panel outside the section", section([SplitScreenBlank(id: "f", panelIndex: 2, start: 1, end: 3, color: "#FF0000")]), false),
            ("window past the section end", section([SplitScreenBlank(id: "f", panelIndex: 1, start: 1, end: 5, color: "#FF0000")]), false),
            ("window before the section start", section([SplitScreenBlank(id: "f", panelIndex: 1, start: -1, end: 2, color: "#FF0000")]), false),
            ("empty window", section([SplitScreenBlank(id: "f", panelIndex: 1, start: 2, end: 2, color: "#FF0000")]), false),
            ("named colour", section([SplitScreenBlank(id: "f", panelIndex: 1, start: 1, end: 3, color: "red")]), false),
            ("three-digit colour", section([SplitScreenBlank(id: "f", panelIndex: 1, start: 1, end: 3, color: "#F00")]), false),
            ("two blanks overlap", section([ok, SplitScreenBlank(id: "g", panelIndex: 1, start: 2.5, end: 3.5, color: "#00FF00")]), false),
            ("duplicate id", section([ok, SplitScreenBlank(id: "f", panelIndex: 1, start: 3, end: 4, color: "#00FF00")]), false),
            ("end to end is fine", section([ok, SplitScreenBlank(id: "g", panelIndex: 1, start: 3, end: 4, color: "#00FF00")]), true),
            ("blank over the clip in its own panel", section([ok, SplitScreenBlank(id: "g", panelIndex: 0, start: 1, end: 3, color: "#00FF00")]), false)
        ]
        for (name, value, valid) in cases {
            if valid { XCTAssertNoThrow(try recipe(value).validate(), name) } else { XCTAssertThrowsError(try recipe(value).validate(), name) }
        }
        // A blank over a clip in the same panel is refused, and so is a section with nothing but blanks.
        XCTAssertThrowsError(try recipe(section([ok]), panelOneClip: true).validate(), "a clip plays under the blank in panel 1")
        var onlyBlanks = recipe(section([ok]))
        onlyBlanks.tracks = []
        XCTAssertThrowsError(try onlyBlanks.validate())
    }

    func testABlankCountsAsAnActivePanelForCollapse() {
        let blank = SplitScreenBlank(id: "f", panelIndex: 1, start: 1, end: 3, color: "#FF0000")
        let value = recipe(section([blank], collapse: true))
        let clip = value.tracks[0].clips[0]
        let canvas = CGRect(x: 0, y: 0, width: 1, height: 1)
        let placements = value.panelPlacements(of: clip, start: 0, end: 4, canvas: canvas)
        XCTAssertEqual(placements.map { [$0.start, $0.end] }, [[0, 1], [1, 3], [3, 4]], "The blank's edges are layout cuts")
        XCTAssertEqual(placements.map { $0.viewport?.height }, [1, 0.5, 1])
        let own = value.panelPlacements(membership: .init(sectionID: "s", panelIndex: 1), start: 1, end: 3, canvas: canvas)
        XCTAssertEqual(own.map { $0.viewport?.height }, [0.5])
    }
}

#if canImport(AVFoundation)
extension SplitScreenMediaParityTests {
    /// 96x160 canvas over [0, 4). Panel 0 plays red "a" for the whole section. Panel 1 shows a green blank over [1, 3) and
    /// the white section background elsewhere (or, when collapsing, nothing).
    @MainActor private func blankScene(_ directory: URL, collapse: Bool) async throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let a = try await SolidVideoFixture.make(directory: directory, name: "a", color: color(1, 0, 0), seconds: 4)
        var recipe = try makeRecipe(["a": a], [
            [TimelineClip(id: "top", sourceAssetID: "a", sourceDuration: 4, splitScreen: .init(sectionID: "s", panelIndex: 0))]
        ], end: 4)
        recipe.splitScreenSections = [.init(id: "s", start: 0, end: 4, panelWeights: [1, 1], backgroundColor: "#FFFFFF", collapseEmptyPanels: collapse,
            blanks: [SplitScreenBlank(id: "fill", panelIndex: 1, start: 1, end: 3, color: "#00FF00")])]
        try recipe.validate()
        return (recipe, ["a": a])
    }

    @MainActor func testBlankPaintsItsPanelWindowInPreviewAndExport() async throws {
        let directory = try artifacts()
        let (recipe, urls) = try await blankScene(directory, collapse: false)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = try await export(recipe, urls, directory, "blank")
        for frames in [generator(preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            for time in [0.0, 0.5, 29.0 / 30, 1.0, 1.5, 2.5, 89.0 / 30, 3.0, 3.5] {
                let blankShowing = time >= 1 && time < 3
                try await assertPixel(frames, time: time, y: 130, expected: [255, 0, 0])
                try await assertPixel(frames, time: time, y: 30, expected: blankShowing ? [0, 255, 0] : [255, 255, 255])
                try await assertPixel(frames, time: time, x: 4, y: 30, expected: blankShowing ? [0, 255, 0] : [255, 255, 255])
                try await assertPixel(frames, time: time, x: 92, y: 79, expected: blankShowing ? [0, 255, 0] : [255, 255, 255])
            }
        }
    }

    @MainActor func testBlankKeepsItsPanelOnScreenWhileACollapsingSectionPlaysIt() async throws {
        let directory = try artifacts()
        let (recipe, urls) = try await blankScene(directory, collapse: true)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = try await export(recipe, urls, directory, "blank-collapse")
        for frames in [generator(preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            try await assertPixel(frames, time: 0.5, y: 130, expected: [255, 0, 0])
            try await assertPixel(frames, time: 0.5, y: 30, expected: [255, 0, 0])
            try await assertPixel(frames, time: 1.5, y: 130, expected: [255, 0, 0])
            try await assertPixel(frames, time: 1.5, y: 30, expected: [0, 255, 0])
            try await assertPixel(frames, time: 3.5, y: 30, expected: [255, 0, 0])
        }
    }
}
#endif
