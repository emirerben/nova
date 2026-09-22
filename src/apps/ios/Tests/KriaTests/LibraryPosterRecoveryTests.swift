import Foundation
import XCTest
@testable import Kria

@MainActor final class LibraryPosterRecoveryTests: XCTestCase {
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        LibraryPosterDeferredProtocol.handler = nil
        super.tearDown()
    }

    private func readyProject(
        id: UUID = UUID(),
        posterURL: URL? = nil,
        posterIdentity: String? = "selected:video.mp4",
        posterStatus: String? = "repairing"
    ) -> ProjectSummary {
        ProjectSummary(
            id: id,
            title: "Saved cut",
            status: .ready,
            updatedAt: .now,
            posterURL: posterURL,
            outputURL: URL(string: "https://media.example/selected.mp4"),
            outputVariantID: "selected",
            posterIdentity: posterIdentity,
            posterStatus: posterStatus
        )
    }

    nonisolated static func immediateSleep(_: TimeInterval) async throws {}

    nonisolated static func posterResponse(_ id: UUID, url: String? = nil, status: String = "ready") -> Data {
        let job: [String: Any] = ["id": id.uuidString, "poster_url": url.map { $0 as Any } ?? NSNull(), "poster_identity": "selected:video.mp4", "poster_status": status]
        return try! JSONSerialization.data(withJSONObject: ["jobs": [job]])
    }

    private func deferredAPI() -> KriaAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [LibraryPosterDeferredProtocol.self]
        return KriaAPI(baseURL: URL(string: "https://poster-recovery.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
    }

    func testRefreshPosterAPIEncodesBrokenIDsAndUsesTenSecondTimeout() async throws {
        let id = UUID()
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/me/jobs/posters/refresh")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.timeoutInterval, 10, accuracy: 0.01)
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: [String]])
            XCTAssertEqual(body["job_ids"], [id.uuidString])
            XCTAssertEqual(body["broken_job_ids"], [id.uuidString])
            return (200, Self.posterResponse(id, url: "https://media.example/poster.jpg"))
        }

        let posters = try await NativeEditorTestSupport.api().refreshLibraryPosters(jobIDs: [id], brokenJobIDs: [id])
        XCTAssertEqual(posters.first?.posterURL?.absoluteString, "https://media.example/poster.jpg")
        XCTAssertEqual(posters.first?.posterStatus, "ready")
    }

    func testLibraryDTODecodesPosterIdentityAndRepairingStatus() async throws {
        let id = UUID()
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/me/jobs")
            return (200, try! JSONSerialization.data(withJSONObject: ["jobs": [["id": id.uuidString, "mode": "generative", "status": "ready", "title": "Saved cut", "created_at": "2026-09-10T10:00:00Z", "poster_url": NSNull(), "poster_identity": "rank-two:video.mp4", "poster_status": "repairing", "output_url": "https://media.example/selected.mp4", "output_variant_id": "rank-two"]]]))
        }
        let library = try await NativeEditorTestSupport.api().library()
        let project = try XCTUnwrap(library.first)
        XCTAssertNil(project.posterURL)
        XCTAssertEqual(project.posterIdentity, "rank-two:video.mp4")
        XCTAssertEqual(project.posterStatus, "repairing")
        XCTAssertEqual(project.outputVariantID, "rank-two")
    }

    func testRecoveryUpdatesOnlyPosterAndPreservesSelectedOutputAndTitle() async {
        let project = readyProject()
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: [String]])
            XCTAssertEqual(body["broken_job_ids"], [project.id.uuidString])
            return (200, Self.posterResponse(project.id, url: "https://media.example/fresh.jpg"))
        }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()
        model.libraryPosterLoaded(project, revision: 0, succeeded: false)
        XCTAssertNil(model.libraryProjects[0].posterURL)
        XCTAssertEqual(model.libraryProjects[0].posterStatus, "repairing")

        await model.recoverLibraryPosters(sleep: Self.immediateSleep)

        let refreshed = model.libraryProjects[0]
        XCTAssertEqual(refreshed.posterURL?.absoluteString, "https://media.example/fresh.jpg")
        XCTAssertEqual(refreshed.title, project.title)
        XCTAssertEqual(refreshed.outputURL, project.outputURL)
        XCTAssertEqual(refreshed.outputVariantID, project.outputVariantID)
        XCTAssertEqual(model.posterLoadRevisions[project.id], 1)
    }

    func testTerminalPosterNeverRequestsRecovery() async {
        let project = readyProject(posterStatus: "unavailable")
        NativeEditorURLProtocol.handler = { _ in XCTFail("terminal poster must not retry"); return (500, Data()) }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()

        await model.recoverLibraryPosters(sleep: Self.immediateSleep)
        XCTAssertTrue(model.recoveringPosterIDs.isEmpty)
    }

    func testRecoveryBatchesTwentyAndGivesEveryEligibleJobATurn() async {
        let projects = (0..<21).map { _ in readyProject() }
        var batches: [[UUID]] = []
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: [String]])
            let ids = try XCTUnwrap(body["job_ids"]).compactMap(UUID.init(uuidString:))
            batches.append(ids)
            let jobs = ids.map { ["id": $0.uuidString, "poster_url": NSNull(), "poster_identity": "selected:video.mp4", "poster_status": "unavailable"] as [String: Any] }
            return (200, try! JSONSerialization.data(withJSONObject: ["jobs": jobs]))
        }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.libraryProjects = projects
        model.beginLibraryPosterRecovery()

        await model.recoverLibraryPosters(sleep: Self.immediateSleep)

        XCTAssertEqual(batches.map(\.count), [20, 1])
        XCTAssertEqual(Set(batches.flatMap { $0 }), Set(projects.map(\.id)))
    }

    func testTransportFailuresStopAfterEightAttempts() async {
        let project = readyProject()
        var requests = 0
        NativeEditorURLProtocol.handler = { _ in requests += 1; return (500, Data()) }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()

        await model.recoverLibraryPosters(sleep: Self.immediateSleep)
        XCTAssertEqual(requests, 8)
    }

    func testBackoffPersistsAcrossRecoveryRestartsAndStopsAtEight() async {
        let project = readyProject()
        let delays = PosterDelayRecorder()
        NativeEditorURLProtocol.handler = { _ in
            (200, Self.posterResponse(project.id, url: "https://media.example/reloaded.jpg"))
        }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()
        let recordSleep: @Sendable (TimeInterval) async throws -> Void = { delay in
            await delays.record(delay)
        }

        for _ in 0..<8 {
            await model.recoverLibraryPosters(sleep: recordSleep)
            let current = model.libraryProjects[0]
            model.libraryPosterLoaded(current, revision: model.posterLoadRevisions[project.id, default: 0], succeeded: false)
        }
        await model.recoverLibraryPosters(sleep: recordSleep)

        let recorded = await delays.values()
        XCTAssertEqual(recorded, [0.25, 2, 5, 10, 20, 30, 45, 60])
    }

    func testOldImageFailureCallbackRevisionIsIgnored() async {
        let project = readyProject()
        NativeEditorURLProtocol.handler = { _ in
            (200, Self.posterResponse(project.id, url: "https://media.example/fresh.jpg"))
        }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()
        await model.recoverLibraryPosters(sleep: Self.immediateSleep)
        XCTAssertEqual(model.posterLoadRevisions[project.id], 1)

        model.libraryPosterLoaded(project, revision: 0, succeeded: false)
        XCTAssertEqual(model.libraryPosterRecoveryVersion, 1)
    }

    func testReloadedLibraryAndCancelledTaskIgnoreHeldPosterResponse() async throws {
        let project = readyProject()
        let started = expectation(description: "poster request held")
        var held: LibraryPosterDeferredProtocol?
        LibraryPosterDeferredProtocol.handler = { transport in
            if transport.request.url?.path == "/me/jobs/posters/refresh" {
                held = transport; started.fulfill()
            } else {
                transport.finish(200, Data("{\"jobs\":[]}".utf8))
            }
        }
        let model = AppModel(api: deferredAPI())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()
        let recovery = Task { await model.recoverLibraryPosters(sleep: Self.immediateSleep) }
        await fulfillment(of: [started], timeout: 3)

        await model.loadLibrary() // advances the snapshot and removes the row
        recovery.cancel()
        try XCTUnwrap(held).finish(200, Self.posterResponse(project.id, url: "https://media.example/stale.jpg"))
        await recovery.value

        XCTAssertTrue(model.libraryProjects.isEmpty)
        XCTAssertTrue(model.recoveringPosterIDs.isEmpty)
    }

    func testNewLibrarySnapshotForSameJobRejectsHeldOldPosterResponse() async throws {
        let project = readyProject()
        let started = expectation(description: "poster request held")
        var held: LibraryPosterDeferredProtocol?
        LibraryPosterDeferredProtocol.handler = { transport in
            if transport.request.url?.path == "/me/jobs/posters/refresh" {
                held = transport; started.fulfill()
            } else {
                let job: [String: Any] = ["id": project.id.uuidString, "mode": "generative", "status": "ready", "title": "Saved cut", "created_at": "2026-09-10T10:00:00Z", "poster_url": "https://media.example/new-signed.jpg", "poster_identity": "selected:video.mp4", "poster_status": "ready", "output_url": "https://media.example/selected.mp4", "output_variant_id": "selected"]
                transport.finish(200, try! JSONSerialization.data(withJSONObject: ["jobs": [job]]))
            }
        }
        let model = AppModel(api: deferredAPI())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()
        let recovery = Task { await model.recoverLibraryPosters(sleep: Self.immediateSleep) }
        await fulfillment(of: [started], timeout: 3)

        await model.loadLibrary()
        try XCTUnwrap(held).finish(200, Self.posterResponse(project.id, url: "https://media.example/old.jpg"))
        await recovery.value

        XCTAssertEqual(model.libraryProjects[0].posterURL?.absoluteString, "https://media.example/new-signed.jpg")
    }

    func testDismissalIgnoresAnInFlightRecoveryResponse() async throws {
        let project = readyProject()
        let started = expectation(description: "poster refresh started")
        var held: LibraryPosterDeferredProtocol?
        LibraryPosterDeferredProtocol.handler = { transport in
            held = transport
            started.fulfill()
        }
        let model = AppModel(api: deferredAPI())
        model.libraryProjects = [project]
        model.beginLibraryPosterRecovery()
        let recovering = Task { await model.recoverLibraryPosters(sleep: Self.immediateSleep) }
        await fulfillment(of: [started], timeout: 3)

        model.endLibraryPosterRecovery()
        try XCTUnwrap(held).finish(200, Self.posterResponse(project.id, url: "https://media.example/stale.jpg"))
        await recovering.value

        XCTAssertNil(model.libraryProjects[0].posterURL)
        XCTAssertTrue(model.recoveringPosterIDs.isEmpty)
    }
}

actor PosterDelayRecorder {
    private var recorded: [TimeInterval] = []
    func record(_ delay: TimeInterval) { recorded.append(delay) }
    func values() -> [TimeInterval] { recorded }
}

/// A controllable protocol kept local to poster recovery tests for future
/// cancellation and stale-response races; it deliberately does not share the
/// project-action transport's global handler.
final class LibraryPosterDeferredProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((LibraryPosterDeferredProtocol) -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.handler?(self) }
    override func stopLoading() {}
    func finish(_ status: Int, _ data: Data) {
        guard let url = request.url else { return }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}
