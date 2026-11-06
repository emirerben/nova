import XCTest
import KriaMediaEngine
@testable import Kria

final class NativeEditorMergeTests: XCTestCase {
    func testLatestReadWindowSchedulesSixtySecondHintAndResumesAtDeadline() {
        let now = ContinuousClock.now
        let window = NativeEditorReadWindow(started: now)
        var cooldown = NativeEditorReadCooldown()
        cooldown.record(seconds: 60, now: now)
        XCTAssertEqual(window.action(cooldown: cooldown, now: now), .wait(.seconds(60)))
        XCTAssertEqual(window.action(cooldown: cooldown, now: now.advanced(by: .seconds(59))), .wait(.seconds(1)))
        XCTAssertEqual(window.action(cooldown: cooldown, now: now.advanced(by: .seconds(60))), .ready)
    }

    func testLatestReadWindowBoundsCumulativeExtendedHints() {
        let now = ContinuousClock.now
        let window = NativeEditorReadWindow(started: now)
        var cooldown = NativeEditorReadCooldown()
        cooldown.record(seconds: 240, now: now)
        XCTAssertEqual(window.action(cooldown: cooldown, now: now), .wait(.seconds(240)))
        cooldown.record(seconds: 90, now: now.advanced(by: .seconds(240)))
        XCTAssertEqual(window.action(cooldown: cooldown, now: now.advanced(by: .seconds(240))), .paused)
        XCTAssertEqual(cooldown.delay(now: now.advanced(by: .seconds(240))), .seconds(90), "Pausing never shortens the server deadline")
    }

    func testLatestReadWindowRetainsCancellationOwnershipAcrossWake() {
        var gate = NativeEditorLatestGate()
        let thread = UUID()
        let old = gate.enqueue(threadID: thread)
        let now = ContinuousClock.now
        var cooldown = NativeEditorReadCooldown()
        cooldown.record(seconds: 60, now: now)
        XCTAssertEqual(NativeEditorReadWindow(started: now).action(cooldown: cooldown, now: now), .wait(.seconds(60)))
        gate.reset()
        let replacement = gate.enqueue(threadID: thread)
        XCTAssertFalse(gate.matches(old), "An eligible deadline cannot restore ownership to an old load")
        gate.finish(old)
        XCTAssertEqual(gate.pending, replacement)
    }

    func testLegacyDeleteKeepsUnrelatedRemoteEdit() {
        let row: JSONValue = .object(["id": .string("a"), "text": .string("old"), "start_s": .number(0), "end_s": .number(1)])
        let base = EditorDocument(snapshot: ["text_elements": .array([row]), "title": .string("old")])
        var local = base, remote = base
        local.textElements = []
        local.deletions = [.init(kind: "text", id: "a")]
        remote.title = "latest"
        let result = NativeEditorMerge.documents(base: base, local: local, remote: remote, baseSong: nil, remoteSong: nil)
        guard case .resolved(let merged) = result else { return XCTFail("Unrelated remote title must survive a legacy deletion") }
        XCTAssertEqual(merged.deletions, local.deletions)
        XCTAssertTrue(merged.textElements.isEmpty)
        XCTAssertEqual(merged.title, "latest")
    }

    func testDeleteAndEditDifferentTextEntitiesMergeWithoutChoice() {
        let a: JSONValue = .object(["id": .string("a"), "text": .string("a")])
        let b: JSONValue = .object(["id": .string("b"), "text": .string("b")])
        let base = state(["text_elements": .array([a, b])])
        var local = base, remote = base
        local.sections["text_elements"] = .array([b])
        local.deletions = .array([.object(["kind": .string("text"), "id": .string("a")])])
        let edited: JSONValue = .object(["id": .string("b"), "text": .string("new")])
        remote.sections["text_elements"] = .array([a, edited])
        let result = NativeEditorMerge.merge(base: base, local: local, remote: remote)
        XCTAssertEqual(result.state?.sections["text_elements"], .array([edited]))
        XCTAssertEqual(result.state?.deletions, local.deletions)
    }

