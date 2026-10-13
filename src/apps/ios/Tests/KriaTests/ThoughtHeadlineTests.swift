import XCTest
@testable import Kria

/// KRI-627: the one-line "what Kria is doing" under the thinking row is derived
/// from raw Gemini thought text. Each test pins one way that line could go wrong
/// while the text is still streaming in.
final class ThoughtHeadlineTests: XCTestCase {
    func testLatestCompleteHeadingWins() {
        let text = "**Reading your direction**\n\nThe user wants a warm tone.\n\n**Matching the Lisbon title**\n\nChecking the clips."
        XCTAssertEqual(ThoughtHeadline.extract(from: text), "Matching the Lisbon title")
    }

    func testHalfStreamedHeadingKeepsThePreviousCompleteOne() {
        let text = "**Reading your direction**\n\nThe user wants a warm tone.\n\n**Matching the"
        XCTAssertEqual(ThoughtHeadline.extract(from: text), "Reading your direction")
    }

    func testHeadingOnlyTextWithoutProseStillWorks() {
        XCTAssertEqual(ThoughtHeadline.extract(from: "**Planning the cut**"), "Planning the cut")
    }

    func testProseFallbackUsesFirstSentenceOfLatestParagraph() {
        let text = "Looking at the first clip.\n\nThe second clip is a better opener. It has motion."
        XCTAssertEqual(ThoughtHeadline.extract(from: text), "The second clip is a better opener.")
    }

    func testJSONAndCodeNeverBecomeTheSummary() {
        XCTAssertNil(ThoughtHeadline.extract(from: "{\"slot\": 1, \"clip\": \"a\"}"))
        XCTAssertNil(ThoughtHeadline.extract(from: "```json\n{\"slot\": 1}\n```"))
        XCTAssertNil(ThoughtHeadline.extract(from: "[1, 2, 3]"))
    }

    func testCodeAfterProseFallsBackToTheProseNotTheCode() {
        let text = "I will pick the first clip.\n\n```json\n{\"slot\": 1}\n```"
        XCTAssertEqual(ThoughtHeadline.extract(from: text), "I will pick the first clip.")
    }

    func testMarkdownMarkersDoNotLeak() {
        let line = ThoughtHeadline.extract(from: "**Checking `slot_1` against __the__ brief**")
        XCTAssertEqual(line, "Checking slot_1 against the brief")
    }

    func testLongSentenceIsCappedToOneLine() {
        let long = String(repeating: "word ", count: 80)
        let line = ThoughtHeadline.extract(from: long)
        XCTAssertNotNil(line)
        XCTAssertLessThanOrEqual(line?.count ?? 0, 91)
        XCTAssertTrue(line?.hasSuffix("…") == true)
    }

    func testEmptyOrWhitespaceTextIsNilSoTheFallbackCopyShows() {
        XCTAssertNil(ThoughtHeadline.extract(from: ""))
        XCTAssertNil(ThoughtHeadline.extract(from: "  \n\n \n"))
    }
}
