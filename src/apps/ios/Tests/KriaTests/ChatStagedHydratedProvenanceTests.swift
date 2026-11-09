import AVFoundation
import KriaMediaEngine
import XCTest
@testable import Kria

/// A chat edit staged on a project whose preview hydrates its clips (server state, not an edit) must stay
/// the AI's work: only the sections the chat changed are dirty, so no conflict banner and chat provenance.
@MainActor
final class ChatStagedHydratedProvenanceTests: XCTestCase {

    private enum Kind: String, CaseIterable {
        case talking, phoneTalking, narrated, phoneNarrated, voiceover
    }

    private static let textRow: JSONValue = .object(["id": .string("note"), "text": .string("from the chat"), "start_s": .number(0),
        "end_s": .number(0.8), "role": .string("generative_intro"), "position": .string("custom"),
        "x_frac": .number(0.5), "y_frac": .number(0.15), "size_px": .number(120), "size_class": .string("large"),
        "font_family": .string("Playfair Display"), "alignment": .string("center"), "effect": .string("fade-in")])

    private static func chatTextDraft(base: String = "generation-1", jobID: String?, revision: Int = 3) -> DraftSnapshot {
        let text: JSONValue = .array([textRow])
        return DraftSnapshot(draftID: "d\(revision)", itemID: "item", variantKey: "variant", draftRevision: revision,
            snapshotHash: "h", etag: "e", baseJobID: jobID, baseGenerationID: base,
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "edit_format": .string("montage"),
                       "editor_payload": .object(["base_generation": .string(base), "text_elements": text,
                                                  "sections": .object(["text_elements": text])])],
            canUndo: true, createdAt: .now)
    }

    private static func request(jobID: UUID) -> DeviceRenderRequest {
        DeviceRenderRequest(
            identity: DeviceRenderIdentity(jobID: jobID, variantID: "variant", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(
                assets: [MediaAsset(id: "source", relativePath: "source")],
                tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 2)])]))
    }

    /// Opens a hydrated project. `stagedAtOpen` puts the chat edit in the draft head before the editor loads.
    private static func project(_ kind: Kind, stagedAtOpen: Bool = false) async throws -> (NativeEditorSession, EditorCommitSpy, UUID) {
        let threadID = UUID(), jobID = UUID()
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        let montage = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        var variant: [String: JSONValue] = [
            "variant_id": .string("variant"),
            "render_generation_id": .string("generation-1"),
            "render_status": .string("ready"),
            "output_url": .string("file:///tmp/kria-chat-provenance.mp4"),
        ]
        var capabilities: [String: JSONValue] = ["timeline": .bool(false), "text_elements": .bool(true), "mix": .bool(false)]
        var poolClips: [NativeEditorSourcePool.Clip] = []
        var device = true
        switch kind {
        case .talking:
            device = false
            variant["resolved_archetype"] = .string("talking_head")
            variant["base_video_path"] = .string("base.mp4")
            variant["base_video_url"] = .string("https://storage.example/base.mp4")
            let cache = NativePreviewAssetCache(project: project, jobID: jobID)
            let remote = try XCTUnwrap(URL(string: "https://storage.example/base.mp4"))
            let download = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
            try FileManager.default.copyItem(at: montage, to: download)
            var base = try await NativeDownloadedMedia.importAsset(from: download, sourceURL: remote,
                response: URLResponse(url: remote, mimeType: "video/mp4", expectedContentLength: -1, textEncodingName: nil), project: cache.project)
            let media = AVURLAsset(url: cache.project.root.appendingPathComponent(base.relativePath))
            let tracks = try await media.loadTracks(withMediaType: .video)
            let track = try XCTUnwrap(tracks.first)
            let size = try await track.load(.naturalSize)
            base.naturalSize = MediaSize(width: abs(size.width), height: abs(size.height))
            base.duration = try await media.load(.duration).seconds
            try cache.store(base, for: "media:generation-1:variant-base-generation-1")
        case .phoneTalking, .narrated, .phoneNarrated, .voiceover:
            let input = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
            try FileManager.default.copyItem(at: montage, to: input)
            defer { try? FileManager.default.removeItem(at: input) }
            let asset = try await AssetImportCoordinator(project: project).importAsset(from: input)
            try SourceAssetStore(project: project).bind(mediaID: "source", original: asset)
            let fingerprint = try XCTUnwrap(asset.fingerprint)
            poolClips = [.init(clipIndex: 0, nativeSource: .init(mediaID: "source", sourceURL: nil,
                original: OriginalMediaDescriptor(sha256: fingerprint.hex, byteCount: fingerprint.byteCount, durationS: 1,
                    width: 1080, height: 1920, orientationDegrees: 0, hasAudio: true), localRequired: true))]
            switch kind {
            case .phoneTalking:
                variant["resolved_archetype"] = .string("subtitled")
                capabilities = ["timeline": .bool(false), "text_elements": .bool(true), "mix": .bool(false),
                                "overlays": .bool(true), "sfx": .bool(true)]
                variant["caption_cues"] = .array([.object(["text": .string("Number three"), "start_s": .number(0.2), "end_s": .number(0.9)])])
            case .narrated:
                variant["resolved_archetype"] = .string("narrated")
                capabilities["timeline"] = .bool(true)
                variant["user_timeline"] = .object(["slots": .array([.object(["slot_id": .string("s1"), "clip_index": .number(0),
                    "in_s": .number(0), "duration_s": .number(0.9), "source_duration_s": .number(1), "removed": .bool(false)])])])
            case .phoneNarrated: variant["resolved_archetype"] = .string("narrated")
            default: variant["resolved_archetype"] = .string("voiceover")
            }
        }
        if device { variant["render_destination"] = .string("device") }
        variant["editor_capabilities"] = .object(capabilities)
        let opening = stagedAtOpen
            ? chatTextDraft(jobID: jobID.uuidString)
            : DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e",
                baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now)
        let fake = EditorCommitSpy(draftSnapshot: opening, authoritativeVariant: variant)
        fake.sourcePoolResult = NativeEditorSourcePool(clips: poolClips, baseGeneration: "generation-1")
        fake.deviceRenderResponse = DeviceRenderStatusResponse(phase: "published", request: request(jobID: jobID), publishedGeneration: "generation-1")
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        return (session, fake, jobID)
    }

    private func assertChatOnlyText(_ session: NativeEditorSession, _ context: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(session.document.textElements.map(\.text), ["from the chat"], context, file: file, line: line)
        XCTAssertEqual(session.dirtySections, [.text], "\(context): hydration must not mark another section changed", file: file, line: line)
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "\(context): a chat edit is not the creator's own work", file: file, line: line)
    }

    /// The chat turn lands after the editor opened and hydrated.
    func testChatTextEditArrivingAfterOpenStaysChatOnlyOnEveryHydratedProject() async throws {
        for kind in Kind.allCases {
            let (session, fake, jobID) = try await Self.project(kind)
            XCTAssertEqual(session.sourcePreviewState, .ready, "\(kind)")
            XCTAssertFalse(session.hasUnsavedChanges, "\(kind)")
            fake.draftSnapshot = Self.chatTextDraft(jobID: jobID.uuidString)
            await session.synchronizePromptRevision()
            assertChatOnlyText(session, "\(kind) arrived after open")
        }
    }

    /// The chat edit is already in the draft head when the editor opens; hydration runs after it is staged.
    func testChatTextEditPresentAtOpenStaysChatOnlyOnEveryHydratedProject() async throws {
        for kind in Kind.allCases {
            let (session, _, _) = try await Self.project(kind, stagedAtOpen: true)
            XCTAssertEqual(session.sourcePreviewState, .ready, "\(kind)")
            assertChatOnlyText(session, "\(kind) present at open")
        }
    }

    /// Failure mode: a creator edit to the timeline on a hydrated project must still count as a timeline change.
    func testCreatorTimelineEditOnAHydratedProjectStillMarksTimelineAndIsNotChatOnly() async throws {
        let (session, fake, jobID) = try await Self.project(.narrated)
        fake.draftSnapshot = Self.chatTextDraft(jobID: jobID.uuidString)
        await session.synchronizePromptRevision()
        assertChatOnlyText(session, "staged")
        session.setClipTiming(clipID: "s1", durationS: 0.5)
        XCTAssertTrue(session.dirtySections.contains(.timeline), "the creator's own timeline edit is a timeline change")
        XCTAssertTrue(session.dirtySections.contains(.text))
        XCTAssertFalse(session.hasOnlyChatStagedChanges, "the creator's own edit lifts the chat-only provenance")
    }

    func testCreatorTimelineEditAloneOnAHydratedProjectMarksTimeline() async throws {
        let (session, _, _) = try await Self.project(.narrated)
        XCTAssertFalse(session.hasUnsavedChanges)
        session.setClipTiming(clipID: "s1", durationS: 0.5)
        XCTAssertEqual(session.dirtySections, [.timeline])
        XCTAssertFalse(session.hasOnlyChatStagedChanges)
    }

    /// Failure mode: a chat edit that really changes the timeline is still reported as a timeline change.
    func testChatEditThatChangesTheTimelineIsStillReportedAsTimeline() async throws {
        let (session, fake, jobID) = try await Self.project(.narrated)
        let slots: JSONValue = .array([.object(["slot_id": .string("s1"), "clip_index": .number(0),
            "in_s": .number(0), "duration_s": .number(0.4), "source_duration_s": .number(1), "removed": .bool(false)])])
        let text: JSONValue = .array([Self.textRow])
        fake.draftSnapshot = DraftSnapshot(draftID: "d4", itemID: "item", variantKey: "variant", draftRevision: 4,
            snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
            snapshot: ["kind": .string("editor"), "schema_version": .number(2), "edit_format": .string("montage"),
                       "editor_payload": .object(["base_generation": .string("generation-1"), "text_elements": text,
                                                  "timeline": .object(["slots": slots]),
                                                  "sections": .object(["text_elements": text, "timeline": .object(["slots": slots])])])],
            canUndo: true, createdAt: .now)
        await session.synchronizePromptRevision()
        XCTAssertTrue(session.dirtySections.contains(.timeline), "the chat's timeline change is real: \(session.dirtySections)")
        XCTAssertTrue(session.dirtySections.contains(.text))
        XCTAssertTrue(session.hasOnlyChatStagedChanges, "still the AI's work")
    }
}
