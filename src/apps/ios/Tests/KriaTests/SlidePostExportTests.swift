import XCTest
@testable import Kria

@MainActor final class SlidePostExportTests: XCTestCase {
    private final class StateBox: @unchecked Sendable {
        var value: SlidePostState
        init(_ value: SlidePostState) { self.value = value }
    }
    private let itemID = "11111111-1111-1111-1111-111111111111"
    private var defaults: UserDefaults!
    private var suite = ""

    override func setUp() { super.setUp(); suite = "SlidePostExportTests.\(UUID())"; defaults = UserDefaults(suiteName: suite) }
    override func tearDown() { NativeEditorURLProtocol.handler = nil; defaults.removePersistentDomain(forName: suite); super.tearDown() }

    private func state(version: Int = 1) -> SlidePostState {
        let refs = [SlidePostSlide(id: "one", assetID: "a", kind: "image"), SlidePostSlide(id: "two", assetID: "b", kind: "video")]
        let draft = SlidePostDraft(version: version, platformProfile: "instagram_carousel", slides: refs, caption: "Caption", renderedVersion: version)
        return SlidePostState(itemID: itemID, title: "Post", jobID: UUID(), draft: draft,
            assets: refs.map { .init(id: $0.assetID, kind: $0.kind, status: "ready") }, renderStatus: "ready", renderedVersion: version,
            slides: refs.map { .init(id: $0.id, assetID: $0.assetID, kind: $0.kind, url: URL(string: "https://test/\($0.id).\($0.kind == "video" ? "mp4" : "jpg")")) })
    }
    private func session(_ value: SlidePostState) async -> SlidePostSession {
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(value)) }
        let result = SlidePostSession(defaults: defaults)
        await result.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        return result
    }
    private func downloader(_ calls: @escaping (URL) -> Void, failSecond: Bool = false) -> SlidePostExporter.Download {
        var count = 0
        return { url in
            count += 1; calls(url)
            if failSecond && count == 2 { throw URLError(.cannotLoadFromNetwork) }
            let file = FileManager.default.temporaryDirectory.appending(path: "export-test-\(UUID()).bin")
            try Data(url.lastPathComponent.utf8).write(to: file)
            return (file, HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
    }

    func testShareKeepsOrderedMixedFilesAndCaption() async {
        let session = await session(state()); var requested: [URL] = []
        let exporter = SlidePostExporter(downloadFile: downloader { requested.append($0) }, authorizePhotos: { .authorized }, writePhotos: { _ in })
        await exporter.prepareShare(session: session, revalidate: {})
        XCTAssertEqual(requested.map(\.lastPathComponent), ["one.jpg", "two.mp4"])
        XCTAssertEqual(exporter.shareItems.count, 3)
        XCTAssertEqual(exporter.shareItems.last?.lastPathComponent, "caption.txt")
        XCTAssertEqual(try? String(contentsOf: exporter.shareItems.last!), "Caption")
        exporter.discardShareDirectory()
    }

    func testFailedSecondDownloadCleansUpAndNeverWritesPhotos() async {
        let session = await session(state()); var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader({ _ in }, failSecond: true), authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(writes, 0)
    }

    func testVersionChangeAfterDownloadBlocksPhotoCommit() async {
        let session = await session(state()); var writes = 0; var changed = false
        let exporter = SlidePostExporter(downloadFile: downloader { _ in
            if !changed { changed = true; session.draft?.caption = "new" }
        }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(writes, 0)
    }

    func testSavingTheSameVersionTwiceWritesPhotosTwice() async {
        let session = await session(state()); var downloads = 0; var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in downloads += 1 }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.saveToPhotos(session: session, revalidate: {})
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(writes, 2, "no receipt: a re-save always writes, like the video editor")
        XCTAssertEqual(downloads, 4)
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
    }

    func testFailedPhotosWriteSurfacesFailureAndRetryCanWrite() async {
        let session = await session(state()); var attempts = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in
            attempts += 1; if attempts == 1 { throw URLError(.cannotWriteToFile) }
        })
        await exporter.saveToPhotos(session: session, revalidate: {})
        guard case .failed = exporter.status else { return XCTFail("expected failure, got \(exporter.status)") }
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(attempts, 2)
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
    }

    func testLookPickerHidesRetiredPresets() {
        let ids = SlidePostLookPanel.looks.map(\.0)
        XCTAssertFalse(ids.contains("stadium_diffusion"))
        XCTAssertFalse(ids.contains("olive_film"))
        XCTAssertTrue(ids.contains("none"))
    }

    func testSecondRevalidationWithNewerServerDraftBlocksPhotoCommit() async {
        let box = StateBox(state())
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(box.value)) }
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        var checks = 0
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })

        await exporter.saveToPhotos(session: session, revalidate: {
            checks += 1
            if checks == 2 { box.value = self.state(version: 2) }
            await session.refresh(api: NativeEditorTestSupport.api(), itemID: self.itemID)
        })

        XCTAssertEqual(checks, 2)
        XCTAssertEqual(writes, 0)
    }

    // MARK: Photos order (creationDate) and export from any state

    func testPhotosAreWrittenWithAscendingCreationDatesInSlideOrder() async {
        let session = await session(state())
        let fixedNow = Date(timeIntervalSince1970: 1_800_000_000)
        var written: [SlidePostExporter.PhotoResource] = []
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized },
                                         writePhotos: { written = $0 }, now: { fixedNow })
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(written.map(\.url.lastPathComponent), ["01.jpg", "02.mp4"], "slide order is preserved")
        XCTAssertEqual(written.map(\.kind), ["image", "video"])
        let dates = written.map(\.creationDate)
        XCTAssertEqual(dates, dates.sorted(), "ascending creationDate makes Photos' date sort equal slide order")
        XCTAssertEqual(Set(dates).count, dates.count, "no two slides share a date")
        XCTAssertEqual(dates.last, fixedNow, "nothing is dated in the future")
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
        XCTAssertEqual(exporter.message, "Saved 2 slides to Photos.")
    }

    func testCreationDatesAreOneSecondApartAndEndAtNow() {
        let now = Date(timeIntervalSince1970: 1_000)
        let dates = SlidePostExporter.creationDates(count: 4, now: now)
        XCTAssertEqual(dates.map(\.timeIntervalSince1970), [997, 998, 999, 1_000])
        XCTAssertTrue(SlidePostExporter.creationDates(count: 0, now: now).isEmpty)
    }

    func testPhotosDeniedSurfacesAsItsOwnStatusAndWritesNothing() async {
        let session = await session(state()); var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .denied }, writePhotos: { _ in writes += 1 })
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(exporter.status, .photosDenied)
        XCTAssertEqual(writes, 0)
    }

    /// A tiny in-memory server for the slide-post routes: PUT saves (version + 1, render cleared),
    /// POST generate starts a render that reads "rendering" for one poll, GET reads the state.
    private final class FakeServer: @unchecked Sendable {
        var state: SlidePostState
        var calls: [String] = []
        var generated = false
        var pollsSinceGenerate = 0
        init(_ state: SlidePostState) { self.state = state }

        func handle(_ request: URLRequest) throws -> (Int, Data) {
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            if method == "PUT" {
                calls.append("save")
                var draft = state.draft!
                draft.version += 1; draft.caption = "Edited"; draft.renderedVersion = nil
                state.draft = draft; state.renderStatus = "not_rendered"; state.renderedVersion = nil; state.slides = []
                return (200, try JSONEncoder().encode(["slide_post": draft]))
            }
            if method == "POST", path.hasSuffix("/generate") {
                calls.append("generate")
                generated = true; pollsSinceGenerate = 0
                return (200, try JSONEncoder().encode(["slide_post": state.draft!]))
            }
            if generated {
                pollsSinceGenerate += 1
                if pollsSinceGenerate >= 2 {
                    var draft = state.draft!
                    draft.renderedVersion = draft.version
                    state.draft = draft; state.renderStatus = "ready"; state.renderedVersion = draft.version
                    state.slides = draft.slides.map { .init(id: $0.id, assetID: $0.assetID, kind: $0.kind, url: URL(string: "https://test/\($0.id).jpg")) }
                } else { state.renderStatus = "rendering" }
            }
            return (200, try JSONEncoder().encode(state))
        }
    }

    private func unrenderedState() -> SlidePostState {
        var value = state()
        value.renderStatus = "not_rendered"; value.renderedVersion = nil; value.slides = []
        value.draft?.renderedVersion = nil
        return value
    }

    func testDirtyUnrenderedPostSavesThenGeneratesThenSavesToPhotos() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        session.draft?.caption = "Edited"
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertFalse(session.canExport)
        var written: [SlidePostExporter.PhotoResource] = []
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized },
                                         writePhotos: { written = $0 })

        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 50)

        XCTAssertEqual(server.calls, ["save", "generate"], "saved first, then rendered")
        XCTAssertEqual(written.count, 2, "then both slides went to Photos")
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// A denial must surface before any save/render work, not after the user waited for the render.
    func testPhotosDeniedStopsTheExportBeforeSavingOrRendering() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        session.draft?.caption = "Edited"
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .denied }, writePhotos: { _ in writes += 1 })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 5)
        XCTAssertEqual(exporter.status, .photosDenied)
        XCTAssertTrue(server.calls.isEmpty, "no save and no render before access is known")
        XCTAssertEqual(writes, 0)
    }

    /// Leaving the editor cancels the flow: it stops instead of polling on and writing to Photos later.
    func testCancellingTheExportStopsItBeforeAnythingIsWritten() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        let task = Task { await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 50) }
        task.cancel()
        await task.value
        XCTAssertEqual(writes, 0)
        XCTAssertEqual(exporter.status, .idle)
        XCTAssertFalse(exporter.isBusy)
    }

    func testSavedButNeverRenderedPostGeneratesWithoutSavingAgain() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 50)
        XCTAssertEqual(server.calls, ["generate"])
        XCTAssertEqual(writes, 1)
    }

    func testCurrentRenderedPostExportsWithoutSavingOrGenerating() async {
        let server = FakeServer(state())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 5)
        XCTAssertTrue(server.calls.isEmpty)
        XCTAssertEqual(writes, 1)
    }

    func testRenderThatNeverFinishesEndsInAFailureStatusNotAHang() async {
        let server = FakeServer(unrenderedState())
        server.generated = true   // already rendering, and the stub's GET will flip to ready only after 2 polls
        server.state.renderStatus = "rendering"
        NativeEditorURLProtocol.handler = { request in
            var (status, data) = try server.handle(request)
            if request.httpMethod == "GET" { server.pollsSinceGenerate = 0; server.state.renderStatus = "rendering"; data = try JSONEncoder().encode(server.state); status = 200 }
            return (status, data)
        }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 3)
        XCTAssertEqual(writes, 0)
        guard case .failed = exporter.status else { return XCTFail("expected a failure status, got \(exporter.status)") }
    }

    // MARK: Gallery open: a server draft is the baseline

    func testPostOpenedWithAServerDraftEqualToItsRestoredLocalCopyIsNotUnsaved() async {
        // First launch: the post has no server draft, so the editor seeds an unsaved local one.
        var empty = state(); empty.draft = nil; empty.renderStatus = "not_rendered"; empty.renderedVersion = nil; empty.slides = []
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(empty)) }
        let first = SlidePostSession(defaults: defaults)
        await first.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        first.seedDraftIfNeeded()
        let seeded = try! XCTUnwrap(first.draft)
        XCTAssertTrue(first.hasUnsavedChanges, "a brand-new post with no server draft still reads unsaved")

        // Later the same post has that draft on the server (saved elsewhere); a fresh session opens it.
        var saved = state(); saved.draft = seeded; saved.draft?.version = 1
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(saved)) }
        let reopened = SlidePostSession(defaults: defaults)
        await reopened.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertFalse(reopened.hasUnsavedChanges, "the server draft is the baseline, not an unsaved edit")
    }

    func testPostWithAServerDraftIsNeverSeededWithALocalOne() async {
        let session = await session(state())
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertFalse(session.seedDraftIfNeeded() && session.hasUnsavedChanges)
    }

    func testFailedRevalidationAbortsBeforeDownloadingOrSaving() async {
        let session = await session(state())
        var downloads = 0
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in downloads += 1 }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })

        await exporter.saveToPhotos(session: session, revalidate: { throw URLError(.cannotConnectToHost) })

        XCTAssertEqual(downloads, 0)
        XCTAssertEqual(writes, 0)
    }
}
