import AVFoundation
import KriaMediaEngine
import UIKit
import XCTest
@testable import Kria

/// A phone Talking edit ("subtitled", device render) reports `timeline: false` and opens Range through the
/// `clips.source_window` leaf instead. These tests load the session the way the app does, with the capability
/// set the server sends, not the fixture session whose timeline is open.
@MainActor
final class NativeTalkingRangeTests: XCTestCase {
    /// The 4.667 s fixture take, cut at two pauses (source 1.0-1.5 and 3.0-3.5): the segments play at output
    /// 0-1, 1-2.5 and 2.5-3.667 from source 0-1, 1.5-3 and 3.5-4.667.
    private static let sourceDuration = 4.666667
    private static let removed: [(Double, Double)] = [(1.0, 1.5), (3.0, 3.5)]
    private static let ids = ["native-phone-talking-source", "native-phone-talking-source-1", "native-phone-talking-source-2"]

    private static func capabilities(leaf: JSONValue?) -> JSONValue {
        var values: [String: JSONValue] = ["timeline": .bool(false), "text_elements": .bool(false), "caption_cues": .bool(true),
                                           "mix": .bool(false), "overlays": .bool(true), "sfx": .bool(true)]
        if let leaf { values["clips"] = .object(["source_window": leaf]) }
        return .object(values)
    }

    private static func cue(_ id: String, _ text: String, _ start: Double, _ end: Double) -> JSONValue {
        .object(["id": .string(id), "text": .string(text), "start_s": .number(start), "end_s": .number(end),
                 "words": .array([.object(["text": .string(text), "start_s": .number(start), "end_s": .number(end)])])])
    }

    private static func row(_ id: String, _ inS: Double, _ duration: Double) -> JSONValue {
        .object(["slot_id": .string(id), "clip_index": .number(0), "in_s": .number(inS), "duration_s": .number(duration),
                 "source_duration_s": .number(sourceDuration), "removed": .bool(false), "transition_after": .string("cut")])
    }

