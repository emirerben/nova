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

    func testDurableSnapshotReopensExactLanesWithCurrentCapabilities() {
        let payload: [String: JSONValue] = [
            "base_generation": .string("g1"), "editor_state_version": .number(2),
            "sections": .object([
                "timeline_slots": .array([.object(["slot_id": .string("s1"), "clip_index": .number(0), "in_s": .number(0.9), "duration_s": .number(3)])]),
                "text_elements": .array([.object(["id": .string("road"), "text": .string("Road trip"), "start_s": .number(0), "end_s": .number(3)])])
            ])
        ]
        let snapshot = DraftSnapshot(draftID: "saved", itemID: "item", variantKey: "initial", draftRevision: 4,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "editor_payload": .object(payload)], canUndo: true, createdAt: .now)
        var variant = Self.variant()
        variant["editor_capabilities"] = .object(["timeline": .bool(true)])
        let draft = snapshot.editorDraft(projectID: UUID(), authoritativeVariant: variant)
        let reopened = EditorDocument(snapshot: draft.serverSnapshot)
        XCTAssertEqual(reopened.clips.first?.inS, 0.9)
        XCTAssertEqual(reopened.clips.first?.durationS, 3)
        XCTAssertEqual(reopened.textElements.map(\.text), ["Road trip"])
        XCTAssertEqual(draft.serverSnapshot["editor_capabilities"], variant["editor_capabilities"])
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

    // KRI-772: a chat draft staged before the server fitted windows to the footage (Troya: 3 s asked of a
    // 3.36 s clip that starts at 0.9 s) opens repaired, so Save is not refused for a window the creator
    // never typed.
    func testStagedDraftWindowLongerThanItsFootageKeepsAuthoredIntent() async throws {
        var payload = Self.chatPayload(shape: .flatOnly)
        // Base s1 has 6 s of footage. 4 s in + 3 s long needs 7 s.
        payload["timeline_slots"] = .array([.object([
            "slot_id": .string("s1"), "clip_index": .number(0), "in_s": .number(4),
            "duration_s": .number(3), "removed": .bool(false),
        ])])
        let snapshot = DraftSnapshot(draftID: "overlong", itemID: "item", variantKey: "initial", draftRevision: 4,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "editor_payload": .object(payload)], canUndo: true, createdAt: .now)
        let (session, _) = await loaded(snapshot)
        let s1 = try XCTUnwrap(session.document.clips.first { $0.id == "s1" })
        XCTAssertEqual(s1.inS, 4, accuracy: 1e-9)
        XCTAssertEqual(s1.durationS ?? 0, 3, accuracy: 1e-6, "Render mapping must not rewrite the draft")
    }

    func testFittedToFootageLeavesFittingAndUnmeasuredWindowsAlone() {
        let fits: [String: JSONValue] = ["in_s": .number(1), "duration_s": .number(2), "source_duration_s": .number(6)]
        XCTAssertEqual(DraftSnapshot.fittedToFootage(fits), fits)
        let unmeasured: [String: JSONValue] = ["in_s": .number(1), "duration_s": .number(20)]
        XCTAssertEqual(DraftSnapshot.fittedToFootage(unmeasured), unmeasured)
        let twice: [String: JSONValue] = ["in_s": .number(1), "duration_s": .number(3), "playback_rate": .number(2), "source_duration_s": .number(3.36)]
        let fitted = DraftSnapshot.fittedToFootage(twice)
        XCTAssertEqual(fitted["duration_s"]?.numberValue ?? 0, 1.18, accuracy: 1e-6)
        let late: [String: JSONValue] = ["in_s": .number(3.3), "duration_s": .number(1), "source_duration_s": .number(3.36)]
        let slid = DraftSnapshot.fittedToFootage(late)
        XCTAssertLessThanOrEqual((slid["in_s"]?.numberValue ?? 9) + (slid["duration_s"]?.numberValue ?? 9), 3.36 + 1e-9)
        XCTAssertGreaterThanOrEqual(slid["duration_s"]?.numberValue ?? 0, 0.1)
    }

    // KRI-773: Troya's restored car clip carried a chat-minted `cs-<state>-<n>` id from a draft stored
    // before #1698. A draft clip that matches no saved clip is a new placement, so the editor must not
    // hold (or send) that id: the server refuses it as a stale slot, and Reload re-applies it.
    func testStagedDraftClipWithAnUnknownIdIsANewPlacement() async throws {
        var payload = Self.chatPayload(shape: .flatOnly)
        payload["timeline_slots"] = .array([.object([
            "slot_id": .string("cs-369BA950-52A-27"), "clip_index": .number(3), "in_s": .number(0),
            "duration_s": .number(3), "removed": .bool(false),
        ])])
        let snapshot = DraftSnapshot(draftID: "minted", itemID: "item", variantKey: "initial", draftRevision: 4,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "editor_payload": .object(payload)], canUndo: true, createdAt: .now)
        let (session, _) = await loaded(snapshot)
        let added = try XCTUnwrap(session.document.clips.first { $0.clipIndex == 3 })
        XCTAssertNil(added.id, "a made-up slot id must not reach the Save")
        XCTAssertFalse(session.document.clips.contains { $0.id?.hasPrefix("cs-") == true })
        // Saved clips keep their real ids.
        XCTAssertTrue(session.document.clips.contains { $0.id == "s1" })
    }

    // New source placements have no server slot ID until Save. They must survive
    // draft hydration, the live timeline, and the next client-state/commit round trip.
    func testRestoredUnusedSourceWithoutSlotIDSurvivesStagingAndSave() async throws {
        for explicitNull in [false, true] {
            var payload = Self.chatPayload(shape: .flatOnly)
            var restored: [String: JSONValue] = [
                "clip_index": .number(3), "in_s": .number(0),
                "duration_s": .number(3), "removed": .bool(false),
                "transition_after": .string("cut"), "look_preset": .string("none"),
            ]
            if explicitNull { restored["slot_id"] = .null }
            payload["timeline_slots"] = .array([.object(restored)])
            let snapshot = DraftSnapshot(draftID: "restored", itemID: "item", variantKey: "initial", draftRevision: 4,
                snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: "g1",
                snapshot: ["kind": .string("editor"), "editor_payload": .object(payload)], canUndo: true, createdAt: .now)
            let (session, fake) = await loaded(snapshot)
            XCTAssertEqual(session.document.clips.map(\.clipIndex), [3, 0, 1, 2])
            XCTAssertEqual(session.timelineClips.count, 4)
            XCTAssertTrue(session.document.opaqueRecords.values.allSatisfy(\.isEmpty), "valid restored clips must not block native preview as opaque content")
            let added = try XCTUnwrap(session.document.clips.first { $0.clipIndex == 3 })
            XCTAssertNil(added.id, "inventing an ID would fail the server's stale-slot validation")
            XCTAssertEqual(added.durationS, 3)
            let nextMessage = try XCTUnwrap(session.exportEditorState())
            XCTAssertEqual(nextMessage.lanes.timelineSlots?.count, 4, "the next chat turn must retain the restored clip")
            await session.save()
            let rows = try XCTUnwrap(fake.lastRequest?.timelineSlots)
            XCTAssertEqual(rows.count, 4)
            guard case let .object(first) = rows[0] else { return XCTFail("Missing restored source") }
            XCTAssertEqual(first["clip_index"], .number(3))
            XCTAssertTrue(first["slot_id"] == nil || first["slot_id"] == .null)
            let (undoable, _) = await loaded(snapshot)
            undoable.undo()
            XCTAssertEqual(undoable.document.clips.map(\.clipIndex), [0, 1, 2])
        }
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

    /// KRI-935: the same lane edited locally and by chat. The chat edit is the newest and the creator asked for
    /// it, so it applies without a prompt; one Undo restores the creator's own version of the lane.
    func testOverlappingLocalEditTakesTheChatEditWithOneUndoBack() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setTextStyle(id: "t1", style: "Inter")
        XCTAssertEqual(session.dirtySections, [.text])
        let localText = session.document.textElements
        fake.draftSnapshot = Self.chatSnapshot()
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .idle)
        XCTAssertEqual(session.document.textElements.first?.text, "changed", "the chat edit is shown")
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.textElements, localText, "one Undo brings back the local edit")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements, localText, "the undone chat draft is not re-applied")
    }

    func testDisjointLocalEditKeepsBothSides() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setClipTiming(clipID: "s1", durationS: 1.2)
        let local = session.document.clips.first { $0.id == "s1" }?.durationS
        fake.draftSnapshot = Self.chatSnapshot(revision: 5, textOnly: true)
        await session.synchronizePromptRevision()
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

    // MARK: KRI-529 — reopening after a chat turn, and sending the editor's state

    private func threadProject(_ id: UUID, revision: Int, job: UUID? = UUID(uuidString: ChatDraftStagingTests.jobID),
                               runtimeVersion: Int = 2) -> ProjectSummary {
        ProjectSummary(id: id, title: "Chat", status: .ready, updatedAt: .now, posterURL: nil,
            outputVariantID: "initial", runtimeVersion: runtimeVersion, serverRevision: revision,
            activeJobID: job, activePlanItemID: "item")
    }

    /// Every chat turn bumps the thread revision. That used to force a full reload on each
    /// reopen (loading state, the OLD render, the chat edit seconds later, local edits reset).
    func testReopeningAfterAChatTurnReconcilesInPlaceAndKeepsManualEdits() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        await session.load(project: threadProject(id, revision: 5), api: fake)
        session.setClipTiming(clipID: "s1", durationS: 1.1)  // the creator's own unsaved edit
        let item = session.player?.currentItem
        XCTAssertTrue(session.needsReload(for: threadProject(id, revision: 7)), "the bump that forced the full load")

        fake.draftSnapshot = Self.chatSnapshot(textOnly: true, shape: .flatOnly)  // a lane the creator did not touch
        let handled = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)

        XCTAssertTrue(handled)
        XCTAssertEqual(session.loadState, .loaded)
        XCTAssertTrue(session.player?.currentItem === item, "the player is not reinstalled")
        XCTAssertEqual(session.document.textElements.first?.text, "changed", "the chat edit is staged at once")
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, 1.1, "the manual edit survives")
        XCTAssertFalse(session.needsReload(for: threadProject(id, revision: 7)))
    }

    func testReopenFallsBackToTheFullReloadWhenTheRenderOrTheJobChanged() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        await session.load(project: threadProject(id, revision: 5), api: fake)

        let otherJob = await session.reconcileOnOpen(project: threadProject(id, revision: 7, job: UUID()), api: fake)
        XCTAssertFalse(otherJob, "a re-plan job needs its own video")

        let legacy = await session.reconcileOnOpen(project: threadProject(id, revision: 7, runtimeVersion: 1), api: fake)
        XCTAssertFalse(legacy)

        fake.editorVariantError = .conflict
        let unreadable = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
        XCTAssertFalse(unreadable, "an unreadable variant keeps the full reload")

        fake.editorVariantError = nil
        fake.authoritativeVariant = Self.variant(generation: "g2")  // a chat-driven render finished while closed
        let newerRender = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
        XCTAssertFalse(newerRender, "a newer render must swap the video")
        XCTAssertTrue(session.needsReload(for: threadProject(id, revision: 7)))
    }

    /// The server treats an exported state as authoritative and ignores its own head draft, so
    /// a state exported before the previous chat edit was staged would silently drop that edit.
    func testExportedStateCarriesThePreviousChatEditBecauseItIsStagedFirst() async throws {
        let (session, fake) = await openedProject(Self.bootstrapSnapshot())
        fake.draftSnapshot = Self.chatSnapshot(textOnly: true, shape: .flatOnly)  // landed, not staged yet
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
        XCTAssertNil(session.exportEditorState()?.lanes.textElements, "without the sync the chat edit is missing")

        let exported = await session.exportEditorStateSyncingDraft()
        let state = try XCTUnwrap(exported)
        XCTAssertNotNil(state.lanes.textElements, "the previous chat edit rides along")
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertEqual(fake.commitCount, 0, "exporting never commits or renders")
    }

    func testExportedStateStillGoesOutWhenTheDraftCannotBeFetched() async throws {
        let (session, fake) = await openedProject(Self.bootstrapSnapshot())
        fake.draftError = .conflict
        let state = await session.exportEditorStateSyncingDraft()
        XCTAssertNotNil(state, "a failed sync never blocks the send")
    }

    // MARK: KRI-535 — first open, and a render that changed under unsaved edits

    /// Opens the editor while the source preview is held in `.preparing`, the window in which
    /// the OLD rendered video used to be shown as if it were current.
    private func openedWhilePreparing(_ snapshot: DraftSnapshot) async throws -> (NativeEditorSession, EditorCommitSpy, Task<Void, Never>) {
        let fake = EditorCommitSpy(draftSnapshot: snapshot, authoritativeVariant: Self.variant())
        fake.suspendNextSourcePool = true
        let project = threadProject(UUID(), revision: 5)
        let session = NativeEditorSession(project: project)
        let loading = Task { await session.load(project: project, api: fake) }
        for _ in 0..<200 where !fake.sourcePoolIsSuspended {
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(fake.sourcePoolIsSuspended, "the source-pool gate did not suspend in time")
        return (session, fake, loading)
    }

    func testFirstOpenHidesTheOldRenderWhileAStagedChatEditPrepares() async throws {
        let (session, fake, loading) = try await openedWhilePreparing(Self.chatSnapshot(shape: .flatOnly))
        XCTAssertEqual(session.sourcePreviewState, .preparing)
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "the AI edit is already staged in the document")
        XCTAssertNotNil(session.player, "the old item may stay installed, but must remain hidden")
        XCTAssertFalse(session.canDisplayCurrentPlayer, "the old render predates the AI edit: never show it as current")
        fake.resumeSourcePool()
        await loading.value
    }

    func testFirstOpenWithNothingStagedStillShowsTheCurrentRender() async throws {
        // A draft built on another render is dropped, so the rendered video IS current.
        let (session, fake, loading) = try await openedWhilePreparing(Self.chatSnapshot(base: "g0", shape: .flatOnly))
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.canDisplayCurrentPlayer)
        fake.resumeSourcePool()
        await loading.value
    }

    func testFirstOpenHidesTheObsoleteRenderWhenTheStagedEditCannotBePreviewed() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.chatSnapshot(shape: .flatOnly), authoritativeVariant: Self.variant())
        fake.sourcePoolResult = nil  // the source pool is unavailable, so the local preview fails
        let project = threadProject(UUID(), revision: 5)
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        XCTAssertTrue(session.sourcePreviewState.isFailure)
        XCTAssertFalse(session.isShowingRenderedFallback)
        XCTAssertFalse(session.canDisplayCurrentPlayer, "an old render cannot represent the staged chat edit")
        XCTAssertFalse(session.canDownloadCurrentVideo, "the obsolete render must not export as current")
        XCTAssertTrue(session.hasUnsavedChanges, "the staged AI edit is still there to save")
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
    }

    func testReopenAsksBeforeReloadingOverANewerRenderWithManualEdits() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        await session.load(project: threadProject(id, revision: 5), api: fake)
        session.setClipTiming(clipID: "s1", durationS: 1.1)  // the creator's own unsaved edit
        let variantFetches = fake.editorVariantJobIDs.count
        fake.authoritativeVariant = Self.variant(generation: "g2")  // a newer render of the same job

        let handled = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)

        XCTAssertTrue(handled, "no silent reload")
        XCTAssertEqual(session.newerJobPrompt?.serverRevision, 7)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, 1.1)
        XCTAssertEqual(fake.editorVariantJobIDs.count, variantFetches + 1, "only the check ran, not a reload")
        XCTAssertTrue(session.needsReload(for: threadProject(id, revision: 7)), "the next open asks again")

        session.keepEditingCurrentJob()
        XCTAssertNil(session.newerJobPrompt)
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, 1.1, "keep editing keeps the work")
    }

    func testSwitchingToTheNewerRenderDiscardsOnlyBecauseTheCreatorChose() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        await session.load(project: threadProject(id, revision: 5), api: fake)
        session.setClipTiming(clipID: "s1", durationS: 1.1)
        fake.authoritativeVariant = Self.variant(generation: "g2")
        _ = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
        let prompt = try XCTUnwrap(session.newerJobPrompt)

        await session.switchToLatestJob(prompt, api: fake)

        XCTAssertNil(session.newerJobPrompt)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertFalse(session.needsReload(for: threadProject(id, revision: 7)))
    }

    func testReopenOverANewerRenderReloadsWithoutAskingWhenNothingOfTheirsIsAtStake() async throws {
        let id = UUID()
        // Clean editor, and an editor holding only the AI's staged edit (the render replaces it).
        for snapshot in [Self.bootstrapSnapshot(), Self.chatSnapshot(shape: .flatOnly)] {
            let fake = EditorCommitSpy(draftSnapshot: snapshot, authoritativeVariant: Self.variant())
            let session = NativeEditorSession(project: threadProject(id, revision: 5))
            await session.load(project: threadProject(id, revision: 5), api: fake)
            fake.authoritativeVariant = Self.variant(generation: "g2")
            let handled = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
            XCTAssertFalse(handled)
            XCTAssertNil(session.newerJobPrompt)
        }
    }

    func testReopenKeepsManualEditsThroughADroppedConnectionButNotThroughASupersededJob() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        await session.load(project: threadProject(id, revision: 5), api: fake)
        session.setClipTiming(clipID: "s1", durationS: 1.1)

        fake.editorVariantError = .offline
        let offline = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
        XCTAssertTrue(offline, "a dropped connection must not wipe the creator's edits or error the editor")
        XCTAssertEqual(session.loadState, .loaded)
        XCTAssertTrue(session.hasUnsavedChanges)

        for superseded in [APIError.contentPlanUnavailable, APIError.conflict] {
            fake.editorVariantError = superseded
            let handled = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
            XCTAssertFalse(handled, "a superseded job takes the full path that follows the thread")
        }
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
        XCTAssertEqual(session.saveState, .idle, "following a re-plan is not a refresh failure")
        XCTAssertEqual(session.refreshSync, .idle)
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

    private static func textDraft(revision: Int, texts: [(String, String)], jobID: String = ChatDraftStagingTests.jobID) -> DraftSnapshot {
        let rows: [JSONValue] = texts.map { id, text in
            .object(["id": .string(id), "text": .string(text), "start_s": .number(0), "end_s": .number(2),
                     "role": .string("generative_intro"), "x_frac": .number(0.5), "y_frac": .number(0.2), "color": .null])
        }
        return DraftSnapshot(draftID: "d\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision, snapshotHash: "h",
            etag: "e", baseJobID: jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "editor_payload": .object([
                "base_generation": .string("g1"), "text_elements": .array(rows)])], canUndo: true, createdAt: .now)
    }

    /// Real sequence (thread 8f208e18): rev A edits labels, rev B (cumulative) changes the title.
    func testCumulativeTitleChangeReplacesTheStagedLabelsAndUpdatesTheDocument() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        fake.draftSnapshot = Self.textDraft(revision: 2, texts: [("t1", "hello A")])
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "hello A")
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        fake.draftSnapshot = Self.textDraft(revision: 3, texts: [("t1", "Ahmet Wedding Vlog")])
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "Ahmet Wedding Vlog")
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        // A variant re-fetch with the same generation must not drop the staged edit.
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "Ahmet Wedding Vlog")
    }

    // MARK: - Editor-state turns (the user never saves mid-flow)

    /// KRI-524: a clip trim and twelve explicit word windows must arrive and save
    /// together, even when the draft retains older nested section values.
    func testCompoundOpeningTrimStagesAndSavesExactWordWindows() async throws {
        var variant = Self.variant()
        let words = "Join us for our favorite bakery and tea shop near the harbor".split(separator: " ")
        func texts(duration: Double) -> [JSONValue] {
            words.enumerated().map { index, word in
                .object(["id": .string("word-\(index)"), "text": .string(String(word)),
                         "start_s": .number(Double(index) * duration / 12),
                         "end_s": .number(Double(index + 1) * duration / 12),
                         "role": .string("generative_sequence"), "effect": .string("fade"),
                         "font_family": .string("Inter"), "font_size_px": .number(72),
                         "x_frac": .number(0.3), "y_frac": .number(0.7)])
            }
        }
        func slots(firstDuration: Double) -> [JSONValue] {
            (0..<3).map { index in
                .object(["slot_id": .string("s\(index + 1)"), "clip_index": .number(Double(index)),
                         "in_s": .number(0), "duration_s": .number(index == 0 ? firstDuration : 2),
                         "source_duration_s": .number(6), "removed": .bool(false)])
            }
        }
        let oldWords = texts(duration: 4.633333)
        variant["duration_s"] = .number(8.633333)
        variant["user_timeline"] = .object(["slots": .array(slots(firstDuration: 4.633333))])
        variant["text_elements"] = .array(oldWords)
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: variant,
            commitResponse: EditorCommitResponse(ok: true, generation: "g2",
                sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: true, mix: false),
                revisionNumber: 4, revisionHash: "r4", expectedDuration: nil))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        let submitted = try XCTUnwrap(session.exportEditorState())
        let newWords = texts(duration: 2)
        let snapshot = DraftSnapshot(draftID: "trim", itemID: "item", variantKey: "initial", draftRevision: 6,
            snapshotHash: "trim", etag: "trim", baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "editor_payload": .object([
                "base_generation": .string("g1"),
                "timeline_slots": .array(slots(firstDuration: 2)), "text_elements": .array(newWords),
                "sections": .object(["timeline_slots": .array(slots(firstDuration: 4.633333)),
                                     "text_elements": .array(oldWords)])])], canUndo: true, createdAt: .now)
        fake.draftSnapshot = Self.echoing(submitted.clientStateID, snapshot)
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.clips.map(\.durationS), [2, 2, 2])
        XCTAssertEqual(session.document.textElements.count, 12)
        XCTAssertEqual(session.dirtySections, [.timeline, .text])
        // A repeated refresh must not restore the older nested sections.
        await session.synchronizePromptRevision()
        await session.save()
        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertEqual(fake.commitCount, 1)
        let savedSlots = try XCTUnwrap(request.timelineSlots)
        XCTAssertEqual(savedSlots.count, 3)
        for (index, value) in savedSlots.enumerated() {
            guard case let .object(row) = value else { return XCTFail("clip row missing") }
            XCTAssertEqual(row["slot_id"], .string("s\(index + 1)"))
            XCTAssertEqual(row["duration_s"], .number(2))
            XCTAssertEqual(row["in_s"], .number(0))
            XCTAssertEqual(row["source_duration_s"], .number(6))
        }
        let savedWords = try XCTUnwrap(request.textElements)
        for (index, value) in savedWords.enumerated() {
            guard case let .object(row) = value, case let .object(expected) = newWords[index] else {
                return XCTFail("word must remain an individual text row")
            }
            for key in ["id", "text", "start_s", "end_s", "effect", "font_family", "font_size_px", "x_frac", "y_frac"] {
                XCTAssertEqual(row[key], expected[key], "word \(index), \(key)")
            }
        }
    }

    private static func echoing(_ stateID: String, _ snapshot: DraftSnapshot) -> DraftSnapshot {
        var value = snapshot.snapshot
        value["client_state_id"] = .string(stateID)
        return DraftSnapshot(draftID: snapshot.draftID, itemID: snapshot.itemID, variantKey: snapshot.variantKey,
            draftRevision: snapshot.draftRevision, snapshotHash: snapshot.snapshotHash, etag: snapshot.etag,
            baseJobID: snapshot.baseJobID, baseGenerationID: snapshot.baseGenerationID, snapshot: value,
            canUndo: snapshot.canUndo, createdAt: snapshot.createdAt)
    }

    private func laneJSON(_ request: EditorStateRequest) throws -> [String: Any] {
        let data = try JSONEncoder().encode(request)
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(root["lanes"] as? [String: Any])
    }

    func testExportOfACleanEditorIsAnEmptyLanesObject() async throws {
        let (session, _) = await loaded(Self.bootstrapSnapshot())
        let state = try XCTUnwrap(session.exportEditorState())
        XCTAssertEqual(state.version, 1)
        XCTAssertEqual(state.baseGeneration, "g1")
        XCTAssertFalse(state.clientStateID.isEmpty)
        let lanes = try laneJSON(state)
        XCTAssertEqual(
            Set(lanes.keys), ["remove_music", "base_generation", "editor_state_version"],
            "A clean editor exports request metadata without any changed lanes"
        )
        XCTAssertEqual(lanes["editor_state_version"] as? Int, 2)
    }

    func testExportCarriesOnlyChangedLanesAndNeverSaveOnlyFields() async throws {
        let (session, _) = await loaded(Self.bootstrapSnapshot())
        session.setClipTiming(clipID: "s1", durationS: 1.2)
        let state = try XCTUnwrap(session.exportEditorState())
        XCTAssertNotNil(state.lanes.timelineSlots)
        XCTAssertNil(state.lanes.textElements)
        XCTAssertNil(state.lanes.mix)
        let lanes = try laneJSON(state)
        for key in ["guided_revision_number", "guided_revision", "copilot_receipt_ids", "accepted_suggestion_ids", "retry_guided_revision"] {
            XCTAssertNil(lanes[key], key)
        }
    }

    func testExportIncludesStagedChatLanesBecauseTheyAreUnsavedToo() async throws {
        let (session, _) = await loaded(Self.chatSnapshot(shape: .flatOnly))
        let state = try XCTUnwrap(session.exportEditorState())
        XCTAssertNotNil(state.lanes.timelineSlots)
        XCTAssertNotNil(state.lanes.textElements)
        XCTAssertNotNil(state.lanes.mix)
        XCTAssertTrue(state.lanes.removeMusic)
        XCTAssertNil(state.lanes.captionCues)
        let lanes = try laneJSON(state)
        XCTAssertNil(lanes["guided_revision_number"])
    }

    func testExportFlushesAnInProgressTextFieldAndHonoursTheByteCap() async throws {
        let (session, _) = await loaded(Self.bootstrapSnapshot())
        session.beginTextCreation()
        session.updatePendingText("typing…")
        let state = try XCTUnwrap(session.exportEditorState())
        XCTAssertNil(session.pendingText)
        XCTAssertTrue(session.document.textElements.contains { $0.text == "typing…" })
        XCTAssertNotNil(state.lanes.textElements)
        XCTAssertNil(session.exportEditorState(maxBytes: 10), "over the cap falls back to the legacy flush")
    }

    /// Capability-on twin of `testChatStagedEditsAloneNeedNoFlushButLocalEditsDo`: even real local
    /// edits are exported, never committed (no save, no render) before the turn.
    func testExportingLocalEditsNeverCommitsOrRenders() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot(shape: .flatOnly))
        session.setClipTiming(clipID: "s1", durationS: 1.1)
        XCTAssertFalse(session.hasOnlyChatStagedChanges, "legacy path would have flushed here")
        let state = try XCTUnwrap(session.exportEditorState())
        XCTAssertNotNil(state.lanes.timelineSlots)
        XCTAssertEqual(fake.commitCount, 0)
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testUnloadedEditorCannotExport() async throws {
        XCTAssertNil(NativeEditorSession().exportEditorState())
    }

    /// The bug: a second cumulative chat draft left Undo on chat #1 and Save disabled.
    func testSecondChatDraftUndoReturnsToThePreChatUnsavedState() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let first = try XCTUnwrap(session.exportEditorState())
        fake.draftSnapshot = Self.echoing(first.clientStateID, Self.textDraft(revision: 2, texts: [("t1", "hello A")]))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "hello A")
        XCTAssertTrue(session.hasUnsavedChanges)

        let second = try XCTUnwrap(session.exportEditorState())
        XCTAssertNotNil(second.lanes.textElements, "chat #1's staged lane is unsaved and is sent")
        fake.draftSnapshot = Self.echoing(second.clientStateID, Self.textDraft(revision: 3, texts: [("t1", "Final")]))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "Final")

        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "hello A", "one Undo = the state sent with the turn")
        XCTAssertTrue(session.hasUnsavedChanges, "still unsaved against the rendered baseline (Save stays enabled)")
        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testEditDuringTurnOnADisjointLaneKeepsBothSides() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let sent = try XCTUnwrap(session.exportEditorState())
        session.setClipTiming(clipID: "s1", durationS: 1.2)
        let local = session.document.clips.first { $0.id == "s1" }?.durationS
        fake.draftSnapshot = Self.echoing(sent.clientStateID, Self.textDraft(revision: 2, texts: [("t1", "from chat")]))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "from chat")
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, local)
        XCTAssertEqual(session.dirtySections, [.timeline, .text])
        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, local, "undo restores the state at apply time")
    }

    /// KRI-935: an edit made while the turn ran, on the lane the chat also changed. The chat's reply applies
    /// without a prompt, and one Undo returns to exactly what the creator had before it arrived.
    func testEditDuringTurnOnTheSameLaneTakesTheChatEditWithOneUndoBack() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let sent = try XCTUnwrap(session.exportEditorState())
        session.setTextStyle(id: "t1", style: "Inter")
        let local = session.document.textElements
        fake.draftSnapshot = Self.echoing(sent.clientStateID, Self.textDraft(revision: 2, texts: [("t1", "from chat")]))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .idle)
        XCTAssertEqual(session.document.textElements.first?.text, "from chat")
        session.undo()
        XCTAssertEqual(session.document.textElements, local, "one Undo restores the creator's pre-chat state")
    }

    func testDraftWithAnUnknownStateIDFallsBackToTheLegacyHeadLogic() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        fake.draftSnapshot = Self.echoing("not-ours", Self.chatSnapshot())
        await session.synchronizePromptRevision()
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "legacy staging path")
    }

    func testStaleGenerationDraftWithAStateIDIsStillIgnored() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let sent = try XCTUnwrap(session.exportEditorState())
        fake.draftSnapshot = Self.echoing(sent.clientStateID, Self.chatSnapshot(base: "g0"))
        await session.synchronizePromptRevision()
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
    }

    /// Incident replay: title moved + label text + label start delayed (all unsaved) must survive a
    /// chat turn that only shortens the timeline, and one Undo must restore the pre-chat state.
    func testIncidentReplayChatBuiltOnUnsavedEditsKeepsAllOfThemAndUndoRestoresThem() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setTextPosition(id: "t1", x: 0.3, y: 0.7)
        session.updateTextContent(id: "t1", content: "Label edited")
        session.updateTextTiming(id: "t1", startS: 0.5)
        let before = session.document
        XCTAssertEqual(session.dirtySections, [.text])
        let sent = try XCTUnwrap(session.exportEditorState())
        let sentText = try XCTUnwrap(sent.lanes.textElements)

        let shortened: [JSONValue] = [
            .object(["slot_id": .string("s1"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(1), "removed": .bool(false), "transition_after": .string("cut")]),
            .object(["slot_id": .string("s2"), "clip_index": .number(1), "in_s": .number(0), "duration_s": .number(2), "removed": .bool(true), "transition_after": .string("cut")]),
            .object(["slot_id": .string("s3"), "clip_index": .number(2), "in_s": .number(0), "duration_s": .number(2), "removed": .bool(true), "transition_after": .string("cut")]),
        ]
        let draft = DraftSnapshot(draftID: "d2", itemID: "item", variantKey: "initial", draftRevision: 2, snapshotHash: "h", etag: "e",
            baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "client_state_id": .string(sent.clientStateID),
                       "editor_payload": .object(["base_generation": .string("g1"), "text_elements": .array(sentText),
                                                  "timeline_slots": .array(shortened)])], canUndo: true, createdAt: .now)
        fake.draftSnapshot = draft
        await session.synchronizePromptRevision()

        let text = try XCTUnwrap(session.document.textElements.first)
        XCTAssertEqual(text.text, "Label edited")
        XCTAssertEqual(text.startS, 0.5)
        XCTAssertEqual(text.raw["x_frac"], .number(0.3))
        XCTAssertEqual(text.raw["y_frac"], .number(0.7))
        XCTAssertEqual(session.document.clips.count, 1, "chat's shortened timeline applied")
        XCTAssertEqual(session.document.clips.first?.durationS, 1)

        session.undo()
        XCTAssertEqual(session.document.textElements, before.textElements)
        XCTAssertEqual(session.document.clips, before.clips)
        XCTAssertTrue(session.hasUnsavedChanges, "the creator's own edits are still unsaved")
        XCTAssertEqual(session.dirtySections, [.text])
    }
}

