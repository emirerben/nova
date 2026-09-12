import Foundation

/// Paired with app/pipeline/visual_editor.py; values are sampled in local seconds.
public struct VisualEditorStyle: Codable, Equatable, Sendable {
    public struct Animation: Codable, Equatable, Sendable {
        public enum Edge: String, Codable, Sendable { case none, fade, pop, slide, zoom }
        public enum Loop: String, Codable, Sendable { case none, pulse, bounce, float }
        public var version: Int = 1
        public var entrance: Edge = .none
        public var exit: Edge = .none
        public var loop: Loop = .none
        public var speed: Double = 1
        public init(entrance: Edge = .none, exit: Edge = .none, loop: Loop = .none, speed: Double = 1) {
            self.entrance = entrance; self.exit = exit; self.loop = loop; self.speed = speed
        }
    }
    public var version: Int = 1
    public var rotationDegrees: Double = 0
    public var fitMode: String = "cover"
    public var zoom: Double = 1
    public var animation: Animation = .init()
    enum CodingKeys: String, CodingKey { case version, rotationDegrees = "rotation_deg", fitMode = "fit_mode", zoom, animation }
    public init(rotationDegrees: Double = 0, fitMode: String = "cover", zoom: Double = 1, animation: Animation = .init()) {
        self.rotationDegrees = rotationDegrees; self.fitMode = fitMode; self.zoom = zoom; self.animation = animation
    }
    public func validate() throws {
        guard version == 1, animation.version == 1, rotationDegrees.isFinite, (-360...360).contains(rotationDegrees),
              ["contain", "cover"].contains(fitMode), zoom.isFinite, (1...4).contains(zoom),
              animation.speed.isFinite, (0.25...3).contains(animation.speed) else { throw RecipeError.invalidTimeline }
    }
    public func sample(time: Double, duration: Double) throws -> TextTransformSample {
        try validate()
        guard time.isFinite, duration.isFinite, duration > 0 else { throw RecipeError.invalidTimeline }
        guard time >= 0, time < duration else { return .init(alpha: 0, scale: 1, xTranslate: 0, yTranslate: 0, revealProgress: 1) }
        let edge = min(0.4 / animation.speed, duration / 2)
        var alpha = 1.0, scale = 1.0, x = 0.0, y = 0.0
        for (effect, progress) in [(animation.entrance, min(1, time / edge)), (animation.exit, min(1, (duration - time) / edge))] {
            let eased = 1 - pow(1 - progress, 3)
            if effect != .none { alpha *= eased }
            switch effect {
            case .pop: scale *= 0.7 + 0.3 * eased
            case .zoom: scale *= 1.2 - 0.2 * eased
            case .slide: x += 40 * (1 - eased)
            default: break
            }
        }
        let wave = sin(2 * Double.pi * time * animation.speed / 1.2)
        switch animation.loop {
        case .none: break
        case .pulse: scale *= 1 + 0.04 * wave
        case .bounce: y -= 12 * abs(wave)
        case .float: y -= 8 * wave
        }
        return .init(alpha: alpha, scale: scale, xTranslate: x, yTranslate: y, revealProgress: 1)
    }
}
