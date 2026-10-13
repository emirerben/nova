import Accelerate
import AVFoundation
import Foundation

/// What a recorded take actually sounds like (KRI-653).
///
/// A 192 s talk-to-camera recording came back with a digitally silent audio track and the recorder
/// called it valid because the track existed. The thresholds match the server's presence gate
/// (`audio_presence` in `clip_speech.py`) so the phone and the API agree on what "silent" means:
/// digital silence is far below -60 dBFS while a quiet room with someone talking has windows well
/// above -45 dBFS.
struct TakeAudioCheck: Codable, Equatable, Sendable {
    enum Verdict: String, Codable, Sendable {
        /// Sound is present.
        case ok
        /// Something was recorded but almost nothing reaches speech level. A warning, never a block.
        case quiet
        /// Nothing was recorded. The take cannot be used for a talking video.
        case silent
        /// The file could not be measured. Never blocks the creator.
        case unknown
    }

    var verdict: Verdict
    var peakDb: Float
    /// Share of 100 ms windows at or above `activeWindowDb`, in 0...1.
    var activeFraction: Float

    static let silentPeakDb: Float = -60
    static let activeWindowDb: Float = -45
    static let minimumActiveFraction: Float = 0.02
    static let windowSamples = 1_600  // 100 ms at 16 kHz
    static let sampleRate: Double = 16_000
    /// Under this a take is too short to call "quiet" (a one-word clip is fine).
    static let minimumJudgedDuration: TimeInterval = 3

    static let unknown = TakeAudioCheck(verdict: .unknown, peakDb: 0, activeFraction: 0)
}

/// Streams Float32 mono samples into 100 ms windows. Pure, so it is unit-testable without a movie.
struct AudioLevelAccumulator {
    private(set) var peak: Float = 0
    private(set) var windowCount = 0
    private(set) var activeWindowCount = 0
    private var filled = 0
    private var windowSumSquares: Float = 0
    private let windowSamples: Int

    init(windowSamples: Int = TakeAudioCheck.windowSamples) { self.windowSamples = windowSamples }

    mutating func ingest(_ samples: UnsafeBufferPointer<Float>) {
        var offset = 0
        while offset < samples.count {
            let count = min(windowSamples - filled, samples.count - offset)
            guard let base = samples.baseAddress else { return }
            var slicePeak: Float = 0
            var sliceSumSquares: Float = 0
            vDSP_maxmgv(base + offset, 1, &slicePeak, vDSP_Length(count))
            vDSP_svesq(base + offset, 1, &sliceSumSquares, vDSP_Length(count))
            peak = max(peak, slicePeak)
            windowSumSquares += sliceSumSquares
            filled += count
            offset += count
            if filled == windowSamples { closeWindow() }
        }
    }

    private mutating func closeWindow() {
        let rms = (windowSumSquares / Float(windowSamples)).squareRoot()
        windowCount += 1
        if Self.decibels(rms) >= TakeAudioCheck.activeWindowDb { activeWindowCount += 1 }
        filled = 0
        windowSumSquares = 0
    }

    func result(duration: TimeInterval) -> TakeAudioCheck {
        // A trailing partial window is ignored, but a clip shorter than one window has no verdict.
        guard windowCount > 0 else { return .unknown }
        let peakDb = Self.decibels(peak)
        let fraction = Float(activeWindowCount) / Float(windowCount)
        let verdict: TakeAudioCheck.Verdict
        if peakDb < TakeAudioCheck.silentPeakDb {
            verdict = .silent
        } else if duration >= TakeAudioCheck.minimumJudgedDuration, fraction < TakeAudioCheck.minimumActiveFraction {
            verdict = .quiet
        } else {
            verdict = .ok
        }
        return TakeAudioCheck(verdict: verdict, peakDb: peakDb, activeFraction: fraction)
    }

    static func decibels(_ linear: Float) -> Float { 20 * log10(max(linear, 1e-9)) }
}

enum TakeAudioMeasurement {
    /// Decodes the movie's audio to 16 kHz mono Float32 and classifies it. Never throws: anything it
    /// cannot read is `.unknown`, which never blocks a creator (the server gate is the backstop).
    static func measure(url: URL) async -> TakeAudioCheck {
        await Task.detached(priority: .userInitiated) { () -> TakeAudioCheck in
            let asset = AVURLAsset(url: url)
            guard let track = try? await asset.loadTracks(withMediaType: .audio).first,
                  let duration = try? await asset.load(.duration).seconds, duration.isFinite,
                  let reader = try? AVAssetReader(asset: asset) else { return .unknown }
            let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: TakeAudioCheck.sampleRate,
                AVNumberOfChannelsKey: 1,
                AVLinearPCMBitDepthKey: 32,
                AVLinearPCMIsFloatKey: true,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false,
            ])
            output.alwaysCopiesSampleData = false
            guard reader.canAdd(output) else { return .unknown }
            reader.add(output)
            guard reader.startReading() else { return .unknown }
            var accumulator = AudioLevelAccumulator()
            while let buffer = output.copyNextSampleBuffer() {
                guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
                let length = CMBlockBufferGetDataLength(block)
                var data = [Float](repeating: 0, count: length / MemoryLayout<Float>.size)
                let status = data.withUnsafeMutableBytes {
                    CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!)
                }
                guard status == kCMBlockBufferNoErr else { return .unknown }
                data.withUnsafeBufferPointer { accumulator.ingest($0) }
            }
            guard reader.status == .completed else { return .unknown }
            return accumulator.result(duration: duration)
        }.value
    }
}

/// Decides, from a stream of live meter readings, that the microphone is not delivering sound.
///
/// `nil` means the connection reports no channels yet: unknown, never silent, because the post-record
/// file check is the authority. A dead mic reports about -160 dB; a quiet room is -50...-60 dB.
struct MicSilenceMonitor {
    static let silentBelowDb: Float = -70
    static let recoveredAboveDb: Float = -60
    /// Silence must persist this long before the warning appears.
    static let silentFor: TimeInterval = 3
    /// Sound must persist this long before the warning clears.
    static let recoveredFor: TimeInterval = 0.5

    private(set) var isSilent = false
    private var silentSince: TimeInterval?
    private var soundSince: TimeInterval?

    mutating func reset() { isSilent = false; silentSince = nil; soundSince = nil }

    /// - Returns: whether the warning is showing after this reading.
    @discardableResult
    mutating func ingest(level: Float?, at time: TimeInterval) -> Bool {
        guard let level else { return isSilent }
        if level < Self.silentBelowDb {
            soundSince = nil
            silentSince = silentSince ?? time
            if time - (silentSince ?? time) >= Self.silentFor { isSilent = true }
        } else if level >= Self.recoveredAboveDb {
            silentSince = nil
            soundSince = soundSince ?? time
            if isSilent, time - (soundSince ?? time) >= Self.recoveredFor { isSilent = false }
        } else {
            // Between the thresholds: not provably silent, not clearly recovered.
            silentSince = nil
        }
        return isSilent
    }
}
