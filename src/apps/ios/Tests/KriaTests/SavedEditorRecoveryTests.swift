import XCTest
import KriaMediaEngine
@testable import Kria

@MainActor
final class SavedEditorRecoveryTests: XCTestCase {
    private let threadID = UUID()
    private let jobID = UUID()
    private let itemID = UUID().uuidString
    private let draftID = UUID().uuidString

    private func variant() -> [String: JSONValue] {
        ["variant_id": .string("guided_story"), "render_status": .string("failed"),
         "render_generation_id": .string("saved-generation"), "editor_state_version": .number(2),
         "duration_s": .number(2), "editor_timeline_mode": .string("authored"),
         "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)]),
         "user_timeline": .object(["slots": .array([.object([
            "slot_id": .string("saved-clip"), "clip_index": .number(0), "in_s": .number(0.5),
            "duration_s": .number(2), "source_duration_s": .number(4), "removed": .bool(false)
         ])])]),
         "text_elements": .array([.object(["id": .string("saved-title"), "text": .string("My saved edit"),
            "start_s": .number(0), "end_s": .number(2), "color": .string("#FFFFFF")])])]
    }

    private func snapshot(variant content: [String: JSONValue]? = nil) -> DraftSnapshot {
        let content = content ?? variant()
        return DraftSnapshot(draftID: draftID, itemID: itemID, variantKey: "guided_story", draftRevision: 4,
            snapshotHash: "saved-hash", etag: "saved-etag", baseJobID: jobID.uuidString,
            baseGenerationID: "saved-generation", snapshot: ["kind": .string("editor"), "editor_payload": .object([
                "editor_state_version": .number(2), "base_generation": .string("saved-generation"),
                "sections": .object(["timeline_slots": content["user_timeline"]!.objectValue!["slots"]!,
                                     "text_elements": content["text_elements"]!])
            ])], canUndo: true, createdAt: Date(timeIntervalSince1970: 1_799_000_000))
    }

    private func thread(variant: [String: JSONValue]? = nil, format: String? = "montage", noOp: Bool = false, selectedVariantID: String = "guided_story") throws -> CreationThread {
        var v = variant ?? self.variant()
        if variant == nil {
            let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
            v["editor_draft"] = try JSONDecoder().decode(JSONValue.self, from: encoder.encode(snapshot()))
        }
        var state: [String: JSONValue] = ["selected_variant_id": .string(selectedVariantID)]
        if let format { state["format"] = .string(format) }
        let events: [JSONValue] = noOp ? [.object([
            "id": .string(UUID().uuidString), "sequence": .number(30), "revision": .number(31),
            "role": .string("assistant"), "event_type": .string("assistant_response"),
            "content": .string("All the text is already white, so the draft is unchanged."),
            "created_at": .string("2026-11-06T06:15:28Z")
        ])] : []
        let object: [String: JSONValue] = [
            "id": .string(threadID.uuidString), "title": .string("Saved edit"), "status": .string("active"),
            "revision": .number(noOp ? 31 : 29), "runtime_version": .number(2), "state": .object(state),
            "active_job_id": .string(jobID.uuidString), "active_plan_item_id": .string(itemID),
            "job": .object(["id": .string(jobID.uuidString), "status": .string("variants_failed"), "variants": .array([.object(v)])]),
            "events": .array(events), "updated_at": .string("2026-11-06T06:15:28Z")
        ]
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(CreationThread.self, from: JSONEncoder().encode(object))
    }

    func testFailedProjectionRetainsFullSavedDraftAcrossColdOpenAndNoOp() throws {
        for noOp in [false, true] {
            let t = try thread(noOp: noOp)
            XCTAssertEqual(t.summary.status, .failed)
            let encoded = try JSONEncoder().encode(XCTUnwrap(t.job?.variants.first))
            let wire = try JSONDecoder().decode([String: JSONValue].self, from: encoded)
            XCTAssertEqual(wire["editor_state_version"], .number(2))
            XCTAssertEqual(wire["editor_draft"]?.objectValue?["draft_id"]?.stringValue, snapshot().draftID)
        }
    }

    func testFullSavedEditRemainsReachableAfterNoOpWithoutChangingFailureStatus() throws {
        for noOp in [false, true] {
            let t = try thread(noOp: noOp)
            XCTAssertEqual(t.savedEditorVariant?.variantID, "guided_story")
            XCTAssertEqual(t.summary.status, .failed)
        }
    }

