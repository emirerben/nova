import XCTest
@testable import Kria

/// KRI-742. Failure modes written before the controller: permission denied, a server pass that fails or
/// hears nothing, a cancel while the permission sheet is up, a late partial after cancel, a system
/// interruption mid-sentence, and a second tap while already listening. The invariant under all of them:
/// what the creator already typed is never lost, and what they said is kept unless the server has better.
@MainActor final class DictationControllerTests: XCTestCase {
    final class FakeEngine: DictationEngine {
        var startError: DictationEngineError?
        var recording: DictationRecording?
        private(set) var cancelCount = 0
        private(set) var stopCount = 0
        private(set) var startCount = 0
        var onPartial: (@MainActor (String) -> Void)?
        var onInterrupted: (@MainActor () -> Void)?
        /// Lets a test hold `start` open to stand in for the system permission sheet.
        var gate: CheckedContinuation<Void, Never>?
        var holdStart = false

        func start(contextualStrings: [String], onPartial: @escaping @MainActor (String) -> Void,
                   onLevel: @escaping @MainActor (CGFloat) -> Void, onInterrupted: @escaping @MainActor () -> Void) async throws {
            startCount += 1
            self.onPartial = onPartial
            self.onInterrupted = onInterrupted
            if holdStart { await withCheckedContinuation { gate = $0 } }
            if let startError { throw startError }
        }
        func stop() async -> DictationRecording? { stopCount += 1; return recording }
        func cancel() { cancelCount += 1 }
    }

    struct FakeTranscriber: DictationTranscriber {
        var result: Result<DictationTranscript, Error>
        func transcribe(_ recording: DictationRecording, contextTerms: [String]) async throws -> DictationTranscript {
            // Like URLSession: a cancelled task fails the request instead of answering it.
            try Task.checkCancellation()
            return try result.get()
        }
    }

    private func recording() -> DictationRecording {
        let url = FileManager.default.temporaryDirectory.appending(path: "test-dictation-\(UUID().uuidString).m4a")
        try? Data([0]).write(to: url)
        return DictationRecording(url: url, duration: 2)
    }

    private func makeController(_ engine: FakeEngine, _ result: Result<DictationTranscript, Error>?) -> DictationController {
        DictationController(engine: engine, transcriber: result.map { FakeTranscriber(result: $0) })
    }

