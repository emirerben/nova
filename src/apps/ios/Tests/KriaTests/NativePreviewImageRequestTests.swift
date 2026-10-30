import AVFoundation
import Combine
import UIKit
import XCTest
@testable import Kria

@MainActor
final class NativePreviewImageRequestTests: XCTestCase {
    private enum Failure: Error { case render }

    private func frame(_ time: Double = 1) -> NativePreviewFrame {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 2, height: 2)).image { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 2, height: 2))
        }
        return NativePreviewFrame(image: image.cgImage!, actualTime: CMTime(seconds: time, preferredTimescale: 600))
    }

    func testInlineCompletionAndDuplicateCallbackReturnFirstFrame() async throws {
        let expected = frame()
        let request = NativePreviewImageRequest(start: { completion in
            completion(.success(expected))
            completion(.failure(Failure.render))
        }, cancel: { XCTFail("completed work must not be cancelled") })
        let result = try await request.value()
        XCTAssertEqual(result.actualTime, expected.actualTime)
    }

    func testFailureIsPropagated() async {
        let request = NativePreviewImageRequest(start: { $0(.failure(Failure.render)) }, cancel: {})
        do { _ = try await request.value(); XCTFail("expected failure") }
        catch { XCTAssertTrue(error is Failure) }
    }

    func testCancellationBeforeStartDoesNotSubmitNativeWork() async {
        let request = NativePreviewImageRequest(start: { _ in XCTFail("cancelled request started") }, cancel: { XCTFail("unstarted request cancelled native work") })
        let task = Task { try await request.value() }
        task.cancel()
        do { _ = try await task.value; XCTFail("expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }

    func testCancellationAfterStartCancelsOnceAndIgnoresLateCallback() async {
        let started = expectation(description: "native work started")
        let cancelled = expectation(description: "native work cancelled once")
        cancelled.assertForOverFulfill = true
        let callbacks = CallbackStore()
        let request = NativePreviewImageRequest(start: { completion in
            callbacks.set(completion)
            started.fulfill()
        }, cancel: {
            callbacks.record("cancel")
            callbacks.complete(.failure(Failure.render))
            cancelled.fulfill()
        })
        let task = Task { try await request.value() }
        await fulfillment(of: [started], timeout: 2)
        task.cancel()
        task.cancel()
        await fulfillment(of: [cancelled], timeout: 2)
        XCTAssertEqual(callbacks.events, ["cancel"])
        callbacks.complete(.success(frame()))
        do { _ = try await task.value; XCTFail("late success escaped cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }

    func testCancellationDuringSubmissionCancelsAfterNativeStartReturns() async {
        let started = expectation(description: "submission entered")
        let cancelling = expectation(description: "cancellation attempted")
        let cancelled = expectation(description: "native work cancelled")
        let releaseSubmission = DispatchSemaphore(value: 0)
        let callbacks = CallbackStore()
        let request = NativePreviewImageRequest(start: { completion in
            callbacks.set(completion)
            callbacks.record("start")
            started.fulfill()
            XCTAssertEqual(releaseSubmission.wait(timeout: .now() + 5), .success)
            callbacks.record("submitted")
        }, cancel: {
            callbacks.record("cancel")
            callbacks.complete(.failure(Failure.render))
            cancelled.fulfill()
        })
        let task = Task { try await request.value() }
        await fulfillment(of: [started], timeout: 2)
        let cancellation = Task.detached {
            cancelling.fulfill()
            task.cancel()
        }
        await fulfillment(of: [cancelling], timeout: 2)
        releaseSubmission.signal()
        await cancellation.value
        await fulfillment(of: [cancelled], timeout: 2)
        XCTAssertEqual(callbacks.events, ["start", "submitted", "cancel"])
        do { _ = try await task.value; XCTFail("expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }

    func testFailedOlderSeekStillRendersNewestQueuedSeek() async throws {
        try await assertQueuedSeek(olderResult: .failure(Failure.render))
    }

    func testSuccessfulOlderSeekDoesNotPublishAheadOfNewestQueuedSeek() async throws {
        try await assertQueuedSeek(olderResult: .success(frame(1)))
    }

    private func assertQueuedSeek(olderResult: Result<NativePreviewFrame, Error>) async throws {
        let firstStarted = expectation(description: "first seek started")
        let latestStarted = expectation(description: "latest seek started")
        var pending: [CheckedContinuation<NativePreviewFrame, Error>] = []
        var times: [Double] = []
        let session = NativeEditorSession(previewFrameRenderer: { _, time in
            times.append(time.seconds)
            return try await withCheckedThrowingContinuation { continuation in
                pending.append(continuation)
                if times.count == 1 { firstStarted.fulfill() }
                else { latestStarted.fulfill() }
            }
        })
        installPlayer(in: session)
        session.seek(to: 1)
        await fulfillment(of: [firstStarted], timeout: 2)
        XCTAssertEqual(times, [1])
        guard !pending.isEmpty else { return }
        session.seek(to: 2)
        session.seek(to: 3)
        pending.removeFirst().resume(with: olderResult)
        await fulfillment(of: [latestStarted], timeout: 2)
        XCTAssertEqual(times, [1, 3], "an older result must drain the latest queued seek")
        XCTAssertNil(session.scrubPreviewFrame, "an outdated result must never become visible")
        guard !pending.isEmpty else { return }
        let published = expectation(description: "latest frame published")
        let subscription = session.$scrubPreviewFrame.dropFirst().sink { image in
            if image != nil { published.fulfill() }
        }
        pending.removeFirst().resume(returning: frame(3))
        await fulfillment(of: [published], timeout: 2)
        withExtendedLifetime(subscription) {}
        XCTAssertNotNil(session.scrubPreviewFrame)
        XCTAssertEqual(session.scrubPreviewTime, 3)
    }

    func testOldGenerationFailureCannotClearReplacementFrame() async throws {
        let oldStarted = expectation(description: "old generation started")
        let newStarted = expectation(description: "new generation started")
        let oldReturned = expectation(description: "old generation returned")
        var pending: [Double: CheckedContinuation<NativePreviewFrame, Error>] = [:]
        let session = NativeEditorSession(previewFrameRenderer: { _, time in
            defer { if time.seconds == 1 { oldReturned.fulfill() } }
            return try await withCheckedThrowingContinuation { continuation in
                pending[time.seconds] = continuation
                if time.seconds == 1 { oldStarted.fulfill() } else { newStarted.fulfill() }
            }
        })
        installPlayer(in: session)
        session.seek(to: 1)
        await fulfillment(of: [oldStarted], timeout: 2)
        installPlayer(in: session)
        session.seek(to: 2)
        await fulfillment(of: [newStarted], timeout: 2)
        let published = expectation(description: "replacement frame published")
        let subscription = session.$scrubPreviewFrame.dropFirst().sink { image in
            if image != nil { published.fulfill() }
        }
        try XCTUnwrap(pending.removeValue(forKey: 2)).resume(returning: frame(2))
        await fulfillment(of: [published], timeout: 2)
        try XCTUnwrap(pending.removeValue(forKey: 1)).resume(throwing: Failure.render)
        await fulfillment(of: [oldReturned], timeout: 2)
        withExtendedLifetime(subscription) {}
        XCTAssertNotNil(session.scrubPreviewFrame)
        XCTAssertEqual(session.scrubPreviewTime, 2)
    }

    func testCancellingCompletedRequestDoesNotCancelNextRequest() async throws {
        let expected = frame()
        let completed = NativePreviewImageRequest(start: { $0(.success(expected)) }, cancel: {
            XCTFail("completed request cancelled a reused native generator")
        })
        let first = Task { try await completed.value() }
        _ = try await first.value
        let started = expectation(description: "next request started")
        let callbacks = CallbackStore()
        let next = NativePreviewImageRequest(start: { completion in
            callbacks.set(completion)
            started.fulfill()
        }, cancel: { XCTFail("next request was not cancelled") })
        let second = Task { try await next.value() }
        await fulfillment(of: [started], timeout: 2)
        first.cancel()
        callbacks.complete(.success(expected))
        let result = try await second.value
        XCTAssertEqual(result.actualTime, expected.actualTime)
    }

    func testDeselectingTextCancelsSuspendedLayerWithoutStartingAnotherSlice() async throws {
        let started = expectation(description: "text layer started")
        let cancelled = expectation(description: "text layer cancelled")
        let (session, callbacks, returned) = try await suspendedTextSession(started: started, cancelled: cancelled)
        session.select(nil, seekToStart: false)
        await fulfillment(of: [cancelled], timeout: 2)
        callbacks.complete(.failure(Failure.render))
        await fulfillment(of: [returned], timeout: 2)
        XCTAssertNil(session.textInteractionFrame)
    }

    func testReleasingSessionCancelsSuspendedTextLayer() async throws {
        let started = expectation(description: "text layer started")
        let cancelled = expectation(description: "text layer cancelled")
        var held: (NativeEditorSession, CallbackStore, XCTestExpectation)? = try await suspendedTextSession(started: started, cancelled: cancelled)
        weak var session = held?.0
        let callbacks = held?.1
        let returned = try XCTUnwrap(held?.2)
        held = nil
        XCTAssertNil(session)
        await fulfillment(of: [cancelled], timeout: 2)
        callbacks?.complete(.failure(Failure.render))
        await fulfillment(of: [returned], timeout: 2)
    }

    private func suspendedTextSession(started: XCTestExpectation, cancelled: XCTestExpectation) async throws -> (NativeEditorSession, CallbackStore, XCTestExpectation) {
        let initialFrame = frame(0)
        let callbacks = CallbackStore()
        let returned = expectation(description: "suspended layer returned")
        var suspend = false
        var sliceCount = 0
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText, previewFrameRenderer: { _, _ in
            guard suspend else { return initialFrame }
            sliceCount += 1
            XCTAssertEqual(sliceCount, 1, "a cancelled text interaction must not start its next slice")
            let request = NativePreviewImageRequest(start: { completion in
                callbacks.set(completion)
                started.fulfill()
            }, cancel: { cancelled.fulfill() })
            defer { returned.fulfill() }
            return try await request.value()
        })
        let initialPublished = expectation(description: "initial scrub published")
        let subscription = session.$scrubPreviewFrame.sink { image in
            if image != nil { initialPublished.fulfill() }
        }
        let footage = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: footage)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        await fulfillment(of: [initialPublished], timeout: 3)
        subscription.cancel()
        suspend = true
        let text = try XCTUnwrap(session.document.textElements.first)
        session.select(EditorSelection(kind: .text, id: text.id), seekToStart: false)
        await fulfillment(of: [started], timeout: 3)
        return (session, callbacks, returned)
    }

    private func installPlayer(in session: NativeEditorSession) {
        session.duration = 10
        let item = AVPlayerItem(asset: AVMutableComposition())
        let composition = AVMutableVideoComposition()
        composition.renderSize = CGSize(width: 16, height: 16)
        composition.frameDuration = CMTime(value: 1, timescale: 30)
        item.videoComposition = composition
        session.player = AVPlayer(playerItem: item)
    }

}

private final class CallbackStore: @unchecked Sendable {
    private let lock = NSLock()
    private var recordedEvents: [String] = []
    var events: [String] {
        lock.lock()
        defer { lock.unlock() }
        return recordedEvents
    }
    func record(_ event: String) {
        lock.lock()
        recordedEvents.append(event)
        lock.unlock()
    }
    private var completion: (@Sendable (Result<NativePreviewFrame, Error>) -> Void)?
    func set(_ completion: @escaping @Sendable (Result<NativePreviewFrame, Error>) -> Void) {
        lock.lock()
        self.completion = completion
        lock.unlock()
    }
    func complete(_ result: Result<NativePreviewFrame, Error>) {
        lock.lock()
        let callback = completion
        lock.unlock()
        callback?(result)
    }
}
