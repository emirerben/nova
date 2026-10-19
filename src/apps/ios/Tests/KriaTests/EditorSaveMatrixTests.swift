import XCTest
@testable import Kria

/// KRI-750: the phone's half of the editor Save contract.
///
/// The phone decides two things BEFORE a Save is sent: whether a source window is within the server's bound
/// (`KriaSourceWindowGeometry.isWithinBounds`) and how a text it created itself goes on the wire
/// (`EditorDocument.encodeSnapshot`, `"role": null`). KRI-736 shipped five server fixes because the phone and
/// the server disagreed on these. `Fixtures/editor_save_matrix.json` is GENERATED from the real server code
/// (`tests/routes/test_editor_save_contract_matrix.py::build_fixture`, with a freshness test); this replays it.
final class EditorSaveMatrixTests: XCTestCase {
    private struct Matrix: Decodable {
        struct Bound: Decodable {
            let name: String
            let inS: Double
            let durationS: Double
            let rate: Double
            let sourceDurationS: Double
            let verdict: String
            let code: String?
            let withinBounds: Bool
            enum CodingKeys: String, CodingKey {
                case name, rate, verdict, code
                case inS = "in_s"
                case durationS = "duration_s"
                case sourceDurationS = "source_duration_s"
                case withinBounds = "within_bounds"
            }
        }
        struct TextEncode: Decodable {
            let name: String
            let id: String
            let text: String
            let startS: Double
            let endS: Double
            let role: String?
            let serverRole: String
            enum CodingKeys: String, CodingKey {
                case name, id, text, role
                case startS = "start_s"
                case endS = "end_s"
                case serverRole = "server_role"
            }
        }
        let bounds: [Bound]
        let textEncode: [TextEncode]
        enum CodingKeys: String, CodingKey {
            case bounds
            case textEncode = "text_encode"
        }
    }

    private func matrix() throws -> Matrix {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "editor_save_matrix", withExtension: "json"))
        return try JSONDecoder().decode(Matrix.self, from: Data(contentsOf: url))
    }

    func testEverySourceWindowVerdictMatchesTheServer() throws {
        let file = try matrix()
        XCTAssertGreaterThanOrEqual(file.bounds.count, 10)
        for bound in file.bounds {
            let window = KriaSourceWindowGeometry.Window(start: bound.inS, length: bound.durationS)
            let within = KriaSourceWindowGeometry.isWithinBounds(window, rate: bound.rate, sourceDuration: bound.sourceDurationS)
            // The server answer is accept / fit (shortened onto the footage) / refuse; the phone asks the same
            // question before it writes, so "within" must be exactly "the server does not refuse".
            XCTAssertEqual(within, bound.verdict != "refuse", "\(bound.name): phone \(within) vs server \(bound.verdict) \(bound.code ?? "")")
            XCTAssertEqual(within, bound.withinBounds, bound.name)
        }
    }

    func testTheIncidentWindowsAreInTheMatrix() throws {
        let names = Set(try matrix().bounds.map(\.name))
        // Prod 2026-10-17: a window within 50 ms of the source end 422'd TIMELINE_OUT_OF_BOUNDS.
        XCTAssertTrue(names.isSuperset(of: ["overshoot_exactly_the_50ms_tolerance", "overshoot_60ms", "in_point_past_the_end"]))
    }

    func testBoundToleranceIsTheServers() throws {
        // The fixture pins the 50 ms edge from both sides: the tolerance constant may not drift from it.
        XCTAssertEqual(KriaSourceWindowGeometry.boundsTolerance, 0.05, accuracy: 1e-12)
        XCTAssertEqual(KriaSourceWindowGeometry.minimumLength, 0.1, accuracy: 1e-12)
    }

    func testANewTextIsEncodedWithTheRoleTheServerAccepts() throws {
        let file = try matrix()
        XCTAssertFalse(file.textEncode.isEmpty)
        for item in file.textEncode {
            var document = EditorDocument.decode(snapshot: ["base_generation": .string("legacy"), "text_elements": .array([])])
            document.textElements.append(EditorTextElement(id: item.id, text: item.text, startS: item.startS, endS: item.endS, role: item.role))
            guard case let .array(rows)? = document.encodeSnapshot()["text_elements"], case let .object(wire)? = rows.first else {
                return XCTFail("\(item.name): the new text is missing from the Save payload")
            }
            XCTAssertEqual(wire["id"], .string(item.id), item.name)
            XCTAssertEqual(wire["text"], .string(item.text), item.name)
            XCTAssertEqual(wire["start_s"], .number(item.startS), item.name)
            XCTAssertEqual(wire["end_s"], .number(item.endS), item.name)
            // nil role is an explicit JSON null (the server maps it to its default), never an invented value.
            XCTAssertEqual(wire["role"], item.role.map(JSONValue.string) ?? .null, item.name)
            if item.role == nil { XCTAssertEqual(item.serverRole, "generative_intro", item.name) }
        }
    }
}
