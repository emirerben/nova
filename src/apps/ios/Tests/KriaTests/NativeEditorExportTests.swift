import XCTest
@testable import Kria

@MainActor
final class NativeEditorExportTests: XCTestCase {
    // #1017 rendered the disabled Save with the actionable glyph, so a clean
    // editor showed a greyed "download" icon that did nothing when tapped.
    func testCleanEditorShowsSavedStateInsteadOfAnActionableSave() {
        let saved = NativeEditorSaveControl(isSaving: false, hasUnsavedChanges: false)
        XCTAssertEqual(saved, .saved)
        XCTAssertFalse(saved.isEnabled)
        XCTAssertEqual(saved.accessibilityLabel, "Saved")

        let unsaved = NativeEditorSaveControl(isSaving: false, hasUnsavedChanges: true)
        XCTAssertEqual(unsaved, .unsaved)
        XCTAssertTrue(unsaved.isEnabled)
        XCTAssertEqual(unsaved.accessibilityLabel, "Save changes")

        let saving = NativeEditorSaveControl(isSaving: true, hasUnsavedChanges: true)
        XCTAssertEqual(saving, .saving)
        XCTAssertFalse(saving.isEnabled)
    }

    func testExportWaitsForSavedEditsAndFinishedRenders() {
        let session = NativeEditorSession(draft: Self.draft(), initialPlaybackURL: Self.missingRender)
        XCTAssertNil(session.exportBlockReason)

        session.addText(content: "Unsaved hook")
        XCTAssertEqual(session.exportBlockReason, "Save your changes to export them.")

        let clean = NativeEditorSession(draft: Self.draft(), initialPlaybackURL: Self.missingRender)
        let olderCutStates: [NativeEditorSaveState] = [
            .saving, .previewPending, .previewFailed("slow"), .renderRetryNeeded("queue"),
            .conflict, .refreshFailed("offline"), .loadFailed("offline"),
        ]
        for state in olderCutStates {
            clean.saveState = state
            XCTAssertNotNil(clean.exportBlockReason, "\(state) would export an older cut")
        }
        for state: NativeEditorSaveState in [.idle, .saved] {
            clean.saveState = state
            XCTAssertNil(clean.exportBlockReason, "\(state) has a finished render")
        }
    }

    func testFixtureExportUsesItsInitialRender() async throws {
        let session = NativeEditorSession(draft: Self.draft(), initialPlaybackURL: Self.missingRender)
        let resolved = try await session.exportRenderURL()
        XCTAssertEqual(resolved, Self.missingRender)

        let noRender = NativeEditorSession(draft: Self.draft())
        do {
            _ = try await noRender.exportRenderURL()
            XCTFail("A session without a render has nothing to export")
        } catch {
            XCTAssertEqual(error as? NativeEditorExportError, .unavailable)
        }
    }

    func testLoadedProjectExportsTheFreshlySignedReadyRender() async throws {
        let fake = Self.loadedSpy(variant: Self.variant(status: "ready", output: "file:///tmp/kria-export-signed-at-load.mp4"))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        XCTAssertEqual(session.loadState, .loaded)

        // Signed URLs expire and chat can rebase the document without
        // replacing the player, so export re-reads the loaded variant.
        fake.authoritativeVariant = Self.variant(status: "ready", output: "file:///tmp/kria-export-signed-at-export.mp4")
        let resolved = try await session.exportRenderURL()
        XCTAssertEqual(resolved.absoluteString, "file:///tmp/kria-export-signed-at-export.mp4")
        XCTAssertEqual(fake.lastVariantID, "variant")
    }

    func testLoadedProjectRefusesRenderingOrFailedVariants() async {
        let fake = Self.loadedSpy(variant: Self.variant(status: "ready", output: "file:///tmp/kria-export-old.mp4"))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())

        fake.authoritativeVariant = Self.variant(status: "rendering", output: "file:///tmp/kria-export-old.mp4")
        do {
            _ = try await session.exportRenderURL()
            XCTFail("A rendering variant still exposes the previous output")
        } catch {
            XCTAssertEqual(error as? NativeEditorExportError, .stillRendering)
        }

