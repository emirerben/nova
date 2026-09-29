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

    /// Chat draft in the server's real shape: flat commit-shaped keys for the
    /// lanes chat touched + the nested `sections` overlaid with the same state.
    private static func chatPayload(base: String = "g1", textOnly: Bool = false) -> [String: JSONValue] {
        func wire(_ id: String, _ index: Double, _ duration: Double, removed: Bool = false) -> JSONValue {
            .object(["slot_id": .string(id), "clip_index": .number(index), "in_s": .number(0), "duration_s": .number(duration),
                     "removed": .bool(removed), "look_preset": .string("none"), "transition_after": .string("cut")])
        }
        let slots: [JSONValue] = [wire("s3", 2, 2), wire("s1", 0, 1.5), wire("s2", 1, 2, removed: true)]
        let text: JSONValue = .array([.object(["id": .string("t1"), "text": .string("changed"), "start_s": .number(0), "end_s": .number(2),
                                               "role": .string("generative_intro"), "x_frac": .number(0.5), "y_frac": .number(0.2),
                                               "color": .null, "glow_color": .null])])
        let mix: JSONValue = .object(["music_level": .number(0.3)])
        var payload: [String: JSONValue] = [
            "base_generation": .string(base), "text_elements": text,
            "sections": .object([
                "text_elements": text,
            ]),
        ]
        if !textOnly {
            payload["timeline_slots"] = .array(slots); payload["mix"] = mix; payload["remove_music"] = .bool(true)
            payload["sections"] = .object([
                "timeline_slots": .array(slots), "text_elements": text, "mix": mix, "music_track_id": .null,
                "music_window": .object(["start_s": .number(1), "alignment": .string("preserve_cuts")]),
            ])
        }
        return payload
    }

    private static func chatSnapshot(base: String = "g1", revision: Int = 3, textOnly: Bool = false) -> DraftSnapshot {
        DraftSnapshot(draftID: "d\(revision)", itemID: "item", variantKey: "initial", draftRevision: revision,
            snapshotHash: "h", etag: "e", baseJobID: Self.jobID, baseGenerationID: base,
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "edit_format": .string("montage"),
                       "editor_payload": .object(chatPayload(base: base, textOnly: textOnly))], canUndo: true, createdAt: .now)
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
}
