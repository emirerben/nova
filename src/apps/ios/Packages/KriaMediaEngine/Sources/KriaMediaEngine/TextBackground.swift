import Foundation

public struct TextBackground: Codable, Equatable, Sendable {
    public let color: TextInk
    public let left: Double
    public let top: Double
    public let width: Double
    public let height: Double
    public let radius: Double
    public init(color: TextInk, left: Double, top: Double, width: Double, height: Double, radius: Double) {
        self.color = color; self.left = left; self.top = top
        self.width = width; self.height = height; self.radius = radius
    }
    private enum CodingKeys: String, CodingKey { case color, left, top, width, height, radius }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["color", "left", "top", "width", "height", "radius"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        color = try c.decode(TextInk.self, forKey: .color)
        left = try c.decode(Double.self, forKey: .left); top = try c.decode(Double.self, forKey: .top)
        width = try c.decode(Double.self, forKey: .width); height = try c.decode(Double.self, forKey: .height)
        radius = try c.decode(Double.self, forKey: .radius)
        try validate()
    }
    func validate() throws {
        try color.validate()
        guard [left, top, width, height, radius].allSatisfy(\.isFinite),
              abs(left) <= 10000, abs(top) <= 10000, width > 0, width <= 20000,
              height > 0, height <= 20000, (0...1000).contains(radius) else {
            throw RecipeError.invalidTimeline
        }
    }
}
