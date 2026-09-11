import Foundation
import XCTest
@testable import KriaMediaEngine

final class ClipTransitionTimingTests: XCTestCase {
    func testExtendedTransitionsCannotUseLegacyCrossfadeCapability() throws {
        for kind in [Transition.Kind.fadeBlack, .fadeWhite, .wipeLeft, .wipeRight] {
            let recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [
                TimelineClip(id: "first", sourceAssetID: "a", sourceDuration: 2),
                TimelineClip(id: "second", sourceAssetID: "a", sourceDuration: 2, timelineStart: 1.8, transition: Transition(kind: kind, duration: 0.2))
            ])], requiredCapabilities: [.crossfade])
            XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(recipe)), recipe)
            let decision = CapabilityNegotiator().decide(for: recipe)
            XCTAssertEqual(decision.route, .cloud)
            XCTAssertTrue(decision.missingCapabilities.contains(.clipTransitions))
        }
    }

    func testFadeCurvesAndWipeBoundariesMatchFFmpegFrames() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_clip_transitions.json").standardizedFileURL
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as? [String: Any])
        let outgoing = try XCTUnwrap(document["outgoing_luma"] as? Double)
        let incoming = try XCTUnwrap(document["incoming_luma"] as? Double)
        for row in try XCTUnwrap(document["rows"] as? [[String: Any]]) {
            let effect = try XCTUnwrap(row["effect"] as? String)
            let progress = try XCTUnwrap(row["progress"] as? Double)
            let expected = try XCTUnwrap(row["luma_row"] as? [Double])
            if effect.hasPrefix("fade") {
                let weights = ClipTransitionTiming.fadeWeights(progress: progress)
                let luma = outgoing * weights.outgoing + incoming * weights.incoming
                    + (effect == "fadeblack" ? 0 : 255) * weights.background
                for value in expected { XCTAssertEqual(floor(luma), value, accuracy: 1, "\(effect) \(progress)") }
            } else {
                let range = ClipTransitionTiming.wipeIncomingRange(width: expected.count, progress: progress, left: effect == "wipeleft")
                for (x, value) in expected.enumerated() { XCTAssertEqual(range.contains(x) ? incoming : outgoing, value, "\(effect) \(progress) x=\(x)") }
            }
        }
    }
}
