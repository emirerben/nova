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
}
