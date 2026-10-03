import AVFoundation
import SwiftUI

/// Peak extraction + drawing for the trim bar. Peaks are normalized 0...1 and cached per
/// sound; any failure leaves the bar on its plain duration strip.
enum NativeSfxWaveformPeaks {
    static let bucketCount = 64

    /// Collapses raw peak samples into `buckets` bars normalized to the loudest bar.
    static func normalize(_ peaks: [Float], buckets: Int = bucketCount) -> [Float] {
        guard !peaks.isEmpty, buckets > 0 else { return [] }
        var bars = [Float](repeating: 0, count: buckets)
        for (index, value) in peaks.enumerated() {
            let bucket = min(buckets - 1, index * buckets / peaks.count)
            bars[bucket] = max(bars[bucket], abs(value))
        }
        let loudest = bars.max() ?? 0
        guard loudest > 0 else { return [] }
        return bars.map { $0 / loudest }
    }

    /// Decodes `url` (downloading remote audio to the caches dir first) into normalized bars.
    static func load(url: URL) async -> [Float]? {
        do {
            var local = url
            if !url.isFileURL {
                let (temp, _) = try await URLSession.shared.download(from: url)
                let ext = url.pathExtension.isEmpty ? "m4a" : url.pathExtension
                local = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + "." + ext)
                try FileManager.default.moveItem(at: temp, to: local)
            }
            defer { if !url.isFileURL { try? FileManager.default.removeItem(at: local) } }
            let asset = AVURLAsset(url: local)
            guard let track = try await asset.loadTracks(withMediaType: .audio).first else { return nil }
            let reader = try AVAssetReader(asset: asset)
            let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
                AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false, AVNumberOfChannelsKey: 1,
            ])
            reader.add(output)
            guard reader.startReading() else { return nil }
            var peaks: [Float] = []
            while let buffer = output.copyNextSampleBuffer(), let block = CMSampleBufferGetDataBuffer(buffer) {
                let length = CMBlockBufferGetDataLength(block)
                var data = Data(count: length)
                data.withUnsafeMutableBytes { _ = CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!) }
                data.withUnsafeBytes { raw in
                    let samples = raw.bindMemory(to: Int16.self)
                    var index = 0
                    while index < samples.count {
                        let end = min(samples.count, index + 512)
                        var peak: Int16 = 0
                        for i in index..<end { peak = max(peak, samples[i] == .min ? .max : abs(samples[i])) }
                        peaks.append(Float(peak) / Float(Int16.max))
                        index = end
                    }
                }
            }
            let bars = normalize(peaks)
            return bars.isEmpty ? nil : bars
        } catch {
            return nil
        }
    }
}

@MainActor
final class NativeSfxWaveformStore: ObservableObject {
    @Published private(set) var bars: [String: [Float]] = [:]
    private var inFlight: Set<String> = []
    private var failed: Set<String> = []

    func load(key: String, url: URL?) async {
        guard let url, bars[key] == nil, !inFlight.contains(key), !failed.contains(key) else { return }
        inFlight.insert(key)
        defer { inFlight.remove(key) }
        if let result = await NativeSfxWaveformPeaks.load(url: url) { bars[key] = result } else { failed.insert(key) }
    }
}

struct NativeSfxWaveformBars: View {
    let bars: [Float]
    var body: some View {
        GeometryReader { geo in
            let count = max(1, bars.count)
            let step = geo.size.width / CGFloat(count)
            Path { path in
                for (index, bar) in bars.enumerated() {
                    let height = max(2, CGFloat(bar) * geo.size.height * 0.8)
                    let x = CGFloat(index) * step + step / 2
                    path.move(to: CGPoint(x: x, y: (geo.size.height - height) / 2))
                    path.addLine(to: CGPoint(x: x, y: (geo.size.height + height) / 2))
                }
            }
            .stroke(KriaColor.ink, style: StrokeStyle(lineWidth: max(1.5, step * 0.5), lineCap: .round))
        }
        .accessibilityHidden(true)
    }
}
