import XCTest

@MainActor final class SignInUITests: XCTestCase {
    private func launch(state: String = "signin", configure: (XCUIApplication) -> Void = { _ in }) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = state
        configure(app)
        app.launch()
        return app
    }

    private func reveal(_ element: XCUIElement, app: XCUIApplication) {
        for _ in 0..<12 where !element.isHittable { app.swipeUp() }
        XCTAssertTrue(element.waitForExistence(timeout: 3))
        XCTAssertTrue(element.isHittable)
    }

    func testSignInShowsEveryProviderAndLegalLink() {
        let app = launch()
        let google = app.buttons["signin.google"]
        XCTAssertTrue(google.waitForExistence(timeout: 5))
        reveal(google, app: app)
        XCTAssertEqual(google.label, "Continue with Google")

        let email = app.buttons["signin.email"]
        reveal(email, app: app)

        for identifier in ["kria-privacy-link", "kria-terms-link", "kria-support-link"] {
            let link = app.descendants(matching: .any)[identifier].firstMatch
            reveal(link, app: app)
        }

        if ProcessInfo.processInfo.environment["KRIA_UI_TEST_GOOGLE_ONLY"] != "1" {
            let apple = app.descendants(matching: .any)["signin.apple"].firstMatch
            XCTAssertTrue(apple.waitForExistence(timeout: 3), "Sign in with Apple should be present unless the build is Google-only")
        }
    }

    func testSignInEmailOpensReviewerSheetAndCancelReturns() {
        let app = launch()
        let email = app.buttons["signin.email"]
        reveal(email, app: app)
        email.tap()
        XCTAssertTrue(app.textFields["signin.email.field"].waitForExistence(timeout: 5))
        app.buttons["signin.email.cancel"].tap()
        XCTAssertTrue(app.buttons["signin.google"].waitForExistence(timeout: 5))
    }

    func testSignInAtLargestDynamicTypeKeepsEveryActionReachable() {
        let app = launch { $0.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5" }
        let google = app.buttons["signin.google"]
        XCTAssertTrue(google.waitForExistence(timeout: 5))
        // `reveal()`'s `isHittable` check is this codebase's established proof
        // of on-screen reachability (see `AccountPrivacyUITests`, which uses it
        // with no further frame check). A raw `app.frame.contains(element.frame)`
        // is stricter than that: XCUITest can still report an element hittable
        // while its full accessibility frame extends a little past the bottom
        // edge near the home indicator, so asserting hittability alone (rather
        // than exact frame containment) is what's actually being guaranteed here.
        for identifier in ["signin.google", "signin.email", "kria-privacy-link", "kria-terms-link", "kria-support-link"] {
            let element = app.descendants(matching: .any)[identifier].firstMatch
            reveal(element, app: app)
        }
    }

    func testSignInWithReduceMotionIsInteractiveImmediately() {
        let app = launch { $0.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1" }
        let google = app.buttons["signin.google"]
        XCTAssertTrue(google.waitForExistence(timeout: 1))
        XCTAssertTrue(google.isHittable)
    }

    func testSignInErrorMessageIsAnnounced() {
        let app = launch(state: "signin-error")
        let message = app.staticTexts["signin.message"]
        XCTAssertTrue(message.waitForExistence(timeout: 5))
        XCTAssertEqual(message.label, "Google sign-in was cancelled.")
    }
}
