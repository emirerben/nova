#if canImport(AVFoundation) && canImport(JavaScriptCore)
import CoreGraphics
import CoreImage
import CoreText
import Foundation
import ImageIO
import JavaScriptCore

/// Executes the bundled production motion functions into native drawing commands.
/// The JavaScript context has no network, file, browser, or wall-clock bridge.
final class NativeMotionPainter: @unchecked Sendable {
    private let lock = NSLock()
    private let runtime: JSContext
    private let instances: Any
    private let font: CGFont
    private let imageURLs: [String: URL]
    private var images: [String: CGImage] = [:]
    private var imageOrder: [String] = []
    private let imageWindows: [(Range<Int>, [String])]
    private let canvas: CGSize
    private let frameRate: Double
    private let activeFrames: [Range<Int>]
    private var fontCache: [Double: CTFont] = [:]

    init(instancesJSON: Data, runtimeHash: String, duration: Double, fontURL: URL,
         imageURLs: [String: URL], canvas: CGSize, frameRate: Double = 30) throws {
        guard instancesJSON.count <= 1_000_000,
              let provider = CGDataProvider(url: fontURL as CFURL), let font = CGFont(provider),
              let runtime = JSContext(),
              let resource = Bundle.module.url(forResource: "NativeMotionRuntime.generated", withExtension: "js") else {
            throw NativePreviewFeatureError("NativeMotionPainter-31")
        }
        self.font = font; self.runtime = runtime; self.canvas = canvas; self.frameRate = frameRate
        instances = try JSONSerialization.jsonObject(with: instancesJSON)
        activeFrames = (instances as? [[String: Any]] ?? []).compactMap { scene in
            guard let start = scene["start_frame"] as? NSNumber, let end = scene["end_frame_exclusive"] as? NSNumber,
                  start.intValue >= 0, end.intValue > start.intValue else { return nil }
            return start.intValue..<end.intValue
        }
        self.imageURLs = imageURLs
        imageWindows = (instances as? [[String: Any]] ?? []).compactMap { scene in
            guard let start = scene["start_frame"] as? NSNumber, let end = scene["end_frame_exclusive"] as? NSNumber,
                  start.intValue >= 0, end.intValue > start.intValue else { return nil }
            let params = scene["params"] as? [String: Any]
            let refs = params?["assets"] as? [[String: Any]] ?? []
            return (start.intValue..<end.intValue, refs.compactMap { $0["asset_id"] as? String })
        }
        let glyphs: @convention(block) (String) -> [NSNumber] = { text in
            let face = CTFontCreateWithGraphicsFont(font, 100, nil, nil)
            return text.unicodeScalars.map { scalar in
                let characters = Array(String(scalar).utf16)
                var glyphs = [CGGlyph](repeating: 0, count: characters.count)
                CTFontGetGlyphsForCharacters(face, characters, &glyphs, characters.count)
                return NSNumber(value: glyphs.first(where: { $0 != 0 }) ?? 0)
            }
        }
        let widths: @convention(block) (Double, [NSNumber]) -> [NSNumber] = { size, values in
            let face = CTFontCreateWithGraphicsFont(font, size, nil, nil)
            let ids = values.map { CGGlyph(clamping: $0.intValue) }
            var advances = [CGSize](repeating: .zero, count: ids.count)
            CTFontGetAdvancesForGlyphs(face, .horizontal, ids, &advances, ids.count)
            return advances.map { NSNumber(value: $0.width) }
        }
        runtime.setObject(glyphs, forKeyedSubscript: "nativeMotionGlyphs" as NSString)
        runtime.setObject(widths, forKeyedSubscript: "nativeMotionWidths" as NSString)
        runtime.evaluateScript(try String(contentsOf: resource, encoding: .utf8))
        guard runtime.exception == nil,
              let exports = runtime.objectForKeyedSubscript("NativeMotionRuntime"),
              exports.forProperty("validateNativeMotionInstances")?.call(withArguments: [instances, Int(ceil(duration * frameRate))])?.forProperty("ok")?.toBool() == true else {
            throw NativePreviewFeatureError("NativeMotionPainter-70")
        }
        let routeOnly = (instances as? [[String: Any]])?.allSatisfy { $0["preset_id"] as? String == "route_trace" } ?? false
        guard exports.forProperty("isCompatiblePersistedMotionRuntimeHash")?.call(withArguments: [runtimeHash, routeOnly])?.toBool() == true else {
            throw NativePreviewFeatureError("NativeMotionPainter-74")
        }
    }

