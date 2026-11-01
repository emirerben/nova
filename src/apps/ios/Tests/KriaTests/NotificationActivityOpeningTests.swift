import XCTest
@testable import Kria

@MainActor final class NotificationActivityOpeningTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private func api(owner: String = "activity-owner") -> KriaAPI {
        let payload = Data("{\"sub\":\"\(owner)\"}".utf8).base64EncodedString()
        return NativeEditorTestSupport.api(tokenStore: NativeEditorMemoryTokenStore(MobileSession(accessToken: "fixture.\(payload).signature", refreshToken: owner, expiresIn: 3600)))
    }

    func testFreshActivityProjectAndSuccessfulAcknowledgementPrecedeSelection() async throws {
        let id = UUID(); let project = UUID()
        let calls = NotificationActivityCalls()
        NativeEditorURLProtocol.handler = { request in
            calls.append(request.url!.path)
            if request.url!.path.contains("creation-threads") {
                return (200, Data("{\"id\":\"\(project)\",\"title\":\"Fresh project\",\"status\":\"draft\",\"revision\":2,\"runtime_version\":2,\"updated_at\":\"2026-09-10T10:00:00Z\"}".utf8))
            }
            return (200, Data("{\"id\":\"\(id)\",\"thread_id\":\"\(project)\",\"kind\":\"video_ready\",\"created_at\":\"2026-09-10T10:00:00Z\",\"read_at\":null}".utf8))
        }
        let model = AppModel(api: api())
        let opened = try await model.openNativeActivity(id: id, expectedOwner: model.api.mediaLibraryOwner)
        XCTAssertTrue(opened)
        XCTAssertEqual(model.selectedProject?.id, project)
        XCTAssertEqual(calls.values, ["/me/activities/\(id)", "/creation-threads/\(project)", "/me/activities/\(id)/read"])
    }

    func testReadAcknowledgement401NeverReplaysUnderSwitchedAccount() async throws {
        let id = UUID(); let project = UUID()
        let payloadA = Data("{\"sub\":\"activity-a\"}".utf8).base64EncodedString()
        let payloadB = Data("{\"sub\":\"activity-b\"}".utf8).base64EncodedString()
        let first = MobileSession(accessToken: "fixture.\(payloadA).sig", refreshToken: "activity-a", expiresIn: 3600)
        let next = MobileSession(accessToken: "fixture.\(payloadB).sig", refreshToken: "activity-b", expiresIn: 3600)
        let store = NativeEditorMemoryTokenStore(first)
        let calls = NotificationActivityCalls()
        let refreshBody = try JSONEncoder().encode(first)
        NativeEditorURLProtocol.handler = { request in
            let path = request.url!.path
            if path == "/auth/mobile/refresh" { try store.write(next); return (200, refreshBody) }
            if path.hasSuffix("/read") {
                calls.append(request.value(forHTTPHeaderField: "Authorization") ?? "missing")
                return (401, Data())
            }
            if path.contains("creation-threads") {
                return (200, Data("{\"id\":\"\(project)\",\"title\":\"Fresh\",\"status\":\"draft\",\"revision\":2,\"runtime_version\":2,\"updated_at\":\"2026-09-10T10:00:00Z\"}".utf8))
            }
            return (200, Data("{\"id\":\"\(id)\",\"thread_id\":\"\(project)\",\"kind\":\"video_ready\",\"created_at\":\"2026-09-10T10:00:00Z\"}".utf8))
        }
        let model = AppModel(api: NativeEditorTestSupport.api(tokenStore: store))
        let owner = try XCTUnwrap(model.api.mediaLibraryOwner)
        do { _ = try await model.openNativeActivity(id: id, expectedOwner: owner); XCTFail("Expected account boundary rejection") } catch {}
        XCTAssertEqual(calls.values, ["Bearer \(first.accessToken)"])
        XCTAssertEqual(try store.read(), next)
        XCTAssertNil(model.selectedProject)
    }

    func testFailedFreshFetchOrAcknowledgementNeverUsesCachedProject() async throws {
        for failure in ["activity", "project", "read"] {
            let id = UUID(); let project = PreviewFixtures.projects[0]
            NativeEditorURLProtocol.handler = { request in
                let path = request.url!.path
                if failure == "activity", path == "/me/activities/\(id)" { return (404, Data()) }
                if failure == "project", path.contains("creation-threads") { return (404, Data()) }
                if failure == "read", path.hasSuffix("/read") { return (503, Data()) }
                if path.contains("creation-threads") {
                    return (200, Data("{\"id\":\"\(project.id)\",\"title\":\"Fresh\",\"status\":\"draft\",\"revision\":2,\"runtime_version\":2,\"updated_at\":\"2026-09-10T10:00:00Z\"}".utf8))
                }
                return (200, Data("{\"id\":\"\(id)\",\"thread_id\":\"\(project.id)\",\"kind\":\"video_ready\",\"created_at\":\"2026-09-10T10:00:00Z\"}".utf8))
            }
            let model = AppModel(api: api())
            model.projects = [project]
            do { _ = try await model.openNativeActivity(id: id, expectedOwner: model.api.mediaLibraryOwner); XCTFail("Expected \(failure) failure") } catch {}
            XCTAssertNil(model.selectedProject)
            XCTAssertNil(model.destination)
        }
    }
}

private final class NotificationActivityCalls: @unchecked Sendable {
    private let lock = NSLock()
    private var paths: [String] = []
    func append(_ path: String) { lock.withLock { paths.append(path) } }
    var values: [String] { lock.withLock { paths } }
}