    /// `savedRows`: the rows the server projects back as `user_timeline.slots` after a Range Save, with their
    /// captions already on the new clock and the new render generation.
    private static func session(leaf: JSONValue?, savedRows: [JSONValue]? = nil, cues: [JSONValue]? = nil,
                                extraVariant: [String: JSONValue] = [:]) async throws -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID(), jobID = UUID()
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        let input = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
        try FileManager.default.copyItem(at: XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4")), to: input)
        defer { try? FileManager.default.removeItem(at: input) }
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: input)
        try SourceAssetStore(project: project).bind(mediaID: "source", original: asset)
        let fingerprint = try XCTUnwrap(asset.fingerprint)
        let descriptor = OriginalMediaDescriptor(sha256: fingerprint.hex, byteCount: fingerprint.byteCount,
            durationS: sourceDuration, width: 1080, height: 1920, orientationDegrees: 0, hasAudio: true)
        let generation = savedRows == nil ? "generation-1" : "generation-2"
        var variant: [String: JSONValue] = [
            "variant_id": .string("variant"),
            "render_generation_id": .string(generation),
            "render_status": .string("ready"),
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
            "output_url": .string("file:///tmp/kria-editor-test.mp4"),
            "editor_capabilities": capabilities(leaf: leaf),
            "silence_cut": .object(["removed": .array(removed.map { .object(["start_s": .number($0.0), "end_s": .number($0.1)]) })]),
            "caption_cues": .array(cues ?? [cue("a", "one", 0.1, 0.9), cue("b", "two", 1.2, 2.4), cue("c", "three", 2.6, 3.5)]),
        ]
        if let savedRows { variant["user_timeline"] = .object(["slots": .array(savedRows)]) }
        for (key, value) in extraVariant { variant[key] = value }
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: generation,
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        fake.sourcePoolResult = NativeEditorSourcePool(
            clips: [.init(clipIndex: 0, nativeSource: .init(mediaID: "source", sourceURL: nil, original: descriptor, localRequired: true))],
            baseGeneration: generation)
        fake.deviceRenderResponse = DeviceRenderStatusResponse(
            phase: "published",
            request: DeviceRenderRequest(
                identity: DeviceRenderIdentity(jobID: jobID, variantID: "variant", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64)),
                recipe: KriaMediaEngine.EditRecipe(
                    assets: [MediaAsset(id: "source", relativePath: "source")],
                    tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 2)])])),
            publishedGeneration: generation)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    private static let open: JSONValue = .object(["editable": .bool(true)])

    private func select(_ session: NativeEditorSession, _ index: Int) -> EditorSelection {
        let id = session.timelineClips[index].id
        session.selectClip(id)
        return EditorSelection(kind: .clip, id: id.uuidString)
    }

    private func captions(_ session: NativeEditorSession) -> [String] {
        session.document.captionCues.map { "\($0.text) \(($0.startS * 1000).rounded() / 1000)-\(($0.endS * 1000).rounded() / 1000)" }
    }

    /// Range on a hydrated Talking segment writes the window and Save carries the timeline rows (same ids,
    /// order and count as the hydrated document) with the captions already moved onto the new clock.
    func testRangeOnATalkingSegmentWritesTheWindowAndSavesTimelineRowsWithRebasedCaptions() async throws {
        let (session, fake) = try await Self.session(leaf: Self.open)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertFalse(session.canEditTimeline, "the server keeps the timeline closed for Talking")
        XCTAssertEqual(session.document.clips.map(\.id), Self.ids)
        XCTAssertEqual(captions(session), ["one 0.1-0.9", "two 1.2-2.4", "three 2.6-3.5"])
        XCTAssertFalse(session.hasUnsavedChanges, "hydration is server state, never an edit")

        let selection = select(session, 1)
        XCTAssertNil(session.sourceWindowBlockedReason(for: selection), "the open leaf unlocks Range")
        XCTAssertTrue(session.setSourceWindow(for: selection, .init(start: 1.3, length: 1.7), rate: 1, sourceDuration: Self.sourceDuration))

        let slot = session.document.clips[1]
        XCTAssertEqual(slot.inS, 1.3, accuracy: 1e-9)
        XCTAssertEqual(slot.durationS ?? 0, 1.7, accuracy: 1e-9)
        XCTAssertTrue(session.dirtySections.contains(.timeline))
        XCTAssertEqual(captions(session), ["one 0.1-0.9", "two 1.4-2.6", "three 2.8-3.7"], "captions follow their words, later ones shift by the 0.2 s growth")

        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
        let request = try XCTUnwrap(fake.lastRequest)
        let rows = try XCTUnwrap(request.timelineSlots).compactMap(\.objectValue)
        XCTAssertEqual(rows.map { $0["slot_id"]?.stringValue }, Self.ids)
        XCTAssertEqual(rows.map { $0["clip_index"]?.numberValue }, [0, 0, 0])
        XCTAssertEqual(try XCTUnwrap(rows[1]["in_s"]?.numberValue), 1.3, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(rows[1]["duration_s"]?.numberValue), 1.7, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(rows[0]["in_s"]?.numberValue), 0, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(rows[2]["in_s"]?.numberValue), 3.5, accuracy: 1e-9)
        XCTAssertEqual(rows.map { $0["removed"] }, [.bool(false), .bool(false), .bool(false)])
        XCTAssertEqual(rows.map { $0["transition_after"]?.stringValue }, ["cut", "cut", "cut"])
        for row in rows {
            for key in ["playback_rate", "source_end_s", "tilt", "source_crop"] { XCTAssertNil(row[key], "\(key) is not part of a Range write") }
        }
        let cues = try XCTUnwrap(request.captionCues).compactMap(\.objectValue)
        XCTAssertEqual(cues.map { $0["text"]?.stringValue }, ["one", "two", "three"])
        XCTAssertEqual(try XCTUnwrap(cues[1]["start_s"]?.numberValue), 1.4, accuracy: 0.001, "the rebased lane travels in the same request")
        XCTAssertEqual(try XCTUnwrap(cues[2]["start_s"]?.numberValue), 2.8, accuracy: 0.001)
    }

    /// A closed leaf names why in plain words; unknown codes and `talking_range_disabled` use the generic line.
    func testClosedLeafNamesItsReason() async throws {
        let cases: [(String?, String)] = [
            ("talking_range_multi_clip", "Range works on single-clip talking videos for now."),
            ("talking_range_word_cuts", "Range isn’t available after removing words from this video yet."),
            ("talking_range_processed_audio", "Range isn’t available after cleaning up this video’s sound yet."),
            ("talking_range_cut_mismatch", "This video’s cut changed. Make the video again to choose a range."),
            ("client_update_required", "Update Kria to choose which part of your take plays."),
            ("talking_range_disabled", "Range isn’t available for this video yet."),
            ("something_new", "Range isn’t available for this video yet."),
            (nil, "Range isn’t available for this video yet."),
        ]
        for (code, copy) in cases {
            var leaf: [String: JSONValue] = ["editable": .bool(false)]
            if let code { leaf["reason"] = .string(code) }
            let (session, _) = try await Self.session(leaf: .object(leaf))
            XCTAssertEqual(session.document.clips.map(\.id), Self.ids, code ?? "no reason")
            let selection = select(session, 1)
            XCTAssertEqual(session.sourceWindowBlockedReason(for: selection), copy, code ?? "no reason")
            XCTAssertFalse(session.setSourceWindow(for: selection, .init(start: 1.5, length: 1.4), rate: 1, sourceDuration: Self.sourceDuration))
            XCTAssertFalse(session.hasUnsavedChanges)
        }
    }

    /// The finished MP4 plays (applied word cuts), so there is no clip to select. The Range affordance still
    /// says why instead of "Select a clip".
    func testNoSelectableClipStillExplainsWhyRangeIsClosed() async throws {
        let leaf: JSONValue = .object(["editable": .bool(false), "reason": .string("talking_range_word_cuts")])
        let cuts: JSONValue = .array([.object(["status": .string("applied")])])
        let (session, _) = try await Self.session(leaf: leaf, extraVariant: ["explicit_speech_cuts": cuts])
        XCTAssertTrue(session.document.clips.isEmpty, "the finished MP4 plays, nothing is hydrated")
        XCTAssertEqual(session.sourceWindowBlockedReason(for: nil), "Range isn’t available after removing words from this video yet.")
    }

    /// A window may grow into the pause beside it, never into the neighbouring segment's source or past the take.
    func testRangeRefusesWindowsThatOverlapANeighbourOrLeaveTheSource() async throws {
        let (session, _) = try await Self.session(leaf: Self.open)
        let selection = select(session, 1)
        let duration = Self.sourceDuration
        XCTAssertFalse(session.setSourceWindow(for: selection, .init(start: 0.9, length: 2.1), rate: 1, sourceDuration: duration), "overlaps the first segment (source 0-1)")
        XCTAssertFalse(session.setSourceWindow(for: selection, .init(start: 1.5, length: 2.1), rate: 1, sourceDuration: duration), "overlaps the last segment (source 3.5-)")
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.setSourceWindow(for: selection, .init(start: 1.0, length: 2.5), rate: 1, sourceDuration: duration), "fills the pause on both sides and only touches the neighbours")
        XCTAssertEqual(session.document.clips[1].inS, 1.0, accuracy: 1e-9)

        let last = select(session, 2)
        XCTAssertFalse(session.setSourceWindow(for: last, .init(start: 3.5, length: 1.4), rate: 1, sourceDuration: duration), "past the end of the take")
        XCTAssertFalse(session.setSourceWindow(for: last, .init(start: -0.5, length: 1.5), rate: 1, sourceDuration: duration), "before the start of the take")
        XCTAssertEqual(session.document.clips.map(\.id), Self.ids)
    }

    /// A Montage-style clip with the timeline closed stays blocked exactly as before, leaf or no leaf.
    func testClipOutsideTheTalkingSourceStaysBlockedWhenTheTimelineIsClosed() throws {
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: 3, sourceDuration: 10, slotID: "slot-a")
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": Self.capabilities(leaf: Self.open),
            "editor_payload": .object(["sections": .object(["timeline_slots": .array([
                .object(["slot_id": .string("slot-a"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(3), "source_duration_s": .number(10)])])])]),
        ]
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot))
        let selection = EditorSelection(kind: .clip, id: session.timelineClips[0].id.uuidString)
        XCTAssertEqual(session.sourceWindowBlockedReason(for: selection), "Clip edits are locked for this render.")
        XCTAssertFalse(session.setSourceWindow(for: selection, .init(start: 1, length: 3), rate: 1, sourceDuration: 10))
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// After a Range Save the server projects the saved rows back as `user_timeline.slots` (no
    /// `native_composite_source` flag). The reopened editor still plays the saved window from the source,
    /// keeps the three segments, and Range stays editable on them.
    func testReopenedAfterSaveStillPlaysTheSavedWindowFromTheSourceAndKeepsRangeOpen() async throws {
        let saved = [Self.row(Self.ids[0], 0, 1), Self.row(Self.ids[1], 1.3, 1.7), Self.row(Self.ids[2], 3.5, 1.166667)]
        let rebased = [Self.cue("a", "one", 0.1, 0.9), Self.cue("b", "two", 1.4, 2.6), Self.cue("c", "three", 2.8, 3.7)]
        let (session, fake) = try await Self.session(leaf: Self.open, savedRows: saved, cues: rebased)

        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertFalse(session.isShowingRenderedFallback, "the saved window plays from the source, not the finished MP4")
        XCTAssertEqual(session.document.clips.map(\.id), Self.ids, "no duplicated or re-split segments")
        XCTAssertEqual(session.document.clips.map(\.inS), [0, 1.3, 3.5])
        XCTAssertEqual(session.timelineClips.count, 3)
        XCTAssertEqual(session.timelineClips[1].trimIn, 1.3, accuracy: 1e-9)
        XCTAssertEqual(session.duration, 3.866667, accuracy: 0.01, "1 + 1.7 + 1.167 s of saved windows")
        XCTAssertFalse(session.hasUnsavedChanges)

        let selection = select(session, 1)
        XCTAssertNil(session.sourceWindowBlockedReason(for: selection))
        XCTAssertTrue(session.setSourceWindow(for: selection, .init(start: 1.2, length: 1.8), rate: 1, sourceDuration: Self.sourceDuration))
        await session.save()
        let rows = try XCTUnwrap(fake.lastRequest?.timelineSlots).compactMap(\.objectValue)
        XCTAssertEqual(rows.map { $0["slot_id"]?.stringValue }, Self.ids)
        XCTAssertEqual(try XCTUnwrap(rows[1]["in_s"]?.numberValue), 1.2, accuracy: 1e-9)
    }
}
