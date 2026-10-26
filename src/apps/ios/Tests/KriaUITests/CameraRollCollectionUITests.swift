import XCTest

/// The full-screen Camera roll (KRI-944), driven against the offline fixture in `CameraRollSurfaceFixture`:
/// `-camera-roll-collection` hosts the surface on its own, `-camera-roll-surface-fixture` puts the same fixture under
/// the real projects drawer. The fixture's Saturday event holds one tile in every state; its Sunday event holds
/// three ideas.
final class CameraRollCollectionUITests: XCTestCase {
    private let saturday = "Saturday · Bodrum"
    private let sunday = "Sunday · Istanbul"
    private let guideIdea = "11111111-1111-4111-8111-000000000001"

    private func launch(_ extra: [String] = [], environment: [String: String] = [:]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection"] + extra
        for (key, value) in environment { app.launchEnvironment[key] = value }
        app.launch()
        return app
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement { app.descendants(matching: .any)[id].firstMatch }

    private func tile(_ app: XCUIApplication, _ key: String) -> XCUIElement { element(app, "camera-roll-tile-fixture-\(key)") }

    /// A tile's `accessibilityValue` is its `CameraRollItemState` phase.
    private func waitForPhase(_ phase: String, of tile: XCUIElement, timeout: TimeInterval = 15, file: StaticString = #filePath, line: UInt = #line) {
        let reached = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", phase), object: tile)
        XCTAssertEqual(XCTWaiter.wait(for: [reached], timeout: timeout), .completed, "Expected phase \(phase), found \(tile.value as? String ?? "nothing")", file: file, line: line)
    }

    private func scrollTo(_ element: XCUIElement, in app: XCUIApplication, limit: Int = 10) {
        for _ in 0..<limit where !element.exists || !element.isHittable { app.swipeUp() }
    }

    private func attach(_ name: String, _ screenshot: XCUIScreenshot) {
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    // MARK: Surface

    func testDrawerOpensTheCameraRollFullScreen() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-camera-roll-surface-fixture"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let row = app.buttons["camera-roll-collection-open"]
        XCTAssertTrue(row.waitForExistence(timeout: 15), "The drawer offers the Camera roll when the server advertises media collections")
        row.tap()
        XCTAssertTrue(app.staticTexts["camera-roll-title"].waitForExistence(timeout: 10))
        XCTAssertTrue(tile(app, "ready").waitForExistence(timeout: 10), "The surface is workspace content, not a sheet over the chat")
        XCTAssertFalse(app.buttons["camera-roll-collection-open"].isHittable, "The drawer closes behind the surface")
        attach("Camera roll opened from the drawer", app.screenshot())
        // The menu circle still reaches the drawer.
        app.buttons["workspace-menu-toggle"].tap()
        XCTAssertTrue(app.buttons["camera-roll-collection-open"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["camera-roll-collection-open"].isHittable)
    }

    func testEveryItemStateIsVisible() {
        let app = launch(["-collection-mixed-states"])
        XCTAssertTrue(tile(app, "ready").waitForExistence(timeout: 10))
        let expected = [("queued", "queued"), ("uploading", "uploading"), ("analyzing", "analyzing"), ("ready", "ready"),
                        ("needs-retry", "needsRetry"), ("failed", "failed"), ("excluded", "excluded")]
        for (key, phase) in expected { waitForPhase(phase, of: tile(app, key)) }
        XCTAssertTrue(app.staticTexts["Uploading 38%"].exists, "The upload ring is backed by real progress")
        XCTAssertTrue(element(app, "camera-roll-retry-fixture-needs-retry").exists)
        XCTAssertFalse(element(app, "camera-roll-retry-fixture-failed").exists, "An item that can never work offers no retry")
        XCTAssertFalse(element(app, "camera-roll-retry-fixture-excluded").exists)
        XCTAssertTrue(element(app, "camera-roll-progress").exists)
        attach("Contact sheet with every tile state", app.screenshot())
        for (key, _) in expected { attach("Tile: \(key)", tile(app, key).screenshot()) }
    }

    func testReduceMotionKeepsEveryStateReadable() {
        let app = launch(["-collection-mixed-states"], environment: ["UI_TEST_REDUCE_MOTION": "1"])
        XCTAssertTrue(tile(app, "ready").waitForExistence(timeout: 10))
        for (key, phase) in [("queued", "queued"), ("uploading", "uploading"), ("analyzing", "analyzing"), ("ready", "ready"), ("needs-retry", "needsRetry")] {
            waitForPhase(phase, of: tile(app, key))
        }
        XCTAssertTrue(app.staticTexts["Uploading 38%"].exists, "Reduce Motion keeps the percentage and drops the ring")
        XCTAssertTrue(app.staticTexts["Looking"].exists)
        XCTAssertTrue(app.staticTexts["Didn't finish"].exists)
        attach("Contact sheet with Reduce Motion", app.screenshot())
    }

    func testRetryFlipsTheTileToAnalyzing() {
        let app = launch(["-collection-mixed-states"])
        let failing = tile(app, "needs-retry")
        waitForPhase("needsRetry", of: failing)
        let retry = element(app, "camera-roll-retry-fixture-needs-retry")
        XCTAssertTrue(retry.waitForExistence(timeout: 5))
        retry.tap()
        waitForPhase("analyzing", of: failing, timeout: 8)
        XCTAssertFalse(element(app, "camera-roll-retry-fixture-needs-retry").exists)
        attach("Tile after Try again", failing.screenshot())
    }

    // MARK: Ideas

    func testDeletingAnIdeaCanBeUndone() {
        let app = launch(["-collection-mixed-states"])
        let idea = element(app, "camera-roll-idea-\(guideIdea)")
        scrollTo(idea, in: app)
        XCTAssertTrue(idea.waitForExistence(timeout: 10))
        attach("Event with ideas", app.screenshot())
        element(app, "camera-roll-idea-menu-\(guideIdea)").tap()
        app.buttons["Delete idea"].tap()
        let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: idea)
        XCTAssertEqual(XCTWaiter.wait(for: [gone], timeout: 5), .completed)
        XCTAssertTrue(app.staticTexts["Idea deleted"].waitForExistence(timeout: 5))
        attach("Idea deleted with Undo", app.screenshot())
        element(app, "camera-roll-undo").tap()
        XCTAssertTrue(element(app, "camera-roll-idea-\(guideIdea)").waitForExistence(timeout: 5), "Undo brings the idea back where it was")
        XCTAssertFalse(app.staticTexts["Idea deleted"].exists)
    }

    func testDeletingAnEventAsksFirstAndUndoRestoresIt() {
        let app = launch(["-collection-mixed-states"])
        let menu = element(app, "camera-roll-event-menu-\(sunday)")
        scrollTo(menu, in: app)
        menu.tap()
        app.buttons["Delete event"].tap()
        XCTAssertTrue(element(app, "camera-roll-delete-event-card").waitForExistence(timeout: 5), "An event with ideas asks before it goes")
        attach("Delete event confirmation", app.screenshot())
        element(app, "camera-roll-delete-event-keep").tap()
        XCTAssertFalse(element(app, "camera-roll-delete-event-card").waitForExistence(timeout: 2))
        XCTAssertTrue(element(app, "camera-roll-event-\(sunday)").exists, "Keeping it changes nothing")
        scrollTo(menu, in: app)
        menu.tap()
        app.buttons["Delete event"].tap()
        element(app, "camera-roll-delete-event-confirm").tap()
        let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: element(app, "camera-roll-event-\(sunday)"))
        XCTAssertEqual(XCTWaiter.wait(for: [gone], timeout: 5), .completed)
        XCTAssertTrue(element(app, "camera-roll-event-\(saturday)").exists, "Other events are untouched")
        XCTAssertTrue(app.staticTexts["Event deleted"].waitForExistence(timeout: 5))
        element(app, "camera-roll-undo").tap()
        XCTAssertTrue(element(app, "camera-roll-event-\(sunday)").waitForExistence(timeout: 5))
    }

