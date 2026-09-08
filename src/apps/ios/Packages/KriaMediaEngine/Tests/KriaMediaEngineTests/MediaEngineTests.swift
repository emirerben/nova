import Foundation
import XCTest
@testable import KriaMediaEngine

final class MediaEngineTests: XCTestCase {
    func testFingerprintIsStreamingAndDeterministic() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("fixture.bin")
        try Data(repeating: 0xAB, count: 2_500_000).write(to: file)
        let first = try SHA256Fingerprinter().fingerprint(file: file)
        let second = try SHA256Fingerprinter().fingerprint(file: file)
        XCTAssertEqual(first, second); XCTAssertEqual(first.byteCount, 2_500_000); XCTAssertEqual(first.algorithm, "sha256"); XCTAssertEqual(first.hex.count, 64)
    }

    func testTimelineTrimSplitAndRetimingMath() {
        let clip = TimelineClip(id: "clip", sourceAssetID: "asset", sourceStart: 4, sourceDuration: 10, timelineStart: 2, rate: 2)
        XCTAssertEqual(clip.duration, 5)
        let trimmed = TimelineMath.trim(clip, sourceStart: 5, sourceDuration: 4)
        XCTAssertEqual(trimmed.sourceStart, 5); XCTAssertEqual(trimmed.sourceDuration, 4)
        let split = try! XCTUnwrap(TimelineMath.split(clip, atTimelineTime: 4)); XCTAssertEqual(split.0.sourceDuration, 4); XCTAssertEqual(split.1.sourceStart, 8); XCTAssertEqual(split.1.timelineStart, 4)
        XCTAssertEqual(TimelineMath.sourceTime(forTimelineTime: 4, in: clip), 8)
        XCTAssertEqual(TimelineMath.timelineTime(forSourceTime: 10, in: clip), 5)
    }

    func testRecipeRoundTripValidation() throws {
        let asset = MediaAsset(id: "a", relativePath: "originals/a.mov")
        let clip = TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 3, text: TextTreatment(text: "Hello"))
        let recipe = EditRecipe(assets: [asset], tracks: [TimelineTrack(id: "video", kind: .video, clips: [clip])], requiredCapabilities: [.animatedText])
        try recipe.validate()
        let decoded = try JSONDecoder().decode(EditRecipe.self, from: JSONEncoder().encode(recipe))
        XCTAssertEqual(decoded, recipe); XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 3)
        let wire = try RecipeJSON.encode(recipe); let wireObject = try XCTUnwrap(JSONSerialization.jsonObject(with: wire) as? [String: Any])
        XCTAssertNotNil(wireObject["schema_version"]); XCTAssertNotNil(wireObject["renderer_version"]); XCTAssertNotNil(wireObject["frame_rate"])
        XCTAssertEqual(try RecipeJSON.decode(wire), recipe)
    }

    func testRecipeRejectsUnsafeValuesAndMigratesLegacyPayload() throws {
        XCTAssertThrowsError(try EditRecipe(frameRate: 0).validate()) { XCTAssertEqual($0 as? RecipeError, .invalidFrameRate(0)) }
        let unsafe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 1, transition: Transition(duration: 2))])])
        XCTAssertThrowsError(try unsafe.validate())
        let legacy: [String: Any] = ["width": 1080, "height": 1920, "fps": 30, "clips": [["source_ref": "legacy", "in_s": 1, "duration_s": 2.0]]]
        let migrated = try RecipeMigration.migrate(JSONSerialization.data(withJSONObject: legacy))
        try migrated.validate(); XCTAssertEqual(migrated.schemaVersion, 1); XCTAssertEqual(migrated.tracks.first?.clips.first?.sourceAssetID, "legacy"); XCTAssertEqual(migrated.tracks.first?.clips.first?.sourceStart, 1)
        let future = try JSONSerialization.data(withJSONObject: ["schema_version": 99])
        XCTAssertThrowsError(try RecipeMigration.migrate(future)) { XCTAssertEqual($0 as? RecipeMigrationError, .unsupportedSchema(99)) }
        let missing = EditRecipe(tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "missing", sourceDuration: 1)])])
        XCTAssertThrowsError(try missing.validate()) { XCTAssertEqual($0 as? RecipeError, .missingAssetReference) }
    }

    func testBackendOwnedRecipeFixtureDecodesAndValidates() throws {
        let fixture = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/kria_edit_recipe_v1.json")
            .standardizedFileURL
        let recipe = try RecipeJSON.decode(Data(contentsOf: fixture))
        try recipe.validate()
        XCTAssertEqual(recipe.schemaVersion, 1)
        XCTAssertEqual(recipe.canvas, .vertical1080)
        XCTAssertEqual(recipe.tracks.first?.clips.first?.sourceAssetID, "media-1")
    }

    func testCapabilityNegotiationFallsBackSafely() {
        let recipe = EditRecipe(requiredCapabilities: [.hdr, .local1080Export])
        let decision = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: [.basicComposition])).decide(for: recipe)
        XCTAssertEqual(decision.route, .cloud); XCTAssertEqual(decision.missingCapabilities, [.hdr, .local1080Export])
        let hot = CapabilityNegotiator().decide(for: EditRecipe(), thermalState: .serious)
        XCTAssertEqual(hot.route, .cloud)
        let lowStorage = CapabilityNegotiator().decide(for: EditRecipe(), freeStorageBytes: 99, estimatedTemporaryBytes: 100)
        XCTAssertEqual(lowStorage.route, .cloud); XCTAssertEqual(lowStorage.reason, "Insufficient temporary storage")
    }

    func testExportCheckpointIsRecoverableAndAtomic() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = FileExportStateStore(directory: dir); let checkpoint = ExportCheckpoint(exportID: "job", status: .exporting, progress: 0.42)
        try store.save(checkpoint); XCTAssertEqual(try store.load(exportID: "job"), checkpoint)
        XCTAssertTrue(ExportRecovery.canResume(checkpoint)); XCTAssertFalse(ExportRecovery.isTerminal(checkpoint)); XCTAssertEqual(ExportRecovery.nextAction(for: checkpoint), .exporting)
        let failed = ExportCheckpoint(exportID: "job", status: .failed); XCTAssertFalse(ExportRecovery.canResume(failed)); XCTAssertEqual(ExportRecovery.nextAction(for: failed), .needsCloudFallback)
    }

    func testUndoRedoHistoryIsBoundedAndClearsRedoOnCommit() {
        var history = UndoRedoHistory(initial: 0, capacity: 2); history.commit(1); history.commit(2); history.commit(3)
        XCTAssertEqual(history.current, 3); XCTAssertEqual(history.undoCount, 2); XCTAssertNil(history.redo())
        XCTAssertEqual(history.undo(), 2); XCTAssertEqual(history.undo(), 1); XCTAssertNil(history.undo()); XCTAssertEqual(history.redo(), 2)
        history.commit(9); XCTAssertEqual(history.current, 9); XCTAssertEqual(history.redoCount, 0)
    }

    func testStorageEstimateAndCrossfade() {
        XCTAssertGreaterThan(StorageEstimate.forAssetBytes(10_000).requiredBytes, 10_000)
        let a = TimelineClip(id: "a", sourceAssetID: "a", sourceDuration: 2)
        let b = TimelineClip(id: "b", sourceAssetID: "b", sourceDuration: 3, timelineStart: 2, transition: Transition(duration: 0.5))
        XCTAssertEqual(TimelineMath.crossfadeOverlap(a, b), 0.5)
    }
}
