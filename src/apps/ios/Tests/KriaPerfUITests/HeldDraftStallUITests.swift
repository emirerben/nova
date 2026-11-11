import XCTest

/// KRI-1239: TestFlight 1910 and 1926 were killed by the iOS watchdog (0x8BADF00D: the main thread missed a scene
/// update for 10 s) after "Make it without these" on Qendresa's held Talking draft, while Kria was thinking. The main
/// thread was inside the transcript's `ForEach`, hashing strings.
///
/// This replays that thread (`KRIA_CHAT_REPLAY=held-draft`: her real events, 12 receipts, the thoughts Kria streams
/// while a turn runs), taps "Make it without these", leaves the app and comes back while Kria thinks, and reads how
/// long the main thread was blocked after the tap (`main.stall@…`, measured inside the app by `MainThreadStallProbe`).
/// Run it on the optimized build, like `scripts/ios/bench.sh` (Debug timings say little about the shipped app):
///
///     xcodebuild build-for-testing -project Kria.xcodeproj -scheme "Kria Perf" -configuration Debug \
///       -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -enableCodeCoverage NO \
///       SWIFT_OPTIMIZATION_LEVEL=-O SWIFT_COMPILATION_MODE=wholemodule
///     xcodebuild test-without-building ... -only-testing:KriaPerfUITests/HeldDraftStallUITests
///
/// Before the fix (2026-11-08, iPhone 16 Pro simulator, iOS 26.5) the tap blocked the main thread for 973-1523 ms,
/// inside `ForEach` → `timelineRow` → `messageRow` → `echoesChoiceAnswer`/`choiceQuestionMode` → `timeline` →
/// String hashing (the same five app frames as the 1926 watchdog report), and the chat then re-scrolled on every
/// content-height change: ~90 scroll/re-measure rounds in 5 s, and in 4 of 8 runs a layout loop that never ended.
/// After it: 171-202 ms worst block, no endless loop in 6 runs. Timings need a quiet Mac: with ffmpeg jobs running
/// beside it (load average 60), one fixed run measured 586 ms.
@MainActor
final class HeldDraftStallUITests: XCTestCase {
    /// The worst main-thread block allowed on the simulator. The watchdog's limit on the phone is 10 s; an iPhone 15 is
    /// a few times slower than a Mac running the simulator, so even 1 s here would be too close.
    private static let maxStallMS = 500
    /// Delta polls Kria thinks for after the tap (~1 s each).
    private static let polls = 30

    override func setUp() {
        continueAfterFailure = false
    }

    func testThinkingAfterMakeItWithoutTheseNeverBlocksTheMainThread() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment = [
            "KRIA_CHAT_CREATION_FLOW": "v2",
            "KRIA_CHAT_REPLAY": "held-draft",
            "KRIA_CHAT_REPLAY_POLLS": "\(Self.polls)",
            "KRIA_PERF_COUNTERS": "1",
        ]
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 3))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()

        let simpler = app.buttons["choice-option-simpler_version"]
        XCTAssertTrue(simpler.waitForExistence(timeout: 30), "the second hold offers Make it without these")
        XCTAssertTrue(app.descendants(matching: .any)["requirement-chip-r12"].waitForExistence(timeout: 10),
                      "the hold's 12 receipts are under the question")
        RunLoop.current.run(until: Date().addingTimeInterval(3))
        let opened = counters(app)
        simpler.tap()

        // Kria thinks for ~30 polls. Halfway, she leaves the app and comes back, as on the phone (the kill was a
        // scene update: the app going to the background).
        RunLoop.current.run(until: Date().addingTimeInterval(12))
        XCUIDevice.shared.press(.home)
        RunLoop.current.run(until: Date().addingTimeInterval(3))
        app.activate()

        let reply = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Left out as agreed")).firstMatch
        let deadline = Date().addingTimeInterval(120)
        while Date() < deadline, !reply.exists {
            XCTAssertNotEqual(app.state, .notRunning, "the app died while Kria was thinking")
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        }
        let arrived = reply.exists
        RunLoop.current.run(until: Date().addingTimeInterval(3))
        // Reported, not asserted: what the settled transcript still does in 3 s when left alone.
        let settled = counters(app)
        RunLoop.current.run(until: Date().addingTimeInterval(3))
        let finished = counters(app)
        let busy = ["chat.rowBuilt", "chat.scrollToEnd"]
            .map { "\($0)+\((finished[$0] ?? 0) - (settled[$0] ?? 0))" }.joined(separator: " ")
        // Only the blocks that started after the tap: opening a cold simulator app has its own, unrelated first stall.
        let tappedAt = opened["main.uptimeMs"] ?? 0
        let afterTap = finished.compactMap { key, value -> (Int, Int)? in
            guard key.hasPrefix("main.stall@"), let start = Int(key.dropFirst("main.stall@".count)), start >= tappedAt else { return nil }
            return (start - tappedAt, value)
        }.sorted { $0.0 < $1.0 }
        let worst = afterTap.map(\.1).max() ?? 0
        let line = afterTap.map { "+\($0.0)ms:\($0.1)ms" }.joined(separator: " ")
        let rebuilds = finished.filter { $0.key.hasPrefix("timeline.") }.map { "\($0.key)=\($0.value)" }.joined(separator: " ")
        let sinceTap = ["chat.rowBuilt", "chat.scrollToEnd"].map { "\($0)+\((settled[$0] ?? 0) - (opened[$0] ?? 0))" }.joined(separator: " ")
        let scrollsSinceTap = (settled["chat.scrollToEnd"] ?? 0) - (opened["chat.scrollToEnd"] ?? 0)
        print("PERF HeldDraftStall worstAfterTapMs=\(worst) openedMaxStallMs=\(opened["main.maxStallMs"] ?? -1) \(rebuilds) sinceTap=[\(sinceTap)] quiet3s=[\(busy)] stalls=[\(line)]")
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "simpler-version-reply"; shot.lifetime = .keepAlways; add(shot)

        XCTAssertTrue(arrived, "the simpler version's reply arrived")
        XCTAssertGreaterThan(tappedAt, 0, "the in-app stall probe is running")
        XCTAssertLessThan(worst, Self.maxStallMS, "after the tap the main thread was blocked for \(worst) ms (blocks of 100 ms+: \(line))")
        // About two follows per poll while Kria thinks (each poll changes the transcript): 54-68 after the fix.
        // Before it, every content-height change scrolled again and re-measured the lazy rows: 118-120 scrolls, or a
        // loop that never ended.
        XCTAssertLessThanOrEqual(scrollsSinceTap, 3 * Self.polls, "the chat kept re-scrolling to the end (\(sinceTap))")
    }

    private func counters(_ app: XCUIApplication) -> [String: Int] {
        let probe = app.staticTexts["perf-counters"]
        guard probe.waitForExistence(timeout: 5) else { return [:] }
        var result: [String: Int] = [:]
        for line in probe.label.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1)
            if parts.count == 2, let value = Int(parts[1]) { result[String(parts[0])] = value }
        }
        return result
    }
}