    func testFindIdeasForAnEventThenAnotherIdeaAddsOne() {
        let app = launch(["-collection-mixed-states"])
        let find = element(app, "camera-roll-find-ideas-\(saturday)")
        XCTAssertTrue(find.waitForExistence(timeout: 10))
        find.tap()
        XCTAssertTrue(element(app, "camera-roll-finder-\(saturday)").waitForExistence(timeout: 5))
        attach("Find ideas for an event", app.screenshot())
        element(app, "camera-roll-find-ideas-go-\(saturday)").tap()
        let count = element(app, "camera-roll-idea-count-\(saturday)")
        XCTAssertTrue(count.waitForExistence(timeout: 15))
        XCTAssertEqual(count.label, "1 idea")
        let another = element(app, "camera-roll-another-idea-\(saturday)")
        scrollTo(another, in: app)
        another.tap()
        let two = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", "2 ideas"), object: count)
        XCTAssertEqual(XCTWaiter.wait(for: [two], timeout: 15), .completed, "Another idea adds one and keeps the first")
    }

    func testIdeaOpensItsDetailPage() {
        let app = launch(["-collection-mixed-states"])
        let open = element(app, "camera-roll-idea-open-\(guideIdea)")
        scrollTo(open, in: app)
        open.tap()
        XCTAssertTrue(app.staticTexts["Three stops for a perfect day in Istanbul"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["story-choose-\(guideIdea)"].exists)
        XCTAssertTrue(app.buttons["story-not-this-one"].exists)
        attach("Idea detail", app.screenshot())
        app.buttons["camera-roll-back"].tap()
        XCTAssertTrue(element(app, "camera-roll-idea-\(guideIdea)").waitForExistence(timeout: 5))
    }

    // MARK: Events

    func testSelectAndMoveItemsBetweenEvents() {
        let app = launch(["-collection-mixed-states"])
        let menu = element(app, "camera-roll-event-menu-\(saturday)")
        XCTAssertTrue(menu.waitForExistence(timeout: 10))
        menu.tap()
        app.buttons["Select and move items"].tap()
        XCTAssertTrue(element(app, "camera-roll-event-name").waitForExistence(timeout: 5))
        tile(app, "ready").tap()
        tile(app, "ready-video").tap()
        XCTAssertEqual(element(app, "camera-roll-edit-count").label, "2 selected")
        attach("Select and move", app.screenshot())
        element(app, "camera-roll-move-to-\(sunday)").tap()
        XCTAssertEqual(element(app, "camera-roll-edit-count").label, "Select items", "The moved items leave the event's grid")
        XCTAssertFalse(tile(app, "ready").exists)
        element(app, "camera-roll-edit-done").tap()
        XCTAssertTrue(element(app, "camera-roll-event-\(sunday)").waitForExistence(timeout: 5))
    }

    func testTastePanelOpensFromTheHeaderAndReturns() {
        let app = launch(["-collection-mixed-states"])
        let taste = app.buttons["camera-roll-taste"]
        XCTAssertTrue(taste.waitForExistence(timeout: 10))
        taste.tap()
        XCTAssertTrue(app.staticTexts["What makes a good post for you?"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label ==[c] %@", "Posts you like")).firstMatch.exists)
        attach("Your taste", app.screenshot())
        app.buttons["camera-roll-back"].tap()
        XCTAssertTrue(app.staticTexts["camera-roll-title"].waitForExistence(timeout: 5))
    }

    // MARK: First run and access

    func testEmptyCameraRollInvitesTheFirstChoice() {
        let app = launch(["-collection-empty"])
        XCTAssertTrue(app.staticTexts["What did you film lately?"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["camera-roll-choose"].exists)
        XCTAssertTrue(app.buttons["camera-roll-saved"].exists)
        attach("Empty camera roll", app.screenshot())
    }

    func testAccessNoticesExplainLimitedAndRemovedAccess() {
        let app = launch(["-collection-mixed-states", "-collection-access-notices"])
        XCTAssertTrue(element(app, "camera-roll-access-removed").waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["camera-roll-open-settings"].exists)
        XCTAssertTrue(element(app, "camera-roll-limited-access").exists)
        XCTAssertTrue(app.staticTexts["Kria can only see the 120 items you shared. Everything else stays private."].exists)
        attach("Limited and removed access", app.screenshot())
    }

    // MARK: Collection changes

    private func findIdeas(_ app: XCUIApplication, in group: String) {
        let find = element(app, "camera-roll-find-ideas-\(group)")
        XCTAssertTrue(find.waitForExistence(timeout: 10))
        find.tap()
        element(app, "camera-roll-find-ideas-go-\(group)").tap()
    }

    func testUnavailableAnalysisIsNotAnEditConflict() {
        let app = launch(["-collection-mixed-states", "-collection-cache-miss"])
        findIdeas(app, in: saturday)
        let notice = element(app, "camera-roll-notice")
        XCTAssertTrue(notice.waitForExistence(timeout: 10))
        XCTAssertEqual(notice.label, "This footage needs analysis. Your collection is saved; new analysis is unavailable right now.")
        XCTAssertFalse(element(app, "camera-roll-review-latest").exists, "Nothing changed elsewhere, so there is no latest collection to open")
        XCTAssertTrue(element(app, "camera-roll-event-\(saturday)").exists, "The collection is untouched")
    }

    func testRealConflictCanOpenLatestWithoutLosingLocalCollection() {
        let app = launch(["-collection-mixed-states", "-collection-conflict"])
        findIdeas(app, in: saturday)
        let latest = element(app, "camera-roll-review-latest")
        XCTAssertTrue(latest.waitForExistence(timeout: 10), "A real conflict offers the latest collection")
        attach("Conflict with Review latest", app.screenshot())
        latest.tap()
        app.buttons["Open latest collection"].tap()
        let notice = element(app, "camera-roll-notice")
        let opened = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", "Opened the latest collection. Your local changes are saved separately."), object: notice)
        XCTAssertEqual(XCTWaiter.wait(for: [opened], timeout: 10), .completed)
        XCTAssertTrue(element(app, "camera-roll-event-\(sunday)").waitForExistence(timeout: 5))
        XCTAssertFalse(element(app, "camera-roll-event-\(saturday)").exists, "The other phone's collection replaces this one")
        XCTAssertFalse(element(app, "camera-roll-review-latest").exists)
        attach("Latest collection opened", app.screenshot())
    }

    /// Undo can arrive while the server is still deleting the event. The restore must land after the delete, or the
    /// ideas are gone on the server while the phone still shows them.
    func testUndoTappedWhileTheEventDeleteIsInFlightKeepsItsIdeas() {
        let app = launch(["-collection-mixed-states", "-collection-slow-group-delete"])
        let menu = element(app, "camera-roll-event-menu-\(sunday)")
        scrollTo(menu, in: app)
        menu.tap()
        app.buttons["Delete event"].tap()
        element(app, "camera-roll-delete-event-confirm").tap()
        XCTAssertTrue(app.staticTexts["Event deleted"].waitForExistence(timeout: 5))
        element(app, "camera-roll-undo").tap()
        XCTAssertTrue(element(app, "camera-roll-event-\(sunday)").waitForExistence(timeout: 5))
        // The server finishes deleting 1.5 s in; then the restore runs. Reading its ideas back shows which won.
        let editMenu = element(app, "camera-roll-event-menu-\(sunday)")
        scrollTo(editMenu, in: app)
        editMenu.tap()
        app.buttons["Select and move items"].tap()
        XCTAssertTrue(element(app, "camera-roll-edit-done").waitForExistence(timeout: 5))
        element(app, "camera-roll-edit-done").tap()
        // Undo already put the cards back on the phone, so let the delete, restore and read-back all finish first.
        let settle = XCTestExpectation(description: "server settles"); settle.isInverted = true
        _ = XCTWaiter.wait(for: [settle], timeout: 4)
        let idea = element(app, "camera-roll-idea-\(guideIdea)")
        scrollTo(idea, in: app)
        XCTAssertTrue(idea.exists, "The restore ran after the delete, so the server still holds the ideas")
        attach("Ideas after delete and Undo", app.screenshot())
    }
}
