import XCTest
@testable import Kria

/// The diagnostics file is a small ring (KRI-587 sized it so a failure survives the seconds after an editor opens),
/// written on the main thread. Routine events, one per caption keystroke, must not touch it or evict a failure.
final class NativePreviewDiagnosticsTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("diagnostics-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        NativePreviewDiagnostics.directoryOverride = directory
    }

    override func tearDownWithError() throws {
        NativePreviewDiagnostics.directoryOverride = nil
        try? FileManager.default.removeItem(at: directory)
    }

    private var file: URL { directory.appendingPathComponent("native-preview-diagnostics.json") }

    private func stages() throws -> [String] {
        try JSONDecoder().decode([[String: String]].self, from: Data(contentsOf: file)).compactMap { $0["stage"] }
    }

    func testABurstOfRoutineRebuildsDoesNotTouchTheFileOrEvictAFailure() throws {
        NativePreviewDiagnostics.record("speaker-background-hidden", fields: ["reason": "hidden_run"])
        let before = try Data(contentsOf: file)
        let modified = try FileManager.default.attributesOfItem(atPath: file.path)[.modificationDate] as? Date

        for _ in 0..<200 {
            NativePreviewDiagnostics.note("preview-rebuild", fields: ["documentChanged": "1"])
            NativePreviewDiagnostics.note("preview-updated-in-place")
            NativePreviewDiagnostics.note("player-replaced", fields: ["was": "finished", "playing": "1"])
        }

        XCTAssertEqual(try Data(contentsOf: file), before, "a routine event does no file I/O")
        XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: file.path)[.modificationDate] as? Date, modified)
        XCTAssertEqual(try stages(), ["speaker-background-hidden"], "the stored failure is still the only event")
    }

    func testTheCountsOfRoutineEventsRideOnTheNextFailureRecord() throws {
        for _ in 0..<40 { NativePreviewDiagnostics.note("preview-updated-in-place") }
        NativePreviewDiagnostics.note("player-replaced", fields: ["was": "finished", "playing": "1"])
        NativePreviewDiagnostics.record("finished-render-fallback", fields: ["error": "x"], attachRoutine: true)

        let events = try JSONDecoder().decode([[String: String]].self, from: Data(contentsOf: file))
        let failure = try XCTUnwrap(events.last)
        XCTAssertEqual(failure["stage"], "finished-render-fallback")
        XCTAssertTrue(try XCTUnwrap(failure["routine"]).contains("preview-updated-in-place=40"), "\(failure)")
        XCTAssertTrue(try XCTUnwrap(failure["routine"]).contains("player-replaced=1"), "\(failure)")
        XCTAssertTrue(try XCTUnwrap(failure["recent"]).contains("player-replaced was=finished"), "the last routine events are named: \(failure)")
    }
}
