import XCTest
@testable import Kria

@MainActor
final class NativeEditorSessionTests: XCTestCase {
    func testProductionSessionStartsFailClosedUntilCapabilitiesLoad() {
        let project = ProjectSummary(id: UUID(), title: "Ready", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        XCTAssertFalse(session.canEditTimeline)
        XCTAssertFalse(session.canEditText)
        XCTAssertFalse(session.canEditCaptions)
        XCTAssertFalse(session.canEditMix)
    }

    func testTrimClampsToMinimumAndUndoRedo() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 8)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 2))
        session.selectClip(first.id)
        session.trimSelected(edge: .trailing, to: 0)
        XCTAssertEqual(session.draft.clips[0].end - session.draft.clips[0].start, 0.1, accuracy: 0.0001)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo(); XCTAssertEqual(session.draft.clips[0].end, 4)
        session.redo(); XCTAssertEqual(session.draft.clips[0].end, 0.1, accuracy: 0.0001)
    }

    func testLeadingTrimAdvancesSourceWithoutLeavingTimelineGap() {
        let first = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 4, start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let second = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 7, start: 4, end: 6, trimIn: 0, trimOut: 2, sourceDuration: 3)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.selectClip(first.id)
        session.trimSelected(edge: .leading, to: 1.5)
        XCTAssertEqual(session.draft.clips[0].start, 0, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 2.5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimIn, 2.5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].start, 2.5, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 4.5, accuracy: 0.0001)
    }

    func testLeadingTrimUsesOneGestureBaselineAndOneUndoStep() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let second = EditorClip(id: UUID(), assetID: UUID(), start: 4, end: 6, trimIn: 0, trimOut: 2, sourceDuration: 3)
        let original = EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: original)

        session.beginTrim(clipID: first.id, edge: .leading)
        for translation in [0.25, 0.5, 0.75, 1.0] { session.updateTrim(by: translation) }
        session.endTrim()

        XCTAssertEqual(session.draft.clips[0].trimIn, 2, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimOut, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 3, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].start, 3, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 5, accuracy: 0.0001)
        session.undo()
        XCTAssertEqual(session.draft, original)
        XCTAssertFalse(session.canUndo)
    }

    func testLeadingTrimCanExtendAndReverseWithinOneGesture() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))

        session.beginTrim(clipID: first.id, edge: .leading)
        session.updateTrim(by: 1)
        session.updateTrim(by: -0.5)
        session.endTrim()

        XCTAssertEqual(session.draft.clips[0].trimIn, 0.5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimOut, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 4.5, accuracy: 0.0001)
    }

    func testTrailingTrimExtendsThroughRippleAndStopsAtSourceEnd() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 6)
        let second = EditorClip(id: UUID(), assetID: UUID(), start: 4, end: 6, trimIn: 0, trimOut: 2, sourceDuration: 3)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))

        session.beginTrim(clipID: first.id, edge: .trailing)
        session.updateTrim(by: 100)
        session.endTrim()

        XCTAssertEqual(session.draft.clips[0].trimIn, 1, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimOut, 6, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].start, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].end, 7, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 7, accuracy: 0.0001)
    }

    func testReturningTrimGestureToBaselineLeavesNoUndoEntry() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let original = EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: original)

        session.beginTrim(clipID: clip.id, edge: .trailing)
        session.updateTrim(by: -1)
        session.updateTrim(by: 0)
        session.endTrim()

        XCTAssertEqual(session.draft, original)
        XCTAssertFalse(session.canUndo)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSourceWindowSlideAndDelete() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 2, trimOut: 4, sourceDuration: 6)
        let second = EditorClip(id: UUID(), assetID: UUID(), start: 2, end: 4, trimIn: 0, trimOut: 2, sourceDuration: 4)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.selectClip(clip.id); session.slideSourceWindow(by: 100); XCTAssertEqual(session.draft.clips[0].trimIn, 4)
        session.deleteSelectedClip(); XCTAssertEqual(session.draft.clips.count, 1); XCTAssertEqual(session.draft.clips[0].start, 0)
    }

    func testSerializationKeepsUnknownServerKeysAndUsesProductionSections() {
        let projectID = UUID(); let clipID = UUID(); let assetID = UUID()
        let snapshot: [String: JSONValue] = ["schema_version": .number(2), "kind": .string("editor"), "future": .object(["keep": .bool(true)]), "editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string("slot-a"), "clip_index": .number(0), "in_s": .number(1), "duration_s": .number(2), "removed": .bool(false)])]), "future_section": .string("untouched")])])]
        let server = DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 3, snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now)
        let draft = server.editorDraft(projectID: projectID)
        XCTAssertEqual(draft.clips.count, 1)
        let persisted = draft.persistedSnapshot()
        let payload = Self.object(persisted["editor_payload"]); let sections = Self.object(payload?["sections"])
        XCTAssertEqual(Self.object(persisted["future"])?["keep"], .bool(true)); XCTAssertEqual(sections?["future_section"], .string("untouched")); XCTAssertNotNil(sections?["timeline_slots"])
        _ = clipID; _ = assetID
    }

    func testReorderingPreservesSourcePoolIndexes() {
        let first = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 7, start: 0, end: 2, trimIn: 0, trimOut: 2, slotID: "a")
        let second = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 2, start: 2, end: 4, trimIn: 0, trimOut: 2, slotID: "b")
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.selectClip(first.id)
        session.moveSelected(by: 1)
        let payload = Self.object(session.draft.persistedSnapshot()["editor_payload"])
        let sections = Self.object(payload?["sections"])
        let slots = Self.array(sections?["timeline_slots"])
        XCTAssertEqual(Self.object(slots.first)?["clip_index"], .number(2))
        XCTAssertEqual(Self.object(slots.last)?["clip_index"], .number(7))
    }

    func testAuthoritativeVariantRebasesStaleRuntimeDraft() {
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("old"), "sections": .object(["captions_enabled": .bool(false), "caption_style": .string("word"), "timeline_slots": .array([.object(["slot_id": .string("old-slot"), "clip_index": .number(9), "in_s": .number(0), "duration_s": .number(5)])])])])]
        let server = DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "old", snapshot: snapshot, canUndo: false, createdAt: .now)
        let authoritative: [String: JSONValue] = [
            "render_generation_id": .string("live"),
            "captions_enabled": .bool(true),
            "voiceover_caption_style": .string("sentence"),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("live-slot"), "clip_index": .number(3), "in_s": .number(1.25), "duration_s": .number(2.5), "source_duration_s": .number(7)])])]),
        ]
        let draft = server.editorDraft(projectID: UUID(), authoritativeVariant: authoritative)
        XCTAssertEqual(draft.clips.first?.slotID, "live-slot")
        XCTAssertEqual(draft.clips.first?.sourceClipIndex, 3)
        XCTAssertEqual(draft.clips.first?.sourceDuration, 7)
        XCTAssertTrue(draft.captions.enabled)
        XCTAssertEqual(draft.captions.style, "sentence")
        XCTAssertEqual(Self.object(draft.serverSnapshot["editor_payload"])?["base_generation"], .string("live"))
    }

    func testGalleryJobPromotesIntoEditorAndRebasesServerSnappedDuration() async {
        let jobID = UUID()
        let emptySnapshot = DraftSnapshot(
            draftID: "unused",
            itemID: "unused",
            variantKey: "initial",
            draftRevision: 0,
            snapshotHash: "",
            etag: "",
            baseJobID: nil,
            baseGenerationID: nil,
            snapshot: [:],
            canUndo: false,
            createdAt: .now
        )
        let fake = EditorCommitSpy(
            draftSnapshot: emptySnapshot,
            openReceipt: OpenInEditorResponse(planItemID: "item-gallery", variantID: "initial"),
            authoritativeVariant: Self.variant(duration: 2, generation: "generation-1")
        )
        let project = ProjectSummary(id: jobID, title: "Gallery cut", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        await session.load(libraryJobID: jobID, api: fake)
        XCTAssertEqual(fake.openedJobID, jobID)
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 2, accuracy: 0.0001)
        XCTAssertNotNil(session.player)

        let clipID = try! XCTUnwrap(session.draft.clips.first?.id)
        session.selectClip(clipID)
        session.trimSelected(edge: .trailing, to: 1.3)
        await session.save()
        XCTAssertEqual(fake.lastItemID, "item-gallery")
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 1.3, accuracy: 0.0001)

        XCTAssertTrue(session.rebaseCleanDraft(from: Self.variant(duration: 1.5, generation: "next")))
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 1.5, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 1.5, accuracy: 0.0001)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSaveCallsExplicitEditorCommitOnlyAfterEdit() async {
        let threadID = UUID(); let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 0, trimOut: 2)
        let snapshot: [String: JSONValue] = ["schema_version": .number(2), "kind": .string("editor"), "editor_payload": .object(["base_generation": .string("generation-1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "removed": .bool(false)])])])])]
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1", snapshot: snapshot, canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 4))
        await session.load(api: fake, threadID: threadID)
        session.selectClip(session.draft.clips[0].id); session.trimSelected(edge: .trailing, to: 1.5); await session.save()
        XCTAssertEqual(fake.commitCount, 1); XCTAssertEqual(fake.lastRequest?.baseGeneration, "generation-1"); XCTAssertEqual(session.saveState, .previewPending); XCTAssertFalse(session.hasUnsavedChanges)
    }

    private static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { value } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(value) = value { value } else { [] } }

    private static func variant(duration: Double, generation: String) -> [String: JSONValue] {
        [
            "variant_id": .string("initial"),
            "render_generation_id": .string(generation),
            "render_status": .string("ready"),
            "output_url": .string("file:///tmp/kria-editor-test.mp4"),
            "resolved_archetype": .string("narrated"),
            "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false)]),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(duration), "source_duration_s": .number(4), "removed": .bool(false)])])]),
        ]
    }
}

private final class EditorCommitSpy: KriaAPIClient, @unchecked Sendable {
    let draftSnapshot: DraftSnapshot
    let openReceipt: OpenInEditorResponse?
    let authoritativeVariant: [String: JSONValue]?
    var commitCount = 0
    var lastRequest: EditorCommitRequest?
    var openedJobID: UUID?
    var lastItemID: String?
    init(draftSnapshot: DraftSnapshot, openReceipt: OpenInEditorResponse? = nil, authoritativeVariant: [String: JSONValue]? = nil) {
        self.draftSnapshot = draftSnapshot
        self.openReceipt = openReceipt
        self.authoritativeVariant = authoritativeVariant
    }
    func projects() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func project(threadID: UUID) async throws -> CreationThread { throw APIError.unsupported }
    func library() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func createThread(message: String?) async throws -> CreationThread { throw APIError.unsupported }
    func exchangeMobileToken(_ credential: AuthCredential, provider: String) async throws -> MobileSession { throw APIError.unsupported }
    func refreshMobileSession(_ refreshToken: String) async throws -> MobileSession { throw APIError.unsupported }
    func revokeMobileSession(_ refreshToken: String) async throws { throw APIError.unsupported }
    func submitTurn(threadID: UUID, message: String, expectedRevision: Int) async throws -> TurnAccepted { throw APIError.unsupported }
    func applyCreationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int) async throws -> CreationThread { throw APIError.unsupported }
    func threadDelta(threadID: UUID, afterSequence: Int) async throws -> ThreadDelta { throw APIError.unsupported }
    func draft(threadID: UUID) async throws -> DraftSnapshot { draftSnapshot }
    func writeDraft(threadID: UUID, snapshot: [String: JSONValue], expectedRevision: Int, etag: String) async throws -> DraftSnapshot { throw APIError.unsupported }
    func openJobInEditor(jobID: UUID) async throws -> OpenInEditorResponse {
        openedJobID = jobID
        guard let openReceipt else { throw APIError.unsupported }
        return openReceipt
    }
    func editorVariant(jobID: UUID, variantID: String) async throws -> [String: JSONValue] {
        authoritativeVariant ?? ["variant_id": .string(variantID), "render_generation_id": .string("generation-1"), "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false)]), "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])])]
    }
    func editorCommit(itemID: String, variantID: String, request: EditorCommitRequest) async throws -> EditorCommitResponse {
        commitCount += 1; lastRequest = request; lastItemID = itemID
        return EditorCommitResponse(ok: true, generation: "next", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
    }
    func undoDraft(threadID: UUID, expectedRevision: Int) async throws -> DraftSnapshot { throw APIError.unsupported }
    func approval(threadID: UUID, approvalID: UUID) async throws -> ApprovalSnapshot { throw APIError.unsupported }
    func decideApproval(threadID: UUID, approvalID: UUID, decision: String, expectedThreadRevision: Int, expectedDraftRevision: Int, fingerprint: String) async throws { throw APIError.unsupported }
    func playbackURL(jobID: UUID) async throws -> URL { throw APIError.unsupported }
    func editRecipe(jobID: UUID, variantID: String?) async throws -> EditRecipe { throw APIError.unsupported }
    func reserveUpload(filename: String, contentType: String, size: Int64, purpose: UploadPurpose) async throws -> UploadReservation { throw APIError.unsupported }
    func cancelUpload(reservationID: UUID) async throws { throw APIError.unsupported }
    func reserveProjectUpload(threadID: UUID, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> ProjectUploadReservation { throw APIError.unsupported }
    func attachProjectMedia(threadID: UUID, mediaID: String, gcsPath: String, filename: String, contentType: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread { throw APIError.unsupported }
}
