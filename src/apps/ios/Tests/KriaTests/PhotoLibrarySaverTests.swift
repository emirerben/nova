import Foundation
import Photos
import XCTest
@testable import Kria

final class PhotoLibrarySaverTests: XCTestCase {
    func testInterruptedSaveRecoversUsingTheSameExportedBytes() async throws {
        let file = try exportedFile()
        defer { try? FileManager.default.removeItem(at: file) }
        let library = RecordingPhotoLibrary(errors: [photosError(.operationInterrupted)])
        try await saver(library).saveVideo(at: file)
        let writes = await library.writes
        XCTAssertEqual(writes.map(\.url), [file, file])
        XCTAssertEqual(writes.map(\.data), [Data("current-approved-edit".utf8), Data("current-approved-edit".utf8)])
        let authorizations = await library.authorizations
        XCTAssertEqual(authorizations, 1)
    }

    func testRepeatedInterruptionStopsWithoutClaimingSuccess() async throws {
        let file = try exportedFile()
        defer { try? FileManager.default.removeItem(at: file) }
        let library = RecordingPhotoLibrary(errors: [photosError(.operationInterrupted), photosError(.operationInterrupted)])
        do {
            try await saver(library).saveVideo(at: file)
            XCTFail("Two interrupted transactions cannot report Saved to Photos")
        } catch {
            XCTAssertEqual((error as NSError).domain, PHPhotosErrorDomain)
            XCTAssertEqual((error as NSError).code, PHPhotosError.Code.operationInterrupted.rawValue)
        }
        let writes = await library.writes
        XCTAssertEqual(writes.count, 2)
    }

    func testPermanentAndUnrelatedFailuresAreNeverRetried() async throws {
        let file = try exportedFile()
        defer { try? FileManager.default.removeItem(at: file) }
        let failures: [NSError] = [
            photosError(.invalidResource), photosError(.missingResource), photosError(.notEnoughSpace),
            photosError(.accessRestricted), photosError(.accessUserDenied), photosError(.userCancelled),
            NSError(domain: NSCocoaErrorDomain, code: 3301),
        ]
        for failure in failures {
            let library = RecordingPhotoLibrary(errors: [failure])
            do {
                try await saver(library).saveVideo(at: file)
                XCTFail("Unexpected successful save for \(failure.domain)/\(failure.code)")
            } catch {
                XCTAssertEqual((error as NSError).domain, failure.domain)
                XCTAssertEqual((error as NSError).code, failure.code)
            }
            let writes = await library.writes
            XCTAssertEqual(writes.count, 1)
        }
    }

    func testSuccessCreatesOnlyOneAsset() async throws {
        let file = try exportedFile()
        defer { try? FileManager.default.removeItem(at: file) }
        let library = RecordingPhotoLibrary(errors: [])
        try await saver(library).saveVideo(at: file)
        let writes = await library.writes
        XCTAssertEqual(writes.count, 1)
    }

    func testDeniedAndRestrictedAuthorizationNeverWrite() async throws {
        for status in [PHAuthorizationStatus.denied, .restricted, .notDetermined] {
            let library = RecordingPhotoLibrary(errors: [], authorization: status)
            do {
                try await saver(library).saveVideo(at: URL(filePath: "/unused.mp4"))
                XCTFail("Unauthorized save succeeded")
            } catch { XCTAssertTrue(error is PhotoLibraryError) }
            let writes = await library.writes
            XCTAssertTrue(writes.isEmpty)
        }
    }

    func testCancellationBeforeSavingDoesNotRequestPermissionOrWrite() async throws {
        let library = RecordingPhotoLibrary(errors: [])
        let save = saver(library)
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            try await save.saveVideo(at: URL(filePath: "/unused.mp4"))
        }
        do { try await task.value; XCTFail("Cancelled export succeeded") }
        catch { XCTAssertTrue(error is CancellationError) }
        let authorizations = await library.authorizations
        let writes = await library.writes
        XCTAssertEqual(authorizations, 0)
        XCTAssertTrue(writes.isEmpty)
    }

    func testCancellationAfterInterruptionPreventsAnotherTransaction() async throws {
        let file = try exportedFile()
        defer { try? FileManager.default.removeItem(at: file) }
        let library = RecordingPhotoLibrary(errors: [photosError(.operationInterrupted)], cancelAfterWrite: true)
        let save = saver(library)
        let task = Task { try await save.saveVideo(at: file) }
        do { try await task.value; XCTFail("Cancelled recovery succeeded") }
        catch { XCTAssertTrue(error is CancellationError) }
        let writes = await library.writes
        XCTAssertEqual(writes.count, 1)
    }

    private func saver(_ library: RecordingPhotoLibrary) -> PhotoLibrarySaver {
        PhotoLibrarySaver(authorize: { await library.authorize() }, writeVideo: { try await library.write($0) })
    }

    private func exportedFile() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appending(path: "photos-save-\(UUID().uuidString).mp4")
        try Data("current-approved-edit".utf8).write(to: url)
        return url
    }

    private func photosError(_ code: PHPhotosError.Code) -> NSError {
        NSError(domain: PHPhotosErrorDomain, code: code.rawValue)
    }
}

private actor RecordingPhotoLibrary {
    struct Write: Sendable { let url: URL; let data: Data }
    var writes: [Write] = []
    var authorizations = 0
    private var errors: [NSError]
    private let authorization: PHAuthorizationStatus
    private let cancelAfterWrite: Bool

    init(errors: [NSError], authorization: PHAuthorizationStatus = .authorized, cancelAfterWrite: Bool = false) {
        self.errors = errors
        self.authorization = authorization
        self.cancelAfterWrite = cancelAfterWrite
    }

    func authorize() -> PHAuthorizationStatus {
        authorizations += 1
        return authorization
    }

    func write(_ url: URL) throws {
        writes.append(Write(url: url, data: try Data(contentsOf: url)))
        if cancelAfterWrite { withUnsafeCurrentTask { $0?.cancel() } }
        if !errors.isEmpty { throw errors.removeFirst() }
    }
}
