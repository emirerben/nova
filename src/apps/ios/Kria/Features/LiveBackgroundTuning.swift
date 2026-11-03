import Foundation

/// The five independent levers of the live green-screen preview. Each default is the shipping behaviour. A Debug build can
/// select the previous behaviour of any lever with `KRIA_TALK_BENCH_LIVE_OLD`, so `scripts/ios/talk-bench.sh` can run the
/// same take with a lever on and off on a phone. A Release build always uses the defaults.
struct LiveBackgroundTuning: Equatable, Sendable {
    /// A new Vision request for every frame, as the export does, instead of one request reused for the whole session.
    var freshSegmentationRequest = true
    /// Vision gets a frame whose short side is the mask's short side (384) instead of the camera's full 1080x1920.
    var reducedSegmentationInput = true
    /// The camera frame is shown at once and the newest finished mask is applied to it, instead of waiting for the mask of
    /// the same frame.
    var asyncSegmentation = true
    /// A video background loops through `AVPlayerLooper`, instead of seeking back to zero on the end notification.
    var loopedVideoBackground = true
    /// A video background is decoded at the size the preview draws it, instead of full-size BGRA. A separate lever from the
    /// loop so a bench A/B can tell which of the two changed the numbers.
    var drawnSizeVideoDecode = true

    /// The values this process runs with.
    static let current = resolve(environment: ProcessInfo.processInfo.environment)

    static func resolve(environment: [String: String]) -> LiveBackgroundTuning {
        #if DEBUG
        guard let list = environment[overrideKey], !list.isEmpty else { return LiveBackgroundTuning() }
        let parsed = parse(oldBehaviours: list)
        assert(parsed.unknown.isEmpty, "\(overrideKey) has unknown names \(parsed.unknown); use \(names.joined(separator: ", ")) or all")
        return parsed.tuning
        #else
        return LiveBackgroundTuning()
        #endif
    }
}

#if DEBUG
extension LiveBackgroundTuning {
    static let overrideKey = "KRIA_TALK_BENCH_LIVE_OLD"
    static let names = ["fresh", "input", "async", "video", "decode"]

    /// `all` or a comma-separated list of lever names, each turned back to its previous behaviour.
    static func parse(oldBehaviours list: String) -> (tuning: LiveBackgroundTuning, unknown: [String]) {
        var tuning = LiveBackgroundTuning()
        var unknown: [String] = []
        for token in list.split(separator: ",").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            switch token {
            case "all": tuning = LiveBackgroundTuning(freshSegmentationRequest: false, reducedSegmentationInput: false,
                                                      asyncSegmentation: false, loopedVideoBackground: false,
                                                      drawnSizeVideoDecode: false)
            case "fresh": tuning.freshSegmentationRequest = false
            case "input": tuning.reducedSegmentationInput = false
            case "async": tuning.asyncSegmentation = false
            case "video": tuning.loopedVideoBackground = false
            case "decode": tuning.drawnSizeVideoDecode = false
            default: unknown.append(token)
            }
        }
        return (tuning, unknown)
    }

    /// One line for the bench report, so each arm of an A/B is provable from its own summary.
    var summary: String {
        "fresh=\(freshSegmentationRequest ? 1 : 0) input=\(reducedSegmentationInput ? 1 : 0) async=\(asyncSegmentation ? 1 : 0) video=\(loopedVideoBackground ? 1 : 0) decode=\(drawnSizeVideoDecode ? 1 : 0)"
    }
}
#endif
