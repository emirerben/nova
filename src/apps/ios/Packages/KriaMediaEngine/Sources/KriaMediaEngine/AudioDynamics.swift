import Foundation
#if canImport(AVFoundation)
@preconcurrency import AVFoundation

/// Gain curve that side-chain ducks the footage's own audio under the recipe's
/// audio-kind tracks (KRI-139). AVAudioMix has no dynamics stage, so the device
/// reads the key (the narration voice) once, runs ffmpeg's `sidechaincompress`
/// gain computer over it with the cloud's narrated settings, and hands the result
/// to `applyAudioGain` as volume ramps: the bed dips while the voice talks and
/// rises in its pauses, as in `_mix_user_voiceover`'s footage-bed branch.
struct AudioDuckEnvelope: Equatable, Sendable {
    /// Piecewise-linear (time, gain) points in timeline seconds, ascending. Gain is
    /// 1 before the first point and holds the last point's value after it.
    let points: [(time: Double, gain: Double)]

    static func == (lhs: AudioDuckEnvelope, rhs: AudioDuckEnvelope) -> Bool {
        lhs.points.count == rhs.points.count && zip(lhs.points, rhs.points).allSatisfy { $0.time == $1.time && $0.gain == $1.gain }
    }

    func gain(at time: Double) -> Double {
        guard let first = points.first, time > first.time else { return points.first?.gain ?? 1 }
        guard let upper = points.firstIndex(where: { $0.time >= time }) else { return points.last?.gain ?? 1 }
        let a = points[upper - 1], b = points[upper]
        guard b.time > a.time else { return b.gain }
        return a.gain + (b.gain - a.gain) * (time - a.time) / (b.time - a.time)
    }

    /// Breakpoints strictly inside `range`, for callers emitting ramps between them.
    func times(in range: ClosedRange<Double>) -> [Double] {
        points.map(\.time).filter { $0 > range.lowerBound && $0 < range.upperBound }
    }

    /// `sidechaincompress=threshold=0.03:ratio=8:attack=15:release=300:makeup=1`
    /// (`template_orchestrate._mix_user_voiceover`), ffmpeg defaults otherwise:
    /// rms detection, channel-average link, knee 2.82843.
    struct Compressor {
        var threshold = 0.03, ratio = 8.0, attackMS = 15.0, releaseMS = 300.0, knee = 2.82843

        /// Gain per key sample, exactly `af_sidechaincompress.c`'s downward
        /// compressor with `detection=rms` (the level is squared, the slope halved).
        func gains(forKey levels: [Float], sampleRate: Double) -> [Float] {
            let attack = min(1, 1 / (attackMS * sampleRate / 4000)), release = min(1, 1 / (releaseMS * sampleRate / 4000))
            let thres = log(threshold), linKneeStart = threshold / knee.squareRoot(), linKneeStop = threshold * knee.squareRoot()
            let adjKneeStart = linKneeStart * linKneeStart
            let kneeStart = log(linKneeStart), kneeStop = log(linKneeStop)
            let compressedKneeStop = (kneeStop - thres) / ratio + thres
            var slope = 0.0
            return levels.map { level in
                let squared = Double(level) * Double(level)
                slope += (squared - slope) * (squared > slope ? attack : release)
                guard slope > 0, slope > adjKneeStart else { return 1 }
                let logSlope = 0.5 * log(slope)
                var gain = (logSlope - thres) / ratio + thres
                if knee > 1, logSlope < kneeStop {
                    gain = Self.hermite(logSlope, kneeStart, kneeStop, kneeStart, compressedKneeStop, 1, 1 / ratio)
                }
                return Float(exp(gain - logSlope))
            }
        }

        private static func hermite(_ x: Double, _ x0: Double, _ x1: Double, _ p0: Double, _ p1: Double, _ m0: Double, _ m1: Double) -> Double {
            let width = x1 - x0, t = (x - x0) / width, t2 = t * t, t3 = t2 * t
            let m0 = m0 * width, m1 = m1 * width
            return (2 * p0 + m0 - 2 * p1 + m1) * t3 + (-3 * p0 - 2 * m0 + 3 * p1 - m1) * t2 + m0 * t + p0
        }
    }

