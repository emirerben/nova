import XCTest
@testable import Kria

@MainActor final class NativeEditorAvailabilityTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testUnavailablePlanIsNotReportedAsAnUnsavedEditConflict() async {
        NativeEditorURLProtocol.handler = { _ in (409, Data(#"{"detail":"Content plan is unavailable"}"#.utf8)) }
        do { _ = try await NativeEditorTestSupport.api().editorVariants(jobID: UUID()); XCTFail("Expected unavailable plan") }
        catch { XCTAssertEqual(error as? APIError, .contentPlanUnavailable) }
    }

    func testNoReadyVariantIsNotReportedAsAnUnsavedEditConflict() async {
        NativeEditorURLProtocol.handler = { _ in (409, Data(#"{"detail":"Video is not ready to open in the editor."}"#.utf8)) }
        do { _ = try await NativeEditorTestSupport.api().openJobInEditor(jobID: UUID()); XCTFail("Expected unavailable edit") }
        catch { XCTAssertEqual(error as? APIError, .editorNotReady) }
    }

    func testUnknownConflictRetainsRevisionProtection() async {
        NativeEditorURLProtocol.handler = { _ in (412, Data(#"{"detail":"baseline_conflict"}"#.utf8)) }
        do { _ = try await NativeEditorTestSupport.api().openJobInEditor(jobID: UUID()); XCTFail("Expected conflict") }
        catch { XCTAssertEqual(error as? APIError, .conflict) }
    }

    // MARK: - Only a baseline conflict means "a newer version exists" (B5)

    private func conflict(status: Int = 409, body: String) async -> APIError? {
        NativeEditorURLProtocol.handler = { _ in (status, Data(body.utf8)) }
        do { _ = try await NativeEditorTestSupport.api().openJobInEditor(jobID: UUID()); return nil }
        catch { return error as? APIError }
    }

    func testBaselineConflictIsRecognisedFromEveryShapeTheServerUses() async {
        let bare = await conflict(body: #"{"detail":"baseline_conflict"}"#)
        XCTAssertEqual(bare?.isBaselineConflict, true, "the Save route's plain detail")
        let problem = await conflict(body: #"{"detail":"baseline_conflict","problem":{"code":"baseline_conflict","recovery":"reload","current_base_generation":"g9"}}"#)
        XCTAssertEqual(problem?.isBaselineConflict, true)
        XCTAssertEqual(problem?.conflictCurrentBaseGeneration, "g9")
        let recovery = await conflict(status: 412, body: #"{"problem":{"code":"something_new","recovery":"reload"}}"#)
        XCTAssertEqual(recovery?.isBaselineConflict, true, "the server's own recovery instruction is honoured")
        XCTAssertTrue(APIError.baselineConflict.isBaselineConflict, "the client's own generation mismatch")
    }

    func testEveryOtherConflictIsNotAVersionMismatch() async {
        for body in [#"{"detail":"editor_draft_unavailable"}"#, #"{"detail":{"code":"TIMELINE_STALE"}}"#,
                     #"{"detail":"variant_initial_render_in_progress"}"#, #"{"detail":"deletion_target_stale"}"#,
                     #"{"detail":"editor_source_catalog_invalid"}"#,
                     #"{"detail":"x","problem":{"code":"deletion_target_stale","recovery":"retry"}}"#, ""] {
            let error = await conflict(body: body)
            XCTAssertEqual(error, .conflict, body)
            XCTAssertEqual(error?.isBaselineConflict, false, body)
        }
        XCTAssertFalse(APIError.conflict.isBaselineConflict, "a conflict with no information proves nothing about versions")
    }
}
