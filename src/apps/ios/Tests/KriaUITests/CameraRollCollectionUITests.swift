import XCTest

/// The full-screen Camera roll (KRI-944, KRI-1005), driven against the offline fixture in `CameraRollSurfaceFixture`:
/// `-camera-roll-collection` hosts the surface on its own, `-camera-roll-surface-fixture` puts the same fixture under
/// the real projects drawer. Home lists the events; each opens its own page. The fixture's Saturday event holds 40
/// items, one tile in every state first; its Sunday event holds six items and three ideas; Cunda Island holds none.
final class CameraRollCollectionUITests: XCTestCase {
    private let saturday = "Saturday · Bodrum"
    private let sunday = "Sunday · Istanbul"
    private let cunda = "Cunda Island"
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

    private func storyID(_ index: Int) -> String { String(format: "11111111-1111-4111-8111-%012d", index) }

    /// A tile's `accessibilityValue` is its `CameraRollItemState` phase.
    private func waitForPhase(_ phase: String, of tile: XCUIElement, timeout: TimeInterval = 15, file: StaticString = #filePath, line: UInt = #line) {
        let reached = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", phase), object: tile)
        XCTAssertEqual(XCTWaiter.wait(for: [reached], timeout: timeout), .completed, "Expected phase \(phase), found \(tile.value as? String ?? "nothing")", file: file, line: line)
    }

    private func wait(_ element: XCUIElement, _ format: String, _ args: Any..., timeout: TimeInterval = 10) -> Bool {
        let met = XCTNSPredicateExpectation(predicate: NSPredicate(format: format, argumentArray: args), object: element)
        return XCTWaiter.wait(for: [met], timeout: timeout) == .completed
    }

    /// Swipes until the element is on screen, then lets the scroll coast to a stop: a tap during the coast only
    /// stops the scroll and never reaches the button.
    private func scrollTo(_ element: XCUIElement, in app: XCUIApplication, limit: Int = 10) {
        var swiped = false
        for _ in 0..<limit where !element.exists || !element.isHittable { app.swipeUp(); swiped = true }
        guard swiped else { return }
        let coast = XCTestExpectation(description: "scroll settles"); coast.isInverted = true
        _ = XCTWaiter.wait(for: [coast], timeout: 1)
    }

