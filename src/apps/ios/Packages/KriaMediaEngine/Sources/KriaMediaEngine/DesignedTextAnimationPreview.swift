#if canImport(AVFoundation)
import Foundation
import CoreImage

public final class DesignedTextAnimationPreview: @unchecked Sendable {
    private let text: RecipeTextLayer
    private let phases: TextAnimationPhases
    private let context = CIContext(options: [.cacheIntermediates: false])
    private let frame = CGRect(x: 0, y: 0, width: 180, height: 96)

    public init(fontURL: URL, effect: TextAnimationPhases.Edge, exit: Bool = false, speed: Double = 1, duration: Double = 3) throws {
        phases = TextAnimationPhases(entrance: exit ? .none : effect, exit: exit ? effect : .none, speed: speed)
        let style = AuthoredTextLayout.Style(fontAssetID: "font", size: 27,
            color: TextInk(red: 0.25, green: 0.31, blue: 0.19, alpha: 1))
        let layer = try AuthoredTextLayout.compile(id: "preview", text: effect == .wordRelay ? "Your Text" : "Text", start: 0, end: duration,
            style: style, fontURL: fontURL, canvas: Canvas(width: 180, height: 96), phases: phases)
        text = try RecipeTextLayer.make(layer, assetURLs: ["font": fontURL], canvas: frame.size)
    }

    public func image(time: Double) throws -> CGImage? {
        let background = CIImage(color: .clear).cropped(to: frame)
        let result = try RecipeVideoCompositor.paintText([text], over: background, canvas: frame, time: time)
        return context.createCGImage(result, from: frame)
    }
}
#endif
