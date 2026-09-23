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
        let exporter = SlidePostExporter(downloadFile: downloader { requested.append($0) }, authorizePhotos: { .authorized }, writePhotos: { _ in }, defaults: defaults)
        await exporter.prepareShare(session: session, revalidate: {})
        XCTAssertEqual(requested.map(\.lastPathComponent), ["one.jpg", "two.mp4"])
        XCTAssertEqual(exporter.shareItems.count, 3)
        XCTAssertEqual(exporter.shareItems.last?.lastPathComponent, "caption.txt")
        XCTAssertEqual(try? String(contentsOf: exporter.shareItems.last!), "Caption")
        exporter.discardShareDirectory()
    }

    func testFailedSecondDownloadCleansUpAndNeverWritesPhotos() async {
        let session = await session(state()); var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader({ _ in }, failSecond: true), authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 }, defaults: defaults)
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(writes, 0)
        XCTAssertFalse(defaults.bool(forKey: "kria.slide-post.photos.\(itemID).1"))
    }

    func testVersionChangeAfterDownloadBlocksPhotoCommit() async {
        let session = await session(state()); var writes = 0; var changed = false
        let exporter = SlidePostExporter(downloadFile: downloader { _ in
            if !changed { changed = true; session.draft?.caption = "new" }
        }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 }, defaults: defaults)
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(writes, 0)
    }

    func testAtomicWriteReceiptPreventsDuplicateAndFailureCanRetry() async {
        let session = await session(state()); var downloads = 0; var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in downloads += 1 }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 }, defaults: defaults)
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(writes, 1); XCTAssertTrue(defaults.bool(forKey: "kria.slide-post.photos.\(itemID).1"))
        await exporter.saveToPhotos(session: session, revalidate: {})
        XCTAssertEqual(writes, 1); XCTAssertEqual(downloads, 2)
        let retryDefaults = UserDefaults(suiteName: "\(suite).retry")!
        let failed = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in throw URLError(.cannotWriteToFile) }, defaults: retryDefaults)
        await failed.saveToPhotos(session: session, revalidate: {})
        XCTAssertFalse(retryDefaults.bool(forKey: "kria.slide-post.photos.\(itemID).1"))
    }

    func testSecondRevalidationWithNewerServerDraftBlocksPhotoCommit() async {
        let box = StateBox(state())
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(box.value)) }
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        var checks = 0
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 }, defaults: defaults)

        await exporter.saveToPhotos(session: session, revalidate: {
            checks += 1
            if checks == 2 { box.value = self.state(version: 2) }
            await session.refresh(api: NativeEditorTestSupport.api(), itemID: self.itemID)
        })

        XCTAssertEqual(checks, 2)
        XCTAssertEqual(writes, 0)
        XCTAssertFalse(defaults.bool(forKey: "kria.slide-post.photos.\(itemID).1"))
    }

    func testFailedRevalidationAbortsBeforeDownloadingOrSaving() async {
        let session = await session(state())
        var downloads = 0
        var writes = 0
        let exporter = SlidePostExporter(downloadFile: downloader { _ in downloads += 1 }, authorizePhotos: { .authorized }, writePhotos: { _ in writes += 1 }, defaults: defaults)

        await exporter.saveToPhotos(session: session, revalidate: { throw URLError(.cannotConnectToHost) })

        XCTAssertEqual(downloads, 0)
        XCTAssertEqual(writes, 0)
        XCTAssertFalse(defaults.bool(forKey: "kria.slide-post.photos.\(itemID).1"))
    }
}
