import Photos
import PhotosUI
import SwiftUI
import XCTest
@testable import Kria

/// KRI-282 regression: files picked in the in-app gallery were all "couldn't be read" because
/// `PhotosPickerItem(itemIdentifier:)` has no item provider to `loadTransferable` from.
final class LibraryAssetLoaderTests: XCTestCase {
    private struct FakeExporter: LibraryAssetExporting {
        let url: URL
        var failure: Error?
        let calls = Calls()
        final class Calls: @unchecked Sendable { var identifiers: [String] = [] }
        func exportFile(localIdentifier: String) async throws -> URL {
            calls.identifiers.append(localIdentifier)
            if let failure { throw failure }
            return url
        }
    }

    private let exported = URL(fileURLWithPath: "/tmp/exported.mov")
    private let viaProviderURL = URL(fileURLWithPath: "/tmp/provider.mov")

    func testItemMadeFromAnIdentifierHasNoProvider() {
        // The root cause: gallery picks are built this way and carry no loadable content types.
        XCTAssertTrue(PhotosPickerItem(itemIdentifier: "ABC/L0/001").supportedContentTypes.isEmpty)
    }

    func testGalleryPickIsReadFromPhotosAndNeverTouchesTheProvider() async throws {
        let exporter = FakeExporter(url: exported)
        var providerCalled = false
        let url = try await PhotoItemFileLoader(exporter: exporter).load(identifier: "id-1", hasProvider: false) {
            providerCalled = true
            return nil
        }
        XCTAssertEqual(url, exported)
        XCTAssertFalse(providerCalled)
        XCTAssertEqual(exporter.calls.identifiers, ["id-1"])
    }

    func testSystemPickerResultStillLoadsThroughItsProvider() async throws {
        let exporter = FakeExporter(url: exported)
        let url = try await PhotoItemFileLoader(exporter: exporter).load(identifier: "id-2", hasProvider: true) { self.viaProviderURL }
        XCTAssertEqual(url, viaProviderURL)
        XCTAssertTrue(exporter.calls.identifiers.isEmpty)
    }

    func testProviderFailureFallsBackToPhotos() async throws {
        struct Boom: Error {}
        let exporter = FakeExporter(url: exported)
        let thrown = try await PhotoItemFileLoader(exporter: exporter).load(identifier: "a", hasProvider: true) { throw Boom() }
        let empty = try await PhotoItemFileLoader(exporter: exporter).load(identifier: "b", hasProvider: true) { nil }
        XCTAssertEqual(thrown, exported)
        XCTAssertEqual(empty, exported)
        XCTAssertEqual(exporter.calls.identifiers, ["a", "b"])
    }

    func testCancellationIsNotSwallowedIntoAnExport() async {
        let exporter = FakeExporter(url: exported)
        do {
            _ = try await PhotoItemFileLoader(exporter: exporter).load(identifier: "a", hasProvider: true) { throw CancellationError() }
            XCTFail("expected cancellation")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        XCTAssertTrue(exporter.calls.identifiers.isEmpty)
    }

    func testExportFailureSurfacesSoTheUserSeesTheUnreadableLine() async {
        let exporter = FakeExporter(url: exported, failure: LibraryAssetExportError.assetNotFound)
        do {
            _ = try await PhotoItemFileLoader(exporter: exporter).load(identifier: "gone", hasProvider: false) { nil }
            XCTFail("expected failure")
        } catch {
            XCTAssertEqual(error as? LibraryAssetExportError, .assetNotFound)
        }
    }

    // MARK: resource choice (video, edited video, photo, edited photo, Live Photo)

    private typealias C = LibraryResourcePicker.Candidate

    func testEditedVideoRenderWinsOverTheOriginal() {
        let original = C(type: .video, filename: "IMG_1.MOV"), edited = C(type: .fullSizeVideo, filename: "FullSizeRender.mov")
        XCTAssertEqual(LibraryResourcePicker.pick(mediaType: .video, from: [original, edited]), edited)
        XCTAssertEqual(LibraryResourcePicker.pick(mediaType: .video, from: [original]), original)
    }

    func testPhotoPrefersEditedThenOriginal() {
        let original = C(type: .photo, filename: "IMG_2.HEIC"), edited = C(type: .fullSizePhoto, filename: "FullSizeRender.jpg")
        XCTAssertEqual(LibraryResourcePicker.pick(mediaType: .image, from: [original, edited]), edited)
        XCTAssertEqual(LibraryResourcePicker.pick(mediaType: .image, from: [original]), original)
    }

    func testLivePhotoUploadsTheStillNotThePairedVideo() {
        let still = C(type: .photo, filename: "IMG_3.HEIC"), paired = C(type: .pairedVideo, filename: "IMG_3.MOV")
        XCTAssertEqual(LibraryResourcePicker.pick(mediaType: .image, from: [paired, still]), still)
    }

    func testNoUsableResourceIsNil() {
        XCTAssertNil(LibraryResourcePicker.pick(mediaType: .video, from: [C(type: .adjustmentData, filename: "x")]))
        XCTAssertNil(LibraryResourcePicker.pick(mediaType: .image, from: []))
    }
}
