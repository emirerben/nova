import XCTest
import UIKit
@testable import Kria

/// KRI-125: a chosen clip needs a thumbnail from the moment it is chosen (keyed by its upload id), handed
/// to the server's media id when it attaches. Uses the real `montage.mp4` / `montage.jpg` the app ships.
@MainActor final class CreationMediaPreviewTests: XCTestCase {
    private var recordIDs: [UUID] = []
    private var mediaIDs: [String] = []
    private var files: [URL] = []

    override func tearDown() {
        recordIDs.forEach { CreationMediaPreview.discard(recordID: $0) }
        mediaIDs.forEach { try? FileManager.default.removeItem(at: CreationMediaPreview.url(mediaID: $0)) }
        files.forEach { try? FileManager.default.removeItem(at: $0) }
        super.tearDown()
    }

    private func bundled(_ name: String, _ ext: String) throws -> URL {
        try XCTUnwrap(Bundle.main.url(forResource: name, withExtension: ext), "\(name).\(ext) must ship in the app bundle")
    }

    private func newRecordID() -> UUID { let id = UUID(); recordIDs.append(id); return id }
    private func newMediaID() -> String { let id = "test-media-\(UUID().uuidString).mp4"; mediaIDs.append(id); return id }
    private func exists(_ url: URL) -> Bool { FileManager.default.fileExists(atPath: url.path) }

    // MARK: generation

    func testAVideoYieldsAThumbnailNoLargerThanThePreviewSize() async throws {
        let data = await CreationMediaPreview.jpegData(for: try bundled("montage", "mp4"))
        let image = try XCTUnwrap(UIImage(data: try XCTUnwrap(data)))
        let longest = max(try XCTUnwrap(image.cgImage).width, try XCTUnwrap(image.cgImage).height)
        XCTAssertGreaterThan(longest, 0)
        XCTAssertLessThanOrEqual(longest, 320)
    }

    /// The visuals pool takes photos, which a video frame generator cannot read.
    func testAPhotoYieldsAThumbnailToo() async throws {
        let data = await CreationMediaPreview.jpegData(for: try bundled("montage", "jpg"))
        let image = try XCTUnwrap(UIImage(data: try XCTUnwrap(data)))
        XCTAssertLessThanOrEqual(max(try XCTUnwrap(image.cgImage).width, try XCTUnwrap(image.cgImage).height), 320)
    }

    func testSomethingThatIsNotMediaYieldsNoThumbnailInsteadOfCrashing() async throws {
        let junk = FileManager.default.temporaryDirectory.appending(path: "junk-\(UUID().uuidString).mp4")
        try Data("not a video".utf8).write(to: junk)
        files.append(junk)
        let data = await CreationMediaPreview.jpegData(for: junk)
        XCTAssertNil(data)
        let missing = await CreationMediaPreview.jpegData(for: junk.appendingPathExtension("gone"))
        XCTAssertNil(missing)
    }

    // MARK: keys and hand-off

    func testSaveEarlyWritesUnderTheUploadIDBeforeThereIsAnyMediaID() async throws {
        let recordID = newRecordID()

        let written = await CreationMediaPreview.saveEarly(localURL: try bundled("montage", "mp4"), recordID: recordID)

        XCTAssertTrue(written)
        XCTAssertTrue(exists(CreationMediaPreview.url(recordID: recordID)))
    }

    func testFinalizeHandsTheEarlyThumbnailToTheMediaIDWithoutRegeneratingIt() async throws {
        let recordID = newRecordID(), mediaID = newMediaID()
        await CreationMediaPreview.saveEarly(localURL: try bundled("montage", "mp4"), recordID: recordID)
        let early = try Data(contentsOf: CreationMediaPreview.url(recordID: recordID))

        // The local file is gone by attach time in some paths; the early thumbnail must be enough.
        await CreationMediaPreview.finalize(recordID: recordID, localURL: URL(fileURLWithPath: "/nonexistent.mp4"), mediaID: mediaID)

        XCTAssertFalse(exists(CreationMediaPreview.url(recordID: recordID)), "handed over, not copied")
        XCTAssertEqual(try Data(contentsOf: CreationMediaPreview.url(mediaID: mediaID)), early)
    }

    /// A clip must never end up attached with no thumbnail just because the early one failed.
    func testFinalizeGeneratesOneWhenTheEarlyThumbnailNeverHappened() async throws {
        let recordID = newRecordID(), mediaID = newMediaID()

        await CreationMediaPreview.finalize(recordID: recordID, localURL: try bundled("montage", "mp4"), mediaID: mediaID)

        XCTAssertTrue(exists(CreationMediaPreview.url(mediaID: mediaID)))
    }

    func testDiscardRemovesTheEarlyThumbnailAndIsSafeToRepeat() async throws {
        let recordID = newRecordID()
        await CreationMediaPreview.saveEarly(localURL: try bundled("montage", "mp4"), recordID: recordID)

        CreationMediaPreview.discard(recordID: recordID)
        CreationMediaPreview.discard(recordID: recordID)

        XCTAssertFalse(exists(CreationMediaPreview.url(recordID: recordID)))
    }

    func testTheTwoKeysNeverCollide() {
        let id = UUID()
        XCTAssertNotEqual(CreationMediaPreview.url(recordID: id), CreationMediaPreview.url(mediaID: id.uuidString))
    }
}
