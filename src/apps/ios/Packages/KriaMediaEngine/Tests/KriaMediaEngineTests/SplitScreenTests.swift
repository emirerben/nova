import XCTest
@testable import KriaMediaEngine
#if canImport(CoreImage)
import CoreImage
#endif

final class SplitScreenTests: XCTestCase {
    func testRoundTripRetainsIndependentMediaAndSectionDuration() throws {
        let section = SplitScreenSection(id: "s", start: 1, end: 5, panelWeights: [1, 2, 1], gutter: 0.02, backgroundColor: "#123456")
        let clips = [
            TimelineClip(id: "a", sourceAssetID: "video", sourceStart: 3, sourceDuration: 2, timelineStart: 1, rate: 2,
                transform: MediaTransform(scale: 1.2, rotationDegrees: 15), volume: 0.7, sourceCrop: .init(x: 0.1, y: 0.2, width: 0.5, height: 0.6), splitScreen: .init(sectionID: "s", panelIndex: 0)),
            TimelineClip(id: "b", sourceAssetID: "photo", sourceDuration: 2, timelineStart: 2, splitScreen: .init(sectionID: "s", panelIndex: 2))
        ]
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", tracks: clips.enumerated().map {
            TimelineTrack(id: "t\($0.offset)", kind: .video, clips: [$0.element])
        }, splitScreenSections: [section])
        let decoded = try RecipeJSON.decode(RecipeJSON.encode(recipe))
        XCTAssertEqual(decoded, recipe)
        XCTAssertEqual(TimelineMath.totalDuration(of: decoded), 5)
        XCTAssertTrue(decoded.effectiveCapabilities.contains(.splitScreen))
        XCTAssertEqual(decoded.tracks[0].clips[0].volume, 0.7)
        XCTAssertEqual(decoded.tracks[0].clips[0].sourceStart, 3)
    }

    func testRejectsInvalidSectionsAndMembership() throws {
        for weights in [[1.0], [1, 0], [1, .nan], [1, 1, 1, 1]] {
            XCTAssertThrowsError(try SplitScreenSection(id: "s", start: 0, end: 1, panelWeights: weights).validate())
        }
        XCTAssertThrowsError(try SplitScreenSection(id: "s", start: 1, end: 1, panelWeights: [1, 1]).validate())
        XCTAssertThrowsError(try SplitScreenSection(id: "s", start: 0, end: 1, panelWeights: [1, 1], backgroundColor: "red").validate())
    }

    func testRejectsOverflowUnknownMembershipAndUntransitionedPanelOverlap() throws {
        XCTAssertThrowsError(try SplitScreenSection(id: "s", start: 0, end: 2,
            panelWeights: [.greatestFiniteMagnitude, .greatestFiniteMagnitude]).validate())
        var recipe = EditRecipe(schemaVersion: 2, tracks: [.init(id: "v", kind: .video, clips: [
            .init(id: "a", sourceAssetID: "a", sourceDuration: 1, splitScreen: .init(sectionID: "missing", panelIndex: 0))
        ])], splitScreenSections: [.init(id: "s", start: 0, end: 2, panelWeights: [1, 1])])
        XCTAssertThrowsError(try recipe.validateSplitScreen())
        recipe.tracks[0].clips[0].splitScreen = .init(sectionID: "s", panelIndex: 2)
        XCTAssertThrowsError(try recipe.validateSplitScreen())
        recipe.tracks[0].clips[0].splitScreen = .init(sectionID: "s", panelIndex: 0)
        XCTAssertNoThrow(try recipe.validateSplitScreen())
        var duplicate = recipe.tracks[0].clips[0]
        duplicate.id = "b"
        recipe.tracks[0].clips.append(duplicate)
        XCTAssertThrowsError(try recipe.validateSplitScreen())
    }

    func testMembershipRejectsUnknownWireFields() throws {
        let wire = Data(#"{"section_id":"s","panel_index":0,"panel_indxe":1}"#.utf8)
        XCTAssertThrowsError(try RecipeJSON.decoder().decode(SplitScreenMembership.self, from: wire))
    }

    func testActualBackendCompiledRecipeDecodesAndValidates() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "split-screen-backend-recipe", withExtension: "json", subdirectory: "Fixtures"))
        let recipe = try RecipeJSON.decode(Data(contentsOf: url))
        try recipe.validate()
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 4)
        let top = try XCTUnwrap(recipe.tracks.flatMap(\.clips).first(where: { $0.id == "top" }))
        let bottom = try XCTUnwrap(recipe.tracks.flatMap(\.clips).first(where: { $0.id == "bottom" }))
        XCTAssertEqual(top.sourceStart, 2)
        XCTAssertEqual(top.sourceDuration, 6)
        XCTAssertEqual(top.rate, 2)
        XCTAssertNotNil(top.sourceCrop)
        XCTAssertEqual(bottom.timelineStart, 1)
        XCTAssertEqual(bottom.sourceStart, 7)
        XCTAssertEqual(bottom.volume, 0)
        XCTAssertEqual(bottom.splitScreen, .init(sectionID: "s", panelIndex: 1))
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(recipe)), recipe)
    }

    func testLegacyRecipeOmitsSectionField() throws {
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encode(EditRecipe())) as? [String: Any])
        XCTAssertNil(object["split_screen_sections"])
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(EditRecipe())).splitScreenSections, [])
    }

#if canImport(CoreImage)
    func testLargeFiniteWeightsNormalizeBeforeMultiplication() throws {
        let section = SplitScreenSection(id: "s", start: 0, end: 1,
            panelWeights: [.greatestFiniteMagnitude / 4, .greatestFiniteMagnitude / 4])
        try section.validate()
        let canvas = CGRect(x: 0, y: 0, width: 96, height: 160)
        XCTAssertEqual(section.panelRect(index: 0, canvas: canvas), CGRect(x: 0, y: 80, width: 96, height: 80))
        XCTAssertEqual(section.panelRect(index: 1, canvas: canvas), CGRect(x: 0, y: 0, width: 96, height: 80))
    }

    func testWeightedPanelsAreTopToBottomWithGutters() {
        let section = SplitScreenSection(id: "s", start: 1, end: 5, panelWeights: [1, 2, 1], gutter: 0.1)
        let canvas = CGRect(x: 0, y: 0, width: 100, height: 200)
        XCTAssertEqual(section.panelRect(index: 0, canvas: canvas), CGRect(x: 0, y: 160, width: 100, height: 40))
        XCTAssertEqual(section.panelRect(index: 1, canvas: canvas), CGRect(x: 0, y: 60, width: 100, height: 80))
        XCTAssertEqual(section.panelRect(index: 2, canvas: canvas), CGRect(x: 0, y: 0, width: 100, height: 40))
    }
#endif
}
