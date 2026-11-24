import XCTest

extension XCUIApplication {
    /// Taps a format card ("montage", "talking_to_camera", …) once it accepts taps.
    func chooseFormat(_ format: String, file: StaticString = #filePath, line: UInt = #line) {
        chooseFormatCard(buttons["format-\(format)"], file: file, line: line)
    }

    /// The format cards are on screen while the chat's formats are still loading ("Loading formats…") and are
    /// disabled until they have. A tap sent then is dropped without an error, and the test fails later on whatever
    /// it expected next (KRI-1604: one such tap in the 11-22 nightly). Every test that picks a format taps here.
    func chooseFormatCard(_ card: XCUIElement, file: StaticString = #filePath, line: UInt = #line) {
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == true AND enabled == true"), object: card)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 20), .completed, "The format card accepts taps", file: file, line: line)
        if !card.isHittable { scrollViews["format-carousel"].swipeLeft() }
        card.tap()
    }
}