    private func settle(_ controller: DictationController, until phase: DictationController.Phase, file: StaticString = #filePath, line: UInt = #line) async {
        for _ in 0..<200 where controller.phase != phase { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(controller.phase, phase, file: file, line: line)
    }

    private final class TextBox { var value: String; init(_ value: String) { self.value = value } }

    private func begin(_ controller: DictationController, base: String, box: TextBox) async {
        controller.start(base: base, contextTerms: []) { box.value = $0 }
        await settle(controller, until: .listening)
    }

    func testPartialWordsAreAppendedToWhatWasAlreadyTyped() async {
        let engine = FakeEngine()
        let controller = makeController(engine, nil)
        let box = TextBox("Make it")
        await begin(controller, base: "Make it", box: box)
        engine.onPartial?("shorter")
        XCTAssertEqual(box.value, "Make it shorter")
        engine.onPartial?("shorter and brighter")
        XCTAssertEqual(box.value, "Make it shorter and brighter")
    }

    func testServerTextReplacesLiveWordsAndKeepsTheTypedPrefix() async {
        let engine = FakeEngine()
        engine.recording = recording()
        let controller = makeController(engine, .success(DictationTranscript(text: "Bodrum klibini öne al.", isNoSpeech: false)))
        let box = TextBox("Please")
        await begin(controller, base: "Please", box: box)
        engine.onPartial?("both rum clip in a")
        await controller.stop()
        XCTAssertEqual(box.value, "Please Bodrum klibini öne al.")
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertNil(controller.notice)
    }

    func testServerFailureKeepsTheLiveWords() async {
        let engine = FakeEngine()
        engine.recording = recording()
        let controller = makeController(engine, .failure(APIError.offline))
        let box = TextBox("")
        await begin(controller, base: "", box: box)
        engine.onPartial?("shorten the intro")
        await controller.stop()
        XCTAssertEqual(box.value, "shorten the intro")
        XCTAssertNil(controller.notice)
    }

    func testServerFailureWithNothingHeardRestoresTheFieldAndSaysSo() async {
        let engine = FakeEngine()
        engine.recording = recording()
        let controller = makeController(engine, .failure(APIError.offline))
        let box = TextBox("typed first")
        await begin(controller, base: "typed first", box: box)
        await controller.stop()
        XCTAssertEqual(box.value, "typed first")
        XCTAssertNotNil(controller.notice)
    }

    func testSilenceNeverBecomesText() async {
        let engine = FakeEngine()
        engine.recording = recording()
        let controller = makeController(engine, .success(DictationTranscript(text: "", isNoSpeech: true)))
        let box = TextBox("typed first")
        await begin(controller, base: "typed first", box: box)
        await controller.stop()
        XCTAssertEqual(box.value, "typed first")
        XCTAssertEqual(controller.notice, "Didn’t catch that. Try again.")
    }

    func testServerSilenceKeepsWordsTheRecognizerDidHear() async {
        let engine = FakeEngine()
        engine.recording = recording()
        let controller = makeController(engine, .success(DictationTranscript(text: "", isNoSpeech: true)))
        let box = TextBox("")
        await begin(controller, base: "", box: box)
        engine.onPartial?("quiet voice")
        await controller.stop()
        XCTAssertEqual(box.value, "quiet voice")
    }

    func testMicrophoneDeniedLeavesTheFieldAloneAndExplains() async {
        let engine = FakeEngine()
        engine.startError = .microphoneDenied
        let controller = makeController(engine, nil)
        let box = TextBox("typed")
        controller.start(base: "typed", contextTerms: []) { box.value = $0 }
        await settle(controller, until: .idle)
        XCTAssertEqual(box.value, "typed")
        XCTAssertTrue(controller.notice?.contains("Microphone access is off") == true)
    }

    func testCancelWhilePermissionSheetIsUpStopsTheEngineOnceItStarts() async {
        let engine = FakeEngine()
        engine.holdStart = true
        let controller = makeController(engine, nil)
        let box = TextBox("")
        controller.start(base: "", contextTerms: []) { box.value = $0 }
        XCTAssertEqual(controller.phase, .requestingPermission)
        controller.cancel()
        XCTAssertEqual(controller.phase, .idle)
        for _ in 0..<100 where engine.gate == nil { try? await Task.sleep(for: .milliseconds(10)) }
        engine.gate?.resume()
        for _ in 0..<100 where engine.cancelCount < 2 { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertGreaterThanOrEqual(engine.cancelCount, 2, "the engine that started after the cancel must be torn down")
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(box.value, "")
    }

    func testPartialAfterCancelIsIgnored() async {
        let engine = FakeEngine()
        let controller = makeController(engine, nil)
        let box = TextBox("")
        await begin(controller, base: "", box: box)
        engine.onPartial?("keep this")
        controller.cancel()
        engine.onPartial?("late words")
        XCTAssertEqual(box.value, "keep this")
        XCTAssertEqual(controller.phase, .idle)
    }

    func testSystemInterruptionFinishesTheDictationInsteadOfLosingIt() async {
        let engine = FakeEngine()
        engine.recording = recording()
        let controller = makeController(engine, .success(DictationTranscript(text: "Add the sunset shot.", isNoSpeech: false)))
        let box = TextBox("")
        await begin(controller, base: "", box: box)
        engine.onInterrupted?()
        await settle(controller, until: .idle)
        XCTAssertEqual(engine.stopCount, 1)
        XCTAssertEqual(box.value, "Add the sunset shot.")
    }

    func testHittingTheTimeLimitStillGetsTheServerText() async {
        let engine = FakeEngine()
        engine.recording = recording()
        let controller = DictationController(
            engine: engine,
            transcriber: FakeTranscriber(result: .success(DictationTranscript(text: "Full server text.", isNoSpeech: false))),
            maxDuration: .milliseconds(60)
        )
        let box = TextBox("")
        controller.start(base: "", contextTerms: []) { box.value = $0 }
        await settle(controller, until: .listening)
        engine.onPartial?("live words")
        await settle(controller, until: .idle)
        XCTAssertEqual(box.value, "Full server text.", "the auto-stop must not cancel its own server pass")
    }

    func testStartWhileAlreadyListeningDoesNothing() async {
        let engine = FakeEngine()
        let controller = makeController(engine, nil)
        let box = TextBox("")
        await begin(controller, base: "", box: box)
        controller.start(base: "x", contextTerms: []) { _ in }
        XCTAssertEqual(engine.startCount, 1)
    }

    func testRecordingFileIsDeletedAfterTheServerPass() async {
        let engine = FakeEngine()
        let file = recording()
        engine.recording = file
        let controller = makeController(engine, .success(DictationTranscript(text: "ok", isNoSpeech: false)))
        let box = TextBox("")
        await begin(controller, base: "", box: box)
        await controller.stop()
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.url.path))
    }

    func testContextTermsDropCameraRollFilenamesAndDuplicates() {
        let terms = DictationController.contextTerms(
            title: "Bodrum weekend",
            mediaNames: ["IMG_4021.MOV", "Sunset_Beach.mp4", "sunset beach.mov", "PXL_2025.mp4", "a.mov", "12345.mov"]
        )
        XCTAssertEqual(terms, ["Bodrum weekend", "Sunset Beach"])
    }

    func testJoinHandlesEmptyPieces() {
        XCTAssertEqual(DictationController.join("", "hello"), "hello")
        XCTAssertEqual(DictationController.join("typed  ", " hello "), "typed hello")
        XCTAssertEqual(DictationController.join("typed", "  "), "typed")
    }
}
