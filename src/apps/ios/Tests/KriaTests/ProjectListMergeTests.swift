import XCTest
@testable import Kria

/// `ProjectListMerge` decides which chats stay in the drawer when a page of the server's list arrives. Written from
/// the ways it can go wrong: keeping a chat the server deleted, losing a cached older chat before its page is
/// loaded, or letting a later page discard chats an earlier page owns.
final class ProjectListMergeTests: XCTestCase {
    /// Chat `n`; a smaller `n` is newer.
    private func chat(_ n: Int) -> ProjectSummary {
        ProjectSummary(
            id: UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", n))!,
            title: "Chat \(n)", status: .draft,
            updatedAt: Date(timeIntervalSince1970: Double(10_000 - n) * 60), posterURL: nil
        )
    }
    private func ids(_ projects: [ProjectSummary]) -> [String] { projects.map(\.title) }

    func testFirstPageKeepsCachedOlderChatsWhileMorePagesExist() {
        let merged = ProjectListMerge.apply(existing: (1...6).map(chat), page: (1...2).map(chat), isFirstPage: true, hasMore: true)
        XCTAssertEqual(ids(merged), ["Chat 1", "Chat 2", "Chat 3", "Chat 4", "Chat 5", "Chat 6"])
    }

    func testChatInsideThePagesSpanThatTheServerDroppedIsRemoved() {
        // Chat 2 sits between chats 1 and 3, which the page returned, so its absence means it was deleted.
        let merged = ProjectListMerge.apply(existing: (1...4).map(chat), page: [chat(1), chat(3)], isFirstPage: true, hasMore: true)
        XCTAssertEqual(ids(merged), ["Chat 1", "Chat 3", "Chat 4"])
    }

    func testFirstPageDropsAChatNewerThanAnythingTheServerReturned() {
        let merged = ProjectListMerge.apply(existing: [chat(1), chat(2), chat(3)], page: [chat(2), chat(3)], isFirstPage: true, hasMore: true)
        XCTAssertEqual(ids(merged), ["Chat 2", "Chat 3"])
    }

    func testLaterPageNeverDropsChatsNewerThanItself() {
        let merged = ProjectListMerge.apply(existing: (1...6).map(chat), page: [chat(4), chat(5)], isFirstPage: false, hasMore: true)
        XCTAssertEqual(ids(merged), ["Chat 1", "Chat 2", "Chat 3", "Chat 4", "Chat 5", "Chat 6"])
    }

    func testLastPageDropsOlderCachedChatsTheServerNoLongerLists() {
        let merged = ProjectListMerge.apply(existing: (1...6).map(chat), page: [chat(4), chat(5)], isFirstPage: false, hasMore: false)
        XCTAssertEqual(ids(merged), ["Chat 1", "Chat 2", "Chat 3", "Chat 4", "Chat 5"])
    }

    func testAnEmptyAccountEmptiesTheList() {
        XCTAssertTrue(ProjectListMerge.apply(existing: (1...3).map(chat), page: [], isFirstPage: true, hasMore: false).isEmpty)
    }

    func testThePagesCopyReplacesTheCachedOneAndTheResultIsNewestFirstWithoutDuplicates() {
        var renamed = chat(2)
        renamed.title = "Renamed on the server"
        let merged = ProjectListMerge.apply(existing: [chat(3), chat(2), chat(1)], page: [renamed, chat(1)], isFirstPage: true, hasMore: true)
        XCTAssertEqual(ids(merged), ["Chat 1", "Renamed on the server", "Chat 3"])
        XCTAssertEqual(Set(merged.map(\.id)).count, merged.count)
    }
}
