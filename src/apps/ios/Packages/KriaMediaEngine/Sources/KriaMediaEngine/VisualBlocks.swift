import Foundation

public struct VisualMediaPlacement: Codable, Equatable, Sendable {
    public enum Motion: String, Codable, Sendable { case none, zoomIn = "zoom_in", zoomOut = "zoom_out", panLeft = "pan_left", panRight = "pan_right" }
    public var order: Int
    public var editorStyle: VisualEditorStyle?
    public var contain: Bool
    public var focalX: Double
    public var focalY: Double
    public var zoom: Double
    public var preCrop: Bool
    public var motion: Motion
    public var widthFraction: Double?
    public var xFraction: Double
    public var yFraction: Double
    public var windowStart: Double
    public var windowEnd: Double
    public var fadeIn: Bool
    public var fadeOut: Bool
    public init(order: Int, contain: Bool = false, focalX: Double = 0.5, focalY: Double = 0.5,
                zoom: Double = 1, preCrop: Bool = false, motion: Motion = .none,
                widthFraction: Double? = nil, xFraction: Double = 0.5, yFraction: Double = 0.5,
                windowStart: Double, windowEnd: Double, fadeIn: Bool = false, fadeOut: Bool = false, editorStyle: VisualEditorStyle? = nil) {
        self.editorStyle = editorStyle
        self.order = order; self.contain = contain; self.focalX = focalX; self.focalY = focalY
        self.zoom = zoom; self.preCrop = preCrop; self.motion = motion; self.widthFraction = widthFraction
        self.xFraction = xFraction; self.yFraction = yFraction; self.windowStart = windowStart; self.windowEnd = windowEnd
        self.fadeIn = fadeIn; self.fadeOut = fadeOut
    }
    func validate() throws {
        try editorStyle?.validate()
        guard (0...1000).contains(order), [focalX, focalY, xFraction, yFraction].allSatisfy({ $0.isFinite && (0...1).contains($0) }),
              zoom.isFinite, (1...4).contains(zoom), widthFraction.map({ $0.isFinite && (0.05...1).contains($0) }) ?? true,
              windowStart.isFinite, windowEnd.isFinite, windowStart >= 0, windowEnd > windowStart, windowEnd <= 1800 else { throw RecipeError.invalidTimeline }
    }
    func alpha(at time: Double) -> Double {
        let authoredAlpha = editorStyle.map { (try? $0.sample(time: time - windowStart, duration: windowEnd - windowStart).alpha) ?? 0 } ?? 1
        guard windowEnd - windowStart > 0.3 else { return authoredAlpha }
        return authoredAlpha * min(fadeIn ? min(1, max(0, (time - windowStart) / 0.15)) : 1,
                                   fadeOut ? min(1, max(0, (windowEnd - time) / 0.15)) : 1)
    }
}

public struct VisualCanvasFill: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable { case solid, gradient, blurPrevious = "blur_previous" }
    public var id: String
    public var start: Double
    public var end: Double
    public var order: Int
    public var kind: Kind
    public var color: TextInk
    public var endColor: TextInk?
    public var angle: Double
    public var blurRadius: Double
    public var fadeIn: Bool
    public var fadeOut: Bool
    public init(id: String, start: Double, end: Double, order: Int, kind: Kind, color: TextInk,
                endColor: TextInk? = nil, angle: Double = 180, blurRadius: Double = 24, fadeIn: Bool = false, fadeOut: Bool = false) {
        self.id = id; self.start = start; self.end = end; self.order = order; self.kind = kind
        self.color = color; self.endColor = endColor; self.angle = angle; self.blurRadius = blurRadius
        self.fadeIn = fadeIn; self.fadeOut = fadeOut
    }
    func validate(duration: Double) throws {
        guard !id.isEmpty, id.count <= 160, start.isFinite, end.isFinite, start >= 0, end > start, end <= duration,
              (0...1000).contains(order), angle.isFinite, (0...360).contains(angle),
              blurRadius.isFinite, (1...80).contains(blurRadius), kind != .gradient || endColor != nil else { throw RecipeError.invalidTimeline }
        try color.validate(); try endColor?.validate()
    }
}

public struct AudioMuteWindow: Codable, Equatable, Sendable {
    public var start: Double
    public var end: Double
    public var clipIDs: [String]
    private enum CodingKeys: String, CodingKey { case start, end, clipIDs = "clipIds" }
    public init(start: Double, end: Double, clipIDs: [String]) { self.start = start; self.end = end; self.clipIDs = clipIDs }
}

#if canImport(AVFoundation)
import AVFoundation
import CoreImage

