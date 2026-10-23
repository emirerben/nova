import XCTest
@testable import Kria

/// KRI-729: capture context and local identifiers never become upload authority.
final class CameraRollCollectionTests: XCTestCase {
    func testAllAddedAssetsSurviveResumeWithoutASecondSelection() throws {
        let collection = try CameraRollCollection(name: "Whole outing", identifiers: (0..<50).map(String.init))
        var legacy = try JSONSerialization.jsonObject(with: JSONEncoder().encode(collection)) as! [String: Any]
        var entries = legacy["entries"] as! [[String: Any]]
        for index in entries.indices { entries[index]["wantsAnalysis"] = index < 10 }
        legacy["entries"] = entries
        let resumed = try JSONDecoder().decode(CameraRollCollection.self, from: JSONSerialization.data(withJSONObject: legacy))
        XCTAssertEqual(resumed.entries.map(\.id), (0..<50).map(String.init))
        let saved = String(decoding: try JSONEncoder().encode(resumed), as: UTF8.self)
        XCTAssertFalse(saved.contains("wantsAnalysis"), "Old unchecked flags must not exclude added content")
    }
    func testFiveHundredItemsSurviveResume() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let originals = MediaLibraryOriginals(owner: "capacity-fixture", base: root)
        var collection = try CameraRollCollection(name: "Full trip", identifiers: (0..<500).map(String.init))
        for index in 0..<500 {
            let asset = SavedMediaAsset(id: UUID(), kind: "video", status: "ready",
                originalDeclaredSHA256: String(repeating: "a", count: 64), originalByteCount: 100_000_000,
                originalAvailability: "device_only",
                metadata: ["filename": .string("IMG_\(index).MOV"), "duration_s": .number(10)],
                sources: [SavedMediaSource(id: UUID(), role: "analysis_proxy", status: "available",
                    byteCount: 1_000_000, contentType: "video/mp4", uploadedSHA256: String(repeating: "b", count: 64), generation: "42")],
                usage: [], storageWarnings: [])
            collection.bind(asset, to: String(index))
            collection.record([metadata(String(index), seconds: Double(index))], sharesCaptureContext: true)
        }
        collection.serverRevision = 7
        collection.setGroup("Last stop", for: "499")
        try collection.save(in: originals)
        let resumed = try CameraRollCollection.load(id: collection.id, in: originals, sharesCaptureContext: true)
        XCTAssertEqual(resumed.entries.count, 500)
        XCTAssertEqual(resumed.entries.last?.id, "499")
        XCTAssertEqual(resumed.entries.compactMap(\.savedAsset).count, 500)
        XCTAssertEqual(resumed.entries.last?.savedAsset?.filename, "IMG_499.MOV")
        XCTAssertEqual(resumed.group(for: "499"), "Last stop")
        XCTAssertEqual(resumed.serverRevision, 7)
    }

    func testSelectionResumeCorrectionsAndRemoval() throws {
        var collection = try CameraRollCollection(name: "Outing", identifiers: ["a", "b", "a"])
        XCTAssertEqual(collection.entries.map(\.id), ["a", "b"])
        collection.record([metadata("a", seconds: 100)], sharesCaptureContext: true)
        XCTAssertEqual(collection.pendingIdentifiers, ["b"])
        collection.setGroup("My event", for: "a")
        let encoded = try JSONEncoder().encode(collection)
        var resumed = try JSONDecoder().decode(CameraRollCollection.self, from: encoded)
        try resumed.select(["a", "c"])
        XCTAssertEqual(resumed.pendingIdentifiers, ["c"])
        XCTAssertEqual(resumed.group(for: "a"), "My event")
        XCTAssertEqual(resumed.entries.count, 2)
        XCTAssertFalse(resumed.entries.contains { $0.id == "b" })
    }

    func testMissingMetadataRevocationAndPrivacySwitch() throws {
        var collection = try CameraRollCollection(name: "Outing", identifiers: ["a", "b"])
        collection.record([metadata("a", seconds: 100), metadata("b", seconds: nil)], sharesCaptureContext: true)
        XCTAssertEqual(collection.group(for: "b"), "Outing")
        collection.clearCaptureContext()
        XCTAssertNil(collection.entries[0].metadata?.capture)
        collection.reconcileAccessibleIdentifiers(["b"])
        XCTAssertEqual(collection.entries.map(\.id), ["b"])
        collection.reconcileAccessibleIdentifiers([])
        XCTAssertTrue(collection.entries.isEmpty)
    }

    func testBoundedSelectionAndUnselectedMetadata() throws {
        XCTAssertThrowsError(try CameraRollCollection(name: "Too many", identifiers: (0..<501).map(String.init)))
        var collection = try CameraRollCollection(name: "Outing", identifiers: ["a"])
        collection.record([metadata("other", seconds: 0)], sharesCaptureContext: true)
        XCTAssertEqual(collection.pendingIdentifiers, ["a"])
        collection.record([metadata("a", seconds: 0)], sharesCaptureContext: false)
        XCTAssertNil(collection.entries[0].metadata?.capture)
    }

    func testTimeGroupsAndExplicitCorrectionSurviveMetadataRefresh() throws {
        var collection = try CameraRollCollection(name: "Outing", identifiers: ["a", "b", "c"])
        collection.record([metadata("a", seconds: 0), metadata("b", seconds: 60), metadata("c", seconds: 20_000)], sharesCaptureContext: true)
        XCTAssertEqual(collection.group(for: "a"), collection.group(for: "b"))
        XCTAssertNotEqual(collection.group(for: "a"), collection.group(for: "c"))
        collection.setGroup("Evening", for: "c")
        collection.record([metadata("c", seconds: 20_001)], sharesCaptureContext: true)
        XCTAssertEqual(collection.group(for: "c"), "Evening")
    }

    private func metadata(_ id: String, seconds: Double?) -> CameraRollMetadata {
        CameraRollMetadata(id: id, kind: "video", duration: 10, width: 1920, height: 1080,
                           modifiedAt: nil,
                           capture: seconds.map { ClipCaptureRaw(captureTime: Date(timeIntervalSince1970: $0), latitude: nil, longitude: nil) })
    }
}
