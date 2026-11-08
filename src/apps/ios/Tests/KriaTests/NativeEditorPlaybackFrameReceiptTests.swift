import AVFoundation
import XCTest
@testable import KriaMediaEngine

final class NativeEditorPlaybackFrameReceiptTests: XCTestCase {
    func testOldRevisionsAndImageGeneratorInstancesCannotAcknowledgePlayback() {
        let playback = PreviewPlaybackFrameReceipt()
        let generator = PreviewPlaybackFrameReceipt()
        let values = Values()
        let revision = UUID()
        playback.arm(revision: revision) { values.append($0) }
        generator.complete(revision: revision, at: 2)
        playback.complete(revision: UUID(), at: 3)
        XCTAssertEqual(values.snapshot, [])
        playback.complete(revision: revision, at: 4)
        playback.complete(revision: revision, at: 5)
        XCTAssertEqual(values.snapshot, [4])
    }

    func testSupersedingAndCancellationDiscardPendingAcknowledgments() {
        let receipt = PreviewPlaybackFrameReceipt()
        let values = Values()
        let old = UUID(), current = UUID()
        receipt.arm(revision: old) { values.append($0) }
        receipt.arm(revision: current) { values.append($0) }
        receipt.complete(revision: old, at: 1)
        XCTAssertEqual(values.snapshot, [])
        receipt.complete(revision: current, at: 2)
        let cancelled = UUID()
        receipt.arm(revision: cancelled) { values.append($0) }
        receipt.cancel()
        receipt.complete(revision: cancelled, at: 3)
        XCTAssertEqual(values.snapshot, [2])
    }

    func testAcknowledgmentCanRearmWithoutHoldingTheReceiptLock() {
        let receipt = PreviewPlaybackFrameReceipt()
        let first = UUID(), next = UUID()
        let values = Values()
        receipt.arm(revision: first) { value in
            values.append(value)
            receipt.arm(revision: next) { values.append($0) }
        }
        receipt.complete(revision: first, at: 1)
        receipt.complete(revision: next, at: 2)
        XCTAssertEqual(values.snapshot, [1, 2])
    }

    private final class Values: @unchecked Sendable {
        private let lock = NSLock()
        private var values: [Double] = []
        func append(_ value: Double) { lock.lock(); values.append(value); lock.unlock() }
        var snapshot: [Double] { lock.lock(); defer { lock.unlock() }; return values }
    }
}
