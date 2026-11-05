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

    // Gallery editor "Review in chat" (round 3). Failure modes: the button only goes back to
    // the video detail (no chat, no render card); an unknown project is never fetched; an
    // unknown conversation leads nowhere instead of the projects list.
    func testOpenCreationThreadSelectsAKnownProjectAndAsksForTheChat() async {
        let project = PreviewFixtures.projects[1]
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.projects = PreviewFixtures.projects; model.selectedProject = PreviewFixtures.projects[0]
        await model.openCreationThread(project.id)
        XCTAssertEqual(model.selectedProject?.id, project.id)
        XCTAssertEqual(model.chatRouteRequest?.threadID, project.id)
    }

    func testOpenCreationThreadFetchesAProjectThisPhoneHasNotListed() async {
        let id = UUID()
        let fetched = response(id, title: "From the gallery")
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads/\(id.uuidString)")
            return (200, fetched)
        }
        let model = AppModel(api: NativeEditorTestSupport.api())
        model.projects = [PreviewFixtures.projects[0]]
        await model.openCreationThread(id)
        XCTAssertEqual(model.selectedProject?.id, id)
        XCTAssertTrue(model.projects.contains { $0.id == id })
        XCTAssertEqual(model.chatRouteRequest?.threadID, id)
    }

    func testOpenCreationThreadWithoutAKnownThreadOpensTheProjectsList() async {
        NativeEditorURLProtocol.handler = { _ in (404, Data()) }
        let model = AppModel(api: NativeEditorTestSupport.api())
        await model.openCreationThread(nil)
        XCTAssertNotNil(model.chatRouteRequest)
        XCTAssertNil(model.chatRouteRequest?.threadID, "nil: the workspace opens the projects drawer")
        await model.openCreationThread(UUID())
        XCTAssertNil(model.chatRouteRequest?.threadID, "a thread that can't be loaded also falls back to the list")
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

    func testLibraryUsesSavedTitleAndKeepsSelectedPreviewMediaTogether() async throws {
        let jobID = UUID()
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/me/jobs")
            return (200, Data("""
            {"jobs":[{"id":"\(jobID)","mode":"generative","status":"ready",
            "title":"  A quiet Sunday  ","created_at":"2026-09-10T10:00:00Z",
            "poster_url":"https://media.example/selected-poster.jpg",
            "output_url":"https://media.example/selected-video.mp4",
            "output_variant_id":"original_text"}],"next_cursor":null}
            """.utf8))
        }

        let library = try await NativeEditorTestSupport.api().library()
        let video = try XCTUnwrap(library.first)

        XCTAssertEqual(video.id, jobID)
        XCTAssertEqual(video.title, "A quiet Sunday")
        XCTAssertEqual(video.status, .ready)
        XCTAssertEqual(video.posterURL?.absoluteString, "https://media.example/selected-poster.jpg")
        XCTAssertEqual(video.outputURL?.absoluteString, "https://media.example/selected-video.mp4")
        XCTAssertEqual(video.outputVariantID, "original_text")
    }

    func testLibraryMissingNullAndBlankTitlesStayTruthfulForLegacyResponses() async throws {
        for titleField in ["", #""title":null,"#, #""title":"  \n  ","#] {
            NativeEditorURLProtocol.handler = { _ in
                (200, Data("""
                {"jobs":[{"id":"\(UUID())","mode":"content_plan","status":"failed",
                \(titleField)"poster_url":null,"created_at":"2026-09-10T10:00:00Z"}]}
                """.utf8))
            }

            let library = try await NativeEditorTestSupport.api().library()
            let video = try XCTUnwrap(library.first)
            XCTAssertEqual(video.title, "Untitled video")
            XCTAssertEqual(video.status, .failed)
            XCTAssertNil(video.posterURL)
            XCTAssertNil(video.outputURL)
            XCTAssertNil(video.outputVariantID)
        }
    }

    func testLibraryLoadsAllCursorPagesAndDeduplicatesJobs() async throws {
        let ids = (0...60).map { _ in UUID() }
        let cursor = "2026-09-10T10:00:00+00:00"
        let stub = LibraryPaginationStub(ids: ids, cursor: cursor)
        NativeEditorURLProtocol.handler = { try stub.response(for: $0) }

        let library = try await NativeEditorTestSupport.api().library()

        XCTAssertEqual(stub.requestCount, 2)
        XCTAssertEqual(stub.firstRequestLimit, "60")
        XCTAssertEqual(stub.secondRequestCursor, cursor)
        XCTAssertEqual(stub.secondRequestPercentEncodedQuery, "limit=60&cursor=2026-09-10T10:00:00%2B00:00")
        XCTAssertEqual(library.map(\.id), ids)
    }

    func testLibraryRejectsRepeatedCursorRatherThanReturningPartialResults() async {
        let cursor = "2026-09-10T10:00:00+00:00"
        NativeEditorURLProtocol.handler = { _ in
            (200, Self.libraryResponse(ids: [UUID()], nextCursor: cursor))
        }

        do {
            _ = try await NativeEditorTestSupport.api().library()
            XCTFail("Expected repeated cursor to fail")
        } catch {
            XCTAssertEqual(error as? APIError, .invalidResponse)
        }
    }

    func testLibraryTransientFailurePreservesExistingLibraryWithoutPreviewFixtures() async {
        let existingID = UUID()
        var calls = 0
        NativeEditorURLProtocol.handler = { _ in
            calls += 1
            return calls == 1
                ? (200, Self.libraryResponse(ids: [existingID], nextCursor: nil))
                : (500, Data())
        }
        let model = AppModel(api: NativeEditorTestSupport.api())
        await model.loadLibrary()
        let existing = try! XCTUnwrap(model.libraryProjects.first)

        await model.loadLibrary()

        XCTAssertEqual(model.libraryProjects, [existing])
        XCTAssertEqual(model.libraryState, .loaded)
        XCTAssertNotNil(model.errorMessage)
    }

    func testInitialLibraryFailureDoesNotInventPreviewProjects() async {
        NativeEditorURLProtocol.handler = { _ in (500, Data()) }
        let model = AppModel(api: NativeEditorTestSupport.api())

        await model.loadLibrary()

        XCTAssertTrue(model.libraryProjects.isEmpty)
        XCTAssertEqual(model.libraryState, .failed(APIError.requestFailed(status: 500).localizedDescription))
        XCTAssertNotNil(model.errorMessage)
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

    /// Failure modes: (1) New chat stayed dead while the first list loaded; (2) once enabled, the workspace
    /// opening that list put the creator back on the remembered chat; (3) the launch sweep deleted the empty
    /// chat they had just opened.
    func testNewChatDuringWorkspaceOpenStaysSelectedAndIsNotSwept() async throws {
        let rememberedID = UUID(), createdID = PreviewFixtures.projectID
        let listRequested = expectation(description: "Project list suspended")
        var heldList: ProjectActionDeferredProtocol?
        var threadReads: [String] = []
        ProjectActionDeferredProtocol.handler = { transport in
            let path = transport.request.url?.path ?? ""
            if path == "/creation-threads/capabilities" {
                transport.finish(200, Data(#"{"formats":[],"runtime_versions":[1]}"#.utf8))
            } else if transport.request.httpMethod == "POST" {
                transport.finish(200, self.response(createdID, title: "New chat"))
            } else if path == "/creation-threads" {
                heldList = transport; listRequested.fulfill()
            } else {
                threadReads.append(path)
                transport.finish(404, Data())
            }
        }
        // A launch normally has the cached list: the remembered chat is there, and the list the creator
        // outran is dropped, so the remembered chat is what the workspace would restore.
        let container = try ModelContainer(for: CachedProject.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        try cache.upsert([ProjectSummary(id: rememberedID, title: "Remembered", status: .draft, updatedAt: .distantPast, posterURL: nil)])
        let model = AppModel(api: deferredAPI(), cache: cache)
        let opening = Task { await model.openWorkspace(preferredProjectID: rememberedID) }
        await fulfillment(of: [listRequested], timeout: 3)
        XCTAssertTrue(model.isLoading, "setup: the list is still loading")

        await model.createProject()
        XCTAssertEqual(model.selectedProject?.id, createdID)

        let list = Data("[".utf8) + response(createdID, title: "New chat") + Data(",".utf8) + response(rememberedID, title: "Remembered") + Data("]".utf8)
        try XCTUnwrap(heldList).finish(200, list)
        await opening.value
        XCTAssertEqual(model.selectedProject?.id, createdID, "A list that finishes later must not pull the creator off the chat they opened")

        await model.sweepAbandonedChats()
        XCTAssertFalse(threadReads.contains("/creation-threads/\(createdID.uuidString)"), "The launch sweep is for chats a killed app left behind, not the one just opened")
    }

    private func heldCreateHandler(held: @escaping @MainActor (ProjectActionDeferredProtocol) -> Void) {
        ProjectActionDeferredProtocol.handler = { transport in
            let path = transport.request.url?.path ?? ""
            if path == "/creation-threads/capabilities" { transport.finish(200, Data(#"{"formats":[],"runtime_versions":[1]}"#.utf8)) }
            else if transport.request.httpMethod == "POST" { held(transport) }
            else { transport.finish(404, Data()) }
        }
    }

    /// Failure modes: the tap showed nothing until the POST returned; a second tap made a second chat; the
    /// pending conversation was selectable as if it had a thread id.
    func testNewChatShowsTheConversationBeforeTheServerReplies() async throws {
        var posts: [ProjectActionDeferredProtocol] = []
        let posted = expectation(description: "create posted")
        heldCreateHandler { posts.append($0); posted.fulfill() }
        let existing = PreviewFixtures.projects[0]
        let model = AppModel(api: deferredAPI())
        model.selectProject(existing)

        model.startNewChat()
        XCTAssertNotNil(model.pendingNewChat, "The new conversation is on screen before any network answer")
        XCTAssertNil(model.pendingNewChat?.failure)
        XCTAssertEqual(model.selectedProject?.id, existing.id, "No thread id exists yet, so nothing is selected")
        XCTAssertTrue(model.isCreatingProject)
        model.startNewChat()
        await fulfillment(of: [posted], timeout: 3)
        XCTAssertEqual(posts.count, 1, "Repeated taps are one chat")

        posts[0].finish(200, response(PreviewFixtures.projectID, title: "New chat"))
        for _ in 0..<200 where model.pendingNewChat != nil { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertNil(model.pendingNewChat)
        XCTAssertEqual(model.selectedProject?.id, PreviewFixtures.projectID)
        XCTAssertNotNil(model.takeCreatedThread(PreviewFixtures.projectID), "The chat opens on the create reply, with no read-back")
    }

    /// Failure modes: a failed create vanished (the drawer was already closed), or could not be retried.
    func testFailedNewChatStaysVisibleAndRetries() async throws {
        var posts: [ProjectActionDeferredProtocol] = []
        var arrived = 0
        heldCreateHandler { posts.append($0); arrived += 1 }
        let existing = PreviewFixtures.projects[0]
        let model = AppModel(api: deferredAPI())
        model.selectProject(existing)

        model.startNewChat()
        for _ in 0..<300 where arrived < 1 { try await Task.sleep(for: .milliseconds(10)) }
        posts[0].finish(500, Data())
        for _ in 0..<300 where model.pendingNewChat?.failure == nil { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertNotNil(model.pendingNewChat?.failure, "The failure shows on the pending conversation")
        XCTAssertFalse(model.isCreatingProject)
        XCTAssertEqual(model.selectedProject?.id, existing.id)

        model.startNewChat()
        XCTAssertNil(model.pendingNewChat?.failure, "Try again returns to the starting state")
        for _ in 0..<300 where arrived < 2 { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(arrived, 2)
        posts[1].finish(200, response(PreviewFixtures.projectID, title: "New chat"))
        for _ in 0..<300 where model.pendingNewChat != nil { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(model.selectedProject?.id, PreviewFixtures.projectID)

        model.startNewChat()
        for _ in 0..<300 where arrived < 3 { try await Task.sleep(for: .milliseconds(10)) }
        posts[2].finish(500, Data())
        for _ in 0..<300 where model.pendingNewChat?.failure == nil { try await Task.sleep(for: .milliseconds(10)) }
        model.cancelPendingNewChat()
        XCTAssertNil(model.pendingNewChat, "Back to my chats leaves the failure")
        XCTAssertEqual(model.selectedProject?.id, PreviewFixtures.projectID)
    }

    /// Failure mode: opening another chat while the new one was being made was undone when the create replied.
    func testNewChatDoesNotPullTheCreatorBackAfterTheyOpenAnotherChat() async throws {
        let createdID = UUID()
        var posts: [ProjectActionDeferredProtocol] = []
        var arrived = 0
        heldCreateHandler { posts.append($0); arrived += 1 }
        let existing = PreviewFixtures.projects[0]
        let model = AppModel(api: deferredAPI())
        model.selectProject(existing)
        model.startNewChat()
        for _ in 0..<300 where arrived < 1 { try await Task.sleep(for: .milliseconds(10)) }

        XCTAssertTrue(model.selectProject(existing))
        XCTAssertNil(model.pendingNewChat, "Choosing a chat leaves the pending one")
        posts[0].finish(200, response(createdID, title: "New chat"))
        for _ in 0..<300 where model.isCreatingProject { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(model.selectedProject?.id, existing.id, "Their latest choice stands")
        XCTAssertNil(model.takeCreatedThread(createdID))
    }

    /// SYNTHETIC, not a device measurement: the transport answers the create call after an injected 500 ms.
    /// It shows WHEN a conversation surface exists relative to that reply; it says nothing about real network
    /// or server time, and no live speedup follows from it.
    func testSyntheticNewChatSurfaceDoesNotWaitForTheInjectedCreateDelay() async throws {
        let delayMs = 500
        var created = false
        ProjectActionDeferredProtocol.handler = { transport in
            let path = transport.request.url?.path ?? ""
            if path == "/creation-threads/capabilities" { transport.finish(200, Data(#"{"formats":[],"runtime_versions":[1]}"#.utf8)) }
            else if transport.request.httpMethod == "POST" {
                DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(delayMs)) {
                    created = true
                    transport.finish(200, self.response(PreviewFixtures.projectID, title: "New chat"))
                }
            } else { transport.finish(404, Data()) }
        }
        let model = AppModel(api: deferredAPI())
        let clock = ContinuousClock()
        let start = clock.now
        model.startNewChat()
        let surfaceMs = Int((clock.now - start).components.attoseconds / 1_000_000_000_000_000)
        XCTAssertNotNil(model.pendingNewChat)
        XCTAssertFalse(created, "setup: the delayed reply has not arrived when the surface exists")
        try await waitFor { model.pendingNewChat == nil }
        let realDuration = clock.now - start
        let realMs = Int(realDuration.components.seconds * 1000) + Int(realDuration.components.attoseconds / 1_000_000_000_000_000)
        print("SYNTHETIC new chat: injected create reply delay \(delayMs) ms; pending conversation present after \(surfaceMs) ms; real thread selected after \(realMs) ms")
        XCTAssertTrue(created, "the create call really ran")
        XCTAssertEqual(model.selectedProject?.id, PreviewFixtures.projectID)
        XCTAssertGreaterThanOrEqual(realMs, delayMs)
        XCTAssertLessThan(surfaceMs, 100)
    }

    private func isolatedDrafts() -> ChatDraftStore {
        ChatDraftStore(defaults: UserDefaults(suiteName: "kria-test-drafts-\(UUID().uuidString)")!)
    }

    private func waitFor(_ condition: @MainActor () -> Bool) async throws {
        for _ in 0..<500 where !condition() { try await Task.sleep(for: .milliseconds(10)) }
    }

    /// Failure modes: typing during the wait was impossible; the typed text leaked into the PRIOR chat's
    /// draft, replaced it, or was lost when the create succeeded.
    func testTextTypedWhileTheChatIsBeingMadeMovesToTheNewChatOnly() async throws {
        let createdID = UUID()
        var posts: [ProjectActionDeferredProtocol] = []
        heldCreateHandler { posts.append($0) }
        let existing = PreviewFixtures.projects[0]
        let model = AppModel(api: deferredAPI(), chatDrafts: isolatedDrafts())
        model.selectProject(existing)
        model.chatDrafts.setDraft("prior unsent words", for: existing.id)

        model.startNewChat()
        model.setPendingNewChatDraft("typed during the wait")
        XCTAssertEqual(model.pendingNewChat?.draft, "typed during the wait")
        XCTAssertEqual(model.chatDrafts.draft(for: existing.id), "prior unsent words", "The pending chat never writes into another conversation")
        try await waitFor { posts.count == 1 }

        posts[0].finish(200, response(createdID, title: "New chat"))
        try await waitFor { model.pendingNewChat == nil }
        XCTAssertEqual(model.selectedProject?.id, createdID)
        XCTAssertEqual(model.chatDrafts.draft(for: createdID), "typed during the wait")
        XCTAssertEqual(model.chatDrafts.draft(for: existing.id), "prior unsent words")
    }

    /// Failure mode: a failed create threw away what the creator had typed, or the retry started empty.
    func testTextSurvivesAFailedCreateAndItsRetry() async throws {
        let createdID = UUID()
        var posts: [ProjectActionDeferredProtocol] = []
        heldCreateHandler { posts.append($0) }
        let model = AppModel(api: deferredAPI(), chatDrafts: isolatedDrafts())
        model.startNewChat()
        model.setPendingNewChatDraft("first words")
        try await waitFor { posts.count == 1 }
        posts[0].finish(500, Data())
        try await waitFor { model.pendingNewChat?.failure != nil }
        XCTAssertEqual(model.pendingNewChat?.draft, "first words")
        model.setPendingNewChatDraft("first words, edited after the failure")

        model.startNewChat()
        XCTAssertEqual(model.pendingNewChat?.draft, "first words, edited after the failure", "Try again is the same conversation")
        try await waitFor { posts.count == 2 }
        posts[1].finish(200, response(createdID, title: "New chat"))
        try await waitFor { model.pendingNewChat == nil }
        XCTAssertEqual(model.chatDrafts.draft(for: createdID), "first words, edited after the failure")
    }

    /// Failure mode: opening another chat during the wait dropped the typed text with the pending chat.
    func testTextTypedBeforeOpeningAnotherChatIsKeptOnTheChatThatGetsMade() async throws {
        let createdID = UUID()
        var posts: [ProjectActionDeferredProtocol] = []
        heldCreateHandler { posts.append($0) }
        let existing = PreviewFixtures.projects[0]
        let model = AppModel(api: deferredAPI(), chatDrafts: isolatedDrafts())
        model.selectProject(existing)
        model.startNewChat()
        model.setPendingNewChatDraft("do not lose this")
        try await waitFor { posts.count == 1 }
        XCTAssertTrue(model.selectProject(existing))
        posts[0].finish(200, response(createdID, title: "New chat"))
        try await waitFor { !model.isCreatingProject }
        XCTAssertEqual(model.selectedProject?.id, existing.id, "Their latest navigation stands")
        XCTAssertEqual(model.chatDrafts.draft(for: createdID), "do not lose this")
        XCTAssertEqual(model.chatDrafts.draft(for: existing.id), "")
    }

    /// Failure modes: from the camera roll the pending conversation was hidden behind the camera roll;
    /// a failed create then left no way back.
    func testNewChatFromTheCameraRollLeavesItAndFailureReturnsToIt() async throws {
        var posts: [ProjectActionDeferredProtocol] = []
        heldCreateHandler { posts.append($0) }
        let model = AppModel(api: deferredAPI())
        model.openCameraRoll()
        XCTAssertTrue(model.showsCameraRoll)
        model.startNewChat()
        XCTAssertNotNil(model.pendingNewChat, "The view puts the pending chat ahead of the camera roll")
        try await waitFor { posts.count == 1 }
        posts[0].finish(500, Data())
        try await waitFor { model.pendingNewChat?.failure != nil }
        model.cancelPendingNewChat()
        XCTAssertTrue(model.showsCameraRoll, "Back lands on the camera roll the creator came from")

        model.startNewChat()
        try await waitFor { posts.count == 2 }
        posts[1].finish(200, response(PreviewFixtures.projectID, title: "New chat"))
        try await waitFor { model.pendingNewChat == nil }
        XCTAssertFalse(model.showsCameraRoll)
        XCTAssertEqual(model.destination, .chat(PreviewFixtures.projectID))
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

    nonisolated fileprivate static func libraryResponse(ids: [UUID], nextCursor: String?) -> Data {
        let jobs = ids.map {
            "{\"id\":\"\($0.uuidString)\",\"mode\":\"generative\",\"status\":\"ready\",\"created_at\":\"2026-09-10T10:00:00Z\"}"
        }.joined(separator: ",")
        let cursor = nextCursor.map { "\"\($0)\"" } ?? "null"
        return Data("{\"jobs\":[\(jobs)],\"next_cursor\":\(cursor)}".utf8)
    }

}

private final class LibraryPaginationStub: @unchecked Sendable {
    private let ids: [UUID]
    private let cursor: String
    private let lock = NSLock()
    private var requests: [URLRequest] = []

    init(ids: [UUID], cursor: String) {
        self.ids = ids
        self.cursor = cursor
    }

    func response(for request: URLRequest) throws -> (Int, Data) {
        lock.withLock { requests.append(request) }
        let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
        if query?.first(where: { $0.name == "cursor" })?.value == nil {
            return (200, ProjectActionsTests.libraryResponse(ids: Array(ids[0...59]), nextCursor: cursor))
        }
        return (200, ProjectActionsTests.libraryResponse(ids: [ids[59], ids[60]], nextCursor: nil))
    }

    var requestCount: Int { lock.withLock { requests.count } }
    var firstRequestLimit: String? {
        lock.withLock {
            URLComponents(url: requests.first?.url ?? URL(string: "https://invalid.test")!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "limit" })?.value
        }
    }
    var secondRequestCursor: String? {
        lock.withLock {
            URLComponents(url: requests.dropFirst().first?.url ?? URL(string: "https://invalid.test")!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "cursor" })?.value
        }
    }
    var secondRequestPercentEncodedQuery: String? {
        lock.withLock {
            URLComponents(url: requests.dropFirst().first?.url ?? URL(string: "https://invalid.test")!, resolvingAgainstBaseURL: false)?.percentEncodedQuery
        }
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
