import AVFoundation
import XCTest
@testable import Kria

/// KRI-1554: the picker refuses a recording longer than the chosen format takes, naming the limit, before it uploads.
///
/// Failure modes, written before these tests:
/// 1. The format's own limit is ignored and the role's default (30 minutes) is used, so a 6-minute Talking clip
///    still uploads and fails after approval.
/// 2. A format's limit leaks into another format: a montage clip gets the Talking limit.
/// 3. A server that predates `max_duration_s` makes the phone refuse everything, or crash on decode.
/// 4. The length of a real file cannot be read (wrong API, wrong units), so nothing is ever refused.
/// 5. A recording just under the limit is refused.
/// 6. A photo is length-checked.
/// 7. The message does not name the limit.
final class SourceLengthLimitTests: XCTestCase {
    private func capabilities() throws -> CreationCapabilities {
        let json = """
        {"formats": [
            {"id": "talking_to_camera", "edit_format": "subtitled", "max_clips": 10,
             "max_duration_s": {"clip": 300, "voiceover": 300, "song": 600, "visual_video": 1800}},
            {"id": "montage", "edit_format": "montage", "max_clips": 50,
             "max_duration_s": {"clip": 1800, "voiceover": 1800, "song": 600, "visual_video": 1800}},
            {"id": "slides", "edit_format": "slides", "max_clips": 50,
             "max_duration_s": {"clip": 1800, "voiceover": 1800, "song": 600, "visual_video": 90}},
            {"id": "narrated", "edit_format": "narrated_planned", "max_clips": 50}
         ],
         "media": {
            "clips": {"max": 50, "max_file_bytes": 4294967296, "max_duration_s": 1800, "content_types": ["video/mp4"]},
            "visuals": {"max": 30, "max_file_bytes": {"image": 1, "video": 2}, "max_duration_s": {"video": 1800},
                        "content_types": ["image/jpeg", "video/mp4"]},
            "voiceover": {"max": 1, "max_file_bytes": 209715200, "content_types": ["audio/mp4"]}
         }}
        """
        return try JSONDecoder().decode(CreationCapabilities.self, from: Data(json.utf8))
    }

    func testEachFormatGetsItsOwnLimit() throws {
        let caps = try capabilities()
        XCTAssertEqual(caps.mediaLimit(for: .clip, format: .talkingToCamera)?.durationLimit(isImage: false), 300)
        XCTAssertEqual(caps.mediaLimit(for: .clip, format: .montage)?.durationLimit(isImage: false), 1800)
        XCTAssertEqual(caps.mediaLimit(for: .clip, format: nil)?.durationLimit(isImage: false), 1800)
        XCTAssertEqual(caps.mediaLimit(for: .visual, format: .slides)?.durationLimit(isImage: false), 90)
        XCTAssertNil(caps.mediaLimit(for: .visual, format: .slides)?.durationLimit(isImage: true))
        // Everything else the role limit says is kept.
        XCTAssertEqual(caps.mediaLimit(for: .clip, format: .talkingToCamera)?.max, 50)
    }

    func testAnOlderServerChecksNothing() throws {
        let caps = try capabilities()
        // No `max_duration_s` on the narrated row or the voiceover role: no length check, no failure.
        XCTAssertNil(caps.mediaLimit(for: .voiceover, format: .narrated)?.durationLimit(isImage: false))
        XCTAssertNotNil(caps.mediaLimit(for: .voiceover, format: .narrated))
    }

    func testARealRecordingOverTheLimitIsRefusedNamingIt() async throws {
        let limit = try XCTUnwrap(capabilities().media?["clips"]).withDurationLimit(10, role: .voiceover)
        let long = try silentRecording(seconds: 12)
        let short = try silentRecording(seconds: 9.5)
        defer { try? FileManager.default.removeItem(at: long); try? FileManager.default.removeItem(at: short) }

        let measured = await CreationUploadError.pickedDurationSeconds(long)
        XCTAssertEqual(try XCTUnwrap(measured), 12, accuracy: 0.1)
        do {
            try await CreationUploadError.checkLength(of: long, role: .voiceover, limit: limit)
            XCTFail("a 12 s voiceover passed a 10 s limit")
        } catch let error as CreationUploadError {
            guard case .tooLong(let seconds, let role) = error else { return XCTFail("\(error)") }
            XCTAssertEqual(seconds, 10)
            XCTAssertEqual(role, .voiceover)
            XCTAssertTrue(error.localizedDescription.contains(CreationUploadError.lengthText(10)), error.localizedDescription)
        }
        try await CreationUploadError.checkLength(of: short, role: .voiceover, limit: limit)
        try await CreationUploadError.checkLength(of: long, role: .voiceover, limit: nil)
    }

    func testTheLimitIsNamedInWords() {
        XCTAssertTrue(CreationUploadError.tooLong(limitSeconds: 300, role: .clip).localizedDescription
            .contains(CreationUploadError.lengthText(300)))
        XCTAssertNotEqual(CreationUploadError.lengthText(300), CreationUploadError.lengthText(90))
    }

    private func silentRecording(seconds: Double) throws -> URL {
        let url = FileManager.default.temporaryDirectory.appending(path: "kri1554-\(UUID().uuidString).caf")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 8_000, channels: 1))
        let file = try AVAudioFile(forWriting: url, settings: format.settings)
        let frames = AVAudioFrameCount(seconds * format.sampleRate)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames))
        buffer.frameLength = frames
        try file.write(from: buffer)
        return url
    }
}
