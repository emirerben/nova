import AVFoundation
import XCTest
@testable import Kria

/// KRI-219/226: a runtime-v2 chat editor turn parks its edit in the draft head
/// (never rendered). The editor must show it immediately as UNSAVED edits.
@MainActor
final class ChatDraftStagingTests: XCTestCase {
    private static let jobID = "5b1d2f0a-6c0e-4b8e-9d0a-1c2d3e4f5a6b"
    private static let musicID = "8c9c1e0e-2d62-4a44-8d0c-2f4d3a0a9b11"

    private static func variant(generation: String = "g1") -> [String: JSONValue] {
        func slot(_ id: String, _ index: Double) -> JSONValue {
            .object(["slot_id": .string(id), "clip_index": .number(index), "in_s": .number(0), "duration_s": .number(2),
                     "source_duration_s": .number(6), "removed": .bool(false)])
        }
        return [
            "variant_id": .string("initial"), "render_generation_id": .string(generation),
            "render_status": .string("ready"), "duration_s": .number(6),
            "output_url": .string("file:///tmp/kria-chat-draft.mp4"),
            "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(true)]),
            "user_timeline": .object(["slots": .array([slot("s1", 0), slot("s2", 1), slot("s3", 2)])]),
            "text_elements": .array([.object(["id": .string("t1"), "text": .string("hello"), "start_s": .number(0), "end_s": .number(2),
                                              "role": .string("generative_intro"), "x_frac": .number(0.5), "y_frac": .number(0.2)])]),
            "music_track_id": .string(musicID), "music_window": .object(["start_s": .number(1), "alignment": .string("preserve_cuts")]),
            "audio_mix": .object(["music_level": .number(0.8)]),
        ]
    }

    /// `.flatOnly`: first chat edit on a non-editor head (no nested `sections`).
    /// `.staleSections`: `sections` present but holding OLD lane values.
    enum Shape { case nested, flatOnly, staleSections }

    /// Chat draft in the server's real shape: flat commit-shaped keys for the
    /// lanes chat touched + the nested `sections` overlaid with the same state.
    private static func chatPayload(base: String = "g1", textOnly: Bool = false, shape: Shape = .nested) -> [String: JSONValue] {
        func wire(_ id: String, _ index: Double, _ duration: Double, removed: Bool = false) -> JSONValue {
            .object(["slot_id": .string(id), "clip_index": .number(index), "in_s": .number(0), "duration_s": .number(duration),
                     "removed": .bool(removed), "look_preset": .string("none"), "transition_after": .string("cut")])
        }
        let slots: [JSONValue] = [wire("s3", 2, 2), wire("s1", 0, 1.5), wire("s2", 1, 2, removed: true)]
        let text: JSONValue = .array([.object(["id": .string("t1"), "text": .string("changed"), "start_s": .number(0), "end_s": .number(3600),
                                               "role": .string("generative_intro"), "effect": .string("karaoke-line"), "x_frac": .number(0.5), "y_frac": .number(0.2),
                                               "color": .null, "glow_color": .null])])
        let mix: JSONValue = .object(["music_level": .number(0.3)])
        var payload: [String: JSONValue] = [
            "base_generation": .string(base), "text_elements": text,
            "sections": .object([
                "text_elements": text,
            ]),
        ]
        if shape == .flatOnly {
            payload["sections"] = nil
            payload["copilot_receipt_ids"] = .array([]); payload["retry_guided_revision"] = .bool(false)
            payload["guided_revision_number"] = .number(2)
        }
        if !textOnly {
            payload["timeline_slots"] = .array(slots); payload["mix"] = mix; payload["remove_music"] = .bool(true)
            payload["sections"] = .object([
                "timeline_slots": .array(slots), "text_elements": text, "mix": mix, "music_track_id": .null,
                "music_window": .object(["start_s": .number(1), "alignment": .string("preserve_cuts")]),
            ])
            if shape == .flatOnly { payload["sections"] = nil }
            if shape == .staleSections {
                payload["sections"] = .object(["text_elements": .array([.object(["id": .string("t1"), "text": .string("OLD")])]),
                                               "mix": .object(["music_level": .number(0.9)])])
            }
        }
        return payload
    }

