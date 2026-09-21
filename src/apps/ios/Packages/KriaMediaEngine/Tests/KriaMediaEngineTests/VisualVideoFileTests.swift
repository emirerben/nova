import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-121: a Visuals-pool video plays from the verified cache file itself,
/// under a name AVFoundation can open.
final class VisualVideoFileTests: XCTestCase {
    private func directory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// A box header: a 32-bit size, then the four-character box type and whatever follows it.
    private func box(_ text: String, payload: Int = 32) -> Data {
        Data([0, 0, 0, 0x18]) + Data(text.utf8) + Data(repeating: 0, count: payload)
    }

    private func fileNumber(_ url: URL) throws -> UInt64 {
        try XCTUnwrap((FileManager.default.attributesOfItem(atPath: url.path)[.systemFileNumber] as? NSNumber)?.uint64Value)
    }

    func testExtensionComesFromTheFileTypeBox() throws {
        let root = try directory(); defer { try? FileManager.default.removeItem(at: root) }
        let cases: [(name: String, bytes: Data, expected: String?)] = [
            ("isom", box("ftypisom"), "mp4"), ("mp42", box("ftypmp42"), "mp4"), ("bare-ftyp", box("ftyp", payload: 0), "mp4"),
            ("quicktime", box("ftypqt  "), "mov"), ("classic-moov", box("moov"), "mov"), ("classic-wide", box("wide", payload: 0), "mov"),
            ("jpeg", Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01]), nil),
            ("png", Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D]), nil),
            ("webm", Data([0x1A, 0x45, 0xDF, 0xA3, 0x9F, 0x42, 0x86, 0x81, 0x01, 0x42, 0xF7, 0x81]), nil),
            ("text", Data("not a video at all".utf8), nil), ("short", Data("ftyp".utf8), nil), ("empty", Data(), nil),
        ]
        for item in cases {
            // The cache names files `<sha256>-<bytes>`: there is no extension to trust.
            let file = root.appendingPathComponent(item.name)
            try item.bytes.write(to: file)
            if let expected = item.expected {
                XCTAssertEqual(try VisualVideoFile.fileExtension(of: file), expected, item.name)
            } else {
                XCTAssertThrowsError(try VisualVideoFile.fileExtension(of: file), item.name)
            }
        }
        XCTAssertThrowsError(try VisualVideoFile.fileExtension(of: root.appendingPathComponent("missing")))
    }

    func testPrepareLinksTheVerifiedFileUnderAPlayableName() throws {
        let root = try directory(); defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("cached")
        try box("ftypisom", payload: 4096).write(to: source)
        let fingerprint = try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: source))
        let videos = root.appendingPathComponent("visual-videos")

        let prepared = try VisualVideoFile.prepare(source: source, fingerprint: fingerprint, directory: videos)
        XCTAssertEqual(prepared.path, videos.appendingPathComponent("\(fingerprint.sha256)-\(fingerprint.byteCount).mp4").path)
        XCTAssertEqual(try Data(contentsOf: prepared), try Data(contentsOf: source))
        // One volume: the playable name is a second link to the verified bytes, not a copy.
        XCTAssertEqual(try fileNumber(prepared), try fileNumber(source))
        XCTAssertEqual(try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: prepared)), fingerprint)

        let again = try VisualVideoFile.prepare(source: source, fingerprint: fingerprint, directory: videos)
        XCTAssertEqual(again, prepared)
        XCTAssertEqual(try fileNumber(again), try fileNumber(source))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: videos.path), [prepared.lastPathComponent])
        // The cache can evict its own name; the prepared link keeps the bytes playable.
        try FileManager.default.removeItem(at: source)
        XCTAssertEqual(try Data(contentsOf: prepared).count, Int(fingerprint.byteCount))
    }

    func testPrepareNamesQuickTimeFilesMov() throws {
        let root = try directory(); defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("cached")
        try box("ftypqt  ", payload: 512).write(to: source)
        let fingerprint = try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: source))
        let prepared = try VisualVideoFile.prepare(source: source, fingerprint: fingerprint, directory: root.appendingPathComponent("visual-videos"))
        XCTAssertEqual(prepared.lastPathComponent, "\(fingerprint.sha256)-\(fingerprint.byteCount).mov")
        XCTAssertEqual(try fileNumber(prepared), try fileNumber(source))
    }

    func testPrepareRefusesBytesThatAreNotAMovie() throws {
        let root = try directory(); defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("cached")
        try Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01]).write(to: source)
        let fingerprint = try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: source))
        let videos = root.appendingPathComponent("visual-videos")
        XCTAssertThrowsError(try VisualVideoFile.prepare(source: source, fingerprint: fingerprint, directory: videos)) {
            XCTAssertEqual($0 as? MediaEngineError, .missingAsset("cached"))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: videos.path))
    }
}
