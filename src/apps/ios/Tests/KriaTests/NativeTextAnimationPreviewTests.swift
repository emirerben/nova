import XCTest
@testable import Kria
@testable import KriaMediaEngine

final class NativeTextAnimationPreviewTests: XCTestCase {
    func testEntranceRepeatsAfterReadableHoldAndResetGap() {
        let speed = 1.0
        let edge = NativeTextAnimationPreviewMath.edgeDuration(speed: speed)
        let first = NativeTextAnimationPreviewMath.sample(phase: "entrance", effect: "fade", speed: speed, clockTime: edge / 2)
        let gap = NativeTextAnimationPreviewMath.sample(phase: "entrance", effect: "fade", speed: speed, clockTime: edge + NativeTextAnimationPreviewMath.readableHold + 0.1)
        let repeated = NativeTextAnimationPreviewMath.sample(phase: "entrance", effect: "fade", speed: speed,
            clockTime: NativeTextAnimationPreviewMath.cycleDuration(phase: "entrance", speed: speed) + edge / 2)
        XCTAssertLessThan(first.alpha, 1)
        XCTAssertEqual(gap.alpha, 0)
        XCTAssertEqual(first.alpha, repeated.alpha, accuracy: 0.000000000001)
        XCTAssertEqual(first.scale, repeated.scale, accuracy: 0.000000000001)
        XCTAssertEqual(first.xTranslate, repeated.xTranslate, accuracy: 0.000000000001)
        XCTAssertEqual(first.yTranslate, repeated.yTranslate, accuracy: 0.000000000001)
        XCTAssertEqual(first.visibleText, repeated.visibleText)
    }

    func testExitDemonstratesDisappearanceThenResets() {
        let edge = NativeTextAnimationPreviewMath.edgeDuration(speed: 1)
        let visible = NativeTextAnimationPreviewMath.sample(phase: "exit", effect: "slide", speed: 1, clockTime: 0.2)
        let leaving = NativeTextAnimationPreviewMath.sample(phase: "exit", effect: "slide", speed: 1,
            clockTime: NativeTextAnimationPreviewMath.readableHold + edge / 2)
        let reset = NativeTextAnimationPreviewMath.sample(phase: "exit", effect: "slide", speed: 1,
            clockTime: NativeTextAnimationPreviewMath.readableHold + edge + 0.1)
        XCTAssertEqual(visible.alpha, 1)
        XCTAssertLessThan(leaving.alpha, 1)
        XCTAssertGreaterThan(leaving.xTranslate, 0)
        XCTAssertEqual(reset.alpha, 0)
    }

    func testEngineTranslationIsScaledToReadablePreviewText() {
        let slide = NativeTextAnimationPreviewMath.sample(phase: "entrance", effect: "slide", speed: 1, clockTime: 0)
        let bounce = NativeTextAnimationPreviewMath.sample(phase: "loop", effect: "bounce", speed: 1, clockTime: 0.3)
        XCTAssertEqual(slide.xTranslate * NativeTextAnimationPreviewMath.previewTranslationScale, 40 * 18 / 78, accuracy: 0.0001)
        XCTAssertEqual(abs(bounce.yTranslate * NativeTextAnimationPreviewMath.previewTranslationScale), 12 * 18 / 78, accuracy: 0.0001)
    }

    func testLoopUsesEngineSamplerAndSpeedFormula() throws {
        let clockTime = 0.3
        let preview = NativeTextAnimationPreviewMath.sample(phase: "loop", effect: "bounce", speed: 2, clockTime: clockTime)
        let engine = try TextAnimationPhases(loop: .bounce, speed: 2).sample(time: clockTime, duration: 2)
        XCTAssertEqual(preview.alpha, engine.alpha)
        XCTAssertEqual(preview.scale, engine.scale)
        XCTAssertEqual(preview.yTranslate, engine.yTranslate)
        XCTAssertEqual(NativeTextAnimationPreviewMath.cycleDuration(phase: "loop", speed: 2), 0.6)
    }

