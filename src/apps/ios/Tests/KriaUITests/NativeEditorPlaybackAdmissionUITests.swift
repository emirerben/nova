import XCTest

@MainActor final class NativeEditorPlaybackAdmissionUITests: XCTestCase {
    func testPlayingTitleUpdateSettlesThroughTheRealSurfaceAndCompositor() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-playback-admission"]
        app.terminate()
        app.launch()
        app.activate()
        defer { app.terminate() }
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
        let result = app.staticTexts["playback-admission-result"]
        XCTAssertTrue(result.waitForExistence(timeout: 10))
        func report() -> [String: Any]? {
            guard let value = result.value as? String, let data = value.data(using: .utf8) else { return nil }
            return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }
        let ready = NSPredicate { _, _ in ["ready", "failed"].contains(report()?["phase"] as? String ?? "") }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: ready, object: nil)], timeout: 30), .completed)
        XCTAssertEqual(report()?["phase"] as? String, "ready", String(describing: report()))
        guard report()?["phase"] as? String == "ready" else { return }
        app.buttons["playback-admission-run"].tap()
        let finished = NSPredicate { _, _ in ["complete", "failed"].contains(report()?["phase"] as? String ?? "") }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: finished, object: nil)], timeout: 15), .completed)
        let values = try XCTUnwrap(report())
        let evidence = XCTAttachment(string: String(describing: values))
        evidence.name = "Foreground playing in-place frame admission"
        evidence.lifetime = .keepAlways
        add(evidence)
        let picture = XCTAttachment(screenshot: app.screenshot())
        picture.name = "Actual foreground player after title update"
        picture.lifetime = .keepAlways
        add(picture)
        XCTAssertEqual(values["phase"] as? String, "complete", String(describing: values))
        XCTAssertNil(values["error"])
        for key in ["sceneActive", "initialAdmitted", "beganPlaying", "textChangedAfterEdit", "pendingAfterEdit", "keptPlayingAfterEdit",
                    "settled", "samePlayer", "sameItem", "sameCompositor", "compositionChanged", "ticketChanged",
                    "previewReady", "previousPreviewAbsent"] {
            XCTAssertEqual(values[key] as? Bool, true, "\(key): \(values)")
        }
        let duration = try XCTUnwrap(values["sourceDuration"] as? Double)
        let editTime = try XCTUnwrap(values["editTime"] as? Double)
        let finalTime = try XCTUnwrap(values["finalTime"] as? Double)
        let finalRate = try XCTUnwrap(values["finalRate"] as? Double)
        XCTAssertGreaterThanOrEqual(duration, 4)
        XCTAssertGreaterThanOrEqual(editTime, 0.05)
        XCTAssertLessThan(editTime, 1)
        XCTAssertGreaterThan(finalTime, editTime + 0.1)
        XCTAssertLessThan(finalTime, 4)
        XCTAssertGreaterThan(finalRate, 0)
        XCTAssertEqual(values["currentText"] as? String, "A clearer story")
    }
}
