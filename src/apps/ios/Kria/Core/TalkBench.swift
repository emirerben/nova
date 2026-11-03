#if DEBUG
import AVFoundation
import KriaMediaEngine
import UIKit

/// Talk-to-camera bench recorder (`scripts/ios/talk-bench.sh`). Off unless the app is launched with `KRIA_TALK_BENCH=1`.
///
/// `Perf` signposts are the source of truth for timing on a phone you can attach Instruments to. A scripted run has no
/// Instruments, so while this is on every `Perf` interval, count and sample, plus the engine's, is also written to
/// `Documents/talk-bench/summary.json` in the app container, which the script pulls with `devicectl`. A Debug-only
/// file: a Release build has no recorder, only the signposts and the `recorder_take_finished` diagnostic.
///
/// `t` is milliseconds since this process started recording; the script uses it to attribute a `talk.hash` to the
/// phase (`talk.use.import`, `talk.save.publishHash`, ...) it happened inside.
final class TalkBench: @unchecked Sendable {
    static let shared = TalkBench()
    static let isEnabled = ProcessInfo.processInfo.environment["KRIA_TALK_BENCH"] == "1"

    /// The longest series kept per name. A 60 s preview delivers about 1,800 frames; this is ten times that.
    private static let seriesCap = 20_000
    private static let flushInterval: TimeInterval = 2

    private let lock = NSLock()
    private let writeQueue = DispatchQueue(label: "app.kria.talk-bench.write", qos: .utility)
    private let originNanos = DispatchTime.now().uptimeNanoseconds
    private var opened: [String: UInt64] = [:]
    private var intervals: [String: [[Any]]] = [:]
    private var counters: [String: [Double]] = [:]
    private var facts: [String: String] = [:]
    private var truncated = Set<String>()
    private var flushScheduled = false
    private var takeCount = 0

