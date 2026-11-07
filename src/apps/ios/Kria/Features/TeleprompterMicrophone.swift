import AVFoundation

/// The front microphone sits beside the front camera and picks up its per-frame electrical noise as a buzz at the
/// video frame rate (a "trrr") in the take; the bottom microphone does not (KRI-1182). Only the phone's own
/// microphone is steered. A headset or any other input the creator chose is left alone.
enum TeleprompterMicrophone {
    /// The data source to record from, or nil to leave the route as it is.
    static func preferredSource(
        inputType: AVAudioSession.Port,
        sources: [AVAudioSessionDataSourceDescription]?
    ) -> AVAudioSessionDataSourceDescription? {
        guard inputType == .builtInMic else { return nil }
        return sources?.first { $0.orientation == .bottom }
    }

    static func preferBottom(on session: AVAudioSession) {
        #if DEBUG
        // Bench lever (scripts/ios/talk-bench.sh): KRIA_TALK_BENCH_MIC=app records the old way for an A/B in one session.
        if TalkBench.isEnabled, ProcessInfo.processInfo.environment["KRIA_TALK_BENCH_MIC"] == "app" {
            let chosen = session.currentRoute.inputs.first?.selectedDataSource
            TalkBench.shared.fact("mic.selected", "\(chosen?.dataSourceName ?? "none")/\(chosen?.orientation?.rawValue ?? "-")")
            return
        }
        #endif
        guard let input = session.currentRoute.inputs.first,
              let bottom = preferredSource(inputType: input.portType, sources: input.dataSources),
              input.selectedDataSource?.dataSourceID != bottom.dataSourceID else { return }
        try? input.setPreferredDataSource(bottom)
        try? session.setPreferredInput(input)
        #if DEBUG
        if TalkBench.isEnabled {
            let chosen = session.currentRoute.inputs.first?.selectedDataSource
            TalkBench.shared.fact("mic.selected", "\(chosen?.dataSourceName ?? "none")/\(chosen?.orientation?.rawValue ?? "-")")
        }
        #endif
    }
}
