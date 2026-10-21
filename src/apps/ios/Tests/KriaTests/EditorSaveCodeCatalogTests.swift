import XCTest
@testable import Kria

/// KRI-770: the app words every server refusal it claims to, and says so honestly for the rest.
///
/// `Fixtures/editor_save_codes.json` lists the machine codes the editor Save can be refused with. The API's
/// `tests/routes/test_editor_save_code_catalog.py` fails when the server can raise a code that is in neither
/// list; this fails when the app disagrees with the list. Together a new server code cannot silently turn into
/// "This save was rejected" again, which is how a trimmed Troya clip lost its only explanation.
final class EditorSaveCodeCatalogTests: XCTestCase {
    private struct Catalog: Decodable {
        let mapped: [String]
        let generic: [String]
    }

    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    private func catalog() throws -> Catalog {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "editor_save_codes", withExtension: "json"))
        return try JSONDecoder().decode(Catalog.self, from: Data(contentsOf: url))
    }

    private func refusal(code: String) async throws -> EditorSaveError {
        NativeEditorURLProtocol.handler = { _ in (422, Data(#"{"detail":{"code":"\#(code)"}}"#.utf8)) }
        do {
            _ = try await save()
            XCTFail("Expected an editor save refusal for \(code)")
        } catch let error as EditorSaveError {
            return error
        }
        return .rejected
    }

    func testEveryMappedCodeHasSpecificCopy() async throws {
        let generic = EditorSaveError.rejected.localizedDescription
        for code in try catalog().mapped {
            let error = try await refusal(code: code)
            XCTAssertNotEqual(error, .rejected, "\(code) is listed as mapped but the app shows the generic line")
            XCTAssertNotEqual(error.localizedDescription, generic, code)
            XCTAssertTrue(error.localizedDescription.count > 20, code)
        }
    }

    /// A code listed as generic must really be generic, so the list cannot rot into a second source of truth.
    func testGenericCodesStayGenericUntilTheyAreWorded() async throws {
        for code in try catalog().generic {
            let error = try await refusal(code: code)
            XCTAssertEqual(error, .rejected, "\(code) now has copy: move it from 'generic' to 'mapped' in editor_save_codes.json")
        }
    }

    func testTrimWindowRefusalsTellTheCreatorWhatToDo() async throws {
        let short = try await refusal(code: "TIMELINE_INVALID_WINDOW")
        XCTAssertTrue(short.localizedDescription.contains("shorter"), short.localizedDescription)
        let past = try await refusal(code: "TIMELINE_OUT_OF_BOUNDS")
        XCTAssertTrue(past.localizedDescription.contains("end of its footage"), past.localizedDescription)
    }

    /// A readable sentence from the server is shown instead of being thrown away.
    func testReadableServerMessageIsShown() async throws {
        NativeEditorURLProtocol.handler = { _ in (422, Data(#"{"detail":"Video media is still being analyzed. Try again when its duration is ready."}"#.utf8)) }
        do {
            _ = try await save()
            XCTFail("Expected an editor save refusal")
        } catch let error as EditorSaveError {
            XCTAssertEqual(error.localizedDescription, "Video media is still being analyzed. Try again when its duration is ready. Your edits are still here.")
        }
        NativeEditorURLProtocol.handler = { _ in (422, Data(#"{"detail":"Clip upload must be video or image"}"#.utf8)) }
        do {
            _ = try await save()
            XCTFail("Expected an editor save refusal")
        } catch let error as EditorSaveError {
            XCTAssertEqual(error.localizedDescription, "Clip upload must be video or image. Your edits are still here.")
        }
    }

    func testMachineCodesAndDeveloperTextAreNotShownAsMessages() {
        XCTAssertFalse(EditorSaveError.isReadableServerMessage("TIMELINE_INVALID_WINDOW"))
        XCTAssertFalse(EditorSaveError.isReadableServerMessage("`ripple_v1` Carousel timing requires a non-empty sequence."))
        XCTAssertFalse(EditorSaveError.isReadableServerMessage("guided_story_source_stale"))
        XCTAssertFalse(EditorSaveError.isReadableServerMessage(""))
    }

    // MARK: harness (same shape as EditorSaveErrorTests)

    private func save() async throws -> EditorCommitResponse {
        try await NativeEditorTestSupport.api().editorCommit(
            itemID: "item",
            variantID: "variant",
            request: EditorCommitRequest(baseGeneration: "generation")
        )
    }
}