    static let sampleRate = 48_000.0
    /// Envelope resolution before simplification; well under the 15 ms attack.
    static let hop = 0.005

    /// Build the envelope from every key clip placed on the timeline. Each clip's
    /// own gain (volume and audio fades) shapes the key, as the cloud's `afade`
    /// runs before `asplit` feeds the compressor.
    static func sidechain(keys: [(clip: TimelineClip, url: URL)], duration: Double, compressor: Compressor = Compressor()) async throws -> AudioDuckEnvelope {
        let frames = Int((max(0, duration) * sampleRate).rounded(.up))
        guard frames > 0 else { return AudioDuckEnvelope(points: []) }
        var key = [Float](repeating: 0, count: frames)
        for (clip, url) in keys {
            try Task.checkCancellation()
            let pcm = try await readMonoLevels(url: url, start: clip.sourceStart, duration: clip.sourceDuration)
            let offset = Int((clip.timelineStart * sampleRate).rounded())
            let length = Double(pcm.count) / sampleRate
            let fadeIn = min(clip.audioFadeIn ?? 0, length / 2), fadeOut = min(clip.audioFadeOut ?? 0, length / 2)
            for index in pcm.indices where offset + index >= 0 && offset + index < frames {
                let t = Double(index) / sampleRate
                var level = Float(clip.volume)
                if fadeIn > 0, t < fadeIn { level *= Float(t / fadeIn) }
                if fadeOut > 0, t > length - fadeOut { level *= Float(max(0, (length - t) / fadeOut)) }
                key[offset + index] = max(key[offset + index], pcm[index] * level)
            }
        }
        try Task.checkCancellation()
        let gains = compressor.gains(forKey: key, sampleRate: sampleRate)
        // Minimum per hop: never let the bed peek above the compressor inside a hop.
        let hopFrames = Int(hop * sampleRate)
        var samples: [(time: Double, gain: Double)] = [(0, Double(gains.first ?? 1))]
        var index = 0
        while index < gains.count {
            let end = min(gains.count, index + hopFrames)
            samples.append((Double(end) / sampleRate, Double(gains[index..<end].min() ?? 1)))
            index = end
        }
        return AudioDuckEnvelope(points: simplify(samples, tolerance: 0.01))
    }

    /// Drop points a straight line between their neighbours reproduces within
    /// `tolerance` (linear gain), so a one-minute bed gets hundreds of ramps, not
    /// twelve thousand.
    static func simplify(_ points: [(time: Double, gain: Double)], tolerance: Double) -> [(time: Double, gain: Double)] {
        guard points.count > 2 else { return points }
        var kept = [points[0]]
        var anchor = 0
        var candidate = 1
        while candidate < points.count - 1 {
            let next = candidate + 1
            let a = points[anchor], b = points[next]
            let fits = (anchor + 1...candidate).allSatisfy { inner in
                let p = points[inner]
                let expected = a.gain + (b.gain - a.gain) * (p.time - a.time) / (b.time - a.time)
                return abs(expected - p.gain) <= tolerance
            }
            if !fits { kept.append(points[candidate]); anchor = candidate }
            candidate = next
        }
        kept.append(points[points.count - 1])
        return kept
    }

