import KriaMediaEngine
import SwiftUI
import UIKit

@MainActor
private final class NativeTextAnimationPreviewDisplayLinkTarget: NSObject {
    weak var clock: NativeTextAnimationPreviewClock?

    init(clock: NativeTextAnimationPreviewClock) {
        self.clock = clock
    }

    @objc func tick(_ link: CADisplayLink) {
        clock?.advance(timestamp: link.timestamp)
    }
}

/// A single display link feeds every candidate tile in the text-animation picker.
@MainActor
final class NativeTextAnimationPreviewClock: ObservableObject {
    @Published private(set) var elapsed: TimeInterval = 0
    @Published private(set) var isPlaying = false
    @Published private(set) var hasEverPlayed = false

    private var displayLink: CADisplayLink?
    private var displayLinkTarget: NativeTextAnimationPreviewDisplayLinkTarget?
    private var lastTimestamp: CFTimeInterval?
    private var userPaused = false
    var hasActiveDisplayLink: Bool { displayLink != nil }

    func startAutoplay() {
        guard !userPaused else { return }
        play()
    }

    func toggleManually() {
        if isPlaying {
            userPaused = true
            pause()
        } else {
            userPaused = false
            play()
        }
    }

    func pauseForLifecycle() { pause() }

    private func play() {
        guard !isPlaying else { return }
        isPlaying = true
        hasEverPlayed = true
        lastTimestamp = nil
        let target = NativeTextAnimationPreviewDisplayLinkTarget(clock: self)
        let link = CADisplayLink(target: target, selector: #selector(NativeTextAnimationPreviewDisplayLinkTarget.tick(_:)))
        link.preferredFramesPerSecond = 30
        link.add(to: .main, forMode: .common)
        displayLinkTarget = target
        displayLink = link
    }

    private func pause() {
        displayLink?.invalidate()
        displayLink = nil
        displayLinkTarget = nil
        lastTimestamp = nil
        isPlaying = false
    }

    func advance(timestamp: CFTimeInterval) {
        guard isPlaying else { return }
        defer { lastTimestamp = timestamp }
        guard let lastTimestamp else { return }
        elapsed += max(0, timestamp - lastTimestamp)
    }

    deinit {
        MainActor.assumeIsolated {
            displayLink?.invalidate()
        }
    }
}

struct NativeTextAnimationPreview: View {
    let phase: String
    let effect: String
    let speed: Double
    @ObservedObject var clock: NativeTextAnimationPreviewClock

    var body: some View {
        if let edge = TextAnimationPhases.Edge(rawValue: effect), edge.isDesigned {
            NativeDesignedAnimationTile(edge: edge, phase: phase, speed: speed, clock: clock)
        } else {
        GeometryReader { proxy in
            let sample = NativeTextAnimationPreviewMath.displaySample(
                phase: phase, effect: effect, speed: speed, clockTime: clock.elapsed,
                hasEverPlayed: clock.hasEverPlayed
            )
            Text(sample.visibleText)
                .font(KriaFont.body(18).weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.65)
                .opacity(sample.alpha)
                .scaleEffect(sample.scale)
                // Engine translations are authored around the 78 px text style. Scale
                // them to this 18 pt sample text, retaining the renderer's Y sign.
                .offset(x: sample.xTranslate * NativeTextAnimationPreviewMath.previewTranslationScale,
                        y: -sample.yTranslate * NativeTextAnimationPreviewMath.previewTranslationScale)
                .frame(width: proxy.size.width, height: proxy.size.height)
                .accessibilityHidden(true)
        }
        }
    }
}

private struct NativeDesignedAnimationTile: View {
    let edge: TextAnimationPhases.Edge
    let phase: String
    let speed: Double
    @ObservedObject var clock: NativeTextAnimationPreviewClock
    @State private var renderer: DesignedTextAnimationPreview?
    @State private var image: CGImage?

    var body: some View {
        Group {
            if let image { Image(decorative: image, scale: 1).resizable().scaledToFit() }
            else { Text("Text").font(KriaFont.body(18).weight(.semibold)) }
        }
        .accessibilityHidden(true)
        .task(id: "\(edge.rawValue)-\(phase)-\(speed)") {
            guard let font = NativeFontCatalog.shared.fontURL(for: "Inter") else { return }
            renderer = try? DesignedTextAnimationPreview(fontURL: font, effect: edge, exit: phase == "exit", speed: speed,
                duration: NativeTextAnimationPreviewMath.layerDuration(speed: speed))
            refresh()
        }
        .onChange(of: clock.elapsed) { _, _ in refresh() }
    }
    private func refresh() {
        // The same cycle as the legacy cards, so every card plays at one pace.
        let speed = NativeTextAnimationPreviewMath.normalizedSpeed(speed)
        let time = clock.hasEverPlayed
            ? NativeTextAnimationPreviewMath.layerTime(phase: phase, speed: speed, clockTime: clock.elapsed)
            : NativeTextAnimationPreviewMath.edgeDuration(speed: speed) + NativeTextAnimationPreviewMath.readableHold
        image = try? renderer?.image(time: time ?? NativeTextAnimationPreviewMath.layerDuration(speed: speed))
    }
}

struct NativeTextAnimationPreviewSample: Equatable {
    let alpha: Double
    let scale: Double
    let xTranslate: Double
    let yTranslate: Double
    let visibleText: String
}

enum NativeTextAnimationPreviewMath {
    static let previewText = "Text"
    static let previewTranslationScale = 18.0 / 78.0
    static let readableHold = 0.9
    static let resetGap = 0.55

