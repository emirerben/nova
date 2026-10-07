import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation

/// Encoded-RGB footage grades, applied after crop and before graphics.
/// Preview and export share this pipeline. FFmpeg's resampling, 8-bit rounding,
/// and temporal RNG differ; release requires fixture and physical-device parity.
nonisolated enum SlidePostLookRenderer {
    enum Error: Swift.Error, Equatable {
        case unknownPreset(String)
        case filterUnavailable(String)
        case missingResource(String)
    }

    private static let names = ["stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"]
    static func supports(_ preset: String) -> Bool { preset == "none" || names.contains(preset) }

    // Swift static initialization is synchronized. Cache successes and failures:
    // a missing asset must throw, never silently yield neutral footage.
    private static let cubes: [String: Data] = {
        var result: [String: Data] = [:]
        for name in names {
            if let url = resource("\(name)-64.cube", "bin"),
               let data = try? Data(contentsOf: url), data.count == 64 * 64 * 64 * 4 * 4 {
                result[name] = data
            }
        }
        return result
    }()
    private static let mask: CIImage? = resource("faded-vignette-mask", "png").flatMap {
        CIImage(contentsOf: $0, options: [.colorSpace: NSNull()])
    }

    private static func resource(_ name: String, _ ext: String) -> URL? {
        Bundle.main.url(forResource: name, withExtension: ext, subdirectory: "Looks")
            ?? Bundle.main.url(forResource: name, withExtension: ext)
    }

    static func apply(_ image: CIImage, preset: String) throws -> CIImage {
        guard supports(preset) else { throw Error.unknownPreset(preset) }
        guard preset != "none" else { return image }
        let grade = try colorCube(image, name: preset)
        switch preset {
        case "olive_film", "smoky_split_tone":
            let olive = preset == "olive_film"
            let softened = try softness(grade, amount: olive ? 0.18 : 0.27)
            let shaded = try vignette(softened, angle: olive ? 0.0616 : 0.154)
            return try grain(shaded, strength: olive ? 2 : 4, seed: olive ? 6413 : 8721)
        case "faded_analog":
            guard let mask else { throw Error.missingResource("faded-vignette-mask.png") }
            // Server stretches the mask to the canvas, then multiplies limited-
            // range Y codes only. Production FFmpeg 7.1.5 converts the full-
            // range grayscale PNG to limited-range YUV (white is 235).
            // Chroma remains from the graded image.
            let scaled = mask.transformed(by: CGAffineTransform(scaleX: image.extent.width / mask.extent.width, y: image.extent.height / mask.extent.height))
                .transformed(by: CGAffineTransform(translationX: image.extent.minX, y: image.extent.minY))
            return try color("fadedMask", source: """
                kernel vec4 fadedMask(__sample s, __sample m) {
                    float y = dot(s.rgb, vec3(0.299, 0.587, 0.114));
                    float code = (16.0 + 219.0*y)/255.0;
                    float maskCode = (16.0 + 219.0*m.r)/255.0;
                    float newY = (255.0*code*maskCode-16.0)/219.0;
                    return vec4(clamp(s.rgb + newY-y, 0.0, 1.0), s.a);
                }
                """, image: try grain(grade, strength: 3, seed: 9321), extra: [scaled])
        case "stadium_diffusion": return try stadium(image, grade: grade)
        default: return grade
        }
    }

    private static func colorCube(_ image: CIImage, name: String) throws -> CIImage {
        guard let data = cubes[name] else { throw Error.missingResource("\(name)-64.cube.bin") }
        let filter = CIFilter.colorCube()
        filter.inputImage = image; filter.cubeDimension = 64; filter.cubeData = data
        return try output(filter, extent: image.extent)
    }

    private static func output(_ filter: CIFilter, extent: CGRect) throws -> CIImage {
        guard let result = filter.outputImage else { throw Error.filterUnavailable(filter.name) }
        return result.cropped(to: extent)
    }

    private final class KernelCache: @unchecked Sendable {
        private let lock = NSLock()
        private var kernels: [String: CIColorKernel] = [:]

        func kernel(_ name: String, source: String) -> CIColorKernel? {
            lock.lock()
            defer { lock.unlock() }
            if let existing = kernels[name] { return existing }
            let compiled = CIColorKernel(source: source)
            kernels[name] = compiled
            return compiled
        }
    }
    private static let kernelCache = KernelCache()

    private static func color(_ name: String, source: String, image: CIImage, extra: [Any] = []) throws -> CIImage {
        guard let kernel = kernelCache.kernel(name, source: source),
              let result = kernel.apply(extent: image.extent, arguments: [image] + extra) else {
            throw Error.filterUnavailable(name)
        }
        return result
    }

    private static func blur(_ image: CIImage, sigma: Double) throws -> CIImage {
        let filter = CIFilter.gaussianBlur(); filter.inputImage = image.clampedToExtent(); filter.radius = Float(sigma)
        return try output(filter, extent: image.extent)
    }

    private static func mix(_ a: CIImage, _ b: CIImage, amount: Double) throws -> CIImage {
        try color("mix", source: "kernel vec4 blend(__sample a, __sample b, float t) { return mix(a,b,t); }", image: a, extra: [b, amount])
    }

    private static func softness(_ image: CIImage, amount: Double) throws -> CIImage {
        let row: [CGFloat] = [1, 4, 6, 4, 1]
        let weights = row.flatMap { y in row.map { x in x * y / 256 } }
        let filter = CIFilter.convolution5X5()
        filter.inputImage = image.clampedToExtent()
        filter.weights = CIVector(values: weights, count: weights.count)
        return try color("lumaSoftness", source: """
            kernel vec4 lumaSoftness(__sample s, __sample b, float amount) {
                float delta = dot(b.rgb-s.rgb, vec3(0.299,0.587,0.114))*amount;
                return vec4(clamp(s.rgb+delta,0.0,1.0),s.a);
            }
            """, image: image, extra: [try output(filter, extent: image.extent), amount])
    }

    private static func vignette(_ image: CIImage, angle: Double) throws -> CIImage {
        try color("vignette", source: """
            kernel vec4 vignette(__sample s, vec2 centre, float focal) {
                vec2 d = destCoord()-centre;
                float factor = 1.0/(1.0+dot(d,d)/(focal*focal));
                return vec4(s.rgb*factor*factor,s.a);
            }
            """, image: image, extra: [CIVector(x: image.extent.midX, y: image.extent.midY), hypot(image.extent.width, image.extent.height) / (2 * tan(angle))])
    }

    /// Stable, zero-mean uniform YUV noise using authored strength/seed inputs;
    /// approximate amplitude, and frozen across frames. FFmpeg uses a different RNG.
    private static func grain(_ image: CIImage, strength: Double, seed: Double) throws -> CIImage {
        try color("grain", source: """
            kernel vec4 grain(__sample s, float strength, float seed, vec2 origin) {
                vec2 p = floor(destCoord()-origin);
                float n0 = fract(sin(dot(p,vec2(12.9898,78.233))+seed)*43758.5453)-0.5;
                float n1 = fract(sin(dot(floor(p/2.0),vec2(39.3468,11.135))+seed+1.0)*23421.631)-0.5;
                float n2 = fract(sin(dot(floor(p/2.0),vec2(73.156,52.235))+seed+2.0)*9513.135)-0.5;
                vec3 delta = vec3(n0/219.0+1.402*n2/224.0,
                    n0/219.0-0.344136*n1/224.0-0.714136*n2/224.0,
                    n0/219.0+1.772*n1/224.0)*strength;
                return vec4(clamp(s.rgb+delta,0.0,1.0),s.a);
            }
            """, image: image, extra: [strength, seed, CIVector(x: image.extent.minX, y: image.extent.minY)])
    }

    private static func resize(_ image: CIImage, width: CGFloat, height: CGFloat) -> CIImage {
        // Upsampling asks for texels beyond the first/last pixel centres.
        // Extend the source before transforming so those taps sample its edge,
        // rather than transparent black. Crop only after interpolation.
        image.clampedToExtent()
            .transformed(by: CGAffineTransform(translationX: -image.extent.minX, y: -image.extent.minY))
            .transformed(by: CGAffineTransform(scaleX: width/image.extent.width, y: height/image.extent.height))
            .cropped(to: CGRect(x: 0, y: 0, width: width, height: height))
    }

    private static let lensKernel = CIWarpKernel(source: """
            kernel vec2 lens(vec2 centre, float diagonalSquared) {
                vec2 d = destCoord()-centre;
                float r2 = dot(d,d)/diagonalSquared;
                return centre+d*(1.0-0.07*r2+0.022*r2*r2);
            }
            """)

    private static func stadium(_ source: CIImage, grade: CIImage) throws -> CIImage {
        let extent = source.extent
        func even(_ x: CGFloat) -> CGFloat { let n = max(2, Int(x.rounded(.toNearestOrEven))); return CGFloat(n + n % 2) }
        let w = even(extent.width/2), h = even(extent.height/2)
        let half = resize(source, width: w, height: h)
        let threshold = try color("highlights", source: """
            kernel vec4 highlights(__sample s) {
                float y = dot(s.rgb,vec3(0.299,0.587,0.114));
                return vec4(vec3(step(180.0/255.0,y)*y),1.0);
            }
            """, image: half)
        let alpha = resize(try blur(threshold, sigma: max(1, Double(min(w,h))*0.012)), width: extent.width, height: extent.height)
            .transformed(by: CGAffineTransform(translationX: extent.minX, y: extent.minY))
        let clean = try color("bloom", source: "kernel vec4 bloom(__sample s, __sample a) { return vec4(mix(s.rgb,vec3(1.0),a.r*0.16),s.a); }", image: grade, extra: [alpha])
        let small = resize(clean, width: w, height: h)
        func zoom(_ sx: CGFloat, _ sy: CGFloat, _ sigma: Double, x: CGFloat? = nil, y: CGFloat? = nil, input: CIImage? = nil) throws -> CIImage {
            let zw = max(w,(w*sx).rounded(.toNearestOrEven)), zh = max(h,(h*sy).rounded(.toNearestOrEven))
            let dx = x ?? floor((zw-w)/2), dy = y ?? floor((zh-h)/2)
            let scaled = resize(input ?? small, width: zw, height: zh)
                .transformed(by: CGAffineTransform(translationX: -dx, y: -dy)).cropped(to: CGRect(x: 0,y: 0,width: w,height: h))
            return try blur(scaled, sigma: sigma)
        }
        let za = try mix(zoom(551/540,979/960,0.9), zoom(565/540,1004/960,1.5), amount: 0.40)
        let zb = try mix(zoom(582/540,1034/960,2.3), zoom(602/540,1070/960,3.0), amount: 0.44)
        let smear = try mix(za,zb,amount: 0.46)
        guard let lens = lensKernel, let warped = lens.apply(extent: small.extent, roiCallback: { _, rect in rect.insetBy(dx: -w,dy: -h) }, image: small.clampedToExtent(), arguments: [CIVector(x:w/2,y:h/2), (w*w+h*h)/4]) else { throw Error.filterUnavailable("lens") }
        let ow = (w*584/540).rounded(.toNearestOrEven), oh = (h*1038/960).rounded(.toNearestOrEven)
        // FFmpeg's crop y is measured from top, Core Image's from bottom.
        let optic = try zoom(584/540,1038/960,1.2,x: ((ow-w)*0.43).rounded(.toNearestOrEven),y: oh-h-((oh-h)*0.46).rounded(.toNearestOrEven),input: warped)
        let shifted = try color("chromatic", source: "kernel vec4 chromatic(__sample s, __sample r, __sample b) { return vec4(r.r,s.g,b.b,s.a); }", image: optic, extra: [optic.clampedToExtent().transformed(by: CGAffineTransform(translationX: 2,y: 0)),optic.clampedToExtent().transformed(by: CGAffineTransform(translationX: -2,y: 0))])
        let hybrid = resize(try mix(smear,shifted,amount: 0.24), width: extent.width,height: extent.height).transformed(by: CGAffineTransform(translationX: extent.minX,y: extent.minY))
        let merged = try color("edgeMerge", source: """
            kernel vec4 edgeMerge(__sample a, __sample b, vec2 centre, vec2 size) {
                float t = clamp((length((destCoord()-centre)/(size/2.0))-0.42)/0.46,0.0,1.0);
                return mix(a,b,t);
            }
            """, image: clean, extra: [hybrid,CIVector(x:extent.midX,y:extent.midY),CIVector(x:extent.width,y:extent.height)])
        return try grain(vignette(merged,angle: .pi/14),strength: 4,seed: 5144)
    }
}
