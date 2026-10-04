import XCTest
@testable import Kria
import KriaMediaEngine

/// KRI-374: attaching a creator's song. The song always uploads in full bytes and is never routed through
/// the analysis-proxy contract or the voiceover-unavailable gate.
@MainActor final class SongCreationTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private let phoneFull = PhoneRenderingCapabilities(
        enabled: true, recipeVersions: [2],
        verifiedFeatures: ["basicComposition", "positionedText", "audioMix", "local1080Export", "musicBed"]
    )

    // MARK: roles

    func testSongRoleAcceptsOnlyAudioAndIsWireNamedSong() {
        XCTAssertEqual(CreationMediaRole.song.rawValue, "song")
        XCTAssertEqual(CreationMediaRole.song.capabilityKey, "song")
        XCTAssertTrue(CreationMediaRole.song.accepts("audio/mpeg"))
        XCTAssertTrue(CreationMediaRole.song.accepts("audio/mp4"))
        XCTAssertFalse(CreationMediaRole.song.accepts("video/mp4"))
        XCTAssertFalse(CreationMediaRole.song.accepts("image/png"))
        XCTAssertTrue(CreationMediaRole.song.isAudio); XCTAssertTrue(CreationMediaRole.voiceover.isAudio)
        XCTAssertFalse(CreationMediaRole.clip.isAudio)
    }

    // MARK: upload destination

    func testSongNeverRoutesToVoiceoverUnavailableOnPhone() {
        // `narrationAudio` is NOT verified here: a voiceover would be refused, a song must not be.
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: phoneFull, sourcePurposes: [], role: .voiceover), .voiceoverUnavailableOnPhone)
        for capabilities in [phoneFull, .disabled, nil] {
            let destination = ProjectUploadDestination.resolve(capabilities: capabilities, sourcePurposes: [], role: .song)
            XCTAssertEqual(destination, .cloud)
            XCTAssertTrue(destination.canUpload)
            XCTAssertNotEqual(destination, .voiceoverUnavailableOnPhone)
        }
    }

    func testSongWaitsForCapabilitiesButNeverForPhoneVerificationOrAFootageDestination() {
        let phone = UploadPurpose.analysisProxy.rawValue
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, capabilitiesLoaded: false, sourcePurposes: [], role: .song), .checking)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, capabilitiesLoaded: true, sourcePurposes: [], role: .song), .cloud)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: phoneFull, sourcePurposes: [phone, UploadPurpose.cloudRenderSource.rawValue], role: .song), .cloud,
                       "a mixed footage project must not stop the song")
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: phoneFull, sourcePurposes: [phone], role: .song), .cloud,
                       "an analysis-proxy footage project must not stop the song")
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: phoneFull, sourcePurposes: [phone], role: .clip), .phone, "footage is unchanged")
    }

    func testAnAttachedOrPendingSongNeverChangesTheProjectsFootageDestination() {
        let projectID = UUID()
        func record(_ role: CreationMediaRole, purpose: UploadPurpose) -> UploadRecoveryRecord {
            UploadRecoveryRecord(id: UUID(), projectID: projectID, localFilePath: "/tmp/x", filename: "x", source: .files, purpose: purpose,
                                 taskIdentifier: 1, retryCount: 0, mediaRole: role)
        }
        let song = record(.song, purpose: .cloudRenderSource)
        let clip = record(.clip, purpose: .analysisProxy)
        let attachedSong = CreationAttachedMedia(id: "song-1", filename: "s.m4a", kind: "audio", previewURL: nil, uploadPurpose: UploadPurpose.cloudRenderSource.rawValue, role: "song")
        let purposes = ProjectUploadDestination.sourcePurposes(media: [attachedSong], records: [song, clip], projectID: projectID)
        XCTAssertEqual(purposes, [clip.purpose.rawValue], "only the clip counts; a full-bytes song would otherwise make a phone project .mixed")
    }

    // MARK: capabilities

    func testCapabilitiesDecodeSongLimitAndOrderQuestions() throws {
        let json = Data(#"""
        {"formats":[],"media":{"clips":{"max":10,"max_file_bytes":1000,"content_types":["video/mp4"]},
          "song":{"max":1,"max_file_bytes":52428800,"content_types":["audio/mpeg","audio/mp4"]}},
         "song_order_questions":true}
        """#.utf8)
        let capabilities = try JSONDecoder().decode(CreationCapabilities.self, from: json)
        XCTAssertTrue(capabilities.songUploadEnabled)
        XCTAssertEqual(capabilities.songLimit?.max, 1)
        XCTAssertEqual(capabilities.songLimit?.contentTypes, ["audio/mpeg", "audio/mp4"])
        XCTAssertEqual(capabilities.songLimit?.byteLimit(contentType: "audio/mpeg"), 52_428_800)
        XCTAssertTrue(capabilities.songOrderQuestionsEnabled)
    }

    func testCapabilitiesWithoutSongHideEverySongSurface() throws {
        let old = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"media":{"clips":{"max":10,"max_file_bytes":1000,"content_types":["video/mp4"]}}}"#.utf8))
        XCTAssertFalse(old.songUploadEnabled); XCTAssertNil(old.songLimit); XCTAssertFalse(old.songOrderQuestionsEnabled)
        let none = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertFalse(none.songUploadEnabled); XCTAssertFalse(none.songOrderQuestionsEnabled)
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"song_order_questions":false}"#.utf8))
        XCTAssertFalse(off.songOrderQuestionsEnabled)
    }

    // MARK: attach request

    func testSongAttachSendsRoleSongAndAudioKind() async throws {
        var seen: [String: Any]?
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            seen = (body["media"] as? [[String: Any]])?.first
            return (200, Self.thread())
        }
        _ = try await NativeEditorTestSupport.api().attachProjectMedia(threadID: PreviewFixtures.projectID, mediaID: "song-1", gcsPath: "users/u/song.mp3", filename: "song.mp3", contentType: "audio/mpeg", expectedRevision: 3, clientEventID: "attach-song", role: .song)
        XCTAssertEqual(seen?["role"] as? String, "song")
        XCTAssertEqual(seen?["kind"] as? String, "audio")
        XCTAssertEqual(seen?["content_type"] as? String, "audio/mpeg")
        XCTAssertNil(seen?["capture_time"], "filming context belongs to footage")
    }

    func testVoiceoverAndFootageAttachBytesAreUnchanged() async throws {
        var roles: [Bool] = []
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            roles.append(((body["media"] as? [[String: Any]])?.first)?.keys.contains("role") == true)
            return (200, Self.thread())
        }
        let api = NativeEditorTestSupport.api()
        _ = try await api.attachProjectMedia(threadID: PreviewFixtures.projectID, mediaID: "v", gcsPath: "p", filename: "v.m4a", contentType: "audio/mp4", expectedRevision: 0, clientEventID: "a")
        _ = try await api.attachProjectMedia(threadID: PreviewFixtures.projectID, mediaID: "c", gcsPath: "p", filename: "c.mov", contentType: "video/quicktime", expectedRevision: 0, clientEventID: "b")
        XCTAssertEqual(roles, [false, false], "only a song carries a role on the wire")
    }

    func testSongAttachedMediaIsRecognizedAndSeparatedFromVoiceover() throws {
        let state: [String: JSONValue] = ["media": .array([
            .object(["media_id": .string("v"), "kind": .string("audio"), "filename": .string("v.m4a")]),
            .object(["media_id": .string("s"), "kind": .string("audio"), "role": .string("song"), "filename": .string("s.mp3"), "duration_s": .number(185)]),
        ])]
        let media = CreationAttachedMedia.parse(state)
        XCTAssertEqual(media.filter(\.isVoiceover).map(\.id), ["v"])
        XCTAssertEqual(CreationAttachedMedia.song(state)?.id, "s")
        XCTAssertEqual(CreationAttachedMedia.song(state)?.durationS, 185)
        XCTAssertNil(CreationAttachedMedia.song(["media": .array([])]))
        let topLevel: [String: JSONValue] = ["song": .object(["media_id": .string("s2"), "filename": .string("x.mp3")])]
        XCTAssertEqual(CreationAttachedMedia.song(topLevel)?.id, "s2")
        XCTAssertTrue(try XCTUnwrap(CreationAttachedMedia.song(topLevel)).isSong)
    }

    private static func thread() -> Data {
        Data("""
        {"id":"\(PreviewFixtures.projectID.uuidString)","title":"Test","status":"active","revision":4,"runtime_version":2,"updated_at":"2026-09-10T10:00:00Z","state":{},"events":[]}
        """.utf8)
    }
}
