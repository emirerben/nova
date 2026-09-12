import XCTest
@testable import Kria

#if DEBUG
final class NativeEditorFixtureTests: XCTestCase {
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    func testAllAccountFreeFixtureShapesAreDeterministic() {
        let shapes = NativeEditorUITestFixtures.Shape.allCases
        XCTAssertEqual(shapes.map(\.rawValue), ["projected-captions", "source-text", "two-text", "boundary", "all-lanes", "stress-71", "unknown-sections"])
        for shape in shapes {
            let first = NativeEditorUITestFixtures.draft(for: shape)
            let second = NativeEditorUITestFixtures.draft(for: shape)
            XCTAssertEqual(first, second, "Fixture \(shape.rawValue) must be deterministic")
            XCTAssertEqual(first.projectID, UUID(uuidString: "00000000-0000-4000-8000-000000000999"))
        }
    }

    func testTwoTextFixtureRetainsDistinctTimedRecords() throws {
        let draft = NativeEditorUITestFixtures.twoText
        XCTAssertEqual(draft.text.map(\.content), ["Opening question", "Your story ritual"])
        let sections = try XCTUnwrap(Self.sections(in: draft.serverSnapshot))
        let records = try XCTUnwrap(Self.array(sections["text_elements"]))
        XCTAssertEqual(records.count, 2)
        XCTAssertEqual(Self.number(Self.object(records[0])?["start_s"]), 0)
        XCTAssertEqual(Self.number(Self.object(records[0])?["end_s"]), 1.5)
        XCTAssertEqual(Self.number(Self.object(records[1])?["start_s"]), 1.5)
        XCTAssertEqual(Self.number(Self.object(records[1])?["end_s"]), 3)
    }

    func testBoundaryFixtureIncludesHalfOpenAndInvalidTimingCases() throws {
        let draft = NativeEditorUITestFixtures.boundary
        let sections = try XCTUnwrap(Self.sections(in: draft.serverSnapshot))
        let records = try XCTUnwrap(Self.array(sections["text_elements"]))
        XCTAssertEqual(records.count, 3)
        XCTAssertEqual(Self.number(Self.object(records[1])?["start_s"]), 2)
        XCTAssertEqual(Self.number(Self.object(records[1])?["end_s"]), 4)
        XCTAssertEqual(Self.number(Self.object(records[2])?["start_s"]), 4)
        XCTAssertEqual(Self.number(Self.object(records[2])?["end_s"]), 4)
        XCTAssertEqual(Self.array(sections["caption_cues"])?.count, 2)
    }

    func testAllLanesFixtureRetainsEveryServerSection() throws {
        let draft = NativeEditorUITestFixtures.allLanes
        let sections = try XCTUnwrap(Self.sections(in: draft.serverSnapshot))
        let expected = [
            "timeline_slots", "text_elements", "caption_cues", "music_track_id", "music_window", "audio_mix",
            "sound_effects", "media_overlays", "visual_blocks", "motion_scenes", "camera_effects", "carousel_moment",
        ]
        for key in expected { XCTAssertNotNil(sections[key], "Missing fixture section \(key)") }
        XCTAssertEqual(Self.array(sections["sound_effects"])?.count, 1)
        XCTAssertEqual(Self.array(sections["media_overlays"])?.count, 1)
    }

    func testStressFixtureMatchesThe71SlotAnd43AssetShape() throws {
        let draft = NativeEditorUITestFixtures.stress
        XCTAssertEqual(draft.clips.count, 71)
        XCTAssertEqual(Set(draft.clips.compactMap(\.sourceClipIndex)).count, 43)
        let sections = try XCTUnwrap(Self.sections(in: draft.serverSnapshot))
        XCTAssertEqual(Self.array(sections["timeline_slots"])?.count, 71)
        XCTAssertEqual(Self.array(sections["caption_cues"])?.count, 40)
        XCTAssertEqual(Self.number(Self.object(sections["fixture_metadata"])?["source_asset_count"]), 43)
    }

    func testUnknownRootSectionAndItemFieldsSurviveCompatibilityProjection() throws {
        let draft = NativeEditorUITestFixtures.unknownSections
        var document = EditorDocument.decode(snapshot: draft.serverSnapshot)
        document.textElements[0].text = "Edited"
        let persisted = document.encodeSnapshot()
        XCTAssertEqual(persisted["future_root_key"], .string("keep"))
        let sections = try XCTUnwrap(Self.sections(in: persisted))
        XCTAssertEqual(Self.object(sections["future_section"])?["opaque"], .array([.number(1), .bool(true), .null]))
        let text = try XCTUnwrap(Self.array(sections["text_elements"])?.first)
        XCTAssertEqual(Self.object(text)?["future_item_field"], .string("keep"))
    }

    func testURLProtocolSupportCapturesAccountFreeAPIRequests() async throws {
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            return (200, Data("[]".utf8))
        }
        let projects = try await NativeEditorTestSupport.api().projects()
        XCTAssertEqual(projects, [])
    }

    private static func sections(in snapshot: [String: JSONValue]) -> [String: JSONValue]? {
        guard let payload = object(snapshot["editor_payload"]) else { return nil }
        return object(payload["sections"])
    }

    private static func object(_ value: JSONValue?) -> [String: JSONValue]? {
        if case let .object(value) = value { return value }
        return nil
    }

    private static func array(_ value: JSONValue?) -> [JSONValue]? {
        if case let .array(value) = value { return value }
        return nil
    }

    private static func number(_ value: JSONValue?) -> Double? {
        if case let .number(value) = value { return value }
        return nil
    }
}
#endif
