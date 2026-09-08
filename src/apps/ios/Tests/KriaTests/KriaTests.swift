import XCTest
import Security
import SwiftData
@testable import Kria

final class KriaTests: XCTestCase {
    override func tearDown() {
        URLProtocolStub.handler = nil
        super.tearDown()
    }
    func testCloudUploadsRequireExplicitConsent() throws {
        XCTAssertThrowsError(try UploadCoordinator().validate(source: .cloudDrive, purpose: .cloudRenderSource, consentGiven: false))
        XCTAssertNoThrow(try UploadCoordinator().validate(source: .cloudDrive, purpose: .cloudRenderSource, consentGiven: true))
        XCTAssertThrowsError(try UploadCoordinator().validate(source: .photos, purpose: .cloudRenderSource, consentGiven: false))
        XCTAssertNoThrow(try UploadCoordinator().validate(source: .photos, purpose: .analysisProxy, consentGiven: false))
    }

    func testGoogleOAuthUsesCodePKCEAndValidatesState() throws {
        let verifier = String(repeating: "v", count: 43)
        let challenge = GoogleOAuth.codeChallenge(for: verifier)
        let url = try XCTUnwrap(GoogleOAuth.authorizationURL(clientID: "client", redirectURI: "kria:/oauth2redirect", state: "state-1", nonce: "nonce-1", codeChallenge: challenge))
        let query = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
        XCTAssertEqual(query.first(where: { $0.name == "response_type" })?.value, "code")
        XCTAssertEqual(query.first(where: { $0.name == "code_challenge_method" })?.value, "S256")
        XCTAssertEqual(query.first(where: { $0.name == "state" })?.value, "state-1")
        XCTAssertNil(query.first(where: { $0.name == "id_token" }))
        let callback = try XCTUnwrap(URL(string: "kria:/oauth2redirect?code=abc&state=state-1"))
        XCTAssertEqual(try GoogleOAuth.authorizationCode(from: callback, expectedState: "state-1"), "abc")
        XCTAssertThrowsError(try GoogleOAuth.authorizationCode(from: callback, expectedState: "wrong")) { XCTAssertEqual($0 as? AuthError, .stateMismatch) }
        let request = GoogleOAuth.tokenRequest(clientID: "client", code: "abc", verifier: verifier, redirectURI: "kria:/oauth2redirect")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/x-www-form-urlencoded")
        let body = String(data: try XCTUnwrap(request.httpBody), encoding: .utf8)
        XCTAssertTrue(body?.contains("code_verifier=\(verifier)") == true)
    }

    func testGeneratedPKCEVerifierMeetsRFC7636Length() {
        let verifier = GoogleOAuth.makeCodeVerifier()
        XCTAssertGreaterThanOrEqual(verifier.count, 43)
        XCTAssertLessThanOrEqual(verifier.count, 128)
    }
    func testLocalEditorOperationsAreDeterministic() async throws {
        let draft = PreviewFixtures.draft
        let operation = LocalEditorOperations()
        let changed = try await operation.apply(.addText("Hello"), to: draft)
        XCTAssertEqual(changed.text.first?.content, "Hello")
        XCTAssertEqual(changed.revision, draft.revision + 1)
    }
    func testTokenStoreRoundTripUsesInjectedKeychainIdentity() throws {
        let store = KeychainTokenStore(service: "com.kria.tests.\(UUID().uuidString)")
        let expected = MobileSession(accessToken: "fixture-access", refreshToken: "fixture-refresh", expiresIn: 900)
        do { try store.write(expected) }
        catch let error as KeychainError where error.status == errSecMissingEntitlement {
            throw XCTSkip("Unsigned simulator test hosts do not have a Keychain entitlement.")
        }
        XCTAssertEqual(try store.read(), expected)
        try store.delete()
        XCTAssertNil(try store.read())
    }

    @MainActor func testSignInFailsClosedWhenSecureStorageRejectsSession() {
        let auth = AuthStore(tokenStore: FailingTokenStore())
        XCTAssertThrowsError(
            try auth.signIn(
                with: MobileSession(accessToken: "access", refreshToken: "refresh", expiresIn: 900),
                displayName: "Creator"
            )
        )
        XCTAssertFalse(auth.isSignedIn)
        XCTAssertNil(auth.displayName)
    }

