import KriaMediaEngine
import SwiftUI

/// The In / Out / Loop effect cards, preview toggle and speed slider. The Text panel and the
/// caption panel's Animation tab both draw this, so text and captions offer the same motion.
struct NativeAnimationPhasePicker<Session: NativeTransactionControlling>: View {
    enum Phase: String, CaseIterable {
        case entrance = "In", exit = "Out", loop = "Loop"
        var key: String { self == .entrance ? "entrance" : self == .exit ? "exit" : "loop" }
        /// (stored value, card title): In and Out offer every edge, the six designed ones included.
        var effects: [(value: String, title: String)] {
            self == .loop
                ? ["None", "Pulse", "Bounce", "Float"].map { ($0.lowercased(), $0) }
                : TextAnimationPhases.Edge.allCases.map { ($0.rawValue, $0.title) }
        }
    }

    @ObservedObject var session: Session
    @ObservedObject var clock: NativeTextAnimationPreviewClock
    /// The `animation_phases` shown as selected: `entrance`, `exit`, `loop`, `speed`.
    let phases: [String: JSONValue]
    /// `native-editor-text-animation` gives `…-phase-In` tabs and `…-preview-toggle`.
    let identifierPrefix: String
    let speedNote: String
    let usesAccessibilityLayout: Bool
    let onEffect: (_ phase: String, _ effect: String) -> Void
    let onSpeed: (Double) -> Void
    @State private var phase: Phase = .entrance
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var speed: Double { phases["speed"]?.numberValue ?? 1 }
    private var selectedEffect: String { phases[phase.key]?.stringValue ?? "none" }

    var body: some View {
        VStack(spacing: 14) {
            NativeEditorPanelTabs(tabs: Phase.allCases, selection: $phase, accessibilityPrefix: identifierPrefix + "-phase")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: usesAccessibilityLayout ? 118 : 92), spacing: 8)], spacing: 10) {
                ForEach(phase.effects, id: \.value) { card(value: $0.value, title: $0.title) }
            }
            Button {
                clock.toggleManually()
            } label: {
                Label(clock.isPlaying ? "Pause previews" : "Play previews", systemImage: clock.isPlaying ? "pause.fill" : "play.fill")
                    .font(KriaFont.body(13).weight(.semibold))
                    .frame(minHeight: 44)
            }
            .accessibilityIdentifier(identifierPrefix + "-preview-toggle")
            if usesAccessibilityLayout {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Speed")
                    speedControl
                }
            } else {
                HStack {
                    Text("Speed").frame(width: 68, alignment: .leading)
                    speedControl
                }
            }
            Text(speedNote)
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear { startPreviewIfAllowed() }
        .onDisappear { clock.pauseForLifecycle() }
        .onChange(of: scenePhase) { _, next in
            if next == .active { startPreviewIfAllowed() } else { clock.pauseForLifecycle() }
        }
        .onChange(of: reduceMotion) { _, enabled in
            if enabled { clock.pauseForLifecycle() } else { startPreviewIfAllowed() }
        }
    }

    private func card(value: String, title: String) -> some View {
        let selected = selectedEffect == value
        return Button { onEffect(phase.key, value) } label: {
            VStack(spacing: 6) {
                NativeTextAnimationPreview(phase: phase.key, effect: value, speed: speed, clock: clock)
                    .frame(maxWidth: .infinity, minHeight: 52)
                    .background(selected ? KriaColor.selectionSoft : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 9))
                Text(title).font(KriaFont.body(11).weight(selected ? .semibold : .regular))
                    .lineLimit(2).multilineTextAlignment(.center)
            }
        }
        .accessibilityLabel("\(phase.rawValue) animation \(title)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var speedControl: some View {
        HStack {
            NativeEditorSlider(session: session, value: Binding(get: { speed }, set: onSpeed), in: 0.25...3, step: 0.25) {
                Text("Animation speed")
            }
            Text("\(speed.formatted())×").frame(minWidth: 42)
        }
    }

    private func startPreviewIfAllowed() {
        guard NativeTextAnimationPreviewAutoplay.allows(reduceMotion: reduceMotion,
                                                         environment: ProcessInfo.processInfo.environment) else { return }
        clock.startAutoplay()
    }
}
