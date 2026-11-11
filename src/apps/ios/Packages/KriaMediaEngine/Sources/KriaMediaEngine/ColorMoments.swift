import Foundation

// MARK: - Colour moments (KRI-1350)

/// A stretch of the finished video drawn in another tone (black-and-white, warm, faded), entered and left with a
/// cut, a white flash, a warm light-leak flash or a dip to black. It is decided per output frame by time, on the
/// fully composed picture below the text, so a green-screen speaker and the background behind them always change
/// together (failure mode 8). Captions, motion text and drawings keep their authored colours (failure mode 9).
///
/// Wire (snake_case on the API, like the rest of the recipe):
/// `{"id": "cm1", "start": 5.0, "end": 12.0, "tone": "mono",
///   "enter": {"style": "light_leak", "duration": 0.33}, "exit": {"style": "cut"}}`
public struct ColorMoment: Codable, Equatable, Sendable, Identifiable {
    public enum Tone: String, Codable, Sendable, CaseIterable { case mono, warm, faded }

    public struct Edge: Codable, Equatable, Sendable {
        public enum Style: String, Codable, Sendable, CaseIterable {
            case cut
            case flashWhite = "flash_white"
            case lightLeak = "light_leak"
            case dipBlack = "dip_black"
        }
        public static let defaultDuration = 1.0 / 3.0
        public static let durationRange = 0.1...0.6
        public var style: Style
        /// Seconds the flash or dip lasts, centred on the edge time. Ignored for a cut.
        public var duration: Double

        public init(style: Style, duration: Double = Edge.defaultDuration) {
            self.style = style; self.duration = duration
        }
        private enum CodingKeys: String, CodingKey { case style, duration }
        public init(from decoder: Decoder) throws {
            try rejectUnknownAssetFields(decoder, allowed: ["style", "duration"])
            let c = try decoder.container(keyedBy: CodingKeys.self)
            style = try c.decode(Style.self, forKey: .style)
            duration = try c.decodeIfPresent(Double.self, forKey: .duration) ?? Edge.defaultDuration
        }
        public func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(style, forKey: .style)
            if style != .cut { try c.encode(duration, forKey: .duration) }
        }
    }

    public static let maxMoments = 6
    public static let minimumLength = 0.5

    public var id: String
    public var start: Double
    public var end: Double
    public var tone: Tone
    public var enter: Edge
    public var exit: Edge

    public init(id: String, start: Double, end: Double, tone: Tone,
                enter: Edge = Edge(style: .cut), exit: Edge = Edge(style: .cut)) {
        self.id = id; self.start = start; self.end = end; self.tone = tone; self.enter = enter; self.exit = exit
    }

    private enum CodingKeys: String, CodingKey { case id, start, end, tone, enter, exit }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["id", "start", "end", "tone", "enter", "exit"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        start = try c.decode(Double.self, forKey: .start)
        end = try c.decode(Double.self, forKey: .end)
        tone = try c.decode(Tone.self, forKey: .tone)
        enter = try c.decodeIfPresent(Edge.self, forKey: .enter) ?? Edge(style: .cut)
        exit = try c.decodeIfPresent(Edge.self, forKey: .exit) ?? Edge(style: .cut)
    }
    /// A cut edge is the default and is never written, so the server's and the phone's bytes agree.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id); try c.encode(start, forKey: .start); try c.encode(end, forKey: .end)
        try c.encode(tone, forKey: .tone)
        if enter.style != .cut { try c.encode(enter, forKey: .enter) }
        if exit.style != .cut { try c.encode(exit, forKey: .exit) }
    }

    func validate(duration total: Double) throws {
        guard !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              start.isFinite, end.isFinite, start >= 0, end <= total + 0.000_001,
              end - start >= Self.minimumLength else { throw RecipeError.invalidTimeline }
        for edge in [enter, exit] where edge.style != .cut {
            guard edge.duration.isFinite, ColorMoment.Edge.durationRange.contains(edge.duration) else { throw RecipeError.invalidTimeline }
        }
    }
}

/// The last seconds of the edit ramp to black (the outro, when present, follows untouched).
public struct EndFade: Codable, Equatable, Sendable {
    public static let durationRange = 0.3...4.0
    public var duration: Double
    public init(duration: Double) { self.duration = duration }
    private enum CodingKeys: String, CodingKey { case duration }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["duration"])
        duration = try decoder.container(keyedBy: CodingKeys.self).decode(Double.self, forKey: .duration)
    }
    func validate(duration total: Double) throws {
        guard duration.isFinite, Self.durationRange.contains(duration), duration <= total else { throw RecipeError.invalidTimeline }
    }
}

