#if canImport(AVFoundation)
import Foundation
import CoreImage
import CoreText

final class NativeDesignedTextPainter: @unchecked Sendable {
    struct Sprite { let image: CIImage; let bounds: CGRect; let range: NSRange; let runIndex: Int; let text: String }
    struct Piece {
        let image: CIImage
        let bounds: CGRect
        let size: Double
        let words: [Sprite]
        let glyphs: [Sprite]
        let fill: TextInk
    }
    let layer: PortableTextLayer
    let bounds: CGRect
    let pieces: [Piece]
    let bitmapBytes: Int
    private let vector: PortableTextVectorPainter
    private let bitmapLimit: Int
    private let anchor: CGPoint
    private let rotation: CGAffineTransform
    private let background: CIImage?
    private let transparent: CIImage

    init(layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws {
        self.layer = layer
        bitmapLimit = maxBitmapBytes
        let base = try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas)
        let size = layer.runs.map(\.fontSize).max() ?? 1
        bounds = base.bounds.insetBy(dx: -size * 0.3, dy: -size * 2.5).intersection(CGRect(origin: .zero, size: canvas)).integral
        let pixelBytes = Int(bounds.width * bounds.height * 4)
        guard pixelBytes <= maxBitmapBytes / 3 else { throw MediaEngineError.unsupportedCapability }
        transparent = CIImage(color: .clear).cropped(to: CGRect(origin: .zero, size: bounds.size))
        anchor = CGPoint(x: base.anchor.x - bounds.minX, y: base.anchor.y - bounds.minY)
        rotation = base.rotation.concatenating(CGAffineTransform(translationX: -bounds.minX, y: -bounds.minY))
        if let box = layer.background {
            background = CIImage(color: CIColor(cgColor: box.color.cgColor)).cropped(to: CGRect(x: box.left,
                y: canvas.height - box.top - box.height, width: box.width, height: box.height))
        } else { background = nil }
        var pieces: [Piece] = []
        var bytes = pixelBytes * 3
        let plain = PortableTextLayer(id: layer.id, start: layer.start, end: layer.end,
            anchorX: layer.anchorX, anchorY: layer.anchorY, rotationDegrees: 0, runs: layer.runs, bend: layer.bend)
        let painter = try PortableTextVectorPainter(layer: plain, assetURLs: assetURLs, canvas: canvas, preserveOffscreenInk: true)
        vector = painter
        for (index, run) in layer.runs.enumerated() {
            let string = run.text as NSString
            let b = painter.segmentBounds(runIndex: index, ranges: [NSRange(location: 0, length: string.length)])[0]
            if b.isNull || b.isEmpty { continue }
            let image = try painter.image(bounds: b, maxBitmapBytes: maxBitmapBytes - bytes, runIndices: [index], includeBackground: false)
                .transformed(by: CGAffineTransform(translationX: b.minX, y: b.minY))
            bytes += Int(b.width * b.height * 4)
            guard bytes <= maxBitmapBytes else { throw MediaEngineError.unsupportedCapability }
            func sprites(_ ranges: [NSRange], needed: Bool) throws -> [Sprite] {
                guard needed else { return [] }
                let boxes = painter.segmentBounds(runIndex: index, ranges: ranges)
                return try zip(ranges, boxes).compactMap { range, box in
                    guard !box.isNull, !box.isEmpty else { return nil }
                    let bitmap = try painter.image(bounds: box, maxBitmapBytes: maxBitmapBytes - bytes,
                        runIndices: [index], includeBackground: false, runRanges: [index: range])
                    bytes += Int(box.width * box.height * 4)
                    return Sprite(image: bitmap.transformed(by: CGAffineTransform(translationX: box.minX, y: box.minY)), bounds: box,
                        range: range, runIndex: index, text: string.substring(with: range))
                }
            }
            let matches = try NSRegularExpression(pattern: "\\S+").matches(in: run.text, range: NSRange(location: 0, length: string.length))
            let edges = [layer.animationPhases?.entrance, layer.animationPhases?.exit]
            let words = try sprites(matches.map(\.range), needed: edges.contains(.wordRelay))
            var ranges: [NSRange] = []
            string.enumerateSubstrings(in: NSRange(location: 0, length: string.length), options: .byComposedCharacterSequences) { _, range, _, _ in ranges.append(range) }
            let glyphs = try sprites(ranges, needed: edges.contains(.elasticCondensed))
            pieces.append(Piece(image: image, bounds: b, size: run.fontSize, words: words, glyphs: glyphs, fill: run.fill))
        }
        self.pieces = pieces
        bitmapBytes = bytes
    }