    static func edgeDuration(speed: Double) -> Double { TextAnimationPhases.edgeSeconds / normalizedSpeed(speed) }

    /// One preview layer: entrance edge, readable hold, exit edge.
    static func layerDuration(speed: Double) -> Double { 2 * edgeDuration(speed: speed) + readableHold }

    /// The preview layer's local time for an In or Out card at `clockTime`, or nil while the card is blank.
    static func layerTime(phase: String, speed: Double, clockTime: Double) -> Double? {
        let edge = edgeDuration(speed: speed)
        let cycleTime = clockTime.truncatingRemainder(dividingBy: cycleDuration(phase: phase, speed: speed))
        switch phase {
        case "entrance": return cycleTime < edge + readableHold ? cycleTime : nil
        case "exit": return cycleTime < readableHold + edge ? (cycleTime < readableHold ? edge : edge + cycleTime) : nil
        default: return nil
        }
    }

    static func cycleDuration(phase: String, speed: Double) -> Double {
        phase == "loop" ? 1.2 / normalizedSpeed(speed) : edgeDuration(speed: speed) + readableHold + resetGap
    }

    static func displaySample(phase: String, effect: String, speed: Double, clockTime: Double,
                              hasEverPlayed: Bool) -> NativeTextAnimationPreviewSample {
        hasEverPlayed ? sample(phase: phase, effect: effect, speed: speed, clockTime: clockTime) : stableSample
    }

    static func sample(phase: String, effect: String, speed: Double, clockTime: Double) -> NativeTextAnimationPreviewSample {
        let speed = normalizedSpeed(speed)
        guard effect != "none" else { return stableSample }
        let duration = cycleDuration(phase: phase, speed: speed)
        let cycleTime = clockTime.truncatingRemainder(dividingBy: duration)

        switch phase {
        case "entrance", "exit":
            guard let localTime = layerTime(phase: phase, speed: speed, clockTime: clockTime) else { return hidden }
            return fromEngine(phase: phase, effect: effect, speed: speed, localTime: localTime, duration: layerDuration(speed: speed))
        case "loop":
            return fromEngine(phase: phase, effect: effect, speed: speed, localTime: cycleTime,
                              duration: max(cycleTime + 1, 2))
        default:
            return stableSample
        }
    }

    private static func fromEngine(phase: String, effect: String, speed: Double, localTime: Double, duration: Double) -> NativeTextAnimationPreviewSample {
        let phases = TextAnimationPhases(entrance: edge(phase == "entrance" ? effect : "none"),
                                         exit: edge(phase == "exit" ? effect : "none"),
                                         loop: loop(phase == "loop" ? effect : "none"), speed: speed)
        guard let value = try? phases.sample(time: localTime, duration: duration) else { return stableSample }
        return NativeTextAnimationPreviewSample(alpha: value.alpha, scale: value.scale,
            xTranslate: value.xTranslate, yTranslate: value.yTranslate,
            visibleText: TextAnimationPhases.visiblePrefix(previewText, progress: value.revealProgress))
    }

    static func normalizedSpeed(_ speed: Double) -> Double { min(3, max(0.25, speed.isFinite ? speed : 1)) }
    private static func edge(_ value: String) -> TextAnimationPhases.Edge { TextAnimationPhases.Edge(rawValue: value) ?? .none }
    private static func loop(_ value: String) -> TextAnimationPhases.Loop { TextAnimationPhases.Loop(rawValue: value) ?? .none }
    static let stableSample = NativeTextAnimationPreviewSample(alpha: 1, scale: 1, xTranslate: 0, yTranslate: 0, visibleText: previewText)
    private static let hidden = NativeTextAnimationPreviewSample(alpha: 0, scale: 1, xTranslate: 0, yTranslate: 0, visibleText: "")
}

enum NativeTextAnimationPreviewAutoplay {
    static func allows(reduceMotion: Bool, environment: [String: String]) -> Bool {
        !reduceMotion && environment["UI_TEST_REDUCE_MOTION"] != "1"
    }
}