    private var directory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appending(path: "talk-bench", directoryHint: .isDirectory)
    }

    /// Called once from `KriaApp.init`. Starts from an empty folder so a run never mixes with the last one.
    static func startIfRequested() {
        guard isEnabled else { return }
        let bench = shared
        try? FileManager.default.removeItem(at: bench.directory)
        try? FileManager.default.createDirectory(at: bench.directory, withIntermediateDirectories: true)
        EngineSignpost.observer = { name, milliseconds, detail in
            if let milliseconds { bench.record(name, milliseconds: milliseconds, endedNow: true, detail: detail) }
            else { bench.count(name, detail: detail) }
        }
        bench.fact("env.background", ProcessInfo.processInfo.environment["KRIA_TALK_BENCH_BACKGROUND"] ?? "none")
        bench.fact("env.cinematic", ProcessInfo.processInfo.environment["KRIA_TALK_BENCH_CINEMATIC"] ?? "0")
        var system = utsname()
        uname(&system)
        bench.fact("device.machine", withUnsafeBytes(of: &system.machine) { String(decoding: $0.prefix(while: { $0 != 0 }), as: UTF8.self) })
        bench.fact("os.version", ProcessInfo.processInfo.operatingSystemVersionString)
        bench.recordThermal(initial: true)
        bench.recordFreeSpace(key: "disk.free_mb.start")
        NotificationCenter.default.addObserver(forName: ProcessInfo.thermalStateDidChangeNotification, object: nil, queue: nil) { _ in
            bench.recordThermal(changed: true)
        }
        NotificationCenter.default.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: nil) { _ in
            bench.flushNow()
        }
    }

    // MARK: Recording

    func began(_ key: String) {
        guard Self.isEnabled else { return }
        lock.lock(); opened[key] = DispatchTime.now().uptimeNanoseconds; lock.unlock()
    }

    func ended(_ key: String, name: String, detail: String) {
        guard Self.isEnabled else { return }
        lock.lock(); let start = opened.removeValue(forKey: key); lock.unlock()
        guard let start else { return }
        append(name, startNanos: start, endNanos: DispatchTime.now().uptimeNanoseconds, detail: detail)
    }

    func finished(_ name: String, since startNanos: UInt64, detail: String) {
        guard Self.isEnabled else { return }
        append(name, startNanos: startNanos, endNanos: DispatchTime.now().uptimeNanoseconds, detail: detail)
    }

    /// A value that is not a span of work. Stored like an interval whose start is the moment it was measured.
    func sample(_ name: String, milliseconds: Double, detail: String) {
        guard Self.isEnabled else { return }
        record(name, milliseconds: milliseconds, endedNow: false, detail: detail)
    }

    func count(_ name: String, detail: String) {
        guard Self.isEnabled else { return }
        let now = nowMilliseconds
        lock.lock()
        tally(name, at: now)
        if !detail.isEmpty { tally("\(name).\(detail)", at: now) }
        lock.unlock()
        scheduleFlush()
    }

    func fact(_ key: String, _ value: String) {
        guard Self.isEnabled else { return }
        lock.lock(); facts[key] = value; lock.unlock()
        scheduleFlush()
    }

    /// Keeps a finished take next to the numbers so ffprobe and the spectrogram run on the file the app wrote. A hard link:
    /// no second write of the movie, and the copy does not compete with the work being measured.
    func keepTake(_ url: URL) {
        guard Self.isEnabled else { return }
        lock.lock(); takeCount += 1; let index = takeCount; lock.unlock()
        let destination = directory.appending(path: "take-\(index).\(url.pathExtension.isEmpty ? "mov" : url.pathExtension)")
        if (try? FileManager.default.linkItem(at: url, to: destination)) == nil { try? FileManager.default.copyItem(at: url, to: destination) }
    }

    /// `thermal.start` is the state at launch, `thermal.end` the latest state at each flush, `thermal.max` the worst seen. A
    /// run that began on a warm phone is not comparable to one that began cool, so the report carries all three.
    private func recordThermal(initial: Bool = false, changed: Bool = false) {
        let name = Self.thermalName(ProcessInfo.processInfo.thermalState)
        lock.lock()
        let worst = Self.thermalRank(facts["thermal.max"] ?? "nominal")
        if initial { facts["thermal.start"] = name }
        if Self.thermalRank(name) >= worst { facts["thermal.max"] = name }
        facts["thermal.end"] = name
        facts["power.low_power_mode"] = ProcessInfo.processInfo.isLowPowerModeEnabled ? "1" : "0"
        lock.unlock()
        if changed { count("talk.thermal.changed", detail: name) }
    }

    /// Free space as the system would report it to the app, in MB. The install of the next run needs about 180 MB of it.
    private func recordFreeSpace(key: String) {
        let values = try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        guard let bytes = values?.volumeAvailableCapacityForImportantUsage else { return }
        lock.lock(); facts[key] = String(bytes / 1_000_000); lock.unlock()
    }

    private static func thermalName(_ state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal: "nominal"
        case .fair: "fair"
        case .serious: "serious"
        case .critical: "critical"
        @unknown default: "unknown"
        }
    }

    private static func thermalRank(_ name: String) -> Int { ["nominal": 0, "fair": 1, "serious": 2, "critical": 3][name] ?? 0 }

    func flushNow() {
        guard Self.isEnabled else { return }
        writeQueue.sync { write() }
    }

    // MARK: Storage

    private var nowMilliseconds: Double { Double(DispatchTime.now().uptimeNanoseconds - originNanos) / 1_000_000 }

    private func record(_ name: String, milliseconds: Double, endedNow: Bool, detail: String) {
        let end = DispatchTime.now().uptimeNanoseconds
        let start = endedNow ? end - UInt64(max(0, milliseconds) * 1_000_000) : end
        append(name, startNanos: start, endNanos: start + UInt64(max(0, milliseconds) * 1_000_000), detail: detail)
    }

    private func append(_ name: String, startNanos: UInt64, endNanos: UInt64, detail: String) {
        let start = Double(startNanos &- originNanos) / 1_000_000
        let duration = Double(endNanos &- startNanos) / 1_000_000
        lock.lock()
        if intervals[name, default: []].count < Self.seriesCap {
            intervals[name, default: []].append([start, duration, detail])
        } else { truncated.insert(name) }
        lock.unlock()
        scheduleFlush()
    }

    private func tally(_ name: String, at time: Double) {
        if counters[name, default: []].count < Self.seriesCap { counters[name, default: []].append(time) } else { truncated.insert(name) }
    }

    private func scheduleFlush() {
        lock.lock()
        let already = flushScheduled
        flushScheduled = true
        lock.unlock()
        guard !already else { return }
        writeQueue.asyncAfter(deadline: .now() + Self.flushInterval) { [self] in write() }
    }

    private func write() {
        recordThermal()
        recordFreeSpace(key: "disk.free_mb.end")
        lock.lock()
        flushScheduled = false
        var document: [String: Any] = [
            "intervals": intervals, "counters": counters, "facts": facts,
            "truncated": Array(truncated).sorted(), "elapsed_ms": nowMilliseconds,
        ]
        lock.unlock()
        document["written"] = ISO8601DateFormatter().string(from: Date())
        guard let data = try? JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]) else { return }
        try? data.write(to: directory.appending(path: "summary.json"), options: .atomic)
    }
}