extension Array where Element == ColorMoment {
    func validateMoments(duration total: Double) throws {
        guard count <= ColorMoment.maxMoments, Set(map(\.id)).count == count else { throw RecipeError.invalidTimeline }
        for moment in self { try moment.validate(duration: total) }
        // Sorted and never overlapping: one tone per frame (failure mode 7).
        for (a, b) in zip(self, dropFirst()) where b.start < a.end - 0.000_001 { throw RecipeError.invalidTimeline }
    }
}

/// Pure timing shared by the renderer and its tests. An edge at time `t` lasting `d` is drawn over
/// `[t - d/2, t + d/2]`; a flash peaks and a dip bottoms out exactly at `t`, and the tone switches at `t`, under
/// the flash. Frames are sampled at their presentation time, so the frame at `t` (on the frame grid) is the peak.
public enum ColorMomentTiming {
    public struct Sample: Equatable, Sendable {
        /// The tone the frame is drawn in, nil = the original colours.
        public var tone: ColorMoment.Tone?
        /// The flash or dip touching this frame and its weight 0...1 (1 = the peak frame).
        public var edge: ColorMoment.Edge.Style?
        public var weight: Double
        /// 0 = untouched, 1 = black (the end fade).
        public var fadeToBlack: Double
    }

    /// The tone is active on `[start, end)`. Edge times are compared with a micro-second tolerance so a server
    /// literal one ulp off the frame grid still lands on its frame (the same problem `RecipeInstructionTiming` solves).
    public static func sample(_ moments: [ColorMoment], endFade: EndFade?, total: Double, frameRate: Double,
                              at time: Double) -> Sample {
        let epsilon = 0.000_001
        var result = Sample(tone: nil, edge: nil, weight: 0, fadeToBlack: 0)
        for moment in moments where time >= moment.start - epsilon && time < moment.end - epsilon {
            result.tone = moment.tone
        }
        for moment in moments {
            for (edge, at) in [(moment.enter, moment.start), (moment.exit, moment.end)] where edge.style != .cut {
                let w = weight(time: time, edgeAt: at, duration: edge.duration)
                if w > result.weight { result.weight = w; result.edge = edge.style }
            }
        }
        if let endFade, total > 0 {
            // Linear in encoded values, reaching black on the edit's last frame (failure mode 12).
            let lastFrame = total - 1 / max(1, frameRate)
            let start = total - endFade.duration
            if time >= start - epsilon && time < total - epsilon {
                result.fadeToBlack = lastFrame > start ? min(1, max(0, (time - start) / (lastFrame - start))) : 1
            }
        }
        return result
    }

    /// A sharp peak with soft shoulders, closed at both window edges. Fitted to Çiğdem's measured light leak
    /// (`tests/fixtures/colour-moments/cigdem-reel-answer-key.json`): one frame from the peak ≈ 0.55, two ≈ 0.23,
    /// three ≈ 0.12 of the peak lift, at 30 fps and a 1/3 s edge.
    public static func weight(time: Double, edgeAt: Double, duration: Double) -> Double {
        guard duration > 0 else { return 0 }
        let u = (time - (edgeAt - duration / 2)) / duration
        guard u > 0, u < 1 else { return 0 }
        // The shape scales with the edge: 0.1106 of its length is one frame of a 1/3 s edge at 30 fps.
        let x = (u - 0.5) / 0.1106
        let peak = 1 / (1 + x * x)
        func smoothstep(_ v: Double) -> Double { let t = min(1, max(0, v)); return t * t * (3 - 2 * t) }
        return peak * smoothstep(u / 0.15) * smoothstep((1 - u) / 0.15)
    }
}

#if canImport(CoreImage)
import CoreImage

/// Draws a colour-moment sample on a composed frame. The compositor works in linear sRGB; tones and flashes are
/// specified on encoded values (as the measured reference and FFmpeg are), so the frame is converted to encoded
/// sRGB, treated, and converted back, like `NativeClipTransitionPainter`.
enum ColorMomentPainter {
    private static let edgeKernel: Result<CIKernel, Error> = Result {
        let kernels = try CIKernel.kernels(withMetalString: """
        #include <CoreImage/CoreImage.h>
        using namespace metal;
        extern "C" { namespace coreimage {
        // style: 1 = white flash, 2 = warm light leak, 3 = dip to black.
        [[stitchable]] float4 kriaColorMomentEdge(sampler frame, float w, float style, float width, float height,
                                                  destination dest) {
            float2 p = dest.coord();
            float4 c = frame.sample(frame.transform(p));
            float3 rgb = c.rgb;
            if (style > 2.5) {
                rgb = rgb * (1.0 - w);
            } else if (style > 1.5) {
                // Warm leak from the top-left corner (Core Image's origin is bottom-left), white at the peak.
                float2 uv = float2(p.x / max(width, 1.0), 1.0 - p.y / max(height, 1.0));
                float g = 0.62 + 0.38 * (1.0 - clamp(length(uv) / 1.4142, 0.0, 1.0));
                float lift = w * (g + (1.0 - g) * w);
                float3 orange = float3(1.0, 0.56, 0.2);
                float core = smoothstep(0.55, 1.0, w);
                float3 leak = mix(orange, float3(1.0), core);
                rgb = 1.0 - (1.0 - rgb) * (1.0 - leak * lift);
            } else {
                rgb = 1.0 - (1.0 - rgb) * (1.0 - w);
            }
            return float4(clamp(rgb, 0.0, 1.0), c.a);
        }
        }}
        """)
        guard let kernel = kernels.first else { throw NativePreviewFeatureError("ColorMomentPainter-1") }
        return kernel
    }