        fake.authoritativeVariant = Self.variant(status: "failed", output: nil)
        do {
            _ = try await session.exportRenderURL()
            XCTFail("A failed render has nothing to export")
        } catch {
            XCTAssertEqual(error as? NativeEditorExportError, .renderFailed)
        }
    }

    func testSaveToPhotosHandsPhotosAPrivateCopyAndRemovesIt() async throws {
        let render = try Self.renderFile()
        defer { try? FileManager.default.removeItem(at: render) }
        let photos = PhotoLibrarySpy()
        let exporter = NativeEditorExporter(photos: photos)
        let session = NativeEditorSession(draft: Self.draft(), initialPlaybackURL: render)

        await exporter.saveToPhotos(from: session)

        XCTAssertEqual(exporter.phase, .savedToPhotos)
        let saved = try XCTUnwrap(photos.saves.first)
        XCTAssertEqual(photos.saves.count, 1)
        XCTAssertNotEqual(saved.url, render, "Photos receives a private copy")
        XCTAssertEqual(saved.contents, Data("render".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: saved.url.path), "The copy is removed after saving")
        XCTAssertTrue(FileManager.default.fileExists(atPath: render.path), "The render itself is untouched")
    }

    func testPhotosFailureIsReportedAndTheCopyIsStillRemoved() async throws {
        let render = try Self.renderFile()
        defer { try? FileManager.default.removeItem(at: render) }
        let photos = PhotoLibrarySpy(error: PhotoLibraryError.notAuthorized)
        let exporter = NativeEditorExporter(photos: photos)
        let session = NativeEditorSession(draft: Self.draft(), initialPlaybackURL: render)

        await exporter.saveToPhotos(from: session)

        XCTAssertEqual(exporter.phase, .failed("Kria needs Photos access to save this video."))
        let attempted = try XCTUnwrap(photos.saves.first)
        XCTAssertFalse(FileManager.default.fileExists(atPath: attempted.url.path))
    }

    func testBlockedExportNeverDownloadsOrSaves() async {
        let fetches = FetchCounter()
        let photos = PhotoLibrarySpy()
        let exporter = NativeEditorExporter(
            downloader: RenderedVideoDownloader(fetch: { url in
                fetches.increment()
                throw URLError(.badServerResponse)
            }),
            photos: photos
        )
        // `.invalid` never resolves, so the counter alone proves no download ran.
        let session = NativeEditorSession(draft: Self.draft(), initialPlaybackURL: URL(string: "https://render.invalid/cut.mp4"))
        session.addText(content: "Unsaved hook")

        await exporter.saveToPhotos(from: session)
        await exporter.share(from: session)

        XCTAssertEqual(exporter.phase, .failed("Save your changes to export them."))
        XCTAssertEqual(fetches.count, 0)
        XCTAssertTrue(photos.saves.isEmpty)
        XCTAssertFalse(exporter.isSharing)
    }

    func testShareKeepsTheCopyUntilTheSheetIsDismissed() async throws {
        let render = try Self.renderFile()
        defer { try? FileManager.default.removeItem(at: render) }
        let exporter = NativeEditorExporter(photos: PhotoLibrarySpy())
        let session = NativeEditorSession(draft: Self.draft(), initialPlaybackURL: render)

        await exporter.share(from: session)

        XCTAssertTrue(exporter.isSharing)
        XCTAssertEqual(exporter.phase, .idle)
        let shared = try XCTUnwrap(exporter.sharedFile)
        XCTAssertNotEqual(shared, render)
        XCTAssertTrue(FileManager.default.fileExists(atPath: shared.path))
        exporter.removeSharedFile()
        XCTAssertNil(exporter.sharedFile)
        XCTAssertFalse(FileManager.default.fileExists(atPath: shared.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: render.path))
    }

    func testRemoteDownloadMovesAFinishedRenderIntoPlace() async throws {
        let downloader = RenderedVideoDownloader(fetch: { url in
            let temporary = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
            try Data("remote render".utf8).write(to: temporary)
            return (temporary, HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        let local = try await downloader.localCopy(of: URL(string: "https://render.test/cut.mp4")!)
        defer { try? FileManager.default.removeItem(at: local) }
        XCTAssertEqual(local.pathExtension, "mp4")
        XCTAssertEqual(try Data(contentsOf: local), Data("remote render".utf8))
    }

    func testRemoteDownloadRejectsAnExpiredSignedURLAndCleansUp() async {
        let leftover = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let downloader = RenderedVideoDownloader(fetch: { url in
            try Data("AccessDenied".utf8).write(to: leftover)
            return (leftover, HTTPURLResponse(url: url, statusCode: 403, httpVersion: nil, headerFields: nil)!)
        })
        do {
            _ = try await downloader.localCopy(of: URL(string: "https://render.test/expired.mp4")!)
            XCTFail("An error page must never be saved as a video")
        } catch {
            XCTAssertEqual(error as? APIError, .requestFailed)
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: leftover.path))
    }

    private static let missingRender = URL(fileURLWithPath: "/tmp/kria-export-fixture.mp4")

    private static func draft() -> EditorDraft {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 4)
        return EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
    }

    private static func renderFile() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appending(path: "render-\(UUID().uuidString).mp4")
        try Data("render".utf8).write(to: url)
        return url
    }

    private static func loadedSpy(variant: [String: JSONValue]) -> EditorCommitSpy {
        EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString,
                baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now
            ),
            authoritativeVariant: variant
        )
    }

    private static func variant(status: String, output: String?) -> [String: JSONValue] {
        var variant: [String: JSONValue] = [
            "variant_id": .string("variant"),
            "render_generation_id": .string("g1"),
            "render_status": .string(status),
            "duration_s": .number(4),
            "resolved_archetype": .string("narrated"),
            "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false)]),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(4), "source_duration_s": .number(4), "removed": .bool(false)])])]),
        ]
        if let output { variant["output_url"] = .string(output) }
        return variant
    }
}

private final class PhotoLibrarySpy: PhotoLibrarySaving, @unchecked Sendable {
    struct Save { let url: URL; let contents: Data? }
    private let lock = NSLock()
    private var recorded: [Save] = []
    private let error: Error?

    init(error: Error? = nil) { self.error = error }

    var saves: [Save] { lock.withLock { recorded } }

    func saveVideo(at url: URL) async throws {
        let save = Save(url: url, contents: try? Data(contentsOf: url))
        lock.withLock { recorded.append(save) }
        if let error { throw error }
    }
}

private final class FetchCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    var count: Int { lock.withLock { value } }
    func increment() { lock.withLock { value += 1 } }
}