    @MainActor func testLogicalSignOutSurvivesSecureDeletionFailure() throws {
        let session = MobileSession(accessToken: "access", refreshToken: "refresh", expiresIn: 900)
        try AuthStore(tokenStore: MemoryTokenStore()).signIn(with: session, displayName: nil)
        let store = DeleteFailingTokenStore(session)
        let auth = AuthStore(tokenStore: store)
        XCTAssertTrue(auth.isSignedIn)

        auth.signOut()

        XCTAssertFalse(auth.isSignedIn)
        XCTAssertFalse(AuthStore(tokenStore: store).isSignedIn)
        // A successful new authentication clears the durable invalidation bit
        // for following tests and for the real recovery path.
        try AuthStore(tokenStore: MemoryTokenStore()).signIn(with: session, displayName: nil)
    }

    func testDeltaCursorIsEncodedAsAQueryItem() async throws {
        let store = MemoryTokenStore()
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.query, "after_sequence=41")
            return (200, Data(#"{"thread_id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B","runtime_version":2,"status":"active","thread_revision":7,"events":[],"after_sequence":41,"next_after_sequence":41,"before_sequence":null,"previous_before_sequence":null,"has_more":false}"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: store, session: stubSession())
        let delta = try await api.threadDelta(threadID: PreviewFixtures.projectID, afterSequence: 41)
        XCTAssertEqual(delta.threadRevision, 7)
    }

    func testFormatSelectionUsesTheCreationActionContract() async throws {
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads/\(PreviewFixtures.projectID.uuidString)/actions")
            let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: Self.bodyData(request)) as? [String: Any])
            XCTAssertEqual(object["action"] as? String, "select_format")
            XCTAssertEqual(object["expected_revision"] as? Int, 7)
            let payload = try XCTUnwrap(object["payload"] as? [String: Any])
            XCTAssertEqual(payload["format"] as? String, "talking_to_camera")
            return (200, Self.threadResponse(jobStatus: nil, state: ["format": "talking_to_camera", "edit_format": "subtitled"]))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let thread = try await api.applyCreationAction(
            threadID: PreviewFixtures.projectID,
            action: "select_format",
            payload: ["format": .string("talking_to_camera")],
            expectedRevision: 7
        )
        XCTAssertEqual(thread.state?["edit_format"]?.stringValue, "subtitled")
    }

    func testTranscriptProjectionKeepsConversationAndHidesAuditEvents() {
        let user = ThreadEvent(
            id: "user-1", sequence: 1, revision: 1, role: "user", eventType: "user_message",
            content: "Keep the opening quick", payload: nil, createdAt: .now
        )
        let assistant = ThreadEvent(
            id: "assistant-1", sequence: 2, revision: 2, role: "assistant", eventType: "assistant_question",
            content: "Should it feel playful or polished?", payload: nil, createdAt: .now
        )
        let audit = ThreadEvent(
            id: "audit-1", sequence: 3, revision: 3, role: "system", eventType: "turn_started",
            content: "internal lifecycle detail", payload: nil, createdAt: .now
        )

        XCTAssertEqual(ChatTranscriptMessage.from(event: user)?.role, .user)
        XCTAssertEqual(ChatTranscriptMessage.from(event: assistant)?.role, .assistant)
        XCTAssertNil(ChatTranscriptMessage.from(event: audit))
    }

    func testDraftWriteSendsServerETagAndRevision() async throws {
        let store = MemoryTokenStore()
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "If-Match"), "\"draft-etag\"")
            let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: Self.bodyData(request)) as? [String: Any])
            XCTAssertEqual(object["expected_draft_revision"] as? Int, 2)
            return (200, Self.draftResponse(revision: 3, etag: "\"next-etag\""))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: store, session: stubSession())
        let draft = try await api.writeDraft(threadID: PreviewFixtures.projectID, snapshot: ["schema_version": .number(2)], expectedRevision: 2, etag: "\"draft-etag\"")
        XCTAssertEqual(draft.draftRevision, 3)
        XCTAssertEqual(draft.etag, "\"next-etag\"")
    }

    func testPersistedEditorLayersRoundTripFromServerDraft() throws {
        let clipID = UUID(), assetID = UUID(), textID = UUID(), trackID = UUID()
        let snapshot: [String: JSONValue] = [
            "schema_version": .number(2),
            "kind": .string("editor"),
            "intent": .string("A concise story"),
            "edit_format": .string("montage"),
            "editor_payload": .object([
                "base_generation": .string("generation-1"),
                "sections": .object([
                    "timeline_slots": .array([]),
                    "ios_editor": .object([
                        "clips": .array([.object([
                            "id": .string(clipID.uuidString), "asset_id": .string(assetID.uuidString),
                            "start": .number(0), "end": .number(4.5), "trim_in": .number(0.25), "trim_out": .number(4.75),
                        ])]),
                        "text": .array([.object([
                            "id": .string(textID.uuidString), "content": .string("Hello"), "x": .number(0.2), "y": .number(0.8), "style": .string("Fraunces"),
                        ])]),
                        "captions_enabled": .bool(true), "music": .object([
                            "track_id": .string(trackID.uuidString), "title": .string("Kria original"), "start": .number(1.25),
                        ]),
                    ]),
                ]),
            ]),
            "strategy": .null,
            "changes": .array([]),
        ]
        let server = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "initial", draftRevision: 4, snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: nil, snapshot: snapshot, canUndo: true, createdAt: .now)
        let draft = server.editorDraft(projectID: PreviewFixtures.projectID)
        XCTAssertEqual(draft.clips.first?.trimIn, 0.25)
        XCTAssertEqual(draft.text.first?.content, "Hello")
        XCTAssertEqual(draft.music?.trackID, trackID)
        XCTAssertTrue(draft.captions.enabled)
    }

    func testCreationThreadMapsTerminalJobStatus() async throws {
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads")
            return (200, Data(#"""
            [
              {"id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B","title":"Ready","status":"active","revision":3,"runtime_version":2,"active_job_id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE","job":{"id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE","status":"variants_ready"},"updated_at":"2026-09-07T12:00:00Z"},
              {"id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE","title":"Done","status":"active","revision":4,"runtime_version":2,"active_job_id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B","job":{"id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B","status":"done"},"updated_at":"2026-09-07T12:00:00Z"},
              {"id":"F53BB746-A61D-4F25-B49B-FC2B1D348EB1","title":"Failed","status":"active","revision":5,"runtime_version":2,"active_job_id":"BC7A40AA-824C-4082-86F0-1C50F71DE735","job":{"id":"BC7A40AA-824C-4082-86F0-1C50F71DE735","status":"processing_failed"},"updated_at":"2026-09-07T12:00:00Z"},
              {"id":"A158BB20-89F3-4F0B-996A-F47332F7DC2A","title":"Cancelled","status":"active","revision":6,"runtime_version":2,"active_job_id":"258D2E32-46D9-42E9-912C-674D6616E292","job":{"id":"258D2E32-46D9-42E9-912C-674D6616E292","status":"cancelled"},"updated_at":"2026-09-07T12:00:00Z"},
              {"id":"D85AA047-A71F-4625-A4EC-9542E6CC9D13","title":"No labeled tracks","status":"active","revision":7,"runtime_version":2,"active_job_id":"EB9AAB32-58FB-480C-8AB0-5D5D270A6D28","job":{"id":"EB9AAB32-58FB-480C-8AB0-5D5D270A6D28","status":"no_labeled_tracks"},"updated_at":"2026-09-07T12:00:00Z"}
            ]
            """#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let projects = try await api.projects()
        XCTAssertEqual(projects.map(\.status), [.ready, .ready, .failed, .failed, .failed])
    }

    func testCreationThreadMapsSelectedVariantMediaAndFullEvents() async throws {
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads/\(PreviewFixtures.projectID.uuidString)")
            XCTAssertEqual(request.url?.query, "projection=full")
            return (200, Data(#"""
            {
              "id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B",
              "title":"Ready",
              "status":"active",
              "revision":9,
              "runtime_version":2,
              "active_job_id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE",
              "state":{"selected_variant_id":"song_text"},
              "job":{
                "id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE",
                "status":"variants_ready",
                "current_phase":"complete",
                "variants":[
                  {"variant_id":"original_text","render_status":"ready","output_url":"https://cdn.example.test/original.mp4","poster_url":"https://cdn.example.test/original.jpg"},
                  {"variant_id":"song_text","render_status":"ready","render_generation_id":"generation-2","output_url":"https://cdn.example.test/song.mp4","poster_url":"https://cdn.example.test/song.jpg"}
                ]
              },
              "events":[
                {"id":"event-1","sequence":1,"revision":9,"role":"assistant","event_type":"render_complete","content":"Your video is ready.","payload":null,"created_at":"2026-09-07T12:00:00Z"}
              ],
              "updated_at":"2026-09-07T12:00:00Z"
            }
            """#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let thread = try await api.project(threadID: PreviewFixtures.projectID)

        XCTAssertEqual(thread.events.count, 1)
        XCTAssertEqual(thread.events.first?.eventType, "render_complete")
        XCTAssertEqual(thread.summary.outputVariantID, "song_text")
        XCTAssertEqual(thread.summary.outputURL, URL(string: "https://cdn.example.test/song.mp4"))
        XCTAssertEqual(thread.summary.posterURL, URL(string: "https://cdn.example.test/song.jpg"))
    }

    func testCreationCapabilitiesUseServerFormatAvailability() async throws {
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/creation-threads/capabilities")
            return (200, Data(#"{"formats":[{"id":"montage","edit_format":"montage","max_clips":20},{"id":"narrated","edit_format":"narrated_planned","max_clips":20}]}"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())

        let capabilities = try await api.creationCapabilities()

        XCTAssertEqual(capabilities.formats.map(\.id), ["montage", "narrated"])
    }

    func testCreationThreadPreservesURLFreeListVariants() throws {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let thread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B",
          "title":"Rendering",
          "status":"active",
          "revision":3,
          "runtime_version":2,
          "active_job_id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE",
          "active_plan_item_id":"F53BB746-A61D-4F25-B49B-FC2B1D348EB1",
          "state":{"selected_variant_id":"original_text"},
          "job":{"id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE","status":"variants_ready","variants":[{"variant_id":"original_text","render_status":"ready"}]},
          "updated_at":"2026-09-07T12:00:00Z"
        }
        """#.utf8))

        XCTAssertEqual(thread.job?.variants.count, 1)
        XCTAssertNil(thread.summary.outputURL)
        XCTAssertNil(thread.summary.posterURL)
        XCTAssertEqual(thread.summary.outputVariantID, "original_text")
        XCTAssertEqual(thread.summary.activePlanItemID, "F53BB746-A61D-4F25-B49B-FC2B1D348EB1")
        XCTAssertTrue(thread.events.isEmpty)
    }

    func testCreationThreadFallsBackFromInFlightSelectionToPlayableVariant() throws {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let thread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B",
          "title":"Rendering revision",
          "status":"active",
          "revision":10,
          "runtime_version":2,
          "active_job_id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE",
          "state":{"selected_variant_id":"song_text"},
          "job":{
            "id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE",
            "status":"variants_ready",
            "variants":[
              {"variant_id":"song_text","render_status":"rendering","output_url":"https://cdn.example.test/stale-song.mp4"},
              {"variant_id":"original_text","render_status":"ready","output_url":"https://cdn.example.test/original.mp4","poster_url":"https://cdn.example.test/original.jpg"}
            ]
          },
          "updated_at":"2026-09-07T12:00:00Z"
        }
        """#.utf8))

        XCTAssertEqual(thread.summary.outputVariantID, "original_text")
        XCTAssertEqual(thread.summary.outputURL, URL(string: "https://cdn.example.test/original.mp4"))
        XCTAssertEqual(thread.summary.posterURL, URL(string: "https://cdn.example.test/original.jpg"))
    }

    func testProjectUploadReservationAndAttachmentUseThreadContract() async throws {
        URLProtocolStub.handler = { request in
            if request.url?.path.hasSuffix("/upload-urls") == true {
                let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: Self.bodyData(request)) as? [String: Any])
                let files = try XCTUnwrap(body["files"] as? [[String: Any]])
                XCTAssertEqual(files.first?["client_upload_id"] as? String, "ios-upload-1")
                return (200, Data(#"[{"media_id":"ios-upload-1.mp4","upload_url":"https://storage.example.test/upload","gcs_path":"users/u/creation-threads/t/ios-upload-1.mp4","content_type":"video/mp4","upload_headers":{"x-goog-if-generation-match":"0"}}]"#.utf8))
            }
            XCTAssertTrue(request.url?.path.hasSuffix("/media") == true)
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: Self.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["expected_revision"] as? Int, 7)
            let media = try XCTUnwrap(body["media"] as? [[String: Any]])
            XCTAssertEqual(media.first?["media_id"] as? String, "ios-upload-1.mp4")
            return (200, Self.threadResponse(jobStatus: nil))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let reservation = try await api.reserveProjectUpload(threadID: PreviewFixtures.projectID, clientUploadID: "ios-upload-1", filename: "clip.mp4", contentType: "video/mp4", size: 123)
        XCTAssertEqual(reservation.mediaID, "ios-upload-1.mp4")
        _ = try await api.attachProjectMedia(threadID: PreviewFixtures.projectID, mediaID: reservation.mediaID, gcsPath: reservation.gcsPath, filename: "clip.mp4", contentType: "video/mp4", expectedRevision: 7, clientEventID: "attach-1")
    }

    func testUnauthorizedRequestRefreshesOnceAndStoresRotation() async throws {
        let old = MobileSession(accessToken: "expired", refreshToken: "refresh-1", expiresIn: 1)
        let store = MemoryTokenStore(old)
        let lock = NSLock()
        var projectCalls = 0
        URLProtocolStub.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" {
                return (200, Data(#"{"access_token":"fresh","refresh_token":"refresh-2","token_type":"Bearer","expires_in":900,"user":{"id":"1","email":"creator@example.com","onboarding_status":"complete","linked_providers":[]}}"#.utf8))
            }
            lock.lock(); projectCalls += 1; let call = projectCalls; lock.unlock()
            if call == 1 { return (401, Data()) }
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fresh")
            return (200, Data("[]".utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: store, session: stubSession())
        _ = try await api.projects()
        XCTAssertEqual(try store.read()?.refreshToken, "refresh-2")
        XCTAssertEqual(projectCalls, 2)
    }

    func testConcurrentUnauthorizedRequestsShareOneRefreshRotation() async throws {
        let old = MobileSession(accessToken: "expired", refreshToken: "refresh-1", expiresIn: 1)
        let store = MemoryTokenStore(old)
        let lock = NSLock()
        var refreshCalls = 0
        URLProtocolStub.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" {
                lock.lock(); refreshCalls += 1; lock.unlock()
                Thread.sleep(forTimeInterval: 0.05)
                return (200, Data(#"{"access_token":"fresh","refresh_token":"refresh-2","token_type":"Bearer","expires_in":900,"user":{"id":"1","email":"creator@example.com","onboarding_status":"complete","linked_providers":[]}}"#.utf8))
            }
            if request.value(forHTTPHeaderField: "Authorization") == "Bearer expired" {
                return (401, Data())
            }
            return (200, Data("[]".utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: store, session: stubSession())
        async let first = api.projects()
        async let second = api.projects()
        _ = try await (first, second)
        XCTAssertEqual(refreshCalls, 1)
        XCTAssertEqual(try store.read()?.refreshToken, "refresh-2")
    }

    func testTerminalUnauthorizedClearsSession() async throws {
        let store = MemoryTokenStore(MobileSession(accessToken: "expired", refreshToken: "refresh", expiresIn: 900))
        URLProtocolStub.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" { return (401, Data()) }
            return (401, Data())
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: store, session: stubSession())
        do { _ = try await api.projects(); XCTFail("Expected session expiry") }
        catch let error as APIError {
            if case .sessionExpired = error {} else { XCTFail("Expected session expiry, got \(error)") }
        }
        XCTAssertNil(try store.read())
    }

    func testRevokeMobileSessionUsesRefreshTokenEndpoint() async throws {
        let store = MemoryTokenStore(MobileSession(accessToken: "access", refreshToken: "refresh", expiresIn: 900))
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/auth/mobile/revoke")
            let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: Self.bodyData(request)) as? [String: Any])
            XCTAssertEqual(object["refresh_token"] as? String, "refresh")
            return (200, Data(#"{"revoked":true}"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: store, session: stubSession())
        try await api.revokeMobileSession("refresh")
    }

    func testServerDatesAcceptFractionalAndOffsetISO8601() async throws {
        URLProtocolStub.handler = { _ in
            (200, Data(#"""
            [{
                "id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B",
                "title":"Timestamped",
                "status":"active",
                "revision":1,
                "runtime_version":2,
                "updated_at":"2026-09-07T12:00:00.123456+00:00"
            }]
            """#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let projects = try await api.projects()
        XCTAssertEqual(projects.first?.title, "Timestamped")
        XCTAssertEqual(try XCTUnwrap(projects.first?.updatedAt.timeIntervalSince1970), 1788782400.123, accuracy: 0.001)
    }

    func testPlaybackURLUsesFreshSignedURLFromOwnedJob() async throws {
        let store = MemoryTokenStore(MobileSession(accessToken: "access", refreshToken: "refresh", expiresIn: 900))
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/me/jobs/\(PreviewFixtures.projectID.uuidString)/playback-url")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer access")
            return (200, Data(#"{"video_url":"https://cdn.example.test/signed/generation-7.mp4?Expires=123"}"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: store, session: stubSession())
        let url = try await api.playbackURL(jobID: PreviewFixtures.projectID)
        XCTAssertEqual(url.host, "cdn.example.test")
        XCTAssertTrue(url.query?.contains("Expires=123") == true)
    }

    func testEditorVariantLoadsAuthoritativeStatusProjection() async throws {
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/generative-jobs/\(PreviewFixtures.projectID.uuidString)/status")
            return (200, Data(#"{"job_id":"job","status":"variants_ready","variants":[{"variant_id":"other"},{"variant_id":"initial","render_generation_id":"generation-live","output_url":"https://cdn.example.test/live.mp4"}]}"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let variant = try await api.editorVariant(jobID: PreviewFixtures.projectID, variantID: "initial")
        XCTAssertEqual(variant["render_generation_id"], .string("generation-live"))
    }

    func testOpenLibraryJobUsesIdempotentEditorPromotionRoute() async throws {
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/me/jobs/\(PreviewFixtures.projectID.uuidString)/open-in-editor")
            return (200, Data(#"{"plan_item_id":"item-7","variant_id":"initial"}"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let receipt = try await api.openJobInEditor(jobID: PreviewFixtures.projectID)
        XCTAssertEqual(receipt, OpenInEditorResponse(planItemID: "item-7", variantID: "initial"))
    }

    func testEditorCommitUsesAtomicRendererContract() async throws {
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/plan-items/item-1/variants/initial/editor-commit")
            let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: Self.bodyData(request)) as? [String: Any])
            XCTAssertEqual(object["base_generation"] as? String, "generation-1")
            XCTAssertEqual((object["caption_meta"] as? [String: Any])?["style"] as? String, "sentence")
            return (200, Data(#"{"ok":true,"generation":"generation-2","sections":{"text_elements":false,"caption_meta":true,"timeline":false,"mix":false},"expected_duration_s":12.3}"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://api.example.test")!, tokenStore: MemoryTokenStore(), session: stubSession())
        let response = try await api.editorCommit(
            itemID: "item-1",
            variantID: "initial",
            request: EditorCommitRequest(captionMeta: ["enabled": .bool(true), "style": .string("sentence")], baseGeneration: "generation-1")
        )
        XCTAssertEqual(response.generation, "generation-2")
        XCTAssertEqual(response.expectedDuration, 12.3)
    }

    @MainActor func testProjectCacheCompareAndSetRejectsStaleRevision() throws {
        let container = try ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let repository = CacheRepository(context: ModelContext(container))
        let initial = ProjectSummary(id: PreviewFixtures.projectID, title: "First", status: .draft, updatedAt: .now, posterURL: nil, serverRevision: 0)
        try repository.upsert([initial])
        var updated = initial; updated.title = "Second"; updated.serverRevision = 1
        try repository.update(updated, expectedServerRevision: 0)
        var stale = updated; stale.title = "Stale"; stale.serverRevision = 2
        XCTAssertThrowsError(try repository.update(stale, expectedServerRevision: 0)) { error in
            XCTAssertEqual(error as? CacheConflictError, .staleProject(expected: 0, actual: 1))
        }
    }

    @MainActor func testProjectCachePreservesEditorRoutingIdentity() throws {
        let container = try ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let repository = CacheRepository(context: ModelContext(container))
        let project = ProjectSummary(
            id: PreviewFixtures.projectID,
            title: "Cached editor",
            status: .ready,
            updatedAt: .now,
            posterURL: nil,
            outputVariantID: "variant-2",
            activePlanItemID: "item-7"
        )

        try repository.upsert([project])

        let cached = try XCTUnwrap(repository.projects().first?.summary)
        XCTAssertEqual(cached.outputVariantID, "variant-2")
        XCTAssertEqual(cached.activePlanItemID, "item-7")
    }

    func testUploadRecoveryPolicyDistinguishesRetryAndReselect() {
        let policy = UploadRecoveryPolicy(maximumAutomaticRetries: 1)
        XCTAssertEqual(policy.action(retryCount: 0, statusCode: 503, fileExists: true), .retry)
        XCTAssertEqual(policy.action(retryCount: 1, statusCode: 503, fileExists: true), .keepForManualRetry)
        XCTAssertEqual(policy.action(retryCount: 0, statusCode: nil, fileExists: false), .chooseFileAgain)
        XCTAssertTrue(policy.uploadReachedStorage(statusCode: 412, hasTransportError: false))
        XCTAssertFalse(policy.uploadReachedStorage(statusCode: 412, hasTransportError: true))
    }

    private func stubSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [URLProtocolStub.self]
        return URLSession(configuration: configuration)
    }

    private static func draftResponse(revision: Int, etag: String) -> Data {
        Data("""
        {"draft_id":"draft","item_id":"item","variant_key":"initial","draft_revision":\(revision),"snapshot_hash":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","etag":\(String(reflecting: etag)),"base_job_id":null,"base_generation_id":null,"snapshot":{"schema_version":2},"can_undo":true,"created_at":"2026-09-07T12:00:00Z"}
        """.utf8)
    }

    private static func threadResponse(jobStatus: String?, state: [String: String] = [:]) -> Data {
        let activeJob = jobStatus == nil ? "null" : #""74A559F6-28D9-4E0D-9EB6-71CD09A958DE""#
        let job = jobStatus.map { #"{"id":"74A559F6-28D9-4E0D-9EB6-71CD09A958DE","status":"\#($0)"}"# } ?? "null"
        let stateData = try! JSONSerialization.data(withJSONObject: state, options: [.sortedKeys])
        let stateJSON = String(decoding: stateData, as: UTF8.self)
        return Data(#"{"id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B","title":"Project","status":"active","revision":8,"runtime_version":2,"active_job_id":\#(activeJob),"job":\#(job),"state":\#(stateJSON),"updated_at":"2026-09-07T12:00:00Z"}"#.utf8)
    }

    private static func bodyData(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var result = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            result.append(buffer, count: count)
        }
        return result
    }
}

private final class MemoryTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var session: MobileSession?
    init(_ session: MobileSession? = nil) { self.session = session }
    func read() throws -> MobileSession? { lock.withLock { session } }
    func write(_ session: MobileSession) throws { lock.withLock { self.session = session } }
    func delete() throws { lock.withLock { session = nil } }
}

private struct FailingTokenStore: TokenStore {
    struct Failure: Error {}
    func read() throws -> MobileSession? { nil }
    func write(_ session: MobileSession) throws { throw Failure() }
    func delete() throws {}
}

private final class DeleteFailingTokenStore: TokenStore, @unchecked Sendable {
    struct Failure: Error {}
    private let session: MobileSession
    init(_ session: MobileSession) { self.session = session }
    func read() throws -> MobileSession? { session }
    func write(_ session: MobileSession) throws {}
    func delete() throws { throw Failure() }
}

private final class URLProtocolStub: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (Int, Data))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, data) = try Self.handler?(request) ?? (500, Data())
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}
