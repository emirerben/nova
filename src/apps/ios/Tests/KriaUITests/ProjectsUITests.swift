import XCTest

@MainActor
final class ProjectsUITests: XCTestCase {
    func testProjectActionsCanBeCancelledWithoutChangingProject() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launch()
        createFreshChat(in: app)
        let menu = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Project actions for'")).firstMatch
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        let originalTitle = app.staticTexts["workspace-project-title"].label
        menu.tap()
        app.buttons["Rename project"].tap()
        let renamePanel = app.alerts["Rename project"]
        XCTAssertTrue(renamePanel.waitForExistence(timeout: 3))
        XCTAssertTrue(renamePanel.textFields.firstMatch.waitForExistence(timeout: 3))
        XCTAssertLessThan(renamePanel.frame.height, app.frame.height * 0.5,
                          "Rename should stay a compact native alert")
        app.buttons["Cancel"].tap()
        XCTAssertEqual(app.staticTexts["workspace-project-title"].label, originalTitle)
        menu.tap()
        let delete = app.buttons["Delete project"]
        XCTAssertTrue(delete.waitForExistence(timeout: 3))
        XCTAssertTrue(delete.isEnabled)
        delete.tap()
        let confirmation = app.alerts["Delete this project?"]
        XCTAssertTrue(confirmation.waitForExistence(timeout: 3))
        confirmation.buttons["Cancel"].tap()
        XCTAssertTrue(app.staticTexts["workspace-project-title"].waitForExistence(timeout: 3))
        XCTAssertEqual(app.staticTexts["workspace-project-title"].label, originalTitle)
    }

    func testRenameValidatesNameAndRetainsInputAfterFailedSaveAndRetry() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launch()
        createFreshChat(in: app)
        let originalTitle = app.staticTexts["workspace-project-title"].label
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Project actions for'")).firstMatch.tap()
        app.buttons["Rename project"].tap()
        let renamePanel = app.alerts["Rename project"]
        XCTAssertTrue(renamePanel.waitForExistence(timeout: 3))
        let field = renamePanel.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 3))
        let save = app.alerts["Rename project"].buttons["OK"]
        func replaceName(_ value: String) {
            field.tap()
            let current = field.value as? String ?? ""
            field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: current.count) + value)
        }
        replaceName("   ")
        XCTAssertFalse(save.isEnabled, "Whitespace-only names cannot be submitted")
        replaceName(String(repeating: "x", count: 121))
        XCTAssertFalse(save.isEnabled, "Names over 120 characters cannot be submitted")
        replaceName(String(repeating: "x", count: 120))
        XCTAssertTrue(save.isEnabled, "The 120-character boundary remains valid")
        let proposed = "  warm   café  "
        replaceName(proposed)
        XCTAssertTrue(save.isEnabled)
        let failure = app.staticTexts["The name couldn’t be saved. Your text is still here; try again."]
        for _ in 0..<2 {
            save.tap()
            XCTAssertTrue(failure.waitForExistence(timeout: 3))
            XCTAssertTrue(app.alerts["Rename project"].waitForExistence(timeout: 3))
            let settled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: save)
            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 3), .completed)
            XCTAssertEqual(field.value as? String, proposed, "A failed save must retain the exact user's input")
        }
        app.buttons["Cancel"].tap()
        XCTAssertEqual(app.staticTexts["workspace-project-title"].label, originalTitle)
    }

    func testPartialDrawerDragsAlwaysSettleAtTheNearestEndpoint() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launch()
        createFreshChat(in: app)
        let menu = app.buttons["workspace-menu-toggle"]
        let closedX = menu.frame.minX
        let width = min(326, app.frame.width - 76)
        func drag(from x: CGFloat, by distance: CGFloat) {
            let start = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: x, dy: app.frame.height * 0.5))
            let end = start.withOffset(CGVector(dx: distance, dy: 0))
            start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0.1)
        }
        func assertSettled(open: Bool) {
            let expected = closedX + (open ? width : 0)
            let settled = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
                abs(menu.frame.minX - expected) < 2
            }, object: menu)
            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 4), .completed,
                           "Drawer must not remain between its endpoints")
        }
        drag(from: 10, by: width * 0.3)
        assertSettled(open: false)
        drag(from: 10, by: width * 0.7)
        assertSettled(open: true)
        drag(from: app.frame.width - 20, by: -width * 0.3)
        assertSettled(open: true)
        drag(from: app.frame.width - 20, by: -width * 0.7)
        assertSettled(open: false)
        func flick(from x: CGFloat, by distance: CGFloat) {
            let start = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: x, dy: app.frame.height * 0.5))
            start.press(forDuration: 0.05,
                        thenDragTo: start.withOffset(CGVector(dx: distance, dy: 0)),
                        withVelocity: .fast, thenHoldForDuration: 0)
        }
        flick(from: 10, by: width * 0.35)
        assertSettled(open: true)
        flick(from: app.frame.width - 20, by: -width * 0.35)
        assertSettled(open: false)
    }

    private func createFreshChat(in app: XCUIApplication) {
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 3))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.staticTexts["What kind of video are we making?"].waitForExistence(timeout: 20))
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
    }
}