    static func make(_ program: MotionSceneProgram?, assets: [String: URL], canvas: CGSize, duration: Double, frameRate: Double) throws -> NativeMotionPainter? {
        guard let program else { return nil }
        guard let font = assets[program.fontAssetID] else { throw MediaEngineError.missingAsset(program.fontAssetID) }
        var images: [String: URL] = [:]
        for (id, assetID) in program.imageAssetIDs {
            guard let url = assets[assetID] else { throw MediaEngineError.missingAsset(assetID) }
            images[id] = url
        }
        return try NativeMotionPainter(instancesJSON: JSONEncoder().encode(program.instances), runtimeHash: program.runtimeHash,
            duration: duration, fontURL: font, imageURLs: images, canvas: canvas, frameRate: frameRate)
    }

    func image(at time: Double) throws -> CIImage {
        lock.lock(); defer { lock.unlock() }
        runtime.exception = nil
        let frame = Int(floor(max(0, time) * frameRate + 0.000_001))
        guard activeFrames.contains(where: { $0.contains(frame) }) else {
            return CIImage(color: .clear).cropped(to: CGRect(origin: .zero, size: canvas))
        }
        let activeImages = Set(imageWindows.filter { $0.0.contains(frame) }.flatMap(\.1))
        for id in activeImages { try loadImage(id, retaining: activeImages) }
        let sizes = images.mapValues { [$0.width, $0.height] }
        guard let commands = runtime.objectForKeyedSubscript("NativeMotionRuntime")?
            .forProperty("nativeMotionFrame")?.call(withArguments: [instances, frame, canvas.width, canvas.height, sizes])?.toArray() as? [[Any]],
              runtime.exception == nil, commands.count <= 20_000,
              let context = CGContext(data: nil, width: Int(canvas.width), height: Int(canvas.height), bitsPerComponent: 8,
                bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw MediaEngineError.exportFailed
        }
        context.translateBy(x: 0, y: canvas.height); context.scaleBy(x: 1, y: -1)
        for command in commands { try draw(command, in: context) }
        guard let result = context.makeImage() else { throw MediaEngineError.exportFailed }
        return CIImage(cgImage: result)
    }

