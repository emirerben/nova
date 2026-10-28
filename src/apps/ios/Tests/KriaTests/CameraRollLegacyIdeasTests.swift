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

    /// KRI-1005: titles were kept as a phone-only `eventTitles` map. A file written then must keep every title, and
    /// an event's About and an empty new event must survive a save and resume.
    func testPhoneOnlyTitlesBecomeEventsAndSurviveResume() throws {
        var collection = try CameraRollCollection(name: "Weekend", identifiers: ["a"])
        collection.setGroup("Event 1", for: "a")
        var legacy = try JSONSerialization.jsonObject(with: JSONEncoder().encode(collection)) as! [String: Any]
        legacy.removeValue(forKey: "events")
        legacy["eventTitles"] = ["Event 1": "Cunda Island"]
        var resumed = try JSONDecoder().decode(CameraRollCollection.self, from: JSONSerialization.data(withJSONObject: legacy))
        XCTAssertEqual(resumed.title(forGroup: "Event 1"), "Cunda Island")
        XCTAssertEqual(resumed.events, [CameraRollEventInfo(group: "Event 1", title: "Cunda Island", about: "")])
        XCTAssertFalse(resumed.serverStoresEvents, "A file from before events never told the server about them")

        resumed.setAbout(group: "Event 1", to: "Stone streets and the sea")
        resumed.addEvent(group: "New event", title: "Ayvalık", about: "")
        let again = try JSONDecoder().decode(CameraRollCollection.self, from: JSONEncoder().encode(resumed))
        XCTAssertEqual(again.about(forGroup: "Event 1"), "Stone streets and the sea")
        XCTAssertEqual(again.title(forGroup: "New event"), "Ayvalık", "An event with nothing in it yet is kept")
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(again), as: UTF8.self).contains("eventTitles"))
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