    func testKeepingRemoteEditedEntityDropsConflictingLocalDeletionIntent() {
        let base = state(["text_elements": .array([.object(["id": .string("a"), "text": .string("old")])])])
        var local = base, remote = base
        local.sections["text_elements"] = .array([])
        local.deletions = .array([.object(["kind": .string("text"), "id": .string("a")])])
        remote.sections["text_elements"] = .array([.object(["id": .string("a"), "text": .string("edited")])])
        let result = NativeEditorMerge.merge(base: base, local: local, remote: remote, preference: .remote)
        XCTAssertEqual(result.state?.sections["text_elements"], remote.sections["text_elements"])
        XCTAssertNil(result.state?.deletions)
    }

    func testUnchangedRemoteOpaqueEntityWithIDIsPreserved() {
        let remote = EditorDocument(snapshot: ["text_elements": .array([.object(["id": .string("future"), "future_extension": .bool(true)])])])
        XCTAssertNotNil(remote.mergeValidationSnapshot(preservingOpaqueFrom: remote))
    }

    func testLateRetryHintSurvivesLaterSuccessAndHugeHintPauses() {
        let now = ContinuousClock.now
        var cooldown = NativeEditorReadCooldown()
        cooldown.record(seconds: 60, now: now)
        cooldown.prune(now: now.advanced(by: .seconds(3)))
        cooldown.record(seconds: 2, now: now.advanced(by: .seconds(3)))
        XCTAssertEqual(cooldown.delay(now: now), .seconds(60))
        cooldown.prune(now: now.advanced(by: .seconds(60)))
        XCTAssertEqual(cooldown.delay(now: now.advanced(by: .seconds(60))), .zero)
        cooldown.record(seconds: Double.greatestFiniteMagnitude, now: now)
        XCTAssertTrue(cooldown.isPaused)
    }

