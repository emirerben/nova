import XCTest
@testable import Kria

final class GeneratedCardTests: XCTestCase {
    /// Cross-platform parity vector: `generated_overlay.event_id` in the API (`test_g3_the_event_id_matches_the_phone`)
    /// pins the same string. The phone finds the image a tap made by this id, so a drift loses the image.
    func testEventIDMatchesTheServersDerivation() {
        XCTAssertEqual(GeneratedCardEvent.wireID(setID: "sg_fixture_ready_v2", cardID: "ss_fixture_generated_overlay"),
                       "2dfd7786-5814-5429-ab12-84d67fcc8f66")
        XCTAssertEqual(GeneratedCardEvent.wireID(setID: "sg_x", cardID: "ss_y"), "84e4bcc5-e514-5b3c-bbcd-554ab7eb3b78")
    }

    func testOnlyAnAsyncGenerateCardNeedsTheCreatorsYes() {
        let paid = SuggestionCard(id: "ss_a", kind: "generated_overlay", title: "Add a generated image", prompt: "Add a generated image overlay",
                                  actionType: "async_generate", costUSD: 0.04)
        let free = SuggestionCard(id: "ss_b", kind: "captions", title: "Add captions", prompt: "Add captions", actionType: "editor_ops")
        XCTAssertTrue(paid.needsSpendConsent)
        XCTAssertFalse(free.needsSpendConsent)
        XCTAssertEqual(paid.costLabel, "$0.04")
    }
}
