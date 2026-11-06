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

    func testExtendedOnDeviceExportWritesMixedKindsWithoutServerRender() async {
        var value = state()
        value.assets = value.assets.map { asset in
            var result = asset
            result.sourceURL = URL(string: "https://source/\(asset.id)")
            return result
        }
        value.draft?.slides[0].edits = SlidePostEdits(lookPreset: "golden_hour")
        let session = await session(value)
        var resources: [SlidePostExporter.PhotoResource] = []
        var sourceIDs: [String] = []
        let exporter = SlidePostExporter(
            downloadFile: { _ in XCTFail("Must not download server derivatives"); throw URLError(.badURL) },
            authorizePhotos: { .authorized }, writePhotos: { resources = $0 },
            onDeviceRender: { _, asset, _, _, _ in sourceIDs.append(asset.id); return Data("jpeg".utf8) },
            onDeviceVideoRender: { _, asset, _, _, url, _ in sourceIDs.append(asset.id); try Data("mp4".utf8).write(to: url) }
        )
        NativeEditorURLProtocol.handler = { _ in XCTFail("Must not save, generate or poll"); return (500, Data()) }
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID, extendedOnDevice: true)
        XCTAssertEqual(sourceIDs, ["a", "b"])
        XCTAssertEqual(resources.map(\.kind), ["image", "video"])
        XCTAssertEqual(resources.map(\.url.lastPathComponent), ["01.jpg", "02.mp4"])
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
        XCTAssertFalse(FileManager.default.fileExists(atPath: resources[0].url.deletingLastPathComponent().path))
    }

    func testSecondLocalVideoFailureNeverWritesPhotosAndCleansFiles() async {
        var value = state()
        value.assets = value.assets.map { asset in
            var result = asset; result.sourceURL = URL(string: "https://source/\(asset.id)"); return result
        }
        let session = await session(value)
        var writes = 0
        var output: URL?
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 },
            onDeviceRender: { _, _, _, _, _ in Data("jpeg".utf8) },
            onDeviceVideoRender: { _, _, _, _, url, _ in output = url; try Data("partial".utf8).write(to: url); throw URLError(.cannotDecodeContentData) })
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID, extendedOnDevice: true)
        XCTAssertEqual(writes, 0)
        if let output { XCTAssertFalse(FileManager.default.fileExists(atPath: output.deletingLastPathComponent().path)) }
        else { XCTFail("Video render did not run") }
        guard case .failed = exporter.status else { return XCTFail("Failure should be visible") }
    }

    func testEditingDuringLocalVideoExportPreventsStalePhotoCommit() async {
        var value = state()
        value.assets = value.assets.map { asset in
            var result = asset; result.sourceURL = URL(string: "https://source/\(asset.id)"); return result
        }
        let session = await session(value)
        var writes = 0
        var output: URL?
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 },
            onDeviceRender: { _, _, _, _, _ in Data("jpeg".utf8) },
            onDeviceVideoRender: { _, _, _, _, url, _ in
                output = url
                session.draft?.caption = "Edited while rendering"
                try Data("mp4".utf8).write(to: url)
            })
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID, extendedOnDevice: true)
        XCTAssertEqual(writes, 0)
        if let output { XCTAssertFalse(FileManager.default.fileExists(atPath: output.deletingLastPathComponent().path)) }
        guard case .failed = exporter.status else { return XCTFail("Changed draft should require a new export") }
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

    // Failure matrix: transient GETs recover without replaying generate; persistent failures
    // stop; terminal reads do not retry; recovered refreshes preserve dirty local edits.
    func testTransientReadAfterGenerateRecoversWithoutAnotherRender() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var failed = false
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET", server.generated, !failed {
                failed = true
                return (503, Data())
            }
            return try server.handle(request)
        }
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 5)
        XCTAssertEqual(server.calls, ["generate"])
        XCTAssertEqual(writes, 1)
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
        XCTAssertNil(session.error)
    }

    func testSuccessfulRefreshClearsReadErrorAndPreservesDirtyDraft() async {
        let session = await session(state())
        session.draft?.caption = "Local edit"
        NativeEditorURLProtocol.handler = { _ in (403, Data()) }
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertNotNil(session.error)
        var latest = state(version: 2)
        latest.draft?.caption = "Remote edit"
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(latest)) }
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertNil(session.error)
        XCTAssertEqual(session.draft?.caption, "Local edit")
        XCTAssertEqual(session.state?.draft?.version, 2)
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testPersistentReadFailureIsBoundedAndDoesNotReplayGenerate() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var reads = 0
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" { reads += 1; return (503, Data()) }
            return try server.handle(request)
        }
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in XCTFail("Must not export stale state") })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 5)
        XCTAssertEqual(reads, 3)
        XCTAssertEqual(server.calls, ["generate"])
        guard case .failed = exporter.status else { return XCTFail("Exhausted reads must stop") }
    }

    func testAcknowledgedGenerateWithLaggingReadIsNotDispatchedTwice() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        let lagging = unrenderedState()
        var reads = 0
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" {
                reads += 1
                if reads < 3 { return (200, try JSONEncoder().encode(lagging)) }
            }
            return try server.handle(request)
        }
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 8)
        XCTAssertEqual(server.calls, ["generate"])
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
    }

    func testUncertainGenerateFailureIsNeverReplayed() async {
        let session = await session(unrenderedState())
        var mutations = 0
        NativeEditorURLProtocol.handler = { _ in mutations += 1; return (503, Data()) }
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in XCTFail("Must not write") })
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 5)
        XCTAssertEqual(mutations, 1)
        guard case .failed = exporter.status else { return XCTFail("Uncertain mutation must stop") }
    }

    func testTerminalReadFailureIsNotRetried() async {
        let session = await session(state())
        var reads = 0
        NativeEditorURLProtocol.handler = { _ in reads += 1; return (403, Data()) }
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(reads, 1)
        XCTAssertNotNil(session.error)
    }

    func testReadRetryHonorsServerBackoffAndNeverRetriesTerminalFailures() {
        XCTAssertEqual(SlidePostSession.readRetryDelay(APIError.requestFailed(status: 429, detail: .init(nil, retryAfter: 12)), attempt: 0), .seconds(12))
        XCTAssertEqual(SlidePostSession.readRetryDelay(URLError(.timedOut), attempt: 0), .milliseconds(500))
        XCTAssertEqual(SlidePostSession.readRetryDelay(URLError(.timedOut), attempt: 1), .seconds(1))
        XCTAssertNil(SlidePostSession.readRetryDelay(APIError.requestFailed(status: 429, detail: .init(nil, retryAfter: 31)), attempt: 0))
        XCTAssertNil(SlidePostSession.readRetryDelay(APIError.sessionExpired, attempt: 0))
        XCTAssertNil(SlidePostSession.readRetryDelay(APIError.invalidResponse, attempt: 0))
        XCTAssertNil(SlidePostSession.readRetryDelay(URLError(.cancelled), attempt: 0))
    }

    func testCancelledRefreshDuringBackoffStopsWithoutPublishingError() async {
        let waiting = expectation(description: "Read entered backoff")
        let session = SlidePostSession(defaults: defaults, readRetrySleep: { _ in
            waiting.fulfill()
            try await Task.sleep(for: .seconds(60))
        })
        var reads = 0
        NativeEditorURLProtocol.handler = { _ in reads += 1; return (503, Data()) }
        let task = Task { await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID) }
        await fulfillment(of: [waiting], timeout: 2)
        task.cancel()
        await task.value
        XCTAssertEqual(reads, 1)
        XCTAssertNil(session.state)
        XCTAssertNil(session.error)
    }

    func testNewerRefreshSupersedesRetryWithoutPublishingOldFailure() async {
        let waiting = expectation(description: "Old read entered backoff")
        var resume: CheckedContinuation<Void, Never>?
        let session = SlidePostSession(defaults: defaults, readRetrySleep: { _ in
            await withCheckedContinuation { resume = $0; waiting.fulfill() }
        })
        NativeEditorURLProtocol.handler = { _ in (503, Data()) }
        let old = Task { await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID) }
        await fulfillment(of: [waiting], timeout: 2)
        let latest = state(version: 2)
        var reads = 0
        NativeEditorURLProtocol.handler = { _ in reads += 1; return (200, try JSONEncoder().encode(latest)) }
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        resume?.resume()
        await old.value
        XCTAssertEqual(reads, 1, "Superseded reads must not issue another GET")
        XCTAssertEqual(session.state?.draft?.version, 2)
        XCTAssertNil(session.error)
    }

    func testItemSwitchDuringBackoffCannotRestoreTheOldItem() async {
        let waiting = expectation(description: "Old item entered backoff")
        var resume: CheckedContinuation<Void, Never>?
        let session = SlidePostSession(defaults: defaults, readRetrySleep: { _ in
            await withCheckedContinuation { resume = $0; waiting.fulfill() }
        })
        NativeEditorURLProtocol.handler = { _ in (503, Data()) }
        let old = Task { await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID) }
        await fulfillment(of: [waiting], timeout: 2)
        let next = SlidePostState(itemID: "22222222-2222-2222-2222-222222222222", title: "Next post", draft: state().draft)
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(next)) }
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: next.itemID)
        resume?.resume()
        await old.value
        XCTAssertEqual(session.state?.itemID, next.itemID)
        XCTAssertNil(session.error)
    }

    func testWrongItemSaveAndCreateNeverDispatchMutations() async {
        let session = await session(unrenderedState())
        session.draft?.caption = "Local edit"
        var mutations = 0
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod != "GET" { mutations += 1 }
            return (503, Data())
        }
        let otherID = "22222222-2222-2222-2222-222222222222"
        await session.save(api: NativeEditorTestSupport.api(), itemID: otherID)
        XCTAssertEqual(mutations, 0)
        XCTAssertNotNil(session.error)
        await session.create(api: NativeEditorTestSupport.api(), itemID: otherID)
        XCTAssertEqual(mutations, 0)
        XCTAssertNotNil(session.error)
        XCTAssertEqual(session.draft?.caption, "Local edit")
    }

    func testExistingRenderWithLaggingReadDoesNotDispatchAgain() async {
        var rendering = unrenderedState()
        rendering.renderStatus = "rendering"
        let session = await session(rendering)
        let server = FakeServer(unrenderedState())
        server.generated = true
        let lagging = unrenderedState()
        var reads = 0
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" {
                reads += 1
                if reads == 1 { return (200, try JSONEncoder().encode(lagging)) }
            }
            return try server.handle(request)
        }
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in })
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 8)
        XCTAssertTrue(server.calls.isEmpty)
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
    }

    func testStandaloneCreateKeepsPollingThroughLaggingAcknowledgement() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        let lagging = unrenderedState()
        var reads = 0
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" {
                reads += 1
                if reads <= 2 { return (200, try JSONEncoder().encode(lagging)) }
            }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        XCTAssertTrue(session.isRendering, "An acknowledged render must keep the workspace poll alive")
        // The workspace only polls while isRendering is true.
        for _ in 0..<4 where session.isRendering { await session.refresh(api: api, itemID: itemID) }
        XCTAssertTrue(session.canExport)
        XCTAssertFalse(session.isRendering)
        XCTAssertEqual(server.calls, ["generate"])
    }

    func testAcknowledgedRenderBudgetRetainsIdentityAndCanRecover() {
        let oldJob = UUID()
        var acknowledgement = SlidePostRenderAcknowledgement(version: 7, previousJobID: oldJob, remainingReads: 2)
        acknowledgement.observe(jobID: oldJob, status: "failed", canExport: false)
        XCTAssertEqual(acknowledgement.outcome, .waiting, "An old failure is not a new render receipt")
        acknowledgement.observe(jobID: oldJob, status: "not_rendered", canExport: false)
        XCTAssertEqual(acknowledgement.outcome, .exhausted)
        XCTAssertEqual(acknowledgement.version, 7, "Exhaustion must not authorize another dispatch")
        acknowledgement.observe(jobID: oldJob, status: "ready", canExport: true)
        XCTAssertEqual(acknowledgement.outcome, .ready)
    }

    func testAcknowledgedRenderRecognizesNewJobFailure() {
        let oldJob = UUID()
        var acknowledgement = SlidePostRenderAcknowledgement(version: 7, previousJobID: oldJob)
        acknowledgement.observe(jobID: UUID(), status: "failed", canExport: false)
        XCTAssertEqual(acknowledgement.outcome, .failed)
        var active = SlidePostRenderAcknowledgement(version: 7, previousJobID: oldJob)
        active.observe(jobID: oldJob, status: "rendering", canExport: false)
        active.observe(jobID: oldJob, status: "failed", canExport: false)
        XCTAssertEqual(active.outcome, .failed)
    }

    func testExhaustedAcknowledgedRenderNeverDispatchesAgain() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        let lagging = unrenderedState()
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" { return (200, try JSONEncoder().encode(lagging)) }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        for _ in 0..<200 where session.isRendering { await session.refresh(api: api, itemID: itemID) }
        XCTAssertFalse(session.isRendering)
        XCTAssertNotNil(session.error)
        await session.create(api: api, itemID: itemID)
        XCTAssertEqual(server.calls, ["generate"])
        XCTAssertNotNil(session.error)
        let next = SlidePostState(itemID: "22222222-2222-2222-2222-222222222222", title: "Next post", draft: state().draft)
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(next)) }
        await session.refresh(api: api, itemID: next.itemID)
        XCTAssertEqual(session.state?.itemID, next.itemID)
        XCTAssertNil(session.error, "The previous item's exhausted render must not poison the new item")
    }

    func testLowerVersionLagConsumesAcknowledgedRenderBudget() async {
        var current = unrenderedState()
        current.draft?.version = 2
        let server = FakeServer(current)
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        let old = unrenderedState()
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" { return (200, try JSONEncoder().encode(old)) }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        for _ in 0..<200 where session.isRendering { await session.refresh(api: api, itemID: itemID) }
        XCTAssertFalse(session.isRendering)
        XCTAssertEqual(session.state?.draft?.version, 2)
        XCTAssertNotNil(session.error)
        await session.create(api: api, itemID: itemID)
        XCTAssertEqual(server.calls, ["generate"])
    }

    func testConfirmedRenderFailureAllowsANewExplicitCreate() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var failed = unrenderedState()
        failed.jobID = UUID()
        failed.renderStatus = "failed"
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" { return (200, try JSONEncoder().encode(failed)) }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        XCTAssertFalse(session.isRendering)
        XCTAssertNotNil(session.error)
        await session.create(api: api, itemID: itemID)
        XCTAssertEqual(server.calls, ["generate", "generate"])
    }

    func testReadyRefreshClearsConfirmedRenderFailureButPreservesUnrelatedErrors() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        var failed = unrenderedState()
        failed.jobID = UUID()
        failed.renderStatus = "failed"
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" { return (200, try JSONEncoder().encode(failed)) }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        XCTAssertNotNil(session.error)
        XCTAssertFalse(session.isRendering)
        let ready = state()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(ready)) }
        await session.refresh(api: api, itemID: itemID)
        XCTAssertTrue(session.canExport)
        XCTAssertNil(session.error, "A confirmed ready read supersedes the earlier render failure")
        session.error = "Keep this unrelated mutation error"
        await session.refresh(api: api, itemID: itemID)
        XCTAssertEqual(session.error, "Keep this unrelated mutation error")
    }

    func testCommittedGenerateWithLostResponseIsReobservedByCreateAndNewExport() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        let lagging = unrenderedState()
        var reads = 0
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "POST" {
                _ = try server.handle(request) // The server committed before the response was lost.
                throw URLError(.networkConnectionLost)
            }
            reads += 1
            if reads == 1 { return (200, try JSONEncoder().encode(lagging)) }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        XCTAssertTrue(session.isRendering, "An uncertain submission must keep read convergence active")
        XCTAssertNotNil(session.error)
        await session.create(api: api, itemID: itemID)
        XCTAssertEqual(server.calls, ["generate"], "A second create must only reobserve the submitted version")
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 })
        await exporter.export(.photos, session: session, api: api, itemID: itemID, pollInterval: .milliseconds(1), maxPolls: 8)
        XCTAssertEqual(server.calls, ["generate"], "A new export flow must not duplicate the uncertain mutation")
        XCTAssertEqual(writes, 1)
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
        XCTAssertNil(session.error)
    }

    func testCancelledGenerateResponseRetainsSubmissionIdentity() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "POST" {
                _ = try server.handle(request)
                throw URLError(.cancelled)
            }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        XCTAssertTrue(session.isRendering)
        await session.create(api: api, itemID: itemID)
        XCTAssertEqual(server.calls, ["generate"])
        session.error = "Keep this unrelated save error"
        await session.refresh(api: api, itemID: itemID)
        XCTAssertTrue(session.canExport)
        XCTAssertEqual(session.error, "Keep this unrelated save error")
    }

    func testDefinitiveGenerateRejectionAllowsALaterExplicitAttempt() async {
        let session = await session(unrenderedState())
        var posts = 0
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            posts += 1
            return (403, Data())
        }
        await session.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertFalse(session.isRendering)
        XCTAssertNotNil(session.error)
        await session.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(posts, 2)
    }

    func testFollowupReadRejectionDoesNotClearSubmittedRenderIdentity() async {
        let server = FakeServer(unrenderedState())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "GET" { return (403, Data()) }
            return try server.handle(request)
        }
        await session.create(api: api, itemID: itemID)
        XCTAssertTrue(session.isRendering)
        await session.create(api: api, itemID: itemID)
        XCTAssertEqual(server.calls, ["generate"], "A read rejection cannot authorize repeating an accepted POST")
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