    private static func chatSnapshot(base: String = "g1", revision: Int = 3, textOnly: Bool = false, shape: Shape = .nested) -> DraftSnapshot {
        DraftSnapshot(draftID: "d\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: base,
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "edit_format": .string("montage"),
                       "editor_payload": .object(chatPayload(base: base, textOnly: textOnly, shape: shape))], canUndo: true, createdAt: .now)
    }

    private static func bootstrapSnapshot() -> DraftSnapshot {
        DraftSnapshot(draftID: "d1", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e",
            baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "editor_payload": .object(["base_generation": .string("g1"), "sections": .object([:])])],
            canUndo: false, createdAt: .now)
    }

    private func loaded(_ snapshot: DraftSnapshot) async -> (NativeEditorSession, EditorCommitSpy) {
        let fake = EditorCommitSpy(draftSnapshot: snapshot, authoritativeVariant: Self.variant(),
            commitResponse: EditorCommitResponse(ok: true, generation: "g2",
                sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: true, mix: true),
                revisionNumber: 4, revisionHash: "r4", expectedDuration: nil))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        return (session, fake)
    }

    func testMatchingGenerationOverlaysChatLanesAsUnsavedEdits() async throws {
        let (session, _) = await loaded(Self.chatSnapshot())
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.dirtySections, [.timeline, .text, .mix, .music])
        XCTAssertEqual(session.document.clips.compactMap(\.id), ["s3", "s1"], "reorder + removal applied")
        XCTAssertEqual(session.document.clips.last?.durationS, 1.5)
        XCTAssertEqual(session.document.tombstones.compactMap(\.id), ["s2"])
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertEqual(session.document.mix["music_level"], .number(0.3))
        XCTAssertNil(session.document.music, "remove music = music_track_id null")
        XCTAssertEqual(session.timelineClips.count, 2, "live preview clips follow the staged timeline")
        XCTAssertFalse(session.isDirty(.captions), "lanes chat did not touch stay clean")
    }

    func testTimelineMergeKeepsMediaIdentityFromTheEditorSlots() async throws {
        let (session, _) = await loaded(Self.chatSnapshot())
        let s1 = try XCTUnwrap(session.document.clips.first { $0.id == "s1" })
        XCTAssertEqual(s1.raw["source_duration_s"], .number(6), "wire-shaped chat slot must not drop media fields")
        XCTAssertEqual(s1.clipIndex, 0)
    }

    func testStagedEditIsUndoableAndSavedThroughTheNormalCommit() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot())
        await session.save()
        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertNotNil(request.timelineSlots)
        XCTAssertNotNil(request.textElements)
        XCTAssertNotNil(request.mix)
        XCTAssertTrue(request.removeMusic)
        XCTAssertNil(request.captionCues, "only changed lanes are committed")
        XCTAssertEqual(request.baseGeneration, "g1")

        let (other, _) = await loaded(Self.chatSnapshot())
        other.undo()
        XCTAssertFalse(other.hasUnsavedChanges)
        XCTAssertEqual(other.document.clips.compactMap(\.id), ["s1", "s2", "s3"])
    }

    func testStaleGenerationDraftIsIgnored() async throws {
        let (session, _) = await loaded(Self.chatSnapshot(base: "g0"))
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.clips.compactMap(\.id), ["s1", "s2", "s3"])
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
        XCTAssertNotNil(session.document.music)
    }

    func testNoChatDraftLeavesTheEditorClean() async throws {
        let empty = DraftSnapshot(draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e",
            baseJobID: Self.jobID, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now)
        for snapshot in [Self.bootstrapSnapshot(), empty] {
            let (session, _) = await loaded(snapshot)
            XCTAssertFalse(session.hasUnsavedChanges)
            XCTAssertTrue(session.dirtySections.isEmpty)
            XCTAssertEqual(session.document.clips.count, 3)
        }
    }

    func testChatDraftArrivingAfterOpenStagesOnceAndNotAfterUndo() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        XCTAssertFalse(session.hasUnsavedChanges)
        fake.draftSnapshot = Self.chatSnapshot()
        await session.synchronizePromptRevision()
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        session.undo()
        XCTAssertFalse(session.hasUnsavedChanges)
        await session.synchronizePromptRevision()
        XCTAssertFalse(session.hasUnsavedChanges, "same draft revision is not re-applied after the creator undid it")
    }

    func testOverlappingLocalEditKeepsTheConflictAndBothSides() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setTextStyle(id: "t1", style: "Inter")
        XCTAssertEqual(session.dirtySections, [.text])
        let localText = session.document.textElements
        fake.draftSnapshot = Self.chatSnapshot()
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .conflict)
        XCTAssertEqual(session.document.textElements, localText, "local edit not overwritten")
    }

    func testDisjointLocalEditKeepsBothSides() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setClipTiming(clipID: "s1", durationS: 1.2)
        let local = session.document.clips.first { $0.id == "s1" }?.durationS
        fake.draftSnapshot = Self.chatSnapshot(revision: 5, textOnly: true)
        await session.synchronizePromptRevision()
        XCTAssertNotEqual(session.saveState, .conflict)
        XCTAssertEqual(session.dirtySections, [.timeline, .text])
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, local)
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
    }

    func testFlatOnlyAndStaleSectionsDraftsStageLikeNestedOnes() async throws {
        for shape in [Shape.flatOnly, .staleSections] {
            let (session, fake) = await loaded(Self.chatSnapshot(shape: shape))
            XCTAssertEqual(session.dirtySections, [.timeline, .text, .mix, .music], "\(shape)")
            XCTAssertEqual(session.document.clips.compactMap(\.id), ["s3", "s1"])
            XCTAssertEqual(session.document.clips.last?.durationS, 1.5)
            XCTAssertEqual(session.document.tombstones.compactMap(\.id), ["s2"])
            XCTAssertEqual(session.document.textElements.first?.text, "changed")
            XCTAssertEqual(session.document.textElements.first?.endS, 3600)
            XCTAssertEqual(session.document.mix["music_level"], .number(0.3))
            XCTAssertNil(session.document.music)
            XCTAssertEqual(session.timelineClips.count, 2)
            XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.raw["source_duration_s"], .number(6))
            await session.save()
            XCTAssertNotNil(fake.lastRequest?.textElements)
            XCTAssertTrue(fake.lastRequest?.removeMusic ?? false)
            let (other, _) = await loaded(Self.chatSnapshot(shape: shape))
            other.undo()
            XCTAssertFalse(other.hasUnsavedChanges)
        }
    }

    func testFlatOnlyStaleGenerationIgnoredAndArrivalAfterOpenStages() async throws {
        let (stale, _) = await loaded(Self.chatSnapshot(base: "g0", shape: .flatOnly))
        XCTAssertFalse(stale.hasUnsavedChanges)
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        fake.draftSnapshot = Self.chatSnapshot(shape: .flatOnly)
        await session.synchronizePromptRevision()
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
    }

    private func openedProject(_ snapshot: DraftSnapshot, draftError: APIError? = nil) async -> (NativeEditorSession, EditorCommitSpy) {
        let fake = EditorCommitSpy(draftSnapshot: snapshot, draftError: draftError, authoritativeVariant: Self.variant(),
            commitResponse: EditorCommitResponse(ok: true, generation: "g2",
                sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: true, mix: true),
                revisionNumber: 4, revisionHash: "r4", expectedDuration: nil))
        let project = ProjectSummary(id: UUID(), title: "Chat", status: .ready, updatedAt: .now, posterURL: nil,
            outputVariantID: "initial", runtimeVersion: 2, activeJobID: UUID(uuidString: Self.jobID), activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        return (session, fake)
    }

    func testOpenPathStagesAnExistingChatDraftAndSurvivesDraftFetchFailure() async throws {
        let (session, _) = await openedProject(Self.chatSnapshot(shape: .flatOnly))
        XCTAssertEqual(session.dirtySections, [.timeline, .text, .mix, .music])
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertEqual(session.timelineClips.count, 2)
        let (failed, _) = await openedProject(Self.chatSnapshot(shape: .flatOnly), draftError: .conflict)
        XCTAssertEqual(failed.loadState, .loaded)
        XCTAssertFalse(failed.hasUnsavedChanges)
        let (stale, _) = await openedProject(Self.chatSnapshot(base: "g0", shape: .flatOnly))
        XCTAssertFalse(stale.hasUnsavedChanges)
    }

    func testReturningToALoadedEditorStagesTheLatestDraft() async throws {
        let (session, fake) = await openedProject(Self.bootstrapSnapshot())
        XCTAssertFalse(session.hasUnsavedChanges)
        fake.draftSnapshot = Self.chatSnapshot(shape: .flatOnly)
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
    }

    func testChatStagedEditsAloneNeedNoFlushButLocalEditsDo() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot(shape: .flatOnly))
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "send must not commit/render chat-staged edits")
        XCTAssertEqual(fake.commitCount, 0)
        session.setClipTiming(clipID: "s1", durationS: 1.1)
        XCTAssertFalse(session.hasOnlyChatStagedChanges, "a manual edit restores the flush")
        session.undo()
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
    }

    func testNewerCumulativeChatDraftReplacesTheStagedOneWithoutConflict() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot(shape: .flatOnly))
        fake.draftSnapshot = Self.chatSnapshot(revision: 4, textOnly: true, shape: .flatOnly)
        await session.synchronizePromptRevision()
        XCTAssertNotEqual(session.saveState, .conflict)
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        // Same revision again is a no-op that keeps the staged edit.
        await session.synchronizePromptRevision()
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
    }

    func testSaveCommitsChatAndManualLanesThenTheStaleDraftIsIgnored() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot(shape: .flatOnly))
        session.setClipTiming(clipID: "s1", durationS: 1.1)
        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertNotNil(fake.lastRequest?.textElements)
        XCTAssertNotNil(fake.lastRequest?.timelineSlots)
        XCTAssertTrue(fake.lastRequest?.removeMusic ?? false)
        fake.authoritativeVariant = Self.variant(generation: "g2")
        await session.synchronizePromptRevision()
        XCTAssertFalse(session.isDirty(.text) || session.isDirty(.timeline), "draft based on g1 is stale after the commit")
    }

    /// Real-thread shape (80ade387): a re-plan re-rendered as a NEW job.
    func testOpeningAfterAReplanTargetsTheNewJobAndDropsTheOldPlayback() async throws {
        let jobA = UUID(), jobB = UUID(), threadID = UUID()
        func project(_ job: UUID, revision: Int) -> ProjectSummary {
            ProjectSummary(id: threadID, title: "Chat", status: .ready, updatedAt: .now, posterURL: nil,
                outputVariantID: "initial", runtimeVersion: 2, serverRevision: revision,
                activeJobID: job, activePlanItemID: "item")
        }
        var variantA = Self.variant(generation: "gA"); variantA["output_url"] = .string("https://example.com/job-a.mp4")
        var variantB = Self.variant(generation: "gB"); variantB["output_url"] = .string("https://example.com/job-b.mp4")
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), draftError: .conflict, authoritativeVariant: variantA)
        let session = NativeEditorSession(project: project(jobA, revision: 20))
        await session.load(project: project(jobA, revision: 20), api: fake)
        func playing() -> String? { (session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent }
        XCTAssertEqual(playing(), "job-a.mp4")
        XCTAssertFalse(session.needsReload(for: project(jobA, revision: 20)))

        // Same revision but a different active job must still reload.
        XCTAssertTrue(session.needsReload(for: project(jobB, revision: 20)))
        fake.authoritativeVariant = variantB
        await session.load(project: project(jobB, revision: 26), api: fake)
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertEqual(playing(), "job-b.mp4", "old job's video must not stay on screen")
        XCTAssertFalse(session.needsReload(for: project(jobB, revision: 26)))
    }

    private func replanFixture(manualEdit: Bool) async -> (NativeEditorSession, EditorCommitSpy, ProjectSummary, UUID) {
        let jobA = UUID(uuidString: Self.jobID)!, jobB = UUID(), threadID = UUID()
        func project(_ job: UUID, revision: Int) -> ProjectSummary {
            ProjectSummary(id: threadID, title: "Chat", status: .ready, updatedAt: .now, posterURL: nil,
                outputVariantID: "initial", runtimeVersion: 2, serverRevision: revision, activeJobID: job, activePlanItemID: "item")
        }
        var variantA = Self.variant(generation: "g1"); variantA["output_url"] = .string("https://example.com/job-a.mp4")
        let fake = EditorCommitSpy(draftSnapshot: Self.chatSnapshot(shape: .flatOnly), authoritativeVariant: variantA)
        let session = NativeEditorSession(project: project(jobA, revision: 5))
        await session.load(project: project(jobA, revision: 5), api: fake)
        if manualEdit { session.setClipTiming(clipID: "s1", durationS: 1.1) }
        // Re-plan finished as a NEW job with its own generation.
        var variantB = Self.variant(generation: "gNew"); variantB["output_url"] = .string("https://example.com/job-b.mp4")
        fake.authoritativeVariant = variantB
        fake.draftSnapshot = DraftSnapshot(draftID: "n", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e",
            baseJobID: jobB.uuidString, baseGenerationID: "gNew", snapshot: [:], canUndo: false, createdAt: .now)
        return (session, fake, project(jobB, revision: 9), jobB)
    }

    func testReplanWithOnlyChatStagedEditsSwitchesToTheNewJobWithoutCommitting() async throws {
        let (session, fake, newProject, jobB) = await replanFixture(manualEdit: false)
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        await session.adoptLatestJob(newProject, api: fake)
        XCTAssertEqual(fake.commitCount, 0)
        XCTAssertNil(session.newerJobPrompt)
        XCTAssertFalse(session.hasUnsavedChanges, "obsolete staged edits dropped")
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertEqual((session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent, "job-b.mp4")
        // The old draft (or a re-sync) must not re-stage.
        await session.synchronizePromptRevision()
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testReplanWithManualEditsPromptsThenSwitchDiscardsAndKeepEditingStays() async throws {
        let (session, fake, newProject, jobB) = await replanFixture(manualEdit: true)
        await session.adoptLatestJob(newProject, api: fake)
        XCTAssertEqual(session.newerJobPrompt, newProject)
        XCTAssertTrue(session.hasUnsavedChanges, "no silent switch or discard")
        XCTAssertNotEqual(fake.editorVariantJobIDs.last, jobB)
        session.keepEditingCurrentJob()
        XCTAssertNil(session.newerJobPrompt)
        XCTAssertTrue(session.hasUnsavedChanges)
        await session.adoptLatestJob(newProject, api: fake)
        XCTAssertNotNil(session.newerJobPrompt)
        await session.switchToLatestJob(newProject, api: fake)
        XCTAssertNil(session.newerJobPrompt)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertEqual(fake.commitCount, 0)
    }

    private func thread(_ id: UUID, job: UUID, revision: Int) throws -> CreationThread {
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(CreationThread.self, from: Data(#"""
        {"id":"\#(id.uuidString)","title":"Chat","status":"active","revision":\#(revision),"runtime_version":2,
         "active_job_id":"\#(job.uuidString)","active_plan_item_id":"item","state":{"selected_variant_id":"initial"},
         "job":{"id":"\#(job.uuidString)","status":"variants_ready","variants":[{"variant_id":"initial","render_status":"ready"}]},
         "updated_at":"2026-09-30T08:00:00Z"}
        """#.utf8))
    }

    /// Editor open on J1 with chat-staged edits; a re-plan then renders J2.
    private func supersededFixture(manualEdit: Bool) async throws -> (NativeEditorSession, EditorCommitSpy, UUID) {
        let threadID = UUID(), jobA = UUID(uuidString: Self.jobID)!, jobB = UUID()
        func project(_ job: UUID, revision: Int) -> ProjectSummary {
            ProjectSummary(id: threadID, title: "Chat", status: .ready, updatedAt: .now, posterURL: nil, outputVariantID: "initial",
                runtimeVersion: 2, serverRevision: revision, activeJobID: job, activePlanItemID: "item")
        }
        var variantA = Self.variant(generation: "g1"); variantA["output_url"] = .string("https://example.com/job-a.mp4")
        let fake = EditorCommitSpy(draftSnapshot: Self.chatSnapshot(shape: .flatOnly), authoritativeVariant: variantA)
        let session = NativeEditorSession(project: project(jobA, revision: 5))
        await session.load(project: project(jobA, revision: 5), api: fake)
        if manualEdit { session.setClipTiming(clipID: "s1", durationS: 1.1) }
        var variantB = Self.variant(generation: "gNew"); variantB["output_url"] = .string("https://example.com/job-b.mp4")
        fake.authoritativeVariant = variantB
        fake.supersededJobIDs = [jobA]
        fake.refreshedThread = try thread(threadID, job: jobB, revision: 9)
        fake.draftSnapshot = DraftSnapshot(draftID: "n", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e",
            baseJobID: jobA.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now)
        return (session, fake, jobB)
    }

    func testOpenEditorFollowsAReplanWhenTheOldJobIsSuperseded() async throws {
        let (session, fake, jobB) = try await supersededFixture(manualEdit: false)
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        // Old job's routes now 409 "content plan unavailable": adopt J2, no error banner.
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(fake.commitCount, 0)
        XCTAssertEqual((session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent, "job-b.mp4")
        if case .refreshFailed = session.saveState { XCTFail("must not show the refresh-failed banner") }
    }

    func testDraftHeadOnTheNewJobAdoptsItToo() async throws {
        let (session, fake, jobB) = try await supersededFixture(manualEdit: false)
        fake.draftSnapshot = DraftSnapshot(draftID: "n", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e",
            baseJobID: jobB.uuidString, baseGenerationID: "gNew", snapshot: [:], canUndo: false, createdAt: .now)
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testSupersededJobWithManualEditsPromptsInsteadOfSwitching() async throws {
        let (session, fake, jobB) = try await supersededFixture(manualEdit: true)
        await session.synchronizePromptRevision()
        XCTAssertNotNil(session.newerJobPrompt)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertNotEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertEqual(fake.commitCount, 0)
    }
}
