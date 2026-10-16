import XCTest

/// Performance benchmarks for launch, screen-open, scrolling and the gallery. Run through `scripts/ios/bench.sh`
/// (optimized build, no coverage), not the Kria scheme: Debug numbers say nothing about the shipped app.
///
/// The app is the real app against the offline fixtures; `PerfFixture` (Debug builds) supplies a slow network
/// (`KRIA_FIXTURE_LATENCY_MS`), a large account (`KRIA_CHAT_SEED_THREADS`, `KRIA_CHAT_LONG_HISTORY`, `KRIA_GALLERY_ITEMS`)
/// and an expired session. Intervals come from the app's own signposts (`Perf`, subsystem `com.kria.app`).
///
/// `TEST_RUNNER_KRIA_PERF_ITERATIONS=10` (set by `bench.sh --iterations`) sets how many times each test measures.
@MainActor
final class PerfUITests: XCTestCase {
    private static let subsystem = "com.kria.app"
    private static let category = "perf"

    private var iterations: Int {
        Int(ProcessInfo.processInfo.environment["KRIA_PERF_ITERATIONS"] ?? "") ?? 5
    }

    private var options: XCTMeasureOptions {
        let options = XCTMeasureOptions()
        options.iterationCount = iterations
        return options
    }

    private func interval(_ name: String) -> XCTOSSignpostMetric {
        XCTOSSignpostMetric(subsystem: Self.subsystem, category: Self.category, name: name)
    }

    /// A launch-ready app: a realistic account (60 chats of 200 messages), a slow network and a warm disk cache.
    private func makeApp(reset: Bool = false, _ environment: [String: String] = [:]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"] + (reset ? ["-perf-reset"] : [])
        var base: [String: String] = [
            "KRIA_CHAT_CREATION_FLOW": "v2",
            "KRIA_FIXTURE_LATENCY_MS": "150",
            "KRIA_FIXTURE_FULL_PROJECTION_MS": "600",
            "KRIA_CHAT_SEED_THREADS": "60",
            "KRIA_CHAT_LONG_HISTORY": "200",
            "KRIA_PERF_DISK_STORE": "1",
            "KRIA_PERF_COUNTERS": "1",
        ]
        base.merge(environment) { _, override in override }
        app.launchEnvironment = base
        return app
    }

    private func transcriptRows(_ app: XCUIApplication) -> XCUIElementQuery {
        app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'chat-message-'"))
    }

    private func waitForTranscript(_ app: XCUIApplication, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(transcriptRows(app).firstMatch.waitForExistence(timeout: 30), "The transcript never appeared", file: file, line: line)
    }

    /// One launch that fills the on-disk chat list, so the measured launches start with a populated cache.
    private func warmCache(_ environment: [String: String] = [:]) {
        let app = makeApp(reset: true, environment)
        app.launch()
        waitForTranscript(app)
        app.terminate()
    }

    /// Sums of `perf-counters` readings, so a counter can be reported per iteration.
    private struct CounterBook {
        private(set) var totals: [String: Int] = [:]
        private(set) var samples = 0
        mutating func add(_ reading: [String: Int]) {
            for (key, value) in reading { totals[key, default: 0] += value }
            samples += 1
        }
        /// `PERF <test> key=average ...` for the keys `prefixes` select: requests per route, rebuilds, fetches.
        func report(_ test: String, prefixes: [String]) {
            guard samples > 0 else { return }
            let pairs = totals.filter { entry in prefixes.contains { entry.key.hasPrefix($0) } }
                .sorted { $0.key < $1.key }
                .map { "\($0.key.replacingOccurrences(of: " ", with: "_"))=\(String(format: "%.2f", Double($0.value) / Double(samples)))" }
            print("PERF \(test) \(pairs.joined(separator: " "))")
        }
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

    // MARK: Launch

    /// Process start to responsive, as the system measures it.
    func testColdLaunch() {
        warmCache()
        let app = makeApp()
        measure(metrics: [XCTApplicationLaunchMetric(waitUntilResponsive: true)], options: options) {
            app.launch()
            app.terminate()
        }
    }

    /// App start until a transcript row is on screen, with the chat list cached and the network slow.
    func testLaunchToTranscript() {
        warmCache()
        var book = CounterBook()
        measure(metrics: [interval("launch.toTranscript"), interval("launch.init"), interval("appmodel.init")], options: options) {
            let app = makeApp()
            app.launch()
            waitForTranscript(app)
            // The requests the launch makes, including the ones that run alongside the transcript.
            RunLoop.current.run(until: Date().addingTimeInterval(2))
            book.add(counters(app))
            app.terminate()
        }
        book.report("testLaunchToTranscript", prefixes: ["net ", "auth "])
    }

    /// The same launch after the access token has expired: every first request 401s and waits for a refresh.
    func testLaunchExpiredToken() {
        let environment = ["KRIA_FIXTURE_EXPIRED_TOKEN": "1"]
        warmCache(environment)
        var book = CounterBook()
        measure(metrics: [interval("launch.toTranscript")], options: options) {
            let app = makeApp(environment)
            app.launch()
            waitForTranscript(app)
            RunLoop.current.run(until: Date().addingTimeInterval(2))
            book.add(counters(app))
            app.terminate()
        }
        book.report("testLaunchExpiredToken", prefixes: ["net ", "auth "])
    }

