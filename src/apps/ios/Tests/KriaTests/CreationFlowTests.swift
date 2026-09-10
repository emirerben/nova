import XCTest
import UIKit
@testable import Kria

@MainActor final class CreationFlowTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testAllFormatPostersDecodeFromTheApplicationBundle() throws {
        for format in CreationFormat.allCases {
            let url = try XCTUnwrap(Bundle.main.url(forResource: format.imageName, withExtension: "jpg"))
            let image = try XCTUnwrap(UIImage(contentsOfFile: url.path))
            XCTAssertGreaterThan(image.size.width, 100)
            XCTAssertGreaterThan(image.size.height, 100)
        }
    }

    func testNewChatUsesAdvertisedRuntimeAndOlderServersDefaultToV1() async throws {
        for advertised in [[], [1], [1, 2]] {
            let runtime = advertised.contains(2) ? 2 : 1
            NativeEditorURLProtocol.handler = { request in
                if request.url?.path == "/creation-threads/capabilities" {
                    var capabilities: [String: Any] = ["formats": []]
                    if !advertised.isEmpty { capabilities["runtime_versions"] = advertised }
                    return (200, try JSONSerialization.data(withJSONObject: capabilities))
                }
                XCTAssertEqual(request.url?.path, "/creation-threads")
                let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
                XCTAssertEqual(body["runtime_version"] as? Int, runtime)
                return (201, Self.thread(runtime: runtime))
            }
            let thread = try await NativeEditorTestSupport.api().createThread(message: nil)
            XCTAssertEqual(thread.runtimeVersion, runtime)
        }
    }

    func testLegacyMessagesAndRetryableActionsPreserveCallerIdentity() async throws {
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["expected_revision"] as? Int, 4)
            if request.url?.path.hasSuffix("/messages") == true {
                XCTAssertEqual(body["message"] as? String, "Keep the laugh")
                XCTAssertEqual(body["client_event_id"] as? String, "message-id")
            } else {
                XCTAssertEqual(body["client_action_id"] as? String, "action-id")
                XCTAssertEqual(body["action"] as? String, "generate")
            }
            return (200, Self.thread(runtime: 1))
        }
        let api = NativeEditorTestSupport.api()
        _ = try await api.sendCreationMessage(threadID: PreviewFixtures.projectID, message: "Keep the laugh", expectedRevision: 4, clientEventID: "message-id")
        _ = try await api.creationAction(threadID: PreviewFixtures.projectID, action: "generate", payload: [:], expectedRevision: 4, clientActionID: "action-id")
        let identity = CreationActionIdentity.reusing(nil, action: "generate", payload: [:], revision: 4)
        XCTAssertEqual(CreationActionIdentity.reusing(identity, action: "generate", payload: [:], revision: 5), identity)
        XCTAssertNotEqual(CreationActionIdentity.reusing(identity, action: "select_format", payload: [:], revision: 5).id, identity.id)
    }

    func testAudioAttachmentUsesAudioKindWithoutChangingClipContract() async throws {
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            let media = try XCTUnwrap((body["media"] as? [[String: Any]])?.first)
            XCTAssertEqual(media["kind"] as? String, "audio")
            XCTAssertEqual(media["content_type"] as? String, "audio/mp4")
            return (200, Self.thread(runtime: 1))
        }
        _ = try await NativeEditorTestSupport.api().attachProjectMedia(threadID: PreviewFixtures.projectID, mediaID: "audio-1", gcsPath: "users/u/audio.m4a", filename: "voiceover.m4a", contentType: "audio/mp4", expectedRevision: 0, clientEventID: "attach-audio")
    }

    func testVisualReservationAndRegistrationUseTheAssetPool() async throws {
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            if request.url?.path.hasSuffix("upload-urls") == true {
                XCTAssertEqual(request.url?.path, "/plan-items/item-1/assets/upload-urls")
                let file = try XCTUnwrap((body["files"] as? [[String: Any]])?.first)
                XCTAssertEqual(file["client_upload_id"] as? String, "upload-1")
                return (200, Data(#"{"urls":[{"reservation_id":"reservation-1","upload_url":"https://storage.test/put","gcs_path":"pool/photo.jpg","upload_headers":{"x-goog-if-generation-match":"0"}}]}"#.utf8))
            }
            XCTAssertEqual(request.url?.path, "/plan-items/item-1/assets")
            XCTAssertEqual(body["reservation_id"] as? String, "reservation-1")
            return (200, Data(#"{"id":"asset-1","kind":"image","status":"ready","source_filename":"photo.jpg","display_url":null,"preview_url":null,"retryable":false}"#.utf8))
        }
        let api = NativeEditorTestSupport.api()
        let target = try await api.reserveVisualUpload(itemID: "item-1", clientUploadID: "upload-1", filename: "photo.jpg", contentType: "image/jpeg", size: 123)
        XCTAssertEqual(target.uploadHeaders["x-goog-if-generation-match"], "0")
        let visual = try await api.registerVisual(itemID: "item-1", reservationID: target.reservationID, gcsPath: target.gcsPath, contentType: "image/jpeg", filename: "photo.jpg")
        XCTAssertEqual(visual.id, "asset-1")
    }

    func testMediaLimitsAndRolesRemainSeparate() throws {
        let limit = try JSONDecoder().decode(CreationMediaLimit.self, from: Data(#"{"max":10,"max_file_bytes":{"image":25,"video":512},"content_types":["image/jpeg","video/mp4"]}"#.utf8))
        XCTAssertEqual(limit.byteLimit(contentType: "image/jpeg"), 25)
        XCTAssertEqual(limit.byteLimit(contentType: "video/mp4"), 512)
        XCTAssertFalse(CreationMediaRole.clip.accepts("image/jpeg"))
        XCTAssertFalse(CreationMediaRole.voiceover.accepts("video/mp4"))
        XCTAssertTrue(CreationMediaRole.visual.accepts("video/mp4"))
    }

    func testLegacyUploadRecoveryDefaultsToClipAndTypedRolesSurviveRelaunch() throws {
        let json = #"{"id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","projectID":"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb","localFilePath":"/tmp/file.mov","filename":"file.mov","source":"files","purpose":"cloud_render_source","taskIdentifier":1,"retryCount":0}"#
        var record = try JSONDecoder().decode(UploadRecoveryRecord.self, from: Data(json.utf8))
        XCTAssertEqual(record.role, .clip)
        record.mediaRole = .visual; record.itemID = "item-1"; record.visualReservationID = "reservation-1"
        let restored = try JSONDecoder().decode(UploadRecoveryRecord.self, from: JSONEncoder().encode(record))
        XCTAssertEqual(restored.role, .visual)
        XCTAssertEqual(restored.visualReservationID, "reservation-1")
    }

    func testPreJobGenerationAndFailureKeepTheDirectionStage() throws {
        for (creator, generation, expected) in [
            ("executing", "preparing", ProjectStatus.rendering),
            ("failed", "failed", ProjectStatus.failed),
            ("awaiting_confirmation", "", ProjectStatus.draft),
            ("awaiting_confirmation", "failed", ProjectStatus.draft),
            ("executing", "failed", ProjectStatus.rendering)
        ] {
            var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Self.thread(runtime: 1)) as? [String: Any])
            object["creator_agent"] = ["status": creator, "summary": "Summer in Madrid"]
            object["state"] = ["generation": ["status": generation]]
            let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
            let thread = try decoder.decode(CreationThread.self, from: JSONSerialization.data(withJSONObject: object))
            XCTAssertNil(thread.activeJobID)
            XCTAssertEqual(thread.summary.status, expected)
            XCTAssertEqual(thread.creatorAgent?["summary"]?.stringValue, "Summer in Madrid")
        }
    }

    func testOlderPollCannotReplaceEqualRevisionMutationProjection() {
        var order = ThreadProjectionOrder()
        let oldPoll = order.begin()
        let action = order.begin()
        XCTAssertTrue(order.accept(action))
        XCTAssertFalse(order.accept(oldPoll))
        let inFlightPoll = order.begin()
        XCTAssertTrue(order.accept(nil)) // attachment response invalidates older reads
        XCTAssertFalse(order.accept(inFlightPoll))
        let newest = order.begin()
        XCTAssertTrue(order.accept(newest))
    }

    private static func thread(runtime: Int) -> Data {
        Data("""
        {"id":"\(PreviewFixtures.projectID.uuidString)","title":"Test","status":"active","revision":4,"runtime_version":\(runtime),"updated_at":"2026-09-10T10:00:00Z","state":{},"events":[]}
        """.utf8)
    }
}