    private func loadImage(_ id: String, retaining active: Set<String>) throws {
        if images[id] != nil { return }
        guard let url = imageURLs[id], let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              CGImageSourceGetCount(source) == 1,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
              let height = properties[kCGImagePropertyPixelHeight] as? NSNumber,
              width.intValue > 0, height.intValue > 0, width.intValue * height.intValue <= 25_000_000,
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 2048,
              ] as CFDictionary) else { throw MediaEngineError.missingAsset(id) }
        let bytes = image.bytesPerRow * image.height
        let limit = 128 * 1024 * 1024
        var used = images.values.reduce(0) { $0 + $1.bytesPerRow * $1.height }
        while used + bytes > limit, let victim = imageOrder.first(where: { !active.contains($0) }) {
            if let removed = images.removeValue(forKey: victim) { used -= removed.bytesPerRow * removed.height }
            imageOrder.removeAll { $0 == victim }
        }
        guard used + bytes <= limit else { throw NativePreviewFeatureError("NativeMotionPainter-133") }
        images[id] = image; imageOrder.append(id)
    }

    private func draw(_ command: [Any], in context: CGContext) throws {
        guard let kind = command.first as? String else { throw RecipeError.invalidTimeline }
        func numbers(_ value: Any) throws -> [Double] {
            guard let values = value as? [NSNumber], values.allSatisfy({ $0.doubleValue.isFinite }) else { throw RecipeError.invalidTimeline }
            return values.map(\.doubleValue)
        }
        func rectangle(_ value: Any) throws -> CGRect {
            let values = try numbers(value)
            guard values.count >= 4 else { throw RecipeError.invalidTimeline }
            return CGRect(x: values[0], y: values[1], width: values[2], height: values[3])
        }
        func number(_ index: Int) throws -> Double {
            guard command.indices.contains(index), let value = command[index] as? NSNumber, value.doubleValue.isFinite else { throw RecipeError.invalidTimeline }
            return value.doubleValue
        }
        func paint(_ value: Any) throws -> CGPathDrawingMode {
            guard let values = value as? [Any], values.count == 5,
                  let style = values[0] as? String, let width = values[1] as? NSNumber,
                  let phase = values[4] as? NSNumber else { throw RecipeError.invalidTimeline }
            let rgba = try numbers(values[2]), dash = try numbers(values[3])
            guard rgba.count == 4 else { throw RecipeError.invalidTimeline }
            let color = CGColor(red: rgba[0], green: rgba[1], blue: rgba[2], alpha: rgba[3])
            context.setFillColor(color); context.setStrokeColor(color)
            context.setLineWidth(width.doubleValue); context.setLineCap(.round); context.setLineJoin(.round)
            context.setLineDash(phase: phase.doubleValue, lengths: dash.map { CGFloat($0) })
            return style == "stroke" ? .stroke : .fill
        }
        switch kind {
        case "save": context.saveGState()
        case "restore": context.restoreGState()
        case "translate": context.translateBy(x: try number(1), y: try number(2))
        case "scale": context.scaleBy(x: try number(1), y: try number(2))
        case "rotate":
            let x = try number(2), y = try number(3)
            context.translateBy(x: x, y: y); context.rotate(by: try number(1) * .pi / 180); context.translateBy(x: -x, y: -y)
        case "skew": context.concatenate(CGAffineTransform(a: 1, b: try number(2), c: try number(1), d: 1, tx: 0, ty: 0))
        case "clip": context.clip(to: try rectangle(command[1]))
        case "rect", "roundRect", "circle":
            let mode = try paint(command[2])
            let values = try numbers(command[1])
            if kind == "circle" {
                context.addEllipse(in: CGRect(x: values[0] - values[2], y: values[1] - values[2], width: values[2] * 2, height: values[2] * 2))
            } else if kind == "roundRect" {
                context.addPath(CGPath(roundedRect: try rectangle(command[1]), cornerWidth: values[4], cornerHeight: values[5], transform: nil))
            } else { context.addRect(try rectangle(command[1])) }
            context.drawPath(using: mode)
        case "path":
            let mode = try paint(command[2])
            guard let segments = command[1] as? [[Any]] else { throw RecipeError.invalidTimeline }
            let path = CGMutablePath()
            for segment in segments {
                switch segment.first as? String {
                case "move":
                    let v = try numbers(Array(segment.dropFirst())); path.move(to: CGPoint(x: v[0], y: v[1]))
                case "cubic":
                    let v = try numbers(Array(segment.dropFirst())); path.addCurve(to: CGPoint(x: v[4], y: v[5]), control1: CGPoint(x: v[0], y: v[1]), control2: CGPoint(x: v[2], y: v[3]))
                case "close": path.closeSubpath()
                case "svg":
                    guard let source = segment[1] as? String else { throw RecipeError.invalidTimeline }
                    try appendTrustedSVG(source, to: path)
                default: throw RecipeError.invalidTimeline
                }
            }
            context.addPath(path); context.drawPath(using: mode)
        case "text":
            guard let text = command[1] as? String else { throw RecipeError.invalidTimeline }
            let x = try number(2), y = try number(3), size = try number(4)
            let mode = try paint(command[5])
            let face = fontCache[size] ?? CTFontCreateWithGraphicsFont(font, size, nil, nil)
            if fontCache.count >= 128 { fontCache.removeAll(keepingCapacity: true) }
            fontCache[size] = face
            var cursor = x
            let path = CGMutablePath()
            for scalar in text.unicodeScalars {
                let chars = Array(String(scalar).utf16)
                var ids = [CGGlyph](repeating: 0, count: chars.count)
                CTFontGetGlyphsForCharacters(face, chars, &ids, chars.count)
                let glyph = ids.first(where: { $0 != 0 }) ?? 0
                var id = glyph, advance = CGSize.zero
                CTFontGetAdvancesForGlyphs(face, .horizontal, &id, &advance, 1)
                var transform = CGAffineTransform(a: 1, b: 0, c: 0, d: -1, tx: cursor, ty: y)
                if let glyphPath = CTFontCreatePathForGlyph(face, glyph, &transform) { path.addPath(glyphPath) }
                cursor += advance.width
            }
            context.addPath(path); context.drawPath(using: mode)
        case "image":
            guard let id = command[1] as? String, let image = images[id],
                  let crop = image.cropping(to: try rectangle(command[2])) else { throw MediaEngineError.exportFailed }
            let dest = try rectangle(command[3])
            context.saveGState()
            if !(command[4] is NSNull) {
                _ = try paint(command[4])
                if let p = command[4] as? [Any], let color = p[2] as? [NSNumber] { context.setAlpha(color[3].doubleValue) }
            }
            context.translateBy(x: dest.minX, y: dest.maxY); context.scaleBy(x: 1, y: -1)
            context.draw(crop, in: CGRect(origin: .zero, size: dest.size)); context.restoreGState()
        default: throw RecipeError.invalidTimeline
        }
    }

    private func appendTrustedSVG(_ source: String, to path: CGMutablePath) throws {
        let values = source.split(separator: " ").map(String.init)
        var index = 0
        while index < values.count {
            let verb = values[index]; index += 1
            let count = verb == "M" ? 2 : verb == "C" ? 6 : 0
            guard count > 0, index + count <= values.count else { throw RecipeError.invalidTimeline }
            let numbers = try values[index..<(index + count)].map { value -> Double in
                guard let number = Double(value), number.isFinite else { throw RecipeError.invalidTimeline }
                return number
            }
            if verb == "M" { path.move(to: CGPoint(x: numbers[0], y: numbers[1])) }
            else { path.addCurve(to: CGPoint(x: numbers[4], y: numbers[5]), control1: CGPoint(x: numbers[0], y: numbers[1]), control2: CGPoint(x: numbers[2], y: numbers[3])) }
            index += count
        }
    }
}
#endif
