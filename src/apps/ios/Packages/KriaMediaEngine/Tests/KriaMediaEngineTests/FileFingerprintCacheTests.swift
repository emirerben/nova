import Foundation
import XCTest
@testable import KriaMediaEngine

/// `FileFingerprintCache` must never report a changed file as unchanged: that would make the editor trust a source or
/// proxy that is no longer what its name says. Written from the ways that can happen: a file rewritten with different
/// bytes (a different size, or the same size and a later modification time), a damaged or missing record, and one
/// record being reused for another file. It must also actually skip the read for a file that did not change.
final class FileFingerprintCacheTests: XCTestCase {
    private var directory: URL!
    private var hashed = HashCounter()

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("fingerprint-cache-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        hashed = HashCounter()
        let counter = hashed
        SHA256Fingerprinter.observer = { _ in counter.increment() }
    }

    override func tearDownWithError() throws {
        SHA256Fingerprinter.observer = nil
        try? FileManager.default.removeItem(at: directory)
    }

    private final class HashCounter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func increment() { lock.lock(); value += 1; lock.unlock() }
        var count: Int { lock.lock(); defer { lock.unlock() }; return value }
    }

    private func write(_ text: String, to name: String, modified: Date? = nil) throws -> URL {
        let url = directory.appendingPathComponent(name)
        try Data(text.utf8).write(to: url)
        if let modified { try FileManager.default.setAttributes([.modificationDate: modified], ofItemAtPath: url.path) }
        return url
    }

    private var cache: FileFingerprintCache { FileFingerprintCache(directory: directory.appendingPathComponent("records", isDirectory: true)) }

    func testAnUnchangedFileIsHashedOnceAndTheSecondAskReadsNothing() throws {
        let file = try write("first version", to: "clip.mp4")
        let first = try cache.fingerprint(file: file)
        let second = try cache.fingerprint(file: file)
        XCTAssertEqual(first, second)
        XCTAssertEqual(hashed.count, 1, "the second ask must come from the record")
    }

    func testAFileRewrittenWithADifferentSizeIsHashedAgain() throws {
        let file = try write("short", to: "clip.mp4")
        let before = try cache.fingerprint(file: file)
        _ = try write("a longer version of the same file", to: "clip.mp4")
        let after = try cache.fingerprint(file: file)
        XCTAssertNotEqual(before.hex, after.hex)
        XCTAssertEqual(hashed.count, 2)
    }

    func testAFileRewrittenWithTheSameSizeButALaterModificationTimeIsHashedAgain() throws {
        let earlier = Date(timeIntervalSince1970: 1_700_000_000)
        let file = try write("same size A", to: "clip.mp4", modified: earlier)
        let before = try cache.fingerprint(file: file)
        _ = try write("same size B", to: "clip.mp4", modified: earlier.addingTimeInterval(60))
        let after = try cache.fingerprint(file: file)
        XCTAssertNotEqual(before.hex, after.hex)
        XCTAssertEqual(hashed.count, 2)
    }

    func testADamagedRecordIsIgnoredAndRewritten() throws {
        let file = try write("some bytes", to: "clip.mp4")
        let expected = try cache.fingerprint(file: file)
        let records = directory.appendingPathComponent("records", isDirectory: true)
        for record in try FileManager.default.contentsOfDirectory(at: records, includingPropertiesForKeys: nil) {
            try Data("not json".utf8).write(to: record)
        }
        XCTAssertEqual(try cache.fingerprint(file: file), expected)
        XCTAssertEqual(hashed.count, 2, "a record that does not decode counts as no record")
        _ = try cache.fingerprint(file: file)
        XCTAssertEqual(hashed.count, 2, "and the rewritten record is trusted again")
    }

    func testTwoFilesDoNotShareARecord() throws {
        let one = try write("contents one", to: "one.mp4")
        let two = try write("contents two", to: "two.mp4")
        let first = try cache.fingerprint(file: one)
        let second = try cache.fingerprint(file: two)
        XCTAssertNotEqual(first.hex, second.hex)
        XCTAssertEqual(try cache.fingerprint(file: one), first)
        XCTAssertEqual(try cache.fingerprint(file: two), second)
        XCTAssertEqual(hashed.count, 2)
    }

    func testAMissingFileThrowsInsteadOfAnsweringFromARecord() throws {
        let file = try write("here now", to: "clip.mp4")
        _ = try cache.fingerprint(file: file)
        try FileManager.default.removeItem(at: file)
        XCTAssertThrowsError(try cache.fingerprint(file: file))
    }
}