    func testTypedValidationRejectsDuplicateRealIdentity() {
        let row: JSONValue = .object(["id": .string("a"), "text": .string("text"), "start_s": .number(0), "end_s": .number(1)])
        let value = EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(["text_elements": .array([row, row])])])])
        XCTAssertNil(value.mergeValidationSnapshot())
    }

    func testBackgroundMusicSourceAndWindowAreOneConflict() {
        let base = state(["background_music": .object(["track_id": .string("a"), "start_s": .number(0), "end_s": .number(10)])])
        var local = base, remote = base
        local.sections["background_music"] = .object(["track_id": .string("a"), "start_s": .number(8), "end_s": .number(10)])
        remote.sections["background_music"] = .object(["track_id": .string("b"), "start_s": .number(0), "end_s": .number(5)])
        XCTAssertNil(NativeEditorMerge.merge(base: base, local: local, remote: remote).state)
        XCTAssertEqual(NativeEditorMerge.merge(base: base, local: local, remote: remote, preference: .remote).state?.sections["background_music"], remote.sections["background_music"])
    }

    func testTypedValidationDoesNotSilentlyDropMalformedDeletion() {
        let malformed = EditorDocument(snapshot: ["editor_payload": .object([
            "sections": .object([:]), "deletions": .array([.object(["id": .string("a")])])
        ])])
        let result = NativeEditorMerge.documents(base: malformed, local: malformed, remote: malformed, baseSong: nil, remoteSong: nil)
        if case .resolved = result { XCTFail("A malformed deletion is not an empty deletion list") }
    }

    func testTypedBridgeMergesTextAndStyleWithoutReplacingRemoteEnvelope() {
        func doc(_ text: String, _ role: String, revision: Int) -> EditorDocument {
            var value = EditorDocument(snapshot: ["opaque_envelope": .number(Double(revision)), "editor_payload": .object(["sections": .object([
                "text_elements": .array([.object(["id": .string("a"), "text": .string(text), "role": .string(role), "start_s": .number(0), "end_s": .number(2)])])
            ])])])
            value.revision.number = revision
            return value
        }
        let result = NativeEditorMerge.documents(base: doc("old", "title", revision: 1),
            local: doc("mine", "title", revision: 1), remote: doc("old", "subtitle", revision: 2), baseSong: nil, remoteSong: nil)
        guard case .resolved(let merged) = result else { return XCTFail("Valid disjoint authored fields should merge") }
        XCTAssertEqual(merged.textElements.first?.text, "mine")
        XCTAssertEqual(merged.textElements.first?.role, "subtitle")
        XCTAssertEqual(merged.revision.number, 2)
        XCTAssertEqual(merged.encodeSnapshot()["opaque_envelope"], .number(2))
    }

    func testDocumentBridgeRejectsInvalidCombinedInterval() {
        func doc(_ start: Double, _ end: Double) -> EditorDocument {
            EditorDocument(snapshot: ["editor_payload": .object(["sections": .object([
                "text_elements": .array([.object(["id": .string("a"), "text": .string("text"), "start_s": .number(start), "end_s": .number(end)])])
            ])])])
        }
        let result = NativeEditorMerge.documents(base: doc(0, 10), local: doc(8, 10), remote: doc(0, 5), baseSong: nil, remoteSong: nil)
        if case .resolved = result { XCTFail("Disjoint endpoints must not produce a backwards interval") }
    }

    func testDocumentBridgeRefusesOldSongEditOnNewAuthority() {
        let base = EditorDocument(snapshot: [:])
        var local = base
        local.userSong = EditorUserSongState(volume: 0.5)
        let result = NativeEditorMerge.documents(base: base, local: local, remote: base,
            baseSong: .string("a"), remoteSong: .string("b"), preference: .local)
        if case .resolved = result { XCTFail("Keeping an old song overlay cannot target a different song") }
    }

    func testTypedValidationRejectsMalformedKnownTextRow() {
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object([
            "text_elements": .array([.object(["id": .string("a"), "text": .number(7)])])
        ])])]
        XCTAssertNil(EditorDocument(snapshot: snapshot).mergeValidationSnapshot())
    }

    func testTypedValidationPreservesClearlyOpaqueFutureRow() {
        let future: JSONValue = .object(["future_extension": .object(["token": .string("keep")])])
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object([
            "text_elements": .array([future])
        ])])]
        let encoded = EditorDocument(snapshot: snapshot).mergeValidationSnapshot()
        XCTAssertEqual(encoded?["editor_payload"]?.objectValue?["sections"]?.objectValue?["text_elements"], .array([future]))
    }

    func testLatestJobTicketRetainsPendingWorkUntilMatchingCompletion() {
        var gate = NativeEditorLatestGate()
        let thread = UUID()
        let first = gate.enqueue(threadID: thread)
        let newest = gate.enqueue(threadID: thread)
        gate.finish(first)
        XCTAssertEqual(gate.pending, newest)
        gate.finish(newest)
        XCTAssertNil(gate.pending)
    }

    func testLatestJobTicketCannotSurviveLoadLifetimeChange() {
        var gate = NativeEditorLatestGate()
        let old = gate.enqueue(threadID: UUID())
        gate.reset()
        XCTAssertFalse(gate.matches(old))
        let next = gate.enqueue(threadID: UUID())
        gate.finish(old)
        XCTAssertEqual(gate.pending, next)
    }

    private func state(_ fields: [String: JSONValue]) -> NativeEditorMerge.State {
        .init(sections: fields, editorState: nil, deletions: nil, userSong: nil)
    }

    func testDisjointEntityFieldsPreserveRemoteOpaqueFields() {
        let base = state(["text_elements": .array([.object(["id": .string("a"), "text": .string("old"), "color": .string("white")])])])
        var local = base, remote = base
        local.sections["text_elements"] = .array([.object(["id": .string("a"), "text": .string("mine"), "color": .string("white")])])
        remote.sections["text_elements"] = .array([.object(["id": .string("a"), "text": .string("old"), "color": .string("red"), "future": .bool(true)])])
        let result = NativeEditorMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.state?.sections["text_elements"], .array([.object(["id": .string("a"), "text": .string("mine"), "color": .string("red"), "future": .bool(true)])]))
    }

    func testTimelineChangeConflictsWithRemoteTimedEditWithoutPublishingCandidate() {
        let base = state(["timeline_slots": .array([]), "caption_cues": .array([])])
        var local = base, remote = base
        local.sections["timeline_slots"] = .array([.object(["slot_id": .string("a")])])
        remote.sections["caption_cues"] = .array([.object(["start_s": .number(1), "text": .string("new")])])
        let result = NativeEditorMerge.merge(base: base, local: local, remote: remote)
        XCTAssertNil(result.state)
        XCTAssertFalse(result.conflicts.isEmpty)
    }

    func testMotionHashIsAtomicButUnrelatedRemoteTitleSurvivesChoice() {
        let base = state(["motion_scenes": .array([]), "motion_runtime_hash": .string("base"), "title": .string("old")])
        var local = base, remote = base
        local.sections["motion_scenes"] = .array([.object(["id": .string("scene")])])
        remote.sections["motion_runtime_hash"] = .string("remote")
        remote.sections["title"] = .string("latest")
        XCTAssertNil(NativeEditorMerge.merge(base: base, local: local, remote: remote).state)
        let chosen = NativeEditorMerge.merge(base: base, local: local, remote: remote, preference: .local)
        XCTAssertEqual(chosen.state?.sections["motion_runtime_hash"], .string("base"))
        XCTAssertEqual(chosen.state?.sections["title"], .string("latest"))
    }

    func testSongAuthorityAndEditCannotBeMixed() {
        var base = state([:])
        base.userSong = .object(["authority": .string("song-a"), "volume": .number(1)])
        var local = base, remote = base
        local.userSong = .object(["authority": .string("song-a"), "volume": .number(0.5)])
        remote.userSong = .object(["authority": .string("song-b"), "volume": .number(1)])
        XCTAssertNil(NativeEditorMerge.merge(base: base, local: local, remote: remote).state)
        XCTAssertEqual(NativeEditorMerge.merge(base: base, local: local, remote: remote, preference: .remote).state?.userSong, remote.userSong)
    }

    func testExplicitEmptyAndRemoteUnknownSectionArePreserved() {
        let base = state(["text_elements": .array([.object(["id": .string("a")])]), "future": .string("old")])
        var local = base, remote = base
        local.sections["text_elements"] = .array([])
        local.sections["future"] = .string("stale local metadata")
        remote.sections["future"] = .string("remote metadata")
        let result = NativeEditorMerge.merge(base: base, local: local, remote: remote)
        XCTAssertEqual(result.state?.sections["text_elements"], .array([]))
        XCTAssertEqual(result.state?.sections["future"], .string("remote metadata"))
    }

    func testDeletionIntentConflictsWithRemoteEntityEdit() {
        let base = state(["text_elements": .array([.object(["id": .string("a"), "text": .string("old")])])])
        var local = base, remote = base
        local.sections["text_elements"] = .array([])
        local.deletions = .array([.object(["kind": .string("text"), "id": .string("a")])])
        remote.sections["text_elements"] = .array([.object(["id": .string("a"), "text": .string("new")])])
        XCTAssertNil(NativeEditorMerge.merge(base: base, local: local, remote: remote).state)
    }
}

