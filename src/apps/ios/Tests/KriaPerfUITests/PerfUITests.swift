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
        // The network model is an assumption (default: a good connection, 150 ms per response and 30 ms of server work
        // on the full chat read; the real handler measured ~2 ms). Change it per run without a rebuild:
        // `TEST_RUNNER_KRIA_PERF_LATENCY_MS=400 TEST_RUNNER_KRIA_PERF_FULL_PROJECTION_MS=30 scripts/ios/bench.sh ...`.
        let runner = ProcessInfo.processInfo.environment
        var base: [String: String] = [
            "KRIA_CHAT_CREATION_FLOW": "v2",
            "KRIA_FIXTURE_LATENCY_MS": runner["KRIA_PERF_LATENCY_MS"] ?? "150",
            "KRIA_FIXTURE_FULL_PROJECTION_MS": runner["KRIA_PERF_FULL_PROJECTION_MS"] ?? "30",
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
        // The transcript cache writes a second after the last change; killing the app sooner would lose it.
        RunLoop.current.run(until: Date().addingTimeInterval(2.5))
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
        measure(metrics: [interval("launch.toTranscript"), interval("launch.toPixels"), interval("launch.init"), interval("appmodel.init")], options: options) {
            let app = makeApp()
            app.launch()
            waitForTranscript(app)
            // The requests the launch makes, including the ones that run alongside the transcript.
            RunLoop.current.run(until: Date().addingTimeInterval(2))
            book.add(counters(app))
            app.terminate()
        }
        book.report("testLaunchToTranscript", prefixes: ["net ", "auth ", "fullread ", "t ", "fixture "])
    }

    /// The same launch after the access token has expired: every first request 401s and waits for a refresh.
    func testLaunchExpiredToken() {
        let environment = ["KRIA_FIXTURE_EXPIRED_TOKEN": "1"]
        warmCache(environment)
        var book = CounterBook()
        measure(metrics: [interval("launch.toTranscript"), interval("launch.toPixels")], options: options) {
            let app = makeApp(environment)
            app.launch()
            waitForTranscript(app)
            RunLoop.current.run(until: Date().addingTimeInterval(2))
            book.add(counters(app))
            app.terminate()
        }
        book.report("testLaunchExpiredToken", prefixes: ["net ", "auth ", "fullread ", "t ", "fixture "])
    }

    // MARK: Opening chats

    /// Functional guard for the persisted transcript: after a chat has been opened once, relaunching shows its
    /// messages while every chat read is still ten seconds away. Fails on a build that waits for the network.
    func testCachedTranscriptShowsBeforeTheNetworkAnswers() {
        warmCache()
        let app = makeApp(["KRIA_CHAT_SLOW_READS": "1"])
        app.launch()
        XCTAssertTrue(
            transcriptRows(app).firstMatch.waitForExistence(timeout: 4),
            "The saved transcript should show without waiting for the (10 s) chat read"
        )
    }

    /// Opens a chat from the drawer, one never opened before each time (10 to 19, inside the 20 the list returns).
    func testOpenOldChatFromDrawer() {
        warmCache()
        let app = makeApp()
        app.launch()
        waitForTranscript(app)
        var next = 10
        var book = CounterBook()
        var before = counters(app)
        measure(metrics: [interval("chat.open"), interval("chat.toPixels"), interval("drawer.open")], options: options) {
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
        book.report("testOpenOldChatFromDrawer", prefixes: ["net ", "fullread "])
    }

    /// Opens `title` from the drawer and waits for its transcript.
    private func openChat(_ app: XCUIApplication, _ title: String) {
        app.buttons["workspace-menu-toggle"].tap()
        let row = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", title)).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 10), "\(title) is not in the drawer")
        row.tap()
        waitForTranscript(app)
    }

    /// Switching back to a chat already opened on this device, the common case: Chat 10 and Chat 11 are each opened
    /// once, then every measured iteration alternates between them.
    func testReopenSeenChat() {
        warmCache()
        let app = makeApp()
        app.launch()
        waitForTranscript(app)
        for title in ["Chat 10", "Chat 11"] {
            openChat(app, title)
            // Let the transcript cache write (it is debounced by a second) before leaving the chat.
            RunLoop.current.run(until: Date().addingTimeInterval(2.5))
        }
        var next = 10
        measure(metrics: [interval("chat.open"), interval("chat.toPixels")], options: options) {
            openChat(app, "Chat \(next)")
            next = next == 10 ? 11 : 10
        }
    }

    /// Functional guard for chat-list paging: with 60 chats the drawer must reach Chat 45 by scrolling. Fails on a
    /// build that only ever loads the newest 20.
    func testDrawerReachesOlderChats() {
        let app = makeApp(reset: true)
        app.launch()
        waitForTranscript(app)
        app.buttons["workspace-menu-toggle"].tap()
        let target = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Chat 45'")).firstMatch
        var swipes = 0
        while !target.exists, swipes < 30 {
            app.swipeUp()
            swipes += 1
        }
        XCTAssertTrue(target.exists, "Chat 45 never appeared in the drawer after \(swipes) scrolls")
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

    /// The same flicks with nothing else going on (no upload ticks, no plan blocks, no song card): what plain
    /// scrolling a long transcript costs.
    func testScrollQuietTranscript() {
        warmCache()
        let app = makeApp()
        app.launch()
        waitForTranscript(app)
        let scroll = app.scrollViews.firstMatch
        XCTAssertTrue(scroll.waitForExistence(timeout: 10))
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
        print("PERF testScrollQuietTranscript timeline.build=\(Double(rebuilds) / Double(iterations))")
    }

    // MARK: Editor

    private var editorEnvironment: [String: String] {
        ["KRIA_EDITOR_FIXTURE": "1", "KRIA_CHAT_LONG_HISTORY": "12"]
    }

    /// Opens the chat's finished render in the native editor and waits until it has finished opening (the preview built
    /// from the source clips). The counters are read through their own window, which stays readable under the editor.
    private func openEditor(_ app: XCUIApplication, file: StaticString = #filePath, line: UInt = #line) {
        let open = app.buttons["Open editor"]
        XCTAssertTrue(open.waitForExistence(timeout: 20), "The chat offers no Open editor", file: file, line: line)
        open.tap()
        XCTAssertTrue(app.staticTexts["native-editor-duration"].waitForExistence(timeout: 30), "The editor never opened", file: file, line: line)
        let deadline = Date().addingTimeInterval(60)
        while Date() < deadline {
            if counters(app)["t editor.open.done"] != nil { return }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        XCTFail("The editor never finished opening", file: file, line: line)
    }

    /// A first open: nothing downloaded yet, so the editor fetches the job, downloads and hashes three clips, and builds
    /// the preview.
    func testEditorOpen() {
        var book = CounterBook()
        measure(metrics: [interval("editor.open"), interval("editor.preview")], options: options) {
            let app = makeApp(reset: true, editorEnvironment)
            app.launch()
            waitForTranscript(app)
            openEditor(app)
            book.add(counters(app))
            app.terminate()
        }
        book.report("testEditorOpen", prefixes: ["net ", "source", "sha256", "t "])
    }

    /// Opening a project whose sources an earlier session already downloaded (a fresh launch, so a fresh editor
    /// session, but the on-disk source cache is warm).
    func testEditorReopenWarm() {
        let warm = makeApp(reset: true, editorEnvironment)
        warm.launch()
        waitForTranscript(warm)
        openEditor(warm)
        warm.terminate()
        var book = CounterBook()
        measure(metrics: [interval("editor.open"), interval("editor.preview")], options: options) {
            let app = makeApp(editorEnvironment)
            app.launch()
            waitForTranscript(app)
            openEditor(app)
            book.add(counters(app))
            app.terminate()
        }
        book.report("testEditorReopenWarm", prefixes: ["net ", "source", "sha256", "t "])
    }

    // MARK: Gallery

    /// The first poster on screen. On failure the accessibility tree is printed, so a missing poster can be told
    /// apart from a gallery that never opened.
    private func waitForPoster(_ app: XCUIApplication, file: StaticString = #filePath, line: UInt = #line) {
        let poster = app.images["project-poster-image"].firstMatch
        guard poster.waitForExistence(timeout: 30) else {
            let buttons = app.buttons.allElementsBoundByIndex.prefix(25).map(\.label).joined(separator: " | ")
            let states = ["Loading your cuts…", "Your next cut will live here.", "Your gallery couldn’t load."]
                .filter { app.staticTexts[$0].exists }
            print("PERFDEBUG no poster. buttons: \(buttons). gallery states: \(states). images: \(app.images.count). counters: \(counters(app))")
            XCTFail("No gallery poster appeared", file: file, line: line)
            return
        }
    }

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
            waitForPoster(app)
            closeGallery(app)
        }
    }

    /// Tapping Gallery straight after launch, with a 200-video library (by default): the list has to load before anything can be
    /// drawn. A fresh launch each time, nothing cached (not even posters).
    func testGalleryOpenRightAfterLaunch() {
        // `TEST_RUNNER_KRIA_PERF_GALLERY_ITEMS=600` tries a bigger library: the old code waited for every page.
        let items = ProcessInfo.processInfo.environment["KRIA_PERF_GALLERY_ITEMS"] ?? "200"
        measure(metrics: [interval("gallery.open")], options: options) {
            let app = makeApp(reset: true, ["KRIA_GALLERY_ITEMS": items])
            app.launch()
            waitForTranscript(app)
            openGallery(app)
            waitForPoster(app)
            app.terminate()
        }
    }

    /// Functional guard for library paging: with 200 videos the gallery reaches video 150 by scrolling. Fails on a
    /// build that never loads past what it fetched first.
    func testGalleryReachesOlderVideos() {
        let app = makeApp(reset: true, ["KRIA_GALLERY_ITEMS": "200"])
        app.launch()
        waitForTranscript(app)
        openGallery(app)
        waitForPoster(app)
        let target = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Cut 150'")).firstMatch
        var swipes = 0
        while !target.exists, swipes < 60 {
            app.swipeUp()
            swipes += 1
        }
        XCTAssertTrue(target.exists, "Cut 150 never appeared in the gallery after \(swipes) scrolls")
    }

    /// Reopening the gallery should not fetch posters it already showed; reports the network fetches per reopen.
    func testGalleryReopenPosterFetches() {
        let environment = ["KRIA_GALLERY_ITEMS": "200"]
        let app = makeApp(reset: true, environment)
        app.launch()
        waitForTranscript(app)
        openGallery(app)
        waitForPoster(app)
        let afterFirstOpen = counters(app)["poster.network"] ?? 0
        closeGallery(app)
        openGallery(app)
        waitForPoster(app)
        RunLoop.current.run(until: Date().addingTimeInterval(2))
        let afterReopen = counters(app)["poster.network"] ?? 0
        add(XCTAttachment(string: "poster.network first=\(afterFirstOpen) reopen=\(afterReopen - afterFirstOpen)"))
        print("PERF testGalleryReopenPosterFetches poster.first=\(afterFirstOpen) poster.reopen=\(afterReopen - afterFirstOpen)")
    }
}
