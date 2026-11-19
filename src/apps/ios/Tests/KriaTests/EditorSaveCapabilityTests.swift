import XCTest
@testable import Kria

/// KRI-1423: the phone's half of the editor capability Save matrix.
///
/// `Fixtures/editor_save_capabilities.json` is GENERATED from the server's own table
/// (`tests/routes/test_editor_capability_save_matrix.py`, with a freshness test). Every row is a document the
/// server Saves. The phone must read each one back and accept it in its own Save guard, because a document the
/// client refuses before sending (`splitScreenIsValid`) is a Save the creator cannot make either.
final class EditorSaveCapabilityTests: XCTestCase {
    private func rows() throws -> [[String: JSONValue]] {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<2 { root.deleteLastPathComponent() }
        let data = try Data(contentsOf: root.appendingPathComponent("Fixtures/editor_save_capabilities.json"))
        let file = try JSONDecoder().decode([String: JSONValue].self, from: data)
        return try XCTUnwrap(file["rows"]?.arrayValue).compactMap(\.objectValue)
    }

    func testTheClientGuardAcceptsEveryDocumentTheServerSaves() throws {
        let rows = try rows()
        XCTAssertGreaterThanOrEqual(rows.count, 30)
        for row in rows {
            let id = row["id"]?.stringValue ?? "?"
            let sections = try XCTUnwrap(row["sections"]?.objectValue, id)
            let document = EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(sections)])])
            XCTAssertTrue(document.splitScreenIsValid, "\(id): the phone would refuse to send a document the server saves")
            let saved = (sections["timeline_slots"]?.arrayValue ?? []).compactMap(\.objectValue)
            let active = Set(saved.filter { $0["removed"]?.boolValue != true }.compactMap { $0["slot_id"]?.stringValue })
            XCTAssertEqual(Set(document.clips.filter { !$0.removed }.compactMap(\.id)), active, "\(id): slots lost on read")
            XCTAssertEqual(document.splitScreenSections.map(\.id),
                           (sections["split_screen_sections"]?.arrayValue ?? []).compactMap { $0.objectValue?["id"]?.stringValue }, id)
        }
    }

    func testThePhotoRowsAreInTheTable() throws {
        let ids = Set(try rows().compactMap { $0["id"]?.stringValue })
        // KRI-1423: a photo cut or trimmed in the editor carries a moved `in_s`; these rows are the ones the server used to refuse.
        XCTAssertTrue(ids.isSuperset(of: ["photo-cut-in-panel", "photo-trim-start-in-panel", "photo-cut-full-screen"]))
    }
}