extension NativeEditorMergeTests {
    private func persistedMergeDocument(_ document: EditorDocument) throws -> EditorDocument {
        EditorDocument(snapshot: try XCTUnwrap(document.mergeValidationSnapshot()))
    }

    private func fallbackSplitMergeDocument(weights: [Double] = [1, 1], sourceStart: Double = 0) -> EditorDocument {
        let section = EditorSplitScreenSection(id: "fallback", durationS: 4, panelCount: 2, panelWeights: weights)
        let slots: [JSONValue] = (0..<2).map { panel in .object([
            "slot_id": .string("fallback-\(panel)"), "clip_index": .number(Double(panel)), "in_s": .number(sourceStart),
            "duration_s": .number(4), "split_section_id": .string("fallback"),
            "split_panel_index": .number(Double(panel)), "split_offset_s": .number(0)]) }
        return EditorDocument(snapshot: ["future_metadata": .string("keep"),
            "user_timeline": .object(["slots": .array(slots), "split_screen_sections": .array([section.json])]),
            "text_elements": .array([.object(["id": .string("title"), "text": .string("Before"),
                "start_s": .number(0), "end_s": .number(2)])])])
    }

    func testFallbackLayoutAndLocalTextRequireChoiceWithoutShadowingLatestDefinitions() throws {
        let base = fallbackSplitMergeDocument()
        var local = base
        local.textElements[0].text = "Local"
        let remote = fallbackSplitMergeDocument(weights: [2, 1])
        guard case .conflicts = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil) else { return XCTFail("A real remote layout edit owns dependent text; stale root definitions must not win silently") }
        for preference in [AuthoredJSONMerge.Preference.local, .remote] {
            let chosen = preference == .local ? local : remote
            guard case .resolved(let merged) = NativeEditorMerge.documents(base: base, local: local, remote: remote,
                baseSong: nil, remoteSong: nil, preference: preference) else { XCTFail("Choice must resolve"); continue }
            XCTAssertEqual(merged.splitScreenSections, chosen.splitScreenSections)
            let canonicalChosen = EditorDocument(snapshot: try XCTUnwrap(chosen.mergeValidationSnapshot()))
            XCTAssertEqual(try persistedMergeDocument(merged).clips, canonicalChosen.clips)
            XCTAssertEqual(try persistedMergeDocument(merged).textElements, canonicalChosen.textElements)
            XCTAssertEqual(merged.encodeSnapshot()["future_metadata"], .string("keep"))
        }
    }

    func testFallbackRowsAndLayoutMergeWithIndependentTitleWithoutInventingLocalTimelineEdit() throws {
        let base = fallbackSplitMergeDocument()
        var local = base
        local.title = "Local title"
        let remote = fallbackSplitMergeDocument(weights: [2, 1], sourceStart: 3)
        guard case .resolved(let merged) = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil) else { return XCTFail("Envelope normalization must not invent a competing timeline edit") }
        XCTAssertEqual(merged.splitScreenSections, remote.splitScreenSections)
        let canonicalRemote = EditorDocument(snapshot: try XCTUnwrap(remote.mergeValidationSnapshot()))
        XCTAssertEqual(try persistedMergeDocument(merged).clips, canonicalRemote.clips)
        XCTAssertEqual(merged.title, "Local title")
        XCTAssertTrue(merged.splitScreenIsValid)
        let reopened = EditorDocument(snapshot: merged.encodeSnapshot())
        XCTAssertEqual(try persistedMergeDocument(reopened).clips, canonicalRemote.clips)
        XCTAssertEqual(reopened.splitScreenSections, remote.splitScreenSections)
    }

    private func splitMergeDocument() -> EditorDocument {
        var slots = (0..<2).map { index in
            EditorTimelineSlot(id: "panel-\(index)", clipIndex: index, inS: 0, durationS: 4,
                raw: ["speaker_subject": .bool(true), "speaker_frame_override": .object(["mode": .string("inherit")])])
        }
        for index in slots.indices { slots[index].setSplitMembership(sectionID: "s", panelIndex: index, offset: 0) }
        var value = EditorDocument(clips: slots, title: "Before")
        value.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        value.speakerBackground = .init(color: "#223344", speakerFrame: .preset("small_bottom_right"))
        return EditorDocument(snapshot: value.encodeSnapshot())
    }

    func testSplitLayoutOnlyEditMergesWithRemoteTitleAndKeepsRemoteMetadata() throws {
        let base = splitMergeDocument()
        var local = base
        local.splitScreenSections[0].panelWeights = [2, 1]
        local.splitScreenSections[0].gutter = 0.02
        var wire = base.encodeSnapshot()
        wire["future_metadata"] = .string("latest")
        var remote = EditorDocument(snapshot: wire)
        remote.title = "Latest title"
        guard case .resolved(let merged) = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil) else { return XCTFail("Layout and unrelated title should merge") }
        XCTAssertEqual(merged.splitScreenSections, local.splitScreenSections)
        XCTAssertEqual(try persistedMergeDocument(merged).clips, try persistedMergeDocument(base).clips)
        XCTAssertEqual(merged.title, remote.title)
        XCTAssertEqual(merged.encodeSnapshot()["future_metadata"], .string("latest"))
        XCTAssertTrue(merged.splitScreenIsValid)
    }

    func testSplitLayoutAndMemberEditsRequireAtomicConflictChoice() throws {
        let base = splitMergeDocument()
        var local = base, remote = base
        local.splitScreenSections[0].panelWeights = [3, 1]
        remote.clips[1].durationS = 3
        remote.title = "Remote title"
        guard case .conflicts = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil) else { return XCTFail("Layout and member timing are one dependent bundle") }
        for preference in [AuthoredJSONMerge.Preference.local, .remote] {
            let chosen = preference == .local ? local : remote
            guard case .resolved(let merged) = NativeEditorMerge.documents(base: base, local: local, remote: remote,
                baseSong: nil, remoteSong: nil, preference: preference) else { XCTFail("Choice must resolve"); continue }
            XCTAssertEqual(merged.splitScreenSections, chosen.splitScreenSections)
            XCTAssertEqual(try persistedMergeDocument(merged).clips, try persistedMergeDocument(chosen).clips)
            XCTAssertEqual(merged.title, "Remote title")
            XCTAssertTrue(merged.splitScreenIsValid)
        }
    }

    func testExplicitUnstackClearSurvivesAutomaticMergeAndReopen() throws {
        let base = splitMergeDocument()
        var local = base, remote = base
        local.clips = [local.clips[1]]
        local.clips[0].clearSplitMembership()
        local.splitScreenSections = []
        remote.title = "Latest"
        guard case .resolved(let merged) = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil) else { return XCTFail("Unstack and title should merge") }
        let encoded = merged.encodeSnapshot()
        XCTAssertEqual(encoded["editor_payload"]?.objectValue?["sections"]?.objectValue?["split_screen_sections"], .array([]))
        XCTAssertEqual(try persistedMergeDocument(merged).clips, try persistedMergeDocument(local).clips)
        XCTAssertEqual(merged.title, "Latest")
        XCTAssertTrue(merged.splitScreenIsValid)
        XCTAssertEqual(EditorDocument(snapshot: encoded).splitScreenSections, [])
    }

    func testScopedSpeakerConflictChoiceKeepsEachChosenOccurrenceAndPeerExact() throws {
        let base = splitMergeDocument()
        var local = base, remote = base
        local.clips[0].raw["speaker_frame_override"] = EditorSpeakerFrameOverride.custom(.preset("small_top_left")!).json
        remote.clips[1].raw["speaker_frame_override"] = EditorSpeakerFrameOverride.full.json
        guard case .conflicts = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil) else { return XCTFail("Existing timeline ownership requires a choice") }
        for preference in [AuthoredJSONMerge.Preference.local, .remote] {
            let chosen = preference == .local ? local : remote
            guard case .resolved(let merged) = NativeEditorMerge.documents(base: base, local: local, remote: remote,
                baseSong: nil, remoteSong: nil, preference: preference) else { XCTFail("Choice must resolve"); continue }
            XCTAssertEqual(try persistedMergeDocument(merged).clips, try persistedMergeDocument(chosen).clips)
            XCTAssertEqual(merged.splitScreenSections, base.splitScreenSections)
            XCTAssertEqual(merged.speakerBackground, base.speakerBackground)
        }
    }

    func testAutomaticMergeRefusesOrphanedSplitMembership() {
        let base = splitMergeDocument()
        var remote = base
        remote.splitScreenSections = []
        if case .resolved = NativeEditorMerge.documents(base: base, local: base, remote: remote,
            baseSong: nil, remoteSong: nil) { XCTFail("An automatic update cannot adopt orphaned panel members") }
    }

    func testPlainLegacyTimelineMergeDoesNotInventSplitField() throws {
        let base = EditorDocument(snapshot: ["timeline_slots": .array([.object([
            "slot_id": .string("ordinary"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(4)])])])
        var local = base, remote = base
        local.clips[0].durationS = 3
        remote.title = "Latest"
        guard case .resolved(let merged) = NativeEditorMerge.documents(base: base, local: local, remote: remote,
            baseSong: nil, remoteSong: nil) else { return XCTFail("Ordinary timeline behavior must remain") }
        XCTAssertEqual(try persistedMergeDocument(merged).clips, try persistedMergeDocument(local).clips)
        XCTAssertEqual(merged.title, "Latest")
        XCTAssertNil(merged.encodeSnapshot()["split_screen_sections"])
    }
}