    static func ease(_ value: Double) -> Double {
        let x = min(1, max(0, value))
        if x == 0 || x == 1 { return x }
        var lo = 0.0, hi = 1.0
        for _ in 0..<16 {
            let t = (lo + hi) / 2
            let bx = 3 * pow(1 - t, 2) * t * 0.23 + 3 * (1 - t) * t * t * 0.32 + t * t * t
            if bx < x { lo = t } else { hi = t }
        }
        let t = (lo + hi) / 2
        return 3 * pow(1 - t, 2) * t + 3 * (1 - t) * t * t + t * t * t
    }
    private func opacity(_ image: CIImage, _ alpha: Double) -> CIImage {
        image.applyingFilter("CIColorMatrix", parameters: ["inputAVector": CIVector(x: 0, y: 0, z: 0, w: min(1, max(0, alpha)))])
    }
    private func around(_ point: CGPoint, scaleX: Double = 1, scaleY: Double = 1, x: Double = 0, y: Double = 0) -> CGAffineTransform {
        CGAffineTransform(translationX: -point.x, y: -point.y)
            .concatenating(CGAffineTransform(scaleX: scaleX, y: scaleY))
            .concatenating(CGAffineTransform(translationX: point.x + x, y: point.y + y))
    }

    func image(localTime: Double, settled: CIImage) throws -> CIImage {
        guard let phases = layer.animationPhases else { return settled }
        let duration = layer.end - layer.start
        guard localTime >= 0, localTime < duration else { return transparent }
        let edge = phases.edgeDuration(duration: duration)
        let effect: TextAnimationPhases.Edge
        let progress: Double
        if phases.exit.isDesigned && localTime > duration - edge {
            effect = phases.exit; progress = (duration - localTime) / edge
        } else if phases.entrance == .wordRelay {
            effect = .wordRelay; progress = min(1, localTime / max(0.001, duration - (phases.exit == .none ? 0 : edge)))
        } else if phases.entrance.isDesigned && localTime < edge {
            effect = phases.entrance; progress = localTime / edge
        } else if phases.entrance == .ribbonWipe || phases.exit == .ribbonWipe {
            effect = .ribbonWipe; progress = 1
        } else { return settled }
        let p = Self.ease(progress)
        let phaseState = try phases.sample(time: localTime, duration: duration)
        if effect == .focusPull {
            return opacity(settled.applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: (pieces.map(\.size).max() ?? 1) * 0.12 * (1 - p)])
                .transformed(by: around(anchor, scaleX: 1.045 - 0.045 * p, scaleY: 1.045 - 0.045 * p)), p)
                .cropped(to: transparent.extent)
        }
        var result = transparent
        func place(_ image: CIImage) { result = image.transformed(by: rotation).composited(over: result).cropped(to: transparent.extent) }
        if let background { place(opacity(background, p)) }
        if effect == .wordRelay {
            let words = pieces.flatMap { piece in piece.words.map { (piece, $0) } }
            guard !words.isEmpty else { return settled }
            let index = min(words.count - 1, Int(progress * Double(words.count)))
            let (piece, sprite) = words[index]
            let rect = sprite.bounds
            let slot = progress * Double(words.count) - Double(index)
            let q = Self.ease(slot * phases.speed / 0.24)
            let center = CGPoint(x: layer.anchorX, y: pieces.map(\.bounds.midY).reduce(0, +) / Double(pieces.count))
            var word = sprite.image
            if phaseState.revealProgress < 1 {
                let prefix = TextAnimationPhases.visiblePrefix(sprite.text, progress: phaseState.revealProgress)
                guard !prefix.isEmpty else { return result }
                let range = NSRange(location: sprite.range.location, length: (prefix as NSString).length)
                word = try vector.image(bounds: rect, maxBitmapBytes: bitmapLimit - bitmapBytes + Int(bounds.width * bounds.height * 4),
                    runIndices: [sprite.runIndex], includeBackground: false, runRanges: [sprite.runIndex: range])
                    .transformed(by: CGAffineTransform(translationX: rect.minX, y: rect.minY))
            }
            place(opacity(word.transformed(by: CGAffineTransform(translationX: center.x - rect.midX, y: center.y - rect.midY - piece.size * 0.15 * (1 - q))), q))
            return result
        }
        for (index, piece) in pieces.enumerated() {
            let stagger = min(0.25, Double(index) * 0.07)
            let q = Self.ease((progress - stagger) / (1 - min(0.25, Double(pieces.count - 1) * 0.07)))
            switch effect {
            case .silkReveal:
                let shifted = piece.image.transformed(by: CGAffineTransform(translationX: 0, y: -piece.bounds.height * (1 - q)))
                place(opacity(shifted.cropped(to: piece.bounds), q))
            case .elasticCondensed:
                for (i, sprite) in piece.glyphs.enumerated() {
                    let rect = sprite.bounds
                    let delay = min(0.3, Double(i) / Double(max(1, piece.glyphs.count)) * 0.3)
                    let g = Self.ease((progress - delay) / 0.7)
                    let overshoot = sin(min(1, max(0, (progress - delay) / 0.7)) * .pi) * 0.08
                    let point = CGPoint(x: rect.midX, y: rect.midY)
                    place(opacity(sprite.image.transformed(by: around(point, scaleX: 0.25 + 0.75 * g + overshoot,
                        scaleY: 1 + 0.08 * (1 - g), y: -piece.size * 0.3 * (1 - g))), g))
                }
            case .ribbonWipe:
                let box = piece.bounds.insetBy(dx: -piece.size * 0.1, dy: -piece.size * 0.06)
                let luminance = piece.fill.red * 0.2126 + piece.fill.green * 0.7152 + piece.fill.blue * 0.0722
                let color = luminance > 0.5 ? CIColor(red: 0.08, green: 0.10, blue: 0.08) : CIColor(red: 0.96, green: 0.94, blue: 0.84)
                let ink = phaseState.revealProgress < 1 ? CIImage.empty() : piece.image
                let ribbon = ink.composited(over: CIImage(color: color).cropped(to: box))
                place(ribbon.cropped(to: CGRect(x: box.minX, y: box.minY, width: box.width * q, height: box.height)))
            case .echoStack:
                let outline = piece.image.applyingFilter("CIMorphologyGradient", parameters: [kCIInputRadiusKey: max(1, piece.size * 0.012)])
                for echo in [-2, -1, 1, 2] {
                    place(opacity(outline.transformed(by: CGAffineTransform(translationX: 0, y: Double(echo) * piece.size * 0.9 * (1 - q))), (1 - q) * 0.65))
                }
                place(opacity(piece.image, p))
            default: place(piece.image)
            }
        }
        if effect == .ribbonWipe && phaseState.revealProgress < 1 {
            return settled.composited(over: result).cropped(to: transparent.extent)
        }
        if effect != .wordRelay && effect != .ribbonWipe && progress > 0.8 {
            return result.applyingFilter("CIDissolveTransition", parameters: [
                kCIInputTargetImageKey: settled, kCIInputTimeKey: min(1, (progress - 0.8) / 0.2)
            ]).cropped(to: transparent.extent)
        }
        return result
    }
}

extension RecipeTextLayer {
    static func makeDesigned(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        let painter = try NativeDesignedTextPainter(layer: layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes)
        let vector = try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas)
        return Self(image: try vector.image(bounds: painter.bounds, maxBitmapBytes: maxBitmapBytes), frame: painter.bounds,
            start: layer.start, end: layer.end, animation: .none, selectionBounds: vector.selectionBounds, portable: layer,
            portableAnchor: vector.anchor, designed: painter,
            discreteReveal: layer.discreteReveal == nil ? nil : NativeDiscreteRevealPainter(layer: layer, assetURLs: assetURLs,
                canvas: canvas, bounds: painter.bounds, maxBitmapBytes: maxBitmapBytes / 3))
    }
}
#endif