    @MainActor
    func testClockPausesWithoutKeepingADisplayLinkAlive() {
        let clock = NativeTextAnimationPreviewClock()
        clock.startAutoplay()
        XCTAssertTrue(clock.isPlaying)
        XCTAssertTrue(clock.hasActiveDisplayLink)
        clock.advance(timestamp: 1)
        clock.advance(timestamp: 1.2)
        let frozen = NativeTextAnimationPreviewMath.displaySample(
            phase: "loop", effect: "bounce", speed: 1, clockTime: clock.elapsed, hasEverPlayed: clock.hasEverPlayed
        )
        clock.pauseForLifecycle()
        clock.advance(timestamp: 1.5)
        let afterPause = NativeTextAnimationPreviewMath.displaySample(
            phase: "loop", effect: "bounce", speed: 1, clockTime: clock.elapsed, hasEverPlayed: clock.hasEverPlayed
        )
        XCTAssertFalse(clock.isPlaying)
        XCTAssertFalse(clock.hasActiveDisplayLink)
        XCTAssertEqual(afterPause, frozen)
    }

    @MainActor
    func testDisplayLinkTargetDoesNotRetainTornDownClock() {
        var clock: NativeTextAnimationPreviewClock? = NativeTextAnimationPreviewClock()
        weak var releasedClock = clock
        clock?.startAutoplay()
        clock = nil
        XCTAssertNil(releasedClock)
    }

    func testNoneIsStableAndTypewriterUsesSharedVisiblePrefix() {
        let none = NativeTextAnimationPreviewMath.sample(phase: "entrance", effect: "none", speed: 1, clockTime: 9)
        let typewriter = NativeTextAnimationPreviewMath.sample(phase: "entrance", effect: "typewriter", speed: 1, clockTime: 0.2)
        XCTAssertEqual(none.alpha, 1)
        XCTAssertEqual(none.visibleText, "Text")
        XCTAssertEqual(typewriter.visibleText, TextAnimationPhases.visiblePrefix("Text", progress: 0.5))
    }

    func testReduceMotionDisablesAutoplayButDoesNotChangePreviewSamples() {
        XCTAssertFalse(NativeTextAnimationPreviewAutoplay.allows(reduceMotion: true, environment: [:]))
        XCTAssertFalse(NativeTextAnimationPreviewAutoplay.allows(reduceMotion: false, environment: ["UI_TEST_REDUCE_MOTION": "1"]))
        XCTAssertTrue(NativeTextAnimationPreviewAutoplay.allows(reduceMotion: false, environment: [:]))
        XCTAssertLessThan(NativeTextAnimationPreviewMath.sample(phase: "entrance", effect: "fade", speed: 1, clockTime: 0.1).alpha, 1)
    }

    func testInitialReduceMotionSampleIsReadableBeforeManualPlay() {
        let initial = NativeTextAnimationPreviewMath.displaySample(
            phase: "entrance", effect: "fade", speed: 1, clockTime: 0, hasEverPlayed: false
        )
        XCTAssertEqual(initial.alpha, 1)
        XCTAssertEqual(initial.visibleText, "Text")
        XCTAssertLessThan(NativeTextAnimationPreviewMath.displaySample(
            phase: "entrance", effect: "fade", speed: 1, clockTime: 0.1, hasEverPlayed: true
        ).alpha, 1)
    }

    @MainActor
    func testManualPauseSurvivesBackgroundAndDoesNotCreateAnotherClock() {
        let clock = NativeTextAnimationPreviewClock()
        clock.startAutoplay()
        clock.toggleManually()
        clock.pauseForLifecycle()
        clock.startAutoplay()
        XCTAssertFalse(clock.isPlaying)
        XCTAssertFalse(clock.hasActiveDisplayLink)
        clock.toggleManually()
        XCTAssertTrue(clock.isPlaying)
        clock.pauseForLifecycle()
    }
}
