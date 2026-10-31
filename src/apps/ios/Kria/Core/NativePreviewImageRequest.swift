import AVFoundation
import KriaMediaEngine

struct NativePreviewFrame: Sendable {
    let image: CGImage
    let actualTime: CMTime
}

typealias NativePreviewFrameRenderer = @MainActor (AVAssetImageGenerator, CMTime) async throws -> NativePreviewFrame

final class NativePreviewImageRequest: @unchecked Sendable {
    typealias Completion = @Sendable (Result<NativePreviewFrame, Error>) -> Void
    private enum State {
        case pending
        case running(CheckedContinuation<NativePreviewFrame, Error>)
        case finished
    }
    private let lock = NSRecursiveLock()
    private var state = State.pending
    private let start: @Sendable (@escaping Completion) -> Void
    private let cancel: @Sendable () -> Void

    init(start: @escaping @Sendable (@escaping Completion) -> Void, cancel: @escaping @Sendable () -> Void) {
        self.start = start
        self.cancel = cancel
    }

    func value() async throws -> NativePreviewFrame {
        try Task.checkCancellation()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                submit(continuation)
            }
        } onCancel: {
            self.cancelRequest()
        }
    }

    private func submit(_ continuation: CheckedContinuation<NativePreviewFrame, Error>) {
        lock.lock()
        guard case .pending = state else {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
            return
        }
        state = .running(continuation)
        start { [weak self] result in self?.complete(result) }
        lock.unlock()
    }

    private func complete(_ result: Result<NativePreviewFrame, Error>) {
        lock.lock()
        guard case .running(let continuation) = state else { lock.unlock(); return }
        state = .finished
        lock.unlock()
        continuation.resume(with: result)
    }

    private func cancelRequest() {
        lock.lock()
        switch state {
        case .pending:
            state = .finished
            lock.unlock()
        case .running(let continuation):
            state = .finished
            lock.unlock()
            cancel()
            continuation.resume(throwing: CancellationError())
        case .finished:
            lock.unlock()
        }
    }

    @MainActor
    static func render(generator: AVAssetImageGenerator, time: CMTime) async throws -> NativePreviewFrame {
        try Task.checkCancellation()
        let owner = GeneratorOwner(generator)
        let request = NativePreviewImageRequest(start: { completion in
            owner.generator.generateCGImagesAsynchronously(forTimes: [NSValue(time: time)]) { _, image, actualTime, result, error in
                if result == .succeeded, let image {
                    completion(.success(NativePreviewFrame(image: image, actualTime: actualTime)))
                } else {
                    completion(.failure(error ?? MediaEngineError.exportFailed))
                }
            }
        }, cancel: { owner.generator.cancelAllCGImageGeneration() })
        return try await request.value()
    }
}

private final class GeneratorOwner: @unchecked Sendable {
    let generator: AVAssetImageGenerator
    init(_ generator: AVAssetImageGenerator) { self.generator = generator }
}