    /// Channel-averaged absolute level (`link=average`) at 48 kHz.
    private static func readMonoLevels(url: URL, start: Double, duration: Double) async throws -> [Float] {
        let asset = AVURLAsset(url: url)
        guard let track = try await asset.loadTracks(withMediaType: .audio).first else { return [] }
        let reader = try AVAssetReader(asset: asset)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: start, preferredTimescale: 60_000), duration: CMTime(seconds: duration, preferredTimescale: 60_000))
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: floatPCMSettings(channels: 2))
        guard reader.canAdd(output) else { throw MediaEngineError.exportUnavailable }
        reader.add(output)
        guard reader.startReading() else { throw reader.error ?? MediaEngineError.exportUnavailable }
        var levels: [Float] = []
        levels.reserveCapacity(Int(duration * sampleRate))
        while let buffer = output.copyNextSampleBuffer() {
            let samples = try interleavedFloats(buffer)
            var index = 0
            while index + 1 < samples.count { levels.append((abs(samples[index]) + abs(samples[index + 1])) / 2); index += 2 }
        }
        guard reader.status == .completed else { throw reader.error ?? MediaEngineError.exportFailed }
        return levels
    }
}

func floatPCMSettings(channels: Int) -> [String: Any] {
    [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: channels,
     AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false]
}

func interleavedFloats(_ buffer: CMSampleBuffer) throws -> [Float] {
    guard let block = CMSampleBufferGetDataBuffer(buffer) else { return [] }
    let length = CMBlockBufferGetDataLength(block)
    var samples = [Float](repeating: 0, count: length / MemoryLayout<Float>.size)
    let status = samples.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!) }
    guard status == noErr else { throw MediaEngineError.exportFailed }
    return samples
}

/// ITU-R BS.1770-4 integrated loudness of interleaved 48 kHz stereo: K-weighting,
/// 400 ms blocks on a 100 ms step, -70 LUFS absolute and -10 LU relative gates.
/// The same measure ffmpeg's `loudnorm`/`ebur128` report.
struct LoudnessMeter {
    private struct Biquad {
        let b0, b1, b2, a1, a2: Double
        var z1 = 0.0, z2 = 0.0
        mutating func process(_ x: Double) -> Double {
            let y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            return y
        }
    }
    private static func kWeighting() -> [Biquad] {
        [Biquad(b0: 1.53512485958697, b1: -2.69169618940638, b2: 1.19839281085285, a1: -1.69065929318241, a2: 0.73248077421585),
         Biquad(b0: 1, b1: -2, b2: 1, a1: -1.99004745483398, a2: 0.99007225036621)]
    }
    private var filters: [[Biquad]] = [kWeighting(), kWeighting()]
    private var subBlocks: [Double] = []
    private var current = 0.0, currentFrames = 0
    private let framesPerSubBlock = 4_800
    private(set) var samplePeak: Float = 0

    mutating func consume(interleavedStereo samples: [Float]) {
        var index = 0
        while index + 1 < samples.count {
            var power = 0.0
            for channel in 0..<2 {
                let x = Double(samples[index + channel])
                samplePeak = max(samplePeak, abs(samples[index + channel]))
                var y = x
                for stage in 0..<2 { y = filters[channel][stage].process(y) }
                power += y * y
            }
            current += power; currentFrames += 1
            if currentFrames == framesPerSubBlock { subBlocks.append(current / Double(framesPerSubBlock)); current = 0; currentFrames = 0 }
            index += 2
        }
    }

    /// Nil when every block is below the absolute gate (silence).
    var integratedLUFS: Double? {
        guard subBlocks.count >= 4 else { return nil }
        let blocks = (0...(subBlocks.count - 4)).map { start in subBlocks[start..<start + 4].reduce(0, +) / 4 }
        func loudness(_ power: Double) -> Double { -0.691 + 10 * log10(power) }
        let absolute = blocks.filter { $0 > 0 && loudness($0) > -70 }
        guard !absolute.isEmpty else { return nil }
        let relativeGate = loudness(absolute.reduce(0, +) / Double(absolute.count)) - 10
        let gated = absolute.filter { loudness($0) > relativeGate }
        guard !gated.isEmpty else { return nil }
        return loudness(gated.reduce(0, +) / Double(gated.count))
    }

