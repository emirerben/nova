import AVFoundation
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
        XCTAssertEqual(session.loadState, .idle)
    }

    func testFixtureSessionStartsLoaded() {
        let session = NativeEditorSession()
        XCTAssertEqual(session.loadState, .loaded)
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
        let payload = Self.object(session.document.encodeSnapshot()["editor_payload"])
        let sections = Self.object(payload?["sections"])
        let slots = Self.array(sections?["timeline_slots"])
        XCTAssertEqual(Self.object(slots.first)?["slot_id"], .string("b"))
        XCTAssertEqual(Self.object(slots.last)?["slot_id"], .string("a"))
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

    func testNarratedVariantProjectsPersistedVisualCutIntoTimeline() {
        let server = DraftSnapshot(
            draftID: "job", itemID: "item", variantKey: "narrated",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: UUID().uuidString,
            baseGenerationID: "generation", snapshot: [:], canUndo: false, createdAt: .now
        )
        let authoritative: [String: JSONValue] = [
            "variant_id": .string("narrated"),
            "resolved_archetype": .string("narrated"),
            "render_status": .string("ready"),
            "ai_timeline": .null,
            "narrated_timings": .array([
                .object(["step_id": .string("shot_1"), "start_s": .number(0), "end_s": .number(12)]),
                .object(["step_id": .string("shot_2"), "start_s": .number(12), "end_s": .number(30)]),
            ]),
            "narrated_clip_assignments": .array([
                .object(["step_id": .string("shot_1"), "clip_id": .string("clip_2"), "source_start_s": .number(1.5)]),
                .object(["step_id": .string("shot_2"), "clip_id": .string("clip_0"), "source_start_s": .number(4)]),
            ]),
            "editor_capabilities": .object(["timeline": .bool(false)]),
        ]

        let draft = server.editorDraft(projectID: UUID(), authoritativeVariant: authoritative)

        XCTAssertEqual(draft.clips.count, 2)
        XCTAssertEqual(draft.clips.map(\.sourceClipIndex), [2, 0])
        XCTAssertEqual(draft.clips.map(\.start), [0, 12])
        XCTAssertEqual(draft.clips.map(\.end), [12, 30])
        XCTAssertEqual(draft.clips.map(\.trimIn), [1.5, 4])
        XCTAssertEqual(draft.clips.map(\.trimOut), [13.5, 22])
    }

    func testAuthoritativeVariantReplacesAdvancedCapabilitiesAndRequiredMotionHash() {
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": .object([
                "motion_scenes": .bool(true),
                "overlays": .bool(false),
            ]),
            "editor_payload": .object([
                "base_generation": .string("old"),
                "sections": .object([
                    "motion_runtime_hash": .string("stale-required"),
                    "motion_scenes": .array([]),
                ]),
            ]),
        ]
        let server = DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: "old",
            snapshot: snapshot, canUndo: false, createdAt: .now
        )
        let authoritative: [String: JSONValue] = [
            "render_generation_id": .string("live"),
            "motion_runtime_hash": .string("live-required"),
            "editor_capabilities": .object([
                "motion_scenes": .bool(false),
                "motion_scenes_reason": .string("motion_runtime_mismatch"),
                "motion_runtime_hash": .string("editor-runtime"),
                "motion_required_runtime_hash": .string("live-required"),
                "overlays": .bool(true),
            ]),
        ]

        let draft = server.editorDraft(projectID: UUID(), authoritativeVariant: authoritative)
        let document = EditorDocument.decode(snapshot: draft.serverSnapshot)
        XCTAssertEqual(document.motionRuntimeHash, "live-required")
        XCTAssertEqual(document.capabilities["motion_scenes"]?.editable, false)
        XCTAssertEqual(document.capabilities["motion_scenes"]?.reason, "motion_runtime_mismatch")
        XCTAssertEqual(document.capabilities["overlays"]?.editable, true)
        XCTAssertNil(document.capabilities["motion_runtime_hash"], "metadata is not an editable capability")
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

    func testReadyCreationProjectHydratesListProjectionAndLoadsItsExistingPlanItem() async throws {
        let threadID = UUID()
        let jobID = UUID()
        let slots: [JSONValue] = (0..<6).map { index in
            .object([
                "slot_id": .string("slot-\(index)"),
                "clip_index": .number(Double(index)),
                "in_s": .number(0),
                "duration_s": .number(1),
                "source_duration_s": .number(2),
                "removed": .bool(false),
            ])
        }
        var variant = Self.variant(duration: 6, generation: "legacy-finished-at")
        variant["variant_id"] = .string("song_text")
        variant["user_timeline"] = .object([:])
        variant["ai_timeline"] = .object(["slots": .array(slots)])
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)",
          "title":"Music montage",
          "status":"active",
          "revision":8,
          "runtime_version":2,
          "active_job_id":"\#(jobID.uuidString)",
          "active_plan_item_id":"item-montage",
          "state":{},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[]},
          "updated_at":"2026-09-09T08:00:00Z"
        }
        """#.utf8))
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "unused", itemID: "unused", variantKey: "initial",
                draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil,
                baseGenerationID: nil, snapshot: [:], canUndo: false, createdAt: .now
            ),
            draftError: .conflict,
            openReceipt: OpenInEditorResponse(planItemID: "item-montage", variantID: "original_text"),
            authoritativeVariant: variant,
            refreshedThread: refreshedThread
        )
        let project = ProjectSummary(
            id: threadID,
            title: "Music montage",
            status: .ready,
            updatedAt: .now,
            posterURL: nil,
            activeJobID: jobID
        )
        let session = NativeEditorSession(project: project)

        await session.load(project: project, api: fake)

        XCTAssertEqual(fake.draftCallCount, 0, "ready projects must not depend on the rollout-gated runtime draft")
        XCTAssertEqual(fake.projectCallCount, 1, "URL-free project-list summaries must hydrate before editor loading")
        XCTAssertNil(fake.openedJobID, "creation projects already own a plan item and must not be promoted again")
        XCTAssertEqual(fake.editorVariantsCallCount, 1, "the job status is authoritative when project projections omit variant identity")
        XCTAssertEqual(session.timelineClips.count, 6)
        XCTAssertEqual(session.duration, 6, accuracy: 0.0001)
        XCTAssertEqual(session.saveState, .idle)
    }

    func testDraftLoadFailureIsNotReportedAsSaveFailure() async {
        let threadID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "unused", itemID: "unused", variantKey: "initial",
                draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil,
                baseGenerationID: nil, snapshot: [:], canUndo: false, createdAt: .now
            ),
            draftError: .requestFailed
        )
        let project = ProjectSummary(
            id: threadID, title: "Draft", status: .draft, updatedAt: .now, posterURL: nil
        )
        let session = NativeEditorSession(project: project)

        await session.load(project: project, api: fake)

        XCTAssertEqual(
            session.saveState,
            .loadFailed("Kria couldn’t complete that request. Check your connection and try again.")
        )
    }

    func testDraftLoadFailsWhenAuthoritativeVariantCannotBeFetched() async {
        let threadID = UUID()
        let jobID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "draft", itemID: "item", variantKey: "selected",
                draftRevision: 1, snapshotHash: "hash", etag: "etag",
                baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: ["editor_payload": .object([:])], canUndo: false, createdAt: .now
            ),
            editorVariantError: .requestFailed
        )
        let session = NativeEditorSession(project: ProjectSummary(
            id: threadID, title: "Draft", status: .draft, updatedAt: .now, posterURL: nil
        ))

        await session.load(api: fake, threadID: threadID)

        XCTAssertEqual(session.loadState, .failed("Kria couldn’t complete that request. Check your connection and try again."))
        XCTAssertEqual(session.saveState, .loadFailed("Kria couldn’t complete that request. Check your connection and try again."))
    }

    func testHydratedProjectPlaybackFallbackUsesRefreshedProjection() async throws {
        let threadID = UUID()
        let jobID = UUID()
        let fallbackURL = URL(fileURLWithPath: "/tmp/kria-refreshed-fallback.mp4")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)",
          "title":"Hydrated",
          "status":"active",
          "revision":8,
          "runtime_version":2,
          "active_job_id":"\#(jobID.uuidString)",
          "active_plan_item_id":"item-hydrated",
          "state":{"selected_variant_id":"original_text"},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[{"variant_id":"original_text","render_status":"ready","output_url":"\#(fallbackURL.absoluteString)"}]},
          "updated_at":"2026-09-09T08:00:00Z"
        }
        """#.utf8))
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative.removeValue(forKey: "output_url")
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "unused", itemID: "unused", variantKey: "initial",
                draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil,
                baseGenerationID: nil, snapshot: [:], canUndo: false, createdAt: .now
            ),
            authoritativeVariant: authoritative,
            refreshedThread: refreshedThread
        )
        let project = ProjectSummary(
            id: threadID, title: "Hydrated", status: .ready, updatedAt: .now,
            posterURL: nil, activeJobID: jobID
        )
        let session = NativeEditorSession(project: project)

        await session.load(project: project, api: fake)

        let asset = try XCTUnwrap(session.player?.currentItem?.asset as? AVURLAsset)
        XCTAssertEqual(asset.url, fallbackURL)
        XCTAssertEqual(fake.lastVariantID, "original_text")
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

    func testSaveKeepsNewerSameSectionEditDirtyWhileCommitIsInFlight() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let response = EditorCommitResponse(
            ok: true, generation: "g2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil
        )
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: response,
            suspendNextCommit: true
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        XCTAssertTrue(session.canEdit(.text))
        session.updateTextContent(id: loadedTextID, content: "Submitted")
        XCTAssertTrue(session.hasUnsavedChanges)

        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.commitIsSuspended {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(fake.commitIsSuspended, "the commit gate did not suspend in time")
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Submitted"))

        session.updateTextContent(id: loadedTextID, content: "Newer local edit")
        fake.resumeCommit()
        await saveTask.value

        XCTAssertEqual(session.document.textElements.first?.text, "Newer local edit")
        XCTAssertTrue(session.isDirty(.text))
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)

        await session.save()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Newer local edit"))
    }

    func testSaveKeepsInFlightRevertDirtyAgainstAcknowledgedServerValue() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let response = EditorCommitResponse(
            ok: true, generation: "g2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil
        )
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: response,
            suspendNextCommit: true
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        session.updateTextContent(id: loadedTextID, content: "Submitted")

        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.commitIsSuspended {
            try? await Task.sleep(for: .milliseconds(10))
        }
        session.updateTextContent(id: loadedTextID, content: "Original")
        fake.resumeCommit()
        await saveTask.value

        XCTAssertEqual(session.document.textElements.first?.text, "Original")
        XCTAssertTrue(session.isDirty(.text))
        XCTAssertTrue(session.hasUnsavedChanges)

        await session.save()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Original"))
    }

    func testPersistedCommitWithFailedEnqueueCanRetryRender() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: EditorCommitResponse(ok: false, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        session.updateTextContent(id: loadedTextID, content: "Submitted")

        await session.save()

        XCTAssertEqual(session.saveState, .renderRetryNeeded("Your edit is saved. Its preview render did not start, so you can retry it safely."))
        XCTAssertFalse(session.hasUnsavedChanges)
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)

        await session.retryRender()

        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Submitted"))
        XCTAssertEqual(session.saveState, .previewPending)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSaveConflictPreservesLocalDocumentSelectionAndUndo() async {
        let threadID = UUID()
        let clipID = UUID()
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string(clipID.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true)])],
            commitError: .conflict
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let selected = try! XCTUnwrap(session.timelineClips.first?.id)
        session.selectClip(selected)
        session.trimSelected(edge: .trailing, to: 1.5)
        let editedDocument = session.document

        await session.save()

        XCTAssertEqual(session.saveState, .conflict)
        XCTAssertEqual(session.document, editedDocument)
        XCTAssertEqual(session.selectedClipID, selected)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)
    }

    func testConflictRebaseKeepsLocalSectionsOnLatestServerBaseline() async {
        let threadID = UUID()
        let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let latestVariant: [String: JSONValue] = [
            "render_generation_id": .string("g2"),
            "resolved_archetype": .string("narrated"),
            "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["text_elements": .bool(true)]),
            "text_elements": .array([.object([
                "id": .string(textID), "text": .string("Changed elsewhere"),
                "start_s": .number(0), "end_s": .number(2),
            ])]),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: latestVariant,
            commitError: .conflict
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: textID, content: "My local edit")

        await session.save()
        XCTAssertEqual(session.saveState, .conflict)

        await session.rebaseAfterConflict()

        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertEqual(session.document.textElements.first?.text, "My local edit")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)
        XCTAssertEqual(session.saveState, .idle)

        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "Changed elsewhere")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSaveRequestFailurePreservesLocalEdits() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitError: .offline
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        session.updateTextContent(id: loadedTextID, content: "Unsaved")

        await session.save()

        XCTAssertEqual(session.saveState, .failed("Kria couldn’t complete that request. Check your connection and try again."))
        XCTAssertEqual(session.document.textElements.first?.text, "Unsaved")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)
    }

    func testSaveUsesExpectedRenderedDurationUntilPreviewRefreshes() async {
        let threadID = UUID()
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 0, trimOut: 2)
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2)])])])])]
        let response = EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: 1.5)
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            commitResponse: response
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1))
        await session.load(api: fake, threadID: threadID)
        session.selectClip(try! XCTUnwrap(session.timelineClips.first?.id))
        session.trimSelected(edge: .trailing, to: 1.25)
        await session.save()

        XCTAssertEqual(session.duration, 1.5, accuracy: 0.0001)
        XCTAssertEqual(session.saveState, .previewPending)
    }

    func testCanonicalDocumentTracksTextEditAndDirtySection() {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.addText(content: "Hook")
        XCTAssertEqual(session.document.textElements.first?.text, "Hook")
        XCTAssertTrue(session.isDirty(.text))
        session.undo()
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testLongTextEditDoesNotChangeTransportDuration() {
        let textID = UUID()
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 0, trimOut: 4)
        let text = TextLayer(id: textID, content: "Short", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [text], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        let originalDuration = session.duration

        session.updateTextContent(id: textID, content: String(repeating: "Long text ", count: 100))

        XCTAssertEqual(session.duration, originalDuration, accuracy: 0.0001)
    }

    func testUndoingTrimRestoresRenderedDurationBoundary() async {
        let threadID = UUID()
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(4)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 4, generation: "g1")
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.selectClip(try! XCTUnwrap(session.timelineClips.first?.id))

        session.trimSelected(edge: .trailing, to: 3)
        XCTAssertEqual(session.duration, 3, accuracy: 0.0001)

        session.undo()
        XCTAssertEqual(session.duration, 4, accuracy: 0.0001)
    }

    func testCaptionMetadataUsesCaptionMetaCommitSection() async {
        let threadID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "g1", snapshot: ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["captions_enabled": .bool(false), "caption_style": .string("word")])])], canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("subtitled"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)]), "captions_enabled": .bool(false), "caption_style": .string("word")],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: true, timeline: false, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.setCaptionStyle("sentence")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.captionMeta?["style"], JSONValue.string("sentence"))
        XCTAssertNil(fake.lastRequest?.captionCues)
        XCTAssertFalse(session.isDirty(.captionMeta))
    }

    func testTypedTextMutationsKeepWireKeysAndOneGestureUndo() {
        let textID = UUID()
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [TextLayer(id: textID, content: "old", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: draft)
        session.beginTransaction()
        session.updateTextContent(id: textID, content: "new")
        session.updateTextTiming(id: textID, startS: 1, endS: 3)
        session.setTextSize(id: textID.uuidString, sizePX: 88)
        session.setTextWidth(id: textID.uuidString, width: 0)
        session.setTextAlignment(id: textID.uuidString, alignment: "center")
        session.setTextAnimation(id: textID.uuidString, animation: "pop-in")
        session.setTextColor(id: textID.uuidString, color: "#fff")
        session.setTextHighlightColor(id: textID.uuidString, color: "#0f0")
        session.setTextShadow(id: textID.uuidString, enabled: true)
        session.setTextStroke(id: textID.uuidString, width: 4)
        session.setTextBehindSubject(id: textID.uuidString, behind: true)
        session.setTextPosition(id: textID, x: 0.2, y: 0.8)
        session.endTransaction()
        let payload = Self.object(session.document.encodeSnapshot()["editor_payload"]); let sections = Self.object(payload?["sections"])
        let text = Self.object(Self.array(sections?["text_elements"]).first)
        XCTAssertEqual(text?["text"], .string("new")); XCTAssertEqual(text?["start_s"], .number(1)); XCTAssertEqual(text?["end_s"], .number(3))
        XCTAssertEqual(text?["size_px"], .number(88)); XCTAssertEqual(text?["max_width_frac"], .number(0.2)); XCTAssertEqual(text?["effect"], .string("pop-in")); XCTAssertNil(text?["font_size_px"]); XCTAssertNil(text?["width"]); XCTAssertNil(text?["animation"])
        XCTAssertEqual(text?["behind_subject"], .bool(true)); XCTAssertTrue(session.canUndo)
        session.undo(); XCTAssertFalse(session.hasUnsavedChanges)
        session.redo(); session.beginTransaction(); session.setTextColor(id: textID.uuidString, color: "#000"); session.endTransaction(); XCTAssertFalse(session.canRedo)
    }

    func testUndoHistoryIsBoundedForLongEditingSessions() {
        let textID = UUID()
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [TextLayer(id: textID, content: "Initial", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: draft)

        for index in 0..<150 {
            session.updateTextContent(id: textID, content: "Edit \(index)")
        }

        var undoCount = 0
        while session.canUndo {
            session.undo()
            undoCount += 1
        }
        XCTAssertEqual(undoCount, 100)
    }

    func testCaptionCueMetadataAndMusicMutationsUseSeparateDirtySections() async {
        let threadID = UUID(); let trackID = UUID(); let cueID = "cue-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["caption_meta": .object(["enabled": .bool(true), "style": .string("word")]), "caption_cues": .array([.object(["id": .string(cueID), "start_s": .number(0), "end_s": .number(1), "text": .string("old")])]), "music_track_id": .string(trackID.uuidString), "music_window": .object(["start_s": .number(0), "alignment": .string("preserve_cuts")]), "audio_mix": .object(["music_level": .number(0.5), "original_level": .number(1)])])])]
        let response = EditorCommitResponse(ok: false, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: true, timeline: false, mix: true, captionCues: true, music: true), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["resolved_archetype": .string("subtitled"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(true)])], commitResponse: response)
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateCaptionCue(id: cueID, text: "new", startS: 0.25, endS: 1.25); session.setCaptionHighlightColor("#0f0")
        session.setMusicWindow(startS: 2, alignment: "resync_beats"); session.setMusicLevel(0.8); session.setOriginalMixLevel(0.6)
        XCTAssertTrue(session.isDirty(.captions)); XCTAssertTrue(session.isDirty(.captionMeta)); XCTAssertTrue(session.isDirty(.music)); XCTAssertTrue(session.isDirty(.mix))
        await session.save()
        XCTAssertEqual(fake.lastRequest?.captionCues?.count, 1); XCTAssertEqual(fake.lastRequest?.captionMeta?["highlight_color"], .string("#0f0")); XCTAssertEqual(fake.lastRequest?.musicWindow?.startS, 2); XCTAssertEqual(fake.lastRequest?.musicWindow?.alignment, .resyncBeats); XCTAssertEqual(fake.lastRequest?.mix?["music_level"], .number(0.8)); XCTAssertEqual(fake.lastRequest?.mix?["original_level"], .number(0.6)); XCTAssertEqual(session.saveState, .renderRetryNeeded("Your edit is saved. Its preview render did not start, so you can retry it safely."))
    }

    func testCapabilityReasonIsExposedAndDisablesTypedMutation() {
        let textID = UUID(); let snapshot: [String: JSONValue] = ["editor_capabilities": .object(["text_elements": .object(["editable": .bool(false), "reason": .string("renderer locked")])]), "editor_payload": .object(["sections": .object(["text_elements": .array([.object(["id": .string(textID.uuidString), "text": .string("old"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [TextLayer(id: textID, content: "old", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        XCTAssertEqual(session.capabilityReason("text_elements"), "renderer locked"); XCTAssertFalse(session.canEdit(.text)); session.updateTextContent(id: textID, content: "new"); XCTAssertEqual(session.document.textElements.first?.text, "old")
    }

    func testTimedLanesMutateWithOneGestureAndHonorWireKeys() {
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": .object([
                "sfx": .bool(true), "overlays": .bool(true), "visual_blocks": .bool(true),
                "motion_scenes": .bool(true), "camera_effects": .bool(true), "carousel": .bool(true), "layer_order": .bool(true),
            ]),
            "editor_payload": .object(["sections": .object([
                "sound_effects": .array([.object(["id": .string("sfx"), "at_s": .number(1), "gain": .number(1), "trim_start_s": .number(0), "trim_end_s": .number(0.5), "duration_s": .number(1)])]),
                "media_overlays": .array([.object(["id": .string("overlay"), "start_s": .number(0), "end_s": .number(2), "x_frac": .number(0.5), "y_frac": .number(0.5), "scale": .number(0.5), "z": .number(1)])]),
                "visual_blocks": .array([.object(["id": .string("visual"), "kind": .string("media"), "start_s": .number(0), "end_s": .number(2)])]),
                "motion_scenes": .array([.object(["id": .string("motion"), "start_frame": .number(0), "end_frame_exclusive": .number(60), "preset_id": .string("push"), "runtime_hash": .string("runtime")])]),
                "motion_runtime_hash": .string("runtime"),
                "camera_effects": .array([.object(["id": .string("camera"), "start_s": .number(0), "end_s": .number(2)])]),
                "carousel_moment": .object(["position": .string("intro")]),
            ])]),
        ]
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        session.beginTransaction()
        session.setSoundEffectTiming(id: "sfx", atS: 2)
        session.setSoundEffectTrim(id: "sfx", trimStartS: 0.1, trimEndS: 0.8)
        session.setSoundEffectGain(id: "sfx", gain: 1.5)
        session.setMediaOverlayPosition(id: "overlay", x: 2, y: -1)
        session.setMediaOverlayScale(id: "overlay", scale: 2)
        session.setMediaOverlayDisplayMode(id: "overlay", mode: "fullscreen")
        session.setCameraEffectIntensity(id: "camera", intensity: 0.8)
        session.setCameraEffectEasing(id: "camera", easing: "sine_pulse")
        session.setCarouselPosition("outro")
        session.endTransaction()
        XCTAssertEqual(session.document.soundEffects.first?.pointS, 2)
        XCTAssertEqual(session.document.soundEffects.first?.raw["gain"], .number(1.5))
        XCTAssertEqual(session.document.mediaOverlays.first?.raw["x_frac"], .number(1))
        XCTAssertEqual(session.document.mediaOverlays.first?.raw["scale"], .number(1))
        XCTAssertEqual(session.document.cameraEffects.first?.raw["intensity"], .number(0.08))
        XCTAssertEqual(session.document.cameraEffects.first?.raw["easing"], .string("sine_pulse"))
        XCTAssertEqual(session.document.carouselMoment?["position"], .string("outro"))
        XCTAssertTrue(session.canUndo)
    }

    func testMotionRuntimeMismatchIsAlwaysReadOnly() {
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": .object([
                "motion_scenes": .object(["editable": .bool(false), "reason": .string("motion_runtime_mismatch")]),
                "motion_runtime_hash": .string("current-runtime"),
            ]),
            "editor_payload": .object(["sections": .object([
                "motion_runtime_hash": .string("editor"),
                "motion_scenes": .array([.object(["id": .string("motion"), "start_frame": .number(0), "end_frame_exclusive": .number(60), "preset_id": .string("route_trace")])]),
            ])]),
        ]
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot))
        XCTAssertTrue(session.isMotionSceneReadOnly(id: "motion"))
        XCTAssertNotNil(session.motionRuntimeMismatchReason(id: "motion"))
        session.setMotionSceneTiming(id: "motion", startS: 1)
        XCTAssertEqual(session.document.motionScenes.first?.startS, 0)
        XCTAssertEqual(session.motionRuntimeMismatchReason(id: "motion"), "motion_runtime_mismatch")
    }


    private static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { value } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(value) = value { value } else { [] } }

    private static func variant(duration: Double, generation: String) -> [String: JSONValue] {
        [
            "variant_id": .string("initial"),
            "render_generation_id": .string(generation),
            "render_status": .string("ready"),
            "duration_s": .number(duration),
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
    let draftError: APIError?
    let openReceipt: OpenInEditorResponse?
    let authoritativeVariant: [String: JSONValue]?
    let editorVariantError: APIError?
    var commitResponse: EditorCommitResponse?
    let commitError: APIError?
    let refreshedThread: CreationThread?
    private(set) var commitIsSuspended = false
    private var commitContinuation: CheckedContinuation<Void, Never>?
    private var commitResumeRequested = false
    private var suspendNextCommit: Bool
    var commitCount = 0
    var lastRequest: EditorCommitRequest?
    var openedJobID: UUID?
    var lastItemID: String?
    var draftCallCount = 0
    var lastVariantID: String?
    var projectCallCount = 0
    var editorVariantsCallCount = 0
    init(draftSnapshot: DraftSnapshot, draftError: APIError? = nil, openReceipt: OpenInEditorResponse? = nil, authoritativeVariant: [String: JSONValue]? = nil, editorVariantError: APIError? = nil, commitResponse: EditorCommitResponse? = nil, commitError: APIError? = nil, refreshedThread: CreationThread? = nil, suspendNextCommit: Bool = false) {
        self.draftSnapshot = draftSnapshot
        self.draftError = draftError
        self.openReceipt = openReceipt
        self.authoritativeVariant = authoritativeVariant
        self.editorVariantError = editorVariantError
        self.commitResponse = commitResponse
        self.commitError = commitError
        self.refreshedThread = refreshedThread
        self.suspendNextCommit = suspendNextCommit
    }
    func projects() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func project(threadID: UUID) async throws -> CreationThread {
        projectCallCount += 1
        guard let refreshedThread else { throw APIError.unsupported }
        return refreshedThread
    }
    func library() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func createThread(message: String?) async throws -> CreationThread { throw APIError.unsupported }
    func exchangeMobileToken(_ credential: AuthCredential, provider: String) async throws -> MobileSession { throw APIError.unsupported }
    func refreshMobileSession(_ refreshToken: String) async throws -> MobileSession { throw APIError.unsupported }
    func revokeMobileSession(_ refreshToken: String) async throws { throw APIError.unsupported }
    func submitTurn(threadID: UUID, message: String, expectedRevision: Int) async throws -> TurnAccepted { throw APIError.unsupported }
    func applyCreationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int) async throws -> CreationThread { throw APIError.unsupported }
    func threadDelta(threadID: UUID, afterSequence: Int) async throws -> ThreadDelta { throw APIError.unsupported }
    func draft(threadID: UUID) async throws -> DraftSnapshot {
        draftCallCount += 1
        if let draftError { throw draftError }
        return draftSnapshot
    }
    func writeDraft(threadID: UUID, snapshot: [String: JSONValue], expectedRevision: Int, etag: String) async throws -> DraftSnapshot { throw APIError.unsupported }
    func openJobInEditor(jobID: UUID) async throws -> OpenInEditorResponse {
        openedJobID = jobID
        guard let openReceipt else { throw APIError.unsupported }
        return openReceipt
    }
    func editorVariant(jobID: UUID, variantID: String) async throws -> [String: JSONValue] {
        lastVariantID = variantID
        if let editorVariantError { throw editorVariantError }
        return authoritativeVariant ?? ["variant_id": .string(variantID), "render_generation_id": .string("generation-1"), "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false)]), "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])])]
    }
    func editorVariants(jobID: UUID) async throws -> [[String: JSONValue]] {
        editorVariantsCallCount += 1
        return [authoritativeVariant ?? ["variant_id": .string("initial"), "render_status": .string("ready")]]
    }
    func editorCommit(itemID: String, variantID: String, request: EditorCommitRequest) async throws -> EditorCommitResponse {
        commitCount += 1; lastRequest = request; lastItemID = itemID
        if suspendNextCommit {
            suspendNextCommit = false
            commitIsSuspended = true
            await withCheckedContinuation { continuation in
                if commitResumeRequested {
                    commitResumeRequested = false
                    continuation.resume()
                } else {
                    commitContinuation = continuation
                }
            }
            commitIsSuspended = false
        }
        if let commitError { throw commitError }
        return commitResponse ?? EditorCommitResponse(ok: true, generation: "next", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
    }
    func resumeCommit() {
        if let commitContinuation {
            commitContinuation.resume()
            self.commitContinuation = nil
        } else {
            commitResumeRequested = true
        }
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
