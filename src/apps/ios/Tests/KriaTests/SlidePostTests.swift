import XCTest
@testable import Kria

@MainActor final class SlidePostTests: XCTestCase {
    private let itemID = "11111111-1111-1111-1111-111111111111"
    private var defaults: UserDefaults!
    private var suite: String!
    override func setUp() {
        super.setUp(); suite = "SlidePostTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suite); defaults = nil
        super.tearDown()
    }
    private func fixture(version: Int = 1, rendered: Bool = true) -> SlidePostState {
        let refs = (1...2).map { SlidePostSlide(id: "slide-\($0)", assetID: "asset-\($0)", kind: $0 == 2 ? "video" : "image") }
        let draft = SlidePostDraft(version: version, platformProfile: "instagram_carousel", slides: refs, caption: "A day outside", renderedVersion: rendered ? version : nil)
        return SlidePostState(itemID: itemID, title: "A day outside", jobID: UUID(uuidString: "22222222-2222-2222-2222-222222222222"), draft: draft,
            assets: refs.map { .init(id: $0.assetID, kind: $0.kind, status: "ready", sourceURL: URL(string: "https://storage.test/source"), durationS: $0.kind == "video" ? 8 : nil, mediaStatus: "available") },
            renderStatus: rendered ? "ready" : "rendering", renderedVersion: rendered ? version : nil,
            slides: refs.map { .init(id: $0.id, assetID: $0.assetID, kind: $0.kind, url: rendered ? URL(string: "https://storage.test/\($0.id)") : nil) }, bundleURL: rendered ? URL(string: "https://storage.test/bundle.zip") : nil)
    }
    func testExportRequiresExactCurrentCompleteOrderedAssets() {
        let good = fixture(); XCTAssertTrue(good.canExport)
        var changed = good; changed.renderedVersion = 0; XCTAssertFalse(changed.canExport)
        changed = good; changed.slides.removeLast(); XCTAssertFalse(changed.canExport)
        changed = good; changed.slides.reverse(); XCTAssertFalse(changed.canExport)
        changed = good; changed.slides[0].url = nil; XCTAssertFalse(changed.canExport)
        changed = good; changed.slides[0].url = URL(string: "http://storage.test/slide"); XCTAssertFalse(changed.canExport)
        changed = good; changed.validationErrors = [.init(code: "missing", message: "Slide unavailable")]; XCTAssertFalse(changed.canExport)
        changed = good; changed.renderStatus = "rendering"; XCTAssertFalse(changed.canExport)
    }
    func testValidationRespectsMediaProfileAndTextLimits() throws {
        var draft = try XCTUnwrap(fixture().draft)
        XCTAssertNil(draft.validationMessage)
        draft.platformProfile = "tiktok_photo"; XCTAssertNotNil(draft.validationMessage)
        draft.platformProfile = "instagram_carousel"
        draft.slides[0].edits = .init(text: .init(content: String(repeating: "a", count: 121), position: "top"))
        XCTAssertNotNil(draft.validationMessage)
        draft.slides[0].edits = .init(text: .init(content: "Welcome", position: "top"), lookPreset: "golden_hour")
        XCTAssertNil(draft.validationMessage)
        draft.coverIndex = 2; XCTAssertNotNil(draft.validationMessage)
    }
    func testAuthenticatedProposalIsReadOnlyUntilExplicitAcceptance() async throws {
        let saved = fixture()
        let response = SlidePostProposal(draft: try XCTUnwrap(saved.draft), baseVersion: 1, fallbackUsed: false, summary: "Review the order")
        var paths: [String] = []
        NativeEditorURLProtocol.handler = { request in
            paths.append(request.url!.path)
            if request.httpMethod == "POST" {
                XCTAssertEqual(request.url?.path, "/plan-items/\(self.itemID)/slide-post/propose")
                let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
                XCTAssertEqual(body["expected_version"] as? Int, 1)
                XCTAssertEqual(body["instruction"] as? String, "Lead with the landscape")
                XCTAssertNil(body["gcs_path"])
                return (200, try JSONEncoder().encode(response))
            }
            return (200, try JSONEncoder().encode(saved))
        }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        await session.propose(api: api, itemID: itemID, instruction: "Lead with the landscape")
        XCTAssertEqual(session.draft, saved.draft)
        XCTAssertNotNil(session.proposal)
        XCTAssertEqual(paths.count, 2)
        XCTAssertFalse(session.hasUnsavedChanges)
    }
    func testSaveSendsExpectedVersionAndNeverServerOwnedMetadata() async throws {
        let saved = fixture(version: 3)
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["expected_version"] as? Int, 2)
            XCTAssertNil(body["version"]); XCTAssertNil(body["rendered_version"])
            XCTAssertNil(body["user_edited"]); XCTAssertNil(body["job_id"])
            return (200, try JSONSerialization.data(withJSONObject: ["slide_post": JSONSerialization.jsonObject(with: JSONEncoder().encode(saved.draft!))]))
        }
        let draft = try await NativeEditorTestSupport.api().saveSlidePost(itemID: itemID, request: .init(draft: saved.draft!, expectedVersion: 2))
        XCTAssertEqual(draft.version, 3)
    }
    func testPollingKeepsLocalEditsAndSilentlyRebasesOntoNewerServerVersion() async throws {
        var remote = fixture()
        var savedExpectedVersion: Int?
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "PUT" {
                let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
                savedExpectedVersion = body["expected_version"] as? Int
                var saved = try XCTUnwrap(remote.draft); saved.version = 3; saved.caption = body["caption"] as? String ?? ""
                return (200, try JSONSerialization.data(withJSONObject: ["slide_post": JSONSerialization.jsonObject(with: JSONEncoder().encode(saved))]))
            }
            return (200, try JSONEncoder().encode(remote))
        }
        let session = SlidePostSession(defaults: defaults)
        let api = NativeEditorTestSupport.api()
        await session.refresh(api: api, itemID: itemID)
        session.setCaption("My unsaved caption")
        await session.refresh(api: api, itemID: itemID)
        XCTAssertEqual(session.draft?.caption, "My unsaved caption")
        XCTAssertFalse(session.canExport)
        remote = fixture(version: 2)
        await session.refresh(api: api, itemID: itemID)
        XCTAssertFalse(session.hasConflict, "the local draft always wins; there is no conflict state")
        XCTAssertNil(session.error)
        XCTAssertEqual(session.draft?.caption, "My unsaved caption")
        XCTAssertTrue(session.hasUnsavedChanges)
        await session.save(api: api, itemID: itemID)
        XCTAssertEqual(savedExpectedVersion, 2, "save is based on the newest server version")
        XCTAssertEqual(session.draft?.caption, "My unsaved caption")
    }
    func testReorderKeepsCoverIdentityAndNavigationKeepsInstruction() async throws {
        let remote = fixture()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        session.selectedID = "slide-2"
        session.instruction = "Keep the final sunset"
        session.moveSlide(id: "slide-1", offset: 1)
        XCTAssertEqual(session.draft?.coverIndex, 1)
        let reopened = SlidePostSession(defaults: defaults)
        await reopened.refresh(api: api, itemID: itemID)
        XCTAssertEqual(reopened.instruction, "Keep the final sunset")
        XCTAssertEqual(reopened.selectedID, "slide-2")
        XCTAssertEqual(reopened.draft?.slides.first?.id, "slide-2")
    }
    func testCleanRestoredDraftAdoptsNewServerVersionWithoutConflict() async throws {
        var remote = fixture()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let api = NativeEditorTestSupport.api()
        let first = SlidePostSession(defaults: defaults)
        await first.refresh(api: api, itemID: itemID)
        first.instruction = "Keep this prompt"
        remote = fixture(version: 2)
        let reopened = SlidePostSession(defaults: defaults)
        await reopened.refresh(api: api, itemID: itemID)
        XCTAssertEqual(reopened.draft?.version, 2)
        XCTAssertEqual(reopened.instruction, "Keep this prompt")
    }
    func testStaleSaveRebasesSilentlyAndRePutsTheLocalDraft() async throws {
        let remote = fixture(version: 2)
        var puts: [Int] = []
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "PUT" {
                let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
                puts.append(try XCTUnwrap(body["expected_version"] as? Int))
                // The first write is based on a version the server has already moved past.
                if puts.count == 1 { return (409, Data(#"{"detail":{"code":"slide_post_version_conflict","current_version":2}}"#.utf8)) }
                var saved = try XCTUnwrap(remote.draft); saved.version = 3; saved.caption = body["caption"] as? String ?? ""
                return (200, try JSONSerialization.data(withJSONObject: ["slide_post": JSONSerialization.jsonObject(with: JSONEncoder().encode(saved))]))
            }
            return (200, try JSONEncoder().encode(remote))
        }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        session.setCaption("Keep this edit")
        await session.save(api: api, itemID: itemID)
        XCTAssertEqual(puts.count, 2, "one 409, then the same draft is sent again")
        XCTAssertEqual(puts.last, 2, "the retry is based on the server's current version")
        XCTAssertFalse(session.hasConflict)
        XCTAssertNil(session.error)
        XCTAssertEqual(session.draft?.caption, "Keep this edit")
        XCTAssertEqual(session.draft?.version, 3)
        XCTAssertFalse(session.hasUnsavedChanges)
    }
    func testSaveThatKeepsFailingGivesUpButKeepsTheLocalDraft() async throws {
        let remote = fixture()
        NativeEditorURLProtocol.handler = { request in
            request.httpMethod == "PUT" ? (409, Data(#"{"detail":{"code":"slide_post_version_conflict","current_version":2}}"#.utf8)) : (200, try JSONEncoder().encode(remote))
        }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        session.setCaption("Keep this edit")
        await session.save(api: api, itemID: itemID)
        XCTAssertFalse(session.hasConflict)
        XCTAssertNotNil(session.error)
        XCTAssertEqual(session.draft?.caption, "Keep this edit")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertFalse(session.canExport)
    }
    func testOlderRefreshCannotRollBackAcceptedVersionOrExports() async throws {
        var remote = fixture(version: 3, rendered: false)
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        remote = fixture(version: 2)
        await session.refresh(api: api, itemID: itemID)
        XCTAssertEqual(session.draft?.version, 3)
        XCTAssertFalse(session.canExport)
        XCTAssertTrue(session.isRendering)
    }
    func testExportRevalidationAdoptsRemoteVersionAndRejectsUnavailableOutput() async throws {
        var remote = fixture()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        remote = fixture(version: 2, rendered: false)
        do {
            try await session.revalidateForExport(api: api, itemID: itemID)
            XCTFail("A newer unrendered draft must block export")
        } catch { XCTAssertEqual(error as? APIError, .conflict) }
        XCTAssertEqual(session.draft?.version, 2)
        XCTAssertFalse(session.canExport)
        NativeEditorURLProtocol.handler = { _ in throw URLError(.notConnectedToInternet) }
        do {
            try await session.revalidateForExport(api: api, itemID: itemID)
            XCTFail("A failed authoritative read must block export")
        } catch { }
    }
    // MARK: Rich text (KRI-298 Lane D)

    private func richSession() async throws -> SlidePostSession {
        let remote = fixture()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        return session
    }
    func testTextCRUDEnforcesFourPerSlideAndMirrorsLegacyText() async throws {
        let session = try await richSession()
        let id = try XCTUnwrap(session.addText(slideID: "slide-1", text: "Athens"))
        XCTAssertEqual(session.selectedTextID, id)
        session.updateText(slideID: "slide-1", textID: id) { $0.position = "custom"; $0.yFrac = 0.9; $0.color = "#FFF0A6" }
        var edits = try XCTUnwrap(session.draft?.slides[0].edits)
        XCTAssertEqual(edits.texts?.first?.color, "#FFF0A6")
        XCTAssertEqual(edits.text, SlidePostText(content: "Athens", position: "bottom"), "custom y 0.9 mirrors to the bottom bucket")
        for index in 2...4 { XCTAssertNotNil(session.addText(slideID: "slide-1", text: "Line \(index)")) }
        XCTAssertNil(session.addText(slideID: "slide-1", text: "Fifth"), "a slide holds at most four texts")
        XCTAssertNil(session.draft?.validationMessage)
        session.removeText(slideID: "slide-1", textID: id)
        edits = try XCTUnwrap(session.draft?.slides[0].edits)
        XCTAssertEqual(edits.texts?.count, 3)
        XCTAssertEqual(edits.text?.content, "Line 2", "the mirror follows the new first text")
        session.updateText(slideID: "slide-1", textID: try XCTUnwrap(edits.texts?.first?.id)) { $0.text = String(repeating: "x", count: 121) }
        XCTAssertNotNil(session.draft?.validationMessage, "an over-long text blocks saving")
    }
    func testEmptyTextBlocksSaveAndIsDroppedWhenEditingEnds() async throws {
        let session = try await richSession()
        let id = try XCTUnwrap(session.addText(slideID: "slide-1", text: "x"))
        session.updateText(slideID: "slide-1", textID: id) { $0.text = "" }
        XCTAssertNotNil(session.draft?.validationMessage)
        session.removeEmptyTexts(slideID: "slide-1")
        XCTAssertNil(session.draft?.validationMessage)
        XCTAssertEqual(session.draft?.slides[0].edits?.texts, [])
    }
    func testUndoRedoAcrossTextEditsAndCoalescedTyping() async throws {
        let session = try await richSession()
        XCTAssertFalse(session.canUndoEdit)
        let id = try XCTUnwrap(session.addText(slideID: "slide-1", text: "A"))
        for text in ["Ab", "Abc", "Abcd"] { session.updateText(slideID: "slide-1", textID: id, coalescing: "edit") { $0.text = text } }
        XCTAssertEqual(session.draft?.slides[0].edits?.texts?.first?.text, "Abcd")
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.texts?.first?.text, "A", "typing is one undo step")
        session.undoEdit()
        XCTAssertNil(session.draft?.slides[0].edits, "back to the slide before any text")
        XCTAssertFalse(session.canUndoEdit)
        XCTAssertTrue(session.canRedoEdit)
        session.redoEdit(); session.redoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.texts?.first?.text, "Abcd")
        session.setCover(id: "slide-2")
        XCTAssertFalse(session.canRedoEdit, "a new edit clears redo")
        XCTAssertEqual(session.draft?.version, 1, "history never rewinds server stamps")
    }
    func testLegacyTextDecodesAndEncodesBothShapes() throws {
        let legacy = Data(#"{"text":{"content":"Hello","position":"top"},"look_preset":"golden_hour"}"#.utf8)
        var edits = try JSONDecoder().decode(SlidePostEdits.self, from: legacy)
        XCTAssertNil(edits.texts)
        XCTAssertEqual(edits.effectiveTexts.first?.text, "Hello")
        XCTAssertEqual(edits.effectiveTexts.first?.position, "top")
        XCTAssertEqual(edits.effectiveTexts.first?.background, "box")
        // Unchanged legacy edits are written back untouched.
        let untouched = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(edits)) as? [String: Any])
        XCTAssertNil(untouched["texts"])
        edits.setTexts([SlidePostTextElement(id: "t1", text: "Lisbon")])
        let written = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(edits)) as? [String: Any])
        XCTAssertEqual((written["texts"] as? [[String: Any]])?.first?["text"] as? String, "Lisbon")
        XCTAssertEqual((written["text"] as? [String: Any])?["content"] as? String, "Lisbon")
        XCTAssertEqual(written["look_preset"] as? String, "golden_hour")
    }
    func testUnknownTextStyleKeysSurviveARoundTripAndDefaultsFill() throws {
        let payload = Data(#"{"id":"t1","text":"Hi","font_family":"Playfair Display","glow":{"radius":4},"letter_spacing":0.2}"#.utf8)
        var element = try JSONDecoder().decode(SlidePostTextElement.self, from: payload)
        XCTAssertEqual(element.fontFamily, "Playfair Display")
        XCTAssertEqual(element.sizePx, 86); XCTAssertEqual(element.position, "bottom"); XCTAssertEqual(element.color, "#FFFFFF")
        XCTAssertEqual(element.extra["letter_spacing"], .number(0.2))
        element.text = "Hello"
        let out = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(element)) as? [String: Any])
        XCTAssertEqual(out["letter_spacing"] as? Double, 0.2)
        XCTAssertEqual((out["glow"] as? [String: Any])?["radius"] as? Double, 4)
        XCTAssertEqual(out["text"] as? String, "Hello")
        XCTAssertNil(out["x_frac"], "unset optionals are omitted, not sent as null")
    }
    func testApplyStyleToAllCopiesEverythingButTheWords() async throws {
        let session = try await richSession()
        let source = try XCTUnwrap(session.addText(slideID: "slide-1", text: "Athens"))
        _ = session.addText(slideID: "slide-2", text: "Naxos")
        session.updateText(slideID: "slide-1", textID: source) {
            $0.fontFamily = "Playfair Display"; $0.color = "#9BCAFF"; $0.sizePx = 120; $0.alignment = "left"
            $0.position = "custom"; $0.xFrac = 0.25; $0.yFrac = 0.4; $0.strokeWidth = 6; $0.shadowEnabled = false; $0.background = "box"
        }
        let before = try XCTUnwrap(session.draft?.slides[1].edits?.texts?.first)
        session.applyStyleToAllSlides(slideID: "slide-1", textID: source)
        let target = try XCTUnwrap(session.draft?.slides[1].edits?.texts?.first)
        XCTAssertEqual(target.text, "Naxos", "words are never copied")
        XCTAssertEqual(target.id, before.id)
        XCTAssertEqual([target.fontFamily, target.color, target.alignment, target.position, target.background], ["Playfair Display", "#9BCAFF", "left", "custom", "box"])
        XCTAssertEqual(target.sizePx, 120); XCTAssertEqual(target.strokeWidth, 6); XCTAssertFalse(target.shadowEnabled)
        XCTAssertEqual(target.xFrac, 0.25); XCTAssertEqual(target.yFrac, 0.4)
        XCTAssertEqual(session.draft?.slides[0].edits?.texts?.first?.text, "Athens")
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides[1].edits?.texts?.first, before, "applying to all is one undo step")
    }
    func testEditedLabelsAreMarkedSoARelabelNeverOverwritesThem() async throws {
        let session = try await richSession()
        let id = try XCTUnwrap(session.addText(slideID: "slide-1", text: "Athens"))
        session.updateText(slideID: "slide-1", textID: id) { $0.role = "label"; $0.labelSource = "place" }
        XCTAssertEqual(session.draft?.slides[0].edits?.texts?.first?.edited, true)
    }
    func testReorderToIndexKeepsCoverIdentityAndAnUndoRestoresIt() async throws {
        let session = try await richSession()
        session.moveSlide(id: "slide-1", toIndex: 1)
        XCTAssertEqual(session.draft?.slides.map(\.id), ["slide-2", "slide-1"])
        XCTAssertEqual(session.draft?.coverIndex, 1, "the cover stays on slide-1")
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides.map(\.id), ["slide-1", "slide-2"])
        XCTAssertEqual(session.draft?.coverIndex, 0)
    }
    func testStagedDraftIsUndoableAndMarksTheEditorDirty() async throws {
        let session = try await richSession()
        var staged = try XCTUnwrap(session.draft)
        staged.slides.reverse(); staged.caption = "From chat"
        session.stageDraft(staged)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndoEdit)
        session.undoEdit()
        XCTAssertFalse(session.hasUnsavedChanges)
    }
    func testRichTextCapabilityDecodesAndDefaultsOff() throws {
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_rich_text":true}"#.utf8))
        XCTAssertTrue(on.slidePostRichTextEnabled)
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertFalse(off.slidePostRichTextEnabled)
    }

    func testGalleryKeepsSlideItemIdentity() async throws {
        NativeEditorURLProtocol.handler = { _ in
            (200, Data(#"{"jobs":[{"id":"22222222-2222-2222-2222-222222222222","title":"Weekend","status":"ready","output_variant_id":"slides","content_plan_item_id":"11111111-1111-1111-1111-111111111111","created_at":"2026-09-22T12:00:00Z"}],"next_cursor":null}"#.utf8))
        }
        let rows = try await NativeEditorTestSupport.api().library()
        XCTAssertTrue(try XCTUnwrap(rows.first).isSlidePost)
        XCTAssertEqual(rows.first?.activePlanItemID, itemID)
    }
}
