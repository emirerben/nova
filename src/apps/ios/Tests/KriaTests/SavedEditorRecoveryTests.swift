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
        XCTAssertFalse(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
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
        XCTAssertFalse(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
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

    func testTransientRetryFailuresKeepVisibleActionWithoutDirtyingLanes() async throws {
        for error: APIError in [.offline, .requestFailed(status: 503)] {
            for dirtyText in [false, true] {
                let (session, fake, _) = try await conflictingRetryFixture()
                if dirtyText { session.updateTextContent(id: "saved-title", content: "Local title") }
                fake.commitErrorQueue = [error, error]
                let before = session.document
                for count in 1...2 {
                    await session.retryRender()
                    XCTAssertEqual(fake.commitCount, count)
                    XCTAssertEqual(fake.lastRequest?.baseGeneration, "saved-generation")
                    XCTAssertEqual(session.document, before)
                    XCTAssertEqual(session.hasUnsavedChanges, dirtyText)
                    guard case .failed = session.saveState else { return XCTFail("failure must remain truthful") }
                    XCTAssertTrue(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
                    XCTAssertFalse(session.isSaving)
                }
            }
        }
    }

    func testRetryConflictFailedReloadKeepsActionAndGenerationFence() async throws {
        let (session, fake, _) = try await conflictingRetryFixture()
        fake.editorVariantError = .offline
        let before = session.document
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertEqual(session.document, before)
        XCTAssertFalse(session.hasUnsavedChanges)
        guard case .failed = session.saveState else { return XCTFail("reload failure is not a saved result") }
        XCTAssertTrue(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
        fake.editorVariantError = nil
        fake.commitErrorQueue = [.conflict]
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 2, "the explicit retry cannot automatically resubmit after reloading")
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "saved-generation")
        XCTAssertEqual(session.document.revision.baseGeneration, "newer-generation")
        XCTAssertEqual(session.document.clips.map(\.id), ["second-clip", "saved-clip"])
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
    }

    func testUncertainRetryResponseRetainsExplicitActionAndReconcilesCommittedGeneration() async throws {
        for error: any Error in [CancellationError(), URLError(.timedOut)] {
            let (session, fake, _) = try await conflictingRetryFixture()
            fake.commitErrorQueue = []; fake.commitThrow = error
            let before = session.document
            await session.retryRender()
            XCTAssertEqual(fake.commitCount, 1, "uncertain results never auto-resend")
            XCTAssertEqual(session.document, before)
            XCTAssertFalse(session.hasUnsavedChanges)
            guard case .failed = session.saveState else { return XCTFail("unknown outcome cannot claim success") }
            XCTAssertTrue(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
            fake.commitThrow = nil; fake.commitErrorQueue = [.conflict]
            await session.retryRender()
            XCTAssertEqual(fake.commitCount, 2)
            XCTAssertEqual(fake.lastRequest?.baseGeneration, "saved-generation")
            XCTAssertEqual(session.document.revision.baseGeneration, "newer-generation")
            XCTAssertEqual(session.document.clips.map(\.id), ["second-clip", "saved-clip"])
            XCTAssertFalse(session.hasUnsavedChanges)
        }
    }

    func testRetrySuccessAndDurableRecoveryHaveAccurateActions() async throws {
        for renderStarted in [false, true] {
            let (session, fake, _) = try await conflictingRetryFixture()
            fake.commitErrorQueue = []
            fake.commitResponse = EditorCommitResponse(ok: true, generation: "next",
                sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false),
                revisionNumber: nil, revisionHash: nil, expectedDuration: nil, renderStarted: renderStarted)
            let clips = session.document.clips
            await session.retryRender()
            XCTAssertEqual(fake.commitCount, 1)
            XCTAssertEqual(session.document.clips, clips)
            XCTAssertFalse(session.hasUnsavedChanges)
            XCTAssertEqual(session.document.revision.baseGeneration, "next")
            XCTAssertEqual(NativeEditorSaveBanner(session: session).showsRenderRetryAction, !renderStarted)
            if renderStarted { XCTAssertEqual(session.saveState, .previewPending) }
            else if case .renderRetryNeeded = session.saveState {} else { XCTFail("durable render recovery must remain retryable") }
        }
    }

    func testOrdinaryFailedSaveDoesNotInventRenderRetryIntent() async throws {
        let t = try thread()
        let fake = EditorCommitSpy(draftSnapshot: snapshot(), draftError: .unsupported,
                                   authoritativeVariant: variant(), commitError: .offline)
        let session = NativeEditorSession(project: t.summary)
        await session.load(project: t.summary, api: fake)
        session.updateTextContent(id: "saved-title", content: "Local title")
        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertFalse(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
    }

    func testConflictReloadCancellationInSameEditorEndsWithVisibleRetry() async throws {
        for dirty in [false, true] {
            let (session, fake, _) = try await conflictingRetryFixture()
            if dirty { session.updateTextContent(id: "saved-title", content: "Keep this local edit") }
            let before = session.document
            fake.editorVariantThrow = CancellationError()
            await session.retryRender()
            XCTAssertEqual(fake.commitCount, 1)
            XCTAssertEqual(session.document, before)
            XCTAssertEqual(session.hasUnsavedChanges, dirty)
            XCTAssertFalse(session.isSaving)
            guard case .failed = session.saveState else { XCTFail("same-editor cancellation needs a terminal failure"); continue }
            XCTAssertTrue(NativeEditorSaveBanner(session: session).showsRenderRetryAction)
            fake.editorVariantThrow = nil; fake.commitErrorQueue = [.conflict]
            await session.retryRender()
            XCTAssertEqual(fake.commitCount, 2)
            XCTAssertEqual(session.document.revision.baseGeneration, "newer-generation")
            XCTAssertEqual(session.document.clips.map(\.id), ["second-clip", "saved-clip"])
            XCTAssertEqual(session.hasUnsavedChanges, dirty)
        }
    }

    func testOldConflictReloadCannotChangeReplacementEditorOrClearItsLoadingState() async throws {
        for target in ["new-job", "new-variant", "same-target-reset"] {
            for cancelled in [false, true] {
                for replacementStillLoading in [false, true] {
                    let (session, old, _) = try await conflictingRetryFixture()
                    old.suspendNextEditorVariant = true
                    let retry = Task { await session.retryRender() }
                    for _ in 0..<200 {
                        if old.editorVariantIsSuspended { break }
                        try await Task.sleep(for: .milliseconds(5))
                    }
                    XCTAssertTrue(old.editorVariantIsSuspended)
                    let nextVariant = target == "new-variant" ? "replacement-variant" : "guided_story"
                    var replacementVariant = variant()
                    replacementVariant["variant_id"] = .string(nextVariant)
                    replacementVariant["text_elements"] = .array([.object([
                        "id": .string("replacement-title"), "text": .string("Replacement editor"),
                        "start_s": .number(0), "end_s": .number(2)
                    ])])
                    let nextDraft = DraftSnapshot(draftID: UUID().uuidString, itemID: itemID,
                        variantKey: nextVariant, draftRevision: 7, snapshotHash: "replacement", etag: "replacement",
                        baseJobID: target == "new-job" ? UUID().uuidString : jobID.uuidString,
                        baseGenerationID: "saved-generation", snapshot: [:], canUndo: false, createdAt: .now)
                    let replacement = EditorCommitSpy(draftSnapshot: nextDraft, authoritativeVariant: replacementVariant)
                    replacement.suspendNextEditorVariant = replacementStillLoading
                    let load = Task { await session.load(api: replacement, threadID: threadID, variantID: nextVariant) }
                    if replacementStillLoading {
                        for _ in 0..<200 {
                            if replacement.editorVariantIsSuspended { break }
                            try await Task.sleep(for: .milliseconds(5))
                        }
                        XCTAssertTrue(replacement.editorVariantIsSuspended)
                    } else { await load.value }
                    let replacementDocument = session.document
                    let replacementState = session.saveState
                    let replacementDirty = session.hasUnsavedChanges
                    if cancelled { old.editorVariantThrow = CancellationError() }
                    old.resumeEditorVariant(); await retry.value
                    let context = "\(target), cancelled=\(cancelled), loading=\(replacementStillLoading)"
                    XCTAssertEqual(old.commitCount, 1, context)
                    XCTAssertEqual(replacement.commitCount, 0, context)
                    XCTAssertEqual(session.document, replacementDocument, context)
                    XCTAssertEqual(session.saveState, replacementState, context)
                    XCTAssertEqual(session.hasUnsavedChanges, replacementDirty, context)
                    XCTAssertEqual(session.isSaving, replacementStillLoading, context)
                    if replacementStillLoading { replacement.resumeEditorVariant(); await load.value }
                    XCTAssertEqual(session.document.textElements.first?.text, "Replacement editor", context)
                    XCTAssertFalse(session.isSaving, context)
                }
            }
        }
    }

    func testOldCommitCannotFinishInsideSameTargetReplacementEditor() async throws {
        for cancelled in [false, true] {
            let (session, old, _) = try await conflictingRetryFixture(suspend: true)
            old.commitErrorQueue = []
            let retry = Task { await session.retryRender() }
            for _ in 0..<200 {
                if old.commitIsSuspended { break }
                try await Task.sleep(for: .milliseconds(5))
            }
            XCTAssertTrue(old.commitIsSuspended)
            let replacement = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
            await session.load(api: replacement, threadID: threadID, variantID: "guided_story")
            session.updateTextContent(id: "saved-title", content: "Replacement local work")
            session.refreshRetryDelays = [.seconds(3600)]
            replacement.draftError = .requestFailed(status: 503)
            for _ in 0..<NativeEditorSession.reconnectingHintAfterFailures { await session.synchronizePromptRevision() }
            XCTAssertEqual(session.refreshSync, .reconnecting)
            let before = session.document
            let state = session.saveState
            if cancelled { old.commitThrow = CancellationError() }
            old.resumeCommit(); await retry.value
            XCTAssertEqual(session.refreshSync, .reconnecting)
            XCTAssertEqual(session.document, before)
            XCTAssertEqual(session.saveState, state)
            XCTAssertTrue(session.hasUnsavedChanges)
            XCTAssertFalse(session.isSaving)
            XCTAssertEqual(old.commitCount, 1)
            XCTAssertEqual(replacement.commitCount, 0)
        }
    }

    func testOldPostCommitReadCannotReconcileSameGenerationReplacement() async throws {
        for dirty in [false, true] {
            let (session, old, _) = try await conflictingRetryFixture()
            old.commitErrorQueue = []
            old.suspendNextEditorVariant = true
            var committed = variant()
            committed["render_generation_id"] = .string("next")
            committed["text_elements"] = .array([.object(["id": .string("saved-title"), "text": .string("Old response text"), "start_s": .number(0), "end_s": .number(2)])])
            old.authoritativeVariant = committed
            let retry = Task { await session.retryRender() }
            for _ in 0..<200 {
                if old.editorVariantIsSuspended { break }
                try await Task.sleep(for: .milliseconds(5))
            }
            XCTAssertTrue(old.editorVariantIsSuspended)
            var replacementVariant = committed
            replacementVariant["text_elements"] = variant()["text_elements"]
            let replacementDraft = DraftSnapshot(draftID: draftID, itemID: itemID, variantKey: "guided_story",
                draftRevision: 5, snapshotHash: "next", etag: "next", baseJobID: jobID.uuidString,
                baseGenerationID: "next", snapshot: [:], canUndo: false, createdAt: .now)
            let replacement = EditorCommitSpy(draftSnapshot: replacementDraft, authoritativeVariant: replacementVariant)
            await session.load(api: replacement, threadID: threadID, variantID: "guided_story")
            XCTAssertEqual(session.document.revision.baseGeneration, "next")
            if dirty { session.updateTextContent(id: "saved-title", content: "Replacement local work") }
            let before = session.document
            let state = session.saveState
            let refresh = session.refreshSync
            let undo = session.canUndo
            old.resumeEditorVariant(); await retry.value
            XCTAssertEqual(session.document, before)
            XCTAssertEqual(session.saveState, state)
            XCTAssertEqual(session.refreshSync, refresh)
            XCTAssertEqual(session.canUndo, undo)
            XCTAssertEqual(session.hasUnsavedChanges, dirty)
            XCTAssertFalse(session.isSaving)
            XCTAssertEqual(old.commitCount, 1)
            XCTAssertEqual(replacement.commitCount, 0)
            if dirty {
                session.undo()
                XCTAssertEqual(session.document.textElements.first?.text, "My saved edit")
                XCTAssertFalse(session.hasUnsavedChanges, "replacement clean baseline must survive")
            }
        }
    }

    func testOlderLoadsCannotApplyOrClearReplacementState() async throws {
        for viaProject in [false, true] {
            for outcome in ["success", "failure", "cancellation"] {
                for dirty in [false, true] {
                  for otherTarget in [false, true] {
                    let t = try thread()
                    let old = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
                    old.suspendNextEditorVariant = true
                    let session = NativeEditorSession(project: t.summary)
                    let load = Task {
                        if viaProject { await session.load(project: t.summary, api: old) }
                        else { await session.load(api: old, threadID: threadID, variantID: "guided_story") }
                    }
                    for _ in 0..<200 {
                        if old.editorVariantIsSuspended { break }
                        try await Task.sleep(for: .milliseconds(5))
                    }
                    XCTAssertTrue(old.editorVariantIsSuspended)
                    let targetVariant = otherTarget ? "other-variant" : "guided_story"
                    var nextVariant = variant(); nextVariant["variant_id"] = .string(targetVariant)
                    let nextDraft = DraftSnapshot(draftID: draftID, itemID: itemID, variantKey: targetVariant,
                        draftRevision: 4, snapshotHash: "replacement", etag: "replacement",
                        baseJobID: otherTarget ? UUID().uuidString : jobID.uuidString,
                        baseGenerationID: "saved-generation", snapshot: [:], canUndo: false, createdAt: .now)
                    let replacement = EditorCommitSpy(draftSnapshot: nextDraft, authoritativeVariant: nextVariant)
                    await session.load(api: replacement, threadID: threadID, variantID: targetVariant)
                    if dirty { session.updateTextContent(id: "saved-title", content: "Replacement edit") }
                    let before = session.document
                    let state = session.saveState
                    if outcome == "failure" { old.editorVariantError = .unsupported }
                    if outcome == "cancellation" { old.editorVariantThrow = CancellationError() }
                    old.resumeEditorVariant(); await load.value
                    XCTAssertEqual(session.document, before, "\(viaProject) \(outcome) \(dirty)")
                    XCTAssertEqual(session.saveState, state)
                    XCTAssertEqual(session.loadState, .loaded)
                    XCTAssertEqual(session.hasUnsavedChanges, dirty)
                    XCTAssertEqual(session.canUndo, dirty)
                    XCTAssertFalse(session.isSaving)
                    if dirty { session.undo(); XCTAssertFalse(session.hasUnsavedChanges) }
                  }
                }
            }
        }
    }

    func testOldPreviewPollCannotApplyToResetEditor() async throws {
        for dirty in [false, true] {
            let (session, old, _) = try await conflictingRetryFixture()
            old.commitErrorQueue = []
            var rendering = variant()
            rendering["render_generation_id"] = .string("next")
            rendering["render_status"] = .string("rendering")
            old.authoritativeVariant = rendering
            session.previewPollInterval = .milliseconds(30)
            await session.retryRender()
            old.suspendNextEditorVariant = true
            for _ in 0..<200 {
                if old.editorVariantIsSuspended { break }
                try await Task.sleep(for: .milliseconds(5))
            }
            XCTAssertTrue(old.editorVariantIsSuspended)
            let replacement = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
            await session.load(api: replacement, threadID: threadID, variantID: "guided_story")
            if dirty { session.updateTextContent(id: "saved-title", content: "Replacement edit") }
            let before = session.document
            let state = session.saveState
            rendering["render_status"] = .string("ready")
            rendering["output_url"] = .string("file:///tmp/cunda-missing-fixture-output.mp4")
            rendering["text_elements"] = .array([.object(["id": .string("saved-title"), "text": .string("Stale poll"), "start_s": .number(0), "end_s": .number(2)])])
            old.authoritativeVariant = rendering
            old.resumeEditorVariant()
            try await Task.sleep(for: .milliseconds(100))
            XCTAssertEqual(session.document, before)
            XCTAssertEqual(session.saveState, state)
            XCTAssertEqual(session.hasUnsavedChanges, dirty)
            XCTAssertEqual(session.canUndo, dirty)
        }
    }

    func testOldCapabilityLoadCannotChangeReplacementPermissionsOrLoading() async throws {
        for fails in [false, true] {
            let t = try thread()
            let old = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
            old.suspendNextCapabilities = true
            old.creationMode = .deviceOnly
            let session = NativeEditorSession(project: t.summary)
            let load = Task { await session.load(api: old, threadID: threadID, variantID: "guided_story") }
            for _ in 0..<200 {
                if old.capabilitiesIsSuspended { break }
                try await Task.sleep(for: .milliseconds(5))
            }
            XCTAssertTrue(old.capabilitiesIsSuspended)
            let replacement = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
            replacement.suspendNextEditorVariant = true
            let next = Task { await session.load(api: replacement, threadID: threadID, variantID: "guided_story") }
            for _ in 0..<200 {
                if replacement.editorVariantIsSuspended { break }
                try await Task.sleep(for: .milliseconds(5))
            }
            let before = session.document
            let editable = session.canEditText
            if fails { old.capabilitiesError = .offline }
            old.resumeCapabilities(); await load.value
            XCTAssertTrue(session.isSaving, "only the replacement load owns cleanup")
            XCTAssertEqual(session.loadState, .loading)
            XCTAssertEqual(session.document, before)
            XCTAssertEqual(session.canEditText, editable)
            replacement.resumeEditorVariant(); await next.value
            XCTAssertTrue(session.canEditText)
            XCTAssertFalse(session.isSaving)
        }
    }

    func testDeferredConflictReloadCannotAdoptReplacementLifetime() async throws {
        let t = try thread()
        let old = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
        old.sourcePoolError = .conflict
        let session = NativeEditorSession(project: t.summary)
        await session.load(api: old, threadID: threadID, variantID: "guided_story")
        let oldReads = old.editorVariantJobIDs.count
        let replacement = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
        await session.load(api: replacement, threadID: threadID, variantID: "guided_story")
        session.updateTextContent(id: "saved-title", content: "Replacement edit")
        let before = session.document
        let state = session.sourcePreviewState
        let replacementReads = replacement.editorVariantJobIDs.count
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(old.editorVariantJobIDs.count, oldReads)
        XCTAssertEqual(replacement.editorVariantJobIDs.count, replacementReads)
        XCTAssertEqual(session.document, before)
        XCTAssertEqual(session.sourcePreviewState, state)
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testDelayedDeviceCapabilityReadDoesNotDispatchForReplacement() async throws {
        for fails in [false, true] {
            let t = try thread()
            var deviceVariant = variant()
            deviceVariant["render_destination"] = .string("device")
            let old = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: deviceVariant)
            let session = NativeEditorSession(project: t.summary)
            let renders = DeviceRenderSessions(fetch: { _, _ in
                await MainActor.run { old.deviceFetchCount += 1 }
                throw APIError.unsupported
            }, factory: { _, _ in throw APIError.unsupported })
            session.useDeviceRendering(renders)
            await session.load(api: old, threadID: threadID, variantID: "guided_story")
            old.suspendNextCapabilities = true
            let refresh = Task { await session.refreshDeviceRender() }
            for _ in 0..<200 {
                if old.capabilitiesIsSuspended { break }
                try await Task.sleep(for: .milliseconds(5))
            }
            XCTAssertTrue(old.capabilitiesIsSuspended)
            let replacement = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: deviceVariant)
            await session.load(api: replacement, threadID: threadID, variantID: "guided_story")
            let before = session.document
            if fails { old.capabilitiesError = .offline }
            old.resumeCapabilities(); await refresh.value
            XCTAssertEqual(old.deviceFetchCount, 0)
            XCTAssertEqual(session.document, before)
        }
    }

    func testPromptRefreshCannotClearOrAdoptReplacementState() async throws {
        for outcome in ["success", "failure", "adoption"] {
            let (session, old, _) = try await conflictingRetryFixture()
            if outcome == "adoption" {
                old.draftSnapshot = DraftSnapshot(draftID: draftID, itemID: itemID, variantKey: "guided_story",
                    draftRevision: 5, snapshotHash: "other", etag: "other", baseJobID: UUID().uuidString,
                    baseGenerationID: "other", snapshot: [:], canUndo: false, createdAt: .now)
            }
            old.draftError = nil
            old.suspendNextEditorVariant = true
            let refresh = Task { await session.synchronizePromptRevision() }
            for _ in 0..<200 {
                if old.editorVariantIsSuspended { break }
                try await Task.sleep(for: .milliseconds(5))
            }
            XCTAssertTrue(old.editorVariantIsSuspended)
            let replacement = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
            await session.load(api: replacement, threadID: threadID, variantID: "guided_story")
            session.updateTextContent(id: "saved-title", content: "Replacement edit")
            session.refreshRetryDelays = [.seconds(3600)]
            replacement.draftError = .offline
            for _ in 0..<NativeEditorSession.reconnectingHintAfterFailures { await session.synchronizePromptRevision() }
            XCTAssertEqual(session.refreshSync, .reconnecting)
            let before = session.document
            let projectReads = old.projectCallCount
            if outcome == "failure" { old.editorVariantError = .conflict }
            old.resumeEditorVariant(); await refresh.value
            XCTAssertEqual(session.refreshSync, .reconnecting)
            XCTAssertEqual(session.document, before)
            XCTAssertTrue(session.hasUnsavedChanges)
            XCTAssertEqual(old.projectCallCount, projectReads, "old refresh must not adopt its thread into replacement")
        }
    }

    func testReopenPreservesEditsMadeDuringReadAndNeverAuthorizesStaleFallback() async throws {
        for replacement in [false, true] {
            for outcome in ["success", "failure", "conflict"] {
                let (session, old, _) = try await conflictingRetryFixture()
                let t = try thread()
                old.suspendNextEditorVariant = true
                var newer = variant(); newer["render_generation_id"] = .string("newer")
                old.authoritativeVariant = newer
                let initiatingLifetime = session.editorLifetimeID
                let open = Task { await session.reconcileOnOpen(project: t.summary, api: old) }
                for _ in 0..<200 {
                    if old.editorVariantIsSuspended { break }
                    try await Task.sleep(for: .milliseconds(5))
                }
                XCTAssertTrue(old.editorVariantIsSuspended)
                if replacement {
                    let next = EditorCommitSpy(draftSnapshot: snapshot(), authoritativeVariant: variant())
                    await session.load(api: next, threadID: threadID, variantID: "guided_story")
                }
                session.updateTextContent(id: "saved-title", content: "Keep my new edit")
                let before = session.document
                if outcome != "success" { old.editorVariantError = outcome == "conflict" ? .conflict : .unsupported }
                old.resumeEditorVariant()
                let handled = await open.value
                XCTAssertTrue(handled, "false would authorize the view to discard edits with fallback load")
                XCTAssertEqual(session.document, before)
                XCTAssertTrue(session.hasUnsavedChanges)
                if replacement {
                    XCTAssertNil(session.newerJobPrompt)
                    let fallback = await session.loadForEditorOpen(project: t.summary, libraryJobID: nil,
                        api: old, expectedLifetime: initiatingLifetime)
                    XCTAssertNil(fallback, "even a delayed caller must not load over a replacement")
                    XCTAssertEqual(session.document, before)
                }
                else if outcome == "success" { XCTAssertNotNil(session.newerJobPrompt) }
                session.undo()
                XCTAssertFalse(session.hasUnsavedChanges)
            }
        }
    }

    func testResetClearsAppliedBadgeAndReplacementAnnouncementDismisses() async throws {
        let (session, old, _) = try await conflictingRetryFixture()
                let t = try thread()
        old.commitErrorQueue = []
        session.previewPollInterval = .seconds(3600)
        await session.retryRender()
        var ready = variant()
        ready["render_generation_id"] = .string("next")
        ready["render_status"] = .string("ready")
        ready["output_url"] = .string("file:///tmp/cunda-badge-fixture.mp4")
        XCTAssertTrue(session.applyPreviewVariant(ready, generation: "next"))
        XCTAssertTrue(session.showsEditApplied)
        var saved = variant()
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        saved["editor_draft"] = try JSONDecoder().decode(JSONValue.self, from: encoder.encode(snapshot()))
        let next = EditorCommitSpy(draftSnapshot: snapshot(), draftError: .unsupported, authoritativeVariant: saved)
        await session.load(project: t.summary, api: next)
        XCTAssertFalse(session.showsEditApplied)
        await session.retryRender()
        XCTAssertTrue(session.applyPreviewVariant(ready, generation: "next"))
        XCTAssertTrue(session.showsEditApplied)
        try await Task.sleep(for: .milliseconds(3200))
        XCTAssertFalse(session.showsEditApplied)
    }

    func testFallbackLoadRechecksEditsAfterReconcileReturns() async throws {
        let (session, old, _) = try await conflictingRetryFixture()
        let t = try thread()
        let lifetime = session.editorLifetimeID
        let handled = await session.reconcileOnOpen(project: t.summary, api: old)
        XCTAssertFalse(handled, "a clean editor may normally reload a newer generation")
        session.updateTextContent(id: "saved-title", content: "Typed before caller resumed")
        let before = session.document
        let loaded = await session.loadForEditorOpen(project: t.summary, libraryJobID: nil,
            api: old, expectedLifetime: lifetime)
        XCTAssertNil(loaded)
        XCTAssertEqual(session.document, before)
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
