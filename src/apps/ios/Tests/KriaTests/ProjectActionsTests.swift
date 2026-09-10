import XCTest
import SwiftData
@testable import Kria

@MainActor final class ProjectActionsTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; ProjectActionDeferredProtocol.handler = nil; super.tearDown() }

    private func response(_ id: UUID, title: String = "Renamed", revision: Int = 2) -> Data {
        Data("{\"id\":\"\(id.uuidString)\",\"title\":\"\(title)\",\"status\":\"draft\",\"revision\":\(revision),\"runtime_version\":2,\"updated_at\":\"2026-09-10T10:00:00Z\"}".utf8)
    }

    func testRenameUsesRevisionAndIdempotencyAndUpdatesSelectionAndCache() async throws {
        let project = PreviewFixtures.projects[0]
        let response = response(project.id)
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "PATCH")
            XCTAssertEqual(request.url?.path, "/creation-threads/\(project.id.uuidString)")
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["title"] as? String, "Renamed")
            XCTAssertEqual(body["expected_revision"] as? Int, project.serverRevision)
            XCTAssertEqual(body["client_event_id"] as? String, "rename-retry-1")
            return (200, response)
        }
        let container = try ModelContainer(for: CachedProject.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)
        model.projects = [project]; model.selectedProject = project
        try await model.renameProject(project, title: "Renamed", clientEventID: "rename-retry-1")
        XCTAssertEqual(model.selectedProject?.title, "Renamed")
        XCTAssertEqual(model.projects.first?.serverRevision, 2)
        XCTAssertEqual(try cache.projects().first?.title, "Renamed")
    }

    func testRenameConflictRefreshesAuthoritativeProjectWithoutApplyingRequestedName() async throws {
        let project = PreviewFixtures.projects[0]
        let latest = response(project.id, title: "Elsewhere", revision: 7)
        NativeEditorURLProtocol.handler = { request in request.httpMethod == "PATCH" ? (409, Data()) : (200, latest) }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.projects = [project]; model.selectedProject = project
        do { try await model.renameProject(project, title: "Mine", clientEventID: "retry"); XCTFail("Expected conflict") }
        catch { XCTAssertEqual(error as? APIError, .conflict) }
        XCTAssertEqual(model.selectedProject?.title, "Elsewhere")
        XCTAssertEqual(model.selectedProject?.serverRevision, 7)
    }

    func testRenameFailurePreservesProject() async {
        let project = PreviewFixtures.projects[0]
        NativeEditorURLProtocol.handler = { _ in (500, Data()) }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.projects = [project]; model.selectedProject = project
        do { try await model.renameProject(project, title: "Mine", clientEventID: "retry"); XCTFail("Expected failure") } catch {}
        XCTAssertEqual(model.selectedProject?.title, project.title)
    }

    func testDeleteAcceptsEmpty204RemovesCacheAndSelectsRemainingProject() async throws {
        let project = PreviewFixtures.projects[0]
        let remaining = PreviewFixtures.projects[1]
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "DELETE" {
                XCTAssertEqual(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value, String(project.serverRevision))
                return (204, Data())
            }
            return (200, Data("{\"jobs\":[]}".utf8))
        }
        let container = try ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        try cache.upsert([project, remaining])
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)
        model.selectedProject = project
        try await model.deleteProject(project)
        XCTAssertEqual(model.projects.map(\.id), [remaining.id])
        XCTAssertEqual(model.selectedProject?.id, remaining.id)
        XCTAssertFalse(try cache.projects().contains { $0.id == project.id })
    }

    func testDeleteRenderingProjectDoesNotSendRequest() async {
        let model = AppModel(api: NativeEditorTestSupport.api())
        NativeEditorURLProtocol.handler = { _ in XCTFail("Must not delete while rendering"); return (204, Data()) }
        do { try await model.deleteProject(PreviewFixtures.projects[2]); XCTFail("Expected guard") }
        catch { XCTAssertEqual(error as? APIError, .conflict) }
    }

    func testDeleteFailureKeepsSelection() async {
        let project = PreviewFixtures.projects[0]
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.projects = [project]; model.selectedProject = project
        NativeEditorURLProtocol.handler = { _ in (500, Data()) }
        do { try await model.deleteProject(project); XCTFail("Expected failure") } catch {}
        XCTAssertEqual(model.selectedProject?.id, project.id)
        XCTAssertEqual(model.projects.count, 1)
    }
    func testDelayedRenameResponseCannotRegressNewerProjectRevision() async throws {
        let original = PreviewFixtures.projects[0]
        var newer = original
        newer.title = "Latest title"; newer.serverRevision = 3
        let delayed = response(original.id, title: "Older rename", revision: 2)
        NativeEditorURLProtocol.handler = { _ in (200, delayed) }
        let container = try ModelContainer(for: CachedProject.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)
        model.projects = [original]; model.selectedProject = original
        model.updateProject(newer)
        // The caller still holds the earlier request's projection while polling
        // has already published revision 3 into the model.
        try await model.renameProject(original, title: "Older rename", clientEventID: "delayed")
        XCTAssertEqual(model.selectedProject?.serverRevision, 3)
        XCTAssertEqual(model.selectedProject?.title, "Latest title")
        XCTAssertEqual(try cache.projects().first?.serverRevision, 3)
    }

    func testLateProjectionAndListCannotResurrectDeletedProject() async throws {
        let project = PreviewFixtures.projects[0]
        let staleList = Data("[".utf8) + response(project.id) + Data("]".utf8)
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "DELETE" { return (204, Data()) }
            if request.url?.path == "/creation-threads" { return (200, staleList) }
            return (200, Data("{\"jobs\":[]}".utf8))
        }
        let container = try ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        try cache.upsert([project])
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)
        model.selectedProject = project
        try await model.deleteProject(project)
        model.updateProject(project)
        model.selectProject(project)
        await model.loadProjects()
        XCTAssertTrue(model.projects.isEmpty)
        XCTAssertNil(model.selectedProject)
        XCTAssertTrue(try cache.projects().isEmpty)
    }

    func testDeleteConflictRefreshesSelectionAndKeepsCache() async throws {
        let project = PreviewFixtures.projects[0]
        let latest = response(project.id, title: "Changed elsewhere", revision: 9)
        NativeEditorURLProtocol.handler = { request in
            request.httpMethod == "DELETE" ? (409, Data()) : (200, latest)
        }
        let container = try ModelContainer(for: CachedProject.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        try cache.upsert([project])
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)
        model.selectedProject = project
        do { try await model.deleteProject(project); XCTFail("Expected conflict") }
        catch { XCTAssertEqual(error as? APIError, .conflict) }
        XCTAssertEqual(model.projects.count, 1)
        XCTAssertEqual(model.selectedProject?.serverRevision, 9)
        XCTAssertEqual(try cache.projects().first?.title, "Changed elsewhere")
    }

    func testDeleteRemovesOnlyOwnedDependentCacheRows() async throws {
        let project = PreviewFixtures.projects[0]
        let remaining = PreviewFixtures.projects[1]
        NativeEditorURLProtocol.handler = { request in
            request.httpMethod == "DELETE" ? (204, Data()) : (200, Data("{\"jobs\":[]}".utf8))
        }
        let container = try ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let context = container.mainContext
        let cache = CacheRepository(context: context)
        try cache.upsert([project, remaining])
        for owner in [project, remaining] {
            context.insert(CachedAsset(AssetSummary(id: UUID(), projectID: owner.id, name: "clip", kind: .video, duration: 3, thumbnailURL: nil)))
            context.insert(CachedUploadJob(UploadJob(id: UUID(), projectID: owner.id, filename: "clip.mov", state: .uploaded, progress: 1, source: .files)))
            context.insert(CachedReceipt(RenderReceipt(id: UUID(), projectID: owner.id, generation: 1, createdAt: .now, status: "ready", message: "Done", outputURL: nil)))
        }
        try context.save()
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)
        try await model.deleteProject(project)
        XCTAssertEqual(try context.fetch(FetchDescriptor<CachedAsset>()).map(\.projectID), [remaining.id])
        XCTAssertEqual(try context.fetch(FetchDescriptor<CachedUploadJob>()).map(\.projectID), [remaining.id])
        XCTAssertEqual(try context.fetch(FetchDescriptor<CachedReceipt>()).map(\.projectID), [remaining.id])
    }

    func testDeleteWithRecoveredUploadDoesNotSendRequest() async throws {
        let key = "kria.background-upload-recovery.v1"
        let previous = UserDefaults.standard.data(forKey: key)
        defer {
            if let previous { UserDefaults.standard.set(previous, forKey: key) }
            else { UserDefaults.standard.removeObject(forKey: key) }
        }
        let project = PreviewFixtures.projects[0]
        let record = UploadRecoveryRecord(id: UUID(), projectID: project.id, localFilePath: "/not-uploaded.mov", filename: "clip.mov", source: .files, purpose: .cloudRenderSource, taskIdentifier: 98765, retryCount: 0)
        UserDefaults.standard.set(try JSONEncoder().encode([record]), forKey: key)
        NativeEditorURLProtocol.handler = { _ in XCTFail("An outstanding upload must block deletion"); return (204, Data()) }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.projects = [project]; model.selectedProject = project
        XCTAssertEqual(model.uploads.records.count, 1)
        do { try await model.deleteProject(project); XCTFail("Expected conflict") }
        catch { XCTAssertEqual(error as? APIError, .conflict) }
        XCTAssertEqual(model.selectedProject?.id, project.id)
    }

    func testInFlightProjectListCannotReplaceCollectionAfterDeletion() async throws {
        let project = PreviewFixtures.projects[0]
        let remaining = PreviewFixtures.projects[1]
        let started = expectation(description: "List request suspended")
        var held: ProjectActionDeferredProtocol?
        ProjectActionDeferredProtocol.handler = { transport in
            if transport.request.url?.path == "/creation-threads" {
                held = transport; started.fulfill()
            } else if transport.request.httpMethod == "DELETE" {
                transport.finish(204, Data())
            } else { transport.finish(200, Data("{\"jobs\":[]}".utf8)) }
        }
        let model = AppModel(api: deferredAPI())
        model.projects = [project, remaining]; model.selectedProject = project
        let loading = Task { await model.loadProjects() }
        await fulfillment(of: [started], timeout: 3)
        try await model.deleteProject(project)
        // The server list began before deletion and also omits the remaining
        // project: accepting any part of this response would lose local state.
        let stale = Data("[".utf8) + response(project.id) + Data("]".utf8)
        try XCTUnwrap(held).finish(200, stale)
        await loading.value
        XCTAssertEqual(model.projects.map(\.id), [remaining.id])
        XCTAssertEqual(model.selectedProject?.id, remaining.id)
        XCTAssertFalse(model.isLoading)
    }

    func testInFlightLibraryCannotRestoreDeletedRender() async throws {
        var project = PreviewFixtures.projects[0]
        let jobID = UUID()
        project.activeJobID = jobID
        let started = expectation(description: "Library request suspended")
        var held: ProjectActionDeferredProtocol?
        var libraryCalls = 0
        ProjectActionDeferredProtocol.handler = { transport in
            if transport.request.httpMethod == "DELETE" { transport.finish(204, Data()); return }
            libraryCalls += 1
            if libraryCalls == 1 { held = transport; started.fulfill() }
            else { transport.finish(200, Data("{\"jobs\":[]}".utf8)) }
        }
        let model = AppModel(api: deferredAPI())
        model.projects = [project]; model.selectedProject = project
        let loading = Task { await model.loadLibrary() }
        await fulfillment(of: [started], timeout: 3)
        try await model.deleteProject(project)
        let stale = Data("{\"jobs\":[{\"id\":\"\(jobID.uuidString)\",\"mode\":\"generative\",\"status\":\"ready\",\"created_at\":\"2026-09-10T10:00:00Z\"}]}".utf8)
        try XCTUnwrap(held).finish(200, stale)
        await loading.value
        XCTAssertEqual(libraryCalls, 2)
        XCTAssertTrue(model.libraryProjects.isEmpty)
        XCTAssertEqual(model.libraryState, .empty)
    }

    func testCreateDeduplicatesWhileRequestIsOutstandingAndRecoversAfterFailure() async throws {
        let started = expectation(description: "Create request suspended")
        var held: ProjectActionDeferredProtocol?
        var requests = 0
        ProjectActionDeferredProtocol.handler = { transport in
            if transport.request.url?.path == "/creation-threads/capabilities" {
                transport.finish(200, Data(#"{"formats":[],"runtime_versions":[1]}"#.utf8))
                return
            }
            requests += 1; held = transport
            if requests == 1 { started.fulfill() }
            else { transport.finish(200, self.response(PreviewFixtures.projectID)) }
        }
        let model = AppModel(api: deferredAPI())
        let creating = Task { await model.createProject() }
        await fulfillment(of: [started], timeout: 3)
        XCTAssertTrue(model.isCreatingProject)
        await model.createProject()
        XCTAssertEqual(requests, 1)
        try XCTUnwrap(held).finish(500, Data())
        await creating.value
        XCTAssertFalse(model.isCreatingProject)
        XCTAssertTrue(model.projects.isEmpty)
        XCTAssertNil(model.selectedProject)
        XCTAssertNotNil(model.errorMessage)
        await model.createProject()
        XCTAssertEqual(requests, 2)
        XCTAssertEqual(model.projects.count, 1)
        XCTAssertEqual(model.selectedProject?.id, PreviewFixtures.projectID)
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(model.isCreatingProject)
    }

    func testOutstandingListCannotEraseSuccessfullyCreatedProject() async throws {
        let started = expectation(description: "Project list suspended")
        var held: ProjectActionDeferredProtocol?
        ProjectActionDeferredProtocol.handler = { transport in
            if transport.request.url?.path == "/creation-threads/capabilities" {
                transport.finish(200, Data(#"{"formats":[],"runtime_versions":[1]}"#.utf8))
                return
            }
            if transport.request.httpMethod == "GET" {
                held = transport; started.fulfill()
            } else {
                transport.finish(200, self.response(PreviewFixtures.projectID, title: "New chat"))
            }
        }
        let model = AppModel(api: deferredAPI())
        let loading = Task { await model.loadProjects() }
        await fulfillment(of: [started], timeout: 3)
        await model.createProject()
        XCTAssertEqual(model.selectedProject?.id, PreviewFixtures.projectID)
        try XCTUnwrap(held).finish(200, Data("[]".utf8))
        await loading.value
        XCTAssertEqual(model.projects.map(\.id), [PreviewFixtures.projectID])
        XCTAssertEqual(model.selectedProject?.title, "New chat")
        XCTAssertEqual(model.projectsState, .loaded)
        XCTAssertFalse(model.isLoading)
    }

    func testProjectListCannotReplaceNewerRenamedRevision() async throws {
        let project = PreviewFixtures.projects[0]
        let renamed = response(project.id, title: "Renamed on this device", revision: 7)
        let staleList = Data("[".utf8) + response(project.id, title: "Old title", revision: 2) + Data("]".utf8)
        NativeEditorURLProtocol.handler = { request in
            request.httpMethod == "PATCH" ? (200, renamed) : (200, staleList)
        }
        let container = try ModelContainer(for: CachedProject.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)
        model.projects = [project]; model.selectedProject = project
        try await model.renameProject(project, title: "Renamed on this device", clientEventID: "rename-before-list")
        await model.loadProjects()
        XCTAssertEqual(model.projects.first?.title, "Renamed on this device")
        XCTAssertEqual(model.selectedProject?.serverRevision, 7)
        XCTAssertEqual(try cache.projects().first?.serverRevision, 7)
        XCTAssertEqual(try cache.projects().first?.title, "Renamed on this device")
    }

    private func deferredAPI() -> KriaAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ProjectActionDeferredProtocol.self]
        return KriaAPI(baseURL: URL(string: "https://project-actions.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
    }

}


/// Leaves selected requests outstanding without blocking URLSession's callback
/// queue, so a mutation can finish before an earlier read is released.
private final class ProjectActionDeferredProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (@MainActor (ProjectActionDeferredProtocol) -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let callback = Callback(transport: self)
        DispatchQueue.main.async { ProjectActionDeferredProtocol.handler?(callback.transport) }
    }
    private struct Callback: @unchecked Sendable { let transport: ProjectActionDeferredProtocol }
    override func stopLoading() {}
    @MainActor func finish(_ status: Int, _ data: Data) {
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}