    // MARK: Opening chats

    /// Opens a chat from the drawer, one never opened before each time (10 to 19, inside the 20 the list returns).
    func testOpenOldChatFromDrawer() {
        warmCache()
        let app = makeApp()
        app.launch()
        waitForTranscript(app)
        var next = 10
        var book = CounterBook()
        var before = counters(app)
        measure(metrics: [interval("chat.open"), interval("drawer.open")], options: options) {
            let title = "Chat \(next)"
            next = next == 19 ? 10 : next + 1
            app.buttons["workspace-menu-toggle"].tap()
            let row = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", title)).firstMatch
            XCTAssertTrue(row.waitForExistence(timeout: 10), "\(title) is not in the drawer")
            row.tap()
            waitForTranscript(app)
            RunLoop.current.run(until: Date().addingTimeInterval(1.5))
            let after = counters(app)
            book.add(after.reduce(into: [:]) { $0[$1.key] = $1.value - (before[$1.key] ?? 0) })
            before = after
        }
        book.report("testOpenOldChatFromDrawer", prefixes: ["net "])
    }

    // MARK: Scrolling

    /// Flicks a 200-message transcript, with plan blocks and a song question mixed in and an upload reporting
    /// progress, the way a busy chat redraws.
    func testScrollLongTranscript() {
        warmCache()
        let app = makeApp(["KRIA_CHAT_PLAN_BLOCKS": "1", "KRIA_CHAT_SONG_ORDER": "1", "KRIA_FIXTURE_UPLOAD_TICKS": "1"])
        app.launch()
        waitForTranscript(app)
        let scroll = app.scrollViews.firstMatch
        XCTAssertTrue(scroll.waitForExistence(timeout: 10))
        // CPU time over an identical gesture is the sim-friendly proxy for redraw cost; hitches need a real phone.
        var metrics: [XCTMetric] = [
            XCTOSSignpostMetric.scrollDecelerationMetric,
            XCTOSSignpostMetric.scrollDraggingMetric,
            XCTCPUMetric(application: app),
        ]
        if #available(iOS 26, *) { metrics.append(XCTHitchMetric(application: app)) }
        let before = counters(app)
        measure(metrics: metrics, options: options) {
            for _ in 0..<4 { scroll.swipeDown(velocity: .fast) }
            for _ in 0..<4 { scroll.swipeUp(velocity: .fast) }
        }
        let after = counters(app)
        let rebuilds = (after["timeline.build"] ?? 0) - (before["timeline.build"] ?? 0)
        let feeds = (after["planFeed.reduce"] ?? 0) - (before["planFeed.reduce"] ?? 0)
        // Per measured iteration; the upload ticks keep redrawing the chat for as long as the test runs.
        print("PERF testScrollLongTranscript timeline.build=\(Double(rebuilds) / Double(iterations)) planFeed.reduce=\(Double(feeds) / Double(iterations))")
    }

    // MARK: Gallery

    /// From the chat, or from the drawer the gallery's menu button returns to.
    private func openGallery(_ app: XCUIApplication) {
        let gallery = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Gallery'")).firstMatch
        if !gallery.exists { app.buttons["workspace-menu-toggle"].tap() }
        XCTAssertTrue(gallery.waitForExistence(timeout: 10))
        gallery.tap()
    }

    /// The gallery's only control is its menu button, which hands back to the (open) drawer.
    private func closeGallery(_ app: XCUIApplication) {
        let menu = app.buttons.matching(NSPredicate(format: "label == 'Open projects'")).firstMatch
        XCTAssertTrue(menu.waitForExistence(timeout: 10))
        menu.tap()
    }

    /// Gallery to the first screen of posters, 200 videos, posters re-signed on every fetch.
    func testGalleryOpen() {
        warmCache(["KRIA_GALLERY_ITEMS": "200"])
        let app = makeApp(["KRIA_GALLERY_ITEMS": "200"])
        app.launch()
        waitForTranscript(app)
        measure(metrics: [interval("gallery.open")], options: options) {
            openGallery(app)
            XCTAssertTrue(app.images["project-poster-image"].firstMatch.waitForExistence(timeout: 30))
            closeGallery(app)
        }
    }

    /// Reopening the gallery should not fetch posters it already showed; reports the network fetches per reopen.
    func testGalleryReopenPosterFetches() {
        let environment = ["KRIA_GALLERY_ITEMS": "200"]
        let app = makeApp(reset: true, environment)
        app.launch()
        waitForTranscript(app)
        openGallery(app)
        XCTAssertTrue(app.images["project-poster-image"].firstMatch.waitForExistence(timeout: 30))
        let afterFirstOpen = counters(app)["poster.network"] ?? 0
        closeGallery(app)
        openGallery(app)
        XCTAssertTrue(app.images["project-poster-image"].firstMatch.waitForExistence(timeout: 30))
        RunLoop.current.run(until: Date().addingTimeInterval(2))
        let afterReopen = counters(app)["poster.network"] ?? 0
        add(XCTAttachment(string: "poster.network first=\(afterFirstOpen) reopen=\(afterReopen - afterFirstOpen)"))
        print("PERF testGalleryReopenPosterFetches poster.first=\(afterFirstOpen) poster.reopen=\(afterReopen - afterFirstOpen)")
    }
}
