import XCTest
@testable import Kria

/// The inode a path currently resolves to. Two paths with the same identity are the same bytes
/// on disk (a hardlink); different identities are independent copies.
func fileIdentity(_ url: URL) -> UInt64? {
    (try? FileManager.default.attributesOfItem(atPath: url.path))?[.systemFileNumber].flatMap { ($0 as? NSNumber)?.uint64Value }
}

/// KRI-125: a cloud clip used to be written to disk four times. These pin the rules that let
/// most of those writes share bytes — and, just as important, the cases where they must not.
@MainActor final class UploadFileSharingTests: XCTestCase {
    private var cleanup: [URL] = []

    override func tearDown() {
        cleanup.forEach { try? FileManager.default.removeItem(at: $0) }
        cleanup = []
        super.tearDown()
    }

    /// A file the app owns and disposes of: its own tmp (what PhotosPicker exports land in).
    private func tempSource(_ contents: String = "clip bytes") throws -> URL {
        let url = FileManager.default.temporaryDirectory.appending(path: "share-\(UUID().uuidString).mp4")
        try Data(contents.utf8).write(to: url)
        cleanup.append(url)
        return url
    }

    /// A file that is inside the app's sandbox but is NOT tmp or Application Support — stands in
    /// for an external Files/iCloud pick, which the user can keep editing while we upload.
    private func externalSource(_ contents: String = "user's file") throws -> URL {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let url = documents.appending(path: "external-\(UUID().uuidString).mp4")
        try Data(contents.utf8).write(to: url)
        cleanup.append(url)
        return url
    }

    // MARK: ownership

    func testTmpAndApplicationSupportAreAppOwnedButDocumentsIsNot() throws {
        XCTAssertTrue(BackgroundUploadCoordinator.isAppOwned(try tempSource()))
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appending(path: "KriaUploads/x.mp4")
        XCTAssertTrue(BackgroundUploadCoordinator.isAppOwned(support), "a path need not exist to be classified")
        XCTAssertFalse(BackgroundUploadCoordinator.isAppOwned(try externalSource()))
    }

    func testOnlyTmpCountsAsDisposableForMoves() throws {
        XCTAssertTrue(BackgroundUploadCoordinator.isInTemporaryDirectory(try tempSource()))
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appending(path: "KriaUploads/x.mp4")
        XCTAssertFalse(BackgroundUploadCoordinator.isInTemporaryDirectory(support),
                       "moving a file out of Application Support could steal the project's original or a staged file")
        XCTAssertFalse(BackgroundUploadCoordinator.isInTemporaryDirectory(try externalSource()))
    }

    func testSiblingDirectoryWithSharedPrefixIsNotMistakenForInsideTmp() {
        // "…/tmp" must not match "…/tmp-evil/x": the check compares whole path components.
        let tmp = FileManager.default.temporaryDirectory
        let lookalike = tmp.deletingLastPathComponent().appending(path: tmp.lastPathComponent + "-evil/x.mp4")
        XCTAssertFalse(BackgroundUploadCoordinator.isInTemporaryDirectory(lookalike))
    }

    // MARK: staging (copy 2)

    func testStagingMovesOurOwnTempFileInsteadOfCopyingIt() throws {
        let source = try tempSource("photos export")
        let originalIdentity = fileIdentity(source)

        let staged = try BackgroundUploadCoordinator.stageIntoRecoveryDirectory(source)
        cleanup.append(staged)

        XCTAssertFalse(FileManager.default.fileExists(atPath: source.path), "a tmp source is renamed into place, not duplicated")
        XCTAssertEqual(try String(contentsOf: staged, encoding: .utf8), "photos export")
        XCTAssertEqual(fileIdentity(staged), originalIdentity, "same inode ⇒ the bytes were never rewritten")
    }

    func testStagingStillCopiesAnExternalSourceSoTheUsersFileSurvives() throws {
        let source = try externalSource("keep me")

        let staged = try BackgroundUploadCoordinator.stageIntoRecoveryDirectory(source)
        cleanup.append(staged)

        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path), "never consume a file the user owns")
        XCTAssertNotEqual(fileIdentity(staged), fileIdentity(source))
        XCTAssertEqual(try String(contentsOf: staged, encoding: .utf8), "keep me")
    }

    // MARK: upload file (copy 4)

    func testUploadFileSharesBytesWithAnAppOwnedSource() throws {
        let source = try tempSource("original")

        let link = try BackgroundUploadCoordinator.linkIntoRecoveryDirectory(source)
        cleanup.append(link)

        XCTAssertEqual(fileIdentity(link), fileIdentity(source))
        // Deleting one name must leave the other readable — the upload file and `originals/`
        // are deleted independently (after attach, and on failure cleanup).
        try FileManager.default.removeItem(at: source)
        XCTAssertEqual(try String(contentsOf: link, encoding: .utf8), "original")
    }

    func testUploadFileIsASnapshotOfAnExternalSource() throws {
        let source = try externalSource("as picked")

        let copy = try BackgroundUploadCoordinator.linkIntoRecoveryDirectory(source)
        cleanup.append(copy)
        XCTAssertNotEqual(fileIdentity(copy), fileIdentity(source), "an external file must be really copied, not hardlinked")

        // The reason it matters: the user edits their file while the upload is still running.
        try Data("edited after picking".utf8).write(to: source)
        XCTAssertEqual(try String(contentsOf: copy, encoding: .utf8), "as picked",
                       "the upload must send what the user picked, not what they edited afterwards")
    }

    func testUploadFileKeepsTheSourceExtensionForContentTypeSniffing() throws {
        let source = try tempSource()
        let link = try BackgroundUploadCoordinator.linkIntoRecoveryDirectory(source)
        cleanup.append(link)
        XCTAssertEqual(link.pathExtension, "mp4", "`enqueue` derives the MIME type from the file's extension")
    }
}
