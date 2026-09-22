import XCTest
@testable import Kria

@MainActor
final class ChatResponsePresentationTests: XCTestCase {
    func testInitialLoadBaselinesHistoryWithoutPulseOrStartTime() {
        let model = ChatResponsePresentation(now: { Date(timeIntervalSince1970: 10) })
        model.observe(events: [event(id: "old")], isInitialLoad: true, isActive: true)

        XCTAssertEqual(model.hapticToken, 0)
        XCTAssertNil(model.startTime(for: "old"))
    }

    func testNewAssistantBatchPulsesOnceAndSharesStartTime() {
        let start = Date(timeIntervalSince1970: 42)
        let model = ChatResponsePresentation(now: { start })
        model.observe(events: [], isInitialLoad: true, isActive: true)
        model.observe(events: [event(id: "a"), event(id: "b")], isInitialLoad: false, isActive: true)

        XCTAssertEqual(model.hapticToken, 1)
        XCTAssertEqual(model.startTime(for: "a"), start)
        XCTAssertEqual(model.startTime(for: "b"), start)
    }

    func testRepeatedAndOldEventsDoNotReplayEffects() {
        let model = ChatResponsePresentation(now: Date.init)
        let assistant = event(id: "a")
        model.observe(events: [assistant], isInitialLoad: true, isActive: true)
        model.observe(events: [assistant], isInitialLoad: false, isActive: true)

        XCTAssertEqual(model.hapticToken, 0)
        XCTAssertNil(model.startTime(for: "a"))
    }

    func testBackgroundAssistantEventDoesNotPulseOrStartReveal() {
        let model = ChatResponsePresentation(now: { Date(timeIntervalSince1970: 7) })
        model.observe(events: [], isInitialLoad: true, isActive: false)
        model.observe(events: [event(id: "background")], isInitialLoad: false, isActive: false)

        XCTAssertEqual(model.hapticToken, 0)
        XCTAssertNil(model.startTime(for: "background"))
        model.observe(events: [event(id: "background")], isInitialLoad: false, isActive: true)
        XCTAssertEqual(model.hapticToken, 0)
    }

    func testTokenizationPreservesWhitespaceNewlinesAndUnicode() {
        let tokenized = ChatResponseText.tokenize("İyi  gün\n世界!")

        XCTAssertEqual(tokenized.tokens.map(\.text), ["İyi", "  ", "gün", "\n", "世界!"])
        XCTAssertEqual(tokenized.wordCount, 3)
    }

    func testRevealTimingCapsLongResponsesAtTwoSeconds() {
        XCTAssertEqual(ChatResponseText.presentationInterval(wordCount: 10), 0.035, accuracy: 0.000001)
        XCTAssertEqual(ChatResponseText.presentationInterval(wordCount: 200), 0.01, accuracy: 0.000001)
        XCTAssertEqual(ChatResponseText.revealCount(elapsed: 2.0, wordCount: 200), 200)
        XCTAssertEqual(ChatResponseText.revealCount(elapsed: 1.0, wordCount: 200), 100)
    }

    private func event(id: String) -> ThreadEvent {
        ThreadEvent(
            id: id,
            sequence: 1,
            revision: 1,
            role: "assistant",
            eventType: "assistant_response",
            content: "Response",
            payload: nil,
            createdAt: Date(timeIntervalSince1970: 1)
        )
    }
}
