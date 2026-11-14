import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1394: chat makes part of a split full screen with a server port of `splitFullScreenResult`
/// (`src/apps/api/app/kria/split_full_screen.py`). Both planners answer the cases in
/// `src/apps/api/tests/fixtures/split_full_screen.json`, which the server generates
/// (`tests/kria/test_split_full_screen_parity.py`). This test runs the real editor action on each case
/// and fails when the phone's timeline, sections or moved lanes differ from the shared answer, or
/// when one side refuses a case the other plans.
final class NativeSplitFullScreenParityTests: XCTestCase {
    private static var editable: [String: EditorCapability] {
        ["timeline": .init(editable: true), "split_screen": .init(editable: true), "split_collapse": .init(editable: true)]
    }

    private static func fixture() throws -> [String: JSONValue] {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { root.deleteLastPathComponent() }
        let data = try Data(contentsOf: root.appendingPathComponent("src/apps/api/tests/fixtures/split_full_screen.json"))
        return try JSONDecoder().decode([String: JSONValue].self, from: data)
    }

    @MainActor
    private func session(for test: [String: JSONValue]) throws -> NativeEditorSession {
        var sections: [String: JSONValue] = [
            "timeline_slots": try XCTUnwrap(test["slots"]),
            "split_screen_sections": try XCTUnwrap(test["split_screen_sections"]),
        ]
        for (key, value) in test["lanes"]?.objectValue ?? [:] { sections[key] = value }
        var document = EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(sections)])])
        document.capabilities = Self.editable
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: ["render_destination": .string("device")]))
        session.replaceDocumentForTesting(document)
        var resolved: [Int: ResolvedEditorSource] = [:]
        for source in test["sources"]?.arrayValue ?? [] {
            let row = try XCTUnwrap(source.objectValue)
            let index = Int(try XCTUnwrap(row["clip_index"]?.numberValue))
            resolved[index] = ResolvedEditorSource(clipIndex: index, mediaID: "m\(index)",
                asset: MediaAsset(id: "a\(index)", relativePath: "a\(index).mp4",
                    fingerprint: .init(hex: String(repeating: "a", count: 64), byteCount: 100), duration: row["duration_s"]?.numberValue),
                url: URL(fileURLWithPath: "/fixture/a\(index).mp4"))
        }
        if !resolved.isEmpty { session.setResolvedSourcesForTesting(resolved) }
        let sectionID = try XCTUnwrap(test["section_id"]?.stringValue)
        if let panel = test["selected_panel"]?.numberValue {
            let slot = try XCTUnwrap(session.document.clips.first { $0.splitSectionID == sectionID && $0.splitPanelIndex == Int(panel) })
            session.select(.init(kind: .clip, id: try XCTUnwrap(slot.id)), seekToStart: false)
        }
        session.currentTime = try XCTUnwrap(test["playhead_s"]?.numberValue)
        return session
    }

    /// The slot keys the fixture compares, new ids named `new-<n>` in the order they appear.
    private func canonicalSlots(_ document: EditorDocument, keys: [String], known: Set<String>) -> [[String: JSONValue]] {
        var names: [String: String] = [:]
        func name(_ value: JSONValue) -> JSONValue {
            guard case .string(let id) = value, !known.contains(id) else { return value }
            if names[id] == nil { names[id] = "new-\(names.count + 1)" }
            return .string(names[id]!)
        }
        // The timeline section exactly as the editor's Save writes it.
        let rows = document.snapshot(for: .timeline)?.objectValue?["slots"]?.arrayValue ?? []
        return rows.map { row in
            guard case .object(let encoded) = row else { return [:] }
            var item: [String: JSONValue] = [:]
            for key in keys {
                guard let value = encoded[key], value != .null, !(key == "removed" && value == .bool(false)) else { continue }
                item[key] = key == "slot_id" || key == "parent_segment_id" ? name(value) : value
            }
            if item["transition_after"] == nil { item["transition_after"] = .string("cut") }
            return item
        }
    }

    private func canonicalLanes(_ document: EditorDocument) -> [String: JSONValue] {
        func span(_ id: String, _ start: Double, _ end: Double) -> [String: JSONValue] {
            ["id": .string(id), "start_s": .number(start), "end_s": .number(end)]
        }
        let captions: [JSONValue] = document.captionCues.map { cue in
            var item = span(cue.id, cue.startS, cue.endS)
            if case .array(let words)? = cue.raw["words"], !words.isEmpty {
                item["words"] = .array(words.compactMap { word in
                    guard let raw = word.objectValue else { return nil }
                    return .object(["start_s": raw["start_s"] ?? .null, "end_s": raw["end_s"] ?? .null])
                })
            }
            return .object(item)
        }
        let effects: [JSONValue] = document.soundEffects.map { effect in
            if let point = effect.pointS { return .object(["id": .string(effect.id), "at_s": .number(point)]) }
            return .object(span(effect.id, effect.startS, effect.endS))
        }
        let overlays: [JSONValue] = document.mediaOverlays.map { .object(span($0.id, $0.startS, $0.endS)) }
        return ["caption_cues": .array(captions), "sound_effects": .array(effects), "media_overlays": .array(overlays)]
    }

    private func assertClose(_ actual: JSONValue, _ expected: JSONValue, tolerance: Double, _ path: String,
                             file: StaticString = #filePath, line: UInt = #line) {
        switch (actual, expected) {
        case (.number(let a), .number(let b)):
            XCTAssertEqual(a, b, accuracy: tolerance, path, file: file, line: line)
        case (.object(let a), .object(let b)):
            XCTAssertEqual(Set(a.keys), Set(b.keys), path, file: file, line: line)
            for key in b.keys where a[key] != nil { assertClose(a[key]!, b[key]!, tolerance: tolerance, "\(path).\(key)", file: file, line: line) }
        case (.array(let a), .array(let b)):
            XCTAssertEqual(a.count, b.count, path, file: file, line: line)
            for (index, pair) in zip(a, b).enumerated() { assertClose(pair.0, pair.1, tolerance: tolerance, "\(path)[\(index)]", file: file, line: line) }
        default:
            XCTAssertEqual(actual, expected, path, file: file, line: line)
        }
    }

    @MainActor
    func testThePhonePlannerGivesTheSharedAnswer() throws {
        let fixture = try Self.fixture()
        let tolerance = fixture["tolerance_s"]?.numberValue ?? 0.000001
        let keys = (fixture["slot_keys"]?.arrayValue ?? []).compactMap(\.stringValue)
        let cases = try XCTUnwrap(fixture["cases"]?.arrayValue)
        XCTAssertGreaterThan(cases.count, 10)
        for value in cases {
            let test = try XCTUnwrap(value.objectValue)
            let name = test["name"]?.stringValue ?? "?"
            let session = try session(for: test)
            let original = session.document
            let side: NativeSplitFullScreenSide = test["side"]?.stringValue == "until" ? .untilPlayhead : .fromPlayhead
            let sectionID = try XCTUnwrap(test["section_id"]?.stringValue)
            let canMake = session.canMakeSplitFullScreen(sectionID: sectionID, side: side)
            session.makeSplitFullScreen(sectionID: sectionID, side: side)
            guard case .object(let expected)? = test["expected"] else {
                XCTAssertFalse(canMake, "\(name): the server refuses it, the phone offers it")
                XCTAssertEqual(session.document, original, "\(name): the server refuses it, the phone changed the edit")
                continue
            }
            XCTAssertTrue(canMake, "\(name): the server plans it, the phone does not offer it")
            let known = Set((test["slots"]?.arrayValue ?? []).compactMap { $0.objectValue?["slot_id"]?.stringValue })
            let slots = canonicalSlots(session.document, keys: keys, known: known)
            assertClose(.array(slots.map(JSONValue.object)), try XCTUnwrap(expected["slots"]), tolerance: tolerance, "\(name).slots")
            let sections: [JSONValue] = session.document.splitScreenSections.map {
                .object(["id": .string($0.id), "duration_s": .number($0.durationS)])
            }
            assertClose(.array(sections), try XCTUnwrap(expected["split_screen_sections"]), tolerance: tolerance, "\(name).sections")
            if case .object(let lanes)? = expected["lanes"], !lanes.isEmpty {
                assertClose(.object(canonicalLanes(session.document)), .object(lanes), tolerance: tolerance, "\(name).lanes")
            }
            XCTAssertTrue(session.document.splitScreenIsValid, "\(name): the result is not a valid split")
        }
    }
}
