import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-132: a library music/sound-effect bed or a recorded voiceover plays
/// from the verified cache file itself, under a name AVFoundation can open --
/// the audio counterpart of `VisualVideoFileTests`.
final class PlayableAudioFileTests: XCTestCase {
    private func directory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func fileNumber(_ url: URL) throws -> UInt64 {
        try XCTUnwrap((FileManager.default.attributesOfItem(atPath: url.path)[.systemFileNumber] as? NSNumber)?.uint64Value)
    }

    func testExtensionComesFromContainerBytes() throws {
        let root = try directory(); defer { try? FileManager.default.removeItem(at: root) }
        let cases: [(name: String, bytes: Data, expected: String?, throwsUnsupported: Bool)] = [
            ("m4a", Data([0, 0, 0, 0x18]) + Data("ftypM4A ".utf8) + Data(repeating: 0, count: 32), "m4a", false),
            ("mp4-audio", Data([0, 0, 0, 0x18]) + Data("ftypisom".utf8) + Data(repeating: 0, count: 32), "m4a", false),
            ("wav", Data("RIFF".utf8) + Data([0, 0, 0, 0]) + Data("WAVE".utf8) + Data(repeating: 0, count: 4), "wav", false),
            ("mp3-id3", Data("ID3".utf8) + Data(repeating: 0, count: 9), "mp3", false),
            ("mp3-frame-sync", Data([0xFF, 0xFB, 0x90, 0x64]) + Data(repeating: 0, count: 8), "mp3", false),
            ("aac-adts", Data([0xFF, 0xF1, 0x50, 0x80]) + Data(repeating: 0, count: 8), "aac", false),
            ("ogg", Data([0x4F, 0x67, 0x67, 0x53]) + Data(repeating: 0, count: 8), nil, true),
            ("webm", Data([0x1A, 0x45, 0xDF, 0xA3, 0x9F, 0x42, 0x86, 0x81]) + Data(repeating: 0, count: 4), nil, true),
            ("jpeg", Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01]), nil, false),
            ("text", Data("not audio at all".utf8), nil, false),
            ("short", Data([0xFF]), nil, false),
            ("empty", Data(), nil, false),
        ]
        for item in cases {
            // The cache names files `<sha256>-<bytes>`: there is no extension to trust.
            let file = root.appendingPathComponent(item.name)
            try item.bytes.write(to: file)
            if let expected = item.expected {
                XCTAssertEqual(try PlayableAudioFile.fileExtension(of: file), expected, item.name)
            } else if item.throwsUnsupported {
                XCTAssertThrowsError(try PlayableAudioFile.fileExtension(of: file), item.name) {
                    XCTAssertEqual($0 as? MediaEngineError, .unsupportedCapability, item.name)
                }
            } else {
                XCTAssertThrowsError(try PlayableAudioFile.fileExtension(of: file), item.name) {
                    XCTAssertEqual($0 as? MediaEngineError, .missingAsset(item.name), item.name)
                }
            }
        }
        XCTAssertThrowsError(try PlayableAudioFile.fileExtension(of: root.appendingPathComponent("missing")))
    }

    func testPrepareLinksTheVerifiedFileUnderAPlayableName() throws {
        let root = try directory(); defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("cached")
        try (Data([0, 0, 0, 0x18]) + Data("ftypM4A ".utf8) + Data(repeating: 0, count: 4096)).write(to: source)
        let fingerprint = try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: source))
        let audioDir = root.appendingPathComponent("playable-audio")

        let prepared = try PlayableAudioFile.prepare(source: source, fingerprint: fingerprint, directory: audioDir)
        XCTAssertEqual(prepared.path, audioDir.appendingPathComponent("\(fingerprint.sha256)-\(fingerprint.byteCount).m4a").path)
        XCTAssertEqual(try Data(contentsOf: prepared), try Data(contentsOf: source))
        // One volume: the playable name is a second link to the verified bytes, not a copy.
        XCTAssertEqual(try fileNumber(prepared), try fileNumber(source))
        XCTAssertEqual(try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: prepared)), fingerprint)

        let again = try PlayableAudioFile.prepare(source: source, fingerprint: fingerprint, directory: audioDir)
        XCTAssertEqual(again, prepared)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: audioDir.path), [prepared.lastPathComponent])
    }

    func testPrepareRefusesAnUnplayableOggContainer() throws {
        let root = try directory(); defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("cached")
        try (Data([0x4F, 0x67, 0x67, 0x53]) + Data(repeating: 0, count: 32)).write(to: source)
        let fingerprint = try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: source))
        let audioDir = root.appendingPathComponent("playable-audio")
        XCTAssertThrowsError(try PlayableAudioFile.prepare(source: source, fingerprint: fingerprint, directory: audioDir)) {
            XCTAssertEqual($0 as? MediaEngineError, .unsupportedCapability)
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: audioDir.path))
    }
}