    func testIncompleteOrMismatchedSnapshotNeverOffersRecovery() throws {
        let valid = try XCTUnwrap(thread().job?.variants.first?.editorDraft)
        XCTAssertNil(try thread(selectedVariantID: "another-variant").savedEditorVariant)
        for key in ["item_id", "variant_key", "base_job_id", "base_generation_id"] {
            var draft = valid; draft[key] = .string(UUID().uuidString)
            var v = variant(); v["editor_draft"] = .object(draft)
            XCTAssertNil(try thread(variant: v).savedEditorVariant, key)
        }
        var v = variant()
        XCTAssertNil(try thread(variant: v).savedEditorVariant, "failed first render")
        v["editor_draft"] = .object(["draft_id": .string(draftID)])
        XCTAssertNil(try thread(variant: v).savedEditorVariant, "raw reference")
        var draft = valid
        var body = try XCTUnwrap(draft["snapshot"]?.objectValue)
        var payload = try XCTUnwrap(body["editor_payload"]?.objectValue)
        payload["base_generation"] = .string("another-generation")
        body["editor_payload"] = .object(payload)
        draft["snapshot"] = .object(body); v["editor_draft"] = .object(draft)
        XCTAssertNil(try thread(variant: v).savedEditorVariant, "body generation mismatch")
        draft["snapshot"] = .object([:]); v["editor_draft"] = .object(draft)
        XCTAssertNil(try thread(variant: v).savedEditorVariant, "missing authored body")
    }

    func testPublishedSourceGenerationAliasNeedsServerAttestationAndFullSnapshot() throws {
        var v = variant()
        let valid = try XCTUnwrap(thread().job?.variants.first?.editorDraft)
        v["editor_draft"] = .object(valid)
        v["render_generation_id"] = .string("published-attempt")
        XCTAssertNil(try thread(variant: v).savedEditorVariant)
        v["editor_draft_available"] = .bool(false)
        XCTAssertNil(try thread(variant: v).savedEditorVariant)
        v["editor_draft_available"] = .bool(true)
        XCTAssertEqual(try thread(variant: v).savedEditorVariant?.variantID, "guided_story")
        v["editor_draft"] = .object(["draft_id": .string(draftID)])
        XCTAssertNil(try thread(variant: v).savedEditorVariant, "an availability hint alone grants nothing")
    }

    func testSlidesAndUnknownFormatNeverOfferVideoRecovery() throws {
        XCTAssertNil(try thread(format: nil).savedEditorVariant)
        XCTAssertNil(try thread(format: "slides").savedEditorVariant)
        XCTAssertNil(try thread(format: "future-format").savedEditorVariant)
    }

