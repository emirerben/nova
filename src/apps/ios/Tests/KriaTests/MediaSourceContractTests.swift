import XCTest
@testable import Kria

/// KRI-118: a proxy-contract violation used to collapse into one generic
/// `APIError.invalidResponse` regardless of cause. These tests pin the
/// specific, user-facing copy for each distinct violation and the
/// early container gate that avoids an opaque AVFoundation transcode failure.
final class MediaSourceContractTests: XCTestCase {
    func testEachViolationHasItsOwnActionableCopy() {
        XCTAssertEqual(MediaSourceContractError.unsupportedContainer.errorDescription, "Export this as MP4 or MOV first.")
        XCTAssertEqual(MediaSourceContractError.missingVideoTrack.errorDescription, "This file doesn't have a video track.")
        XCTAssertEqual(MediaSourceContractError.durationMismatch.errorDescription, "This clip's length changed during upload. Try again.")
        XCTAssertEqual(MediaSourceContractError.audioPresenceMismatch.errorDescription, "This clip's audio didn't match after upload. Try again.")
        XCTAssertEqual(MediaSourceContractError.unsupportedGeometry.errorDescription, "This clip couldn't be processed. Try re-exporting it.")
    }

    /// Every case's copy must be distinct -- otherwise splitting the guard
    /// bought nothing over the single generic message it replaced.
    func testViolationCopyIsAllDistinct() {
        let allCases: [MediaSourceContractError] = [.unsupportedContainer, .missingVideoTrack, .durationMismatch, .audioPresenceMismatch, .unsupportedGeometry]
        let messages = Set(allCases.compactMap(\.errorDescription))
        XCTAssertEqual(messages.count, allCases.count)
    }

    func testSupportedContainersPassEarlyValidation() throws {
        for ext in ["mp4", "mov", "m4v", "MP4", "MOV", "qt"] {
            XCTAssertNoThrow(
                try ProjectMediaUploadContract.validateSupportedContainer(URL(fileURLWithPath: "/tmp/clip.\(ext)")),
                "expected .\(ext) to be treated as a supported container"
            )
        }
    }

    func testUnsupportedContainersFailEarlyWithActionableCopy() {
        for ext in ["avi", "mkv", "webm", "wmv", ""] {
            let url = ext.isEmpty ? URL(fileURLWithPath: "/tmp/clip") : URL(fileURLWithPath: "/tmp/clip.\(ext)")
            XCTAssertThrowsError(try ProjectMediaUploadContract.validateSupportedContainer(url)) { error in
                XCTAssertEqual(error as? MediaSourceContractError, .unsupportedContainer)
            }
        }
    }
}