    private func attach(_ name: String, _ screenshot: XCUIScreenshot) {
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func card(_ app: XCUIApplication, _ group: String) -> XCUIElement { element(app, "camera-roll-event-\(group)") }

    /// Taps the event's card on home and waits for its page.
    private func openEvent(_ app: XCUIApplication, _ group: String, file: StaticString = #filePath, line: UInt = #line) {
        let card = card(app, group)
        XCTAssertTrue(card.waitForExistence(timeout: 10), "Home lists \(group)", file: file, line: line)
        scrollTo(card, in: app)
        card.tap()
        XCTAssertTrue(element(app, "camera-roll-event-page-\(group)").waitForExistence(timeout: 5), "\(group) opens its page", file: file, line: line)
    }

    private func showAll(_ app: XCUIApplication, _ group: String) {
        let more = element(app, "camera-roll-expand-\(group)")
        XCTAssertTrue(more.waitForExistence(timeout: 5))
        more.tap()
    }

    /// What the fixture server stored for each event, as "group|title|about" lines.
    private func serverEvents(_ app: XCUIApplication) -> XCUIElement { element(app, "camera-roll-fixture-server-events") }

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
        XCTAssertTrue(card(app, saturday).waitForExistence(timeout: 10), "The surface is workspace content, not a sheet over the chat")
        XCTAssertFalse(app.buttons["camera-roll-collection-open"].isHittable, "The drawer closes behind the surface")
        attach("Camera roll opened from the drawer", app.screenshot())
        // The menu circle still reaches the drawer.
        app.buttons["workspace-menu-toggle"].tap()
        XCTAssertTrue(app.buttons["camera-roll-collection-open"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["camera-roll-collection-open"].isHittable)
    }

    func testHomeListsEventCardsThatOpenTheirPages() {
        let app = launch(["-collection-mixed-states"])
        let saturdayCard = card(app, saturday)
        XCTAssertTrue(saturdayCard.waitForExistence(timeout: 10))
        XCTAssertTrue(card(app, sunday).exists)
        XCTAssertTrue(card(app, cunda).exists, "An event with nothing in it yet is still listed")
        XCTAssertTrue(wait(card(app, sunday), "value == %@", "3 ideas"), "A card says how many ideas its event has")
        XCTAssertEqual(saturdayCard.value as? String, "Find ideas", "An event without ideas offers to find some")
        XCTAssertFalse(app.staticTexts.containing(NSPredicate(format: "label CONTAINS[c] %@", "all ready")).firstMatch.exists,
                       "A settled event has no readiness line")
        XCTAssertTrue(app.buttons["camera-roll-new-event"].exists, "Adding an event is a top-level action")
        attach("Events list", app.screenshot())
        openEvent(app, sunday)
        XCTAssertEqual(element(app, "camera-roll-event-title").label, sunday)
        XCTAssertTrue(element(app, "camera-roll-idea-\(guideIdea)").waitForExistence(timeout: 5), "The event's ideas live on its page")
        attach("Event page", app.screenshot())
        app.buttons["camera-roll-back"].tap()
        XCTAssertTrue(card(app, saturday).waitForExistence(timeout: 5))
    }

    func testEventPageShowsOneRowOfMediaUntilShowAll() {
        let app = launch(["-collection-mixed-states"])
        openEvent(app, saturday)
        let firstRow = ["queued", "uploading", "analyzing"].map { tile(app, $0) }
        for item in firstRow { XCTAssertTrue(item.waitForExistence(timeout: 5)) }
        XCTAssertEqual(Set(firstRow.map { Int($0.frame.minY.rounded()) }).count, 1, "The first items share one row")
        XCTAssertFalse(tile(app, "ready").exists, "A 40-item event shows one row until Show all")
        attach("Event collapsed to one row", app.screenshot())
        showAll(app, saturday)
        XCTAssertTrue(tile(app, "ready").waitForExistence(timeout: 5))
        XCTAssertTrue(tile(app, "saturday-21").exists)
        attach("Event after Show all", app.screenshot())
        app.buttons["camera-roll-back"].tap()
        openEvent(app, sunday)
        XCTAssertTrue(tile(app, "sunday-1").waitForExistence(timeout: 5))
        XCTAssertFalse(tile(app, "sunday-4").exists, "An event with ideas starts collapsed too")
    }

    func testEveryItemStateIsVisible() {
        let app = launch(["-collection-mixed-states"])
        XCTAssertTrue(element(app, "camera-roll-progress").waitForExistence(timeout: 10))
        XCTAssertTrue(wait(card(app, saturday), "label CONTAINS %@", "still looking"), "A card names its unfinished work")
        openEvent(app, saturday)
        showAll(app, saturday)
        XCTAssertTrue(tile(app, "ready").waitForExistence(timeout: 10))
        let expected = [("queued", "queued"), ("uploading", "uploading"), ("analyzing", "analyzing"), ("ready", "ready"),
                        ("needs-retry", "needsRetry"), ("failed", "failed"), ("excluded", "excluded")]
        for (key, phase) in expected { waitForPhase(phase, of: tile(app, key)) }
        XCTAssertTrue(app.staticTexts["Uploading 38%"].exists, "The upload ring is backed by real progress")
        XCTAssertTrue(element(app, "camera-roll-retry-fixture-needs-retry").exists)
        XCTAssertFalse(element(app, "camera-roll-retry-fixture-failed").exists, "An item that can never work offers no retry")
        XCTAssertFalse(element(app, "camera-roll-retry-fixture-excluded").exists)
        attach("Contact sheet with every tile state", app.screenshot())
        for (key, _) in expected { attach("Tile: \(key)", tile(app, key).screenshot()) }
    }

    func testReduceMotionKeepsEveryStateReadable() {
        let app = launch(["-collection-mixed-states"], environment: ["UI_TEST_REDUCE_MOTION": "1"])
        openEvent(app, saturday)
        showAll(app, saturday)
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
        openEvent(app, saturday)
        showAll(app, saturday)
        let failing = tile(app, "needs-retry")
        waitForPhase("needsRetry", of: failing)
        let retry = element(app, "camera-roll-retry-fixture-needs-retry")
        XCTAssertTrue(retry.waitForExistence(timeout: 5))
        retry.tap()
        waitForPhase("analyzing", of: failing, timeout: 8)
        XCTAssertFalse(element(app, "camera-roll-retry-fixture-needs-retry").exists)
        attach("Tile after Try again", failing.screenshot())
    }

    // MARK: Events

    func testNewEventWithTitleAndAboutReachesTheServer() {
        let app = launch(["-collection-mixed-states"])
        let add = app.buttons["camera-roll-new-event"]
        XCTAssertTrue(add.waitForExistence(timeout: 10))
        add.tap()
        let title = element(app, "camera-roll-new-event-title")
        XCTAssertTrue(title.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["camera-roll-new-event-choose"].exists, "The sheet can go straight to Photos")
        title.tap()
        title.typeText("Ayvalık weekend")
        let about = element(app, "camera-roll-new-event-about")
        about.tap()
        about.typeText("Old stone streets and the sea, for people planning a trip")
        attach("New event sheet", app.screenshot())
        app.buttons["camera-roll-new-event-create"].tap()
        XCTAssertTrue(element(app, "camera-roll-event-page-New event").waitForExistence(timeout: 5), "Creating opens the new event's page")
        XCTAssertEqual(element(app, "camera-roll-event-title").label, "Ayvalık weekend")
        XCTAssertTrue(wait(element(app, "camera-roll-event-about-New event"), "value CONTAINS %@", "Old stone streets"))
        XCTAssertTrue(element(app, "camera-roll-event-add-New event").exists, "An empty event offers to add footage")
        XCTAssertTrue(wait(serverEvents(app), "label CONTAINS %@", "New event|Ayvalık weekend|Old stone streets and the sea, for people planning a trip"),
                      "The title and about reach the server")
        attach("New event page", app.screenshot())
        app.buttons["camera-roll-back"].tap()
        XCTAssertTrue(wait(card(app, "New event"), "label CONTAINS %@", "Ayvalık weekend"))
    }

    func testAboutThisEventIsSavedToTheServer() {
        let app = launch(["-collection-mixed-states"])
        openEvent(app, sunday)
        let about = element(app, "camera-roll-event-about-\(sunday)")
        XCTAssertTrue(about.waitForExistence(timeout: 10), "A server that stores events gets an About editor")
        about.tap()
        about.typeText("Ferry day with friends across the Bosphorus")
        app.buttons["camera-roll-event-about-save"].tap()
        XCTAssertTrue(wait(serverEvents(app), "label CONTAINS %@", "\(sunday)|\(sunday)|Ferry day with friends across the Bosphorus"))
        XCTAssertTrue(wait(serverEvents(app), "label CONTAINS %@", "\(cunda)|\(cunda)|"), "The first sync also sends the events kept on the phone")
        attach("About this event saved", app.screenshot())
        app.buttons["camera-roll-back"].tap()
        openEvent(app, sunday)
        XCTAssertTrue(wait(element(app, "camera-roll-event-about-\(sunday)"), "value CONTAINS %@", "Ferry day"))
    }

    func testAnOlderServerKeepsTitlesOnThePhoneAndHidesAbout() {
        let app = launch(["-collection-mixed-states", "-collection-legacy-server"])
        openEvent(app, sunday)
        XCTAssertFalse(element(app, "camera-roll-event-about-\(sunday)").waitForExistence(timeout: 3), "Nothing to save About to")
        element(app, "camera-roll-event-menu-\(sunday)").tap()
        app.buttons["Rename"].tap()
        let name = element(app, "camera-roll-event-name")
        XCTAssertTrue(name.waitForExistence(timeout: 5))
        name.clearAndType("Ferry Sunday")
        element(app, "camera-roll-edit-done").tap()
        XCTAssertTrue(wait(element(app, "camera-roll-event-title"), "label == %@", "Ferry Sunday"), "The rename stays on the phone")
        XCTAssertFalse(element(app, "camera-roll-notice").waitForExistence(timeout: 3), "The older server is never sent events it would refuse")
    }

    func testSelectAndMoveItemsBetweenEvents() {
        let app = launch(["-collection-mixed-states"])
        openEvent(app, saturday)
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
        app.buttons["camera-roll-back"].tap()
        XCTAssertTrue(wait(card(app, sunday), "label CONTAINS %@", "8 items"), "Sunday now holds the two moved items")
    }

    // MARK: Ideas

    func testDeletingAnIdeaCanBeUndone() {
        let app = launch(["-collection-mixed-states"])
        openEvent(app, sunday)
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
        openEvent(app, sunday)
        let menu = element(app, "camera-roll-event-menu-\(sunday)")
        menu.tap()
        app.buttons["Delete event"].tap()
        XCTAssertTrue(element(app, "camera-roll-delete-event-card").waitForExistence(timeout: 5), "An event with ideas asks before it goes")
        attach("Delete event confirmation", app.screenshot())
        element(app, "camera-roll-delete-event-keep").tap()
        XCTAssertFalse(element(app, "camera-roll-delete-event-card").waitForExistence(timeout: 2))
        XCTAssertTrue(element(app, "camera-roll-event-page-\(sunday)").exists, "Keeping it changes nothing")
        menu.tap()
        app.buttons["Delete event"].tap()
        element(app, "camera-roll-delete-event-confirm").tap()
        let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: card(app, sunday))
        XCTAssertEqual(XCTWaiter.wait(for: [gone], timeout: 5), .completed)
        XCTAssertTrue(card(app, saturday).waitForExistence(timeout: 5), "Deleting returns home; other events are untouched")
        XCTAssertTrue(app.staticTexts["Event deleted"].waitForExistence(timeout: 5))
        element(app, "camera-roll-undo").tap()
        XCTAssertTrue(card(app, sunday).waitForExistence(timeout: 5))
    }

