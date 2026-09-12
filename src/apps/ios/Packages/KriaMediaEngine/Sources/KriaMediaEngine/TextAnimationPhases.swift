import Foundation

/// Deterministic output-clock sampling shared by native preview and export.
public extension TextAnimationPhases {
    func sample(time: Double, duration: Double) throws -> TextTransformSample {
        try validate()
        guard time.isFinite, duration.isFinite, duration > 0 else { throw RecipeError.invalidTimeline }
        guard time >= 0, time < duration else {
            return TextTransformSample(alpha: 0, scale: 1, xTranslate: 0, yTranslate: 0, revealProgress: 0)
        }
        let edgeDuration = min(0.4 / speed, duration / 2)
        var alpha = 1.0, scale = 1.0, x = 0.0, y = 0.0, reveal = 1.0
        for (effect, progress) in [(entrance, min(1, time / edgeDuration)), (exit, min(1, (duration - time) / edgeDuration))] {
            let eased = 1 - pow(1 - progress, 3)
            switch effect {
            case .none: break
            case .fade: alpha *= eased
            case .pop: scale *= 0.7 + 0.3 * eased; alpha *= eased
            case .slide: x += 40 * (1 - eased); alpha *= eased
            case .typewriter: reveal = min(reveal, progress)
            }
        }
        let wave = sin(2 * Double.pi * time * speed / 1.2)
        switch loop {
        case .none: break
        case .pulse: scale *= 1 + 0.04 * wave
        case .bounce: y -= 12 * abs(wave)
        case .float: y -= 8 * wave
        }
        return TextTransformSample(alpha: alpha, scale: scale, xTranslate: x, yTranslate: y, revealProgress: reveal)
    }
    static func visiblePrefix(_ text: String, progress: Double) -> String {
        guard progress.isFinite else { return "" }
        return String(text.prefix(min(text.count, max(0, Int(floor(Double(text.count) * progress + 1e-9))))))
    }
}
