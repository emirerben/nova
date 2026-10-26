import XCTest
@testable import Kria

/// The suggestion is optional: builds must read responses from servers that predate it.
final class CameraRollResearchSuggestionTests: XCTestCase {
    private func decode(_ extra: [String: Any]) throws -> CameraRollStories {
        let json = ["stories": [], "revision": 1].merging(extra) { $1 }
        return try JSONDecoder().decode(CameraRollStories.self, from: JSONSerialization.data(withJSONObject: json))
    }

    func testSuggestionDecodesAndIsAbsentFromOlderServers() throws {
        XCTAssertEqual(try decode(["suggested_research_topic": "Troy"]).suggestedResearchTopic, "Troy")
        XCTAssertNil(try decode([:]).suggestedResearchTopic)
    }
}
