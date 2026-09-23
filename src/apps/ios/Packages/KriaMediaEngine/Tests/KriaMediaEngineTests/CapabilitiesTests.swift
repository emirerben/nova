import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-118: a renderer-version/schema mismatch used to read as the same
/// generic "route to cloud" reason as a genuinely unsupported edit feature,
/// and the storage gate scaled against source footage size instead of the
/// bounded output a render actually writes. These pin both fixes.
final class CapabilitiesTests: XCTestCase {
    private func recipe() -> EditRecipe {
        let clip = TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2)
        return EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a.mp4")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [clip])])
    }

    func testRendererVersionMismatchReasonRoutesToRendererOutdated() {
        var recipe = recipe()
        recipe.rendererVersion = "kria-ios-999"
        let decision = CapabilityNegotiator().decide(for: recipe)
        XCTAssertEqual(decision.route, .cloud)
        XCTAssertEqual(DeviceRenderFailureReasonCode.forRouteDecision(reason: decision.reason), .rendererOutdated)
    }

    /// `recipe.validate()` throwing `RecipeError.unsupportedSchema` (the
    /// renderer/version guard above passes, but the schema itself is outside
    /// what this build's `validate()` recognizes) must land in the same
    /// "update the app" bucket as the plain version-string mismatch, not the
    /// unsupported-FEATURE bucket a specific missing capability gets.
    func testValidateSchemaMismatchReasonRoutesToRendererOutdated() {
        var recipe = recipe()
        recipe.schemaVersion = 99
        recipe.rendererVersion = "kria-ios-99"
        let decision = CapabilityNegotiator().decide(for: recipe)
        XCTAssertEqual(decision.route, .cloud)
        XCTAssertEqual(DeviceRenderFailureReasonCode.forRouteDecision(reason: decision.reason), .rendererOutdated)
    }

    /// A missing capability within an otherwise-understood, current-schema
    /// recipe is a different bucket: "start a new edit", not "update the app".
    func testMissingCapabilityReasonStaysUnsupportedRecipe() {
        let clip = TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2, rate: 2, text: TextTreatment(text: "Hello"))
        let recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a.mp4")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [clip])])
        let minimal = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: [.basicComposition, .local1080Export]))
        let decision = minimal.decide(for: recipe)
        XCTAssertEqual(decision.route, .cloud)
        XCTAssertEqual(DeviceRenderFailureReasonCode.forRouteDecision(reason: decision.reason), .unsupportedRecipe)
    }

    func testEstimatedOutputScalesWithDurationNotSourceSize() {
        let short = StorageEstimate.forEstimatedOutput(durationS: 5).requiredBytes
        let long = StorageEstimate.forEstimatedOutput(durationS: 55).requiredBytes
        XCTAssertGreaterThan(long, short)
        // A short-form export (well under the sub-60s target) needs on the
        // order of a couple hundred MB, not gigabytes -- the whole point of
        // decoupling this from source footage size.
        XCTAssertLessThan(long, 1024 * 1024 * 1024)
    }

    func testEstimatedOutputScalesWithPendingProjectsCount() {
        let one = StorageEstimate.forEstimatedOutput(durationS: 30, pendingProjects: 0).requiredBytes
        let three = StorageEstimate.forEstimatedOutput(durationS: 30, pendingProjects: 2).requiredBytes
        XCTAssertGreaterThan(three, one)
    }

    func testEstimatedOutputHasAGenerousFloorEvenForATrivialClip() {
        let floor = StorageEstimate.forEstimatedOutput(durationS: 0).requiredBytes
        XCTAssertGreaterThanOrEqual(floor, 200 * 1024 * 1024)
    }

    func testEstimatedOutputFailsSafelyOnOverflow() {
        XCTAssertEqual(StorageEstimate.forEstimatedOutput(durationS: .infinity).requiredBytes, .max)
        XCTAssertEqual(StorageEstimate.forEstimatedOutput(durationS: 60, pendingProjects: .max).requiredBytes, .max)
    }
}
