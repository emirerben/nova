import AVFoundation
import KriaMediaEngine
import UIKit
import XCTest
@testable import Kria

@MainActor
final class NativeEditorSessionTests: XCTestCase {
    func testDeviceTimelineDurationUsesShorterOriginalAndKeepsEOFMargin() throws {
        XCTAssertEqual(try XCTUnwrap(NativeEditorSession.deviceTimelineDuration(proxyDuration: 2.2, localDuration: 2.0, minimum: 0.1)),
                       1.95, accuracy: 0.0001)
        XCTAssertNil(NativeEditorSession.deviceTimelineDuration(proxyDuration: 0.14, localDuration: 0.14, minimum: 0.1))
        XCTAssertNil(NativeEditorSession.deviceTimelineDuration(proxyDuration: nil, localDuration: nil, minimum: 0.1))
    }

    func testAdmittedTimelinePhotoAppendsThreeSecondPlacementAndUndoRedo() async throws {
        let threadID = UUID()
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["editor_revision_number"] = .number(7)
        authoritative["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true),
            "phone_editor_media": .object([
                "enabled": .bool(true), "source_registration": .bool(true),
                "visual_kinds": .array([.string("image")]),
            ]),
        ])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant",
            draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1",
            snapshot: [:], canUndo: false, createdAt: .now), authoritativeVariant: authoritative)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "variant", clientImportID: UUID(),
            baseGeneration: "generation-1", guidedRevisionNumber: 7, sourceKind: .visual)
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: nil)
        let uploads = BackgroundUploadCoordinator(api: fake, defaultsKey: "timeline-placement-\(UUID().uuidString)", sessionConfiguration: .ephemeral)
        uploads.beginEditorPlacement(placement)
        session.useMediaUploads(uploads)
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "photo",
            sourceIndex: 4, source: nil, error: nil, reasonCode: nil, retryable: false)
        let before = session.document.clips.count
        try await session.placeEditorSource(placement, response: response)
        let inserted = try XCTUnwrap(session.document.clips.last)
        XCTAssertEqual(session.document.clips.count, before + 1)
        XCTAssertEqual(inserted.durationS, 3)
        XCTAssertEqual(inserted.raw["editor_source_placement_id"], .string(placement.id.uuidString))
        session.undo(); XCTAssertEqual(session.document.clips.count, before)
        session.redo(); XCTAssertEqual(session.document.clips.count, before + 1)
    }

    func testAdmittedFootageUsesShorterOriginalWithEOFMarginAndDeduplicatesReadyCallback() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 1)
        uploads.beginEditorPlacement(placement)
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "proxy",
            sourceIndex: 9, source: ["duration_s": .number(1.2)], error: nil, reasonCode: nil, retryable: false)
        let before = session.document.clips.count
        try await session.placeEditorSource(placement, response: response)
        XCTAssertEqual(session.document.clips.count, before + 1)
        XCTAssertEqual(try XCTUnwrap(session.document.clips.last?.durationS), 0.95, accuracy: 0.0001)
        // The acknowledgement removes the ledger intent; a duplicated ready
        // callback must not resurrect a user-visible slot.
        try await session.placeEditorSource(placement, response: response)
        XCTAssertEqual(session.document.clips.count, before + 1)
    }

    func testCanceledOrStalePlacementNeverAppends() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "proxy",
            sourceIndex: 9, source: ["duration_s": .number(2)], error: nil, reasonCode: nil, retryable: false)
        let canceled = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 2)
        uploads.beginEditorPlacement(canceled)
        await uploads.discardEditorPlacement(canceled.id)
        let before = session.document.clips.count
        try await session.placeEditorSource(canceled, response: response)
        XCTAssertEqual(session.document.clips.count, before)
        var staleTarget = target
        staleTarget = EditorSourceRegistrationTarget(itemID: staleTarget.itemID, variantID: staleTarget.variantID,
            clientImportID: UUID(), baseGeneration: "stale", guidedRevisionNumber: staleTarget.guidedRevisionNumber, sourceKind: .footage)
        let stale = PendingEditorSourcePlacement(id: UUID(), target: staleTarget, lane: .timeline, visual: nil, localDurationS: 2)
        uploads.beginEditorPlacement(stale)
        try await session.placeEditorSource(stale, response: response)
        XCTAssertEqual(session.document.clips.count, before)
    }

    func testLeavingEditorKeepsReadyImportPendingWithoutAppendingToHiddenDocument() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 2)
        uploads.beginEditorPlacement(placement)
        session.suspendEditorImports()
        let before = session.document.clips.count
        try await session.placeEditorSource(placement, response: .init(importID: target.clientImportID, status: "ready",
            sourceID: "proxy", sourceIndex: 9, source: ["duration_s": .number(2)], error: nil, reasonCode: nil, retryable: false))
        XCTAssertEqual(session.document.clips.count, before)
        XCTAssertTrue(uploads.containsEditorPlacement(placement.id))
    }

    func testReadyImportRechecksCapacityAfterOtherPlacementsFillTimeline() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "proxy",
            sourceIndex: 9, source: ["duration_s": .number(1)], error: nil, reasonCode: nil, retryable: false)
        while session.document.clips.count < 20 {
            let nextTarget = EditorSourceRegistrationTarget(itemID: target.itemID, variantID: target.variantID,
                clientImportID: UUID(), baseGeneration: target.baseGeneration, guidedRevisionNumber: 7, sourceKind: .footage)
            let placement = PendingEditorSourcePlacement(id: UUID(), target: nextTarget, lane: .timeline, visual: nil, localDurationS: 1)
            uploads.beginEditorPlacement(placement)
            try await session.placeEditorSource(placement, response: response)
        }
        let late = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 1)
        uploads.beginEditorPlacement(late)
        do {
            try await session.placeEditorSource(late, response: response)
            XCTFail("A ready import must not exceed the current twenty-clip limit")
        } catch {}
        XCTAssertEqual(session.document.clips.count, 20)
        XCTAssertTrue(uploads.containsEditorPlacement(late.id))
    }

    func testFootageEditsPersistAndUndoWithoutChangingTimelineWindows() throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let original = session.document
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let windows = session.timelineClips.map { [$0.start, $0.end] }
        session.setFootagePlaybackRate(selection, rate: 0.5)
        session.setFootageCrop(selection, crop: .init(x: 0.1, y: 0.2, width: 0.7, height: 0.6))
        let restored = EditorDocument(snapshot: session.document.encodeSnapshot())
        XCTAssertEqual(restored.clips.first?.raw["playback_rate"], .number(0.5))
        XCTAssertEqual(restored.clips.first?.raw["source_crop"]?.objectValue?["y"], .number(0.2))
        XCTAssertEqual(session.timelineClips.map { [$0.start, $0.end] }, windows)
        session.undo()
        XCTAssertNil(session.footageCrop(for: selection))
        XCTAssertEqual(session.footagePlaybackRate(for: selection), 0.5)
        session.undo()
        XCTAssertEqual(session.document, original)
    }

    /// The server closes `clips.source_crop/playback_rate/looks` on device
    /// variants because the phone compiler rejects them. A value saved before
    /// that clamp must still clear, as an explicit null the guided writer honors.
    func testDeviceVariantRefusesNewCropSpeedAndLookButClearsSavedOnes() async throws {
        let (session, fake) = await Self.footageSession(destination: "device", operationsEditable: false, slot: [
            "source_crop": .object(["x": .number(0.1), "y": .number(0.1), "width": .number(0.5), "height": .number(0.5)]),
            "playback_rate": .number(2),
            "look_preset": .string("golden_hour"),
        ])
        XCTAssertTrue(session.rendersOnDevice)
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let slotID = try XCTUnwrap(session.document.clips.first?.id)
        let original = session.document

        session.setFootageCrop(selection, crop: .init(x: 0.2, y: 0.2, width: 0.6, height: 0.6))
        session.setFootagePlaybackRate(selection, rate: 0.5)
        session.setClipLookPreset(clipID: slotID, preset: "olive_film")
        session.setClipLookAdjustments(clipID: slotID, adjustments: ["exposure": .number(0.2)])
        XCTAssertEqual(session.document, original, "A device variant can't take a new crop, speed or look")
        XCTAssertFalse(session.hasUnsavedChanges)

        session.setFootageCrop(selection, crop: nil)
        session.setFootagePlaybackRate(selection, rate: 1)
        session.setClipLookPreset(clipID: slotID, preset: "none")
        let slot = try XCTUnwrap(session.document.clips.first)
        XCTAssertEqual(slot.raw["source_crop"], .null)
        XCTAssertEqual(slot.raw["playback_rate"], .null)
        XCTAssertEqual(slot.lookPreset, "none")
        XCTAssertNil(session.footageCrop(for: selection))
        XCTAssertEqual(session.footagePlaybackRate(for: selection), 1)
        XCTAssertTrue(session.hasUnsavedChanges)

        await session.save()
        let sent = try XCTUnwrap(fake.lastRequest?.timelineSlots?.first?.objectValue)
        XCTAssertEqual(sent["source_crop"], .null, "An omitted key would keep the stored crop")
        XCTAssertEqual(sent["playback_rate"], .null)
    }

    /// KRI-132 journey fix: a device-rendered edit's Add-clip control is silently
    /// disabled (`NativeEditorTimelineView.canAddClip`) -- the session exposes why,
    /// mirroring `visualImportUnavailableMessage`'s `rendersOnDevice` case.
    func testAddClipUnavailableMessageExplainsDeviceRenderedEditsOnly() async throws {
        let (device, _) = await Self.footageSession(destination: "device", operationsEditable: false)
        XCTAssertNotNil(device.addClipUnavailableMessage)
        let (cloud, _) = await Self.footageSession(destination: "cloud", operationsEditable: true)
        XCTAssertNil(cloud.addClipUnavailableMessage)
    }

    func testCloudVariantKeepsCropSpeedAndLookEditable() async throws {
        let (session, _) = await Self.footageSession(destination: "cloud", operationsEditable: true)
        XCTAssertFalse(session.rendersOnDevice)
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let slotID = try XCTUnwrap(session.document.clips.first?.id)

        session.setFootageCrop(selection, crop: .init(x: 0.1, y: 0.2, width: 0.7, height: 0.6))
        session.setFootagePlaybackRate(selection, rate: 0.5)
        session.setClipLookPreset(clipID: slotID, preset: "olive_film")
        var slot = try XCTUnwrap(session.document.clips.first)
        XCTAssertEqual(slot.raw["source_crop"]?.objectValue?["y"], .number(0.2))
        XCTAssertEqual(slot.raw["playback_rate"], .number(0.5))
        XCTAssertEqual(slot.lookPreset, "olive_film")

        // The saved slot never had these keys, so a reset drops them again.
        session.setFootageCrop(selection, crop: nil)
        session.setFootagePlaybackRate(selection, rate: 1)
        slot = try XCTUnwrap(session.document.clips.first)
        XCTAssertNil(slot.raw["source_crop"])
        XCTAssertNil(slot.raw["playback_rate"])
    }

    func testClosedClipCropCapabilityIsHonoredOnCloudVariants() async throws {
        let (session, _) = await Self.footageSession(destination: "cloud", operationsEditable: false)
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let original = session.document

        session.setFootageCrop(selection, crop: .init(x: 0.1, y: 0.2, width: 0.7, height: 0.6))
        session.setFootagePlaybackRate(selection, rate: 0.5)
        XCTAssertEqual(session.document, original, "`clips.*` wins over the broader `timeline` capability")
    }

    func testRenderedRebaseRefreshesGenerationSourcesButLocalEditsDoNot() async {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        XCTAssertEqual(fake.sourcePoolCallCount, 1)
        let clip = try! XCTUnwrap(session.timelineClips.first)
        session.setClipTiming(clipID: clip.id, durationS: 1.5)
        session.undo()
        XCTAssertEqual(fake.sourcePoolCallCount, 1, "Local edit and undo retain immutable inputs")

        let refresh = expectation(description: "Resolve sources for rendered generation")
        fake.sourcePoolExpectation = refresh
        let sourcePlayer = try! XCTUnwrap(session.player)
        XCTAssertTrue(session.rebaseCleanDraft(from: Self.variant(duration: 2, generation: "g2")))
        XCTAssertEqual(session.sourcePreviewState, .preparing, "Old preview must not remain ready")
        let finishedURL = try! XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        session.installFinishedRenderPlayer(url: finishedURL)
        XCTAssertTrue(session.canDisplayCurrentPlayer, "The completed render stays visible while editable sources rebuild")
        XCTAssertFalse(session.player === sourcePlayer, "Authoritative playback replaces the stale source composition")
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        session.pausePlayback()
        await fulfillment(of: [refresh], timeout: 3)
        XCTAssertEqual(fake.sourcePoolCallCount, 2)
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertTrue(session.rebaseCleanDraft(from: Self.variant(duration: 2, generation: "g2")))
        XCTAssertEqual(fake.sourcePoolCallCount, 2, "Repeated authority keeps the same generation inputs")
    }

    func testLegacyConversationRefreshDoesNotUseRuntimeTwoDraftOrEraseLocalEdits() async throws {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now), draftError: .conflict,
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let project = ProjectSummary(id: UUID(), title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        let original = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.draftCallCount, 0)
        XCTAssertEqual(session.document, original)
        XCTAssertEqual(session.saveState, .idle)
        let clip = try XCTUnwrap(session.timelineClips.first)
        session.setClipTiming(clipID: clip.id, durationS: 1.5)
        let local = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, local)
        XCTAssertTrue(session.hasUnsavedChanges)
        fake.authoritativeVariant = Self.variant(duration: 2, generation: "g2")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, local)
        XCTAssertEqual(session.saveState, .conflict)
        XCTAssertEqual(fake.draftCallCount, 0)
    }

    func testLegacyRefreshRejectsMissingOrUnknownSelectionWithoutLoadedTargetFallback() async throws {
        let jobID = UUID()
        let threadID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let project = ProjectSummary(id: threadID, title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        let original = session.document
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        for state in ["{}", #"{"selected_variant_id":"unknown"}"#] {
            fake.refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
            {
              "id":"\#(threadID.uuidString)","title":"Legacy","status":"active",
              "revision":8,"runtime_version":1,
              "active_job_id":"\#(jobID.uuidString)","active_plan_item_id":"item",
              "state":\#(state),
              "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[
                {"variant_id":"initial","render_status":"ready"},
                {"variant_id":"other","render_status":"ready"}
              ]},"updated_at":"2026-09-09T08:00:00Z"
            }
            """#.utf8))
            fake.lastVariantID = nil
            await session.synchronizePromptRevision()
            XCTAssertNil(fake.lastVariantID, "Invalid projection must not fetch the loaded variant")
            XCTAssertEqual(session.document, original)
            XCTAssertFalse(session.hasUnsavedChanges)
            XCTAssertEqual(fake.draftCallCount, 0)
            guard case .refreshFailed = session.saveState else {
                return XCTFail("Invalid selection must report a refresh failure")
            }
        }
    }

    func testLegacyVisualRemovalRefreshKeepsSelectedRenderingVariant() async throws {
        let jobID = UUID()
        let threadID = UUID()
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let projection = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)","title":"Legacy","status":"active",
          "revision":8,"runtime_version":1,
          "active_job_id":"\#(jobID.uuidString)","active_plan_item_id":"item",
          "state":{"selected_variant_id":"selected"},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready_partial","variants":[
            {"variant_id":"other","render_status":"ready","output_url":"https://example.com/other.mp4"},
            {"variant_id":"selected","render_status":"rendering","render_generation_id":"g2"}
          ]},"updated_at":"2026-09-09T08:00:00Z"
        }
        """#.utf8))
        XCTAssertEqual(projection.summary.outputVariantID, "other", "Playback falls back while the selected edit renders")
        var variant = Self.variant(duration: 2, generation: "g1")
        variant["variant_id"] = .string("selected")
        // Stable source IDs let this assertion compare the complete clip lane.
        variant["user_timeline"] = .object(["slots": .array([.object([
            "slot_id": .string(UUID().uuidString), "clip_index": .number(0),
            "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(4),
        ])])])
        variant["text_elements"] = .array([.object([
            "id": .string(UUID().uuidString), "text": .string("Keep this title"),
            "start_s": .number(0), "end_s": .number(2),
        ])])
        variant["audio_mix"] = .object(["original_level": .number(0.7)])
        variant["visual_blocks"] = .array([.object([
            "id": .string("uploaded-photo"), "kind": .string("media"),
            "start_s": .number(0), "end_s": .number(2),
            "src_gcs_path": .string("owned/photo.png"),
        ])])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "selected", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now), draftError: .conflict,
            authoritativeVariant: variant, refreshedThread: projection)
        let project = ProjectSummary(id: threadID, title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "selected", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        let before = session.document
        XCTAssertEqual(before.visualBlocks.count, 1)
        variant["visual_blocks"] = .array([])
        variant["render_generation_id"] = .string("g2")
        variant["render_status"] = .string("rendering")
        fake.authoritativeVariant = variant
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.lastVariantID, "selected")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertTrue(session.document.visualBlocks.isEmpty)
        XCTAssertEqual(session.document.clips, before.clips)
        XCTAssertEqual(session.document.textElements, before.textElements)
        XCTAssertEqual(session.document.mix, before.mix)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(fake.draftCallCount, 0)

        let accepted = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, accepted)
        XCTAssertEqual(session.saveState, .idle)

        // A subsequent explicit selection is authoritative even if both cuts
        // happen to carry an identical editor document.
        fake.refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)","title":"Legacy","status":"active",
          "revision":9,"runtime_version":1,
          "active_job_id":"\#(jobID.uuidString)","active_plan_item_id":"item",
          "state":{"selected_variant_id":"other"},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[
            {"variant_id":"other","render_status":"ready"},
            {"variant_id":"selected","render_status":"ready"}
          ]},"updated_at":"2026-09-09T08:00:01Z"
        }
        """#.utf8))
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.lastVariantID, "other")
    }

    func testLegacyRefreshAfterManualSaveAndConflictRebasePreservesNewLocalEdit() async throws {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now), draftError: .conflict,
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"),
            commitResponse: EditorCommitResponse(ok: false, generation: "g2",
                sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false),
                revisionNumber: nil, revisionHash: nil, expectedDuration: nil))
        let project = ProjectSummary(id: UUID(), title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 1.5)
        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertFalse(session.hasUnsavedChanges)
        let saved = Self.variant(duration: 1.5, generation: "g2")
        fake.authoritativeVariant = saved
        XCTAssertTrue(session.rebaseCleanDraft(from: saved))
        session.saveState = .saved
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 1.2)
        let afterSave = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, afterSave)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertNotEqual(session.saveState, .conflict)

        fake.authoritativeVariant = Self.variant(duration: 1.4, generation: "g3")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .conflict)
        await session.rebaseAfterConflict()
        let rebased = session.document
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(rebased.revision.baseGeneration, "g3")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, rebased)
        XCTAssertNotEqual(session.saveState, .conflict)
        XCTAssertEqual(fake.draftCallCount, 0)
    }

    func testPromptRefreshRecoveryClearsFailureForUnchangedDocument() async {
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        let baseline = session.document
        fake.draftError = .requestFailed(status: 503)
        await session.synchronizePromptRevision()
        guard case .refreshFailed = session.saveState else { return XCTFail("Expected refresh failure") }
        // Repeated failures must retain the original state, not the last error.
        await session.synchronizePromptRevision()
        fake.draftError = nil
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, baseline)
        XCTAssertEqual(session.saveState, .idle)
    }

    func testPromptRefreshRecoveryPreservesUnrelatedSaveAndRenderFailures() async {
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        let failures: [NativeEditorSaveState] = [.failed("Save failed"), .renderRetryNeeded("Render failed")]
        for failure in failures {
            session.saveState = failure
            fake.draftError = .requestFailed(status: 503)
            await session.synchronizePromptRevision()
            fake.draftError = nil
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.saveState, failure, "Restore the failure that preceded the refresh")

            session.saveState = .idle
            fake.draftError = .requestFailed(status: 503)
            await session.synchronizePromptRevision()
            session.saveState = failure
            fake.draftError = nil
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.saveState, failure, "Keep a newer save/render failure")
        }
    }

    func testOrdinaryComposedPauseResumeDoesNotSeekOrRenderAnotherStill() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        let original = try XCTUnwrap(session.player?.currentItem)
        let item = AVPlayerItem(asset: original.asset)
        item.videoComposition = original.videoComposition
        let player = DelayedSeekPlayer(playerItem: item)
        session.player = player
        session.togglePlayback()
        XCTAssertEqual(player.targets.count, 1, "Initial scrub surface needs one handoff")
        player.completeSeek()
        await Task.yield()
        await Task.yield()
        for _ in 0..<3 {
            session.pausePlayback()
            XCTAssertFalse(session.isPlaying)
            XCTAssertNil(session.scrubPreviewFrame, "Pause retains the player surface")
            session.togglePlayback()
        }
        XCTAssertEqual(player.targets.count, 1, "Ordinary resume must not restart decoding with an exact seek")
        session.pausePlayback()
    }

    func testFixtureSourceFailureRetainsAndPlaysInitialFinishedRender() async throws {
        let renderURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText, initialPlaybackURL: renderURL)
        let initialPlayer = try XCTUnwrap(session.player)

        await session.prepareFixtureSourcePreview(
            url: URL(fileURLWithPath: "/tmp/kria-invalid-source-fixture.mp4"),
            forceFailure: true
        )

        guard case .failed = session.sourcePreviewState else { return XCTFail("Fixture source failure must remain visible") }
        XCTAssertTrue(session.isShowingRenderedFallback)
        XCTAssertTrue(session.canDisplayCurrentPlayer)
        XCTAssertTrue(session.player === initialPlayer, "The initial finished-render player remains available")
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        session.pausePlayback()
    }

    func testDownloadUsesVisibleSourcePreviewInsteadOfOlderDeviceFile() async throws {
        let olderDeviceFile = URL(fileURLWithPath: "/tmp/kria-older-device-render.mp4")
        let session = NativeEditorSession(
            draft: NativeEditorUITestFixtures.sourceText,
            initialPlaybackURL: olderDeviceFile
        )
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))

        await session.prepareFixtureSourcePreview(url: sourceURL)

        XCTAssertTrue(session.hasSourcePreview)
        XCTAssertEqual(try session.videoDownloadRoute(deviceLocalFile: olderDeviceFile), .sourcePreview)
    }

    func testDownloadRejectsStaleFinishedRenderWhileCurrentPreviewPrepares() async throws {
        let olderRender = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(
            draft: NativeEditorUITestFixtures.sourceText,
            initialPlaybackURL: olderRender
        )

        let preparation = Task {
            await session.prepareFixtureSourcePreview(url: olderRender, delayedLoad: true)
        }
        await Task.yield()

        XCTAssertThrowsError(try session.videoDownloadRoute(deviceLocalFile: olderRender))
        await preparation.value
    }

    func testDisplayedSourcePreviewExportsAPlayableVideo() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: sourceURL)

        let exported = try await session.exportDisplayedSourcePreview()
        defer { try? FileManager.default.removeItem(at: exported.cleanupURL) }

        XCTAssertTrue(FileManager.default.fileExists(atPath: exported.fileURL.path))
        // This is the file the user saves to Photos, so it is a published
        // video and carries the brand outro after the edit. `session.duration`
        // is the editor's own timeline, which deliberately does not include it
        // — branding is added by the exporter, not by the composition the
        // creator scrubs.
        let outroURL = try XCTUnwrap(KriaBranding.outroURL())
        let outro = try await AVURLAsset(url: outroURL).load(.duration).seconds
        let duration = try await AVURLAsset(url: exported.fileURL).load(.duration).seconds
        XCTAssertEqual(duration, session.duration + outro, accuracy: 0.1)
    }

    func testProjectSessionUsesFreshPlaybackHandoffBeforeHydration() throws {
        let staleURL = URL(fileURLWithPath: "/tmp/kria-stale-project-render.mp4")
        let freshURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let project = ProjectSummary(
            id: UUID(), title: "Gallery edit", status: .ready, updatedAt: .now,
            posterURL: nil, outputURL: staleURL
        )

        let session = NativeEditorSession(project: project, initialPlaybackURL: freshURL)

        XCTAssertEqual(session.loadState, .idle)
        XCTAssertTrue(session.canDisplayCurrentPlayer, "The result screen's player is available before editor hydration")
        let asset = try XCTUnwrap(session.player?.currentItem?.asset as? AVURLAsset)
        XCTAssertEqual(asset.url, freshURL)
    }

    /// KRI-91: opening a project (the shared chat-editor session's path) with
    /// no explicit playback URL falls back to `project.outputURL` — a
    /// possibly long-stale cached summary, not something anyone just
    /// fetched for this screen. That seed must not display until `load()`
    /// confirms it, unlike the explicit-URL case above.
    func testUncachedProjectOutputURLDoesNotDisplayBeforeHydration() throws {
        let cachedURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let project = ProjectSummary(
            id: UUID(), title: "Reopened project", status: .ready, updatedAt: .now,
            posterURL: nil, outputURL: cachedURL
        )

        let session = NativeEditorSession(project: project)

        XCTAssertEqual(session.loadState, .idle)
        XCTAssertNotNil(session.player, "The cached URL still seeds the player so playback can start the instant it's confirmed")
        XCTAssertFalse(session.canDisplayCurrentPlayer, "An unconfirmed cached seed must not display — it may already be stale")
    }

    func testNeedsReloadIsTrueBeforeAnyLoadAndFalseAfterMatchingRevision() async {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let project = ProjectSummary(
            id: UUID(), title: "Revision check", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", serverRevision: 4,
            activeJobID: jobID, activePlanItemID: "item"
        )
        let session = NativeEditorSession(project: project)
        XCTAssertTrue(session.needsReload(for: project), "A never-loaded session always needs its first load")

        await session.load(project: project, api: fake)
        XCTAssertFalse(session.needsReload(for: project), "The same revision the session just loaded does not need a reload")

        let newerProject = ProjectSummary(
            id: project.id, title: project.title, status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", serverRevision: 5,
            activeJobID: jobID, activePlanItemID: "item"
        )
        XCTAssertTrue(
            session.needsReload(for: newerProject),
            "A shared session left open across a server-side change must reload rather than keep showing the stale video indefinitely"
        )
    }

    /// The editor installs the last finished cloud render immediately on
    /// open so something is on screen right away, then swaps to a local,
    /// editable composition built straight from the current document. If
    /// the server's render hasn't caught up with the last save yet
    /// (render_status != "ready"), that finished render must not display
    /// during the swap window — otherwise a fresh title/text edit flashes
    /// its pre-edit styling for the few seconds the swap takes.
    func testUncaughtUpRenderStaysHiddenWhilePreviewPreparesUntilConfirmedCurrent() async throws {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        // A save queued a re-render that hasn't finished yet: the render
        // this response's output_url points to predates the document the
        // rest of this same response describes.
        fake.authoritativeVariant?["render_status"] = .string("rendering")
        fake.suspendNextSourcePool = true
        let project = ProjectSummary(
            id: UUID(), title: "Rendering", status: .rendering, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial",
            activeJobID: jobID, activePlanItemID: "item"
        )
        let session = NativeEditorSession(project: project)

        let loadTask = Task { await session.load(project: project, api: fake) }
        for _ in 0..<100 where !fake.sourcePoolIsSuspended {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(fake.sourcePoolIsSuspended, "the source-pool gate did not suspend in time")

        XCTAssertEqual(session.sourcePreviewState, .preparing)
        XCTAssertNotNil(session.player, "the not-yet-current render is still installed as a fallback")
        XCTAssertFalse(
            session.canDisplayCurrentPlayer,
            "A render that predates the current document must not display while the current-document preview is still preparing"
        )

        fake.resumeSourcePool()
        await loadTask.value
    }

    func testFixtureSourceFailureDoesNotPlayStaleEditablePreview() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertTrue(session.hasSourcePreview)

        await session.prepareFixtureSourcePreview(
            url: URL(fileURLWithPath: "/tmp/kria-invalid-source-fixture.mp4"),
            forceFailure: true
        )

        guard case .failed = session.sourcePreviewState else { return XCTFail("Fixture source failure must remain visible") }
        XCTAssertFalse(session.isShowingRenderedFallback)
        XCTAssertFalse(session.canDisplayCurrentPlayer)
        session.togglePlayback()
        XCTAssertFalse(session.isPlaying, "A stale editable preview cannot play after source preparation fails")
    }

    func testPauseDuringScrubHandoffRetainsTargetWithoutRestartingPlayback() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        let player = DelayedSeekPlayer()
        session.player = player
        session.seek(to: 1.2)
        session.togglePlayback()
        session.reconcilePlaybackState(.paused)
        XCTAssertTrue(session.isPlaying, "Transient paused observation must not cancel play intent")
        session.pausePlayback()
        XCTAssertEqual(session.currentTime, 1.2, accuracy: 0.001)
        XCTAssertEqual(player.targets.count, 2)
        player.completeSeek() // Invalidated paused seek.
        player.completeSeek() // Actual playback handoff.
        await Task.yield()
        await Task.yield()
        XCTAssertFalse(session.isPlaying)
        XCTAssertEqual(player.rate, 0, "Late handoff completion must never restart a paused player")
    }

    /// KRI-95: the real editor's scrub path now records seek latency the same
    /// way MediaDiagnosticView's debug harness always did, so KRI-97's
    /// physical-device seek-p95 measurement has real numbers to read.
    func testScrubRecordsSeekLatencyIntoPreviewInstrumentation() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        XCTAssertTrue(session.previewInstrumentation.snapshot().isEmpty)
        session.seek(to: 0.5)
        for _ in 0..<100 {
            if session.scrubPreviewFrame != nil { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertNotNil(session.scrubPreviewFrame)
        let events = session.previewInstrumentation.snapshot()
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events.first?.name, .seekLatency)
        XCTAssertGreaterThanOrEqual(events.first?.value ?? -1, 0)
    }

    func testBufferingDuringPlayHandoffCannotLeaveScrubImageOverMovingVideo() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        session.seek(to: 0.5)
        for _ in 0..<100 {
            if session.scrubPreviewFrame != nil { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertNotNil(session.scrubPreviewFrame)
        let player = DelayedSeekPlayer()
        session.player = player
        session.togglePlayback()
        // Buffering arrives before the play-position seek completion.
        session.reconcilePlaybackState(.waitingToPlayAtSpecifiedRate)
        XCTAssertTrue(session.isPlaying, "Buffering must preserve the play request")
        player.completeSeek(finished: false)
        await Task.yield()
        await Task.yield()
        session.reconcilePlaybackState(.playing)
        XCTAssertNil(session.scrubPreviewFrame, "A moving player must never stay covered by its old scrub still")
        let seeks = player.targets.count
        session.pausePlayback()
        session.togglePlayback()
        XCTAssertEqual(player.targets.count, seeks, "A recovered player must not retain a stale scrub target")
        player.pause()
    }

    func testStalledPausedSeekRecoversNewestTargetAndIgnoresLateCompletion() async {
        let session = NativeEditorSession()
        session.duration = 60
        let player = DelayedSeekPlayer()
        session.player = player
        session.seek(to: 18)
        let recovery = player.expectSeek(to: 17)
        session.seek(to: 17)
        await fulfillment(of: [recovery], timeout: 3)
        XCTAssertEqual(player.targets, [18, 17])
        session.seek(to: 16)
        let newest = player.expectSeek(to: 16)
        player.completeSeek() // The stalled request completes after replacement.
        player.completeSeek()
        await fulfillment(of: [newest], timeout: 3)
        XCTAssertEqual(player.targets, [18, 17, 16])
        XCTAssertEqual(session.currentTime, 16)
        let noFurtherSeek = player.expectNoSeek()
        player.completeSeek()
        await fulfillment(of: [noFurtherSeek], timeout: 0.5)
    }

    func testStalledFinalScrubSampleRetriesWithoutAnotherFingerEvent() async {
        let session = NativeEditorSession()
        session.duration = 60
        let player = DelayedSeekPlayer()
        session.player = player
        session.seek(to: 18)
        let recovery = player.expectSeek(to: 18)
        await fulfillment(of: [recovery], timeout: 3)
        XCTAssertEqual(player.targets, [18, 18])
        let noFurtherSeek = player.expectNoSeek()
        player.completeSeek()
        player.completeSeek()
        await fulfillment(of: [noFurtherSeek], timeout: 0.5)
        XCTAssertEqual(player.targets, [18, 18])
    }

    func testRapidScrubbingCoalescesDecoderSeeksAndKeepsLatestClock() async {
        let session = NativeEditorSession()
        session.duration = 60
        let player = DelayedSeekPlayer()
        session.player = player
        for step in 1...100 { session.seek(to: Double(step) / 10) }
        XCTAssertEqual(session.currentTime, 10)
        XCTAssertEqual(player.targets, [0.1])
        let latest = player.expectSeek(to: 10)
        player.completeSeek()
        await fulfillment(of: [latest], timeout: 3)
        XCTAssertEqual(player.targets, [0.1, 10])
        XCTAssertEqual(session.currentTime, 10)
        let next = player.expectSeek(to: 2)
        player.completeSeek()
        session.seek(to: 2)
        await fulfillment(of: [next], timeout: 3)
        XCTAssertEqual(player.targets, [0.1, 10, 2])
        let noFurtherSeek = player.expectNoSeek()
        player.completeSeek()
        await fulfillment(of: [noFurtherSeek], timeout: 0.5)
    }

    func testMusicSourceSurvivesUUIDCaseRoundTripWithoutPublicCatalog() {
        let id = UUID().uuidString
        let url = "https://media.example.test/owned-track.m4a"
        XCTAssertEqual(NativeEditorSession.previewMusicURL(trackID: id, variant: [
            "music_track_id": .string(id.lowercased()), "music_preview_url": .string(url)
        ]), URL(string: url))
        XCTAssertEqual(NativeEditorSession.previewMusicURL(trackID: id, variant: [
            "background_music": .object(["track_id": .string(id.lowercased()), "preview_url": .string(url)])
        ]), URL(string: url))
        XCTAssertNil(NativeEditorSession.previewMusicURL(trackID: UUID().uuidString, variant: [
            "music_track_id": .string(id.lowercased()), "music_preview_url": .string(url)
        ]))
    }

    func testCanonicalTextIdentityPreservesTimedStoryLayersAcrossDraftBridge() throws {
        let rows: [JSONValue] = (0..<172).map { index in
            .object(["id": .string("story-label-\(index)"), "text": .string("Caption \(index)"),
                     "start_s": .number(Double(index) * 0.25), "end_s": .number(Double(index + 1) * 0.25),
                     "font_family": .string("Fraunces"), "size_px": .number(64), "effect": .string("pop-in")])
        }
        let snapshot = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "guided_story",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: ["editor_payload": .object(["sections": .object(["text_elements": .array(rows)])])],
            canUndo: false, createdAt: .now)
        let draft = snapshot.editorDraft(projectID: UUID())
        let session = NativeEditorSession(draft: draft)
        XCTAssertEqual(session.document.textElements.count, 172)
        XCTAssertEqual(session.document.textElements.filter { $0.startS <= 0 && $0.endS > 0 }.count, 1)
        for (index, layer) in session.document.textElements.enumerated() {
            XCTAssertEqual(layer.id, "story-label-\(index)")
            XCTAssertEqual(layer.startS, Double(index) * 0.25)
            XCTAssertEqual(layer.endS, Double(index + 1) * 0.25)
            XCTAssertEqual(layer.raw["size_px"], .number(64))
            XCTAssertEqual(layer.raw["effect"], .string("pop-in"))
        }
        let roundTrip = try JSONDecoder().decode(EditorDraft.self, from: JSONEncoder().encode(draft))
        XCTAssertEqual(roundTrip.text.first?.canonicalID, "story-label-0")
    }

    func testTextTransformUsesGestureBaselineAndSingleUndo() {
        let session = NativeEditorSession()
        session.addText(content: "Scale and rotate")
        let id = session.document.textElements[0].id
        session.setTextSize(id: id, sizePX: 80)
        session.setTextWidth(id: id, width: 0.5)
        let baseline = session.document.textElements[0]
        let before = session.document
        session.beginDirectManipulation()
        session.transformText(from: baseline, scale: 1.2, rotationDelta: 10)
        session.transformText(from: baseline, scale: 1.5, rotationDelta: 35)
        session.endDirectManipulation()
        let result = session.document.textElements[0]
        XCTAssertEqual(result.raw["size_px"], .number(120))
        XCTAssertEqual(result.raw["max_width_frac"], .number(0.75))
        XCTAssertEqual(result.raw["rotation_deg"], .number(35))
        session.undo()
        XCTAssertEqual(session.document, before)
        session.redo()
        XCTAssertEqual(session.document.textElements[0], result)
    }

    func testTextScalingContinuesBeyondCanvasAndFormerSizeLimit() {
        let session = NativeEditorSession()
        session.addText(content: "Large text")
        let id = session.document.textElements[0].id
        session.setTextSize(id: id, sizePX: 100)
        let baseline = session.document.textElements[0]
        session.transformText(from: baseline, scale: 12, rotationDelta: 0)
        XCTAssertEqual(session.document.textElements[0].raw["size_px"]?.numberValue ?? 0, 1200, accuracy: 0.001)
        XCTAssertGreaterThan(session.document.textElements[0].raw["max_width_frac"]?.numberValue ?? 0, 1)
        session.setTextSize(id: id, sizePX: 1600)
        XCTAssertEqual(session.document.textElements[0].raw["size_px"]?.numberValue ?? 0, 1600, accuracy: 0.001)
        session.setTextSize(id: id, sizePX: .infinity)
        XCTAssertEqual(session.document.textElements[0].raw["size_px"]?.numberValue ?? 0, 1600, accuracy: 0.001)
    }

    func testPhaseEditsPreserveLegacyEntranceAndUndoTogether() {
        let session = NativeEditorSession()
        session.addText(content: "Motion")
        let id = session.document.textElements[0].id
        session.setTextAnimation(id: id, animation: "pop-in")
        let baseline = session.document
        session.beginTransaction()
        session.setTextPhase(id: id, phase: "exit", effect: "typewriter")
        session.setTextAnimationSpeed(id: id, speed: 2)
        session.endTransaction()
        let phases = NativeEditorSession.textPhases(for: session.document.textElements[0])
        XCTAssertEqual(phases["entrance"], .string("pop"))
        XCTAssertEqual(phases["exit"], .string("typewriter"))
        XCTAssertEqual(phases["speed"], .number(2))
        session.undo()
        XCTAssertEqual(session.document, baseline)
    }

    func testTextDragCrossingCarouselUsesOutputDeltaAndKeepsBaseDuration() {
        let clips: [EditorClip] = [
            EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 4),
            EditorClip(id: UUID(), assetID: UUID(), start: 4, end: 8, trimIn: 0, trimOut: 4, sourceDuration: 4),
        ]
        let snapshot: [String: JSONValue] = ["carousel_moment": .object(["position": .string("middle"), "duration_s": .number(3)])]
        let draft = EditorDraft(projectID: UUID(), clips: clips, text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        session.addText(content: "Move")
        let id = session.document.textElements[0].id
        session.updateTextTiming(id: id, startS: 2, endS: 3)
        let baseline = session.document
        session.beginTimedBodyMove(kind: .text, id: id)
        session.updateTimedBodyMove(by: 2)
        session.updateTimedBodyMove(by: 6)
        session.endTimedBodyMove()
        XCTAssertEqual(session.document.textElements[0].startS, 5)
        XCTAssertEqual(session.document.textElements[0].endS, 6)
        session.undo()
        XCTAssertEqual(session.document, baseline)
    }

    func testPendingTextOnlyCommitsOnDoneAtPlayheadAsOneUndoStep() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 6, trimIn: 0, trimOut: 6, sourceDuration: 6)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.currentTime = 4.5
        session.beginTextCreation()
        session.updatePendingText("One")
        session.updatePendingText("One more game")
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertFalse(session.canUndo)
        let selection = session.finishTextCreation()
        XCTAssertEqual(selection, session.selection)
        XCTAssertEqual(session.document.textElements.first?.text, "One more game")
        XCTAssertEqual(session.document.textElements.first?.startS, 4.5)
        XCTAssertEqual(session.document.textElements.first?.endS, 6)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.canUndo)
        session.redo()
        XCTAssertEqual(session.document.textElements.first?.text, "One more game")
    }

    func testCancelAndEmptyDonePreserveExistingTextAndHistory() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 6, trimIn: 0, trimOut: 6, sourceDuration: 6)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.addText(content: "Keep me")
        let baseline = session.document
        session.beginTextCreation()
        session.updatePendingText("Discard me")
        session.cancelTextCreation()
        XCTAssertEqual(session.document, baseline)
        session.beginTextCreation()
        session.updatePendingText(" \n ")
        XCTAssertNil(session.finishTextCreation())
        XCTAssertEqual(session.document, baseline)
        session.undo()
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.canUndo)
    }

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

    func testHydrationDoesNotSynthesizeAnUnmuteOverrideForAnUntouchedSlot() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 2, trimIn: 0, trimOut: 2, slotID: "slot-a")
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object([
            "timeline_slots": .array([.object(["slot_id": .string("slot-a"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2)])])
        ])])]
        let draft = EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        XCTAssertNil(session.document.clips.first?.raw["muted"])
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
        for absentGeneration in [JSONValue.null, .string("")] {
            var legacy = authoritative
            legacy["render_generation_id"] = absentGeneration
            legacy["render_finished_at"] = .string("live-finished")
            let restored = server.editorDraft(projectID: UUID(), authoritativeVariant: legacy)
            XCTAssertEqual(Self.object(restored.serverSnapshot["editor_payload"])?["base_generation"], .string("live-finished"))
        }
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

    func testPhoneGalleryJobUsesCreationThreadForLocalOriginals() async {
        let jobID = UUID(), threadID = UUID()
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
        var variant = Self.variant(duration: 2, generation: "generation-1")
        variant["render_destination"] = .string("device")
        let fake = EditorCommitSpy(
            draftSnapshot: emptySnapshot,
            openReceipt: OpenInEditorResponse(planItemID: "item-gallery", variantID: "initial", creationThreadID: threadID),
            authoritativeVariant: variant
        )
        let project = ProjectSummary(id: jobID, title: "Gallery cut", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        await session.load(libraryJobID: jobID, api: fake)
        XCTAssertEqual(fake.openedJobID, jobID)
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 2, accuracy: 0.0001)
        XCTAssertNotNil(session.player)

        XCTAssertEqual(session.deviceRenderKey, DeviceRenderKey(projectID: threadID, jobID: jobID, variantID: "initial"))
    }

    /// `NativeEditorView.loadEditor()` calls `showDeviceOutput` with the
    /// device coordinator's local receipt right after a device-rendered
    /// variant loads. That local file must win over whatever
    /// server-signed `output_url` the load installed first — the remote
    /// signature is sized for a short-lived playback window (see
    /// `PLAYBACK_URL_TTL_MIN`), while the on-device export is immediate,
    /// free, and available offline. Only once the local receipt is gone
    /// (cleaned up, reinstalled app, other device) should playback fall
    /// back to the remote URL.
    func testDeviceLocalExportPreferredOverRemoteOutputURL() async {
        let jobID = UUID(), threadID = UUID()
        let emptySnapshot = DraftSnapshot(
            draftID: "unused", itemID: "unused", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now
        )
        var variant = Self.variant(duration: 2, generation: "generation-1")
        variant["render_destination"] = .string("device")
        let fake = EditorCommitSpy(
            draftSnapshot: emptySnapshot,
            openReceipt: OpenInEditorResponse(planItemID: "item-gallery", variantID: "initial", creationThreadID: threadID),
            authoritativeVariant: variant
        )
        let project = ProjectSummary(id: jobID, title: "Gallery cut", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        await session.load(libraryJobID: jobID, api: fake)
        let remoteAsset = try! XCTUnwrap(session.player?.currentItem?.asset as? AVURLAsset)
        XCTAssertEqual(remoteAsset.url, URL(string: "file:///tmp/kria-editor-test.mp4"))

        let localFile = try! XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        session.showDeviceOutput(localFile)

        let localAsset = try! XCTUnwrap(session.player?.currentItem?.asset as? AVURLAsset)
        XCTAssertEqual(localAsset.url, localFile, "The on-device export must win over the remote signed URL when both are available")
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
            draftError: .requestFailed(status: 500)
        )
        let project = ProjectSummary(
            id: threadID, title: "Draft", status: .draft, updatedAt: .now, posterURL: nil
        )
        let session = NativeEditorSession(project: project)

        await session.load(project: project, api: fake)

        XCTAssertEqual(
            session.saveState,
            .loadFailed("Kria hit a problem on its side. Your chat and footage are safe. Try again in a moment.")
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
            editorVariantError: .requestFailed(status: 404)
        )
        let session = NativeEditorSession(project: ProjectSummary(
            id: threadID, title: "Draft", status: .draft, updatedAt: .now, posterURL: nil
        ))

        await session.load(api: fake, threadID: threadID)

        XCTAssertEqual(session.loadState, .failed("Kria couldn’t complete that request."))
        XCTAssertEqual(session.saveState, .loadFailed("Kria couldn’t complete that request."))
    }

    func testHydratedProjectRetainsFinishedOutputWhenSourcePreviewFails() async throws {
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

        let player = try XCTUnwrap(session.player)
        guard case .failed = session.sourcePreviewState else { return XCTFail("Missing sources must remain explicit") }
        XCTAssertTrue(session.isShowingRenderedFallback)
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying, "A finished render remains playable while source preview is unavailable")
        XCTAssertTrue(session.player === player, "The failed source preview must not discard the finished render")
        session.pausePlayback()
        XCTAssertEqual(fake.lastVariantID, "original_text")
    }

    func testAddClipUploadsReservesAndAppendsTimelineSlot() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditTimeline)

        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        // The fixture's default `editorVariant` already seeds one timeline
        // slot at clip_index 0 (see EditorCommitSpy.editorVariant) — the newly
        // added clip mints the next pool index, 1.
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: jobID.uuidString, clipIndex: 1, kind: "video")

        await session.addClip(fileURL: tempURL)

        XCTAssertNil(session.addClipError)
        XCTAssertEqual(fake.uploadFileCalls.count, 1)
        XCTAssertEqual(fake.uploadFileCalls.first?.reservation.gcsPath, "users/u/generative/abc123def456/clip.mp4")
        XCTAssertEqual(fake.addClipCalls.map(\.gcsPath), ["users/u/generative/abc123def456/clip.mp4"])
        XCTAssertEqual(session.document.clips.count, 2)
        XCTAssertEqual(session.document.clips.last?.clipIndex, 1)
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testAddClipSurfacesUploadFailureWithoutMutatingTimeline() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)

        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        // reserveUploadResult left nil — the fake throws .unsupported, matching
        // a reservation failure (e.g. the network call itself failing).

        await session.addClip(fileURL: tempURL)

        XCTAssertNotNil(session.addClipError)
        // Only the fixture's pre-existing slot (see EditorCommitSpy.editorVariant)
        // — the failed upload must not have staged anything new.
        XCTAssertEqual(session.document.clips.count, 1)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// KRI-125: the add-clip sheet closes as soon as a file is chosen, so the session — not the
    /// sheet — must report "adding" for the whole time, including while the file is still being
    /// fetched out of Photos (seconds for an iCloud asset). It also has to finish with no view attached.
    func testAddClipReportsAddingWhileTheFileIsStillBeingFetched() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: jobID.uuidString, clipIndex: 1, kind: "video")
        let latch = FetchLatch()

        let adding = Task { @MainActor in await session.addClip { await latch.wait(); return tempURL } }
        while !latch.isWaiting { await Task.yield() }

        XCTAssertTrue(session.isAddingClip, "the editor must show progress while Photos is still handing the file over")
        XCTAssertTrue(fake.uploadFileCalls.isEmpty, "nothing uploads until the file exists")

        latch.open()
        await adding.value

        XCTAssertFalse(session.isAddingClip)
        XCTAssertNil(session.addClipError)
        XCTAssertEqual(session.document.clips.count, 2)
    }

    /// iOS terminates an app whose background-task expiration handler does not end the task. With the
    /// sheet gone the user backgrounds the app mid-upload, so a handler that only "reports" would kill
    /// it and lose the editor's unsaved timeline. The handler must end the assertion, exactly once.
    func testAddClipsExpirationHandlerEndsTheBackgroundAssertionExactlyOnce() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let activity = RecordingEditorActivity()
        session.backgroundActivity = activity
        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: jobID.uuidString, clipIndex: 1, kind: "video")
        let latch = FetchLatch()

        let adding = Task { @MainActor in await session.addClip { await latch.wait(); return tempURL } }
        while !latch.isWaiting { await Task.yield() }
        XCTAssertEqual(activity.beginCount, 1)
        XCTAssertEqual(activity.endCount, 0)

        activity.expire()
        XCTAssertEqual(activity.endCount, 1, "the handler must end the assertion or iOS terminates the app")

        latch.open()
        await adding.value
        XCTAssertEqual(activity.endCount, 1, "the normal exit must not end it a second time")
    }

    func testAddClipBalancesItsBackgroundAssertionOnEveryExit() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let activity = RecordingEditorActivity()
        session.backgroundActivity = activity

        await session.addClip { throw AddClipSourceUnreadable() }   // fails before any upload

        XCTAssertEqual(activity.beginCount, 1)
        XCTAssertEqual(activity.endCount, 1)
    }

    /// The sheet is dismissed before `addClip` runs, so a silent early return would drop the user's pick
    /// with no signal at all.
    func testAddClipReportsWhyItCannotProceedInsteadOfSilentlyDroppingThePick() async throws {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        // Never loaded: no API, no job. `canEditTimeline` is false.
        await session.addClip { URL(fileURLWithPath: "/nonexistent.mp4") }

        XCTAssertNotNil(session.addClipError, "a dropped pick must say so")
        XCTAssertFalse(session.isAddingClip)
    }

    func testAddClipExplainsAnUnreadablePhotoWithoutTouchingTheTimeline() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)

        await session.addClip { throw AddClipSourceUnreadable() }

        XCTAssertEqual(session.addClipError, "This file couldn’t be read. Try Files or choose it again.")
        XCTAssertFalse(session.isAddingClip)
        XCTAssertTrue(fake.uploadFileCalls.isEmpty)
        XCTAssertEqual(session.document.clips.count, 1)
    }

    /// The sheet is gone by the time the upload runs, so the user will background the app. When the
    /// connection then drops, "This file couldn't be added" plus a raw URLError is no help.
    func testAddClipExplainsAnInterruptedUpload() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.uploadFileError = URLError(.networkConnectionLost)

        await session.addClip(fileURL: tempURL)

        XCTAssertEqual(session.addClipError, "The upload was interrupted. Your edit is unchanged. Keep Kria open while it uploads and try again.")
        XCTAssertTrue(fake.addClipCalls.isEmpty, "a clip that never finished uploading must not be minted into the pool")
        XCTAssertEqual(session.document.clips.count, 1)
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

    func testPhoneSaveReconcilesAppOwnedRendererWithCreationProjectIdentity() async {
        let threadID = UUID(), jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        fake.phoneDestination = true
        let renderSessions = DeviceRenderSessions(fetch: { job, variant in
            XCTAssertEqual(job, jobID)
            XCTAssertEqual(variant, "variant")
            fake.deviceFetchCount += 1
            throw APIError.requestFailed(status: 503)
        }, factory: { _, _ in throw APIError.unsupported })
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 4))
        session.useDeviceRendering(renderSessions)
        await session.load(api: fake, threadID: threadID)
        session.selectClip(session.draft.clips[0].id)
        session.trimSelected(edge: .trailing, to: 1.5)
        await session.save()
        let key = DeviceRenderKey(projectID: threadID, jobID: jobID, variantID: "variant")
        XCTAssertEqual(session.deviceRenderKey, key)
        XCTAssertEqual(fake.deviceFetchCount, 1)
        XCTAssertEqual(fake.lastRequest?.guidedRevisionNumber, 7)
        XCTAssertEqual(renderSessions.presentations[key]?.phase, .needsAttention)
        XCTAssertEqual(session.saveState, .previewPending)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testDevicePublishedGenerationAndIdentityFenceReadyPreview() async throws {
        let threadID = UUID(), jobID = UUID()
        let request = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a")
        let statusBox = SessionTestStatus(DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "published-g2"))
        let output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let renderSessions = DeviceRenderSessions(fetch: { _, _ in await statusBox.response }, factory: { _, request in
            try DeviceRenderCoordinator(directory: output, exporter: SessionTestExport(), sources: SessionTestSources(), publisher: SessionTestPublisher())
        })
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["editor_capabilities"] = .object(["timeline": .bool(true), "text_elements": .bool(true)])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative,
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.useDeviceRendering(renderSessions)
        await session.load(api: fake, threadID: threadID)
        session.selectClip(session.draft.clips[0].id)
        session.trimSelected(edge: .trailing, to: 1.5)
        await session.save()

        let key = DeviceRenderKey(projectID: threadID, jobID: jobID, variantID: "variant")
        XCTAssertEqual(renderSessions.presentations[key]?.publishedGeneration, "published-g2")
        await statusBox.set(DeviceRenderStatusResponse(phase: "published", request: deviceRenderRequest(jobID: jobID, revision: 2, digest: "b"), publishedGeneration: "published-g2"))
        await renderSessions.reconcile(key, capabilities: .disabled)
        XCTAssertFalse(session.applyPreviewVariant(["render_generation_id": .string("published-g2"), "render_status": .string("ready"), "output_url": .string("https://storage.example/g2.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .previewPending)
        await statusBox.set(DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "published-g2"))
        await renderSessions.reconcile(key, capabilities: .disabled)
        session.trimSelected(edge: .trailing, to: 1.25)
        XCTAssertTrue(session.applyPreviewVariant(["render_generation_id": .string("published-g2"), "render_status": .string("ready"), "output_url": .string("https://storage.example/g2.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .saved)
        XCTAssertEqual(session.document.revision.baseGeneration, "published-g2")
        XCTAssertTrue(session.hasUnsavedChanges)

        // A subsequent save must use the published device generation.
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "published-g2")
        XCTAssertEqual(session.saveState, .previewPending)
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

    func testPreviewPollIgnoresMismatchedGenerationAndAcceptsMatchingReadyGeneration() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: textID, content: "Submitted")
        await session.save()

        XCTAssertFalse(session.applyPreviewVariant(["render_generation_id": .string("old"), "render_status": .string("ready"), "output_url": .string("https://storage.example/old.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .previewPending)
        session.updateTextContent(id: textID, content: "Follow-up")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.applyPreviewVariant(["render_generation_id": .string("g2"), "render_status": .string("ready"), "output_url": .string("https://storage.example/g2.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .saved)
        XCTAssertTrue(session.hasUnsavedChanges, "A follow-up edit must survive installation of the ready render")
    }

    func testFailedPreviewPollKeepsAcknowledgedSectionsRetryable() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: textID, content: "Submitted")
        await session.save()

        XCTAssertTrue(session.applyPreviewVariant(["render_generation_id": .string("g2"), "render_status": .string("failed")], generation: "g2"))
        XCTAssertEqual(session.saveState, .renderRetryNeeded("Your edit is saved, but its preview render failed. You can retry it safely."))

        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(session.saveState, .previewPending)
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
            authoritativeVariant: latestVariant.merging(["render_generation_id": .string("g1")]) { _, new in new },
            commitError: .conflict
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: textID, content: "My local edit")

        await session.save()
        XCTAssertEqual(session.saveState, .conflict)

        fake.authoritativeVariant = latestVariant
        let sourceRefresh = expectation(description: "Conflict rebase resolves new generation sources")
        fake.sourcePoolExpectation = sourceRefresh
        await session.rebaseAfterConflict()
        await fulfillment(of: [sourceRefresh], timeout: 3)
        XCTAssertEqual(fake.sourcePoolCallCount, 2)

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

    // KRI-110: guided-story captions are caption_cue-tagged TextElements, not
    // caption_cues rows. canEditCaptions must open for them via the
    // capability the guided revision actually advertises, without extending
    // the narrated/subtitled archetype allowlist.
    func testGuidedStoryCaptionTaggedTextElementsEnableCaptionEditing() async {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
                "text_elements": .array([captionElement]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        XCTAssertEqual(session.document.captionUnits.map(\.id), ["narration-caption-1"])
    }

    func testGuidedStoryWithoutCaptionTaggedTextElementsLeavesCaptionsClosed() async {
        let threadID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertFalse(session.canEditCaptions)
    }

    func testGuidedStoryCaptionEditRoutesThroughTextElementsNotCaptionCues() async {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
                "text_elements": .array([captionElement]),
            ],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        XCTAssertTrue(session.isDirty(.text))
        XCTAssertFalse(session.isDirty(.captions))
        // Timing is pinned server-side for guided captions — a timing-only
        // call with no text must be a no-op, not silently accepted.
        session.updateCaptionCue(id: "narration-caption-1", startS: 5)
        XCTAssertEqual(session.document.textElements.first(where: { $0.id == "narration-caption-1" })?.startS, 0)
        await session.save()
        XCTAssertNotNil(fake.lastRequest?.textElements)
        XCTAssertNil(fake.lastRequest?.captionCues)
    }

    // KRI-110: the render compiler now overlays caption_meta onto
    // caption_cue-tagged text elements' raw fields (applyingCaptionMeta),
    // so appearance is open regardless of render destination — cloud or
    // phone-pilot, the caption's style now actually reaches the burn.
    func testCaptionAppearanceOpenForTextLaneCaptionsRegardlessOfRenderDestination() async {
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        for renderDestination: JSONValue? in [nil, .string("device")] {
            let threadID = UUID()
            var variant: [String: JSONValue] = [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true), "caption_editor_style": .bool(true)]),
                "text_elements": .array([captionElement]),
            ]
            if let renderDestination { variant["render_destination"] = renderDestination }
            let fake = EditorCommitSpy(
                draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
                authoritativeVariant: variant
            )
            let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
            await session.load(api: fake, threadID: threadID)
            XCTAssertTrue(session.canEditCaptions)
            XCTAssertTrue(session.canEditCaptionAppearance)
        }
    }

    // KRI-110: a variant that never carries the narrated-only
    // `captions_enabled` field (guided_story never does) must not load as
    // captions-off. The backend treats missing as enabled; deriving `false`
    // here manufactured an explicit `caption_meta.enabled == false` that the
    // render compiler honors by hiding every caption in the on-device preview.
    func testVariantWithoutCaptionsEnabledFieldLoadsAsCaptionsOn() async {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        for captionsEnabled: JSONValue? in [nil, .null] {
            var variant: [String: JSONValue] = [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
                "text_elements": .array([captionElement]),
            ]
            if let captionsEnabled { variant["captions_enabled"] = captionsEnabled }
            let fake = EditorCommitSpy(
                draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
                authoritativeVariant: variant
            )
            let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
            await session.load(api: fake, threadID: threadID)
            XCTAssertNotEqual(session.document.captionMeta["enabled"], .bool(false), "captions_enabled=\(String(describing: captionsEnabled)) must not manufacture an explicit captions-off")
            XCTAssertTrue(session.draft.captions.enabled)
            XCTAssertFalse(session.hasUnsavedChanges)
        }
        // An explicit server-side off still loads as off.
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("subtitled"), "base_video_path": .string("base.mp4"), "captions_enabled": .bool(false)]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: true, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertEqual(session.document.captionMeta["enabled"], .bool(false))
        XCTAssertFalse(session.draft.captions.enabled)
    }

    // KRI-110: editing a guided-story caption's text must reach the video
    // the canvas is actually showing, through the same live text update any
    // ordinary text edit takes. Uses the exact element shape guided_story.py
    // persists (word_timings, static effect, custom position, pinned
    // source_params).
    func testGuidedStoryCaptionTextEditReachesTheDisplayedSourcePreview() async throws {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(1.5), "role": .string("generative_sequence"),
            "position": .string("custom"), "x_frac": .number(0.5), "y_frac": .number(0.82),
            "font_family": .string("Inter-Bold"), "size_px": .number(58), "color": .string("#FFFFFF"),
            "highlight_color": .string("#FFFFFF"), "stroke_width": .number(6), "shadow_enabled": .bool(true),
            "effect": .string("static"), "alignment": .string("center"), "max_width_frac": .number(0.84),
            "word_timings": .array([
                .object(["text": .string("Spoken"), "start_s": .number(0), "end_s": .number(0.7)]),
                .object(["text": .string("words"), "start_s": .number(0.7), "end_s": .number(1.5)]),
            ]),
            "source_params": .object(["source": .string("caption_cue"), "key": .string("0"), "identity": .string("pinned-narration-caption-0")]),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "render_generation_id": .string("g1"),
                "editor_capabilities": .object(["text_elements": .bool(true), "caption_editor_style": .bool(true)]),
                "text_elements": .array([captionElement]),
                "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertTrue(session.hasSourcePreview)
        let before = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        XCTAssertEqual(before.textLayers.map { $0.runs.map(\.text).joined(separator: " ") }, ["Spoken words"])

        session.beginTransaction()
        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        session.endTransaction()
        XCTAssertEqual(session.document.captionUnits.first?.text, "Edited words")

        for _ in 0..<200 {
            if session.displayedSourcePreviewRecipe?.textLayers.first?.runs.map(\.text).joined(separator: " ") == "Edited words" { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertEqual(session.sourcePreviewState, .ready, "A caption text edit must never fail the source preview over to the stale finished render")
        let after = try XCTUnwrap(session.displayedSourcePreviewRecipe, "The canvas must still be showing the editable source composition after a caption edit")
        XCTAssertEqual(after.textLayers.map { $0.runs.map(\.text).joined(separator: " ") }, ["Edited words"])
    }

    /// A loaded guided-story session with one caption element and, when
    /// `finishedRender` is given, a completed cloud render installed as the
    /// finished-render fallback — the state a real production video opens in.
    private func loadGuidedStorySession(finishedRender: URL? = nil) async -> NativeEditorSession {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(1.5), "role": .string("generative_sequence"),
            "position": .string("custom"), "x_frac": .number(0.5), "y_frac": .number(0.82),
            "font_family": .string("Inter-Bold"), "size_px": .number(58), "color": .string("#FFFFFF"),
            "effect": .string("static"), "alignment": .string("center"), "max_width_frac": .number(0.84),
            "source_params": .object(["source": .string("caption_cue"), "key": .string("0"), "identity": .string("pinned-narration-caption-0")]),
        ])
        var variant: [String: JSONValue] = [
            "resolved_archetype": .string("guided_story"),
            "render_generation_id": .string("g1"),
            "render_status": .string("ready"),
            "editor_capabilities": .object(["text_elements": .bool(true), "caption_editor_style": .bool(true)]),
            "text_elements": .array([captionElement]),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])]),
        ]
        if let finishedRender { variant["output_url"] = .string(finishedRender.absoluteString) }
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        return session
    }

    private func displayedCaptionTexts(_ session: NativeEditorSession) -> [String]? {
        session.displayedSourcePreviewRecipe?.textLayers.map { $0.runs.map(\.text).joined(separator: " ") }
    }

    private func waitForDisplayedCaptionTexts(_ session: NativeEditorSession, _ expected: [String]) async throws {
        for _ in 0..<200 where displayedCaptionTexts(session) != expected {
            try await Task.sleep(for: .milliseconds(25))
        }
    }

    // KRI-110: retyping a caption passes through the empty string. That is
    // nothing to draw, not a broken edit — the live composition must stay on
    // the canvas rather than fail over to the finished cloud render.
    func testCaptionClearedMidEditKeepsTheLiveCompositionOnTheCanvas() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = await loadGuidedStorySession(finishedRender: sourceURL)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        try await waitForDisplayedCaptionTexts(session, ["Spoken words"])

        session.beginTransaction()
        session.updateCaptionCue(id: "narration-caption-1", text: "")
        try await waitForDisplayedCaptionTexts(session, [])
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertFalse(session.isShowingRenderedFallback)
        XCTAssertEqual(displayedCaptionTexts(session), [], "An empty caption draws nothing; it must not take the composition down")

        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        session.endTransaction()
        try await waitForDisplayedCaptionTexts(session, ["Edited words"])
        XCTAssertEqual(displayedCaptionTexts(session), ["Edited words"])
    }

    // KRI-110: a compile failure hands the canvas to the finished render as a
    // fallback. When the very next edit compiles again, the recovered
    // composition must take the canvas back — otherwise the user keeps
    // watching the stale cloud render while every edit lands off-screen.
    func testRecoveredCompositionTakesTheCanvasBackFromTheFinishedRenderFallback() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = await loadGuidedStorySession(finishedRender: sourceURL)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        try await waitForDisplayedCaptionTexts(session, ["Spoken words"])

        // Over the layout's 500-character ceiling: the one text-element input
        // that still hard-fails a compile.
        session.updateCaptionCue(id: "narration-caption-1", text: String(repeating: "x", count: 600))
        for _ in 0..<200 where !session.isShowingRenderedFallback {
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertTrue(session.isShowingRenderedFallback, "precondition: the failure fell back to the finished render")
        XCTAssertNil(session.displayedSourcePreviewRecipe)

        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        try await waitForDisplayedCaptionTexts(session, ["Edited words"])
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertFalse(session.isShowingRenderedFallback)
        XCTAssertEqual(displayedCaptionTexts(session), ["Edited words"], "The canvas must show the recovered composition, not the stale render")
    }

    // KRI-110: a slider drag delivers many samples per second; each one used
    // to recompile and repaint the entire composition (~170 caption layouts
    // on a real guided story). Samples inside one transaction coalesce into
    // a single rebuild once the finger pauses, and the result is the final
    // value.
    func testGestureSamplesInsideOneTransactionCoalesceIntoOneRebuild() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = await loadGuidedStorySession()
        await session.prepareFixtureSourcePreview(url: sourceURL)
        try await waitForDisplayedCaptionTexts(session, ["Spoken words"])
        let compiles = session.sourcePreviewCompileCount

        session.beginTransaction()
        for size in stride(from: 60, through: 74, by: 2) {
            session.setCaptionSize(Double(size))
            try await Task.sleep(for: .milliseconds(10))
        }
        session.endTransaction()
        try await Task.sleep(for: .milliseconds(NativeEditorSession.previewCoalesceMilliseconds * 4))
        for _ in 0..<100 where session.sourcePreviewCompileCount == compiles {
            try await Task.sleep(for: .milliseconds(25))
        }

        XCTAssertEqual(session.sourcePreviewCompileCount, compiles + 1, "Eight samples inside one drag must produce one rebuild")
        let run = try XCTUnwrap(session.displayedSourcePreviewRecipe?.textLayers.first?.runs.first)
        XCTAssertEqual(run.fontSize, 74)
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

    private static func devicePlacementSession() async -> (NativeEditorSession, BackgroundUploadCoordinator, EditorSourceRegistrationTarget) {
        let threadID = UUID()
        var authoritative = variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["editor_revision_number"] = .number(7)
        authoritative["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true),
            "phone_editor_media": .object(["enabled": .bool(true), "source_registration": .bool(true),
                "visual_kinds": .array([.string("image")])]),
        ])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant",
            draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1",
            snapshot: [:], canUndo: false, createdAt: .now), authoritativeVariant: authoritative)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let uploads = BackgroundUploadCoordinator(api: fake, defaultsKey: "placement-\(UUID().uuidString)", sessionConfiguration: .ephemeral)
        session.useMediaUploads(uploads)
        return (session, uploads, .init(itemID: "item", variantID: "variant", clientImportID: UUID(),
            baseGeneration: "generation-1", guidedRevisionNumber: 7, sourceKind: .footage))
    }

    /// Loads one clip slot (plus `extras`) under the server's per-clip
    /// `clips.*` crop, speed and look capabilities.
    private static func footageSession(destination: String, operationsEditable: Bool, slot extras: [String: JSONValue] = [:]) async -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID()
        var authoritative = variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string(destination)
        let operation: JSONValue = .object(["editable": .bool(operationsEditable)])
        authoritative["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
            "clips": .object(["source_crop": operation, "playback_rate": operation, "looks": operation]),
        ])
        var slot: [String: JSONValue] = ["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(4), "removed": .bool(false)]
        slot.merge(extras) { _, extra in extra }
        authoritative["user_timeline"] = .object(["slots": .array([.object(slot)])])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 4))
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }
}

private func deviceRenderRequest(jobID: UUID, revision: Int, digest: String) -> DeviceRenderRequest {
    DeviceRenderRequest(
        identity: DeviceRenderIdentity(jobID: jobID, variantID: "variant", recipeRevision: revision, recipeDigest: String(repeating: digest, count: 64)),
        recipe: KriaMediaEngine.EditRecipe(
            assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 2)])]
        )
    )
}

private actor SessionTestExport: LocalExporting {
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}

private struct SessionTestSources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}

private actor SessionTestPublisher: DeviceRenderPublishing {
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}

private actor SessionTestStatus {
    private(set) var response: DeviceRenderStatusResponse
    init(_ response: DeviceRenderStatusResponse) { self.response = response }
    func set(_ response: DeviceRenderStatusResponse) { self.response = response }
}

final class EditorCommitSpy: KriaAPIClient, @unchecked Sendable {
    let draftSnapshot: DraftSnapshot
    var draftError: APIError?
    let openReceipt: OpenInEditorResponse?
    var authoritativeVariant: [String: JSONValue]?
    let editorVariantError: APIError?
    var commitResponse: EditorCommitResponse?
    let commitError: APIError?
    var refreshedThread: CreationThread?
    private(set) var commitIsSuspended = false
    private var commitContinuation: CheckedContinuation<Void, Never>?
    private var commitResumeRequested = false
    private var suspendNextCommit: Bool
    var suspendNextSourcePool = false
    private(set) var sourcePoolIsSuspended = false
    private var sourcePoolContinuation: CheckedContinuation<Void, Never>?
    private var sourcePoolResumeRequested = false
    var phoneDestination = false
    var deviceFetchCount = 0
    var commitCount = 0
    var lastRequest: EditorCommitRequest?
    var openedJobID: UUID?
    var lastItemID: String?
    var draftCallCount = 0
    var lastVariantID: String?
    var projectCallCount = 0
    var editorVariantsCallCount = 0
    var sourcePoolCallCount = 0
    var sourcePoolExpectation: XCTestExpectation?
    var reserveUploadResult: UploadReservation?
    /// Makes the PUT itself fail (after a successful reservation), e.g. a dropped connection.
    var uploadFileError: (any Error)?
    var addClipResult: AddClipResult?
    var addClipCalls: [(jobID: UUID, gcsPath: String)] = []
    var uploadFileCalls: [(reservation: UploadReservation, fileURL: URL)] = []
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
    func reviewerSignIn(email: String, password: String) async throws -> MobileSession { throw APIError.unsupported }
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
        return authoritativeVariant ?? ["editor_revision_number": phoneDestination ? .number(7) : .null, "render_destination": .string(phoneDestination ? "device" : "cloud"), "variant_id": .string(variantID), "render_generation_id": .string("generation-1"), "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false)]), "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])])]
    }
    func editorSourcePool(jobID: UUID, variantID: String) async throws -> NativeEditorSourcePool {
        sourcePoolCallCount += 1
        sourcePoolExpectation?.fulfill()
        sourcePoolExpectation = nil
        if suspendNextSourcePool {
            suspendNextSourcePool = false
            sourcePoolIsSuspended = true
            await withCheckedContinuation { continuation in
                if sourcePoolResumeRequested {
                    sourcePoolResumeRequested = false
                    continuation.resume()
                } else {
                    sourcePoolContinuation = continuation
                }
            }
            sourcePoolIsSuspended = false
        }
        throw APIError.unsupported
    }
    func resumeSourcePool() {
        if let sourcePoolContinuation {
            sourcePoolContinuation.resume()
            self.sourcePoolContinuation = nil
        } else {
            sourcePoolResumeRequested = true
        }
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
    // Qualified: this file now imports KriaMediaEngine, which has its own
    // `EditRecipe` (the render recipe). The API client returns Kria's DTO.
    func editRecipe(jobID: UUID, variantID: String?) async throws -> Kria.EditRecipe { throw APIError.unsupported }
    func reserveUpload(filename: String, contentType: String, size: Int64, purpose: UploadPurpose?) async throws -> UploadReservation {
        guard let reserveUploadResult else { throw APIError.unsupported }
        return reserveUploadResult
    }
    func cancelUpload(reservationID: UUID) async throws { throw APIError.unsupported }
    func reserveProjectUpload(threadID: UUID, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> ProjectUploadReservation { throw APIError.unsupported }
    func attachProjectMedia(threadID: UUID, mediaID: String, gcsPath: String, filename: String, contentType: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread { throw APIError.unsupported }
    func addClip(jobID: UUID, gcsPath: String) async throws -> AddClipResult {
        addClipCalls.append((jobID, gcsPath))
        guard let addClipResult else { throw APIError.unsupported }
        return addClipResult
    }
    func uploadFile(to reservation: UploadReservation, fileURL: URL) async throws {
        uploadFileCalls.append((reservation, fileURL))
        if let uploadFileError { throw uploadFileError }
    }
}

private final class DelayedSeekPlayer: AVPlayer, @unchecked Sendable {
    var targets: [Double] = []
    private var completions: [@Sendable (Bool) -> Void] = []
    private var seekExpectation: (target: Double?, expectation: XCTestExpectation)?

    func expectSeek(to target: Double) -> XCTestExpectation {
        let expectation = XCTestExpectation(description: "Seek to \(target)")
        seekExpectation = (target, expectation)
        return expectation
    }

    func expectNoSeek() -> XCTestExpectation {
        let expectation = XCTestExpectation(description: "Completed seek must not retry")
        expectation.isInverted = true
        seekExpectation = (nil, expectation)
        return expectation
    }
    override func seek(to time: CMTime, toleranceBefore: CMTime, toleranceAfter: CMTime,
                       completionHandler: @escaping @Sendable (Bool) -> Void) {
        targets.append(time.seconds)
        completions.append(completionHandler)
        if let pending = seekExpectation,
           pending.target == nil || abs(pending.target! - time.seconds) < 0.001 {
            seekExpectation = nil
            pending.expectation.fulfill()
        }
    }
    func completeSeek(finished: Bool = true) {
        guard !completions.isEmpty else { return }
        completions.removeFirst()(finished)
    }
}

/// Records background-task begin/end and lets a test fire the expiration handler.
@MainActor private final class RecordingEditorActivity: BackgroundActivityAssertion, @unchecked Sendable {
    private(set) var beginCount = 0
    private(set) var endCount = 0
    private var handler: (@Sendable () -> Void)?
    func begin(name: String, expirationHandler: @escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier {
        beginCount += 1
        handler = expirationHandler
        return UIBackgroundTaskIdentifier(rawValue: 7)
    }
    func end(_ identifier: UIBackgroundTaskIdentifier) { endCount += 1 }
    func expire() { handler?() }
}

/// Holds a simulated Photos export open so a test can observe the session while the file is still
/// being fetched, then lets it finish.
@MainActor private final class FetchLatch {
    private var continuation: CheckedContinuation<Void, Never>?
    private var opened = false
    var isWaiting: Bool { continuation != nil }

    func wait() async {
        if opened { return }
        await withCheckedContinuation { continuation = $0 }
    }

    func open() {
        opened = true
        continuation?.resume()
        continuation = nil
    }
}