/// Killer screen S2 ("Couldn't refresh this edit") and the editor's convergence paths. Each test
/// drives the real session against the API double with the failure shape seen in prod.
extension ChatDraftStagingTests {
    private func settle(timeout: Duration = .seconds(5), _ condition: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return condition()
    }

    private static func conflict(_ code: String) -> APIError {
        .conflict(detail: ConflictDetail("problem", code: code))
    }

    private static func notFound(_ code: String) -> APIError {
        .requestFailed(status: 404, detail: RequestFailureDetail(nil, code: code))
    }

    // MARK: S2-a / S2-b: temporary failures wait and recover on their own

    func testOfflineRefreshRetriesOnItsOwnAndLandsTheChatEdit() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.refreshRetryDelays = [.milliseconds(20)]
        fake.draftError = .offline
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .idle, "offline is not a save problem")
        XCTAssertEqual(session.refreshSync, .idle, "one failure shows nothing")
        // The chat edit lands while the phone is offline; nothing else triggers another sync.
        fake.draftSnapshot = Self.textDraft(revision: 2, texts: [("t1", "from chat")])
        fake.draftError = nil
        let landed = await settle { session.document.textElements.first?.text == "from chat" }
        XCTAssertTrue(landed, "the retry must run without new chat events")
        XCTAssertEqual(session.refreshSync, .idle)
    }

    func testServerRetryAfterIsHonouredBeforeTheNextTry() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.refreshRetryDelays = [.milliseconds(10)]
        fake.draftError = .requestFailed(status: 429, detail: RequestFailureDetail(nil, retryAfter: 1))
        await session.synchronizePromptRevision()
        let calls = fake.draftCallCount
        fake.draftError = nil
        try await Task.sleep(for: .milliseconds(400))
        XCTAssertEqual(fake.draftCallCount, calls, "Retry-After: 1 must hold the retry back")
        let retried = await settle { fake.draftCallCount > calls }
        XCTAssertTrue(retried)
    }

    func testReconnectRunsAPendingRetryImmediately() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.refreshRetryDelays = [.seconds(3600)]
        fake.draftError = .offline
        for _ in 0..<NativeEditorSession.reconnectingHintAfterFailures { await session.synchronizePromptRevision() }
        XCTAssertEqual(session.refreshSync, .reconnecting)
        fake.draftSnapshot = Self.textDraft(revision: 2, texts: [("t1", "from chat")])
        fake.draftError = nil
        await session.resumeRefreshSync()
        XCTAssertEqual(session.document.textElements.first?.text, "from chat")
        XCTAssertEqual(session.refreshSync, .idle, "any good read clears the hint")
    }

    // MARK: S2-h: sign-in and update are app-wide gates

    func testSignedOutOrUpdateRequiredLeavesTheEditorAloneAndDoesNotRetry() async throws {
        for gate in [APIError.sessionExpired, .nativeUpdateRequired] {
            let (session, fake) = await loaded(Self.bootstrapSnapshot())
            session.refreshRetryDelays = [.milliseconds(10)]
            fake.draftError = gate
            await session.synchronizePromptRevision()
            let calls = fake.draftCallCount
            try await Task.sleep(for: .milliseconds(150))
            XCTAssertEqual(fake.draftCallCount, calls, "\(gate): retrying cannot help, the app gate owns it")
            XCTAssertEqual(session.saveState, .idle)
            XCTAssertEqual(session.refreshSync, .idle)
        }
    }

    // MARK: S2-e: the edit target is gone

    func testProjectResetOrDeletedShowsHonestCopyOnceAndStopsSyncing() async throws {
        for lost in [Self.conflict("draft_target_missing"), Self.notFound("thread_not_found"), Self.notFound("thread_deleted")] {
            let (session, fake) = await loaded(Self.bootstrapSnapshot())
            session.refreshRetryDelays = [.milliseconds(10)]
            fake.draftError = lost
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.refreshSync, .targetLost(NativeEditorSession.targetLostMessage))
            XCTAssertEqual(session.saveState, .idle)
            let calls = fake.draftCallCount
            await session.synchronizePromptRevision()
            try await Task.sleep(for: .milliseconds(80))
            XCTAssertEqual(fake.draftCallCount, calls, "\(lost): no repeating banner, no repeating reads")
        }
    }

    func testChatRuntimeSwitchedOffKeepsTheEditorWorkingSilently() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        fake.draftError = Self.notFound("kria_runtime_unavailable")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.refreshSync, .idle)
        XCTAssertEqual(session.saveState, .idle)
        let calls = fake.draftCallCount
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.draftCallCount, calls)
    }

    // MARK: S2-c: read-path conflicts never say "changed elsewhere"

    func testPersistentReadConflictRetriesBrieflyThenStaysQuiet() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.refreshRetryDelays = [.milliseconds(10)]
        fake.draftError = Self.conflict("draft_variant_ambiguous")
        await session.synchronizePromptRevision()
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(fake.draftCallCount, 2 + NativeEditorSession.readConflictRetryLimit, "load + first sync + bounded retries")
        XCTAssertEqual(session.saveState, .idle)
        XCTAssertEqual(session.refreshSync, .idle)
    }

    /// The draft read succeeds but the variant read keeps conflicting (a guided timeline out of
    /// step with its baseline, or a vanished content plan): the half-good read must not reset
    /// the retry streak, or the editor re-reads every ~1.5 s for the life of the session.
    func testPersistentVariantConflictAfterAGoodDraftReadStaysBounded() async throws {
        for conflict in [APIError.conflict, .contentPlanUnavailable] {
            let (session, fake) = await loaded(Self.bootstrapSnapshot())
            session.refreshRetryDelays = [.milliseconds(10)]
            let before = fake.editorVariantJobIDs.count
            fake.editorVariantError = conflict
            await session.synchronizePromptRevision()
            try await Task.sleep(for: .milliseconds(300))
            let reads = fake.editorVariantJobIDs.count - before
            XCTAssertEqual(reads, 1 + NativeEditorSession.readConflictRetryLimit, "\(conflict): first sync + bounded retries")
            try await Task.sleep(for: .milliseconds(150))
            XCTAssertEqual(fake.editorVariantJobIDs.count - before, reads, "\(conflict): retries stop")
            XCTAssertEqual(session.saveState, .idle)
            XCTAssertEqual(session.refreshSync, .idle)
        }
    }

    /// The connection comes back while a save is in flight: the retry is deferred, not dropped,
    /// and the successful save clears the offline hint on its own.
    func testReconnectDuringASaveClearsTheOfflineHintAndStillRetries() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant(),
            commitResponse: EditorCommitResponse(ok: true, generation: "g2",
                sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: true, mix: true),
                revisionNumber: 4, revisionHash: "r4", expectedDuration: nil),
            suspendNextCommit: true)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        session.refreshRetryDelays = [.seconds(3600)]
        fake.draftError = .offline
        for _ in 0..<NativeEditorSession.reconnectingHintAfterFailures { await session.synchronizePromptRevision() }
        XCTAssertEqual(session.refreshSync, .reconnecting)

        session.setClipTiming(clipID: "s1", durationS: 1.1)
        let saving = Task { await session.save() }
        let suspended = await settle { fake.commitIsSuspended }
        XCTAssertTrue(suspended, "the commit gate did not suspend in time")
        fake.draftError = nil
        session.refreshRetryDelays = [.milliseconds(20)]
        let callsDuringSave = fake.draftCallCount
        await session.resumeRefreshSync()  // reachability fires mid-save
        XCTAssertEqual(fake.draftCallCount, callsDuringSave, "no read while the save is in flight")

        fake.resumeCommit()
        await saving.value
        XCTAssertEqual(session.refreshSync, .idle, "the committed save proves the connection works")
        let retried = await settle { fake.draftCallCount > callsDuringSave }
        XCTAssertTrue(retried, "the deferred retry still runs after the save")
    }

    // MARK: L-a / S2-d: a re-plan whose new job is still rendering

    func testReplanStillRenderingKeepsTheOldVideoThenSwitchesWhenReady() async throws {
        let (session, fake, newProject, jobB) = await replanFixture(manualEdit: false)
        session.newVersionPollDelays = [.milliseconds(20)]
        fake.editorVariantsByJob[jobB] = []
        await session.adoptLatestJob(newProject, api: fake)
        func playing() -> String? { (session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent }
        XCTAssertEqual(playing(), "job-a.mp4", "the working video must not be thrown away")
        XCTAssertEqual(session.loadState, .loaded)
        XCTAssertNil(session.newerJobPrompt, "nothing is ready to switch to")
        XCTAssertEqual(session.refreshSync, .newVersionRendering)
        XCTAssertNotEqual(fake.editorVariantJobIDs.last, jobB)

        var ready = Self.variant(generation: "gNew"); ready["output_url"] = .string("https://example.com/job-b.mp4")
        fake.editorVariantsByJob[jobB] = [ready]
        let switched = await settle { playing() == "job-b.mp4" }
        XCTAssertTrue(switched, "the watch adopts the new job on its own once it is ready")
        XCTAssertEqual(session.refreshSync, .idle)
        XCTAssertEqual(session.loadState, .loaded)
    }

    func testReplanStillRenderingWithManualEditsAsksOnlyOnceItIsReady() async throws {
        let (session, fake, newProject, jobB) = await replanFixture(manualEdit: true)
        session.newVersionPollDelays = [.milliseconds(20)]
        fake.editorVariantsByJob[jobB] = [["variant_id": .string("initial"), "render_status": .string("rendering")]]
        await session.adoptLatestJob(newProject, api: fake)
        XCTAssertNil(session.newerJobPrompt, "'A new version is ready' would be false while it renders")
        XCTAssertTrue(session.hasUnsavedChanges)
        fake.editorVariantsByJob[jobB] = [Self.variant(generation: "gNew")]
        let asked = await settle { session.newerJobPrompt != nil }
        XCTAssertTrue(asked)
        XCTAssertTrue(session.hasUnsavedChanges, "asking never discards")
    }

    func testChatSyncDuringAReplanRenderShowsTheNeutralChipNotAFailure() async throws {
        let (session, fake, newProject, jobB) = await replanFixture(manualEdit: false)
        session.refreshRetryDelays = [.seconds(3600)]
        session.newVersionPollDelays = [.seconds(3600)]
        // The draft follows the plan item to the new job, which has no variants yet (drafts.py:270-285).
        fake.draftError = Self.conflict("draft_variant_ambiguous")
        fake.refreshedThread = try decodeThread(newProject.id, job: jobB)
        fake.editorVariantsByJob[jobB] = []
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.refreshSync, .newVersionRendering)
        XCTAssertEqual(session.saveState, .idle)
        XCTAssertEqual((session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent, "job-a.mp4")
    }

    func testFailedNewRenderLeavesTheCurrentVideoAndNoChip() async throws {
        let (session, fake, newProject, jobB) = await replanFixture(manualEdit: false)
        fake.editorVariantsByJob[jobB] = [["variant_id": .string("initial"), "render_status": .string("failed")]]
        await session.adoptLatestJob(newProject, api: fake)
        XCTAssertEqual(session.refreshSync, .idle)
        XCTAssertEqual((session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent, "job-a.mp4")
    }

    private func decodeThread(_ id: UUID, job: UUID) throws -> CreationThread {
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(CreationThread.self, from: Data(#"""
        {"id":"\#(id.uuidString)","title":"Chat","status":"active","revision":9,"runtime_version":2,
         "active_job_id":"\#(job.uuidString)","active_plan_item_id":"item","state":{"selected_variant_id":"initial"},
         "job":{"id":"\#(job.uuidString)","status":"processing","variants":[]},
         "updated_at":"2026-09-30T08:00:00Z"}
        """#.utf8))
    }

    // MARK: L-b: reopening offline keeps the working editor

    func testReopenOfflineKeepsACleanEditorInsteadOfTheCouldntOpenScreen() async throws {
        for failure in [APIError.offline, .requestFailed(status: 503)] {
            let id = UUID()
            let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
            let session = NativeEditorSession(project: threadProject(id, revision: 5))
            await session.load(project: threadProject(id, revision: 5), api: fake)
            session.refreshRetryDelays = [.seconds(3600)]
            XCTAssertFalse(session.hasUnsavedChanges)
            fake.editorVariantError = failure
            let handled = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
            XCTAssertTrue(handled, "\(failure): a full reload would fail the same way and replace a working editor")
            XCTAssertEqual(session.loadState, .loaded)
        }
    }

    // MARK: L-c: first open waits for a video that is still rendering, or for a connection

    func testFirstOpenOfflineWaitsBehindTheOpeningScreenAndOpensWhenBack() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        fake.editorVariantScript = [.failure(.offline), .failure(.offline)]
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        session.loadRetryDelays = [.milliseconds(20)]
        await session.load(project: threadProject(id, revision: 5), api: fake)
        XCTAssertEqual(session.loadState, .loading, "not 'The editor couldn't open'")
        XCTAssertNotNil(session.loadWaitingMessage)
        let opened = await settle { session.loadState == .loaded }
        XCTAssertTrue(opened)
        XCTAssertNil(session.loadWaitingMessage)
        XCTAssertEqual(session.saveState, .idle)
    }

    func testFirstOpenOfAVideoStillRenderingSaysSoAndOpensWhenReady() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(),
            openReceipt: OpenInEditorResponse(planItemID: "item", variantID: "initial"), authoritativeVariant: Self.variant())
        fake.openErrors = [.editorNotReady, .editorNotReady]
        let session = NativeEditorSession()
        session.loadRenderingDelays = [.milliseconds(20)]
        await session.load(libraryJobID: UUID(uuidString: Self.jobID)!, api: fake)
        XCTAssertEqual(session.loadState, .loading)
        XCTAssertTrue(session.loadWaitingMessage?.contains("still rendering") == true)
        let opened = await settle { session.loadState == .loaded }
        XCTAssertTrue(opened)
    }

    func testFirstOpenOfAGenuinelyUnopenableVideoStillSaysSo() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(),
            openReceipt: OpenInEditorResponse(planItemID: "item", variantID: "initial"), authoritativeVariant: Self.variant())
        fake.openErrors = [.contentPlanUnavailable]
        let session = NativeEditorSession()
        session.loadRetryDelays = [.milliseconds(20)]
        await session.load(libraryJobID: UUID(uuidString: Self.jobID)!, api: fake)
        guard case .failed = session.loadState else { return XCTFail("a missing content plan cannot be waited out") }
    }

    // MARK: N3 / C1: a failed capabilities read is not "iPhone rendering unavailable"

    func testFailedCapabilitiesReadNeverRoutesThePhoneRenderToUnavailable() async throws {
        let threadID = UUID(), jobID = UUID(uuidString: Self.jobID)!
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot())
        fake.phoneDestination = true
        let renderSessions = DeviceRenderSessions(fetch: { _, _ in
            await MainActor.run { fake.deviceFetchCount += 1 }
            throw APIError.unsupported
        }, factory: { _, _ in throw APIError.unsupported })
        let session = NativeEditorSession()
        session.useDeviceRendering(renderSessions)
        // Opened on flaky Wi-Fi: the server has never answered the capabilities question.
        fake.capabilitiesError = .offline
        await session.load(api: fake, threadID: threadID)
        let key = try XCTUnwrap(session.deviceRenderKey)
        XCTAssertEqual(key.jobID, jobID)

        await session.refreshDeviceRender()
        XCTAssertEqual(fake.deviceFetchCount, 0, "no reconcile against a made-up .disabled")
        XCTAssertNil(renderSessions.presentations[key], "no 'Rendering on iPhone is not available' card")

        // Once the server has answered, a later failed read keeps that answer.
        fake.capabilitiesError = nil
        fake.phoneRenderingCapabilities = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1], verifiedFeatures: [])
        await session.refreshDeviceRender()
        XCTAssertEqual(fake.deviceFetchCount, 1)
        fake.capabilitiesError = .requestFailed(status: 503)
        await session.refreshDeviceRender()
        XCTAssertEqual(fake.deviceFetchCount, 2, "reconciled with the last real capabilities")
    }

    // MARK: P-a: the post-save preview converges

    private func savedSession() async -> (NativeEditorSession, EditorCommitSpy) {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant(generation: "g1"),
            commitResponse: EditorCommitResponse(ok: true, generation: "g2",
                sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false),
                revisionNumber: 4, revisionHash: "r4", expectedDuration: nil))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        session.previewPollInterval = .milliseconds(10)
        session.setClipTiming(clipID: "s1", durationS: 1.1)
        await session.save()
        return (session, fake)
    }

    func testPreviewFollowsANewerGenerationSavedFromAnotherDevice() async throws {
        let (session, fake) = await savedSession()
        XCTAssertEqual(session.saveState, .previewPending)
        // The iPad saved on top of this phone's g2 and its render finished first.
        fake.authoritativeVariant = Self.variant(generation: "g3")
        let saved = await settle { session.saveState == .saved }
        XCTAssertTrue(saved, "waiting for g2 can never end once the server is on g3: \(session.saveState)")
        XCTAssertEqual(session.document.revision.baseGeneration, "g3")
    }

    func testPreviewNeverTreatsThePreSaveVideoAsNewer() async throws {
        let (session, fake) = await savedSession()
        session.previewPollBudget = 4
        session.retryPreviewRefresh()
        fake.authoritativeVariant = Self.variant(generation: "g1")
        let settled = await settle { if case .previewFailed = session.saveState { true } else { false } }
        XCTAssertTrue(settled, "the old g1 video is not this save's preview")
    }

    func testOfflinePollsDoNotSpendThePreviewBudget() async throws {
        let (session, fake) = await savedSession()
        session.previewPollBudget = 2
        fake.editorVariantScript = Array(repeating: .failure(.offline), count: 4)
        session.retryPreviewRefresh()
        fake.authoritativeVariant = Self.variant(generation: "g2")
        let saved = await settle { session.saveState == .saved }
        XCTAssertTrue(saved, "four offline polls with a budget of two must still reach the ready preview: \(session.saveState)")
    }

    func testPreviewPollErrorsNeverShowConflictCopy() async throws {
        let (session, fake) = await savedSession()
        session.previewPollBudget = 2
        fake.editorVariantError = .conflict
        session.retryPreviewRefresh()
        let failed = await settle { if case .previewFailed = session.saveState { true } else { false } }
        XCTAssertTrue(failed)
        guard case let .previewFailed(message) = session.saveState else { return }
        XCTAssertFalse(message.contains("changed elsewhere"), message)
    }
}