    /// Measure the composition's mixed audio exactly as the writer will read it.
    static func measure(asset: AVAsset, audioMix: AVAudioMix?) async throws -> LoudnessMeter {
        let tracks = try await asset.loadTracks(withMediaType: .audio)
        var meter = LoudnessMeter()
        guard !tracks.isEmpty else { return meter }
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: floatPCMSettings(channels: 2))
        output.audioMix = audioMix
        guard reader.canAdd(output) else { throw MediaEngineError.exportUnavailable }
        reader.add(output)
        guard reader.startReading() else { throw reader.error ?? MediaEngineError.exportUnavailable }
        while let buffer = output.copyNextSampleBuffer() {
            try Task.checkCancellation()
            meter.consume(interleavedStereo: try interleavedFloats(buffer))
        }
        guard reader.status == .completed else { throw reader.error ?? MediaEngineError.exportFailed }
        return meter
    }
}

/// One gain toward the loudness target plus a peak limiter at the cloud's
/// `TP=-1.5` ceiling (sample peak; instant attack, 50 ms release), applied to the
/// writer's 16-bit interleaved stereo PCM.
struct LoudnessNormalizer {
    static let ceiling: Float = pow(10, -1.5 / 20)
    /// Bounds mirror a sane loudnorm: never boost a near-silent mix by more than
    /// 20 dB, never cut by more than 30.
    static let gainRangeDB: ClosedRange<Double> = -30...20
    let gain: Float
    private var reduction: Float = 1
    private let release: Float = 1 - exp(-1 / (0.05 * 48_000))

    init?(target: Double, meter: LoudnessMeter) {
        guard let measured = meter.integratedLUFS else { return nil }
        let db = min(Self.gainRangeDB.upperBound, max(Self.gainRangeDB.lowerBound, target - measured))
        gain = Float(pow(10, db / 20))
    }

    mutating func process(_ samples: inout [Int16]) {
        var index = 0
        while index + 1 < samples.count {
            let left = Float(samples[index]) / 32_768 * gain, right = Float(samples[index + 1]) / 32_768 * gain
            let peak = max(abs(left), abs(right))
            let required: Float = peak > Self.ceiling ? Self.ceiling / peak : 1
            reduction = required < reduction ? required : reduction + (1 - reduction) * release
            reduction = min(reduction, required)
            samples[index] = Int16(clamping: Int((left * reduction * 32_768).rounded()))
            samples[index + 1] = Int16(clamping: Int((right * reduction * 32_768).rounded()))
            index += 2
        }
    }

    /// A copy of `sample` with its 16-bit PCM normalized; timing and format kept.
    mutating func normalized(_ sample: CMSampleBuffer) throws -> CMSampleBuffer {
        guard let block = CMSampleBufferGetDataBuffer(sample), let format = CMSampleBufferGetFormatDescription(sample) else { return sample }
        let length = CMBlockBufferGetDataLength(block)
        var pcm = [Int16](repeating: 0, count: length / 2)
        guard pcm.withUnsafeMutableBytes({ CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!) }) == noErr else { throw MediaEngineError.exportFailed }
        process(&pcm)
        var output: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: length,
                blockAllocator: kCFAllocatorDefault, customBlockSource: nil, offsetToData: 0, dataLength: length,
                flags: kCMBlockBufferAssureMemoryNowFlag, blockBufferOut: &output) == noErr, let output,
              pcm.withUnsafeBytes({ CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: output, offsetIntoDestination: 0, dataLength: length) }) == noErr else {
            throw MediaEngineError.exportFailed
        }
        var result: CMSampleBuffer?
        guard CMAudioSampleBufferCreateReadyWithPacketDescriptions(allocator: kCFAllocatorDefault, dataBuffer: output, formatDescription: format,
                sampleCount: CMSampleBufferGetNumSamples(sample), presentationTimeStamp: CMSampleBufferGetPresentationTimeStamp(sample),
                packetDescriptions: nil, sampleBufferOut: &result) == noErr, let result else { throw MediaEngineError.exportFailed }
        return result
    }
}
#endif
