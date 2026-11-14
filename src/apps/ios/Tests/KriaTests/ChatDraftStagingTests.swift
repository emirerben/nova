import AVFoundation
import XCTest
@testable import Kria

/// KRI-219/226: a runtime-v2 chat editor turn parks its edit in the draft head
/// (never rendered). The editor must show it immediately as UNSAVED edits.
@MainActor
final class ChatDraftStagingTests: XCTestCase {

    private static let jobID = "5b1d2f0a-6c0e-4b8e-9d0a-1c2d3e4f5a6b"
    private static let musicID = "8c9c1e0e-2d62-4a44-8d0c-2f4d3a0a9b11"

    private static func variant(generation: String = "g1", output: String = "file:///tmp/kria-chat-draft.mp4") -> [String: JSONValue] {
        func slot(_ id: String, _ index: Double) -> JSONValue {
            .object(["slot_id": .string(id), "clip_index": .number(index), "in_s": .number(0), "duration_s": .number(2),
                     "source_duration_s": .number(6), "removed": .bool(false)])
        }
        return [
            "variant_id": .string("initial"), "render_generation_id": .string(generation),
            "render_status": .string("ready"), "duration_s": .number(6),
            "output_url": .string(output),
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
    private static func chatPayload(base: String = "g1", textOnly: Bool = false, shape: Shape = .nested, effect: String = "karaoke-line") -> [String: JSONValue] {
        func wire(_ id: String, _ index: Double, _ duration: Double, removed: Bool = false) -> JSONValue {
            .object(["slot_id": .string(id), "clip_index": .number(index), "in_s": .number(0), "duration_s": .number(duration),
                     "removed": .bool(removed), "look_preset": .string("none"), "transition_after": .string("cut")])
        }
        let slots: [JSONValue] = [wire("s3", 2, 2), wire("s1", 0, 1.5), wire("s2", 1, 2, removed: true)]
        let text: JSONValue = .array([.object(["id": .string("t1"), "text": .string("changed"), "start_s": .number(0), "end_s": .number(3600),
                                               "role": .string("generative_intro"), "effect": .string(effect), "x_frac": .number(0.5), "y_frac": .number(0.2),
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

    private static func chatSnapshot(base: String = "g1", revision: Int = 3, textOnly: Bool = false, shape: Shape = .nested,
                                     effect: String = "karaoke-line") -> DraftSnapshot {
        DraftSnapshot(draftID: "d\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: base,
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "edit_format": .string("montage"),
                       "editor_payload": .object(chatPayload(base: base, textOnly: textOnly, shape: shape, effect: effect))], canUndo: true, createdAt: .now)
    }

    private static func bootstrapSnapshot() -> DraftSnapshot {
        DraftSnapshot(draftID: "d1", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e",
            baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "editor_payload": .object(["base_generation": .string("g1"), "sections": .object([:])])],
            canUndo: false, createdAt: .now)
    }

    func testRecoveredPermissionsUnlockRangeWithoutSavingLocalText() async throws {
        var locked = Self.variant()
        locked["editor_capabilities"] = .object(["timeline": .bool(false), "text_elements": .bool(true)])
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: locked)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        session.setTextStyle(id: "t1", style: "Inter")
        let editedText = session.document.textElements
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        XCTAssertNotNil(session.sourceWindowBlockedReason(for: selection))

        fake.editorVariantError = .offline
        await session.synchronizePromptRevision()
        fake.editorVariantError = nil
        fake.authoritativeVariant = Self.variant()
        await session.synchronizePromptRevision()

        XCTAssertNil(session.sourceWindowBlockedReason(for: selection))
        XCTAssertTrue(session.canEditTimeline)
        XCTAssertEqual(session.document.textElements, editedText)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(fake.commitCount, 0)
        session.undo()
        XCTAssertNil(session.sourceWindowBlockedReason(for: selection), "Undo must not restore stale permissions")
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

    func testSavedTextOnlySnapshotKeepsLockedRenderedClipsWithoutResurrectingExplicitClears() {
        for explicitClear in [false, true] {
            var sections: [String: JSONValue] = ["text_elements": .array([
                .object(["id": .string("title"), "text": .string("My saved title"),
                         "start_s": .number(0), "end_s": .number(2)])
            ])]
            if explicitClear { sections["timeline_slots"] = .array([]) }
            let snapshot = DraftSnapshot(draftID: "saved", itemID: "item", variantKey: "initial",
                draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: Self.jobID,
                baseGenerationID: "g1", snapshot: ["kind": .string("editor"), "editor_payload": .object([
                    "base_generation": .string("g1"), "editor_state_version": .number(2),
                    "sections": .object(sections)
                ])], canUndo: true, createdAt: .now)
            var variant = Self.variant()
            variant["resolved_archetype"] = .string("guided_story")
            variant["editor_capabilities"] = .object(["timeline": .bool(false), "text_elements": .bool(true)])
            let originalSnapshot = snapshot.snapshot
            let loaded = snapshot.editorDraft(projectID: UUID(), authoritativeVariant: variant)
            let document = EditorDocument(snapshot: loaded.serverSnapshot)
            XCTAssertEqual(document.clips.count, explicitClear ? 0 : 3)
            XCTAssertEqual(document.textElements.map(\.text), ["My saved title"])
            XCTAssertEqual(document.capabilities["timeline"]?.editable, false)
            XCTAssertEqual(snapshot.snapshot, originalSnapshot)
        }
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

    /// Edits already submitted with the turn are its base, not concurrent conflicts.
    func testChatBasedOnSubmittedLocalEditsAppliesAndUndoRestoresCompleteLocalDocument() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setTextStyle(id: "t1", style: "Inter")
        let before = session.document
        let sent = try XCTUnwrap(session.exportEditorState())
        fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: before, text: "changed"))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .idle)
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertEqual(session.document.textElements.first?.raw["font_family"], .string("Inter"))
        XCTAssertTrue(session.hasUnsavedChanges)
        let applied = session.document
        session.undo()
        assertAuthoredDocument(session.document, equals: before)
        XCTAssertEqual(session.document.revision, before.revision)
        await session.synchronizePromptRevision()
        assertAuthoredDocument(session.document, equals: before)
        session.redo()
        assertAuthoredDocument(session.document, equals: applied)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testDisjointLocalEditKeepsBothSides() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setClipTiming(clipID: "s1", durationS: 1.2)
        let before = session.document
        let sent = try XCTUnwrap(session.exportEditorState())
        fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: before, text: "changed", revision: 5))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.dirtySections, [.timeline, .text])
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, 1.2)
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertEqual(session.document.textElements.first?.endS, 2, "wording-only input preserves its submitted timing")
        session.undo()
        assertAuthoredDocument(session.document, equals: before)
        XCTAssertEqual(fake.commitCount, 0)
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

    // MARK: KRI-1333 — thread 1894f8d0: a later chat turn rebuilt on the published video

    /// The head a chat turn stores from the phone's state: flat lanes plus the Save defaults
    /// every phone state carries (`editor_state_version: 2`, `remove_music: false`), no `sections`.
    private static func incidentHead(revision: Int = 7) -> DraftSnapshot {
        var payload = chatPayload(shape: .flatOnly)
        payload["editor_state_version"] = .number(2)
        return DraftSnapshot(draftID: "d\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "edit_format": .string("montage"),
                       "editor_payload": .object(payload)], canUndo: true, createdAt: .now)
    }

    private static func encoded(_ state: EditorStateRequest) throws -> [String: JSONValue] {
        try XCTUnwrap(JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(state)).objectValue)
    }

    /// Which variant a committed snapshot belongs to: its generation id, else its finish time; an unstamped draft keeps
    /// owning authoring state on a variant that carries no generation id.
    func testCommittedSnapshotOwnsAuthoringStateOnlyForItsOwnVariantBaseline() {
        func text(base: String?, variant: [String: JSONValue]) -> [String] {
            let payload: [String: JSONValue] = [
                "editor_state_version": .number(2),
                "sections": .object(["text_elements": .array([.object(["id": .string("road"), "text": .string("Road trip"),
                                                                       "start_s": .number(0), "end_s": .number(3)])])]),
            ]
            let snapshot = DraftSnapshot(draftID: "saved", itemID: "item", variantKey: "initial", draftRevision: 4,
                snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: base,
                snapshot: ["kind": .string("editor"), "editor_payload": .object(payload)], canUndo: true, createdAt: .now)
            return EditorDocument(snapshot: snapshot.editorDraft(projectID: UUID(), authoritativeVariant: variant).serverSnapshot).textElements.map(\.text)
        }
        var finishedOnly = Self.variant()
        finishedOnly["render_generation_id"] = nil
        finishedOnly["render_finished_at"] = .string("2026-11-01T10:00:00Z")
        XCTAssertEqual(text(base: "2026-11-01T10:00:00Z", variant: finishedOnly), ["Road trip"], "stamped with the finish time")
        XCTAssertEqual(text(base: nil, variant: finishedOnly), ["Road trip"], "no base id on either side: unchanged")
        XCTAssertEqual(text(base: "2026-10-01T10:00:00Z", variant: finishedOnly), ["hello"], "another render's snapshot")
        XCTAssertEqual(text(base: nil, variant: Self.variant()), ["hello"], "a variant with a generation the draft never named")
    }

    func testAChatHeadIsNotACommittedSnapshotSoTheCleanDocumentIsTheVariant() throws {
        let draft = Self.incidentHead().editorDraft(projectID: UUID(), authoritativeVariant: Self.variant())
        let clean = EditorDocument(snapshot: draft.serverSnapshot)
        XCTAssertEqual(clean.textElements.map(\.text), ["hello"], "the published text, not the chat head's")
        XCTAssertEqual(clean.clips.compactMap(\.id), ["s1", "s2", "s3"])
        XCTAssertNotNil(clean.music)
    }

    /// KRI-1394: a chat turn that only changed the creator's song is still a chat head. Its `sections` hold
    /// whatever the turn started from, never the published text.
    func testAChatHeadWithOnlyASongEditIsNotACommittedSnapshot() {
        let song: JSONValue = .object(["volume": .number(0.4), "removed": .bool(false)])
        let payload: [String: JSONValue] = [
            "base_generation": .string("g1"), "editor_state_version": .number(2), "user_song": song,
            "sections": .object(["user_song": song, "text_elements": .array([.object(["id": .string("t1"), "text": .string("unsaved"),
                                                                                     "start_s": .number(0), "end_s": .number(2)])])]),
        ]
        let head = DraftSnapshot(draftID: "d5", itemID: "item", variantKey: "initial", draftRevision: 5,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "editor_payload": .object(payload)],
            canUndo: true, createdAt: .now)
        let clean = EditorDocument(snapshot: head.editorDraft(projectID: UUID(), authoritativeVariant: Self.variant()).serverSnapshot)
        XCTAssertEqual(clean.textElements.map(\.text), ["hello"], "the published text, not the chat head's")
    }

    func testUndoingAStagedChatHeadReturnsToThePublishedDocument() async throws {
        let (session, _) = await loaded(Self.incidentHead())
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        session.undo()
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.textElements.map(\.text), ["hello"])
        XCTAssertEqual(session.document.clips.compactMap(\.id), ["s1", "s2", "s3"])
    }

    func testJobPathOpenWhoseDraftReadFailedStagesTheHeadBeforeExport() async throws {
        let (session, fake) = await openedProject(Self.incidentHead(), draftError: .conflict)
        XCTAssertFalse(session.hasUnsavedChanges, "the best-effort open read lost the chat head")
        fake.draftError = nil
        let exported = await session.exportEditorStateSyncingDraft()
        let state = try XCTUnwrap(exported)
        XCTAssertNotNil(state.lanes.textElements)
        XCTAssertNotNil(state.lanes.timelineSlots)
        XCTAssertEqual(try Self.encoded(state)["draft_revision"], .number(7), "names the chat draft it carries")
    }

    func testAStateThatNeverSawTheHeadNamesNoRevisionSoTheServerDecides() async throws {
        let (session, _) = await openedProject(Self.incidentHead(), draftError: .conflict)
        let exported = await session.exportEditorStateSyncingDraft()
        let state = try XCTUnwrap(exported)
        XCTAssertNil(state.lanes.textElements)
        XCTAssertNil(try Self.encoded(state)["draft_revision"])
    }

    func testUndoingTheStagedChatEditDeclaresTheRevisionItDiscarded() async throws {
        let (session, _) = await openedProject(Self.incidentHead())
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        session.undo()
        let exported = await session.exportEditorStateSyncingDraft()
        let state = try XCTUnwrap(exported)
        XCTAssertNil(state.lanes.textElements, "the creator discarded it")
        XCTAssertEqual(try Self.encoded(state)["draft_revision"], .number(7))
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

        let submitted = session.document
        let sent = try XCTUnwrap(session.exportEditorState())
        fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: submitted, text: "changed"))
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
    private func openedWhilePreparing(_ snapshot: DraftSnapshot, variant: [String: JSONValue] = ChatDraftStagingTests.variant()) async throws -> (NativeEditorSession, EditorCommitSpy, Task<Void, Never>) {
        let fake = EditorCommitSpy(draftSnapshot: snapshot, authoritativeVariant: variant)
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

    private static func finishedVideo() throws -> URL {
        try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
    }

    private static func playingURL(_ session: NativeEditorSession) -> URL? {
        (session.player?.currentItem?.asset as? AVURLAsset)?.url
    }

    /// Prod, build 1934: a 50-clip edit with a staged AI change never finished its live preview, and Play did nothing.
    func testPlayWhileAStagedEditsPreviewNeverFinishesPlaysTheVersionBeforeItLabelled() async throws {
        let finished = try Self.finishedVideo()
        let (session, fake, loading) = try await openedWhilePreparing(Self.chatSnapshot(shape: .flatOnly),
                                                                      variant: Self.variant(output: finished.absoluteString))
        XCTAssertEqual(session.sourcePreviewState, .preparing)
        XCTAssertFalse(session.isShowingVersionBeforeStagedEdit, "nothing old is shown until the creator asks to play")

        session.togglePlayback()

        XCTAssertTrue(session.isPlaying, "Play must do something while the edit is still being applied")
        XCTAssertEqual(Self.playingURL(session), finished, "the last finished video plays")
        XCTAssertTrue(session.isShowingVersionBeforeStagedEdit, "and it is labelled as the version before the AI edit")
        XCTAssertFalse(session.canDisplayCurrentPlayer, "it is never presented as the edited video")
        XCTAssertFalse(session.canDownloadCurrentVideo, "the finished video does not export as the edited one")
        XCTAssertNotNil(session.exportBlockReason)
        session.pausePlayback()
        fake.resumeSourcePool()
        await loading.value
    }

    func testAFailedStagedEditPreviewOffersRetryOverThePlayableVersionBeforeIt() async throws {
        let finished = try Self.finishedVideo()
        let fake = EditorCommitSpy(draftSnapshot: Self.chatSnapshot(shape: .flatOnly),
                                   authoritativeVariant: Self.variant(output: finished.absoluteString))
        fake.sourcePoolResult = nil  // the live preview cannot be built
        let project = threadProject(UUID(), revision: 5)
        let session = NativeEditorSession(project: project)
        session.sourcePreviewQuietRetryDelay = .milliseconds(10)
        await session.load(project: project, api: fake)
        for _ in 0..<100 where !session.sourcePreviewState.isFailure { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertTrue(session.sourcePreviewState.isFailure)

        XCTAssertTrue(session.isShowingVersionBeforeStagedEdit, "the finished video stays watchable, labelled as before the AI edit")
        XCTAssertTrue(session.fallbackOffersRetry, "the failure keeps its Retry")
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        XCTAssertEqual(Self.playingURL(session), finished)
        XCTAssertFalse(session.isShowingRenderedFallback, "it is not the current video")
        XCTAssertFalse(session.canDownloadCurrentVideo)
        XCTAssertTrue(session.hasUnsavedChanges, "the staged AI edit is still there to save")
        session.pausePlayback()
    }

    /// An open editor receives the chat edit while its live preview is showing; the rebuild is slow.
    func testPlayDuringAStagedEditsRebuildHandsTheRetainedFrameToTheVersionBeforeItThenToTheLiveEdit() async throws {
        let finished = try Self.finishedVideo()
        var variant = Self.variant(output: finished.absoluteString)
        variant["music_track_id"] = nil; variant["music_window"] = nil  // the fixture preview has no song source
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: variant)
        let project = threadProject(UUID(), revision: 5)
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        session.beginPreviewPresentation()
        defer { session.endPreviewPresentation() }
        await session.prepareFixtureSourcePreview(url: finished)
        XCTAssertEqual(session.sourcePreviewState, .ready, "the live preview is up before the chat edit arrives")
        XCTAssertEqual(session.loadState, .loaded)
        for _ in 0..<250 where session.player?.currentItem?.status != .readyToPlay { try await Task.sleep(for: .milliseconds(20)) }
        let ticket = try XCTUnwrap(session.admissiblePreviewDisplayTicket)
        XCTAssertTrue(session.previewFrameWasShown(player: try XCTUnwrap(session.player), ticket: ticket))

        fake.draftSnapshot = Self.chatSnapshot(textOnly: true, effect: "none")
        await session.synchronizePromptRevision()
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        session.retainAdmittedPreview()
        let rebuild = Task { await session.prepareFixtureSourcePreview(url: finished, delayedLoad: true) }
        for _ in 0..<50 where session.sourcePreviewState != .preparing { await Task.yield() }
        XCTAssertEqual(session.previousPreviewPresentation?.status, .updating)

        session.togglePlayback()

        XCTAssertTrue(session.isPlaying, "Play is not a silent no-op while the edit is applied")
        XCTAssertNil(session.previousPreviewPresentation, "the frozen frame gives way to a playing video")
        XCTAssertTrue(session.isShowingVersionBeforeStagedEdit)
        XCTAssertFalse(session.canDownloadCurrentVideo)
        await rebuild.value
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertTrue(session.canDisplayCurrentPlayer, "the live preview with the AI edit takes over")
        XCTAssertFalse(session.isShowingVersionBeforeStagedEdit)
        session.pausePlayback()
    }

    func testReopenMergesANewerRenderWithoutDiscardingIndependentManualEdits() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        await session.load(project: threadProject(id, revision: 5), api: fake)
        session.setClipTiming(clipID: "s1", durationS: 1.1)
        let before = session.document
        let variantFetches = fake.editorVariantJobIDs.count
        fake.authoritativeVariant = Self.variant(generation: "g2")
        let handled = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
        XCTAssertTrue(handled)
        XCTAssertNil(session.newerJobPrompt)
        assertAuthoredDocument(session.document, equals: before)
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(fake.editorVariantJobIDs.count, variantFetches + 2, "one readiness check and one authoritative rebase read")
        XCTAssertEqual(fake.editorVariantJobIDs.last, UUID(uuidString: Self.jobID))
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testNewerRenderOverlapShowsLatestAndUndoRestoresMyEditOnTheNewBase() async throws {
        let id = UUID()
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        let session = NativeEditorSession(project: threadProject(id, revision: 5))
        await session.load(project: threadProject(id, revision: 5), api: fake)
        session.updateTextContent(id: "t1", content: "local wording")
        var latest = Self.variant(generation: "g2")
        guard case var .array(rows)? = latest["text_elements"], case var .object(row) = rows[0] else { return XCTFail("text fixture missing") }
        row["text"] = .string("remote wording"); rows[0] = .object(row); latest["text_elements"] = .array(rows)
        fake.authoritativeVariant = latest
        _ = await session.reconcileOnOpen(project: threadProject(id, revision: 7), api: fake)
        XCTAssertEqual(session.document.textElements.first?.text, "remote wording", "the latest version wins an overlap")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertEqual(session.convergeNotice, "Updated to the latest version. Some of your edits were replaced. Undo to restore them.")
        XCTAssertTrue(session.canUndo)
        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "local wording")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2", "Undo never restores the old base generation")
        XCTAssertTrue(session.hasUnsavedChanges)
        session.redo()
        XCTAssertEqual(session.document.textElements.first?.text, "remote wording")
        XCTAssertEqual(fake.commitCount, 0)
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

    func testReopenKeepsManualEditsThroughDroppedConnectionAndSupersededJob() async throws {
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
            XCTAssertTrue(handled, "a superseded read cannot authorize discarding manual edits")
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

    /// KRI-1384: chat "undo that" writes a newer head whose lane is back to the saved value. Server
    /// rows placed by fractions carry `position: custom`, which the editor's clean document also spells.
    private static func restoredHead(revision: Int) -> DraftSnapshot {
        let rows = variant()["text_elements"]?.arrayValue ?? []
        let published: JSONValue = .array(rows.map { row in
            guard case var .object(object) = row else { return row }
            object["position"] = .string("custom")
            return .object(object)
        })
        return DraftSnapshot(draftID: "d\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision,
            snapshotHash: "h\(revision)", etag: "e\(revision)", baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "edit_format": .string("montage"),
                       "editor_payload": .object(["base_generation": .string("g1"), "text_elements": published,
                                                  "sections": .object(["text_elements": published])])],
            canUndo: true, createdAt: .now)
    }

    func testANewerChatHeadThatPutsALaneBackReachesTheLiveEditor() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        fake.draftSnapshot = Self.chatSnapshot(textOnly: true)
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        fake.draftSnapshot = Self.restoredHead(revision: 4)
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "hello", "the chat's undo reached the editor")
        XCTAssertFalse(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "changed", "the editor's Undo brings the chat edit back")
        session.redo()
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "hello", "the same head is not applied twice")
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testANewerChatHeadThatPutsALaneBackLeavesALaneTheCreatorEditedSinceAlone() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        fake.draftSnapshot = Self.chatSnapshot(textOnly: true)
        await session.synchronizePromptRevision()
        session.setTextStyle(id: "t1", style: "Inter")
        fake.draftSnapshot = Self.restoredHead(revision: 4)
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertEqual(session.document.textElements.first?.raw["font_family"], .string("Inter"))
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    private static func withoutNulls(_ value: JSONValue?) -> JSONValue? {
        switch value {
        case let .object(object): return .object(object.compactMapValues { withoutNulls($0) })
        case let .array(array): return .array(array.compactMap { withoutNulls($0) })
        case .some(.null), .none: return nil
        default: return value
        }
    }

    func testAnUntouchedStagedChatLaneIsSentBackAsTheHeadWroteIt() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let head = Self.chatSnapshot(textOnly: true)
        fake.draftSnapshot = head
        await session.synchronizePromptRevision()
        let state = try XCTUnwrap(session.exportEditorState())
        let lanes = try XCTUnwrap(Self.encoded(state)["lanes"]?.objectValue)
        let written = Self.withoutNulls(head.snapshot["editor_payload"]?.objectValue?["text_elements"])
        XCTAssertEqual(Self.withoutNulls(lanes["text_elements"]), written)
    }

    /// Failure mode 26: a newer AI draft replacing an earlier staged AI lane is not "your edits were replaced".
    func testNewerAIDraftOverlappingAnEarlierStagedAILaneNeverClaimsTheCreatorsEditsWereReplaced() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setMusicLevel(0.5)
        fake.draftSnapshot = Self.chatSnapshot(revision: 3, textOnly: true, shape: .flatOnly)
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertFalse(session.hasOnlyChatStagedChanges, "the creator's mix edit sits beside the AI's text lane")
        XCTAssertNil(session.convergeNotice)
        fake.draftSnapshot = Self.chatSnapshot(revision: 4, textOnly: true, shape: .flatOnly, effect: "pop")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.raw["effect"], .string("pop"), "the newer AI draft wins the AI lane")
        XCTAssertEqual(session.document.mix["music_level"], .number(0.5), "the creator's own edit is untouched")
        XCTAssertNil(session.convergeNotice, "nothing of the creator's was replaced")
    }

    /// Failure mode 23 on the sync path: a newer render with no fresh head under a chat-only editor.
    func testNewerRenderWithoutAFreshHeadKeepsTheAIStagedEditOnTheNewVersion() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot(shape: .flatOnly))
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        fake.authoritativeVariant = Self.variant(generation: "g2")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertEqual(session.document.textElements.first?.text, "changed")
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "still the AI's edit, still unsaved")
        XCTAssertNil(session.convergeNotice)
        XCTAssertFalse(session.canUndo)
    }

    private static func transitionDraft(revision: Int, base: String = "g1", transitions: [String: String], echo: String? = nil) -> DraftSnapshot {
        let slots: [JSONValue] = [("s1", 0.0), ("s2", 1.0), ("s3", 2.0)].map { id, index in
            .object(["slot_id": .string(id), "clip_index": .number(index), "in_s": .number(0), "duration_s": .number(2),
                     "removed": .bool(false), "transition_after": .string(transitions[id] ?? "cut")])
        }
        let draft = DraftSnapshot(draftID: "t\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: base,
            snapshot: ["kind": .string("editor"), "editor_payload": .object([
                "base_generation": .string(base), "timeline_slots": .array(slots)])], canUndo: true, createdAt: .now)
        return echo.map { Self.echoing($0, draft) } ?? draft
    }

    private static func transition(_ session: NativeEditorSession, _ id: String) -> String? {
        session.document.clips.first { $0.id == id }?.transitionAfter
    }

    func testChatStagedTransitionSurvivesSuccessiveRendersOfTheSameJobWithoutConflict() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        fake.draftSnapshot = Self.transitionDraft(revision: 3, transitions: ["s1": "crossfade"])
        await session.synchronizePromptRevision()
        XCTAssertEqual(Self.transition(session, "s1"), "crossfade")
        XCTAssertTrue(session.hasOnlyChatStagedChanges)

        for (revision, generation) in [(4, "g2"), (5, "g3")] {
            fake.authoritativeVariant = Self.variant(generation: generation)
            fake.draftSnapshot = Self.transitionDraft(revision: revision, base: generation, transitions: ["s1": "crossfade"])
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.document.revision.baseGeneration, generation)
            XCTAssertEqual(Self.transition(session, "s1"), "crossfade", generation)
            XCTAssertTrue(session.hasOnlyChatStagedChanges, "the AI's edit is still the AI's after \(generation)")
        }
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testTwoDraftRevisionsEchoingTheSameSubmittedStateReplaceEachOtherWithoutConflict() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let sent = try XCTUnwrap(session.exportEditorState())
        fake.draftSnapshot = Self.transitionDraft(revision: 3, transitions: ["s1": "crossfade"], echo: sent.clientStateID)
        await session.synchronizePromptRevision()
        XCTAssertEqual(Self.transition(session, "s1"), "crossfade")
        XCTAssertTrue(session.hasOnlyChatStagedChanges)

        fake.draftSnapshot = Self.transitionDraft(revision: 4, transitions: ["s1": "crossfade", "s2": "dip_to_black"], echo: sent.clientStateID)
        await session.synchronizePromptRevision()
        XCTAssertEqual(Self.transition(session, "s1"), "crossfade")
        XCTAssertEqual(Self.transition(session, "s2"), "dip_to_black")
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testChatTransitionWinsAnOverlapWithAManualOneAndUndoRestoresTheManualOne() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let sent = try XCTUnwrap(session.exportEditorState())
        session.setClipTransition(clipID: "s1", transition: "flash")
        fake.draftSnapshot = Self.transitionDraft(revision: 3, transitions: ["s1": "crossfade"], echo: sent.clientStateID)
        await session.synchronizePromptRevision()
        XCTAssertEqual(Self.transition(session, "s1"), "crossfade")
        XCTAssertTrue(session.canUndo)
        session.undo()
        XCTAssertEqual(Self.transition(session, "s1"), "flash")
    }

    func testNewerRenderChangingTheSameTransitionWinsAndUndoRestoresMine() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setClipTransition(clipID: "s1", transition: "flash")
        var latest = Self.variant(generation: "g2")
        guard case var .object(timeline)? = latest["user_timeline"], case var .array(slots)? = timeline["slots"],
              case var .object(first) = slots[0] else { return XCTFail("timeline fixture missing") }
        first["transition_after"] = .string("crossfade"); slots[0] = .object(first)
        timeline["slots"] = .array(slots); latest["user_timeline"] = .object(timeline)
        fake.authoritativeVariant = latest
        await session.synchronizePromptRevision()
        XCTAssertEqual(Self.transition(session, "s1"), "crossfade")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        session.undo()
        XCTAssertEqual(Self.transition(session, "s1"), "flash")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
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

    /// Failure mode 23: the AI's staged edit is replayed on a latest that does not contain it. It stays the AI's.
    func testSaveConflictReplaysAnAIStagedEditOnALatestWithoutItAndKeepsItTheAIs() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot())
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        // Another device saved g2 (without the AI edit); the head on g1 can neither restage nor be applied again.
        fake.authoritativeVariant = Self.variant(generation: "g2")
        fake.commitErrorQueue = [.baselineConflict, .baselineConflict]
        await session.save()
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertEqual(session.document.textElements.first?.text, "changed", "the AI edit is still shown")
        XCTAssertEqual(session.document.clips.compactMap(\.id), ["s3", "s1"])
        XCTAssertTrue(session.hasUnsavedChanges, "it is still unsaved, not reported as saved")
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "and it is still the AI's, not the creator's")
        XCTAssertNil(session.convergeNotice)
        XCTAssertFalse(session.canUndo, "no creator undo step for the AI's edit")
        XCTAssertNotEqual(session.saveState, .saved)
    }

    func testSaveConflictSendsTheReplayedAIEditOnTheNewBase() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot())
        fake.authoritativeVariant = Self.variant(generation: "g2")
        fake.commitErrorQueue = [.baselineConflict]
        await session.save()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(fake.lastRequest?.textElements?.first?.objectValue?["text"]?.stringValue, "changed")
        XCTAssertNotNil(fake.lastRequest?.timelineSlots)
        XCTAssertNil(session.convergeNotice)
    }

    /// The latest is a (quantized) render of the same edit: it wins every overlap, nothing is left to save.
    func testSaveConflictWhereTheLatestRenderedTheSameAIEditShowsTheLatestAndSaves() async throws {
        let (session, fake) = await loaded(Self.chatSnapshot(textOnly: true))
        var latest = Self.variant(generation: "g2")
        guard case var .array(rows)? = latest["text_elements"], case var .object(row) = rows[0] else { return XCTFail("fixture") }
        row["text"] = .string("changed (rendered)"); rows[0] = .object(row); latest["text_elements"] = .array(rows)
        fake.authoritativeVariant = latest
        fake.commitErrorQueue = [.baselineConflict]
        await session.save()
        XCTAssertEqual(session.document.textElements.first?.text, "changed (rendered)")
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(session.saveState, .saved)
        XCTAssertEqual(fake.commitCount, 1, "nothing of the AI's is left to send")
        XCTAssertNil(session.convergeNotice)
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

    func testExplicitChatMusicRemovalClearsCanonicalAndLegacyRepresentations() async throws {
        let (session, _) = await loaded(Self.bootstrapSnapshot())
        let original = session.document.encodeSnapshot()
        let legacy: JSONValue = .object(["track_id": .string(Self.musicID), "start_s": .number(1)])
        let opaque: JSONValue = .object(["future": .array([.null, .string("preserve")])])
        for shape in ["canonical-window", "legacy-alias", "legacy-alias-with-canonical-nulls"] {
            var input = original
            var payload = try XCTUnwrap(input["editor_payload"]?.objectValue)
            var sections = try XCTUnwrap(payload["sections"]?.objectValue)
            sections["future_section"] = opaque
            if shape != "canonical-window" {
                sections["music"] = legacy
                sections["music_track_id"] = shape == "legacy-alias" ? nil : .null
                sections["music_window"] = shape == "legacy-alias" ? nil : .null
            }
            payload["sections"] = .object(sections)
            input["editor_payload"] = .object(payload)
            let noRemoval = DraftSnapshot(draftID: "keep", itemID: "item", variantKey: "initial",
                draftRevision: 3, snapshotHash: "h", etag: "e", baseJobID: Self.jobID,
                baseGenerationID: "g1", snapshot: ["kind": .string("editor"), "editor_payload": .object([
                    "base_generation": .string("g1"), "remove_music": .bool(false),
                ])], canUndo: true, createdAt: .now)
            XCTAssertNil(noRemoval.stagedChatEdit(over: input, variant: Self.variant(), appliedRevision: nil),
                "Without explicit removal, preserve the existing representation: \(shape)")
            let removal = DraftSnapshot(draftID: "remove", itemID: "item", variantKey: "initial",
                draftRevision: 3, snapshotHash: "h", etag: "e", baseJobID: Self.jobID,
                baseGenerationID: "g1", snapshot: ["kind": .string("editor"), "editor_payload": .object([
                    "base_generation": .string("g1"), "remove_music": .bool(true),
                ])], canUndo: true, createdAt: .now)
            let staged = try XCTUnwrap(removal.stagedChatEdit(over: input, variant: Self.variant(), appliedRevision: nil), shape)
            XCTAssertEqual(staged.sections, [.music], shape)
            let result = try XCTUnwrap(staged.snapshot["editor_payload"]?.objectValue?["sections"]?.objectValue)
            XCTAssertNil(result["music"], shape)
            XCTAssertEqual(result["music_track_id"], .null, shape)
            XCTAssertEqual(result["music_window"], .null, shape)
            let musicKeys = Set(["music", "music_track_id", "music_window"])
            XCTAssertEqual(result.filter { !musicKeys.contains($0.key) }, sections.filter { !musicKeys.contains($0.key) }, shape)
        }
    }

    func testFlatChatMusicRemovalRemainsCanonicalThroughManualTrimAndNewGenerationMerge() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        var original = session.document.encodeSnapshot()
        let opaque: JSONValue = .object(["future": .array([.null, .string("preserve"), .number(7)])])
        original["future_root"] = opaque
        var payload = try XCTUnwrap(original["editor_payload"]?.objectValue)
        payload["future_payload"] = opaque
        var sections = try XCTUnwrap(payload["sections"]?.objectValue)
        sections["future_section"] = opaque
        payload["sections"] = .object(sections)
        original["editor_payload"] = .object(payload)
        let base = EditorDocument(snapshot: original)
        let staged = try XCTUnwrap(Self.chatSnapshot(shape: .flatOnly).stagedChatEdit(
            over: original, variant: Self.variant(), appliedRevision: nil))
        let stagedPayload = try XCTUnwrap(staged.snapshot["editor_payload"]?.objectValue)
        let stagedSections = try XCTUnwrap(stagedPayload["sections"]?.objectValue)
        XCTAssertEqual(stagedSections["music_track_id"], .null)
        XCTAssertEqual(stagedSections["music_window"], .null)
        // The removal instruction owns only its authored lanes, including when
        // the server sends no nested sections and the base has a music window.
        XCTAssertEqual(staged.snapshot.filter { $0.key != "editor_payload" }, original.filter { $0.key != "editor_payload" })
        XCTAssertEqual(stagedPayload.filter { $0.key != "sections" }, payload.filter { $0.key != "sections" })
        let touched = Set(["timeline_slots", "text_elements", "mix", "music_track_id", "music_window"])
        XCTAssertEqual(stagedSections.filter { !touched.contains($0.key) }, sections.filter { !touched.contains($0.key) })

        var local = EditorDocument(snapshot: staged.snapshot)
        let clipIndex = try XCTUnwrap(local.clips.firstIndex { $0.id == "s1" })
        local.clips[clipIndex].durationS = 1.1
        var remote = base
        remote.revision.baseGeneration = "gNew"
        let result = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil)
        guard case .resolved(let merged) = result else {
            return XCTFail("Canonical chat music removal plus a local trim must survive strict typed merge validation")
        }
        XCTAssertNil(merged.music)
        XCTAssertEqual(merged.snapshot(for: .music), .object(["music_track_id": .null, "music_window": .null]))
        XCTAssertEqual(merged.clips.first { $0.id == "s1" }?.durationS, 1.1)
        XCTAssertEqual(merged.textElements, local.textElements)
        XCTAssertEqual(merged.textElements.first?.text, "changed")
        XCTAssertEqual(merged.revision.baseGeneration, "gNew")
        let mergedRaw = merged.encodeSnapshot()
        XCTAssertEqual(mergedRaw["future_root"], opaque)
        XCTAssertEqual(mergedRaw["editor_payload"]?.objectValue?["future_payload"], opaque)
        XCTAssertEqual(mergedRaw["editor_payload"]?.objectValue?["sections"]?.objectValue?["future_section"], opaque)
        XCTAssertEqual(fake.commitCount, 0)
    }

    private func replanFixture(manualEdit: Bool) async throws -> (NativeEditorSession, EditorCommitSpy, ProjectSummary, UUID) {
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
        let latest = try thread(threadID, job: jobB, revision: 9)
        fake.refreshedThread = latest
        return (session, fake, latest.summary, jobB)
    }

    func testReplanWithOnlyChatStagedEditsSwitchesToTheNewJobWithoutCommitting() async throws {
        let (session, fake, newProject, jobB) = try await replanFixture(manualEdit: false)
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        await session.adoptLatestJob(newProject, api: fake)
        let adopted = await settle {
            (session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent == "job-b.mp4"
                && session.loadState == .loaded && !session.hasUnsavedChanges
        }
        XCTAssertTrue(adopted, "queued observation finishes adopting the new job")
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

    func testReplanWithMatchingEditableBaseMergesManualEditsWithoutSwitchPrompt() async throws {
        let (session, fake, newProject, jobB) = try await replanFixture(manualEdit: true)
        let before = session.document
        await session.adoptLatestJob(newProject, api: fake)
        let adopted = await settle { session.document.revision.baseGeneration == "gNew" }
        XCTAssertTrue(adopted)
        XCTAssertNil(session.newerJobPrompt)
        assertAuthoredDocument(session.document, equals: before)
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, 1.1)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertFalse(session.isBehindActiveJob(newProject))
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testReplanWithoutMatchingEditableBasePromptsThenSwitchDiscardsAndKeepEditingStays() async throws {
        let (session, fake, newProject, jobB) = try await replanFixture(manualEdit: true)
        let available = fake.draftSnapshot
        fake.draftSnapshot = Self.bootstrapSnapshot()
        await session.adoptLatestJob(newProject, api: fake)
        let prompted = await settle { session.newerJobPrompt != nil }
        XCTAssertTrue(prompted, "queued observation reaches the manual-edit decision")
        XCTAssertEqual(session.newerJobPrompt, newProject)
        XCTAssertTrue(session.hasUnsavedChanges, "no silent switch or discard")
        XCTAssertNotEqual(fake.editorVariantJobIDs.last, jobB)
        session.keepEditingCurrentJob()
        XCTAssertNil(session.newerJobPrompt)
        XCTAssertTrue(session.hasUnsavedChanges)
        await session.adoptLatestJob(newProject, api: fake)
        let promptedAgain = await settle { session.newerJobPrompt != nil }
        XCTAssertTrue(promptedAgain, "a new observation presents the decision again")
        XCTAssertNotNil(session.newerJobPrompt)
        fake.draftSnapshot = available
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
        let adopted = await settle {
            (session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent == "job-b.mp4"
                && session.loadState == .loaded && !session.hasUnsavedChanges
        }
        XCTAssertTrue(adopted, "queued observation finishes adopting the new job")
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
        let adopted = await settle {
            (session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent == "job-b.mp4"
                && session.loadState == .loaded && !session.hasUnsavedChanges
        }
        XCTAssertTrue(adopted, "queued observation finishes adopting the new job")
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testSupersededJobWithManualEditsPromptsInsteadOfSwitching() async throws {
        let (session, fake, jobB) = try await supersededFixture(manualEdit: true)
        await session.synchronizePromptRevision()
        let prompted = await settle { session.newerJobPrompt != nil }
        XCTAssertTrue(prompted, "queued observation reaches the manual-edit decision")
        XCTAssertNotNil(session.newerJobPrompt)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertNotEqual(fake.editorVariantJobIDs.last, jobB)
        XCTAssertEqual(fake.commitCount, 0)
    }

    private static func wordingDraft(from submitted: EditorDocument, text: String, revision: Int = 3, positionX: Double? = nil) throws -> DraftSnapshot {
        guard case let .array(rows)? = submitted.snapshot(for: .text) else {
            throw APIError.invalidResponse
        }
        let changed = rows.map { value -> JSONValue in
            guard case var .object(row) = value, row["id"] == .string("t1") else { return value }
            row["text"] = .string(text)
            if let positionX { row["x_frac"] = .number(positionX) }
            return .object(row)
        }
        return DraftSnapshot(draftID: "wording-\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision,
            snapshotHash: "wording", etag: "wording", baseJobID: Self.jobID, baseGenerationID: submitted.revision.baseGeneration,
            snapshot: ["kind": .string("editor"), "editor_payload": .object([
                "base_generation": .string(submitted.revision.baseGeneration), "text_elements": .array(changed)])],
            canUndo: true, createdAt: .now)
    }

    private func assertAuthoredDocument(_ actual: EditorDocument, equals expected: EditorDocument,
                                       file: StaticString = #filePath, line: UInt = #line) {
        for section in EditorSection.allCases {
            XCTAssertEqual(actual.snapshot(for: section), expected.snapshot(for: section), "\(section)", file: file, line: line)
        }
        XCTAssertEqual(actual.deletions, expected.deletions, file: file, line: line)
        XCTAssertEqual(actual.editorState, expected.editorState, file: file, line: line)
        XCTAssertEqual(actual.userSong, expected.userSong, file: file, line: line)
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
    func testProvenReplyEchoingManualFontDoesNotMakeThatFontChatOwned() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        session.setTextStyle(id: "t1", style: "Inter")
        let submitted = session.document
        let sent = try XCTUnwrap(session.exportEditorState())
        fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: submitted, text: "first reply", revision: 2))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "first reply")
        XCTAssertEqual(session.document.textElements.first?.raw["font_family"], .string("Inter"))
        XCTAssertFalse(session.hasOnlyChatStagedChanges, "echoing an existing manual font does not make that font chat-owned")
        let before = session.document
        let history = session.undoHistoryCount
        var overwritten = before
        overwritten.textElements[0].raw["font_family"] = .string("Courier")
        fake.draftSnapshot = Self.echoing("unknown-cumulative-state", try Self.wordingDraft(from: overwritten, text: "second reply", revision: 3))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "second reply", "the AI's newer reply is shown")
        XCTAssertEqual(session.undoHistoryCount, history + 1)
        session.undo()
        XCTAssertEqual(session.document, before, "Undo restores the creator's own edit")
        await session.save()
        XCTAssertEqual(fake.commitCount, 1, "no state blocks Save")
    }

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
        let submitted = session.document
        let sent = try XCTUnwrap(session.exportEditorState())
        session.setClipTiming(clipID: "s1", durationS: 1.2)
        let local = session.document.clips.first { $0.id == "s1" }?.durationS
        fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: submitted, text: "from chat", revision: 2))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.text, "from chat")
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, local)
        XCTAssertEqual(session.dirtySections, [.timeline, .text])
        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "hello")
        XCTAssertEqual(session.document.clips.first { $0.id == "s1" }?.durationS, local, "undo restores the state at apply time")
    }

    /// Different authored fields on the same entity merge; sharing a lane is not a conflict.
    func testPostSubmitStyleAndChatWordingOnSameEntityMergeWithCompleteUndo() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let submitted = session.document
        let sent = try XCTUnwrap(session.exportEditorState())
        session.setTextStyle(id: "t1", style: "Inter")
        let before = session.document
        fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: submitted, text: "from chat", revision: 2))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .idle)
        XCTAssertEqual(session.document.textElements.first?.text, "from chat")
        XCTAssertEqual(session.document.textElements.first?.raw["font_family"], .string("Inter"))
        let applied = session.document
        session.undo()
        assertAuthoredDocument(session.document, equals: before)
        XCTAssertEqual(session.document.revision, before.revision)
        await session.synchronizePromptRevision()
        assertAuthoredDocument(session.document, equals: before)
        session.redo()
        assertAuthoredDocument(session.document, equals: applied)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testPostSubmitOverlappingWordingChatWinsKeepsIndependentFieldsAndUndoRestoresMine() async throws {
        for provenance in ["submitted", "missing", "unknown"] {
            let (session, fake) = await loaded(Self.bootstrapSnapshot())
            let submitted = session.document
            let sent = try XCTUnwrap(session.exportEditorState())
            session.updateTextContent(id: "t1", content: "local after submission")
            session.setTextStyle(id: "t1", style: "Inter")
            let before = session.document
            let response = try Self.wordingDraft(from: submitted, text: "chat wording", positionX: 0.8)
            switch provenance {
            case "submitted": fake.draftSnapshot = Self.echoing(sent.clientStateID, response)
            case "unknown": fake.draftSnapshot = Self.echoing("not-this-session", response)
            default: fake.draftSnapshot = response
            }
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.document.textElements.first?.text, "chat wording", provenance)
            XCTAssertEqual(session.document.textElements.first?.raw["font_family"], .string("Inter"), "independent local style is kept")
            XCTAssertEqual(session.document.textElements.first?.raw["x_frac"], .number(0.8), "independent chat positioning is kept")
            XCTAssertEqual(session.document.revision.baseGeneration, "g1")
            XCTAssertEqual(fake.commitCount, 0, "converging stages; it does not save")
            let applied = session.document
            session.undo()
            assertAuthoredDocument(session.document, equals: before)
            await session.synchronizePromptRevision()
            assertAuthoredDocument(session.document, equals: before)
            session.redo()
            assertAuthoredDocument(session.document, equals: applied)
            await session.save()
            XCTAssertEqual(fake.commitCount, 1, "no state blocks Save: \(provenance)")
        }
    }

    func testPostSubmitTimelineAndChatTimingChatWinsAndUndoRestoresMyTrim() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let sent = try XCTUnwrap(session.exportEditorState())
        session.setClipTiming(clipID: "s1", durationS: 1.2)
        let before = session.document
        fake.draftSnapshot = Self.echoing(sent.clientStateID, Self.chatSnapshot(textOnly: true))
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document.textElements.first?.endS, 3600, "the chat payload's text timing is shown")
        XCTAssertTrue(session.canUndo)
        session.undo()
        XCTAssertEqual(session.document, before)
        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
    }

    func testConvergeReadInFlightWhenTheEditorLoadsAnotherEditIsDropped() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        let submitted = session.document
        let sent = try XCTUnwrap(session.exportEditorState())
        session.updateTextContent(id: "t1", content: "local after submission")
        fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: submitted, text: "old chat result"))
        fake.suspendNextEditorVariant = true
        let reading = Task { await session.synchronizePromptRevision() }
        let held = await settle { fake.editorVariantIsSuspended }
        XCTAssertTrue(held)
        let replacementJob = UUID(), replacementThread = UUID()
        let replacementAPI = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "replacement", itemID: "replacement-item", variantKey: "initial",
            draftRevision: 1, snapshotHash: "replacement", etag: "replacement", baseJobID: replacementJob.uuidString,
            baseGenerationID: "replacement-generation", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(generation: "replacement-generation"))
        await session.load(api: replacementAPI, threadID: replacementThread)
        let replacement = session.document
        fake.resumeEditorVariant()
        await reading.value
        XCTAssertEqual(session.document, replacement, "a read that started before the load cannot converge into the new editor")
        XCTAssertNil(session.convergeNotice)
        XCTAssertEqual(fake.commitCount + replacementAPI.commitCount, 0)
    }

    func testChatReadFinishingDuringTransactionOrTrimDefersUntilGestureEnds() async throws {
        for gesture in ["transaction", "trim"] {
            let (session, fake) = await loaded(Self.bootstrapSnapshot())
            let submitted = session.document
            let sent = try XCTUnwrap(session.exportEditorState())
            fake.draftSnapshot = Self.echoing(sent.clientStateID, try Self.wordingDraft(from: submitted, text: "after gesture"))
            fake.suspendNextEditorVariant = true
            let reading = Task { await session.synchronizePromptRevision() }
            let held = await settle { fake.editorVariantIsSuspended }
            XCTAssertTrue(held)
            if gesture == "transaction" {
                session.beginTransaction()
                session.setTextStyle(id: "t1", style: "Inter")
            } else {
                session.beginTrim(clipID: try XCTUnwrap(session.timelineClips.first?.id), edge: .trailing)
                session.updateTrim(by: -0.2)
            }
            let duringGesture = session.document
            let historyDuringGesture = session.undoHistoryCount
            fake.resumeEditorVariant()
            await reading.value
            XCTAssertEqual(session.document, duringGesture, "the read must not fall through into rebase while \(gesture) owns edits")
            XCTAssertEqual(session.undoHistoryCount, historyDuringGesture)
            XCTAssertEqual(fake.commitCount, 0)
            if gesture == "transaction" { session.endTransaction() } else { session.endTrim() }
            let beforeApplication = session.document
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.document.textElements.first?.text, "after gesture")
            if gesture == "transaction" {
                XCTAssertEqual(session.document.textElements.first?.raw["font_family"], .string("Inter"))
            } else {
                XCTAssertEqual(session.document.snapshot(for: .timeline), beforeApplication.snapshot(for: .timeline))
            }
            session.undo()
            assertAuthoredDocument(session.document, equals: beforeApplication)
            XCTAssertEqual(fake.commitCount, 0)
        }
    }

    func testDraftWithAnUnknownStateIDFallsBackToTheLegacyHeadLogic() async throws {
        let (session, fake) = await loaded(Self.bootstrapSnapshot())
        XCTAssertFalse(session.hasUnsavedChanges, "no local authored edit needs overwrite permission")
        fake.draftSnapshot = Self.echoing("not-ours", Self.chatSnapshot())
        await session.synchronizePromptRevision()
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "legacy staging path")
        XCTAssertEqual(fake.commitCount, 0)
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
        let (session, fake, newProject, jobB) = try await replanFixture(manualEdit: false)
        session.newVersionPollDelays = [.milliseconds(20)]
        fake.editorVariantsByJob[jobB] = []
        await session.adoptLatestJob(newProject, api: fake)
        let rendering = await settle { session.refreshSync == .newVersionRendering }
        XCTAssertTrue(rendering, "queued observation recognizes the unfinished render")
        func playing() -> String? { (session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent }
        XCTAssertEqual(playing(), "job-a.mp4", "the working video must not be thrown away")
        XCTAssertEqual(session.loadState, .loaded)
        XCTAssertNil(session.newerJobPrompt, "nothing is ready to switch to")
        XCTAssertEqual(session.refreshSync, .newVersionRendering)
        XCTAssertNotEqual(fake.editorVariantJobIDs.last, jobB)

        var ready = Self.variant(generation: "gNew"); ready["output_url"] = .string("https://example.com/job-b.mp4")
        fake.editorVariantsByJob[jobB] = [ready]
        let switched = await settle {
            playing() == "job-b.mp4" && session.loadState == .loaded && session.refreshSync == .idle
        }
        XCTAssertTrue(switched, "the watch adopts the new job on its own once it is ready")
        XCTAssertEqual(session.refreshSync, .idle)
        XCTAssertEqual(session.loadState, .loaded)
    }

    func testReplanStillRenderingWithManualEditsMergesOnlyOnceItIsReady() async throws {
        let (session, fake, newProject, jobB) = try await replanFixture(manualEdit: true)
        session.newVersionPollDelays = [.milliseconds(20)]
        fake.editorVariantsByJob[jobB] = [["variant_id": .string("initial"), "render_status": .string("rendering")]]
        await session.adoptLatestJob(newProject, api: fake)
        let rendering = await settle { session.refreshSync == .newVersionRendering }
        XCTAssertTrue(rendering, "queued observation recognizes the unfinished render")
        XCTAssertNil(session.newerJobPrompt, "'A new version is ready' would be false while it renders")
        XCTAssertTrue(session.hasUnsavedChanges)
        let before = session.document
        fake.editorVariantsByJob[jobB] = [Self.variant(generation: "gNew")]
        let merged = await settle { session.document.revision.baseGeneration == "gNew" }
        XCTAssertTrue(merged)
        XCTAssertNil(session.newerJobPrompt)
        assertAuthoredDocument(session.document, equals: before)
        XCTAssertTrue(session.hasUnsavedChanges, "automatic adoption preserves manual edits")
        XCTAssertFalse(session.isBehindActiveJob(newProject))
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testChatSyncDuringAReplanRenderShowsTheNeutralChipNotAFailure() async throws {
        let (session, fake, newProject, jobB) = try await replanFixture(manualEdit: false)
        session.refreshRetryDelays = [.seconds(3600)]
        session.newVersionPollDelays = [.seconds(3600)]
        // The draft follows the plan item to the new job, which has no variants yet (drafts.py:270-285).
        fake.draftError = Self.conflict("draft_variant_ambiguous")
        fake.refreshedThread = try decodeThread(newProject.id, job: jobB)
        fake.editorVariantsByJob[jobB] = []
        await session.synchronizePromptRevision()
        let rendering = await settle { session.refreshSync == .newVersionRendering }
        XCTAssertTrue(rendering, "queued observation recognizes the unfinished render")
        XCTAssertEqual(session.refreshSync, .newVersionRendering)
        XCTAssertEqual(session.saveState, .idle)
        XCTAssertEqual((session.player?.currentItem?.asset as? AVURLAsset)?.url.lastPathComponent, "job-a.mp4")
    }

    func testFailedNewRenderLeavesTheCurrentVideoAndNoChip() async throws {
        let (session, fake, newProject, jobB) = try await replanFixture(manualEdit: false)
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

    func testFirstOpenRecoversAfterFastBudgetAndWaitsWhileHidden() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        fake.editorVariantScript = Array(repeating: .failure(.offline), count: 6)
        let session = NativeEditorSession()
        session.loadRetryDelays = [.zero]
        var now = ContinuousClock.now
        var paused = false
        session.loadReadNow = { now }
        session.loadReadSleep = { delay in
            now = now.advanced(by: delay)
            if delay >= .seconds(300), !paused { paused = true; session.setObserversVisible(false) }
            await Task.yield()
        }
        await session.load(api: fake, threadID: UUID())
        let reachedPause = await settle { paused }; XCTAssertTrue(reachedPause)
        let reads = fake.editorVariantJobIDs.count
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(fake.editorVariantJobIDs.count, reads)
        session.setObserversVisible(true)
        let opened = await settle { session.loadState == .loaded }; XCTAssertTrue(opened)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testLateOldLoadRetryAfterCannotDelayAnotherTargetOrAccount() async throws {
        for (changeAccount, sameTarget) in [(false, false), (true, true), (false, true)] {
            let old = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
            old.mediaLibraryOwner = "first"
            old.suspendNextEditorVariant = true
            let replacement = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant(generation: "g2"))
            replacement.mediaLibraryOwner = changeAccount ? "second" : "first"
            let session = NativeEditorSession()
            let thread = UUID()
            var delays: [Duration] = []
            var now = ContinuousClock.now
            session.loadReadNow = { now }
            session.loadReadSleep = { delay in delays.append(delay); now = now.advanced(by: delay); await Task.yield() }
            let oldLoad = Task { await session.load(api: old, threadID: thread, variantID: "initial") }
            let suspended = await settle { old.editorVariantIsSuspended }
            XCTAssertTrue(suspended)
            let newVariant = sameTarget ? "initial" : "other"
            var replacementFinished = false
            let newLoad = Task {
                await session.load(api: replacement, threadID: thread, variantID: newVariant)
                replacementFinished = true
            }
            let replaced = await settle { replacementFinished }
            XCTAssertTrue(replaced, "a replacement epoch cannot wait for an obsolete transport")
            XCTAssertTrue(old.editorVariantIsSuspended, "the original GET is still held while replacement completes")
            XCTAssertEqual(replacement.editorVariantJobIDs.count, 1)
            XCTAssertEqual(session.loadState, .loaded)
            XCTAssertEqual(session.document.revision.baseGeneration, "g2")
            old.editorVariantError = .requestFailed(status: 429, detail: .init("old target", retryAfter: 600))
            old.resumeEditorVariant()
            await oldLoad.value; await newLoad.value
            XCTAssertEqual(session.loadState, .loaded)
            XCTAssertEqual(session.document.revision.baseGeneration, "g2")
            XCTAssertEqual(replacement.lastVariantID, newVariant)
            await session.load(api: replacement, threadID: thread, variantID: newVariant)
            XCTAssertEqual(session.loadState, .loaded)
            XCTAssertEqual(session.document.revision.baseGeneration, "g2")
            XCTAssertTrue(delays.isEmpty, "an obsolete epoch cannot install a late deadline, even for the same owner and target")
            XCTAssertEqual(old.commitCount + replacement.commitCount, 0)
            session.setObserversVisible(false)
        }
    }

    func testObserverCooldownKeysSeparateOwnerEnvironmentAndTargetButNotLifetime() {
        let a = NativeEditorSession.ObservationScope(owner: "owner", environment: "https://a.test", target: "variant:A")
        XCTAssertEqual(a, NativeEditorSession.ObservationScope(owner: "owner", environment: "https://a.test", target: "variant:A"))
        XCTAssertNotEqual(a, NativeEditorSession.ObservationScope(owner: "other", environment: "https://a.test", target: "variant:A"))
        XCTAssertNotEqual(a, NativeEditorSession.ObservationScope(owner: "owner", environment: "https://b.test", target: "variant:A"))
        XCTAssertNotEqual(a, NativeEditorSession.ObservationScope(owner: "owner", environment: "https://a.test", target: "variant:B"))
    }

    func testFirstOpenRetainsSixHundredSecondReadDeadline() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        fake.editorVariantScript = [.failure(.requestFailed(status: 429, detail: .init("wait", retryAfter: 600)))]
        let session = NativeEditorSession()
        var now = ContinuousClock.now
        var waits: [Duration] = []
        session.loadReadNow = { now }
        session.loadReadSleep = { delay in waits.append(delay); now = now.advanced(by: delay); await Task.yield() }
        await session.load(api: fake, threadID: UUID())
        let opened = await settle { session.loadState == .loaded }; XCTAssertTrue(opened)
        XCTAssertTrue(waits.contains(.seconds(600)))
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testAcknowledgedGalleryPromotionUsesOnlyGETsAfterReadExhaustion() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(),
            openReceipt: OpenInEditorResponse(planItemID: "item", variantID: "initial"), authoritativeVariant: Self.variant())
        fake.editorVariantScript = Array(repeating: .failure(.offline), count: 6)
        let session = NativeEditorSession()
        session.loadRetryDelays = [.zero]
        var now = ContinuousClock.now
        session.loadReadNow = { now }
        session.loadReadSleep = { delay in now = now.advanced(by: delay); await Task.yield() }
        await session.load(libraryJobID: UUID(uuidString: Self.jobID)!, api: fake)
        let opened = await settle { session.loadState == .loaded }; XCTAssertTrue(opened)
        XCTAssertEqual(fake.openCallCount, 1)
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testKnownNotReadyGalleryOpenObservesPastEightyChecksBeforeFinishingPromotion() async throws {
        let job = UUID(uuidString: Self.jobID)!
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(),
            openReceipt: OpenInEditorResponse(planItemID: "item", variantID: "initial"), authoritativeVariant: Self.variant())
        fake.openErrors = [.editorNotReady]
        fake.editorVariantsByJob[job] = [["variant_id": .string("initial"), "render_status": .string("rendering")]]
        let session = NativeEditorSession()
        session.loadRenderingDelays = [.zero]
        var now = ContinuousClock.now
        var reachedSlow = false
        session.loadReadNow = { now }
        session.loadReadSleep = { delay in
            now = now.advanced(by: delay)
            if delay >= .seconds(300) {
                reachedSlow = true
                fake.editorVariantsByJob[job] = [Self.variant()]
            }
            await Task.yield()
        }
        await session.load(libraryJobID: job, api: fake)
        let opened = await settle { session.loadState == .loaded }; XCTAssertTrue(opened)
        XCTAssertTrue(reachedSlow)
        XCTAssertGreaterThanOrEqual(fake.editorVariantsCallCount, 80)
        XCTAssertEqual(fake.openCallCount, 2, "one rejected attempt, then one original authorized open after GET readiness")
        XCTAssertEqual(fake.commitCount, 0)
    }

    func testGalleryReadinessStopsForTerminalVariantsButReadyStillWins() async throws {
        let job = UUID(uuidString: Self.jobID)!
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(), authoritativeVariant: Self.variant())
        for status in ["failed", "cancelled", "superseded", "needs_attention"] {
            fake.editorVariantsByJob[job] = [["variant_id": .string("initial"), "render_status": .string(status)]]
            let terminal = await NativeEditorSession.readiness(ofJob: job, preferredVariantID: nil, api: fake)
            XCTAssertEqual(terminal, .failed, status)
            fake.editorVariantsByJob[job]?.append(["variant_id": .string("other"), "render_status": .string("ready")])
            let ready = await NativeEditorSession.readiness(ofJob: job, preferredVariantID: nil, api: fake)
            XCTAssertEqual(ready, .ready, status)
            let selectedTerminal = await NativeEditorSession.readiness(ofJob: job, preferredVariantID: "initial", api: fake)
            XCTAssertEqual(selectedTerminal, .failed, "another ready variant cannot replace the selected terminal one")
        }
    }

    func testUncertainGalleryPromotionDoesNotAutomaticallyReplayPOST() async throws {
        let fake = EditorCommitSpy(draftSnapshot: Self.bootstrapSnapshot(),
            openReceipt: OpenInEditorResponse(planItemID: "item", variantID: "initial"), authoritativeVariant: Self.variant())
        fake.openErrors = [.offline]
        let session = NativeEditorSession()
        session.loadReadSleep = { _ in await Task.yield() }
        await session.load(libraryJobID: UUID(uuidString: Self.jobID)!, api: fake)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(fake.openCallCount, 1)
        guard case .failed = session.loadState else { return XCTFail("unknown promotion must remain explicit") }
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

    func testExhaustedSavedPreviewRecoversWithoutCheckAndPausesWhileHidden() async throws {
        let (session, fake) = await savedSession()
        session.previewPollBudget = 2
        var now = ContinuousClock.now
        var paused = false
        session.previewReadNow = { now }
        session.previewReadSleep = { delay in
            now = now.advanced(by: delay)
            if delay >= .seconds(300), !paused { paused = true; session.setObserversVisible(false) }
            await Task.yield()
        }
        session.retryPreviewRefresh()
        let reachedPause = await settle { paused }; XCTAssertTrue(reachedPause)
        let reads = fake.editorVariantJobIDs.count
        fake.authoritativeVariant = Self.variant(generation: "g2")
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(fake.editorVariantJobIDs.count, reads)
        session.setObserversVisible(true)
        let ready = await settle { session.saveState == .saved }; XCTAssertTrue(ready)
        XCTAssertEqual(fake.commitCount, 1)
    }

    func testSavedPreviewRetainsSixHundredSecondReadDeadline() async throws {
        let (session, fake) = await savedSession()
        session.previewPollInterval = .zero
        var now = ContinuousClock.now
        var waits: [Duration] = []
        session.previewReadNow = { now }
        session.previewReadSleep = { delay in waits.append(delay); now = now.advanced(by: delay); await Task.yield() }
        fake.editorVariantScript = [.failure(.requestFailed(status: 429, detail: .init("wait", retryAfter: 600)))]
        fake.authoritativeVariant = Self.variant(generation: "g2")
        session.retryPreviewRefresh()
        let ready = await settle { session.saveState == .saved }; XCTAssertTrue(ready)
        XCTAssertTrue(waits.contains(.seconds(600)))
        XCTAssertEqual(fake.commitCount, 1)
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
    // MARK: - One definition of the variant's current generation (B4)

    /// A variant that never went through the editor has no `render_generation_id`; its baseline is `render_finished_at`.
    private static func finishTimeOnlyVariant(_ finishedAt: String = "2026-09-10T10:00:00Z") -> [String: JSONValue] {
        var value = variant()
        value["render_generation_id"] = nil
        value["render_finished_at"] = .string(finishedAt)
        return value
    }

    private func stage(base: String, headCurrent: Bool?, variant: [String: JSONValue]) -> DraftSnapshot.StagedChatEdit? {
        var head = Self.chatSnapshot(base: base, shape: .flatOnly)
        head.headCurrent = headCurrent
        let input = Self.bootstrapSnapshot().editorDraft(projectID: UUID(), authoritativeVariant: variant).serverSnapshot
        return head.stagedChatEdit(over: input, variant: variant, appliedRevision: nil)
    }

    func testHeadBasedOnTheVariantsFinishTimeIsStaged() {
        let finished = "2026-09-10T10:00:00Z"
        XCTAssertNotNil(stage(base: finished, headCurrent: nil, variant: Self.finishTimeOnlyVariant(finished)),
                        "the finish time is the baseline when there is no generation id")
    }

    func testHeadMarkedCurrentByTheServerIsAcceptedWhateverItsBase() {
        XCTAssertNotNil(stage(base: "source-generation", headCurrent: true, variant: Self.variant(generation: "upload-attempt")),
                        "a head the server certified is never dropped by a phone-side re-derivation")
    }

    func testHeadWithoutTheFlagOrMarkedStaleIsStillComparedAgainstTheBaseline() {
        XCTAssertNil(stage(base: "old", headCurrent: nil, variant: Self.variant(generation: "new")), "older server, stale head")
        XCTAssertNil(stage(base: "old", headCurrent: false, variant: Self.variant(generation: "new")), "server says not current")
        XCTAssertNotNil(stage(base: "same", headCurrent: false, variant: Self.variant(generation: "same")),
                        "not-current does not veto a head whose base matches the baseline")
    }

    func testDraftSnapshotDecodesTheServersHeadCurrentFields() throws {
        func decode(_ extra: String) throws -> DraftSnapshot {
            let json = """
            {"draft_id":"d","item_id":"i","variant_key":"v","draft_revision":1,"snapshot_hash":"h","etag":"e",
             "base_job_id":null,"base_generation_id":"g","snapshot":{},"can_undo":false,"created_at":"2026-09-10T10:00:00Z"\(extra)}
            """
            let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
            return try decoder.decode(DraftSnapshot.self, from: Data(json.utf8))
        }
        let current = try decode(#","head_current":true,"base_generation":"g2""#)
        XCTAssertEqual(current.headCurrent, true)
        let older = try decode("")
        XCTAssertNil(older.headCurrent, "an older server omits the field")
    }

    func testVariantBaselineFollowsTheServersRule() {
        XCTAssertEqual(variantBaseline(["render_generation_id": .string("g"), "render_finished_at": .string("t")]), "g")
        XCTAssertEqual(variantBaseline(["render_generation_id": .string(""), "render_finished_at": .string("t")]), "t")
        XCTAssertEqual(variantBaseline(["render_finished_at": .string("t")]), "t")
        XCTAssertEqual(variantBaseline([:]), "")
        XCTAssertEqual(variantBaseline(nil), "")
    }
}