extension VisualMediaPlacement {
    func position(_ source: CIImage, preferred: CGAffineTransform, canvas: CGRect, time: Double, clipStart: Double, clipEnd: Double) -> CIImage {
        var image = basePosition(source, preferred: preferred, canvas: canvas, time: time, clipStart: clipStart, clipEnd: clipEnd)
        guard let editorStyle, let sample = try? editorStyle.sample(time: time - windowStart, duration: windowEnd - windowStart) else { return image }
        if widthFraction == nil && !contain { image = image.cropped(to: canvas) }
        let center = CGPoint(x: image.extent.midX, y: image.extent.midY)
        return image.transformed(by: CGAffineTransform(translationX: -center.x, y: -center.y)
            .concatenating(CGAffineTransform(scaleX: sample.scale, y: sample.scale))
            .concatenating(CGAffineTransform(rotationAngle: -editorStyle.rotationDegrees * .pi / 180))
            .concatenating(CGAffineTransform(translationX: center.x + sample.xTranslate * canvas.width / 1080,
                                          y: center.y - sample.yTranslate * canvas.height / 1920)))
    }
    private func basePosition(_ source: CIImage, preferred: CGAffineTransform, canvas: CGRect, time: Double, clipStart: Double, clipEnd: Double) -> CIImage {
        var image = source.transformed(by: preferred)
        image = image.transformed(by: CGAffineTransform(translationX: -image.extent.minX, y: -image.extent.minY))
        let size = image.extent.size
        if let widthFraction {
            let width = (canvas.width * widthFraction * (editorStyle?.zoom ?? 1)).rounded(.toNearestOrEven)
            let scale = width / size.width
            return image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
                .transformed(by: CGAffineTransform(translationX: canvas.width * xFraction - width / 2,
                    y: canvas.height * (1 - yFraction) - size.height * scale / 2))
        }
        if preCrop {
            let cover = max(canvas.width / size.width, canvas.height / size.height)
            image = image.transformed(by: CGAffineTransform(scaleX: cover, y: cover))
                .transformed(by: CGAffineTransform(translationX: (canvas.width - size.width * cover) / 2, y: (canvas.height - size.height * cover) / 2))
                .cropped(to: canvas)
        }
        let progress = min(1, max(0, floor((time - clipStart) * 30 + 0.000_001) / max(1, ((clipEnd - clipStart) * 30).rounded() - 1)))
        let amount = zoom * (motion == .zoomIn ? 1 + 0.08 * progress : motion == .zoomOut ? 1.08 - 0.08 * progress : 1)
        let x = min(1, max(0, focalX + (motion == .panRight ? 0.08 : motion == .panLeft ? -0.08 : 0) * progress))
        let base = contain ? min(canvas.width / image.extent.width, canvas.height / image.extent.height) : max(canvas.width / image.extent.width, canvas.height / image.extent.height)
        let scale = base * amount
        return image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            .transformed(by: CGAffineTransform(translationX: (canvas.width - image.extent.width * scale) * x,
                y: (canvas.height - image.extent.height * scale) * (1 - focalY)))
    }
}

extension VisualCanvasFill {
    func image(canvas: CGSize, previous: CGImage? = nil) throws -> CIImage {
        let rectangle = CGRect(origin: .zero, size: canvas)
        if kind == .solid { return CIImage(color: CIColor(cgColor: color.cgColor)).cropped(to: rectangle) }
        if kind == .blurPrevious {
            guard let previous else { throw MediaEngineError.missingAsset(id) }
            let context = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
            var image = CIImage(cgImage: previous)
            for _ in 0..<2 { image = image.clampedToExtent().applyingFilter("CIBoxBlur", parameters: ["inputRadius": blurRadius]).cropped(to: rectangle) }
            guard let blurred = context.createCGImage(image, from: rectangle) else { throw MediaEngineError.exportFailed }
            return CIImage(cgImage: blurred)
        }
        guard let endColor else { throw RecipeError.invalidTimeline }
        let dx = cos(angle * .pi / 180), dy = sin(angle * .pi / 180)
        let corners = [0, dx * canvas.width, dy * canvas.height, dx * canvas.width + dy * canvas.height]
        let low = corners.min()!, high = corners.max()!, span = max(1, high - low)
        var pixels = [UInt8](repeating: 255, count: Int(canvas.width * canvas.height) * 4)
        let from = [color.red, color.green, color.blue], to = [endColor.red, endColor.green, endColor.blue]
        for y in 0..<Int(canvas.height) { for x in 0..<Int(canvas.width) {
            let t = min(1, max(0, (dx * Double(x) + dy * Double(y) - low) / span))
            for channel in 0..<3 { pixels[(y * Int(canvas.width) + x) * 4 + channel] = UInt8(clamping: Int((255 * (from[channel] + (to[channel] - from[channel]) * t)).rounded(.toNearestOrEven))) }
        } }
        guard let provider = CGDataProvider(data: Data(pixels) as CFData),
              let image = CGImage(width: Int(canvas.width), height: Int(canvas.height), bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: Int(canvas.width) * 4,
                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue), provider: provider,
                decode: nil, shouldInterpolate: false, intent: .defaultIntent) else { throw MediaEngineError.exportFailed }
        return CIImage(cgImage: image)
    }
}

func applyAudioGain(_ parameter: AVMutableAudioMixInputParameters, clip: TimelineClip, gain: Double, windows: [AudioMuteWindow]) {
    let affected = windows.filter { $0.clipIDs.contains(clip.id) && $0.start < clip.timelineStart + clip.duration && $0.end > clip.timelineStart }
    let times = Set([clip.timelineStart] + affected.flatMap { [max(clip.timelineStart, $0.start), min(clip.timelineStart + clip.duration, $0.end)] }).sorted()
    for time in times {
        let muted = affected.contains { $0.start <= time && $0.end > time }
        parameter.setVolume(muted ? 0 : Float(gain * clip.volume), at: CMTime(seconds: time, preferredTimescale: 60_000))
    }
}
#endif
