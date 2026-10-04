import XCTest
@testable import Kria

final class EditorSaveErrorTests: XCTestCase {
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    func testKnownEditorSaveCodesUseSafeRecoveryCopy() async throws {
        let cases: [(String, EditorSaveError, String)] = [
            (#"{"detail":{"code":"unsupported_phone_edit","reason":"secret internals"}}"#, .unsupportedPhoneEdit, "text style or layout"),
            (#"{"detail":{"code":"phone_rendering_unavailable"}}"#, .phoneRenderingUnavailable, "edits are still here"),
            (#"{"detail":"guided_story_source_stale"}"#, .guidedStorySourceStale, "source changed or is unavailable"),
        ]
        for (body, expected, copy) in cases {
            NativeEditorURLProtocol.handler = { _ in (422, Data(body.utf8)) }
            do {
                _ = try await save()
                XCTFail("Expected editor save rejection")
            } catch let error as EditorSaveError {
                XCTAssertEqual(error, expected)
                XCTAssertTrue(error.localizedDescription.contains(copy))
                if expected == .unsupportedPhoneEdit {
                    XCTAssertEqual(error.localizedDescription, "This text style or layout can’t be saved on this iPhone yet. Your edits are still here; adjust the style or size and try again.")
                }
            }
        }
    }

    /// KRI-428: the song codes carry their own recovery copy; only "unavailable" shows the server's reason.
    func testSongCodesMapToRecoveryCopy() async throws {
        let cases: [(String, EditorSaveError, String)] = [
            (#"{"detail":{"code":"user_song_lipsync_locked","reason":"secret internals"}}"#, .userSongLipsyncLocked,
             "Lip-sync keeps the song where you filmed it."),
            (#"{"detail":{"code":"user_song_window_out_of_range","reason":"That edit runs past the end of your song."}}"#, .userSongWindowOutOfRange,
             "That start point runs past the end of your song. Slide it earlier."),
            (#"{"detail":{"code":"user_song_unavailable"}}"#, .userSongUnavailable(reason: nil),
             "This edit no longer has a song. Reopen the editor to continue."),
            (#"{"detail":{"code":"user_song_unavailable","reason":"The song was removed."}}"#, .userSongUnavailable(reason: "The song was removed."),
             "The song was removed."),
            (#"{"detail":"user_song_unavailable"}"#, .userSongUnavailable(reason: nil),
             "This edit no longer has a song. Reopen the editor to continue."),
        ]
        for (body, expected, copy) in cases {
            NativeEditorURLProtocol.handler = { _ in (422, Data(body.utf8)) }
            do {
                _ = try await save()
                XCTFail("Expected editor save rejection")
            } catch let error as EditorSaveError {
                XCTAssertEqual(error, expected, body)
                XCTAssertEqual(error.localizedDescription, copy)
            }
        }
    }

    func testValidationDetailWithTextElementsMapsToTextSettings() async throws {
        NativeEditorURLProtocol.handler = { _ in
            (422, Data(#"{"detail":[{"type":"string_type","loc":["body","text_elements",0,"text"],"msg":"internal user text"}]}"#.utf8))
        }
        do {
            _ = try await save()
            XCTFail("Expected editor save rejection")
        } catch let error as EditorSaveError {
            XCTAssertEqual(error, .invalidTextSettings)
            XCTAssertEqual(error.localizedDescription, "Text settings are invalid. Your edits are still here; review text, style, and timing.")
        }
    }

    func testUnknownMaliciousMessageIsNotExposed() async throws {
        let secret = "DROP DATABASE; user=creator@example.com"
        NativeEditorURLProtocol.handler = { _ in
            (422, Data(#"{"detail":{"code":"unknown","msg":"DROP DATABASE; user=creator@example.com"}}"#.utf8))
        }
        do {
            _ = try await save()
            XCTFail("Expected editor save rejection")
        } catch let error as EditorSaveError {
            XCTAssertEqual(error, .rejected)
            XCTAssertFalse(error.localizedDescription.contains(secret))
        }
    }

    func testUnrelated422RemainsRequestFailed() async throws {
        NativeEditorURLProtocol.handler = { _ in (422, Data(#"{"detail":{"code":"unknown"}}"#.utf8)) }
        do {
            _ = try await NativeEditorTestSupport.api().project(threadID: UUID())
            XCTFail("Expected request failure")
        } catch {
            XCTAssertEqual(error as? APIError, .requestFailed(status: 422))
        }
    }

    func testMalformedEditorDetailIsSafeAndMissingLocationDoesNotCrash() async throws {
        for body in [
            "not-json",
            #"{"detail":[{"type":"value_error","msg":"private details"}]}"#,
        ] {
            NativeEditorURLProtocol.handler = { _ in (422, Data(body.utf8)) }
            do {
                _ = try await save()
                XCTFail("Expected editor save rejection")
            } catch let error as EditorSaveError {
                XCTAssertEqual(error, .rejected)
                XCTAssertFalse(error.localizedDescription.contains("private details"))
            }
        }
    }

    private func save() async throws -> EditorCommitResponse {
        try await NativeEditorTestSupport.api().editorCommit(
            itemID: "item",
            variantID: "variant",
            request: EditorCommitRequest(baseGeneration: "generation")
        )
    }
}