extension TeleprompterRecordingFlow {
    /// The scenario's background, whatever the store restored: `none` clears it, `photo` and `video` replace it.
    func installBenchBackground() async {
        guard TalkBench.isEnabled, speakerBackgroundEnabled else { return }
        guard let kind = TalkBenchFixtures.requestedBackground else {
            if background != nil { clearBackground() }
            return
        }
        do {
            let began = DispatchTime.now().uptimeNanoseconds
            let file = kind == .image ? try TalkBenchFixtures.makePhoto() : try await TalkBenchFixtures.makeVideo()
            TalkBench.shared.finished("talk.bench.backgroundMake", since: began, detail: kind.rawValue)
            chooseBackground(from: file, kind: kind)
        } catch {
            backgroundNote = "The bench could not make its \(kind.rawValue) background: \(error.localizedDescription)"
        }
    }
}

/// Backgrounds for a scripted run: nobody can pick from Photos in an unattended test, so the bench makes one.
enum TalkBenchFixtures {
    enum Failure: Error { case writerUnavailable }

    static var requestedBackground: SpeakerBackgroundKind? {
        switch ProcessInfo.processInfo.environment["KRIA_TALK_BENCH_BACKGROUND"] {
        case "photo": .image
        case "video": .video
        default: nil
        }
    }

    /// A busy still: bars and a gradient, so a trailing mask edge has something to show against.
    static func makePhoto() throws -> URL {
        let size = CGSize(width: 1080, height: 1920)
        let image = UIGraphicsImageRenderer(size: size).image { context in
            draw(in: context.cgContext, size: size, phase: 0)
        }
        let url = FileManager.default.temporaryDirectory.appending(path: "talk-bench-background.jpg")
        try image.jpegData(compressionQuality: 0.9)?.write(to: url, options: .atomic)
        return url
    }

    /// Ten seconds of 720x1280 H.264 with bars sweeping across, so the looping video background has real motion to decode.
    static func makeVideo() async throws -> URL {
        let size = CGSize(width: 720, height: 1280)
        let url = FileManager.default.temporaryDirectory.appending(path: "talk-bench-background.mp4")
        try? FileManager.default.removeItem(at: url)
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: Int(size.width), AVVideoHeightKey: Int(size.height),
        ])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: Int(size.width), kCVPixelBufferHeightKey as String: Int(size.height),
        ])
        guard writer.canAdd(input) else { throw Failure.writerUnavailable }
        writer.add(input)
        guard writer.startWriting() else { throw writer.error ?? Failure.writerUnavailable }
        writer.startSession(atSourceTime: .zero)
        let frames = 300
        for frame in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            guard let pool = adaptor.pixelBufferPool else { throw Failure.writerUnavailable }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer)
            guard let buffer else { throw Failure.writerUnavailable }
            CVPixelBufferLockBaseAddress(buffer, [])
            if let context = CGContext(data: CVPixelBufferGetBaseAddress(buffer), width: Int(size.width), height: Int(size.height),
                                       bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buffer), space: CGColorSpaceCreateDeviceRGB(),
                                       bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) {
                draw(in: context, size: size, phase: CGFloat(frame) / CGFloat(frames))
            }
            CVPixelBufferUnlockBaseAddress(buffer, [])
            guard adaptor.append(buffer, withPresentationTime: CMTime(value: CMTimeValue(frame), timescale: 30)) else {
                throw writer.error ?? Failure.writerUnavailable
            }
        }
        input.markAsFinished()
        await writer.finishWriting()
        guard writer.status == .completed else { throw writer.error ?? Failure.writerUnavailable }
        return url
    }

    private static func draw(in context: CGContext, size: CGSize, phase: CGFloat) {
        let colors = [UIColor(red: 0.1, green: 0.55, blue: 0.3, alpha: 1).cgColor, UIColor(red: 0.05, green: 0.2, blue: 0.5, alpha: 1).cgColor]
        if let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors as CFArray, locations: [0, 1]) {
            context.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: size.width, y: size.height), options: [])
        }
        context.setFillColor(UIColor(white: 1, alpha: 0.35).cgColor)
        let bar = size.width / 8
        for index in 0..<10 {
            let x = (CGFloat(index) * bar * 1.6 + phase * size.width * 1.6).truncatingRemainder(dividingBy: size.width * 1.6) - bar
            context.fill(CGRect(x: x, y: 0, width: bar, height: size.height))
        }
    }
}
#endif
