import XCTest
import KriaMediaEngine
@testable import Kria

final class NativeEditorAIActivityTests: XCTestCase {
    private let project = UUID()

    func testFeedbackRequiresARealRequestAndStopsAtTerminalState() {
        XCTAssertNil(activity(sending: true))
        XCTAssertNil(activity(thinking: true))
        let sent = activity(sending: true, sendID: "request-1")
        XCTAssertEqual(sent?.requestID, "request-1")
        XCTAssertEqual(sent?.status, .sending)
        XCTAssertEqual(activity(thinking: true, turnID: "turn-1")?.status, .thinking)
        // A retained request ID after a terminal/no-op/failure is not activity.
        XCTAssertNil(activity(sendID: "request-1", turnID: "turn-1"))
    }

    func testNewRequestReplacesOlderTurnAndOtherProjectCannotBorrowItsHalo() {
        let next = activity(sending: true, sendID: "request-2", thinking: true, turnID: "turn-1")
        XCTAssertEqual(next?.requestID, "request-2")
        XCTAssertNil(NativeEditorAIStatus.current(projectID: UUID(), conversation: next))
        XCTAssertEqual(NativeEditorAIStatus.current(projectID: project, conversation: next), .sending)
    }

    func testAcceptedTurnDoesNotKeepReadingSendingWhilePOSTUnwinds() {
        XCTAssertEqual(activity(sending: true, sendID: nil, thinking: true, turnID: "turn-1")?.status, .thinking)
    }

    func testPreviewWorkStopsOnReadinessOrFailureWithoutInventingSuccess() {
        XCTAssertEqual(status(preview: .preparing), .preparingPreview)
        XCTAssertNil(status(preview: .ready))
        XCTAssertNil(status(preview: .failed("Preview failed")))
        XCTAssertNil(status(preview: .originalsUnavailable))
        XCTAssertEqual(status(refresh: .newVersionRendering), .rendering)
        // A completed old job may remain on screen while the new job is rendering.
        for oldPhase in [DeviceRenderPhase.synced, .localReady, .superseded] {
            XCTAssertEqual(status(refresh: .newVersionRendering, render: .init(phase: oldPhase)), .rendering)
        }
        XCTAssertEqual(status(refresh: .newVersionRendering,
                              render: .init(phase: .rendering, waiting: .connection)), .rendering)
        XCTAssertNil(status(refresh: .targetLost("Project changed")))
    }

    func testRenderingAndPublicationRemainDifferentAndTerminalStatesStop() {
        for phase in [DeviceRenderPhase.preparing, .rendering] {
            XCTAssertEqual(status(render: .init(phase: phase)), .rendering)
        }
        XCTAssertEqual(status(render: .init(phase: .syncing)), .syncing)
        for phase in [DeviceRenderPhase.localReady, .synced, .needsAttention, .cancelled, .superseded] {
            XCTAssertNil(status(render: .init(phase: phase)))
        }
        for wait in [DeviceRenderWait.connection, .server, .cooling, .storage(neededBytes: 100), .checking] {
            XCTAssertNil(status(render: .init(phase: .rendering, waiting: wait)))
        }
    }

    private func activity(sending: Bool = false, sendID: String? = nil,
                          thinking: Bool = false, turnID: String? = nil) -> NativeEditorAIActivity? {
        NativeEditorAIActivity.current(projectID: project, isSending: sending, sendingRequestID: sendID,
                                       isThinking: thinking, thinkingRequestID: turnID)
    }

    private func status(preview: NativeSourcePreviewState = .ready,
                        refresh: NativeEditorRefreshSync = .idle,
                        render: DeviceRenderPresentation? = nil) -> NativeEditorAIStatus? {
        NativeEditorAIStatus.current(projectID: project, conversation: nil,
                                    refresh: refresh, preview: preview, render: render)
    }
}