    /// `frame` is in the compositor's linear working space and covers `canvas`.
    static func image(_ frame: CIImage, sample: ColorMomentTiming.Sample, canvas: CGRect) throws -> CIImage {
        guard sample.tone != nil || sample.weight > 0 else { return frame }
        var encoded = frame.applyingFilter("CILinearToSRGBToneCurve")
        if let tone = sample.tone { encoded = toned(encoded, tone) }
        if let style = sample.edge, sample.weight > 0 {
            let code: Double = switch style { case .flashWhite: 1; case .lightLeak: 2; case .dipBlack: 3; case .cut: 0 }
            if code > 0 {
                guard let lit = try edgeKernel.get().apply(extent: canvas, roiCallback: { _, rect in rect }, arguments: [
                    encoded, sample.weight, code, canvas.width, canvas.height
                ]) else { throw NativePreviewFeatureError("ColorMomentPainter-2") }
                encoded = lit
            }
        }
        return encoded.applyingFilter("CISRGBToneCurveToLinear").cropped(to: canvas)
    }

    /// Multiplies the encoded picture towards black; applied last, over text and drawings too.
    static func faded(_ frame: CIImage, amount: Double, canvas: CGRect) -> CIImage {
        guard amount > 0 else { return frame }
        let keep = CGFloat(max(0, 1 - amount))
        return frame.applyingFilter("CILinearToSRGBToneCurve")
            .applyingFilter("CIColorMatrix", parameters: [
                "inputRVector": CIVector(x: keep, y: 0, z: 0, w: 0),
                "inputGVector": CIVector(x: 0, y: keep, z: 0, w: 0),
                "inputBVector": CIVector(x: 0, y: 0, z: keep, w: 0),
            ])
            .applyingFilter("CISRGBToneCurveToLinear").cropped(to: canvas)
    }

    private static func toned(_ encoded: CIImage, _ tone: ColorMoment.Tone) -> CIImage {
        switch tone {
        case .mono:
            // Rec. 709 luma weights on encoded values, a touch of contrast like archive prints.
            let r = CIVector(x: 0.2126, y: 0.7152, z: 0.0722, w: 0)
            return encoded.applyingFilter("CIColorMatrix", parameters: [
                "inputRVector": r, "inputGVector": r, "inputBVector": r,
            ]).applyingFilter("CIColorControls", parameters: [kCIInputSaturationKey: 0, kCIInputContrastKey: 1.06])
        case .warm:
            return encoded.applyingFilter("CIColorMatrix", parameters: [
                "inputRVector": CIVector(x: 1.06, y: 0.04, z: 0, w: 0),
                "inputGVector": CIVector(x: 0, y: 1.0, z: 0, w: 0),
                "inputBVector": CIVector(x: 0, y: 0, z: 0.84, w: 0),
                "inputBiasVector": CIVector(x: 0.02, y: 0.005, z: 0, w: 0),
            ])
        case .faded:
            return encoded.applyingFilter("CIColorControls", parameters: [
                kCIInputSaturationKey: 0.62, kCIInputContrastKey: 0.86, kCIInputBrightnessKey: 0.04,
            ])
        }
    }
}

/// One per composition: the recipe's moments, its end fade and the edit's own length (the outro is after it).
struct ColorMomentProgram: Sendable {
    let moments: [ColorMoment]
    let endFade: EndFade?
    let total: Double
    let frameRate: Double

    init?(recipe: EditRecipe, total: Double) {
        guard !recipe.colorMoments.isEmpty || recipe.endFade != nil else { return nil }
        moments = recipe.colorMoments.sorted { $0.start < $1.start }
        endFade = recipe.endFade
        self.total = total
        frameRate = recipe.frameRate
    }

    func sample(at time: Double) -> ColorMomentTiming.Sample {
        ColorMomentTiming.sample(moments, endFade: endFade, total: total, frameRate: frameRate, at: time)
    }
}
#endif
