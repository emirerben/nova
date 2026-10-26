import XCTest
@testable import Kria

/// KRI-944: ideas are kept per event. A collection saved before that stored one `savedIdeas` object; reading it must
/// not lose the collection (its items, uploads and corrections), only drop the ideas, which the server still holds.
final class CameraRollLegacyIdeasTests: XCTestCase {
    func testCollectionSavedWithOneIdeaSetStillLoadsAndDropsOnlyTheIdeas() throws {
        var collection = try CameraRollCollection(name: "Weekend", identifiers: ["a", "b"])
        collection.setGroup("Saturday", for: "a")
        collection.serverRevision = 4
        var legacy = try JSONSerialization.jsonObject(with: JSONEncoder().encode(collection)) as! [String: Any]
        legacy.removeValue(forKey: "ideasByGroup")
        legacy.removeValue(forKey: "eventTitles")
        legacy["savedIdeas"] = ["revision": 4, "query": "old", "selection": ["name": "Weekend", "members": []],
                                "result": ["stories": [], "coverage": NSNull()]] as [String: Any]
        let resumed = try JSONDecoder().decode(CameraRollCollection.self, from: JSONSerialization.data(withJSONObject: legacy))
        XCTAssertEqual(resumed.entries.map(\.id), ["a", "b"])
        XCTAssertEqual(resumed.group(for: "a"), "Saturday")
        XCTAssertEqual(resumed.serverRevision, 4)
        XCTAssertTrue(resumed.ideasByGroup.isEmpty)
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(resumed), as: UTF8.self).contains("savedIdeas"))
    }

    func testRenamingAnEventKeepsItsServerKey() throws {
        var collection = try CameraRollCollection(name: "Weekend", identifiers: ["a"])
        collection.setGroup("Event 1", for: "a")
        collection.rename(group: "Event 1", to: "Saturday in Bodrum")
        XCTAssertEqual(collection.group(for: "a"), "Event 1", "Ideas are keyed by the server's group, so a rename cannot orphan them")
        XCTAssertEqual(collection.title(forGroup: "Event 1"), "Saturday in Bodrum")
        collection.rename(group: "Event 1", to: "  ")
        XCTAssertEqual(collection.title(forGroup: "Event 1"), "Event 1")
    }
}