    func testFindIdeasForAnEventThenAnotherIdeaAddsOne() {
        let app = launch(["-collection-mixed-states"])
        findIdeas(app, in: saturday)
        attach("Finding ideas for an event", app.screenshot())
        XCTAssertTrue(element(app, "camera-roll-idea-\(storyID(101))").waitForExistence(timeout: 15))
        let another = element(app, "camera-roll-another-idea-\(saturday)")
        scrollTo(another, in: app)
        another.tap()
        XCTAssertTrue(element(app, "camera-roll-idea-\(storyID(102))").waitForExistence(timeout: 15), "Another idea adds one")
        XCTAssertTrue(element(app, "camera-roll-idea-\(storyID(101))").exists, "and keeps the first")
        app.buttons["camera-roll-back"].tap()
        XCTAssertTrue(wait(card(app, saturday), "value == %@", "2 ideas"))
    }

    func testIdeaOpensItsDetailPage() {
        let app = launch(["-collection-mixed-states"])
        openEvent(app, sunday)
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

    // MARK: Using an idea

    private let ferryIdea = "11111111-1111-4111-8111-000000000002"
    private let simitIdea = "11111111-1111-4111-8111-000000000003"

    func testEveryIdeaCardShowsItsClipsAndUse() {
        let app = launch(["-collection-mixed-states"])
        openEvent(app, sunday)
        for idea in [guideIdea, ferryIdea, simitIdea] {
            let use = element(app, "camera-roll-idea-use-\(idea)")
            scrollTo(use, in: app)
            XCTAssertTrue(use.waitForExistence(timeout: 5), "Every idea offers Use this idea")
            XCTAssertTrue(element(app, "camera-roll-idea-clips-\(idea)").exists, "Every idea shows its clips")
        }
        XCTAssertEqual(app.descendants(matching: .any).matching(identifier: "camera-roll-idea-best-fit").count, 1, "Only the first idea is the best fit")
        attach("Every idea card", app.screenshot())
    }

    func testOpenQuestionsSayWhyUseWaitsAndAnsweringEnablesIt() {
        let app = launch(["-collection-mixed-states"])
        openEvent(app, sunday)
        let use = element(app, "camera-roll-idea-use-\(guideIdea)")
        scrollTo(use, in: app)
        XCTAssertTrue(use.waitForExistence(timeout: 5))
        XCTAssertFalse(use.isEnabled, "An idea with an open question can't be used yet")
        XCTAssertEqual(element(app, "camera-roll-idea-use-reason-\(guideIdea)").label, "Answer or skip the question above to use this idea.")
        XCTAssertTrue(app.staticTexts["What was the name of the ferry café?"].exists, "The question is on the card")
        attach("Idea waiting on a question", app.screenshot())
        let skip = element(app, "story-question-skip-q1")
        scrollTo(skip, in: app)
        skip.tap()
        XCTAssertTrue(wait(use, "enabled == true"), "Skipping the question enables Use")
        XCTAssertFalse(element(app, "camera-roll-idea-use-reason-\(guideIdea)").exists)
    }

    func testUseShowsEachStepThenOpensTheChat() {
        let app = launch(["-collection-mixed-states", "-collection-slow-use"])
        openEvent(app, sunday)
        let use = element(app, "camera-roll-idea-use-\(ferryIdea)")
        scrollTo(use, in: app)
        use.tap()
        XCTAssertTrue(element(app, "camera-roll-use-sheet").waitForExistence(timeout: 5), "Use opens the progress sheet at once")
        let clips = element(app, "camera-roll-use-step-clips")
        XCTAssertTrue(wait(clips, "label CONTAINS %@", "Uploading clips 1/2", timeout: 10), "Clip uploads count up")
        attach("Use progress", app.screenshot())
        XCTAssertTrue(wait(element(app, "camera-roll-use-step-chat"), "value == %@", "In progress", timeout: 15))
        XCTAssertTrue(element(app, "camera-roll-opened-chat").waitForExistence(timeout: 15), "Success goes straight to the chat")
    }

    func testUseKeepsGoingAfterTheSheetCloses() {
        let app = launch(["-collection-mixed-states", "-collection-slow-use"])
        openEvent(app, sunday)
        let use = element(app, "camera-roll-idea-use-\(ferryIdea)")
        scrollTo(use, in: app)
        use.tap()
        XCTAssertTrue(element(app, "camera-roll-use-sheet").waitForExistence(timeout: 5))
        element(app, "camera-roll-use-close").tap()
        XCTAssertTrue(wait(use, "label CONTAINS %@", "Show progress"), "The card says the idea is still being built")
        use.tap()
        XCTAssertTrue(element(app, "camera-roll-use-sheet").waitForExistence(timeout: 5), "The sheet reopens on the same work")
        element(app, "camera-roll-use-close").tap()
        XCTAssertTrue(element(app, "camera-roll-opened-chat").waitForExistence(timeout: 20), "Closing the sheet never cancels the work")
    }

    func testAFailedStepOffersRetry() {
        let app = launch(["-collection-mixed-states", "-collection-use-fails-once"])
        openEvent(app, sunday)
        let use = element(app, "camera-roll-idea-use-\(ferryIdea)")
        scrollTo(use, in: app)
        use.tap()
        let error = element(app, "camera-roll-use-error")
        XCTAssertTrue(error.waitForExistence(timeout: 10), "A failure shows on the sheet")
        XCTAssertEqual(element(app, "camera-roll-use-step-chat").value as? String, "Failed")
        attach("Use failed with Retry", app.screenshot())
        element(app, "camera-roll-use-retry").tap()
        XCTAssertTrue(element(app, "camera-roll-opened-chat").waitForExistence(timeout: 15))
    }

    func testUseSaysWhenTheChatCannotOpen() {
        let app = launch(["-collection-mixed-states", "-collection-chat-deleted"])
        openEvent(app, sunday)
        let use = element(app, "camera-roll-idea-use-\(ferryIdea)")
        scrollTo(use, in: app)
        use.tap()
        let error = element(app, "camera-roll-use-error")
        XCTAssertTrue(wait(error, "label CONTAINS %@", "was deleted", timeout: 10), "A chat the phone won't open says why instead of doing nothing")
        XCTAssertFalse(element(app, "camera-roll-opened-chat").exists)
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
        openEvent(app, group)
        let find = element(app, "camera-roll-find-ideas-\(group)")
        scrollTo(find, in: app)
        XCTAssertTrue(find.waitForExistence(timeout: 10))
        find.tap()
        let go = element(app, "camera-roll-find-ideas-go-\(group)")
        XCTAssertTrue(go.waitForExistence(timeout: 5))
        scrollTo(go, in: app)
        go.tap()
    }

    func testUnavailableAnalysisIsNotAnEditConflict() {
        let app = launch(["-collection-mixed-states", "-collection-cache-miss"])
        findIdeas(app, in: saturday)
        let notice = element(app, "camera-roll-notice")
        XCTAssertTrue(notice.waitForExistence(timeout: 10))
        XCTAssertEqual(notice.label, "This footage needs analysis. Your collection is saved; new analysis is unavailable right now.")
        XCTAssertFalse(element(app, "camera-roll-review-latest").exists, "Nothing changed elsewhere, so there is no latest collection to open")
        XCTAssertTrue(element(app, "camera-roll-event-page-\(saturday)").exists, "The collection is untouched")
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
        XCTAssertTrue(card(app, sunday).waitForExistence(timeout: 5), "The event that is gone closes; home shows the latest collection")
        XCTAssertFalse(card(app, saturday).exists, "The other phone's collection replaces this one")
        XCTAssertFalse(element(app, "camera-roll-review-latest").exists)
        attach("Latest collection opened", app.screenshot())
    }

    /// Undo can arrive while the server is still deleting the event. The restore must land after the delete, or the
    /// ideas are gone on the server while the phone still shows them.
    func testUndoTappedWhileTheEventDeleteIsInFlightKeepsItsIdeas() {
        let app = launch(["-collection-mixed-states", "-collection-slow-group-delete"])
        openEvent(app, sunday)
        element(app, "camera-roll-event-menu-\(sunday)").tap()
        app.buttons["Delete event"].tap()
        element(app, "camera-roll-delete-event-confirm").tap()
        XCTAssertTrue(app.staticTexts["Event deleted"].waitForExistence(timeout: 5))
        element(app, "camera-roll-undo").tap()
        // The server finishes deleting 1.5 s in; then the restore runs. Reading its ideas back shows which won.
        openEvent(app, sunday)
        element(app, "camera-roll-event-menu-\(sunday)").tap()
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

private extension XCUIElement {
    func clearAndType(_ text: String) {
        tap()
        if let current = value as? String, !current.isEmpty {
            typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: current.count))
        }
        typeText(text)
    }
}