    func testUnchangedFailedSavedEditOffersExplicitRetryAfterNoOp() async throws {
        let t = try thread(noOp: true)
        var v = variant()
        v["editor_draft"] = .object(try XCTUnwrap(t.job?.variants.first?.editorDraft))
        let fake = EditorCommitSpy(draftSnapshot: snapshot(), draftError: .unsupported, authoritativeVariant: v)
        let session = NativeEditorSession(project: t.summary)
        await session.load(project: t.summary, api: fake)
        XCTAssertFalse(session.hasUnsavedChanges)
        guard case .renderRetryNeeded = session.saveState else { return XCTFail("A stranded saved edit needs a real exit") }
        XCTAssertEqual(fake.commitCount, 0, "opening is read-only")
        await session.save()
        XCTAssertEqual(fake.commitCount, 0, "ordinary unchanged Save still does not dispatch")
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "saved-generation")
        XCTAssertEqual(fake.lastRequest?.timelineSlots?.first?.objectValue?["slot_id"]?.stringValue, "saved-clip")
        XCTAssertEqual(fake.lastRequest?.timelineSlots?.first?.objectValue?["in_s"]?.numberValue, 0.5)
        XCTAssertEqual(fake.lastRequest?.timelineSlots?.first?.objectValue?["duration_s"]?.numberValue, 2)
    }

    func testLegacyRoundedSavedDraftColdOpenCompileAndExplicitRetryKeepExactLanes() async throws {
        var v = variant()
        v["duration_s"] = .number(58.0 / 30)
        v["render_destination"] = .string("device")
        v["text_elements"] = .array([])
        v["user_timeline"] = .object(["slots": .array([.object([
            "slot_id": .string("saved-clip"), "clip_index": .number(0), "in_s": .number(0),
            "duration_s": .number(58.0 / 30), "source_duration_s": .number(1.92),
            "source_kind": .string("video"), "source_start_locked": .bool(false), "removed": .bool(false)
        ])])])
        let saved = snapshot(variant: v)
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        v["editor_draft"] = try JSONDecoder().decode(JSONValue.self, from: encoder.encode(saved))
        let t = try thread(variant: v, noOp: true)
        XCTAssertNotNil(t.savedEditorVariant)
        let fake = EditorCommitSpy(draftSnapshot: saved, draftError: .unsupported, authoritativeVariant: v)
        let session = NativeEditorSession(project: t.summary)
        await session.load(project: t.summary, api: fake)
        let before = session.document
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "bound-original",
            asset: MediaAsset(id: "original", relativePath: "original.mp4",
                fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 1.92),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let program = try compiler.compile(document: session.document, clips: session.timelineClips,
            items: session.timelineItems, sources: [0: source])
        let clip = try XCTUnwrap(program.recipe.tracks.first?.clips.first)
        XCTAssertEqual(clip.rate, 1, accuracy: 0.000_001)
        XCTAssertEqual(clip.sourceDuration, 1.92, accuracy: 0.000_001)
        XCTAssertEqual(clip.holdDuration ?? 0, 58.0 / 30 - 1.92, accuracy: 0.000_001)
        XCTAssertEqual(session.document, before)
        XCTAssertEqual(fake.commitCount, 0)
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 1)
        let sent = try XCTUnwrap(fake.lastRequest?.timelineSlots?.first?.objectValue)
        XCTAssertEqual(sent["in_s"], .number(0))
        XCTAssertEqual(sent["duration_s"]?.numberValue ?? 0, 58.0 / 30, accuracy: 0.000_001)
        XCTAssertNil(sent["source_end_s"], "compatibility must not rewrite historical authoring")
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "saved-generation")
    }



    func testSavedRenderRetryIsSingleFlightAndKeepsNewerLocalEdits() async throws {
        let t = try thread(noOp: true)
        var v = variant()
        v["editor_draft"] = .object(try XCTUnwrap(t.job?.variants.first?.editorDraft))
        let fake = EditorCommitSpy(draftSnapshot: snapshot(), draftError: .unsupported,
            authoritativeVariant: v, suspendNextCommit: true)
        let session = NativeEditorSession(project: t.summary)
        await session.load(project: t.summary, api: fake)
        let retry = Task { await session.retryRender() }
        for _ in 0..<200 {
            if fake.commitIsSuspended { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertTrue(fake.commitIsSuspended)
        session.updateTextContent(id: "saved-title", content: "A correction during retry")
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 1, "a second tap while saving must not duplicate dispatch")
        fake.authoritativeVariant?["render_generation_id"] = .string("next")
        fake.authoritativeVariant?["render_status"] = .string("rendering")
        fake.resumeCommit()
        await retry.value
        XCTAssertEqual(session.document.textElements.map(\.text), ["A correction during retry"])
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    private func conflictingRetryFixture(suspend: Bool = false) async throws -> (NativeEditorSession, EditorCommitSpy, [String: JSONValue]) {
        var v = variant()
        var rows = try XCTUnwrap(v["user_timeline"]?.objectValue?["slots"]?.arrayValue)
        var second = try XCTUnwrap(rows[0].objectValue)
        second["slot_id"] = .string("second-clip"); second["in_s"] = .number(1)
        rows.append(.object(second)); v["user_timeline"] = .object(["slots": .array(rows)])
        let saved = snapshot(variant: v)
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        v["editor_draft"] = try JSONDecoder().decode(JSONValue.self, from: encoder.encode(saved))
        let t = try thread(variant: v, noOp: true)
        let fake = EditorCommitSpy(draftSnapshot: saved, draftError: .unsupported,
                                   authoritativeVariant: v, suspendNextCommit: suspend)
        let session = NativeEditorSession(project: t.summary)
        await session.load(project: t.summary, api: fake)
        var newer = v
        var trimmed = try XCTUnwrap(rows[0].objectValue)
        trimmed["in_s"] = .number(1.25); trimmed["duration_s"] = .number(0.75)
        newer["user_timeline"] = .object(["slots": .array([rows[1], .object(trimmed)])])
        newer["render_generation_id"] = .string("newer-generation")
        newer.removeValue(forKey: "editor_draft")
        fake.authoritativeVariant = newer
        fake.commitErrorQueue = [.conflict]
        return (session, fake, newer)
    }

    func testUnchangedRetryConflictNeverReplaysOldTimelineOverNewerTrimAndOrder() async throws {
        let (session, fake, _) = try await conflictingRetryFixture()
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 1, "retry consent for G1 must not overwrite G2")
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "saved-generation")
        XCTAssertEqual(session.document.revision.baseGeneration, "newer-generation")
        XCTAssertEqual(session.document.clips.map(\.id), ["second-clip", "saved-clip"])
        XCTAssertEqual(session.document.clips.last?.inS, 1.25)
        XCTAssertEqual(session.document.clips.last?.durationS, 0.75)
        XCTAssertFalse(session.hasUnsavedChanges, "retry intent is not a local timeline edit")
        guard case .renderRetryNeeded = session.saveState else { return XCTFail("newer edit requires explicit review/retry") }
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 2, "only a new explicit retry may submit G2")
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "newer-generation")
        XCTAssertEqual(fake.lastRequest?.timelineSlots?.first?.objectValue?["slot_id"], .string("second-clip"))
    }

    func testRetryConflictKeepsConcurrentLocalEditsWithoutAutomaticallySubmittingThem() async throws {
        let (session, fake, _) = try await conflictingRetryFixture(suspend: true)
        let retry = Task { await session.retryRender() }
        for _ in 0..<200 {
            if fake.commitIsSuspended { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertTrue(fake.commitIsSuspended)
        session.transactDocument(section: .timeline) { $0.clips[0].durationS = 1.5 }
        session.updateTextContent(id: "saved-title", content: "Concurrent local work")
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 1)
        fake.resumeCommit(); await retry.value
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertEqual(session.document.revision.baseGeneration, "newer-generation")
        XCTAssertEqual(session.document.clips.first?.durationS, 1.5)
        XCTAssertEqual(session.document.textElements.first?.text, "Concurrent local work")
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testRetryConflictPreservesDirtyTextWithoutReplayingCleanTimeline() async throws {
        let (session, fake, _) = try await conflictingRetryFixture()
        session.updateTextContent(id: "saved-title", content: "My unsaved title")
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertEqual(session.document.clips.map(\.id), ["second-clip", "saved-clip"])
        XCTAssertEqual(session.document.clips.last?.inS, 1.25)
        XCTAssertEqual(session.document.textElements.first?.text, "My unsaved title")
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testGenuineTimelineSaveRetainsExistingConflictRebaseContract() async throws {
        let (session, fake, _) = try await conflictingRetryFixture()
        session.transactDocument(section: .timeline) { $0.clips[0].durationS = 1.5 }
        await session.save()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "newer-generation")
        XCTAssertEqual(fake.lastRequest?.timelineSlots?.first?.objectValue?["duration_s"], .number(1.5))
    }

    func testFailedProjectLoadsSavedLanesWithoutRenderAndReconcilesOwnDirtyEdits() async throws {
        let t = try thread()
        let fake = EditorCommitSpy(draftSnapshot: snapshot(), draftError: .unsupported, authoritativeVariant: variant())
        let session = NativeEditorSession(project: t.summary)
        await session.load(project: t.summary, api: fake)
        XCTAssertEqual(session.loadState, .loaded)
        XCTAssertEqual(session.document.clips.map(\.id), ["saved-clip"])
        XCTAssertEqual(session.document.clips.first?.inS, 0.5)
        XCTAssertEqual(session.document.textElements.map(\.text), ["My saved edit"])
        XCTAssertEqual(fake.commitCount, 0)
        XCTAssertNil(fake.openedJobID)
        session.updateTextContent(id: "saved-title", content: "My local correction")
        XCTAssertTrue(session.hasUnsavedChanges)
        let reopened = try thread(noOp: true)
        let reconciled = await session.reconcileOnOpen(project: reopened.summary, api: fake)
        XCTAssertTrue(reconciled)
        XCTAssertEqual(session.document.textElements.map(\.text), ["My local correction"])
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(fake.commitCount, 0)
        XCTAssertEqual(reopened.summary.status, .failed)
    }
}
