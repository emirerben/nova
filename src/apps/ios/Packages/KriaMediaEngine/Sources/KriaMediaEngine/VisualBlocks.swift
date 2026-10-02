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
        return authoredAlpha * Self.fadeEnvelope(at: time, windowStart: windowStart, windowEnd: windowEnd,
                                                 fadeIn: fadeIn, fadeOut: fadeOut)
    }

    /// The one fade curve for placed media: linear 0.15 s ramps at each
    /// requested edge, and none at all on a window of 0.3 s or less. The
    /// pinned phone Talking recipe fades its overlay cards through
    /// `alpha(at:)`; editor overlays that keep their `MediaTransform`
    /// positioning fade through the compositor's `OverlayFadeWindow` (on the
    /// layer's actual bounds), and `TimelineClip.overlayFadeAlpha(at:)`
    /// evaluates the same curve on the clip's own window. All of them call
    /// this, so the editor preview and the device render share a curve.
    public static func fadeEnvelope(at time: Double, windowStart: Double, windowEnd: Double,
                                    fadeIn: Bool, fadeOut: Bool) -> Double {
        guard windowEnd - windowStart > 0.3 else { return 1 }
        return min(fadeIn ? min(1, max(0, (time - windowStart) / 0.15)) : 1,
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

/// Seconds of source audio each side of a same-source cut borrows across it.
/// The two sides then overlap for twice this long, so the room tone (rain,
/// traffic) runs straight through the join instead of dipping to silence for
/// the two edge declicks, which is what made speech-cleanup cuts sound jumpy.
let audioCutCrossfadeHandle: TimeInterval = 0.025

/// The audio a clip borrows past its edges at a speech-cleanup style cut: two
/// clips of the SAME source laid back to back with a removed span between them
/// in the source. The outgoing clip's audio runs `tail` into that span and the
/// incoming clip's starts `lead` before its window, crossfading over the cut.
/// Only audio widens; the picture still hard-cuts on the original boundary.
struct AudioCutHandles: Equatable, Sendable {
    var lead: TimeInterval = 0
    var tail: TimeInterval = 0
    static let none = AudioCutHandles()

    /// Handles per clip ID. A cut qualifies only when both sides play their
    /// source at 1x with no hold, no transition and no authored audio fade, and
    /// the source jumps forward: the borrowed audio is then removed material
    /// (pause, breath, filler edge), never a kept word. Each handle stays
    /// inside half the removed span and a quarter of either clip.
    static func plan(_ tracks: [TimelineTrack]) -> [String: AudioCutHandles] {
        var result: [String: AudioCutHandles] = [:]
        for track in tracks where track.kind == .video {
            let clips = track.clips.sorted { $0.timelineStart < $1.timelineStart }
            for (outgoing, incoming) in zip(clips, clips.dropFirst()) {
                let removed = incoming.sourceStart - (outgoing.sourceStart + outgoing.sourceDuration)
                guard outgoing.sourceAssetID == incoming.sourceAssetID,
                      outgoing.rate == 1, incoming.rate == 1,
                      outgoing.holdDuration == nil, incoming.holdDuration == nil,
                      (incoming.transition?.duration ?? 0) == 0,
                      (outgoing.audioFadeOut ?? 0) == 0, (incoming.audioFadeIn ?? 0) == 0,
                      abs(incoming.timelineStart - (outgoing.timelineStart + outgoing.duration)) < 0.001,
                      removed > 0.001 else { continue }
                let handle = min(audioCutCrossfadeHandle, removed / 2, outgoing.sourceDuration / 4, incoming.sourceDuration / 4)
                result[outgoing.id, default: .none].tail = handle
                result[incoming.id, default: .none].lead = handle
            }
        }
        return result
    }
}

extension TimelineClip {
    /// The window this clip's audio track plays once a cut's handles widen it.
    /// Handles are only planned for 1x clips, so source and timeline move together.
    func widened(by handles: AudioCutHandles) -> TimelineClip {
        guard handles != .none else { return self }
        var clip = self
        clip.sourceStart -= handles.lead
        clip.timelineStart -= handles.lead
        clip.sourceDuration += handles.lead + handles.tail
        return clip
    }
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

/// Length of the fade a source-audio clip gets at each edge. AVAudioMix applies
/// ramps per render buffer, so the audible fade runs roughly 10 ms longer than
/// requested (measured with the PCM tests); 25 ms lands a hard cut at ~zero on
/// both sides and is still inaudible as a fade.
let audioEdgeFade: TimeInterval = 0.025

/// Complete gain envelope for one clip's audio track (KRI-184).
///
/// AVAudioMix renders a track at unity until its first set point, and applies
/// volume per render buffer, so a clip whose first point sits exactly on its cut
/// leaks the first few milliseconds at full level even when muted. Seeding the
/// timeline origin with 0 closes that; the edge ramps stop hard cuts clicking.
///
/// AVAudioMix interpolates a `setVolume` point into the next point (measured:
/// a mute window written as a 0 point then a full point faded back in across
/// the whole window), so every level after the leading zero is an explicit
/// ramp, back to back. Mute windows gate the steady region between the edge
/// fades: silent inside a window, with a declick ramp on the audible side of
/// each window edge. Window edges inside an edge zone are ignored (at most
/// 25 ms of a window edge, or of an authored fade).
///
/// KRI-139: an authored `audioFadeIn`/`audioFadeOut` longer than the declick
/// edge replaces it, and a `duck` envelope scales the steady region between the
/// edges (the side-chain ducked footage bed).
///
/// `cut` crossfades a same-source cut: `clip` is already `widened(by: cut)`, and
/// each borrowed edge fades over twice its handle on an equal-power curve,
/// centred on the original boundary, so the two sides overlap instead of each
/// declicking to silence.
func applyAudioGain(_ parameter: AVMutableAudioMixInputParameters, clip: TimelineClip, gain: Double, windows: [AudioMuteWindow], duck: AudioDuckEnvelope? = nil, cut: AudioCutHandles = .none) {
    func cm(_ seconds: Double) -> CMTime { CMTime(seconds: seconds, preferredTimescale: 60_000) }
    func ramp(_ from: Float, _ to: Float, _ start: Double, _ end: Double) {
        let range = CMTimeRange(start: cm(start), end: cm(end))
        if range.duration > .zero { parameter.setVolumeRamp(fromStartVolume: from, toEndVolume: to, timeRange: range) }
    }
    let start = clip.timelineStart
    // Audio stops with the moving segment; a held video tail carries no sound.
    let audioEnd = start + clip.sourceDuration / clip.rate
    let edge = max(0, min(audioEdgeFade, (audioEnd - start) / 4))
    let half = (audioEnd - start) / 2
    let fadeIn = cut.lead > 0 ? 2 * cut.lead : max(edge, min(clip.audioFadeIn ?? 0, half))
    let fadeOut = cut.tail > 0 ? 2 * cut.tail : max(edge, min(clip.audioFadeOut ?? 0, half))
    let steadyStart = start + fadeIn, steadyEnd = audioEnd - fadeOut
    // A crossfade edge follows sin/cos so the overlapped power stays level;
    // AVAudioMix ramps are linear, so the curve is a few linear pieces.
    func edgeRamp(_ level: Float, _ from: Double, _ to: Double, rising: Bool, curved: Bool) {
        let pieces = curved ? 3 : 1, piece = (to - from) / Double(pieces)
        func share(_ step: Int) -> Float {
            let progress = Double(step) / Double(pieces), reached = rising ? progress : 1 - progress
            return level * Float(curved ? sin(reached * .pi / 2) : reached)
        }
        for step in 0..<pieces { ramp(share(step), share(step + 1), from + piece * Double(step), from + piece * Double(step + 1)) }
    }
    let affected = windows.filter { $0.clipIDs.contains(clip.id) && $0.start < clip.timelineStart + clip.duration && $0.end > clip.timelineStart }
    // Window edges split the steady region into runs: a muted run holds 0, an
    // audible run ramps from and to 0 at each window edge it touches.
    let edges = Set(affected.flatMap { [$0.start, $0.end] }.filter { $0 > steadyStart && $0 < steadyEnd }).sorted()
    let cuts = [steadyStart] + edges + [steadyEnd]
    var corners: [(time: Double, gain: Double)] = []
    for (from, to) in zip(cuts, cuts.dropFirst()) {
        if affected.contains(where: { $0.start <= from && $0.end > from }) { corners += [(from, 0), (to, 0)]; continue }
        let declick = min(audioEdgeFade, (to - from) / 2)
        corners += from > steadyStart ? [(from, 0), (from + declick, 1)] : [(from, 1)]
        corners += to < steadyEnd ? [(to - declick, 1), (to, 0)] : [(to, 1)]
    }
    // Piecewise linear like the duck curve, so the two multiply corner by corner.
    let gate = AudioDuckEnvelope(points: corners)
    func level(_ time: Double) -> Float { Float(gain * clip.volume * gate.gain(at: time) * (duck?.gain(at: time) ?? 1)) }
    // Nothing plays before the clip: keep the implicit unity default away from it.
    // The fade-in ramp starts at 0, so the point has nothing to interpolate toward.
    if start > 0 { parameter.setVolume(0, at: .zero) }
    edgeRamp(level(steadyStart), start, steadyStart, rising: true, curved: cut.lead > 0)
    let marks = Set(corners.map(\.time) + (duck?.times(in: steadyStart...steadyEnd) ?? [])).sorted()
    for (from, to) in zip(marks, marks.dropFirst()) { ramp(level(from), level(to), from, to) }
    edgeRamp(level(steadyEnd), steadyEnd, audioEnd, rising: false, curved: cut.tail > 0)
}
#endif
